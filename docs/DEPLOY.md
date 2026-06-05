# Saral — Deploy Runbook

Saral ships as two process groups (API + worker) sharing one image, plus Postgres 16, Redis 7,
and a Next.js frontend on Vercel. Default targets: **Fly.io** or **Render** (single container;
Kubernetes is out of scope for the demo). No LLM key is required — without one the deterministic
**stub provider** runs the full pipeline (eval passes, traces stream) for a free, offline demo.

## Environment variables

All settings live in `backend/saral/config.py` (pydantic-settings) with safe local defaults.
Only the datastore URLs and `SESSION_SECRET` must be set in production.

### Required in prod (no safe default)

| Variable | Description | Source |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://<user>:<pass>@<host>:5432/saral` | `fly postgres attach` / Render managed PG `connectionString` |
| `REDIS_URL` | `redis://...` or `rediss://...` (TLS) | Upstash (Fly) / Render managed Redis |
| `SESSION_SECRET` | ≥32-byte HMAC key for signing session tokens (the mock IdP) | generate: `openssl rand -hex 32` |

### Optional (app falls back to the stub LLM)

| Variable | Default | Description |
|---|---|---|
| `SARVAM_API_KEY` | none | Sarvam — Hindi/Hinglish language understanding |
| `ANTHROPIC_API_KEY` | none | Anthropic (Claude) fallback |
| `LLM_PROVIDER_ORDER` | `sarvam,anthropic,stub` | provider fallback chain |
| `STORE_BACKEND` / `CHECKPOINT_BACKEND` | `memory` | set `postgres` in prod for durability + cross-process resume |
| `SESSION_TTL_MIN` | `30` | session token TTL |
| `STEP_UP_TTL_S` | `300` | OTP challenge validity |
| `EXPOSE_TEST_OTP` | `true` | surface the OTP in responses (auto-off when `APP_ENV=prod`) |

## Fly.io

```bash
fly launch --no-deploy --name saral --region bom
fly postgres create --name saral-postgres --region bom
fly postgres attach saral-postgres --app saral   # note the DATABASE_URL; use the +asyncpg scheme
fly secrets set \
  DATABASE_URL="postgresql+asyncpg://<user>:<pass>@<host>.internal:5432/saral" \
  REDIS_URL="rediss://default:<token>@<upstash-host>:<port>" \
  SESSION_SECRET="$(openssl rand -hex 32)" \
  SARVAM_API_KEY="sk_..." LLM_PROVIDER_ORDER="sarvam,anthropic,stub"
fly deploy
fly ssh console -C "cd /app && PYTHONPATH=backend python -m alembic upgrade head"
curl https://saral.fly.dev/health
```

## Render

1. Push the repo to GitHub.
2. Render dashboard → New → Blueprint → select `render.yaml`. It creates `saral-api`,
   `saral-worker`, `saral-postgres` (PG 16), `saral-redis` (Redis 7).
3. Set secret env vars on both services: `SESSION_SECRET`, and optionally `SARVAM_API_KEY` /
   `ANTHROPIC_API_KEY` / `LLM_PROVIDER_ORDER`. `DATABASE_URL` / `REDIS_URL` are auto-wired.
4. Migrations run automatically via `preDeployCommand` (`alembic upgrade head`).

## Frontend (Vercel)

```bash
cd frontend && npx vercel deploy --prod
# Vercel → Project Settings → Environment Variables: NEXT_PUBLIC_API_URL = https://saral.fly.dev
```

## Smoke test (live API)

```bash
BASE=https://saral.fly.dev
curl -s $BASE/health | jq .

# Mock IdP: mint a session token (identity is token-derived thereafter)
TOK=$(curl -s -X POST $BASE/auth/session -H 'Content-Type: application/json' \
  -d '{"user_id":"U1001"}' | jq -r .token)

# Start a conversation, send a Hindi explanation query, stream the trace + grounded answer
CONV=$(curl -s -X POST $BASE/conversations -H 'Content-Type: application/json' \
  -d '{"user_id":"U1001"}' | jq -r .id)
curl -s -X POST $BASE/conversations/$CONV/messages -H 'Content-Type: application/json' \
  -d '{"content":"Why was my claim CLM2010 partially reduced?"}'
curl -sN $BASE/conversations/$CONV/stream

# Eval suite
curl -s -X POST $BASE/eval/run | jq '.summary'
```

## Notes

- **DPDP Act 2023**: deploy to `bom` (Fly) or `singapore` (Render) for data residency. Raw PII
  is redacted before storage; the hash-chained `audit_log` holds only reason codes + tokenized
  refs.
- **SSE**: `fly.toml` sets `X-Accel-Buffering: no` so stream chunks are not buffered.
- **Worker scaling**: the worker consumes a Redis Streams consumer group; add machines to scale
  horizontally — no config change.
- **Step-up OTP**: there is no SMS channel; in non-prod the OTP is returned in the response
  (`test_otp`). `APP_ENV=prod` disables that surface.
