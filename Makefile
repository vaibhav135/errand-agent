# Dev tasks. Targets are written for GNU Make 3.81 (the macOS default).
.PHONY: help install dev dev-backend dev-frontend chat test lint format seed-data build

BACKEND := backend
FRONTEND := frontend

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

install: ## Install backend (uv) and frontend (bun) dependencies
	cd $(BACKEND) && uv sync
	cd $(FRONTEND) && bun install

# -j2 runs both servers in parallel; Ctrl-C stops both.
dev: ## Run backend (:8000) and frontend (:5173) together
	@$(MAKE) -j2 dev-backend dev-frontend

dev-backend: ## Run FastAPI with reload on :8000
	cd $(BACKEND) && uv run uvicorn errand_agent.api.app:create_app --factory --reload --port 8000

dev-frontend: ## Run Vite dev server on :5173
	cd $(FRONTEND) && bun run dev --port 5173

chat: ## Chat with the live agent in the terminal (needs ERRAND_MODEL + key in backend/.env)
	cd $(BACKEND) && uv run python -m errand_agent.api.chat

test: ## Run backend tests
	cd $(BACKEND) && uv run pytest

lint: ## Lint + typecheck backend and frontend
	cd $(BACKEND) && uv run ruff check . && uv run ruff format --check . && uv run pyright
	cd $(FRONTEND) && bun run lint && bunx tsc -b

format: ## Auto-format backend
	cd $(BACKEND) && uv run ruff format . && uv run ruff check --fix .

seed-data: ## Regenerate the committed demo-user seed (memories + conversation)
	cd $(BACKEND) && uv run python -m errand_agent.memory.seedgen

build: ## Production build of the frontend
	cd $(FRONTEND) && bun run build
