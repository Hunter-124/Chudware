# chudware-web RUNBOOK (Hunter)

Host: Fedora 44. Production gateway: `127.0.0.1:8300`, data
`~/chudware-web-data/`. Canonical staging: `127.0.0.1:8310`, owned by the
separate `chudware-staging` account under `/var/lib/chudware-staging`.
Public portal: `https://app.chudware.com` through the tunnel — the only hostname that
serves the gateway. The marketing site at `https://chudware.com` and
`https://www.chudware.com` is Cloudflare Pages and is **not** tunnel ingress, so curling
the apex exercises Pages and tells you nothing about the gateway. Canonical
provisioning keeps staging unpublished; current live ingress must be verified.

Setting up external credentials for the first time (OAuth clients, Stripe, mail,
session secret, first admin) is a separate walkthrough:
[`SETUP_CREDENTIALS.md`](SETUP_CREDENTIALS.md). This file covers operating a
deployment that already has them.

---

## (a) SECURITY FIRST — rotate leaked credentials

`Webservice/Cloudflare-info.txt` (and git history) held a **live Cloudflare API token** and **R2 S3 keys**. Treat them as compromised.

### Rotate now

1. **Cloudflare API token**
   - Dashboard → My Profile → API Tokens → find/revoke the leaked token.
   - Create a replacement with least privilege needed for `cf_provision.py` (tunnels, DNS on `chudware.com`) — it no longer touches Access applications, so that permission is not needed.
   - Store the new token **only** outside git (password manager / `~/chudware-web-data/secrets/`). Never re-commit.

2. **R2 Access Key ID + Secret**
   - Dashboard → R2 → Manage R2 API Tokens → revoke the leaked pair.
   - Create a new R2 token if you still need object storage; put keys in the same secrets dir / env — not the repo.
   - Nothing consumes them today in any case: R2 is not enabled on the account at all
     (error 10042 from `GET /accounts/<id>/r2/buckets`), so both the monitor's status
     publish and the backup's off-box copy stay switched off until a human enables R2
     in the dashboard (§b manual steps, §f).

3. **Tunnel token**
   - Remotely-managed tunnel token lives only in `~/chudware-web-data/cloudflared.env`
     as `TUNNEL_TOKEN=…` (mode 600). It must not remain in `gateway.env`.
   - If the old API token could mint tunnel credentials, rotate the tunnel token after API
     token rotation (`cloudflared tunnel token <name>` or re-run provision), then run
     `sudo Webservice/deploy/harden_host.sh --apply tunnel`.

### Optional history rewrite (explicit approval only)

Secrets may still be in git history even if the working tree is clean. **Do not force-push without Hunter’s explicit go-ahead.**

```bash
# Example: purge Cloudflare-info.txt from all history (destructive rewrite)
git filter-repo --path Webservice/Cloudflare-info.txt --invert-paths
# Then ONLY if Hunter approved force-push to the remote default branch:
#   git push --force-with-lease origin master   # or main
```

If you skip rewrite: still rotate everything above; assume attackers who cloned the public repo already have the old material.

---

## Production host hardening baseline

An operator with a separately verified administrator account may run
`sudo Webservice/deploy/harden_host.sh --apply launch` on `livingroom-pc`.
The source `launch` profile includes removal of general sudo from the service
identity, plus `firewall`, `tunnel`, `updates`, `keys`, `backups`, `gateway`, and
`entitlement`. Source policy is not evidence of the installed host policy.

- **Firewall:** the default zone allows the `ssh` service and KDE Connect on
  `1716/tcp+udp`; the blanket `1025-65535/tcp+udp` LAN opening is gone. The
  script refuses to reload firewalld unless the active zone already allows TCP
  22 and sshd is listening. Immediately after applying, open a second SSH
  connection and scan from another LAN host:

  ```bash
  ssh livingroom true
  sudo nmap -sS -Pn -p- --open --min-rate 10000 --max-retries 1 192.168.0.183
  nmap -Pn -p 5432,8300,8310 192.168.0.183
  ```

  Expected open ports are `22` (sshd), `1716` (KDE Connect), and the intentional
  production-mail set: `25/465/993` (`stalwart-mail`), `587` (`mail-edge`), and
  `8443` (`mail-caddy`, with `roundcube` behind it). Those rootful Podman
  publishes are owned by CloudCastle's revenue-bearing mail service, not
  Chudware. Netavark DNAT deliberately bypasses the firewalld zone's port list;
  do **not** add a pre-netavark drop or close them during Chudware hardening.
  No other port may be open. In particular, PostgreSQL 5432, production gateway
  8300, and staging gateway 8310 must scan as filtered from the LAN.
- **Security updates:** `dnf-automatic.timer` is enabled and
  `/etc/dnf/automatic.conf` uses `upgrade_type = security` plus
  `apply_updates = yes`.
- **Tunnel secret:** `cloudflared.env` is owned by `liveuser`, mode 600.
  cloudflared reads `TUNNEL_TOKEN` from its environment; `ExecStart` contains no
  token expansion. Verify with `ps aux | grep '[c]loudflared'`.
- **LAN pivot:** the outbound `id_ed25519_to_other` key and its `other-pc`
  config stanza are absent. Preserve the inbound keys used to manage this host.
- **Gateway attack surfaces:** production and staging set
  `CHUDWARE_WEB_TERMINAL_ENABLED=0` and
  `CHUDWARE_WEB_SHARE_PROVIDER_KEYS=0`. The first removes the browser shell;
  the second prevents a customer context from inheriting host-owned provider
  credentials and spending the host account.
- **Backups:** provision and verify the environment-specific timer and dedicated
  read-only backup authority described in §g. Every nightly and pre-deploy
  artifact streams through `age` before writing; never grant general sudo to
  make unreadable customer state disappear from a backup failure.

### Shared-host capacity

`livingroom-pc` is not a Chudware-only host. It also runs the production mail
stack above in rootful Podman and a self-hosted CI runner. Treat host-level
firewall, container, package-update, CPU, memory, and disk changes as shared
infrastructure changes.

The two gateways each have `MemoryMax=1G`. At the 2026-08-10 hardening baseline
they used about 58 MB each (peaks 120 MB), the three mail services together used
about 160 MB at their systemd peaks, 9.4 GiB of 15 GiB RAM remained available,
and 49 GiB of 117 GiB disk remained free. Those ceilings leave ample memory and
disk headroom for mail. CPU was saturated by concurrent CI/build work while
mail containers were below 0.1% CPU combined; schedule production builds with
the understanding that CI, rather than gateway or mail memory, is the current
contention risk.

### Privilege prerequisites and unverified installed state

The service identity must have no general sudo, wheel membership, administrator
groups or shared production/staging authority. A separate operator administrator
must remain available before removing privileges. Backup read access to subuid
state requires narrowly scoped read/traverse ACLs or an independently reviewed,
read-only backup identity for that environment, not `NOPASSWD: ALL`.

Capture installed UID/group/sudoers, user-manager authority and SSH policy as
operator evidence. This checkout does not establish current live wheel/sudo or
SSH password-authentication state. A source hardening control does not prove it
has been applied; host compromise remains a risk until those boundaries are
provisioned and independently demonstrated.

## (b) Cloudflare — what is already provisioned vs manual

### Pre-provisioned (driver / `Webservice/deploy/cf_provision.py`)

Do **not** hand-create these if provision already ran:

| Item | Value / note |
|------|----------------|
| Zero Trust org | exists |
| Team domain | `https://bruh234324.cloudflareaccess.com` |
| One-Time PIN IdP | already enabled |
| Zone | `chudware.com` active (Cloudflare Registrar, `boyd`/`emily.ns.cloudflare.com`) |
| Tunnel | `chudware-web`, id `95256541-2ebf-4101-92a1-daecd240c087` (remotely-managed) |
| Production ingress | `app.chudware.com` → `http://127.0.0.1:8300` — the only hostname that reaches the gateway |
| Staging ingress | Unpublished by default in current source; historical 2026-09-02 measurement carried none. `cf_provision.py --publish-staging` is explicit opt-in, never routine setup. Current installed ingress unverified |
| Foreign ingress | `scootware.us`, `www.scootware.us` → `http://127.0.0.1:8320` — an unrelated project sharing this tunnel. Not Chudware, not a stray rule; leave them alone |
| Catch-all ingress | `http_status:404` — no apex, `www`, or `sso.chudware.com` origin route |
| Pages DNS | `chudware.com`, `www.chudware.com` → `chudware-site.pages.dev`, proxied. The apex is Pages, **not** the gateway: it answers 200 with the marketing site |
| Access application | **none, and none exists in the account.** `/admin` is gated by the gateway itself; provisioning neither creates nor rewrites an Access app, so there is no gateway AUD to keep unique |

`cf_provision.py` is **driver-owned** and lands beside these deploy files — DeployOps does
not implement it. It writes the gateway's Cloudflare values into
`~/chudware-web-data/gateway.env` (never committed). If provisioning places a fresh
`TUNNEL_TOKEN` there, immediately run the tunnel hardening item; it moves the token to
the dedicated mode-600 file and removes it from the gateway environment:

```bash
CHUDWARE_WEB_PUBLIC_BASE_URL=https://app.chudware.com   # the only value provisioning writes
sudo Webservice/deploy/harden_host.sh --apply tunnel
```

