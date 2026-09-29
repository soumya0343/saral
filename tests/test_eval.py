"""Eval harness: the suite runs, the safety bar holds, and regressions are detected."""

import pytest

from saral.eval.metrics import summarize
from saral.eval.runner import run_eval
from saral.eval.scenarios import load_scenarios
from saral.eval.schemas import (
    Category,
    EvalReport,
    MetricSummary,
    ScenarioResult,
)
from saral.tools import mock_backend as mb


def setup_function():
    mb._reset_state()


def test_scenarios_load_and_span_categories():
    scenarios = load_scenarios()
    assert len(scenarios) >= 25
    cats = {s.category for s in scenarios}
    required = {Category.INFORMATION, Category.ACTION, Category.COMPLIANCE, Category.ADVERSARIAL}
    assert required <= cats


async def test_run_eval_offline_tier_holds_the_committed_baseline():
    from saral.eval.gate import check, load_baseline

    report = await run_eval(persist=False, save_report=False)
    assert isinstance(report, EvalReport)
    assert report.tier == "offline" and report.judge_model == "rubric"
    s = report.summary
    assert s.n >= 150
    # No metric may drop below the committed baseline (data/eval_baselines/offline.json).
    baseline = load_baseline("offline")
    assert baseline is not None
    assert check(s, baseline) == []
    # Offline judge-vs-human agreement is circular (rubric == labels): never reported.
    assert s.judge_human_agreement is None
    # The reply-level metrics are actually measured (not defaulted).
    assert any("language_match" in r.metrics for r in report.results)
    assert any("turns" in r.metrics for r in report.results)
    assert any("no_false_block" in r.metrics for r in report.results)


def test_regression_detection():
    # Synthesize two summaries: routing drops 1.0 -> 0.8 must be flagged.
    from saral.eval.runner import _regressions

    def mk(routing: float) -> MetricSummary:
        return MetricSummary(
            n=10, pass_rate=1.0, routing_accuracy=routing, tool_sequence_correctness=1.0,
            compliance_block_rate=1.0, groundedness=1.0, resolution_accuracy=1.0,
            cross_lingual_consistency=1.0, latency_p50_ms=1, latency_p95_ms=2,
            cost_per_run_usd=0.0,
        )

    prior = EvalReport(config_version="v0", created_at="t", summary=mk(1.0), results=[])
    regr = _regressions(mk(0.8), prior)
    assert "routing_accuracy" in regr
    assert regr["routing_accuracy"] == pytest.approx(-0.2)


def test_summarize_block_rate_fails_on_miss():
    # A must-block scenario that did NOT block -> block rate < 1.0.
    results = [
        ScenarioResult(
            scenario_id="x", category=Category.ADVERSARIAL, passed=False,
            metrics={"compliance_block": False},
        )
    ]
    summary = summarize(results, load_scenarios()[:0])
    assert summary.compliance_block_rate == 0.0
