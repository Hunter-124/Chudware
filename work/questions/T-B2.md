# Questions — T-B2 (BOM roll-up)

Both items below are **non-blocking**: I proceeded with the recommended answer and the task is
merge-ready. Flagging for integrator awareness / a follow-up task.

## Q1. Two additive-OPTIONAL fields on `BomLine` (`mpn`, `description`)

**Context.** Frozen contract `aicad.sourcing/1` §3 defines `BomLine` with `line_id, kind,
quantity, unit, part_ref, vendor_part, cost_model, unit_cost, ext_cost, currency`. The roll-up
needs to carry a manufacturer part number from `rollup()` (where a bought line is known only by
MPN — e.g. an ECAD footprint — before a supplier/`part_ref` is resolved) through to
`resolve_prices()`. It also benefits from a human-readable `description` for the demo order
summary (the task file's original `BomLine` sketch listed both `mpn` and `description`).

**Recommended answer (implemented).** Add `mpn: str | None = None` and `description: str = ""`
as **additive OPTIONAL** fields. Contract §0 explicitly permits new optional fields with **no
version bump**, and every §3-shaped consumer that reads only the frozen fields is unaffected
because both default to `None`/`""`. Frozen §3 fields are honored field-for-field (`part_ref`
stays the resolved `VendorPart.key`, null until priced). If the integrator prefers zero schema
surface change, the MPN could instead be smuggled through `part_ref` and parsed back out — I
judged a typed optional field cleaner and contract-legal. No version bump taken.

*Status: proceeding as implemented; unblocked.*

## Q2. `engine_oracle/ecad/fab/bom.py::write_bom` drops the MPN column (upstream)

**Observation.** `write_bom` filters `fields` against `_STANDARD_FIELDS` (which contains
`"mpn"`) **before** `mpn = fields.pop("MPN", fields.pop("mpn", ""))`, so the MPN is removed from
`fields` first and the pop always yields `""`. The emitted CSV/JSON MPN column is therefore
**always empty**, regardless of footprint data. Verified empirically.

**Impact + handling.** Out of T-B2 scope (that shared exporter is not in my Files/areas touched,
and fixing it risks its own tests). The roll-up's ECAD merge still goes **through** the real
`write_bom` CSV for grouping/qty/DNP (DoD: "BOM CSV merged"), but recovers each row's MPN from
the source board footprints by ref so electronic lines remain priceable. Recommend a small
follow-up ECAD task to fix `write_bom` (read MPN before filtering, or from a top-level `fp["mpn"]`)
so the exporter itself carries MPN.

*Status: worked around; unblocked.*
