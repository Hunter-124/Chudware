# chudware-web RUNBOOK (Hunter)

Host: Fedora 44. Production gateway: `127.0.0.1:8300`, data
`~/chudware-web-data/`. Staging gateway: `127.0.0.1:8310`, data
`~/chudware-web-staging/`.
Public portal: `https://app.chudware.com` through the tunnel. The marketing site
at `https://chudware.com` and `https://www.chudware.com` is Cloudflare Pages.
Staging ingress is `https://staging.chudware.com`.

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

Run `sudo Webservice/deploy/harden_host.sh --apply launch` on `livingroom-pc`.
The `launch` profile is intentionally limited to `firewall`, `tunnel`, `updates`,
`keys`, `backups`, and `gateway`; it cannot apply the two owner-declined controls.

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
- **Backups:** keep the existing timer. It encrypts every artifact with `age`
  before writing it and contains the environment-gated off-box R2 push. Do not
  build a parallel plaintext backup path.

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

### Accepted residual risk

`liveuser` deliberately remains in `wheel` with `NOPASSWD: ALL`, and sshd
deliberately keeps password authentication enabled. Therefore a remote-code-
execution vulnerability in the gateway is still a root compromise of the
production host. The controls above shrink exposure and remove the highest-value
LAN pivot, but they do **not** eliminate that root-compromise path. Revisit the
declined `sudo` and `sshd` items before treating the host as strongly isolated.

## (b) Cloudflare — what is already provisioned vs manual

### Pre-provisioned (driver / `Webservice/deploy/cf_provision.py`)

Do **not** hand-create these if provision already ran:

| Item | Value / note |
|------|----------------|
| Zero Trust org | exists |
| Team domain | `https://bruh234324.cloudflareaccess.com` |
| One-Time PIN IdP | already enabled |
| Zone | `chudware.com` active |
| Tunnel | `chudware-web` (remotely-managed) |
| Production ingress | `app.chudware.com` → `http://127.0.0.1:8300` |
| Staging ingress | `staging.chudware.com` → `http://127.0.0.1:8310` |
| Catch-all ingress | `http_status:404` — no apex, www, `cad.scootware.us`, or `sso.chudware.com` origin route |
| Pages DNS | `chudware.com`, `www.chudware.com` → `chudware-site.pages.dev` |
| Access application | **none.** `/admin` is gated by the gateway itself; provisioning neither creates nor rewrites an Access app, so there is no gateway AUD to keep unique |

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
`python -m authn grant-admin` (§c). Cloudflare Access is a *fallback* credential the
gateway still accepts from a caller with **no** session — set
`CHUDWARE_WEB_CF_TEAM_DOMAIN` + `CHUDWARE_WEB_CF_AUD` by hand if you want it, and the
gateway verifies `Cf-Access-Jwt-Assertion` against team JWKS
(`…/cdn-cgi/access/certs`) with `iss` = team domain and `aud` = `CHUDWARE_WEB_CF_AUD`.
JWKS rotates ~every 6 weeks (previous key valid ~7 days) — always fetch remotely,
match `kid`. Leaving both unset is the supported configuration.

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
   changes ingress/DNS/Pages only — it no longer touches Access at all, so the
   leftover `app.chudware.com/admin*` application must be deleted by hand in the
   Zero Trust dashboard, otherwise Cloudflare keeps showing its own login page in
   front of the gateway's.
5. From an incognito browser, re-check public marketing pages at the apex,
   session-cookie login at `app.chudware.com`, an anonymous `GET /admin`
   redirecting to `/signin?next=%2Fadmin` (not to `cloudflareaccess.com`),
   admin-role enforcement at `/admin`, and staging isolation.
   `Webservice/deploy/verify_public.sh` asserts exactly this.

This repository change records the cutover but does **not** apply it to live
Cloudflare.

### Hunter remaining manual steps

