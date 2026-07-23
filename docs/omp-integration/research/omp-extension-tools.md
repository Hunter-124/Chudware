Tool + Extension + MCP Plug-in Model — OMP vs CAD
===================================================
Scope: how OMP declares/dispatches tools, its extension mechanism, its MCP client
model, and how a FORK would register CAD's domain tools (drawing/sim/ecad/cam/plm)
into OMP's registry, vs CAD's existing Python tool system.

---

## 1. OMP: tool declaration, registry, dispatch, approval

### 1.1 Tool shape (`AgentTool`)
Defined in `packages/ai` (`pi-agent-core` re-export) and consumed everywhere as
`Tool = AgentTool<any,any,any>`
(`scratch/omp-src/packages/coding-agent/src/tools/index.ts:102-103`).
Key fields (from `docs/approval-mode.md:70-73` + `docs/extensions.md:256-299`):
- `name`, `label`, `description`, `parameters` (Zod schema — `pi.zod` is zod/v4,
  legacy TypeBox shim via `pi.typebox`)
- `hidden`, `defaultInactive`, `deferrable`
- `approval?: ToolApprovalDecision | ((args) => ToolApprovalDecision)` where
  `ToolTier = "read" | "write" | "exec"`, decision can carry `reason`/`override`
- `execute(toolCallId, params, signal, onUpdate, ctx): Promise<AgentToolResult>`
  (streaming partial results via `onUpdate`)
- optional `onSession(event, ctx)`, `renderCall(...)`, `renderResult(...)` (TUI-only)
- `formatApprovalDetails?(args)` — used to build the approval prompt body.

Built-in tools live as one class/module per tool under
`packages/coding-agent/src/tools/*.ts` (bash.ts, read.ts, write.ts, grep.ts, glob.ts,
task.ts, browser.ts, eval.ts, ast-edit.ts, memory-*.ts, etc.) and are assembled by
`createTools`-style factories that read `ToolSession` (huge session-context interface,
`tools/index.ts:142-356`) for cwd, event bus, MCP manager, artifact manager, etc.
Each tool factory typically exposes `SomeTool.createIf(session)` (see `ask.md:40`)
to conditionally register itself (e.g. AskTool only when `session.hasUI`).

### 1.2 Registry / dispatch
- No single "ToolRegistry" class was found as such; the "registry" is the *active
  tool array* assembled per-session by `createTools(...)` in
  `packages/coding-agent/src/tools/index.ts`, plus custom tools
  (`.omp/tools/`, `.claude/tools/`, plugins — `ToolPathWithSource`,
  `tools/index.ts:178-182`) and MCP-bridged tools (`mcp__*`, see §3) merged in.
- `ToolSession.getToolByName`, `isToolActive`, `setActiveToolNames`
  (`tools/index.ts:216-221`) are the live lookup/activation surface used by the
  agent loop and by extensions (`pi.getActiveTools/getAllTools/setActiveTools`,
  `docs/extensions.md:120`).
- Dispatch: the agent loop calls `execute(toolCallId, params, signal, onUpdate, ctx)`
  directly on the resolved `AgentTool`. Every call is wrapped for extension
  interception: `tool_call` (pre-exec, may `{block:true}`), `tool_result`
  (post-exec, middleware-style, may patch output), plus observability events
  `tool_execution_start/update/end` and `tool_approval_requested/resolved`
  (`docs/extensions.md:226-233`, `docs/hooks.md:138-141` — `HookToolWrapper.execute()`
  emits `tool_call` before execution).

### 1.3 Approval / permission model
`packages/coding-agent/src/tools/approval.ts` (full file read):
- Resolution order (`resolveApproval`, lines 93-132): tool's own
  `approval(args)` decision (default tier `"exec"`) → user per-tool config
  override (`tools.approval.<name>: allow|deny|prompt`) → active `ApprovalMode`
  tier comparison.
