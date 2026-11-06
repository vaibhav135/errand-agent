"""BUG-3: the context budget must cap what each model request really carries."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.messages import ModelRequest
from pydantic_ai.models.function import AgentInfo, FunctionModel

from errand_agent.agent.handler import MessageHandler, Reply
from errand_agent.memory.tokens import estimate_tokens
from errand_agent.mocks.scenario import Scenario
from errand_agent.tracing.recorder import Tracer
from errand_agent.workflow.context import ContextBudget
from tests.unit.workflow_world import World


def part_text(part: Any) -> str:
    if isinstance(part, ToolCallPart):
        return part.tool_name + json.dumps(part.args_as_dict())
    content = getattr(part, "content", "")
    return content if isinstance(content, str) else json.dumps(content, default=str)


def sent_tokens(messages: list[ModelMessage], info: AgentInfo) -> int:
    """What the model receives, measured independently of the app: the instructions,
    every message part and the tool definitions."""
    instructions = [
        m.instructions for m in messages if isinstance(m, ModelRequest) and m.instructions
    ]
    tools = [
        {"name": t.name, "description": t.description, "parameters": t.parameters_json_schema}
        for t in info.function_tools
    ]
    text = "\n".join(
        [*instructions[-1:], *(part_text(p) for m in messages for p in m.parts), json.dumps(tools)]
    )
    return estimate_tokens(text)


class Measuring:
    """Plays the model from a script and measures every request it receives."""

    def __init__(self, *steps: list[Any]) -> None:
        self.steps = list(steps)
        self.sizes: list[int] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.sizes.append(sent_tokens(messages, info))
        return ModelResponse(parts=self.steps.pop(0))


def bookings_four_times() -> Measuring:
    lookup = [ToolCallPart("find_my_bookings", {})]
    return Measuring(lookup, lookup, lookup, lookup, [TextPart("Here they are.")])


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path, Scenario())


async def send(world: World, model: Measuring, total: int, text: str = "my bookings?") -> Reply:
    handler = MessageHandler(
        conn=world.conn,
        clock=world.clock,
        ids=world.ids,
        env=world.mock,
        tracer=Tracer(world.clock, world.ids, subscribers=[world.events.append]),
        model=FunctionModel(model),
        context_budget=ContextBudget(total=total, memories=600),
    )
    return await handler.handle(session_id="sess_1", user_id="user_1", text=text)


def events(world: World, kind: str) -> list[dict[str, Any]]:
    return [e.to_dict() for e in world.events if e.type == kind]


async def test_every_request_fits_the_budget(world: World) -> None:
    model = bookings_four_times()  # each lookup returns the whole booking history

    # A run starts at ~3,600 (tool schemas + hidden tool prompt + instructions) and each
    # lookup adds ~850 (as TOON): 6,000 holds two results but not four, so masking must
    # kick in.
    reply = await send(world, model, total=6000)

    assert reply.outcome.kind == "answered"
    assert len(model.sizes) == 5
    assert max(model.sizes) <= 6000, model.sizes


async def test_tool_definitions_are_counted(world: World) -> None:
    await send(world, Measuring([TextPart("Hello!")]), total=6000, text="hi")

    [sent] = events(world, "context_sent")
    assert sent["tokens"].get("tools", 0) > 1000  # 17 tool schemas


async def test_a_run_that_cannot_fit_ends_honestly(world: World) -> None:
    model = Measuring([ToolCallPart("find_my_bookings", {})], [TextPart("never sent")])

    # Room to start (~3,600 since the slimmer tool schemas) but not for one bookings
    # result (~820 as TOON).
    reply = await send(world, model, total=4000)

    assert reply.outcome.kind == "limit_reached"
    assert len(model.sizes) == 1  # the oversized second request was never made
    assert [e["limit"] for e in events(world, "limit_hit")] == ["context_tokens"]
