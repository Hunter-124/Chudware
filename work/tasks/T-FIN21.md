---
id: T-FIN21
title: PCB reliability - board assembly modal + vibration screening
workstream: FIN
owner: fable-fin
status: doing
branch: task/T-FIN21-pcb-reliability-vibe
depends_on: [T-FIN16]
lane: simulation
kind: new
priority: 4
---

## Context

Electronics that ship get shaken: reuse the T-FIN16 board+components solid (component
masses from T-FIN8 metadata) for run_fea_modal on the mounted board (mount holes fixed),
flag resonances inside a user-supplied excitation band, and run frequency-response at the
worst mode to estimate board deflection — screening against the classic deflection-based
solder-joint risk heuristics (documented as heuristics, not life prediction). One tool:
ecad_vibe_check.

## Files / areas touched

- services/mcp/ecad_thermal.py or ecad_reliability.py — ecad_vibe_check
- reuse fea_modal + fea_frequency_response, mount-hole fixture generation
- test: demo board modal solve, seeded heavy-component fixture lowers f1 as expected

## Definition of done

- Demo board reports mounted natural frequencies + band verdict
- Deflection screen produces a documented heuristic verdict with citations
- Heavy-component fixture shifts modes plausibly (sanity regression)
