"""Orchestrator: the conversational agent, wired to workflow commands only.

What: `build_orchestrator()` returns the PydanticAI `Agent[AgentDeps, str]` with the
      tools from tools.py; `FORBIDDEN_TOOL_NAMES` lists what it must never have.

Why:  One place that defines what the model can do. Instructions (system prompt, state,
      memories) and message history come from the ContextBuilder at run time, so the
      agent itself holds no conversation state.

How:  The model is supplied per run (`agent.run(model=...)` or `agent.override`), so
      tests use FunctionModel and the app uses the configured model. A structural test
      checks that no consequential provider action is exposed as a tool.
"""

from pydantic_ai import Agent, Tool

from errand_agent.agent.deps import AgentDeps
from errand_agent.agent.tools import ALL_TOOLS

FORBIDDEN_TOOL_NAMES = frozenset(
    {"book", "cancel", "reschedule", "hold_slot", "approve", "grant", "execute"}
)


def build_orchestrator() -> Agent[AgentDeps, str]:
    return Agent(
        deps_type=AgentDeps,
        output_type=str,
        tools=[Tool(fn, takes_ctx=True) for fn in ALL_TOOLS],
        retries=2,
        name="errand-orchestrator",
    )
