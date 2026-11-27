#!/bin/bash
# start_vphone.sh — 一条命令完整拉起 vphone-cli 全栈，直到健康。
#
# 这是**唯一的开机引导入口**：把 CLAUDE.md「启动 VM」一节里那几个原本要手动逐条
# 敲的步骤（amfidont / vphone-cli / SSH / vphoned_fix / frida 隧道 / 健康守护）收口
# 成一条幂等命令，避免像 frida 隧道那样「单独拉起、经常遗漏」导致 run 退回普通启动、
# API 抓取失效（参见 service._run_with_capture 的 ServerNotRunningError 回退）。
#
# 幂等：每步先探测，已在跑就跳过。只有当 MCP→UDS→vsock 链确认健康、且 frida
# 隧道（API 抓取必需）打通后才退出 0。
#
# 用法：
#   scripts/start_vphone.sh            拉起全栈 + 健康/隧道校验后退出
#   SKIP_FRIDA=1 scripts/start_vphone.sh   跳过 frida 隧道（不跑 API 抓取时）
#   SKIP_NAT_CHECK=1 scripts/start_vphone.sh  显式跳过 guest 公网出站健康闸
#   SKIP_VPHONE_SSHD_REMOVE=1 scripts/start_vphone.sh  跳过开机移除 guest com.vphone.sshd
#   VPHONE_UDS_READY_TIMEOUT=180 scripts/start_vphone.sh   慢启动时放宽 UDS shell 等待
#
# Requires: sudo (for amfidont), python3, OpenSSH, iproxy (libimobiledevice)

set -euo pipefail

# Paths are overridable via env (VPHONE_CLI_DIR / VPHONE_SOCK) to match
# actions.resolve_sock(); autophone's own dir is derived from this script's
# location so it's never hard-coded. Prefer the vphone-cli checkout beside this
# autophone repository; falling back to another checkout can make the health
# daemon monitor one VM while the down hook recovers a different one.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOPHONE_DIR="$(dirname "$SCRIPT_DIR")"

_first_dir() { for d in "$@"; do [ -d "$d" ] && { echo "$d"; return; }; done; echo "$1"; }
VPHONE_CLI_DIR="${VPHONE_CLI_DIR:-$(_first_dir "$AUTOPHONE_DIR/../vphone-cli" "$HOME/Documents/github/vphone-cli" "$HOME/github/vphone-cli")}"
VPHONE_BIN="$VPHONE_CLI_DIR/.build/vphone-cli.app/Contents/MacOS/vphone-cli"
# VM_DIR 可覆写以并列多镜像（例如 VM_DIR=vm-alt 切到第二镜像）。默认 vm/；
# 相对路径按 vphone-cli 仓库根解析，绝对路径原样用。
if [ "${AUTOPHONE_VPHONE_PROTOCOL:-legacy}" != "legacy" ]; then
    echo "Use python -m autophone.rig.lifecycle start --target <target.json> for upstream_v1" >&2
    exit 1
fi
if [ -n "${VPHONE_LIBRARY_ROOT:-}" ]; then
    VM_DIR="${VM_DIR:-vm-1}"
else
    VM_DIR="${VM_DIR:-vm}"
fi
case "$VM_DIR" in /*) ;; *) VM_DIR="${VPHONE_LIBRARY_ROOT:-$VPHONE_CLI_DIR}/$VM_DIR" ;; esac
SOCK="${VPHONE_SOCK:-$VM_DIR/vphone.sock}"
CONFIG="$VM_DIR/config.plist"
RIG_ID="${AUTOPHONE_RIG_ID:-${AP_DEVICE:-$(basename "$VM_DIR")}}"
case "$RIG_ID" in
    ''|*[!A-Za-z0-9._-]*)
        echo "[start_vphone] FATAL: invalid RIG_ID: $RIG_ID" >&2
        exit 1
        ;;
esac
RUNTIME_DIR="${AUTOPHONE_RIG_RUNTIME_DIR:-/tmp/autophone-rigs/$RIG_ID}"
HEALTH_JSON="${VPHONE_HEALTH_STATE_FILE:-$RUNTIME_DIR/health.json}"
LEGACY_HEALTH_JSON="/tmp/vphone_health.json"
HEALTH_PID_FILE="${VPHONE_HEALTH_PID_FILE:-$RUNTIME_DIR/health.pid}"
HEALTH_LOG="${VPHONE_HEALTH_LOG:-$RUNTIME_DIR/health.log}"
VPHONE_PID_FILE="${VPHONE_PID_FILE:-$RUNTIME_DIR/vphone.pid}"
IPROXY_PID_FILE="${VPHONE_IPROXY_PID_FILE:-$RUNTIME_DIR/iproxy.pid}"
FRIDA_TUNNEL_PID_FILE="${VPHONE_FRIDA_TUNNEL_PID_FILE:-$RUNTIME_DIR/frida-tunnel.pid}"
FRIDA_TUNNEL_LOG="${VPHONE_FRIDA_TUNNEL_LOG:-$RUNTIME_DIR/frida-tunnel.log}"
VPHONE_LOG_DIR="${VPHONE_LOG_DIR:-$VM_DIR/logs}"
VPHONE_LOG="$VPHONE_LOG_DIR/vphone-cli-start.log"
AMFIDONT_HELPER="$VPHONE_CLI_DIR/scripts/start_amfidont_for_vphone.sh"
BOOT_PREFLIGHT="$VPHONE_CLI_DIR/scripts/boot_host_preflight.sh"

SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -o PreferredAuthentications=password -o PubkeyAuthentication=no -o NumberOfPasswordPrompts=1"
SSH_PORT="${AUTOPHONE_SSH_PORT:-2222}"
SSH_USER="mobile"
: "${AUTOPHONE_SSH_PASSWORD:?Set the guest SSH password for legacy startup}"
password_ssh() {
    python3 "$AUTOPHONE_DIR/autophone/device/ssh_auth.py" "$@"
}
SSH_PRIV_USER="$SSH_USER"
GUEST_EXEC_PATH="/var/jb/usr/bin:/var/jb/bin:/var/jb/usr/sbin:/var/jb/sbin:/iosbinpack64/bin:/iosbinpack64/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
FRIDA_PORT="${AUTOPHONE_FRIDA_HOST_PORT:-${FRIDA_PORT:-27042}}"
FRIDA_GUEST_PORT="${AUTOPHONE_FRIDA_GUEST_PORT:-27042}"
# 变量名沿用旧名以兼容既有环境；BL-232 阶段 1 起只作 Frida 枚举探针的超时上限。
FRIDA_ATTACH_TIMEOUT="${FRIDA_ATTACH_TIMEOUT:-5}"
UDS_READY_TIMEOUT="${VPHONE_UDS_READY_TIMEOUT:-120}"
NAT_PROBE_HOST="${VPHONE_NAT_PROBE_HOST:-www.baidu.com}"
NAT_PROBE_PORT="${VPHONE_NAT_PROBE_PORT:-443}"
USB_EXEC_TRACE_MODE="${VPHONE_USB_EXEC_TRACE:-0}"
USB_EXEC_TRACE_SCRIPT="$AUTOPHONE_DIR/scripts/trace_usbmux_xcode.sh"
MULTI_VM_MODE="${AUTOPHONE_EXPERIMENTAL_MULTI_VM:-0}"
USB_ATTACH_LOCK="${AUTOPHONE_USB_ATTACH_LOCK:-/tmp/autophone-usb-attach.lock}"
USB_ATTACH_LOCK_WAIT="${AUTOPHONE_USB_ATTACH_LOCK_WAIT:-300}"

read_predicted_udid() {
    local file="$VM_DIR/udid-prediction.txt"
    [ -f "$file" ] || return 0
    sed -nE 's/^UDID[=:][[:space:]]*//p' "$file" | head -1
}

EXPECTED_USB_UDID="${AUTOPHONE_EXPECTED_USB_UDID:-$(read_predicted_udid)}"

# python 入口固定从 autophone 仓库根跑（tools.* 模块可导入）。这些步骤只依赖
# 标准库，裸 python3 即可，无需 uv venv。
cd "$AUTOPHONE_DIR"

# 把解析好的 SOCK 导出给所有 python 子步骤：vphone_uds 认 VPHONE_SOCK（ssh_bringup
# 经它走 UDS），否则 VPHONE_CLI_DIR/VPHONE_SOCK 覆写后这里等的是目标 sock、SSH/health
# 却走默认 sock，可能操作错 VM。health daemon 另用 --sock 显式传同一值。
export VPHONE_SOCK="$SOCK"
# 同时导出 VM_DIR：health daemon → vsock_health → on_vphone_down.sh 全是 env 继承（都没
# 覆写子进程 env），导出后掉线自动恢复能带着 VM_DIR 重跑本脚本、拉回正确那台镜像。
export VM_DIR
export AUTOPHONE_RIG_ID="$RIG_ID"
export AUTOPHONE_RIG_RUNTIME_DIR="$RUNTIME_DIR"
export AUTOPHONE_EXPECTED_USB_UDID="$EXPECTED_USB_UDID"
export AUTOPHONE_SSH_PORT="$SSH_PORT"
export AUTOPHONE_FRIDA_HOST_PORT="$FRIDA_PORT"
export AUTOPHONE_FRIDA_GUEST_PORT="$FRIDA_GUEST_PORT"
export AUTOPHONE_EXPERIMENTAL_MULTI_VM="$MULTI_VM_MODE"
export VPHONE_HEALTH_STATE_FILE="$HEALTH_JSON"
export VPHONE_HEALTH_PID_FILE="$HEALTH_PID_FILE"
export VPHONE_HEALTH_LOG="$HEALTH_LOG"
export VPHONE_IPROXY_PID_FILE="$IPROXY_PID_FILE"
export VPHONE_FRIDA_TUNNEL_PID_FILE="$FRIDA_TUNNEL_PID_FILE"

log() { echo "[start_vphone] $(date '+%H:%M:%S') $*"; }

# 结构化阶段通道（与上面的文本输出并行；AUTOPHONE_STAGE_LOG 未设置时全部是空操作，
# 不产生任何额外输出或文件）。见 docs/platform-vm-management-task-console.md §6.1。
# shellcheck source=scripts/lib/stage_events.sh
. "$SCRIPT_DIR/lib/stage_events.sh"

# die 先把失败挂到当前阶段上再退出；文本输出格式与阶段通道引入前一字不差。
# 不在任何阶段内时（参数校验、启动锁、Step 8）不发射阶段事件。
die() {
    if [ -n "${AUTOPHONE_CURRENT_STAGE:-}" ]; then
        stage_error "$AUTOPHONE_CURRENT_STAGE" startup_failed "$*"
    fi
    log "FATAL: $*"
    exit 1
}

# amfidont 前置授权探测（§6.3）：定义 start_amfidont / ensure_amfidont。
# shellcheck source=scripts/lib/amfidont.sh
. "$SCRIPT_DIR/lib/amfidont.sh"

# VM_DIR 覆写后尽早校验（die 此时已定义）。相对路径已在上面按 VPHONE_CLI_DIR 解析。
[ -d "$VM_DIR" ] || die "VM_DIR 不存在：${VM_DIR}（相对值按 $VPHONE_CLI_DIR 解析；如 VM_DIR=vm）"
mkdir -p "$RUNTIME_DIR"

case "$SSH_PORT:$FRIDA_PORT:$FRIDA_GUEST_PORT" in
    *[!0-9:]*) die "SSH/Frida ports must be integers: ssh=$SSH_PORT host_frida=$FRIDA_PORT guest_frida=$FRIDA_GUEST_PORT" ;;
esac
[ "$SSH_PORT" -ge 1 ] && [ "$SSH_PORT" -le 65535 ] || die "invalid SSH port: $SSH_PORT"
[ "$FRIDA_PORT" -ge 1 ] && [ "$FRIDA_PORT" -le 65535 ] || die "invalid Frida host port: $FRIDA_PORT"
[ "$FRIDA_GUEST_PORT" -ge 1 ] && [ "$FRIDA_GUEST_PORT" -le 65535 ] || die "invalid Frida guest port: $FRIDA_GUEST_PORT"
[ "$SSH_PORT" != "$FRIDA_PORT" ] || die "SSH and Frida host ports must differ: $SSH_PORT"
case "$MULTI_VM_MODE" in
    0|1) ;;
    *) die "AUTOPHONE_EXPERIMENTAL_MULTI_VM must be 0 or 1: $MULTI_VM_MODE" ;;
