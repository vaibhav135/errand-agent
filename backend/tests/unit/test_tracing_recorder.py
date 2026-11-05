import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from errand_agent.core.clock import FakeClock
from errand_agent.core.ids import SequentialIdGen
from errand_agent.tracing.recorder import JsonlWriter, TraceEvent, Tracer

START = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


class Collector:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def __call__(self, event: TraceEvent) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def sink() -> Collector:
    return Collector()


@pytest.fixture
def tracer(clock: FakeClock, sink: Collector) -> Tracer:
    return Tracer(clock, SequentialIdGen(), subscribers=[sink])


# --- envelope & sequencing -------------------------------------------------------


def test_start_run_emits_run_started_with_envelope(tracer: Tracer, sink: Collector) -> None:
    run = tracer.start_run(session_id="sess_1", trigger="user_message")

    assert run.run_id == "run_0001"
    started = sink.events[0].to_dict()
    assert started == {
        "ts": "2026-11-05T09:00:00+00:00",
        "run_id": "run_0001",
        "session_id": "sess_1",
        "seq": 0,
        "type": "run_started",
        "trigger": "user_message",
    }


def test_seq_increments_within_a_run(tracer: Tracer, sink: Collector) -> None:
    run = tracer.start_run(session_id="s", trigger="user_message")
    run.emit("state_transition", {"from": "idle", "to": "searching"})
    run.emit("state_transition", {"from": "searching", "to": "comparing"})

    assert [e.seq for e in sink.events] == [0, 1, 2]


def test_unknown_event_type_is_rejected(tracer: Tracer) -> None:
    run = tracer.start_run(session_id="s", trigger="user_message")

    with pytest.raises(ValueError, match="tool_cal"):
        run.emit("tool_cal", {})


@pytest.mark.parametrize("lifecycle", ["run_started", "run_finished"])
def test_lifecycle_events_cannot_be_emitted_directly(tracer: Tracer, lifecycle: str) -> None:
    # Only start_run()/finish() produce these, so "exactly one run_finished" holds.
    run = tracer.start_run(session_id="s", trigger="user_message")

    with pytest.raises(ValueError, match=lifecycle):
        run.emit(lifecycle, {})


@pytest.mark.parametrize("reserved", ["ts", "run_id", "session_id", "seq", "type"])
def test_payload_cannot_overwrite_envelope(tracer: Tracer, reserved: str) -> None:
    run = tracer.start_run(session_id="s", trigger="user_message")

    with pytest.raises(ValueError, match=reserved):
        run.emit("tool_call", {reserved: "x"})


# --- spans ---------------------------------------------------------------------------


def test_span_records_latency_and_fields(tracer: Tracer, sink: Collector, clock: FakeClock) -> None:
    run = tracer.start_run(session_id="s", trigger="user_message")

    with run.span("tool_call", tool="search_providers", args={"service": "plumber"}) as span:
        clock.advance(0.25)
        span.set(status="ok", result={"count": 4})

    event = sink.events[-1].to_dict()
    assert event["type"] == "tool_call"
    assert event["tool"] == "search_providers"
    assert event["args"] == {"service": "plumber"}
    assert event["status"] == "ok"
    assert event["result"] == {"count": 4}
    assert event["latency_ms"] == 250
    assert event["ts"] == "2026-11-05T09:00:00+00:00"  # stamped at span start


def test_span_records_error_and_reraises(tracer: Tracer, sink: Collector) -> None:
    run = tracer.start_run(session_id="s", trigger="user_message")

    with pytest.raises(TimeoutError), run.span("tool_call", tool="book", args={}):
        raise TimeoutError("provider did not answer")

    event = sink.events[-1].to_dict()
    assert event["status"] == "error"
    assert event["error"] == {"type": "TimeoutError", "message": "provider did not answer"}


def test_span_on_finished_run_fails_before_body_runs(tracer: Tracer) -> None:
    # If validation happened only at emit time (in span's finally), an exception
    # from the body would be replaced by the "finished" error. It must fail first.
    run = tracer.start_run(session_id="s", trigger="user_message")
    run.finish(outcome="ok")
    body_ran = False

    with pytest.raises(RuntimeError, match="finished"), run.span("tool_call", tool="book"):
        body_ran = True

    assert body_ran is False


def test_span_set_rejects_envelope_keys(tracer: Tracer) -> None:
    run = tracer.start_run(session_id="s", trigger="user_message")

    with pytest.raises(ValueError, match="seq"), run.span("tool_call", tool="book") as span:
        span.set(seq=99)


# --- finishing & totals --------------------------------------------------------------


def test_finish_emits_totals(tracer: Tracer, sink: Collector, clock: FakeClock) -> None:
    run = tracer.start_run(session_id="s", trigger="user_message")
    run.emit(
        "model_call", {"model": "m", "input_tokens": 100, "output_tokens": 20, "cost_usd": 0.002}
    )
    run.emit(
        "model_call", {"model": "m", "input_tokens": 50, "output_tokens": 10, "cost_usd": 0.001}
    )
    with run.span("tool_call", tool="search_providers", args={}) as span:
        span.set(status="ok")
    clock.advance(1.5)

    run.finish(outcome="booked")

    finished = sink.events[-1].to_dict()
    assert finished["type"] == "run_finished"
    assert finished["outcome"] == "booked"
    assert finished["totals"] == {
        "model_calls": 2,
        "tool_calls": 1,
        "input_tokens": 150,
        "output_tokens": 30,
        "cost_usd": pytest.approx(0.003),
        "cost_complete": True,
        "duration_ms": 1500,
    }


