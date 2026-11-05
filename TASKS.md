# TASKS

Atomic commits (Conventional Commits). Each commit ticks its own item(s) here.
Features/refactors: test + impl in one green commit. Bug fixes: `test:` repro commit
(`xfail(strict=True)`) then `fix:` commit; both logged in [BUGS.md](BUGS.md).

## Phase 0 — Setup
- [x] Repo hygiene: `.gitignore`, TASKS.md, BUGS.md, README stub
- [x] Backend scaffold (uv, FastAPI, PydanticAI, pytest, ruff, pyright) + smoke test
- [x] Frontend scaffold (bun, Vite, React, TS, Tailwind, shadcn/ui)
- [ ] `justfile`: `dev`, `test`, `eval`, `lint`

## Phase 1 — Foundations
- [ ] `core/`: injectable Clock (real + fake), config from env, ID helpers
- [ ] `store/`: SQLite connection (WAL), SQL migrations runner, `agent.db`
- [ ] `tracing/`: JSONL trace recorder (state transitions, model calls, tools, latency, tokens, cost)
- [ ] Import-direction test (`workflow/` must not import `agent/` or `api/`)

## Phase 2 — Mock world
- [ ] `mock_world.db`, seeded providers (incl. a prompt-injection review), scenario config + seeded RNG
- [ ] `search_providers`, `get_provider`
- [ ] `hold_slot` (10-min expiry, configurable price drift)
- [ ] `book` (70/15/15 outcomes, timeout-after-success, `hold_expired`) + `get_booking_status`
- [ ] `cancel` (first call 503), `reschedule` (`slot_conflict`), `list_bookings`

## Phase 3 — Workflow core
- [ ] State model + explicit transitions
- [ ] Approval gate: approval bound to hash(action, provider, slot, price); invalidated on change
- [ ] Booking executor: persisted idempotency key, timeout → `get_booking_status`, no duplicate bookings
- [ ] Cancel / reschedule with bounded retries
- [ ] Hard limits: model turns, tool calls, wall-clock; every exit has a user-facing outcome

## Phase 4 — Memory & context
- [ ] `memories` table: explicit vs inferred, active / pending_confirm / forgotten
- [ ] Inspect / correct / forget operations
- [ ] Budgeted context selection + per-turn context log
- [ ] Seed ≥50 memories + long prior conversation

## Phase 5 — Agent
- [ ] PydanticAI orchestrator + tool wrappers (model configurable via env)
- [ ] Quarantined review-reader sub-agent (no tools; typed summary only)
- [ ] Chat-history persistence (`ModelMessagesTypeAdapter`)

## Phase 6 — Proactive follow-up
- [ ] Simulated provider-cancellation event + monitor
- [ ] Notify → replacement search within preferences → fresh approval → rebook

## Phase 7 — API & Web UI
- [ ] FastAPI routes: messages, approvals, memories, SSE event stream
- [ ] ChatPane + ApprovalCard (button + typed "yes")
- [ ] TracePane (live trace events)
- [ ] MemoryPanel (inspect / correct / forget)

## Phase 8 — Evals
- [ ] Scripted-model harness (`FunctionModel`) + simulated users
- [ ] ≥20 scenarios asserting outcomes and invariants
- [ ] Generated eval table (`evals/results.md`)

## Phase 9 — Deliverables
- [ ] README (setup, run, test, eval)
- [ ] Design note (≤1 page) + annotated timeout-after-success trace excerpt
- [ ] BUGS.md finalised (3 real bugs)
- [ ] Raw end-to-end trace in `docs/traces/`
- [ ] Demo recording (3–5 min, chat + trace side by side)
- [ ] AI coding transcripts in `transcripts/`
