# 设备控制台与嵌入式操作

**简体中文** | [English](device-console.en.md)

> 适用范围：Autophone 公开核心平台（单机本地模式与多 VM Control Plane 模式）。

在看板首页（`http://127.0.0.1:8000`）选择目标 Rig 后，可读取截图、打开远程交互控制台、操作模拟定位，或在“**用例回归、IPA 与录制**”中执行用例回归并打开 HTML 证据报告。

## 交互锁与坐标规则

- 远程控制台会话与网页录制在目标 Rig 上持有该设备的 `ScreenLock`，防止自动化 Run 与人工操作互相抢占触屏。
- `upstream_v1` 协议下，“发送文本”写入目标 guest 剪贴板（`UIPasteboard`），在输入框内选择“粘贴”即可输入。
- 坐标按最近一帧画面的尺寸提交；创建会话可固定 `request_id`、发送操作可沿用原 `event_id` 保证重试幂等。

## 免设备交互演示页

无需启动 VM，启动看板后直接访问以下地址即可体验控制台交互流程：

```text
http://127.0.0.1:8000/embed/device-console?demo=1
```

## HTTP 接口摘要

请求受看板鉴权保护（配置了 `AUTOPHONE_AUTH` 时使用 Basic Auth）。完整请求与响应模型见[HTTP API 参考](../reference/http-api.md)。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/platform/targets` | 读取设备列表、部署模式（`local`/`control`）与在线状态 |
| `GET /devices/{device}/status` | 读取目标设备健康与会话状态 |
| `GET /devices/{device}/screen.jpg` | 读取目标设备当前截图 |
| `POST /api/devices/{device}/console` | 创建远程控制会话（返回 `session_id`、`access_token`、`access_header`） |
| `GET /api/devices/{device}/console/{session_id}/frame` | 读取 JPEG 帧与 `X-Frame-Seq` |
| `POST /api/devices/{device}/console/{session_id}/event` | 发送点击、滑动、按键或剪贴板事件 |
| `POST /api/devices/{device}/console/{session_id}/heartbeat` | 续期控制会话 |
| `POST /api/devices/{device}/console/{session_id}/stop` | 释放设备锁并结束会话 |

## 跨站点嵌入

通过 `POST /api/access/launch-tokens` 为指定设备签发短期单次启动凭证。嵌入页 `/embed/device-console` 消费 URL 中的 `launch` 参数后会立即从地址栏清除该参数，并调用 `POST /api/access/embed-sessions` 兑换会话凭证。

反向代理部署时应启用 TLS，配置 `AUTOPHONE_AUTH`，并让代理访问日志省略查询字符串。另见[核心分发安装与验证](../public-core.md)与[核心交互式架构图](../architecture.html)。
