"""Bind a device marker to locally verified artifact bytes and source identity."""
from __future__ import annotations

import hashlib
import io
import json
import plistlib
import re
from pathlib import Path
from typing import Any, Mapping
import zipfile

from autophone.packages import store
from autophone.packages.model import BuildSelector

MARKER = 'autophone-ui-eval-build-marker.json'
MARKER_SCHEMA_VERSION = 'ui-eval-installed-build-marker/v1'
# 与 autophone.workflow.requirement.build_receipt.BUILD_RECEIPT_SCHEMA_VERSION 同值。
# 此处写字面量而不 import：`autophone.packages` 不依赖 `autophone.workflow`。
RELEASE_BUILD_RECEIPT_SCHEMA_VERSION = 'ui-eval-release-build-receipt/v2'

_SHA256 = re.compile(r'[0-9a-f]{64}')
_GIT_COMMIT_SHA = re.compile(r'[0-9a-f]{40}')


def _release_build_receipt(build: Mapping[str, Any],
                           build_receipt: Mapping[str, Any] | str | Path | None,
                           ) -> Mapping[str, Any] | None:
    """解析一份可选的 Release Build Receipt 文档；缺失或形态不符一律返回 ``None``。

    回执不是本判据的前提：Package Store 的 BuildRecord 上没有回执指针，轮次侧的
    ``identity.build.build_receipt``（``iterations.py``）也只在建单方传入回执时才写。
    因此这里只做「存在则校验」，不把回执变成硬前提。
    """

    candidate: Any = build_receipt if build_receipt is not None else build.get('build_receipt')
    if isinstance(candidate, (str, Path)):
        path = Path(candidate)
        if not path.is_file():
            return None
        try:
            candidate = json.loads(path.read_bytes())
        except (OSError, ValueError):
            return None
    if not isinstance(candidate, Mapping):
        return None
    if candidate.get('schema_version') != RELEASE_BUILD_RECEIPT_SCHEMA_VERSION:
        return None
    return candidate


def _verified_artifact(build: Mapping[str, Any], *, root: Path | None,
                       code_root: Path | None) -> tuple[Any, Any, bytes, Any]:
    """解析该 Build 并读取包仓库中的 IPA 字节；本机复算 sha256 必须等于登记值。"""

    app = str(build['app'])
    registry = store.load_registry(app, root=root or store.ROOT, code_root=code_root)
    sha = build.get('artifact_sha256') or build.get('sha256')
    if not sha:
        raise ValueError('artifact identity requires artifact_sha256')
    ref = registry.resolve(BuildSelector(build_id=build.get('build_id'),
                                        slot=build.get('slot'), artifact_sha256=sha))
    if (ref.artifact_sha256 != sha or (build.get('slot') and ref.slot != build['slot'])):
        raise ValueError('artifact identity mismatch')
    record = registry.build(ref.build_id)
    path = store._stored_build_path(app, record, root=root or store.ROOT, code_root=code_root)
    artifact = path.read_bytes()
    if hashlib.sha256(artifact).hexdigest() != sha:
        raise ValueError('artifact_sha256 mismatch')
    return ref, record, artifact, registry


def _ipa_info_plist(artifact: bytes) -> bytes:
    """IPA 内唯一一份 ``Payload/<App>.app/Info.plist`` 的原始字节；缺失或多于一份时抛出异常。"""

    with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
        entries = [entry for entry in archive.infolist()
                   if len(entry.filename.split('/')) == 3
                   and entry.filename.startswith('Payload/')
                   and entry.filename.split('/')[1].endswith('.app')
                   and entry.filename.split('/')[-1] == 'Info.plist']
        if len(entries) != 1:
            raise ValueError('Info.plist missing or ambiguous in IPA')
        return archive.read(entries[0])


def expected_info_plist_sha256(build: Mapping[str, Any], *, root: Path | None = None,
                               code_root: Path | None = None) -> str:
    """该 Build 的 IPA 内 ``Payload/<App>.app/Info.plist`` 字节的 sha256（BL-241）。

    只读：本机复算 IPA sha256 等于登记的 ``artifact_sha256`` 后才取值，不写回 Build Record。
    Info.plist 缺失或多于一份时抛出异常。
    """

    _ref, _record, artifact, _registry = _verified_artifact(build, root=root, code_root=code_root)
    return hashlib.sha256(_ipa_info_plist(artifact)).hexdigest()


def _plist_text(info: Mapping[str, Any], key: str) -> str | None:
    value = info.get(key)
    return value if isinstance(value, str) and value else None


