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
| Sim depth | `.subckt` + built-in **NE555** & op-amp macromodel; `.step` sweep; **Fourier/THD, Monte-Carlo, temp sweep, thermal noise** (`.four/.mc/.temp/.noise`); **`.tf`** (gain/Rin/Rout); **MOSFET-L1 AC**; default-all-nodes probes | `test_simdepth`, `test_simdepth2`, `test_tf_mosfet` |
| SPICE I/O | `engine/sim/spice.py` parse/serialize + netlist→circuit bridge | `test_spice` |
| DRC | `engine/ecad/drc.py` — net-aware clearance/width/via/annular/hole/edge + ratsnest MST | `test_drc` (11) |
| Zone fill | `engine/ecad/zonefill.py` — copper pours w/ clearance + thermal relief (grid raster, G36-safe) | `test_zonefill` (9) |
| Autoroute | `engine/ecad/autoroute.py` — 2-layer Lee/maze router w/ vias, DRC-clean | `test_autoroute` (8) |
| Fabrication | `engine/ecad/fab/` — Gerber RS-274X, Excellon, BOM, pick-and-place | `test_fab` |
| Interop / persistence | `engine/ecad/formats.py` — native `.ecad+` ZIP container + KiCad `.kicad_sch`/`.kicad_pcb` import | `test_formats` (7) |
| 3D bridge | `engine/ecad/threed.py` — board→STEP + `aicad.part/1` import doc | `test_threed` — board volume exact, part doc **compiles in the CAD pipeline** |
| Sch→PCB sync | `engine/ecad/sync.py` — `update_board_from_sch` (footprint resolve + pad-net bind, idempotent); RPC `ecad_update_from_sch` | `test_sync` |
| Worker RPC | `engine/ecad/rpc.py` — 16 `ecad_*` methods (fenced into `engine/worker.py`) | `test_rpc`; legacy `test_worker_doc` still green |
| AI tools | `mcp_server/ecad_tools.py` — 19 tools w/ rich context helpers (place/wire/status/sim/sweep/autoroute/fill/sync + **`ecad_design`** = full orchestrator) | `test_ecad_tools`; legacy `test_roundtrip` still green |
| **AI orchestrator** | `engine/ecad/orchestrator.py` + `recipes.py` — NL request → verified schematic (plan→build→ERC→sim→fix), recipe fallback for weak models; RPC + MCP `ecad_design` | `test_orchestrator` |
| Full pipeline | `engine/ecad/test_integration.py` + `demo.py` (11-step: AI design→sim suite→PCB→sync→autoroute→pour→DRC→fab→STEP→.ecad+→KiCad import) + `docs/ECAD_USERGUIDE.md` | end-to-end green |

**27 test suites green** (25 ECAD/sim/mcp + 2 legacy) as of Wave M. `python -m engine.ecad.demo` runs the full pipeline.

**Waves L–M additions:** autorouter v2 (**rip-up-and-reroute** + **differential-pair routing** w/ length-match), **hierarchical multi-sheet** schematics (sheet symbols ↔ child hier-labels; flat netlist byte-identical), **DFM** manufacturability checks (`dfm.py`: silk-over-pad, mask sliver, acid trap, courtyard overlap, drill/annular minimums), **signal integrity** (controlled-impedance calc + transmission-line sim), simulator **transformers** (coupled inductors `K`) + **behavioral `B`-sources** (safe-AST V/I expressions). 24 AI tools incl. `ecad_run_dfm`, `ecad_check_diff_impedance`, diff-pair autoroute.

**Regression:** the 5 shared/legacy suites touched by fenced appends remain green. No edits to
`engine/doc/*`. All new work is additive and isolated.

## Key decisions made autonomously (⚑ = please confirm)

1. ⚑ **Clean-room, GPL-clean.** No KiCad source copied (KiCad is GPLv3); we parse its file formats
   and import the user's own libraries. Locked in `docs/contracts/licenses.md`.
2. ⚑ **Native MNA simulator instead of ngspice** — satisfies "improve the simulations" and stays
   GPL-clean. ngspice remains a documented, subprocess-isolated *optional* backend (not built).
3. **Mirrors `engine/doc` architecture** exactly (schema/ops/session/compile/RPC/tools/PM-forms).

## C++ GUI (Wave N — BUILT + headless-verified)

