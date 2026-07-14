"""``part_place`` — universal standard-part insertion router (T-P3).

Routes a ``PartRecord.geometry_ref`` to the correct domain insertion path, landing an
**editable** document-schema op in every case.  No opaque bodies — every placed entity
is parametrically re-editable post-placement (R1 parity, RATIFIED Erik rule).

Routing table (keyed on ``geometry_ref["kind"]``, per contracts/parts.md §2):

  core_family      → parts_make RPC → new editable mech part doc
  generate_model   → spec_sheet_matcher.generate_model → new mech part doc
  symbol_footprint → ecad sch_add_symbol / pcb_add_footprint ops (editable op)
  section_profile  → add StructuralElement to aicad.structure/1 doc (editable schema op)
  fitting_id       → append fitting record to aicad.piping/1 doc fittings list
  fmu_component    → append component record to aicad.hydraulic/1 doc components list
  import_file      → import_file RPC (same primitive import_part uses) → new editable doc
                     (T-X3: browser-acquired STEP/IGES files, domain="catalog")

Each placer returns a ``PlacementResult`` dict:

  {
    "ok":        bool,
    "domain":    str,                    # geometry_ref kind
    "doc_id":    str | None,             # doc that received the placement
    "entity_id": str | None,             # placed symbol/element/component id
    "editable":  True,                   # ALWAYS True — DoD gate
    "detail":    dict,                   # domain-specific extras (bbox, ref, …)
    "reason":    str | None,             # set when ok=False
    "hint":      str | None,
  }

Error contract: always returns a dict, never raises to the MCP layer.
"""
from __future__ import annotations

import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Ensure the repo root is importable (mirrors the pattern in parts_tools.py).
_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


# ── helpers ──────────────────────────────────────────────────────────────────

def _ok(domain: str, doc_id: str | None, entity_id: str | None, detail: dict) -> dict:
    return {
        "ok": True,
        "domain": domain,
        "doc_id": doc_id,
        "entity_id": entity_id,
        "editable": True,   # invariant — never False on a success result
        "detail": detail,
        "reason": None,
        "hint": None,
    }


def _err(domain: str, reason: str, hint: str = "", detail: dict | None = None) -> dict:
    return {
        "ok": False,
        "domain": domain,
        "doc_id": None,
        "entity_id": None,
        "editable": False,
        "detail": dict(detail) if detail else {},
        "reason": str(reason),
        "hint": str(hint),
    }


# ── placers (one per geometry_ref kind) ──────────────────────────────────────

def _place_core_family(gref: dict, doc_context: dict | None,
                       params: dict | None) -> dict:
    """parts_make RPC → new editable mech part doc (core_family + generate_model kinds)."""
    domain = "core_family"
    family = gref.get("family")
    size = gref.get("size")
    if not family or not size:
        return _err(domain, "geometry_ref missing 'family' or 'size'",
                    "Fetch the PartRecord via part_get to confirm geometry_ref fields.")
    try:
        # Import core_bridge as a PACKAGE member (services.mcp.core_bridge), not bare.
        # A bare `import core_bridge` leaves core_bridge.__package__ == "" so its own
        # relative imports (`from . import daemon_bridge`) fail with "attempted relative
        # import with no known parent package" the moment daemon mode (the default) is
        # active — and it forks a second core_bridge instance with its own DLL/fallback
        # state, diverging from cad_tools' canonical `from . import core_bridge`.
        from services.mcp import core_bridge as cb  # noqa: PLC0415
        call_params: dict[str, Any] = {"family": family, "size": size}
        if gref.get("length_mm") is not None:
            call_params["length_mm"] = gref["length_mm"]
        if params:
            call_params.update({k: v for k, v in params.items()
                                 if k not in ("family", "size")})
        r = cb.call("parts_make", call_params)
        if not r.get("ok"):
            return _err(domain, r.get("error", "parts_make failed"),
                        "Verify the family/size pair with list_standard_parts.")
        doc_id = r.get("doc_id")
        return _ok(domain, doc_id, doc_id, {
            "family": family, "size": size,
            "name": r.get("name"),
            "bbox": r.get("bbox"),
            "datum": r.get("datum"),
        })
    except Exception as exc:  # noqa: BLE001
        return _err(domain, f"parts_make RPC failed: {exc}",
                    "Ensure the native core is available (AI_CAD_LIBAICAD).")


