# Billet WASM public demo runbook

**When:** ~40 min stakeholder demo  
**Client:** browser WASM (`~/billet-web/app/`) → shared `aicad-hostd` via gateway  
**Do not** restart hostd mid-demo. Gateway restart is required once before showtime (see Blocker).

---

## Canonical URL

Use this exact URL (bookmark it):

```
https://cad.scootware.us/app/
```

Notes:

- Apex `https://scootware.us/app/` **301s to** `https://cad.scootware.us/` (root, **not** `/app`). Do not send stakeholders to apex.
- Local fallback (no CF Access):  
  `http://127.0.0.1:8399/app/?dev_user=haedmonds69@gmail.com`  
  (dev uvicorn with `BILLET_WEB_DEV=1` only — not the public path.)

---

## Login (Cloudflare Access)

1. Open `https://cad.scootware.us/app/` in **Chrome** (preferred) or Firefox.
2. Cloudflare Access interstitial: **Sign in · Cloudflare Access** for host `cad.scootware.us`.
3. Authenticate with an allowlisted email (OTP and/or Google IdP if configured).
   - Hunter admin emails: `haedmonds69@gmail.com`, `haedmonds@crimson.ua.edu`
   - Allowlist lives in `~/billet-web-data/users.yaml` and CF Access policy `billet-users`.
4. After Access sets the `CF_Authorization` cookie, the gateway validates `Cf-Access-Jwt-Assertion` and serves the WASM shell.
5. Session duration is Access app default (**24h**). Re-auth only if cookie expired.

---

## What “good” looks like

| Checkpoint | Expected |
|---|---|
| Page load | Dark shell; `#status` moves past “Loading Billet WASM…” |
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

If after Access login DevTools shows `crossOriginIsolated === false` (headers missing on HTML/JS/WASM):

1. Vendor **`coi-serviceworker`** into `~/billet-web/app/` and register it from `index.html` so the service worker rewrites responses to inject COOP/COEP (standard COI polyfill path).
2. Hard-reload (or unregister old SW) once.
3. Re-check `crossOriginIsolated === true`, then proceed with demo.

This is the **only** planned isolation fallback; do not try random header hacks mid-demo. Prefer fixing gateway/tunnel headers over the SW path when time allows.

---

## Pre-demo operator checklist

```bash
# 1) REQUIRED if gateway predates appserve (see verification notes)
systemctl --user restart billet-gateway

# 2) Confirm services
systemctl --user is-active billet-gateway billet-cloudflared billet-hostd

# 3) Local auth probe of MIME + isolation headers (dev server already good;
#    after prod restart, use a real CF JWT or temporary local smoke)
curl -sI -X GET 'http://127.0.0.1:8399/app/?dev_user=haedmonds69@gmail.com' | grep -iE 'HTTP/|cross-origin|content-type'
curl -sI -X GET 'http://127.0.0.1:8399/app/Billet.wasm?dev_user=haedmonds69@gmail.com' | grep -iE 'HTTP/|cross-origin|content-type'

# Expect on 200s:
#   cross-origin-opener-policy: same-origin
#   cross-origin-embedder-policy: require-corp
#   content-type: application/wasm   (for Billet.wasm)

# 4) Public path reaches Access (unauthenticated)
curl -sI https://cad.scootware.us/app/ | head -15
# Expect 302 → bruh234324.cloudflareaccess.com/.../login/cad.scootware.us
```

Bundle on disk (served from `BILLET_WEB_APP_DIR` default `~/billet-web/app/`):

- `index.html` / `Billet.html`
- `Billet.js`
- `Billet.wasm` (~9.2 MiB)
- `Billet.data` (~2.5 MiB)

---

## Architecture (one line)

Browser → Cloudflare Access → tunnel (`billet-cloudflared`) → `billet-gateway` `:8300` → static `/app` + WS `/engine` → shared `billet-hostd` (`aicad-hostd`).

---

## Talk track (30s)

“This is the real Billet GUI compiled to WASM, not a VNC stream. After Cloudflare Access, the browser loads the same ribbon and FeatureManager as desktop, talks to Hunter’s shared engine daemon over WebSocket, and stays cross-origin isolated so multi-threaded WASM can use SharedArrayBuffer.”
