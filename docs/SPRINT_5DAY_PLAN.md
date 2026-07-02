# 5-Day Sprint — Idea-to-Production Walking Skeleton

Companion to [PLATFORM_ADR.md](PLATFORM_ADR.md). Team of 4 (A = Bruh, B/C/D = teammates),
every task designed to be driven with Claude Code / the in-app agent against this repo
(graphify is set up — start every task with `graphify query`).

**Sprint goal (the Day-5 demo):** one prompt — *"battery-powered soil-moisture sensor on a
cedar post"* — runs through the DAG orchestrator and touches every pillar: mechanical enclosure
(existing), PCB (existing ECAD), firmware built + tested in Renode (new), priced BOM with
supplier links + fab quote (new), post structural check with rate-table cost estimate (new),
one human approval gate before "ordering."

**Scope guardrails — explicitly OUT of this sprint:** photo metrology, autorouting
improvements, full IFC/BIM authoring, bundling any building-code text, GNN recognition,
Werk24/TraceParts paid tiers, C++ ports of anything new, any UI beyond one DAG panel + one
sourcing panel. If a task is fighting you for >half a day, stub it and log it to the backlog.

---

## Workstream A — DAG Orchestrator (Person A)

| Day | Task |
|---|---|
| 1 AM | Freeze `contracts/dag_schema.md`: node types (`plan`, `design`, `ecad`, `firmware`, `structural`, `source`, `simulate`, `verify`, `gate`, `report`), node contract (inputs/tools/end_condition/verify/on_fail/autonomy), checkpoint format, run states. |
| 1 PM | `services/dag/` scaffold: LangGraph + `langgraph-checkpoint-sqlite`; executor that loads a graph JSON, runs nodes, persists checkpoints to `%APPDATA%/AI-CAD/runs/`. |
| 2 | Node runner = wrap existing agent: each node gets a **context pack** (GSL `describe_part` output, node inputs, allow-listed MCP tools only — never full history). Port `mission_planner.md` → `dag_planner.md` prompt that emits nodes+edges instead of an ordered list; cap graph at ~20 nodes; every cycle must carry a retry budget (working loops, guaranteed termination). |
| 3 | Human-in-the-loop: `gate` nodes via LangGraph `interrupt()`; add `pause` verdict to `core/agent/machine.h` per INTAKE plan Gate 0; resume-from-checkpoint after app restart. Autonomy dial: per-run default + per-node override. |
| 4 | Verify nodes: wire deterministic checks as end conditions (`check_interference`, `ecad_run_erc/drc`, firmware test pass, D/C ratio < 1.0, budget ≤ target). MCP tools: `dag_plan`, `dag_run`, `dag_status`, `dag_approve`, `dag_abort`. |
| 5 | Integration day: run the demo mission; fix seams; record the run. |

## Workstream B — Sourcing, Cost, Spec-Sheet Matching (Person B)

| Day | Task |
|---|---|
| 1 AM | `contracts/sourcing_schema.md`; extend `VendorPart` with `unit_price`, `currency`, `price_breaks[]`, `stock`, `lead_time_days`, `moq`, `fetched_ts` (additive → no schema bump). |
| 1 PM | **Nexar (Octopart) provider** — GraphQL, free 1K calls/mo — filling price/stock/datasheet for electronic parts; Digi-Key + Mouser providers behind the same `PartProvider` interface (keys applied for on day 1). |
| 2 | **BOM rollup:** walk assembly tree + ECAD BOM CSV → unified BOM (made vs bought); `bom_rollup` + `bom_price` MCP tools. Bought lines resolve via providers; made lines via cost model (below). |
| 3 | **Cost model:** material = volume × density × $/kg (seed table, user-overridable, Commodities-API for live metal/lumber reference); machining = existing CAM `cycle_time_s` × machine rate config → `cam_cost_estimate` tool. Quote-out: Xometry API (STEP upload → instant quote) + JLCPCB API (PCB quote) as `request_quote` tool. |
| 4 | **Spec-sheet matching v1 (deterministic path):** BOLTS (LGPL dist) blt tables → parametric generation through existing `core/parts` generators for standard fasteners/bearings; part-number normalization (strip vendor suffixes) → BOLTS/KiCad lookup. |
| 5 | **Spec-sheet matching v2 (AI path):** LLM extracts a dimension dict from a pasted datasheet/listing → fills a parametric template (`script` feature) → GSL render for user confirmation → registers as `VendorPart` with `source: "spec_sheet"`. This is the INTAKE plan's `search_reference_data` path — reuse its schema. Integration demo. |

