#!/bin/bash
# collect_vphone_crash.sh — vphone VM 挂掉后一键采集诊断包。
#
# start_vphone.sh 拉起的 VM 本体是 macOS Virtualization.framework 的子进程
# `com.apple.Virtualization.VirtualMachine`（不是 vphone-cli 主进程本身）。它崩了
# 之后 UDS/vsock 立刻 Connection refused、health 翻 down，但**崩因不在 vphone-cli 的
# start.log 里**——只在系统 unified log 里，而 unified log 是环形缓冲、不主动落盘，
# 拖久了就被冲掉。所以挂掉后要尽快跑这个脚本把窗口内的证据固化下来。
#
# 注意：AMPDeviceDiscoveryAgent 是已确认的一条 62078 触发源，但禁用 AMP 只是一项
# 缓解；根因尚未闭合。采集时必须保留精确的 IOUSB 异常类型和 usbmux ConnectByPort
# 记录，不能把所有 IOUSBHostCIException 都写成 CommandTimeout。
#
# 用法：
#   scripts/collect_vphone_crash.sh            采集最近 30 分钟
#   scripts/collect_vphone_crash.sh 120        采集最近 120 分钟
#   VPHONE_CRASH_LOG_START="2026-09-23 10:00:00+0800" \
#   VPHONE_CRASH_LOG_END="2026-09-23 11:00:01+0800" scripts/collect_vphone_crash.sh
#                                              采集固定起止窗口（两者须同时设置，
#                                              格式同 log show --start/--end）
#
# 产物：/tmp/vphone_crash_<时间戳>/ 下若干 .log + 一个 SUMMARY.txt

set -uo pipefail

MINUTES="${1:-30}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOPHONE_DIR="$(dirname "$SCRIPT_DIR")"
AUTOPHONE_STATE_ROOT="${AUTOPHONE_STATE_DIR:-$AUTOPHONE_DIR}"
if [ -d "$AUTOPHONE_STATE_ROOT" ]; then
    AUTOPHONE_STATE_ROOT="$(cd "$AUTOPHONE_STATE_ROOT" && pwd -P)"
