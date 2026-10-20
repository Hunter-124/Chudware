-- Account-scoped telemetry destinations, encrypted-mail retention, and settled
-- checkout identity. Keep telemetry mode IDs stable for signed device policies.
ALTER TABLE account_security_policies
    ADD COLUMN endpoint TEXT,
    ADD COLUMN config JSONB NOT NULL DEFAULT '{}'::jsonb;

UPDATE account_security_policies a
SET endpoint = p.endpoint, config = p.config
FROM security_policies p
WHERE p.id = a.policy_id;

ALTER TABLE account_security_policies
    ADD CONSTRAINT account_security_policies_config_check CHECK (config = '{}'::jsonb),
    ADD CONSTRAINT account_security_policies_endpoint_check CHECK (
        (policy_id = 'customer_endpoint' AND endpoint IS NOT NULL
         AND endpoint ~ '^https://[^/[:space:]]+(/.*)?$')
        OR (policy_id <> 'customer_endpoint' AND endpoint IS NULL)
    );

-- Global policies describe only modes; tenant-specific values cannot be written
-- here again. Existing assignments retain their last provisioned destination.
ALTER TABLE security_policies DROP COLUMN endpoint, DROP COLUMN config;
INSERT INTO security_policies (id, telemetry_mode)
VALUES ('customer_endpoint', 'customer_endpoint')
ON CONFLICT (id) DO NOTHING;

ALTER TABLE mail_outbox ADD COLUMN context_expires_at TIMESTAMPTZ;
-- Old plaintext queue capabilities are not a supported encrypted format.
-- Retire queued legacy messages and erase sent/abandoned contexts as well.
UPDATE mail_outbox
SET ctx_json = '{}',
    attempts = CASE WHEN sent_at IS NULL THEN 5 ELSE attempts END,
    last_error = CASE WHEN sent_at IS NULL THEN 'legacy plaintext context removed' ELSE last_error END;

CREATE TABLE billing_checkout_settlements (
    checkout_session_id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('payment', 'subscription')),
    stripe_payment_intent TEXT NOT NULL DEFAULT '',
    settled_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX billing_checkout_settlements_payment_intent_idx
    ON billing_checkout_settlements (stripe_payment_intent)
    WHERE stripe_payment_intent <> '';

CREATE UNIQUE INDEX mk_orders_payment_intent_unique
    ON mk_orders (stripe_payment_intent)
    WHERE stripe_payment_intent <> '';
