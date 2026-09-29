"""Judge-vs-human Cohen's kappa + the trust floor gate (ADR-0003 / CONTEXT 'Judge')."""

from __future__ import annotations

from saral.eval.agreement import cohens_kappa


def test_perfect_agreement_two_classes():
    assert cohens_kappa([(True, True), (False, False), (True, True), (False, False)]) == 1.0


def test_chance_agreement_is_zero():
    assert cohens_kappa([(True, True), (True, False), (False, True), (False, False)]) == 0.0


def test_single_class_is_undefined():
    # An all-pass suite has no disagreement to measure -> kappa undefined -> language untrusted.
    assert cohens_kappa([(True, True)] * 5) is None


def test_too_few_samples_is_undefined():
    assert cohens_kappa([(True, True)]) is None


def test_below_floor_language_is_gated_untrusted():
    """A language whose judge kappa is undefined/low must land in judge_untrusted_languages."""
    from saral.eval.metrics import summarize
    from saral.eval.schemas import Category, JudgeVerdict, Scenario, ScenarioResult

    def _sc(sid):
        return Scenario(
            id=sid, category=Category.INFORMATION, language="hi",
            user_id="U1", message="x", human_label=True,
        )

    def _res(sid):
        return ScenarioResult(
            scenario_id=sid, category=Category.INFORMATION,
            passed=True, judge=JudgeVerdict(passed=True),
        )

    scenarios = [_sc("s1"), _sc("s2")]
    results = [_res("s1"), _res("s2")]
    summary = summarize(results, scenarios, tier="live", human_labels={"s1": True, "s2": True})
    # Unanimous labels -> kappa undefined -> hi is not validated.
    assert "hi" in summary.judge_untrusted_languages
    assert "hi" not in summary.judge_kappa_by_language


def test_offline_summary_never_reports_judge_agreement():
    """The offline rubric judge is built from the labels: agreeing with them proves nothing."""
    from saral.eval.metrics import summarize
    from saral.eval.schemas import Category, JudgeVerdict, Scenario, ScenarioResult

    sc = Scenario(id="s1", category=Category.INFORMATION, language="en", user_id="U1",
                  message="x", human_label=True)
    res = ScenarioResult(scenario_id="s1", category=Category.INFORMATION, passed=True,
                         judge=JudgeVerdict(passed=True), language="en")
    s = summarize([res], [sc], tier="offline")
    assert s.judge_human_agreement is None and s.judge_kappa_by_language == {}


def test_live_language_without_labels_is_untrusted():
    from saral.eval.metrics import summarize
    from saral.eval.schemas import Category, JudgeVerdict, ScenarioResult

    res = ScenarioResult(scenario_id="s1", category=Category.INFORMATION, passed=True,
                         judge=JudgeVerdict(passed=True), language="hinglish")
    s = summarize([res], [], tier="live", human_labels={})
    assert s.judge_untrusted_languages == ["hinglish"]