- `ApprovalMode` = `"always-ask" | "write" | "yolo"`, each capped to a max
  `ToolTier` (`APPROVAL_MODE_MAX_TIER`, lines 34-38): always-ask→read,
  write→write, yolo→exec.
- `yolo` mode ignores tool-declared `override` prompts but still honors explicit
  user `deny`.
- `requiresApproval()` throws if policy is `deny`; returns `{required, reason}`
  for `prompt`.
- `formatApprovalPrompt()` builds the human prompt text, noting MCP-origin tools
  specially (`tool.name.startsWith("mcp__")`, line 171).

This is a **flat, session-scoped 3-tier capability model** (read/write/exec) with
per-tool override + global mode — no concept of "families"/"roles"/"risk classes"
beyond that tier, and no persistent multi-context allow-list enforcement at a
central choke point (contrast with CAD, §5).

---

## 2. OMP: extension mechanism (`swarm-extension` as example)

### 2.1 What "extension" means
Two related-but-distinct constructs in OMP:
1. **Extension runtime** (`packages/coding-agent/src/extensibility/extensions/*`,
   documented in `docs/extensions.md`) — the general plugin API every extension
   (including `swarm-extension`) is built on.
2. **`swarm-extension` package** itself — a *packaged user-facing extension*
   (`docs/user-facing-packages.md` reference) that layers multi-agent DAG pipeline
   orchestration on top of that API. It does NOT extend a different "extension
   point" system — it *is* one instance of an extension.

### 2.2 Extension factory contract
`export default function ext(pi: ExtensionAPI): void { ... }` — a TS/JS module.
Concretely, `swarm-extension/src/extension.ts:22-65`:
- `pi.setLabel("Swarm Orchestrator")`
- `pi.registerCommand("swarm", { description, getArgumentCompletions, handler })`
  — the *entire* extension surface it uses is slash-command registration; it does
  NOT register any LLM-callable tool (`pi.registerTool`) — it's driven by user
  slash-command only (`/swarm run <file.yaml>`), then it runs sub-agents via its
  own `PipelineController`/DAG code (`swarm/dag.ts`, `swarm/pipeline.ts`,
  `swarm/executor.ts`) and finally injects a synthetic conversation message
  (`pi.sendMessage({customType:"swarm-result", ...}, {triggerTurn:false})`,
  `extension.ts:181-194`) so the LLM sees the outcome.
- Registration-only during load; runtime actions (`pi.sendMessage`, tool
  execute-time actions) become valid only after `ExtensionRunner.initialize(...)`
  wires the session (`docs/extensions.md:38-65`) — calling them too early throws
  `ExtensionRuntimeNotInitializedError`.

### 2.3 Full hook/event surface (from `docs/extensions.md:194-252`, canonical
types in `extensibility/extensions/types.ts`)
- Session lifecycle: `session_start`, `session_before_switch/switch`,
  `session_before_branch/branch`, `session_before_compact/compacting/compact`,
  `session_before_tree/tree`, `session_shutdown`
- Prompt/turn: `input`, `before_agent_start`, `before_provider_request` (can
  replace the outbound provider payload), `after_provider_response`, `context`,
  `agent_start/agent_end`, `session_stop` (cancelable, capped at 8 continuations,
  never fires for subagents), `turn_start/turn_end`, `message_start/update/end`
- Tool lifecycle: `tool_call` (pre-exec, blockable), `tool_result` (post-exec,
  patchable), `tool_execution_start/update/end`, `tool_approval_requested/resolved`
- Reliability: `auto_compaction_start/end`, `auto_retry_start/end`,
  `ttsr_triggered`, `todo_reminder`, `goal_updated`, `credential_disabled`
- User command interception: `user_bash`, `user_python` (both overridable via
  `{result}`)
- `resources_discover` — typed but currently has no live emitter.

