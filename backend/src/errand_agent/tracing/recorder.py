"""Trace recorder: one structured event stream per run.

What: `Tracer.start_run()` opens a `RunTrace`; it emits typed events (state
      transitions, model calls, tool calls, approvals, context sent, limits,
      errors) to subscribers, and closes with exactly one `run_finished` that
      carries the outcome and totals. `JsonlWriter` is the file subscriber.

Why:  The assignment needs a trace per run (transitions, model calls, tools and
      arguments, sanitized results, latency, tokens, cost), and evals assert on
      these events rather than on reply wording. A run is one inbound event
      (user message, approval, proactive event) handled end to end, so a booking
      spans several runs, each with its own domain outcome.

How:  - Envelope per event: ts, run_id, session_id, seq, type, then the payload.
        Event types are a fixed set; payload keys may not shadow the envelope.
      - Lifecycle (full definition: docs/ARCHITECTURE.md, "Run lifecycle"):
        `run_started` on start; `finish(outcome)` emits `run_finished`
        once, then the run is closed (later emits raise). As a context manager,
        an escaping exception records outcome "error" and re-raises; leaving
        without `finish()` records "unspecified", since that is always a bug.
        A crash leaves no `run_finished` at all, which marks the run as orphaned.
      - `span()` times work with the injected clock; ts is the span's start.
      - Totals: call counts, tokens, duration, cost. `cost_complete` is False
        if any model call had no known price, so partial sums are never
        mistaken for the full cost.
      - Subscribers are isolated: one failing (disk full, closed SSE client) is
        logged and skipped; tracing never breaks the business logic.
      - `JsonlWriter` opens, appends and closes per event, so a partial trace
        is on disk and readable if the process dies mid-run.
"""

import json
import logging
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from errand_agent.core.clock import Clock
from errand_agent.core.ids import IdGen
from errand_agent.tracing.sanitize import sanitize

logger = logging.getLogger(__name__)

LIFECYCLE_TYPES = frozenset({"run_started", "run_finished"})
EVENT_TYPES = LIFECYCLE_TYPES | {
    "state_transition",
    "model_call",
    "context_sent",
    "tool_call",
    "approval",
    "memory",
    "limit_hit",
    "error",
}
ENVELOPE_KEYS = frozenset({"ts", "run_id", "session_id", "seq", "type"})


@dataclass(frozen=True)
class TraceEvent:
    ts: datetime
    run_id: str
    session_id: str
    seq: int
    type: str
    data: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts.isoformat(),
            "run_id": self.run_id,
            "session_id": self.session_id,
            "seq": self.seq,
            "type": self.type,
            **self.data,
        }


Subscriber = Callable[[TraceEvent], None]


def _error_info(exc: BaseException) -> dict[str, str]:
    return {"type": type(exc).__name__, "message": str(exc)}


def _check_payload_keys(keys: Iterable[str]) -> None:
    clash = ENVELOPE_KEYS & set(keys)
    if clash:
        raise ValueError(f"payload keys {sorted(clash)} would overwrite the envelope")


@dataclass
class Span:
    fields: dict[str, Any] = field(default_factory=dict)

    def set(self, **fields: Any) -> None:
        _check_payload_keys(fields)  # fail here, not later inside span()'s finally
        self.fields.update(fields)


