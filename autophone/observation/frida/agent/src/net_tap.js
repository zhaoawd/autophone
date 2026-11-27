'use strict';
// M1 — 抓取层生产版（guest 侧，编译后由 python 长连 spawn-gate / attach 加载）。
//
// 在 M0b discovery（net_discovery.js）已钉死的 hook 点上，补 M1 缺口：**body 字节**。
// discovery 只外带元数据（header_keys / body_len），M1 要含明文 body 的 JSONL，
// 故每条 req/resp 把 body 经 base64 带出（超 BODY_CAP 截断）。
//
// 抓取侧只读、转发透明：completion block 始终原样 call orig，不改 app 行为。
// req+resp 不在 guest 合并——各自 send，host 按 task_ptr 合并（§4.2），响应不来也能看到孤立 req。
// 关联层 marker：rpc.exports.mark(case_id, step, operation?) → send 一条 {ev:'mark'}（§六）。
//
// ── 接口场景拦截（docs/api-scenario-interception.md）───────────────────────────────
// 同一个 agent 兼「只读抓取」与「受控拦截」（§二：不能起第二个 Frida session）。
// rpc.exports.configureScenario(compiled) 下发 host 侧编译好的规则；resume 短路点匹配规则、
// 合成响应/超时并**不发真网络**（§6.2）。规则为空时 resume 行为与纯抓取严格一致（零风险回归）。
// scenarioHealth() 诚实上报：① 短路 hook 是否装到活 NSURLSession 类 ② attach 前覆盖缺口（§三/§八）。

import ObjC from 'frida-objc-bridge';
import { httpResponseStatus } from './http_response_status.mjs';
import { guardDelegateCallbacks } from './delegate_invocation_guard.mjs';
import {
  createDelegateBodyAccumulator,
  planDelegateBodyChunk,
  recordDelegateBodyChunk,
} from './delegate_body_accumulator.mjs';
import { deliverLocal } from './controlled_delivery.mjs';
import { planDelegateShortCircuit } from './scenario_delegate.mjs';

const C = ObjC.classes;
const NSHTTPURLResponse = C.NSHTTPURLResponse;

// body 软上限：超过只带前 N 字节 + 标记 truncated（M4 治理会细化阈值/sha256，这里先防灌爆）。
const BODY_CAP = 256 * 1024;

// 抓取开关（页面/编排可运行时暂停-恢复，见 rpc.setEnabled）。关闭时 req/resp 不外带，
// 但 mark 事件始终发——动作归因不受开关影响。
let CAPTURING = true;

var CAPTURE_ID = String(Process.id) + ':' + Date.now() + ':' + Math.random().toString(36).slice(2);
var EVENT_SEQ = 0, TASK_SEQ = 0, CAPTURE_SEALED = false;
var TASK_INSTANCES = {}, TASK_BY_PTR = {}, LIFECYCLE_HOOKS = {}, TASK_CREATION_HOOKS = {};
var DELEGATE_CAPTURE_HOOKS = {};

function emitAgentEvent(event) {
  if (CAPTURE_SEALED) return null;
  event.capture_id = CAPTURE_ID;
  event.event_seq = ++EVENT_SEQ;
  if (event.task_ptr && !event.task_instance_id) {
    var task = TASK_BY_PTR[String(event.task_ptr)];
    if (task) event.task_instance_id = task.task_instance_id;
  }
  send(event);
  return event;
}


function taskSnapshot(taskPtr) {
  var snapshot = { state: null, tid: null, bytes_received: null, bytes_sent: null,
    status: null, error_code: null, error_domain: null, native_response_present: null, snapshot_errors: [] };
  try {
    var task = new ObjC.Object(typeof taskPtr === 'string' ? ptr(taskPtr) : taskPtr);
    [['state', 'state'], ['tid', 'taskIdentifier'], ['bytes_received', 'countOfBytesReceived'],
     ['bytes_sent', 'countOfBytesSent']].forEach(function (pair) {
      try { snapshot[pair[0]] = Number(task[pair[1]]()); }
      catch (e) { snapshot.snapshot_errors.push(pair[0]); }
    });
    try {
      var response = task.response();
      snapshot.native_response_present = !!(response && !response.isNull());
      if (snapshot.native_response_present) snapshot.status = httpResponseStatus(response);
    } catch (e) { snapshot.snapshot_errors.push('status'); }
    try {
      var error = task.error();
      if (error && !error.isNull()) {
        snapshot.error_code = Number(error.code());
        snapshot.error_domain = String(error.domain());
      }
    } catch (e) { snapshot.snapshot_errors.push('error_code'); }
  } catch (e) { snapshot.snapshot_errors.push('task'); }
  return snapshot;
}

function observeTask(taskPtr, created, sessionPtr) {
  var key = String(taskPtr), snapshot = taskSnapshot(taskPtr);
  var current = TASK_BY_PTR[key];
  if (!created && current && current.tid !== null && snapshot.tid !== null
      && current.tid === snapshot.tid) return current;
  var task = { task_ptr: key, task_instance_id: CAPTURE_ID + ':task:' + (++TASK_SEQ),
    terminal: false, terminal_source: null, callback_complete: false, cancel_requested: false, request_observed: false,
    response_observed: false, data_callbacks: 0, observed_data_bytes: 0,
    identity_source: created ? 'creation' : 'first_observation',
    identity_ambiguous: snapshot.tid === null || !!(current && !current.terminal
      && !(created && current.controlled_local && current.synthetic_callback_complete
        && current.state === 1 && current.bytes_sent === 0 && current.bytes_received === 0
        && current.native_response_present === false && current.snapshot_errors.length === 0)),
    coverage_gap: !created || snapshot.state !== 1, hook_coverage: [],
    delegate_coverage_gap: true, delegate_hook_coverage: [] };
  Object.assign(task, snapshot);
  TASK_BY_PTR[key] = task;
  TASK_INSTANCES[task.task_instance_id] = task;
  installTaskLifecycleHooks(taskPtr, task);
  installObservedTaskDelegateHooks(taskPtr, task, sessionPtr);
  emitTaskLifecycle(task, 'task_observed');
  return task;
}

function observeCreatedTask(taskPtr, sessionPtr) {
  var current = TASK_BY_PTR[String(taskPtr)], snapshot = taskSnapshot(taskPtr);
  // NSURLSession factories may call another observed factory internally.
  if (current && current.identity_source === 'creation' && !current.terminal
      && !current.request_observed && snapshot.tid !== null && snapshot.tid === current.tid) return current;
  return observeTask(taskPtr, true, sessionPtr);
}

function installTaskCreationHooks() {
  var errors = 0;
  ['- dataTaskWithRequest:', '- dataTaskWithURL:', '- uploadTaskWithRequest:fromData:',
   '- uploadTaskWithRequest:fromFile:', '- downloadTaskWithRequest:', '- downloadTaskWithURL:']
    .forEach(function (selector) {
      try {
        var method = C.NSURLSession[selector];
        if (!method || TASK_CREATION_HOOKS[String(method.implementation)]) return;
        Interceptor.attach(method.implementation, {
          onEnter: function (args) { this.capture_session_ptr = args[0]; },
          onLeave: function (retval) {
            if (!CAPTURING || CAPTURE_SEALED || retval.isNull()) return;
            try { observeCreatedTask(retval, this.capture_session_ptr); }
            catch (e) { emitAgentEvent({ ev: 'lifecycle_err', what: 'task_creation', ts: Date.now() }); }
          }
        });
        TASK_CREATION_HOOKS[String(method.implementation)] = selector;
        emitAgentEvent({ ev: 'hook_ok', what: 'task_creation', selector: selector,
          implementation: String(method.implementation), ts: Date.now() });
      } catch (e) {
        errors++;
        emitAgentEvent({ ev: 'hook_err', what: 'task_creation', selector: selector });
      }
    });
  return { count: Object.keys(TASK_CREATION_HOOKS).length, errors: errors };
}

function installCaptureLifecycleHooks() {
  installPassiveResumeHook();
  var creation = installTaskCreationHooks();
  var result = { ok: RESUME_HOOK_MODE === 'passive' && creation.count > 0 && creation.errors === 0,
    resume_mode: RESUME_HOOK_MODE, creation_hook_count: creation.count,
    creation_hook_errors: creation.errors, scope: 'named_foundation_task_creation', class_enumeration: false };
  emitAgentEvent(Object.assign({ ev: result.ok ? 'lifecycle_coverage_ready' : 'lifecycle_coverage_failed',
    ts: Date.now() }, result));
  return result;
}

function installObservedTaskDelegateHooks(taskPtr, record, sessionPtr) {
  record.delegate_coverage_gap = true;
  record.delegate_hook_coverage = [];
  try {
    var session;
    if (sessionPtr) {
      session = new ObjC.Object(typeof sessionPtr === 'string' ? ptr(sessionPtr) : sessionPtr);
    } else {
      var task = new ObjC.Object(typeof taskPtr === 'string' ? ptr(taskPtr) : taskPtr);
      session = task.session ? task.session() : null;
    }
    if (!session || session.isNull()) return;
    var delegate = session.delegate();
    if (!delegate || delegate.isNull()) return;
    var cls = delegate.$class;
    var className = delegate.$className;
    [['- URLSession:dataTask:didReceiveResponse:completionHandler:', 'resp'],
     ['- URLSession:dataTask:didReceiveData:', 'data'],
     ['- URLSession:task:didCompleteWithError:', 'done']].forEach(function (pair) {
      var coverage = { selector: pair[0], kind: pair[1], class_name: className,
        source: 'observed_session_delegate', implementation: null, installed: false };
      try {
        var method = cls[pair[0]];
        if (method) {
          coverage.implementation = String(method.implementation);
          hookDelegateImpl(method, pair[1]);
          coverage.installed = !!DELEGATE_CAPTURE_HOOKS[pair[1] + ':' + coverage.implementation];
        }
      } catch (e) { coverage.error = 'delegate_hook_install_failed'; }
      record.delegate_hook_coverage.push(coverage);
    });
    record.delegate_coverage_gap = !(record.identity_source === 'creation' && record.state === 1
      && record.delegate_hook_coverage.length === 3
      && record.delegate_hook_coverage.every(function (coverage) { return coverage.installed; }));
  } catch (e) { record.delegate_coverage_gap = true; }
}

