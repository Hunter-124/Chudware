"""ECAD -> thermal FEA handoff — one call from a routed board to a solved
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
require the TRANSIENT path, which keeps per-load magnitudes + schedules. We
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
from services.mcp.cad_common import coerce_json_or_repr, require_mapping

# Environment presets: film coefficient (W/m^2-K) + default ambient (C).
# still_air ~ natural convection off a horizontal board; forced_air ~ moderate fan flow
# (2-3 m/s); cold_plate holds the board BOTTOM at the plate temperature (Dirichlet) with
# still air on top. Engineering nominals — override with h_w_m2k / t_amb_c for a custom
# environment.
ENVIRONMENTS: dict = {
    "still_air": {"h_w_m2k": 10.0, "t_amb_c": 25.0, "cold_plate": False},
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

_THERMAL: dict = {
    "last_handoff": None,  # session state for ecad_verify_thermal
    "last_ctx": None,
}  # solve context cache for heatsink_select
# geometry/loads/mesh reuse.


def _dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2)


def _err(
    reason: str, hint: str = "", next_actions: Optional[list] = None, **extra: Any
) -> str:
    return _dumps(_err_d(reason, hint, next_actions, **extra))


def _err_d(
    reason: str, hint: str = "", next_actions: Optional[list] = None, **extra: Any
) -> dict:
    """Dict-shaped error envelope (thermal_handoff_impl returns dicts; the MCP tool
    and the GUI worker endpoint each serialize/relay as their transport needs)."""
    out: dict = {
        "ok": False,
        "error": reason,
        "warnings": [],
        "hint": hint or reason,
        "next_actions": next_actions or [],
    }
    out.update(extra)
    return out


# --------------------------------------------------------------------------- pure helpers
# Module-level and engine-free so tests can drive them without OCP/libchudware.


def resolve_environment(
    environment: str, h_w_m2k: Optional[float], t_amb_c: Optional[float]
) -> dict:
    """Resolve and validate convection inputs in W/m²-K and degrees C."""
    if not isinstance(environment, str) or not environment.strip():
        raise ValueError("environment must be a non-empty preset name")
    env = ENVIRONMENTS.get(environment)
    if env is None:
        if h_w_m2k is None:
            raise ValueError(
                f"unknown environment {environment!r} — use one of "
                f"{sorted(ENVIRONMENTS)} or pass h_w_m2k (+ t_amb_c) for a custom one"
            )
        env = {"h_w_m2k": h_w_m2k, "t_amb_c": 25.0, "cold_plate": False}
    out = dict(env)
    if h_w_m2k is not None:
        out["h_w_m2k"] = h_w_m2k
    if t_amb_c is not None:
        out["t_amb_c"] = t_amb_c
    try:
        out["h_w_m2k"] = float(out["h_w_m2k"])
        out["t_amb_c"] = float(out["t_amb_c"])
    except (TypeError, ValueError) as exc:
        raise ValueError("h_w_m2k and t_amb_c must be numeric") from exc
    if not math.isfinite(out["h_w_m2k"]) or out["h_w_m2k"] <= 0.0:
        raise ValueError("h_w_m2k must be finite and > 0 W/m²-K")
    if not math.isfinite(out["t_amb_c"]) or out["t_amb_c"] < -273.15:
        raise ValueError("t_amb_c must be finite and >= -273.15 C")
    out["name"] = environment
    return out


def _rect_grid(
    rect: list,
    z: float,
    max_step: float = _GRID_MAX_STEP,
    exclude: Optional[list] = None,
) -> list:
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
            if exclude and any(
                r[0] <= x <= r[2] and r[1] <= y <= r[3] for r in exclude
            ):
                continue
            pts.append([round(x, 4), round(y, 4), round(z, 4)])
    return pts


def _faceset(points: list, normal: list) -> dict:
    return {
        "points": points,
        "tol_mm": _FACE_TOL_MM,
        "face_normal": normal,
        "perp_tol_mm": _PERP_TOL_MM,
    }


def component_power_w(rec: dict) -> Optional[float]:
    """Operating power in watts: typ preferred, else max; ``None`` when unknown."""
    power = rec.get("power_w")
    if power is None:
        return None
    if not isinstance(power, dict):
        raise ValueError(f"{rec.get('ref') or '?'} power_w must be an object")
    saw_value = False
    for key in ("typ", "max"):
        raw = power.get(key)
        if raw is None:
            continue
        saw_value = True
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(
                f"{rec.get('ref') or '?'} power_w.{key} must be numeric watts"
            )
        value = float(raw)
        if not math.isfinite(value) or value < 0.0:
            raise ValueError(
                f"{rec.get('ref') or '?'} power_w.{key} must be finite and >= 0 W"
            )
        if value > 0.0:
            return value
    return 0.0 if saw_value else None


def build_heat_loads(components: list, schedules: Optional[dict] = None) -> tuple:
    """Build component fluxes with explicit no-power and no-geometry coverage."""
    if schedules is not None and not isinstance(schedules, dict):
        raise ValueError("transient.schedules must be an object keyed by component ref")
    loads: list = []
    conditions: list = []
    sources: list = []
    unpowered: list = []
    unmapped: list = []
    for rec in components:
        if not isinstance(rec, dict):
            raise ValueError("boardphys components must be objects")
        ref = rec.get("ref") or rec.get("id") or "?"
        fea = rec.get("fea")
        if not isinstance(fea, dict):
            unmapped.append(ref)
            continue
        p_w = component_power_w(rec)
        if p_w is None or p_w == 0.0:
            unpowered.append(ref)
            continue
        try:
            area_mm2 = float(fea["top_area_mm2"])
            surface_z = float(
                fea["z_bot_mm"] if fea.get("side") == "bottom" else fea["z_top_mm"]
            )
            rect = [float(value) for value in fea["rect_mm"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{ref} has malformed FEA geometry") from exc
        if (
            not math.isfinite(area_mm2)
            or area_mm2 <= 0.0
            or not math.isfinite(surface_z)
            or len(rect) != 4
            or not all(math.isfinite(value) for value in rect)
            or rect[2] <= rect[0]
            or rect[3] <= rect[1]
        ):
            raise ValueError(
                f"{ref} FEA top area/rectangle must be finite and positive in mm"
            )
        geometric_area = (rect[2] - rect[0]) * (rect[3] - rect[1])
        if abs(area_mm2 - geometric_area) > max(1e-6, geometric_area * 0.02):
            raise ValueError(
                f"{ref} top_area_mm2={area_mm2:g} does not match rect area "
                f"{geometric_area:g} mm²"
            )
        flux_w_m2 = p_w / (area_mm2 * 1e-6)
        normal = (
            [0.0, 0.0, 1.0] if fea.get("side", "top") != "bottom" else [0.0, 0.0, -1.0]
        )
        spec = {
            "faces": _faceset(_rect_grid(rect, surface_z, max_step=1.0), normal),
            "type": "flux",
            "magnitude": flux_w_m2,
            "_ref": ref,
        }
        sched = (schedules or {}).get(ref)
        if sched is not None:
            if not isinstance(sched, dict):
                raise ValueError(f"transient schedule for {ref} must be an object")
            spec["schedule"] = sched
        loads.append(spec)
        which = "typ" if (rec.get("power_w") or {}).get("typ") else "max"
        conditions.append(
            sh.condition(
                f"heat_{ref}",
                "heat_source",
                p_w,
                "W",
                f"boardphys.components[{ref}].power_w.{which}",
                applied_to=ref,
            )
        )
        sources.append(
            {
                "ref": ref,
                "power_w": p_w,
                "flux_w_m2": round(flux_w_m2, 1),
                "top_area_mm2": area_mm2,
                "scheduled": sched is not None,
            }
        )
    return loads, conditions, sources, unpowered, unmapped


def build_i2r_loads(pdn_block: dict, board: dict) -> tuple:
    """Map validated PDN watts to top-K surface fluxes with exact omitted-power accounting."""
    if not isinstance(pdn_block, dict):
        raise ValueError("PDN block must be an object")
    cells: list = []
    total_w = 0.0
    for net_index, net in enumerate(pdn_block.get("nets", []) or []):
        if not isinstance(net, dict) or not isinstance(net.get("i2r_map"), dict):
            raise ValueError(f"PDN net[{net_index}] has no i2r_map object")
        m = net["i2r_map"]
        cols = m.get("columns")
        if not isinstance(cols, list):
            raise ValueError(f"PDN net[{net_index}] i2r columns must be a list")
        try:
            i_layer = cols.index("layer")
            i_x = cols.index("x_mm")
            i_y = cols.index("y_mm")
            i_p = cols.index("p_w")
        except ValueError as exc:
            raise ValueError(
                f"PDN net[{net_index}] i2r columns must include layer/x_mm/y_mm/p_w"
            ) from exc
        size = float(m.get("cell_size_mm"))
        reported_total = float(m.get("total_w"))
        if (
            not math.isfinite(size)
            or size <= 0.0
            or not math.isfinite(reported_total)
            or reported_total < 0.0
        ):
            raise ValueError(
                "PDN cell_size_mm must be > 0 and total_w finite/non-negative"
            )
        total_w += reported_total
        for row_index, cell in enumerate(m.get("cells", []) or []):
            if not isinstance(cell, list) or len(cell) <= max(i_layer, i_x, i_y, i_p):
                raise ValueError(f"PDN cell[{row_index}] does not match its columns")
            x, y, power = float(cell[i_x]), float(cell[i_y]), float(cell[i_p])
            if not all(math.isfinite(value) for value in (x, y, power)) or power < 0.0:
                raise ValueError(
                    "PDN cell coordinates/power must be finite and power >= 0 W"
                )
            if power > 0.0:
                cells.append(
                    {"layer": cell[i_layer], "x": x, "y": y, "p_w": power, "size": size}
                )
        for via_index, via in enumerate(m.get("vias", []) or []):
            if not isinstance(via, dict):
                raise ValueError(f"PDN via[{via_index}] must be an object")
            at = via.get("at")
            if not isinstance(at, list) or len(at) < 2:
                raise ValueError(f"PDN via[{via_index}].at must be [x_mm, y_mm]")
            x, y, power = float(at[0]), float(at[1]), float(via.get("p_w", 0.0))
            if not all(math.isfinite(value) for value in (x, y, power)) or power < 0.0:
                raise ValueError(
                    "PDN via coordinates/power must be finite and power >= 0 W"
                )
            if power > 0.0:
                cells.append(
                    {"layer": "F.Cu", "x": x, "y": y, "p_w": power, "size": size}
                )
    if not cells:
        return (
            [],
            None,
            {
                "included": False,
                "total_w": round(total_w, 4),
                "n_cell_loads": 0,
                "omitted_w": round(total_w, 4),
                "note": "no explicit PDN I2R cells; all reported power is unmapped",
            },
        )
    cells.sort(key=lambda cell: cell["p_w"], reverse=True)
    kept = cells[:_MAX_I2R_LOADS]
    z_top, z_bot = float(board["z_top_mm"]), float(board["z_bot_mm"])
    if not math.isfinite(z_top) or not math.isfinite(z_bot) or z_top <= z_bot:
        raise ValueError("board z extents must be finite with z_top_mm > z_bot_mm")
    loads = []
    for cell in kept:
        on_top = not str(cell["layer"]).startswith("B.")
        z = z_top if on_top else z_bot
        normal = [0.0, 0.0, 1.0] if on_top else [0.0, 0.0, -1.0]
        half = cell["size"] / 2.0
        rect = [cell["x"] - half, cell["y"] - half, cell["x"] + half, cell["y"] + half]
        flux = cell["p_w"] / (cell["size"] * cell["size"] * 1e-6)
        loads.append(
            {
                "faces": _faceset(
                    _rect_grid(rect, z, max_step=max(cell["size"] / 2.0, 0.5)), normal
                ),
                "type": "flux",
                "magnitude": flux,
                "_ref": "i2r",
            }
        )
    included_w = sum(cell["p_w"] for cell in kept)
    if total_w + max(1e-9, total_w * 1e-6) < included_w:
        raise ValueError("PDN total_w is smaller than the mapped cell/via power")
    omitted = max(0.0, total_w - included_w)
    info = {
        "included": True,
        "total_w": round(total_w, 4),
        "included_w": round(included_w, 4),
        "n_cell_loads": len(kept),
        "omitted_w": round(omitted, 4),
    }
    cond = sh.condition(
        "trace_heating_i2r",
        "distributed_flux",
        info,
        "W",
        "boardphys.pdn.nets[*].i2r_map (ecad_run_pdn)",
    )
    return loads, cond, info


def quasi_steady_transient(board: dict, material: dict, env: dict) -> dict:
    """Transient spec that integrates to ~steady state: t_end = 5 tau of the lumped
    board (rho cp V / h A), bounded to [60 s, 2 h]; ~120 implicit steps."""
    try:
        bbox = [float(v) for v in board["bbox_mm"]]
        thickness_mm = float(board["thickness_mm"])
        rho = float(material["rho_kg_m3"])
        cp = float(material["cp_j_kgk"])
        h = float(env["h_w_m2k"])
        t_amb = float(env["t_amb_c"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "board/material/environment thermal properties must be numeric"
        ) from exc
    if (
        len(bbox) != 4
        or not all(math.isfinite(v) for v in bbox)
        or bbox[2] <= bbox[0]
        or bbox[3] <= bbox[1]
        or not math.isfinite(thickness_mm)
        or thickness_mm <= 0.0
    ):
        raise ValueError("board bbox and thickness must be finite and positive in mm")
    if not all(math.isfinite(v) and v > 0.0 for v in (rho, cp, h)):
        raise ValueError("rho_kg_m3, cp_j_kgk, and h_w_m2k must be finite and > 0")
    if not math.isfinite(t_amb) or t_amb < -273.15:
        raise ValueError("t_amb_c must be finite and >= -273.15 C")
    x0, y0, x1, y1 = bbox
    area_m2 = (x1 - x0) * (y1 - y0) * 1e-6
    vol_m3 = area_m2 * thickness_mm * 1e-3
    tau = rho * cp * vol_m3 / (h * 2.0 * area_m2)
    if not math.isfinite(tau) or tau <= 0.0:
        raise ValueError("derived board thermal time constant must be finite and > 0")
    t_end = min(max(5.0 * tau, 60.0), 7200.0)
    return {
        "t_end_s": round(t_end, 1),
        "dt_s": round(t_end / 120.0, 3),
        "t_init_c": t_amb,
        "capacitance": "lumped",
        "_quasi_steady": True,
    }


def map_component_temps(
    components: list,
    points: list,
    temperature: list,
    envelope_max: Optional[list] = None,
) -> list:
    """Map a complete finite nodal temperature field back to component bodies."""
    if not isinstance(components, list):
        raise ValueError("components must be a list")
    if not isinstance(points, list) or not isinstance(temperature, list):
        raise ValueError("thermal points and temperature must be lists")
    n = len(temperature)
    if len(points) != 3 * n:
        raise ValueError(
            "thermal point array must contain exactly 3 coordinates per temperature"
        )
    if not all(
        isinstance(v, (int, float)) and math.isfinite(float(v))
        for v in points + temperature
    ):
        raise ValueError("thermal points and temperatures must be finite numbers")
    if envelope_max is not None:
        if (
            not isinstance(envelope_max, list)
            or len(envelope_max) != n
            or not all(
                isinstance(v, (int, float)) and math.isfinite(float(v))
                for v in envelope_max
            )
        ):
            raise ValueError(
                "thermal envelope_max must be a finite list matching temperature"
            )
    out = []
    for rec in components:
        if not isinstance(rec, dict):
            raise ValueError("thermal component records must be objects")
        fea = rec.get("fea")
        ref = rec.get("ref") or rec.get("id") or "?"
        row: dict = {
            "ref": ref,
            "power_w": component_power_w(rec),
            "t_case_c": None,
            "t_case_peak_c": None,
            "tj_c": None,
            "theta_jc": ((rec.get("theta_c_per_w") or {}).get("jc")),
            "theta_ja": ((rec.get("theta_c_per_w") or {}).get("ja")),
            "tj_max_c": ((rec.get("limits_c") or {}).get("tj_max")),
            "n_nodes": 0,
            "mapped": False,
            "map_status": "component has no FEA body",
        }
        if fea and n:
            try:
                rect = [float(v) for v in fea["rect_mm"]]
                z0, z1 = float(fea["z_bot_mm"]), float(fea["z_top_mm"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{ref} has malformed FEA geometry") from exc
            if (
                len(rect) != 4
                or not all(math.isfinite(v) for v in rect + [z0, z1])
                or rect[2] <= rect[0]
                or rect[3] <= rect[1]
                or z1 == z0
            ):
                raise ValueError(
                    f"{ref} FEA geometry must be finite and non-degenerate"
                )
            x0, y0, x1, y1 = rect
            zlo = min(z0, z1) - 0.3
            zhi = max(z0, z1) + 0.3
            halo = 0.3
            t_max = None
            pk_max = None
            cnt = 0
            for i in range(n):
                x, y, z = points[3 * i], points[3 * i + 1], points[3 * i + 2]
                if (
                    x0 - halo <= x <= x1 + halo
                    and y0 - halo <= y <= y1 + halo
                    and zlo <= z <= zhi
                ):
                    cnt += 1
                    t = temperature[i]
                    t_max = t if t_max is None else max(t_max, t)
                    if envelope_max is not None:
                        pk = envelope_max[i]
                        pk_max = pk if pk_max is None else max(pk_max, pk)
            row["n_nodes"] = cnt
            if t_max is not None:
                row["mapped"] = True
                row["map_status"] = "mapped"
                row["t_case_c"] = round(t_max, 2)
                row["t_case_peak_c"] = (
                    round(pk_max, 2) if pk_max is not None else round(t_max, 2)
                )
                jc = row["theta_jc"]
                p = row["power_w"]
                if (
                    isinstance(jc, (int, float))
                    and math.isfinite(float(jc))
                    and jc >= 0.0
                ):
                    if isinstance(p, (int, float)) and math.isfinite(float(p)):
                        row["tj_c"] = round(t_max + p * jc, 2)
            else:
                row["map_status"] = "no solver nodes matched the component body"
        out.append(row)
    return out


def build_component_table(rows: list, t_amb_c: float) -> list:
    """Verify-side per-component table: Tj (or Tcase fallback) vs tj_max -> margin/pass.
    Components without limits or mapped temperatures remain explicitly unchecked."""
    if not isinstance(t_amb_c, (int, float)) or not math.isfinite(float(t_amb_c)):
        raise ValueError("ambient temperature must be finite")
    table = []
    for r in rows:
        if not isinstance(r, dict):
            raise ValueError("component temperature rows must be objects")
        tj = r.get("tj_c")
        basis = "tj (case + P*theta_jc)"
        if tj is None:
            tj = r.get("t_case_c")
            basis = "t_case (theta_jc unknown — Tj >= this)"
        lim = r.get("tj_max_c")
        entry = {
            "ref": r.get("ref", "?"),
            "power_w": r.get("power_w"),
            "t_case_c": r.get("t_case_c"),
            "tj_c": r.get("tj_c"),
            "tj_used_c": tj,
            "basis": basis,
            "tj_max_c": lim,
            "margin_c": None,
            "pass": None,
        }
        if (
            isinstance(tj, (int, float))
            and math.isfinite(float(tj))
            and isinstance(lim, (int, float))
            and math.isfinite(float(lim))
        ):
            entry["margin_c"] = round(lim - tj, 2)
            entry["pass"] = entry["margin_c"] >= 0.0
        elif tj is None:
            entry["basis"] = "not solved (no nodes matched or dry run)"
        elif not isinstance(tj, (int, float)) or not math.isfinite(float(tj)):
            entry["basis"] = "invalid non-finite solved temperature"
        elif lim is None:
            entry["basis"] += "; tj_max unknown -> unchecked"
        else:
            entry["basis"] += "; tj_max invalid -> unchecked"
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
    total_p = sum(
        r["power_w"] for r in rows if isinstance(r.get("power_w"), (int, float))
    )
    for r in rows:
        ja = r.get("theta_ja")
        p = r.get("power_w")
        solved = r.get("tj_c") if r.get("tj_c") is not None else r.get("t_case_c")
        if not (
            isinstance(ja, (int, float))
            and isinstance(p, (int, float))
            and isinstance(solved, (int, float))
        ):
            continue
        rise_hand = p * ja
        rise_solved = solved - t_amb_c
        dev = abs(rise_solved - rise_hand) / max(abs(rise_hand), 1e-9) * 100.0
        if total_p > 0 and p / total_p < _JA_CHECK_MIN_POWER_SHARE:
            advisories.append(
                f"{r['ref']}: theta_ja hand calc not checked ({p} W is "
                f"{p / total_p:.0%} of board power — its temperature is dominated by "
                f"neighbor heating; solved rise {rise_solved:.1f} C vs self-heating "
                f"prediction {rise_hand:.1f} C)"
            )
            continue
        deviation_pct = round(dev, 1)
        checks.append(
            sh.check(
                f"tj_rise_{r['ref']}",
                "C",
                round(rise_hand, 2),
                round(rise_solved, 2),
                deviation_pct,
                tolerance_pct,
                deviation_pct <= tolerance_pct,
            )
        )
    return checks, advisories


# --------------------------------------------------------------------------- registration


# --------------------------------------------------------------------------- shared impl
# Session-free implementation of the ECAD -> thermal FEA handoff. The MCP tool
# below wraps it with the server's session lookup / output-dir conventions; the desktop
# GUI's worker endpoint (engine_oracle/sim/multiphysics_rpc.py) wraps it with an
# explicitly-passed board document. ONE pipeline, two transports — the fw_* pattern.
def thermal_handoff_impl(
    board_doc: dict,
    *,
    doc_id: str = "",
    environment: str = "still_air",
    h_w_m2k: Optional[float] = None,
    t_amb_c: Optional[float] = None,
    include_trace_heating: bool = False,
    transient: Optional[dict] = None,
    mesh_fineness: float = 0.35,
    dry_run: bool = False,
    out_path: str = "",
    find_pdn=None,
) -> dict:
    """Board doc (chudware ECAD board JSON) -> solved thermal FEA study dict."""
    warnings: list = []
    if not isinstance(board_doc, dict) or not board_doc:
        return _err_d(
            "no board document supplied",
            hint="thermal handoff needs a routed board's document JSON.",
            next_actions=["ecad_new_pcb('myboard')", "ecad_make_board(...)"],
        )

    if isinstance(transient, str) and transient.strip():
        coerced = coerce_json_or_repr(transient)
        if not isinstance(coerced, dict):
            return _err_d(
                f"transient is not a valid JSON object: {transient!r}",
                hint='Pass transient={"t_end_s": 3600, "dt_s": 30, '
                '"schedules": {...}} or a JSON object string.',
            )
        transient = coerced
    if transient is not None and not isinstance(transient, dict):
        return _err_d("transient must be an object or null")
    try:
        mesh_fineness = float(mesh_fineness)
    except (TypeError, ValueError):
        return _err_d("mesh_fineness must be numeric")
    if not math.isfinite(mesh_fineness) or not 0.0 < mesh_fineness <= 1.0:
        return _err_d("mesh_fineness must be finite and in (0, 1]")

    schedules = (
        (transient or {}).get("schedules") if isinstance(transient, dict) else None
    )
    if schedules is not None:
        if not isinstance(schedules, dict):
            return _err_d(
                "transient.schedules must be an object keyed by component ref"
            )
        for ref, schedule in schedules.items():
            if (
                not isinstance(ref, str)
                or not ref.strip()
                or not isinstance(schedule, dict)
            ):
                return _err_d(
                    "each transient schedule needs a component ref and object value"
                )
            if "duty" in schedule:
                if "points" in schedule:
                    return _err_d(
                        f"transient schedule for {ref} cannot mix duty and points"
                    )
                try:
                    duty = float(schedule["duty"])
                    period = float(schedule.get("period_s", 0.0))
                except (TypeError, ValueError):
                    return _err_d(f"transient duty/period for {ref} must be numeric")
                if (
                    not math.isfinite(duty)
                    or not 0.0 <= duty <= 1.0
                    or not math.isfinite(period)
                    or period <= 0.0
                ):
                    return _err_d(
                        f"transient schedule for {ref} needs duty in [0,1] and period_s > 0"
                    )
            elif "points" in schedule:
                points = schedule["points"]
                if not isinstance(points, list) or not points:
                    return _err_d(
                        f"transient schedule points for {ref} must be non-empty"
                    )
                last_t = -1.0
                try:
                    for point in points:
                        if not isinstance(point, list) or len(point) != 2:
                            raise ValueError
                        time_s, scale = float(point[0]), float(point[1])
                        if (
                            not math.isfinite(time_s)
                            or time_s < 0.0
                            or time_s <= last_t
                            or not math.isfinite(scale)
                        ):
                            raise ValueError
                        last_t = time_s
                    period = float(schedule.get("period_s", 0.0))
                except (TypeError, ValueError):
                    return _err_d(
                        f"transient schedule points for {ref} need increasing finite "
                        "[time_s, scale] pairs"
                    )
                if (
                    not math.isfinite(period)
                    or period < 0.0
                    or (period > 0.0 and last_t >= period)
                ):
                    return _err_d(
                        f"transient schedule period_s for {ref} must exceed its last point"
                    )
            else:
                return _err_d(
                    f"transient schedule for {ref} needs duty/period_s or points"
                )

    explicit_tr = None
    if isinstance(transient, dict) and any(
        key in transient for key in ("t_end_s", "dt_s", "t_init_c", "capacitance")
    ):
        if "t_end_s" not in transient:
            return _err_d("explicit transient parameters require t_end_s")
        try:
            explicit_tr = {
                "t_end_s": float(transient["t_end_s"]),
                "dt_s": float(transient.get("dt_s", 0.0)),
                "t_init_c": (
                    None
                    if "t_init_c" not in transient
                    else float(transient["t_init_c"])
                ),
                "capacitance": transient.get("capacitance", "lumped"),
            }
        except (TypeError, ValueError):
            return _err_d("transient t_end_s, dt_s, and t_init_c must be numeric")
        init_c = explicit_tr["t_init_c"]
        if (
            not math.isfinite(explicit_tr["t_end_s"])
            or explicit_tr["t_end_s"] <= 0.0
            or not math.isfinite(explicit_tr["dt_s"])
            or explicit_tr["dt_s"] < 0.0
            or explicit_tr["dt_s"] > explicit_tr["t_end_s"]
            or (init_c is not None and (not math.isfinite(init_c) or init_c < -273.15))
        ):
            return _err_d(
                "transient requires finite t_end_s > 0, 0 <= dt_s <= t_end_s, "
                "and t_init_c >= -273.15 C"
            )
        if explicit_tr["capacitance"] not in ("lumped", "consistent"):
            return _err_d("transient capacitance must be 'lumped' or 'consistent'")

    try:
        env = resolve_environment(environment, h_w_m2k, t_amb_c)
    except ValueError as exc:
        return _err_d(str(exc))
    if explicit_tr is not None and explicit_tr["t_init_c"] is None:
        explicit_tr["t_init_c"] = env["t_amb_c"]

    if not out_path:
        import tempfile

        out_path = os.path.join(
            tempfile.gettempdir(), f"chudware_board_fea_{os.getpid()}.step"
        )

    try:
        from services.ecad.threed import (
            board_to_fea_step,
            BoardGeometryError,
        )  # STAYS: no C++ counterpart
    except ImportError as exc:
        return _err_d(
            f"3D kernel unavailable (ImportError: {exc})",
            hint="OCP/build123d not installed — the FEA solid cannot be built.",
            next_actions=["pip install build123d"],
            available=False,
        )
    try:
        built = board_to_fea_step(board_doc, out_path)
    except BoardGeometryError as exc:
        return _err_d(
            f"board geometry failed: {exc}",
            hint="the board needs a closed Edge.Cuts outline.",
            next_actions=["ecad_run_drc()", "ecad_status()"],
        )
    except Exception as exc:  # noqa: BLE001
        return _err_d(f"FEA solid build failed: {type(exc).__name__}: {exc}")

    try:
        material = built["material"]
        board_geo = built["board"]
        components = built["components"]
        summary_meta = built["summary"]
        assumptions_meta = built["assumptions"]
        step_path = built["step_path"]
        if (
            not isinstance(summary_meta, dict)
            or not isinstance(assumptions_meta, list)
            or not isinstance(step_path, str)
            or not step_path
        ):
            raise ValueError("FEA builder returned malformed summary/assumptions/path")
        if not isinstance(summary_meta.get("warnings", []), list):
            raise ValueError("FEA builder warnings must be a list")
        if not isinstance(material, dict) or not isinstance(board_geo, dict):
            raise ValueError("FEA builder returned malformed board/material metadata")
        if not isinstance(components, list):
            raise ValueError("FEA builder returned malformed component metadata")
        k_w_mk = float(material["k_w_mk"])
        if not math.isfinite(k_w_mk) or k_w_mk <= 0.0:
            raise ValueError("material k_w_mk must be finite and > 0")
        default_tr = quasi_steady_transient(board_geo, material, env)
        for rec in components:
            if not isinstance(rec, dict):
                raise ValueError("component metadata must contain objects")
            theta = rec.get("theta_c_per_w") or {}
            limits = rec.get("limits_c") or {}
            if not isinstance(theta, dict) or not isinstance(limits, dict):
                raise ValueError(
                    f"{rec.get('ref') or '?'} thermal metadata must be objects"
                )
            for key in ("ja", "jc"):
                value = theta.get(key)
                if value is not None and (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                    or value < 0.0
                ):
                    raise ValueError(
                        f"{rec.get('ref') or '?'} theta_{key} must be finite and >= 0 C/W"
                    )
            limit = limits.get("tj_max")
            if limit is not None and (
                isinstance(limit, bool)
                or not isinstance(limit, (int, float))
                or not math.isfinite(float(limit))
                or limit < -273.15
            ):
                raise ValueError(
                    f"{rec.get('ref') or '?'} tj_max must be finite and >= -273.15 C"
                )
    except (KeyError, TypeError, ValueError) as exc:
        return _err_d(f"invalid FEA thermal metadata: {exc}")
    warnings.extend(summary_meta.get("warnings", []))

    component_refs = {str(rec.get("ref") or rec.get("id") or "?") for rec in components}
    unknown_schedules = sorted(set(schedules or {}) - component_refs)
    if unknown_schedules:
        return _err_d(
            "transient schedules reference unknown components: "
            + ", ".join(unknown_schedules)
        )

    try:
        loads, conditions, sources, unpowered, unmapped = build_heat_loads(
            components, schedules
        )
    except ValueError as exc:
        return _err_d(f"invalid component heat source: {exc}")
    coverage = {
        "n_components": len(components),
        "n_heat_sources": len(sources),
        "n_mapped_components": len(components) - len(unmapped),
        "unpowered_refs": unpowered,
        "unmapped_refs": unmapped,
        "note": (
            "every mapped component has positive power metadata"
            if not unpowered
            else f"{len(unpowered)} mapped component(s) have NO positive power metadata "
            f"and got NO heat source: {', '.join(unpowered)}"
        ),
    }
    if unmapped:
        warnings.append(
            f"{len(unmapped)} component(s) have no FEA body and were not thermally "
            f"mapped: {', '.join(unmapped)}"
        )
    if not sources:
        return _err_d(
            "no mapped component carries positive power metadata — nothing to heat",
            hint="seed power_w on at least one mapped component.",
            next_actions=["ecad_status()"],
            coverage=coverage,
        )

    trace_info = {"included": False}
    if include_trace_heating:
        try:
            pdn = (
                find_pdn(board_doc.get("name", "board"), out_path) if find_pdn else None
            )
        except Exception as exc:  # noqa: BLE001
            return _err_d(
                f"failed to load requested PDN I2R map: {type(exc).__name__}: {exc}"
            )
        if pdn is None:
            warnings.append(
                "include_trace_heating=True but no PDN I2R map found — "
                "run ecad_run_pdn first; continuing without trace heating."
            )
        else:
            try:
                i2r_loads, i2r_cond, trace_info = build_i2r_loads(pdn, board_geo)
            except ValueError as exc:
                return _err_d(f"invalid PDN I2R heat map: {exc}")
            loads.extend(i2r_loads)
            if i2r_cond:
                conditions.append(i2r_cond)
            if trace_info.get("omitted_w"):
                warnings.append(
                    f"I2R map truncated to the {trace_info['n_cell_loads']} hottest "
                    f"cells; {trace_info['omitted_w']} W omitted."
                )

    comp_rects_top = [
        c["fea"]["rect_mm"]
        for c in components
        if c.get("fea") and c["fea"].get("side", "top") != "bottom"
    ]
    top_pts = _rect_grid(
        board_geo["bbox_mm"], board_geo["z_top_mm"], exclude=comp_rects_top
    )
    for component in components:
        fea = component.get("fea")
        if fea and fea.get("side", "top") != "bottom":
            top_pts += _rect_grid(fea["rect_mm"], fea["z_top_mm"], max_step=1.0)
    fixtures: list = []
    conv_loads: list = [
        {
            "faces": _faceset(top_pts, [0.0, 0.0, 1.0]),
            "type": "convection",
            "h": env["h_w_m2k"],
            "t_amb_c": env["t_amb_c"],
        }
    ]
    if env.get("cold_plate"):
        fixtures.append(
            {
                **_faceset(
                    _rect_grid(board_geo["bbox_mm"], board_geo["z_bot_mm"]),
                    [0.0, 0.0, -1.0],
                ),
                "temp_c": env["t_amb_c"],
            }
        )
        conditions.append(
            sh.condition(
                "cold_plate_bottom",
                "fixed_temperature",
                env["t_amb_c"],
                "C",
                "environment preset 'cold_plate'",
                applied_to="board bottom face",
            )
        )
    else:
        conv_loads.append(
            {
                "faces": _faceset(
                    _rect_grid(board_geo["bbox_mm"], board_geo["z_bot_mm"]),
                    [0.0, 0.0, -1.0],
                ),
                "type": "convection",
                "h": env["h_w_m2k"],
                "t_amb_c": env["t_amb_c"],
            }
        )
    conditions.append(
        sh.condition(
            "convection_env",
            "convection",
            {"h_w_m2k": env["h_w_m2k"], "t_amb_c": env["t_amb_c"]},
            "W/m2-K",
            f"environment preset {env['name']!r}",
            applied_to="board top/bottom + component top faces",
        )
    )
    warnings.append(
        "component SIDE faces carry no convection (conservative — "
        "solved temps run slightly hot)"
    )

    if explicit_tr is not None:
        tr = explicit_tr
        quasi_steady = False
    else:
        tr = default_tr
        tr.pop("_quasi_steady", None)
        quasi_steady = True

    expectations = []
    for rec in components:
        ja = (rec.get("theta_c_per_w") or {}).get("ja")
        p_w = component_power_w(rec)
        if isinstance(ja, (int, float)) and isinstance(p_w, (int, float)):
            expectations.append(
                sh.expectation(
                    f"tj_{rec.get('ref')}_c",
                    round(env["t_amb_c"] + p_w * ja, 2),
                    "C",
                    f"hand calc Ta + P*theta_ja (JEDEC theta_ja = {ja} C/W; layout-"
                    "dependent seed — generous tolerance applies)",
                )
            )

    envelope = sh.build_handoff(
        "ecad_to_thermal_fea",
        sh.study_ref("ecad", "ecad_thermal_handoff", doc_id=doc_id or None),
        sh.study_ref("thermal_fea", "run_fea_thermal"),
        geometry=[
            sh.geometry_ref(
                "board",
                "step",
                path=built["step_path"],
                derived_by="engine.ecad.threed.board_to_fea_step",
                meta={"fused_single_solid": True},
            )
        ],
        conditions=conditions,
        expectations=expectations or None,
        warnings=warnings or None,
        meta={
            "environment": env,
            "material": material,
            "assumptions": built["assumptions"],
            "transient": tr,
            "quasi_steady": quasi_steady,
        },
    )

    base = {
        "ok": True,
        "doc_id": doc_id,
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
    ctx = {
        "doc_id": doc_id,
        "step_path": built["step_path"],
        "board": board_geo,
        "components": components,
        "env": env,
        "material": material,
        "transient": tr,
        "flux_loads": loads,
        "fixtures": fixtures,
        "mesh": None,
        "mesh_fineness": mesh_fineness,
        "quasi_steady": quasi_steady,
        "handoff_envelope_id": envelope.get("id"),
    }
    _THERMAL["last_ctx"] = ctx

    if dry_run:
        return sh.attach(
            {
                **base,
                "solved": False,
                "dry_run": True,
                "n_flux_loads": len(loads),
                "n_convection_loads": len(conv_loads),
                "n_fixtures": len(fixtures),
                "hint": "dry run: geometry + generated BCs + envelope only (no solve). "
                f"{len(sources)} heat source(s), env {env['name']!r}.",
                "next_actions": ["ecad_thermal_handoff(dry_run=False)  # solve"],
            },
            envelope,
        )

    source_refs_for_mesh = {source["ref"] for source in sources}
    source_spans = [
        min(
            float(component["fea"]["rect_mm"][2])
            - float(component["fea"]["rect_mm"][0]),
            float(component["fea"]["rect_mm"][3])
            - float(component["fea"]["rect_mm"][1]),
        )
        for component in components
        if (component.get("ref") or component.get("id")) in source_refs_for_mesh
        and isinstance(component.get("fea"), dict)
    ]
    # A heat-source patch smaller than the automatically chosen bulk element can
    # contain no complete exterior FE face. Resolve at least two elements across
    # the smallest powered footprint; the core still applies its memory governor
    # and the caller's fineness everywhere else.
    source_max_size_mm = min(source_spans) / 2.0 if source_spans else 0.0
    try:
        from services.mcp import core_bridge as cb

        mesh_err = None
    except Exception as exc:  # noqa: BLE001
        cb = None
        mesh_err = f"core bridge import failed: {exc}"
    if cb is not None:
        try:
            mesh_params = {"step_path": built["step_path"], "fineness": mesh_fineness}
            if source_max_size_mm > 0.0:
                mesh_params["max_size_mm"] = source_max_size_mm
            mesh = cb.call("sim_mesh", mesh_params)
        except Exception as exc:  # noqa: BLE001
            mesh = {"ok": False, "error": str(exc)}
        if not isinstance(mesh, dict):
            mesh_err = "mesher returned a non-object response"
        else:
            mesh_err = None if mesh.get("ok") else mesh.get("error", "mesh failed")
    if cb is None:
        reason = f"FEA leg unavailable: {mesh_err}"
        return sh.attach(
            {
                **base,
                "ok": False,
                "solved": False,
                "dry_run": False,
                "available": False,
                "error": reason,
                "error_solve": reason,
                "hint": "the core mesher/solver is not available in this environment.",
                "next_actions": [
                    "set CHUDWARE_LIBCHUDWARE to a sim-enabled libchudware build and retry"
                ],
            },
            envelope,
        )
    if mesh_err:
        reason = f"thermal meshing failed: {mesh_err}"
        return sh.attach(
            {
                **base,
                "ok": False,
                "solved": False,
                "dry_run": False,
                "available": True,
                "error": reason,
                "error_solve": reason,
                "hint": "the thermal mesher rejected or could not mesh the generated geometry.",
                "next_actions": [
                    "try a coarser mesh_fineness or repair board geometry"
                ],
            },
            envelope,
        )

    try:
        mesh_points = mesh["points"]
        mesh_tets = mesh["tets"]
        nodes_per_elem = mesh.get("nodes_per_elem", 4)
        if (
            not isinstance(mesh_points, list)
            or not mesh_points
            or len(mesh_points) % 3 != 0
            or not all(
                isinstance(v, (int, float)) and math.isfinite(float(v))
                for v in mesh_points
            )
        ):
            raise ValueError("points must be a non-empty finite flat xyz array")
        if (
            isinstance(nodes_per_elem, bool)
            or not isinstance(nodes_per_elem, int)
            or nodes_per_elem not in (4, 10)
        ):
            raise ValueError("nodes_per_elem must be 4 or 10")
        n_nodes = len(mesh_points) // 3
        if (
            not isinstance(mesh_tets, list)
            or not mesh_tets
            or len(mesh_tets) % nodes_per_elem != 0
            or not all(
                isinstance(v, int) and not isinstance(v, bool) and 0 <= v < n_nodes
                for v in mesh_tets
            )
        ):
            raise ValueError("tets must contain complete in-range integer connectivity")
    except (KeyError, TypeError, ValueError) as exc:
        return sh.attach(
            {
                **base,
                "ok": False,
                "solved": False,
                "dry_run": False,
                "error": f"invalid thermal mesh: {exc}",
                "error_solve": f"invalid thermal mesh: {exc}",
            },
            envelope,
        )

    study = {
        "points": mesh_points,
        "tets": mesh_tets,
        "nodes_per_elem": nodes_per_elem,
        "material": {
            "name": material.get("name", "board"),
            "k_w_mk": material["k_w_mk"],
            "cp_j_kgk": material["cp_j_kgk"],
            "rho_kg_m3": material["rho_kg_m3"],
        },
        "fixtures": fixtures,
        "loads": [{k: v for k, v in load.items() if k != "_ref"} for load in loads]
        + conv_loads,
        "transient": tr,
    }
    try:
        res = cb.call("sim_run_thermal", {"study": study})
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "error": str(exc)}
    if not isinstance(res, dict) or not res.get("ok"):
        error = (
            res.get("error") if isinstance(res, dict) else "non-object solver response"
        )
        return sh.attach(
            {
                **base,
                "ok": False,
                "solved": False,
                "dry_run": False,
                "error": f"thermal solve failed: {error}",
                "error_solve": f"thermal solve failed: {error}",
                "hint": "try a coarser mesh (mesh_fineness=0.2) or dry_run=True "
                "to inspect the generated BCs.",
            },
            envelope,
        )

    try:
        temperatures = res.get("temperature")
        envelope_max = res.get("envelope_max")
        rows = map_component_temps(components, mesh_points, temperatures, envelope_max)
        summary = res.get("summary") or {}
        if not isinstance(summary, dict):
            raise ValueError("summary must be an object")
        final_max = summary.get("final_max_temp_c", summary.get("max_temp_c"))
        peak_max = summary.get("max_temp_c")
        if final_max is None:
            final_max = max(temperatures)
        if peak_max is None:
            peak_max = (
                max(envelope_max) if envelope_max is not None else max(temperatures)
            )
        final_max = float(final_max)
        peak_max = float(peak_max)
        if not math.isfinite(final_max) or not math.isfinite(peak_max):
            raise ValueError("summary temperatures must be finite")
    except (TypeError, ValueError) as exc:
        return sh.attach(
            {
                **base,
                "ok": False,
                "solved": False,
                "dry_run": False,
                "error": f"invalid thermal solver result: {exc}",
                "error_solve": f"invalid thermal solver result: {exc}",
            },
            envelope,
        )

    ctx["mesh"] = mesh
    source_refs = {source["ref"] for source in sources}
    mapped_source_refs = {
        row["ref"] for row in rows if row.get("mapped") and row["ref"] in source_refs
    }
    map_missing = sorted(source_refs - mapped_source_refs)
    map_back_complete = not map_missing
    result = sh.attach(
        {
            **base,
            "ok": map_back_complete,
            "solved": True,
            "dry_run": False,
            "map_back_complete": map_back_complete,
            "unmapped_result_refs": map_missing,
            "solver": (
                "C++ core transient conduction (backward Euler, quasi-steady "
                "final frame)"
                if quasi_steady
                else "C++ core transient conduction (backward Euler)"
            ),
            "n_nodes": res.get("n_nodes", len(mesh_points) // 3),
            "n_elems": res.get("n_elems", len(mesh_tets) // nodes_per_elem),
            "n_steps": res.get("n_steps"),
            "board_max_temp_c": final_max,
            "board_peak_temp_c": peak_max,
            "summary": summary,
            "components": rows,
            "solver_notes": res.get("solver_notes", []),
            "hint": (
                f"solved: board max {final_max} C in {env['name']} "
                f"(h={env['h_w_m2k']} W/m2K, Ta={env['t_amb_c']} C); "
                f"{len(sources)} heat source(s). "
                + (
                    "Run ecad_verify_thermal() for the per-component verdict."
                    if map_back_complete
                    else f"Map-back incomplete for: {', '.join(map_missing)}."
                )
            ),
            "next_actions": (
                ["ecad_verify_thermal()"]
                if map_back_complete
                else ["refine the mesh or repair component FEA geometry and re-run"]
            ),
        },
        envelope,
    )
    if not map_back_complete:
        result["error"] = (
            "thermal solve completed but powered-component map-back was incomplete: "
            + ", ".join(map_missing)
        )
    return result


def register_ecad_thermal_tools(mcp) -> None:
    """Register ecad_thermal_handoff / ecad_verify_thermal on ``mcp`` (FastMCP)."""

    # Same module identity the rest of the server uses (see piping_tools note on the
    # double-import/_PERIODIC_TIMER hazard).
    from services.mcp.ecad_tools import _get_pcb, _default_out_dir
    from services.mcp.ecad_common import OpError

    def _find_pdn_block(board_name: str, step_path: str) -> Optional[dict]:
        """Locate the aicad.boardphys/1 pdn block written by ecad_run_pdn: sidecar next
        to a prior ecad_to_3d STEP, the pdn-only sidecar, or next to our FEA STEP."""
        out_dir = str(_default_out_dir(board_name))
        candidates = [
            os.path.join(out_dir, "board.step.boardphys.json"),
            os.path.join(out_dir, "pdn.boardphys.json"),
            step_path + ".boardphys.json",
        ]
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
    def ecad_thermal_handoff(
        doc_id: str = "",
        environment: str = "still_air",
        h_w_m2k: Optional[float] = None,
        t_amb_c: Optional[float] = None,
        include_trace_heating: bool = False,
        transient: Optional[dict] = None,
        mesh_fineness: float = 0.35,
        dry_run: bool = False,
        path: str = "",
    ) -> str:
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
           duty-cycle study (schedules keyed by component ref, transient-schedule shapes).
        6. Maps results back per component: case temp = max nodal T over the component
           body, Tj = Tcase + P*theta_jc when jc is known (honest null otherwise).

        dry_run=True stops before meshing/solving and returns the geometry, generated
        BCs and the aicad.simhandoff/1 envelope (coupling 'ecad_to_thermal_fea') for
        inspection. Then run ecad_verify_thermal() for the Tj/limits verdict.
        """
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(
                e.message,
                hint="thermal handoff needs an open routed board.",
                next_actions=["ecad_new_pcb('myboard')", "ecad_make_board(...)"],
            )
        board_doc = session.doc
        out_path = path or str(
            _default_out_dir(board_doc.get("name", "board")) / "board_fea.step"
        )
        result = thermal_handoff_impl(
            board_doc,
            doc_id=session.doc_id,
            environment=environment,
            h_w_m2k=h_w_m2k,
            t_amb_c=t_amb_c,
            include_trace_heating=include_trace_heating,
            transient=transient,
            mesh_fineness=mesh_fineness,
            dry_run=dry_run,
            out_path=out_path,
            find_pdn=_find_pdn_block,
        )
        _THERMAL["last_handoff"] = (
            result if result.get("ok") and result.get("solved") else None
        )
        return _dumps(result)

    @mcp.tool()
    def ecad_verify_thermal(
        handoff_result: Union[str, dict, None] = None, tolerance_pct: float = 75.0
    ) -> str:
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
            # LIVE CRASH class: a python-repr handoff_result string (single-quoted,
            # bare None) would json.loads-fail even though it IS the dict it looks
            # like — try the shared repr/JSON coercer before giving up.
            coerced = coerce_json_or_repr(hr)
            if isinstance(coerced, dict):
                hr = coerced
            else:
                try:
                    hr = json.loads(hr)
                except ValueError as e:
                    return _err(f"handoff_result is not valid JSON: {e}")
        if hr is None:
            hr = _THERMAL.get("last_handoff")
        if not isinstance(hr, dict):
            return _err(
                "no solved ecad_thermal_handoff result available",
                hint="run ecad_thermal_handoff() (dry_run=False) first, or pass "
                "its result.",
                next_actions=["ecad_thermal_handoff()"],
            )
        if not hr.get("solved"):
            return _err(
                "handoff result was not solved (dry run or solver unavailable) "
                "— nothing to verify",
                next_actions=["ecad_thermal_handoff(dry_run=False)"],
            )
        if not hr.get("ok") or hr.get("map_back_complete") is False:
            return _err(
                "handoff result is incomplete or invalid — nothing to verify",
                hint=hr.get("error", "rerun the thermal handoff"),
                next_actions=["ecad_thermal_handoff(dry_run=False)"],
            )

        try:
            tolerance = float(tolerance_pct)
            env = hr.get("environment") or {}
            t_amb = float(env.get("t_amb_c", 25.0))
            if (
                not math.isfinite(tolerance)
                or tolerance < 0.0
                or not math.isfinite(t_amb)
                or t_amb < -273.15
            ):
                raise ValueError
            rows = hr.get("components") or []
            if not isinstance(rows, list) or not rows:
                return _err("handoff has no component temperature rows to verify")
            table = build_component_table(rows, t_amb)
            checks, ja_advisories = theta_ja_checks(rows, t_amb, tolerance)
        except (TypeError, ValueError):
            return _err(
                "tolerance_pct and handoff temperatures must be finite; "
                "tolerance_pct must be >= 0"
            )

        limit_fails = [e for e in table if e["pass"] is False]
        unchecked = [e["ref"] for e in table if e["pass"] is None]
        advisories = [
            f"{e['ref']}: Tj {e['tj_used_c']} C exceeds tj_max {e['tj_max_c']} C "
            f"(margin {e['margin_c']} C)"
            for e in limit_fails
        ] + ja_advisories
        if unchecked:
            advisories.append(
                "unchecked (missing Tj limit or unsolved): " + ", ".join(unchecked)
            )

        limits_pass = bool(table) and not limit_fails and not unchecked
        checks_pass = all(c["pass"] for c in checks) and bool(checks)
        verdict = (
            f"{'PASS' if limits_pass else 'INCOMPLETE/FAIL'}: "
            f"{sum(1 for e in table if e['pass'])} component(s) within Tj limits, "
            f"{len(limit_fails)} over, {len(unchecked)} unchecked; theta_ja hand "
            f"calc {'agrees' if checks_pass else 'DISAGREES'} within "
            f"{tolerance:.0f} % rise tolerance"
            + ("" if checks else " (no component had a theta_ja to check)")
        )

        result: dict = {
            "ok": True,
            "pass": bool(limits_pass and (checks_pass or not checks)),
            "limits_pass": limits_pass,
            "handcalc_pass": checks_pass if checks else None,
            "verdict": verdict,
            "t_amb_c": t_amb,
            "table": table,
            "advisories": advisories,
            "warnings": [
                "theta_ja hand calc uses JEDEC single-part standard-board "
                "values — generous tolerance is intentional"
            ],
            "hint": verdict,
            "next_actions": (
                [
                    "increase copper / add thermal vias / move to 'forced_air' and re-run "
                    "ecad_thermal_handoff()"
                ]
                if limit_fails
                else ["add valid Tj limits / repair component map-back and re-run"]
                if unchecked
                else [
                    "design margin OK — consider "
                    "ecad_thermal_handoff(include_trace_heating=True) for the "
                    "copper-heating case"
                ]
            ),
        }
        if checks:
            src_env = hr.get("simhandoff") or {}
            sh.attach(
                result,
                sh.build_verify(
                    "ecad_to_thermal_fea",
                    sh.study_ref(
                        "ecad", "ecad_thermal_handoff", doc_id=hr.get("doc_id")
                    ),
                    sh.study_ref("thermal_fea", "run_fea_thermal"),
                    checks,
                    verdict,
                    handoff_id=src_env.get("id"),
                    advisories=advisories or None,
                    warnings=result["warnings"],
                ),
            )
        return _dumps(result)


