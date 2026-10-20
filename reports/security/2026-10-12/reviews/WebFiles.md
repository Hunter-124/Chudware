## Yield — findings

All findings below are **confirmed by current source**, not runtime-tested. No files were edited; no checks, payloads, network requests, or production operations were executed.

### WEB-1 — High: removed team members retain access to newly published document meshes

**CWE:** CWE-863, Incorrect Authorization; CWE-613, Insufficient Session Expiration  
**Impact:** Continued disclosure of confidential team geometry after membership removal.

**Source-to-sink path**

1. A legitimate team member opens or lists a team document. Daemon-confirmed responses create an HTTP mesh grant:
   - `Webservice/gateway/engine_bridge.py:675-698`
2. The grant records only `(email, doc_id)` and an expiration 24 hours later—not the team/root whose authorization permitted access:
   - `engine_bridge.py:69-80,113-149`
3. Removing a team member deletes membership and revokes **chat**, but does not invalidate mesh grants:
   - `Webservice/gateway/orgs.py:1347-1370`
   - Organization removal/demotion similarly only invokes chat revocation: `orgs.py:1065-1066,1120-1121`.
4. The removed member requests `/files/aicm?scope=personal&path=<known-document-handoff>`. Explicit personal selection avoids rechecking the old team:
   - `orgs.py:397-404`
   - `Webservice/gateway/workspace.py:546-553`
5. For handoffs outside the personal root, the route checks only the cached document grant:
   - `workspace.py:560-567,571-581`

**Why this matters beyond retaining an old download:** grants authorize the **document**, not the previously fetched revision. The member knows the PID/document naming prefix from earlier access and can request a later `_rN` mesh while the document remains open and collaborators continue editing.

**Controls considered:** authentication still applies; account suspension can prevent access; grants are bounded, expire, and invalidate when the daemon socket generation changes. These do not implement team-membership revocation. A removed user needs an otherwise active account and a previously granted document.

**Minimal remediation:** bind grants to their authorizing workspace/team and revalidate that authorization at fetch time, or invalidate all relevant grants on membership/organization changes. A cached document identifier alone must not remain sufficient.

**Safe isolated reproduction recipe — not run**

Use the temporary roots/accounts/org helpers from `tests/test_tenant_isolation.py`:

1. Create the Alice-owned team with Bob as a member.
2. Patch `_mesh_socket_generation` to a fixed fixture tuple and `tempfile.tempdir` to a fresh `tmp_path` child.
3. Pass a daemon-confirmed `doc_list` response for a document whose path is inside that team root through `_authorize_daemon_response`, with Bob’s email and that team root in `allowed_roots`.
4. Remove Bob through the existing team-member DELETE route.
5. **After removal**, create only a tiny fixture handoff, e.g. `tmp_path/private/chudware_cpp_docs/p4242_doc-audit_r2.aicm`, containing `b"new-after-removal"`.
6. Bob requests `/files/aicm` with `scope=personal` and that path. Source predicts `200` and the new bytes; the correct result is `403`.
7. Keep everything in `tmp_path`; do not create `/dev/shm` objects or connect to a real daemon.

---

### WEB-2 — High: upload limits and throttles execute only after multipart files have been buffered/spooled

**CWE:** CWE-400, Uncontrolled Resource Consumption; CWE-770, Allocation Without Limits  
**Impact:** One authenticated customer can impose repeated shared temporary-storage/memory load, including requests that will ultimately be rejected for upload size, quota, authorization to a selected team, or rate limit.

**Locations**

- `Webservice/gateway/workspace.py:215-235,245-259`
- `Webservice/gateway/bucket.py:217-247,272-288`
- `Webservice/gateway/limits.py:135-144`

Both endpoints declare `UploadFile = File(...)` and enforce upload/rate/quota policy **inside the handler**.

The locally installed framework source establishes the ordering:

- `Webservice/gateway/.venv/lib/python3.14/site-packages/fastapi/routing.py:420-423`: `await request.form()` occurs first.
- `routing.py:473-476,690-693`: dependencies/handler execution occur afterward.
- `.../starlette/formparsers.py:145-169`: defaults allow 1,000 files; the spool threshold is 1 MiB.
- `formparsers.py:178-186`: `max_part_size` checks apply to **non-file fields**, not file payloads.
- `formparsers.py:226-240,281-300`: files are spooled and all request chunks consumed before the handler sees them.

