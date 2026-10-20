# Public rollout — security state and cutover runbook

Written 2026-07-28 after a full audit of the gateway, the static front end, and
the `livingroom-pc` host. Read this before widening access to anyone.

> **Updated 2026-08-10:** the streamed-session product (podman session
> containers, `/api/sessions`, the `/stream/<sid>/…` proxy, `/transfer`, the
> session image and the `chudware-webd-sync` auto-promote units) was **removed**.
> The gateway now serves only the WASM `/app` client and the `/engine` bridge.
> Table rows below marked _(surface deleted)_ record findings against code that
> no longer exists; they are kept because the reasoning still applies to the
> surfaces that remain.
> **2026-10-13 source hardening:** web contract 2 routes admitted CAD/agent methods
> to socket-activated, namespace-isolated workspace cells and actor-private workers.
> The explicit 660-method policy refuses unclassified methods, devices, lifecycle
> authority, and unsupported provider/capture paths. BYOK credentials do not enter
> other actors' namespaces; revoked silent observers close. Saved solve state reopens
> warm, with authenticated cache provenance and background verification of foreign caches.
> This supersedes the earlier version-1 `hello`/`ping` containment in source only.
> **Not deployed or release-qualified:** authenticated worker artifacts, native publisher
> fixtures, installed runner/host isolation, quotas, rotations and incident evidence remain
> prerequisites in the [current audit](../../reports/security/2026-10-12/SECURITY_AUDIT.md).


---

## 0. The one thing to understand first

The service was built as a **single-tenant team tool** and then had a
whole-university domain (`*.ua.edu`) auto-approved into it. Before this pass,
every authenticated identity shared one workspace, one file bucket, one CAD
daemon and one chat room, with no ownership checks anywhere. That is fine for
five teammates and completely wrong for strangers.

The historical pass did not close the multi-tenant boundary. Current P0 containment
prevents general browser RPC forwarding; tenant isolation and §4 operations remain open.

---

## 1. What was fixed

| Severity | Issue | Fix |
|---|---|---|
| CRITICAL | _(surface deleted)_ `/stream/<sid>/<path>` used `urljoin`, so `path` could replace the upstream host — an authenticated user could make the gateway fetch `http://192.168.0.1/` inside the home LAN | `build_upstream_url()` rejected any path with a scheme/netloc/control char. The stream proxy was deleted 2026-08-10. |
| CRITICAL | `/workspace/*` and `/api/bucket/*` were one shared tree — any user could read, overwrite or `rmtree` any other user's files | Per-account roots via `user_slug(email)`; containment checked on the fully-resolved path; final-component symlinks rejected; `?scope=shared` is admin-only |
| CRITICAL | `/engine` piped any user into the one shared CAD daemon, which resolves whatever absolute path it is handed | Source cutover: web contract 2, explicit `contracts/cell_methods.json`, principal/role/epoch checks, one namespace-isolated cell per workspace, actor-private workers, no-follow paths and revocation. The 21-scenario two-tenant acceptance passes locally; production artifact/host qualification remains blocked. |
| CRITICAL | _(surface deleted)_ Host AI provider keys (Firecrawl/Gemini/Moonshot/OpenRouter) were copied into every session container's environment | Session containers were deleted 2026-08-10. Host keys are now resolved server-side per request and only ever attached to a canonical, provider-matching endpoint; client-supplied credentials are stripped |
| CRITICAL | Admin browser terminal = interactive shell as the gateway account | Off by default (`CHUDWARE_WEB_TERMINAL_ENABLED=0`); per-admin session ownership; opaque ids; session cap; idle timeout; output throttle; `MemoryMax`/`CPUQuota` on the scope |
| HIGH | _(surface deleted)_ Identity headers (`Authorization`, `Cf-Access-Jwt-Assertion`, `X-Forwarded-*`) forwarded to the proxy upstream | Scrubbed before forwarding; the proxy was deleted 2026-08-10 |
| HIGH | No CSP anywhere | CSP + HSTS + Permissions-Policy + COOP added; verified zero inline scripts in served HTML |
| HIGH | Uploads had no size cap — one request could fill the host disk | `max_upload_bytes` enforced on `Content-Length` **and** mid-stream; partial files unlinked |
| HIGH | Unpinned deps allowing known-CVE versions of `PyJWT` / `python-multipart` | Fully `==`-pinned |
| HIGH | The committed YAML allowlist carried real admin emails in the repo | That file was deleted 2026-08-10; accounts live in PostgreSQL, roles are assigned there, and nothing in the repo grants anyone access |
| MEDIUM | _(surface deleted)_ Session ids were 48 bits (`uuid4().hex[:12]`) | `secrets.token_urlsafe(24)`; the session manager was deleted 2026-08-10. Admin terminal session ids still use the wide form |
| MEDIUM | `/healthzFOO` was unauthenticated (prefix match) | Exact-match + `/` boundary |
| MEDIUM | `/healthz` leaked `dev_mode`, RAM, VRAM and live-session counts to anonymous callers | Anonymous callers get `{"ok": true}` |
| MEDIUM | `/static/admin.*` readable by any authenticated user | Admin-gated |
| MEDIUM | Chat WebSocket had a `?email=` identity fallback | Deleted |
| MEDIUM | `_valid_project` / `admin_browse` accepted any absolute host path | Confined to `CHUDWARE_WEB_PROJECT_ROOTS` |
| MEDIUM | No per-user rate limits or quotas anywhere | `limits.py`: per-account rate limiters, upload caps, disk quota; `/engine` bridge cap per account |
| LOW | `admin_logs` returned raw journal output that can contain tokens | Redaction pass for JWTs, `cfat_`, `Bearer`, `*_KEY=`/`*_SECRET=`/`*_TOKEN=` |
| LOW | Release filenames could inject into `Content-Disposition` | `[A-Za-z0-9._-]{1,128}` only |