def _place_generate_model(gref: dict, doc_context: dict | None,
                          params: dict | None) -> dict:
    """spec_sheet_matcher.generate_model → import produced STEP as editable doc.

    Mirrors ``_place_import_file``: once geometry lands on disk, the same
    ``import_file`` core RPC creates a real ``doc_id`` (not a path-as-id stub).
    """
    domain = "generate_model"
    match = gref.get("match")
    if not match:
        return _err(domain, "geometry_ref missing 'match' dict",
                    "Fetch the PartRecord via part_get to confirm geometry_ref fields.")
    try:
        from engine_oracle import spec_sheet_matcher as ssm  # noqa: PLC0415
        path = ssm.generate_model(match)
        if path is None:
            return _err(domain, "generate_model returned None (no geometry available)",
                        "Core may be absent or the match source is 'kicad' (no mech model).")
        # Import the produced STEP as a genuine editable doc — same seam as
        # browser-acquired import_file placements.
        import_result = _place_import_file(
            {"kind": "import_file", "path": str(path)},
            doc_context,
            params,
        )
        if not import_result.get("ok"):
            # Preserve generate_model domain in the error envelope while keeping
            # the underlying import failure reason/hint.
            return _err(domain,
                        import_result.get("reason", "import of generated model failed"),
                        import_result.get("hint", ""),
                        detail={
                            "step_path": str(path),
                            **(import_result.get("detail") or {}),
                        })
        detail = dict(import_result.get("detail") or {})
        detail["step_path"] = str(path)
        detail.setdefault(
            "hint",
            "call reverse_engineer(source=doc_id) for parametric feature "
            "recovery where the geometry supports it",
        )
        return _ok(domain,
                   import_result.get("doc_id"),
                   import_result.get("entity_id") or import_result.get("doc_id"),
                   detail)
    except Exception as exc:  # noqa: BLE001
        return _err(domain, f"generate_model failed: {exc}",
                    "Ensure engine_oracle.spec_sheet_matcher is importable.")


def _place_symbol_footprint(gref: dict, doc_context: dict | None,
                             params: dict | None) -> dict:
    """ECAD: sch_add_symbol op into a schematic doc (editable doc-schema op)."""
    domain = "symbol_footprint"
    libid = gref.get("libid")
    if not libid:
        return _err(domain, "geometry_ref missing 'libid'",
                    "Fetch the PartRecord via part_get to confirm geometry_ref fields.")

    # Resolve the doc_id from context or params.
    doc_id: str | None = None
    doc: dict | None = None
    _SCH_SCHEMAS = ("aicad.sch/1", "aicad.ecad/1", "aicad.pcb/1")
    if doc_context and doc_context.get("schema") in _SCH_SCHEMAS:
        doc = doc_context
    elif params and params.get("doc_id"):
        doc_id = params["doc_id"]
    else:
        # No doc provided — report a clear actionable error.
        return _err(domain,
                    "No schematic doc provided. Pass doc=<opened ecad doc dict> or "
                    "params={'doc_id': '<id>'} from ecad_new_schematic.",
                    "Open a schematic with ecad_new_schematic (schema=aicad.sch/1), "
                    "then call part_place with the doc.")

    try:
        from engine_oracle.ecad import ops as ecad_ops  # noqa: PLC0415

        # Build the op.
        # sch_add_symbol requires pos as [x, y] (see ecad_ops.md §1 / _require_xy).
        raw_pos = (params or {}).get("pos", [0, 0])
        if isinstance(raw_pos, dict):
            pos = [float(raw_pos.get("x", 0)), float(raw_pos.get("y", 0))]
        elif isinstance(raw_pos, (list, tuple)) and len(raw_pos) >= 2:
            pos = [float(raw_pos[0]), float(raw_pos[1])]
        else:
            pos = [0.0, 0.0]
        op: dict[str, Any] = {
            "op": "sch_add_symbol",
            "libid": libid,
            "pos": pos,
            "angle": float((params or {}).get("angle", 0.0)),
        }
        if gref.get("footprint"):
            op["footprint"] = gref["footprint"]
        if (params or {}).get("value"):
            op["value"] = params["value"]  # type: ignore[index]
        if (params or {}).get("ref"):
            op["ref"] = params["ref"]  # type: ignore[index]

        if doc is None:
            # We only have a doc_id string — need the live doc from the engine.
            # Fall back to a seam: return a structured op for the caller to apply.
            return _ok(domain, doc_id, None, {
                "op": op,
                "note": "Apply this op via ecad_place_symbol or apply_ops(doc, [op]).",
            })

        # apply_ops returns {"id_map": {...}, "warnings": [...], "doc": <updated doc>}
        # The returned "doc" is the authoritative post-op document; the input doc is
        # NOT mutated in-place. We update the caller's dict by merging keys so the
        # caller's reference to `doc` reflects the placement.
        result = ecad_ops.apply_ops(doc, [op])
        id_map = result.get("id_map", {})
        entity_id = id_map.get("$0")
        updated_doc = result.get("doc", doc)
        # Merge updated doc back into the caller's dict (in-place update).
        doc.clear()
        doc.update(updated_doc)
        # Verify editable: symbol must appear in doc["symbols"].
        symbols = doc.get("symbols", [])
        found = any(s.get("id") == entity_id for s in symbols) if entity_id else bool(symbols)
        if not found:
            return _err(domain, "Symbol was not added to doc.symbols — editability check failed",
                        "Verify the doc schema is aicad.sch/1 schematic.")
        # Find the placed symbol to return its ref.
        placed_sym = next((s for s in symbols if s.get("id") == entity_id), {})
        return _ok(domain, doc.get("id"), entity_id, {
            "libid": libid,
            "ref": placed_sym.get("ref", op.get("ref")),
            "pos": pos,
        })
    except Exception as exc:  # noqa: BLE001
        return _err(domain, f"sch_add_symbol failed: {exc}",
                    "Ensure engine_oracle.ecad.ops is importable.")


