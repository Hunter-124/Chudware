## yield: findings

**Audit status:** Read-only review completed for the five assigned modules and the dependencies listed below. No edits, tests, payload execution, network calls, scanners, or git commands were performed. Findings are confirmed by current source unless explicitly qualified. Reproduction recipes are proposals for the integrator, not exercised results.

### ID-01 — High — OAuth onboarding activates an attacker’s pre-registered password
**CWE-287 / CWE-863 — Improper authentication and authorization**

**Evidence**
- `Webservice/gateway/authn.py:706-717`: anonymous registration installs an unverified password for a supplied email.
- `Webservice/gateway/oauth.py:503-520`: a previously unseen provider subject links to the existing account by email.
- `Webservice/gateway/oauth.py:595-599`: the callback calls `complete_verification` on that account.
- `Webservice/gateway/authn.py:673-679,240-248`: this marks its existing password credential verified, without proving who chose that password.
- `Webservice/gateway/authn.py:744-758`: that password subsequently opens a session.

**Execution path / impact:** An attacker registers a prospective customer’s email with an attacker-chosen password, without access to the inbox. When the legitimate customer first signs in through Google, GitHub, or Microsoft, the genuine OAuth proof silently activates the attacker’s password. The attacker can then log into the customer’s account and access its files and account capabilities.

**Related privileged variant, same root cause:** `authn.py:1013-1017` implements `grant-admin --oauth-only` by preserving any existing password, marking it verified, and granting admin. An attacker who pre-registers the future operator address therefore gains an admin password when the operator performs the intended OAuth-only bootstrap. This is not a claim that the CLI itself is remotely callable.

**Nearby controls:** Provider verification, state binding, pending status, and the initial unverified-password login refusal are present. They do not establish ownership of the pre-existing password.

**Minimal remediation:** Do not promote an unverified password merely because a separate authentication method proves the email. Remove/invalidate that unproven credential or require the now-authenticated owner to establish a new password. OAuth-only admin bootstrap must remove existing password credentials and invalidate their outstanding recovery/verification capabilities and sessions as appropriate.

**Safe isolated repro:** In the temporary PostgreSQL fixture, register `victim@example.test` with a fixture password but do not verify it. Confirm password login is initially refused. Simulate only the successful provider result using the existing `test_authn.py` OAuth callback fixture pattern, returning that email and a new fixture subject; use a genuine local state/cookie transaction. Confirm a separate client can now log in with the original attacker password. Repeat separately with `_cli(["grant-admin", "--email", ..., "--oauth-only"])` against the fixture database and observe the password authenticates an admin.

---

### ID-02 — Medium — Verification GET can force a victim into the attacker’s account
**CWE-352 — Login CSRF**

**Evidence**
- `Webservice/gateway/authn.py:769-779`: a GET containing a verification token creates a session and unconditionally sets the browser’s session cookie.
- `Webservice/gateway/authn.py:410-431`: redemption binds the token to email/purpose/expiry, not the initiating browser.
- `Webservice/gateway/authn.py:489-501`: the resulting cookie is `SameSite=Lax`.
- `Webservice/gateway/auth.py:320-331`: the authentication subtree remains publicly reachable even when an existing session accompanies the request.

**Execution path / impact:** An attacker registers an address they control and sends its unused verification URL to another person. Following that ordinary top-level link signs the victim into the attacker’s account, replacing any current account cookie. If the victim subsequently uploads confidential CAD files or otherwise works without noticing the account switch, the attacker can access that work.

**Nearby controls:** Single-use tokens prevent replay, not first-use login CSRF. HttpOnly/Secure/Lax do not prevent setting the session during a top-level navigation. OAuth has a browser-bound transaction cookie; this flow does not.

**Minimal remediation:** Separate email verification from automatic login, or require browser-bound confirmation of the intended identity before establishing/replacing a session.

**Safe isolated repro:** Register two fixture identities. Retain an unused verification token belonging to the attacker fixture. In an independent client authenticated as the victim fixture, GET that token’s verification URL with redirects disabled. Check that the new session resolves to the attacker fixture and `/api/whoami` reports that identity. No browser exploitation or file upload is needed to establish the defect.

---

