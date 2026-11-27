#!/bin/bash
# prune_runs.sh — 只保留最近 N 次本机运行记录，清理更早的状态。
#
# 运行记录位于 STATE_ROOT/runs/platform/<日期>/<run_id>/，不进入 Git。这个脚本按
# 「最近 N 次」滚动保留，删除更早的 run 目录并清理空日期目录。长期证据应先显式迁入
# evidence/，不由本脚本选择或归档。
#
# 只动 runs/platform。**不碰 runs/rec**——录制资产是蒸馏 operation 的复用资产，
# 按时间删会误删有价值的录制，要清理录制请手动按 app/录制 ID 删。
#
# run 目录名形如 20260629-225527-food_ordering-3e4b，前缀是可字典序排序的时间戳，
# 所以跨日期目录把所有 run 目录按名字排序、保留末尾 N 个即为「最近 N 次」。
#
# 用法：
#   scripts/prune_runs.sh                 预览将删除哪些（dry-run，默认保留 50）
#   scripts/prune_runs.sh -n 30           保留最近 30 次（仍是预览）
#   scripts/prune_runs.sh -n 30 --apply   删除本机状态
#
# 删除默认 dry-run，必须显式 --apply 才执行。

set -euo pipefail

KEEP=50
MODE=dry          # dry | apply

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--keep)  KEEP="$2"; shift 2 ;;
    --apply)    MODE=apply; shift ;;
    --commit)   echo "--commit 已停用：运行记录不进入 Git；请使用 --apply。" >&2; exit 2 ;;
    --dry-run)  MODE=dry; shift ;;
    -h|--help)  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $1（--help 看用法）" >&2; exit 2 ;;
  esac
done

if ! [[ "$KEEP" =~ ^[0-9]+$ ]]; then
  echo "保留数 N 必须是非负整数，得到: $KEEP" >&2; exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE_ROOT="${AUTOPHONE_STATE_DIR:-$REPO_ROOT}"
if [[ -d "$STATE_ROOT" ]]; then
  STATE_ROOT="$(cd "$STATE_ROOT" && pwd -P)"
fi
if [[ -z "$STATE_ROOT" || "$STATE_ROOT" == "/" ]]; then
  echo "拒绝从过宽的 STATE_ROOT 清理运行记录: $STATE_ROOT" >&2; exit 2
fi
PLATFORM_DIR="$STATE_ROOT/runs/platform"

if [[ ! -d "$PLATFORM_DIR" ]]; then
  echo "无 runs/platform，没有运行记录可清理。"; exit 0
fi

# 所有 run 目录（runs/platform/<日期>/<run_id>），按 run_id 名字排序（时间戳前缀）。
# while-read 读数组而非 mapfile：兼容 macOS 自带 bash 3.2（无 mapfile/readarray）。
ALL_RUNS=()
while IFS= read -r line; do
  ALL_RUNS+=("$line")
done < <(find "$PLATFORM_DIR" -mindepth 2 -maxdepth 2 -type d | sort)
TOTAL=${#ALL_RUNS[@]}

# 用 [ -le ] 而非 (( ))：bash 3.2（macOS 自带）在 set -u 下，(( )) 里引用裸变量名
# 会误报 "unbound variable"，算术里一律给变量加 $ 前缀规避。
if [ "$TOTAL" -le "$KEEP" ]; then
  echo "当前 $TOTAL 次运行记录 ≤ 保留数 ${KEEP}，无需清理。"; exit 0
fi

DELETE_COUNT=$(( $TOTAL - $KEEP ))
TO_DELETE=("${ALL_RUNS[@]:0:DELETE_COUNT}")   # 排序后靠前的（更早的）被删

echo "运行记录共 $TOTAL 次，保留最近 $KEEP 次，删除最早 $DELETE_COUNT 次："
for d in "${TO_DELETE[@]}"; do
  sz=$(du -sh "$d" 2>/dev/null | cut -f1)
  echo "  - ${d#$STATE_ROOT/}  ($sz)"
done

if [[ "$MODE" == dry ]]; then
  echo
  echo "（预览模式，未删除。加 --apply 删除本机状态。）"
  exit 0
fi

for d in "${TO_DELETE[@]}"; do
  rm -rf "$d"
done

# 清掉随之变空的日期目录。
find "$PLATFORM_DIR" -mindepth 1 -maxdepth 1 -type d -empty -exec rmdir {} \;

echo
echo "已删除 $DELETE_COUNT 次本机运行记录；未修改 Git 索引。"