esac
if [ "$MULTI_VM_MODE" = "1" ] && [ -z "$EXPECTED_USB_UDID" ]; then
    die "AUTOPHONE_EXPERIMENTAL_MULTI_VM=1 requires AUTOPHONE_EXPECTED_USB_UDID or $VM_DIR/udid-prediction.txt"
fi
case "$EXPECTED_USB_UDID" in
    *[[:space:]]*) die "expected USB UDID must not contain whitespace" ;;
esac

# 独立启动的旧单 Rig 读者仍读取 /tmp/vphone_health.json。实验性多 VM 不更新
# 这个全局兼容链接；每个 Rig 必须通过 DeviceTarget 显式传 health_state_file。
if [ "$MULTI_VM_MODE" != "1" ] && [ "$HEALTH_JSON" != "$LEGACY_HEALTH_JSON" ]; then
    ln -sfn "$HEALTH_JSON" "$LEGACY_HEALTH_JSON"
fi

case "$UDS_READY_TIMEOUT" in
    ''|*[!0-9]*)
        die "VPHONE_UDS_READY_TIMEOUT must be a positive integer, got: $UDS_READY_TIMEOUT"
        ;;
    0)
        die "VPHONE_UDS_READY_TIMEOUT must be greater than 0"
        ;;
esac

# ─── 生命周期锁：同一 Rig 同一时刻只允许一个启动/恢复流程 ───────────────────
# 不同 Rig 使用不同锁；虚拟 USB attach 的宿主共享阶段另用短时全局锁。
LOCK_DIR="${VPHONE_START_LOCK:-$RUNTIME_DIR/start.lock}"
LOCK_WAIT_MAX="${VPHONE_START_LOCK_WAIT:-600}"
USB_ATTACH_LOCK_HELD=0
START_LOCK_HELD=0

clear_lock_dir() {
    local lock_dir="$1"
    [ -e "$lock_dir" ] || return 0
    if [ -L "$lock_dir" ] || [ ! -d "$lock_dir" ]; then
        return 1
    fi
    rm -f -- "$lock_dir/pid"
    rmdir -- "$lock_dir" 2>/dev/null
}

cleanup_locks() {
    if [ "$USB_ATTACH_LOCK_HELD" = "1" ]; then
        clear_lock_dir "$USB_ATTACH_LOCK" || log "USB attach lock cleanup refused: $USB_ATTACH_LOCK"
    fi
    # 生命周期锁可能已移交给 Recovery Assignment 的内层 start_vphone.sh
    # （见 release_start_lock_for_recovery）；移交后退出时不得删除内层持有的锁。
    if [ "$START_LOCK_HELD" = "1" ]; then
        clear_lock_dir "$LOCK_DIR" || log "Rig lifecycle lock cleanup refused: $LOCK_DIR"
    fi
}

acquire_start_lock() {
    local waited=0 holder
    while ! mkdir "$LOCK_DIR" 2>/dev/null; do
        holder="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
        if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
            log "清掉失效锁（holder pid=${holder} 已不在）: $LOCK_DIR"
            clear_lock_dir "$LOCK_DIR" || die "失效锁目录包含未知内容或不是目录，拒绝清理: $LOCK_DIR"
            continue
        fi
        if [ "$waited" -ge "$LOCK_WAIT_MAX" ]; then
            die "等另一个 start_vphone.sh（pid=${holder:-unknown}）超过 ${LOCK_WAIT_MAX}s 仍未释放锁 $LOCK_DIR"
        fi
        if [ $((waited % 30)) -eq 0 ]; then
            log "另一个 start_vphone.sh 正在跑（pid=${holder:-unknown}）— 等它结束后幂等续跑..."
        fi
        sleep 3
        waited=$((waited + 3))
    done
    echo "$$" > "$LOCK_DIR/pid"
    START_LOCK_HELD=1
    trap cleanup_locks EXIT
}

release_start_lock_for_recovery() {
    # Recovery Assignment 的内层 start_vphone.sh 使用同一个 $RUNTIME_DIR/start.lock
    # （DeviceTarget.process_environment 传同一个 AUTOPHONE_RIG_RUNTIME_DIR）。外层持锁
    # 等待内层会互等：内层 acquire_start_lock 最多等 VPHONE_START_LOCK_WAIT(600s)，
    # RecoveryScript 的超时也是 600s。只在外层已完成 Step 1-7 的全部 VM 生命周期步骤
    # 之后移交；移交后外层只做控制面读查询和回执文件写入。
    [ "$START_LOCK_HELD" = "1" ] || return 1
    [ "$(cat "$LOCK_DIR/pid" 2>/dev/null || true)" = "$$" ] || return 1
    clear_lock_dir "$LOCK_DIR" || return 1
    START_LOCK_HELD=0
    log "Released rig lifecycle lock for Recovery Assignment: $LOCK_DIR"
}

acquire_usb_attach_lock() {
    local waited=0 holder
    while ! mkdir "$USB_ATTACH_LOCK" 2>/dev/null; do
        holder="$(cat "$USB_ATTACH_LOCK/pid" 2>/dev/null || true)"
        if [ -n "$holder" ] && ! kill -0 "$holder" 2>/dev/null; then
            log "清掉失效 USB attach 锁（holder pid=${holder}）"
            clear_lock_dir "$USB_ATTACH_LOCK" || die "USB attach 锁目录包含未知内容或不是目录，拒绝清理: $USB_ATTACH_LOCK"
            continue
        fi
        if [ "$waited" -ge "$USB_ATTACH_LOCK_WAIT" ]; then
            die "等待宿主 USB attach 锁超过 ${USB_ATTACH_LOCK_WAIT}s（holder=${holder:-unknown}）"
        fi
        sleep 2
        waited=$((waited + 2))
    done
    echo "$$" > "$USB_ATTACH_LOCK/pid"
    USB_ATTACH_LOCK_HELD=1
    log "Acquired host USB attach lock for rig=$RIG_ID"
}

release_usb_attach_lock() {
    if [ "$USB_ATTACH_LOCK_HELD" = "1" ]; then
        clear_lock_dir "$USB_ATTACH_LOCK" || die "USB attach 锁目录包含未知内容或不是目录，拒绝清理: $USB_ATTACH_LOCK"
        USB_ATTACH_LOCK_HELD=0
        log "Released host USB attach lock for rig=$RIG_ID"
    fi
}

acquire_start_lock
# Host-global setup, vphone launch and usbmux/lockdown SSH binding form one
# attach critical section. Different Rigs remain concurrently running after
# this lock is released, but two launch flows cannot race the initial attach.
acquire_usb_attach_lock

tail_log_if_any() {
    local file="$1"
    local lines="${2:-80}"
    if [ -s "$file" ]; then
        log "Last $lines lines of $file:"
        tail -n "$lines" "$file" || true
    fi
}

retry_cmd() {
    local attempts="$1"
    shift
    local n=1
    local rc=0
    while true; do
        "$@" && return 0
        rc=$?
        if [ "$n" -ge "$attempts" ]; then
            return "$rc"
        fi
        log "command failed (rc=$rc), retrying ($n/$attempts): $*"
        sleep 1
        n=$((n + 1))
    done
}

run_boot_preflight_if_any() {
    if [ -x "$BOOT_PREFLIGHT" ] || [ -f "$BOOT_PREFLIGHT" ]; then
        log "Running vphone-cli boot preflight for launch-policy diagnostics..."
        (cd "$VPHONE_CLI_DIR" && zsh "$BOOT_PREFLIGHT" --assert-bootable) || true
    else
        log "No boot preflight script found: $BOOT_PREFLIGHT"
    fi
}

kill_recorded_helper() {
    local pid_file="$1"
    shift
    [ -f "$pid_file" ] || return 0
    local pid command required
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    case "$pid" in ''|*[!0-9]*) rm -f "$pid_file"; return 0 ;; esac
    command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    for required in "$@"; do
        case "$command" in
            *"$required"*) ;;
            *)
                log "WARNING: stale helper PID file does not match target; skip kill pid=$pid file=$pid_file"
                rm -f "$pid_file"
                return 0
                ;;
        esac
    done
    kill "$pid" 2>/dev/null || true
    rm -f "$pid_file"
}

cleanup_stale_host_helpers() {
    # These helpers are tied to the previous guest boot. If the VM died, keeping
    # them around makes later SSH/frida/health checks report stale success.
    if [ -n "$EXPECTED_USB_UDID" ]; then
        kill_recorded_helper "$IPROXY_PID_FILE" "iproxy" "-u $EXPECTED_USB_UDID" " $SSH_PORT:"
    else
        kill_recorded_helper "$IPROXY_PID_FILE" "iproxy" " $SSH_PORT:"
    fi
    kill_recorded_helper "$FRIDA_TUNNEL_PID_FILE" \
        "-L $FRIDA_PORT:127.0.0.1:$FRIDA_GUEST_PORT" "-p $SSH_PORT"
    # Unlike a bare pkill, this waits until old probes have actually exited.
    # Otherwise an old guard can write the new state file or fire the same down
    # hook while the replacement is booting.
    python3 -m tools.phase1.health_daemon stop --sock "$SOCK" \
        --pid-file "$HEALTH_PID_FILE" >/dev/null \
        || die "failed to stop old health daemon(s) for sock=$SOCK"
    rm -f "$HEALTH_JSON"
}

uds_shell_ready() {
    python3 - "$SOCK" <<'PY'
import sys

from autophone.device.shell import run_shell

sock = sys.argv[1]
try:
    result = run_shell("echo AUTOPHONE_UDS_READY", timeout_ms=5_000, sock_path=sock)
except Exception as exc:
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    sys.exit(1)

if "AUTOPHONE_UDS_READY" not in result.stdout:
    print(f"unexpected shell output: {result.stdout!r} {result.stderr!r}", file=sys.stderr)
    sys.exit(1)
PY
}

# 快速实登探针：已通就不必再跑 ssh_bringup（exp 变体 guest sshd 原生自启 :22；或 VM 经
# make boot 已带 2222→22 forward——这两种情形 ssh_bringup 再起 iproxy 反而撞端口）。
ssh_login_ok() {
    password_ssh ssh $SSH_OPTS -p "$SSH_PORT" "$SSH_USER@127.0.0.1" 'exit 0' 2>/dev/null
}

select_ssh_priv_user() {
    if password_ssh ssh $SSH_OPTS -p "$SSH_PORT" \
        "root@127.0.0.1" 'exit 0' 2>/dev/null; then
        SSH_PRIV_USER="root"
        log "Privileged guest commands will use root SSH"
    else
        SSH_PRIV_USER="$SSH_USER"
        log "root SSH unavailable — privileged guest commands will use mobile+sudo"
    fi
}

run_guest_privileged() {
    local remote_command="$1"
    local wrapped="/var/jb/usr/bin/env PATH='$GUEST_EXEC_PATH' /var/jb/bin/bash -c '$remote_command'"
    if [ "$SSH_PRIV_USER" = "root" ]; then
        password_ssh ssh $SSH_OPTS -p "$SSH_PORT" \
            "$SSH_PRIV_USER@127.0.0.1" "$wrapped"
    else
        printf '%s\n' "$AUTOPHONE_SSH_PASSWORD" | password_ssh ssh $SSH_OPTS -p "$SSH_PORT" \
            "$SSH_USER@127.0.0.1" "/var/jb/usr/bin/sudo -S $wrapped"
    fi
}

# ─── 阶段 host: Step 0 停 AMP 设备发现 + Step 1 amfidont ────────────────────
stage_start host

