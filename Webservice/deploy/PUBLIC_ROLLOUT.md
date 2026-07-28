# Public rollout — security state and cutover runbook

Written 2026-07-28 after a full audit of the gateway, the static front end, and
the `livingroom-pc` host. Read this before widening access to anyone.

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
| CRITICAL | `/stream/<sid>/<path>` used `urljoin`, so `path` could replace the upstream host — an authenticated user could make the gateway fetch `http://192.168.0.1/` inside the home LAN | `build_upstream_url()` rejects any path with a scheme/netloc/control char; result can only live under the fixed base |
| CRITICAL | `/workspace/*` and `/api/bucket/*` were one shared tree — any user could read, overwrite or `rmtree` any other user's files | Per-account roots via `user_slug(email)`; containment checked on the fully-resolved path; final-component symlinks rejected; `?scope=shared` is admin-only |
| CRITICAL | `/engine` piped any user into the one shared CAD daemon, which resolves whatever absolute path it is handed | Caller must own a live session; `path_violation()` rejects any RPC naming a path outside the caller's own roots |
| CRITICAL | Host AI provider keys (Firecrawl/Gemini/Moonshot/OpenRouter) were copied into every session container's environment | Withheld by default; opt in with `CHUDWARE_WEB_SHARE_PROVIDER_KEYS=1` |
| CRITICAL | Admin browser terminal = interactive shell as the gateway account | Off by default (`CHUDWARE_WEB_TERMINAL_ENABLED=0`); per-admin session ownership; opaque ids; session cap; idle timeout; output throttle; `MemoryMax`/`CPUQuota` on the scope |
| HIGH | Identity headers (`Authorization`, `Cf-Access-Jwt-Assertion`, `X-Forwarded-*`) forwarded to the proxy upstream | Scrubbed before forwarding |
| HIGH | No CSP anywhere | CSP + HSTS + Permissions-Policy + COOP added; verified zero inline scripts in served HTML |
| HIGH | Uploads had no size cap — one request could fill the host disk | `max_upload_bytes` enforced on `Content-Length` **and** mid-stream; partial files unlinked |
| HIGH | Unpinned deps allowing known-CVE versions of `PyJWT` / `python-multipart` | Fully `==`-pinned |
| HIGH | `users.yaml` with real admin emails committed to the repo | Replaced with a template; runtime list lives outside the repo |
| MEDIUM | Session ids were 48 bits (`uuid4().hex[:12]`) | `secrets.token_urlsafe(24)` |
| MEDIUM | `/healthzFOO` was unauthenticated (prefix match) | Exact-match + `/` boundary |
| MEDIUM | `/healthz` leaked `dev_mode`, RAM, VRAM, session counts to anonymous callers | Anonymous callers get `{"ok": true}` |
| MEDIUM | `/static/admin.*` readable by any authenticated user | Admin-gated |
| MEDIUM | Chat WebSocket had a `?email=` identity fallback | Deleted |
| MEDIUM | `_valid_project` / `admin_browse` accepted any absolute host path | Confined to `CHUDWARE_WEB_PROJECT_ROOTS` |
| MEDIUM | No per-user rate limits or quotas anywhere | `limits.py`: per-account rate limiters, upload caps, disk quota, session caps, hard session lifetime |
| LOW | `admin_logs` returned raw journal output that can contain tokens | Redaction pass for JWTs, `cfat_`, `Bearer`, `*_KEY=`/`*_SECRET=`/`*_TOKEN=` |
| LOW | Release filenames could inject into `Content-Disposition` | `[A-Za-z0-9._-]{1,128}` only |

Verification: `Webservice/gateway/.venv/bin/python -m pytest tests/ -q` → **215
passed, 2 skipped**. Each fix carries a test that fails when the vulnerability is
reintroduced (mutation-checked, not just written).

---

## 2. What changed about accounts

Authorization moved from a hand-edited YAML allowlist to an account store
(`gateway/accounts.py`, SQLite at `<data_root>/accounts.db`).

- An unknown identity that authenticates is **registered as `pending`**, not
  refused — a stranger lands in an approval queue instead of a dead end.
- Only `status == "active"` yields a session. Role comes from the database; a
  self-registered account can never be `admin`.
- Existing `users.yaml` entries are imported at startup **and** adopted lazily on
  first request, so current teammates keep their role. An admin `denied` or
  `suspended` decision always outranks the YAML file.
- `CHUDWARE_WEB_AUTO_APPROVE_DOMAINS` defaults to `ua.edu` (matching the old
  hard-coded behaviour). **Set it to an empty string to close that door** and
  route every new identity through the queue.

Admin API: `GET /api/admin/accounts`, `.../approve|deny|suspend|plan`,
`GET /api/admin/accounts/pending-count`. Self-service: `GET /api/me/account`.

---

## 3. Blocking gaps — do NOT open registration until these are closed

These are real and currently unaddressed. Listed in the order they matter.

1. **Plan entitlements are defined but not enforced.** `plans.py` declares
   session/storage/token limits per tier and `/api/billing/status` reports them,
   but `sessions.py` never consults a plan. Today a `free` account and a `team`
   account get identical resources. Monetization is not functional until the
   plan is wired into `manager.start()` (concurrency + heavy seat) and into the
   quota check (storage).
