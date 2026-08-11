-- 0005_credits.sql — prepaid AI credits, immutable ledger, and model pricing.


CREATE TABLE IF NOT EXISTS model_rates (
    model                    TEXT PRIMARY KEY,
    prompt_usd_per_mtok      NUMERIC(20, 10) NOT NULL CHECK (prompt_usd_per_mtok >= 0),
    completion_usd_per_mtok  NUMERIC(20, 10) NOT NULL CHECK (completion_usd_per_mtok >= 0),
    fetched_at               TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS credit_ledger (
    id                 TEXT PRIMARY KEY,
    email              TEXT NOT NULL REFERENCES accounts (email) ON DELETE CASCADE,
    ts                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind               TEXT NOT NULL CHECK (kind IN ('purchase', 'spend', 'grant', 'adjust', 'refund')),
    model              TEXT NOT NULL DEFAULT '',
    prompt_tokens      BIGINT NOT NULL DEFAULT 0 CHECK (prompt_tokens >= 0),
    completion_tokens  BIGINT NOT NULL DEFAULT 0 CHECK (completion_tokens >= 0),
    credits_delta      BIGINT NOT NULL,
    stripe_event_id    TEXT,
    note               TEXT NOT NULL DEFAULT ''
);

-- Stripe retries are normal. A provider event may affect the balance exactly once,
-- even when two webhook workers race the same delivery.
CREATE UNIQUE INDEX IF NOT EXISTS credit_ledger_stripe_event_once
    ON credit_ledger (stripe_event_id)
    WHERE stripe_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS credit_ledger_email_ts
    ON credit_ledger (email, ts DESC);

