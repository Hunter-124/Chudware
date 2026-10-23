-- Native betas without an authenticated owner-approved publisher cannot promote.
-- Historic rows are not re-attested by migration; sinks reverify their signed bytes.
ALTER TABLE releases
    ADD COLUMN IF NOT EXISTS native_publisher_verified BOOLEAN NOT NULL DEFAULT FALSE;
