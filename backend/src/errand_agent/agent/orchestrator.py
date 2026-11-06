"""Orchestrator: the conversational agent, wired to workflow commands only.

What: `build_orchestrator()` returns the PydanticAI `Agent[AgentDeps, str]` with the
      tools from tools.py; `FORBIDDEN_TOOL_NAMES` lists what it must never have.

Why:  One place that defines what the model can do. Instructions (system prompt, state,
      memories) and message history come from the ContextBuilder at run time, so the
      agent itself holds no conversation state.

How:  The model is supplied per run (`agent.run(model=...)` or `agent.override`), so
      tests use FunctionModel and the app uses the configured model. A structural test
      checks that no consequential provider action is exposed as a tool. The context
      guard (agent/budget.py) runs before every model request to keep it in budget,
      and every tool call is traced (agent/tool_trace.py);
      `orchestrator_tools()` is also what the budget measures, so the count matches
      exactly the definitions the model receives.
"""

from pydantic.json_schema import JsonSchemaValue
from pydantic_ai import Agent, Tool
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.tools import GenerateToolJsonSchema
from pydantic_core import core_schema

from errand_agent.agent.budget import guard_context
from errand_agent.agent.deps import AgentDeps
from errand_agent.agent.tool_trace import TraceAgentTools
from errand_agent.agent.tools import ALL_TOOLS

FORBIDDEN_TOOL_NAMES = frozenset(
    {"book", "cancel", "reschedule", "hold_slot", "approve", "grant", "execute"}
)


class LeanToolJsonSchema(GenerateToolJsonSchema):
    """Tool schemas without the optional-parameter boilerplate (docs/DECISIONS.md, D3).

    `area: str | None = None` is written as `{"type": "string"}` and left out of
    `required`, instead of `{"anyOf": [{"type": "string"}, {"type": "null"}],
    "default": null}`: "not required" already tells the model it may omit it, and the
    `null` branch cost ~40 tokens per parameter on every request. Only the JSON shown
    to the model changes; validation still uses the function's real types, so a
    `null` the model sends anyway is still accepted. A test pins every tool's
    parameter names, types and required flags.
    """

    def nullable_schema(self, schema: core_schema.NullableSchema) -> JsonSchemaValue:
        return self.generate_inner(schema["schema"])

    def default_schema(self, schema: core_schema.WithDefaultSchema) -> JsonSchemaValue:
        json_schema = super().default_schema(schema)
        if "default" in json_schema and json_schema["default"] is None:
            del json_schema["default"]
        return json_schema


def orchestrator_tools() -> list[Tool[AgentDeps]]:
    return [Tool(fn, takes_ctx=True, schema_generator=LeanToolJsonSchema) for fn in ALL_TOOLS]


def build_orchestrator() -> Agent[AgentDeps, str]:
    return Agent(
        deps_type=AgentDeps,
        output_type=str,
        tools=orchestrator_tools(),
        retries=2,
        name="errand-orchestrator",
        capabilities=[ProcessHistory(guard_context), TraceAgentTools()],
    )