Consequently, the handlers’ “during streaming” checks actually read an already populated `UploadFile`. Even declared oversized requests are not rejected before parsing.

**Controls considered:** authentication middleware runs first. Per-account upload limits, final quota checks, partial-file removal, and parser file-count limits exist, but occur too late or bound the wrong resource. The frontend mentions an edge request-size ceiling; its live configuration was not inspected. Even an effective per-request edge cap does not make handler throttling protect pre-handler resource consumption under concurrent requests. No global early body/concurrency limit is registered in `app.py`.

The checked-in unit gives the shared process a 1 GiB memory ceiling: `Webservice/deploy/chudware-gateway.service:23-28`; that limits the host blast radius, not customer-service interruption.

**Minimal remediation:** enforce the authenticated request budget and declared length before multipart parsing; count ASGI receive bytes and abort at the real cap; bound concurrently parsing uploads and multipart file counts. Preserve the existing post-write quota check.

**Safe isolated reproduction recipe — not run**

In a temporary fixture-only FastAPI app including the actual workspace/bucket router:

1. Set upload cap to **64 bytes** and patch `MultiPartParser.spool_max_size` to **32 bytes**.
2. Supply four 128-byte file-body chunks in a multipart ASGI receive stream, plus its small header/footer; total fixture below 1 KiB.
3. Wrap the real temporary spool object or `UploadFile.write` to record bytes written. Record when the handler’s upload limiter first executes.
4. Use fixture user/root resolution; no production DB or credentials.
5. Observe that the complete file is written into the spool **before** the limiter/cap rejection. Repeat with a limiter deliberately returning `429`; parsing still precedes rejection.
6. An early-limit implementation should stop consuming the fixture after crossing the cap and should reject an exhausted request budget before any file spool writes.

Existing `test_workspace_chunked_upload_capped_midstream` asserts eventual `413` and no destination file; it does **not** establish bounded request parsing.

---

### WEB-3 — High: marketplace description attribute injection can turn an ordinary link into an immediate credit purchase

**CWE:** CWE-79, Improper Neutralization During Web Page Generation; CWE-840, Business Logic Errors  
**Impact:** A seller can make a logged-in buyer spend credits by clicking a description link presented as documentation rather than a purchase control.

This is **browser-executable application-action injection**, not a claim that inline arbitrary JavaScript bypasses CSP.

**Source-to-sink path**

- Seller description is stored as text with length/control-character checks, preserving quotes:
  - `Webservice/gateway/marketplace.py:90-102,250-255,2084-2111`
  - `Webservice/gateway/marketplace_schema.py:380-397`
- `esc()` uses text-node serialization, which escapes `<`/`&` but **does not escape quotation marks for HTML attributes**:
  - `Webservice/gateway/static/marketplace.d527cfd78705.js:24-28`
- Markdown accepts quotes within an HTTP(S) URL and interpolates the URL into a quoted `href`:
  - `marketplace.d527cfd78705.js:323-332`
- Rendered description reaches `innerHTML`:
  - `marketplace.d527cfd78705.js:425`
- The page binds purchase handlers to **every** `[data-buy]` element in the document, including attacker-created attributes in description links:
  - `marketplace.d527cfd78705.js:440-442`
- Clicking invokes authenticated checkout without another confirmation:
  - `marketplace.d527cfd78705.js:452-458`
- Credit checkout immediately settles:
  - `Webservice/gateway/marketplace_commerce.py:631-644`
  - `marketplace_commerce.py:269-355`

**Concrete description value**

```text
[Read drawing](https://example.invalid/"data-buy="credits)
```

It renders an anchor with a real `data-buy="credits"` attribute. Missing whitespace between the closing quote and the next attribute does not prevent HTML parsing from creating the attribute. The normal application script attaches the checkout listener; CSP does not block its execution.

**Important execution constraint:** use a published listing with **no previews**. The current gallery code independently serializes JSON into an attribute without quote escaping (`:362-363`), and `wireGallery()` can throw while parsing it (`:432`), before purchase listeners attach. I have not relied on that broken-gallery branch. No-preview listings are source-reachable: submission does not require preview assets (`marketplace.py:2564-2628`), and `galleryHtml()` explicitly handles an empty gallery (`JS:361`).

