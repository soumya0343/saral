# Saral — Architecture

> Saral (सरल — "simple") is a supervisor-orchestrated multi-agent system that resolves
> customer-support queries for regulated enterprises (insurance / lending) in **English,
> Hindi, and Hinglish**. Its differentiator is **per-customer document grounding**: answers
> cite *this customer's own* policy clause (e.g. "rejected under rider Clause R1.3"), not a
> generic FAQ.

This document is the single reference for how the system is put together. For product/requirements
context see [`.claude/Saral_PRD_TRD.md`](.claude/Saral_PRD_TRD.md); for deployment steps see
[`docs/DEPLOY.md`](docs/DEPLOY.md).

---

## 1. System at a glance

```
                 ┌────────────────────────────────────────────────────────┐
   Browser ──►   │  Next.js frontend (chat UI + /eval dashboard, Vercel)   │
                 └───────────────────────────┬────────────────────────────┘
                                             │ REST + SSE
                 ┌───────────────────────────▼────────────────────────────┐
                 │  FastAPI  (api/)   — identify, converse, reply, SSE      │
                 └───────────────────────────┬────────────────────────────┘
                                             │ enqueue RunRequest
                                  ┌──────────▼──────────┐
                                  │  Redis Streams       │  topic: agent-runs
                                  │  (run bus)           │  group: workers
                                  └──────────┬──────────┘
                                             │ XREADGROUP
                 ┌───────────────────────────▼────────────────────────────┐
                 │  Worker (worker/)  — consume, checkpoint/resume, reap    │
                 │                                                          │
                 │     LangGraph supervisor graph (graph/)                  │
                 │   START → triage → compliance → identity → supervisor    │
                 │              ┌──────────route──────────┐                 │
                 │              ▼        ▼        ▼        ▼                 │
                 │            rag     action    mixed    suspend            │
                 │              └────────┴────► synthesis ────► END         │
                 └──────────┬──────────────────────────┬──────────────────┘
                            │                           │
                  ┌─────────▼────────┐        ┌─────────▼─────────┐
                  │ PostgreSQL 16    │        │  Hybrid RAG index  │
                  │ state · runs ·   │        │  corpus + per-     │
                  │ audit · escal.   │        │  customer docs     │
                  └──────────────────┘        └───────────────────┘
```

**Two runtimes, one image:** `api` (uvicorn) and `worker` (`python -m saral.worker.main`).
API never runs the graph inline by default — it enqueues; the worker owns execution. This gives
crash-resume, horizontal scaling, and partial-failure isolation.

---

## 2. Tech stack

| Layer | Choice |
|---|---|
| Language | Python **3.12+** (uv-managed, `uv.lock`) |
| Web | FastAPI `0.115+`, uvicorn, `sse-starlette` (token-trace streaming) |
| Orchestration | **LangGraph** `0.2.60+` (StateGraph), `langchain-core` |
| DB | PostgreSQL 16 via SQLAlchemy `2.0` async + `asyncpg` (`psycopg` for sync/migrations); Alembic |
| Queue | Redis 7 Streams (consumer groups) |
| LLM SDKs | `anthropic`; Sarvam/Gemini/Groq/Cerebras via OpenAI-compat HTTP |
| Retrieval | `rank-bm25` (lexical) + `sentence-transformers` (multilingual-e5, optional) |
| Auth | `pyjwt` session tokens |
| Config | `pydantic` + `pydantic-settings` |
| PII | regex (default) or Presidio (optional extra) |
| Frontend | Next.js 15, React 19, Tailwind 4 |
| Dev | ruff, mypy, pytest + pytest-asyncio, respx |

LLM SDKs and embeddings are **optional**. Absent keys → deterministic **stub** provider + **hashing**
embedder, so the whole stack runs offline and reproducibly (used in CI and dev).

---

## 3. Repository layout

