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

## C++ GUI (Waves N–R — BUILT + headless-verified, interactive)

`host/src/ui/ecad/` (mirror + 2D canvas + dockable panel) is **compiled into `AI-CAD.exe` (VS2022)
and verified headlessly** (`AI_CAD_SHOT=ecad`, kinds `sch`/`pcb`/`3d`/`ai`/`editsch`/`select`/`del`).
Wired into the dockspace (tabbed with the Viewport), the draw loop, a **"PCB" ribbon tab**, and the
dispatcher. It is now an interactive editor:
- **AI launchpad (N/P):** the empty editor takes a natural-language request → `ecad_design` → a
  verified schematic loads in the canvas. "AI Design" leads the ribbon's New group.
- **Create/analyze (N/F):** New Schematic/New PCB; Autoroute / Fill Zones / ERC / DRC / Gerber /
  Drill / BOM operate on the current doc.
- **Seamless PCB→3D (O):** "Board to 3D" exports the board STEP and imports it into a live CAD part
  shown in the 3D viewport — design a housing around it. (The headline integration.)
- **Interactive editing (Q/R):** a Select/Place/Route toolbar + part picker. Place drops a symbol/
  footprint at the grid-snapped click; Route adds wire/track segments; Select hit-tests + highlights;
  Delete removes; drag moves a symbol/footprint. Edits emit ecad ops via `ecadApplyOps`.
- **Verified renders (dark + light themes):** schematic (symbols/pins/net-labels/Nets+Symbols panel/
  ERC strip + selection halo) and PCB (outline/footprints+pads/tracks/copper-pour/Layers panel/DRC).

Build: `cmake -S host -B host/build -G "Visual Studio 17 2022" -A x64 && cmake --build host/build
--config Release --target AI-CAD`. Shot: `AI-CAD.exe --ui-screenshot out.png` with `AI_CAD_SHOT=ecad`.
`docs/ECAD_UI_INTEGRATION.md` documents the wiring. NOTE: the worker runs STAGED app code at
`%APPDATA%\AI-CAD\app` — engine edits need re-staging to reach a running GUI.

- **Simulation in the GUI (S):** a Simulate button + Transient/AC selector runs the native solver
  (`ecad_simulate` RPC) and plots V/I traces (transient) or a Bode magnitude rolloff (AC) in the
  editor's Simulation tab.
- **Undo/redo in the GUI (T):** toolbar Undo/Redo buttons + Ctrl+Z / Ctrl+Y (focus-gated to the ECAD
  window so they never collide with the CAD doc's history) drive the worker's `ecad_undo`/`ecad_redo`.
- **One-click AI board (U):** an "AI Board" launchpad button chains the whole flow — `ecad_design`
  (schematic) → `ecad_new` pcb → `ecad_update_from_sch` (populate footprints + bound nets) →
  auto-sized rectangular `pcb_set_outline` (bbox over all pads + 5mm margin) → `ecad_autoroute` →
  load the routed board into the editor. AI-designed schematics now carry footprints
  (`_DEFAULT_FOOTPRINTS` in `orchestrator.py`) so the board populates with real parts. Verified via
  `AI_CAD_ECAD_KIND=aiboard` (routed divider board: 2× R_0603 + outline + track). Hands off directly
  to **Board to 3D** for enclosure design — closing the schematic→PCB→3D→housing loop.
- **Property inspector (V):** selecting a symbol/footprint (Select tool) shows an inline Ref + Value
  editor on its own toolbar row; Apply / Enter commits via `sch_edit_symbol` / `pcb_edit_footprint`.
  Buffers reload on selection change so edits never bleed between parts. Verified in
  `AI_CAD_ECAD_KIND=select` (R1 / 10k populate the fields) + headless op round-trip.
- **Rotate / flip (W):** the selected part rotates +90° (R key or the inspector's Rotate button) and
  flips (F key / Flip button — `mirror` toggle for symbols, `side` top/bottom for footprints), via
  `sch_edit_symbol` / `pcb_edit_footprint`. Keys are gated on canvas-hover + no text focus so they
  never fire while typing a value. Verified `AI_CAD_ECAD_KIND=rotate` (R1 turns vertical, R2 stays
  horizontal).
- **Full AI→3D chain (X):** the whole vision composes in one flow — a natural-language request becomes
  an AI-designed schematic → populated + routed PCB → a 3D board solid (substrate + component proxies)
  in the CAD viewport, ready to design a housing around. Verified `AI_CAD_ECAD_KIND=aiboard3d` (AI
  Board then Board-to-3D back to back; the 3D viewport shows the imported board part). This is the
  end-to-end "design PCBs → 3D models → housing, with AI throughout" goal, demonstrated headlessly.
- **Save / Open (Y):** the PCB ribbon's Save/Open buttons now persist + reload native `.ecad+`
  containers (Open also imports `.kicad_sch`/`.kicad_pcb`) via native Win32 dialogs →
  `ecad_save`/`ecad_open`; the opened doc adopts into the editor. Fixed a real engine bug found here:
  `formats.open` embedded raw `bytes` (thumbnail / unknown ZIP entries) in the doc, which raised
  `TypeError: Object of type bytes is not JSON serializable` when the worker emitted the ecad_open
  response — now base64-encoded in `_extra_entries` (decoded on save). Verified `AI_CAD_ECAD_KIND=
  saveopen` (design → save .ecad+ → reopen; symbols 3→3 round-trip, response JSON-clean).
- **Zoom to violation (Z):** clicking an ERC/DRC row in the results strip pans + zooms the canvas to
  the violation's location and flashes a pulsing crosshair marker there (`EcadCanvas::focusOn`). Rows
  with a location show a "Click to zoom to (x, y)" tooltip. Verified `AI_CAD_ECAD_KIND=violzoom`
  (marker lands on the unconnected pin of a floating-net ERC error).

**GUI follow-ups (not blocking):** full PropertyManager forms for the remaining ECAD op fields
(footprint reassignment / DNP / sim-params — Ref/Value + rotate/flip are DONE); move the editor
mirror onto AppState for multi-doc.

## Remaining work / known debt (prioritized)

1. **GUI editor polish** — place/route/select/delete/move + AI launchpad + PCB→3D bridge +
   simulation plot (transient & AC) + undo/redo + one-click AI→routed board + Ref/Value property
   inspector + rotate/flip + native Save/Open + zoom-to-violation are DONE (Waves N–Z); remaining:
   PM forms for footprint reassignment + per-field DNP/sim-params, multi-doc.
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