### ID-03 — Medium — Anonymous security-event input is fully decompressed before its expansion limit or rate limit
**CWE-409 / CWE-400 — Data amplification and resource exhaustion**

**Evidence**
- `Webservice/gateway/security.py:45-55`: `gzip.decompress(body)` runs before checking the 512 KiB expanded limit.
- `Webservice/gateway/security.py:106-124`: complete request buffering, decompression, and schema parsing precede both rate limiters and device authentication.
- `Webservice/gateway/auth.py:117-124`: `/api/security/events` is public.

**Execution path / impact:** An unauthenticated client submits highly compressible input within the 64 KiB compressed limit. The complete expanded bytes are allocated before rejection. Repeated rejected bodies never reach the endpoint’s rate-limit counters. These operations run synchronously in the async handler, affecting the shared gateway’s availability.

The compressed-size cap limits the maximum amplification; this is **not** a claim of infinite expansion or a demonstrated process crash. Omitting Content-Length also reaches unrestricted `request.body()` buffering before the actual compressed-size check.

**Minimal remediation:** Count bytes while streaming the request; use bounded incremental decompression that stops at the expanded ceiling; apply the applicable ingress rate limit before expensive parsing.

**Safe isolated repro:** Use a single gzip body representing **513 KiB** of repeated bytes. Wrap the real decompressor with a test spy recording the returned length. Call `_decode` and assert it raises 413 only after the spy observes the complete 513 KiB result. An in-process endpoint test can additionally confirm the IP limiter is not consulted for this rejected body. Do not send large payloads or stress traffic.

---

### ID-04 — Medium — Unauthenticated reset/resend requests can invalidate every deliverable recovery link
**CWE-640 — Weak password-recovery mechanism**

**Evidence**
- `Webservice/gateway/authn.py:389-400`: minting a token invalidates all outstanding tokens of that purpose.
- `Webservice/gateway/authn.py:783-804,808-829`: resend/reset initiation mints the replacement before queuing its mail.
- `Webservice/gateway/mailer.py:83,140-148`: mail is limited to five messages per recipient per hour; an excess send silently returns without enqueueing.
- `Webservice/gateway/limits.py:110-111`: the authentication IP budget permits twenty attempts per fifteen minutes.

**Execution path / impact:** An anonymous caller who knows an email makes six password-reset requests. The first five mailed tokens are invalidated; the sixth, only-live token is never mailed. The owner cannot recover through any received link, and further legitimate requests during the exhausted mail window repeat the problem. The same failure affects pending-account verification. Only six requests are required; neither mailbox access nor a distributed attack is necessary.

**Nearby controls:** Generic 202 responses and recipient throttling prevent disclosure/mail flooding, but the token invalidation is committed before delivery eligibility is known.

**Minimal remediation:** Reserve the recipient’s issuance/mail budget before invalidating existing links, and atomically persist a replacement token with an accepted outbox entry. A throttled request must not destroy an already deliverable recovery capability.

**Safe isolated repro:** Seed a fixture credential directly, clear the fixture recipient’s mail budget, and POST `/api/auth/forgot` six times. Read only the fixture outbox; assert exactly five links were queued and each is already unusable. Confirm a live token exists whose hash does not correspond to any queued link. No drain or external mail transport is required.

---

### ID-05 — Medium — Account-specific customer telemetry endpoints overwrite one global policy row
**CWE-668 / CWE-863 — Cross-account configuration exposure**

**Evidence**
- `Webservice/gateway/accounts.py:539-575`: the account-scoped security-policy API upserts `security_policies` using the mode string as its global primary key; assigning `customer_endpoint` updates the shared endpoint.
- `Webservice/gateway/migrations/0011_security.sql:48-54,90-95`: the schema permits only the three mode IDs and account rows reference them.
- `Webservice/gateway/entitlement.py:93-105,125-126`: other enterprise accounts resolve that same endpoint and receive it in their signed security policy.
- `Webservice/gateway/entitlement.py:701-708,159-175`: subsequent receipt forwarding POSTs to the resolved endpoint.

**Execution path / impact:** After an operator assigns customer A’s endpoint, assigning customer B’s endpoint through B’s account URL silently changes A’s destination too. A’s signed device receipts can consequently be sent to B’s server, or A’s refresh flow can fail if B rejects them.

