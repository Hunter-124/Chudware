"""Piping -> 3D CFD handoff: extract the internal fluid volume from a pipe run and
hand it to the existing LBM / OpenFOAM RANS solvers.

Three public entry points:

* :func:`internal_volume` — boolean the bore out of the pipe solid to get a
  watertight fluid-domain solid; exports it as STEP. Falls back to a thin-wall
  swept cylinder when the outer solid is unavailable (kernel-absent: contract-shaped
  soft failure). Never raises except for :class:`PipingGeometryError`.

* :func:`cfd_boundary_from_solve` — translate a ``piping_solve`` result into CFD
  inlet/outlet boundary conditions: inlet velocity (from mean flow in the run) +
  inlet/outlet pressure (from the node pressures). Pure Python — no kernel needed
  (the MCP tools use the core's ``piping_cfd_boundary`` port, ``core/piping/cfd.cpp``).

* :func:`verify_pressure_drop` — compare the 1D network pressure drop (from
  ``piping_solve`` / ``piping_report``) with the CFD-derived pressure drop, flag
  if the difference exceeds ``tolerance_pct``. Contract-shaped soft failure. Used by
  the ``services.sim.multiphysics_rpc`` verify leg; the MCP tool calls the core's
  ``piping_verify_cfd`` port.

Design contract:
  - contract-shaped soft failures ``{"ok": False, "reason": ..., "hint": ...}``
    everywhere (matches the core ``piping_solve`` / ``catalog.nearest_pipe`` style).
  - build123d / OCP imported lazily (same as ``threed.py``); absent kernel → soft fail.
  - ``fluids`` optional; absent → soft fail on hydraulic calcs only.
  - No new dependencies beyond what the piping hydraulics modules already use.
"""

from __future__ import annotations

import math
import os
import uuid
from typing import Any, NamedTuple, Optional

from services.piping.schema import SCHEMA_PIPING


class _BoreSpec(NamedTuple):
    """Minimal pipe-spec stand-in for an explicit ``bore_mm`` override (mm).

    ``outer_dia`` only feeds the default bend radius (1.5·OD) in
    :func:`_run_fluid_solid`; with no wall data the bore itself is the best stand-in.
    """
    inner_dia: float
    outer_dia: float

__all__ = [
    "PipingCFDError",
    "internal_volume",
    "cfd_boundary_from_solve",
    "verify_pressure_drop",
]

# ── helpers ────────────────────────────────────────────────────────────────────


class PipingCFDError(Exception):
    """Structural impossibility (missing kernel, no geometry at all)."""


def _err(reason: str, hint: Optional[str] = None, **extra: Any) -> dict:
    out: dict[str, Any] = {"ok": False, "reason": reason}
    if hint:
        out["hint"] = hint
    out.update(extra)
    return out


def _have_kernel() -> bool:
    try:
        import build123d  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _dist(a, b) -> float:
    return math.sqrt(sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)))


# ── internal volume extraction ─────────────────────────────────────────────────

def _bore_wire(pts, bend_r: float):
    """Centreline path wire — identical to threed._path_wire but imported locally
    to avoid coupling to the outer-solid builder."""
    from build123d import BuildLine, Line, TangentArc, Vector

    def _sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
    def _scale(a, s): return (a[0] * s, a[1] * s, a[2] * s)
    def _dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
    def _norm(a): return math.sqrt(_dot(a, a))
    def _unit(a):
        n = _norm(a)
        return (a[0] / n, a[1] / n, a[2] / n) if n > 1e-12 else a
    def _add(a, b): return (a[0] + b[0], a[1] + b[1], a[2] + b[2])

    P = [tuple(map(float, p)) for p in pts]
    with BuildLine() as bl:
        cur = P[0]
        for i in range(1, len(P) - 1):
            a, b, c = P[i - 1], P[i], P[i + 1]
            u = _unit(_sub(b, a))
            w = _unit(_sub(c, b))
            d = max(-1.0, min(1.0, _dot(u, w)))
            delta = math.acos(d)
            if delta < 1e-6:
                continue
            # An end segment may be used whole, a segment shared with another bend half each
            # (a route ends one LR take-out past its last corner — that elbow keeps its radius).
            t = bend_r * math.tan(delta / 2.0)
            t = min(t, (1.0 if i == 1 else 0.5) * _norm(_sub(b, a)),
                    (1.0 if i == len(P) - 2 else 0.5) * _norm(_sub(c, b)))
            arc_start = _sub(b, _scale(u, t))
            arc_end = _add(b, _scale(w, t))
            if _norm(_sub(arc_start, cur)) > 1e-9:
                Line(Vector(*cur), Vector(*arc_start))
            TangentArc(Vector(*arc_start), Vector(*arc_end), tangent=Vector(*u))
            cur = arc_end
        if _norm(_sub(P[-1], cur)) > 1e-9:
            Line(Vector(*cur), Vector(*P[-1]))
    return bl.wire()


