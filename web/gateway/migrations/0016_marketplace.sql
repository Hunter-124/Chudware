-- The 3D model marketplace: listings, assets, commerce, community, moderation,
-- and third-party import plumbing.
--
-- Three deliberate shape decisions run through the whole schema.
--
-- 1. TWO CURRENCIES, TWO CUSTODY MODELS. A listing may be sold for cash, for
--    site credits, for either, or given away. Cash sales are Stripe Connect
--    *destination charges*: Stripe collects, takes our `application_fee_amount`
--    and pays the seller. The gateway therefore never custodies cash and
--    ``mk_orders`` records cash only for reporting. Credits ARE our own
--    liability, so a credit sale moves value inside ``credit_ledger`` (new
--    ``market_buy`` / ``market_earn`` / ``market_payout`` kinds added at the
--    bottom of this file) and a seller's earned credits are the same balance
--    they spend on AI tokens. That equivalence is the product: site currency is
--    AI tokens, so earning it is immediately useful and needs no cash-out.
--
-- 2. PROVENANCE IS NOT OPTIONAL. ``mk_listings.origin`` is NOT NULL with no
--    default, so a listing cannot exist without disclosing whether a human or a
--    generative model produced it. The human-made section is not a tag anyone
--    can self-apply for free: claiming ``origin = 'human'`` requires a matching
--    ``mk_attestations`` row, and only a reviewer moves it to 'verified'. The
--    partial unique index on ``mk_attestations.listing_id`` keeps that one-to-one.
--
-- 3. OPEN SOURCE IS A FIRST-CLASS SECTION, NOT A PRICE OF ZERO.
--    ``mk_listings.open_source`` is a stored boolean rather than a query-time
--    join because the open-source shelf is the most-served query in the whole
--    schema and must be a single index scan. It is derived (license is an
--    open-source license AND nothing is being charged) and
--    ``marketplace.derive_open_source()`` is its ONLY writer;
--    ``tests/test_marketplace.py::test_open_source_flag_matches_license`` is the
--    guard that keeps the denormalisation honest.
--
-- Identifiers are UUIDs minted in Python, matching 0015_organizations.sql: the
-- deployed cluster has neither pgcrypto nor uuid-ossp. Timestamps are
-- TIMESTAMPTZ, not the epoch doubles the pre-Postgres ``accounts`` table still
-- carries, because every marketplace window (publication, review SLA, payout
-- eligibility) is compared by the database.


-- ---------------------------------------------------------------------------
-- Reference data: licenses, categories, external sources
-- ---------------------------------------------------------------------------