### 2.4 What extensions CAN register (`ExtensionAPI`, `docs/extensions.md:111-134`)
`on`, `registerTool`, `registerCommand`, `registerShortcut`, `registerFlag`,
`registerMessageRenderer`, `registerAssistantThinkingRenderer`, `setLabel`,
`getFlag`, `sendMessage`, `sendUserMessage`, `appendEntry`, `exec`,
`getActiveTools`/`getAllTools`/`setActiveTools`, `getCommands`,
`getSessionName`/`setSessionName`, `setModel`, `get/setThinkingLevel`,
`registerProvider` (!), `events` (shared bus), plus `pi.logger`, `pi.zod`,
`pi.typebox`, `pi.pi` (package exports).

`registerProvider` is notable: extensions can add a new LLM provider, not just
tools/commands.

### 2.5 Discovery
Filesystem-scanned extension source paths (`extension-loading.md`, referenced by
`ToolSession.extensionPaths`, `tools/index.ts:170-176`) — forwarded to subagents
so they re-bind to their own session-scoped `ExtensionAPI` rather than re-scanning
disk.

---

## 3. OMP: MCP client model (consuming external MCP servers)

Full pipeline (`docs/mcp-server-tool-authoring.md:5-16`, files under
`packages/coding-agent/src/mcp/*`):

```
Config sources (.omp/.claude/.cursor/.vscode/mcp.json, mcp.json, .mcp.json)
  -> discovery providers normalize to canonical MCPServer (capability layer, priority + dedupe by name)
  -> loadAllMCPConfigs -> MCPServerConfig (skips enabled:false)
  -> MCPManager connects + listTools (manager.ts, 1366 lines)
       - resolves OAuth credential injection (Bearer header / OAUTH_ACCESS_TOKEN env)
       - resolves header/env values via resolveConfigValue() (`!cmd` shell secrets, env-var-name-first)
       - best-effort loads resources/prompts, subscribes to resource updates
  -> MCPTool / DeferredMCPTool bridge (tool-bridge.ts, full file read) exposes each
     remote tool as mcp__<sanitized_server>_<sanitized_tool>
  -> AgentSession.refreshMCPTools() hot-swaps the live tool set (no restart)
```

- Transports: `stdio` (default), `http`, `sse` (kept, internally treated as http).
  Config validated structurally in `config.ts` (`validateServerConfig`): rejects
  both `command`+`url` set, requires the right field per transport, rejects
  unknown `type`.