# =============================================================================
# Board/enclosure airflow handoff — ECAD board -> OpenFOAM conjugate
# heat transfer (CHT), verifying the preset-h film-coefficient thermal solve.
#
# COUPLING ID: ``ecad_to_cht`` (new, justified): nothing from the thermal FEA
# *solution* is transferred to the CFD side — the CHT study is built directly
# from the ECAD board (geometry + boardphys powers), exactly like
# ``ecad_to_thermal_fea`` but with a different target solver. ``thermal_to_cfd``
# would wrongly imply the thermal FEA RESULT generates the CFD BCs; instead the
# preset-h solve only supplies EXPECTATIONS that ecad_verify_airflow checks.
# =============================================================================

_AIRFLOW: dict = {"last": None}  # session state for ecad_verify_airflow

# Effective isotropic conductivity for a simplified component body (molded package
# with die/leadframe smeared in) — an engineering nominal, overridable per call.
_COMPONENT_KAPPA_W_MK = 10.0
# Component boxes are sunk this far INTO the board slab so the solid regions share a
# robust interface after snappyHexMesh/splitMeshRegions (coincident faces are fragile).
_EMBED_MM = 0.2

_REGION_BAD = __import__("re").compile(r"[^A-Za-z0-9_]")
_REGION_TEMP_RE = __import__("re").compile(
    r"([A-Za-z0-9_]+): (-?\d+(?:\.\d+)?) C mean, (-?\d+(?:\.\d+)?) C peak"
)


