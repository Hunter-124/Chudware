> Superseded 2026-08-10: this survey predates the gateway and its "what is
> missing" list is no longer the gap list. The streamed-session product it
> scopes (per-user podman orchestration, GUI pixel streaming) was removed; the
> gateway now serves only the WASM `/app` client and the `/engine` bridge. The
> port/transport/secret inventory below is still a useful map of the surfaces.

# Research: Web / Network / Auth Surfaces (Chudware → multi-user web)

Scope: Webservice/, .env (keys only), config.example.json, services/ipc/, services/browser/, services/agent/ (server/event surface only), services/mcp/ (transport/server only), docs/design/REDACTION_DESIGN.md, docs/plans/AI_HARNESS_ROADMAP.md, opencode.json, .mcp.json, README webservice mentions, git posture of Webservice/Cloudflare-info.txt.

Read-only. Secret **values redacted** — only key names reported.

---

## 1) What servers/ports does the stack already open?

| Surface | Transport | Bind / address | Evidence |
|---|---|---|---|
| **MCP server** | **stdio only** (JSON-RPC over stdin/stdout) | No TCP/HTTP port | `services/mcp/server.py:15` “Runs over stdio”; main uses `mcp.server.stdio.stdio_server` / `mcp.run()` at `server.py:682-700` |
| **chudware-hostd** (engine daemon) | NDJSON over **AF_UNIX** (POSIX) / named pipe (Win) | `$XDG_RUNTIME_DIR/chudware/hostd-<key>.sock` (or `/run/user/<uid>/chudware/…` or `/tmp/chudware-<uid>/…`) | `services/ipc/ipc_transport.h:8-11`; `services/ipc/ipc_discovery.cpp:47-89` (`runtimeDir`, `socketPath`); `services/mcp/daemon_bridge.py:168-200`; `core/hostd/main.cpp:206-214` `listener.listen(sock)` |
| **CEF daemon** (`--cef-daemon`) | NDJSON over **AF_UNIX** | `$XDG_RUNTIME_DIR/chudware/cefd-<key>.sock` or `/tmp/…` | `services/browser/cef_client.py:1-17,69-78,81-91` |
| **OAuth CallbackServer** (LLM subscription login) | **localhost HTTP** (stdlib `http.server.HTTPServer`) | **Fixed ports:** Codex `1455`, Anthropic `54545`; path `/auth/callback` or `/callback` | `services/agent/providers/sub_auth.py:47-143`; `codex_sub_provider.py:20-22`; `anthropic_sub_provider.py:26-28` |
| **llama.cpp / local OpenAI-compat** | Outbound client + optional **child HTTP server** | Default example `http://127.0.0.1:1234/v1`; installer can spawn a local server | `services/installer/llamacpp.py`; `services/agent/providers/llamacpp_provider.py` |
| **Chrome DevTools (login only)** | Ephemeral `http://127.0.0.1:<free_port>/json` | OS-assigned free port during native Chrome login | `services/browser/session.py:210-224` (`_free_port`, `_devtools_tabs`) |
| **Discord notify** | Outbound HTTPS client only | No listen | `work/tools/notify_discord.py:52` `API = "https://discord.com/api/v10"` |
| **result_serving** | In-process payload compaction | **Not a network server** | `services/mcp/result_serving.py:1-10,326+` |
| **agent_events** | In-process live sink + `events.jsonl` journal | **No network transport** | `services/agent/agent_events.py:16-18` “No new transport is introduced”; journal under cache runs dir |

**Summary:** Production stack is **local IPC + stdio**. The only intentional localhost TCP listeners are **short-lived OAuth callbacks** and **optional local model servers**. There is **no multi-user HTTP/WebSocket gateway** today.

IPC framing (reuse-worthy pattern, not a web server): newline-delimited JSON-RPC `{"id", "method", "params"}` / `{"id", "result"|"error"}` + id-less `{"event", "data"}` pushes (`ipc_transport.h:3-6`). Same-user socket dir mode `0700`, socket `0600` (`ipc_discovery.cpp:68-73`; transport chmod 0600).

---

## 2) services/browser/ — what is it? Any HTTP serving?

