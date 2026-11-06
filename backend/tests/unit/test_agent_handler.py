from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from errand_agent.agent.handler import MessageHandler, Reply
from errand_agent.memory.conversation import ConversationStore
from errand_agent.mocks.scenario import Scenario
from errand_agent.providers.errors import ProviderTimeout
from errand_agent.tracing.recorder import Tracer
from errand_agent.workflow.guarded_env import RunBudget
from tests.unit.agent_script import ScriptedModel, call, say
from tests.unit.test_workflow_changes import COOLBREEZE
from tests.unit.workflow_world import ScriptedEnv, World


def no_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    raise AssertionError("the model must not be called for this message")


def handler(
    world: World, model: Any = no_model, *, env: Any = None, budget: RunBudget | None = None
) -> MessageHandler:
    return MessageHandler(
        conn=world.conn,
        clock=world.clock,
        ids=world.ids,
        env=env or world.mock,
        tracer=Tracer(world.clock, world.ids, subscribers=[world.events.append]),
        model=model if isinstance(model, FunctionModel) else FunctionModel(model),
        budget=budget or RunBudget(),
    )


async def send(h: MessageHandler, text: str) -> Reply:
    return await h.handle(session_id="sess_1", user_id="user_1", text=text)


async def pending_card(world: World) -> str:
    """A book errand awaiting approval of QuickFix's first slot; returns the errand id."""
    errand_id, option = await world.comparing_errand()
    proposal = await world.flow().propose(errand_id, option, option.slots[0])
    assert proposal.approval is not None
    return errand_id


def saved(world: World) -> list[tuple[str, str]]:
    store = ConversationStore(world.conn, clock=world.clock, ids=world.ids)
    return [(m.role, m.content) for m in store.for_session("sess_1")]


def bookings(world: World) -> int:
    return world.mock_count(
        "SELECT count(*) FROM bookings WHERE user_id = 'user_1' AND idempotency_key IS NOT NULL"
    )


def run_events(world: World, run_id: str, kind: str) -> list[dict[str, Any]]:
    return [
        e.to_dict() for e in world.events if e.type == kind and e.to_dict().get("run_id") == run_id
    ]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path, Scenario())


# --- confirming: code only, never the model -------------------------------------------------


@pytest.mark.parametrize("text", ["yes", "yup", "yesss", "haan ji", "theek hai", "👍"])
async def test_a_typed_yes_books_without_asking_the_model(world: World, text: str) -> None:
    errand_id = await pending_card(world)

    reply = await send(handler(world), text)

    assert reply.outcome.kind == "booked"
    assert world.errands.get(errand_id).state == "booked"
    assert bookings(world) == 1
    assert saved(world) == [("user", text), ("assistant", reply.outcome.message)]
    assert run_events(world, reply.run_id, "run_finished")[0]["outcome"] == "booked"


@pytest.mark.parametrize("text", ["yes but at 11am", "yes?", "absolutely, let's roll"])
async def test_anything_but_a_plain_yes_goes_to_the_model(world: World, text: str) -> None:
    errand_id = await pending_card(world)
    model = ScriptedModel([say("Tap Approve on the card, or reply yes.")])

    reply = await send(handler(world, model), text)

    assert reply.outcome.kind == "answered"
    assert world.errands.get(errand_id).state == "awaiting_approval"
    assert bookings(world) == 0


async def test_yes_with_two_pending_cards_is_ambiguous(world: World) -> None:
    await pending_card(world)
    cancel = world.errands.create("sess_1", "cancel", booking_id=COOLBREEZE)
    await world.change_flow().propose_cancel(cancel.errand_id)
    model = ScriptedModel([say("Which one do you mean?")])

    reply = await send(handler(world, model), "yes")

    assert reply.outcome.kind == "answered"
    assert bookings(world) == 0


async def test_a_second_yes_does_not_book_twice(world: World) -> None:
    await pending_card(world)
    h = handler(world, ScriptedModel([say("You're all set.")]))

    first = await send(h, "yes")
    second = await send(h, "yes")  # nothing pending now: an ordinary message

    assert (first.outcome.kind, second.outcome.kind) == ("booked", "answered")
    assert bookings(world) == 1


# --- declining: code for the clear cases, the model for the rest ------------------------------


