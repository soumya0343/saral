"""Judge-vs-human agreement: Cohen's kappa (CONTEXT 'Judge': floor kappa >= 0.6).

The judge is only trusted in a language where it agrees with human labels beyond chance.
Kappa corrects raw agreement for chance — critical when labels are skewed (an all-pass suite
has high raw agreement but no real signal). Kappa is *undefined* when there is no disagreement
to measure (a single class, or unanimous raters): we return None and the caller treats that
language as un-validated -> gated behind human review rather than silently trusted.
"""

from __future__ import annotations


def cohens_kappa(pairs: list[tuple[bool, bool]]) -> float | None:
    """Cohen's kappa over (judge_pass, human_pass) pairs. None when undefined."""
    n = len(pairs)
    if n < 2:
        return None
    po = sum(1 for a, b in pairs if a == b) / n
    p_judge_true = sum(1 for a, _ in pairs if a) / n
    p_human_true = sum(1 for _, b in pairs if b) / n
    pe = p_judge_true * p_human_true + (1 - p_judge_true) * (1 - p_human_true)
    if pe >= 1.0:  # both raters unanimous -> no chance-corrected signal; kappa undefined
        return None
    return round((po - pe) / (1 - pe), 3)
