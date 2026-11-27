"""Read lock state and display geometry without image/OCR access.

锁屏状态（``native_lock_state`` / ``probe_native_lock_state`` / ``native_lock_probe_for_socket``）自
BL-232 阶段 2 起经 guest 辅助程序读 Darwin 通知状态，不 attach SpringBoard。

``NativeScreenStateProbe`` / ``NativeForegroundProbe`` 仍经 Frida attach SpringBoard 读
``isUILocked``、屏幕尺寸与前台 App；平台内唯一引用方是无调用方的
``workflow/ui_verification/ui_verification_observation.py``（评估文档 C15），其余在
``experiments/`` 下。新代码不得使用它们。
"""
import math
import os
import time
from autophone.logging import get_logger
from autophone.observation.no_screenshot import NoScreenshotError, checked_lock_state
from autophone.device.frida_endpoint import resolve_frida_host
from autophone.device.stage_guard import OperationClass, StageGuard, guarded_device_operation

LOG = get_logger(__name__)

#: 原生锁屏状态单次 UDS 读取的上限（秒）。超时按「状态未知」处理，不判为未锁屏。
#: SpringBoard 刚拉起时的等待（``SPRINGBOARD_SETTLE_S``）不计入该上限。
NATIVE_LOCK_STATE_TIMEOUT_S = 5.0
#: ``"0"`` 关闭原生判据（测试套件默认关闭，避免经本机在跑的 VM socket 推送并执行辅助程序）。
#: 隔离解除探针（BL-223）不受该开关控制。
ENV_NATIVE_LOCK_STATE = "AUTOPHONE_NATIVE_LOCK_STATE"

_AGENT = """
const fn=(n,r,a)=>new NativeFunction(Module.getGlobalExportByName(n),r,a);
const cls=fn('objc_getClass','pointer',['pointer']),sel=fn('sel_registerName','pointer',['pointer']);
const cm=fn('class_getClassMethod','pointer',['pointer','pointer']),im=fn('class_getInstanceMethod','pointer',['pointer','pointer']);
const enc=fn('method_getTypeEncoding','pointer',['pointer']),imp=fn('method_getImplementation','pointer',['pointer']);
function invoke(c,receiver,name,isClass,returnType,encoding) {
 const s=sel(Memory.allocUtf8String(name)),m=(isClass?cm:im)(c,s);
 if(m.isNull()||enc(m).readUtf8String()!==encoding)throw Error('unsupported method signature: '+name);
 return new NativeFunction(imp(m),returnType,['pointer','pointer'])(receiver,s);
}
function singleton(name,selector){const c=cls(Memory.allocUtf8String(name));if(c.isNull())throw Error('missing class '+name);const obj=invoke(c,c,selector,true,'pointer','@16@0:8');if(obj.isNull())throw Error('null singleton');return [c,obj];}
rpc.exports={read(){
 const [c,obj]=singleton('SBLockScreenManager','sharedInstance');
 const locked=invoke(c,obj,'isUILocked',false,'bool','B16@0:8');
 const [sc,screen]=singleton('UIScreen','mainScreen');
 const bounds=invoke(sc,screen,'bounds',false,[['double','double'],['double','double']],'{CGRect={CGPoint=dd}{CGSize=dd}}16@0:8');
 const scale=invoke(sc,screen,'scale',false,'double','d16@0:8');
 return {locked:Boolean(locked),frame_size:{width:bounds[1][0],height:bounds[1][1]},scale:scale,source:'SpringBoard.SBLockScreenManager.isUILocked+UIScreen'};
}};
"""

