-- 0011_security.sql — release trust, device-bound entitlements, and security telemetry.
--
-- This migration is extended by the security implementation in the same release.
-- Existing release rows were signed before key identifiers were explicit; they use
-- the initial release trust root during the one-time cutover.
ALTER TABLE releases
    ADD COLUMN IF NOT EXISTS key_id TEXT;

UPDATE releases
SET key_id = 'release-v1'
WHERE key_id IS NULL;

ALTER TABLE releases
    ALTER COLUMN key_id SET NOT NULL;

ALTER TABLE releases
    ALTER COLUMN key_id SET DEFAULT 'release-v1';

ALTER TABLE releases
    ADD CONSTRAINT releases_version_canonical_check
        CHECK (version ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'),
    ADD CONSTRAINT releases_min_supported_canonical_check
        CHECK (min_supported ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$');

-- The approved lab-phase cutover has no known customer devices/releases. Old
-- release rows cannot be authenticated to the new inner-manifest contract, so
-- remove them rather than inventing sidecar metadata. They must be republished.
DELETE FROM releases;

ALTER TABLE releases
    ADD COLUMN IF NOT EXISTS payload_manifest_key TEXT,
    ADD COLUMN IF NOT EXISTS payload_manifest_sha256 CHAR(64),
    ADD COLUMN IF NOT EXISTS payload_signature_key TEXT,
    ADD COLUMN IF NOT EXISTS payload_signature_sha256 CHAR(64);

ALTER TABLE releases
    ALTER COLUMN payload_manifest_key SET NOT NULL,
    ALTER COLUMN payload_manifest_sha256 SET NOT NULL,
    ALTER COLUMN payload_signature_key SET NOT NULL,
    ALTER COLUMN payload_signature_sha256 SET NOT NULL,
    ADD CONSTRAINT releases_payload_manifest_sha256_check
        CHECK (payload_manifest_sha256 ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT releases_payload_signature_sha256_check
        CHECK (payload_signature_sha256 ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT releases_payload_manifest_key_unique UNIQUE (payload_manifest_key),
    ADD CONSTRAINT releases_payload_signature_key_unique UNIQUE (payload_signature_key);

CREATE TABLE IF NOT EXISTS security_policies (
    id             TEXT PRIMARY KEY CHECK (id IN ('standard', 'minimized', 'customer_endpoint')),
    telemetry_mode TEXT NOT NULL CHECK (telemetry_mode = id),
    endpoint       TEXT,
    config         JSONB NOT NULL CHECK (config = '{}'::jsonb),
    CHECK ((id = 'customer_endpoint' AND endpoint ~ '^https://[^/[:space:]]+(/.*)?$')
        OR (id <> 'customer_endpoint' AND endpoint IS NULL))
);

INSERT INTO security_policies (id, telemetry_mode, endpoint, config)
VALUES
    ('standard', 'standard', NULL, '{}'::jsonb),
    ('minimized', 'minimized', NULL, '{}'::jsonb)
ON CONFLICT (id) DO NOTHING;

ALTER TABLE desktop_entitlements
    ADD COLUMN IF NOT EXISTS device_public_key BYTEA,
    ADD COLUMN IF NOT EXISTS last_refresh_sequence BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_event_sequence BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_security_checkin_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS policy_id TEXT NOT NULL DEFAULT 'standard';

-- One-release clean cutover: legacy rows remain recognizable so their old
-- signed leases can receive a signed reauthorization refusal, but cannot renew.
UPDATE desktop_entitlements
   SET revoked_at = COALESCE(revoked_at, now())
 WHERE device_public_key IS NULL;

ALTER TABLE desktop_entitlements
    DROP CONSTRAINT IF EXISTS desktop_entitlements_active_device_key_check,
    ADD CONSTRAINT desktop_entitlements_active_device_key_check
        CHECK (revoked_at IS NOT NULL OR device_public_key IS NOT NULL),
    DROP CONSTRAINT IF EXISTS desktop_entitlements_refresh_sequence_check,
    ADD CONSTRAINT desktop_entitlements_refresh_sequence_check
        CHECK (last_refresh_sequence >= 0),
    DROP CONSTRAINT IF EXISTS desktop_entitlements_event_sequence_check,
    ADD CONSTRAINT desktop_entitlements_event_sequence_check
        CHECK (last_event_sequence >= 0),
    DROP CONSTRAINT IF EXISTS desktop_entitlements_policy_id_fkey,
    ADD CONSTRAINT desktop_entitlements_policy_id_fkey
        FOREIGN KEY (policy_id) REFERENCES security_policies (id);

CREATE TABLE IF NOT EXISTS account_security_policies (
    email       TEXT PRIMARY KEY REFERENCES accounts (email) ON DELETE CASCADE,
    policy_id   TEXT NOT NULL REFERENCES security_policies (id),
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    assigned_by TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS security_event_batches (
    batch_id    UUID PRIMARY KEY,
    subject     UUID NOT NULL,
    email       TEXT NOT NULL,
    sequence    BIGINT NOT NULL,
    policy_id   TEXT NOT NULL REFERENCES security_policies (id),
    app_version TEXT NOT NULL,
    platform    TEXT NOT NULL,
    events      JSONB NOT NULL,
    risk_score  INTEGER NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '30 days'),
    UNIQUE (subject, sequence)
);

CREATE INDEX IF NOT EXISTS security_event_batches_expiry_idx
    ON security_event_batches (expires_at, batch_id);
CREATE INDEX IF NOT EXISTS security_event_batches_device_window_idx
    ON security_event_batches (subject, received_at);

CREATE TABLE IF NOT EXISTS security_actions (
    id             UUID PRIMARY KEY,
    email          TEXT NOT NULL,
    subject        UUID NOT NULL,
    action         TEXT NOT NULL CHECK (action = 'revoke_device'),
    reason_codes   TEXT[] NOT NULL,
    risk_score     INTEGER NOT NULL,
    source_batches UUID[] NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    reversed_at    TIMESTAMPTZ,
    reversed_by    TEXT
);

CREATE TABLE IF NOT EXISTS security_appeals (
    id            UUID PRIMARY KEY,
    action_id     UUID NOT NULL REFERENCES security_actions (id),
    email         TEXT NOT NULL,
    subject       UUID NOT NULL,
    message       TEXT NOT NULL,
    state         TEXT NOT NULL CHECK (state IN ('pending', 'restored', 'upheld')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at   TIMESTAMPTZ,
    decided_by    TEXT,
    decision_note TEXT
);