1. **Rotate leaked API token + R2 keys** (§a) — do this before relying on any old token in scripts.
2. **Delete the `app.chudware.com/admin*` Access application** in Zero Trust →
   Access → Applications. `cf_provision.py` deliberately stopped managing it, so
   nothing in this repo will remove it for you, and while it exists Cloudflare
   answers `/admin` with its own login page instead of the gateway's.
3. **Grant the admin role** to each operator address, which is now the *whole*
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

Staging is a second **systemd user** service, not a development mode of
production:

| | Production | Staging |
|---|---|---|
| Unit | `chudware-gateway` | `chudware-gateway-staging` |
| Loopback | `127.0.0.1:8300` | `127.0.0.1:8310` |
| Data root | `~/chudware-web-data` | `~/chudware-web-staging` |
| Database | `chudware_prod` | `chudware_staging` |
| Mail | configured delivery backend | `console` |
| Stripe | live when provisioned | test key or disabled |

Install/update the unit atomically, then enable it:

```bash
install -Dm644 Webservice/deploy/chudware-gateway-staging.service \
  ~/.config/systemd/user/chudware-gateway-staging.service
systemctl --user daemon-reload
systemctl --user enable --now chudware-gateway-staging.service
```

`~/chudware-web-staging/gateway.env` is mode 600 and must contain
`CHUDWARE_DB_URL` for the `chudware_staging` role/database, an independent
`CHUDWARE_WEB_SESSION_SECRET`, and `CHUDWARE_DEPLOY_HEALTH_EMAIL` naming an
active account in that database. Set `CHUDWARE_MAIL_BACKEND=console`,
`CHUDWARE_WEB_TERMINAL_ENABLED=0`, and
`CHUDWARE_WEB_SHARE_PROVIDER_KEYS=0`. Leave Stripe unset until test credentials
exist; if configured, every Stripe secret and price must be a test-mode object.
The unit rejects an `sk_live_` secret.

The initial health identity may be an operational account with no password or
OAuth identity; it exists only so `python -m authn ops-session` can exercise
the same server-side session path as a customer. Do not copy an account or a
session row from production.

Deploy and verify:

```bash
bash Webservice/deploy/update.sh staging
systemctl --user is-active chudware-gateway-staging
curl -sS http://127.0.0.1:8310/healthz
pid=$(systemctl --user show -p MainPID --value chudware-gateway-staging)
tr '\0' '\n' </proc/$pid/environ | sed -n 's/^CHUDWARE_DB_URL=.*/CHUDWARE_DB_URL=<redacted>/p'
```

For the database-isolation proof, do not trust `gateway.env`: inspect the
running process as above, then parse the database name from that process's
`CHUDWARE_DB_URL`. It must be `chudware_staging`, never `chudware_prod`.

---

## (e) Demo-day checklist

- [ ] Secrets rotated; `gateway.env` has `CHUDWARE_WEB_SESSION_SECRET`; `cloudflared.env` has `TUNNEL_TOKEN` (both mode 600).
- [ ] `bash Webservice/deploy/setup.sh` already run; linger enabled.
- [ ] `systemctl --user start chudware-gateway chudware-cloudflared chudware-monitor.timer`
- [ ] `curl -sS http://127.0.0.1:8300/healthz` — free RAM, VRAM, engine bridges look sane.
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

- A Cloudflare login page means the old `app.chudware.com/admin*` Access application
  still exists; delete it in Zero Trust (§b manual steps). The gateway no longer
  provisions it, so nothing here removes it for you.
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

### Engine daemon (chudware-hostd) logs

```bash
systemctl --user status chudware-hostd.service
journalctl --user -u chudware-hostd.service -n 200 --no-pager
```

One shared daemon serves every web client over a unix socket; `/healthz`
reports `engine_bridges` (live `/engine` websockets).

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
systemctl --user restart chudware-gateway

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
`CHUDWARE_STATUS_BUCKET` / `CHUDWARE_STATUS_KEY`). Configure the monitor unit
with `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, and `R2_ENDPOINT`; the object
must be routed to the Pages `https://chudware.com/status.json` path. Until R2
is enabled (Cloudflare currently returns error 10042), the sampler still
appends its local JSONL record but exits 2 and names every missing credential
on stderr rather than pretending the public snapshot was updated. `--dry-run`
prints the exact public body without touching R2.