**What it is:** User-authenticated **headless browser acquisition** for parts vendors — **not** a web UI host.

- Primary headless path: **host CEF daemon** (`Chudware --cef-daemon`) driven by `cef_client.py` over AF_UNIX JSON-RPC (`services/browser/README.md:1-12`; `cef_client.py:1-17`).
- Residual: **Playwright / patchright / camoufox** factory in `driver.py` for headed login, embed, cookie export (`driver.py:1-27`; env `CHUDWARE_PW_BACKEND`).
- `session.py`: `browser_login` (headed), `browser_fetch` / `browser_download` (headless CEF) (`session.py:1-34`).
- `embed.py`: **in-process** Chromium on a private asyncio thread; streams **JPEG frames via CDP screencast** to ImGui panels over engine worker RPCs — “plain request/response seam”, **not HTTP to a browser client** (`embed.py:1-20`).
- Profiles: local only under app data (`README.md:34-38`); never synced.
- Challenges: detect, never solve (`README.md:30-32`).

**HTTP serving?** **No application HTTP server.** Transient reads of Chrome’s DevTools `/json` on 127.0.0.1 during native login only (`session.py:219-224`). No FastAPI/static site/WebRTC/VNC surface in this tree.

---

## 3) Existing auth / token / session / user-identity in services/?

| Kind | Purpose | Multi-user web identity? |
|---|---|---|
| **LLM subscription OAuth** (codex/claude/cursor/xai) | PKCE + `CallbackServer` + tokens in `config.json` → `subscription_auth` | **No** — single-operator AI provider login |
| **API keys** (`api_key`, OPENROUTER_*, etc.) | Cloud model providers via `config.get_provider` | **No** |
| **Browser site sessions** | Cookie jars in local profiles for McMaster/DigiKey/… | **No** — vendor shopping logins |
| **Redaction / compliance_mode** | ITAR scrub of outbound prompts (`redaction.py`, `docs/design/REDACTION_DESIGN.md`) | Explicitly **out of scope:** “multi-user shared sessions” (`docs/design/REDACTION_DESIGN.md:20-22`) |
| **hostd trust model** | Unix socket same-user DACL; “no additional authentication” | **No** — same OS user (`core/hostd/child_supervisor.h:30-33`) |
| **MCP tool allow-lists** | Per-context tool policy (`server.py` tool_registry) | Capability gating, not users |

Key files:
- `services/agent/providers/sub_auth.py:1-7,47-48,146-149` — PKCE, callback server, credential store.
- `services/agent/providers/subscription_login.py:1-17,67-85` — `login/logout/status/accounts/switch` for LLM vendors only.
- Ports: `codex_sub_provider.py:20-22` (1455), `anthropic_sub_provider.py:26-28` (54545); xAI/cursor device-code (no callback server).
- `services/agent/redaction.py:1-23` — compliance wrap, not Entra/Google.

**No Microsoft Entra, Google OAuth for app users, session cookies for CAD users, JWT gateway, or RBAC** appears under `services/`.

---

## 4) Cloudflare credentials file — git posture (SECURITY)

**File:** `Webservice/Cloudflare-info.txt`  
**Contents (labels only — values MUST NOT be logged/committed):** Account ID, API Token, Access Key ID, Secret Access Key, S3 API endpoint (R2).

**Gitignore:** Root `.gitignore` has `# secrets` → `.env` and `config.json` (`.gitignore:8-9,34-35`) but **no** `Webservice/`, `Cloudflare*`, or `*.txt` secret pattern. Repo-wide content search for `Webservice|Cloudflare-info` as tracked path names did not show an ignore rule.

**Assessment:**
- File is **present on disk** and **not ignored**.
- Without a shell `git status`/`ls-files` in this read-only scout, treat as **untracked-or-trackable risk**: any `git add Webservice` or `git add -A` will stage live secrets.
- **FLAG LOUDLY:** If this file was ever committed/pushed, **rotate all Cloudflare API tokens and R2 keys immediately**. Do not print values in tickets/logs.