The operator door for `/admin` is the account store: a `chudware_sid` session whose
`accounts` row carries `role = 'admin'`, granted only by
`python -m authn grant-admin` (§c). The gateway's own self-serve sign-in is the entire
front door: `GET /` with an HTML `Accept` 303s to `/signin`, `/signin` and `/signup`
answer 200, and `/api/auth/oauth/{google,github,microsoft}/start` 302 with
`redirect_uri=https://app.chudware.com/api/auth/oauth/<provider>/callback`.
`auth.py::AuthMiddleware._authenticate` tries the `chudware_sid` cookie **first**;
Cloudflare Access is only the fallback it reaches for when a caller has no session.
That fallback is still supported code and a legitimate deployment mode — set
`CHUDWARE_WEB_CF_TEAM_DOMAIN` + `CHUDWARE_WEB_CF_AUD` by hand if you want it, and the
gateway verifies `Cf-Access-Jwt-Assertion` against team JWKS
(`…/cdn-cgi/access/certs`) with `iss` = team domain and `aud` = `CHUDWARE_WEB_CF_AUD`.
JWKS rotates ~every 6 weeks (previous key valid ~7 days) — always fetch remotely,
match `kid`. It is **unused here:** no Access application sits in front of
`app.chudware.com`, and both variables were removed from `gateway.env` on
2026-09-02, so an unauthenticated API call now answers the honest
`{"error":"not signed in"}`. Leaving both unset is the supported configuration.
Leaving them *set* with no Access app in front only costs you that message —
the misleading `{"error":"missing Cf-Access-Jwt-Assertion"}` — because the
Access branch is entered before the honest one. It is no longer a fault: a
leftover `CF_Authorization` cookie presented to a gateway with Access
unconfigured used to reach a 500, and now reaches the same 401.

### Required cutover order — do not invert

Removing the Access wall in front of `/admin` before the gateway's own sign-in is
known-good would expose the origin with no login at all. Perform this cutover in
order:

1. Deploy the gateway build while the existing Access application still protects
   the origin.
2. Through the origin or a temporary hosts override, register, verify, and sign
   in with the gateway's `chudware_sid` cookie. Confirm `/`, `/api/portal/account`,
   and `/app/` enforce that session and sign-out makes them inaccessible.
3. Grant yourself the role and confirm it is the gate:
   `cd Webservice/gateway && .venv/bin/python -m authn grant-admin --email <addr>`,
   then check that this account reaches `/admin` while a `role = 'user'` account
   receives 403 there.
4. Only after steps 1–3 pass, run `python3 Webservice/deploy/cf_provision.py`. It
   changes ingress/DNS/Pages only — it no longer touches Access at all. The leftover
   `app.chudware.com/admin*` application that used to need deleting by hand **is gone**:
   as of 2026-09-02 the account holds no Access application over any `chudware.com`
   host, so Cloudflare no longer shows its own login page in front of the gateway's.
5. From an incognito browser, re-check public marketing pages at the apex,
   session-cookie login at `app.chudware.com`, an anonymous `GET /admin`
   redirecting to `/signin?next=%2Fadmin` (not to `cloudflareaccess.com`),
   admin-role enforcement at `/admin`, and staging isolation.
   `Webservice/deploy/verify_public.sh` asserts exactly this.

This repository change records the cutover but does **not** apply it to live
Cloudflare.

### Hunter remaining manual steps

1. **Rotate leaked API token + R2 keys** (§a) — do this before relying on any old token in scripts.
2. ~~**Delete the `app.chudware.com/admin*` Access application**~~ — **done.** Verified
   2026-09-02: the account contains no Access application over a `chudware.com` host at
   all (the ones that remain belong to an unrelated project on `scootware.us`), so
   nothing fronts `/admin` but the gateway's own sign-in. `cf_provision.py` deliberately
   does not manage Access, so nothing here recreates it either.
3. **Enable R2 in the Cloudflare dashboard.** R2 is not enabled on this account:
   `GET /accounts/9a800a5f674233fe127cdb44b395ebfa/r2/buckets` answers error 10042,
   "Please enable R2 through the Cloudflare Dashboard". Until a human does that, bucket
   `chudware-releases` does not exist, the monitor's public `status.json` publish stays
   switched off (§f), and `backup.sh` keeps local copies only. This is the one item in
   this list no script can do for you.
4. **Grant the admin role** to each operator address, which is now the *whole*
   `/admin` allowlist — there is no second list to keep in sync:
   ```bash
   cd Webservice/gateway && .venv/bin/python -m authn grant-admin --email <addr>
   ```
   Then sign in at `https://app.chudware.com/signin` and open `/admin`.

### Auth honesty (what a login actually proves)

Site login is the gateway's own: an email plus an argon2id-hashed password, or Google/GitHub
OAuth if those client credentials are configured. Both prove control of an **email mailbox**
or of a Google/GitHub account. Neither is Microsoft Entra / "sign in with your school
Microsoft account", and nothing here is an institutional SSO — do not market it as one.

`/admin` proves possession of a gateway session for an account whose `role` column an
operator set from the CLI. That is a deliberate step someone with database access took,
not a mailbox check — which is what the Cloudflare Access One-Time PIN in front of
`/admin` used to prove, and the reason it was the wrong door.

---

## (c) Accounts and sign-in

There is no users file. Accounts live in PostgreSQL and people create their own:

```
POST /api/auth/register        email + password -> pending account, verification mail
GET  /api/auth/verify?token=…  activates the account on a 14-day trial, signs in
POST /api/auth/login           argon2id check -> new session row + chudware_sid cookie
POST /api/auth/logout          revokes that session row
POST /api/auth/forgot | reset  emailed password reset
GET  /api/auth/oauth/{google|github}/start | /callback
```

- Sessions are **server-side rows**, not signed blobs: the `chudware_sid` cookie is an
  opaque id, so revoking a row logs that browser out immediately. TTL
  `CHUDWARE_WEB_SESSION_TTL_DAYS` (30), trial window `CHUDWARE_WEB_TRIAL_DAYS` (14).
- `CHUDWARE_WEB_SESSION_SECRET` must be set in `gateway.env`; outside `CHUDWARE_WEB_DEV=1`
  the gateway refuses to start without it unless the optional CF Access fallback is
  fully configured. Generate one with
  `python3 -c 'import secrets;print(secrets.token_urlsafe(48))'`. Optional
  `CHUDWARE_WEB_GOOGLE_CLIENT_ID`/`_SECRET` and `CHUDWARE_WEB_GITHUB_CLIENT_ID`/`_SECRET`
  enable the OAuth buttons.
- These endpoints are **API-only today** — the hosted sign-in form and marketing page are
  a later slice. An unauthenticated HTML request gets a 401 page; an authenticated but
  non-`active` account gets a 403 status page whose copy depends on the state
  (pending / denied / suspended / trial_expired).
- Status is `pending` → `active` (self-serve on verification) and an admin may set
  `denied`/`suspended`. Only `active` accounts get any rights; role and plan are read from
  the row, never from the client.
- **heavy** is a persisted per-account flag surfaced in `/admin` and `/api/me`.
  Nothing enforces it since the streamed-session product was removed; it is kept
  as the hook plan entitlements will use.
- **First admin, or locked out?** Run the operator bootstrap as the service user, in
  the gateway directory, with `CHUDWARE_DB_URL` set. It registers the address if
  needed, sets the password, marks it verified, activates it, and grants `admin`:
  ```bash
  cd Webservice/gateway && .venv/bin/python -m authn grant-admin --email you@chudware.com
  ```
  It prompts twice for the password (or reads one line from stdin with
  `--password-stdin`); the password never appears in argv. Idempotent — rerun it to
  reset a forgotten password. `role` has no HTTP writer by design, so this CLI is
  the only way to grant it, and it is now the *only* gate on `/admin` — there is no
  Access allow policy to add the address to as well.

---

## (d) Staging gateway

Staging uses a separate Unix identity and user manager, not another unit under
production's account. A distinct session secret or database alone is insufficient.

| Boundary | Production | Staging |
|---|---|---|
| Service identity/home | existing production account | `chudware-staging`, `/var/lib/chudware-staging` |
| Gateway | `chudware-gateway`, `127.0.0.1:8300` | `chudware-gateway-staging`, `127.0.0.1:8310` |
| Browser execution | one `chudware-cell@<32-hex-id>.service` per workspace in production's user manager | separate cells in staging's user manager; no production socket access |
| State/workspace/bucket | configured production roots | dedicated home `chudware-web-staging/{workspace,bucket}` |
| PostgreSQL login/database | `chudware_prod` | `chudware_staging` |
| Customer release root/CDN bucket | configured production targets | `chudware-web-staging/customer-releases`, `chudware-releases-staging` |
| Entitlement authority | shipped product public root | independent staging public root; production clients reject it |

An operator must provision the separate account/group without sudo or production
groups, its home mode `0700`, its independent reviewed checkout/runtime inputs,
and its user manager/linger. Do not copy production environments, sessions,
provider/OAuth credentials, signing keys or user data into staging.
Provision a `NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT` staging database role,
grant only its database, and revoke public/foreign production connection and
schema privileges. Prove those ACLs with synthetic identities before use.

Under the **staging account**, create owner-only `chudware-web-staging/gateway.env`
with the dedicated DSN, independent session secret and a synthetic health account.
Provision its own encrypted entitlement credential; configure
`CHUDWARE_STAGING_ENTITLEMENT_PUBLIC_KEY` as the matching 64-character hex public
key, distinct from production. Stripe keys/prices are test-mode only or unset.
Keep terminal/provider sharing disabled. Source and daemon inputs must be in the
dedicated home because staging units hide `/home`.

After authenticated runtime/tool inputs exist, run only from its checkout.
`setup.sh` is bootstrap provisioning, not a maintenance-gated live redeploy:
use it only with independently established absence of existing active/unknown
work. Subsequent deployments use `update.sh` and its protected lease; never
rerun setup over a live gateway/cell installation:

```bash
bash Webservice/deploy/setup.sh staging
CHUDWARE_DEPLOY_ENV=staging bash Webservice/deploy/install_webd.sh --restart
bash Webservice/deploy/update.sh staging
systemctl --user is-active chudware-gateway-staging
curl -fsS http://127.0.0.1:8310/healthz
```