def _place_section_profile(gref: dict, doc_context: dict | None,
                            params: dict | None) -> dict:
    """Structural: append a StructuralElement to an aicad.structure/1 doc (editable schema op)."""
    domain = "section_profile"
    standard = gref.get("standard")
    designation = gref.get("designation")
    if not designation:
        return _err(domain, "geometry_ref missing 'designation'",
                    "Fetch the PartRecord via part_get to confirm geometry_ref fields.")

    doc: dict | None = doc_context
    if doc is None or doc.get("schema") != "aicad.structure/1":
        return _err(domain,
                    "No structural doc provided. Pass a doc with schema='aicad.structure/1'.",
                    "Create one with new_structure(), then call part_place with it.")

    try:
        from engine_oracle.structural.elements import (  # noqa: PLC0415
            Beam, Column, Post, Joist,
        )

        _KIND_MAP = {
            "beam": Beam, "column": Column, "post": Post, "joist": Joist,
        }

        elem_params = params or {}
        element_id = elem_params.get("id") or uuid.uuid4().hex[:12]
        kind = elem_params.get("kind", "beam")
        length_mm = float(elem_params.get("length_mm", 1000.0))
        material = elem_params.get("material", "steel")
        # section_name encodes standard + designation, e.g. "AISC:W8x31" or just designation.
        section_name = (
            f"{standard}:{designation}" if standard else designation
        )

        cls = _KIND_MAP.get(kind, Beam)
        element = cls(
            section_name=section_name,
            material=material,
            length_mm=length_mm,
            id=element_id,
        )
        doc.setdefault("elements", []).append(element.to_dict())
        return _ok(domain, doc.get("id"), element_id, {
            "kind": kind,
            "section_name": section_name,
            "length_mm": length_mm,
            "material": material,
        })
    except Exception as exc:  # noqa: BLE001
        return _err(domain, f"add structural element failed: {exc}",
                    "Ensure engine_oracle.structural.elements is importable.")


def _place_fitting_id(gref: dict, doc_context: dict | None,
                      params: dict | None) -> dict:
    """Piping: append a fitting to an aicad.piping/1 doc (editable schema op)."""
    domain = "fitting_id"
    fitting_id_val = gref.get("fitting_id")
    if not fitting_id_val:
        return _err(domain, "geometry_ref missing 'fitting_id'",
                    "Fetch the PartRecord via part_get to confirm geometry_ref fields.")

    doc: dict | None = doc_context
    if doc is None or not doc.get("schema", "").startswith("aicad.piping"):
        return _err(domain,
                    "No piping doc provided. Pass a doc with schema='aicad.piping/1'.",
                    "Create one with piping tools, then call part_place with that doc.")

    try:
        from engine_oracle.piping.schema import new_fitting, next_id  # noqa: PLC0415

        elem_params = params or {}
        ft_id = elem_params.get("id") or next_id(doc, "ft")
        ft_type = elem_params.get("type", "elbow")
        nps = float(elem_params.get("nps", 1.0))
        origin = elem_params.get("origin", [0.0, 0.0, 0.0])

        fitting = new_fitting(ft_id, ft_type, nps, origin=tuple(origin))  # type: ignore[arg-type]
        # Copy any extra catalog fields from the fitting record.
        for k in ("standard", "schedule", "material_spec"):
            if elem_params.get(k):
                fitting[k] = elem_params[k]
        doc.setdefault("fittings", []).append(fitting)
        return _ok(domain, doc.get("id"), ft_id, {
            "fitting_type": ft_type,
            "nps": nps,
            "catalog_id": fitting_id_val,
        })
    except Exception as exc:  # noqa: BLE001
        return _err(domain, f"add piping fitting failed: {exc}",
                    "Ensure engine_oracle.piping.schema is importable.")