**Prerequisite qualification:** The setter is correctly restricted to a global administrator. There is no demonstrated low-privilege API bypass. This is a confirmed tenant-isolation defect during legitimate customer-specific provisioning; an attacker-controlled endpoint becomes relevant if the operator configures it for that attacker’s own customer account.

**Minimal remediation:** Store endpoint/configuration on the account assignment or use uniquely identified tenant-scoped policy instances. Keep telemetry mode separate from policy-instance identity.

**Safe isolated repro:** Using fixture admin state, create two enterprise accounts. Assign `https://a.example.test/receipt` to A, then `https://b.example.test/receipt` to B. GET A’s account-scoped policy and call `_policy_assignment(A)`; both currently resolve B’s endpoint. No forwarding or network call is necessary.

---

### ID-06 — Medium — Database-readable outbox retains usable plaintext reset and verification tokens
**CWE-312 — Cleartext storage of sensitive information**

**Evidence**
- `Webservice/gateway/authn.py:717-725,820-829`: complete bearer URLs are supplied as mail context.
- `Webservice/gateway/mailer.py:145-148`: context is stored verbatim as JSON in `mail_outbox`.
- `Webservice/gateway/mailer.py:226-240`: successful delivery retains the context; scrubbing waits until it is over 24 hours old.
- `Webservice/gateway/migrations/0002_mail.sql:14-24`: this is a normal database text column.
- Reset and verification TTLs are one hour and twenty-four hours respectively (`authn.py:67-69`).

**Execution path / impact:** Someone with read-only access to a database snapshot, backup, or the outbox can recover still-valid bearer links and redeem them to take over accounts. Thus hashing `email_tokens` does not provide the claimed protection against database disclosure. A read-only database compromise is a prerequisite, not a remote SQL-injection finding.

**Nearby controls:** Tokens are hashed in the token table; console bodies are suppressed and old contexts are scrubbed. Those controls do not protect live bearer material in the outbox.

**Minimal remediation:** Encrypt bearer-bearing queue context with a key outside the database and erase it promptly after successful delivery; expire/redact abandoned entries as well.

**Safe isolated repro:** Issue one reset for a fixture account without draining mail. Extract the token from that fixture’s `mail_outbox.ctx_json`, not `email_tokens`. Redeem it through `/api/auth/reset` and confirm it establishes a new fixture password/session. Never inspect a real outbox.

---

### ID-07 — Medium — Suspension revocation is lazy, allowing dormant sessions to survive reinstatement
**CWE-613 — Insufficient session expiration**

**Evidence**
- `Webservice/gateway/accounts.py:203-236`: suspension/denial changes account status and revokes chat access but does not revoke session rows.
- `Webservice/gateway/auth.py:508-526`: session revocation occurs only when a later HTTP authentication attempt discovers denied/suspended status.
- `Webservice/gateway/auth.py:645-654`: WebSocket revalidation rejects inactive status but does not itself revoke those rows.
- `Webservice/gateway/tests/test_authn.py:404-427`: the existing regression explicitly performs an intervening denied HTTP request before reinstatement.

**Execution path / impact:** A stolen or otherwise unwanted session remains usable after the administrator suspends and later reinstates an account if no session-authenticated HTTP request occurred during suspension. The attacker can simply leave the session dormant. Access is blocked while suspended; the defect is survival across reinstatement.

**Minimal remediation:** Revoke all relevant sessions transactionally when entering denied/suspended state, rather than waiting for an attempted use.

**Safe isolated repro:** Create an active fixture account and session. Call `set_status(..., "suspended")`, then `set_status(..., "active")` without making an HTTP request between them. Present the original session to the protected fixture route; it is still accepted. Compare with the existing test’s intervening-request case.

---

### ID-08 — Medium, configuration-dependent — Pinned PyJWT recursive-payload exception escapes Access authentication
**CWE-248 — Uncaught exception**  
**GHSA-42vr-xj54-vc7v / CVE-2026-101918**

