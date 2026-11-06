# Design note

**The LLM proposes, the workflow decides.** Claude reads the request and calls tools that
search, rank, hold a slot and create an approval card. No tool books, cancels or
approves. Deterministic code does those, and only on the user's approval. Detail:
[ARCHITECTURE.md](ARCHITECTURE.md); reasons and rejected options: [DECISIONS.md](DECISIONS.md).

**State model.** Each request is an *errand* (book, cancel or reschedule) with an explicit
state (`collecting → comparing → awaiting_approval → booking → verifying → booked`, plus
parked and terminal states). Every legal move is a row in a table, and any other move
raises. One *run* per inbound event (a message, a click, a reconcile pass, a provider's
cancellation) carries the errand forward. Hard limits end a run, never an errand.

**Approval enforcement.** A proposal holds the slot and stores an approval fingerprinted on
provider, slot and price. Only a plain "yes" (a fixed vocabulary, matched in code) or a
click that sends back the fingerprint it displayed can grant it. "yes but at 11" goes to
the model, which cannot approve. If the hold expired and the re-hold quotes a different
price, the approval is void and a new card asks again. A model reply claiming an unverified booking gets a code-written
correction.

**Idempotency and recovery.** The approval is consumed and an idempotency key written
*before* `book` is called. A timeout is never treated as failure: the workflow asks
`get_booking_status` with the same key, and retries (same key, at most 3 attempts) only if
the provider says nothing was booked. Anything still unknown is parked, and a reconciler
(at startup, every 30 s, and before each turn) settles it from the provider's records,
never by booking again. The provider world lives in its own database, so a crash
mid-booking is recovered on restart.

**Memory.** Facts are explicit (saved only with a quote from the user's own message) or
inferred (from booking history, shown to the user by code and kept out of the model's
context until confirmed). Explicit beats inferred; facts can be corrected or forgotten,
which also scrubs them from stored messages. Each request's context is capped at 7,500
calibrated tokens (tools, instructions, relevant current memories, this session's recent
messages), and what was sent or left out is logged.

**Observability.** One JSONL trace per run: state transitions, every model call (tokens,
latency, cost including the prompt cache), every provider and agent tool call with
sanitized arguments and results, the context sent, approvals, limits and errors. Personal
details are traced by field name only.

**Trade-offs.** Guarantees live in code rather than prompts: more code, but nothing a
review or a model says can grant permission. SQLite with explicit transactions, not a
workflow engine: enough for one process, and keys plus a reconciler give exactly-once
effects. Provider cancellations are found by polling `list_bookings` (the contract has no
webhooks). Only Anthropic models were tested.

### Trace excerpt: a booking that timed out after succeeding

The user typed "yes" to a QuickFix Plumbing card; the mock was armed to book and then
time out. Abridged from the approval run; the full conversation (3 runs, real model,
$0.034) is in [docs/trace/](trace/), recorded with `make trace`.

```
run_started       trigger: user_message ("yes": decided in code, 0 model calls)
approval          granted → consumed                ← used up before anything is sent
state_transition  awaiting_approval → booking        reason: idem_831a…  (the one key)
tool_call         book(hold_3ae8…, idem_831a…)      → error: ProviderTimeout
                                                    ← the provider booked; the answer was lost
state_transition  booking → verifying               event: timeout   (not "failed")
tool_call         get_booking_status(idem_831a…)    → booked, bk_6e5f…
state_transition  verifying → booked                event: confirmed
run_finished      booked · 2 provider calls · no second book
                  "Booked QuickFix Plumbing for Sat 07 Nov, 10:00 at ₹699."  ← written by code
```
