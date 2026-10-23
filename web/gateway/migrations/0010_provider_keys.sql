-- Account-scoped API credentials for browser AI turns.
-- encrypted_key is an AEAD envelope produced by provider_keys.py; plaintext
-- provider credentials must never enter PostgreSQL or its off-box backups.
CREATE TABLE provider_keys (
    email              TEXT        NOT NULL REFERENCES accounts (email) ON DELETE CASCADE,
    provider           TEXT        NOT NULL,
    encrypted_key      BYTEA       NOT NULL,
    key_hint           TEXT        NOT NULL CHECK (char_length(key_hint) BETWEEN 1 AND 4),
    validation_state   TEXT        NOT NULL CHECK (validation_state = 'validated'),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_validated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at       TIMESTAMPTZ,
    PRIMARY KEY (email, provider)
);

CREATE INDEX provider_keys_validated_by_account
    ON provider_keys (email, provider)
    WHERE validation_state = 'validated';
