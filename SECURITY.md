# Security Policy

**English** | [简体中文](#安全策略)

## Supported Versions

Only the latest minor release line of Autophone Public Core receives security fixes:

| Version | Supported |
| --- | --- |
| `0.2.x` | ✅ Supported |
| `0.1.x` | ❌ Not supported |
| `< 0.1.0` | ❌ Not supported |

## Reporting a Vulnerability

Please report security vulnerabilities privately via GitHub Private Vulnerability Reporting:

- **Direct Advisory Form**: <https://github.com/zhaoawd/autophone/security/advisories/new>

If private vulnerability reporting is unavailable in your GitHub view, open a minimal public issue asking the maintainers for a private contact channel. Do **not** include exploit steps, payloads, tokens, or technical details in any public issue; all technical details should be exchanged exclusively through the confirmed private channel.

## Response SLA Targets

We aim to meet the following response milestones on a best-effort basis:

- **Acknowledgement**: within **72 hours** of receiving a report.
- **Initial Triage Assessment**: within **7 days** (severity assessment, confirmation of scope, and expected remediation path).

## What to Include & Redaction Guidance

To help us reproduce and assess an issue quickly, please include:

- **Affected version**: release tag (e.g. `v0.2.0`), installed package versions, or commit SHA.
- **Deployment mode**: single-VM local mode, multi-VM PostgreSQL Control Plane, embedded console, or standalone `vphone_mcp`.
- **Reproduction prerequisites**: required privileges, host/guest environment details, and minimal reproduction steps.
- **Security impact**: what boundary is bypassed (e.g. auth, Control Plane isolation, ScreenLock, guest UDS, or credential handling).

**Redact sensitive data before sending**: remove all API/auth tokens, passwords, device UDIDs, private URLs, and personal/application PII. Raw run directories may contain screenshots, clipboard contents, network captures, or third-party app data — provide only minimal, redacted excerpts directly relevant to the vulnerability.

---

# 安全策略

[English](#security-policy) | **简体中文**

## 支持的版本

Autophone 公开核心当前仅对最新次版本系列提供安全修复：

| 版本 | 支持状态 |
| --- | --- |
| `0.2.x` | ✅ 支持 |
| `0.1.x` | ❌ 不再支持 |
| `< 0.1.0` | ❌ 不再支持 |

## 漏洞报告渠道

请优先通过 GitHub 私密安全公告入口提交漏洞报告：

- **直接提交入口**：<https://github.com/zhaoawd/autophone/security/advisories/new>

若当前页面未提供私密漏洞报告入口，请先创建一个仅包含“请求私密联系方式”的最小化公开 Issue。**切勿**在公开 Issue 中披露漏洞细节、利用方式或敏感日志；所有技术细节均应通过双方确认的私密渠道交换。

## 响应时效目标（SLA）

维护团队按以下目标尽最大努力响应安全报告：

- **确认收到（Acknowledgement）**：收到报告后 **72 小时内**。
- **初步研判（Initial Triage）**：收到报告后 **7 天内**（确认影响范围、严重程度及后续修复计划）。

## 报告内容与脱敏要求

私密报告请尽量注明：

- **受影响版本**：发布标签（如 `v0.2.0`）、安装包版本或源码 commit SHA。
- **部署模式**：单机本地模式、多 VM PostgreSQL Control Plane、嵌入式控制台或独立 `vphone_mcp`。
- **复现前提**：所需权限、宿主/guest 环境信息与最小复现步骤。
- **安全影响**：涉及的安全边界（如认证鉴权、Control Plane 隔离、ScreenLock、guest UDS 或凭证处理）。

**提交前请务必脱敏**：移除所有访问令牌、密码、设备 UDID、内部 URL 以及个人或应用隐私数据（PII）。原始运行目录可能包含截图、剪贴板内容、网络请求或被测应用数据，请仅提供与问题直接相关的最小化脱敏摘录。