Verification: `Webservice/gateway/.venv/bin/python -m pytest tests/ -q` → **215
passed, 2 skipped**. Each fix carries a test that fails when the vulnerability is
reintroduced (mutation-checked, not just written).

---

## 2. What changed about accounts

Authorization moved from a hand-edited YAML allowlist to an account store
(`gateway/accounts.py`, PostgreSQL via `CHUDWARE_DB_URL`), and sign-in moved off
the Cloudflare Access edge into the gateway itself (`gateway/authn.py`,
`gateway/oauth.py`).

- Anyone can register: email + argon2id password, or Google/GitHub OAuth. Every
  new row is created **`pending`** — a stranger lands in an approval queue
  instead of a dead end.
- Redeeming the emailed verification link promotes that pending row to `active`
  on a 14-day trial (`CHUDWARE_WEB_TRIAL_DAYS`). The promoting `UPDATE` is
  guarded on `status = 'pending'`, so a stale link can never resurrect an
  account an admin denied or suspended.
- Only `status == "active"` yields any rights. Role comes from the database; a
  self-registered account can never be `admin`.
- A login writes a **server-side session row** and sets the opaque
  `chudware_sid` cookie, so revoking the row logs that browser out at once.
- Cloudflare Access is no longer a door at all — not to the site and, since
  2026-08-19, not to `/admin` either. `/admin` takes a `chudware_sid` session whose
  account row carries `role = 'admin'` (`python -m authn grant-admin`). Access is
  only a fallback credential the gateway accepts from a caller with no session, and
  there is no domain auto-approval knob: only email verification and admin
  approval activate accounts. Configured trial domains sponsor Claude AI for
  already active, verified users; they never approve signups.
- **The Access application itself is now deleted** (verified 2026-09-02). The
  self-hosted app "Billet Web" — id `a5c88ac6-8364-4fa1-9610-c2ab5dcf9fa8`, the one
  every hostname used to share so that a single `CHUDWARE_WEB_CF_AUD` validated all of
  them — no longer exists in the account, and the only Access applications left belong
  to an unrelated project on `scootware.us`. There is therefore no Access layer in front
  of the gateway at any path, and no shared AUD to keep unique any more. The code path
  stays supported for a deployment that wants it, but both
  `CHUDWARE_WEB_CF_TEAM_DOMAIN` and `CHUDWARE_WEB_CF_AUD` were removed from
  `gateway.env` on 2026-09-02, because leaving them set with no Access app in
  front made `auth.py::AuthMiddleware._authenticate` answer an unauthenticated
  API call with the misleading `{"error":"missing Cf-Access-Jwt-Assertion"}`
  instead of `not signed in`. Worse, a browser still presenting the deleted
  app's `CF_Authorization` cookie reached a "CF Access not configured" branch
  that returned **500**; `_authenticate` now decides whether Access is
  configured before it looks at any token, so that request is a plain 401.

Admin API: `GET /api/admin/accounts`, `.../approve|deny|suspend|plan`,
`GET /api/admin/accounts/pending-count`. Self-service: `GET /api/me/account`.

---

## 3. Remaining public-registration risks

The first two former blockers are now shipped; the remaining process-isolation
and host-boundary risks still matter.

1. **Plan entitlements and storage limits are now enforced.** `limits.py`
   applies each plan's storage quota, and `/engine` checks whether the plan has
   cloud access before opening a bridge.
2. **Pooled AI use is metered.** `credits.py` reserves and reconciles prepaid
   credits against provider token costs; a zero-balance pooled actor is stopped
   before provider dispatch. BYOK remains outside the pooled credit ledger.
3. **The CAD daemon is still one shared process.** The path jail stops user A
   naming user B's files, but a malformed or expensive RPC still degrades the
   daemon for *everyone*. True isolation needs a daemon per user, bound to a
   per-user socket.
4. **Host blast radius remains material.** The gateway account `liveuser` still
   has elevated host access by owner choice. `deploy/harden_host.sh` applies the
   accepted firewall, tunnel, update, key, backup, and gateway controls; revisit
   its declined `sudo` and `sshd` items before treating the host as isolated.
5. **Credentials exposed in git history must remain rotated.**
   `Webservice/Cloudflare-info.txt` is untracked and gitignored, but old values
   remain in commit `84963165`; never rely on any credential found there.

---

## 4. Host hardening

`deploy/harden_host.sh` is dry-run by default and item-scoped. Run it and read
the output before applying anything:

