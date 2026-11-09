# Chudware public demo front door

A host-side service that offers a **free, public, throwaway** Chudware web session at
`app.chudware.com` without accounts and without exposing the host. It is not part of the
container image and is separate from the single-operator [gateway](../README.md): that
gateway trusts whoever reaches it, this front door is what makes reaching it safe for a
stranger.

```
 browser ──https──> Cloudflare ──tunnel──> 127.0.0.1:8300  front door (this package)
                                                │  queue · admission · expiry · proxy · AI broker
                          ┌─────────────────────┼─────────────────────┐
                          ▼                     ▼                     ▼
              127.0.0.1:18300         127.0.0.1:18301         127.0.0.1:18302      (published ports)
              podman container        podman container        podman container     (max 3 at once)
              (gateway + engine)      │  /run/chudware-demo/provider.sock  ◄── one unix socket per session
                                      ▼                                        served by the front door
                                  OpenRouter (free models) · Anthropic (Claude), owner keys held only by the front door
```

* **Queue.** At most 3 sessions run at once; everyone else waits in a FIFO queue with a
  waiting page (position, estimated wait, automatic hand-over to the app).
* **Sessions.** 15 minutes idle or 45 minutes total, whichever comes first. The app shows a
  banner 5 minutes before either, with a *download your work* action. At expiry the container
  and **all** of its data are destroyed (tmpfs only; nothing is ever written to disk).
* **Isolation.** Every session is its own rootless podman container running as a dedicated
  host user whose network egress is firewalled away from the LAN/tailnet (below).
* **AI.** Free OpenRouter models plus Claude Opus/Sonnet/Haiku 5.5 on the owner's Claude API
  key, through a per-session unix socket. Both keys are read by the front door alone and
  never enter a container.
* **No accounts.** An anonymous signed cookie names the queue entry / session.

## Behaviour

| | |
|---|---|
| Join | Explicit button on `/` (`POST /demo/join`); crawlers and link previews never take a slot. |
| One per client | At most one active-or-queued entry per client address (IPv6 clients share a /64). The address is `CF-Connecting-IP`, trusted **only** from a loopback peer (the listener is loopback behind cloudflared). |
| Abuse limits | Queue max 30; max 6 joins per client per 10 minutes. |
| Holding a place | A queued visitor must keep asking `GET /demo/status`; an entry not polled for 60 s is dropped. The waiting page keeps one long-poll (`?wait=20`) in flight, which is not a timer, so a background tab whose timers the browser throttles keeps its place. |
| Admission | When a slot frees, the head is admitted: container started, `/healthz` awaited (90 s) → `active`; on failure the entry ends as `start_failed`, everything is cleaned up and the next visitor is admitted. |
| Capacity guard | Admission is refused while host `MemAvailable` < 4 GiB or free disk on podman's storage < 3 GiB (sessions still *starting* count 1 GiB each against the floor). The visitor stays queued with `note: "capacity"`; an unreadable probe fails closed. |
| Activity | Proxied HTTP requests and client-to-server WebSocket frames count; `/healthz`, `/demo/status` polls and pings do not. Any periodic background traffic from the app therefore keeps a session from idling, but the 45-minute hard limit always applies. |
| Expiry | Checked every second. Every 15 s the front door also reconciles with podman: a session whose container died ends as `container_exited`, and any untracked `chudware-demo-*` container is removed. |
| Graceful stop | `systemctl stop` ends every session (so no container outlives its reaper). |
| Crash / SIGKILL | On the next start, labelled running containers whose labels are complete, whose host directory exists, that pass a health probe and are within their hard lifetime are **adopted** (quota counters restored); everything else is removed. Adoption requires a persistent `CHUDWARE_DEMO_COOKIE_KEY` (otherwise nobody could reclaim a session, so all leftovers are removed). |

## HTTP surface

Own routes: `GET /`, `GET /demo/status`, `POST /demo/join`, `POST /demo/end`,
`GET /demo/health`, `GET /demo/static/{waiting.js,waiting.css}`.
Proxied to an **admitted** visitor's container only: `/app`, `/app/*`, `/engine` (WebSocket),
`/workspace/*`, `/files/*`, `/reveng/*`, `/healthz`. Everything else is a 404.

