from datetime import date
from pathlib import Path
from typing import Any

import pytest

from errand_agent.agent.deps import AgentDeps
from errand_agent.agent.orchestrator import FORBIDDEN_TOOL_NAMES, build_orchestrator
from errand_agent.mocks.scenario import Scenario
from tests.unit.agent_script import ScriptedModel, call, deps_for, retries, say, tool_returns
from tests.unit.workflow_world import World

ASK = "Find a plumber in Indiranagar this week, mornings only, under ₹1,500."
FULL = {
    "service": "plumber",
    "area": "Indiranagar",
    "date_start": "2026-11-05",
    "date_end": "2026-11-08",
    "earliest_start": "08:00",
    "latest_start": "12:00",
    "budget_max_inr": 1500,
}


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path, Scenario())


async def run(world: World, deps: AgentDeps, *steps: Any) -> Any:
    script = ScriptedModel(*steps)
    result = await build_orchestrator().run(deps.user_message, deps=deps, model=script.model())
    return script, result.all_messages()


# --- what the model can and can't do ------------------------------------------------------


async def test_no_consequential_action_is_a_tool(world: World) -> None:
    script, _ = await run(world, deps_for(world, "hi"), [say("hello")])

    assert script.tools_offered  # the agent does have tools...
    assert not (script.tools_offered & FORBIDDEN_TOOL_NAMES)  # ...but none of these


# --- requirements ---------------------------------------------------------------------------


async def test_a_complete_request_needs_nothing_more(world: World) -> None:
    deps = deps_for(world, ASK)

    _, messages = await run(world, deps, [call("update_requirements", **FULL)], [say("ok")])

    [returned] = tool_returns(messages, "update_requirements")
    assert returned["still_needed"] == [] and returned["can_ask_once"] == []
    r = world.errands.get(deps.errand_id).requirements  # type: ignore[arg-type]
    assert (r.service, r.area, r.budget_max_inr) == ("plumber", "Indiranagar", 1500)


async def test_a_correction_keeps_everything_else(world: World) -> None:
    deps = deps_for(world, ASK)
    await run(world, deps, [call("update_requirements", **FULL)], [say("ok")])

    deps.user_message = "Actually make it HSR Layout."
    await run(world, deps, [call("update_requirements", area="HSR Layout")], [say("ok")])

    r = world.errands.get(deps.errand_id).requirements  # type: ignore[arg-type]
    assert r.area == "HSR Layout"
    assert (r.service, r.budget_max_inr, r.date_range.start) == (  # type: ignore[union-attr]
        "plumber",
        1500,
        date(2026, 11, 5),
    )


async def test_a_past_date_is_sent_back_to_the_model(world: World) -> None:
    deps = deps_for(world, "plumber on the 1st")

    _, messages = await run(
        world,
        deps,
        [call("update_requirements", service="plumber", date_start="2026-11-01")],
        [say("Which date this week?")],
    )

    assert "past" in retries(messages, "update_requirements")[0]
    assert (
        deps.errand_id is None or world.errands.get(deps.errand_id).requirements.date_range is None
    )


# --- search and propose -----------------------------------------------------------------------


async def test_search_needs_complete_requirements(world: World) -> None:
    deps = deps_for(world, "I need a plumber")

    _, messages = await run(
        world,
        deps,
        [call("update_requirements", service="plumber")],
        [call("search_and_compare")],
        [say("Which area?")],
    )

    assert "area" in retries(messages, "search_and_compare")[0]


async def searched(world: World) -> AgentDeps:
    deps = deps_for(world, ASK)
    await run(
        world,
        deps,
        [call("update_requirements", **FULL)],
        [call("search_and_compare")],
        [say("options")],
    )
    return deps


def first_slot(messages: list[Any]) -> str:
    return tool_returns(messages, "search_and_compare")[0]["options"][0]["slots"][0]


async def test_search_returns_ranked_options_and_remembers_them(world: World) -> None:
    deps = deps_for(world, ASK)

    _, messages = await run(
        world,
        deps,
        [call("update_requirements", **FULL)],
        [call("search_and_compare")],
        [say("here")],
    )

    [found] = tool_returns(messages, "search_and_compare")
    assert [o["name"] for o in found["options"]] == [
        "FlowRight Plumbers",
        "HomeFlow Plumbing",
        "QuickFix Plumbing",
    ]
    errand = world.errands.get(deps.errand_id)  # type: ignore[arg-type]
    assert errand.state == "comparing"
    assert world.errands.options_json(errand.errand_id) is not None