# ─── Step 0: 停宿主 iOS 设备发现守护（已确认的 USB 主探测源）────────────────
# AMPDeviceDiscoveryAgent 的 62078 探测曾确定性触发 CommandTimeout。disable 是必要
# 缓解，但不是根因闭合：仍有未识别客户端走同一端口。必须在 VM 起来前 assert，避免
# AMP 抢先探测。SIP 挡 launchctl bootout，故 disable(挡按需重拉，跨重启持久) +
# pkill(杀当前实例)。全程 fail-open，不影响 iproxy/SSH/frida。
disable_amp_device_discovery() {
    local uid
    uid="$(id -u)"
    for lbl in com.apple.AMPDeviceDiscoveryAgent com.apple.AMPDevicesAgent; do
        launchctl disable "gui/$uid/$lbl" 2>/dev/null || true
    done
    if pgrep -f "AMPDeviceDiscoveryAgent" >/dev/null 2>&1; then
        pkill -f "AMPDeviceDiscoveryAgent" 2>/dev/null || true
        log "已停宿主 AMPDeviceDiscoveryAgent（USB 主控超时崩溃扳机；disable 挡重拉）"
    else
        log "AMPDeviceDiscoveryAgent 未运行（已 disable，挡 usbmuxd 按需重拉）"
    fi
}

disable_amp_device_discovery

# ─── Step 0b: 可选的 Xcode/usbmux 追踪 ──────────────────────────────────────
# 优先抓 usbmuxd 日志里短命 Xcode[pid] 的 exec/PPID/argv；SIP 拒绝 DTrace 时会退到
# 0.2s 的 ps 采样（仍含父链但可能漏极短进程）。显式开关且 fail-open，绝不阻断 VM 启动。
case "$USB_EXEC_TRACE_MODE" in
    1|true|TRUE|yes|YES)
        if "$USB_EXEC_TRACE_SCRIPT" start; then
            log "Xcode/usbmux exec trace active（down 时会固化到诊断包）"
        else
            log "WARNING: Xcode/usbmux exec trace 未启动（fail-open；先认证 sudo 或检查 ${USB_EXEC_TRACE_SCRIPT}）"
        fi
        ;;
    0|false|FALSE|no|NO|'')
        ;;
    *)
        log "WARNING: 忽略无效 VPHONE_USB_EXEC_TRACE=${USB_EXEC_TRACE_MODE}（应为 0 或 1）"
        ;;
esac

# ─── Step 1: amfidont ────────────────────────────────────────────────────────
# 前置授权探测与启动都在 scripts/lib/amfidont.sh（§6.3 三步）。
ensure_amfidont

stage_end host

# ─── Step 2: vphone-cli ─────────────────────────────────────────────────────
stage_start boot
# 「已在跑」按**目标 sock** 判，不用全局 pgrep：并列多镜像（vm / vm-alt）时全局 pgrep
# 分不清跑的是哪台。目标 sock 的 UDS 活 → 跳过启动；否则若另有 vphone-cli 在跑（别的
# sock）时，默认仍拒绝第二个 VM；只有显式启用实验模式且提供预期 USB UDID 后，
# 才在宿主 USB attach 锁内启动目标 VM。
need_launch=1
if [ -S "$SOCK" ] && uds_shell_ready >/dev/null 2>&1; then
    log "vphone-cli already running on target sock ($SOCK)"
    need_launch=0
else
    # argv 保留参数边界；vphone-cli 启动后可改名为 vphone-vm。
    # 无法读取进程身份时拒绝启动，避免重复拉起同一镜像。
    vphone_process_state="$(python3 -m tools.vphone_process --binary "$VPHONE_BIN" --config "$CONFIG")" \
        || die "无法核对现有 VM 进程，拒绝启动第二个 VM"
    case "$vphone_process_state" in
        target)
            log "vphone target process already running; waiting for target sock ($SOCK)"
            need_launch=0
            ;;
        other)
            if [ "$MULTI_VM_MODE" != "1" ]; then
                die "另一个 vphone 进程正在跑，但不是目标 sock=${SOCK}（VM_DIR=${VM_DIR}）。默认一次只允许一个 VM；完成同 Host 多 Rig P0 后才可设置 AUTOPHONE_EXPERIMENTAL_MULTI_VM=1。"
            fi
            log "Experimental multi-VM admission enabled for rig=$RIG_ID udid=$EXPECTED_USB_UDID"
            ;;
        none) ;;
        *) die "无法识别 VM 进程探测结果: $vphone_process_state" ;;
    esac
fi

if [ "$need_launch" = "1" ]; then
    log "Starting vphone-cli..."
    [ -x "$VPHONE_BIN" ] || die "vphone-cli binary not found: $VPHONE_BIN"
    [ -f "$CONFIG" ] || die "config not found: $CONFIG"
    mkdir -p "$VPHONE_LOG_DIR"
    : >"$VPHONE_LOG"
    cleanup_stale_host_helpers
    if [ -S "$SOCK" ]; then
        log "Removing stale vphone.sock before launch: $SOCK"
        rm -f "$SOCK"
    fi
    VPHONE_PID="$(python3 - "$VPHONE_BIN" "$CONFIG" "$VPHONE_LOG" "$VM_DIR" <<'PY'
import subprocess
import sys

binary, config, log_path, cwd = sys.argv[1:5]
log = open(log_path, "ab", buffering=0)
proc = subprocess.Popen(
    [binary, "--config", config],
    cwd=cwd,
    stdin=subprocess.DEVNULL,
    stdout=log,
    stderr=subprocess.STDOUT,
    start_new_session=True,
)
print(proc.pid)
PY
)"
    printf '%s\n' "$VPHONE_PID" > "$VPHONE_PID_FILE"
    log "vphone-cli launched (pid $VPHONE_PID, log=$VPHONE_LOG)"
fi

stage_end boot

# ─── Step 3: Wait for UDS shell readiness ───────────────────────────────────
stage_start uds

log "Waiting for vphone.sock + UDS shell..."
elapsed=0
last_uds_error="socket file missing"
while true; do
    if [ -S "$SOCK" ]; then
        if uds_err="$(uds_shell_ready 2>&1)"; then
            break
        fi
        last_uds_error="$uds_err"
    fi
    vphone_process_state="$(python3 -m tools.vphone_process --binary "$VPHONE_BIN" --config "$CONFIG")" \
        || die "无法核对等待期间的 VM 进程，拒绝继续启动"
    if [ "$vphone_process_state" != "target" ]; then
        tail_log_if_any "$VPHONE_LOG" 120
        run_boot_preflight_if_any
        die "vphone-cli exited while waiting for UDS shell (last readiness error: ${last_uds_error:-unknown}; log=$VPHONE_LOG)"
    fi
    sleep 1
    elapsed=$((elapsed + 1))
    if [ $((elapsed % 10)) -eq 0 ]; then
        log "  UDS shell not ready yet (${elapsed}s/${UDS_READY_TIMEOUT}s): ${last_uds_error:-unknown}"
    fi
    if [ "$elapsed" -ge "$UDS_READY_TIMEOUT" ]; then
        tail_log_if_any "$VPHONE_LOG" 120
        run_boot_preflight_if_any
        die "UDS shell not ready after ${UDS_READY_TIMEOUT}s (last readiness error: ${last_uds_error:-unknown}; log=$VPHONE_LOG)"
    fi
done
log "UDS shell ready ($elapsed s)"
stage_end uds

# ─── Step 3b: VZNAT outbound-TCP health ─────────────────────────────────────
stage_start network
# UDS/SSH/DNS can all be healthy while a stale macOS InternetSharing process
# silently drops guest public TCP.  That state made Baidu return 412000106 and
# was previously misreported as a fully healthy rig.  The guest has no network
# client binaries (no curl/nc/wget, and vm has no ssh either), so
# autophone.device.nat_health dials via bash's /dev/tcp redirection under
# timeout(1).  Exit 1 = NAT really is down; exit 2 = the probe itself could not
# run, which says nothing about NAT — the two get different remedies.
if [ "${SKIP_NAT_CHECK:-0}" = "1" ]; then
    log "SKIP_NAT_CHECK=1 — 跳过 guest 公网出站健康闸"
else
    log "Checking guest VZNAT outbound TCP (${NAT_PROBE_HOST}:${NAT_PROBE_PORT})..."
    nat_rc=0
    python3 -m autophone.device.nat_health \
        --host "$NAT_PROBE_HOST" \
        --port "$NAT_PROBE_PORT" || nat_rc=$?
    case "$nat_rc" in
        0) log "Guest VZNAT outbound TCP ready" ;;
        2) die "guest NAT probe could not run (guest bash/timeout missing or guest shell unusable) — this is NOT evidence that NAT is down; fix the guest tooling or set SKIP_NAT_CHECK=1 explicitly" ;;
        *) die "guest VZNAT outbound TCP unavailable (${NAT_PROBE_HOST}:${NAT_PROBE_PORT}); restart vphone to recreate VZNATNetworkDeviceAttachment, or set SKIP_NAT_CHECK=1 explicitly" ;;
    esac
fi

stage_end network

# ─── Step 4: SSH bring-up (host keys + sshd + iproxy + 实登验证) ─────────────
stage_start ssh
# 走 canonical 的 tools.phase1.ssh_bringup（不再手搓 iproxy + nc 等待）：开机后
# guest 22 口没有可用 sshd（dropbear 坏 + /var/jb sshd 不自启），裸 iproxy 连不上。
# ssh_bringup 经 UDS shell 生成 host key、起 sshd、host 起 iproxy，再实登一次确认。

if [ -n "$EXPECTED_USB_UDID" ]; then
    log "Binding SSH tunnel to expected usbmux UDID on host port $SSH_PORT..."
    python3 -m tools.phase1.ssh_bringup \
        --host-port "$SSH_PORT" \
        --udid "$EXPECTED_USB_UDID" \
        --pid-file "$IPROXY_PID_FILE" \
        || die "ssh_bringup failed for udid=$EXPECTED_USB_UDID"
elif ssh_login_ok; then
    log "SSH already reachable on port $SSH_PORT (native sshd / existing forward) — skip ssh_bringup"
else
    log "Bringing up SSH (ssh_bringup: host keys + sshd + iproxy + verify)..."
    python3 -m tools.phase1.ssh_bringup \
        --host-port "$SSH_PORT" \
        --pid-file "$IPROXY_PID_FILE" \
        || die "ssh_bringup failed — guest SSH not reachable"
fi
log "SSH ready on port $SSH_PORT"
release_usb_attach_lock
select_ssh_priv_user

# ─── Step 4b: 移除 guest launchd job com.vphone.sshd（BL-193）──────────────
# vm 的只读系统卷 launchd.plist 注册了 com.vphone.sshd（/cores/vphone_sshd.sh，
# 监听 :22）。该 job 启动即以 `sshd: no hostkeys available -- exiting` 退出，约每 5s
# 被 launchd 重拉一次，并持续追加 /var/log/vphone_sshd.log。本项目 SSH 走 dropbear
# :22222，不依赖它。job 定义在只读卷上，每次开机都会重新加载，所以每次开机移除一次。
# iOS 26 guest 的新式 `launchctl bootout` / `print system` 不可用，这里用旧接口
# `launchctl_ list/remove`（2026-09-25 vm 实测 root 执行 remove 返回 0，之后 list
# 查不到；不跨重启）。只卸载这一个 label：不执行 launchctl disable，不修改、不删除、
# 不截断 guest 上的任何文件。vm-alt 没有该 job，查询结果为未加载时跳过。
# fail-open：这是清理动作，不是就绪条件；查询或移除失败只记 WARNING，不中止 bring-up。
# 关闭：SKIP_VPHONE_SSHD_REMOVE=1
VPHONE_SSHD_LABEL="com.vphone.sshd"
GUEST_LEGACY_LAUNCHCTL="/iosbinpack64/bin/launchctl_"
VPHONE_SSHD_STATE=""
VPHONE_SSHD_DETAIL=""

