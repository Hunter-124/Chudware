-- Transactional mail outbox.
--
-- Sends are queued here and drained by a single asyncio task in mailer.py.
-- Nothing is ever delivered inline on a request: a dead SMTP host or a
-- rate-limited Resend account must not turn a signup into a 500.
--
-- A row is claimed by incrementing `attempts` and pushing `next_attempt_at`
-- out by the backoff for that attempt, all in one UPDATE ... RETURNING, so a
-- worker that dies mid-delivery still burns exactly one attempt and the row
-- becomes eligible again when its lease expires. Once `attempts` reaches the
-- cap (5) the row stops being selected and stays as a permanent failure with
-- `last_error` for an operator to read.

CREATE TABLE IF NOT EXISTS mail_outbox (
    id              BIGSERIAL PRIMARY KEY,
    to_addr         TEXT        NOT NULL,
    template        TEXT        NOT NULL,
    ctx_json        TEXT        NOT NULL,
    attempts        INTEGER     NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL,
    sent_at         TIMESTAMPTZ,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The drain query is "undelivered, still under the attempt cap, lease expired,
-- oldest first". Partial index so delivered mail (the overwhelming majority of
-- the table over time) costs nothing to skip.
CREATE INDEX IF NOT EXISTS mail_outbox_due_idx
    ON mail_outbox (next_attempt_at, id)
    WHERE sent_at IS NULL;
