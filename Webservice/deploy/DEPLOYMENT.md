# Chudware Web — Cloudflare deployment facts

Non-secret topology and IDs for the public gateway deployment. This file is
safe to commit. **No live credentials belong here** — secrets were removed
from `Webservice/Cloudflare-info.txt` (untracked, gitignored) and are
referenced below by placeholder only.

| Placeholder | What it is | Where the real value lives |
| --- | --- | --- |
| `<CF_API_TOKEN>` | Cloudflare API token used by `cf_provision.py` | `Webservice/Cloudflare-info.txt` (untracked, `chmod 600`) or `$CF_API_TOKEN` |
| `<R2_ACCESS_KEY_ID>` | R2 S3-compatible Access Key ID | `Webservice/Cloudflare-info.txt` |
| `<R2_SECRET_ACCESS_KEY>` | R2 S3-compatible Secret Access Key | `Webservice/Cloudflare-info.txt` |

> **ROTATION REQUIRED:** these credentials were committed to git history in
> `84963165` (removed from tracking in `ac1b912f`). The values in git history
> must be considered compromised — rotate them before relying on this
> deployment. See "Credential rotation" at the bottom.

## Account

- Account ID: `9a800a5f674233fe127cdb44b395ebfa`
- R2 S3 API endpoint: `https://9a800a5f674233fe127cdb44b395ebfa.r2.cloudflarestorage.com`
  (credentials: `<R2_ACCESS_KEY_ID>` / `<R2_SECRET_ACCESS_KEY>`)

Verify the API token (after rotation, with the NEW token):

```
curl -X GET "https://api.cloudflare.com/client/v4/accounts/9a800a5f674233fe127cdb44b395ebfa/tokens/verify" \
     -H "Authorization: Bearer <CF_API_TOKEN>"
```

## Domains

- Primary: `chudware.com` (zone `0cebfc25c546a96058479d6ba227cd21`), also `www.chudware.com`
- Legacy: `cad.scootware.us` (zone `175dbe20817f2b573a81e085031ef922`) — still live

## Tunnel

- Name: `billet-web`, ID `95256541-2ebf-4101-92a1-daecd240c087` (remotely managed)
- DNS: each host is a PROXIED CNAME → `<tunnel-id>.cfargotunnel.com`

Ingress table (all → local services on `127.0.0.1`):

| Hostname | Service |
| --- | --- |
| `cad.scootware.us` | `http://127.0.0.1:8300` (FastAPI gateway) |
| `chudware.com` | `http://127.0.0.1:8300` |
| `www.chudware.com` | `http://127.0.0.1:8300` |
| `sso.chudware.com` | `http://127.0.0.1:8301` (oauth2-proxy, Microsoft Entra front door — NOT behind the Access app) |
| *(catch-all)* | `http_status:404` |

## Cloudflare Access

- ONE self-hosted app "Billet Web", ID `a5c88ac6-8364-4fa1-9610-c2ab5dcf9fa8`
- `self_hosted_domains = [cad.scootware.us, chudware.com, www.chudware.com]`
- Shared AUD: `b375474d216c1f4a1925493000f31c32e85ed9b198001fbd2622fe451a5a879f`
  → gateway env `CHUDWARE_WEB_CF_AUD`
- **All domains share ONE app so they share ONE AUD** — the gateway validates a
  single `CHUDWARE_WEB_CF_AUD` (`Webservice/gateway/auth.py`). A per-domain app
  would mint a different AUD and JWT validation would fail. Add new domains to
  THIS app's `self_hosted_domains`, never as a new app.
- Policy "billet-users": explicit user allowlist + `email_domain` rules
  (`ua.edu`, `crimson.ua.edu`). CF `email_domain` is an EXACT match (no
  subdomain wildcard), so each domain must be listed explicitly.

## Allowlist / users file (production)

The tracked `Webservice/gateway/users.yaml` is a **template that grants nobody
access**. Production MUST load the real allowlist from a file OUTSIDE the repo:

