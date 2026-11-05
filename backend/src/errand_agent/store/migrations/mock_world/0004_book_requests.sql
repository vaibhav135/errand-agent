-- Every book() call records idempotency key -> hold on first use, in the same
-- transaction as its outcome. This lets get_booking_status tell "seen, nothing
-- booked" (not_booked, e.g. after a timeout that didn't book) from "never seen"
-- (unknown_reference), and lets book() refuse a key reused for a different hold.
CREATE TABLE book_requests (
    idempotency_key TEXT PRIMARY KEY,
    hold_id         TEXT NOT NULL REFERENCES holds (hold_id),
    user_id         TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
