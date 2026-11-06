-- Side effects sent to a provider, keyed by the idempotency key we generated. Written
-- at the commit point (with the consumed approval) *before* the provider is called, so
-- after a crash the reconciler knows exactly which keys may have taken effect.
CREATE TABLE provider_actions (
    idempotency_key TEXT PRIMARY KEY,
    errand_id       TEXT NOT NULL REFERENCES errands (errand_id),
    approval_id     TEXT NOT NULL REFERENCES approvals (approval_id),
    operation       TEXT NOT NULL CHECK (operation IN ('book', 'reschedule', 'cancel')),
    hold_id         TEXT,
    booking_id      TEXT,
    state           TEXT NOT NULL
                    CHECK (state IN ('in_flight', 'verifying', 'succeeded', 'failed', 'unverified')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX provider_actions_open ON provider_actions (state)
    WHERE state IN ('in_flight', 'verifying', 'unverified');
