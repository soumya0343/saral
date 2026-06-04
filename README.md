# Saral — Multilingual Enterprise Support-Resolution Agent

A supervisor-orchestrated multi-agent customer-support resolution system for regulated
enterprises (insurance/lending). Understands English / Hindi / Hinglish, retrieves grounded
answers, takes authorized account actions, enforces compliance + PII rules, and is validated
by a labeled evaluation suite.

See [`Saral_PRD_TRD.md`](Saral_PRD_TRD.md) for the product + technical spec, and
[`.claude/plans/00-roadmap.md`](.claude/plans/00-roadmap.md) for the build roadmap.

## Stack
FastAPI · LangGraph · PostgreSQL 16 · Redis 7 (Streams) · Next.js + Tailwind · Presidio · uv

## Quickstart (local)
```bash
uv sync                 # install backend deps
make up                 # postgres + redis + api + worker via docker compose
curl localhost:8000/health
```

## Development
```bash
make test               # pytest
make lint               # ruff
make migrate            # alembic upgrade head
make eval               # run eval suite (Phase 4+)
```

> **LLM keys are optional in dev.** With no Sarvam/Anthropic key set, the system uses a
> deterministic stub provider so the full stack runs offline and reproducibly.
