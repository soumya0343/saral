"""Regression gate: no eval metric may get worse than its committed baseline.

`data/eval_baselines/<tier>.json` holds the floor for every "higher is better" metric and the
ceiling for false_block_rate. CI runs the offline tier and fails on any drop. When a change
genuinely improves a metric, re-baseline (`make eval-baseline`) and commit the new file, so the
improvement is locked in. Safety targets (100% block, 0% false block) are reported against
their target, not the baseline — the baseline only stops things getting worse.

    python -m saral.eval.gate                 # run offline tier, compare to baseline
    python -m saral.eval.gate --update        # run offline tier, write it as the new baseline
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from saral.eval.schemas import EvalTier, MetricSummary

BASELINE_DIR = Path("data/eval_baselines")
HIGHER_IS_BETTER = [
    "pass_rate",
    "routing_accuracy",
    "tool_sequence_correctness",
    "compliance_block_rate",
    "groundedness",
    "explanation_groundedness",
    "language_match",
    "answer_correctness",
    "faithfulness",
    "multi_turn_success",
    "rm_request_accuracy",
    "resolution_accuracy",
    "cross_lingual_consistency",
]
LOWER_IS_BETTER = ["false_block_rate"]
TARGETS = {"compliance_block_rate": 1.0, "false_block_rate": 0.0}
_EPS = 1e-9


def baseline_path(tier: EvalTier) -> Path:
    return BASELINE_DIR / f"{tier}.json"


def load_baseline(tier: EvalTier) -> dict[str, float] | None:
    p = baseline_path(tier)
    return json.loads(p.read_text())["metrics"] if p.exists() else None


def write_baseline(tier: EvalTier, summary: MetricSummary, n: int) -> Path:
    p = baseline_path(tier)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = summary.model_dump()
    metrics = {k: round(data[k], 4) for k in HIGHER_IS_BETTER + LOWER_IS_BETTER}
    p.write_text(json.dumps({"tier": tier, "n": n, "metrics": metrics}, indent=2) + "\n")
    return p


def check(summary: MetricSummary, baseline: dict[str, float]) -> list[str]:
    """Human-readable failures; empty = gate passes."""
    # Compare at the precision the baseline is stored with (4 dp).
    cur = {k: round(v, 4) if isinstance(v, float) else v for k, v in summary.model_dump().items()}
    fails: list[str] = []
    for k in HIGHER_IS_BETTER:
        if k in baseline and cur[k] < baseline[k] - _EPS:
            fails.append(f"{k} dropped: {cur[k]:.4f} < baseline {baseline[k]:.4f}")
    for k in LOWER_IS_BETTER:
        if k in baseline and cur[k] > baseline[k] + _EPS:
            fails.append(f"{k} rose: {cur[k]:.4f} > baseline {baseline[k]:.4f}")
    return fails


def target_gaps(summary: MetricSummary) -> list[str]:
    cur = summary.model_dump()
    return [
        f"{k} = {cur[k]:.2%} (target {target:.0%})"
        for k, target in TARGETS.items()
        if abs(cur[k] - target) > _EPS
    ]


def main() -> None:
    p = argparse.ArgumentParser(description="Eval regression gate (offline tier)")
    p.add_argument("--update", action="store_true", help="write the run as the new baseline")
    a = p.parse_args()

    from saral.eval.runner import print_report, run_eval

    report = asyncio.run(run_eval(expect_tier="offline", persist=False, save_report=False))
    print_report(report)
    if a.update:
        path = write_baseline("offline", report.summary, report.summary.n)
        print(f"\nbaseline written: {path} — commit it")
        return
    baseline = load_baseline("offline")
    if baseline is None:
        raise SystemExit("no offline baseline: run `make eval-baseline` and commit it")
    for gap in target_gaps(report.summary):
        print(f"  target not met (tracked, not gating): {gap}")
    fails = check(report.summary, baseline)
    if fails:
        raise SystemExit("EVAL GATE FAILED:\n  " + "\n  ".join(fails))
    print("\neval gate: no metric below baseline")


if __name__ == "__main__":
    main()
