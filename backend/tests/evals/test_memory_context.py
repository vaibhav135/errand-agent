"""Eval scenarios: memory, context management, proactive follow-up, limits."""

from pathlib import Path
from typing import Any

import pytest

from errand_agent.workflow.guarded_env import RunBudget
from tests.evals.conftest import EvalRow
from tests.evals.harness import SESSION, USER, Sim
from tests.unit.agent_script import ScriptedModel, call, say
from tests.unit.test_agent_handler import REPLACEMENT
from tests.unit.test_agent_tools import first_slot
from tests.unit.test_workflow_changes import COOLBREEZE


def active(sim: Sim, key: str) -> str:
    """The id of the user's active memory with this key."""
    row = sim.world.conn.execute(
        "SELECT memory_id FROM memories WHERE user_id = ? AND key = ? AND status = 'active'",
        (USER, key),
    ).fetchone()
    return row["memory_id"]


def ids_with_key(sim: Sim, key: str) -> set[str]:
    rows = sim.world.conn.execute("SELECT memory_id FROM memories WHERE key = ?", (key,))
    return {r["memory_id"] for r in rows}


def last_context(sim: Sim) -> dict[str, Any]:
    return sim.events("context_sent")[-1]


@pytest.mark.eval(
    scenario="A preference stated once is used in a later session",
    category="persistent memory",
    expected="saved as explicit with the user's words; in the next session's context",
)
async def test_a_preference_carries_over(tmp_path: Path, report: EvalRow) -> None:
    sim = Sim(
        tmp_path,
        ScriptedModel(
            [call("remember", key="earliest_start", value="10:00", quote="never before 10 am")],
            [say("Noted: nothing before 10:00.")],
            [say("Hi again! Which service do you need?")],
        ),
    )

    await sim.say("Please, never before 10 am.")
    later = sim.new_session("sess_2")
    await sim.say("hi", session=later)

    memory_id = active(sim, "earliest_start")
    row = sim.world.conn.execute(
        "SELECT source, evidence FROM memories WHERE memory_id = ?", (memory_id,)
    ).fetchone()
    context = last_context(sim)
    assert (row["source"], row["evidence"]) == ("explicit", "never before 10 am")
    assert context["session_id"] == later and memory_id in context["memory_ids"]
    assert "Earliest start: 10:00" in context["memories"]
    sim.check()
    report.actual = "explicit memory with the quote; included in sess_2's context"
    report.notes = "the quote is checked against the user's own message (provenance)"


@pytest.mark.eval(
    scenario="A preference corrected, then forgotten",
    category="memory correction/forgetting",
    expected="the correction replaces the old value; once forgotten, never sent again",
)
async def test_correct_then_forget(tmp_path: Path, report: EvalRow) -> None:
    sim = Sim(
        tmp_path,
        ScriptedModel(
            [call("remember", key="earliest_start", value="10:00", quote="after 10 am")],
            [say("Noted.")],
        ),
    )
    # Each later turn is scripted once the memory's id is known. (Looked up here, between
    # turns: the scripted model runs in a worker thread, where this SQLite connection
    # can't be used.)
    steps = sim.model.steps

    await sim.say("I prefer visits after 10 am")
    first = active(sim, "earliest_start")
    steps += [
        [call("correct_memory", memory_id=first, new_value="09:00", quote="make it 9 am")],
        [say("Changed to 9:00.")],
    ]
    await sim.say("Actually, make it 9 am")
    corrected = active(sim, "earliest_start")
    steps += [[call("forget_memory", memory_id=corrected)], [say("Forgotten.")]]
    await sim.say("Forget my time preference")
    steps += [[say("Sure, which area?")]]
    await sim.say("I need a plumber")

    memory_ids = ids_with_key(sim, "earliest_start")
    statuses = {
        r["status"]
        for r in sim.world.conn.execute("SELECT status FROM memories WHERE key = 'earliest_start'")
    }
    after = last_context(sim)
    assert first != corrected and memory_ids == {first, corrected}
    assert statuses == {"superseded", "forgotten"}
    assert not memory_ids & set(after["memory_ids"])  # neither value reaches the model now
    assert "Earliest start" not in after["memories"]
    sim.check()  # invariant 4 checks every context against the times they changed
    report.actual = "10:00 superseded by 09:00, then forgotten; absent from later context"
    report.notes = "the forget scrubs the value; invariant 4 checks each context's timing"


