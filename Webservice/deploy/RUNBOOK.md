# chudware-web RUNBOOK (Hunter)

Host: Fedora 44, rootless podman, gateway `127.0.0.1:8300`, data `~/chudware-web-data/`.
Public: `https://chudware.com` via Cloudflare Tunnel + Access.

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
| Access application | self-hosted app for `chudware.com` |

`cf_provision.py` is **driver-owned** and lands beside these deploy files — DeployOps does not implement it. It should write into **`~/chudware-web-data/gateway.env`** (never committed):

```bash
TUNNEL_TOKEN=...                 # cloudflared tunnel run --token
CHUDWARE_CF_TEAM_DOMAIN=https://bruh234324.cloudflareaccess.com
CHUDWARE_CF_AUD=...                # Access Application Audience (AUD) tag
```

Gateway verifies `Cf-Access-Jwt-Assertion` against team JWKS (`…/cdn-cgi/access/certs`) with `iss` = team domain and `aud` = `CHUDWARE_CF_AUD`. JWKS rotates ~every 6 weeks (previous key valid ~7 days) — always fetch remotely, match `kid`.

### Hunter remaining manual steps

1. **Rotate leaked API token + R2 keys** (§a) — do this before relying on any old token in scripts.
2. **Optional Google IdP** (nice for Gmail allowlist users):
   - [Google Cloud Console](https://console.cloud.google.com/) → project → OAuth consent **External**.
   - OAuth client type **Web**:
     - Authorized JS origin: `https://bruh234324.cloudflareaccess.com`
     - Redirect URI: `https://bruh234324.cloudflareaccess.com/cdn-cgi/access/callback`
   - Zero Trust → Integrations → Identity providers → **Google** → paste Client ID/Secret → Test.
3. **Edit allowed emails** (Access policy Include → Emails), via Zero Trust dashboard **or** API after the new token exists. Prefer **exact emails**, not whole domains, for personal Google accounts. List each `@ua.edu` address that should use One-Time PIN.

### Auth honesty (do not market as Entra)

Phase-1 login is:

- **Google OAuth** (if IdP configured) for listed Google emails, and/or
- **Cloudflare Access One-Time PIN** to listed addresses (including `@ua.edu`).

OTP proves control of the **email mailbox**. It is **not** Microsoft Entra / “sign in with school Microsoft account.”

### Entra SSO (implemented — 2b: self-hosted oauth2-proxy, path 2a still needs UA IT)

Path **2a** (Cloudflare Access's own Entra IdP) needs an app registered *inside*
the ua.edu Entra tenant with admin consent — not something a student can self-serve.
Path **2b** is what's actually live: a second, independent front door at
`https://sso.chudware.com`, parallel to the Cloudflare Access app on `chudware.com`
(OTP + Google unaffected). Full rationale in `research/auth.md`.

```
browser -> Cloudflare Tunnel -> sso.chudware.com -> oauth2-proxy (127.0.0.1:8301,
  entra-id provider, oidc_issuer_url=.../common/v2.0, multi-tenant) -> verified login
  -> reverse-proxy to gateway (127.0.0.1:8300) with HTTP Basic Auth
  (username=email, password=shared secret) -> gateway/auth.py SSO branch
```

Already provisioned: DNS CNAME + tunnel ingress for `sso.chudware.com`, the
`chudware-sso.service` unit (installed, **not started**), `oauth2-proxy` binary at
`~/.local/bin/oauth2-proxy`, config skeleton at `~/chudware-web-data/oauth2-proxy.cfg`
(template: `Webservice/deploy/oauth2-proxy.cfg.example`), and
`CHUDWARE_WEB_SSO_SHARED_SECRET` in `gateway.env` matching the config's
`basic_auth_password`.

**Hunter's one remaining manual step** — register the Azure app (needs your Microsoft
account, not scriptable):

1. [portal.azure.com](https://portal.azure.com) → Microsoft Entra ID → App registrations → New registration.
2. Supported account types: **Accounts in any organizational directory (Any Microsoft Entra ID tenant - Multitenant)**.
3. Redirect URI (Web): `https://sso.chudware.com/oauth2/callback`.
4. Certificates & secrets → New client secret → copy the **Value** immediately (shown once).
5. API permissions → Microsoft Graph → Delegated → `openid`, `profile`, `email`, `offline_access`, `User.Read`. **Do not add** `Directory.Read.All`/`GroupMember.Read.All` — those force per-tenant admin consent and will wall out UA students whose IT hasn't approved this app.
6. Copy the **Application (client) ID** + client secret **Value** into `~/chudware-web-data/oauth2-proxy.cfg` (`client_id` / `client_secret`), then:
   ```bash
   systemctl --user enable --now chudware-sso.service
   journalctl --user -u chudware-sso.service -n 40 --no-pager   # confirm clean start
   ```

**Known limitation (not guaranteed to clear on its own):** UA's tenant may still show
**"Need admin approval"** on first `@ua.edu` login if UA locks down user consent for
third-party multi-tenant apps — common at universities. If that happens, the only
fixes are a formal request to UA IT/security to approve this specific app, or falling
back to path 2a. Test with one real `@ua.edu`/`@crimson.ua.edu` account after
activation to find out which situation UA is in.

---

## (c) `users.yaml` + heavy seats

File: `Webservice/gateway/users.yaml`

```yaml
users:
  - email: you@example.com
    display: Hunter
    role: admin          # admin | user
    heavy: true          # GPU seat (≤2 concurrent heavy)
  - email: alice@ua.edu
    display: Alice
    role: user
    heavy: false
```

- Email must match Access JWT `email` claim (or `X-Dev-User` when `CHUDWARE_WEB_DEV=1`).
- **heavy: true** → session may get CDI GPU + `nvh264enc`; default seats are CPU `x264enc` / llvmpipe.
- Cap: demo default **3** sessions; hard **4**; **≤2** concurrent heavy. Edit list before demo; restart gateway if it only loads users at boot.

---

## (d) Demo-day checklist

- [ ] Secrets rotated; `gateway.env` has `TUNNEL_TOKEN`, `CHUDWARE_CF_TEAM_DOMAIN`, `CHUDWARE_CF_AUD` (mode 600).
- [ ] `bash Webservice/deploy/setup.sh` already run; linger enabled.
- [ ] `systemctl --user start chudware-gateway chudware-cloudflared chudware-monitor.timer`
- [ ] Session image present: `podman image exists localhost/chudware-session:latest`
- [ ] Optional warm seat: start one session as admin, leave idle for fast first guest.
- [ ] `curl -sS http://127.0.0.1:8300/healthz` — sessions, free RAM, VRAM, image bit look sane.
- [ ] Access allowlist + `users.yaml` include every demo attendee email.
- [ ] Browser smoke: `https://chudware.com` → Access challenge → portal → stream iframe.
- [ ] Know the “not on the list” path (Access deny vs portal allowlist page).
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

### Session container logs

```bash
podman ps -a --filter name=chudware-sess-
podman logs --tail 200 chudware-sess-<slug>
podman inspect chudware-sess-<slug> --format '{{.State.Status}} {{.HostConfig.Memory}}'
```

Published ports: host `127.0.0.1:8400+n` → container `:8080`.

### Monitor / reaper

```bash
systemctl --user status chudware-monitor.timer
python3 Webservice/deploy/monitor.py --dry-run   # one JSONL sample
tail -n 5 ~/chudware-web-data/logs/monitor.jsonl
```

- `MemAvailable < 4 GiB` → `podman stop` oldest idle session (idle from `/healthz` if available).
- Disk free &lt; 10% on data root or container storage → touches `~/chudware-web-data/REFUSE_NEW`; gateway `sessions.start` returns **503** until free space recovers and monitor clears the flag.

### GPU CDI after driver update

Rootless GPU seats need CDI device nodes. After NVIDIA driver upgrades:

```bash
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
# verify
podman run --rm --device nvidia.com/gpu=all --security-opt label=disable \
  registry.fedoraproject.org/fedora-minimal:44 nvidia-smi -L
```

Then recreate heavy session containers so they pick up the new CDI spec.

### Linger / user units vanish on logout

```bash
loginctl show-user "$USER" -p Linger
sudo loginctl enable-linger "$USER"
```

## (f) Admin ops dashboard + backups

### /admin live ops (added post-v1)

`https://chudware.com/admin` (admin role) now shows, above the allowlist:

- **Live sessions** — per seat: screenshot thumbnail (refreshes ~10 s), status,
  uptime, idle, CPU/mem (`podman stats`), and a **Stop** button. **Click a
  thumbnail to spectate** that seat live (view-only noVNC popout; the stream
  proxy admits admins alongside the owner).
- **Host bar** — seats used/max, free RAM, VRAM used/total + GPU util
  (`nvidia-smi`), load, idle timeout, plus **service chips**
  (cloudflared / monitor / backup unit states) and a **pause new sessions**
  switch (touches the same `REFUSE_NEW` flag the disk monitor uses; running
  seats are unaffected, new starts get a busy message).
- **Allowlist table is editable in place** — click the role pill to toggle
  admin/user (self-demotion blocked), the GPU pill to toggle the heavy seat,
  the project cell to reassign; changes apply on their next session start.
- **Gateway log** — journal tail in the page (`/api/admin/logs`), optional
  5 s follow. No SSH needed for demo-day triage.

Portal users get a **Paste text…** button in the stream bar — types text into
the focused widget inside their seat via `xdotool` (the browser clipboard can't
reach the container's X clipboard). 20k char cap, argv-passed (no shell).

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

- Tars **all of `~/chudware-web-data/`** (PLM bare repos, users.yaml, chat.db,
  per-user homes incl. unpushed WIP, env files) minus caches/logs/tmp.
- Homes are subuid-owned → tar runs under **`podman unshare`**.
- Destination `~/chudware-web-backups/` (mode 700, tarballs 600), newest
  **14** kept. Override via `CHUDWARE_WEB_BACKUP_DIR` / `CHUDWARE_WEB_BACKUP_KEEP`.

```bash
systemctl --user start chudware-backup.service     # manual backup now
systemctl --user list-timers chudware-backup.timer # next run
# restore (same subuid mapping):
podman unshare tar -xzf ~/chudware-web-backups/chudware-web-data-<stamp>.tar.gz -C ~
```

Off-host copies are on you: rsync the backup dir somewhere that isn't this
machine when the project data starts mattering.


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
bash Webservice/deploy/update.sh --image       # also rebuild session container
bash Webservice/deploy/update.sh --wasm        # also wasm
```

Logs/lock: `~/chudware-web-data/logs/update.log`, `deploy.lock`, `deploy-status.json`
(shared with admin `rebuild.sh` / `redeploy.sh`).

**Auto-promote engine → webd:** `chudware-webd-sync.path` watches
`core/build-occt-linux/bin/chudware-hostd` and re-runs `install_webd.sh --restart`
(idle-wait when sessions are connected). Enabled by `setup.sh` and
`install_webd.sh`. Confirm with:

```bash
systemctl --user is-enabled chudware-webd-sync.path
systemctl --user status chudware-webd-sync.path --no-pager
```

Desktop against the **shared** daemon (same engine as web sessions):

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
| `Webservice/deploy/chudware-*.service` / `.timer` / `.path` | systemd **user** units (incl. webd-sync) |
| `Webservice/deploy/monitor.py` | Host sample + reaper (stdlib) |
| `Webservice/deploy/cf_provision.py` | Driver: tunnel/DNS/Access via API |
| `~/chudware-web-data/gateway.env` | Secrets + CF team/AUD/token (gitignored conceptually; never commit) |
| `~/chudware-web-data/{homes,plm,logs,chat.db}` | Runtime data |
| `Webservice/gateway/users.yaml` | Allowlist + heavy seats |

## AI modeling agent (remote/subscription models)

Sessions run the in-app AI agent against **remote providers only** — no local
models on this host. Two ways to give sessions credentials:

1. **Shared keys (yours):** create `~/chudware-web-data/ai_providers.env`
   (`chmod 600`) with `KEY=VALUE` lines (`OPENROUTER_API_KEY=`,
   `ANTHROPIC_API_KEY=`, `OPENAI_API_KEY=`, `XAI_API_KEY=`, `GEMINI_API_KEY=`, …).
   The gateway forwards every UPPER_CASE var in that file into each session
   container at start. Restart is NOT needed — the file is re-read per session
   start; running seats keep their old env until restarted.
2. **Bring-your-own:** users open Chudware ▸ Settings ▸ AI provider inside their
   session and enter their own key / subscription login. It persists in their
   private HOME volume (`~/chudware-web-data/homes/<slug>`), never on the shared
   image and never visible to other users.

The session image bakes the Python worker runtime at `/opt/chudware/runtime/venv`
(`CHUDWARE_PYTHON`) + app payload at `/opt/chudware/app` (`CHUDWARE_REPO`), so the
agent works out of the box once a provider credential exists.
