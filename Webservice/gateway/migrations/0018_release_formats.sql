-- 0018_release_formats.sql — Linux package formats beside the AppImage.
--
-- Linux shipped exactly one artifact shape, so a release identity was
-- (channel, version, platform, arch) and the AppImage was implied. A .deb, an
-- .rpm and a sysroot tarball of the SAME version are four different files with
-- four different digests and four different signatures, and every one of them
-- is a legitimate, simultaneously published row. `format` is that fourth
-- dimension.
--
-- It is a ROUTING key, not an identity claim: the Ed25519 artifact preimage is
-- still (key_id, version, platform, arch, sha256) and must never change, or
-- every shipped 0.1.1 client rejects every future release. The digest already
-- binds the exact bytes, so a signature cannot be replayed from one format onto
-- another regardless of this column.
--
-- DEFAULT 'default' is what makes this a non-event for the running system:
-- Postgres backfills every existing row, `GET /api/releases/latest` without a
-- `format` parameter still selects exactly the row it selected yesterday, and
-- the shipped desktop updater's request is unchanged.
ALTER TABLE releases
    ADD COLUMN IF NOT EXISTS format TEXT NOT NULL DEFAULT 'default';

-- Drop-then-add rather than a bare ADD: this migration is applied once through
-- the schema_migrations ledger, but a re-run on a partially migrated database
-- (a restored dump, a hand-repaired host) must not fail on a duplicate name.
-- Same pattern 0014 uses for releases_version_canonical_check.
ALTER TABLE releases
    DROP CONSTRAINT IF EXISTS releases_format_check;
ALTER TABLE releases
    ADD CONSTRAINT releases_format_check CHECK (
        format IN ('default', 'deb', 'rpm', 'tar')
    );

-- The primary key gains `format`. Without this, publishing the .deb of a
-- version whose AppImage already exists violates the key and the nightly loses
-- three of its four Linux artifacts to a 409 that reads like a duplicate
-- publish. `releases_pkey` is the name Postgres gave the inline PRIMARY KEY in
-- 0007; artifact_key keeps its own UNIQUE constraint, which is what actually
-- guarantees two rows can never claim the same bytes on disk.
ALTER TABLE releases
    DROP CONSTRAINT IF EXISTS releases_pkey;
ALTER TABLE releases
    ADD CONSTRAINT releases_pkey PRIMARY KEY (channel, version, platform, arch, format);

-- The latest-release lookup is now keyed on four columns, and so is the
-- newest-per-tuple scan behind /api/releases/available. The 0007 index without
-- `format` would still be usable but would fetch every format of a tuple and
-- discard three quarters of them on each manifest check.
DROP INDEX IF EXISTS releases_latest_idx;
CREATE INDEX IF NOT EXISTS releases_latest_idx
    ON releases (channel, platform, arch, format, published_at DESC);
