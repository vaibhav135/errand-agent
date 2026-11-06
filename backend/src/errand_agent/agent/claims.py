"""Claim check: a model's reply may not claim what code hasn't verified (BUG-12).

What: `claim(text, kind)` finds an affirmative "booked / cancelled / moved" in a reply;
      `correction(text, errand, waiting)` returns a code-written note when that claim
      contradicts the session's verified state, else None.

Why:  "Never claim success until verified." Bookings, cancellations and moves only
      happen on the approval path, in code, and their replies are code-written. A model
      turn's reply is the model's own text, though, and before this only the prompt
      asked it not to overclaim. This makes it a check.

How:  - Only a claim about the *open* request counts: "booked" while a booking request is
        still open (a card waiting, or still searching), "cancelled" / "moved" while that
        request is. A model turn never completes one (only an approval does), so such a
        claim is always unverified. With nothing open, "your visit is already booked"
        is about a real past booking and is left alone.
      - A negation or hedge just before the word means it isn't a claim ("isn't booked
        until you approve", "it'll be booked", "nothing is booked yet").
      - The note is added after the model's text rather than replacing it: nothing the
        model said is silently dropped, and the verified status sits right after the
        claim. It's a heuristic on wording, so a stray false positive only adds a note
        that is still true.
"""

import re
from dataclasses import dataclass
from typing import Literal

from errand_agent.workflow.approvals import Approval
from errand_agent.workflow.errands import Errand
from errand_agent.workflow.outcomes import when

Kind = Literal["book", "cancel", "reschedule"]

# Past participles only: "booked", not "book" / "booking" ("Approve to book" is no claim).
_CLAIM: dict[Kind, re.Pattern[str]] = {
    "book": re.compile(r"\b(booked|confirmed)\b", re.IGNORECASE),
    "cancel": re.compile(r"\b(cancell?ed)\b", re.IGNORECASE),  # British and American
    "reschedule": re.compile(r"\b(rescheduled|moved)\b", re.IGNORECASE),
}

# Words that, shortly before the claim word in the same clause, make it a negation
# ("isn't", "nothing", "not … yet"), a condition ("until", "once", "if", "unless") or the
# future ("will", "'ll", "to be"). "n't" and "'ll" are matched as suffixes, so they cover
# isn't / won't / haven't and it'll / we'll without listing every form.
_HEDGE = re.compile(
    r"n't|'ll\b|\b(not|nothing|yet|until|unless|once|before|after|if|when|will|would|"
    r"can|could|to be|pending)\b",
    re.IGNORECASE,
)
_LOOK_BACK = 40  # characters before the claim word that are searched for a hedge
_CLAUSE_END = re.compile(r"[.!?;:\n]")  # a hedge in an earlier sentence doesn't count

_DONE: dict[Kind, str] = {"book": "booked", "cancel": "cancelled", "reschedule": "moved"}


@dataclass(frozen=True)
class Correction:
    phrase: str  # the claim word found, for the trace
    note: str  # code-written, from verified state; added after the model's text


def claim(text: str, kind: Kind) -> str | None:
    """The first affirmative completion claim of `kind` in `text`, else None."""
    for match in _CLAIM[kind].finditer(text):
        before = _CLAUSE_END.split(text[max(0, match.start() - _LOOK_BACK) : match.start()])[-1]
        if not _HEDGE.search(before):
            return match.group(1).lower()
    return None


def correction(text: str, errand: Errand | None, waiting: Approval | None) -> Correction | None:
    """A note when `text` claims the open request is done; None if nothing contradicts."""
    if errand is None:
        return None
    kind: Kind = errand.kind
    phrase = claim(text, kind)
    if phrase is None:
        return None
    note = f"Note from the app: nothing is {_DONE[kind]} yet."
    if waiting is not None:
        d = waiting.details
        card = {
            "book": f"The {d.provider_name} card for {when(d.slot)} at ₹{d.price_inr:,}",
            "cancel": f"The card to cancel {d.provider_name} on {when(d.slot)}",
            "reschedule": f"The card to move {d.provider_name} to {when(d.slot)}",
        }[kind]
        note += f" {card} is still waiting for your approval."
    return Correction(phrase=phrase, note=note)
