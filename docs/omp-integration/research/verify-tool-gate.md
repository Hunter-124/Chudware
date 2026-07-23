# Verify: tool registry / approval gate + copilot-mode contract

## Graphify cross-check (disagreement flagged)
`graphify query "how are agent tools registered gated by family and risk and dispatched"` and
`--type explain tool_registry` / `explain copilot_mode_tools` returned **irrelevant/stale
results** — CAM toolpath nodes (`core/cam/model.h`, `core/agent/micro.cpp` Step/verify_step),
nothing from `services/mcp/tool_registry.py` or `services/agent/copilot_mode_tools.py`. The
prebuilt graph at `graphify-out/graph.json` does not surface this Python subsystem meaningfully
for this query shape. **Treated as unreliable for this area; all verdicts below are from direct
source reads.**

## 1. `services/mcp/tool_registry.py` — CONFIRMED (single chokepoint)

- **`_FamilyView.tool()`** (L283–305): the ONE registration interceptor. `view(mcp, family)`
  (L308–311) wraps FastMCP so every `register_*_tools(view(mcp, family))` call site tags +
  guards its tools transparently — `server.py` never calls `mcp.tool()` directly for domain
  tools, always through `_reg.view(mcp, family)` (confirmed ~90 call sites in
  `services/mcp/server.py:274-570`, spanning families: cad, sim, gsl, recon, intake, ecad, cam,
  plm, parts, sourcing, em, mission, ledger, advisor, workspace, reveng, research, gsl).
- **`{family, risk}` tagging**: `record(name, family, risk=None)` (L85–88) stores into a
  process-wide `_CATALOG` dict; risk is auto-derived by `classify_risk()` (L56–66) from
  name-prefix/suffix heuristics (`RISK_READ/WRITE/SPEND/EXTERNAL`, L24–48), with an explicit
  override table (`_RISK_OVERRIDES`, empty today) and an explicit read-exact set for names the
  classifier misses (e.g. `ledger_get`, `workspace_status`). Default-to-WRITE fail-safe for
  unmatched names (L32, L66) — CONFIRMED, sound design (unknown tool assumed mutating).
- **`_guard(fn, name)`** (L246–280): the single shared dispatch wrapper, sync+async variants,
  applied to every registered tool at decoration time (`_FamilyView.tool.deco`, L301:
  `return inner(_guard(fn, name))`). At call time it checks `is_allowed(name)` →
  `deny_reason()` (settings-disabled tool, disabled family in config, deny-by-risk-class env,
  active allow-list) and returns a contract-shaped `{"ok": False, "reason", "hint"}` refusal
  (never a traceback) instead of running — CONFIRMED exactly as the dossier/plan claims.
- **Write-lease integration**: `_guard` also checks `doc_id` kwarg against
  `_workspace_lease_refusal()` (L225–243) for any non-READ-risk tool, deferred-importing
  `services.agent.workspace.check_write_lease`. Read tools skip this entirely (fast path,
  L236-237) — CONFIRMED.
- **Default policy = allow-all** (module docstring L14, `is_allowed`/`deny_reason` L162-211):
  behavior-neutral until a DAG node `tools:` list or `CHUDWARE_TOOLS_ALLOW`/`_DENY_RISK` env is
  set — CONFIRMED, matches contract §6.

## 2. `_get_fn` dual-dispatch resolver — CONFIRMED (one live registry, multiple call surfaces)

- Canonical definition: `services/verify/battery.py:614` `_get_fn(module_key, fn_name)` —
  resolves the raw Python function off a private in-process FastMCP instance per family
  (`_PRIVATE_MCP` cache), for families `cad/recon/ecad/cam`.
