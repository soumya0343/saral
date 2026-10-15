# Saral — Architecture

> Saral (सरल — "simple") is a supervisor-orchestrated multi-agent system that resolves
> customer-support queries for regulated enterprises (insurance / lending) in **English,
> Hindi, and Hinglish**. Its differentiator is **per-customer document grounding**: answers
> cite *this customer's own* policy clause (e.g. "rejected under rider Clause R1.3"), not a
> generic FAQ.

This document describes the system as built (through roadmap Phase 4). For what was audited,
what changed in each phase and what is still open, see
[`docs/AUDIT_AND_ROADMAP.md`](docs/AUDIT_AND_ROADMAP.md); for deployment steps see
[`docs/DEPLOY.md`](docs/DEPLOY.md); for how to run it locally see the [README](README.md).

---

## 1. System at a glance

```
   Browser ──►  Next.js frontend (chat UI + /eval dashboard)
                        │ REST + SSE, bearer access token
                        ▼
                FastAPI (api/) ── sign-in, converse, reply, SSE, /ready, /metrics
                        │ XADD RunRequest                    ▲ GET/POST /rm/requests
                        ▼                                    │ (X-Service-Key)
                Redis Streams ── run bus + per-conversation   RM's own system
                        │        trace stream                (outside Saral)
                        │ XREADGROUP
                        ▼
                Worker (worker/) ── consume, checkpoint/resume, heartbeat, reaper
                        │
                        ▼
        LangGraph graph (graph/build.py)
        START → triage → compliance → identity → supervisor
                                                    ├─ rag ──────┐
                                                    ├─ action ───┤
                                                    ├─ rag + action (mixed, parallel)
                                                    ├─ respond ──┤
                                                    │            ▼
                                                    │        synthesis
                                                    │            ▼
                                                    └─ suspend ► handoff → END
                        │                         │                  │
                        ▼                         ▼                  ▼
                 PostgreSQL 16             Retrieval index      rm_requests
          conversations · runs ·       generic corpus + each    (requested change
          audit · escalations ·        customer's own docs      encrypted at rest)
          checkpoints · mock core
```

**Two runtimes, one image:** `api` (uvicorn) and `worker` (`python -m saral.worker.main`). The
API never runs the graph inline; it enqueues and the worker executes. That gives crash-resume,
horizontal scaling and partial-failure isolation. For local development `make dev` runs both in
one process (`RUN_WORKER_INPROC=true`). The live Hugging Face Space runs them as two processes in
one container, with Postgres and Redis alongside (§16).

**Saral never writes customer data.** A confirmed change, a claim, a ticket and every other
escalation becomes a request for the customer's relationship manager (RM), a human who works in
their own system outside Saral. There is no operator UI in this repository.

---

## 2. Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.12+ (uv-managed, `uv.lock`) |
| Web | FastAPI, uvicorn, `sse-starlette` |
| Orchestration | LangGraph `StateGraph`, Postgres checkpointer (`langgraph-checkpoint-postgres`) |
| DB | PostgreSQL 16 — SQLAlchemy 2 async + `asyncpg` for app tables, a sync engine for the mock core store and RM requests; Alembic |
| Queue | Redis 7 Streams (consumer group, `XAUTOCLAIM` recovery) |
| LLMs | Gemini and Groq free tiers over OpenAI-compatible HTTP; Sarvam for language detection only; Cerebras and Anthropic supported but not in the default chains; deterministic stub last |
| Retrieval | dense `multilingual-e5-small` (local) + `rank-bm25`, fused by Reciprocal Rank Fusion |
| Auth | `pyjwt` access tokens + rotating refresh tokens (mock IdP), OTP step-up |
| Encryption | `cryptography` (Fernet) for the RM request's requested change |
| Observability | `structlog`, `prometheus-client` |
| PII | regex (default) or Presidio (optional extra) |
| Frontend | Next.js 15, React 19, Tailwind 4 |
| Dev | ruff, mypy, pytest + pytest-asyncio, respx |

Every model key is optional. With none set the stub provider and the hashing embedder take
over, so the whole stack runs offline and reproducibly (CI and the offline eval tier use this).

---

## 3. Repository layout

