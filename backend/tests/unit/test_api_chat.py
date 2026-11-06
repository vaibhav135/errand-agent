"""Chat API: sessions, history, and a turn answered as an SSE stream (D4)."""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.models.function import FunctionModel

from errand_agent.api.app import Services, create_app
from errand_agent.core.clock import FakeClock
from errand_agent.core.config import Settings
from errand_agent.core.ids import SequentialIdGen
from tests.unit.test_agent_loop import Streaming


def client_for(settings: Settings, clock: FakeClock, model: Any = None) -> TestClient:
    return TestClient(create_app(settings, clock=clock, ids=SequentialIdGen(), model=model))


@pytest.fixture
def chat(app_settings: Settings, app_clock: FakeClock) -> Iterator[TestClient]:
    model = FunctionModel(stream_function=Streaming(("list_memories", {}), ["Hel", "lo!"]))
    with client_for(app_settings, app_clock, model) as client:
        yield client


def events(client: TestClient, session_id: str, text: str) -> list[tuple[str, dict[str, Any]]]:
    """POST a message and read the SSE reply as (event, data) pairs."""
    out: list[tuple[str, dict[str, Any]]] = []
    with client.stream(
        "POST", f"/api/sessions/{session_id}/messages", json={"text": text}
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        event = ""
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
            elif line.startswith("data: "):
                out.append((event, json.loads(line.removeprefix("data: "))))
    return out


def new_session(client: TestClient) -> str:
    response = client.post("/api/sessions")
    assert response.status_code == 201
    return response.json()["session_id"]


def test_a_new_session_starts_empty(chat: TestClient) -> None:
    session_id = new_session(chat)

    assert chat.get(f"/api/sessions/{session_id}/messages").json() == []


def test_a_turn_streams_progress_text_and_trace_then_the_final_reply(chat: TestClient) -> None:
    session_id = new_session(chat)

    stream = events(chat, session_id, "what do you remember?")

    kinds = [kind for kind, _ in stream]
    assert kinds[-1] == "final"
    assert ("tool", {"tool": "list_memories"}) in stream
    assert "".join(d["chunk"] for k, d in stream if k == "text") == "Hello!"
    trace_types = {d["type"] for k, d in stream if k == "trace"}
    assert {"run_started", "agent_tool", "model_call", "run_finished"} <= trace_types
    final = stream[-1][1]
    assert (final["kind"], final["message"]) == ("answered", "Hello!")
    assert final["run_id"].startswith("run_")


def test_history_shows_the_turn_without_internal_notes(chat: TestClient) -> None:
    session_id = new_session(chat)
    events(chat, session_id, "hi")

    history = chat.get(f"/api/sessions/{session_id}/messages").json()

    assert [(m["role"], m["content"]) for m in history] == [
        ("user", "hi"),
        ("assistant", "Hello!"),
    ]


def test_unknown_session_is_404(chat: TestClient) -> None:
    assert chat.post("/api/sessions/sess_nope/messages", json={"text": "hi"}).status_code == 404
    assert chat.get("/api/sessions/sess_nope/messages").status_code == 404


def test_without_a_key_a_turn_says_what_to_set(
    app_settings: Settings, app_clock: FakeClock
) -> None:
    with client_for(app_settings, app_clock) as client:  # no model override, no key
        session_id = new_session(client)
        response = client.post(f"/api/sessions/{session_id}/messages", json={"text": "hi"})

    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]


def test_the_cards_approve_button_decides_in_code(
    app_settings: Settings, app_clock: FakeClock
) -> None:
    model = FunctionModel(
        stream_function=Streaming(("start_cancel", {"booking_id": "bk_hist_coolbreeze"}), ["Ok?"])
    )
    with client_for(app_settings, app_clock, model) as client:
        session_id = new_session(client)
        card = events(client, session_id, "cancel my AC servicing")[-1][1]["approval"]

        response = client.post(
            f"/api/approvals/{card['approval_id']}/decision",
            json={"approve": True, "fingerprint": card["fingerprint"]},
        )

    assert response.status_code == 200
    assert response.json()["kind"] == "cancelled"


def test_a_pending_card_can_be_fetched_after_a_reload(
    app_settings: Settings, app_clock: FakeClock
) -> None:
    # A card made while the user was away (Phase 7) must still be there when they look.
    model = FunctionModel(
        stream_function=Streaming(("start_cancel", {"booking_id": "bk_hist_coolbreeze"}), ["Ok?"])
    )
    with client_for(app_settings, app_clock, model) as client:
        session_id = new_session(client)
        assert client.get(f"/api/sessions/{session_id}/pending").json() == []
        card = events(client, session_id, "cancel my AC servicing")[-1][1]["approval"]

        pending = client.get(f"/api/sessions/{session_id}/pending").json()

    assert [p["approval_id"] for p in pending] == [card["approval_id"]]
    assert pending[0]["fingerprint"] == card["fingerprint"]


def test_deciding_an_unknown_card_is_404(chat: TestClient) -> None:
    response = chat.post(
        "/api/approvals/appr_nope/decision", json={"approve": True, "fingerprint": "x"}
    )

    assert response.status_code == 404


def test_one_turn_at_a_time_per_session(chat: TestClient) -> None:
    services: Services = chat.app.state.services  # type: ignore[attr-defined]

    assert services.lock_for("sess_a") is services.lock_for("sess_a")
    assert services.lock_for("sess_a") is not services.lock_for("sess_b")
