"""Geometry auto-prefill for engineering calculators.

Maps calc input slots (declared in ``services/research/calcs/registry``) to
live geometry queries so the advisory engine can pass measured values to calcs
instead of asking the user.

**Standalone module — no advisory-engine import dependency.**  The doc-query
interface is injected as a callable (``DocQuerier`` Protocol), making this fully
testable against fixture stubs without the C++ bridge.

Contract: contracts/advisor.md §7 — prefilled inputs are recorded as
``inputs.prefilled = {param_name: source_description}`` in the aicad.calc/1
envelope so the model and audit can distinguish measured from assumed values.

Prefill hint kinds (declared by each calc in ``prefill_hints``)
---------------------------------------------------------------
- ``cylinder_diameter``  — diameter of a named cylindrical face [mm]
- ``bbox_x`` / ``bbox_y`` / ``bbox_z``  — bounding-box dimension [mm]
- ``span``   — longest bounding-box dimension [mm]
- ``wall_thickness``  — local wall thickness at a face [mm]
- ``material_id``  — material id string assigned to the doc body

Each hint kind maps to a ``DocQuerier`` call; unresolvable slots are left for
the model/user and are NOT recorded in ``prefilled``.
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Protocol

# ---------------------------------------------------------------------------
# DocQuerier protocol
# ---------------------------------------------------------------------------

class DocQuerier(Protocol):
    """Injectable interface for doc geometry queries.

    Each method returns the measured value or ``None`` if unresolvable.
    Implementations wrap the real bridge (doc_eval_measure, doc_bbox, etc.)
    or a test stub.
    """

    def cylinder_diameter(self, doc_id: str, face_ref: Optional[str]) -> Optional[float]:
        """Diameter of a cylindrical face [mm].  ``face_ref`` e.g. ``"F7"``."""
        ...

    def bbox(self, doc_id: str, body_ref: Optional[str]) -> Optional[dict[str, float]]:
        """Bounding box {x_mm, y_mm, z_mm} of a body. ``body_ref`` optional."""
        ...

    def wall_thickness(self, doc_id: str, face_ref: Optional[str]) -> Optional[float]:
        """Local wall thickness at a face [mm]."""
        ...

    def material_id(self, doc_id: str, body_ref: Optional[str]) -> Optional[str]:
        """Material id string assigned to the doc body."""
        ...


# ---------------------------------------------------------------------------
# Bridge querier (wraps real engine bridge calls)
# ---------------------------------------------------------------------------

class BridgeQuerier:
    """Concrete DocQuerier backed by the C++ core RPCs (via ``core_bridge``).

    The bridge calls are imported lazily so this module loads without the built
    core present (e.g. in CI without libchudware). Every query is best-effort:
    any failure degrades to ``None`` (the advisor then skips that check).
    """

    def cylinder_diameter(self, doc_id: str, face_ref: Optional[str]) -> Optional[float]:
        if not face_ref:
            return None
        try:
            from services.mcp import core_bridge as cb
            result = cb.call("eval_measure", {"doc_id": doc_id, "refs": [face_ref]})
            d = result.get("diameter")
            return float(d) if d is not None else None
        except Exception:  # noqa: BLE001
            return None

    def bbox(self, doc_id: str, body_ref: Optional[str]) -> Optional[dict[str, float]]:
        try:
            from services.mcp import core_bridge as cb
            result = cb.call("doc_bbox", {"doc_id": doc_id})
            size = result.get("size")
            if size and len(size) == 3:
                return {"x_mm": float(size[0]), "y_mm": float(size[1]), "z_mm": float(size[2])}
        except Exception:  # noqa: BLE001
            pass
        return None

    def wall_thickness(self, doc_id: str, face_ref: Optional[str]) -> Optional[float]:
        try:
            from services.mcp import core_bridge as cb
            result = cb.call("eval_thickness", {"doc_id": doc_id})
            t = result.get("min_thickness_mm")
            return float(t) if t is not None else None
        except Exception:  # noqa: BLE001
            return None

    def material_id(self, doc_id: str, body_ref: Optional[str]) -> Optional[str]:
        # No C++ material-query RPC counterpart (the old engine bridge's doc_material
        # was not ported) — degrade to None so the advisor skips material checks.
        return None


_DEFAULT_QUERIER = BridgeQuerier()


# ---------------------------------------------------------------------------
# Prefill logic
# ---------------------------------------------------------------------------

def _resolve_hint(
    hint_kind: str,
    doc_id: str,
    refs: dict[str, Any],
    querier: DocQuerier,
) -> tuple[Optional[Any], str]:
    """Resolve a single prefill hint.  Returns (value_or_None, source_description)."""
    face_ref  = refs.get("face_ref")
    body_ref  = refs.get("body_ref")
    bbox_axis = {"bbox_x": "x_mm", "bbox_y": "y_mm", "bbox_z": "z_mm"}.get(hint_kind)

    if hint_kind == "cylinder_diameter":
        val = querier.cylinder_diameter(doc_id, face_ref)
        src = f"doc_eval_measure diameter face={face_ref or 'auto'}"
        return val, src

    if bbox_axis is not None:
        bbox = querier.bbox(doc_id, body_ref)
        if bbox is not None:
            val = bbox.get(bbox_axis)
            src = f"doc_bbox {bbox_axis} body={body_ref or 'auto'}"
            return val, src
        return None, ""

    if hint_kind == "span":
        bbox = querier.bbox(doc_id, body_ref)
        if bbox is not None:
            val = max(bbox["x_mm"], bbox["y_mm"], bbox["z_mm"])
            src = f"doc_bbox max(x,y,z) body={body_ref or 'auto'}"
            return val, src
        return None, ""

    if hint_kind == "wall_thickness":
        val = querier.wall_thickness(doc_id, face_ref)
        src = f"doc_eval_thickness face={face_ref or 'auto'}"
        return val, src

    if hint_kind == "material_id":
        val = querier.material_id(doc_id, body_ref)
        src = f"doc_material body={body_ref or 'auto'}"
        return val, src

    return None, ""


def prefill_inputs(
    calc_id: str,
    doc_id: str,
    refs: Optional[dict[str, Any]] = None,
    querier: Optional[DocQuerier] = None,
    user_inputs: Optional[dict[str, Any]] = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Resolve prefill hints for a calculator from live doc geometry.

    Parameters
    ----------
    calc_id :      Calculator id (must be registered in ``calcs`` registry).
    doc_id :       Engine doc session id.
    refs :         Optional geometry references: {face_ref, body_ref}.
    querier :      DocQuerier implementation (defaults to BridgeQuerier).
    user_inputs :  Inputs already provided by the user/model — these are NOT
                   overridden by prefill (user wins).

    Returns
    -------
    (prefilled_values, provenance)
        - prefilled_values: {param_name: value} for resolved slots.
        - provenance: {param_name: source_description} — goes into
          ``inputs.prefilled`` in the aicad.calc/1 envelope.

    Unresolvable slots or slots already in user_inputs are silently skipped.
    """
    from services.research.calcs.registry import get_calc  # local import

    q = querier if querier is not None else _DEFAULT_QUERIER
    r = refs or {}
    already = set(user_inputs or {})

    entry = get_calc(calc_id)
    if entry is None:
        return {}, {}

    hints: dict[str, str] = entry["prefill_hints"]
    prefilled_values: dict[str, Any] = {}
    provenance: dict[str, str] = {}

    for param, hint_kind in hints.items():
        if param in already:
            continue  # user-supplied; never overwrite
        val, src = _resolve_hint(hint_kind, doc_id, r, q)
        if val is not None:
            prefilled_values[param] = val
            provenance[param] = src

    return prefilled_values, provenance


def build_prefilled_inputs(
    calc_id: str,
    doc_id: str,
    user_inputs: dict[str, Any],
    refs: Optional[dict[str, Any]] = None,
    querier: Optional[DocQuerier] = None,
) -> tuple[dict[str, Any], Optional[dict[str, str]]]:
    """Merge user inputs with prefilled geometry values.

    Returns (merged_inputs, prefilled_provenance_or_None).
    Use ``prefilled_provenance`` as the ``prefilled`` arg to ``run_calc``.
    """
    pf_vals, pf_prov = prefill_inputs(
        calc_id, doc_id, refs=refs, querier=querier, user_inputs=user_inputs
    )
    merged = {**pf_vals, **user_inputs}  # user wins on conflict
    return merged, (pf_prov if pf_prov else None)