```
livingroom 'bash -s' < Webservice/deploy/harden_host.sh          # dry run
livingroom 'bash -s' -- --apply updates < .../harden_host.sh     # one item
```

Items, and why each one can bite:

- `sudo` — drops `liveuser` from `wheel`. **If you log into the desktop as
  `liveuser` you need another admin account first.**
- `firewall` — the default zone currently opens **all** of `1025-65535/tcp+udp`
  to the LAN. Closing it may break KDE Connect and mDNS peers; the script re-adds
  1716.
- `sshd` — keys only, `MaxAuthTries 3`, no X11/TCP forwarding. **Confirm a
  working key login first.**
- `tunnel` — the cloudflared token is in the process argv and readable by any
  local user via `ps`. Manual (needs a CF login).
- `updates` — no unattended security patching is configured at all.
- `keys` / `backups` — reported, not automated. Backups are unencrypted, contain
  every secret, and sit on the same disk as the service.

---

## 5. Cutover to a public homepage — APPLIED 2026-08-11

Decision taken and now live: the marketing site is **static, on Cloudflare
Pages, off the home box entirely**, and the application moved to its own
hostname. Public and bot traffic no longer reach the origin at all.

```text
chudware.com, www.chudware.com  -> Cloudflare Pages (project chudware-site)
                                   PUBLIC. No Access. No tunnel. 23 pages.
app.chudware.com                -> cloudflared tunnel -> 127.0.0.1:8300
                                   gateway's own authn is the front door
app.chudware.com/admin          -> same gateway; chudware_sid + accounts.role='admin'
```

The tunnel is `chudware-web`, id `95256541-2ebf-4101-92a1-daecd240c087`. Verified
2026-09-02 it is healthy on 4 connections and carries exactly three rules plus the
catch-all:

```text
app.chudware.com  -> http://127.0.0.1:8300   Chudware gateway
scootware.us      -> http://127.0.0.1:8320   unrelated project sharing this tunnel
www.scootware.us  -> http://127.0.0.1:8320   same — not Chudware, not a stray rule
<catch-all>       -> http_status:404
```

There is no apex, `www`, or staging rule: `https://chudware.com/` answers 200 from
Pages, and `https://app.chudware.com/` 303-redirects an HTML request to `/signin`.
Checking the gateway by curling the apex measures Pages and nothing else.

What was applied, in the order it has to happen:

1. `app.chudware.com` created as a proxied CNAME to
   `<tunnel-id>.cfargotunnel.com` and added to the tunnel ingress → `:8300`.
2. Gateway deployed (prod was 18 commits behind on `8571334f`). This is what
   made public signup possible at all: the older commit had no `PUBLIC_PATHS`,
   so `/signin` and `/signup` answered `401 missing Cf-Access-Jwt-Assertion`
   and a visitor could not reach a sign-up form by any route.
3. `CHUDWARE_WEB_PUBLIC_BASE_URL` repointed to `https://app.chudware.com`. It builds
   every verification and reset link, so leaving it on the apex would have
   mailed people links into the static site.
4. Access removed from the apex **before** DNS moved. In the other order the
   marketing site inherits the login wall, because Access is applied per
   hostname at the edge.
5. Apex + `www` repointed to the Pages project; both added as Pages custom
   domains.
6. Apex, `www` and `sso.chudware.com` removed from the tunnel ingress, and the
   `sso` DNS record deleted — it pointed at an oauth2-proxy on `:8301` that no
   longer exists as a unit.
7. Access scoped to `app.chudware.com/admin` on the *existing* app. **Superseded
   2026-08-19:** that scoping is what made an operator meet Cloudflare's login
   page instead of the gateway's, and any browser holding a stale
   `CF_Authorization` cookie was 401'd while carrying a valid session.
   `cf_provision.py` no longer provisions Access, and the leftover
   `app.chudware.com/admin*` application has since been **deleted** — verified
   2026-09-02, the account holds no Access application over any `chudware.com` host.

Verified from outside the LAN, anonymously, 28/28 checks: every marketing route
200 with no Access redirect; `/signin`, `/signup` and `/healthz` 200 on the app
host; `/`, `/workspace/list`, `/bucket`, `/api/me`, `/api/sessions`,
`/api/provider-keys`, `/api/admin/*` and `/static/admin.js` all 401 to an
anonymous caller; and a real password sign-in returns a `chudware_sid` session
that reports `role: admin`. The one check that changed on 2026-08-19: `/admin`
now redirects to the gateway's `/signin?next=%2Fadmin`, not to the Access login.

Use curl, not Python `urllib`, to re-run any of that. Cloudflare's bot
protection 403s the default `Python-urllib` user agent, which is
indistinguishable from a genuine authorization failure and will send you
chasing a security regression that is not there.

### Still open here

- **Prices — RESOLVED 2026-09-05.** The live catalog in
  `provision_stripe.py::CATALOG_SPEC` is ratified ($30/$150 monthly, 50%
  `.edu`, credit packs $10/8k, $25/20k, $100/80k) and the public
  `/pricing/` page, FAQ, and About copy now publish exactly those figures and
  the `plans.py` entitlements (Desktop: native app + 5 GB; Studio: adds the
  browser workspace + 100 GB; trial 14 days with both). Change a number in
  one place and the other two are wrong — keep them in step.
