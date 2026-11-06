# BUGS

Real bugs hit while building, logged when found (**open**) and closed with a `test:`
commit that reproduces the bug (`xfail(strict=True)`) and the `fix:` commit that removes
the marker. Things found alongside that are *not* bugs (refactors, UX, test gaps) are
listed at the end.

| # | Bug | Severity | Status |
|---|---|---|---|
| 1 | Rollback masked the real database error | medium | fixed |
| 2 | Seeded history contradicts the mock world | high | fixed |
| 3 | The context budget isn't a cap | high | fixed |
| 4 | Slash commands leak to the model | low | fixed |
| 5 | The agent's own tool calls aren't traced | medium | fixed |
| 6 | An inference can contradict an explicit preference | medium | fixed |
| 7 | The token estimate undercounts real tokens by ~45% | high | fixed |
| 8 | The model doesn't know what code showed the user | medium | fixed |
| 9 | The test suite runs out of file descriptors on a default Mac | medium | fixed |

## 1. Rollback masked the real database error
- **What broke:** When a write failed in a way that makes SQLite end the transaction
  itself, `transaction()` raised `OperationalError: cannot rollback - no transaction is
  active` instead of the real error (e.g. `IntegrityError` from a trigger's
  `RAISE(ROLLBACK)`, or a disk-full / I/O error). Data was safe; the cause was lost.
- **Why:** The `except` branch always issued `ROLLBACK`. SQLite had already rolled back,
  so the second rollback failed, and Python propagated the newer exception.
- **How found:** Code review of `transaction()` while building the migration runner;
  reproduced with a `RAISE(ROLLBACK)` trigger before fixing.
- **Fix:** Roll back only if `conn.in_transaction`.
- **Regression test:** `backend/tests/unit/test_store_db.py::test_original_error_survives_when_sqlite_already_rolled_back`
- **Commits:** repro `9a809a2` · fix `0d4e6c6`

## 2. Seeded history contradicts the mock world
- **Status:** fixed
- **What broke:** on a plain "hi" in the first live `make chat`, the agent apologised for
  "its own" earlier mistakes. The seeded transcript has the assistant saying "Saturday's
  cleaning is confirmed for 11:00." (3×) and "Your booking is confirmed…", but no such
  booking exists in the mock world. The agent called `find_my_bookings`, saw the
  contradiction and confessed, unprompted. Trusting the provider over the transcript was
  right; the seeded data was wrong.
- **Why:** the seed had two sources of truth built independently, the seed generator
  (memories + conversation) and the mock world's booking history, and nothing
  cross-checked them. Three defects followed:
  1. the canned `FILLER` replies stated bookings that never existed;
  2. the inferred memories cited bookings the history didn't have ("2 completed
     bookings with AquaCare": 0 completed, 1 cancelled);
  3. facts were shuffled into sessions regardless of their date, so the superseded
     "plumbers under 1200" was said 40 days *after* "up to 1500" replaced it (44 of
     48 facts were said more than 10 days from when they were learned).
- **How found:** live chat; trace `run_9617af2d…` (all 201 messages in context, one
  `find_my_bookings` call).
- **Fix:** the mock world's history is the one source of truth for bookings (it now
  holds the completed bookings the inferences cite); facts are said in the session
  nearest their date, oldest first; the small talk states no booking facts.
- **Regression test:** `backend/tests/unit/test_memory_seed.py::test_facts_are_said_when_they_were_learned`,
  `::test_inferred_reasons_match_the_booking_history`,
  `::test_the_seeded_assistant_makes_no_booking_claims`; and
  `backend/tests/unit/test_memory_inference.py::test_the_seeds_usual_providers_are_what_the_rules_derive`
  (runs the real inference rules over the history).
- **Commits:** repro `dae1e17` · fix `c347e46`

## 3. The context budget isn't a cap
- **Status:** fixed
- **What broke:** with `ERRAND_CONTEXT_TOKEN_BUDGET=6000`, one run's real model requests
  were 6,959, 10,623 and 11,200 input tokens. The `context_sent` event reported 3,500,
  so the trace understates what was actually sent. The assignment asks for a fixed
  per-turn budget.
- **Why:** the budget only governed the context *packet* the run started with, and even
  that was filled from the wrong place:
  1. the conversation came from *all* of the user's sessions (201 messages, none
     dropped, because the whole seeded history fit), so nothing ever limited it;
  2. the 17 tool definitions sent with every request (~2,000 tokens) weren't counted;
  3. each tool call and result in a run was appended to later requests uncapped (the
     repro measured 2,457 → 5,062 against 4,500).
  Hypothesis (c), estimate vs. the provider's tokenizer, is now observable rather than
  guessed: every `model_call` records both numbers.
- **How found:** same trace: `model_call.input_tokens` vs `context_sent.tokens.total`.
- **Fix:** decided after reviewing how robust agents manage context (`docs/DECISIONS.md`,
  D1): history scoped to the current session (earlier sessions logged as out of
  scope, never sent); the budget counts tool definitions and reserves 1,500 tokens of
  working room; a context guard before every request masks the run's oldest tool
  results and, if it still can't fit, ends the run `limit_reached` (`context_tokens`).
