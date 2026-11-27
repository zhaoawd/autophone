'use strict';
// M0b-3 — NSURLSession discovery（guest 侧，编译后由 python spawn-gate 加载）。
//
// 目标：把抓取层四个不确定性钉死，全部**只读、转发透明**（response 侧 wrap block 也始终
// 原样 call orig，不改 app 行为）：
//   1. 命中面    —— -[NSURLSessionTask resume] 覆盖哪些 task 子类；NSURLConnection / AFNetworking 是否在用
//   2. 响应侧    —— app 走 completion block 还是 session delegate（决定 §4.1 响应 hook）
//   3. body 可读 —— req/resp body 是否明文可读，含 gzip(Content-Encoding) / stream body 三种
//   4. 关联键    —— task_ptr 在 req(resume) 与 resp(completion) 两处是否同一、稳定
//
// 不落库、不重放、不拦截放行；只 send 结构化事件，host 侧汇总（见 net_discovery.py）。

import ObjC from 'frida-objc-bridge';

const C = ObjC.classes;
const NSHTTPURLResponse = C.NSHTTPURLResponse;

function nstr(o) {
  try { return (o && !o.isNull()) ? o.toString() : null; } catch (e) { return null; }
}

// NSDictionary<NSString,NSString> → JS 的 key 列表（只取 key，value 可能含敏感头，不外带）
function headerKeys(dict) {
  var keys = [];
  try {
    if (!dict || dict.isNull()) return keys;
    var d = new ObjC.Object(dict);
    var e = d.keyEnumerator();
    var k;
    while ((k = e.nextObject()) !== null) keys.push(k.toString());
  } catch (e) { /* ignore */ }
  return keys;
}

function headerValue(dict, name) {
  try {
    if (!dict || dict.isNull()) return null;
    var d = new ObjC.Object(dict);
    var v = d.objectForKey_(name);
    return v ? v.toString() : null;
  } catch (e) { return null; }
}

// frida 把 ObjC 的 NSUInteger(64位) 返回值序列化成字符串，统一转成 JS number
function intOf(v) { var n = parseInt('' + v, 10); return isNaN(n) ? -1 : n; }

function readReq(reqPtr) {
  try {
    if (!reqPtr || reqPtr.isNull()) return null;
    var req = new ObjC.Object(reqPtr);
    var url = req.URL();
    var headers = req.allHTTPHeaderFields();
    var body = req.HTTPBody();
    var stream = req.HTTPBodyStream();
    var hasBody = body && !body.isNull();
    var hasStream = stream && !stream.isNull();
    return {
      url: url ? nstr(url.absoluteString()) : null,
      host: url ? nstr(url.host()) : null,
      method: nstr(req.HTTPMethod()),
      header_keys: headerKeys(headers),
      content_encoding: headerValue(headers, 'Content-Encoding'),
      content_type: headerValue(headers, 'Content-Type'),
      body_len: hasBody ? intOf(new ObjC.Object(body).length()) : -1,
      body_stream: !!hasStream,          // stream/upload body → §4.3 坑1，HTTPBody 读不到
    };
  } catch (e) { return { err: '' + e }; }
}

// ── ① + ④ 请求侧：hook -[NSURLSessionTask resume]（基类，覆盖所有 task 子类）──────
(function hookResume() {
  try {
    var m = C.NSURLSessionTask['- resume'];
    Interceptor.attach(m.implementation, {
      onEnter: function (args) {
        try {
          var taskPtr = args[0];
          var task = new ObjC.Object(taskPtr);
          var req = null;
          try { req = task.currentRequest(); } catch (e) {}
          if (!req || req.isNull()) { try { req = task.originalRequest(); } catch (e) {} }
          var tid = null;
          try { tid = task.taskIdentifier(); } catch (e) {}
          send({
            ev: 'req', side: 'resume',
            cls: task.$className,
            task_ptr: taskPtr.toString(),
            tid: tid,
            req: readReq(req ? req.handle : null),
            ts: Date.now()
          });
        } catch (e) { send({ ev: 'req_err', err: '' + e }); }
      }
    });
    send({ ev: 'hook_ok', what: 'resume' });
  } catch (e) { send({ ev: 'hook_err', what: 'resume', err: '' + e }); }
})();

