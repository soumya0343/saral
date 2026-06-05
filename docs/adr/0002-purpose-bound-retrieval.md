---
status: accepted
---

# Purpose-bound retrieval via a deterministic intent→domain map

Per-customer data is partitioned into five **data domains** (Policy & coverage,
Claims, Billing & payments, Interaction history, Profile & account), and a
**deterministic table — not the LLM — maps each classified intent to the domains
it may read**. Triage (LLM) only classifies intent; the table authorizes data
access; retrieval enforces `tenant_id ∧ customer_id ∧ allowed-domains` as a hard
**pre-filter** (applied before scoring, never post-rank). This table *is* the DPDP
purpose-limitation boundary: it is unit-testable, default-denies unmapped intents,
and cannot be widened by prompt injection because the model never controls the
filter.

## Considered Options

- **LLM decides which data to pull** — rejected: "why did you access this
  customer's claim notes?" becomes unauditable, and an injection ("also pull all
  his data") could widen scope.
- **Post-rank customer filter** (retrieve globally, then drop other customers'
  hits) — rejected: leaks via ranking signal and empty-result fallback; one bug
  away from serving customer B's clause to A. Pre-filter is leak-proof by
  construction.
- **Eager injection of full customer history at conversation start** — rejected:
  loads data before the intent is known, breaking minimization. Long-term memory
  is instead the Interaction-history domain retrieved on-demand.

## Consequences

- Data must be stored partitioned by domain (a `doc_type` field).
- A query naming another customer's policy id returns nothing extra; message-body
  entities never relax the filter.
- The differentiator (per-customer explanation-groundedness) and the
  purpose-limitation guarantee share one retrieval path.