- **Tool-bridge mechanics** (`tool-bridge.ts`, read in full):
  - `createMCPToolName(server, tool)` sanitizes to `[a-z_]+`, strips redundant
    server-name prefix, prefixes `mcp__` (lines 276-299).
  - `prepareOutboundArgs()` (lines 158-165) strips the harness-internal `i`
    intent field (unless the server's own schema declares it), drops unused
    optional placeholder args, and resolves `local://` URLs to real files before
    sending to the external server — i.e., OMP's internal-URL virtual filesystem
    is translated at the MCP boundary so external servers only ever see real
    paths.
  - Retry: `isRetriableConnectionError()` (network/stale-session patterns) drives
    one reconnect+retry; abort semantics preserved (`ToolAbortError`).
  - `normalizeSchemaForMCP()` runs the MCP JSON Schema through OMP's own JSON
    Schema pipeline (`packages/ai/src/schema/*`) before exposing it as a
    `CustomTool` parameter schema — i.e. **inbound** MCP schemas are normalized
    through the SAME schema machinery used for native tools (draft/dereference/
    normalize/strict-validation in `packages/ai/src/schema/`).
  - Approval: MCP-origin tools get a special note in the approval prompt
    (`approval.ts:171`, "Origin: MCP server tool") but otherwise obey the exact
    same tier/policy resolution as native tools — MCP tools have no
    `approval` field of their own unless a user override sets one, so by default
    they resolve to tier `"exec"` (`getToolDecision` default, `approval.ts:69`)
    and thus require write/yolo mode or an explicit allow.
- Live operator UX: `/mcp add|remove|enable|disable|test|reauth|reconnect|reload|
  resources|prompts|notifications` (`mcp-command-controller.ts`); config writes
  are atomic (temp+rename); `#reloadMCP()` disconnects all, reconnects, and calls
  `session.refreshMCPTools()`.
- Headless/SDK path: `discoverAndLoadMCPTools()` (`mcp/loader.ts`) — same pipeline
  minus the `/mcp` interactive UX, returns tools + per-server errors.

**mnemopi is itself an MCP *server* implementation** (not client):
`packages/mnemopi/src/mcp-server.ts` + `mcp-tools.ts` — a worked example of OMP
exposing its own capability (long-term memory) as an MCP server other clients
(e.g. Claude Desktop) can consume. This is the template a fork would mirror to
expose CAD's engine the other direction if needed, though CAD already has that
(`services/mcp/server.py`, FastMCP-based).

---

## 4. CAD: current tool system

### 4.1 Declaration + registration
CAD tools are plain Python functions decorated with `@mcp.tool()` from the
`mcp.server.fastmcp.FastMCP` SDK, defined per-family in `services/mcp/*_tools.py`
(cad_doc_tools, cad_eval_tools, cad_render_tools, cad_sim_tools, cad_asm_tools,
cad_drawing_tools, ecad_pcb_tools, ecad_sch_tools, cam_tools, cost_tools,
structural_tools, req_tools, gsl_tools, parts_tools, reveng_*_tools, sourcing_tools,
firmware_tools, ledger_tools, workspace_tools, dimension_scheme_tools, ~40 modules).
`register_<family>_tools(mcp)` functions register a family's tools into a live
`FastMCP` instance (`services/mcp/cad_tools.py` is now a compat facade re-exporting
the split per-family modules, `cad_tools.py:1-8,142`).

`services/mcp/server.py` (748 lines) is the FastMCP app entrypoint: builds
instructions text with a **capability-dial profile** system (`full` vs `lite`,
`AICAD_MCP_PROFILE` / capability-letter-derived, `server.py:28-101`) that tunes
how prescriptive the tool-usage instructions are per model tier (A/B/C
capability letters) — a concept with no OMP analogue.

### 4.2 Registry / dispatch — `services/mcp/tool_registry.py` (full file read,
311 lines) — this is the single most important file for fork feasibility:
- `_FamilyView` (lines 283-311): a proxy over the `FastMCP` object that
  intercepts `.tool()` so `server.py` wraps every `register_*_tools(mcp)` call
  site as `register_x_tools(_FamilyView(mcp, "family_name"))`. Every
  `@mcp.tool()` decoration is thus tagged into a **process-wide catalog**
  `{name: {family, risk}}` (`record()`, lines 85-88) and wrapped in `_guard(fn,
  name)` (lines 246-280) — ONE shared choke point for every tool, sync or async.
- **Risk classification** (`classify_risk`, lines 56-66): name-pattern-derived
  4-tier risk (`read`, `write`, `spend`, `external`) via prefix/suffix
  heuristics + explicit override table, overridable per-name.
  This is richer than OMP's fixed read/write/exec tier because it adds
  `spend` (metered/paid) and `external` (submit/export/import/request) classes
  for CAD-specific concerns (paid compute, artifact export/BOM submission).
- **Policy enforcement** (`is_allowed`/`deny_reason`, lines 162-221): layered
  checks in order — meta-tools always pass → per-tool `enabled` flag
  (Settings→AI→Tools) → `agent.tool_families` config (family disable) →
  risk-class deny-list (`AI_CAD_TOOLS_DENY_RISK`) → explicit allow-list
  (`AI_CAD_TOOLS_ALLOW`, or DAG-node `tools:` list via `set_policy()`).
  Default policy = everything allowed (fail-open by default, deny-by-exception).
- **Workspace write-lease integration** (`_workspace_lease_refusal`, lines
  225-243): mutating tools carrying a `doc_id` kwarg are checked against a
  multi-agent write-lease registry (`services/agent/workspace.py`) — refuses
  concurrent-agent conflicting mutation on the SAME document. No OMP analogue
  (OMP has no concept of a shared mutable document with multi-agent leases;
  closest is file-system edit + LSP diagnostics).