# 查询 job 是否已加载，结果写入 VPHONE_SSHD_STATE：
#   loaded        list 输出含本 label 的 "Label" 行
#   not_loaded    list 输出 Could not find service
#   tool_missing  guest 上没有 launchctl_
#   query_failed  SSH 失败（255 时最多重试 3 次）或输出无法判定
# VPHONE_SSHD_DETAIL 记录用于日志的摘要（PID、LastExitStatus 或输出片段）。
query_vphone_sshd_state() {
    local output="" rc=0 attempt=1
    local query="if [ ! -x ${GUEST_LEGACY_LAUNCHCTL} ]; then echo AUTOPHONE_LAUNCHCTL_MISSING; exit 0; fi; ${GUEST_LEGACY_LAUNCHCTL} list ${VPHONE_SSHD_LABEL} 2>&1; echo AUTOPHONE_LIST_RC=\$?"
    while :; do
        rc=0
        output="$(run_guest_privileged "$query" 2>&1)" || rc=$?
        [ "$rc" = "255" ] && [ "$attempt" -lt 3 ] || break
        log "com.vphone.sshd 状态查询 SSH 失败（rc=${rc}），重试（${attempt}/3）"
        sleep 1
        attempt=$((attempt + 1))
    done
    VPHONE_SSHD_DETAIL="$(printf '%s\n' "$output" | tr '\n' ' ' | cut -c1-200)"
    if [ "$rc" != "0" ]; then
        VPHONE_SSHD_STATE="query_failed"
        VPHONE_SSHD_DETAIL="ssh rc=${rc}: ${VPHONE_SSHD_DETAIL}"
    elif grep -qF AUTOPHONE_LAUNCHCTL_MISSING <<<"$output"; then
        VPHONE_SSHD_STATE="tool_missing"
    elif grep -qF "\"Label\" = \"${VPHONE_SSHD_LABEL}\";" <<<"$output"; then
        VPHONE_SSHD_STATE="loaded"
        VPHONE_SSHD_DETAIL="$(grep -E '"(PID|LastExitStatus)" = ' <<<"$output" \
            | tr -d '\t;"' | tr '\n' ' ' | sed 's/ *$//' || true)"
        [ -n "$VPHONE_SSHD_DETAIL" ] || VPHONE_SSHD_DETAIL="no PID/LastExitStatus"
    elif grep -qF "Could not find service \"${VPHONE_SSHD_LABEL}\"" <<<"$output"; then
        VPHONE_SSHD_STATE="not_loaded"
    else
        VPHONE_SSHD_STATE="query_failed"
    fi
}

remove_vphone_sshd_loop() {
    if [ "${SKIP_VPHONE_SSHD_REMOVE:-0}" = "1" ]; then
        log "SKIP_VPHONE_SSHD_REMOVE=1 — 跳过 guest ${VPHONE_SSHD_LABEL} 移除"
        return 0
    fi
    query_vphone_sshd_state
    case "$VPHONE_SSHD_STATE" in
        not_loaded)
            log "guest ${VPHONE_SSHD_LABEL} 未加载，跳过移除"
            return 0
            ;;
        tool_missing)
            log "guest 无 ${GUEST_LEGACY_LAUNCHCTL}，跳过 ${VPHONE_SSHD_LABEL} 移除"
            return 0
            ;;
        loaded)
            ;;
        *)
            log "WARNING: guest ${VPHONE_SSHD_LABEL} 状态无法判定，未移除（fail-open）：${VPHONE_SSHD_DETAIL}"
            return 0
            ;;
    esac

    log "移除前 guest ${VPHONE_SSHD_LABEL} 状态：loaded（${VPHONE_SSHD_DETAIL}）"
    local remove_output="" remove_rc=0
    remove_output="$(run_guest_privileged "${GUEST_LEGACY_LAUNCHCTL} remove ${VPHONE_SSHD_LABEL}" 2>&1)" \
        || remove_rc=$?
    if [ "$remove_rc" != "0" ]; then
        log "WARNING: ${GUEST_LEGACY_LAUNCHCTL} remove ${VPHONE_SSHD_LABEL} 返回 ${remove_rc}（fail-open，继续启动）：$(printf '%s' "$remove_output" | tr '\n' ' ' | cut -c1-200)"
    else
        log "已执行 ${GUEST_LEGACY_LAUNCHCTL} remove ${VPHONE_SSHD_LABEL}（rc=0）"
    fi

    query_vphone_sshd_state
    case "$VPHONE_SSHD_STATE" in
        not_loaded)
            log "移除后 guest ${VPHONE_SSHD_LABEL} 状态：not_loaded"
            ;;
        *)
            log "WARNING: 移除后 guest ${VPHONE_SSHD_LABEL} 状态：${VPHONE_SSHD_STATE}（fail-open，继续启动）：${VPHONE_SSHD_DETAIL}"
            ;;
    esac
    return 0
}

remove_vphone_sshd_loop
stage_end ssh

# ─── Step 5: vphoned fix —— host 侧每次开机重新部署 + 跑一次 ─────────────────
# /cores 是开机重建/只读层：guest 里的 /cores/vphoned_fix.sh 以及曾注入到
# vphone_jb_setup.sh 的开机调用，都会被重建抹掉；部分启动态还会直接 read-only。
# 所以跟 SSH 一样，改由 host 每次开机推送真源脚本到 writable 的 /var/mobile 再 sudo
# 执行，不指望 guest 内文件存活，也不再依赖 inject_vphoned_fix.sh 的开机注入。
stage_start vphoned
VPHONED_FIX_SRC="${AUTOPHONE_VPHONED_FIX_SRC:-}"
GUEST_VPHONED_FIX="/var/mobile/vphoned_fix.sh"
# tmp 带 $$：即便有别的部署方绕开互斥锁并发（或锁被人为覆写路径），两边也各用各的
# tmp，不会再出现「A 刚 scp 完、B 的 mv 把它消费掉」的竞态。
GUEST_VPHONED_FIX_TMP="/var/mobile/.vphoned_fix.sh.tmp.$$"
deploy_vphoned_fix() {
    password_ssh ssh $SSH_OPTS -T -p "$SSH_PORT" "$SSH_USER@127.0.0.1" \
        "/iosbinpack64/bin/cat > '$GUEST_VPHONED_FIX_TMP'" < "$VPHONED_FIX_SRC" \
    && password_ssh ssh $SSH_OPTS -p "$SSH_PORT" "$SSH_USER@127.0.0.1" \
        "/iosbinpack64/bin/chmod 755 '$GUEST_VPHONED_FIX_TMP' && /iosbinpack64/bin/mv -f '$GUEST_VPHONED_FIX_TMP' '$GUEST_VPHONED_FIX' && test -x '$GUEST_VPHONED_FIX'"
}
if [ -n "$VPHONED_FIX_SRC" ] && [ -f "$VPHONED_FIX_SRC" ]; then
    log "Deploying vphoned_fix.sh → guest $GUEST_VPHONED_FIX (host 侧每次开机重部署)..."
    retry_cmd 3 deploy_vphoned_fix \
        || die "vphoned_fix.sh 部署到 ${GUEST_VPHONED_FIX} 失败（上传+原子替换整组重试 3 次）"
    log "vphoned_fix.sh 部署完成并校验可执行"

    log "Running vphoned_fix..."
    retry_cmd 3 run_guest_privileged "/var/jb/bin/bash $GUEST_VPHONED_FIX" \
        || log "WARNING: vphoned_fix.sh returned non-zero (may be first boot without cache)"
else
    log "AUTOPHONE_VPHONED_FIX_SRC not configured; skipping optional legacy vphoned_fix deployment"
fi
stage_end vphoned

# ─── Step 5b: frida-server + 隧道（API 抓取必需） ────────────────────────────
# /var/jb/Library/LaunchDaemons 不会随这台 VM 开机自动加载，因此不能假定安装包的
# re.frida.server.plist 已经拉起服务。host 侧先经 SSH 幂等确保 guest server，再建
# ssh -L 隧道透到本机 27042，否则 capture_session.spawn_gate 会
# ServerNotRunningError、run 退回普通启动、API 抓取静默失效。
#
# vphone 的 systemhook 会向 frida-server 注入 TweakLoader；Frida 17.16.1 在 iOS 26.1
# 初始化 MobileGestalt/os_log 时会因此 SIGSEGV。仅对 frida-server 子进程设置 hook
# runtime 已支持的安全开关，保留被测 App 的 tweak/Frida 注入能力。
#
# 健壮性（改法 A+B，应对 guest 重启而 host 不重启的场景）：
#   A. 隧道带保活——guest 重启时底层 TCP 断，ssh 客户端默认不主动发现、进程僵死，
#      下次 pgrep 误判“还在跑”。加 ServerAliveInterval/CountMax 让死连接 ~45s 内自退；
#      ExitOnForwardFailure 让端口占用等建立期失败立即退出而非空挂。
#   B. 进程在不直接信——隧道口的进程在不等于 frida-server 还连得通。命中后先跑
#      一次 frida-ps，不通就 kill 旧进程重建，避免僵死隧道造成的“假在跑”。
stage_start frida
FRIDA_TUNNEL_OPTS="-o ServerAliveInterval=15 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes"

# 只认本 Rig 这条隧道：转发映射 **和** 目标 SSH 端口都要对上。裸
# `pgrep -f "$FRIDA_PORT:127.0.0.1:$FRIDA_GUEST_PORT"` 是无边界子串——
# FRIDA_PORT=7042 时会命中另一台的 "27042:127.0.0.1:27042"，进而把别人的 pid
# 写进本机 pid 文件、kill 掉别人的隧道，并把“隧道已在跑”判成真。
frida_tunnel_pids() {
    /bin/ps -axo pid=,command= 2>/dev/null | awk \
        -v map="$FRIDA_PORT:127.0.0.1:$FRIDA_GUEST_PORT" \
        -v port="$SSH_PORT" '
        {
            has_map = 0
            has_port = 0
            for (i = 2; i <= NF; i++) {
                if (($i == "-L" && $(i + 1) == map) || $i == "-L" map) has_map = 1
                if (($i == "-p" && $(i + 1) == port) || $i == "-p" port) has_port = 1
            }
            if (has_map && has_port) print $1
        }'
}

open_frida_tunnel() {
    log "Opening frida tunnel host:$FRIDA_PORT → guest:$FRIDA_GUEST_PORT..."
    retry_cmd 3 password_ssh ssh $SSH_OPTS $FRIDA_TUNNEL_OPTS -fN \
        -L "$FRIDA_PORT:127.0.0.1:$FRIDA_GUEST_PORT" -p "$SSH_PORT" "$SSH_USER@127.0.0.1"
    frida_tunnel_pids | head -1 > "$FRIDA_TUNNEL_PID_FILE" || true
}

kill_frida_tunnel() {
    # 只杀本机这条端口转发 ssh，不误伤别的 ssh 会话（更不能误伤别的 Rig）。
    if [ -f "$FRIDA_TUNNEL_PID_FILE" ]; then
        kill_recorded_helper "$FRIDA_TUNNEL_PID_FILE" \
            "-L $FRIDA_PORT:127.0.0.1:$FRIDA_GUEST_PORT" "-p $SSH_PORT"
    else
        for pid in $(frida_tunnel_pids); do
            kill "$pid" 2>/dev/null || true
        done
    fi
    sleep 1
}

# 返回 0=在跑、1=未跑。SSH 本身失败（rc=255）不能当成“未跑”，否则会去重复启动已在跑的
# frida-server 并因端口占用 FATAL；重试 3 次，仍失败时原样返回 255。
guest_frida_running() {
    local attempt=1
    local rc=255

    while [ "$attempt" -le 3 ]; do
        if password_ssh ssh $SSH_OPTS -o LogLevel=ERROR -p "$SSH_PORT" "$SSH_USER@127.0.0.1" \
            '/bin/ps -A -o command | /var/jb/usr/bin/grep -Eq "^/var/jb/usr/sbin/frida-server([[:space:]]|$)"'; then
            return 0
        else
            rc=$?
        fi
        [ "$rc" -eq 255 ] || return "$rc"
        if [ "$attempt" -lt 3 ]; then
            log "frida-server 运行探测 SSH 失败（rc=255），重试 $((attempt + 1))/3"
            sleep 1
        fi
        attempt=$((attempt + 1))
    done
    return "$rc"
}

read_guest_frida_version() {
    local attempt=1
    local output=""
    local rc=1

    while [ "$attempt" -le 3 ]; do
        if output="$(
            password_ssh ssh $SSH_OPTS -o LogLevel=ERROR \
                -p "$SSH_PORT" "$SSH_USER@127.0.0.1" \
                'version="$(/var/jb/usr/bin/dpkg-query -W -f="\${Version}" re.frida.server 2>/dev/null || true)"; if [ -z "$version" ] && [ -x /var/jb/usr/sbin/frida-server ]; then version="$(/var/jb/usr/sbin/frida-server --version 2>/dev/null || true)"; fi; [ -n "$version" ] && printf "%s\n" "$version"' 2>&1
        )"; then
            printf '%s\n' "$output"
            return 0
        else
            rc=$?
        fi

        if [ "$attempt" -lt 3 ]; then
            log "guest Frida version query failed (rc=$rc), retrying ($attempt/3): $output" >&2
            sleep 1
        fi
        attempt=$((attempt + 1))
    done

    log "guest Frida version query failed after 3 attempts (last rc=$rc): $output" >&2
    return "$rc"
}