fi
VPHONE_CLI_DIR="${VPHONE_CLI_DIR:-$HOME/github/vphone-cli}"
[ -d "$VPHONE_CLI_DIR" ] || VPHONE_CLI_DIR="$HOME/Documents/github/vphone-cli"
if [ -n "${VM_DIR:-}" ]; then
    case "$VM_DIR" in /*) ;; *) VM_DIR="$VPHONE_CLI_DIR/$VM_DIR" ;; esac
else
    VM_DIR="$VPHONE_CLI_DIR/vm"
fi
RIG_ID="${AUTOPHONE_RIG_ID:-${AP_DEVICE:-$(basename "$VM_DIR")}}"
RUNTIME_DIR="${AUTOPHONE_RIG_RUNTIME_DIR:-/tmp/autophone-rigs/$RIG_ID}"
VPHONE_LOG_DIR="${VPHONE_LOG_DIR:-$VM_DIR/logs}"
SOCK="${VPHONE_SOCK:-$VM_DIR/vphone.sock}"
HEALTH_LOG="${VPHONE_HEALTH_LOG:-$RUNTIME_DIR/health.log}"
HEALTH_JSON="${VPHONE_HEALTH_STATE_FILE:-$RUNTIME_DIR/health.json}"
VPHONE_PID_FILE="${VPHONE_PID_FILE:-$RUNTIME_DIR/vphone.pid}"
IPROXY_PID_FILE="${VPHONE_IPROXY_PID_FILE:-$RUNTIME_DIR/iproxy.pid}"
FRIDA_TUNNEL_PID_FILE="${VPHONE_FRIDA_TUNNEL_PID_FILE:-$RUNTIME_DIR/frida-tunnel.pid}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
DIAGNOSIS_HELPER="$AUTOPHONE_DIR/tools/phase1/vphone_crash_diagnosis.py"
USB_EXEC_TRACE_LOG="${VPHONE_USB_EXEC_TRACE_LOG:-/tmp/vphone_usbmux_xcode_exec.log}"
USB_EXEC_TRACE_PID_FILE="${VPHONE_USB_EXEC_TRACE_PID_FILE:-/tmp/vphone_usbmux_xcode_exec.pid}"

OUT_ROOT="${VPHONE_CRASH_OUTPUT_ROOT:-$RUNTIME_DIR/diagnostics}"
OUT="$OUT_ROOT/vphone_crash_$(date '+%Y%m%dT%H%M%S')"
mkdir -p "$OUT"
LOG="/usr/bin/log"

# unified log 时间范围：默认 --last N 分钟；固定窗口采集可传入起止时间，
# 避免 --last 按分钟取整后把相邻窗口的事件重复计入。
LOG_START="${VPHONE_CRASH_LOG_START:-}"
LOG_END="${VPHONE_CRASH_LOG_END:-}"
if [ -n "$LOG_START" ] && [ -n "$LOG_END" ]; then
    LOG_RANGE=(--start "$LOG_START" --end "$LOG_END")
    WINDOW_TEXT="${LOG_START} 至 ${LOG_END}"
elif [ -n "$LOG_START" ] || [ -n "$LOG_END" ]; then
    echo "[collect] FATAL: VPHONE_CRASH_LOG_START 与 VPHONE_CRASH_LOG_END 必须同时设置" >&2
    exit 2
else
    LOG_RANGE=(--last "${MINUTES}m")
    WINDOW_TEXT="最近 ${MINUTES} 分钟"
fi

echo "[collect] 采集窗口：${WINDOW_TEXT} → $OUT"

# ── 1. VM 子进程的 fatal / USB 异常（最可能的崩因）──────────────────────────
# `F` 级别 + IOUSBHostCIException 是观测到的确定性崩溃签名。
$LOG show "${LOG_RANGE[@]}" --style compact \
    --predicate 'eventMessage CONTAINS "IOUSBHostCIException" OR eventMessage CONTAINS "Fatal error" OR (process == "com.apple.Virtualization.VirtualMachine" AND messageType == 16)' \
    2>/dev/null > "$OUT/vm_fatal.log"

# ── 2. VM 子进程完整 unified log（崩溃前后上下文，含 USB mux 抖动）────────────
$LOG show "${LOG_RANGE[@]}" --style compact \
    --predicate 'process == "com.apple.Virtualization.VirtualMachine"' \
    2>/dev/null > "$OUT/vm_full.log"

# ── 3. 系统侧对 VM 的处置：内存压力杀 / jetsam / 崩溃上报 ─────────────────────
$LOG show "${LOG_RANGE[@]}" --style compact \
    --predicate 'eventMessage CONTAINS[c] "jetsam" OR eventMessage CONTAINS[c] "memorystatus" OR (process == "spindump" OR process == "ReportCrash" OR process == "SubmitDiagInfo")' \
    2>/dev/null | grep -iE "vphone|Virtualization|VirtualMachine|jetsam|memorystatus" > "$OUT/system_kill.log"

# ── 3b. 宿主进程（vphone-cli / vphone-vm）自身的退出记录 ─────────────────────
# 宿主进程退出时 VM 随之销毁，第 1-3 节没有任何异常签名（2026-09-30 两台 VM 均属此类）。
# 这里固化宿主进程的 AppKit terminate: 流程与其之前的界面动作、runningboardd 的
# termination reported、launchd 的 service inactive、launchservicesd 的 QUITTING 以及
# 崩溃上报行；由 tools.phase1.vphone_host_exit 判定「应用内退出 / 无 terminate: 的退出 / 崩溃」。
$LOG show "${LOG_RANGE[@]}" --style compact \
    --predicate '((process == "vphone-cli" OR process == "vphone-vm") AND (eventMessage CONTAINS[c] "terminat" OR eventMessage CONTAINS "sendAction" OR eventMessage CONTAINS "trackMouse")) OR (process == "runningboardd" AND eventMessage CONTAINS "termination reported" AND eventMessage CONTAINS "vphone-") OR (process == "launchd" AND eventMessage CONTAINS "vphone-") OR (process == "launchservicesd" AND eventMessage CONTAINS "QUITTING") OR ((process == "ReportCrash" OR process == "osanalyticsd") AND eventMessage CONTAINS "vphone-")' \
    2>/dev/null > "$OUT/host_exit.log"

# ── 4. vphone-cli 主进程自己的启动日志 ───────────────────────────────────────
[ -f "$VPHONE_LOG_DIR/vphone-cli-start.log" ] && \
    tail -n 400 "$VPHONE_LOG_DIR/vphone-cli-start.log" > "$OUT/vphone-cli-start.tail.log"

# ── 5. health 守护的状态流水（healthy→down 的翻转时刻）───────────────────────
if [ -f "$HEALTH_LOG" ]; then
    tail -n 300 "$HEALTH_LOG" > "$OUT/health.tail.log"
    grep -nE "overall=(down|unknown)|Connection refused|ProtocolError" "$HEALTH_LOG" \
        | tail -40 > "$OUT/health_transitions.log"
fi
cp "$HEALTH_JSON" "$OUT/" 2>/dev/null

# ── 6. 崩溃 / hang / 资源报告落盘文件（如果系统生成了）────────────────────────
{
    echo "== ~/Library/Logs/DiagnosticReports =="
    ls -lt "$HOME/Library/Logs/DiagnosticReports/" 2>/dev/null | grep -iE "virtual|vphone|jetsam|hang" | head
    echo "== /Library/Logs/DiagnosticReports (需 sudo 读内容) =="
    ls -lt /Library/Logs/DiagnosticReports/ 2>/dev/null | grep -iE "virtual|vphone|jetsam|hang" | head
} > "$OUT/crash_reports_index.log" 2>&1

# ── 7. 进程现状快照 ──────────────────────────────────────────────────────────
{
    echo "== vphone-cli / vphone-vm / VM / amfidont / 隧道进程 =="
    pgrep -fl "vphone-cli|vphone-vm|Virtualization.VirtualMachine|amfidont|iproxy|-L [0-9]*:127.0.0.1:[0-9]*|vsock_health" || echo "(无匹配进程)"
    echo "rig_id=$RIG_ID"
    echo "target_vm_dir=$VM_DIR"
    echo "target_socket=$SOCK"
    echo "vphone_pid=$(cat "$VPHONE_PID_FILE" 2>/dev/null || true)"
    echo "iproxy_pid=$(cat "$IPROXY_PID_FILE" 2>/dev/null || true)"
    echo "frida_tunnel_pid=$(cat "$FRIDA_TUNNEL_PID_FILE" 2>/dev/null || true)"
    echo
    echo "== usbmuxd 客户端快照（仅能捕获 down 边沿尚存活的短命客户端） =="
    ps -axo pid,ppid,lstart,etime,command 2>/dev/null \
        | grep -E '[u]sbmuxd|[X]code|[Aa]MPDevice|[Mm]obileDevice' \
        || echo "(无匹配进程)"
    echo
    echo "== vphone.sock 存在？ =="
    ls -l "$SOCK" 2>&1
} > "$OUT/processes.log"

predicted_udid_of() {
    # 与 start_vphone.sh 的 read_predicted_udid 同一解析：只取 UDID= 行。
    sed -nE 's/^UDID[=:][[:space:]]*//p' "$1/udid-prediction.txt" 2>/dev/null | head -1 \
        | grep . || echo unknown
}

