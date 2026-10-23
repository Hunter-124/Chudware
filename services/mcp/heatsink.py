"""Heatsink selection loop — parts-library + parametric candidates solved
to a Tj (or case-temp) target on top of the ECAD->thermal pipeline.

Tool:
  heatsink_select   given a hot component ref + a target from the last
                    ecad_thermal_handoff run: gather candidates (curated parts-library
                    heatsink catalog, domain "thermal", + a parametric plain-finned
                    aluminum sink generator), "mount" each on the component top
                    (bonded contact + TIM interface resistance), re-solve the thermal
                    study per candidate reusing the CACHED board mesh, and return a
                    ranked table {candidate, achieved_tj_c, delta_vs_target, mass_g,
                    cost} with the winner's study kept + an aicad.simhandoff/1 verify
                    envelope. Impossible targets are reported honestly (best
                    achievable named, never a fake win).

THE MOUNT MODEL (documented honesty): the core thermal solver takes ONE material per
study, so a fused aluminum sink cannot carry its own conductivity. Instead a mounted
candidate is modeled as an EFFECTIVE CONVECTION over the component's top face:
    h_eff = 1 / ((R_tim + R_sa) * A_top)
where R_sa is the sink-to-ambient resistance (library datasheet nominal, or the
plain-fin analytic estimate for parametric sinks: fin efficiency tanh(mL)/mL with the
environment's film coefficient) and R_tim = t_tim / (k_tim * A_contact) is the thermal
interface material resistance (defaults: 0.1 mm of 3.0 W/m-K grease-class TIM).
Board geometry is UNCHANGED, so the board mesh from the baseline solve is reused for
every candidate; board spreading and neighbor heating stay fully in the solve. What
the model does NOT capture: heat spreading beyond the component footprint through the
sink base overhang (conservative — a large overhung sink performs slightly better
than modeled), and sink-to-board radiation/bypass. Both are listed in assumptions.

When no sim-enabled libchudware is loadable, the tool degrades HONESTLY to
mode="network_estimate": achieved_tj = T_amb + P*(theta_jc + R_tim + R_sa) — a
series-resistance hand calc that IGNORES board coupling/neighbor heating, flagged as
such in warnings.
"""

from __future__ import annotations

import math
from typing import Optional

from services.mcp import simhandoff as sh
from services.mcp._envelopes import plain_dumps as _dumps, report_err as _err
# Import the same canonical module object the server registers so thermal state is shared.
from services.mcp import ecad_thermal as et

# Aluminum (6063-class extrusion): conductivity + density for fin efficiency + mass.
ALU_K_W_MK = 200.0
ALU_RHO_KG_M3 = 2700.0

# TIM interface defaults (documented): 0.1 mm bondline of a 3.0 W/m-K grease/pad-class
# thermal interface material across the sink/component contact area.
TIM_T_MM_DEFAULT = 0.1
TIM_K_W_MK_DEFAULT = 3.0

# Parametric family: sink base sizes as multiples of the component footprint span
# (clamped to sane mm), with fin height ~= base size (squat->tall progression).
_PARAM_SCALES = (1.5, 2.5, 4.0, 6.0)
_PARAM_MIN_BASE_MM = 12.0
_PARAM_MAX_BASE_MM = 100.0

_HEATSINK: dict = {"winner_study": None}   # kept winner study (session state)


# --------------------------------------------------------------------------- pure helpers
# Engine-free so tests can drive the generator/physics without OCP/libchudware.


def validate_sink_spec(spec: dict) -> list:
    """Structural validity of a plain-finned sink spec. Returns [] when the geometry
    is buildable: positive dims, fins fit across the base with positive gaps."""
    errs: list = []
    for key in ("base_l_mm", "base_w_mm", "base_t_mm", "n_fins", "fin_h_mm", "fin_t_mm"):
        v = spec.get(key)
        if not isinstance(v, (int, float)) or v <= 0:
            errs.append(f"{key} must be > 0 (got {v!r})")
    if errs:
        return errs
    n = int(spec["n_fins"])
    if n != spec["n_fins"] or n < 2:
        errs.append(f"n_fins must be an integer >= 2 (got {spec['n_fins']!r})")
        return errs
    fins_w = n * float(spec["fin_t_mm"])
    if fins_w >= float(spec["base_w_mm"]):
        errs.append(f"{n} fins x {spec['fin_t_mm']} mm = {fins_w} mm do not fit on a "
                    f"{spec['base_w_mm']} mm base with positive air gaps")
    if float(spec["fin_h_mm"]) > 12.0 * float(spec["fin_t_mm"]):
        errs.append(f"fin aspect ratio {spec['fin_h_mm']}/{spec['fin_t_mm']} > 12 — "
                    "unmanufacturable extrusion (thicken fins or shorten them)")
    return errs


