# Session verification checklist — PERF phase (2026-07-10)

**When:** end of this session, before declaring the phase done.  
**How:** open the built app (`host/build-linux/bin/Billet`), run each item, mark PASS/FAIL.  
**Do not** re-baseline golden images/mesh hashes unless an item explicitly says so.

Machine: Linux host build. Prefer headed (`AI_CAD_SHOT_HEADED=1` or launch binary directly).

---

## Already shipped earlier (sanity only if time)

| # | Area | What to do | Expect | Pass? |
|---|------|------------|--------|-------|
| A1 | T-PERF1 PM live preview | Edit extrude depth in PropertyManager while watching viewport | Typing stays smooth; mesh may lag 1 beat then catch up; "Rebuilding…" chip may flash | |
| A2 | T-PERF1 feature tree | Suppress / reorder / copy a feature | No freeze; tree updates when apply completes | |
| A3 | T-PERF3 mesh load | Open a mid-size part / switch docs | Viewport keeps last mesh; new mesh appears without multi-second GL stall | |

---

## T-PERF2 — async latest-wins drag pipelines

| # | Path | What to do | Expect | Pass? |
|---|------|------------|--------|-------|
| B1 | 2D sketch drag | Enter sketch, drag a point under several constraints; move mouse fast | Cursor/UI ~60 fps; geometry trails slightly but never freezes the window | |
| B2 | 2D sketch release | Release mouse after a fast drag | Final pose matches last drag sample; relations still solve | |
| B3 | 3D sketch drag | Drag a 3D-sketch entity | Same: no GL freeze, last-solved preview | |
| B4 | Push-pull | Push-pull a face with continuous mouse motion | First sample may rebuild once; subsequent samples coalesce; no multi-second lockup | |
| B5 | Assembly drag | Drag a free component in an assembly | Component follows; mates re-solve async | |
| B6 | Explode slider | Drag explode factor rapidly | Slider smooth; explode factor tracks latest sample when RPCs catch up | |

---

## T-PERF4 — async tabs / files / long tools

| # | Path | What to do | Expect | Pass? |
|---|------|------------|--------|-------|
| C1 | Tab switch (cold) | Open 2+ docs; switch to a tab that needs rebuild | UI stays interactive; progress/busy indicator; mesh updates when ready | |
| C2 | File open | Open a non-trivial `.aicad` / STEP from recent or dialog | Progress toast; no freeze; document appears when ready | |
| C3 | File save | Save active doc | Progress toast; no freeze | |
| C4 | sim_mesh | Mesh a study in Simulate that used to take a long time | Panel shows busy; can still pan UI; mesh lands async | |
| C5 | Drawing HLR | Create/update a drawing view that triggers HLR | Drawing panel doesn't lock the whole app | |

---

## T-PERF5 — per-doc engine locking

| # | Path | What to do | Expect | Pass? |
|---|------|------------|--------|-------|
| D1 | Two docs | Open two parts; run a heavy op on A while lightly editing B | B remains responsive (no full global stall) | |
| D2 | Cross-doc | Project external refs / assembly that touches two docs | No deadlock; both docs consistent after ops settle | |
| D3 | Contention meter | Optional: check profiler/status if exposed | Meters move; no permanent lock | |

---

## T-PERF6 — kernel copy amplification

| # | Path | What to do | Expect | Pass? |
|---|------|------------|--------|-------|
| E1 | Import-heavy edit | Import a multi-MB STEP; edit a late feature repeatedly | Edit latency much lower than pre-fix (no multi-MB whole-doc copy per keystroke feel) | |
| E2 | Undo/redo | Undo/redo through several edits on that doc | Correct geometry; no crash | |
| E3 | Save/load | Save and reopen the import-heavy doc | Schema still loads; geometry matches | |

---

## T-PERF7 — owner-map + tess deflection

| # | Path | What to do | Expect | Pass? |
|---|------|------------|--------|-------|
| F1 | Owner map | Edit mid-history feature; pick faces after rebuild | Face/edge owners still correct (PM / selection) | |
| F2 | Tess size | Open a very large part (meters-scale) | Mesh triangle count reasonable (not millions from 0.05 mm abs); silhouette still looks OK | |
| F3 | Tess small | Small precision part (~10–50 mm) | Still meshed finely enough for fillets/holes | |
| F4 | Golden tests | If deflection changed: note which golden/mesh-hash tests need rebaseline | List failures here at verify time | |

---

## T-PERF8 — sketcher interactive mode

| # | Path | What to do | Expect | Pass? |
|---|------|------------|--------|-------|
| G1 | Drag under load | Dense sketch (many constraints); drag hard | Feels snappier than pre-interactive; no wrong final pose | |
| G2 | Release status | On mouse-up | Full diagnostics/status refresh once | |
| G3 | Oracle parity | Exact/non-interactive solve path unchanged | `scripts/gate.sh` / sketch parity still green | |

---

## T-PERF9 — sketch connected components

| # | Path | What to do | Expect | Pass? |
|---|------|------------|--------|-------|
| H1 | Independent profiles | Two separate closed profiles far apart; drag one | Other profile geometry stays put; only dragged component re-solves | |
| H2 | Coupled | Profiles linked by a dimension/relation | Still solve together | |
| H3 | Large sketch | 20+ independent holes/profiles | Drag stays interactive | |

---

## T-PERF10 — odds & ends

| # | Item | What to do | Expect | Pass? |
|---|------|------------|--------|-------|
| I1 | Auto-coincide scope | Edit one entity in a large sketch | Coincident proposals don't full-scan forever | |
| I2 | Fillet bisect | All-edge fillet that partially fails | Faster than O(E²) full rebuilds; sensible partial result | |
| I3 | Unify skip | Fuse-heavy model | No obvious regression; compile not slower | |
| I4 | AICM shared-mem | Rebuild loop with hostd | No extra temp-file storm if path enabled | |
| I5 | CEF lazy | Cold launch | Browser/CEF not forced before first web surface (if implemented) | |
| I6 | Idle throttle | Leave app idle focused | CPU lower than full vsync spin | |
| I7 | Profiler | `tools/profile_modeling.py` short run | Sample rate / symbols less broken than pre-fix | |
| I8 | Scrape pool | Parts live search; type quickly | Old scrapes cancel; current query wins | |

---

## Gate / build (agent responsibility — confirm green before handoff)

| # | Command / check | Result |
|---|-----------------|--------|
| J1 | `./build.sh` (host) | |
| J2 | `./build.sh core` + ctest if OCCT present | |
| J3 | `python3 scripts/check.py` | |
| J4 | `scripts/gate.sh` (or noted SKIPs) | |
| J5 | All T-PERF1..10 marked done on board / DAG | |

---

## Notes / failures during verify

_Use this space at end of session:_

```
(date/time)
item:
observed:
```
