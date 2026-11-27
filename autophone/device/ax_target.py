"""ax_target.py — semantic targeting by accessibility structure: find an element
by its label/role/identifier in an AX tree dump and tap it.

Host-side AX spike (docs/phase1b-ax-spike.md). The deeper upgrade to the OCR
`tap_text` path: OCR sees visible *text* (pixels), AX sees *structure* — icons
with no text, role/enabled/value semantics, hierarchy. They are complementary
fallbacks, not a choice.

This module owns the HOST side only:
  - parse an `autophone.ax/1` dump (produced in-guest by ax_dump.dylib),
  - select a node by label/role/identifier (+ optional region / enabled filter),
  - map its frame from POINTS to the socket's native tap space.

The AX frame is in points (screen coords, top-left origin); native px = points *
screen.scale. We read `screen.scale` from the dump and never hardcode ×3 — same
discipline as ocr_target — so a firmware that changes scale won't silently
mis-map.

Subcommands:
  dump   <ax.json>                 print every node + native coords
  locate <ax.json> --query 发送     best match + native tap coords
  probe                            build-free, VM-free self-test on a synthetic
                                   dump (disambiguates two 发送 by role+region,
                                   asserts points→native mapping)
  pull   [--sock …]                file_get an already-triggered guest dump back
                                   over UDS and parse it (needs VM).
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SCHEMA = "autophone.ax/1"
DEFAULT_GUEST_DUMP_PATH = "/tmp/autophone_ax.json"
DUMP_NOTIFICATION = "com.autophone.ax.dump"


class AxError(RuntimeError):
    pass


@dataclass
class Node:
    label: str
    role: str
    traits: list[str]
    # frame in POINTS, screen coords, top-left origin: [x, y, w, h]
    frame: tuple[float, float, float, float]
    enabled: bool
    value: Any
    identifier: str
    path: str
    # filled in by mapping against the dump's screen block:
    native_x: int = 0
    native_y: int = 0
    nx: float = 0.0          # normalized [0,1] center, for region filters
    ny: float = 0.0
    hittable: bool | None = None
    occluded_reason: str = ""


@dataclass
class AxDump:
    app: str
    scale: float
    w: float                 # screen width in points
    h: float                 # screen height in points
    view_controller: str = ""
    nodes: list[Node] = field(default_factory=list)
    ax_backend: str | None = None
    dumper_version: int | None = None
    application_state: int | None = None
    key_window_scene_activation_state: int | None = None
    key_window_is_app_window: bool | None = None
    foreground_active: bool | None = None
    foreground_occlusion_reason: str = ""


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _map_node(raw: dict[str, Any], scale: float, sw: float, sh: float) -> Node:
    fx, fy, fw, fh = (float(v) for v in raw["frame"])
    cx = fx + fw / 2.0
    cy = fy + fh / 2.0
    raw_reason = raw.get("occluded_reason")
    return Node(
        label=raw.get("label") or "",
        role=raw.get("role") or "",
        traits=list(raw.get("traits") or []),
        frame=(fx, fy, fw, fh),
        enabled=bool(raw.get("enabled", True)),
        value=raw.get("value"),
        identifier=raw.get("identifier") or "",
        path=raw.get("path") or "",
        # points → native pixels (NOT a hardcoded ×3; comes from the dump).
        native_x=round(cx * scale),
        native_y=round(cy * scale),
        # normalize against the points screen size for region constraints.
        nx=(cx / sw) if sw else 0.0,
        ny=(cy / sh) if sh else 0.0,
        hittable=_optional_bool(raw.get("hittable")),
        occluded_reason=raw_reason if isinstance(raw_reason, str) else "",
    )


def parse_dump(obj: dict[str, Any], *, ax_backend: str | None = None) -> AxDump:
    """Validate + map an `autophone.ax/1` dump. Fail-closed on a wrong schema or
    a missing/zero screen block (which would make the mapping silently wrong)."""
    if obj.get("schema") != SCHEMA:
        raise AxError(f"unexpected schema {obj.get('schema')!r}, want {SCHEMA!r}")
    screen = obj.get("screen") or {}
    scale = float(screen.get("scale", 0) or 0)
    sw = float(screen.get("w", 0) or 0)
    sh = float(screen.get("h", 0) or 0)
    if scale <= 0 or sw <= 0 or sh <= 0:
        raise AxError(
            f"implausible screen block scale={scale} w={sw} h={sh}; "
            "cannot map points→native")
    raw_backend = obj.get("ax_backend")
    resolved_backend: str | None
    if raw_backend is not None:
        if not isinstance(raw_backend, str) or not raw_backend.strip():
            raise AxError(f"invalid ax_backend in AX dump: {raw_backend!r}")
        resolved_backend = raw_backend.strip()
    else:
        resolved_backend = None
    if ax_backend is not None:
        if not isinstance(ax_backend, str) or not ax_backend.strip():
            raise AxError(f"invalid ax_backend argument: {ax_backend!r}")
        expected_backend = ax_backend.strip()
        if resolved_backend is not None and resolved_backend != expected_backend:
            raise AxError(
                f"ax_backend mismatch: dump={resolved_backend!r}, expected={expected_backend!r}"
            )
        resolved_backend = expected_backend
    nodes = [_map_node(n, scale, sw, sh) for n in (obj.get("nodes") or [])]
    return AxDump(
        app=obj.get("app") or "",
        scale=scale,
        w=sw,
        h=sh,
        view_controller=str(obj.get("view_controller") or ""),
        nodes=nodes,
        ax_backend=resolved_backend,
        dumper_version=_optional_int(obj.get("dumper_version")),
        application_state=_optional_int(obj.get("application_state")),
        key_window_scene_activation_state=_optional_int(
            obj.get("key_window_scene_activation_state")
        ),
        key_window_is_app_window=_optional_bool(obj.get("key_window_is_app_window")),
        foreground_active=_optional_bool(obj.get("foreground_active")),
        foreground_occlusion_reason=(
            obj.get("foreground_occlusion_reason")
            if isinstance(obj.get("foreground_occlusion_reason"), str)
            else ""
        ),
    )


def load_dump(path: str | Path) -> AxDump:
    with Path(path).open(encoding="utf-8") as f:
        return parse_dump(json.load(f))


def find_node(
    dump: AxDump,
    query: str,
    *,
    role: str | None = None,
    identifier: str | None = None,
    region: tuple[float, float, float, float] | None = None,
    enabled_only: bool = True,
    hittable_only: bool = False,
) -> list[Node]:
    """Matches for `query`, sorted best-first. Match is on label OR value OR
    identifier (substring), optionally filtered by role, exact identifier,
    normalized region (nx0,ny0,nx1,ny1), enabled state, and hittability.

    When `hittable_only=True`, nodes with `hittable is False` are skipped while
    legacy/synthetic nodes with `hittable is None` remain eligible. Default is
    `False` so callers such as `tap_ax` can distinguish occluded matches from
    missing locators.

    Sort key prefers: an exact label match, then an enabled node, then a shorter
    label (more specific). `region`/`role` disambiguate a label that appears more
    than once (e.g. two 发送) — the same job region does for OCR, plus role.
    """
    out: list[Node] = []
    for n in dump.nodes:
        hay = (n.label, str(n.value or ""), n.identifier)
        if query and not any(query in h for h in hay):
            continue
        if role and n.role != role:
            continue
        if identifier and n.identifier != identifier:
            continue
        if enabled_only and not n.enabled:
            continue
        if hittable_only and n.hittable is False:
            continue
        if region and not (region[0] <= n.nx <= region[2]
                           and region[1] <= n.ny <= region[3]):
            continue
        out.append(n)
    out.sort(key=lambda n: (n.label != query, not n.enabled, len(n.label)))
    return out


# ── synthetic fixture for the VM-free probe ────────────────────────────────

def _synthetic_dump() -> dict[str, Any]:
    """A plausible chat-app AX tree: two 发送 (one disabled keyboard-return in
    the upper area, one enabled send button lower-right) so the probe must use
    role+region+enabled to pick the right one; plus an icon-only + button that
    has NO text (the case OCR can't reach) addressable by identifier."""
    return {
        "schema": SCHEMA,
        "app": "com.example.chat",
        "screen": {"scale": 3.0, "w": 430, "h": 932},
        "nodes": [
            {"label": "群聊名称", "role": "header", "traits": ["header"],
             "frame": [120, 40, 190, 30], "enabled": True, "path": "0/0"},
            {"label": "发送", "role": "button", "traits": ["button"],
             "frame": [360, 300, 50, 28], "enabled": False,  # a disabled decoy
             "path": "0/1/3"},
            {"label": "", "role": "button", "traits": ["button"],
             "identifier": "chat_input_plus",                 # icon-only, no text
             "frame": [12, 858, 36, 36], "enabled": True, "path": "0/2/0"},
            {"label": "相册", "role": "button", "traits": ["button"],
             "frame": [40, 600, 60, 60], "enabled": True, "path": "0/3/1"},
            {"label": "发送", "role": "button", "traits": ["button"],
             "frame": [350, 858, 60, 36], "enabled": True,    # the real one
             "path": "0/2/5"},
        ],
    }


def _cmd_dump(args: argparse.Namespace) -> int:
    dump = load_dump(args.ax_json)
    print(f"app {dump.app}  scale {dump.scale}  screen {dump.w:.0f}x{dump.h:.0f}pt"
          f"  {len(dump.nodes)} nodes")
    for n in dump.nodes:
        en = "" if n.enabled else " DISABLED"
        ident = f" #{n.identifier}" if n.identifier else ""
        print(f"  [{n.role or '?':10}] {n.label!r}{ident}{en}  "
              f"frame={tuple(round(v) for v in n.frame)}pt  "
              f"native=({n.native_x},{n.native_y})")
    return 0


def _cmd_locate(args: argparse.Namespace) -> int:
    dump = load_dump(args.ax_json)
    region = tuple(args.region) if args.region else None
    matches = find_node(dump, args.query, role=args.role,
                        identifier=args.identifier, region=region,
                        enabled_only=not args.include_disabled)
    if not matches:
        print(f"no node for query={args.query!r} role={args.role}", file=sys.stderr)
        return 1
    n = matches[0]
    print(json.dumps({
        "label": n.label, "role": n.role, "identifier": n.identifier,
        "enabled": n.enabled, "value": n.value,
        "frame_pt": [round(v) for v in n.frame],
        "center_norm": [round(n.nx, 3), round(n.ny, 3)],
        "native_tap": [n.native_x, n.native_y],
        "other_matches": len(matches) - 1,
    }, ensure_ascii=False, indent=2))
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    """VM-free, build-free self-test of parse → select → points→native mapping."""
    dump = parse_dump(_synthetic_dump())
    ok = True

    def check(desc: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"  {'ok  ' if cond else 'FAIL'}  {desc}{('  ' + detail) if detail else ''}")
        ok = ok and cond

    # 1) two 发送 exist; enabled_only + lower-right region must pick the real one.
    all_send = find_node(dump, "发送", enabled_only=False)
    check("two 发送 nodes present", len(all_send) == 2, f"got {len(all_send)}")
    send = find_node(dump, "发送", role="button",
                     region=(0.5, 0.5, 1.0, 1.0))            # lower-right
    picked = send[0] if send else None
    check("picked the enabled lower-right 发送",
          picked is not None and picked.enabled and picked.frame[1] > 800,
          f"native={(picked.native_x, picked.native_y) if picked else None}")
    # frame center (350+30, 858+18)=(380,876) pt × scale 3 → (1140, 2628) native
    check("发送 points→native maps correctly",
          picked is not None and (picked.native_x, picked.native_y) == (1140, 2628),
          f"got {(picked.native_x, picked.native_y) if picked else None}")

    # 2) the icon-only + button has no label — only AX can reach it (by id).
    plus = find_node(dump, "", identifier="chat_input_plus")
    check("icon-only + addressable by identifier (OCR can't)",
          len(plus) == 1 and plus[0].label == "",
          f"native={(plus[0].native_x, plus[0].native_y) if plus else None}")

    # 3) enabled_only must hide the disabled decoy 发送.
    enabled_send = find_node(dump, "发送", enabled_only=True)
    check("enabled_only filters out the disabled decoy",
          all(n.enabled for n in enabled_send) and len(enabled_send) == 1)

    # 4) fail-closed: a zero scale must raise, not silently map to (0,0).
    try:
        parse_dump({"schema": SCHEMA, "screen": {"scale": 0, "w": 430, "h": 932},
                    "nodes": []})
        check("zero-scale dump rejected", False, "no raise")
    except AxError:
        check("zero-scale dump rejected", True)

    print("PROBE PASS" if ok else "PROBE FAIL")
    return 0 if ok else 1


def _cmd_pull(args: argparse.Namespace) -> int:
    """Pull an already-triggered guest dump back over UDS file_get and parse it.

    The transport this used to wait on now exists (upstream UDS file_get, commit
    30d1f47). This pulls /tmp/autophone_ax.json to the host and prints the nodes.

    It does NOT inject or trigger — `pull` is the host half once a guest dump exists.
    """
    from autophone.device.uds import request
    from autophone.device.shell import resolve_sock

    sock = args.sock or resolve_sock()
    local = args.out
    resp, _ = request({"t": "file_get", "path": args.guest_path, "save": local},
                      sock_path=sock, timeout=20)
    if not resp.get("ok"):
        print(f"file_get {args.guest_path} failed: {resp}", file=sys.stderr)
        print("(trigger a guest AX dump first before calling pull)", file=sys.stderr)
        return 1
    dump = load_dump(local)
    print(f"pulled {resp.get('size')} bytes -> {local}")
    print(f"app {dump.app}  scale {dump.scale}  screen {dump.w:.0f}x{dump.h:.0f}pt"
          f"  {len(dump.nodes)} nodes")
    for n in dump.nodes:
        en = "" if n.enabled else " DISABLED"
        ident = f" #{n.identifier}" if n.identifier else ""
        print(f"  [{n.role or '?':10}] {n.label!r}{ident}{en}  "
              f"native=({n.native_x},{n.native_y})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("dump", help="print every node + native coords")
    pd.add_argument("ax_json")
    pd.set_defaults(func=_cmd_dump)

    pl = sub.add_parser("locate", help="best match for --query + native coords")
    pl.add_argument("ax_json")
    pl.add_argument("--query", default="")
    pl.add_argument("--role", default=None)
    pl.add_argument("--identifier", default=None)
    pl.add_argument("--region", type=float, nargs=4, default=None,
                    metavar=("NX0", "NY0", "NX1", "NY1"))
    pl.add_argument("--include-disabled", action="store_true")
    pl.set_defaults(func=_cmd_locate)

    pp = sub.add_parser("probe", help="deterministic self-test (no VM, no build)")
    pp.set_defaults(func=_cmd_probe)

    pt = sub.add_parser("pull", help="file_get an already-triggered guest dump → parse (needs VM)")
    pt.add_argument("--sock", default=None)
    pt.add_argument("--guest-path", default=DEFAULT_GUEST_DUMP_PATH)
    pt.add_argument("--out", default="/tmp/ax_pulled.json")
    pt.set_defaults(func=_cmd_pull)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
