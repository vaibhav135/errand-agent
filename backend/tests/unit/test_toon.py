"""TOON experiment: the encoding must carry every id and value exactly."""

import csv
import json
from pathlib import Path

import pytest

from errand_agent.agent.toon import to_toon
from errand_agent.mocks.scenario import Scenario
from tests.unit.agent_script import ScriptedModel, call, deps_for, say, tool_returns
from tests.unit.workflow_world import World


def parse(text: str) -> list[dict[str, str]]:
    """A minimal TOON reader for the tests: header, then one row per line. Rows are
    comma-separated with JSON-style quoting (\\" inside quotes), which csv can read."""
    header, *lines = text.splitlines()
    fields = header[header.index("{") + 1 : header.index("}")].split(",")
    rows = [line[2:] for line in lines if line.startswith("  ")]  # not "key: value" lines
    reader = csv.reader(rows, escapechar="\\", doublequote=False)
    return [dict(zip(fields, cells, strict=True)) for cells in reader]


def test_a_uniform_list_becomes_a_table() -> None:
    text = to_toon(
        "bookings", [{"id": "bk_1", "status": "confirmed"}, {"id": "bk_2", "status": "completed"}]
    )

    assert text == "bookings[2]{id,status}:\n  bk_1,confirmed\n  bk_2,completed"


def test_awkward_values_survive_exactly() -> None:
    rows = [{"name": 'Fix, "Quick"', "note": " padded ", "empty": "", "n": 3}]

    assert parse(to_toon("x", rows)) == [
        {"name": 'Fix, "Quick"', "note": " padded ", "empty": "", "n": "3"}
    ]


def test_irregular_lists_fall_back_to_json() -> None:
    rows = [{"a": 1}, {"b": 2}]

    assert json.loads(to_toon("x", rows)) == rows


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path, Scenario())


async def test_find_my_bookings_as_toon_keeps_every_booking(world: World) -> None:
    from errand_agent.agent.orchestrator import build_orchestrator

    as_json = deps_for(world, "my bookings?")
    as_toon = deps_for(world, "my bookings?")
    as_toon.result_format = "toon"
    results = []
    for deps in (as_json, as_toon):
        script = ScriptedModel([call("find_my_bookings")], [say("ok")])
        result = await build_orchestrator().run("my bookings?", deps=deps, model=script.model())
        [r] = tool_returns(result.all_messages(), "find_my_bookings")
        results.append(r)

    json_rows, toon_text = results
    assert isinstance(toon_text, str) and toon_text.startswith("bookings[")
    assert parse(toon_text) == [{k: str(v) for k, v in row.items()} for row in json_rows]
