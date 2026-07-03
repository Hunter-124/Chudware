---
id: T-R4
title: PLM items & releases - part numbers, rev letters, release pinning + BOM freeze, where-used
workstream: R
owner: unassigned
status: todo
branch: task/T-R4-plm-items-releases
depends_on: [T-R2]
lane: plm-projects
kind: new
priority: 3
---

## Context

The "P" in PLM ([docs/PLM_VERSION_CONTROL_PLAN.md](../../docs/PLM_VERSION_CONTROL_PLAN.md)
§3.4): stable item identity + release management, kept invisible until it helps.
Every doc in a project gets an item_id and an auto part number (PRJ-0001, editable);
rev letters increment only at release. Releasing an assembly pins its component
references to exact revs and freezes the BOM (feeding existing bom_rollup).
Lifecycle fixed: In work → Released → Superseded (release of rev B auto-supersedes
rev A). Where-used derives from the manifest and powers stale-ref flags (T-R3) and
the Project Hub (T-R6).

## Files / areas touched

- services/plm/items.py — NEW: item registry in manifest.json (aicad.plm/1),
  part-number assignment/edit, rev-letter progression, lifecycle transitions
- services/plm/release.py — NEW: release pre-flight (dirty docs, stale refs,
  optional verify_battery hook), assembly ref pinning, BOM freeze snapshot
  (aicad.sourcing/1-compatible so bom_rollup/bom_price consume it)
- services/plm/whereused.py — NEW: manifest-derived index (item → consuming
  assemblies/drawings/CAM jobs)
- Drawing title-block: released rev letter stamps via existing set_titleblock
  path (engine/doc/drawing.py); superseded watermark on old-rev prints
- plm_tools.py: plm_release pre-flight envelope, plm_where_used, plm_item_set
  (part number edit)

## Definition of done

- [ ] Items auto-register on first vault entry; part numbers editable; rev
      letters only move at release; lifecycle transitions enforced (no
      edit-in-place of Released — "start rev C" copies forward)
- [ ] Releasing an assembly pins component revs + writes a BOM freeze that
      bom_rollup reproduces exactly later, even after components change
- [ ] Release pre-flight blocks on unsaved docs and lists stale refs; overridable
      with explicit confirmation (ease-of-use: warn, don't wall)
- [ ] Drawing title block shows the released rev letter automatically
- [ ] plm_where_used answers item → consumers correctly on a 3-level test project
- [ ] Tests: rev progression, supersede chain, pin/freeze round-trip, where-used;
      gate + graphify update .
