"""Measure how long the agent takes to reply: time to first word and total, per turn.

What: `make latency` (or `uv run python scripts/measure_latency.py --effort low --cache`)
      runs a few live turns through the real handler and prints, per turn: seconds to
      the first streamed word, total seconds, model calls, input tokens (and how many
      came from the prompt cache), output tokens, cost, and the start of the reply.

Why:  D5 / BUGS.md: a "hello" took 3.5-8.6 s before its first word. Effort and prompt
      caching are the levers; this shows what each does on our real prompt, tools and
      memories, instead of guessing. Latency is noisy, so "hello" runs several times.

How:  - A throwaway data directory (seeded demo user, fresh mock world), so your own
        databases are untouched; the handler is the one the app uses.
      - Turns: "hello" x --runs (each in its own session), then a plumber search and a
        booking request in one session. The booking request ends at the approval card:
        approval is enforced in code, so nothing is booked.
      - Time to first word = the first `on_text` callback; total = until the reply.
        Token and cache counts come from the run's `model_call` trace events.
      - Live model calls cost money (a few cents per run on the default Sonnet).
"""

import argparse
import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from errand_agent.agent.handler import MessageHandler
from errand_agent.api.app import build_handler, build_services
from errand_agent.core.clock import SystemClock
from errand_agent.core.config import Settings
from errand_agent.core.ids import RandomIdGen
from errand_agent.memory.seedgen import DEMO_USER_ID

SEARCH = "Find me a plumber in HSR Layout for tomorrow morning"
BOOK = "Book the cheapest one for tomorrow at 11"


def run_events(trace_dir: Path, run_id: str, kind: str) -> list[dict[str, Any]]:
    path = trace_dir / f"{run_id}.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines()]
    return [e for e in events if e["type"] == kind]


async def turn(
    handler: MessageHandler, trace_dir: Path, session_id: str, text: str
) -> dict[str, Any]:
    first: list[float] = []

    async def on_text(_chunk: str) -> None:
        if not first:
            first.append(time.perf_counter())

    started = time.perf_counter()
    reply = await handler.handle(
        session_id=session_id, user_id=DEMO_USER_ID, text=text, on_text=on_text
    )
    total = time.perf_counter() - started
    calls = run_events(trace_dir, reply.run_id, "model_call")
    tools = [e["tool"] for e in run_events(trace_dir, reply.run_id, "agent_tool")]
    return {
        "prompt": text,
        "first_word_s": round(first[0] - started, 2) if first else None,
        "total_s": round(total, 2),
        "model_calls": len(calls),
        "input_tokens": sum(c["input_tokens"] for c in calls),
        "cache_read_tokens": sum(c.get("cache_read_tokens", 0) for c in calls),
        "cache_write_tokens": sum(c.get("cache_write_tokens", 0) for c in calls),
        "output_tokens": sum(c["output_tokens"] for c in calls),
        "cost_usd": round(sum(c["cost_usd"] or 0 for c in calls), 4),
        "outcome": reply.outcome.kind,
        "tools": tools,  # which steps the model took, to compare settings on quality
        "reply": reply.outcome.message.replace("\n", " ")[:90],
    }


async def measure(settings: Settings, runs: int) -> list[dict[str, Any]]:
    services = build_services(settings, SystemClock(), RandomIdGen())
    try:
        handler = build_handler(services)
        results = []
        for i in range(runs):
            session = f"sess_latency_hello_{i}"
            services.errands.create_session(session, user_id=DEMO_USER_ID)
            results.append(await turn(handler, settings.trace_dir, session, "hello"))
        session = "sess_latency_errand"
        services.errands.create_session(session, user_id=DEMO_USER_ID)
        for text in (SEARCH, BOOK):
            results.append(await turn(handler, settings.trace_dir, session, text))
        return results
    finally:
        services.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Time to first word and total, per turn.")
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--cache", action="store_true", help="prompt-cache the stable prefix")
    parser.add_argument("--runs", type=int, default=3, help='how many "hello" turns')
    args = parser.parse_args()
    os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(  # pyright: ignore[reportCallIssue]  (paths derived from data_dir)
            data_dir=Path(tmp),
            effort=args.effort,
            prompt_cache=args.cache,
            reconcile_interval_seconds=0,
        )
        label = f"model={settings.model} effort={args.effort or 'default'} cache={args.cache}"
        print(label)
        results = asyncio.run(measure(settings, args.runs))

    cols = ("first_word_s", "total_s", "model_calls", "input_tokens", "cache_read_tokens",
            "output_tokens", "cost_usd", "outcome")  # fmt: skip
    print("prompt".ljust(14) + "".join(c.replace("_tokens", "").rjust(13) for c in cols))
    for r in results:
        print(r["prompt"][:13].ljust(14) + "".join(str(r[c]).rjust(13) for c in cols))
    for r in results:
        print(f"  {r['prompt'][:24]!r}: tools {r['tools']}\n      {r['reply']}")
    print(f"total cost: ${sum(r['cost_usd'] for r in results):.4f}")


if __name__ == "__main__":
    main()
