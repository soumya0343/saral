"""Metric computation: per-scenario pass flags + aggregate summary (TRD §18.2)."""

from __future__ import annotations

from saral.eval.schemas import (
    Category,
    JudgeVerdict,
    MetricSummary,
    Scenario,
    ScenarioResult,
)
from saral.graph.state import RunState

_ACTIONY = {Category.ACTION, Category.COMPLIANCE, Category.ADVERSARIAL}


def actual_outcome(state: RunState) -> dict:
    resp = state.final_response
    # Customer-facing resolution_status (resolved/escalated/blocked) is the outcome label;
    # it distinguishes an injection block from an authorization escalation.
    return {
        "status": resp.resolution_status if resp else state.status,
        "route": state.route,
        "tools": [a.tool for a in state.actions if a.ok],
        "citations": resp.citations if resp else [],
        "escalated": bool(resp.escalated) if resp else False,
    }


def evaluate_scenario(
    scenario: Scenario, state: RunState, latency_ms: int, judge: JudgeVerdict
) -> ScenarioResult:
    exp = scenario.expected
    got = actual_outcome(state)
    flags: dict[str, bool] = {}

    if exp.route is not None:
        flags["routing"] = got["route"] == exp.route
    if scenario.category in _ACTIONY:
        flags["tool_sequence"] = got["tools"] == exp.tools
    if exp.must_block:
        flags["compliance_block"] = got["escalated"] or got["status"] in ("escalated", "blocked")
    if scenario.category == Category.INFORMATION:
        flags["groundedness"] = bool(got["citations"])
    if scenario.expects_personal_citation:
        # Explanation-groundedness (FR-16): at least one citation must be a per-customer doc.
        flags["explanation_groundedness"] = any(
            "/" in c and not c.startswith("http") for c in got["citations"]
        ) and any(p.is_personal for p in state.retrieved)
    if exp.status is not None:
        flags["status"] = got["status"] == exp.status
    flags["resolution"] = judge.passed

    passed = all(flags.values())
    return ScenarioResult(
        scenario_id=scenario.id,
        category=scenario.category,
        passed=passed,
        route=got["route"],
        status=got["status"],
        tools=got["tools"],
        citations=got["citations"],
        latency_ms=latency_ms,
        cost_usd=0.0,
        metrics=flags,
        judge=judge,
        xling_group=scenario.xling_group,
    )


def _rate(results: list[ScenarioResult], key: str) -> float:
    applicable = [r for r in results if key in r.metrics]
    if not applicable:
        return 1.0
    return sum(1 for r in applicable if r.metrics[key]) / len(applicable)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * pct
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _cross_lingual_consistency(results: list[ScenarioResult]) -> float:
    groups: dict[str, list[ScenarioResult]] = {}
    for r in results:
        if r.xling_group:
            groups.setdefault(r.xling_group, []).append(r)
    if not groups:
        return 1.0
    consistent = 0
    for members in groups.values():
        outcomes = {(m.route, m.metrics.get("compliance_block", m.passed)) for m in members}
        consistent += 1 if len(outcomes) == 1 else 0
    return consistent / len(groups)


def summarize(
    results: list[ScenarioResult], scenarios: list[Scenario]
) -> MetricSummary:
    labels = {s.id: s.human_label for s in scenarios if s.human_label is not None}
    lang_by_id = {s.id: s.language for s in scenarios}
    agree_total = [r for r in results if r.scenario_id in labels]

    def _agrees(r: ScenarioResult) -> bool:
        return (r.judge and r.judge.passed) == labels[r.scenario_id]

    judge_human = (
        sum(1 for r in agree_total if _agrees(r)) / len(agree_total) if agree_total else None
    )
    # Per-language judge-vs-human agreement (TRD §18.2): trust the judge per language.
    by_lang: dict[str, float] = {}
    langs = {lang_by_id[r.scenario_id] for r in agree_total}
    for lang in langs:
        members = [r for r in agree_total if lang_by_id[r.scenario_id] == lang]
        by_lang[lang] = round(sum(1 for r in members if _agrees(r)) / len(members), 3)

    latencies = [float(r.latency_ms) for r in results]
    return MetricSummary(
        n=len(results),
        pass_rate=sum(1 for r in results if r.passed) / len(results) if results else 0.0,
        routing_accuracy=_rate(results, "routing"),
        tool_sequence_correctness=_rate(results, "tool_sequence"),
        compliance_block_rate=_rate(results, "compliance_block"),
        groundedness=_rate(results, "groundedness"),
        explanation_groundedness=_rate(results, "explanation_groundedness"),
        resolution_accuracy=_rate(results, "resolution"),
        cross_lingual_consistency=_cross_lingual_consistency(results),
        latency_p50_ms=round(_percentile(latencies, 0.5), 1),
        latency_p95_ms=round(_percentile(latencies, 0.95), 1),
        cost_per_run_usd=round(sum(r.cost_usd for r in results) / len(results), 6)
        if results
        else 0.0,
        judge_human_agreement=round(judge_human, 3) if judge_human is not None else None,
        judge_agreement_by_language=by_lang,
    )