`.github/workflows/prod-external-watch.yml` runs every five minutes on
`cloudcastle-server` (`cc-artifact-host`), never on the production host. It
checks both `https://app.chudware.com/healthz` and the operational Pages status
blob. Failures (including DNS, TLS, timeout, malformed or degraded JSON) are
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

### Shared /admin terminal (disabled in production)

`CHUDWARE_WEB_TERMINAL_ENABLED=0` is part of the production and staging
hardening baseline. With that setting the terminal routes are unavailable. Do
not enable this merely for convenience: it exposes an interactive shell as the
gateway account, and that account currently has passwordless sudo.

If an operator deliberately sets the flag to `1` for a bounded maintenance
window, each session is a named **tmux** session on the gateway host (socket
`$CHUDWARE_TMUX_SOCKET`, default `chudware`). Sessions survive browser
disconnects and gateway restarts, and multiple admins can attach concurrently.
REST endpoints are `GET/POST /api/admin/terminal/sessions` and
`DELETE /api/admin/terminal/sessions/{name}`; WebSocket
`/admin/terminal?name=<s>` attaches. `tmux` must be installed. Set the flag back
to `0`, restart the gateway, and kill leftover sessions when maintenance ends:

```bash
tmux -L chudware kill-server
systemctl --user restart chudware-gateway
```

### Nightly backups

`chudware-backup.timer` (03:30 + jitter, `Persistent=true`) runs
`Webservice/deploy/backup.sh`:

- `pg_dump -Fc` of **both** databases (`chudware_prod`, `chudware_staging`), plus
  a tar of the non-database state in `~/chudware-web-data/` (PLM bare repos,
  per-account workspace and bucket trees) minus `logs/` and `tmp/`.
- Every artifact is piped through `age -r <recipient>` before it touches disk.
  The recipient is `CHUDWARE_BACKUP_AGE_RECIPIENT` or
  `~/chudware-web-data/secrets/backup_age_recipient`; with neither, the script
  refuses to run rather than write plaintext.
- **`secrets/` and every `*.env` are deliberately excluded.** They hold the
  PostgreSQL role passwords, the session secret and the Stripe/OAuth keys — the
  credentials that protect the dump they would otherwise sit beside. One leaked
  archive must not be both the lock and the key. Back those up through your own
  password store. Do not "fix" the excludes.
- `homes/` spans several uids (hostd writes under a subuid), so the tar
  elevates through `sudo -n` when it finds an unreadable file. Without that it
  exits 2 and the night's backup is silently lost — which is exactly what was
  happening before 2026-08-10.
- Destination `~/chudware-web-backups/` (mode 700, artifacts 600), newest
  **7** of each family kept. Override via `CHUDWARE_WEB_BACKUP_DIR` /
  `CHUDWARE_WEB_BACKUP_KEEP`.
- Uploaded to the R2 bucket `chudware-backups` (30-day lifecycle rule, applied
  idempotently on every run) when `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`
  and `R2_ENDPOINT` are set. Without them the run says so loudly and keeps the
  local copy.

```bash
systemctl --user start chudware-backup.service     # manual backup now
systemctl --user list-timers chudware-backup.timer # next run

# restore a database:
age -d -i <identity> < ~/chudware-web-backups/chudware_prod-<stamp>.dump.age > /tmp/prod.dump
pg_restore -h 127.0.0.1 -U chudware_prod -d chudware_prod --clean --if-exists /tmp/prod.dump

# restore the file state:
age -d -i <identity> < ~/chudware-web-backups/chudware-web-data-<stamp>.tar.age \
  | tar -x -C ~/chudware-web-data
```

The age **identity** (private key) must live off this machine. Keeping it in
`secrets/` next to the archives it decrypts defeats the encryption for any
attacker who gets the disk.


## (h) Pull / rebuild / redeploy

The same script deploys both environments:

