"""Tool definitions: a fixed cost on every request, so keep them lean without changing
what the model can call (docs/DECISIONS.md, D3)."""

import json
from typing import Any

from errand_agent.agent.orchestrator import orchestrator_tools
from errand_agent.memory.tokens import estimate_tokens

# The contract: every tool's parameters, their types and whether they're required.
# Slimming the schemas must leave this exactly as it is.
CONTRACT: dict[str, dict[str, tuple[str, bool]]] = {
    "update_requirements": {
        "service": ("string[plumber|electrician|ac_servicing|cleaning|carpenter]", False),
        "area": ("string", False),
        "date_start": ("string:date", False),
        "date_end": ("string:date", False),
        "earliest_start": ("string:time", False),
        "latest_start": ("string:time", False),
        "no_time_preference": ("boolean", False),
        "budget_max_inr": ("integer", False),
        "no_budget": ("boolean", False),
        "preferred_provider_id": ("string", False),
    },
    "search_and_compare": {},
    "propose_booking": {
        "option_number": ("integer", True),
        "slot": ("string:date-time", True),
    },
    "decline_pending": {},
    "get_reviews_summary": {"provider_id": ("string", True)},
    "find_my_bookings": {},
    "available_slots": {
        "booking_id": ("string", True),
        "date_start": ("string:date", True),
        "date_end": ("string:date", True),
    },
    "start_cancel": {"booking_id": ("string", True)},
    "start_reschedule": {
        "booking_id": ("string", True),
        "new_slot": ("string:date-time", True),
    },
    "remember": {
        "key": (
            "string[default_address|earliest_start|latest_start|budget_max_inr"
            "|preferred_provider|avoided_provider|note]",
            True,
        ),
        "value": ("string or integer or object", True),
        "quote": ("string", True),
        "scope": ("string", False),
        "topic": ("string[home_services|food|travel|family|work|other]", False),
    },
    "list_memories": {"include_personal_notes": ("boolean", False)},
    "forget_memory": {"memory_id": ("string", True)},
    "show_suggestions": {},
    "correct_memory": {
        "memory_id": ("string", True),
        "new_value": ("string or integer or object", True),
        "quote": ("string", True),
    },
    "update_profile": {
        "name": ("string", False),
        "phone": ("string", False),
        "email": ("string", False),
    },
    "add_address": {
        "label": ("string", True),
        "line": ("string", True),
        "area": ("string", True),
        "make_default": ("boolean", False),
    },
}


def _kind(prop: dict[str, Any]) -> str:
    """A parameter's type, ignoring how optionality is spelled (a `null` branch)."""
    kinds = []
    for option in prop.get("anyOf", [prop]):
        if option.get("type") == "null":
            continue
        kind = option.get("type", "any")
        if "enum" in option:
            kind += "[" + "|".join(option["enum"]) + "]"
        if "format" in option:
            kind += ":" + option["format"]
        kinds.append(kind)
    return " or ".join(kinds)


def test_slimming_never_changes_what_the_model_can_call() -> None:
    actual = {}
    for tool in orchestrator_tools():
        schema = tool.tool_def.parameters_json_schema
        required = set(schema.get("required", []))
        actual[tool.tool_def.name] = {
            name: (_kind(prop), name in required)
            for name, prop in schema.get("properties", {}).items()
        }

    assert actual == CONTRACT


def test_tool_definitions_stay_lean() -> None:
    definitions = [
        {
            "name": t.tool_def.name,
            "description": t.tool_def.description,
            "parameters": t.tool_def.parameters_json_schema,
        }
        for t in orchestrator_tools()
    ]

    # Sent with every request: ~3,000 estimated tokens before slimming, ~2,500 after
    # (lean optional parameters + tighter descriptions). This cap stops it creeping back.
    assert estimate_tokens(json.dumps(definitions)) <= 2550
