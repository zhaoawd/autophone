'use strict';
// crash_trap —— 诊断用「原始崩点捕获」agent（纯 native，无 ObjC bridge，可直接 load 或 frida-compile）。
//
// 背景：集成了第三方崩溃上报 SDK 的 App，crash log 的 faulting 帧常落在 SDK 的
// kill(getpid(), 捕获信号) 重抛点，且上报被 marshal 到主线程，**原始出错线程的现场已丢**，
// 所以 .ips 定位不到真正的 SIGBUS 崩点。
//
// 本 agent 做两件事，抢在崩溃上报 SDK 前面拿到真现场：
//   ① Process.setExceptionHandler —— Frida 走 Mach task 异常端口，早于 BSD 信号（也早于 SDK 的 POSIX
//      handler）。SIGBUS/SIGSEGV 一发就在**原始出错线程**上回调，带 pc/lr/sp + fault 地址 + 该线程
//      backtrace（符号化到 module+offset）。send('crash', ...) 带出后 return false，让进程照常死（同时
//      产出一份干净 .ips 兜底）。
//   ② 可选摘掉 App 的 POSIX 信号 handler。这会改变 App 信号语义，所以默认绝不启用；
//      只有 host 显式调 setNeutralizeHandlers(true) 时才用于排除第三方崩溃上报 SDK 的重抛污染。
//
// 只诊断、不改行为：return false 表示「抓完让它继续崩」。设 rpc swallow(true) 可改成吞掉异常保活（危险，
// 状态可能已损坏，仅用于「想让 app 别死好继续观察」的临时排查）。

const FATAL = { 4: 'SIGILL', 5: 'SIGTRAP', 6: 'SIGABRT', 8: 'SIGFPE', 10: 'SIGBUS', 11: 'SIGSEGV' };
const SIG_DFL = ptr(0);
const SIG_IGN = ptr(1);

let swallow = false;      // 默认让进程照常崩（诊断真实性优先）
let handlersNeutralized = false;
let caught = 0;
const seenModules = {};

function ap(line) { try { send({ ev: 'log', line: '[crash_trap] ' + line }); } catch (e) {} }

function moduleFor(addr) {
  try {
    const m = Process.findModuleByAddress(addr);
    if (!m) return null;
    return { name: m.name, base: m.base.toString(), offset: addr.sub(m.base).toString() };
  } catch (e) { return null; }
}

function frameInfo(addr) {
  const out = { pc: addr.toString() };
  const mod = moduleFor(addr);
  if (mod) { out.module = mod.name; out.offset = mod.offset; }
  try {
    const sym = DebugSymbol.fromAddress(addr);
    if (sym && sym.name) out.symbol = sym.name;
    if (sym && sym.moduleName && !out.module) out.module = sym.moduleName;
  } catch (e) {}
  return out;
}

// —— ① Mach 层异常捕获（真现场） ——
function installExceptionHandler() {
  Process.setExceptionHandler(function (details) {
    caught += 1;
    const ctx = details.context || {};
    const payload = {
      ev: 'crash',
      n: caught,
      type: details.type,                       // 'access-violation' / 'bus' / 'illegal-instruction' ...
      signal: null,
      fault_address: details.address ? details.address.toString() : null,
      memory: details.memory || null,           // { operation, address }
      thread_id: (Process.getCurrentThreadId && Process.getCurrentThreadId()) || null,
      pc: ctx.pc ? ctx.pc.toString() : null,
      lr: ctx.lr ? ctx.lr.toString() : null,
      sp: ctx.sp ? ctx.sp.toString() : null,
      pc_frame: ctx.pc ? frameInfo(ctx.pc) : null,
      lr_frame: ctx.lr ? frameInfo(ctx.lr) : null,
      backtrace: [],
    };
    try {
      const bt = Thread.backtrace(ctx, Backtracer.ACCURATE);
      for (let i = 0; i < bt.length; i++) payload.backtrace.push(frameInfo(bt[i]));
    } catch (e) {
      try {
        const bt = Thread.backtrace(ctx, Backtracer.FUZZY);
        for (let i = 0; i < bt.length; i++) payload.backtrace.push(frameInfo(bt[i]));
        payload.backtrace_mode = 'fuzzy';
      } catch (e2) { payload.backtrace_error = String(e2); }
    }
    send(payload);
    ap('CAUGHT #' + caught + ' type=' + details.type + ' @pc=' + payload.pc +
       ' fault=' + payload.fault_address + ' -> ' + JSON.stringify(payload.pc_frame));
    return swallow;   // false = 让它继续崩（拿到真现场即可）；true = 吞掉保活
  });
  ap('Process.setExceptionHandler installed');
}

