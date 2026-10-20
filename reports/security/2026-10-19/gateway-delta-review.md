# Gateway delta review — 2026-10-19 (source and dependencies only, read-only)

> Scope note (added by the 2026-10-19 assessment): this review covers the **uncommitted working tree**. Production runs commit `92a4ebb7`, which predates all of the RB-01..RB-12 remediations listed here as intact.

## Method and limits
- **Baseline:** `.git/logs/HEAD` and `origin/master` both stop at `92a4ebb7` on 2026-10-12, which is the audit's base commit. Nothing has been committed since, so every later change is uncommitted. I couldn't run `git log` or `git diff` without a shell. Instead I reviewed the current tree in full for the internet-facing surface, and used `docs/REFACTOR_LOG.md` (entries 2026-10-13 to 10-16) and the file ordering to find what changed.
- **Not covered:** production hosts, Cloudflare/WAF settings, the deployed venv, and the cell runtime internals beyond the launcher flags. Nothing was executed.
- **Evidence labels:** Source-confirmed means the full path was traced in source. Conditional means it depends on something I couldn't see, and the dependency is named.

## Findings

| ID | Sev | Prerequisite | Location | Impact | Fix |
|---|---|---|---|---|---|
| GW-D01 | **High** | **Anonymous** (variant: any signed-up account) | No body limit before parsing. Public prefixes `auth.py:114-139`. FastAPI reads and parses the whole JSON body before the handler runs (`.venv/.../fastapi/routing.py:425-438`); Starlette has no cap (`starlette/requests.py:254-257`). Anonymous routes taking a body: `authn.py:778,820,872,890,917`; `releases.py:941,1051,1229` (body parsed before the token check); `/api/marketplace` write routes such as `marketplace_commerce.py:631`. A chunked upload skips the Content-Length check in `billing.py:713-719` and `entitlement.py:650-659`. The service runs one process with no limit flag and `MemoryMax=1G` (`chudware-gateway.service:24,27`). Variant: multipart is parsed before the role/feature check in `releases.py:1831-1833` and `marketplace.py:2318-2327`. | One request carrying tens of MB of compact JSON (e.g. an array of `{}`) grows past 1 GiB once parsed. Systemd then OOM-kills the gateway, which restarts after 3 s and drops every session, WebSocket and `/engine` bridge. It can be repeated indefinitely. Per-IP rate limits run inside the handlers, so they never fire. **Source-confirmed; conditional** on Cloudflare forwarding bodies that large (its default plan limit is 100 MB). | Add an ASGI byte limit outermost that rejects oversized Content-Length and counts streamed bytes, with per-prefix caps (e.g. 64 KiB for `/api/auth` and marketplace JSON, 1 MiB for the webhook). Use `bounded_form` and check authorization before parsing multipart. Optionally add a Cloudflare size rule. |
| GW-D02 | Medium | Anonymous | `authn.py:128-142` keys on the exact client IP. Limits are in `limits.py:112-113` and called at `authn.py:786,828,875,898,926` and `oauth.py:548,583`; releases and security key on `request.client.host` the same way. The limiter's cleanup (`limits.py:57-84`) can't evict keys that are still fresh. The public site claims "per IP or network" (`public-site/src/pages/security.astro:140`). | Anyone with one IPv6 /64 can rotate addresses and get effectively unlimited per-IP budget. That opens credential stuffing across many accounts (only the 5-per-email cap remains) and powers D03–D05. The key tables grow without bound and the cleanup does O(n) work per new key. In reverse, everyone behind one CGNAT or campus IP shares 20 auth actions per 15 min (an OAuth sign-in uses 2), so one abuser can lock them all out. | Key IPv6 on /64 (plus /48 as a second tier). Add per-action and global auth budgets and bounded LRU eviction. Add a Cloudflare rate-limit rule for `/api/auth/*`, and Turnstile on register/forgot once a threshold is hit. |
| GW-D03 | Medium (account takeover) | Anonymous, plus either the victim clicking an unsolicited link or a corporate link scanner fetching it | Register lets anonymous callers put a password on **any** account without a credential, OAuth link or admin role, including existing *active* accounts (`authn.py:795-815`). The verification link marks it verified on a plain GET (`authn.py:860-869` → `707-712` → `259-270`). OAuth links accounts by email and keeps verified passwords (`oauth.py:505-525`, `authn.py:725-747`, which deletes only `NOT email_verified`). | (a) Active accounts that have no password and no OAuth link (e.g. old Cloudflare Access-era users): the attacker's password becomes valid as soon as the link is fetched, giving access to existing data. (b) New addresses: the attacker controls a trial account in the victim's name. Their password survives the victim's later Google/GitHub/Microsoft sign-in, so they share the victim's future work. (c) A verified account on a configured trial domain also draws on the admin-sponsored Claude subscription (`credits.py:841-866`, `provider_accounts.py:771-847`). Mail scanners such as Safe Links often fetch links in mail on their own. **Conditional.** | Treat any existing non-pending account as taken. Make verification a page whose button POSTs, rather than consuming the token on GET. Tell the owner when a password is added. When OAuth links to an account whose password came from self-registration, require re-confirming that password or drop it. |
| GW-D04 | Medium | Anonymous | `ensure_account` creates a permanent pending account before the "taken" check (`authn.py:796`; `accounts.py:196-231`). The mail limit is keyed on the exact address (`mailer.py:91,164-183`), so `user+N@` and Gmail dot variants each get their own budget. There is no global send budget, against a documented 100/day Resend free tier (`mailer.py:89-90`). There is no CAPTCHA. Each anonymous OAuth start writes a state row to the database (`oauth.py:215`). Trial accounts get cloud workspaces with 5 GiB (`plans.py:41-47`). | Email-bombing one inbox: about 80/h from one IPv4, unbounded with D02. Exhausting the mail quota stops legitimate verification and reset mail. Sending to non-existent addresses causes bounces that can get the sending account suspended. The admin pending queue and the database can be flooded. Anyone with a catch-all domain can farm unlimited free cloud workspaces (compute and storage abuse). | Keep unverified sign-ups in a separate table with a TTL. Normalise the recipient key used for throttling and add a global outbound budget that reserves room for recovery mail. Add Turnstile and per-network/global caps on registrations. Gate trial cloud capacity (per-domain caps or approval). |
| GW-D05 | Medium | Anonymous | Argon2id (64 MiB, t=3, p=4) runs synchronously inside `async def` handlers on the single event loop: `authn.py:793` (register), `836-839` (login, including the dummy hash for unknown emails), and the reset path. | Each attempt freezes all HTTP and WebSocket traffic for the length of a hash (an estimated tens to hundreds of ms; not measured). The per-email cap is bypassed by using a new email each time and the per-IP cap via D02, so a modest request rate starves the whole gateway. | Run hashing in a small semaphore-bounded worker pool, return 429 when it is saturated, and add a global auth budget. |
| GW-D06 | Low | Anonymous | `authn.py:831` and `limits.py:112` count every attempt (not just failures) against the email, before the password check. | 5 requests every 15 min lock any account, including admins, out of password sign-in indefinitely, from a single IP. | Count only failures, keyed on (email, network). Escalate to a challenge instead of refusing outright. |
| GW-D07 | Low | Admin-configured customer endpoint run by that customer | `entitlement.py:158-176`: `urllib.request.urlopen` follows 301/302/303 redirects (POST becomes GET to any host). This bypasses the pinned-address HTTP client added for RB-08. | Blind GET requests to loopback or LAN addresses, triggered by that customer's device refreshes. | Use the pinned client with redirects disabled. |
| GW-D08 | Low | Needs a separate XSS | `app.py:136-141` sets `connect-src 'self' ws: wss:`. | Any WebSocket host is allowed, which gives XSS an exfiltration channel. | Restrict to `'self'`. |
| GW-D09 | Low, still open (2026-10-12 Origin-check note) | Same-site origin (`*.chudware.com`) or a future SameSite change | No server-side Origin check on `/engine`, `/ws/chat` or mutations (`engine_bridge.py:323`, `chat.py:323`). | Cross-site WebSocket hijacking from a compromised sibling subdomain. | Check `Origin` against the public base URL on WebSocket accept and on unsafe methods. |
| GW-D10 | Info | Any active trial account | `app.py:476-497`: `/healthz` returns host memory, GPU (nvidia-smi output), `refuse_new`, `dev_mode` and presence counts to non-admin users. | Host fingerprinting by any free account. | Limit the detailed response to admins. |
| GW-D11 | Info, conditional | — | `requirements.txt:14` pins PyJWT 2.15.0, but the local `Webservice/gateway/.venv` still has `pyjwt-2.13.0.dist-info`. | If the production venv is equally stale, the PyJWT advisories are still live. `auth.py:566` catches `RecursionError`, which mitigates the main one, and Cloudflare Access isn't configured. | Check the prod venv against `requirements.txt` and rebuild if needed. |
| GW-D12 | Info | — | `contracts/cell_methods.json` now has **664** entries (571 allowed, 93 refused) against the **660** classified in the 10-13 acceptance. | 4 methods were added after acceptance without review. | Diff the policy against the accepted version and re-run the two-tenant method matrix. |

