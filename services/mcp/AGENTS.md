# services/mcp — MCP server

**Owner:** N5e (C++ cutover) / current Python implementation.

stdio MCP server exposing CAD tools to external agents (Claude Code, Cursor, etc.).

## Entry points
- `python -m mcp_server.server`
- `python -m mcp_server.test_roundtrip`

## Migration
- Thin client over `libaicad` at N5e; Python deleted when C++ agent/MCP land.