```
sarvam/
├── backend/saral/          # application (see §4)
├── frontend/               # Next.js chat UI (app/page.tsx) + eval dashboard (app/eval/)
├── data/
│   ├── customers/          # manifest.yaml: 15 customers, 3 heroes with authored documents
│   ├── policy_corpus/      # 53 tenant-wide policy/FAQ documents, 15 with Hindi parallels (_hi.md)
│   ├── scenarios/          # 171 eval scenarios in 5 files + heldout/ (blind injection sets)
│   ├── rag_calibration/    # queries for the retrieval score-floor sweep
│   ├── eval_baselines/     # offline.json: the floor the CI gate enforces
│   └── eval_reports/       # JSON run reports (offline and live)
├── tests/                  # 29 pytest files + conftest (forces offline determinism)
├── scripts/                # calibrate_floor.py
├── deploy/hf-space/        # Hugging Face Space: supervisor app.py, build.sh, Space card
├── docs/
│   ├── AUDIT_AND_ROADMAP.md  # audit, phase outcomes, open decisions
│   ├── DEPLOY.md             # Fly / Render / Vercel runbook
│   └── adr/                  # 4 ADRs (see §16)
├── .github/workflows/ci.yml  # lint, typecheck, migrate, tests, eval gate, frontend build, image smoke test
├── Dockerfile.api          # one image: api (default CMD), worker, migrate; e5 weights baked in
├── docker-compose.yml      # local: postgres + redis + migrate + api + worker
├── fly.toml / render.yaml  # deploy targets
├── Makefile                # dev / test / eval targets (`make help`)
└── .env.example            # every config knob with a safe dev default
```

---

## 4. Backend packages (`backend/saral/`)

| Package | Responsibility |
|---|---|
| `api/` | FastAPI app; customer routes, auth routes, eval routes, RM routes; request-id middleware; rate limits; auth dependencies |
| `graph/` | `RunState`, the nodes and their wiring, checkpointer lifecycle |
| `agents/` | triage, rag, action, synthesis |
| `actions/` | write confirmation + idempotency key, reply-to-suspended-run planning, claim intake |
| `rm/` | RM request store, encryption of the requested change, English case summary |
| `llm/` | providers, per-role fallback chains, circuit breaker, token accounting, startup model probe |
| `rag/` | chunking, generic index, per-customer index, generated customer documents, embedders |
| `auth/` | mock IdP: access + refresh tokens, identity gate, OTP step-up |
| `authz/` | intent→data-domain map, which actions need step-up |
| `compliance/` | injection guard, authorization, the gate, PII redaction, audit chain, erasure |
| `tools/` | mock core insurer system (customers, policies, claims, tickets) behind a durable store |
| `db/` | SQLAlchemy models, repository, session, Alembic migrations |
| `eval/` | scenario loading, multi-turn driver, metrics, judge, κ agreement, gate, labels, held-out report |
| `worker/` | Redis consumer and single-run executor |
| `jobs/` | orphan reaper, retention purge |
| `metrics.py` | Prometheus counters and histograms |
| `config.py`, `schemas.py`, `runbus.py`, `logging.py` | settings, shared domain models, Redis run/trace bus, structured logging |

---

## 5. Request lifecycle

1. **Sign in** — `POST /customers` with a name and a mobile or e-mail. A new customer is created
   and signed in. An existing customer gets a login OTP and exchanges it at
   `POST /auth/login/verify`. Either way the client ends up with a short-lived access token (JWT)
   and a rotating refresh token.
2. **Start** — `POST /conversations` creates a conversation for the token's customer.
3. **Message** — `POST /conversations/{id}/messages` builds a `RunRequest`, adds it to the Redis
   stream and returns. The client reads `GET /conversations/{id}/stream` (SSE) for live trace
   events and the final response. Trace events live in a short per-conversation stream, so a late
   or reconnecting client replays what it missed.
4. **Execute** — a worker reads the request, loads any checkpoint (`thread_id` = `run_id`), runs
   the graph, publishes trace events and persists the run, its actions, the audit entries, any
   escalation and the suspend state in one transaction.
5. **Reply** — when a run suspends (`awaiting_input` for an OTP or a missing value,
   `awaiting_confirmation` for the read-back), `POST /conversations/{id}/reply` turns the
   customer's reply into the next run through `actions/turns.py`. The eval's multi-turn driver
   calls the same function, so the eval exercises the real resume logic.

The customer's identity comes only from the bearer token. Nothing in a request body or path can
name a different customer, and a conversation that belongs to someone else returns 404.

### HTTP endpoints