- Set `CHUDWARE_WEB_USERS_FILE=/path/to/users.yaml` (e.g.
  `~/chudware-web-data/users.yaml`, `chmod 600`) in the gateway env.
- On startup `config.py`'s `ensure_data_dirs()` seeds that path from the repo
  template if it does not exist — the seeded file grants nobody access, so a
  fresh boot serves health checks but logs NOBODY in. This is safe but silent.
- **Recovery if locked out:** edit the runtime file pointed to by
  `CHUDWARE_WEB_USERS_FILE` (add your email with `role: admin`), or unset the
  override so the repo template is used, then restart the gateway. Admin role
  changes also require a matching entry in the CF Access policy — run
  `python3 Webservice/deploy/cf_provision.py --sync-users` to push the runtime
  users file into the Access allow policy.

## Safe application updates

Run `Webservice/deploy/update.sh` from the production checkout. The updater and
`install_webd.sh --restart` now treat the native daemon, OMP/Bun provider
dependencies, and WASM bundle as one immutable release under
`~/chudware-web/releases/`; `~/chudware-web/current` moves only after every
artifact is built and validated. The joint manifest and running stamp are
written after cutover, so a failed native build, provider install, WASM build,
unit restart, or readiness probe exits nonzero instead of reporting success
over a mixed/stale deployment.

Before a shared-daemon restart, the installer reads the authenticated
`engine_bridges` value from detailed `/healthz` using the loopback SSO secret
and an active administrator account. Missing authentication, missing
telemetry, or an unreachable gateway fails closed; a nonzero bridge count
defers the restart rather than disconnecting active browser sessions.
Anonymous `/healthz` intentionally remains the bare `{"ok": true}` liveness
contract and must not be used for drain decisions.

Production WASM builds default to CMake `MinSizeRel`; Release's `wasm-opt -O3`
peaked above 9 GiB RSS and was OOM-killed on the 16 GiB deployment host.
Operators may set `CHUDWARE_WASM_BUILD_TYPE` explicitly on larger builders, but
the promoted artifact must still come from the installer's private stage.

The gateway unit and application both enforce
`CHUDWARE_WEB_WS_MAX_MESSAGE_BYTES` (4 MiB by default). Keep the unit template,
gateway setting, and reverse proxy limit aligned when changing it.

## Purge cache (per zone)

```
curl -s -X POST "https://api.cloudflare.com/client/v4/zones/<ZONE_ID>/purge_cache" \
     -H "Authorization: Bearer <CF_API_TOKEN>" -H "Content-Type: application/json" \
     --data '{"purge_everything":true}'
```

## Credential rotation (operator runbook)

The API token and R2 keys below were exposed in git history. Rotate ALL of
them; treat anything derived from the old token as compromised.

**Cloudflare API token:**

1. Cloudflare dashboard → profile (top right) → **My Profile → API Tokens**.
2. Find the token used by this deployment and **Roll** it (or **Delete** and
   create a replacement with the same permissions: Tunnel edit, DNS edit,
   Access edit on the account).
3. Update the value in `Webservice/Cloudflare-info.txt` (local only,
   `chmod 600`) and in `$CF_API_TOKEN` wherever the operator shell exports it.
4. Re-run `python3 Webservice/deploy/cf_provision.py` only if tunnel/app
   config must be re-pushed; the running tunnel itself authenticates with
   `TUNNEL_TOKEN` in `~/chudware-web-data/gateway.env`, not this API token.

**R2 S3 credentials:**

1. Cloudflare dashboard → **R2 → Manage R2 API Tokens** (account level).
2. Delete the API token that issued Access Key ID `<R2_ACCESS_KEY_ID>` and
   create a new R2 API token with the same object read/write scope.
3. Update `<R2_ACCESS_KEY_ID>` / `<R2_SECRET_ACCESS_KEY>` in
   `Webservice/Cloudflare-info.txt` and in any S3 client config that consumed
   them (the gateway does not use R2 directly; this is operator tooling).

**After rotation:** search the repo and git history for the old values before
considering the incident closed; if this repo is ever made public, the
history containing the old credentials must be purged or the repo re-created.
