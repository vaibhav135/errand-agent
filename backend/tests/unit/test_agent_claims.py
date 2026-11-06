from datetime import datetime

import pytest

from errand_agent.agent.claims import claim, correction
from errand_agent.core.clock import IST
from errand_agent.workflow.approvals import Approval, ApprovalDetails
from errand_agent.workflow.errands import Errand
from errand_agent.workflow.requirements import Requirements

SLOT = datetime(2026, 11, 7, 11, 0, tzinfo=IST)


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("All done, your plumber is booked!", "book"),
        ("Your booking is confirmed.", "book"),
        ("Booked QuickFix for Saturday.", "book"),
        ("I've cancelled it as you asked.", "cancel"),
        ("Done: it's canceled.", "cancel"),
        ("Rescheduled to 15:00.", "reschedule"),
        ("I moved it to Sunday.", "reschedule"),
    ],
)
def test_a_completion_claim_is_found(text: str, kind: str) -> None:
    assert claim(text, kind) is not None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("I've held it. It isn't booked until you approve it.", "book"),
        ("Not booked yet: approve the card.", "book"),
        ("Once you approve, it'll be booked.", "book"),
        ("It will be booked after you approve.", "book"),
        ("Nothing is booked yet.", "book"),
        ("The slot is waiting to be booked.", "book"),
        ("Approve to confirm.", "book"),  # "confirm", not "confirmed"
        ("Shall I cancel it? It won't be cancelled unless you approve.", "cancel"),
        ("If you approve, it will be rescheduled to 15:00.", "reschedule"),
        ("Your plumber is booked.", "cancel"),  # a different kind of claim
    ],
)
def test_a_negated_or_hedged_mention_is_not_a_claim(text: str, kind: str) -> None:
    assert claim(text, kind) is None  # type: ignore[arg-type]


def errand(kind: str, state: str = "awaiting_approval") -> Errand:
    now = datetime(2026, 11, 5, 9, 0, tzinfo=IST)
    return Errand(
        errand_id="err_1",
        session_id="sess_1",
        kind=kind,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        requirements=Requirements(),
        booking_id=None,
        hold_id=None,
        hold_expires_at=None,
        created_at=now,
        updated_at=now,
    )


def card(action: str = "book") -> Approval:
    return Approval(
        approval_id="appr_1",
        errand_id="err_1",
        action=action,  # type: ignore[arg-type]
        fingerprint="f" * 64,
        details=ApprovalDetails(
            action=action,  # type: ignore[arg-type]
            provider_id="plm_quickfix",
            provider_name="QuickFix Plumbing",
            slot=SLOT,
            price_inr=699,
        ),
        status="pending",
        created_at=SLOT,
        decided_at=None,
        decided_via=None,
        reason=None,
    )


def test_a_false_booked_claim_gets_the_verified_status() -> None:
    fix = correction("All done, your plumber is booked!", errand("book"), card())

    assert fix is not None
    assert fix.phrase == "booked"
    assert fix.note == (
        "Note from the app: nothing is booked yet. The QuickFix Plumbing card for"
        " Sat 07 Nov, 11:00 at ₹699 is still waiting for your approval."
    )


def test_with_no_card_out_the_note_still_says_nothing_is_booked() -> None:
    fix = correction("I've booked it for you.", errand("book", state="comparing"), None)

    assert fix is not None and fix.note == "Note from the app: nothing is booked yet."


def test_a_false_cancel_claim_names_the_waiting_card() -> None:
    fix = correction("Done, I cancelled it.", errand("cancel"), card("cancel"))

    assert fix is not None
    assert "nothing is cancelled yet" in fix.note
    assert "QuickFix Plumbing" in fix.note


def test_with_nothing_open_a_mention_of_an_old_booking_is_left_alone() -> None:
    assert correction("Your TimberCraft visit is already booked.", None, None) is None


def test_a_true_or_hedged_reply_needs_no_note() -> None:
    assert correction("It isn't booked until you approve it.", errand("book"), card()) is None