# ── 7c. 多 VM 归属映射 ────────────────────────────────────────────────────────
# 第 1/2 节按进程名 com.apple.Virtualization.VirtualMachine 抓的是宿主级证据，同机两台
# VM 的行混在一起，只能靠日志行里的 pid 区分。这里把「宿主进程 → VZ helper 子
# 进程 → VM 目录 → 预期 UDID」的映射固化下来，并标出本次采集的目标 VM；宿主级日志不收窄。
# 宿主进程启动后可改名为 vphone-vm，枚举与 start_vphone.sh 共用 tools.vphone_process：
# 按真实 argv 识别两种进程名，--config 规范化后再与目标 VM 目录比较（路径可能含 `..`）。
{
    echo "== 采集目标 =="
    echo "target_rig_id=$RIG_ID"
    echo "target_vm_dir=$VM_DIR"
    echo "target_expected_udid=${AUTOPHONE_EXPECTED_USB_UDID:-$(predicted_udid_of "$VM_DIR")}"
    echo "target_vphone_pid=$(cat "$VPHONE_PID_FILE" 2>/dev/null || true)"
    echo "multi_vm_mode=${AUTOPHONE_EXPERIMENTAL_MULTI_VM:-0}"
    echo
    echo "== 本机全部宿主进程 vphone-cli / vphone-vm（pid → 进程名 → VM 目录 → VZ helper 子进程 → 预期 UDID）=="
    host_rows="$(PYTHONPATH="$AUTOPHONE_DIR${PYTHONPATH:+:$PYTHONPATH}" \
        "$PYTHON_BIN" -m tools.vphone_process --list --target-vm-dir "$VM_DIR")"
    host_rc=$?
    if [ "$host_rc" -ne 0 ]; then
        echo "(宿主进程枚举失败 rc=$host_rc)"
    elif [ -z "$host_rows" ]; then
        echo "(无 vphone-cli / vphone-vm 进程)"
    else
        while IFS=$'\t' read -r cli_pid process_name vm_dir_of is_target; do
            [ -n "$cli_pid" ] || continue
            # VZ helper 是 launchd 起的 XPC 服务（ppid=1），不能按父进程找；按持有该 VM
            # Disk.img 的进程定位。
            helper_pids="$(lsof -t "$vm_dir_of/Disk.img" 2>/dev/null | tr '\n' ' ')"
            udid_of="$(predicted_udid_of "$vm_dir_of")"
            marker=""
            [ "$is_target" = 1 ] && marker="  <== target"
            echo "vphone_cli_pid=$cli_pid process_name=$process_name vm_dir=$vm_dir_of vz_helper_pids=[${helper_pids% }] expected_udid=$udid_of$marker"
        done <<< "$host_rows"
    fi
    echo
    echo "== VZ helper 进程（pid ppid 启动时间）=="
    ps -axo pid,ppid,lstart,etime,command 2>/dev/null | grep -E '[V]irtualization.VirtualMachine' \
        || echo "(无匹配进程)"
    echo
    echo "== usbmuxd 可见设备（idevice_id -l）=="
    idevice_id -l 2>/dev/null || echo "(idevice_id 不可用或无设备)"
} > "$OUT/vm_attribution.log" 2>&1

