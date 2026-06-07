# Saral — Multilingual Enterprise Support-Resolution Agent

> सरल — "simple"

A supervisor-orchestrated multi-agent system that resolves customer-support
queries for regulated enterprises (insurance / lending). It understands English,
Hindi, and Hinglish, retrieves grounded answers from a policy corpus, takes
authorized account actions, enforces compliance + PII rules on every turn, and is
validated by a labeled evaluation suite with an LLM judge.

## Why this is interesting

- **Identity is token-derived, never trusted from the message** — a mock IdP mints
  short-TTL session tokens; reads need a valid session, **state-changing actions
  require step-up (OTP)** and an explicit **read-back confirmation** before they fire.
- **Per-customer document grounding (the differentiator)** — answers cite *this
  customer's own* policy clause ("rejected under rider Clause R1.3"), in Hindi or
  English, not a generic FAQ template. Retrieval is hard-filtered by the verified
  `user_id` ∧ an intent→domain purpose-limitation map, over a **local multilingual
  (multilingual-e5) embedding index** so Hindi/Hinglish queries match Hindi documents.
- **Per-role free model routing** — models are assigned per role, not one global
  chain: Hindi-strong **Gemini Flash** for synthesis, fast **Groq/Cerebras Llama**
  for triage, **Sarvam** for language detection, a **pinned** judge for eval. Every
  provider is OpenAI-compatible and optional; missing keys skip to a stub.
- **Groundedness is enforced, not hoped** — retrieval below a relevance **score floor**
  escalates instead of answering; a **grounding check** rejects any LLM phrasing whose
  numbers/clause-ids don't trace to a retrieved passage, falling back to a deterministic
  draft. The factual core is always deterministic; the LLM only rephrases tone.
- **Real agent orchestration**, not a single prompt — a LangGraph supervisor routes
  to specialist agents and fans out parallel branches for mixed intents.
- **Compliance as a gate, not a peer** — injection detection, referenced-id ownership,
  and authorization run *before* any action executes; PII is redacted before storage
  and before the response is emitted; every decision is an enum **reason code** in the
  audit log.
- **Suspend/resume with exactly-once writes** — a write suspends for step-up +
  confirmation and resumes in a fresh run that re-validates identity; a
  conversation-anchored idempotency key prevents double-writes across crash-resume.
  A pending write's authority is **mortal**: past its TTL it is abandoned, never
  auto-fired, and an orphan reaper closes the run.
- **Event-driven** — the API enqueues runs onto a Redis Streams bus; a separate
  worker consumes them, with checkpoint/resume and partial-failure isolation.
- **Measured, not vibes** — ~95 labeled scenarios (explanation-groundedness,
  impersonation, multilingual triples, injection) scored by an LLM judge whose
  **Cohen's κ vs human labels** is computed per language; a language below the κ
  floor is gated as "needs human review", not silently trusted.
- **Runs offline** — no LLM key needed in dev; a deterministic stub provider and the
  hashing-embedder floor keep the full stack reproducible.

## Architecture

```
                       Redis Streams (run bus)
   FastAPI  ──enqueue──►  ════════════  ──consume──►  Worker
   (REST + SSE)                                       │
                                                      ▼
                                        LangGraph supervisor graph
                                                      │
   START → triage → compliance → identity → supervisor ─route─►  ┐
       ├─ synthesis              (respond / injection-blocked → escalate)
       ├─ END (suspend)          (write → step-up OTP / read-back confirmation)
       ├─ rag → synthesis        (information; per-customer scoped retrieval; below
       │                          the score floor → escalate, never invent)
       ├─ action → synthesis     (reads, or a CONFIRMED write, executed idempotently)
       └─ {rag, action} → synthesis   (mixed intent: parallel fan-out)
                                                      │
                                            synthesis → END
```

