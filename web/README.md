# Chudware Web

Chudware in the browser: the desktop ImGui GUI (`host/`) compiled to WebAssembly with
Emscripten, plus a small gateway that serves it and connects it to a CAD engine.

**Status: experimental, single operator.** The gateway has no accounts. Everyone who can
reach it acts as the one configured operator, with full access to that operator's
workspace and engine. Run it for yourself on your own machine, or behind your own
authenticating reverse proxy. It is not built for serving multiple users.

| Path | What it is |
|---|---|
| `gateway/` | FastAPI app. Serves the WASM bundle at `/app`, a workspace file API, and the `WS /engine` bridge to the operator's engine cell. |
| `deploy/cell/` | Cell runtime: bubblewrap launchers for hostd and agent actors, a CONNECT-only egress broker, and disk-quota admission. |
| `deploy/*.service`, `deploy/*.socket` | systemd user unit templates for the gateway, cells and egress brokers. |
| `deploy/install_cells.sh` | Stages the cell payload and installs the units. |
| `deploy/provision_cell_quota.py` | Root tool that provisions the kernel project quotas cells require. |

The gateway and cells are Linux-only. The browser client runs anywhere with
WebAssembly, WebGL2 and cross-origin isolation (`SharedArrayBuffer`).

## Build the browser client

```sh
./build.sh wasm
```