| Method · Path | Auth | Purpose |
|---|---|---|
| `POST /customers` | none (rate-limited per IP) | sign up, or start login for an existing customer |
| `POST /auth/login/verify` | none (rate-limited) | exchange the login OTP for tokens |
| `POST /auth/refresh`, `POST /auth/logout` | refresh token | rotate / revoke the refresh token |
| `GET /me`, `GET /me/conversations` | bearer | profile (with policy and claim ids) and conversation history |
| `POST /conversations` | bearer | start a conversation |
| `POST /conversations/{id}/messages` | bearer | send a message → enqueue a run |
| `POST /conversations/{id}/reply` | bearer | reply to a suspended run (OTP, yes/no, missing value) |
| `GET /conversations/{id}/stream` | bearer | SSE: trace events + final response |
| `GET /conversations/{id}` | bearer | full message history |
| `DELETE /me/data` | bearer | self-service erasure |
| `GET /rm/requests`, `GET /rm/requests/{id}` | `X-Service-Key` | the RM's system pulls requests (change decrypted, case summary) |
| `POST /rm/requests/{id}/status` | `X-Service-Key` | the RM's system reports `done` / `rejected` + a note |
| `POST /escalations/{id}/resolve` | `X-Service-Key` | legacy: record who handled an escalation |
| `POST /auth/step-up` | bearer | standalone step-up harness |
| `POST /auth/session` | dev/test only | mint a token for a user id (disabled in prod) |
| `POST /eval/run` | open in dev; in prod `X-Service-Key`, or anyone with `EVAL_RUN_PUBLIC` (3 runs per IP per hour) | run the offline tier in a child process (stub models, hashing embedder, in-memory store), whatever the server is configured with. The live tier is CLI-only (`make eval-live`) |
| `GET /eval/report`, `GET /eval/reports` | none | latest report, all reports |
| `GET /health`, `GET /ready`, `GET /metrics` | none | liveness, readiness (DB + Redis), Prometheus |

The service-key routes return 404 when `SERVICE_API_KEY` is unset.

---

## 6. The graph (`graph/`)

**State — `RunState`** (`graph/state.py`), main fields:

- Identity: `run_id`, `conversation_id`, `tenant_id`, `user_id` (token-derived), `auth_level`
- Input: `raw_message`, `history`, `known_entities`, `language_hint`
- Triage output: `language`, `intents`, `entities`, `search_query`, `allowed_domains`
- Routing and results: `route`, `retrieved`, `ungrounded`, `compliance_decisions`, `actions`,
  `final_response`
- Escalation: `escalation`, `rm_request_id`, `rm_request_kind`, `rm_request_created`
- Write custody: `pending_write`, `intent_nonce`, `step_up_owed`, `write_confirmed`,
  `suspend_prompt`, `challenge_id`, `otp_attempts_left`, `resume_reply`, `challenge_response`
- Bookkeeping: `degraded_agents`, `step_count`, `tokens_used` (additive reducers, so parallel
  branches can both write), `status`

`status` is one of `in_progress`, `awaiting_input`, `awaiting_confirmation`, `resolved`,
`escalated`, `degraded`, plus the abnormal closes set by the worker or reaper: `crashed`,
`timed_out`, `orphaned`, `abandoned`.

**Nodes:**

```
START
 → triage       language, intents, entities, standalone search query
 → compliance   injection guard, referenced-id ownership, per-action authorization
 → identity     allowed data domains; for a write: claim intake → OTP → read-back → confirm
 → supervisor   pick the route
     ├─ rag       retrieve (scoped to the customer + allowed domains), screen passages
     ├─ action    read tools; a write runs only once confirmed
     ├─ mixed     rag and action in parallel
     ├─ respond   straight to synthesis (small talk, blocked, complaint, unsupported language)
     └─ suspend   straight to handoff (waiting on the customer)
 → synthesis    the grounded, language-matched reply
 → handoff      an escalated run becomes an RM request; the reference is added to the reply
 → END
```

**What decides the path:**

- **Injection blocked** → nothing is read or executed; the reply refuses and escalates.
- **Write intent** → identity checks the auth level. Below step-up it mints an OTP and suspends.
  After a correct OTP it reads the change back and waits for yes/no. Five wrong codes burn the
  challenge and the run goes to a human.
- **Same change already open** → before any OTP, the graph looks for an open RM request with
  the same dedup key and answers with its reference.