- `services/dag/verify.py:201` defines its OWN `_get_fn(module_key, fn_name)` that explicitly
  **delegates to `battery._get_fn`** for cad/recon/ecad/cam (L208-209:
  `if module_key in ("cad","recon","ecad","cam"): return _battery_get_fn(...)`) and extends the
  *same* `_PRIVATE_MCP` cache locally for `firmware`/`structural`/`sourcing` (L193-230) —
  confirming "ONE registry, extended, not duplicated."
- `services/agent/reviewer_exec.py:68,90` (AC-check dispatch) and
  `services/mcp/design_review_tools.py:246,277,324` (design_review battery) both import and call
  `battery._get_fn` / the `verify.py` extension directly — same singleton, three call sites
  (agent_turn's tool loop via reviewer_exec, DAG verify node via verify.py, design_review MCP
  tool) — CONFIRMED "DAG/verify/chat all call the same fns," per docstrings at
  `agent_turn.py:12-14` ("dispatch it against the live MCP tool registry — the SAME
  `services.verify.battery._get_fn` singleton the verify node uses") and
  `reviewer_exec.py:9-11`.
- **Important nuance not to overstate**: `_get_fn` bypasses `_FamilyView`'s allow-list `_guard`
  wrapper *for the AC/verify dispatch path* — it resolves the raw function off a **private**
  `FastMCP("battery-<module>")` instance (`battery.py`), not the live `server.py` `mcp` object,
  so those internal calls are NOT subject to the same-process allow-list gate that the live
  agent's tool-call loop is. This is intentional (verify/AC-checks run deterministic checks, not
  user-facing tool calls) but is a distinct, ungated code path — flag for the plan: any new
  firmware/MCP-client tool that needs both (a) live-agent dispatch gated by `_guard`, and (b)
  DAG-verify dispatch via `_get_fn`, must be registered on BOTH the live `mcp` (via
  `_reg.view(mcp, family)`) AND wired into `verify.py`'s module_key extension (as firmware
  already is, `verify.py:216-221` `register_firmware_tools(_mcp)` on a private `battery-firmware`
  FastMCP instance) if AC/verify checks need to call it.

## 3. `services/agent/copilot_mode_tools.py` — CONFIRMED (JSON contract, shared C++/Python)

- Single source of truth: `contracts/copilot_modes.json` (`aicad.copilot_modes/1`), loaded once
  at import time (`_load_contract()`, L49-54) via `_contract_path()` (L36-46) which resolves
  `CHUDWARE_COPILOT_MODES_JSON` env override → `<repo>/contracts/copilot_modes.json`. Module
  docstring (L1-9) states explicitly: "the Python (MCP/DAG) and C++ (in-app,
  `core/agent/copilot_modes.cpp`) runtimes cannot diverge" and parity is tested via
  `services/agent/test_copilot_modes_parity.py` vs `core/tests/test_copilot_modes.cpp` over the
  same mode×tool matrix — CONFIRMED (module keeps NO private copy of the tool sets, all sets are
  `frozenset(_CONTRACT[...])`, L57-88).
- Modes: `ask|plan|edit` = restricted allowlists (`tools_for_mode`, L97-112); `agent|swarm` =
  unrestricted at the mode layer (`None` = full catalog, role-card allowlist still applies).
  `apply_mode_tools()` (L115-186) intersects role_tools with the mode set, applies denylists
  (`_MUTATING_TOOLS`, `_EDIT_DENIED`), then unions `SHARED_DEFAULT_TOOLS` as a floor so restricted
  modes never dead-end — CONFIRMED matches plan's description.
- This module governs *mode*-layer restriction; `tool_registry.py`'s `_guard` governs
  *dispatch*-layer allow/deny (settings, family, risk, lease). They are two independent, both
  always-active gates a tool call must pass — CONFIRMED as complementary, not overlapping,
  seams.

## 4. New firmware tools — CONFIRMED pattern already exists

