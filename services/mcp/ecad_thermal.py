"""ECAD -> thermal FEA handoff (T-FIN16) — one call from a routed board to a solved
board-level thermal study, conforming to ``aicad.simhandoff/1``.

Same design contract as ``ecad_tools`` / ``piping_tools``: JSON-string results with
``{ok, ..., warnings, hint, next_actions}``, engine helpers called directly, module-level
session state so the verify tool can run with no arguments.

Tools:
  ecad_thermal_handoff   board -> fused FEA solid (engine.ecad.threed.board_to_fea_step)
                         with layer-homogenized FR4+Cu material -> per-component heat
                         sources from boardphys power metadata (+ optional PDN I2R trace
                         heating) -> environment-preset convection BCs -> core thermal
                         solve (sim_run_thermal) -> per-component case-temp / Tj map-back.
  ecad_verify_thermal    per-component Tj vs limits table with margins + a theta_ja
                         hand-calc cross-check at a GENEROUS tolerance (JEDEC theta_ja
                         assumes a standard 2s2p test board, not this layout).

THE SOLVE PATH (documented honesty): the core steady kernel aggregates all flux loads
into ONE magnitude per study (core/sim/fea_run.cpp), so distinct per-component powers
require the TRANSIENT path (T-FIN18), which keeps per-load magnitudes + schedules. We
therefore run a transient integration to QUASI-STEADY state (t_end = 5 thermal time
constants of the lumped board) and read the final frame as the steady answer, unless
the caller passes an explicit ``transient`` spec (duty-cycle studies).

CONFORMANCE: both tools embed a validated ``aicad.simhandoff/1`` envelope under the
reserved ``simhandoff`` key (contracts/sim_handoff.md, coupling ``ecad_to_thermal_fea``:
one ``heat_source`` condition PER component, ``applied_to`` = the component ref).
"""

from __future__ import annotations

import json
import math
import os
from typing import Any, Optional, Union

from services.mcp import simhandoff as sh

# Environment presets: film coefficient (W/m^2-K) + default ambient (C).
# still_air ~ natural convection off a horizontal board; forced_air ~ moderate fan flow
# (2-3 m/s); cold_plate holds the board BOTTOM at the plate temperature (Dirichlet) with
# still air on top. Engineering nominals — override with h_w_m2k / t_amb_c for a custom
# environment.
ENVIRONMENTS: dict = {
    "still_air":  {"h_w_m2k": 10.0, "t_amb_c": 25.0, "cold_plate": False},
    "forced_air": {"h_w_m2k": 50.0, "t_amb_c": 25.0, "cold_plate": False},
    "cold_plate": {"h_w_m2k": 10.0, "t_amb_c": 25.0, "cold_plate": True},
}

# Max distinct PDN I2R cell flux loads passed to the solver; the hottest cells win and
# the omitted remainder is reported (never silently dropped).
_MAX_I2R_LOADS = 48
# Face-sample grid spacing / matching tolerances (mm). Every surface mesh node must be
# within tol of SOME sample point, so tol >= step/sqrt(2) with margin.
_GRID_MAX_STEP = 1.0
_FACE_TOL_MM = 0.8
_PERP_TOL_MM = 0.3

_THERMAL: dict = {"last_handoff": None}   # session state for ecad_verify_thermal


def _dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2)


def _err(reason: str, hint: str = "", next_actions: Optional[list] = None,
         **extra: Any) -> str:
    return _dumps(_err_d(reason, hint, next_actions, **extra))


def _err_d(reason: str, hint: str = "", next_actions: Optional[list] = None,
           **extra: Any) -> dict:
    """Dict-shaped error envelope (thermal_handoff_impl returns dicts; the MCP tool
    and the GUI worker endpoint each serialize/relay as their transport needs)."""
    out: dict = {"ok": False, "error": reason, "warnings": [],
                 "hint": hint or reason, "next_actions": next_actions or []}
    out.update(extra)
    return out


# --------------------------------------------------------------------------- pure helpers
# Module-level and engine-free so tests can drive them without OCP/libaicad.


def resolve_environment(environment: str, h_w_m2k: Optional[float],
                        t_amb_c: Optional[float]) -> dict:
    """Preset name (or 'custom' with explicit h/t_amb) -> {name, h_w_m2k, t_amb_c,
    cold_plate}. Raises ValueError for an unknown preset without explicit h."""
    env = ENVIRONMENTS.get(environment)
    if env is None:
        if h_w_m2k is None:
            raise ValueError(
                f"unknown environment {environment!r} — use one of "
                f"{sorted(ENVIRONMENTS)} or pass h_w_m2k (+ t_amb_c) for a custom one")
        env = {"h_w_m2k": float(h_w_m2k), "t_amb_c": 25.0, "cold_plate": False}
    out = dict(env)
    if h_w_m2k is not None:
        out["h_w_m2k"] = float(h_w_m2k)
    if t_amb_c is not None:
        out["t_amb_c"] = float(t_amb_c)
    out["name"] = environment
    return out


