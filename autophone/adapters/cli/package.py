from __future__ import annotations

import argparse

from autophone.adapters.extensions import register_extensions
from autophone.adapters.cli import _common as common


def _source_revision_from_args(args: argparse.Namespace):
    """Build a SourceRevision from the CLI's mutually exclusive source flags.

    Omitting all of them records ``unknown`` — an explicit gap that managed
    delivery refuses to consume, never a value inferred from the current Git
    working tree.
    """
    from autophone.packages.model import SourceRevision

    commit = getattr(args, "commit", None)
    tree = getattr(args, "source_tree_sha256", None)
    base = getattr(args, "base_commit", None)
    if commit and tree:
        raise SystemExit("--commit and --source-tree-sha256 are mutually exclusive")
    if commit:
        if base:
            raise SystemExit("--base-commit only applies to --source-tree-sha256")
        return SourceRevision.git_commit(commit)
    if tree:
        return SourceRevision.source_tree(
            tree, base_commit_sha=base,
            state=getattr(args, "source_state", None) or "dirty",
        )
    if base:
        raise SystemExit("--base-commit requires --source-tree-sha256")
    return None


def cmd_package_add(args: argparse.Namespace) -> int:
    from autophone.packages import store as packages

    result = packages.register_build(
        args.app_name, args.ipa,
        source_revision=_source_revision_from_args(args),
        label=args.label, force=args.force,
        artifact_store_id=args.artifact_store,
        make_candidate=not args.no_candidate,
        retention_class=args.retention_class,
    )
    build_receipt_path = None
    if getattr(args, "build_receipt", None) is not None:
        try:
            build_receipt_path = args.record_build_receipt(args.build_receipt, result)
        except ValueError as exc:
            # 登记已完成且不可撤销；回执写回失败只报告，不伪造成功。
            print(
                f"build {result.ref.build_id} registered, "
                f"but the build receipt was not updated [{getattr(exc, 'code', 'invalid_receipt')}]: {exc}",
                file=common.sys.stderr,
            )
            return 1
    if result.duplicate:
        payload = {
            "skipped": "duplicate",
            "reason": f"相同 source revision + sha256 已登记为 {result.ref.build_id}，未重复存储",
            "existing": result.to_dict(),
        }
    else:
        payload = {"added": result.to_dict()}
    if build_receipt_path is not None:
        payload["build_receipt_path"] = build_receipt_path
    print(common.json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_package_list(args: argparse.Namespace) -> int:
    from autophone.packages import store as packages

    print(common.json.dumps(packages.catalog(args.app_name), ensure_ascii=False, indent=2))
    return 0


def _package_install_application():
    from autophone.application.control_plane import configured_control_plane
    from autophone.application.package_install import PackageInstallApplication
    from autophone.packages import store as packages
    from autophone.rig.observed_state import FileRigObservedState

    return PackageInstallApplication(
        packages,
        configured_control_plane,
        FileRigObservedState.compatibility(),
    )


def cmd_package_install(args: argparse.Namespace) -> int:
    result = _package_install_application().install(
        args.app_name,
        args.slot,
        build_id=getattr(args, "build_id", None),
        verify=not args.no_verify,
        target_rig_id=args.rig_id,
        device=getattr(args, "device", None),
        priority=args.priority,
        idempotency_key=args.idempotency_key,
    )
    key = "assignment" if result.get("assignment_id") else "installed"
    print(common.json.dumps({key: result}, ensure_ascii=False, indent=2))
    return 0


def cmd_package_default(args: argparse.Namespace) -> int:
    from autophone.packages import store as packages

    packages.set_default_slot(args.app_name, args.slot)
    print(common.json.dumps(
        {"app": args.app_name, "default_slot": args.slot},
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def cmd_package_remove(args: argparse.Namespace) -> int:
    from autophone.packages import store as packages

    if getattr(args, "build_id", None):
        entry = packages.remove_build(
            args.app_name, args.build_id,
            policy="force" if args.force else "guarded",
        )
    else:
        entry = packages.remove_package(args.app_name, args.slot)
    print(common.json.dumps({"removed": entry.to_view()}, ensure_ascii=False, indent=2))
    return 0


def cmd_package_promote(args: argparse.Namespace) -> int:
    from autophone.packages import store as packages

    print(common.json.dumps({
        "promoted": packages.promote_candidate(args.app_name, args.build_id),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_package_prune(args: argparse.Namespace) -> int:
    from autophone.packages import store as packages

    print(common.json.dumps({
        "pruned": packages.prune_build_artifact(
            args.app_name, args.build_id,
            policy="force" if args.force else "guarded",
        ),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_package_relocate(args: argparse.Namespace) -> int:
    from autophone.packages import store as packages

    print(common.json.dumps({
        "relocated": packages.relocate_build_artifact(
            args.app_name, args.build_id, args.artifact_store,
        ),
    }, ensure_ascii=False, indent=2))
    return 0


def _migration_exclusions(args: argparse.Namespace) -> list:
    """Build the exclusion decisions this invocation carries.

    A decision has to say who decided, when, and why; the flags therefore
    refuse to invent those values, and a decision file stays the way to record
    a different reason per artifact.
    """
    from autophone.packages import migration as package_migration
    from autophone.util import now_iso

    exclusions = []
    if args.decision_file:
        exclusions.extend(package_migration.load_decision_file(args.decision_file))
    for sha256 in args.exclude_artifact or ():
        if not args.exclusion_reason or not args.decided_by:
            raise package_migration.DecisionError(
                "--exclude-artifact requires --exclusion-reason and --decided-by",
            )
        exclusions.append(package_migration.ArtifactExclusion(
            artifact_sha256=sha256,
            reason=args.exclusion_reason,
            decided_at=args.decided_at or now_iso(),
            decided_by=args.decided_by,
        ))
    return exclusions


def cmd_package_migrate(args: argparse.Namespace) -> int:
    """Preview (default) or apply the v1 → v2 package store migration."""
    from autophone.packages import migration as package_migration

    exclusions = _migration_exclusions(args)
    if not args.apply:
        plan = package_migration.plan(args.app_name, exclusions=exclusions)
        print(common.json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return 0 if plan.clean else 1
    print(common.json.dumps(
        package_migration.apply(args.app_name, exclusions=exclusions),
        ensure_ascii=False, indent=2))
    return 0


def cmd_package_migrate_manifest(args: argparse.Namespace) -> int:
    from autophone.packages import store as packages

    result = packages.migrate_manifest_versions(args.app_name)
    print(common.json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    pkg = sub.add_parser("package", help="ordered IPA store: upload + install builds")
    pkg_sub = pkg.add_subparsers(dest="package_cmd", required=True)
    pkg_add = pkg_sub.add_parser("add", help="ingest an IPA into the host store")
    pkg_add.add_argument("app_name")
    pkg_add.add_argument("ipa")
    pkg_add.add_argument("--label", default=None, help="human label / build name (default: version or filename)")
    pkg_add.add_argument("--force", action="store_true", help="import even if the app is unknown or the IPA bundle id mismatches the profile")
    pkg_add.add_argument("--commit", default=None, help="产生该 Build 的完整产品源码 commit（干净工作树）")
    pkg_add.add_argument("--source-tree-sha256", default=None, help="dirty/generated 源码树摘要")
    pkg_add.add_argument("--base-commit", default=None, help="--source-tree-sha256 的基线 commit")
    pkg_add.add_argument("--source-state", choices=["dirty", "generated"], default=None,
                         help="源码树状态（默认 dirty）")
    pkg_add.add_argument("--artifact-store", default=None,
                         help="产物存储 ID；缺省使用 App Profile 配置，未配置时使用 central；复用已有 Blob 时显式冲突会拒绝入库")
    pkg_add.add_argument("--retention-class",
                         choices=["iteration", "candidate", "accepted", "baseline"],
                         default=None, help="产物保留分类")
    pkg_add.add_argument("--no-candidate", action="store_true",
                         help="只登记迭代 Build，不替换当前 Candidate")
    pkg_add.set_defaults(func=cmd_package_add)
    register_extensions("package", pkg_add, pkg_sub)

    pkg_list = pkg_sub.add_parser("list", help="list Version Slots and their Build history")
    pkg_list.add_argument("app_name")
    pkg_list.set_defaults(func=cmd_package_list)
    pkg_install = pkg_sub.add_parser("install", help="upload + install one Build into the VM (default: installed, else newest candidate)")
    pkg_install.add_argument("app_name")
    pkg_install.add_argument("--slot", default=None, help="version slot；不带 --build-id 时解析该 slot 的 Candidate")
    pkg_install.add_argument("--build-id", default=None, help="精确 Build（自动化调用方必须用这个）")
    pkg_install.add_argument("--no-verify", action="store_true", help="skip the post-install LaunchServices registration check")
    pkg_install.add_argument("--rig-id", default=None, help="target Rig when Control Plane is configured")
    pkg_install.add_argument(
        "--device", default=None,
        help="目标设备（VM 目录名或 rig_id）；控制面已配置且未给 --rig-id 时必填，"
             "按设备解析唯一 Rig 固定安装目标（BL-218）",
    )
    pkg_install.add_argument("--priority", choices=["high", "normal"], default="normal")
    pkg_install.add_argument("--idempotency-key", default=None)
    pkg_install.set_defaults(func=cmd_package_install)
    pkg_default = pkg_sub.add_parser("default", help="set the default slot without changing any Rig")
    pkg_default.add_argument("app_name")
    pkg_default.add_argument("slot")
    pkg_default.set_defaults(func=cmd_package_default)
    pkg_remove = pkg_sub.add_parser("remove", help="drop a slot from the host store")
    pkg_remove.add_argument("app_name")
    pkg_remove.add_argument("slot", nargs="?", default=None, help="slot（兼容入口，删该 slot 的 Candidate）")
    pkg_remove.add_argument("--build-id", default=None, help="精确 Build")
    pkg_remove.add_argument("--force", action="store_true", help="忽略保留闸（installed / candidate / 已接受需求引用）")
    pkg_remove.set_defaults(func=cmd_package_remove)
    pkg_promote = pkg_sub.add_parser("promote", help="将现有 Build 显式晋级为 Version Slot Candidate")
    pkg_promote.add_argument("app_name")
    pkg_promote.add_argument("--build-id", required=True)
    pkg_promote.set_defaults(func=cmd_package_promote)
    pkg_prune = pkg_sub.add_parser("prune", help="删除 IPA 字节并保留 Build Record")
    pkg_prune.add_argument("app_name")
    pkg_prune.add_argument("--build-id", required=True)
    pkg_prune.add_argument("--force", action="store_true", help="忽略 installed/Candidate/accepted/baseline 保留闸")
    pkg_prune.set_defaults(func=cmd_package_prune)
    pkg_relocate = pkg_sub.add_parser("relocate", help="校验并复制 Build 产物到另一个 Artifact Store")
    pkg_relocate.add_argument("app_name")
    pkg_relocate.add_argument("--build-id", required=True)
    pkg_relocate.add_argument("--artifact-store", required=True)
    pkg_relocate.set_defaults(func=cmd_package_relocate)
    pkg_migrate = pkg_sub.add_parser(
        "migrate", help="package store v1 → v2 迁移：默认只读预览，--apply 才写入")
    pkg_migrate.add_argument("app_name")
    pkg_migrate.add_argument("--apply", action="store_true", help="写入 v2 manifest 与 Target 级回执")
    pkg_migrate.add_argument(
        "--exclude-artifact", action="append", metavar="SHA256",
        help="排除裁定：该 Artifact 退出来源冲突门，Source Revision 保持 unknown；可重复")
    pkg_migrate.add_argument(
        "--exclusion-reason", help="--exclude-artifact 的裁定理由，必填")
    pkg_migrate.add_argument(
        "--decided-by", help="--exclude-artifact 的裁定人，必填")
    pkg_migrate.add_argument(
        "--decided-at", help="裁定时间；默认取当前时间")
    pkg_migrate.add_argument(
        "--decision-file", help="裁定记录 JSON：{\"decided_by\",\"decided_at\",\"exclusions\":[…]}")
    pkg_migrate.set_defaults(func=cmd_package_migrate)
    pkg_migrate_manifest = pkg_sub.add_parser(
        "migrate-manifest",
        help="backfill short/build version fields from registered IPA files",
    )
    pkg_migrate_manifest.add_argument("app_name")
    pkg_migrate_manifest.set_defaults(func=cmd_package_migrate_manifest)
