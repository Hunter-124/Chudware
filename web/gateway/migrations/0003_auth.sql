-- 0003_auth.sql — self-serve authentication.
--
-- The gateway has always had *authorization* (accounts.status) and never had
-- *authentication*: every pre-existing auth branch trusted an edge IdP to have
-- proven the email. These four tables are the credential store that finally
-- lets the gateway prove it itself.

-- Trial window for the free 14-day trial a verified registration lands on.
-- accounts is owned by 0001_init.sql; this column belongs to the signup flow,
-- so it is added here rather than widening that snapshot.
--
-- DOUBLE PRECISION epoch seconds, deliberately unlike the TIMESTAMPTZ columns
-- in the new tables below: every other timestamp on `accounts` is an epoch
-- float, the admin UI renders them as such, and one odd column out would make
-- the row's JSON inconsistent with itself for no gain.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS trial_ends_at DOUBLE PRECISION;

-- One password per account. Absent for OAuth-only identities — that absence is
-- load-bearing: POST /api/auth/forgot uses it to decide between a reset link
-- and a "you signed in with Google" mail.
CREATE TABLE IF NOT EXISTS credentials (
    email          TEXT PRIMARY KEY REFERENCES accounts (email) ON DELETE CASCADE,
    password_hash  TEXT        NOT NULL,
    email_verified BOOLEAN     NOT NULL DEFAULT FALSE,
    verified_at    TIMESTAMPTZ,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Server-side sessions, deliberately NOT stateless signed cookies: suspending
-- an account has to kill its live sessions on the very next request, which a
-- self-contained cookie cannot do without a revocation table anyway.
CREATE TABLE IF NOT EXISTS sessions (
    sid          TEXT PRIMARY KEY,
    email        TEXT        NOT NULL REFERENCES accounts (email) ON DELETE CASCADE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    user_agent   TEXT        NOT NULL DEFAULT '',
    ip           INET,
    revoked_at   TIMESTAMPTZ
);

-- Revocation-by-email is the hot path for suspension; index only the sessions
-- that can still authenticate anything.
CREATE INDEX IF NOT EXISTS sessions_live_by_email
    ON sessions (email) WHERE revoked_at IS NULL;

-- Google/GitHub identities. (provider, subject) is the stable key — the email
-- on a provider account can change, the subject cannot.
CREATE TABLE IF NOT EXISTS oauth_identities (
    provider  TEXT        NOT NULL,
    subject   TEXT        NOT NULL,
    email     TEXT        NOT NULL REFERENCES accounts (email) ON DELETE CASCADE,
    linked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, subject)
);

CREATE INDEX IF NOT EXISTS oauth_identities_by_email
    ON oauth_identities (email);

-- Email verification, password reset, and OAuth state.
--
-- ONLY the SHA-256 of the token is stored. A database dump, a backup tarball,
-- or a stray log line therefore never yields a usable token: the plaintext
-- exists solely in the mail we sent and in the URL the user clicks.
-- No FK on email: purpose='oauth_state' rows are minted before we know who is
-- logging in, and carry an empty email until the callback resolves one.
CREATE TABLE IF NOT EXISTS email_tokens (
    token_hash TEXT PRIMARY KEY,
    email      TEXT        NOT NULL,
    purpose    TEXT        NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS email_tokens_by_email_purpose
    ON email_tokens (email, purpose) WHERE used_at IS NULL;
