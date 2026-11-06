# TASKS

Atomic commits (Conventional Commits). Each commit ticks its own item(s) here.
Features/refactors: test + impl in one green commit. Bug fixes: `test:` repro commit
(`xfail(strict=True)`) then `fix:` commit; both logged in [BUGS.md](BUGS.md).

## Phase 0 — Setup
- [x] Repo hygiene: `.gitignore`, TASKS.md, BUGS.md, README stub
- [x] Backend scaffold (uv, FastAPI, PydanticAI, pytest, ruff, pyright) + smoke test
- [x] Frontend scaffold (bun, Vite, React, TS, Tailwind, shadcn/ui)
- [x] `Makefile`: `install`, `dev`, `test`, `lint`, `format`, `build` (`eval` added in Phase 8)

## Phase 1 — Foundations
- [x] `core/`: injectable Clock (real + fake), config from env, ID helpers
- [x] `store/`: SQLite connection (WAL), transactions, SQL migrations runner
- [x] `tracing/`: JSONL trace recorder (state transitions, model calls, tools, latency, tokens, cost)
- [x] Import-direction test (`workflow/` must not import `agent/` or `api/`)

## Phase 2 — Mock world
- [x] `providers/` contract layer (`ToolEnv`, models, errors)
- [x] `mock_world.db` schema, seeded providers, 14 prompt-injection fixtures, keyed RNG
- [x] Scenario config (scripted outcomes, weights, price drift, slot conflicts)
- [x] `search_providers`, `get_provider`
- [x] `hold_slot` (10-min expiry, configurable price drift)
- [x] `book` (70/15/15 outcomes, timeout-after-success, `hold_expired`) + `get_booking_status`
- [x] `cancel` (first call 503), `reschedule` (`slot_conflict`), `list_bookings`
- [x] `MockControl.cancel_by_provider` (simulated provider event for Phase 6)

## Phase 3 — Workflow core
- [x] First `agent.db` migration; create + migrate `agent.db` at app startup
- [x] Requirements model: missing / ask-once fields, correction merge, date checks
- [x] State model + explicit transitions (tables per kind, history, generated diagrams)
- [x] Filtering + deterministic ranking of search results
- [x] Approval gate: approval bound to hash(action, provider, slot, price); invalidated on change
- [x] `GuardedToolEnv`: traced provider calls + per-run limits
- [x] `propose_booking`: hold + approval + transition in one atomic unit; shows a changed quote
- [x] Booking executor: persisted idempotency key, timeout → `get_booking_status`, no duplicate bookings
- [x] Reconciler: startup pass + 30 s background loop; resolves `unverified` / crashed bookings
      (completes the approved action with the same hold + key); retries `cancel_pending` until
      min(15 min after approval, 1 h before the visit), then `cancel_failed`
- [x] Cancel / reschedule with bounded retries
- [x] Hard limits: model turns, tool calls, wall-clock; every exit has a user-facing outcome

## Phase 4 — Memory & context
- [x] `memories`: closed typed keys, explicit (verbatim-quote provenance) vs inferred (pending until
      confirmed); inspect / correct / forget (value wiped, tombstone blocks re-inference)
- [x] `user_profile`: structured PII collected when first needed; never sent to the model
- [x] `messages` table (conversation history)
- [x] Token estimate + relevant-memory selection within budget
- [x] History scrubbing: forgotten quotes and saved PII leave stored messages
- [x] `ContextBuilder` + per-turn `context_sent` log; tests prove forgotten / pending / PII never sent
- [x] Inference rules from booking history (pending only)
- [x] Seed ≥50 memories + long prior conversation (deterministic generator, `make seed-data`)

## Phase 5 — Agent
- [x] Model factory: ERRAND_MODEL / ERRAND_REVIEW_MODEL, keys passed explicitly, clear errors
- [x] Agent tools = workflow commands only (no book / cancel / approve tools), FunctionModel-tested
- [x] Quarantined review reader: no tools, fixed-list fields only (no free text)
- [x] Run loop via `agent.iter`: turn budget, `model_call` traces with tokens and cost
- [ ] Message handler: per-run reconcile, typed yes/no in code (no LLM), context → history,
      persistence (our `messages` table, not PydanticAI's history), outcome
- [ ] `make chat` CLI for live testing; live smoke test (skipped without a key)

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
- [ ] Large mock world for scale tests (`make seed-large`, `ERRAND_MOCK_WORLD=large`): deterministic
      generator (Faker `en_IN` + templates, fixed seed), long booking histories, hundreds of
      reviews per provider from a curated, committed snippet library; attack fixtures kept.
      The hand-written 30-provider world stays the default for evals and the demo.

## Phase 9 — Deliverables
- [ ] README (setup, run, test, eval)
- [ ] Design note (≤1 page) + annotated timeout-after-success trace excerpt
- [ ] BUGS.md finalised (3 real bugs)
- [ ] Raw end-to-end trace in `docs/traces/`
- [ ] Demo recording (3–5 min, chat + trace side by side)
- [ ] AI coding transcripts in `transcripts/`
