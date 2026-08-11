# chudware-web — self-hosted multi-user Chudware in the browser

Ratified design (advisor: `work/questions/T-WEB1.md`, research: `Webservice/research/`).
Original design date 2026-07-14; **revised 2026-08-10** when the streamed-session
product was removed. Host: Fedora 44, Ryzen 7600X3D 6C/12T, 32 GiB RAM, RTX 3080 Ti 12 GiB.

The browser runs a WASM build of `host/src/ui`. There are no per-user containers,
no pixel streaming, and no session manager: every signed-in account shares one
`chudware-hostd` daemon, and isolation is enforced in the gateway.

## Architecture

```mermaid
flowchart LR
  B[Browser: WASM /app] -->|HTTPS/WSS| CF[Cloudflare Tunnel + Access\nGoogle IdP allowlist + ua.edu email OTP]
  CF -->|Cf-Access-Jwt-Assertion| GW[Gateway: FastAPI on 127.0.0.1:8300\nauth + /engine bridge + workspace/bucket + chat WS + admin]
  GW -->|unix socket, NDJSON-RPC| WD[(chudware-hostd\nONE shared daemon)]
  GW --> WS[(~/chudware-web-data/workspace/&lt;slug&gt;\nper-account trees)]
  GW --> PLM[(~/chudware-web-data/plm/*.git\ncentral bare vaults)]
```

## Decisions (advisor verdicts)
1. **Delivery**: WASM only. A WASM build of `host/src/ui` talks to the shared
   `chudware-hostd` through the gateway's WS `/engine` bridge
   (`Webservice/gateway/engine_bridge.py` ↔ `services/ipc/ipc_transport_wasm.cpp`),
   a byte-for-byte RPC/event pass-through, so engine/AI behavior is identical by
   construction. See `Webservice/README.md`.
2. **Auth**: Cloudflare Tunnel + Access. IdPs: Google (exact-email allowlist) + One-Time PIN
   for listed ua.edu addresses. Honest wording: OTP is email-verified school mailbox, NOT
   Entra OAuth. Phase-2 upgrade paths in RUNBOOK. Gateway MUST verify
   `Cf-Access-Jwt-Assertion` (iss/aud/exp vs team JWKS, 6-week key rotation) and map
   `email → user record`. `CHUDWARE_WEB_DEV=1` bypass for local testing only.
3. **PLM**: NO shared working-tree mount. Central bare folder remotes
   `~/chudware-web-data/plm/<project>.git`; per-account private clone under that
   account's workspace root.
   T-R7 subset in `services/plm/remote.py` (dulwich folder kind): seed/clone, `plm_add_remote`,
   `plm_sync` (fetch + linear replay + expected-old push, same-item_id collision →
   copy-as-variant §6.4), `plm_lock`/`plm_unlock` CAS on `refs/aicad/locks`. Additive under
   `aicad.plm/1`. Author injection: vault `_user_author()` reads `CHUDWARE_USER_ID` /
   `CHUDWARE_USER_DISPLAY`.
4. **Tenant isolation is the gateway's job.** The shared daemon resolves whatever
   absolute path it is handed, so containment cannot live below the gateway:
   `_inject_identity()` overwrites the client's claimed identity with the
   gateway-verified one, and `path_violation()` rejects any RPC naming a path
   outside the caller's own workspace/bucket roots. Provider-manager RPCs
   (`agent_provider_*`) are admin-only; client-supplied credentials are stripped.
5. **Resources**: the gateway is a single `MemoryMax=1G` unit. Per-account ceilings are
   upload size, disk quota across workspace + bucket, WS frame size, and per-account
   rate limits; `/engine` additionally caps concurrent bridges per account. The disk
   monitor touches `~/chudware-web-data/REFUSE_NEW` under disk pressure.
6. **Chat**: portal-level collapsible sidebar, gateway WS fan-out, SQLite persistence.
   Not inside the Chudware binary.
7. **Layout**: everything under `Webservice/` except the T-R7 subset (services/plm + MCP/worker
   wiring). Secrets only in gitignored files (`Webservice/Cloudflare-info.txt`,
   `Webservice/secrets/`); `config.example.env` committed instead.

## Component contract
- Gateway (port 8300): `/` portal; `/app` WASM client; `/engine` WS bridge to the shared
  daemon; `/workspace/*` and `/bucket/*` per-account file APIs; `/files/aicm` mesh fetch;
  `/ws/chat`; `/api/admin/*` allowlist + ops + terminal + releases; `/api/billing/*`;
  `/healthz` (bare `{ok}` anonymous; RAM/VRAM/presence/engine-bridge detail authenticated).
- Users file: `Webservice/gateway/users.yaml` — `email, display, role(admin|user), heavy(bool)`.
- Data root: `~/chudware-web-data/{workspace,bucket,plm,chat.db,logs}`.

## Monitoring (advisor §C)
systemd user units (gateway, cloudflared) `Restart=on-failure`; monitor timer (1–5 min):
free RAM, disk, `nvidia-smi` VRAM; JSONL log; `/healthz` for a remote glance.
No Prometheus in v1.

## Security notes
- **ROTATE the Cloudflare API token + R2 keys** from `Cloudflare-info.txt` — they were pushed
  to the public GitHub repo (untracked in ac1b912f, but history retains them).
- Gateway binds 127.0.0.1 only; the tunnel is the sole ingress; JWT verified on every request.
- The shared daemon is the blast radius: every `/engine` hardening rule in §4 is
  load-bearing, not defense in depth.