def _rect_grid(rect: list, z: float, max_step: float = _GRID_MAX_STEP,
               exclude: Optional[list] = None) -> list:
    """Sample-point grid [[x,y,z],...] over rect [x0,y0,x1,y1] at height z.

    ``exclude`` is a list of rects whose interior points are dropped (used to keep
    board-top convection off the covered footprint areas — after fusion those regions
    are INTERNAL, not wetted surface)."""
    x0, y0, x1, y1 = [float(v) for v in rect]
    nx = max(2, int(math.ceil((x1 - x0) / max_step)) + 1)
    ny = max(2, int(math.ceil((y1 - y0) / max_step)) + 1)
    pts = []
    for i in range(nx):
        x = x0 + (x1 - x0) * i / (nx - 1)
        for j in range(ny):
            y = y0 + (y1 - y0) * j / (ny - 1)
            if exclude and any(r[0] <= x <= r[2] and r[1] <= y <= r[3] for r in exclude):
                continue
            pts.append([round(x, 4), round(y, 4), round(z, 4)])
    return pts


def _faceset(points: list, normal: list) -> dict:
    return {"points": points, "tol_mm": _FACE_TOL_MM,
            "face_normal": normal, "perp_tol_mm": _PERP_TOL_MM}


def component_power_w(rec: dict) -> Optional[float]:
    """Operating power for a boardphys component record: typ preferred, else max.
    None (honest) when the metadata has neither — the component gets NO heat source."""
    p = rec.get("power_w") or {}
    for key in ("typ", "max"):
        v = p.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def build_heat_loads(components: list, schedules: Optional[dict] = None) -> tuple:
    """Per-component flux loads + envelope conditions from boardphys records.

    Returns ``(loads, conditions, sources, unpowered_refs)`` where each load is a
    sim_run_thermal flux spec over the component's top face (P / top_area), each
    condition is one simhandoff ``heat_source`` per powered component (applied_to =
    the component ref — the contract's pre-listed ``ecad_to_thermal_fea`` shape), and
    ``unpowered_refs`` lists components that got NO heat source (coverage honesty)."""
    loads: list = []
    conditions: list = []
    sources: list = []
    unpowered: list = []
    for rec in components:
        ref = rec.get("ref") or rec.get("id") or "?"
        fea = rec.get("fea")
        if not fea:
            continue  # body never placed — already in summary warnings
        p_w = component_power_w(rec)
        if p_w is None:
            unpowered.append(ref)
            continue
        area_mm2 = float(fea["top_area_mm2"])
        if area_mm2 <= 0:
            unpowered.append(ref)
            continue
        flux_w_m2 = p_w / (area_mm2 * 1e-6)
        top = fea["z_top_mm"]
        normal = [0.0, 0.0, 1.0] if fea.get("side", "top") != "bottom" else [0.0, 0.0, -1.0]
        spec = {"faces": _faceset(_rect_grid(fea["rect_mm"], top, max_step=1.0), normal),
                "type": "flux", "magnitude": flux_w_m2, "_ref": ref}
        sched = (schedules or {}).get(ref)
        if isinstance(sched, dict):
            spec["schedule"] = sched
        loads.append(spec)
        which = "typ" if (rec.get("power_w") or {}).get("typ") else "max"
        conditions.append(sh.condition(
            f"heat_{ref}", "heat_source", p_w, "W",
            f"boardphys.components[{ref}].power_w.{which}",
            applied_to=ref))
        sources.append({"ref": ref, "power_w": p_w,
                        "flux_w_m2": round(flux_w_m2, 1),
                        "top_area_mm2": area_mm2,
                        "scheduled": bool(sched)})
    return loads, conditions, sources, unpowered