**Required fix (for implementer):**
```
# .gitignore
Webservice/**
!Webservice/README.md
!Webservice/*.example
```
or at minimum `Webservice/Cloudflare-info.txt` + move secrets to env/`config.json` (already gitignored pattern).

---

## 5) Python web frameworks in requirements / stack

From `requirements.txt`:

| Present | Role |
|---|---|
| `httpx==0.28.1` | HTTP **client** (providers) |
| `mcp==1.27.2` | MCP SDK (FastMCP **stdio**) |
| `anyio>=4.0.0` | Async I/O for MCP stdio |
| `playwright==1.61.0` | Browser automation |
| `dulwich`, `pydantic`, … | PLM / schemas |

**Absent:** FastAPI, Flask, Starlette (as direct app), aiohttp, websockets, uvicorn, django, tornado, sanic.

Note: FastMCP is imported (`from mcp.server.fastmcp import FastMCP` — `server.py:25`) but configured for **stdio**, not HTTP SSE transport.

Env examples (keys only):
- Root `.env` (gitignored): `NEXAR_CLIENT_ID`, `NEXAR_CLIENT_SECRET`, `JLCPCB_ACCESS_KEY`, `JLCPCB_SECRET_KEY`.
- `tools/flywheel/.env.mlflow.example`: `MLFLOW_TRACKING_URI`.
- `tools/flywheel/.env.argilla.example`: `ARGILLA_API_KEY`, `ARGILLA_AUTH_SECRET_KEY` (optional `ARGILLA_API_URL`).

`config.example.json` keys: `provider`, `model`, `api_key`, `reasoning_effort`, `max_output_tokens`, `context_window`, `openai.base_url`, `llamacpp.*`, `telemetry_consent`, `experimental.*` — **no user OAuth block**.

---

## 6) Chat precedent for a user shoutbox

| Precedent | Reusable? | Evidence |
|---|---|---|
| **Org channel messaging** | **Yes as data model** | `services/agent/org.py:67-68` `MSG_KINDS`; `OrgState.channel_send` `282-333` — typed messages (`info/question/decision/warning/interface_change`), 120-word cap, ack, ledger artifacts. Agent-dept channels, not human shoutbox. |
| **agent_events + EventJournal** | **Yes as event schema/persistence pattern** | `agent_events.py` builders + `events.jsonl` fan-out; live GUI sink. In-process, not multi-user WS. |
| **`.worksync/` / teamsync** | **Partial / incomplete** | `.gitignore:60-64` documents `work/tools/teamsync.py` + `.worksync/` local mirror for team messages; **teamsync.py and team_mcp.py not present** in tree (glob empty). `.mcp.json` still references `work/tools/team_mcp.py` (gitignored local config). |
| **Discord notify** | Outbound ops alerts only | `work/tools/notify_discord.py` — bot token in gitignored `work/sync/`, polls “inbox” via missing teamsync. |
| **loop Session.messages** | No | LLM chat history, not multi-user. |

**README / roadmap:** README parks “SaaS/cloud hosting” (`README.md` “Currently parked”); no Webservice architecture. `docs/plans/AI_HARNESS_ROADMAP.md` is agent harness (WS0–WS5), not web multi-user. `docs/design/REDACTION_DESIGN.md` multi-user shared sessions OOS.

`opencode.json` / `.mcp.json`: local stdio MCP client configs with absolute paths; **gitignored** (`.gitignore:42-47`).

---

## RECOMMENDATION

### Winner: **Build the multi-user gateway fresh**; reuse **patterns and local IPC**, not the desktop transports as-is.

**Reuse (patterns / libraries):**
1. **Event model** — `aicad.agent_events/2` + JSONL journal shape for session telemetry into the browser UI; map live sinks to WebSocket fan-out instead of ImGui.
2. **Org `channel_send` schema** — typed messages + ack + persistence as the shoutbox core (new store: sqlite/JSONL under shared data dir, not in-process OrgState alone).
3. **OAuth plumbing ideas** from `sub_auth.CallbackServer` / PKCE — **patterns only**; implement Entra + Google with a real ASGI stack and proper redirect URLs (not fixed 1455/54545 LLM ports).
4. **IPC framing / hostd** — keep **one hostd or per-session engine** behind AF_UNIX inside the container; gateway is HTTP/WS front, not a rewrite of `ipc_transport`.
5. **httpx + anyio** already in requirements for outbound calls.

