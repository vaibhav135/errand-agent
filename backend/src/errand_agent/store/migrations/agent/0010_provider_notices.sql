-- Provider cancellations the monitor has handled, one row per booking (Phase 7, D6).
-- Written in the same transaction as the notice to the user, so each cancellation is
-- acted on exactly once: across restarts, and when two passes overlap (the primary key
-- refuses the second).
CREATE TABLE provider_notices (
    booking_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions (session_id),
    decision   TEXT NOT NULL CHECK (decision IN ('replace', 'notify_only', 'past')),
    reason     TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
