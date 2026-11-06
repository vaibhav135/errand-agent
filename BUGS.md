# Bugs

Real bugs hit while building. Each was logged as soon as it was found, reproduced by a
test committed first as an expected failure (`xfail(strict=True)`), and fixed in a
separate commit that removes the marker; both commits are listed. Numbers are stable:
code, commits and docs refer to them.

## The top three

### BUG-12 · The model's false "booked" reached the user unchecked
- **What broke:** with an approval card still waiting, the model replied "All done, your
  plumber is booked!". Nothing was booked (approval enforcement held), but that sentence
  was shown to the user as written.
- **Why:** booking outcomes are written by code, but a model turn's reply is the model's
  own text, and only the system prompt asked it not to claim unverified results. The
  assignment's "never claim success until verified" needs code, like every other
  guarantee here: a prompt is a request, not a check.
- **How found:** an eval written to attack the system with a deliberately misbehaving
  model, not by a user.
- **Fix:** before a model turn's reply is saved, `agent/claims.py` checks it against
  verified state. A claim that the open request is done ("booked", "cancelled", "moved",
  not negated or hedged just before) gets a code-written note after it: "Note from the
  app: nothing is booked yet. The … card is still waiting for your approval." The
  model's text is kept, the verified status follows it, and the correction is traced
  (`claim_corrected`). A mention of a past booking with nothing open is left alone.
