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

## T-PERF10 — odds & ends (incl. deferred micro-items shipped this pass)

| # | Item | What to do | Expect | Pass? |
|---|------|------------|--------|-------|
| I1 | Auto-coincide scope | Edit one entity in a large sketch | Coincident proposals don't full-scan forever | |
| I2 | Fillet bisect | All-edge fillet that partially fails | Faster than O(E²) full rebuilds; sensible partial result | |
| I3 | Unify skip | Fuse-heavy model | No obvious regression; compile not slower | |
| I4 | **AICM shared-mem** | Edit a part; watch `/dev/shm/aicad_*` (or rebuild loop) | `tess.aicm_path` is `aicm-shm://…`; mesh still appears; no `/tmp/aicad_cpp_docs` storm; close doc frees shm | **PASS** (agent lifecycle; user trusts) |
| I5 | **CEF** | Vendor browser / sign-in | CEF boots **before GLFW** (eager default). Lazy deferred — post-GLFW init crashed vendor open (SIGABRT). `AI_CAD_CEF_LAZY=1` experimental only. | **agent: eager restored**; mesh-loader join on exit |
| I6 | Idle throttle | Leave app idle focused | CPU lower than full vsync spin | **PASS** (user vibe check) |
| I7 | **Profiler** | `python3 tools/profile_modeling.py --workload fillet --seconds 3 --hz 1000` | ~thousands of samples (not ~76/10s); hot frames are OCCT/geom (not only `sample_handler`); names under `aicad!…` where unstripped | **PASS** (agent) ~2.9k/3s + ~2.0k/2s reconfirm; OCCT hotspots |
| I8 | Scrape pool | Parts live search; type quickly | Old scrapes cancel; current query wins | |

---

## Gate / build (agent responsibility — confirm green before handoff)

| # | Command / check | Result |
|---|-----------------|--------|
| J1 | `./build.sh` (host) + `cmake --build core/build-occt-linux` + `./build.sh stage` | **PASS** (joint re-verify 2026-07-10) |
| J2 | ctest aicm/document/overlay/sketch* on build-occt-linux | **PASS** 12/12 |
| J3 | `python3 scripts/check.py` | **PARTIAL** — pre-existing `feature_surface_audit` sketch3d ribbon FAIL (not from PERF) |
| J4 | AICM shm lifecycle (agent) | **PASS** — `aicm-shm:///aicad_doc-…_r1`; mid shm=10 after 5 boxed docs; after `doc_close` shm=0; **0 new** `/tmp/aicad_cpp_docs` files (cold-open/field/overlay also on handoff) |
| J5 | Profiler sample (agent) | **PASS** — ~2.9k / 3s @ 1 kHz + reconfirm ~2.0k / 2s; hotspots OCCT/geom not `sample_handler` |
| J6 | Headless `AI_CAD_SHOT=m1` smoke | **PASS** (eager CEF again — expect `[cef] calling CefInitialize` at cold start) |
| J7 | All T-PERF1..10 + deferred micro-items | **YES** (only remaining optional: out-of-line embedded_brep store — schema/PLM) |

### Joint headed walk (you + agent)

| # | What you do | Expect | Pass? |
|---|-------------|--------|-------|
| H-I4 / H-I4b | AICM shm + mesh + close frees | Agent lifecycle; user trusts | **PASS** (agent; user trusts) |
| H-I5 | Parts → vendor browser sign-in | Was crash (lazy CEF after GLFW). Eager CEF restored + mesh-loader join | **fix shipped** — re-try login |
| H-A1 | PM extrude depth type-while-watch | UI stays live; mesh catches up | |
| H-B1 | Evaluate → Performance → Profiler open | Labeled **FPS** in window header + top-right overlay | **shipped** |
| H-I6 | Leave idle focused | Idle CPU vibe OK | **PASS** (user) |

### Notes from joint walk (2026-07-10)

```
H-I5 FAIL (user): opening vendor browser to sign in crashed app (SIGABRT,
  "terminate called without an active exception" / std::thread::~thread on exit).
  Root: CEF lazy-after-GLFW not viable; also mesh loader never joined on teardown.
  Fix: eager CefInitialize before run_gui; AppState::stopMeshLoader() on exit.
H-B1 request: FPS counter when Performance profiler open + labeled — shipped.
H-I4/I4b/I6: user trusts agent / vibe pass.
```

### Still intentionally deferred (not phase blockers)

- T-PERF6(b): out-of-line content-addressed `embedded_brep` store (save/load/PLM schema) — large schema change, not requested this pass.

---

## Notes / failures during verify

_Use this space at end of session:_

```
(date/time)
item:
observed:
```