The scripts reject the shared production identity before mutation. Before widening
access, capture installed unit/drop-in parity, both running UIDs/groups, daemon
sockets, sanitized effective root/role settings, failed cross-environment session
and file requests, IAM bucket scope, both environments' timer state and backup
coverage. Do not print environment secret values. Decommission historical
same-account staging only through a reviewed operational migration; no live
service was changed by the local remediation.

Staging administrators cannot call production rebuild/redeploy and receive only
their own journal. `cf_provision.py` preserves foreign routes and leaves staging
unpublished; publication needs explicit `--publish-staging` plus the evidence
above. Current live configuration and historical exposure remain unverified.

---

## (e) Demo-day checklist

- [ ] Secrets rotated; `gateway.env` has `CHUDWARE_WEB_SESSION_SECRET`; `cloudflared.env` has `TUNNEL_TOKEN` (both mode 600).
- [ ] `bash Webservice/deploy/setup.sh` already run; linger enabled.
- [ ] `systemctl --user start chudware-gateway chudware-cloudflared chudware-monitor.timer`
- [ ] `curl -fsS http://127.0.0.1:8300/healthz` — exactly `{"ok":true}` is gateway liveness, not a work census or release-readiness proof; inspect host capacity separately.
- [ ] Every demo attendee has registered and verified (or you approved them at `/admin`).
- [ ] Browser smoke: `https://app.chudware.com` → sign in → portal → `/app` loads.
- [ ] Know the not-yet-approved path (the gateway's pending/denied status page) and the
      403 status page a non-admin account gets at `/admin`.
- [ ] Confirm `REFUSE_NEW` is absent unless disk is actually critical: `ls ~/chudware-web-data/REFUSE_NEW`.

---

## (f) Troubleshooting

### JWKS / 401 after an Access login

Only relevant when the optional Access fallback is configured; with it unset a caller
with no session simply gets `/signin`.

- Confirm `CHUDWARE_WEB_CF_AUD` matches Access app → Overview / Additional settings → Application Audience.
- Team domain must be exactly `https://bruh234324.cloudflareaccess.com` (no trailing slash mismatch vs gateway config).
- Force JWKS refresh: restart gateway after Access app recreation (AUD changes if app is recreated).
- CF rotates Access signing keys ~6 weeks; gateway must use live JWKS URL, not a pinned PEM.

### 401 or a Cloudflare login page at `/admin` while signed in

- A Cloudflare login page means someone recreated a self-hosted Access application over
  `app.chudware.com`; there is none in the account today (§b). Delete it in Zero Trust.
  The gateway does not provision Access, so nothing here removes it for you.
- A 403 status page means the session is fine and the account's `role` is not `admin`:
  run `python -m authn grant-admin --email <addr>` and reload.

### cloudflared not connecting

```bash
systemctl --user status chudware-cloudflared
journalctl --user -u chudware-cloudflared -n 80 --no-pager
# token present, without printing it?
stat -c '%a %U:%G %n' ~/chudware-web-data/cloudflared.env
sed -n 's/^TUNNEL_TOKEN=.*/TUNNEL_TOKEN=<redacted>/p' \
  ~/chudware-web-data/cloudflared.env
ps aux | grep '[c]loudflared'
```

Binary: `/usr/local/bin/cloudflared`. The unit runs `cloudflared tunnel run`;
cloudflared reads `TUNNEL_TOKEN` from the protected environment instead of argv.

### Gateway / uvicorn

```bash
systemctl --user status chudware-gateway
journalctl --user -u chudware-gateway -n 80 --no-pager
curl -sS http://127.0.0.1:8300/healthz
```

Env file: `~/chudware-web-data/gateway.env`. Unit `MemoryMax=1G`.

### Workspace cell engine logs

```bash
systemctl --user list-units --all 'chudware-cell@*.service'
systemctl --user status chudware-cell@<32-hex-id>.service
journalctl --user -u chudware-cell@<32-hex-id>.service -n 200 --no-pager
```

Each browser workspace has a private socket-activated cell, not one shared
hostd. `/healthz` is only `{"ok":true}`; neither it nor a zero `/engine` bridge
count certifies idle native/agent work. The protected deployment census in §h
is the cutover authority. An optional shared local/native `chudware-hostd`
may exist, but is not a browser consumer: do not stop, restart or force-drain
it for cell deployment, including when a gateway or cell probe fails.

These are **source-template ceilings**, not observed installed-host settings:

| Service role | Memory maximum | Task maximum | CPU quota | Swap maximum |
|---|---:|---:|---:|---:|
| Optional local/native `chudware-hostd.service` | 8 GiB | 512 | Not set by template | Not set by template |
| Browser workspace `chudware-cell@.service` | 8 GiB | 512 | 200% | 0 |
| Workspace egress broker | 256 MiB | 64 | 50% | 0 |

The optional daemon additionally sets `MemoryHigh=7G`; it is not a tenant
isolation boundary or a browser fallback. Its idle/GUI-exit grace settings are
not active-agent lifetime limits. `install_webd.sh` retains its template but
does not enable or restart it. A separately authorized local/native deployment
must inspect effective unit/drop-ins and parent-cgroup limits; copying this
table does not qualify service privileges or resource enforcement. Cell
deployment drain and rollback never acquire authority over its sessions.

### Monitor

```bash
systemctl --user status chudware-monitor.timer
python3 Webservice/deploy/monitor.py --dry-run   # one JSONL sample
tail -n 5 ~/chudware-web-data/logs/monitor.jsonl
```

- Disk free &lt; 10% on the data root → touches `~/chudware-web-data/REFUSE_NEW`.
  The flag is advisory: `/healthz` and `/admin` surface it so you know the host
  is under disk pressure. Clear it by hand from `/admin` once space recovers.

### Metrics, dashboards, status publishing, and off-box alerting

The gateway exposes Prometheus text at `GET /metrics`. It contains request
counts and latency histograms labelled by the **matched route template** (never
raw user/document ids), current `/engine` bridge and browser-session counts,
five-minute credit spend rate, mail outbox backlog/failures, Stripe webhook
outcomes, PostgreSQL pool saturation, entitlement check-ins, and update
manifest hits. The values include business telemetry and are not public.

Generate independent operator secrets and add them to the mode-600
`~/chudware-web-data/gateway.env`:

```bash
printf 'CHUDWARE_METRICS_ADMIN_TOKEN=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" >> ~/chudware-web-data/gateway.env
printf 'CHUDWARE_GRAFANA_ADMIN_PASSWORD=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" >> ~/chudware-web-data/gateway.env
chmod 600 ~/chudware-web-data/gateway.env
# Apply gateway configuration only through §h's authenticated maintenance cutover:
bash Webservice/deploy/update.sh prod --continuous --no-pull --gateway-only

# Exact token contract used by Prometheus and an operator smoke:
set -a; . ~/chudware-web-data/gateway.env; set +a
curl -fsS -H "Authorization: Bearer $CHUDWARE_METRICS_ADMIN_TOKEN" \
  http://127.0.0.1:8300/metrics | head -30
```

`monitor.py --install-observability` is the sole Podman surface in this
repository. It creates two rootless containers using host networking because
the gateway is intentionally loopback-only; both services explicitly bind
loopback too, so neither dashboard becomes a public listener:

- `chudware-prometheus` at `127.0.0.1:9090`, scraping the host gateway every
  15 seconds with the metrics token and retaining samples for **30 days**;
- `chudware-grafana` at `127.0.0.1:3000`, signup disabled, with Prometheus
  provisioned as its default datasource.

```bash
sudo dnf install -y podman
python3 Webservice/deploy/monitor.py --install-observability
podman ps --filter name=chudware-
# From a workstation: ssh -L 3000:127.0.0.1:3000 livingroom
```

The two named volumes survive container replacement. Re-running the install
command is the supported configuration update and preserves those volumes.
The installer also enables the rootless `podman-restart.service`, so both
`--restart=unless-stopped` containers return after a reboot.
The ports bind loopback only; do not add them to the Cloudflare tunnel.

Every normal monitor sample also writes a non-sensitive public status object
to R2 (`s3://chudware-releases/status.json` by default; override with
`CHUDWARE_STATUS_BUCKET` / `CHUDWARE_STATUS_KEY`); the object must be routed to the
Pages `https://chudware.com/status.json` path. **That publisher is optional and is off
today.** R2 is not enabled on the Cloudflare account — `GET
/accounts/9a800a5f674233fe127cdb44b395ebfa/r2/buckets` answers error 10042, "Please
enable R2 through the Cloudflare Dashboard" — so there is no bucket to write to until a
human enables it. Configuring the monitor unit with `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY` and `R2_ENDPOINT` is what switches the publish on; the presence
of all three *is* the switch, there is no separate opt-in flag. With none of them set
the sampler still appends its local JSONL record, records
`status_publish = {"ok": false, "skipped": "r2_not_configured", …}` naming the missing
variables and error 10042, prints one `monitor: status publish skipped (optional)` line
on stderr, and **exits 0** — an unconfigured optional publisher must not hold
`chudware-monitor.service` in `failed`, which is exactly what it did continuously from
2026-08-24. A *partially* configured or configured-but-broken publisher is still a hard
failure: loud stderr, `status_publish = {"ok": false, "error": …}`, exit 2. `--dry-run`
prints the exact public body without touching R2.

`.github/workflows/prod-external-watch.yml` runs every five minutes on
`cloudcastle-server` (`cc-artifact-host`), never on the production host. It
checks both `https://app.chudware.com/healthz` and the operational Pages status
blob — that second check cannot pass until R2 is enabled and a first status object is
actually published. Failures (including DNS, TLS, timeout, malformed or degraded JSON) are
persisted on that runner; only the third consecutive failure opens the exact
`[prod-watch] Chudware production surface unavailable` issue and sends mail.
Later failures deduplicate against that open issue and send no more mail. The
first healthy run comments with the recovery time and closes it. Configure
repository secrets `PROD_WATCH_RESEND_API_KEY`, `PROD_WATCH_EMAIL`, and
`PROD_WATCH_FROM`; missing mail credentials fail the alert run loudly after
the GitHub issue has still been opened.

### Linger / user units vanish on logout

```bash
loginctl show-user "$USER" -p Linger
sudo loginctl enable-linger "$USER"
```

## (g) Admin ops dashboard + backups

### /admin ops

`https://app.chudware.com/admin` (admin role) shows, above the accounts queue:

- **Host bar** — free RAM, VRAM used/total + GPU util (`nvidia-smi`), load,
  plus **service chips** (cloudflared / monitor / backup unit states) and a
  **REFUSE_NEW** toggle (the same disk-pressure flag the monitor writes; the
  monitor re-creates it while disk is critical, so a manual clear can be
  overridden on its next sample).
- **Accounts queue** (`GET /api/admin/accounts`, filterable by status, with a
  pending badge from `/api/admin/accounts/pending-count`) — approve, deny or
  suspend a registration and set its plan in place
  (`POST /api/admin/accounts/{email}/{approve,deny,suspend,plan}`).
- **Gateway log** — journal tail in the page (`/api/admin/logs`), optional
  5 s follow. No SSH needed for demo-day triage.
- **Deployment status (read-only)** — operator-written deployment status and
  `update.log` under the selected data root (`GET /api/admin/deploy/status`,
  `GET /api/admin/deploy/log?lines=400`). Both require an active admin session.
  The System panel describes §h's qualified operator workflow; it cannot launch
  builds or service restarts. These snapshots are not a work census or cutover
  authority, and reading them does not create or repair deployment state.

### Shared /admin terminal (disabled in production)

`CHUDWARE_WEB_TERMINAL_ENABLED=0` is part of the production and staging
hardening baseline. With that setting the terminal routes are unavailable.
Do not enable it for operator convenience: it exposes a shell as the gateway
account. Historical audit evidence recorded passwordless sudo on that account;
this checkout does not prove that installed authority was removed (§Privilege
prerequisites and unverified installed state).

The legacy terminal implementation uses named **tmux** sessions on socket
`$CHUDWARE_TMUX_SOCKET` (default `chudware`), surviving browser disconnects.
`GET/POST /api/admin/terminal/sessions`,
`DELETE /api/admin/terminal/sessions/{name}` and
`/admin/terminal?name=<s>` describe that mechanism, not production approval.
Do not use `tmux kill-server` to clear unknown or user-owned sessions. Any
independently authorized cleanup must address only positively owned, settled
sessions. Apply a disabled-terminal gateway configuration through §h's
maintenance lease, never a bare gateway restart under active/unknown work.

### Nightly backups

Each environment's own user manager runs `chudware-backup.timer` (03:30 +
jitter, `Persistent=true`) and `Webservice/deploy/backup.sh`. One run backs up
only `CHUDWARE_DEPLOY_ENV=prod|staging`; it never carries both databases'
credentials or reads both environments' state. Staging requires the dedicated
`chudware-staging` identity, `/var/lib/chudware-staging` home, and dedicated
`chudware-web-staging` data root with all storage roots beneath it.

