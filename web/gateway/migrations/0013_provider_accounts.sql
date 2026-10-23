-- Account-scoped AI subscription links (Claude Pro/Max, ChatGPT Plus/Pro).
--
-- Sibling of provider_keys: that table holds a metered API key, this one holds
-- an OAuth grant against a subscription the account already pays for. Both
-- store an AEAD envelope produced with CHUDWARE_WEB_PROVIDER_KEY_ENCRYPTION_KEY;
-- plaintext tokens must never enter PostgreSQL or its off-box backups.
--
-- scope='personal' is the linking account's own subscription and is only ever
-- used for that account's turns. scope='shared' is an operator-owned account
-- that backs the managed pool for everyone; only an admin can create one, which
-- is why the row still carries the linking admin's email for attribution.
CREATE TABLE provider_accounts (
    email             TEXT        NOT NULL REFERENCES accounts (email) ON DELETE CASCADE,
    provider          TEXT        NOT NULL,
    account_key       TEXT        NOT NULL,
    scope             TEXT        NOT NULL CHECK (scope IN ('personal', 'shared')),
    encrypted_tokens  BYTEA       NOT NULL,
    label             TEXT        NOT NULL DEFAULT '',
    plan              TEXT        NOT NULL DEFAULT '',
    access_expires_at TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_refreshed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at      TIMESTAMPTZ,
    PRIMARY KEY (email, provider, account_key)
);

-- Personal resolution is always keyed by the caller's own email.
CREATE INDEX provider_accounts_personal_by_account
    ON provider_accounts (email, provider)
    WHERE scope = 'personal';

-- Pool resolution ignores the owning admin: any shared row for the provider
-- will do, oldest-used first so the pool spreads across linked accounts.
CREATE INDEX provider_accounts_shared_pool
    ON provider_accounts (provider, last_used_at NULLS FIRST)
    WHERE scope = 'shared';

-- In-flight OAuth logins. A row lives only between "begin" and "complete", and
-- holds the PKCE verifier (Claude) or the device-authorization handle (ChatGPT)
-- encrypted under the same envelope, because either is enough to complete a
-- sign-in on the account's behalf if the database leaks.
--
-- The gateway runs multiple uvicorn workers, so this state cannot live in
-- process memory: the "begin" request and the "complete" request routinely
-- land on different workers.
CREATE TABLE provider_oauth_flows (
    flow_id          TEXT        PRIMARY KEY,
    email            TEXT        NOT NULL REFERENCES accounts (email) ON DELETE CASCADE,
    provider         TEXT        NOT NULL,
    scope            TEXT        NOT NULL CHECK (scope IN ('personal', 'shared')),
    kind             TEXT        NOT NULL CHECK (kind IN ('pkce_paste', 'device')),
    encrypted_secret BYTEA       NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at       TIMESTAMPTZ NOT NULL
);

-- Expired flows are swept opportunistically on each begin.
CREATE INDEX provider_oauth_flows_expiry ON provider_oauth_flows (expires_at);
