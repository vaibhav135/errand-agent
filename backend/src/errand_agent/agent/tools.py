"""Agent tools: the only things the model can do, all of them workflow commands.

What: Tool functions over `RunContext[AgentDeps]`: requirements, search & compare,
      propose a booking, decline what's pending, the user's bookings and slots, start a
      cancel / reschedule, memories (remember / list / forget / correct / show
      suggestions) and profile.

Why:  The model understands the user; the workflow decides. There is deliberately no
      tool that books, cancels, reschedules, holds or approves: the model can only
      *propose*, and an approval card is the furthest it can get. Approval itself
      happens in code (button or typed yes), without the model. The one approval tool
      the model has, `decline_pending`, can only take authority away.

How:  - Invalid input (a past date, a slot that wasn't listed, a quote the user never
        said) raises ModelRetry with a readable reason, so the model fixes it instead of
        the workflow guessing.
      - Options are saved on the errand, so "option 2" is the same option next turn,
        and only a slot that was actually listed can be proposed.
      - Memory and booking tools only touch the current user's own records.
      - Results are plain structured data; facts like "not booked yet" are stated in
        the result rather than left for the model to infer. Profile tools return the
        saved field names, never the values.
      - Suggestions (inferred, unconfirmed) never reach the model: `list_memories`
        returns only how many await review, and `show_suggestions` has *code* show
        them to the user, who confirms or rejects in code (docs/DECISIONS.md, D2).
"""

from datetime import date, datetime, time
from typing import Any

from pydantic import ValidationError
from pydantic_ai import ModelRetry, RunContext

from errand_agent.agent.deps import AgentDeps
from errand_agent.agent.loop import trace_model_call
from errand_agent.agent.review_reader import summarise_reviews
from errand_agent.agent.toon import to_toon
from errand_agent.core.clock import IST
from errand_agent.memory.store import Memory, MemoryKey, ProvenanceError, Topic
from errand_agent.providers.models import (
    Booking,
    DateRange,
    ProviderOk,
    SearchFailed,
    SearchOk,
    Service,
)
from errand_agent.workflow.decline import decline_pending as decline
from errand_agent.workflow.errands import ABANDONABLE, Errand
from errand_agent.workflow.ranking import RankedOptions, rank_options
from errand_agent.workflow.requirements import (
    RequirementsPatch,
    TimeWindow,
    date_problems,
    merge,
    missing_required,
    unasked_optional,
)

NOT_YET_BOOKED = "Not booked yet: show these details and ask the user to approve."


def _iso(dt: datetime) -> str:
    return dt.astimezone(IST).isoformat(timespec="minutes")


def _today(deps: AgentDeps) -> date:
    return deps.clock.now().astimezone(IST).date()


# --- requirements --------------------------------------------------------------------


def _book_errand(deps: AgentDeps, *, create: bool) -> Errand | None:
    """The book errand this conversation is working on (optionally creating one)."""
    if deps.errand_id:
        errand = deps.errands.get(deps.errand_id)
        if errand.kind == "book" and errand.state in ABANDONABLE:
            return errand
    latest = deps.errands.latest_open(deps.session_id)
    if latest is not None and latest.kind == "book" and latest.state in ABANDONABLE:
        deps.errand_id = latest.errand_id
        return latest
    if not create:
        return None
    errand = deps.errands.create(deps.session_id, "book")
    deps.errand_id = errand.errand_id
    return errand


