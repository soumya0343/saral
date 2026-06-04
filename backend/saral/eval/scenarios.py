"""Load labeled scenarios from YAML."""

from __future__ import annotations

from pathlib import Path

import yaml

from saral.config import get_settings
from saral.eval.schemas import Scenario


def load_scenarios(path: str | Path | None = None) -> list[Scenario]:
    p = Path(path or get_settings().scenarios_path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    return [Scenario.model_validate(item) for item in raw]
