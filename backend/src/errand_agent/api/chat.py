"""Terminal chat: talk to the live agent before (and alongside) the web UI.

What: `make chat` -> `main()`: a read-reply loop for the demo user over the same
      MessageHandler, databases and traces the web API uses. `render_card()` draws
      an approval card; each reply ends with a footer read from the run's trace.

Why:  To see the *real* model use our tools while the only moving parts are the
      agent and the workflow, so a surprise has one suspect, and a prompt change can
      be retried in seconds. It also makes the trace of every turn one path away.

How:  - Standard library only: `input()` with `readline` imported gives line editing
        and history; output is plain text, with the footer dimmed (ANSI) only when
        writing to a real terminal, so tests compare plain strings.
      - Like the API, startup runs one reconcile pass (restart recovery), then a new
        session starts for the demo user.
      - The card is drawn from the approval's stored details, never from the model's
        text, exactly as the web UI will.
      - Anything starting with "/" is a chat command, handled here: it never reaches
        the model or the saved conversation (BUG-4).
      - Default models in use -> a notice first; a missing key -> what to set, exit 2.
"""

import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from errand_agent.agent.handler import MessageHandler, Reply
from errand_agent.agent.models import ModelNotConfigured
from errand_agent.api.app import Services, build_handler, build_services
from errand_agent.core.clock import Clock, SystemClock
from errand_agent.core.config import Settings, default_model_notice, load_settings
from errand_agent.core.ids import RandomIdGen
from errand_agent.memory.seedgen import DEMO_USER_ID
from errand_agent.workflow.approvals import Approval
from errand_agent.workflow.outcomes import when

CARD_WIDTH = 56
TITLES = {
    "book": "Approve booking?",
    "cancel": "Approve cancellation?",
    "reschedule": "Approve new time?",
}
DIM, RESET = "\033[2m", "\033[0m"
QUIT_COMMANDS = ("/quit", "/exit")


def _rule(prefix: str, text: str) -> str:
    head = f"{prefix} {text} "
    return head + "─" * max(3, CARD_WIDTH - len(head))


def render_card(approval: Approval) -> str:
    d = approval.details
    return "\n".join(
        [
            _rule("┌─", TITLES[approval.action]),
            f"│ {d.provider_name} · {when(d.slot)} · ₹{d.price_inr}",
            _rule("└─", 'reply "yes" to approve, "no" to decline'),
        ]
    )


def footer(reply: Reply, trace_dir: Path) -> str:
    path = trace_dir / f"{reply.run_id}.jsonl"
    finished: dict[str, Any] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event["type"] == "run_finished":
                finished = event
    t = finished.get("totals", {})
    calls = t.get("model_calls", 0)
    tokens = t.get("input_tokens", 0) + t.get("output_tokens", 0)
    cost = t.get("cost_usd")
    priced = f"${cost:.4f}" if cost is not None and t.get("cost_complete") else "cost n/a"
    seconds = t.get("duration_ms", 0) / 1000
    return (
        f"{reply.outcome.kind} · {calls} model call{'' if calls == 1 else 's'}"
        f" · {tokens:,} tokens · {priced} · {seconds:.1f} s · {path}"
    )


async def chat_loop(
    handler: MessageHandler,
    services: Services,
    *,
    read: Callable[[], str],
    write: Callable[[str], None],
    styled: bool = False,
) -> None:
    session_id = services.ids.new("sess")
    services.errands.create_session(session_id, user_id=DEMO_USER_ID)
    write(f"Errand chat · session {session_id} · /quit or Ctrl-D to leave")
    while True:
        try:
            text = read().strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not text:
            continue
        if text.startswith("/"):  # a chat command, never a message to the agent
            if text in QUIT_COMMANDS:
                return
            write(f"Unknown command {text.split()[0]}. Commands: {', '.join(QUIT_COMMANDS)}")
            continue
        reply = await handler.handle(session_id=session_id, user_id=DEMO_USER_ID, text=text)
        write(reply.outcome.message)
        if reply.outcome.approval is not None:
            write(render_card(reply.outcome.approval))
        line = footer(reply, services.settings.trace_dir)
        write(f"{DIM}{line}{RESET}" if styled else line)


async def _start(
    services: Services,
    handler: MessageHandler,
    read: Callable[[], str],
    write: Callable[[str], None],
    styled: bool,
) -> None:
    await services.reconciler.reconcile_once()  # restart recovery, as the API does
    await chat_loop(handler, services, read=read, write=write, styled=styled)


def main(
    *,
    settings: Settings | None = None,
    clock: Clock | None = None,
    read: Callable[[], str] | None = None,
    write: Callable[[str], None] | None = None,
) -> int:
    if read is None:
        import readline  # noqa: F401  (side effect: line editing + history for input())

        read = lambda: input("\nyou > ")  # noqa: E731
    styled = write is None and sys.stdout.isatty()
    write = write or print
    settings = settings or load_settings()
    notice = default_model_notice(settings)
    if notice is not None:
        write(notice)
    services = build_services(settings, clock or SystemClock(), RandomIdGen())
    try:
        try:
            handler = build_handler(services)
        except ModelNotConfigured as exc:
            write(f"Can't start the chat: {exc}")
            return 2
        asyncio.run(_start(services, handler, read, write, styled))
        return 0
    finally:
        services.close()


if __name__ == "__main__":
    sys.exit(main())
