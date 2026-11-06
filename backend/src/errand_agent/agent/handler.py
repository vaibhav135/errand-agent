"""Message handler: one user message in, one truthful reply out.

What: `MessageHandler.handle(session_id=, user_id=, text=)` -> `Reply` (a RunOutcome
      plus the run id). The single entry point the CLI and the web API call.

Why:  This is where "the LLM proposes, the workflow decides" is wired together. A
      typed yes or no is decided in code, without the model; everything else goes to
      the model, which can only propose. Every path, including limits and errors,
      ends in a RunOutcome whose message is saved and traced.

How:  1. Reconcile first: settle this session's parked side effects (unverified
         booking, pending cancel) so the run sees the provider's truth. A failed
         reconcile is logged and doesn't block the message.
      2. Save the user's message before the run, so it survives a crash and the
         profile tools can scrub PII out of it.
      3. Inside run_with_outcome (trace, budget meter, guarded env):
         - plain yes + exactly one pending card -> grant (typed) and execute the
           matching flow; the reply is the code-written outcome.
         - plain no + something pending -> decline it in code.
         - otherwise -> build the context packet, run the orchestrator with the
           packet as instructions + history (the current message goes once, as the
           prompt), and turn any approval card a tool produced into needs_approval.
      4. Save the reply with its run id. If code showed the user suggestions, the saved
         reply ends with a note saying so (the count only), because the model next
         turn sees only its saved text, not what code drew (BUG-8). Approval cards
         need no note: STATE reports any pending approval.
      The session must already exist (created by the CLI / API).
"""

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic_ai.models import Model

from errand_agent.agent.budget import tool_definition_tokens
from errand_agent.agent.deps import AgentDeps
from errand_agent.agent.loop import run_agent, to_history
from errand_agent.agent.orchestrator import build_orchestrator, orchestrator_tools
from errand_agent.agent.prompts import SYSTEM_PROMPT, instructions
from errand_agent.core.clock import Clock
from errand_agent.core.ids import IdGen
from errand_agent.memory.conversation import ConversationStore
from errand_agent.memory.profile import ProfileStore
from errand_agent.memory.store import Memory, MemoryStore
from errand_agent.memory.tokens import estimate_tokens
from errand_agent.providers.tool_env import ToolEnv
from errand_agent.tracing.recorder import Tracer
from errand_agent.workflow.actions import ProviderActionStore
from errand_agent.workflow.approvals import Approval, ApprovalStore, is_explicit_no
from errand_agent.workflow.booking import BookingFlow
from errand_agent.workflow.changes import ChangeFlow
from errand_agent.workflow.context import ContextBudget, ContextBuilder
from errand_agent.workflow.decline import decline_pending
from errand_agent.workflow.errands import ErrandStore
from errand_agent.workflow.guarded_env import RunBudget
from errand_agent.workflow.outcomes import (
    RunContext,
    RunOutcome,
    for_change,
    for_execution,
    run_with_outcome,
)
from errand_agent.workflow.reconciler import Reconciler

logger = logging.getLogger(__name__)

DECLINED = {
    "book": "OK, I won't book it. Want to look at the other options?",
    "cancel": "OK, I'll leave your booking as it is.",
    "reschedule": "OK, your booking stays at its current time.",
}


def shown_note(shown: Sequence[Memory]) -> str:
    """What code showed the user, recorded in the saved reply for the model's next turn
    (BUG-8): the count only, never the suggestions' content."""
    if not shown:
        return ""
    n = len(shown)
    return f"\n\n[The app showed the user {n} suggestion{'' if n == 1 else 's'} to review.]"


@dataclass(frozen=True)
class Reply:
    outcome: RunOutcome
    run_id: str
    suggestions: tuple[Memory, ...] = ()  # for code to show; the model never saw them