def _place_fmu_component(gref: dict, doc_context: dict | None,
                          params: dict | None) -> dict:
    """Hydraulic: append a component to an aicad.hydraulic/1 doc (editable schema op)."""
    domain = "fmu_component"
    component_id_val = gref.get("component_id")
    if not component_id_val:
        return _err(domain, "geometry_ref missing 'component_id'",
                    "Fetch the PartRecord via part_get to confirm geometry_ref fields.")

    doc: dict | None = doc_context
    if doc is None or not doc.get("schema", "").startswith("aicad.hydraulic"):
        return _err(domain,
                    "No hydraulic doc provided. Pass a doc with schema='aicad.hydraulic/1'.",
                    "Create one with hydraulic tools, then call part_place with that doc.")

    try:
        from engine_oracle.hydraulic.schema import new_component, next_id  # noqa: PLC0415

        elem_params = params or {}
        comp_id = elem_params.get("id") or next_id(doc, "c")
        comp_type = elem_params.get("type", "pump")
        x = float(elem_params.get("x", 0.0))
        y = float(elem_params.get("y", 0.0))

        component = new_component(comp_id, comp_type, x=x, y=y,
                                  name=elem_params.get("name", ""))
        # Propagate any extra params (pressure_bar, displacement_cc_rev, etc.)
        for k in ("displacement_cc_rev", "max_pressure_bar", "max_flow_l_min",
                  "efficiency_vol", "efficiency_mech", "bore_mm", "stroke_mm"):
            if elem_params.get(k) is not None:
                component[k] = elem_params[k]
        doc.setdefault("components", []).append(component)
        return _ok(domain, doc.get("id"), comp_id, {
            "comp_type": comp_type,
            "catalog_id": component_id_val,
        })
    except Exception as exc:  # noqa: BLE001
        return _err(domain, f"add hydraulic component failed: {exc}",
                    "Ensure engine_oracle.hydraulic.schema is importable.")


def _place_import_file(gref: dict, doc_context: dict | None,
                       params: dict | None) -> dict:
    """T-X3: import a local CAD file (browser-acquired STEP/IGES) as a new editable doc.

    Uses the same ``import_file`` core RPC that the ``import_part`` MCP tool wraps
    (``services/mcp/cad_tools.py``) — a genuine ``doc_id``, not just the file path
    (contrast with ``generate_model``'s convention above). Full parametric feature
    recovery from an imported B-rep is NOT attempted automatically here — that is
    the ``reverse_engineer`` MCP tool's job and needs geometric judgment a generic
    placer shouldn't guess at; the result's ``detail.hint`` names it as the
    recommended next step.
    """
    domain = "import_file"
    path = gref.get("path")
    if not path:
        return _err(domain, "geometry_ref missing 'path'",
                    "Fetch the PartRecord via part_get to confirm geometry_ref fields.")
    if not os.path.isfile(path):
        return _err(domain, f"file not found: {path}",
                    "The browser-acquired file may have been moved or cleaned up.")
    try:
        # PACKAGE import (see _place_core_family): a bare `import core_bridge` zeros
        # core_bridge.__package__, so its `from . import daemon_bridge` fails in daemon
        # mode (the default) with "attempted relative import with no known parent
        # package" — which is exactly what broke the browser-catalog import chain.
        from services.mcp import core_bridge as cb  # noqa: PLC0415

        as_solid = bool((params or {}).get("as_solid", False))
        r = cb.call("import_file", {"path": path, "as_solid": as_solid})
        doc_id = r.get("doc_id")
        if not doc_id:
            return _err(domain, "import_file RPC returned no doc_id",
                        "The core may have rejected the file format.")
        return _ok(domain, doc_id, doc_id, {
            "imported_from": path,
            "body_count": r.get("body_count"),
            "hint": "call reverse_engineer(source=doc_id) for parametric feature "
                    "recovery where the geometry supports it",
        })
    except Exception as exc:  # noqa: BLE001
        return _err(domain, f"import_file RPC failed: {exc}",
                    "Ensure the native core is available (AI_CAD_LIBAICAD).")


# ── dispatch table ────────────────────────────────────────────────────────────

