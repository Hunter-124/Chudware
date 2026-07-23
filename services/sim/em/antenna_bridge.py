"""EM bridge for PCB antennas — analytic resonance always + openEMS S11 when present.

Given a parametric antenna design (from :mod:`engine_oracle.ecad.antenna`) or an
already-emitted geometry dict, build:

1. **Analytic estimate (always)** — closed-form resonant frequency + rough
   fractional bandwidth / return-loss floor from the design equations.
2. **Full-wave S11 (when openEMS is importable)** — a minimal FDTD model of the
   radiator (rectangular patch as a metal box on a dielectric slab, or a wire /
   strip approximation for monopole/IFA) driven by a lumped port. When openEMS
   is absent the full-wave block is an **honest skip**
   ``{skipped: true, reason: ...}`` — never fabricated numbers.

License containment: openEMS / CSXCAD are imported **lazily** inside the
full-wave path only (same rule as :mod:`engine_oracle.sim.em.openems_adapter`).
"""
from __future__ import annotations

import math
import os
import tempfile
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from ...sim.em.analytic import C0

# Re-export for callers that want the vacuum constant from this module.
__all__ = [
    "openems_available",
    "analytic_resonance",
    "fullwave_s11",
    "analyze_antenna",
]


def openems_available() -> dict:
    """Detect openEMS + CSXCAD without raising.

    Returns ``{installed: bool, version: str|None, detail: str}``.
    """
    try:
        import openEMS  # noqa: F401  (lazy, GPL containment)
        import CSXCAD   # noqa: F401
        ver = getattr(openEMS, "__version__", None)
        return {
            "installed": True,
            "version": ver,
            "detail": "openEMS + CSXCAD python modules importable",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "installed": False,
            "version": None,
            "detail": f"import failed: {exc}",
        }


# --------------------------------------------------------------------------- analytic


def _as_dimensions(spec: Union[dict, Any]) -> dict:
    """Normalise a design / dimensions / geometry payload into a dimensions dict."""
    if not isinstance(spec, dict):
        raise TypeError("antenna spec must be a dict")
    if "dimensions" in spec and isinstance(spec["dimensions"], dict):
        return dict(spec["dimensions"])
    if "kind" in spec and ("freq_hz" in spec or "length_mm" in spec or "width_mm" in spec
                           or "total_length_mm" in spec):
        return dict(spec)
    if "geometry" in spec and callable(spec.get("geometry")):
        # design_antenna result without pulling dimensions key? already handled above
        dims = spec.get("dimensions")
        if isinstance(dims, dict):
            return dict(dims)
    raise ValueError(
        "need a design dict with 'dimensions', or a flat dimensions dict "
        "(kind, freq_hz, …)")


def analytic_resonance(spec: Union[dict, Any]) -> dict:
    """Closed-form resonance + bandwidth estimate (always available).

    Uses the design-time target frequency as the resonant estimate and the
    textbook bandwidth fraction stored on the dimensions (or a kind default).
    Also recomputes a cross-check resonant frequency from the stored geometry
    sizes when possible:

      * patch: f = c / (2 (L+2ΔL) √εeff)
      * monopole: f = vf · c / (4 L)
      * ifa/pifa: f = c / (4 L_total √εeff)
    """
    d = _as_dimensions(spec)
    kind = str(d.get("kind") or "unknown").lower()
    f_target = float(d.get("freq_hz") or 0.0)
    er = float(d.get("substrate_er") or 4.4)
    eeff = float(d.get("eeff") or d.get("feed", {}).get("eeff") or ((er + 1.0) / 2.0))
    bw_frac = float(d.get("bandwidth_frac") or 0.05)

    f_est = f_target
    method = "design-target"
    if kind == "patch" and d.get("length_mm") is not None:
        l_m = float(d["length_mm"]) * 1e-3
        dl_m = float(d.get("delta_l_mm") or 0.0) * 1e-3
        # Balanis 14-7 inverted: f = c / (2 (L+2ΔL) √εeff)
        denom = 2.0 * (l_m + 2.0 * dl_m) * math.sqrt(max(eeff, 1.0))
        if denom > 0:
            f_est = C0 / denom
            method = "patch-Balanis-14.2"
    elif kind == "monopole" and d.get("length_mm") is not None:
        l_m = float(d["length_mm"]) * 1e-3
        vf = float(d.get("velocity_factor") or (1.0 / math.sqrt(max(eeff, 1.0))))
        if l_m > 0:
            f_est = vf * C0 / (4.0 * l_m)
            method = "monopole-quarter-wave"
    elif kind in ("ifa", "pifa") and d.get("total_length_mm") is not None:
        l_m = float(d["total_length_mm"]) * 1e-3
        if l_m > 0:
            f_est = C0 / (4.0 * l_m * math.sqrt(max(eeff, 1.0)))
            method = "ifa-guided-quarter-wave"

    # Rough match quality: thin antennas on FR-4 typically reach ~-10 dB at band
    # centre when sized correctly; report a conservative analytic floor.
    s11_floor_db = -12.0 if kind == "patch" else -10.0
    bw_hz = bw_frac * f_est

    return {
        "ok": True,
        "kind": kind,
        "method": method,
        "f_res_hz": float(f_est),
        "f_res_ghz": round(f_est / 1e9, 6),
        "f_target_hz": f_target,
        "bandwidth_frac": bw_frac,
        "bandwidth_hz": float(bw_hz),
        "bandwidth_mhz": round(bw_hz / 1e6, 3),
        "s11_est_db": s11_floor_db,
        "eeff": eeff,
        "substrate_er": er,
        "note": ("analytic closed-form estimate from design equations; "
                 "not a full-wave solve"),
    }


