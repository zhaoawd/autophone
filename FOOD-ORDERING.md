# Food Ordering 可选示例

[示例目录](examples/food-ordering/apps/food_ordering)包含一个 App Profile、一条草稿用例和一份 Recipe。R1 使用已注册的 `assert_present` 动作检查首页上的“立即下单”。示例不分发 IPA。

在安装了核心包的环境中，从源码根目录或核心构建输出目录执行：

```bash
export AUTOPHONE_APP_ASSET_DIR="$PWD/examples/food-ordering"
export AUTOPHONE_STATE_DIR="$(mktemp -d)"
autophone app show food_ordering
autophone app case lint food_ordering
```

第一条命令显示示例的 Bundle ID。第二条校验用例库。已安装的核心包也可以用合成 OCR 输入执行这份 Recipe 的正反例；完整入口见[分发验证脚本](scripts/verify_core_install.py)。真实 App 验证需要自行提供兼容构建，按 Build ID 安装并履行项目的用例准入流程。
