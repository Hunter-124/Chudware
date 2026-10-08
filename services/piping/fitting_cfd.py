"""CFD-derived fitting loss curves shared by the GUI worker and MCP tools.

Geometry setup uses the live engine only for property lookup and document export.
Each solve runs in services.sim.jobs' isolated core_rpc child: killing a job stops
its solver, never the user's daemon. The services worker therefore needs no local
engine document table. Native calls from its background thread use core_bridge's
ordinary daemon connection; status/cancel never make native calls on the worker lane.
"""
from __future__ import annotations

import copy
import math
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from services.piping import catalog, cfd_handoff
from services.sim import jobs


def _err(reason: str, hint: str) -> dict:
    return {"ok": False, "reason": reason, "hint": hint}


def _positive(value, label: str) -> float:
    n = float(value)
    if not math.isfinite(n) or n <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return n


def validate_request(fitting: dict, quality: str = "quick", geometry: dict | None = None,
                     reynolds: list | None = None) -> dict | None:
    if not isinstance(fitting, dict) or not fitting.get("type"):
        return _err("invalid_params", "fitting must contain a type and optional params/nps")
    if quality not in ("quick", "standard"):
        return _err("invalid_params", "quality must be quick or standard")
    if geometry is None and fitting["type"] != "elbow":
        kind = fitting["type"]
        return _err("unsupported_fitting_geometry", f"{kind} has no supported bore extraction. "
                    "Use piping_fitting_k's correlation (Crane for valves), or supply geometry "
                    "with stl_path or doc_id, inlet, outlet, and inner_dia_mm.")
    if geometry is not None:
        if not isinstance(geometry, dict):
            return _err("invalid_params", "geometry must be an object")
        if bool(geometry.get("stl_path")) == bool(geometry.get("doc_id")):
            return _err("invalid_params", "geometry needs exactly one of stl_path or doc_id")
        try:
            _positive(geometry.get("inner_dia_mm"), "geometry.inner_dia_mm")
            for side in ("inlet", "outlet"):
                plane = geometry.get(side) or {}
                for key in ("point", "normal"):
                    v = plane.get(key)
                    if not isinstance(v, (list, tuple)) or len(v) != 3 or not all(
                            math.isfinite(float(x)) for x in v):
                        raise ValueError(f"geometry.{side}.{key} must be a finite 3-vector")
                if sum(float(x) ** 2 for x in plane["normal"]) <= 1e-24:
                    raise ValueError(f"geometry.{side}.normal must be nonzero")
        except (ValueError, TypeError) as exc:
            return _err("invalid_params", str(exc))
    if reynolds is not None:
        try:
            if not isinstance(reynolds, list) or not reynolds:
                raise ValueError("reynolds must be a nonempty list")
            values = [_positive(x, "Reynolds number") for x in reynolds]
            if len(set(values)) != len(values):
                raise ValueError("Reynolds points must be distinct")
        except (ValueError, TypeError) as exc:
            return _err("invalid_params", str(exc))
    return None


def fit_two_k(points: list[dict], inner_dia_mm: float) -> dict:
    """Nonnegative least squares Hooper 2-K, scaled for numerical stability."""
    diameter_in = _positive(inner_dia_mm, "inner diameter") / 25.4
    if len(points) < 2:
        raise ValueError("2-K fitting requires at least two Reynolds points")
    scale = min(_positive(p["re"], "Reynolds number") for p in points)
    xs = [scale / p["re"] for p in points]
    ys = [float(p["K"]) for p in points]
    xm, ym = sum(xs) / len(xs), sum(ys) / len(ys)
    variance = sum((x - xm) ** 2 for x in xs)
    if variance <= 1e-24:
        raise ValueError("2-K fitting requires distinct Reynolds points")
    slope = sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / variance
    intercept = ym - slope * xm
    if slope < 0:
        slope, intercept = 0.0, max(0.0, ym)
    elif intercept < 0:
        slope = max(0.0, sum(x * y for x, y in zip(xs, ys)) / sum(x * x for x in xs))
        intercept = 0.0
    return {"K1": slope * scale, "Kinf": intercept / (1 + 1 / diameter_in)}


def make_k_model(points: list[dict], inner_dia_mm: float, provenance: dict) -> dict:
    table = [{"re": float(p["re"]), "K": float(p["K"])}
             for p in sorted(points, key=lambda p: p["re"])]
    model = {"kind": "table", "points": table, "source": "cfd", "provenance": provenance}
    if len(table) >= 2:
        model["fit"] = fit_two_k(table, inner_dia_mm)
    return model


