-- Initial schema: the three stores the gateway carried as separate SQLite
-- files (accounts.db, chat.db, billing_webhook_events.sqlite3).
--
-- Timestamps are stored as double precision UNIX epoch seconds, not
-- TIMESTAMPTZ, everywhere the SQLite schema did: the JSON the gateway returns
-- and the admin UI that renders it both speak epoch floats, and converting at
-- the boundary would change an observable API for no gain. New tables added by
-- later migrations should use TIMESTAMPTZ.

CREATE TABLE accounts (
    email              TEXT PRIMARY KEY,
    display            TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'pending',
    plan               TEXT NOT NULL DEFAULT 'free',
    role               TEXT NOT NULL DEFAULT 'user',
    created_at         DOUBLE PRECISION NOT NULL,
    decided_at         DOUBLE PRECISION,
    decided_by         TEXT NOT NULL DEFAULT '',
    stripe_customer_id TEXT NOT NULL DEFAULT '',
    notes              TEXT NOT NULL DEFAULT '',
    heavy              BOOLEAN NOT NULL DEFAULT FALSE
);

-- The admin queue lists and counts by status.
CREATE INDEX idx_accounts_status ON accounts (status);
-- The billing webhook resolves customer -> account. Partial: the column is
-- NOT NULL DEFAULT '' and the empty string is the overwhelming majority.
CREATE INDEX idx_accounts_stripe ON accounts (stripe_customer_id)
    WHERE stripe_customer_id <> '';

CREATE TABLE messages (
    id      BIGSERIAL PRIMARY KEY,
    ts      DOUBLE PRECISION NOT NULL,
    email   TEXT NOT NULL,
    display TEXT NOT NULL,
    text    TEXT NOT NULL
);

CREATE INDEX idx_messages_ts ON messages (ts);

-- Stripe redelivers at least once. event_id being the primary key is the
-- entire idempotency guarantee: INSERT ... ON CONFLICT DO NOTHING in the same
-- transaction as the entitlement mutation.
CREATE TABLE processed_events (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    processed_at DOUBLE PRECISION NOT NULL
);