// frida 17 移除了 Module.findExportByName(null, name) 静态法，改用 Module.getGlobalExportByName；
// 兼容新旧两版：新版优先，老版回退。
function globalExport(name) {
  try { if (typeof Module.findGlobalExportByName === 'function') return Module.findGlobalExportByName(name); } catch (e) {}
  try { if (typeof Module.getGlobalExportByName === 'function') return Module.getGlobalExportByName(name); } catch (e) {}
  try { if (typeof Module.findExportByName === 'function') return Module.findExportByName(null, name); } catch (e) {}
  return null;
}

// —— ② 摘掉 app 的 POSIX 信号 handler（防崩溃上报 SDK 重抛污染） ——
let sigactionN = null, signalN = null;
function neutralizePosixHandlers() {
  const p_sigaction = globalExport('sigaction');
  const p_signal = globalExport('signal');

  // hook sigaction(int sig, const struct sigaction *act, struct sigaction *oact)
  if (p_sigaction) {
    sigactionN = new NativeFunction(p_sigaction, 'int', ['int', 'pointer', 'pointer']);
    Interceptor.attach(p_sigaction, {
      onEnter: function (args) {
        const sig = args[0].toInt32();
        if (FATAL[sig] && !args[1].isNull()) {
          // 把 app 想装的 sa_handler/sa_sigaction 抹成 SIG_DFL（结构头 8 字节是 handler 指针）
          try { args[1].writePointer(SIG_DFL); } catch (e) {}
          ap('blocked sigaction(' + FATAL[sig] + ') -> forced SIG_DFL');
        }
      }
    });
  }
  // hook signal(int sig, void (*handler)(int))
  if (p_signal) {
    signalN = new NativeFunction(p_signal, 'pointer', ['int', 'pointer']);
    Interceptor.attach(p_signal, {
      onEnter: function (args) {
        const sig = args[0].toInt32();
        if (FATAL[sig] && !args[1].equals(SIG_DFL) && !args[1].equals(SIG_IGN)) {
          args[1] = SIG_DFL;
          ap('blocked signal(' + FATAL[sig] + ') -> forced SIG_DFL');
        }
      }
    });
  }
  // 主动复位当前已装的 disposition（第三方 SDK 多半在 attach 前就装好了）
  if (sigactionN) {
    // struct sigaction on arm64 darwin: { __sigaction_u sa_handler; sigset_t sa_mask; int sa_flags; } —— 16 bytes 足够置 handler
    const act = Memory.alloc(16);
    act.writePointer(SIG_DFL);
    act.add(8).writeU32(0);   // sa_mask
    act.add(12).writeInt(0);  // sa_flags
    Object.keys(FATAL).forEach(function (k) {
      try { sigactionN(parseInt(k, 10), act, NULL); ap('reset ' + FATAL[k] + ' -> SIG_DFL'); } catch (e) {}
    });
  }
}

rpc.exports = {
  ping: function () { return 'pong'; },
  status: function () { return { caught: caught, swallow: swallow,
                                  handlers_neutralized: handlersNeutralized }; },
  setSwallow: function (v) { swallow = !!v; return swallow; },
  setNeutralizeHandlers: function (v) {
    if (!v || handlersNeutralized) return handlersNeutralized;
    neutralizePosixHandlers();
    handlersNeutralized = true;
    return true;
  },
};

installExceptionHandler();
send({ ev: 'ready', pid: Process.id, arch: Process.arch,
       main_module: (Process.enumerateModules()[0] || {}).name || null,
       observe_only: true });
