-- agent.db: our own state (the mock world lives in a separate file). Tables arrive
-- with the feature that first needs them; this is the base. Datetimes are UTC
-- ISO-8601 text with whole seconds, compared as text (same rule as mock_world).
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
