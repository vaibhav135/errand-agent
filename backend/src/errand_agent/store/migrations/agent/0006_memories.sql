-- Long-term preferences (not profile PII, not one errand's details). Explicit facts
-- carry the user's own words as evidence; inferred ones wait for confirmation.
-- Forgetting wipes value and evidence but keeps a tombstone, which also blocks the
-- same fact from being inferred again.
CREATE TABLE memories (
    memory_id     TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    key           TEXT NOT NULL CHECK (key IN ('default_address', 'earliest_start',
                      'latest_start', 'budget_max_inr', 'preferred_provider',
                      'avoided_provider', 'note')),
    scope         TEXT NOT NULL,              -- 'global' or a service
    value         TEXT,                       -- JSON; NULL once forgotten
    topic         TEXT,                       -- notes only
    source        TEXT NOT NULL CHECK (source IN ('explicit', 'inferred')),
    status        TEXT NOT NULL CHECK (status IN ('active', 'pending_confirmation',
                      'superseded', 'forgotten', 'rejected')),
    evidence      TEXT,                       -- user's quote or inference reason; NULL once forgotten
    superseded_by TEXT REFERENCES memories (memory_id),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    confirmed_at  TEXT,
    forgotten_at  TEXT
);

-- One active fact per (user, key, scope); notes are free to coexist.
CREATE UNIQUE INDEX memories_one_active
    ON memories (user_id, key, scope) WHERE status = 'active' AND key != 'note';

CREATE INDEX memories_by_user ON memories (user_id, status);