def elbow_test_section(fitting: dict, schedule: str) -> tuple[dict, dict, dict, float]:
    """Generate a single bent run using the same ID, OD and sweep as internal_volume."""
    spec = catalog.nearest_pipe(fitting.get("nps", 2), schedule)
    if isinstance(spec, dict):
        raise ValueError(spec.get("hint") or spec.get("reason"))
    params = fitting.get("params") or {}
    angle = float(params.get("angle", 90))
    if not math.isfinite(angle) or not 0 < angle < 180:
        raise ValueError("elbow angle must be between 0 and 180 degrees (exclusive)")
    radius = params.get("radius_mm")
    if radius is None:
        kind = params.get("radius", "LR")
        if kind not in ("LR", "SR"):
            raise ValueError("params.radius must be LR or SR; use radius_mm for an explicit radius")
        radius = spec.outer_dia * (1 if kind == "SR" else 1.5)
    radius = _positive(radius, "bend radius")
    if radius <= spec.inner_dia / 2:
        raise ValueError("bend radius must exceed half the bore diameter")
    theta = math.radians(angle)
    takeout = radius * math.tan(theta / 2)
    # One diameter of straight pipe on each side; native piping/run_id extends to 5D.
    leg = takeout + spec.inner_dia
    path = [[-leg, 0, 0], [0, 0, 0], [leg * math.cos(theta), leg * math.sin(theta), 0]]
    run = {"id": "k-test-run", "name": "Fitting test section", "nps": spec.nps,
           "schedule": schedule, "material": "A106-B", "bend_radius": radius,
           "end_type": "BW", "path": path}
    ft = copy.deepcopy(fitting)
    ft.update({"id": "k-test-fitting", "run_id": run["id"], "nps": spec.nps,
               "origin": [0, 0, 0]})
    ft.setdefault("params", {}).pop("k_model", None)
    doc = {"schema": "aicad.piping/1", "id": "k-test", "name": "Fitting CFD test",
           "units": {"system": "MMGS"}, "material": "A106-B", "runs": [run], "fittings": [ft]}
    inlet = {"point": path[0], "normal": [1, 0, 0]}
    outlet = {"point": path[-1], "normal": [-math.cos(theta), -math.sin(theta), 0]}
    return doc, inlet, outlet, spec.inner_dia


class Cancelled(Exception):
    pass