Identity runs before any data read and gates writes; a write suspends the run
(`awaiting_input` for the OTP, then `awaiting_confirmation` for the read-back) and
resumes via `POST /conversations/{id}/reply` — re-validating the token before the
idempotent write fires.

**Agents** ([`backend/saral/agents/`](backend/saral/agents/))
- `triage` — language + intent + entity detection
- `rag` — grounded retrieval over the policy corpus
- `action` — authorized account actions against a mock backend
- `synthesis` — composes the final grounded, language-matched response

**Cross-cutting**
- [`auth/`](backend/saral/auth/) — mock IdP (JWT session tokens), identity gate, step-up OTP
- [`authz/`](backend/saral/authz/) — intent→domain purpose-limitation map (+ on-demand long-term memory) + action risk tiers
- [`actions/`](backend/saral/actions/) — write confirmation + conversation-anchored idempotency + pending-write TTL
- [`compliance/`](backend/saral/compliance/) — `injection`, `authz`, `gate`, `pii` (regex/Presidio), `audit`, `erasure`
- [`graph/`](backend/saral/graph/) — supervisor graph, run state machine, checkpointing, score-floor + grounding gates
- [`llm/`](backend/saral/llm/) — per-role provider routing (Gemini · Groq · Cerebras · Sarvam · Anthropic · stub)
- [`rag/`](backend/saral/rag/) — generic corpus + per-customer multilingual-e5 index over [`data/customers/`](data/customers/)
- [`eval/`](backend/saral/eval/) — scenarios, metrics, per-language LLM judge + Cohen's κ gating, runner
- [`jobs/`](backend/saral/jobs/) — orphan reaper + data-retention purge

## Stack

FastAPI · LangGraph · PostgreSQL 16 · Redis 7 (Streams) · Next.js + Tailwind ·
multilingual-e5 embeddings (sentence-transformers) · Gemini / Groq / Cerebras / Sarvam
(free tiers, OpenAI-compatible) · Presidio (PII) · uv

## Quickstart

**Prerequisites:** Docker + Docker Compose, `uv`, Node 18+ (frontend).

```bash
cp .env.example .env          # fill keys later; all optional in dev (stub fallback)
uv sync --extra embeddings    # backend deps + local multilingual-e5 (first run downloads ~120MB)
make up                       # postgres + redis + api + worker (docker compose)
make migrate                  # apply DB migrations
curl localhost:8000/health    # {"status":"ok"}
```

> Plain `uv sync` works too — without the `embeddings` extra the retrieval layer
> falls back to the dependency-free hashing embedder (the offline/CI floor).

To use real models, paste free-tier keys into `.env` (`GEMINI_API_KEY`,
`GROQ_API_KEY`, `CEREBRAS_API_KEY`, `SARVAM_API_KEY`) and set `EMBEDDER=sentence-transformer`.
Every key is optional — unset providers skip to the stub.

Frontend:

```bash
cd frontend && npm install && npm run dev   # http://localhost:3000
```

> **LLM keys are optional in dev.** With no provider keys set
> (`SARVAM_API_KEY` / `GEMINI_API_KEY` / `GROQ_API_KEY` / `CEREBRAS_API_KEY` /
> `ANTHROPIC_API_KEY`), the system falls back to a deterministic stub provider, so the
> full stack runs offline and reproducibly. Set per-role chains via
> `LLM_ROLE_SYNTHESIS` / `LLM_ROLE_TRIAGE` / `LLM_ROLE_JUDGE` (see [`.env.example`](.env.example)).

### Local without docker

