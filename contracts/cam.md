# Contract: CAM simulation frames, audit, machines, job diff

> **Frozen seam.** Append-only; changes require maintainer sign-off + a version bump
> (see `contracts/README.md`). Executable producers live in `core/cam/` (`stock_sim`,
> `audit`, `feeds`/`MachineDB`, `session`) with thin MCP wrappers under `services/mcp/`.

## 1. Purpose & scope

Manufacture-tab simulation, AI CAM tooling, and deterministic G-code verification share
four JSON envelopes:

| schema | produced by | consumed by |
|---|---|---|
| `aicad.cam_sim_frames/1` | `cam_simulate` (appended fields) | host viewport playhead, MCP |
| `aicad.cam_audit/1` | `cam_audit` | GUI G-code tab, `cam_programmer`, narration |
| `aicad.machines/1` | `cam_machines` / `machines.json` | setup profiles, feeds clamps, audit envelope |
| `aicad.cam_job_diff/1` | `cam_job_diff` (MCP) | agent revision review |

Non-goals: 5-axis kinematics, rest-roughing region limiting, full RS-274 interpreter.

## 2. Conventions

- Envelopes are plain JSON objects. Unknown keys are ignored by consumers; producers
  MUST NOT remove documented keys once shipped.
- Lengths are millimetres, feeds mm/min, spindle rpm, times seconds unless noted.
- Height-field `z` arrays are row-major `j*nx+i` (same as `SimResult::to_json` field).

## 3. `aicad.cam_sim_frames/1` — simulation frame snapshots

Appended on each entry of `cam_simulate` → `results[]` when `options.frames > 0`.
Existing `cam_simulate` fields are unchanged.

```jsonc
{
  // …existing SimResult keys (ok, cycle_time_s, field?, …)…
  "frames": [
    {
      "t_s": 1.25,                    // cumulative seconds at snapshot
      "field": {
        "nx": 64,
        "ny": 64,
        "res": 0.5,                   // mm
        "lo": [ -25.0, -25.0 ],       // world XY of node (0,0)
        "z": [ /* nx*ny floats, row-major j*nx+i */ ]
      }
    }
  ],
  // present only when the core reduced requested frame count or downsampled grids
  "frames_clamped": true
}
```

Rules:

- Request via `cam_simulate` params `options.frames` (integer). `0` / omitted → no `frames` key.
- Core clamps requested count to **≤ 64**; when reduced, set `frames_clamped: true`.
- Snapshot grids with `nx*ny > 128*128` are **downsampled** to ≤128 per axis (strided
  sampling) for the `frames[]` payloads only; the full-resolution `field` / `remaining`
  (when `include_field`) is untouched. Downsample also sets `frames_clamped: true`.
- Frame times are strictly increasing; the final remaining field is guaranteed as the
  last frame (extra append if the last equal-interval snapshot is not bit-identical).
- Equal-interval targets are `k * cycle_time_s / N` for `k = 1..N` using the same
  feed-limited move timing as `estimate_time` / `cycle_time_s`.

## 4. `aicad.cam_audit/1` — deterministic G-code audit

Result of `cam_audit`:

```jsonc
{
  "schema": "aicad.cam_audit/1",
  "ok": true,                         // false iff errors non-empty
  "errors": [ "…" ],
  "warnings": [ "…" ],
  "stats": {
    "lines": 1204,
    "tool_changes": 3,
    "tools_used": [ 1, 2, 6 ],
    "bounds": {
      "x": [ -12.0, 88.5 ],
      "y": [ -5.0, 40.0 ],
      "z": [ -15.0, 25.0 ]
    },
    "feed_range_mm_min": [ 100.0, 2400.0 ],
    "rpm_range": [ 800.0, 12000.0 ],
    "est_time_s": 312.4
  },
  "provenance": {
    "dialect": "grbl",
    "machine_profile": "benchtop_vmc",
    "source": "cam_post"              // or "supplied"
  }
}
```

