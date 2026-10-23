-- Tenant cells use an immutable account UUID, never an email-derived path.
-- PostgreSQL's built-in generator also covers direct SQL inserts and backfills.
ALTER TABLE accounts ADD COLUMN uid UUID DEFAULT gen_random_uuid();
ALTER TABLE accounts ALTER COLUMN uid SET NOT NULL;
ALTER TABLE accounts ADD CONSTRAINT accounts_uid_unique UNIQUE (uid);

CREATE FUNCTION prevent_account_uid_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.uid IS DISTINCT FROM OLD.uid THEN
        RAISE EXCEPTION 'accounts.uid is immutable' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER accounts_uid_immutable
BEFORE UPDATE OF uid ON accounts
FOR EACH ROW EXECUTE FUNCTION prevent_account_uid_update();

-- Viewers can read shared workspaces but cannot mutate their files or documents.
ALTER TABLE team_members DROP CONSTRAINT team_members_role_check;
ALTER TABLE team_members ADD CONSTRAINT team_members_role_check
    CHECK (role IN ('manager', 'member', 'viewer'));

-- Revocation history survives account, organization and team deletion. There
-- are deliberately no foreign keys: deleted principals must stay revoked.
CREATE TABLE cell_principal_epochs (
    workspace_key TEXT NOT NULL,
    actor_key UUID NOT NULL,
    epoch BIGINT NOT NULL DEFAULT 0 CHECK (epoch >= 0),
    PRIMARY KEY (workspace_key, actor_key)
);
