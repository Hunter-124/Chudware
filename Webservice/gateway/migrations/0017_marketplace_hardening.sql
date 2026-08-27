-- Marketplace hardening: partial refunds, and evidence bound to the claim it
-- was reviewed against.
--
-- Both of these close defects found by auditing 0016 rather than by using it.
-- The marketplace ships disabled (CHUDWARE_MARKETPLACE_ENABLED defaults false),
-- so these tables are empty everywhere and the constraints below can be added
-- and validated immediately instead of NOT VALID.


-- ---------------------------------------------------------------------------
-- Partial refunds
-- ---------------------------------------------------------------------------

-- 0016 modelled a refund as a single boolean transition: mk_orders.status went
-- to 'refunded' and the entitlement was deleted. The admin API meanwhile
-- deliberately accepts an `amount_cents`, so refunding one cent of a $100 order
-- revoked the buyer's entire download AND removed the whole order from every
-- revenue query (they filter status='paid'), understating revenue by $99.99. A
-- second partial refund was then refused outright because the order already
-- read as refunded.
--
-- Cumulative refunded amounts fix all three. The order stays 'paid' -- and stays
-- in revenue, netted -- until refunds reach the full amount, at which point it
-- becomes 'refunded' and the entitlement is revoked. Revenue is therefore
-- `amount - refunded`, which is the honest number for a partial refund and
-- identical to the old number for a full one.
ALTER TABLE mk_orders
    ADD COLUMN refunded_cents   INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN refunded_credits BIGINT  NOT NULL DEFAULT 0;

-- Cannot refund more than was charged, and cannot refund in a currency the
-- order was not paid in. The second half matters: without it a credits order
-- could accumulate a cents refund that no code path would ever pay out.
ALTER TABLE mk_orders
    ADD CONSTRAINT mk_orders_refunds_within_amount CHECK (
        refunded_cents >= 0
        AND refunded_credits >= 0
        AND refunded_cents <= amount_cents
        AND refunded_credits <= amount_credits
    );

-- 'refunded' means fully refunded, and nothing else may claim to be. This is
-- what lets a revenue query trust `status <> 'refunded'` as "still earned
-- something" while still needing to net out refunded_cents.
ALTER TABLE mk_orders
    ADD CONSTRAINT mk_orders_refunded_status_is_total CHECK (
        (status = 'refunded') = (
            (amount_cents > 0 AND refunded_cents = amount_cents)
            OR (amount_credits > 0 AND refunded_credits = amount_credits)
        )
        OR status IN ('pending', 'failed', 'canceled')
    );

-- Any refund at all implies the order was paid; refunded_at marks the FIRST
-- refund, not the completing one, so partial history is not lost.
ALTER TABLE mk_orders
    ADD CONSTRAINT mk_orders_refund_implies_paid CHECK (
        (refunded_cents = 0 AND refunded_credits = 0) OR paid_at IS NOT NULL
    );

-- The refund reporting scan. Partial, because refunded rows are a small
-- minority of a table the revenue dashboard range-scans constantly.
CREATE INDEX mk_orders_refunded
    ON mk_orders (paid_at DESC)
    WHERE refunded_cents > 0 OR refunded_credits > 0;


-- ---------------------------------------------------------------------------
-- Evidence is bound to the attestation it was reviewed against
-- ---------------------------------------------------------------------------

-- 0016 attached evidence to a LISTING, and the admin reviewer query joined
-- evidence by listing_id. Nothing stopped a seller from getting a human-made
-- claim verified, deleting the reviewed files through the ordinary owner asset
-- routes, and uploading different ones -- while the listing stayed 'verified'
-- and the reviewer UI showed the NEW files as though they were the reviewed
-- set. That defeats the only guarantee the human-made shelf makes.
--
-- Binding evidence to an attestation makes the reviewed set addressable and
-- immutable-by-construction: changing evidence now requires a new attestation,
-- and a new attestation is 'pending', so verification cannot survive a swap.
--
-- ON DELETE CASCADE: an attestation and its evidence are one artifact. Nullable,
-- because evidence is uploaded BEFORE the attestation row exists (the studio
-- collects files, then submits the claim), and marketplace binds the unbound
-- rows at submit time.
ALTER TABLE mk_assets
    ADD COLUMN attestation_id UUID REFERENCES mk_attestations (id) ON DELETE CASCADE;

-- Only evidence may carry a claim binding. A source file pointing at an
-- attestation would be meaningless and would make the reviewer query wrong.
ALTER TABLE mk_assets
    ADD CONSTRAINT mk_assets_attestation_only_for_evidence CHECK (
        attestation_id IS NULL OR kind = 'evidence'
    );

-- The reviewer's lookup: every file backing one claim.
CREATE INDEX mk_assets_by_attestation
    ON mk_assets (attestation_id) WHERE attestation_id IS NOT NULL;

-- Unbound evidence awaiting a claim submission. Bounded and short-lived, so a
-- partial index keeps the bind-at-submit query off a sequential scan.
CREATE INDEX mk_assets_unbound_evidence
    ON mk_assets (listing_id) WHERE kind = 'evidence' AND attestation_id IS NULL;