`host/src/ui/ecad/` (mirror + 2D canvas + dockable panel) is now **compiled into `AI-CAD.exe`
(VS2022) and verified headlessly** via `AI_CAD_SHOT=ecad` (+ `AI_CAD_ECAD_KIND=pcb`). It is wired
into the dockspace (tabbed with the Viewport), the draw loop, a new **"PCB" ribbon tab** (New/
Schematic/Board/Outputs), and the dispatcher (`ecad_*`/`sch_*`/`pcb_*` → `ecadDispatch` → worker
RPCs). New Schematic/New PCB create live docs; Autoroute/Fill Zones/ERC-DRC/Gerber/Drill/BOM/Board-
to-3D operate on the current doc. **Verified renders (dark + light themes):** schematic (symbols,
pins, net labels, Nets/Symbols panel, ERC strip) and PCB (board outline, footprints + pads, tracks,
copper pour, per-layer Layers panel, DRC strip). `docs/ECAD_UI_INTEGRATION.md` documents the wiring.

Build: `cmake -S host -B host/build -G "Visual Studio 17 2022" -A x64 && cmake --build host/build
--config Release --target AI-CAD`. Shot: `AI-CAD.exe --ui-screenshot out.png` with `AI_CAD_SHOT=ecad`.

**GUI follow-ups (not blocking):** interactive canvas placement (click-to-place symbol/footprint via
`ecad_resolve_pick` + PM forms — the ops/RPC already exist; only the canvas-click → op wiring + the
`PMFieldKind` additions in `docs/ECAD_UI_INTEGRATION.md §f` remain); move the editor mirror onto
AppState for undo/redo + multi-doc; `ecad_open` file dialog.

## Remaining work / known debt (prioritized)

1. **Interactive ECAD editing in the GUI** — placement/routing is currently driven by the worker/AI
   (ribbon New + the orchestrator); add canvas click-to-place + PM forms for fully manual editing
   (engine ops + RPC already exist; see the GUI follow-ups above).
2. **Full bus routing** (electrical): member labels already net correctly (KiCad's bus is largely a
   drawing convenience over labels); explicit bus-wire + bus-entry tap objects are a follow-up.
3. **Advanced routing**: push-and-shove (router is eviction-based rip-up + maze, not negotiated-
   congestion); single-net length matching (diff-pair length-match is DONE); blind/buried vias.
4. **Simulator**: voltage-controlled-switch AC stub; pole-zero/distortion analyses; lossy
   transmission line (LTRA — lossless `T` is DONE). 
5. **Cosmetic/realism**: copper-region realism in `threed.py` (proxies + substrate only); zone
   island removal is area-thresholded (not width-based necking).

**Resolved across Waves E–N:** ~~the C++ ECAD GUI~~ (built + verified, both themes),
~~pcb_update_from_sch~~, ~~.ecad+ container~~, ~~KiCad sch/pcb import~~,
~~KiCad file-open~~, ~~zone fill~~, ~~autoroute~~ + ~~rip-up~~ + ~~differential pairs/length-match~~,
~~hierarchical sheets~~, ~~DFM checks~~, ~~signal integrity (impedance + transmission lines)~~,
~~transformers (K)~~, ~~behavioral B-sources~~, ~~`.subckt`/NE555/`.step`/default-probes~~,
~~Fourier/THD~~, ~~Monte-Carlo~~, ~~temp sweep~~, ~~noise~~, ~~`.tf`~~, ~~MOSFET-L1 AC~~,
~~AI design orchestrator + `ecad_design`~~, ~~Gerber extensions~~ — all DONE & tested.

## How to run everything
```
$py = "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe"
# full demo:
& $py -m engine.ecad.demo
# all suites:
foreach ($m in 'engine.ecad.test_ops','engine.ecad.test_netlist','engine.ecad.test_library',
  'engine.ecad.test_drc','engine.ecad.test_fab','engine.ecad.test_threed','engine.ecad.test_rpc',
  'engine.ecad.test_sync','engine.ecad.test_zonefill','engine.ecad.test_autoroute',
  'engine.ecad.test_formats','engine.ecad.test_integration','engine.sim.test_circuit',
  'engine.sim.test_spice','engine.sim.test_simdepth','mcp_server.test_ecad_tools') { & $py -m $m }
```
