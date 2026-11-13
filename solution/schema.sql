PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS metadata (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drops (
    fingerprint TEXT PRIMARY KEY,
    lead_rows_total INTEGER NOT NULL,
    duplicate_lead_rows_merged INTEGER NOT NULL,
    field_warnings INTEGER NOT NULL,
    events_total INTEGER NOT NULL,
    malformed_events INTEGER NOT NULL,
    duplicate_events_dropped INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    workflow_id TEXT NOT NULL,
    phone_token TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    phone_masked TEXT NOT NULL,
    fields TEXT NOT NULL,
    PRIMARY KEY (workflow_id, phone_token)
);

CREATE TABLE IF NOT EXISTS rejections (
    drop_fingerprint TEXT NOT NULL REFERENCES drops(fingerprint) DEFERRABLE INITIALLY DEFERRED,
    workflow_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (drop_fingerprint, workflow_id, batch_id, row_number)
);

-- Events can precede leads. The keyed identity remains immutable while link
-- eligibility is derived against the current lead table when reporting.
CREATE TABLE IF NOT EXISTS ledger (
    event_id TEXT PRIMARY KEY,
    drop_fingerprint TEXT NOT NULL REFERENCES drops(fingerprint) DEFERRABLE INITIALLY DEFERRED,
    workflow_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    phone_token TEXT,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    disposition_code TEXT
);

CREATE TRIGGER IF NOT EXISTS ledger_no_updates
BEFORE UPDATE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'Ledger is append-only');
END;

CREATE TRIGGER IF NOT EXISTS ledger_no_deletes
BEFORE DELETE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'Ledger is append-only');
END;
