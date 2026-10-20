## Yield — findings

All findings below are **confirmed by current source inspection, not executed reproductions**. Deployment-dependent prerequisites are stated explicitly. No product files were changed; no tests, scanners, payloads, network calls, or git commands were run.

### 1. High — Concurrent marketplace upload and mutation can deadlock a gateway event loop
**CWE-833: Deadlock**

- **Locations:** `Webservice/gateway/marketplace.py:2344-2366`, `marketplace.py:1700-1706`, `marketplace.py:2147-2149`; synchronous transaction implementation at `Webservice/gateway/db.py:147-190`.
- **Attacker prerequisite:** Authenticated seller with a listing; marketplace enabled.
- **Source → sink:** An upload acquires the listing’s PostgreSQL `FOR UPDATE` lock, then awaits `file.read()` while retaining the transaction. A concurrent PATCH/upload/deletion for that listing executes a synchronous database operation on the same event-loop thread and waits for that lock. The first coroutine cannot resume to finish its upload and release the lock because the second coroutine blocks the event loop.
- **Impact:** Cross-customer availability loss in the affected gateway worker. Repeated overlapping requests can affect additional workers.
- **Nearby controls:** Seller ownership, write/upload rate limits, size limits, and seller quota checks exist, but two authorized requests suffice. `db.py` configures connection-pool/connect timeouts, not statement or row-lock timeouts. Actual server-side timeout configuration was not inspected; it may bound the outage rather than prevent event-loop blocking.
- **Minimal remediation:** Read/spool uploads outside database transactions, then use a short transaction to reauthorize and commit metadata/quota changes. Do not run blocking database waits on the event loop.
- **Safe isolated reproduction:** In a subprocess using only temporary asset storage and a disposable PostgreSQL fixture, invoke the upload coroutine with a fixture `UploadFile.read()` that signals a barrier and yields after the listing lock is acquired. Schedule a second listing mutation on the same loop. Set database `lock_timeout='250ms'` on the disposable connections and an external subprocess deadline. Observe that an unrelated heartbeat coroutine cannot run until the database timeout breaks the wait. No large payload or external service is needed.

### 2. High — Subscription checkout completion overrides an operator’s account suspension
**CWE-863: Incorrect Authorization**

- **Locations:** `Webservice/gateway/billing.py:436-454`; suspension semantics at `Webservice/gateway/accounts.py:203-228`; account authorization at `Webservice/gateway/auth.py:438-459`.
- **Attacker prerequisite:** An account holder retains an unfinished Stripe subscription Checkout URL obtained before suspension/denial, or its legitimate completion event has not yet been processed.
- **Source → sink:** The user completes the existing hosted checkout after suspension. The correctly signed `checkout.session.completed` event calls `ensure_account()`, then unconditionally calls `set_status(email, "active", decided_by="stripe")`. No check preserves an existing `suspended` or `denied` status.
- **Impact:** A suspended customer regains gateway access and existing organization privileges. A suspended account whose stored role remains `admin` also regains that role; this is not promotion of an ordinary customer to admin.
- **Nearby controls:** The webhook signature and event-ID deduplication prevent forgery and replay, but not a first legitimate event crossing an operator revocation boundary. `ensure_account()` itself deliberately preserves denied/suspended accounts; the subsequent billing call defeats that control.
- **Minimal remediation:** Keep commercial subscription state separate from administrative approval. Checkout must not reactivate operator-suspended or denied accounts.
- **Safe isolated reproduction:** Use `test_billing.py`’s fake Stripe transport and temporary database. Seed an account, arrange a subscription Checkout completion event with server-created metadata, suspend the account before delivering the event to the handler, and inspect the resulting status. Current code changes it to `active`. Repeat with an ordinary role and, separately, a retained admin role.

### 3. High, conditional payment-method exposure — Non-marketplace billing grants value before payment settles
**CWE-841: Improper Enforcement of Behavioral Workflow**