# --------------------------------------------------------------------------- full-wave openEMS


def _default_freq_sweep(f_res_hz: float, n_freq: int = 51) -> dict:
    f0 = max(float(f_res_hz), 1e6)
    return {
        "f_start_hz": f0 * 0.7,
        "f_stop_hz": f0 * 1.3,
        "n_freq": int(n_freq),
        "f0_hz": f0,
        "fc_hz": f0 * 0.3,  # gaussian half-bandwidth for openEMS SetGaussExcite
    }


def fullwave_s11(
    spec: Union[dict, Any],
    *,
    geometry: Optional[dict] = None,
    n_freq: int = 51,
    max_timesteps: int = 40_000,
    end_criteria: float = 1e-3,
    force_skip: bool = False,
    _execute: Optional[Callable] = None,
) -> dict:
    """Full-wave S11 via openEMS when installed; honest skip otherwise.

    Parameters
    ----------
    spec:
        Antenna design or dimensions dict (used for kind / sizes / target f).
    geometry:
        Optional pre-built geometry dict; if omitted and ``spec`` has a
        ``geometry`` callable it is invoked at the origin.
    force_skip:
        When True, behave as if openEMS is absent (for tests).
    _execute:
        Optional injectable runner ``(plan) -> raw`` for unit tests; default
        builds CSXCAD geometry and calls openEMS.
    """
    avail = openems_available()
    if force_skip or not avail.get("installed"):
        return {
            "ok": False,
            "skipped": True,
            "reason": ("openEMS not installed — full-wave antenna S11 unavailable; "
                       "analytic resonance is still returned by analyze_antenna. "
                       + str(avail.get("detail") or "")),
            "solver": "openems",
            "available": avail,
        }

    dims = _as_dimensions(spec)
    kind = str(dims.get("kind") or "patch").lower()
    f_res = float(dims.get("freq_hz") or 2.45e9)
    sweep = _default_freq_sweep(f_res, n_freq=n_freq)

    if geometry is None:
        geom_fn = spec.get("geometry") if isinstance(spec, dict) else None
        if callable(geom_fn):
            geometry = geom_fn((0.0, 0.0))
        else:
            geometry = None

    plan = {
        "kind": kind,
        "dims": dims,
        "geometry": geometry,
        "sweep": sweep,
        "max_timesteps": int(max_timesteps),
        "end_criteria": float(end_criteria),
        "unit_m": 1e-3,
    }

    try:
        runner = _execute or _openems_run_antenna
        raw = runner(plan)
        return _shape_s11_result(raw, plan, avail)
    except Exception as exc:  # noqa: BLE001 — never traceback out of the bridge
        msg = str(exc)
        skipped = "not installed" in msg.lower() or "import" in msg.lower()
        return {
            "ok": False,
            "skipped": bool(skipped),
            "reason": f"openEMS antenna run failed: {type(exc).__name__}: {exc}",
            "solver": "openems",
            "available": avail,
        }