def _region_name(ref: str, taken: set) -> str:
    """OpenFOAM-word region name for a component ref, unique among ``taken``."""
    base = "c_" + (_REGION_BAD.sub("_", str(ref)).strip("_") or "x")
    name, i = base, 1
    while name in taken or name in ("fluid", "board"):
        i += 1
        name = f"{base}_{i}"
    taken.add(name)
    return name


def write_box_stl(
    path: str, x0: float, y0: float, z0: float, x1: float, y1: float, z1: float
) -> str:
    """Write an axis-aligned watertight box as ASCII STL (world mm), outward normals."""
    v = [
        (x0, y0, z0),
        (x1, y0, z0),
        (x1, y1, z0),
        (x0, y1, z0),
        (x0, y0, z1),
        (x1, y0, z1),
        (x1, y1, z1),
        (x0, y1, z1),
    ]
    # (normal, [i, j, k]) x 12 — two CCW-outward triangles per face.
    faces = [
        ((0, 0, -1), (0, 2, 1)),
        ((0, 0, -1), (0, 3, 2)),
        ((0, 0, 1), (4, 5, 6)),
        ((0, 0, 1), (4, 6, 7)),
        ((0, -1, 0), (0, 1, 5)),
        ((0, -1, 0), (0, 5, 4)),
        ((0, 1, 0), (2, 3, 7)),
        ((0, 1, 0), (2, 7, 6)),
        ((-1, 0, 0), (0, 4, 7)),
        ((-1, 0, 0), (0, 7, 3)),
        ((1, 0, 0), (1, 2, 6)),
        ((1, 0, 0), (1, 6, 5)),
    ]
    lines = ["solid box"]
    for n, tri in faces:
        lines.append(f"  facet normal {n[0]} {n[1]} {n[2]}")
        lines.append("    outer loop")
        for i in tri:
            lines.append(f"      vertex {v[i][0]:.6f} {v[i][1]:.6f} {v[i][2]:.6f}")
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append("endsolid box")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="ascii") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def build_airflow_solids(
    board_geo: dict,
    material: dict,
    components: list,
    out_dir: str,
    component_kappa_w_mk: float = _COMPONENT_KAPPA_W_MK,
    embed_mm: float = _EMBED_MM,
) -> dict:
    """Build validated board and powered-component watertight STL solids for CHT."""
    if (
        not isinstance(board_geo, dict)
        or not isinstance(material, dict)
        or not isinstance(components, list)
    ):
        raise ValueError("board, material, and components must be structured objects")
    try:
        bbox = [float(v) for v in board_geo["bbox_mm"]]
        zb, zt = float(board_geo["z_bot_mm"]), float(board_geo["z_top_mm"])
        board_k = float(material["k_w_mk"])
        component_k = float(component_kappa_w_mk)
        embed = float(embed_mm)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("airflow geometry/material fields must be numeric") from exc
    if (
        len(bbox) != 4
        or not all(math.isfinite(v) for v in bbox + [zb, zt])
        or bbox[2] <= bbox[0]
        or bbox[3] <= bbox[1]
        or zt <= zb
    ):
        raise ValueError(
            "board airflow geometry must be finite and non-degenerate in mm"
        )
    if (
        not math.isfinite(board_k)
        or board_k <= 0.0
        or not math.isfinite(component_k)
        or component_k <= 0.0
    ):
        raise ValueError("board/component conductivity must be finite and > 0 W/m-K")
    if not math.isfinite(embed) or embed <= 0.0:
        raise ValueError("component embed_mm must be finite and > 0")

    records: list = []
    unpowered: list = []
    unmapped: list = []
    for rec in components:
        if not isinstance(rec, dict):
            raise ValueError("airflow component records must be objects")
        ref = rec.get("ref") or rec.get("id") or "?"
        p_w = component_power_w(rec)
        if p_w is None or p_w == 0.0:
            unpowered.append(ref)
            continue
        fea = rec.get("fea")
        if not isinstance(fea, dict):
            unmapped.append(ref)
            continue
        try:
            rect = [float(v) for v in fea["rect_mm"]]
            czb = float(fea["z_bot_mm"])
            czt = float(fea["z_top_mm"])
            area = float(fea["top_area_mm2"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{ref} has malformed airflow geometry") from exc
        if (
            len(rect) != 4
            or not all(math.isfinite(v) for v in rect + [czb, czt, area])
            or rect[2] <= rect[0]
            or rect[3] <= rect[1]
            or czt == czb
            or area <= 0.0
        ):
            raise ValueError(
                f"{ref} airflow geometry must be finite and non-degenerate"
            )
        geometric_area = (rect[2] - rect[0]) * (rect[3] - rect[1])
        if abs(area - geometric_area) > max(1e-6, geometric_area * 0.02):
            raise ValueError(f"{ref} top_area_mm2 does not match its rectangle area")
        records.append((rec, ref, p_w, rect, min(czb, czt), max(czb, czt)))

    x0, y0, x1, y1 = bbox
    solids = [
        {
            "name": "board",
            "stl_path": write_box_stl(
                os.path.join(out_dir, "board.stl"), x0, y0, zb, x1, y1, zt
            ),
            "kappa_w_mk": board_k,
            "power_w": 0.0,
        }
    ]
    conditions: list = []
    sources: list = []
    region_map: dict = {}
    taken: set = set()
    for rec, ref, p_w, rect, czb, czt in records:
        if rec["fea"].get("side", "top") != "bottom":
            czb -= embed
        else:
            czt += embed
        name = _region_name(ref, taken)
        region_map[ref] = name
        solids.append(
            {
                "name": name,
                "stl_path": write_box_stl(
                    os.path.join(out_dir, f"{name}.stl"),
                    rect[0],
                    rect[1],
                    czb,
                    rect[2],
                    rect[3],
                    czt,
                ),
                "kappa_w_mk": component_k,
                "power_w": p_w,
            }
        )
        which = "typ" if (rec.get("power_w") or {}).get("typ") else "max"
        conditions.append(
            sh.condition(
                f"heat_{ref}",
                "heat_source",
                p_w,
                "W",
                f"boardphys.components[{ref}].power_w.{which}",
                applied_to=ref,
            )
        )
        sources.append({"ref": ref, "region": name, "power_w": p_w})
    assumptions = [
        "board modeled as its bounding-box slab with the homogenized in-plane k "
        f"({board_k} W/mK) applied ISOTROPICALLY (the CHT solid model is isotropic; "
        "through-plane conduction is over-estimated)",
        "component bodies are their FEA cap boxes with an effective isotropic "
        f"k = {component_k} W/mK (molded package nominal), sunk {embed} mm into "
        "the board so solid regions share a meshable interface",
        "each powered component dissipates power_w as a volumetric source over its "
        "whole body (no die/junction detail — compare CASE temperatures)",
    ]
    return {
        "solids": solids,
        "conditions": conditions,
        "sources": sources,
        "unpowered_refs": unpowered,
        "unmapped_refs": unmapped,
        "region_map": region_map,
        "assumptions": assumptions,
    }


def build_enclosure_walls(board_geo: dict, enclosure: dict, out_dir: str) -> tuple:
    """Build a validated four-wall passive enclosure duct, open along the flow axis."""
    if not isinstance(board_geo, dict) or not isinstance(enclosure, dict):
        raise ValueError("board geometry and enclosure must be objects")
    try:
        c = float(enclosure.get("clearance_mm", 10.0))
        t = float(enclosure.get("wall_mm", 2.0))
        k = float(enclosure.get("kappa_w_mk", 0.2))
        bbox = [float(v) for v in board_geo["bbox_mm"]]
        zb, zt = float(board_geo["z_bot_mm"]), float(board_geo["z_top_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("enclosure and board dimensions must be numeric") from exc
    if not all(math.isfinite(v) for v in [c, t, k, zb, zt] + bbox):
        raise ValueError("enclosure and board dimensions must be finite")
    if c <= 0.0 or t <= 0.0 or k <= 0.0:
        raise ValueError(
            "enclosure clearance, wall thickness, and conductivity must be > 0"
        )
    if len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1] or zt <= zb:
        raise ValueError("board geometry must be non-degenerate")
    x0, y0, x1, y1 = bbox
    ix0, ix1 = x0 - c, x1 + c
    iy0, iy1, iz0, iz1 = y0 - c, y1 + c, zb - c, zt + c
    walls = [
        ("encl_bottom", ix0, iy0 - t, iz0 - t, ix1, iy1 + t, iz0),
        ("encl_top", ix0, iy0 - t, iz1, ix1, iy1 + t, iz1 + t),
        ("encl_left", ix0, iy0 - t, iz0, ix1, iy0, iz1),
        ("encl_right", ix0, iy1, iz0, ix1, iy1 + t, iz1),
    ]
    solids = [
        {
            "name": name,
            "stl_path": write_box_stl(
                os.path.join(out_dir, f"{name}.stl"), a, b, cz, d, e, f
            ),
            "kappa_w_mk": k,
            "power_w": 0.0,
        }
        for name, a, b, cz, d, e, f in walls
    ]
    assumptions = [
        f"enclosure modeled as a passive 4-wall duct (wall {t} mm, k={k} W/mK, "
        f"clearance {c} mm) open along the flow axis; the tunnel domain extends "
        "beyond it, so some inlet flow bypasses OUTSIDE the duct — inside-duct "
        "velocity is lower than the nominal inlet velocity (conservative)",
        "no radiation, no fan curve — inlet velocity is imposed, not solved",
    ]
    return solids, assumptions


def parse_region_temps(solver_notes: list) -> dict:
    """{region: {'mean_c': x, 'peak_c': y}} from postprocess_cht solver notes
    ('per-region: board: 41 C mean, 44 C peak; c_U1: ...'). Integer-degree
    resolution — that's the note format; fine for a %-rise verify."""
    out: dict = {}
    for note in solver_notes or []:
        s = str(note)
        if "per-region" not in s and " C mean" not in s:
            continue
        for m in _REGION_TEMP_RE.finditer(s):
            out[m.group(1)] = {"mean_c": float(m.group(2)), "peak_c": float(m.group(3))}
    return out


def airflow_deviation_table(
    preset_rows: list,
    cht_rows: list,
    t_amb_preset_c: float,
    inlet_temp_c: float,
    h_preset_w_m2k: Optional[float],
    tolerance_pct: float,
) -> tuple:
    """Per-component preset-h vs CHT deviation table — 'was the film-coefficient
    assumption sane?'.

    Compares temperature RISE above each study's own ambient (absolute temps would
    launder error through the shared ambient). Returns ``(table, checks, guidance)``:
    ``checks`` are simhandoff verify checks (|deviation| vs tolerance on the rise);
    ``guidance`` strings are emitted when the preset was OPTIMISTIC (CHT hotter),
    including an effective-h suggestion h_eff ~= h_preset * rise_preset/rise_cht."""
    by_ref = {r.get("ref"): r for r in preset_rows or []}
    table: list = []
    checks: list = []
    guidance: list = []
    for row in cht_rows:
        ref = row["ref"]
        pre = by_ref.get(ref)
        t_pre = pre.get("t_case_c") if isinstance(pre, dict) else None
        entry = {
            "ref": ref,
            "power_w": row.get("power_w"),
            "preset_t_case_c": t_pre,
            "cht_t_peak_c": row.get("t_peak_c"),
            "cht_t_mean_c": row.get("t_mean_c"),
            "rise_preset_c": None,
            "rise_cht_c": None,
            "deviation_pct": None,
            "verdict": "no preset solve to compare",
        }
        t_cht = row.get("t_peak_c")
        if isinstance(t_pre, (int, float)) and isinstance(t_cht, (int, float)):
            rise_p = t_pre - t_amb_preset_c
            rise_c = t_cht - inlet_temp_c
            dev = (rise_c - rise_p) / max(abs(rise_p), 1e-9) * 100.0
            deviation_pct = round(dev, 1)
            entry.update(
                rise_preset_c=round(rise_p, 2),
                rise_cht_c=round(rise_c, 2),
                deviation_pct=deviation_pct,
            )
            if deviation_pct > tolerance_pct:
                entry["verdict"] = "preset OPTIMISTIC (CHT runs hotter)"
                h_eff = (
                    h_preset_w_m2k * rise_p / max(rise_c, 1e-9)
                    if isinstance(h_preset_w_m2k, (int, float))
                    else None
                )
                guidance.append(
                    f"{ref}: CHT rise {rise_c:.1f} C vs preset {rise_p:.1f} C "
                    f"(+{deviation_pct:.1f} %) — the film coefficient was optimistic"
                    + (
                        f"; effective h ~= {h_eff:.1f} W/m2-K — re-run "
                        f"ecad_thermal_handoff(h_w_m2k={h_eff:.1f}) for a preset-"
                        "path study that matches the airflow"
                        if h_eff
                        else ""
                    )
                )
            elif deviation_pct < -tolerance_pct:
                entry["verdict"] = "preset conservative (CHT runs cooler)"
            else:
                entry["verdict"] = "sane (within tolerance)"
            checks.append(
                sh.check(
                    f"t_rise_{ref}",
                    "C",
                    round(rise_p, 2),
                    round(rise_c, 2),
                    abs(deviation_pct),
                    tolerance_pct,
                    abs(deviation_pct) <= tolerance_pct,
                )
            )
        table.append(entry)
    return table, checks, guidance


def register_ecad_airflow_tools(mcp) -> None:
    """Register ecad_airflow_handoff / ecad_verify_airflow on ``mcp`` (FastMCP)."""

    from services.mcp.ecad_tools import _get_pcb, _default_out_dir
    from services.mcp.ecad_common import OpError

    def _openfoam_runner():
        try:
            from services.sim.foam_runner import get_runner

            r = get_runner()
            return r if (r is not None and r.available()) else None
        except Exception:  # noqa: BLE001
            return None

    @mcp.tool()
    def ecad_airflow_handoff(
        doc_id: str = "",
        inlet_mps: float = 2.0,
        inlet_temp_c: float = 25.0,
        preset: str = "coarse",
        enclosure: Optional[Union[dict, str]] = None,
        component_kappa_w_mk: float = _COMPONENT_KAPPA_W_MK,
        dry_run: bool = False,
        path: str = "",
    ) -> str:
        """Hand the active board's thermal problem to OpenFOAM conjugate heat transfer
        (CHT) — the check for when convection PRESETS aren't enough (enclosures, fans).

        Pipeline:
        1. Reuses the thermal-handoff FEA geometry (board_to_fea_step) for footprints/powers,
           then exports the board slab + every POWERED component as separate watertight
           STL solids (world mm; components sunk 0.2 mm into the board so the regions
           couple). Components without power metadata get no solid (honesty).
        2. Air domain: open ambient by default; pass enclosure={"clearance_mm":10,
           "wall_mm":2} for a passive 4-wall duct open along the flow (+x) axis.
        3. Runs chtMultiRegionFoam via the conjugate-CFD seam (run_openfoam_tunnel thermal=
           {mode:'conjugate', solids:[...]}) with the given inlet velocity/temperature;
           per-region volumetric heat sources carry each component's power_w.
        4. Maps per-region mean/peak temperatures back to component refs.
        Then run ecad_verify_airflow() to quantify the preset-h thermal error
        ('was the film-coefficient assumption sane?').

        Without OpenFOAM (or with dry_run=True) it still returns the full case inputs
        + a VALID aicad.simhandoff/1 envelope (coupling 'ecad_to_cht') with
        skipped=true, and offers the LBM wind tunnel as a qualitative-flow ADVISORY —
        clearly labeled NON-THERMAL, never a temperature verdict.

        Budget note: even 'coarse' CHT is minutes of meshing+solving; finer presets
        grow fast.
        """
        warnings: list = []
        # SILENT-DISCARD guard: `isinstance(enclosure, dict)` below used to just skip
        # a stringified/python-repr enclosure spec — the caller's explicit
        # clearance_mm/wall_mm duct walls were silently dropped in favour of the
        # open_ambient default while the result still reported ok=True.
        if isinstance(enclosure, str) and enclosure.strip():
            coerced = coerce_json_or_repr(enclosure)
            if not isinstance(coerced, dict):
                return _err(
                    f"enclosure is not a valid JSON object: {enclosure!r}",
                    hint='Pass enclosure={"clearance_mm": 10, "wall_mm": 2} '
                    "or a JSON object string.",
                )
            enclosure = coerced
        if enclosure is not None and not isinstance(enclosure, dict):
            return _err("enclosure must be an object or null")
        try:
            inlet_mps = float(inlet_mps)
            inlet_temp_c = float(inlet_temp_c)
            component_kappa_w_mk = float(component_kappa_w_mk)
        except (TypeError, ValueError):
            return _err(
                "inlet speed, inlet temperature, and component conductivity "
                "must be numeric"
            )
        if not math.isfinite(inlet_mps) or inlet_mps <= 0.0:
            return _err("inlet_mps must be finite and > 0 m/s")
        if not math.isfinite(inlet_temp_c) or inlet_temp_c < -273.15:
            return _err("inlet_temp_c must be finite and >= -273.15 C")
        if not math.isfinite(component_kappa_w_mk) or component_kappa_w_mk <= 0.0:
            return _err("component_kappa_w_mk must be finite and > 0 W/m-K")
        if preset not in ("coarse", "medium", "fine", "xfine"):
            return _err("preset must be one of coarse, medium, fine, or xfine")
        try:
            session = _get_pcb(doc_id or None)
        except OpError as e:
            return _err(
                e.message,
                hint="airflow handoff needs an open routed board.",
                next_actions=["ecad_new_pcb('myboard')", "ecad_make_board(...)"],
            )
        board_doc = session.doc

        out_dir = path or str(
            _default_out_dir(board_doc.get("name", "board")) / "airflow"
        )
        try:
            from services.ecad.threed import (
                board_to_fea_step,
                BoardGeometryError,
            )  # STAYS: no C++ counterpart
        except ImportError as e:
            return _err(
                f"3D kernel unavailable (ImportError: {e})",
                hint="OCP/build123d not installed — board geometry cannot be built.",
                next_actions=["pip install build123d"],
                available=False,
            )
        try:
            built = board_to_fea_step(
                board_doc, os.path.join(out_dir, "board_ref.step")
            )
        except BoardGeometryError as e:
            return _err(
                f"board geometry failed: {e}",
                hint="the board needs a closed Edge.Cuts outline.",
                next_actions=["ecad_run_drc()", "ecad_status()"],
            )
        except Exception as e:  # noqa: BLE001
            return _err(f"board geometry build failed: {type(e).__name__}: {e}")

        try:
            built_summary = built.get("summary")
            if not isinstance(built_summary, dict) or not isinstance(
                built_summary.get("warnings", []), list
            ):
                raise ValueError("FEA builder returned malformed summary/warnings")
            warnings.extend(built_summary.get("warnings", []))
            case = build_airflow_solids(
                built["board"],
                built["material"],
                built["components"],
                out_dir,
                component_kappa_w_mk=component_kappa_w_mk,
            )
            assumptions = list(case["assumptions"])
            solids = list(case["solids"])
            air_domain = {"mode": "open_ambient"}
            if isinstance(enclosure, dict):
                walls, encl_assum = build_enclosure_walls(
                    built["board"], enclosure, out_dir
                )
                solids += walls
                assumptions += encl_assum
                air_domain = {
                    "mode": "enclosure_duct",
                    **{
                        key: float(enclosure.get(key, default))
                        for key, default in (
                            ("clearance_mm", 10.0),
                            ("wall_mm", 2.0),
                            ("kappa_w_mk", 0.2),
                        )
                    },
                }
        except (KeyError, TypeError, ValueError) as exc:
            return _err(f"invalid airflow thermal setup: {exc}")
        if not case["sources"]:
            return _err(
                "no mapped component carries positive power metadata — nothing to heat",
                hint="seed power_w on at least one component with FEA geometry.",
                next_actions=["ecad_status()"],
            )
        if case["unmapped_refs"]:
            warnings.append(
                "powered components without airflow geometry were omitted: "
                + ", ".join(case["unmapped_refs"])
            )

        conditions = list(case["conditions"]) + [
            sh.condition(
                "inlet_velocity",
                "inlet_velocity",
                inlet_mps,
                "m/s",
                "caller inlet_mps (fan/ambient flow)",
                applied_to="domain inlet",
            ),
            sh.condition(
                "inlet_temperature",
                "inlet_temperature",
                inlet_temp_c,
                "C",
                "caller inlet_temp_c",
                applied_to="domain inlet",
            ),
        ]

        # Expectations from the preset-h thermal solve, when one is in session.
        pre = _THERMAL.get("last_handoff")
        expectations = []
        preset_env: dict = {}
        if isinstance(pre, dict) and pre.get("solved"):
            preset_env = pre.get("environment") or {}
            for r in pre.get("components") or []:
                if isinstance(r.get("t_case_c"), (int, float)):
                    expectations.append(
                        sh.expectation(
                            f"t_case_{r['ref']}_c",
                            r["t_case_c"],
                            "C",
                            f"ecad_thermal_handoff preset-h solve "
                            f"(h={preset_env.get('h_w_m2k')} W/m2-K)",
                        )
                    )

        envelope = sh.build_handoff(
            "ecad_to_cht",
            sh.study_ref("ecad", "ecad_airflow_handoff", doc_id=session.doc_id),
            sh.study_ref("cfd_cht", "run_openfoam_tunnel"),
            geometry=[
                sh.geometry_ref(
                    "board"
                    if s["name"] == "board"
                    else (
                        "enclosure_wall"
                        if s["name"].startswith("encl_")
                        else "component_body"
                    ),
                    "stl",
                    path=s["stl_path"],
                    derived_by="services.mcp.ecad_thermal.build_airflow_solids",
                    meta={
                        "region": s["name"],
                        "kappa_w_mk": s["kappa_w_mk"],
                        "power_w": s["power_w"],
                    },
                )
                for s in solids
            ],
            conditions=conditions,
            expectations=expectations or None,
            warnings=warnings or None,
            meta={
                "air_domain": air_domain,
                "preset": preset,
                "assumptions": assumptions,
                "coupling_note": "new id ecad_to_cht: the CHT study is built from "
                "the ECAD board directly; the preset-h FEA result "
                "is only an expectation, not a BC source",
            },
        )

        base = {
            "ok": True,
            "doc_id": session.doc_id,
            "air_domain": air_domain,
            "solids": [
                {k: s[k] for k in ("name", "stl_path", "kappa_w_mk", "power_w")}
                for s in solids
            ],
            "sources": case["sources"],
            "coverage": {
                "n_components": len(built["components"]),
                "n_heat_sources": len(case["sources"]),
                "unpowered_refs": case["unpowered_refs"],
                "unmapped_refs": case["unmapped_refs"],
            },
            "inlet": {"speed_mps": inlet_mps, "temp_c": inlet_temp_c},
            "preset": preset,
            "assumptions": assumptions,
            "warnings": warnings,
        }

        runner_ok = _openfoam_runner() is not None
        lbm_advisory = (
            "run_cfd_flow(direction=[1,0,0], speed_mps=%g)  # ADVISORY "
            "LBM flow render — qualitative airflow ONLY, NON-THERMAL, "
            "never a temperature verdict" % float(inlet_mps)
        )
        if dry_run:
            _AIRFLOW["last"] = None
            return _dumps(
                sh.attach(
                    {
                        **base,
                        "solved": False,
                        "skipped": True,
                        "skip_reason": "dry run requested",
                        "openfoam_available": runner_ok,
                        "hint": "case inputs + envelope built; CHT solve skipped by request.",
                        "next_actions": (
                            ["ecad_airflow_handoff(dry_run=False)"]
                            if runner_ok
                            else [lbm_advisory]
                        ),
                    },
                    envelope,
                )
            )
        if not runner_ok:
            _AIRFLOW["last"] = None
            reason = (
                "OpenFOAM not available (provision with "
                "tools/provision_openfoam.py or install the conda-forge "
                "'openfoam' package)"
            )
            return _dumps(
                sh.attach(
                    {
                        **base,
                        "ok": False,
                        "solved": False,
                        "skipped": True,
                        "available": False,
                        "error": reason,
                        "skip_reason": reason,
                        "openfoam_available": False,
                        "hint": f"case inputs + envelope built; CHT solve unavailable: {reason}.",
                        "next_actions": [lbm_advisory],
                    },
                    envelope,
                )
            )

        # ── live CHT via the conjugate-CFD seam ─────────────────────────────────
        try:
            from services.sim.openfoam import run_openfoam_tunnel

            res = run_openfoam_tunnel(
                None,
                direction=(1.0, 0.0, 0.0),
                speed_mps=float(inlet_mps),
                preset=preset,
                thermal={
                    "enabled": True,
                    "mode": "conjugate",
                    "inlet_temp_c": float(inlet_temp_c),
                    "solids": solids,
                },
            )
        except Exception as e:  # noqa: BLE001
            _AIRFLOW["last"] = None
            error = f"CHT run failed: {type(e).__name__}: {e}"
            return _dumps(
                sh.attach(
                    {
                        **base,
                        "ok": False,
                        "solved": False,
                        "skipped": False,
                        "available": True,
                        "error": error,
                        "skip_reason": error,
                        "openfoam_available": True,
                        "hint": "case inputs + envelope are valid; the OpenFOAM run failed.",
                        "next_actions": [
                            lbm_advisory,
                            "set CHUDWARE_FOAM_KEEP=1 and retry to inspect the case",
                        ],
                    },
                    envelope,
                )
            )

        try:
            solver_notes = list(res.solver_notes or [])
            reynolds = float(res.reynolds)
            if not math.isfinite(reynolds) or reynolds < 0.0:
                raise ValueError("Reynolds number must be finite and non-negative")
            region_temps = parse_region_temps(solver_notes)
            rows = []
            missing = []
            for src in case["sources"]:
                rt = region_temps.get(src["region"]) or {}
                mapped = (
                    isinstance(rt.get("mean_c"), (int, float))
                    and math.isfinite(float(rt["mean_c"]))
                    and isinstance(rt.get("peak_c"), (int, float))
                    and math.isfinite(float(rt["peak_c"]))
                )
                if not mapped:
                    missing.append(src["ref"])
                rows.append(
                    {
                        "ref": src["ref"],
                        "region": src["region"],
                        "power_w": src["power_w"],
                        "t_mean_c": rt.get("mean_c"),
                        "t_peak_c": rt.get("peak_c"),
                        "mapped": mapped,
                    }
                )
        except (AttributeError, TypeError, ValueError) as exc:
            _AIRFLOW["last"] = None
            return _dumps(
                sh.attach(
                    {
                        **base,
                        "ok": False,
                        "solved": False,
                        "skipped": False,
                        "available": True,
                        "error": f"invalid CHT solver result: {exc}",
                    },
                    envelope,
                )
            )
        board_rt = region_temps.get("board") or {}
        map_back_complete = not missing
        result = sh.attach(
            {
                **base,
                "ok": map_back_complete,
                "solved": True,
                "skipped": False,
                "available": True,
                "map_back_complete": map_back_complete,
                "unmapped_result_refs": missing,
                "solver": "OpenFOAM chtMultiRegionFoam (conjugate solid+fluid)",
                "reynolds": reynolds,
                "board_t_mean_c": board_rt.get("mean_c"),
                "board_t_peak_c": board_rt.get("peak_c"),
                "components": rows,
                "region_temps": region_temps,
                "overlay_path": getattr(res, "overlay_path", None),
                "solver_notes": solver_notes,
                "hint": (
                    (getattr(res, "reynolds_note", None) or "CHT solved")
                    + (
                        " Run ecad_verify_airflow() to check the preset-h assumption."
                        if map_back_complete
                        else f" Temperature map-back missing for: {', '.join(missing)}."
                    )
                ),
                "next_actions": (
                    ["ecad_verify_airflow()"]
                    if map_back_complete
                    else ["inspect CHT region naming/postprocessing and re-run"]
                ),
            },
            envelope,
        )
        if not map_back_complete:
            result["error"] = (
                "CHT solve completed but powered-component map-back was incomplete: "
                + ", ".join(missing)
            )
        _AIRFLOW["last"] = result if map_back_complete else None
        return _dumps(result)

    @mcp.tool()
    def ecad_verify_airflow(
        airflow_result: Union[str, dict, None] = None,
        preset_result: Union[str, dict, None] = None,
        tolerance_pct: float = 40.0,
    ) -> str:
        """Was the thermal handoff's film-coefficient assumption sane? Compare the preset-h FEA
        component temperatures against the CHT (airflow) solve, per component.

        Deviation is measured on the temperature RISE above each study's own ambient.
        Default tolerance 40 %: a convection preset is an engineering nominal, not a
        flow solve — the check flags assumption-breaking environments (enclosed board,
        starved fan), not solver noise. Guidance is emitted whenever the preset was
        OPTIMISTIC (CHT hotter), including an effective-h to re-run the preset path
        with. Uses the last solved ecad_airflow_handoff / ecad_thermal_handoff in the
        session unless results are passed explicitly.
        """

        def _load(v, fallback):
            if isinstance(v, str) and v.strip():
                # LIVE CRASH class: a python-repr result string would json.loads-fail
                # even though it IS the dict it looks like — coerce first.
                coerced = coerce_json_or_repr(v)
                if isinstance(coerced, dict):
                    return coerced
                try:
                    return json.loads(v)
                except ValueError:
                    return None
            return v if isinstance(v, dict) else fallback

        air = _load(airflow_result, _AIRFLOW.get("last"))
        pre = _load(preset_result, _THERMAL.get("last_handoff"))
        if (
            not isinstance(air, dict)
            or not air.get("ok")
            or not air.get("solved")
            or air.get("map_back_complete") is False
        ):
            return _err(
                "no complete solved ecad_airflow_handoff result available",
                hint="run ecad_airflow_handoff() (with OpenFOAM available) "
                "first, or pass its complete result.",
                next_actions=["ecad_airflow_handoff()"],
            )
        if (
            not isinstance(pre, dict)
            or not pre.get("ok")
            or not pre.get("solved")
            or pre.get("map_back_complete") is False
        ):
            return _err(
                "no complete solved ecad_thermal_handoff (preset-h) result "
                "to verify against",
                hint="run ecad_thermal_handoff() first — the verify quantifies "
                "its film-coefficient error.",
                next_actions=["ecad_thermal_handoff()"],
            )

        try:
            env = pre.get("environment") or {}
            t_amb = float(env["t_amb_c"])
            h_pre = float(env["h_w_m2k"])
            inlet_c = float((air.get("inlet") or {})["temp_c"])
            tolerance = float(tolerance_pct)
            if (
                not all(math.isfinite(v) for v in (t_amb, h_pre, inlet_c, tolerance))
                or t_amb < -273.15
                or inlet_c < -273.15
                or h_pre <= 0.0
                or tolerance < 0.0
            ):
                raise ValueError
            table, checks, guidance = airflow_deviation_table(
                pre.get("components") or [],
                air.get("components") or [],
                t_amb,
                inlet_c,
                h_pre,
                tolerance,
            )
        except (KeyError, TypeError, ValueError):
            return _err(
                "airflow verification temperatures, h, and tolerance must be "
                "finite; h > 0 and tolerance_pct >= 0"
            )
        if not checks:
            return _err(
                "no component appears with finite temperatures in both solves "
                "— nothing to compare",
                hint="both complete studies must run on the same board/session.",
            )

        optimistic = [g for g in guidance]
        unchecked = [row["ref"] for row in table if row["deviation_pct"] is None]
        all_pass = not unchecked and all(c["pass"] for c in checks)
        verdict = (
            f"{'SANE' if all_pass else 'INCOMPLETE/NOT SANE'}: preset h="
            f"{h_pre} W/m2-K vs CHT — {sum(1 for c in checks if c['pass'])}/"
            f"{len(checks)} component rise(s) within {tolerance:.0f} %"
            + (f"; {len(unchecked)} component(s) unchecked" if unchecked else "")
            + (
                ""
                if not optimistic
                else f"; preset OPTIMISTIC for {len(optimistic)} component(s)"
            )
        )
        result: dict = {
            "ok": True,
            "pass": all_pass,
            "verdict": verdict,
            "preset_env": {"h_w_m2k": h_pre, "t_amb_c": t_amb},
            "inlet_temp_c": inlet_c,
            "table": table,
            "guidance": guidance,
            "unchecked_refs": unchecked,
            "warnings": [
                "CHT peak (case) temp vs preset max nodal temp — both are "
                "case-level; junction detail is out of scope for either",
                "region temps carry integer-degree resolution (parsed from "
                "the CHT per-region notes)",
            ],
            "hint": verdict
            + (" — see guidance for the effective-h re-run." if guidance else ""),
            "next_actions": (
                [g.split("re-run ")[-1] for g in guidance if "re-run " in g][:1]
                or [
                    "margins look consistent — preset path is trustworthy "
                    "for this environment"
                ]
            ),
        }
        sh.attach(
            result,
            sh.build_verify(
                "ecad_to_cht",
                sh.study_ref(
                    "thermal_fea", "ecad_thermal_handoff", doc_id=pre.get("doc_id")
                ),
                sh.study_ref(
                    "cfd_cht", "ecad_airflow_handoff", doc_id=air.get("doc_id")
                ),
                checks,
                verdict,
                handoff_id=(air.get("simhandoff") or {}).get("id"),
                advisories=guidance or None,
                warnings=result["warnings"],
            ),
        )
        return _dumps(result)
