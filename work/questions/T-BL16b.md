# T-BL16b — advisor questions

## Q1 (2026-07-06, opus-sweep): the task premise doesn't match the code — how should motion playback actually work?

Recon (agent, read-only) found the two load-bearing assumptions in T-BL16b's DoD are false in
the current ui/imgui tree, so the "small GUI follow-up" is actually a multi-part renderer+backend
change. Escalating rather than guessing.

**Finding 1 — no per-body transform in the viewport (contradicts the DoD hint).**
`ui/imgui/src/viewport/scene.h:56-65` `GBody` holds only VAO/VBO/colour/metallic/roughness — NO
model matrix. `Scene::loadAicm` (scene.cpp:166-199) uploads already-world-space vertices; component
poses are baked SERVER-SIDE into the AICM tessellation. The GUI's only way to move a body today is
to re-mutate the doc → re-solve → re-tessellate → reload the whole AICM (asm_panels.cpp:97-101).
So the DoD's "the assembly render path already applies components[].transform per body; reuse it per
frame" has nothing to reuse — there is no per-body matrix uniform.

**Finding 2 — `simulate_motion` is not reachable from the GUI.**
`simulate_motion` is a Python MCP-only composite (`services/mcp/cad_tools.py:3748` → `core_bridge.py`
ctypes). It is NOT in `engine/worker.py`'s `_METHODS` nor routed through `services/worker` →
`cpp_core`, so ui/imgui's `WorkerClient::call` cannot invoke it. But the DoD says BOTH "GUI obtains a
trajectory by calling simulate_motion" AND "no re-implementation of the solve/interference loop in
the GUI" — mutually exclusive as things stand. The GUI *can* already call the primitives
`doc_apply_ops`, `doc_solve_mates`, `doc_tree` (→ components[].transform), `eval_interference`
(confirmed live call sites), so a client-side sweep is technically possible but is exactly the
"re-implementation" the DoD forbids.

**Options (need a ruling on approach + scope — this is bigger than a P4 GUI tweak):**
- **(a) Expose `simulate_motion` to the worker seam** (add it to the C++ `cpp_core`/`WorkerClient`
  route, or a thin worker method that calls the same core primitives) + **(c)** add a per-`GBody`
  model matrix to the Scene renderer, driven client-side from the returned trajectory JSON (cheap,
  scrub-friendly). Honors "no GUI re-implementation" (the sweep stays in the backend) but is TWO real
  changes (backend RPC + renderer capability). RECOMMENDED.
- **(b) Client-side sweep in C++** using the reachable doc_* primitives + the per-body matrix. Fastest
  to build entirely in the GUI, but violates the DoD's "no re-implementation of the solve loop".
- **(d) Per-frame doc re-tessellation** (push each frame's transforms via doc_apply_ops → re-solve →
  reload AICM). No renderer change, uses reachable calls, but CANNOT scrub smoothly (a full
  tessellation per frame) — poor UX; probably a non-starter for "playback".

RECOMMENDED: (a)+(c). But it reclassifies T-BL16b from "P4 GUI follow-up" to a renderer+backend
feature — please confirm the approach + whether the scope/priority still fits, or split it
(e.g. "expose simulate_motion to the worker" as its own backend task, then the GUI playback on top).
Left T-BL16b at `doing`, unblocked parts none — awaiting this ruling.
