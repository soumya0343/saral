"""Synchronous filesystem store for eval reports (called off the event loop)."""

from __future__ import annotations

from pathlib import Path

from saral.eval.schemas import EvalReport, EvalTier, ScenarioResult


def list_reports(reports_dir: str | Path, tier: EvalTier | None = None) -> list[EvalReport]:
    d = Path(reports_dir)
    if not d.exists():
        return []
    reports = [
        EvalReport.model_validate_json(p.read_text()) for p in sorted(d.glob("report_*.json"))
    ]
    return [r for r in reports if tier is None or r.tier == tier]


def latest_report(reports_dir: str | Path, tier: EvalTier | None = None) -> EvalReport | None:
    reports = list_reports(reports_dir, tier)
    return reports[-1] if reports else None


def write_report(report: EvalReport, reports_dir: str | Path) -> Path:
    d = Path(reports_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamp = report.created_at.replace(":", "").replace("-", "")[:15]
    path = d / f"report_{stamp}_{report.tier}_{report.config_version}.json"
    path.write_text(report.model_dump_json(indent=2))
    return path


# --- live-tier partial results (resume after a rate-limit abort) ------------------------------


def _partial_path(reports_dir: str | Path, config_version: str) -> Path:
    return Path(reports_dir) / f".partial_live_{config_version}.jsonl"


def load_partial(reports_dir: str | Path, config_version: str) -> list[ScenarioResult]:
    p = _partial_path(reports_dir, config_version)
    if not p.exists():
        return []
    return [ScenarioResult.model_validate_json(line) for line in p.read_text().splitlines() if line]


def append_partial(reports_dir: str | Path, config_version: str, result: ScenarioResult) -> None:
    p = _partial_path(reports_dir, config_version)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(result.model_dump_json() + "\n")


def clear_partial(reports_dir: str | Path, config_version: str) -> None:
    _partial_path(reports_dir, config_version).unlink(missing_ok=True)
