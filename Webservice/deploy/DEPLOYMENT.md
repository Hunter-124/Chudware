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

Ingress table — the tunnel is healthy on 4 connections and carries exactly these
rules, verified 2026-09-02:

| Hostname | Service |
| --- | --- |
| `app.chudware.com` | `http://127.0.0.1:8300` (FastAPI gateway) |
| `scootware.us` | `http://127.0.0.1:8320` — an unrelated project sharing this tunnel |
| `www.scootware.us` | `http://127.0.0.1:8320` — same. Not Chudware; do not remove |
| *(catch-all)* | `http_status:404` |

### Staging is loopback-only

`chudware-gateway-staging.service` runs as a systemd user unit on the host and
listens on `http://127.0.0.1:8310`, but it is **not publicly routed**:
`staging.chudware.com` is absent from the tunnel ingress above and has no DNS
record in the `chudware.com` zone. Reach it over loopback or an SSH tunnel. If a
public staging hostname is added back, it must never be routed to port 8300. The
staging gateway uses `~/chudware-web-staging`, PostgreSQL database
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
  [app.chudware.com/admin*]`) **has been deleted.** Verified 2026-09-02: account
  `9a800a5f674233fe127cdb44b395ebfa` holds no Access application over any
  `chudware.com` host — the ten that remain are "Kinocasino …" apps scoped to
  `scootware.us`. Nothing shows a Cloudflare login page in front of `/admin` any
  more; an anonymous HTML navigation there gets the gateway's own
  `303 → /signin?next=%2Fadmin`.
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
bash Webservice/deploy/update.sh prod --continuous
bash Webservice/deploy/update.sh prod --tag v1.2.0
```

CI promotes production with `--continuous`; `--tag` remains for a deliberate
pinned release. `--continuous` waives only the tag requirement — it is rejected
for `staging` and rejected together with `--tag`.

Before source or schema changes, it takes a custom-format `pg_dump` under the
selected data root's `backups/`. It then performs an explicit migration,
restarts only the selected unit, and requires HTTP 200 plus valid response
bodies from anonymous `/healthz` and session-authenticated `/api/me`. Any
backup, migration, restart, or health failure exits nonzero. The updater also
parses `CHUDWARE_DB_URL` and refuses a staging deploy unless the database name
is exactly `chudware_staging` (and likewise requires `chudware_prod` for
production).

The production path treats the native daemon, artifact-locked Chudcode binary,
and WASM bundle as one immutable release under `~/chudware-web/releases/`;
`~/chudware-web/current` moves only after every artifact is built and validated.
Staging's default path is gateway-only so a
green master deployment cannot bounce the production desktop engine.

Before a shared-daemon production restart, `install_webd.sh` reads the
authenticated `engine_bridges` value from detailed `/healthz`. It resolves an
active account, mints a short-lived ops session, and fails closed when session
minting or telemetry fails or when active bridges do not drain. Anonymous
`/healthz` intentionally remains the bare `{"ok": true}` liveness contract and
must not be used for drain decisions.

Drain failures are pre-mutation refusals (exit 4): service definitions are installed
only after the idle proof succeeds. CI leaves the existing release in place rather
than attempting rollback. Save work, close browser workspace tabs, and retry the
deployment once the bridges disconnect.

DWG import in the browser runs inside the shared hostd, which calls LibreDWG's
`dwg2dxf` as a subprocess (DWG export is native). LibreDWG is GPL-3.0: it stays on the
server, is never linked into the engine, and never ships in the `/app` bundle.
Before anything else changes, `install_webd.sh` runs `scripts/provision_libredwg.py`
for the hostd unit's environment (`~/.local/share/Chudware/tools/libredwg/bin`, or
`$XDG_DATA_HOME/Chudware/...` when the user manager sets it). A verified install
returns at once; the first install downloads the SHA256-pinned source release and
needs a C toolchain (`gcc`, `make`). A failed provision is a pre-mutation refusal
(exit 4).

### Release retention and rollback

