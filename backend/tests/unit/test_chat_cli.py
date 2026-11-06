import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from errand_agent.api.app import Services, build_handler, build_services
from errand_agent.api.chat import ChatState, chat_loop, main, numbered, render_card
from errand_agent.core.clock import FakeClock
from errand_agent.core.config import Settings
from errand_agent.core.ids import SequentialIdGen
from errand_agent.memory.store import MemoryStore
from errand_agent.workflow.approvals import Approval, ApprovalDetails
from tests.unit.agent_script import ScriptedModel, call, say

COOLBREEZE = "bk_hist_coolbreeze"  # seeded for the demo user: in 5 days at 10:00 IST, ₹1200


class Terminal:
    """Plays the user at the keyboard: hands out lines, then EOF (Ctrl-D)."""

    def __init__(self, *lines: str) -> None:
        self.lines = list(lines)
        self.out: list[str] = []

    def read(self) -> str:
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)

    def write(self, text: str) -> None:
        self.out.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.out)


@pytest.fixture
def services(app_settings: Settings, app_clock: FakeClock) -> Iterator[Services]:
    s = build_services(app_settings, app_clock, SequentialIdGen())
    yield s
    s.close()


async def chat(services: Services, terminal: Terminal, model: ScriptedModel) -> None:
    handler = build_handler(services, model=model.model())
    await chat_loop(
        handler,
        services,
        read=terminal.read,
        write=terminal.write,
    )


# --- the approval card is drawn by code ---------------------------------------------------


def card(action: str) -> Approval:
    return Approval(
        approval_id="appr_1",
        errand_id="err_1",
        action=action,  # type: ignore[arg-type]
        fingerprint="f",
        details=ApprovalDetails(
            action=action,  # type: ignore[arg-type]
            provider_id="plm_quickfix",
            provider_name="QuickFix Plumbing",
            slot=datetime(2026, 11, 7, 3, 30, tzinfo=UTC),
            price_inr=699,
            booking_id=None if action == "book" else "bk_1",
        ),
        status="pending",
        created_at=datetime(2026, 11, 5, tzinfo=UTC),
        decided_at=None,
        decided_via=None,
        reason=None,
    )


@pytest.mark.parametrize(
    ("action", "title"),
    [("book", "Approve booking?"), ("cancel", "Approve cancellation?"),
     ("reschedule", "Approve new time?")],
)  # fmt: skip
def test_card_shows_the_exact_details(action: str, title: str) -> None:
    text = render_card(card(action))

    assert title in text
    assert "QuickFix Plumbing · Sat 07 Nov, 09:00 · ₹699" in text  # slot shown in IST
    assert 'reply "yes" to approve, "no" to decline' in text


# --- a conversation -------------------------------------------------------------------------


async def test_model_proposes_user_types_yes(services: Services) -> None:
    terminal = Terminal("Cancel my AC servicing", "yes", "/quit")
    model = ScriptedModel([call("start_cancel", booking_id=COOLBREEZE)], [say("Cancel this one?")])

    await chat(services, terminal, model)

    out = terminal.text
    assert "Cancel this one?" in out
    assert "Approve cancellation?" in out
    assert "CoolBreeze AC Care · Tue 10 Nov, 10:00 · ₹1200" in out
    assert "Cancelled your CoolBreeze AC Care booking" in out


async def test_every_reply_has_a_footer_from_its_trace(services: Services) -> None:
    terminal = Terminal("hi")
    model = ScriptedModel([say("Hello!")])

    await chat(services, terminal, model)

    footer = terminal.out[-1]
    assert "answered" in footer
    assert "1 model call" in footer and "tokens" in footer
    trace = Path(footer.split()[-1])  # the run's trace file, to open when debugging
    assert trace.suffix == ".jsonl" and '"run_finished"' in trace.read_text()


async def test_ctrl_d_ends_the_chat_quietly(services: Services) -> None:
    terminal = Terminal()  # EOF straight away

    await chat(services, terminal, ScriptedModel())  # returns instead of raising


async def test_a_working_line_shows_before_each_reply(services: Services) -> None:
    terminal = Terminal("hi")

    await chat(services, terminal, ScriptedModel([say("Hello!")]))

    assert terminal.out.index("working…") < terminal.out.index("Hello!")