Before enabling the timer, the operator must provision mode-0600
`~/.config/chudware/backup.env` for that environment. It supplies the reviewed
gateway configuration: `CHUDWARE_DEPLOY_ENV`, `CHUDWARE_DB_URL`,
`CHUDWARE_WEB_DATA_ROOT`, `CHUDWARE_WORKSPACE_ROOT`, `CHUDWARE_WEB_BUCKET_ROOT`,
`CHUDWARE_CELLS_ROOT`, and (when configured)
`CHUDWARE_WEB_UNIVERSITY_WORKSPACE_ROOT`, plus the existing gateway encryption
configuration required by `Settings`. `CHUDWARE_GATEWAY_PYTHON` selects the
provisioned gateway interpreter if its checkout differs from the default.
Keep its role/database and every root independent from the other environment.
The backup unit loads this file; it does not implicitly inherit gateway or R2
credentials. Installed unit/config parity and actual authority remain operator
prerequisites, not claims made by this source change.

- `pg_dump --format=custom` uses the selected environment's shared gateway DSN
  validation, with credentials in a private, cleaned-up temporary pgpass and
  a password-free command line. It pipes directly into `age`, never a plaintext
  dump. `update.sh` uses the same helper for encrypted pre-deploy rollback
  points under `DATA_ROOT/backups/`; a missing recipient or failed dump/encryption
  aborts before deployment changes the database or service.
- The encrypted state tar starts with `backup-manifest.json`: environment,
  configured data/workspace/bucket/cells roots, optional institutional root,
  source device/inode and mount ID/path, and credential/volatile exclusions.
  Each root has its own `roots/<label>/` tree; overlapping nested roots are
  enumerated separately. Configured mount roots are explicit boundaries;
  an unconfigured nested mount, unreadable/missing root or file, link, or
  unsupported special file fails closed. Nothing elevates through sudo.
  Opaque `.step+` bytes, embedded solve/build caches, PLM state, institutional
  cache and durable cell/actor home/data/config/cache state are retained without
  reserialization. `data/logs`, `data/tmp`, `data/backups`, cell `run` directories
  and named Unix sockets are recreatable and excluded.
- The recipient remains `CHUDWARE_BACKUP_AGE_RECIPIENT` or the selected data
  root's `secrets/backup_age_recipient`. With neither, the run refuses.
  **Credential trees are never data:** every nested `secrets`/`credentials`,
  hidden equivalent, `.ssh`, `.aws`, `.azure`, `.gnupg`, `.pki`, `.docker`,
  `.kube`, `gcloud`, `credstore[.encrypted]` and browser profile directory is
  excluded. This includes `university/secrets/rclone.conf`. Every `.env*`,
  `*.env[.*]`, `rclone.conf`, provider auth/credential/token file, `.netrc`,
  `.pgpass`, `Cloudflare-info.txt`, and `*.pem`/`*.key`/`*.p12`/`*.pfx` file
  is excluded, as are configured credential paths recorded in the manifest.
  The actor `state/actors/<actor>/data/<owner-hash>/config.json` and desktop
  `Chudware`/`Billet`/`AI-CAD` `config.json` stores mix settings with OAuth/API
  credentials; they and their temporary copies are excluded, not serialized.
  Application-created actor `chudware-{byok,subscription,pool}-*` temporary SDK
  homes are credential authority and excluded as complete trees; the separate
  opaque `Sessions/` histories remain. An installed SDK's other private runtime
  stores are not qualified by this source change: operators must inventory and
  separately protect them before enabling backups, using explicit credential
  paths rather than guessing undocumented SDK filenames. Other durable actor
  state is retained. Reprovision these settings/credentials independently through
  the operator's password/key custody process; do not weaken exclusions.
- Provision dedicated **read-only** access to all selected durable roots,
  including subuid-owned actor state, using reviewed read/traverse ACLs or a
  separately confined per-environment backup authority. Do not grant general
  sudo, write access to customer state, production access to staging, or staging
  access to production. Prove completeness with inert markers under each root,
  and prove a missing/read-denied root fails. Timer success/freshness, restore
  drills and actual ACLs must be captured separately as operational evidence.
- `CHUDWARE_WEB_BACKUP_DIR` defaults to `~/chudware-web-backups/`; it must be
  owned by the backup identity and mode 0700, with encrypted artifacts 0600.
  Its parent must already exist, be identity-owned, not group/world writable,
  and have no linked ancestors. Missing or unsafe parents fail before creation.
  `CHUDWARE_WEB_BACKUP_KEEP` defaults to **7** per exact environment/family,
  and `CHUDWARE_BACKUP_LIFECYCLE_DAYS` bounds local and remote age at **30** days.
  Retention removes only exact helper-named regular, single-link files owned by
  that identity. Foreign-environment files, legacy/plaintext files, unrelated
  files, links and other runs' partials are not silently collected. Existing
  plaintext rollback dumps require a separate reviewed operator disposition.
- R2 upload requires that environment's `R2_ACCESS_KEY_ID`,
  `R2_SECRET_ACCESS_KEY` and `R2_ENDPOINT`, with independently verified private
  bucket/IAM scope. `CHUDWARE_BACKUP_BUCKET` defaults to `chudware-backups` for
  production and `chudware-backups-staging` for staging; staging refuses the
  default production bucket. Keys/lifecycle prefixes are environment-scoped
  (`prod/` or `staging/`). Without all credentials, output explicitly says
  **LOCAL COPY ONLY**; it is not evidence of an off-host backup.

```bash
# Run under only the selected environment's backup identity/user manager.
systemctl --user start chudware-backup.service
systemctl --user list-timers chudware-backup.timer
```

### Isolated recovery, never an in-place production extraction

Run drills on a separate recovery host/workspace with synthetic database
credentials. Bring the age identity through independent private custody; the
private key must not live next to the backups or in their archived state.
Use an explicit, operator-owned, non-group/world-writable parent, private umask,
and cleanup trap. No fixed `/tmp/prod.dump` and no retained plaintext dump:

```bash
umask 077
drill="$(mktemp -d "$HOME/chudware-recovery.XXXXXXXX")"
trap 'rm -rf -- "$drill"' EXIT HUP INT TERM
gateway_python=/absolute/path/to/recovery/gateway/.venv/bin/python
helper=/absolute/path/to/recovery/checkout/Webservice/deploy/backup_state.py
identity=/absolute/path/to/independently/custodied/age-identity
state_archive="/absolute/path/to/nightly-state-prod-<stamp>-<id>.tar.age"
database_archive="/absolute/path/to/nightly-database-prod-<stamp>-<id>.dump.age"

# Decrypt as a stream into a private staging directory. Validate the manifest,
# every member path/type and the complete age stream before placing any files.
"$gateway_python" "$helper" restore-state --environment prod \
  --artifact "$state_archive" --identity "$identity" --target "$drill/recovered"

# Provision an empty scratch database/login separately. Supply its credential
# privately in the environment, not argv; neither may be a prod/staging service
# target. This helper streams age into transactional pg_restore and never uses
# --clean/--create; existing object conflicts fail rather than erase objects.
# CHUDWARE_RESTORE_DB_URL is supplied by the operator's private credential path.
"$gateway_python" "$helper" restore-database --artifact "$database_archive" \
  --identity "$identity" --target-db chudware_recovery_drill \
  --temporary-root "$drill/private"
```

State restore never follows archive links or trusts archived absolute source
paths as destinations. Recovered roots stay beneath the explicit isolated target;
targets overlapping original storage roots are rejected. Existing targets are
preserved by default; `--replace` explicitly replaces the entire isolated target
only after validation/decryption succeeds. Recovery temporary directories and
pgpass files are private and cleaned up on exit. Inspect manifest coverage,
opaque cached part bytes and actor state, map ownership/mount placement for the
recovery host, restore credentials independently, and prove a schema-compatible
database/application recovery before a separately reviewed live cutover.

The configured native state directory's `cache-provenance.key` is also a
credential and is excluded by `*.key`. Recover the original per-environment/cell
key through separately protected credential custody to retain authenticated
cache trust; never share staging/production keys or put one in the state archive.
Without it, restored opaque `.step+` caches still provide the last cached display
immediately, marked unverified while the engine rebuilds/verifies feature history
in the background and writes new locally authenticated caches. Do not treat that
unverified display as a successfully completed trust-recovery drill.

## (h) Pull / rebuild / redeploy

The same script deploys both environments, subject to independent qualification.
Automatic master activation is suspended; these conditional operator commands
do not provision missing authority or approve a live operation:

```bash
# Qualified staging operator, under the dedicated staging identity:
bash Webservice/deploy/update.sh staging

# Explicitly authorized continuous-production policy, not automatic on push:
bash Webservice/deploy/update.sh prod --continuous

# Deliberate pinned release; the tag is mandatory and re-verified at HEAD:
bash Webservice/deploy/update.sh prod --tag v1.2.0

# Operational variants retain the same environment guardrails:
bash Webservice/deploy/update.sh staging --no-pull --gateway-only
bash Webservice/deploy/update.sh prod --tag v1.2.0 --pull-only
bash Webservice/deploy/update.sh prod --tag v1.2.0 --web-only
```

External prerequisites are not installed or approved merely by invoking these
commands:

- A real local Linux x86_64 `chud` matching
  `vendor/chudcode/CHUDCODE_ARTIFACT.lock`, supplied as `CHUDWARE_CHUD_BIN`.
  `install_webd.sh` verifies and stages its bytes; replacing a lock requires
  independently obtaining the matching executable, not trusting a filename.
- An independently authenticated `CHUDWARE_BASE_PYTHON=/usr/bin/python3`,
  the complete reviewed `vendor/python/RUNTIME_WHEELS.lock`, and authenticated
  offline wheels at `CHUDWARE_WHEELHOUSE` (default `<repo>/wheelhouse`), including
  `chudcode-rpc`. The retained cell gets its own materialized venv and source
  closure. A user-site `pip install`, editable SDK checkout or gateway venv
  cannot substitute for this release-private runtime.
- Protected, environment-matching `gateway.env`, independent real mail/provider
  encryption keys, an explicit isolated database target, encrypted systemd
  entitlement credential, and an active admin identity for operator health.
  Offline mail configuration never proves DNS/SPF/DKIM, provider acceptance or
  recipient receipt.
- A validated schema-7 prior payload with a root-owned authorization record, or
  an independently provisioned exact-commit first-cutover acknowledgment as
  described below. Neither self-computed hashes nor local fixtures grant it.
- Installed cell/broker configuration, enforced quota proof, delegated cgroups,
  authenticated OS runtime/trust store and a reachable gateway implementing the
  protected maintenance API. Failure or absence refuses cutover, not permission
  to stop a user's daemon. Current production/ingress/rotation/off-host-recovery
  and genuine provider authority require external attestations.

**`update.sh` exit codes:** `0` completed; `1` failed after its mutation boundary;
`2` usage error; `3` another deploy owns the flock; `4` premutation refusal.
Refusals include dirty checkout, protected configuration/runtime/authority
failures, wrong environment or exact ref, active work, and unknown census.
A refusal attempts to release only this run's own lease; a failed release leaves
admission **unknown**, not proven open. Exit `1` does not imply an automatic full
rollback is possible: preserve state and use the lease recovery policy below.

For a dirty-tree refusal, inspect the checkout and coordinate its owners. Never
stash, reset, checkout over or delete somebody else's shared changes merely to
make deployment proceed. The historical 2026-09-03 exit-1 dirty-tree alert and
torn-retained-release incident remain provenance, not current rollback authority.

Before checkout/dependency/schema or payload mutation, the script resolves the
exact target commit, requires a clean tree and qualified prior-payload authority,
then acquires maintenance and proves all cells inactive. Gateway deployments
take an encrypted `backups/pre-deploy-<environment>-<UTC>.dump`, migrate the
selected database and restart only the selected gateway under that lease.
Logs/status live under the selected data root. A deployment flock (fd 9,
`CHUDWARE_DEPLOY_LOCK`, default `$HOME/chudware-web-data/logs/deploy.lock`)
serializes users of that same configured lock; separate staging and production
identities need an independently provisioned shared-checkout/lock policy if
they share any mutable checkout.

Staging defaults to gateway-only. Production promotes the joint cell-engine/WASM
payload, never restarts the optional shared local/native daemon. `--continuous`
waives only the exact `v*` tag requirement; it does not waive runtime, admin,
lease, work-census or rollback-authority gates. Production must choose a pinned
tag or continuous policy, not both; staging rejects either production policy.
Health is restricted to the selected loopback gateway (8300 prod, 8310 staging).

`<selected-data-root>/logs/deploy-status.json` records the last deploy's
`release`: the `v*` tag for a pinned run, the full commit sha for a continuous
one. In continuous mode that sha is the only name for what shipped, so read it
before assuming which commit production is on. The `/admin` System panel reads
that snapshot and `<selected-data-root>/logs/update.log`, including the release
identifier. A recorded `running` value is not a live work/lock census; the browser
does not rewrite old status records or use them to authorize a cutover.

Exercise health refusal locally with
`pytest Webservice/deploy/tests/test_process_credentials.py::test_postmutation_failure_remains_failed_and_keeps_maintenance_closed`
and `::test_private_fd_health_requires_the_selected_admin_identity_without_secret_artifacts`.
These use disposable loopback peers and private descriptors; do not deliberately
break a live staging health route to test refusal.

`install_webd.sh` builds the WASM client and retained cell-engine payload as one
joint release. Promotion runs under the durable deployment-maintenance lease:
accepted work must settle, every supported cell must self-exit, and actual
inactivity must be observed before cutover. Unknown activity refuses promotion.
The script neither enables nor restarts the shared local/native daemon. It
retains the newest `CHUDWARE_WEBD_KEEP_RELEASES` releases (default 3) under
`~/chudware-web/releases/`; rollback additionally requires authenticated release
authorization and compatible gateway/schema recovery.

### Protected maintenance lease and cutover authority

`update.sh`, `install_webd.sh` (with or without `--restart`) and manual
`rollback.sh` use `deployment_drain.sh` and
`gateway_preflight.py deployment-drain`; `--restart` is not a shared-hostd restart.
The unsafe browser build/restart shortcuts and their shell scripts have been
removed: `POST /api/admin/deploy/rebuild` and
`POST /api/admin/deploy/redeploy` are no longer routes, even for a production
admin. There is no browser replacement launcher. Use the qualified deployment
path above, not bare `systemctl restart/stop`, hostd `--stop/--force`, or a
disconnected-browser/bridge-only idle guess. Old admin rebuild/restart logs may
remain on disk as historical records; the browser now reads the operator's
`update.log`, not those retired jobs' output.

The gateway persists its lease in
`<CHUDWARE_WEB_DATA_ROOT>/.deployment-maintenance/lease.json`, outside every
tenant-mounted root (directory 0700, file 0600). The lease is a random 64-hex
ownership token; only an authenticated active admin plus the matching token can
begin/status/exit/release it. Another token conflicts. Missing, malformed, unsafe
or unexpectedly disappearing established state closes admission. Gateway restart,
disk-monitor `REFUSE_NEW` removal and passage of time do not release maintenance.
Do not delete/edit the state file to open admission or claim another operator's
lease.

The lifecycle is:

1. `prepare` calls `POST /api/_ops/deployment-drain/begin`, closing new HTTP,
   WebSocket and gateway-job admission without cancelling already accepted work.
   Accepted streaming bodies, entire WebSockets and executor/background work
   retain their admission until actual completion, even after wrapper cancellation.
   Health, authenticated `GET /api/me` and protected operator routes remain available.
2. `GET /api/_ops/deployment-drain/status?lease_id=…` counts
   `http_requests`, `gateway_jobs`, `engine_bridges` and `provider_streams`.
   Only after all are zero does it close each cell's native admission, recording
   its manager `InvocationID` and drain generation in the durable lease.
   Status is therefore a maintenance operation, not a read-only telemetry probe.
