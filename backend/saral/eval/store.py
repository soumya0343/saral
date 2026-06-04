"""Synchronous filesystem store for eval reports (called off the event loop)."""

from __future__ import annotations

from pathlib import Path

from saral.eval.schemas import EvalReport


def list_reports(reports_dir: str | Path) -> list[EvalReport]:
    d = Path(reports_dir)
    if not d.exists():
        return []
    return [EvalReport.model_validate_json(p.read_text()) for p in sorted(d.glob("report_*.json"))]


def latest_report(reports_dir: str | Path) -> EvalReport | None:
    reports = list_reports(reports_dir)
    return reports[-1] if reports else None


def write_report(report: EvalReport, reports_dir: str | Path) -> Path:
    d = Path(reports_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamp = report.created_at.replace(":", "").replace("-", "")[:15]
    path = d / f"report_{stamp}_{report.config_version}.json"
    path.write_text(report.model_dump_json(indent=2))
    return path