**Build fresh:**
1. **ASGI gateway** (recommend **FastAPI or Starlette + uvicorn** + **websockets** — none present today) for OAuth session cookies/JWT, reverse-proxy/stream of per-user app sessions, shoutbox WS, health.
2. **Per-user container orchestration** (podman) — no existing multi-tenant session manager.
3. **Streaming the GUI to browser** — no VNC/WebRTC/HTTP frame server in-repo (browser embed is CDP→ImGui only); choose streaming stack outside current code.
4. **Entra (UA) + Google whitelist** identity — no app-user IdP code exists.
5. **Shared PLM mount** — coordinate with PLM scout; gateway should not reimplement vault, only authz + path mapping.

**Do not reuse as the public web surface:**
- MCP stdio FastMCP (agent tool bus, not user HTTP).
- CEF/Playwright embed (vendor automation).
- hostd unix socket exposed to network (same-user trust only).

### Ranking alternatives

| Option | Verdict | Why (≤3) |
|---|---|---|
| **A. Fresh ASGI gateway + container sessions (WINNER)** | **Do this** | No HTTP framework/server exists; stdio/AF_UNIX cannot serve browsers; clean security boundary |
| **B. Expose FastMCP HTTP/SSE transport** | Reject as primary UX | MCP is agent tool protocol; no GUI stream; wrong auth model |
| **C. Tunnel hostd IPC to browser** | Reject | Socket is same-user local; no frames/input path; catastrophic if remote |

### Required secret / .gitignore fixes (do first)
1. **Add** `Webservice/` secret globs (or whole dir except examples) to `.gitignore`.
2. **Ensure** `Webservice/Cloudflare-info.txt` never committed; if history has it, **rotate** CF API token + R2 keys + scrub history.
3. Prefer env vars / root `config.json` (already ignored) for R2: e.g. `CF_ACCOUNT_ID`, `CF_API_TOKEN`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_ENDPOINT` — document in `Webservice/config.example.env` only.
4. Keep ignoring `.env`, `config.json`, `.mcp.json`, `opencode.json`, `.worksync/`.
5. Do not commit live tokens from root `.env` (already ignored — good).

---

## OPEN QUESTIONS

1. Will each user container run full **Chudware+GLFW+GL** (needs virtual GL/X as HeadlessScout noted) or a future headless/stream-native path?
2. Shared hostd vs **one engine daemon per session** under cgroup GPU sharing (3–6 users, one 3080 Ti)?
3. Shoutbox storage: extend PLM/git-vault, separate sqlite, or redis — and retention/moderation?
4. Cloudflare role: **Tunnel only**, R2 for assets, or full edge auth — credentials file implies R2 ready; tunnel binary not installed yet on host.
5. Is `work/tools/teamsync.py` / `team_mcp.py` deleted intentionally (refs remain in `.gitignore` / `.mcp.json`)?

---

## Config / env key inventory (names only)

**`.env`:** `NEXAR_CLIENT_ID`, `NEXAR_CLIENT_SECRET`, `JLCPCB_ACCESS_KEY`, `JLCPCB_SECRET_KEY`  
**`Webservice/Cloudflare-info.txt` labels:** Account ID, API Token, Access Key ID, Secret Access Key, S3 API endpoint  
**`config.example.json`:** provider, model, api_key, reasoning_effort, max_output_tokens, context_window, openai.base_url, llamacpp.{base_url,model_path,auto_start,ctx_size,gpu_layers,vision}, telemetry_consent, experimental.*  
**Browser env:** `CHUDWARE_PW_BACKEND`, `CHUDWARE_BIN`, `CHUDWARE_CEF_SOCKET`, `CHUDWARE_CEF_KEY`  
**Hostd env:** `CHUDWARE_HOSTD_SOCKET`, `CHUDWARE_HOSTD_BIN`, `CHUDWARE_INSTANCE_KEY`, `XDG_RUNTIME_DIR`