// ── ② + ③ + ④ 响应侧（completion 路）：hook dataTask...completionHandler: ───────
// onLeave 拿 task_ptr，wrap completion block 读 response（始终原样转发）。
function readResp(taskPtr, dataPtr, respPtr, errPtr) {
  try {
    var status = null, respHeaders = [], contentEncoding = null, bodyLen = -1;
    if (respPtr && !respPtr.isNull()) {
      var r = new ObjC.Object(respPtr);
      if (r.isKindOfClass_(NSHTTPURLResponse)) {
        status = r.statusCode();
        respHeaders = headerKeys(r.allHeaderFields());
        contentEncoding = headerValue(r.allHeaderFields(), 'Content-Encoding');
      }
    }
    if (dataPtr && !dataPtr.isNull()) bodyLen = intOf(new ObjC.Object(dataPtr).length());
    var err = (errPtr && !errPtr.isNull()) ? nstr(new ObjC.Object(errPtr)) : null;
    send({
      ev: 'resp', side: 'completion',
      task_ptr: taskPtr,
      status: status, resp_header_keys: respHeaders,
      content_encoding: contentEncoding,    // 服务端 gzip 时这里非空 → §4.3 坑1 验证
      body_len: bodyLen, error: err, ts: Date.now()
    });
  } catch (e) { send({ ev: 'resp_err', task_ptr: taskPtr, err: '' + e }); }
}

['- dataTaskWithRequest:completionHandler:',
 '- dataTaskWithURL:completionHandler:',
 '- uploadTaskWithRequest:fromData:completionHandler:'
].forEach(function (sel) {
  try {
    var m = C.NSURLSession[sel];
    if (!m) return;
    // block 参数索引：self(0) _cmd(1) request/url(2) [fromData(3)] completion(末位)
    var blockIdx = (sel.indexOf('fromData') >= 0) ? 4 : 3;
    Interceptor.attach(m.implementation, {
      // 关键：必须在 onEnter wrap——NSURLSession 在方法内会 Block_copy 完成块，
      // 等 onLeave 再改原块，调用的是已拷贝的副本（旧 invoke），resp 永远抓不到。
      // 在 onEnter 改 invoke 指针，拷贝会带上它。task_ptr 此刻未知，用共享 ctx 在 onLeave 回填。
      onEnter: function (args) {
        var bp = args[blockIdx];
        this.bp = bp;
        this.ctx = { task_ptr: null };
        var ctx = this.ctx;
        try {
          if (bp && !bp.isNull()) {
            var blk = new ObjC.Block(bp);
            var orig = blk.implementation;
            blk.implementation = function (data, response, error) {
              readResp(ctx.task_ptr, data, response, error);  // 只读
              return orig(data, response, error);              // 原样转发
            };
          }
        } catch (e) { send({ ev: 'wrap_err', sel: sel, err: '' + e }); }
      },
      onLeave: function (retval) {
        this.ctx.task_ptr = retval.toString();
        send({ ev: 'create', sel: sel, task_ptr: this.ctx.task_ptr,
               has_completion: !!(this.bp && !this.bp.isNull()) });
      }
    });
    send({ ev: 'hook_ok', what: sel });
  } catch (e) { send({ ev: 'hook_err', what: sel, err: '' + e }); }
});

// 同时探 delegate 路：app 若用 delegate，记下 delegate 类，供 §4.1 决策
['+ sessionWithConfiguration:delegate:delegateQueue:',
 '- initWithConfiguration:delegate:delegateQueue:'
].forEach(function (sel) {
  try {
    var owner = C.NSURLSession;
    var m = owner[sel];
    if (!m) return;
    Interceptor.attach(m.implementation, {
      onEnter: function (args) {
        try {
          var delg = args[3];
          var cls = (delg && !delg.isNull()) ? new ObjC.Object(delg).$className : null;
          send({ ev: 'session_delegate', sel: sel, delegate_cls: cls });
        } catch (e) {}
      }
    });
  } catch (e) {}
});

// 命中面旁证：老接口 / 三方栈是否在用（只报类是否在场 + 关键方法）
send({
  ev: 'surface',
  NSURLConnection: !!C.NSURLConnection,
  AFHTTPSessionManager: !!C.AFHTTPSessionManager,
  AFURLSessionManager: !!C.AFURLSessionManager,
});

rpc.exports = { ping: function () { return 'pong'; } };
send({ ev: 'loaded', ts: Date.now() });
