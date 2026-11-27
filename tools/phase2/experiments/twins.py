"""Discrimination twins 实验（窄）：证 **normalized discriminator signature** 能把 R47/R49 这对
「不同操作、断言标记无公共子串」的用例配成 twin，并用 negative-control 已录证据**交叉验证**它们
互为对方的对偶。纯原语已提到平台层 `autophone.experiments.twins`（派生 Exception 面也复用它）；本文件
只剩薄薄的 CLI：吃 negctl 已落的 artifact（runs/negctl/<label>/artifact.json）+ recipe 断言 +
dimensions.yaml，落一份 twins-artifact.json。

  R47 sig = (auth_session, authenticated)   [assert 下班]
  R49 sig = (auth_session, unauthenticated) [assert 账号密码登录]
  → 同 dimension、对偶 pole ⇒ twin（按签名，不按形状）。R49 的 flip→unexpectedly_logged_in 归一化
    落到 authenticated 极 = R47 断言的那一极 ⇒ agree（这对 twin 互相证明）。

ADR-0002（docs/adr/0002-discrimination-twins-and-cross-validation.md）。**不是平台代码、不建图。**

  uv run python3 -m tools.phase2.experiments.twins --build-label uat-2026-06-19,uat-2026-06-19c
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from autophone.paths import CODE_ROOT, STATE_ROOT
# 纯原语全部从平台层复用（实验与派生 Exception 面同一套逻辑，避免两份漂移）。
from autophone.experiments.twins import (  # noqa: F401  (re-export，供测试 import)
    CaseEvidence,
    Dimension,
    Pole,
    Signature,
    TwinPair,
    Vocabulary,
    build_signatures,
    cross_validate,
    discriminator_signature,
    evidence_from_probes,
    find_twins,
    load_vocabulary,
    recipe_discriminator_markers,
    signatures_for_active_cases,
)


def run(build_labels: list[str], *, case_ids: list[str], app_dir: Path) -> dict[str, Any]:
    """纯编排：算签名 → 配 twin → 读 negctl 证据交叉验证 → 返回 artifact dict。

    `build_labels` 可给多个（同一 app build 的迭代 negctl run）；证据按列表顺序合并（probes 串接），
    让分散在多次 run 的证据拼成完整一对——如 R47 的牙在第一次 run、R49 的 flip 越界 stage 在后一次。
    """
    vocab = load_vocabulary(app_dir / "discrimination" / "dimensions.yaml")
    sigs = build_signatures(case_ids, app_dir / "recipes", vocab)
    pairs = find_twins(sigs)

    probes: list[dict[str, Any]] = []
    sources: list[str] = []
    for label in build_labels:
        negctl_path = STATE_ROOT / "runs" / "negctl" / label / "artifact.json"
        if negctl_path.exists():
            probes.extend((json.loads(negctl_path.read_text(encoding="utf-8")) or {}).get("probes") or [])
            sources.append(str(negctl_path))
    evidence = evidence_from_probes(probes, vocab)
    return {
        "experiment": "discrimination twins (narrow R47/R49)",
        "build_labels": build_labels,
        "adr": "docs/adr/0002-discrimination-twins-and-cross-validation.md",
        "negctl_artifacts": sources,
        "signatures": [s.as_dict() for s in sigs],
        "twins": [cross_validate(p, evidence, vocab) for p in pairs],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="discrimination twins 实验（窄；R47/R49，纯后处理 negctl artifact）")
    ap.add_argument("--build-label", required=True,
                    help="读 runs/negctl/<label>/artifact.json 取交叉验证证据；可逗号分隔多个（同 build 的迭代 run，证据合并）")
    ap.add_argument("--cases", default="R47,R49", help="逗号分隔；默认 R47,R49")
    ap.add_argument("--app", required=True, help="App Profile slug")
    args = ap.parse_args(argv)

    app_dir = CODE_ROOT / "apps" / args.app
    case_ids = [c.strip().upper() for c in args.cases.split(",") if c.strip()]
    build_labels = [b.strip() for b in args.build_label.split(",") if b.strip()]
    artifact = run(build_labels, case_ids=case_ids, app_dir=app_dir)

    out_dir = STATE_ROOT / "runs" / "negctl" / build_labels[-1]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "twins-artifact.json"
    out_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"signatures": artifact["signatures"],
                      "twins": [{k: v[k] for k in ("dimension", "poles", "state", "reason")} for v in artifact["twins"]]},
                     ensure_ascii=False, indent=2))
    print(f"\nartifact → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
