"""Eval harness: a simulated user talking to the real agent, offline, plus invariants.

What: `Sim` runs one scenario end to end through the real MessageHandler, workflow and
      mock provider world (temp databases, FakeClock, a scripted model). The simulated
      user can `say`, `approve` / `decline` the waiting card, `pick` a slot, trigger
      `provider_cancels`, and `restart` the app. `violations()` checks the invariants
      that must hold whatever happened; every scenario ends with `check()`.

Why:  The assignment asks for scenarios that assert outcomes *and* invariants ("no
      booking without valid approval"), offline against a scripted model. A scripted
      model can't measure the model's judgment (that's `make smoke`); what these show is
      that the system keeps its guarantees whatever the model does, including a model
      that misbehaves on purpose.

How:  Invariants, checked from the databases and the trace after each scenario:
        1. every booking the agent made traces to a consumed approval for exactly that
           provider and price, and that slot (or a consumed reschedule to it);
        2. at most one booking per errand;
        3. every run finished with a real outcome (never "unspecified"), and every run
           the user started or was told about saved a reply;
        4. no context sent to the model held a memory that was forgotten, superseded,
           rejected or still unconfirmed at that moment;
        5. every context sent stayed within its token budget;
        6. every run stayed within its model-turn and tool-call limits.
"""

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic_ai.models.function import FunctionModel

from errand_agent.agent.handler import MessageHandler, Reply
from errand_agent.memory.conversation import ConversationStore
from errand_agent.memory.seed import seed_demo_user
from errand_agent.mocks.control import MockControl
from errand_agent.mocks.scenario import Scenario
from errand_agent.store.db import connect
from errand_agent.tracing.recorder import Tracer
from errand_agent.workflow.approvals import Approval
from errand_agent.workflow.guarded_env import RunBudget
from errand_agent.workflow.monitor import ProviderMonitor, ProviderNotice
from errand_agent.workflow.reconciler import Reconciler
from tests.unit.agent_script import ScriptedModel, Step, call, say
from tests.unit.test_agent_tools import ASK, FULL, first_slot
from tests.unit.workflow_world import World

SESSION, USER = "sess_1", "user_1"  # the World's session and user

# The provider as the agent sees it: a ToolEnv, or a ScriptedEnv (tests/unit), a proxy that
# forwards to one and fails or crashes on cue. Typed Any because the proxy isn't declared
# as a ToolEnv (it forwards via __getattr__), as in the unit tests.
Env = Any

# Runs that answer the user, or tell them something: each must leave a saved reply.
ANSWERED = {"user_message", "approval_click", "slot_pick", "provider_replacement"}


