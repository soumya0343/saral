"""Evaluation runner — two tiers, one suite.

- offline: stub LLM + hashing embedder. Deterministic and free: runs in CI on every change.
  It checks plumbing AND the deterministic replies (language, facts, grounding), but it cannot
  tell you how the real models behave.
- live: the real free models (and multilingual embeddings) the product actually uses, judged by
  a PINNED LLM judge that reads the replies. Throttled for free-tier limits and resumable
  (`--resume`) when a quota runs out mid-suite. This is the tier that can prove — or disprove —
  the Hindi/Hinglish per-customer grounding claim.

The tier is DERIVED from the configuration in effect (no real LLM + hashing embedder = offline),
so a report can never be mislabelled; `--tier` only asserts what you expect.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from saral.config import get_settings
from saral.eval import labels as eval_labels
from saral.eval.conversation import run_scenario
from saral.eval.judge import JudgeUnavailable, LLMJudge, RubricJudge, live_judge
from saral.eval.metrics import actual_outcome, evaluate_scenario, summarize
from saral.eval.scenarios import load_scenarios
from saral.eval.schemas import (
    EvalReport,
    EvalTier,
    JudgeVerdict,
    MetricSummary,
    Scenario,
    ScenarioResult,
)
from saral.eval.store import (
    append_partial,
    clear_partial,
    latest_report,
    load_partial,
    write_report,
)
from saral.llm import usage
from saral.logging import get_logger

log = get_logger(__name__)

# Higher is better, except false_block_rate (lower is better).
_REGRESSION_KEYS = [
    "pass_rate",
    "routing_accuracy",
    "tool_sequence_correctness",
    "compliance_block_rate",
    "groundedness",
    "explanation_groundedness",
    "resolution_accuracy",
    "cross_lingual_consistency",
    "language_match",
    "answer_correctness",
    "faithfulness",
    "multi_turn_success",
]
_LOWER_IS_BETTER = ["false_block_rate"]


class EvalConfigError(RuntimeError):
    pass


def detect_tier() -> EvalTier:
    from saral.llm.factory import get_llm
    from saral.rag.embedder import HashingEmbedder, get_embedder

    real_llm = any(get_llm(role).has_real_provider for role in ("triage", "synthesis"))
    if not real_llm and isinstance(get_embedder(), HashingEmbedder):
        return "offline"
    return "live"


def _context(scenario: Scenario, outcome, *, with_content: bool) -> dict:
    ctx: dict[str, Any] = {
        "category": scenario.category.value,
        "language": scenario.language,
        "expected": scenario.expected.model_dump(),
        "actual": actual_outcome(outcome.final, with_content=with_content),
    }
    # Expected tools cover the whole conversation, not just its last turn.
    ctx["actual"]["tools"] = [a.tool for st in outcome.states for a in st.actions if a.ok]
    if with_content:
        ctx["conversation"] = [
            {"customer": u, "agent": r}
            for u, r in zip(outcome.user_turns, outcome.replies, strict=True)
        ]
    return ctx


async def _run_one(scenario: Scenario, judge: RubricJudge | LLMJudge) -> ScenarioResult:
    t0 = time.perf_counter()
    try:
        outcome = await run_scenario(scenario)
    except Exception as e:  # noqa: BLE001 — one broken scenario must not sink the suite
        log.error("eval.scenario_error", scenario=scenario.id, error=str(e))
        return ScenarioResult(
            scenario_id=scenario.id,
            category=scenario.category,
            passed=False,
            language=scenario.language,
            metrics={"ran": False},
            error=str(e),
            xling_group=scenario.xling_group,
        )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    live = isinstance(judge, LLMJudge)
    before = usage.current()
    verdict: JudgeVerdict = await judge.evaluate(_context(scenario, outcome, with_content=live))
    tokens = sum(s.tokens_used for s in outcome.states) + (usage.current() - before)
    return evaluate_scenario(scenario, outcome, latency_ms, verdict, tokens=tokens)


def _regressions(current: MetricSummary, prior: EvalReport | None) -> dict[str, float]:
    if prior is None:
        return {}
    cur, old = current.model_dump(), prior.summary.model_dump()
    out: dict[str, float] = {}
    for key in _REGRESSION_KEYS:
        if key in old and (delta := round(cur[key] - old[key], 4)) < 0:
            out[key] = delta
    for key in _LOWER_IS_BETTER:
        if key in old and (delta := round(cur[key] - old[key], 4)) > 0:
            out[key] = delta
    return out


async def run_eval(
    *,
    expect_tier: EvalTier | None = None,
    persist: bool = True,
    save_report: bool = True,
    only: list[str] | None = None,
    limit: int | None = None,
    resume: bool = False,
) -> EvalReport:
    settings = get_settings()
    tier = detect_tier()
    if expect_tier and tier != expect_tier:
        raise EvalConfigError(
            f"asked for the {expect_tier} tier but the config is {tier}: "
            + (
                "offline needs no LLM keys/stub chains and EMBEDDER=hashing (use `make eval`)"
                if expect_tier == "offline"
                else "live needs real LLM keys (see .env.example)"
            )
        )
    scenarios = load_scenarios()
    if only:
        scenarios = [s for s in scenarios if s.id in set(only)]
    if limit:
        scenarios = scenarios[:limit]

    judge: RubricJudge | LLMJudge = live_judge() if tier == "live" else RubricJudge()
    done: dict[str, ScenarioResult] = {}
    if tier == "live" and resume:
        partial = load_partial(settings.eval_reports_dir, settings.config_version)
        done = {r.scenario_id: r for r in partial}
        log.info("eval.resume", already_done=len(done))
    elif tier == "live":
        clear_partial(settings.eval_reports_dir, settings.config_version)

    # Multi-turn scenarios reply to OTP prompts: surface the code like the demo does.
    demo_before = settings.demo_mode
    settings.demo_mode = True
    results: list[ScenarioResult] = []
    try:
        for i, scenario in enumerate(scenarios):
            if scenario.id in done:
                results.append(done[scenario.id])
                continue
            from saral.tools import mock_backend as mb

            mb._reset_state()  # every scenario starts from the same seeded core system
            result = await _run_one(scenario, judge)
            results.append(result)
            if tier == "live":
                append_partial(settings.eval_reports_dir, settings.config_version, result)
                if i < len(scenarios) - 1:
                    await asyncio.sleep(settings.eval_live_gap_s)  # stay under free-tier RPM
    except JudgeUnavailable as e:
        raise EvalConfigError(
            f"{e}. {len(results)} scenario results kept — rerun with --resume to continue."
        ) from e
    finally:
        settings.demo_mode = demo_before

    human = eval_labels.labels_for(results) if tier == "live" else None
    summary = summarize(results, scenarios, tier=tier, human_labels=human)
    prior = await asyncio.to_thread(latest_report, settings.eval_reports_dir, tier)
    report = EvalReport(
        config_version=settings.config_version,
        created_at=datetime.now(UTC).isoformat(),
        tier=tier,
        judge_model=judge.model,
        kappa_note=(
            "offline: the rubric judge is derived from the labels, so judge-vs-human agreement "
            "is not meaningful and is not reported"
            if tier == "offline"
            else f"live: kappa from {len(human or {})} human labels on this run's replies"
        ),
        summary=summary,
        results=results,
        regressions=_regressions(summary, prior),
        prior_version=prior.config_version if prior else None,
    )
    if save_report:
        await asyncio.to_thread(write_report, report, settings.eval_reports_dir)
    if tier == "live":
        clear_partial(settings.eval_reports_dir, settings.config_version)
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
        tier=report.tier,
        version=report.config_version,
        judge=report.judge_model,
        n=s.n,
        pass_rate=s.pass_rate,
        block_rate=s.compliance_block_rate,
        false_block_rate=s.false_block_rate,
        language_match=s.language_match,
        faithfulness=s.faithfulness,
        regressions=report.regressions,
    )


def print_report(report: EvalReport) -> None:
    s = report.summary
    print(f"\nSaral eval — {report.tier} tier — config {report.config_version} — {s.n} scenarios")
    print(f"  judge: {report.judge_model}")
    rows = [
        ("pass_rate", s.pass_rate),
        ("routing_accuracy", s.routing_accuracy),
        ("tool_sequence_correctness", s.tool_sequence_correctness),
        ("compliance_block_rate (target 100%)", s.compliance_block_rate),
        ("false_block_rate (lower is better)", s.false_block_rate),
        ("groundedness", s.groundedness),
        ("explanation_groundedness", s.explanation_groundedness),
        ("language_match", s.language_match),
        ("answer_correctness", s.answer_correctness),
        ("faithfulness", s.faithfulness),
        ("multi_turn_success", s.multi_turn_success),
        ("resolution_accuracy", s.resolution_accuracy),
        ("cross_lingual_consistency", s.cross_lingual_consistency),
    ]
    for name, value in rows:
        print(f"  {name:38} {value:7.2%}")
    print(f"  {'pass_rate by language':38} {s.pass_rate_by_language}")
    print(f"  {'latency p50/p95 ms':38} {s.latency_p50_ms}/{s.latency_p95_ms}")
    print(f"  {'tokens':38} {s.tokens_total}")
    if s.judge_kappa_by_language:
        print(f"  {'judge kappa (per language)':38} {s.judge_kappa_by_language}")
    print(f"  kappa: {report.kappa_note}")
    if s.judge_untrusted_languages:
        print(f"  judge NOT validated for: {s.judge_untrusted_languages}")
    errors = [r.scenario_id for r in report.results if r.error]
    if errors:
        print(f"  scenarios that crashed: {errors}")
    failing = [r.scenario_id for r in report.results if not r.passed and not r.error]
    print(f"  failing ({len(failing)}): {failing}")
    if report.regressions:
        print(f"  REGRESSIONS vs prior {report.tier} report: {report.regressions}")


def main() -> None:
    p = argparse.ArgumentParser(description="Run the Saral eval suite")
    p.add_argument("--tier", choices=["offline", "live"], help="assert the expected tier")
    p.add_argument("--only", nargs="*", help="scenario ids to run")
    p.add_argument("--limit", type=int, help="run only the first N scenarios")
    p.add_argument("--resume", action="store_true", help="live: continue an aborted run")
    p.add_argument("--no-save", action="store_true", help="don't write a report file")
    a = p.parse_args()
    try:
        report = asyncio.run(
            run_eval(
                expect_tier=a.tier,
                only=a.only,
                limit=a.limit,
                resume=a.resume,
                save_report=not a.no_save,
            )
        )
    except EvalConfigError as e:
        raise SystemExit(f"eval: {e}") from e
    print_report(report)


if __name__ == "__main__":
    main()