```
sarvam/
├── backend/saral/          # application (see §4)
├── frontend/               # Next.js chat UI + /eval dashboard
├── data/
│   ├── customers/          # 3 hero customers (U1001/U1003/U1010) + 12 thin (leak tests)
│   ├── policy_corpus/      # 10 tenant-wide policy/FAQ markdown docs
│   ├── scenarios/          # ~95 eval scenarios (scenarios.yaml 28 + scenarios_expansion.yaml 67)
│   └── eval_reports/       # JSON run reports
├── tests/                  # 19 pytest files + conftest (forces offline determinism)
├── docs/
│   ├── DEPLOY.md           # Fly / Render / Vercel runbook
│   └── adr/                # 4 ADRs (see §15)
├── .claude/                # Saral_PRD_TRD.md, CONTEXT.md — product/terminology source
├── .github/                # CI workflows
├── Dockerfile.api          # uvicorn image (ships data/ read-only)
├── Dockerfile.worker       # worker image
├── docker-compose.yml      # local: postgres + redis + api + worker
├── fly.toml                # Fly: app + worker process groups (region bom)
├── render.yaml             # Render Blueprint: api, worker, PG, Redis
├── Makefile                # dev/test/deploy targets
├── pyproject.toml / uv.lock
├── alembic.ini
└── .env.example            # all config knobs (safe dev defaults)
```

---

## 4. Backend packages (`backend/saral/`)

| Package | Responsibility |
|---|---|
| `api/` | FastAPI app, routes, middleware, auth + eval routes |
| `graph/` | LangGraph state, node wiring, checkpointer |
| `agents/` | triage, rag, action, synthesis specialists |
| `llm/` | provider abstraction + per-role fallback factory |
| `rag/` | hybrid corpus index, per-customer index, embedders |
| `db/` | SQLAlchemy models, repository, session, migrations |
| `compliance/` | injection, authz, audit, PII, erasure gates |
| `authz/` | intent→domain map, step-up risk tiers |
| `auth/` | identity, session tokens, step-up OTP |
| `actions/` | write confirmation parsing + idempotency |
| `tools/` | mock core-system backend (claims/policy/tickets) |
| `eval/` | scenario runner, LLM-judge, metrics, kappa agreement |
| `worker/` | Redis consumer + run executor |
| `jobs/` | orphan reaper, message purge |
| `config.py` | central `Settings` (60+ fields) |
| `schemas.py` | shared domain models |
| `runbus.py`, `logging.py` | Redis pub/sub trace bus, structured logging |

---

## 5. Request lifecycle

1. **Identify** — `POST /customers` resolves name/mobile/email → `user_id`.
2. **Start** — `POST /conversations` mints a **session token** (JWT, derived from mock IdP — never trusted from message body).
3. **Message** — `POST /conversations/{id}/messages` validates token, builds a `RunRequest`, enqueues it on Redis stream `agent-runs`, returns immediately. Client opens **SSE** (`GET /conversations/{id}/stream`) for live trace events.
4. **Execute** — worker `XREADGROUP`s the request, loads any checkpoint (thread_id = run_id), runs the graph, streams trace events to Redis pub/sub (relayed to SSE), persists final state.
5. **Reply / resume** — if the run suspended (awaiting OTP or confirmation), `POST /conversations/{id}/reply` re-enqueues with `resume_reply` / `challenge_response`; the worker resumes from checkpoint.

### HTTP endpoints

| Method · Path | Module | Purpose |
|---|---|---|
| `POST /customers` | routes | identify customer (name/mobile/email → user_id) |
| `POST /conversations` | routes | start conversation, mint session token |
| `POST /conversations/{id}/messages` | routes | send message → enqueue run |
| `POST /conversations/{id}/reply` | routes | resume from awaiting_input / awaiting_confirmation |
| `GET /conversations/{id}/stream` | routes | SSE trace event stream |
| `GET /conversations/{id}/messages` | routes | conversation history |
| `DELETE /users/{user_id}/data` | routes | DPDP erasure (tombstone PII) |
| `POST /escalations/{id}/resolve` | routes | operator handoff / close-out |
| `POST /session` | auth_routes | mint session token |
| `POST /step-up` | auth_routes | submit OTP challenge response |
| `POST /eval/run` | eval_routes | run the eval suite |
| `GET /eval/report` | eval_routes | latest eval report |
| `GET /eval/reports` | eval_routes | all eval reports |
| `GET /health` | app | health check (Fly/Render probe) |

