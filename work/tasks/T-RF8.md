---
id: T-RF8
title: RF wave-1 MCP god-file split + dual-import shim removal (node R1e)
workstream: RF
owner: unassigned
status: done
branch: task/T-RF8-mcp-split
depends_on: [T-RF3]
lane: ai-copilot
kind: overhaul
priority: 2
---

## Context

344 MCP tools across ~60 modules; god files: cad_tools.py 7,952 lines/68 tools,
ecad_tools.py 4,479, ascii2d.py 3,516, reveng_tools.py 2,040. Registration pattern
(server.py + tool_registry.py) is fine — keep it. Five modules carry drift-prone
dual-import shims between canonical and compatibility package names.

## Scope

Split cad_tools.py into per-family modules (doc/sketch/feature/eval/export…), same for
ecad_tools.py and reveng_tools.py; keep tool_registry.py as the single enforcement
point and server.py's register_* style. Remove the 5 dual-import shims in favor of one
canonical path. MCP freshness rule: update server instructions + cad_guide + touched
work/context packs in the same branch.

## Definition of done

- [x] No MCP module over ~2k lines; tool count unchanged (344) and all register.
- [x] Zero dual-import shims remain.
- [x] MCP server boots and a smoke tool call works; pytest services/agent green.
- [x] gate.sh green; REFACTOR_LOG handoff; MCP docs refreshed.
