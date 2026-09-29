# Saral — Repo Audit & Improvement Roadmap

*Audit date: 2026-09-29 · Commit audited: `5ef1671` (main, clean tree)*

Covers: what is implemented, what is broken or only claimed, the nearby problems worth
solving, and a phased plan with exit criteria. Every finding points to a file/line, and the
ones marked **(repro'd)** were confirmed by running the code during the audit.

---

## Status (updated 2026-09-29)

| Phase | State | Notes |
|---|---|---|
| 0 — Lock the edge | ✅ done | S1–S8 fixed (bearer auth + ownership, login OTP, hashed/attempt-limited OTP, refresh rotation, self-service erasure, service-key endpoints, CORS, rate limits, prod-secret guard), negation-safe confirmation, default-deny retrieval, CI, mypy clean. |
| 6.1 — Free chain repair | ✅ done | R10: live-verified free models, per-role `provider:model`, reasoning params, startup probe, stub-fallback alert. |
| 1 — Correctness & reliability | ✅ done | 1.1 atomic seq counter + commit-before-enqueue · 1.2 retried/loud persist · 1.3 async Postgres checkpointer (crash-resume proven across processes; finished checkpoints deleted) · 1.4 mixed read+write answers the read · 1.5 OTP retry budget · 1.6 worker concurrency + per-conversation ordering + heartbeat, LLM deadlines/failover/breaker, LID ‖ classify · 1.7 replayable trace stream (run_id + Last-Event-ID) · 1.8 per-run token usage · 1.9 store calls off the event loop. |
| 2 — Honest eval | ✅ done | Judge sees the reply · reply-level metrics (language match, answer correctness, faithfulness, false-block rate, multi-turn, pass by language) · offline + live tiers (tier derived from config) · pinned live judge (different family, no fallback, abort + `--resume`) · 25 multi-turn conversations driven through the API's own resume logic · 34-case injection set (attacks + benign look-alikes) · human labels bound to exact live replies (κ only from those) · CI gate vs committed baseline (`data/eval_baselines/offline.json`). |
| 2 — first live run | ✅ 22-scenario sample (2026-09-29) | judge `groq:qwen/qwen3.8-27b` (pinned) · pass 63.6% (EN 71% · HI 50% · Hinglish 71%) · language match 91% · answer correctness 78% · faithfulness 100% · false-block 100% on the 2 look-alikes sampled · p50/p95 2.5 s / 22 s · 36k tokens. Judge **not yet validated** (0 human labels): run `make eval-labels` and label. Live-only defects found → Phase 3: **(L1)** LLM triage drops `get_claim_status` in Hindi/mixed messages and retrieval then answers about a *different* claim (CLM2010 for a CLM2001 question) — retrieval must honour the claim id asked about; **(L2)** Hindi "what does my policy cover" mis-routed to a policy lookup, English "Which policy ID?" reply; **(L3)** LLM phrasing drops the clause number ("6.2") from correct lapse explanations; **(L4)** p95 22 s — Gemini 503s + Groq 429s under a sequential suite. |
| 3 — Differentiator quality | ✅ mostly (see "Phase 3 — outcome") | L1–L3 fixed; 3.1/3.5/3.6/3.7/3.8 done; 3.2 without pgvector; 3.4 without NLI; 3.3 floor target not reachable with free local models; 3.9 rules plateau ~63% on blind attacks — classifier needed. Offline pass 63.6% → 87.0%, false blocks 50% → 0%. |
| 4 — Close the product loops | ✅ (see "Phase 4 — outcome") | Every escalation → complete, encrypted, deduplicated, queryable RM request with an English case summary; RM owns all writes; claim intake slot-filling; complaint set; Prometheus + `/ready`. OTel deferred. |
| 5 → 7 | ⏳ next | Phase 5 (pick adjacent problems; recommended B claim pre-adjudication, C lapse prevention). Open from Phase 3: injection classifier (needs HF licence), answerability check for the floor. |

Open items found while implementing: ~~the capability guard suppresses writes message-wide~~
(fixed in 3.6), Gemini free Flash is often overloaded
(503/slow — synthesis frequently served by Groq gpt-oss-120b; ~6–8 s), and hero U1003's data still
frames her as the deceased claimant (D11).

## 0. TL;DR

Saral has a strong design (identity gating, purpose-bound retrieval, suspend/resume,
hash-chained audit, a free-provider LLM layer, and an eval harness with κ gating) and clean
code hygiene (94/94 tests pass, ruff clean). But three gaps undercut what the README says it
does:

1. **The HTTP edge has no authentication.** Anyone can open a session as any `user_id`,
   read any conversation, stream any trace (including the OTP event), erase any user's data,
   or trigger an eval run. The identity model is sound inside the graph and absent at the door.
2. **The eval cannot catch failures of the differentiator.** It runs on the stub LLM, the hashing
   embedder, and regex triage. The judge never sees the reply text, and the rubric mirrors the
   labels. It scores 100% because of how it is built, not because it measures the live
   Gemini/e5/Sarvam path.
3. **Several reliability claims are not true in deployment.** The Postgres checkpointer is
   never installed, so the app silently falls back to in-memory checkpoints. A run is enqueued
   before its DB commit, which races with the worker. The groundedness score floor defaults to
   0, so the gate is off. And with `APP_ENV=prod` there is no way to deliver an OTP, so no
   write can ever complete.

Recommended order: **Phase 0 (lock down the edge) → Phase 1 (correctness races) → Phase 2
(make the eval honest) → Phase 3 (improve differentiator quality, now measurable) → Phase 4
(close product loops) → Phase 5 (adjacent problems) → Phase 6 (free & open-source model
stack v2; Phase 6.1 is urgent because free-tier drift already broke the default config) → Phase 7
(docs/deploy hygiene, continuous).**

---

## 1. Inventory — what is implemented today

Legend: ✅ works as described · 🟡 partial / works only on some paths · ❌ claimed but not working

| Area | Status | Notes |
|---|---|---|
| LangGraph supervisor graph (triage → compliance → identity → supervisor → rag/action/mixed → synthesis) | ✅ | [graph/build.py](../backend/saral/graph/build.py). Deterministic rule-based router; parallel fan-out for mixed. |
| Triage: LLM-primary intent + regex fallback, Sarvam `/text-lid` language, sticky language | 🟡 | LLM path is good. The regex fallback misses common Hinglish (see D6). Only EN/HI/Hinglish are supported. |
| Closed action set + `_normalize` clamp + capability-question guard | ✅ | A good safety pattern: the LLM understands the message, deterministic code decides what runs. |
| Compliance gate: injection regex, referenced-id ownership, per-action authz | 🟡 | Ownership check is solid. The injection regex has many false positives and is easy to bypass (S9). |
| Identity gate + step-up OTP + read-back confirmation + pending-write TTL | 🟡 | Flow is well modelled. OTP delivery, brute-force limits, and the confirmation parser are weak (S4, S5). |
| Intent→domain purpose-limitation map | 🟡 | Default-deny is not enforced for an empty domain list (S8). The history lexicon is too broad (D10). |
| Hybrid RAG (e5 + BM25 + RRF), per-customer hard pre-filter | 🟡 | Pre-filter design is right. Chunking drops headings, the score floor is off, and only 3 hero customers have documents (D1–D3). |
| Synthesis: deterministic draft + LLM rephrase + numeric/ID grounding check | 🟡 | Fallback text is English-only and dumps the top chunk. The grounding check ignores polarity (D4, D5). |
| Idempotent writes (conversation-anchored keys) | ✅ | [actions/confirm.py](../backend/saral/actions/confirm.py) and [tools/store.py](../backend/saral/tools/store.py). |
| RM request on escalation (human RM approves and performs all data writes) | ✅ (Phase 4) | **By design:** the agent never writes customer data; every escalation becomes a complete, encrypted, deduplicated, queryable RM request (D12 closed in Phase 4). |
| Redis Streams run bus, consumer group, XAUTOCLAIM | 🟡 | Works. One run at a time per worker, no DLQ, no MAXLEN (R5, S10). |
| Checkpoint/crash-resume | ❌ | The Postgres saver is never installed and the app silently falls back to memory (R2). |
| SSE trace stream | 🟡 | Uses pub/sub with no replay and no auth. Leaks PII and the OTP (R6, S3). |
| Persistence: runs, actions, audit chain, escalations, suspend custody | 🟡 | Enqueue-before-commit race can lose the entire persist (R1). |
| Hash-chained audit | 🟡 | Chains are per-run with no cross-run link, and the hash leaves out key fields (S11). |
| PII redaction (regex / Presidio) | 🟡 | Presidio runs English-only. Redaction misses the trace, the Redis stream, and the mock store (S10). |
| DPDP erasure + consent gate | 🟡 | Erasure skips the mock store, Redis, and escalation replies (S10). |
| Orphan reaper, retention purge | ✅ | [jobs/](../backend/saral/jobs/). |
| Eval: ~95 scenarios, judge, κ per language, regressions | ❌ as a measurement / ✅ as plumbing | Circular by construction (E1–E5). |
| Frontend: onboarding, chat, trace dev-mode, OTP popup, history sidebar, `/eval` page | ✅ | Works as a demo UI. |
| Deploy: Fly / Render / Vercel configs, Dockerfiles | 🟡 | Prod config makes writes impossible (S4) and asks for a checkpointer that is not installed (R2). |
| CI | ❌ | `.github/workflows/` is empty and untracked. ARCHITECTURE.md says CI exists. |

**Health snapshot:** `pytest` 94 passed · `ruff` clean · `mypy` **10 errors** (runbus return
type, `store.py` Optional access, `action.py` callable typing, `gate.py` `str | None`,
`worker/main.py` str-unpack, 2 unused ignores) · no CI · `docs/` and `.claude/` are gitignored,
so new ADRs and this file are not tracked unless force-added.

---

## 2. Findings — what is going wrong

Severity: **P0** = security or data-loss risk in the deployed demo · **P1** = breaks a headline
claim or the differentiator · **P2** = quality/ops debt.

### 2.1 Security (P0)

**S1 — No authentication on any endpoint.**
[routes.py:131](../backend/saral/api/routes.py#L131): `POST /conversations {user_id}` mints a
session token for *whatever* `user_id` is in the body. The client never sends a token back,
and every later call is keyed only by `conversation_id`. "Identity is token-derived, never
trusted from the message" holds inside the graph but is bypassed at the API.
*Fix:* the client holds the token and sends `Authorization: Bearer` on every call. Add a
FastAPI dependency that decodes it and checks that `conversation.user_id == claims.sub`.
`POST /conversations` derives the user from the token and never from the body.

**S2 — Account takeover by phone number.**
[store.py:409-421](../backend/saral/tools/store.py#L409): `identify_customer` returns an
existing account when the **mobile or email alone** matches. The name is not checked and there
is no OTP. Knowing someone's mobile number gives you their policy, claims, and conversation.
*Fix:* make "returning customer" login OTP-gated (see S4 for the delivery channel). Keep
tap-to-login for the three demo heroes behind an explicit `DEMO_MODE` flag.

**S3 — Unauthenticated destructive, expensive, or PII endpoints.**
- `DELETE /users/{id}/data` ([routes.py:356](../backend/saral/api/routes.py#L356)) lets anyone erase anyone.
- `POST /escalations/{id}/resolve` takes `operator_id` from the request body, so the operator is self-asserted.
- `POST /eval/run` ([eval_routes.py:17](../backend/saral/api/eval_routes.py#L17)) burns free-tier quota and is a DoS lever.
- `GET /users/{id}/conversations`, `GET /conversations/{id}` expose transcripts.
- `GET /conversations/{id}/stream` ([routes.py:321](../backend/saral/api/routes.py#L321)) broadcasts entities (mobile, email), passages, and the **OTP event** to any subscriber.
- CORS is `*` ([app.py:57](../backend/saral/api/app.py#L57)).

*Fix:* **no admin dashboard or operator UI is needed** (decided: none for now). The fix is to
stop exposing these endpoints publicly, not to build a UI on top of them:
- **Customer routes** (send/reply/stream/history/list): a customer-token dependency. The
  conversation must belong to `claims.sub`. This is not admin work; it is the same session token
  the chat already mints.
- **Erasure is customer self-service** (the DPDP right belongs to the data principal):
  `DELETE /me/data` with the customer token. `DELETE /users/{id}/data` is removed.
- **Server-to-server endpoints** (the RM's system setting a request status, replacing
  `/escalations/{id}/resolve`): a static `SERVICE_API_KEY` header from env, compared with
  `hmac.compare_digest`. The caller identity is fixed per key, not taken from the body. No UI; the
  RM's own system or `curl` calls it.
- **Eval:** `make eval` / CLI only. `POST /eval/run` is disabled when `APP_ENV=prod` (or needs
  `SERVICE_API_KEY`). The read-only `GET /eval/report(s)` stays public for the frontend `/eval`
  page, since reports hold only synthetic scenario data.
- CORS restricted to `FRONTEND_ORIGIN`. Per-IP + per-user rate limit (slowapi or a Redis
  token bucket).

**S4 — OTP: undeliverable in prod, brute-forceable, and weakly generated.**
- With `APP_ENV=prod` (set in [fly.toml](../fly.toml) and [render.yaml](../render.yaml)), `test_otp` is suppressed and there is no other channel. The customer can never receive a code, so **every write is impossible in the deployed app**. In dev, the code is shown in the same browser session, which proves nothing.
- `POST /auth/step-up` verification allows **unlimited attempts** inside a 5-minute TTL. A 6-digit code has 10⁶ options, and nothing rate-limits guesses.
- `challenge_id = f"chl_{user_id}_{ms}"` ([store.py:252](../backend/saral/tools/store.py#L252)) is predictable. The code is stored in plaintext and compared with `!=`, which is not constant-time.

*Fix:* free delivery via email (Resend or Brevo free tier, or SMTP) **or** TOTP (authenticator
app via `pyotp`, which needs no provider). Store `sha256(code + salt)` and compare with
`hmac.compare_digest`. Burn the challenge after 3–5 failures. Use `secrets.token_urlsafe`
for challenge ids. Keep the in-browser "simulated SMS" as an explicit `DEMO_MODE` harness.

**S5 — The confirmation parser can read a "no" as "yes" and fire a write (repro'd).**
[confirm.py:19-35](../backend/saral/actions/confirm.py#L19) checks YES tokens before NO tokens,
using a token-set intersection:

| Reply | Parsed |
|---|---|
| `जी नहीं` ("no, sir") | **yes** |
| `not ok` | **yes** |
| `haan nahi` / `nahi, ok nahi` | **yes** |
| `no wait yes` | yes |

This is the deterministic path. It runs with no LLM key, when the resume LLM call fails, and in
eval. *Fix:* any negation token → `no`. Accept yes only when there are YES tokens and **no** NO
tokens. Treat mixed or ambiguous replies as `unclear` and re-ask. Make `जी` neutral and require
`जी हाँ`/`ji haan`. Add table-driven tests.

**S6 — Sessions never expire; step-up authority is long-lived.**
[routes.py:147](../backend/saral/api/routes.py#L147) `_claims_or_refresh` silently re-mints an
expired token, so the session is effectively immortal. A step-up token lives for the full
30-minute session TTL. *Fix:* return 401 on expiry and let the host re-authenticate.

**History is unaffected.** Messages, the conversation list, sticky language, pending writes and RM
requests are keyed by `conversation_id` → `user_id` in Postgres, not by the token. Re-auth yields a
new token for the same `user_id`, and the customer resumes the same conversations with full history.
Move the token off the `Conversation` row to the client. For smooth UX, pair a short access
token (~30 min) with a rotating, revocable refresh token (7–30 days, httpOnly cookie): the frontend
catches the 401, calls `/auth/refresh`, and retries, so the customer notices nothing. How much
history is kept is set by retention (`message_retention_days`), not by session length. Give
step-up tokens a short TTL (about 5 minutes) and bind them to the specific `pending_write`
(claim: `act=<idempotency_key>`).

**S7 — Insecure defaults are not refused in prod.** The dev `session_secret` default is accepted
when `APP_ENV=prod`, and there is no `aud`/`jti`. *Fix:* add a settings validator that fails
startup in prod with the default secret.

**S8 — Purpose-limitation default-deny is violated for an empty domain set (repro'd).**
[customer_index.py:100](../backend/saral/rag/customer_index.py#L100):
`allowed = set(allowed_domains) if allowed_domains else None`. An empty list, which is exactly
the default-deny result for complaint, small-talk, or unknown intents, becomes `None`, which
means **no filter**. `search("claim", "U1001", [])` returns 4 personal passages. Passages with
`domain is None` also always pass. It is not reachable through today's routes, but it is one
routing change away from an ADR-0002 violation. *Fix:* `None` → deny, `[]` → deny, and require
a domain on every per-customer doc at load time. Add a regression test.

**S9 — The injection guard both over-blocks and under-protects (repro'd).**
[injection.py](../backend/saral/compliance/injection.py). All of these legitimate messages are
blocked and escalated:
- `मेरा क्लेम कब अप्रूव करेंगे?` ("when will my claim be approved?") matches `अप्रूव कर`
- `When will you approve my loan application?`
- `Are there no restrictions on nominee change?`
- `क्या कंप्लायंस टीम से बात हो सकती है?` (any mention of "compliance" in Devanagari)
- `You are now charging me twice!`

Meanwhile, trivial rephrasings, other scripts, and **indirect injection** get through. Retrieved
customer documents and correspondence go straight into the synthesis prompt without screening.
*Fix:* keep a high-precision regex tier for blatant cases only. Add an LLM classifier on the
triage call as a structured `injection_risk` field (free Groq). Screen retrieved passages the
same way. Wrap passages in delimiters with a "data, not instructions" system rule. Build an
adversarial test set with benign look-alikes to measure the false-positive rate.

**S10 — PII leaks outside the redaction boundary.**
- Trace events publish raw `entities`, full `passages`, and `run_started.message` over SSE ([worker/runner.py](../backend/saral/worker/runner.py)).
- The Redis stream stores the whole `RunRequest` (raw message plus 40-turn history) **forever**. `xadd` has no `MAXLEN` ([runbus.py:41](../backend/saral/runbus.py#L41)).
- The mock store keeps raw ticket subjects and claim notes (the raw message) and user mobile/email.
- Erasure ([compliance/erasure.py](../backend/saral/compliance/erasure.py)) touches none of these, nor `Escalation.operator_reply`.
- Presidio is called with `language="en"` only ([pii.py:64](../backend/saral/compliance/pii.py#L64)), so Devanagari names and addresses are never detected. The regex misses Devanagari digits.

*Fix:* redact the trace payloads (show a masked preview in dev mode only). Use `XADD MAXLEN ~`
plus `XDEL` after ack. Extend erasure to the store, Redis, and escalations. Add Hindi-aware
PII: IndicNER, or a Presidio custom recognizer set plus a Devanagari digit map.

**S11 — Audit tamper-evidence is weaker than claimed.** Each run starts its own chain at
GENESIS ([repository.py](../backend/saral/db/repository.py) → `audit.build_chain`), so deleting a
whole run's audit rows is undetectable. The hash covers only `prev|seq|decision|actor|reason`.
`run_id`, `conversation_id`, `user_ref`, `args_hash`, `reason_code`, and the timestamp can all
be edited without breaking the chain. *Fix:* one chain per tenant, or per conversation with a
per-tenant anchor, serialized with `SELECT … FOR UPDATE` on a chain-head row. Hash a canonical
JSON of all columns. Publish a daily root hash (log line or file) as an external anchor.

### 2.2 Correctness & reliability (P0/P1)

**R1 — Enqueue-before-commit race loses whole runs (P0).**
`send_message` and `reply` call `enqueue_run` *inside* the request, but the DB session commits
only after the handler returns ([db/session.py](../backend/saral/db/session.py), `yield` then
`commit`). The worker can finish first, especially on the fast deterministic or suspend paths:
- `persist_run` computes `max(sequence_num)+1` before the user message is committed, so both
  rows get the same seq. The unique index `ix_messages_conversation_seq` fails, and `persist_run`
  raises. [`_persist`](../backend/saral/worker/runner.py) **swallows** the error, so the run
  record, actions, audit chain, escalation, and suspend custody are all lost.
- In `reply`, `convo.suspend_status = None` ([routes.py:313](../backend/saral/api/routes.py#L313))
  commits *after* the worker may already have set a new `awaiting_confirmation`. The API
  overwrites it, and the customer's next "yes" gets `409 not awaiting a reply`.

*Fix:* commit before enqueue (explicit `await db.commit()` then `enqueue_run`), or use an
outbox table plus a relay. Allocate message sequence numbers from a per-conversation counter
row under `FOR UPDATE`, not `max+1`. Make persist failures loud: retry, then dead-letter, and
emit an `error` trace.

**R2 — Crash-resume across processes is not real (P1).**
[checkpoint.py:26-29](../backend/saral/graph/checkpoint.py#L26):
`langgraph-checkpoint-postgres` is not in `pyproject.toml`/`uv.lock`. Even if it were, the code
uses the **sync** `PostgresSaver` with an async graph (`aget_state`/`astream`), and
`from_conn_string` returns a context manager. The `except` silently falls back to `MemorySaver`,
so Fly/Render's `CHECKPOINT_BACKEND=postgres` does nothing and a reclaimed message restarts from
scratch. *Fix:* add the dependency. Use `AsyncPostgresSaver` opened in the worker lifespan.
**Fail loudly** in prod if the configured backend cannot load. Add an integration test that
kills a worker mid-run (docker-compose) and asserts the resume.

**R3 — Mixed read + write drops the read (P1).**
[build.py:321-328](../backend/saral/graph/build.py#L321): any write intent sends the whole run to
suspend. "What is my claim status, and update my mobile number", the PRD §1 headline example,
answers only the write flow and never the claim status. Only `writes[0]` is handled, so a
second write is dropped silently. *Fix:* run the read/info branches and fold their answer into
the suspend message ("Your claim CLM2010 is partially approved… To update your number I need to
verify it's you."). Queue additional writes as a list of `PendingWrite`s confirmed one at a time.

**R4 — One mistyped OTP escalates to a human (P1).**
[build.py:396](../backend/saral/graph/build.py#L396): any `challenge_response` with auth still
below step-up escalates immediately. *Fix:* allow N attempts (the same counter as S4), then
re-send or escalate.

**R5 — Throughput and latency make NFR-1 (p95 < 8 s) unenforceable (P1).**
- The worker handles one run at a time (`count=1`, awaited sequentially) ([worker/main.py](../backend/saral/worker/main.py)).
- LLM calls: 60 s timeout × (1 + `llm_retry_cap`=2) × every provider in the chain. Retries fire even on non-retriable 4xx (401/400). There is no backoff and no `Retry-After` handling. A new `httpx.AsyncClient` is created per call ([openai_compat.py](../backend/saral/llm/openai_compat.py)).
- There is no circuit breaker, so a dead provider is retried on every turn.
- Triage, Sarvam LID, resume interpretation, and synthesis run serially.

*Fix:* bounded concurrency in the worker (an `asyncio.Semaphore(N)` of tasks, ack on
completion). Per-role deadlines (triage ~3 s, synthesis ~6 s) instead of a 60 s timeout. Retry
only 429/5xx/timeouts, with jittered backoff and `Retry-After`. Share an HTTP client. Add a
simple circuit breaker (open for 60 s after K failures). Run Sarvam LID concurrently with
intent classification (`asyncio.gather`).

**R6 — The SSE stream can lose events.** Redis pub/sub is fire-and-forget. Events published
before the browser subscribes are gone, and the frontend works around this with a 1 s `onopen`
timeout ([page.tsx:402](../frontend/app/page.tsx#L402)). *Fix:* a per-conversation Redis
Stream (`XADD trace:{cid} MAXLEN ~200`) read with `XREAD` from a `Last-Event-ID`, which gives
replay and reconnect for free.

**R7 — Shared mutable singletons are unsafe under concurrency.** `_synth.last_tokens`,
`FallbackLLM.last_tokens`, and the key round-robin pointer are module-level singletons.
With in-proc mode or R5's concurrency, token counts and key rotation get mixed across runs.
*Fix:* return usage as part of the call result instead of storing it on the instance.

**R8 — Sync DB I/O on the event loop.** `ComplianceGate.evaluate → check_authorization →
Store` is sync SQLAlchemy called directly in an async node. `identify_customer` and
`get_customer_context` are sync in async routes. *Fix:* use `asyncio.to_thread` now, and an
async store later.

**R9 — The loop guard is dead code.** The graph is a DAG with no cycles, so
`max_step_count` never triggers. Either remove it or add a real loop, such as a synthesis
self-check → re-retrieve (see D5).

**R10 — Free-tier drift has already broken the default provider config (P0 for the live path).**
Checked 2026-09-29 against provider docs:
- **Groq:** `groq_model = "llama-3.3-70b-versatile"` ([config.py](../backend/saral/config.py)) is
  **no longer on Groq's free tier**. Llama 3.1 8B and 3.3 70B moved to enterprise-only on
  2026-08-16. The triage role's first provider fails on every call.
- **Cerebras:** the free tier is now a **$5 trial credit that expires after 30 days**, not a
  permanent daily allowance. The second provider in the triage chain disappears after a month.
- **Result:** live triage silently falls through to the **stub**, which is the regex path. The
  LLM-primary understanding described in the README is not running, and nothing alerts on it.
- **Gemini:** `gemini-2.0-flash` is no longer listed on the Gemini free-tier pricing page (free
  listings are 2.5/3.x Flash, Flash-Lite and Gemma 4). Google no longer publishes free-tier limits.
  Reports range from ~20 req/day for 3.x Flash to ~500/day for Flash-Lite, so check in AI Studio.
  Free-tier Gemini content **is used to improve Google's products**, so only redacted text should
  be sent.
- **Sarvam:** gives ₹100 of credit on signup, then bills (LID ₹3.5 per 10k chars). It is a trial,
  not a free tier.
- **Multi-key rotation** ([openai_compat.py](../backend/saral/llm/openai_compat.py)) does not
  add capacity on Groq, whose limits are per organization. Spreading load across several accounts
  would likely breach provider terms.

*Fix:* Phase 6.1: update model ids, add a startup provider/model probe, alert when the chain
lands on the stub, and add a budget-aware router.

### 2.3 Differentiator quality (P1) — per-customer multilingual grounding

**D1 — The groundedness gate is off by default.** `retrieval_score_floor = 0.0`
([config.py:85](../backend/saral/config.py#L85), `.env.example`). "Below the floor → escalate,
never invent" never fires. *Fix:* calibrate on a labeled relevant/irrelevant set for e5 (see
Phase 3). Store a per-embedder floor and log the score distribution.

**D2 — Chunking drops section headings.** Both chunkers ([rag/index.py:30](../backend/saral/rag/index.py#L30),
[customer_index.py:35](../backend/saral/rag/customer_index.py#L35)) drop any block that is a lone
`#…` line. Markdown headings are followed by a blank line, so **every** `## Section` and
`### Clause 6.2` heading is discarded and chunks lose their clause context. *Fix:* use a
heading-aware splitter that prepends the heading path (`Policy Schedule › 6. Lapse › 6.2`) to
each chunk and records `clause_id` metadata. Cite as `doc#clause-6.2` rather than `doc#chunk3`.

**D3 — Personal grounding exists for only 3 customers.** `CustomerIndex` loads hero docs from
disk once at boot. Sandbox customers (created at onboarding) and the 12 thin customers have
**no** personal documents. A claim filed via `file_claim` produces no adjudication note, so
there is nothing to ground on. *Fix:* build an ingestion pipeline. Generate a policy schedule
from a template per product plus the structured row, in EN/HI. Write documents to Postgres
(pgvector) with `customer_id`/`domain`/`language`/`clause_id` columns. Re-index on write. Keep
the files as seed data only.

**D4 — Fallback answers are English chunk dumps.** When the LLM is unavailable or its phrasing
fails the grounding check, `compose()` returns the top chunk truncated at 280 characters behind
"Based on our policy (doc#3):". `_action_summary` ignores `lang`
([synthesis.py:61](../backend/saral/agents/synthesis.py#L61)), so a Hindi customer gets
"Claim CLM2030 status: rejected (...)" in English. The degraded notice and the
`needs_clarification` strings are English-only. *Fix:* localize all deterministic templates
(EN/HI/Hinglish tables, as `build.py` already does). Make the deterministic answer
extractive-by-clause (pick the sentence with the highest query overlap) instead of a
truncated chunk.

**D5 — The grounding check is numeric only.** `_grounded` checks that numbers and IDs appear
in the sources. Polarity flips ("is covered" vs "is not covered"), wrong clause attribution,
and invented steps all pass. `citations` lists *every* retrieved passage, not the ones used.
*Fix:* have the LLM return `{message, used_citation_ids[], claims[]}` as structured output.
Verify each claim against its cited passage with a cheap NLI/entailment check (a second
free-tier call, or a local multilingual NLI model such as `MoritzLaurer/mDeBERTa-v3-base-xnli`).
Reject and fall back on any unsupported claim. Show only the used citations.

**D6 — Language detection has gaps.**
- The regex fallback misses common Hinglish. `claim reject kyun hua` is detected as **en**
  (repro'd), and that is the README's own Hinglish demo query. `_HINGLISH_MARKERS`
  ([triage.py:41](../backend/saral/agents/triage.py#L41)) lacks `kyun/kyu/hua/tha/raha/gaya/wala/aap/hum/ka/ki/ke/se/mein`.
- Sarvam LID results for other Indic languages (ta, bn, mr…) are mapped to EN/HI by script, so
  the reply comes back in the wrong language with no notice.
- The rule "Devanagari → HI" misclassifies Marathi and Nepali.

*Fix:* expand the markers and use a scored marker ratio rather than any-hit. Add an explicit
`UNSUPPORTED` language with a polite bilingual reply. Plan for Marathi, Tamil, and more via
Sarvam (Phase 5).

**D7 — The generic corpus is a toy.** 10 English docs totalling **736 words**. Hindi generic
questions rely on cross-lingual e5 against almost nothing. *Fix:* expand to ~40–60 docs with
Hindi parallels for the most common FAQs, and add an "answerable/unanswerable" label for
floor calibration.

**D8 — Data model mismatches.**
- `get_claim_status` with no claim id replies "You don't have any claims on file yet", even
  when the customer has claims. Any `ToolError` returns the same text
  ([synthesis.py](../backend/saral/agents/synthesis.py), [action.py](../backend/saral/agents/action.py)).
- `_context` and `file_claim` pick *the first* policy ([store.py](../backend/saral/tools/store.py)).
  Multi-policy customers and the PRD story "what about my other policy?" are unsupported.

*Fix:* add `list_claims` and `list_policies` read tools. On a missing id, disambiguate against
the customer's own list ("You have 2 claims: CLM2010 (partially approved), CLM2011 (filed) —
which one?").

**D9 — Hindi prompts assume a female customer.** `चाहती हैं` / `chahti` are hard-coded
([build.py:74-106](../backend/saral/graph/build.py#L74)). *Fix:* use gender-neutral phrasing
(`आप … सेट करना चाहेंगे/चाहेंगी` → rephrase to `कृपया नया मोबाइल नंबर बताएं`) or a profile
salutation field.

**D10 — Triage lexicon precision.**
- `_HISTORY` ([triage.py:100](../backend/saral/agents/triage.py#L100)) includes `before`, `pehle`,
  `previous`, and `earlier`. "What happens to my claim before surgery?" sets `wants_history`,
  which **authorizes the interaction-history domain** for an unrelated question: a
  purpose-limitation over-grant.
- Substring matching: `hi` is inside "this"/"which", `miss` inside "permission", `cover` inside
  "discover".
- COMPLAINT intent has no route. An angry customer gets the generic "How can I help…" with no
  ticket and no escalation.

*Fix:* use word-boundary regexes. Narrow the history phrases to full expressions ("last
time I called", "pichli baar"). Route COMPLAINT to empathy + offer a ticket + escalate on
repeat.

**D11 — The demo data breaks its own domain.** Hero U1003 (Meena) chats about **her own death
claim**, which was filed by the nominee. The system has no concept of claimant ≠ policyholder.
*Fix:* short term, reframe the data (the nominee is the customer and gets a claimant role on
the policy). Long term, add a party/role model (Phase 5-G).

**D12 — The RM request is not actionable.**

**Design (decided):** every escalation creates a request in Saral's backend for the customer's
relationship manager (RM), a human. The RM approves and performs **all** write access to the
customer's data. Saral never applies the change itself, and approving or applying happens
outside Saral. That part is correct and stays.

Gaps *inside* that design:
- **The requested change is lost.** The `escalations` row stores only `pending_action="update_contact"`
  ([models.py `Escalation`](../backend/saral/db/models.py), [schemas.py `EscalationRecord`](../backend/saral/schemas.py)).
  It does not store the field or the new value. `persist_run` clears `conversation.pending_write`
  on the terminal escalated status, and the stored transcript has the number redacted to
  `<MOBILE>`. The RM receives "customer wants a contact update" with no way to know the new number.
- **No customer reference on the row.** It has only `conversation_id`, so the RM has to join
  through the conversation.
- **No dedup.** Asking twice creates two RM requests. The `idempotency_key` already on the
  `PendingWrite` is not used for the request.
- **No request status.** The customer cannot ask "what happened to my request?", and there is no
  `open / done / rejected` field that the RM's side can set.
- **Inconsistency to confirm:** today only `update_contact` becomes an RM request.
  `raise_ticket` and `file_claim` still *execute* in Saral's mock store on confirmation
  ([build.py `_RM_APPROVAL_ACTIONS`](../backend/saral/graph/build.py#L212)). If the RM owns
  **all** data writes, add these two to the set as well (see Decisions).
- `file_claim` (if it stays in Saral) collects nothing: no incident date, amount, hospital, or
  documents. `last_updated="2026-06-05"` is hard-coded. `raise_ticket`'s subject is the raw
  message, which may contain PII.

*Fix:* Phase 4.3, which makes the RM request complete, deduplicated and queryable, plus claim-intake
slot filling.

### 2.4 Evaluation (P1) — why "100% on all metrics" is not evidence

**E1 — The judge never sees the answer.** `actual_outcome()` ([metrics.py:19](../backend/saral/eval/metrics.py#L19))
returns `status/route/tools/citations/escalated`, not `message`. "Resolution accuracy" is
therefore label matching. Groundedness means "has at least one citation", and
explanation-groundedness means "cited any personal doc". Nothing checks that the reply is
correct, faithful, or in the right language.

**E2 — The eval measures the offline skeleton.** `conftest.py` and `make eval` pin the stub
LLM, the hashing embedder, and the regex triage. The live path (Gemini phrasing, e5 retrieval,
Groq triage, Sarvam LID), which is where the differentiator lives, is never scored. The
deterministic judge's rubric mirrors the same labels humans wrote, so κ is high by design.

**E3 — Missing dimensions.**
- No **language-match** metric (FR-1) and no **faithfulness** metric on text.
- **0 multi-turn scenarios** (no `history` anywhere in the YAML), so follow-ups, clarification
  loops, OTP/confirm/resume, topic switches, and "samajh nahi aaya" are unevaluated.
- No false-positive rate for the injection guard (S9).
- Cost is `0.0`, and tokens are counted only for synthesis.

**E4 — The judge is not pinned.** `llm_role_judge = "gemini,stub"`. If Gemini rate-limits in the
middle of a suite, the judge silently swaps to the stub, which contradicts ADR-0003.

**E5 — No regression gate.** There is no CI, `data/eval_reports/` is gitignored, and "regressions
vs prior" compare only against whatever JSON happens to be on local disk.

### 2.5 Ops / DX / docs drift (P2)

- No CI at all. The mypy errors have accumulated because of it.
- Docs are out of sync with code:
  - ARCHITECTURE.md §8 lists `llm_provider_order = sarvam,anthropic,stub` and says Sarvam is primary chat. Config says Sarvam is LID-only and the order is `gemini,groq,cerebras`.
  - DEPLOY.md still tells you to set `LLM_PROVIDER_ORDER="sarvam,anthropic,stub"`.
  - `CONVERSATION_MEMORY_TURNS` is 8 in the docs and 40 in code.
  - The docs reference `.github` CI and `.claude/CONTEXT.md`, which are untracked or ignored.
- `docs/` is in `.gitignore`, so new docs and ADRs will not be committed unless force-added.
- Mock-store tables are created by `create_all` plus ad-hoc `_ensure_columns`, not Alembic.
- Two DB drivers exist for one DB: async asyncpg for the app and sync psycopg for the store.
- There is no `/ready` probe (DB/Redis/embedder loaded), no metrics (Prometheus/OTel), no
  request rate limits, and no DLQ.
- Verify model ids are still served. `gemini-2.0-flash` may be retired on the free tier, and
  Cerebras availability varies by key. Add a startup `GET /models` check that logs the
  resolved chain.
- `StarletteDeprecationWarning` in tests (httpx testclient).

---

## 3. Adjacent problems worth solving

Chosen to (a) extend the per-customer multilingual grounding differentiator, (b) stay on free
tiers, and (c) be credible for a regulated Indian insurer or lender.

| # | Problem | Why it matters | Fit with Saral | Effort |
|---|---|---|---|---|
| **A** | **RM request quality (not an RM console)** | The RM works outside Saral by design. What Saral controls is how good the request it hands over is. | An LLM-written, clause-cited case summary attached to each RM request, in English for the RM even when the chat was in Hindi. Customer-side status lookup ("what happened to my request?"). A full console with operator replies is **out of scope** by decision. | S |
| **B** | **Claim-intake copilot with pre-adjudication** | Many rejections come from missing documents or excluded items. A mistake prevented is worth more than one explained. | Slot-fills the claim (date, amount, hospital) in Hindi/Hinglish. Checks each item against *this customer's* exclusions/sub-limits before filing ("room rent above 1% of SI is proportionately deducted under your Clause 4.1"). Document checklist per product. OCR on uploaded bills with local Tesseract `hin+eng`. | L |
| **C** | **Lapse prevention & revival** | Hero U1003's whole story is a claim rejected for lapse. Retention is revenue. | Proactive grace-period reminders in the customer's language, a revival quote grounded in Clause 6.3, a deterministic premium-due calculator, and an opt-in consent purpose for outreach. | M |
| **D** | **Grievance & appeal drafting with regulatory timelines** | Regulators require acknowledgement and resolution within fixed timelines, with a path to the Insurance Ombudsman or RBI Integrated Ombudsman. *(Verify current IRDAI/RBI timelines before encoding them.)* | Draft an appeal grounded in the rejection clause and appeal window (Clause 8.1), open a grievance with a timeline tracker, and remind the customer about escalation rights. | M |
| **E** | **Lending actions** | The corpus has loan/EMI docs but no loan tools. | Deterministic `get_loan_summary`, `foreclosure_quote`, and `emi_schedule`, plus a bounced-EMI explanation grounded in the loan agreement. Keep collections out of scope, or follow the RBI Fair Practices Code (calling hours, language). | M |
| **F** | **Voice & WhatsApp channels** | Voice/IVR and WhatsApp are the dominant support channels in India, especially outside metros. | Channel adapters feed the same run bus. Sarvam STT/TTS for voice, via free credits. WhatsApp through Meta Cloud API test numbers. The PRD lists voice as a non-goal, so this is a deliberate scope change. | L |
| **G** | **Multi-party identity (nominee / claimant / co-borrower)** | Real claims are often filed by someone other than the policyholder (D11). | Party-role model on policies/claims, role-scoped domains in the purpose map, relationship verification in step-up. | M |
| **H** | **Compliance-owner analytics** | PRD persona with no product surface today. | Dashboard: block reasons, injection FP rate, escalation reasons and SLA breaches, complaint topics by language, audit-chain verification status. | S–M |
| **I** | **DPDP data-principal rights beyond erasure** | DPDP gives rights to access, correction, and grievance, and purpose-specific consent. | "What data do you hold on me?" (grounded export), correction requests, a per-purpose consent ledger with receipts. | M |

**Recommendation:** do **A → B → C** first. A is small and makes every escalation useful to the
RM who receives it. B is the strongest possible extension of "grounded in *your* policy": it moves
the system from explaining rejections to preventing them. C turns U1003's story into a proactive
feature. F is the biggest reach win but the biggest scope change, so decide it explicitly.

---

## 4. Roadmap

Each task lists **Where**, **How**, and **Done when**. Keep the offline stub/hashing floor for CI
throughout. Real providers stay free-tier (ADR-0003).

### Phase 0 — Lock the edge and restore hygiene (≈2–3 days) · P0

| # | Task | Where | Done when |
|---|---|---|---|
| 0.1 | Bearer-token auth dependency on every customer route; `user_id` comes from the token; conversation ownership check | `api/routes.py`, new `api/deps.py`, `frontend/app/page.tsx` | Test: token for U1001 cannot read, post to, stream, or reply on U1003's conversation (403). `POST /conversations` ignores the body `user_id`. |
| 0.2 | Close the admin surface **without a dashboard**: `DELETE /me/data` (customer token, self-service erasure) replaces `DELETE /users/{id}/data`; `SERVICE_API_KEY` header for the RM-status endpoint (replaces `/escalations/{id}/resolve`, no body-supplied operator); `POST /eval/run` off in prod (use `make eval`); `GET /eval/report(s)` stays public | `api/routes.py`, `api/eval_routes.py`, new `api/deps.py`, `config.py` | No unauthenticated caller can erase, resolve, or trigger eval. A customer can erase only their own data. No new UI. |
| 0.3 | OTP hardening: hashed code, constant-time compare, max attempts, random challenge id; `DEMO_MODE` flag controls the in-browser OTP; real channel = email (free SMTP/Resend) or TOTP | `auth/stepup.py`, `tools/store.py`, config | Brute-force test locks after 5 attempts. The prod config can complete a write end-to-end. |
| 0.4 | Returning-customer login requires OTP (hero tap-login only under `DEMO_MODE`) | `store.identify_customer`, `/customers` route | Knowing a mobile number alone gives no access. |
| 0.5 | Fix `parse_confirmation` (negation wins; ambiguous → unclear; `जी` neutral) | `actions/confirm.py`, tests | Table test covering the 6 repro cases plus 20 more. |
| 0.6 | Default-deny for empty/None domains; require a domain per doc | `rag/customer_index.py` | `search(..., [])` → `[]`. Test added. |
| 0.7 | Strip PII from trace events (dev-mode masked preview); `XADD MAXLEN`; OTP event only to the authenticated owner | `worker/runner.py`, `runbus.py` | Grep test: no raw mobile/email in published events. |
| 0.8 | Refuse the default secret in prod; lock CORS to `FRONTEND_ORIGIN`; per-IP/user rate limit | `config.py`, `api/app.py` | Prod boot fails with the default secret. 429 on burst. |
| 0.9 | CI: GitHub Actions running `ruff`, `mypy` (fix the 10 errors), `pytest`, offline `make eval` with a regression threshold | `.github/workflows/ci.yml`, un-ignore `docs/` | Green CI on a PR. `docs/` is tracked. |

### Phase 1 — Correctness & reliability (≈1 week) · P0/P1

| # | Task | Where | Done when |
|---|---|---|---|
| 1.1 | Commit-then-enqueue (or an outbox table + relay); per-conversation seq counter under `FOR UPDATE` | `api/routes.py`, `db/repository.py`, migration 0007 | Stress test with 200 fast deterministic turns has 0 persist failures. The reply→suspend race test passes. |
| 1.2 | Make persist failure loud: retry, then emit an `error` trace, then a metric | `worker/runner.py` | Injected DB failure is visible to the client and in logs as ERROR. |
| 1.3 | Real async Postgres checkpointer; fail fast in prod if unavailable | `pyproject.toml` (+`langgraph-checkpoint-postgres`), `graph/checkpoint.py`, `worker/main.py` lifespan | docker-compose kill-test: SIGKILL the worker mid-synthesis, restart, and the run completes from its checkpoint. |
| 1.4 | Mixed read+write: answer the reads and include them in the suspend prompt; multi-write queue | `graph/build.py` (identity/supervisor split: run rag/action-read, then suspend in synthesis) | "Claim status + update my number" returns the status *and* asks for the OTP. |
| 1.5 | OTP retry budget (3) before escalation | `graph/build.py`, `api/routes.py` | One typo → "That code didn't match, 2 tries left". |
| 1.6 | Worker concurrency (semaphore N=4) + per-role deadlines + retry only 429/5xx/timeout with backoff + shared HTTP client + circuit breaker; run LID ‖ classify in parallel | `worker/main.py`, `llm/openai_compat.py`, `llm/factory.py`, `agents/triage.py` | Live p95 < 8 s on a 50-turn smoke. A dead provider costs < 1 s after the breaker opens. |
| 1.7 | Replayable SSE via a per-conversation Redis Stream + `Last-Event-ID` | `runbus.py`, `api/routes.py`, frontend | Subscribing after the run finished still delivers all events. Reconnect resumes. |
| 1.8 | Remove singleton mutable usage; return usage with results | `llm/*`, `agents/synthesis.py` | Concurrency test shows correct per-run token counts. |
| 1.9 | Sync store calls off the event loop | `compliance/gate.py`, `graph/build.py`, routes | Event-loop blocking check (aiodebug or a simple timer) stays clean. |

### Phase 2 — Make the eval honest (≈1 week) · P1 — *do before tuning quality*

| # | Task | Where | Done when |
|---|---|---|---|
| 2.1 | Pass `message`, `language`, used citations, and cited passage texts into the judge context | `eval/metrics.py::actual_outcome`, `eval/judge.py` | The judge prompt contains the reply text. |
| 2.2 | New metrics: **language_match** (deterministic script/marker check + LID), **faithfulness** (claim-level entailment vs cited passages), **answer_correctness** vs `expected.answer_facts` (key facts per scenario, e.g. `["Clause 6.2", "lapsed", "01-Dec-2025"]`) | `eval/metrics.py`, `eval/schemas.py`, scenarios YAML | Metrics appear in the report and the `/eval` UI. |
| 2.3 | **Two eval tiers**: `eval-offline` (stub/hashing, CI, deterministic) and `eval-live` (real Gemini/Groq/Sarvam + e5, throttled, run nightly or manually, report committed) | `Makefile`, `eval/runner.py` (rate limiter, resumable) | The live report exists and shows honest (not 100%) numbers with a baseline. |
| 2.4 | Pin the judge: a single provider/model, **no stub fallback** in live mode (abort the suite on judge failure); use a different model family from synthesis (e.g. Groq Llama judge vs Gemini synthesis) to limit self-preference | `config.py`, `eval/judge.py` | A judge outage aborts the suite with a clear error. |
| 2.5 | Multi-turn scenarios: 25+ conversations with `turns: [...]` covering follow-ups, "samajh nahi aaya", clarify loops, OTP right/wrong, confirm/cancel, topic switch mid-suspend, mixed read+write | `data/scenarios/multiturn.yaml`, runner support | They run in both tiers. |
| 2.6 | Injection FP/FN set: 40 attacks (incl. indirect via docs) + 40 benign look-alikes (the S9 repros) | scenarios | The report shows block rate **and** false-positive rate. |
| 2.7 | Human labels on *live* outputs (≥ 30 per language) for κ; label on text, not on status | `data/scenarios/labels_live.yaml`, small labeling script | κ per language computed against live outputs. |
| 2.8 | Commit eval reports (or keep them in the `eval_results` table) and gate CI on offline metrics plus a stored live baseline | CI, `eval/store.py` | A regression fails the PR. |

### Phase 3 — Differentiator quality (≈1–2 weeks) · P1 — now measurable

| # | Task | Where | Done when (measured by the Phase 2 live eval) |
|---|---|---|---|
| 3.1 | Heading-aware, clause-aware chunker; `clause_id` metadata; clause-level citations | `rag/index.py`, `rag/customer_index.py`, shared `rag/chunking.py` | Personal citations look like `U1003/claims/CLM2030.md#6.2`. Explanation-groundedness (live) ↑. |
| 3.2 | Move retrieval to Postgres + **pgvector** (free, already in stack) with a `customer_id/domain/language/clause_id` pre-filter in SQL; ingestion API + templated doc generation for any customer; re-index on write (filed claim → adjudication stub) | `rag/`, migration, `tools/store.py` | A new sandbox customer gets grounded, clause-cited answers. |
| 3.3 | Calibrate `retrieval_score_floor` for e5 on labeled answerable/unanswerable queries; add a cross-encoder reranker only if needed (e.g. `bge-reranker-v2-m3`, multilingual, local) | `config.py`, `scripts/calibrate_floor.py` | Unanswerable → escalate ≥ 90% with answerable recall ≥ 95%. |
| 3.4 | Structured synthesis output `{message, used_citations, claims[]}` + claim-level entailment check (a second free call or local mDeBERTa-xnli); show only used citations | `agents/synthesis.py` | Faithfulness (live) ≥ 95%. Polarity-flip adversarial set is caught. |
| 3.5 | Localize every deterministic template (action summaries, degraded notice, clarifications); gender-neutral Hindi; extractive fallback answer | `agents/synthesis.py`, `graph/build.py`, `agents/action.py` | Language-match = 100% on the offline tier (no English leaks for HI/Hinglish). |
| 3.6 | Triage precision: word-boundary lexicons, expanded Hinglish markers with a ratio score, narrowed history phrases, COMPLAINT route, `UNSUPPORTED` language reply | `agents/triage.py`, `graph/build.py` | `claim reject kyun hua` → hinglish. "before surgery" does not grant the history domain. Tests added. |
| 3.7 | `list_claims` / `list_policies` tools + disambiguation; multi-policy seed data | `tools/`, `agents/action.py`, manifest | "What about my other policy?" works. A missing claim id asks which claim. |
| 3.8 | Expand the generic corpus to ~50 docs with HI parallels for the top 15 FAQs | `data/policy_corpus/` | Generic HI questions have a grounded answer in the live eval. |
| 3.9 | Injection defense v2: Prompt Guard 2 86M (free, local; see 6.3d) on the message + passage screening + delimited context | `agents/triage.py`, `compliance/injection.py`, `agents/synthesis.py` | FP rate on benign look-alikes < 2% with block rate at 100% on the attack set. |

#### Phase 3 — outcome (2026-09-29)

| # | State | What shipped / what the numbers say |
|---|---|---|
| L1 | ✅ | Retrieval honours the claim/policy the customer named: manifest docs carry `about: [CLM…, POL…]` and a named id drops documents about a *different* id of that kind (`rag/customer_index._anchored`). Triage adds a deterministic floor under the LLM: an explicit, id-bearing read it drops is restored (`triage._reconcile`). |
| L2 | ✅ | A policy "lookup" with no id and no lookup phrasing is an information question (`_reconcile`); id-less reads answer for all of the customer's own rows instead of "Which policy ID?". |
| L3 | ✅ | Prompt keeps clause numbers; `ensure_clause` re-appends the clause the answer rests on if the model drops it. |
| 3.1 | ✅ | `rag/chunking.py`: heading path attached to every paragraph (and embedded with it), `clause_id` + clause refs; citations `policy_schedule.md#4.1`. Claim notes cite by position (they *mention* 6.2, they aren't clause 6.2). |
| 3.2 | ◐ | Generated EN+HI policy schedule / claim note for every policy and claim without an authored doc (`rag/customer_docs.py`), rebuilt when the rows change — sandbox / thin customers and newly filed claims are groundable. **pgvector deferred**: in-memory per-customer indexes are enough at demo scale and keep free hosting simple. |
| 3.3 | ❌ target not met | `make calibrate-floor` on 66 answerable / 47 unanswerable queries: e5 floor 0.827 keeps 95.5% answerable but escalates only 83% unanswerable, and would wrongly escalate 6/35 short real scenario questions. A free 118M multilingual cross-encoder (mmarco-mMiniLMv2) was *worse* (28% at the same recall; 0/11 Hinglish) and was removed. Floor stays 0.0. Next: bge-reranker-v2-m3 (568M) or an LLM answerability check. |
| 3.4 | ◐ | Sources/actions passed as delimited `<source>` / `<action>` data; model returns `USED: S1,S2` → only used citations shown; deterministic polarity check (covered ↔ not covered, देय ↔ देय नहीं) rejects flipped phrasing on top of the numeric/id check. No NLI model / second LLM call (quota + latency on free tiers). |
| 3.5 | ✅ | Every deterministic template localized (action summaries, not-found, degraded notice, default help); gender-neutral Hindi clarify prompts; extractive fallback picks the best-matching sentence across the top passages (clause-bearing sentences favoured for "why"; metadata headers skipped). Remaining offline language misses are cross-language documents (EN customer ↔ Hindi hero docs, all Hinglish) — the offline tier has no translator; the live LLM does that. |
| 3.6 | ✅ | Whole-word lexicons (`hi` ∉ "this", `cover` ∉ "discover", `do i` ≠ "do it"); scored Hinglish detection (`claim reject kyun hua` → hinglish); history domain only on real past references; capability guard per sentence ("How do I pay? Also update my email…" keeps the write); meta follow-ups ("samajh nahi aaya") → information; COMPLAINT → empathy + ticket offer, repeat → escalation `repeated_complaint`; `UNSUPPORTED` language (Tamil/Bengali/Urdu script, Sarvam non-en/hi) → bilingual reply, no reads/writes. |
| 3.7 | ✅ (changed) | `list_claims` / `list_policies`; customer context fills a single id only when unambiguous. Instead of asking "which claim?", an id-less read **answers for all** of the customer's claims/policies (a status question has an answer for each). U1002 now holds two policies. |
| 3.8 | ✅ | Generic corpus 10 → 53 docs (28 new EN, 15 HI parallels), numbers cross-checked against existing docs and hero files; new facts invented for fictional products are listed in the agent report. Customer-language version preferred when both surface. |
| 3.9 | ◐ | Guard v2 matches attack *structure* (override, persona, fake role tags, prompt extraction, bulk exfiltration, verification bypass, impersonation+privileged ask) after normalization (NFKC, zero-width, spaced letters incl. Devanagari, leetspeak); lone "approve my refund" is no longer an attack (no approve tool exists). Passage screening drops retrieved chunks carrying model-directed instructions. Optional local classifier hook (`INJECTION_CLASSIFIER_MODEL`, Prompt Guard 2 — gated, needs HF_TOKEN). In-suite: 16/16 attacks, 0/18 look-alikes. **Blind held-out** (`make eval-heldout`): set 1 first read 17/35 attacks / 0/35 FP (v1: 8/35, 1/35), then used for tuning; set 2 first read **25/40 attacks, 1/40 FP** (v1: 6/40, 0/40). Target (100% / <2%) needs the classifier. |

**Offline tier (154 scenarios):** pass 63.6% → **87.0%** (EN 84→93%, HI 54→93%, Hinglish 41→71%) · attacks blocked 89→**100%** · false blocks 50→**0%** · language match 72→88% · answer correctness 31→77% · multi-turn 96→100% · routing 99→100%. New baseline committed-to-be in `data/eval_baselines/offline.json`.

**Live tier (same 22-scenario sample as the first live run, judge `groq:qwen/qwen3.8-27b`):**
pass 63.6% → **86.4%** (EN 71→71% · HI 50→88% · Hinglish 71→100%) · language match 91→100% ·
answer correctness 78→100% · attacks blocked 50→100% · false blocks 100→0% · faithfulness 94%
(the one miss was a clause number appended to a reply that used no source — fixed after the run,
re-check passes) · p50/p95 4.6 s / 21 s · 41k tokens. L1 confirmed fixed live: "क्लेम CLM2001 का
स्टेटस?" now answers CLM2001 correctly (its only miss, a mixed route, is fixed and re-checked).
Still failing: a health-only customer asking "when will you approve my loan?" gets a generic
offer to help (should say there's no loan on file + general timelines); and one judge error — it
failed `mt_mixed_read_write_en` for not repeating the claim status answered two turns earlier.
The judge is still **unvalidated** (0 human labels) — `make eval-labels` is the next human step.

### Phase 4 — Close the product loops (≈1–2 weeks) · adjacent problem A + D12

| # | Task | Done when |
|---|---|---|
| 4.1 | **Case summary on every RM request:** a grounded, clause-cited summary (what was asked, what was tried, why it escalated, relevant clauses), written in English for the RM. Generated by the free synthesis chain, with a deterministic fallback. | Every `escalations` row has a summary. No PII beyond what the RM needs. |
| 4.2 | *(Out of scope by decision: the RM works outside Saral.)* Operator console and operator replies in the chat. Revisit only if the RM workflow moves into Saral. | — |
| 4.3 | **Complete RM request.** Migration adds `user_id`, `requested_change` (field + new value, **encrypted at rest**: Fernet/pgcrypto key from env, readable only via the `SERVICE_API_KEY` endpoint that the RM's system calls), `idempotency_key` (**unique**, so asking twice gives one request), and `status` (`open` / `done` / `rejected`, set by the RM's system through the same service-key endpoint; no Saral UI). Erasure tombstones `requested_change`. Customer tool `get_request_status`. If the RM owns all writes, add `raise_ticket` / `file_claim` to `_RM_APPROVAL_ACTIONS`. | The RM can see the new number. A duplicate ask does not create a second request. The customer can ask for status in Hindi and get it. |
| 4.4 | Claim intake v1: slot-filling (incident date, amount, hospital/garage, description) with multilingual clarifications; the read-back lists all slots | A filed claim carries structured fields, not the raw message. |
| 4.5 | Complaint path: empathy + offer ticket + escalate on repeat/severity | The complaint scenario set passes. |
| 4.6 | Observability: OTel traces per node, Prometheus metrics (latency per node/provider, 429s, breaker state, escalations by reason), `/ready` probe | A Grafana/Fly metrics dashboard or JSON endpoint shows them. |

#### Phase 4 — outcome (2026-09-30)

| # | State | What shipped |
|---|---|---|
| 4.3 | ✅ | **Every escalation is a request for the RM** (`saral/rm/requests.py`, table `rm_requests` beside the core-system store): customer ref, kind (`update_contact` / `file_claim` / `raise_ticket` / `handoff`), reason, SLA, status `open`/`done`/`rejected`, RM note for the customer. The requested change (field + new value, claim facts, ticket subject) is **Fernet-encrypted at rest** (`RM_REQUEST_KEY`; prod refuses to boot without it) and decrypted only by the service-key endpoints `GET /rm/requests`, `GET /rm/requests/{id}`, `POST /rm/requests/{id}/status`. **Dedup:** unique partial index on the change's key while open — the same ask is caught *before* a second OTP and answered with the open request's reference. New graph node `handoff` raises the request for any escalation and gives the customer its reference; migration 0009 links `escalations.request_id` / `user_id`. Customer tool `get_request_status` ("मेरे अनुरोध की स्थिति?") shows status + RM note in their language; another customer's id is simply not found. Erasure tombstones the change and summary. **Decision 6 taken:** the RM owns *all* writes — `file_claim` and `raise_ticket` are RM requests too (`RM_APPROVAL_ACTIONS`, reversible). |
| 4.1 | ✅ | English case summary on every request (`saral/rm/summary.py`): customer, chat language, redacted words, what was requested (never the value), intents, tools tried, compliance blocks, relevant sources, why it escalated. Rewritten by the free synthesis chain when available, kept only if every number/id traces to the facts. |
| 4.4 | ✅ | Claim intake (`actions/claim_intake.py`): policy (asked only when the customer holds several), incident date (dd/mm/yyyy, "12 Sep", "कल"/"kal"/"yesterday"; no future / >3-year dates), amount (₹, "1.2 lakh", "45 हज़ार"), hospital/garage, what happened — over as many turns as needed, localized asks, cancel / topic switch honoured, a human after 3 rounds. Read-back lists every fact; the RM request carries them structured. An optional LLM pass fills gaps, re-validated. |
| 4.5 | ✅ | Complaint path (from 3.6) + Hindi/Hinglish complaint words; scenario set EN/HI/Hinglish: empathy + ticket offer, escalation to an RM `handoff` on repeat. |
| 4.6 | ◐ | Prometheus: `saral_node_seconds{node}`, `saral_llm_calls_total{provider,outcome=ok\|rate_limited\|timeout\|error}`, `saral_llm_breaker_open{provider}`, `saral_llm_served_by_stub_total`, `saral_escalations_total{reason}`, `saral_rm_requests_total{kind,created}`, `saral_runs_total{status}`. API `/metrics` + `/ready` (DB + Redis); the worker serves its own on `WORKER_METRICS_PORT`. **OTel traces deferred** — structured logs already carry run_id per node. |
| 4.2 | — | Out of scope by decision (no RM console). |

**Offline tier:** 171 scenarios (17 new in `data/scenarios/rm_requests.yaml`) · pass 88.3% · RM-request accuracy 100% · every other gated metric at or above the Phase 3 baseline.

**Live tier (16 Phase 4 scenarios, real free models):** first run 10/16 — it found three real
bugs the offline tier can't see, all fixed with regression tests: Sarvam LID calls short
romanized Hindi ("mera mobile number badal do") English, so Hinglish customers got English
read-backs (now: ≥2 distinctive Hindi words override an `en` verdict); the Hindi read-back said
"पंजीकृत mobile" (field names now localized); the LLM added an info intent to a repeat Hinglish
complaint so it never escalated (`_reconcile` keeps a pure complaint a complaint). The other
misses were the judge not knowing that handing a confirmed change to the RM is the correct
outcome, and a harness transcript showing a bare "yes" where OTP had been verified — the judge
instructions now describe the RM design (so live numbers before/after this change aren't
strictly comparable) and the transcript says "[verified with OTP] yes". Re-run of the six:
6/6. Judge still unvalidated (0 human labels).

### Phase 5 — Adjacent problems (pick 2–3; ≈1–2 weeks each)

- **B · Claim-intake copilot with pre-adjudication:** exclusion/sub-limit checker over the
  customer's own clauses. Document checklist per product. Upload + local OCR
  (Tesseract `hin+eng`) → line items → per-item "likely payable / deducted under Clause X".
  Eval: synthetic bills with known outcomes.
- **C · Lapse prevention & revival:** scheduled job (worker cron) → due/grace detection →
  outreach message in the customer's language, gated by an outreach consent purpose. Revival
  quote grounded in the revival clause. In-chat "pay now" stub.
- **D · Grievance & appeal:** appeal draft grounded in the rejection clause plus the appeal
  window. Grievance ticket with a regulatory timeline tracker (verify current IRDAI/RBI timelines
  first). Ombudsman information when timelines lapse.
- **E · Lending tools:** deterministic loan summary, foreclosure quote, EMI schedule, bounced-EMI
  explanation grounded in the loan agreement.
- **F · Channels:** WhatsApp adapter (Meta Cloud API test number) and a voice prototype (Sarvam
  STT → run bus → Sarvam TTS). Record an ADR, since this reverses a PRD non-goal.
- **G · Multi-party identity:** party-role model (holder / nominee / claimant / co-borrower),
  role-scoped domains, and the U1003 data reframed with the nominee as the customer.
- **H · Compliance dashboard** and **I · DPDP access/correction + per-purpose consent ledger**.

### Phase 6 — Free & open-source model stack v2 · free-only

**Constraint (firm):** free only. No paid APIs, and no "cheap but paid" ones. **Jev (TypeSafe)
was evaluated and rejected:** it has no free tier ($0.042/M tokens, $5 starter credit), is
US-hosted, and is English-first with unmeasured Hindi quality. Its useful idea, *typed,
calibrated decisions instead of free-form JSON*, is reproduced for free in 6.3 with local
models. Trial credits (Cerebras $5 for 30 days, Sarvam ₹100) are treated as **temporary bonuses,
never load-bearing**.

**Design principle:** make the pipeline *need fewer LLM calls*. Hosted free LLMs are used only
where generation is unavoidable (Hindi phrasing, standalone-query rewrite, the eval judge).
Everything that is a **classification or a check** moves to small open-source models running
locally: unlimited, free, in-India, deterministic enough for CI.

#### 6.0 Free landscape (checked 2026-09-29 — limits change often, re-verify before relying on them)

| Provider (free, no card) | Useful models | Limits | Notes for Saral |
|---|---|---|---|
| **Groq** | `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`, `meta-llama/llama-prompt-guard-2-86m` / `-22m`, `whisper-large-v3(-turbo)` | Chat models: 30 RPM · **1K RPD · 8K TPM · 200K TPD, per model, per org**. Prompt Guard: 30 RPM · **14.4K RPD** · 500K TPD. | Quota is per model, so assigning *different models to different roles* legitimately triples capacity. **TPD is the real ceiling:** 200K ÷ ~2K tokens/call ≈ 100 calls/day/model. |
| **Google AI Studio (Gemini API)** | Gemini 2.5/3.x Flash, **Flash-Lite**, **Gemma 4** | Unpublished per-project limits (reports: ~20 RPD on 3.x Flash, ~500 RPD on Flash-Lite, varies) | Best free Hindi generation. Free-tier content **is used to improve Google's products**, so send redacted text only. Read the real limits in AI Studio. |
| **OpenRouter `:free` models** | Rotating set (Gemma / Qwen / Llama / DeepSeek variants) | 20 RPM · **50 RPD** (1K RPD needs a one-time $10 purchase, which we do not make) | A last-resort fallback only. The model set changes without notice. |
| **Cloudflare Workers AI** | Llama / Qwen / Gemma / gpt-oss, BGE embeddings, rerankers, Whisper | **10K neurons/day** shared (≈ 1.3K small-LLM responses) | A good extra fallback pool, and a reranker option if the host has no spare CPU. |
| **Mistral (La Plateforme free)** | Small / Medium / Large | ~$10/month free credit. The larger "Experiment" quota **requires opting into training on your data**. | Use the plain free credit only. **Never opt in to training with customer text.** |
| Cerebras, Sarvam | gpt-oss-120b, qwen-3.8-27b / Sarvam 105B, LID, translate | **Trials:** $5 for 30 days / ₹100 once | Bonus only. Do not depend on them. |

**Open-source, run locally** (free and unlimited; CPU is fine at Saral's volume):

| Model | Size | Replaces / adds | Why |
|---|---|---|---|
| **AI4Bharat IndicLID** | small (fastText + IndicBERT) | Sarvam `/text-lid` (a paid trial) + regex fallback | Covers **native and romanized** scripts for all 22 scheduled languages (47 classes). It is the first language ID for romanized Indic text, so it handles Hinglish and flags Marathi, Tamil and others instead of forcing them into EN/HI (D6). |
| **Llama Prompt Guard 2 86M** (mDeBERTa) | 86M | The false-positive-heavy injection regex (S9) + passage screening | Evaluated on attacks in **Hindi** and 7 other languages. Runs locally, or on Groq's free tier at 14.4K RPD. |
| **multilingual-e5 embeddings + classifier head** (SetFit or logistic regression, temperature-scaled) | reuses the loaded e5 | Triage intent + flags (capability question, history reference, in-scope, complaint severity, resume kind, yes/no confirmation) | The free, local version of Jev's typed decisions: calibrated probabilities, fixed label set, ~10 ms, trained on our own EN/HI/Hinglish data. |
| **bge-m3** *or* **multilingual-e5-base/large** | 568M / 278M / 560M | e5-small | bge-m3 led 8 of 13 Indian languages in a 2025 study, while e5-large was best on Hindi. Choose with the Phase 2 eval, not by reputation. bge-m3 also gives sparse vectors and an 8K context. |
| **bge-reranker-v2-m3** | 568M | Nothing today; would sit on top of RRF | A multilingual cross-encoder over the top ~20 candidates. Better ordering, and its score is a real relevance signal for the score floor (D1). |
| **mDeBERTa-v3-base-xnli** (or an equivalent multilingual NLI model) | 278M | Numbers-only grounding check (D5) | Claim-level entailment against the cited passage. XNLI training data includes Hindi. |
| **IndicNER** (AI4Bharat) + regex + Devanagari-digit mapping | ~BERT-base | English-only Presidio (S10) | Finds Hindi names and locations that Presidio misses. The GLiNER multi-PII models do **not** list Hindi; test them before relying on them. |
| **IndicTrans2** (AI4Bharat) | 200M distilled / 1B | Nothing (runs offline, not in the request path) | Generates Hindi parallels of the generic corpus (D7) and Hindi versions of templated customer docs (3.2), with human spot-checks. |
| **Langfuse (self-hosted OSS)** | — | Nothing | LLM call tracing, token/quota dashboards, prompt versions tied to eval runs. |

Not viable on free compute: self-hosting a generative LLM for serving. Sarvam 30B/105B have
open weights under Apache 2.0 but need a GPU. Keep them in mind only if free GPU hosting appears.

#### 6.1 Urgent: repair the free chain (R10) · do this during Phase 0

| # | Task | Where | Done when |
|---|---|---|---|
| 6.1a | Update defaults: triage → Groq `openai/gpt-oss-20b` → `qwen/qwen3.8-27b` → local classifier; synthesis → Gemini Flash-Lite / 2.5 Flash (verified in AI Studio) → Groq `openai/gpt-oss-120b` → Workers AI → localized deterministic template; judge → Groq `openai/gpt-oss-120b` **pinned, no fallback**. Remove Cerebras and Sarvam from default chains; keep them as optional bonus providers. | `config.py`, `.env.example`, docs | No default model id points at a model that isn't on a free tier. |
| 6.1b | Startup probe: call each provider's `GET /models`, log the resolved chain per role, and **fail health** (or at least WARN loudly) if a role resolves to stub-only while keys are set. | `llm/factory.py`, `/ready` | A retired model id shows up at boot, not as silent quality loss. |
| 6.1c | Metric + trace flag whenever a turn is served by the stub although real providers are configured. | `llm/factory.py`, runner trace | Visible in dev-mode trace and logs. |

#### 6.2 Budget-aware routing (stretch the free quotas)

| # | Task | Done when |
|---|---|---|
| 6.2a | **Quota ledger in Redis:** per provider × model counters for RPM/RPD/TPM/TPD, filled from the `x-ratelimit-*` response headers where sent, otherwise counted locally. The router **skips a provider before it returns 429** and honours `Retry-After`. | No 429 storms in a 500-turn soak. The chain degrades gracefully. |
| 6.2b | **Cut tokens per call:** synthesis history 40 turns → rolling summary + last 6 turns; drop the JSON-schema-in-prompt where the provider supports native `response_format` schemas; compress retrieved passages to their matching clauses. | Median synthesis prompt below ~1.2K tokens (≈ 2× more calls inside Groq's 200K TPD). |
| 6.2c | **Spread roles across models,** since quotas are per model: triage on `gpt-oss-20b`, synthesis fallback on `gpt-oss-120b`, judge on `qwen3.8-27b` or `gpt-oss-120b`. The eval runs off-peak with its own daily cap. | Eval never starves live traffic. |
| 6.2d | **Response cache** keyed by (normalized query, language, customer-doc version, intent) for generic-corpus FAQ answers, with TTL. Never cache across customers for personal answers. | Repeat FAQ turns cost 0 LLM calls. |
| 6.2e | Remove multi-account key rotation. Keep single-org keys per provider. | Compliant with provider terms. |

#### 6.3 Local decision models (the free "Jev-style" layer) — cuts ~1–2 LLM calls per turn

| # | Task | Where | Done when |
|---|---|---|---|
| 6.3a | Labelled triage set: ~600 messages (200 each EN/HI/Hinglish) covering intents, mixed intents, capability questions, resume replies, yes/no incl. negations (S5), out-of-scope, history references, complaint severity. 20% held out. Seed with the scenario YAML plus redacted real turns, and extend with hand-checked paraphrases from a free LLM. | `data/triage_bench/` | Reviewed; 50-item double-label check. |
| 6.3b | `DecisionClient` interface (`choice / score / flag` → value + calibrated probability) with a `LocalE5Head` implementation: SetFit or logistic regression on the e5 embeddings already loaded, temperature scaling fitted on the held-out split, always with an explicit `other` class. | `backend/saral/decide/` | ECE ≤ 0.05 on held-out data. ~10 ms per decision on CPU. |
| 6.3c | Triage v2: IndicLID for language → local head for intent + flags → call the LLM **only** when the head's confidence is below a threshold or a `search_query` rewrite is needed (a multi-turn reference). The deterministic AND-guards stay (capability → no write; negation → no confirm). | `agents/triage.py`, `actions/resume.py`, `actions/confirm.py` | LLM triage calls per turn drop ≥ 60%. Per-language macro-F1 ≥ the current LLM triage on the Phase 2 eval. 0 capability→write errors. |
| 6.3d | Prompt Guard 2 (86M) as the primary injection signal on the message **and on retrieved passages**. The regex shrinks to a few blatant patterns. | `compliance/injection.py` | FP rate on benign look-alikes (S9 repros) < 2% with 100% block on the attack set. |

#### 6.4 Retrieval and grounding upgrades (all local)

| # | Task | Done when |
|---|---|---|
| 6.4a | Embedder bake-off on our EN/HI/Hinglish retrieval set: e5-small (current) vs e5-base vs e5-large vs bge-m3. Measure recall@5, MRR, latency and RAM. | Winner chosen by data. |
| 6.4b | Add bge-reranker-v2-m3 over the top 20. Calibrate `retrieval_score_floor` on the **reranker** score. | Unanswerable → escalate ≥ 90% at ≥ 95% answerable recall (D1). |
| 6.4c | Local multilingual NLI for claim-level faithfulness (D5), used alongside the structured `{message, used_citations, claims}` synthesis output. | Faithfulness ≥ 95% on live eval; the polarity-flip set is caught. |
| 6.4d | IndicTrans2 offline job: Hindi parallels for the top generic FAQs + templated per-customer docs; human spot-check 10%. | Generic HI questions grounded in Hindi passages. |
| 6.4e | IndicNER + regex + Devanagari digits in the PII layer (S10). | Hindi names redacted in the stored transcript test. |

#### 6.5 Hosting the local models for free

The full local set (IndicLID, e5 or bge-m3, reranker, Prompt Guard, NLI, IndicNER) needs roughly
2–3 GB RAM on CPU. Options, in order of preference:
1. Run it inside the worker process if the host plan allows the RAM.
2. Run it as a small separate inference service (`/embed`, `/rerank`, `/classify`, `/lid`,
   `/nli`, `/ner`) on a free CPU host such as a Hugging Face Space or Oracle Cloud Always Free ARM
   (check current terms and India-region availability).
3. Cloudflare Workers AI for embeddings and reranking only, as a fallback within its 10K
   neurons/day.

Models load once at boot, and their versions are pinned in config and recorded in every eval
report.

#### 6.6 Exit criteria for Phase 6 (all measured by the Phase 2 live eval)

- Zero required spend. Every default provider is free-tier, or it is a local model.
- ≤ 1 hosted-LLM call per typical turn, down from 2–3 today. ≥ 100 turns/day sustained on the
  free quotas, with graceful degradation beyond that.
- Per language (EN / HI / Hinglish), triage, language-match and faithfulness are equal to or
  better than before the swap.
- Injection false-positive rate < 2% at 100% block on the attack set.
- A retired model or an exhausted quota is visible within one request, not discovered weeks later.

*Sources (checked 2026-09-29):* [Groq rate limits](https://console.groq.com/docs/rate-limits) ·
[Groq free-tier change (Llama removed 2026-08-16)](https://klymentiev.com/blog/groq-pricing) ·
[Gemini API pricing (free tier, data use)](https://ai.google.dev/gemini-api/docs/pricing) ·
[Gemini free limits reports](https://pecollective.com/tools/gemini-free-tier-guide/) ·
[Cerebras rate limits](https://inference-docs.cerebras.ai/support/rate-limits) ·
[Sarvam pricing](https://docs.sarvam.ai/api/getting-started/pricing) ·
[Sarvam 30B/105B open weights](https://www.sarvam.ai/blogs/sarvam-30b-105b) ·
[OpenRouter free limits](https://openrouter.zendesk.com/hc/en-us/articles/39501163636379-OpenRouter-Rate-Limits-What-You-Need-to-Know) ·
[Cloudflare Workers AI pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/) ·
[Mistral free tier](https://www.kdnuggets.com/5-free-llm-api-providers-you-can-use-in-2026) ·
[Llama Prompt Guard 2 86M](https://huggingface.co/meta-llama/Llama-Prompt-Guard-2-86M) ·
[IndicLID](https://github.com/AI4Bharat/IndicLID) ·
[bge-m3 vs e5 on Indian languages](https://arxiv.org/pdf/2506.01615) ·
[BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3) ·
[GLiNER multi PII (no Hindi listed)](https://huggingface.co/urchade/gliner_multi_pii-v1) ·
Jev (rejected, paid): [TypeSafe](https://typesafe.ai/blog/introducing-system-one-models-and-jev),
[TDS Banking77 test](https://towardsdatascience.com/jev-vs-llms-when-ai-moves-from-generation-to-decision-making/)

### Phase 7 — Docs & deploy hygiene (continuous)

- Re-sync README, ARCHITECTURE, and DEPLOY with the code (provider roles, memory turns, env vars).
  Add ADRs for: API auth model (0005), outbox/commit ordering (0006), pgvector store (0007),
  eval tiers + judge pinning (0008), and channels, if chosen (0009).
- Move mock-store tables into Alembic. Use one DB driver strategy.
- Add a startup provider check (`GET /models`) that logs the resolved chain and warns on retired model ids.

---

## 5. Decisions needed from you

1. **OTP channel for the deployed demo:** email (free tier, most realistic) vs TOTP
   (no provider, less familiar to users) vs keep the simulated popup under an explicit
   `DEMO_MODE` banner.
2. **Sandbox auto-signup:** keep "any name + mobile creates a customer" (easy demo, requires
   templated docs from 3.2) or restrict to seeded customers.
3. **Adjacent-problem picks for Phase 5:** recommended B + C (+ A already in Phase 4). Is
   voice/WhatsApp (F) in scope? It reverses a PRD non-goal.
4. **Live-eval budget:** how many free-tier calls per nightly run are acceptable (~95
   single-turn + 25 multi-turn ≈ 400–600 calls with judge + entailment)?
5. **Retrieval store:** move to pgvector (recommended, since Postgres is already there) vs keep
   in-memory plus a disk cache.
6. ~~**RM scope**~~ — decided in Phase 4: the RM owns *all* writes (`RM_APPROVAL_ACTIONS`
   defaults to all three; remove one to let Saral execute it).
7. **Where local models run:** the API/worker box (needs ~2–3 GB RAM for the Phase 6 local set),
   or a separate free inference host (Hugging Face Space CPU / Oracle Always Free ARM, subject to
   current terms)?

---

## Appendix — Reproductions run during the audit

```text
parse_confirmation: 'जी नहीं'→yes · 'not ok'→yes · 'haan nahi'→yes · 'nahi, ok nahi'→yes
detect_injection (all True): 'मेरा क्लेम कब अप्रूव करेंगे?' · 'When will you approve my loan application?'
  · 'Are there no restrictions on nominee change?' · 'क्या कंप्लायंस टीम से बात हो सकती है?'
  · 'You are now charging me twice!'
classify('What happens to my claim before surgery?') → [unknown], entities={'wants_history': True}
detect_language('claim reject kyun hua') → en
CustomerIndex.search('claim', 'U1001', allowed_domains=[]) → 4 personal passages (expected 0)
pytest: 94 passed · ruff: clean · mypy: 10 errors in 8 files
uv.lock: no langgraph-checkpoint-postgres (Postgres checkpointer silently falls back to memory)
```
