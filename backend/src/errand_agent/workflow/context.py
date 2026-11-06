"""Context builder: exactly what reaches the model each turn, within a fixed budget.

What: `ContextBuilder.build(...)` → a `ContextPacket` (system prompt, state, relevant
      memories, recent conversation) with per-section token counts, and a
      `context_sent` trace event recording what was included and what was left out.

Why:  The assignment: cap the model context at a fixed token budget; only relevant,
      current preferences may reach the model; forgotten facts and unconfirmed inferred
      facts never; log what context was sent each turn and prove it in tests. Our own
      rule adds: profile PII never reaches the model.

How:  The budget (ERRAND_CONTEXT_TOKEN_BUDGET) covers *everything* a request carries
      (docs/DECISIONS.md, D1). Filled in priority order:
      1. tool definitions (always; measured by the caller from the agent's schemas)
      2. system prompt (always)
      3. state (always): today's date in IST, the current errand (kind, state,
         requirements, what's still needed, what can be asked once, any pending
         approval), address labels and areas, and has-phone / has-email flags. This is
         the structured "memory" of the conversation, built from the DB, so older turns
         can be dropped without an LLM summary.
      4. relevant memories (`select_memories`), capped at ERRAND_MEMORY_TOKEN_BUDGET
      5. working room (reserved, never filled here): the run's own tool calls and
         results; the agent's context guard enforces the total during the run
      6. the *current session's* conversation, newest first while it fits, then put
         back in order. Earlier sessions are never sent: memories, state and tools carry
         continuity. `messages_out_of_scope` counts them, so the trace proves it.
      If 1-3 alone exceed the budget, ContextOverflow is raised rather than sending a
      truncated system prompt. Stored history has already been scrubbed of forgotten
      facts and saved PII (memory/scrub.py). Token counts use the deterministic,
      pessimistic estimate, so offline evals see exactly the numbers a live run would.
"""

from pydantic import BaseModel, ConfigDict

from errand_agent.core.clock import IST, Clock
from errand_agent.core.config import Settings
from errand_agent.memory.conversation import ConversationStore
from errand_agent.memory.profile import ProfileStore
from errand_agent.memory.selection import ExclusionReason, select_memories
from errand_agent.memory.store import MemoryStore
from errand_agent.memory.tokens import estimate_tokens
from errand_agent.tracing.recorder import RunTrace
from errand_agent.workflow.approvals import ApprovalStore
from errand_agent.workflow.errands import Errand, ErrandStore
from errand_agent.workflow.outcomes import when
from errand_agent.workflow.requirements import (
    Requirements,
    TimeWindow,
    missing_required,
    unasked_optional,
)

MESSAGE_OVERHEAD_TOKENS = 4  # role markers and separators per message


class ContextOverflow(Exception):
    """The tool definitions, system prompt and state alone don't fit the budget."""


