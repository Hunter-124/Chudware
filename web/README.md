# Chudware Web

The desktop GUI (`host/`) compiled to WebAssembly, served by a gateway connected to a
native CAD engine and Python workers. Serving the WASM files alone is not a working CAD
server. Chudware's own software is free under [MIT](../LICENSE); third-party components
and the documented optional non-commercial backend retain their
[own terms](../THIRD_PARTY_NOTICES.md).

## Quickstart

Clone the public MIT source repository and enter its root:

```sh
git clone https://github.com/Hunter-124/Chudware.git
cd Chudware
docker compose -f web/compose.yaml up --build -d --wait
```

Open <http://127.0.0.1:8300/app/>. The first build compiles the native engine and browser
client; it is not a download of an advertised public prebuilt Chudware image.

### Prerequisites and sizing

- **Linux x86-64** with Docker Engine and the **Docker Compose v2 plugin, 2.24.4 or
  newer**. The HTTPS override uses Compose's `!reset` support. The old `docker-compose`
  Python command is not the supported interface. An ARM host would need x86-64
  emulation; that is not a supported native ARM deployment.
- Unprivileged user namespaces and nested bubblewrap isolation must work inside the
  container. Kernel policy, the container runtime, seccomp or an LSM can prohibit them.
  Startup probes real actor isolation and refuses to run if it cannot provide it;
  there is no unsandboxed worker fallback. Docker Desktop's VM and Podman providers
  are not assumed equivalent to a Linux Docker host. Rootless Podman can use
  `podman compose` with a compatible Compose provider, but must satisfy the same
  namespace and Compose-feature requirements.
- Internet access during the build for Fedora packages, public C++ dependencies,
  Python wheels and the Emscripten toolchain. The recommended container needs neither
  the optional online-agent SDK nor its executable because online Copilot is unsupported
  in this deployment; it retains the other root Python requirements and general workers.
- Budget a substantial native/WASM build, not a tiny web-server image. As a planning
  allowance, use **16 GiB RAM and 40 GiB free disk** for the build and caches; these
  are estimates, not measured minimums. Large models need additional runtime memory
  and storage. The default build parallelism is conservatively two jobs. To reduce
  peak build memory, use:

  ```sh
  docker compose -f web/compose.yaml build --build-arg BUILD_JOBS=1
  docker compose -f web/compose.yaml up -d --no-build
  ```

