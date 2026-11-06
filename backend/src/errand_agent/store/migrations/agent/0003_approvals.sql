-- Approvals: the user's agreement to one exact action. Only the workflow creates
-- them; the user can only grant or reject an existing one.
CREATE TABLE approvals (
    approval_id TEXT PRIMARY KEY,
    errand_id   TEXT NOT NULL REFERENCES errands (errand_id),
    action      TEXT NOT NULL CHECK (action IN ('book', 'reschedule', 'cancel')),
    fingerprint TEXT NOT NULL,          -- sha256 of the material details
    details     TEXT NOT NULL,          -- JSON (workflow.approvals.ApprovalDetails)
    status      TEXT NOT NULL
                CHECK (status IN ('pending', 'granted', 'rejected', 'invalidated', 'consumed')),
    created_at  TEXT NOT NULL,
    decided_at  TEXT,
    decided_via TEXT CHECK (decided_via IN ('button', 'typed')),
    reason      TEXT                    -- why it was rejected / invalidated
);

-- At most one live (pending or granted) approval per errand.
CREATE UNIQUE INDEX approvals_one_live_per_errand
    ON approvals (errand_id) WHERE status IN ('pending', 'granted');