**Prerequisites / nearby controls**

- Marketplace enabled.
- Attacker has a published credit-priced listing, including any first-paid-listing review requirement.
- Victim is authenticated, has sufficient credits, and has not already purchased it.
- Backend correctly prevents self-purchase, unavailable listings, duplicate entitlement purchases, and insufficient balances. None proves informed user intent.
- Arbitrary event-handler JavaScript remains blocked by the ordinary portal CSP. Do not label the generic quote issue alone a confirmed arbitrary-JS XSS.

**Minimal remediation:** create links/attributes with DOM APIs; validate URLs as URLs, not a regex followed by HTML interpolation. Bind checkout only to explicitly created buttons inside the trusted purchase-action container. Apply correct attribute escaping to the other affected renderers rather than reusing text escaping.

**Safe isolated reproduction recipe — not run**

Use a local browser fixture serving only copied static HTML/JS under the existing portal CSP, with **all fetches intercepted locally**:

1. Serve `marketplace.html` and the current marketplace script from a temporary directory.
2. Open `#/m/audit`.
3. Intercept listing fetch with:

```json
{
  "id": "audit",
  "slug": "audit",
  "title": "Audit fixture",
  "description": "[Read drawing](https://example.invalid/\"data-buy=\"credits)",
  "pricing_kind": "credits",
  "price_credits": 1,
  "price_cents": 0,
  "origin": "ai_assisted",
  "assets": [],
  "preview_urls": [],
  "thumbnail_url": null,
  "seller": {"handle": "fixture"},
  "can": {}
}
```

4. Return `{"items":[]}` for review fetches. Intercept checkout and **record it without forwarding**; return an isolated fixture error or response. Block all nonlocal navigation.
5. Assert that `#mk-description a` has `dataset.buy === "credits"`.
6. Click that description link; the intercepted request is `POST /api/marketplace/listings/audit/checkout` with `{"currency":"credits"}`.
7. No real seller, credit ledger, Stripe service, or account is required.

---

### WEB-4 — Medium: recents/chat persistence bypasses storage quotas, and rename makes the bypass cumulative

**CWE:** CWE-770, Allocation Without Limits; CWE-863, Incorrect Authorization  
**Impact:** A customer can grow storage past the plan quota and consume the shared filesystem, affecting other customers.

**Source-to-sink path**

- Recents accepts up to ten strings, but does not bound each string’s size or enforce storage quota:
  - `Webservice/gateway/workspace.py:387-410`
- Chat limits an individual body to 5 MiB but writes it without storage accounting:
  - `workspace.py:516-537`
- The ordinary rename endpoint permits the hidden metadata files to be renamed to arbitrary in-root destinations and performs no quota check:
  - `workspace.py:280-292`
- Repeating PUT → rename preserves every prior file and allocates another outside the normal upload/copy quota enforcement.

**Controls considered:** authenticated identity, root jail, rate limiting, ten-entry recents limit, chat envelope validation, and chat’s per-request cap all apply. Rate limits constrain speed, not total storage. The quota scanner would count the resulting ordinary files, but these routes never consult it.

**Minimal remediation:** account for every persistent write, including metadata/chat and temporary replacement space, using the existing root storage lock and correct account/team quota. Bound recents string/body sizes. Do not rely exclusively on making metadata filenames inaccessible: a single unbounded recents write already bypasses accounting.

**Safe isolated reproduction recipe — not run**

With `test_tenant_isolation.py`’s temporary roots and a fixture account:

1. Set that plan’s storage quota to **64 bytes** using the same `dataclasses.replace` pattern as `test_workspace_disk_quota_enforced`.
2. `PUT /workspace/recents?scope=personal` with `{"files":["x"*128]}`.
3. Assert the request succeeds and `.chudware_recents.json` is larger than the quota.
4. Rename it with:
   ```json
   {"src":".chudware_recents.json","dst":"saved-1.json"}
   ```
5. Repeat the PUT once. Both files remain while the account is already over quota.
6. Total test data is under 1 KiB. A corrected implementation should return `507` before committing the first over-quota write.

## Yield — hypotheses, policy gaps, and unavailable evidence