---

## 6. The supervisor graph (`graph/`)

**Framework:** LangGraph `StateGraph` compiled to a runnable, with a checkpointer for suspend/resume.

**State — `RunState`** (`graph/state.py`), key fields:
- Identity: `run_id`, `conversation_id`, `tenant_id`, `user_id`, `auth_level`
- Input: `raw_message`, `history`, `known_entities`
- Triage out: `language`, `intents`, `entities`, `allowed_domains`
- Routing: `route` ∈ {respond, rag, action, mixed, suspend, end}
- RAG: `retrieved: Passage[]`, `ungrounded`
- Decisions: `compliance_decisions`, `actions`, `final_response`
- Write custody: `pending_write`, `intent_nonce`, `step_up_owed`, `challenge_id`, `resume_reply`, `challenge_response`
- Bookkeeping: `degraded_agents` (additive reducer), `status`, `step_count` (additive)

**Node flow:**

```
START
 → triage_node        language + intents + entities (deterministic, doubles as stub)
 → compliance_node    injection check, referenced-id ownership, per-action authz
 → identity_node      derive allowed_domains; if write → mint step-up OTP or read-back
 → supervisor_node    pick route
     ├─ rag      → rag_node      → score-floor gate (ungrounded? escalate)
     ├─ action   → action_node   (writes only via confirmed PendingWrite)
     ├─ mixed    → [rag, action] parallel fan-out
     ├─ suspend  → END           (awaiting_input / awaiting_confirmation, persisted)
     └─ respond  → synthesis_node
 → synthesis_node     final_response (ResponsePayload), status
 → END
```

**Notable transitions (each tied to a TRD/ADR rule):**
- **Injection blocked** → run blocked, synthesis escalates.
- **Write intent** → identity gates on auth level: if `< step_up`, mint OTP and suspend; on resume verify OTP, read back the change, await yes/no.
- **Stale pending write** (past `PENDING_WRITE_TTL_S`) → discarded; resume must re-earn step-up ("authority is mortal", ADR-0004).
- **Retrieval below score floor** → escalate (`ungrounded`) instead of inventing (ADR-0002 / FR-18).
- **Degraded agent** → recorded additively; status downgraded resolved→degraded.

---

## 7. Agents (`agents/`)

| Agent | In | Out | Notes |
|---|---|---|---|
| **TriageAgent** | raw message | `IntentResult` (language, intents, entities, confidence) | Deterministic regex (Devanagari + Hinglish markers) for language; optional Sarvam `/text-lid`. Regex lexicons in 3 scripts for intent; regex for policy_id/claim_id/mobile/email. |
| **RagAgent** | query, user_id, allowed_domains | `Passage[]` | Wraps `search_knowledge()`: generic corpus + per-customer docs, hard-filtered. Never free-generates. |
| **ActionAgent** | intents, entities, user_id, message, pending_write | `ActionRecord[]` | Dispatches mock tools. Reads run directly; **writes execute only via confirmed PendingWrite**, deduped by idempotency_key. Missing args → `needs_clarification`, never a guessed call. |
| **SynthesisAgent** | `SynthesisContext` (lang, message, passages, actions, decisions, degraded) | `ResponsePayload` | Deterministic template composer (EN/HI/HINGLISH) + optional LLM grounding check. Every fact must trace to a passage or action result. Marks escalation on blocks/degradation. |

