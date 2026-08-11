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

---

## 0. The one thing to understand first

The service was built as a **single-tenant team tool** and then had a
whole-university domain (`*.ua.edu`) auto-approved into it. Before this pass,
every authenticated identity shared one workspace, one file bucket, one CAD
daemon and one chat room, with no ownership checks anywhere. That is fine for
five teammates and completely wrong for strangers.

This pass closed the app-level holes. It did **not** make the host safe to point
the public internet at — see §4.

---

## 1. What was fixed

| Severity | Issue | Fix |
|---|---|---|
| CRITICAL | _(surface deleted)_ `/stream/<sid>/<path>` used `urljoin`, so `path` could replace the upstream host — an authenticated user could make the gateway fetch `http://192.168.0.1/` inside the home LAN | `build_upstream_url()` rejected any path with a scheme/netloc/control char. The stream proxy was deleted 2026-08-10. |
| CRITICAL | `/workspace/*` and `/api/bucket/*` were one shared tree — any user could read, overwrite or `rmtree` any other user's files | Per-account roots via `user_slug(email)`; containment checked on the fully-resolved path; final-component symlinks rejected; `?scope=shared` is admin-only |
| CRITICAL | `/engine` piped any user into the one shared CAD daemon, which resolves whatever absolute path it is handed | `_inject_identity()` overwrites the client's claimed identity; `path_violation()` rejects any RPC naming a path outside the caller's own roots; `agent_provider_*` is admin-only; per-account bridge cap |
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
- Cloudflare Access is no longer the front door. It is kept in front of `/admin`
  as the operator door, and there is no domain auto-approval knob anywhere —
  the only paths to `active` are email verification and an admin approval.

Admin API: `GET /api/admin/accounts`, `.../approve|deny|suspend|plan`,
`GET /api/admin/accounts/pending-count`. Self-service: `GET /api/me/account`.

---

## 3. Blocking gaps — do NOT open registration until these are closed

These are real and currently unaddressed. Listed in the order they matter.

1. **Plan entitlements are defined but not enforced.** `plans.py` declares
   storage/token limits per tier and `/api/billing/status` reports them, but
   nothing consults a plan. Today a `free` account and a `team` account get
   identical resources. Monetization is not functional until the plan is wired
   into the disk-quota check (storage) and the token budget.
2. **No LLM token metering.** `monthly_llm_tokens` is a number in a dataclass.
   Nothing counts tokens, nothing stops a user spending your provider balance.
   Every web turn spends the **host account's** provider balance — there is no
   per-user credential and no per-user budget. This is the single biggest
   unpriced cost in the service; it must be metered before you bill for copilot
   use or open registration.
3. **The CAD daemon is still one shared process.** The path jail stops user A
   naming user B's files, but a malformed or expensive RPC still degrades the
   daemon for *everyone*. True isolation needs a daemon per user, bound to a
   per-user socket.
4. **Host blast radius.** The gateway account `liveuser` is in `wheel` with
   `NOPASSWD: ALL` and holds SSH private keys to another LAN machine
   (`192.168.0.123`). Any RCE in the web app is root on the box and a pivot into
   the house. `deploy/harden_host.sh` fixes this; it has not been applied.
5. **Live credentials in the repo.** `Webservice/Cloudflare-info.txt` holds a
   Cloudflare API token (verified still ACTIVE) and R2 access keys. The file is
   gitignored now, but the values are in git history from commit `84963165`. The
   repo is currently private. **Rotate both** — see DEPLOYMENT.md.

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
app.chudware.com/admin          -> Cloudflare Access (SAME app, same AUD)
```

What was applied, in the order it has to happen:

1. `app.chudware.com` created as a proxied CNAME to
   `<tunnel-id>.cfargotunnel.com` and added to the tunnel ingress → `:8300`.
2. Gateway deployed (prod was 18 commits behind on `8571334f`). This is what
   made public signup possible at all: the older commit had no `PUBLIC_PATHS`,
   so `/signin` and `/signup` answered `401 missing Cf-Access-Jwt-Assertion`
   and a visitor could not reach a sign-up form by any route.
3. `CHUDWARE_WEB_PUBLIC_URL` repointed to `https://app.chudware.com`. It builds
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
7. Access scoped to `app.chudware.com/admin` on the *existing* app, so the AUD
   is unchanged. A second app would mint a different AUD and the gateway
   validates exactly one `CHUDWARE_WEB_CF_AUD`.

Verified from outside the LAN, anonymously, 28/28 checks: every marketing route
200 with no Access redirect; `/signin`, `/signup` and `/healthz` 200 on the app
host; `/`, `/workspace/list`, `/bucket`, `/api/me`, `/api/sessions`,
`/api/provider-keys`, `/api/admin/*` and `/static/admin.js` all 401 to an
anonymous caller; `/admin` redirects to the Access login; and a real password
sign-in returns a `chudware_sid` session that reports `role: admin`.

Use curl, not Python `urllib`, to re-run any of that. Cloudflare's bot
protection 403s the default `Python-urllib` user agent, which is
indistinguishable from a genuine authorization failure and will send you
chasing a security regression that is not there.

### Still open here