Checks (deterministic modal subset; no LLM): units (G20 → error), machine envelope,
feeds/speeds vs profile + CuttingDB chipload warnings, tool numbers ∈ job library,
toolpath bbox round-trip, rapids below clearance (warning).

## 5. `aicad.machines/1` — machine profile records

Canonical store: `services/research/data/machines.json`

```jsonc
{
  "schema": "aicad.machines/1",
  "machines": [
    {
      "id": "benchtop_vmc",
      "label": "Benchtop VMC",
      "kind": "mill3",                  // MachineKind string
      "travel_mm": [ 300.0, 300.0, 150.0 ],
      "rapid_xy_mm_min": 5000.0,
      "rapid_z_mm_min": 3000.0,
      "max_rpm": 15000.0,
      "min_rpm": 100.0,
      "max_feed_mm_min": 5000.0,
      "tool_change_s": 6.0,
      "accel_mm_s2": 0.0,
      "post_dialect": "grbl",
      "notes": ""
    }
  ]
}
```

Core `cam_machines` returns the same records (built-ins + optional
`CHUDWARE_MACHINES_REGISTRY` override file). Wire field names on the C++ `Machine`
struct (`rapid_xy`, `travel`, …) may differ at the RPC edge; adapters map to this
schema for JSON files and cross-checks.

## 6. `aicad.cam_job_diff/1` — job tree diff

Result of MCP `cam_job_diff`:

```jsonc
{
  "schema": "aicad.cam_job_diff/1",
  "ok": true,
  "ops_added": [ "op_pocket_2" ],
  "ops_removed": [],
  "ops_changed": [ { "op_id": "op_face_1", "fields": [ "feed", "rpm" ] } ],
  "tools_added": [ 3 ],
  "tools_removed": [],
  "setups_added": [],
  "setups_removed": [],
  "summary": "1 op added, 1 op changed (feed, rpm), tools +1"
}
```

## 7. Related RPCs

| RPC | notes |
|---|---|
| `cam_simulate` | `options.frames`, `include_field`; results carry frames schema |
| `cam_audit` | core RPC; MCP thin wrapper |
| `cam_machines` | lists profiles |
| `cam_feeds` | CuttingDB lookup for GUI autofill |
| `cam_tree` | each setup may include `machine_detail` (full `Machine::to_json`) alongside kind string `machine` |
| `cam_job_diff` | MCP-only |

## 8. CAM operation-generation completeness (additive)

`cam_post`, `cam_setup_sheet`, and each `cam_simulate.results[]` entry carry
`incomplete: boolean`. It is `true` if any unsuppressed operation in the
corresponding setup scope failed to generate a toolpath. Their `warnings` arrays
contain a readable `"operation <id>: <reason>"` string for each refusal; post
and simulation preserve any existing warnings and partial results. Consumers
MUST NOT export an incomplete posted program as a complete machining job.
`cam_setup_sheet` now includes a `warnings: string[]` field. `cam_audit`
also adds each generation refusal to both `warnings` and `errors`, making
`ok: false` even when the remaining G-code passes its modal checks.

All generated toolpath feeds remain mm/min internally. A turning operation's
explicit `feed` in `extra.feed_mode: "per_rev"` is entered in mm/rev and resolved
to mm/min before toolpath generation; the post converts emitted F words back
to mm/rev under G95. `cam_audit.stats.feed_range_mm_min` continues to report
effective mm/min, including G95 moves.
For a selected turning operation in G95 mode, `cam_feeds.feed` is returned in
mm/rev (with an additive `feed_mode: "per_rev"`); its other rate fields remain
mm/min. Without that mode, `cam_feeds.feed` remains mm/min.
`cam_audit` tracks G94/G95 modally, checks G95 F words against
0.001–2.0 mm/rev, and rejects cutting motion with the spindle off or without
a positive programmed S on spindle-based machines. Laser, plasma, and
waterjet machine profiles are exempt from the spindle requirement.
