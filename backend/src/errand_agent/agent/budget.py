"""Context guard: no model request exceeds the context budget.

What: `guard_context`, run by PydanticAI before *every* model request (a
      `ProcessHistory` capability on the orchestrator); `tool_definition_tokens()`,
      the cost of the tool schemas sent with every request; `request_tokens()`.

Why:  The ContextBuilder sizes what a run *starts* with. Within a run, each tool call
      and result is appended to the messages, so later requests grow (BUG-3: 2,457 ->
      5,062 tokens against a 4,500 budget). The assignment asks for a fixed cap on the
      model context, so the cap must hold for every request, not just the first.

How:  - Size = fixed part (tool definitions + instructions, set on the deps by the
        handler) + every message part, estimated with the same pessimistic estimator
        as the ContextBuilder.
      - Over budget -> observation masking: the oldest tool results from this run are
        replaced by a short placeholder, oldest first, until it fits. The newest
        results (the ones the model is about to read) are never masked. This is the
        cheap, deterministic compaction that matched LLM summarisation in the
        JetBrains study (docs/DECISIONS.md, D1). Masks persist for the rest of the run.
      - Still over -> the meter refuses (`limit_hit: context_tokens`) and the run ends
        `limit_reached` with an honest message; the oversized request is never sent.
      - The estimate is recorded on the deps, so the `model_call` event can show it
        next to the provider's real input tokens (calibration).
      - `async` on purpose: PydanticAI runs a synchronous processor in a worker
        thread, and our SQLite connections refuse other threads.
"""

import dataclasses
import json
from collections.abc import Sequence
from typing import Any

from pydantic_ai import ModelMessage, RunContext, Tool, ToolCallPart
from pydantic_ai.messages import ModelRequest, ToolReturnPart

from errand_agent.agent.deps import AgentDeps
from errand_agent.memory.tokens import estimate_tokens
from errand_agent.workflow.guarded_env import LimitExceeded

MASKED = "[result omitted to fit the context budget]"
PART_OVERHEAD_TOKENS = 4  # part kind, tool name and call id markers


def tool_definition_tokens(tools: Sequence[Tool[Any]]) -> int:
    """Estimated tokens of the tool schemas, which ship with every request."""
    definitions = [
        {
            "name": t.tool_def.name,
            "description": t.tool_def.description,
            "parameters": t.tool_def.parameters_json_schema,
        }
        for t in tools
    ]
    return estimate_tokens(json.dumps(definitions))


def _part_text(part: Any) -> str:
    if isinstance(part, ToolCallPart):
        return part.tool_name + json.dumps(part.args_as_dict())
    content = getattr(part, "content", "")
    return content if isinstance(content, str) else json.dumps(content, default=str)


def request_tokens(messages: Sequence[ModelMessage], *, fixed: int) -> int:
    parts = [p for m in messages for p in m.parts]
    return fixed + sum(estimate_tokens(_part_text(p)) + PART_OVERHEAD_TOKENS for p in parts)


def _maskable(messages: list[ModelMessage]) -> list[tuple[int, int]]:
    """(message, part) positions of this run's older tool results, oldest first. The
    last message holds the newest results, which the model is about to read."""
    return [
        (i, j)
        for i, m in enumerate(messages[:-1])
        if isinstance(m, ModelRequest)
        for j, p in enumerate(m.parts)
        if isinstance(p, ToolReturnPart) and p.content != MASKED
    ]


def _mask(messages: list[ModelMessage], i: int, j: int) -> None:
    request = messages[i]
    assert isinstance(request, ModelRequest)
    parts = list(request.parts)
    parts[j] = dataclasses.replace(parts[j], content=MASKED)  # type: ignore[call-arg]
    messages[i] = dataclasses.replace(request, parts=parts)


async def guard_context(
    ctx: RunContext[AgentDeps], messages: list[ModelMessage]
) -> list[ModelMessage]:
    deps = ctx.deps
    if deps.context_limit is None:  # unbudgeted (e.g. a bare agent in a test)
        return messages
    limit = deps.context_limit
    messages = list(messages)
    size = request_tokens(messages, fixed=deps.fixed_tokens)
    for i, j in _maskable(messages):
        if size <= limit:
            break
        _mask(messages, i, j)
        size = request_tokens(messages, fixed=deps.fixed_tokens)
    if size > limit:
        if deps.meter is not None:
            deps.meter.refuse_context(size, limit)  # traces limit_hit, raises
        raise LimitExceeded("context_tokens", size, limit, "model_turn")
    deps.request_estimate = size
    return messages