class NativeScreenStateProbe:
    _agent = _AGENT
    def __init__(self, *, host=None, read=None):
        self.host = resolve_frida_host(host)
        self._read = read

    def __call__(self):
        value = self._read() if self._read else self._read_native()
        try:
            if not isinstance(value, dict):
                raise ValueError("state is not an object")
            checked_lock_state(lambda: value["locked"])
            if value["source"] != "SpringBoard.SBLockScreenManager.isUILocked+UIScreen":
                raise ValueError("unsupported state source")
            for number in (value["scale"], value["frame_size"]["width"], value["frame_size"]["height"]):
                if type(number) not in (int, float) or not math.isfinite(number) or number <= 0:
                    raise ValueError("invalid display geometry")
        except (KeyError, TypeError, ValueError) as exc:
            raise NoScreenshotError("invalid native screen state") from exc
        return value

    @guarded_device_operation(OperationClass.FRIDA, identity=lambda a: {"frida_endpoint": a["self"].host})
    def _read_native(self):
        import frida
        device = frida.get_device_manager().add_remote_device(self.host)
        processes = [p for p in device.enumerate_processes() if p.name == "SpringBoard"]
        if len(processes) != 1:
            raise NoScreenshotError("SpringBoard process not uniquely available")
        session = device.attach(processes[0].pid)
        from autophone.device.stage_guard import StageGuard
        from autophone.device.frida_lifetime import FridaLifetime
        lifetime = FridaLifetime(session, host=self.host) if StageGuard.current() is not None else None
        try:
            script = session.create_script(self._agent)
            if lifetime is not None:
                lifetime.add_script(script)
            script.load()
            exports = lifetime.exports(script.exports_sync) if lifetime is not None else script.exports_sync
            return exports.read()
        finally:
            if lifetime is not None:
                lifetime.dispose()
            else:
                session.detach()


class NativeForegroundProbe(NativeScreenStateProbe):
    _agent = """
const fn=(n,r,a)=>new NativeFunction(Module.getGlobalExportByName(n),r,a);
const cls=fn('objc_getClass','pointer',['pointer']),sel=fn('sel_registerName','pointer',['pointer']);
const cm=fn('class_getClassMethod','pointer',['pointer','pointer']),im=fn('class_getInstanceMethod','pointer',['pointer','pointer']);
const enc=fn('method_getTypeEncoding','pointer',['pointer']),imp=fn('method_getImplementation','pointer',['pointer']);
function invoke(c,receiver,name,isClass,returnType,encoding) {
 const s=sel(Memory.allocUtf8String(name)),m=(isClass?cm:im)(c,s);
 if(m.isNull()||enc(m).readUtf8String()!==encoding)throw Error('unsupported method signature: '+name);
 return new NativeFunction(imp(m),returnType,['pointer','pointer'])(receiver,s);
}
function singleton(name,selector){const c=cls(Memory.allocUtf8String(name));if(c.isNull())throw Error('missing class '+name);const obj=invoke(c,c,selector,true,'pointer','@16@0:8');if(obj.isNull())throw Error('null singleton');return [c,obj];}
rpc.exports={read(){
 const [sc,sb]=singleton('SpringBoard','sharedApplication');
 const app=invoke(sc,sb,'_accessibilityFrontMostApplication',false,'pointer','@16@0:8');
 if(app.isNull())throw Error('no frontmost app');
 const objectClass=fn('object_getClass','pointer',['pointer']);
 const str=invoke(objectClass(app),app,'bundleIdentifier',false,'pointer','@16@0:8');
 if(str.isNull())throw Error('no bundle id');
 const c=objectClass(str),selector=sel(Memory.allocUtf8String('UTF8String')),m=im(c,selector);
 const encoding=enc(m).readUtf8String();
 if(!['r*16@0:8','*16@0:8'].includes(encoding))throw Error('unsupported UTF8String '+encoding);
 return {bundle_id:new NativeFunction(imp(m),'pointer',['pointer','pointer'])(str,selector).readUtf8String(),source:'SpringBoard._accessibilityFrontMostApplication'};
}};
"""

    def __call__(self):
        value = self._read() if self._read else self._read_native()
        if not isinstance(value, dict) or value.get('source') != 'SpringBoard._accessibilityFrontMostApplication' or not isinstance(value.get('bundle_id'), str) or not value['bundle_id'].strip():
            raise NoScreenshotError('native foreground identity unavailable')
        return value


# ---------------------------------------------------------------- 锁屏状态（BL-232 阶段 2）

