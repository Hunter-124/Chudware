# chudware-web RUNBOOK (Hunter)

Host: Fedora 44, gateway `127.0.0.1:8300`, data `~/chudware-web-data/`.
Public: `https://chudware.com` via Cloudflare Tunnel; sign-in is self-serve in the gateway.

---

## (a) SECURITY FIRST — rotate leaked credentials

`Webservice/Cloudflare-info.txt` (and git history) held a **live Cloudflare API token** and **R2 S3 keys**. Treat them as compromised.

### Rotate now

1. **Cloudflare API token**
   - Dashboard → My Profile → API Tokens → find/revoke the leaked token.
   - Create a replacement with least privilege needed for `cf_provision.py` (Access apps, tunnels, DNS on `chudware.com`).
   - Store the new token **only** outside git (password manager / `~/chudware-web-data/secrets/`). Never re-commit.

2. **R2 Access Key ID + Secret**
   - Dashboard → R2 → Manage R2 API Tokens → revoke the leaked pair.
   - Create a new R2 token if you still need object storage; put keys in the same secrets dir / env — not the repo.

3. **Tunnel token**
   - Remotely-managed tunnel token lives in `~/chudware-web-data/gateway.env` as `TUNNEL_TOKEN=…`.
   - If the old API token could mint tunnel credentials, rotate the tunnel token after API token rotation (`cloudflared tunnel token <name>` or re-run provision).

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
| Public hostname | `chudware.com` → `http://127.0.0.1:8300` (WSS over same HTTPS) |
| Extra hostname | `www.chudware.com` → same tunnel, same service |
| Access application | self-hosted app for `chudware.com` — gates `/admin` only |

`cf_provision.py` is **driver-owned** and lands beside these deploy files — DeployOps does not implement it. It should write into **`~/chudware-web-data/gateway.env`** (never committed):

```bash
TUNNEL_TOKEN=...                 # cloudflared tunnel run --token
CHUDWARE_CF_TEAM_DOMAIN=https://bruh234324.cloudflareaccess.com
CHUDWARE_CF_AUD=...                # Access Application Audience (AUD) tag
```

Access is the operator door for `/admin`. When an Access credential is present the gateway verifies `Cf-Access-Jwt-Assertion` against team JWKS (`…/cdn-cgi/access/certs`) with `iss` = team domain and `aud` = `CHUDWARE_CF_AUD`. JWKS rotates ~every 6 weeks (previous key valid ~7 days) — always fetch remotely, match `kid`. Everyone else authenticates to the gateway itself (§c).

### Hunter remaining manual steps