def build_i2r_loads(pdn_block: dict, board: dict) -> tuple:
    """PDN I2R map -> distributed flux loads (top-K cells) + one summary condition.

    Returns ``(loads, condition, info)``. Cells beyond ``_MAX_I2R_LOADS`` are dropped
    hottest-first and their total reported in ``info['omitted_w']`` — visible, never
    silent."""
    cells: list = []
    total_w = 0.0
    for net in (pdn_block or {}).get("nets", []) or []:
        m = net.get("i2r_map") or {}
        cols = m.get("columns") or []
        try:
            i_layer = cols.index("layer"); i_x = cols.index("x_mm")
            i_y = cols.index("y_mm"); i_p = cols.index("p_w")
        except ValueError:
            continue
        size = float(m.get("cell_size_mm", 1.0) or 1.0)
        total_w += float(m.get("total_w", 0.0) or 0.0)
        for c in m.get("cells", []) or []:
            p = float(c[i_p])
            if p > 0:
                cells.append({"layer": c[i_layer], "x": float(c[i_x]),
                              "y": float(c[i_y]), "p_w": p, "size": size})
        for via in m.get("vias", []) or []:
            p = float(via.get("p_w", 0.0) or 0.0)
            at = via.get("at") or [0.0, 0.0]
            if p > 0:
                cells.append({"layer": "F.Cu", "x": float(at[0]), "y": float(at[1]),
                              "p_w": p, "size": size})
    if not cells:
        return [], None, {"included": False, "total_w": 0.0, "n_cell_loads": 0,
                          "omitted_w": 0.0,
                          "note": "no PDN I2R cells found — run ecad_run_pdn first"}
    cells.sort(key=lambda c: c["p_w"], reverse=True)
    kept, dropped = cells[:_MAX_I2R_LOADS], cells[_MAX_I2R_LOADS:]
    z_top = float(board["z_top_mm"])
    z_bot = float(board["z_bot_mm"])
    loads = []
    for c in kept:
        on_top = not str(c["layer"]).startswith("B.")
        z = z_top if on_top else z_bot
        normal = [0.0, 0.0, 1.0] if on_top else [0.0, 0.0, -1.0]
        half = c["size"] / 2.0
        rect = [c["x"] - half, c["y"] - half, c["x"] + half, c["y"] + half]
        flux = c["p_w"] / (c["size"] * c["size"] * 1e-6)
        loads.append({"faces": _faceset(_rect_grid(rect, z, max_step=max(c["size"] / 2.0, 0.5)),
                                        normal),
                      "type": "flux", "magnitude": flux, "_ref": "i2r"})
    omitted = sum(c["p_w"] for c in dropped)
    cond = sh.condition(
        "trace_heating_i2r", "distributed_flux",
        {"total_w": round(total_w, 4), "n_cell_loads": len(kept),
         "omitted_w": round(omitted, 4)},
        "W", "boardphys.pdn.nets[*].i2r_map (ecad_run_pdn)")
    return loads, cond, {"included": True, "total_w": round(total_w, 4),
                         "n_cell_loads": len(kept), "omitted_w": round(omitted, 4)}


def quasi_steady_transient(board: dict, material: dict, env: dict) -> dict:
    """Transient spec that integrates to ~steady state: t_end = 5 tau of the lumped
    board (rho cp V / h A), bounded to [60 s, 2 h]; ~120 implicit steps."""
    x0, y0, x1, y1 = board["bbox_mm"]
    area_m2 = (x1 - x0) * (y1 - y0) * 1e-6
    vol_m3 = area_m2 * float(board["thickness_mm"]) * 1e-3
    h = max(float(env["h_w_m2k"]), 1e-3)
    tau = (float(material["rho_kg_m3"]) * float(material["cp_j_kgk"]) * vol_m3
           / (h * 2.0 * max(area_m2, 1e-9)))
    t_end = min(max(5.0 * tau, 60.0), 7200.0)
    return {"t_end_s": round(t_end, 1), "dt_s": round(t_end / 120.0, 3),
            "t_init_c": float(env["t_amb_c"]), "capacitance": "lumped",
            "_quasi_steady": True}


def map_component_temps(components: list, points: list, temperature: list,
                        envelope_max: Optional[list] = None) -> list:
    """Per-component case temp from the solved nodal field: max T over the nodes inside
    the component's world rect + z span (small halo). Tj = Tcase + P x theta_jc when jc
    is known; honest null otherwise."""
    out = []
    n = len(temperature)
    for rec in components:
        fea = rec.get("fea")
        ref = rec.get("ref") or rec.get("id") or "?"
        row: dict = {"ref": ref, "power_w": component_power_w(rec),
                     "t_case_c": None, "t_case_peak_c": None, "tj_c": None,
                     "theta_jc": ((rec.get("theta_c_per_w") or {}).get("jc")),
                     "theta_ja": ((rec.get("theta_c_per_w") or {}).get("ja")),
                     "tj_max_c": ((rec.get("limits_c") or {}).get("tj_max")),
                     "n_nodes": 0}
        if fea and n:
            x0, y0, x1, y1 = [float(v) for v in fea["rect_mm"]]
            zlo = min(fea["z_bot_mm"], fea["z_top_mm"]) - 0.3
            zhi = max(fea["z_bot_mm"], fea["z_top_mm"]) + 0.3
            halo = 0.3
            t_max = None
            pk_max = None
            cnt = 0
            for i in range(n):
                x, y, z = points[3 * i], points[3 * i + 1], points[3 * i + 2]
                if (x0 - halo <= x <= x1 + halo and y0 - halo <= y <= y1 + halo
                        and zlo <= z <= zhi):
                    cnt += 1
                    t = temperature[i]
                    t_max = t if t_max is None else max(t_max, t)
                    if envelope_max is not None:
                        pk = envelope_max[i]
                        pk_max = pk if pk_max is None else max(pk_max, pk)
            row["n_nodes"] = cnt
            if t_max is not None:
                row["t_case_c"] = round(t_max, 2)
                row["t_case_peak_c"] = round(pk_max, 2) if pk_max is not None else round(t_max, 2)
                jc = row["theta_jc"]
                p = row["power_w"]
                if isinstance(jc, (int, float)) and isinstance(p, (int, float)):
                    row["tj_c"] = round(t_max + p * jc, 2)
        out.append(row)
    return out