async def update_requirements(
    ctx: RunContext[AgentDeps],
    service: Service | None = None,
    area: str | None = None,
    date_start: date | None = None,
    date_end: date | None = None,
    earliest_start: time | None = None,
    latest_start: time | None = None,
    no_time_preference: bool = False,
    budget_max_inr: int | None = None,
    no_budget: bool = False,
    preferred_provider_id: str | None = None,
) -> dict[str, Any]:
    """Record booking details the user gave or corrected this turn (the rest are kept).
    Dates are in IST. Set no_time_preference / no_budget if they have none."""
    deps = ctx.deps
    errand = _book_errand(deps, create=True)
    assert errand is not None
    current = errand.requirements

    fields: dict[str, Any] = {}
    if service is not None:
        fields["service"] = service
    if area is not None:
        fields["area"] = area.strip()
    if date_start is not None or date_end is not None:
        start = date_start or _today(deps)
        fields["date_range"] = {"start": start, "end": date_end or start}
    if no_time_preference:
        fields["time_window"] = "any"
    elif earliest_start is not None or latest_start is not None:
        base = current.time_window if isinstance(current.time_window, TimeWindow) else None
        fields["time_window"] = {
            "earliest": earliest_start or (base.earliest if base else None),
            "latest": latest_start or (base.latest if base else None),
        }
    if no_budget:
        fields["budget_max_inr"] = "any"
    elif budget_max_inr is not None:
        fields["budget_max_inr"] = budget_max_inr
    if preferred_provider_id is not None:
        fields["preferred_provider_id"] = preferred_provider_id

    try:
        patch = RequirementsPatch.model_validate(fields)
    except ValidationError as exc:
        raise ModelRetry(f"invalid requirements: {exc.errors()[0]['msg']}") from exc
    if patch.date_range is not None:
        problems = date_problems(patch.date_range, today=_today(deps))
        if problems:
            raise ModelRetry("; ".join(problems))

    updated = merge(current, patch)
    if updated != current and errand.state in ("comparing", "awaiting_approval"):
        deps.approvals.invalidate_live(errand.errand_id, reason="requirements changed")
        deps.errands.transition(
            errand.errand_id, "requirements_changed", run=deps.run, reason="user changed details"
        )
    deps.errands.set_requirements(errand.errand_id, updated)
    return {
        "errand_id": errand.errand_id,
        "requirements": updated.model_dump(mode="json", exclude_none=True),
        "still_needed": missing_required(updated),
        "can_ask_once": unasked_optional(updated),
    }


# --- search & propose ----------------------------------------------------------------------


async def search_and_compare(ctx: RunContext[AgentDeps]) -> dict[str, Any]:
    """Search and rank providers for the current request, once requirements are complete."""
    deps = ctx.deps
    errand = _book_errand(deps, create=False)
    if errand is None:
        raise ModelRetry("no booking request yet: call update_requirements first")
    r = errand.requirements
    missing = missing_required(r)
    if missing:
        raise ModelRetry(f"still needed before searching: {', '.join(missing)}; ask the user")
    assert r.service is not None and r.area is not None and r.date_range is not None

    result = await deps.env.search_providers(r.service, r.area, r.date_range)
    if isinstance(result, SearchFailed):
        return {"error": result.status, "hint": "ask the user to correct this detail"}
    assert isinstance(result, SearchOk)
    ranked = rank_options(result.providers, r)
    deps.errands.set_options(errand.errand_id, ranked.model_dump_json())
    if ranked.options and errand.state == "collecting":
        deps.errands.transition(
            errand.errand_id, "options_ready", run=deps.run, reason=f"{len(ranked.options)} options"
        )
    return {
        "options": [
            {
                "option": i + 1,
                "provider_id": o.provider_id,
                "name": o.name,
                "price_inr": o.price_inr,
                "rating": o.rating,
                "review_count": o.review_count,
                "verified": o.verified,
                "slots": [_iso(s) for s in o.slots],
                "reasons": o.reasons,
            }
            for i, o in enumerate(ranked.options)
        ],
        "preferred_note": ranked.preferred_note,
        "note": None
        if ranked.options
        else "No provider matches; suggest relaxing the budget, time window or dates.",
    }


async def propose_booking(
    ctx: RunContext[AgentDeps], option_number: int, slot: datetime
) -> dict[str, Any]:
    """Hold one of the listed slots and ask the user to approve the exact provider, slot
    and price. This does NOT book: only the user's approval can."""
    deps = ctx.deps
    errand = _book_errand(deps, create=False)
    if errand is None or errand.state != "comparing":
        raise ModelRetry("search_and_compare first, then propose one of its options")
    raw = deps.errands.options_json(errand.errand_id)
    ranked = RankedOptions.model_validate_json(raw) if raw else RankedOptions(options=[])
    if not 1 <= option_number <= len(ranked.options):
        raise ModelRetry(f"option_number must be between 1 and {len(ranked.options)}")
    option = ranked.options[option_number - 1]
    if slot.tzinfo is None:
        raise ModelRetry("give the slot with its time zone, e.g. 2026-11-07T09:00+05:30")
    if slot not in option.slots:
        raise ModelRetry(
            f"that slot isn't one of option {option_number}'s listed slots: "
            + ", ".join(_iso(s) for s in option.slots)
        )

    proposal = await deps.booking_flow().propose(errand.errand_id, option, slot)
    if proposal.status == "unavailable" or proposal.approval is None:
        return {"status": "unavailable", "reason": proposal.reason}
    deps.cards.append(proposal.approval)
    d = proposal.approval.details
    return {
        "status": "awaiting_user_approval",
        "approval_id": proposal.approval.approval_id,
        "provider": d.provider_name,
        "slot": _iso(d.slot),
        "price_inr": d.price_inr,
        "price_changed_from": proposal.price_changed_from,
        "instruction": NOT_YET_BOOKED,
    }