# ── 7b. 短命 Xcode 客户端的 exec 证据 ─────────────────────────────────────────
# 优先 DTrace exec-success（PID、PPID、psargs）；被 SIP 拒绝时是带父链的 0.2s ps
# 采样后备，可能漏掉立即退出的 Xcode[pid]。整份日志只过滤 Xcode，保留供跨日志对齐。
{
    echo "== Xcode/usbmux exec trace =="
    echo "log=$USB_EXEC_TRACE_LOG"
    echo "pid_file=$USB_EXEC_TRACE_PID_FILE"
    if [ -f "$USB_EXEC_TRACE_PID_FILE" ]; then
        echo "monitor_pid=$(cat "$USB_EXEC_TRACE_PID_FILE" 2>/dev/null || true)"
    else
        echo "monitor_pid=(not started)"
    fi
} > "$OUT/usbmux_exec_trace_status.log"
if [ -f "$USB_EXEC_TRACE_LOG" ]; then
    cp "$USB_EXEC_TRACE_LOG" "$OUT/usbmux_xcode_exec.log" 2>/dev/null \
        || echo "failed to copy $USB_EXEC_TRACE_LOG" >> "$OUT/usbmux_exec_trace_status.log"
else
    echo "trace log absent; start with VPHONE_USB_EXEC_TRACE=1 scripts/start_vphone.sh" \
        >> "$OUT/usbmux_exec_trace_status.log"
fi

