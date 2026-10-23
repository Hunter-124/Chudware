-- 0014_ci_releases.sql — CI-signed beta publications and the build ledger.
--
-- Nightly CI signs every artifact TWICE over the same sha256: once as the beta
-- version it is published under (0.1.0-beta.42) and once as the stable version
-- it would carry after promotion (0.1.0). Storing that second signature on the
-- beta row is what makes beta -> stable a one-click admin action instead of a
-- re-sign: the bytes never change, so the stable signature minted at build time
-- is still the truth weeks later, and no signing key has to exist on the host.
ALTER TABLE releases
    ADD COLUMN IF NOT EXISTS stable_version   TEXT,
    ADD COLUMN IF NOT EXISTS stable_signature TEXT;

-- A version without its signature (or the reverse) would advertise a promotable
-- row that cannot be promoted, so the pair is all-or-none. The stable version
-- itself must be a canonical triple: it becomes `releases.version` on promotion.
ALTER TABLE releases
    ADD CONSTRAINT releases_stable_pair_all_or_none CHECK (
        (stable_version IS NULL AND stable_signature IS NULL)
        OR (stable_version IS NOT NULL AND stable_signature IS NOT NULL)
    ),
    ADD CONSTRAINT releases_stable_version_canonical_check CHECK (
        stable_version IS NULL OR
        stable_version ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
    );

-- 0011 pinned every version to a bare major.minor.patch. Nightly betas are
-- prereleases by construction, so the beta channel now accepts a `-suffix`
-- while the stable channel stays exactly as strict as before: a customer-facing
-- stable row must never be a prerelease, and dropping the check outright would
-- have allowed that silently.
ALTER TABLE releases
    DROP CONSTRAINT IF EXISTS releases_version_canonical_check;
ALTER TABLE releases
    ADD CONSTRAINT releases_version_canonical_check CHECK (
        version ~ '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?$'
        AND (channel = 'beta' OR version !~ '-')
    );

-- Build ledger. One row per (workflow run, platform, arch) so a re-run of the
-- same job overwrites its own state instead of appending a second history; the
-- nightly planner reads it to skip a commit that already published, and /admin
-- renders it as the CI status board.
CREATE TABLE IF NOT EXISTS ci_builds (
    id         BIGSERIAL   PRIMARY KEY,
    run_id     TEXT        NOT NULL,
    commit_sha TEXT        NOT NULL,
    channel    TEXT        NOT NULL CHECK (channel IN ('stable', 'beta')),
    platform   TEXT        NOT NULL CHECK (platform IN ('linux', 'windows', 'macos')),
    arch       TEXT        NOT NULL CHECK (arch IN ('x86_64', 'arm64')),
    version    TEXT        NOT NULL,
    status     TEXT        NOT NULL CHECK (status IN (
                   'queued', 'building', 'testing', 'packaging',
                   'publishing', 'published', 'failed')),
    detail     TEXT        NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, platform, arch)
);

CREATE INDEX IF NOT EXISTS ci_builds_recent_idx
    ON ci_builds (updated_at DESC, id DESC);

-- The nightly planner's "did this commit already ship?" query.
CREATE INDEX IF NOT EXISTS ci_builds_commit_status_idx
    ON ci_builds (commit_sha, status);
