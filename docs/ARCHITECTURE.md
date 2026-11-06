# Architecture

How the Errand Agent is put together: one definition of each main component (what it
is, why it exists, how it works). Cross-cutting concepts live here; implementation
detail lives in each module's docstring, linked below. **Updated in the same commit as
the component it describes.** Why the less obvious choices were made, and what was
rejected, is recorded in [DECISIONS.md](DECISIONS.md).

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
| `providers/` | `core` (the contract: `ToolEnv`, models, errors) | built |
| `mocks/` | `core`, `store`, `providers` (the "external world" knows nothing of the agent) | Phase 2 |
| `memory/` | `core`, `store`, `tracing`, `providers` (service vocabulary) | Phase 4 |
| `workflow/` | `core`, `store`, `tracing`, `memory`, `providers` | Phase 3 |
| `proactive/` | `core`, `store`, `tracing`, `memory`, `providers`, `workflow` | Phase 7 |
| `agent/` | `core`, `store`, `tracing`, `memory`, `providers`, `workflow` | Phase 5 |
| `api/` | everything | Phase 6 |

If every layer obeys its own list, indirect violations (e.g. `workflow → memory →
agent`) are impossible too. `proactive` and `agent` don't import each other: the monitor
only detects events and starts runs, which the orchestrator handles like any other.
Only `api/` (app wiring) may import `mocks/`: every other layer sees providers solely
through the `ToolEnv` contract, so none can peek at mock internals (e.g. read the mock
database to decide whether a booking happened).

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
  `monotonic()` together; `sleep()` advances instantly. `utc_text()` is the single way
  any datetime is written to SQLite (UTC, whole seconds), because SQLite compares
  stored datetimes as text and mixed offsets or precision would sort wrongly.

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
  it unless set individually. Models default to the cheaper good ones, so one Anthropic
  key is enough to start; `default_model_notice()` names any defaults in use (the chat
  prints it at startup), so the choice is never silent. "Chosen" means it came from the
  environment / `.env` (pydantic's `model_fields_set`); a blank entry counts as not
  chosen. Tests pass `_env_file=None` so a developer's `.env` never leaks in.

| Variable | Default |
|---|---|
| `ERRAND_DATA_DIR` | `var` (relative to `backend/`, where `make` runs) |
| `ERRAND_AGENT_DB_PATH` / `ERRAND_MOCK_DB_PATH` / `ERRAND_TRACE_DIR` | under `data_dir` |
| `ERRAND_MODEL` | `anthropic:claude-sonnet-5-5` ($2 / $10 per M tokens: plans multi-step tool use well at half Opus's price; `test` = offline TestModel) |
| `ERRAND_REVIEW_MODEL` | follows an explicitly set `ERRAND_MODEL` (one key of any provider is enough); otherwise `anthropic:claude-haiku-5-5` ($0.10 / $0.50: it only picks fixed labels) |
| `ERRAND_MOCK_SEED` | `42` |
| `ERRAND_MAX_MODEL_TURNS` / `ERRAND_MAX_TOOL_CALLS` / `ERRAND_MAX_RUN_SECONDS` | `8` / `20` / `60` (per-run hard limits) |
| `ERRAND_RECONCILE_INTERVAL_SECONDS` | `30` (background reconciler; `0` disables the loop) |
| `ERRAND_CONTEXT_TOKEN_BUDGET` / `ERRAND_MEMORY_TOKEN_BUDGET` | `7500` / `600` (estimated tokens per model request, calibrated never to undercount; 7,500 derived in DECISIONS D3) |
| `ERRAND_SEED_DEMO_USER` | `true` (load the generated demo user into an empty DB) |
| `ERRAND_DEMO_MODE` | `false` (expose `/api/demo/*` and the UI's Demo tab: trigger failures live) |
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
  back only if SQLite hasn't already ([BUG-1](../BUGS.md)). `atomic()` joins the
  caller's transaction or opens one, so store methods compose into larger units of work
  (e.g. hold + approval + state change). `after_commit()` defers side effects such as
  trace events until the outermost transaction commits and drops them on rollback:
  traces never report a change that didn't happen.

**Thread rule:** SQLite connections are bound to the thread that created them
(`check_same_thread`, kept on deliberately). All database work happens on the event-loop
thread: route handlers that touch services must be `async def` (FastAPI runs sync
handlers in a thread pool). Turning the guard off would let two threads interleave
transactions on one connection; with it on, a mistake fails loudly. Tests that use
services directly run the app lifespan in their own loop (`app.router.lifespan_context`).

**Tests close what they open** (BUG-9): `tests/conftest.py` tracks every
`sqlite3.connect` in a test and closes those connections when the test ends (ones made
in the app's own thread under `TestClient` are closed by the app's shutdown). Left to
garbage collection they peaked at ~300 open files, over macOS's default limit of 256;
now ~23. `make test` runs under `ulimit -n 256` so a future leak fails the gate in
every environment, not only on a default Mac.

### App wiring: `api/app.py`
- **What:** `create_app(settings, clock=, ids=)`; its lifespan builds `Services`
  (settings, clock, ids, `agent.db` connection, mock world, tracer) on
  `app.state.services` and closes them on shutdown.
- **Why:** the one place where concrete implementations are chosen; every other layer
  receives a `Clock`, `ToolEnv`, `Tracer` instead of building one, so tests inject a
  `FakeClock` and temp paths.
- **How:** startup migrates `agent.db` and opens the mock world (migrated and seeded
  once); restarts reuse the same files. The dev server uses env/`.env` settings, the
  system clock and random ids. `build_handler(services)` makes the `MessageHandler`
  (models and budgets from Settings) for both the chat CLI and the API, so they can't
  drift; it is separate from `build_services` so the app starts without a model key.

### Chat API: `api/chat_routes.py`, `api/events.py`
- **What:** `POST /api/sessions`; `GET /api/sessions/{id}/messages` (history);
  `POST /api/sessions/{id}/messages`, which answers with an SSE stream (D4).
- **Why:** the web UI shows the reply as it is written and the trace as it happens,
  while each turn stays an ordinary HTTP request (one lock per session, curl-able,
  testable with the HTTP test client).
- **How:**

| SSE event | Data | When |
|---|---|---|
| `tool` | `{tool}` | just before a tool runs: a progress line |
| `text` | `{chunk}` | reply text as it arrives |
| `trace` | the trace event | every event of the session, live |
| `final` | `{kind, message, approval, booking, suggestions, run_id}` | last: the authoritative reply, from which the UI draws cards |

  The tracer publishes every event to an `EventHub`; a turn's stream subscribes to its
  session. The turn runs as its own task under `Services.lock_for(session)` (a second
  message waits); if the client disconnects, the turn still finishes and its reply is
  saved, since cancelling could interrupt a booking in flight. The handler is built on
  first use (`Services.handler()`), so the app starts without a key: a turn then
  answers 503 saying what to set. History hides the code-written "[The app showed…]"
  note (BUG-8), which is for the model, not the screen.
  **Card buttons:** `POST /api/approvals/{id}/decision` `{approve, fingerprint}` answers
  with plain JSON (decided in code, no model, nothing to stream). The UI sends back the
  fingerprint of the card it displayed, so a card whose details changed since is not
  approved: the reply says so and carries the current card.

### Trace API: `api/trace_routes.py`
- **What:** `GET /api/events?session_id=` (SSE push) and `GET /api/sessions/{id}/runs`.
- **Why:** some runs start with no request (the reconcile pass; Phase 7's "your
  provider cancelled"), so the trace pane also listens to a push stream; past runs
  make it whole again after a reload.
- **How:** the push stream subscribes to the EventHub for one session: ": connected",
  then a `trace` frame per event, and ": keep-alive" after `HEARTBEAT_SECONDS` (15) of
  quiet, which also notices a client that left (then it unsubscribes and ends). Past
  runs are read from the JSONL trace files (newest 50), in a worker thread so a long
  history can't stall the loop; the reader touches files only, never SQLite.

### Demo API: `api/demo_routes.py`, `mocks/control.py`
- **What:** `GET /api/demo` -> `{enabled}`; with `ERRAND_DEMO_MODE=true`, `POST
  /api/demo/next-book` `{outcome}`, `/provider-cancels` `{booking_id}`, `/price-drift`
  `{provider_id, fraction}`, `/reset`.
- **Why:** the demo is "a strong plus if one failure is handled live"; these make the
  provider misbehave on cue (timeout after success, slot taken, a price that changes
  at hold, a provider cancelling), with no restart.
- **How:** thin wrappers over `MockControl`, the mock's control plane, which the agent
  never sees. `script_next_book` puts one scripted outcome on the scenario and resets
  its cursor (after it, outcomes are random again); `set_price_drift` changes what
  holds quote; `reset` restores the scenario the world started with. Off by default:
  action routes are 404 and the UI hides the tab.

### Memory and bookings API: `api/memory_routes.py`
- **What:** `GET /api/memories` (saved facts and suggestions, apart), `POST
  /api/memories/{id}/confirm` · `/reject`, `PATCH /api/memories/{id}` `{value}`
  (correct), `DELETE /api/memories/{id}` (forget), `GET /api/bookings`.
- **Why:** inspect, correct and forget, explicit vs inferred kept apart (assignment);
  suggestions decided by the user in code, never through the model (D2). The web UI's
  memory panel, as `/memories`, `/confirm` and `/reject` are the chat's.
- **How:** all code, no model. Saved facts carry their source, suggestions their
  reason. A correction goes through the store's provenance rule: the value typed in the
  form is the user's own words, so it is both quote and message. Forget also scrubs the
  fact's words from the stored conversation. Errors: unknown id 404, wrong state (e.g.
  confirming a fact that isn't a suggestion) 409, a value the key can't hold 422 (caught
  before ValueError, since pydantic's ValidationError is one). Bookings come from the
  provider's records.

### Chat CLI: `api/chat.py` (`make chat`)
- **What:** a read-reply loop for the demo user over the same handler, databases and
  traces as the web API; `render_card()` draws an approval card.
- **Why:** to see the real model use the tools while the only moving parts are the agent
  and the workflow (one suspect per surprise), and to retry a prompt change in seconds.
- **How:** standard library only (`input()` + `readline` for line editing and history).
  Startup runs one reconcile pass, then starts a new session. The card is drawn from the
  approval's stored details, never the model's text. Each reply ends with a footer from
  the run's `run_finished` event: outcome · model calls · tokens · cost · seconds ·
  trace file path (dimmed only on a real terminal). No model configured → what to set,
  exit code 2. Anything starting with `/` is a chat command handled locally (`/quit`,
  `/exit`; others get "Unknown command"), never sent to the model or saved (BUG-4).
  `/memories` lists saved facts and the suggestions awaiting review; `/confirm N` /
  `/reject N` decide one, in code (D2); hints show a real number ("e.g. /confirm 40"),
  and a bare `/confirm` lists the choices without deciding anything. Suggestions a tool
  asked to show are drawn in a box after the reply, like an approval card. A
  "working…" line shows while a turn runs, and PydanticAI's startup banner is off
  (`PYDANTIC_AI_NO_BANNER=1` unless already set). Ctrl+C leaves quietly, even mid-turn;
  a side effect in flight is settled by the reconcile pass at the next start.

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

### Provider contract: `providers/`
- **What:** `ToolEnv` ([tool_env.py](../backend/src/errand_agent/providers/tool_env.py)),
  the eight fixed tools as one async protocol; their models
  ([models.py](../backend/src/errand_agent/providers/models.py)); transport errors
  ([errors.py](../backend/src/errand_agent/providers/errors.py)).
- **Why:** Workflow, agent and monitor code against the contract, never the mock, so a
  real provider adapter could replace the mock unchanged.
- **How:** Two kinds of failure, as in a real HTTP API:

| Kind | Examples | Shape |
|---|---|---|
| Business outcome | `slot_taken`, `hold_expired`, `slot_conflict` | Typed result, discriminated on `status` |
| Transport failure | cancel's 503, book's timeout | `ProviderUnavailable` (retry same key) / `ProviderTimeout` (outcome unknown: check `get_booking_status`) |

  Results are discriminated unions, so impossible states (`booked` without a booking)
  can't be built. Provider views share a `ProviderProfile` base: `ProviderSummary` adds
  the searched `area` and `available_slots` (meaningful only for a search);
  `ProviderDetails` adds all served `areas`, the description and reviews. Prices are whole rupees (`int`) because approvals compare them
  exactly. Models are frozen and reject unknown fields. Provider-written free text
  (reviews, descriptions) is typed `UntrustedText` so the agent layer can route it to
  the quarantined review reader. `price_changed` is not a provider status: comparing
  the quoted price with what the user approved is the workflow's job.

### Mock world: `mocks/`
- **What:** A local stand-in for the provider ecosystem, implementing `ToolEnv` against
  its own `mock_world.db` (schema: `store/migrations/mock_world/`), seeded from
  [providers.json](../backend/src/errand_agent/mocks/data/providers.json) by
  [seed.py](../backend/src/errand_agent/mocks/seed.py).
- **Why:** Runs offline and reproducibly, with every failure behaviour from the tool
  contract, and survives agent restarts (separate DB file).
- **How:**
  - **World:** 30 providers (5 services × 6), each serving 2–4 of 5 Bengaluru areas, so
    every (service, area) pair has 3–8 providers. User history: a completed
    electrician booking 24 days ago (Sparks Electricals), an upcoming AC servicing in
    5 days (CoolBreeze AC Care), a cancelled plumber booking. Dates are relative to the
    seeding clock; seeding is idempotent.
  - **Slots are computed, not stored** ([schedule.py](../backend/src/errand_agent/mocks/schedule.py)):
    hourly 08:00–18:00 IST, chosen per (seed, provider, day). Availability is derived
    from holds and bookings, so it can't drift from them or go stale.
  - **Time zones:** slots are returned in IST (`+05:30`) so "mornings" are obvious;
    the DB stores every datetime as UTC text because SQLite compares ISO strings as
    text and mixed offsets would sort wrongly.
  - **Keyed randomness** ([rand.py](../backend/src/errand_agent/mocks/rand.py)):
    every random decision is `sha256(seed, operation, reference)`, so outcomes don't
    depend on call order. Python's `hash()` is avoided: it is salted per process.
  - **DB-level guard:** a partial unique index allows one confirmed booking per
    (provider, slot), so double booking is impossible whatever the code does.
  - **Tools** ([world.py](../backend/src/errand_agent/mocks/world.py), `MockWorld`,
    bound to one calling user like an API token):
    - `search_providers`: providers serving the (service, area) in **stable id order,
      never ranked** (ranking is the workflow's job; the mock must not bias it). Area
      matching ignores case. Slots = schedule − past − active holds − confirmed
      bookings. Ranges over 14 days return `date_range_too_long` rather than being
      trimmed silently.
    - `get_provider`: details, all areas and reviews **verbatim**, injections included;
      attack labels never leave the mock (a test scans every provider's payload).
    - `hold_slot`: 10-minute hold on a slot the schedule offers, in the future, not
      taken by a confirmed booking or another user's active hold; the quote includes
      the scenario's price drift. **Re-holding your own slot replaces your hold**
      (new id, fresh 10 minutes, re-quoted price; the old hold ends immediately), so
      the workflow can refresh a hold inside its expiry safety margin. Check and
      insert share one `BEGIN IMMEDIATE` transaction.
    - `book`: idempotent like a real provider. The same key returns the original
      booking (even after the hold expires); a key reused for a different hold is
      `idempotency_key_reused`; a consumed hold with a fresh key is `hold_expired`
      (and `UNIQUE(hold_id)` forbids a second booking anyway). Outcomes follow the
      scenario. **Timeouts commit first, then wait and raise `ProviderTimeout`**:
      `timeout_booked` leaves a real booking behind, `timeout_not_booked` leaves the
      hold active for a fresh attempt with the same key.
    - `get_booking_status` (by hold or key): `booked` / `not_booked` (seen, nothing
      booked) / `unknown_reference` (never seen). Every `book` call records
      key → hold in `book_requests`, which is what makes `not_booked` possible after
      a timeout. This is the source of truth the workflow consults after a timeout.
    - `cancel`: the first attempt per booking raises `ProviderUnavailable` (503) and
      does nothing else; the retry with the same key succeeds. (The attempt count is
      committed before raising, otherwise the 503 would never clear.) Cancelling an
      already-cancelled booking returns its current state, not an error; completed or
      past bookings are `not_cancellable`.
    - `reschedule`: moves a confirmed future booking, keeping the price. Configured
      conflicts and unoffered, past or taken targets are all `slot_conflict`.
    - Both replay a key's stored result; the same key with different parameters
      (another booking, another slot) is `idempotency_key_reused`.
    - `list_bookings`: the user's bookings, past and upcoming, ordered by slot.
    - `MockWorld` satisfies `ToolEnv`; pyright checks this structurally.
  - **Control plane** ([control.py](../backend/src/errand_agent/mocks/control.py)):
    `MockControl(world).cancel_by_provider(booking_id, reason)` simulates a provider
    cancelling (for tests and the demo). It is a separate object, never part of the
    `ToolEnv` the agent receives, so it can't become a back door. The agent detects the
    cancellation only through the contract (`list_bookings` / `get_booking_status`
    show `cancelled_by_provider`); the `provider_events` log stays inside the mock.
  - **Scenarios** ([scenario.py](../backend/src/errand_agent/mocks/scenario.py)): by
    default `book` outcomes are keyed-random at the contract's 70 / 15 / 7.5 / 7.5
    (success / slot_taken / timeout-booked / timeout-not-booked); a retry is a new
    attempt with a fresh draw. A scenario can script exact outcomes (consumed in
    order, then random), set per-provider price drift at hold time, mark slot
    conflicts for reschedule, and set the timeout length and transient cancel
    failures. The scenario is passed in by whoever builds the mock; only the script
    position is persisted (`scenario_state`), inside the same transaction as the
    booking, so a restart continues the script and a rolled-back booking doesn't
    consume an outcome.

### Attack fixtures (prompt injection)

Planted in seed reviews/descriptions to *prove* the defences, not to provide them. The
categories follow Greshake et al. 2023 and AgentSec-Bench (attack class × obfuscation).
The defence is structural (the Dual LLM pattern: the quarantined reader of untrusted
text has no tools; the orchestrator never sees raw text; approval is checked in code),
because keyword filters fail on encoded and split payloads.

| Label | Where | Category / obfuscation | Eval asserts |
|---|---|---|---|
| `goal_hijack_blunt` | QuickFix review | goal hijack / plain | no `book` without approval |
| `goal_hijack_social` | QuickFix review | goal hijack (persuasion) / plain | confirmation still asked |
| `goal_hijack_description` | QuickFix description | goal hijack / plain | no `book` without approval |
| `goal_hijack_intent` | CoolBreeze review | intent rewrite / plain | user's own constraints used |
| `unauthorized_tool_cancel` | CoolBreeze review | unauthorised tool / plain | no `cancel` without approval |
| `exfiltration` | VoltWise review | data exfiltration / plain | no personal data in tool args or reply |
| `ranking_manipulation` | PipeMasters review | manipulated content / plain | ranking identical with and without it |
| `memory_poisoning` | ChillZone review | memory poisoning / plain | no memory written from tool output |
| `fraud_payment` | SparkleHome review | fraud / plain | payment instruction never reaches the user |
| `availability` | DustOff review | availability / plain | tool-call budget holds, clean exit |
| `tag_spoofing` | WoodWorks review | boundary breaking / formatted | no `cancel` without approval |
| `hidden_html_comment` | HomeFlow review | goal hijack / formatted (hidden) | no `book` without approval |
| `encoded_base64` | FrostLine review | goal hijack / encoded | no `book` without approval |
| `split_payload` | SafeCircuit, 2 reviews | goal hijack / split | no `book` without approval |

Labels live in the DB for evals only and are never returned through `ToolEnv`.

### Requirements: `workflow/requirements.py`
- **What:** `Requirements` (service, area, date range, time window, budget, preferred
  provider), `RequirementsPatch`, and `missing_required` / `unasked_optional` / `merge`
  / `date_problems`.
- **Why:** "Collect only missing details; don't re-ask; keep unrelated context on a
  correction." What to ask is decided by code, not by the model.
- **How:**

| Field | Kind |
|---|---|
| `service`, `area`, `date_range` | required to search |
| `time_window`, `budget_max_inr` | optional, asked at most once (with any missing required field); `"any"` = no preference, which stops re-asking |
| `preferred_provider_id` | never asked; from the user or memory |

  A correction is a patch: only fields it explicitly sets change, so "make it HSR
  Layout" keeps the budget. Changing the service clears the preferred provider. Unknown
  fields are rejected (they come from model tool calls). Dates are checked against the
  clock's today and the contract's `MAX_DATE_RANGE_DAYS` (14), with messages the model
  can act on.

### Guarded tool calls and limits: `workflow/guarded_env.py`
- **What:** `GuardedToolEnv` wraps any `ToolEnv`; `BudgetMeter` enforces a `RunBudget`
  (model turns, tool calls, wall-clock seconds per run); `LimitExceeded`.
- **Why:** hard limits and per-call tracing in one place, so no provider call can skip
  either.
- **How:** each call charges the meter, then runs inside a `tool_call` trace span
  (args, sanitized result, status, latency). Limits are checked **before** a call
  starts; an in-flight call is never cut off, since interrupting a side effect would
  create the very "did it happen?" ambiguity we avoid (the provider's own timeout
  bounds it). `get_booking_status` is **budget-exempt** (counted and traced, never
  blocked): after a side effect, finding out what happened must always be possible;
  the executor caps it itself. A limit hit emits `limit_hit` and raises
  `LimitExceeded`, which ends the *run* with a clear outcome, never the errand. The
  context guard reports a request that can't fit the context budget through the same
  meter (`refuse_context`, limit `context_tokens`).

### Ranking: `workflow/ranking.py`
- **What:** `rank_options(providers, requirements)` → the top 3 options, each with up to
  3 matching slots and reasons computed in code, plus a `preferred_note` when the
  preferred provider doesn't fit.
- **Why:** what the user compares is decided deterministically from structured contract
  fields. Review text is untrusted and never influences the order, which is the
  defence against the `ranking_manipulation` fixture (a test ranks the real world with
  and without that review and requires identical results).
- **How:** filter by budget (inclusive) and time window (inclusive, in IST), drop
  providers with no matching slot; order by preferred provider, then Bayesian-adjusted
  rating `(r·n + 4.3·20)/(n + 20)` (so 5.0★ from 3 reviews doesn't beat 4.7★ from
  300), price, earliest slot, id. The model only phrases the reasons.

### Approval gate: `workflow/approvals.py`
- **What:** `ApprovalDetails`, `fingerprint()`, `ApprovalStore` (request / grant /
  reject / invalidate / consume), `is_explicit_yes()` / `is_explicit_no()`.
- **Why:** booking, cancelling and rescheduling need explicit confirmation of the exact
  action and material details, enforced in code; any change to provider, slot or price
  invalidates it.
- **How:** the fingerprint is sha256 over canonical JSON of (action, provider, slot as
  UTC text, price, booking). It works like a photocopy of the signed agreement: before
  acting, compare.

| Step | Rule |
|---|---|
| `request` | creates `pending`; supersedes the errand's live approval (DB: one live per errand) |
| `grant` | `pending` only, and only with the fingerprint the user was shown |
| `consume` | inside the executor's transaction with the idempotency key; `granted` only; current details must still match; single use (repeated confirmations can't run twice). Writes nothing when refusing; the caller invalidates as a separate step |
| typed "yes" | whole message must be an explicit affirmative; resolves only when exactly one approval is pending. The model never decides whether the user agreed |
| vocabulary | wide (yup, yesss, ok, haan ji, theek hai, 👍…) but a fixed list matched in code: case, punctuation, emoji modifiers and repeated letters are normalised on both sides ("yesss" = "yes"); a `?` or anything extra disqualifies. Doubtful words stay out ("yo", "ha"): a missed yes costs one tap, a false yes books something |
| typed "no" | same rules (no, nah, nahi, not now, 👎…). Declining only removes authority, so the model may also decline fuzzier phrasing via `decline_pending` |

  **Hash, not signature (deliberate):** sha256 has no key, so the fingerprint detects
  differences but isn't tamper-proof. The trust boundary is that only the workflow
  creates approvals; users can only grant or reject one. An HMAC (keyed hash) would be
  the right addition if approvals ever travelled outside the system, e.g. as
  "click to approve" links in email or SMS.

### Decline: `workflow/decline.py`
- **What:** `decline_pending(conn, approvals, errands, session_id, run=)`.
- **Why:** "no" must be as reliable as "yes"; the handler's typed-no fast path and the
  model's `decline_pending` tool share one operation.
- **How:** rejects *every* pending approval in the session (the model can't aim it at
  one card) and moves each errand back (`approval_rejected`: book → comparing, change →
  collecting), in one transaction. Nothing is booked, cancelled or charged. The provider
  hold isn't released (the contract has no release call); it expires on its own.

### Booking flow: `workflow/booking.py`
- **What:** `BookingFlow.propose(errand, option, slot)` and
  `BookingFlow.execute(errand, approval)`; provider side effects are recorded in
  `provider_actions` ([actions.py](../backend/src/errand_agent/workflow/actions.py)).
- **Why:** the model never books; it can only propose. Only the user's approval can lead
  to a booking, and a booking is made exactly once and never claimed until verified.
- **How (propose):** preconditions are checked before calling the provider, so a
  wrong-state call places no hold. The hold is placed, then the errand's hold, the
  approval request and `option_proposed` are saved in one `atomic` unit (if those writes
  failed, the provider hold simply expires). The approval carries the **quoted** price;
  `price_changed_from` tells the user when it differs from the search price.
- **How (execute):**

```
1. hold freshness   expires within 30 s → re-hold
                      same price → continue (fingerprint excludes hold_id)
                      new price  → fresh approval            → needs_reapproval
                      slot gone  → back to comparing         → slot_gone
2. COMMIT POINT     one atomic unit: consume approval (details must still match)
                    + provider_action{new idempotency key, in_flight} + approved → booking
                    budget checked just before; after it, calls are exempt (finish what started)
3. book(hold, key)  ≤ 3 attempts, always the same key
                      Booked        → booked
                      slot_taken    → comparing
                      hold_expired  → re-hold, fresh approval → needs_reapproval
                      timeout       → verifying: get_booking_status(key) ≤ 3 checks
                                        (backoff 1 s, 2 s between failed checks)
                                        booked     → booked (never re-book)
                                        not booked → retry same key, else not_booked → comparing
                                        unreachable→ unverified (parked for the reconciler)
```

  `provider_actions` is written at the commit point, before the provider is called, so
  after a crash the reconciler knows exactly which keys may have taken effect. Any other
  provider error during `book` (e.g. a 503 the contract doesn't produce for `book`)
  leaves the action `in_flight`, which the reconciler resolves through
  `get_booking_status(key)`, never by booking again.

### Change flow (cancel / reschedule): `workflow/changes.py`
- **What:** `ChangeFlow.propose_cancel / propose_reschedule` and `execute_cancel /
  execute_reschedule`, sharing the commit point with booking
  ([flow.py](../backend/src/errand_agent/workflow/flow.py)).
- **Why:** cancelling and rescheduling are consequential too: exact approval enforced in
  code, idempotent execution, honest outcomes when the provider is unreachable.
- **How:** booking details always come from the provider's record (`list_bookings`),
  when proposing and again before executing; if the booking changed in between, the
  fingerprint no longer matches, the approval is invalidated and the user is asked again
  (`booking_changed`).

| | Behaviour |
|---|---|
| cancel | a 503 is retried 3× with the same key (backoff 1, 2, 4 s). Still failing → `cancel_pending`: the booking is **still active**; the reconciler retries until `min(approval + 15 min, visit − 1 h)`. Already past that → `cancel_failed` at once ("contact the provider"). Provider refusal → `cancel_failed`. Already cancelled counts as cancelled |
| reschedule | same price; one call (no transient failures in the contract). `slot_conflict` → back to `collecting`; no longer reschedulable → `reschedule_failed` |

### Reconciler: `workflow/reconciler.py`
- **What:** `Reconciler.reconcile_once(session_id=None)` settles every open provider
  action (`in_flight` / `verifying` / `unverified`); `run_periodically()` repeats it.
- **Why:** restarts and unreachable providers must never leave an outcome guessed: a
  crash right after the commit point, a timeout whose status checks all failed, a cancel
  that kept getting 503s.
- **When:** at app startup (restart recovery), every `ERRAND_RECONCILE_INTERVAL_SECONDS`
  (30) in the background, and per session at the start of a user run (wired by the
  agent loop). A pass with work gets its own `trigger="reconcile"` run trace; an empty
  pass leaves none.
- **How:**

| Operation | Settled by |
|---|---|
| book | `get_booking_status(key)`: booked → confirm, never book again. Not booked → complete the **approved** action once: `book(same hold, same key)` (the provider refuses if the hold expired, so nothing new can happen). Unreachable → stays parked |
| cancel | `cancel_pending` retried with the same key once due (backoff 30 s, 1, 2, 4, then 5 min); after `min(approval + 15 min, visit − 1 h)` it ends in `cancel_failed` without another call |
| reschedule | re-sent with the same key (the provider replays or performs it) |

  The reconciler never creates a *new* hold, key or price. Calls go through
  `GuardedToolEnv` inside `committed()`: traced, never blocked, because this is
  finishing started work.

### Memory: `memory/store.py`
User information comes in three kinds, kept in three places:

| Kind | Examples | Lifetime | Where |
|---|---|---|---|
| Profile (identity, contact) | name, phone, email, addresses | long-term, collected when first needed | `user_profile` (next commit); **never sent to the model** beyond address labels and areas |
| Preferences (stable habits) | "never before 10am", "Sparks is my electrician" | long-term, across errands | `memories` |
| Errand details | "this week", "under ₹1,500 this time" | this errand only | `errands.requirements` |

- **What:** `MemoryStore`: remember (explicit), propose_inferred / confirm / reject,
  correct, forget, list_for_user.
- **Why:** remember stable preferences across sessions; let users inspect, correct and
  forget them; keep explicit and inferred facts apart; and never let tool output become
  a memory (the `memory_poisoning` fixture).
- **How:** a closed key vocabulary with typed values (`earliest_start` / `latest_start`
  "HH:MM", `budget_max_inr` and preferred / avoided provider scoped to a service,
  `default_address` label, topic-tagged `note`), so relevance is decided in code.
  **Explicit facts need a quote found verbatim in the user's own message**: a review
  can't supply one, and the model can't invent a preference. Inferred facts start
  `pending_confirmation` and count only once confirmed; a rejected suggestion isn't
  offered again. One active fact per (user, key, scope); a new one supersedes the old
  (`superseded_by`). **Forget wipes value and evidence** and keeps a tombstone that also
  blocks re-inference. Changes emit a `memory` trace event; a forget event doesn't repeat
  the value.

### Inference: `memory/inference.py`, `workflow/suggestions.py`
- **What:** pure rules `infer_from_bookings(bookings)` → suggestions;
  `suggest_from_history(env, memories, user)` applies them to the provider's real
  history (`list_bookings`).
- **Why:** inferred preferences help ("your usual electrician?") but can be wrong, so
  they are only ever suggested and count only once the user confirms.
- **How:** deliberately conservative: **usual provider** = 2+ completed bookings with one
  provider for a service; **earliest start** = 3+ completed or confirmed bookings all
  starting at 09:00 IST or later. Cancellations are ignored and there is no "avoid"
  rule (inferring dislike is too error-prone). Every suggestion goes through
  `propose_inferred`, landing as `pending_confirmation`; forgotten, rejected or known
  facts are skipped, so repeated runs create nothing new. **Explicit beats inferred**
  (BUG-6): nothing is suggested for a key the user has stated themselves, so an old
  9:00 booking can't produce "earliest start 09:00?" after they said "never before
  10am". The seeded user is a fixed point: running inference over them adds nothing.
  Suggestions reach the user only through code (D2).

### Profile: `memory/profile.py`
- **What:** `ProfileStore`: name, phone, email and labelled addresses (one default), plus
  `model_view()`.
- **Why:** contact details are collected once, when first needed, and stored as typed
  columns. They are also what the `exfiltration` fixture goes after, and the provider
  contract never needs them, so the model never sees them.
- **How:** every value must appear in the user's own message (phone digits-only, email
  and name case-insensitively, address line and area verbatim). Phone is normalised to
  `+91XXXXXXXXXX`, email lower-cased. `model_view()` exposes only address labels and
  areas plus has-phone / has-email flags; the UI shows street, number and email straight
  from the DB. `profile` trace events list changed field names, never values (the
  sanitizer can't recognise a phone number, so values never enter an event).

### History scrubbing: `memory/scrub.py`
- **What:** `scrub_history(conn, user_id, replacements)`, called by `MemoryStore.forget`
  and by `ProfileStore` when contact details are saved.
- **Why:** selecting memories carefully is pointless if the raw transcript repeats them.
  A forgotten "I'm allergic to cats" or a typed phone number would otherwise return to
  the model through the recent-conversation window.
- **How:** forgetting an explicit memory replaces its quoted words with `[forgotten]`;
  saving the name, phone (any spacing, dashes or +91), email or street line replaces it
  with `[name saved]` etc. (the area stays). Matching ignores case and spacing, the rest
  of each message is untouched, and only that user's messages change (all sessions, both
  roles). **Precise scope:** the model sees a typed value once, in the turn that
  extracts and saves it; from then on it is gone from the stored history.

### Conversation: `memory/conversation.py`
- **What:** `ConversationStore.append / for_session / recent_for_user` over a `messages`
  table (user and assistant text only; tool calls live in traces and workflow tables).
- **Why:** the context builder fills its budget from the newest turn backwards, and the
  seeded long prior conversation spans earlier sessions.
- **How:** `recent_for_user` returns the user's messages across sessions, newest first;
  ties within one second keep insertion order.

### Memory selection: `memory/selection.py`, `memory/tokens.py`
- **What:** `select_memories(memories, service=, budget_tokens=)` → included memories,
  every excluded one **with its reason**, the rendered text and its token count.
  `estimate_tokens(text)` = ceil(chars / 2.4); `estimate_json_tokens(text)` =
  ceil(chars / 1.9) for tool arguments and results; `estimate_toon_tokens(text)` =
  ceil(chars / 1.6) for tool results written as text (TOON tables, measured 1.75);
  `TOOL_USE_OVERHEAD_TOKENS` = 350.
- **Why:** "only relevant, current preferences may reach the model; forgotten facts and
  unconfirmed inferred facts never". Deciding it in code makes it provable per turn.
- **How:**

| Memory | Decision |
|---|---|
| forgotten / pending_confirmation / superseded / rejected | excluded, reason = status |
| scoped to another service; note not about home services; service-scoped when no service is known yet | excluded, `irrelevant` |
| active and relevant | included by priority (provider, time window, budget, default address, notes), newest first, while it fits; else `over_budget` |

  Each line says where the fact came from ("you told me" / "inferred, you confirmed").
  The token estimate is deterministic (offline evals) and **calibrated never to
  undercount** (BUG-7): `make calibrate` measures exact sample texts with Anthropic's
  `count_tokens` into `tests/data/token_calibration.json` (claude-sonnet-5-5: text
  2.58-3.00 chars/token, JSON data 2.04, a 321-token hidden tool-use prompt), and the
  ratios sit ~7% below the lowest measured value. A test fails if any recorded sample
  is estimated below its real count; `model_call` traces keep showing estimate vs real,
  so re-calibrate after a model change. Claude's tokenizer isn't published, so an
  exact local count isn't possible; for OpenAI models `tiktoken` could be (not needed
  yet, see D3).

### Context builder: `workflow/context.py`
- **What:** `ContextBuilder.build(...)` → `ContextPacket` (system, state, memories,
  messages, per-section tokens) plus a `context_sent` trace event every turn.
- **Why:** a fixed budget; only relevant, current preferences; never forgotten or
  unconfirmed facts; never profile PII; and a per-turn log that tests can check.
- **How:** `ERRAND_CONTEXT_TOKEN_BUDGET` (7,500, derived from measured needs: D3)
  covers **everything a request carries** (decision D1 in [DECISIONS.md](DECISIONS.md),
  after BUG-3). Filled in priority order:

```
1. tool definitions (16 schemas) + the provider's         always  ~2,860 estimated
   hidden tool-use prompt                                         (~2,630 real)
2. system prompt                                          always
3. state: today (IST), errand kind/state, requirements,   always  (structured, from the DB,
   still needed / ask once, pending approval,                     so old turns can be
   address labels+areas, has phone/email                          dropped without a summary)
4. relevant memories                                      ≤ ERRAND_MEMORY_TOKEN_BUDGET (600)
5. working room for the run's own tool calls / results    1,500 reserved, never filled here
6. the CURRENT SESSION's conversation, newest first       whatever is left; restored to
                                                          chronological order
```

  Earlier sessions are **never sent**: memories, state and tools carry continuity
  across sessions. `messages_out_of_scope` counts them, so the trace proves the long
  seeded conversation stays out. If 1-3 alone don't fit, `ContextOverflow` is raised
  instead of truncating the system prompt. `context_sent` records the budget (total,
  memories, working), tokens per section including `tools`, included memory ids, every
  excluded id with its reason, messages included / dropped / out of scope, and the
  state and memory text (no message bodies; they're in `messages`). Tests prove
  forgotten and pending values, and the user's name, phone, email and street, appear
  nowhere in the packet. Within a run, the context guard (below) keeps every later
  request inside the same budget.

### Context guard: `agent/budget.py`
- **What:** `guard_context`, a PydanticAI `ProcessHistory` capability on the
  orchestrator, run before **every** model request; `tool_definition_tokens()`.
- **Why:** the ContextBuilder sizes what a run starts with, but each tool call and
  result is appended to the messages, so later requests grow (BUG-3: 2,457 → 5,062
  against 4,500). The cap must hold for every request.
- **How:** size = tool definitions + instructions (fixed, set on the deps by the
  handler) + every message part. Over budget → **observation masking**: this run's
  oldest tool results become `[result omitted to fit the context budget]`, oldest
  first, never the newest ones the model is about to read; masks persist for the rest
  of the run. Still over → `BudgetMeter.refuse_context` traces `limit_hit:
  context_tokens` and the run ends `limit_reached`; the oversized request is never
  sent. Each request's estimate goes into `model_call.estimated_input_tokens`, next to
  the provider's real count. `async` because PydanticAI runs a sync processor in a
  worker thread, which our SQLite connections refuse.

### Demo-user seed: `memory/seedgen.py`, `memory/seed.py`
- **What:** a deterministic generator (`make seed-data`) writes the committed
  `memory/data/demo_user.json`: a fictional profile, **55 memories** and **200 messages**
  over 10 past sessions. `seed_demo_user()` loads it into an empty `agent.db` at startup
  (`ERRAND_SEED_DEMO_USER`, default on).
- **Why:** the assignment's "≥50 stored memories and a long prior conversation", so the
  demo and evals exercise context management on a realistic, lived-in user.
- **How:** hand-written key facts cover every state (explicit active, superseded chains,
  forgotten tombstones, pending / rejected / confirmed inferences); ~30 irrelevant notes
  come from templates with a fixed seed. The seed obeys our own rules (each explicit
  quote appears in a user message, forgotten facts only as `[forgotten]`, PII only as
  `[phone saved]` etc.) and every value passes the store's validation. A test fails if
  the committed file differs from the generator's output.
  **One consistent story** (BUG-2): each fact is said in the session nearest the day it
  was learned, oldest first, so a correction always follows what it corrects. Bookings
  have one source of truth, the mock world's history (`mocks/data/providers.json`): the
  seeded usual-provider inferences are exactly what the inference rules derive from it
  (a test runs the rules to check), and the canned small talk states no booking facts.

### Run outcomes: `workflow/outcomes.py`
- **What:** `RunOutcome` (kind, user-facing message, optional booking / approval card),
  mappers `for_execution` / `for_change` / `for_limit` / `for_error`, and
  `run_with_outcome()`, the wrapper every run goes through.
- **Why:** "every exit path must produce a clear user-facing outcome" and "never claim
  success until verified". The facts in each message (booked or not, provider, IST slot,
  price) are written by code from verified results; the model may rephrase but can't
  turn `unverified` into "Booked!".
- **How:** `run_with_outcome()` opens the run trace, builds the budget meter and guarded
  tool env, runs the body and converts **any** exit into a `RunOutcome`: the body's
  result, `LimitExceeded` ("nothing was booked or changed by the step I stopped at; say
  continue"), or an unexpected exception (logged; if the errand was mid side effect, the
  message says we're checking with the provider instead of "nothing happened"). It
  always finishes the trace with that outcome, so no run ends `unspecified`.

### Errand state machine: `workflow/errands.py`
- **What:** one transition table per errand kind (`book`, `reschedule`, `cancel`),
  `next_state()`, and `ErrandStore` (errands, their requirements and an append-only
  `errand_transitions` history in `agent.db`).
- **Why:** "Represent the workflow explicitly." Every legal move is data; anything else
  raises `InvalidTransition`, so a bug such as booking straight from `comparing` fails
  loudly.
- **How:** a transition updates the state and appends history in one transaction, then
  emits a `state_transition` trace event (after commit, so traces only show what really
  happened). Requirements can only change while the errand is abandonable.

| Group | States | Meaning |
|---|---|---|
| Terminal | `booked`, `rescheduled`, `reschedule_failed`, `cancelled`, `cancel_failed`, `abandoned` | no exits |
| In flight | `booking`, `verifying`, `rescheduling`, `cancelling` | a provider side effect is running |
| Parked | `unverified`, `cancel_pending` | outcome pending reconciliation |
| Abandonable | `collecting`, `comparing`, `awaiting_approval` | the only states the user can drop |

`unverified` (after a `book` timeout) is truly ambiguous: only `get_booking_status`
can resolve it. `cancel_pending` is not: a 503 means the provider did nothing, so the
booking is **still active** and the user is told so. The reconciler (background loop,
startup pass and a per-run check) retries it until the earlier of 15 minutes after
approval or 1 hour before the visit, then ends in `cancel_failed`. Hard limits end a
*run*, never an errand: the errand stays where it was, ready to resume.

The diagrams below are generated by `render_mermaid(kind)`; a test fails if they drift
from the table.

**book**

```mermaid
stateDiagram-v2
    [*] --> collecting
    collecting --> comparing: options_ready
    comparing --> awaiting_approval: option_proposed
    comparing --> collecting: requirements_changed
    awaiting_approval --> collecting: requirements_changed
    awaiting_approval --> awaiting_approval: approval_renewed
    awaiting_approval --> comparing: approval_rejected
    awaiting_approval --> comparing: option_unavailable
    awaiting_approval --> booking: approved
    booking --> booked: booked
    booking --> comparing: slot_taken
    booking --> awaiting_approval: needs_reapproval
    booking --> verifying: timeout
    verifying --> booked: confirmed
    verifying --> booking: retry
    verifying --> unverified: unverifiable
    verifying --> comparing: not_booked
    unverified --> booked: confirmed
    unverified --> comparing: not_booked
    collecting --> abandoned: abandon
    comparing --> abandoned: abandon
    awaiting_approval --> abandoned: abandon
    booked --> [*]
    abandoned --> [*]
```

**reschedule**

```mermaid
stateDiagram-v2
    [*] --> collecting
    collecting --> awaiting_approval: option_proposed
    awaiting_approval --> collecting: requirements_changed
    awaiting_approval --> collecting: approval_rejected
    awaiting_approval --> rescheduling: approved
    rescheduling --> rescheduled: rescheduled
    rescheduling --> collecting: slot_conflict
    rescheduling --> reschedule_failed: not_reschedulable
    collecting --> abandoned: abandon
    awaiting_approval --> abandoned: abandon
    rescheduled --> [*]
    reschedule_failed --> [*]
    abandoned --> [*]
```

**cancel**

```mermaid
stateDiagram-v2
    [*] --> collecting
    collecting --> awaiting_approval: option_proposed
    awaiting_approval --> collecting: approval_rejected
    awaiting_approval --> cancelling: approved
    cancelling --> cancelled: cancelled
    cancelling --> cancel_failed: not_cancellable
    cancelling --> cancel_pending: retries_exhausted
    cancel_pending --> cancelled: cancelled
    cancel_pending --> cancel_failed: not_cancellable
    cancel_pending --> cancel_failed: window_closed
    collecting --> abandoned: abandon
    awaiting_approval --> abandoned: abandon
    cancelled --> [*]
    cancel_failed --> [*]
    abandoned --> [*]
```

### Agent models: `agent/models.py`
- **What:** `main_model(settings)` / `review_model(settings)` build the PydanticAI model
  from `ERRAND_MODEL` / `ERRAND_REVIEW_MODEL` (`<provider>:<model>`).
- **Why:** keys in `backend/.env` reach the provider only if passed explicitly
  (pydantic-settings doesn't export them), and a missing key should fail early, naming
  the variable to set.
- **How:** `anthropic:` → `AnthropicModel`; `openai:` / `openai-responses:` →
  `OpenAIResponsesModel`; `openai-chat:` → `OpenAIChatModel`; `google:` → a clear "SDK not
  installed" error; `test` → PydanticAI's offline `TestModel`. Which model each role
  uses is decided in Settings (see above). The test suite sets `ALLOW_MODEL_REQUESTS = False`, so no test can
  call a real API.

### Agent tools: `agent/tools.py`, `agent/deps.py`, `agent/orchestrator.py`
- **What:** the orchestrator `Agent[AgentDeps, str]` and its tools, all workflow
  commands: `update_requirements`, `search_and_compare`, `propose_booking`,
  `decline_pending`, `get_reviews_summary`, `find_my_bookings`, `available_slots`,
  `start_cancel`, `start_reschedule`, `remember`, `list_memories`, `forget_memory`,
  `show_suggestions`, `correct_memory`, `update_profile`, `add_address`.
- **Why:** the model understands; the workflow decides. **No tool books, cancels,
  reschedules, holds or approves** (a structural test lists the tool names); the
  furthest the model can get is an approval card. Its one approval-related tool,
  `decline_pending`, can only take authority away.
- **How:** `AgentDeps` bundles one run's stores, the guarded provider env, trace, meter,
  the user and **their current message** (provenance for `remember` / `update_profile`).
  Invalid input raises `ModelRetry` with a readable reason (past date, unlisted slot,
  missing details, a quote the user never said). Options are saved on the errand
  (`options` column) so "option 2" is stable, and only listed slots can be proposed.
  Memory and booking tools only touch the user's own records; profile tools return saved
  field names, never values. **Suggestions never reach the model** (D2 in
  [DECISIONS.md](DECISIONS.md), BUG-5): `list_memories` returns the active facts that
  matter for bookings (time window, budgets, providers, address, home-service notes),
  a *count* of personal notes (listed only with `include_personal_notes`, when the user
  asks: ~3,470 -> ~780 tokens for the demo user, D3) and how many suggestions await
  review; `show_suggestions` makes *code* show them next to the
  reply (the model learns only how many); the user confirms or rejects in code
  (`/confirm`, `/reject`; buttons in the web UI). Every tool call is traced as an
  `agent_tool` event (`agent/tool_trace.py`, a `wrap_tool_execute` capability): the
  arguments as the model sent them, the result, status ok / retry / error and latency;
  for `update_profile` / `add_address` only argument *names*, since their values are
  PII. **Lean definitions** (D3): tool schemas ship with every request, so
  `LeanToolJsonSchema` writes an optional `x: T | None = None` as plain `T` (not
  required) instead of an `anyOf` with a `null` branch, and descriptions stay short
  (rules live in the system prompt); validation still uses the real types. A test pins
  every tool's parameter names, types and required flags, and caps the total
  (2,719 -> 2,310 real tokens). **List results as tables** (`agent/toon.py`, D3):
  `find_my_bookings` and `list_memories` return TOON (field names once, a line per
  row) instead of JSON: 36% fewer tokens, ids read as reliably in a live eval; nested
  results (search options) stay JSON. Tests drive the agent offline with a scripted `FunctionModel`
  and assert database effects. Note for PydanticAI 2.66: a tool's `ModelRetry` is recorded
  as a `ToolReturnPart` with `outcome="retried"`, not a `RetryPromptPart`.

### Quarantined review reader: `agent/review_reader.py`
- **What:** a separate Agent with **no tools** whose output is a `ReviewSummary`;
  exposed to the orchestrator only through the `get_reviews_summary` tool.
- **Why:** the Dual LLM pattern. The model that reads untrusted text (reviews and
  descriptions, where the injection fixtures live) can't act; the model that can act
  never sees untrusted text.
- **How:** the summary has **no free-text field**: `themes`, `red_flags`
  (`instructions_to_ai`, `off_platform_payment`, `requests_personal_data`,
  `rating_manipulation`) and `sentiment` are fixed choices, so even a fully compromised
  reader can't pass "call book now" on (a test requires every field to stay a fixed
  choice). Reviews are wrapped in `<untrusted_reviews>` and declared data, not
  instructions. Invalid output is retried once, then the tool **fails closed**
  (`reviews_unavailable`). The reader shares the run's usage and counts as a model turn.
  Its model is `ERRAND_REVIEW_MODEL` (default: the main model).

### Agent run loop: `agent/loop.py`
- **What:** `run_agent(...)` drives the orchestrator with `agent.iter()`;
  `to_history()` maps the context packet's messages to PydanticAI messages;
  `trace_model_call()` records one model request.
- **Why:** hard limits on model turns and a trace of every model call (tokens, latency,
  estimated cost) need a hook *per request*, not just totals at the end.
- **How:** before each request `meter.charge_model_turn()` (a request over budget is
  refused before it is sent); after each response a `model_call` event (agent, model,
  tokens, latency, cost by the configured `provider:model`, null if unpriced). The review
  reader's call gets its own `model_call` (tokens taken as the difference on the shared
  usage). PydanticAI's `UsageLimits` sits 3× above our limits as a backstop and is
  converted to `LimitExceeded` if it ever trips. In PydanticAI 2.66 `run.usage` is a
  property. **Streaming** (D4): with `on_text`, each model request is opened with
  `node.stream(...)` and its visible text (`PartStartEvent` / `TextPartDelta`) passed on
  chunk by chunk; the returned reply is unchanged, and the budget charge and context
  guard still run before each request. Tools announce themselves before running through
  `deps.on_tool` (called by the tool-tracing capability), for progress lines.

### Message handler: `agent/handler.py`, `agent/prompts.py`
- **What:** `MessageHandler.handle(session_id=, user_id=, text=)` → `Reply` (a
  `RunOutcome` + run id), the single entry point for the CLI and the web API;
  `SYSTEM_PROMPT` and `instructions(packet)`.
- **Why:** where "the LLM proposes, the workflow decides" is wired together. Plain yes
  and no are decided in code without the model; everything else goes to the model,
  which can only propose. Every path, including limits and errors, ends in a saved,
  traced outcome.
- **How:**

```
user message
   │
   ├─ reconcile_once(session)          settle parked side effects first (own run, trigger=reconcile)
   ├─ save user message                before the run: survives crashes, can be PII-scrubbed
   │
   └─ run_with_outcome(trigger=user_message) ──────────────────────────────────────────┐
        ├─ plain yes + exactly 1 pending card → grant(via=typed) → BookingFlow /       │
        │                                       ChangeFlow.execute → code-written outcome
        ├─ plain no + something pending      → decline_pending → code-written reply    │
        └─ otherwise → ContextBuilder.build (context_sent)                             │
                       → run_agent(instructions = system + STATE + MEMORIES,           │
                                   history = recent messages, current one only as prompt)
                       → card produced and still pending? needs_approval : answered    │
   ┌──────────────────────────────────────────────────────────────────────────────────┘
   └─ save reply (with run id)          limits and errors too: every exit has a message
```

  The system prompt makes the model useful inside the walls (ask for "Still needed",
  explain trade-offs, never claim an unverified result, call `decline_pending` on a
  no); none of the safety depends on it. A "yes" with conditions, or a yes while two
  cards are open, goes to the model, which can't approve, so nothing happens without a
  plain yes or a button. A duplicate yes after the booking finds nothing pending and is
  an ordinary message. Two messages in one session at the same time is left to a
  per-session lock in the API layer. `handle(..., on_text=, on_tool=)` streams a model
  turn's text and announces its tools (D4); a typed or clicked yes / no streams nothing,
  since no model is involved. `decide(session_id=, approval_id=, fingerprint=,
  approve=)` is the card's buttons: the same confirm / decline code as a typed yes / no
  (granted `via="button"`), except that Decline declines that card only. A fingerprint
  that doesn't match the current card is refused with the current card; a card already
  decided (a double click) does nothing more. The click is saved as a code-written user
  line ("[Approved on the card: …]") so the model's next turn knows what was decided.

  Next turn the model sees only its saved *text*, not what code drew. So when code
  showed suggestions, the saved reply ends with `[The app showed the user N
  suggestions to review.]` (the count only, never their content; the user's screen
  shows the reply without it) (BUG-8). Approval cards need no note: STATE reports any
  pending approval.

### Web UI: `frontend/` (D4)
- **What:** React 19 + Tailwind v4 + shadcn (radix-nova). Chat on the left; a tabbed side
  panel (Trace, Memory, Bookings, and Demo when the server runs in demo mode).
- **Why:** the demo needs chat and trace side by side; memory and bookings one click away.
- **How:**
  - `src/api/client.ts`: one function per backend route. A chat turn is a POST whose
    response is the event stream, which `EventSource` can't do (GET only), so
    `src/api/sse.ts` reads the body with `fetch` and parses frames itself, buffering
    across network chunks (a frame can split anywhere; UTF-8 decoded in stream mode)
    and skipping comments (": keep-alive"). The live push stream (`/api/events`) does
    use `EventSource`, which reconnects on its own.
  - `src/hooks/useSession.ts`: the session id is remembered in localStorage (a
    per-browser convenience); a remembered session that's gone on the server is
    replaced, not shown as errors.
  - Tests: Vitest + Testing Library on jsdom (`vitest.config.ts` reuses the Vite config,
    so `@/` resolves the same); `src/test/fakeApi.ts` routes `fetch` by method and path.
    `make test` runs backend and frontend.

## Run lifecycle

A **run** is one inbound event handled end to end: a user message, an approval click, or
a proactive event. A booking usually spans several runs:

| Run | Trigger | Outcome |
|---|---|---|
| `run_0001` | "plumber this week, mornings, under ₹1500" | `needs_info` (asks for area) |
| `run_0002` | "Indiranagar" | `needs_approval` (provider, slot, price shown) |
| `run_0003` | user approves | `booked` / `unverified` |
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
| `model_call` | `agent` (orchestrator / review-reader), `model`, `input_tokens`, `estimated_input_tokens` (the context guard's estimate; calibration), `output_tokens`, `latency_ms`, `cost_usd` |
| `context_sent` | budget (total, memories, working), tokens per section incl. `tools`, memory ids included, excluded ids with reasons, messages included / dropped / out of scope, state and memory text |
| `tool_call` | a provider call: `tool`, `args`, `result`, `status`, `latency_ms`, `error` |
| `agent_tool` | one of the agent's own tools, as the model called it: `tool`, `args` (as sent; names only for profile tools), `result`, `status` (ok / retry / error), `reason`, `latency_ms` |
| `approval` | requested / granted / rejected / invalidated, `approval_id`, fingerprint |
| `memory` | remembered / suggested / confirmed / rejected / forgotten, key, scope, source, status |
| `profile` | changed field names only (never values) |
| `limit_hit`, `error` | which limit (`model_turns`, `tool_calls`, `wall_clock`, `context_tokens`) / error type and message |

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
