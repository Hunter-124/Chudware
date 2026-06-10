"""Headless multi-view screenshots of a model's mesh (PyVista / VTK off-screen).

Used by MCP-headless mode and tests. In the desktop app the live OpenGL viewport is the
primary screenshot source, but this gives the model the same kind of multi-angle imagery
without a window.
"""
from __future__ import annotations

import os
from pathlib import Path

# Six views in a 2x3 sheet: enclosures put their interesting features (I/O cutouts,
# mounting patterns, ports) on the back/bottom faces, which a 4-view sheet never showed —
# the model literally could not verify them.
_VIEWS = ("iso", "front", "back", "right", "top", "bottom")
_GRID_SHAPE = (2, 3)
_GRID_SIZE = (1536, 768)  # keeps each tile at the former 512x384

# Distinct, visually separable colors cycled per part in an assembly render, so the model can
# tell the bodies apart and judge how they fit. Chosen to read well on the white background.
_PART_COLORS = (
    "#4C72B0", "#DD8452", "#55A868", "#C44E52", "#8172B3",
    "#937860", "#DA8BC3", "#7F7F7F", "#CCB974", "#64B5CD",
)


def _pv():
    """Lazy-import pyvista; returns the module or None when pyvista/vtk is not installed."""
    os.environ.setdefault("PYVISTA_OFF_SCREEN", "true")
    try:
        import pyvista as pv  # noqa: PLC0415
        pv.OFF_SCREEN = True
        return pv
    except ImportError:
        return None


def _apply_view(plotter, name: str) -> None:
    if name == "iso":
        plotter.view_isometric()
    elif name == "front":
        plotter.view_xz()                    # looking down -Y
    elif name == "back":
        plotter.view_xz(negative=True)       # looking down +Y
    elif name == "right":
        plotter.view_yz()                    # looking down -X
    elif name == "top":
        plotter.view_xy()                    # looking down -Z
    elif name == "bottom":
        plotter.view_xy(negative=True)       # looking down +Z
    else:
        plotter.view_isometric()


def _prep(stl_path: str, pv):
    """Load mesh + clean feature edges (silhouette/creases/holes), no triangulation noise."""
    mesh = pv.read(str(stl_path))
    try:
        mesh = mesh.compute_normals(split_vertices=True, feature_angle=30.0)
    except Exception:  # noqa: BLE001
        pass
    try:
        edges = mesh.extract_feature_edges(
            feature_angle=20.0,
            boundary_edges=True,
            non_manifold_edges=False,
            feature_edges=True,
            manifold_edges=False,
        )
    except Exception:  # noqa: BLE001
        edges = None
    return mesh, edges


def _add_part(p, mesh, edges) -> None:
    p.set_background("white")
    p.add_mesh(mesh, color="#9db4cc", smooth_shading=True, specular=0.25,
               specular_power=15, show_edges=False)
    if edges is not None and edges.n_points:
        p.add_mesh(edges, color="#1c2a36", line_width=2)


def render_views(stl_path: str, out_dir: Path, prefix: str = "view",
                 views: tuple[str, ...] = _VIEWS,
                 size: tuple[int, int] = (1024, 768)) -> dict[str, str]:
    """Render ``stl_path`` from each named view to a PNG. Returns {view: png_path},
    or an empty dict if pyvista is unavailable."""
    pv = _pv()
    if pv is None:
        return {}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    mesh, edges = _prep(stl_path, pv)

    paths: dict[str, str] = {}
    for name in views:
        p = pv.Plotter(off_screen=True, window_size=list(size))
        _add_part(p, mesh, edges)
        p.add_axes(line_width=3)
        _apply_view(p, name)
        p.reset_camera()
        p.camera.zoom(1.15)
        png = out_dir / f"{prefix}_{name}.png"
        p.screenshot(str(png))
        p.close()
        paths[name] = str(png)
    return paths


def render_grid(stl_path: str, out_path: Path,
                size: tuple[int, int] = _GRID_SIZE) -> str | None:
    """Single 2x3 contact sheet (iso/front/back/right/top/bottom) — one image for the model.
    Returns None if pyvista/vtk is unavailable."""
    pv = _pv()
    if pv is None:
        return None
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mesh, edges = _prep(stl_path, pv)

    p = pv.Plotter(off_screen=True, shape=_GRID_SHAPE, window_size=list(size), border=True)
    for idx, name in enumerate(_VIEWS):
        p.subplot(idx // _GRID_SHAPE[1], idx % _GRID_SHAPE[1])
        _add_part(p, mesh, edges)
        p.add_text(name, font_size=10, color="#26323d")
        _apply_view(p, name)
        p.reset_camera()
        p.camera.zoom(1.1)
    p.screenshot(str(out_path))
    p.close()
    return str(out_path)


def _coerce_parts(parts: list) -> list[tuple[str, str]]:
    """Normalize a parts list into [(name, stl_path)]. Accepts dicts ({"name","stl_path"})
    or (name, stl_path) tuples; drops entries without an existing STL."""
    items: list[tuple[str, str]] = []
    for i, p in enumerate(parts):
        if isinstance(p, dict):
            name = p.get("name") or f"part{i + 1}"
            stl = p.get("stl_path")
        else:
            name, stl = p
        if stl and Path(stl).exists():
            items.append((str(name), str(stl)))
    return items


def render_assembly_grid(parts: list, out_path: Path,
                         size: tuple[int, int] = _GRID_SIZE) -> str | None:
    """Color-coded 2x3 contact sheet of ALL parts placed together (iso/front/back/right/
    top/bottom), so the model can visually verify the fit. ``parts`` is the AssemblyResult
    ``parts`` list (dicts with name + stl_path) or (name, stl_path) tuples. Each body gets a
    distinct color and a legend entry. Returns None if pyvista is unavailable or no part has
    a mesh."""
    pv = _pv()
    if pv is None:
        return None
    items = _coerce_parts(parts)
    if not items:
        return None
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    loaded = []  # (name, mesh, edges, color)
    for i, (name, stl) in enumerate(items):
        mesh, edges = _prep(stl, pv)
        loaded.append((name, mesh, edges, _PART_COLORS[i % len(_PART_COLORS)]))

    p = pv.Plotter(off_screen=True, shape=_GRID_SHAPE, window_size=list(size), border=True)
    for idx, view in enumerate(_VIEWS):
        p.subplot(idx // _GRID_SHAPE[1], idx % _GRID_SHAPE[1])
        p.set_background("white")
        for _name, mesh, edges, color in loaded:
            p.add_mesh(mesh, color=color, smooth_shading=True, specular=0.25,
                       specular_power=15, show_edges=False)
            if edges is not None and edges.n_points:
                p.add_mesh(edges, color="#1c2a36", line_width=1)
        p.add_text(view, font_size=10, color="#26323d")
        _apply_view(p, view)
        p.reset_camera()
        p.camera.zoom(1.1)
    # Legend (color -> part name) in the iso quadrant; non-fatal if the VTK build balks.
    try:
        p.subplot(0, 0)
        p.add_legend(labels=[[n, c] for n, _m, _e, c in loaded],
                     bcolor="white", border=True, size=(0.32, 0.32))
    except Exception:  # noqa: BLE001
        pass
    p.screenshot(str(out_path))
    p.close()
    return str(out_path)
