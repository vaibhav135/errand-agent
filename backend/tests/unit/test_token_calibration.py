"""BUG-7: the offline token estimate must never be below the provider's real count.

The real counts come from tests/data/token_calibration.json, recorded by
`make calibrate` (Anthropic's count_tokens) on the exact texts stored there, so this
test runs offline and stays valid until the next calibration.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from errand_agent.memory.tokens import estimate_tokens

CALIBRATION = json.loads(
    (Path(__file__).resolve().parents[1] / "data" / "token_calibration.json").read_text()
)


def estimate(sample: dict[str, Any]) -> int:
    return estimate_tokens(sample["text"])


@pytest.mark.xfail(strict=True, reason="BUG-7: chars/3.5 undercounts Claude's real tokens")
@pytest.mark.parametrize("sample", CALIBRATION["samples"], ids=lambda s: s["name"])
def test_the_estimate_is_never_below_the_real_count(sample: dict[str, Any]) -> None:
    assert estimate(sample) >= sample["real_tokens"], (
        f"{sample['name']}: estimated {estimate(sample)}, real {sample['real_tokens']}"
    )


@pytest.mark.xfail(strict=True, reason="BUG-7: the hidden tool-use prompt isn't counted")
def test_tool_definitions_include_the_hidden_tool_use_prompt() -> None:
    from errand_agent.agent.budget import tool_definition_tokens
    from errand_agent.agent.orchestrator import orchestrator_tools

    [schemas] = [s for s in CALIBRATION["samples"] if s["name"] == "tool_schemas"]
    real = schemas["real_tokens"] + CALIBRATION["tool_use_overhead"]

    assert tool_definition_tokens(orchestrator_tools()) >= real
