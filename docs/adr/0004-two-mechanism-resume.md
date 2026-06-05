---
status: accepted
---

# Two distinct resume mechanisms: confirmation vs crash

"Suspend/resume" is split into two mechanisms instead of one, because conflating
them creates the write cross-product the PRD flags as its #1 risk. **Crash resume**
(worker dies mid-run) uses the LangGraph checkpoint + Redis `XAUTOCLAIM` — same
logical run, recovers in seconds. **Confirmation/clarification resume** (the system
needs the human to answer "yes/no" or clarify) does NOT pause an in-memory run:
it **persists a `pending_write` on the conversation and ends the run** (worker
freed, status `awaiting_confirmation`/`awaiting_input`, `needs_input` SSE emitted);
the customer's reply arrives as a **brand-new run** that detects the persisted
state and re-validates the token for free.

## Consequences

- "Resume under an expired token" cannot happen for confirmations — a fresh run
  always reads the current token, so identity re-validation is structural, not
  bolted on.
- Idempotency keys are **conversation-anchored** (`sha256(conversation_id + tool +
  canonical_args + intent_nonce)`), not `run_id`-anchored — the current
  `f"{run_id}:{action}"` key would double-write across a confirmation resume
  because the run id changes. The `intent_nonce` is stamped when the `pending_write`
  is created and persisted with it, so a retry reuses the key while a genuinely new
  intent gets a fresh one.
- **Context is immortal, authority is mortal**: the transcript + `close_reason` +
  `closed_at` are retained forever, but a `pending_write` past its TTL is marked
  `abandoned` and never auto-fires — resume re-earns step-up + confirm. The dedup
  window is tied to the pending-write TTL so both expire together.
