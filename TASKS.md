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
- [x] Message handler: per-run reconcile, typed yes/no in code (no LLM), context → history,
      persistence (our `messages` table, not PydanticAI's history), outcome
- [x] `make chat` CLI for live testing (stdlib loop, code-drawn card, per-turn trace footer)
- [x] Manual live testing: three `make chat` sessions, which found BUG-2-5, 7, 8; the
      automated smoke test moved to Phase 8, next to the evals
- [x] Bugs from live testing fixed (BUGS.md 2-9); context design D1-D3 in docs/DECISIONS.md;
      leaner fixed context (`list_memories`, tool schemas)

## Phase 6 — API & Web UI (design: docs/DECISIONS.md, D4)
- [x] Streaming in the run loop: text deltas and progress lines, tested offline
- [x] Sessions + messages endpoints (`POST` answers with an SSE stream), per-session lock
- [x] Approval decision endpoint: the button path (`handler.decide`), same as typed yes/no
- [x] Memory endpoints (inspect, confirm / reject suggestions, correct, forget) + bookings
- [x] `GET /events` push stream + past runs for the trace pane
- [x] Demo endpoints (`ERRAND_DEMO_MODE`): timeout after success, slot taken, provider
      cancels, price change at hold
- [x] Frontend: Vitest, API client, layout shell (chat + tabbed side panel)
- [x] Chat: streamed replies, progress lines, Stop; ApprovalCard / SuggestionsCard /
      BookingCard
- [ ] TracePane (runs, one-line events, expand to JSON)
- [ ] Memory and Bookings tabs
- [ ] Demo tab; end-to-end run with `make dev`

## Phase 7 — Proactive follow-up
- [ ] Simulated provider-cancellation event + monitor
- [ ] Notify → replacement search within preferences → fresh approval → rebook

## Phase 8 — Evals
- [ ] Live smoke test (`make smoke`, skipped without a key, not part of `make test`): a real
      conversation reaches an approval card and nothing is booked without "yes"
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
- [ ] AI coding transcripts: `scripts/export_transcript.py` turns the raw session JSONL(s)
      (kept outside git) into `docs/transcript.md`: full user/assistant turns, one line per
      tool call, assignment PDF text omitted, secrets/PII redacted (fails on key-shaped text)