def info_plist_basis(build: Mapping[str, Any], *, root: Path | None = None,
                     code_root: Path | None = None) -> dict[str, Any]:
    """BL-241 第二步：``info_plist`` 粒度判定所需的宿主侧基准，只读。

    - 基准 IPA 必须本机复算 sha256 等于 ``artifact_sha256``，否则抛出异常（调用方记原因，
      不产出 ``match``）；
    - ``source_revision_kind`` 取自 Package Store 解析出的 Build Record；
    - ``ipa_short_version``/``ipa_build_version`` 取自复算后 IPA 的 Info.plist
      （``CFBundleShortVersionString``/``CFBundleVersion``），只作比较基准的只读派生，
      不写回 Build Record 或安装回执；
    - 只对 ``kind=unknown`` 扫描唯一性：同一 App 的其他 Build 中，IPA 可复算的逐一比较
      Info.plist 字节，相同则 ``ambiguous``；IPA 缺失或无法复算的，Build Record 登记的
      ``bundle_id`` 或 Version Slot ``short_version`` 与基准 IPA 不同则不可能字节相同（二者都
      写在 Info.plist 中），否则记入 ``unverified_build_ids``，结果为 ``unverified``。
      其他 kind 记 ``not_evaluated``，不读取其他 IPA。
    """

    ref, _record, artifact, registry = _verified_artifact(build, root=root, code_root=code_root)
    raw = _ipa_info_plist(artifact)
    digest = hashlib.sha256(raw).hexdigest()
    info = plistlib.loads(raw)
    if not isinstance(info, Mapping):
        raise ValueError('Info.plist is not a dictionary')
    kind = ref.source_revision.to_dict().get('kind')
    short_version = _plist_text(info, 'CFBundleShortVersionString')
    bundle_id = _plist_text(info, 'CFBundleIdentifier')
    same: list[str] = []
    unverified: list[str] = []
    uniqueness = 'not_evaluated'
    if kind == 'unknown':
        app = str(build['app'])
        for other in registry.builds:
            if other.build_id == ref.build_id:
                continue
            try:
                path = store._stored_build_path(app, other, root=root or store.ROOT,
                                                code_root=code_root)
                data = path.read_bytes()
                if hashlib.sha256(data).hexdigest() != other.sha256:
                    raise ValueError('artifact_sha256 mismatch')
                other_raw = _ipa_info_plist(data)
            except (OSError, ValueError, zipfile.BadZipFile, store.PackageError):
                other_short = registry.short_version_of(other)
                if ((other.bundle_id and bundle_id and other.bundle_id != bundle_id)
                        or (other_short and short_version and other_short != short_version)):
                    continue
                unverified.append(other.build_id)
                continue
            if hashlib.sha256(other_raw).hexdigest() == digest:
                same.append(other.build_id)
        uniqueness = 'ambiguous' if same else 'unverified' if unverified else 'unique'
    return {
        'build_id': ref.build_id,
        'source_revision_kind': kind,
        'info_plist_sha256': digest,
        'ipa_short_version': short_version,
        'ipa_build_version': _plist_text(info, 'CFBundleVersion'),
        'uniqueness': uniqueness,
        'same_info_plist_build_ids': sorted(same),
        'unverified_build_ids': sorted(unverified),
        'reason': None,
    }


def _stored_marker(build: Mapping[str, Any], *, root: Path | None,
                   code_root: Path | None) -> tuple[Any, Any, bytes | None]:
    """读取包仓库中该 Build 的 IPA，核对字节 sha256 后取出 marker；IPA 不含 marker 时为 ``None``。"""

    app = str(build['app'])
    registry = store.load_registry(app, root=root or store.ROOT, code_root=code_root)
    sha = build.get('artifact_sha256') or build.get('sha256')
    if not sha:
        raise ValueError('build marker requires artifact_sha256')
    ref = registry.resolve(BuildSelector(build_id=build.get('build_id'),
                                        slot=build.get('slot'), artifact_sha256=sha))
    if (ref.artifact_sha256 != sha or (build.get('slot') and ref.slot != build['slot'])):
        raise ValueError('build marker artifact identity mismatch')
    record = registry.build(ref.build_id)
    path = store._stored_build_path(app, record, root=root or store.ROOT, code_root=code_root)
    # Hash and parse the same bytes, including when another process replaces the blob.
    artifact = path.read_bytes()
    if hashlib.sha256(artifact).hexdigest() != sha:
        raise ValueError('build marker artifact_sha256 mismatch')
    with zipfile.ZipFile(io.BytesIO(artifact)) as archive:
        entries = [entry for entry in archive.infolist()
                   if len(entry.filename.split('/')) == 3
                   and entry.filename.startswith('Payload/')
                   and entry.filename.split('/')[1].endswith('.app')
                   and entry.filename.split('/')[-1] == MARKER]
        if len(entries) > 1:
            raise ValueError('build marker missing or ambiguous in IPA')
        raw = archive.read(entries[0]) if entries else None
    return ref, record, raw