- A current browser with WebAssembly, WebGL2 and cross-origin-isolated shared memory
  (`SharedArrayBuffer`). Localhost HTTP is a secure-context exception; ordinary
  remote HTTP is not. Use [authenticated HTTPS](#remote-access-with-https-and-authentication)
  for another machine.

The image uses Fedora 44, Python 3.13 and Emscripten 6.0.3. It builds the matching
client/engine/contracts as one unit. It is a source/developer build, not a signed native
release. Tags for OS packages/base images and some transitive dependency constraints
can move: a source commit alone is not a byte-for-byte image lock. Record the source
revision and retain the built image for repeatable upgrades/rollback. The default local
image tag is `chudware-selfhost:local`; `CHUDWARE_IMAGE` can choose another locally built
or independently verified image. No registry reference here implies a published public
artifact.

### Scope and trust

**Experimental, one trusted operator.** The gateway has no accounts. Anyone who reaches
it controls that operator's workspace and engine, including other local users able to
reach the loopback port. Multiple proxy usernames do not create separate CAD users.
Do not offer this deployment to mutually untrusted users.

The base Compose publishes only `127.0.0.1:8300`; inside the container the gateway uses
port 8300 and one uvicorn worker. The application runs as UID/GID 10001 with a read-only
root filesystem, dropped capabilities and no-new-privileges. Compose explicitly relaxes
the outer seccomp/AppArmor profiles and system-path masks (`seccomp=unconfined`,
`apparmor=unconfined`, `systempaths=unconfined`) so nested bubblewrap can create namespaces,
mount its isolated filesystem and mount a fresh `/proc`. Docker's
[default AppArmor profile](https://docs.docker.com/engine/security/apparmor/) denies mounts;
masked parent procfs paths also prevent the nested procfs mount. These settings reduce
outer-container syscall, MAC and path filtering, not the actor sandbox. This is not
hardened hostile multi-user isolation. It adds no `privileged`, host namespaces,
`SYS_ADMIN`, host-home mounts or container-engine socket. Do not add those to work
around a failed sandbox probe.

Default runtime limits are four CPUs, 8 GiB memory (without extra container swap) and
512 processes. Override `CHUDWARE_CPUS` / `CHUDWARE_MEMORY` before Compose creation
when needed; these limits do not set the build's memory budget or impose a disk quota.

The explicit `CHUDWARE_WEB_CELL_BACKEND=container` selects this private container
boundary; an unset backend still selects the advanced systemd/quota path. General
Python workers remain in actor bubblewrap sandboxes with sanitized environments and
the existing public-HTTPS egress policy. The gateway retains trusted-principal method
admission, no-follow workspace access and document-specific mesh grants. It does not
forward your host environment or provider credentials.

**There is no portable per-owner hard disk quota in this container path.** Request
size caps and CPU/memory/PID limits do not cap volume growth. Workspace, logs, actor
state and caches can fill the host filesystem. Monitor storage and keep backups;
use the [advanced systemd path](#advanced-systemd-deployment-with-project-quotas) when
you require enforced filesystem project quotas.

**Browser limitations:** online Copilot is unavailable because remote-model credentials
are not passed into cells. General Python workers are included, but that does not
install every optional solver, firmware toolchain, external desktop application or
model weight. Features requiring those programs or native OS integrations are not
promised by this image. The browser build is not full desktop parity. Start with small
models, save explicitly, and keep independent copies of important work.

For optional owner-operated paid hosting, [contact the project](https://chudware.com/contact).
Payment covers operation, convenience and support for the open-source project; it
never unlocks native or self-hosted software. No checkout, price, automatic provisioning
or current hosting availability is implied.

## Persistence and saving

Compose creates three named volumes (their actual names have the Compose project prefix):

| Volume key | Container path | What persists |
|---|---|---|
| `work` | `/work` | Saved CAD documents, imported/exported files, hidden recents/chat stores and workspace project data. |
| `data` | `/data` | Gateway home, cell identity/metadata and private cell socket directory. |
| `state` | `/state` | Engine/actor state, caches, recovery information, logs and published meshes. |

The immutable application, native engine, contracts and Python environment are under
`/opt/chudware`; browser assets are under `/opt/chudware/app`. Those come from the image,
not a data volume. Do not mount the whole checkout or your home directory over them.

Use **File → Save / Save As** to save a document into the server workspace before
stopping, restarting or replacing the container. Uploading a local file copies it to
that workspace; saving there is not a download to your laptop. Use the workspace file
picker/download action to obtain a separate local copy. An open unsaved document is
not a durable backup: clean engine termination can discard session autosaves. Crash
recovery state is not a replacement for a saved `.step+` file.

Keep `CHUDWARE_WEB_OPERATOR` stable (default `operator`). Changing it changes the cell
identity and can make existing metadata/state inconsistent. Keep the same Compose
project name and volume mapping across upgrades: changing `-p`, `COMPOSE_PROJECT_NAME`
or the project directory convention can select empty new volumes rather than your old
files. Do not change these settings to fix a missing-files symptom without first
locating the original volumes.

### Stop, restart and uninstall without deleting data

Save open documents first, then use the same checkout, environment and project identity
as at installation:

```sh
docker compose -f web/compose.yaml stop
docker compose -f web/compose.yaml start
# Or replace the running application processes:
docker compose -f web/compose.yaml restart
```

The supervisor restarts the engine after an idle exit or failure. A gateway/container
restart drops WebSockets and in-memory mesh grants: reload/reconnect and reopen the
saved document if the view is stale.

To remove containers and the project network while preserving named volumes:

```sh
docker compose -f web/compose.yaml down
```

This is also the safe uninstall path. Keep your backup, image/revision information and
volumes if you might reinstall. **Do not add `-v` / `--volumes`, run `docker volume prune`,
or remove the named volumes unless you intentionally want to delete the stored work.**
Removing a checkout or application image alone is not a backup and does not remove the
named volumes.

## Backup and restore

Back up all three volumes together, not just visible CAD files. Save documents, close
browser sessions and stop the service for a consistent snapshot. These commands stream
an archive to your own shell, so they do not need a host directory writable by UID 10001:

```sh
umask 077
docker compose -f web/compose.yaml stop
docker compose -f web/compose.yaml run --rm -T --no-deps --entrypoint tar \
  chudware -czf - -C / data work state > chudware-backup.tar.gz
docker compose -f web/compose.yaml start
```

Sockets are ephemeral and tar may report that it ignored them; the runtime recreates
them. Do not ignore actual read/write errors or a failed archive command. Check the
archive exit status before restarting or upgrading, copy it to another disk/machine,
and protect it as confidential project data. Retain the old source revision, image ID
(and preferably `docker image save` output), Compose configuration and operator setting.
A routine restore drill into a separate project is stronger evidence than an archive
that has never been read.

Restore **only into empty volumes**, using the same image/revision and operator that
created the backup. Do not overlay a backup onto a live workspace: newer files would
remain and state could be inconsistent. For example, choose a new, unused project
name; stop the original project before starting this one because both use port 8300:

```sh
COMPOSE_PROJECT_NAME=chudware-restore docker compose -f web/compose.yaml \
  run --rm -T --no-deps --entrypoint tar chudware -xzf - -C / < chudware-backup.tar.gz
COMPOSE_PROJECT_NAME=chudware-restore docker compose -f web/compose.yaml up -d --no-build
```

Do not reuse `chudware-restore` if it already has data; pick another unused project name.
Open the restored saved documents and inspect them before retiring the original
volumes. Keep the chosen new project name for subsequent operations. With a custom
image, pass the same `CHUDWARE_IMAGE` to both commands.

For HTTPS, use the same extra `-f web/compose.https.yaml` and `--env-file` options on
all commands in this guide. The optional proxy also persists `caddy_data` (ACME
account/certificates) and `caddy_config`. With **both services stopped**, archive these
separately:

```sh
docker compose --env-file "$HOME/.config/chudware-web.env" \
  -f web/compose.yaml -f web/compose.https.yaml \
  run --rm -T --no-deps --entrypoint tar caddy \
  -czf - -C / data config > caddy-backup.tar.gz
```

Before starting a restored HTTPS project, restore this second archive into its empty
proxy volumes as well:

```sh
COMPOSE_PROJECT_NAME=chudware-restore docker compose \
  --env-file "$HOME/.config/chudware-web.env" \
  -f web/compose.yaml -f web/compose.https.yaml \
  run --rm -T --no-deps --entrypoint tar caddy \
  -xzf - -C / < caddy-backup.tar.gz
```

Retain the private environment file, old proxy image and configuration separately.
Losing proxy state does not delete CAD work, but restoring it avoids unnecessary
certificate reissuance/rate limits. Never commit credentials or backups.

## Upgrade and rollback

1. Save documents and make a stopped, complete backup as above. Keep the previous
   checkout, Compose settings, image and backup until the upgrade is accepted. Do not
   discard your only working image to reclaim build space.
2. Select an explicitly reviewed source revision in a separate checkout; do not blindly
   replace an installation with a moving branch. Run commands with the **same Compose
   project identity, operator and volumes**. Read that revision's release notes for
   file/state migrations. There is no automatic backwards-compatibility guarantee.
3. Preserve the old default image tag, then build and start the new source:

   ```sh
   docker image tag chudware-selfhost:local chudware-selfhost:rollback
   docker compose -f web/compose.yaml build --pull
   docker compose -f web/compose.yaml up -d --no-build
   ```

   If you set `CHUDWARE_IMAGE`, preserve/tag that image instead and consistently select
   your new tag. `--pull` may update base/package inputs; record the resulting image ID,
   not merely the source revision. An image upgrade does not intentionally remove the
   named volumes.

   For HTTPS, also retain/tag `chudware-selfhost-proxy:local` before rebuilding.
   `CHUDWARE_PROXY_IMAGE` selects that proxy image just as `CHUDWARE_IMAGE` selects
   the application. Restore the old proxy image/configuration on rollback and keep
   the HTTPS override present; do not fall back to an unauthenticated port.
4. Check logs and health, then open a saved document, perform a small model operation,
   save and reopen it. Keep the backup untouched until you are satisfied.

If the new image has not changed persisted formats, switch back using the old checkout
and the retained image without rebuilding it:

```sh
CHUDWARE_IMAGE=chudware-selfhost:rollback docker compose -f web/compose.yaml up -d --no-build
```

If it has written incompatible state/files, **stop it and restore the pre-upgrade backup
into empty volumes** using the rollback image and old Compose configuration. The restore
procedure above preserves the failed/newer volumes for recovery rather than deleting
them. Running old code against migrated state is not a safe rollback strategy. Refresh
the browser after either transition so client assets match the engine version.

## Remote access with HTTPS and authentication

Do not publish port 8300 directly or replace the loopback binding with `0.0.0.0` on a
public host. The optional `compose.https.yaml` adds Caddy, removes the application host
port, and terminates TLS plus HTTP Basic authentication for **every route**, including
WebSocket upgrades, files, workspace operations and the WASM bundle. The application
remains accessible only through the proxy network path.

You need a domain whose DNS points to this host and reachable TCP ports 80/443 for
Caddy's automatic public certificates. Rootless engines may require an administrator's
approved low-port forwarding configuration. For private/internal domains, use your own
trusted-certificate/authenticating proxy rather than pretending public ACME will work.

Generate a password hash interactively; do not pass the plaintext password on the
command line or store it in the environment file:

```sh
docker run --rm -it caddy:2.10.2-alpine caddy hash-password
```

Create a private environment file outside the checkout, for example
`$HOME/.config/chudware-web.env`, with mode `0600`. Put your actual hostname, chosen
username and the **complete generated hash** in it:

```dotenv
CHUDWARE_DOMAIN=cad.example.com
CHUDWARE_AUTH_USER=operator
CHUDWARE_AUTH_HASH='paste-the-generated-bcrypt-hash-here'
```

The single quotes preserve the hash's literal `$` characters in a Compose env file.
This example hash text must be replaced; it is not a working credential. Then, from
the repository root:

```sh
docker compose --env-file "$HOME/.config/chudware-web.env" \
  -f web/compose.yaml -f web/compose.https.yaml up --build -d --wait
```

Open `https://` followed by your configured hostname and `/app/`, and log in with the
username and original password. Use the same env file and both Compose files for
stop/start, backup, upgrade and uninstall; omitting the override could republish an
unauthenticated application port. The HTTPS override configures the expected public
Host allowance; Caddy preserves same-origin/WebSocket behavior. Do not disable
host/origin checks or strip COOP/COEP headers to make a proxy work.

Before exposing real files, confirm that unauthenticated HTML, workspace, mesh and
WebSocket requests are denied and authenticated access works. All authenticated
clients still act as **the same operator**. This is not account isolation. Public
hosting also requires your own host updates, monitoring, firewall and backup policy.

## Health, logs and troubleshooting

```sh
docker compose -f web/compose.yaml ps
docker compose -f web/compose.yaml logs --tail=200 chudware
curl --fail http://127.0.0.1:8300/healthz
```

`/healthz` is gateway **liveness**, not proof that the bundle, native geometry engine
or Python workers are ready. A meaningful functional check is to create a small part,
save it in `/work` through the UI, reopen it and exercise the Python-backed feature
you need. Use the HTTPS URL/authentication and full Compose options on a remote setup.

Compose's container healthcheck is stronger than this bare HTTP endpoint: it also
validates the payload and a ready native-cell hello. It still is not a full
modeling/save/reopen or Python-feature smoke test.

On a disposable local deployment, the repository also provides an end-to-end probe
(requires Python packages `httpx==0.28.1` and `websockets==16.1`):

```sh
python3 web/deploy/smoke.py http://127.0.0.1:8300
```

It checks browser assets/security, a real native box/save/mesh/reopen operation and a
general Python worker. It creates and removes only its UUID-named smoke file. To test
persistence, run with `--keep --receipt /tmp/chudware-smoke.json` using a new receipt
path, recreate the disposable deployment without deleting its volumes, then run
with `--resume /tmp/chudware-smoke.json`. The probe does not start containers and does
not bypass proxy authentication; use it only against the local trusted endpoint.

| Symptom | What to check |
|---|---|
| Build killed / out of memory | Lower `BUILD_JOBS` to 1, increase build RAM/swap, and check host/container storage. Runtime limits are not build-resource limits. |
| Startup reports namespace/bubblewrap failure | Read the failed isolation probe in logs. Retain the documented seccomp/AppArmor/system-path options and check Linux unprivileged-user-namespace and SELinux policy. Do not add privileged mode, host mounts or an unsandboxed worker fallback. Use a host that supports the boundary or the advanced systemd path. |
| Container starts but no usable geometry | Check engine/supervisor errors in logs, then try an actual new part operation. A successful health response or WebSocket handshake alone is insufficient. |
| Blank client or missing shared-memory/WebGL support | Use a current browser, inspect its startup capability report, and use localhost or HTTPS. The proxy must retain COOP/COEP, correct WASM MIME type and WebSocket upgrades. |
| `400` host / `403` origin | Use the configured hostname. An external proxy must preserve Host, forward WebSocket upgrades and use the same scheme/origin as the page. Do not permit arbitrary hosts or origins. |
| Files seem missing after restart/upgrade | Check the Compose project name, named-volume mapping and stable operator setting. A new project usually creates empty volumes; do not delete the old ones. |
| Permissions or state-path errors after custom mounts | The default named volumes are owned by UID/GID 10001. Custom mounts must be writable by that identity, private, and free of symlinked path components; do not use `chmod 777`. Prefer the documented named volumes. |
| Disk fills despite container memory limits | The container has no portable disk quota. Check all volumes and image/build-cache growth, preserve backups, then deliberately reclaim obsolete data. |
| Copilot remote-model turn refused | Expected browser limitation. Do not inject host credentials into the cell. Use a native setup with the optional runtime/SDK and your own provider configuration. |
| Proxy fails to get certificates | Check DNS, ports 80/443, rootless low-port forwarding, firewall and Caddy logs; retain its certificate volume to avoid repeated issuance. |

Include the source revision/image ID, host/runtime/Compose versions and relevant
redacted logs in a bug report. Never paste environment files, passwords, provider keys
or confidential project data into public issues.

## Source-only browser development

For a native desktop build, see [the root README](../README.md#building-from-source).
For a browser client without containers:

```sh
./build.sh wasm
```

This needs [emsdk](https://github.com/emscripten-core/emsdk) at `~/.emsdk` or `$EMSDK`.
It stages `index.html`, `Chudware.js`, `chudware.wasm`, `chudware.data` and compressed
variants into `host/build-wasm/app/`; `CHUDWARE_WEB_APP` can select another stage
location. The build finishes with the existing asset-parity check.

To inspect the GUI **without an engine**, serve those assets with the required headers:

```sh
python3 host/wasm/coop_server.py 8765 host/build-wasm/app
```

For an actual source-backed gateway, first install/configure the engine cells below,
then create `web/gateway/.venv` and install `web/gateway/requirements.txt`. Run from
`web/gateway`:

```sh
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8300 --ws-max-size 4194304
```

The default backend is `systemd`, operator `operator`, home
`~/.local/share/chudware-web`, workspace `<home>/workspace`, and bundle
`host/build-wasm/app`. Settings are listed in `gateway/config.example.env`.
The routes are `/app/…`, `WS /engine`, `/workspace/…`, `GET /files/aicm` and
`GET /healthz`. Workspace file operations are relative to the workspace root and
walk no-follow directory descriptors; published mesh files require document grants.
Tests/check guidance is in [CONTRIBUTING.md](../CONTRIBUTING.md#checks).

## Advanced systemd deployment with project quotas

This is the existing Linux operator-managed path, **not a prerequisite for Compose**.
It requires a systemd user manager and root-provisioned filesystem quotas. Its installer
also installs the optional agent SDK/runtime from the pinned release assets in this
repository, even though online Copilot remains unsupported in browser cells.
Do not change its quota admission to bypass a failed proof.

The browser never talks to a shared desktop daemon. The gateway derives the cell id
from `user:<operator>` (first 32 hex characters of SHA-256), writes private
`<home>/cells/<id>/cell.json`, starts `chudware-cell@<id>.socket`, and connects to the
pinned `gw.sock`. The first connection activates `chudware-cell@<id>.service`.

`chudware-cell-launch` validates metadata against
`~/.config/chudware/cell-runtime.json` and admits workspace/state only with a
root-issued quota proof. Bubblewrap provides a tmpfs root, separate user/PID/IPC/UTS/
network/cgroup namespaces, no capabilities, a read-only payload at `/opt/chudware`,
workspace `/work` and state `/state`; there is no host home, D-Bus or `/run/user`.
Nested actors retain read-only/read-write workspace lanes and scoped state. Egress
uses the cell CONNECT broker, public unicast HTTPS only; restrict destinations with
an exact `egress_hosts` list in `cell-runtime.json`. The gateway's method policy,
trusted principal and per-document event/mesh ownership remain enforced.

The cell units default to `MemoryMax=8G`, `CPUQuota=200%`, `TasksMax=512`; tune using
systemd drop-ins. This boundary has not been audited for hostile multi-user service.

### Requirements and installation

- systemd user manager; use `loginctl enable-linger` for operation without a login.
- `/usr/bin/bwrap`, unprivileged user namespaces, `/usr/bin/python3` and `git`.
- XFS or ext4 with project quotas enabled, holding the workspace and `<home>/cells`.
  A cell refuses to start without an enforced quota.
- Network access to the pinned SDK source archive and native runtime release assets in
  this repository (or `CHUDWARE_CHUD_BIN` pointing to an artifact the lock can verify).

```sh
./build.sh daemon
web/deploy/install_cells.sh
```

The installer stages the engine, verified optional-agent binary/notices, full worker
venv, services/prompts, policy and launchers under `<home>/releases/<timestamp>/cell`,
then updates `<home>/current`. It also creates the gateway venv, default runtime config
and systemd user units. It honors `CHUDWARE_WEB_HOME`, `CHUDWARE_WORKSPACE_ROOT`,
`CHUDWARE_ENGINE_DIR` and `CHUDWARE_CHUD_BIN`. Without the latter it downloads and verifies
the locked runtime asset. Build/stage the browser client separately as described above.

### Provision quotas

Quota roots must exist and be empty when first provisioned. Create them and record
their device/inode values:

```sh
H=~/.local/share/chudware-web
ID=$(python3 -c 'import hashlib; print(hashlib.sha256(b"user:operator").hexdigest()[:32])')
mkdir -p "$H/workspace"
install -d -m 700 "$H/cells" "$H/cells/$ID" "$H/cells/$ID/state"
stat -c '%n device=%d inode=%i' "$H/workspace" "$H/cells/$ID/state"
```

Create a root-owned `/etc/chudware/plan.json`, replacing the uid, paths, cell id,
device/inode numbers and project id with the actual values for your host:

```json
{
  "schema": "chudware-cell-quota-plan-v1",
  "service_uid": 1000,
  "owners": [{
    "owner_key": "user:operator",
    "budget_bytes": 21474836480,
    "budget_inodes": 1000000,
    "groups": [{
      "project_id": 4201,
      "hard_bytes": 21474836480,
      "hard_inodes": 1000000,
      "roots": [
        {"workspace_key": "user:operator", "kind": "workspace",
         "path": "/home/me/.local/share/chudware-web/workspace", "device": 2049, "inode": 1234},
        {"workspace_key": "user:operator", "kind": "state",
         "path": "/home/me/.local/share/chudware-web/cells/<id>/state", "device": 2049, "inode": 1235}
      ]
    }]
  }]
}
```

Use absolute paths without symlinks; `service_uid` is the gateway user, `hard_bytes`
must be a multiple of the filesystem block size, and the project id must not be used
by anything else on the filesystem. Review the plan, then run:

```sh
sudo install -d -m 755 /etc/chudware/cell-quota.d
sudo web/deploy/provision_cell_quota.py --plan /etc/chudware/plan.json
sudo web/deploy/provision_cell_quota.py --plan /etc/chudware/plan.json --apply
systemctl --user enable --now chudware-gateway
```

Quota proof is boot-bound. Reapply after every reboot/remount and whenever the plan
changes. Logs use `journalctl --user -u chudware-gateway`, `-u chudware-cell@<id>` and
`-u chudware-egress-broker@<id>`. For remote access to this non-Compose path, supply your
own TLS proxy authenticating **all** routes, forwarding WebSocket upgrades and original
Host, accepting the configured upload size, and adding its hostname to
`CHUDWARE_WEB_ALLOWED_HOSTS`. Host/origin checks are not authentication; non-browser
requests without an Origin header remain allowed.

## Phone and tablet controls

`/app/` uses compact controls at widths up to 900 CSS pixels or with a coarse primary
pointer. Device pixel ratio affects the rendering buffer, not UI sizing.

- **File** opens/saves documents; **More** contains Settings, Search, Web and the
  project folder. The workspace button selects Model, Drawing, Simulation and the
  other available workspaces.
- The ribbon's tab selector and **Commands** menu expose the full command groups.
  Drag vertically to scroll long command menus.
- **Files** and **Chat** toggle separate drawers; only one is open at a time.
- Tap a text field, then **Keyboard** in the bottom bar to open the phone keyboard.
  **Hide keyboard** dismisses it. Unicode/composition and multiline paste feed the
  selected field; pasting a newline does not submit chat.
- Drag on the model to orbit, or choose **Pan**. Pinch to zoom and drag two fingers
  to pan. After a pinch, lift both fingers before starting another one-finger drag.
  Camera gestures do not start over a drawer or menu.
- The canvas follows the browser's visible viewport and safe-area insets. The
  bottom control strip is reserved space, not an overlay over the composer.

The startup panel lists missing WebAssembly, WebGL2 or isolated-shared-memory support.
Mobile layout/input has been tested in Chromium touch emulation; physical iOS/Safari
keyboard and safe-area behavior has not.
