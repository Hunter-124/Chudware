-- Commercial billing state. Accounts predates PostgreSQL and exposes epoch
-- seconds in its API, so its added timestamps deliberately match that existing
-- DOUBLE PRECISION convention. New standalone tables use TIMESTAMPTZ.

ALTER TABLE accounts ADD COLUMN IF NOT EXISTS subscribed_since DOUBLE PRECISION;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS paid_through DOUBLE PRECISION;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS subscription_id TEXT NOT NULL DEFAULT '';
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS subscription_status TEXT NOT NULL DEFAULT '';
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS price_id TEXT NOT NULL DEFAULT '';
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS credit_balance BIGINT NOT NULL DEFAULT 0;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS credit_ceiling_monthly BIGINT NOT NULL DEFAULT 0;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS ai_grant BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS is_edu BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE accounts ADD CONSTRAINT accounts_credit_balance_nonnegative
    CHECK (credit_balance >= 0);
ALTER TABLE accounts ADD CONSTRAINT accounts_credit_ceiling_nonnegative
    CHECK (credit_ceiling_monthly >= 0);
COMMENT ON COLUMN accounts.credit_ceiling_monthly IS
    'Monthly pooled-AI credit ceiling; 0 means unlimited.';

-- Clean commercial-tier cutover. Historical names must not survive because
-- plans.py no longer recognizes them and every limit is now enforced from it.
UPDATE accounts SET plan = CASE plan
    WHEN 'free' THEN 'trial'
    WHEN 'pro' THEN 'desktop'
    WHEN 'team' THEN 'studio'
    ELSE plan
END
WHERE plan IN ('free', 'pro', 'team');
ALTER TABLE accounts ALTER COLUMN plan SET DEFAULT 'trial';

CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_subscription
    ON accounts (subscription_id) WHERE subscription_id <> '';

CREATE TABLE IF NOT EXISTS trial_domains (
    domain      TEXT PRIMARY KEY,
    trial_days  INTEGER NOT NULL CHECK (trial_days > 0),
    ai_credits  BIGINT NOT NULL DEFAULT 0 CHECK (ai_credits >= 0),
    expires_at  TIMESTAMPTZ,
    note        TEXT NOT NULL DEFAULT ''
);