2. **No LLM token metering.** `monthly_llm_tokens` is a number in a dataclass.
   Nothing counts tokens, nothing stops a user spending your provider balance.
   With shared host keys now withheld by default this is contained, but the
   moment you re-enable them or bill for copilot use, you need real metering.
3. **The CAD daemon is still one shared process.** The path jail stops user A
   naming user B's files, but a malformed or expensive RPC still degrades the
   daemon for *everyone*. True isolation needs a daemon per session (or per
   user), bound to a per-user socket.
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

## 5. Cutover to a public homepage

Decision taken: the marketing site is **static, on Cloudflare Pages, off the
home box entirely.** Public and bot traffic must never reach the origin. The app
moves to `app.chudware.com` behind Access.

Today all three hostnames (`chudware.com`, `www.chudware.com`,
`cad.scootware.us`) tunnel to `127.0.0.1:8300` and Cloudflare Access gates the
whole origin — so there is currently no way to serve an unauthenticated page
without punching a hole in that gate. Hence the split.

**Nothing below has been applied. These are the steps; you run them.**

1. **Deploy the site.** From `Webservice/public-site/`:
   `wrangler pages deploy . --project-name chudware-site`
   (dashboard alternative in that directory's README).
2. **Set the real prices.** Every figure in `pricing.html` is a placeholder and
   is labelled as such on the page. Replace them, then delete the `ph-tag` chips
   and the `.notice` banner. The tier *shape* (what each tier includes) was
   invented as layout filler — ratify it before publishing.
3. **Point the apex at Pages.** `chudware.com` + `www` → the Pages project.
4. **Add the app hostname.** Create `app.chudware.com` as a proxied CNAME to
   `<tunnel-id>.cfargotunnel.com` and add it to the tunnel ingress → `127.0.0.1:8300`.
5. **Update the Access app.** Add `app.chudware.com` to the *existing* self-hosted
   app's `self_hosted_domains`. Do **not** create a second app — all domains share
   one AUD and the gateway validates a single `CHUDWARE_WEB_CF_AUD`.
   Remove `chudware.com`/`www` from it once the apex serves Pages.
6. **Bypass the Stripe webhook.** `/api/billing/webhook` cannot present an Access
   JWT. Add an Access *bypass* policy for that exact path, or Stripe events will
   be rejected at the edge. It is authenticated by HMAC signature in the app.
7. **Retire or re-point `cad.scootware.us`.**
8. **Decide on auto-approval.** Set `CHUDWARE_WEB_AUTO_APPROVE_DOMAINS=` (empty)
   when you want every new identity to queue.

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
   `21e2356c`). Verify `/healthz` and one real session.
4. Publish the Pages site and do the DNS/Access cutover (§5).
5. Wire plan entitlements into `sessions.py` (§3.1) — this is the gate for
   taking money.
6. Add token metering (§3.2), then enable Stripe.
7. Only then set `CHUDWARE_WEB_AUTO_APPROVE_DOMAINS=` and open registration.

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

### Three regressions this pass caused, and what they teach

All three were invisible to the test suite and to `curl`, and all three only
showed up by driving the real thing. Worth remembering when hardening further:

1. **CSP killed the WASM client.** Every asset returned 200; the page just
   never started. `script-src 'self'` blocks `WebAssembly.instantiate` without
   `'wasm-unsafe-eval'`, blob workers need `worker-src`, and the inline
   bootstrap script needed a hash. Fixed in `appserve.py`, which now derives
   inline-script hashes from the file on disk so a rebuild refreshes them.
2. **Requiring a container seat on `/engine` took the web client offline.**
   `/app` is browser-native and never has a podman seat. Isolation belongs to
   `_inject_identity` + `path_violation()`, not to seat ownership.
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
you can watch it. Log lands in `/tmp/chudware-webd-sync-wasm.log`. Note
`chudware-webd-sync.path` only fires on a rebuilt `chudware-hostd` binary, so a
source-only pull will never trigger it.

**2. Two switches are ON for convenience and must be flipped before public
registration** (both commented in `~/chudware-web-data/gateway.env`):
`CHUDWARE_WEB_TERMINAL_ENABLED=1` and `CHUDWARE_WEB_SHARE_PROVIDER_KEYS=1`.

**3. The blockers in §3 are unchanged** — plan entitlements still are not
enforced (`sessions.py` never reads a plan), there is no token metering, the
CAD daemon is still one shared process, `liveuser` still has NOPASSWD sudo plus
LAN SSH keys, and the Cloudflare API token in git history is still live.

### Operating notes

- Deploy = pull + `systemctl --user restart chudware-gateway`. As root the user
  bus needs: `setpriv --reuid=1000 --regid=1000 --init-groups env
  XDG_RUNTIME_DIR=/run/user/1000
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus systemctl --user ...`
- **Warn before restarting** — it drops every live terminal and engine socket.
- To exercise authenticated endpoints without a browser, use the SSO shared
  secret over loopback: `curl -u "you@crimson.ua.edu:$CHUDWARE_WEB_SSO_SHARED_SECRET"`.
  Browsers will NOT attach that header to a WebSocket handshake, so a WS 403 in
  a headless-browser test is usually the harness, not the server — confirm with
  a real WS client before chasing it.
- Rollback commit from before this pass: `~/chudware-web-data/ROLLBACK_COMMIT`.
  Backups: `~/chudware-web-backups/` (local + unencrypted; see §4).
