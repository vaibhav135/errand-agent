# Design decisions

Decisions that changed or extended the design, with the reasoning and what was rejected.
`ARCHITECTURE.md` describes how the code works today; this file records *why* it is
shaped that way when the choice wasn't obvious. Newest last.

## D1. Context: session-scoped history, a budget that counts everything (2026-11-06)

**Status:** accepted; implemented by the BUG-3 fix.

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
assignment says unconfirmed inferred facts "must never be included". `list_memories`
will report only that a suggestion awaits the user's answer (its key and scope, not its
value); the user confirms or rejects it through code, the same pattern as approvals.

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