Each agent has a deterministic path so the system produces reproducible eval runs without any API key.

---

## 8. LLM layer (`llm/`)

**Protocol** (`base.py`):
```python
class LLMClient(Protocol):
    name: str
    available: bool
    async def complete(messages, max_tokens) -> str
    async def structured(messages, schema: type[T], max_tokens) -> T
```

**Factory** (`factory.py`) — `get_llm(role)` builds a `FallbackLLM` chain: tries each available
provider in order, retries up to `llm_retry_cap` (2), falls to the next on failure. **Stub is always
appended last** → never fully unavailable.

**Per-role chains** (config):
```
llm_provider_order  = sarvam,anthropic,stub      # global default
llm_role_triage     = groq,cerebras,stub          # fast free-tier classify
llm_role_synthesis  = gemini,sarvam,stub          # Hindi-strong generation
llm_role_judge      = gemini,stub                 # PINNED — no mid-suite swap
```

**Providers:** Sarvam (primary, Hindi + `/text-lid`), Anthropic (Claude), Gemini, Groq, Cerebras
(all OpenAI-compat), Stub (deterministic offline).

---

## 9. Retrieval / RAG (`rag/`) — the differentiator

**Hybrid index** (`index.py`) — `HybridIndex`: dense cosine + BM25 lexical fused by Reciprocal
Rank Fusion. Corpus chunked on `\n\n`, lone headers dropped. Per-passage cosine surfaced for the
score-floor gate.

**Embedders** (`embedder.py`):
- `SentenceTransformerEmbedder` — `intfloat/multilingual-e5-small`, asymmetric (`query:` / `passage:` prefixes), cross-lingual. Production.
- `HashingEmbedder` — deterministic hashing trick, no dependency. CI/offline floor.

**Per-customer index** (`customer_index.py`) — `CustomerIndex` loads `data/customers/manifest.yaml`
and applies a **HARD pre-filter before scoring**:
```
(customer_id == user_id) AND (domain in allowed_domains OR domain is None)
```
Never post-rank relaxation — entity IDs in the message can't widen scope (injection-immune). No-op
if no manifest (generic corpus unaffected).

**Entrypoint** `search_knowledge(query, top_k, user_id, allowed_domains)` — personal docs lead,
generic backfills (deduped by doc_id).

---

## 10. Identity, authz & compliance

Compliance runs **as a gate, not a peer** — deterministic and fail-closed, *before* any action fires.

**Compliance gate** (`compliance/gate.py`) — `ComplianceGate.evaluate()` runs in order:
1. **Injection** (`injection.py`) — 30+ regex patterns incl. Hinglish/Devanagari ("नियम भूल", "रिफंड अप्रूव"), DAN persona, "ignore rules", "act as admin".
2. **Referenced-id ownership** — user must own the policy/claim referenced (impersonation defense).
3. **Per-action authorization** (`authz.py`) — deterministic `check_authorization()`; ownership-gated set: get_claim_status, get_policy_details, update_contact, raise_ticket, file_claim.

**Authz policy** (`authz/policy.py`) — intent→domain purpose-limitation map (default-deny).
DataDomains: POLICY_COVERAGE, CLAIMS, BILLING, INTERACTION_HISTORY, PROFILE_ACCOUNT.
Step-up (state-changing) actions: update_contact, raise_ticket, file_claim.

**Write safety** (`actions/confirm.py`):
- `idempotency_key(conversation_id, tool, args, intent_nonce)` — **conversation-anchored** (not run-anchored) so crash-resume reuses the same key → exactly-once.
- `build_pending_write(...)` — parses an action intent into a confirmable write; `None` if args missing.
- `is_stale(...)` — past `PENDING_WRITE_TTL_S` → never auto-fires; resume re-earns step-up.
- `parse_confirmation(text)` — multilingual yes/no (haan/नहीं/जी/cancel/…).

