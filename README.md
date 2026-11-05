# Errand Agent

A chat-based agent that arranges home-service errands (search, compare, hold, book, cancel,
reschedule) against local mock providers, with code-enforced approvals, idempotent booking,
persistent memory, and full run traces.

- `backend/`: Python 3.12 · uv · FastAPI · PydanticAI · SQLite
- `frontend/`: bun · Vite · React · TypeScript · Tailwind · shadcn/ui

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and [bun](https://bun.sh/).

```sh
make install   # backend + frontend deps
make dev       # FastAPI on :8000, Vite on :5173 (open http://localhost:5173)
make test      # backend tests
make lint      # ruff, pyright, oxlint, tsc
make           # list all targets
```

Progress is tracked in [TASKS.md](TASKS.md).
