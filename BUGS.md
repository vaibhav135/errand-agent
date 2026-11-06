# BUGS

Real bugs hit while building, logged when found (**open**) and closed with a `test:`
commit that reproduces the bug (`xfail(strict=True)`) and the `fix:` commit that removes
the marker. Things found alongside that are *not* bugs (refactors, UX, test gaps) are
listed at the end.

| # | Bug | Severity | Status |
|---|---|---|---|
| 1 | Rollback masked the real database error | medium | fixed |
| 2 | Seeded history contradicts the mock world | high | open |
| 3 | The context budget isn't a cap | high | open |
| 4 | Slash commands leak to the model | low | open |

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
- **Status:** open
- **What broke:** on a plain "hi" in the first live `make chat`, the agent apologised for
  "its own" earlier mistakes. The seeded transcript has the assistant saying "Saturday's
  cleaning is confirmed for 11:00." (3×) and "Your booking is confirmed…", but no such
  booking exists in the mock world. The agent called `find_my_bookings`, saw the
  contradiction and confessed, unprompted. Trusting the provider over the transcript was
  right; the seeded data was wrong.
- **Why:** the generator's `FILLER` exchanges contain hand-written factual claims
  (bookings, ratings, schedules) that are never checked against the mock world's data.
  Suspected second cause, to confirm in the repro: facts are placed into sessions by a
  shuffle that ignores their `days_ago`, so a superseded statement ("plumbers under
  1200") can be said *after* the one that replaced it ("up to 1500"); the agent also
  said "I told you up to 1200 was saved".
- **How found:** live chat; trace `run_9617af2d…` (all 201 messages in context, one
  `find_my_bookings` call).
- **Fix:** -
- **Regression test:** -
- **Commits:** -

## 3. The context budget isn't a cap
- **Status:** open
- **What broke:** with `ERRAND_CONTEXT_TOKEN_BUDGET=6000`, one run's real model requests
  were 6,959, 10,623 and 11,200 input tokens. The `context_sent` event reported 3,500,
  so the trace understates what was actually sent. The assignment asks for a fixed
  per-turn budget.
- **Why (hypotheses, to confirm in the repro):** (a) the budget covers only our context
  packet, not the 17 tool definitions sent with every request; (b) tool calls and
  results accumulate within a run, growing each request; (c) the chars/3.5 estimate may
  differ from the provider's tokenizer.
- **How found:** same trace: `model_call.input_tokens` vs `context_sent.tokens.total`.
- **Fix:** -
- **Regression test:** -
- **Commits:** -

## 4. Slash commands leak to the model
- **Status:** open
- **What broke:** `/trace` (no longer a chat command after the CLI was slimmed down) was
  sent to the model as a user message; it replied "I don't have a /trace command".
- **Why:** `chat_loop` handles only `/quit` and `/exit`; any other text, including
  `/`-prefixed commands, goes to the handler.
- **How found:** live chat.
- **Fix:** -
- **Regression test:** -
- **Commits:** -

## Not bugs: follow-ups found alongside

| Item | Kind | Plan |
|---|---|---|
| The seeded "long conversation" fits in ~3k tokens, so no message is ever dropped from context: context management is never actually exercised | test-data gap | with BUG-3: a seed or test that really overflows the budget |
| PydanticAI's startup banner prints in the chat | UX | set `PYDANTIC_AI_NO_BANNER=1` |
| No "working…" indicator during a 5–15 s turn, so the user types ahead; those keystrokes echo into the output and are sent as the next messages | UX | print a working line before each run |

<!-- Template
## N. <short title>
- **Status:** open | fixed
- **What broke:**
- **Why:**
- **Fix:**
- **Regression test:** `path/to/test_file.py::test_name`
- **Commits:** repro `<hash>` · fix `<hash>`
-->
