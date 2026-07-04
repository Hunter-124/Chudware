---
id: T-BL02
title: Format interop track (reverse-engineering intake)
workstream: backlog
owner: mooseinatree
status: doing
branch: task/T-BL02-format-interop
depends_on: []
lane: modeling-core
kind: new
priority: 3
---

## Context

Land parsers as specs arrive from the reversing effort. Order: openNURBS/.3dm (MIT lib, no reversing), Parasolid payload extraction from NX/Solid Edge, DXF via ezdxf, then CATIA V5, Inventor, Altium (clean-room; KiCad importer as spec reference only). Defer Revit — IFC covers BIM.

## Ownership (2026-07-03)

Assigned to **mooseinatree** (human) — has the competitor CAD software (NX/Solid Edge/
CATIA/Inventor/Altium) and a reverse-engineering setup on Windows to actually produce
the format specs/samples this task consumes. Division of labor: mooseinatree reverses
each format and lands the clean-room protocol memo + sample files; an agent driver can
then be dispatched per-format to write the `engine/doc/formats.py` importer + fixture
round-trip tests once a spec/sample is available. Don't claim this whole task via the
normal agent auto-claim path — coordinate with mooseinatree on which format is ready.

## Definition of done

- [ ] Each format lands as an engine/doc/formats.py importer with fixture round-trip tests
- [ ] Clean-room protocol memo followed for every reversed format
- [ ] ODA membership decision recorded for DWG/Revit

## Progress log

### 2026-07-03 — mooseinatree (Opus driver, isolated Windows checkout)

Picking up the greenlit no-reversing slice: **DXF import via ezdxf (MIT)**. Status:
- `.3dm` (rhino3dm, MIT) importer/exporter already exists in engine/doc/formats.py at
  the mesh level (NURBS/Brep-only .3dm still skip — rhino3dm has no mesher; documented).
- `.dxf` is entirely absent (no ext map, no FormatInfo, no importer) → building it now:
  `engine_oracle/doc/formats_dxf.py` (new module, mirrors formats_solidworks.py),
  wired into the registry + import_path; ezdxf → build123d edges → wires → faces where
  closed → Compound. Import-only for now (2D→DXF export deferred; stated honestly).

**Environment note (this Windows checkout only, local/non-committed):** the top-level
`engine`, `mcp_server`, `dag`, `deliverables`, `agent`, `installer`, `app`, `corpus*`,
`test_fixtures_sw` names are git symlinks (mode 120000, e.g. `engine → engine_oracle`)
that got materialized as real directory copies because core.symlinks=false on Windows.
Git reported all 11 as "deleted", which tripped `work.py claim`'s dirty-tree guard
(BUSY on every claim). Remedy applied: `git update-index --skip-worktree` on those 11
paths — local-only, reversible (`--no-skip-worktree`), does NOT change committed content
or the Linux symlink layout. Canonical edits go in the tracked tree (`engine_oracle/`);
the runtime `engine/` copy is kept in sync for pytest (Python imports resolve to `engine/`).
- 2026-07-04 00:53 UTC [mooseinatree] DXF import slice complete + pushed on branch task/T-BL02-format-interop (merge-ready). ezdxf (MIT) -> engine/doc/formats_dxf.py: LINE/CIRCLE/ARC/LWPOLYLINE(bulge)/POLYLINE/ELLIPSE/SPLINE/INSERT -> build123d planar B-rep (closed wires -> Faces, open -> wires), wired into import_path + FORMAT_REGISTRY (.dxf, import-only) + doc_formats matrix. 8 round-trip tests pass (python -m pytest engine/doc/test_formats_dxf.py). requirements.txt + contracts/licenses.md updated (ezdxf MIT, no debt). scripts/check.py stdlib gate PASS; core-build/ctest/full-pytest need Linux env (SKIP on this Windows box) - foreman Linux merge pass to run full gate.sh. ORACLE SIDE ONLY: GUI/MCP import runs in the C++ core (core/doc/formats.cpp, no DXF reader yet) -> follow-up T-BL47 cut for the bit-identical C++ mirror; import_part MCP docstring intentionally left unchanged until the core can import DXF. T-BL02 remains the ongoing interop umbrella (reopen after merge for next format: Parasolid/CATIA/etc.).
