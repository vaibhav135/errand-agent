"""Run outcomes: every exit path ends with a clear, truthful message for the user.

What: `RunOutcome` (kind + user-facing message + optional booking / approval card),
      mappers from workflow results (`for_execution`, `for_change`, `for_limit`,
      `for_error`), and `run_with_outcome()`, the wrapper every run goes through.

Why:  The assignment: "every exit path must produce a clear user-facing outcome" and
      "never claim success until verified". The facts in each message (booked or not,
      provider, slot, price) are written by code from verified results; the model may
      rephrase, but it can't turn `unverified` into "Booked!".

How:  - Messages are templates filled from verified data; slots are shown in IST.
        Unconfirmed outcomes never say it was booked; `unverified` and in-flight errors
        say we're checking and ask the user not to retry.
      - run_with_outcome() opens the run trace, builds the budget meter and the guarded
        tool env, runs the body, and converts *any* exit into a RunOutcome: the body's
        own result, LimitExceeded (resumable, nothing done in that step), or an
        unexpected exception (logged, with a message that reflects whether a side
        effect was in flight). It always finishes the trace with that outcome, so no
        run ends "unspecified".
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from errand_agent.core.clock import IST, Clock
from errand_agent.providers.models import Booking
from errand_agent.providers.tool_env import ToolEnv
from errand_agent.tracing.recorder import RunTrace, Tracer
from errand_agent.workflow.approvals import Approval
from errand_agent.workflow.booking import Execution
from errand_agent.workflow.changes import CANCEL_RETRY_WINDOW, ChangeResult
from errand_agent.workflow.errands import IN_FLIGHT, PARKED, ErrandStore
from errand_agent.workflow.guarded_env import BudgetMeter, GuardedToolEnv, LimitExceeded, RunBudget

logger = logging.getLogger(__name__)

OutcomeKind = Literal[
    "needs_info",
    "options_ready",
    "needs_approval",
    "answered",
    "booked",
    "needs_reapproval",
    "slot_gone",
    "slot_taken",
    "not_booked",
    "unverified",
    "cancelled",
    "cancel_pending",
    "cancel_failed",
    "rescheduled",
    "slot_conflict",
    "reschedule_failed",
    "booking_changed",
    "limit_reached",
    "error",
]


class RunOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: OutcomeKind
    message: str
    booking: Booking | None = None
    approval: Approval | None = None  # an approval card to show, if any


def when(slot: datetime) -> str:
    return f"{slot.astimezone(IST):%a %d %b, %H:%M}"


# --- workflow results -> outcomes ------------------------------------------------------------


def for_execution(result: Execution, *, previous_price: int | None = None) -> RunOutcome:
    b = result.booking
    match result.outcome:
        case "booked":
            assert b is not None
            return RunOutcome(
                kind="booked",
                booking=b,
                message=(
                    f"Booked {b.provider_name} for {when(b.slot)} at ₹{b.price_inr}."
                    f" Booking ID {b.booking_id}."
                ),
            )
        case "needs_reapproval":
            assert result.approval is not None
            d = result.approval.details
            was = f" from ₹{previous_price}" if previous_price is not None else ""
            return RunOutcome(
                kind="needs_reapproval",
                approval=result.approval,
                message=(
                    f"{d.provider_name}'s price for {when(d.slot)} changed{was} to"
                    f" ₹{d.price_inr}. Approve the new price if you'd like to go ahead."
                ),
            )
        case "slot_gone":
            return RunOutcome(
                kind="slot_gone",
                message="That slot is no longer available, so I didn't go ahead."
                " Want to choose another time?",
            )
        case "slot_taken":
            return RunOutcome(
                kind="slot_taken",
                message="Someone else took that slot just before I could confirm it."
                " Here are your other options.",
            )
        case "not_booked":
            return RunOutcome(
                kind="not_booked",
                message="It didn't go through: the provider has no booking for it after"
                " several tries, and nothing was charged. Want to pick another slot?",
            )
        case "unverified":
            return RunOutcome(
                kind="unverified",
                message="I couldn't confirm with the provider whether it went through."
                " I'm checking and will update you. Please don't book again meanwhile.",
            )


def for_change(result: ChangeResult) -> RunOutcome:
    b = result.booking
    match result.outcome:
        case "cancelled":
            assert b is not None
            return RunOutcome(
                kind="cancelled",
                booking=b,
                message=f"Cancelled your {b.provider_name} booking for {when(b.slot)}.",
            )
        case "cancel_pending":
            assert b is not None
            minutes = int(CANCEL_RETRY_WINDOW.total_seconds() // 60)
            return RunOutcome(
                kind="cancel_pending",
                booking=b,
                message=(
                    f"I couldn't reach {b.provider_name} to cancel. Your booking for"
                    f" {when(b.slot)} is still active. I'll keep trying for up to"
                    f" {minutes} minutes and let you know."
                ),
            )
        case "cancel_failed":
            if result.reason == "window_closed":
                text = (
                    "I couldn't reach the provider, and the visit is less than an hour away."
                    " Your booking is still active: please contact them directly."
                )
            elif result.reason == "booking_not_found":
                text = "The provider has no record of that booking."
            else:
                text = "The provider says this booking can't be cancelled anymore."
            return RunOutcome(kind="cancel_failed", booking=b, message=text)
        case "rescheduled":
            assert b is not None
            return RunOutcome(
                kind="rescheduled",
                booking=b,
                message=(
                    f"Done: moved your {b.provider_name} booking to {when(b.slot)}"
                    f" (same price, ₹{b.price_inr})."
                ),
            )
        case "slot_conflict":
            return RunOutcome(
                kind="slot_conflict",
                message="That time clashes with the provider's schedule."
                " Want to pick another time?",
            )
        case "reschedule_failed":
            return RunOutcome(
                kind="reschedule_failed",
                message="This booking can't be rescheduled anymore.",
            )
        case "booking_changed":
            return RunOutcome(
                kind="booking_changed",
                booking=b,
                message="That booking has changed since you approved, so I didn't go ahead."
                " Here are the current details.",
            )


def for_limit(exc: LimitExceeded) -> RunOutcome:
    what = {
        "tool_calls": "too many steps",
        "model_turns": "too many back-and-forths",
        "wall_clock": "it was taking too long",
        "context_tokens": "it needed more information at once than I can handle",
    }[exc.limit]
    return RunOutcome(
        kind="limit_reached",
        message=(
            f"I stopped this request because {what}. Nothing was booked or changed by the"
            ' step I stopped at, and your progress is saved: say "continue" to pick up'
            " where we left off."
        ),
    )


def for_error(exc: BaseException, *, errand_state: str | None) -> RunOutcome:
    if errand_state in IN_FLIGHT or errand_state in PARKED:
        doing = {
            "booking": "booking",
            "verifying": "booking",
            "unverified": "booking",
            "cancelling": "cancelling",
            "cancel_pending": "cancelling",
            "rescheduling": "rescheduling",
        }.get(errand_state or "", "working on it")
        return RunOutcome(
            kind="error",
            message=(
                f"Something went wrong on my side while {doing}. I'm checking with the"
                " provider whether it went through and will update you. Please don't"
                " retry it yourself."
            ),
        )
    return RunOutcome(
        kind="error",
        message="Something went wrong on my side. Nothing was booked or changed; please try again.",
    )


# --- the run wrapper -------------------------------------------------------------------------


@dataclass
class RunContext:
    run: RunTrace
    meter: BudgetMeter
    env: GuardedToolEnv
    errand_id: str | None = None  # set by the body so errors can report honestly


async def run_with_outcome(
    tracer: Tracer,
    *,
    session_id: str,
    trigger: str,
    budget: RunBudget,
    env: ToolEnv,
    clock: Clock,
    errands: ErrandStore,
    body: Callable[[RunContext], Awaitable[RunOutcome]],
) -> RunOutcome:
    with tracer.start_run(session_id=session_id, trigger=trigger) as run:
        meter = BudgetMeter(budget, clock=clock, run=run)
        ctx = RunContext(run=run, meter=meter, env=GuardedToolEnv(env, run=run, meter=meter))
        error: dict[str, str] | None = None
        try:
            outcome = await body(ctx)
        except LimitExceeded as exc:
            outcome = for_limit(exc)
        except Exception as exc:
            logger.exception("run failed")
            state = errands.get(ctx.errand_id).state if ctx.errand_id else None
            outcome = for_error(exc, errand_state=state)
            error = {"type": type(exc).__name__, "message": str(exc)}
        extra: dict[str, object] = {"message": outcome.message}
        if error is not None:
            extra["error"] = error
        run.finish(outcome=outcome.kind, **extra)
        return outcome
