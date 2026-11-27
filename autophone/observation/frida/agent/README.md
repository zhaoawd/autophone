# Frida Agent

Python 加载的 Agent 通过 `frida-compile` 将 `frida-objc-bridge` 编入脚本，以使用 Objective-C API。

## 构建

`frida` 依赖组提供 Python 编译工具。npm 锁文件固定 ObjC bridge 版本。项目的按需构建入口会安装锁定的依赖并将产物写入状态目录。

```bash
uv sync --group frida
uv run --group frida python -c 'from autophone.observation.frida.agent_bundle import agent_build_path, ensure_agent_bundle; print(ensure_agent_bundle(agent_build_path("objc_bridge_probe.js")))'
```

将文件名替换为 `corelocation_control.js` 可单独构建系统定位控制脚本。
使用另外安装的兼容编译器时，设置 `FRIDA_COMPILE` 后可从本目录运行 `npm run build`。

## 验证

在已授权的测试 App 上执行仓库的 `objc_bundle_probe`，检查 `ObjC.available`、`NSURLSession` 类解析和 RPC 响应。
