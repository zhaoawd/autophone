'use strict';
// M0a — Frida 传输契约探针（guest 侧）。
// 只验四件事，故意不碰 NSURLSession：
//   1. 脚本能在常驻 session 里加载（host 侧负责，见 .py）
//   2. ObjC.available —— phase3 记过 raw create_script 可能没有 ObjC bridge，这里实测
//   3. rpc.exports.{ping,mark} —— host 能否调进脚本（marker 的命脉）
//   4. send(payload, data) 的二进制旁路 —— body 的命脉
//
// 全部走 [AP] 风格的结构化 message；不 hook 任何东西，纯只读、零副作用。

function reportObjC() {
  var avail = false, sample = null;
  try {
    avail = (typeof ObjC !== 'undefined') && ObjC.available === true;
    if (avail) {
      // 取一个 ObjC 类证明 bridge 真能解类，而不只是 available 标志为真
      sample = ObjC.classes.NSURLSession ? 'NSURLSession resolvable'
                                         : 'available but NSURLSession missing';
    }
  } catch (e) {
    avail = false;
    sample = '' + e;
  }
  // hook 原语是否在（frida 17 起 ObjC/Java bridge 不再是自动全局，但这些仍是）：
  // 抓取层 net_tap.js 需要 Interceptor + ObjC.classes，故两者都得报清楚。
  var prims = {
    Interceptor: typeof Interceptor !== 'undefined',
    Module: typeof Module !== 'undefined',
    CModule: typeof CModule !== 'undefined'
  };
  send({ ev: 'objc', available: avail, sample: sample, primitives: prims });
}

rpc.exports = {
  // host 同步调用，验 RPC 往返
  ping: function () {
    return 'pong';
  },
  // 验「RPC 触发 + 从 rpc 里 send() 出来」——这正是 §6 关联层 marker 的形态
  mark: function (caseId, step, operation) {
    send({ ev: 'mark', case_id: caseId, step: step, operation: operation || null, ts: Date.now() });
    return true;
  },
  // 验二进制旁路：payload + 5 字节 data
  sendData: function () {
    var bytes = new Uint8Array([1, 2, 3, 4, 5]);
    send({ ev: 'probe_data', n: bytes.length }, bytes.buffer);
    return true;
  }
};

// 加载即上报：ObjC 可用性 + 一条 loaded 标记（证明常驻脚本活着）
reportObjC();
send({ ev: 'loaded', ts: Date.now() });
