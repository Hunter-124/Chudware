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

- Application: `app.chudware.com`
- Marketing: `chudware.com` and `www.chudware.com` (Cloudflare Pages)

## Tunnel

- Name: `chudware-web`, ID `95256541-2ebf-4101-92a1-daecd240c087` (remotely managed)
- DNS: `app.chudware.com` is a PROXIED CNAME → `<tunnel-id>.cfargotunnel.com`;
  the apex and `www` are Cloudflare Pages custom domains.

Ingress table (all → local services on `127.0.0.1`):

| Hostname | Service |
| --- | --- |
| `app.chudware.com` | `http://127.0.0.1:8300` (FastAPI gateway) |
| *(catch-all)* | `http_status:404` |

### Staging ingress

`staging.chudware.com` is a separate tunnel ingress to
`http://127.0.0.1:8310`. It must never be routed to port 8300. The staging
gateway uses `~/chudware-web-staging`, PostgreSQL database
`chudware_staging`, console-only mail, and Stripe test credentials (or no
Stripe credentials). Its unit refuses to start if an `sk_live_` Stripe secret
is present.

Production remains on port 8300 with data under `~/chudware-web-data` and
database `chudware_prod`. Both gateways share application source and the webd
engine, but never mutable gateway state or database credentials.

## The `/admin` door

- `/admin` is gated by the gateway alone: a `chudware_sid` session whose `accounts`
  row carries `role = 'admin'`. `cf_provision.py` provisions no Access application.
- The legacy self-hosted app "Billet Web"
  (ID `a5c88ac6-8364-4fa1-9610-c2ab5dcf9fa8`, `self_hosted_domains =
  [app.chudware.com/admin*]`) must be **deleted by hand in Zero Trust**. While it
  lives, Cloudflare answers `/admin` with its own login page instead of the
  gateway's `/signin` — which is the reason it is being removed.
- Cloudflare Access remains a *fallback* credential the gateway accepts from a
  caller with **no** session: set `CHUDWARE_WEB_CF_TEAM_DOMAIN` and
  `CHUDWARE_WEB_CF_AUD` by hand if you want that, and nothing provisions them.
  Roles are read from the account store either way, never from an Access policy.

## Accounts (production)

There is no allowlist file. Accounts live in PostgreSQL (`accounts`, plus the
credential, session and email-token tables created by
`Webservice/gateway/migrations/*.sql`) and people create their own:

- `POST /api/auth/register` stores an argon2id password hash and mails a
  verification link; redeeming the link activates the account on a 14-day trial
  (`CHUDWARE_WEB_TRIAL_DAYS`) and opens a server-side session carried by the
  `chudware_sid` cookie.
- Optional Google/GitHub sign-in resolves to the same rows — set
  `CHUDWARE_WEB_GOOGLE_CLIENT_ID`/`_SECRET` and/or
  `CHUDWARE_WEB_GITHUB_CLIENT_ID`/`_SECRET` in the gateway env.
- `CHUDWARE_WEB_SESSION_SECRET` is required: outside dev mode the gateway refuses
  to start without it unless the optional CF Access fallback is fully configured.
  Generate one with `python3 -c 'import secrets;print(secrets.token_urlsafe(48))'`.
- Admins approve, deny and suspend from `/admin`
  (`POST /api/admin/accounts/{email}/{approve,deny,suspend}`).
- **First admin / recovery if locked out:** `cd Webservice/gateway && .venv/bin/python
  -m authn grant-admin --email you@example.com` against `CHUDWARE_DB_URL`, then sign
  in normally. It creates-or-promotes, sets the password (prompted, never in argv),
  and marks the address verified without needing working outbound mail. This CLI is
  the only writer of `role`, and it is now the only gate on `/admin` — there is no
  Access allow policy to add the address to as well.

## Safe application updates

There is one deployment entry point for both environments:

```
bash Webservice/deploy/update.sh staging
bash Webservice/deploy/update.sh prod --tag v1.2.0
```

Before source or schema changes, it takes a custom-format `pg_dump` under the
selected data root's `backups/`. It then performs an explicit migration,
restarts only the selected unit, and requires HTTP 200 plus valid response
bodies from anonymous `/healthz` and session-authenticated `/api/me`. Any
backup, migration, restart, or health failure exits nonzero. The updater also
parses `CHUDWARE_DB_URL` and refuses a staging deploy unless the database name
is exactly `chudware_staging` (and likewise requires `chudware_prod` for
production).

The production path continues to treat the native daemon, OMP/Bun provider
dependencies, and WASM bundle as one immutable release under
`~/chudware-web/releases/`; `~/chudware-web/current` moves only after every
artifact is built and validated. Staging's default path is gateway-only so a
green master deployment cannot bounce the production desktop engine.

Before a shared-daemon production restart, `install_webd.sh` reads the
authenticated `engine_bridges` value from detailed `/healthz`. It resolves an
active account, mints a short-lived ops session, and fails closed when session
minting or telemetry fails or when active bridges do not drain. Anonymous
`/healthz` intentionally remains the bare `{"ok": true}` liveness contract and
must not be used for drain decisions.

### Deployment workflow contract

The workflow owner must implement these requirements without duplicating the
shell deployment logic:

- **Staging:** react only to a successful completion of `linux-gate.yml` for
  the canonical repository's `master` branch, select the single
  `livingroom-pc` self-hosted Linux runner, and invoke
  `/home/liveuser/Repositories/CAD/Webservice/deploy/update.sh staging`.
- **Production:** trigger only by `workflow_dispatch`; require a `v*` tag input
  and invoke `update.sh prod --tag <tag>`. The script independently verifies
  that deployed HEAD is exactly that release tag.
- No deploy workflow exists yet. Follow the single-runner selection shape of
  `native-build.yml`'s `runs-on: [self-hosted, …]` matrix rows (line 45).
  Deployment is one side effect, never a matrix: exactly one selected runner
  may execute it.
- Serialize prod and staging through the updater's host-wide flock. Propagate
  its nonzero exit so a failed backup, migration, or either health assertion
  makes the workflow fail.
- Every job must be `self-hosted`; do not upload deployment artifacts. The
  canonical checkout at `/home/liveuser/Repositories/CAD` must be clean before
  invocation because the updater deliberately uses `git pull --ff-only` and
  refuses local changes.

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
   create a replacement with the same permissions minus Access: Tunnel edit and
   DNS edit on the account).
3. Update the value in `Webservice/Cloudflare-info.txt` (local only,
   `chmod 600`) and in `$CF_API_TOKEN` wherever the operator shell exports it.
4. Re-run `python3 Webservice/deploy/cf_provision.py` only if tunnel/app
   config must be re-pushed; the running tunnel itself authenticates with
   `TUNNEL_TOKEN` in `~/chudware-web-data/cloudflared.env`, not the gateway environment.

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