These are **not additional confirmed remote findings**:

- **Recursive-copy symlink escape:** `workspace.py:328-350` checks the top-level source but calls default `shutil.copytree`, which follows descendant symlinks. `limits.py:158-168` skips symlinks when estimating incoming size. A pre-existing descendant symlink to a file outside the jail could therefore be dereferenced into a downloadable in-root copy. I did **not** establish a remotely accessible symlink-creation primitive in this assignment. Uploads do not preserve filesystem symlink types, and these routes do not unpack archives. Native/agent review should correlate any genuine untrusted link-creation path before promoting this to a remotely exploitable finding.
- **TOCTOU:** resolve/check and later open/FileResponse are separate operations; the gateway lock explicitly excludes native/Box writers (`limits.py:179-185`). No proven attacker-controlled concurrent symlink writer was found here.
- **CSRF / WebSocket Origin:** `AuthMiddleware` does not validate Origin, and chat accepts authenticated sockets without an Origin check. Production session cookies are explicitly SameSite=Lax (`authn.py:490-495`), blocking ordinary cross-site credentialed POST/WS attacks. A malicious **same-site sibling origin** would change that analysis, but no such controllable origin was established. There is no global permissive CORS middleware in `app.py`. Do not call absence of a CSRF token alone a proven exploitable CSRF.
- **Caches:** workspace/bucket downloads explicitly use `no-store`. Meshes matching `_r\d+` receive year-long private immutable caching (`workspace.py:72-80`), including caller-owned files that merely contain that pattern. This complicates revocation/shared-browser account switching and permits stale mutable “revisioned” files; no deployed cache behavior was exercised. API JSON responses such as chat history lack explicit `no-store`, but no cross-user shared-cache reuse was demonstrated.
- **Failed overwrite integrity:** upload writes directly to existing destinations and unlinks on failure (`workspace.py:247-260`; `bucket.py:273-290`). Thus a failed replacement can destroy the previous file. This is a real data-loss behavior, but I did not rank it as a separate adversarial authorization defect: the requesting actor already has delete rights to that file.
- **No executable-content finding from downloads alone:** attachment headers and `nosniff` prevent treating uploaded HTML/SVG as a demonstrated navigational stored XSS. A dangerous filename or file’s textual contents are not, by themselves, browser execution.
- **Public operational claims:** `public-site/src/pages/security.astro` describes tunnel-only ingress, account/root boundaries, enforced caps, and no independent certification/audit. The cap claim is qualified by WEB-2/WEB-4. Actual tunnel configuration, deployment state, operational isolation, and rollout claims remain the deployment reviewer’s responsibility.

### Public frontend dependency advisory classification

I consumed **the integrator’s** `artifact://8`; I did not run another scan.

- Current pins: Astro **7.2.0**, MDX **7.0.5** (`package.json:13-16`; lockfile `:252-255,2380-2383`), devalue **5.9.0** (`package-lock.json:2784-2787`).
- The integrator’s report identifies Astro AVIF optimizer RCE, **GHSA-26w7-cxv4-gfx2**, and base-path authorization bypass, **GHSA-376h-93r7-7g6f**, plus devalue advisories.
- **Not demonstrated as live public-site RCE/auth bypass.** `astro.config.mjs:4-13` sets `output: 'static'`, no server adapter or custom base. `.github/workflows/site-build.yml:37-54` runs `npm ci`, builds static output, then publishes `dist` to Cloudflare Pages.
- No `astro:assets`, `getImage`, `Image`/`Picture`, server actions, server rendering, or devalue calls were found in site source. Reviewed screenshots are ordinary `<img>` references to static PNG files.
- The only source `fetch()` found is the browser release-metadata request in `src/pages/download.astro:343`, rendered with `textContent`.
- Accordingly: **confirmed vulnerable build dependencies requiring upgrade; remotely reachable advisory execution not established in this static deployment.** An attacker-controlled AVIF passed to an optimizer would be a different/build-time prerequisite, not a customer’s request to the deployed static site.
- Exact dependency pins, lockfile integrity fields, SHA-pinned checkout, and `persist-credentials: false` are positive supply-chain controls; they do not remove the reported advisories.

## Yield — reviewed coverage and positive controls