@pytest.mark.eval(
    scenario="A suggestion inferred from bookings, before and after the user confirms it",
    category="explicit vs inferred memory",
    expected="never sent while unconfirmed; used once the user confirms it",
)
async def test_an_unconfirmed_suggestion_stays_out(tmp_path: Path, report: EvalRow) -> None:
    sim = Sim(
        tmp_path,
        ScriptedModel(
            [say("Hi! What can I do for you?")],
            [call("update_requirements", service="ac_servicing")],
            [say("Which area?")],
            [say("Got it.")],
        ),
    )
    sim.seed_demo_user()
    # The seed has two suggestions awaiting review; this one is about AC servicing.
    suggestion = sim.world.conn.execute(
        "SELECT memory_id FROM memories"
        " WHERE status = 'pending_confirmation' AND scope = 'ac_servicing'"
    ).fetchone()["memory_id"]

    await sim.say("hi")
    before = last_context(sim)
    sim.handler._memories.confirm(suggestion)  # pyright: ignore[reportPrivateUsage]  (the Confirm button)
    await sim.say("I need AC servicing")
    await sim.say("At home")
    after = last_context(sim)

    excluded = {m["memory_id"]: m["reason"] for m in before["excluded_memories"]}
    assert suggestion not in before["memory_ids"]
    assert excluded[suggestion] == "pending_confirmation"
    assert suggestion in after["memory_ids"]  # confirmed, and relevant to AC servicing
    sim.check()
    report.actual = "excluded as pending_confirmation; included after Confirm"
    report.notes = "only code shows suggestions to the user; the model never saw it unconfirmed"


@pytest.mark.eval(
    scenario="55 memories, 200 earlier messages and a 300-message session",
    category="context budget",
    expected="every request within the token budget; old sessions and overflow left out, logged",
)
async def test_context_stays_within_budget(tmp_path: Path, report: EvalRow) -> None:
    sim = Sim(tmp_path, ScriptedModel([say("Sure, which area?")]))
    sim.seed_demo_user()
    for i in range(150):  # a long current conversation, ~30 words a message
        sim.conversation.append(SESSION, "user", f"Message {i}: " + "something about my home " * 6)
        sim.conversation.append(SESSION, "assistant", f"Reply {i}: " + "here is what I found " * 6)

    await sim.say("I need a plumber")

    c = last_context(sim)
    tokens, budget = c["tokens"], c["budget"]
    assert tokens["total"] <= budget["total"]
    assert tokens["memories"] <= budget["memories"]
    assert c["messages_out_of_scope"] >= 200  # earlier sessions: never sent
    assert c["messages_dropped"] > 0  # this session's oldest didn't fit
    sim.check()
    report.actual = (
        f"{tokens['total']}/{budget['total']} tokens; {len(c['memory_ids'])} memories sent;"
        f" {c['messages_dropped']} old messages dropped; {c['messages_out_of_scope']} out of scope"
    )
    report.notes = "every request's context is logged (context_sent) with what was left out and why"


@pytest.mark.eval(
    scenario="A provider cancels a booking",
    category="proactive cancellation",
    expected="noticed unprompted; told once; replacement card; no rebooking without approval",
)
async def test_provider_cancellation(tmp_path: Path, report: EvalRow) -> None:
    sim = Sim(
        tmp_path,
        ScriptedModel(
            [call("update_requirements", **REPLACEMENT)],
            [call("search_and_compare")],
            lambda m: [call("propose_booking", option_number=1, slot=first_slot(m))],
            [say("Here's a replacement. Approve it to book.")],
        ),
    )

    (notice,) = await sim.provider_cancels(COOLBREEZE)
    (replacement,) = sim.replacements
    again = await sim.monitor_pass()  # the next pass: nothing new
    assert replacement.outcome.kind == "needs_approval" and sim.agent_bookings() == []
    booked = await sim.approve()

    told = [text for role, text in sim.said() if "cancelled your AC servicing" in text]
    assert notice.decision == "replace" and again == []
    assert len(told) == 1
    assert booked.outcome.kind == "booked" and len(sim.agent_bookings()) == 1
    sim.check()
    report.actual = "notice + replacement card; second pass silent; booked only after Approve"
    report.notes = "detected via list_bookings only (never the mock's event table)"


@pytest.mark.eval(
    scenario="The model keeps calling tools without finishing",
    category="limit exhaustion",
    expected="stopped at the turn limit with an honest message; nothing booked",
)
async def test_a_looping_model_is_stopped(tmp_path: Path, report: EvalRow) -> None:
    loop = [[call("list_memories")] for _ in range(10)]
    sim = Sim(tmp_path, ScriptedModel(*loop), budget=RunBudget(max_model_turns=3))

    reply = await sim.say("What do you remember about me?")

    (hit,) = sim.events("limit_hit")
    assert reply.outcome.kind == "limit_reached"
    assert "Nothing was booked or changed" in reply.outcome.message
    assert hit["limit"] == "model_turns"
    assert sim.agent_bookings() == []
    sim.check()  # invariant 6: the run stayed within its limits
    report.actual = "limit_reached after 3 model turns; honest message saved"
    report.notes = "the meter refuses the 4th request before it is sent"
