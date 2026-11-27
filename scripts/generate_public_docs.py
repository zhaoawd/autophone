"""Generate public reference docs from code truth (zero heavy dependencies).

Usage:
    python3 scripts/generate_public_docs.py --write
    python3 scripts/generate_public_docs.py --check
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REFERENCE_DIR = ROOT / "docs" / "reference"

SCHEMA_FILES: tuple[tuple[str, str], ...] = (
    ("app-profile.v1.json", "App Profile (`profile.yaml`)"),
    ("case-registry.v2.json", "Case Registry (`cases/*.yaml`)"),
    ("run-spec.v1.json", "Run Spec (`POST /runs` / `run-spec.json`)"),
    ("run-result.v1.json", "Run Result (`platform-run-result.json`)"),
)

_VERB_EXAMPLES: dict[str, str] = {
    "assert_absent": '- action: assert_absent\n  query: "网络异常"\n  wait_s: 2',
    "assert_present": '- action: assert_present\n  queries: ["通用", "关于本机"]\n  wait_s: 5',
    "assert_present_enhanced": '- action: assert_present_enhanced\n  queries: ["隐私政策"]\n  wait_s: 3',
    "assert_region_nonempty": '- action: assert_region_nonempty\n  regions:\n    - [[0.05, 0.10, 0.95, 0.40], "header"]\n  wait_s: 3',
    "assert_remains_absent": '- action: assert_remains_absent\n  query: "加载中"\n  duration_s: 3\n  poll_ms: 500',
    "assert_screen": '- action: assert_screen\n  screen_id: settings_home\n  wait_s: 3',
    "handle_dialog_if_present": '- action: handle_dialog_if_present\n  query: "允许"\n  tap_query: "允许"',
    "manual_pause": '- action: manual_pause\n  prompt: "请人工核对当前画面后继续"',
    "press_key": '- action: press_key\n  name: home',
    "swipe_xy": '- action: swipe_xy\n  px1: 215\n  py1: 720\n  px2: 215\n  py2: 260\n  ms: 300',
    "tap_ax": '- action: tap_ax\n  label: "通用"\n  role: Button',
    "tap_text": '- action: tap_text\n  query: "通用"\n  retries: 2',
    "tap_xy": '- action: tap_xy\n  px: 215\n  py: 420',
    "type_text": '- action: type_text\n  text: "hello"',
    "wait_text": '- action: wait_text\n  query: "关于本机"\n  timeout_s: 10',
}


def _clean_cell(text: str) -> str:
    return " ".join(str(text or "").split()).replace("|", "\\|")


def _first_doc_line(obj: Any) -> str:
    doc = inspect.getdoc(obj) or ""
    if not doc:
        return "—"
    first_para = doc.split("\n\n", 1)[0]
    return _clean_cell(first_para)


def _normalize_default(value: Any) -> str:
    if value is inspect.Parameter.empty or value is argparse.SUPPRESS:
        return "—"
    if isinstance(value, Path):
        text = str(value)
    else:
        text = repr(value)
    from autophone.paths import STATE_ROOT

    replacements = sorted(
        (
            (str(STATE_ROOT.resolve()), "<ROOT>"),
            (str(ROOT.resolve()), "<ROOT>"),
            (str(Path.home().resolve()), "<HOME>"),
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for prefix, token in replacements:
        if prefix and prefix in text:
            text = text.replace(prefix, token)
    return f"`{text}`"


def render_verbs_doc() -> str:
    from autophone.device.uds import HARDWARE_KEYS
    from autophone.execution.engine import recipe

    lines = [
        "# Recipe Verb 动作手册（自动生成）",
        "",
        "> 本文档由 `scripts/generate_public_docs.py` 从 `autophone.execution.engine.recipe.ACTIONS` 与 `CaseContext` 自动生成，请勿手工修改。",
        "",
        "## 坐标与约束约定",
        "",
        "- **预览像素坐标（`px`, `py`, `px1`, `py1`, `px2`, `py2`, `fallback_xy`）**：使用预览帧像素坐标（默认竖屏 `430×932`，执行层按设备缩放比换算为原生像素）。",
        "- **归一化区域（`region`, `regions`）**：`[nx0, ny0, nx1, ny1]`，取值范围 `[0.0, 1.0]`，与屏幕物理分辨率无关。",
        f"- **硬件按键白名单（`press_key.name`）**：`{'`、`'.join(HARDWARE_KEYS)}`。",
        "- **静态校验**：所有步骤在触达设备前均由 `validate_recipe()` 校验动作名、必填参数、禁止未知字段与参数类型。",
        "",
        "## 可用 Verb 总览",
        "",
        "| Verb | 必填参数 | 说明 |",
        "| --- | --- | --- |",
    ]
    for verb in sorted(recipe.ACTIONS):
        fn = recipe.ACTIONS[verb]
        sig_params = list(inspect.signature(fn).parameters.values())[1:]
        required = [
            f"`{p.name}`"
            for p in sig_params
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
            and p.default is inspect.Parameter.empty
        ]
        req_text = "、".join(required) if required else "无"
        lines.append(f"| [`{verb}`](#{verb.replace('_', '-')}) | {req_text} | {_first_doc_line(fn)} |")

    lines.append("")
    lines.append("## Verb 参数详情")
    lines.append("")
    for verb in sorted(recipe.ACTIONS):
        fn = recipe.ACTIONS[verb]
        sig_params = list(inspect.signature(fn).parameters.values())[1:]
        kwargs = [
            p for p in sig_params
            if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
        ]
        lines.append(f"### `{verb}`")
        lines.append("")
        lines.append(_first_doc_line(fn))
        lines.append("")
        if kwargs:
            lines.append("| 参数 | 必填 | 类型约束 | 默认值 |")
            lines.append("| --- | --- | --- | --- |")
            for param in kwargs:
                req = "是" if param.default is inspect.Parameter.empty else "否"
                value_spec = (
                    recipe._ACTION_VALUE_CHECKS.get((verb, param.name))
                    or recipe._PARAM_TYPES.get(param.name)
                )
                type_text = value_spec[1] if value_spec else (
                    str(param.annotation) if param.annotation is not inspect.Parameter.empty else "any"
                )
                default_text = _normalize_default(param.default)
                lines.append(f"| `{param.name}` | {req} | `{_clean_cell(type_text)}` | {default_text} |")
            lines.append("")
        if verb in _VERB_EXAMPLES:
            lines.append("```yaml")
            lines.append(_VERB_EXAMPLES[verb])
            lines.append("```")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _walk_parser(
    parser: argparse.ArgumentParser,
    command_path: str,
    sections: list[tuple[str, argparse.ArgumentParser, str]],
    *,
    skip_top_level: frozenset[str] = frozenset(),
    help_text: str = "",
) -> None:
    sections.append((command_path, parser, help_text))
    sub_action = next(
        (action for action in parser._actions if isinstance(action, argparse._SubParsersAction)),
        None,
    )
    if sub_action is None:
        return
    help_map = {action.dest: (action.help or "") for action in sub_action._choices_actions}
    seen_ids: set[int] = set()
    for name in sorted(sub_action.choices):
        if command_path == "autophone" and name in skip_top_level:
            continue
        child = sub_action.choices[name]
        if id(child) in seen_ids:
            continue
        seen_ids.add(id(child))
        _walk_parser(child, f"{command_path} {name}", sections, help_text=help_map.get(name, ""))


def render_cli_doc() -> str:
    from autophone.cli import EXPERIMENTAL_CLI_COMMANDS, build_parser

    previous_exp = os.environ.pop("AUTOPHONE_CLI_SHOW_EXPERIMENTAL", None)
    previous_ext = os.environ.get("AUTOPHONE_DISABLE_EXTENSIONS")
    os.environ["AUTOPHONE_DISABLE_EXTENSIONS"] = "1"
    try:
        parser = build_parser(include_extensions=False)
    finally:
        if previous_exp is not None:
            os.environ["AUTOPHONE_CLI_SHOW_EXPERIMENTAL"] = previous_exp
        if previous_ext is None:
            os.environ.pop("AUTOPHONE_DISABLE_EXTENSIONS", None)
        else:
            os.environ["AUTOPHONE_DISABLE_EXTENSIONS"] = previous_ext

    sections: list[tuple[str, argparse.ArgumentParser, str]] = []
    _walk_parser(parser, "autophone", sections, skip_top_level=EXPERIMENTAL_CLI_COMMANDS)

    lines = [
        "# CLI 命令参考（自动生成）",
        "",
        "> 本文档由 `scripts/generate_public_docs.py` 从公开核心态 `autophone.cli.build_parser(include_extensions=False)` 自动生成，请勿手工修改。",
        "> 默认仅列出公开核心主命令；如需在 `autophone --help` 中查看实验性子命令，可设置 `AUTOPHONE_CLI_SHOW_EXPERIMENTAL=1`。",
        "",
        "## 命令索引",
        "",
        "| 命令 | 说明 |",
        "| --- | --- |",
    ]
    for path, cmd_parser, help_text in sections:
        if path == "autophone":
            continue
        desc = _clean_cell(help_text or cmd_parser.description or "—")
        lines.append(f"| `{path}` | {desc} |")

    lines.append("")
    lines.append("## 命令参数详情")
    lines.append("")
    for path, cmd_parser, help_text in sections:
        lines.append(f"### `{path}`")
        lines.append("")
        desc = _clean_cell(help_text or cmd_parser.description or "")
        if desc:
            lines.append(desc)
            lines.append("")
        actions = [
            action for action in cmd_parser._actions
            if not isinstance(action, argparse._SubParsersAction)
            and action.help is not argparse.SUPPRESS
            and action.dest != "help"
        ]
        if actions:
            lines.append("| 参数 / 选项 | 必填 | 可选值 | 默认值 | 说明 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for action in actions:
                flags = ", ".join(f"`{f}`" for f in action.option_strings) if action.option_strings else f"`<{action.dest}>`"
                required = "是" if (action.required or not action.option_strings and action.nargs not in ("?", "*")) else "否"
                choices = ", ".join(f"`{c}`" for c in action.choices) if action.choices else "—"
                default = _normalize_default(action.default) if action.option_strings else "—"
                help_str = _clean_cell(action.help or "—")
                lines.append(f"| {flags} | {required} | {choices} | {default} | {help_str} |")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _schema_type(prop: dict[str, Any]) -> str:
    if "const" in prop:
        return f"const `{prop['const']!r}`"
    if "enum" in prop:
        vals = " \\| ".join(f"`{v!r}`" for v in prop["enum"])
        return f"enum: {vals}"
    if "$ref" in prop:
        return f"`{prop['$ref']}`"
    if "oneOf" in prop:
        return "oneOf"
    if "anyOf" in prop:
        return "anyOf"
    raw = prop.get("type")
    if isinstance(raw, list):
        return " \\| ".join(f"`{item}`" for item in raw)
    if raw == "array":
        items = prop.get("items")
        if isinstance(items, dict):
            return f"`array<{items.get('type') or items.get('$ref') or 'any'}>`"
        return "`array`"
    return f"`{raw}`" if raw else "`object`"


def render_schemas_doc() -> str:
    schema_dir = ROOT / "autophone" / "_data" / "schemas"
    lines = [
        "# 核心 JSON Schema 字典（自动生成）",
        "",
        "> 本文档由 `scripts/generate_public_docs.py` 从 `autophone/_data/schemas/` 核心契约文件自动生成，请勿手工修改。",
        "",
    ]
    for filename, title in SCHEMA_FILES:
        doc = json.loads((schema_dir / filename).read_text(encoding="utf-8"))
        required = set(doc.get("required") or [])
        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"- Schema 文件：`autophone/_data/schemas/{filename}`")
        if doc.get("description"):
            lines.append(f"- 说明：{_clean_cell(doc['description'])}")
        if required:
            lines.append("- 必填字段：" + "、".join(f"`{k}`" for k in sorted(required)))
        lines.append("")
        props = doc.get("properties") or {}
        if props:
            lines.append("| 字段 | 类型 / 枚举 | 必填 | 说明 |")
            lines.append("| --- | --- | --- | --- |")
            for name in sorted(props):
                prop = props[name]
                req = "是" if name in required else "否"
                desc = _clean_cell(prop.get("description") or "—")
                lines.append(f"| `{name}` | {_schema_type(prop)} | {req} | {desc} |")
            lines.append("")
        defs = doc.get("$defs") or {}
        if defs:
            lines.append("### 嵌套定义（`$defs`）")
            lines.append("")
            for def_name in sorted(defs):
                sub = defs[def_name]
                sub_req = sorted(sub.get("required") or [])
                sub_props = sorted((sub.get("properties") or {}).keys())
                summary = []
                if sub_req:
                    summary.append("必填: " + ", ".join(f"`{k}`" for k in sub_req))
                if sub_props:
                    summary.append("字段: " + ", ".join(f"`{k}`" for k in sub_props[:10]) + ("…" if len(sub_props) > 10 else ""))
                if not summary and sub.get("description"):
                    summary.append(_clean_cell(sub["description"]))
                lines.append(f"- **`{def_name}`**：" + ("；".join(summary) if summary else _schema_type(sub)))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _openapi_group(path: str) -> str:
    if path in {"/", "/health"} or path.startswith("/api/platform"):
        return "平台与健康检查"
    if path.startswith(("/devices/", "/api/devices/", "/api/access/", "/embed/")):
        return "设备与控制台"
    if path.startswith("/api/location"):
        return "系统定位与路线"
    if path.startswith("/api/control"):
        return "Control Plane 控制面"
    if path.startswith(("/runs", "/api/runs")):
        return "回归运行与证据报告"
    if path.startswith("/api/apps"):
        return "App、用例、IPA 与录制"
    return "其他核心接口"


def _fetch_core_openapi() -> dict[str, Any]:
    snippet = """