### Reviewed paths / subareas

1. **Full assigned Python file surfaces:** `workspace.py`, `bucket.py`, `app.py`.
2. **Relevant serving/transports:** `appserve.py`, `portal_serve.py`, `chat.py`.
3. **Focused boundary dependencies:** `limits.py`; `auth.py` middleware/public-path behavior; `authn.py` cookie policy; `orgs.py` selection/revocation; `engine_bridge.py` mesh grants and daemon-response enrollment; gateway dependency pins and installed FastAPI/Starlette parsing/serving implementation.
4. **Browser surfaces:** static inventory and injection/source searches across first-party scripts; focused reads of bucket, portal, login, marketplace, studio, admin, admin marketplace/billing/terminal render paths and marketplace bootstrap.
5. **Public site:** source/public inventories; Astro configuration; package/lock metadata; headers; BaseLayout; docs-search; download renderer; security claims; image/dynamic-rendering searches; site-build workflow.
6. **Existing check source:** tenant-isolation, header/CSRF, portal, appserve, chat, bucket UI, and marketplace-input-hardening tests; selected test bodies and exact relevant test names.
7. **Minimal marketplace backend tracing only:** description normalization/publication and credit checkout sink, to establish WEB-3. General commerce/auth audit remains delegated.

### Positive controls observed

- Root containment uses fully resolved paths, rejects traversal/absolute forms and final-component symlinks.
- Legacy shared workspace/bucket scope is admin-only; team membership is resolved on file requests.
- Bucket root rename and both root-deletion paths are explicitly refused.
- Upload/copy writes use a bounded striped storage lock and final actual-byte quota checks.
- Personal chat persistence remains personal even while a team workspace is selected; replacement is atomic.
- Mesh handoffs require document grants, restrictive handoff names, expected scratch directories, and socket-generation validation; arbitrary shared scratch is not generally exposed.
- Chat history/presence is team/account scoped, queries are parameterized, and membership deletion actively revokes idle chat sockets.
- Bucket filenames and ordinary portal data use DOM `textContent`/attribute APIs rather than interpolated HTML.
- Static/app paths are resolved beneath configured roots; default Starlette static serving performs realpath containment.
- FileResponse quotes/percent-encodes attachment filenames (`starlette/responses.py:319-325`); no hand-built unescaped download header found.
- Ordinary portal CSP excludes inline scripts/eval; WASM-specific permissions remain isolated to `/app`; CSP, `nosniff`, anti-framing, HSTS and opener policy are applied.
- Portal assets are content-hashed; compressed responses vary on `Accept-Encoding`; stale precompressed bundle siblings are rejected.
- Public site has restrictive declarative headers, locally hosted scripts/fonts, static output, and no discovered user-content ingestion endpoint.

### Explicit limits

No production/runtime/browser behavior was exercised. No live deployment headers, CDN caching, mounted Box semantics, customer data, credentials, or advisory exploit payloads were inspected. Vendored minified terminal/window libraries were not exhaustively audited. Backend marketplace commerce, broader engine/agent/native parsers and archive import handling remain outside this assignment.

## Suggested existing checks for the integrator

Run only in the repository’s isolated test environment; **none were run here**:

- `tests/test_tenant_isolation.py` — especially:
  - `test_workspace_rejects_planted_symlink`
  - `test_workspace_chunked_upload_capped_midstream`
  - `test_workspace_disk_quota_enforced`
  - `test_workspace_copy_enforces_quota_without_partial_destination`
  - `test_mesh_handoffs_require_the_accounts_daemon_grant`
- `tests/test_chat.py::test_membership_removal_revokes_idle_socket_without_affecting_others`
- `tests/test_headers_csrf.py`
- `tests/test_appserve.py`
- `tests/test_portal.py`
- `tests/test_bucket_ui.py`
- `tests/test_marketplace_input_hardening.py`

Existing tests cover useful controls but do not replace the four bounded reproductions above.

**Coverage summary:** four substantiated findings: post-revocation geometry disclosure, pre-limit multipart resource consumption, browser application-action injection causing unintended credit checkout, and cumulative storage-quota bypass. No confirmed arbitrary-JavaScript XSS, general direct path traversal, filename header injection, archive-extraction exploit, or live Astro RCE was established in this scope.