**Rules:** LCSC lookups user-triggered only, never cached/redistributed. McMaster = link-out
until eProcurement approval. GrabCAD: no programmatic fetch, ever (ToS).

## Workstream C — Firmware/Embedded (Person C)

| Day | Task |
|---|---|
| 1 | `contracts/firmware_rpc.md` + `engine/firmware/` scaffold. Toolchain baseline: **CMake + arm-none-eabi** template project (zero ToS risk) with `compile_commands.json`; ESP-IDF + Arduino CLI (both subprocess-safe) as alternate backends. Project record persisted via doc-meta, linked to the ECAD board doc. |
| 2 | **Compile loop:** `fw_new_project(board|mcu)`, `fw_build`, `fw_sources` MCP tools; MCU selected from ECAD BOM when present (the chip picked in schematic seeds the firmware target). Pin-map header generated from ECAD netlist (net names → `#define`s) — this is the pillar's killer feature, nobody links schematic → firmware pins automatically. |
| 3 | **Emulation loop: Renode (MIT, bundleable).** `fw_sim_run` boots the ELF on a Renode platform, captures UART, asserts on expected output/GPIO state (Robot-style keywords); this is the agent's end condition: generate → build → emulate → assert → iterate. QEMU fallback for generic Cortex-M. |
| 4 | **Hardware path:** probe-rs (MIT) bundled for flash + DAP debug (`fw_flash`, `fw_debug_*` tools); esptool subprocess for ESP32. clangd (subprocess LSP, `--query-driver='**/arm-none-eabi-*'`) for a code panel in the app. |
| 5 | `firmware.md` agent prompt (generate HAL-level C from a behavior spec + pin map); demo: blink/sensor-read firmware written by the agent, proven in Renode, for the demo board. |

## Workstream D — Civil/Structural/Carpentry (Person D)

