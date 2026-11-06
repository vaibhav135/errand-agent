"""ProviderMonitor: notice a provider cancelling a booking, without being asked.

What: `ProviderMonitor.check_once()` reads each user's bookings through the contract
      (`list_bookings`) and handles every new `cancelled_by_provider` exactly once:
      decides whether to tell the user, says so in the right conversation, and hands
      the notice on (`on_notice`) so the app can look for a replacement (D6).
      `run_periodically()` repeats that in the background.

Why:  The assignment's proactive follow-up: "detect it without a user prompt, decide
      whether and how to notify the user, find a replacement within known preferences,
      and get fresh approval before rebooking". Detection only uses the contract: the
      mock's own event table (with the provider's reason) stays inside the mock, as a
      real provider's database would, so swapping in a real provider changes nothing
      here.

How:  - Users = everyone with a session. A pass that finds nothing new leaves no trace;
        each cancellation gets its own run (`trigger="provider_cancelled"`).
      - Once only: the cancellation is claimed in `provider_notices` in the same
        transaction as the notice, so it is never handled twice (restarts, overlapping
        passes: the primary key refuses the second) and never claimed without its notice.
      - Decision, in code, traced with its reason:
          slot already past                                  -> "past": recorded, nobody told
          another confirmed booking, same service, within a day -> "notify_only"
          otherwise                                          -> "replace"
      - Where: the user's most recent session, even if the booking was made in an older
        one, since that's the chat the user is in (BUG-13).
      - `on_notice` runs after the commit for "replace" and "notify_only". If it fails,
        that is logged: the user has already been told, and the claim stands.
"""

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from errand_agent.core.clock import Clock, utc_text
from errand_agent.memory.conversation import ConversationStore
from errand_agent.memory.selection import SERVICE_LABEL
from errand_agent.providers.errors import ProviderError
from errand_agent.providers.models import Booking
from errand_agent.providers.tool_env import ToolEnv
from errand_agent.store.db import transaction
from errand_agent.tracing.recorder import Tracer
from errand_agent.workflow.outcomes import when

logger = logging.getLogger(__name__)

MONITOR_INTERVAL_SECONDS = 10.0
NEARBY = timedelta(days=1)  # another booking this close already covers the service

Decision = Literal["replace", "notify_only", "past"]


class ProviderNotice(BaseModel):
    """One handled cancellation: what was decided, and what the user was told."""

    model_config = ConfigDict(frozen=True)

    booking: Booking  # the cancelled booking, as the provider reports it
    user_id: str
    session_id: str
    decision: Decision
    reason: str
    message: str | None  # what the user was told; None when nobody was
    run_id: str

    @property
    def booking_id(self) -> str:
        return self.booking.booking_id