## Anonymous routes (Q1)
The public-route matcher is exact match or `<entry>/` subpath (`auth.py:114-139`); the `/healthzFOO` fix is intact. On public paths the user is only *soft*-attached. Every route below accepts and parses its full body before its own checks run (GW-D01).

- **Health:** `GET /healthz` returns only `{ok:true}` to anonymous callers.
- **Stripe:** `POST /api/billing/webhook` — HMAC-verified; returns 503 if billing isn't configured.
- **Desktop entitlement:** `POST /api/entitlement/refresh` — device-signed.
- **Security events:** `POST /api/security/events` — IP rate limit applied first, gzip expansion bounded.
- **Releases, open:**
  - `GET /api/releases/latest` — download URL only if `_may_download`.
  - `GET /api/releases/available` — catalogue, CORS allowlist.
  - `GET /api/releases/base-version`
  - `GET /api/releases/artifact/{key:path}` — needs a session, entitlement or download token, plus a published key.
- **Releases, token-gated** (Bearer `CHUDWARE_GATEWAY_RELEASE_TOKEN`, compared with `hmac.compare_digest`): `POST /api/releases/latest/publish` (public only because of the `/api/releases/latest` prefix), `POST /api/releases/ci/publish`, `PUT /api/releases/ci/upload/{name}`, `POST /api/releases/ci/status`, `GET /api/releases/ci/builds`.
- **Sign-in API:**
  - `POST /api/auth/{register,login,logout,resend-verification,forgot,reset}`
  - `GET /api/auth/verify`
  - `GET /api/auth/oauth/{provider}/{start,callback}`
