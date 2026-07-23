# ui/viewport — AICM loader only (R0b remnant)

This directory was the planned "shared OpenGL viewport pipeline". That
consolidation never happened: the host builds its own renderer/mesh/camera/
id-pass/overlays under `host/src/` + `host/src/render/`, and the copies here
were orphaned (in no build). R0b deleted them.

## What remains and why

- `render/aicm_loader.{h,cpp}` — AICM v1 mesh-format reader (contract:
  `docs/contracts/mesh_format.md`). Pure CPU, no GL. Kept because
  `core/CMakeLists.txt` compiles it into the libchudware test/tool targets
  (include dir `ui/viewport/render`). Depends only on
  `core/util/aicm_handoff.h`.

## Gotchas

- `host/src/render/aicm_loader.{h,cpp}` is a duplicate of the same loader,
  compiled into the host GUI. The two are deliberately NOT merged in R0b;
  dedup is future work (care: core must stay GUI-free).
- `CMakeLists.txt` here is orphaned (no parent `add_subdirectory`) and still
  references the deleted renderer/mesh/camera/id_pass/overlays sources and
  the retired `ui/abstract` tree — integrator owns its removal; do not try
  to configure it standalone.
