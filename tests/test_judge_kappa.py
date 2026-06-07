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
    summary = summarize(results, scenarios)
    # Unanimous labels -> kappa undefined -> hi is not validated.
    assert "hi" in summary.judge_untrusted_languages
    assert "hi" not in summary.judge_kappa_by_language