`install_webd.sh` keeps the newest `CHUDWARE_WEBD_KEEP_RELEASES` release
directories under `~/chudware-web/releases/` (default 3, an integer of at least
1, counting the release it just promoted) and deletes only what falls out of
that window. It previously deleted the previous release as soon as the new one
was promoted, so `releases/` held exactly one good tree and a bad promotion had
nothing to go back to. Retention is the prerequisite for rollback, not a
convenience: keeping N releases costs N full hostd/libchudware/WASM trees on
disk, which is the price of being able to undo a promotion without a rebuild.
Whatever `current` points at is never collected regardless of what the window
arithmetic says, and — unchanged from before — a *failed* promotion still
leaves its partial tree behind for inspection.

The set of `releases/*/deploy-manifest` files is the release index; there is no
separate index file. Each is flat `key=value` (`schema=5`), and adds
`release_id=` and `built_at_utc=` (strict `YYYY-MM-DDTHH:MM:SSZ`) to the
existing `joint_sha256`, the `checkout_head` git rev it was built from, and the
per-artifact digests. The verified `chud` binary, checked-in Chudcode artifact
lock, `CHUDCODE-NOTICE`, and `CHUDCODE-THIRD-PARTY-NOTICES.txt` each have their
own SHA-256 and participate in the joint hash. Rollback rechecks both the
manifest digests and the notice hashes/provenance recorded in the lock before
moving `current`. Advisor-enabled releases likewise hash the exact ONNX Runtime
SONAME payload and four-file `polymesh-advisor/` package, so either model or
agent-runtime changes produce a new joint release identifiable from disk alone.

Release ids are `YYYYmmddTHHMMSSZ-<joint12>-<pid>`. The timestamp is UTC: it is
what retention sorts on lexicographically, and a local-time stamp is
non-monotonic across a DST fall-back hour, which could make the collector
delete the wrong release.

`Webservice/deploy/rollback.sh` is the reverse actuator and takes no build
step:

- `--list` prints the retained releases newest first and marks the one
  `current` points at.
- Bare, or `--to <release-id>` (e.g.
  `--to 20260822T171130Z-4f2a9c1d3b07-8241`), swaps `current` atomically to
  that release, drains and restarts `chudware-hostd` through the same sequence
  `install_webd.sh` uses, and rewrites `.webd-running-joint` to the restored
  release's joint sha. The default target is the newest retained release that
  is not already current.
- `--force` skips the bridge drain, for when production is already broken and
  waiting out `CHUDWARE_WEBD_RESTART_MAX_WAIT` protects nobody.
- Exit 0 restored, 1 failure, 2 usage error or no rollback target available.
- Retained-release validation checks the Linux binary and both notices against
  that release's artifact lock. Unavailable downloads for macOS or Windows
  do not make a verified Linux release unrestorable, and locks written before
  the x64-only matrix (which also list Linux ARM/macOS ARM) still verify. The
  global `check-lock` publication gate requires exactly the three supported
  targets: Linux x86_64, macOS x86_64 and Windows x86_64. The committed lock
  pins the `v0.4.3` runtime (Windows pinned from the native CAD build).

Rollback restores an already-built tree and never touches PostgreSQL. A
promotion whose schema migration is not backward compatible is therefore not
recoverable this way — restore that deploy's `backups/pre-deploy-prod-<UTC>.dump`
first.

### Deployment workflow contract

Production is **continuous**: it tracks canonical `master`, not tags.

- **Staging:** react only to a successful completion of `linux-gate.yml` for
  the canonical repository's `master` branch, select the single
  `livingroom-pc` self-hosted Linux runner, and invoke
  `/home/liveuser/Repositories/CAD/Webservice/deploy/update.sh staging`.
