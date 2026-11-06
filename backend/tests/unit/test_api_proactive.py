"""Proactive follow-up, wired into the app: a provider cancels, the open chat hears it."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from errand_agent.api.app import Services, build_services
from errand_agent.core.clock import IST, FakeClock
from errand_agent.core.config import Settings
from errand_agent.core.ids import SequentialIdGen
from errand_agent.memory.conversation import ConversationStore
from errand_agent.memory.seedgen import DEMO_USER_ID
from tests.unit.agent_script import ScriptedModel, call, say
from tests.unit.test_agent_tools import first_slot

COOLBREEZE = "bk_hist_coolbreeze"  # the seeded demo user's upcoming AC servicing


@pytest.fixture
async def services(app_settings: Settings, app_clock: FakeClock) -> AsyncIterator[Services]:
    s = build_services(app_settings, app_clock, SequentialIdGen())
    try:
        yield s
    finally:
        s.close()


def messages_pushed(services: Services) -> list[dict[str, Any]]:
    """Chat messages the hub pushes (what an open browser receives on /api/events)."""
    pushed: list[dict[str, Any]] = []

    def listen(name: str, data: dict[str, Any]) -> None:
        if name == "message":
            pushed.append(data)

    services.hub.subscribe(None, listen)
    return pushed


async def replacement_model(services: Services) -> ScriptedModel:
    """A scripted model that searches the cancelled booking's day and proposes the first slot."""
    bookings = await services.world.list_bookings(DEMO_USER_ID)
    (cancelled,) = [b for b in bookings if b.booking_id == COOLBREEZE]
    day = cancelled.slot.astimezone(IST).date().isoformat()
    return ScriptedModel(
        [
            call(
                "update_requirements",
                service="ac_servicing",
                area="Indiranagar",
                date_start=day,
                date_end=day,
                earliest_start="08:00",
                latest_start="12:00",
                budget_max_inr=2500,
            )
        ],
        [call("search_and_compare")],
        lambda m: [call("propose_booking", option_number=1, slot=first_slot(m))],
        [say("Here's a replacement. Approve it to book.")],
    )


async def test_a_cancellation_reaches_the_open_chat_with_a_fresh_card(services: Services) -> None:
    services.model = (await replacement_model(services)).model()
    pushed = messages_pushed(services)
    services.control.cancel_by_provider(COOLBREEZE, "technician unavailable")

    await services.monitor.check_once()

    notice, reply = pushed
    assert notice["content"].startswith("CoolBreeze AC Care cancelled your AC servicing")
    assert notice["reply"] is None
    assert reply["content"] == "Here's a replacement. Approve it to book."
    assert reply["reply"]["kind"] == "needs_approval" and reply["reply"]["approval"] is not None
    assert notice["session_id"] == reply["session_id"]


async def test_without_a_model_the_user_is_still_told(services: Services) -> None:
    pushed = messages_pushed(services)  # no model key in the test settings
    services.control.cancel_by_provider(COOLBREEZE, "technician unavailable")

    await services.monitor.check_once()

    notice, fallback = pushed
    assert "couldn't look for a replacement" in fallback["content"]
    saved = ConversationStore(services.agent_db, clock=services.clock, ids=services.ids)
    said = [m.content for m in saved.for_session(notice["session_id"])]
    assert said[-2:] == [notice["content"], fallback["content"]]


async def test_the_replacement_waits_for_a_turn_in_progress(
    services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    services.model = (await replacement_model(services)).model()
    pushed = messages_pushed(services)
    # Spy on the replacement run. Without the lock it starts right after the notice is
    # pushed, with nothing awaited in between, so "not started yet" can't be luck.
    handler = services.handler()
    started: list[str] = []
    run_replacement = handler.handle_provider_cancellation

    async def spy(notice: Any, **kwargs: Any) -> Any:
        started.append(notice.booking_id)
        return await run_replacement(notice, **kwargs)

    monkeypatch.setattr(handler, "handle_provider_cancellation", spy)
    services.control.cancel_by_provider(COOLBREEZE, "technician unavailable")
    session = services.agent_db.execute(
        "SELECT session_id FROM sessions WHERE user_id = ? ORDER BY created_at DESC, rowid DESC",
        (DEMO_USER_ID,),
    ).fetchone()["session_id"]
    lock = services.lock_for(session)
    await lock.acquire()  # the user's turn is running

    check = asyncio.create_task(services.monitor.check_once())
    for _ in range(1000):  # until the notice is out (bounded, so a bug can't hang)
        if pushed:
            break
        await asyncio.sleep(0)

    assert len(pushed) == 1  # the news at once, even mid-turn
    assert started == []  # but the replacement waits for the turn
    lock.release()
    await check
    assert started == [COOLBREEZE] and len(pushed) == 2