- **Pages and assets:**
  - `GET /signin|/signup|/forgot|/reset` (`login.html`)
  - `GET /auth-assets/*` — hashed `login*` files only.
  - `GET /marketplace`
  - `GET /marketplace-assets/*` — hashed `marketplace*` files only.
  - `GET /static/base.css` — exact path.
  - `GET /install.sh`
  - `GET /brand-assets/<hashed>.woff2`
  - `GET /favicon.ico`, `/favicon.svg`, `/icon-192.png`, `/apple-touch-icon.png`, `/robots.txt` — fixed map.
- **Marketplace API** (all return 404 unless `CHUDWARE_MARKETPLACE_ENABLED`):
  - Anonymous reads: `GET /api/marketplace/{meta, handle-check, listings, listings/{id}/reviews, listings/{slug}, sellers/{handle}, assets/{id}, suggest, download/{id}}`. Download serves anonymously only for free, ungated assets.
  - Writes and private reads return 401 from `require_user` inside the handler: favorite, reviews POST, report, collections, checkout, tip, render, orders, me.

Everything else gets 401 (or 303 to `/signin`), or WebSocket close 4401. That includes `/`, `/app*`, `/bucket`, `/studio`, `/engine`, `/ws/chat`, `/admin*`, `/api/admin/*`, `/api/_ops/*`, `/metrics`, `/desktop/authorize`, `/workspace/*`, `/files/aicm`, `/api/{portal,orgs,providers,studio,plans,me,chat}`, `/portal-assets/*` and `/static/*`.