class RunTrace:
    def __init__(
        self,
        *,
        run_id: str,
        session_id: str,
        clock: Clock,
        subscribers: Sequence[Subscriber],
    ) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self._clock = clock
        self._subscribers = subscribers
        self._seq = 0
        self._finished = False
        self._started_mono = clock.monotonic()
        self._model_calls = 0
        self._tool_calls = 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._cost: float | None = None
        self._cost_complete = True

    # --- public API ---------------------------------------------------------------

    def emit(self, type: str, data: Mapping[str, Any] | None = None) -> None:
        if type in LIFECYCLE_TYPES:
            raise ValueError(f"{type} is emitted by start_run()/finish(), not emit()")
        self._emit(type, dict(data or {}))

    @contextmanager
    def span(self, type: str, **fields: Any) -> Iterator[Span]:
        # Validate everything *before* running the body, so the emit in `finally`
        # cannot raise and mask an exception from the body (same failure shape
        # as BUG-1).
        if type in LIFECYCLE_TYPES:
            raise ValueError(f"{type} is emitted by start_run()/finish(), not span()")
        self._check_can_emit(type, fields)

        ts = self._clock.now()
        started = self._clock.monotonic()
        span = Span(dict(fields))
        try:
            yield span
        except BaseException as exc:
            span.fields.update(status="error", error=_error_info(exc))
            raise
        finally:
            span.fields.setdefault("status", "ok")
            span.fields["latency_ms"] = round((self._clock.monotonic() - started) * 1000)
            self._emit(type, span.fields, ts=ts)

    def finish(self, outcome: str, **extra: Any) -> None:
        if self._finished:
            raise RuntimeError(f"run {self.run_id} already finished")
        totals = {
            "model_calls": self._model_calls,
            "tool_calls": self._tool_calls,
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "cost_usd": self._cost,
            "cost_complete": self._cost_complete,
            "duration_ms": round((self._clock.monotonic() - self._started_mono) * 1000),
        }
        self._emit("run_finished", {"outcome": outcome, **extra, "totals": totals})
        self._finished = True

    def __enter__(self) -> "RunTrace":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._finished:
            return
        if exc is not None:
            self.finish(outcome="error", error=_error_info(exc))
        else:
            self.finish(outcome="unspecified")

    # --- internals ----------------------------------------------------------------

    def _check_can_emit(self, type: str, data: Mapping[str, Any]) -> None:
        if self._finished:
            raise RuntimeError(f"run {self.run_id} is finished; cannot emit {type}")
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown trace event type {type!r}")
        _check_payload_keys(data)

    def _emit(self, type: str, data: dict[str, Any], ts: datetime | None = None) -> None:
        self._check_can_emit(type, data)
        data = sanitize(data)  # every payload, so no call site can forget
        self._accumulate(type, data)
        event = TraceEvent(
            ts=ts or self._clock.now(),
            run_id=self.run_id,
            session_id=self.session_id,
            seq=self._seq,
            type=type,
            data=data,
        )
        self._seq += 1
        for subscriber in self._subscribers:
            try:
                subscriber(event)
            except Exception:
                logger.exception("trace subscriber %r failed on %s", subscriber, type)

    def _accumulate(self, type: str, data: Mapping[str, Any]) -> None:
        if type == "tool_call":
            self._tool_calls += 1
        elif type == "model_call":
            self._model_calls += 1
            self._input_tokens += int(data.get("input_tokens") or 0)
            self._output_tokens += int(data.get("output_tokens") or 0)
            cost = data.get("cost_usd")
            if cost is None:
                self._cost_complete = False
            else:
                self._cost = (self._cost or 0.0) + float(cost)


class Tracer:
    def __init__(self, clock: Clock, ids: IdGen, subscribers: Sequence[Subscriber] = ()) -> None:
        self._clock = clock
        self._ids = ids
        self._subscribers = list(subscribers)

    def start_run(self, *, session_id: str, trigger: str) -> RunTrace:
        run = RunTrace(
            run_id=self._ids.new("run"),
            session_id=session_id,
            clock=self._clock,
            subscribers=self._subscribers,
        )
        run._emit("run_started", {"trigger": trigger})  # pyright: ignore[reportPrivateUsage]
        return run


def _json_default(value: object) -> str:
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)  # Paths, enums, etc. Traces favour "readable" over "fails".


class JsonlWriter:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._directory.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: TraceEvent) -> None:
        line = json.dumps(event.to_dict(), default=_json_default, ensure_ascii=False)
        # Open/append/close per event: the line is on disk before the next one,
        # so a crash mid-run still leaves a readable partial trace.
        with (self._directory / f"{event.run_id}.jsonl").open("a", encoding="utf-8") as f:
            f.write(line + "\n")