ensure_guest_frida_server() {
    if guest_frida_running; then
        log "guest frida-server already running"
        return
    fi

    log "Starting guest frida-server with tweak injection disabled..."
    retry_cmd 3 run_guest_privileged \
        "/var/jb/usr/bin/env DISABLE_TWEAKS=1 _MSSafeMode=1 /var/jb/usr/sbin/frida-server -D" \
        || die "guest frida-server 启动命令失败"

    sleep 2
    guest_frida_running \
        || die "guest frida-server 启动后未保持运行（检查 CrashReporter/frida-server*.ips）"
    log "guest frida-server started with tweak injection disabled"
}

if [ "${SKIP_FRIDA:-0}" = "1" ]; then
    log "SKIP_FRIDA=1 — 跳过 frida 隧道"
    # 显式跳过不是失败：阶段正常结束，状态记 skipped（§6.1）。
    stage_end frida skipped
else
    FRIDA_PY="${FRIDA_PY:-$AUTOPHONE_DIR/.venv/bin/python}"
    FRIDA_PS="${FRIDA_PS:-}"
    if [ ! -x "$FRIDA_PY" ]; then
        die "Frida Python 不存在或不可执行：${FRIDA_PY}（先 uv sync --group frida）"
    fi
    if [ -n "$FRIDA_PS" ] && [ ! -x "$FRIDA_PS" ]; then
        die "FRIDA_PS 指定的探针不存在或不可执行：${FRIDA_PS}"
    fi

    # uv console script 的 shebang 是创建 venv 时的绝对路径；仓库移动后
    # .venv/bin/frida-ps 仍可带 +x，却会在 exec 时 bad interpreter。默认通过同一个
    # venv Python 运行模块，避免把 host 探针自身损坏误报为 guest/tunnel 不通。
    # 隔离 A/B 环境仍可显式传 FRIDA_PS 指向配套探针。
    run_frida_ps() {
        if [ -n "$FRIDA_PS" ]; then
            "$FRIDA_PS" "$@"
        else
            "$FRIDA_PY" -m frida_tools.ps "$@"
        fi
    }
    "$FRIDA_PY" -c 'import frida_tools.ps' \
        || die "Frida tools 模块不可导入：${FRIDA_PY} -m frida_tools.ps（先 uv sync --group frida）"

    # Frida 是 host client ↔ guest server 的双端协议。仅验 frida-ps 能列进程不够：
    # 版本漂移可能到 attach/load/首个 Hook 才暴露，让崩溃诊断失去基线。
    # 启动入口因此 fail-closed 锁死精确同版；A/B 候选环境用 FRIDA_PY /
    # FRIDA_PS / FRIDA_PORT 指向另一套配对环境，不覆盖基线。
    HOST_FRIDA_VERSION="$("$FRIDA_PY" -c 'import frida; print(frida.__version__)')" \
        || die "无法读取 host frida-python 版本：${FRIDA_PY}"
    GUEST_FRIDA_VERSION="$(read_guest_frida_version)" \
        || die "连续 3 次无法读取 guest re.frida.server 版本（SSH 认证、软件包或 dpkg 异常；见上方最后错误）"
    # Debian revision suffix does not belong to Frida's protocol version.
    GUEST_FRIDA_VERSION="${GUEST_FRIDA_VERSION%%-*}"
    if [ "$HOST_FRIDA_VERSION" != "$GUEST_FRIDA_VERSION" ]; then
        die "Frida 版本不匹配：host=${HOST_FRIDA_VERSION} guest=${GUEST_FRIDA_VERSION}。请配对 host/server；升级走 docs/frida-crash-diagnosis.md 的隔离 A/B，不覆盖基线"
    fi
    log "frida version contract matched: host=guest=${HOST_FRIDA_VERSION}"

    ensure_guest_frida_server

    # 改法 B：命中本 Rig 隧道后补一次真连通性校验，僵死隧道则 kill 重建。
    if [ -n "$(frida_tunnel_pids)" ]; then
        if run_frida_ps -H "127.0.0.1:$FRIDA_PORT" >/dev/null 2>&1; then
            log "frida tunnel host:$FRIDA_PORT already running and reachable"
        else
            log "frida tunnel 进程在但 frida-ps 不通（疑似 guest 重启后僵死）— kill 重建"
            kill_frida_tunnel
            open_frida_tunnel || die "frida tunnel ssh -L failed"
        fi
    else
        open_frida_tunnel || die "frida tunnel ssh -L failed"
    fi

    # 连通性以 frida-ps -H 为准，不看 guest netstat（输出不全、不可作判据）。
    # frida-server 自启可能略晚，最多等 15s。
    log "Verifying frida connectivity (frida-ps -H 127.0.0.1:$FRIDA_PORT)..."
    elapsed=0
    until run_frida_ps -H "127.0.0.1:$FRIDA_PORT" >/dev/null 2>&1; do
        sleep 1
        elapsed=$((elapsed + 1))
        if [ $elapsed -ge 15 ]; then
            die "frida 经 host:$FRIDA_PORT 列不出进程（15s）：frida-server 没起 或 隧道断"
        fi
    done
    log "frida reachable ($elapsed s)：host:$FRIDA_PORT 列得到进程"

    # BL-232 阶段 1（R1）：启动与自动恢复只枚举进程，不 attach SpringBoard。attach 并
    # detach SpringBoard 后会留下 Frida agent 映射残留（backlog §9.69），之后的安装会被
    # 安装前残留检查拒绝。判据为 frida-server 经 Python 绑定能列出进程且其中有
    # SpringBoard；探针在独立子进程中运行，有硬超时。注入能力不在此证明，由 Rig 准备
    # 对被测 App 的契约探针承担。
    verify_frida_springboard_enumerated() {
        log "Verifying frida enumeration (SpringBoard listed, no attach, ${FRIDA_ATTACH_TIMEOUT}s bound)..."
        FRIDA_ENUM_RESULT="$(
            "$FRIDA_PY" -m tools.phase1.frida_health \
                --host "127.0.0.1:$FRIDA_PORT" \
                --target SpringBoard \
                --timeout "$FRIDA_ATTACH_TIMEOUT"
        )" || die "Frida 枚举不到 SpringBoard（只枚举，不 attach）：${FRIDA_ENUM_RESULT:-probe produced no result}"
        log "frida enumeration ready (enumerate_only): $FRIDA_ENUM_RESULT"
    }
    verify_frida_springboard_enumerated
    stage_end frida
fi

# ─── 阶段 health: Step 6 健康守护 + Step 7 轮询到 healthy ───────────────────
stage_start health

# ─── Step 6: Health daemon ──────────────────────────────────────────────────

# A process scan followed by Popen is racy when two recovery hooks arrive
# together. The manager holds a separate lock across scan → reap → spawn and
# reconciles pre-existing duplicate guards for this exact socket.
ON_DOWN_HOOK="$AUTOPHONE_DIR/scripts/on_vphone_down.sh"
HEALTH_RESULT="$(python3 -m tools.phase1.health_daemon ensure \
    --sock "$SOCK" \
    --state-file "$HEALTH_JSON" \
    --pid-file "$HEALTH_PID_FILE" \
    --log-file "$HEALTH_LOG" \
    --on-down "$ON_DOWN_HOOK")" \
    || die "failed to establish singleton health daemon for sock=$SOCK"
log "Health daemon reconciled: $HEALTH_RESULT"

# ─── Step 7: Wait for healthy ───────────────────────────────────────────────

log "Polling health until healthy..."
elapsed=0
while true; do
    if [ -f "$HEALTH_JSON" ]; then
        state=$(python3 -c "import json; print(json.load(open('$HEALTH_JSON'))['state'])" 2>/dev/null || echo "error")
        if [ "$state" = "healthy" ]; then
            log "Chain healthy!"
            break
        fi
        log "  state=$state, waiting..."
    fi
    sleep 2
    elapsed=$((elapsed + 2))
    if [ $elapsed -ge 30 ]; then
        die "Health did not reach 'healthy' after 30s (last state: ${state:-unknown})"
    fi
done
stage_end health

# ─── Step 8: Control Plane 引导 ─────────────────────────────────────────────
#
# 目标架构 §16.0「兼容路径退出」第一步：本机开发默认走 Control Plane 路径。
# 拓扑是三个进程——serve(:8000) 把 Run 写成 Assignment 进 PostgreSQL，
# autophone-control(:8100) 从 PG 轮询派单并持有 Agent WebSocket，rig agent 连
# :8100 执行。serve 自己既不跑 Scheduler 也不提供 agent WebSocket，
# 所以 autophone-control 这个进程省不掉。
#
# 全程 best-effort：任何一环不成只 WARNING 并回退单 VM 兼容路径，不 die。VM 才是
# 稀缺资源，控制面起不来不该把一台已经健康的 VM 判成启动失败；退出码也不该把
# 「VM 起不来」和「PG 起不来」混成同一类故障。
#
# 「回退」必须对 serve 也成立：serve 判定走哪条路只看 control-plane.env，所以这个
# 文件是引导成功的**回执**，只在全部校验通过后才写；任何失败分支与
# SKIP_CONTROL_PLANE=1 都先把它删掉。否则脚本判 off、serve 判 on，Assignment 写进
# PG 却没有 Scheduler 派单，run 永久停在 queued 且两边都不报错。
# 整体关闭：SKIP_CONTROL_PLANE=1

CONTROL_PLANE_STATUS="off(skipped)"
CONTROL_BOOTSTRAP_STARTED_AT=0
CONTROL_PLANE_ENV_FILE="$RUNTIME_DIR/control-plane.env"
CONTROL_TARGET_FILE="$RUNTIME_DIR/device-target.json"
CONTROL_HOST_ID_FILE="$RUNTIME_DIR/host-id"
CONTROL_PID_FILE="$RUNTIME_DIR/control-plane.pid"
CONTROL_LOG="$RUNTIME_DIR/control-plane.log"
RIG_AGENT_PID_FILE="$RUNTIME_DIR/rig-agent.pid"
RIG_AGENT_LOG="$RUNTIME_DIR/rig-agent.log"
CONTROL_DB_NAME="${AUTOPHONE_CONTROL_DB:-autophone_control}"
CONTROL_DSN="${AUTOPHONE_DATABASE_URL:-postgresql:///$CONTROL_DB_NAME}"
# 探测 URL 由端口单一真源派生，避免「探 A 端口、子进程绑 B 端口」。
CONTROL_PORT="${AUTOPHONE_CONTROL_PORT:-8100}"
CONTROL_WS="${AUTOPHONE_CONTROL_WS:-ws://127.0.0.1:$CONTROL_PORT/api/control/agent/ws}"
CONTROL_HTTP="http://127.0.0.1:$CONTROL_PORT"
CONTROL_HEALTH_MARK='"service":"autophone-control-plane"'
CONTROL_PG_FORMULA="${AUTOPHONE_CONTROL_PG_FORMULA:-postgresql@17}"
UV_BIN="${UV_BIN:-uv}"
# 后台服务直接用 venv 里的解释器起，PID 文件记录的就是真正持有端口的进程。
# 经 `uv run` 起的话 PID 是包装进程，kill 掉它 listener 仍活着占住端口，
# 下一次拉起必然 EADDRINUSE 且每次重跑都复现。同 FRIDA_PY 的既有做法。
# 前台命令（emit_target / control rig list）仍走 uv run：它们的 PID 无所谓，
# 而且 uv 会顺带把 platform 依赖组同步进同一个 .venv，供下面两个 daemon 用。
CONTROL_PY="${AUTOPHONE_CONTROL_PY:-$AUTOPHONE_DIR/.venv/bin/python}"

