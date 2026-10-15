# Saral — Deploy Runbook

**Live:** https://soumya0343-saral.hf.space — a free Hugging Face ZeroGPU Space
([Space page](https://huggingface.co/spaces/soumya0343/saral)).

Saral is two process groups (API + worker) sharing one image, plus PostgreSQL 16, Redis 7 and a
Next.js frontend. Three ways to run it:

| Target | Cost | Notes |
|---|---|---|
| **Hugging Face Space** (live) | free | one container: Gradio shell + Postgres + Redis + worker + API + static frontend |
| Fly.io / Render | paid | separate API and worker machines; `fly.toml`, `render.yaml` |
| Any VM with Docker | VM cost | `docker-compose.yml` as in local development |

No LLM key is required anywhere: without one, the deterministic **stub provider** runs the full
pipeline. With free Gemini / Groq keys the real models answer.

## Hugging Face Space (free)

### How it is put together

ZeroGPU is the only free hardware for an app Space, and ZeroGPU Spaces must be Gradio apps that
start through `demo.launch()`. [`deploy/hf-space/app.py`](../deploy/hf-space/app.py) is a small
supervisor around Gradio's server:

| Piece | How it runs |
|---|---|
| Postgres 16 | local disk (`/tmp`), started as the `postgres` user (the Space runs as root) |
| Redis | `127.0.0.1` only; run bus and trace streams, ephemeral |
| Migrations | `alembic upgrade head` on every boot |
| Worker | `python -m saral.worker.main`, its own process, restarted if it exits |
| API | Saral's FastAPI app mounted at `/api` on Gradio's server |
| Frontend | static Next.js build served at `/` and `/eval`, calling `/api` on the same origin |

Saral never uses the GPU; e5 runs on the Space's CPUs (16 cores). A no-op `@spaces.GPU`
function exists only because ZeroGPU Spaces expect one.

**Persistence.** Space disk is wiped on restart, and Spaces can only connect out on ports 80, 443
and 8080, so a hosted Postgres (port 5432) is unreachable. The database therefore lives in the
Space and is dumped (`pg_dump`, ~40KB) to the private storage bucket `soumya0343/saral-data`,
mounted at `/data`:

- every `SARAL_DUMP_EVERY_S` seconds (default 60) when rows changed, and on SIGTERM;
- restored at boot when no database exists yet.

A settings-change restart was observed to kill the container before the SIGTERM dump, so the
periodic dump is what bounds data loss — at most about a minute. The bucket cannot hold the live
database itself: it keeps files across restarts but not their permissions, and Postgres refuses
a data directory it doesn't own.

**Client IP.** Hugging Face's proxy *appends* the real client IP to `X-Forwarded-For` and keeps
whatever the client sent in front. The Space sets `CLIENT_IP_HEADER=X-Forwarded-For` and
`CLIENT_IP_FROM_RIGHT=1`, so rate limits use the address the proxy saw, not a forged one.

### Deploy / redeploy

```bash
brew install hf && hf auth login          # once
deploy/hf-space/build.sh                  # build into deploy/hf-space/.build and upload
UPLOAD=0 deploy/hf-space/build.sh         # build only, to inspect what would ship
```

The script ships only what the Space runs (backend, `data/`, `alembic.ini`, the static frontend,
`app.py`, `packages.txt`) and generates `requirements.txt` from `pyproject.toml`. The Space is
built from your **local working tree**, not from GitHub: pushing to `main` does not redeploy it.
Every upload rebuilds and restarts the Space.

### First-time setup (already done for `soumya0343/saral`)

```bash
hf buckets create <user>/saral-data --private
hf repos create <user>/saral --type space --sdk gradio --flavor zero-a10g \
  --volume hf://buckets/<user>/saral-data:/data --secrets-file <file> --private
SPACE=<user>/saral deploy/hf-space/build.sh
hf repos settings <user>/saral --type space --public      # once it works
```

Requirements: a free account older than 30 days with a verified email (it may host 2 ZeroGPU
Spaces). The Space must be **public** for visitors; Hugging Face's "protected" (public app,
private code) needs PRO. Secrets stay hidden either way.

### Secrets and variables

| Secret | Notes |
|---|---|
| `SESSION_SECRET` | `openssl rand -hex 32` |
| `RM_REQUEST_KEY` | Fernet key; losing or rotating it makes stored RM requests unreadable |
| `SERVICE_API_KEY` | for the RM endpoints (`X-Service-Key`) |
| `GEMINI_API_KEY`, `GROQ_API_KEY`, `SARVAM_API_KEY` | free tiers; optional |

Hugging Face secrets are write-only. The local copy of the generated three is
`~/.saral/hf-space-secrets.env` (mode 600). Change one with
`hf spaces secrets add <user>/saral -s KEY=value`.

Everything else has a default in `app.py` (`APP_ENV=prod`, `DEMO_MODE=true`, Postgres and Redis
URLs, `STORE_BACKEND` / `CHECKPOINT_BACKEND=postgres`, the client-IP settings); a Space variable
overrides any of them.

### Operating it

```bash
hf spaces logs <user>/saral            # runtime log ("[space] …" lines are the supervisor)
hf spaces logs <user>/saral --build    # build log
KEY=$(grep ^SERVICE_API_KEY= ~/.saral/hf-space-secrets.env | cut -d= -f2-)
curl -s -H "X-Service-Key: $KEY" https://<user>-saral.hf.space/api/rm/requests?status=open
```

- **Restart:** `hf spaces restart` returns 403 on free ZeroGPU ("Subscribe to PRO"). Redeploy, or
  change a variable: `hf spaces variables add <user>/saral --env SARAL_DUMP_EVERY_S=60`.
- **Sleep:** free Spaces sleep when unused and wake on the next visit (Postgres restore, then e5
  and the indexes load: about 40s from container start to a ready worker, measured on a restart).
- **Quota:** every visitor's chat spends your free Gemini / Groq quota; past it, replies fall
  back to the deterministic stub.
- **Demo data is synthetic.** `DEMO_MODE=true` shows OTPs as a popup, so anyone can sign in as a
  demo customer. Don't load real data.

## Environment variables (all targets)

All settings live in `backend/saral/config.py` with safe local defaults. In prod (`APP_ENV=prod`)
the app refuses to start without `SESSION_SECRET` (≥32 bytes) and `RM_REQUEST_KEY`.

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | local | `postgresql+asyncpg://<user>:<pass>@<host>:5432/saral` |
| `REDIS_URL` | local | `redis://…` or `rediss://…` |
| `SESSION_SECRET` | dev value | HMAC key for session tokens — `openssl rand -hex 32` |
| `RM_REQUEST_KEY` | derived in dev | Fernet key: `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"` |
| `FRONTEND_ORIGINS` | `http://localhost:3000` | CORS allow-list (not needed when frontend and API share an origin) |
| `GEMINI_API_KEY`, `GROQ_API_KEY` | unset | free model tiers; several comma-separated keys rotate |
| `SARVAM_API_KEY` | unset | language detection only |
| `LLM_ROLE_SYNTHESIS` / `_TRIAGE` / `_JUDGE` | free Gemini / Groq chains | see `.env.example` |
| `STORE_BACKEND` / `CHECKPOINT_BACKEND` | `memory` | set `postgres` in prod for durability and crash-resume |
| `SERVICE_API_KEY` | unset | RM endpoints (`/rm/requests…`), legacy `/escalations/{id}/resolve`, and `POST /eval/run` in prod; unset disables them |
| `RM_APPROVAL_ACTIONS` | `update_contact,raise_ticket,file_claim` | confirmed writes raised to the RM instead of executed |
| `DEMO_MODE` | `false` | no SMS channel: show the OTP as a simulated SMS popup |
| `CLIENT_IP_HEADER` | unset | header with the real client IP for rate limits (`Fly-Client-IP` on Fly, `X-Forwarded-For` on Hugging Face) |
| `CLIENT_IP_FROM_RIGHT` | `0` | for a comma-separated header: which entry from the right is the client (`1` behind a proxy that appends, e.g. Hugging Face); `0` = first entry |
| `WORKER_METRICS_PORT` | `9100` | the worker's Prometheus port (`0` = off) |
| `OTP_MAX_ATTEMPTS` | `5` | wrong OTP guesses before a challenge is burned |

## Fly.io (paid)

No free plan for new accounts. The worker needs ≥1GB (torch + multilingual-e5: ~870MB resident,
~965MB peak at warm-up); the API fits 512MB.

```bash
fly launch --no-deploy --name saral --region bom
fly postgres create --name saral-postgres --region bom
fly postgres attach saral-postgres --app saral   # use the +asyncpg scheme in DATABASE_URL
fly secrets set \
  DATABASE_URL="postgresql+asyncpg://<user>:<pass>@<host>.internal:5432/saral" \
  REDIS_URL="rediss://default:<token>@<upstash-host>:<port>" \
  SESSION_SECRET="$(openssl rand -hex 32)" \
  RM_REQUEST_KEY="<Fernet key>" \
  GEMINI_API_KEY="..." GROQ_API_KEY="..."
fly deploy
fly ssh console -C "cd /app && PYTHONPATH=backend python -m alembic upgrade head"
curl https://saral.fly.dev/ready
```

## Render (paid)

Free web services sleep and are capped at 512MB, free Postgres is deleted after 30 days, and
there is no free background worker, so `render.yaml` uses paid plans.

1. Push the repo to GitHub.
2. Render → New → Blueprint → select `render.yaml`. It creates `saral-api`, `saral-worker`,
   `saral-postgres` and `saral-redis`.
3. Set the secret env vars on both services: `SESSION_SECRET`, `RM_REQUEST_KEY`, and optionally
   `GEMINI_API_KEY` / `GROQ_API_KEY` / `SARVAM_API_KEY`. `DATABASE_URL` / `REDIS_URL` are wired.
4. Migrations run via `preDeployCommand`.

Frontend for Fly/Render: Vercel, root directory `frontend`, with `NEXT_PUBLIC_API_URL` set to
the API host in the project settings.

## Smoke test (any target)

```bash
BASE=https://soumya0343-saral.hf.space/api     # or https://saral.fly.dev
curl -s $BASE/ready

# Sign in as a seeded customer: existing accounts need the login OTP (DEMO_MODE returns it)
CH=$(curl -s -X POST $BASE/customers -H 'Content-Type: application/json' \
  -d '{"name":"Asha Verma","mobile":"9876500001"}')
TOK=$(curl -s -X POST $BASE/auth/login/verify -H 'Content-Type: application/json' \
  -d "{\"challenge_id\":\"$(echo $CH | jq -r .challenge_id)\",\"code\":\"$(echo $CH | jq -r .test_otp)\"}" \
  | jq -r .tokens.access_token)
AUTH="Authorization: Bearer $TOK"

CONV=$(curl -s -X POST $BASE/conversations -H "$AUTH" | jq -r .id)
RUN=$(curl -s -X POST $BASE/conversations/$CONV/messages -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"content":"Why was my claim CLM2010 partially reduced?"}' | jq -r .run_id)
curl -sN "$BASE/conversations/$CONV/stream?run_id=$RUN" -H "$AUTH"   # trace + final answer
```

## Notes

- **SSE:** `fly.toml` sets `X-Accel-Buffering: no`; on Hugging Face events were measured
  arriving live (~0.2s).
- **Worker scaling** (Fly/Render): the worker reads a Redis Streams consumer group; add machines
  to scale horizontally.
- **Step-up OTP:** there is no SMS channel. With `DEMO_MODE=false` the code is never surfaced and
  writes need a real delivery channel.
