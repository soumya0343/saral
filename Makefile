.PHONY: help install up down logs api worker test lint fmt typecheck migrate eval

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

eval: ## Run eval suite (Phase 4+)
	PYTHONPATH=backend uv run python -m saral.eval.runner