- **Stripe webhook bypass.** Still not needed: `/api/billing/webhook` is in
  `PUBLIC_PATHS`, so it is not gated by the app, and no `/admin` Access scope
  exists to sit outside of. The endpoint is live-mode only as of 2026-09-05;
  see `SETUP_CREDENTIALS.md` §6 for why.
- **The retired `cad` hostname on `scootware.us`** is fully gone: no Access
  application, no tunnel ingress, and no DNS record left in that zone.
- **Watch the queue.** Every registration is created `pending`; only email
  verification (which starts the trial) or an admin approval makes it `active`.
  `GET /api/admin/accounts/pending-count`.

## 6. Stripe setup

Environment (systemd reads `gateway.env` at unit start — `systemctl --user
restart chudware-gateway` after editing; went live 2026-09-05):

```
CHUDWARE_WEB_STRIPE_SECRET_KEY=sk_live_...
CHUDWARE_WEB_STRIPE_WEBHOOK_SECRET=whsec_...      # for THIS endpoint
CHUDWARE_STRIPE_PRICE_DESKTOP=price_...              # Desktop tier
CHUDWARE_STRIPE_PRICE_DESKTOP_EDU=price_...
CHUDWARE_STRIPE_PRICE_STUDIO=price_...               # Studio tier
CHUDWARE_STRIPE_PRICE_STUDIO_EDU=price_...
CHUDWARE_STRIPE_PRICE_CREDITS_STARTER=price_...
CHUDWARE_STRIPE_PRICE_CREDITS_STANDARD=price_...
CHUDWARE_STRIPE_PRICE_CREDITS_BULK=price_...
CHUDWARE_WEB_STRIPE_SUCCESS_URL=https://app.chudware.com/?billing=success
CHUDWARE_WEB_STRIPE_CANCEL_URL=https://app.chudware.com/?billing=cancel
```

`stripe` is pinned in `requirements.txt` and installed in the production venv.
Until the key is set, the billing routes return 503 and the gateway boots
normally — but Stripe auto-disables a webhook endpoint that keeps getting 503,
so never provision the endpoint before the keys are in `gateway.env`
(`SETUP_CREDENTIALS.md` §6).

Webhook hardening already in place: signature verified before parsing, 64 KiB
body cap, event ids persisted so Stripe's at-least-once redelivery cannot
double-apply. Cancellation and payment failure downgrade the *plan* only —
they never un-approve the human.

---

## 7. Suggested order

1. Rotate the Cloudflare API token and R2 keys (§3.5).
2. Apply `harden_host.sh` items — `updates` and `firewall` first, `sudo` and
   `sshd` once you have a second admin account and a verified key login.
3. Deploy the current gateway (it is several commits behind; the box runs
   `21e2356c`). Verify `/healthz` and one real `/app` session end to end.
4. Publish the Pages site and do the DNS/Access cutover (§5).
5. Wire plan entitlements into the disk-quota and token paths (§3.1) — this is
   the gate for taking money.
6. Add token metering (§3.2), then enable Stripe.
7. Only then publish the apex. Registration is already self-serve and every new
   account starts `pending`, so going live is a DNS decision, not an auth one.

---

## 8. Where this left off (2026-07-28)

### Deployed and working

Prod (`livingroom-pc`) runs the gateway at whatever `master` was last pulled;
check with `livingroom 'cd /home/liveuser/Repositories/CAD && sudo -u liveuser git log --oneline -1'`.
All 217 tests pass. Verified live: admin + standard-user authorization, the
account queue, per-account workspace isolation, the admin terminal (REST +
WebSocket + reattach), and the `/app` WASM client booting with a strict CSP.

The marketing site is live at **https://chudware-site.pages.dev** (Cloudflare
Pages project `chudware-site`). No DNS had been changed *at that date* —
`chudware.com` still went to the gateway behind Access. **Superseded by §5:** the apex
and `www` are proxied CNAMEs to the Pages project now, carry no Access wall, and have no
route to the origin at all; the gateway is reachable only at `app.chudware.com`.

#### Provider-key sharing boundary
Current P0 containment disables every inference/model-probe RPC at `/engine`,
regardless of this setting or caller role. The former turn-selection behavior
below is historical, not an available browser execution path. Account credential
storage APIs remain unchanged; no stored credential is injected through `/engine`.


`CHUDWARE_WEB_SHARE_PROVIDER_KEYS` is now an enforced financial and credential
boundary. It defaults to `0`, and public or otherwise multi-tenant deployments
must leave it there. At `0`, `/engine` does not read or inject credentials from
the host `config.json` or `~/chudware-web-data/ai_providers.env`; this applies
equally to customers, administrators, and the operator's own web account.
For `key_source: "byok"`, the gateway first verifies that this account has a
validated credential for the selected provider; the worker decrypts that
account-scoped credential server-side and the turn bypasses the credit ledger.
Selecting another provider without an attached key does not reuse that key:
eligible trial-domain users can take the isolated Claude sponsor path, other
accounts with `ai_grant` take the managed pool path, and accounts with neither
usable source are refused before the shared daemon. None falls back to the
operator's ambient profile.