-- Licenses are a table rather than a Python enum because the FILTER semantics
-- users actually want ("show me things I can use commercially", "hide
-- share-alike") are properties of the license, and putting them here lets one
-- indexed join answer a facet query instead of the API post-filtering pages.
CREATE TABLE mk_licenses (
    id                   TEXT PRIMARY KEY,
    name                 TEXT NOT NULL,
    url                  TEXT NOT NULL DEFAULT '',
    -- Drives the open-source shelf. SPDX open licenses only; a "free download,
    -- all rights reserved" license is free-as-in-beer and deliberately FALSE.
    is_open_source       BOOLEAN NOT NULL,
    allows_commercial    BOOLEAN NOT NULL,
    allows_derivatives   BOOLEAN NOT NULL,
    requires_attribution BOOLEAN NOT NULL,
    share_alike          BOOLEAN NOT NULL,
    -- A seller may only attach a license to a PAID listing if it permits it.
    -- CC-BY-NC on a listing you are charging for is a contradiction, and CC0 on
    -- a paid listing is legal but misleads the buyer about what they bought.
    sellable             BOOLEAN NOT NULL DEFAULT TRUE,
    sort_order           INTEGER NOT NULL DEFAULT 100,
    active               BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX mk_licenses_open_source ON mk_licenses (sort_order) WHERE is_open_source AND active;

-- Two levels only (parent IS NULL = top shelf). A deeper tree reads well in a
-- schema diagram and is miserable in a filter sidebar; the tag array carries
-- the long tail instead.
CREATE TABLE mk_categories (
    id         TEXT PRIMARY KEY,
    parent_id  TEXT REFERENCES mk_categories (id) ON DELETE RESTRICT,
    name       TEXT NOT NULL,
    -- Short glyph/emoji rendered in the sidebar. Presentation lives with the
    -- taxonomy so adding a category needs no front-end deploy.
    icon       TEXT NOT NULL DEFAULT '',
    blurb      TEXT NOT NULL DEFAULT '',
    sort_order INTEGER NOT NULL DEFAULT 100,
    active     BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT mk_categories_no_self_parent CHECK (parent_id IS NULL OR parent_id <> id)
);

CREATE INDEX mk_categories_by_parent ON mk_categories (parent_id, sort_order);

-- Third-party model sites. ``kind`` records how much we can lawfully do with
-- each one, and it is the ONLY thing that decides whether a file may be
-- fetched:
--   'api'      documented public API, direct download permitted by its terms
--   'oauth'    per-user OAuth; the user's own token authorises their own files
--   'metadata' public page metadata (OpenGraph/JSON-LD) may be read; files may
--              NOT be fetched, because the site gates them behind its own terms
--   'link'     reference only; we store a URL and nothing else
-- ``supports_file_fetch`` is therefore never TRUE for 'metadata' or 'link', and
-- marketplace_sources.py refuses to download when it is FALSE. This is a
-- compliance boundary, not a capability gap: do not "fix" it by flipping a flag.
CREATE TABLE mk_sources (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    url                 TEXT NOT NULL DEFAULT '',
    kind                TEXT NOT NULL,
    supports_file_fetch BOOLEAN NOT NULL DEFAULT FALSE,
    -- Set FALSE to retire a source without orphaning the listings that came
    -- from it (mk_listings.source_id is ON DELETE RESTRICT for that reason).
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    notes               TEXT NOT NULL DEFAULT '',
    sort_order          INTEGER NOT NULL DEFAULT 100,
    CONSTRAINT mk_sources_kind CHECK (kind IN ('api', 'oauth', 'metadata', 'link')),
    CONSTRAINT mk_sources_fetch_requires_grant
        CHECK (NOT supports_file_fetch OR kind IN ('api', 'oauth'))
);


-- ---------------------------------------------------------------------------
-- Sellers
-- ---------------------------------------------------------------------------

-- A seller is an ACCOUNT that opted in, not a new identity: the PK is the
-- account email so every existing per-tenant helper (limits.user_slug, the
-- workspace jail, the credit ledger) applies unchanged.
--
-- The Stripe Connect columns are a CACHE of the connected account's state.
-- Stripe is authoritative; ``connect_refreshed_at`` says how stale this copy is
-- and marketplace_commerce.refresh_connect() re-reads it before any decision
-- that turns on ``connect_charges_enabled``. Never gate a payout on these
-- columns without a refresh: a seller whose Connect account was restricted an
-- hour ago still looks payable here.
CREATE TABLE mk_sellers (
    email                     TEXT PRIMARY KEY REFERENCES accounts (email) ON DELETE CASCADE,
    -- URL identity: /marketplace/@handle. Immutable once a listing exists so
    -- shared links never rot (enforced in marketplace.py, not by the schema,
    -- because a seller with zero listings may still rename freely).
    handle                    TEXT NOT NULL,
    display_name              TEXT NOT NULL,
    bio                       TEXT NOT NULL DEFAULT '',
    website                   TEXT NOT NULL DEFAULT '',
    country                   TEXT NOT NULL DEFAULT '',
    -- Selling as a team. NULL = selling as an individual. ON DELETE SET NULL so
    -- dissolving an org demotes the storefront rather than deleting its history.
    org_id                    UUID REFERENCES organizations (id) ON DELETE SET NULL,
    connect_account_id        TEXT NOT NULL DEFAULT '',
    connect_charges_enabled   BOOLEAN NOT NULL DEFAULT FALSE,
    connect_payouts_enabled   BOOLEAN NOT NULL DEFAULT FALSE,
    connect_details_submitted BOOLEAN NOT NULL DEFAULT FALSE,
    connect_refreshed_at      TIMESTAMPTZ,
    -- Editorial trust marks, both admin-only. `verified` is identity; `featured`
    -- is curation. Neither is purchasable.
    verified                  BOOLEAN NOT NULL DEFAULT FALSE,
    featured                  BOOLEAN NOT NULL DEFAULT FALSE,
    -- Set when an operator bars someone from publishing. Existing listings stay
    -- reachable for buyers who already paid; new writes are refused.
    publishing_blocked        BOOLEAN NOT NULL DEFAULT FALSE,
    blocked_reason            TEXT NOT NULL DEFAULT '',
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mk_sellers_handle_shape CHECK (handle ~ '^[a-z0-9][a-z0-9_-]{1,38}$'),
    CONSTRAINT mk_sellers_display_name_present CHECK (length(btrim(display_name)) > 0)
);

CREATE UNIQUE INDEX mk_sellers_handle_unique ON mk_sellers (lower(handle));
-- One connected account per seller, and one seller per connected account: a
-- shared connect id would send one seller's money to another.
CREATE UNIQUE INDEX mk_sellers_connect_unique
    ON mk_sellers (connect_account_id) WHERE connect_account_id <> '';
CREATE INDEX mk_sellers_featured ON mk_sellers (created_at DESC) WHERE featured;


-- ---------------------------------------------------------------------------
-- Listings
-- ---------------------------------------------------------------------------

CREATE TABLE mk_listings (
    id                 UUID PRIMARY KEY,
    -- Stable public URL component. Kept even after unpublishing so an inbound
    -- link resolves to "withdrawn" rather than 404.
    slug               TEXT NOT NULL,
    seller_email       TEXT NOT NULL REFERENCES mk_sellers (email) ON DELETE CASCADE,
    title              TEXT NOT NULL,
    summary            TEXT NOT NULL DEFAULT '',
    description        TEXT NOT NULL DEFAULT '',
    category_id        TEXT REFERENCES mk_categories (id) ON DELETE RESTRICT,
    license_id         TEXT NOT NULL REFERENCES mk_licenses (id) ON DELETE RESTRICT,

    -- Lifecycle. 'in_review' exists because a first-time seller's first paid
    -- listing is reviewed; 'unlisted' is seller-initiated withdrawal (buyers who
    -- paid keep their download); 'removed' is moderator takedown.
    status             TEXT NOT NULL DEFAULT 'draft',

    pricing_kind       TEXT NOT NULL DEFAULT 'free',
    price_cents        INTEGER NOT NULL DEFAULT 0,
    price_credits      BIGINT NOT NULL DEFAULT 0,
    -- Donations on a free listing. The whole point of the open-source shelf is
    -- that "free" and "unrewarded" are different things.
    tips_enabled       BOOLEAN NOT NULL DEFAULT FALSE,

    -- Provenance. No default: a caller must say.
    origin             TEXT NOT NULL,
    origin_note        TEXT NOT NULL DEFAULT '',
    attestation_status TEXT NOT NULL DEFAULT 'none',

    -- Derived, single-writer denormalisation. See header note 3.
    open_source        BOOLEAN NOT NULL DEFAULT FALSE,

    -- Remix lineage. ON DELETE SET NULL, not CASCADE: deleting an upstream
    -- listing must never silently delete every derivative built on it.
    forked_from        UUID REFERENCES mk_listings (id) ON DELETE SET NULL,

    -- Third-party origin. NULL source_id = authored here.
    source_id          TEXT REFERENCES mk_sources (id) ON DELETE RESTRICT,
    source_url         TEXT NOT NULL DEFAULT '',
    source_author      TEXT NOT NULL DEFAULT '',

    tags               TEXT[] NOT NULL DEFAULT '{}',

    -- Geometry facts, populated by the render/ingest pipeline. Buyers filter on
    -- these ("fits my printer"), so they are columns rather than JSON.
    units              TEXT NOT NULL DEFAULT 'mm',
    bbox_mm            DOUBLE PRECISION[],
    part_count         INTEGER NOT NULL DEFAULT 0,
    triangle_count     BIGINT NOT NULL DEFAULT 0,
    -- Denormalised primary image so a 60-card grid needs no per-row asset join.
    -- marketplace_render.py is its only writer.
    thumbnail_asset_id UUID,

    -- Counters. Denormalised on purpose: the grid sorts by them.
    views              BIGINT NOT NULL DEFAULT 0,
    downloads          BIGINT NOT NULL DEFAULT 0,
    favorites          INTEGER NOT NULL DEFAULT 0,
    sales              INTEGER NOT NULL DEFAULT 0,
    rating_sum         INTEGER NOT NULL DEFAULT 0,
    rating_count       INTEGER NOT NULL DEFAULT 0,

    -- Editorial curation of the LISTING, distinct from mk_sellers.featured which
    -- curates the storefront's people. Both exist because the homepage needs to
    -- promote one specific model without endorsing everything its author sells.
    featured           BOOLEAN NOT NULL DEFAULT FALSE,
    featured_at        TIMESTAMPTZ,

    review_note        TEXT NOT NULL DEFAULT '',
    reviewed_by        TEXT NOT NULL DEFAULT '',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at       TIMESTAMPTZ,

    CONSTRAINT mk_listings_slug_shape CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,78}$'),
    CONSTRAINT mk_listings_title_len CHECK (length(btrim(title)) BETWEEN 3 AND 140),
    CONSTRAINT mk_listings_summary_len CHECK (length(summary) <= 280),
    CONSTRAINT mk_listings_description_len CHECK (length(description) <= 40000),
    CONSTRAINT mk_listings_status CHECK (
        status IN ('draft', 'in_review', 'published', 'rejected', 'unlisted', 'removed')
    ),
    CONSTRAINT mk_listings_pricing_kind CHECK (
        pricing_kind IN ('free', 'cash', 'credits', 'cash_or_credits')
    ),
    CONSTRAINT mk_listings_origin CHECK (origin IN ('human', 'ai_assisted', 'ai_generated')),
    CONSTRAINT mk_listings_attestation_status CHECK (
        attestation_status IN ('none', 'pending', 'verified', 'revoked')
    ),
    CONSTRAINT mk_listings_price_nonneg CHECK (price_cents >= 0 AND price_credits >= 0),
    CONSTRAINT mk_listings_counters_nonneg CHECK (
        views >= 0 AND downloads >= 0 AND favorites >= 0 AND sales >= 0
        AND rating_sum >= 0 AND rating_count >= 0
        AND part_count >= 0 AND triangle_count >= 0
    ),
    -- The prices a pricing_kind actually uses must be set, and the ones it does
    -- not use must be zero. Without the second half a listing switched from
    -- 'cash' to 'free' keeps a stale price_cents that the UI would happily show.
    CONSTRAINT mk_listings_price_matches_kind CHECK (
        CASE pricing_kind
            WHEN 'free'            THEN price_cents = 0 AND price_credits = 0
            WHEN 'cash'            THEN price_cents > 0 AND price_credits = 0
            WHEN 'credits'         THEN price_cents = 0 AND price_credits > 0
            WHEN 'cash_or_credits' THEN price_cents > 0 AND price_credits > 0
        END
    ),
    -- Stripe's own floor for a USD charge. Rejecting it here beats discovering
    -- it when a buyer's checkout 400s.
    CONSTRAINT mk_listings_cash_floor CHECK (price_cents = 0 OR price_cents >= 100),
    -- An open-source listing is never sold. Header note 3's invariant, half of
    -- it enforced by the database because it is a one-way implication.
    CONSTRAINT mk_listings_open_source_is_free CHECK (
        NOT open_source OR pricing_kind = 'free'
    ),
    -- Only a human-made claim can carry an attestation state.
    CONSTRAINT mk_listings_attestation_needs_human CHECK (
        attestation_status = 'none' OR origin = 'human'
    ),
    -- A published listing must have a publication timestamp, and nothing else
    -- may have one. Sorting the shelf by published_at DESC depends on it.
    CONSTRAINT mk_listings_published_at_matches_status CHECK (
        (status = 'published') = (published_at IS NOT NULL)
    ),
    CONSTRAINT mk_listings_no_self_fork CHECK (forked_from IS NULL OR forked_from <> id),
    -- 24 tags is already more than any sidebar shows; the cap stops a seller
    -- keyword-stuffing the FTS document below.
    CONSTRAINT mk_listings_tag_count CHECK (cardinality(tags) <= 24),
    -- ``array_to_tsvector`` in the generated column below rejects a NULL
    -- element with a runtime error rather than ignoring it, so a NULL tag would
    -- make the row unwritable with an opaque message. Refuse it by name here.
    CONSTRAINT mk_listings_tags_not_null_elements CHECK (array_position(tags, NULL) IS NULL),
    CONSTRAINT mk_listings_bbox_shape CHECK (bbox_mm IS NULL OR cardinality(bbox_mm) = 3),
    -- Same pairing discipline as published_at/status: an editorially featured
    -- listing has a timestamp and nothing else does, so "recently featured"
    -- is a real ordering rather than a guess.
    CONSTRAINT mk_listings_featured_at_matches CHECK (featured = (featured_at IS NOT NULL))
);

CREATE UNIQUE INDEX mk_listings_slug_unique ON mk_listings (lower(slug));

-- The storefront's four shelves, each a single index scan.
CREATE INDEX mk_listings_shelf_new
    ON mk_listings (published_at DESC) WHERE status = 'published';
CREATE INDEX mk_listings_shelf_open_source
    ON mk_listings (published_at DESC) WHERE status = 'published' AND open_source;
-- The human-made shelf: verified provenance only, which is the whole promise.
CREATE INDEX mk_listings_shelf_human
    ON mk_listings (published_at DESC)
    WHERE status = 'published' AND origin = 'human' AND attestation_status = 'verified';
CREATE INDEX mk_listings_shelf_downloads
    ON mk_listings (downloads DESC) WHERE status = 'published';
-- The curated homepage band. Editorial picks are a handful of rows, so this is
-- partial on both conditions.
CREATE INDEX mk_listings_shelf_featured
    ON mk_listings (featured_at DESC) WHERE status = 'published' AND featured;

CREATE INDEX mk_listings_by_seller ON mk_listings (seller_email, updated_at DESC);
CREATE INDEX mk_listings_by_category
    ON mk_listings (category_id, published_at DESC) WHERE status = 'published';
CREATE INDEX mk_listings_by_license
    ON mk_listings (license_id) WHERE status = 'published';
CREATE INDEX mk_listings_tags ON mk_listings USING GIN (tags);
CREATE INDEX mk_listings_forks ON mk_listings (forked_from) WHERE forked_from IS NOT NULL;
CREATE INDEX mk_listings_by_source ON mk_listings (source_id) WHERE source_id IS NOT NULL;
-- The moderation queue. Partial, because 'in_review' is a handful of rows in a
-- table that will hold millions.
CREATE INDEX mk_listings_review_queue
    ON mk_listings (created_at) WHERE status = 'in_review';

-- Full-text search as a STORED generated column: the alternative is a trigger
-- (which this schema otherwise has none of) or building the tsquery against
-- concatenated columns at query time, which cannot use an index. Weights are
-- the ranking policy in one place — title beats tags beats body.
--
-- Tags go through ``array_to_tsvector``, not ``to_tsvector(array_to_string(...))``,
-- for two independent reasons. First, ``array_to_string`` is only STABLE, so the
-- expression is rejected outright: "generation expression is not immutable".
-- Second, it is the behaviour we want anyway — ``array_to_tsvector`` emits each
-- element as a lexeme verbatim with no stemming or stop-word removal, and CAD
-- tags are part numbers, standards and abbreviations ('m3', 'iso4762',
-- 'nema17') that an English stemmer mangles. It requires already-normalised
-- input, which is why marketplace.normalize_tags() lower-cases and de-duplicates
-- before any write.
ALTER TABLE mk_listings
    ADD COLUMN search_doc tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A')
        || setweight(array_to_tsvector(tags), 'B')
        || setweight(to_tsvector('english', coalesce(summary, '')), 'C')
        || setweight(to_tsvector('english', coalesce(description, '')), 'D')
    ) STORED;