def compute(fitting: dict, *, schedule: str = "40", fluid=None, reynolds: list | None = None,
            quality: str = "quick", geometry: dict | None = None,
            rpc: Callable | None = None, progress: Callable | None = None,
            cancel_event: threading.Event | None = None) -> dict:
    """Extract bore once, solve each Re in a cancellable durable child, return K(Re)."""
    refusal = validate_request(fitting, quality, geometry, reynolds)
    if refusal:
        return refusal
    if rpc is None:
        from services.mcp.core_bridge import call
        rpc = call
    cancel_event = cancel_event or threading.Event()
    progress = progress or (lambda pct, stage: None)
    started = datetime.now(timezone.utc).isoformat()
    start_clock = time.monotonic()
    points, warnings = [], []
    active_job = None
    try:
        progress(0, "Resolving fluid and bore")
        fl = fluid or "water"
        if isinstance(fl, dict) and ("rho" in fl or "mu" in fl) and (
                fl.get("rho") is None or fl.get("mu") is None):
            raise ValueError("fluid.rho and fluid.mu must be supplied together")
        if isinstance(fl, dict) and fl.get("rho") is not None and fl.get("mu") is not None:
            rho, mu = _positive(fl["rho"], "density"), _positive(fl["mu"], "viscosity")
        else:
            props = rpc("piping_fluid", {"name": fl.get("name", "water") if isinstance(fl, dict) else fl,
                                         "T": fl.get("T", 293.15) if isinstance(fl, dict) else 293.15,
                                         "P": fl.get("P", 101325) if isinstance(fl, dict) else 101325})
            if not props.get("ok"):
                return props
            rho, mu = _positive(props["rho"], "density"), _positive(props["mu"], "viscosity")
        res = sorted(float(x) for x in (reynolds or ([1e4, 1e5] if quality == "quick" else [1e4, 1e5, 1e6])))
        cells = 12 if quality == "quick" else 16
        with tempfile.TemporaryDirectory(prefix="fitting_k_") as directory:
            payload = {"method": "sim_run_pipe_flow", "progress_file": jobs.PROGRESS_TOKEN}
            params = {"density_kgm3": rho, "viscosity_pas": mu, "cells_across": cells,
                      "roughness_mm": 0.0, "progress_path": jobs.PROGRESS_TOKEN}
            if geometry is None:
                doc, inlet, outlet, diameter = elbow_test_section(fitting, schedule)
                step = os.path.join(directory, "bore.step")
                volume = cfd_handoff.internal_volume(doc, step, run_id="k-test-run")
                if not volume.get("ok"):
                    return volume
                warnings.extend(volume.get("warnings") or [])
                # A spline fallback is not the requested fitting and cannot calibrate its K.
                if warnings:
                    return _err("bore_extraction_failed", "; ".join(map(str, warnings)))
                payload["open_step"] = step
                params.update({"piping": doc, "run_id": "k-test-run"})
            else:
                inlet, outlet = geometry["inlet"], geometry["outlet"]
                diameter = float(geometry["inner_dia_mm"])
                if geometry.get("stl_path"):
                    params["stl_path"] = os.path.abspath(os.path.expanduser(geometry["stl_path"]))
                else:
                    step = os.path.join(directory, "bore.step")
                    exported = rpc("doc_export_step", {"doc_id": geometry["doc_id"], "path": step})
                    if exported.get("ok") is False:
                        return exported
                    payload["open_step"] = step
                    if geometry.get("body"):
                        params["body"] = geometry["body"]
            params.update({"inlet": inlet, "outlet": outlet})
            for index, re in enumerate(res):
                if cancel_event.is_set():
                    raise Cancelled()
                velocity = re * mu / (rho * diameter / 1000)
                payload["params"] = {**params, "mean_velocity_ms": velocity}
                active_job = jobs.start("piping_fitting_k_point", "core_rpc", payload)["job_id"]
                while True:
                    if cancel_event.is_set():
                        jobs.cancel(active_job)
                        active_job = None
                        raise Cancelled()
                    status = jobs.status(active_job)
                    pct = min(100, max(0, float((status.get("progress") or {}).get("pct", 0))))
                    progress((index + pct / 100) * 100 / len(res), f"Re {re:g}: solving point {index + 1}/{len(res)}")
                    if status["state"] in jobs.TERMINAL:
                        break
                    cancel_event.wait(0.2)
                raw = jobs.raw_result(active_job)
                failed = jobs.refusal_result(active_job)
                active_job = None
                if raw is None:
                    return failed or _err("cfd_failed", status.get("error") or "Internal-flow job failed or was cancelled")
                if not raw.get("converged"):
                    return _err("cfd_not_converged", f"Re {re:g} did not converge; no K model was applied")
                breakdown = raw.get("one_d_breakdown") or {}
                if geometry is None and "friction_pa" not in breakdown:
                    return _err("one_d_unavailable", "Native solve returned no straight-friction breakdown")
                friction = breakdown["friction_pa"] if geometry is None else raw["darcy_estimate_pa"]
                dynamic = breakdown.get("dynamic_pressure_pa", 0.5 * rho * velocity ** 2)
                dynamic = _positive(dynamic, "dynamic pressure")
                k = (float(raw["dp_pa"]) - float(friction)) / dynamic
                if not math.isfinite(k) or k < 0:
                    return _err("invalid_cfd_k", f"Re {re:g}: CFD pressure loss minus friction gives K={k}; refine the geometry/resolution")
                points.append({"re": re, "K": k, "dp_pa": raw["dp_pa"], "velocity_ms": velocity,
                               "converged": True, "steps": raw.get("steps"),
                               "provenance": raw.get("provenance") or {}})
                warnings.extend(raw.get("warnings") or [])
            provenance = {"engine": "cpp-lbm-internal", "cells_across": cells,
                          "steps": [p["steps"] for p in points], "converged": True,
                          "quality": quality, "started_at": started,
                          "completed_at": datetime.now(timezone.utc).isoformat(),
                          "wall_time_s": time.monotonic() - start_clock,
                          "inner_dia_mm": diameter, "fluid": {"rho": rho, "mu": mu},
                          "point_provenance": [p["provenance"] for p in points]}
            provenance["fit_method"] = "nonnegative_least_squares_hooper_2k"
            model = make_k_model(points, diameter, provenance)
            out = {"ok": True, "k_model": model, "points": points, "warnings": warnings}
            reference = copy.deepcopy(fitting)
            reference.setdefault("params", {}).pop("k_model", None)
            try:
                corr = rpc("piping_fitting_k", {"fitting": reference, "schedule": schedule,
                                              "reynolds": res[-1], "fluid": {"rho": rho, "mu": mu}})
            except Exception as exc:
                corr = _err("correlation_unavailable", str(exc))
            if corr.get("ok") and corr.get("K") is not None:
                out["correlation_K"] = corr["K"]
                out["correlation_reynolds"] = res[-1]
                if float(corr["K"]) != 0:
                    out["deviation_pct"] = 100 * (points[-1]["K"] / float(corr["K"]) - 1)
                out["correlation_method"] = corr.get("method")
            else:
                out["correlation"] = corr
                warnings.append({"code": "correlation_unavailable", "hint": corr.get("hint") or corr.get("reason")})
            progress(100, "CFD loss curve ready")
            return out
    except Cancelled:
        return _err("cancelled", "CFD fitting calibration cancelled")
    except cfd_handoff.PipingCFDError as exc:
        return _err("bore_extraction_unavailable", str(exc))
    except (ValueError, TypeError, KeyError, OSError) as exc:
        return _err("invalid_setup", str(exc))
    except Exception as exc:
        return _err("engine_unavailable", str(exc))
    finally:
        if active_job:
            jobs.cancel(active_job)


def apply_model(piping: dict, fitting_id: str, k_model: dict, rpc: Callable) -> dict:
    """Atomic native mutation; GUI and agents use exactly the same fitting edit path."""
    return rpc("piping_edit", {"piping": piping, "ops": [{"op": "update_fitting",
               "id": fitting_id, "params": {"k_model": k_model}}]})
