# Python Cutover Status (P5 tracker)

Accelerated strangler plan: each subsystem reaches C++ parity → routing flips →
Python module deleted in the same commit. Target: **zero Python** at N6b.

## Routing seam
- `services/worker/worker_router.{h,cpp}` — policy layer (new in N4a)
- `services/worker/worker_client.cpp` — production routing (env-gated)
- `core/api/libaicad.h` — flat C ABI (`aicad_call`)

## Subsystem matrix

| Subsystem | C++ location | Oracle | Routed to C++ | Python deleted |
|-----------|--------------|--------|---------------|----------------|
| doc session (new/open/save/undo/tree/rebuild) | `core/doc/` | `engine_oracle/doc/` | opt-in `AI_CAD_CPP_DOCS=1` | no |
| sketch solver | `core/sketch/` | `engine_oracle/doc/` | partial | no |
| geom features (extrude/revolve/boolean/…) | `core/geom/` | `engine_oracle/doc/features/` | partial (box/cylinder/washer) | no |
| sim (FEA/CFD/SPICE) | `core/sim/` | `engine_oracle/sim/` | opt-in `AI_CAD_CPP_SIM` | no |
| ECAD | `core/ecad/` | `engine_oracle/ecad/` | opt-in `AI_CAD_CPP_ECAD` | no |
| CAM | `core/cam/` | — | MCP tools only | no |
| agent loop | `core/agent/` | `services/agent/` | opt-in `AI_CAD_CPP_AGENT` | no |
| MCP server | — | `services/mcp/` | n/a (thin client target) | no |
| formats/drawing/export | `core/doc/` | `engine_oracle/doc/` | partial | no |

## Next cutover steps (ordered)
1. Complete canonical schema mapping (extrude mode add/cut, revolve axis, boolean targets) — see `docs/CPP_MIGRATION.md` Step 2.5.
2. Flip `AI_CAD_CPP_DOCS` default ON once m1 headless workflow is green on C++ only.
3. Delete `engine_oracle/doc/` feature modules as each passes `core/tests` + corpus parity.
4. Port agent to C++ (`core/agent/`) or keep `services/agent` as thin libaicad client.
5. Delete `engine_oracle/` entirely (N6b).

## Verification
- `bash scripts/gate.sh` — core ctest + ui/abstract test
- `ctest --test-dir core/build-linux` — 41+ doctest suites
- Future: restore `scripts/check.py` as hard gate once ribbon parity closes
