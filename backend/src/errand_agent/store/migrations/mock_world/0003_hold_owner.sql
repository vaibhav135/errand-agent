-- Holds record who placed them, so re-holding your own slot (e.g. the workflow
-- refreshing a hold inside its expiry safety margin) replaces your hold instead of
-- being blocked by it. Another user's active hold still blocks the slot.
ALTER TABLE holds ADD COLUMN user_id TEXT NOT NULL DEFAULT '';
