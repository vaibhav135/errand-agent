import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel
from pydantic_ai.usage import RequestUsage

from errand_agent.agent.loop import run_agent, to_history
from errand_agent.agent.orchestrator import build_orchestrator
from errand_agent.mocks.scenario import Scenario
from errand_agent.workflow.guarded_env import LimitExceeded, RunBudget
from tests.unit.agent_script import deps_for
from tests.unit.workflow_world import World


class Recorder:
    """Scripted model that reports token usage and records what it was sent."""

    def __init__(self, *steps: list[Any]) -> None:
        self.steps = list(steps)
        self.seen: list[list[ModelMessage]] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.seen.append(list(messages))
        return ModelResponse(
            parts=self.steps.pop(0), usage=RequestUsage(input_tokens=120, output_tokens=30)
        )


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path, Scenario())


def events(world: World, kind: str) -> list[dict[str, Any]]:
    return [e.to_dict() for e in world.events if e.type == kind]


async def test_every_model_request_is_charged_and_traced(world: World) -> None:
    deps = deps_for(world, "what do you remember?")
    model = Recorder([ToolCallPart("list_memories", {})], [TextPart("Nothing yet.")])

    text = await run_agent(
        build_orchestrator(),
        deps,
        model=FunctionModel(model),
        prompt=deps.user_message,
        instructions="Be helpful.",
        history=[],
    )

    assert text == "Nothing yet."
    assert deps.meter is not None and deps.meter.model_turns == 2
    calls = events(world, "model_call")
    assert len(calls) == 2
    assert all((c["input_tokens"], c["output_tokens"]) == (120, 30) for c in calls)
    assert all("latency_ms" in c and c["cost_usd"] is None for c in calls)  # unpriced model


async def test_turn_budget_stops_before_the_extra_request(world: World) -> None:
    deps = deps_for(world, "loop", budget=RunBudget(max_model_turns=1))
    model = Recorder([ToolCallPart("list_memories", {})], [TextPart("never sent")])

    with pytest.raises(LimitExceeded) as exc:
        await run_agent(
            build_orchestrator(),
            deps,
            model=FunctionModel(model),
            prompt=deps.user_message,
            instructions="x",
            history=[],
        )

    assert exc.value.limit == "model_turns"
    assert len(model.seen) == 1  # the second request was never made
    assert events(world, "limit_hit")[0]["limit"] == "model_turns"


async def test_instructions_and_history_reach_the_model(world: World) -> None:
    deps = deps_for(world, "and the price?")
    model = Recorder([TextPart("₹699.")])
    history = to_history([("user", "Find me a plumber"), ("assistant", "Which area?")])

    await run_agent(
        build_orchestrator(),
        deps,
        model=FunctionModel(model),
        prompt=deps.user_message,
        instructions="STATE: comparing",
        history=history,
    )

    sent = model.seen[0]
    prompts = [
        p.content
        for m in sent
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    ]
    assert prompts == ["Find me a plumber", "and the price?"]
    assert any(
        isinstance(m, ModelResponse) and m.parts[0].content == "Which area?"  # type: ignore[union-attr]
        for m in sent
    )
    assert any(getattr(m, "instructions", None) == "STATE: comparing" for m in sent)


def test_to_history_maps_roles() -> None:
    history = to_history([("user", "hi"), ("assistant", "hello")])

    assert isinstance(history[0], ModelRequest) and isinstance(history[1], ModelResponse)


async def test_review_reader_calls_are_traced_too(world: World) -> None:
    deps = deps_for(world, "reviews?")

    def reader(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"themes": ["punctual"], "red_flags": [], "sentiment": "positive"},
                )
            ],
            usage=RequestUsage(input_tokens=900, output_tokens=20),
        )

    deps.review_model = FunctionModel(reader)
    model = Recorder(
        [ToolCallPart("get_reviews_summary", {"provider_id": "plm_quickfix"})],
        [TextPart("They're punctual.")],
    )

    await run_agent(
        build_orchestrator(),
        deps,
        model=FunctionModel(model),
        prompt=deps.user_message,
        instructions="x",
        history=[],
    )

    reader_calls = [c for c in events(world, "model_call") if c.get("agent") == "review-reader"]
    assert [(c["input_tokens"], c["output_tokens"]) for c in reader_calls] == [(900, 20)]


# --- streaming (D4) ---------------------------------------------------------------------


class Streaming:
    """A scripted model that streams: each step is text chunks or one tool call."""

    def __init__(self, *steps: list[str] | tuple[str, dict[str, Any]]) -> None:
        self.steps = list(steps)

    async def __call__(
        self, messages: list[ModelMessage], info: AgentInfo
    ) -> AsyncIterator[str | DeltaToolCalls]:
        step = self.steps.pop(0)
        if isinstance(step, tuple):  # a tool call
            name, args = step
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(args))}
        else:
            for chunk in step:
                yield chunk


async def test_streamed_text_arrives_in_chunks_and_the_reply_is_unchanged(world: World) -> None:
    deps = deps_for(world, "hi")
    chunks: list[str] = []

    async def on_text(chunk: str) -> None:
        chunks.append(chunk)

    text = await run_agent(
        build_orchestrator(),
        deps,
        model=FunctionModel(stream_function=Streaming(["Hel", "lo ", "there"])),
        prompt="hi",
        instructions="x",
        history=[],
        on_text=on_text,
    )

    assert chunks == ["Hel", "lo ", "there"]
    assert text == "Hello there"


async def test_tools_are_announced_before_they_run_and_still_budgeted(world: World) -> None:
    deps = deps_for(world, "what do you remember?")
    order: list[str] = []

    async def on_text(chunk: str) -> None:
        order.append(f"text:{chunk}")

    async def on_tool(name: str) -> None:
        order.append(f"tool:{name}")

    deps.on_tool = on_tool
    await run_agent(
        build_orchestrator(),
        deps,
        model=FunctionModel(stream_function=Streaming(("list_memories", {}), ["Nothing yet."])),
        prompt=deps.user_message,
        instructions="x",
        history=[],
        on_text=on_text,
    )

    assert order == ["tool:list_memories", "text:Nothing yet."]
    assert deps.meter is not None and deps.meter.model_turns == 2  # each request charged
    assert len(events(world, "model_call")) == 2  # and traced