class ContextBudget(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: int = 7500  # derived from measured needs: docs/DECISIONS.md, D3
    memories: int = 600
    # Kept free for the run's own tool calls and results (a search, then a proposal);
    # beyond it, the context guard masks older results rather than fail.
    working: int = 1500

    @classmethod
    def from_settings(cls, settings: Settings) -> "ContextBudget":
        return cls(total=settings.context_token_budget, memories=settings.memory_token_budget)


class ContextPacket(BaseModel):
    model_config = ConfigDict(frozen=True)

    system: str
    state: str
    memories: str
    messages: list[tuple[str, str]]  # (role, content), chronological
    tokens: dict[str, int]
    memory_ids: list[str]
    excluded_memories: list[tuple[str, ExclusionReason]]
    messages_dropped: int  # this session's messages that didn't fit
    messages_out_of_scope: int  # the user's messages in other sessions: never sent


class ContextBuilder:
    def __init__(
        self,
        *,
        clock: Clock,
        errands: ErrandStore,
        approvals: ApprovalStore,
        memories: MemoryStore,
        profiles: ProfileStore,
        conversation: ConversationStore,
        budget: ContextBudget,
    ) -> None:
        self._clock = clock
        self._errands = errands
        self._approvals = approvals
        self._memories = memories
        self._profiles = profiles
        self._conversation = conversation
        self._budget = budget

    def build(
        self,
        *,
        user_id: str,
        session_id: str,
        errand_id: str | None,
        system: str,
        run: RunTrace | None,
        tool_tokens: int = 0,
    ) -> ContextPacket:
        errand = self._errands.get(errand_id) if errand_id else None
        state = self._state(user_id, errand)
        base = tool_tokens + estimate_tokens(system) + estimate_tokens(state)
        if base > self._budget.total:
            raise ContextOverflow(
                f"tools + system + state need {base} tokens; budget {self._budget.total}"
            )

        service = errand.requirements.service if errand else None
        selection = select_memories(
            self._memories.all_for_user(user_id),
            service=service,
            budget_tokens=min(self._budget.memories, self._budget.total - base),
        )

        room = max(0, self._budget.total - base - selection.tokens - self._budget.working)
        history = list(reversed(self._conversation.for_session(session_id)))  # newest first
        kept: list[tuple[str, str]] = []
        used = 0
        for message in history:
            cost = estimate_tokens(message.content) + MESSAGE_OVERHEAD_TOKENS
            if used + cost > room:
                break
            kept.append((message.role, message.content))
            used += cost
        kept.reverse()

        tokens = {
            "tools": tool_tokens,
            "system": estimate_tokens(system),
            "state": estimate_tokens(state),
            "memories": selection.tokens,
            "messages": used,
        }
        tokens["total"] = sum(tokens.values())
        packet = ContextPacket(
            system=system,
            state=state,
            memories=selection.text,
            messages=kept,
            tokens=tokens,
            memory_ids=[m.memory_id for m in selection.included],
            excluded_memories=selection.excluded,
            messages_dropped=len(history) - len(kept),
            messages_out_of_scope=self._conversation.count_in_other_sessions(user_id, session_id),
        )
        if run is not None:
            run.emit(
                "context_sent",
                {
                    "budget": {
                        "total": self._budget.total,
                        "memories": self._budget.memories,
                        "working": self._budget.working,
                    },
                    "tokens": tokens,
                    "memory_ids": packet.memory_ids,
                    "excluded_memories": [
                        {"memory_id": mid, "reason": reason} for mid, reason in selection.excluded
                    ],
                    "messages_included": len(kept),
                    "messages_dropped": packet.messages_dropped,
                    "messages_out_of_scope": packet.messages_out_of_scope,
                    "state": state,
                    "memories": selection.text,
                },
            )
        return packet

    # --- the state section -------------------------------------------------------------

    def _state(self, user_id: str, errand: Errand | None) -> str:
        lines = [f"Today: {self._clock.now().astimezone(IST):%a %d %b %Y} (IST)"]
        if errand is None:
            lines.append("No active errand.")
        else:
            lines.append(f"Errand: {errand.kind}, state {errand.state}")
            lines.append(f"Requirements: {_requirements(errand.requirements)}")
            missing = missing_required(errand.requirements)
            if missing:
                lines.append(f"Still needed: {', '.join(missing)}")
            optional = unasked_optional(errand.requirements)
            if optional:
                lines.append(f"Can ask once: {', '.join(optional)}")
            approval = self._approvals.live_for_errand(errand.errand_id)
            if approval is not None and approval.status == "pending":
                d = approval.details
                what = "" if d.action == "book" else f"{d.action} "
                lines.append(
                    f"Pending approval: {what}{d.provider_name}, {when(d.slot)}, ₹{d.price_inr}"
                )

        view = self._profiles.model_view(user_id)
        addresses = "; ".join(
            f"{a['label']} ({a['area']}{', default' if a['default'] else ''})"
            for a in view.addresses
        )
        lines.append(f"Addresses: {addresses or 'none saved'}")
        lines.append(
            f"Contact on file: phone {'yes' if view.has_phone else 'no'},"
            f" email {'yes' if view.has_email else 'no'}"
        )
        return "\n".join(lines)


def _requirements(r: Requirements) -> str:
    parts: list[str] = []
    if r.service:
        parts.append(f"service: {r.service}")
    if r.area:
        parts.append(f"area: {r.area}")
    if r.date_range:
        parts.append(f"dates: {r.date_range.start} to {r.date_range.end}")
    if r.time_window == "any":
        parts.append("time window: any")
    elif isinstance(r.time_window, TimeWindow):
        start = f"{r.time_window.earliest:%H:%M}" if r.time_window.earliest else ""
        end = f"{r.time_window.latest:%H:%M}" if r.time_window.latest else ""
        parts.append(f"time window: {start}-{end}")
    if r.budget_max_inr == "any":
        parts.append("budget: any")
    elif r.budget_max_inr is not None:
        parts.append(f"budget: up to ₹{r.budget_max_inr}")
    if r.preferred_provider_id:
        parts.append(f"preferred provider: {r.preferred_provider_id}")
    return "; ".join(parts) or "none yet"
