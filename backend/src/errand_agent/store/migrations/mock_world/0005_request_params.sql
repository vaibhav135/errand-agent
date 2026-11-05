-- A cancel/reschedule idempotency key identifies the whole request, not just the
-- booking: replaying a key with different parameters (e.g. another slot) must be
-- refused, so the parameters are stored next to the result.
ALTER TABLE requests ADD COLUMN request TEXT NOT NULL DEFAULT '{}';