- **Confirmed write** → actions listed in `RM_APPROVAL_ACTIONS` (default: all three writes) are
  not executed. The run ends `escalated` with reason `awaiting_manager_approval` and the handoff
  node raises the RM request.
- **Stale pending write** (older than `PENDING_WRITE_TTL_S`) → discarded; the customer must
  verify and confirm again (ADR-0004).
- **Nothing retrieved, or top score below the floor** → `ungrounded`; synthesis escalates
  instead of inventing an answer.
- **A specialist fails** → recorded in `degraded_agents`; the run continues and ends `degraded`.
- **Unsupported language** → a short bilingual reply, no retrieval.
- **Complaint** → empathy and a ticket offer first; a repeated complaint escalates.

---

## 7. Agents (`agents/`)

| Agent | Does | Notes |
|---|---|---|
| **Triage** | language, intents, entities, search query | A deterministic pass (script detection, scored Hinglish markers, word-boundary lexicons in three scripts, id/mobile/e-mail regexes) always runs. When a real model is available its result is fitted into the closed intent/action set and reconciled with the deterministic one: an explicit read the model dropped is restored, a pure complaint stays a complaint, and a write that appears only inside a capability question ("can I change my number?") is downgraded to information. Sarvam `/text-lid` is used for language detection only and is overridden when it labels clearly romanized Hindi as English. |
| **RAG** | scoped retrieval | Wraps `search_knowledge()`. Returns passages with citations. Never generates. |
| **Action** | tool calls against the mock core system | Reads run directly (`get_claim_status`, `get_policy_details`, list variants, `get_request_status`). A write runs only from a confirmed `PendingWrite`. Missing arguments produce a clarification, never a guessed call. |
| **Synthesis** | the reply | The facts come from a deterministic composer (EN / HI / Hinglish templates and an extractive answer from the best passage). A real model may rephrase it; the rephrase is kept only if it passes the checks in §9. |

Every agent has a deterministic path, so eval runs are reproducible with no API key.

---

## 8. Writes, claim intake and RM requests

### Confirming a write (`actions/`)

- `build_pending_write` turns an action intent into a confirmable write, or returns `None` when
  arguments are missing.
- The **idempotency key** is anchored to the conversation, tool, arguments and a server-side
  nonce, not to the run, so a crash-resume reuses it.
- The change is **read back** in the customer's language (the field name is localized) and waits
  for an explicit yes. `parse_confirmation` understands yes/no in the three languages; when a
  real model is available it classifies free-form replies, and a deterministic guard still
  re-checks yes/no before anything proceeds.

### Claim intake (`actions/claim_intake.py`)

A claim needs five facts: policy, incident date, amount, hospital or garage, and what happened.

- They are collected over as many turns as the customer needs; a single message may fill several.
- Parsers are deterministic: ISO, `dd/mm/yyyy`, "12 Sep", and relative dates in the three
  languages; `₹` / Rs / lakh / hazar amounts; provider names in English and Hindi. Future dates
  and dates more than three years old are rejected.
- An optional model pass may propose values; each one is re-validated by the same parsers.
- The policy is asked for only when the customer holds more than one.
- Cancelling or changing topic ends the intake. After 3 rounds without progress the run goes to
  a human.

### RM requests (`rm/`)

`handoff_node` runs after every turn. When the run ended `escalated` it raises one request:

| Field | Content |
|---|---|
| `id` | `REQ` + 6 digits, given to the customer |
| `kind` | `update_contact`, `raise_ticket`, `file_claim`, or `handoff` (no change attached) |
| `reason` | why it went to a human (`awaiting_manager_approval`, `step_up_failed`, `repeated_complaint`, `retrieval_below_floor`, …) |
| `change_enc` | the requested change, Fernet-encrypted with `RM_REQUEST_KEY` |
| `summary` | English case summary: what was asked, what the agent tried, why it escalated, which sources matter. Never contains the new value. A model-written version is kept only if every number and id in it traces to the facts and PII redaction leaves it clean. |
| `status` | `open`, `done`, `rejected`; plus `handled_by` and a PII-redacted `rm_note` |
| `sla_target` | 24h |
| `dedup_key` | hash of customer + kind + the change; unique among open requests (partial unique index) |

- **Dedup:** the same change requested twice returns the open request's reference without a
  second OTP. Once the RM closes it, the same change can be requested again.
- **Status for the customer:** `get_request_status` is a read tool scoped to the verified
  customer. Another customer's reference is simply not found.