3. The gateway enumerates actual `chudware-cell@*.service` units with the selected
   user manager and uses each hostd-owned private `operator.sock` for
   `cell_census`/`cell_drain`; connecting that socket cannot activate an inactive
   cell. A cell's `active`/aggregate `cell_jobs` includes independent pins:
   non-control **sessions**, RPCs in flight, native cache verification, profiler
   tasks, **dirty documents**, busy workers, background jobs and unknown workers.
   Closing browser sockets does not end agent/native/background work or save dirty
   docs. Cached SDK processes alone are not work only when their settle/queue/STT
   and process ownership are positively attested idle; an unavailable/malformed
   SDK or process census remains unknown.
4. `complete=true`, `quiescent=true`, all five work counters zero,
   `cells_unknown=0` and `cells_exiting=0` permit the protected
   `POST …/exit-cells` request. Each cell rechecks its generation and work boundary,
   then **self-exits when quiescent**; no unit stop, signal, job abort or deadline
   escalation is used. An exit acknowledgment or disappearing socket is not
   inactivity proof. The helper waits for a complete subsequent manager census
   with `cells_active=0`, `cells_unknown=0` and `cells_exiting=0` before mutation.
   Manager errors, transitions, PID/invocation changes, unknown native/worker
   capability or contradictory census pin the deployment.
5. Following mutation/restart, `complete` requires that same inactive-cell proof,
   exact anonymous `/healthz == {"ok":true}`, and authenticated `/api/me` with a
   nonempty admin email (matching the configured health email when specified).
   Only then does `POST …/release` reopen admission. Reopening a closed live cell
   requires the lease's matching invocation/generation; unknown/exiting cells
   refuse release. The optional shared native daemon never participates.

`CHUDWARE_WEBD_RESTART_MAX_WAIT` defaults to 600 seconds (CLI `--max-wait`
accepts 0–3540). It bounds the **operator wait**, never customer work or agent
lifetimes. A timeout fails deployment instead of making unknown/busy work idle.
`CHUDWARE_DEPLOY_HEALTH_ATTEMPTS` defaults to 30 (1–300). Scripts carry the
private unlinked mode-0600 regular-file lease on fd 8 and the flock on fd 9.
`CHUDWARE_DEPLOY_SESSION_FD` optionally names a separately supplied protected
admin-session descriptor; otherwise the helper creates/revokes a real temporary
admin session from protected configuration. It never prints session/lease values
or uses a synthetic encryption key. `CHUDWARE_DEPLOY_HEALTH_EMAIL` (fallback
`CHUDWARE_WEBD_HEALTH_EMAIL`) selects the admin; otherwise it selects an active
admin from the environment's database.

**Failure ownership:** a premutation refusal releases only its own lease; nested
consumers (`DRAIN_BORROWED`) never release their caller's lease. After mutation,
failure keeps maintenance closed and does not automatically restore gateway
source/dependencies/schema or stop cells. If a release reply is lost, the script
tries to reclose with the same lease and reports an unconfirmed reclosure as an
unsafe failed rollout. A failed premutation release likewise reports unknown
admission. Neither condition is success or permission to force work.

Protected recovery requires the **same** lease in an inherited owner-only,
unlinked regular-file descriptor, a qualified compatible gateway/payload, and
preserved customer data. The following are the existing helper commands, not a
token-export or lost-descriptor recovery recipe; fd 8 must already contain that
privately recovered matching lease:

```bash
gateway_python="${CHUDWARE_GATEWAY_PYTHON:-$PWD/Webservice/gateway/.venv/bin/python}"
gateway_env="${CHUDWARE_WEB_GATEWAY_ENV:-$HOME/chudware-web-data/gateway.env}"
"$gateway_python" -I Webservice/deploy/gateway_preflight.py deployment-drain status \
  --env-file "$gateway_env" --environment prod --lease-fd 8 --port 8300
"$gateway_python" -I Webservice/deploy/gateway_preflight.py deployment-drain check \
  --env-file "$gateway_env" --environment prod --lease-fd 8 --port 8300
# Only after independently completing compatible recovery:
"$gateway_python" -I Webservice/deploy/gateway_preflight.py deployment-drain complete \
  --env-file "$gateway_env" --environment prod --lease-fd 8 --port 8300
```

`begin` closes the same lease without waiting/exiting; `prepare` performs the
full quiescence/self-exit/inactivity handshake; `check` proves closed/inactive;
`health` checks liveness/admin identity only; `complete` checks inactivity and
health before release. Raw `release` can reopen generation-fenced live cells
even while accepted work remains and **does not check quiescence, recovered
payload/schema or health**: it is only the
premutation abandonment operation, not postmutation recovery approval.
For staging use its separate identity/configuration with `--environment staging
--port 8310`. A terminated shell loses its anonymous descriptor; this source
provides no turnkey credential-custody/lost-lease recovery command. Reacquiring
that existing token from protected gateway-private state requires independently
authorized operator custody, never tenant access or disclosure into logs/argv.

Desktop against the optional **shared local/native** daemon (not a browser gateway consumer):

```bash
~/chudware-web/chudware-shared.sh
```

### Roll back a bad production release

`rollback.sh --automatic` always exits **5** for operator recovery: retained
native/cell bytes do not authenticate compatible gateway checkout/dependencies or
database schema, even without a mutation marker. Do not assume production has
automatically returned to the previous release. Establish the installed payload
and failure/lease state before touching anything:

```bash
cat ~/chudware-web/.cell-payload-installed-joint  # installed payload identity, not a running/shared-daemon assertion
readlink -f ~/chudware-web/current        # promoted release directory
grep -E '^(release_id|joint_sha256|checkout_head|built_at_utc)=' \
  ~/chudware-web/current/deploy-manifest
cat ~/chudware-web-data/logs/deploy-status.json   # last deploy: rc + release (tag or sha)
bash Webservice/deploy/rollback.sh --list # retained releases, newest first, current marked
```

If `--list` shows only one release, skip to "When there is no way back" below.

Authorize each independently qualified known-good schema-7 release before relying
on it for rollback. The following records its reviewed identity; merely recomputing
a service-owned manifest/tree hash is not proof of safety or independent approval:

```bash
release=~/chudware-web/releases/<release-id>
python3 -I -S ~/Repositories/CAD/Webservice/deploy/deployment_release.py tree-digest "$release"
grep -E '^(joint_sha256|release_tree_sha256)=' "$release/deploy-manifest"
sudo install -d -o root -g root -m 0755 /var/lib/chudware-deployment-trust/prod
printf '%s\n' <release_tree_sha256> | sudo tee /var/lib/chudware-deployment-trust/prod/<joint_sha256> >/dev/null
sudo chmod 0644 /var/lib/chudware-deployment-trust/prod/<joint_sha256>
```

Use `staging` instead of `prod` for staging releases. Every authorization record
and ancestor must be root-owned and not group/world-writable; its contents are
exactly the full runtime-tree SHA-256 plus LF, at
`/var/lib/chudware-deployment-trust/<environment>/<joint_sha256>`.
Never create this authority from service-account automation.

`promotion-preflight --web-home WEB_HOME --environment prod --target-commit SHA`
(in `deployment_release.py`, invoked with `/usr/bin/python3 -I -S`) validates
and authorizes the current schema-7 payload. If none qualifies, an operator may
independently provision `--first-cutover-ack
/var/lib/chudware-deployment-trust/prod/first-cutover/<exact-40-hex-commit>`.
The protected root-owned record must contain exactly:

```text
schema=1
target_commit=<exact-40-hex-commit>
recovery_scope=manual_gateway_checkout_and_database_schema
verified_off_host_encrypted_backup=yes
isolated_restore_and_schema_compatibility_drill=yes
prior_gateway_source_and_dependencies_available=yes
maintenance_and_customer_data_reconciliation_plan=yes
operator_accepts_no_automatic_full_rollback=yes
```

Those are independently verified operator prerequisites, not fields to assert
because a local test passed. The tool never writes the acknowledgment. Supply
its exact path to `update.sh`/`install_webd.sh --first-cutover-ack PATH`; it waives
only the missing prior-payload gate, not maintenance/runtime/health or manual
gateway/schema recovery requirements.

Roll back:

```bash
# newest retained release that is not the current one:
bash Webservice/deploy/rollback.sh

# or a specific release id from --list:
bash Webservice/deploy/rollback.sh --to 20260822T171130Z-4f2a9c1d3b07-8241
```

Exit `0` restores the qualified payload and completes lease health/release;
`1` is postmutation failure with maintenance retained; `2` is usage/no target;
`4` is premutation refusal; `5` is automatic-mode operator recovery required.
There is **no `--force` option**. Busy or unknown work refuses rollback using
the same maintenance handshake and bounded operator wait as promotion.
An atomic `current` swap prevents a missing-link gap, not partial gateway/schema
recovery or proof that a failed rollout is healthy.

With no `--to`, `rollback.sh` selects the newest independently authorized
candidate, printing each unusable candidate. No qualifying target exits `2`;
an explicitly named unqualified target refuses before mutation (`4`), never
silently selects another. A restore promotes a new private `*-rollback-*`
snapshot, installs only its authorized cell/egress templates, writes
`.cell-payload-installed-joint`, and restarts the selected gateway under the
lease. It never stops/restarts the optional shared native daemon or replaces
gateway source/dependencies/schema.

Confirm it took:

```bash
systemctl --user is-active chudware-gateway
curl -fsS http://127.0.0.1:8300/healthz     # liveness only; helper also checked authenticated admin identity
cat ~/chudware-web/.cell-payload-installed-joint  # must equal the restored payload's joint_sha256
python3 ~/Repositories/CAD/scripts/check_web_parity.py --bundle ~/chudware-web/app
```

`check_web_parity.py` exits `0` for parity, `1` for divergence and `2` when it
cannot evaluate; `2` is unresolved, never passing. These read-only observations
are not genuine signing/provider/CI/runtime/host authority. The rollback script
itself requires inactive cells and authenticated health before reopening.