def _sweep_solid(path_wire, radius: float):
    """Sweep a solid disc (no bore) of given radius along path_wire."""
    from build123d import BuildPart, BuildSketch, Plane, Circle, sweep
    with BuildPart() as bp:
        with BuildSketch(Plane(origin=path_wire @ 0, z_dir=path_wire % 0)):
            Circle(radius)
        sweep(path=path_wire)
    return bp.part


def _run_fluid_solid(run: dict, spec, warnings: list):
    """Return the swept fluid-domain solid (inner bore radius) for one pipe run."""
    pts = run.get("path", [])
    if len(pts) < 2:
        warnings.append(f"{run.get('id')}: path has fewer than 2 points — skipped")
        return None
    inner_r = spec.inner_dia / 2.0  # mm
    bend_r = run.get("bend_radius") or (1.5 * spec.outer_dia)
    try:
        wire = _bore_wire(pts, bend_r)
        solid = _sweep_solid(wire, inner_r)
        return solid
    except Exception as exc:  # noqa: BLE001
        # Fallback: try a straight-line spline sweep
        try:
            from build123d import BuildLine, Spline, Vector
            with BuildLine() as bl:
                Spline(*[Vector(*map(float, p)) for p in pts])
            solid = _sweep_solid(bl.wire(), inner_r)
            warnings.append(
                f"{run.get('id')}: bore-wire sweep failed ({exc}); used spline fallback"
            )
            return solid
        except Exception as exc2:  # noqa: BLE001
            warnings.append(f"{run.get('id')}: fluid solid failed — {exc2}")
            return None


def internal_volume(
    doc: dict,
    out_path: str,
    *,
    run_id: Optional[str] = None,
    opts: Optional[dict] = None,
) -> dict:
    """Extract the internal fluid volume (bore solid) from a pipe run and write it as STEP.

    Parameters
    ----------
    doc:
        An ``aicad.piping/1`` document.
    out_path:
        Destination STEP file path (will be created/overwritten).
    run_id:
        If given, extract only that run's bore. Otherwise extract all non-suppressed runs
        and union them into one fluid-domain compound.
    opts:
        Reserved for future options (ignored for now).

    Returns
    -------
    dict
        ``{"ok": True, "step_path": ..., "run_ids": [...], "volume_mm3": ...,
           "inlet_xyz": [...], "outlet_xyz": [...], "warnings": [...]}``
        or ``{"ok": False, "reason": ..., "hint": ...}`` on soft failure.

    Raises
    ------
    PipingCFDError
        Only if the kernel is entirely absent or zero geometry is produced.
    """
    if doc.get("schema") != SCHEMA_PIPING:
        raise PipingCFDError(
            f"not an {SCHEMA_PIPING} document: {doc.get('schema')!r}"
        )

    if not _have_kernel():
        raise PipingCFDError(
            "build123d/OCP kernel not available — cannot extract internal volume. "
            "Install build123d (pip install build123d) or run on a machine with OCP."
        )

    from build123d import Compound, export_step
    from services.piping import catalog

    warnings: list = []
    children = []
    run_ids = []
    inlet_xyz: Optional[list] = None
    outlet_xyz: Optional[list] = None

    runs = doc.get("runs", [])
    if run_id is not None:
        runs = [r for r in runs if r.get("id") == run_id]
        if not runs:
            return _err(
                f"run_id {run_id!r} not found in document",
                hint=f"available run ids: {[r.get('id') for r in doc.get('runs', [])]}",
            )

    for run in runs:
        if run.get("suppressed"):
            continue
        bore_override = run.get("bore_mm")
        if bore_override:
            # Explicit inner diameter (mm) beats the NPS/schedule catalog — the SAME
            # precedence the core 1-D solver applies (build_network, core/piping/solve.cpp).
            # Externally-supplied networks (e.g. the hydraulic→piping handoff, whose
            # hose IDs are SAE dash sizes absent from the catalog) rely on this;
            # ignoring it swept the wrong bore and the extracted fluid volume
            # disagreed with π/4·d²·L (multiphysics regression suite).
            spec = _BoreSpec(inner_dia=float(bore_override),
                             outer_dia=float(bore_override))
        else:
            spec = catalog.nearest_pipe(
                nps=run.get("nps"), schedule=run.get("schedule", "40")
            )
        if isinstance(spec, dict):  # catalog failure
            warnings.append(
                f"{run.get('id')}: pipe size unresolved — {spec.get('reason')}"
            )
            continue

        solid = _run_fluid_solid(run, spec, warnings)
        if solid is None:
            continue

        # record inlet/outlet from path endpoints (first run encountered)
        if inlet_xyz is None:
            path = run.get("path", [])
            if path:
                inlet_xyz = [float(c) for c in path[0]]
                outlet_xyz = [float(c) for c in path[-1]]

        children.append(solid)
        run_ids.append(run.get("id"))

    if not children:
        raise PipingCFDError(
            "no fluid-domain geometry produced (all runs unresolved or suppressed): "
            + "; ".join(warnings[:4])
        )

    if len(children) == 1:
        assembly = children[0]
        try:
            vol = float(assembly.volume)
        except Exception:  # noqa: BLE001
            vol = 0.0
    else:
        assembly = Compound(children=children)
        assembly.label = "FluidDomain"
        try:
            vol = float(sum(getattr(c, "volume", 0.0) for c in children))
        except Exception:  # noqa: BLE001
            vol = 0.0

    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    export_step(assembly, out_path)

    return {
        "ok": True,
        "step_path": out_path,
        "run_ids": run_ids,
        "volume_mm3": round(vol, 3),
        "inlet_xyz": inlet_xyz,
        "outlet_xyz": outlet_xyz,
        "warnings": warnings,
    }