Managed access is separate from host-profile sharing. After an account's own
matching BYOK key and personal Claude link, an active user with a verified email
or OAuth identity on a configured, unexpired trial domain (or true subdomain)
can use active admins' linked personal/shared Claude OAuth subscriptions.
Sponsored turns run in an isolated worker with Anthropic forced, no gateway
token-spend cap and no credit reservation or balance debit; trial expiry and
upstream provider limits remain. Other domains, pending/unverified users, and
lookalike domain suffixes cannot use those credentials. Linked Codex OAuth
still cannot run in isolation. The metered fallback is distinct: `ai_grant`,
eligible cloud plan, and a positive credit balance yield `key_source: "pool"`;
the gateway reserves estimated cost and writes a credit-ledger spend row.
Zero balance refuses pooled turns. Admins have no automatic pool bypass.

Set `CHUDWARE_WEB_SHARE_PROVIDER_KEYS=1` only for a personal or single-tenant
deployment where the operator genuinely is every web user. That compatibility
mode restores the old behavior: model probes may receive the host key and
unmetered turns may resolve the shared daemon's host provider profile.

Historical warning: before the provider-key gate was implemented, this
environment variable had no consumer and therefore enforced nothing.
`_load_host_keys()` always read the host provider profile plus
`ai_providers.env`, `agent_list_models` injected those credentials even when
the flag was `0`, and unmetered turns could reach the shared daemon's ambient
profile. A deployment document that merely claimed `=0` prevented host spend
was false; this section records that failure rather than silently making the
old claim appear to have always been true.

### Three regressions this pass caused, and what they teach

All three were invisible to the test suite and to `curl`, and all three only
showed up by driving the real thing. Worth remembering when hardening further:

1. **CSP killed the WASM client.** Every asset returned 200; the page just
   never started. `script-src 'self'` blocks `WebAssembly.instantiate` without
   `'wasm-unsafe-eval'`, blob workers need `worker-src`, and the inline
   bootstrap script needed a hash. Fixed in `appserve.py`, which now derives
   inline-script hashes from the file on disk so a rebuild refreshes them.
2. **Requiring a container seat on `/engine` took the web client offline.**
   `/app` is browser-native and never had a podman seat. The old identity/path-key
   checks were insufficient: the 2026-10 audit reproduced process execution,
   ambient credential disclosure and a NUL path escape. The current bridge is
   default-deny; safe modeling needs real tenant ownership and worker isolation.
3. **The terminal declared dead sessions on every deploy.** `shell_died` only
   meant the `tmux attach` client died, which happens on every gateway restart
   while the tmux server survives.

### Resume here

**1. Refresh the stale `/app` bundle.** This is the "web UI is behind the
desktop build" complaint. `/app` is a *build artifact*, not source — `git pull`
never updates it. The staged bundle is from 2026-07-24. emsdk IS installed at
`/home/liveuser/.emsdk`, so on the box:

```
cd /home/liveuser/Repositories/CAD && EMSDK=$HOME/.emsdk ./build.sh wasm
```

That restages `~/chudware-web/app`. It is a long emscripten build — run it when
you can watch it. `install_webd.sh` also builds it as part of a joint release
(log: `/tmp/chudware-webd-wasm.log`). Nothing promotes a new engine build
automatically — run `update.sh` or `install_webd.sh --restart` yourself.

**2. One switch is ON for convenience and must be flipped before public
registration** (commented in `~/chudware-web-data/gateway.env`):
`CHUDWARE_WEB_TERMINAL_ENABLED=1`.

**3. The blockers in §3 are unchanged** — plan entitlements still are not
enforced (nothing reads a plan), there is no token metering, the CAD daemon is
still one shared process, `liveuser` still has NOPASSWD sudo plus LAN SSH keys,
and the Cloudflare API token in git history is still live.

### Operating notes

- Deploy = pull + `systemctl --user restart chudware-gateway`. As root the user
  bus needs: `setpriv --reuid=1000 --regid=1000 --init-groups env
  XDG_RUNTIME_DIR=/run/user/1000
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus systemctl --user ...`
- **Warn before restarting** — it drops every live terminal and engine socket.
- To exercise authenticated endpoints without a browser, mint a short-lived
  session over loopback and send it as the cookie:
  ```bash
  SID="$(cd Webservice/gateway && .venv/bin/python -m authn ops-session --email you@crimson.ua.edu)"
  curl -b "chudware_sid=$SID" http://127.0.0.1:8300/healthz
  ```
  It reads `CHUDWARE_DB_URL` from the environment, exits 1 if that address has
  no active account, and expires on its own (900 s default). A WS client must
  carry the same cookie — a 403 on a handshake from a harness with no cookie jar
  is the harness, not the server.
- Rollback commit from before this pass: `~/chudware-web-data/ROLLBACK_COMMIT`.
  Backups: `~/chudware-web-backups/` (local + unencrypted; see §4).

---

## 9. Outbound transactional mail — STILL NOT SENDING (2026-08-11)

