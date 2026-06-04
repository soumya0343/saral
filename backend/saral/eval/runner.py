"""Evaluation runner.

Executes every scenario against the pinned config, scores per-metric, summarizes, and
compares against the previous report to surface regressions — a real prompt → eval → deploy
loop. Reports are written as JSON (so the loop works offline) and best-effort to
`eval_results` in Postgres.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from saral.config import get_settings
from saral.eval.judge import Judge
from saral.eval.metrics import actual_outcome, evaluate_scenario, summarize
from saral.eval.scenarios import load_scenarios
from saral.eval.schemas import EvalReport, MetricSummary, Scenario, ScenarioResult
from saral.eval.store import latest_report, write_report
from saral.graph.build import build_graph
from saral.graph.state import RunState
from saral.logging import get_logger

log = get_logger(__name__)

# Metrics where a drop versus the previous version counts as a regression.
_REGRESSION_KEYS = [
    "pass_rate",
    "routing_accuracy",
    "tool_sequence_correctness",
    "compliance_block_rate",
    "groundedness",
    "resolution_accuracy",
    "cross_lingual_consistency",
]


async def _run_one(scenario: Scenario, judge: Judge) -> ScenarioResult:
    graph = build_graph()
    init = RunState(
        run_id=f"eval-{scenario.id}",
        conversation_id=f"eval-{scenario.id}",
        user_id=scenario.user_id,
        raw_message=scenario.message,
    )
    t0 = time.perf_counter()
    state = RunState.model_validate(await graph.ainvoke(init))
    latency_ms = int((time.perf_counter() - t0) * 1000)

    verdict = await judge.evaluate(
        {
            "category": scenario.category.value,
            "expected": scenario.expected.model_dump(),
            "actual": actual_outcome(state),
        }
    )
    return evaluate_scenario(scenario, state, latency_ms, verdict)


def _regressions(current: MetricSummary, prior: EvalReport | None) -> dict[str, float]:
    if prior is None:
        return {}
    out: dict[str, float] = {}
    cur = current.model_dump()
    old = prior.summary.model_dump()
    for key in _REGRESSION_KEYS:
        delta = round(cur[key] - old[key], 4)
        if delta < 0:
            out[key] = delta
    return out


async def run_eval(*, persist: bool = True, save_report: bool = True) -> EvalReport:
    settings = get_settings()
    scenarios = load_scenarios()
    judge = Judge()

    results: list[ScenarioResult] = []
    for scenario in scenarios:
        results.append(await _run_one(scenario, judge))

    summary = summarize(results, scenarios)
    prior = await asyncio.to_thread(latest_report, settings.eval_reports_dir)

    report = EvalReport(
        config_version=settings.config_version,
        created_at=datetime.now(UTC).isoformat(),
        summary=summary,
        results=results,
        regressions=_regressions(summary, prior),
        prior_version=prior.config_version if prior else None,
    )

    if save_report:
        await asyncio.to_thread(write_report, report, settings.eval_reports_dir)
    if persist and settings.app_env != "test":
        await _persist(report)

    _log_summary(report)
    return report


async def _persist(report: EvalReport) -> None:
    try:
        from saral.eval.repository import persist_report

        await persist_report(report)
    except Exception as e:  # noqa: BLE001
        log.warning("eval.persist_failed", error=str(e))


def _log_summary(report: EvalReport) -> None:
    s = report.summary
    log.info(
        "eval.summary",
        version=report.config_version,
        n=s.n,
        pass_rate=s.pass_rate,
        block_rate=s.compliance_block_rate,
        routing=s.routing_accuracy,
        tool_seq=s.tool_sequence_correctness,
        groundedness=s.groundedness,
        cross_lingual=s.cross_lingual_consistency,
        p95_ms=s.latency_p95_ms,
        regressions=report.regressions,
    )


def main() -> None:
    import asyncio

    report = asyncio.run(run_eval())
    s = report.summary
    print(f"\nSaral eval — config {report.config_version} — {s.n} scenarios")
    print(f"  pass_rate                  {s.pass_rate:.2%}")
    print(f"  routing_accuracy           {s.routing_accuracy:.2%}")
    print(f"  tool_sequence_correctness  {s.tool_sequence_correctness:.2%}")
    print(f"  compliance_block_rate      {s.compliance_block_rate:.2%}  (target 100%)")
    print(f"  groundedness               {s.groundedness:.2%}")
    print(f"  resolution_accuracy        {s.resolution_accuracy:.2%}")
    print(f"  cross_lingual_consistency  {s.cross_lingual_consistency:.2%}")
    print(f"  latency p50/p95 ms         {s.latency_p50_ms}/{s.latency_p95_ms}")
    if s.judge_human_agreement is not None:
        print(f"  judge_human_agreement      {s.judge_human_agreement:.2%}")
    if report.regressions:
        print(f"  REGRESSIONS vs {report.prior_version}: {report.regressions}")
    else:
        print("  no regressions")


if __name__ == "__main__":
    main()