- Refusals are **contract-shaped JSON**, never exceptions/tracebacks:
  `{"ok": False, "reason": ..., "hint": ...}` (`refusal()`, lines 214-221) — the
  model always gets actionable text back, matching FastMCP tool-result
  conventions.
- `_get_fn(module_key, fn_name)` resolver pattern (used by
  `services/verify/battery.py`, `services/agent/reviewer_exec.py`,
  `services/dag/verify.py`) lazily imports and registers a family's
  `register_*_tools(mcp)` against a private, singleton `FastMCP`-like object
  cached in a module dict, then does `getattr` on the underlying real function —
  this is how NON-MCP callers (the verify DAG node, mission dispatch, DAG
  executor) invoke the exact same tool function objects the MCP surface would
  dispatch, guaranteeing ONE registry/ONE set of side effects regardless of
  entry point (agent turn tool-call loop vs deterministic DAG verify step).
  `_get_fn` covers module_keys `cad, recon, ecad, cam`; `reviewer_exec.py` and
  `services/dag/verify.py` each extend it with more families
  (`verify_battery`, `fit_report`, `research`, `gsl`, `firmware`, `structural`,
  `sourcing`, `reveng`) sharing the same private cache dict.

### 4.3 Tool schema surface for the model (`services/agent/tool_schemas.py`)
Not a JSON-Schema/Zod system like OMP — instead renders a compact **plain-text
signature + description block** for the turn prompt via `inspect.signature()`
on the REAL dispatchable callable (never fabricates a kwarg the dispatcher would
reject), token-budgeted per role tier, with `tiers.trim_tool_schema` controlling
verbosity (first-sentence vs first-paragraph). This is prompt-engineering
infrastructure, distinct from OMP's typed Zod/JSON-Schema tool-parameter model
(which providers consume directly as function-calling schemas). CAD tools are
NOT declared with JSON Schema at the Python layer at all — FastMCP derives the
MCP `inputSchema` from the Python function signature/type hints automatically,
and `tool_schemas.py` is a SEPARATE, redundant text rendering for the
in-house turn-prompt (non-MCP) path.

### 4.4 Mode-based tool allowlists — `services/agent/copilot_mode_tools.py`
(74/244 lines read) — per-mode (`agent|plan|swarm|edit|ask`) tool allowlists
loaded from a JSON contract (`contracts/copilot_modes.json`) shared **byte-for-
byte with the C++ host** (`core/agent/copilot_modes.cpp`) so Python and C++
runtimes cannot diverge — parity enforced by
`test_copilot_modes_parity.py` vs a C++ test over the same matrix. `SHARED_
DEFAULT_TOOLS` floor ensures every mode keeps doc lifecycle/web/advisor/ask_user.
This dual-runtime-contract pattern (JSON is the single source of truth consumed
by two languages) has no OMP equivalent and is a hard constraint for any fork:
whatever registers CAD tools into OMP's registry must not silently diverge from
the C++ host's understanding of what's allowed in each mode.

### 4.5 MCP consumption in CAD (client side) vs exposure (server side)
CAD's `services/mcp/` is almost entirely the **server** side — CAD tools ARE MCP
tools (FastMCP), consumed by external MCP clients (Claude Desktop/Code) AND by
CAD's own in-process agent loop via the `_get_fn` bypass (§4.2). I did not find
a CAD-side generic *MCP client* (connecting outward to arbitrary third-party MCP
servers) comparable to OMP's `packages/coding-agent/src/mcp/manager.ts` — CAD's
agent stack is architected as "be an MCP server, dispatch directly" rather than
"be an MCP client of many servers." (Not exhaustively verified across
`services/agent/*.py`; no `MCPManager`/`connectToServer`-equivalent surfaced in
greps — treat as [INFERENCE] pending a dedicated search of client-side code
before committing to "CAD has zero MCP client capability.")

