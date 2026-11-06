"""Record the design note's end-to-end trace: a real booking with a timeout after success.

What: `make trace` runs one short conversation with the real model (a canned user asks
      for a plumber, picks an option, approves) with the booking armed to time out
      *after* it went through, and copies that conversation's trace files (one JSONL per
      run, in order) into docs/trace/.

Why:  The assignment asks for one end-to-end trace, raw file plus an annotated excerpt in
      the design note, for a run that includes a timeout after success. Recording it
      with the real model keeps the model calls, tokens and cost real; a script makes it
      reproducible rather than a hand-picked file.

How:  The app's own services on a throwaway data directory (seeded demo user), so your
      databases are untouched. The mock's control plane arms the next booking with
      `timeout_booked`: the provider books, then the call times out, and the workflow
      must check get_booking_status rather than book again. Costs a few cents.
"""

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

from errand_agent.api.app import build_handler, build_services
from errand_agent.core.clock import SystemClock
from errand_agent.core.config import Settings
from errand_agent.core.ids import RandomIdGen
from errand_agent.memory.seedgen import DEMO_USER_ID

OUT = Path(__file__).resolve().parents[2] / "docs" / "trace"
SESSION = "sess_design_note"
ASK = (
    "Find me a plumber at home (HSR Layout) for tomorrow, any time between 10:00 and"
    " 13:00, under ₹1500. My kitchen tap is leaking."
)
PICK = "The cheapest one, at its earliest slot, please. Hold it for me."
CONFIRM = "Yes, that's all correct. Please go ahead."
MAX_TURNS = 5


async def record(settings: Settings) -> list[Path]:
    services = build_services(settings, SystemClock(), RandomIdGen())
    try:
        handler = build_handler(services)
        services.errands.create_session(SESSION, user_id=DEMO_USER_ID)

        async def say(text: str):  # type: ignore[no-untyped-def]
            reply = await handler.handle(session_id=SESSION, user_id=DEMO_USER_ID, text=text)
            print(f"you> {text}\nagent [{reply.outcome.kind}]> {reply.outcome.message[:160]}\n")
            return reply

        reply = await say(ASK)
        for _ in range(MAX_TURNS - 1):
            if reply.outcome.approval is not None:
                break
            reply = await say(PICK if reply.outcome.options else CONFIRM)
        if reply.outcome.approval is None:
            raise SystemExit("no approval card was reached; nothing recorded")

        services.control.script_next_book("timeout_booked")  # books, then times out
        booked = await say("yes")
        if booked.outcome.kind != "booked":
            raise SystemExit(f"expected booked, got {booked.outcome.kind}")
    finally:
        services.close()
    return session_traces(settings.trace_dir)


def session_traces(trace_dir: Path) -> list[Path]:
    """This conversation's trace files, oldest run first."""
    runs = []
    for path in trace_dir.glob("*.jsonl"):
        first = json.loads(path.read_text().splitlines()[0])
        if first["session_id"] == SESSION:
            runs.append((first["ts"], path))
    return [path for _, path in sorted(runs)]


def main() -> None:
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(  # pyright: ignore[reportCallIssue]  (paths derived from data_dir)
            data_dir=Path(tmp), reconcile_interval_seconds=0, monitor_interval_seconds=0
        )
        traces = asyncio.run(record(settings))
        if OUT.exists():
            shutil.rmtree(OUT)
        OUT.mkdir(parents=True)
        for i, path in enumerate(traces, start=1):
            first = json.loads(path.read_text().splitlines()[0])
            shutil.copy(path, OUT / f"{i:02d}_{first['trigger']}_{path.name}")
    for path in sorted(OUT.glob("*.jsonl")):
        events = [json.loads(line) for line in path.read_text().splitlines()]
        print(f"{path.name}: {len(events)} events, outcome {events[-1].get('outcome')}")


if __name__ == "__main__":
    main()