def fin_pitch_mm(spec: dict) -> float:
    """Center-to-center fin pitch across the base width (fins run along base_l)."""
    n = int(spec["n_fins"])
    return (float(spec["base_w_mm"]) - float(spec["fin_t_mm"])) / max(n - 1, 1)


def sink_mass_g(spec: dict) -> float:
    """Aluminum mass: base slab + N fins."""
    base = spec["base_l_mm"] * spec["base_w_mm"] * spec["base_t_mm"]
    fins = int(spec["n_fins"]) * spec["base_l_mm"] * spec["fin_t_mm"] * spec["fin_h_mm"]
    return round((base + fins) * 1e-3 * ALU_RHO_KG_M3 * 1e-3, 2)  # mm3 -> g


def sink_theta_sa(spec: dict, h_w_m2k: float) -> float:
    """Sink-to-ambient resistance (C/W) for a plain fin array in a film coefficient
    ``h_w_m2k``: fin surfaces at fin efficiency tanh(mL)/mL (m = sqrt(2h/(k t))) plus
    the exposed base between fins at unit efficiency. Standard textbook estimate —
    good to selection-grade accuracy, not a wind-tunnel number."""
    h = max(float(h_w_m2k), 1e-6)
    t = float(spec["fin_t_mm"]) * 1e-3
    l_fin = float(spec["fin_h_mm"]) * 1e-3
    m = math.sqrt(2.0 * h / (ALU_K_W_MK * t))
    ml = m * l_fin
    eta = math.tanh(ml) / ml if ml > 1e-9 else 1.0
    n = int(spec["n_fins"])
    length = float(spec["base_l_mm"]) * 1e-3
    # Per-fin wetted area: two faces + tip.
    a_fin = length * (2.0 * l_fin + t)
    # Exposed base between/around fins.
    a_base = length * max(float(spec["base_w_mm"]) * 1e-3 - n * t, 0.0)
    a_eff = n * eta * a_fin + a_base
    return 1.0 / (h * max(a_eff, 1e-9))


def sink_ops(spec: dict, at: Optional[list] = None) -> list:
    """Engine add_features ops building the sink: base box + N fin boxes, base corner
    at ``at`` (default origin), fins running along base_l, spread across base_w."""
    ax, ay, az = (at or [0.0, 0.0, 0.0])
    ops = [{"op": "add_feature", "feature": {
        "type": "box", "name": "SinkBase",
        "params": {"length": {"value": spec["base_l_mm"], "expr": None},
                   "width": {"value": spec["base_w_mm"], "expr": None},
                   "height": {"value": spec["base_t_mm"], "expr": None},
                   "at": [ax, ay, az], "centered": False}}}]
    pitch = fin_pitch_mm(spec)
    for i in range(int(spec["n_fins"])):
        ops.append({"op": "add_feature", "feature": {
            "type": "box", "name": f"Fin{i + 1}",
            "params": {"length": {"value": spec["base_l_mm"], "expr": None},
                       "width": {"value": spec["fin_t_mm"], "expr": None},
                       "height": {"value": spec["fin_h_mm"], "expr": None},
                       "at": [ax, ay + i * pitch, az + spec["base_t_mm"]],
                       "centered": False, "mode": "boss", "merge": True}}})
    return ops


