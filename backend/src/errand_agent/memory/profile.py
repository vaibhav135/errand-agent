"""User profile: identity and contact details, stored structured and kept private.

What: `ProfileStore` (name, phone, email, labelled addresses): update, add/remove
      address, forget a field, and `model_view()`, the only part of a profile that
      may reach the model.

Why:  Long-term personal details are collected once (when first needed), not re-asked,
      and stored as typed columns rather than free text. They are also the data an
      attacker most wants (the `exfiltration` fixture), and the provider contract never
      needs them, so the model doesn't get to see them at all.

How:  - Every value must appear in the user's own message (phone compared digits-only,
        email and name case-insensitively, address line and area verbatim), so neither
        a review nor the model can supply contact details.
      - Phone: Indian mobile, normalised to +91XXXXXXXXXX. Email: simple format check,
        lower-cased. One default address per user (DB index); the first is default.
      - `model_view()`: address labels and areas, and has_phone / has_email flags.
        No name, street, number or email. The UI shows those straight from the DB.
      - `profile` trace events list the changed *field names*, never their values: the
        trace sanitizer redacts secrets by key name and wouldn't recognise a phone
        number, so values simply never enter an event.
      - Forgetting a field sets it to NULL; removing an address deletes the row.
      - Saved values are scrubbed from the user's stored messages ("[phone saved]",
        ...), so the model sees a typed value only in the turn that extracts it.
"""

import re
import sqlite3
from typing import Literal

from pydantic import BaseModel, ConfigDict

from errand_agent.core.clock import Clock, utc_text
from errand_agent.core.ids import IdGen
from errand_agent.memory.scrub import Replacement, phone_pattern, phrase_pattern, scrub_history
from errand_agent.memory.store import ProvenanceError
from errand_agent.store.db import after_commit, atomic
from errand_agent.tracing.recorder import RunTrace

ContactField = Literal["name", "phone", "email"]
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class Address(BaseModel):
    model_config = ConfigDict(frozen=True)

    address_id: str
    label: str
    line: str
    area: str
    is_default: bool


