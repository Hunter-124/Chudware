# Auth gateway research — Chudware web service (home host)

**Scope:** External research only (no repo edits / installs).  
**Goal:** Auth for a self-hosted multi-user Chudware web service (~3–6 concurrent users) on Fedora 44 + rootless Podman, with Microsoft Entra (University of Alabama / ua.edu) + whitelisted Google accounts, plus tunnel/exposure and streaming constraints.  
**Date:** 2026-07-14.

---

## 1) Option A — Cloudflare Tunnel (`cloudflared`) + Cloudflare Access (Zero Trust)

### Free-tier seats / limits

- Cloudflare for Teams / Zero Trust **Free** was announced as protecting **up to 50 users at no cost** ([blog.cloudflare.com/teams-plans](https://blog.cloudflare.com/teams-plans/)).
- Seats are consumed by **authentication events** (Access login / App Launcher / device Gateway activity). Inactive users can be removed manually or via seat expiration (1 month–1 year). Exceeding seats blocks additional logins until seats free or plan upgrades ([seat management docs](https://developers.cloudflare.com/cloudflare-one/team-and-resources/users/seat-management/)).
- Community/third-party summaries (2025–2026) still cite **50 free seats**, then PAYG ~**$7/user/month** for the whole org once over free. Official marketing pages are plan-sales heavy; treat **50** as the operational free budget for this design (well above 6 users).
- Access feature limits (apps, IdPs, emails/rule, etc.) are generous for this scale ([account limits](https://developers.cloudflare.com/cloudflare-one/account-limits/)): e.g. 50 IdPs, 1000 emails per rule, 500 applications.

**Verdict on free tier:** Fine for 6–50 users. No need to pay for Access seats at UA lab scale.

### Identity: Microsoft Entra ID + Google

#### Google (easy, whitelist-friendly)

- Official IdP path: **Google** (not only Google Workspace). Any Google account can authenticate if Access policy allows ([Google IdP docs](https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/google/)).
- Setup: GCP project → OAuth consent screen **External** → OAuth Web client → redirect  
  `https://<team-name>.cloudflareaccess.com/cdn-cgi/access/callback`  
  + JS origin `https://<team-name>.cloudflareaccess.com`.
- **Email whitelist policies** (not “Google whitelist” alone): Access app policy **Allow** with **Include → Emails** (exact list) and/or **Emails ending in** (`@gmail.com` only if you want whole domain — prefer exact emails for personal Google).
- Multiple IdPs allowed; policy can **Include** emails from either Google or Entra ([policies](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/)).

Example policy shape:

| Action | Rule type | Selector | Value |
|--------|-----------|----------|--------|
| Allow  | Include   | Emails   | `alice@ua.edu`, `bob@gmail.com`, … |

Or domain for UA + explicit Google emails:

| Action | Rule type | Selector | Value |
|--------|-----------|----------|--------|
| Allow  | Include   | Emails ending in | `@ua.edu` |
|        | Include   | Emails   | `friend@gmail.com`, `collab@company.com` |

#### Microsoft Entra ID — critical constraint for university tenants

Official Cloudflare Entra integration **requires an app registration in some Entra tenant you control**, with:

- Application (client) ID  
- Directory (**tenant**) ID  
- Client secret  
- Redirect: `https://<team-name>.cloudflareaccess.com/cdn-cgi/access/callback`  
- Graph delegated permissions including `openid`, `profile`, `email`, `User.Read`, and (for groups) `Directory.Read.All` / `GroupMember.Read.All` + **admin consent in that tenant**  
  ([Entra IdP docs](https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/entra-id/))

**What this means for University of Alabama students:**

1. Students **typically cannot** create enterprise app registrations or grant admin consent in the **ua.edu** tenant. You cannot “register Cloudflare Access inside UA Entra” without UA IT.
2. Cloudflare’s Entra connector is **tenant-scoped** (you paste a specific Directory/tenant ID). Community consensus: Access Entra IdP is **not multi-tenant /common** the way a SaaS multi-tenant app is ([community thread](https://community.cloudflare.com/t/auth-against-azure-ad-multi-tenant/275689)). So: **one Entra tenant per Cloudflare IdP entry**, not “any Microsoft work account.”
3. Therefore **generic “Microsoft login works for any Entra account without any app reg” is FALSE for Cloudflare Access’s Entra IdP.** Access does not magically use Microsoft’s first-party multi-tenant consumer path for arbitrary university tenants.

**Workable Entra strategies without UA admin:**

| Approach | Works? | Notes |
|----------|--------|-------|
| Register Access IdP app **in UA tenant** | Needs UA IT | Best if IT cooperates; full group claims/SCIM possible (SCIM needs Entra P1/P2). |
| Register app in **your personal free Entra tenant**, single-tenant | Only **your** tenant’s users | UA students cannot sign in as their ua.edu identities. |
| Register **multi-tenant** app in personal Entra + hope CF uses `/common` | **Not supported by CF Access Entra IdP field model** | CF requires Directory ID; not documented for multi-tenant external logins. |
| Don’t use Entra IdP; use **One-time PIN / email OTP** to `@ua.edu` | Works without Entra | Weaker than OAuth; still Access-gated. |
| Use **Google** for everyone who has Gmail; UA users use **whatever IdP IT allows** | Partial | Many universities federate Google Workspace or Microsoft only. |
| **oauth2-proxy / custom OIDC multi-tenant** instead of CF Entra IdP | See §2 | Can use `/common` + multi-tenant app; university **user-consent policies** still bite. |

**Bottom line:** Cloudflare Access is excellent for Google + email allowlists. For **ua.edu Entra without UA IT**, Access’s Microsoft Entra IdP **does not solve** “students log in with school Microsoft accounts with zero admin involvement.” You need either UA IT app registration **or** a multi-tenant OIDC stack outside Access’s Entra connector **or** a non-Entra login (OTP / Google).

### JWT: `Cf-Access-Jwt-Assertion` validation

Official ([Validate JWTs](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/)):

- Header: **`Cf-Access-Jwt-Assertion`** (prefer over `CF_Authorization` cookie).
- JWKS: `https://<team-name>.cloudflareaccess.com/cdn-cgi/access/certs`  
  (JWK `keys` + PEM `public_certs`; rotate ~every **6 weeks**, previous key valid **7 days** — always fetch remotely, match `kid`).
- Claims to enforce:
  - `iss` = `https://<team-name>.cloudflareaccess.com`
  - `aud` = **Application Audience (AUD) tag** (from Access app → Additional settings; stable until app recreated)
  - `exp` / signature RS256
- Identity claims typically include `email` (use for session mapping).
- **If the origin is only reachable via Cloudflare Tunnel + Access**, CF terminates Access at the edge; origin still **should** validate JWT if the origin is ever reachable another way. With tunnel-only bind to localhost/pod network, risk is lower but defense-in-depth remains recommended.

### WebSocket / streaming / WebRTC

- **Published tunnel routes** for browsers are **HTTP/HTTPS** (and unix sockets). Non-HTTP TCP/SSH/RDP/SMB require **client-side `cloudflared`**, not raw browser ([protocols](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/protocols/)).
- **WebSockets over HTTPS** (browser `wss://` upgrade) are supported on Cloudflare HTTP proxying generally ([WebSockets network docs](https://developers.cloudflare.com/network/websockets/)); community + CF guidance: no special tunnel flag for WS if origin is HTTP(S).
- **Public UDP / native WebRTC media through Tunnel public hostname: NO.**  
  - No public UDP endpoint on Tunnel.  
  - Spectrum UDP is enterprise-class product.  
  - Private-network UDP needs WARP clients on both sides — not browser guests.  
  - WebRTC: signaling over WS/HTTPS can ride the tunnel; **media needs host/STUN/TURN publicly reachable (UDP)** or fall back to **TCP/TLS data channels / pure WebSocket streaming** (higher latency).
- Bandwidth: Free tunnel has **no hard documented bandwidth cap** for general proxying; practical caveats:
  - Cloudflare free zone **100 MB upload** limit applies when traffic is proxied as a normal CF zone feature (community reports this interacting with tunnels).
  - Free-tier long-lived connections often discussed as **~100s of minutes to 24h** class limits depending on product path — plan reconnect/backoff for streaming sessions.
  - Video-ish streams over **WS/TCP** through free edge will work for low concurrent users but expect **extra latency vs LAN**, no QoS, and possible idle timeouts. For 3–6 CAD stream sessions this is usually acceptable if codec/framerate are modest; not a live-production CDN.

**Streaming design implication for Chudware:** Prefer **WebSocket or WebTransport-over-HTTP** (or fMP4/WebCodecs over HTTPS) rather than browser WebRTC media unless you run a separate public TURN (UDP) outside free Tunnel.

### Option A pros/cons (summary)

**Pros**

- No open inbound ports on home ISP; TLS + DDoS edge free.
- Access policies + Google IdP + email lists are first-class.
- Free ≤50 seats.
- JWT contract is clear for a Python gateway.

**Cons**

- **Entra multi-tenant / UA without IT is poorly supported** by CF Entra IdP.
- No public UDP / poor WebRTC media.
- Edge latency + free-tier connection/upload caveats for heavy streams.
- Dashboard-heavy setup (manual).

---

## 2) Option B — Self-managed: oauth2-proxy + Caddy (TLS) + port-forward / alt tunnel

### Shape

```
Internet → (router port 443 or alternate tunnel) → Caddy (TLS) → oauth2-proxy → Chudware gateway / session router
```

- **Google OAuth:** personal GCP project, External consent, redirect `https://app.example/oauth2/callback`. Email restriction via oauth2-proxy `--email-domain` / `--authenticated-emails-file`.
- **Microsoft:** oauth2-proxy **`entra-id`** provider (v7.15 docs) explicitly supports multi-tenant:

```toml
provider = "entra-id"
oidc_issuer_url = "https://login.microsoftonline.com/common/v2.0"
insecure_oidc_skip_issuer_verification = true
client_id = "..."
client_secret = "..."
scope = "openid profile email"
# optional harden:
entra_id_allowed_tenants = ["<ua-tenant-guid-if-known>", "..."]
email_domains = ["ua.edu"]  # plus authenticated-emails-file for Gmail allowlist
```

Source: [oauth2-proxy Microsoft Entra ID](https://oauth2-proxy.github.io/oauth2-proxy/configuration/providers/ms_entra_id/).

App registration in **your** free Entra tenant:

- Supported account types: **Accounts in any Microsoft Entra directory (multitenant)**  
  ([single vs multi-tenant](https://learn.microsoft.com/en-us/entra/identity-platform/single-and-multi-tenant-apps)).
- Minimal scopes: `openid` (+ `profile` `email` for personal MSA / email claims). Avoid `Directory.Read.All` so you stay in **user-consentable** space.
- Redirect URI to oauth2-proxy callback.

### Will UA users login without ua.edu admin consent?

**Maybe — not guaranteed.**

- Multi-tenant apps can authenticate users from other tenants **if that foreign tenant’s consent policy allows user consent** for the requested permissions.
- Default Microsoft guidance: users can consent to low-impact delegated permissions (`openid`, `profile`, `email`, `offline_access`, `User.Read`) unless tenant admin **disables user consent** or restricts to verified publishers only ([user consent overview](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/user-admin-consent-overview), [configure user consent](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/configure-user-consent)).
- **Universities frequently lock down user consent** (“Admin approval required”). UW and peers publish policies that refuse broad third-party apps. **UA may show “Need admin approval”** for any multi-tenant app — **only UA IT can clear that**, and they often won’t for a hobby/home CAD server.
- Even when user consent works, **first user from UA** creates a service principal in the UA tenant (enterprise app shadow). Security teams may later block it.
- **Personal Microsoft accounts** path differs; work/school ua.edu is Entra workforce.

**Practical expectation:** Treat multi-tenant Entra as **best-effort for ua.edu**. Success path without IT is **not contractually reliable**. Fallback: Google accounts, or OTP, or formal UA IT enterprise app.

### Port-forward + Caddy vs Tunnel

- Home ISP: open 443, dynamic DNS, TLS via Let’s Encrypt — full WebSocket control, optional UDP/TURN on another port if ISP allows.
- Risks: residential IP exposure, CGNAT, ToS, DDoS, cert renewals.
- Can combine: **oauth2-proxy origin** still behind **Cloudflare Tunnel** but **without** Access Entra — only tunnel for exposure, auth local. Or Tunnel + Access for Google-only.

### Option B pros/cons

**Pros**

- True multi-tenant Entra **possible** in software (unlike CF Access Entra IdP).
- Full control of cookies, WebSockets, timeouts, headers (`X-Forwarded-User` / `X-Auth-Request-Email`).
- No 50-seat product coupling.

**Cons**

- You own TLS, patching, secret rotation, abuse.
- UA admin-consent policy still a hard external dependency.
- oauth2-proxy is **one IdP config per process** historically awkward for “pick Google OR Entra” — often run two proxies or use a richer IdP (Authentik) or Caddy with multiple OAuth modules / custom gateway.

---

## 3) Option C — Keycloak / Authentik (full IdP)

| | Authentik | Keycloak |
|--|-----------|----------|
| RAM (small) | ~150–350 MB + Postgres (Redis removed ~2025.10 in reports) | Often **400 MB–2+ GB** even small |
| Ops | UI flows, outposts | Realms/clients/mappers — steeper |
| For 6 users | Usable but **extra moving parts** | Overkill |

**Verdict for 6 users:** **Too heavy** as the *primary* answer if Cloudflare Access or oauth2-proxy already covers login. Authentik is the less heavy of the two if you need a self-hosted IdP that brokers Google + Entra + invite-only users with one portal; Keycloak is unjustified at this scale unless you already run it.

---

## RECOMMENDATION (ranked)

### 1) **Winner: Cloudflare Tunnel + Cloudflare Access** (Google + email policies; Entra only if UA IT or you accept limits)

**Why (≤3 bullets):**

1. Best fit for **home host**: no inbound ports, free ≤50 seats, mature Access policies for **exact email allowlists**, dual IdP buttons (Google + optional Entra).
2. Clear **JWT** contract for a Python session gateway; WS streaming over HTTPS is OK; UDP/WebRTC media was never free-tunnel-friendly anyway.
3. Lowest ops load vs oauth2-proxy/Caddy/Keycloak on a workstation already running Podman CAD sessions.

**Entra reality check (deciding bullet):**  
Do **not** plan on “any ua.edu account works via CF Microsoft login with zero IT.” Plan:

- **Phase 1:** Google IdP + Access **Emails** include list (UA users can use Google if they have it, or you list personal emails).
- **Phase 2a:** If UA IT will register an enterprise app → add Entra IdP (single-tenant UA) + policy `@ua.edu`.
- **Phase 2b:** If Entra-without-IT is mandatory → pivot auth to **oauth2-proxy multi-tenant** (runner-up) and **test one ua.edu login early**; expect possible admin-approval wall.

### 2) oauth2-proxy + Caddy (+ optional Tunnel for exposure only)

Use when you need multi-tenant Entra `/common` or full control of streaming timeouts/headers. Still email-file allowlist for Google.

### 3) Authentik

Only if you want a polished login portal / invite flows / multiple apps SSO later. Skip Keycloak at N≈6.

---

## Winner setup — exact steps

### A. Hunter manual (dashboard / consoles)

1. **Cloudflare account** (already have) → Zero Trust / Cloudflare One onboarding → note **Team name** → `https://<team>.cloudflareaccess.com`.
2. **Domain** on Cloudflare (buy or transfer / CNAME setup) for `cad.example.com`.
3. **Tunnel:** Zero Trust → Networks → Tunnels → Create → install `cloudflared` on Fedora (Podman or host service) with tunnel token. Public hostname `cad.example.com` → `http://127.0.0.1:<gateway-port>` (or pod IP). **Do not** expose the port on the LAN firewall.
4. **Google OAuth (manual):**
   - [console.cloud.google.com](https://console.cloud.google.com/) → new project → OAuth consent **External** → app name / support email.
   - Create OAuth client **Web** → origins `https://<team>.cloudflareaccess.com` → redirect `https://<team>.cloudflareaccess.com/cdn-cgi/access/callback`.
   - Copy Client ID + Secret into Zero Trust → Integrations → Identity providers → **Google**.
   - Test IdP.
5. **Access application (manual):**
   - Access controls → Applications → Self-hosted → domain `cad.example.com`.
   - Policy **Allow**:
     - Include **Emails**: paste whitelist (UA + Google).
     - Optional second policy / same Include list for domain `@ua.edu` **only after** Entra IdP works.
   - Session duration as desired (e.g. 24h).
   - Copy **Application Audience (AUD)** tag.
6. **Entra (only if UA IT or personal-tenant experiment):**
   - Entra admin center → App registration → Web redirect to CF callback → client secret → Graph delegated perms → **admin consent in that tenant**.
   - Zero Trust → IdP → Azure AD / Entra → paste client id, secret, **directory id**.
   - Cannot skip Directory ID; multi-tenant-any-org is not the documented path.
7. **Seat hygiene:** Settings → remove inactive seats if testing creates junk users.

**Cannot automate without Hunter’s Cloudflare/Google/Entra credentials:** OAuth client creation, consent screens, Access policy email list, tunnel token binding, AUD copy, any UA IT tickets.

### B. Automatable in-repo / on host (later implementation; not this research phase)

- Install/run `cloudflared` via Quadlet/systemd user unit with token from env/secret.
- Python gateway middleware validating JWT (see snippet).
- Podman network so only `cloudflared` can reach the gateway.
- Config: `TEAM_DOMAIN`, `POLICY_AUD`, email→session mapping.
- Chat/stream services as plain HTTPS+WS backends behind same Access app (same cookie/JWT).

### C. Python gateway JWT validation guidance

```python
# Conceptual; use PyJWT + cryptography, or authlib, or jwt[crypto]
import time
import requests
import jwt
from jwt import PyJWKClient

TEAM_DOMAIN = "https://<team>.cloudflareaccess.com"  # no trailing path
AUD = "<application-audience-tag>"
JWKS_URL = f"{TEAM_DOMAIN}/cdn-cgi/access/certs"
# PyJWKClient expects standard JWKS; CF serves {"keys":[...]} at this URL — OK.

_jwks = PyJWKClient(JWKS_URL, cache_keys=True)

def email_from_access_request(headers) -> str:
    token = headers.get("Cf-Access-Jwt-Assertion")
    if not token:
        raise PermissionError("missing Cf-Access-Jwt-Assertion")
    key = _jwks.get_signing_key_from_jwt(token)
    payload = jwt.decode(
        token,
        key.key,
        algorithms=["RS256"],
        audience=AUD,
        issuer=TEAM_DOMAIN,
        options={"require": ["exp", "iss", "aud"]},
    )
    email = payload.get("email")
    if not email:
        raise PermissionError("no email claim")
    # Optional: enforce local allowlist again
    # if email not in ALLOW: raise PermissionError
    return email
```

Notes:

- Prefer header over cookie for non-browser clients.
- Cache JWKS; handle key rotation via `kid`.
- After Access, map `email` → Chudware user id / container session.
- WebSocket: validate JWT on **HTTP upgrade** request (browsers send Access cookie automatically on same site; non-browser clients need header).

### D. Streaming checklist under winner

- Implement remote UI stream as **WSS** (or HTTPS chunked), not raw WebRTC media.
- If WebRTC later: separate **TURN on UDP** (not free Tunnel public hostname), or TCP-only TURN (worse).
- Keep concurrent encoders ≤ GPU/CPU budget (3080 Ti helps encode; CF edge won’t).

---

## Manual vs automate matrix

| Task | Hunter manual | Automatable later |
|------|---------------|-------------------|
| CF Zero Trust enable, team name | Yes | — |
| Domain DNS | Yes | Terraform optional |
| Google Cloud OAuth client + consent | Yes | — |
| Access app + email policy + AUD | Yes | API/Terraform possible once token exists |
| Entra app + secret + admin consent | Yes (or UA IT) | — |
| `cloudflared` install + token | Token from UI | Service unit/Quadlet |
| JWT validation + session bind | — | Code |
| Podman session lifecycle | — | Code |
| UA IT exception for multi-tenant app | Yes if needed | — |

---

## OPEN QUESTIONS

1. **Does University of Alabama Entra allow user consent** for multi-tenant apps requesting only `openid profile email`? (One real ua.edu login test answers this; policies are not public-complete.)
2. **Do target users all have Google accounts**, or is Microsoft the only realistic IdP for them?
3. **Streaming protocol final choice** (WS frame stream vs WebRTC): HeadlessScout notes no in-tree WebRTC — aligns with Tunnel WS path.
4. **Domain ownership:** which zone will front `cad.*`? (Needed before tunnel hostname.)
5. **Will UA IT ever approve a registered app** for a research CAD service? If yes, CF Entra single-tenant is cleaner than multi-tenant hacks.
6. **Cloudflare free upload/connection limits** under sustained multi-user CAD streams — measure with a pilot (3 users, 30 min) before relying on free tier for demos.

---

## Sources (primary)

- Cloudflare Zero Trust free / 50 users: https://blog.cloudflare.com/teams-plans/
- Seat management: https://developers.cloudflare.com/cloudflare-one/team-and-resources/users/seat-management/
- Account limits: https://developers.cloudflare.com/cloudflare-one/account-limits/
- Validate JWTs / AUD / JWKS: https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/
- Entra IdP setup: https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/entra-id/
- Google IdP setup: https://developers.cloudflare.com/cloudflare-one/integrations/identity-providers/google/
- Access policies (email selectors): https://developers.cloudflare.com/cloudflare-one/access-controls/policies/
- Tunnel published protocols: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/protocols/
- WebSockets (CF network): https://developers.cloudflare.com/network/websockets/
- Community multi-tenant Entra + Access limitation: https://community.cloudflare.com/t/auth-against-azure-ad-multi-tenant/275689
- Microsoft single vs multi-tenant apps: https://learn.microsoft.com/en-us/entra/identity-platform/single-and-multi-tenant-apps
- User vs admin consent: https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/user-admin-consent-overview
- oauth2-proxy Entra multi-tenant: https://oauth2-proxy.github.io/oauth2-proxy/configuration/providers/ms_entra_id/
- Authentik vs Keycloak resource notes (secondary roundups 2025–2026): https://authhost.de/en/blog/authentik-vs-authelia-vs-keycloak

