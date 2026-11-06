"""Orchestrator: the conversational agent, wired to workflow commands only.

What: `build_orchestrator()` returns the PydanticAI `Agent[AgentDeps, str]` with the
      tools from tools.py; `FORBIDDEN_TOOL_NAMES` lists what it must never have.

Why:  One place that defines what the model can do. Instructions (system prompt, state,
      memories) and message history come from the ContextBuilder at run time, so the
      agent itself holds no conversation state.

How:  The model is supplied per run (`agent.run(model=...)` or `agent.override`), so
      tests use FunctionModel and the app uses the configured model. A structural test
      checks that no consequential provider action is exposed as a tool. The context
      guard (agent/budget.py) runs before every model request to keep it in budget;
      `orchestrator_tools()` is also what the budget measures, so the count matches
      exactly the definitions the model receives.
"""

from pydantic_ai import Agent, Tool
from pydantic_ai.capabilities import ProcessHistory

from errand_agent.agent.budget import guard_context
from errand_agent.agent.deps import AgentDeps
from errand_agent.agent.tools import ALL_TOOLS

FORBIDDEN_TOOL_NAMES = frozenset(
    {"book", "cancel", "reschedule", "hold_slot", "approve", "grant", "execute"}
)


def orchestrator_tools() -> list[Tool[AgentDeps]]:
    return [Tool(fn, takes_ctx=True) for fn in ALL_TOOLS]


def build_orchestrator() -> Agent[AgentDeps, str]:
    return Agent(
        deps_type=AgentDeps,
        output_type=str,
        tools=orchestrator_tools(),
        retries=2,
        name="errand-orchestrator",
        capabilities=[ProcessHistory(guard_context)],
    )