- **For the RM's system:** the three `/rm/requests` endpoints, service-key only. They change a
  request's status, never customer data.
- **Erasure** removes the encrypted change and replaces the summary with a tombstone.
- The `escalations` row links to its request (`request_id`, `user_id`, migration 0009).

---

## 9. LLM layer (`llm/`)

`get_llm(role)` returns a `FallbackLLM` over the role's chain. Chain entries are `provider` or
`provider:model`, because free quotas are per model.

```
LLM_ROLE_SYNTHESIS = gemini:gemini-3.8-flash, gemini:gemini-3.5-flash-lite, groq:openai/gpt-oss-120b, stub
LLM_ROLE_TRIAGE    = groq:openai/gpt-oss-20b, groq:qwen/qwen3.8-27b, stub
LLM_ROLE_JUDGE     = groq:qwen/qwen3.8-27b, stub      # pinned for the live eval
```

- **Deadline per role** (triage 5s, synthesis 10s, judge 60s) bounds retries and fallbacks
  together. When it runs out only the stub is still tried, so a caller always gets an answer.
- **Retries** only on retryable errors (429, 5xx, timeout, malformed output), with jittered
  backoff, honouring `Retry-After` when it fits in the deadline.
- **Circuit breaker** per provider: 3 consecutive failures open it for 60s.
- **Key rotation:** each key variable accepts several comma-separated keys, rotated round-robin
  and on 429.
- **Startup probe** (`probe.py`): the worker lists each keyed provider's models once at boot and
  logs an error for any configured model that is no longer served, or a role that resolves to the
  stub alone. Free tiers retire models; without this a retired model fails silently into the stub.
- **Token accounting** is a `ContextVar`, so concurrent runs do not mix their counts.

### Checks on the synthesis rephrase

The model sees the retrieved passages and action results as delimited `<source>` / `<action>`
blocks and must end with a `USED:` line naming the sources it relied on. The rephrase is
discarded in favour of the deterministic reply when:

- a number or clause id in it does not appear in the sources (grounding check),
- its polarity contradicts the sources (says approved where the source says rejected), or
- it is empty or a stub echo.

If it dropped the clause number the deterministic reply had, the clause is restored. Only the
sources named in `USED:` are shown as citations.

---

## 10. Retrieval (`rag/`) — the differentiator

**Chunking** (`chunking.py`) — documents are split on headings and numbered clauses, not blank
lines. Each chunk carries its section path and clause id, so a citation reads
`policy_schedule.md#4.1`.

**Generic index** (`index.py`) — `HybridIndex`: dense cosine and BM25 rankings fused by
Reciprocal Rank Fusion over the 53 corpus documents.

**Per-customer index** (`customer_index.py`) — loads `data/customers/manifest.yaml` and applies
a **hard pre-filter before scoring**:

```
customer_id == verified user_id   AND   domain in allowed_domains
```