```bash
# Automatic after green linux-gate.yml on canonical master:
bash Webservice/deploy/update.sh staging

# Explicit workflow_dispatch only; the tag is mandatory and re-verified at HEAD:
bash Webservice/deploy/update.sh prod --tag v1.2.0

# Operational variants retain the same environment guardrails:
bash Webservice/deploy/update.sh staging --no-pull --gateway-only
bash Webservice/deploy/update.sh prod --tag v1.2.0 --pull-only
bash Webservice/deploy/update.sh prod --tag v1.2.0 --web-only
```

Each gateway deploy takes `backups/pre-deploy-<environment>-<UTC>.dump`,
migrates that environment's database, restarts its unit, and asserts both
anonymous `/healthz` and authenticated `/api/me`. Logs and status live under
the selected data root; the flock is shared under
`~/chudware-web-data/logs/deploy.lock` so two environments cannot mutate the
single checkout concurrently. Staging defaults to gateway-only. Production
keeps the joint native hostd/WASM promotion and refuses any HEAD that is not an
exact `v*` tag.

To prove the fail-closed assertion without changing code, set
`CHUDWARE_DEPLOY_HEALTHZ_PATH=/deliberately-broken` and
`CHUDWARE_DEPLOY_HEALTH_ATTEMPTS=1` for a `--no-pull --gateway-only` staging
run. It must exit nonzero; immediately rerun without those overrides.

`install_webd.sh` builds the WASM client and promotes the engine in one joint
release, then restarts `chudware-hostd` only when the joint sha changed.

Desktop against the **shared** daemon (same engine the web client uses):

```bash
~/chudware-web/chudware-shared.sh
```

---

## Layout reference

| Path | Role |
|------|------|
| `Webservice/deploy/setup.sh` | Idempotent dirs, venv, unit install, enable |
| `Webservice/deploy/update.sh` | Environment-aware dump → pull → migrate → restart → health assertion |
| `Webservice/deploy/rebuild.sh` / `redeploy.sh` | Admin-locked rebuild targets / production gateway restart |
| `Webservice/deploy/install_webd.sh` | Promote engine build and install hostd plus both gateway units |
| `Webservice/deploy/chudware-gateway{,-staging}.service` | Production 8300 / staging 8310 systemd **user** units |
| `Webservice/deploy/monitor.py` | Host sample + disk-pressure flag (stdlib) |
| `Webservice/deploy/cf_provision.py` | Driver: tunnel/DNS/Pages via API — no Access application |
| `~/chudware-web-data/gateway.env` | Production DB/session/Stripe/OAuth settings and hardened gateway flags (never commit) |
| `~/chudware-web-data/cloudflared.env` | Tunnel token only; mode 600; loaded only by cloudflared |
| `~/chudware-web-staging/gateway.env` | Staging DB/session secret, disabled attack surfaces, and test-only settings (never commit) |
| `~/chudware-web-data/{workspace,bucket,plm,logs}` | Runtime file state |
| PostgreSQL (`CHUDWARE_DB_URL`) | Accounts, chat history, webhook events, credentials, sessions, email tokens, mail outbox |

## AI modeling agent (remote/subscription models)

The hosted client uses remote providers, but customer turns must never inherit
the `liveuser` account's provider credentials. Production and staging therefore
set `CHUDWARE_WEB_SHARE_PROVIDER_KEYS=0`.

With sharing disabled, `gateway/engine_bridge.py` must not inject keys from the
host `config.json` or `~/chudware-web-data/ai_providers.env` into a customer
request. A turn uses either that account's own stored provider credential or the
metered managed-credit path. This is a financial boundary as well as a secrecy
boundary: falling back to a host key lets any customer spend the host provider
account outside the credit ledger.

Provider credentials are only attached server-side to a canonical,
provider-matching endpoint. Client-supplied `api_key`, `base_url`, and
`auth_mode` fields are stripped. `agent_provider_*` remains admin-only. Do not
set `CHUDWARE_WEB_SHARE_PROVIDER_KEYS=1` in a public environment.