def build_component_table(rows: list, t_amb_c: float) -> list:
    """Verify-side per-component table: Tj (or Tcase fallback) vs tj_max -> margin/pass.
    Components without limits are listed as unchecked (honesty), never assumed OK."""
    table = []
    for r in rows:
        tj = r.get("tj_c")
        basis = "tj (case + P*theta_jc)"
        if tj is None:
            tj = r.get("t_case_c")
            basis = "t_case (theta_jc unknown — Tj >= this)"
        lim = r.get("tj_max_c")
        entry = {"ref": r["ref"], "power_w": r.get("power_w"),
                 "t_case_c": r.get("t_case_c"), "tj_c": r.get("tj_c"),
                 "tj_used_c": tj, "basis": basis, "tj_max_c": lim,
                 "margin_c": None, "pass": None}
        if isinstance(tj, (int, float)) and isinstance(lim, (int, float)):
            entry["margin_c"] = round(lim - tj, 2)
            entry["pass"] = entry["margin_c"] > 0
        elif tj is None:
            entry["basis"] = "not solved (no nodes matched or dry run)"
        elif lim is None:
            entry["basis"] += "; tj_max unknown -> unchecked"
        table.append(entry)
    return table


# A theta_ja hand calc predicts SELF-heating on a JEDEC standard board; a component
# contributing a small share of the board's total power sits mostly in its NEIGHBORS'
# heat, which theta_ja cannot see — those get an advisory instead of a pass/fail check.
_JA_CHECK_MIN_POWER_SHARE = 0.25


def theta_ja_checks(rows: list, t_amb_c: float, tolerance_pct: float) -> tuple:
    """Hand-calc cross-check: Tj_est = Ta + P x theta_ja vs the solved Tj (or Tcase).

    Deviation is measured on the temperature RISE above ambient (comparing absolute
    temperatures would launder any error through the shared ambient). The tolerance is
    deliberately GENEROUS: JEDEC theta_ja is measured on a standard 2s2p test board in
    still air — a different copper area, board size and airflow than this layout, so
    50-100 % rise disagreement is normal. This check catches order-of-magnitude setup
    errors (wrong units, wrong area, missing source), not layout-grade accuracy.

    Returns ``(checks, advisories)``: components below ``_JA_CHECK_MIN_POWER_SHARE`` of
    the total powered watts are excluded from pass/fail (their solved temperature is
    dominated by neighbor heating, which theta_ja can never predict) and reported as
    advisories instead."""
    checks = []
    advisories = []
    total_p = sum(r["power_w"] for r in rows
                  if isinstance(r.get("power_w"), (int, float)))
    for r in rows:
        ja = r.get("theta_ja")
        p = r.get("power_w")
        solved = r.get("tj_c") if r.get("tj_c") is not None else r.get("t_case_c")
        if not (isinstance(ja, (int, float)) and isinstance(p, (int, float))
                and isinstance(solved, (int, float))):
            continue
        rise_hand = p * ja
        rise_solved = solved - t_amb_c
        dev = abs(rise_solved - rise_hand) / max(abs(rise_hand), 1e-9) * 100.0
        if total_p > 0 and p / total_p < _JA_CHECK_MIN_POWER_SHARE:
            advisories.append(
                f"{r['ref']}: theta_ja hand calc not checked ({p} W is "
                f"{p / total_p:.0%} of board power — its temperature is dominated by "
                f"neighbor heating; solved rise {rise_solved:.1f} C vs self-heating "
                f"prediction {rise_hand:.1f} C)")
            continue
        checks.append(sh.check(f"tj_rise_{r['ref']}", "C",
                               round(rise_hand, 2), round(rise_solved, 2),
                               round(dev, 1), tolerance_pct, dev <= tolerance_pct))
    return checks, advisories


# --------------------------------------------------------------------------- registration