| Day | Task |
|---|---|
| 1 | `contracts/structural_schema.md` following the ECAD precedent: `engine/structural/`, doc kind `aicad.structure/1`; element types `beam`, `column`, `post`, `joist`, `stud_wall`, `footing` compiled to B-rep via OCCT prisms (they render in the normal viewport). |
| 2 | **PyNite (MIT) integration:** structure doc → PyNite model → member forces/deflections; load cases (dead/live/wind presets). |
| 3 | **Section libraries + checks:** bundle AISC shapes DB (free) + dimensional lumber table; D/C ratio post-processor (simplified AISC/NDS-style checks, labeled "preliminary — not sealed engineering"); `struct_analyze`, `struct_check` MCP tools. |
| 4 | **Code compliance (agent, not database):** jurisdiction-adoption metadata bundled; `code_check` = agentic RAG against public code viewers at runtime, returning citations + a deterministic rule pass (span limits, deflection L/360, fastener schedules) from our own encoded rules. Never bundle code text. |
| 5 | **Cost for work:** takeoff from structure doc (board-feet, steel weight, fastener count via Workstream B's BOM) × user-editable rate table seeded with ENR national averages → `struct_estimate` tool. Demo: cedar post under sensor-enclosure load, checked + priced. |

---

## Workstream E — Piping + Hydraulics (sprint 2, starts after T-INT)

Tracked as T-E1..T-E5 in `work/tasks/`. Summary: E1 — `engine/piping/` module (ECAD-precedent)
with pipe-run sweeps + CSV fitting catalog seeded from `fluids.piping` B36 data; E2 — 1D network
solve (EPANET/WNTR) + pressure-drop reports (`fluids`) + user-CSV pump curves with affinity
scaling; E3 — pandapipes gas/heat backend + CoolProp properties + pump/system operating point;
E4 — internal-volume extraction → existing LBM/OpenFOAM CFD with 1D-vs-3D verify node; E5 —
oil-hydraulic circuits (ISO 1219 schematic layer, OpenHydraulics FMUs via fmpy, sizing calcs
feeding the sourcing BOM). All bundle-freely licenses except OpenHydraulics runtime, handled as
FMU subprocess. See PLATFORM_ADR.md Decision 6.

## Post-Sprint Backlog (priority order)

1. **V&V harness** — CoFEA + MacNeal-Harder + NASA TMR cases in CI, auto-generated V&V report
   citing ASME V&V 10/20; CalculiX (subprocess) as second structural opinion. (~1 wk)
2. **Format interop track** (with the reversing effort): openNURBS/.3dm (days — MIT lib);
   Parasolid payload extraction from NX/Solid Edge (~1 wk); DXF via ezdxf (days); then CATIA V5,
   Inventor, Altium clean-room parsers as specs arrive; ODA membership decision for DWG/Revit.
3. **Intake Gates 1–3** (process profiles, photo metrology, fit coupons) per INTAKE plan.
4. **Fatigue + thermal FEA** (S-N curves on existing static results; CalculiX thermal),
   natural-frequency already covered by modal — expose in DAG verify nodes.
5. **FPGA flow:** Verilator (Artistic-2.0, bundleable) + GHDL/Icarus subprocess; agent
   write→simulate→assert loop mirroring the Renode pattern.
6. **Certified-tool escape hatch:** Nastran deck + OpenFOAM case export for customers needing
   sealed/certified analysis.
7. **Wire harness / cable design** (connects ECAD to mechanical routing).
8. **Tolerance stack-up analysis** (1D worst-case + RSS on feature dims — cheap, high value).
9. **Requirements manager** — structured requirements doc that DAG verify nodes check against
   (closes the loop from "idea" to "proven").
10. **PDM-lite:** git-backed revisioning of `.step+` docs + release states (the DAG checkpoint
    store is 80% of this).
11. **Docs/compliance generation:** assembly instructions, user manuals, UL/CE/FCC checklist
    agent per product category.
12. **TraceParts API** (paid) for supplier-catalog CAD once volume justifies it; Werk24 for
    drawing/GD&T extraction.

## Suggested additional AI-powered tools (beyond your list)

DFM advisor already exists in intake — extend per-process. Add: generative-design/topology
optimization node (open-source SIMP on existing FEA), packaging/dunnage design from bounding
geometry, test-plan generator (turns requirements into physical test procedures + fixture
models), maintenance/BOM-spares advisor, and a "design review" agent that runs the full check
battery (interference, DFM, DRC, D/C, budget) and writes the review doc — each is just another
MCP tool family + DAG node type.

## License guardrails (pin to the wall)

| Bundle freely | Subprocess only (+ source offer) | Avoid |
|---|---|---|
| LangGraph, PyNite, Renode, probe-rs, ngspice, Verilator, openNURBS, ezdxf, BOLTS data (LGPL dist), IfcOpenShell/ifctester (LGPL, dynamic link), clangd, AISC DB, EPANET/WNTR, pandapipes, fluids/ht/thermo, CoolProp, OpenHydraulics (via FMU), fmpy | CalculiX, OpenFOAM, GHDL, Icarus, OpenOCD, esptool, Arduino CLI, LibreDWG | AGPL anything (Ladybug), GPL linking (FreeCAD Fasteners WB code, KiCad importer code — specs/data only), GrabCAD scraping, retailer scraping, bundling IBC/NDS text, OpenSees (UC license), Wokwi (proprietary) |