async def decline_pending(ctx: RunContext[AgentDeps]) -> dict[str, Any]:
    """The user declined what awaits their approval: withdraws every pending card.
    Nothing is booked or changed."""
    deps = ctx.deps
    declined = decline(deps.conn, deps.approvals, deps.errands, deps.session_id, run=deps.run)
    if not declined:
        return {"status": "nothing_pending"}
    return {
        "status": "declined",
        "declined": [
            {
                "action": a.details.action,
                "provider": a.details.provider_name,
                "slot": _iso(a.details.slot),
            }
            for a in declined
        ],
    }


async def get_reviews_summary(ctx: RunContext[AgentDeps], provider_id: str) -> dict[str, Any]:
    """A provider's reviews as labels (themes, red flags, sentiment). Pass red flags on
    to the user as warnings."""
    deps = ctx.deps
    if deps.review_model is None:
        return {"status": "reviews_unavailable"}
    details = await deps.env.get_provider(provider_id)
    if not isinstance(details, ProviderOk):
        return {"status": "provider_not_found"}
    if deps.meter is not None:
        deps.meter.charge_model_turn()  # the reader's call counts toward the run's turns
    # The reader shares this run's usage object, so its own tokens are the difference.
    u = ctx.usage
    before = (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens)
    started = deps.clock.monotonic()
    summary = await summarise_reviews(details.provider, model=deps.review_model, usage=u)
    trace_model_call(
        deps,
        agent="review-reader",
        model=deps.review_model,
        input_tokens=u.input_tokens - before[0],
        output_tokens=u.output_tokens - before[1],
        latency_s=deps.clock.monotonic() - started,
        cache_read_tokens=u.cache_read_tokens - before[2],
        cache_write_tokens=u.cache_write_tokens - before[3],
    )
    if summary is None:
        return {"status": "reviews_unavailable"}
    p = details.provider
    return {
        "provider": p.name,
        "rating": p.rating,
        "review_count": p.review_count,
        **summary.model_dump(),
    }


# --- existing bookings ---------------------------------------------------------------------


async def _bookings(deps: AgentDeps) -> list[Booking]:
    return await deps.env.list_bookings(deps.user_id)


async def find_my_bookings(ctx: RunContext[AgentDeps]) -> list[dict[str, Any]] | str:
    """The user's past and upcoming bookings (for "same electrician as last time",
    rescheduling or cancelling)."""
    now = ctx.deps.clock.now()
    rows = [
        {
            "booking_id": b.booking_id,
            "provider_id": b.provider_id,
            "provider_name": b.provider_name,
            "service": b.service,
            "slot": _iso(b.slot),
            "status": b.status,
            "when": "upcoming" if b.slot > now else "past",
        }
        for b in await _bookings(ctx.deps)
    ]
    return to_toon("bookings", rows) if ctx.deps.result_format == "toon" else rows


async def available_slots(
    ctx: RunContext[AgentDeps], booking_id: str, date_start: date, date_end: date
) -> dict[str, Any]:
    """Free slots of the provider of one of the user's bookings (to reschedule it)."""
    deps = ctx.deps
    booking = next((b for b in await _bookings(deps) if b.booking_id == booking_id), None)
    if booking is None:
        return {"status": "booking_not_found"}
    try:
        window = DateRange(start=date_start, end=date_end)
    except ValidationError as exc:
        raise ModelRetry(f"invalid dates: {exc.errors()[0]['msg']}") from exc
    problems = date_problems(window, today=_today(deps))
    if problems:
        raise ModelRetry("; ".join(problems))
    details = await deps.env.get_provider(booking.provider_id)
    if not isinstance(details, ProviderOk) or not details.provider.areas:
        return {"status": "provider_unavailable"}
    search = await deps.env.search_providers(booking.service, details.provider.areas[0], window)
    if not isinstance(search, SearchOk):
        return {"status": "search_failed"}
    hit = next((p for p in search.providers if p.provider_id == booking.provider_id), None)
    return {
        "provider": booking.provider_name,
        "slots": [_iso(s) for s in hit.available_slots] if hit else [],
    }


