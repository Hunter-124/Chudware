# services/mcp — CAD tool registry and stdio MCP server

`services/mcp/server.py` (`mcp = FastMCP("Chudware", …)`) is the one live CAD tool
registry. It has two consumers:
1. External MCP clients over stdio: `python -m services.mcp.server`, server id `chudware`,
   registered by `services/installer/register_mcp.py` / `setup_mcp_clients.py`.
2. The online Chudcode agent, in-process: `services/agent/chudcode_tools.build_host_tools`
   imports this registry. There is no MCP hop and no second catalog.

Engine-bound tools use `core_bridge.call`. It defaults to the shared
`chudware-hostd` via `daemon_bridge`; `CHUDWARE_ENGINE_DAEMON=0` loads libchudware
in-process instead. There is no C++ MCP server.

## Canonical guidance
- `docs/dev/chudcode-integration.md` — host-tool projection, allowlist tri-state, floors,
  CAD chokepoints, result envelopes.
- `docs/dev/engineering-notes.md` — persistence/save invariants (`cad_common`),
  manufacturing/print rules.
- Tool-surface freshness: a change to an agent-facing tool, prompt, or workflow updates the
  server instructions in `server.py` and the matching `cad_guide.py` topics in the same
  change. `test_mcp_freshness.py` fails when a registered tool appears in neither;
  `scripts/advertised_tool_audit.py` (run by `scripts/check.py`) audits the advertised
  tool surface.

## Entry points
- `python -m services.mcp.server` — the stdio server.
- `python -m services.mcp.test_roundtrip` — an in-process roundtrip. In daemon mode it
  attaches to the running hostd, so use a private daemon or `CHUDWARE_ENGINE_DAEMON=0`.
- `pytest services/mcp -q` (service venv). `conftest.py` starts a private hostd and SKIPs
  the suite when none can start.

## Layout
- Registration: each family registers via `register_*_tools(tool_registry.view(mcp,
  "<family>"))`. `tool_registry` stamps `{family, risk}` and enforces policy at dispatch.
- `cad_tools.py`, `ecad_tools.py` and `reveng_tools.py` are compat facades for importers.
  The tools live in per-family modules:
  - `cad_{doc,eval,render,exchange,sim,asm,drawing,motion,animation}_tools.py` over
    `cad_common.py` + `cad_guide.py`;
  - `ecad_{sch,pcb,sim,qa}_tools.py` over `ecad_common.py`;
  - `reveng_{session,build}_tools.py` over `reveng_common.py`.
- New tools go to the matching family module, never into the facades. Shared helpers go
  in the `*_common.py`. A new family also updates `test_mcp_freshness.py`.
