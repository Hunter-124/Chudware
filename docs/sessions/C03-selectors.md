# C03 — Selector build + resolve

Model: **sonnet** · Wave 0 · Depends: none · Follow `docs/sessions/README.md` conventions.

## Mission
`engine/doc/selectors.py`: build selector objects from picked topology and re-resolve them
against rebuilt topology by scored best-match — the contract's exact algorithm.

## Read first
1. `contracts/selectors.md` — normative algorithm (weights, thresholds, margin, tiebreak)
2. `contracts/doc_schema.md` §0/§5 (selector-or-datum, reserved datums)
3. `engine/doc/schema.py` (`validate_selector` — your outputs must pass it)

## Owned paths
`engine/doc/selectors.py` · `engine/doc/test_selectors.py`

## Deliverables
1. Lightweight descriptors decoupled from OCP so resolution is unit-testable:
   `TopoInfo` dataclass: `kind, index, surf, centroid(np3), axis(np3|None), measure(float|None),
   owner(str|None), hint_index(int)`.
2. OCP describers: `describe_faces(shape, owners: dict[int,str]) -> list[TopoInfo]` (surface
   type via `BRepAdaptor_Surface(face).GetType()`, centroid/area via
   `BRepGProp.SurfaceProperties_s`, axis for plane/cylinder/cone from the adaptor),
   `describe_edges(...)` (curve type, midpoint centroid, length via `LinearProperties_s`,
   line direction / circle axis), `describe_vertices(...)`.
3. `make_selector(info: TopoInfo) -> dict` (contract §1 shape, passes `validate_selector`).
4. `resolve(selector: dict, candidates: list[TopoInfo], bbox_diag: float)
   -> ResolveResult {index: int | None, score, second_score, dangling_reason: str | None}` —
   EXACTLY contract §3: owner-filtered candidate set with fallback, scoring with/without axis,
   accept `< 0.35` AND margin `> 0.05`, hint_index tiebreak. Region resolution per §4
   (`resolve_region(selector, regions, sketch_bbox_diag)`).
5. `test_selectors.py` — the contract §5 torture suite implemented with SYNTHETIC TopoInfo sets
   (no OCP needed for the algorithm tests) PLUS one OCP smoke: describe a filleted box's faces,
   make a selector for the cylindrical fillet face, perturb the box size, re-describe, resolve
   → still finds the fillet face. Cases: moved-face re-resolve; deleted-owner fallback then
   dangling; symmetric twins force dangling via margin; hint_index rescues owner-matched ties;
   axis-less scoring path (vertices).

## Constraints
- Pure functions; no document/compiler imports (C01 integrates you later via the descriptors).
- Do not edit `engine/doc/__init__.py` — report desired exports.

## Verification
`& $py -m engine.doc.test_selectors` green; schema fixture suite still green.
