-- Profile PII: identity and contact details, collected when first needed. Kept out of
-- the model's context (only address labels and areas are shown to it) and out of
-- traces (events list changed field names, never values).
CREATE TABLE user_profile (
    user_id    TEXT PRIMARY KEY,
    name       TEXT,
    phone      TEXT,                -- normalised +91XXXXXXXXXX
    email      TEXT,                -- lower-cased
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE user_addresses (
    address_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    label      TEXT NOT NULL,       -- "Home", "Parents"
    line       TEXT NOT NULL,       -- street line: PII
    area       TEXT NOT NULL,       -- locality used for searches
    is_default INTEGER NOT NULL CHECK (is_default IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX user_addresses_one_default ON user_addresses (user_id) WHERE is_default = 1;