- **Production:** `.github/workflows/deploy-prod.yml` fires on every push to
  canonical `master` and on manual dispatch.

  **Platform CI workflows are advisory; the live Stripe sandbox gate blocks promotion.**
  On the current fleet not one of them can conclude: `fast-gate` and
  `native-build` both matrix onto `cc-mac-arm` (macbook-air-m2, offline) and
  `native-build` only reports success when all four legs pass; `linux-gate` is
  the only workflow that concludes at all, and it carries a standing backlog of
  roughly fifty failures unrelated to shipping a web bundle — agent
  tool-guidance curation, MCP suites, torch, and DVC. Blocking on a workflow
  that cannot conclude does not make anything safer; it stops production tracking master,
  silently, behind a tree that looks green. That is exactly how production came
  to serve a bundle linked 2026-08-07 for fifteen days. All three states are
  read and reported into the run log and summary; none of them vetoes a release.

  What gates instead actually runs, in order:

  The separate **live Stripe sandbox gate** must succeed before promotion starts.
  The platform-build workflow states are advisory, not a substitute for this gate.

  1. **`scripts/check.py`, inline** — nine deterministic source audits, ~17 s,
     including icon-atlas coverage and ribbon-command-to-handler routing, the
     two surfaces most likely to be wrong in an asset or ribbon change. Hard
     fail. It runs under `~/.local/share/Chudware/mcp-venv` because
     `advertised_tool_audit` warms the real tool registry and needs PyYAML, the
     MCP SDK, NumPy and SciPy; under the system interpreter it warms a 7-tool
     catalog and fails for a reason unrelated to the commit.
  2. **`install_webd.sh`** — builds core, host and the WASM relink, validates
     the prompt payload, and asserts the linked bundle is byte-identical to the
     repo *before* swapping `current`. A broken build cannot reach production;
     the swap simply never happens.
  3. **Post-deploy verification**, then **automatic rollback** to the previous
     retained release plus a filed issue if any of it fails.

  Deployment is triggered only by master pushes and explicit manual dispatches.
  CI-completion events do not enqueue deployments: GitHub's concurrency group
  retains only one pending run, so an older CI completion could otherwise cancel
  the pending deployment of a newer master commit. Active promotions are never
  cancelled. Use `workflow_dispatch` to retry a failed promotion.

  It runs on the single `cc-linux-livingroom` runner as `liveuser` and invokes:

  ```
  CHUDWARE_DEPLOY_HEALTH_BASE_URL=http://127.0.0.1:8300 \
    /home/liveuser/Repositories/CAD/Webservice/deploy/update.sh prod --continuous
  ```

  `--continuous` waives only the `v*` tag requirement. The pre-deploy
  `pg_dump`, the `chudware_prod` database-name assert, the entitlement
  credential assert, the builds, the joint promotion, the drain-gated hostd
  restart, and both post-deploy health assertions are identical to a tagged
  run.
- **The tagged path is unchanged:** `update.sh prod --tag vX.Y.Z` still
  deploys a deliberate pinned release and still independently verifies that
  deployed HEAD is exactly that release tag. The CD workflow deliberately
  creates **no** tags: `release.yml` triggers on `push: tags: v*` and any tag
  the deploy path invented would spuriously build desktop installers.
- Production requires an explicit promotion policy: exactly one of
  `--continuous` or `--tag`. A bare `update.sh prod` exits **2** rather than
  guessing, and `--continuous` with `staging` or alongside `--tag` also exits 2.
  Under `--continuous` the deployed ref is `master`, because no tag ref exists
  for an untagged HEAD.
- `deploy-status.json` under the selected data root's `logs/` records
  `last.release`: the `v*` tag for a pinned deploy, and the full commit sha for
  a continuous one. That sha is the only identifier of what shipped in
  continuous mode, and it is what the `/admin` Deploy card displays.
- **On failure, roll back:** a failed deploy or failed post-deploy
  verification invokes
  `/home/liveuser/Repositories/CAD/Webservice/deploy/rollback.sh`, and the
  workflow run still fails. Rollback is remediation, not an excuse to report
  success.
- Deployment is one side effect, never a matrix: exactly one selected runner
  may execute it. Follow the single-runner selection shape of
  `native-build.yml`'s `runs-on: [self-hosted, …]` matrix rows — a label
  array, never a bare string.