- **Regression test:** `backend/tests/evals/test_approval.py::test_a_false_booked_claim_is_corrected`
  (plus the compromised model's false "cancelled"), and the phrase table in
  `backend/tests/unit/test_agent_claims.py` (claims vs negations and hedges).
- **Commits:** repro `84acbdd` · fix `d01e810`

### BUG-7 · The token estimate undercounted real tokens by ~45%
- **What broke:** every model call's estimate was well below the provider's real input
  count: 2,582 vs 3,763 (1.46×), 4,404 vs 6,264 (1.42×), 4,843 vs **7,474** (1.54×). The
  estimate is meant to be pessimistic, so a 6,000-token cap let a 7,474-token request
  through: the fixed context budget held in our units, not the provider's.
- **Why:** measured with Anthropic's `count_tokens` on our own texts, all three
  hypotheses held: JSON data is dense (2.04 chars/token); a hidden tool-use prompt of 321
  tokens is added whenever tools are offered, and we never counted it; and even prose
  and tool schemas run 2.58–3.00 chars/token on this model, not the assumed 3.5.
- **How found:** the calibration field added for BUG-3 (`estimated_input_tokens` next to
  `input_tokens` on every model call), in a live chat.
- **Fix:** a calibrated estimate (text 2.4, JSON 1.9 chars/token, +350 per request with
  tools), each set ~7% below the lowest measurement. `make calibrate` records exact
  sample texts with their real counts, and the 7,500-token budget is derived from
  measured needs (`docs/DECISIONS.md`, D3).
- **Regression test:** `backend/tests/unit/test_token_calibration.py` (every recorded
  sample is estimated at or above its real count; tool definitions include the hidden
  prompt), offline from `backend/tests/data/token_calibration.json`.
- **Commits:** repro `d3bd25b` · fix `9931c5b`

### BUG-8 · The model didn't know what code had shown the user
- **What broke:** after code drew two memory suggestions in a box under the model's
  reply, the next turn the model told the user "I never actually displayed them, even
  though I said I had", which was false, and offered to show them again.
- **Why:** what code renders isn't part of what the model sees next turn. Only the text
  of its own reply is saved to the conversation, and tool results don't carry across
  runs. Approval cards don't have this gap because the state section reports "Pending
  approval: …"; suggestions had nothing similar.
- **How found:** a live chat (trace `run_2513f045…`).
- **Fix:** when code shows suggestions, the saved reply ends with a code-written note,
  "[The app showed the user N suggestions to review.]": the count only, never their
  content, and only in the saved conversation (the screen shows the reply without it). A
  state line was rejected: it could say suggestions *exist*, not whether they were
  already *shown*, which was the actual mistake.
- **Regression test:** `backend/tests/unit/test_agent_tool_tracing.py::test_next_turn_the_model_knows_suggestions_were_shown`
- **Commits:** repro `9c7b7d3` · fix `1a63b8c`

## Honourable mentions

**BUG-1 · Rollback masked the real database error.** When SQLite had already ended a
failed transaction itself, our second `ROLLBACK` raised "cannot rollback" and hid the
real error. Fix: roll back only if a transaction is still open.
Test `test_store_db.py::test_original_error_survives_when_sqlite_already_rolled_back` ·
`9a809a2` → `0d4e6c6`

**BUG-2 · Seeded history contradicted the mock world.** On a plain "hi", the agent
apologised for "its" earlier mistakes: the seeded chat claimed bookings the provider had
never made. Fix: the mock world's history is the one source of truth for bookings, and
facts are said in the session nearest their date.
Tests in `test_memory_seed.py` and `test_memory_inference.py` · `dae1e17` → `c347e46`

**BUG-3 · The context budget wasn't a cap.** With a 6,000-token budget, real requests
reached 11,200 tokens: the budget ignored tool definitions, other sessions and the run's
own tool results. Fix: session-scoped history, everything counted, and a guard before
every request (D1).
Tests in `test_context_budget.py` · `9874509` → `b1ea3fe`

**BUG-4 · Slash commands leaked to the model.** `/trace` in the terminal chat went to the
model as a message. Fix: anything starting with `/` is handled by the CLI.
Test `test_chat_cli.py::test_unknown_commands_never_reach_the_model` · `5705b2c` → `02234a3`

**BUG-5 · The agent's own tool calls weren't traced.** Only provider calls were, so the
trace couldn't show the model reading suggestions it shouldn't have seen. Fix: every
agent tool call is traced (`agent_tool`), and suggestions reach the user through code
only (D2).
Tests in `test_agent_tool_tracing.py` · `584f1b8` → `055cc84`

**BUG-6 · An inference could contradict an explicit preference.** The rules suggested
"earliest start 09:00" although the user had said "never before 10am". Fix: explicit
beats inferred.
Test `test_memory_inference.py::test_an_explicit_preference_blocks_a_different_inference`
· `32174f3` → `e7997da`

**BUG-9 · The test suite ran out of file descriptors on a default Mac.** Tests never
closed their SQLite connections (peak ~300 open files against macOS's 256). Fix: an
autouse fixture closes them (peak 23), and `make test` runs under `ulimit -n 256`.
Test `test_test_hygiene.py` · `dda6be5` → `7a57433`

**BUG-10 · The web chat showed nothing while the agent worked.** A turn with no tool call
was silent for seconds, then the reply landed at once. Fix: typing dots, a streaming
caret, and a steady reveal of bursty chunks (latency itself: D5).
Tests in `Chat.test.tsx` · `57c7d25` → `60fcc60`

**BUG-11 · "Here are your other options" after a taken slot, but none were shown.** The
code-written outcome promised options it never listed. Fix: the last search minus the
lost slot is listed, and drawn on the slots card.
Test `test_agent_handler.py::test_a_taken_slot_lists_the_options_still_available` ·
`2031167` → `972056b`

## Not bugs: follow-ups found alongside

| Item | Kind | Outcome |
|---|---|---|
| The seeded "long conversation" fit in ~3k tokens, so context trimming was never exercised | test-data gap | done: old sessions are out of scope by design (BUG-3), trimming is tested with 500 messages, and an eval runs 55 memories + 300 messages |
| PydanticAI's startup banner printed in the chat | UX | done: suppressed (`PYDANTIC_AI_NO_BANNER=1`) |
| No "working…" line during a 5–15 s turn in the terminal chat, so typed-ahead keys were sent as messages | UX | done: a "working…" line before each run |
| `list_memories` returned all 48 facts (~2.6k tokens) on most turns | token efficiency | done: booking facts plus a count of personal notes (~780 tokens); then TOON tables (D3: −31% per request, same accuracy) |
| The suggestions hint said "/confirm N" and a user typed a literal N | UX | done: hints show a real number; a bare `/confirm` lists the choices |
| The model said "I've started the plumber booking", which can read as booked | prompt wording | done: before approval it's "looking for" or "proposing" |
| A "hello" took 3.5–8.6 s before the first word | latency | done (D5): `low` effort halved errand turns; caching cut cost per turn 55–83%; the variance was the API's own |

<!-- Template
## N. <short title>
- **Status:** open | fixed
- **What broke:**
- **Why:**
- **Fix:**
- **Regression test:** `path/to/test_file.py::test_name`
- **Commits:** repro `<hash>` · fix `<hash>`
-->