# ── 8. 宿主内存压力（VZ 在宿主吃紧时虚拟 USB 命令易超时）──────────────────────
# 崩溃是"当下"事件，但 vm_stat / 内存压力是"现在"的采样：脚本挂掉后越快跑，越能
# 反映崩溃时的宿主水位。同时抓 unified log 里窗口内的内存压力/jetsam 事件做时间对齐。
{
    echo "== 现在的内存压力（memory_pressure）=="
    /usr/bin/memory_pressure 2>/dev/null | grep -iE "percentage|free|pressure" || echo "(取不到)"
    echo
    echo "== vm_stat 快照 =="
    /usr/bin/vm_stat 2>/dev/null
    echo
    echo "== 吃内存 top 10（RSS）=="
    ps -axo rss,pid,comm -m 2>/dev/null | head -11
    echo
    echo "== 窗口内系统内存压力/jetsam 事件（对齐崩溃时刻）=="
    $LOG show "${LOG_RANGE[@]}" --style compact \
        --predicate 'eventMessage CONTAINS[c] "memory pressure" OR eventMessage CONTAINS[c] "memorystatus" OR eventMessage CONTAINS[c] "jetsam"' \
        2>/dev/null | tail -30 || echo "(无)"
} > "$OUT/host_memory.log" 2>&1

# ── 9. 崩溃前的重操作与 usbmux 关联窗 ───────────────────────────────────────
# 必须保留完整窗口：固定 tail -40 会在 Xcode/Bonjour 风暴时截掉最早的
# USBMuxConnectByPort，从而漏掉 32498 (f2,7e) -> lockdownd:62078 的关联。
{
    echo "== autophone run.log 里最近的装包/抓取/frida 动作（跨所有 run）=="
    # 装包走 scp+ipa_install，抓取走 frida spawn_gate —— 都是待证的重活动来源。
    grep -rhiE --include='run.log' \
        "ipa_install|scp|spawn_gate|frida|capture|package install" \
        "$AUTOPHONE_STATE_ROOT/runs" \
        2>/dev/null | tail -20 || echo "(无 run.log 命中)"
    echo
    echo "== 完整 usbmux 关联窗 =="
    echo "见 $OUT/usbmux_window.log（不做 tail 截断）"
} > "$OUT/heavy_ops.log" 2>&1

$LOG show "${LOG_RANGE[@]}" --style compact \
    --predicate 'process == "usbmuxd" OR eventMessage CONTAINS "AppleUSBDeviceMux" OR eventMessage CONTAINS "IOUSBHost"' \
    2>/dev/null > "$OUT/usbmux_window.log"

# ── 10. 结构化 USB 诊断 ───────────────────────────────────────────────────────
# 分类精确的 IOUSBHostCIExceptionType*，解析 usbmux 原始端口，并记录宿主/VZ/VM
# 指纹。跨机器比较时，macOS build 和 VM config 不能只靠人工备注。
if [ -f "$DIAGNOSIS_HELPER" ]; then
    PYTHONPATH="$AUTOPHONE_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" -m tools.phase1.vphone_crash_diagnosis \
        --vm-fatal "$OUT/vm_fatal.log" \
        --usbmux "$OUT/usbmux_window.log" \
        --vphone-cli-dir "$VPHONE_CLI_DIR" \
        --vm-dir "$VM_DIR" \
        --json "$OUT/usb_diagnosis.json" \
        --timeline "$OUT/usb_timeline.log" \
        --summary "$OUT/usb_summary.txt"
    DIAGNOSIS_RC=$?
    if [ "$DIAGNOSIS_RC" -ne 0 ]; then
        echo "USB diagnosis helper failed (rc=$DIAGNOSIS_RC)" > "$OUT/usb_summary.txt"
    fi
else
    echo "USB diagnosis helper missing: $DIAGNOSIS_HELPER" > "$OUT/usb_summary.txt"
fi

