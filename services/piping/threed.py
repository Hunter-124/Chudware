"""Piping -> 3D bridge: turn an ``aicad.piping/1`` document into a STEP solid + a CAD
``aicad.part/1`` doc, mirroring :mod:`services.ecad.threed`.

Two entry points (contract §7):

* :func:`piping_to_step` — genuine OCCT sweep of each run's annular cross-section along its
  filleted 3-D centreline (bend radius honoured, so elbows are real, not mitred) + parametric
  fitting solids (elbow/tee/reducer/flange) -> one ``Compound`` -> STEP on disk.
* :func:`piping_to_part_doc` — a minimal ``aicad.part/1`` document whose single ``import``
  feature references the STEP, matching the envelope the core part-document compiler expects,
  so the host opens the routed pipe directly in the CAD viewport (same as ECAD's
  board_to_part_doc).

build123d / OCP are imported lazily inside functions so importing this module stays cheap and
side-effect-free on a box without the kernel. Units are **mm**. Geometry realizability issues
are collected in the returned ``summary`` (contract-shaped), never raised.
"""

from __future__ import annotations

import math
import os
import uuid
from dataclasses import dataclass, field

from services.piping import catalog
from services.piping.schema import SCHEMA_PIPING

_ARC_LEG = 1.0  # mm straight tangent stub kept either side of a fillet arc (sweep stability)

# advisory colours per material family (rgb 0..1); default steel grey
_MAT_COLOR = {
    "PVC": (0.85, 0.85, 0.80), "CPVC": (0.80, 0.82, 0.70),
    "copper": (0.72, 0.45, 0.20),
}
_DEFAULT_COLOR = (0.55, 0.57, 0.60)


class PipingGeometryError(Exception):
    """Raised only for structurally impossible input (e.g. no kernel, no valid geometry at all)."""


@dataclass
class _Summary:
    n_runs: int = 0
    n_fittings: int = 0
    n_skipped: int = 0
    total_length: float = 0.0
    volume: float = 0.0
    warnings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "n_runs": self.n_runs, "n_fittings": self.n_fittings,
            "n_skipped": self.n_skipped, "total_length": round(self.total_length, 3),
            "volume": round(self.volume, 3), "warnings": list(self.warnings),
        }


# ── small vector helpers (no numpy dependency) ──────────────────────────────────

def _sub(a, b): return (a[0] - b[0], a[1] - b[1], a[2] - b[2])
def _add(a, b): return (a[0] + b[0], a[1] + b[1], a[2] + b[2])
def _scale(a, s): return (a[0] * s, a[1] * s, a[2] * s)
def _dot(a, b): return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
def _norm(a): return math.sqrt(_dot(a, a))


def _unit(a):
    n = _norm(a)
    return (a[0] / n, a[1] / n, a[2] / n) if n > 1e-12 else a


def _resolve_spec(item: dict, summary: _Summary):
    """Return a PipeSpec for a run/fitting or None (recording a warning) on catalog failure."""
    spec = catalog.nearest_pipe(nps=item.get("nps"), schedule=item.get("schedule", "40"))
    if isinstance(spec, dict):  # contract-shaped failure
        summary.warnings.append(
            f"{item.get('id')}: pipe size unresolved — {spec.get('reason')}"
            + (f" ({spec['hint']})" if spec.get("hint") else ""))
        return None
    return spec


def _color(rgb):
    from build123d import Color
    return Color(float(rgb[0]), float(rgb[1]), float(rgb[2]))


def _mat_color(item: dict, doc_mat):
    mat = item.get("material") or doc_mat
    return _MAT_COLOR.get(mat, _DEFAULT_COLOR)


# ── pipe-run sweep (the routed pipe) ────────────────────────────────────────────

