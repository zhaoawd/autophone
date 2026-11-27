'use strict';
// M0b-1 — ObjC bridge bundle 验证（guest 侧 / 编译后由 python-loaded 加载）。
//
// 背景：frida 17 起 ObjC/Java bridge 不再是 raw create_script 的自动全局——
// M0a 实测 python-loaded 脚本里 `typeof ObjC === 'undefined'`（CLI REPL 会自动注入，
// 故 phase3 走 CLI）。Phase 4 §5/§6 主路押在 frida-python 长连，必须让 python-loaded
// 脚本自带 ObjC。本探针 import frida-objc-bridge，经 frida-compile 打成单文件，
// 验「编译后 python 加载的脚本能解 ObjC.classes.NSURLSession」——抓取层 net_tap 的前置。
//
// 只读、零副作用、不 hook 任何东西。

import ObjC from 'frida-objc-bridge';

function probe() {
  var avail = false;
  var sample = null;
  var nsurlsession = false;
  try {
    avail = ObjC.available === true;
    if (avail) {
      // 真正解类，而不只看 available 标志：NSURLSession 是抓取层的核心类
      var cls = ObjC.classes.NSURLSession;
      nsurlsession = !!cls;
      sample = nsurlsession
        ? ('' + cls + ' / +[NSURLSession sharedSession] resolvable='
           + (typeof cls.sharedSession === 'function'))
        : 'available but NSURLSession not in ObjC.classes';
    }
  } catch (e) {
    avail = false;
    sample = '' + e;
  }
  var prims = {
    Interceptor: typeof Interceptor !== 'undefined',
    Module: typeof Module !== 'undefined',
    CModule: typeof CModule !== 'undefined'
  };
  send({
    ev: 'objc_bundled',
    available: avail,
    nsurlsession: nsurlsession,
    sample: sample,
    primitives: prims
  });
}

rpc.exports = {
  ping: function () { return 'pong'; }
};

probe();
send({ ev: 'loaded', ts: Date.now() });
