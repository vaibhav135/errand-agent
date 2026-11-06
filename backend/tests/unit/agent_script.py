"""Test helpers: a scripted stand-in for the LLM, and agent deps over the test world."""

from collections.abc import Callable, Sequence
from typing import Any

from pydantic_ai import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.messages import ModelRequest, RetryPromptPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from errand_agent.agent.deps import AgentDeps
from errand_agent.memory.profile import ProfileStore
from errand_agent.memory.store import MemoryStore
from errand_agent.workflow.guarded_env import BudgetMeter, GuardedToolEnv, RunBudget
from tests.unit.workflow_world import World

Step = Sequence[ToolCallPart | TextPart] | Callable[[list[ModelMessage]], Sequence[Any]]


def call(tool: str, **args: Any) -> ToolCallPart:
    return ToolCallPart(tool, args)


def say(text: str) -> TextPart:
    return TextPart(text)


class ScriptedModel:
    """Plays the model: returns the next scripted step each time it's asked.

    A step is a list of parts, or a function of the messages so far (to use a tool's
    result, e.g. "propose the first slot search returned"). `tools_offered` records
    which tool names the agent exposed to the model.
    """

    def __init__(self, *steps: Step) -> None:
        self.steps = list(steps)
        self.tools_offered: set[str] = set()

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.tools_offered = {t.name for t in info.function_tools}
        step = self.steps.pop(0)
        parts = step(messages) if callable(step) else step
        return ModelResponse(parts=list(parts))

    def model(self) -> FunctionModel:
        return FunctionModel(self)


def _parts(messages: list[ModelMessage], tool: str) -> list[Any]:
    return [
        p
        for m in messages
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, ToolReturnPart | RetryPromptPart) and p.tool_name == tool
    ]


def _retried(part: Any) -> bool:
    # PydanticAI 2.66 records a tool's ModelRetry as a ToolReturnPart with
    # outcome="retried" (older versions used RetryPromptPart); handle both.
    return isinstance(part, RetryPromptPart) or getattr(part, "outcome", None) == "retried"


def tool_returns(messages: list[ModelMessage], tool: str) -> list[Any]:
    """Successful results of `tool` (retries excluded)."""
    return [p.content for p in _parts(messages, tool) if not _retried(p)]


def retries(messages: list[ModelMessage], tool: str) -> list[str]:
    """Messages the workflow sent back to the model via ModelRetry for `tool`."""
    return [str(p.content) for p in _parts(messages, tool) if _retried(p)]


def deps_for(world: World, user_message: str, budget: RunBudget | None = None) -> AgentDeps:
    meter = BudgetMeter(budget or RunBudget(), clock=world.clock, run=world.run)
    return AgentDeps(
        conn=world.conn,
        clock=world.clock,
        ids=world.ids,
        env=GuardedToolEnv(world.mock, run=world.run, meter=meter),
        errands=world.errands,
        approvals=world.approvals,
        actions=world.actions,
        memories=MemoryStore(world.conn, clock=world.clock, ids=world.ids),
        profiles=ProfileStore(world.conn, clock=world.clock, ids=world.ids),
        user_id="user_1",
        session_id="sess_1",
        user_message=user_message,
        run=world.run,
        meter=meter,
    )
