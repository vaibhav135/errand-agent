-- Errands: one user task (book / reschedule / cancel) and its explicit state.
-- errand_transitions is the append-only history of every state change.
CREATE TABLE errands (
    errand_id    TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES sessions (session_id),
    kind         TEXT NOT NULL CHECK (kind IN ('book', 'reschedule', 'cancel')),
    state        TEXT NOT NULL,
    requirements TEXT NOT NULL,        -- JSON (workflow.requirements.Requirements)
    booking_id   TEXT,                 -- the existing booking, for reschedule / cancel
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX errands_by_session ON errands (session_id, state);

CREATE TABLE errand_transitions (
    transition_id INTEGER PRIMARY KEY,
    errand_id     TEXT NOT NULL REFERENCES errands (errand_id),
    from_state    TEXT NOT NULL,
    to_state      TEXT NOT NULL,
    event         TEXT NOT NULL,
    reason        TEXT NOT NULL,
    run_id        TEXT,
    at            TEXT NOT NULL
);