def stored_build_marker(build: Mapping[str, Any], *, root: Path | None = None,
                        code_root: Path | None = None) -> bytes | None:
    """返回包仓库中该 Build 的 IPA 内 marker 原始字节；IPA 不含 marker 时返回 ``None``。

    只核对产物身份（registry 解析、本机复算 IPA sha256、marker 唯一），不核对源码身份，
    供 Provisioning 判断设备上已装的 App 是否就是这份 IPA（BL-237）。registry 无法解析、
    字节不符或 marker 多于一份时抛出异常。
    """

    return _stored_marker(build, root=root, code_root=code_root)[2]


def expected_build_marker(build: Mapping[str, Any], *, root: Path | None = None,
                          code_root: Path | None = None,
                          build_receipt: Mapping[str, Any] | str | Path | None = None,
                          ) -> bytes:
    """返回该 Build 的 IPA 内 marker 原始字节，并核对它确实属于这次点名的产物。

    两种源码登记形态各用自己可核对的字段（口径与
    ``autophone.workflow.requirement.build_receipt._assert_receipt_source_identity``
    一致，BL-146）：

    ``source_tree``：BuildRecord 记的是源码树摘要，与 marker 的
    ``source_tree_sha256`` 逐位对照。这是源码层面的直接对照。

    ``git_commit``：BuildRecord 记的是 commit，而 marker 不复述 commit——这是
    marker 生产者的既有形态，见
    ``autophone/workflow/ui_verification/ui_verification_eval_target_live.py``
    的 marker 写入处（固定五个键 ``schema_version``/``target_id``/``bundle_id``/
    ``source_tree_sha256``/``app_tree_sha256``）与同文件 ``_embedded_eval_build_marker``
    的精确键集合校验（多一个 ``source_revision`` 会被那条判据拒绝）。因此这里
    不要求 marker 复述 commit，只要求该 BuildRecord 是完整的 git 身份——
    ``state`` 为 ``clean``、``commit_sha`` 为 40 位十六进制。

    绑定由三处承担，不因此减弱：``build_id`` 本身就把 ``source_revision`` 与
    ``artifact_sha256`` 一起哈希（``model.compute_build_id``）；本函数在本机复算
    IPA 字节的 sha256 必须等于 registry 记录；调用方（``state_driver_runtime``）
    再要求设备侧 marker 字节与本函数返回值逐位相等。commit 一侧另有一条独立对照：
    ``autophone.workflow.requirement.iterations._assert_source_revision_agrees``
    要求轮次声明的 ``source_commit`` 等于该 BuildRecord 的 commit。

    其他 kind（``unknown``、缺失、字段不合法）一律拒绝。

    ``build_receipt`` 是可选加强：传入（或 ``build['build_receipt']`` 内联）一份
    ``ui-eval-release-build-receipt/v2`` 回执，且其 ``build_id`` 与 ``ipa.sha256``
    都等于本次点名的构建时，额外要求 marker 的 ``source_tree_sha256`` 等于回执的
    同名字段。回执缺失不失败。
    """

    sha = build.get('artifact_sha256') or build.get('sha256')
    ref, record, raw = _stored_marker(build, root=root, code_root=code_root)
    if raw is None:
        raise ValueError('build marker missing or ambiguous in IPA')
    revision = ref.source_revision.to_dict()
    if build.get('source_revision') is not None and build['source_revision'] != revision:
        raise ValueError('build marker source_revision mismatch')
    marker = json.loads(raw)
    if not isinstance(marker, dict) or marker.get('schema_version') != MARKER_SCHEMA_VERSION:
        raise ValueError('build marker schema invalid')
    for field in ('source_tree_sha256', 'app_tree_sha256'):
        if not _SHA256.fullmatch(str(marker.get(field) or '')):
            raise ValueError(f'build marker {field} invalid')
    if marker.get('bundle_id') != record.bundle_id:
        raise ValueError('build marker bundle_id mismatch')
    if revision['kind'] == 'source_tree':
        if marker.get('source_tree_sha256') != revision['tree_sha256']:
            raise ValueError('build marker source_revision mismatch')
    elif revision['kind'] == 'git_commit':
        if (revision.get('state') != 'clean'
                or not _GIT_COMMIT_SHA.fullmatch(str(revision.get('commit_sha') or ''))):
            raise ValueError('build marker source revision is not a clean git commit')
    else:
        raise ValueError('build marker source_revision not verifiable')
    receipt = _release_build_receipt(build, build_receipt)
    if receipt is not None:
        ipa = receipt.get('ipa')
        ipa_sha = ipa.get('sha256') if isinstance(ipa, Mapping) else None
        if receipt.get('build_id') == ref.build_id and ipa_sha == sha:
            if marker.get('source_tree_sha256') != receipt.get('source_tree_sha256'):
                raise ValueError('build marker source_tree_sha256 differs from the build receipt')
    return raw