class MessageHandler:
    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        clock: Clock,
        ids: IdGen,
        env: ToolEnv,
        tracer: Tracer,
        model: Model,
        review_model: Model | None = None,
        budget: RunBudget | None = None,
        context_budget: ContextBudget | None = None,
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._ids = ids
        self._env = env
        self._tracer = tracer
        self._model = model
        self._review_model = review_model
        self._budget = budget or RunBudget()
        self._context_budget = context_budget or ContextBudget()
        self._errands = ErrandStore(conn, clock=clock, ids=ids)
        self._approvals = ApprovalStore(conn, clock=clock, ids=ids)
        self._actions = ProviderActionStore(conn, clock=clock)
        self._memories = MemoryStore(conn, clock=clock, ids=ids)
        self._profiles = ProfileStore(conn, clock=clock, ids=ids)
        self._conversation = ConversationStore(conn, clock=clock, ids=ids)
        self._context = ContextBuilder(
            clock=clock,
            errands=self._errands,
            approvals=self._approvals,
            memories=self._memories,
            profiles=self._profiles,
            conversation=self._conversation,
            budget=self._context_budget,
        )
        self._reconciler = Reconciler(
            conn=conn,
            env=env,
            errands=self._errands,
            approvals=self._approvals,
            actions=self._actions,
            clock=clock,
            tracer=tracer,
        )
        self._agent = build_orchestrator()
        self._tool_tokens = tool_definition_tokens(orchestrator_tools())  # sent every request

    async def handle(self, *, session_id: str, user_id: str, text: str) -> Reply:
        try:
            await self._reconciler.reconcile_once(session_id=session_id)
        except Exception:  # the background loop retries; don't block the user
            logger.exception("per-run reconcile failed")
        self._conversation.append(session_id, "user", text)

        run_id = ""
        shown: list[Memory] = []  # suggestions a tool asked code to show the user

        async def body(ctx: RunContext) -> RunOutcome:
            nonlocal run_id
            run_id = ctx.run.run_id
            return await self._respond(
                ctx, session_id=session_id, user_id=user_id, text=text, shown=shown
            )

        outcome = await run_with_outcome(
            self._tracer,
            session_id=session_id,
            trigger="user_message",
            budget=self._budget,
            env=self._env,
            clock=self._clock,
            errands=self._errands,
            body=body,
        )
        self._conversation.append(
            session_id, "assistant", outcome.message + shown_note(shown), run_id=run_id
        )
        return Reply(outcome=outcome, run_id=run_id, suggestions=tuple(shown))

    # --- the three paths -------------------------------------------------------------

    async def _respond(
        self, ctx: RunContext, *, session_id: str, user_id: str, text: str, shown: list[Memory]
    ) -> RunOutcome:
        approval_id = self._approvals.resolve_typed_yes(session_id, text)
        if approval_id is not None:
            return await self._confirm(ctx, approval_id)
        if is_explicit_no(text) and self._approvals.pending_for_session(session_id):
            return self._decline(ctx, session_id)
        return await self._converse(
            ctx, session_id=session_id, user_id=user_id, text=text, shown=shown
        )

    async def _confirm(self, ctx: RunContext, approval_id: str) -> RunOutcome:
        approval = self._approvals.get(approval_id)
        ctx.errand_id = approval.errand_id
        # A card's details never change (new details mean a new approval), so the
        # pending approval's fingerprint is exactly what the user was shown.
        self._approvals.grant(
            approval_id, presented_fingerprint=approval.fingerprint, via="typed", run=ctx.run
        )
        if approval.action == "book":
            result = await self._booking_flow(ctx).execute(approval.errand_id, approval_id)
            return for_execution(result, previous_price=approval.details.price_inr)
        changes = self._change_flow(ctx)
        if approval.action == "cancel":
            return for_change(await changes.execute_cancel(approval.errand_id, approval_id))
        return for_change(await changes.execute_reschedule(approval.errand_id, approval_id))

    def _decline(self, ctx: RunContext, session_id: str) -> RunOutcome:
        declined = decline_pending(
            self._conn, self._approvals, self._errands, session_id, run=ctx.run
        )
        actions = {a.action for a in declined}
        message = (
            DECLINED[actions.pop()] if len(actions) == 1 else "OK, I won't go ahead with those."
        )
        return RunOutcome(kind="answered", message=message)

    async def _converse(
        self, ctx: RunContext, *, session_id: str, user_id: str, text: str, shown: list[Memory]
    ) -> RunOutcome:
        errand = self._errands.latest_open(session_id)
        deps = AgentDeps(
            conn=self._conn,
            clock=self._clock,
            ids=self._ids,
            env=ctx.env,
            errands=self._errands,
            approvals=self._approvals,
            actions=self._actions,
            memories=self._memories,
            profiles=self._profiles,
            user_id=user_id,
            session_id=session_id,
            user_message=text,
            run=ctx.run,
            meter=ctx.meter,
            review_model=self._review_model,
            errand_id=errand.errand_id if errand else None,
        )
        ctx.errand_id = deps.errand_id
        packet = self._context.build(
            user_id=user_id,
            session_id=session_id,
            errand_id=deps.errand_id,
            system=SYSTEM_PROMPT,
            run=ctx.run,
            tool_tokens=self._tool_tokens,
        )
        messages = list(packet.messages)
        if messages and messages[-1] == ("user", text):
            messages.pop()  # the message being answered goes once, as the prompt
        text_instructions = instructions(packet)
        deps.context_limit = self._context_budget.total  # the guard caps every request
        deps.fixed_tokens = self._tool_tokens + estimate_tokens(text_instructions)
        try:
            reply = await run_agent(
                self._agent,
                deps,
                model=self._model,
                prompt=text,
                instructions=text_instructions,
                history=to_history(messages),
            )
        finally:
            ctx.errand_id = deps.errand_id  # so an error reports the errand honestly

        shown.extend(deps.suggestions)
        card = self._live_card(deps.cards)
        if card is not None:
            message = reply.strip() or "Please check the details on the card and approve."
            return RunOutcome(kind="needs_approval", message=message, approval=card)
        return RunOutcome(
            kind="answered", message=reply.strip() or "Sorry, could you say that again?"
        )

    # --- helpers ---------------------------------------------------------------------

    def _live_card(self, cards: list[Approval]) -> Approval | None:
        """The newest card from this run that is still pending (a later step may have
        superseded or declined an earlier one)."""
        for card in reversed(cards):
            current = self._approvals.get(card.approval_id)
            if current.status == "pending":
                return current
        return None

    def _flow_kwargs(self, ctx: RunContext) -> dict[str, object]:
        return {
            "conn": self._conn,
            "env": ctx.env,
            "errands": self._errands,
            "approvals": self._approvals,
            "actions": self._actions,
            "clock": self._clock,
            "ids": self._ids,
            "run": ctx.run,
            "meter": ctx.meter,
        }

    def _booking_flow(self, ctx: RunContext) -> BookingFlow:
        return BookingFlow(**self._flow_kwargs(ctx))  # type: ignore[arg-type]

    def _change_flow(self, ctx: RunContext) -> ChangeFlow:
        return ChangeFlow(**self._flow_kwargs(ctx))  # type: ignore[arg-type]
