#!/bin/bash
# stage_events.sh — 结构化阶段/恢复事件发射器（被 scripts/start_vphone.sh 与
# scripts/on_vphone_down.sh source）。
#
# 这是与现有 `log "..."` 文本输出**并行**的第二条通道，不替换、不改动文本输出。
# 目标文件由 AUTOPHONE_STAGE_LOG 指定；未设置（或为空）时全部发射函数是空操作，
# 脚本行为与本文件引入前完全一致。
#
# 格式：JSON Lines（一行一个 JSON 对象），字段顺序固定：
#   {"event":...,<调用方给的键值对>,"ts":...,"rig_id":...,"vm_dir":...}
#   ts 是 UTC ISO 8601（`YYYY-MM-DDTHH:MM:SSZ`），不是 epoch。
#
# 事件类型：
#   阶段通道（start_vphone.sh）：stage_start / stage_end / stage_error
#   恢复事件（on_vphone_down.sh）：down_detected / guard_decision /
#                                  recovery_started / recovery_succeeded /
#                                  recovery_failed
#
# 阶段名固定八个，与 docs/platform-vm-management-task-console.md §6.1 的表一一对应：
#   host / boot / uds / network / ssh / vphoned / frida / health
#
# fail-open：写入失败（目录不可建、磁盘满、文件不可写）只丢事件，不影响调用方的
# 退出码，也不产生额外 stdout/stderr 输出。阶段通道是观测手段，不是启动前提。

# 当前进入但尚未结束的阶段名。stage_start 置位，stage_end / stage_error 清空。
# 调用方的 die 用它决定 stage_error 挂在哪个阶段上。
AUTOPHONE_CURRENT_STAGE="${AUTOPHONE_CURRENT_STAGE:-}"

# JSON 字符串转义。覆盖反斜杠、双引号、换行、回车、制表符；其余 ASCII 控制字符
# 不做转义（本仓库的阶段名、错误码与消息里没有出现过，若出现会产出不合法 JSON —
# 这属于已知限制，不是静默截断）。
_stage_json_escape() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\r'/\\r}"
    s="${s//$'\t'/\\t}"
    printf '%s' "$s"
}

# 发射一条事件。
#   $1                事件名
#   $2..              载荷，形如 `key=value`（字符串）或 `key:=value`（原样写入 JSON，
#                     仅当 value 是十进制整数时生效，否则退回字符串，避免产出不合法 JSON）
autophone_emit_event() {
    local target="${AUTOPHONE_STAGE_LOG:-}"
    [ -n "$target" ] || return 0

    local event="$1"
    shift

    local json kv key value dir
    json="{\"event\":\"$(_stage_json_escape "$event")\""
    for kv in "$@"; do
        case "$kv" in
            *:=*)
                key="${kv%%:=*}"
                value="${kv#*:=}"
                case "$value" in
                    ''|*[!0-9-]*|-|*-*-*)
                        json="$json,\"$(_stage_json_escape "$key")\":\"$(_stage_json_escape "$value")\""
                        ;;
                    *)
                        json="$json,\"$(_stage_json_escape "$key")\":$value"
                        ;;
                esac
                ;;
            *)
                key="${kv%%=*}"
                value="${kv#*=}"
                json="$json,\"$(_stage_json_escape "$key")\":\"$(_stage_json_escape "$value")\""
                ;;
        esac
    done
    json="$json,\"ts\":\"$(date -u '+%Y-%m-%dT%H:%M:%SZ')\""
    json="$json,\"rig_id\":\"$(_stage_json_escape "${AUTOPHONE_RIG_ID:-}")\""
    json="$json,\"vm_dir\":\"$(_stage_json_escape "${VM_DIR:-}")\"}"

    dir="$(dirname "$target")"
    [ -d "$dir" ] || mkdir -p "$dir" 2>/dev/null || return 0
    printf '%s\n' "$json" >>"$target" 2>/dev/null || return 0
    return 0
}

# 进入一个阶段。
stage_start() {
    AUTOPHONE_CURRENT_STAGE="$1"
    autophone_emit_event stage_start "stage=$1"
    return 0
}

# 正常离开一个阶段。$2 是状态，默认 ok；frida 阶段在 SKIP_FRIDA=1 时传 skipped。
stage_end() {
    autophone_emit_event stage_end "stage=$1" "status=${2:-ok}"
    AUTOPHONE_CURRENT_STAGE=""
    return 0
}

# 阶段失败。$2=稳定错误码，$3=人可读消息。
# 发射后清空当前阶段，因此紧随其后的 die 不会再发射第二条同阶段事件。
stage_error() {
    autophone_emit_event stage_error "stage=$1" "code=$2" "message=$3"
    AUTOPHONE_CURRENT_STAGE=""
    return 0
}

# 从事件通道推断恢复失败停在哪个阶段：优先取最后一条 stage_error 的阶段名；
# 没有则取最后一个未闭合的 stage_start 的阶段名；两者都没有则 unknown。
#   $1 事件文件；$2 起始行号（1 基，只看这一行及之后的内容）
autophone_last_failed_stage() {
    local file="$1" from_line="${2:-1}"
    if [ ! -f "$file" ]; then
        printf 'unknown'
        return 0
    fi
    tail -n "+$from_line" "$file" 2>/dev/null | awk '
        function stage_of(line,   s) {
            if (match(line, /"stage":"[^"]*"/)) {
                s = substr(line, RSTART + 9, RLENGTH - 10)
                return s
            }
            return ""
        }
        /"event":"stage_error"/ { e = stage_of($0); if (e != "") err = e; next }
        /"event":"stage_start"/ { s = stage_of($0); if (s != "") open = s; next }
        /"event":"stage_end"/   { s = stage_of($0); if (s != "" && s == open) open = ""; next }
        END {
            if (err != "") print err
            else if (open != "") print open
            else print "unknown"
        }
    ' || printf 'unknown'
    return 0
}
