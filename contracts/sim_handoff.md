# Contract: `aicad.simhandoff/1` — cross-sim handoff & verify envelopes

> **Frozen seam.** Append-only; changes require maintainer sign-off + a version bump
> (see `contracts/README.md`). The executable form is `services/mcp/simhandoff.py`
> (builders + structural validator). First conforming producer/consumer pair:
> `piping_cfd_handoff` / `piping_verify_cfd` (`services/mcp/piping_tools.py`).

## 1. Purpose & scope

One physics domain solves, then hands its result to another solver as geometry +
boundary conditions; a verify step later cross-checks the two answers. The piping→CFD
pair proved the pattern (1-D network solve → bore solid → LBM CFD → dP comparison
verdict). This contract generalizes that pattern into **three envelope shapes** so
every coupling implements against one seam:

1. **Handoff envelope** — what one study gives the next: the source result reference,
   the derived geometry references, and the generated boundary conditions / loads,
   each with explicit units and provenance.
2. **Verify envelope** — the cross-check: which quantities were compared, at what
   tolerance, with what verdict.
3. **Study ref** — the provenance atom: enough fields that any sim result can name
   exactly what produced it (domain, tool, doc, study, upstream handoff).

Known couplings this shape must carry (SIM_FW_FINALIZE_PLAN_2026-07-10 §3 — plan doc pruned 2026-07-23 after the FIN wave shipped; couplings preserved here):

| coupling id | source → target | notes on shape usage |
|---|---|---|
| `piping_to_cfd` | 1-D pipe network → LBM/RANS CFD | worked example, §6 |
| `ecad_to_thermal_fea` | board power metadata → thermal FEA | one `heat_source` condition **per component** (`applied_to` names the component/region); geometry = board + component bodies (multiple entries, distinct `role`s) |
| `motion_to_fea` | motion study joint reactions → static FEA | one condition per reaction force/moment; optional `case` labels group conditions into per-timestep/pose load cases |
| `hydraulic_to_piping` | hydraulic circuit → piping network solve | doc-to-doc: `geometry` may be empty; a `geometry_ref` of `kind:"doc"` carries the generated piping document |
| `thermal_to_cfd` | thermal FEA surface temps → conjugate CFD | `temperature`/`heat_flux` conditions on named faces; expectations compare temperatures |
| `hydraulic_to_fea` | cylinder/actuator mount loads → static FEA | one force condition per case; `case` labels (`extend_max_pressure` / `retract_max_pressure`) group per-stroke load cases (shipped by `hydraulic_fea_handoff`) |

Non-goals: this is a *data* contract, not an orchestration contract (DAG scheduling is
`contracts/dag_schema.md`); it does not prescribe solver internals or mesh formats.

## 2. Conventions

