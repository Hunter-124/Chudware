# billet-web RUNBOOK (Hunter)

Host: Fedora 44, rootless podman, gateway `127.0.0.1:8300`, data `~/billet-web-data/`.
Public: `https://cad.scootware.us` via Cloudflare Tunnel + Access.

---

## (a) SECURITY FIRST — rotate leaked credentials

`Webservice/Cloudflare-info.txt` (and git history) held a **live Cloudflare API token** and **R2 S3 keys**. Treat them as compromised.

### Rotate now

1. **Cloudflare API token**
   - Dashboard → My Profile → API Tokens → find/revoke the leaked token.
   - Create a replacement with least privilege needed for `cf_provision.py` (Access apps, tunnels, DNS on `scootware.us`).
   - Store the new token **only** outside git (password manager / `~/billet-web-data/secrets/`). Never re-commit.

2. **R2 Access Key ID + Secret**
   - Dashboard → R2 → Manage R2 API Tokens → revoke the leaked pair.
   - Create a new R2 token if you still need object storage; put keys in the same secrets dir / env — not the repo.

3. **Tunnel token**
   - Remotely-managed tunnel token lives in `~/billet-web-data/gateway.env` as `TUNNEL_TOKEN=…`.
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
| Zone | `scootware.us` active |
| Tunnel | `billet-web` (remotely-managed) |
| Public hostname | `cad.scootware.us` → `http://127.0.0.1:8300` (WSS over same HTTPS) |
| Access application | self-hosted app for `cad.scootware.us` |

`cf_provision.py` is **driver-owned** and lands beside these deploy files — DeployOps does not implement it. It should write into **`~/billet-web-data/gateway.env`** (never committed):

```bash
TUNNEL_TOKEN=...                 # cloudflared tunnel run --token
BILLET_CF_TEAM_DOMAIN=https://bruh234324.cloudflareaccess.com
BILLET_CF_AUD=...                # Access Application Audience (AUD) tag
```

Gateway verifies `Cf-Access-Jwt-Assertion` against team JWKS (`…/cdn-cgi/access/certs`) with `iss` = team domain and `aud` = `BILLET_CF_AUD`. JWKS rotates ~every 6 weeks (previous key valid ~7 days) — always fetch remotely, match `kid`.

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

### Phase-2 Entra paths (from T-WEB1 Q2)

| Path | When | What |
|------|------|------|
| **2a** | UA IT will register an enterprise app | CF Access **Entra IdP**, single-tenant UA Directory ID + client secret + admin consent; policy `@ua.edu`. |
| **2b** | Need multi-tenant Entra without CF Entra connector | Self-hosted **oauth2-proxy** `entra-id` with `oidc_issuer_url = https://login.microsoftonline.com/common/v2.0` (+ optional tunnel for exposure only). UA tenant may still show **admin approval required**. |

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

- Email must match Access JWT `email` claim (or `X-Dev-User` when `BILLET_WEB_DEV=1`).
- **heavy: true** → session may get CDI GPU + `nvh264enc`; default seats are CPU `x264enc` / llvmpipe.
- Cap: demo default **3** sessions; hard **4**; **≤2** concurrent heavy. Edit list before demo; restart gateway if it only loads users at boot.

---

## (d) Demo-day checklist

- [ ] Secrets rotated; `gateway.env` has `TUNNEL_TOKEN`, `BILLET_CF_TEAM_DOMAIN`, `BILLET_CF_AUD` (mode 600).
- [ ] `bash Webservice/deploy/setup.sh` already run; linger enabled.
- [ ] `systemctl --user start billet-gateway billet-cloudflared billet-monitor.timer`
- [ ] Session image present: `podman image exists localhost/billet-session:latest`
- [ ] Optional warm seat: start one session as admin, leave idle for fast first guest.
- [ ] `curl -sS http://127.0.0.1:8300/healthz` — sessions, free RAM, VRAM, image bit look sane.
- [ ] Access allowlist + `users.yaml` include every demo attendee email.
- [ ] Browser smoke: `https://cad.scootware.us` → Access challenge → portal → stream iframe.
- [ ] Know the “not on the list” path (Access deny vs portal allowlist page).
- [ ] Confirm `REFUSE_NEW` is absent unless disk is actually critical: `ls ~/billet-web-data/REFUSE_NEW`.

---

## (e) Troubleshooting

### JWKS / 401 after Access login

- Confirm `BILLET_CF_AUD` matches Access app → Overview / Additional settings → Application Audience.
- Team domain must be exactly `https://bruh234324.cloudflareaccess.com` (no trailing slash mismatch vs gateway config).
- Force JWKS refresh: restart gateway after Access app recreation (AUD changes if app is recreated).
- CF rotates Access signing keys ~6 weeks; gateway must use live JWKS URL, not a pinned PEM.

