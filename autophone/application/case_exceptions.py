"""Unified derived Exception view + thin persisted disposition overlay (ADR-0003).

An **Exception** is anything blocking a case from *becoming* or *staying* a trustworthy
active asset. It is a value **derived fresh** from live state every time — never a stored
ticket asserting brokenness (a ticket drifts out of sync; a green case can still carry an
open "broken" record). Three kinds are unified into one derived set with one review surface:

  - **triage**  — `automation == ui_ready` but the case has no recipe → misconfigured.
  - **failure** — the case's most recent regression run ended non-green (failed/error/blocked).
  - **drift**   — the discrimination witness is stale / toothless / unproven on this build
                  (the soft-hold dimension already computed by `promotion.discrimination_status`).
  - **twin**    — a discrimination-twin cross-validation *disagreement* (ADR-0002), per pair.
  - **extraction** — a demand-pull (ADR-0004): active `needs_fixture` cases grouped by the
                  precondition they need, surfaced only when **no operation provides** that
                  state (declared, never fuzzy-matched). The count = how many cases one
                  extraction would unblock, so the demand ranks itself.

The only thing persisted is a thin **disposition overlay**: the human's stance
(acknowledged / snoozed / triaged / wontfix) keyed to a *stable* signature
`(case_id, kind, signature)`. The overlay **auto-clears**: a stored stance is only ever
surfaced while its exception still derives, and is pruned the next time the overlay is
written. The signature is deliberately coarse (the failure *kind*, the drift *state*) so it
stays stable across refreshes of the same condition, yet a *materially different* condition
(failed→error, stale→toothless) is a new exception whose old stance no longer applies.

The derivation functions remain pure: they take plain data + small callables and are unit
testable without a VM. ``ExceptionView`` is the cohesive read/write boundary that assembles
those facts for one app; transport layers only translate its result and errors.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from autophone.util import now_iso, read_json, write_json_atomic

KINDS = ("triage", "failure", "drift", "twin", "extraction")
STANCES = ("acknowledged", "snoozed", "triaged", "wontfix")

# 非绿的 per-case run 裁决（run-result.v1 cases[].status 的失败子集）。passed/pending/skipped
# 不是 exception；blocked 也算（被前置挡住，需人处理）。
_FAILURE_STATUSES = {"failed", "error", "blocked"}
# drift kind 只对这些 discrimination_status state 出告警（proven/not_required 不是 exception）。
_DRIFT_STATES = {"stale", "toothless", "witness_imprecise", "no_green_baseline", "inconclusive", "unproven"}


def overlay_key(case_id: str, kind: str, signature: str) -> str:
    """稳定叠加层主键。signature 故意取「失败种类 / 漂移态」这种粗粒度——同一条件反复 derive
    出同一个 key（stance 黏住），而**实质不同**的条件（failed→error、stale→toothless）是新
    exception、新 key，旧 stance 自动失配（不再 mute）。"""
    return f"{case_id}|{kind}|{signature}"


def _iso_now() -> str:
    return now_iso()


def _naive(dt: datetime) -> datetime:
    """丢掉 tzinfo 统一成 naive 比较——`now_iso()` 带本地偏移，但人手填的 until 常是 date-only
    （naive）；混比会抛 TypeError。snooze 到期是粗粒度判断，按本地壁钟比足够。"""
    return dt.replace(tzinfo=None)


def _expired(until: Any, now_iso_str: str) -> bool:
    """snooze/wontfix 的 until 是否已过期（到点即重新 surface）。解析失败按未过期处理（fail-soft，
    宁可多 nag 一次也不静默吞掉一条 exception）。"""
    if not until:
        return False
    try:
        return _naive(datetime.fromisoformat(str(until))) <= _naive(datetime.fromisoformat(now_iso_str))
    except ValueError:
        return False


def _apply_disposition(
    key: str,
    dispositions: dict[str, dict[str, Any]],
    now_iso_str: str,
) -> dict[str, Any] | None:
    """取该 exception 的当前生效 stance。snooze/wontfix 到期 → 视作无 stance（重新浮现）。"""
    d = dispositions.get(key)
    if not d:
        return None
    stance = d.get("stance")
    if stance in ("snoozed", "wontfix") and _expired(d.get("until"), now_iso_str):
        return None
    return d


def derive_exceptions(
    cases: list[dict[str, Any]],
    *,
    case_verdicts: dict[str, dict[str, Any]] | None = None,
    has_recipe: Callable[[str], bool] | None = None,
    discrimination_of: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    dispositions: dict[str, dict[str, Any]] | None = None,
    now_iso_str: str | None = None,
) -> list[dict[str, Any]]:
    """从 live 状态派生三类 exception 的并集，并贴上当前生效的 disposition（ADR-0003）。

    纯函数：所有外部事实经参数注入——
      - `case_verdicts`: {case_id -> {status, run_id, ended_at}}，各用例**最近一次** run 裁决；
      - `has_recipe(case_id) -> bool`: 该用例是否已有 recipe（triage kind 判 ui_ready 是否名实相符）；
      - `discrimination_of(case) -> {state,...}|None`: 该用例的区分力派生态（drift kind 复用
        `promotion.discrimination_status`；返回 None 表示该维度不适用，不出 drift exception）；
      - `dispositions`: 叠加层（`overlay_key -> {stance,...}`）。

    只对 **active** 用例派生（draft 还在建、deprecated 已退役，都不是「在用资产的信任阻塞」）。
    """
    case_verdicts = case_verdicts or {}
    dispositions = dispositions or {}
    now_iso_str = now_iso_str or _iso_now()

    out: list[dict[str, Any]] = []
    for case in cases:
        if case.get("status") != "active":
            continue
        cid = str(case.get("id") or "")
        item = str(case.get("item") or "")
        automation = str(case.get("automation") or "")

        # --- triage：声明 ui_ready 却无 recipe（名实不符）---
        if automation == "ui_ready" and has_recipe is not None and not has_recipe(cid):
            out.append(_build(cid, item, "triage", "ui_ready_no_recipe",
                              "声明 ui_ready 但缺 recipe——无法端到端执行",
                              {"automation": automation}, dispositions, now_iso_str))

        # --- failure：最近一次 run 非绿 ---
        verdict = case_verdicts.get(cid)
        if verdict and str(verdict.get("status")) in _FAILURE_STATUSES:
            st = str(verdict.get("status"))
            out.append(_build(cid, item, "failure", st,
                              f"最近一次回归裁决为 {st}",
                              {"status": st, "run_id": verdict.get("run_id"),
                               "ended_at": verdict.get("ended_at")},
                              dispositions, now_iso_str))

        # --- drift：区分力 soft-hold 非 proven ---
        if discrimination_of is not None:
            disc = discrimination_of(case)
            state = (disc or {}).get("state")
            if state in _DRIFT_STATES:
                out.append(_build(cid, item, "drift", str(state),
                                  f"区分力 {state}：当前 build 未复证判定面有牙",
                                  {"discrimination": disc}, dispositions, now_iso_str))
    return out


def _build(
    case_id: str,
    item: str,
    kind: str,
    signature: str,
    summary: str,
    detail: dict[str, Any],
    dispositions: dict[str, dict[str, Any]],
    now_iso_str: str,
) -> dict[str, Any]:
    key = overlay_key(case_id, kind, signature)
    disposition = _apply_disposition(key, dispositions, now_iso_str)
    return {
        "key": key,
        "case_id": case_id,
        "item": item,
        "kind": kind,
        "signature": signature,
        "summary": summary,
        "detail": detail,
        "disposition": disposition,
        "muted": disposition is not None,
    }


def derive_twin_exceptions(
    validations: list[dict[str, Any]],
    *,
    case_items: dict[str, str] | None = None,
    dispositions: dict[str, dict[str, Any]] | None = None,
    now_iso_str: str | None = None,
) -> list[dict[str, Any]]:
    """把 twin 交叉验证结果里 **disagree** 的那些升成派生 exception（ADR-0002 的 kind 落到 0003 面）。

    `validations` 是 `twins.cross_validate` 的结果列表。只有 disagree 出 exception——agree/
    agree_no_crossing/inconclusive 都不是矛盾，不告警。每对一条「针对 pair 的单一 exception」
    （ADR-0002），挂在 a.case_id 上，signature 编入 dimension + partner（不同对/不同维 = 不同
    exception → 旧 stance 自动失配）。**纯派生，不持久化图、不自动 quarantine partner**（仍 deferred）。
    """
    case_items = case_items or {}
    dispositions = dispositions or {}
    now_iso_str = now_iso_str or _iso_now()
    out: list[dict[str, Any]] = []
    for v in validations:
        if v.get("state") != "disagree":
            continue
        a_cid = str((v.get("a") or {}).get("case_id") or "")
        b_cid = str((v.get("b") or {}).get("case_id") or "")
        dim = str(v.get("dimension") or "")
        reason = str(v.get("reason") or "")
        out.append(_build(
            a_cid, case_items.get(a_cid, ""), "twin", f"{dim}:{b_cid}",
            f"twin 失配（与 {b_cid}，{dim}）：{reason}",
            {"pair": [a_cid, b_cid], "dimension": dim, "poles": v.get("poles"), "reason": reason},
            dispositions, now_iso_str))
    return out


_DEMAND_RE = re.compile(r"[「『]([^」』]+)[」』]")


def fixture_demand_token(case: dict[str, Any]) -> str:
    """一条 needs_fixture 用例需要的预置条件 token。取 `automation_reason` 里「」/『』内的关键词
    （from_excel 启发式已把命中关键词括进来）；没有括号则退回整条 reason 去空白，再空则「未分类」。
    fail-soft：永远给得出一个稳定 token（宁可粗也不丢需求）。"""
    reason = str(case.get("automation_reason") or "")
    m = _DEMAND_RE.search(reason)
    if m:
        return m.group(1).strip()
    return reason.strip() or "未分类"


def derive_extraction_exceptions(
    cases: list[dict[str, Any]],
    *,
    demand_providers: dict[str, Any] | None = None,
    dispositions: dict[str, dict[str, Any]] | None = None,
    now_iso_str: str | None = None,
) -> list[dict[str, Any]]:
    """把「转换被预置态卡住」聚成 **抽取需求** exception（ADR-0004 demand-pull，落到 0003 面）。

    转换需要某个预置态、而**没有任何 operation 能把设备带到该态**时——这条 `needs_fixture` 用例就是
    一个「抽取需求」（其*解法是抽取一个 operation*）。按需求 token 分组：一个 token 只要被 **某个已
    登记的 operation 供给**（`demand_providers[token]` 非空）就**不出** exception——那只是没接线
    （triage 范畴），不是抽取需求。token→provider 是**人工登记**的（拒绝 fuzzy 自动匹配，沿用 twins
    `dimensions.yaml` 同等纪律）；`demand_providers` 缺省/空 ⇒ 一切 token 皆未供给（全部浮现）。

    每个未供给 token 出**一条** exception，挂在最小成员 case_id 上（稳定代表），detail 带全部成员
    case_id + 计数——计数 = 抽取这一个 fixture 能解锁多少条用例，让需求**自己排出优先级**。
    **纯派生**：不建抽取队列、不自动起草 operation、不动语料挖掘（皆 deferred）。

    只看 **active** 且 `automation == needs_fixture` 的用例（draft 还在建、其他自动化档不是预置态阻塞）。
    """
    demand_providers = demand_providers or {}
    dispositions = dispositions or {}
    now_iso_str = now_iso_str or _iso_now()

    # token -> 有序去重的成员 case_id 列表（按出现序，稳定）。
    groups: dict[str, list[str]] = {}
    items: dict[str, str] = {}  # 代表 case_id -> item
    for case in cases:
        if case.get("status") != "active" or str(case.get("automation") or "") != "needs_fixture":
            continue
        token = fixture_demand_token(case)
        cid = str(case.get("id") or "")
        groups.setdefault(token, [])
        if cid and cid not in groups[token]:
            groups[token].append(cid)
            items[cid] = str(case.get("item") or "")

    out: list[dict[str, Any]] = []
    for token, members in groups.items():
        if not members:
            continue
        if demand_providers.get(token):  # 已有操作供给 → 接线问题，不是抽取需求
            continue
        rep = min(members)  # 稳定代表（最小 R 号一般最先落库、最不易先离队）
        n = len(members)
        out.append(_build(
            rep, items.get(rep, ""), "extraction", token,
            f"抽取需求「{token}」：{n} 条用例需要此预置态，暂无操作供给",
            {"demand": token, "cases": members, "count": n}, dispositions, now_iso_str))
    return out


# --------------------------------------------------------------------------------------
# disposition overlay 持久化（薄薄一层；唯一落库的东西）
# --------------------------------------------------------------------------------------


def load_overlay(path: Path) -> dict[str, dict[str, Any]]:
    """读叠加层（缺文件 → 空）。"""
    return read_json(path) or {}


def prune_overlay(
    overlay: dict[str, dict[str, Any]],
    live_keys: set[str],
) -> dict[str, dict[str, Any]]:
    """剔掉所有底层 exception 已消失（key 不在 live_keys）的 stance——ADR-0003「派生条件消失即
    auto-clear」。返回新 dict（不原地改）。"""
    return {k: v for k, v in overlay.items() if k in live_keys}


def set_disposition(
    path: Path,
    *,
    case_id: str,
    kind: str,
    signature: str,
    stance: str,
    note: str | None = None,
    until: str | None = None,
    target: str | None = None,
    by: str | None = None,
    live_keys: set[str] | None = None,
    now_iso_str: str | None = None,
) -> dict[str, Any]:
    """记一条人的 stance 到叠加层。fail-closed：

      - stance 必须 ∈ STANCES；
      - 若传了 `live_keys`，该 key 必须当前**确实派生出 exception**（不能给不存在的 exception
        下 disposition）——否则 ValueError；
    写盘时顺手 prune 掉所有已失配的旧 stance（auto-clear）。返回这条 stance 记录。
    """
    if stance not in STANCES:
        raise ValueError(f"invalid stance {stance!r}: expected one of {STANCES}")
    if kind not in KINDS:
        raise ValueError(f"invalid kind {kind!r}: expected one of {KINDS}")
    key = overlay_key(case_id, kind, signature)
    if live_keys is not None and key not in live_keys:
        raise ValueError(f"no live exception for {key!r}: cannot disposition a non-derived condition")

    overlay = load_overlay(path)
    record = {
        "case_id": case_id,
        "kind": kind,
        "signature": signature,
        "stance": stance,
        "at": now_iso_str or _iso_now(),
    }
    if note:
        record["note"] = note
    if until:
        record["until"] = until
    if target:
        record["target"] = target
    if by:
        record["by"] = by
    overlay[key] = record
    if live_keys is not None:
        overlay = prune_overlay(overlay, live_keys | {key})
    write_json_atomic(path, overlay)
    return record


def clear_disposition(
    path: Path,
    *,
    case_id: str,
    kind: str,
    signature: str,
    live_keys: set[str] | None = None,
) -> bool:
    """撤掉一条 stance。返回是否真的删到了。顺手 prune 失配项。"""
    key = overlay_key(case_id, kind, signature)
    overlay = load_overlay(path)
    existed = key in overlay
    overlay.pop(key, None)
    if live_keys is not None:
        overlay = prune_overlay(overlay, live_keys)
    write_json_atomic(path, overlay)
    return existed


class ExceptionNotLive(ValueError):
    """A disposition target no longer exists in the freshly derived view."""


def _derive_twin_validations(profile: Any, rows: list[dict[str, Any]], negctl_root: Path) -> list[dict[str, Any]]:
    """Derive cross-validation facts from active cases and all available negctl evidence."""
    from autophone.experiments import twins

    vocab_path = profile.discrimination_vocab_path()
    if not vocab_path.exists():
        return []
    vocab = twins.load_vocabulary(vocab_path)
    signatures = twins.signatures_for_active_cases(rows, profile.recipes_dir(), vocab)
    pairs = twins.find_twins(signatures)
    if not pairs:
        return []
    probes: list[dict[str, Any]] = []
    if negctl_root.exists():
        for artifact in sorted(negctl_root.glob("*/artifact.json")):
            try:
                probes.extend((json.loads(artifact.read_text(encoding="utf-8")) or {}).get("probes") or [])
            except (OSError, json.JSONDecodeError):
                continue
    evidence = twins.evidence_from_probes(probes, vocab)
    return [twins.cross_validate(pair, evidence, vocab) for pair in pairs]


def _load_demand_providers(path: Path) -> dict[str, object]:
    """Load the declared fixture supply map; malformed registries fail closed."""
    if not path.exists():
        return {}
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    demands = data.get("demands")
    if not isinstance(demands, dict):
        raise ValueError(f"fixtures registry {path} 缺合法 `demands:` 映射")
    return demands


class ExceptionView:
    """Current derived exceptions plus their thin human disposition overlay.

    The view owns the complete live-state assembly seam.  It persists only a
    disposition; exceptions themselves are recomputed on every operation.
    """

    def __init__(self, profile: Any, *, catalog: Any, negctl_root: Path) -> None:
        self.profile = profile
        self.catalog = catalog
        self.negctl_root = negctl_root

    def current(
        self,
        *,
        kind: str | None = None,
        include_muted: bool = True,
        experimental: bool = False,
    ) -> dict[str, Any]:
        from autophone.assets.promotion import (
            case_requires_discrimination,
            discrimination_status,
            load_discrimination_proof,
        )
        from autophone.assets.promotion_identity import (
            current_artifact_content_digest,
            current_discrimination_context,
        )
        # Intentional late dependency: ExceptionView owns live case assembly;
        # the derivation helpers above remain independent of tools.phase2.
        from autophone.execution.engine.cases_loader import load_cases

        rows = [case for case in load_cases(self.profile.cases_path()) if case.get("status") == "active"]
        verdicts = self.catalog.case_verdicts(self.profile.app)
        artifact_digest = current_artifact_content_digest(self.profile)

        def discrimination_of(case: dict[str, Any]) -> dict[str, Any] | None:
            if not self.profile.require_discrimination or not case_requires_discrimination(case):
                return None
            case_id = str(case.get("id"))
            disc_ctx = current_discrimination_context(
                self.profile,
                case_id=case_id,
                current_artifact_content_digest=artifact_digest,
            )
            proof = load_discrimination_proof(
                self.negctl_root,
                case_id=case_id,
                app=self.profile.app,
                preferred_build_label=self.profile.build_label,
                **disc_ctx,
            )
            return discrimination_status(
                required=True,
                proof=proof,
                current_build_label=self.profile.build_label,
                **disc_ctx,
            )

        overlay = load_overlay(self.profile.dispositions_path())
        items = derive_exceptions(
            rows,
            case_verdicts=verdicts,
            has_recipe=self.profile.has_recipe,
            discrimination_of=discrimination_of,
            dispositions=overlay,
        )
        if experimental or kind in ("twin", "extraction"):
            validations = _derive_twin_validations(self.profile, rows, self.negctl_root)
            items += derive_twin_exceptions(
                validations,
                case_items={str(case.get("id")): str(case.get("item") or "") for case in rows},
                dispositions=overlay,
            )
            items += derive_extraction_exceptions(
                rows,
                demand_providers=_load_demand_providers(self.profile.fixtures_path()),
                dispositions=overlay,
            )
        if kind:
            items = [item for item in items if item.get("kind") == kind]
        needs_attention = sum(1 for item in items if not item.get("muted"))
        if not include_muted:
            items = [item for item in items if not item.get("muted")]
        return {
            "app": self.profile.app,
            "build_label": self.profile.build_label,
            "count": len(items),
            "needs_attention": needs_attention,
            "exceptions": items,
        }

    def set_disposition(
        self,
        *,
        case_id: str,
        kind: str,
        signature: str,
        stance: str,
        note: str | None = None,
        until: str | None = None,
        target: str | None = None,
        by: str | None = None,
    ) -> dict[str, Any]:
        live_keys = self._live_keys()
        if stance not in STANCES:
            raise ValueError(f"invalid stance {stance!r}: expected one of {STANCES}")
        if kind not in KINDS:
            raise ValueError(f"invalid kind {kind!r}: expected one of {KINDS}")
        key = overlay_key(case_id, kind, signature)
        if key not in live_keys:
            raise ExceptionNotLive(f"no live exception for {key!r}: cannot disposition a non-derived condition")
        return set_disposition(
            self.profile.dispositions_path(),
            case_id=case_id,
            kind=kind,
            signature=signature,
            stance=stance,
            note=note,
            until=until,
            target=target,
            by=by,
            live_keys=live_keys,
        )

    def clear_disposition(self, *, case_id: str, kind: str, signature: str) -> bool:
        return clear_disposition(
            self.profile.dispositions_path(),
            case_id=case_id,
            kind=kind,
            signature=signature,
            live_keys=self._live_keys(),
        )

    def _live_keys(self) -> set[str]:
        return {
            item["key"]
            for item in self.current(experimental=True)["exceptions"]
        }