CREATE INDEX mk_listings_search ON mk_listings USING GIN (search_doc);
-- Trigram index for the type-ahead box, which fires on prefixes too short for
-- FTS to match. Created only when pg_trgm is available: it ships in
-- postgresql-contrib, and a missing extension must degrade to plain FTS rather
-- than fail the migration on a minimal cluster.
DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
    CREATE INDEX mk_listings_title_trgm ON mk_listings USING GIN (title gin_trgm_ops);
EXCEPTION WHEN insufficient_privilege OR feature_not_supported OR undefined_file THEN
    RAISE NOTICE 'pg_trgm unavailable; marketplace type-ahead falls back to full-text search';
END
$$;


-- ---------------------------------------------------------------------------
-- Versions and assets
-- ---------------------------------------------------------------------------

CREATE TABLE mk_versions (
    id         UUID PRIMARY KEY,
    listing_id UUID NOT NULL REFERENCES mk_listings (id) ON DELETE CASCADE,
    label      TEXT NOT NULL,
    changelog  TEXT NOT NULL DEFAULT '',
    is_current BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mk_versions_label_shape CHECK (length(btrim(label)) BETWEEN 1 AND 40),
    CONSTRAINT mk_versions_changelog_len CHECK (length(changelog) <= 8000)
);

CREATE UNIQUE INDEX mk_versions_label_unique ON mk_versions (listing_id, lower(label));
-- Exactly one current version per listing, in the database rather than in
-- application care: two "current" rows means the buy button and the download
-- button disagree about what is being sold.
CREATE UNIQUE INDEX mk_versions_one_current ON mk_versions (listing_id) WHERE is_current;