def test_cost_marked_incomplete_when_a_price_is_unknown(tracer: Tracer, sink: Collector) -> None:
    run = tracer.start_run(session_id="s", trigger="user_message")
    run.emit(
        "model_call", {"model": "known", "input_tokens": 1, "output_tokens": 1, "cost_usd": 0.01}
    )
    run.emit(
        "model_call", {"model": "unknown", "input_tokens": 1, "output_tokens": 1, "cost_usd": None}
    )

    run.finish(outcome="ok")

    totals = sink.events[-1].to_dict()["totals"]
    assert totals["cost_usd"] == pytest.approx(0.01)
    assert totals["cost_complete"] is False


def test_cost_is_none_when_no_price_known(tracer: Tracer, sink: Collector) -> None:
    run = tracer.start_run(session_id="s", trigger="user_message")

    run.finish(outcome="ok")

    totals = sink.events[-1].to_dict()["totals"]
    assert totals["cost_usd"] is None
    assert totals["cost_complete"] is True  # nothing was priced, nothing is missing


def test_emit_after_finish_is_rejected(tracer: Tracer) -> None:
    run = tracer.start_run(session_id="s", trigger="user_message")
    run.finish(outcome="ok")

    with pytest.raises(RuntimeError):
        run.emit("state_transition", {})
    with pytest.raises(RuntimeError):
        run.finish(outcome="ok")


def test_context_manager_finishes_with_error_on_exception(tracer: Tracer, sink: Collector) -> None:
    with pytest.raises(KeyError), tracer.start_run(session_id="s", trigger="user_message"):
        raise KeyError("boom")

    finished = sink.events[-1].to_dict()
    assert finished["type"] == "run_finished"
    assert finished["outcome"] == "error"
    assert finished["error"]["type"] == "KeyError"


def test_context_manager_keeps_explicit_outcome(tracer: Tracer, sink: Collector) -> None:
    with tracer.start_run(session_id="s", trigger="user_message") as run:
        run.finish(outcome="needs_approval")

    assert sink.types().count("run_finished") == 1
    assert sink.events[-1].to_dict()["outcome"] == "needs_approval"


def test_leaving_without_finish_records_unspecified(tracer: Tracer, sink: Collector) -> None:
    # Every run must end with a domain outcome; forgetting finish() is a bug, so
    # it is recorded visibly (evals assert no run ends "unspecified").
    with tracer.start_run(session_id="s", trigger="user_message"):
        pass

    assert sink.events[-1].to_dict()["outcome"] == "unspecified"


# --- subscribers -----------------------------------------------------------------------


def test_failing_subscriber_does_not_break_run_or_others(clock: FakeClock, sink: Collector) -> None:
    def broken(_: TraceEvent) -> None:
        raise OSError("disk full")

    tracer = Tracer(clock, SequentialIdGen(), subscribers=[broken, sink])

    with tracer.start_run(session_id="s", trigger="user_message") as run:
        run.emit("state_transition", {"from": "a", "to": "b"})

    assert sink.types() == ["run_started", "state_transition", "run_finished"]


# --- JSONL writer -----------------------------------------------------------------------


def test_jsonl_writer_writes_one_object_per_line(clock: FakeClock, tmp_path: Path) -> None:
    tracer = Tracer(clock, SequentialIdGen(), subscribers=[JsonlWriter(tmp_path / "traces")])

    with tracer.start_run(session_id="s", trigger="user_message") as run:
        run.emit("state_transition", {"from": "idle", "to": "searching"})

    lines = (tmp_path / "traces" / "run_0001.jsonl").read_text().splitlines()
    events = [json.loads(line) for line in lines]
    assert [e["type"] for e in events] == ["run_started", "state_transition", "run_finished"]


def test_jsonl_file_is_readable_before_the_run_finishes(clock: FakeClock, tmp_path: Path) -> None:
    # Crash safety: if the process dies mid-run, what was emitted is on disk.
    tracer = Tracer(clock, SequentialIdGen(), subscribers=[JsonlWriter(tmp_path)])
    run = tracer.start_run(session_id="s", trigger="user_message")
    run.emit("state_transition", {"from": "idle", "to": "booking"})

    lines = (tmp_path / "run_0001.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_jsonl_serialises_datetimes_as_iso(clock: FakeClock, tmp_path: Path) -> None:
    tracer = Tracer(clock, SequentialIdGen(), subscribers=[JsonlWriter(tmp_path)])
    run = tracer.start_run(session_id="s", trigger="user_message")

    run.emit("tool_call", {"tool": "hold_slot", "result": {"expires_at": START}})

    last = json.loads((tmp_path / "run_0001.jsonl").read_text().splitlines()[-1])
    assert last["result"]["expires_at"] == "2026-11-05T09:00:00+00:00"
