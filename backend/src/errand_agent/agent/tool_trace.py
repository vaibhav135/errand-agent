"""Agent tool tracing: every call the model makes to one of our tools leaves a trace.

What: `TraceAgentTools`, a PydanticAI capability on the orchestrator that wraps every
      tool execution in an `agent_tool` trace span.

Why:  The assignment requires tool names and arguments in every trace. Provider calls
      were traced (GuardedToolEnv, `tool_call`), but the agent's own tools
      (`list_memories`, `propose_booking`, ...) left nothing, so a trace couldn't show
      what the model actually did (BUG-5).

How:  - One `agent_tool` span per call: tool, the args as the model sent them, result,
        status (ok / retry / error), latency. Provider calls a tool makes still appear as their own
        `tool_call` events, so a turn reads: agent_tool find_my_bookings -> tool_call
        list_bookings.
      - The recorder sanitizes every payload (secrets redacted, long text cut), but
        sanitizing doesn't know personal data. Contact details are profile PII that
        never enters traces, so for the profile tools only the *names* of the
        arguments are recorded, the rule profile events already follow.
      - A `ModelRetry` (the model sent bad input and is asked to fix it) is recorded as
        status "retry" with its reason, then re-raised for PydanticAI to handle.
"""

from dataclasses import dataclass
from typing import Any

from pydantic_ai import RunContext, ToolCallPart
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.capabilities.abstract import ValidatedToolArgs, WrapToolExecuteHandler
from pydantic_ai.exceptions import ModelRetry, ToolRetryError
from pydantic_ai.tools import ToolDefinition

from errand_agent.agent.deps import AgentDeps

ARG_NAMES_ONLY = frozenset({"update_profile", "add_address"})  # their values are PII


def traced_args(call: ToolCallPart) -> Any:
    """The arguments as the model sent them (not the validated ones with defaults filled
    in); for profile tools, their names only."""
    sent = call.args_as_dict()
    if call.tool_name in ARG_NAMES_ONLY:
        return sorted(name for name, value in sent.items() if value is not None)
    return sent


@dataclass
class TraceAgentTools(AbstractCapability[AgentDeps]):
    async def wrap_tool_execute(
        self,
        ctx: RunContext[AgentDeps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: ValidatedToolArgs,
        handler: WrapToolExecuteHandler,
    ) -> Any:
        if ctx.deps.on_tool is not None:  # announce it first: a progress line in the UI
            await ctx.deps.on_tool(call.tool_name)
        run = ctx.deps.run
        if run is None:
            return await handler(args)
        retry: BaseException | None = None
        result: Any = None
        with run.span("agent_tool", tool=call.tool_name, args=traced_args(call)) as span:
            try:
                result = await handler(args)
                span.set(result=result)
            except (ModelRetry, ToolRetryError) as exc:  # not an error: the model retries
                span.set(status="retry", reason=str(exc))
                retry = exc
        if retry is not None:
            raise retry  # outside the span, so it stays recorded as a retry
        return result
