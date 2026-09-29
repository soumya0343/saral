"""Evaluation schemas: scenarios, judge verdicts, per-scenario results, and the report."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

EvalTier = Literal["offline", "live"]


class Category(StrEnum):
    INFORMATION = "information"
    ACTION = "action"
    COMPLIANCE = "compliance"
    ADVERSARIAL = "adversarial"
    SMALL_TALK = "small_talk"
    # Benign message that LOOKS like an attack ("when will you approve my loan?"): scored only on
    # not being blocked (false-positive rate of the injection guard) + the judge.
    LOOKALIKE = "lookalike"


class Expected(BaseModel):
    route: str | None = None  # respond | rag | action | mixed
    status: str | None = None  # resolved | escalated | blocked
    tools: list[str] = Field(default_factory=list)  # expected ok tool sequence
    must_block: bool = False  # compliance/adversarial safety bar
    answer_contains: list[str] = Field(default_factory=list)  # facts that must be in the reply
    cite_docs: list[str] = Field(default_factory=list)  # source filenames expected in citations
    # Benign look-alike of an attack ("when will you approve my claim?"): must NOT be blocked
    # by the injection guard. Measures the guard's false-positive rate.
    must_not_block: bool = False


class Turn(BaseModel):
    """One customer message in a multi-turn scenario, with what must hold after it.

    `say` placeholders: "{otp}" = the code just sent (DEMO_MODE), "{wrong_otp}" = a wrong one.
    """

    say: str
    expect: Expected = Field(default_factory=Expected)


class Scenario(BaseModel):
    id: str
    category: Category
    language: str
    user_id: str
    message: str = ""  # single-turn scenarios
    turns: list[Turn] = Field(default_factory=list)  # multi-turn scenarios (instead of message)
    expected: Expected = Field(default_factory=Expected)  # multi-turn: checked on the LAST turn
    xling_group: str | None = None  # cross-lingual triple id
    human_label: bool | None = None  # human pass/fail for judge validation
    # Starting auth level for the run (token-derived). Writes start at "session" and gate up.
    auth_level: str = "session"
    # Drive a suspended write through step-up + confirmation to completion (a verified,
    # confirming customer). Set false to assert the suspend itself (expected.status awaiting_*).
    complete_stepup: bool = True
    # The cited clause must match THIS customer's own variant (explanation-groundedness).
    expects_personal_citation: bool = False

    @model_validator(mode="after")
    def _message_or_turns(self) -> Scenario:
        if bool(self.message) == bool(self.turns):
            raise ValueError(f"scenario {self.id}: set exactly one of `message` or `turns`")
        return self

    @property
    def is_multi_turn(self) -> bool:
        return bool(self.turns)


class JudgeVerdict(BaseModel):
    passed: bool
    score: float = Field(ge=0.0, le=1.0, default=0.0)
    reason: str = ""
    # Every factual claim in the reply is supported by the passages / action results. Set by an
    # LLM judge (live tier); None when not judged (offline rubric) or nothing factual was said.
    faithful: bool | None = None


class ScenarioResult(BaseModel):
    scenario_id: str
    category: Category
    passed: bool
    route: str | None = None
    status: str | None = None
    tools: list[str] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    latency_ms: int = 0
    cost_usd: float = 0.0
    metrics: dict[str, bool] = Field(default_factory=dict)  # per-dimension pass flags
    judge: JudgeVerdict | None = None
    xling_group: str | None = None
    language: str = ""
    reply: str = ""  # the final customer-facing message (multi-turn: last assistant turn)
    # Multi-turn: every assistant reply in order, and which turn expectations failed.
    transcript: list[dict[str, str]] = Field(default_factory=list)
    failed_turns: list[int] = Field(default_factory=list)
    tokens: int = 0
    error: str | None = None  # the run itself raised (counted as a failure)


class MetricSummary(BaseModel):
    n: int
    pass_rate: float
    routing_accuracy: float
    tool_sequence_correctness: float
    compliance_block_rate: float
    groundedness: float
    explanation_groundedness: float = 1.0
    resolution_accuracy: float
    cross_lingual_consistency: float
    latency_p50_ms: float
    latency_p95_ms: float
    cost_per_run_usd: float
    # --- Phase 2: measure the reply itself, not just the plumbing ---
    language_match: float = 1.0  # reply is in the customer's language/script
    answer_correctness: float = 1.0  # required facts present + expected source cited
    faithfulness: float = 1.0  # no number/id the sources don't contain; LLM claim check (live)
    false_block_rate: float = 0.0  # benign look-alikes wrongly blocked by the injection guard
    multi_turn_success: float = 1.0  # multi-turn scenarios passing every turn expectation
    pass_rate_by_language: dict[str, float] = Field(default_factory=dict)
    tokens_total: int = 0
    judge_human_agreement: float | None = None
    # Raw judge-vs-human agreement per language.
    judge_agreement_by_language: dict[str, float] = Field(default_factory=dict)
    # Chance-corrected agreement (Cohen's kappa) per language; only languages where it is
    # defined. The floor (config.judge_kappa_floor) gates trust.
    judge_kappa_by_language: dict[str, float] = Field(default_factory=dict)
    # Languages where the judge is NOT validated (kappa undefined or below floor): the
    # resolution metric there needs human review, not the judge.
    judge_untrusted_languages: list[str] = Field(default_factory=list)


class EvalReport(BaseModel):
    config_version: str
    created_at: str
    # offline = stub LLM + hashing embedder (deterministic, CI); live = real free models.
    # Derived from the actual config at run time, never from a flag, so it can't be mislabelled.
    tier: EvalTier = "offline"
    judge_model: str = "rubric"  # "rubric" (deterministic) or the pinned LLM judge
    # Offline kappa is circular (the rubric judge mirrors the labels): only live kappa means
    # anything, and only on labels given to that run's actual replies.
    kappa_note: str = ""
    summary: MetricSummary
    results: list[ScenarioResult]
    regressions: dict[str, float] = Field(default_factory=dict)  # metric -> delta vs prior (<0)
    prior_version: str | None = None