### 4.6 `agent_turn.py` dispatch loop
`services/agent/agent_turn.py:12-15,2600-2603` — the turn loop parses a
fenced-JSON tool call from the model's text output (NOT native function-calling
in the OMP/Anthropic-tool-call-block sense — CAD's turn loop appears to use a
JSON-in-text convention) and dispatches against the SAME live MCP tool registry
(`battery._get_fn` singleton) the verify DAG node uses, applies
`agent.tool_options` (`param_defaults`, `max_calls_per_turn`, `timeout_s`), then
feeds the result back. [Needs confirmation whether native function-calling is
also used for some providers; only the fenced-JSON path was directly observed.]

---

## 5. Fork-integration feasibility: registering CAD-native tools into OMP

### 5.1 The core translation problem
OMP tools are declared as `{name, parameters: ZodSchema, execute(...): Promise<
AgentToolResult>, approval, tier}` TypeScript objects running in a Bun/Node
process. CAD tools are Python functions decorated `@mcp.tool()`, running
in-process with `libaicad` (native C++ core via ctypes `core_bridge`), addressed
either via FastMCP stdio/HTTP or via the in-process `_get_fn` bypass.

**Two integration paths exist, both already partially proven by existing code:**

**(A) MCP client path (near-zero new code) — reuse OMP's `MCPManager` unmodified.**
Since CAD already runs a full FastMCP server (`services/mcp/server.py`, stdio),
an OMP fork can add it as a configured MCP server
(`.omp/mcp.json: {"cad": {"command": "python", "args": ["-m","services.mcp.server"]}}`)
and the ENTIRE tool-bridge pipeline (§3) works with zero CAD-side changes:
tools appear as `mcp__cad_<name>`, schemas normalized automatically from
FastMCP-derived JSON Schema, approval defaults to tier `"exec"` (every CAD tool
prompts for approval by default — coarser than CAD's own 4-tier risk model,
see §5.2), and CAD's own `tool_registry.py` guard still enforces family/risk
denial server-side regardless of what OMP thinks it's allowed to call.
This is the SIDECAR pattern already decided project-wide, applied to tools
specifically. Effort: **S** — mostly config + verifying stdio process lifecycle
(who launches/owns the Python MCP server process: OMP's bun sidecar bridge, or
CAD's C++ host — needs a decision, see open questions).

**(B) Native TS tool shim path — write thin `AgentTool` wrappers per CAD tool
family that call the bun↔Python bridge RPC (the sidecar) directly**, bypassing
MCP's JSON-RPC/stdio overhead and getting native approval-tier mapping (CAD's
4-class risk → OMP's 3-tier: `read`→read, `write`→write, `spend`/`external`→exec
with `reason` set from the risk class) plus native OMP renderCall/renderResult
for the GUI. This requires: (1) a codegen step from CAD's FastMCP tool registry
(`tool_registry.catalog()` already gives {name, family, risk} + FastMCP tool
objects give JSON Schema) to generate one `AgentTool` per CAD tool at build time
or dynamically at session-start (mirroring how `tool-bridge.ts` builds MCPTool
per definition, but skipping the wire protocol and going straight to the bridge
RPC). Effort: **M-L** per family (drawing/sim/ecad/cam/plm each ~dozens of
tools) but the *pattern* is a single reusable generator, so marginal cost per
new CAD tool family is low once built.

