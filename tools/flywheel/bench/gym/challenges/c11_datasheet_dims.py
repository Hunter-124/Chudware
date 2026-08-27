"""C11 — dimension-from-datasheet: derive seat/hole dims from a datasheet block.

Request embeds a text datasheet (LOCAL) specifying shaft Ø, bearing 6202, bolt M5×0.8,
and a PCD formula. Seat must match 6202 OD (35.0); M5 clearance hole in [5.3, 5.8];
calc/research must precede the seat dimension; dimension provenance non-empty.

Ground-truth ACs the gym grades ITSELF:
  AC1  seat dim == 35.0 (6202 OD) via ledger_dimension
  AC2  calc_bolt_circle decision OR calc/research tool_call precedes seat dimension (seq)
  AC3  bolt_circle.hole_d in [5.3, 5.8] (M5 clearance)
  AC4  battery PASS
  AC5  dimension events carry non-empty provenance

Seeds:
  1  clean PASS (research/calc → dims with provenance → battery)
  2  tool_misuse / no-calc-before-dim — seat dim without prior calc (AC2 FAIL)
  3  context_loss — cites nonexistent DIM99; also empty provenance on a dim
"""
from __future__ import annotations

from . import (Challenge, AcceptanceCheck, ledger_dimension, ledger_artifact,
               ledger_decision_value, coerce_number)

SEAT_NOM = 35.0                     # mm, 6202 outer diameter
M5_CLEARANCE_BAND = (5.3, 5.8)      # mm, close-to-normal M5 clearance
BOLT_HOLES = 4

_CALC_TOOLS = frozenset({
    "research_thread_spec", "research_standard_section", "part_search",
})


def _events(ledger: dict) -> list[dict]:
    return list(ledger.get("events", []) or [])


def _tool_calls(ledger: dict) -> list[tuple[int, str, dict]]:
    out: list[tuple[int, str, dict]] = []
    for e in _events(ledger):
        p = e.get("payload") or {}
        if e.get("kind") == "tool_call":
            tool = str(p.get("tool") or "")
            if tool:
                out.append((int(e.get("seq") or 0), tool, p))
        elif e.get("kind") == "agent_turn" and p.get("event") == "tool_call":
            tool = str(p.get("tool") or "")
            if tool:
                out.append((int(e.get("seq") or 0), tool, p))
    return out


def _first_seat_dim_seq(ledger: dict) -> int | None:
    for e in _events(ledger):
        if e.get("kind") != "dimension":
            continue
        p = e.get("payload") or {}
        name = str(p.get("name") or "").lower()
        if "seat" in name or "bearing" in name:
            return int(e.get("seq") or 0)
    return None


# ---- ground-truth graders ---------------------------------------------------

def _grade_seat(ledger: dict, ctx: dict) -> "tuple[str,str]":
    dim = ledger_dimension(ledger, "seat") or ledger_dimension(ledger, "bearing")
    if not dim:
        return "FAIL", "no seat/bearing dimension recorded in ledger"
    val = coerce_number(dim.get("value", 0))
    if val is None:
        return "FAIL", f"seat value {dim.get('value')!r} is not numeric"
    if abs(val - SEAT_NOM) > 1e-6:
        return "FAIL", f"seat value {dim.get('value')} != {SEAT_NOM} (6202 OD)"
    return "PASS", f"seat d{SEAT_NOM} (6202 OD)"


def _grade_calc_before_seat(ledger: dict, ctx: dict) -> "tuple[str,str]":
    """AC2: calc decision or calc/research tool precedes seat dimension event."""
    seat_seq = _first_seat_dim_seq(ledger)
    if seat_seq is None:
        return "FAIL", "no seat dimension event to order against"

    for e in _events(ledger):
        if e.get("kind") != "decision":
            continue
        p = e.get("payload") or {}
        did = str(p.get("id") or "")
        if did in ("calc_bolt_circle", "calc_seat", "calc_from_datasheet"):
            if int(e.get("seq") or 0) < seat_seq:
                return "PASS", f"decision {did}@seq={e.get('seq')} before seat@seq={seat_seq}"

    for seq, tool, _p in _tool_calls(ledger):
        if tool in _CALC_TOOLS and seq < seat_seq:
            return "PASS", f"tool {tool}@seq={seq} before seat@seq={seat_seq}"
        if tool.startswith("research_") and seq < seat_seq:
            return "PASS", f"tool {tool}@seq={seq} before seat@seq={seat_seq}"

    return "FAIL", f"no calc/research decision or tool before seat dimension seq={seat_seq}"


