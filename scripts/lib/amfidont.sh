#!/bin/bash
# amfidont.sh — amfidont 的前置授权探测与启动（被 scripts/start_vphone.sh source）。
#
# 从 start_vphone.sh Step 1 抽出来，目的有两个：
#   1. 补上 docs/platform-vm-management-task-console.md §6.3 的三步探测，把
#      「非交互环境下拿不到管理员授权」映射成稳定错误码 authorization_required，
#      而不是一句含义不明的英文 die 文本。
#   2. 让这段逻辑可以被测试单独 source（tests/test_recovery_events.py 用 PATH
#      前置 stub 真跑一次），不需要活 VM，也不需要真的 sudo。
#
# 依赖调用方提供的符号：log / die / stage_error（stage_error 来自
# scripts/lib/stage_events.sh）、变量 AMFIDONT_HELPER 与 VPHONE_CLI_DIR。
# log / die 只在调用方没定义时才用下面的兜底实现，因此 start_vphone.sh 里的
# 文本输出格式不受影响。

_AMFIDONT_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! declare -F stage_error >/dev/null 2>&1; then
    # shellcheck source=scripts/lib/stage_events.sh
    . "$_AMFIDONT_LIB_DIR/stage_events.sh"
fi

if ! declare -F log >/dev/null 2>&1; then
    log() { echo "[start_vphone] $(date '+%H:%M:%S') $*"; }
fi
if ! declare -F die >/dev/null 2>&1; then
    die() { log "FATAL: $*"; exit 1; }
fi

AMFIDONT_AUTHORIZATION_HINT='amfidont 未运行、vphone-cli 的 amfidont helper 不可执行，且当前环境没有可用的免密 sudo（sudo -n true 不通过）。请在主机终端手工执行一次：cd <vphone-cli> && zsh scripts/start_amfidont_for_vphone.sh，或先在终端里 sudo -v 缓存授权后重跑 scripts/start_vphone.sh。本流程不收集管理员密码。'

# 按 §6.3 三步探测后启动 amfidont。调用方（Step 1）已确认 amfidont 未在跑。
start_amfidont() {
    if [ -x "$AMFIDONT_HELPER" ]; then
        log "Starting amfidont via vphone-cli helper (path + CDHash allowlist)..."
        (cd "$VPHONE_CLI_DIR" && zsh "$AMFIDONT_HELPER") || die "amfidont helper failed: $AMFIDONT_HELPER"
    else
        # 第三步：既没有在跑的 amfidont，也没有 helper。此时只剩 sudo 一条路；
        # 非交互环境（含自动恢复钩子）拿不到授权，fail-closed 报稳定错误码。
        if ! sudo -n true 2>/dev/null; then
            stage_error host authorization_required "$AMFIDONT_AUTHORIZATION_HINT"
            die "authorization_required: $AMFIDONT_AUTHORIZATION_HINT"
        fi
        log "Starting amfidont (requires sudo)..."
        sudo xcrun amfidont daemon --path "$VPHONE_CLI_DIR" --spoof-apple &
        sleep 2
    fi
    pgrep -f "amfidont" >/dev/null || die "amfidont failed to start"
    log "amfidont started"
}

# Step 1 的完整语义：已在跑就跳过，否则走 start_amfidont。
# 探测放宽到任意 amfidont 进程：实际可能以 `python -m amfidont ...` 跑（非
# `xcrun amfidont daemon`），按窄模式探测会误判没起、再 sudo 重拉。
ensure_amfidont() {
    if ! pgrep -f "amfidont" >/dev/null 2>&1; then
        start_amfidont
    else
        log "amfidont already running"
    fi
}
