.PHONY: help install up down logs api worker test lint fmt typecheck migrate eval eval-gate eval-baseline eval-live eval-labels

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install backend deps (uv)
	uv sync

up: ## Start full stack (postgres, redis, api, worker)
	docker compose up --build -d

down: ## Stop stack
	docker compose down

logs: ## Tail stack logs
	docker compose logs -f

api: ## Run API locally (needs postgres+redis up)
	uv run uvicorn saral.api.app:app --reload --app-dir backend

dev: ## Run API + worker in ONE process (shared mock state) for local/frontend testing
	RUN_WORKER_INPROC=true uv run uvicorn saral.api.app:app --reload --app-dir backend

worker: ## Run worker locally
	uv run python -m saral.worker.main

test: ## Run pytest
	uv run pytest -q

lint: ## Ruff check
	uv run ruff check backend tests

fmt: ## Ruff format
	uv run ruff format backend tests

typecheck: ## mypy
	uv run mypy backend

migrate: ## Apply DB migrations (Phase 1+)
	uv run alembic upgrade head

# Offline tier: stub LLM + hashing embedder, whatever .env says (deterministic, free, CI).
EVAL_OFFLINE_ENV = APP_ENV=test EMBEDDER=hashing LLM_PROVIDER_ORDER=stub LLM_ROLE_TRIAGE=stub \
	LLM_ROLE_SYNTHESIS=stub LLM_ROLE_JUDGE=stub GROQ_API_KEY= GEMINI_API_KEY= CEREBRAS_API_KEY= \
	SARVAM_API_KEY= ANTHROPIC_API_KEY= LOG_LEVEL=ERROR PYTHONPATH=backend

eval: ## Offline eval (deterministic, no API keys used)
	$(EVAL_OFFLINE_ENV) uv run python -m saral.eval.runner --tier offline

eval-gate: ## Offline eval + fail if any metric is below data/eval_baselines/offline.json
	$(EVAL_OFFLINE_ENV) uv run python -m saral.eval.gate

eval-baseline: ## Re-baseline the offline tier after a genuine improvement (commit the file)
	$(EVAL_OFFLINE_ENV) uv run python -m saral.eval.gate --update

eval-live: ## Live eval with the real free models from .env (throttled; ARGS="--limit 20" / "--resume")
	PYTHONPATH=backend uv run python -m saral.eval.runner --tier live $(ARGS)

eval-heldout: ## Injection guard on the blind held-out sets (not gated; see saral/eval/heldout.py)
	LOG_LEVEL=ERROR PYTHONPATH=backend uv run python -m saral.eval.heldout

calibrate-floor: ## Retrieval score-floor calibration for the active embedder (needs e5 locally)
	LOG_LEVEL=ERROR PYTHONPATH=backend uv run python scripts/calibrate_floor.py

eval-labels: ## Add the latest live run's replies to data/eval_labels/live_labels.yaml for human labelling
	PYTHONPATH=backend uv run python -m saral.eval.labels export