`CHUDWARE_MAIL_BACKEND` is absent from `~/chudware-web-data/gateway.env`, so it
defaults to `console` and the journal confirms `mail: drain started
backend=console` on every boot. Every verification and reset link ever
"delivered" was written to the log. This is the one reason a registrant never
receives anything; the funnel itself is proven working end to end (register →
202 → `pending` → link → `active` + trial + session).

Do not read the absence of queued mail as a second bug. `mail_outbox` is empty
because the console backend drains successfully — the message is rendered,
logged, and marked sent.

### The transport that exists

`livingroom-pc` already runs the CloudCastle mail platform, so no third party is
needed to *send*. What blocks it is authorization, not capability:

- Stalwart is reachable from the host at **`10.89.0.2:465`** (implicit TLS,
  banner `220 mx.scootware.us Stalwart ESMTP`). Its cert is a real public-CA
  cert for `mx.scootware.us` and verifies against the system trust store when
  SNI is that name.
- The **published** ports do not work from the host. `127.0.0.1:{25,465,993,8080}`
  and `:587` all time out, because firewalld's `trusted` zone admits the
  container subnets (`10.89.0.0/24`, `10.88.0.0/16`) but the loopback DNAT path
  does not match them. Dial the bridge address, not localhost. Host-native
  ports (22, 5432, 8300) are fine, which is how to tell a firewall problem from
  a dead listener.
- Stalwart has **no 587 listener** — it is refused outright. Submission is 465
  only, which is why `CHUDWARE_SMTP_TLS=implicit` had to be added to `mailer.py`.
- Sending as `@chudware.com` is **refused today**: authenticating as
  `noreply@cloudcastle.ai` and issuing `MAIL FROM:<noreply@chudware.com>`
  returns `501 5.5.4 You are not allowed to send from this address`
  (`smtp.session.auth.must_match_sender`). `chudware.com` is not a configured
  domain on that server.

### What finishing it requires

1. **The Stalwart registry admin credential** (`admin@scootware.us`). It is not
   in `Website-Credentials/cloudcastle-mail.local.txt`, which carries only the
   `@cloudcastle.ai` mailboxes — and `admin@cloudcastle.ai` authenticates but
   answers `forbidden` to `x:Domain/query`. Without it the domain cannot be
   added. `stalwart-mail/install.sh` documents recreating a temporary admin via
   `STALWART_RECOVERY_MODE`, which restarts the mail server for both live
   domains — deliberately not done unattended.
2. Add `chudware.com` as a domain, create `noreply@chudware.com`, let Stalwart
   generate an **RSA-2048** DKIM key. Its Ed25519 signature was already retired
   for returning `neutral (no key)` at Gmail.
3. Widen `mustMatchSender` for the new domain, then
   `x:Action/set {"create":{"a":{"@type":"ReloadSettings"}}}` and require
   `created` — settings are cached at boot and a registry write alone changes
   nothing.
4. DNS on `chudware.com`. Note the SPF record Cloudflare Email Routing just
   created is `v=spf1 include:_spf.mx.cloudflare.net ~all` and authorizes
   forwarding only; it must also authorize `ip4:174.110.99.16` before Stalwart
   can send for the domain. Publish the DKIM public key and a DMARC record.
5. Then, in `gateway.env`:
   ```
   CHUDWARE_MAIL_BACKEND=smtp
   CHUDWARE_SMTP_HOST=mx.scootware.us     # + /etc/hosts: 10.89.0.2 mx.scootware.us
   CHUDWARE_SMTP_PORT=465
   CHUDWARE_SMTP_TLS=implicit
   CHUDWARE_SMTP_USER=noreply@chudware.com
   CHUDWARE_SMTP_PASS=...
   ```
   The `/etc/hosts` pin is what lets normal certificate hostname verification
   succeed while still dialling the only address that is reachable.

### The deliverability decision nobody can engineer around

The sending IP is residential Spectrum (`174.110.99.16`) with a PTR of
`syn-174-110-099-016.res.spectrum.com` that does not resolve back. A Gmail
header audit on 2026-08-08 from this host, with **SPF pass, RSA DKIM pass and
DMARC `p=reject` pass**, still landed in **Spam**. There is no message-format,
TLS, or authentication fault left to tune; the remaining classifier input is the
IP and its missing FCrDNS.

That is survivable for a link the user is actively waiting for — and the pending
page does say to check spam — but a signup funnel where first-contact mail is
filed as spam converts badly. If reliable inbox placement becomes a
requirement, the only fixes are Spectrum-provided FCrDNS, a business-mail
connection, or a reputable authenticated outbound relay as a smarthost.
Mailboxes, inbound MX, IMAP, webmail and filtering can stay self-hosted either
way; only the outbound hop moves.

### Inbound @chudware.com — done

The domain had **no MX at all**, so every address the site advertises
(`support@`, `sales@`, `billing@`, `legal@`, `privacy@`) was a black hole, as
was `SUPPORT_EMAIL` on every gateway error page. Cloudflare Email Routing is now
enabled on the zone (MX + SPF + DKIM records created).

**One manual step remains:** Cloudflare emailed `haedmonds69@gmail.com` a
verification link for the forwarding destination. Until it is clicked, rule
creation fails with `2054 Destination address is not verified`. After clicking,
create the forwards:

