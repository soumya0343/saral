# Per-Customer Dataset — the differentiator's proof

This directory holds the **document-fidelity** synthetic data that proves Saral's
differentiator: a bot that grounds answers in *this customer's own* policy clauses, claim
adjudication notes, and history — not a generic FAQ template (TRD §12.3, FR-16).

## Hero vs thin (CONTEXT)

- **Hero customers** (`is_hero: true` in `manifest.yaml`) — hand-authored full records across
  the five data domains, with numbered clauses, named riders, and adjudication notes that cite
  those clauses. The per-customer RAG index loads their `documents`. 3 heroes today:
  - **U1001 — Asha Verma** (English, health) — partial claim, Clause 4.1 + 4.2.
  - **U1003 — मीना कुमारी** (Hindi, term life, **lapsed**) — rejected death claim, Clause 6.2.
  - **U1010 — रमेश अय्यर** (Hindi, health floater) — rejected CI-rider claim, Clause R1.3.
- **Thin customers** (`is_hero: false`) — structured rows only, for statistical N and
  cross-customer leak tests. No authored documents.

## The five data domains (purpose-limitation unit)

`policy_coverage · claims · billing · interaction_history · profile_account`. Every document is
tagged with its domain; retrieval enforces `customer_id ∧ allowed-domains` (the intent→domain
map) as a hard pre-filter *before* scoring — so a query naming another customer's policy id can
never widen scope.

## Language coverage

2 of 3 heroes are authored in Hindi (Devanagari) with real clause phrasing; 1 in English; the
correspondence includes a Hinglish thread. This is the core wedge: grounding a Hindi query in
the customer's own Hindi/English policy text.

## How the structured rows stay consistent

The `policies` / `claims` rows in `manifest.yaml` carry the same clause refs, amounts, and
statuses as the authored documents. The mock core-system store seeds reads from these rows; the
RAG index cites the documents. Same numbers, two surfaces — that internal consistency is the
point. See `DIRT.md` for the planted edge cases.
