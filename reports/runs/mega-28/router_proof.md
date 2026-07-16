# mega-28 router proof (E28)

**Axis:** E28 — C++ / Python autorouter honours min clearance (≥0.2 mm)  
**Baseline:** `271aedac` (mega-27 residual AC4: DRC errors=6)  
**Prover:** FatalGrouse (RouterFix)  
**No push** (Main gates).

---

## Root cause

Live path for `board_from_schematic` / `ecad_autoroute` is **Python**
`engine_oracle/ecad/autoroute.py` (not hostd for ECAD sessions). The Lee/A*
grid halo (`clearance + half_track_width` at **cell centres**) is correct for
orthogonal grid runs, but the post-path **45° corner chamfer**
(`_CORNER_CHAMFER_MM`, default was **0.5 mm**) trims the *inner* corner of an L
toward any pad the route just skirted. That systematically produced fixed
sub-clearance pad↔track gaps:

| Run | DRC clearance errors | Typical gap |
|---|---|---|
| mega-24..27 live AC4 | **6** | **0.1296 mm** < 0.2 mm |
| synthetic IC stress (chamfer 0.5) | **6** | **0.1548 mm** < 0.2 mm |

C++ `core/ecad/autoroute.cpp` never had the chamfer (already orthogonal-only)
and already stayed clean on the same stress board; parity still needed the
Python fix + shared `min_clearance` floor.

---

## Fix (minimal)

| File | Change |
|---|---|
| `engine_oracle/ecad/autoroute.py` | Default `AI_CAD_ROUTE_CHAMFER` **0** (was 0.5). Add `_CLEARANCE_FLOOR` from opts `min_clearance` / `min_clearance_mm` into `_clearance()`. |
| `core/ecad/autoroute.cpp` | `thread_local g_clearance_floor`; `clearance_of()` floors; `autoroute` / `route_diff_pair` honour opts `min_clearance` / `min_clearance_mm`. |
| `services/mcp/ecad_tools.py` | MCP `ecad_autoroute(..., min_clearance=0.0)` plumbs into opts. |
| `services/agent/agent_turn.py` | Force-dispatch keeps `min_clearance` / maps `min_clearance_mm` → `min_clearance` (no longer stripped). |
| `core/tests/test_autoroute.cpp` + `core/CMakeLists.txt` | ctest `autoroute` — IC stress board + two-net board assert DRC clearance errors == 0 with `min_clearance=0.2`. |

**Not weakened:** DRC rule stays 0.2 mm; no silence of residual_drc.

---

## Deterministic replay (before → after)

Fixture: two 0.5 mm-pitch QFN-like footprints on an **80×60 mm** outline
(same room class as mega-27 C27), interleaved nets `3V3/GND/N$2/N$9/...`,
track width 0.15, rule clearance 0.2. Replays the **same 6-error class** as
mega-27 AC4 under pre-fix chamfer.

| Phase | Chamfer | opts | clearance errors |
|---|---|---|---|
| **BEFORE** (pre-fix) | 0.5 | `pitch=0.25` | **6** |
| **AFTER** (Python) | 0.0 | `pitch=0.25, min_clearance=0.2` | **0** |
| **AFTER** retry ladder | 0.0 | `pitch=0.3, min_clearance_mm=0.2` | **0** |
| **AFTER** (C++ `libaicad` `ecad_autoroute` inline board) | n/a | `pitch=0.25, min_clearance=0.2` | **0** |

Machine JSON: `reports/runs/mega-28/router_proof.json`.

BEFORE sample messages (pad↔track, same net pattern as mega-27 AC4):

```
clearance 0.1548mm < required 0.2mm between t10 (net 'GND') and u2b0 (net '3V3')
clearance 0.1548mm < required 0.2mm between t13 (net 'N$2') and u1t1 (net 'N$9')
… (6 total)
```

AFTER: `clearance_errors=0`, nets still fully routed (`3V3,GND,N$2,N$9`).

mega-27 live residual for comparison (ledger / `criterion_AC4.json`):

```
errors=6  … clearance 0.1296mm < required 0.2mm between p11 and t32 (GND) …
```

---

## Gates

| Gate | Result |
|---|---|
| `core/build-occt-linux/bin/test_autoroute` (ctest NAME `autoroute`) | **2/2 PASS** (6 assertions) |
| Python `engine_oracle/ecad/test_autoroute.py` | **ALL AUTOROUTER TESTS PASSED** (incl. clearance / tight / via / terminal) |
| `./build.sh daemon` + `./build.sh stage` | **DONE** — staged to `services/dist/AI-CAD/` |
| Engine oracle lockstep | Python chamfer default + `min_clearance` floor mirrored in C++ `clearance_of` / opts |

### NEW hostd stamp

```
path    : /home/hunter/Repositories/CAD/services/dist/AI-CAD/aicad-hostd
version : aicad-hostd night2-3084-g271aedac-dirty
mtime   : 2026-07-15 21:09:14 -0400  (epoch 1784164154.6792681)
size    : 761744
md5     : 6b9c8502383cd964bfbfe38a5946728b
lib     : services/dist/AI-CAD/libaicad.so (same stage)
```

hostd self-drains on mtime flip per `docs/HOSTD.md` / `build.sh stage` contract.

## Verdict

| Metric | Value |
|---|---|
| before DRC clearance errors | **6** |
| after DRC clearance errors | **0** |
| E28 | **PASS** |
| Ready for MegaRun28 | **yes** — DM `router proven` + stamp above |

No push.