```bash
for l in support sales billing legal privacy postmaster abuse; do
  curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$ZONE/email/routing/rules" \
    -H "Authorization: Bearer $CF_TOKEN" -H 'Content-Type: application/json' \
    --data "{\"name\":\"$l\",\"enabled\":true,
      \"matchers\":[{\"type\":\"literal\",\"field\":\"to\",\"value\":\"$l@chudware.com\"}],
      \"actions\":[{\"type\":\"forward\",\"value\":[\"haedmonds69@gmail.com\"]}]}"
done
```

---

## 10. Defects found while cutting over (2026-08-11)

Fixed in this pass:

- **Pending accounts were told a lie with no way out.** An identity arriving
  through Access is registered by `ensure_account()` as `pending` with no
  credentials row, no token and no queued mail — but the status page claimed
  "We sent a confirmation link" and linked `/#resend`, a fragment no page
  implements. `resend_verification()` also keyed off a credentials row such an
  account never has, so it minted nothing. Now keyed off `pending` status, with
  a working button whose handler is served over the public `/auth-assets` route
  (a non-active caller cannot read `/static/*`).
- **Silent plaintext SMTP fallback.** `_deliver_smtp` did
  `if client.has_extn("starttls")` and, when absent, continued over the
  plaintext socket with the relay password and every reset link on the wire.
  Now refuses.
- **A fabricated public status page.** `public/status.json` was a static file
  asserting "Systems operational" including "Downloads: Operational" — with
  zero published releases and `chudware-monitor.service` failed. Page removed
  rather than shipped as a false claim.
- **Placeholder pricing, an invented copilot transcript, fabricated system
  requirements and dead download links** on the public site. See the site commit.

Found, NOT fixed — each is a real gap:

- ~~**No code path can create an admin.**~~ **FIXED.** `accounts.set_role` now
  exists and `python -m authn grant-admin --email …` creates-or-promotes an
  active admin (password from a prompt or stdin, verified without needing
  outbound mail, idempotent). It is deliberately CLI-only — `role` is the one
  column an authenticated caller must never move, so there is no admin-API
  writer. `users.yaml` lost: no module reads it, `config.py` has no
  `users_file` setting, and `CHUDWARE_WEB_USERS_FILE` in `gateway.env` is dead
  env — delete that line. The raw `UPDATE accounts SET role='admin'` used to
  unblock the cutover is no longer the only mechanism.
- **`CHUDWARE_WEB_SSO_SHARED_SECRET` is dead env too.** The HTTP-Basic-on-
  loopback handoff it belonged to is gone from `auth.py` (no `Basic` handling
  anywhere), and there is no oauth2-proxy unit. The deploy runbook's
  "verify the authenticated surface with `curl -u email:$SECRET`" recipe
  therefore cannot work — it returns `401 missing Cf-Access-Jwt-Assertion` for
  every path and reads as a catastrophic failure. Re-confirmed dead 2026-09-02.
  Mint a real session row instead — `python -m authn ops-session` (§8), or
  `authn.create_session("<email>", ttl=datetime.timedelta(minutes=15))` from the gateway
  venv with `~/chudware-web-data/gateway.env` sourced — and send it as the
  `chudware_sid` cookie. The runbook recipe is gone and `verify_public.sh` replaced it.
- ~~**`chudware-monitor.service` is failing every 2 minutes**~~ — **fixed.** It exited 2
  with `status publish requires R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ENDPOINT` and
  so sat in `failed` continuously from 2026-08-24, taking the RAM/disk/GPU/session
  reaper down with it. The status publish is an *optional* publisher now: with none of
  the three R2 variables set the sampler prints one skip line, records the skip in its
  JSONL sample, and exits 0; partially configured or genuinely broken still exits 2. R2
  itself is **still not enabled on the account** — `GET /accounts/<id>/r2/buckets`
  answers error 10042 — so the publish stays off until a human enables R2 in the
  dashboard. Keeping the sampler alive is not cosmetic: this host shares `/` with a
  self-hosted Actions runner whose cache filled the disk on 2026-08-10 and took
  Stalwart's RocksDB down with `No space left on device`.
- **The Cloudflare API token and R2 keys in `Webservice/Cloudflare-info.txt` are
  compromised** — the file says so itself; they were committed in `84963165`
  and are still the live credentials. Rotate them.
- **No Google OAuth in prod.** With Access no longer fronting the host, a user
  who cannot receive mail has no self-service recovery at all: no OAuth, and
  password reset needs the transport in §9. Configuring OAuth would give a mail-
  independent way in.

## 11. Desktop sign-in through OAuth (2026-08-17)

The native app has always signed in by opening the system browser at
`/desktop/authorize` and waiting on a loopback listener; there is no
email/password field in the desktop UI at all. An unauthenticated caller is
bounced to `/signin?next=<that>`, and the password form honoured `next` — but
the three OAuth anchors dropped it and the callback always redirected to `/`.
Signing in with a provider therefore stranded the browser on the portal and the
app's listener timed out. Fixed: `oauth.py` threads a validated same-origin
destination through start → callback, bound into the signed state and carried
beside the verifier in the one transaction cookie, and `login*.js` stamps it
onto the anchors. `tests/test_desktop_oauth_chain.py` drives the whole chain.

