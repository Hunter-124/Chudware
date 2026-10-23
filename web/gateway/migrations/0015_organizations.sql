-- Organizations, teams, memberships, and invitations.
--
-- An organization is a billing/ownership boundary; a team is a collaboration
-- boundary INSIDE one organization and owns exactly one shared bucket tree
-- (``bucket_root/orgs/<org_uuid>/teams/<team_uuid>``). They are distinct
-- entities on purpose: an account can belong to an org without belonging to
-- any of its teams, and team membership is what grants shared-file access for
-- an ordinary org member.
--
-- Identifiers are UUIDs minted in Python (no pgcrypto/uuid-ossp dependency on
-- the deployed cluster) and are the ONLY handle a client ever sees. They are
-- opaque: a non-member gets 404 for a well-formed id that exists, so the ids
-- cannot be used to enumerate customers. Because those ids are also path
-- components of the shared bucket tree, every route re-validates the canonical
-- UUID text form before it is joined onto a filesystem path.
--
-- Timestamps are TIMESTAMPTZ (not the epoch doubles the pre-Postgres accounts
-- table still carries) so invitation expiry is compared by the database in a
-- single time zone.

CREATE TABLE organizations (
    id         UUID        PRIMARY KEY,
    name       TEXT        NOT NULL CHECK (btrim(name) <> ''),
    -- Attribution only. No FK: deleting an account must not be blocked by, nor
    -- silently rewrite, the historical record of who created the org. Current
    -- ownership lives in org_members.role = 'owner'.
    created_by TEXT        NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Organization membership. 'owner' is the billing/authority root, 'admin' can
-- run the org day to day (invite, manage members and teams, reach every team's
-- files), 'member' can only reach the teams it actually belongs to.
CREATE TABLE org_members (
    org_id    UUID        NOT NULL REFERENCES organizations (id) ON DELETE CASCADE,
    email     TEXT        NOT NULL REFERENCES accounts (email) ON DELETE CASCADE,
    role      TEXT        NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, email)
);

-- Exactly one owner per organization, enforced by the database rather than by
-- application care: an owner transfer demotes the incumbent and promotes the
-- successor inside one transaction, and a bug that skipped the demotion would
-- otherwise leave two accounts each believing they hold the billing root.
CREATE UNIQUE INDEX org_members_single_owner
    ON org_members (org_id)
    WHERE role = 'owner';

-- "Which orgs am I in" is the hottest lookup in this schema: it runs on every
-- portal load and on every team-scoped bucket request.
CREATE INDEX org_members_by_email ON org_members (email);

CREATE TABLE teams (
    id         UUID        PRIMARY KEY,
    org_id     UUID        NOT NULL REFERENCES organizations (id) ON DELETE CASCADE,
    name       TEXT        NOT NULL CHECK (btrim(name) <> ''),
    -- The team every accepted invitation joins. Created with the org, never
    -- deletable, so "you are in the org" always implies "you have somewhere to
    -- put files". Renaming it is allowed; the flag, not the name, is the anchor.
    is_general BOOLEAN     NOT NULL DEFAULT FALSE,
    created_by TEXT        NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX teams_org_name_unique ON teams (org_id, lower(name));
CREATE UNIQUE INDEX teams_one_general_per_org ON teams (org_id) WHERE is_general;
CREATE INDEX teams_by_org ON teams (org_id);

-- Team membership. 'manager' may add/remove members and rename the team;
-- 'member' may read and write the team's shared bucket. Org owner/admin have
-- manager authority over every team WITHOUT a row here, which is why absence
-- of a row is never by itself a denial.
CREATE TABLE team_members (
    team_id  UUID        NOT NULL REFERENCES teams (id) ON DELETE CASCADE,
    email    TEXT        NOT NULL REFERENCES accounts (email) ON DELETE CASCADE,
    role     TEXT        NOT NULL CHECK (role IN ('manager', 'member')),
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (team_id, email)
);

CREATE INDEX team_members_by_email ON team_members (email);

-- Outstanding invitations. The plaintext token exists exactly once, in the
-- email that carried it: only its SHA-256 is stored, so a database leak cannot
-- be replayed into org membership. There is no FK on ``email`` because the
-- whole point is inviting an address that has no account yet.
CREATE TABLE org_invitations (
    id          UUID        PRIMARY KEY,
    org_id      UUID        NOT NULL REFERENCES organizations (id) ON DELETE CASCADE,
    email       TEXT        NOT NULL CHECK (btrim(email) <> ''),
    role        TEXT        NOT NULL CHECK (role IN ('admin', 'member')),
    token_hash  TEXT        NOT NULL UNIQUE,
    invited_by  TEXT        NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    accepted_at TIMESTAMPTZ,
    accepted_by TEXT        NOT NULL DEFAULT '',
    revoked_at  TIMESTAMPTZ
);

-- One live invitation per address per org. Re-inviting revokes the previous
-- one first, so a resend cannot leave a fan of independently redeemable links
-- behind (the same rule authn.store_token applies to verification mail).
CREATE UNIQUE INDEX org_invitations_one_open_per_email
    ON org_invitations (org_id, lower(email))
    WHERE accepted_at IS NULL AND revoked_at IS NULL;

-- "What am I invited to" for the portal's own list.
CREATE INDEX org_invitations_open_by_email
    ON org_invitations (lower(email))
    WHERE accepted_at IS NULL AND revoked_at IS NULL;
