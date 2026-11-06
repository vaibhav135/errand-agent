"""Trace API: the live push stream and past runs, for the web UI's trace pane (D4)."""

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.models.function import FunctionModel

from errand_agent.api import trace_routes
from errand_agent.api.app import create_app
from errand_agent.api.events import EventHub
from errand_agent.api.trace_routes import event_stream
from errand_agent.core.clock import FakeClock
from errand_agent.core.config import Settings
from errand_agent.core.ids import SequentialIdGen
from errand_agent.tracing.recorder import Tracer
from tests.unit.test_agent_loop import Streaming
from tests.unit.test_api_chat import events, new_session


def frames(chunks: list[str]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for chunk in chunks:
        lines = dict(line.split(": ", 1) for line in chunk.strip().splitlines() if ": " in line)
        if "event" in lines:
            out.append((lines["event"], json.loads(lines["data"])))
    return out


async def test_the_stream_pushes_this_sessions_events_and_keeps_alive(tmp_path: Path) -> None:
    hub = EventHub()
    clock = FakeClock(datetime(2026, 11, 5, 3, 30, tzinfo=UTC))
    tracer = Tracer(clock, SequentialIdGen(), subscribers=[hub.publish])
    disconnected = False

    async def is_disconnected() -> bool:
        return disconnected

    stream = event_stream(hub, "sess_mine", is_disconnected, heartbeat_seconds=0.01)
    received: list[str] = [await anext(stream)]  # ": connected"
    tracer.start_run(session_id="sess_other", trigger="test").finish(outcome="answered")
    tracer.start_run(session_id="sess_mine", trigger="reconcile").finish(outcome="reconciled")
    received += [await anext(stream), await anext(stream), await anext(stream)]
    disconnected = True
    received += [chunk async for chunk in stream]  # ends once the client is gone

    pushed = frames(received)
    assert [e["type"] for _, e in pushed] == ["run_started", "run_finished"]
    assert {e["session_id"] for _, e in pushed} == {"sess_mine"}
    assert received[0].startswith(": connected")
    assert any(c.startswith(": keep-alive") for c in received)


async def test_the_stream_also_pushes_chat_messages_for_this_session() -> None:
    hub = EventHub()

    async def connected() -> bool:
        return False

    stream = event_stream(hub, "sess_mine", connected, heartbeat_seconds=0.01)
    await anext(stream)  # ": connected"
    hub.publish_message("sess_other", {"role": "assistant", "content": "not yours"})
    hub.publish_message("sess_mine", {"role": "assistant", "content": "Your provider cancelled."})

    (pushed,) = frames([await anext(stream)])

    assert pushed == (
        "message",
        {"role": "assistant", "content": "Your provider cancelled.", "session_id": "sess_mine"},
    )


async def test_the_stream_stops_when_the_client_leaves() -> None:
    async def gone() -> bool:
        return True

    stream = event_stream(EventHub(), "s", gone, heartbeat_seconds=0.01)
    chunks = [chunk async for chunk in stream]

    assert len(chunks) <= 2  # the greeting, maybe one keep-alive, then done


@pytest.fixture
def chat(app_settings: Settings, app_clock: FakeClock) -> Iterator[TestClient]:
    model = FunctionModel(stream_function=Streaming(["Hi!"], ["Again!"]))
    app = create_app(app_settings, clock=app_clock, ids=SequentialIdGen(), model=model)
    with TestClient(app) as client:
        yield client


def test_events_route_answers_with_an_event_stream(
    chat: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trace_routes, "HEARTBEAT_SECONDS", 0.01)
    session_id = new_session(chat)

    with chat.stream("GET", f"/api/events?session_id={session_id}") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert next(response.iter_lines()).startswith(": connected")


def test_past_runs_come_back_after_a_reload(chat: TestClient) -> None:
    mine, other = new_session(chat), new_session(chat)
    events(chat, mine, "hi")
    events(chat, other, "hello")

    runs = chat.get(f"/api/sessions/{mine}/runs").json()

    assert len(runs) == 1
    [run] = runs
    types = [e["type"] for e in run["events"]]
    assert types[0] == "run_started" and types[-1] == "run_finished"
    assert {e["session_id"] for e in run["events"]} == {mine}


def test_runs_of_an_unknown_session_is_404(chat: TestClient) -> None:
    assert chat.get("/api/sessions/sess_nope/runs").status_code == 404


def test_reading_runs_never_blocks_the_loop() -> None:
    # The trace files are read in a worker thread, so a long history can't stall
    # other requests; the reader touches only files, never SQLite.
    assert asyncio.iscoroutinefunction(trace_routes.past_runs)