def parametric_candidates(footprint_l_mm: float, footprint_w_mm: float,
                          scales: Optional[tuple] = None) -> list:
    """Plain-finned candidate specs sized as multiples of the component footprint.
    Every returned spec passes :func:`validate_sink_spec` by construction."""
    span = max(float(footprint_l_mm), float(footprint_w_mm), 1.0)
    out: list = []
    for s in (scales or _PARAM_SCALES):
        base = min(max(span * s, _PARAM_MIN_BASE_MM), _PARAM_MAX_BASE_MM)
        fin_t = max(1.0, round(base / 30.0, 1))
        fin_h = min(round(base * 0.6, 1), 12.0 * fin_t)
        n = max(2, int(base // (3.0 * fin_t)))
        while n * fin_t >= base and n > 2:
            n -= 1
        spec = {"id": f"param-{int(round(base))}sq",
                "base_l_mm": round(base, 1), "base_w_mm": round(base, 1),
                "base_t_mm": max(2.0, round(base / 15.0, 1)),
                "n_fins": n, "fin_h_mm": fin_h, "fin_t_mm": fin_t,
                "material": "aluminum"}
        errs = validate_sink_spec(spec)
        if errs:  # never emit an invalid spec; skip (should not happen by construction)
            continue
        if not any(abs(c["base_l_mm"] - spec["base_l_mm"]) < 0.5 for c in out):
            out.append(spec)
    return out


def tim_resistance_c_per_w(t_mm: float, k_w_mk: float, area_mm2: float) -> float:
    """Bondline resistance ``R = t / (k A)`` for physically valid TIM inputs."""
    values: dict[str, float] = {}
    for name, value in (("t_mm", t_mm), ("k_w_mk", k_w_mk),
                        ("area_mm2", area_mm2)):
        if isinstance(value, bool):
            raise ValueError(f"{name} must be a finite number, got {value!r}")
        try:
            finite = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{name} must be a finite number, got {value!r}") from None
        if not math.isfinite(finite):
            raise ValueError(f"{name} must be finite, got {value!r}")
        values[name] = finite
    if values["t_mm"] < 0.0:
        raise ValueError("t_mm must be >= 0")
    if values["k_w_mk"] <= 0.0:
        raise ValueError("k_w_mk must be > 0")
    if values["area_mm2"] <= 0.0:
        raise ValueError("area_mm2 must be > 0")
    return (values["t_mm"] * 1e-3) / (
        values["k_w_mk"] * values["area_mm2"] * 1e-6)


def library_candidates(env_name: str, h_w_m2k: float,
                       footprint_l_mm: float, footprint_w_mm: float,
                       limit: int = 12) -> tuple:
    """Curated parts-library heatsinks (domain "thermal") as candidate dicts.

    Uses the datasheet-class theta_sa nominal matching the environment (natural vs
    forced); when the environment's h is neither preset, the natural value is scaled
    by h_ratio^-0.8 (turbulent-ish film scaling) and flagged. Sinks smaller than the
    component footprint are dropped (they cannot cover the die/case). Returns
    (candidates, notice)."""
    try:
        from services.parts import search as parts_search
        records = parts_search("heatsink", domain="thermal", include_live=False,
                               limit=limit)
    except Exception as exc:  # noqa: BLE001
        return [], f"parts library unavailable ({type(exc).__name__}: {exc})"
    out: list = []
    for rec in records:
        p = rec.get("params") or {}
        th_nat = p.get("theta_sa_c_per_w")
        if not isinstance(th_nat, (int, float)):
            continue  # no thermal metadata -> cannot rank honestly
        if (p.get("l_mm", 0) < footprint_l_mm * 0.9
                or p.get("w_mm", 0) < footprint_w_mm * 0.9):
            continue  # cannot cover the component
        th_forced = p.get("theta_sa_forced_c_per_w")
        basis = "datasheet natural-convection nominal"
        if h_w_m2k >= 30.0 and isinstance(th_forced, (int, float)):
            theta, basis = float(th_forced), "datasheet forced-air nominal"
        elif h_w_m2k <= 15.0:
            theta = float(th_nat)
        else:
            theta = float(th_nat) * (10.0 / h_w_m2k) ** 0.8
            basis = f"natural nominal scaled to h={h_w_m2k} W/m2K (h^-0.8)"
        out.append({"id": rec.get("designation") or rec.get("part_id"),
                    "part_id": rec.get("part_id"), "source": "library",
                    "title": rec.get("title"),
                    "base_l_mm": p.get("l_mm"), "base_w_mm": p.get("w_mm"),
                    "h_mm": p.get("h_mm"),
                    "theta_sa_c_per_w": round(theta, 3),
                    "theta_basis": basis,
                    "mass_g": p.get("mass_g"),
                    "cost_usd": p.get("cost_usd")})
    return out, None


def gather_candidates(env_name: str, h_w_m2k: float,
                      footprint_l_mm: float, footprint_w_mm: float,
                      include_library: bool = True,
                      include_parametric: bool = True,
                      max_candidates: int = 10) -> tuple:
    """Library + parametric candidate list, each carrying theta_sa_c_per_w for the
    given environment. Returns (candidates, notices)."""
    cands: list = []
    notices: list = []
    if include_library:
        lib, notice = library_candidates(env_name, h_w_m2k,
                                         footprint_l_mm, footprint_w_mm)
        if notice:
            notices.append(notice)
        cands.extend(lib)
    if include_parametric:
        for spec in parametric_candidates(footprint_l_mm, footprint_w_mm):
            cands.append({**spec, "source": "parametric",
                          "title": (f"parametric plain-finned Al sink "
                                    f"{spec['base_l_mm']}x{spec['base_w_mm']} mm, "
                                    f"{spec['n_fins']} fins x {spec['fin_h_mm']} mm"),
                          "theta_sa_c_per_w": round(sink_theta_sa(spec, h_w_m2k), 3),
                          "theta_basis": ("plain-fin analytic estimate "
                                          f"(eta_fin model, h={h_w_m2k} W/m2K)"),
                          "mass_g": sink_mass_g(spec),
                          "cost_usd": None})
    cands.sort(key=lambda c: c["theta_sa_c_per_w"], reverse=True)  # small->large sink
    if len(cands) > max_candidates:
        notices.append(f"{len(cands) - max_candidates} candidate(s) beyond "
                       f"max_candidates={max_candidates} dropped (kept the spread).")
        # Keep an even spread across the theta range rather than one end.
        idx = ([0] if max_candidates == 1 else
               [round(i * (len(cands) - 1) / (max_candidates - 1))
                for i in range(max_candidates)])
        cands = [cands[i] for i in sorted(set(idx))]
    return cands, notices


# --------------------------------------------------------------------------- implementation


def heatsink_select_impl(component_ref: str,
                         tj_target_c: Optional[float] = None,
                         t_case_target_c: Optional[float] = None,
                         tim_t_mm: float = TIM_T_MM_DEFAULT,
                         tim_k_w_mk: float = TIM_K_W_MK_DEFAULT,
                         include_library: bool = True,
                         include_parametric: bool = True,
                         max_candidates: int = 8,
                         doc_id: Optional[str] = None,
                         handoff_id: Optional[str] = None) -> dict:
    """Pick a heatsink for a hot component: candidates re-solved to a Tj target.

    Returns a dict (not JSON). Worker seam (multiphysics_rpc) and the MCP
    tool both call this; the MCP tool wraps with json.dumps.

    Optional worker ownership: doc_id and handoff_id (the source result.simhandoff.id)
    must match the single captured last thermal context before any candidate work.
    Omit both to intentionally select from the last thermal study, as legacy MCP
    calls do. max_candidates must be a positive integer; 1 keeps the smallest sink.
    """
    warnings: list = []
    try:
        # Validate the public TIM inputs before context/candidate work. In particular,
        # NaN must never flow through comparisons and masquerade as a winning score.
        tim_resistance_c_per_w(tim_t_mm, tim_k_w_mk, 1.0)
        tim_t_mm = float(tim_t_mm)
        tim_k_w_mk = float(tim_k_w_mk)
    except ValueError as exc:
        return {"ok": False, "error": f"invalid TIM physics: {exc}",
                "warnings": [],
                "hint": "Use a finite non-negative bondline thickness and a finite "
                        "positive TIM conductivity.",
                "next_actions": []}
    if (isinstance(max_candidates, bool)
            or not isinstance(max_candidates, int) or max_candidates < 1):
        return {"ok": False, "reason": "invalid_max_candidates",
                "error": f"max_candidates must be a positive integer (got {max_candidates!r})",
                "warnings": [], "hint": "Set max_candidates to an integer >= 1.",
                "next_actions": []}
    ctx = et._THERMAL.get("last_ctx")
    if not isinstance(ctx, dict):
        return {"ok": False, "error": "no thermal context — run ecad_thermal_handoff() first",
                "warnings": [],
                "hint": "heatsink_select re-solves the last handoff's study; "
                        "there is nothing cached yet.",
                "next_actions": ["ecad_thermal_handoff()",
                                 "heatsink_select('U1', tj_target_c=85)"]}
    expected_source = {"doc_id": doc_id, "handoff_id": handoff_id}
    current_source = {"doc_id": ctx.get("doc_id"),
                      "handoff_id": ctx.get("handoff_envelope_id")}
    for key, reason in (("doc_id", "thermal_source_doc_mismatch"),
                        ("handoff_id", "thermal_source_handoff_mismatch")):
        expected = expected_source[key]
        if expected is not None and expected != current_source[key]:
            return {
                "ok": False, "reason": reason,
                "error": f"Thermal source {key} mismatch: requested {expected!r}, "
                         f"but the cached study belongs to {current_source[key]!r}. "
                         "Heatsink selection was refused before candidate work.",
                "expected_source": expected_source, "current_source": current_source,
                "warnings": [],
                "hint": "Run the thermal handoff again for the selected board, then "
                        "select its new result as the heatsink source.",
                "next_actions": ["ecad_thermal_handoff(doc_id=<selected board>)",
                                 "Select the new thermal result and retry heatsink selection"],
            }
    if tj_target_c is None and t_case_target_c is None:
        return {"ok": False, "error": "pass a target: tj_target_c or t_case_target_c",
                "warnings": [], "hint": "pass a target: tj_target_c or t_case_target_c",
                "next_actions": []}

    env = ctx["env"]
    t_amb = float(env["t_amb_c"])
    components = ctx["components"]
    rec = next((r for r in components
                if (r.get("ref") or r.get("id")) == component_ref), None)
    if rec is None or not rec.get("fea"):
        known = [r.get("ref") or r.get("id") for r in components if r.get("fea")]
        return {"ok": False,
                "error": f"component {component_ref!r} not found in the last handoff",
                "warnings": [], "hint": f"components with bodies: {known}",
                "next_actions": []}
    p_w = et.component_power_w(rec)
    if p_w is None:
        return {"ok": False,
                "error": f"{component_ref} has no power metadata — nothing to sink",
                "warnings": [],
                "hint": "seed power_w via library eng blocks or sim_params.eng.",
                "next_actions": []}
    fea = rec["fea"]
    rect = fea["rect_mm"]
    fp_l, fp_w = rect[2] - rect[0], rect[3] - rect[1]
    try:
        a_top_mm2 = float(fea["top_area_mm2"])
        tim_resistance_c_per_w(tim_t_mm, tim_k_w_mk, a_top_mm2)
    except (TypeError, ValueError, OverflowError) as exc:
        return {"ok": False,
                "error": f"invalid TIM/contact physics for {component_ref}: {exc}",
                "warnings": warnings,
                "hint": "The component top contact area must be finite and > 0 before "
                        "heatsink candidates can be ranked.",
                "next_actions": []}
    theta_jc = (rec.get("theta_c_per_w") or {}).get("jc")

    target_kind = "tj" if tj_target_c is not None else "t_case"
    target_c = float(tj_target_c if tj_target_c is not None else t_case_target_c)
    if target_kind == "tj" and not isinstance(theta_jc, (int, float)):
        warnings.append(f"{component_ref} has no theta_jc — Tj target checked "
                        "against CASE temperature (Tj >= case; optimistic by the "
                        "unknown junction-to-case rise).")
    if target_c <= t_amb:
        return {"ok": False,
                "error": f"target {target_c} C is at/below ambient {t_amb} C — no "
                         "passive heatsink can cool below ambient",
                "warnings": warnings,
                "hint": "lower t_amb_c / change environment, or use active "
                        "cooling (TEC) which is out of this tool's scope.",
                "next_actions": []}

    cands, notices = gather_candidates(
        env.get("name", "custom"), float(env["h_w_m2k"]), fp_l, fp_w,
        include_library=include_library, include_parametric=include_parametric,
        max_candidates=max_candidates)
    warnings.extend(notices)
    if not cands:
        return {"ok": False, "error": "no heatsink candidates available",
                "warnings": warnings,
                "hint": "enable include_parametric=True or check the parts "
                        "library (domain 'thermal').",
                "next_actions": []}

    # ── solve leg: FEA re-solve with cached mesh, else network estimate ──
    mesh = ctx.get("mesh")
    cb = None
    if mesh is not None:
        try:
            from services.mcp import core_bridge as _cb
            cb = _cb
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"core bridge unavailable ({exc}) — network estimate.")
    mode = "fea_resolve" if (mesh is not None and cb is not None) else \
           "network_estimate"
    if mode == "network_estimate":
        warnings.append(
            "network_estimate mode: Tj = Ta + P*(theta_jc + R_tim + R_sa) — "
            "board spreading and NEIGHBOR HEATING are ignored (optimistic). "
            "Run ecad_thermal_handoff() live (sim-enabled libchudware) for the "
            "FEA re-solve per candidate.")

    conv_base, fixtures = _base_convection(ctx)
    material = ctx["material"]
    rows: list = []
    winner_solve: Optional[dict] = None
    for cand in cands:
        a_contact = min(a_top_mm2,
                        float(cand["base_l_mm"]) * float(cand["base_w_mm"]))
        r_tim = tim_resistance_c_per_w(tim_t_mm, tim_k_w_mk, a_contact)
        r_total = r_tim + float(cand["theta_sa_c_per_w"])
        row = {"candidate": cand["id"], "source": cand["source"],
               "title": cand.get("title"),
               "theta_sa_c_per_w": cand["theta_sa_c_per_w"],
               "theta_basis": cand.get("theta_basis"),
               "r_tim_c_per_w": round(r_tim, 4),
               "mass_g": cand.get("mass_g"), "cost_usd": cand.get("cost_usd"),
               "achieved_tj_c": None, "achieved_t_case_c": None,
               "basis": None, "solve": None}
        if mode == "fea_resolve":
            solved = _resolve_candidate(cb, ctx, conv_base, fixtures, material,
                                        rec, r_total, a_top_mm2)
            if not solved.get("ok"):
                row["solve"] = f"solve failed: {solved.get('error')}"
                rows.append(row)
                continue
            row["achieved_t_case_c"] = solved["t_case_c"]
            row["achieved_tj_c"] = solved["tj_c"]
            row["board_max_temp_c"] = solved["board_max_temp_c"]
            row["solve"] = "fea"
            row["_study"] = solved
        else:
            jc = theta_jc if isinstance(theta_jc, (int, float)) else 0.0
            t_case = t_amb + p_w * r_total
            row["achieved_t_case_c"] = round(t_case, 2)
            row["achieved_tj_c"] = round(t_case + p_w * jc, 2)
            row["solve"] = "network"
        achieved = (row["achieved_tj_c"] if target_kind == "tj"
                    else row["achieved_t_case_c"])
        if achieved is None and target_kind == "tj":
            achieved = row["achieved_t_case_c"]
            row["basis"] = "case (theta_jc unknown)"
        row["achieved_c"] = achieved
        row["delta_vs_target_c"] = (round(achieved - target_c, 2)
                                    if achieved is not None else None)
        row["meets_target"] = (achieved is not None and achieved <= target_c)
        rows.append(row)

    solved_rows = [r for r in rows if r.get("achieved_c") is not None]
    if not solved_rows:
        return {"ok": False, "error": "every candidate solve failed",
                "warnings": warnings,
                "hint": "inspect the per-candidate 'solve' errors in table.",
                "table": [{k: v for k, v in r.items() if k != "_study"}
                          for r in rows],
                "next_actions": []}
    meeting = [r for r in solved_rows if r["meets_target"]]
    best = min(solved_rows, key=lambda r: r["achieved_c"])
    if meeting:
        # Winner = lightest that meets target (cost tiebreak when known).
        winner = min(meeting, key=lambda r: (r.get("mass_g") or 1e9,
                                             r.get("cost_usd") or 1e9))
        target_met = True
    else:
        winner = best
        target_met = False
        warnings.append(
            f"IMPOSSIBLE TARGET in this environment: no candidate reaches "
            f"{target_kind} <= {target_c} C; best achievable is "
            f"{best['achieved_c']} C with {best['candidate']!r}. Consider "
            f"forced air / cold plate, lower ambient, or spreading the load.")
    winner_solve = winner.pop("_study", None)
    for r in rows:
        r.pop("_study", None)
    rows.sort(key=lambda r: (r.get("achieved_c") is None,
                             r.get("achieved_c") if r.get("achieved_c") is not None
                             else 1e9))

    if winner_solve is not None:
        _HEATSINK["winner_study"] = {"component_ref": component_ref,
                                     "candidate": winner["candidate"],
                                     "study": winner_solve}

    # ── simhandoff verify envelope: target vs achieved on the winner ────
    achieved = winner["achieved_c"]
    dev_pct = abs(achieved - target_c) / max(abs(target_c - t_amb), 1e-9) * 100.0
    verdict = (f"{'PASS' if target_met else 'FAIL'}: "
               f"{winner['candidate']!r} achieves {target_kind} {achieved} C vs "
               f"target {target_c} C ({'meets' if target_met else 'misses by '}"
               f"{'' if target_met else str(round(achieved - target_c, 2)) + ' C'})"
               f" in {env.get('name')} [{mode}]")
    envelope = sh.build_verify(
        "ecad_heatsink_to_thermal_fea",
        sh.study_ref("ecad", "heatsink_select", doc_id=ctx.get("doc_id"),
                     handoff_id=ctx.get("handoff_envelope_id")),
        sh.study_ref("thermal_fea", "sim_run_thermal" if mode == "fea_resolve"
                     else "network_estimate"),
        [sh.check(f"{target_kind}_{component_ref}_c", "C",
                  target_c, achieved, round(dev_pct, 1),
                  0.0 if not target_met else round(dev_pct, 1) + 1.0,
                  target_met)],
        verdict,
        advisories=warnings or None,
        warnings=[f"mount model: h_eff over the component top "
                  f"(single-material solver); TIM {tim_t_mm} mm @ "
                  f"{tim_k_w_mk} W/m-K; sink-base overhang spreading not "
                  "modeled (conservative)"])
    errs = sh.validate_envelope(envelope)
    if errs:  # never ship a malformed envelope silently
        warnings.append(f"simhandoff envelope validation: {errs}")

    result = sh.attach({
        "ok": True,
        "component_ref": component_ref,
        "power_w": p_w,
        "target": {"kind": target_kind, "value_c": target_c},
        "environment": env,
        "mode": mode,
        "mesh_cached": bool(mesh is not None),
        "tim": {"t_mm": tim_t_mm, "k_w_mk": tim_k_w_mk,
                "note": "bonded contact through a TIM bondline; "
                        "R_tim = t/(k*A_contact)"},
        "target_met": target_met,
        "winner": {k: v for k, v in winner.items()},
        "best_achievable": {"candidate": best["candidate"],
                            "achieved_c": best["achieved_c"]},
        "table": rows,
        "assumptions": [
            "mounted sink modeled as effective convection h_eff = "
            "1/((R_tim+R_sa)*A_top) over the component top face (core solver is "
            "single-material) — board spreading + neighbor heating stay in the "
            "solve" if mode == "fea_resolve" else
            "network estimate: series resistances only, board coupling ignored",
            "sink-base overhang spreading beyond the component footprint not "
            "modeled (conservative)",
            "parametric theta_sa from the plain-fin efficiency estimate; library "
            "theta_sa from curated datasheet-class nominals — verify the chosen "
            "part's datasheet before release",
        ],
        "warnings": warnings,
        "hint": verdict,
        "next_actions": ([f"part_source('{winner.get('candidate')}')  # source it"
                          if winner["source"] == "library" else
                          "services.mcp.heatsink.sink_ops(winner spec) via "
                          "add_features to model the chosen parametric sink"]
                         if target_met else
                         ["ecad_thermal_handoff(environment='forced_air') then "
                          "heatsink_select(...) again",
                          "lower the target or reduce component power"]),
    }, envelope)
    return result


def heatsink_select_from_params(params: dict) -> dict:
    """Worker params entry; optional doc_id/handoff_id pin the source thermal study."""
    p = params or {}
    return heatsink_select_impl(
        component_ref=str(p.get("component_ref") or ""),
        tj_target_c=p.get("tj_target_c"),
        t_case_target_c=p.get("t_case_target_c"),
        tim_t_mm=(p.get("tim_t_mm") if p.get("tim_t_mm") is not None
                  else TIM_T_MM_DEFAULT),
        tim_k_w_mk=(p.get("tim_k_w_mk") if p.get("tim_k_w_mk") is not None
                    else TIM_K_W_MK_DEFAULT),
        include_library=bool(p.get("include_library", True)),
        include_parametric=bool(p.get("include_parametric", True)),
        max_candidates=p.get("max_candidates", 8),
        doc_id=p.get("doc_id"),
        handoff_id=p.get("handoff_id"),
    )


# --------------------------------------------------------------------------- registration


def register_heatsink_tools(mcp) -> None:
    """Register heatsink_select on ``mcp`` (FastMCP)."""

    @mcp.tool()
    def heatsink_select(component_ref: str,
                        tj_target_c: Optional[float] = None,
                        t_case_target_c: Optional[float] = None,
                        tim_t_mm: float = TIM_T_MM_DEFAULT,
                        tim_k_w_mk: float = TIM_K_W_MK_DEFAULT,
                        include_library: bool = True,
                        include_parametric: bool = True,
                        max_candidates: int = 8) -> str:
        """Pick a heatsink for a hot component: candidates re-solved to a Tj target.

        Run ecad_thermal_handoff() FIRST (its geometry/loads/mesh are the cached
        context this tool re-solves). Then heatsink_select("U1", tj_target_c=85):

        1. CANDIDATES — curated parts-library heatsinks (part_search domain
           "thermal", entries carry datasheet-class theta_sa + mass + cost) plus a
           parametric plain-finned aluminum generator sized to the component
           footprint (theta_sa from the standard fin-efficiency estimate; geometry
           buildable via services.mcp.heatsink.sink_ops engine ops).
        2. MOUNT — bonded contact through a TIM bondline (default 0.1 mm of
           3.0 W/m-K grease-class TIM; override tim_t_mm/tim_k_w_mk). Because the
           core solver is single-material, the mounted sink becomes an EFFECTIVE
           convection h_eff = 1/((R_tim + R_sa) * A_top) over the component top —
           board spreading + neighbor heating stay fully in the FEA re-solve.
        3. RE-SOLVE — one thermal solve per candidate, sequentially, REUSING the
           cached board mesh (geometry unchanged). Without a sim-enabled libchudware
           the tool degrades honestly to mode="network_estimate"
           (Tj = Ta + P*(theta_jc + R_tim + R_sa); board coupling ignored, flagged).
        4. RANKED TABLE — {candidate, achieved_tj_c, delta_vs_target_c, mass_g,
           cost_usd}; winner = lightest candidate meeting the target; its study is
           kept and the result carries an aicad.simhandoff/1 VERIFY envelope.
           An impossible target is reported honestly: target_met=false with the
           best achievable candidate named — never a fake win.

        Target: tj_target_c (junction, needs theta_jc — falls back to case with a
        warning when jc is unknown) or t_case_target_c (case temperature).
        """
        result = heatsink_select_impl(
            component_ref=component_ref, tj_target_c=tj_target_c,
            t_case_target_c=t_case_target_c, tim_t_mm=tim_t_mm,
            tim_k_w_mk=tim_k_w_mk, include_library=include_library,
            include_parametric=include_parametric,
            max_candidates=max_candidates)
        return _dumps(result)



# --------------------------------------------------------------------------- solve leg


def _base_convection(ctx: dict) -> tuple:
    """Environment convection loads over the board top (component footprints
    excluded), board bottom (unless cold-plate fixture), and the OTHER components'
    tops — the target component's top is replaced per candidate. Returns
    (fn(target_ref, h_eff) -> conv_loads, fixtures)."""
    env = ctx["env"]
    board = ctx["board"]
    components = ctx["components"]
    fixtures = list(ctx.get("fixtures") or [])

    def build(target_ref: str, h_eff: float) -> list:
        comp_rects_top = [c["fea"]["rect_mm"] for c in components
                          if c.get("fea") and c["fea"].get("side", "top") != "bottom"]
        env_pts = et._rect_grid(board["bbox_mm"], board["z_top_mm"],
                                exclude=comp_rects_top)
        target_pts: list = []
        for c in components:
            fea = c.get("fea")
            if not fea or fea.get("side", "top") == "bottom":
                continue
            pts = et._rect_grid(fea["rect_mm"], fea["z_top_mm"], max_step=1.0)
            if (c.get("ref") or c.get("id")) == target_ref:
                target_pts = pts
            else:
                env_pts += pts
        loads = [{"faces": et._faceset(env_pts, [0.0, 0.0, 1.0]),
                  "type": "convection", "h": env["h_w_m2k"],
                  "t_amb_c": env["t_amb_c"]}]
        if target_pts:
            loads.append({"faces": et._faceset(target_pts, [0.0, 0.0, 1.0]),
                          "type": "convection", "h": h_eff,
                          "t_amb_c": env["t_amb_c"]})
        if not env.get("cold_plate"):
            loads.append({"faces": et._faceset(
                et._rect_grid(board["bbox_mm"], board["z_bot_mm"]),
                [0.0, 0.0, -1.0]),
                "type": "convection", "h": env["h_w_m2k"],
                "t_amb_c": env["t_amb_c"]})
        return loads

    return build, fixtures


def _resolve_candidate(cb, ctx: dict, conv_builder, fixtures: list, material: dict,
                       rec: dict, r_total_c_per_w: float, a_top_mm2: float) -> dict:
    """One candidate re-solve on the CACHED mesh: swap the target component's top
    convection for h_eff = 1/(R_total * A_top). Returns {ok, t_case_c, tj_c,
    board_max_temp_c, summary} or {ok: False, error}."""
    ref = rec.get("ref") or rec.get("id")
    h_eff = 1.0 / (max(r_total_c_per_w, 1e-9) * a_top_mm2 * 1e-6)
    mesh = ctx["mesh"]
    study = {"points": mesh["points"], "tets": mesh["tets"],
             "nodes_per_elem": mesh.get("nodes_per_elem", 4),
             "material": {"name": material["name"], "k_w_mk": material["k_w_mk"],
                          "cp_j_kgk": material["cp_j_kgk"],
                          "rho_kg_m3": material["rho_kg_m3"]},
             "fixtures": fixtures,
             "loads": [{k: v for k, v in ld.items() if k != "_ref"}
                       for ld in ctx["flux_loads"]] + conv_builder(ref, h_eff),
             "transient": ctx["transient"]}
    try:
        res = cb.call("sim_run_thermal", {"study": study})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "solve failed")}
    try:
        rows = et.map_component_temps([rec], mesh["points"],
                                      res.get("temperature") or [],
                                      res.get("envelope_max"))
    except (TypeError, ValueError) as exc:
        return {"ok": False, "error": f"invalid thermal result: {exc}"}
    row = rows[0] if rows else {}
    t_case = row.get("t_case_c")
    tj = row.get("tj_c")
    if not row.get("mapped") or not isinstance(t_case, (int, float)) or not math.isfinite(t_case):
        return {"ok": False,
                "error": row.get("map_status") or "thermal result did not map to the candidate"}
    if tj is not None and (not isinstance(tj, (int, float)) or not math.isfinite(tj)):
        return {"ok": False, "error": "thermal result produced a non-finite junction temperature"}
    summary = res.get("summary")
    if not isinstance(summary, dict):
        return {"ok": False, "error": "thermal result is missing its summary"}
    board_max = summary.get("final_max_temp_c", summary.get("max_temp_c"))
    if not isinstance(board_max, (int, float)) or not math.isfinite(board_max):
        return {"ok": False, "error": "thermal result is missing a finite board maximum"}
    return {"ok": True,
            "h_eff_w_m2k": round(h_eff, 1),
            "t_case_c": t_case,
            "tj_c": tj,
            "board_max_temp_c": board_max,
            "summary": summary}
