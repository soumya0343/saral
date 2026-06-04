"""Best-effort persistence of eval results to Postgres."""

from __future__ import annotations

from saral.db.models import EvalResult
from saral.db.session import get_sessionmaker
from saral.eval.schemas import EvalReport


async def persist_report(report: EvalReport) -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        for r in report.results:
            session.add(
                EvalResult(
                    scenario_id=r.scenario_id,
                    config_version=report.config_version,
                    metrics={**r.metrics, "latency_ms": r.latency_ms},
                    passed=r.passed,
                )
            )
        await session.commit()
