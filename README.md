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
make chat      # chat with the live agent in the terminal (needs a model + key in backend/.env)
make test      # backend tests
make lint      # ruff, pyright, oxlint, tsc
make seed-data # regenerate the demo user's seed (memories + conversation)
make           # list all targets
```

To run the live agent, copy `backend/.env.example` to `backend/.env` (gitignored) and set
`ERRAND_MODEL` plus the API key for that provider, e.g.
`ERRAND_MODEL=anthropic:claude-opus-5-5` and `ANTHROPIC_API_KEY=...`. Tests and evals
never need a key: they run offline against a scripted model.

How it fits together: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Progress is tracked
in [TASKS.md](TASKS.md).
