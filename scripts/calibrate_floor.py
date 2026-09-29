"""Calibrate `retrieval_score_floor` for the active embedder on labelled queries.

    make calibrate-floor        # or: PYTHONPATH=backend uv run python scripts/calibrate_floor.py

For every answerable / unanswerable query it takes the top dense-cosine score the generic index
returns (the same score the floor gate reads) and sweeps candidate floors. Target (roadmap 3.3):
unanswerable -> escalate >= 90% while answerable recall (not escalated) >= 95%. It also reports
retrieval hit@k (the labelled doc, or its `_hi` parallel, in the top k) so a floor is never
tuned on top of retrieval that is wrong anyway. Prints the recommendation; set it in .env as
RETRIEVAL_SCORE_FLOOR (and in config.py's per-embedder default if it should ship).
"""

from __future__ import annotations

import argparse
import statistics
from pathlib import Path

import yaml

from saral.config import get_settings
from saral.rag.index import _parallel_stem, get_index


def _top(query: str) -> tuple[float, list[str]]:
    passages = get_index().search(query, top_k=get_settings().retrieval_top_k)
    return max((p.score for p in passages), default=0.0), [p.doc_id for p in passages]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", default="data/rag_calibration/queries.yaml")
    ap.add_argument("--min-recall", type=float, default=0.95)
    ap.add_argument("--min-reject", type=float, default=0.90)
    a = ap.parse_args()

    data = yaml.safe_load(Path(a.queries).read_text(encoding="utf-8"))
    s = get_settings()
    print(f"embedder={s.embedder} model={s.embedder_model} top_k={s.retrieval_top_k}")

    pos: list[tuple[float, str, str]] = []
    hits = 0
    for row in data["answerable"]:
        score, docs = _top(row["q"])
        hit = _parallel_stem(row["doc"]) in {_parallel_stem(d) for d in docs}
        hits += hit
        pos.append((score, row.get("lang", "?"), row["q"]))
    neg = [(_top(row["q"])[0], row.get("lang", "?"), row["q"]) for row in data["unanswerable"]]

    def summary(xs: list[tuple[float, str, str]]) -> str:
        v = [x[0] for x in xs]
        return f"min {min(v):.3f} p10 {sorted(v)[len(v) // 10]:.3f} median {statistics.median(v):.3f} max {max(v):.3f}"  # noqa: E501

    print(f"answerable   n={len(pos)} hit@k={hits / len(pos):.1%}  {summary(pos)}")
    print(f"unanswerable n={len(neg)}               {summary(neg)}")

    best = None
    print("\nfloor  answerable-kept  unanswerable-escalated")
    candidates = sorted({round(x[0], 3) for x in pos + neg})
    for f in candidates:
        recall = sum(x[0] >= f for x in pos) / len(pos)
        reject = sum(x[0] < f for x in neg) / len(neg)
        if recall >= a.min_recall and (best is None or reject > best[2]):
            best = (f, recall, reject)
    for f in sorted({round(c, 2) for c in candidates})[::2]:
        recall = sum(x[0] >= f for x in pos) / len(pos)
        reject = sum(x[0] < f for x in neg) / len(neg)
        print(f"{f:.2f}   {recall:6.1%}          {reject:6.1%}")

    if best is None:
        print("\nno floor keeps the answerable recall target")
        return
    f, recall, reject = best
    met = "MEETS" if reject >= a.min_reject else "DOES NOT MEET"
    print(
        f"\nrecommended floor {f:.3f}: answerable kept {recall:.1%}, unanswerable escalated "
        f"{reject:.1%} — {met} the {a.min_reject:.0%} target"
    )
    for lang in sorted({x[1] for x in pos + neg}):
        p = [x for x in pos if x[1] == lang]
        n = [x for x in neg if x[1] == lang]
        print(
            f"  {lang:9} kept {sum(x[0] >= f for x in p)}/{len(p)}  "
            f"escalated {sum(x[0] < f for x in n)}/{len(n)}"
        )
    leaks = sorted((x for x in neg if x[0] >= f), reverse=True)[:8]
    if leaks:
        print("  highest-scoring unanswerable queries (would be answered):")
        for score, lang, q in leaks:
            print(f"    {score:.3f} [{lang}] {q}")


if __name__ == "__main__":
    main()