- **Locations:** `Webservice/gateway/billing.py:287-310`, `billing.py:421-448`; credit mutation at `Webservice/gateway/credits.py:553-588`.
- **Attacker prerequisite:** A customer can use a delayed-settlement payment method for the relevant Stripe checkout. Credit purchases additionally require the credit-checkout feature to have been enabled when the session was created. Actual enabled Stripe payment methods were unavailable.
- **Source → sink:** A genuine `checkout.session.completed` with `payment_status="unpaid"` immediately grants credit-pack credits or sets a subscription plan/status to active. Unlike marketplace settlement, these paths never inspect `payment_status`.
- **Impact:** An unsettled/ultimately failed credit purchase produces spendable credits, including marketplace purchasing power. Subscription completion also activates the paid account plan before settlement. **Do not overstate desktop impact:** desktop token issuance separately requires a valid `paid_through` window.
- **Nearby controls:** Price IDs and pack credits are server-authoritative, signatures are checked, and identical event IDs apply once. None proves payment settlement. The non-marketplace handler has no corresponding asynchronous-failure reversal. Marketplace settlement correctly checks `"paid"` at `marketplace_commerce.py:1683`.
- **Minimal remediation:** Gate grants on confirmed payment settlement, support delayed-payment success/failure explicitly, and use checkout/payment identity to make settlement idempotent across event types.
- **Safe isolated reproduction:** Extend the existing temporary billing fixture’s credit event with `"payment_status": "unpaid"` and a recognized pack price whose fake provider metadata contains `chud_credits`. Process it once and inspect the positive ledger/balance change. Deliver a separate asynchronous-failure event and verify that current code leaves the grant intact. Repeat the unpaid event for subscription mode and inspect the account plan/status.
- **Classification:** Missing settlement enforcement is confirmed in source. Whether customers can exercise it in the deployed Stripe configuration is unavailable deployment evidence, not a confirmed operational exposure.

### 4. Medium — Fork references disclose another seller’s private listing metadata
**CWE-639: Authorization Bypass Through User-Controlled Key**

- **Locations:** `Webservice/gateway/marketplace.py:211-215,245-250`, `marketplace.py:575-580`, `marketplace.py:672-676`; containing-listing authorization at `marketplace.py:1146-1157`.
- **Attacker prerequisite:** Seller account and knowledge of the victim listing UUID—for example, a listing previously public and subsequently unlisted.
- **Source → sink:** A seller submits their own listing with `forked_from=<victim UUID>`. Validation checks only that the victim row exists. Reading the attacker’s authorized listing subsequently selects and returns the victim’s `id`, `slug`, and `title` without checking visibility or entitlement on that upstream object.
- **Impact:** Disclosure of private draft/unlisted/removed listing metadata. Publishing the child can expose the upstream metadata to anonymous visitors. This is **not** an asset-byte disclosure.
- **Nearby controls:** `_visible()` correctly protects direct access to private listings, and `_owned_listing()` protects seller mutations. Neither applies to the joined upstream object.
- **Minimal remediation:** Authorize referenced listings when setting `forked_from`, and independently filter the upstream response at read time because visibility can change later.
- **Safe isolated reproduction:** Create sellers A and B in temporary fixtures. B creates an unpublished listing titled `PRIVATE-CANARY`. Confirm A’s direct GET returns 404. A creates a valid free listing referencing B’s UUID, then GETs A’s own listing through `/api/marketplace/listings/{id}`. Current response includes `forked_from.title == "PRIVATE-CANARY"`.

### 5. Medium — A recoverable card failure permanently prevents later successful marketplace settlement
**CWE-841: Improper Enforcement of Behavioral Workflow**

