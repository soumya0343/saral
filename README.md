# Saral — Multilingual Enterprise Support-Resolution Agent

> सरल — "simple"

A supervisor-orchestrated multi-agent system that resolves customer-support
queries for regulated enterprises (insurance / lending). It understands English,
Hindi, and Hinglish, retrieves grounded answers from a policy corpus, takes
authorized account actions, enforces compliance + PII rules on every turn, and is
validated by a labeled evaluation suite with an LLM judge.

## Why this is interesting

- **Real agent orchestration**, not a single prompt — a LangGraph supervisor
  routes to specialist agents and fans out parallel branches for mixed intents.
- **Compliance as a gate, not a peer** — injection detection + authorization run
  *before* any action executes; PII is redacted before storage and before the
  response is emitted.
- **Event-driven** — the API enqueues runs onto a Redis Streams bus; a separate
  worker consumes them, with checkpoint/resume and partial-failure isolation.
- **Measured, not vibes** — a labeled scenario suite scored by an LLM judge,
  surfaced in a frontend eval dashboard.
- **Runs offline** — no LLM key needed in dev; a deterministic stub provider
  keeps the full stack reproducible.

## Architecture

```
                       Redis Streams (run bus)
   FastAPI  ──enqueue──►  ════════════  ──consume──►  Worker
   (REST + SSE)                                       │
                                                      ▼
                                        LangGraph supervisor graph
                                                      │
   START → triage → compliance → supervisor ─route─►  ┐
       ├─ synthesis              (respond / injection-blocked → escalate)
       ├─ rag → synthesis        (information)
       ├─ action → synthesis     (account action; gate pre-enforced)
       └─ {rag, action} → synthesis   (mixed intent: parallel fan-out)
                                                      │
                                            synthesis → END
```

**Agents** ([`backend/saral/agents/`](backend/saral/agents/))
- `triage` — language + intent + entity detection
- `rag` — grounded retrieval over the policy corpus
- `action` — authorized account actions against a mock backend
- `synthesis` — composes the final grounded, language-matched response

**Cross-cutting**
- [`compliance/`](backend/saral/compliance/) — `injection`, `authz`, `gate`, `pii`, `audit`
- [`graph/`](backend/saral/graph/) — supervisor graph, run state, checkpointing
- [`llm/`](backend/saral/llm/) — provider abstraction (Sarvam · Anthropic · stub)
- [`rag/`](backend/saral/rag/) — embedder + index over [`data/policy_corpus/`](data/policy_corpus/)
- [`eval/`](backend/saral/eval/) — scenarios, metrics, LLM judge, runner

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
| `POST` | `/customers` | create a customer |
| `POST` | `/conversations` | start a conversation |
| `POST` | `/conversations/{id}/messages` | send a message (enqueues a run) |
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

---

*Personal project by Soumya Gupta. Not affiliated with any company.*