# rig agent 注册进 Control Plane 的能力集合（目标架构 §16.0 第三步④ / BL-026）。
# 不传 --capability 时 `autophone/rig/main.py` 的默认值是 []，Scheduler 于是只能派
# `required_capabilities` 为空集的 Assignment；带非空能力的 Assignment（装包、交付
# 设备工作）会永远停在 wait_reason=capability_support_insufficient。
#
# 标识与提交侧同一套（`autophone/workflow/coordinator/device_work.py` 的
# DEVICE_WORK_CAPABILITIES、`autophone/application/package_install.py` 的装包 Assignment）：
#   vm_uds      —— UDS 设备面（截图 / tap / shell）。Step 1-7 全部探通才走到这里，成立。
#   app_install —— 经包仓库装 IPA，与 vm_uds 同一条 UDS/SSH 链，同时成立。
#   frida       —— Step 5b 的 frida-server + 隧道。SKIP_FRIDA=1 时该步跳过，不声明；
#                  不跳过时该步失败会 die，走到这里即已探通。
# 只声明本次 bring-up 真的探通的能力：多报会把干不了的活派过来，在 rig 上以失败收场。
RIG_CAPABILITIES="vm_uds app_install"
if [ "${SKIP_FRIDA:-0}" != "1" ]; then
    RIG_CAPABILITIES="$RIG_CAPABILITIES frida"
fi

# host_id 一旦变化，同一个 rig_id 在新 host 下注册会被 register_rig 拒绝
# （Rig host cannot change without increasing rig_generation）。macOS 的
# LocalHostName 会因 Bonjour 重名、改机器名、换网络而变，所以首次派生后固化到
# $RUNTIME_DIR/host-id，之后一律读该文件。AUTOPHONE_HOST_ID 显式覆写优先级最高。
normalize_host_id() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9._-' '-' | sed 's/-*$//'
}

derive_host_id() {
    local name
    name="$(scutil --get LocalHostName 2>/dev/null || true)"
    [ -n "$name" ] || name="$(hostname -s 2>/dev/null || true)"
    name="$(normalize_host_id "$name")"
    [ -n "$name" ] || name="host-local"
    printf '%s' "$name"
}

resolve_host_id() {
    local explicit="${AUTOPHONE_HOST_ID:-}" stored=""
    if [ -n "$explicit" ]; then
        normalize_host_id "$explicit"
        return 0
    fi
    if [ -f "$CONTROL_HOST_ID_FILE" ]; then
        stored="$(normalize_host_id "$(cat "$CONTROL_HOST_ID_FILE" 2>/dev/null || true)")"
        if [ -n "$stored" ]; then
            printf '%s' "$stored"
            return 0
        fi
    fi
    derive_host_id
}

discard_control_plane_env() {
    # serve 只认这个文件。脚本判 off 时必须让它也判 off。
    rm -f "$CONTROL_PLANE_ENV_FILE"
}

control_plane_skip() {
    CONTROL_PLANE_STATUS="off($1)"
    discard_control_plane_env
    log "WARNING: $2 — 本次回退单 VM 兼容路径"
}

ensure_postgres_running() {
    if ! command -v pg_isready >/dev/null 2>&1; then
        control_plane_skip "no-postgres-client" "未找到 pg_isready"
        return 1
    fi
    if pg_isready -q; then
        return 0
    fi
    log "PostgreSQL 未就绪，尝试 brew services start $CONTROL_PG_FORMULA ..."
    brew services start "$CONTROL_PG_FORMULA" >/dev/null 2>&1 || true
    local waited=0
    until pg_isready -q; do
        sleep 1
        waited=$((waited + 1))
        if [ "$waited" -ge 15 ]; then
            control_plane_skip "postgres-unavailable" "PostgreSQL 15s 内仍不可用"
            return 1
        fi
    done
    log "PostgreSQL ready after $waited s"
    return 0
}

ensure_control_database() {
    # 单条查询而不是 `psql -l | cut | grep -qx`：pipefail 下 grep -q 命中即退出会给
    # 上游 psql 一个 SIGPIPE(141)，管道整体返回 141，被误判成「库不存在」。
    local found
    found="$(psql -d postgres -tAc "select 1 from pg_database where datname = '$CONTROL_DB_NAME'" 2>/dev/null || true)"
    if [ "$found" = "1" ]; then
        return 0
    fi
    log "creating database $CONTROL_DB_NAME ..."
    if ! createdb "$CONTROL_DB_NAME" >/dev/null 2>&1; then
        control_plane_skip "createdb-failed" "createdb 失败 db=$CONTROL_DB_NAME"
        return 1
    fi
    return 0
}

ensure_control_python() {
    [ -x "$CONTROL_PY" ] && return 0
    control_plane_skip "python-venv-missing" \
        "找不到可执行的 venv 解释器 python=$CONTROL_PY （先跑 uv sync --group platform）"
    return 1
}

write_control_plane_env() {
    # 引导成功的回执，全部校验通过后才写（见本节开头）。每次重写：内容完全由本次
    # 引导的取值决定，重写即对齐。serve 在另一个终端启动时靠它接上同一个控制面
    # （autophone.adapters.cli.serve.control_plane_env_path 按同一套默认值把这个路径推回来）。
    cat > "$CONTROL_PLANE_ENV_FILE" <<CONTROL_ENV_EOF
# generated by scripts/start_vphone.sh; regenerated on every successful bring-up, do not edit
AUTOPHONE_DATABASE_URL=$CONTROL_DSN
AUTOPHONE_CONTROL_WS=$CONTROL_WS
CONTROL_ENV_EOF
    chmod 600 "$CONTROL_PLANE_ENV_FILE" 2>/dev/null || true
}

control_http_healthy() {
    # 校验响应体：别的 checkout 的控制面（指向另一个 DSN）或任何在同端口上提供
    # /health 的服务，都会让裸 `curl -sf` 判成就绪，rig agent 于是注册到错误的库。
    local body
    body="$(curl -sf --max-time 3 "$CONTROL_HTTP/health" 2>/dev/null || true)"
    case "$body" in
        *"$CONTROL_HEALTH_MARK"*) return 0 ;;
        *) return 1 ;;
    esac
}

control_http_source_identity_reason() {
    # 在跑的控制面进程执行的代码是否就是本 checkout 当前的代码（BL-151）。
    # 控制面的代码与 Agent 一样是启动时快照：09-16 实测旧控制面不解析 Agent 握手里的
    # source_identity，rigs 行静默写 null，host 侧的身份闸随之退化成 WARNING 放行。
    # 判据取 /health 自报的身份，比对走 rig_agent_source_identity_reason 用的同一个
    # compare_reported_identity，不另写一份。body 经环境变量传进去，避免引号与换行。
    # stdout 打一行中文原因，退出码 0 表示一致。
    local body
    body="$(curl -sf --max-time 3 "${CONTROL_HTTP}/health" 2>/dev/null || true)"
    (
        cd "${AUTOPHONE_DIR}" || exit 1
        CONTROL_HEALTH_BODY="${body}" "${CONTROL_PY}" -c '
import json, os, sys
from autophone.rig.source_identity import IDENTITY_MATCH, compare_reported_identity
try:
    payload = json.loads(os.environ.get("CONTROL_HEALTH_BODY") or "{}")
except ValueError:
    payload = {}
reported = payload.get("source_identity") if isinstance(payload, dict) else None
status, reason = compare_reported_identity(
    reported if isinstance(reported, dict) else None,
    subject="在跑的控制面",
    host_label="本次 bring-up",
)
print(reason)
sys.exit(0 if status == IDENTITY_MATCH else 1)
'
    )
}

start_detached_service() {
    local log_file="$1"
    shift
    "$CONTROL_PY" - "$AUTOPHONE_DIR" "$log_file" "$CONTROL_PY" "$@" <<'PYTHON'
import subprocess
import sys

cwd, log_path, *command = sys.argv[1:]
with open(log_path, "ab", buffering=0) as log:
    child = subprocess.Popen(
        command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True,
    )
print(child.pid)
PYTHON
}

start_control_http() {
    local reason
    if control_http_healthy; then
        if reason="$(control_http_source_identity_reason)"; then
            # 变量一律加花括号：bash 3.2 + set -u 下 `$VAR` 紧跟全角字符会报
            # unbound variable（见 scripts/on_vphone_down.sh 的同类修复）。
            log "autophone-control already healthy at ${CONTROL_HTTP}（${reason}）"
            return 0
        fi
        # Agent 经 WebSocket 连控制面，控制面重启后 Agent 自己重连（09-16 实测约 1s），
        # 所以这里只重启控制面，不动 Agent。
        log "autophone-control 在跑但${reason:-源码身份无法核对} — 重启以加载当前代码（rig agent 会自动重连，无需一并重启）"
    fi
    kill_recorded_helper "${CONTROL_PID_FILE}" "autophone.control.http"
    # 旧进程若还占着端口，新进程绑不上，而下面的 until control_http_healthy 会被**旧**
    # 进程立刻答成「就绪」，闸就静默失效了。所以先确认端口上的旧控制面真的没了。
    local stopping=0
    while [ "${stopping}" -lt 30 ]; do
        control_http_healthy || break
        sleep 0.1
        stopping=$((stopping + 1))
    done
    if control_http_healthy; then
        control_plane_skip "control-http-not-stopped" \
            "在跑的 autophone.control.http 没能停掉（PID 文件 ${CONTROL_PID_FILE} 不匹配或进程属于别的会话），拒绝把它当就绪"
        return 1
    fi
    log "starting autophone-control ..."
    local child
    child="$(start_detached_service "$CONTROL_LOG" -m autophone.control.http)" || return 1
    printf '%s\n' "$child" > "$CONTROL_PID_FILE"
    local waited=0
    until control_http_healthy; do
        sleep 1
        waited=$((waited + 1))
        if [ "$waited" -ge 20 ]; then
            control_plane_skip "control-http-unavailable" \
                "autophone-control 20s 内没起来 log=$CONTROL_LOG"
            return 1
        fi
    done
    log "autophone-control ready after $waited s"
    return 0
}

rig_agent_running_pid() {
    (
        cd "$AUTOPHONE_DIR" || exit 1
        "$CONTROL_PY" -c '
from pathlib import Path
import sys
from autophone.rig.process_guard import running_agent_pid
pid = running_agent_pid(Path(sys.argv[1]), Path(sys.argv[2]))
if pid is not None:
    print(pid)
' "$CONTROL_TARGET_FILE" "$RIG_AGENT_PID_FILE"
    )
}

stop_rig_agent() {
    local pid="$1" waited=0
    kill "$pid" 2>/dev/null || return 0
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$waited" -ge 100 ]; then
            control_plane_skip "rig-agent-stop-timeout" "Agent $pid 尚未退出，拒绝启动替代进程"
            return 1
        fi
        sleep 0.1
        waited=$((waited + 1))
    done
}

rig_agent_capabilities_current() {
    # 在跑的 Agent 注册的能力集合是否与本次 bring-up 声明的完全一致。
    # 能力只在 WebSocket 握手时注册一次，所以「进程还在」不等于「注册的还是这一套」：
    # 上一轮（或加 --capability 之前）起的 Agent 会把旧集合一直留在 rigs 表里。
    local pid="$1" command declared expected
    command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    [ -n "$command" ] || return 1
    # 按完整参数值比较能力集合，避免 grep -c 只统计单行 ps 输出而错误重启。
    declared="$(printf '%s\n' "$command" | awk '{for (i=1; i<=NF; i++) if ($i == "--capability") print $(++i)}' | sort -u)"
    expected="$(printf '%s\n' "$RIG_CAPABILITIES" | tr ' ' '\n' | sort -u)"
    [ "$declared" = "$expected" ]
}

rig_agent_source_identity_reason() {
    # 在跑的 Agent 执行的代码是否就是本 checkout 当前的代码（BL-151）。
    # 进程内代码是启动时快照，所以「进程还在」「能力集合还对」都不说明它跑的是新代码：
    # 09-16 实测到旧 Agent 忽略新加的 run_spec 字段，整轮空跑且 host 侧退出码 0。
    # 判据取 runtime dir 里 Agent 自己写的身份文件（与 PID 文件同目录），不走控制面
    # ——控制面此刻未必可达，而重启判定不该依赖它。
    # stdout 打一行中文原因，退出码 0 表示一致。
    (
        cd "$AUTOPHONE_DIR" || exit 1
        "$CONTROL_PY" -c '
import sys
from autophone.rig.source_identity import IDENTITY_MATCH, identity_file_status
status, reason = identity_file_status(sys.argv[1], pid=int(sys.argv[2]))
print(reason)
sys.exit(0 if status == IDENTITY_MATCH else 1)
' "$(dirname "$RIG_AGENT_PID_FILE")" "$1"
    )
}

