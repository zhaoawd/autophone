"""Phase 2 regression engine (docs/phase2-regression.md §十 M1).

Layers:
  registry.py  — @case decorator + CASE_SCRIPTS registry (§10.2)
  actions.py   — CaseContext: tap_text / wait_text / assert_* (§10.3)
  runner.py    — gate → run step-script → assert → classify (§10.4)
  report.py    — result.json + report.md + 失败帧 (§10.5)
"""