def _grade_m5_clearance(ledger: dict, ctx: dict) -> "tuple[str,str]":
    hole_d = ledger_decision_value(ledger, "bolt_circle", "hole_d")
    hole_n = coerce_number(hole_d)
    if hole_n is None:
        return "FAIL", "no bolt_circle.hole_d decision value"
    lo, hi = M5_CLEARANCE_BAND
    if not (lo <= hole_n <= hi):
        return "FAIL", f"hole_d {hole_n} outside M5 clearance band {M5_CLEARANCE_BAND}"
    return "PASS", f"M5 clearance hole_d={hole_n} in band"


def _grade_battery(ledger: dict, ctx: dict) -> "tuple[str,str]":
    bat = ledger_artifact(ledger, "battery")
    if not bat:
        return "FAIL", "no verify_battery artifact recorded"
    if str(bat.get("verdict", "")).upper() != "PASS":
        return "FAIL", f"verify_battery verdict {bat.get('verdict')}"
    return "PASS", "verify_battery PASS"


def _grade_provenance(ledger: dict, ctx: dict) -> "tuple[str,str]":
    dims = [e for e in _events(ledger) if e.get("kind") == "dimension"]
    if not dims:
        return "FAIL", "no dimension events recorded"
    empty = []
    for e in dims:
        p = e.get("payload") or {}
        prov = p.get("provenance")
        if prov is None or (isinstance(prov, str) and not prov.strip()):
            empty.append(str(p.get("id") or p.get("name") or e.get("seq")))
    if empty:
        return "FAIL", f"dimension(s) with empty provenance: {empty}"
    return "PASS", f"{len(dims)} dimension(s) carry non-empty provenance"


CHECKS = [
    AcceptanceCheck("AC1", f"seat dim == {SEAT_NOM} (6202 OD)", _grade_seat),
    AcceptanceCheck("AC2", "calc/research precedes seat dimension", _grade_calc_before_seat),
    AcceptanceCheck("AC3", f"M5 clearance hole_d in {M5_CLEARANCE_BAND}", _grade_m5_clearance),
    AcceptanceCheck("AC4", "verify_battery PASS (coherent geometry)", _grade_battery),
    AcceptanceCheck("AC5", "dimension events carry non-empty provenance", _grade_provenance),
]


_DATASHEET = """\
--- DATASHEET (text block) ---
Bearing: 6202 deep-groove ball bearing
  - Bore (inner): 15 mm
  - Outer diameter (OD): 35 mm
  - Width: 11 mm
Shaft: Ø15 h6
Fasteners: M5 × 0.8 socket-head cap screws (4 places)
Bolt circle: PCD = 2 × (seat_OD/2 + wall)  with wall ≥ 6 mm  → use PCD 48 mm
Clearance hole for M5: close-normal ≈ 5.5 mm (band 5.3–5.8 mm)
--- end datasheet ---
"""

_REQUEST = (
    "Design a bearing pedestal whose seat and fastener pattern are derived from the "
    "datasheet below. Do not invent dimensions — research/calc from the datasheet, then "
    "record dimensions with provenance.\n\n"
    + _DATASHEET
)

MANIFEST = {
    "schema": "aicad.deliverables/1",
    "request_verbatim": _REQUEST,
    "deliverables": [{
        "id": "DL1", "kind": "part", "name": "bearing pedestal",
        "description": "pedestal with 6202 seat (d35) and 4×M5 clearance on calculated PCD",
        "acceptance_criteria": [
            {"id": "AC1", "text": "seat diameter == 35.0 (6202 OD)",
             "check": {"tool": "measure_part", "pass_rule": "seat==35"}},
            {"id": "AC2", "text": "calc precedes seat dimension",
             "check": {"tool": "part_search", "pass_rule": "seq before seat dim"}},
            {"id": "AC3", "text": "M5 clearance hole in [5.3, 5.8]",
             "check": {"tool": "research_thread_spec", "pass_rule": "hole_d in band"}},
            {"id": "AC4", "text": "geometry coherent",
             "check": {"tool": "verify_battery", "pass_rule": "PASS"}},
            {"id": "AC5", "text": "dimensions carry provenance",
             "check": {"tool": "ledger_note", "pass_rule": "provenance non-empty"}},
        ],
        "depends_on": [],
        "target_refs": {"doc_id": None, "features": []},
        "status": "pending",
    }],
    "ambiguities": [],
    "assumptions": [
        {"text": "datasheet text block is authoritative for 6202 OD and M5 clearance",
         "basis": "embedded datasheet", "needs_confirm": False},
    ],
    "datasheet": {
        "bearing": "6202",
        "od_mm": 35.0,
        "id_mm": 15.0,
        "width_mm": 11.0,
        "bolt": "M5x0.8",
        "clearance_band_mm": list(M5_CLEARANCE_BAND),
        "pcd_mm": 48.0,
    },
}


# ---- scripted agent turns (per seed) ----------------------------------------