**Evidence**
- `Webservice/gateway/requirements.txt:14`: PyJWT is pinned to `2.13.0`.
- `Webservice/gateway/auth.py:170-172,540-560`: caller-controlled Access header/cookie reaches `get_signing_key_from_jwt`; only `jwt.PyJWTError` is caught.
- `Webservice/gateway/.venv/lib/python3.14/site-packages/jwt/jwks_client.py:213-226`: unverified token decoding precedes key lookup.
- `Webservice/gateway/.venv/lib/python3.14/site-packages/jwt/api_jwt.py:287-300`: payload JSON parsing catches `ValueError`, not `RecursionError`.
- `Webservice/gateway/auth.py:320-326`: public-path soft authentication also catches only `AuthError`.
- Integrator-provided `scratch/security-audit-2026-10-12/gateway-installed-audit.json` identifies `2.15.0` as the first fixed release.

**Reachability / impact:** With non-dev Access authentication configured and no valid gateway session taking precedence, an unsigned deeply nested token can cause an uncaught request-level exception. This can affect public paths’ optional authentication too. **No authentication bypass, process crash, persistent exhaustion, or customer-data exposure is established.** Access-disabled deployments reject before this parser. Google/Microsoft token payloads come from provider exchanges, so they do not offer the same direct arbitrary-token source.

**Minimal remediation:** Update the dependency to a release containing the fix and preserve normalized invalid-token handling.

**Safe isolated repro:** Under the integrator’s isolated harness, construct one token with a small, bounded nesting depth just above the interpreter’s JSON recursion threshold. Use `JWKSCache` with a test-only `.invalid` URL and replace its network fetch method with an assertion that fails if called. Exercise `_authenticate` with fixture Access configuration and that cookie/header. Observe `RecursionError` rather than `AuthError`; the failure precedes any fetch.

Other advisory preconditions examined are absent here: the gateway uses RS256-only verification with fresh options dictionaries, not mixed HS/asymmetric allowlists or reused mutable verification options. Attacker control of trusted provider JWKS responses was not established.

---

### ID-09 — Low — Registration retains a deterministic account-existence timing differential
**CWE-208 — Observable timing discrepancy**

**Evidence**
- `Webservice/gateway/authn.py:706-716`: an existing identity returns without password hashing; a new identity calls `set_password`.
- `Webservice/gateway/authn.py:83,168-169,211`: the new-account branch performs Argon2id with 64 MiB and three passes.
- `Webservice/gateway/tests/test_authn.py:275-287`: existing coverage compares status/body, not work performed.

**Execution path / impact:** Anonymous callers can distinguish existing addresses from unused addresses through the expensive-work difference, despite identical 202 bodies. This exposes account membership. The source establishes the differential; its statistical distinguishability on a deployed network was **not measured**.

**Nearby controls:** The IP throttle limits sampling. Password login correctly performs dummy verification for unknown accounts, but registration does not equalize its branches.

**Minimal remediation:** Equalize expensive work across registration outcomes under the existing throttle, or defer processing so response timing is independent of account existence.

**Safe isolated repro:** Use a spy around the real `hash_password`, submit one fixture existing address and one new address, and assert only the new-address request calls it. This demonstrates the work differential without timing loops, external email, or production sampling.

## yield: positive_controls

- Argon2id password storage; minimum/maximum password policy for registration/reset; password login performs dummy verification for unknown credentials.
- Password-login IP and email throttles execute before Argon2 verification.
- Random 256-bit session bearers, hashed session storage, absolute expiry, revocation-aware lookup, HttpOnly/Secure/Lax cookies.
- Email tokens are purpose-bound, expiring, and atomically single-use; password reset revokes existing sessions.
- Account role/heavy/status come from the store, not caller claims. Unknown identities become pending users. Trial expiry is enforced during resolution.
- Administrative account mutations require an authenticated database-derived admin role. Role changes are not exposed as HTTP account mutations.
- Access verification fixes algorithm, issuer, audience, and required expiry; valid gateway sessions take precedence; absent Access configuration fails closed.
- OAuth state is random, HMAC-bound to provider/browser/destination, stored hashed, single-use, and cleared from cookies after callback.
- OAuth redirects use configured origins and reject nonlocal return destinations. Provider-error HTML is escaped.
- Google requires a verified email; GitHub requires a verified primary email; Microsoft performs issuer/tenant checks and the `xms_edov` guard for nonconsumer tenants.
- Existing OAuth links follow stable provider subjects rather than rebinding on email changes.
- Security-event ingestion checks device signatures, sequence/order, policy, canonical UUIDs, and event schema before persisting events or applying sanctions.
- Deployment source disables uvicorn access logs and refuses dev-mode startup. Console mail suppresses bearer-bearing bodies. These are source controls, not verified production settings.