- **Locations:** `Webservice/gateway/marketplace_commerce.py:1796-1817`, `marketplace_commerce.py:1681-1682`, `marketplace_commerce.py:1951-1962`; refund status guard at `marketplace_commerce.py:1518-1521`.
- **Trigger/prerequisite:** A buyer’s payment attempt fails and they subsequently succeed in the same hosted checkout. The buyer controls the attempted payment instruments; no webhook forgery is needed.
- **Source → sink:** `payment_intent.payment_failed` marks the order `failed`. A later correctly signed paid Checkout completion reaches `_settle_checkout()`, which immediately returns unless the order is still `pending`.
- **Impact:** Money can be collected while the buyer receives no entitlement and the order remains failed. Normal order refunds also reject the order as not paid. This is a payment-integrity/availability defect, not demonstrated cross-account theft.
- **Nearby controls:** Correct amount/currency checks exist but are never reached once the order is `failed`. Event-ID deduplication does not repair the state transition. Creating a new local checkout does not recover the already-successful old session.
- **Minimal remediation:** Treat an individual PaymentIntent attempt failure as nonterminal for its Checkout session, or reconcile a verified successful session to a paid state while enforcing single-purchase/payment binding.
- **Safe isolated reproduction:** Using `test_marketplace_commerce.py`’s `Market` and fake Stripe fixtures, create a pending cash order. Feed a `payment_intent.payment_failed` event carrying its server-generated order metadata, followed by a distinct `checkout_event(...)` with `payment_status="paid"` and the exact order amount. Inspect the order and entitlement: current code leaves `status="failed"` and grants none.

### 6. Medium — Organization member removal can race ownership transfer and delete the protected owner
**CWE-367: Time-of-check Time-of-use Race Condition**

- **Locations:** `Webservice/gateway/orgs.py:1090-1119`, `orgs.py:893-909`; schema at `Webservice/gateway/migrations/0015_organizations.sql:34-48`.
- **Attacker prerequisite:** Existing organization administrator, plus concurrent request execution overlapping a legitimate ownership transfer. A strictly single-worker execution model that serializes these synchronous route bodies limits this particular schedule; deployed worker topology was not inspected.
- **Source → sink:** Removal reads the target’s role without a row lock, checks that it is not owner, then later deletes by `(org_id, email)` without a role predicate. In between, the incumbent owner can promote that target. The administrator’s already-authorized removal then deletes the new owner.
- **Impact:** An administrator bypasses owner-removal protection, leaving the organization ownerless. Owner-only repair operations become unavailable, and shared writes fail the owner-account funding check.
- **Nearby controls:** One transaction per operation is not sufficient at the default isolation level. The partial unique index enforces **at most one** owner, not **at least one**. Transfer also does not lock its target membership or verify that promotion updated one row.
- **Minimal remediation:** Serialize authority-changing operations on a common organization lock acquired before authorization snapshots; lock/revalidate target memberships and verify update cardinality.
- **Safe isolated reproduction:** In a disposable DB, create owner O, administrator A, and Studio-plan member T. Hold T’s General-team membership row locked from a fixture connection. In worker thread 1, invoke A’s removal of T; it passes the member-role check and waits deleting the team membership. In worker thread 2, transfer ownership O→T and commit. Release the fixture lock. Removal then deletes T from `org_members`. Assert owner count is zero. Use short PostgreSQL lock/statement timeouts and bounded thread joins.

## Yield — positive controls and rejected candidates

- Webhook raw-body signature verification precedes mutation; event-ID insertion and nested account/credit mutations share the transaction helper on the consistent module-import path.
- Marketplace cash settlement checks paid status, exact recorded amount/currency, and a payment-intent identifier.
- Credit purchases lock buyer/seller account rows in sorted order. Database uniqueness prevents two pending/paid purchases of the same listing by the same buyer; the earlier entitlement precheck is **not**, by itself, a double-purchase finding.
- Credit reservations deduct estimated cost before dispatch; reconciliation and grants use transactional ledger/balance changes. Credit/admin controls require admin authority.
- Organization invitations are email-bound, expiring, single-use, and token-hashed. Invitation deletion scopes both invitation and organization IDs. Org admins cannot ordinarily invite/promote administrators; site-wide admin does not imply tenant membership.
- Paid asset downloads require visibility plus entitlement. Evidence is excluded from customer asset routes and served only through an admin, attestation-and-listing-scoped join.
- Upload filenames/paths reject traversal and symlinks. Response MIME types derive from an allowlist, non-image files attach, and `nosniff` is set.
- Collections, orders, seller data, portal invoices, and device revocation are account-scoped.
- Refunds and payout execution are admin-only; amounts are bounded, order rows locked, full refunds revoke the associated entitlement, and Stripe operations carry idempotency keys.
- Onshape linking binds one-use state to the current account and uses PKCE. Stored source tokens use authenticated encryption with account/source AAD.
- Source fetches validate HTTPS/domain/public-address constraints, pin the resolved address with hostname TLS verification, revalidate redirects, and impose size/deadline limits.
- Inspected dynamic SQL uses fixed allowlisted fragments and bound values. **No substantiated SQL-injection finding.**
- Cash-out of a general credit balance, absorption of seller refund shortfalls, and customer-payment-method configuration need business/deployment policy context; they are not presented as proven fraud vulnerabilities.

