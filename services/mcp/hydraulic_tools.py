"""AI-Hydraulic MCP tools — ISO 1219 hydraulic circuit design + component sizing.

Same design contract as ``piping_tools`` (project owner: "the AI is extremely limited; its tools
must do the bookkeeping and return rich, actionable context"). Each tool returns a JSON string
``{ok, ...result, warnings:[], hint:"...", next_actions:[...]}`` produced by the C++ core
(T-RF19/T-RF20: the tool envelopes are owned by ``core/hydraulic`` behind the libchudware RPCs
``hydraulic_*`` — reached via :mod:`services.mcp.core_bridge`); the MCP layer keeps only input
parsing and its tiny session state so tools can reuse the last active circuit document.

Tools:
  hydraulic_new             create a new aicad.hydraulic/1 circuit document
  hydraulic_add_component   add an ISO 1219 component to the active circuit
  hydraulic_add_hose        add a hose/line net connecting two or more component ports
  hydraulic_size_cylinder   size a hydraulic cylinder (force, flow, cycle time)
  hydraulic_size_pump       size a hydraulic pump (displacement, power, torque)
  hydraulic_bom             extract a sourcing BOM from the active circuit
  hydraulic_piping_handoff  hose/tube runs -> piping 1D solver (real per-line pressure drops)
  hydraulic_verify_piping   cross-check per-hose estimates vs piping solve + pump margin
  hydraulic_fea_handoff     cylinder mount loads at max pressure -> static FEA load cases
  hydraulic_verify_fea      cross-check envelope mount forces vs independent recompute

Registration is deferred behind :func:`register_hydraulic_tools` so ``server.py`` wires us in
with a tiny fenced block — mirrors :func:`register_piping_tools`.

CONFORMANCE: ``hydraulic_piping_handoff`` / ``hydraulic_verify_piping`` and
``hydraulic_fea_handoff`` / ``hydraulic_verify_fea`` conform to ``aicad.simhandoff/1``
(contracts/sim_handoff.md — couplings ``hydraulic_to_piping`` and ``hydraulic_to_fea``).
Successful results carry a validated envelope under the reserved ``simhandoff`` key
(built exclusively through ``services.mcp.simhandoff``); pre-existing fields unchanged.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any, Optional, Union

from . import core_bridge as cb
from services.mcp import simhandoff as sh

# Session bookkeeping: the last active circuit document
_HYD: dict = {"doc": None}


def _dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2)


def _err(reason: str, hint: Optional[str] = None, **extra: Any) -> dict:
    out: dict[str, Any] = {"ok": False, "reason": reason}
    if hint:
        out["hint"] = hint
    out.update(extra)
    return out


# ------------------------------------------------------- component-type dialect (live fix)
# Measured 2026-07-30 driving cad_guide("hydraulic")'s own workflow verbatim: the guide says
# "Add pump, valve, cylinder, reservoir, etc." and the core refuses `cylinder`, `motor`,
# `check_valve`, `relief_valve` and `linear_cylinder` outright ("unknown component type").
# Every mapping below is a SPELLING of one member of the frozen ISO 1219 enum in
# contracts/hydraulic_schema.md — a word-order flip (check_valve -> valve_check) or the
# generic noun whose enum has exactly one non-degenerate member (`motor` -> motor_hydraulic;
# `cylinder` -> cylinder_linear, since a rotary cylinder is always named as such). Nothing
# here spans two enum members, so no reading is lost.
#
# Bare `valve` is DELIBERATELY absent: four valve types exist (directional / check / relief /
# flow_control) and picking one would be inventing the circuit's function. It stays refused,
# and _resolve_component_type says why instead of listing twelve types with no guidance.
_TYPE_ALIASES: dict[str, str] = {
    "cylinder": "cylinder_linear",
    "linearcylinder": "cylinder_linear",
    "actuator": "cylinder_linear",
    "ram": "cylinder_linear",
    "rotarycylinder": "cylinder_rotary",
    "rotaryactuator": "cylinder_rotary",
    "motor": "motor_hydraulic",
    "hydraulicmotor": "motor_hydraulic",
    "checkvalve": "valve_check",
    "nonreturnvalve": "valve_check",
    "reliefvalve": "valve_relief",
    "pressurereliefvalve": "valve_relief",
    "directionalvalve": "valve_directional",
    "directionalcontrolvalve": "valve_directional",
    "spoolvalve": "valve_directional",
    "flowcontrolvalve": "valve_flow_control",
    "throttlevalve": "valve_flow_control",
    "tank": "reservoir",
    "oiltank": "reservoir",
    "heatexchanger": "heat_exchanger",
    "cooler": "heat_exchanger",
}

_AMBIGUOUS_TYPES: dict[str, tuple] = {
    "valve": ("valve_directional", "valve_check", "valve_relief", "valve_flow_control"),
}


def _resolve_component_type(t: str) -> tuple:
    """A supplied component `type` -> (canonical enum member, note or None).

    Raises ValueError for a generic noun that spans several enum members, because guessing
    which valve a circuit needs would change what the circuit DOES.
    """
    raw = str(t or "")
    key = raw.lower().replace("_", "").replace("-", "").replace(" ", "")
    if key in _AMBIGUOUS_TYPES:
        opts = _AMBIGUOUS_TYPES[key]
        raise ValueError(
            f"component type {raw!r} is ambiguous — {len(opts)} ISO 1219 valve types exist "
            f"({', '.join(opts)}) and they do different things, so there is nothing safe to "
            "assume. Name the one the circuit needs.")
    canon = _TYPE_ALIASES.get(key)
    if canon and canon != raw:
        return canon, f"type {raw!r} read as {canon!r} (ISO 1219 enum spelling)"
    return raw, None


# Axis WORDS a model writes instead of a 3-vector, e.g. hydraulic_fea_handoff(axis="z")
# — measured 2026-07-30: `_as_obj("z")` fails JSON parsing and the whole call is refused
# with "could not parse inputs". Same table and Z-up convention as cad_sim_tools._AXIS_DIRS.
_AXIS_WORDS: dict[str, list] = {
    "x": [1.0, 0.0, 0.0], "+x": [1.0, 0.0, 0.0], "-x": [-1.0, 0.0, 0.0],
    "y": [0.0, 1.0, 0.0], "+y": [0.0, 1.0, 0.0], "-y": [0.0, -1.0, 0.0],
    "z": [0.0, 0.0, 1.0], "+z": [0.0, 0.0, 1.0], "-z": [0.0, 0.0, -1.0],
    "up": [0.0, 0.0, 1.0], "down": [0.0, 0.0, -1.0],
    "vertical": [0.0, 0.0, 1.0], "horizontal": [1.0, 0.0, 0.0],
}


def _axis_arg(axis):
    """An `axis` argument -> a 3-vector, accepting the signed-axis WORDS as well as JSON."""
    if isinstance(axis, str):
        word = axis.strip().lower()
        if word in _AXIS_WORDS:
            return list(_AXIS_WORDS[word])
    return _as_obj(axis)


def _load_doc(doc: Union[str, dict, None]) -> Any:
    """Accept a hydraulic doc as a dict, a JSON string, or a path to a ``.json`` file."""
    if doc is None:
        return None
    if isinstance(doc, dict):
        return doc
    if isinstance(doc, str):
        s = doc.strip()
        if s and s[0] != "{" and os.path.isfile(s):
            with open(s) as fh:
                return json.load(fh)
        return json.loads(s)
    raise TypeError(f"doc must be a dict, JSON string, or file path (got {type(doc).__name__})")


def _as_obj(x: Union[str, list, dict, None]) -> Any:
    """Parse an argument that may arrive as a JSON string or native object."""
    if x is None or isinstance(x, (list, dict)):
        return x
    if isinstance(x, str):
        s = x.strip()
        return json.loads(s) if s else None
    return x


def _active_doc() -> Optional[dict]:
    return _HYD.get("doc")


# ── hydraulic → piping / FEA handoff helpers (module-level: engine-less testable) ──
#
# The translators/verifies the tools used pre-T-RF20 now live in the C++ core
# (core/hydraulic/handoff.cpp — estimate_line_dp, hoses_to_piping, both envelope
# builders, the margin feedback); the tool bodies below are thin core_bridge
# wrappers. The pure-Python copies kept here are the simhandoff-envelope
# compositions the MCP test suite exercises engine-less (same shapes the core
# emits — byte-parity proven by the T-RF19 harness).

# ISO VG32 mineral hydraulic oil at ~40 °C — the default working fluid for line-loss
# estimates and the piping solve (ν = 32 cSt, ρ = 870 kg/m³ → μ = ρ·ν ≈ 0.0278 Pa·s).
HYDRAULIC_OIL = {"name": "hydraulic_oil_iso_vg32", "rho": 870.0, "mu": 0.0278}

# Default absolute roughness for hose/tube bore (m) — matches the piping solver's
# commercial-steel default so estimate vs solve compare like-for-like.
_HOSE_EPS_M = 4.6e-5


def estimate_line_dp(*, flow_lpm: float, id_mm: float, length_mm: float,
                     rho: float = HYDRAULIC_OIL["rho"],
                     mu: float = HYDRAULIC_OIL["mu"],
                     eps_m: float = _HOSE_EPS_M) -> dict:
    """Cheap per-hose Darcy–Weisbach pressure-drop estimate (pure math, no deps).

    This is the hydraulic-side prediction that ``hydraulic_verify_piping`` later
    cross-checks against the piping 1-D solve: laminar f = 64/Re below Re 2300,
    Swamee–Jain explicit Colebrook approximation above. Returns
    ``{ok, velocity_ms, reynolds, friction, regime, dp_kpa}``.
    """
    values = (flow_lpm, id_mm, length_mm, rho, mu, eps_m)
    if not all(isinstance(v, (int, float)) and math.isfinite(float(v))
               for v in values):
        return {"ok": False, "reason": "line-loss inputs must all be finite numbers"}
    if flow_lpm <= 0 or id_mm <= 0 or length_mm <= 0:
        return {"ok": False,
                "reason": "flow_lpm, id_mm, length_mm must all be finite and > 0"}
    if rho <= 0 or mu <= 0 or eps_m < 0:
        return {"ok": False,
                "reason": "rho and mu must be > 0; eps_m must be >= 0"}
    try:
        d_m = id_mm / 1000.0
        area = math.pi / 4.0 * d_m ** 2
        q_m3s = flow_lpm / 60_000.0
        v = q_m3s / area
        re = rho * v * d_m / mu
        if re < 2300.0:
            f = 64.0 / max(re, 1e-9)
            regime = "laminar"
        else:
            f = 0.25 / math.log10(
                eps_m / (3.7 * d_m) + 5.74 / re ** 0.9) ** 2
            regime = "turbulent (Swamee-Jain)"
        dp_pa = f * (length_mm / 1000.0) / d_m * 0.5 * rho * v ** 2
    except (OverflowError, ValueError, ZeroDivisionError):
        return {"ok": False, "reason": "line pressure-drop calculation overflowed"}
    if (not all(math.isfinite(v) for v in (area, q_m3s, v, re, f, dp_pa))
            or area <= 0 or q_m3s <= 0 or v <= 0 or re <= 0
            or f <= 0 or dp_pa < 0):
        return {"ok": False, "reason": "line pressure-drop calculation overflowed"}
    return {
        "ok": True,
        "velocity_ms": round(v, 4),
        "reynolds": round(re, 1),
        "friction": round(f, 5),
        "regime": regime,
        "dp_kpa": round(dp_pa / 1000.0, 4),
    }


def _piping_handoff_envelope(hyd_doc: dict, net: dict,
                             supply_pressure_bar: float) -> dict:
    """aicad.simhandoff/1 handoff envelope for the hydraulic→piping coupling.

    Doc-to-doc coupling: the generated ``aicad.piping/1`` document rides INLINE in a
    ``geometry_ref`` (contract §1 table, ``hydraulic_to_piping`` row) together with
    the boundary conditions + fluid in ``meta`` so ``piping_solve(source_handoff=...)``
    can consume the envelope with no other arguments.
    """
    conditions = [sh.condition(
        "supply_pressure", "pressure", supply_pressure_bar * 100.0, "kPa",
        "hydraulic circuit operating pressure (supply_pressure_bar x 100)",
        applied_to="all run starts")]
    expectations = []
    for row in net["lines"]:
        rid = row["run_id"]
        conditions.append(sh.condition(
            f"{rid}_flow", "flow", row["flow_m3h"], "m3/h",
            f"hydraulic hose {row['hose_id']} ({row['net_name']}) flow_lpm "
            "converted by x0.06 to m3/h",
            applied_to=rid))
        est = net["estimates"].get(rid) or {}
        if est.get("ok"):
            expectations.append(sh.expectation(
                f"pressure_drop:{rid}", est["dp_kpa"], "kPa",
                f"per-hose Darcy estimate (estimate_line_dp, {est['regime']}) "
                f"for hose {row['hose_id']}"))
    return sh.build_handoff(
        "hydraulic_to_piping",
        sh.study_ref("hydraulic", "hydraulic_piping_handoff",
                     doc_id=hyd_doc.get("id"),
                     result_digest={"hoses": len(net["lines"]),
                                    "supply_pressure_bar": supply_pressure_bar}),
        sh.study_ref("piping", "piping_solve"),
        geometry=[sh.geometry_ref("doc", "doc", inline=net["piping_doc"],
                                  derived_by="hoses_to_piping", units="mm")],
        conditions=conditions,
        expectations=expectations or None,
        meta={"boundary": net["boundary"], "fluid": net["fluid"],
              "lines": net["lines"],
              "topology_model": (
                  "independent_hose_line_cases (each run has its own "
                  "fixed-pressure start and prescribed-flow end; component-port "
                  "continuity is provenance only)")})


def _margin_update(supply_pressure_bar: float, load_pressure_bar: Optional[float],
                   est_loss_bar: Optional[float],
                   solved_loss_bar: Optional[float]) -> dict:
    """Pump sizing margin fed back from line losses (real losses change the margin)."""
    out: dict[str, Any] = {"supply_pressure_bar": supply_pressure_bar,
                           "load_pressure_bar": load_pressure_bar,
                           "line_loss_estimate_bar": est_loss_bar,
                           "line_loss_solved_bar": solved_loss_bar}
    if load_pressure_bar is not None:
        if est_loss_bar is not None:
            out["pump_margin_estimate_bar"] = round(
                supply_pressure_bar - load_pressure_bar - est_loss_bar, 4)
        if solved_loss_bar is not None:
            out["pump_margin_solved_bar"] = round(
                supply_pressure_bar - load_pressure_bar - solved_loss_bar, 4)
            out["pump_margin_solved_pct"] = round(
                out["pump_margin_solved_bar"] / supply_pressure_bar * 100.0, 2)
    return out


def _verify_piping_envelope(handoff: dict, solve_res: dict,
                            tolerance_pct: float) -> "tuple[dict, list]":
    """Build the hydraulic→piping verify envelope: per-line estimate vs solved dP.

    Returns ``(envelope, checks)``. Expectation quantities are
    ``pressure_drop:<run_id>``; solved values come from the matching solve link's
    ``dp_kpa``. Deviation is relative to the SOLVED value (the trusted number).
    """
    links = {lk.get("id"): lk for lk in solve_res.get("links", [])}
    checks = []
    unavailable = []
    for exp in handoff.get("expectations") or []:
        q = exp.get("quantity", "")
        rid = q.split(":", 1)[1] if ":" in q else q
        lk = links.get(rid)
        try:
            solved = abs(float(lk["dp_kpa"])) if lk is not None else math.nan
            est = float(exp["value"])
        except (KeyError, TypeError, ValueError):
            solved = est = math.nan
        if not math.isfinite(solved) or not math.isfinite(est):
            unavailable.append(
                f"{q or rid}: comparison unavailable (missing or non-finite "
                "estimate/solved pressure drop)")
            checks.append(sh.check(f"availability:{q or rid}", "1", 1.0, 0.0,
                                   100.0, 0.0, False))
            continue
        base = max(abs(solved), 1e-9)
        stored_dev = round(abs(solved - est) / base * 100.0, 2)
        checks.append(sh.check(q, "kPa", est, round(solved, 4),
                               stored_dev, tolerance_pct,
                               stored_dev <= tolerance_pct))
    if unavailable:
        verdict = ("FAIL: pressure-drop verification unavailable for "
                   f"{len(unavailable)} expected hose line(s)")
    else:
        n_pass = sum(1 for c in checks if c["pass"])
        verdict = (f"PASS: {n_pass}/{len(checks)} hose-line dP estimates within "
                   f"{tolerance_pct}% of the piping 1D solve"
                   if checks and n_pass == len(checks) else
                   f"FAIL: {len(checks) - n_pass}/{len(checks)} hose-line dP estimates "
                   f"deviate more than {tolerance_pct}% from the piping 1D solve")
    env = sh.build_verify(
        "hydraulic_to_piping",
        sh.study_ref("hydraulic", "hydraulic_piping_handoff"),
        sh.study_ref("piping", "piping_solve", handoff_id=handoff.get("id")),
        checks, verdict, handoff_id=handoff.get("id"),
        warnings=unavailable or None)
    return env, checks


def _unit3(axis) -> list:
    source = [0.0, 0.0, 1.0] if axis is None else axis
    if not isinstance(source, (list, tuple)) or len(source) != 3:
        raise ValueError("axis must be a 3-vector")
    v = [float(c) for c in source]
    if not all(math.isfinite(c) for c in v):
        raise ValueError("axis components must be finite")
    n = math.sqrt(sum(c * c for c in v))
    if not math.isfinite(n) or n <= 0:
        raise ValueError("axis must be non-zero")
    return [c / n for c in v]


def _cylinder_mount_forces(*, bore_mm: float, rod_mm: float, max_pressure_bar: float,
                           efficiency: float = 1.0) -> dict:
    """Mount reaction magnitudes (N) at max pressure — full bore extend, annulus retract."""
    a_bore = math.pi / 4.0 * (bore_mm * 1e-3) ** 2
    a_ann = math.pi / 4.0 * ((bore_mm * 1e-3) ** 2 - (rod_mm * 1e-3) ** 2)
    p_pa = max_pressure_bar * 1e5
    return {"extend_n": a_bore * p_pa * efficiency,
            "retract_n": a_ann * p_pa * efficiency}


def _fea_handoff_envelope(*, bore_mm: float, rod_mm: float, max_pressure_bar: float,
                          axis: list, efficiency: float,
                          load_face: str, fixture_face: str,
                          doc_id: Optional[str] = None,
                          target_doc_id: Optional[str] = None) -> dict:
    """aicad.simhandoff/1 handoff envelope for the hydraulic→FEA coupling.

    Mirrors the ``motion_to_fea`` pattern (contract §1 table): one force condition per
    load case (``case`` labels group extend/retract), plus the shared fixture. Extend
    thrust reacts INTO the mount (−axis); retract pulls the mount toward the load
    (+axis). Geometry may be empty (doc-to-doc) — the mounting geometry lives in the
    target FEA document when ``target_doc_id`` is given.
    """
    f = _cylinder_mount_forces(bore_mm=bore_mm, rod_mm=rod_mm,
                               max_pressure_bar=max_pressure_bar,
                               efficiency=efficiency)
    ax = _unit3(axis)
    ext_vec = [round(-c * f["extend_n"], 3) for c in ax]
    ret_vec = [round(c * f["retract_n"], 3) for c in ax]
    prov = (f"hydraulic cylinder bore {bore_mm} mm rod {rod_mm} mm at max pressure "
            f"{max_pressure_bar} bar (area x pressure x efficiency {efficiency})")
    conditions = [
        sh.condition("mount_force_extend", "force", ext_vec, "N",
                     prov + " - extend, full-bore area, reaction into mount (-axis)",
                     applied_to=load_face, case="extend_max_pressure"),
        sh.condition("mount_force_retract", "force", ret_vec, "N",
                     prov + " - retract, annulus area, reaction toward load (+axis)",
                     applied_to=load_face, case="retract_max_pressure"),
        sh.condition("mount_fixture", "fixture", {"type": "fixed"}, "1",
                     "mounting structure support (held fixed in every load case)",
                     applied_to=fixture_face),
    ]
    expectations = [
        sh.expectation("mount_force:extend_max_pressure", round(f["extend_n"], 3), "N",
                       "pi/4*bore^2 x max pressure x efficiency"),
        sh.expectation("mount_force:retract_max_pressure", round(f["retract_n"], 3), "N",
                       "pi/4*(bore^2-rod^2) x max pressure x efficiency"),
    ]
    return sh.build_handoff(
        "hydraulic_to_fea",
        sh.study_ref("hydraulic", "hydraulic_fea_handoff", doc_id=doc_id,
                     result_digest={"bore_mm": bore_mm, "rod_mm": rod_mm,
                                    "max_pressure_bar": max_pressure_bar,
                                    "efficiency": efficiency,
                                    "extend_force_n": round(f["extend_n"], 3),
                                    "retract_force_n": round(f["retract_n"], 3)}),
        sh.study_ref("fea", "run_fea_static", doc_id=target_doc_id),
        geometry=([sh.geometry_ref("mount", "doc_id", doc_id=target_doc_id,
                                   derived_by="caller-supplied mounting geometry")]
                  if target_doc_id else []),
        conditions=conditions,
        expectations=expectations,
        meta={"axis": ax, "bore_mm": bore_mm, "rod_mm": rod_mm,
              "max_pressure_bar": max_pressure_bar, "efficiency": efficiency})


def _verify_fea_envelope(handoff: dict, tolerance_pct: float,
                         fea_result: Optional[dict] = None) -> dict:
    """Verify both unambiguous cylinder load cases against independent A×P math."""
    meta = handoff.get("meta") or {}
    checks = []
    warnings: list = []
    unavailable: list = []
    try:
        bore = float(meta["bore_mm"])
        rod = float(meta.get("rod_mm", 0.0))
        pressure = float(meta["max_pressure_bar"])
        efficiency = float(meta.get("efficiency", 1.0))
        if (not all(math.isfinite(v) for v in
                    (bore, rod, pressure, efficiency)) or
                bore <= 0 or rod < 0 or rod >= bore or pressure <= 0 or
                not 0 < efficiency <= 1):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        unavailable.append(
            "hydraulic→FEA comparison unavailable: handoff metadata is "
            "missing or physically invalid")
        checks.append(sh.check("availability:handoff_metadata", "1", 1.0, 0.0,
                               100.0, 0.0, False))
        want = {}
        axis = None
    else:
        f = _cylinder_mount_forces(
            bore_mm=bore, rod_mm=rod, max_pressure_bar=pressure,
            efficiency=efficiency)
        want = {"extend_max_pressure": f["extend_n"],
                "retract_max_pressure": f["retract_n"]}
        try:
            if "axis" not in meta:
                raise ValueError("axis is missing")
            axis = _unit3(meta["axis"])
        except (TypeError, ValueError) as exc:
            axis = None
            message = f"invalid cylinder axis provenance: {exc}"
            unavailable.append(message)
            checks.append(sh.check("availability:cylinder_axis", "1", 1.0, 0.0,
                                   100.0, 0.0, False))

    vectors = {case: [] for case in want}
    for cond in handoff.get("conditions") or []:
        case = cond.get("case")
        if cond.get("kind") == "force" and case in vectors:
            vectors[case].append(cond.get("value"))
    for case, expect in want.items():
        candidates = vectors[case]
        vec = candidates[0] if len(candidates) == 1 else None
        try:
            if not isinstance(vec, list) or len(vec) != 3:
                raise ValueError
            numbers = [float(c) for c in vec]
            if not all(math.isfinite(c) for c in numbers):
                raise ValueError
        except (TypeError, ValueError):
            unavailable.append(
                f"{case}: comparison unavailable because exactly one finite "
                "3-vector force condition is required")
            checks.append(sh.check(f"availability:mount_force:{case}", "1",
                                   1.0, 0.0, 100.0, 0.0, False))
            continue
        mag = math.sqrt(sum(c * c for c in numbers))
        stored_dev = round(abs(mag - expect) / max(expect, 1e-9) * 100.0, 3)
        checks.append(sh.check(f"mount_force:{case}", "N", round(expect, 3),
                               round(mag, 3), stored_dev, tolerance_pct,
                               stored_dev <= tolerance_pct))
        if axis is not None and mag > 0:
            cosang = abs(sum(a * c for a, c in zip(axis, numbers))) / mag
            if cosang < 0.999:
                warnings.append(f"{case}: force vector not colinear with cylinder "
                                f"axis (|cos| = {cosang:.4f})")
    ok = bool(checks) and all(c["pass"] for c in checks)
    verdict = (f"PASS: all {len(checks)} mount-load case force magnitudes match "
               f"the independent area x pressure recompute within {tolerance_pct}%"
               if ok else "FAIL: mount-load cases are missing, ambiguous, invalid, "
                          "or deviate from the independent area x pressure recompute")
    advisories = None
    if isinstance(fea_result, dict):
        summ = fea_result.get("summary") or fea_result
        detail = {k: summ.get(k) for k in
                  ("max_von_mises_mpa", "max_disp_mm", "min_fos") if k in summ}
        if detail:
            advisories = [{"name": "fea_static_summary", "detail": detail}]
    return sh.build_verify(
        "hydraulic_to_fea",
        sh.study_ref("hydraulic", "hydraulic_fea_handoff"),
        sh.study_ref("fea", "run_fea_static", handoff_id=handoff.get("id")),
        checks, verdict, handoff_id=handoff.get("id"),
        advisories=advisories, warnings=(warnings + unavailable) or None)


def register_hydraulic_tools(mcp) -> None:
    """Register the AI-Hydraulic tools on the given FastMCP instance.

    Called from ``mcp_server/server.py`` after ``mcp`` is created. A function (not import-time
    side effects) so the tools can be driven against a stub ``mcp`` in tests — mirrors
    :func:`register_piping_tools`.
    """

    @mcp.tool()
    def hydraulic_new(name: str = "Hydraulic Circuit") -> str:
        """Create a new ``aicad.hydraulic/1`` hydraulic circuit document.

        Initialises an empty circuit with ISO 1219 unit defaults (bar / L/min / kN / mm).
        The new document becomes the active session document for subsequent
        ``hydraulic_add_component``, ``hydraulic_add_hose``, and ``hydraulic_bom`` calls.

        Returns the initial document envelope with schema, id, and empty component/hose lists.
        """
        try:
            r = cb.call("hydraulic_new", {"name": name})
        except Exception as exc:  # noqa: BLE001 — contract-shaped soft-fail, never raise
            return _dumps(_err(f"core engine unavailable: {exc}"))
        if r.get("ok") and isinstance(r.get("doc"), dict):
            _HYD["doc"] = r["doc"]
        return _dumps(r)

    @mcp.tool()
    def hydraulic_add_component(
        type: str,
        name: str = "",
        x: float = 0.0,
        y: float = 0.0,
        params: Union[str, dict, None] = None,
        sourcing: Union[str, dict, None] = None,
        doc: Union[str, dict, None] = None,
    ) -> str:
        """Add an ISO 1219 hydraulic component to the active circuit document.

        ``type`` must be one of the ISO 1219 component types defined in
        ``contracts/hydraulic_schema.md``:
        pump, cylinder_linear, cylinder_rotary, valve_directional, valve_check,
        valve_relief, valve_flow_control, accumulator, reservoir, motor_hydraulic,
        filter, heat_exchanger.
        Common spellings of those members are accepted and echoed under ``interpreted``
        (``cylinder``/``actuator``->cylinder_linear, ``motor``->motor_hydraulic,
        ``check_valve``->valve_check, ``relief_valve``->valve_relief, ``tank``->reservoir,
        ...). A bare ``valve`` is refused: four valve types exist and they do different
        things.

        ``x``, ``y`` set the schematic canvas position (mm). ``params`` is a JSON object
        of type-specific parameters (bore_mm, stroke_mm, displacement_cc_rev, etc.) —
        see the contract for per-type param keys. ``sourcing`` carries mpn/manufacturer/
        description/quantity/unit/category for BOM roll-up.

        ``doc`` overrides the session document (optional — if omitted, uses the last
        ``hydraulic_new`` document).

        Returns the updated document with the new component appended.
        """
        try:
            type, type_note = _resolve_component_type(type)
        except ValueError as exc:
            return _dumps(_err(str(exc),
                               hint="pick the specific ISO 1219 member — see the type list "
                                    "in contracts/hydraulic_schema.md"))
        try:
            d = _load_doc(doc) if doc is not None else _active_doc()
            p = _as_obj(params) or {}
            s = _as_obj(sourcing) or {}
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}"))

        try:
            r = cb.call("hydraulic_add_component", {
                "doc": d, "type": type, "name": name, "x": x, "y": y,
                "params": p, "sourcing": s})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        if r.get("ok") and isinstance(r.get("doc"), dict):
            _HYD["doc"] = r["doc"]  # additive "doc" key: the updated active circuit
        if r.get("ok") and type_note:
            r["interpreted"] = [type_note]
        return _dumps(r)

    @mcp.tool()
    def hydraulic_add_hose(
        net_name: str,
        connections: Union[str, list],
        doc: Union[str, dict, None] = None,
    ) -> str:
        """Add a hose/line net connecting two or more hydraulic component ports.

        ``net_name`` is a label for the net (e.g. ``"PRESSURE_LINE"``, ``"RETURN_LINE"``).
        ``connections`` is a JSON list of ``{"component_id": "c1", "port": "p1"}`` objects
        identifying which port on which component each end of the hose connects to.

        Port types: pressure, tank, work_a, work_b, drain, generic.

        ``doc`` overrides the session document (optional).

        Returns the updated document with the new hose appended.
        """
        try:
            d = _load_doc(doc) if doc is not None else _active_doc()
            conns = _as_obj(connections)
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}"))

        try:
            r = cb.call("hydraulic_add_hose", {
                "doc": d, "net_name": net_name, "connections": conns})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        if r.get("ok") and isinstance(r.get("doc"), dict):
            _HYD["doc"] = r["doc"]  # additive "doc" key: the updated active circuit
        return _dumps(r)

    @mcp.tool()
    def hydraulic_size_cylinder(
        bore_mm: float,
        pressure_bar: float,
        stroke_mm: float,
        rod_mm: float = 0.0,
        velocity_mm_s: float = 50.0,
        efficiency: float = 0.95,
    ) -> str:
        """Size a hydraulic cylinder: extend/retract force, flow, and cycle time.

        Physics:
        - Extend force (kN) = bore area (m²) × pressure (Pa) × efficiency / 1000
        - Retract force uses the annulus area (bore² − rod²)
        - Flow (L/min) = area (m²) × velocity (m/s) × 60 × 1000
        - Cycle time (s) = stroke_mm / velocity_mm_s

        Hand-calc check: 50 mm bore, 200 bar, efficiency=1 → ≈ 39.27 kN extend force.

        All inputs in the circuit's native units (mm, bar). Returns a contract-shaped dict
        with all sizing results plus recommended pump flow.
        """
        try:
            r = cb.call("hydraulic_size_cylinder", {
                "bore_mm": bore_mm, "rod_mm": rod_mm, "pressure_bar": pressure_bar,
                "stroke_mm": stroke_mm, "velocity_mm_s": velocity_mm_s,
                "efficiency": efficiency})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        return _dumps(r)

    @mcp.tool()
    def hydraulic_size_pump(
        flow_lpm: float,
        pressure_bar: float,
        efficiency: float = 0.85,
        speed_rpm: float = 1500.0,
    ) -> str:
        """Size a hydraulic pump: displacement, hydraulic power, shaft power, torque.

        Physics:
        - Displacement (cc/rev) = Q (L/min) × 1000 / speed (rpm)
        - Hydraulic power (kW) = Q (m³/s) × ΔP (Pa) / 1000
        - Shaft power (kW) = hydraulic power / efficiency
        - Torque (N·m) = shaft power × 1000 / (2π × speed/60)

        Hand-calc check: 10 L/min, 200 bar, η=0.85 → shaft ≈ 3.92 kW.

        Returns displacement, power, and torque in a contract-shaped dict ready to pass to
        component selection.
        """
        try:
            r = cb.call("hydraulic_size_pump", {
                "flow_lpm": flow_lpm, "pressure_bar": pressure_bar,
                "efficiency": efficiency, "speed_rpm": speed_rpm})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        return _dumps(r)

    @mcp.tool()
    def hydraulic_bom(doc: Union[str, dict, None] = None) -> str:
        """Extract a sourcing bill of materials from the active hydraulic circuit.

        Emits VendorPart-compatible sourcing lines for every component in the circuit:
        id, type, name, mpn, manufacturer, description, quantity, unit, category.

        Components without an MPN are included but flagged as un-sourced. A summary
        counts components by type and lists those missing MPNs.

        ``doc`` overrides the session document (optional — uses the last ``hydraulic_new``
        document if omitted).

        Returns a list of BOM lines and a sourcing summary.
        """
        try:
            d = _load_doc(doc) if doc is not None else _active_doc()
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}"))

        try:
            r = cb.call("hydraulic_bom", {"doc": d})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        return _dumps(r)

    @mcp.tool()
    def hydraulic_piping_handoff(supply_pressure_bar: float,
                                 flow_lpm: float,
                                 load_pressure_bar: Optional[float] = None,
                                 lines: Union[str, dict, None] = None,
                                 fluid: Union[str, dict, None] = None,
                                 default_id_mm: float = 12.7,
                                 default_length_mm: float = 1000.0,
                                 tolerance_pct: float = 30.0,
                                 doc: Union[str, dict, None] = None) -> str:
        """Hand the circuit's hose/tube runs to the piping 1-D solver for REAL per-line
        pressure drops at the operating point — replacing per-hose estimates — and feed
        the losses back into the pump sizing margin.

        Each hose in the active ``aicad.hydraulic/1`` circuit becomes a straight pipe
        run with its exact bore (``bore_mm``, since SAE hose sizes aren't NPS catalog
        pipes). ``supply_pressure_bar`` is the circuit operating pressure (fixed at
        every run start); ``flow_lpm`` the default line flow. Per-hose overrides go in
        ``lines`` = ``{hose_id: {"id_mm":.., "length_mm":.., "flow_lpm":..}}`` (the
        same keys are also read from fields stored on the hose object itself).
        ``fluid`` overrides the default ISO VG32 oil ``{"rho":870,"mu":0.0278}``.

        The piping solve ALWAYS runs live in the C++ core (it IS the solver — the old
        fluids-absent ``solve_pending`` stage does not exist on the core path): the
        result includes per-line dP + the updated pump margin
        (``margin.pump_margin_solved_bar`` = supply − load − real line losses, needs
        ``load_pressure_bar``) plus the estimate-vs-solve verify verdict. The generated
        network can always be re-solved with ``piping_solve(source_handoff=<simhandoff>)``.

        Conforms to ``aicad.simhandoff/1`` (coupling ``hydraulic_to_piping``): the
        generated piping doc rides INLINE in the envelope's geometry ref, per-line
        flows are conditions, and the per-hose Darcy estimates are expectations that
        ``hydraulic_verify_piping`` cross-checks at ``tolerance_pct``.
        """
        try:
            d = _load_doc(doc) if doc is not None else _active_doc()
            ln = _as_obj(lines)
            fl = _as_obj(fluid)
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}"))

        try:
            r = cb.call("hydraulic_piping_handoff", {
                "doc": d, "supply_pressure_bar": supply_pressure_bar,
                "flow_lpm": flow_lpm, "load_pressure_bar": load_pressure_bar,
                "lines": ln, "fluid": fl, "default_id_mm": default_id_mm,
                "default_length_mm": default_length_mm,
                "tolerance_pct": tolerance_pct})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        if r.get("ok") and r.get("stage") == "solved":
            # Session state stays MCP-side: remember the live handoff+solve pair
            # so hydraulic_verify_piping can be called with no arguments.
            _HYD["piping_handoff"] = {"handoff": r.get("simhandoff"),
                                      "solve": r.get("solve")}
        return _dumps(r)

    @mcp.tool()
    def hydraulic_verify_piping(handoff: Union[str, dict, None] = None,
                                solve_result: Union[str, dict, None] = None,
                                supply_pressure_bar: Optional[float] = None,
                                load_pressure_bar: Optional[float] = None,
                                tolerance_pct: float = 30.0) -> str:
        """Verify step for the hydraulic→piping coupling: per-hose Darcy estimate vs
        piping 1-D solve, per line, plus the pump-margin feedback.

        ``handoff`` is the ``simhandoff`` envelope from ``hydraulic_piping_handoff``
        (or the whole result — the envelope is pulled from its ``simhandoff`` key);
        ``solve_result`` is the ``piping_solve`` output for the generated network.
        With no arguments, uses the session's last live handoff+solve pair.

        Each check compares one line's estimated dP (envelope expectation
        ``pressure_drop:<run_id>``) against the solved link ``dp_kpa`` at
        ``tolerance_pct`` (deviation relative to the solved value). Pass
        ``supply_pressure_bar``/``load_pressure_bar`` to also get the updated pump
        margin from the REAL losses. Returns ``{ok, pass, verdict, checks, margin}``
        with a validated ``aicad.simhandoff/1`` verify envelope under ``simhandoff``.
        """
        try:
            h = _as_obj(handoff)
            srs = _as_obj(solve_result)
        except (ValueError, TypeError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}"))
        sess = _HYD.get("piping_handoff") or {}
        if h is None:
            h = sess.get("handoff")
        if srs is None:
            srs = sess.get("solve")

        try:
            # The core also accepts the whole producer result for `handoff` (it pulls
            # the envelope from its `simhandoff` key), exactly like the old handler.
            r = cb.call("hydraulic_verify_piping", {
                "handoff": h, "solve_result": srs,
                "supply_pressure_bar": supply_pressure_bar,
                "load_pressure_bar": load_pressure_bar,
                "tolerance_pct": float(tolerance_pct)})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        return _dumps(r)

    @mcp.tool()
    def hydraulic_fea_handoff(bore_mm: float,
                              max_pressure_bar: float,
                              rod_mm: float = 0.0,
                              axis: Union[str, list, None] = None,
                              load_face: str = "the cylinder mount face",
                              fixture_face: str = "the base",
                              efficiency: float = 1.0,
                              target_doc_id: Optional[str] = None,
                              doc: Union[str, dict, None] = None) -> str:
        """Turn cylinder/actuator mount loads at max pressure into static-FEA load
        cases on the mounting geometry (companion of the motion→FEA pattern).

        Computes the mount reaction at ``max_pressure_bar``: extend thrust =
        π/4·bore² · P (full bore, reacts INTO the mount along −axis) and retract pull
        = π/4·(bore²−rod²) · P (annulus, +axis). ``axis`` is the cylinder axis in the
        mount document's world frame (default [0,0,1]) as a 3-vector or a signed-axis
        word (``"z"``/``"+z"``/``"-x"``/``"up"``/``"down"``); ``load_face`` /
        ``fixture_face`` are plain-word face queries on the mounting part;
        ``target_doc_id`` names the open FEA target document when known.

        Returns ready-to-call ``run_fea_static`` argument sets under ``fea_args``
        (one per load case: ``extend_max_pressure``, ``retract_max_pressure``) plus a
        validated ``aicad.simhandoff/1`` handoff envelope (coupling
        ``hydraulic_to_fea``) whose conditions carry the force vectors with ``case``
        labels and whose expectations record the A×P magnitudes for
        ``hydraulic_verify_fea`` to cross-check. This tool does NOT run the solve —
        pass ``fea_args`` straight to ``run_fea_static(doc_id=target_doc_id, ...)``.
        """
        try:
            ax = _axis_arg(axis)
            d = _load_doc(doc) if doc is not None else _active_doc()
        except (ValueError, TypeError, OSError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}",
                               hint='axis must be a non-zero [x,y,z] vector or a signed-axis '
                                    'word like "z" / "-x" / "up"'))

        try:
            r = cb.call("hydraulic_fea_handoff", {
                "bore_mm": float(bore_mm), "max_pressure_bar": float(max_pressure_bar),
                "rod_mm": float(rod_mm), "axis": ax, "load_face": load_face,
                "fixture_face": fixture_face, "efficiency": float(efficiency),
                "target_doc_id": target_doc_id, "doc": d})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        if r.get("ok") and isinstance(r.get("simhandoff"), dict):
            _HYD["fea_handoff"] = r["simhandoff"]  # session state stays MCP-side
        return _dumps(r)

    @mcp.tool()
    def hydraulic_verify_fea(handoff: Union[str, dict, None] = None,
                             fea_result: Union[str, dict, None] = None,
                             tolerance_pct: float = 0.5) -> str:
        """Verify step for the hydraulic→FEA coupling: recompute the mount forces
        independently (area × max pressure from the envelope's recorded cylinder
        parameters) and check each load-case force condition matches in magnitude at
        ``tolerance_pct`` — plus a colinearity warning if a vector drifted off the
        cylinder axis. ``handoff`` defaults to the session's last
        ``hydraulic_fea_handoff`` envelope (or pass that tool's result).
        ``fea_result`` (a ``run_fea_static`` output) is attached as an advisory
        (max stress / displacement / FoS) — it does not gate the verdict.

        Returns ``{ok, pass, verdict, checks}`` with a validated
        ``aicad.simhandoff/1`` verify envelope under ``simhandoff``.
        """
        try:
            h = _as_obj(handoff)
            fr = _as_obj(fea_result)
        except (ValueError, TypeError) as exc:
            return _dumps(_err(f"could not parse inputs: {exc}"))
        if h is None:
            h = _HYD.get("fea_handoff")

        try:
            # The core unwraps a whole producer result (pulls `simhandoff`) itself.
            r = cb.call("hydraulic_verify_fea", {
                "handoff": h, "fea_result": fr,
                "tolerance_pct": float(tolerance_pct)})
        except Exception as exc:  # noqa: BLE001
            return _dumps(_err(f"core engine unavailable: {exc}"))
        return _dumps(r)
