"""Seed data: populate mock_world.db with providers, reviews and the user's history.

What: `seed_world(conn, clock, user_id=...)` loads `mocks/data/providers.json`
      (30 hand-written providers, 5 services x 6, across 5 Bengaluru areas) and
      inserts providers, service areas, reviews and booking history. Returns
      True if it seeded, False if the world was already populated.

Why:  The demo and evals need a stable, readable world: every (service, area)
      pair has 3-8 providers (the search contract), the user's history supports
      "same electrician as last time" and "move my AC servicing", and reviews
      carry the prompt-injection fixtures (see docs/ARCHITECTURE.md).

How:  - The JSON is validated with pydantic on load, so a typo in a service, area
        or attack label fails loudly instead of seeding bad data.
      - Idempotent: runs once per database (skips if providers exist), so an
        agent restart never duplicates history.
      - Dates are relative to the seeding clock ("24 days ago", "in 5 days"), so
        "upcoming" stays upcoming whenever the world is created. History slots
        are fixed IST hours, stored as UTC text like every mock_world datetime.
      - Attack labels are stored for evals and never exposed through ToolEnv.
"""

import json
import sqlite3
from datetime import datetime, time, timedelta
from importlib.resources import files
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from errand_agent.core.clock import Clock, utc_text
from errand_agent.mocks.schedule import IST
from errand_agent.providers.models import BookingStatus, Service
from errand_agent.store.db import transaction

Area = Literal["Indiranagar", "Koramangala", "HSR Layout", "Whitefield", "Jayanagar"]
AttackCategory = Literal[
    "goal_hijack_blunt",
    "goal_hijack_social",
    "goal_hijack_intent",
    "goal_hijack_description",
    "unauthorized_tool_cancel",
    "exfiltration",
    "ranking_manipulation",
    "memory_poisoning",
    "fraud_payment",
    "availability",
    "tag_spoofing",
    "hidden_html_comment",
    "encoded_base64",
    "split_payload",
]

SERVICES: tuple[str, ...] = get_args(Service)
AREAS: tuple[str, ...] = get_args(Area)
ATTACK_CATEGORIES: frozenset[str] = frozenset(get_args(AttackCategory))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _SeedReview(_Strict):
    author: str
    rating: int = Field(ge=1, le=5)
    days_ago: int = Field(ge=0)
    text: str
    attack: AttackCategory | None = None


class _SeedProvider(_Strict):
    provider_id: str
    name: str
    service: Service
    areas: list[Area] = Field(min_length=1)
    rating: float = Field(ge=1, le=5)
    review_count: int = Field(ge=0)
    price_inr: int = Field(gt=0)
    verified: bool
    years_experience: int = Field(ge=0)
    languages: list[str]
    description: str
    description_attack: AttackCategory | None = None
    reviews: list[_SeedReview]


class _SeedHistory(_Strict):
    booking_id: str
    provider_id: str
    days_from_today: int
    hour_ist: int = Field(ge=0, le=23)
    status: BookingStatus
    price_inr: int = Field(gt=0)


class _SeedFile(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)  # tolerates "_comment"

    areas: list[Area]
    providers: list[_SeedProvider]
    history: list[_SeedHistory]


def load_seed() -> _SeedFile:
    raw = (files("errand_agent.mocks") / "data" / "providers.json").read_text()
    return _SeedFile.model_validate(json.loads(raw))


def seed_world(conn: sqlite3.Connection, clock: Clock, *, user_id: str) -> bool:
    if conn.execute("SELECT 1 FROM providers LIMIT 1").fetchone():
        return False

    data = load_seed()
    now = clock.now()
    today = now.astimezone(IST).date()
    stamp = utc_text(now)
    by_id = {p.provider_id: p for p in data.providers}

    with transaction(conn):
        for p in data.providers:
            conn.execute(
                "INSERT INTO providers (provider_id, name, service, rating, review_count,"
                " price_inr, verified, years_experience, languages, description,"
                " description_attack) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    p.provider_id,
                    p.name,
                    p.service,
                    p.rating,
                    p.review_count,
                    p.price_inr,
                    int(p.verified),
                    p.years_experience,
                    json.dumps(p.languages),
                    p.description,
                    p.description_attack,
                ),
            )
            conn.executemany(
                "INSERT INTO provider_areas (provider_id, area) VALUES (?, ?)",
                [(p.provider_id, area) for area in p.areas],
            )
            conn.executemany(
                "INSERT INTO reviews (provider_id, author, rating, posted_on, text, attack)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        p.provider_id,
                        r.author,
                        r.rating,
                        (today - timedelta(days=r.days_ago)).isoformat(),
                        r.text,
                        r.attack,
                    )
                    for r in p.reviews
                ],
            )

        for h in data.history:
            slot_ist = datetime.combine(
                today + timedelta(days=h.days_from_today), time(hour=h.hour_ist), IST
            )
            conn.execute(
                "INSERT INTO bookings (booking_id, user_id, provider_id, service, slot,"
                " price_inr, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    h.booking_id,
                    user_id,
                    h.provider_id,
                    by_id[h.provider_id].service,
                    utc_text(slot_ist),
                    h.price_inr,
                    h.status,
                    stamp,
                    stamp,
                ),
            )
    return True