class Profile(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: str
    name: str | None
    phone: str | None
    email: str | None
    addresses: list[Address]


class ProfileView(BaseModel):
    """What the model may know about the user: nothing that identifies or reaches them."""

    model_config = ConfigDict(frozen=True)

    addresses: list[dict[str, object]]
    has_phone: bool
    has_email: bool


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def normalise_phone(raw: str) -> str:
    digits = _digits(raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) != 10 or digits[0] not in "6789":
        raise ValueError(f"not an Indian mobile number: {raw!r}")
    return f"+91{digits}"


def _require_said(value: str, user_message: str, *, what: str, digits_only: bool = False) -> None:
    found = (
        _digits(value)[-10:] in _digits(user_message)
        if digits_only
        else _norm(value) in _norm(user_message)
    )
    if not value.strip() or not found:
        raise ProvenanceError(f"the {what} must come from the user's own message")


class ProfileStore:
    def __init__(self, conn: sqlite3.Connection, *, clock: Clock, ids: IdGen) -> None:
        self._conn = conn
        self._clock = clock
        self._ids = ids

    def get(self, user_id: str) -> Profile:
        row = self._conn.execute(
            "SELECT * FROM user_profile WHERE user_id = ?", (user_id,)
        ).fetchone()
        addresses = [
            Address(
                address_id=r["address_id"],
                label=r["label"],
                line=r["line"],
                area=r["area"],
                is_default=bool(r["is_default"]),
            )
            for r in self._conn.execute(
                "SELECT * FROM user_addresses WHERE user_id = ? ORDER BY created_at, address_id",
                (user_id,),
            )
        ]
        return Profile(
            user_id=user_id,
            name=row["name"] if row else None,
            phone=row["phone"] if row else None,
            email=row["email"] if row else None,
            addresses=addresses,
        )

    def model_view(self, user_id: str) -> ProfileView:
        p = self.get(user_id)
        return ProfileView(
            addresses=[
                {"label": a.label, "area": a.area, "default": a.is_default} for a in p.addresses
            ],
            has_phone=p.phone is not None,
            has_email=p.email is not None,
        )

    def update(
        self,
        user_id: str,
        *,
        name: str | None = None,
        phone: str | None = None,
        email: str | None = None,
        user_message: str,
        run: RunTrace | None = None,
    ) -> Profile:
        changes: dict[str, str] = {}
        scrubs: list[Replacement] = []
        if name is not None:
            _require_said(name, user_message, what="name")
            changes["name"] = name.strip()
            scrubs.append((phrase_pattern(name), "[name saved]"))
        if phone is not None:
            normalised = normalise_phone(phone)
            _require_said(phone, user_message, what="phone number", digits_only=True)
            changes["phone"] = normalised
            scrubs.append((phone_pattern(normalised), "[phone saved]"))
        if email is not None:
            if not _EMAIL.fullmatch(email.strip()):
                raise ValueError(f"not an email address: {email!r}")
            _require_said(email, user_message, what="email")
            changes["email"] = email.strip().lower()
            scrubs.append((phrase_pattern(email.strip()), "[email saved]"))
        if changes:
            now = utc_text(self._clock.now())
            with atomic(self._conn):
                self._conn.execute(
                    "INSERT OR IGNORE INTO user_profile (user_id, created_at, updated_at)"
                    " VALUES (?, ?, ?)",
                    (user_id, now, now),
                )
                for field, value in changes.items():  # field names are from a fixed set
                    self._conn.execute(
                        f"UPDATE user_profile SET {field} = ?, updated_at = ? WHERE user_id = ?",
                        (value, now, user_id),
                    )
                scrub_history(self._conn, user_id, scrubs)
                self._trace(run, sorted(changes))
        return self.get(user_id)

    def add_address(
        self,
        user_id: str,
        *,
        label: str,
        line: str,
        area: str,
        user_message: str,
        make_default: bool = False,
        run: RunTrace | None = None,
    ) -> Address:
        _require_said(line, user_message, what="address")
        _require_said(area, user_message, what="area")
        address_id = self._ids.new("addr")
        now = utc_text(self._clock.now())
        with atomic(self._conn):
            first = (
                self._conn.execute(
                    "SELECT 1 FROM user_addresses WHERE user_id = ?", (user_id,)
                ).fetchone()
                is None
            )
            default = first or make_default
            if default:
                self._conn.execute(
                    "UPDATE user_addresses SET is_default = 0 WHERE user_id = ?", (user_id,)
                )
            self._conn.execute(
                "INSERT INTO user_addresses (address_id, user_id, label, line, area, is_default,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    address_id,
                    user_id,
                    label.strip(),
                    line.strip(),
                    area.strip(),
                    int(default),
                    now,
                    now,
                ),
            )
            # The street line is PII; the area stays (not identifying, needed for search).
            scrub_history(self._conn, user_id, [(phrase_pattern(line), "[address saved]")])
            self._trace(run, ["address"])
        return next(a for a in self.get(user_id).addresses if a.address_id == address_id)

    def remove_address(self, user_id: str, address_id: str, *, run: RunTrace | None = None) -> None:
        with atomic(self._conn):
            row = self._conn.execute(
                "SELECT is_default FROM user_addresses WHERE address_id = ? AND user_id = ?",
                (address_id, user_id),
            ).fetchone()
            if row is None:
                raise KeyError(f"no address {address_id!r}")
            self._conn.execute("DELETE FROM user_addresses WHERE address_id = ?", (address_id,))
            if row["is_default"]:  # promote the oldest remaining address
                self._conn.execute(
                    "UPDATE user_addresses SET is_default = 1 WHERE address_id = ("
                    " SELECT address_id FROM user_addresses WHERE user_id = ?"
                    " ORDER BY created_at, address_id LIMIT 1)",
                    (user_id,),
                )
            self._trace(run, ["address"])

    def forget_field(
        self, user_id: str, field: ContactField, *, run: RunTrace | None = None
    ) -> None:
        if field not in ("name", "phone", "email"):
            raise ValueError(f"unknown profile field {field!r}")
        with atomic(self._conn):
            self._conn.execute(
                f"UPDATE user_profile SET {field} = NULL, updated_at = ? WHERE user_id = ?",
                (utc_text(self._clock.now()), user_id),
            )
            self._trace(run, [field])

    def _trace(self, run: RunTrace | None, fields: list[str]) -> None:
        if run is not None:
            after_commit(self._conn, lambda: run.emit("profile", {"fields": fields}))
