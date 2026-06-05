"""Load labeled scenarios from YAML."""

from __future__ import annotations

from pathlib import Path

import yaml

from saral.config import get_settings
from saral.eval.schemas import Scenario


def load_scenarios(path: str | Path | None = None) -> list[Scenario]:
    """Load scenarios from the configured file PLUS any sibling `*.yaml` in its directory.

    A single-file path stays the canonical set; expansion files (e.g. scenarios_expansion.yaml)
    in the same directory are merged so the suite can grow without reformatting the base file.
    """
    p = Path(path or get_settings().scenarios_path)
    files = sorted({p, *p.parent.glob("*.yaml")}) if p.exists() else sorted(p.parent.glob("*.yaml"))
    seen: set[str] = set()
    scenarios: list[Scenario] = []
    for f in files:
        raw = yaml.safe_load(f.read_text(encoding="utf-8")) or []
        for item in raw:
            sc = Scenario.model_validate(item)
            if sc.id in seen:
                continue  # ids are unique across files; first wins
            seen.add(sc.id)
            scenarios.append(sc)
    return scenarios