# --------------------------------------------------------------------------- shared impl
# Session-free implementation of the ECAD -> thermal FEA handoff (T-FIN24). The MCP tool
# below wraps it with the server's session lookup / output-dir conventions; the desktop
# GUI's worker endpoint (engine_oracle/sim/multiphysics_rpc.py) wraps it with an
# explicitly-passed board document. ONE pipeline, two transports — the fw_* pattern.
def thermal_handoff_impl(board_doc: dict, *,
                         doc_id: str = "",
                         environment: str = "still_air",
                         h_w_m2k: Optional[float] = None,
                         t_amb_c: Optional[float] = None,
                         include_trace_heating: bool = False,
                         transient: Optional[dict] = None,
                         mesh_fineness: float = 0.35,
                         dry_run: bool = False,
                         out_path: str = "",
                         find_pdn=None) -> dict:
    """Board doc (aicad ECAD board JSON) -> solved thermal FEA study dict.

    Returns the same payload shape as the ecad_thermal_handoff MCP tool (dict, not
    JSON string), with a validated aicad.simhandoff/1 envelope under "simhandoff".
    ``find_pdn`` is an optional callable ``(board_name, step_path) -> Optional[dict]``
    supplying the boardphys PDN block for include_trace_heating (the MCP wrapper
    injects its sidecar scan; callers without one degrade with a warning).
    """
    warnings: list = []
    if not isinstance(board_doc, dict) or not board_doc:
        return _err_d("no board document supplied",
                      hint="thermal handoff needs a routed board's document JSON.",
                      next_actions=["ecad_new_pcb('myboard')", "ecad_make_board(...)"])

    try:
        env = resolve_environment(environment, h_w_m2k, t_amb_c)
    except ValueError as e:
        return _err_d(str(e))

    if not out_path:
        import tempfile
        out_path = os.path.join(tempfile.gettempdir(),
                                f"aicad_board_fea_{os.getpid()}.step")

    # ── 1. FEA solid + homogenized material ─────────────────────────────
    try:
        from engine.ecad.threed import board_to_fea_step, BoardGeometryError
    except ImportError as e:
        return _err_d(f"3D kernel unavailable (ImportError: {e})",
                      hint="OCP/build123d not installed — the FEA solid cannot be built.",
                      next_actions=["pip install build123d"], available=False)
    try:
        built = board_to_fea_step(board_doc, out_path)
    except BoardGeometryError as e:
        return _err_d(f"board geometry failed: {e}",
                      hint="the board needs a closed Edge.Cuts outline.",
                      next_actions=["ecad_run_drc()", "ecad_status()"])
    except Exception as e:  # noqa: BLE001
        return _err_d(f"FEA solid build failed: {type(e).__name__}: {e}")

    material = built["material"]
    board_geo = built["board"]
    components = built["components"]
    warnings.extend(built["summary"].get("warnings", []))

    # ── 2. heat sources from power metadata ─────────────────────────────
    schedules = (transient or {}).get("schedules") if isinstance(transient, dict) else None
    loads, conditions, sources, unpowered = build_heat_loads(components, schedules)
    coverage = {"n_components": len(components),
                "n_heat_sources": len(sources),
                "unpowered_refs": unpowered,
                "note": ("all components carry power metadata" if not unpowered else
                         f"{len(unpowered)} component(s) have NO power metadata and "
                         f"got NO heat source: {', '.join(unpowered)} — seed power_w "
                         "via library eng blocks or sim_params.eng overrides")}
    if not sources:
        return _err_d("no component carries power metadata — nothing to heat",
                      hint="seed power_w on at least one component "
                           "(contracts/ecad/boardphys.md eng blocks).",
                      next_actions=["ecad_status()"], coverage=coverage)

    # ── 3. optional PDN I2R trace heating ───────────────────────────────
    trace_info = {"included": False}
    if include_trace_heating:
        pdn = find_pdn(board_doc.get("name", "board"), out_path) if find_pdn else None
        if pdn is None:
            warnings.append("include_trace_heating=True but no PDN I2R map found — "
                            "run ecad_run_pdn first; continuing without trace heating.")
        else:
            i2r_loads, i2r_cond, trace_info = build_i2r_loads(pdn, board_geo)
            loads.extend(i2r_loads)
            if i2r_cond:
                conditions.append(i2r_cond)
            if trace_info.get("omitted_w"):
                warnings.append(
                    f"I2R map truncated to the {trace_info['n_cell_loads']} hottest "
                    f"cells; {trace_info['omitted_w']} W omitted.")

    # ── 4. convection BCs ───────────────────────────────────────────────
    comp_rects_top = [c["fea"]["rect_mm"] for c in components
                      if c.get("fea") and c["fea"].get("side", "top") != "bottom"]
    top_pts = _rect_grid(board_geo["bbox_mm"], board_geo["z_top_mm"],
                         exclude=comp_rects_top)
    for c in components:
        fea = c.get("fea")
        if fea and fea.get("side", "top") != "bottom":
            top_pts += _rect_grid(fea["rect_mm"], fea["z_top_mm"], max_step=1.0)
    fixtures: list = []
    conv_loads: list = [
        {"faces": _faceset(top_pts, [0.0, 0.0, 1.0]), "type": "convection",
         "h": env["h_w_m2k"], "t_amb_c": env["t_amb_c"]}]
    if env.get("cold_plate"):
        fixtures.append({**_faceset(_rect_grid(board_geo["bbox_mm"],
                                               board_geo["z_bot_mm"]),
                                    [0.0, 0.0, -1.0]),
                         "temp_c": env["t_amb_c"]})
        conditions.append(sh.condition(
            "cold_plate_bottom", "fixed_temperature", env["t_amb_c"], "C",
            "environment preset 'cold_plate'", applied_to="board bottom face"))
    else:
        conv_loads.append(
            {"faces": _faceset(_rect_grid(board_geo["bbox_mm"], board_geo["z_bot_mm"]),
                               [0.0, 0.0, -1.0]),
             "type": "convection", "h": env["h_w_m2k"], "t_amb_c": env["t_amb_c"]})
    conditions.append(sh.condition(
        "convection_env", "convection",
        {"h_w_m2k": env["h_w_m2k"], "t_amb_c": env["t_amb_c"]},
        "W/m2-K", f"environment preset {env['name']!r}",
        applied_to="board top/bottom + component top faces"))
    warnings.append("component SIDE faces carry no convection (conservative — "
                    "solved temps run slightly hot)")

    # ── transient spec ──────────────────────────────────────────────────
    if isinstance(transient, dict) and transient.get("t_end_s"):
        tr = {"t_end_s": float(transient["t_end_s"]),
              "dt_s": float(transient.get("dt_s") or 0.0),
              "t_init_c": float(transient.get("t_init_c", env["t_amb_c"])),
              "capacitance": transient.get("capacitance", "lumped")}
        quasi_steady = False
    else:
        tr = quasi_steady_transient(board_geo, material, env)
        tr.pop("_quasi_steady", None)
        quasi_steady = True

    # ── expectations (theta_ja hand-calc, when known) ───────────────────
    expectations = []
    for rec in components:
        ja = (rec.get("theta_c_per_w") or {}).get("ja")
        p = component_power_w(rec)
        if isinstance(ja, (int, float)) and isinstance(p, (int, float)):
            expectations.append(sh.expectation(
                f"tj_{rec.get('ref')}_c", round(env["t_amb_c"] + p * ja, 2), "C",
                f"hand calc Ta + P*theta_ja (JEDEC theta_ja = {ja} C/W; layout-"
                "dependent seed — generous tolerance applies)"))

    envelope = sh.build_handoff(
        "ecad_to_thermal_fea",
        sh.study_ref("ecad", "ecad_thermal_handoff", doc_id=doc_id or None),
        sh.study_ref("thermal_fea", "run_fea_thermal"),
        geometry=[sh.geometry_ref("board", "step", path=built["step_path"],
                                  derived_by="engine.ecad.threed.board_to_fea_step",
                                  meta={"fused_single_solid": True})],
        conditions=conditions,
        expectations=expectations or None,
        warnings=warnings or None,
        meta={"environment": env, "material": material,
              "assumptions": built["assumptions"],
              "transient": tr, "quasi_steady": quasi_steady})

    base = {
        "ok": True, "doc_id": doc_id,
        "step_path": built["step_path"],
        "environment": env,
        "material": material,
        "assumptions": built["assumptions"],
        "coverage": coverage,
        "sources": sources,
        "trace_heating": trace_info,
        "transient_spec": tr,
        "quasi_steady": quasi_steady,
        "warnings": warnings,
    }

    if dry_run:
        return sh.attach({
            **base, "solved": False, "dry_run": True,
            "n_flux_loads": len(loads), "n_convection_loads": len(conv_loads),
            "n_fixtures": len(fixtures),
            "hint": "dry run: geometry + generated BCs + envelope only (no solve). "
                    f"{len(sources)} heat source(s), env {env['name']!r}.",
            "next_actions": ["ecad_thermal_handoff(dry_run=False)  # solve"],
        }, envelope)

    # ── 5. mesh + solve via the core thermal solver ─────────────────────
    try:
        from mcp_server import core_bridge as cb
    except Exception as e:  # noqa: BLE001
        cb = None
        mesh_err = f"core bridge import failed: {e}"
    if cb is not None:
        try:
            mesh = cb.call("sim_mesh", {"step_path": built["step_path"],
                                        "fineness": float(mesh_fineness)})
        except Exception as e:  # noqa: BLE001
            mesh = {"ok": False, "error": str(e)}
        mesh_err = None if mesh.get("ok") else mesh.get("error", "mesh failed")
    if cb is None or mesh_err:
        return sh.attach({
            **base, "solved": False, "dry_run": False,
            "error_solve": f"FEA leg unavailable: {mesh_err}",
            "hint": "geometry + BCs + envelope are valid; the mesher/solver "
                    "(libaicad with Netgen) is not available in this environment.",
            "next_actions": ["set AI_CAD_LIBAICAD to a sim-enabled libaicad build "
                             "and retry"],
        }, envelope)

    study = {"points": mesh["points"], "tets": mesh["tets"],
             "nodes_per_elem": mesh.get("nodes_per_elem", 4),
             "material": {"name": material["name"], "k_w_mk": material["k_w_mk"],
                          "cp_j_kgk": material["cp_j_kgk"],
                          "rho_kg_m3": material["rho_kg_m3"]},
             "fixtures": fixtures,
             "loads": [{k: v for k, v in ld.items() if k != "_ref"} for ld in loads]
                      + conv_loads,
             "transient": tr}
    try:
        res = cb.call("sim_run_thermal", {"study": study})
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "error": str(e)}
    if not res.get("ok"):
        return _err_d(f"thermal solve failed: {res.get('error')}",
                      hint="try a coarser mesh (mesh_fineness=0.2) or dry_run=True "
                           "to inspect the generated BCs.",
                      step_path=built["step_path"])

    rows = map_component_temps(components, mesh["points"],
                               res.get("temperature") or [],
                               res.get("envelope_max"))
    summary = res.get("summary", {})
    return sh.attach({
        **base, "solved": True, "dry_run": False,
        "solver": "C++ core transient conduction (backward Euler, quasi-steady "
                  "final frame)" if quasi_steady else
                  "C++ core transient conduction (backward Euler)",
        "n_nodes": res.get("n_nodes"), "n_elems": res.get("n_elems"),
        "n_steps": res.get("n_steps"),
        "board_max_temp_c": summary.get("final_max_temp_c",
                                        summary.get("max_temp_c")),
        "board_peak_temp_c": summary.get("max_temp_c"),
        "summary": summary,
        "components": rows,
        "solver_notes": res.get("solver_notes", []),
        "hint": (f"solved: board max {summary.get('final_max_temp_c', summary.get('max_temp_c'))} C "
                 f"in {env['name']} (h={env['h_w_m2k']} W/m2K, Ta={env['t_amb_c']} C); "
                 f"{len(sources)} heat source(s). Run ecad_verify_thermal() for the "
                 "per-component Tj/limits verdict."),
        "next_actions": ["ecad_verify_thermal()",
                         "ecad_thermal_handoff(include_trace_heating=True)  # add "
                         "copper I2R heating" if not trace_info.get("included") else
                         "ecad_thermal_handoff(environment='forced_air')  # compare"],
    }, envelope)