1. **Rotate leaked API token + R2 keys** (§a) — do this before relying on any old token in scripts.
2. **Optional Google IdP for the `/admin` door** (Access-side, not the site login):
   - [Google Cloud Console](https://console.cloud.google.com/) → project → OAuth consent **External**.
   - OAuth client type **Web**:
     - Authorized JS origin: `https://bruh234324.cloudflareaccess.com`
     - Redirect URI: `https://bruh234324.cloudflareaccess.com/cdn-cgi/access/callback`
   - Zero Trust → Integrations → Identity providers → **Google** → paste Client ID/Secret → Test.
3. **Edit the Access allowlist** (Access policy Include → Emails), via Zero Trust dashboard **or** API after the new token exists. This list is operators only — it decides who may reach `/admin`, not who may use the site. Prefer **exact emails** over whole domains.

### Auth honesty (what a login actually proves)

Site login is the gateway's own: an email plus an argon2id-hashed password, or Google/GitHub
OAuth if those client credentials are configured. Both prove control of an **email mailbox**
or of a Google/GitHub account. Neither is Microsoft Entra / "sign in with your school
Microsoft account", and nothing here is an institutional SSO — do not market it as one.

Cloudflare Access One-Time PIN still guards `/admin`, and it proves the same thing: mailbox
control for an address listed in the Access policy.

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
  the gateway refuses to start without it unless CF Access is fully configured. Generate
  one with `python3 -c 'import secrets;print(secrets.token_urlsafe(48))'`. Optional
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
- **Locked out?** `UPDATE accounts SET role='admin', status='active' WHERE email='you@…';`
  against `CHUDWARE_DB_URL`, then sign in normally.

---

## (d) Demo-day checklist

- [ ] Secrets rotated; `gateway.env` has `TUNNEL_TOKEN`, `CHUDWARE_CF_TEAM_DOMAIN`, `CHUDWARE_CF_AUD`, `CHUDWARE_WEB_SESSION_SECRET` (mode 600).
- [ ] `bash Webservice/deploy/setup.sh` already run; linger enabled.
- [ ] `systemctl --user start chudware-gateway chudware-cloudflared chudware-monitor.timer`
- [ ] `curl -sS http://127.0.0.1:8300/healthz` — free RAM, VRAM, engine bridges look sane.
- [ ] Every demo attendee has registered and verified (or you approved them at `/admin`).
- [ ] Browser smoke: `https://chudware.com` → sign in → portal → `/app` loads.
- [ ] Know the not-yet-approved path (the gateway's pending/denied status page) and the
      Access deny page for `/admin`.
- [ ] Confirm `REFUSE_NEW` is absent unless disk is actually critical: `ls ~/chudware-web-data/REFUSE_NEW`.

---

## (e) Troubleshooting

### JWKS / 401 after Access login

- Confirm `CHUDWARE_CF_AUD` matches Access app → Overview / Additional settings → Application Audience.
- Team domain must be exactly `https://bruh234324.cloudflareaccess.com` (no trailing slash mismatch vs gateway config).
- Force JWKS refresh: restart gateway after Access app recreation (AUD changes if app is recreated).
- CF rotates Access signing keys ~6 weeks; gateway must use live JWKS URL, not a pinned PEM.

### cloudflared not connecting

```bash
systemctl --user status chudware-cloudflared
journalctl --user -u chudware-cloudflared -n 80 --no-pager
# token present?
grep -E '^TUNNEL_TOKEN=' ~/chudware-web-data/gateway.env | sed 's/=.*/=***/'
```

Binary: `/usr/local/bin/cloudflared` (v2026.7.1 on this host). Unit runs:  
`cloudflared tunnel run --token ${TUNNEL_TOKEN}`.

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

### Linger / user units vanish on logout

```bash
loginctl show-user "$USER" -p Linger
sudo loginctl enable-linger "$USER"
```

## (f) Admin ops dashboard + backups

### /admin ops

`https://chudware.com/admin` (admin role) shows, above the accounts queue:

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

### Shared /admin terminal (tmux-backed)

The `/admin` browser terminal is now **shared, saved, and concurrent**. Each
session is a named **tmux** session on the gateway host (socket
`$CHUDWARE_TMUX_SOCKET`, default `chudware`), so it survives browser disconnects
*and* a gateway restart, and several admins can attach to the same session at
once — they see and drive the same `omp` instance, which makes it easy to keep
track of the actual work in flight.

- **Shared-sessions picker** (top of the terminal pane) lists every session
  with a live client count; click one to attach, `+ New session` to start one,
  the `×` on a pill to kill it. A browser **tab** is just this browser's
  attachment: closing it (or `Ctrl+Alt+W`) only **detaches** — the session
  keeps running. `Ctrl+Alt+T` starts a new session.
- REST (admin-only): `GET/POST /api/admin/terminal/sessions`,
  `DELETE /api/admin/terminal/sessions/{name}`. WS `/admin/terminal?name=<s>`
  attaches (creating on demand).
- **Host dependency:** `tmux` must be installed (`dnf install -y tmux`). The
  first session starts the tmux server inside a `systemd-run --user --scope`
  unit so heavy `omp` builds live outside the gateway's MemoryMax cgroup; if the
  user bus is missing the create retries unscoped. Without tmux the terminal
  falls back to the old private, per-connection shell (not shared or saved).
- Sessions persist with zero clients (`destroy-unattached off`); they die only
  on an explicit kill or when the shell/`omp` in them exits. List/inspect from a
  shell with `tmux -L chudware ls`.

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


## (g) Pull / rebuild / redeploy (keep this host current)

One entry point for day-to-day sync from the other machine / `origin/master`:

```bash
# Full path: git pull --ff-only + host GUI + engine + install_webd --restart + gateway
bash Webservice/deploy/update.sh

# Variants
bash Webservice/deploy/update.sh --pull-only   # git only
bash Webservice/deploy/update.sh --no-pull     # rebuild current tree
bash Webservice/deploy/update.sh --host-only   # desktop Chudware binary
bash Webservice/deploy/update.sh --web-only    # engine + webd + gateway
```

Logs/lock: `~/chudware-web-data/logs/update.log`, `deploy.lock`, `deploy-status.json`
(shared with admin `rebuild.sh` / `redeploy.sh`).

`install_webd.sh` builds the WASM client and promotes the engine in one joint
release, then restarts `chudware-hostd` only when the joint sha changed. There
is no auto-promote path unit — run `update.sh` (or `install_webd.sh --restart`)
when you want the host to pick up a new engine build.

Desktop against the **shared** daemon (same engine the web client uses):

```bash
~/chudware-web/chudware-shared.sh
```

---

## Layout reference

| Path | Role |
|------|------|
| `Webservice/deploy/setup.sh` | Idempotent dirs, venv, unit install, enable |
| `Webservice/deploy/update.sh` | **Day-to-day:** pull → rebuild host/engine → webd → gateway |
| `Webservice/deploy/rebuild.sh` / `redeploy.sh` | Admin-locked rebuild targets / gateway restart |
| `Webservice/deploy/install_webd.sh` | Promote engine build → `~/chudware-web` + hostd unit |
| `Webservice/deploy/chudware-*.service` / `.timer` | systemd **user** units |
| `Webservice/deploy/monitor.py` | Host sample + disk-pressure flag (stdlib) |
| `Webservice/deploy/cf_provision.py` | Driver: tunnel/DNS/Access via API |
| `~/chudware-web-data/gateway.env` | Secrets + CF team/AUD/token + `CHUDWARE_WEB_SESSION_SECRET` (never commit) |
| `~/chudware-web-data/{workspace,bucket,plm,logs}` | Runtime file state |
| PostgreSQL (`CHUDWARE_DB_URL`) | Accounts, chat history, webhook events, credentials, sessions, email tokens, mail outbox |

## AI modeling agent (remote/subscription models)

The web client runs the in-app AI agent against **remote providers only** — no
local models on this host. Credentials are resolved SERVER-SIDE and never sent
to the browser:

1. **Host config.json** — whatever provider keys the gateway account's Chudware
   config already carries. `gateway/engine_bridge.py` fills them in for
   `agent_list_models` and leaves agent turns to the shared daemon.
2. **Shared keys file (fallback):** create `~/chudware-web-data/ai_providers.env`
   (`chmod 600`) with `KEY=VALUE` lines (`OPENROUTER_API_KEY=`,
   `ANTHROPIC_API_KEY=`, `OPENAI_API_KEY=`, `XAI_API_KEY=`, `GEMINI_API_KEY=`, …).
   Re-read per request; no gateway restart needed.

Keys are only ever attached to a **canonical, provider-matching endpoint** —
a client that names any other base_url gets a generic rejection, never a key.
Client-supplied `api_key`/`base_url`/`auth_mode` on agent turns are stripped.
`agent_provider_*` (the administrator's auth store) is admin-only.

All web turns spend the host account's provider quota; per-user billing
isolation is not implemented.
