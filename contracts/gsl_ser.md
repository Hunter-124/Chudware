# gsl-ser/v1 — canonical GSL text serialization

Status: **FROZEN** (v1). Append-only; a breaking change is `gsl-ser/v2` via a
versioned contract change, never an in-place edit of v1 semantics.

Executable reference: `tools/translator/gsl_ser.py` (code is normative for behaviour,
this file for wording). Serialization choice matters: controlled studies swing
small-model accuracy 5–60% on serialization choice alone. The serialization is a lens over
the numbers the deterministic describers already produce — it never recomputes a value
(inherits `contracts/narration.md`).

---

## Scope

`gsl-ser/v1` is the compact, versioned text a prompt carries as the CAD/CAM/FEA/ECAD
*state* the learned layer narrates. It is produced from a **state dict** — the
`_digest`-shaped payload (`services/mcp/gsl_tools.py:_digest`) extended with optional
`sketch` / `cam` / `fea` / `ecad` sections — so it is engine-free and runs in CI with
no `libchudware`. It is NOT a replacement for the human-facing narration in
`contracts/narration.md`; it is the machine-parseable envelope that round-trips through
`extract_slots` (see the round-trip invariant below).

The architecture boundary is binding: the learned layer **never invents a numeral**.
Every numeral in any caption must trace to a slot enumerated from the state; numbers are
copied and verified post-hoc, and on verification failure the system degrades to the
deterministic template, never to fiction.

---

## Version tag (mandatory)

Every serialization begins with, and every prompt that embeds one carries, the header:

```
#gsl-ser/v1 domain=<brep|sketch|cam|fea|ecad>
```

Consumers accept any `gsl-ser/vN` header they know and MUST reject an unknown major
version rather than mis-parse it. `SER_VERSION` is the single source of the tag.

---

## Slots — the unit of fidelity

A **slot** is one atomic, verifiable fact: `(path, value, unit, kind)`.

- **path** — a fully-qualified dotted key locating the fact in the state, e.g.
  `part.bbox.x`, `feat.Bolts.diameter`, `cam.op.Rough.machined`, `fea.min_fos`,
  `ecad.trace.SW.impedance`. Paths are the alignment anchors for slot comparison.
- **value** — the **canonical (serialized) value**: the raw number formatted at its
  unit's precision and re-parsed (`canon()`), so ground-truth and re-extracted slots
  agree to the last digit. Categorical slots carry a whitespace-free token.
- **unit** — from the fixed vocabulary below.
- **kind** — `dim` | `count` | `tolerance` | `material` | `enum`.

### Unit vocabulary + precision (from `contracts/narration.md`)

| unit | meaning | format | prose |
|---|---|---|---|
| `mm` | linear dimension / volume `mm3` | 3 sig figs (`:.3g`) | `6.35mm` |
| `deg` | angle | `:.4g` | `45deg` |
| `MPa` | stress | 1 decimal | `247.3MPa` |
| `Hz` | frequency | 1 decimal | `234.1Hz` |
| `mm/min` | feed rate | integer | `1500mm/min` |
| `s` | time | 1 decimal | `142.7s` |
| `pct` | percentage | 1 decimal | `87.5pct` |
| `ohm` | trace impedance | 1 decimal | `50.0ohm` |
| `ratio` | dimensionless ratio (FOS) | 2 decimals | `1.43` |
| `count` | integer count | integer | `6` |
| `none` | categorical (material/enum) | token | `6061_T6` |

`count` and `ratio` are **dimensionless**: they appear as bare numbers in prose (a bare
integer/decimal is an acceptable candidate for them). All other numeric units MUST carry
their unit token in prose. Unit *sanity* (the ksi/MPa 6.9× guard) is the unit-sanity
verifier's job, not this serialization's — v1 only folds exact-equivalent spellings
(`cm→mm`, `%→pct`, `ohms→ohm`, …) on extraction of free text.

---

## Canonical text form

After the header, one line per slot in **enumeration order** (see below):

```
<path> = <value><unit>
```

then, for B-rep states, a single **degree-ordered adjacency** line (structural, not a
slot): faces ordered by descending adjacency degree then sid, each edge `A-B` with its
higher-degree endpoint first:

```
adj = f3-f1,f3-f2,f1-f2
```

Worked example (a bolt-circle plate):

```
#gsl-ser/v1 domain=brep
part.name = bolt_plate
part.bbox.x = 120mm
part.bbox.y = 80mm
part.bbox.z = 10mm
part.faces = 18count
part.edges = 40count
part.sym.order = 6count
part.sym.type = rotational
feat.Bolts.type = bolt_circle
feat.Bolts.faces = 6count
feat.Bolts.diameter = 8mm
feat.Bolts.pcd = 60mm
feat.Bolts.count = 6count
adj = ...
```

The strict form suffixes `count` (machine round-trip); the prose caption
(`render_caption`) drops it. Both are grounded by construction.

### Enumeration order (deterministic)

`enumerate_slots(state)` — and therefore `serialize` — emits: `part.name`, bbox x/y/z,
faces, edges, symmetry (order, type); then each feature group in tree order
(`type`, `faces`, then the present numeric params); then bores; then the domain sections
`sketch`, `cam`, `fea`, `ecad` in that order. A partial state (e.g. FEA-only) enumerates
only its own slots. Order is stable so re-extraction compares 1:1.

---

## Round-trip invariant

```
extract_slots(serialize(state)) == enumerate_slots(state)          # exact
```

`roundtrip_diff(state)` returns `[]` when this holds. Exactness is guaranteed because
`Slot.value` is already the canonical rounded value, so serialize→parse is lossless.
This is the re-extraction diff every serializer change must keep empty.

---

## Groundedness seam

Extrinsic-hallucination detection (a caption numeral that grounds to no slot) is a
groundedness question, owned by the verifier battery. `gsl-ser/v1` defines only
the seam (`tools/translator/groundedness.py`: the `GroundednessChecker` protocol +
`check(text, state) -> [Verdict]`); slot-fidelity scoring is independent of it.

---

## Adding a domain to v1

v1 sections are additive (they do not change existing slots, so they are not a version
bump). To add one: (1) extend `enumerate_slots`/`_domain_slots` with the new section's
slots using existing units; (2) confirm `roundtrip_diff` is `[]` for representative
states. A NEW unit is a v1
extension only if it is dimensionless-or-additive; anything that changes an existing
slot's unit/precision is a `gsl-ser/v2` change.