# ── boundary condition translation ─────────────────────────────────────────────

def cfd_boundary_from_solve(
    solve_result: dict,
    *,
    inlet_run_id: Optional[str] = None,
    outlet_node_id: Optional[str] = None,
) -> dict:
    """Translate a ``piping_solve`` result into CFD inlet/outlet boundary conditions.

    The 1D solver gives per-link velocity and per-node pressure. This function
    picks the inlet link (highest-flow link feeding into the network, or the link
    for ``inlet_run_id``) and expresses:

    * **inlet_velocity_ms** — mean axial flow speed in the pipe bore (m/s)
    * **inlet_pressure_kpa** — gauge pressure at the inlet node (kPa)
    * **outlet_pressure_kpa** — gauge pressure at the outlet node (kPa)
    * **pressure_drop_1d_kpa** — inlet − outlet (the budget the CFD must agree with)
    * **fluid** — rho + mu from the solve (for run_cfd_flow / run_cfd_accurate)
    * **ref_diameter_mm** — inner diameter of the inlet pipe (for Reynolds scaling)

    Parameters
    ----------
    solve_result:
        Dict returned by the core ``piping_solve`` RPC.
    inlet_run_id:
        If given, use this link as the inlet. Otherwise pick the link with the
        largest absolute flow.
    outlet_node_id:
        If given, use this node as the outlet. Otherwise pick the node with the
        lowest pressure (the outlet end of the network).

    Returns
    -------
    dict
        ``{"ok": True, "inlet_velocity_ms": ..., "inlet_pressure_kpa": ...,
           "outlet_pressure_kpa": ..., "pressure_drop_1d_kpa": ...,
           "fluid": {...}, "ref_diameter_mm": ..., "inlet_link": ...,
           "outlet_node": ...}``
        or contract-shaped soft failure.
    """
    if not solve_result.get("ok"):
        return _err(
            "solve_result is not ok — cannot extract CFD boundary conditions",
            hint="run piping_solve first and pass a successful result",
        )

    links = solve_result.get("links", [])
    nodes = solve_result.get("nodes", [])
    if not links:
        return _err("solve_result has no links", hint="check the piping document has runs")
    if not nodes:
        return _err("solve_result has no nodes")

    # pick inlet link
    if inlet_run_id is not None:
        inlet_links = [lk for lk in links if lk.get("id") == inlet_run_id]
        if not inlet_links:
            return _err(
                f"inlet_run_id {inlet_run_id!r} not found in solve links",
                hint=f"available link ids: {[lk.get('id') for lk in links]}",
            )
        inlet_link = inlet_links[0]
    else:
        inlet_link = max(links, key=lambda lk: abs(lk.get("flow_m3h", 0.0)))

    # pick outlet node: the node at the downstream end of the inlet link with lowest pressure
    if outlet_node_id is not None:
        outlet_nodes = [nd for nd in nodes if nd.get("id") == outlet_node_id]
        if not outlet_nodes:
            return _err(
                f"outlet_node_id {outlet_node_id!r} not found in solve nodes",
                hint=f"available node ids: {[nd.get('id') for nd in nodes]}",
            )
        outlet_node = outlet_nodes[0]
    else:
        outlet_node = min(nodes, key=lambda nd: nd.get("pressure_kpa", 0.0))

    # inlet node: the "from" end of the inlet link (highest pressure)
    inlet_node_id = inlet_link.get("from")
    inlet_nodes = [nd for nd in nodes if nd.get("id") == inlet_node_id]
    if not inlet_nodes:
        return _err(
            f"inlet node {inlet_node_id!r} not found in solve result nodes",
        )
    inlet_node = inlet_nodes[0]

    inlet_v_ms = float(abs(inlet_link.get("velocity_ms", 0.0)))
    inlet_p_kpa = float(inlet_node.get("pressure_kpa", 0.0))
    outlet_p_kpa = float(outlet_node.get("pressure_kpa", 0.0))
    dp_1d = inlet_p_kpa - outlet_p_kpa

    fluid = solve_result.get("fluid", {})
    ref_dia_mm = float(inlet_link.get("id_mm", 0.0))

    return {
        "ok": True,
        "inlet_velocity_ms": round(inlet_v_ms, 4),
        "inlet_pressure_kpa": round(inlet_p_kpa, 3),
        "outlet_pressure_kpa": round(outlet_p_kpa, 3),
        "pressure_drop_1d_kpa": round(dp_1d, 3),
        "fluid": fluid,
        "ref_diameter_mm": round(ref_dia_mm, 3),
        "inlet_link": inlet_link.get("id"),
        "outlet_node": outlet_node.get("id"),
        "hint": (
            "Use inlet_velocity_ms as the CFD inlet BC; outlet_pressure_kpa as the "
            "CFD outlet pressure (relative); compare CFD pressure drop to "
            "pressure_drop_1d_kpa via piping_verify_cfd."
        ),
    }