#: 锁屏状态来源标识（证据字段 ``source``）。
LOCK_STATE_SOURCE = "notify:com.apple.springboard.lockstate+hasBlankedScreen"
#: SpringBoard 拉起后多久内的读数不采信（秒）。实验 X1（2026-10-09 vm，n=1）：新 pid 出现后
#: 立即读到 ``lockstate=0``（旧值），+5 s 为 1，+15 s ``hasBlankedScreen=1``。取 20 s，待补数据。
SPRINGBOARD_SETTLE_S = 20.0
#: 等待结束后额外留的余量（秒），避免 guest 与宿主计时的舍入差把第二次读数仍判为过早。
SETTLE_MARGIN_S = 0.5


def judge_lock_state(output, *, settle_s=SPRINGBOARD_SETTLE_S):
    """判读辅助程序一次输出（协议 ``autophone-lockstate/1``），返回判定记录，不抛异常。

    判据：

    - 读取前后 SpringBoard 都唯一存在且身份（pid、启动时间）相同，否则 ``SPRINGBOARD_ABSENT`` /
      ``SPRINGBOARD_CHANGED``；
    - 两个通知名都读取成功，状态值都为 0 或 1，否则 ``NOTIFY_FAILED`` / ``STATE_INVALID``；
    - SpringBoard 已运行至少 ``settle_s`` 秒，否则 ``SPRINGBOARD_SETTLING``（带 ``settle_remaining_s``）；
    - ``locked = lockstate == 1 or hasBlankedScreen == 1``。熄屏未锁（``lockstate=0``、
      ``hasBlankedScreen=1``）在 X1 中未出现；按「需要唤醒」判为锁屏，此时截图停在旧帧，按 Home
      只唤醒屏幕（待验证假设）。
    """
    record = {"ok": False, "stage": "OUTPUT_INVALID", "locked": None, "lockstate": None,
              "has_blanked_screen": None, "springboard": None, "detail": ""}
    try:
        before, after = output["springboard_before"], output["springboard_after"]
        if before.get("count") != 1 or after.get("count") != 1:
            record.update(stage="SPRINGBOARD_ABSENT",
                          detail=f"SpringBoard count before={before.get('count')} after={after.get('count')}")
            return record
        if (before["pid"], before["start"]) != (after["pid"], after["start"]):
            record.update(stage="SPRINGBOARD_CHANGED",
                          detail=f"SpringBoard pid {before['pid']} -> {after['pid']} during the read")
            return record
        age = float(output["now"]) - float(before["start"])
        record["springboard"] = {"pid": int(before["pid"]), "start": float(before["start"]),
                                 "age_s": round(age, 3)}
        values = {}
        for item in output["notify"]:
            if item.get("register_status") != 0 or item.get("get_status") != 0:
                record.update(stage="NOTIFY_FAILED",
                              detail=(f"{item.get('name')} register_status={item.get('register_status')} "
                                      f"get_status={item.get('get_status')}"))
                return record
            values[item["name"]] = item["state"]
        from autophone.device.lock_state_helper import NOTIFY_BLANKED, NOTIFY_LOCKSTATE

        lockstate, blanked = values.get(NOTIFY_LOCKSTATE), values.get(NOTIFY_BLANKED)
        if lockstate not in (0, 1) or blanked not in (0, 1) or type(lockstate) is bool or type(blanked) is bool:
            record.update(stage="STATE_INVALID", detail=f"lockstate={lockstate!r} hasBlankedScreen={blanked!r}")
            return record
        record.update(lockstate=lockstate, has_blanked_screen=blanked)
        if age < settle_s:
            record.update(stage="SPRINGBOARD_SETTLING", settle_remaining_s=round(settle_s - age, 3),
                          detail=(f"SpringBoard pid {before['pid']} started {age:.1f} s ago "
                                  f"(< {settle_s:g} s); lockstate may still be the previous value"))
            return record
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        record.update(stage="OUTPUT_INVALID", detail=f"{type(exc).__name__}: {exc}")
        return record
    record.update(ok=True, stage="READY", locked=bool(lockstate == 1 or blanked == 1),
                  detail=f"lockstate={lockstate} hasBlankedScreen={blanked}")
    return record


