# Architecture

How the Errand Agent is put together: one definition of each main component (what it
is, why it exists, how it works). Cross-cutting concepts live here; implementation
detail lives in each module's docstring, linked below. **Updated in the same commit as
the component it describes.**

## Core principle: the LLM proposes, the workflow decides

The model (`agent/`) understands requests and proposes actions. Deterministic code
(`workflow/`) decides whether an action runs: it checks approvals, holds, limits and
idempotency, and is the only code that triggers side effects (`book`, `cancel`,
`reschedule`). Approval is therefore enforced by application code, never inferred from
model output, and prompt injection in tool results cannot grant permission.

## Layers and import rules

An import is the only way one module can call another, so the dependency direction *is*
the enforcement mechanism. Each layer may import only the layers listed:

| Layer | May import | Status |
|---|---|---|
| `core/` | nothing internal | built |
| `store/`, `tracing/` | `core` | built |
| `mocks/` | `core`, `store` (the "external world" knows nothing of the agent) | Phase 2 |
| `memory/` | `core`, `store`, `tracing` | Phase 4 |
| `workflow/` | `core`, `store`, `tracing`, `memory`, `mocks` (interface only, narrowed in Phase 3) | Phase 3 |
| `proactive/` | `core`, `store`, `tracing`, `memory`, `mocks`, `workflow` | Phase 6 |
| `agent/` | `core`, `store`, `tracing`, `memory`, `mocks`, `workflow` | Phase 5 |
| `api/` | everything | Phase 7 |

If every layer obeys its own list, indirect violations (e.g. `workflow → memory →
agent`) are impossible too. `proactive` and `agent` don't import each other: the monitor
only detects events and starts runs, which the orchestrator handles like any other.

