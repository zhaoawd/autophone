from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path

from autophone.application.device import DeviceApplication

_STATIC_ASSET_RE = re.compile(
    r"(?P<url>/static/(?P<path>[A-Za-z0-9_./-]+))\?v=[^\"'<>\s]+"
)


def _fingerprint_static_assets(html: str, static_dir: Path) -> str:
    def replace(match: re.Match[str]) -> str:
        asset = static_dir / match.group("path")
        if not asset.is_file():
            return match.group(0)
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()[:12]
        return f"{match.group('url')}?v={digest}"

    return _STATIC_ASSET_RE.sub(replace, html)


def render_dashboard_html() -> str:
    """Single-file SPA dashboard served at ``/`` by the FastAPI app.

    Full-fidelity build of the Claude design
    (``docs/archive/assets/design-composer/autophone 平台.dc.html``). The pane list is defined once in
    ``static/dashboard/core.js`` (the ``PANES`` array) and that is the single
    source of truth — currently 16 panes; do not restate the names here.
    Panes are wired to the real
    endpoints in ``api.py`` — ``/api/apps``,
    ``/api/apps/{app}/cases|packages|operations``,
    ``/devices/{d}/status|screen.mjpeg``, ``POST /runs`` (type=regression /
    verification / operation), ``/api/runs/...``, ``/api/verifications``,
    ``/api/promotion/{draft_id}``.

    Vanilla JS, no build step. Interface failures render explicit error/empty
    states — never fabricated data. Offline preview: append ``?demo=1`` (or set
    ``localStorage.ap_demo='1'``) to render against built-in fixtures; bare ``/``
    in production hits the live endpoints.

    Refactored: HTML/CSS/JS split into separate files in static/ directory.
    """
    static_dir = Path(__file__).parent / "static"
    html_file = static_dir / "dashboard.html"

    if not html_file.exists():
        return "<h1>Error: dashboard.html not found</h1>"

    target = DeviceApplication().target()
    html = _fingerprint_static_assets(
        html_file.read_text(encoding="utf-8"),
        static_dir,
    )
    return (
        html.replace('"__AUTOPHONE_DEVICE__"', json.dumps(target["device"]))
        .replace("__AUTOPHONE_TARGET__", json.dumps(target))
    )


def render_device_console_embed_html() -> str:
    """远程操作嵌入页，由 ``GET /embed/device-console`` 返回（BL-102）。

    与看板首页的区别：不注入 ``AUTOPHONE_DEVICE`` / ``AUTOPHONE_TARGET``。设备键与模式
    由 URL query 参数给出，同一个页面既服务 autophone 看板弹窗内的 iframe，也服务
    B 端平台的 iframe，服务端不替调用方决定操作哪台设备。

    页面自持画面、手势、心跳、重试与关闭逻辑（``static/embed/device-console.js``），
    不加载看板脚本；与宿主之间只走 postMessage
    （``docs/platform-device-console-embed.md`` §10.3）。
    """
    static_dir = Path(__file__).parent / "static"
    html_file = static_dir / "embed" / "device-console.html"

    if not html_file.exists():
        return "<h1>Error: embed/device-console.html not found</h1>"

    return _fingerprint_static_assets(html_file.read_text(encoding="utf-8"), static_dir)