### 5.2 Approval/risk model mismatch (must be resolved either way)
OMP: 3-tier (read/write/exec), single mode-derived ceiling.
CAD: 4-class (read/write/spend/external) PLUS write-lease-per-document PLUS
family-enable PLUS per-tool Settings toggle PLUS DAG-node explicit allow-list.
A fork MUST decide the mapping direction. Recommended: keep CAD's
`tool_registry.py` as the SOURCE OF TRUTH (it's the file the C++ host and DAG
verify path already trust — contract-frozen), and have the OMP-side approval
resolver treat every CAD-native tool's OMP `approval` field as a *pure derived
projection* of CAD's risk class (`spend`/`external` → `{tier:"exec", reason:
"<CAD risk class>"}`), NEVER let OMP's own approval override the CAD-side
`_guard()`/write-lease check — i.e. CAD tool_registry stays the final gate no
matter which UI/runtime dispatched the call, avoiding the double-registry
divergence the CAD codebase has clearly already spent significant effort
avoiding (`_get_fn` singleton pattern, `contracts/providers.md §6`,
`contracts/copilot_modes.json` C++/Python parity tests).

### 5.3 CAD copilot-mode allowlists vs OMP approval modes
These are ORTHOGONAL axes today: OMP's `ApprovalMode` (always-ask/write/yolo)
gates BY RISK TIER; CAD's copilot mode (agent/plan/swarm/edit/ask) gates BY
TOOL IDENTITY (explicit per-mode sets from a JSON contract). A fork exposing
CAD's copilot modes through OMP's UI needs a NEW axis — OMP has no "named mode
with explicit tool allowlist" concept; the closest is `pi.setActiveTools()`
(extension API, can restrict at runtime) or the swarm-extension's own per-agent
role tool restriction pattern (`swarm/schema.ts` — not read in depth here, but
implied by DAG per-agent config). Recommend implementing CAD's 5 copilot modes
as an OMP-side session-start hook that calls `pi.setActiveTools()` with the set
read from `contracts/copilot_modes.json` — keeping the JSON contract as sole
source of truth so the C++ host, Python agent, and OMP fork all read the same
file (three-way parity, extending CAD's existing two-way test).

### 5.4 The `_get_fn` dual-dispatch pattern is the fork's biggest asset
Because CAD tools are ALREADY resolvable both via FastMCP `tools/call` (for MCP
clients / OMP sidecar) AND via direct Python function reference (`_get_fn`, for
CAD's own DAG/verify/reviewer machinery), an OMP fork gets the DAG/verify/
mission-dispatch machinery "for free" over the exact same tool functions the
chat surface calls — no parallel reimplementation needed. Any TS-side
convenience wrapper (path B above) should still route through the SAME bridge
RPC endpoint that ultimately calls `_get_fn`-resolved functions, not a second
Python entrypoint, to preserve this guarantee.

### 5.5 Native-binary / bun-only risk
CAD's engine work happens through `core_bridge` (ctypes → `libaicad` native
DLL/so, single-process-affinity likely — same process the desktop app runs).
OMP tools run in Bun. Any FORK where OMP drives CAD tools MUST go through IPC
(the already-decided bun-sidecar bridge) rather than attempting to load
`libaicad` into Bun directly — ctypes bindings, GPU/rendering, and any
thread-affinity in the native core make in-process reuse from Bun infeasible
without a full native N-API/FFI rewrite (out of scope; not attempted by
anything observed in either repo).

---

## 6. Recommendation table

| Feature | OMP location | CAD counterpart | Recommendation | Effort | Hard deps/risks |
|---|---|---|---|---|---|
| Core AgentTool loop (declare/dispatch) | `coding-agent/src/tools/index.ts`, `approval.ts` | `agent_turn.py` fenced-JSON dispatch + `tool_registry.py` guard | FORK (drive UI from OMP loop) | L | fenced-JSON→native tool-call migration on CAD side if kept; approval-tier mapping (§5.2) |
| Approval/permission tiers | `tools/approval.ts` (3-tier) | `tool_registry.py` (4-class risk + write-lease + family + settings) | KEEP CAD's as source of truth; OMP tier = derived projection | M | must never let OMP override CAD's `_guard()` |
| CAD tool exposure to OMP | n/a | FastMCP `services/mcp/server.py` (stdio) | SIDECAR via existing MCP client (`MCPManager`) — zero CAD change | S | who owns the stdio process lifecycle (bridge vs C++ host) |
| Native TS tool wrappers (perf/UX upgrade over MCP) | `mcp/tool-bridge.ts` pattern | `tool_registry.catalog()` + FastMCP schemas | PORT generator (codegen `AgentTool` per CAD tool from catalog) | M-L (one-time), S per new family after | must route through same bridge RPC → `_get_fn`, not a second Python entrypoint |
| Extension mechanism (hooks: tool_call/tool_result/session lifecycle) | `extensibility/extensions/*`, `docs/extensions.md` | none (no plugin hook system) | FORK — reuse OMP's hook bus wholesale; CAD gets it for free | S (already exists in OMP) | none significant |
| swarm-extension (DAG pipeline orchestration) | `packages/swarm-extension` | `services/dag/*`, `mission_dispatch.py`, `buildgraph` | KEEP CAD's DAG engine (far more mature: verify nodes, write-leases, ledger); do NOT adopt swarm-extension's YAML DAG | KEEP | swarm-extension is comparatively thin (single-file DAG, no risk/lease model) |
| Copilot modes (agent/plan/swarm/edit/ask) | none (closest: `pi.setActiveTools`) | `copilot_mode_tools.py` + `contracts/copilot_modes.json` (C++ parity) | PORT as OMP session-start hook reading the same JSON contract | M | 3-way (C++/Python/TS) contract parity now required, not just 2-way |
| Tool schema for prompt | Zod/JSON-Schema native (`packages/ai/src/schema/*`) | `tool_schemas.py` text-signature renderer (separate from FastMCP's auto schema) | KEEP CAD's FastMCP-derived JSON Schema as the ONE schema (drop `tool_schemas.py`'s redundant text renderer once native function-calling used) | S | only if CAD's turn loop moves off fenced-JSON to native calling |
| MCP client (consuming 3rd-party servers) | `packages/coding-agent/src/mcp/*` (full manager, OAuth, discovery, `/mcp` UX) | not found (CAD is MCP-server-only) [INFERENCE, needs deeper search] | FORK — reuse OMP's MCP client wholesale; CAD gains 3rd-party MCP consumption for free | S | none — pure gain |
| MCP server-authoring template | `packages/mnemopi/src/mcp-server.ts` | `services/mcp/server.py` (CAD's own, more mature) | KEEP CAD's FastMCP server | KEEP | none |
| Domain tool families (drawing/sim/ecad/cam/plm) | none native | `cad_drawing_tools.py`, `cad_sim_tools.py`, `ecad_*_tools.py`, `cam_tools.py`, `plm_bridge.py` | KEEP CAD's Python implementations; expose via path (A) or (B) above | — | native engine coupling (`core_bridge`/`libaicad`), see §5.5 |
| Write-lease / multi-agent doc concurrency | none | `workspace.py` (`check_write_lease`), wired into `tool_registry._guard` | KEEP CAD's; OMP fork must call through the same guard, not bypass it | — | any OMP-native tool wrapper (path B) MUST NOT skip this check |

---

## Open questions for the decision menu
1. Path (A) MCP-sidecar-only vs Path (B) native-TS-wrapper-generator for CAD
   tools — (A) is near-free but adds MCP JSON-RPC overhead + coarser
   (exec-tier-by-default) approval UX; (B) is nicer UX but is real engineering
   (codegen + bridge RPC design).
2. Who launches/owns the `services/mcp/server.py` stdio process in a fork: the
   bun sidecar bridge (spawns Python as a subprocess) or the existing C++ host
   (which server.py's own docstring says "the desktop host registers it into
   the machine's MCP client configs" — implying C++ already manages this
   lifecycle today)?
3. Whether to migrate CAD's turn loop off fenced-JSON-in-text tool calls to
   native provider function-calling if OMP becomes the loop driver (OMP assumes
   native tool-call blocks per `packages/ai`'s provider abstraction).
4. Whether `copilot_mode_tools.py`'s JSON contract becomes 3-way canonical
   (C++, Python, TS) or whether OMP fork re-derives its active-tool set from
   CAD's Python side at session start via an RPC call instead of a shared file
   (avoids a 3rd copy of the parity problem).
</content>
