-- Signed release evidence and immutable object identities.
ALTER TABLE releases
    ADD COLUMN IF NOT EXISTS commit_sha TEXT,
    ADD COLUMN IF NOT EXISTS sbom_key TEXT,
    ADD COLUMN IF NOT EXISTS sbom_sha256 CHAR(64),
    ADD COLUMN IF NOT EXISTS sbom_signature_key TEXT,
    ADD COLUMN IF NOT EXISTS sbom_signature_sha256 CHAR(64),
    ADD COLUMN IF NOT EXISTS provenance_key TEXT,
    ADD COLUMN IF NOT EXISTS provenance_sha256 CHAR(64),
    ADD COLUMN IF NOT EXISTS provenance_signature_key TEXT,
    ADD COLUMN IF NOT EXISTS provenance_signature_sha256 CHAR(64),
    ADD COLUMN IF NOT EXISTS platform_test_key TEXT,
    ADD COLUMN IF NOT EXISTS platform_test_sha256 CHAR(64),
    ADD COLUMN IF NOT EXISTS platform_test_signature_key TEXT,
    ADD COLUMN IF NOT EXISTS platform_test_signature_sha256 CHAR(64);

ALTER TABLE releases
    ADD CONSTRAINT releases_evidence_all_or_none CHECK (
        (commit_sha IS NULL AND sbom_key IS NULL AND sbom_sha256 IS NULL AND
         sbom_signature_key IS NULL AND sbom_signature_sha256 IS NULL AND
         provenance_key IS NULL AND provenance_sha256 IS NULL AND
         provenance_signature_key IS NULL AND provenance_signature_sha256 IS NULL AND
         platform_test_key IS NULL AND platform_test_sha256 IS NULL AND
         platform_test_signature_key IS NULL AND platform_test_signature_sha256 IS NULL)
        OR
        (commit_sha IS NOT NULL AND sbom_key IS NOT NULL AND sbom_sha256 IS NOT NULL AND
         sbom_signature_key IS NOT NULL AND sbom_signature_sha256 IS NOT NULL AND
         provenance_key IS NOT NULL AND provenance_sha256 IS NOT NULL AND
         provenance_signature_key IS NOT NULL AND provenance_signature_sha256 IS NOT NULL AND
         platform_test_key IS NOT NULL AND platform_test_sha256 IS NOT NULL AND
         platform_test_signature_key IS NOT NULL AND platform_test_signature_sha256 IS NOT NULL)
    ) NOT VALID;

CREATE TABLE IF NOT EXISTS release_channels (
    channel TEXT PRIMARY KEY CHECK (channel IN ('stable', 'beta')),
    min_supported TEXT CHECK (
        min_supported IS NULL OR
        min_supported ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
    ),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO release_channels (channel, min_supported)
VALUES ('stable', NULL), ('beta', NULL)
ON CONFLICT (channel) DO NOTHING;

-- Lab-safe cutover: existing rows remain readable, but every new publication is
-- required by the API to carry the complete signed evidence bundle.