- **Prices.** Every figure was placeholder filler, so the pricing page now
  states commercial-preview status and publishes no numbers. Ratify the tier
  shape before putting figures back.
- **Stripe webhook bypass.** Not needed yet: Stripe is unconfigured, and
  `/api/billing/webhook` is in `PUBLIC_PATHS` so it is not gated by the app.
  It is also outside the `/admin` Access scope, so no edge bypass is required
  unless Access is ever widened back to the whole host.
- **`cad.scootware.us`** is already gone from Access and the tunnel ingress.
- **Watch the queue.** Every registration is created `pending`; only email
  verification (which starts the trial) or an admin approval makes it `active`.
  `GET /api/admin/accounts/pending-count`.

## 6. Stripe setup

Environment (read at call time; no restart needed to rotate):

```
CHUDWARE_WEB_STRIPE_SECRET_KEY=sk_live_...
CHUDWARE_WEB_STRIPE_WEBHOOK_SECRET=whsec_...      # for THIS endpoint
CHUDWARE_WEB_STRIPE_PRICE_PRO=price_...
CHUDWARE_WEB_STRIPE_PRICE_TEAM=price_...
CHUDWARE_WEB_STRIPE_SUCCESS_URL=https://app.chudware.com/?billing=success
CHUDWARE_WEB_STRIPE_CANCEL_URL=https://app.chudware.com/?billing=cancel
```

`stripe` is not yet in `requirements.txt` — add it pinned when you enable
billing. Until the key is set, the billing routes return 503 and the gateway
boots normally.

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
Pages project `chudware-site`). No DNS was changed — `chudware.com` still goes
to the gateway behind Access. The apex cutover is §5.

#### Provider-key sharing boundary

`CHUDWARE_WEB_SHARE_PROVIDER_KEYS` is now an enforced financial and credential
boundary. It defaults to `0`, and public or otherwise multi-tenant deployments
must leave it there. At `0`, `/engine` does not read or inject credentials from
the host `config.json` or `~/chudware-web-data/ai_providers.env`; this applies
equally to customers, administrators, and the operator's own web account.
For `key_source: "byok"`, the gateway first verifies that this account has a
validated credential for the selected provider; the worker decrypts that
account-scoped credential server-side and the turn bypasses the credit ledger.
Selecting another provider without an attached key does not reuse it: an
account with `ai_grant` takes the managed pool path, while an account without
either a matching key or a grant is refused before the shared daemon. It never
falls back to the operator's ambient profile.

Managed access is separate from host-profile sharing. An account with
`ai_grant`, an eligible cloud plan, and a positive credit balance is asserted
server-side as `key_source: "pool"`; the gateway reserves its estimated cost,
writes the credit-ledger spend row, and only then forwards the turn. Zero
balance is a hard refusal. Administrators receive no bypass: they need either
their own validated provider key or the same pooled grant and credits.

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
   `/app` is browser-native and never had a podman seat. Isolation belongs to
   `_inject_identity` + `path_violation()`, not to seat ownership. (The seat
   requirement, and the whole container product, were removed 2026-08-10; the
   lesson is why `/engine` authorizes on identity alone today.)
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

- **No code path can create an admin.** `accounts.py` only ever *reads* `role`;
  nothing writes it. `users.yaml` — which the runbooks and the
  `chudware-webservice-multitenant` skill both describe as the allowlist and
  role source, imported idempotently at startup — is **dead**: no module reads
  it and `config.py` has no `users_file` setting at all, so
  `CHUDWARE_WEB_USERS_FILE` in `gateway.env` is dead env. Production had
  **zero** admins and the owner's `role: admin` line had no effect; it was set
  with a direct `UPDATE` to unblock the cutover. Either restore an import or
  give `accounts.py` a `set_role` reachable from the admin API, and delete
  whichever mechanism loses.
- **`CHUDWARE_WEB_SSO_SHARED_SECRET` is dead env too.** The HTTP-Basic-on-
  loopback handoff it belonged to is gone from `auth.py` (no `Basic` handling
  anywhere), and there is no oauth2-proxy unit. The deploy runbook's
  "verify the authenticated surface with `curl -u email:$SECRET`" recipe
  therefore cannot work — it returns `401 missing Cf-Access-Jwt-Assertion` for
  every path and reads as a catastrophic failure. Use
  `python -m authn ops-session` (§8) instead, and fix the runbook.
- **`chudware-monitor.service` is failing every 2 minutes**, exiting 2 with
  `status publish requires R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_ENDPOINT`.
  That is the RAM/disk/GPU/session reaper. It is not cosmetic: this host shares
  `/` with a self-hosted Actions runner whose cache filled the disk on
  2026-08-10 and took Stalwart's RocksDB down with `No space left on device`.
- **The Cloudflare API token and R2 keys in `Webservice/Cloudflare-info.txt` are
  compromised** — the file says so itself; they were committed in `84963165`
  and are still the live credentials. Rotate them.
- **No Google OAuth in prod.** With Access no longer fronting the host, a user
  who cannot receive mail has no self-service recovery at all: no OAuth, and
  password reset needs the transport in §9. Configuring OAuth would give a mail-
  independent way in.
