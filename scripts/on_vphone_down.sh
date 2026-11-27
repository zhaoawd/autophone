#!/bin/bash
# 掉线钩子仅持久化观测、采集诊断并提交恢复请求；恢复由持 Lease 的 Assignment 执行。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOPHONE_DIR="$(dirname "$SCRIPT_DIR")"
_first_dir() { for d in "$@"; do [ -d "$d" ] && { echo "$d"; return; }; done; echo "$1"; }
VPHONE_CLI_DIR="${VPHONE_CLI_DIR:-$(_first_dir "$AUTOPHONE_DIR/../vphone-cli" "$HOME/Documents/github/vphone-cli" "$HOME/github/vphone-cli")}"
VM_DIR="${VM_DIR:-vm}"
case "$VM_DIR" in /*) ;; *) VM_DIR="$VPHONE_CLI_DIR/$VM_DIR" ;; esac
VPHONE_LOG_DIR="${VPHONE_LOG_DIR:-$VM_DIR/logs}"
RIG_ID="${AUTOPHONE_RIG_ID:-${AP_DEVICE:-$(basename "$VM_DIR")}}"
export VPHONE_CLI_DIR VM_DIR VPHONE_LOG_DIR
export AUTOPHONE_RIG_ID="$RIG_ID"
export AUTOPHONE_RIG_RUNTIME_DIR="${AUTOPHONE_RIG_RUNTIME_DIR:-/tmp/autophone-rigs/$RIG_ID}"
export AUTOPHONE_RIG_LOG_DIR="${AUTOPHONE_RIG_LOG_DIR:-$VPHONE_LOG_DIR}"
# The health daemon may inherit a one-shot startup stage log.  A down edge must
# keep its visibility events in the managed recovery stream for this Rig.
unset AUTOPHONE_STAGE_LOG
PYTHON="${AUTOPHONE_CONTROL_PY:-$AUTOPHONE_DIR/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then PYTHON=python3; fi
cd "$AUTOPHONE_DIR"
exec "$PYTHON" -m autophone.recovery_observation
