"""Phase 2 eval machinery: reply-language scorer, reply-level metrics, multi-turn driver,
labels bound to exact replies, pinned live judge, tier detection, and the regression gate."""

from __future__ import annotations

import pytest

from saral.eval.schemas import Category, Expected, JudgeVerdict, Scenario, ScenarioResult, Turn
from saral.tools import mock_backend as mb


def setup_function():
    mb._reset_state()


# --- reply language -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("Your policy covers hospitalisation up to ₹5,00,000.", "en"),
        ("Aapki policy mein hospitalisation ₹5,00,000 tak cover hota hai.", "hinglish"),
        ("आपका दावा खंड 6.2 के अनुसार अस्वीकृत हुआ क्योंकि policy lapse हो गई थी।", "hi"),
        ("Claim CLM2030 status: rejected (Claim rejected — policy lapsed)", "en"),
        ("CLM2030", "unknown"),
    ],
)
def test_reply_language(text, lang):
    from saral.eval.lang import reply_language

    assert reply_language(text) == lang


def test_every_localized_template_is_classified_as_its_language():
    """Guards the scorer against drift: all deterministic EN/HI/Hinglish strings match."""
    from saral.actions import confirm
    from saral.agents import synthesis
    from saral.eval.lang import reply_language
    from saral.graph import build
    from saral.schemas import Language

    want = {Language.EN: "en", Language.HI: "hi", Language.HINGLISH: "hinglish"}
    tables = [v for v in vars(build).values() if isinstance(v, dict) and Language.EN in v]
    tables += [
        v for k, v in vars(synthesis).items()
        if isinstance(v, dict) and Language.EN in v and k != "_LANG_STYLE"
    ]
    tables += list(confirm._READ_BACK.values())
    for table in tables:
        for lang, expected in want.items():
            text = table.get(lang)
            if isinstance(text, str):
                clean = text.replace("{field}", "mobile").replace("{value}", "9811122233")
                assert reply_language(clean.replace("{", "").replace("}", "")) == expected, text


# --- metrics on the reply ---------------------------------------------------------------------


def test_faithfulness_flags_an_invented_number_but_not_citation_labels():
    from saral.eval.metrics import Outcome, evaluate_scenario
    from saral.graph.state import RunState
    from saral.schemas import Passage, ResponsePayload

    passage = Passage(doc_id="U1001/policy.md", chunk_id=4, text="Waiting period is 36 months.",
                      scope="customer", domain="policy_coverage")

    def outcome(reply: str) -> Outcome:
        st = RunState(
            conversation_id="c", user_id="U1001", raw_message="waiting period?",
            status="resolved", retrieved=[passage],
            final_response=ResponsePayload(resolution_status="resolved", message=reply,
                                           citations=[passage.citation]),
        )
        return Outcome(states=[st], user_turns=["waiting period?"])

    sc = Scenario(id="s", category=Category.INFORMATION, language="en", user_id="U1001",
                  message="waiting period?")
    ok = evaluate_scenario(sc, outcome(f"Per ({passage.citation}): it is 36 months."), 1,
                           JudgeVerdict(passed=True))
    bad = evaluate_scenario(sc, outcome("It is 24 months."), 1, JudgeVerdict(passed=True))
    assert ok.metrics["faithfulness"] is True
    assert bad.metrics["faithfulness"] is False


# --- multi-turn driver ------------------------------------------------------------------------


async def test_multi_turn_driver_runs_the_real_resume_logic(monkeypatch):
    from saral.config import get_settings
    from saral.eval.conversation import run_scenario

    monkeypatch.setattr(get_settings(), "demo_mode", True)
    sc = Scenario(
        id="t", category=Category.ACTION, language="en", user_id="U1001",
        turns=[
            Turn(say="Please update my mobile number to 9811122233",
                 expect=Expected(status="awaiting_input")),
            Turn(say="{wrong_otp}", expect=Expected(status="awaiting_input")),
            Turn(say="{otp}", expect=Expected(status="awaiting_confirmation")),
            Turn(say="जी नहीं", expect=Expected(status="resolved")),
        ],
    )
    out = await run_scenario(sc)
    assert out.failed_turns == []
    assert out.final.pending_write is None  # cancelled, never raised to the RM


def test_scenario_needs_exactly_one_of_message_or_turns():
    with pytest.raises(ValueError):
        Scenario(id="x", category=Category.ACTION, language="en", user_id="U1")
    with pytest.raises(ValueError):
        Scenario(id="x", category=Category.ACTION, language="en", user_id="U1",
                 message="a", turns=[Turn(say="b")])


# --- labels bound to the exact reply ----------------------------------------------------------


def test_human_label_only_applies_to_the_reply_it_was_given_for(tmp_path):
    import yaml

    from saral.eval.labels import labels_for, reply_sha

    r = ScenarioResult(scenario_id="s1", category=Category.INFORMATION, passed=True, reply="A")
    path = tmp_path / "labels.yaml"
    path.write_text(yaml.safe_dump([
        {"scenario_id": "s1", "reply_sha": reply_sha(r), "human_passed": False},
    ]))
    assert labels_for([r], path) == {"s1": False}
    changed = r.model_copy(update={"reply": "B"})
    assert labels_for([changed], path) == {}  # a different reply: the label no longer applies


# --- pinned live judge + tier ---------------------------------------------------------------


def test_live_judge_is_pinned_and_refuses_the_stub(monkeypatch):
    from saral.config import get_settings
    from saral.eval.judge import JudgeUnavailable, LLMJudge, live_judge

    with pytest.raises(JudgeUnavailable):
        LLMJudge("stub")
    monkeypatch.setattr(get_settings(), "llm_role_judge", "stub")
    with pytest.raises(JudgeUnavailable):
        live_judge()
    monkeypatch.setattr(get_settings(), "llm_role_judge", "groq:qwen/qwen3.8-27b,stub")
    with pytest.raises(JudgeUnavailable):  # conftest blanks keys: no key -> can't pin
        live_judge()
    monkeypatch.setattr(get_settings(), "groq_api_key", "k")
    j = live_judge()
    assert j.model == "groq:qwen/qwen3.8-27b"
    assert [p.name for p in j._llm.providers] == ["groq"]  # no fallback, no stub


def test_tier_is_derived_from_config_not_claimed():
    from saral.eval.runner import detect_tier

    assert detect_tier() == "offline"  # conftest: stub chains + hashing embedder


# --- gate ------------------------------------------------------------------------------------


def test_gate_flags_drops_and_rises():
    from saral.eval.gate import check
    from saral.eval.schemas import MetricSummary

    def mk(**kw) -> MetricSummary:
        base: dict = {
            "n": 1, "pass_rate": 0.6, "routing_accuracy": 1.0,
            "tool_sequence_correctness": 1.0, "compliance_block_rate": 0.9,
            "groundedness": 1.0, "resolution_accuracy": 1.0, "cross_lingual_consistency": 1.0,
            "latency_p50_ms": 1, "latency_p95_ms": 1, "cost_per_run_usd": 0.0,
            "false_block_rate": 0.5,
        }
        base.update(kw)
        return MetricSummary(**base)

    baseline = {"pass_rate": 0.6, "compliance_block_rate": 0.9, "false_block_rate": 0.5}
    assert check(mk(), baseline) == []
    assert check(mk(pass_rate=0.7, false_block_rate=0.1), baseline) == []  # improvements pass
    fails = check(mk(compliance_block_rate=0.8, false_block_rate=0.6), baseline)
    assert len(fails) == 2