class Sim:
    def __init__(
        self,
        tmp_path: Path,
        model: ScriptedModel | None = None,
        *,
        scenario: Scenario | None = None,
        env: Env | None = None,
        budget: RunBudget | None = None,
        review_model: FunctionModel | None = None,  # the quarantined review reader
    ) -> None:
        self.review_model = review_model
        # Bookings succeed unless the scenario scripts otherwise: evals are deterministic.
        self.world = World(tmp_path, scenario or Scenario(book_outcomes=["success"] * 5))
        w = self.world
        self.tracer = Tracer(w.clock, w.ids, subscribers=[w.events.append])
        self.model = model or ScriptedModel()
        self.budget = budget or RunBudget()
        self.conversation = ConversationStore(
            self.world.conn, clock=self.world.clock, ids=self.world.ids
        )
        self.handler = self._handler(env)
        self.replacements: list[Reply] = []
        self.crashed: set[str] = set()  # runs the process died in: no reply to save

    def _handler(self, env: Env | None = None) -> MessageHandler:
        w = self.world
        return MessageHandler(
            conn=w.conn,
            clock=w.clock,
            ids=w.ids,
            env=env or w.mock,
            tracer=self.tracer,
            model=FunctionModel(self.model),
            review_model=self.review_model,
            budget=self.budget,
        )

    # --- what the simulated user does ------------------------------------------------------

    def _user_pause(self) -> None:
        """A user takes a moment between actions. Without it every event would share one
        FakeClock instant, and "was this memory forgotten before that context was sent?"
        couldn't be told apart from "in the same turn, after it"."""
        self.world.clock.advance(30)

    async def say(self, text: str, *, session: str = SESSION) -> Reply:
        self._user_pause()
        return await self.handler.handle(session_id=session, user_id=USER, text=text)

    def new_session(self, session_id: str) -> str:
        """Another conversation for the same user (a later visit)."""
        self.world.clock.advance(60)
        self.world.errands.create_session(session_id, user_id=USER)
        return session_id

    def seed_demo_user(self) -> None:
        """The generated demo profile: 55 memories (one suggestion awaiting review) and
        200 messages in 10 earlier sessions. Its user is the World's user_1."""
        assert seed_demo_user(self.world.conn, self.world.clock)

    async def approve(self, *, fingerprint: str | None = None) -> Reply:
        return await self._click(True, fingerprint)

    async def decline(self) -> Reply:
        return await self._click(False, None)

    async def _click(self, approve: bool, fingerprint: str | None) -> Reply:
        self._user_pause()
        card = self.waiting_card()
        return await self.handler.decide(
            session_id=SESSION,
            approval_id=card.approval_id,
            fingerprint=fingerprint or card.fingerprint,
            approve=approve,
        )

    async def pick(self, provider_id: str, slot: datetime) -> Reply:
        self._user_pause()
        return await self.handler.pick(session_id=SESSION, provider_id=provider_id, slot=slot)

    async def provider_cancels(self, booking_id: str) -> list[ProviderNotice]:
        """The provider cancels; the app's monitor notices on its next pass, and a
        "replace" notice runs the replacement turn, as the app wires it."""
        MockControl(self.world.mock).cancel_by_provider(booking_id, "technician unavailable")
        return await self.monitor_pass()

    async def monitor_pass(self) -> list[ProviderNotice]:
        """One pass of the app's provider monitor (it runs every 10 s in the app)."""

        async def on_notice(notice: ProviderNotice) -> None:
            if notice.decision == "replace":
                self.replacements.append(await self.handler.handle_provider_cancellation(notice))

        monitor = ProviderMonitor(
            conn=self.world.conn,
            env=self.world.mock,
            conversations=self.conversation,
            clock=self.world.clock,
            tracer=self.tracer,
            on_notice=on_notice,
        )
        return await monitor.check_once()

    def use_env(self, env: Env) -> None:
        """Talk to the provider through `env` from now on (e.g. a ScriptedEnv that
        crashes or fails on cue), same databases."""
        self.handler = self._handler(env)

    def reopen_mock(self, scenario: Scenario) -> None:
        """The provider world with a different scenario (e.g. slot conflicts)."""
        self.world.mock = self.world.open_mock(scenario)
        self.handler = self._handler()

    def died_mid_run(self) -> None:
        """The process was killed during the latest run (a simulated crash)."""
        self.crashed.add(self.events("run_started")[-1]["run_id"])

    async def restart(self, env: Env | None = None) -> None:
        """A new process on the same databases: a fresh handler, then the startup
        reconcile pass that settles whatever the last one left unknown."""
        self.handler = self._handler(env)
        w = self.world
        await Reconciler(
            conn=w.conn,
            env=env or w.mock,
            errands=w.errands,
            approvals=w.approvals,
            actions=w.actions,
            clock=w.clock,
            tracer=self.tracer,
        ).reconcile_once()

    # --- what can be observed ----------------------------------------------------------------

    def waiting_card(self) -> Any:
        (card,) = self.world.approvals.pending_for_session(SESSION)
        return card

    def agent_bookings(self) -> list[sqlite3.Row]:
        """Bookings the agent made (seeded history has no idempotency key)."""
        with closing(connect(self.world.mock_path)) as conn:  # BUG-9: don't hold it open
            return conn.execute(
                "SELECT * FROM bookings WHERE idempotency_key IS NOT NULL ORDER BY created_at"
            ).fetchall()

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.world.events if e.type == kind]

    def provider_calls(self, tool: str) -> list[dict[str, Any]]:
        """The provider calls the agent made with `tool`, from the trace (status, result)."""
        return [e for e in self.events("tool_call") if e["tool"] == tool]

    def said(self) -> list[tuple[str, str]]:
        return [(m.role, m.content) for m in self.conversation.for_session(SESSION)]

    # --- invariants ----------------------------------------------------------------------

    def check(self) -> None:
        problems = self.violations()
        assert not problems, "invariants broken:\n- " + "\n- ".join(problems)

    def violations(self) -> list[str]:
        return [
            *self._bookings_have_approvals(),
            *self._one_booking_per_errand(),
            *self._runs_end_with_an_outcome(),
            *self._context_has_only_current_memories(),
            *self._context_within_budget(),
            *self._runs_within_limits(),
        ]

    def _bookings_have_approvals(self) -> list[str]:
        out = []
        conn = self.world.conn
        for b in self.agent_bookings():
            row = conn.execute(
                "SELECT a.status, a.details FROM provider_actions p"
                " JOIN approvals a USING (approval_id)"
                " WHERE p.idempotency_key = ? AND p.operation = 'book'",
                (b["idempotency_key"],),
            ).fetchone()
            if row is None:
                out.append(f"booking {b['booking_id']} has no approval behind it")
                continue
            d = json.loads(row["details"])
            if row["status"] != "consumed":
                out.append(f"booking {b['booking_id']}: its approval is {row['status']}")
            if (d["provider_id"], d["price_inr"]) != (b["provider_id"], b["price_inr"]):
                out.append(f"booking {b['booking_id']}: provider/price differ from the approval")
            approved_slots = {_at(d["slot"])} | self._rescheduled_to(b["booking_id"])
            if _at(b["slot"]) not in approved_slots:
                out.append(f"booking {b['booking_id']}: slot {b['slot']} was never approved")
        return out

    def _rescheduled_to(self, booking_id: str) -> set[datetime]:
        rows = self.world.conn.execute(
            "SELECT details FROM approvals WHERE action = 'reschedule' AND status = 'consumed'"
        ).fetchall()
        details = [json.loads(r["details"]) for r in rows]
        return {_at(d["slot"]) for d in details if d.get("booking_id") == booking_id}

    def _one_booking_per_errand(self) -> list[str]:
        rows = self.world.conn.execute(
            "SELECT errand_id, count(*) AS n FROM provider_actions"
            " WHERE operation = 'book' AND state = 'succeeded' GROUP BY errand_id HAVING n > 1"
        ).fetchall()
        return [f"errand {r['errand_id']} booked {r['n']} times" for r in rows]

    def _runs_end_with_an_outcome(self) -> list[str]:
        out = []
        started = {e["run_id"]: e["trigger"] for e in self.events("run_started")}
        finished = {e["run_id"]: e["outcome"] for e in self.events("run_finished")}
        rows = self.world.conn.execute("SELECT run_id FROM messages WHERE role = 'assistant'")
        replied = {r["run_id"] for r in rows}  # in any session (scenarios may open more)
        for run_id, trigger in started.items():
            if run_id in self.crashed or trigger == "test":  # "test": World's own fixture run
                continue
            if run_id not in finished:
                out.append(f"run {run_id} ({trigger}) never finished")
            elif finished[run_id] == "unspecified":
                out.append(f"run {run_id} ({trigger}) ended without an outcome")
            elif trigger in ANSWERED and run_id not in replied:
                out.append(f"run {run_id} ({trigger}) saved no reply")
        return out

    def _context_has_only_current_memories(self) -> list[str]:
        out = []
        rows = self.world.conn.execute("SELECT * FROM memories").fetchall()
        memories = {r["memory_id"]: r for r in rows}
        replaced_at = {
            r["memory_id"]: memories[r["superseded_by"]]["created_at"]
            for r in rows
            if r["superseded_by"] in memories
        }
        for event in self.events("context_sent"):
            at = _at(event["ts"])
            for memory_id in event.get("memory_ids", []):
                m = memories.get(memory_id)
                if m is None:
                    continue
                if m["source"] == "inferred" and not _before(m["confirmed_at"], at):
                    out.append(f"{memory_id} was sent before the user confirmed it")
                if _before(m["forgotten_at"], at):
                    out.append(f"{memory_id} was sent after it was forgotten")
                if _before(replaced_at.get(memory_id), at):
                    out.append(f"{memory_id} was sent after it was replaced")
                if m["status"] == "rejected":
                    out.append(f"{memory_id} was sent although the user rejected it")
        return out

    def _context_within_budget(self) -> list[str]:
        return [
            f"context of run {e['run_id']}: {e['tokens']['total']} > {e['budget']['total']}"
            for e in self.events("context_sent")
            if e["tokens"]["total"] > e["budget"]["total"]
        ]

    def _runs_within_limits(self) -> list[str]:
        b = self.budget
        out = []
        for e in self.events("run_finished"):
            t = e["totals"]
            if t["model_calls"] > b.max_model_turns or t["tool_calls"] > b.max_tool_calls:
                out.append(f"run {e['run_id']} went past its limits: {t}")
        return out


# --- common scripts ------------------------------------------------------------------------


def proposes(*more: Step) -> ScriptedModel:
    """A model that takes the full request, searches, and proposes the first slot of the
    first option; then whatever `more` scripts for later turns."""
    return ScriptedModel(
        [call("update_requirements", **FULL)],
        [call("search_and_compare")],
        lambda m: [call("propose_booking", option_number=1, slot=first_slot(m))],
        [say("I've held it. Approve the card to book.")],
        *more,
    )


async def to_card(sim: Sim) -> Approval:
    """The user asks; the scripted model (see `proposes`) gets to an approval card."""
    reply = await sim.say(ASK)
    assert reply.outcome.kind == "needs_approval" and reply.outcome.approval is not None
    return reply.outcome.approval


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _before(text: str | None, at: datetime) -> bool:
    """Whether the stored time `text` is strictly before `at` (None: never happened).
    Strictly: within one turn the context is built first and a forget may follow at
    the same instant; across turns the simulated user's pause separates them."""
    return text is not None and _at(text) < at
