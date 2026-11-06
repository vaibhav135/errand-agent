"""Memory selection: which preferences reach the model, within a token budget.

What: `select_memories(memories, service=, budget_tokens=)` returns the included
      memories, every excluded one with its reason, the rendered text and its tokens.

Why:  "Only relevant, current preferences may reach the model. Forgotten facts and
      unconfirmed inferred facts must never be included." Deciding that in code makes
      it provable: the reasons are logged per turn and tested.

How:  - Excluded by status: forgotten, pending_confirmation, superseded, rejected.
      - Excluded as irrelevant: a preference scoped to another service, and notes
        whose topic isn't home_services. With no service known yet, only global
        preferences count.
      - Included in priority order (preferred/avoided provider, time window, budget,
        default address, notes), newest first within a priority, while they fit the
        budget; the rest are `over_budget`.
      - Each line says where the fact came from: "(you told me)" for explicit,
        "(inferred, you confirmed)" for a confirmed inference.
      - Output order doesn't depend on input order.
"""

from datetime import time
from typing import Literal

from pydantic import BaseModel, ConfigDict

from errand_agent.memory.store import Memory, ProviderRef
from errand_agent.memory.tokens import estimate_tokens

ExclusionReason = Literal[
    "forgotten", "pending_confirmation", "superseded", "rejected", "irrelevant", "over_budget"
]

_PRIORITY = {
    "preferred_provider": 0,
    "avoided_provider": 0,
    "earliest_start": 1,
    "latest_start": 1,
    "budget_max_inr": 2,
    "default_address": 3,
    "note": 4,
}
_SERVICE_LABEL = {
    "plumber": "plumber",
    "electrician": "electrician",
    "ac_servicing": "AC servicing",
    "cleaning": "cleaning",
    "carpenter": "carpenter",
}


class Selection(BaseModel):
    model_config = ConfigDict(frozen=True)

    included: list[Memory]
    excluded: list[tuple[str, ExclusionReason]]
    text: str
    tokens: int


def _relevant(m: Memory, service: str | None) -> bool:
    if m.key == "note":
        return m.topic == "home_services"
    return m.scope == "global" or m.scope == service


def render(m: Memory) -> str:
    """A memory as the model sees it: the fact and where it came from."""
    origin = "you told me" if m.source == "explicit" else "inferred, you confirmed"
    return f"- {describe(m)} ({origin})"


def describe(m: Memory) -> str:
    """The fact alone, in words (e.g. "Usual electrician: Sparks Electricals")."""
    v = m.value
    svc = _SERVICE_LABEL.get(m.scope, m.scope)
    match m.key:
        case "earliest_start" | "latest_start":
            assert isinstance(v, time)
            label = "Earliest start" if m.key == "earliest_start" else "Latest start"
            fact = f"{label}: {v:%H:%M}"
        case "budget_max_inr":
            fact = f"Budget for {svc}: up to ₹{v}"
        case "preferred_provider" | "avoided_provider":
            assert isinstance(v, ProviderRef)
            fact = (
                f"Usual {svc}: {v.name}"
                if m.key == "preferred_provider"
                else (f"Avoid for {svc}: {v.name}")
            )
        case "default_address":
            fact = f"Default address: {v}"
        case "note":
            fact = f"Note: {v}"
    return fact


def select_memories(
    memories: list[Memory], *, service: str | None, budget_tokens: int
) -> Selection:
    excluded: list[tuple[str, ExclusionReason]] = []
    candidates: list[Memory] = []
    for m in memories:
        if m.status != "active":
            excluded.append((m.memory_id, m.status))  # type: ignore[arg-type]
        elif not _relevant(m, service):
            excluded.append((m.memory_id, "irrelevant"))
        else:
            candidates.append(m)

    candidates.sort(key=lambda m: m.memory_id)
    candidates.sort(key=lambda m: m.updated_at, reverse=True)
    candidates.sort(key=lambda m: _PRIORITY[m.key])

    included: list[Memory] = []
    lines: list[str] = []
    used = 0
    for m in candidates:
        line = render(m)
        cost = estimate_tokens(line + "\n")
        if used + cost > budget_tokens:
            excluded.append((m.memory_id, "over_budget"))
            continue
        included.append(m)
        lines.append(line)
        used += cost

    return Selection(
        included=included,
        excluded=sorted(excluded),
        text="\n".join(lines),
        tokens=used,
    )