## yield: policy_gaps_and_limits

- **MFA:** No local MFA, admin step-up, or required OAuth/Access authentication-strength claim is enforced by these modules. This is a policy gap, not a demonstrated second-factor bypass. Provider/edge MFA policy was unavailable.
- **PKCE/OIDC nonce:** The browser verifier binds OAuth state but is not an OAuth PKCE verifier. Authorization requests/exchanges omit PKCE, and OIDC nonce is absent (`oauth.py:277-286,539-558`). Existing state binding blocks ordinary callback CSRF; no independent code-interception exploit was established.
- **Origin/CSRF:** No general origin/CSRF middleware was found in the reviewed application wiring. ID-02 is the concrete exploitable navigation case; no blanket cross-site POST bypass is claimed.
- **Registration races:** Separate lookup/upsert operations could matter with multiple concurrent workers, but the documented single-worker synchronous handler path does not establish a current remote race. Not reported as a finding.
- **Security-event rate-limit peer identity:** Production unit source enables trusted loopback proxy-header processing. Therefore a claim that every tunneled request necessarily shares one loopback bucket is not established.
- **Signed “active” event acknowledgements for revoked devices:** The event endpoint can produce them, but entitlement refresh separately rejects revoked devices. A desktop enforcement bypass was not established within this assignment.
- Live edge settings, Access policy, proxy header transformations, production secrets, deployed dependency versions, provider responses, database permissions, and browser behavior were not probed.
- No Python language server was available, per the integrator. Current source, not historical rollout documentation or prior reviews, was used as authority.

## yield: reviewed_paths

1. **Complete assigned implementations:**  
   `Webservice/gateway/auth.py`, `authn.py`, `oauth.py`, `security.py`, `accounts.py`.
2. **Relevant dependencies:**  
   `app.py` authentication/router/security-header wiring; `limits.py`; `mailer.py` enqueue, retention, and drain paths; `db.py` transaction handling; `config.py` identity settings and validation; `entitlement.py` policy resolution, receipt forwarding, and refresh revocation checks.
3. **Persistence constraints:**  
   `migrations/0003_auth.sql`, `0002_mail.sql`, `0011_session_hashes.sql`, relevant `0011_security.sql` sections.
4. **Tests read or inspected for relevant cases:**  
   `tests/test_authn.py`, `test_accounts.py`, `test_auth_accounts_gate.py`, `test_admin_account_door.py`, `test_pending_verification.py`, `test_headers_csrf.py`, `test_security.py`, `test_mailer.py`, and relevant `tests/conftest.py` fixtures.
5. **Dependency/deployment evidence:**  
   `requirements.txt`; relevant `deploy/chudware-gateway.service` and staging service entries; installed PyJWT parser/JWKS source; integrator-provided advisory JSON.

## yield: suggested_integrator_checks

Existing regression targets, **not run**:

```text
pytest Webservice/gateway/tests/test_authn.py
pytest Webservice/gateway/tests/test_accounts.py
pytest Webservice/gateway/tests/test_auth_accounts_gate.py
pytest Webservice/gateway/tests/test_admin_account_door.py
pytest Webservice/gateway/tests/test_pending_verification.py
pytest Webservice/gateway/tests/test_headers_csrf.py
pytest Webservice/gateway/tests/test_security.py
pytest Webservice/gateway/tests/test_mailer.py
```

Run only within an isolated temporary environment with outbound transport disabled. **Unset `CHUDWARE_TEST_DB_URL` unless it explicitly identifies a disposable database:** `conftest.py` drops/recreates the selected database’s `public` schema. Use temporary storage/home paths and fixture credentials. Existing tests do not cover all finding sequences; use the bounded recipes above to supplement them.

**Coverage summary:** All assigned identity flows were traced through their nearby controls. Nine findings survive, including one configuration-dependent dependency defect; remaining gaps and unproven candidates are explicitly separated. No product changes or verification were performed.
