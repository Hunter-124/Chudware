"""CatalogProvider adapter for ISO 1219 hydraulic components.

Enumerates the HYD_COMPONENT_TYPES enum (now core/hydraulic/schema.cpp).
One PartRecord per component type. Domain: "hydraulic".
geometry_ref.kind = "fmu_component".

The component_type names ARE the primary identifier; params carry type + pump/actuator
sub-enums where applicable (verbatim from the schema, as small common core).

Provenance / license: ISO 1219 component-type vocabulary (clean-room — enum
strings, not ISO text). See engine_oracle/hydraulic/schema.py and
contracts/licenses.md.
"""
from __future__ import annotations

import logging
from typing import Iterator

log = logging.getLogger(__name__)

# Human-readable titles for ISO 1219 component types.
_TITLES: dict[str, str] = {
    "pump":                 "Hydraulic pump (ISO 1219)",
    "cylinder_linear":      "Linear hydraulic cylinder / actuator (ISO 1219)",
    "cylinder_rotary":      "Rotary hydraulic cylinder / actuator (ISO 1219)",
    "valve_directional":    "Directional control valve (ISO 1219)",
    "valve_check":          "Check valve (ISO 1219)",
    "valve_relief":         "Relief / pressure-limiting valve (ISO 1219)",
    "valve_flow_control":   "Flow control valve (ISO 1219)",
    "accumulator":          "Hydraulic accumulator (ISO 1219)",
    "reservoir":            "Hydraulic reservoir / tank (ISO 1219)",
    "motor_hydraulic":      "Hydraulic motor (ISO 1219)",
    "filter":               "Hydraulic filter / strainer (ISO 1219)",
    "heat_exchanger":       "Hydraulic oil cooler / heat exchanger (ISO 1219)",
}

# Pump sub-type vocabulary (informational — carried in params)
_PUMP_TYPES = ("gear", "vane", "piston_axial", "piston_radial", "screw")
# Actuator sub-types
_ACTUATOR_TYPES = ("single_acting", "double_acting")
# Port vocabulary (informational)
_PORT_TYPES = ("pressure", "tank", "work_a", "work_b", "drain", "generic")


class HydraulicComponentsAdapter:
    """CatalogProvider for ISO 1219 hydraulic component types.

    Enumerates one PartRecord per component type. The enum source
    (engine_oracle/hydraulic/schema.py HYD_COMPONENT_TYPES) was ported to
    core/hydraulic/schema.cpp with no list-RPC — the hardcoded _TITLES list
    below is the catalog (same set the Python enum carried).
    """

    domain: str = "hydraulic"
    provider_id: str = "hydraulic_components"

    def _get_types(self) -> set:
        return set(_TITLES.keys())

    # ------------------------------------------------------------------
    # CatalogProvider protocol
    # ------------------------------------------------------------------

    def search(self, query: str, *,
               domain: str | None = None,
               filters: dict | None = None) -> list[dict]:
        if domain and domain != "hydraulic":
            return []
        norm = query.strip().lower()
        if not norm:
            return list(self.iter_records())
        results = []
        for rec in self.iter_records():
            title = (rec.get("title") or "").lower()
            designation = (rec.get("designation") or "").lower()
            if norm in title or norm in designation:
                results.append(rec)
        return results

    def get(self, part_id: str) -> dict | None:
        if not part_id or not part_id.startswith("hydraulic:"):
            return None
        rest = part_id[len("hydraulic:"):]
        # rest = "ISO1219:<component_type>"
        sub = rest.split(":", 1)
        if len(sub) != 2 or sub[0] != "ISO1219":
            return None
        ctype = sub[1]
        types = self._get_types()
        if ctype not in types:
            return None
        return _make_record(ctype)

    def count(self) -> int:
        return len(self._get_types())

    def iter_records(self) -> Iterator[dict]:
        for ctype in sorted(self._get_types()):
            yield _make_record(ctype)


# ---------------------------------------------------------------------------
# Record builder
# ---------------------------------------------------------------------------

def _make_record(component_type: str) -> dict:
    title = _TITLES.get(component_type,
                        f"Hydraulic component: {component_type} (ISO 1219)")
    designation = component_type

    params: dict = {
        "nominal_size": component_type,
        "standard":     "ISO 1219",
        "component_type": component_type,
    }
    # Attach relevant sub-enum vocabularies verbatim
    if component_type == "pump":
        params["pump_types"] = list(_PUMP_TYPES)
    elif component_type in ("cylinder_linear", "cylinder_rotary"):
        params["actuator_types"] = list(_ACTUATOR_TYPES)
    params["port_types"] = list(_PORT_TYPES)

    return {
        "part_id":      f"hydraulic:ISO1219:{component_type}",
        "domain":       "hydraulic",
        "title":        title,
        "standard":     "ISO 1219",
        "designation":  designation,
        "params":       params,
        "geometry_ref": {
            "kind":         "fmu_component",
            "component_id": component_type,
        },
        "sourcing":     [],
        "provenance":   (
            "ISO 1219 hydraulic component-type vocabulary (clean-room — type name strings, "
            "not ISO text). Source: HYD_COMPONENT_TYPES (now core/hydraulic/schema.cpp). "
            "See contracts/licenses.md."
        ),
        "license":      "clean-room",
    }
