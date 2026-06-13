# AI-ECAD — Build Status (autonomous session 2026-06-13)

> Companion to `docs/ECAD_PLAN.md`. What got built, what's verified, how to run it, and what's
> left. Branch: `claude/epic-gould-fa9964` (isolated worktree). 6 commits, M0→Wave D.

## TL;DR

A **complete, tested, headless electronics-design engine** now lives in `engine/ecad/` +
`engine/sim/`, exposed via worker RPC and AI/MCP tools, with a C++ GUI scaffold staged for
integration. You can build a schematic, generate a netlist, run ERC, **simulate it** (native MNA —
matches closed-form to ~1e-6), lay out a PCB, run DRC, export Gerber/drill/BOM, and produce a
**STEP solid that imports into the CAD app to design an enclosure** — all verified by tests.

Run the demo to see the whole pipeline:
```
& "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" -m engine.ecad.demo
```

## What's done & verified (Python — all green)

| Area | Modules | Verified by |
|---|---|---|
| Schema + transforms | `engine/ecad/schema.py` | self-validating factories; pin/pad world math |
| Ops + session | `ops.py` (33 ops), `session.py` (undo/redo, lazy compile) | `test_ops` (15) |
| Connectivity | `netlist.py` (deterministic union-find), `erc.py` (full catalog) | `test_netlist` (7) |
| Library | `library/` sexpr + KiCad `.kicad_sym`/`.kicad_mod` import + native resolver + 25-sym/19-fp clean-room **starter lib** | `test_library` (9) |
| Simulator | `engine/sim/circuit.py` + `devices.py` — MNA op/dc/tran/ac | `test_circuit` — **all 8 analytic gates** (divider exact, RC τ 0.001%, RLC ring 0.006%, op-amp gain exact, RC −3 dB/−20 dB-dec) |
| SPICE I/O | `engine/sim/spice.py` parse/serialize + netlist→circuit bridge | `test_spice` |
| DRC | `engine/ecad/drc.py` — net-aware clearance/width/via/annular/hole/edge + ratsnest MST | `test_drc` (11) |
| Fabrication | `engine/ecad/fab/` — Gerber RS-274X, Excellon, BOM, pick-and-place | `test_fab` |
| 3D bridge | `engine/ecad/threed.py` — board→STEP + `aicad.part/1` import doc | `test_threed` — board volume exact, part doc **compiles in the CAD pipeline** |
| Worker RPC | `engine/ecad/rpc.py` — 13 `ecad_*` methods (fenced into `engine/worker.py`) | `test_rpc` (5); legacy `test_worker_doc` still green |
| AI tools | `mcp_server/ecad_tools.py` — 13 tools w/ rich context helpers | `test_ecad_tools`; legacy `test_roundtrip` still green |
| Full pipeline | `engine/ecad/test_integration.py` + `demo.py` | end-to-end green |

**Regression:** the 5 shared/legacy suites touched by fenced appends remain green. No edits to
`engine/doc/*`. All new work is additive and isolated.

## Key decisions made autonomously (⚑ = please confirm)

1. ⚑ **Clean-room, GPL-clean.** No KiCad source copied (KiCad is GPLv3); we parse its file formats
   and import the user's own libraries. Locked in `docs/contracts/licenses.md`.
2. ⚑ **Native MNA simulator instead of ngspice** — satisfies "improve the simulations" and stays
   GPL-clean. ngspice remains a documented, subprocess-isolated *optional* backend (not built).
3. **Mirrors `engine/doc` architecture** exactly (schema/ops/session/compile/RPC/tools/PM-forms).
4. **C++ UI not build-verified** — see below.

## C++ GUI (Wave D — scaffold, NOT build-verified)

`host/src/ui/ecad/` (mirror + 2D canvas + dockable panel) is written correct-by-construction against
the real host headers but **was not compiled** (this environment has no FetchContent'd ImGui/GLFW,
and the parallel CAD agent owns `host/` build files). Integration is deliberately deferred to a
guide: **`docs/ECAD_UI_INTEGRATION.md`** has the exact fenced snippets for `CMakeLists.txt`,
`app.cpp`, `title_bar.cpp`, `ribbon_def.cpp`, the PM-form entries, and the worker-RPC call pattern.
Expect minor ImGui-signature fixups on first real build.

## Remaining work / known debt (prioritized)

1. **Wire + build the C++ UI** — apply `ECAD_UI_INTEGRATION.md` steps (a)–(c) for an empty-state
   render, then (d)–(f) for live docs + PM forms + picking. First build will need small fixups.
2. **`pcb_update_from_sch`** is a stub — the netlist-driven back-annotation (add missing footprints,
   rebind pad nets, report removed parts) needs both docs; implement at the session/worker layer.
3. **`sch_import_kicad`** is a stub — needs a `.kicad_sch` importer (the `.kicad_sym`/`.kicad_mod`
   importers already exist in `library/`).
4. **`.ecad+` container** (`engine/ecad/formats.py`) not built — `ecad_save/open` fall back to plain
   JSON. Mirror `engine/doc/stepplus.py` (ZIP: document.json + netlist + thumbnail).
5. **Simulator gaps**: MOSFET level-1 AC + voltage-controlled switch are registered stubs;
   `.subckt` (NE555) expansion not done; sim core returns empty traces if `study.probes` is empty
   (the AI tool layer defaults to all nodes — consider doing the same in the core).
6. **Bus routing** is parse-only (members expand; bus-entry merges into nets are a follow-up).
7. **Gerber extensions** have cosmetic quirks (`B.Fab→.GBP2`, `F.Fab→.GGP`); normalize to the
   conventional set. Copper-region realism in `threed.py` is deferred (proxies + substrate only).
8. **Agent orchestrator** integration — the ECAD tools are ready for the M9-style orchestrator; an
   ECAD design loop (intake→place→wire→ERC→sim→fix) can reuse the CAD loop's feedback discipline.

## How to run everything
```
$py = "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe"
# full demo:
& $py -m engine.ecad.demo
# all suites:
foreach ($m in 'engine.ecad.test_ops','engine.ecad.test_netlist','engine.ecad.test_library',
  'engine.ecad.test_drc','engine.ecad.test_fab','engine.ecad.test_threed','engine.ecad.test_rpc',
  'engine.ecad.test_integration','engine.sim.test_circuit','engine.sim.test_spice',
  'mcp_server.test_ecad_tools') { & $py -m $m }
```