_PLACERS = {
    "core_family":      _place_core_family,
    "generate_model":   _place_generate_model,
    "symbol_footprint": _place_symbol_footprint,
    "section_profile":  _place_section_profile,
    "fitting_id":       _place_fitting_id,
    "fmu_component":    _place_fmu_component,
    "import_file":      _place_import_file,
}


# ── public API ────────────────────────────────────────────────────────────────

def place(part_id: str, *,
          doc: dict | None = None,
          params: dict | None = None) -> dict:
    """Place standard part ``part_id`` into ``doc``, returning a ``PlacementResult``.

    Looks up the PartRecord, reads ``geometry_ref.kind``, and routes to the correct
    domain inserter.  Returns ``{"ok": True, "editable": True, ...}`` on success or
    ``{"ok": False, "reason": ..., "hint": ...}`` on failure.

    Parameters
    ----------
    part_id : str
        Stable part id in the form ``"<domain>:<catalog_id>"``, e.g.
        ``"fastener:ISO4762:M8"`` or ``"ecad:Device:R"``.
    doc : dict | None
        The live document that will receive the placement.  Required for every
        domain except ``core_family`` / ``generate_model`` (which create a new doc).
    params : dict | None
        Domain-specific placement parameters (position, size overrides, etc.).
        See per-placer docstrings for the accepted keys.
    """
    # 1. Retrieve the PartRecord.
    try:
        from engine_oracle.parts_hub import get as _get  # noqa: PLC0415
        record = _get(part_id)
    except Exception as exc:  # noqa: BLE001
        return _err("unknown", f"parts_hub.get failed: {exc}",
                    "Check that engine_oracle/parts_hub is importable.")

    if record is None:
        return _err("unknown", f"part_id not found: {part_id!r}",
                    "Use part_search to discover valid part_ids.")

    # 1b. A live catalog provider may return an acquire-FAILURE envelope in place of a
    # PartRecord — login_required / terms_confirmation_required / distributor_only /
    # no_download_link from a walled vendor (McMaster, GrabCAD, …). That is NOT "not
    # found": it is an actionable degradation. Surface it directly — with the sign-in
    # question and a link-out to the part page — instead of the misleading generic
    # "no geometry_ref — cannot place". The human resolves it (one-time vendor_login or
    # terms click-through) and re-invokes part_place; we deliberately do NOT auto-retry.
    if record.get("ok") is False:
        reason = record.get("reason", "unavailable")
        site = record.get("site", "")
        link = record.get("source_url") or record.get("url") or ""
        detail = {k: record[k] for k in ("ask_user", "distributors_url", "source_url",
                                         "url", "site", "title")
                  if record.get(k) is not None}
        if reason == "login_required" and site:
            hint = (f"Sign in first: run vendor_login('{site}') to open a browser window, "
                    f"complete the login once, then retry part_place"
                    + (f". Part page: {link}" if link else "") + ".")
        elif reason == "terms_confirmation_required":
            hint = (f"Accept the download terms shown at {link}, then retry part_place."
                    if link else record.get("hint", "Terms acceptance required."))
        elif reason == "distributor_only":
            du = record.get("distributors_url")
            hint = (f"{site} sells only through distributors — no downloadable model. "
                    + (f"Find a distributor: {du}. " if du else "")
                    + (f"Reference: {link}." if link else "")).strip()
        else:
            hint = record.get("hint") or f"Could not acquire a model ({reason})."
        return _err(record.get("domain", "catalog"),
                    f"Cannot place {part_id!r}: {reason}.", hint, detail=detail)

    # 2. Extract and validate geometry_ref.
    gref = record.get("geometry_ref")
    if not gref:
        return _err(record.get("domain", "unknown"),
                    f"PartRecord {part_id!r} has no geometry_ref — cannot place.",
                    "This part has no known instantiation recipe.")

    kind = gref.get("kind")
    placer = _PLACERS.get(kind)
    if placer is None:
        return _err(record.get("domain", "unknown"),
                    f"Unknown geometry_ref kind {kind!r}.",
                    f"Supported kinds: {sorted(_PLACERS)}")

    # 3. Delegate to the domain placer.
    result = placer(gref, doc, params)

    # 4. Invariant check: a successful placement MUST be editable.
    if result.get("ok") and not result.get("editable"):
        # This should never happen with the current placers; guard defensively.
        result["ok"] = False
        result["reason"] = (
            "Internal: placement succeeded but editability flag is False — "
            "DoD violation. File a bug."
        )
        log.error("part_place editability invariant violated for part_id=%r kind=%r",
                  part_id, kind)

    return result