```bash
make dev    # API + worker in ONE process (shared mock state) — for frontend dev
```

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/auth/session` | mock IdP: mint a session token (identity is token-derived) |
| `POST` | `/auth/step-up` | request + verify a step-up OTP challenge |
| `POST` | `/customers` | create a customer |
| `POST` | `/conversations` | start a conversation (mints a session token) |
| `POST` | `/conversations/{id}/messages` | send a message (enqueues a run) |
| `POST` | `/conversations/{id}/reply` | resume a suspended run (OTP / yes-no), re-validates identity |
| `GET`  | `/conversations/{id}/stream` | SSE: live agent trace + final response |
| `GET`  | `/conversations/{id}` | full message history |
| `POST` | `/escalations/{id}/resolve` | operator audit: record who handled it + when |
| `DELETE` | `/users/{id}/data` | right-to-erasure: tombstone PII, audit chain left intact |
| `POST` | `/eval/run`, `GET /eval/report`, `GET /eval/reports` | run + inspect evals |

Interactive docs at `localhost:8000/docs`.

## Evaluation

```bash
make eval               # run labeled scenario suite (LLM judge)
```

Scenarios live in [`data/scenarios/`](data/scenarios/); reports land in
`data/eval_reports/` and render in the frontend at `/eval`. The report includes
per-language judge **Cohen's κ** vs human labels — a language whose κ is undefined
(too few / unanimous labels) or below `JUDGE_KAPPA_FLOOR` is listed as
**untrusted** (its resolution metric needs human review). Eval pins `EMBEDDER=hashing`
for deterministic, reproducible scores.

## Maintenance jobs

```bash
python -m saral.jobs.purge     # retention: purge old redacted messages + dedup keys (keep audit 7y)
```

The worker also runs an **orphan reaper** on an interval: suspended conversations
whose pending write has out-lived its TTL are abandoned (never auto-fired) and
escalated. See [`backend/saral/jobs/`](backend/saral/jobs/).

## Development

```bash
make test               # pytest
make lint               # ruff check
make fmt                # ruff format
make typecheck          # mypy
make logs               # tail stack logs
```

Run `make help` for all targets.

## Deploy

Two process groups (API + worker) share one image. Default targets: **Fly.io** or
**Render** (single container; K8s out of scope). Frontend → **Vercel**. Full runbook:
[`docs/DEPLOY.md`](docs/DEPLOY.md).

```bash
# Fly.io
fly launch --no-deploy --name saral --region bom
fly postgres create --name saral-postgres --region bom && fly postgres attach saral-postgres
fly secrets set DATABASE_URL="postgresql+asyncpg://..." REDIS_URL="rediss://..." \
                SESSION_SECRET="$(openssl rand -hex 32)"
fly deploy && fly ssh console -C "cd /app && PYTHONPATH=backend python -m alembic upgrade head"

# Render: push to GitHub → New Blueprint → select render.yaml (migrations run on deploy)
```

> No LLM key needed — without one the deterministic stub provider serves a working,
> free demo (full pipeline, passing eval, streaming traces).

## Compliance

Designed for regulated Indian enterprises under the **DPDP Act 2023**:

- **Identity** — `user_id` is token-derived; writes require step-up + confirmation, re-validated
  on resume. No write executes under a stale auth level; the operator is **audit-only** (recorded
  on an escalation as who/when, never a graph actor).
- **Multi-tenancy** — every table carries `tenant_id` (leak-proof retrofit avoided).
- **PII** — redacted before storage and before any logged preview (regex by default,
  Presidio NER in prod with automatic regex fallback); the append-only, hash-chained `audit_log`
  holds only enum **reason codes** + tokenized refs (no raw PII), so the 7-year regulatory hold
  stays erasure-compatible.
- **Consent + erasure** — processing proceeds only while consent is `granted`; `DELETE
  /users/{id}/data` tombstones PII-bearing rows and withdraws consent, leaving the hash-chained
  audit valid. **Retention**: a purge job drops redacted messages after 90 days; audit kept 7 years.
- **Purpose limitation** — a deterministic intent→domain map bounds which data domain each
  intent may read; the LLM never controls it, so injection can't widen scope. Long-term memory
  (interaction history) is retrieved on demand through the same map, never eagerly.

---

*Personal project by Soumya Gupta. Not affiliated with any company.*
