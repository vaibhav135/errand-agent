"""Suggestions: run the inference rules over the user's real booking history.

What: `suggest_from_history(env, memories, user_id)` fetches `list_bookings`, applies
      `infer_from_bookings`, and stores each result as a pending suggestion.

Why:  The rules are pure; this is the one place that connects them to the provider's
      record and the memory store.

How:  Every suggestion goes through `MemoryStore.propose_inferred`, so it lands as
      `pending_confirmation`, and anything forgotten, rejected or already known is
      skipped there: running this repeatedly creates nothing new.
"""

from errand_agent.memory.inference import infer_from_bookings
from errand_agent.memory.store import Memory, MemoryStore
from errand_agent.providers.tool_env import ToolEnv
from errand_agent.tracing.recorder import RunTrace


async def suggest_from_history(
    env: ToolEnv, memories: MemoryStore, user_id: str, *, run: RunTrace | None = None
) -> list[Memory]:
    created: list[Memory] = []
    for s in infer_from_bookings(await env.list_bookings(user_id)):
        m = memories.propose_inferred(
            user_id,
            s.key,
            s.value,
            scope=s.scope,
            reason=s.reason,
            run=run,
        )
        if m is not None:
            created.append(m)
    return created