start_rig_agent() {
    local running reason
    if ! running="$(rig_agent_running_pid)"; then
        control_plane_skip "rig-agent-identity-unverified" "Agent 身份无法核对，拒绝另起进程"
        return 1
    fi
    if [ -n "$running" ]; then
        if ! rig_agent_capabilities_current "$running"; then
            log "autophone-rig-agent 在跑但注册的能力集合不是 [$RIG_CAPABILITIES] — 重启以重新注册"
            stop_rig_agent "$running" || return 1
        elif reason="$(rig_agent_source_identity_reason "$running")"; then
            # 变量一律加花括号：bash 3.2 + set -u 下 `$VAR` 紧跟全角字符会报
            # unbound variable（见 scripts/on_vphone_down.sh 的同类修复）。
            log "autophone-rig-agent already running for target ${CONTROL_TARGET_FILE}（${reason}）"
            return 0
        else
            log "autophone-rig-agent 在跑但${reason:-源码身份无法核对} — 重启以加载当前代码"
            stop_rig_agent "$running" || return 1
        fi
    fi
    log "starting autophone-rig-agent (capabilities: $RIG_CAPABILITIES) ..."
    # 子进程 cwd 固定为仓库根：run 产物按 STATE_ROOT 解析（默认即仓库根），与 serve 落到同一个
    # runs/platform。要改落点用 AUTOPHONE_RUN_ROOT，别靠 cwd 漂移。
    local capability
    local agent_args=(-m autophone.rig.main --target "$CONTROL_TARGET_FILE" --control-ws "$CONTROL_WS")
    for capability in $RIG_CAPABILITIES; do
        agent_args+=(--capability "$capability")
    done
    if [ -n "${AUTOPHONE_RUN_ROOT:-}" ]; then
        agent_args+=(--run-root "$AUTOPHONE_RUN_ROOT")
    fi
    (
        local child waited=0 owner
        child="$(start_detached_service "$RIG_AGENT_LOG" "${agent_args[@]}")" || return 1
        # 只有取得进程锁的 Agent 写 PID，启动失败的子进程不得覆盖在跑者。
        while [ "$waited" -lt 50 ]; do
            if ! kill -0 "$child" 2>/dev/null; then
                return 1
            fi
            owner="$(cat "$RIG_AGENT_PID_FILE" 2>/dev/null || true)"
            [ "$owner" = "$child" ] && return 0
            sleep 0.1
            waited=$((waited + 1))
        done
        kill "$child" 2>/dev/null || true
        return 1
    ) || {
        control_plane_skip "rig-agent-start-failed" "Agent 未取得进程锁或启动失败，查看 $RIG_AGENT_LOG"
        return 1
    }
    return 0
}

control_rig_row() {
    # 打印本 Rig 在 Control Plane 里的 "<status> <generation> <last_seen_epoch>"；
    # 没注册就什么都不打印。三个字段一起取：quarantine 分支要用 generation 写
    # Recovery Receipt，用 last_seen 判本轮 Agent 有没有真的握过手。
    "$UV_BIN" run --group platform python3 -m autophone.cli control rig list 2>/dev/null \
        | python3 -c '
import json, sys
from datetime import datetime
rig_id = sys.argv[1]
try:
    rigs = json.loads(sys.stdin.read() or "{}").get("rigs", [])
except Exception:
    sys.exit(0)
for rig in rigs:
    if rig.get("rig_id") != rig_id:
        continue
    try:
        last_seen = datetime.fromisoformat(str(rig.get("last_seen"))).timestamp()
    except Exception:
        last_seen = 0.0
    print("%s %s %.3f" % (rig.get("status"), rig.get("generation"), last_seen))
    break
' "$RIG_ID"
}

handshake_seen_this_bringup() {
    # register_rig 每次握手都把 rigs.last_seen 刷成 now()，所以「本轮之后刷新过」
    # 是本机能拿到的、最强的「Agent WebSocket 真的连上了」判据。
    # 只判 agent 进程存在会漏掉「进程活着但 WS 没连上」——活机第二轮实测到过。
    awk "BEGIN{exit !(${1:-0} >= $CONTROL_BOOTSTRAP_STARTED_AT)}"
}

restart_rig_agent() {
    # Rig 在 quarantine 时不会被派单，重启它的 Agent 不会打断任何执行中的 Attempt；
    # 目的只是强制一次握手，好用 last_seen 判 WS 是否真的通。
    local running
    running="$(rig_agent_running_pid)" || return 1
    [ -z "$running" ] || stop_rig_agent "$running" || return 1
    start_rig_agent
}

write_rig_probe_result() {
    # Recovery Receipt 必须带真实 Probe Result（目标架构 §M4：「恢复操作必须写入带
    # Generation、Actor 和 Probe Result 的 Recovery Receipt；失败回执保持 Quarantine」）。
    # 唯一的证据来源是 Step 7 判过的健康守护状态文件——这里现读一次，不构造探针结论：
    # state 不是 healthy 就写 passed=false，Control Plane 于是保持 quarantine。
    python3 - "$HEALTH_JSON" "$1" <<'PROBE_EOF'
import json, sys, time
health_path, out_path = sys.argv[1], sys.argv[2]
read_error = ""
try:
    with open(health_path, encoding="utf-8") as handle:
        health = json.load(handle)
except Exception as exc:
    health, read_error = dict(), "%s: %s" % (type(exc).__name__, exc)
state = str(health.get("state") or "")
probe = dict(
    passed=state == "healthy",
    source="scripts/start_vphone.sh Step 7 health daemon gate",
    health_state=state or "unreadable",
    health_state_file=health_path,
    last_error=health.get("last_error"),
    last_probe_ts=health.get("last_probe_ts"),
    probe_count=health.get("probe_count"),
    checked_at=time.time(),
)
if read_error:
    probe["read_error"] = read_error
with open(out_path, "w", encoding="utf-8") as handle:
    json.dump(probe, handle, ensure_ascii=False)
print("passed" if probe["passed"] else "failed")
PROBE_EOF
}

RIG_RECOVERY_MODE=""
RIG_RECOVERY_ID=""
RIG_RECOVERY_STATE=""
RIG_RECOVERY_INCIDENTS=""
RECOVERY_RESUME_TIMEOUT="${AUTOPHONE_RECOVERY_RESUME_TIMEOUT:-900}"
RECOVERY_RESUME_POLL="${AUTOPHONE_RECOVERY_RESUME_POLL:-5}"

control_cli_manual() {
    # 可复制的手工命令：另一个终端没有本脚本导出的 AUTOPHONE_DATABASE_URL，必须显式带上。
    printf 'AUTOPHONE_DATABASE_URL=%s uv run --group platform python3 -m autophone.cli control %s' \
        "$CONTROL_DSN" "$*"
}

control_active_recoveries() {
    # 打印本 Rig 未关闭的 recovery incident，每行 "<recovery_id> <state>"，按创建时间升序。
    # 查询失败返回非零，与「没有未关闭 incident」（零输出且返回 0）区分。
    local output
    output="$("$UV_BIN" run --group platform python3 -m autophone.cli control recovery list \
        --rig-id "$RIG_ID" --active 2>/dev/null)" || return 1
    printf '%s' "$output" | python3 -c '
import json, sys
try:
    rows = json.loads(sys.stdin.read())["recoveries"]
    lines = ["%s %s" % (row["recovery_id"], row["state"]) for row in rows]
except Exception:
    sys.exit(1)
for line in lines:
    print(line)
'
}

control_recovery_state() {
    local output
    output="$("$UV_BIN" run --group platform python3 -m autophone.cli control recovery get "$1" 2>/dev/null)" || return 1
    printf '%s' "$output" | python3 -c '
import json, sys
try:
    print(json.loads(sys.stdin.read())["state"])
except Exception:
    sys.exit(1)
'
}

resume_recovery_incident() {
    # 未关闭的 down-edge incident 只能由 Recovery Assignment 关闭，record_rig_recovery
    # 对此显式拒绝（postgres.py record_rig_recovery）。RecoveryCoordinator.resume 只对
    # quarantined 要求 retry；observed/abort_requested/deferred/waiting_lease 不带 retry
    # 即重新入队或保持排队，recovering 直接返回当前行。
    local recovery_id="$1" state="$2" retry_args=""
    [ "$state" = "quarantined" ] && retry_args="--retry"
    RIG_RECOVERY_MODE="assignment"
    RIG_RECOVERY_ID="$recovery_id"
    RIG_RECOVERY_STATE="$state"
    if ! release_start_lock_for_recovery; then
        log "WARNING: 无法移交 rig 生命周期锁 ${LOCK_DIR}，不自动 resume（内层恢复脚本会等锁至超时）recovery_id=$recovery_id state=$state"
        return 1
    fi
    log "rig $RIG_ID 有未关闭的 recovery incident（recovery_id=$recovery_id state=${state}）—— 走 control recovery resume ${retry_args:-（无 --retry）}"
    # shellcheck disable=SC2086
    if ! "$UV_BIN" run --group platform python3 -m autophone.cli control recovery resume \
            "$recovery_id" $retry_args >/dev/null; then
        log "WARNING: control recovery resume 失败 recovery_id=$recovery_id state=$state"
        return 1
    fi
    local waited=0 current="$state" failures=0
    log "等待 Recovery Assignment 完成 recovery_id=${recovery_id}（超时 ${RECOVERY_RESUME_TIMEOUT}s）"
    while [ "$waited" -lt "$RECOVERY_RESUME_TIMEOUT" ]; do
        sleep "$RECOVERY_RESUME_POLL"
        waited=$((waited + RECOVERY_RESUME_POLL))
        if ! current="$(control_recovery_state "$recovery_id")"; then
            current="unreadable"
            failures=$((failures + 1))
            [ "$failures" -lt 3 ] && continue
            break
        fi
        failures=0
        RIG_RECOVERY_STATE="$current"
        case "$current" in
            recovered)
                log "recovery incident 已恢复 recovery_id=${recovery_id}（${waited}s）"
                return 0
                ;;
            quarantined|isolation_confirmed)
                break
                ;;
        esac
        if [ $((waited % 60)) -lt "$RECOVERY_RESUME_POLL" ]; then
            log "  recovery_id=$recovery_id state=$current (${waited}s/${RECOVERY_RESUME_TIMEOUT}s)"
        fi
    done
    RIG_RECOVERY_STATE="$current"
    log "WARNING: Recovery Assignment 未恢复 rig=$RIG_ID recovery_id=$recovery_id state=$current waited=${waited}s"
    return 1
}