def _path_wire(pts, bend_r: float):
    """A build123d Wire following ``pts`` (mm) with interior vertices rounded to ``bend_r``
    via tangent circular arcs. Genuinely 3-D (non-planar OK)."""
    from build123d import BuildLine, Line, TangentArc, Vector

    P = [tuple(map(float, p)) for p in pts]
    with BuildLine() as bl:
        cur = P[0]
        for i in range(1, len(P) - 1):
            a, b, c = P[i - 1], P[i], P[i + 1]
            u = _unit(_sub(b, a))
            w = _unit(_sub(c, b))
            d = max(-1.0, min(1.0, _dot(u, w)))
            delta = math.acos(d)
            if delta < 1e-6:  # colinear — no bend
                continue
            # clamp tangent setback so it never eats past the neighbouring vertices
            t = bend_r * math.tan(delta / 2.0)
            t = min(t, 0.49 * _norm(_sub(b, a)), 0.49 * _norm(_sub(c, b)))
            arc_start = _sub(b, _scale(u, t))
            arc_end = _add(b, _scale(w, t))
            Line(Vector(*cur), Vector(*arc_start))
            TangentArc(Vector(*arc_start), Vector(*arc_end), tangent=Vector(*u))
            cur = arc_end
        Line(Vector(*cur), Vector(*P[-1]))
    return bl.wire()


def _sweep_annulus(path_wire, outer_dia: float, wall: float):
    """Sweep an annular (or solid, if wall<=0) circular section along ``path_wire``."""
    from build123d import BuildPart, BuildSketch, Plane, Circle, sweep, Mode
    with BuildPart() as bp:
        with BuildSketch(Plane(origin=path_wire @ 0, z_dir=path_wire % 0)):
            Circle(outer_dia / 2.0)
            if wall and wall > 0:
                Circle(max(0.1, outer_dia / 2.0 - wall), mode=Mode.SUBTRACT)
        sweep(path=path_wire)
    return bp.part


def _run_solid(run: dict, spec, summary: _Summary):
    """Swept solid for one pipe_run. Falls back to a smooth spline path if the arc-wire
    sweep fails (degenerate corner), then to None (recording a warning)."""
    pts = run.get("path", [])
    bend_r = run.get("bend_radius") or (1.5 * spec.outer_dia)  # long-radius default
    try:
        part = _sweep_annulus(_path_wire(pts, bend_r), spec.outer_dia, spec.wall)
    except Exception as exc:  # noqa: BLE001
        try:
            from build123d import BuildLine, Spline, Vector
            with BuildLine() as bl:
                Spline(*[Vector(*map(float, p)) for p in pts])
            part = _sweep_annulus(bl.wire(), spec.outer_dia, spec.wall)
            summary.warnings.append(f"{run.get('id')}: arc sweep failed ({exc}); used spline path")
        except Exception as exc2:  # noqa: BLE001
            summary.warnings.append(f"{run.get('id')}: sweep failed — {exc2}")
            return None
    # centreline length for the summary
    length = sum(_norm(_sub(tuple(pts[i + 1]), tuple(pts[i]))) for i in range(len(pts) - 1))
    summary.total_length += length
    return part


# ── fitting solids ──────────────────────────────────────────────────────────────

def _bore(solid, inner_dia: float, axis_len: float, loc=None):
    """Subtract an over-long bore cylinder (radius inner_dia/2) from ``solid`` along local Z."""
    from build123d import Cylinder, Pos
    if inner_dia <= 0:
        return solid
    cut = Cylinder(radius=inner_dia / 2.0, height=axis_len)
    if loc is not None:
        cut = loc * cut
    try:
        return solid - cut
    except Exception:  # noqa: BLE001
        return solid


def _elbow_solid(ft: dict, spec, summary: _Summary):
    """A LR/SR elbow of the given angle, incoming along +X, turning in the XY plane."""
    params = ft.get("params", {}) or {}
    angle = float(params.get("angle", 90))
    r_kind = params.get("radius", "LR")
    R = spec.outer_dia * (1.5 if r_kind != "SR" else 1.0)
    theta = math.radians(angle)
    # canonical corner: come in along +X to origin, leave rotated by `angle` in +XY
    out_dir = (math.cos(theta), math.sin(theta), 0.0)
    leg = _ARC_LEG + 0.25 * spec.outer_dia
    pin = _scale((-1.0, 0.0, 0.0), R + leg)          # a point back along -X
    pout = _scale(out_dir, R + leg)
    pts = [pin, (0.0, 0.0, 0.0), pout]
    run = {"id": ft.get("id"), "path": pts, "bend_radius": R}
    return _run_solid(run, spec, summary)


