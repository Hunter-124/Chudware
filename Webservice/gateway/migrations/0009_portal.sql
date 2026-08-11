-- 0009_portal.sql — administrable per-device desktop entitlements.
--
-- The original entitlement table admitted one subject per account. A commercial
-- account can have several computers, so identity is now (email, device_name)
-- while each installation keeps its opaque, globally-unique subject.
ALTER TABLE desktop_entitlements
    ADD COLUMN IF NOT EXISTS device_name TEXT,
    ADD COLUMN IF NOT EXISTS last_checkin_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;

UPDATE desktop_entitlements
   SET device_name = 'Legacy device'
 WHERE device_name IS NULL OR btrim(device_name) = '';

ALTER TABLE desktop_entitlements
    ALTER COLUMN device_name SET NOT NULL;

ALTER TABLE desktop_entitlements
    DROP CONSTRAINT IF EXISTS desktop_entitlements_pkey;

ALTER TABLE desktop_entitlements
    DROP CONSTRAINT IF EXISTS desktop_entitlements_email_device_key;

ALTER TABLE desktop_entitlements
    ADD CONSTRAINT desktop_entitlements_email_device_key
        UNIQUE (email, device_name);

CREATE INDEX IF NOT EXISTS desktop_entitlements_active_by_email
    ON desktop_entitlements (email, created_at DESC)
    WHERE revoked_at IS NULL;