- Serialize prod and staging through the updater's host-wide flock. Exit **3**
  means another deploy already holds `CHUDWARE_DEPLOY_LOCK`; CI treats 3 as a
  clean skip, because the deploy already in flight is promoting the same or a
  newer master commit. Every other nonzero exit — failed backup, migration,
  build, drain, or either health assertion — must fail the workflow.
- Export `CHUDWARE_DEPLOY_HEALTH_BASE_URL=http://127.0.0.1:8300` for every
  prod invocation. `post_deploy_health_assert` used to default its base URL to
  `http://127.0.0.1:$GATEWAY_PORT`, and `GATEWAY_PORT` was assigned only in the
  staging arm of the environment case, so under `set -euo pipefail` a prod run
  without that export died on an unbound variable *after* the backup, build and
  restart had already happened. The prod arm now sets `GATEWAY_PORT=8300`
  itself, so the export is belt-and-braces rather than load-bearing. A second
  single-arm variable was found and fixed the same way: `EXPECTED_DB` was
  prod-only, so every `update.sh staging` run aborted under `set -u` inside
  `assert_database_target` — the very assertion described above — and the
  staging arm now sets `chudware_staging` explicitly.
- Every job must be `self-hosted`; do not upload deployment artifacts. The
  canonical checkout at `/home/liveuser/Repositories/CAD` must be clean before
  invocation because the updater deliberately uses `git pull --ff-only` and
  refuses local changes.

**Why the policy changed.** The tag-gated design made promotion a thing a
human had to remember, and between 2026-08-07 and 2026-08-22 nobody did:
production served a bundle 15 days stale. The web client bakes its assets into
`Chudware.data` at Emscripten **link** time, so unlike the desktop build it
cannot pick up an asset change from disk. A missed promotion is invisible to
logs and to `/healthz` — it only surfaces when somebody looks at the pixels.
Automatic promotion removes the human step that failed.

**What it costs.** Every promotion restarts the shared `chudware-hostd` and
drains live `/engine` bridges, because the joint sha couples the WASM bundle to
the native daemon by design. Continuous deployment therefore means materially
more restarts than a tag cadence did. `CHUDWARE_WEBD_RESTART_MAX_WAIT`
(default 600s) is what keeps those restarts from dropping active users: the
installer waits for authenticated `engine_bridges` to reach 0 and fails closed
instead of restarting under load. Expiry refuses the deployment; shortening the
wait does not authorize disconnecting users.

Production WASM builds default to CMake `MinSizeRel`; Release's `wasm-opt -O3`
peaked above 9 GiB RSS and was OOM-killed on the 16 GiB deployment host.
Operators may set `CHUDWARE_WASM_BUILD_TYPE` explicitly on larger builders, but
the promoted artifact must still come from the installer's private stage.

The gateway unit and application both enforce
`CHUDWARE_WEB_WS_MAX_MESSAGE_BYTES` (4 MiB by default). Keep the unit template,
gateway setting, and reverse proxy limit aligned when changing it.

### Continuous asset-parity detection (INACTIVE)

`.github/workflows/prod-external-watch.yml` is the only check that inspects
what production actually serves rather than what a deploy believed it built.
Its asset-parity step is **inactive**: it requires the repository secret
`PROD_WATCH_PARITY_COOKIE`, which has never been created, so every five-minute
run emits a warning and skips the parity check.

That secret must contain the value of a `chudware_sid` session cookie for an
account that can reach the auth-gated `/app` — the same server-side session row
a normal sign-in creates. Without it `scripts/check_web_parity.py --url` cannot
see the bundle at all, because `/app` is behind sign-in.

Until the secret exists, production asset parity is verified **only at deploy
time**, by `install_webd.sh`'s own `check_web_parity.py --bundle` gate over the
staged release. That gate proves the release it is about to promote is
self-consistent; it says nothing about what the live gateway is serving
minutes or days later, so a stale `current`, a half-swapped symlink, or a
hand-edited `app/` tree would go undetected.

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