Three live-config defects surfaced while deploying it:

- **`CHUDWARE_WEB_PUBLIC_URL` in `gateway.env` was dead env.** `config.py` reads
  `CHUDWARE_WEB_PUBLIC_BASE_URL`, so §5's "repointed to app.chudware.com" was
  never in effect: every verification, reset, and OAuth `redirect_uri` was built
  for the apex static site. Renamed in place. The repo's staging unit had the
  same dead spelling and is fixed too.
- **`CHUDWARE_WEB_SESSION_SECRET` was absent**, so the OAuth state HMAC was
  keyed on the empty string. Generated and set.
- **Prod was one restart away from not booting.** The installed
  `chudware-gateway.service` predated the entitlement signing key becoming a
  startup hard requirement, and no `~/.config/credstore.encrypted` existed —
  the running process only survived because it had never been restarted since.
  The raw key was sitting at `~/chudware-web-data/entitlement_ed25519.key`; it
  is now encrypted into `chudware-entitlement.cred` and the canonical hardened
  unit from `deploy/` is installed. Staging was in exactly this state and has
  since been fixed the same way — see the staging note at the end of this
  section.

Provider credentials existed nowhere on the host when this landed. **Google and
GitHub are now configured and live**, both probed against their token endpoints
(`invalid_grant` / `bad_verification_code` prove the client pair is accepted,
as opposed to `invalid_client` / `incorrect_client_credentials`):

- Google: project `chudware`, client `Chudware`, consent screen published to
  production (External). `openid email` are non-sensitive, so publishing needed
  no verification review.
- GitHub: OAuth app `Chudware` (owner `Hunter-124`). Generating its secret needs
  GitHub sudo mode, i.e. a fresh 2FA challenge.

Proven end to end afterwards: a sign-in that touched no password landed on
`/desktop/authorize`, and a loopback listener standing in for the desktop app
captured a signed `enterprise` entitlement.

**Microsoft is now configured and proven end to end** (personal Microsoft
account, no password, landed in the portal as an Enterprise admin). App
registration `Chudware` = `19a4341c-3a4e-41ac-aa3e-358743497856`, tenant
`d8b2abaa-94b3-475a-83a3-dcee5131d7c4` (`Default Directory` /
`haedmonds69gmail.onmicrosoft.com`), audience "all Microsoft account users",
redirect `https://app.chudware.com/api/auth/oauth/microsoft/callback`, optional
ID-token claims `email` + `xms_edov`, Graph `email` permission on. A duplicate
`Chudware` registration (`8e081bc7…`) was deleted; only one exists now.
`CHUDWARE_WEB_MICROSOFT_CLIENT_SECRET` is the key name — `_SECRET` is dead env.

Four things cost real time here, all worth knowing before touching Entra again:

- **Device-code flows cannot be used in this tenant.** Both Azure CLI and Graph
  CLI die with `AADSTS530035` after a *successful* sign-in. There are no
  Conditional Access policies to relax (no Premium licence), so this is
  security defaults blocking device code outright. Portal or nothing.
- **A client secret scraped from the portal DOM can be wrong.** The value read
  out of the row's input did not match the real secret, and
  `grant_type=authorization_code` still answers `invalid_grant` for a bad
  secret because the code is checked first. Validate every secret with
  `grant_type=client_credentials&scope=…/.default` before deploying it: that
  answers `invalid_client` / `AADSTS7000215` immediately, or returns a token.
- **An app registration holds at most two client secrets.** Creating a third
  fails with "credential limit has been reached".
- **`xms_edov` is Entra-only.** Requiring it from every Microsoft identity
  refused every *personal* Microsoft account, which is the common case. The
  guard now exempts the fixed consumers tenant
  (`9188040d-6c67-4c5b-b112-36a304b66dad`) and still demands proof of domain
  ownership from every real directory — see `oauth.MICROSOFT_CONSUMERS_TID`.

### Clicking "Authorize" did nothing — CSP `form-action`

The consent form POSTs to `/desktop/authorize`, which used to answer `303` to
`http://127.0.0.1:<port>/callback`. Chromium enforces `form-action 'self'`
against the *redirect target* of a form submission, so the loopback hop was
aborted inside the browser (`net::ERR_ABORTED`, and a
`securitypolicyviolation` event naming `form-action` when instrumented live).
`curl` never reproduced it. The POST now returns a handoff page whose meta
refresh is an ordinary document navigation, plus a manual button for when the
app is not listening; `tests/test_desktop_oauth_chain.py` pins that a redirect
cannot come back.

The consent page also reuses `/signin`'s two-panel `auth-shell` so it fills the
viewport instead of being a narrow centred column.

### Staging is no longer one restart from failing

`chudware-entitlement-staging.cred` was provisioned from
`~/chudware-web-data/entitlement_ed25519.key` with
`systemd-creds encrypt --user --name=entitlement.key`, and the installed
staging unit — still the pre-hardening one, with the dead
`CHUDWARE_WEB_PUBLIC_URL` spelling and no `LoadCredentialEncrypted` — was
replaced with the canonical `Webservice/deploy/chudware-gateway-staging.service`.
Staging restarts clean on port 8310 (`healthz=200`).
