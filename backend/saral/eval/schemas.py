"""Evaluation schemas: scenarios, judge verdicts, per-scenario results, and the report."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Category(StrEnum):
    INFORMATION = "information"
    ACTION = "action"
    COMPLIANCE = "compliance"
    ADVERSARIAL = "adversarial"
    SMALL_TALK = "small_talk"


class Expected(BaseModel):
    route: str | None = None  # respond | rag | action | mixed
    status: str | None = None  # resolved | escalated | blocked
    tools: list[str] = Field(default_factory=list)  # expected ok tool sequence
    must_block: bool = False  # compliance/adversarial safety bar
    answer_contains: list[str] = Field(default_factory=list)  # keywords expected in reply
    cite_docs: list[str] = Field(default_factory=list)  # source filenames expected in citations


class Scenario(BaseModel):
    id: str
    category: Category
    language: str
    user_id: str
    message: str
    expected: Expected = Field(default_factory=Expected)
    xling_group: str | None = None  # cross-lingual triple id
    human_label: bool | None = None  # human pass/fail for judge validation


class JudgeVerdict(BaseModel):
    passed: bool
    score: float = Field(ge=0.0, le=1.0, default=0.0)
    reason: str = ""


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


class MetricSummary(BaseModel):
    n: int
    pass_rate: float
    routing_accuracy: float
    tool_sequence_correctness: float
    compliance_block_rate: float
    groundedness: float
    resolution_accuracy: float
    cross_lingual_consistency: float
    latency_p50_ms: float
    latency_p95_ms: float
    cost_per_run_usd: float
    judge_human_agreement: float | None = None


class EvalReport(BaseModel):
    config_version: str
    created_at: str
    summary: MetricSummary
    results: list[ScenarioResult]
    regressions: dict[str, float] = Field(default_factory=dict)  # metric -> delta vs prior (<0)
    prior_version: str | None = None