class ProviderMonitor:
    def __init__(
        self,
        *,
        conn: sqlite3.Connection,
        env: ToolEnv,
        conversations: ConversationStore,
        clock: Clock,
        tracer: Tracer,
        on_notice: Callable[[ProviderNotice], Awaitable[None]] | None = None,
    ) -> None:
        self._conn = conn
        self._env = env
        self._conversations = conversations
        self._clock = clock
        self._tracer = tracer
        self._on_notice = on_notice

    # --- entry points --------------------------------------------------------------------

    async def run_periodically(
        self,
        interval_seconds: float = MONITOR_INTERVAL_SECONDS,
        *,
        max_passes: int | None = None,
    ) -> None:
        passes = 0
        while max_passes is None or passes < max_passes:
            try:
                await self.check_once()
            except Exception:  # keep the loop alive; the next pass looks again
                logger.exception("provider monitor pass failed")
            passes += 1
            if max_passes is None or passes < max_passes:
                await self._clock.sleep(interval_seconds)

    async def check_once(self) -> list[ProviderNotice]:
        notices: list[ProviderNotice] = []
        for user_id in self._users():
            try:
                bookings = await self._env.list_bookings(user_id)
            except ProviderError:  # unreachable this time: the next pass looks again
                logger.warning("list_bookings failed for %s; will retry", user_id)
                continue
            for booking in bookings:
                if booking.status != "cancelled_by_provider" or self._handled(booking):
                    continue
                notice = self._handle(user_id, booking, bookings)
                if notice is None:
                    continue  # another pass claimed it first
                notices.append(notice)
                if notice.decision != "past" and self._on_notice is not None:
                    try:
                        await self._on_notice(notice)
                    except Exception:
                        logger.exception("handing on the notice for %s failed", booking.booking_id)
        return notices

    # --- one cancellation ----------------------------------------------------------------

    def _handle(
        self, user_id: str, booking: Booking, bookings: list[Booking]
    ) -> ProviderNotice | None:
        decision, reason, other = self._decide(booking, bookings)
        session_id = self._session_for(user_id)
        message = _message(booking, decision, other)
        with self._tracer.start_run(session_id=session_id, trigger="provider_cancelled") as run:
            run.emit(
                "provider_cancellation",
                {
                    "booking_id": booking.booking_id,
                    "provider_id": booking.provider_id,
                    "provider_name": booking.provider_name,
                    "service": booking.service,
                    "slot": booking.slot.isoformat(),
                },
            )
            try:
                with transaction(self._conn):
                    self._conn.execute(
                        "INSERT INTO provider_notices"
                        " (booking_id, user_id, session_id, decision, reason, run_id, created_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            booking.booking_id,
                            user_id,
                            session_id,
                            decision,
                            reason,
                            run.run_id,
                            utc_text(self._clock.now()),
                        ),
                    )
                    if message is not None:
                        self._conversations.append(
                            session_id, "assistant", message, run_id=run.run_id
                        )
            except sqlite3.IntegrityError:
                run.finish(outcome="already_handled")
                return None
            run.emit(
                "notice_decision",
                {
                    "booking_id": booking.booking_id,
                    "decision": decision,
                    "reason": reason,
                    "told_user": message is not None,
                },
            )
            run.finish(outcome=f"provider_cancelled:{decision}")
        return ProviderNotice(
            booking=booking,
            user_id=user_id,
            session_id=session_id,
            decision=decision,
            reason=reason,
            message=message,
            run_id=run.run_id,
        )

    def _decide(
        self, booking: Booking, bookings: list[Booking]
    ) -> tuple[Decision, str, Booking | None]:
        if booking.slot <= self._clock.now():
            return "past", "the slot has already passed", None
        other = next(
            (
                b
                for b in bookings
                if b.booking_id != booking.booking_id
                and b.service == booking.service
                and b.status == "confirmed"
                and abs(b.slot - booking.slot) <= NEARBY
            ),
            None,
        )
        if other is not None:
            return "notify_only", f"{other.booking_id} already covers it", other
        return "replace", "upcoming, and no other booking covers it", None

    # --- lookups -------------------------------------------------------------------------

    def _users(self) -> list[str]:
        rows = self._conn.execute("SELECT DISTINCT user_id FROM sessions ORDER BY user_id")
        return [row["user_id"] for row in rows]

    def _handled(self, booking: Booking) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM provider_notices WHERE booking_id = ?", (booking.booking_id,)
        ).fetchone()
        return row is not None

    def _session_for(self, user_id: str) -> str:
        # The user's newest chat, wherever the booking was made: that's the one they're
        # in, and the web UI only listens to the open chat (BUG-13). The replacement run
        # loses nothing: its prompt carries the booking, and preferences come from memory.
        row = self._conn.execute(
            "SELECT session_id FROM sessions WHERE user_id = ?"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return row["session_id"]


def _message(booking: Booking, decision: Decision, other: Booking | None) -> str | None:
    if decision == "past":
        return None
    service = SERVICE_LABEL.get(booking.service, booking.service)
    said = f"{booking.provider_name} cancelled your {service} booking for {when(booking.slot)}."
    if other is not None:
        return (
            f"{said} You already have {other.provider_name} booked for {when(other.slot)},"
            " so I haven't looked for a replacement."
        )
    return f"{said} I'm looking for a replacement within your preferences."
