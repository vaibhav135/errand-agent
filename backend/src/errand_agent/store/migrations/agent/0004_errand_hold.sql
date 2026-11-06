-- The provider hold behind a book errand's pending approval. Refreshed when it nears
-- expiry; the approval's fingerprint excludes hold_id, so a same-price re-hold keeps it.
ALTER TABLE errands ADD COLUMN hold_id TEXT;
ALTER TABLE errands ADD COLUMN hold_expires_at TEXT;