async def test_a_typed_no_declines_in_code(world: World) -> None:
    errand_id = await pending_card(world)

    reply = await send(handler(world), "nah")

    assert reply.outcome.kind == "answered"
    assert world.errands.get(errand_id).state == "comparing"
    assert world.approvals.pending_for_session("sess_1") == []
    assert bookings(world) == 0


async def test_the_model_can_decline_a_fuzzy_no(world: World) -> None:
    errand_id = await pending_card(world)
    model = ScriptedModel([call("decline_pending")], [say("OK, I won't book it.")])

    reply = await send(handler(world, model), "neit, leave it")

    assert reply.outcome.kind == "answered"
    assert world.errands.get(errand_id).state == "comparing"
    assert bookings(world) == 0


async def test_declining_with_nothing_pending_changes_nothing(world: World) -> None:
    model = ScriptedModel([call("decline_pending")], [say("Nothing to decline.")])

    reply = await send(handler(world, model), "forget it")

    assert reply.outcome.kind == "answered"
    assert world.errands.open_errands("sess_1") == []


# --- the model path ---------------------------------------------------------------------------


async def test_a_card_from_the_model_turn_needs_approval(world: World) -> None:
    model = ScriptedModel(
        [call("start_cancel", booking_id=COOLBREEZE)], [say("Shall I cancel it?")]
    )

    reply = await send(handler(world, model), "Cancel my AC servicing")

    assert reply.outcome.kind == "needs_approval"
    assert reply.outcome.message == "Shall I cancel it?"
    card = reply.outcome.approval
    assert card is not None and card.details.booking_id == COOLBREEZE
    assert saved(world)[-1] == ("assistant", "Shall I cancel it?")
    assert run_events(world, reply.run_id, "context_sent")  # the model path logs its context


async def test_model_then_typed_yes_cancels(world: World) -> None:
    model = ScriptedModel([call("start_cancel", booking_id=COOLBREEZE)], [say("Confirm?")])
    h = handler(world, model)

    await send(h, "Cancel my AC servicing")
    reply = await send(h, "yes")

    assert reply.outcome.kind == "cancelled"
    assert run_events(world, reply.run_id, "context_sent") == []  # no model on this path


async def test_the_current_message_is_sent_once(world: World) -> None:
    seen: list[list[ModelMessage]] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(list(messages))
        return ModelResponse(parts=[TextPart("ok")])

    h = handler(world, model)
    await send(h, "Find me a plumber")
    await send(h, "in Indiranagar")

    prompts = [
        p.content
        for m in seen[-1]
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    ]
    assert prompts == ["Find me a plumber", "in Indiranagar"]


async def test_the_system_prompt_and_state_reach_the_model(world: World) -> None:
    seen: list[list[ModelMessage]] = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(list(messages))
        return ModelResponse(parts=[TextPart("ok")])

    await send(handler(world, model), "hi")

    instructions = next(getattr(m, "instructions", None) for m in seen[0])
    assert instructions is not None
    assert "never say" in instructions.lower()  # a rule from the system prompt
    assert "Today:" in instructions  # the state section


# --- every exit is an outcome -----------------------------------------------------------------


async def test_a_limit_ends_with_a_saved_honest_reply(world: World) -> None:
    model = ScriptedModel([ToolCallPart("list_memories", {})], [say("never sent")])

    reply = await send(handler(world, model, budget=RunBudget(max_model_turns=1)), "hi")

    assert reply.outcome.kind == "limit_reached"
    assert saved(world)[-1] == ("assistant", reply.outcome.message)
    assert run_events(world, reply.run_id, "run_finished")[0]["outcome"] == "limit_reached"


# --- reconcile first ----------------------------------------------------------------------


async def test_parked_work_is_settled_before_the_run(tmp_path: Path) -> None:
    world = World(tmp_path, Scenario(book_outcomes=["timeout_booked"]))
    env = ScriptedEnv(world.mock)
    errand_id, approval_id = await world.approved_errand(env)
    env.status_error = ProviderTimeout("status endpoint down")
    assert (await world.flow(env).execute(errand_id, approval_id)).outcome == "unverified"

    await send(handler(world, ScriptedModel([say("hello")])), "hi")  # the endpoint is back

    assert world.errands.get(errand_id).state == "booked"
    assert bookings(world) == 1
    reconcile_runs = [
        e for e in world.events if e.type == "run_started" and e.to_dict()["trigger"] == "reconcile"
    ]
    assert len(reconcile_runs) == 1