# ── verify node: 1D vs 3D pressure drop ───────────────────────────────────────

def verify_pressure_drop(
    solve_result: dict,
    cfd_result: dict,
    *,
    tolerance_pct: float = 20.0,
    run_id: Optional[str] = None,
) -> dict:
    """Compare the 1D network pressure drop with the 3D CFD pressure drop.

    The 1D budget comes from ``solve_result`` (``piping_solve`` output) — either the
    total network ``max_imbalance``-corrected drop, or a per-link drop for
    ``run_id``. The CFD budget comes from ``cfd_result`` (``run_cfd_flow`` or
    ``run_cfd_accurate`` output) — interpreted as the drag-based pressure drop across
    the fluid domain.

    The CFD pressure drop is estimated from the drag force and the pipe
    cross-section::

        dP_cfd = drag_n / frontal_area_m2  [Pa]  →  kPa

    For internal pipe flow this is the wall drag divided by the bore area — a
    first-principles approximation that is exact for a straight duct, approximate for
    bends/fittings (which is why the 20 % default tolerance is generous).

    Parameters
    ----------
    solve_result:
        Output of the core ``piping_solve`` RPC.
    cfd_result:
        Output of ``run_cfd_flow`` (LBM) or the result dict from a completed
        ``run_cfd_accurate`` job.
    tolerance_pct:
        Acceptable deviation between 1D and 3D pressure drop as a percentage of the
        1D value. Default 20 %.
    run_id:
        If given, extract the 1D pressure drop for that specific run link.

    Returns
    -------
    dict
        ``{"ok": True, "pass": bool, "dp_1d_kpa": ..., "dp_3d_kpa": ...,
           "deviation_pct": ..., "tolerance_pct": ..., "verdict": str,
           "velocity_limits": {...}, "warnings": [...]}``
        or contract-shaped soft failure.
    """
    if not solve_result.get("ok"):
        return _err(
            "solve_result is not ok",
            hint="run piping_solve first",
        )
    if not cfd_result.get("ok"):
        return _err(
            "cfd_result is not ok",
            hint="run piping_cfd_handoff or run_cfd_flow first",
        )

    warnings: list = []

    # ── 1D pressure drop ──────────────────────────────────────────────────────
    links = solve_result.get("links", [])
    if run_id is not None:
        target_links = [lk for lk in links if lk.get("id") == run_id]
        if not target_links:
            return _err(
                f"run_id {run_id!r} not found in solve links",
                hint=f"available: {[lk.get('id') for lk in links]}",
            )
        dp_1d_kpa = float(target_links[0].get("dp_kpa", 0.0))
    else:
        # Total network drop: sum all link dp_kpa (conservative: worst-case path)
        dp_1d_kpa = float(sum(abs(lk.get("dp_kpa", 0.0)) for lk in links))

    if dp_1d_kpa <= 0.0:
        warnings.append("1D pressure drop is zero or negative — result may be trivial")

    # ── 3D CFD pressure drop ──────────────────────────────────────────────────
    # run_cfd_flow reports drag_n (N) and frontal_area_mm2 (mm²).
    # For internal pipe flow: dP = F_drag / A_bore → Pa → kPa.
    # run_cfd_accurate reports drag_n and ref_area_mm2 similarly.
    drag_n = float(cfd_result.get("drag_n", 0.0))
    # prefer bore area from the solve boundary extraction; fall back to CFD report
    area_mm2 = float(
        cfd_result.get("frontal_area_mm2")
        or cfd_result.get("ref_area_mm2")
        or 0.0
    )

    dp_3d_kpa: float
    if drag_n == 0.0 or area_mm2 == 0.0:
        # Fallback: try to derive from velocity + Cd using Bernoulli
        # (run_cfd_flow always provides cd + velocity_ms + ref_length_mm)
        cd = float(cfd_result.get("cd", 0.0))
        v_ms = float(cfd_result.get("velocity_ms", 0.0))
        rho = float((solve_result.get("fluid") or {}).get("rho", 998.2))
        if cd > 0.0 and v_ms > 0.0:
            dp_3d_kpa = 0.5 * rho * v_ms ** 2 * cd / 1000.0
            warnings.append(
                "frontal_area_mm2 not in CFD result — pressure drop estimated from "
                "Cd × ½ρV² (approximate; prefer internal-flow CFD mode)"
            )
        else:
            return _err(
                "CFD result has no drag_n/frontal_area_mm2 or cd/velocity_ms — "
                "cannot derive pressure drop",
                hint="run run_cfd_flow with the fluid-domain STEP imported as a CAD part",
            )
    else:
        area_m2 = area_mm2 * 1e-6  # mm² → m²
        dp_3d_kpa = drag_n / area_m2 / 1000.0  # Pa → kPa

    # ── comparison ────────────────────────────────────────────────────────────
    if dp_1d_kpa > 0.0:
        deviation_pct = abs(dp_3d_kpa - dp_1d_kpa) / dp_1d_kpa * 100.0
    else:
        deviation_pct = 0.0 if dp_3d_kpa == 0.0 else float("inf")

    passed = deviation_pct <= tolerance_pct

    # ── velocity limits ───────────────────────────────────────────────────────
    # Advisory: flag any pipe link whose velocity exceeds recommended limits.
    # (water: 3 m/s general service; compressed air / steam: much higher, skipped here)
    VELOCITY_LIMIT_MS = 3.0  # m/s for liquid service (advisory)
    velocity_violations = []
    for lk in links:
        v = abs(float(lk.get("velocity_ms", 0.0)))
        if v > VELOCITY_LIMIT_MS:
            velocity_violations.append({
                "link": lk.get("id"),
                "velocity_ms": round(v, 3),
                "limit_ms": VELOCITY_LIMIT_MS,
            })

    if velocity_violations:
        warnings.append(
            f"{len(velocity_violations)} link(s) exceed {VELOCITY_LIMIT_MS} m/s "
            f"(erosion risk) — consider larger pipe or reduced flow"
        )

    verdict: str
    if passed:
        verdict = (
            f"PASS: 3D CFD dp ({dp_3d_kpa:.2f} kPa) within {tolerance_pct:.0f}% "
            f"of 1D solve dp ({dp_1d_kpa:.2f} kPa) — Δ {deviation_pct:.1f}%"
        )
    else:
        verdict = (
            f"FAIL: 3D CFD dp ({dp_3d_kpa:.2f} kPa) deviates {deviation_pct:.1f}% "
            f"from 1D solve dp ({dp_1d_kpa:.2f} kPa) — tolerance {tolerance_pct:.0f}%"
        )

    return {
        "ok": True,
        "pass": passed,
        "dp_1d_kpa": round(dp_1d_kpa, 3),
        "dp_3d_kpa": round(dp_3d_kpa, 3),
        "deviation_pct": round(deviation_pct, 2),
        "tolerance_pct": tolerance_pct,
        "verdict": verdict,
        "velocity_limits": {
            "checked": True,
            "limit_ms": VELOCITY_LIMIT_MS,
            "violations": velocity_violations,
        },
        "warnings": warnings,
        "hint": None if passed else (
            "Large 1D vs 3D deviation is expected for elbows/tees (secondary flows) — "
            "check that the CFD domain is the fluid bore, not the pipe wall solid. "
            "If using run_cfd_flow on the bore STEP, ensure inlet velocity matches "
            "the 1D solve velocity (use piping_cfd_handoff to set BCs automatically)."
        ),
    }
