"""Agent run loop: every model request is budgeted and traced.

What: `run_agent(agent, deps, model=, prompt=, instructions=, history=)` runs the
      orchestrator with `agent.iter()` and returns its reply; `to_history()` turns the
      context packet's messages into PydanticAI messages; `trace_model_call()` records
      one model request.

Why:  The assignment requires hard limits on model turns and a trace of every model
      call with tokens, latency and estimated cost. `agent.iter()` lets us act at each
      request instead of only seeing the totals at the end.

How:  - Before each request: `meter.charge_model_turn()`, so a request over the budget is
        refused *before* it is sent.
      - After each response: a `model_call` event (agent, model, input/output tokens,
        latency, cost). Cost is looked up by the configured model ("provider:name");
        unpriced models give null, which marks the run's totals `cost_complete: false`.
      - PydanticAI's UsageLimits is set well above our limits as a backstop; if it ever
        trips, it is converted into our LimitExceeded so every limit exit looks the same.
      - Instructions (system + state + memories) and history come from the
        ContextBuilder; the agent itself keeps no conversation state.
"""

from collections.abc import Sequence

from pydantic_ai import Agent, ModelMessage, ModelResponse, TextPart
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import Model
from pydantic_ai.usage import UsageLimits

from errand_agent.agent.deps import AgentDeps
from errand_agent.core.clock import IST
from errand_agent.tracing.cost import estimate_cost
from errand_agent.workflow.guarded_env import LimitExceeded

BACKSTOP_FACTOR = 3  # PydanticAI's own limits sit well above ours


def model_key(model: Model) -> str:
    return f"{model.system}:{model.model_name}"


def to_history(messages: Sequence[tuple[str, str]]) -> list[ModelMessage]:
    history: list[ModelMessage] = []
    for role, content in messages:
        if role == "user":
            history.append(ModelRequest(parts=[UserPromptPart(content)]))
        else:
            history.append(ModelResponse(parts=[TextPart(content)]))
    return history


def trace_model_call(
    deps: AgentDeps,
    *,
    agent: str,
    model: Model,
    input_tokens: int,
    output_tokens: int,
    latency_s: float,
) -> None:
    if deps.run is None:
        return
    key = model_key(model)
    deps.run.emit(
        "model_call",
        {
            "agent": agent,
            "model": key,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": round(latency_s * 1000),
            "cost_usd": estimate_cost(
                key,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                on=deps.clock.now().astimezone(IST).date(),
            ),
        },
    )


async def run_agent(
    agent: Agent[AgentDeps, str],
    deps: AgentDeps,
    *,
    model: Model,
    prompt: str,
    instructions: str,
    history: list[ModelMessage],
) -> str:
    limits = None
    if deps.meter is not None:
        budget = deps.meter.budget
        limits = UsageLimits(
            request_limit=budget.max_model_turns * BACKSTOP_FACTOR,
            tool_calls_limit=budget.max_tool_calls * BACKSTOP_FACTOR,
        )
    try:
        async with agent.iter(
            prompt,
            deps=deps,
            model=model,
            instructions=instructions,
            message_history=history,
            usage_limits=limits,
        ) as run:
            started = deps.clock.monotonic()
            async for node in run:
                if Agent.is_model_request_node(node):
                    if deps.meter is not None:
                        deps.meter.charge_model_turn()  # refuse before the request is sent
                    started = deps.clock.monotonic()
                elif Agent.is_call_tools_node(node):
                    usage = node.model_response.usage
                    trace_model_call(
                        deps,
                        agent="orchestrator",
                        model=model,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        latency_s=deps.clock.monotonic() - started,
                    )
            assert run.result is not None
            return run.result.output
    except UsageLimitExceeded as exc:
        raise LimitExceeded("model_turns", 0, 0, f"backstop: {exc}") from exc