- **Envelope transport:** envelopes are plain JSON objects. Producing tools embed them
  in their existing result payload under the reserved key **`simhandoff`** — existing
  result fields are untouched (conformance is additive; adopting the contract must
  never change a tool's pre-existing output fields).
- **Units are explicit, always.** Every `value` carries a `units` string (UCUM-ish
  plain text: `"m/s"`, `"kPa"`, `"W"`, `"N"`, `"N*m"`, `"degC"`, `"mm"`, `"%"`).
  A dimensionless quantity uses `units: "1"`. No implicit-unit fields.
- **Provenance is explicit, always.** Every generated condition/expectation names, in
  its `provenance` string, the source-result field(s) it was derived from — a human- and
  agent-readable sentence, e.g. `"1D solve link r1 velocity_ms"`.
- **Soft-fail:** validators return `list[str]` of JSON-path-prefixed messages (empty =
  valid), matching `engine.doc.schema` / `engine.piping.schema` style.
- **Open vocabularies:** `coupling`, condition `kind`, geometry `role`, and check
  `quantity` are open strings (recommended values listed below) — the validator checks
  structure, not enumeration membership, so new couplings never need a version bump.
- `id` fields are UUID strings; `created` is ISO-8601 UTC.

## 3. Study ref (provenance atom)

```jsonc
{
  "domain": "piping",            // REQUIRED — physics/authoring domain: piping|cfd|fea|thermal|motion|ecad|hydraulic|...
  "tool":   "piping_solve",      // REQUIRED — the MCP tool / engine entry that produced the result
  "doc_id": "d12",               // optional — engine document the study ran on
  "study":  "flow-1",            // optional — named study within the doc (sim_study_record)
  "handoff_id": "<uuid>",        // optional — id of the handoff envelope that SPAWNED this study
  "result_digest": { ... }       // optional — small inline excerpt of the result (key numbers only)
}
```

A sim result "names what produced it" by carrying a study ref: results born from a
handoff carry the `handoff_id`, closing the provenance chain
`source study → handoff → target study → verify`.

## 4. Handoff envelope (`kind: "handoff"`)

```jsonc
{
  "schema": "aicad.simhandoff/1",
  "kind": "handoff",
  "id": "<uuid>",
  "created": "2026-07-10T18:00:00Z",
  "coupling": "piping_to_cfd",       // REQUIRED — coupling id (§1 table or new)

  "source": { <study ref> },         // REQUIRED — what produced this handoff
  "target": { <study ref> },         // REQUIRED — what should consume it (tool known
                                     //   up front; doc_id/study filled when spawned)

  "geometry": [                      // derived geometry refs (may be empty for doc-to-doc)
    {
      "role": "fluid_domain",        // REQUIRED — semantic role: fluid_domain|board|component_bodies|doc|...
      "kind": "step",                // REQUIRED — step|stl|doc|doc_id|mesh|...
      "path": "/tmp/.../fluid_domain.step",  // one of path / doc_id / inline is REQUIRED
      "doc_id": null,                //   (doc_id once imported into the engine)
      "inline": null,                //   (small generated documents, e.g. aicad.piping/1)
      "derived_by": "piping_internal_volume",  // optional — tool/function that derived it
      "units": "mm",                 // length units of the geometry payload (default mm)
      "meta": { "volume_mm3": 253437.2 }       // optional free-form
    }
  ],

  "conditions": [                    // REQUIRED — generated BCs/loads (may be empty only
    {                                //   when the coupling is pure doc translation)
      "name": "inlet_velocity",      // REQUIRED — unique within the envelope
      "kind": "velocity",            // REQUIRED — velocity|pressure|heat_source|heat_flux|
                                     //   temperature|force|moment|fixture|flow|mass_flow|...
      "value": 1.23,                 // REQUIRED — number | [x,y,z] vector | object (tables)
      "units": "m/s",                // REQUIRED
      "applied_to": "inlet",         // optional — region/face/component/node the condition binds to
      "case": null,                  // optional — load-case label (motion→FEA per-pose cases)
      "provenance": "1D solve link r1 velocity_ms"   // REQUIRED — source-field sentence
    }
  ],

  "expectations": [                  // optional — the quantities verify will compare
    {
      "quantity": "pressure_drop",   // REQUIRED
      "value": 12.3,                 // REQUIRED — the source-side prediction
      "units": "kPa",                // REQUIRED
      "provenance": "sum of 1D link dp_kpa"  // REQUIRED
    }
  ],

  "warnings": [ "..." ],             // optional
  "meta": { ... }                    // optional free-form (solver knobs forwarded, etc.)
}
```

Structural rules (enforced by `validate_envelope`):
- `schema` must be `"aicad.simhandoff/1"`, `kind` must be `"handoff"`.
- `coupling`, `source`, `target` required; study refs require `domain` + `tool`.
- every condition requires `name` (unique), `kind`, `value`, `units`, `provenance`.
- every geometry ref requires `role`, `kind`, and at least one of `path`/`doc_id`/`inline`.
- every expectation requires `quantity`, `value`, `units`, `provenance`.

## 5. Verify envelope (`kind: "verify"`)

```jsonc
{
  "schema": "aicad.simhandoff/1",
  "kind": "verify",
  "id": "<uuid>",
  "created": "2026-07-10T18:05:00Z",
  "coupling": "piping_to_cfd",

  "source": { <study ref> },         // REQUIRED — the source-side study compared
  "target": { <study ref> },         // REQUIRED — the target-side study compared
  "handoff_id": "<uuid>",            // optional — the handoff that linked them

  "checks": [                        // REQUIRED — ≥1 quantitative comparison
    {
      "quantity": "pressure_drop",   // REQUIRED — what was compared
      "units": "kPa",                // REQUIRED
      "source_value": 12.3,          // REQUIRED — source-domain number
      "target_value": 13.1,          // REQUIRED — target-domain number
      "deviation_pct": 6.5,          // REQUIRED — |target−source|/|source|·100 (or coupling-defined)
      "tolerance_pct": 20.0,         // REQUIRED
      "pass": true                   // REQUIRED
    }
  ],

  "pass": true,                      // REQUIRED — AND of all checks
  "verdict": "PASS: ...",            // REQUIRED — one human-readable sentence
  "advisories": [                    // optional — non-gating findings (e.g. velocity limits)
    { "name": "velocity_limit", "detail": { ... } }
  ],
  "warnings": [ "..." ]
}
```

Structural rules: `checks` non-empty; each check requires all seven fields; `pass` must
equal the AND of check passes; `verdict` non-empty string.

## 6. Worked example — piping → CFD (the proven pair)

`piping_cfd_handoff(doc, boundary)` solves the 1-D network, extracts the bore STEP,
derives CFD BCs, runs LBM CFD, and embeds this envelope under `result["simhandoff"]`:

```jsonc
{
  "schema": "aicad.simhandoff/1",
  "kind": "handoff",
  "id": "0b6c…",
  "created": "2026-07-10T18:00:12Z",
  "coupling": "piping_to_cfd",
  "source": { "domain": "piping", "tool": "piping_solve",
              "result_digest": { "converged": true, "total_dp_kpa": 12.30 } },
  "target": { "domain": "cfd", "tool": "run_cfd_flow", "doc_id": "d17" },
  "geometry": [
    { "role": "fluid_domain", "kind": "step",
      "path": "/tmp/piping_cfd_x1/fluid_domain.step", "doc_id": "d17",
      "derived_by": "piping_internal_volume", "units": "mm",
      "meta": { "volume_mm3": 253437.2 } }
  ],
  "conditions": [
    { "name": "inlet_velocity",  "kind": "velocity", "value": 1.23, "units": "m/s",
      "applied_to": "inlet",  "provenance": "1D solve link r1 velocity_ms" },
    { "name": "inlet_pressure",  "kind": "pressure", "value": 300.0, "units": "kPa",
      "applied_to": "inlet",  "provenance": "1D solve node n0 pressure_kpa" },
    { "name": "outlet_pressure", "kind": "pressure", "value": 287.7, "units": "kPa",
      "applied_to": "outlet", "provenance": "1D solve node n1 pressure_kpa" }
  ],
  "expectations": [
    { "quantity": "pressure_drop", "value": 12.3, "units": "kPa",
      "provenance": "1D inlet−outlet pressure (cfd_boundary_from_solve pressure_drop_1d_kpa)" }
  ]
}
```

`piping_verify_cfd(solve_result, cfd_result, tolerance_pct=20)` then embeds:

```jsonc
{
  "schema": "aicad.simhandoff/1",
  "kind": "verify",
  "id": "9a41…",
  "created": "2026-07-10T18:05:40Z",
  "coupling": "piping_to_cfd",
  "source": { "domain": "piping", "tool": "piping_solve" },
  "target": { "domain": "cfd", "tool": "run_cfd_flow" },
  "checks": [
    { "quantity": "pressure_drop", "units": "kPa",
      "source_value": 12.30, "target_value": 13.10,
      "deviation_pct": 6.5, "tolerance_pct": 20.0, "pass": true }
  ],
  "pass": true,
  "verdict": "PASS: 3D CFD dp (13.10 kPa) within 20% of 1D solve dp (12.30 kPa) — Δ 6.5%",
  "advisories": [
    { "name": "velocity_limit",
      "detail": { "violations": [ { "link": "r2", "velocity_ms": 3.4, "limit_ms": 3.0 } ] } }
  ]
}
```

The piping tools' pre-existing top-level fields (`solve`, `boundary_cfd`, `step_path`,
`cfd`, `dp_1d_kpa`, `dp_3d_kpa`, `verdict`, …) are unchanged — the envelope is a
conformance overlay, not a replacement.

## 7. Helper library (executable form)

`services/mcp/simhandoff.py` — no FastMCP/engine imports; pure builders + validator so
any tool module (and tests) can use it engine-less:

| helper | builds |
|---|---|
| `study_ref(domain, tool, ...)` | §3 study ref |
| `geometry_ref(role, kind, ...)` | §4 geometry entry |
| `condition(name, kind, value, units, provenance, ...)` | §4 condition |
| `expectation(quantity, value, units, provenance)` | §4 expectation |
| `build_handoff(coupling, source, target, ...)` | §4 envelope (id/created stamped) |
| `check(quantity, units, source_value, target_value, deviation_pct, tolerance_pct, passed)` | §5 check |
| `build_verify(coupling, source, target, checks, verdict, ...)` | §5 envelope (overall `pass` computed) |
| `validate_envelope(env) -> list[str]` | structural validation, both kinds |
| `attach(result, env) -> result` | validate + embed under `result["simhandoff"]` (raises on invalid — a producer bug, never user input) |

Producers of the ECAD→thermal, thermal→CFD, motion→FEA, and hydraulic couplings build and
validate their envelopes exclusively through these helpers.