async def start_cancel(ctx: RunContext[AgentDeps], booking_id: str) -> dict[str, Any]:
    """Ask the user to approve cancelling one of their bookings. Does NOT cancel."""
    deps = ctx.deps
    if booking_id not in {b.booking_id for b in await _bookings(deps)}:
        return {"status": "booking_not_found"}
    errand = deps.errands.create(deps.session_id, "cancel", booking_id=booking_id)
    deps.errand_id = errand.errand_id
    proposal = await deps.change_flow().propose_cancel(errand.errand_id)
    if proposal.approval is None:
        return {"status": proposal.status}
    deps.cards.append(proposal.approval)
    return {
        "status": "awaiting_user_approval",
        "approval_id": proposal.approval.approval_id,
        "instruction": "Not cancelled yet: ask the user to approve the cancellation.",
    }


async def start_reschedule(
    ctx: RunContext[AgentDeps], booking_id: str, new_slot: datetime
) -> dict[str, Any]:
    """Ask the user to approve moving one of their bookings to a new slot. Does NOT move it."""
    deps = ctx.deps
    if new_slot.tzinfo is None:
        raise ModelRetry("give the slot with its time zone, e.g. 2026-11-07T10:00+05:30")
    if booking_id not in {b.booking_id for b in await _bookings(deps)}:
        return {"status": "booking_not_found"}
    errand = deps.errands.create(deps.session_id, "reschedule", booking_id=booking_id)
    deps.errand_id = errand.errand_id
    proposal = await deps.change_flow().propose_reschedule(errand.errand_id, new_slot)
    if proposal.approval is None:
        return {"status": proposal.status}
    deps.cards.append(proposal.approval)
    return {
        "status": "awaiting_user_approval",
        "approval_id": proposal.approval.approval_id,
        "new_slot": _iso(new_slot),
        "instruction": "Not moved yet: ask the user to approve the new time.",
    }


# --- memory ---------------------------------------------------------------------------------


def _own_memory(deps: AgentDeps, memory_id: str) -> Memory:
    try:
        m = deps.memories.get(memory_id)
    except KeyError as exc:
        raise ModelRetry(f"no memory {memory_id}; call list_memories") from exc
    if m.user_id != deps.user_id:
        raise ModelRetry(f"no memory {memory_id}; call list_memories")
    return m


async def remember(
    ctx: RunContext[AgentDeps],
    key: MemoryKey,
    value: str | int | dict[str, str],
    quote: str,
    scope: str = "global",
    topic: Topic | None = None,
) -> dict[str, Any]:
    """Save a lasting preference the user stated in THIS message (not a one-off detail
    of this booking). `quote` must be copied exactly from the user's message."""
    deps = ctx.deps
    try:
        m = deps.memories.remember(
            deps.user_id,
            key,
            value,
            scope=scope,
            topic=topic,
            quote=quote,
            user_message=deps.user_message,
            run=deps.run,
        )
    except ProvenanceError as exc:
        raise ModelRetry(
            "the quote must be copied exactly from the user's latest message; only remember"
            " what the user themselves said"
        ) from exc
    except (ValidationError, ValueError) as exc:
        raise ModelRetry(f"can't remember that: {exc}") from exc
    return {"saved": m.memory_id, "key": m.key, "scope": m.scope}


def _personal(m: Memory) -> bool:
    """A note about the user's life (food, travel, work...), not about home services."""
    return m.key == "note" and m.topic != "home_services"


