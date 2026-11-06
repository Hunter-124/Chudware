---
name: design-for-manufacturability
description: Process-specific manufacturing and 3D-print workflow, including validated slicing and safe printer-media delivery.
inject: auto
triggers:
  keywords: [design for manufacturability, manufacturability, DFM review, manufacturing review, injection molding, CNC machining, FDM printing, SLA printing, 3D printing, slice, printer media, flashdrive]
  tools: [set_process, resolve_process, dfm_check, dfm_reinforce, wall_thickness, slice_part, verify_gcode, print_media_inventory, publish_print_media]
  phases: []
---

# Design for Manufacturability

## Sequence

1. Identify the intended process, material, scale, and production quantity only when they affect the decision.
2. Resolve the process profile; if a missing choice changes geometry, ask rather than assume.
3. Apply only relevant checks: wall/tool access for machining, wall/overhang for printing, wall/draft for molding, bend rules for sheet metal.
4. Run `dfm_check`, fix named measured failures, and rerun it before export or release.
5. Record the selected process and any deliberate exception with its rationale.

## Strength and process geometry

- Additive: screen thin walls with `dfm_check` and `dfm_reinforce`; the latter
  proposes edits tested on an isolated clone, never applies them. Use whole
  extrusion-width multiples and inspect changed volume and fit. Favor internal
  fillets starting at roughly the adjacent wall thickness, gussets/ribs along
  load paths and tangent or lofted transitions over abrupt steps. Orient principal
  loads along layers; evaluate `run_topology_optimization` for load-bearing parts.
  Radius is a starting heuristic, not a strength guarantee.
- Subtractive: internal corners need at least the cutter radius; aim for 10–20%
  extra clearance when practical. Avoid deep narrow pockets; use organic surfaces
  only when their machining time is justified and the chosen setup can access them.
- Sources: [FDM corner guidance](https://meshra.ai/blog/chamfer-vs-fillet-3d-printing);
  [Protolabs Network CNC design guide](https://www.hubs.com/guides/cnc-machining/).

## Restraint

- Do not block a concept on a rule for a process the user did not choose.
- Treat heuristics as guidance until a selected process profile or measured failure makes them actionable.
- Prefer one targeted fix over speculative redesign.

## Manufacturing floor
  - The stored material is authoritative: set_material, never prose.
  - The default verify_battery covers only its reported checks. Inspect
    coverage.ran and coverage.skipped; explicitly request applicable ECAD and
    power checks. Fits, loads, tolerances, drawings and manufacturability require
    appropriate separate checks before you claim them.
  - Shops get export_part(fmt="step"); save_part's .step+ is Chudware's
    editable document, not ISO STEP.
  - Review a drawing as rendered pixels after its last annotation edit and
    clear its structured warnings before export_drawing.
  3D PRINTING (cad_guide("printing") has the full rules)
  1. While modelling every printed part, run wall_thickness() for the process-minimum
     RED heat map AND dfm_check(printer=<model>). Fix every red region at its named
     face/feature or explicitly justify it before export/slicing; fix DFM failures
     and re-run both. Defer min_wall_mm to the bound process/FDM profile, not memory.
     Use 45° bed-edge chamfers, teardrop horizontal holes, line-width-multiple walls,
     no sealed support pockets; use DFM's estimate to cut material/time.
  2. slice_part is the ONLY route to G-code: vendor presets for the user's
     printer, measured orientation, supports from the analysis, verify_gcode on
     the result. Never fetch a slicer, write slicer .ini keys, or reuse an
     unmeasured build_dir. Several parts in one job: slice_part(parts=[...]);
     add complete_individual_objects=True for PrusaSlicer's "Complete
     individual objects" (one object at a time, clearance-checked).
  3. Deliver only print_ready results, or say exactly which check failed and why.
     If the user wants a not-print_ready file on their media anyway, that is their
     call: publish it with the job's user_override (their words), never refuse.
  4. Printer media (USB/SD) holds exactly the current set; the printer shows
     names, not dates. The moment a flashdrive/SD delivery is asked for, call
     print_media_inventory (read-only) on its mount, compare against
     the project's COMPLETE job set, then publish_print_media with every part's
     .print.json, the inventory_token, and media_idle=True only after the user
     confirms no printer is using the media. Never cp/fs_write/shell-copy G-code
     onto media or pick "current" by mtime. Read back with the inventory and
     use raw files at the drive root by default, or layout="project" for ONE
     top-level project folder. Never create nested delivery folders or copy CAD,
     3MF, STL or review packets to printer media. After inventory, identify stale
     or duplicate files belonging to this project and include their relative paths
     in retire_paths. Preserve unrelated or ambiguous files and surface them to
     the user rather than deleting them blindly. Report exact returned filenames
     and warnings; do not call media clean while unmanaged printables remain.
     Permanently delete deprecated G-code after verified commit; never archive
     it on media or host. Temporary rollback staging is removed after commit.
  5. Printing never authorizes firmware maintenance: do not create/copy/flash
     .bin/.hex/.elf/.uf2/.dfu images, reset firmware/EEPROM, run slicer post_process
     scripts, or use fw_flash/raw serial/shell as a print-delivery workaround.
     Only inspectable text G-code can be delivered. Firmware/media-write commands
     and root firmware images are hard refusals, not printability overrides.
     Warn that suspect media may auto-flash at boot; leave unknown images untouched
     until the user approves moving them off the media.