def test_the_pydantic_ai_banner_is_off(
    app_settings: Settings, app_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PYDANTIC_AI_NO_BANNER", raising=False)
    keyed = app_settings.model_copy(update={"anthropic_api_key": SecretStr("sk-ant-test")})
    terminal = Terminal()

    main(settings=keyed, clock=app_clock, read=terminal.read, write=terminal.write)

    assert os.environ["PYDANTIC_AI_NO_BANNER"] == "1"


async def test_memories_shows_saved_facts_and_suggestions(services: Services) -> None:
    terminal = Terminal("/memories")

    await chat(services, terminal, ScriptedModel())  # handled in code: no model needed

    out = terminal.text
    assert "Saved:" in out and "Earliest start: 10:00 (you told me)" in out
    assert "Suggestions from your bookings" in out
    assert "Usual electrician: Sparks Electricals (2 completed bookings with" in out


async def test_confirm_and_reject_are_decided_in_code(services: Services) -> None:
    memories = MemoryStore(services.agent_db, clock=services.clock, ids=services.ids)
    number = {m.scope: n for n, m in numbered(memories) if m.status == "pending_confirmation"}
    terminal = Terminal(
        f"/confirm {number['electrician']}", f"/reject {number['ac_servicing']}", "/confirm 999"
    )

    await chat(services, terminal, ScriptedModel())

    status = {m.scope: m.status for m in memories.all_for_user("user_1") if m.source == "inferred"}
    assert status["electrician"] == "active"
    assert status["ac_servicing"] == "rejected"
    assert "Saved: Usual electrician: Sparks Electricals." in terminal.text
    assert "No suggestion 999; see /memories." in terminal.text


async def test_hints_use_real_numbers_and_a_bare_confirm_lists_the_choices(
    services: Services,
) -> None:
    memories = MemoryStore(services.agent_db, clock=services.clock, ids=services.ids)
    first = min(n for n, m in numbered(memories) if m.status == "pending_confirmation")
    terminal = Terminal("/memories", "/confirm")

    await chat(services, terminal, ScriptedModel())

    assert f"/confirm {first}" in terminal.out[1]  # an example with a real number, not "N"
    listing = terminal.out[2]  # the bare /confirm
    assert "Which one?" in listing and f"{first}. Usual electrician" in listing
    statuses = {m.status for m in memories.all_for_user("user_1") if m.source == "inferred"}
    assert "pending_confirmation" in statuses  # nothing was decided


async def test_unknown_commands_never_reach_the_model(services: Services) -> None:
    terminal = Terminal("/trace", "/quit")

    await chat(services, terminal, ScriptedModel())  # no script: any model call fails

    assert "Unknown command /trace" in terminal.text
    saved = services.agent_db.execute(
        "SELECT count(*) FROM messages WHERE content = '/trace'"
    ).fetchone()[0]
    assert saved == 0  # not part of the conversation either


# --- startup ---------------------------------------------------------------------------------


def test_without_a_key_it_says_what_to_set(app_settings: Settings, app_clock: FakeClock) -> None:
    terminal = Terminal("hi")

    code = main(settings=app_settings, clock=app_clock, read=terminal.read, write=terminal.write)

    assert code == 2
    assert "ANTHROPIC_API_KEY" in terminal.text and "backend/.env" in terminal.text


def interrupt(
    keyed: Settings, clock: FakeClock, monkeypatch: pytest.MonkeyPatch, *, mid_turn: bool
) -> tuple[int, str]:
    """Run main() with a chat that gets Ctrl+C (as asyncio.run raises it), mid-turn or not."""

    async def interrupted(*args: Any) -> None:
        state: ChatState = args[-1]
        state.turn_running = mid_turn
        raise KeyboardInterrupt

    monkeypatch.setattr("errand_agent.api.chat._start", interrupted)
    terminal = Terminal()
    code = main(settings=keyed, clock=clock, read=terminal.read, write=terminal.write)
    return code, terminal.text


@pytest.fixture
def keyed(app_settings: Settings) -> Settings:
    return app_settings.model_copy(update={"anthropic_api_key": SecretStr("sk-ant-test")})


def test_ctrl_c_mid_turn_exits_gracefully(
    keyed: Settings, app_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, text = interrupt(keyed, app_clock, monkeypatch, mid_turn=True)

    assert code == 0  # no traceback
    assert "Interrupted" in text and "checked with the provider" in text


def test_ctrl_c_at_the_prompt_just_says_bye(
    keyed: Settings, app_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, text = interrupt(keyed, app_clock, monkeypatch, mid_turn=False)

    assert code == 0
    assert "Bye." in text and "in progress" not in text  # nothing was in progress


def test_default_models_are_announced(app_settings: Settings, app_clock: FakeClock) -> None:
    keyed = app_settings.model_copy(update={"anthropic_api_key": SecretStr("sk-ant-test")})
    terminal = Terminal()  # leaves at once: no model request is made

    code = main(settings=keyed, clock=app_clock, read=terminal.read, write=terminal.write)

    assert code == 0
    assert "Using default models" in terminal.out[0]
