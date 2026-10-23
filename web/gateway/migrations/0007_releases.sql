-- 0007_releases.sql — signed desktop release manifests.
--
-- R2 object bytes remain outside PostgreSQL. Each row binds the canonical object
-- key to its expected size, digest, and Ed25519 signature. The gateway exposes
-- only the newest publication for a channel/platform/architecture tuple.
CREATE TABLE IF NOT EXISTS releases (
    channel       TEXT        NOT NULL CHECK (channel IN ('stable', 'beta')),
    version       TEXT        NOT NULL,
    min_supported TEXT        NOT NULL,
    platform      TEXT        NOT NULL CHECK (platform IN ('linux', 'windows', 'macos')),
    arch          TEXT        NOT NULL CHECK (arch IN ('x86_64', 'arm64')),
    sha256        CHAR(64)    NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size          BIGINT      NOT NULL CHECK (size > 0),
    signature     TEXT        NOT NULL,
    notes_url     TEXT        NOT NULL,
    artifact_key  TEXT        NOT NULL UNIQUE,
    published_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (channel, version, platform, arch)
);

CREATE INDEX IF NOT EXISTS releases_latest_idx
    ON releases (channel, platform, arch, published_at DESC);
