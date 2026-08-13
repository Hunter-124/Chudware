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

-- Payload sidecars were not recorded by pre-security publishers. Add nullable
-- columns first so the migration is safe against lab databases with old rows;
-- operators must republish or delete those rows before the final constraint.
ALTER TABLE releases
    ADD COLUMN IF NOT EXISTS payload_manifest_key TEXT,
    ADD COLUMN IF NOT EXISTS payload_manifest_sha256 CHAR(64),
    ADD COLUMN IF NOT EXISTS payload_signature_key TEXT,
    ADD COLUMN IF NOT EXISTS payload_signature_sha256 CHAR(64);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM releases
        WHERE payload_manifest_key IS NULL
           OR payload_manifest_sha256 IS NULL
           OR payload_signature_key IS NULL
           OR payload_signature_sha256 IS NULL
    ) THEN
        RAISE EXCEPTION
            'security migration requires existing releases to be republished with payload sidecars or deleted';
    END IF;
END $$;

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
