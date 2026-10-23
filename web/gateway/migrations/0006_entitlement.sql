-- 0006_entitlement.sql — stable desktop entitlement subjects.
--
-- Account email is the human login and may eventually change. Signed desktop
-- tokens therefore use an opaque UUID subject whose lifetime is independent of
-- display/login spelling. Private signing material never enters PostgreSQL.
CREATE TABLE IF NOT EXISTS desktop_entitlements (
    email      TEXT PRIMARY KEY REFERENCES accounts (email) ON DELETE CASCADE,
    subject    UUID        NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
