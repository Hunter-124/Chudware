# Chudware WASM public demo runbook

**When:** ~40 min stakeholder demo  
**Client:** browser WASM (`~/chudware-web/app/`) → shared `chudware-hostd` via gateway  
**Do not** restart hostd mid-demo. Gateway restart is required once before showtime (see Blocker).

---

## Canonical URL

Use this exact URL (bookmark it):

```
https://app.chudware.com/app/
```

Notes:

- Local fallback (no sign-in):  
  `http://127.0.0.1:8399/app/?dev_user=haedmonds69@gmail.com`  
  (dev uvicorn with `CHUDWARE_WEB_DEV=1` only — not the public path.)

---

## Login (self-serve accounts, in the gateway)

1. Open `https://app.chudware.com/app/` in **Chrome** (preferred) or Firefox.
2. Sign-in is the gateway's own. An unauthenticated browser is sent to the
   hosted sign-in page; an authenticated but non-`active` account gets a 403
   status page whose copy depends on its state (pending / denied / suspended /
   trial_expired).
3. So seed every attendee **before** showtime:
   ```bash
   curl -sS -X POST https://app.chudware.com/api/auth/register \
     -H 'content-type: application/json' \
     -d '{"email":"guest@crimson.ua.edu","password":"<their password>"}'
   ```
   They click the emailed link (`GET /api/auth/verify?token=…`): it activates the account
   on a 14-day trial and 303-redirects to `/` with the `chudware_sid` cookie already set,
   so the first visit needs no separate login step.
   - Hunter admin accounts: `haedmonds69@gmail.com`, `haedmonds@crimson.ua.edu`
   - Anyone stuck at `pending` (unclicked link) can be approved from `/admin`.
4. Returning users `POST /api/auth/login`, which sets the same `chudware_sid` cookie —
   a server-side session row, TTL `CHUDWARE_WEB_SESSION_TTL_DAYS` (30 days).
5. Cloudflare Access only challenges you on `/admin`; the demo path never sees it.

---

## What “good” looks like

| Checkpoint | Expected |
|---|---|
| Page load | Dark shell; `#status` moves past “Loading Chudware WASM…” |
| Isolation | DevTools console: `crossOriginIsolated === true` (required for pthreads / SharedArrayBuffer) |
| UI | Full ImGui chrome: **ribbon** + **FeatureManager** tree visible |
| Daemon | Status bar / HUD shows **Ready** and **daemon connected** (WS to `/engine` on same origin) |
| Sanity action | Orbit viewport; open a part from workspace if prompted |

If `crossOriginIsolated` is **false**, pthread WASM will fail or hang — see Fallback.

---

## SharedArrayBuffer / COOP+COEP dependency

The WASM client is built with **`-pthread`**. Browsers only expose `SharedArrayBuffer` when the document is cross-origin isolated:

- Response headers on **every** `/app` asset must include:
  - `Cross-Origin-Opener-Policy: same-origin`
  - `Cross-Origin-Embedder-Policy: require-corp`
- `.wasm` must be served as `Content-Type: application/wasm`

Gateway implementation: `Webservice/gateway/appserve.py` (`_coop_headers` + MIME overrides).  
Cloudflare Tunnel is expected **not** to strip these origin headers on authenticated asset responses.

---

## ONE fallback if COOP/COEP is stripped by the tunnel

If after sign-in DevTools shows `crossOriginIsolated === false` (headers missing on HTML/JS/WASM):

1. Vendor **`coi-serviceworker`** into `~/chudware-web/app/` and register it from `index.html` so the service worker rewrites responses to inject COOP/COEP (standard COI polyfill path).
2. Hard-reload (or unregister old SW) once.
3. Re-check `crossOriginIsolated === true`, then proceed with demo.

This is the **only** planned isolation fallback; do not try random header hacks mid-demo. Prefer fixing gateway/tunnel headers over the SW path when time allows.

---

## Pre-demo operator checklist

```bash
# 1) REQUIRED if gateway predates appserve (see verification notes)
systemctl --user restart chudware-gateway

# 2) Confirm services
systemctl --user is-active chudware-gateway chudware-cloudflared chudware-hostd

# 3) Local auth probe of MIME + isolation headers (dev server already good;
#    after prod restart, use a real CF JWT or temporary local smoke)
curl -sI -X GET 'http://127.0.0.1:8399/app/?dev_user=haedmonds69@gmail.com' | grep -iE 'HTTP/|cross-origin|content-type'
curl -sI -X GET 'http://127.0.0.1:8399/app/Chudware.wasm?dev_user=haedmonds69@gmail.com' | grep -iE 'HTTP/|cross-origin|content-type'

# Expect on 200s:
#   cross-origin-opener-policy: same-origin
#   cross-origin-embedder-policy: require-corp
#   content-type: application/wasm   (for Chudware.wasm)

# 4) Public path is served by the gateway, and refuses anonymous callers
curl -sI https://app.chudware.com/app/ | head -15
# Expect a redirect to the gateway sign-in page — NOT to bruh234324.cloudflareaccess.com
```

Bundle on disk (served from `CHUDWARE_WEB_APP_DIR` default `~/chudware-web/app/`):

- `index.html` / `Chudware.html`
- `Chudware.js`
- `Chudware.wasm` (~9.2 MiB)
- `Chudware.data` (~2.5 MiB)

---

## Architecture (one line)

Browser → Cloudflare Tunnel (`chudware-cloudflared`) → `chudware-gateway` `:8300` (session-cookie auth) → static `/app` + WS `/engine` → shared `chudware-hostd` (`chudware-hostd`).

---

## Talk track (30s)

“This is the real Chudware GUI compiled to WASM, not a VNC stream. After signing in, the browser loads the same ribbon and FeatureManager as desktop, talks to Hunter’s shared engine daemon over WebSocket, and stays cross-origin isolated so multi-threaded WASM can use SharedArrayBuffer.”