Rollback does **not** undo gateway code/dependencies or a database migration.
Never blindly restore `pre-deploy-prod-<UTC>.dump` over current customer data.
A gateway/schema failure requires the independently drilled compatible recovery
and data-reconciliation plan, retaining the maintenance lease until health and
inactivity are proven.

**When there is no qualified prior payload.** Historical retention deleted prior
releases; a single or corrupt retained directory is not a safety net. Fix-forward
still needs independent exact-commit first-cutover acknowledgment, authenticated
runtime inputs and the same lease/census gates. An unavailable gateway or lost
operator descriptor is not permission to run a forced rollback. Review retention
and qualification before an incident; raising `CHUDWARE_WEBD_KEEP_RELEASES`
(default 3, minimum 2) preserves bytes, not rollback authority.

---

## Layout reference

| Path | Role |
|------|------|
| `Webservice/deploy/setup.sh` | Idempotent dirs, venv, unit install, enable |
| `Webservice/deploy/update.sh` | Exact-ref/configuration/authority preflight → durable maintenance and inactive cells → encrypted dump/checkout/migration/payload → selected gateway restart → authenticated health/release |
| `Webservice/gateway/deploy_ops.py` | Authenticated read-only operator deployment status and `update.log`; no browser build/restart routes or launchers |
| `Webservice/deploy/install_webd.sh` | Retained joint cell-engine/WASM/runtime payload and cell templates; maintenance-gated promotion, no shared-daemon operation |
| `Webservice/deploy/rollback.sh` | `--list` / independently authorized payload snapshot and cell templates; maintenance-gated selected gateway restart, never full gateway/schema rollback |
| `Webservice/deploy/deployment_drain.sh` / `gateway_preflight.py` | Private lease/session descriptors, protected admin operations, fail-closed work census and recovery lifecycle |
| `Webservice/deploy/chudware-gateway{,-staging}.service` | Production 8300 / staging 8310 systemd **user** units |
| `Webservice/deploy/monitor.py` | Host sample + disk-pressure flag (stdlib) |
| `Webservice/deploy/cf_provision.py` | Driver: tunnel/DNS/Pages via API — no Access application |
| `~/chudware-web-data/gateway.env` | Production DB/session/Stripe/OAuth settings and hardened gateway flags (never commit) |
| `~/chudware-web-data/cloudflared.env` | Tunnel token only; mode 600; loaded only by cloudflared |
| `~/chudware-web-staging/gateway.env` | Staging DB/session secret, disabled attack surfaces, and test-only settings (never commit) |
| `~/chudware-web-data/{workspace,bucket,plm,logs}` | Runtime file state |
| PostgreSQL (`CHUDWARE_DB_URL`) | Accounts, chat history, webhook events, credentials, sessions, email tokens, mail outbox |

## AI modeling agent (gateway-owned BYOK provider broker)

Browser turns currently require an account's **validated BYOK** key for
`anthropic`, `openai`, `xai`, `openrouter` or `gemini`. `GET /api/providers`
lists only those supported validated account keys and reports
`shared_host_accounts:false`. Pool, linked/sponsored subscription and ambient
operator credentials are not tenant-cell execution capabilities; without a
supported validated key, `agent_admin_turn` refuses
`credential_isolation_unsupported` ("BYOK provider key required").
Keep `CHUDWARE_WEB_SHARE_PROVIDER_KEYS=0`; setting a sharing/pool variable does
not grant a browser turn authority.

`gateway/engine_bridge.py` rejects reserved client identity/secret/grant fields
and mints a gateway-authenticated `cpg1` grant after resolving the live principal.
Only that grant, never decrypted BYOK/OAuth/operator key bytes, crosses into the
cell/actor worker and Chudcode. It binds cell, workspace, actor, account UID,
epoch, workspace right, team, provider, BYOK source and role. Worker credential
identity is (workspace, actor, provider), not key bytes, so key rotation does
not change cancel/resume ownership.

`gateway/provider_broker.py` owns mode-0600
`<cells-root>/<cell-id>/provider.sock` listeners inside the public gateway process;
this ASGI application is **not a public gateway route**. Cell/actor forwarders
reach fixed `http://127.0.0.1:18081/<provider>/…` endpoints through the mounted
private socket. The grant replaces SDK auth only for the local broker. The broker
revalidates current account/role/epoch/right/team entitlement and validated key
before requests, after slow uploads and every second while a transfer runs.
It resolves the newest rotated key on the host and attaches it only to the
canonical provider endpoint/allowlisted model or generation route. Revocation
terminates the transfer; an interrupted stream is failure, never a fabricated
terminal reply. Chudcode retains native provider routing and SDK parsing.

The broker ignores ambient HTTP proxy configuration, does not follow redirects
or retain cookies, and restricts forwarded headers/routes. Successful provider
body/SSE bytes are forwarded unchanged without accumulated stream history;
upstream error bodies are not exposed because they could reflect secrets.
Limits are 64 active requests, 4 per actor, 16 MiB request/non-SSE response bodies
and bounded connect/upload/header operations, not an agent/SSE wall-clock lifetime
cap. `provider_streams` remains pinned through actual transfer cleanup.

Actor `HOME`/XDG/agent state remains private, Ask/Plan `/work` is readonly,
browser/computer overlays are off, and no host login enters the cell. The distinct
CONNECT egress broker at port 18080 is for public HTTPS research transport; it
does not inject provider credentials or replace the port-18081 auth broker.
Desktop turns (no web actor) retain their normal Chudcode login.

Real-SDK local provider-wire fixtures can demonstrate streamed bytes, preserved
thinking/signature strings, cache-prefix declarations and persisted session
identity. They do **not** authenticate a genuine provider thinking signature,
prove an actual provider cache hit, account readiness/billing, live external
service deliverability, or authorize a production deployment.

## University of Alabama Box workspace

Production remains `livingroom-pc`, user `liveuser`, gateway port 8300; do not
start the inactive development gateway or restart unrelated services.
`provision_university_workspace.py` uses the existing production database and
environment to create the University of Alabama organization and ME-490 team.
The existing paid operator is the org owner/funder; the already verified
`haedmonds@crimson.ua.edu` identity is a team manager, not a platform admin.
No credential, account plan, credit balance, AI grant or provider setting changes.

The three `CHUDWARE_WEB_UNIVERSITY_{ORG_ID,TEAM_ID,WORKSPACE_ROOT}` settings
enable automatic institutional enrollment. For that enrollment, active accounts
must have a verified password credential or a linked identity produced by the
existing verified OAuth flow.
Eligibility matches exactly `ua.edu` or valid dot-subdomains, including
`crimson.ua.edu`; self-declared addresses, lookalikes and suspended users fail.
Eligible users enroll as ordinary org/team members. This is a domain admission
policy: deleting a membership alone does not revoke domain eligibility; suspend
the account to revoke its access. Existing organization owners/admins and explicitly
invited team members also have access through ordinary membership checks, without
requiring a UA address. Removing an external member revokes that member's access;
platform-admin status alone never grants team access. Team writes retain the live
organization owner's active Studio/Enterprise funding check.

Default `/workspace/*`, bucket files, portal workspace metadata and `/engine`
resolve the same team root. Explicit `team=<uuid>` or `scope=personal` wins;
`POST /workspace/selection` stores the browser's authenticated selection.
`/app/?team=<uuid|personal>` selects it when launching the browser app.
Chat history stays private in the account's personal tree. Expired-trial UA
access is limited to the portal/workspace surfaces; it does not renew the
trial, grant desktop licenses, enable other services, or authorize AI turns.

Install `chudware-university-box.service` as a `liveuser` user unit with rclone
at `~/chudware-web-data/university/bin/rclone` and an independently authorized,
mode-0600 Box config at `~/chudware-web-data/university/secrets/rclone.conf`.
The private remote pins `root_folder_id = 417549774517`, the dedicated
`Team 5 Human-Powered Rover Program Development and Sustainability/Chudware Workspace`
child; the unit therefore mounts `box:` at
`~/chudware-web-data/university/ME-490`. Existing teammate folders are untouched.
The mount uses persistent VFS full caching, 5-second deferred upload,
5-second Box change-notification polling and a one-hour directory-cache ceiling,
with recursive metadata refresh on startup. The earlier 15-second ceiling
expired while a large quota scan was still traversing the tree; a post-upload
scan alone took about54 seconds. Change notifications provide live invalidation
without repeatedly re-fetching every unchanged remote directory.
Polling is required: without it a cached direct read remained stale until a
directory listing; with polling, a remote overwrite reached direct cached reads
in 6–13 seconds across the deployment smokes. Writes are asynchronous, not a promise of
instant remote durability; wait for the rclone upload queue to drain and verify
Box bytes before declaring a delivery. Concurrent external edits use Box
versions/last-write behavior, not CAD merge or distributed locking.
Mounted filesystem handlers run in FastAPI's threadpool, not the ASGI event
loop: quota scans and a slow remote file must not freeze other tenants or
`/healthz`. Upload caps and full owner-plan quota checks remain enforced.
The limiter is thread-safe. Bounded, root-keyed process locks serialize gateway
uploads/copies and tree mutations across both file APIs, including preflight
and final quota accounting; they are not distributed locks and cannot make
quota admission atomic against direct Box or native-daemon writers.
The corrected production smoke returned HTTP 200 for a public authenticated
upload in 67.316s while 268 concurrent loopback health probes had zero errors
and a maximum latency of 2.19ms. This proves gateway responsiveness, not fast
per-upload accounting: full remote quota scans can still be slow. The upload's
Box bytes and bucket download matched, the smoke file was deleted remotely,
and the VFS queue/error counters finished at zero.