# ── 10b. 宿主进程退出判定 ─────────────────────────────────────────────────────
# 目标 pid 取 PID 文件：down 边沿时它仍是刚退出的那个宿主进程。
HOST_EXIT_HELPER="$AUTOPHONE_DIR/tools/phase1/vphone_host_exit.py"
if [ -f "$HOST_EXIT_HELPER" ]; then
    PYTHONPATH="$AUTOPHONE_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" -m tools.phase1.vphone_host_exit \
        --log "$OUT/host_exit.log" \
        --target-pid "$(cat "$VPHONE_PID_FILE" 2>/dev/null || true)" \
        --json "$OUT/host_exit.json" \
        --summary "$OUT/host_exit_summary.txt"
    HOST_EXIT_RC=$?
    if [ "$HOST_EXIT_RC" -ne 0 ]; then
        echo "host exit helper failed (rc=$HOST_EXIT_RC)" > "$OUT/host_exit_summary.txt"
    fi
else
    echo "host exit helper missing: $HOST_EXIT_HELPER" > "$OUT/host_exit_summary.txt"
fi

# ── SUMMARY：一眼看崩因 ──────────────────────────────────────────────────────
{
    echo "vphone VM 崩溃诊断摘要  ($(date))"
    echo "采集窗口：${WINDOW_TEXT}"
    echo
    echo "== USB 精确分型与跨机元数据 =="
    cat "$OUT/usb_summary.txt" 2>/dev/null || echo "(USB 结构化诊断未生成，看 vm_fatal.log)"
    echo
    echo "== 宿主进程（vphone-cli / vphone-vm）退出记录 =="
    cat "$OUT/host_exit_summary.txt" 2>/dev/null || echo "(宿主进程退出判定未生成，看 host_exit.log)"
    echo
    echo "VM 归属（宿主级日志含同机全部 VM，按 pid 对照 vm_attribution.log）："
    grep -E "^vphone_cli_pid=|^\(宿主进程枚举失败" "$OUT/vm_attribution.log" 2>/dev/null || echo "  (无 vphone-cli / vphone-vm 进程)"
    echo
    echo "health 最后一次 healthy→down 翻转："
    grep -m1 -E "overall=(down|unknown)|ProtocolError" "$OUT/health_transitions.log" 2>/dev/null || echo "  (无)"
    echo
    echo "宿主内存水位（采集时刻，越快跑越接近崩溃态）："
    grep -iE "System-wide memory free percentage|percentage" "$OUT/host_memory.log" 2>/dev/null | head -1 || echo "  (取不到，看 host_memory.log)"
    JETSAM_N=$(grep -ciE "jetsam|memorystatus" "$OUT/host_memory.log" 2>/dev/null || echo 0)
    echo "窗口内内存压力/jetsam 事件条数：$JETSAM_N"
    echo
    echo "崩溃前的重操作（待与 usb_timeline.log 对齐，不作为根因判定）："
    grep -m3 -iE "ipa_install|scp|spawn_gate|frida|capture" "$OUT/heavy_ops.log" 2>/dev/null || echo "  (无 run.log 命中)"
    echo
    if grep -q '^HOST_EXIT_TARGET: pid=[0-9]* verdict=app_terminate' "$OUT/host_exit_summary.txt" 2>/dev/null; then
        echo "→ 目标 VM 的宿主进程经应用内退出流程结束；VM 随宿主进程销毁，USB 与内存证据不用于本次归因。"
    fi
    USB_TOTAL="$(sed -n 's/^USB_EXCEPTION_TOTAL: //p' "$OUT/usb_summary.txt" 2>/dev/null | head -1)"
    if [[ "$USB_TOTAL" =~ ^[1-9][0-9]*$ ]]; then
        echo "→ 观察到虚拟 USB 主控异常；AMP 是已抑制的已知来源，第二个 62078 客户端仍待识别。"
        echo "  看 usb_timeline.log 对齐精确异常类型与 ConnectByPort；看 usb_diagnosis.json 做跨机器聚合。"
    elif grep -q '^62078 候选 ConnectByPort：[1-9]' "$OUT/usb_summary.txt" 2>/dev/null; then
        echo "→ 本采集窗口观察到 62078 ConnectByPort，但未观察到带类型的 USB 异常；不能据此归因。"
    fi
} > "$OUT/SUMMARY.txt"

echo "[collect] 完成。先看："
echo "  cat $OUT/SUMMARY.txt"
echo "所有文件："
ls -1 "$OUT"