The filter is never relaxed after ranking. Ids in the message cannot widen it; they can only
narrow retrieval to the claim or policy the customer named (the manifest's `about:` field), so a
question about CLM2001 is never answered from the CLM2010 note.

**Generated documents** (`customer_docs.py`) — every policy and claim without an authored
document gets a schedule or claim note rendered from its row, in English and Hindi, and
re-rendered when the row changes. Non-hero and sandbox customers are therefore groundable too.

**Language preference** — when a document has a parallel in the customer's language (`_hi.md`),
that version is preferred.

**Entrypoint** — `search_knowledge(query, top_k, user_id, allowed_domains, mentioned_ids,
language)`: the customer's own documents lead, the generic corpus backfills.

**Embedders** (`embedder.py`) — `multilingual-e5-small` (local, `query:` / `passage:` prefixes,
cross-lingual) is the live path. `HashingEmbedder` is the dependency-free offline floor; it has
no cross-lingual semantics.

**Score floor** — `RETRIEVAL_SCORE_FLOOR` stays `0.0`. `make calibrate-floor` showed e5's top
score cannot separate answerable from unanswerable questions well enough: the best threshold
would wrongly escalate about 17% of real questions. Empty retrieval still escalates.

---

## 11. Identity, authorization and compliance

Compliance is a gate that runs before any tool call. It is deterministic and fails closed.

**Compliance gate** (`compliance/gate.py`), in order:

1. **Injection guard** (`injection.py`, v2) — matches attack *structure*, not lone words: each
   family needs an operator aimed at the agent plus its target (override, persona, fake role
   tag, prompt extraction, and others). Input is normalized first (NFKC, spaced-out letters
   including Devanagari, leetspeak). Retrieved passages are screened with the passage families
   before they reach the model. An optional local classifier (Llama Prompt Guard 2) can be
   layered on with `INJECTION_CLASSIFIER_MODEL`; it is off by default.
2. **Referenced-id ownership** — a policy or claim id in the message must belong to the customer.
3. **Per-action authorization** (`authz.py`).

**Authorization policy** (`authz/policy.py`) — a deterministic intent→domain map (default deny)
over five data domains: `policy_coverage`, `claims`, `billing`, `interaction_history`,
`profile_account`. The model never controls it, so an injection cannot widen what is read.
`update_contact`, `raise_ticket` and `file_claim` require step-up.

**Step-up** (`auth/stepup.py`) — a 6-digit code, single-use, TTL-bounded, bound to the customer,
5 attempts. There is no SMS channel: with `DEMO_MODE=true` the code is returned to the client and
shown as a simulated SMS popup.

**Rate limits** (`api/ratelimit.py`) — fixed windows in Redis, per IP on unauthenticated routes
and per customer on authenticated ones. They fail open if Redis is down; the OTP attempt cap
still holds.

**Audit** (`compliance/audit.py`) — append-only and hash-chained. It stores enum reason codes
and tokenized references, no raw PII, so the 7-year hold is compatible with erasure.

**Erasure** (`compliance/erasure.py`) — tombstones message content and action arguments and
results, withdraws consent, clears suspend custody, and tombstones the customer's RM requests.
The audit chain is untouched and stays valid.

**PII** (`compliance/pii.py`) — redaction before storage, before any logged preview, and on the
reply before it is sent. Regex by default; Presidio when installed and selected.

---

## 12. Persistence

**App tables** (`db/models.py`, async):

| Table | Purpose |
|---|---|
| `conversations` | tenant, customer, language, status, consent; suspend custody (pending write, challenge id, original message) |
| `messages` | role, content (PII-redacted before store), sequence |
| `agent_runs` | status, route, step count, latency, close reason |
| `action_records` | tool, redacted args and result, idempotency key, nonce |
| `audit_log` | hash-chained decisions with tokenized references |
| `escalations` | detected intent, blocking reason, attempted actions, SLA, `user_id`, `request_id` |
| `eval_results` | per-scenario results by config version |

**Beside them** (sync engine, `STORE_BACKEND` = `postgres`, or in-memory SQLite for tests):

- `rm_requests` — §8.
- `mock_users`, `mock_policies`, `mock_claims`, `mock_tickets`, `mock_idempotency`,
  `mock_otp_challenges`, `mock_refresh_tokens`, `mock_meta` — the mock core insurer system and
  mock IdP state, seeded from the customer manifest.

**LangGraph checkpoints** — `CHECKPOINT_BACKEND=postgres` makes crash-resume work across worker
processes. `memory` is enough for tests and single-process dev. In prod a Postgres checkpointer
that cannot open is a hard error.

**Migrations** (`db/migrations/versions/`): `0001_initial` → `0002_runs_audit` →
`0003_eval_results` → `0004_identity_v2` → `0005_run_close_reason` → `0006_operator_audit` →
`0007_step_up_grant` → `0008_message_seq_counter` → `0009_escalation_rm_request`.

---

## 13. Worker and jobs

**Worker** (`worker/main.py`)

- Reads the stream through a consumer group. Up to `WORKER_CONCURRENCY` runs execute at once;
  runs of the same conversation are serialized so turns cannot overtake each other.
- A heartbeat keeps a running entry fresh, so a slow but alive run is not reclaimed and executed
  twice. `XAUTOCLAIM` hands entries from a dead worker to a live one, which resumes from the
  checkpoint.
- The embedder and both indexes are built before the first entry is read.
- Serves its own Prometheus endpoint on `WORKER_METRICS_PORT` (default 9100; if the port is
  taken it logs a warning and runs without it).

**Runner** (`worker/runner.py`) — executes one run, publishes trace events, persists the final
state, and maps a timeout or exception to a terminal close reason.

**Jobs** (`jobs/`) — the reaper abandons suspended conversations whose pending write has
outlived its TTL (never auto-fired) and escalates them; `purge.py` deletes messages past
`MESSAGE_RETENTION_DAYS` and expired dedup keys.

---

## 14. Observability

| Metric | Labels |
|---|---|
| `saral_node_seconds` | node |
| `saral_runs_total` | status |
| `saral_llm_calls_total` | provider, outcome (ok, rate-limited, timeout, …) |
| `saral_llm_served_by_stub_total` | — (calls that fell to the stub with real keys set) |
| `saral_llm_breaker_open` | provider |
| `saral_escalations_total` | reason |
| `saral_rm_requests_total` | kind, created |

- `GET /health` — liveness. `GET /ready` — database `SELECT 1` and Redis ping, 503 on failure.
- Logs are structured (`structlog`) with a per-request id.
- The chat UI has a developer mode that shows the per-node trace, timing and tokens for a turn.
- Per-node distributed tracing (OpenTelemetry) is not built.

---

## 15. Evaluation (`eval/`)

**Scenarios** — 171 in `data/scenarios/`: `scenarios.yaml` (28), `scenarios_expansion.yaml`
(67), `multiturn.yaml` (25), `rm_requests.yaml` (17) and `injection.yaml` (34: 16 attacks and 18
benign look-alikes).

**Two tiers, same scenarios**

- **Offline** — stub model and hashing embedder. Deterministic and free; CI runs it.
- **Live** — the real free models from `.env`, judged by a pinned model of a different family
  than the synthesis model, with no fallback.

The tier is derived from the configuration actually in effect, so a stub run cannot be reported
as live.

**Metrics** look at the reply: routing, tool sequence, attack block rate and false-block rate,
groundedness, explanation groundedness (cites the customer's own documents), reply language and
script, answer correctness, faithfulness, multi-turn success, RM request accuracy, resolution,
cross-lingual consistency, and pass rate per language.

**Gate** (`gate.py`) — `make eval-gate` fails when any offline metric is worse than
`data/eval_baselines/offline.json`. Current baseline (171 scenarios):

| Metric | Baseline |
|---|---|
| pass rate | 88.3% |
| routing, tool sequence, groundedness, faithfulness, multi-turn, RM request accuracy | 100% |
| attacks blocked / false blocks | 100% / 0% |
| reply language match | 88.9% |
| answer correctness | 76.9% |

The two lower numbers are mostly the stub and hashing embedder: the offline tier cannot rephrase
into Hinglish or retrieve across languages.

**Judge trust** — the offline judge is a rubric built from the expectations, so its agreement
with humans would be circular and is not reported. Trust comes only from the live tier: humans
label the exact live replies (`make eval-labels`) and Cohen's κ is computed per language. A
language with κ undefined or below `JUDGE_KAPPA_FLOOR` (0.6) is reported as not validated.
**No human labels exist yet, so the live judge is currently unvalidated.**

**Held-out injection sets** (`data/scenarios/heldout/`, `make eval-heldout`) — written without
sight of the detector, outside the suite and the gate. The report keeps each set's first blind
reading and marks sets that have since been used for tuning. First blind readings: 17/35 and
25/40 attacks caught; 0/35 and 1/40 benign messages blocked.

---

## 16. Configuration and deployment

One pydantic `Settings` (`config.py`); every knob is an environment variable with a safe dev
default. [`.env.example`](.env.example) lists them all. The ones that change behaviour:

| Setting | Default | Meaning |
|---|---|---|
| `APP_ENV` | `dev` | `prod` refuses to start without `SESSION_SECRET` (≥32 bytes) and `RM_REQUEST_KEY` |
| `DEMO_MODE` | `false` | show OTPs as a simulated SMS popup (needed to sign in locally) |
| `SERVICE_API_KEY` | unset | enables the RM and other server-to-server routes |
| `CLIENT_IP_HEADER` / `CLIENT_IP_FROM_RIGHT` | unset / 0 | real client IP for rate limits behind a proxy; `1` = last entry of `X-Forwarded-For` (proxies that append, e.g. Hugging Face) |
| `RM_APPROVAL_ACTIONS` | all three writes | confirmed writes raised to the RM instead of executed |
| `RM_REQUEST_KEY` | derived in dev | Fernet key for the requested change |
| `SESSION_TTL_MIN` / `REFRESH_TTL_DAYS` | 30 / 14 | access and refresh token lifetimes |
| `STEP_UP_TTL_S` / `OTP_MAX_ATTEMPTS` | 300 / 5 | OTP validity and attempt cap |
| `PENDING_WRITE_TTL_S` / `ORPHAN_TTL_S` | 86400 | pending-write authority and reaper horizon |
| `EMBEDDER` | `sentence-transformer` | `hashing` for the offline floor |
| `RETRIEVAL_TOP_K` / `RETRIEVAL_SCORE_FLOOR` | 4 / 0.0 | passages per query; floor off (see §10) |
| `STORE_BACKEND` / `CHECKPOINT_BACKEND` | `memory` | `postgres` for durable mock store and crash-resume |
| `WORKER_CONCURRENCY` | 4 | runs executed at once per worker |
| `LLM_DEADLINE_TRIAGE_S` / `_SYNTHESIS_S` | 5 / 10 | whole-call budget per role |
| `LLM_BREAKER_THRESHOLD` / `_COOLDOWN_S` | 3 / 60 | circuit breaker |
| `CONVERSATION_MEMORY_TURNS` | 40 | history passed to the graph |
| `MESSAGE_RETENTION_DAYS` / `AUDIT_RETENTION_YEARS` | 90 / 7 | retention |
| `PII_BACKEND` | `regex` | `presidio` for NER |
| `INJECTION_CLASSIFIER_MODEL` | unset | optional Prompt Guard 2 layer |
| `CONFIG_VERSION` | `v1` | pins eval comparability |

**Deploy** ([`docs/DEPLOY.md`](docs/DEPLOY.md))

- **Hugging Face ZeroGPU Space (live, free)** — https://soumya0343-saral.hf.space. ZeroGPU Spaces
  must be Gradio apps, so [`deploy/hf-space/app.py`](deploy/hf-space/app.py) supervises one
  container around Gradio's server:
  - Postgres on local disk and Redis on localhost; migrations on boot.
  - The worker as its own process, restarted if it exits.
  - Saral's API mounted at `/api`; the static Next.js build at `/` and `/eval`, same origin.
  - The database dumped to a private bucket at `/data` every 60s when it changed, and restored
    on boot. Outbound port 5432 is blocked on Spaces, so a hosted Postgres is not an option.
  - The GPU is never used: e5 runs on the Space's CPUs.
  - `deploy/hf-space/build.sh` assembles the Space from the repo and uploads it.
- **Fly.io / Render (paid)** — separate API and worker machines from one image; frontend on
  Vercel with `NEXT_PUBLIC_API_URL`. `data/` is baked into the image and read-only.

---

## 17. Design principles

- **Identity is token-derived, never trusted from the message.** Reads need a valid session;
  writes need an OTP and a read-back confirmation.
- **Saral never writes customer data.** The RM does, outside Saral, from a complete request.
- **Compliance is a gate, not a peer.** Injection, ownership and authorization run before any
  tool, deterministically, failing closed.
- **Per-customer grounding is hard-filtered.** Scope is the verified customer and the
  intent→domain map; the model cannot widen it.
- **The facts are deterministic; the model only rephrases.** A rephrase that fails a check is
  dropped.
- **Authority is mortal.** A stale pending write is abandoned, never auto-fired.
- **Runs offline.** Stub model and hashing embedder keep the system deterministic for CI.
- **Auditable and erasable.** A PII-free hash-chained audit coexists with erasure.
- **Event-driven.** The API enqueues; the worker executes with checkpoint/resume.
- **Measured.** No metric may fall below its committed baseline.

### Architecture decision records ([`docs/adr/`](docs/adr/))

| ADR | Decision |
|---|---|
| [0001](docs/adr/0001-identity-model.md) | Token-derived identity + step-up for writes |
| [0002](docs/adr/0002-purpose-bound-retrieval.md) | Purpose-bound retrieval |
| [0003](docs/adr/0003-free-providers-and-multilingual-embeddings.md) | Free LLM providers + multilingual-e5 embeddings |
| [0004](docs/adr/0004-two-mechanism-resume.md) | Two-mechanism resume; stale write authority is mortal |

The RM-owns-writes decision (Phase 4) is recorded in the roadmap, not yet as an ADR.

---

## 18. Not built

- An operator or RM console (out of scope by decision; the RM's system uses the service-key API).
- A real SMS channel and a real identity provider (both mocked and labelled as such).
- A real core insurer system (the `mock_*` tables stand in for it).
- OpenTelemetry tracing, a vector database (retrieval is in-process), and a validated live judge.
