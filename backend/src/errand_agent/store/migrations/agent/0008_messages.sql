-- Conversation history: what the user and the assistant said, per session. Tool calls
-- and results are not stored here (they live in traces and workflow tables).
CREATE TABLE messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions (session_id),
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    run_id     TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX messages_by_session ON messages (session_id, created_at);
