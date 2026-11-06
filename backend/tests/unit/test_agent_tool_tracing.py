"""BUG-5: the agent's own tool calls are traced, and unconfirmed suggestions never reach
the model (docs/DECISIONS.md, D2)."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from errand_agent.agent.handler import MessageHandler, Reply
from errand_agent.memory.seed import seed_demo_user
from errand_agent.mocks.scenario import Scenario
from errand_agent.tracing.recorder import Tracer
from tests.unit.workflow_world import World

PHONE = "+919876543210"


class Seeing:
    """Plays the model from a script and keeps everything it was sent."""

    def __init__(self, *steps: list[Any]) -> None:
        self.steps = list(steps)
        self.received: list[str] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.received.append(repr(messages))
        return ModelResponse(parts=self.steps.pop(0))


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path, Scenario())


async def send(world: World, model: Seeing, text: str) -> Reply:
    handler = MessageHandler(
        conn=world.conn,
        clock=world.clock,
        ids=world.ids,
        env=world.mock,
        tracer=Tracer(world.clock, world.ids, subscribers=[world.events.append]),
        model=FunctionModel(model),
    )
    return await handler.handle(session_id="sess_1", user_id="user_1", text=text)


def agent_tools(world: World) -> list[dict[str, Any]]:
    return [e.to_dict() for e in world.events if e.type == "agent_tool"]


async def test_every_agent_tool_call_is_traced(world: World) -> None:
    model = Seeing(
        [ToolCallPart("list_memories", {})],
        [ToolCallPart("update_requirements", {"service": "plumber"})],
        [TextPart("Which area?")],
    )

    await send(world, model, "I need a plumber")

    calls = agent_tools(world)
    assert [c["tool"] for c in calls] == ["list_memories", "update_requirements"]
    assert calls[1]["args"] == {"service": "plumber"}
    assert all(c["status"] == "ok" and "latency_ms" in c for c in calls)


async def test_contact_details_are_traced_by_name_only(world: World) -> None:
    model = Seeing([ToolCallPart("update_profile", {"phone": PHONE})], [TextPart("Saved.")])

    await send(world, model, f"my number is {PHONE}")

    [call] = agent_tools(world)
    assert call["tool"] == "update_profile" and call["args"] == ["phone"]
    everything = json.dumps([e.to_dict() for e in world.events], default=str)
    assert "9876543210" not in everything


async def test_pending_suggestions_never_reach_the_model(world: World) -> None:
    seed_demo_user(world.conn, world.clock)  # pending: Sparks (electrician), CoolBreeze (AC)
    model = Seeing([ToolCallPart("list_memories", {})], [TextPart("Here's what I know.")])

    await send(world, model, "what do you remember about me?")

    seen = "\n".join(model.received)
    assert "Sparks" not in seen and "CoolBreeze" not in seen
    assert "awaiting_review" in seen  # it may know *that* suggestions wait, not what


async def test_show_suggestions_hands_them_to_code_not_the_model(world: World) -> None:
    seed_demo_user(world.conn, world.clock)
    model = Seeing([ToolCallPart("show_suggestions", {})], [TextPart("Shown below.")])

    reply = await send(world, model, "what have you noticed about me?")

    shown = {(m.scope, m.status) for m in reply.suggestions}
    assert shown == {
        ("electrician", "pending_confirmation"),
        ("ac_servicing", "pending_confirmation"),
    }
    seen = "\n".join(model.received)
    assert "Sparks" not in seen and "CoolBreeze" not in seen
    assert "'shown': 2" in seen or '"shown": 2' in seen  # only how many