**Enforced by** `backend/tests/unit/test_layering.py`. It parses every module with `ast`
(so aliased, relative and function-local imports count, while docstrings, comments and
strings that merely mention a module don't), and fails if a layer imports outside its
list or if a new top-level folder has no rule yet. Not caught: `importlib` with a
computed string, which would be deliberate evasion.

## Components

### Clock: `core/clock.py`
- **What:** `Clock` protocol (`now`, `monotonic`, `sleep`); `SystemClock` and `FakeClock`.
- **Why:** Hold expiry (10 min), retry backoff, wall-clock limits and "this week" all
  depend on time. Tests must control it, and **the agent and the mocks must share one
  clock**, otherwise a test can expire a hold on one side while the other still
  treats it as valid.
- **How:** Nothing calls `datetime.now()` or `time.sleep()` directly; a clock is
  injected. All datetimes are UTC-aware. `FakeClock.advance()` moves `now()` and
  `monotonic()` together; `sleep()` advances instantly.

```
app:   clock = SystemClock()                    ─┬─▶ MockWorld(clock)
test:  clock = FakeClock("2026-11-05T09:00Z")    ├─▶ Workflow(clock)
       clock.advance(11 * 60)  # whole world      └─▶ Tracer(clock)
```

Against a real provider the clocks would differ, so the workflow treats the provider's
`expires_at` as authoritative and re-holds within a safety margin before it expires.

### Settings: `core/config.py`
- **What:** `Settings` (pydantic-settings), read once from `ERRAND_*` env vars / `.env`.
- **Why:** One validated place for configuration; bad values fail at startup.
- **How:** `data_dir` (default `var/`) is the root; DB paths and `trace_dir` derive from
  it unless set individually. `model` is `None` until chosen. Tests pass
  `_env_file=None` so a developer's `.env` never leaks in.

| Variable | Default |
|---|---|
| `ERRAND_DATA_DIR` | `var` (relative to `backend/`, where `make` runs) |
| `ERRAND_AGENT_DB_PATH` / `ERRAND_MOCK_DB_PATH` / `ERRAND_TRACE_DIR` | under `data_dir` |
| `ERRAND_MODEL` | unset (e.g. `anthropic:claude-sonnet-5-5`) |
| `ERRAND_MOCK_SEED` | `42` |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GOOGLE_API_KEY` | unset; standard names, no prefix |

Local values go in `backend/.env` (gitignored), copied from the committed
`backend/.env.example`. Blank entries (`KEY=`) count as unset. Provider keys are
`SecretStr` fields passed explicitly to the model provider: pydantic-settings reads
`.env` into `Settings` only and never exports it to `os.environ`, so relying on the SDKs
reading the environment would silently miss keys that live in `.env`.

### IDs: `core/ids.py`
- **What:** `IdGen` protocol; `RandomIdGen` (`run_<uuid4 hex>`) and `SequentialIdGen`
  (`run_0001`, one counter per prefix).
- **Why:** Stable IDs make traces and eval output identical run to run; the prefix says
  what kind of thing an ID is.
- **How:** Prefixes are lowercase letters only. A fresh `SequentialIdGen` restarts at
  0001, so restart tests must reuse the same instance.

### Database: `store/db.py`
- **What:** `connect(path)` and `transaction(conn)`.
- **Why:** Booking correctness needs atomic, crash-durable writes, and the chat loop and
  proactive monitor write concurrently.
- **How:** `autocommit=True` (no implicit transactions), WAL, `foreign_keys=ON`,
  `busy_timeout=5000`, `synchronous=NORMAL` (survives process crashes).
  `transaction()` uses `BEGIN IMMEDIATE` so the write lock is taken at BEGIN, and rolls
  back only if SQLite hasn't already ([BUG-1](../BUGS.md)).

### Migrations: `store/migrate.py`
- **What:** `migrate(conn, directory, clock)` applies `NNNN_name.sql` files in order.
- **Why:** Reproducible schemas for `agent.db` and `mock_world.db`. Alembic would bring
  SQLAlchemy for features we don't use; revisit if that changes.
- **How:** Each file runs atomically with its `schema_migrations` row; a sha256 checksum
  rejects edits to applied files; files live in
  `store/migrations/{agent,mock_world}/`. Never edit an applied migration: add a new
  one, or delete `var/*.db` to rebuild locally.

### Tracing: `tracing/`
- **What:** `Tracer` / `RunTrace` ([recorder.py](../backend/src/errand_agent/tracing/recorder.py)),
  `sanitize` ([sanitize.py](../backend/src/errand_agent/tracing/sanitize.py)),
  `estimate_cost` ([cost.py](../backend/src/errand_agent/tracing/cost.py)).
- **Why:** A trace per run (transitions, model calls, tool calls with sanitized
  results, latency, tokens, cost); evals assert on these events, not on reply text.
- **How:** see the run lifecycle below. Payloads are sanitized automatically (secrets
  redacted by key name, long strings truncated). Unknown model prices give
  `cost_usd: null` and `cost_complete: false`, never a silent zero.
- **Cost table:** `PRICES` covers Anthropic, OpenAI and Google models, keyed by
  PydanticAI model string. Prices are effective-dated (e.g. Gemini 4 Argon doubles on
  2026-12-01), and `estimate_cost(..., on=clock.now().date())` picks the one in force,
  so announced changes need no code edit on the day. Only Anthropic and OpenAI SDKs
  are installed; Gemini is priced but not runnable yet.

## Run lifecycle

A **run** is one inbound event handled end to end: a user message, an approval click, or
a proactive event. A booking usually spans several runs:

| Run | Trigger | Outcome |
|---|---|---|
| `run_0001` | "plumber this week, mornings, under ₹1500" | `needs_info` (asks for area) |
| `run_0002` | "Indiranagar" | `needs_approval` (provider, slot, price shown) |
| `run_0003` | user approves | `booked` / `booking_unverified` |
| `run_0004` | proactive: provider cancelled | `needs_approval` (replacement offered) |

```
start_run()                  emit()/span() …                finish(outcome)
     │                             │                              │
     ▼                             ▼                              ▼
 [run_started] ──────────▶  [ open: events flow ] ─────────▶ [run_finished] ──▶ closed
   seq 0                      seq 1, 2, 3 …                   exactly once       emit() → error
```

**How a run ends:**

| # | Path | Recorded | Meaning |
|---|---|---|---|
| 1 | `finish("booked")` | `run_finished`, `outcome: booked` | Normal: the workflow knows the domain outcome |
| 2 | Exception escapes the `with` | `outcome: error` + error type/message; exception re-raised | Crashes still get an outcome; nothing is swallowed |
| 3 | `with` exits without `finish()` | `outcome: unspecified` | Always a bug; evals assert no run ends this way |
| 4 | Process dies mid-run | `run_started` but **no** `run_finished` | Orphaned run, detectable after restart |

**Invariants:** exactly one `run_finished` per run (lifecycle events can't be emitted by
hand); no events after it; payload keys can't overwrite the envelope; `span()` validates
before its body runs, so its emit can't mask the body's exception; a failing subscriber
is logged and skipped, never breaking the run.

**Event envelope:** `{ts, run_id, session_id, seq, type, ...payload}`, one JSON object
per line in `var/traces/<run_id>.jsonl`, written line by line so partial traces survive
crashes.

| `type` | Payload |
|---|---|
| `run_started` / `run_finished` | `trigger` / `outcome`, `totals` (calls, tokens, cost, `cost_complete`, `duration_ms`) |
| `state_transition` | `from`, `to`, `reason` |
| `model_call` | `model`, `input_tokens`, `output_tokens`, `latency_ms`, `cost_usd` |
| `context_sent` | memory IDs included, token count, budget |
| `tool_call` | `tool`, `args`, `result`, `status`, `latency_ms`, `error` |
| `approval` | requested / granted / rejected / invalidated, `approval_id`, fingerprint |
| `limit_hit`, `error` | which limit / error type and message |

## Runtime data

All under `backend/var/` (gitignored), set by `ERRAND_DATA_DIR`:

```
var/
├── agent.db          # our state: workflow, approvals, idempotency keys, memory (Phase 3+)
├── mock_world.db     # the simulated providers; survives agent restarts (Phase 2)
└── traces/<run_id>.jsonl
```

Two database files on purpose: the "restart mid-booking" scenario needs the mock world
to remember a booking that succeeded even when the agent's process restarts.