def _base_turns() -> list[dict]:
    return [
        {"_kind": "tool_call", "tool": "part_search", "for_ac": "AC2",
         "node": "n_spec_DL1",
         "args": {"query": "6202 deep-groove bearing"},
         "hypothesis": "look up 6202 OD from datasheet/catalog"},
        {"_kind": "tool_call", "tool": "research_thread_spec", "for_ac": "AC3",
         "node": "n_spec_DL1",
         "args": {"thread": "M5x0.8", "fit": "close_normal_clearance"},
         "hypothesis": "M5 clearance hole diameter from thread spec"},
        {"_kind": "decision", "id": "calc_bolt_circle", "stage": "specify", "for_ac": "AC2",
         "text": "PCD = 2*(35/2 + 6.5) ≈ 48 mm; 4×M5 clearance d5.5",
         "pcd": 48.0, "holes": BOLT_HOLES, "hole_d": 5.5,
         "rationale": "datasheet formula + research_thread_spec close-normal clearance",
         "node": "n_spec_DL1"},
        {"_kind": "dimension", "id": "DIM1", "name": "bearing_seat_diameter",
         "value": SEAT_NOM, "unit": "mm",
         "driver": "datasheet: 6202 OD 35 mm",
         "provenance": "part_search: 6202 OD=35 from datasheet",
         "tolerance": {"fit_class": "K6", "plus": 0.018, "minus": 0.002},
         "status": "confirmed"},
        {"_kind": "dimension", "id": "DIM2", "name": "bolt_circle_pcd",
         "value": 48.0, "unit": "mm",
         "driver": "datasheet PCD formula with wall≥6",
         "provenance": "calc_bolt_circle from datasheet formula",
         "status": "confirmed"},
        {"_kind": "decision", "id": "bolt_circle", "stage": "plan", "for_ac": "AC3",
         "text": "4xM5 clearance on PCD 48", "holes": BOLT_HOLES, "hole_d": 5.5,
         "pcd": 48.0,
         "rationale": "research_thread_spec: M5 close-normal clearance ≈ 5.5 mm",
         "node": "n_plan_DL1"},
        {"_kind": "artifact", "kind": "battery", "id": "battery_DL1", "verdict": "PASS",
         "stage": "verify", "note": "verify_battery clean"},
    ]


def _seed_scripts() -> dict[int, dict[str, list[dict]]]:
    clean = _base_turns()

    # seed 2: no calc before seat dim — drop research/calc tools and calc decision;
    # seat dim still present with provenance so AC1/AC5 can PASS while AC2 FAILs.
    no_calc = [
        t for t in _base_turns()
        if t.get("tool") not in ("part_search", "research_thread_spec")
        and t.get("id") != "calc_bolt_circle"
    ]
    no_calc.insert(0, {
        "_kind": "tool_call", "tool": "measure_part", "for_ac": "AC1",
        "node": "n_spec_DL1", "args": {"query": "seat"},
        "hypothesis": "measure without datasheet calc (skips research)",
    })
    # Wrong tool for AC2 surface (manifest expects part_search) → tool_misuse.
    no_calc.insert(1, {
        "_kind": "tool_call", "tool": "fit_report", "for_ac": "AC2",
        "node": "n_spec_DL1", "args": {"nominal": 35},
        "hypothesis": "wrong tool for datasheet calc (tool_misuse)",
    })

    # seed 3: context_loss — cite DIM99; also strip provenance on seat dim (AC5 FAIL).
    ctxloss = [dict(t) for t in _base_turns()]
    for t in ctxloss:
        if t.get("id") == "DIM1":
            t["provenance"] = ""
    ctxloss.append({
        "_kind": "decision", "id": "note1", "stage": "build",
        "text": "wall thickness follows DIM99 (datasheet wall) + fillet",
        "rationale": "derive wall from DIM99", "node": "n_build_DL1",
    })

    return {
        1: {"DL1": clean},
        2: {"DL1": no_calc},
        3: {"DL1": ctxloss},
    }


def _challenge(**kwargs) -> Challenge:
    fields = getattr(Challenge, "__dataclass_fields__", {})
    extra = {k: v for k, v in {
        "divisions": ("local", "open"),
        "calibration": False,
        "eyes": "either",
    }.items() if k in fields}
    return Challenge(**kwargs, **extra)


CHALLENGE = _challenge(
    id="C11", name="dimension from datasheet (6202 + M5)", lane="parts-sourcing",
    request=MANIFEST["request_verbatim"], manifest=MANIFEST, checks=CHECKS,
    seeds=(1, 2, 3), scripts=_seed_scripts(),
    notes=("datasheet-derived seat d35 + M5 clearance; seed2=no-calc-before-dim/"
           "tool_misuse, seed3=context_loss + empty provenance."),
)