def _shape_s11_result(raw: dict, plan: dict, avail: dict) -> dict:
    import numpy as np

    freq_raw = raw.get("freq_hz")
    s11_raw = raw.get("s11")
    freq = np.asarray([] if freq_raw is None else freq_raw, dtype=float)
    s11 = np.asarray([] if s11_raw is None else s11_raw, dtype=complex)
    if freq.size == 0 or s11.size == 0:
        return {
            "ok": False,
            "skipped": False,
            "reason": "openEMS returned empty S11 spectrum",
            "solver": "openems",
            "available": avail,
        }
    s11_db = 20.0 * np.log10(np.maximum(np.abs(s11), 1e-12))
    # Clamp numerical underflow floors (e.g. all-zero port signals → -∞ dB).
    s11_db = np.maximum(s11_db, -80.0)
    idx = int(np.argmin(s11_db))
    # -10 dB bandwidth around the minimum.
    below = s11_db <= -10.0
    if np.any(below):
        # largest contiguous region containing idx, else span of all below
        bw_hz = float(freq[below][-1] - freq[below][0]) if below.sum() > 1 else 0.0
    else:
        bw_hz = 0.0

    # Downsample for compact payload.
    n = len(freq)
    step = max(1, n // 41)
    freq_ghz = (freq[::step] / 1e9).tolist()
    s11_db_list = [round(float(v), 3) for v in s11_db[::step]]

    return {
        "ok": True,
        "skipped": False,
        "solver": "openems",
        "available": avail,
        "kind": plan["kind"],
        "f_res_hz": float(freq[idx]),
        "f_res_ghz": round(float(freq[idx]) / 1e9, 6),
        "s11_min_db": round(float(s11_db[idx]), 3),
        "bandwidth_hz_s11_m10db": float(bw_hz),
        "bandwidth_mhz_s11_m10db": round(bw_hz / 1e6, 3),
        "freq_ghz": [round(float(v), 5) for v in freq_ghz],
        "s11_db": s11_db_list,
        "n_freq": int(n),
        "solver_detail": raw.get("solver_detail") or {
            "method": "openEMS FDTD lumped-port antenna model",
        },
    }


def _openems_run_antenna(plan: dict) -> dict:
    """Build a minimal CSXCAD model and run openEMS for S11.

    Follows the official openEMS "Simple Patch Antenna" layout
    (docs.openems.de): substrate z=0..h, zero-thickness metal sheets for
    patch/ground, lumped feed with ``edges2grid='xy'``, then SmoothMeshLines.
    Geometry is simplified for monopole/IFA but uses the same port pattern.
    """
    import numpy as np
    try:
        from CSXCAD import ContinuousStructure
        from openEMS import openEMS
        from openEMS.physical_constants import C0 as C0_OPENEMS
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"openEMS not installed ({exc})") from exc

    dims = plan["dims"]
    kind = plan["kind"]
    sweep = plan["sweep"]
    unit = plan["unit_m"]
    er = float(dims.get("substrate_er") or 4.4)
    h = float(dims.get("substrate_h_mm") or 1.6)

    if kind == "patch":
        patch_w = float(dims.get("width_mm") or 30.0)
        patch_l = float(dims.get("length_mm") or 30.0)
        feed_x = float(dims.get("inset_mm") or 0.0) * 0.0  # centred; offset via feed_pos
        # Classic inset: feed offset along length (y) from centre toward edge.
        inset = float(dims.get("inset_mm") or patch_l * 0.15)
        feed_pos = -patch_l / 2.0 + max(inset, 1.0)  # y position of the feed
        sub_w = max(patch_w * 1.6, patch_w + 20.0)
        sub_l = max(patch_l * 1.6, patch_l + 20.0)
    elif kind == "monopole":
        patch_w = float(dims.get("width_mm") or 1.5)
        patch_l = float(dims.get("length_mm") or 20.0)
        feed_pos = 0.0
        sub_w = max(patch_l * 1.2, 30.0)
        sub_l = max(patch_l * 0.8, 20.0)
    else:
        patch_w = float(dims.get("span_mm") or dims.get("arm_length_mm") or 20.0)
        patch_l = float(dims.get("height_mm") or 5.0)
        feed_pos = float(dims.get("short_spacing_mm") or 2.0)
        sub_w = max(patch_w * 1.4, 30.0)
        sub_l = max(patch_l * 2.0, 20.0)

    f0 = float(sweep["f0_hz"])
    fc = float(sweep["fc_hz"])
    # openEMS mesh resolution (mm): λ_min / 20 in free space, tutorial style.
    mesh_res = C0_OPENEMS / (f0 + fc) / unit / 20.0
    mesh_res = max(min(mesh_res, 3.0), 0.6)

    # Sim box (mm) — compact enough for unit-test wall-time, large enough for MUR.
    sim_xy = max(sub_w, sub_l) + 2.0 * max(mesh_res * 8.0, 12.0)
    sim_z_neg = max(h * 2.0, 8.0)
    sim_z_pos = max(h * 6.0, 20.0)

    n_ts = max(int(plan["max_timesteps"]), 12_000)
    fdtd = openEMS(NrTS=n_ts, EndCriteria=float(plan["end_criteria"]))
    fdtd.SetGaussExcite(f0, fc)
    fdtd.SetBoundaryCond(["MUR", "MUR", "MUR", "MUR", "MUR", "MUR"])

    csx = ContinuousStructure()
    fdtd.SetCSX(csx)
    mesh = csx.GetGrid()
    mesh.SetDeltaUnit(unit)

    # Air box extents.
    mesh.AddLine("x", [-sim_xy / 2.0, sim_xy / 2.0])
    mesh.AddLine("y", [-sim_xy / 2.0, sim_xy / 2.0])
    mesh.AddLine("z", [-sim_z_neg, sim_z_pos])

    # Substrate: z = 0 .. h (tutorial convention).
    sub = csx.AddMaterial("substrate", epsilon=er)
    sub.AddBox([-sub_w / 2.0, -sub_l / 2.0, 0.0],
               [sub_w / 2.0, sub_l / 2.0, h], priority=0)
    # Extra z-lines through the substrate thickness.
    n_sub_cells = 4
    for zi in range(n_sub_cells + 1):
        mesh.AddLine("z", [h * zi / n_sub_cells])

    # Ground plane at z = 0 (zero-thickness metal sheet).
    gnd = csx.AddMetal("gnd")
    if kind == "patch":
        gnd.AddBox([-sub_w / 2.0, -sub_l / 2.0, 0.0],
                   [sub_w / 2.0, sub_l / 2.0, 0.0], priority=10)
    else:
        # Partial ground for monopole / IFA (feed side only).
        gnd.AddBox([-sub_w / 2.0, -sub_l / 2.0, 0.0],
                   [sub_w / 2.0, 0.0, 0.0], priority=10)
    try:
        fdtd.AddEdges2Grid(dirs="xy", properties=gnd)
    except Exception:  # noqa: BLE001
        pass

    metal = csx.AddMetal("radiator")
    if kind == "patch":
        # Patch sheet at z = h.
        metal.AddBox([-patch_w / 2.0, -patch_l / 2.0, h],
                     [patch_w / 2.0, patch_l / 2.0, h], priority=10)
        try:
            fdtd.AddEdges2Grid(dirs="xy", properties=metal, metal_edge_res=mesh_res / 2.0)
        except Exception:  # noqa: BLE001
            pass
        # Probe feed: vertical lumped port under the patch (tutorial style).
        port_start = [0.0, feed_pos, 0.0]
        port_stop = [0.0, feed_pos, h]
    elif kind == "monopole":
        # Vertical strip standing on the substrate top (simplified PCB monopole).
        metal.AddBox([-patch_w / 2.0, 0.0, h],
                     [patch_w / 2.0, patch_l, h], priority=10)
        try:
            fdtd.AddEdges2Grid(dirs="xy", properties=metal, metal_edge_res=mesh_res / 2.0)
        except Exception:  # noqa: BLE001
            pass
        port_start = [0.0, 0.0, 0.0]
        port_stop = [0.0, 0.0, h]
    else:
        # Planar IFA on substrate top: short + arm.
        tw = float(dims.get("trace_width_mm") or 1.0)
        metal.AddBox([0.0, 0.0, h], [tw, patch_l, h], priority=10)
        metal.AddBox([0.0, patch_l - tw, h], [patch_w, patch_l, h], priority=10)
        metal.AddBox([feed_pos - tw / 2.0, 0.0, h],
                     [feed_pos + tw / 2.0, patch_l, h], priority=10)
        try:
            fdtd.AddEdges2Grid(dirs="xy", properties=metal, metal_edge_res=mesh_res / 2.0)
        except Exception:  # noqa: BLE001
            pass
        port_start = [feed_pos, 0.0, 0.0]
        port_stop = [feed_pos, 0.0, h]

    # Lumped port with edges2grid so the port is meshed (tutorial key detail).
    port = fdtd.AddLumpedPort(
        1, 50.0, port_start, port_stop, "z", 1.0,
        priority=5, edges2grid="xy",
    )

    # Smooth the mesh (tutorial).
    try:
        mesh.SmoothMeshLines("all", mesh_res, 1.4)
    except Exception:  # noqa: BLE001
        pass

    # Cap cell count for CI: if still huge after smooth, accept it — wall-time
    # budget is enforced by NrTS.
    sim_path = tempfile.mkdtemp(prefix="chudware_ant_")
    # openEMS's fdtd.Run chdirs into sim_path and cleanup=True deletes it,
    # leaving the PROCESS CWD a dead directory — which breaks every later
    # CWD-relative read in this process (pytest suite pollution). Restore it.
    prev_cwd = os.getcwd()
    try:
        try:
            fdtd.Run(sim_path, cleanup=True)
        except TypeError:
            fdtd.Run(sim_path)

        freq = np.linspace(sweep["f_start_hz"], sweep["f_stop_hz"], sweep["n_freq"])
        port.CalcPort(sim_path, freq)
        inc = np.asarray(port.uf_inc, dtype=complex)
        ref = np.asarray(port.uf_ref, dtype=complex)
        # Guard NaNs / zeros from under-excited runs.
        inc_abs = np.abs(inc)
        if not np.isfinite(inc_abs).any() or float(np.nanmax(inc_abs)) < 1e-30:
            raise RuntimeError(
                "openEMS port voltages empty/NaN — mesh or excitation failed")
        inc = np.where(inc_abs < 1e-30, 1e-30, inc)
        s11 = ref / inc
        s11 = np.where(np.isfinite(s11), s11, 1.0 + 0j)
    finally:
        try:
            os.chdir(prev_cwd)
        except OSError:
            pass
        try:
            import shutil
            shutil.rmtree(sim_path, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

    return {
        "freq_hz": freq.tolist(),
        "s11": s11,
        "solver_detail": {
            "method": "openEMS FDTD lumped-port antenna (tutorial layout)",
            "mesh_res_mm": float(mesh_res),
            "sim_path_cleared": True,
            "kind": kind,
            "n_timesteps": n_ts,
        },
    }


# --------------------------------------------------------------------------- combined


def analyze_antenna(
    spec: Union[dict, Any],
    *,
    geometry: Optional[dict] = None,
    want_fullwave: bool = True,
    n_freq: int = 51,
    max_timesteps: int = 40_000,
    force_skip_fullwave: bool = False,
    _execute: Optional[Callable] = None,
) -> dict:
    """Resonance / return-loss analysis for a PCB antenna.

    Always includes ``analytic`` (closed-form). When ``want_fullwave`` is True
    also includes ``fullwave`` — either a real openEMS S11 result or an honest
    ``{skipped: true, reason}`` block.
    """
    analytic = analytic_resonance(spec)
    out: Dict[str, Any] = {
        "ok": True,
        "kind": analytic.get("kind"),
        "analytic": analytic,
    }
    if want_fullwave:
        fw = fullwave_s11(
            spec,
            geometry=geometry,
            n_freq=n_freq,
            max_timesteps=max_timesteps,
            force_skip=force_skip_fullwave,
            _execute=_execute,
        )
        out["fullwave"] = fw
        # Convenience top-level summary.
        if fw.get("ok") and not fw.get("skipped"):
            out["f_res_hz"] = fw.get("f_res_hz", analytic["f_res_hz"])
            out["s11_min_db"] = fw.get("s11_min_db")
            out["source"] = "fullwave-openems"
        else:
            out["f_res_hz"] = analytic["f_res_hz"]
            out["s11_min_db"] = analytic.get("s11_est_db")
            out["source"] = "analytic"
            if fw.get("skipped"):
                out["fullwave_skipped"] = True
                out["fullwave_skip_reason"] = fw.get("reason")
    else:
        out["f_res_hz"] = analytic["f_res_hz"]
        out["s11_min_db"] = analytic.get("s11_est_db")
        out["source"] = "analytic"
    out["f_res_ghz"] = round(float(out["f_res_hz"]) / 1e9, 6)
    return out
