"""Schema validation + factories for ``aicad.hydraulic/1`` documents.

Executable form of ``contracts/hydraulic_schema.md``. Structural validation only (envelopes,
types, id prefixes, enum membership) — physics sizing is a separate pass (:mod:`engine.hydraulic.solve`),
exactly as engine.piping keeps DRC/3D out of its schema validator.

Every ``validate_*`` returns a list of error strings (empty = valid), each prefixed with a
JSON-path-ish locator so failures are actionable — same convention as engine.piping.schema and
engine.ecad.schema.

Coordinates are ``[x, y]`` in **mm** (schematic canvas); pressures in **bar**; flows in **L/min**.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

SCHEMA_HYD = "aicad.hydraulic/1"

# ── enums (module constants so solve.py / bom.py share one definition) ─────────

# ISO 1219 component types
HYD_COMPONENT_TYPES = {
    "pump",
    "cylinder_linear",
    "cylinder_rotary",
    "valve_directional",
    "valve_check",
    "valve_relief",
    "valve_flow_control",
    "accumulator",
    "reservoir",
    "motor_hydraulic",
    "filter",
    "heat_exchanger",
}

HYD_PORT_TYPES = {"pressure", "tank", "work_a", "work_b", "drain", "generic"}

HYD_ACTUATOR_TYPES = {"single_acting", "double_acting"}

HYD_PUMP_TYPES = {"gear", "vane", "piston_axial", "piston_radial", "screw"}

# id prefix -> the document list key it indexes (used by next_id + validators)
_PREFIXES = {"c": "components", "h": "hoses"}


# ── factories ────────────────────────────────────────────────────────────────

def new_hydraulic(name: str = "Hydraulic Circuit") -> dict:
    """Create a new empty ``aicad.hydraulic/1`` document envelope."""
    return {
        "schema": SCHEMA_HYD,
        "id": str(uuid.uuid4()),
        "name": name,
        "units": {
            "pressure": "bar",
            "flow": "L/min",
            "force": "kN",
            "position": "mm",
        },
        "components": [],
        "hoses": [],
        "annotations": [],
    }


def new_component(id: str, type: str, *, name: str = "", x: float = 0.0,
                  y: float = 0.0, **kw) -> dict:
    """Create a new hydraulic component dict.

    ``type`` must be a member of :data:`HYD_COMPONENT_TYPES`.
    Extra keyword arguments are merged into ``params``.
    """
    comp: dict[str, Any] = {
        "id": id,
        "type": type,
        "name": name or type,
        "position": [float(x), float(y)],
        "angle": 0.0,
        "ports": [],
        "params": {},
        "sourcing": {
            "mpn": None,
            "manufacturer": None,
            "description": None,
            "quantity": 1,
            "unit": "ea",
            "category": None,
        },
    }
    # Absorb recognised top-level fields; everything else goes into params
    top_keys = {"angle", "ports"}
    for k, v in kw.items():
        if k in top_keys:
            comp[k] = v
        else:
            comp["params"][k] = v
    return comp


def new_hose(id: str, net_name: str, connections: list) -> dict:
    """Create a new hose/line net dict.

    ``connections`` is a list of ``{"component_id": ..., "port": ...}`` dicts.
    """
    return {
        "id": id,
        "net_name": net_name,
        "connections": list(connections),
    }


def is_hydraulic(doc: dict) -> bool:
    """Return True if *doc* is an ``aicad.hydraulic/1`` document."""
    return isinstance(doc, dict) and doc.get("schema") == SCHEMA_HYD


def next_id(doc: dict, prefix: str) -> str:
    """Allocate the next unused ``<prefix><n>`` id (never reuses; scans current ids).

    ``prefix`` must be ``"c"`` (components) or ``"h"`` (hoses).
    """
    key = _PREFIXES.get(prefix)
    items = doc.get(key, []) if key else []
    n = 0
    pat = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    for it in items:
        m = pat.match(it.get("id", "")) if isinstance(it, dict) else None
        if m:
            n = max(n, int(m.group(1)))
    return f"{prefix}{n + 1}"


# ── validation helpers ─────────────────────────────────────────────────────────

def _is_xy(v: Any) -> bool:
    return (isinstance(v, (list, tuple)) and len(v) == 2
            and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in v))


def _check_ids(items: list, prefix: str, loc: str, errs: list[str]) -> None:
    seen: set[str] = set()
    pat = re.compile(rf"^{re.escape(prefix)}\d+$")
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            errs.append(f"{loc}[{i}]: not an object")
            continue
        oid = it.get("id")
        if not isinstance(oid, str) or not pat.match(oid):
            errs.append(
                f"{loc}[{i}].id: missing or bad prefix (want {prefix}<n>) got {oid!r}"
            )
        elif oid in seen:
            errs.append(f"{loc}[{i}].id: duplicate {oid!r}")
        else:
            seen.add(oid)


def validate_component(comp: dict, loc: str = "component") -> list[str]:
    """Validate a single component object. Returns a list of error strings."""
    errs: list[str] = []
    if not isinstance(comp, dict):
        return [f"{loc}: not an object"]

    ctype = comp.get("type")
    if ctype not in HYD_COMPONENT_TYPES:
        errs.append(f"{loc}.type: {ctype!r} not in HYD_COMPONENT_TYPES")

    pos = comp.get("position")
    if not _is_xy(pos):
        errs.append(f"{loc}.position: expected [x, y] numbers, got {pos!r}")

    angle = comp.get("angle")
    if angle is not None and (isinstance(angle, bool)
                              or not isinstance(angle, (int, float))):
        errs.append(f"{loc}.angle: must be a number (degrees), got {angle!r}")

    ports = comp.get("ports", [])
    if not isinstance(ports, list):
        errs.append(f"{loc}.ports: must be a list")
    else:
        for j, port in enumerate(ports):
            if not isinstance(port, dict):
                errs.append(f"{loc}.ports[{j}]: not an object")
                continue
            pt = port.get("port_type")
            if pt not in HYD_PORT_TYPES:
                errs.append(f"{loc}.ports[{j}].port_type: {pt!r} not in HYD_PORT_TYPES")

    params = comp.get("params")
    if params is not None and not isinstance(params, dict):
        errs.append(f"{loc}.params: must be an object or omitted")

    sourcing = comp.get("sourcing")
    if sourcing is not None and not isinstance(sourcing, dict):
        errs.append(f"{loc}.sourcing: must be an object or omitted")

    return errs


def validate_hose(hose: dict, component_ids: set[str], loc: str = "hose") -> list[str]:
    """Validate a single hose object. Returns a list of error strings."""
    errs: list[str] = []
    if not isinstance(hose, dict):
        return [f"{loc}: not an object"]

    conns = hose.get("connections")
    if not isinstance(conns, list):
        errs.append(f"{loc}.connections: must be a list")
    else:
        for j, conn in enumerate(conns):
            if not isinstance(conn, dict):
                errs.append(f"{loc}.connections[{j}]: not an object")
                continue
            cid = conn.get("component_id")
            if cid is not None and cid not in component_ids:
                errs.append(
                    f"{loc}.connections[{j}].component_id: {cid!r} not a known component"
                )
            if "port" not in conn:
                errs.append(f"{loc}.connections[{j}].port: required")

    return errs


def validate_hydraulic(doc: dict) -> list[str]:
    """Validate an ``aicad.hydraulic/1`` document. Returns a list of error strings.

    Empty list means the document is structurally valid. Uses the same JSON-path-prefixed
    error convention as engine.piping.schema.validate_piping.
    """
    errs: list[str] = []
    if not isinstance(doc, dict):
        return ["doc: not an object"]
    if doc.get("schema") != SCHEMA_HYD:
        return [f"schema: expected {SCHEMA_HYD!r} got {doc.get('schema')!r}"]

    if not isinstance(doc.get("id"), str):
        errs.append("id: missing or not a string")

    components = doc.get("components", [])
    hoses = doc.get("hoses", [])

    if not isinstance(components, list):
        errs.append("components: must be a list")
        components = []
    if not isinstance(hoses, list):
        errs.append("hoses: must be a list")
        hoses = []

    _check_ids(components, "c", "components", errs)
    _check_ids(hoses, "h", "hoses", errs)

    for i, comp in enumerate(components):
        errs.extend(validate_component(comp, f"components[{i}]"))

    # Build the set of known component ids for hose reference checks
    comp_ids = {c.get("id") for c in components if isinstance(c, dict)}
    for i, hose in enumerate(hoses):
        errs.extend(validate_hose(hose, comp_ids, f"hoses[{i}]"))

    return errs


# Convenience alias
def validate(doc: dict) -> list[str]:
    return validate_hydraulic(doc)