function emitTaskLifecycle(task, kind, details) {
  if (CAPTURE_SEALED) return;
  Object.assign(task, taskSnapshot(task.task_ptr));
  if (kind === 'cancel_requested') task.cancel_requested = true;
  if (kind === 'response_observed') task.response_observed = true;
  if (kind === 'data_observed') {
    task.data_callbacks++;
    task.observed_data_bytes += Number(details && details.data_bytes) || 0;
  }
  if (kind === 'completion' || kind === 'delegate_done') task.callback_complete = true;
  if (kind === 'completion' || kind === 'delegate_done' ||
      (kind === 'state_observed' && task.state === 3)) {
    task.terminal = task.state === 3;
    task.terminal_source = task.terminal ? kind : null;
  }
  emitAgentEvent(Object.assign({ ev: 'task_lifecycle', kind: kind, ts: Date.now() }, task, details || {}));
}

// Only inspect the observed concrete task and its ancestry. No global class enumeration.
function installTaskLifecycleHooks(taskPtr, record) {
  try {
    var cls = new ObjC.Object(typeof taskPtr === 'string' ? ptr(taskPtr) : taskPtr).$class;
    var visited = {};
    while (cls && !visited[String(cls)]) {
      visited[String(cls)] = true;
      ['- cancel', '- setState:'].forEach(function (selector) {
        try {
          var method = cls[selector];
          if (!method) return;
          var address = String(method.implementation), key = selector + ':' + address;
          if (!LIFECYCLE_HOOKS[key]) {
            Interceptor.attach(method.implementation, {
              onEnter: function (args) {
                this.task_ptr = args[0];
                var tracked = TASK_BY_PTR[String(args[0])];
                if (tracked && selector === '- cancel') emitTaskLifecycle(tracked, 'cancel_requested');
              },
              onLeave: function () {
                var tracked = TASK_BY_PTR[String(this.task_ptr)];
                if (tracked && selector === '- setState:') emitTaskLifecycle(tracked, 'state_observed');
              }
            });
            LIFECYCLE_HOOKS[key] = { selector: selector, implementation: address, installed: true };
          }
          if (!record.hook_coverage.some(function (item) { return item.selector === selector && item.implementation === address; })) {
            record.hook_coverage.push(LIFECYCLE_HOOKS[key]);
          }
        } catch (e) {
          record.hook_coverage.push({ selector: selector, installed: false, error: 'hook_install_failed' });
        }
      });
      cls = cls.$superClass;
    }
  } catch (e) { record.coverage_gap = true; }
  if (!['- cancel', '- setState:'].every(function (selector) {
    return record.hook_coverage.some(function (item) { return item.selector === selector && item.installed; });
  })) record.coverage_gap = true;
}

function sealCapture(timeoutMs, nonce) {
  var deadline = Date.now() + Math.min(5000, Math.max(0, Number(timeoutMs) || 0));
  return new Promise(function (resolve) {
    function finish() {
      var pending = Object.keys(TASK_INSTANCES).map(function (id) { return TASK_INSTANCES[id]; })
        .filter(function (task) {
          var cancelledWithoutResponse = task.terminal && task.state === 3
            && task.error_code === -999 && task.error_domain === 'NSURLErrorDomain'
            && task.native_response_present === false && task.bytes_received === 0
            && task.response_observed === false && task.data_callbacks === 0
            && task.observed_data_bytes === 0 && task.snapshot_errors.length === 0;
          if (task.controlled_local) return task.delivery_pending || !task.synthetic_callback_complete;
          return task.request_observed && (task.state !== 3 || !task.terminal
            || (!task.callback_complete && !cancelledWithoutResponse));
        });
      if (pending.length && Date.now() < deadline) { setTimeout(finish, 10); return; }
      var ack = emitAgentEvent({ ev: 'capture_sealed', nonce: nonce || null, ts: Date.now(),
        pending: pending, hook_coverage: Object.keys(LIFECYCLE_HOOKS).map(function (k) { return LIFECYCLE_HOOKS[k]; }),
        task_count: Object.keys(TASK_INSTANCES).length, ok: true });
      CAPTURE_SEALED = true;
      CAPTURING = false;
      resolve(ack);
    }
    finish();
  });
}

// App-scoped capture policy.  The default is full capture.  A caller may
// configure known static/tile URLs as drops and keep bodies only for selected
// domains.  Unknown URLs remain as metadata so a changed callback endpoint is
// still discoverable without copying every map payload.
let CAPTURE_FILTER = {
  allow_url_substrings: [],
  deny_url_substrings: [],
  body_url_substrings: [],
};
let TASK_CAPTURE_POLICIES = {};

function normalizedStrings(values) {
  if (!Array.isArray(values)) return [];
  return values.map(function (value) {
    return String(value || '').trim().toLowerCase();
  }).filter(function (value) { return value.length > 0; });
}

function capturePolicy(url) {
  const value = String(url || '').toLowerCase();
  if (CAPTURE_FILTER.deny_url_substrings.some(function (part) {
    return value.indexOf(part) >= 0;
  })) return 'drop';
  if (
    CAPTURE_FILTER.allow_url_substrings.length > 0
    && !CAPTURE_FILTER.allow_url_substrings.some(function (part) {
      return value.indexOf(part) >= 0;
    })
  ) return 'drop';
  if (
    CAPTURE_FILTER.body_url_substrings.length > 0
    && !CAPTURE_FILTER.body_url_substrings.some(function (part) {
      return value.indexOf(part) >= 0;
    })
  ) return 'metadata';
  return 'full';
}

function rememberCapturePolicy(taskPtr, url) {
  const key = String(taskPtr || '');
  const policy = capturePolicy(url);
  if (key) TASK_CAPTURE_POLICIES[key] = policy;
  return policy;
}

function capturePolicyForTask(taskPtr) {
  const key = String(taskPtr || '');
  if (key && TASK_CAPTURE_POLICIES[key]) return TASK_CAPTURE_POLICIES[key];
  return rememberCapturePolicy(key, taskRequestUrl(taskPtr));
}

function forgetCapturePolicy(taskPtr) {
  delete TASK_CAPTURE_POLICIES[String(taskPtr || '')];
}

function nstr(o) {
  try { return (o && !o.isNull()) ? o.toString() : null; } catch (e) { return null; }
}

function headerDict(dict) {
  var out = {};
  try {
    if (!dict || dict.isNull()) return out;
    var d = new ObjC.Object(dict);
    var e = d.keyEnumerator();
    var k;
    while ((k = e.nextObject()) !== null) {
      var v = d.objectForKey_(k);
      out[k.toString()] = v ? v.toString() : null;
    }
  } catch (e) { /* ignore */ }
  return out;
}

function headerValue(dict, name) {
  try {
    if (!dict || dict.isNull()) return null;
    var d = new ObjC.Object(dict);
    var v = d.objectForKey_(name);
    return v ? v.toString() : null;
  } catch (e) { return null; }
}

function intOf(v) { var n = parseInt('' + v, 10); return isNaN(n) ? -1 : n; }

// NSData* → {b64: string|null, len, truncated}。
// 已聚合的 NSData 使用 Objective-C 自带 -base64EncodedStringWithOptions: 导出，body
// 直接进入 JSON payload（不走 send 二进制旁路）。delegate 分片在回调有效期内另走
// copyDataBytes() 复制为 JS-owned ArrayBuffer。超 BODY_CAP 先 subdataWithRange: 截断。
function dataBytes(nsdata, includeBody) {
  if (nsdata == null) return { b64: null, len: -1, truncated: false };
  var d;
  try {
    if (nsdata instanceof ObjC.Object) d = nsdata;
    else { if (nsdata.isNull && nsdata.isNull()) return { b64: null, len: -1, truncated: false }; d = new ObjC.Object(nsdata); }
  } catch (e) { return { b64: null, len: -3, truncated: false, err: 'wrap:' + e }; }
  var len;
  try { len = intOf(d.length()); }
  catch (e) { return { b64: null, len: -4, truncated: false, err: 'length:' + e }; }
  if (len <= 0) return { b64: null, len: len, truncated: false };
  if (includeBody === false) {
    return {
      b64: null,
      len: len,
      truncated: false,
      filtered: true,
    };
  }
  try {
    var src = d, truncated = false;
    if (len > BODY_CAP) { src = d.subdataWithRange_([0, BODY_CAP]); truncated = true; }
    var b64 = src.base64EncodedStringWithOptions_(0);
    return { b64: b64 ? b64.toString() : null, len: len, truncated: truncated };
  } catch (e) { return { b64: null, len: -5, truncated: false, err: 'b64:' + e }; }
}

function readReqMeta(reqPtr) {
  try {
    if (!reqPtr || reqPtr.isNull()) return null;
    var req = new ObjC.Object(reqPtr);
    var url = req.URL();
    var headers = req.allHTTPHeaderFields();
    var stream = req.HTTPBodyStream();
    var hasStream = stream && !stream.isNull();
    return {
      url: url ? nstr(url.absoluteString()) : null,
      host: url ? nstr(url.host()) : null,
      method: nstr(req.HTTPMethod()),
      headers: headerDict(headers),
      content_encoding: headerValue(headers, 'Content-Encoding'),
      content_type: headerValue(headers, 'Content-Type'),
      body_stream: !!hasStream,   // stream/upload → HTTPBody 读不到（§4.3 坑1），body 旁路会是空
    };
  } catch (e) { return { err: '' + e }; }
}

// ════════════════════════════════════════════════════════════════════════════════
//  接口场景拦截：状态 + Block ABI 原语（§四~§八）
// ════════════════════════════════════════════════════════════════════════════════

// host 侧编译产物（compiled_rules，见 autophone/platform/scenario/rules.py）。空 = 纯抓取。
var SCENARIO_RULES = [];
// 规则命中计数器（跨 task 累加，configureScenario / close 重置）。key = rule_id。
var SCENARIO_COUNTS = {};
// 挂载模型（'attach' | 'spawn'），host 下发；仅用于 health 诚实上报覆盖缺口，不改拦截逻辑。
var SCENARIO_MODE = null;
var SCENARIO_CONTROLLED = false, SCENARIO_HASH = null;
var CONTROLLED_DELIVERIES = {};
// task_ptr → 保留的 completion block 拷贝（Block_copy）。拦截时用它合成回调、之后 Block_release。
var COMPLETION_BLOCKS = {};
// 短路 hook 是否成功装到活 NSURLSessionTask.resume（scenarioHealth ① 判据）。
var RESUME_HOOK_OK = false;
var RESUME_HOOK_MODE = 'none';
var RESUME_LISTENER = null;