def _tee_solid(ft: dict, spec, summary: _Summary):
    """Equal/reducing tee: run cylinder along X + branch cylinder along +Y from the centre."""
    from build123d import Cylinder, Pos, Rot
    params = ft.get("params", {}) or {}
    branch_nps = params.get("branch_nps") or ft.get("nps")
    bspec = catalog.nearest_pipe(nps=branch_nps, schedule=ft.get("schedule", "40"))
    bod = bspec.outer_dia if not isinstance(bspec, dict) else spec.outer_dia
    bid = bspec.inner_dia if not isinstance(bspec, dict) else spec.inner_dia
    dims = catalog.fitting_dims("tee", spec.nps)
    c = dims.get("center_to_end") if isinstance(dims, dict) and dims.get("ok") is not False \
        else 1.5 * spec.outer_dia
    run_len = 2.0 * float(c)
    branch_len = float(c)
    # run along X (Cylinder is along Z by default -> rotate 90 about Y)
    run = Rot(0, 90, 0) * Cylinder(radius=spec.outer_dia / 2.0, height=run_len)
    run = _bore(run, spec.inner_dia, run_len + 2, loc=Rot(0, 90, 0))
    # branch along +Y from centre
    branch = Pos(0, branch_len / 2.0, 0) * Rot(-90, 0, 0) * Cylinder(radius=bod / 2.0, height=branch_len)
    branch = _bore(branch, bid, branch_len + 2, loc=Pos(0, branch_len / 2.0, 0) * Rot(-90, 0, 0))
    try:
        return run + branch
    except Exception as exc:  # noqa: BLE001
        summary.warnings.append(f"{ft.get('id')}: tee union failed — {exc}")
        return run


def _reducer_solid(ft: dict, spec, summary: _Summary):
    """Concentric reducer: frustum from nps (large) to nps2 (small) over catalog length H."""
    from build123d import Cone, Cylinder
    spec2 = catalog.nearest_pipe(nps=ft.get("nps2"), schedule=ft.get("schedule", "40"))
    if isinstance(spec2, dict):
        summary.warnings.append(f"{ft.get('id')}: reducer small end unresolved — {spec2.get('reason')}")
        return None
    dims = catalog.fitting_dims("reducer", spec.nps, nps2=spec2.nps)
    H = dims.get("length") if isinstance(dims, dict) and dims.get("ok") is not False \
        else 2.0 * (spec.outer_dia + spec2.outer_dia) / 2.0
    H = float(H)
    outer = Cone(bottom_radius=spec.outer_dia / 2.0, top_radius=spec2.outer_dia / 2.0, height=H)
    try:
        inner = Cone(bottom_radius=max(0.1, spec.inner_dia / 2.0),
                     top_radius=max(0.1, spec2.inner_dia / 2.0), height=H + 2)
        return outer - inner
    except Exception:  # noqa: BLE001
        return outer


def _flange_solid(ft: dict, spec, summary: _Summary):
    """Weld-neck flange proxy: disc + short hub, bored, with bolt-circle holes."""
    from build123d import Cylinder, Pos, Rot
    params = ft.get("params", {}) or {}
    fclass = int(params.get("flange_class", 150))
    dims = catalog.fitting_dims("flange", spec.nps, flange_class=fclass)
    if not isinstance(dims, dict) or dims.get("ok") is False:
        summary.warnings.append(f"{ft.get('id')}: flange dims unresolved — "
                                f"{dims.get('reason') if isinstance(dims, dict) else 'no data'}")
        return None
    od = float(dims["od"]); thk = float(dims["thickness"])
    bc = float(dims["bolt_circle"]); nb = int(dims["bolt_holes"]); bd = float(dims["bolt_dia"])
    hub_len = max(2.0, 0.5 * spec.outer_dia)
    disc = Cylinder(radius=od / 2.0, height=thk)
    hub = Pos(0, 0, (thk + hub_len) / 2.0) * Cylinder(radius=spec.outer_dia / 2.0, height=hub_len)
    try:
        body = disc + hub
    except Exception:  # noqa: BLE001
        body = disc
    body = _bore(body, spec.inner_dia, thk + hub_len + 2, loc=Pos(0, 0, hub_len / 2.0))
    # bolt holes around the bolt circle
    for i in range(nb):
        a = 2.0 * math.pi * i / nb
        x, y = bc / 2.0 * math.cos(a), bc / 2.0 * math.sin(a)
        try:
            body = body - Pos(x, y, 0) * Cylinder(radius=bd / 2.0, height=thk + 2)
        except Exception:  # noqa: BLE001
            summary.warnings.append(f"{ft.get('id')}: bolt hole {i} failed")
    return body


_FITTING_BUILDERS = {
    "elbow": _elbow_solid, "tee": _tee_solid,
    "reducer": _reducer_solid, "flange": _flange_solid,
}


