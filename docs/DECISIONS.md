# Design decisions

Decisions that changed or extended the design, with the reasoning and what was rejected.
`ARCHITECTURE.md` (the map) and `COMPONENTS.md` (the detail) describe how the code
works today; this file records *why* it is
shaped that way when the choice wasn't obvious. Newest last.

## D1. Context: session-scoped history, a budget that counts everything (2026-11-06)

**Status:** implemented by the BUG-3 fix. How it works now: COMPONENTS.md, "Context
builder" (the budget split: tools ~3,270 incl. the hidden tool prompt, system, state, memories ≤600, working room
1,500, then this session's messages) and "Context guard" (in-run masking).

**Trigger:** the first live chat (BUGS.md, BUG-2 and BUG-3). On a plain "hi", all 201
seeded messages from 10 past sessions reached the model, and the real request was
6,959 → 11,200 input tokens against a 6,000-token "budget" whose log said 3,500.

**What robust systems do** (sources below):
- **Memory is split by scope.** Short-term memory is the *current thread/session's*
  messages, trimmed to fit; long-term memory is a separate store shared across sessions
  and retrieved selectively. Continuity across sessions comes from the store, not from
  replaying old transcripts (LangGraph, OpenAI Agents SDK sessions, MemGPT, Mem0).
- **Less context is better, not just cheaper.** "Find the smallest set of high-signal
  tokens that maximize the likelihood of your desired outcome" (Anthropic). Every
  frontier model tested degrades as input grows, well before the limit, and distractors
  make it worse (Chroma, "Context Rot"); information in the middle of long contexts is
  used worst ("Lost in the Middle").
- **Simple, deterministic trimming beats clever summarisation.** Keep the last N turns
  (OpenAI cookbook); dropping old tool outputs ("observation masking") matched or beat
  LLM summarisation at lower cost (JetBrains Research, "The Complexity Trap"); clearing
  old tool results is "one of the safest lightest touch forms of compaction" (Anthropic).
- **Structured state and just-in-time retrieval** instead of preloading; a small tool
  set with compact outputs, since tool definitions ship with every request (Anthropic).

**Where we already matched:** the long-term store (closed key vocabulary, status
filters, relevance by service, its own token cap, every exclusion logged); the STATE
section rebuilt from the DB each turn (structured notes); facts fetched by tools
(bookings) or a sub-agent returning compact labels (reviews); tool results never stored
in the conversation, so old observations are masked between turns.

**Decision:**
1. **History is scoped to the current session.** Earlier sessions are never sent; the
   `context_sent` event logs them as out of scope (with a count) so the trace proves it.
   Memories, state and tools carry continuity.
2. **The budget counts everything that is sent:** system prompt, state, memories,
   history, the tool definitions (measured from the agent's real schemas), and the tool
   calls and results that accumulate within a run.
3. **Within a run, older tool results are masked** (replaced by a short placeholder)
   when the next request would exceed the budget, via a PydanticAI history processor.
   If it still can't fit, the run ends `limit_reached`, which already has an honest
   user-facing outcome.
4. **The estimate is calibrated against reality:** each `model_call` records our
   estimate next to the provider's `input_tokens`; evals check the estimate stays
   pessimistic.
5. **The seeded long conversation is kept** (the assignment asks for it) and is
   provably excluded; a separate eval drives one long *session* to exercise trimming.

**Rejected:**
- *LLM summarisation of old sessions:* costs a model call, isn't deterministic (bad for
  offline evals), and the JetBrains result says it isn't better than masking.
- *Vector search over memories / MemGPT-style self-managed paging:* both make "forgotten
  and unconfirmed facts never reach the model" harder to prove; with a closed key
  vocabulary, deterministic filtering is both simpler and testable.