async def list_memories(
    ctx: RunContext[AgentDeps], include_personal_notes: bool = False
) -> dict[str, Any]:
    """The user's saved booking preferences, how many personal notes there are (set
    include_personal_notes to list them, only if asked) and how many suggestions await
    their review."""
    remembered = ctx.deps.memories.list_for_user(ctx.deps.user_id)
    active = [m for m in remembered if m.status == "active"]
    sent = [m for m in active if include_personal_notes or not _personal(m)]
    result: dict[str, Any] = {
        "saved": [
            {
                "memory_id": m.memory_id,
                "key": m.key,
                "scope": m.scope,
                "value": m.value.model_dump() if hasattr(m.value, "model_dump") else str(m.value),
                "source": m.source,
                **({"topic": m.topic} if m.topic else {}),
            }
            for m in sent
        ],
        "suggestions_awaiting_review": sum(m.status == "pending_confirmation" for m in remembered),
    }
    if not include_personal_notes:
        # Counted, not sent: the demo user's 36 notes about food, travel or work cost
        # ~2,700 tokens on every call (more than the 1,500 working room), for nothing a
        # booking needs. Measured: ~3,470 -> ~780 (estimated tokens).
        result["personal_notes"] = sum(_personal(m) for m in active)
    if ctx.deps.result_format == "toon":
        rows = [{**row, "topic": row.get("topic")} for row in result.pop("saved")]  # uniform
        counts = "\n".join(f"{k}: {v}" for k, v in result.items())
        return to_toon("saved", rows) + "\n" + counts  # type: ignore[return-value]
    return result


async def show_suggestions(ctx: RunContext[AgentDeps]) -> dict[str, Any]:
    """Show the user their pending suggestions to confirm or reject. You learn only how
    many were shown."""
    deps = ctx.deps
    pending = [
        m for m in deps.memories.list_for_user(deps.user_id) if m.status == "pending_confirmation"
    ]
    deps.suggestions = pending
    return {"shown": len(pending)}


async def forget_memory(ctx: RunContext[AgentDeps], memory_id: str) -> dict[str, Any]:
    """Forget a saved preference or suggestion (the user asked to)."""
    _own_memory(ctx.deps, memory_id)
    ctx.deps.memories.forget(memory_id, run=ctx.deps.run)
    return {"forgotten": memory_id}


async def correct_memory(
    ctx: RunContext[AgentDeps], memory_id: str, new_value: str | int | dict[str, str], quote: str
) -> dict[str, Any]:
    """Replace a saved preference with what the user just said (`quote` copied exactly)."""
    _own_memory(ctx.deps, memory_id)
    try:
        m = ctx.deps.memories.correct(
            memory_id,
            new_value,
            quote=quote,
            user_message=ctx.deps.user_message,
            run=ctx.deps.run,
        )
    except ProvenanceError as exc:
        raise ModelRetry("the quote must be copied exactly from the user's latest message") from exc
    except (ValidationError, ValueError) as exc:
        raise ModelRetry(f"can't correct that: {exc}") from exc
    return {"saved": m.memory_id, "replaced": memory_id}


# --- profile ---------------------------------------------------------------------------------


async def update_profile(
    ctx: RunContext[AgentDeps],
    name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
) -> dict[str, Any]:
    """Save contact details the user typed in THIS message (copied exactly)."""
    deps = ctx.deps
    try:
        deps.profiles.update(
            deps.user_id,
            name=name,
            phone=phone,
            email=email,
            user_message=deps.user_message,
            run=deps.run,
        )
    except ProvenanceError as exc:
        raise ModelRetry("only save details the user typed in their latest message") from exc
    except ValueError as exc:
        raise ModelRetry(str(exc)) from exc
    saved: list[str] = [
        f for f, v in (("name", name), ("phone", phone), ("email", email)) if v is not None
    ]
    return {"saved": saved}


async def add_address(
    ctx: RunContext[AgentDeps], label: str, line: str, area: str, make_default: bool = False
) -> dict[str, Any]:
    """Save an address the user typed in THIS message: a label (e.g. Home), the street
    line and the area/locality, each copied exactly."""
    deps = ctx.deps
    try:
        a = deps.profiles.add_address(
            deps.user_id,
            label=label,
            line=line,
            area=area,
            user_message=deps.user_message,
            make_default=make_default,
            run=deps.run,
        )
    except ProvenanceError as exc:
        raise ModelRetry("only save an address the user typed in their latest message") from exc
    return {"saved": a.label, "area": a.area, "default": a.is_default}


ALL_TOOLS = [
    update_requirements,
    search_and_compare,
    propose_booking,
    decline_pending,
    get_reviews_summary,
    find_my_bookings,
    available_slots,
    start_cancel,
    start_reschedule,
    remember,
    list_memories,
    forget_memory,
    show_suggestions,
    correct_memory,
    update_profile,
    add_address,
]
