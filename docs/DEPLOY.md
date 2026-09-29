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
| `SESSION_SECRET` | ≥32-byte HMAC key for signing session tokens (the mock IdP). **Boot fails in prod if unset/default.** | generate: `openssl rand -hex 32` |
| `FRONTEND_ORIGINS` | CORS allow-list: the deployed frontend origin(s), comma-separated | e.g. `https://saral.vercel.app` |

### Optional (app falls back to the stub LLM)

| Variable | Default | Description |
|---|---|---|
| `SARVAM_API_KEY` | none | Sarvam — Hindi/Hinglish language understanding |
| `ANTHROPIC_API_KEY` | none | Anthropic (Claude) fallback |
| `LLM_PROVIDER_ORDER` | `sarvam,anthropic,stub` | provider fallback chain |
| `STORE_BACKEND` / `CHECKPOINT_BACKEND` | `memory` | set `postgres` in prod for durability + cross-process resume |
| `SESSION_TTL_MIN` | `30` | session token TTL |
| `STEP_UP_TTL_S` | `300` | OTP challenge validity |
| `SERVICE_API_KEY` | unset | server-to-server key for the RM's system (`/escalations/{id}/resolve`) and `POST /eval/run` in prod; unset disables both |
| `CLIENT_IP_HEADER` | unset | header with the real client IP for rate limits (`Fly-Client-IP` on Fly) |
| `DEMO_MODE` | `false` | no SMS channel: show the OTP as a simulated SMS popup (set `true` for the public demo) |
| `OTP_MAX_ATTEMPTS` | `5` | wrong OTP guesses before a challenge is burned |

## Sizing

One image (`Dockerfile.api`) runs every process; the command picks api or worker. The worker
holds torch + multilingual-e5 — measured ~870MB resident, ~965MB peak while the indexes warm at
startup — so it needs **≥1GB** (configured: Fly `1536mb`, Render `standard`). The api idles at
~100MB and fits 512MB, but `POST /eval` loads e5 in the api process: run evals in CI or on the
worker, or size the api like the worker.

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

# Sign in as a seeded customer: existing accounts need the login OTP (DEMO_MODE returns it)
CH=$(curl -s -X POST $BASE/customers -H 'Content-Type: application/json' \
  -d '{"name":"Asha Verma","mobile":"9876500001"}')
TOK=$(curl -s -X POST $BASE/auth/login/verify -H 'Content-Type: application/json' \
  -d "{\"challenge_id\":\"$(echo $CH | jq -r .challenge_id)\",\"code\":\"$(echo $CH | jq -r .test_otp)\"}" \
  | jq -r .tokens.access_token)
AUTH="Authorization: Bearer $TOK"

# Start a conversation, stream the trace, send a question (identity comes from the token)
CONV=$(curl -s -X POST $BASE/conversations -H "$AUTH" | jq -r .id)
curl -sN $BASE/conversations/$CONV/stream -H "$AUTH" &
curl -s -X POST $BASE/conversations/$CONV/messages -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"content":"Why was my claim CLM2010 partially reduced?"}'

# Eval suite: run it with `make eval` (POST /eval/run needs X-Service-Key in prod)
```

## Notes

- **DPDP Act 2023**: deploy to `bom` (Fly) or `singapore` (Render) for data residency. Raw PII
  is redacted before storage; the hash-chained `audit_log` holds only reason codes + tokenized
  refs.
- **SSE**: `fly.toml` sets `X-Accel-Buffering: no` so stream chunks are not buffered.
- **Worker scaling**: the worker consumes a Redis Streams consumer group; add machines to scale
  horizontally — no config change.
- **Step-up OTP**: there is no SMS channel; with `DEMO_MODE=true` the OTP is returned as
  `test_otp` and shown as a simulated SMS popup. With `DEMO_MODE=false` it is never surfaced, and
  writes need a real delivery channel.