-- Every byte a listing owns: the CAD sources buyers pay for, the generated
-- previews anyone may see, and the evidence attached to a human-made claim.
--
-- ``gated`` is the access decision and it is stored per asset, not inferred from
-- ``kind``: a seller may deliberately expose one STL as a free sample of a paid
-- listing, and a render is ungated even on a paid listing because it is the
-- shop window. marketplace.asset_visible() is the single reader of this flag.
CREATE TABLE mk_assets (
    id           UUID PRIMARY KEY,
    listing_id   UUID NOT NULL REFERENCES mk_listings (id) ON DELETE CASCADE,
    version_id   UUID REFERENCES mk_versions (id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT '',
    filename     TEXT NOT NULL,
    -- Relative to ``settings.listing_asset_root(listing_id)`` — the LISTING's
    -- directory, not the marketplace root. Anchoring per listing rather than at
    -- the root means a row physically cannot name another listing's bytes even
    -- if the containment check were skipped, because the listing id is supplied
    -- by the joiner and never by the row. The absolute path is never stored, so
    -- relocating storage is a config change. ``marketplace.asset_path()`` is the
    -- only joiner and re-verifies containment after resolve().
    rel_path     TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    format       TEXT NOT NULL DEFAULT '',
    bytes        BIGINT NOT NULL DEFAULT 0,
    sha256       TEXT NOT NULL DEFAULT '',
    width        INTEGER,
    height       INTEGER,
    sort_order   INTEGER NOT NULL DEFAULT 0,
    gated        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mk_assets_kind CHECK (
        kind IN ('source', 'export', 'image', 'render', 'document', 'evidence')
    ),
    CONSTRAINT mk_assets_role CHECK (
        role IN ('', 'primary', 'thumbnail', 'contact_sheet', 'gallery', 'sample')
    ),
    CONSTRAINT mk_assets_bytes_nonneg CHECK (bytes >= 0),
    CONSTRAINT mk_assets_rel_path_relative CHECK (
        rel_path <> '' AND left(rel_path, 1) <> '/' AND rel_path NOT LIKE '%..%'
    ),
    -- Generated previews are the shop window and can never be paywalled;
    -- evidence backs a provenance claim and is reviewer-only, never public.
    CONSTRAINT mk_assets_render_ungated CHECK (kind <> 'render' OR NOT gated),
    CONSTRAINT mk_assets_evidence_gated CHECK (kind <> 'evidence' OR gated)
);

CREATE INDEX mk_assets_by_listing ON mk_assets (listing_id, kind, sort_order);
CREATE UNIQUE INDEX mk_assets_one_thumbnail
    ON mk_assets (listing_id) WHERE role = 'thumbnail';
-- Content-addressed dedupe within a listing: re-uploading the same STEP file
-- under a second name is a mistake, not a feature.
CREATE UNIQUE INDEX mk_assets_sha_unique
    ON mk_assets (listing_id, sha256) WHERE sha256 <> '';

-- Deferred until mk_assets exists. NOT VALID would let existing rows escape;
-- the table is empty at migration time, so validate immediately.
ALTER TABLE mk_listings
    ADD CONSTRAINT mk_listings_thumbnail_fk
    FOREIGN KEY (thumbnail_asset_id) REFERENCES mk_assets (id) ON DELETE SET NULL;


-- ---------------------------------------------------------------------------
-- Provenance attestation (the human-made section)
-- ---------------------------------------------------------------------------

-- What a seller signs to claim a model was made by a person. The bar is
-- deliberately a paragraph of process rather than a checkbox: a checkbox costs
-- nothing to lie on, and the review queue needs something a reviewer can judge.
CREATE TABLE mk_attestations (
    id            UUID PRIMARY KEY,
    listing_id    UUID NOT NULL REFERENCES mk_listings (id) ON DELETE CASCADE,
    email         TEXT NOT NULL REFERENCES accounts (email) ON DELETE CASCADE,
    -- How it was made. The 120-char floor is the review bar; see the CHECK.
    process_note  TEXT NOT NULL,
    -- CAD packages, machines, measuring kit. Free-text array so a reviewer sees
    -- 'calipers + SolidWorks 2019' rather than a closed vocabulary we guessed.
    tools         TEXT[] NOT NULL DEFAULT '{}',
    hours         NUMERIC(7, 2),
    -- An honest 'I used AI for the fillet pattern' belongs on a human-made
    -- listing; a hidden one is the fraud. Declaring it does not fail review, it
    -- routes to origin='ai_assisted' if the reviewer disagrees with the balance.
    ai_tools_note TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',
    submitted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at   TIMESTAMPTZ,
    reviewed_by   TEXT NOT NULL DEFAULT '',
    review_note   TEXT NOT NULL DEFAULT '',
    CONSTRAINT mk_attestations_status CHECK (
        status IN ('pending', 'verified', 'rejected', 'revoked')
    ),
    CONSTRAINT mk_attestations_process_note_substantive
        CHECK (length(btrim(process_note)) BETWEEN 120 AND 8000),
    CONSTRAINT mk_attestations_hours_sane CHECK (hours IS NULL OR hours > 0),
    CONSTRAINT mk_attestations_reviewed_together CHECK (
        (reviewed_at IS NULL) = (reviewed_by = '')
    )
);

-- One live claim per listing; a revoked one is history and may be superseded.
CREATE UNIQUE INDEX mk_attestations_one_live
    ON mk_attestations (listing_id) WHERE status <> 'revoked';
CREATE INDEX mk_attestations_queue
    ON mk_attestations (submitted_at) WHERE status = 'pending';


-- ---------------------------------------------------------------------------
-- Commerce
-- ---------------------------------------------------------------------------

-- One row per attempted transfer of value. Cash rows are a REPORTING record of
-- a Stripe destination charge (Stripe holds the money and pays the seller);
-- credit rows are the authoritative record of an internal balance move whose
-- double entry lives in credit_ledger.
CREATE TABLE mk_orders (
    id                      UUID PRIMARY KEY,
    -- RESTRICT, not CASCADE: a paid order is a financial record and must
    -- outlive any attempt to delete the thing it bought.
    listing_id              UUID NOT NULL REFERENCES mk_listings (id) ON DELETE RESTRICT,
    version_id              UUID REFERENCES mk_versions (id) ON DELETE SET NULL,
    buyer_email             TEXT NOT NULL REFERENCES accounts (email) ON DELETE RESTRICT,
    -- Denormalised at sale time on purpose: if the listing changes hands, the
    -- order still records who was actually paid.
    seller_email            TEXT NOT NULL,
    kind                    TEXT NOT NULL DEFAULT 'purchase',
    currency                TEXT NOT NULL,
    amount_cents            INTEGER NOT NULL DEFAULT 0,
    amount_credits          BIGINT NOT NULL DEFAULT 0,
    -- Our cut, resolved at sale time from settings so a later fee change cannot
    -- rewrite history.
    fee_cents               INTEGER NOT NULL DEFAULT 0,
    fee_credits             BIGINT NOT NULL DEFAULT 0,
    net_cents               INTEGER NOT NULL DEFAULT 0,
    net_credits             BIGINT NOT NULL DEFAULT 0,
    status                  TEXT NOT NULL DEFAULT 'pending',
    stripe_checkout_session TEXT NOT NULL DEFAULT '',
    stripe_payment_intent   TEXT NOT NULL DEFAULT '',
    -- The exactly-once key for webhook-driven settlement, mirroring
    -- credit_ledger.stripe_event_id.
    stripe_event_id         TEXT,
    failure_reason          TEXT NOT NULL DEFAULT '',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    paid_at                 TIMESTAMPTZ,
    refunded_at             TIMESTAMPTZ,
    CONSTRAINT mk_orders_kind CHECK (kind IN ('purchase', 'tip')),
    CONSTRAINT mk_orders_currency CHECK (currency IN ('usd', 'credits')),
    CONSTRAINT mk_orders_status CHECK (
        status IN ('pending', 'paid', 'refunded', 'failed', 'canceled')
    ),
    CONSTRAINT mk_orders_amounts_nonneg CHECK (
        amount_cents >= 0 AND amount_credits >= 0
        AND fee_cents >= 0 AND fee_credits >= 0
        AND net_cents >= 0 AND net_credits >= 0
    ),
    -- The currency decides which pair of columns is live; the other pair must be
    -- zero so SUM() over the table is never silently mixing units.
    CONSTRAINT mk_orders_currency_columns CHECK (
        CASE currency
            WHEN 'usd'     THEN amount_credits = 0 AND fee_credits = 0 AND net_credits = 0
                                AND amount_cents > 0
            WHEN 'credits' THEN amount_cents = 0 AND fee_cents = 0 AND net_cents = 0
                                AND amount_credits > 0
        END
    ),
    -- Money must not appear or vanish in the split.
    CONSTRAINT mk_orders_split_balances CHECK (
        fee_cents + net_cents = amount_cents
        AND fee_credits + net_credits = amount_credits
    ),
    CONSTRAINT mk_orders_paid_at_matches_status CHECK (
        status IN ('pending', 'failed', 'canceled') = (paid_at IS NULL)
    ),
    CONSTRAINT mk_orders_refunded_implies_paid CHECK (
        refunded_at IS NULL OR paid_at IS NOT NULL
    ),
    -- A seller cannot buy from themselves: it is the obvious way to launder a
    -- credit grant into a cash payout, and the obvious way to fake sales rank.
    CONSTRAINT mk_orders_no_self_dealing CHECK (buyer_email <> seller_email)
);

CREATE UNIQUE INDEX mk_orders_stripe_event_once
    ON mk_orders (stripe_event_id) WHERE stripe_event_id IS NOT NULL;
CREATE UNIQUE INDEX mk_orders_checkout_session_unique
    ON mk_orders (stripe_checkout_session) WHERE stripe_checkout_session <> '';
-- One paid purchase per buyer per listing. Tips are excluded (you may tip
-- repeatedly) and so are failures (a retry after a declined card is legitimate).
CREATE UNIQUE INDEX mk_orders_one_purchase_per_buyer
    ON mk_orders (listing_id, buyer_email)
    WHERE kind = 'purchase' AND status IN ('pending', 'paid');
CREATE INDEX mk_orders_by_buyer ON mk_orders (buyer_email, created_at DESC);
CREATE INDEX mk_orders_by_seller ON mk_orders (seller_email, created_at DESC);
CREATE INDEX mk_orders_by_listing ON mk_orders (listing_id, created_at DESC);
-- The revenue dashboard's range scan.
CREATE INDEX mk_orders_paid_timeline
    ON mk_orders (paid_at DESC) WHERE status = 'paid';

-- Who may download the gated assets of a listing. A row here is the ONLY thing
-- that unlocks a paid download; free and open-source listings are served
-- without one, so this table stays proportional to sales rather than traffic.
CREATE TABLE mk_entitlements (
    listing_id UUID NOT NULL REFERENCES mk_listings (id) ON DELETE CASCADE,
    email      TEXT NOT NULL REFERENCES accounts (email) ON DELETE CASCADE,
    -- SET NULL rather than CASCADE: revoking or deleting an order must not
    -- silently repossess a download the buyer already paid for. Refunds revoke
    -- explicitly, in marketplace_commerce.refund_order().
    order_id   UUID REFERENCES mk_orders (id) ON DELETE SET NULL,
    source     TEXT NOT NULL DEFAULT 'purchase',
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (listing_id, email),
    CONSTRAINT mk_entitlements_source CHECK (
        source IN ('purchase', 'tip', 'seller', 'admin', 'import')
    )
);

CREATE INDEX mk_entitlements_by_email ON mk_entitlements (email, granted_at DESC);

-- Seller cash-out of earned CREDITS. Cash sales need nothing here — Stripe pays
-- those out directly. This exists because credits earned on the site are a real
-- liability, and a seller who would rather have money than AI tokens has to be
-- able to say so. Admin-approved because it is the one path that turns an
-- internal balance into an outbound transfer.
CREATE TABLE mk_payout_requests (
    id                 UUID PRIMARY KEY,
    seller_email       TEXT NOT NULL REFERENCES mk_sellers (email) ON DELETE CASCADE,
    credits            BIGINT NOT NULL,
    -- Rate frozen at request time; a later change to the conversion rate must
    -- not silently re-price a pending request.
    cents              INTEGER NOT NULL,
    credits_per_dollar INTEGER NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending',
    stripe_transfer_id TEXT NOT NULL DEFAULT '',
    note               TEXT NOT NULL DEFAULT '',
    requested_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at         TIMESTAMPTZ,
    decided_by         TEXT NOT NULL DEFAULT '',
    CONSTRAINT mk_payout_requests_status CHECK (
        status IN ('pending', 'approved', 'paid', 'denied', 'failed')
    ),
    CONSTRAINT mk_payout_requests_amounts CHECK (
        credits > 0 AND cents > 0 AND credits_per_dollar > 0
    ),
    CONSTRAINT mk_payout_requests_decided_together CHECK (
        (decided_at IS NULL) = (decided_by = '')
    )
);

-- One open request per seller: concurrent requests could together exceed the
-- balance, and each looks affordable on its own.
CREATE UNIQUE INDEX mk_payout_requests_one_open
    ON mk_payout_requests (seller_email) WHERE status IN ('pending', 'approved');
CREATE INDEX mk_payout_requests_queue
    ON mk_payout_requests (requested_at) WHERE status = 'pending';


-- ---------------------------------------------------------------------------
-- Community
-- ---------------------------------------------------------------------------

CREATE TABLE mk_reviews (
    id         UUID PRIMARY KEY,
    listing_id UUID NOT NULL REFERENCES mk_listings (id) ON DELETE CASCADE,
    email      TEXT NOT NULL REFERENCES accounts (email) ON DELETE CASCADE,
    rating     SMALLINT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    -- Earned by holding an entitlement at review time. Stored rather than joined
    -- because it is a claim about the past: a refund should not retroactively
    -- unverify a review that was honest when written.
    verified_purchase BOOLEAN NOT NULL DEFAULT FALSE,
    hidden     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mk_reviews_rating_range CHECK (rating BETWEEN 1 AND 5),
    CONSTRAINT mk_reviews_body_len CHECK (length(body) <= 4000)
);

CREATE UNIQUE INDEX mk_reviews_one_per_author ON mk_reviews (listing_id, email);
CREATE INDEX mk_reviews_by_listing
    ON mk_reviews (listing_id, created_at DESC) WHERE NOT hidden;

CREATE TABLE mk_favorites (
    listing_id UUID NOT NULL REFERENCES mk_listings (id) ON DELETE CASCADE,
    email      TEXT NOT NULL REFERENCES accounts (email) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (listing_id, email)
);

CREATE INDEX mk_favorites_by_email ON mk_favorites (email, created_at DESC);

CREATE TABLE mk_collections (
    id         UUID PRIMARY KEY,
    email      TEXT NOT NULL REFERENCES accounts (email) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    slug       TEXT NOT NULL,
    blurb      TEXT NOT NULL DEFAULT '',
    is_public  BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT mk_collections_name_len CHECK (length(btrim(name)) BETWEEN 1 AND 80),
    CONSTRAINT mk_collections_slug_shape CHECK (slug ~ '^[a-z0-9][a-z0-9-]{0,60}$')
);

CREATE UNIQUE INDEX mk_collections_slug_unique ON mk_collections (email, lower(slug));

CREATE TABLE mk_collection_items (
    collection_id UUID NOT NULL REFERENCES mk_collections (id) ON DELETE CASCADE,
    listing_id    UUID NOT NULL REFERENCES mk_listings (id) ON DELETE CASCADE,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    added_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (collection_id, listing_id)
);

-- Download counting, deduplicated per account per day. A per-event log would be
-- the largest table in the schema and answer no question the product asks; this
-- shape gives a truthful unique-download count and a cheap abuse signal, and
-- retention can drop old days without touching listing counters.
CREATE TABLE mk_downloads (
    listing_id UUID NOT NULL REFERENCES mk_listings (id) ON DELETE CASCADE,
    email      TEXT NOT NULL REFERENCES accounts (email) ON DELETE CASCADE,
    day        DATE NOT NULL,
    count      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (listing_id, email, day),
    CONSTRAINT mk_downloads_count_positive CHECK (count > 0)
);

CREATE INDEX mk_downloads_by_day ON mk_downloads (day DESC);


-- ---------------------------------------------------------------------------
-- Moderation
-- ---------------------------------------------------------------------------

-- Abuse, licensing and provenance complaints. 'ai_misattribution' is a
-- first-class reason rather than 'other' because the human-made shelf's promise
-- is only as good as the channel for challenging it.
CREATE TABLE mk_reports (
    id             UUID PRIMARY KEY,
    listing_id     UUID NOT NULL REFERENCES mk_listings (id) ON DELETE CASCADE,
    reporter_email TEXT NOT NULL REFERENCES accounts (email) ON DELETE CASCADE,
    reason         TEXT NOT NULL,
    detail         TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'open',
    resolution     TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at    TIMESTAMPTZ,
    resolved_by    TEXT NOT NULL DEFAULT '',
    CONSTRAINT mk_reports_reason CHECK (
        reason IN ('license', 'ai_misattribution', 'stolen', 'malware', 'nsfw', 'spam', 'other')
    ),
    CONSTRAINT mk_reports_status CHECK (status IN ('open', 'actioned', 'dismissed')),
    CONSTRAINT mk_reports_detail_len CHECK (length(detail) <= 8000),
    CONSTRAINT mk_reports_resolved_together CHECK (
        (resolved_at IS NULL) = (resolved_by = '')
    )
);

-- One open report per person per listing; re-reporting is not extra signal.
CREATE UNIQUE INDEX mk_reports_one_open_per_reporter
    ON mk_reports (listing_id, reporter_email) WHERE status = 'open';
CREATE INDEX mk_reports_queue ON mk_reports (created_at) WHERE status = 'open';


-- ---------------------------------------------------------------------------
-- Third-party imports
-- ---------------------------------------------------------------------------

CREATE TABLE mk_imports (
    id           UUID PRIMARY KEY,
    source_id    TEXT NOT NULL REFERENCES mk_sources (id) ON DELETE RESTRICT,
    -- The source's own stable id when it exposes one, '' when only a URL exists.
    source_ref   TEXT NOT NULL DEFAULT '',
    source_url   TEXT NOT NULL,
    requested_by TEXT NOT NULL REFERENCES accounts (email) ON DELETE CASCADE,
    listing_id   UUID REFERENCES mk_listings (id) ON DELETE SET NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    -- Whatever the adapter actually read, kept verbatim. When an importer
    -- mis-maps a field, this is the difference between re-deriving it and
    -- re-fetching a page that has since changed.
    payload      JSONB NOT NULL DEFAULT '{}'::jsonb,
    error        TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    CONSTRAINT mk_imports_status CHECK (
        status IN ('pending', 'fetched', 'linked', 'published', 'failed')
    )
);

-- The same upstream model is imported once. Partial, because source_ref is ''
-- for the metadata-only sources that expose no id.
CREATE UNIQUE INDEX mk_imports_source_ref_unique
    ON mk_imports (source_id, source_ref) WHERE source_ref <> '';
CREATE INDEX mk_imports_by_requester ON mk_imports (requested_by, created_at DESC);
CREATE INDEX mk_imports_queue ON mk_imports (created_at) WHERE status = 'pending';

-- Per-user OAuth links to import sources. Deliberately the same shape as
-- provider_accounts (0013): tokens are an AES-GCM envelope whose AAD binds
-- email+source, so an envelope lifted from this table cannot be replayed
-- against provider_accounts or vice versa.
CREATE TABLE mk_source_accounts (
    email            TEXT NOT NULL REFERENCES accounts (email) ON DELETE CASCADE,
    source_id        TEXT NOT NULL REFERENCES mk_sources (id) ON DELETE CASCADE,
    account_label    TEXT NOT NULL DEFAULT '',
    encrypted_tokens BYTEA NOT NULL,
    expires_at       TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at     TIMESTAMPTZ,
    PRIMARY KEY (email, source_id)
);


-- ---------------------------------------------------------------------------
-- Credit ledger: marketplace movement kinds
-- ---------------------------------------------------------------------------

-- 0005_credits.sql constrained kind to the AI-usage vocabulary. The marketplace
-- adds three movements that must NOT be conflated with AI spend, because
-- metrics.py and credits.spend_history() report 'spend' as model consumption:
--   market_buy     a buyer paying for a listing            (negative delta)
--   market_earn    a seller receiving the net of a sale     (positive delta)
--   market_payout  earned credits converted to a cash-out   (negative delta)
ALTER TABLE credit_ledger DROP CONSTRAINT IF EXISTS credit_ledger_kind_check;
ALTER TABLE credit_ledger
    ADD CONSTRAINT credit_ledger_kind_check CHECK (
        kind IN (
            'purchase', 'spend', 'grant', 'adjust', 'refund',
            'market_buy', 'market_earn', 'market_payout'
        )
    );

-- Marketplace rows carry the order id in ``note`` as JSON; this index makes
-- "everything that moved for this order" a lookup rather than a scan, and backs
-- the reconciliation test that asserts every paid credit order has exactly one
-- matching debit and one matching credit.
CREATE INDEX credit_ledger_marketplace
    ON credit_ledger (email, ts DESC)
    WHERE kind IN ('market_buy', 'market_earn', 'market_payout');


-- ---------------------------------------------------------------------------
-- Seed data
-- ---------------------------------------------------------------------------

-- Licenses. The open-source set is the one the community actually uses for
-- physical designs: permissive code licenses (models here are often generated by
-- scripts), Creative Commons, and the hardware-specific CERN OHL family.
INSERT INTO mk_licenses
    (id, name, url, is_open_source, allows_commercial, allows_derivatives,
     requires_attribution, share_alike, sellable, sort_order)
VALUES
    ('CC0-1.0', 'CC0 1.0 (public domain)', 'https://creativecommons.org/publicdomain/zero/1.0/',
     TRUE, TRUE, TRUE, FALSE, FALSE, FALSE, 10),
    ('CC-BY-4.0', 'CC BY 4.0', 'https://creativecommons.org/licenses/by/4.0/',
     TRUE, TRUE, TRUE, TRUE, FALSE, FALSE, 20),
    ('CC-BY-SA-4.0', 'CC BY-SA 4.0', 'https://creativecommons.org/licenses/by-sa/4.0/',
     TRUE, TRUE, TRUE, TRUE, TRUE, FALSE, 30),
    ('MIT', 'MIT', 'https://spdx.org/licenses/MIT.html',
     TRUE, TRUE, TRUE, TRUE, FALSE, FALSE, 40),
    ('Apache-2.0', 'Apache 2.0', 'https://spdx.org/licenses/Apache-2.0.html',
     TRUE, TRUE, TRUE, TRUE, FALSE, FALSE, 50),
    ('GPL-3.0-or-later', 'GPL 3.0 or later', 'https://spdx.org/licenses/GPL-3.0-or-later.html',
     TRUE, TRUE, TRUE, TRUE, TRUE, FALSE, 60),
    ('CERN-OHL-P-2.0', 'CERN OHL v2 Permissive', 'https://spdx.org/licenses/CERN-OHL-P-2.0.html',
     TRUE, TRUE, TRUE, TRUE, FALSE, FALSE, 70),
    ('CERN-OHL-W-2.0', 'CERN OHL v2 Weakly Reciprocal', 'https://spdx.org/licenses/CERN-OHL-W-2.0.html',
     TRUE, TRUE, TRUE, TRUE, TRUE, FALSE, 80),
    ('CERN-OHL-S-2.0', 'CERN OHL v2 Strongly Reciprocal', 'https://spdx.org/licenses/CERN-OHL-S-2.0.html',
     TRUE, TRUE, TRUE, TRUE, TRUE, FALSE, 90),
    -- Free downloads that are NOT open source. Kept distinct so the open-source
    -- shelf means what it says.
    ('CC-BY-NC-4.0', 'CC BY-NC 4.0 (non-commercial)', 'https://creativecommons.org/licenses/by-nc/4.0/',
     FALSE, FALSE, TRUE, TRUE, FALSE, FALSE, 200),
    ('CC-BY-ND-4.0', 'CC BY-ND 4.0 (no derivatives)', 'https://creativecommons.org/licenses/by-nd/4.0/',
     FALSE, TRUE, FALSE, TRUE, FALSE, FALSE, 210),
    -- The paid tiers. 'standard' is the default commercial license for a sale;
    -- 'extended' permits redistribution inside a product.
    ('chud-standard-1.0', 'Chudware Standard Commercial', 'https://chudware.com/legal/marketplace-standard',
     FALSE, TRUE, TRUE, FALSE, FALSE, TRUE, 300),
    ('chud-extended-1.0', 'Chudware Extended Commercial', 'https://chudware.com/legal/marketplace-extended',
     FALSE, TRUE, TRUE, FALSE, FALSE, TRUE, 310),
    ('chud-personal-1.0', 'Chudware Personal Use Only', 'https://chudware.com/legal/marketplace-personal',
     FALSE, FALSE, TRUE, FALSE, FALSE, TRUE, 320);

INSERT INTO mk_categories (id, parent_id, name, icon, blurb, sort_order) VALUES
    ('mechanical',  NULL, 'Mechanical',        '⚙',  'Machine elements, transmissions, mechanisms.', 10),
    ('fasteners',  'mechanical', 'Fasteners & hardware', '', 'Screws, nuts, inserts, standards parts.', 11),
    ('bearings',   'mechanical', 'Bearings & bushings',   '', 'Rolling and plain bearings.',            12),
    ('gears',      'mechanical', 'Gears & drives',        '', 'Spur, helical, worm, belts, chains.',    13),
    ('brackets',   'mechanical', 'Brackets & mounts',     '', 'Structural mounts and adapters.',        14),
    ('enclosures',  NULL, 'Enclosures',        '▣',  'Cases, housings, panels, cabinets.',           20),
    ('printing',    NULL, '3D printing',       '◈',  'Print-ready parts, jigs, calibration.',        30),
    ('printer_parts', 'printing', 'Printer upgrades',   '', 'Mods and replacement parts.',            31),
    ('functional',  'printing', 'Functional prints',    '', 'Parts that do a job, not decoration.',   32),
    ('electronics',  NULL, 'Electronics',      '⌁',  'PCB footprints, connectors, ECAD parts.',      40),
    ('robotics',     NULL, 'Robotics',         '⬡',  'Actuators, joints, grippers, chassis.',        50),
    ('automotive',   NULL, 'Automotive',       '⬢',  'Vehicle parts, tooling, fixtures.',            60),
    ('aerospace',    NULL, 'Aerospace',        '◭',  'Airframe, UAV, propulsion.',                   70),
    ('architecture', NULL, 'Architecture',     '⌂',  'Building elements, fixtures, furniture.',      80),
    ('tooling',      NULL, 'Tooling & fixtures', '⚒', 'Workholding, jigs, gauges, molds.',           90),
    ('assemblies',   NULL, 'Full assemblies',  '◫',  'Multi-part machines and kits.',               100),
    ('artistic',     NULL, 'Art & decorative', '✦',  'Sculptural, ornamental, display.',            110),
    ('other',        NULL, 'Other',            '·',  'Everything that fits nowhere above.',         900);

-- Third-party sources. ``kind`` is set by what each site's terms and public
-- interfaces actually permit, NOT by what would be convenient:
--   * GrabCAD gates every download behind an authenticated session and its
--     terms forbid automated retrieval, so it is 'metadata': we may read a
--     public model page's OpenGraph/JSON-LD to prefill a listing, and the
--     uploader supplies the file they are entitled to. Flipping this to 'api'
--     would make us a scraper.
--   * Thingiverse, Sketchfab, MyMiniFactory, GitHub and Onshape publish real
--     APIs; the first four allow direct download of openly licensed content,
--     and Onshape is per-user OAuth over the user's own documents.
INSERT INTO mk_sources (id, name, url, kind, supports_file_fetch, enabled, notes, sort_order) VALUES
    ('grabcad', 'GrabCAD', 'https://grabcad.com', 'metadata', FALSE, TRUE,
     'Public model pages are read for title/description/images/author. Downloads are session-gated and automated retrieval is disallowed, so the uploader attaches the file.', 10),
    ('thingiverse', 'Thingiverse', 'https://www.thingiverse.com', 'api', TRUE, TRUE,
     'Documented REST API. Requires CHUDWARE_MK_THINGIVERSE_TOKEN; without it the adapter degrades to metadata.', 20),
    ('printables', 'Printables', 'https://www.printables.com', 'metadata', FALSE, TRUE,
     'No documented public download API. Page metadata only.', 30),
    ('github', 'GitHub', 'https://github.com', 'api', TRUE, TRUE,
     'Public repositories and releases. Licenses come from the repo license API, so open-source provenance is machine-checkable.', 40),
    ('sketchfab', 'Sketchfab', 'https://sketchfab.com', 'api', TRUE, TRUE,
     'Documented REST API with a downloadable-under-CC filter. Requires CHUDWARE_MK_SKETCHFAB_TOKEN.', 50),
    ('onshape', 'Onshape', 'https://www.onshape.com', 'oauth', TRUE, TRUE,
     'Per-user OAuth over the user own documents; export via the translation API.', 60),
    ('myminifactory', 'MyMiniFactory', 'https://www.myminifactory.com', 'api', TRUE, TRUE,
     'Documented REST API. Requires CHUDWARE_MK_MYMINIFACTORY_TOKEN.', 70),
    ('cults3d', 'Cults3D', 'https://cults3d.com', 'metadata', FALSE, TRUE,
     'GraphQL API covers a creator own catalogue only. Page metadata for everything else.', 80),
    ('thangs', 'Thangs', 'https://thangs.com', 'metadata', FALSE, TRUE,
     'Page metadata only.', 90),
    ('traceparts', 'TraceParts', 'https://www.traceparts.com', 'link', FALSE, TRUE,
     'Supplier catalogue; redistribution not granted. Reference link only.', 100),
    ('nih3d', 'NIH 3D', 'https://3d.nih.gov', 'api', TRUE, TRUE,
     'Public-domain and CC bioscientific models with an open API.', 110);
