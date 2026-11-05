-- Mock provider world. All datetimes are UTC ISO-8601 text ("...+00:00"): SQLite
-- compares them as text, so a single offset keeps ordering correct. Conversion to
-- IST happens at the ToolEnv boundary. Slots are not stored: they are computed
-- (mocks/schedule.py); availability is derived from holds and bookings.

CREATE TABLE providers (
    provider_id        TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    service            TEXT NOT NULL,
    rating             REAL NOT NULL CHECK (rating BETWEEN 1 AND 5),
    review_count       INTEGER NOT NULL CHECK (review_count >= 0),
    price_inr          INTEGER NOT NULL CHECK (price_inr > 0),
    verified           INTEGER NOT NULL CHECK (verified IN (0, 1)),
    years_experience   INTEGER NOT NULL CHECK (years_experience >= 0),
    languages          TEXT NOT NULL,          -- JSON array
    description        TEXT NOT NULL,
    description_attack TEXT                    -- fixture label; never exposed via ToolEnv
);

CREATE TABLE provider_areas (
    provider_id TEXT NOT NULL REFERENCES providers (provider_id),
    area        TEXT NOT NULL,
    PRIMARY KEY (provider_id, area)
);

CREATE TABLE reviews (
    review_id   INTEGER PRIMARY KEY,
    provider_id TEXT NOT NULL REFERENCES providers (provider_id),
    author      TEXT NOT NULL,
    rating      INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    posted_on   TEXT NOT NULL,                 -- ISO date
    text        TEXT NOT NULL,
    attack      TEXT                           -- fixture label; never exposed via ToolEnv
);

CREATE TABLE holds (
    hold_id          TEXT PRIMARY KEY,
    provider_id      TEXT NOT NULL REFERENCES providers (provider_id),
    slot             TEXT NOT NULL,
    quoted_price_inr INTEGER NOT NULL CHECK (quoted_price_inr > 0),
    expires_at       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    book_attempts    INTEGER NOT NULL DEFAULT 0  -- feeds keyed randomness on retries
);

CREATE TABLE bookings (
    booking_id      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    provider_id     TEXT NOT NULL REFERENCES providers (provider_id),
    service         TEXT NOT NULL,
    slot            TEXT NOT NULL,
    price_inr       INTEGER NOT NULL CHECK (price_inr > 0),
    status          TEXT NOT NULL
                    CHECK (status IN ('confirmed', 'cancelled', 'cancelled_by_provider', 'completed')),
    hold_id         TEXT UNIQUE REFERENCES holds (hold_id),
    idempotency_key TEXT UNIQUE,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Double booking is impossible at the database level, whatever the code does.
CREATE UNIQUE INDEX bookings_one_confirmed_per_slot
    ON bookings (provider_id, slot) WHERE status = 'confirmed';

CREATE INDEX bookings_by_user ON bookings (user_id);

-- Replays cancel/reschedule results for a repeated idempotency key.
CREATE TABLE requests (
    idempotency_key TEXT PRIMARY KEY,
    operation       TEXT NOT NULL CHECK (operation IN ('cancel', 'reschedule')),
    booking_id      TEXT NOT NULL REFERENCES bookings (booking_id),
    result          TEXT NOT NULL,             -- JSON of the result returned
    created_at      TEXT NOT NULL
);

-- Drives "the first cancel attempt per booking returns 503".
CREATE TABLE cancel_attempts (
    booking_id TEXT PRIMARY KEY REFERENCES bookings (booking_id),
    attempts   INTEGER NOT NULL DEFAULT 0
);