## Enumeration, brute force and signup abuse (Q2)
- **Account enumeration:** register, login, forgot and resend all return identical responses. Register now does one Argon2 hash on every path, which fixes ID-09. Login runs a dummy hash for unknown emails and only returns 403 "unverified" when the password is correct. Forgot and resend still have a small timing gap (mail preparation plus a database insert versus nothing, not measured). I found no new strong oracle.
- **Brute force and credential stuffing:** 5 attempts per email per 15 min is effective against targeted guessing. The per-IP limit can be bypassed (D02), and there's no CAPTCHA, MFA or breached-password check. The lockout side-effect is D06.
- **Signup abuse:** D04 and D03. CPU exhaustion is D05; memory exhaustion is D01.

## Admin route protection (Q3)
- Every admin surface is outside the public list and checks `role=="admin"` from the database (`accounts.py:85-90`): `/admin`, `/admin/terminal` (`CHUDWARE_WEB_TERMINAL_ENABLED=0` in prod), all `/api/admin/*` (accounts, billing, marketplace, credits, releases, deploy status/log, logs, ops, browse, refuse-new, appeals, terminal), `/api/_ops/deployment-drain/*` and `/metrics`.
- The admin role is never granted over HTTP, and register refuses to add a password to an admin.
- `deploy_ops.py` is now read-only (status and log), which removes the old staging-to-production deploy trigger.
- Admin static assets are gated by `_AdminGatedStatic`.
- **Gaps:**
  - No MFA, step-up or IP allowlist for admins.
  - Admin password login goes through the public `/api/auth/login`.
  - Admins can be locked out (D06).
  - `/api/admin/releases/upload` parses the multipart body before checking the role (D01 variant).

## New path traversal, SSRF or injection (Q4)
- **No new path traversal or injection found.** The new or changed routes I reviewed are safe: the favicon/robots fixed map, brand-asset font validation, the drain lease regex, `/files/aicm` with `open_beneath` (no-follow), `workspace._resolve` rejecting control characters, and `.`/`..` rejection in `directory_beneath`.
- **SQL:** everything in the reviewed routes is parameterised.
- **SSRF:** the only deviation is D07.
- **Network isolation:** cells run under bubblewrap with `--unshare-net --clearenv --cap-drop ALL` (`Webservice/deploy/cell/cell_runtime.py:15-17`).
- **Public site:** neither `Webservice/public-site` nor the anonymously served gateway files contain secrets or internal hostnames (searched for livingroom, cloudcastle, scootware, `10.89.`, `192.168.`, `ts.net`, tunnel names and the team domain). The one minor item: `public/images/provenance.json` exposes build commit IDs and capture commands, which is informational only.

## Regression check — RB-01 to RB-12 (Q5)