This needs [emsdk](https://github.com/emscripten-core/emsdk) at `~/.emsdk` (or `$EMSDK`).
It stages `index.html`, `Chudware.js`, `Chudware.wasm`, `Chudware.data` and Brotli/gzip
variants into `host/build-wasm/app/`. Set `CHUDWARE_WEB_APP` to stage somewhere else.
The build ends with `scripts/check_web_parity.py`, which fails if the assets baked
into `Chudware.data` differ from `assets/`.

To look at the UI without an engine, serve the bundle statically with the required
COOP/COEP headers:

```sh
python3 host/wasm/coop_server.py 8765 host/build-wasm/app
```

CAD and AI features need the gateway and an engine cell.

## Run the gateway

```sh
cd web/gateway
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8300 --ws-max-size 4194304
```

Then open <http://127.0.0.1:8300/app/>. Every setting is optional; see
`gateway/config.example.env`. The defaults are:

- operator `operator`;
- data under `~/.local/share/chudware-web` (`CHUDWARE_WEB_HOME`);
- workspace in `<home>/workspace` (`CHUDWARE_WORKSPACE_ROOT`);
- bundle from `host/build-wasm/app` (`CHUDWARE_WEB_APP`).

Endpoints:

| Route | Purpose |
|---|---|
| `GET /app/…` | The WASM bundle, with COOP/COEP, a wasm-capable CSP, and precompressed variants. `/` redirects here. |
| `WS /engine` | Newline-delimited JSON-RPC to the operator's engine cell. |
| `/workspace/list`, `download`, `upload`, `mkdir`, `rename`, `delete`, `copy` | Workspace files, as paths relative to the workspace root. |
| `/workspace/recents`, `/workspace/chat` | The client's recent-files list and Copilot chat store, kept in the workspace. |
| `GET /files/aicm` | Mesh files the engine cell published for an open document. |
| `GET /healthz` | Liveness. |

Tests (`pytest` is not in `requirements.txt`; install it into the venv first):

```sh
.venv/bin/pip install pytest
.venv/bin/python -m pytest              # from web/gateway
.venv/bin/python -m pytest ../deploy/tests     # cell runtime and quota policy
```

## Engine cells

The browser never talks to a shared daemon. Its engine is a `chudware-hostd` that runs
inside a cell, an OS-isolated sandbox that holds the operator's workspace. A
connection works like this:

1. A browser opens `WS /engine`. The gateway derives the cell id from the workspace key
   (`user:<operator>`) as the first 32 hex characters of its SHA-256. It then writes
   `<home>/cells/<id>/cell.json`, which binds the cell to the workspace root.
2. The gateway runs `systemctl --user start chudware-cell@<id>.socket`. The socket unit
   owns `<home>/cells/<id>/gw.sock`. The first connection starts
   `chudware-cell@<id>.service`.
3. `chudware-cell-launch` validates `cell.json` against `~/.config/chudware/cell-runtime.json`.
   It admits the workspace and cell-state directories only with a root-issued quota
   proof, then starts hostd under bubblewrap. Inside the sandbox:
   - the root is a tmpfs;
   - user, PID, IPC, UTS, network and cgroup namespaces are separate, and there are no
     capabilities;
   - the payload is mounted read-only at `/opt/chudware`;
   - the workspace is at `/work` and cell state at `/state`;
   - there is no host home, D-Bus or `/run/user`.
4. Agent tools run in nested `chudware-actor-sandbox` lanes. Each lane has its own
   state, and its workspace access is read-write or read-only. Network access goes
   only through the per-cell egress broker. That broker accepts `CONNECT <host>:443`
   to public unicast addresses only. To restrict destinations, set an exact-host
   `egress_hosts` list in `cell-runtime.json`.
5. The gateway forwards a browser frame only if `contracts/cell_methods.json` allows the
   method. It rewrites `hello` with the trusted principal and refuses identity or
   credential fields. It forwards a document's events and meshes only to the connection
   that opened that document.

The unit templates set cgroup limits (`MemoryMax=8G`, `CPUQuota=200%`, `TasksMax=512` for
a cell). Tune them with systemd drop-ins.

### Requirements

- systemd with a user manager. Run `loginctl enable-linger` to keep it running without
  a login session.
- bubblewrap at `/usr/bin/bwrap`, and unprivileged user namespaces enabled.
- `/usr/bin/python3` and `git`, used to install the worker requirements.
- An XFS or ext4 filesystem with project quotas enabled. It must hold the workspace and
  `<home>/cells`. Cells refuse to start without an enforced quota.

### Install

```sh
./build.sh daemon                 # hostd + engine library in core/build-occt-linux/bin
web/deploy/install_cells.sh
```

The installer stages the payload into `<home>/releases/<timestamp>/cell` and points
`<home>/current` at it. The payload contains:

- the engine;
- the verified Chudcode binary;
- a worker venv built from `requirements.txt`;
- `services/` and the agent prompts;
- the cell policy and the launchers.

The installer also does the following:

- creates `web/gateway/.venv`;
- writes a default `~/.config/chudware/cell-runtime.json` if none exists;
- installs the units into `~/.config/systemd/user/`.

It honours `CHUDWARE_WEB_HOME`, `CHUDWARE_WORKSPACE_ROOT`, `CHUDWARE_ENGINE_DIR` and
`CHUDWARE_CHUD_BIN`. Without `CHUDWARE_CHUD_BIN`, it downloads the pinned public
Chudcode release and verifies it against `vendor/chudcode/CHUDCODE_ARTIFACT.lock`.

### Provision quotas

Quota roots must exist and be empty when they are first provisioned. Create the
workspace and the cell-state directory, then describe both in a root-owned plan:

```sh
H=~/.local/share/chudware-web
ID=$(python3 -c 'import hashlib; print(hashlib.sha256(b"user:operator").hexdigest()[:32])')
mkdir -p "$H/workspace"
install -d -m 700 "$H/cells" "$H/cells/$ID" "$H/cells/$ID/state"
stat -c '%n device=%d inode=%i' "$H/workspace" "$H/cells/$ID/state"
```

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

Plan rules:

- Use absolute paths with no symlinks.
- `service_uid` is the uid that runs the gateway.
- `hard_bytes` must be a multiple of the filesystem block size.
- `project_id` must be a project not used by anything else on that filesystem.

Review the plan, then run as root:

```sh
sudo install -d -m 755 /etc/chudware/cell-quota.d
sudo web/deploy/provision_cell_quota.py --plan /etc/chudware/plan.json            # inspect
sudo web/deploy/provision_cell_quota.py --plan /etc/chudware/plan.json --apply    # enforce
```

The proof is bound to the boot. Re-run `--apply` after every reboot or remount, and
whenever the plan changes.

### Start

```sh
systemctl --user enable --now chudware-gateway
```

Logs are under `journalctl --user -u chudware-gateway`, `-u chudware-cell@<id>` and
`-u chudware-egress-broker@<id>`.

## Exposing it beyond localhost

The gateway binds to `127.0.0.1`. To reach it from elsewhere, put it behind a reverse
proxy that terminates TLS **and authenticates every request**: basic auth, mTLS, or an
identity-aware proxy. The proxy must:

- forward WebSocket upgrades on `/engine`;
- pass the original `Host` header;
- allow request bodies up to `CHUDWARE_WEB_MAX_UPLOAD_BYTES`.

Add the public hostname to `CHUDWARE_WEB_ALLOWED_HOSTS`.

## Security notes

- **No authentication.** Reaching the port means full control of the operator's
  workspace and engine. The listener is loopback by default, but other users on the
  same machine can still connect to it.
- **Browser attacks.** The gateway refuses `Host` headers that are not in
  `CHUDWARE_WEB_ALLOWED_HOSTS`, which blocks DNS rebinding. It also refuses WebSocket
  handshakes and state-changing requests whose `Origin` is a different site, so other
  web pages cannot drive the engine through your browser. Requests without an
  `Origin` header, such as from curl, are allowed.
- **File access.** Workspace paths are relative. Every file operation walks no-follow
  directory descriptors, so symlinks inside the workspace cannot reach outside it.
  Uploads and request bodies are size-capped before parsing.
- **Cell isolation.** The cell sandbox limits what a compromised engine or agent tool
  can reach. It has not been audited as a hostile multi-user boundary.
- **Cell network access.** By default, cell egress reaches any public HTTPS host. Set
  `egress_hosts` to restrict it.
- **No AI in the browser.** The gateway passes no AI provider credentials into cells,
  so Copilot turns that need a remote model are refused there. Use the desktop app
  for AI features.

## Phone and tablet controls

`/app/` uses compact controls at widths up to 900 CSS pixels or with a coarse
primary pointer. Device pixel ratio affects the rendering buffer, not UI sizing.

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

The startup panel lists any missing browser capabilities, such as WebAssembly, WebGL2,
or cross-origin-isolated shared memory. Mobile layout and input have been tested in
Chromium touch emulation. Physical iOS/Safari keyboard and safe-area behaviour have
not.