// Block ABI（64-bit）：isa(0x00) flags+reserved(0x08) invoke(0x10) descriptor(0x18)。
// frida-objc-bridge 的 ObjC.Block 在不同版本上「调用块」的 API 不稳，直接按 ABI 取 invoke 指针
// 自建 NativeFunction 调用最可靠、版本无关。completion 块签名：void(^)(NSData*, NSURLResponse*, NSError*)。
var BLOCK_INVOKE_OFFSET = Process.pointerSize === 8 ? 0x10 : 0x0c;
// 跨 frida 版本解析全局 C 导出：frida 16+ 移除了静态 Module.getExportByName(null,name)（17.x 上为
// undefined → "not a function"），改用 Module.getGlobalExportByName(name)。两者都试，旧版兜底。
function _globalExport(name) {
  if (typeof Module.getGlobalExportByName === 'function') return Module.getGlobalExportByName(name);
  if (typeof Module.getExportByName === 'function') return Module.getExportByName(null, name);
  throw new Error('no global export resolver on this frida');
}
var _Block_copy = null, _Block_release = null;
try {
  _Block_copy = new NativeFunction(_globalExport('_Block_copy'), 'pointer', ['pointer']);
  _Block_release = new NativeFunction(_globalExport('_Block_release'), 'void', ['pointer']);
} catch (e) { emitAgentEvent({ ev: 'scenario_warn', what: 'block_runtime', err: '' + e }); }

function retainBlock(bp) {
  try { return (_Block_copy && bp && !bp.isNull()) ? _Block_copy(bp) : null; }
  catch (e) { return null; }
}
function releaseBlock(bp) {
  try { if (_Block_release && bp && !bp.isNull()) _Block_release(bp); } catch (e) {}
}

// 调用一个 completion block（bp 为 block 指针）：void(^)(NSData*, NSURLResponse*, NSError*)。
function invokeCompletion(bp, dataPtr, respPtr, errPtr) {
  try {
    var invokePtr = bp.add(BLOCK_INVOKE_OFFSET).readPointer();
    var fn = new NativeFunction(invokePtr, 'void', ['pointer', 'pointer', 'pointer', 'pointer']);
    fn(bp, dataPtr || NULL, respPtr || NULL, errPtr || NULL);
    return true;
  } catch (e) { emitAgentEvent({ ev: 'scenario_err', what: 'invoke', err: '' + e }); return false; }
}

