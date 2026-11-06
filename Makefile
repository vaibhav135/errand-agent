# Dev tasks. Targets are written for GNU Make 3.81 (the macOS default).
.PHONY: help install dev demo dev-backend dev-frontend chat test lint format seed-data calibrate latency build

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

demo: ## Like dev, with the Demo tab (trigger provider failures live)
	@ERRAND_DEMO_MODE=true $(MAKE) -j2 dev-backend dev-frontend

dev-backend: ## Run FastAPI with reload on :8000
	cd $(BACKEND) && uv run uvicorn errand_agent.api.app:create_app --factory --reload --port 8000

dev-frontend: ## Run Vite dev server on :5173
	cd $(FRONTEND) && bun run dev --port 5173

chat: ## Chat with the live agent in the terminal (needs ERRAND_MODEL + key in backend/.env)
	cd $(BACKEND) && uv run python -m errand_agent.api.chat

# ulimit: run under macOS's default open-file limit everywhere, so a descriptor leak
# fails here too, not only on a default Mac (BUG-9).
test: ## Run backend and frontend tests
	cd $(BACKEND) && ulimit -n 256 && uv run pytest
	cd $(FRONTEND) && bun run test

lint: ## Lint + typecheck backend and frontend
	cd $(BACKEND) && uv run ruff check . && uv run ruff format --check . && uv run pyright
	cd $(FRONTEND) && bun run lint && bunx tsc -b

format: ## Auto-format backend
	cd $(BACKEND) && uv run ruff format . && uv run ruff check --fix .

seed-data: ## Regenerate the committed demo-user seed (memories + conversation)
	cd $(BACKEND) && uv run python -m errand_agent.memory.seedgen

calibrate: ## Measure real token counts (free count_tokens API; needs a key) for the estimator test
	cd $(BACKEND) && uv run python scripts/calibrate_tokens.py

# Live model calls: a few cents per run. Pass options with ARGS, e.g. ARGS="--effort low --cache".
latency: ## Time to first word and total for a few live turns (costs a few cents; needs a key)
	cd $(BACKEND) && uv run python scripts/measure_latency.py $(ARGS)

build: ## Production build of the frontend
	cd $(FRONTEND) && bun run build
