"""Held-out injection report — the guard measured on sets written BLIND to it.

    PYTHONPATH=backend uv run python -m saral.eval.heldout

`data/scenarios/heldout/*.yaml` were written by someone who never saw the detector, so they
measure how the rules generalize to techniques nobody tuned for. They are deliberately NOT part
of the eval suite or the CI gate: once a set has been used to change the detector it is
contaminated (its number only goes up), so each report says which sets are still clean. When a
set gets used for tuning, add its filename to CONTAMINATED and commission a fresh blind set.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import yaml

from saral.compliance.injection import detect_injection

HELDOUT_DIR = Path("data/scenarios/heldout")
# Sets already used to revise the detector (numbers are optimistic from then on), with the
# first blind reading so the honest number isn't lost.
CONTAMINATED = {
    "injection_heldout.yaml": "tuned on; first blind read 17/35 attacks, 0/35 false blocks",
    "injection_heldout_2.yaml": "one false-block fix (own claims) after the first blind read "
    "of 25/40 attacks, 1/40 false blocks — no attack rule was tuned on it",
}


def score(path: Path) -> dict[str, dict[str, list[int]]]:
    """{language: {"attack": [caught, n], "lookalike": [blocked, n]}}."""
    out: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: {"attack": [0, 0], "lookalike": [0, 0]}
    )
    for sc in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
        hit = detect_injection(sc["message"])[0]
        kind = "attack" if sc.get("expected", {}).get("must_block") else "lookalike"
        out[sc["language"]][kind][0] += hit
        out[sc["language"]][kind][1] += 1
    return out


def main() -> None:
    for path in sorted(HELDOUT_DIR.glob("*.yaml")):
        res = score(path)
        att = [sum(r["attack"][i] for r in res.values()) for i in (0, 1)]
        look = [sum(r["lookalike"][i] for r in res.values()) for i in (0, 1)]
        note = CONTAMINATED.get(path.name)
        state = f"used for tuning: {note}" if note else "clean"
        print(f"{path.name}  [{state}]")
        print(f"  attacks blocked      {att[0]}/{att[1]} ({att[0] / max(att[1], 1):.0%})")
        print(f"  look-alikes blocked  {look[0]}/{look[1]} ({look[0] / max(look[1], 1):.0%})")
        for lang, r in sorted(res.items()):
            print(
                f"    {lang:9} attacks {r['attack'][0]}/{r['attack'][1]}  "
                f"false blocks {r['lookalike'][0]}/{r['lookalike'][1]}"
            )


if __name__ == "__main__":
    main()
