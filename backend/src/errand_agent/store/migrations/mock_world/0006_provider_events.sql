-- Simulated provider-side events (e.g. a provider cancelling a booking), recorded
-- for the demo and traces. Internal to the mock: the agent detects the effect only
-- through the contract tools (list_bookings / get_booking_status).
CREATE TABLE provider_events (
    event_id   INTEGER PRIMARY KEY,
    booking_id TEXT NOT NULL REFERENCES bookings (booking_id),
    kind       TEXT NOT NULL CHECK (kind IN ('cancelled_by_provider')),
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