- *Prompt-caching tuning* (Manus's top lesson: stable prefix, append-only context): a
  real cost win, noted as future work for the design note.

**Related decision: pending suggestions stay out of the model's context.** The
assignment says unconfirmed inferred facts "must never be included"; how suggestions
reach the user instead is decided in D2.

**Sources:**
[Anthropic: Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) ·
[Anthropic: Context management](https://www.anthropic.com/news/context-management) ·
[Manus: Context engineering lessons](https://manus.im/en/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus) ·
[OpenAI cookbook: Session memory](https://developers.openai.com/cookbook/examples/agents_sdk/session_memory) ·
[LangGraph: Memory concepts](https://docs.langchain.com/oss/python/concepts/memory) ·
[JetBrains Research: The Complexity Trap (arXiv 2508.21433)](https://arxiv.org/abs/2508.21433v3) ·
[Chroma "Context Rot" (summary)](https://www.zenml.io/llmops-database/context-rot-evaluating-llm-performance-degradation-with-increasing-input-tokens) ·
[Lost in the Middle (arXiv 2307.03172)](https://arxiv.org/abs/2307.03172v3) ·
[MemGPT (arXiv 2310.08560)](https://arxiv.org/pdf/2310.08560) ·
[Mem0 (arXiv 2504.19413)](https://arxiv.org/abs/2504.19413v1)

## D2. Suggestions reach the user through code, never through the model (2026-11-06)

**Status:** implemented by the BUG-5 fix (COMPONENTS.md, "Agent tools" and "Chat
CLI"); the web UI's memory panel adds Confirm / Reject buttons in Phase 6.

**Trigger:** BUG-5. In the first live chat the model listed the two pending suggestions
("Sparks… CoolBreeze") by value: the context builder excluded them, but the
`list_memories` tool returned them, and tool results are part of what the model reads.

**The requirements in tension** (assignment): users must be able to *inspect* their
memory, which includes suggestions awaiting an answer; and "unconfirmed inferred facts
must never be included" in the model's context, "and prove it in tests". So a
suggestion must reach the user without passing through the model: the same shape as
approvals, where code shows the details and the model never decides.

**Who does what:** *creating* a suggestion needs no LLM; it is counting over the
provider's booking history (`memory/inference.py`, e.g. 2+ completed bookings with
one provider). The LLM is needed for language: it proposes explicit preferences from
what the user said (`remember`, verified against the user's words), and it drives the
conversation.

**Decision (option A, with a conversational refinement):**
1. `list_memories` returns active facts only, plus how many suggestions await review.
2. `confirm_suggestion` / `reject_suggestion` are removed from the model's tools.
3. A `show_suggestions` tool lets the model *ask code* to show them: code renders the
   suggestions (values, reasons) next to the reply, like an approval card; the model
   only learns how many were shown.
4. The user confirms or rejects in code: `/memories`, `/confirm N`, `/reject N` in the
   chat; Confirm / Reject buttons in the web UI's memory panel.
5. Once confirmed, a suggestion is an ordinary active preference and reaches the model
   when relevant, as any other.

**Rejected:**
- *B. The model sees "a suggestion about your electrician", code shows the value, the
  user replies yes/no:* "yes" would then mean two things whenever an approval card is
  also pending, on the most safety-critical path (typed yes → booking), and the model
  still sees part of the unconfirmed fact.
- *C. Keep the values, labelled "unconfirmed":* fails a literal reading of the
  requirement, and our own logs couldn't prove otherwise.
- *An LLM extracting preferences from vague conversation* (Mem0-style): could fit as a
  future way to *create* pending suggestions (the model may propose; it must never be
  handed unconfirmed facts back), but isn't needed for the requirements.

## D3. Counting tokens honestly, and sizing the budget from measured needs (2026-11-06)

**Status:** implemented by the BUG-7 fix (counting, budget 7,500); the fixed part was
then slimmed and the budget re-derived (see "Re-derived after slimming" below).

**Trigger:** BUG-7. The trace's calibration field showed our estimate ~45% below the
provider's real count (4,843 vs 7,474), so a 6,000 "cap" let 7,474 through.

**Counting.** Claude's tokenizer isn't published; local libraries are unverified for
current models, and newer Claude tokenizers produce ~30% more tokens than earlier ones
(Anthropic docs; independent measurements 1.45-1.47×). Anthropic's `count_tokens`
endpoint is the reference (free, rate-limited). Measured on our own texts with
claude-sonnet-5-5: prose and tool schemas 2.58-3.00 chars/token, JSON data 2.04, and a
321-token hidden tool-use prompt whenever tools are offered.
**Decision:** a calibrated offline estimate: text 2.4, JSON data 1.9, +350 per request
with tools, each ~7% below the lowest measurement so it never undercounts; recorded
samples (`make calibrate`) and a test keep it honest; traces keep showing drift.
**Rejected:** counting live before every request (exact, but latency, rate limits, no
offline evals, Anthropic-only); a blunt ×1.6 factor (still a guess, wastes budget);
`tiktoken` (exact only for OpenAI models; not used here yet, so deferred).

**Sizing the budget.** No source names a "right" budget for a task agent; the guidance
is "the smallest set of high-signal tokens" (Anthropic), and degradation with input
length shows up well before the window is full (Chroma), but published thresholds
(e.g. context editing's 100,000-token default) are far above our scale. So the budget is
the smallest that fits a realistic worst-case request, measured in real tokens:

| Component | Real tokens |
|---|---|
| tool schemas + hidden tool-use prompt | ~3,030 |
| system prompt | 542 |
| state | ~100-200 |
| memories (cap) | ≤ 600 |
| working room: worst realistic run (reschedule: bookings 1,002 + slots 340 + calls) | ~1,500 |
| history: a 10-exchange errand session (live chat: avg 86, max 400 per message) | ~1,700 |
| **total** | **≈ 7,500** |

**How we compare with real conversations:** general chat averages 2-2.5 turns
(LMSYS-Chat-1M, WildChat); tool-using customer-service tasks 4-10 turns with 3-9 tool
calls (τ-bench); τ²-bench airline on claude-sonnet-5-5 averages $0.16 per task, in line
with our ~$0.15-0.30 per booking. Our conversation share is normal; our *fixed* share
(~3,700 per request) is heavy next to lean setups (600-1,500 input per call in vendor
cost models).

**Decision:** budget 7,500 now (the measured need), then slim the fixed part (trim tool
descriptions and parameters; `list_memories` returns booking-relevant facts plus a note
count, since its 2,326-token result can't fit the working room) and re-derive; it may
land near 6,000-6,500. TOON (tabular encoding for list-shaped tool results) measured
27-40% smaller than compact JSON on our data; to be adopted for the flat lists only
after a test shows the model still gets every id and slot it needs.

**Re-derived after slimming.** `list_memories` now sends booking facts and counts
personal notes (~3,470 -> ~780 estimated tokens per call). Tool definitions dropped from
2,719 to 2,310 real tokens (−409; 3,009 -> 2,508 estimated), via lean schemas for
optional parameters (a `null` branch cost ~40 tokens each) and tighter descriptions; a
test pins every tool's parameters so the contract can't change. That's less than the
~600-700 first predicted: enums and required fields are content, not boilerplate.
The budget is enforced in *estimated* tokens, which run ~7-15% above real by design,
so the need must be derived in the same units:

| Component | Estimated tokens |
|---|---|
| tool schemas + hidden tool-use prompt | 2,858 (was 3,359) |
| system prompt | 678 |
| state | ~200 |
| memories (cap) | 600 |
| working room | 1,500 |
| history, a 10-exchange session (1,700 real × 1.08) | ~1,840 |
| **need** | **≈ 7,680** |

**Decision: the budget stays 7,500.** The earlier "may land near 6,000-6,500" mixed real
and estimated units and was wrong. Slimming didn't make a smaller budget possible; it
gave the conversation room: history space within 7,500 rose from ~1,160 to ~1,660
estimated tokens (about 5 more messages per request).

**TOON: evaluated on a throwaway branch, adopted.** Decision criteria, set before
measuring: at least 25% fewer real tokens on the results, and the model picks the right
ids at least as reliably as with JSON.
- Tokens (`count_tokens`): the bookings list, 1,162 as compact JSON -> 748 as TOON
  (−36%). TOON measured 1.75 chars/token, *denser* than JSON (2.04): the calibration
  test caught that the JSON ratio would undercount it, so text tool results get their
  own ratio (1.6).
- Accuracy (live, claude-sonnet-5-5): five questions that require reading the right row
  and copying the exact id ("cancel my AC servicing", "same electrician as last
  time", "oldest plumbing booking", ...), two runs each: TOON 10/10, JSON 9/10 (its miss
  answered with a list instead of the id); input tokens per question ~1,058 -> ~725
  (−31%). A small sample of simple lookups: enough to rule out a reading penalty, not
  to claim TOON reads better.
- Scope: `find_my_bookings` and `list_memories` (flat lists); search options stay JSON
  (nested slot lists are TOON's weak spot). The handler runs the agent with TOON; unit
  tests that build deps directly stay on JSON for readable assertions.

**Sources:** [Anthropic token counting](https://docs.anthropic.com/en/docs/build-with-claude/token-counting) ·
[Anthropic tool use pricing and tokens](https://docs.anthropic.com/claude/docs/tool-use-pricing-and-tokens) ·
[Claude context editing](https://docs.claude.com/en/docs/build-with-claude/context-editing) ·
[Anthropic context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) ·
[Chroma "Context Rot" (summary)](https://www.zenml.io/llmops-database/context-rot-evaluating-llm-performance-degradation-with-increasing-input-tokens) ·
[LMSYS-Chat-1M](https://arxiv.org/pdf/2309.11998v2) ·
[WildChat](https://arxiv.org/pdf/2405.01470) ·
[τ-bench](https://arxiv.org/abs/2406.12045?context=cs) ·
[τ²-bench airline leaderboard](https://openrouter.ai/benchmarks/tau2-bench-airline) ·
[OpenAI function calling guide](https://developers.openai.com/docs/guides/function-calling) ·
[TOON (telekom)](https://github.com/telekom/toon) ·
[TOON critical analysis](https://dev.to/ikaganacar/toon-benchmarks-a-critical-analysis-of-different-results-5h66)

## D4. Web UI: SSE with streamed replies; chat beside a tabbed side panel (2026-11-06)

**Status:** accepted; Phase 6.

**Layout.** Chat on the left; a tabbed side panel (Trace, Memory, Bookings, Demo) on the
right. The demo needs chat and trace side by side ("show the agent's chat and its trace
side by side"); memory, bookings and demo controls stay one click away. Trace events are
one line each (icon, type, key fact) and expand to their sanitized JSON on click; runs
are collapsible under a header with outcome, calls, tokens, cost and time.
**Rejected:** two fixed panes with a drawer (memory hidden during the demo); three
columns (cramped below ~1,400px, which matters for a screen recording).

**Transport: SSE, not WebSockets.** `POST /sessions/{id}/messages` answers with an event
stream (progress lines, text deltas, trace events, then the final reply with its
code-drawn cards); `GET /events` pushes what arrives unasked (Phase 7's "your provider
cancelled"). It's how the model providers stream to us and the common pattern for LLM
chat UIs; each turn stays an ordinary request (fits the per-session lock, curl-able,
easy to test); browsers reconnect `EventSource` on their own; a Stop button works by
aborting the request. **Rejected:** WebSockets, which earn their keep with typing
indicators, presence and many users, none of which we have; switching later only
touches the transport, since the event types stay the same.

**Streaming replies.** A turn is several model calls (think, call a tool, read the
result, answer) and only the last produces visible text, so: the answer streams as it
is written, and the tool phases show progress lines drawn from the trace ("Searching
plumbers…"). Approval cards, booking confirmations and suggestions stay code-written
from verified data, after the text; a typed or clicked "yes" never touches the model.
Caveat: streamed text can't be taken back, so a turn that later hits a limit shows its
partial text followed by the honest code-written limit message.

**Demo controls.** A dev-only tab (`ERRAND_DEMO_MODE=true`) that triggers failures on
the mock world through its control plane (timeout after success, slot taken, provider
cancels, price change at hold), so the assignment's "failure handled live" can be shown
in one recording without restarts.

## D5. Reply latency: low effort, prompt caching, and a visible wait (2026-11-06)

**Status:** accepted.

**Problem.** In the web chat a "hello" took 3.5–8.6 s before anything appeared, then the
whole reply at once (BUGS.md, BUG-10 and the latency row). The trace showed one Sonnet 5.5
call with ~3.4K tokens in and ~50 out, so the time was inside the model call. Streaming
worked end to end (the browser received separate `text` events); a ~50-token reply is
generated in about half a second, so its chunks land in one burst.

**What we learned.** Sonnet 5.5 thinks adaptively by default at `high` effort, and
thinking can't be switched off (`disabled` is a 400; the lowest settings are
`between_tools`, or adaptive at `low` effort, which the Anthropic docs recommend trying
first for latency). Thinking text is hidden but billed as output tokens: the same
~50-token "hello" reply came with 53–105 output tokens. Nothing was prompt-cached, so the
same ~3.6K tokens of tools and instructions were processed fresh on every model call,
and an errand turn makes several.

**Measured** with `make latency` (real handler, prompt, tools and seeded memories; three
"hello" turns, then a plumber search and a booking request in one session), one pass
per setting, 2026-11-06:

| Setting | hello: first word / cost | Search: first word, calls / cost | Booking: total, calls / cost | Pass cost |
|---|---|---|---|---|
| default (`high`), no cache | 0.9–1.15 s / $0.0073–0.0078 | 13.4 s, 6 / $0.044 | 10.1 s, 4 / $0.035 | $0.10 |
| `low` effort | 0.96–1.32 s / $0.0075–0.0078 | 6.1 s, 3 / $0.028 | 7.1 s, 3 / $0.029 | $0.08 |
| `low` effort + cache | 0.98–1.31 s / $0.0013–0.0014 | 6.7 s, 3 / $0.0096 | 6.2 s, 3 / $0.013 | $0.035 |

- **Low effort is the latency win:** errand turns about halved, mostly through fewer
  model calls (less thinking between tools: search output 1,780 -> 528 tokens).
- **Caching is the cost win:** ~98% of a repeat call's input came from the cache
  (3,305 of 3,383 tokens); cost per turn fell 55–83%. At our prompt size it made no
  measurable latency difference: 3.4K tokens of input are processed quickly anyway.
- **Same behaviour:** the search updated the requirements and searched; the booking
  request ended at the approval card ("It isn't booked until you approve it").
- "hello" stayed at ~1 s to the first word in every setting. The 3.5–8.6 s seen in the
  browser were the same call on a slower moment of the API, which we don't control.

**Decision.** `ERRAND_EFFORT=low` and `ERRAND_PROMPT_CACHE=true` by default, on every
Anthropic request (orchestrator and review reader). Caching marks two breakpoints, after
the tool definitions (identical every turn) and after the instructions; the API reuses
the longest cached prefix it finds. Trace `model_call` events now record cache reads and
writes, and cost prices them at their own rates (reads 0.1x input, 5-minute writes
1.25x), so the trace's cost stays honest. The UI shows the wait (BUG-10): typing dots
until the first word, a caret while the reply streams, and a steady reveal of bursty
chunks.

**Limits of the evidence.** One pass per setting, on one day: enough to see a halving
and an 80% cost drop, not a precise p95. A cache-only pass was skipped to save cost (its
cost effect is visible in the third row; at this prompt size it shouldn't move latency).
`low` effort could matter on harder turns than these two; the live smoke run planned for
Phase 8 exercises the real model on more flows, and either setting can be turned back
(`ERRAND_EFFORT=high`, `ERRAND_PROMPT_CACHE=false`).
**Rejected:** `between_tools` (the lowest thinking setting): `low` already removed most
thinking and keeps per-turn effort adjustable; a smaller model for the orchestrator
(Haiku): untested on multi-step tool use here, and the latency came from thinking and
call count, not model speed.

## D6. Proactive follow-up: code detects and decides, the agent finds a replacement (2026-11-06)

**Status:** accepted; Phase 7.

**Requirement.** "When a booked provider cancels (simulated event), the agent must detect
it without a user prompt, decide whether and how to notify the user, find a replacement
within known preferences, and get fresh approval before rebooking."

**Detect: a monitor polling the contract.** `ProviderMonitor` reads each user's bookings
with `list_bookings` every 10 s (`ERRAND_MONITOR_INTERVAL_SECONDS`). Only the provider
is simulated (the Demo tab's "Provider cancels", `MockControl` in tests); the monitor
never reads the mock's internal event table, which holds the provider's reason, because
a real provider wouldn't give us its database. So the notice can say what happened but
not why, and a real provider would need no change here. **Rejected:** a side channel
from the mock (a shortcut that works only because it's a mock); webhooks (the contract
has none; if a provider offered one, it would replace only the polling).

**Once only.** Each cancellation is claimed in `provider_notices` in the same transaction
as the notice to the user: never handled twice (restarts, overlapping passes: the
primary key refuses the second) and never claimed without its notice.

**Decide: code rules, traced with their reason** (`notice_decision`):

| Situation | Decision |
|---|---|
| the slot has already passed | `past`: recorded, nobody told |
| another confirmed booking for the same service within a day | `notify_only`: told which booking covers it, no search |
| the user's own cancellation (`cancelled`) | not a provider event: ignored |
| otherwise | `replace`: told, and a replacement is looked for |

How: always saved in the conversation (the session the booking was made in, else the
latest), and pushed live to an open chat (`message` on `/api/events`, with a toast).
**Rejected:** letting the model decide whether to notify: these rules are the
testable part, and a wrong guess here either spams or stays silent.

**Find a replacement: the agent.** For `replace`, the handler runs the model on a
code-written prompt (what was cancelled; same service, close to that time, within saved
preferences; propose it). It uses the same tools as a user turn, so it ends at an
approval card: fresh approval is enforced in code, and nothing is rebooked until a yes
or a click. The prompt is not saved as a user message and doesn't count as the user's
words (the run's provenance text is empty, so nothing can be remembered from it). The
run takes the session's lock, so it waits for a turn in progress. Without a model, a
code-written fallback says so; on limits or errors, the usual honest outcome is saved.
**Rejected:** a code-only replacement (rank and propose the top option without the
model): cheaper and more predictable, but the assignment asks for a real LLM driving
the tool calls, and the model path already carries preferences, explanation and limits.

**Seen later.** A card proposed while the user was away comes back with the history
(`GET /sessions/{id}/pending`), drawn under the latest reply.

**Optional rule not taken:** "slot less than 2 hours away: notify only". Easy to add to
the decision table if same-day replacements prove unrealistic.

## D7. Evals: deterministic scenarios gate every run; the real model only on demand (2026-11-06)

**Status:** accepted; Phase 8.

**Practice we checked.** Agent projects commonly run two lanes: deterministic tests
(no model calls, no key, seconds) on every commit, and evals that call a real model or
an LLM judge on merge, nightly or on demand, kept out of the default run with a marker
and read as trends rather than hard gates; hard invariants gate, quality signals are
watched (PydanticAI's testing docs; Arize, "Evals in CI"; Autonoma's CI guide).

**Ours.** The 25 scenarios use a scripted model: deterministic, offline and fast, and
what they assert are hard invariants ("no booking without valid approval"). By that
practice they belong in the every-run lane, so `make test` runs them. `make evals` runs
the same scenarios and writes `evals/results.md`; ordinary test runs don't touch it, so
the repo doesn't change on every run. The real model's judgment is checked by
`make smoke` (one conversation, ~$0.05, on demand).

**What scripted scenarios can and can't show.** They can't measure the model; they show
the code keeps its guarantees whatever the model does, so several make the model
misbehave on purpose (lie about a booking, invent a slot, obey an injected review). That
found BUG-12: a false "booked" in the model's text reached the user.

**Skipped:** the large generated mock world (Phase 8's last item, our own idea for scale
tests). None of the assignment's requirements needs it, and the hand-written 30-provider
world covers every eval and the demo.