**Audit** (`compliance/audit.py`) — append-only, **hash-chained** (hash_prev/hash_self) for tamper
evidence. Stores reason codes + tokenized refs (`user_ref`, `args_hash`) — **no raw PII**. 7-year hold.

**Erasure** (`compliance/erasure.py`) — `erase_user_data(user_id)` tombstones message content +
action args/result, withdraws consent, clears suspend custody. **Audit log untouched** (no PII, survives erasure).

**PII** (`compliance/pii.py`) — `redact_pii()` regex or Presidio; applied to messages before storage
and synthesis output before send (FR-6).

---

## 11. Persistence (`db/`)

SQLAlchemy async ORM. Core tables:

| Table | Purpose |
|---|---|
| `Conversation` | tenant/user/language/status; `consent_status`; suspend custody (`pending_write` JSONB, `challenge_id`, `original_message`) |
| `Message` | role/content/sequence; **content PII-redacted before store** |
| `AgentRun` | status, route, step_count, latency_ms, cost_usd, `close_reason`, `closed_at` |
| `ActionRecordRow` | tool/args/result (redacted), `idempotency_key`, `intent_nonce`, state |
| `AuditLog` | hash-chained decisions; tokenized refs; 7-year retention |
| `Escalation` | detected_intent, blocking_reason, attempted_actions, sla_target, operator handling |
| `EvalResult` | config_version, scenario_id, passed, metrics |

`repository.py` — `persist_run(state)` writes run + actions + audit chain + escalation + suspend
custody atomically. `erase_user_data()` for DPDP erasure.

**Migrations** (`db/migrations/versions/`): 0001_initial → 0002_runs_audit → 0003_eval_results →
0004_identity_v2 → 0005_run_close_reason → 0006_operator_audit.

---

## 12. Worker & jobs

**Worker** (`worker/main.py`) — Redis Streams consumer (`XREADGROUP`, group `workers`, consumer id
`{host}-{pid}`). Periodically reaps orphaned suspended conversations; `XAUTOCLAIM` recovers stuck
pending messages.

**Runner** (`worker/runner.py`) — `execute_run(req)`: load checkpoint by thread_id, resume if
mid-flight, stream graph updates to Redis pub/sub, persist final state. Maps TimeoutError/Exception
to terminal close reasons (`timed_out`, `crashed`).

**Jobs** (`jobs/`) — `reaper.py` abandons stale pending writes past TTL; `purge.py` deletes message
content past `MESSAGE_RETENTION_DAYS`.

---

## 13. Evaluation (`eval/`)

**Runner** (`runner.py`) — runs every scenario against the **pinned** config, optionally completing
step-up (auth=step_up, reply="yes") for write scenarios, judges each, computes per-metric flags,
diffs against prior report for regressions, persists JSON.

**Judge** (`judge.py`) — LLM-as-judge on the `judge` role chain (pinned). Deterministic stub grades
status match / safety blocks / tool sequence / citations; real model judges via prompt + context.

**Metrics** (`metrics.py`) — per-scenario flags (routing, tool_seq, compliance_block, groundedness,
explanation_groundedness, status, resolution). `summarize()` aggregates + computes
**Cohen's kappa per language** vs human labels. If κ < `judge_kappa_floor` (0.6) the language is
**judge-untrusted** → resolution needs human review (TRD §18.2).

**Agreement** (`agreement.py`) — `cohens_kappa()`, chance-corrected, `None` when unanimous.

**Scenarios** — `data/scenarios/scenarios.yaml`, ~95 cases across categories
{information, action, compliance, adversarial, small_talk}; multilingual triples grouped by
`xling_group` for cross-lingual consistency; ~40 carry `human_label` to validate the judge.

---

## 14. Configuration & deployment

**Config** (`config.py`) — single pydantic `Settings`; all knobs env-driven with safe dev defaults.
Key tunables:

