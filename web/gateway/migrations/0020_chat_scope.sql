-- Old portal-wide rows have no proven team ownership. Preserve them only for
-- the sender's personal history; new rows always name their resolved team/account.
ALTER TABLE messages ADD COLUMN scope_key TEXT;
UPDATE messages SET scope_key = 'personal:' || lower(email);
ALTER TABLE messages ALTER COLUMN scope_key SET NOT NULL;
CREATE INDEX idx_messages_scope_id ON messages (scope_key, id DESC);