`GET /demo/status` (cookie-scoped, never cached):

```json
{"state":"queued|starting|active|expired|none","position":int|null,"queue_length":int,
 "eta_seconds":int|null,"idle_expires_at":epoch|null,"hard_expires_at":epoch|null,"warn":bool,
 "note":null|"capacity"|"idle"|"hard_limit"|"ended"|"queue_timeout"|"start_failed"|"container_exited"|"shutdown",
 "active_sessions":int,"max_sessions":int,"ai_remaining_today":int|null,"claude_available":bool}
```

The first seven keys are the cross-slice contract. Additions: `note` (why a session ended,
or `capacity` while the host is full), the session counts, `ai_remaining_today` (`null` when
no OpenRouter key is configured), `claude_available` (true when a Claude key is configured
and neither the daily nor the monthly USD budget is spent) and the state `none`, returned
only to a caller without a valid cookie (an in-app caller whose session no longer exists
gets `expired`). `?wait=N` (clamped to 25)
holds the request until the state or position changes. `warn` is true when either the hard
or the idle deadline is ≤ 5 minutes away. `eta_seconds` is a rough estimate: current
occupants free their slot at min(hard, idle) expiry; visitors ahead are assumed to hold one
for the median of recent sessions (half the hard limit until there is history).

Mutating routes require POST and refuse a cross-origin `Origin`. The session cookie is
`__Host-cw_demo`, `HttpOnly; Secure; SameSite=Lax; Path=/`, an HMAC-signed random id.

## AI lane

Per session the front door serves an OpenAI-compatible endpoint (free models) and a native
Claude Messages endpoint on `<session dir>/run/provider.sock`, mounted at
`/run/chudware-demo/provider.sock` (socket mode
0666 in a 0711 directory: the container user can connect, never write). The socket **is** the
identity.

