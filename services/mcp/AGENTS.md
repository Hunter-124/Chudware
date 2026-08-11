# services/mcp — MCP server

**Owner:** N5e (C++ cutover) / current Python implementation.

stdio MCP server exposing CAD tools to external agents (Claude Code, Cursor, etc.).

## Entry points
- `python -m services.mcp.server`
- `python -m services.mcp.test_roundtrip`

## Layout (T-RF8)
- `cad_tools.py` / `ecad_tools.py` / `reveng_tools.py` are compat facades; the tools live
  in per-family modules: `cad_{doc,eval,render,exchange,sim,asm,drawing}_tools.py` over
  `cad_common.py` + `cad_guide.py`; `ecad_{sch,pcb,sim}_tools.py` over `ecad_common.py`;
  `reveng_{session,build}_tools.py` over `reveng_common.py`. New tools go to the matching
  family module (shared helpers to the `*_common.py`), never back into the facades.

## Migration
- Thin client over `libchudware` at N5e; Python deleted when C++ agent/MCP land.