def register_ecad_thermal_tools(mcp) -> None:
    """Register ecad_thermal_handoff / ecad_verify_thermal on ``mcp`` (FastMCP)."""

    # Same module identity the rest of the server uses (see piping_tools note on the
    # double-import/_PERIODIC_TIMER hazard).
    from mcp_server.ecad_tools import _get_pcb, _default_out_dir
    from engine.ecad.ops import OpError

    def _find_pdn_block(board_name: str, step_path: str) -> Optional[dict]:
        """Locate the aicad.boardphys/1 pdn block written by ecad_run_pdn: sidecar next
        to a prior ecad_to_3d STEP, the pdn-only sidecar, or next to our FEA STEP."""
        out_dir = str(_default_out_dir(board_name))
        candidates = [os.path.join(out_dir, "board.step.boardphys.json"),
                      os.path.join(out_dir, "pdn.boardphys.json"),
                      step_path + ".boardphys.json"]
        for path in candidates:
            try:
                if os.path.isfile(path):
                    with open(path, encoding="utf-8") as fh:
                        phys = json.load(fh)
                    if isinstance(phys.get("pdn"), dict):
                        return phys["pdn"]
            except Exception:  # noqa: BLE001
                continue
        return None

    @mcp.tool()
    def ecad_thermal_handoff(doc_id: str = "",
                             environment: str = "still_air",
                             h_w_m2k: Optional[float] = None,
                             t_amb_c: Optional[float] = None,
                             include_trace_heating: bool = False,
                             transient: Optional[dict] = None,
                             mesh_fineness: float = 0.35,
                             dry_run: bool = False,
                             path: str = "") -> str:
        """One call from the active routed board to a solved thermal FEA study.

        Pipeline (zero manual BC setup):
        1. Builds the FEA-ready solid — board substrate + simplified component bodies
           fused into ONE solid (engine.ecad.threed.board_to_fea_step) with the
           FR4+copper stackup HOMOGENIZED to one effective material (in-plane
           rule-of-mixtures k; every assumption listed in the result).
        2. Applies one heat source PER component from its boardphys power metadata
           (power_w.typ, else .max) as a flux over its top face. Components WITHOUT
           power data get NO heat source and are listed in coverage.unpowered_refs
           (honesty rule — never invented watts).
        3. include_trace_heating=True adds the PDN I2R map (ecad_run_pdn) as distributed
           flux cells — copper trace/via heating visibly moves hotspots.
        4. Convection from the environment preset: 'still_air' (h=10), 'forced_air'
           (h=50), 'cold_plate' (board bottom held at t_amb + still air on top), or
           custom h_w_m2k/t_amb_c. Applied to the board top (outside component
           footprints), board bottom, and component tops.
        5. Solves via the core thermal solver. Steady answer = final frame of a
           quasi-steady transient (5 lumped time constants) because the steady kernel
           aggregates flux magnitudes; pass transient={"t_end_s":..., "dt_s":...,
           "schedules": {"U1": {"duty":0.25,"period_s":1.0}, ...}} for a true
           duty-cycle study (schedules keyed by component ref, T-FIN18 shapes).
        6. Maps results back per component: case temp = max nodal T over the component
           body, Tj = Tcase + P*theta_jc when jc is known (honest null otherwise).

        dry_run=True stops before meshing/solving and returns the geometry, generated
        BCs and the aicad.simhandoff/1 envelope (coupling 'ecad_to_thermal_fea') for
        inspection. Then run ecad_verify_thermal() for the Tj/limits verdict.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(e.message, hint="thermal handoff needs an open routed board.",
                        next_actions=["ecad_new_pcb('myboard')", "ecad_make_board(...)"])
        board_doc = session.doc
        out_path = path or str(_default_out_dir(board_doc.get("name", "board"))
                               / "board_fea.step")
        result = thermal_handoff_impl(
            board_doc, doc_id=session.doc_id, environment=environment,
            h_w_m2k=h_w_m2k, t_amb_c=t_amb_c,
            include_trace_heating=include_trace_heating, transient=transient,
            mesh_fineness=mesh_fineness, dry_run=dry_run, out_path=out_path,
            find_pdn=_find_pdn_block)
        _THERMAL["last_handoff"] = result if result.get("solved") else None
        return _dumps(result)

    @mcp.tool()
    def ecad_verify_thermal(handoff_result: Union[str, dict, None] = None,
                            tolerance_pct: float = 75.0) -> str:
        """Verify the last (or a passed) ecad_thermal_handoff result against component
        temperature limits + a theta_ja hand calc.

        * Per-component table: Tj (case + P*theta_jc, or Tcase when jc unknown) vs
          tj_max -> margin_c + pass. Components without limits are listed UNCHECKED.
        * Hand-calc cross-check (the aicad.simhandoff/1 verify checks): temperature
          RISE Tj-Ta vs P*theta_ja per component with a known theta_ja. Tolerance is
          GENEROUS by design (default 75 %): JEDEC theta_ja assumes a standard 2s2p
          test board in still air, not this layout — the check catches setup blunders
          (units/area/missing source), not layout-grade accuracy.

        Returns {ok, pass, verdict, table, checks, advisories, simhandoff}.
        """
        hr = handoff_result
        if isinstance(hr, str) and hr.strip():
            try:
                hr = json.loads(hr)
            except ValueError as e:
                return _err(f"handoff_result is not valid JSON: {e}")
        if hr is None:
            hr = _THERMAL.get("last_handoff")
        if not isinstance(hr, dict):
            return _err("no solved ecad_thermal_handoff result available",
                        hint="run ecad_thermal_handoff() (dry_run=False) first, or pass "
                             "its result.",
                        next_actions=["ecad_thermal_handoff()"])
        if not hr.get("solved"):
            return _err("handoff result was not solved (dry run or solver unavailable) "
                        "— nothing to verify",
                        next_actions=["ecad_thermal_handoff(dry_run=False)"])

        env = hr.get("environment") or {}
        t_amb = float(env.get("t_amb_c", 25.0))
        rows = hr.get("components") or []
        table = build_component_table(rows, t_amb)
        checks, ja_advisories = theta_ja_checks(rows, t_amb, float(tolerance_pct))

        limit_fails = [e for e in table if e["pass"] is False]
        unchecked = [e["ref"] for e in table if e["pass"] is None]
        advisories = [f"{e['ref']}: Tj {e['tj_used_c']} C exceeds tj_max {e['tj_max_c']} C "
                      f"(margin {e['margin_c']} C)" for e in limit_fails] + ja_advisories
        if unchecked:
            advisories.append("unchecked (missing Tj limit or unsolved): "
                              + ", ".join(unchecked))

        limits_pass = not limit_fails and any(e["pass"] for e in table)
        checks_pass = all(c["pass"] for c in checks) and bool(checks)
        verdict = (f"{'PASS' if limits_pass else 'FAIL'}: "
                   f"{sum(1 for e in table if e['pass'])} component(s) within Tj limits, "
                   f"{len(limit_fails)} over, {len(unchecked)} unchecked; theta_ja hand "
                   f"calc {'agrees' if checks_pass else 'DISAGREES'} within "
                   f"{tolerance_pct:.0f} % rise tolerance"
                   + ("" if checks else " (no component had a theta_ja to check)"))

        result: dict = {
            "ok": True,
            "pass": bool(limits_pass and (checks_pass or not checks)),
            "limits_pass": limits_pass,
            "handcalc_pass": checks_pass if checks else None,
            "verdict": verdict,
            "t_amb_c": t_amb,
            "table": table,
            "advisories": advisories,
            "warnings": ["theta_ja hand calc uses JEDEC single-part standard-board "
                         "values — generous tolerance is intentional"],
            "hint": verdict,
            "next_actions": (["increase copper / add thermal vias / move to "
                              "'forced_air' and re-run ecad_thermal_handoff()"]
                             if limit_fails else
                             ["design margin OK — consider "
                              "ecad_thermal_handoff(include_trace_heating=True) for the "
                              "copper-heating case"]),
        }
        if checks:
            src_env = (hr.get("simhandoff") or {})
            sh.attach(result, sh.build_verify(
                "ecad_to_thermal_fea",
                sh.study_ref("ecad", "ecad_thermal_handoff", doc_id=hr.get("doc_id")),
                sh.study_ref("thermal_fea", "run_fea_thermal"),
                checks, verdict,
                handoff_id=src_env.get("id"),
                advisories=advisories or None,
                warnings=result["warnings"]))
        return _dumps(result)
