# core/cam — CAM toolpaths, post-processing and stock simulation

Scope: core/cam/**, core/api/libchudware.cpp, core/tests/test_cam.cpp; services/mcp/cam_tools.py
Contracts: contracts/cam.md (simulation/audit/machine/job-diff shapes); core/cam/session.h declares the native job RPCs.
Canonical guidance: .claude/skills/chudware-dev/SKILL.md (manufacturing safety, real toolpath evidence).

## Entry points
- `core/cam/session.{h,cpp}` — stateful `cam_new`, `cam_apply_ops`, `cam_generate`,
  `cam_simulate`, `cam_post`; `generate_setup` builds/caches setup paths and warnings.
- `core/cam/operations.{h,cpp}` `generate` — operation + resolved geometry/tool/setup
  → toolpath via the individual milling/turning/cutting generators.
- `core/cam/model.h` — JSON job/setup/operation/tool/stock/WCS/toolpath model.
- `core/cam/post.{h,cpp}` `post_setup` — toolpaths → dialect-specific G-code.
- `core/cam/stock_sim.{h,cpp}` `simulate_setup` — Z-dexel heightfield stock removal,
  gouge, rapid-through-stock and optional holder/shank checks.
- `core/cam/feeds.{h,cpp}` — cutting and machine databases, feeds/speeds resolution.
- `core/api/libchudware.cpp` `cam_program_from_part` — native `cam_program` fast path
  over a real part: job creation → operations → generation → simulation → post.
- `services/mcp/cam_tools.py` — agent-facing job tools and real-part geometry extraction.

## How it connects
CAM generators are OCCT-free numerics/JSON; geometry is passed as regions, paths,
drill points or meshes. The extraction/API layer bridges real document geometry.
The native session holds the job and generated paths; MCP tools use `core_bridge`
to the same engine. Generation warnings propagate to the simulation/post outputs,
not a fabricated complete NC program.

## Debugging playbook
- Integrator build: `./build.sh core`; tests: `ctest --test-dir core/build-linux -R cam --output-on-failure`.
  A configured `core/build-occt-linux` tree also works. OCCT is not required by the
  CAM generator library itself; do not create a parallel build tree per worker.
- `feeds.cpp` can fall back to aluminum when a material id is not resolved. Trace the
  MCP material-to-cutting-group mapping before interpreting feeds/speeds.
- Post comments must be escaped for the selected dialect. Keep posts clean-room;
  do not copy vendor post code.
- Stock checks are sampled heightfield evidence, not exact B-rep collision proof.
  Inspect `SimResult.remaining`, warning fields and enabled collision checks;
  holder checking is off by default and needs tool geometry.
- CAM/Manufacture icons and reviews render actual `cam_simulate` paths over stock.
  An unrenderable operation is a reportable CAM defect, never a fake icon.

## Graphify starters
- graphify query "cam_generate toolpath generate_setup"
- graphify explain "Operation"
- graphify path "cam_program_from_part" "post_setup"

## Related tasks/docs
- docs/cam_samples/ — reference CAM models
- contracts/cam.md — frozen CAM evidence shapes
- work/context/services-mcp.md — model-facing tool registry
- work/context/build-test.md — consumed build trees and gate