- **Regression test:** `backend/tests/unit/test_context_budget.py` (every request fits,
  measured by the scripted model itself; tool definitions counted; an unfittable run
  ends honestly) and `backend/tests/unit/test_memory_seed.py::test_only_the_current_session_reaches_the_model`
- **Commits:** repro `9874509` · fix `b1ea3fe`

## 4. Slash commands leak to the model
- **Status:** fixed
- **What broke:** `/trace` (no longer a chat command after the CLI was slimmed down) was
  sent to the model as a user message; it replied "I don't have a /trace command".
- **Why:** `chat_loop` handles only `/quit` and `/exit`; any other text, including
  `/`-prefixed commands, goes to the handler.
- **How found:** live chat.
- **Fix:** anything starting with `/` is a chat command handled in the CLI; unknown ones
  get "Unknown command". None reaches the agent or the saved conversation.
- **Regression test:** `backend/tests/unit/test_chat_cli.py::test_unknown_commands_never_reach_the_model`
- **Commits:** repro `5705b2c` · fix `02234a3`

## 5. The agent's own tool calls aren't traced
- **Status:** fixed
- **What broke:** in the first live chat the model mentioned the two pending suggestions
  (Sparks, CoolBreeze) although the context builder had excluded them. The run had 3
  model calls but only 1 `tool_call` event (`list_bookings`), so it most likely called
  `list_memories`, but the trace can't show it. The assignment requires tool names and
  arguments in every trace.
- **Why:** only provider calls are traced (through `GuardedToolEnv`); the agent's own
  tools (`list_memories`, `update_requirements`, `propose_booking`, …) run without any
  trace event.
- **How found:** reading the trace while checking what reached the model.
- **Related:** `list_memories` returns pending suggestions *with their values* to the
  model. Decided to keep unconfirmed inferences out of the model's context entirely:
  suggestions reach the user through code (`docs/DECISIONS.md`, D2); fixed together
  with this bug.
- **Fix:** a `wrap_tool_execute` capability on the orchestrator traces every agent tool
  call as an `agent_tool` event (arguments as sent, result, ok / retry / error,
  latency; argument names only for the profile tools, whose values are PII). Per D2,
  `list_memories` now returns active facts and a count of suggestions; the
  confirm/reject tools are gone; `show_suggestions` makes code show them, and the user
  decides in code (`/memories`, `/confirm N`, `/reject N`).
- **Regression test:** `backend/tests/unit/test_agent_tool_tracing.py`
  (`::test_every_agent_tool_call_is_traced`, `::test_contact_details_are_traced_by_name_only`,
  `::test_pending_suggestions_never_reach_the_model`)
- **Commits:** repro `584f1b8` · fix `055cc84`

## 6. An inference can contradict an explicit preference
- **Status:** fixed
- **What broke:** run over the demo user's booking history, the inference rules suggest
  "earliest start 09:00" (three old bookings started at 9:00), although the user
  explicitly said "never before 10am" 40 days ago. The store would save it as a pending
  suggestion, so the agent would ask the user to confirm the opposite of what they told
  it. Latent today: `suggest_from_history` isn't called at runtime yet.
- **Why:** `MemoryStore.propose_inferred` skips a suggestion only if the *same value* is
  already active / pending / rejected, or the fact was forgotten. A *different* value
  for a key the user stated explicitly isn't blocked, so explicit doesn't beat inferred.
- **How found:** writing the BUG-2 consistency test, which runs the real rules over the
  seeded history.
- **Fix:** explicit beats inferred: `propose_inferred` skips a suggestion when the user
  has an active explicit fact for the same key and scope (notes excepted, since they
  coexist). The seeded user is now a fixed point of inference.
- **Regression test:** `backend/tests/unit/test_memory_inference.py::test_an_explicit_preference_blocks_a_different_inference`,
  `::test_the_seeded_user_is_a_fixed_point_of_inference`
- **Commits:** repro `32174f3` · fix `e7997da`

## 7. The token estimate undercounts real tokens by ~45%
- **Status:** fixed
- **What broke:** in the second live chat, every `model_call` showed our estimate well
  below the provider's real input count: 2,582 vs 3,763 (1.46×), 4,404 vs 6,264
  (1.42×), 4,843 vs **7,474** (1.54×). The estimate is meant to be *pessimistic*; it
  isn't, so the 6,000-token cap let a 7,474-token request through. BUG-3's fix holds
  in our units, not in the provider's.
- **Why:** measured with Anthropic's `count_tokens` on our own texts, all three
  hypotheses held: (a) JSON *data* is dense, 2.04 chars/token; (b) a hidden tool-use
  prompt of 321 tokens is added whenever tools are offered, which we never counted;
  and on top of that (c) even prose and tool schemas run 2.58-3.00 chars/token on this
  model, not 3.5 (newer Claude tokenizers produce ~30% more tokens per character).
