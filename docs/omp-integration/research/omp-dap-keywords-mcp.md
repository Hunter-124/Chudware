# OMP DAP / LSP / Slash-Commands / MCP-Client / Approval — Reuse Map for Firmware Workstream

Scope: `scratch/omp-src/packages/{coding-agent,agent,ai}` (pinned 6ae7cdbf) +
`omp-inspection/oh-my-pi/packages/mnemopi` (MCP server authoring example — no
standalone `mcp_server` top-level dir exists in the inspection clone; the
canonical example server lives at `packages/mnemopi/src/mcp-server.ts`).

## 1. DAP / Debugger integration — **PORT** (S-M effort, highest-value reuse)

OMP ships a full, hardened DAP client/session/tool stack under
`packages/coding-agent/src/dap/`:

- `dap/client.ts` — raw DAP wire client (`DapClient`). Spawns the adapter
  process via `ptree.spawn` (client.ts:153), speaks Content-Length-framed
  JSON-RPC-like DAP messages (shared framing module,
  `jsonrpc/message-framing.ts:1-5`, reused by both LSP and DAP). Supports two
  transport shapes:
  - **stdio** — adapter's stdin/stdout are the DAP stream.
  - **socket mode** — for adapters that listen on a socket instead (e.g.
    `dlv`); unix domain socket on Linux/macOS, TCP w/ client-addr dial
    elsewhere (`client.ts:151-153`, `connectSocket` `client.ts:682`,
    CHANGELOG `*5812`: "Socket-mode DAP adapter support ... Linux uses unix
    domain sockets, macOS/other platforms use TCP").
  - Write timeouts + guaranteed process teardown on connection failure
    (`client.ts:426-441`), chunk-deque buffering to avoid O(n²) growth
    (CHANGELOG `*2731`), header-resync so junk before `Content-Length` can't
    wedge the reader forever (`client.ts:504-524`).
  - Reverse-request support: adapter-initiated `runInTerminal` /
    `startDebugging` via `onReverseRequest()` (CHANGELOG `*5803-5805`).
- `dap/config.ts` — adapter registry + config merge. Built-ins in
  `dap/defaults.json`: `gdb` (`-i dap`, langs `c/cpp/rust`, defaults.json:3-6),
  `lldb-dap` (defaults.json:17-19), `debugpy`, `dlv` (`-i`... socket connect,
  defaults.json:62-64), `js-debug-adapter`, `kotlin-debug-adapter`,
  `php-debug-adapter`, `bash-debug-adapter`, `dart`/`flutter-debug-adapter`.
  User/project override via `dap.json`/`.dap.json`/`dap.yaml`/`.dap.yaml`/
  `dap.yml` discovered per-cwd (`config.ts:125`, `getConfigSources`), merged
  over defaults (`config.ts:82-113`), with relative-command resolution
  against the debug cwd (tested `dap-config.test.ts:170-192`). Plugins can
  ship adapters too — marketplace installer embeds `dapAdapters` from a
  plugin manifest into `.dap.json`/`.dap.yaml`
  (`extensibility/plugins/marketplace/manager.ts:396-406`).
  Extensionless-binary heuristics prefer `gdb`/`lldb-dap` for files with no
  extension (`config.ts:12` `EXTENSIONLESS_DEBUGGER_ORDER`), and adapters
  advertising `acceptsDirectoryProgram` are preferred for directory-shaped
  launch targets (CHANGELOG `*3145`) — directly relevant to firmware ELF/ihex
  targets that often lack conventional extensions.
- `dap/session.ts` — `DapSessionManager`: launch/attach handshake
  (`session.ts:147-149,300-303` — fires `launch`, completes `configurationDone`
  handshake, then awaits the launch response per spec quirk that many
  adapters don't respond to `launch` until after `configurationDone`),
  breakpoint mutation serialized per-session (CHANGELOG `*2788`), idle-session
  cleanup (`session.ts:1031-1033`).
- `tools/debug.ts` — the agent-facing `debug` tool (`DebugTool`, `tools/debug.ts:690-693`,
  summary "Debug a running process with DAP (debugger adapter protocol)").
  Action surface (schema `tools/debug.ts:39-140`): `launch`/`attach`,
  `continue`/`step_in`/`step_out`/`step_over`/`pause`, breakpoint CRUD
  (source + **function** + **instruction** + **data** breakpoints, with
  `hit_condition`, CHANGELOG `*5810`), `evaluate` (defaults to top stopped
  frame, CHANGELOG under 5.9x notes), `stack`/`threads`/`scopes`/`variables`,
  `modules`/`loaded_sources` (module & source introspection,
  CHANGELOG `*5802`), memory read/write actions
  (`access_type: read|write|readWrite`, `debug.ts:137`), and
  `custom_request` — passthrough for **arbitrary DAP commands**
  (`debug.ts:138`, CHANGELOG `*5803`) — this is the escape hatch for
  vendor-specific requests (e.g. OpenOCD/probe-rs custom DAP extensions like
  `rtos`, `readMemory` beyond spec, SVD-register views).
  Read-only vs exec-tier action split for approval purposes
  (`tools/debug.ts:62-65`). Adapter-unavailable messaging is table-driven per
  adapter (`debug.ts:503-525`).
- `debug.enabled` setting gates the tool (`settings-schema.ts:3591-3593`).

**Firmware reuse plan**: PORT `dap/` (client+session+config+tool) essentially
unmodified — it is transport-agnostic DAP, and the `custom_request` escape
hatch already covers non-standard adapter extensions. New work is entirely
additive at the *config* layer: add `dap.json` entries for
`cortex-debug`/`probe-rs`'s `probe-rs dap-server` (stdio), OpenOCD-fronted
`arm-none-eabi-gdb -i dap` (reuses the existing `gdb` adapter unmodified —
just point `command` at the cross toolchain gdb and pass `-x` init scripts via
`launchDefaults.args`), and `avr-gdb`. Socket-mode transport already exists
for adapters like `dlv` that listen rather than speak stdio — same shape
OpenOCD's gdbserver needs (TCP, not stdio) if a raw-OpenOCD (non-DAP) bridge
is ever wanted instead of going through a real DAP adapter. No engine fork
needed: this is the single most reusable subsystem in the whole survey.

## 2. LSP integration — **PORT** (S, already firmware-ready for clangd)

- `lsp/config.ts` — same discovery/merge shape as DAP: `LspConfig` w/
  `servers: Record<string, ServerConfig>` + `idleTimeoutMs`
  (`config.ts:13-17`), default table `lsp/defaults.json` merged with project
  `dap`-sibling config files, `$PID` token substitution (`config.ts:23`).
  **`clangd` is already a built-in default** (`lsp/defaults.json:27-31`):
  `command: clangd`, args `--background-index --clang-tidy
  --header-insertion=iwyu`, fileTypes `.c .cpp .cc .cxx .h .hpp .hxx .m .mm`,
  rootMarkers `compile_commands.json CMakeLists.txt .clangd .clang-format
  Makefile` — exactly what an embedded C/C++ CMake/Make project needs, no
  changes required.
- `lsp/client.ts` + `jsonrpc/message-framing.ts` — same Content-Length framing
  as DAP (shared module), so both protocols benefit from the same framing
  hardening (header-resync, per-message isolation so one bad message doesn't
  kill the reader loop — CHANGELOG `*2784`).
- Published clients for specific ecosystems exist under `lsp/clients/`
  (`BiomeClient`, `SwiftLintClient` — `lsp/config.ts:8-9`) as a pattern for
  adding a dedicated wrapper client if a firmware toolchain needs bespoke
  handling beyond generic LSP (e.g. a `compile_commands.json` generator
  shim for non-CMake embedded build systems).
- `lsp` tool (companion to `debug`) exposes diagnostics/reload/format/code
  actions/workspace-symbols to the agent, with `*` wildcard for
  workspace-wide/all-servers operations (CHANGELOG `*5811`, `5903`).

**Firmware reuse plan**: PORT wholesale. `clangd` config is already correct;
the only firmware-specific work is ensuring `compile_commands.json` gets
generated for the embedded build (CMake `CMAKE_EXPORT_COMPILE_COMMANDS=ON` or
`bear`/`compiledb` wrapping a Makefile build) — that's a build-integration
task outside OMP's scope, not an LSP-layer change.

## 3. "Magic keywords" — slash-command / trigger / macro system — **PORT (builtin registry) + reuse pattern (user-defined)**

Two parallel but unified systems, both dispatched through one spec shape:

- **Builtin slash commands**: `slash-commands/types.ts` defines
  `SlashCommandSpec extends BuiltinSlashCommand` (`types.ts:15-27,89-136`):
  `name`, `aliases`, `description`, `allowArgs`, `subcommands:
  SubcommandDef[]` (`types.ts:7-12`, dropdown-completable, e.g. `/mcp add`,
  `/mcp list`), `inlineHint`, and two handler slots — `handle` (shared
  TUI/ACP text handler) and `handleTui` (TUI-only, supersedes `handle`, for
  anything needing `InteractiveModeContext` — selectors/wizards/dashboards).
  Handler result is `SlashCommandResult = undefined | {consumed:true} |
  {prompt:string}` (`types.ts:48`) — the `{prompt}` variant is the "keyword
  rewrites the user's message" path (e.g. `/force <tool> <prompt>`,
  `types.ts:46`). Registry array `BUILTIN_SLASH_COMMAND_REGISTRY` in
  `slash-commands/builtin-registry.ts:206-2624` — one array, ~2600 lines,
  dozens of commands (`/settings`, `/plan`, `/goal`, `/vibe`, `/mcp`, `/dap`
  implicitly via `/debug`? — not present; debugger is tool-invoked not
  slash-invoked) each a plain object literal.
  Parsing: `helpers/parse.ts` → `parseSlashCommand`, `parseSubcommand`
  (imported `builtin-registry.ts:52`).
- **User-defined custom commands**: `extensibility/custom-commands/` —
  `types.ts:21-119` defines `CustomCommandAPI` (cwd + injected deps),
  `CustomCommandFactory = (api) => CustomCommand | CustomCommand[] |
  Promise<...>` (`types.ts:96-98`), `CustomCommand.execute(args, ctx) =>
  string|undefined` (returns a prompt to send, same shape as builtin
  `{prompt}`). `CustomCommandSource = "bundled"|"user"|"project"`
  (`types.ts:101`). `loader.ts:33-38` dynamically `import()`s a command
  module and calls its default export as the factory — arbitrary TS/JS, not
  a restricted DSL. `discoverCustomCommands` (`loader.ts:83-98`) walks
  `agentDir/commands` (user) plus project dirs, dedupes by resolved path.
  Bundled examples: `extensibility/custom-commands/bundled/{ci-green,review}`
  — full `CustomCommand` classes with `name`, `description`, `execute()`.
  Also: **frontmatter-driven markdown commands** exist separately
  (`slash-commands.ts:40-47`, `parseFrontmatter`/YAML frontmatter + body,
  `legacy-pi-coding-agent-shim.ts:367-379`) — the lighter-weight "drop a
  `.md` file with `description:` frontmatter" flavor, same pattern used by
  Skills (`extensibility/skills.ts:102-108`).
  MCP **prompts** are unified into the same command surface too
  (`extensions/get-commands-handler.ts:8-9`: "prompt commands loaded as
  `LoadedCustomCommand` — user/project/bundled custom commands **and MCP
  prompts**").

**Firmware reuse plan**: PORT the `SlashCommandSpec` registry mechanism and
the `CustomCommand`/`CustomCommandFactory` extensibility layer as-is — they
are domain-agnostic dispatch/registration plumbing. New firmware-specific
"magic keywords" (e.g. `/flash`, `/reset-target`, `/openocd`, `/probe list`)
are just new entries appended to a registry array or new bundled
`CustomCommand`s under `bundled/` — zero framework changes, S effort per
command.

## 4. External MCP client — **PORT** (M effort; config/discovery adds firmware servers, code unchanged)

`packages/coding-agent/src/mcp/`:
- `mcp/config.ts` — multi-source config loader via a generic "capability"
  system (`loadCapability(mcpCapability.id, {cwd})`, `config.ts:101`), legacy
  `MCPServerConfig` union normalized from canonical `MCPServer`
  (`convertToLegacyConfig`, `config.ts:38-86`): `stdio` (`command`,`args`,
  `env`,`cwd`), `http` (`url`,`headers`), `sse` (`url`,`headers`) — transport
  literally selected by which fields are present (`config.ts:40`). Per-server
  `enabled`/`timeout`/`auth`/`oauth`. User-level allow/deny lists layered over
  source config (`config.ts:108-124`, denylist always wins). Built-in
  filters strip the native Exa integration and (optionally) browser-automation
  MCP servers so as not to double up with builtin tools
  (`config.ts:128-139,225-249,286-304`) — a good pattern for a firmware agent
  to reserve/blacklist server names that collide with a native `debug`/`lsp`
  tool.
- `mcp/transports/{stdio,http,sse}.ts` — one transport module per wire
  protocol (`transports/index.ts` re-exports); `stdio.ts` spawns the server
  process, `http.ts`/`sse.ts` are streamable-HTTP / legacy-SSE clients per
  MCP spec (`docs/mcp-protocol-transports.md` referenced in inspection repo
  file list).
- `mcp/manager.ts` — `MCPManager` (singleton, `manager.ts:168-184`): connect
  lifecycle (`connectToServer`/`disconnectServer` from `client.ts`), tool
  discovery+caching (`listTools`, `MCPToolCache`), resource
  subscribe/unsubscribe with epoch-guarded rollback on late/stale
  subscriptions (`manager.ts:250-306`, `resolveSubscriptionPostAction`),
  prompts (`getPrompt`/`listPrompts` — feeds slash-command unification above),
  **reconnect-storm circuit breaker** (`manager.ts:73-94`: sliding 30s
  window, burst limit 5, guards against a misbehaving stdio server
  fork-bombing the agent — cites real incident issue #1592). OAuth-aware
  transports (`AuthRefreshableMCPTransport`, `manager.ts:59-65`, plus
  `oauth-credentials.ts`/`oauth-flow.ts`) for third-party servers requiring
  auth. Deterministic tool ordering for prompt-cache stability
  (`sortMCPToolsByName`, `manager.ts:125-128` — byte-identical tool array
  across reconnects avoids invalidating Anthropic prompt-cache breakpoints).
- `mcp/tool-bridge.ts` — `MCPTool`/`DeferredMCPTool` wrap a remote MCP tool
  as a `CustomTool` the agent's tool-execution loop can call uniformly
  alongside builtins.
- Config surfaces: project/user `mcp.json` (+ plugin-embedded) discovered via
  the same capability system as DAP/LSP configs (`getMCPConfigPath`,
  `mcp/config.ts:7`); `/mcp add`/`/mcp list`/`/mcp reconnect` slash
  subcommands drive it interactively (`slash-commands/helpers/mcp.ts`,
  `modes/controllers/mcp-command-controller.ts`).
- **Authoring a server** (for glue work OMP itself needs, e.g. an OpenOCD/
  probe-rs bridge you write and expose over MCP): `mnemopi/src/mcp-server.ts`
  is a complete, minimal, dependency-light example (`mcp-server.ts:1-66`):
  JSON-RPC 2.0 envelope helpers (`ok`/`err`), `listToolsJson()`,
  `callToolJson(name, args)` dispatching to `handleToolCall` from
  `mcp-tools.ts`, error results wrapped as `{content:[...], isError:true}`
  rather than thrown — the shape any hand-rolled firmware MCP server (e.g.
  wrapping `probe-rs`/`pyocd`/`avrdude`/`openocd` CLI/telnet interfaces)
  should mimic to interoperate with OMP's client.

**Firmware reuse plan**: PORT the client stack unmodified. New work is
(a) writing small stdio MCP servers ("glue") around
`probe-rs`/`pyocd`/`avrdude`/OpenOCD's Tcl/telnet RPC port where a raw DAP
adapter doesn't already exist, using the mnemopi server as the template, and
(b) config entries in `mcp.json` pointing at them. No transport/manager code
changes required — stdio transport already supports arbitrary long-running
subprocess servers.

## 5. Tool execution + approval/permission model — **PORT** (S; tier system maps directly)

`tools/approval.ts` (full file read, 190 lines):
- Each tool declares an `approval` — either a static `ToolApprovalDecision`
  or a `(args) => ToolApprovalDecision` function (`approval.ts:74`) — mapping
  to a `ToolTier`: `read < write < exec` (`approval.ts:26-32`, rank-ordered).
- Three approval modes gate max auto-approved tier: `always-ask`→`read`,
  `write`→`write`, `yolo`→`exec` (`APPROVAL_MODE_MAX_TIER`, `approval.ts:34-38`).
- Resolution order (`resolveApproval`, `approval.ts:93-132`): (1) tool's own
  per-call decision, defaulting to tier `exec` if unspecified — safe-by-default;
  (2) user's per-tool config override `tools.approval.<name>: allow|deny|prompt`
  (`approval.ts:100`, denylist throws with a clear remediation message,
  `approval.ts:148-153`); (3) mode-vs-tier comparison. A tool can force
  `override: true` (`approval.ts:106-115`) to always prompt regardless of
  mode (short of `yolo`+explicit user allow) — used for genuinely dangerous
  actions.
- `truncateForPrompt` (`approval.ts:159-163`) bounds what's shown in an
  approval dialog.
- The `debug` tool already models this: read-only DAP actions (stack,
  variables, evaluate-read) vs exec-tier (launch, continue, breakpoint
  mutation, memory write) are pre-classified (`tools/debug.ts:62-65`) — this
  tier split is exactly the shape needed to auto-approve "read register/
  memory" while prompting for "flash / reset / write memory" on real
  hardware, which matters more for firmware than software debugging (an
  errant flash-erase or wrong-target write is destructive in a way stepping
  through Python isn't).
- ACP bridge (`index.ts:283-284`, `getClientBridge`) routes fs/terminal/
  permission requests to a connected editor host when running under ACP
  instead of the TUI — relevant if the firmware agent embeds in Billet via
  ACP rather than owning its own TUI.

**Firmware reuse plan**: PORT unmodified. Extend tier classification
conventions to programmer/flash tools (`flash tool`: tier `exec`,
`override:true` by default, since erasing/reflashing hardware is
irreversible) and treat "connect to target" (attach) as tier `write` (safe to
default-approve in `write` mode) vs `launch`(reset+run)/breakpoint-write as
`exec`.

## Cross-cutting notes
- DAP, LSP, and (by extension) any hand-rolled hardware-adapter transport
  share one framing primitive (`jsonrpc/message-framing.ts`) — reuse it
  directly for any new stdio-framed protocol glue rather than reinventing
  Content-Length parsing.
- DAP/LSP/MCP all use the same discovery pattern: default JSON in-package +
  `name.json`/`.name.json`/`name.yaml` project/user override files merged
  via a shared "capability" loader (`discovery/loadCapability`) — a new
  `programmers.json` (avrdude/pyocd/probe-rs configs) or `flash.json` config
  surface for the firmware workstream should follow this exact convention
  rather than inventing a new one.
- None of DAP/LSP/slash-commands/MCP-client require SIDECAR or FORK — this
  entire surface is pure TypeScript logic with no engine/runtime dependency
  that would force a bun-sidecar boundary; it can be imported/vendored
  directly into the firmware agent package. SIDECAR only becomes relevant if
  CAD's host process wants to keep the whole OMP agent runtime (TUI/session
  loop) out-of-process from the C++ host — a decision orthogonal to this
  dossier's scope (already covered by the reuse-mechanism decision in the
  task brief).

## Effort/Recommendation summary
| Area | Recommendation | Effort | Key files |
|---|---|---|---|
| DAP client/session/tool | PORT | S–M | `dap/client.ts`, `dap/session.ts`, `dap/config.ts`, `tools/debug.ts` |
| DAP adapter configs (gdb/lldb-dap present; add probe-rs/OpenOCD/avr-gdb) | PORT + config GLUE | S | `dap/defaults.json`, project `dap.json` |
| LSP client + clangd default | PORT | S | `lsp/config.ts`, `lsp/defaults.json:27-31`, `lsp/client.ts` |
| Slash-command registry (builtin) | PORT | S | `slash-commands/types.ts`, `slash-commands/builtin-registry.ts` |
| User-defined custom commands | PORT | S | `extensibility/custom-commands/{types,loader}.ts`, `bundled/` |
| MCP client (config/transport/manager/bridge) | PORT | M | `mcp/{config,manager,client,tool-bridge}.ts`, `mcp/transports/*` |
| MCP server authoring (for firmware glue servers) | PORT pattern, then GLUE new servers | S per server | `mnemopi/src/mcp-server.ts` |
| Approval/permission tiering | PORT | S | `tools/approval.ts` |
