#!/usr/bin/env bash
# 注册资产 YAML 的 git 自定义合并驱动（用例库/操作库）。
#
# 合并驱动需要两半：
#   ① .gitattributes 里 `merge=casesyaml`（已入库、随仓库共享）；
#   ② 本机 .git/config 里 [merge "casesyaml"] 的 driver 命令（per-clone，不入库）。
# 本脚本装的是②。**每台机器 clone 后各跑一次**（不跑则这些文件回退默认 text 合并）。
#
# 用法： bash tools/git/install_merge_driver.sh
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

# 用 uv run 保证 PyYAML 在环境里；%O/%A/%B/%P 由 git 在合并时填入。
git config merge.casesyaml.name "用例库/操作库 YAML 语义三方合并"
git config merge.casesyaml.driver \
  "uv run python3 -m tools.git.merge_yaml_assets %O %A %B %P"

echo "已注册 merge driver 'casesyaml'。自检："
uv run python3 -m tools.git.merge_yaml_assets --selftest