| | |
|---|---|
| `GET /v1/models` | The allowlist ∩ OpenRouter's live `/models` (cached 10 min), so a retired model disappears instead of erroring. Entries carry OpenRouter's `name`, `context_length`, `top_provider`, `architecture` and `supported_parameters` when known. `?output_modalities=…` (OMP's rerank/decision discovery) answers an empty list; `/v1/images|embeddings|videos/models` are an instant local 404. Probes are never counted or forwarded. |
| `POST /v1/chat/completions` | Streaming allowed. `model` must be allowlisted, else `403 model_not_allowed`. |
| Allowlist | `CHUDWARE_DEMO_AI_MODELS`; every entry must end in `:free` or the front door refuses to start. Default: `nvidia/nemotron-3-ultra-550b-a55b:free`, `nvidia/nemotron-3.5-super-vl:free`, `google/gemma-4-31b-it:free`, `cohere/north-mini-code:free`. |
| Per-session cap | 15 requests and 200 000 total tokens (reported `usage`; a byte-length estimate when a stream ends without one) → `429 demo_quota_exhausted`. Counters survive a front-door crash. |
| Daily cap | 45 requests across all sessions per UTC day (the owner's key is on OpenRouter's free tier, ≈ 50 free-model requests/day) → `429 demo_ai_daily_limit` ("The demo's free AI allowance for today is used up; modeling still works"). An upstream 429 that names a per-day limit exhausts the budget too; a per-minute 429 is `429 demo_ai_rate_limited` with `Retry-After`. Shown on the waiting page via `ai_remaining_today`. |
| Key | Added by the front door; any client `Authorization`/`X-Api-Key`/cookie is dropped. A key OpenRouter refuses (401/402/403) is reported to the visitor only as `503 ai_unavailable` and logged. No key configured: `503 ai_unavailable`, the rest of the demo works. |
| Forwarding | `models`, `route`, `provider`, `plugins`, `transforms`, `preset`, `web_search_options` are stripped (they could route to a paid model or add paid plugins); output is capped at 16 384 tokens per request; streams get `include_usage`. |
| Logging | Per request: session id prefix, model, and counters. Never prompts, completions or the key. |
| Retries | Deterministic refusals (`model_not_allowed`, `demo_quota_exhausted`, `demo_ai_daily_limit`, `ai_unavailable`, `invalid_request`, `request_too_large`) carry `x-should-retry: false`, which OMP's transport honours; without it a 429 is re-POSTed five times. The transient `demo_ai_rate_limited` does not. |
| `POST /v1/messages`, `POST /v1/messages/count_tokens` | Native Claude Messages API, Claude models only; streaming allowed. The front door adds the owner's key (`CHUDWARE_DEMO_ANTHROPIC_KEY`, base `CHUDWARE_DEMO_ANTHROPIC_BASE`). `/v1/models` lists the Claude rows first, then the free rows. |
| Claude allowlist | `CHUDWARE_DEMO_CLAUDE_MODELS`, default and maximum exactly `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-haiku-5-5`; any other id (Fable, Mythos, …) is refused at config and with `403 model_not_allowed`. |
| Claude USD caps | Per session $10 (`CHUDWARE_DEMO_CLAUDE_SESSION_USD`), per client network per UTC day $10 (`…_CLIENT_DAILY_USD`), all sessions per UTC day $30 (`…_DAILY_USD`), per UTC calendar month $99 (`…_MONTHLY_USD`). A request is admitted only if each total plus its advance charge stays within its cap. Session and client caps → `429 demo_quota_exhausted`; daily and monthly → `429 demo_ai_daily_limit`. Errors use `{"type":"error","error":{"type":<code>,"message":…}}`. |
| Claude charging | Every turn is first counted with the (free) upstream `count_tokens`; the advance charge is that count at the dearest input rate (1 h cache write) plus the full `max_tokens` at the output rate. It is settled from the reported usage, including cache read/write pricing and the `iterations` of server-side work; Haiku 5.5 is billed $0.10 in / $0.50 out per MTok for prompts up to 100k tokens and $0.50 / $2.50 above. A stream cut before its final usage, or a read timeout after the request was sent, keeps the advance charge; only a connection that never opened is refunded. A correction settled after midnight (or month end) never lowers the newer period. The ledger persists at `state_dir/claude-spend.json`. |
| Claude forwarding | Only the fields of a plain turn are forwarded (`model`, `messages`, `system`, `max_tokens`, `stop_sequences`, `stream`, `temperature`, `top_p`, `top_k`, `tools`, `tool_choice`, `thinking`, `metadata`, `output_config`, `cache_control`, and `context_management` with `clear_*` edits only), so server compaction, fallbacks, MCP servers, containers, priority tier, fast mode and inference geo never reach the API. Server tools (tool `type` other than `custom`) are dropped. Image and document blocks must be inline (`base64`, `text` or `content` sources, at any nesting): a URL or file-id source could be small when counted and large when billed, so it is refused with `400 invalid_request`. `anthropic-beta` keeps only allowlisted families (interleaved thinking, fine-grained tool streaming, effort, context management, structured outputs, extended cache TTL, mid-conversation system/output-config/tool changes, prompt-caching scope, thinking controls and token count, task budgets). `max_tokens` is capped at 32 000 (`CHUDWARE_DEMO_CLAUDE_MAX_OUTPUT_TOKENS`) and a thinking budget is clamped below it. |
| Claude errors | An upstream 429 → `429 demo_ai_rate_limited` with the upstream `Retry-After`. 401/403, or a 400 mentioning a credit balance → `503 ai_unavailable` (logged, not shown). No Claude key: `503 ai_unavailable`. |

## Session container flags

`podman run -d --rm --name chudware-demo-<sid>` with these flags (built in
`podman.build_run_args`, asserted in `tests/test_podman.py`). The security set was measured on
the target host (Fedora 44, SELinux enforcing, rootless podman 5.8.9); two other candidate
sets failed.

| Flag | Why |
|---|---|
| `--user 10001:10001`, default rootless user namespace | The image's non-root uid; in the default mapping it is an unprivileged subuid on the host, not the demo user. (`--userns=keep-id` would run it *as* the demo user.) |
| `--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-privileges` | Same as `web/compose.yaml`. |
| `--security-opt systempaths=unconfined` | The nested bubblewrap actor sandbox mounts a fresh `/proc`; podman masks parent `/proc` paths and refuses (`bwrap: Can't mount proc on /proc`). |
| `--security-opt label=disable` | Needed on this SELinux host for the same nested mount. **Tradeoff:** SELinux confinement of the container is off. Remaining boundaries: default seccomp profile (deliberately *not* relaxed, `seccomp=unconfined` did not help), the user namespace (container root and uid 10001 are unprivileged subuids), no capabilities, read-only root, no-new-privileges, the nested bwrap sandbox that runs all untrusted worker code, and the dedicated host user with its egress firewall. |
| `--cpus 2 --memory 3500m --memory-swap 3500m --pids-limit 512` | Per-session ceiling; no swap. `--shm-size 256m`, `nofile 8192`, `core 0` as in compose. |
| `--tmpfs` on `/data` 256m, `/work` 1g, `/state` 1g, `/run` 64m, `/tmp` 512m, each `rw,nosuid,nodev,U,mode=0700` (`/tmp` 1777) | No named volumes, nothing on disk: destruction is `podman rm -f`. `U` is podman's spelling of compose's `uid=/gid=` (the image refuses a `/data` `/work` `/state` not owned by uid 10001 with mode 0700). tmpfs is charged to the memory limit, so a session filling it to the brim OOMs only itself (the caps sum to 2.8 GiB of the 3.5). |
| `--volume <session dir>/run:/run/chudware-demo:rw,nosuid,nodev,noexec` | The only host path in the container: the provider socket directory. |
| `--network pasta` | The rootless default. Measured: host loopback services are **not** reachable, the public internet is, the LAN **is** (hence the firewall). |
| `--publish 127.0.0.1:<port>:8300/tcp` | The inner gateway, on a free port in `PORT_BASE..` (loopback only). |
| `--pull=never --no-healthcheck --log-driver=k8s-file --log-opt=max-size=5m --rm` | Sessions never pull (`install.sh` pulls once); the front door does its own health probing; bounded logs; self-cleaning. |
| `--label chudware.demo[.sid/.port/.started/.ip]` | What adoption after a restart reads. |
| `--env CHUDWARE_WEB_DEMO=1 …` | The compose environment plus the demo flag. `CHUDWARE_WEB_ALLOWED_HOSTS=127.0.0.1,localhost`: the proxy always addresses the container as `127.0.0.1:<port>`. **No secret is ever passed.** |

The front door also runs `podman` with a scrubbed environment and removes the secrets from
its own environment at start.

### Proxy

Only a verified cookie naming an `active` session is proxied, and only to that session's own
port. Host and Origin are rewritten to `127.0.0.1:<port>` / `http://127.0.0.1:<port>` (what
the inner TrustedHost / same-origin checks need); the cookie, `Authorization`, `Cf-*`,
`X-Forwarded-*` are removed, upstream `Set-Cookie`/`Server`/`Date` are dropped, `Location`
headers pointing at the inner address are made relative, and COOP/COEP and every other
upstream header are preserved. Dot segments, backslashes, control characters and non-ASCII
targets are refused. A request body is capped at 110 MiB; a WebSocket frame at 4 MiB;
open WebSockets are closed the moment their session ends.

## Host isolation: dedicated user and egress firewall

Measured with default pasta: LAN addresses (e.g. the router) **are** reachable from a session
container. So the front door and its rootless podman run as a dedicated system account,
`chudware-demo`, and `deploy/egress.nft` firewalls that account's egress. Pasta originates
every container connection as a socket of the user running podman, so an output-hook rule on
`meta skuid "chudware-demo"` covers container traffic:

* accepted: `127.0.0.0/8`, `::1` (front door → published ports) and the public internet;
* rejected: `10/8`, `172.16/12`, `192.168/16`, `100.64/10` (CGNAT / Tailscale), `169.254/16`,
  `224/4`, `fc00::/7` (incl. Tailscale `fd7a:`), `fe80::/10`, `ff00::/8`.

It is its own table (`inet chudware_demo`) so firewalld does not flush it, and
`chudware-demo-egress.service` (root oneshot, ordered before the demo user's manager) loads
it at boot. Stopping that unit leaves the rules in place (fail closed); remove with
`nft delete table inet chudware_demo`. (Rules use `reject` rather than `drop` so a blocked
connect fails immediately instead of hanging.)

Residual risks, stated plainly:

* **DNS.** Pasta forwards container DNS to the host's resolvers. With systemd-resolved
  (`127.0.0.53`) nothing is needed. If `/etc/resolv.conf` names a LAN resolver, DNS breaks:
  add it to `dns_resolvers_v4/v6` in `egress.nft` (port 53 only) and reload.
* The `127.0.0.0/8` accept also covers anything pasta maps to the host loopback. Measured
  unreachable on this host; re-verify with the smoke commands after a podman/passt upgrade.
* `label=disable` (above).
* An exposed listener would let a client forge `CF-Connecting-IP` and evade the per-client
  limits: keep `--host 127.0.0.1`.

## Install

On the demo host (a systemd Linux with rootless podman, `nft`, `newuidmap`, `loginctl`),
from a checkout of this repository:

```sh
sudo web/demo/deploy/install.sh        # [--no-pull] [--no-start]
```

Idempotent. It: creates the `chudware-demo` user (nologin shell, home
`/var/lib/chudware-demo`, subuid/subgid ranges, linger); loads the egress firewall *before*
anything can start a container; installs the front door to `/opt/chudware-demo` (root-owned,
own venv from `web/gateway/requirements.txt`: no new dependencies); creates
`/etc/chudware-demo/demo.env` from `config.example.env` **only if absent** (mode 0600, owner
`chudware-demo`, fresh cookie key); installs and enables the user unit; pulls the session
image as that user; starts the service. Then:

1. Put the keys in `/etc/chudware-demo/demo.env` (`CHUDWARE_DEMO_OPENROUTER_KEY=…`,
   `CHUDWARE_DEMO_ANTHROPIC_KEY=…`; for Claude use a key from a dedicated Claude Console
   workspace with a monthly spend limit set, as a hard backstop behind the USD caps) and
   `sudo systemctl --machine=chudware-demo@.host --user restart chudware-demo.service`.
2. Point the existing tunnel (`app.chudware.com`) at `http://127.0.0.1:8300`.

Update: pull the repository and re-run `install.sh` (to change the image, set
`CHUDWARE_DEMO_IMAGE_TAG` in the env file first). Sessions are ended by the restart.
Logs: `sudo journalctl _UID=$(id -u chudware-demo)`. All settings: `config.example.env`.

The unit deliberately sets no `NoNewPrivileges` (rootless podman needs the setuid
`newuidmap`/`newgidmap`). The unit has `MemoryMax=1G`; sessions run in their own transient
scopes and are bounded by the flags above, not by the unit.

## Verification

Automated (isolated container; no podman, no network, no real OpenRouter):

```sh
scripts/testbox.py --suite platform                       # includes web/demo/tests
scripts/testbox.py -- python -m pytest web/demo/tests -q  # just this package
```

Covers: queue FIFO and 3-slot admission, poll-timeout drop, idle/hard expiry and the warn flag
(fake clock), start failure → next admitted, capacity guard, per-client and rate limits,
adoption of labelled containers, reconcile, the exact podman flag set, proxy refusing
non-admitted/queued/tampered cookies, header isolation, Host/Origin rewriting, WebSocket
proxying and teardown, allowlist 403, quotas 429, daily budget, missing-key 503, client
`Authorization` stripped, streaming usage, model catalogue, deploy-asset invariants.

Smoke on the demo host (after `install.sh`; `dp` runs podman as the demo user):

```sh
dp() { (cd / && sudo -u chudware-demo env HOME=/var/lib/chudware-demo \
        XDG_RUNTIME_DIR=/run/user/$(id -u chudware-demo) podman "$@"); }
B=http://127.0.0.1:8300

sudo nft list table inet chudware_demo | head -30           # firewall loaded
sudo systemctl --machine=chudware-demo@.host --user status chudware-demo.service
curl -s $B/demo/health; echo                                  # {"ok":true,"active":0,"queued":0,"max":3}
curl -s $B/demo/status; echo                                  # "state":"none", ai_remaining_today
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' $B/healthz       # 401 (not admitted)

# Join, wait for admission (container start, < 60 s), use the app through the proxy.
C=$(curl -si -X POST $B/demo/join | sed -n 's/^[Ss]et-[Cc]ookie: \([^;]*\).*/\1/p')
until curl -s -H "Cookie: $C" $B/demo/status | grep -q '"state":"active"'; do sleep 2; done
curl -s -H "Cookie: $C" $B/demo/status; echo
curl -si -H "Cookie: $C" $B/healthz                          # 200 {"ok":true,"demo":true}
curl -sI -H "Cookie: $C" $B/app/ | grep -i 'cross-origin'    # COOP/COEP preserved
dp ps --format '{{.Names}} {{.Status}}'                       # one chudware-demo-<sid>
SID=$(dp ps --format '{{.Names}}' | head -1)

# Hardening as built.
dp inspect $SID --format '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}} {{.HostConfig.Memory}} {{.HostConfig.PidsLimit}}'
dp exec -i $SID /opt/chudware/venv/bin/python - <<'EOF'
import socket
def probe(addr, port):
    s = socket.socket(); s.settimeout(3)
    try: return s.connect_ex((addr, port)) == 0
    finally: s.close()
print("public 1.1.1.1:443        ", probe("1.1.1.1", 443), "(expect True)")
print("LAN router 192.168.1.1:80 ", probe("192.168.1.1", 80), "(expect False)")
print("host guest-addr 169.254.1.2:8300", probe("169.254.1.2", 8300), "(expect False)")
try: print("host.containers.internal:8300", probe(socket.gethostbyname("host.containers.internal"), 8300), "(expect False)")
except OSError: print("host.containers.internal does not resolve (fine)")
c = socket.socket(socket.AF_UNIX); c.connect("/run/chudware-demo/provider.sock"); print("provider.sock connect: ok")
EOF
dp exec $SID /opt/chudware/venv/bin/python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:18081/v1/models').read()[:200])"

# AI lane from the host side: allowlist and error shapes (socket dir is 0711; the socket is 0666).
SOCK=/run/user/$(id -u chudware-demo)/chudware-demo/${SID#chudware-demo-}/run/provider.sock
sudo curl -s --unix-socket $SOCK http://x/v1/models | head -c 300; echo
sudo curl -s --unix-socket $SOCK -d '{"model":"gpt-4o","messages":[]}' http://x/v1/chat/completions   # 403 model_not_allowed

# Expiry: temporarily set CHUDWARE_DEMO_HARD_SECONDS=120, WARN_SECONDS=60, IDLE_SECONDS=60 in
# /etc/chudware-demo/demo.env and restart. Join; status shows "warn":true for the last minute,
# then "expired" (note idle/hard_limit); `dp ps` is empty and /run/user/<uid>/chudware-demo/ has no <sid> dir.
# Queue: set CHUDWARE_DEMO_MAX_ACTIVE=1, join from two "clients" (loopback peers may set the header):
curl -si -X POST -H 'CF-Connecting-IP: 198.51.100.1' $B/demo/join >/dev/null
curl -s  -X POST -H 'CF-Connecting-IP: 198.51.100.2' $B/demo/join   # state queued, position 1, eta_seconds
# Adoption: with a session active, `sudo systemctl --machine=chudware-demo@.host --user kill -s KILL chudware-demo.service`
# (restarts itself): the same cookie is still "active" and `dp ps` shows the same container.
# Podman/passt flags: dp run --rm --network pasta docker.io/library/alpine true   (pasta present)
```

## Contract with the container image

The image side (not part of this package) must provide, and this package relies on:
`GET /healthz` → `{"ok": true, "demo": true}` when `CHUDWARE_WEB_DEMO=1` (a probe without
`demo` is accepted with a warning, i.e. an old image); the provider lane at
`http://127.0.0.1:18081/v1` forwarding to `/run/chudware-demo/provider.sock`; and the app
shell polling `GET /demo/status` (same origin, ≈ every 15 s) only when `/healthz` says
`demo: true`.

## Known limits

* The session gets full CAD for 45 minutes; the front door cannot judge *what* a visitor
  runs. Containment is the container/bwrap/user/firewall stack above, not content filtering.
* The free-tier OpenRouter allowance (≈ 50 requests/day account-wide) is the real AI
  ceiling; the demo says so on the waiting page and degrades to "modeling still works".
* Idle detection counts client-to-server traffic; if the app sends periodic background
  requests, a session whose tab is merely left open only ends at the 45-minute hard limit.
* Capacity guard and `--memory` are *limits*, not reservations: three sessions at their
  3.5 GiB ceiling plus the other service on the host can still exceed 15 GiB.
