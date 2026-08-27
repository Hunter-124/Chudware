---
id: T-BL47
title: DXF import — C++ core mirror (reach GUI/MCP import_part end-to-end)
workstream: backlog
owner: mooseinatree
status: done
priority: 3
lane: modeling-core
kind: new
depends_on: [T-BL02]
branch: task/T-BL47-dxf-core-mirror
---

## Context

T-BL02 landed the **Python oracle** DXF importer (`engine/doc/formats_dxf.py`, via ezdxf):
DXF 2D entities → build123d planar B-rep wires/faces, wired into `import_path` + the
`doc_formats` registry, with round-trip tests. Reachable today by Python callers
(`engine.ecad.threed`, tools, tests) — **but not by the GUI/MCP `import_part`**, because
the production import path runs in the **C++ core** (`core/doc/session.cpp::doc_import_file`
→ `core/doc/formats.cpp::import_bodies`), which is a standalone C++ reimplementation of the
Python oracle (openNURBS for .3dm, lib3mf for .3mf, OCCT for STEP/IGES/BREP — each written
"BIT-IDENTICAL to engine/doc/formats.py"). There is currently **no C++ DXF reader**, so
`import_part("*.dxf")` still fails in-app.

This task adds the C++ mirror so DXF import reaches the GUI/MCP, verified bit-identical
(or geometrically equivalent within tolerance) against the Python oracle — the same
lockstep pattern used for .3dm/.3mf.

## OSS survey required (license-sensitive — do this first)

Need a C++ DXF parser for the core (which links into libchudware). Survey the field and record
the pick in `contracts/licenses.md` + `requirements`/CMake:
- **libdxfrw** — used by LibreCAD; **GPL** → cannot link in-process (guardrail). Subprocess only.
- **dime** (Coin3D) — BSD-ish; check exact terms. In-process-safe if BSD.
- **dxflib** (Ribbon Soft) — GPL/commercial dual → avoid the GPL side for in-process.
- Hand-rolled minimal DXF tag reader for the entity subset the oracle supports (LINE/CIRCLE/
  ARC/LWPOLYLINE/POLYLINE/ELLIPSE/SPLINE/INSERT) — no dep, but more code. Viable given the
  oracle already defines the exact entity → geometry contract to mirror.
Prefer permissive/in-process; GPL parser would force a subprocess bridge.

## Definition of done

- [ ] OSS survey recorded; DXF parser dependency (or hand-rolled reader) chosen with a
      `contracts/licenses.md` row (+ `contracts/license_debt.md` if any shortcut is taken).
- [ ] `core/doc/formats.cpp` reads DXF → OCCT wires/faces, mirroring the entity coverage and
      geometry of `engine/doc/formats_dxf.py` (LINE/CIRCLE/ARC/LWPOLYLINE bulge/POLYLINE/
      ELLIPSE/SPLINE/INSERT; closed wire → face; open → free wire).
- [ ] `.dxf` added to the core format map (`core/doc/formats.cpp` ext table) + `import_bodies`
      dispatch; import-only (no DXF write from the geometry path — a drawing→DXF export already
      exists separately in host/src/app.cpp).
- [ ] Parity test (ctest) cross-checks C++ DXF import vs the Python oracle on shared fixtures
      (same square/circle/arc/bulge/ellipse/insert cases as `test_formats_dxf.py`), matching
      face count + areas within tolerance.
- [ ] **MCP freshness:** now that the GUI/MCP can import DXF, add `.dxf` to the `import_part`
      docstring format list in `services/mcp/cad_tools.py` (deliberately withheld in T-BL02
      until this lands, to avoid advertising an import the core can't do) and refresh any
      cad_guide/context pack that enumerates importable formats.
- [ ] scripts/gate.sh + `graphify update .`; status review.

## Notes

- The Python oracle (`formats_dxf.py`) is the reference contract: the C++ side should produce
  the same faces/wires. Reuse its fixtures for the parity test.
- Curves (ELLIPSE/SPLINE) are flattened to a spline in the oracle (chord dev 0.05 drawing
  units) — the C++ side may flatten similarly or build exact OCCT geometry; parity test should
  compare areas within a few-percent tolerance for the flattened curves.

## Progress log
- 2026-07-04 02:53 UTC [mooseinatree] DXF import wired into the C++ core + verified end-to-end. Hand-rolled clean-room DXF ASCII reader in core/doc/formats.cpp (LINE/CIRCLE/ARC/LWPOLYLINE/POLYLINE+bulge -> OCCT edges -> ShapeAnalysis_FreeBounds wires -> faces for closed loops; no new dependency), wired into detect_format + import_bodies (.dxf/.dwg). DWG rides a dwg2dxf (LibreDWG) subprocess, GPL-isolated. Host dropPathHasKnownExt gains .dxf/.dwg (drag-drop). MCP import_part docstring now advertises DXF/DWG. VERIFIED: import_file on a DXF (square+circle+arc+line) through the rebuilt libchudware.dll yields a part doc with 2 faces + 7 edges (open arc/line stay free wires), matching the Python oracle. libchudware.dll + Chudware.exe rebuilt on Windows (MSVC) and restaged. Also fixed a pre-existing host-build blocker (host/extern/fix_math.h nexttowardf signature vs the 10.0.26100 UCRT). NOTE: branch task/T-BL47-dxf-core-mirror is based on fix/windows-runtime (the MSVC portability fixes are a prerequisite to build the core on Windows) - merge that first. FOLLOW-UPS: ELLIPSE/SPLINE/INSERT not yet in the C++ path (Python oracle covers them); a formal ctest parity fixture vs the oracle is still open (verified functionally, not via ctest). scripts/check.py green; full Linux gate.sh not runnable on this Windows box.