def probe_native_lock_state(*, sock_path=None, timeout_s=NATIVE_LOCK_STATE_TIMEOUT_S,
                            settle_s=SPRINGBOARD_SETTLE_S, helper=None, sleep=time.sleep, host=None):
    """经 guest 辅助程序读 notify 锁屏状态（BL-232 阶段 2，L1a），不 attach SpringBoard。

    ``ok: False`` 表示状态未知，从不表示「未锁屏」。SpringBoard 拉起不足 ``settle_s`` 秒时等到满
    ``settle_s`` 后再读一次，第二次读数的 SpringBoard 身份须与第一次相同才采信（``confirmed_after_wait_s``）；
    期间 SpringBoard 再次变化或仍不可判定时返回 ``ok: False``。

    ``host`` 为阶段 2 之前的 Frida 端点参数，已不再使用，保留只为兼容旧调用。
    """
    del host
    started = time.monotonic()
    sock = str(sock_path) if sock_path else None
    try:
        if helper is None:
            from autophone.device.lock_state_helper import GuestLockStateHelper

            if sock is None:
                from autophone.device.shell import resolve_sock

                sock = resolve_sock()
            helper = GuestLockStateHelper(sock, timeout_s=timeout_s)
        reading = helper.read()
        judged = judge_lock_state(reading["output"], settle_s=settle_s)
        reads = 1
        waited = None
        if judged["stage"] == "SPRINGBOARD_SETTLING":
            first = judged["springboard"]
            waited = min(float(judged["settle_remaining_s"]) + SETTLE_MARGIN_S, settle_s + SETTLE_MARGIN_S)
            LOG.info("SpringBoard 拉起不足 %.0f s，等待 %.1f s 后复读锁屏状态：pid=%s",
                     settle_s, waited, first["pid"])
            sleep(waited)
            reading = helper.read()
            judged = judge_lock_state(reading["output"], settle_s=settle_s)
            reads = 2
            second = judged.get("springboard") or {}
            if judged["ok"] and (second.get("pid"), second.get("start")) != (first["pid"], first["start"]):
                judged = {**judged, "ok": False, "locked": None, "stage": "SPRINGBOARD_CHANGED",
                          "detail": f"SpringBoard pid {first['pid']} -> {second.get('pid')} while waiting"}
    except Exception as exc:  # noqa: BLE001 - helper unavailable: state unknown
        code = getattr(exc, "code", None)
        return {"ok": False, "stage": str(code or "PROBE_FAILED").upper(), "error_class": type(exc).__name__,
                "detail": getattr(exc, "detail", None) or str(exc) or repr(exc), "source": LOCK_STATE_SOURCE,
                "socket": sock, "elapsed_ms": round((time.monotonic() - started) * 1000)}
    result = {
        "ok": judged["ok"],
        "stage": judged["stage"],
        "locked": judged["locked"] if judged["ok"] else None,
        "lockstate": judged["lockstate"],
        "has_blanked_screen": judged["has_blanked_screen"],
        "springboard": judged["springboard"],
        "source": LOCK_STATE_SOURCE,
        "detail": judged["detail"],
        "socket": getattr(helper, "sock_path", sock),
        "helper": reading.get("deploy"),
        "reads": reads,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }
    if waited is not None:
        result["confirmed_after_wait_s"] = round(waited, 3)
    return result


