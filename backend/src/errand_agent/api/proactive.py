"""Proactive follow-up, wired into the app: what happens after the monitor's notice.

What: `handle_notice(services, notice)`, the ProviderMonitor's `on_notice` in the app:
      pushes the notice to an open chat and, for "replace", runs the replacement turn
      under the session's lock and pushes its reply (D6).

Why:  The monitor decides and records (workflow/monitor.py); the app decides who hears
      it. An open browser gets it live on `GET /api/events` (`message` events); a closed
      one finds it in the conversation, and any card via `GET /sessions/{id}/pending`.

How:  - The notice is already saved by the monitor; it is pushed at once, even while a
        turn is running, so the user hears the news first.
      - The replacement run takes the session lock, so it waits for a turn in progress
        and never interleaves with one.
      - No model configured: a code-written fallback says so (saved and pushed), so the
        user is never left with only "I'm looking…".
      - Message shape, as in the history plus the reply: {role, content, run_id, reply},
        where reply is the `final` payload (with any approval card) or None.
"""

import logging
from typing import TYPE_CHECKING, Any

from errand_agent.agent.models import ModelNotConfigured
from errand_agent.api.chat_routes import reply_json  # the same `final` shape as a turn
from errand_agent.memory.conversation import ConversationStore
from errand_agent.workflow.monitor import ProviderNotice

if TYPE_CHECKING:
    from errand_agent.api.app import Services

logger = logging.getLogger(__name__)

NO_MODEL = (
    "I couldn't look for a replacement: no model is configured. Tell me when you'd like"
    " to rebook and I'll search then."
)


async def handle_notice(services: "Services", notice: ProviderNotice) -> None:
    hub = services.hub
    hub.publish_message(notice.session_id, _message(notice.message or "", notice.run_id, None))
    if notice.decision != "replace":
        return
    try:
        handler = services.handler()
    except ModelNotConfigured:
        logger.warning("no model configured: can't look for a replacement")
        conversation = ConversationStore(services.agent_db, clock=services.clock, ids=services.ids)
        saved = conversation.append(notice.session_id, "assistant", NO_MODEL, run_id=notice.run_id)
        hub.publish_message(notice.session_id, _message(saved.content, notice.run_id, None))
        return
    async with services.lock_for(notice.session_id):  # waits for a turn in progress
        reply = await handler.handle_provider_cancellation(notice)
    hub.publish_message(
        notice.session_id, _message(reply.outcome.message, reply.run_id, reply_json(reply))
    )


def _message(content: str, run_id: str, reply: dict[str, Any] | None) -> dict[str, Any]:
    return {"role": "assistant", "content": content, "run_id": run_id, "reply": reply}
