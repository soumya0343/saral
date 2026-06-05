# Planted Dirt Catalogue (eval checklist)

Deliberate messiness so the synthetic data can't flatter the bot (CONTEXT 'Dirt'). Each item
is an eval edge case: the system must handle it without inventing facts.

| # | Dirt | Customer / file | What it tests |
|---|------|-----------------|---------------|
| 1 | Name typo/duplicate "Asha Vermaa" vs "Asha Verma" | U1001/correspondence.md | Same customer, not a new one — no false split |
| 2 | Partial claim (not full reject) citing TWO clauses (4.1 + 4.2) | U1001/claims/CLM2010.md | Explanation-groundedness: cite the customer's own variant clauses |
| 3 | Lapsed policy → death claim rejected | U1003 (POL1003 lapsed) | Reject for lapse (Clause 6.2), not a generic exclusion |
| 4 | Unpaid premium after a verbal "will pay next week" promise | U1003/correspondence.md | Why the lapse happened; no premium received |
| 5 | Critical-illness rider claim rejected — diabetes not listed | U1010/claims/CLM2050.md | Cite rider Clause R1.3's specific list, not a template |
| 6 | Hinglish complaint about the same rejection | U1010/correspondence.md | Cross-lingual grounding to the same clause |
| 7 | Lapsed thin policies (U1022 health, U1030 motor) | manifest.yaml | Status-aware answers for thin customers |
| 8 | Personal-loan "policies" with zero sum_assured (U1023, U1029) | manifest.yaml | Product variety; no health-only assumptions |

## Explanation-groundedness anchors (the differentiator's proof)

- **U1001 / CLM2010** — "Why was my claim reduced?" → must cite **Clause 4.1 (proportionate
  deduction)** and **Clause 4.2 (10% co-pay)** from *Asha's own* Variant-B schedule.
- **U1003 / CLM2030** — "मेरा क्लेम क्यों अस्वीकृत हुआ?" → must cite **खंड 6.2 (lapse)**.
- **U1010 / CLM2050** — "Diabetes claim reject kyun hua?" → must cite **राइडर खंड R1.3**.

A generic-template answer (e.g. "claims may be rejected per policy terms") fails the
explanation-groundedness metric; only the customer's own clause passes.