recover_quarantined_rig() {
    # 一次 preflight blocked（提 run 时 VM 正好 down）就会把 Rig 打进 quarantine，
    # register_rig 又显式保留该状态：不自愈的话，VM 重新健康、agent 也重连了，本机
    # 仍永久判 off 退回兼容路径，直到有人手工 recover，而脚本只打一行 off(...) 不提示。
    # 触发条件只有两个，都不依赖 preflight 自己的探针（vm_uds 探测另有缺陷，
    # 由 fix/device-session-probe-uds 处理）：本轮 Step 7 健康闸通过、且本轮
    # Agent 已经握过手（调用方用 last_seen 判，见 handshake_seen_this_bringup）。
    #
    # 按是否存在未关闭的 down-edge recovery incident 分两条路径：
    #   存在 1 个 → control recovery resume，由 Recovery Assignment 关闭 incident；
    #   不存在   → control rig recover，写 Recovery Receipt；
    #   存在多个或查询失败 → 不自愈，只给手工指令。
    local generation="$1"
    local probe_file="$RUNTIME_DIR/rig-recovery-probe.json" verdict="" incidents="" count=0
    RIG_RECOVERY_MODE="receipt"
    RIG_RECOVERY_ID=""
    RIG_RECOVERY_STATE=""
    RIG_RECOVERY_INCIDENTS=""
    verdict="$(write_rig_probe_result "$probe_file" 2>/dev/null || true)"
    if [ "$verdict" != "passed" ]; then
        log "WARNING: rig $RIG_ID 在 quarantine，但健康探针未通过（${verdict}），不自愈"
        return 1
    fi
    if ! incidents="$(control_active_recoveries)"; then
        RIG_RECOVERY_MODE="query-failed"
        log "WARNING: 查询 rig $RIG_ID 未关闭的 recovery incident 失败，不自愈"
        return 1
    fi
    if [ -n "$incidents" ]; then
        count="$(printf '%s\n' "$incidents" | wc -l | tr -d ' ')"
    fi
    if [ "$count" -gt 1 ]; then
        RIG_RECOVERY_MODE="multiple"
        RIG_RECOVERY_INCIDENTS="$(printf '%s\n' "$incidents" | sed 's/ /:/' | tr '\n' ' ' | sed 's/ $//')"
        log "WARNING: rig $RIG_ID 有 $count 个未关闭的 recovery incident（${RIG_RECOVERY_INCIDENTS}），不自动 resume"
        return 1
    fi
    if [ "$count" -eq 1 ]; then
        local recovery_id state
        read -r recovery_id state <<<"$incidents"
        resume_recovery_incident "$recovery_id" "$state"
        return
    fi
    log "rig $RIG_ID 在 quarantine，无未关闭 recovery incident，本次健康闸已通过 —— 写 Recovery Receipt 拉回 online"
    # 有 Lease 过期中断且未闭包的 Attempt 时，rig recover 先请求闭包恢复 Assignment（BL-076），
    # 由已握手的 Agent 以新 Fencing 执行；--wait-seconds 等它到终态，未通过时非零退出、保持 quarantine。
    if ! "$UV_BIN" run --group platform python3 -m autophone.cli control rig recover \
            "$RIG_ID" --rig-generation "$generation" \
            --actor "start_vphone.sh@$AUTOPHONE_HOST_ID" \
            --probe-result "$probe_file" --wait-seconds 120 >/dev/null; then
        log "WARNING: control rig recover 失败 rig=$RIG_ID generation=$generation"
        return 1
    fi
    return 0
}

rig_quarantine_hint() {
    # rig-quarantine 回退的手工指引，按本轮自愈走到的路径区分。
    local prefix="rig $RIG_ID 在 Control Plane 里是 quarantine 且本次未能自愈"
    local retry_hint=""
    if [ "$RIG_RECOVERY_MODE" = "assignment" ] && [ "$RIG_RECOVERY_STATE" = "recovered" ]; then
        printf '%s；recovery_id=%s 已 recovered 但 rig 仍是 quarantine。执行 %s 查看其余未关闭 incident' \
            "$prefix" "$RIG_RECOVERY_ID" "$(control_cli_manual "recovery list --rig-id $RIG_ID --active")"
        return 0
    fi
    case "$RIG_RECOVERY_MODE" in
        assignment)
            [ "$RIG_RECOVERY_STATE" = "quarantined" ] && retry_hint=" --retry"
            printf '%s；未关闭的 recovery incident 只能由 Recovery Assignment 关闭，不能跑 control rig recover（recovery_id=%s state=%s）。先看 %s，再查 %s；确认当前健康后执行 %s' \
                "$prefix" "$RIG_RECOVERY_ID" "${RIG_RECOVERY_STATE:-unknown}" "$RIG_AGENT_LOG" \
                "$(control_cli_manual "recovery get $RIG_RECOVERY_ID")" \
                "$(control_cli_manual "recovery resume $RIG_RECOVERY_ID$retry_hint")"
            ;;
        multiple)
            printf '%s；有多个未关闭的 recovery incident（%s），不能跑 control rig recover。逐个执行 %s，按状态 resume（quarantined 需 --retry）' \
                "$prefix" "$RIG_RECOVERY_INCIDENTS" "$(control_cli_manual "recovery get <recovery_id>")"
            ;;
        query-failed)
            printf '%s；recovery incident 查询失败。先执行 %s：有未关闭 incident 走 control recovery resume，没有才跑 control rig recover' \
                "$prefix" "$(control_cli_manual "recovery list --rig-id $RIG_ID --active")"
            ;;
        *)
            printf '%s；先看 %s，确认 %s 返回的 recoveries 为空后，据实执行 %s' \
                "$prefix" "$RIG_AGENT_LOG" \
                "$(control_cli_manual "recovery list --rig-id $RIG_ID --active")" \
                "$(control_cli_manual "rig recover $RIG_ID --rig-generation <generation> --actor <actor> --probe-result $RUNTIME_DIR/rig-recovery-probe.json")"
            ;;
    esac
}

verify_rig_registered() {
    # 唯一的通过判据是 control rig list 里本 rig status=online——不是「agent 进程在」，
    # 也不是「PID 文件对得上」：进程活着但 WebSocket 没连上时那两个都成立。
    local waited=0 row="" status="" generation="" last_seen=""
    local recovered=0 restarted=0 seen_quarantine=0
    while [ "$waited" -lt 20 ]; do
        row="$(control_rig_row || true)"
        status=""
        generation=""
        last_seen=0
        read -r status generation last_seen <<<"$row" || true
        [ -n "$last_seen" ] || last_seen=0
        case "$status" in
            online)
                log "rig registered online in Control Plane: $RIG_ID"
                return 0
                ;;
            quarantine)
                seen_quarantine=1
                # 自愈只试一次：成功就回到循环复核 online，失败或复核仍是 quarantine
                # 就按 rig-quarantine 回退——恢复必须留下回执，不重试掩盖失败。
                if [ "$recovered" -eq 1 ]; then
                    break
                fi
                if handshake_seen_this_bringup "$last_seen"; then
                    recovered=1
                    recover_quarantined_rig "$generation" || break
                    continue
                fi
                # 进程在但本轮没握过手 = WS 没连上。重启一次 Agent 逼它握手，
                # 而不是就这么把一台没人执行的 Rig 恢复成 online。
                if [ "$restarted" -eq 0 ]; then
                    restarted=1
                    log "rig $RIG_ID 在 quarantine 且本轮未见握手，重启 rig agent 以确认 WS 连通"
                    restart_rig_agent
                fi
                ;;
        esac
        sleep 1
        waited=$((waited + 1))
    done
    if [ "$seen_quarantine" -eq 1 ]; then
        control_plane_skip "rig-quarantine" "$(rig_quarantine_hint)"
        return 1
    fi
    control_plane_skip "rig-not-registered" \
        "rig 20s 内没在 Control Plane 上线 log=$RIG_AGENT_LOG"
    return 1
}

verify_device_session_channel() {
    # 交互通道（BL-100）：页面的屏幕预览与 device console 在控制面模式下走它。
    # **只 WARNING，不改引导结论**——通道不通时页面这两块不可用，但 run 派单与
    # Assignment 执行完全不受影响，把一台健康的 rig 判成引导失败没有道理。
    local waited=0
    while [ "$waited" -lt 10 ]; do
        if curl -sf "$CONTROL_HTTP/api/control/device-sessions" 2>/dev/null \
                | grep -q "\"device\""; then
            log "device session channel online: rig=$RIG_ID"
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    log "WARNING: 交互通道 10s 内没有设备注册上来 —— 页面屏幕预览与 device console 会不可用（log=${RIG_AGENT_LOG}）"
    return 0
}

bootstrap_control_plane() {
    if [ -n "${AUTOPHONE_RECOVERY_ASSIGNMENT_ID:-}" ]; then
        # Recovery Assignment 已持有 Lease；保留控制面配置及当前 Rig Agent。
        log "Recovery Assignment: 保留 Control Plane 与 Rig Agent"
        CONTROL_PLANE_STATUS="retained(recovery-assignment)"
        return 0
    fi
    if [ "${SKIP_CONTROL_PLANE:-0}" = "1" ]; then
        # 也删 env 文件：否则上一次成功引导写下的文件会让 serve 继续判 on，
        # 「本次跳过」就只对脚本自己成立。
        discard_control_plane_env
        log "SKIP_CONTROL_PLANE=1 — 跳过 Control Plane 引导，本次走单 VM 兼容路径"
        CONTROL_PLANE_STATUS="off(skipped)"
        return 0
    fi
    # DeviceTarget 要求 expected_usb_udid 非空，不能编一个：它是 rig 身份的一部分，
    # 编出来的值会在控制面里注册成一台不存在的设备。
    if [ -z "$EXPECTED_USB_UDID" ]; then
        control_plane_skip "no-usb-udid" \
            "expected USB UDID 为空，无法构造 DeviceTarget"
        return 0
    fi
    ensure_postgres_running || return 0
    ensure_control_database || return 0

    AUTOPHONE_HOST_ID="$(resolve_host_id)"
    export AUTOPHONE_HOST_ID
    printf '%s\n' "$AUTOPHONE_HOST_ID" > "$CONTROL_HOST_ID_FILE"
    export AUTOPHONE_DATABASE_URL="$CONTROL_DSN"
    export AUTOPHONE_CONTROL_WS="$CONTROL_WS"

    # cli_dir / log_dir 显式传参而不是靠 export：这两个在 Step 1-7 里是普通 shell
    # 变量，导出它们会改变前七步所有 python 子进程看到的环境。
    if ! "$UV_BIN" run --group platform python3 -m autophone.rig.emit_target \
            --out "$CONTROL_TARGET_FILE" \
            --cli-dir "$VPHONE_CLI_DIR" \
            --log-dir "$VPHONE_LOG_DIR" >/dev/null; then
        control_plane_skip "device-target-failed" \
            "DeviceTarget 生成失败 target=$CONTROL_TARGET_FILE"
        return 0
    fi

    ensure_control_python || return 0
    start_control_http || return 0
    # 本轮引导的起点：quarantine 自愈要靠它判 rigs.last_seen 是不是本轮刷新的。
    CONTROL_BOOTSTRAP_STARTED_AT="$(date +%s)"
    start_rig_agent || return 0
    verify_rig_registered || return 0
    verify_device_session_channel
    write_control_plane_env
    CONTROL_PLANE_STATUS="on($CONTROL_DSN)"
    return 0
}

bootstrap_control_plane

# ─── Step 9: ScreenLock 隔离标记（BL-223）──────────────────────────────────
# 标记 = StageGuard quiesce 未验证（写标记的进程可能留有未确认释放的 Frida 会话或挂起进程）。
# Recovery Receipt 只证明健康闸通过，不证明这些资源已释放，所以这里只提示、不清除；
# 解除走 `device screenlock release-quarantine`（前置核对、设备探针、人工确认、归档与回执）。
SCREEN_LOCK_STATUS="unchecked"
check_screen_lock_quarantine() {
    local out=""
    if ! out="$("${UV_BIN}" run --group platform python3 -m autophone.cli \
            device screenlock status --device "${RIG_ID}" 2>/dev/null)"; then
        SCREEN_LOCK_STATUS="unknown"
        log "WARNING: ScreenLock 隔离标记状态读取失败（device=${RIG_ID}），未核对"
        return 0
    fi
    # 用 case 而不是 `printf | grep -q`：pipefail 下 grep 提前退出会让管道返回 141。
    case "$out" in
        *'"quarantined": true'*)
        SCREEN_LOCK_STATUS="quarantined"
        log "WARNING: ScreenLock 隔离标记存在（device=${RIG_ID}，reason=device_quiesce_unverified），Run 取锁会被拒，本脚本不自动清除。详情：uv run --group platform python3 -m autophone.cli device screenlock status --device ${RIG_ID}；核对设备静默后解除：uv run --group platform python3 -m autophone.cli device screenlock release-quarantine --device ${RIG_ID} --actor <你> --reason <原因> --confirm-quiesced（不带 --confirm-quiesced 只核对不解除）"
        return 0
        ;;
    esac
    SCREEN_LOCK_STATUS="clear"
}
check_screen_lock_quarantine

# ─── Done ───────────────────────────────────────────────────────────────────

log "Full stack ready. rig=$RIG_ID sock=$SOCK ssh=$SSH_PORT frida=$FRIDA_PORT->$FRIDA_GUEST_PORT udid=${EXPECTED_USB_UDID:-unbound} control_plane=$CONTROL_PLANE_STATUS screen_lock=$SCREEN_LOCK_STATUS"
exit 0
