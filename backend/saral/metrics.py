"""Prometheus metrics (free, pull-based). The API serves them at /metrics; the worker — a
separate process — serves its own on WORKER_METRICS_PORT. Label values are small closed sets
(node names, provider names, outcomes, reasons), never ids or customer data."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

NODE_SECONDS = Histogram(
    "saral_node_seconds",
    "Wall-clock per graph node",
    ["node"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30),
)
RUNS = Counter("saral_runs_total", "Finished runs by terminal status", ["status"])
LLM_CALLS = Counter(
    "saral_llm_calls_total",
    "LLM provider attempts by outcome (ok | rate_limited | timeout | error)",
    ["provider", "outcome"],
)
LLM_SERVED_BY_STUB = Counter(
    "saral_llm_served_by_stub_total", "Calls that fell to the deterministic stub with real keys set"
)
BREAKER_OPEN = Gauge("saral_llm_breaker_open", "1 while a provider's circuit is open", ["provider"])
ESCALATIONS = Counter("saral_escalations_total", "Escalations by reason", ["reason"])
RM_REQUESTS = Counter(
    "saral_rm_requests_total", "RM requests by kind; created=false is a deduplicated ask",
    ["kind", "created"],
)


def llm_outcome(err: Exception | None) -> str:
    if err is None:
        return "ok"
    text = str(err).lower()
    if "429" in text or "rate-limit" in text or "rate limit" in text:
        return "rate_limited"
    if "time budget" in text or "timeout" in text or "timed out" in text:
        return "timeout"
    return "error"
