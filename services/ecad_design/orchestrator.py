"""The autonomous ECAD design orchestrator -- natural language -> VERIFIED schematic.

Re-homed from ``engine_oracle/ecad/orchestrator.py`` to ``services/ecad_design/``:
this is THIN LLM ORCHESTRATION (a prompt/validate/retry state machine over a provider), not
engine logic, so it lives services-side. The deterministic machinery it drives — the ECAD
session/ops, the recipe fallback library, netlist + SPICE simulation — is C++-dispatched and
consumed via the core_bridge-backed facades in ``services.mcp.ecad_common``; only
``services.ecad.library.starter`` / ``library.resolve`` remain direct imports (no ecad_library
RPC surface).

A DETERMINISTIC PYTHON STATE MACHINE drives the whole flow. Code decides every transition; the
model only makes scoped decisions (the design intent) inside one bounded stage. This mirrors the
CAD orchestrator philosophy (contracts/design_pipeline.md) and reuses the agent-loop
discipline (services/agent/loop.py): bounded retries, a same-error / fail-streak detector, and rich,
actionable feedback nudges. The point is that a WEAK local model succeeds because code does the
bookkeeping + validation, and a recipe fallback guarantees a result no matter what the model emits.

Stages (code controls transitions; one transparency event per stage carries prompt/input/raw/
parsed/result):

  1. INTAKE/PLAN  -- ask the provider for a structured design intent (parts list of REAL libids,
                    nets by name, values, a sim study + expected result). Parse + VALIDATE strictly
                    (libids resolve in the library; every net references a real component pin). On
                    failure: bounded retry with a corrective nudge; after N failures fall back to
                    the closest recipe so the loop ALWAYS produces something.
  2. BUILD        -- materialize the design via ECAD ops on a real EcadSession: place each component
                    (sch_add_symbol on a grid), then realize connectivity by attaching net LABELS /
                    POWER PORTS at each member pin's world coordinate (robust by-name nets, NOT
                    fragile geometric autorouting). Schema-validated + transactional.
  3. ERC          -- run the electrical rules check; if there are ERRORS, the FIX stage applies a
                    deterministic repair for common cases (missing GND/driver -> add a power port),
                    bounded by the fail-streak detector.
  4. SIMULATE     -- netlist -> SPICE -> simulate the study; compare the key result to the plan's
                    `expect` within tolerance. Records pass/fail; never loops forever.
  5. REPORT       -- return a DesignRun with the stage events, applied ops, ERC, sim, and a
                    stop_reason mirroring services/agent/loop.py (confirmed | stabilized | failed | fallback
                    | max_attempts).

Compaction by construction: each model call gets a FRESH bounded context (request + a catalog
subset + recipe few-shots + a compact status summary), never a growing history -- sized for a
4-8k-context local model.

Honest division of labor (see the report): the MODEL decides the topology/parts/values; CODE
guarantees validity (strict parse + schema-validated ops + recipe fallback), realizes connectivity,
runs ERC + simulation, and compares against the expectation. A model that returns garbage still
yields a verified schematic via the recipe path.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

# netlist/schema/session/ops/recipes and the SPICE stack are C++-dispatched
# (core/ecad/*, core/sim/spice.cpp) — consumed here via the core_bridge-backed facades in
# services.mcp.ecad_common and the ecad_recipe/ecad_closest_recipe/sim_* RPCs.
# services.ecad.library.starter + library.resolve have NO ecad_library RPC surface — those
# lazy imports STAY (see _catalog_lines / validate_design / _remap_pins_by_name).
from services.mcp.ecad_common import (
    OpError,
    _netlist_to_circuit,
    _rpc,
    _sim_run,
    ecad_session,
    netlist_mod,
)
from services.mcp.ecad_common import schema as ecad_schema

EmitFn = Callable[[dict], None]

# --------------------------------------------------------------------------- tunables (mirror loop.py)

# Bounded retries for the PLAN stage before we fall back to a recipe (loop.py _NO_CODE_LIMIT spirit).
_PLAN_ATTEMPTS = 3
# A FIX-stage repair is bounded the same way the agent loop bounds its fail streak.
_FIX_ATTEMPTS = 3
# Context budget for a small-context (4-8k) local model -- every model call stays under this many
# *characters* of prompt (a crude but safe proxy: ~4 chars/token => ~well under 4k tokens).
_CONTEXT_CHAR_CAP = 12_000

# Deterministic grid layout pitch (mm) for placed symbols. Coarse + on the 2.54 grid.
_GRID_PITCH_X = 25.4
_GRID_PITCH_Y = 25.4
_GRID_COLS = 4

_JSON_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)

# Power-rail-like net names get a POWER PORT (a driver) in addition to labels, so ERC sees a source
# and the netlist names the net after the rail. Everything else is a plain global label.
_POWER_RAIL_RE = re.compile(r"^(GND|VCC|VDD|VEE|VSS|\+?\d+V\d*|\+?\d+V|-?\d+V\d*|\+\d+V\d+)$",
                            re.IGNORECASE)


# --------------------------------------------------------------------------- transparency events


@dataclass
class StageEvent:
    """One transparency record per stage (prompt/input/raw/parsed/result), a la events.md."""
    stage: str
    ok: bool
    detail: str = ""
    prompt: Optional[str] = None        # system+user prompt sent to the model (PLAN/FIX)
    raw: Optional[str] = None           # the model's raw reply
    parsed: Optional[dict] = None       # the parsed/validated structure
    result: Optional[dict] = None       # stage outcome summary
    attempt: int = 0


@dataclass
class DesignRun:
    """The full result of a run_design() call."""
    ok: bool
    request: str
    doc_id: Optional[str]
    stop_reason: str                    # confirmed | stabilized | failed | fallback | max_attempts
    recipe_used: Optional[str] = None   # set when the recipe fallback (or a direct recipe) was used
    revision: int = 0
    stages: list = field(default_factory=list)     # list[StageEvent as dict]
    ops_applied: int = 0
    erc: dict = field(default_factory=dict)        # {"errors": int, "warnings": int, "rules": {...}}
    sim: dict = field(default_factory=dict)        # {"analysis", "converged", "expect", "got", "pass"}
    design: Optional[dict] = None       # the materialized design intent (parts/nets/study)
    notes: str = ""


# --------------------------------------------------------------------------- prompts (role-style)


_PLAN_SYSTEM = """\
You are the ECAD INTAKE planner. Turn the user's request into a STRUCTURED design intent for a
small analog schematic. Reply with EXACTLY ONE ```json block and nothing else.

The json shape (every field required):
{
  "components": [ {"ref": "V1", "libid": "Simulation_SPICE:V_source", "value": "5"}, ... ],
  "nets": { "NETNAME": [ {"ref": "V1", "pin": "+"}, {"ref": "R1", "pin": "1"} ], ... },
  "study": { "analysis": "op", "params": {}, "expect": {"probe": "V(VMID)", "value": 2.5, "tol": 0.05} }
}

Rules you MUST follow:
- Use ONLY libids from the CATALOG below (copy them exactly). Give every component a unique ref.
- Reference pins by the NUMBER shown for that libid in the catalog (a resistor has "1"/"2"; the
  voltage source has "+"/"-").
- Every net lists the pins that connect to it BY NAME; use "GND" for ground and a rail name like
  "VCC" / "+5V" for supplies. Every component pin should appear in exactly one net.
- The study.analysis is one of: "op" (DC point), "ac" (frequency), "tran" (transient). Put the
  single most telling result in study.expect (probe is "V(NET)", value is the number you expect,
  tol is a fraction like 0.05).
- Keep it minimal and CORRECT -- match the worked EXAMPLES in spirit.\
"""

_FIX_SYSTEM = """\
You are the ECAD fixer. The schematic below has ELECTRICAL RULE errors. Reply with EXACTLY ONE
```json block: a list of corrective NETS to add or extend, shaped like
{"add_nets": {"GND": [{"ref":"U1","pin":"4"}], ...}}.
Only name pins that exist. Prefer connecting an undriven power pin to its rail (GND / VCC).\
"""


# --------------------------------------------------------------------------- helpers


def _emit(emit: EmitFn | None, ev: StageEvent) -> None:
    if emit is not None:
        try:
            emit(asdict(ev))
        except Exception:  # noqa: BLE001 - a flaky sink must never break the run
            pass


def _cap(s: str | None, n: int) -> str | None:
    if not s:
        return s
    return s if len(s) <= n else s[:n] + "\n...[truncated]"



def _closest_recipe(request: str) -> dict:
    """Return the native deterministic recipe, with a local last-resort safety net."""
    try:
        design = _rpc("ecad_closest_recipe", {"request": request})
        required = ("name", "components", "nets", "study")
        if not isinstance(design, dict) or not all(key in design for key in required):
            raise TypeError("ecad_closest_recipe returned an invalid design")
        return design
    except Exception:  # noqa: BLE001 - an unavailable core must not break fallback planning
        from services.ecad_design import recipes as recipes_mod
        return recipes_mod.closest_recipe(request)

def _catalog_lines() -> list[str]:
    """A COMPACT catalog of the simulatable starter parts (libid + pin numbers + a sim hint).

    Bounded by construction: only the handful of parts a small analog design needs, one line each,
    so the planner context stays tiny. Pin NUMBERS are shown because nets reference pins by number.
    """
    from services.ecad.library.starter import SYMBOLS  # STAYS: no ecad_library RPC

    wanted = [
        "Simulation_SPICE:V_source", "Simulation_SPICE:I_source",
        "Device:R", "Device:C", "Device:L", "Device:D", "Device:LED",
        "Amplifier_Operational:OPAMP_dual", "Timer:NE555",
        "power:GND", "power:VCC", "power:+5V",
    ]
    lines: list[str] = []
    for libid in wanted:
        sdef = SYMBOLS.get(libid)
        if not sdef:
            continue
        pins = ", ".join(f"{p['number']}={p['name']}" if p['name'] not in ("~", p['number'])
                         else f"{p['number']}"
                         for p in sdef.get("pins", []) if p.get("unit", 1) == 1)
        desc = sdef.get("description", "")
        lines.append(f"  {libid}  pins[{pins}]  - {desc}")
    return lines


def _few_shot_text() -> str:
    """Two recipe designs rendered as compact few-shot examples for the planner."""
    examples = [_rpc("ecad_recipe", {"name": "voltage_divider",
                                     "params": {"vin": 5.0, "vout": 2.5}}),
                _rpc("ecad_recipe", {"name": "inverting_amp", "params": {"gain": 10.0}})]
    blocks = []
    for ex in examples:
        compact = {
            "components": [{"ref": c["ref"], "libid": c["libid"], "value": c["value"]}
                          for c in ex["components"]],
            "nets": ex["nets"],
            "study": ex["study"],
        }
        blocks.append(f"EXAMPLE - {ex['request']}:\n```json\n"
                      + json.dumps(compact, separators=(",", ":")) + "\n```")
    return "\n\n".join(blocks)


def _plan_user(request: str) -> str:
    """Bounded planner context: request + catalog subset + few-shots. No growing history."""
    parts = [
        f"USER REQUEST: {request}",
        "",
        "CATALOG (use these libids exactly; pins are NUMBER=name):",
        *_catalog_lines(),
        "",
        _few_shot_text(),
        "",
        "Now produce the design-intent json for the USER REQUEST above.",
    ]
    return "\n".join(parts)


def _extract_json(reply: str) -> Any | None:
    """Parse a ```json fenced block, tolerating a bare {...}/[...] (loop.py _extract_plan spirit)."""
    if not reply:
        return None
    m = _JSON_RE.search(reply)
    raw = m.group(1) if m else None
    if raw is None:
        a = min((reply.find(ch) for ch in "{[" if ch in reply), default=-1)
        b = max(reply.rfind("}"), reply.rfind("]"))
        if 0 <= a <= b:
            raw = reply[a:b + 1]
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- validation


def validate_design(design: Any) -> tuple[bool, list[str]]:
    """Strictly validate a design intent (recipe OR model plan) -- the contract both must satisfy.

    Checks: shape; libids RESOLVE in the library; every net pin references a real component AND a
    real pin number on that component's resolved SymbolDef; study has a usable analysis + expect.
    Returns (ok, errors). Pure; never raises.
    """
    from services.ecad.library import resolve as lib_resolve  # STAYS: no ecad_library RPC

    errs: list[str] = []
    if not isinstance(design, dict):
        return False, ["design is not an object"]

    comps = design.get("components")
    nets = design.get("nets")
    study = design.get("study")
    if not isinstance(comps, list) or not comps:
        errs.append("components must be a non-empty list")
    if not isinstance(nets, dict) or not nets:
        errs.append("nets must be a non-empty object")
    if not isinstance(study, dict):
        errs.append("study must be an object")
    if errs:
        return False, errs

    # components: unique refs, resolvable libids, valid pin set per resolved def.
    refs: dict[str, set[str]] = {}     # ref -> set of valid pin numbers
    seen_ref: set[str] = set()
    for i, c in enumerate(comps):
        if not isinstance(c, dict):
            errs.append(f"components[{i}] is not an object")
            continue
        ref = c.get("ref")
        libid = c.get("libid")
        if not isinstance(ref, str) or not ref:
            errs.append(f"components[{i}].ref missing")
            continue
        if ref in seen_ref:
            errs.append(f"duplicate ref {ref!r}")
        seen_ref.add(ref)
        if not isinstance(libid, str) or not libid:
            errs.append(f"{ref}: libid missing")
            continue
        sdef = lib_resolve(libid)
        if not isinstance(sdef, dict):
            errs.append(f"{ref}: libid {libid!r} does not resolve in the library")
            continue
        pin_numbers = {str(p.get("number")) for p in sdef.get("pins", [])}
        pin_names = {str(p.get("name")) for p in sdef.get("pins", [])}
        refs[ref] = pin_numbers | pin_names

    # nets: every member references a known ref + a real pin on it.
    for net_name, members in nets.items():
        if not isinstance(members, list) or not members:
            errs.append(f"net {net_name!r} must be a non-empty list")
            continue
        for m in members:
            if not isinstance(m, dict) or "ref" not in m or "pin" not in m:
                errs.append(f"net {net_name!r}: a member is not {{ref,pin}}")
                continue
            ref = m["ref"]
            pin = str(m["pin"])
            if ref not in refs:
                errs.append(f"net {net_name!r}: ref {ref!r} is not a placed component")
            elif pin not in refs[ref]:
                errs.append(f"net {net_name!r}: {ref} has no pin {pin!r}")

    # study sanity.
    analysis = study.get("analysis")
    if analysis not in ("op", "dc", "ac", "tran"):
        errs.append(f"study.analysis {analysis!r} must be op|dc|ac|tran")
    expect = study.get("expect")
    if not isinstance(expect, dict) or "probe" not in expect or "value" not in expect:
        errs.append("study.expect must have a 'probe' and a 'value'")

    return (not errs), errs


# --------------------------------------------------------------------------- BUILD


def _layout_pos(index: int) -> list[float]:
    """Deterministic grid position for the *index*-th placed component (on the 2.54 grid)."""
    col = index % _GRID_COLS
    row = index // _GRID_COLS
    return [col * _GRID_PITCH_X, -row * _GRID_PITCH_Y]


def _pin_world_map(doc: dict) -> dict[tuple[str, str], list[float]]:
    """Map (ref, pin-number) AND (ref, pin-name) -> world [x,y] for every placed pin."""
    out: dict[tuple[str, str], list[float]] = {}
    for sym, pin, world in ecad_schema.iter_placed_pins(doc):
        ref = sym.get("ref", "")
        wx = [round(world[0], 6), round(world[1], 6)]
        out[(ref, str(pin.get("number")))] = wx
        if pin.get("name"):
            out.setdefault((ref, str(pin.get("name"))), wx)
    return out


def _is_power_rail(net_name: str) -> bool:
    return bool(_POWER_RAIL_RE.match(net_name.strip()))


# Default footprints by libid so AI-designed schematics are board-ready (the update-from-sch ->
# autoroute -> 3D flow needs footprints). All fpids resolve in the clean-room starter library.
# Sources / power symbols are intentionally absent (not placed on a board).
_DEFAULT_FOOTPRINTS = {
    "Device:R": "Resistor_SMD:R_0603",
    "Device:C": "Capacitor_SMD:C_0603",
    "Device:C_pol": "Capacitor_THT:CP_Radial_D5.0mm_P2.00mm",
    "Device:L": "Inductor_SMD:L_0603",
    "Device:Ferrite_Bead": "Inductor_SMD:FB_0603",
    "Device:D": "Diode_SMD:D_SMA",
    "Device:LED": "LED_SMD:LED_0805",
    "Device:Q_NPN": "Package_TO_SOT_SMD:SOT-23",
    "Device:Q_PNP": "Package_TO_SOT_SMD:SOT-23",
    "Device:NMOS": "Package_TO_SOT_SMD:SOT-23",
    "Device:PMOS": "Package_TO_SOT_SMD:SOT-23",
    "Amplifier_Operational:OPAMP_dual": "Package_SO:SOIC-8",
    "Amplifier_Operational:LM358": "Package_SO:SOIC-8",
    "Timer:NE555": "Package_DIP:DIP-8",
    "Regulator_Linear:AMS1117-3.3": "Package_TO_SOT_SMD:SOT-223",
    "Regulator_Linear:7805": "Package_TO_SOT_THT:TO-220-3",
    "MCU:Generic_QFP48": "Package_QFP:LQFP-48_7x7mm_P0.5mm",
    "Connector:Header_2x03": "Connector_PinHeader_2.54mm:Header_2x03",
    "Connector:Header_2x05": "Connector_PinHeader_2.54mm:Header_2x05",
    "Connector:USB_C": "Connector_USB:USB_C",
    "Connector:USB_B": "Connector_USB:USB_B",
    # flight-computer + battery-charger parts pack (mission master, 2026-07)
    "Sensor_Motion:MPU6050": "Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm",
    "Sensor_Pressure:BMP280": "Package_LGA:LGA-8_2x2.5mm",
    "Sensor_Magnetic:LIS3MDL": "Package_LGA:LGA-12_2x2mm",
    "RF_GPS:NEO6M": "RF_Module:GPS_Castellated_1x6",
    "Regulator_Switching:Buck_3V3": "Package_TO_SOT_SMD:SOT-23-6",
    "Device:Crystal_32k768": "Crystal:Crystal_SMD_3215-2Pin",
    "Device:Crystal_HSE": "Crystal:Crystal_HC49-4H_Vertical",
    "Connector:SWD_2x5": "Connector_PinHeader_2.54mm:Header_2x05",
    "Battery_Management:MCP73831": "Package_TO_SOT_SMD:SOT-23-5",
    "Battery_Management:SwitchingCharger_SOP8": "Package_SO:SOIC-8",
    "Device:NMOS_Power": "Package_SO:SOIC-8",
    "Device:PMOS_Power": "Package_SO:SOIC-8",
    "Device:PMOS_Reverse": "Package_TO_SOT_SMD:SOT-23",
    "Device:R_Sense": "Resistor_SMD:R_2512",
    "Power_Management:Load_Switch": "Package_TO_SOT_SMD:SOT-23-5",
    "Power_Management:LVD_Comparator": "Package_TO_SOT_SMD:SOT-23-5",
    "Device:NTC": "Resistor_SMD:R_0603",
    "Device:L_Power": "Inductor_SMD:L_Power_7x7",
}


def build_design(session: ecad_session.EcadSession, design: dict) -> dict:
    """Materialize *design* onto *session* via ECAD ops. Returns a build summary.

    Two transactional op batches:
      1. place every component with sch_add_symbol on a deterministic grid;
      2. realize connectivity: for each net, drop a global LABEL at every member pin's world
         coordinate (a robust by-name net), plus a POWER PORT for power-rail nets so ERC sees a
         driver and the netlist names the net after the rail.
    Schema-validated + transactional via session.apply_ops (a bad batch leaves the session intact).
    """
    # -- batch 1: place components --
    place_ops: list[dict] = []
    for i, c in enumerate(design["components"]):
        op = {"op": "sch_add_symbol", "libid": c["libid"], "ref": c["ref"],
              "pos": _layout_pos(i)}
        if c.get("value") not in (None, ""):
            op["value"] = str(c["value"])
        fp = c.get("footprint") or _DEFAULT_FOOTPRINTS.get(c["libid"])
        if fp:
            op["footprint"] = fp  # board-ready: update-from-sch can place a real footprint
        # Persist a source's AC magnitude ONTO the schematic symbol so the netlist
        # (and therefore any later ecad_simulate('ac'), not just this design's own
        # self-check) has a real small-signal stimulus — otherwise an AC sweep of a
        # filter built by ecad_design reads a flat zero (Bug E-1).
        if c.get("ac_mag") is not None:
            sp = {"ac_mag": c["ac_mag"]}
            if c.get("ac_phase") is not None:
                sp["ac_phase"] = c["ac_phase"]
            op["sim_params"] = sp
        place_ops.append(op)
    resp = ecad_session.apply_ops(session, session.revision, place_ops)
    ops_applied = len(place_ops)

    # -- batch 2: connectivity by labels + power ports --
    pin_world = _pin_world_map(session.doc)
    conn_ops: list[dict] = []
    missing: list[str] = []
    for net_name, members in design["nets"].items():
        power_rail = _is_power_rail(net_name)
        for m in members:
            key = (m["ref"], str(m["pin"]))
            pos = pin_world.get(key)
            if pos is None:
                missing.append(f"{m['ref']}.{m['pin']}")
                continue
            conn_ops.append({"op": "sch_add_label", "text": net_name,
                             "pos": pos, "scope": "global"})
        # one power port per rail net, offset so it does not collide with a pin label
        if power_rail and members:
            anchor = pin_world.get((members[0]["ref"], str(members[0]["pin"])))
            if anchor is not None:
                conn_ops.append({"op": "sch_add_power", "net": net_name,
                                 "pos": [anchor[0], anchor[1]], "symbol": net_name})
    if conn_ops:
        ecad_session.apply_ops(session, session.revision, conn_ops)
        ops_applied += len(conn_ops)

    return {"ops_applied": ops_applied, "missing_pins": missing,
            "revision": session.revision}


# --------------------------------------------------------------------------- ERC + FIX


def _erc_summary(session: ecad_session.EcadSession) -> dict:
    """Compact ERC summary from the session's last compile (errors/warnings + per-rule counts)."""
    comp = session.last_compile or ecad_session.tree(session)["compile"]
    erc = comp.get("erc", []) or []
    rules: dict[str, int] = {}
    errors = 0
    warnings = 0
    for v in erc:
        rules[v["rule"]] = rules.get(v["rule"], 0) + 1
        if v.get("severity") == "error":
            errors += 1
        else:
            warnings += 1
    return {"errors": errors, "warnings": warnings, "rules": rules,
            "violations": erc}


def _deterministic_fix(session: ecad_session.EcadSession, erc: dict) -> list[dict]:
    """Code-side repair for common ERC errors. Returns the connectivity ops that were applied.

    Handles the load-bearing cases without the model:
      * power_not_driven / no_driver -- add a POWER PORT (a driver) on the offending net.
      * pin_not_connected on a power_in pin -- tie it to GND via a label + power port.
    Deterministic + bounded; anything it cannot repair is left for the report.
    """
    fixes: list[dict] = []
    pin_world = _pin_world_map(session.doc)
    by_pin = (session.last_compile or {}).get("netlist", {})
    by_pin = by_pin.get("by_pin", {}) if isinstance(by_pin, dict) else {}
    ref_of: dict[str, str] = {}
    for sym in session.doc.get("symbols", []):
        ref_of[sym.get("id", "")] = sym.get("ref", "")

    for v in erc.get("violations", []):
        if v.get("severity") != "error":
            continue
        rule = v.get("rule")
        net = v.get("net")
        if rule in ("power_not_driven", "no_driver") and net:
            # add a power port on this net (a driver). Anchor it at the first member pin.
            anchor = None
            for pinref in v.get("pins", []):
                sid, _dot, pno = pinref.partition(".")
                ref = ref_of.get(sid, sid)
                anchor = pin_world.get((ref, pno))
                if anchor is not None:
                    break
            if anchor is not None:
                fixes.append({"op": "sch_add_power", "net": net,
                              "pos": [anchor[0], anchor[1]], "symbol": net})
    if fixes:
        ecad_session.apply_ops(session, session.revision, fixes)
    return fixes


# --------------------------------------------------------------------------- SIMULATE


def _remap_pins_by_name(netlist: dict) -> None:
    """Augment every component's ``pins`` dict with NAME keys (in place).

    The netlist keys pins by NUMBER, but ``netlist_to_spice_circuit`` looks them up by the
    SymbolDef's ``sim.pin_order`` -- which for multi-pin parts (the op-amp, the 555) uses pin NAMES.
    Adding name-keyed aliases lets those subckt-backed parts resolve their nodes correctly. A no-op
    for parts whose pin_order already uses numbers (R/C/L/V). Read-only use of the library.
    """
    from services.ecad.library.starter import SYMBOLS  # STAYS: no ecad_library RPC

    for comp in netlist.get("components", []):
        sdef = SYMBOLS.get(comp.get("libid"))
        if not sdef:
            continue
        num2net = comp.get("pins", {})
        for pin in sdef.get("pins", []):
            num = str(pin.get("number"))
            name = str(pin.get("name"))
            if num in num2net and name and name not in num2net:
                num2net[name] = num2net[num]


def _ac_mag_for(design: dict) -> tuple[str | None, float]:
    """The (source ref, AC magnitude) for an AC study, if the design declares one."""
    study = design.get("study", {})
    params = study.get("params", {})
    src = params.get("ac_source")
    mag = float(params.get("ac_mag", 1.0))
    if src is None:
        for c in design.get("components", []):
            if c.get("ac_mag") is not None or c.get("libid", "").endswith("V_source"):
                src = c.get("ref")
                mag = float(c.get("ac_mag", mag))
                break
    return src, mag


def _build_study(design: dict) -> dict:
    """Translate the design's study spec into a circuit-simulator study dict."""
    spec = design.get("study", {})
    analysis = spec.get("analysis", "op")
    params = dict(spec.get("params", {}))
    expect = spec.get("expect", {})
    probe = expect.get("probe", "")
    if analysis == "op":
        an: dict = {"type": "op"}
    elif analysis == "ac":
        an = {"type": "ac", "sweep": params.get("sweep", "dec"),
              "points": int(params.get("points", 20)),
              "fstart": float(params.get("fstart", 1.0)),
              "fstop": float(params.get("fstop", 1e6))}
    elif analysis == "tran":
        an = {"type": "tran", "tstep": float(params.get("tstep", 1e-5)),
              "tstop": float(params.get("tstop", 1e-2)),
              "uic": bool(params.get("uic", True))}
    elif analysis == "dc":
        an = {"type": "dc", "source": params.get("source", "V1"),
              "start": float(params.get("start", 0.0)),
              "stop": float(params.get("stop", 5.0)),
              "step": float(params.get("step", 0.1))}
    else:
        an = {"type": "op"}
    probes = [probe] if probe else []
    return {"analyses": [an], "probes": probes}


def simulate_design(session: ecad_session.EcadSession, design: dict) -> dict:
    """Build netlist -> SPICE circuit -> simulate the study; compare to the plan's expect.

    Returns {analysis, converged, probe, expect, got, pass, tol, warnings, log}. Never raises.
    """
    nl = netlist_mod.build_netlist(session.doc)
    _remap_pins_by_name(nl)
    circuit, warns = _netlist_to_circuit(nl)

    study = _build_study(design)
    expect = design.get("study", {}).get("expect", {})
    analysis = study["analyses"][0]["type"]

    out: dict = {"analysis": analysis, "converged": False,
                 "probe": expect.get("probe", ""), "expect": expect.get("value"),
                 "got": None, "pass": False, "tol": expect.get("tol", 0.05),
                 "warnings": warns, "device_count": len(circuit.get("devices", []))}
    if not circuit.get("devices"):
        out["log"] = ["no simulatable devices in the netlist"]
        return out

    # set the AC drive on the source (the netlist path only carries the DC value).
    if analysis == "ac":
        src, mag = _ac_mag_for(design)
        for d in circuit.get("devices", []):
            if d.get("kind") == "V" and (src is None or d.get("name") == src):
                d.setdefault("params", {})["ac"] = {"mag": mag, "phase": 0.0}

    result = _sim_run(circuit, study)  # C++ sim_spice RPC
    out["converged"] = bool(result.get("convergence", {}).get(analysis, False))
    out["log"] = result.get("log", [])[-6:]

    got = _read_probe(result, analysis, expect)
    out["got"] = got
    exp_val = expect.get("value")
    tol = float(expect.get("tol", 0.05))
    if got is not None and exp_val is not None and math.isfinite(got):
        denom = abs(exp_val) if exp_val else 1.0
        out["pass"] = abs(got - exp_val) / denom <= tol
    return out


def _read_probe(result: dict, analysis: str, expect: dict) -> float | None:
    """Extract the single comparison value for the study from a sim result."""
    probe = expect.get("probe", "")
    try:
        if analysis == "op":
            return float(result.get("op", {}).get(probe))
        if analysis == "ac":
            ac = result.get("ac", {})
            f = ac.get("x_f", [])
            mag = ac.get("mag", {}).get(probe, [])
            if not f or not mag:
                return None
            at = expect.get("at_freq")
            if at is not None:
                # linear interpolation of |H| at the target frequency
                return float(_interp(at, f, mag))
            return float(mag[len(mag) // 2])
        if analysis == "tran":
            tr = result.get("tran", {})
            t = tr.get("x_t", [])
            v = tr.get("traces", {}).get(probe, [])
            if not t or not v:
                return None
            if expect.get("kind") == "period":
                return _estimate_period(t, v)
            return float(v[-1])
        if analysis == "dc":
            dc = result.get("dc", {})
            v = dc.get("traces", {}).get(probe, [])
            return float(v[-1]) if v else None
    except (TypeError, ValueError):
        return None
    return None


def _interp(x: float, xs: list, ys: list) -> float:
    """Tiny linear interpolation (avoids a numpy dependency in this module)."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if xs[i] >= x:
            x0, x1 = xs[i - 1], xs[i]
            y0, y1 = ys[i - 1], ys[i]
            if x1 == x0:
                return y0
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return ys[-1]


def _estimate_period(t: list, v: list) -> float | None:
    """Estimate an oscillation period from rising-edge mid-crossings; None if it never oscillates."""
    vmin, vmax = min(v), max(v)
    if vmax - vmin < 1e-3:
        return None
    mid = 0.5 * (vmin + vmax)
    crossings = [t[i] for i in range(1, len(v))
                 if v[i - 1] < mid <= v[i]]
    if len(crossings) < 2:
        return None
    deltas = [crossings[i] - crossings[i - 1] for i in range(1, len(crossings))]
    return sum(deltas) / len(deltas)


# --------------------------------------------------------------------------- the state machine


def plan_design(request: str, *, provider=None, max_attempts: int = _PLAN_ATTEMPTS,
                emit: EmitFn | None = None) -> tuple[dict | None, str | None, list]:
    """PLAN only — never creates a session or builds a schematic.

    Returns ``(design, recipe_used, events)``. When *provider* is None or planning
    fails/errors, returns the closest built-in recipe so the caller can still BUILD
    a verified schematic. Used by ``ecad_design`` to isolate the (possibly slow)
    model call from session creation so a timed-out provider cannot race a recipe
    fallback build.
    """
    stages: list[StageEvent] = []
    if provider is not None:
        design, recipe_used, plan_events = _plan_stage(request, provider, max_attempts)
        stages.extend(plan_events)
        if design is not None:
            return design, recipe_used, stages
    # No provider, or planning failed -> deterministic recipe (the safety net).
    design = _closest_recipe(request)
    recipe_used = design.get("name")
    ev = StageEvent(stage="plan", ok=True, attempt=0,
                    detail=f"used recipe fallback {recipe_used!r}",
                    parsed=design, result={"recipe_used": recipe_used})
    stages.append(ev)
    _emit(emit, ev)
    return design, recipe_used, stages


def run_design(request: str, *, provider=None, doc_id: str | None = None,
               max_attempts: int = _PLAN_ATTEMPTS, emit: EmitFn | None = None,
               session: ecad_session.EcadSession | None = None,
               design: dict | None = None,
               recipe_used: str | None = None) -> DesignRun:
    """Run the full deterministic design pipeline for *request*; return a :class:`DesignRun`.

    ``provider`` is any services.agent.providers Provider (a MockProvider in tests). When None, the PLAN
    stage is skipped and the closest recipe is used directly -- the run still produces + verifies a
    real schematic. ``session`` lets a caller pass an existing EcadSession (else one is created).

    ``design`` (optional): a pre-validated design intent from :func:`plan_design`. When supplied,
    the PLAN stage is skipped entirely — this is how ``ecad_design`` separates a budgeted provider
    plan from a single non-racy BUILD so a late/timed-out provider can never create a second
    schematic that overwrites the recipe fallback.
    ``recipe_used`` pairs with a pre-planned design to preserve stop_reason/report fidelity.
    """
    stages: list[StageEvent] = []
    if session is None:
        session = ecad_session.new_session("sch", name="orchestrator")
    stop_reason = "confirmed"

    # -- stage 1: INTAKE / PLAN --------------------------------------------
    if design is not None:
        # Pre-planned path: no provider call, no second opinion. Record transparency only.
        if recipe_used:
            stop_reason = "fallback"
        ev = StageEvent(
            stage="plan", ok=True, attempt=0,
            detail=(f"pre-planned design (recipe={recipe_used!r})"
                    if recipe_used else "pre-planned design"),
            parsed=design, result={"recipe_used": recipe_used},
        )
        stages.append(ev)
        _emit(emit, ev)
    else:
        recipe_used = None
        design = None
        if provider is not None:
            design, recipe_used, plan_events = _plan_stage(request, provider, max_attempts)
            stages.extend(plan_events)
        if design is None:
            # no provider, or planning failed -> deterministic recipe fallback (the safety net).
            design = _closest_recipe(request)
            recipe_used = design.get("name")
            ev = StageEvent(stage="plan", ok=True, attempt=0,
                            detail=f"used recipe fallback {recipe_used!r}",
                            parsed=design, result={"recipe_used": recipe_used})
            stages.append(ev)
            _emit(emit, ev)
            stop_reason = "fallback"

    # -- stage 2: BUILD ----------------------------------------------------
    try:
        build_res = build_design(session, design)
    except OpError as e:
        ev = StageEvent(stage="build", ok=False, detail=f"op error: {e.message}",
                        result=e.to_envelope())
        stages.append(ev)
        _emit(emit, ev)
        return _finish(request, session, design, recipe_used, "failed", stages,
                       ops_applied=0)
    ev = StageEvent(stage="build", ok=True,
                    detail=f"placed {len(design['components'])} parts, "
                           f"{build_res['ops_applied']} ops",
                    result=build_res)
    stages.append(ev)
    _emit(emit, ev)

    # -- stage 3: ERC (+ FIX) ----------------------------------------------
    erc = _erc_summary(session)
    ev = StageEvent(stage="erc", ok=(erc["errors"] == 0),
                    detail=f"{erc['errors']} error(s), {erc['warnings']} warning(s)",
                    result={"errors": erc["errors"], "warnings": erc["warnings"],
                            "rules": erc["rules"]})
    stages.append(ev)
    _emit(emit, ev)

    if erc["errors"] > 0:
        # FIX is deterministic (code-side); never re-enter the provider on the build path.
        erc, fix_events = _fix_stage(session, erc, provider=None)
        stages.extend(fix_events)

    # -- stage 4: SIMULATE -------------------------------------------------
    sim = simulate_design(session, design)
    ev = StageEvent(stage="simulate", ok=sim["converged"],
                    detail=(f"{sim['analysis']}: {sim['probe']}={_fmt_num(sim['got'])} "
                            f"(expect {_fmt_num(sim['expect'])}, "
                            f"{'PASS' if sim['pass'] else 'MISS'})"),
                    result=sim)
    stages.append(ev)
    _emit(emit, ev)

    # -- stage 5: REPORT ---------------------------------------------------
    if erc["errors"] > 0:
        stop_reason = "failed" if stop_reason != "fallback" else stop_reason
    ok = (erc["errors"] == 0) and sim["pass"]
    return _finish(request, session, design, recipe_used,
                   stop_reason if not ok or stop_reason == "fallback" else "confirmed",
                   stages, ops_applied=build_res["ops_applied"], erc=erc, sim=sim, ok=ok)


def _plan_stage(request: str, provider, max_attempts: int) -> tuple[dict | None, str | None, list]:
    """The bounded PLAN loop: ask the model, parse, validate; nudge on failure; fall back.

    Mirrors loop.py's bounded-retry + same-error discipline. Returns (design|None, recipe_used|None,
    events). A None design means the caller uses the recipe fallback.
    """
    events: list[StageEvent] = []
    user_text = _plan_user(request)
    last_errs: list[str] = []
    for attempt in range(1, max_attempts + 1):
        prompt = user_text
        if last_errs:
            prompt = (user_text + "\n\nYOUR PREVIOUS REPLY WAS INVALID:\n- "
                      + "\n- ".join(last_errs[:6])
                      + "\nReturn a corrected json block that fixes EXACTLY these problems.")
        try:
            reply = provider.complete(_PLAN_SYSTEM, _wrap_user(prompt))
        except Exception as e:  # noqa: BLE001
            ev = StageEvent(stage="plan", ok=False, attempt=attempt,
                            detail=f"provider error: {type(e).__name__}: {e}",
                            prompt=_cap(prompt, _CONTEXT_CHAR_CAP))
            events.append(ev)
            break
        parsed = _extract_json(reply or "")
        ok, errs = validate_design(parsed) if parsed is not None else (False, ["no json parsed"])
        ev = StageEvent(stage="plan", ok=ok, attempt=attempt,
                        detail=("valid design" if ok else "invalid: " + "; ".join(errs[:4])),
                        prompt=_cap(prompt, _CONTEXT_CHAR_CAP), raw=_cap(reply, 4000),
                        parsed=parsed if ok else None,
                        result={"errors": errs} if not ok else {"components":
                                len(parsed["components"]), "nets": len(parsed["nets"])})
        events.append(ev)
        if ok:
            return parsed, None, events
        last_errs = errs
    return None, None, events


def _fix_stage(session: ecad_session.EcadSession, erc: dict, provider) -> tuple[dict, list]:
    """Bounded FIX loop: deterministic repair first; re-run ERC; stop on clean or same error.

    Mirrors loop.py's _SAME_ERROR_LIMIT: if a repair pass does not change the error signature it is
    pointless to retry, so we stop and keep the design as-is (honest, not infinite).
    """
    events: list[StageEvent] = []
    prev_sig = _erc_sig(erc)
    for attempt in range(1, _FIX_ATTEMPTS + 1):
        fixes = _deterministic_fix(session, erc)
        new_erc = _erc_summary(session)
        sig = _erc_sig(new_erc)
        ev = StageEvent(stage="fix", ok=(new_erc["errors"] == 0), attempt=attempt,
                        detail=(f"applied {len(fixes)} repair op(s); "
                                f"{new_erc['errors']} error(s) remain"),
                        result={"errors": new_erc["errors"], "rules": new_erc["rules"],
                                "fixes": len(fixes)})
        events.append(ev)
        erc = new_erc
        if erc["errors"] == 0:
            break
        if sig == prev_sig and not fixes:
            ev.detail += " (identical failure -- stopping)"
            break
        prev_sig = sig
    return erc, events


def _erc_sig(erc: dict) -> str:
    """A stable signature of the ERC error set (rule + count), for the same-error detector."""
    items = sorted((r, n) for r, n in erc.get("rules", {}).items())
    return ";".join(f"{r}:{n}" for r, n in items)


def _wrap_user(text: str) -> list[dict]:
    """Wrap a user-prompt string in the provider message shape (single bounded turn, no history)."""
    return [{"role": "user", "content": [{"type": "text", "text": text}]}]


def _fmt_num(x: Any) -> str:
    if x is None:
        return "n/a"
    try:
        return f"{float(x):.4g}"
    except (TypeError, ValueError):
        return str(x)


def _finish(request: str, session, design, recipe_used, stop_reason, stages,
            *, ops_applied: int = 0, erc: dict | None = None, sim: dict | None = None,
            ok: bool | None = None) -> DesignRun:
    erc = erc or _erc_summary(session)
    erc_out = {"errors": erc["errors"], "warnings": erc["warnings"], "rules": erc["rules"]}
    sim_out = sim or {}
    if ok is None:
        ok = (erc["errors"] == 0) and bool(sim_out.get("pass"))
    notes = design.get("notes", "") if isinstance(design, dict) else ""
    return DesignRun(
        ok=bool(ok), request=request, doc_id=session.doc_id, stop_reason=stop_reason,
        recipe_used=recipe_used, revision=session.revision,
        stages=[asdict(s) for s in stages], ops_applied=ops_applied,
        erc=erc_out, sim=sim_out, design=design, notes=notes,
    )