// 规则匹配（§6.2）：method + host + path + query 全等/子集，按 SCENARIO_RULES 顺序首个「且计数未耗尽」命中。
// meta = readReqMeta 结果（含 url/host/method）。返回命中规则或 null。
function matchRule(meta) {
  if (!meta || !SCENARIO_RULES.length) return null;
  var method = (meta.method || '').toUpperCase();
  var host = meta.host || '';
  var path = '', query = {};
  try {
    if (meta.url) {
      var u = meta.url;
      var q = u.indexOf('?');
      var pathPart = q >= 0 ? u.substring(0, q) : u;
      // 去掉 scheme://host 前缀 → path
      var m = pathPart.match(/^[a-z]+:\/\/[^/]+(\/[^?#]*)?/i);
      path = m ? (m[1] || '/') : pathPart;
      if (q >= 0) {
        var qs = u.substring(q + 1);
        qs.split('&').forEach(function (kv) {
          if (!kv) return;
          var i = kv.indexOf('=');
          var k = i >= 0 ? kv.substring(0, i) : kv;
          var v = i >= 0 ? kv.substring(i + 1) : '';
          try { query[decodeURIComponent(k)] = decodeURIComponent(v); } catch (e) { query[k] = v; }
        });
      }
    }
  } catch (e) { /* 解析失败 → 仅按 method/host 粗匹配 */ }

  for (var idx = 0; idx < SCENARIO_RULES.length; idx++) {
    var r = SCENARIO_RULES[idx];
    var mt = r.match || {};
    if (mt.exact_url && mt.exact_url !== meta.url) continue;
    if (mt.method && mt.method.toUpperCase() !== method) continue;
    if (mt.host && mt.host !== host) continue;
    if (mt.path && mt.path !== path) continue;
    if (mt.path_prefix && path.indexOf(mt.path_prefix) !== 0) continue;
    if (mt.path_suffix && path.substring(path.length - mt.path_suffix.length) !== mt.path_suffix) continue;
    if (mt.query) {
      var ok = true;
      for (var qk in mt.query) {
        if (('' + query[qk]) !== ('' + mt.query[qk])) { ok = false; break; }
      }
      if (!ok) continue;
    }
    // 计数闸：first_n 且已达上限 → 该规则耗尽，继续找下一条（§七 生效次数）。
    var times = r.times || { mode: 'always' };
    var used = SCENARIO_COUNTS[r.rule_id] || 0;
    if (times.mode === 'first_n' && used >= (times.n || 0)) continue;
    return r;
  }
  return null;
}

// 合成一个 NSHTTPURLResponse（fixed_json 用）。失败返回 null（调用方降级为无 response）。
function synthResponse(urlObj, status, headers) {
  try {
    var hdr = C.NSMutableDictionary.dictionary();
    hdr.setObject_forKey_('application/json', 'Content-Type');
    if (headers) {
      for (var k in headers) hdr.setObject_forKey_('' + headers[k], '' + k);
    }
    var resp = C.NSHTTPURLResponse.alloc()
      .initWithURL_statusCode_HTTPVersion_headerFields_(urlObj, status || 200, 'HTTP/1.1', hdr);
    return resp;
  } catch (e) { emitAgentEvent({ ev: 'scenario_err', what: 'synth_resp', err: '' + e }); return null; }
}

function synthData(jsonText) {
  try {
    var s = C.NSString.stringWithString_('' + (jsonText == null ? '' : jsonText));
    return s.dataUsingEncoding_(4);  // NSUTF8StringEncoding
  } catch (e) { emitAgentEvent({ ev: 'scenario_err', what: 'synth_data', err: '' + e }); return null; }
}

function synthTimeoutError() {
  try {
    // NSURLErrorDomain / NSURLErrorTimedOut(-1001)。立即回调、无悬挂状态（§一 第一期只做立即超时）。
    return C.NSError.errorWithDomain_code_userInfo_('NSURLErrorDomain', -1001, NULL);
  } catch (e) { emitAgentEvent({ ev: 'scenario_err', what: 'synth_err', err: '' + e }); return null; }
}

// 命中：写命中事件并按 task/delegate 形态选效果路。
// 返回 true 表示使用本地回调。调用方根据运行模式处理未应用的规则。
function applyControlledRule(rule, taskPtr, reqObj) {
  var tp = String(taskPtr), record = TASK_BY_PTR[tp];
  if (!record || record.controlled_local) {
    emitAgentEvent({ ev: 'scenario_err', code: 'controlled_task_identity_invalid', task_ptr: tp });
    return false;
  }
  var bp = COMPLETION_BLOCKS[tp], via = bp ? 'completion' : 'delegate';
  var ordinal = (SCENARIO_COUNTS[rule.rule_id] || 0) + 1;
  SCENARIO_COUNTS[rule.rule_id] = ordinal;
  var binding = { rule_id: rule.rule_id, ordinal: ordinal, task_ptr: tp,
    task_instance_id: record.task_instance_id, controlled: true,
    rule_snapshot_hash: SCENARIO_HASH, via: via };
  var retained = [], blocks = [], blockCopies = [], released = false;
  var state = { retained: retained, blocks: blocks, blockCopies: blockCopies };
  CONTROLLED_DELIVERIES[record.task_instance_id] = state;
  function emit(kind, extra) {
    return emitAgentEvent(Object.assign({ ev: kind, ts: Date.now() }, binding, extra || {}));
  }
  function hold(object) {
    if (!object || object.isNull()) throw new Error('controlled_native_object_missing');
    object.retain(); return own(object);
  }
  function own(object) {
    if (!object || object.isNull()) throw new Error('controlled_native_object_missing');
    retained.push(object); return object;
  }
  function release() {
    if (released) return;
    released = true;
    // Defer release until the active native callback has returned.
    setTimeout(function () {
      for (var i = blockCopies.length - 1; i >= 0; i--) releaseBlock(blockCopies[i]);
      for (var j = retained.length - 1; j >= 0; j--) {
        try { retained[j].release(); } catch (e) {}
      }
      delete CONTROLLED_DELIVERIES[record.task_instance_id];
    }, 0);
  }
  emit('scenario_route', { route: 'local', original_resume_called: false });
  try {
    if (!rule.action || rule.action.type !== 'fixed_json' || rule.action.status !== 200)
      throw new Error('controlled_action_invalid');
    var task = hold(new ObjC.Object(typeof taskPtr === 'string' ? ptr(taskPtr) : taskPtr));
    var request = hold(reqObj), session, delegate, queue = null;
    if (via === 'delegate') {
      session = hold(task.session()); delegate = hold(session.delegate());
      if (isDemultiplexingDelegate(delegate)) throw new Error('controlled_demux_unsupported');
      var selectors = ['URLSession:dataTask:didReceiveResponse:completionHandler:',
        'URLSession:dataTask:didReceiveData:', 'URLSession:task:didCompleteWithError:'];
      if (!selectors.every(function (s) { return !!delegate.respondsToSelector_(ObjC.selector(s)); }))
        throw new Error('controlled_delegate_unavailable');
      try { queue = session.delegateQueue(); } catch (e) {}
      if (queue && !queue.isNull()) hold(queue);
    } else {
      // This is already a Block_copy from the task factory. Transfer its ownership.
      blockCopies.push(bp); delete COMPLETION_BLOCKS[tp];
    }
    record.controlled_local = true;
    record.delivery_pending = true;
    record.synthetic_callback_complete = false;
    emit('scenario_hit', { action: 'fixed_json', applied: true });
    var run = function () {
      state.execution = deliverLocal({ via: via,
        emit: function (phase, extra) {
          if (phase === 'failed') record.delivery_pending = false;
          emit('scenario_delivery', Object.assign({ phase: phase }, extra || {}));
        },
        // alloc/init transfers an owned reference; NSData below is autoreleased.
        makeResponse: function () { return own(synthResponse(request.URL(), 200, rule.action.headers)); },
        makeData: function () { return hold(synthData(rule.action.body)); },
        readData: function (data) { return dataBytes(data, true); },
        response: function (response, acknowledge) {
          var block = new ObjC.Block({ retType: 'void', argTypes: ['int64'], implementation: acknowledge });
          blocks.push(block);
          var copied = retainBlock(block.handle || block);
          if (!copied || (copied.isNull && copied.isNull())) throw new Error('controlled_block_missing');
          blockCopies.push(copied);
          delegate.URLSession_dataTask_didReceiveResponse_completionHandler_(session, task, response, copied);
        },
        data: function (data) { delegate.URLSession_dataTask_didReceiveData_(session, task, data); },
        complete: function () { delegate.URLSession_task_didCompleteWithError_(session, task, NULL); },
        completion: function (data, response) {
          if (!invokeCompletion(bp, data.handle, response.handle, NULL)) throw new Error('controlled_completion_failed');
        },
        success: function () {
          record.synthetic_callback_complete = true;
          record.delivery_pending = false;
          emitTaskLifecycle(record, 'synthetic_completion');
        }, release: release });
    };
    if (queue && !queue.isNull()) {
      var queuedBlock = new ObjC.Block({ retType: 'void', argTypes: [], implementation: run });
      blocks.push(queuedBlock);
      queue.addOperationWithBlock_(queuedBlock);
    } else setTimeout(run, 0);
    return true;
  } catch (e) {
    record.delivery_pending = false;
    emit('scenario_delivery', { phase: 'failed', code: 'controlled_route_unavailable' });
    release();
    return false;
  }
}

// 名称包含 Demux 的分派代理可能依赖原始 task 生命周期，跳过本地响应替换。
function isDemultiplexingDelegate(delegate) {
  return String(delegate.$className || '').indexOf('Demux') >= 0;
}

function applyRule(rule, taskPtr, reqObj) {
  if (SCENARIO_CONTROLLED) return applyControlledRule(rule, taskPtr, reqObj);
  var tp = '' + taskPtr;
  var action = (rule.action && rule.action.type) || 'fixed_json';
  var bp = COMPLETION_BLOCKS[tp];
  if (bp) return applyViaCompletionBlock(rule, tp, bp, reqObj, action);
  return applyViaDelegate(rule, taskPtr, reqObj, action);
}

// ── 路① completion-block：合成 (data,resp,err) 调保留的块（原实现，行为不变）───────────
function applyViaCompletionBlock(rule, tp, bp, reqObj, action) {
  var ordinal = (SCENARIO_COUNTS[rule.rule_id] || 0) + 1;
  var dataPtr = NULL, respPtr = NULL, errPtr = NULL;
  if (action === 'timeout') {
    var err = synthTimeoutError();
    errPtr = err ? err.handle : NULL;
  } else {  // fixed_json（默认）
    var urlObj = null;
    try { urlObj = reqObj ? reqObj.URL() : null; } catch (e) {}
    var resp = synthResponse(urlObj, (rule.action && rule.action.status) || 200,
                             rule.action && rule.action.headers);
    var data = synthData(rule.action && rule.action.body);
    respPtr = resp ? resp.handle : NULL;
    dataPtr = data ? data.handle : NULL;
  }
  // 命中事件在**拦截决策点**落（§8.1）：不等响应——「立即超时」在响应侧不产生任何记录。
  SCENARIO_COUNTS[rule.rule_id] = ordinal;
  emitAgentEvent({ ev: 'scenario_hit', rule_id: rule.rule_id, action: action, applied: true,
         via: 'completion', ordinal: ordinal, task_ptr: tp, ts: Date.now() });
  // 异步回调，脱离 app 的 resume 调用栈（减少重入/持锁风险）。完成块签名固定，见 invokeCompletion。
  var savedBp = bp;
  setTimeout(function () {
    invokeCompletion(savedBp, dataPtr, respPtr, errPtr);
    releaseBlock(savedBp);
  }, 0);
  delete COMPLETION_BLOCKS[tp];
  return true;
}

// ── 路② delegate：无 completion block（RN 等），驱动 app 的 NSURLSessionDataDelegate 回调 ──
//    决策/可行性走纯层 planDelegateShortCircuit（scenario_delegate.mjs，可单测）；这里只做效果侧：
//    取 session/delegate、按 plan 合成对象、脱离 resume 栈在 delegateQueue 上串行驱动 delegate。
function applyViaDelegate(rule, taskPtr, reqObj, action) {
  var tp = '' + taskPtr;
  var task, session, delegate, queue = null;
  try {
    task = new ObjC.Object(taskPtr);
    session = (task.session ? task.session() : null);
    if (!session || session.isNull()) throw new Error('no session');
    delegate = session.delegate();
    if (!delegate || delegate.isNull()) throw new Error('no delegate');
    try { queue = session.delegateQueue(); } catch (e) { queue = null; }
  } catch (e) {
    // delegate 通路取不到（如 session/delegate 为空）→ 与旧 no_completion_block 同策：放行真网络。
    emitAgentEvent({ ev: 'scenario_hit', rule_id: rule.rule_id, action: action, applied: false,
           reason: 'no_delegate', detail: '' + e, task_ptr: tp, ts: Date.now() });
    return false;
  }

  var plan = planDelegateShortCircuit(rule.action, function (sel) {
    try { return !!delegate.respondsToSelector_(ObjC.selector(sel)); } catch (e) { return false; }
  });
  if (!plan.applicable) {
    // delegate 缺必需回调 → 无法安全走完（会吊死/给残缺响应）→ fail-closed 放行真网络。
    emitAgentEvent({ ev: 'scenario_hit', rule_id: rule.rule_id, action: action, applied: false,
           reason: 'delegate_missing_selectors', missing: plan.missing, task_ptr: tp, ts: Date.now() });
    return false;
  }

  if (action === 'fixed_json' && isDemultiplexingDelegate(delegate)) {
    emitAgentEvent({ ev: 'scenario_hit', rule_id: rule.rule_id, action: action, applied: false,
           reason: 'delegate_demux_unsupported', task_ptr: tp, ts: Date.now() });
    return false;
  }

  var ordinal = (SCENARIO_COUNTS[rule.rule_id] || 0) + 1;
  SCENARIO_COUNTS[rule.rule_id] = ordinal;
  // applied=true 只表示「已决定短路且已接受调度」，不冒充效果侧已送达。真正执行结果另发
  // scenario_delivery started/step/completed|failed，host 的穿透探针必须看到 completed 才判绿。
  emitAgentEvent({ ev: 'scenario_hit', rule_id: rule.rule_id, action: action, applied: true,
         via: 'delegate', delivery: 'scheduled', ordinal: ordinal, task_ptr: tp, ts: Date.now() });

  // 脱离 resume 调用栈驱动 delegate：优先在 session.delegateQueue 上串行执行（与 app 真实回调
  // 同一队列，避免线程/重入假设被破坏）；无队列则 setTimeout 兜底（与 completion 路一致）。
  var delivery = {
    rule_id: rule.rule_id,
    action: action,
    ordinal: ordinal,
    task_ptr: tp,
    delegate_class: delegate.$className || null,
    queue_class: queue && !queue.isNull() ? (queue.$className || null) : null
  };
  var run = function () { driveDelegate(plan, delegate, session, task, reqObj, delivery); };
  var enqueued = false;
  if (queue && !queue.isNull()) {
    try {
      var blk = new ObjC.Block({ retType: 'void', argTypes: [], implementation: run });
      queue.addOperationWithBlock_(blk);
      enqueued = true;
    } catch (e) { emitAgentEvent({ ev: 'scenario_warn', what: 'delegate_enqueue', err: '' + e }); }
  }
  if (!enqueued) setTimeout(run, 0);
  return true;
}

// 按 plan 顺序合成对象并调 delegate 回调。签名对齐 NSURLSessionDataDelegate。
function deliveryEvent(delivery, phase, extra) {
  var ev = {
    ev: 'scenario_delivery',
    phase: phase,
    rule_id: delivery.rule_id,
    action: delivery.action,
    ordinal: delivery.ordinal,
    task_ptr: delivery.task_ptr,
    delegate_class: delivery.delegate_class,
    queue_class: delivery.queue_class,
    ts: Date.now()
  };
  extra = extra || {};
  for (var k in extra) ev[k] = extra[k];
  emitAgentEvent(ev);
}

function driveDelegate(plan, delegate, session, task, reqObj, delivery) {
  try {
    deliveryEvent(delivery, 'started', { steps: plan.steps.length });
    var urlObj = null;
    try { urlObj = reqObj ? reqObj.URL() : null; } catch (e) {}
    for (var i = 0; i < plan.steps.length; i++) {
      var step = plan.steps[i];
      if (step.kind === 'response') {
        var resp = synthResponse(urlObj, step.status, step.headers);
        if (!resp) { emitAgentEvent({ ev: 'scenario_err', what: 'drive_resp' }); continue; }
        // completionHandler: void(^)(NSURLSessionResponseDisposition)。我们扮演 URL loading system，
        // delegate 会回调它表态 Allow/Cancel——我们只驱动自己这套，故给个 no-op 块即可。
        // Disposition=NSInteger=arm64 int64；**不能用 'long'**（frida-objc-bridge 别名表无 long，会崩构造）。
        var dispositionCalled = false, disposition = null;
        var noop = new ObjC.Block({
          retType: 'void', argTypes: ['int64'],
          implementation: function (d) {
            dispositionCalled = true;
            disposition = Number(d);
            deliveryEvent(delivery, 'response_disposition', { disposition: disposition });
          }
        });
        delegate.URLSession_dataTask_didReceiveResponse_completionHandler_(session, task, resp, noop);
        deliveryEvent(delivery, 'step', {
          step: step.kind, index: i, response_completion_called: dispositionCalled,
          disposition: disposition
        });
      } else if (step.kind === 'data') {
        var data = synthData(step.body);
        if (data) delegate.URLSession_dataTask_didReceiveData_(session, task, data);
        deliveryEvent(delivery, 'step', { step: step.kind, index: i, body_length: step.body.length });
      } else if (step.kind === 'complete') {
        var errObj = NULL;
        if (step.error) {
          var e = C.NSError.errorWithDomain_code_userInfo_(step.error.domain, step.error.code, NULL);
          if (e) errObj = e;
        }
        delegate.URLSession_task_didCompleteWithError_(session, task, errObj);
        deliveryEvent(delivery, 'step', { step: step.kind, index: i, error_code: step.error && step.error.code });
      }
    }
    deliveryEvent(delivery, 'completed', { steps: plan.steps.length });
  } catch (e) {
    deliveryEvent(delivery, 'failed', { err: '' + e });
    emitAgentEvent({ ev: 'scenario_err', what: 'drive_delegate', err: '' + e,
           rule_id: delivery.rule_id, task_ptr: delivery.task_ptr });
  }
}

// ── 请求侧：观察/拦截 -[NSURLSessionTask resume]（基类，覆盖所有 task 子类）────────
// bundle load 时不安装任何 resume hook。只读抓取由 installCaptureHooks 显式安装
// Interceptor.attach；只有 configureScenario 才安装 Interceptor.replace 以便短路。
// 实机已证常驻 replace 即使规则为空，卸载后也可能令同一 App PID 的 NSURLSession
// 持续报 -100；因此“只读”必须在 hook 形态上也只读。

function observeResumeRequest(selfPtr) {
  var task = new ObjC.Object(selfPtr);
  var req = null;
  try { req = task.currentRequest(); } catch (e) {}
  if (!req || req.isNull()) { try { req = task.originalRequest(); } catch (e) {} }
  var meta = readReqMeta(req ? req.handle : null);

  if (CAPTURING) {
    var policy = rememberCapturePolicy(selfPtr, meta ? meta.url : null);
    var tid = null;
    try { tid = task.taskIdentifier(); } catch (e) {}
    var sessPtr = null;
    try {
      var s = task.session ? task.session() : null;
      sessPtr = s ? s.handle.toString() : null;
    } catch (e) {}
    var body = null;
    var bodyAbsent = false, bodyReadError = null;
    try {
      if (!req || req.isNull()) {
        bodyReadError = 'request_unavailable';
      } else {
        body = req.HTTPBody();
        bodyAbsent = body === null || (body !== undefined && body.isNull());
      }
    } catch (e) {
      bodyReadError = 'HTTPBody_read_failed';
      bodyAbsent = false;
    }
    if (policy !== 'drop') {
      var lifecycleTask = observeTask(selfPtr, false);
      if (lifecycleTask.request_observed) {
        emitTaskLifecycle(lifecycleTask, 'resume_observed');
        return { task: task, req: req, meta: meta };
      }
      lifecycleTask.request_observed = true;
      var b = dataBytes(body ? body.handle : null, policy === 'full');
      emitAgentEvent({
        ev: 'req', side: 'resume',
        cls: task.$className,
        task_ptr: selfPtr.toString(),
        session_ptr: sessPtr,
        tid: tid,
        req: meta,
        body_len: b.len, body_truncated: b.truncated, body_err: bodyReadError || b.err || null,
        body_absent: bodyAbsent,
        body_b64: b.b64,
        body_filtered: policy !== 'full' || !!b.filtered,
        ts: Date.now()
      });
    }
  }
  return { task: task, req: req, meta: meta };
}

function installPassiveResumeHook() {
  if (RESUME_HOOK_MODE === 'passive' || RESUME_HOOK_MODE === 'replace') {
    return RESUME_HOOK_MODE;
  }
  try {
    var resumeImpl = C.NSURLSessionTask['- resume'].implementation;
    RESUME_LISTENER = Interceptor.attach(resumeImpl, {
      onEnter: function (args) {
        try {
          observeResumeRequest(args[0]);
        } catch (e) {
          emitAgentEvent({ ev: 'req_err', mode: 'passive', err: '' + e });
        }
      }
    });
    RESUME_HOOK_MODE = 'passive';
    RESUME_HOOK_OK = true;
    emitAgentEvent({ ev: 'hook_ok', what: 'resume', mode: RESUME_HOOK_MODE });
  } catch (e) {
    emitAgentEvent({ ev: 'hook_err', what: 'resume', mode: 'passive', err: '' + e });
  }
  return RESUME_HOOK_MODE;
}

function installScenarioResumeHook() {
  if (RESUME_HOOK_MODE === 'replace') return RESUME_HOOK_MODE;
  try {
    if (RESUME_LISTENER) {
      RESUME_LISTENER.detach();
      RESUME_LISTENER = null;
    }
    var resumeImpl = C.NSURLSessionTask['- resume'].implementation;
    var origResume = new NativeFunction(resumeImpl, 'void', ['pointer', 'pointer']);
    Interceptor.replace(resumeImpl, new NativeCallback(function (selfPtr, selPtr) {
      var intercepted = false, matched = false;
      try {
        var observed = observeResumeRequest(selfPtr);
        var req = observed.req;
        var meta = observed.meta;

        if (SCENARIO_RULES.length) {
          var rule = matchRule(meta);
          if (rule) { matched = true; intercepted = applyRule(rule, selfPtr, req); }
        }
      } catch (e) {
        emitAgentEvent({ ev: 'req_err', code: 'resume_observation_failed' });
        if (SCENARIO_CONTROLLED) matched = true;
      }
      if (SCENARIO_CONTROLLED && matched && !intercepted) {
        try { new ObjC.Object(selfPtr).cancel(); } catch (e) {}
        return;
      }
      if (!intercepted) origResume(selfPtr, selPtr);   // 未命中/未应用 → 走真网络
    }, 'void', ['pointer', 'pointer']));
    RESUME_HOOK_MODE = 'replace';
    RESUME_HOOK_OK = true;
    emitAgentEvent({ ev: 'hook_ok', what: 'resume', mode: RESUME_HOOK_MODE });
  } catch (e) {
    RESUME_HOOK_OK = false;
    RESUME_HOOK_MODE = 'none';
    emitAgentEvent({ ev: 'hook_err', what: 'resume', mode: 'replace', err: '' + e });
  }
  return RESUME_HOOK_MODE;
}

// ── 响应侧（completion 路）：hook dataTask...completionHandler:，wrap block 读 resp body ──
//    并为拦截保留 completion block 拷贝（Block_copy），供 resume 短路时合成回调用。
function taskRequestUrl(taskPtr) {
  try {
    var handle = (typeof taskPtr === 'string') ? ptr(taskPtr) : taskPtr;
    var task = new ObjC.Object(handle);
    var req = null;
    try { req = task.currentRequest(); } catch (e) {}
    if (!req || req.isNull()) { try { req = task.originalRequest(); } catch (e) {} }
    var url = req ? req.URL() : null;
    return url ? nstr(url.absoluteString()) : null;
  } catch (e) {
    return null;
  }
}

function emitResp(taskPtr, dataPtr, respPtr, errPtr, taskInstanceId) {
  try {
    if (!CAPTURING) return;   // 抓取暂停：不外带 resp
    var localTask = TASK_INSTANCES[taskInstanceId] || TASK_BY_PTR[String(taskPtr)];
    if (localTask && localTask.controlled_local) return;
    var policy = capturePolicyForTask(taskPtr);
    if (policy === 'drop') {
      forgetCapturePolicy(taskPtr);
      return;
    }
    var status = null, respHeaders = {}, contentEncoding = null;
    if (respPtr && !respPtr.isNull()) {
      var r = new ObjC.Object(respPtr);
      if (r.isKindOfClass_(NSHTTPURLResponse)) {
        status = httpResponseStatus(r);
        respHeaders = headerDict(r.allHeaderFields());
        contentEncoding = headerValue(r.allHeaderFields(), 'Content-Encoding');
      }
    }
    var b = dataBytes(dataPtr, policy === 'full');
    var err = (errPtr && !errPtr.isNull()) ? nstr(new ObjC.Object(errPtr)) : null;
    var respEvent = {
      ev: 'resp', side: 'completion',
      task_instance_id: taskInstanceId || null,
      task_ptr: taskPtr,
      status: status, resp_headers: respHeaders,
      content_encoding: contentEncoding,
      body_len: b.len, body_truncated: b.truncated, body_err: b.err || null, body_b64: b.b64,
      body_filtered: policy !== 'full' || !!b.filtered,
      error: err, ts: Date.now()
    };
    emitAgentEvent(respEvent);
    // Callback completion is recorded after the original callback returns.
    forgetCapturePolicy(taskPtr);
  } catch (e) { emitAgentEvent({ ev: 'resp_err', task_ptr: taskPtr, err: '' + e }); }
}

function forwardObservedCompletion(ctx, original, data, response, error) {
  var held = null, tracked = TASK_INSTANCES[ctx.task_instance_id];
  try {
    held = new ObjC.Object(ptr(ctx.task_ptr)); held.retain();
  } catch (e) {
    held = null;
    emitAgentEvent({ ev: 'lifecycle_err', what: 'completion_task_retain' });
  }
  try {
    emitResp(ctx.task_ptr, data, response, error, ctx.task_instance_id);
    var tp = ctx.task_ptr;
    if (tp && COMPLETION_BLOCKS[tp]) { releaseBlock(COMPLETION_BLOCKS[tp]); delete COMPLETION_BLOCKS[tp]; }
    var result = original(data, response, error);
    if (held && tracked && !tracked.controlled_local) emitTaskLifecycle(tracked, 'completion');
    return result;
  } finally { if (held) held.release(); }
}

['- dataTaskWithRequest:completionHandler:',
 '- dataTaskWithURL:completionHandler:',
 '- uploadTaskWithRequest:fromData:completionHandler:'
].forEach(function (sel) {
  try {
    var m = C.NSURLSession[sel];
    if (!m) return;
    var blockIdx = (sel.indexOf('fromData') >= 0) ? 4 : 3;
    Interceptor.attach(m.implementation, {
      // 必须 onEnter wrap：NSURLSession 在方法内 Block_copy 完成块，onLeave 再改改的是副本。
      onEnter: function (args) {
        var bp = args[blockIdx];
        this.bp = bp;
        this.retained = null;
        this.capture_session_ptr = args[0];
        this.ctx = { task_ptr: null };
        var ctx = this.ctx;
        try {
          if (bp && !bp.isNull()) {
            var blk = new ObjC.Block(bp);
            var orig = blk.implementation;
            blk.implementation = function (data, response, error) {
              return forwardObservedCompletion(ctx, orig, data, response, error);
            };
            // **仅在有场景规则时**保留一份供拦截短路合成回调；纯抓取（规则为空）零保留，
            // 与旧的只读 attach 抓取严格一致（Finding 3：无条件 Block_copy 会永久泄漏每个 block）。
            if (SCENARIO_RULES.length) this.retained = retainBlock(bp);
          }
        } catch (e) { emitAgentEvent({ ev: 'wrap_err', sel: sel, err: '' + e }); }
      },
      onLeave: function (retval) {
        this.ctx.task_ptr = retval.toString();
        if (!retval.isNull()) this.ctx.task_instance_id = observeCreatedTask(retval, this.capture_session_ptr).task_instance_id;
        if (this.retained && !this.retained.isNull()) {
          COMPLETION_BLOCKS[retval.toString()] = this.retained;
        }
      }
    });
    emitAgentEvent({ ev: 'hook_ok', what: sel });
  } catch (e) { emitAgentEvent({ ev: 'hook_err', what: sel, err: '' + e }); }
});

// ── 响应侧（delegate 路）：RN(RCTHTTPRequestHandler) 等走 dataTaskWithRequest:（无 completion），
//    body 经 NSURLSession delegate 分片下发。completion hook 抓不到，故按 task 累积 delegate 分片。────
//
//   -URLSession:dataTask:didReceiveResponse:completionHandler:  → 记 status/headers（args: 2 sess 3 task 4 resp）
//   -URLSession:dataTask:didReceiveData:                        → 追加 body 分片（args: 2 sess 3 task 4 data）
//   -URLSession:task:didCompleteWithError:                      → 收尾 emit（args: 2 sess 3 task 4 error）
//
// NSURLSession 默认已解 Content-Encoding：delegate 拿到的是解压后的字节，故 content_encoding 置空，
// 避免 host 侧再 gunzip 明文报错。task_ptr 用 task 对象指针（与 resume 侧同一对象 → 同键可合并）。
var PENDING = {};   // task_ptr → status/headers + JS-owned ArrayBuffer accumulators

function pendingFor(taskPtr) {
  var k = '' + taskPtr;
  if (!PENDING[k]) {
    PENDING[k] = {
      status: null,
      headers: {},
      body: createDelegateBodyAccumulator(BODY_CAP),
    };
  }
  return PENDING[k];
}

function copyDataBytes(data, length) {
  if (length <= 0) return { bytes: null, error: null };
  try {
    var address = data.bytes();
    if (!address || address.isNull()) return { bytes: null, error: 'NSData bytes is null' };
    return { bytes: address.readByteArray(length), error: null };
  } catch (e) {
    return { bytes: null, error: String(e) };
  }
}

// Combine JS-owned ArrayBuffer chunks once, after the delegate task completes.
// dataWithBytes:length: copies the temporary Memory.alloc buffer before return.
function dataFromCopiedChunks(chunks) {
  if (!Array.isArray(chunks) || chunks.length === 0) return null;
  var total = chunks.reduce(function (sum, chunk) {
    return sum + Math.max(0, Number(chunk.length) || 0);
  }, 0);
  if (total <= 0) return null;
  var bytes = Memory.alloc(total);
  var offset = 0;
  for (var i = 0; i < chunks.length; i++) {
    var chunk = chunks[i];
    if (!chunk.bytes || chunk.bytes.byteLength !== chunk.length) {
      throw new Error('delegate body chunk length mismatch');
    }
    bytes.add(offset).writeByteArray(chunk.bytes);
    offset += chunk.length;
  }
  return C.NSData.dataWithBytes_length_(bytes, total);
}

function hookDelegateImpl(m, kind) {
  var hookKey = kind + ':' + String(m.implementation);
  if (DELEGATE_CAPTURE_HOOKS[hookKey]) return false;
  Interceptor.attach(m.implementation, guardDelegateCallbacks(kind, {
    onEnter: function (args) {
      var taskPtr = args[3];                 // 第一个显式参数是 session，第二个才是 task/dataTask
      try {
        if (!CAPTURING) return;
        var localTask = TASK_BY_PTR[String(taskPtr)];
        if (localTask && localTask.controlled_local) return;
        var policy = capturePolicyForTask(taskPtr);
        if (policy === 'drop') {
          return;
        }
        var lifecycleTask = TASK_BY_PTR[String(taskPtr)] || observeTask(taskPtr, false);
        var p = pendingFor(taskPtr);
        p.capture_policy = policy;
        if (kind === 'resp') {
          emitTaskLifecycle(lifecycleTask, 'response_observed');
          var respPtr = args[4];
          if (respPtr && !respPtr.isNull()) {
            var r = new ObjC.Object(respPtr);
            if (r.isKindOfClass_(NSHTTPURLResponse)) {
              p.status = httpResponseStatus(r);
              p.headers = headerDict(r.allHeaderFields());
            }
          }
        } else if (kind === 'data') {
          var dataPtr = args[4];
          if (dataPtr && !dataPtr.isNull()) {
            var d = new ObjC.Object(dataPtr);
            var length = intOf(d.length());
            emitTaskLifecycle(lifecycleTask, 'data_observed', { data_bytes: length });
            if (policy === 'full') {
              var plan = planDelegateBodyChunk(p.body, length);
              var copied = plan.captureLength > 0
                ? copyDataBytes(d, plan.captureLength)
                : { bytes: null, error: null };
              recordDelegateBodyChunk(p.body, {
                actualLength: plan.actualLength,
                captureLength: copied.bytes ? plan.captureLength : 0,
                bytes: copied.bytes,
                error: copied.error,
              });
            } else {
              p.body.bodyLength += Math.max(0, length);
            }
          }
        } else if (kind === 'done') {
          var heldTask = new ObjC.Object(taskPtr);
          heldTask.retain();
          this.completedNativeTask = heldTask;
          var errPtr = args[4];
          var err = (errPtr && !errPtr.isNull()) ? nstr(new ObjC.Object(errPtr)) : null;
          var b;
          if (policy === 'full') {
            var bodyData = null;
            var bodyError = p.body.error;
            try {
              bodyData = dataFromCopiedChunks(p.body.chunks);
            } catch (bodyDecodeError) {
              bodyError = bodyError || String(bodyDecodeError);
            }
            var exported = dataBytes(bodyData ? bodyData.handle : null, true);
            b = {
              b64: exported.b64,
              len: p.body.bodyLength,
              truncated: p.body.truncated,
              err: bodyError || exported.err || null,
            };
          } else {
            b = {
              b64: null,
              len: p.body.bodyLength,
              truncated: false,
              filtered: true,
              err: null,
            };
          }
          // status/headers 优先取 didReceiveResponse 记的；缺则从 task.response() 兜底
          // （didReceiveResponse 的实现地址偶有不可拦截，task 上的 response 恒可读）。
          if (p.status === null) {
            try {
              var task = new ObjC.Object(taskPtr);
              var rr = task.response();
              if (rr && !rr.isNull() && rr.isKindOfClass_(NSHTTPURLResponse)) {
                p.status = httpResponseStatus(rr);
                p.headers = headerDict(rr.allHeaderFields());
              }
            } catch (e2) {}
          }
          var respEvent = {
            ev: 'resp', side: 'delegate',
            task_ptr: '' + taskPtr,
            status: p.status, resp_headers: p.headers,
            content_encoding: null,   // delegate 拿到的已是解码后字节
            body_len: b.len, body_truncated: b.truncated, body_err: b.err || null, body_b64: b.b64,
            body_filtered: policy !== 'full' || !!b.filtered,
            error: err, ts: Date.now()
          };
          emitAgentEvent(respEvent);
          this.completedLifecycleTask = lifecycleTask;
        }
      } catch (e) {
        emitAgentEvent({ ev: 'resp_err', side: 'delegate', kind: kind, err: '' + e });
      } finally {
        if (kind === 'done') {
          delete PENDING['' + taskPtr];
          forgetCapturePolicy(taskPtr);
        }
      }
    },
    onLeave: function () {
      try {
        if (this.completedLifecycleTask) emitTaskLifecycle(this.completedLifecycleTask, 'delegate_done');
      } finally {
        if (this.completedNativeTask) this.completedNativeTask.release();
      }
    }
  }, emitAgentEvent));
  DELEGATE_CAPTURE_HOOKS[hookKey] = { kind: kind, implementation: String(m.implementation), installed: true };
  return true;
}

// 只读抓取用的 delegate 观察 hook：**全量枚举 ObjC 类**（`for name in C`），是 load 期最重操作。
// 挂起态 spawn 进程扛不住这轮枚举会自崩（TransportError: connection closed）。故**不在 load 期自动跑**，
// 拆成具名函数：attach（活进程）路径 load 后自动装；spawn 路径用 globalThis.__AP_DEFER_CAPTURE 抑制，
// 需要抓取时 resume 后经 rpc.installCaptureHooks 事后安装。纯拦截短路（resume-replace）不依赖它。
var CAPTURE_HOOKS_INSTALLED = false;
function hookDelegates(delegateClassNames) {
  if (CAPTURE_HOOKS_INSTALLED) { return Object.keys({}).length; }  // 幂等：重复调用不二次 hook
  CAPTURE_HOOKS_INSTALLED = true;
  var sels = [
    ['- URLSession:dataTask:didReceiveResponse:completionHandler:', 'resp'],
    ['- URLSession:dataTask:didReceiveData:', 'data'],
    ['- URLSession:task:didCompleteWithError:', 'done']
  ];
  var seen = {};   // 去重：不同类继承同一实现只 hook 一次
  var installed = 0;
  var skippedSystem = 0;

  function hookClass(cls, name) {
    var ownMethods = [];
    try { ownMethods = cls.$ownMethods || []; } catch (e) { return; }
    for (var i = 0; i < sels.length; i++) {
      var sel = sels[i][0], kind = sels[i][1];
      // cls[sel] 也会返回继承方法；旧实现因此把 Foundation/SDK 共享 IMP 一并 hook，既过宽又会
      // 命中不可拦地址。响应观察只需真正 override/实现回调的 delegate 类。
      if (ownMethods.indexOf(sel) < 0) continue;
      var m;
      try { m = cls[sel]; } catch (e) { continue; }
      if (!m) continue;
      var module = null;
      try { module = Process.findModuleByAddress(m.implementation); } catch (e) {}
      var modulePath = module && module.path ? String(module.path) : '';
      if (modulePath.indexOf('/System/Library/') === 0 || modulePath.indexOf('/usr/lib/') === 0) {
        skippedSystem += 1;
        continue;
      }
      var addr = '' + m.implementation;
      var key = kind + ':' + addr;
      if (seen[key]) continue;
      seen[key] = true;
      try {
        if (hookDelegateImpl(m, kind)) installed += 1;
      } catch (e) {
        emitAgentEvent({ ev: 'hook_err', what: 'delegate:' + kind, class_name: name,
               implementation: addr, err: '' + e });
      }
    }
  }

  // 只观察直接实现回调的类，跳过继承的共享实现。
  var requestedNames = Array.isArray(delegateClassNames)
    ? delegateClassNames.map(function (value) {
        return String(value || '').trim();
      }).filter(function (value) { return value.length > 0; })
    : [];
  var scope = requestedNames.length
    ? 'named_delegate_implementations'
    : 'own_delegate_implementations';
  if (requestedNames.length) {
    requestedNames.forEach(function (name) {
      var cls;
      try { cls = C[name]; } catch (e) { return; }
      if (cls) hookClass(cls, name);
    });
  } else {
    for (var name in C) {
      var cls;
      try { cls = C[name]; } catch (e) { continue; }
      hookClass(cls, name);
    }
  }
  emitAgentEvent({ ev: 'hook_ok', what: 'delegate', scope: scope,
         requested_classes: requestedNames,
         candidates: Object.keys(seen).length, count: installed, skipped_system: skippedSystem });
  return installed;
}

// 注意：**load 期不自动装 delegate 抓取 hook**。上面 hookDelegates 全类枚举（`for name in C`）是最重
// 操作，挂起态 spawn 进程扛不住会自崩（TransportError: connection closed，原 spawn 抓取卡点即此原因）。
// 改由 host 在**进程已活**时经 rpc.installCaptureHooks 显式安装：
//   - spawn_gate：resume 之后调（进程已活）；
//   - AttachSession：load 之后调（本就 attach 到活进程）。
// 纯拦截短路（resume-replace + applyViaDelegate）不依赖抓取 hook，拦截路径 install_capture=False 不装。
// （frida spawn env 传不到 NSProcessInfo、脚本源前置会破坏 bundle 字节 manifest——都不可行，故走 rpc。）

// ── RN delegate 穿透探针 ────────────────────────────────────────────────────────
// 不碰业务 UI/缓存：在被测进程内复用它自己的 RCTHTTPRequestHandler，让 handler 对一个
// `.invalid` URL 创建真实 NSURLSessionDataTask。resume 仍经过上面的场景短路；若三段合成回调
// 被 RCT handler 吸收，它会按内部 task→delegate 映射转发到这里的最小 delegate，RPC 直接拿到
// status/body/error。仅由显式 rpc.runRnDelegateProbe 调用，生产抓取/场景路径不会自动执行。
var RN_PROBE_DELEGATE_CLASS = null;
var RN_PROBE_STATES = {};
var RN_PROBE_SEQ = 0;

function appBuildInfo() {
  try {
    var bundle = C.NSBundle.mainBundle();
    var info = bundle.infoDictionary();
    function value(key) {
      var v = info.objectForKey_(key);
      return v && !v.isNull() ? v.toString() : null;
    }
    var sessionDelegates = [];
    try {
      var sessions = ObjC.chooseSync(C.NSURLSession);
      var unique = {};
      for (var i = 0; i < sessions.length; i++) {
        try {
          var delegate = sessions[i].delegate();
          var name = delegate && !delegate.isNull() ? delegate.$className : null;
          if (name && !unique[name]) { unique[name] = true; sessionDelegates.push(name); }
        } catch (e) {}
      }
      sessionDelegates.sort();
    } catch (e) {}
    return {
      bundle_id: bundle.bundleIdentifier().toString(),
      short_version: value('CFBundleShortVersionString'),
      build_version: value('CFBundleVersion'),
      executable: value('CFBundleExecutable'),
      rct_network_task_methods: C.RCTNetworkTask
        ? C.RCTNetworkTask.$ownMethods.filter(function (m) {
            return m.indexOf('initWith') >= 0 || m.indexOf('Block') >= 0 ||
                   m.indexOf('start') >= 0 || m.indexOf('status') >= 0;
          })
        : [],
      active_urlsession_delegate_classes: sessionDelegates
    };
  } catch (e) {
    return { error: '' + e };
  }
}

function objcPointerString(value) {
  if (value && value.handle) return '' + value.handle;
  return '' + value;
}

function ensureRnProbeDelegateClass() {
  if (RN_PROBE_DELEGATE_CLASS) return RN_PROBE_DELEGATE_CLASS;
  var className = 'APRNDelegateProbe_' + Process.id + '_' + Date.now();
  RN_PROBE_DELEGATE_CLASS = ObjC.registerClass({
    name: className,
    super: C.NSObject,
    methods: {
      '- URLRequest:didReceiveResponse:': {
        retType: 'void', argTypes: ['object', 'object'],
        implementation: function (tokenPtr, responsePtr) {
          var state = this.data;
          try {
            var response = new ObjC.Object(responsePtr);
            state.status = response.respondsToSelector_(ObjC.selector('statusCode'))
              ? Number(response.statusCode()) : null;
            state.token_ptr = objcPointerString(tokenPtr);
            emitAgentEvent({ ev: 'rn_delegate_probe', phase: 'response', status: state.status,
                   token_ptr: state.token_ptr, ts: Date.now() });
          } catch (e) {
            state.callback_error = 'response: ' + e;
            emitAgentEvent({ ev: 'rn_delegate_probe', phase: 'callback_error', where: 'response',
                   err: '' + e, ts: Date.now() });
          }
        }
      },
      '- URLRequest:didReceiveData:': {
        retType: 'void', argTypes: ['object', 'object'],
        implementation: function (tokenPtr, dataPtr) {
          var state = this.data;
          try {
            var data = new ObjC.Object(dataPtr);
            var text = C.NSString.alloc().initWithData_encoding_(data, 4);
            state.body += text ? text.toString() : '';
            state.token_ptr = objcPointerString(tokenPtr);
            emitAgentEvent({ ev: 'rn_delegate_probe', phase: 'data', body_length: state.body.length,
                   token_ptr: state.token_ptr, ts: Date.now() });
          } catch (e) {
            state.callback_error = 'data: ' + e;
            emitAgentEvent({ ev: 'rn_delegate_probe', phase: 'callback_error', where: 'data',
                   err: '' + e, ts: Date.now() });
          }
        }
      },
      '- URLRequest:didCompleteWithError:': {
        retType: 'void', argTypes: ['object', 'object'],
        implementation: function (tokenPtr, errorPtr) {
          var state = this.data;
          try {
            state.token_ptr = objcPointerString(tokenPtr);
            state.error = (errorPtr && !errorPtr.isNull())
              ? nstr(new ObjC.Object(errorPtr)) : null;
          } catch (e) {
            state.callback_error = 'complete: ' + e;
            emitAgentEvent({ ev: 'rn_delegate_probe', phase: 'callback_error', where: 'complete',
                   err: '' + e, ts: Date.now() });
          }
          state.finish('completed');
        }
      }
    }
  });
  return RN_PROBE_DELEGATE_CLASS;
}

function runRnDelegateProbe(url, timeoutMs) {
  var probeId = 'rn-probe-' + (++RN_PROBE_SEQ);
  var state = {
    probe_id: probeId,
    phase: 'setup',
    status: null,
    body: '',
    error: null,
    callback_error: null,
    task_ptr: null,
    token_ptr: null,
    handler_class: null,
    finish: function (phase) {
      if (state.phase === 'completed' || state.phase === 'setup_failed') return;
      state.phase = phase;
      emitAgentEvent({ ev: 'rn_delegate_probe', phase: phase, result: rnProbeSnapshot(state), ts: Date.now() });
    }
  };

  // RPC 线程直接做轻量 setup；NSURLSession 的真实回调仍由 handler 自己的 delegateQueue 承担。
  var pool = C.NSAutoreleasePool.alloc().init();
  try {
    var Handler = C.RCTHTTPRequestHandler;
    if (!Handler) throw new Error('RCTHTTPRequestHandler class not loaded');
    var handlers = ObjC.chooseSync(Handler);
    var handler = handlers.length ? handlers[0] : Handler.alloc().init();
    var Delegate = ensureRnProbeDelegateClass();
    var delegate = Delegate.alloc().init();
    ObjC.bind(delegate, state);
    // 保持对象直到异步回调结束；诊断探针单次运行，进程在会话退出时被 kill，不做冒险释放。
    state.handler = handler;
    state.delegate = delegate;
    state.handler_class = handler.$className || null;

    var urlObj = C.NSURL.URLWithString_(String(url));
    if (!urlObj || urlObj.isNull()) throw new Error('invalid probe URL: ' + url);
    var req = C.NSMutableURLRequest.requestWithURL_(urlObj);
    req.setHTTPMethod_('GET');
    req.setCachePolicy_(1);  // NSURLRequestReloadIgnoringLocalCacheData
    state.phase = 'started';
    emitAgentEvent({ ev: 'rn_delegate_probe', phase: 'started', probe_id: probeId, url: String(url),
           handler_class: state.handler_class, existing_handlers: handlers.length, ts: Date.now() });
    var task = handler.sendRequest_withDelegate_(req, delegate);
    state.task_ptr = task ? '' + task.handle : null;
    if (!task || task.isNull()) throw new Error('RCTHTTPRequestHandler returned no task');
  } catch (e) {
    state.callback_error = '' + e;
    state.finish('setup_failed');
  } finally {
    pool.drain();
  }
  RN_PROBE_STATES[probeId] = state;
  return rnProbeSnapshot(state);
}

function rnProbeSnapshot(state) {
  return {
    probe_id: state.probe_id,
    ok: state.phase === 'completed' && !state.error && !state.callback_error,
    phase: state.phase,
    status: state.status,
    body: state.body,
    error: state.error,
    callback_error: state.callback_error,
    task_ptr: state.task_ptr,
    token_ptr: state.token_ptr,
    handler_class: state.handler_class,
    rct_task_ptr: state.rct_task_ptr || null
  };
}

function rnDelegateProbeStatus(probeId) {
  var state = RN_PROBE_STATES[String(probeId)];
  if (!state) return { ok: false, phase: 'missing', probe_id: String(probeId) };
  if (state.network_task) {
    try {
      var token = state.network_task.$ivars._requestToken;
      if (token) state.task_ptr = objcPointerString(token);
    } catch (e) {}
  }
  return rnProbeSnapshot(state);
}

function runRnNetworkTaskProbe(url) {
  var probeId = 'rn-network-task-probe-' + (++RN_PROBE_SEQ);
  var state = {
    probe_id: probeId,
    phase: 'setup',
    status: null,
    body: '',
    error: null,
    callback_error: null,
    task_ptr: null,
    token_ptr: null,
    handler_class: null,
    rct_task_ptr: null,
    finish: function (phase) {
      if (state.phase === 'completed' || state.phase === 'setup_failed') return;
      state.phase = phase;
      emitAgentEvent({ ev: 'rn_network_task_probe', phase: phase, result: rnProbeSnapshot(state), ts: Date.now() });
    }
  };

  var pool = C.NSAutoreleasePool.alloc().init();
  try {
    var Handler = C.RCTHTTPRequestHandler;
    var NetworkTask = C.RCTNetworkTask;
    if (!Handler || !NetworkTask) throw new Error('RCTHTTPRequestHandler/RCTNetworkTask class not loaded');
    var handlers = ObjC.chooseSync(Handler);
    var handler = handlers.length ? handlers[0] : Handler.alloc().init();
    state.handler = handler;
    state.handler_class = handler.$className || null;

    var urlObj = C.NSURL.URLWithString_(String(url));
    if (!urlObj || urlObj.isNull()) throw new Error('invalid probe URL: ' + url);
    var req = C.NSMutableURLRequest.requestWithURL_(urlObj);
    req.setHTTPMethod_('GET');
    req.setCachePolicy_(1);

    var responseBlock = new ObjC.Block({
      retType: 'void', argTypes: ['object'],
      implementation: function (response) {
        try {
          state.status = response && response.respondsToSelector_(ObjC.selector('statusCode'))
            ? Number(response.statusCode()) : null;
          emitAgentEvent({ ev: 'rn_network_task_probe', phase: 'response', status: state.status,
                 probe_id: probeId, ts: Date.now() });
        } catch (e) {
          state.callback_error = 'response: ' + e;
        }
      }
    });
    var completionBlock = new ObjC.Block({
      retType: 'void', argTypes: ['object', 'object', 'object'],
      implementation: function (response, data, error) {
        try {
          if (response && response.respondsToSelector_(ObjC.selector('statusCode'))) {
            state.status = Number(response.statusCode());
          }
          if (data) {
            var text = C.NSString.alloc().initWithData_encoding_(data, 4);
            state.body = text ? text.toString() : '';
          }
          state.error = error ? nstr(error) : null;
        } catch (e) {
          state.callback_error = 'completion: ' + e;
        }
        state.finish('completed');
      }
    });
    state.response_block = responseBlock;
    state.completion_block = completionBlock;

    var networkTask = NetworkTask.alloc()
      .initWithRequest_handler_callbackQueue_(req, handler, ObjC.mainQueue);
    if (!networkTask || networkTask.isNull()) throw new Error('RCTNetworkTask init returned nil');
    state.network_task = networkTask;
    state.rct_task_ptr = objcPointerString(networkTask);
    networkTask.setResponseBlock_(responseBlock);
    networkTask.setCompletionBlock_(completionBlock);
    state.phase = 'started';
    emitAgentEvent({ ev: 'rn_network_task_probe', phase: 'started', probe_id: probeId,
           url: String(url), handler_class: state.handler_class,
           rct_task_ptr: state.rct_task_ptr, existing_handlers: handlers.length, ts: Date.now() });
    networkTask.start();
    try {
      var token = networkTask.$ivars._requestToken;
      if (token) state.task_ptr = objcPointerString(token);
    } catch (e) {}
  } catch (e) {
    state.callback_error = '' + e;
    state.finish('setup_failed');
  } finally {
    pool.drain();
  }
  RN_PROBE_STATES[probeId] = state;
  return rnProbeSnapshot(state);
}

// ── 关联层 marker（§六）+ 场景控制（§六/§八）：runner/编排经 rpc 打点、下发规则、探健康。──
rpc.exports = {
  ping: function () { return 'pong'; },
  sealCapture: sealCapture,
  installCaptureLifecycleHooks: installCaptureLifecycleHooks,
  mark: function (caseId, step, operation, actionId, subjectEpochRef) {
    emitAgentEvent({ ev: 'mark', case_id: caseId, step: step, operation: operation || null,
           action_id: actionId || null, subject_epoch_ref: subjectEpochRef || null,
           ts: Date.now() });
    return true;
  },
  setEnabled: function (on) {
    CAPTURING = !!on;
    emitAgentEvent({ ev: 'capture_state', on: CAPTURING, ts: Date.now() });
    return CAPTURING;
  },
  configureCaptureFilter: function (config) {
    config = config || {};
    CAPTURE_FILTER = {
      allow_url_substrings: normalizedStrings(config.allow_url_substrings),
      deny_url_substrings: normalizedStrings(config.deny_url_substrings),
      body_url_substrings: normalizedStrings(config.body_url_substrings),
    };
    TASK_CAPTURE_POLICIES = {};
    emitAgentEvent({
      ev: 'capture_filter_configured',
      allow_count: CAPTURE_FILTER.allow_url_substrings.length,
      deny_count: CAPTURE_FILTER.deny_url_substrings.length,
      body_count: CAPTURE_FILTER.body_url_substrings.length,
      ts: Date.now()
    });
    return {
      ok: true,
      allow_count: CAPTURE_FILTER.allow_url_substrings.length,
      deny_count: CAPTURE_FILTER.deny_url_substrings.length,
      body_count: CAPTURE_FILTER.body_url_substrings.length
    };
  },
  // 事后安装只读抓取 hook（全类枚举，重）。spawn 路径 load 期抑制了自动安装，需抓取时 **resume 后**
  // （进程已活、能扛枚举）调用这个。幂等。纯拦截短路不需要调。
  installCaptureHooks: function (options) {
    options = options || {};
    installPassiveResumeHook();
    installTaskCreationHooks();
    var delegateClassNames = Array.isArray(options.delegate_class_names)
      ? options.delegate_class_names
      : [];
    var n = hookDelegates(delegateClassNames);
    emitAgentEvent({ ev: 'capture_hooks_installed', count: n, resume_mode: RESUME_HOOK_MODE,
           delegate_scope: delegateClassNames.length ? 'named' : 'all',
           delegate_class_names: delegateClassNames,
           ts: Date.now() });
    return n;
  },
  runRnDelegateProbe: function (url, timeoutMs) {
    return runRnDelegateProbe(url, timeoutMs);
  },
  rnDelegateProbeStatus: function (probeId) {
    return rnDelegateProbeStatus(probeId);
  },
  runRnNetworkTaskProbe: function (url) {
    return runRnNetworkTaskProbe(url);
  },
  appBuildInfo: function () {
    return appBuildInfo();
  },
  // 下发 host 侧编译好的规则（agent 只做哑执行，不含编译逻辑，§6.1）。resume 前调用（§六 规则先于 resume）。
  configureScenario: function (compiled) {
    compiled = compiled || {};
    installScenarioResumeHook();
    SCENARIO_RULES = Array.isArray(compiled.rules) ? compiled.rules : [];
    SCENARIO_MODE = compiled.mode || null;
    SCENARIO_CONTROLLED = compiled.controlled === true;
    SCENARIO_HASH = compiled.hash || null;
    SCENARIO_COUNTS = {};   // 新 session/新规则集：计数器归零
    // 释放上一场景残留的 completion block 拷贝，避免跨规则集泄漏（Finding 3）。
    for (var _tp in COMPLETION_BLOCKS) releaseBlock(COMPLETION_BLOCKS[_tp]);
    COMPLETION_BLOCKS = {};
    emitAgentEvent({ ev: 'scenario_configured', rules: SCENARIO_RULES.length,
           hash: SCENARIO_HASH, controlled: SCENARIO_CONTROLLED, pid: Process.id, mode: SCENARIO_MODE, ts: Date.now() });
    return { ok: true, rules: SCENARIO_RULES.length, controlled: SCENARIO_CONTROLLED, hash: SCENARIO_HASH, pid: Process.id };
  },
  // 健康诚实上报（§三/§八 闸1）：① 短路 hook 是否装到活 NSURLSession 类 ② attach 前覆盖缺口。
  scenarioHealth: function () {
    var clsOk = false;
    try { clsOk = !!(C.NSURLSessionTask && C.NSURLSession); } catch (e) {}
    return {
      hook_installed: !!RESUME_HOOK_OK,        // resume 观察/短路点已装
      hook_mode: RESUME_HOOK_MODE,
      nsurlsession_class: clsOk,               // 活 NSURLSession/Task 类可解
      block_runtime: !!(_Block_copy && _Block_release),  // 合成回调所需 Block ABI 原语在场
      interception_ready: !!(
        RESUME_HOOK_MODE === 'replace'
        && RESUME_HOOK_OK && clsOk && _Block_copy && _Block_release
      ),
      coverage_gap: SCENARIO_MODE === 'attach', // attach 拦不到挂载前请求（物理约束，§三）
      mode: SCENARIO_MODE,
      controlled: SCENARIO_CONTROLLED, hash: SCENARIO_HASH, pid: Process.id,
      configured_rules: SCENARIO_RULES.length
    };
  }
};
emitAgentEvent({ ev: 'loaded', ts: Date.now() });