| RB | Status in current source | Evidence |
|---|---|---|
| RB-01 | **Intact, contained.** Strict single-object JSON with duplicate keys rejected, then re-serialized. Default-deny admission from `cell_methods.json` plus a rights check. `agent_spawn`, `agent_stop`, `agent_list`, `daemon_*`, `recovery_*` and `worker_control_info` are refused. `run_model`, `doc_compile_cpp` and others are allowed **inside** the bubblewrap cells by design. | `engine_bridge.py:72-129,438`; `cell_runtime.py:15-17`. Caveat: D12. |
| RB-02 | **Intact.** NUL and control characters rejected in all bridge strings; no-follow fd walks; mesh grants keyed by (uid, workspace, doc) and invalidated. | `engine_bridge.py:72-97,196-200`; `cells.py:78-129`; `workspace.py:117-139` |
| RB-03 | **Intact at the gateway.** Reserved credential fields are refused; grants are minted server-side; cells run with a cleared environment. One new deliberate pooled-credential path: admin-sponsored subscriptions for trial domains (see D03(c)). | `engine_bridge.py:84-89`; `provider_broker.py`; `provider_accounts.py:771-847` |
| RB-04 | **Intact.** OAuth deletes unverified passwords; `grant-admin --oauth-only` removes credentials and revokes capabilities and sessions; suspension revokes sessions in the same transaction; session creation locks the account row and refuses blocked accounts; billing never changes account status; register refuses admin/OAuth/password-holding accounts. The related 10-12 identity findings are also fixed: ID-02 (`authn.py:860-869`), ID-03 (`security.py:44-62,113-124`), ID-04 (`authn.py:749-772`, `mailer.py:164-183`), ID-06 (encrypted outbox context), ID-08 (`auth.py:566`), ID-09 (`authn.py:793`). **New residual: D03.** | `authn.py:725-747,1094-1102,319-334,795-810`; `accounts.py:257-291`; `billing.py:~455-474` |
| RB-05 | Not re-verified (parser and cell scope). | — |
| RB-06 | **Intact in the gateway part.** Token compared with `hmac.compare_digest`; `verify_bundle` checked; publisher attestation required for stable. | `releases.py:436-446,684-695` |
| RB-07 | **Intact in the gateway part.** Staging must use its own database and role; deploy routes are read-only; staging release origin is pinned. | `db.py:63-80`; `deploy_ops.py`; `releases.py:_public_base_url` |
| RB-08 | Mostly intact (marketplace sources use the pinned client). **D07 is a new exception.** | `marketplace_sources.py:349-466` |
| RB-09 | **Intact.** The buy action is only wired to direct children of `#mk-buy-actions`; markdown is escaped first. | `marketplace.4a35abb03b57.js:325-360,455` |
| RB-10 | **Intact for the browser.** All `fw_*` and `mfg_*` methods are refused for cells. | `cell_methods.json` |
| RB-11 | Operational, not source. No secrets found in the public site or anonymously served gateway files. | grep results above |
| RB-12 | **Source intact; deployment unverified.** Pins are fixed and `RecursionError` is caught, but the local venv still has PyJWT 2.13.0 (D11). | `requirements.txt:10-26` |

The remaining 10-12 multipart spooling issue is fixed for workspace and bucket uploads (`bounded_form`) but **still open** for admin release upload and studio asset upload (D01 variant). The missing Origin check is **still open** (D09).

## Reviewed paths
- **Gateway, in full or the relevant sections:** `auth.py`, `authn.py`, `oauth.py`, `security.py`, `entitlement.py` (refresh, authorize, receipt forwarding), `billing.py` (webhook, checkout), `releases.py` (public, CI and admin), `portal_serve.py`, `app.py`, `deployment_drain.py`, `deploy_ops.py`, `engine_bridge.py`, `cells.py`, `workspace.py`, `limits.py`, `mailer.py`, `accounts.py`, `credits.py`, `provider_accounts.py`, `provider_broker.py`, `orgs.py` (create, invite), `plans.py`.
- **Marketplace:** the anonymous read routes and the auth on write routes in `marketplace.py`, `marketplace_commerce.py`, `marketplace_render.py`.
- **Policy and deployment:** `contracts/cell_methods.json`, `Webservice/deploy/chudware-gateway.service`, `cell/cell_runtime.py`.
- **Frontend:** `static/login.*.js`, `static/marketplace.*.js`.
- **Dependencies:** installed FastAPI/Starlette body handling; gateway venv package versions.
- **Public site:** `Webservice/public-site/{public,src}`.
- **Prior audit:** `SECURITY_AUDIT.md`, `dependency-dispositions.md`, `reviews/WebIdentity.md`, plus the other reviews' references.

**Checks the parent should run** (in an isolated environment):
- `pytest Webservice/gateway/tests/{test_authn,test_headers_csrf,test_security,test_engine_bridge,test_releases}.py`
- Recreate D01 with a bounded body against a disposable gateway running with a reduced `MemoryMax`.
- Diff `contracts/cell_methods.json` against the policy accepted on 10-13.
- Check the production venv's PyJWT version.
- In production, query for accounts that are active but have no credentials row and no `oauth_identities` row (the population at risk from D03(a)).