| Setting | Default | Meaning |
|---|---|---|
| `SESSION_TTL_MIN` | 30 | session token validity |
| `STEP_UP_TTL_S` | 300 | OTP challenge validity |
| `PENDING_WRITE_TTL_S` | 86400 | stale-write authority TTL |
| `ORPHAN_TTL_S` | 86400 | reaper TTL |
| `RETRIEVAL_TOP_K` | 4 | passages per query |
| `RETRIEVAL_SCORE_FLOOR` | 0.0 | below → escalate |
| `MESSAGE_RETENTION_DAYS` | 90 | purge TTL |
| `AUDIT_RETENTION_YEARS` | 7 | regulatory hold |
| `MAX_STEP_COUNT` | 25 | supervisor step limit |
| `TOOL_TIMEOUT_S` | 10.0 | per-tool timeout |
| `EMBEDDER` | sentence-transformer | `hashing` offline floor |
| `PII_BACKEND` | regex | `presidio` for prod NER |
| `STORE_BACKEND` | memory | `postgres` for prod (mock core-system store) |
| `CHECKPOINT_BACKEND` | memory | LangGraph checkpoint storage |
| `LLM_RETRY_CAP` | 2 | retries per provider before fallback |
| `TRIAGE_LLM_LANGUAGE` | true | use Sarvam `/text-lid` for language detection |
| `CONVERSATION_MEMORY_TURNS` | 8 | short-term context window |
| `CONFIG_VERSION` | v1 | pins eval comparability (no mid-suite drift) |
| `JUDGE_KAPPA_FLOOR` | 0.6 | per-language judge trust threshold |

Full `Settings` has ~56 fields (per-provider keys/models/URLs, paths, Redis stream names, reclaim
tuning). The above are the behavioral knobs; see [`backend/saral/config.py`](backend/saral/config.py) for the rest.

**Deploy targets** (`docs/DEPLOY.md`):
- **Fly.io** — region `bom` (Mumbai; DPDP residency + Sarvam proximity), app + worker process
  groups, Fly Postgres + Upstash Redis, `/health` checks. Migrations: `alembic upgrade head` via SSH.
- **Render** — Blueprint auto-creates api, worker, PG 16, Redis 7; pre-deploy runs migrations;
  `DATABASE_URL`/`REDIS_URL` auto-wired.
- **Frontend** — Vercel, `NEXT_PUBLIC_API_URL` → API host.

Same image, two process commands. `data/` (corpus + customers + scenarios) is baked in, read-only at runtime.

---

## 15. Design principles (why it's built this way)

- **Identity is token-derived, never trusted from the message.** Reads need a valid session token; writes need step-up OTP **+** read-back confirmation.
- **Compliance is a gate, not a peer.** Injection / authz / PII run before any action — deterministic, fail-closed.
- **Per-customer grounding is hard-filtered.** Scope set by verified `user_id` ∧ intent→domain map; the LLM never widens it.
- **Exactly-once writes.** Conversation-anchored idempotency keys survive crash-resume.
- **Authority is mortal.** Stale pending writes are abandoned, not auto-fired.
- **Runs offline.** Stub LLM + hashing embedder make the full system deterministic and CI-friendly.
- **Auditable & erasable.** Hash-chained PII-free audit (7yr) coexists with DPDP erasure.
- **Event-driven.** API enqueues; worker executes with checkpoint/resume and partial-failure isolation.

### Architecture decision records ([`docs/adr/`](docs/adr/))

| ADR | Decision |
|---|---|
| [0001](docs/adr/0001-identity-model.md) | Token-derived identity + step-up for writes |
| [0002](docs/adr/0002-purpose-bound-retrieval.md) | Purpose-bound retrieval (score floor → escalate, don't invent) |
| [0003](docs/adr/0003-free-providers-and-multilingual-embeddings.md) | Free LLM providers + multilingual-e5 embeddings |
| [0004](docs/adr/0004-two-mechanism-resume.md) | Two-mechanism resume; stale write authority is mortal |
