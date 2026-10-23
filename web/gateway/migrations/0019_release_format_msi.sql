-- 0019_release_format_msi.sql — Windows Installer packages beside Inno Setup.
--
-- A CHECK constraint cannot be widened in place. Drop and re-add the named
-- constraint so both the normal migration ledger and an operator retry against
-- a populated, partially migrated database are idempotent. This is the same
-- retry-safe pattern used by 0014 and 0018.
ALTER TABLE releases
    DROP CONSTRAINT IF EXISTS releases_format_check;
ALTER TABLE releases
    ADD CONSTRAINT releases_format_check CHECK (
        format IN ('default', 'deb', 'rpm', 'tar', 'msi')
    );
