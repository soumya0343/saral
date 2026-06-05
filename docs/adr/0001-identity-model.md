---
status: accepted
---

# Identity model: token-derived customer, consumed not minted

Saral embeds in a host bank/insurer app that already authenticates the customer,
so Saral **consumes** an inbound session token (validates signature + claims;
`sub` → customer id) and never mints trust from the message body or derives a
customer id from message content. The `Customer` is the only graph actor; the
`Operator` is audit-only (who handled an escalation, when) and never drives a turn
or fires a tool — this removes the "execution under operator token" cross-product
entirely. Step-up is a **declarative per-tool attribute** (`requires_step_up`, true
for all writes), and the bundled `/auth/session` + OTP endpoints are a labeled
**test harness simulating the host app**, not a claimed production IdP.

## Considered Options

- **Saral owns the IdP + OTP outright** (the PRD's literal §11.5 framing) —
  rejected: overclaims an auth boundary that actually lives in the host app, and
  builds more than the embed reality needs.
- **Single `user_id`, operators can trigger writes** — rejected: forces a
  per-customer-retrieval-scoped-by-operator-token failure mode and a stale-token
  resume cross-product, for an actor the embed doesn't even have.

## Consequences

- Per-customer retrieval and all data scope are anchored to the conversation's
  token-derived customer id, never the live request body.
- Resume is always customer-driven (see [0004](0004-two-mechanism-resume.md)),
  so "execute under an operator's token" is not a state that can exist.
- `/auth/session` and `request_step_up`/`TEST_OTP` must be documented as a harness;
  presenting them as production auth would be dishonest.
