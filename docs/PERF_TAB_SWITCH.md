# Doc-tab switch latency — measured numbers (2026-07-14)

Owner ask for Cycle 8: produce real before/after-style tab-switch timing numbers
using the already-landed `logTabPerf` instrumentation in
`host/src/core/doc_tabs.cpp` (`AppState::switchToTabCore`).

| Field | Value |
|---|---|
| **Date** | 2026-07-14 (UTC 2026-07-15T01:17Z) |
| **Baseline commit (cycle)** | `1e92db50` |
| **Tree measured** | `c8c643b0` + local measurement harness (`AI_CAD_SHOT=tabperf`) |
| **Host binary** | `host/build-linux/bin/Billet` (rebuilt for the harness) |
| **Machine** | Linux 6.19 / Ryzen 5 7600X3D / RTX 3080 Ti (shared multi-agent box) |

## What was measured

**Seam:** `AppState::switchToTab` → `switchToTabCore` (the exact path the
doc-tab strip uses when the user clicks a tab — see deferred `s_switchTo` in
`host/src/ui/doc_tabs.cpp` calling `st.switchToTab`).

**Path observed:** all samples were `path=gpu-cache` (warm A↔B re-entry after
both parts already have an immutable session + GPU mesh cache). No
`async-rebuild` / `pushed-async` samples in this run — those only fire on a
cold or invalidated tab.

**Metric:** wall time inside `switchToTabCore` from entry to the
`[perf] tab_switch … total=…ms` log line (`std::chrono::steady_clock`).
Also records the mesh-bind sub-interval (`mesh=…ms`).

### GUI-click path (headless)

**Could not drive a real mouse-click tab strip headless.** Xvfb +
`--ui-screenshot` has no synthetic click harness for the doc-tab strip.
Instead the measurement scenario calls `st.switchToTab(idx)` directly —
**the same C++ function the strip click path invokes after the ImGui button
defers**. So this is the full switch body (park session, reseat subsystems,
GPU-cache bind, camera restore, log), **not** ImGui hit-testing / input
latency.

Not measured: MCP `doc_switch` (no such RPC — worker only has
`DocMirror::activate` / `doc_rebuild`; the host tab switch is GUI-side).

## Method

1. Added a measurement-only scenario `AI_CAD_SHOT=tabperf` in
   `host/src/ui/scenarios.cpp` (+ flags in `app_state.h` / `app.cpp`):
   - `new_part` A → box 60×40×10 → `addDocTab`
   - `new_part` B → box 80×50×20 → `addDocTab` (parks A's session + GPU cache)
   - 24 alternating `switchToTab(A)` / `switchToTab(B)` with 2 settle frames
     between switches
2. Ran headless:

```bash
AI_CAD_SHOT=tabperf AI_CAD_REPO="$(pwd)" \
  timeout 120 xvfb-run -a ./host/build-linux/bin/Billet \
  --ui-screenshot scratch/tabperf.png
```

3. Harvested `[perf] tab_switch …` lines (emitted by `logTabPerf` via
   `st.addLog` / stderr) and computed median / p95 on the `total=` field.

Reproduce anytime with the same command after a host build.

## Results (n = 24 warm A↔B switches)

All 24 switches: `path=gpu-cache`.

| metric | total (ms) | mesh bind (ms) |
|---|---:|---:|
| **min** | 0.0713 | 0.0668 |
| **median** | **0.0807** | **0.0756** |
| **mean** | 0.0857 | 0.0806 |
| **p95** | **0.1093** | **0.1036** |
| **max** | 0.1336 | 0.1241 |

Sorted `total=` samples (ms):

```
0.0713 0.0717 0.0736 0.0753 0.0759 0.0759 0.0760 0.0770
0.0795 0.0796 0.0797 0.0807 0.0808 0.0817 0.0821 0.0824
0.0831 0.0920 0.0921 0.0951 0.1028 0.1061 0.1099 0.1336
```

### Read of the numbers

- Warm doc-tab switch on two simple box parts is **sub-millisecond** on this
  box (~0.08 ms median / ~0.11 ms p95). Almost all of that is the mesh bind
  (`mesh ≈ total`).
- This is the **after** number for the T-PERF tab-switch work (local GPU
  cache + park/bind, no worker round-trip on an unchanged parked tab). There
  is no pre-instrumentation baseline captured in-repo for the old sync
  `doc_rebuild` path; historically that path was multi-millisecond to
  multi-second under worker load (see `docs/PERF_AUDIT_2026-07-10.md` §P7/P8
  and T-PERF4). Treat these figures as the **current floor** for warm
  A↔B switches, not a controlled A/B pair from the same harness.

### Cold / rebuild path (not exercised here)

`switchToTabCore` also logs:

- `path=async-rebuild shell=…ms` — shell UI switch only; rebuild drains async
  (`[perf] tab_switch_rebuild … total=…ms` on completion)
- `path=pushed-async shell=…ms` — foreign push awaiting prewarm

Those need a deliberate cache-invalidate or first-open scenario; out of scope
for this warm A↔B ask.

## Harness files (measurement only)

| file | change |
|---|---|
| `host/src/core/app_state.h` | `wantTabPerf` / `tabPerfShotDone` flags |
| `host/src/app.cpp` | `AI_CAD_SHOT=tabperf` wiring |
| `host/src/ui/scenarios.h` | `runTabPerf` + state |
| `host/src/ui/scenarios.cpp` | scenario body + `endFrame` keep-alive |

No change to `switchToTabCore` / `logTabPerf` behavior beyond what was already
landed. No git push from this agent.
