---
id: T-FIN26
title: Docs/MCP freshness sweep - cad_guide topics, context packs, graphify refresh
workstream: FIN
owner: unassigned
status: todo
branch: task/T-FIN26-docs-mcp-freshness-sweep
depends_on: [T-FIN25]
lane: platform-infra
kind: fix
priority: 3
---

## Context

Per the standing MCP freshness rule each task updates its own docs, but a closing
sweep catches the cross-cutting surface: cad_guide('firmware'/'ecad'/'simulation') read
end-to-end for coherence with the new tools, MCP server instructions refreshed, work/context
section packs for core-sim/ecad/services-mcp regenerated (work-ctx-refresh), graphify
update run, and the live-vs-gated capability map in docs updated (the audit convention from
T-V1). Also sweep T-SIM10/T-SIM11 statuses to reflect what T-FIN24 absorbed.

## Files / areas touched

- services/mcp cad_guide topics + server instructions
- work/context/ packs via work-ctx-refresh
- docs/ capability map update
- graphify update .

## Definition of done

- cad_guide topics accurate against the shipped tool surface (spot-checked by fresh-agent dry run)
- Context packs regenerated; graphify current
- Board statuses reconciled (T-SIM10 superseded note, T-SIM11 linkage)
