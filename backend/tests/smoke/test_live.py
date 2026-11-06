"""Live smoke test: the real model, one real conversation (`make smoke`, a few cents).

What: a canned user asks for a plumber with every detail given, answers follow-ups in
      words (at most MAX_TURNS), and the test checks the conversation reaches an
      approval card, nothing is booked before "yes", and exactly one booking after it.

Why:  The evals run against a scripted model, so they prove the code's guarantees but
      not that the real model can actually drive the tools to a card. This is the one
      check of the model's judgment end to end. Marked `smoke`: excluded from the
      default run (pyproject addopts), skipped without a key, never part of `make test`.

How:  The app's own services on a throwaway data directory (seeded demo user), with the
      next booking scripted to succeed so the outcome after "yes" is deterministic.
      On failure the transcript is in the assertion message.
"""

from pathlib import Path

import pytest
from pydantic_ai import models

from errand_agent.api.app import build_handler, build_services
from errand_agent.core.clock import SystemClock
from errand_agent.core.config import Settings
from errand_agent.core.ids import RandomIdGen
from errand_agent.memory.conversation import ConversationStore
from errand_agent.memory.seedgen import DEMO_USER_ID
from errand_agent.store.db import connect

MAX_TURNS = 5
ASK = (
    "Find me a plumber at home (HSR Layout) for tomorrow, any time between 10:00 and"
    " 13:00, under ₹1500. My kitchen tap is leaking."
)
PICK = "The cheapest one, at its earliest slot, please. Hold it for me."
CONFIRM = "Yes, that's all correct. Please go ahead."

pytestmark = pytest.mark.smoke


async def test_a_real_conversation_reaches_a_card_and_books_only_on_yes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(  # pyright: ignore[reportCallIssue]  (paths derived from data_dir)
        data_dir=tmp_path, reconcile_interval_seconds=0, monitor_interval_seconds=0
    )
    if settings.anthropic_api_key is None and settings.model.startswith("anthropic:"):
        pytest.skip("no ANTHROPIC_API_KEY in backend/.env")
    monkeypatch.setattr(models, "ALLOW_MODEL_REQUESTS", True)  # the suite blocks them
    services = build_services(settings, SystemClock(), RandomIdGen())
    try:
        handler = build_handler(services)
        session = "sess_smoke"
        services.errands.create_session(session, user_id=DEMO_USER_ID)
        services.control.script_next_book("success")

        def bookings() -> int:
            conn = connect(settings.mock_db_path)
            try:
                return conn.execute(
                    "SELECT count(*) FROM bookings WHERE idempotency_key IS NOT NULL"
                ).fetchone()[0]
            finally:
                conn.close()

        def transcript() -> str:
            store = ConversationStore(services.agent_db, clock=services.clock, ids=services.ids)
            return "\n".join(f"{m.role}: {m.content}" for m in store.for_session(session))

        reply = await handler.handle(session_id=session, user_id=DEMO_USER_ID, text=ASK)
        for _ in range(MAX_TURNS - 1):
            if reply.outcome.approval is not None:
                break
            follow_up = PICK if reply.outcome.options else CONFIRM
            reply = await handler.handle(session_id=session, user_id=DEMO_USER_ID, text=follow_up)

        assert reply.outcome.kind == "needs_approval", f"no card yet:\n{transcript()}"
        assert bookings() == 0, f"booked before approval:\n{transcript()}"

        booked = await handler.handle(session_id=session, user_id=DEMO_USER_ID, text="yes")

        assert booked.outcome.kind == "booked", f"not booked after yes:\n{transcript()}"
        assert bookings() == 1
    finally:
        services.close()