`mcp_server.firmware_tools.register_firmware_tools` is ALREADY registered twice: once directly on
the live `mcp` tagged family `"ecad"` (`server.py:428`: `register_firmware_tools(_reg.view(mcp,
"ecad"))`), and once as a `verify.py`-local extension for AC/DAG-verify dispatch on a private
`battery-firmware` FastMCP instance (`verify.py:216-219`). **New firmware capability tools slot
in via the identical pattern**: import + `register_<x>_tools(_reg.view(mcp, "<family>"))` in
`server.py` (gets tagged/guarded automatically, no `tool_registry.py` edit needed — module
docstring L4-5 says explicitly "the individual `*_tools.py` modules are never edited"), plus (if
DAG-verify/AC checks need to invoke them) an entry in `verify.py`'s `module_key` branch (L216 for
firmware already there) or `reviewer_exec.py`'s `_EXTRA_MODULES` table (L49-51, currently
`verify_battery`/`fit_report`).

## 5. MCP-server-only claim — **CONFIRMED**, with nuance the plan already captures correctly

Grep across the whole repo for MCP-client-style code (`ClientSession`, `stdio_client`,
`sse_client`, `MCPManager`, `.connectToServer`) found **zero hits**. All "MCP client" mentions in
the codebase refer to *external* clients (Claude Code/Cursor/VS Code/opencode) connecting **into**
CAD's own FastMCP server (`services/mcp/server.py`, stdio transport) — confirmed via:
- `installer/setup_mcp_clients.py` + `mcp_setup.bat` — writes THIRD-PARTY client configs
  (`.mcp.json`, `.vscode/mcp.json`, Grok `.grok/`) so external tools can talk to CAD's server;
  never the reverse.
- `contracts/interaction.md:138`, `contracts/deliverables.md:292`, `contracts/providers.md:5-6` —
  all describe "external MCP clients" as consumers of CAD's server capabilities (elicitation,
  resource_links, provider auth), not CAD consuming other servers.
- `docs/omp-integration/research/cad-embedded-inventory.md:165-167` (prior research) already
  states this correctly: "CAD IS an MCP server, not an MCP client of other servers."
- `docs/omp-integration/research/omp-extension-tools.md:290-299,417` flags this as
  `[INFERENCE, needs deeper search]` pending confirmation — **this verification closes that gap
  and upgrades it to CONFIRMED**: no `MCPManager`/`ClientSession`/`connectToServer`-equivalent
  exists anywhere in `services/` or `core/`. CAD is unambiguously MCP-server-only today.

An external MCP-CLIENT capability (CAD consuming a third-party MCP server, e.g. hardware-vendor
OpenOCD-MCP) does NOT exist and would need net-new code (a client/manager module) — but its
integration with the existing gate is straightforward and requires no change to
`tool_registry.py`'s chokepoint design: any tools discovered from a remote MCP server would need
to be wrapped as local FastMCP tool functions (thin proxies calling the remote server) and
registered the same way — `register_<remote>_tools(_reg.view(mcp, "<family>"))` — so they get
`{family, risk}` tagged and pass through the same `_guard`. There is no way to expose a raw
external-MCP tool directly to the agent without going through `_reg.view()`, since `server.py`'s
`mcp` instance IS the only tool surface the agent's turn loop / DAG dispatch resolves against.

## Corrections to the plan
None substantive — the plan's characterization of the gate (`tool_registry.py` chokepoint,
`{family,risk}` tagging, `_get_fn` singleton, MCP-server-only) all CONFIRMED. Two additions worth
folding in:
1. **`_get_fn`'s AC/verify dispatch path is NOT gated by `_guard`'s allow-list** — it resolves
   raw functions off private `battery-<family>` FastMCP instances, separate from the live `mcp`
   object the agent's turn loop dispatches against. New tools needing both live-agent and
   DAG-verify access must register on both surfaces (pattern: firmware already does this).
2. The `[INFERENCE]` in `omp-extension-tools.md` about MCP-client absence is now **CONFIRMED**
   by exhaustive grep — no MCP client/manager code exists anywhere in the repo.
