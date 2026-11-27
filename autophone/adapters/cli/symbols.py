from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from autophone.adapters.cli import _common as common
from autophone.assets.apps import AppProfile


def _resolve_cli_profile_and_package_root(
    args: argparse.Namespace,
) -> tuple[str | AppProfile, Path | None]:
    output_root: Path | None = getattr(args, "output_root", None)
    explicit_pkg_root: Path | None = getattr(args, "package_root", None)

    if output_root is None:
        profile = common.load_app_profile(args.app_name)
        return profile, explicit_pkg_root or profile.host_state_root

    candidate_profile = output_root / "apps" / args.app_name / "profile.yaml"
    if candidate_profile.is_file():
        profile = common.load_app_profile(args.app_name, root=output_root, state_root=output_root)
        return profile, explicit_pkg_root or profile.host_state_root

    inferred_pkg_root = (
        output_root
        if (output_root / "packages" / args.app_name).is_dir()
        else common.STATE_ROOT
    )
    return args.app_name, explicit_pkg_root or inferred_pkg_root


def _emit_summary(summary: dict[str, Any], *, as_json: bool) -> int:
    if as_json:
        print(common.json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    lines = [
        f"app: {summary['app']}",
        f"source: {summary['source']}",
        f"symbol_path: {summary['symbol_path']}",
        f"claims_path: {summary['claims_path']}",
        f"artifact_content_digest: {summary['artifact_content_digest']}",
        f"binding_sha256: {summary['binding_sha256']}",
        f"controls: {summary['control_count']}",
        f"claims_written: {summary['claims_written']}",
    ]
    if summary.get("bundle_format"):
        lines.insert(2, f"bundle_format: {summary['bundle_format']}")
    print("\n".join(lines))
    return 0


def _emit_error(exc: Exception, *, as_json: bool) -> int:
    code = getattr(exc, "code", "symbol_tool_error")
    if as_json:
        print(
            common.json.dumps(
                {"ok": False, "error": {"code": code, "message": str(exc)}},
                ensure_ascii=False,
                indent=2,
            ),
            file=common.sys.stderr,
        )
    else:
        print(f"symbol tool failed [{code}]: {exc}", file=common.sys.stderr)
    return 1


def cmd_symbols_import_l2(args: argparse.Namespace) -> int:
    """Import an L2 build-side symbol manifest into ``apps/<app>/symbols/artifacts/``."""
    from autophone.assets.app_symbols import SymbolBindingError
    from autophone.assets.symbol_tools import import_l2_manifest

    if args.output_root is None:
        common.assert_assets_writable("import L2 app symbol binding")
    try:
        target, package_root = _resolve_cli_profile_and_package_root(args)
        summary = import_l2_manifest(
            target,
            args.manifest,
            build_id=args.build_id,
            output_root=args.output_root,
            package_root=package_root,
            overwrite=args.overwrite,
        )
    except (SymbolBindingError, FileNotFoundError, OSError, ValueError) as exc:
        return _emit_error(exc, as_json=args.as_json)
    return _emit_summary(summary, as_json=args.as_json)


def cmd_symbols_extract_l1_rn(args: argparse.Namespace) -> int:
    """Blind-extract React Native ``testID`` / ``accessibilityIdentifier`` candidates from an IPA."""
    from autophone.assets.app_symbols import SymbolBindingError
    from autophone.assets.symbol_tools import extract_l1_rn_symbols

    if args.output_root is None:
        common.assert_assets_writable("extract L1 RN app symbol binding")
    try:
        target, _ = _resolve_cli_profile_and_package_root(args)
        summary = extract_l1_rn_symbols(
            target,
            ipa_path=args.ipa,
            build_id=args.build_id,
            default_screen_id=args.default_screen_id,
            symbol_prefix=args.symbol_prefix,
            output_root=args.output_root,
            overwrite=args.overwrite,
        )
    except (SymbolBindingError, FileNotFoundError, OSError, ValueError) as exc:
        return _emit_error(exc, as_json=args.as_json)
    return _emit_summary(summary, as_json=args.as_json)


def register(sub: argparse._SubParsersAction) -> None:
    symbols = sub.add_parser(
        "symbols",
        help="manage immutable app-symbols.v1 bindings (L2 manifest import & L1 RN extraction)",
    )
    symbols_sub = symbols.add_subparsers(dest="symbols_cmd", required=True)

    import_l2 = symbols_sub.add_parser(
        "import-l2",
        help="import a build-side L2 thin symbol manifest and emit static_hint claims",
    )
    import_l2.add_argument("app_name", help="app id / 目录名")
    import_l2.add_argument(
        "--manifest",
        required=True,
        type=common.Path,
        help="构建侧 L2 薄清单 JSON/YAML 文件路径",
    )
    import_l2.add_argument(
        "--build-id",
        default=None,
        help="Package Store 精确 Build ID（缺省时使用清单内 artifact_identity）",
    )
    import_l2.add_argument(
        "--output-root",
        type=common.Path,
        default=None,
        help="输出资产根目录（默认写入当前 App 资产根 apps/<app>/symbols/）",
    )
    import_l2.add_argument(
        "--package-root",
        type=common.Path,
        default=None,
        help="Package Store 根目录（默认 STATE_ROOT）",
    )
    import_l2.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖同制品摘要下内容有变更的既有符号文件",
    )
    import_l2.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="以 JSON 格式输出结构化摘要",
    )
    import_l2.set_defaults(func=cmd_symbols_import_l2)

    extract_l1 = symbols_sub.add_parser(
        "extract-l1-rn",
        help="blind-extract React Native main.jsbundle identifiers from an unencrypted IPA",
    )
    extract_l1.add_argument("app_name", help="app id / 目录名")
    extract_l1.add_argument(
        "--ipa",
        required=True,
        type=common.Path,
        help="未加密 React Native IPA 路径",
    )
    extract_l1.add_argument(
        "--build-id",
        default=None,
        help="显式 Build ID（省略时按 IPA SHA-256 派生 bld-<sha[:12]>）",
    )
    extract_l1.add_argument(
        "--default-screen-id",
        default="UNASSIGNED",
        help="提取候选默认归属的 screen_id（默认 UNASSIGNED）",
    )
    extract_l1.add_argument(
        "--symbol-prefix",
        default="ctrl.rn",
        help="派生逻辑 symbol_id 前缀（默认 ctrl.rn）",
    )
    extract_l1.add_argument(
        "--output-root",
        type=common.Path,
        default=None,
        help="输出资产根目录（默认写入当前 App 资产根 apps/<app>/symbols/）",
    )
    extract_l1.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖同制品摘要下内容有变更的既有符号文件",
    )
    extract_l1.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="以 JSON 格式输出结构化摘要",
    )
    extract_l1.set_defaults(func=cmd_symbols_extract_l1_rn)