### cloudflared not connecting

```bash
systemctl --user status billet-cloudflared
journalctl --user -u billet-cloudflared -n 80 --no-pager
# token present?
grep -E '^TUNNEL_TOKEN=' ~/billet-web-data/gateway.env | sed 's/=.*/=***/'
```

Binary: `/usr/local/bin/cloudflared` (v2026.7.1 on this host). Unit runs:  
`cloudflared tunnel run --token ${TUNNEL_TOKEN}`.

### Gateway / uvicorn

```bash
systemctl --user status billet-gateway
journalctl --user -u billet-gateway -n 80 --no-pager
curl -sS http://127.0.0.1:8300/healthz
```

Env file: `~/billet-web-data/gateway.env`. Unit `MemoryMax=1G`.

### Session container logs

```bash
podman ps -a --filter name=billet-sess-
podman logs --tail 200 billet-sess-<slug>
podman inspect billet-sess-<slug> --format '{{.State.Status}} {{.HostConfig.Memory}}'
```

Published ports: host `127.0.0.1:8400+n` → container `:8080`.

### Monitor / reaper

```bash
systemctl --user status billet-monitor.timer
python3 Webservice/deploy/monitor.py --dry-run   # one JSONL sample
tail -n 5 ~/billet-web-data/logs/monitor.jsonl
```

- `MemAvailable < 4 GiB` → `podman stop` oldest idle session (idle from `/healthz` if available).
- Disk free &lt; 10% on data root or container storage → touches `~/billet-web-data/REFUSE_NEW`; gateway `sessions.start` returns **503** until free space recovers and monitor clears the flag.

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

`https://cad.scootware.us/admin` (admin role) now shows, above the allowlist:

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

### Nightly backups

`billet-backup.timer` (03:30 + jitter, `Persistent=true`) runs
`Webservice/deploy/backup.sh`:

- Tars **all of `~/billet-web-data/`** (PLM bare repos, users.yaml, chat.db,
  per-user homes incl. unpushed WIP, env files) minus caches/logs/tmp.
- Homes are subuid-owned → tar runs under **`podman unshare`**.
- Destination `~/billet-web-backups/` (mode 700, tarballs 600), newest
  **14** kept. Override via `BILLET_WEB_BACKUP_DIR` / `BILLET_WEB_BACKUP_KEEP`.

```bash
systemctl --user start billet-backup.service     # manual backup now
systemctl --user list-timers billet-backup.timer # next run
# restore (same subuid mapping):
podman unshare tar -xzf ~/billet-web-backups/billet-web-data-<stamp>.tar.gz -C ~
```

Off-host copies are on you: rsync the backup dir somewhere that isn't this
machine when the project data starts mattering.

---

## Layout reference

| Path | Role |
|------|------|
| `Webservice/deploy/setup.sh` | Idempotent dirs, venv, unit install, enable |
| `Webservice/deploy/billet-*.service` / `.timer` | systemd **user** units |
| `Webservice/deploy/monitor.py` | Host sample + reaper (stdlib) |
| `Webservice/deploy/cf_provision.py` | Driver: tunnel/DNS/Access via API |
| `~/billet-web-data/gateway.env` | Secrets + CF team/AUD/token (gitignored conceptually; never commit) |
| `~/billet-web-data/{homes,plm,logs,chat.db}` | Runtime data |
| `Webservice/gateway/users.yaml` | Allowlist + heavy seats |

## AI modeling agent (remote/subscription models)

Sessions run the in-app AI agent against **remote providers only** — no local
models on this host. Two ways to give sessions credentials:

1. **Shared keys (yours):** create `~/billet-web-data/ai_providers.env`
   (`chmod 600`) with `KEY=VALUE` lines (`OPENROUTER_API_KEY=`,
   `ANTHROPIC_API_KEY=`, `OPENAI_API_KEY=`, `XAI_API_KEY=`, `GEMINI_API_KEY=`, …).
   The gateway forwards every UPPER_CASE var in that file into each session
   container at start. Restart is NOT needed — the file is re-read per session
   start; running seats keep their old env until restarted.
2. **Bring-your-own:** users open Billet ▸ Settings ▸ AI provider inside their
   session and enter their own key / subscription login. It persists in their
   private HOME volume (`~/billet-web-data/homes/<slug>`), never on the shared
   image and never visible to other users.

The session image bakes the Python worker runtime at `/opt/billet/runtime/venv`
(`AI_CAD_PYTHON`) + app payload at `/opt/billet/app` (`AI_CAD_REPO`), so the
agent works out of the box once a provider credential exists.