## Yield — reviewed paths and limits

1. **Primary scope:** `billing.py`, `credits.py`, `orgs.py`, `marketplace.py`, `marketplace_commerce.py` — payment/grant lifecycle, replay handling, transaction boundaries, invitations/roles, listing references, asset CRUD/downloads, purchases, tips, Connect, payouts, refunds.
2. **Authorization/storage/schema:** `marketplace_authz.py`, `marketplace_schema.py`, `db.py`; relevant sections of migrations `0015_organizations.sql`, `0016_marketplace.sql`, `0017_marketplace_hardening.sql`.
3. **Dependency surfaces:** `marketplace_sources.py` — fetch/import ownership, acceptance, streaming, Onshape linkage/token storage; `portal_api.py`; relevant `entitlement.py` issuance/refresh/device-revocation controls; `accounts.py` status/subscription semantics; `auth.py` middleware/account gating.
4. **Admin:** `admin_marketplace.py` route guards, evidence, payout decisions, import visibility; `admin_billing.py` route-guard inventory and refund path. Numerical dashboard/report queries were not exhaustively audited.
5. **Existing checks/fixtures inspected or indexed:** `tests/conftest.py`, `test_billing.py`, `test_marketplace_commerce.py`, `test_marketplace.py`, `test_marketplace_admin.py`, `test_orgs.py`, `test_credits.py`, `test_portal.py`; dependency declarations in `requirements.txt`.

**Limits:** No live configuration, Stripe dashboard/event-delivery configuration, credentials, cloud/device access, or deployed worker/DB timeout settings inspected. Individual source-provider metadata parsers, rendering/native ingestion, and the separate general provider-account subsystem were not exhaustively reviewed here. No Python LSP was available. Concurrent worktree changes mean evidence describes the source snapshots read, not a frozen commit.

## Suggested integrator checks — not run

Use only disposable PostgreSQL/assets, generated test keys, fake Stripe transports, and blocked outbound networking. Ensure `CHUDWARE_TEST_DB_URL` does not select a real/customer database.

- `tests/test_billing.py::TestWebhook`
- `tests/test_marketplace_commerce.py`
- `tests/test_orgs.py`, especially owner protection, transfer, invitation binding, and cross-org ID checks
- `tests/test_marketplace.py::test_paid_listing_asset_is_refused_without_an_entitlement`
- `tests/test_marketplace_admin.py::test_evidence_is_reachable_only_through_the_admin_attestation_route`
- `tests/test_marketplace_admin.py::test_evidence_route_will_not_serve_another_listings_asset`
- `tests/test_marketplace_admin.py::test_refund_route_is_operator_only`
- `tests/test_portal.py::test_invoice_history_is_scoped_through_billing_owner`
- `tests/test_portal.py::test_portal_device_list_and_revoke_are_account_scoped`
- `tests/test_credits.py::test_credit_pack_event_replayed_three_times_applies_once`
- Add the six bounded regression recipes above; existing sequential tests do not establish the missing concurrency or payment-transition properties.

**Coverage summary:** Six surviving source-level findings: gateway availability, administrative suspension bypass, unsettled-payment grants, private listing metadata IDOR, failed-to-paid settlement loss, and an organization ownership race. No verification was executed.