- **How found:** the calibration field added by BUG-3 (`estimated_input_tokens` next
  to `input_tokens`), on the user's second live chat.
- **Fix:** a calibrated estimate (text 2.4, JSON data 1.9, +350 per request with
  tools), each ~7% below the lowest measurement. `make calibrate` records exact sample
  texts with their real counts; the budget default becomes 7,500, derived from
  measured needs (`docs/DECISIONS.md`, D3).
- **Regression test:** `backend/tests/unit/test_token_calibration.py` (every recorded
  sample is estimated at or above its real count; tool definitions include the hidden
  tool-use prompt), offline from `backend/tests/data/token_calibration.json`.
- **Commits:** repro `d3bd25b` · fix `9931c5b`

## 8. The model doesn't know what code showed the user
- **Status:** fixed
- **What broke:** after code drew the two suggestions in a box under its reply, the next
  turn the model told the user "I never actually displayed them, even though I said I
  had", which was false, and offered to show them again.
- **Why:** things code renders (the suggestions box) aren't part of what the model sees
  next turn: only the text of its own reply is saved to the conversation, and tool
  results don't carry across runs. Approval cards don't have this gap because STATE
  reports "Pending approval: …"; suggestions have no such line.
- **How found:** the user's second live chat (trace `run_2513f045…`).
- **Fix:** when code shows suggestions, the saved reply ends with a code-written note,
  "[The app showed the user N suggestions to review.]": the count only, never their
  content, and only in the saved conversation (the user's screen shows the reply
  without it). A STATE line was rejected: it could say suggestions *exist*, not
  whether they were already *shown*, which was the actual mistake.
- **Regression test:** `backend/tests/unit/test_agent_tool_tracing.py::test_next_turn_the_model_knows_suggestions_were_shown`
- **Commits:** repro `9c7b7d3` · fix `1a63b8c`

## 9. The test suite runs out of file descriptors on a default Mac
- **Status:** fixed
- **What broke:** `make test` failed for the user with 14 failures and 17 errors, all
  `OSError: [Errno 24] Too many open files` or SQLite's "unable to open database
  file". It passed on the machine running the commit gate.
- **Why:** the tests never close their SQLite connections. Each WAL connection holds up
  to 3 files (`.db`, `-wal`, `-shm`), and helpers like `World.mock_exec` / `mock_count`
  open a new connection on every call. Test objects reference each other in cycles
  (World -> stores -> connection), so connections are freed only when Python's
  occasional full garbage collection runs; across the whole session the peak reached
  ~300 open descriptors. macOS's default limit is 256; the gate's shell allowed
  1,048,576, which hid it. Under `ulimit -n 256` the gate reproduces the failure.
  The app itself closes its connections (`Services.close()`).
- **How found:** the user ran `make test` in a default terminal.
- **Workaround until fixed:** `ulimit -n 4096` in that terminal (no longer needed).
- **Fix:** an autouse fixture in `tests/conftest.py` tracks every `sqlite3.connect`
  during a test and closes those connections at teardown (ones made in the app's
  thread under `TestClient` are closed by the app's own shutdown); `mock_exec` /
  `mock_count` close their connection at once. Peak open files: ~302 -> 23. `make test`
  now runs under `ulimit -n 256`, so a future leak fails the gate in every environment.
- **Regression test:** `backend/tests/unit/test_test_hygiene.py` (a connection opened
  in one test is closed when it ends), plus the gate's `ulimit -n 256`.
- **Commits:** repro `dda6be5` · fix `7a57433`

## Not bugs: follow-ups found alongside

| Item | Kind | Plan |
|---|---|---|
| The seeded "long conversation" fits in ~3k tokens, so no message is ever dropped from context: context management is never actually exercised | test-data gap | addressed by BUG-3: old sessions are now provably out of scope (`messages_out_of_scope`), and trimming within a session is tested with 500 messages; a long-session eval follows in Phase 8 |
| PydanticAI's startup banner prints in the chat | UX | done: the chat sets `PYDANTIC_AI_NO_BANNER=1` unless already set |
| No "working…" indicator during a 5–15 s turn, so the user types ahead; those keystrokes echo into the output and are sent as the next messages | UX | done: a "working…" line prints before each run |
| `list_memories` returns all 48 saved facts, 30 of them personal notes, on every call (~2.6k tokens; called on 3 of 4 turns in one chat) | token efficiency | return the booking-relevant facts plus "and N personal notes"; consider a compact format (TOON) for list-shaped tool results |
| The suggestions hint says "/confirm N"; the user typed a literal N, and a bare `/confirm` just errors | UX | show the real numbers ("/confirm 40 · /reject 44"); a bare `/confirm` lists the choices |
| The model said "I've started the plumber booking", which can read as booked | prompt wording | say "started looking for a plumber"; nothing is a booking until confirmed |

<!-- Template
## N. <short title>
- **Status:** open | fixed
- **What broke:**
- **Why:**
- **Fix:**
- **Regression test:** `path/to/test_file.py::test_name`
- **Commits:** repro `<hash>` · fix `<hash>`
-->