def native_lock_state(*, host=None, sock_path=None, timeout_s=NATIVE_LOCK_STATE_TIMEOUT_S):
    """Return ``(locked, evidence)`` for the rig's guest, never raising.

    显示熄灭后 UDS 截图停在最后一帧，截图 OCR 会把旧帧当成当前画面。这里经 guest 辅助程序读
    notify ``com.apple.springboard.lockstate`` 与 ``hasBlankedScreen``（BL-232 阶段 2，不 attach
    SpringBoard）；辅助程序不可用、读数不可判定、SpringBoard 刚拉起且等待后仍不可判定，或
    ``AUTOPHONE_NATIVE_LOCK_STATE=0`` 时返回 ``(None, 原因)``，调用方回退截图 OCR 判据（BL-211）。
    不回退到 Frida ``isUILocked``。``sock_path`` 缺省为本进程目标 VM 的 socket；``host`` 已不使用。
    供 ``autophone.device.unlock.ensure_unlocked(lock_probe=...)`` 与 Rig 就绪门使用。
    """
    del host
    if os.environ.get(ENV_NATIVE_LOCK_STATE, "").strip() == "0":
        return None, f"native lock state disabled by {ENV_NATIVE_LOCK_STATE}=0"
    try:
        result = probe_native_lock_state(sock_path=sock_path, timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001 - unknown state, caller falls back to OCR
        return None, f"native lock state unavailable: {type(exc).__name__}: {exc}"
    locked = result.get("locked")
    if result.get("ok") is not True or type(locked) is not bool:
        return None, (f"native lock state unavailable: stage={result.get('stage')}: "
                      f"{result.get('detail')}")
    springboard = result.get("springboard") or {}
    waited = (f", confirmed after {result['confirmed_after_wait_s']} s"
              if result.get("confirmed_after_wait_s") is not None else "")
    return locked, (f"notify lockstate={result.get('lockstate')} "
                    f"hasBlankedScreen={result.get('has_blanked_screen')} locked={locked} "
                    f"via {result.get('socket')} (SpringBoard pid {springboard.get('pid')}, "
                    f"up {springboard.get('age_s')} s{waited}; {result.get('elapsed_ms')} ms)")


def frida_endpoint_for_socket(sock_path):
    """Return ``(endpoint, evidence)`` for the Frida tunnel of the VM behind ``sock_path``.

    BL-232 阶段 2 起锁屏状态不再经 Frida 读取；交付路径（``state_driver_runtime``）仍调用本函数，
    只把结果写入 ``unlock/lock-probe.json`` 的 ``frida_endpoint`` 字段。端点只在能证明对应关系时
    返回：已绑定 Stage 且其 socket 与 ``sock_path`` 相同 → 用该 Stage 的 Frida 端点；
    否则 ``sock_path`` 等于本进程目标的 socket → 用本进程的 Frida 端点。都不成立时返回
    ``(None, 原因)``，该字段记为 null；锁屏读数本身不受影响。
    """
    from pathlib import Path

    from autophone.device.target import resolve_target

    wanted = str(Path(sock_path).resolve())
    guard = StageGuard.current()
    if guard is not None and guard.socket_path is not None:
        if guard.socket_path == wanted and guard.frida_endpoint:
            return guard.frida_endpoint, f"Stage Frida endpoint {guard.frida_endpoint} for {wanted}"
        if guard.socket_path != wanted:
            return None, (f"native lock state endpoint not resolvable: socket {wanted} "
                          f"is not the bound Stage device {guard.socket_path}")
    try:
        process_sock = str(Path(resolve_target().sock).resolve())
        endpoint = resolve_frida_host()
    except Exception as exc:  # noqa: BLE001 - unknown endpoint, caller falls back to OCR
        return None, f"native lock state endpoint not resolvable: {type(exc).__name__}: {exc}"
    if process_sock != wanted:
        return None, (f"native lock state endpoint not resolvable: socket {wanted} "
                      f"is not this process target {process_sock}")
    return endpoint, f"process Frida endpoint {endpoint} for {wanted}"


def native_lock_probe_for_socket(sock_path, *, timeout_s=NATIVE_LOCK_STATE_TIMEOUT_S):
    """``ensure_unlocked(lock_probe=...)`` 用的原生判据，绑定到 ``sock_path`` 所在 VM。

    BL-232 阶段 2 起经 ``sock_path`` 本身的 UDS shell 执行 guest 辅助程序读 notify 锁屏状态，
    读数与解锁操作走同一个 socket，不再需要把 socket 对应到 Frida 端点。回归执行链用
    ``native_lock_state``（进程目标），交付路径用本函数（显式 socket），两者同一来源、同一套
    超时与回退语义（BL-211）。``frida_endpoint_for_socket`` 保留，交付路径只把它写入
    ``unlock/lock-probe.json`` 的 ``frida_endpoint`` 字段，不用于读取。
    """
    def probe():
        return native_lock_state(sock_path=sock_path, timeout_s=timeout_s)

    return probe