The rclone control socket is **Unix-only**, inside the mode-0700 secrets
directory, with mode0700 itself: `secrets/rc.sock`. It is not a TCP listener,
public route or anonymous Box share. `--rc-no-auth` here relies on that
same-user filesystem boundary; rclone RC is shell-equivalent authority and
must never be moved to a network listener without proper authentication.
Check pending uploads without reading credentials:

```sh
~/chudware-web-data/university/bin/rclone rc \
  --unix-socket ~/chudware-web-data/university/secrets/rc.sock vfs/stats
```

Require `diskCache.uploadsQueued = 0`, `uploadsInProgress = 0`,
`erroredFiles = 0` and `outOfSpace = false` before stopping a delivery mount.
The original workstation's Box credentials remain untouched; its independent
OAuth grant is separate from the production mount's grant.

**Never copy an in-use Box refresh token to a second host.** Box refresh tokens
are one-use; see <https://rclone.org/box/#invalid-refresh-token>. Authorize a
separate OAuth grant, or explicitly transfer sole credential authority after
stopping all old consumers. Never make an anonymous share. Gateway resolution
requires a real mount (`Path.is_mount()`), so an absent mount returns 503
instead of silently writing an unsynchronized local directory. An offline
remote can still leave pending writes in the persistent VFS cache: preserve
that cache and inspect the service journal before stopping/replacing it.

After the curated project upload, start/enable only the university mount,
exercise a round-trip through the mount and Box, deploy the gateway at the
normal clean boundary, and sign in with the actual university OAuth account.
Check portal name/backing, Files and CAD browser roots, explicit Personal,
save/reopen in the Box-backed project, unverified/outside isolation, and account
plan/AI/admin invariants. Independent Box OAuth and actual project upload are
runtime prerequisites; source configuration alone is not a live deployment.

## Linux browser tenant cells (security release gate)

Browser CAD and AI must use the payload-installed `chudware-cell@.socket` /
`chudware-cell@.service`, never the shared desktop/webd socket. The gateway passes
only a lowercase 32-hex cell ID to the user manager. The socket unit owns
`<web-home>/cells/<id>/gw.sock` (0600); the cell inherits listener fd 3 and never
sees its host pathname. Cell state and workspace persist across restarts; do not
delete either as a repair step. Existing sessions must drain before release
promotion; installing these templates does not restart or enable any active cell.

Provision a service-owned mode-0600 `~/.config/chudware/cell-runtime.json`
(parent configuration directory mode 0700). Example, with **real absolute paths
and no symlinks**:

```json
{
  "schema": "chudware-cell-runtime-v1",
  "cells_root": "/var/lib/chudware/chudware-web/cells",
  "allowed_roots": ["/var/lib/chudware/workspaces", "/var/lib/chudware/buckets"],
  "quota_proof_root": "/etc/chudware/cell-quota.d",
  "egress_hosts": ["api.anthropic.com", "api.openai.com"]
}
```

`cells_root` must match the installed template's `<web-home>/cells` and gateway
`CHUDWARE_CELLS_ROOT`; staging uses its dedicated service account and web home.
`allowed_roots` must exactly match admitted personal/team/university workspace
bases, not the service home, gateway database, credential trees or cells root.
The gateway alone writes `cell.json`; launch checks its schema, workspace hash,
private ownership, all path components and root containment. Symlinked paths,
`..`, malformed IDs, missing configuration, missing broker or unavailable
bubblewrap fail closed. No unsandboxed fallback exists.
Mount sources are pinned through bubblewrap setup. Trusted isolated initializers
close every mount descriptor before executing workers; only the supervisor
retains gateway listener fd 3. No host-directory descriptor reaches actor code,
and no unsupported bubblewrap descriptor-preservation option is required.
The adjacent release-hashed `chudware-worker` starts only the authenticated
`/opt/chudware/venv/bin/python` with isolated Python mode, a fixed source bootstrap
and an explicit environment allowlist. It uses no `.pth` startup hook or ambient
Python dependency path.

The cell has a tmpfs root, separate user/PID/IPC/UTS/network namespaces, no
capabilities, a new `/proc`, readonly payload and system runtime directories,
readonly certificate trust directories, only null/zero/urandom devices,
and writable `/work` + `/state`. It has no host home, `/run/user`, D-Bus, gateway
data or ambient operator environment. Nested `chudware-actor-sandbox` maps only
that actor's state to `/actor`; Ask/Plan use readonly `/work`. Nested launches
cannot gain another actor or write access to an already readonly workspace.
Each actor has its own loopback network namespace and proxy forwarder; no proxy
is configured in the supervisor's environment.

The host-side socket-activated egress broker accepts only
`CONNECT <hostname>:443 HTTP/1.1`. All DNS answers must be public
unicast; mixed private/public answers refuse. Connection uses the numeric
resolved sockaddr, never a second hostname resolution. Every redirect requiring
a new tunnel is subject to the same rule. `egress_hosts` is an optional exact-host
allowlist: absent/null allows arbitrary public research destinations, an explicit
list restricts them, and an empty list denies all egress. There are no wildcard
entries, credential injection or operator provider tokens in this CONNECT broker.
It is public-HTTPS research transport, distinct from the gateway-owned BYOK
authentication broker at port 18081 described above. Boundaries are 32 concurrent
tunnels, 8 KiB CONNECT headers, 64 MiB total bytes per tunnel, 120 seconds stream
inactivity and 10 seconds handshake/connect inactivity; active streams have no
wall-clock lifetime cap. Clients must explicitly use the cell proxy or fail.

Cell service defaults are `MemoryMax=8G`, `MemorySwapMax=0`, `CPUQuota=200%`,
`TasksMax=512`, `IOWeight=50`; broker limits are 256 MiB / 50% CPU / 64 tasks.
Tune using operator-managed per-instance drop-ins only after reviewing aggregate
tenant capacity. Never add `RuntimeMaxSec`, `LimitAS`, `ulimit -v` or an agent
wall-clock timeout. Require delegated cpu/memory/pids/io controllers and verify
the effective properties on the deployment host. Inspect without stopping users:

```sh
systemctl --user show chudware-cell@<32-hex-id>.service \
  -p MemoryMax -p MemorySwapMax -p CPUQuotaPerSecUSec -p TasksMax -p IOWeight
journalctl --user -u chudware-cell@<32-hex-id>.service
journalctl --user -u chudware-egress-broker@<32-hex-id>.service
```

**Hard disk quota is a root-provisioned admission gate.** Provision a complete
`chudware-cell-quota-plan-v1` owner inventory covering workspace, cell-state and
bucket roots. On one filesystem they share the owner's project budget; split
filesystems must have independently enforced limits whose sum fits that budget.
Only enforced XFS/ext4 project quotas are admitted. Accounting, cgroup IO
weighting, FUSE/Box roots, bind/idmapped aliases and nested mounts are not substitutes.

Review the plan independently, protect it and its ancestors from service-user
writes, then inspect it with `provision_cell_quota.py --plan <protected-plan>
--proof-root /etc/chudware/cell-quota.d`. Root may add `--apply` to provision the
reviewed limits and write the protected `quota-policy.json`. The helper does not
mount storage, start services or migrate existing unattributed data. A nonempty
unsafe root requires an offline, separately reviewed migration that preserves
saved solve state; do not invent a project identity or discard cached builds.

Admission binds the proof to the boot, mount, device and root inode, verifies
inheritance, and binds the checked descriptors rather than reopening pathnames.
Re-provision the proof after a reboot or changed mount identity. Where the service
UID cannot inspect quota enforcement, only the root-controlled enforcement proof
is accepted; root remains trusted. Reserve backing capacity for all approved
budgets and filesystem overhead rather than relying on overcommitted quotas.

The unprivileged cell/actor user namespace is part of this boundary. An ordinary
owner in the initial user namespace can clear project inheritance on XFS; the
nested namespace must reject that mutation and project-ID changes. Do not run
tenant code directly as the service UID outside the cell. Qualification must
exhaust the actual kernel quota, show workspace and state share the owner budget,
and show another owner remains writable. Gateway upload accounting alone proves
none of those properties.

The worker runtime is staged from `ensure_runtime` with independently
authenticated `CHUDWARE_BASE_PYTHON=/usr/bin/python3`, the reviewed complete
`vendor/python/RUNTIME_WHEELS.lock` and an offline authenticated wheelhouse.
Missing/incomplete wheel identity (including chudcode-rpc) prevents staging;
the gateway venv, editable checkout, package index and source-build fallback
are never accepted. The host base interpreter, readonly `/usr/bin`, library
directories, stdlib and certificate trust are an operator-authenticated OS
prerequisite. Release payload copies materialize venv links so the existing
nonregular-file integrity rejection remains intact.

Release gates run only in an owned disposable environment, never against open
user sessions/providers/devices: existing `test_cell_runtime.py`,
`test_cell_storage_quota.py`, gateway principal/revocation, worker ownership,
warm-reopen and retained-release/rollback suites provide source/local fixture
proof. Real cell/storage qualification requires the explicit root-authorized
disposable quota helper and enforced project-quota storage; a prerequisite skip
is not acceptance. A namespace/policy failure with prerequisites present fails.
Actual browser acceptance must exercise the WASM canvas, tenant-safe CAD
save/reopen and real SDK path; source/HTTP/fixture success cannot substitute for
that surface. Preserve historical vulnerable audit artifacts unchanged.

Even successful disposable browser/SDK/census/quota evidence does not certify
genuine provider signatures/cache hits, approved private runtime wheel/image
authority, Windows/macOS native signing, authenticated protected CI, installed
host/ingress/rotation policy or off-host recovery. Current deployment requires
those independently supplied attestations plus installed template/cgroup/quota
and BYOK isolation proof; unavailable collectors/prerequisites remain unresolved,
not zero or passing. See `docs/RELEASING.md` for the separate authority gates.

