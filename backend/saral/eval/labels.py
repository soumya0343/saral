"""Human labels on LIVE eval replies — the only thing that can validate the LLM judge.

A label belongs to one exact reply (sha256 of its text): if a later run produces a different
reply for the same scenario, the old label no longer applies and is ignored. Workflow:

    make eval-live                          # writes a live report
    python -m saral.eval.labels export      # add unlabelled replies to data/eval_labels/
    # humans set `human_passed: true|false` (the judge's verdict is deliberately NOT shown)
    make eval-live                          # kappa per language now uses those labels
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import yaml

from saral.config import get_settings
from saral.eval.schemas import ScenarioResult
from saral.eval.store import latest_report

LABELS_PATH = Path("data/eval_labels/live_labels.yaml")


def reply_sha(result: ScenarioResult) -> str:
    body = result.reply + "".join(t.get("assistant", "") for t in result.transcript)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _load(path: Path = LABELS_PATH) -> list[dict]:
    if not path.exists():
        return []
    return yaml.safe_load(path.read_text(encoding="utf-8")) or []


def labels_for(results: list[ScenarioResult], path: Path = LABELS_PATH) -> dict[str, bool]:
    """scenario_id -> human verdict, only where the label was given to this exact reply."""
    by_key = {
        (e["scenario_id"], e["reply_sha"]): e["human_passed"]
        for e in _load(path)
        if e.get("human_passed") is not None
    }
    out: dict[str, bool] = {}
    for r in results:
        verdict = by_key.get((r.scenario_id, reply_sha(r)))
        if verdict is not None:
            out[r.scenario_id] = bool(verdict)
    return out


def export(path: Path = LABELS_PATH) -> int:
    """Append the latest live report's replies that have no label yet. Returns how many."""
    report = latest_report(get_settings().eval_reports_dir, tier="live")
    if report is None:
        raise SystemExit("no live report yet: run `make eval-live` first")
    entries = _load(path)
    known = {(e["scenario_id"], e["reply_sha"]) for e in entries}
    added = 0
    for r in report.results:
        key = (r.scenario_id, reply_sha(r))
        if key in known or r.error:
            continue
        entries.append(
            {
                "scenario_id": r.scenario_id,
                "language": r.language,
                "reply_sha": key[1],
                "conversation": r.transcript or [{"user": "", "assistant": r.reply}],
                "human_passed": None,  # fill in: true / false
                "labeler": None,
                "note": None,
            }
        )
        added += 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(entries, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    return added


def main() -> None:
    if sys.argv[1:] != ["export"]:
        raise SystemExit("usage: python -m saral.eval.labels export")
    n = export()
    print(f"added {n} unlabelled replies to {LABELS_PATH} — set human_passed on each")


if __name__ == "__main__":
    main()
