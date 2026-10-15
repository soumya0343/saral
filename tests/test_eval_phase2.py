"""Phase 2 eval machinery: reply-language scorer, reply-level metrics, multi-turn driver,
labels bound to exact replies, pinned live judge, tier detection, and the regression gate."""

from __future__ import annotations

import pytest

from saral.config import get_settings
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


async def test_eval_refuses_the_durable_store(monkeypatch):
    """Every scenario wipes the mock store: against Postgres that deletes real dev data."""
    from saral.eval.runner import EvalConfigError, run_eval
    from saral.tools import mock_backend as mb

    monkeypatch.setattr(get_settings(), "store_backend", "postgres")
    monkeypatch.setattr(mb, "_reset_state", lambda: pytest.fail("store was reset"))
    with pytest.raises(EvalConfigError, match="STORE_BACKEND=memory"):
        await run_eval(expect_tier="offline", persist=False, save_report=False)


def _report(created_at: str = "2026-10-13T00:00:00+00:00", tier: str = "offline"):
    from saral.eval.schemas import EvalReport, MetricSummary

    required = {k: 1 for k, f in MetricSummary.model_fields.items() if f.is_required()}
    return EvalReport(
        config_version="v1", created_at=created_at, tier=tier, judge_model="rubric",
        summary=MetricSummary(**required), results=[],
    )


def _fake_eval_child(monkeypatch, reports_dir) -> dict:
    """Replace the eval child process with one that writes a report; returns what it saw."""
    import asyncio

    from saral.eval import store

    seen: dict = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            store.write_report(_report(), reports_dir)
            return b"", b""

    async def fake_exec(*cmd, env, **_):
        seen.update(cmd=cmd, env=env)
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(get_settings(), "eval_reports_dir", str(reports_dir))
    return seen


def test_eval_button_runs_offline_in_a_child_process(monkeypatch, tmp_path):
    """POST /eval/run must work on a server with real keys and a durable store: it runs the
    offline tier in a child process (stub models, in-memory store) and returns its report."""
    from fastapi.testclient import TestClient

    from saral.api import eval_routes
    from saral.api.app import create_app

    monkeypatch.setattr(get_settings(), "groq_api_key", "k")  # a "live" server
    monkeypatch.setattr(get_settings(), "store_backend", "postgres")
    seen = _fake_eval_child(monkeypatch, tmp_path)
    r = TestClient(create_app()).post("/eval/run")
    assert r.status_code == 200 and r.json()["tier"] == "offline"
    assert seen["cmd"][1:] == ("-m", "saral.eval.runner", "--tier", "offline")
    env = seen["env"]
    assert env["STORE_BACKEND"] == "memory" and env["LLM_ROLE_SYNTHESIS"] == "stub"
    assert env["GROQ_API_KEY"] == "" and env["EMBEDDER"] == "hashing"
    assert eval_routes._BACKEND_DIR in env["PYTHONPATH"]


def test_public_demo_lets_anyone_run_it_rate_limited_per_ip(monkeypatch, tmp_path):
    """EVAL_RUN_PUBLIC on a prod deployment: no key needed, but each client IP is limited;
    the operator's service key is not."""
    from fastapi.testclient import TestClient

    from saral.api import eval_routes
    from saral.api.app import create_app

    s = get_settings()
    monkeypatch.setattr(s, "app_env", "prod")
    monkeypatch.setattr(s, "service_api_key", "s3cret")
    _fake_eval_child(monkeypatch, tmp_path)
    hits: list[tuple] = []

    async def fake_hit(bucket, subject, limit, window_s):
        hits.append((bucket, limit, window_s))

    monkeypatch.setattr(eval_routes, "hit", fake_hit)
    api = TestClient(create_app())
    assert api.post("/eval/run").status_code == 401  # not public: key required

    monkeypatch.setattr(s, "eval_run_public", True)
    assert api.post("/eval/run").status_code == 200
    assert hits == [("eval_run", s.eval_run_public_per_hour, 3600)]
    assert api.post("/eval/run", headers={"X-Service-Key": "s3cret"}).status_code == 200
    assert len(hits) == 1  # the operator is not counted


def test_prod_run_prunes_old_offline_reports_but_never_live_ones(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from saral.api.app import create_app
    from saral.eval import store

    for day in range(1, 6):
        store.write_report(_report(f"2026-09-0{day}T00:00:00+00:00"), tmp_path)
    store.write_report(_report("2026-09-02T00:00:00+00:00", tier="live"), tmp_path)
    s = get_settings()
    monkeypatch.setattr(s, "app_env", "prod")
    monkeypatch.setattr(s, "eval_run_public", True)
    monkeypatch.setattr(s, "eval_reports_keep", 3)
    monkeypatch.setattr(s, "rate_limit_enabled", False)
    _fake_eval_child(monkeypatch, tmp_path)
    assert TestClient(create_app()).post("/eval/run").status_code == 200
    names = sorted(p.name for p in tmp_path.glob("report_*.json"))
    assert len([n for n in names if "_offline_" in n]) == 3
    assert "report_20261013T000000_offline_v1.json" in names  # the new run is kept
    assert [n for n in names if "_live_" in n] == ["report_20260902T000000_live_v1.json"]


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