async def test_proposing_a_listed_slot_makes_an_approval_card(world: World) -> None:
    deps = await searched(world)
    deps.user_message = "The first one, earliest slot."

    _, messages = await run(
        world,
        deps,
        [call("search_and_compare")],
        lambda m: [call("propose_booking", option_number=1, slot=first_slot(m))],
        [say("Please approve.")],
    )

    [proposed] = tool_returns(messages, "propose_booking")
    assert proposed["status"] == "awaiting_user_approval"
    assert proposed["instruction"].startswith("Not booked yet")
    [card] = deps.cards
    assert card.status == "pending" and card.details.provider_name == "FlowRight Plumbers"
    assert world.errands.get(deps.errand_id).state == "awaiting_approval"  # type: ignore[arg-type]


async def test_an_invented_slot_is_refused_before_any_hold(world: World) -> None:
    deps = await searched(world)

    _, messages = await run(
        world,
        deps,
        [call("propose_booking", option_number=1, slot="2026-11-06T07:00+05:30")],
        [say("sorry")],
    )

    assert "listed slots" in retries(messages, "propose_booking")[0]
    assert world.mock_count("SELECT count(*) FROM holds") == 0
    assert deps.cards == []


# --- existing bookings -------------------------------------------------------------------------


async def test_find_my_bookings_and_start_a_cancel(world: World) -> None:
    deps = deps_for(world, "Cancel my AC servicing.")

    _, messages = await run(
        world,
        deps,
        [call("find_my_bookings")],
        [call("start_cancel", booking_id="bk_hist_coolbreeze")],
        [say("Approve the cancellation?")],
    )

    bookings = tool_returns(messages, "find_my_bookings")[0]
    assert {b["booking_id"] for b in bookings} >= {"bk_hist_coolbreeze", "bk_hist_sparks"}
    [card] = deps.cards
    assert card.details.action == "cancel" and card.details.booking_id == "bk_hist_coolbreeze"


async def test_someone_elses_booking_cannot_be_cancelled(world: World) -> None:
    world.mock_exec(
        "UPDATE bookings SET user_id = 'user_2' WHERE booking_id = 'bk_hist_coolbreeze'"
    )
    deps = deps_for(world, "cancel bk_hist_coolbreeze")

    _, messages = await run(
        world,
        deps,
        [call("start_cancel", booking_id="bk_hist_coolbreeze")],
        [say("hmm")],
    )

    assert tool_returns(messages, "start_cancel") == [{"status": "booking_not_found"}]
    assert deps.cards == []


# --- memory and profile -----------------------------------------------------------------------


async def test_remember_needs_the_users_own_words(world: World) -> None:
    deps = deps_for(world, "Please remember: never before 10am.")

    _, messages = await run(
        world,
        deps,
        [call("remember", key="earliest_start", value="10:00", quote="never before 10am")],
        [
            call(
                "remember",
                key="preferred_provider",
                scope="ac_servicing",
                value={"provider_id": "ac_chillzone", "name": "ChillZone"},
                quote="they always prefer ChillZone",
            )
        ],  # from a review, not the user
        [say("Saved your start time.")],
    )

    assert len(tool_returns(messages, "remember")) == 1
    assert "copied exactly" in retries(messages, "remember")[0]
    keys = {m.key for m in deps.memories.list_for_user("user_1")}
    assert keys == {"earliest_start"}


async def test_memory_tools_only_touch_the_users_own_memories(world: World) -> None:
    deps = deps_for(world, "forget that")
    other = deps.memories.remember(
        "user_2", "earliest_start", "09:00", quote="9am", user_message="9am"
    )

    _, messages = await run(
        world,
        deps,
        [call("forget_memory", memory_id=other.memory_id)],
        [say("ok")],
    )

    assert retries(messages, "forget_memory")
    assert deps.memories.get(other.memory_id).status == "active"


async def test_profile_details_must_be_typed_by_the_user(world: World) -> None:
    deps = deps_for(world, "my number is 98450 12345")

    _, messages = await run(
        world,
        deps,
        [call("update_profile", phone="98765 43210")],
        [call("update_profile", phone="98450 12345")],
        [say("Saved.")],
    )

    assert retries(messages, "update_profile")
    assert tool_returns(messages, "update_profile") == [{"saved": ["phone"]}]  # names, no value
    assert deps.profiles.get("user_1").phone == "+919845012345"