def _place(shape, origin, orient):
    """Rotate by XYZ Euler degrees then translate to ``origin`` (both from the fitting)."""
    from build123d import Pos, Rot
    rx, ry, rz = (float(orient[0]), float(orient[1]), float(orient[2])) if orient else (0, 0, 0)
    ox, oy, oz = (float(origin[0]), float(origin[1]), float(origin[2])) if origin else (0, 0, 0)
    return Pos(ox, oy, oz) * Rot(rx, ry, rz) * shape


# ── public API ───────────────────────────────────────────────────────────────

def piping_to_step(doc: dict, out_path: str, opts=None) -> dict:
    """Convert ``doc`` (an ``aicad.piping/1`` document) into a STEP solid at ``out_path``.

    Returns ``{"step_path": <abs>, "summary": {...}}``. Raises :class:`PipingGeometryError`
    only when the kernel is missing or nothing at all could be built.
    """
    if doc.get("schema") != SCHEMA_PIPING:
        raise PipingGeometryError(f"not an {SCHEMA_PIPING} document: {doc.get('schema')!r}")
    try:
        from build123d import Compound, export_step
    except Exception as exc:  # noqa: BLE001
        raise PipingGeometryError(f"build123d/OCP kernel not available: {exc}") from exc

    summary = _Summary()
    doc_mat = doc.get("material")
    children: list = []

    for run in doc.get("runs", []):
        if run.get("suppressed"):
            continue
        spec = _resolve_spec(run, summary)
        if spec is None:
            summary.n_skipped += 1
            continue
        solid = _run_solid(run, spec, summary)
        if solid is None:
            summary.n_skipped += 1
            continue
        try:
            solid.color = _color(_mat_color(run, doc_mat))
            solid.label = str(run.get("name") or run.get("id") or "run")
        except Exception:  # noqa: BLE001
            pass
        children.append(solid)
        summary.n_runs += 1

    for ft in doc.get("fittings", []):
        if ft.get("suppressed"):
            continue
        spec = _resolve_spec(ft, summary)
        if spec is None:
            summary.n_skipped += 1
            continue
        builder = _FITTING_BUILDERS.get(ft.get("type"))
        if builder is None:
            summary.warnings.append(f"{ft.get('id')}: unknown fitting type {ft.get('type')!r}")
            summary.n_skipped += 1
            continue
        try:
            shape = builder(ft, spec, summary)
        except Exception as exc:  # noqa: BLE001
            summary.warnings.append(f"{ft.get('id')}: build failed — {exc}")
            shape = None
        if shape is None:
            summary.n_skipped += 1
            continue
        placed = _place(shape, ft.get("origin"), ft.get("orient"))
        try:
            placed.color = _color(_mat_color(ft, doc_mat))
            placed.label = str(ft.get("name") or ft.get("id") or ft.get("type"))
        except Exception:  # noqa: BLE001
            pass
        children.append(placed)
        summary.n_fittings += 1

    if not children:
        raise PipingGeometryError(
            "no geometry produced (all runs/fittings unresolved or suppressed): "
            + "; ".join(summary.warnings[:4]))

    assembly = Compound(children=children)
    assembly.label = str(doc.get("name") or "Piping")
    try:
        summary.volume = float(sum(getattr(c, "volume", 0.0) for c in children))
    except Exception:  # noqa: BLE001
        summary.volume = 0.0

    out_path = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    export_step(assembly, out_path)
    return {"step_path": out_path, "summary": summary.as_dict()}


def piping_to_part_doc(doc: dict, step_path: str) -> dict:
    """A minimal ``aicad.part/1`` document whose single ``import`` feature references
    ``step_path`` — the same envelope the core part-document compiler compiles + the host opens
    in the viewport (identical shape to services.ecad.threed.board_to_part_doc)."""
    name = str(doc.get("name") or "Piping")
    return {
        "schema": "aicad.part/1",
        "id": "doc-" + uuid.uuid4().hex[:8],
        "name": name,
        "units": {"system": "MMGS"},
        "material": None,
        "equations": {"globals": []},
        "features": [
            {
                "id": "f1",
                "type": "import",
                "name": "Piping",
                "suppressed": False,
                "params": {
                    "source_path": os.path.abspath(step_path),
                    "format": "step",
                    "original_name": name,
                },
                "refs": {},
            }
        ],
    }
