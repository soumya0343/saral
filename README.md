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
  `user_id` ∧ an intent→domain purpose-limitation map.
- **Real agent orchestration**, not a single prompt — a LangGraph supervisor routes
  to specialist agents and fans out parallel branches for mixed intents.
- **Compliance as a gate, not a peer** — injection detection, referenced-id ownership,
  and authorization run *before* any action executes; PII is redacted before storage
  and before the response is emitted.
- **Suspend/resume with exactly-once writes** — a write suspends for step-up +
  confirmation and resumes in a fresh run that re-validates identity; a
  conversation-anchored idempotency key prevents double-writes across crash-resume.
- **Event-driven** — the API enqueues runs onto a Redis Streams bus; a separate
  worker consumes them, with checkpoint/resume and partial-failure isolation.
- **Measured, not vibes** — ~95 labeled scenarios (explanation-groundedness,
  impersonation, multilingual triples, injection) scored by an LLM judge validated
  per language, surfaced in a frontend eval dashboard.
- **Runs offline** — no LLM key needed in dev; a deterministic stub provider keeps
  the full stack reproducible.

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
       ├─ rag → synthesis        (information; per-customer scoped retrieval)
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
- [`authz/`](backend/saral/authz/) — intent→domain purpose-limitation map + action risk tiers
- [`actions/`](backend/saral/actions/) — write confirmation + conversation-anchored idempotency
- [`compliance/`](backend/saral/compliance/) — `injection`, `authz`, `gate`, `pii`, `audit`
- [`graph/`](backend/saral/graph/) — supervisor graph, run state machine, checkpointing
- [`llm/`](backend/saral/llm/) — provider abstraction (Sarvam · Anthropic · stub)
- [`rag/`](backend/saral/rag/) — generic corpus + per-customer index over [`data/customers/`](data/customers/)
- [`eval/`](backend/saral/eval/) — scenarios, metrics, per-language LLM judge, runner

## Stack

FastAPI · LangGraph · PostgreSQL 16 · Redis 7 (Streams) · Next.js + Tailwind ·
Presidio (PII) · uv

## Quickstart

```bash
uv sync                 # install backend deps
make up                 # postgres + redis + api + worker (docker compose)
make migrate            # apply DB migrations
curl localhost:8000/health
```

Frontend:

```bash
cd frontend && npm install && npm run dev   # http://localhost:3000
```

> **LLM keys are optional in dev.** With no `SARVAM_API_KEY` / `ANTHROPIC_API_KEY`
> set, the system falls back to a deterministic stub provider, so the full stack
> runs offline and reproducibly.

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
| `POST` | `/eval/run`, `GET /eval/report`, `GET /eval/reports` | run + inspect evals |

Interactive docs at `localhost:8000/docs`.

## Evaluation

```bash
make eval               # run labeled scenario suite (LLM judge)
```

Scenarios live in [`data/scenarios/`](data/scenarios/); reports land in
`data/eval_reports/` and render in the frontend at `/eval`.

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
  on resume. No write executes under a stale auth level.
- **Multi-tenancy** — every table carries `tenant_id` (leak-proof retrofit avoided).
- **PII** — redacted before storage and before any logged preview; the append-only,
  hash-chained `audit_log` holds only reason codes + tokenized refs (no raw PII), so the 7-year
  regulatory hold stays erasure-compatible.
- **Purpose limitation** — a deterministic intent→domain map bounds which data domain each
  intent may read; the LLM never controls it, so injection can't widen scope.

---

*Personal project by Soumya Gupta. Not affiliated with any company.*
