-- Persistent scenario progress (e.g. how many scripted book outcomes were used),
-- so a process restart mid-scenario continues where it stopped.
CREATE TABLE scenario_state (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