import json
from autophone.adapters.http.app import create_app
app = create_app()
print(json.dumps(app.openapi(), ensure_ascii=False))
"""
    with TemporaryDirectory(prefix="autophone-openapi-") as scratch:
        env = {
            **os.environ,
            "AUTOPHONE_DISABLE_EXTENSIONS": "1",
            "AUTOPHONE_STATE_DIR": scratch,
            "AUTOPHONE_SCHEDULER_ENABLED": "0",
            "AUTOPHONE_FOREGROUND_ENABLED": "0",
            "PYTHONPATH": str(ROOT),
        }
        for key in ("AUTOPHONE_DATABASE_URL", "AUTOPHONE_CONTROL_HTTP", "AUTOPHONE_CONTROL_WS"):
            env.pop(key, None)
        out = subprocess.check_output(
            [sys.executable, "-c", snippet],
            cwd=ROOT,
            env=env,
            text=True,
        )
    return json.loads(out)


def render_http_api_doc() -> str:
    spec = _fetch_core_openapi()
    grouped: dict[str, list[tuple[str, str, str, str]]] = {}
    for path, methods in sorted((spec.get("paths") or {}).items()):
        group = _openapi_group(path)
        for method, operation in sorted(methods.items()):
            if method.lower() not in {"get", "post", "put", "patch", "delete"}:
                continue
            summary = _clean_cell(operation.get("summary") or operation.get("operationId") or "—")
            params = [
                f"`{p['name']}` ({p['in']})"
                for p in (operation.get("parameters") or [])
                if isinstance(p, dict) and "name" in p and "in" in p
            ]
            if operation.get("requestBody"):
                params.append("`body` (json)")
            param_text = ", ".join(params) if params else "—"
            grouped.setdefault(group, []).append((method.upper(), path, summary, param_text))

    group_order = [
        "平台与健康检查",
        "设备与控制台",
        "系统定位与路线",
        "App、用例、IPA 与录制",
        "回归运行与证据报告",
        "Control Plane 控制面",
        "其他核心接口",
    ]
    lines = [
        "# HTTP API 接口目录（自动生成）",
        "",
        "> 本文档由 `scripts/generate_public_docs.py` 从公开核心态 FastAPI `app.openapi()`（不含 `delivery` 扩展）自动生成，请勿手工修改。",
        "> 启动看板后亦可直接访问 `/openapi.json` 或 `/docs` 获取实时交互式接口文档。",
        "",
    ]
    for group in group_order:
        rows = grouped.get(group)
        if not rows:
            continue
        lines.append(f"## {group}")
        lines.append("")
        lines.append("| 方法 | 路径 | 参数 / 请求体 | 摘要 |")
        lines.append("| --- | --- | --- | --- |")
        for method, path, summary, param_text in rows:
            lines.append(f"| `{method}` | `{path}` | {param_text} | {summary} |")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def generate_all() -> dict[Path, str]:
    return {
        REFERENCE_DIR / "verbs.md": render_verbs_doc(),
        REFERENCE_DIR / "cli.md": render_cli_doc(),
        REFERENCE_DIR / "schemas.md": render_schemas_doc(),
        REFERENCE_DIR / "http-api.md": render_http_api_doc(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="write generated docs to docs/reference/")
    mode.add_argument("--check", action="store_true", help="fail if docs/reference/ differs from generated output")
    args = parser.parse_args(argv)

    rendered = generate_all()
    if args.write:
        REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
        for path, content in rendered.items():
            path.write_text(content, encoding="utf-8")
            print(f"Wrote {path.relative_to(ROOT)}")
        return 0

    drifted: list[str] = []
    for path, expected in rendered.items():
        if not path.is_file():
            drifted.append(f"missing: {path.relative_to(ROOT)}")
            continue
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            drifted.append(f"out of date: {path.relative_to(ROOT)}")
    if drifted:
        print("Public reference docs are out of date. Run `python3 scripts/generate_public_docs.py --write`:",
              file=sys.stderr)
        for item in drifted:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print(f"Public reference docs up to date ({len(rendered)} files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
