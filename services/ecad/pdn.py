"""PDN v1 — resistive DC IR-drop solve of power-net copper.

Meshes the copper of ONE power net (zone pours, track segments, pads, via barrels) into a
2.5D resistor network and solves the DC node voltages with a sparse linear solve
(numpy/scipy only — no external field solver).

Model
-----
* Each copper layer carrying the net is rasterised onto a uniform square grid of pitch
  ``resolution_mm``.  Every copper cell is a node; 4-neighbour copper cells are joined by
  a conductance ``G = 1/R_sheet`` (for square cells the aspect ratio cancels, so a uniform
  strip solves to the exact ``R = R_sheet * L/W`` closed form).  Sheet resistance comes
  from the stackup copper thickness: ``R_sheet = rho_Cu / t``.
* Via barrels join the copper layers they span with the classic annular-barrel
  resistance ``R = rho_Cu * L / (pi * (r_drill+plating)^2 - pi * r_drill^2)``, the span
  length taken from the dielectric thicknesses between the spanned copper layers.
* Regulator output pads (auto-detected via ``power_out`` pin etype on the placed
  symbol def, or passed explicitly) are Dirichlet sources held at ``vsrc``.
* Component supply currents (aicad.boardphys/1 ``supply.rails`` metadata;
  overridable per ref) are constant-current sinks injected at the component's pads on
  the net.

Honesty rule (contracts/ecad/boardphys.md) applies: a component with no rail-current
metadata sinks ``None`` — it is REPORTED as unknown coverage, never silently modelled
as 0 A and forgotten.

Outputs
-------
:func:`solve_pdn` returns a report dict with per-component voltage drop vs. the rail's
``voltage_v_min`` tolerance, current-density hotspots (J in A/mm² vs a configurable
limit), and the per-element I²R dissipation map. :func:`i2r_map_block` shapes that map
into the ``pdn`` boardphys sidecar extension (schema ``aicad.pdn/1``) consumed by the
ECAD→thermal handoff; :func:`attach_pdn` merges one or more net results onto
an ``aicad.boardphys/1`` sidecar document.

Deliberate v1 scope: DC only (no plane inductance / AC impedance), arc tracks ignored
(same scope note as drc.py), single net per solve (the return path is a separate solve
of the GND net).
"""
from __future__ import annotations

import math
import time
from typing import Any

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla
from scipy.sparse.csgraph import connected_components

from . import metadata
from .schema import copper_layer_ids, pad_world
from .zonefill import _points_in_polygon, fill_zone

# ── physical constants / defaults ────────────────────────────────────────────────

RHO_CU_OHM_M = 1.724e-8          # annealed copper resistivity at 20 °C
DEFAULT_PLATING_MM = 0.025       # via barrel plating (25 µm ≈ 1 oz hole wall, IPC class 2)
DEFAULT_COPPER_T_MM = 0.035      # 1 oz foil when the stackup carries no thickness
DEFAULT_J_LIMIT_A_MM2 = 30.0     # conservative external-layer current-density ceiling
DEFAULT_RESOLUTION_MM = 0.25
MAX_CELLS_PER_LAYER = 1_200_000  # auto-coarsen guard (keeps demo boards <10 s)
I2R_MAX_CELL_ROWS = 20_000       # sidecar cap; remainder reported as omitted_w

PDN_SCHEMA = "aicad.pdn/1"


def sheet_resistance(thickness_mm: float) -> float:
    """Copper sheet resistance (ohm/square) for a foil thickness in mm."""
    return RHO_CU_OHM_M / (float(thickness_mm) * 1e-3)


def via_resistance(drill_mm: float, length_mm: float,
                   plating_mm: float = DEFAULT_PLATING_MM) -> float:
    """DC resistance of a plated via barrel: rho*L / (annular plated cross-section)."""
    r_in = float(drill_mm) * 1e-3 / 2.0
    r_out = r_in + float(plating_mm) * 1e-3
    area = math.pi * (r_out * r_out - r_in * r_in)
    return RHO_CU_OHM_M * (float(length_mm) * 1e-3) / area


# ── stackup helpers ──────────────────────────────────────────────────────────────

def _copper_thicknesses(board: dict) -> dict[str, float]:
    """layer id -> copper thickness (mm), defaulting honest-but-stated 1 oz."""
    st = board.get("stackup", {})
    n_cu = int(st.get("copper_layers", 2))
    out = {lid: DEFAULT_COPPER_T_MM for lid in copper_layer_ids(n_cu)}
    for lyr in st.get("layers", []):
        if lyr.get("type") == "copper" and lyr.get("id") in out:
            t = lyr.get("thickness")
            if isinstance(t, (int, float)) and t > 0:
                out[lyr["id"]] = float(t)
    return out


def _dielectric_span_mm(board: dict, lyr_a: str, lyr_b: str) -> float:
    """Dielectric distance between two copper layers, from the ordered stackup layer list."""
    layers = board.get("stackup", {}).get("layers", [])
    ids = [l.get("id") for l in layers]
    if lyr_a in ids and lyr_b in ids:
        i, j = sorted((ids.index(lyr_a), ids.index(lyr_b)))
        span = sum(float(layers[k].get("thickness", 0.0) or 0.0) for k in range(i + 1, j))
        if span > 0:
            return span
    # fall back: board thickness split across copper gaps
    n_cu = int(board.get("stackup", {}).get("copper_layers", 2))
    total = float(board.get("stackup", {}).get("thickness", 1.6))
    return total / max(1, n_cu - 1)


# ── net copper collection ────────────────────────────────────────────────────────

def _pad_copper_layers(pad: dict, cu_ids: list[str]) -> list[str]:
    layers = [l for l in pad.get("layers", []) if l in cu_ids]
    if pad.get("type") in ("tht", "connector") and not layers:
        return list(cu_ids)
    return layers


def _net_items(board: dict, net: str) -> dict:
    """Collect the net's copper primitives: tracks, vias, zones, pads (with world pose)."""
    cu_ids = copper_layer_ids(int(board.get("stackup", {}).get("copper_layers", 2)))
    tracks = [t for t in board.get("tracks", [])
              if t.get("net") == net and t.get("kind", "segment") == "segment"]
    vias = [v for v in board.get("vias", []) if v.get("net") == net]
    zones = [z for z in board.get("zones", []) if z.get("net") == net]
    pads = []  # (fp, pad, world_xy, [copper layers])
    for fp in board.get("footprints", []):
        for pad in fp.get("pads", []):
            if pad.get("net") != net:
                continue
            layers = _pad_copper_layers(pad, cu_ids)
            if layers:
                pads.append((fp, pad, pad_world(fp, pad), layers))
    return {"cu_ids": cu_ids, "tracks": tracks, "vias": vias, "zones": zones, "pads": pads}


# ── rasterisation ────────────────────────────────────────────────────────────────

class _Grid:
    """Per-layer boolean copper masks on a shared uniform square grid."""

    def __init__(self, x0: float, y0: float, nx: int, ny: int, res: float,
                 layers: list[str]):
        self.x0, self.y0, self.nx, self.ny, self.res = x0, y0, nx, ny, res
        self.masks = {lid: np.zeros((ny, nx), dtype=bool) for lid in layers}
        # cell-centre coordinate vectors
        self.xs = x0 + (np.arange(nx) + 0.5) * res
        self.ys = y0 + (np.arange(ny) + 0.5) * res

    def cell_of(self, x: float, y: float) -> tuple[int, int] | None:
        ix = int((x - self.x0) // self.res)
        iy = int((y - self.y0) // self.res)
        if 0 <= ix < self.nx and 0 <= iy < self.ny:
            return iy, ix
        return None

    def _sub(self, bx0, by0, bx1, by1):
        ix0 = max(0, int((bx0 - self.x0) // self.res))
        iy0 = max(0, int((by0 - self.y0) // self.res))
        ix1 = min(self.nx, int(math.ceil((bx1 - self.x0) / self.res)) + 1)
        iy1 = min(self.ny, int(math.ceil((by1 - self.y0) / self.res)) + 1)
        return ix0, iy0, ix1, iy1

    def stamp_segment(self, layer: str, p1, p2, width: float) -> None:
        r = width / 2.0
        bx0, bx1 = min(p1[0], p2[0]) - r, max(p1[0], p2[0]) + r
        by0, by1 = min(p1[1], p2[1]) - r, max(p1[1], p2[1]) + r
        ix0, iy0, ix1, iy1 = self._sub(bx0, by0, bx1, by1)
        if ix0 >= ix1 or iy0 >= iy1:
            return
        X, Y = np.meshgrid(self.xs[ix0:ix1], self.ys[iy0:iy1])
        ax, ay = p1
        dx, dy = p2[0] - ax, p2[1] - ay
        L2 = dx * dx + dy * dy
        if L2 <= 1e-18:
            d2 = (X - ax) ** 2 + (Y - ay) ** 2
        else:
            t = np.clip(((X - ax) * dx + (Y - ay) * dy) / L2, 0.0, 1.0)
            d2 = (X - (ax + t * dx)) ** 2 + (Y - (ay + t * dy)) ** 2
        self.masks[layer][iy0:iy1, ix0:ix1] |= d2 <= r * r

    def stamp_disk(self, layer: str, cx: float, cy: float, r: float) -> None:
        ix0, iy0, ix1, iy1 = self._sub(cx - r, cy - r, cx + r, cy + r)
        if ix0 >= ix1 or iy0 >= iy1:
            return
        X, Y = np.meshgrid(self.xs[ix0:ix1], self.ys[iy0:iy1])
        self.masks[layer][iy0:iy1, ix0:ix1] |= \
            (X - cx) ** 2 + (Y - cy) ** 2 <= r * r
        # always claim the centre cell so attachment/via nodes exist even when r < res/2
        c = self.cell_of(cx, cy)
        if c:
            self.masks[layer][c] = True

    def stamp_rect(self, layer: str, cx: float, cy: float, w: float, h: float,
                   angle_deg: float) -> None:
        hw, hh = w / 2.0, h / 2.0
        rad = math.radians(angle_deg)
        ext = abs(hw * math.cos(rad)) + abs(hh * math.sin(rad))
        eyt = abs(hw * math.sin(rad)) + abs(hh * math.cos(rad))
        ix0, iy0, ix1, iy1 = self._sub(cx - ext, cy - eyt, cx + ext, cy + eyt)
        if ix0 >= ix1 or iy0 >= iy1:
            return
        X, Y = np.meshgrid(self.xs[ix0:ix1], self.ys[iy0:iy1])
        c, s = math.cos(-rad), math.sin(-rad)
        lx = (X - cx) * c - (Y - cy) * s
        ly = (X - cx) * s + (Y - cy) * c
        self.masks[layer][iy0:iy1, ix0:ix1] |= (np.abs(lx) <= hw) & (np.abs(ly) <= hh)
        cc = self.cell_of(cx, cy)
        if cc:
            self.masks[layer][cc] = True

    def stamp_polygons(self, layer: str, polys: list) -> None:
        for poly in polys:
            if not poly or len(poly) < 3:
                continue
            xs = [p[0] for p in poly]
            ys = [p[1] for p in poly]
            ix0, iy0, ix1, iy1 = self._sub(min(xs), min(ys), max(xs), max(ys))
            if ix0 >= ix1 or iy0 >= iy1:
                continue
            X, Y = np.meshgrid(self.xs[ix0:ix1], self.ys[iy0:iy1])
            self.masks[layer][iy0:iy1, ix0:ix1] |= _points_in_polygon(X, Y, poly)


def _build_grid(board: dict, items: dict, resolution_mm: float | None) -> _Grid | None:
    pts_x: list[float] = []
    pts_y: list[float] = []
    widths: list[float] = []
    for t in items["tracks"]:
        for p in (t.get("p1", [0, 0]), t.get("p2", [0, 0])):
            pts_x.append(float(p[0])); pts_y.append(float(p[1]))
        widths.append(float(t.get("width", 0.25)))
    for v in items["vias"]:
        at = v.get("at", [0, 0])
        pts_x.append(float(at[0])); pts_y.append(float(at[1]))
    for z in items["zones"]:
        for poly in (z.get("filled_polys") or [z.get("polygon") or []]):
            for p in poly:
                pts_x.append(float(p[0])); pts_y.append(float(p[1]))
    for _fp, pad, (wx, wy), _layers in items["pads"]:
        pts_x.append(wx); pts_y.append(wy)
        size = pad.get("size", [1.0, 1.0])
        widths.append(min(float(size[0]), float(size[1])))
    if not pts_x:
        return None
    res = float(resolution_mm) if resolution_mm else 0.0
    if res <= 0:
        res = min([DEFAULT_RESOLUTION_MM] + [w / 2.0 for w in widths if w > 0])
        res = max(0.1, min(1.0, res))
    margin = 2.0 * res + 1.0
    # snap the origin to a multiple of res so cell centres are deterministic
    # (res*(k+0.5)) regardless of the board's bounding box
    x0 = res * math.floor((min(pts_x) - margin) / res)
    y0 = res * math.floor((min(pts_y) - margin) / res)
    x1 = max(pts_x) + margin
    y1 = max(pts_y) + margin
    # coarsen rather than explode on huge boards
    while ((x1 - x0) / res) * ((y1 - y0) / res) > MAX_CELLS_PER_LAYER:
        res *= 1.5
    nx = max(2, int(math.ceil((x1 - x0) / res)))
    ny = max(2, int(math.ceil((y1 - y0) / res)))
    return _Grid(x0, y0, nx, ny, res, items["cu_ids"])


def _rasterise(board: dict, items: dict, grid: _Grid) -> None:
    for z in items["zones"]:
        layer = z.get("layer")
        if layer not in grid.masks:
            continue
        polys = z.get("filled_polys")
        if not polys:
            try:
                polys = fill_zone(board, z)
            except Exception:  # noqa: BLE001 — fall back to the raw outline
                polys = [z.get("polygon") or []]
        grid.stamp_polygons(layer, polys or [])
    for t in items["tracks"]:
        layer = t.get("layer")
        if layer in grid.masks:
            grid.stamp_segment(layer, t.get("p1", [0, 0]), t.get("p2", [0, 0]),
                               float(t.get("width", 0.25)))
    for fp, pad, (wx, wy), layers in items["pads"]:
        size = pad.get("size", [1.0, 1.0])
        angle = float(fp.get("angle", 0.0)) + float(pad.get("angle", 0.0))
        for layer in layers:
            if layer not in grid.masks:
                continue
            if pad.get("shape") == "circle":
                grid.stamp_disk(layer, wx, wy, float(size[0]) / 2.0)
            else:
                grid.stamp_rect(layer, wx, wy, float(size[0]), float(size[1]), angle)
    for v in items["vias"]:
        at = v.get("at", [0, 0])
        r = float(v.get("diameter", 0.6)) / 2.0
        span = v.get("layers") or ["F.Cu", "B.Cu"]
        for layer in span:
            if layer in grid.masks:
                grid.stamp_disk(layer, float(at[0]), float(at[1]), r)


# ── network assembly + solve ─────────────────────────────────────────────────────

class _Network:
    """Sparse resistor network over the copper grid: node ids, Laplacian, edge list."""

    def __init__(self, board: dict, items: dict, grid: _Grid,
                 plating_mm: float = DEFAULT_PLATING_MM):
        self.grid = grid
        self.thick = _copper_thicknesses(board)
        self.node_of: dict[str, np.ndarray] = {}   # layer -> int array (ny,nx), -1 = no copper
        self.node_layer: list[str] = []
        self.node_iy: list[int] = []
        self.node_ix: list[int] = []
        n = 0
        for lid in items["cu_ids"]:
            mask = grid.masks[lid]
            idx = np.full(mask.shape, -1, dtype=np.int64)
            cnt = int(mask.sum())
            idx[mask] = np.arange(n, n + cnt)
            self.node_of[lid] = idx
            iy, ix = np.nonzero(mask)
            self.node_layer.extend([lid] * cnt)
            self.node_iy.extend(iy.tolist())
            self.node_ix.extend(ix.tolist())
            n += cnt
        self.n = n
        rows: list[np.ndarray] = []
        cols: list[np.ndarray] = []
        gs: list[np.ndarray] = []
        # in-plane edges: square cells -> conductance = 1/R_sheet per neighbour pair
        for lid in items["cu_ids"]:
            g = 1.0 / sheet_resistance(self.thick[lid])
            idx = self.node_of[lid]
            m = grid.masks[lid]
            for (sa, sb) in (((slice(None), slice(0, -1)), (slice(None), slice(1, None))),
                             ((slice(0, -1), slice(None)), (slice(1, None), slice(None)))):
                both = m[sa] & m[sb]
                a = idx[sa][both]
                b = idx[sb][both]
                if a.size:
                    rows.append(a); cols.append(b)
                    gs.append(np.full(a.size, g))
        # via edges: chain across the spanned copper layers
        self.via_edges: list[dict] = []
        board_cu = items["cu_ids"]
        for v in items["vias"]:
            at = v.get("at", [0, 0])
            span = [l for l in (v.get("layers") or ["F.Cu", "B.Cu"]) if l in board_cu]
            span.sort(key=board_cu.index)
            drill = float(v.get("drill", 0.3))
            c = grid.cell_of(float(at[0]), float(at[1]))
            if c is None or len(span) < 2:
                continue
            for la, lb in zip(span[:-1], span[1:]):
                na = int(self.node_of[la][c])
                nb = int(self.node_of[lb][c])
                if na < 0 or nb < 0:
                    continue
                r_via = via_resistance(drill, _dielectric_span_mm(board, la, lb), plating_mm)
                rows.append(np.array([na])); cols.append(np.array([nb]))
                gs.append(np.array([1.0 / r_via]))
                self.via_edges.append({"id": v.get("id"), "at": [float(at[0]), float(at[1])],
                                       "a": na, "b": nb, "r": r_via})
        if rows:
            self.er = np.concatenate(rows)
            self.ec = np.concatenate(cols)
            self.eg = np.concatenate(gs)
        else:
            self.er = np.zeros(0, dtype=np.int64)
            self.ec = np.zeros(0, dtype=np.int64)
            self.eg = np.zeros(0)

    def laplacian(self) -> sp.csr_matrix:
        i = np.concatenate([self.er, self.ec, self.er, self.ec])
        j = np.concatenate([self.ec, self.er, self.er, self.ec])
        v = np.concatenate([-self.eg, -self.eg, self.eg, self.eg])
        return sp.coo_matrix((v, (i, j)), shape=(self.n, self.n)).tocsr()

    def attach(self, layer: str, x: float, y: float, search_mm: float = 1.5) -> int:
        """Nearest copper node on ``layer`` to (x, y); -1 when none within reach."""
        idx = self.node_of.get(layer)
        if idx is None:
            return -1
        c = self.grid.cell_of(x, y)
        if c and idx[c] >= 0:
            return int(idx[c])
        rad = max(1, int(math.ceil(search_mm / self.grid.res)))
        if c is None:
            return -1
        iy, ix = c
        best, best_d = -1, float("inf")
        for dy in range(-rad, rad + 1):
            for dx in range(-rad, rad + 1):
                jy, jx = iy + dy, ix + dx
                if 0 <= jy < self.grid.ny and 0 <= jx < self.grid.nx and idx[jy, jx] >= 0:
                    d = dx * dx + dy * dy
                    if d < best_d:
                        best, best_d = int(idx[jy, jx]), d
        return best


def _solve_dirichlet(L: sp.csr_matrix, src_nodes: list[int], vsrc: float,
                     inj: np.ndarray) -> np.ndarray:
    """Solve L·V = I with V fixed to vsrc on src_nodes (Dirichlet elimination)."""
    n = L.shape[0]
    fixed = np.zeros(n, dtype=bool)
    fixed[src_nodes] = True
    free = np.nonzero(~fixed)[0]
    V = np.full(n, vsrc)
    if free.size == 0:
        return V
    Lff = L[free][:, free]
    Lfs = L[free][:, np.nonzero(fixed)[0]]
    rhs = inj[free] - Lfs @ np.full(int(fixed.sum()), vsrc)
    V[free] = spla.spsolve(Lff.tocsc(), rhs)
    return V


def _pad_nodes(netw: _Network, grid: _Grid, fp: dict, pad: dict,
               layers: list[str]) -> list[int]:
    """All copper nodes under a pad's extent (the pad is the contact patch — treating
    it as a distributed contact avoids fictitious point-injection spreading R)."""
    wx, wy = pad_world(fp, pad)
    size = pad.get("size", [1.0, 1.0])
    r = max(float(size[0]), float(size[1])) / 2.0
    nodes: list[int] = []
    for layer in layers:
        idx = netw.node_of.get(layer)
        if idx is None:
            continue
        ix0, iy0, ix1, iy1 = grid._sub(wx - r, wy - r, wx + r, wy + r)
        if ix0 < ix1 and iy0 < iy1:
            sub = idx[iy0:iy1, ix0:ix1]
            X, Y = np.meshgrid(grid.xs[ix0:ix1], grid.ys[iy0:iy1])
            sel = sub[(sub >= 0) & ((X - wx) ** 2 + (Y - wy) ** 2 <= r * r)]
            nodes.extend(int(n) for n in sel)
        if not nodes:
            nd = netw.attach(layer, wx, wy)
            if nd >= 0:
                nodes.append(nd)
    return sorted(set(nodes))


# ── metadata: sinks + sources ────────────────────────────────────────────────────

def _match_rail(eng: dict, net: str) -> dict | None:
    """Pick the supply rail whose current applies on ``net`` (name match, else the
    only rail). Returns None when the metadata gives no usable rail."""
    rails = ((eng.get("supply") or {}).get("rails")) or []
    rails = [r for r in rails if isinstance(r, dict)]
    if not rails:
        return None
    net_l = str(net).lower().lstrip("+")
    for r in rails:
        name = str(r.get("name") or "").lower().lstrip("+")
        if name and (name == net_l or name == str(net).lower()):
            return r
    if len(rails) == 1:
        return rails[0]
    return None


def _auto_sources(board: dict, net: str, library: Any = None) -> list[tuple[dict, dict]]:
    """(footprint, pad) pairs whose placed symbol drives this net with a power_out pin."""
    lib = metadata._lib(library)
    out = []
    for fp in board.get("footprints", []):
        sdef = lib.resolve(str(fp.get("libid") or "")) if fp.get("libid") else None
        if not sdef:
            continue
        etypes = {str(p.get("number")): p.get("etype") for p in sdef.get("pins", [])}
        for pad in fp.get("pads", []):
            if pad.get("net") != net:
                continue
            pin = str(pad.get("pin") or pad.get("number") or "")
            if etypes.get(pin) == "power_out":
                out.append((fp, pad))
    return out


def guess_net_voltage(board: dict, net: str, library: Any = None) -> float | None:
    """Nominal rail voltage for a net: metadata voltage_v_nom, else parse '+3V3'/'+5V'."""
    for fp in board.get("footprints", []):
        if not any(p.get("net") == net for p in fp.get("pads", [])):
            continue
        rail = _match_rail(metadata.eng_for_footprint(fp, library), net)
        if rail and rail.get("voltage_v_nom") is not None:
            return float(rail["voltage_v_nom"])
    import re
    m = re.fullmatch(r"[+-]?(\d+)V(\d*)", str(net).strip(), flags=re.IGNORECASE)
    if m:
        return float(f"{m.group(1)}.{m.group(2) or 0}")
    return None


# ── the solve ────────────────────────────────────────────────────────────────────

def solve_pdn(board: dict, net: str, *,
              vsrc: float | None = None,
              source_refs: list[str] | None = None,
              sink_overrides: dict[str, float] | None = None,
              resolution_mm: float | None = None,
              j_limit_a_mm2: float = DEFAULT_J_LIMIT_A_MM2,
              plating_mm: float = DEFAULT_PLATING_MM,
              library: Any = None) -> dict:
    """DC IR-drop solve of one power net. Returns a report dict (never raises for
    modelling gaps — ``ok: False`` + ``error`` for unusable inputs).

    * ``vsrc`` — regulator output voltage; default from rail metadata / net name.
    * ``source_refs`` — refs to treat as sources (their pads on the net are held at
      vsrc). Default: auto-detect via ``power_out`` symbol pins.
    * ``sink_overrides`` — {ref: amps} replacing/providing the metadata current.
    """
    t0 = time.time()
    sink_overrides = dict(sink_overrides or {})
    items = _net_items(board, net)
    if not (items["tracks"] or items["zones"] or items["pads"]):
        return {"ok": False, "net": net, "error": f"net {net!r} has no copper on the board"}

    grid = _build_grid(board, items, resolution_mm)
    if grid is None:
        return {"ok": False, "net": net, "error": f"net {net!r} has no geometry to mesh"}
    _rasterise(board, items, grid)
    netw = _Network(board, items, grid, plating_mm=plating_mm)
    if netw.n == 0:
        return {"ok": False, "net": net, "error": f"net {net!r} rasterised to no copper cells"}

    # ---- sources
    src_pairs: list[tuple[dict, dict]] = []
    if source_refs:
        wanted = {str(r) for r in source_refs}
        for fp in board.get("footprints", []):
            if str(fp.get("ref")) in wanted:
                for pad in fp.get("pads", []):
                    if pad.get("net") == net:
                        src_pairs.append((fp, pad))
    else:
        src_pairs = _auto_sources(board, net, library)
    if not src_pairs:
        return {"ok": False, "net": net,
                "error": f"no source found for net {net!r}: no placed symbol drives it with a "
                         "power_out pin — pass source_refs explicitly (regulator/connector ref)"}
    src_refs_used = sorted({str(fp.get("ref")) for fp, _ in src_pairs})

    if vsrc is None:
        vsrc = guess_net_voltage(board, net, library)
    if vsrc is None:
        return {"ok": False, "net": net,
                "error": f"cannot infer the nominal voltage of {net!r} — pass vsrc"}
    vsrc = float(vsrc)

    src_nodes: list[int] = []
    for fp, pad in src_pairs:
        src_nodes.extend(_pad_nodes(netw, grid, fp, pad,
                                    _pad_copper_layers(pad, items["cu_ids"])))
    src_nodes = sorted(set(src_nodes))
    if not src_nodes:
        return {"ok": False, "net": net,
                "error": "source pads did not land on meshed copper (check the routing)"}

    # ---- sinks (per component on the net, minus the sources)
    L = netw.laplacian()
    ncomp, labels = connected_components(L != 0, directed=False)
    src_comp = {labels[nd] for nd in src_nodes}

    inj = np.zeros(netw.n)
    sinks: list[dict] = []
    unknown_sinks: list[str] = []
    by_ref: dict[str, list[tuple[dict, dict]]] = {}
    for fp, pad, _w, _l in items["pads"]:
        ref = str(fp.get("ref"))
        if ref in src_refs_used:
            continue
        by_ref.setdefault(ref, []).append((fp, pad))
    for ref in sorted(by_ref):
        fp = by_ref[ref][0][0]
        eng = metadata.eng_for_footprint(fp, library)
        rail = _match_rail(eng, net)
        i_a = sink_overrides.get(ref)
        if i_a is None and rail is not None:
            i_a = rail.get("current_a_active")
        v_min = rail.get("voltage_v_min") if rail else None
        nodes: list[int] = []
        for f, pad in by_ref[ref]:
            nodes.extend(_pad_nodes(netw, grid, f, pad,
                                    _pad_copper_layers(pad, items["cu_ids"])))
        nodes = sorted(set(nodes))
        entry = {"ref": ref, "i_a": (float(i_a) if i_a is not None else None),
                 "v_min": (float(v_min) if v_min is not None else None),
                 "nodes": nodes, "connected": bool(nodes) and
                 all(labels[nd] in src_comp for nd in nodes)}
        if i_a is None:
            unknown_sinks.append(ref)
        elif entry["connected"] and nodes:
            share = float(i_a) / len(nodes)
            for nd in nodes:
                inj[nd] -= share
        sinks.append(entry)

    # keep only the source-connected component(s) in the solve — dangling copper
    # islands (other pours stubbed to the net) make the Laplacian singular.
    connected_mask = np.isin(labels, list(src_comp))
    V = np.full(netw.n, vsrc)
    sub = np.nonzero(connected_mask)[0]
    remap = -np.ones(netw.n, dtype=np.int64)
    remap[sub] = np.arange(sub.size)
    keep = connected_mask[netw.er]  # both ends share a component, so one check suffices
    er, ec, eg = remap[netw.er[keep]], remap[netw.ec[keep]], netw.eg[keep]
    i = np.concatenate([er, ec, er, ec])
    j = np.concatenate([ec, er, er, ec])
    val = np.concatenate([-eg, -eg, eg, eg])
    Lc = sp.coo_matrix((val, (i, j)), shape=(sub.size, sub.size)).tocsr()
    Vc = _solve_dirichlet(Lc, [int(remap[nd]) for nd in src_nodes], vsrc, inj[sub])
    V[sub] = Vc

    # ---- edge currents, density, I2R
    dv = V[netw.er] - V[netw.ec]
    ie = netw.eg * dv                       # A, signed a->b
    pe = netw.eg * dv * dv                  # W per edge
    cell_p = np.zeros(netw.n)
    np.add.at(cell_p, netw.er, pe / 2.0)
    np.add.at(cell_p, netw.ec, pe / 2.0)
    # per-cell current: half the sum of |I| on incident edges
    cell_i = np.zeros(netw.n)
    np.add.at(cell_i, netw.er, np.abs(ie) / 2.0)
    np.add.at(cell_i, netw.ec, np.abs(ie) / 2.0)
    # in-plane current density J = I_cell / (res * t)  [A/mm^2]
    node_layers = np.array(netw.node_layer)
    t_of_node = np.array([netw.thick[l] for l in netw.node_layer])
    with np.errstate(divide="ignore", invalid="ignore"):
        j_cell = cell_i / (grid.res * t_of_node)
    # exclude Dirichlet cells from hotspot stats (point injection is a mesh artefact)
    stat = np.ones(netw.n, dtype=bool)
    stat[src_nodes] = False
    for s in sinks:
        for nd in s["nodes"]:
            stat[nd] = False
    j_stat = np.where(stat, j_cell, 0.0)
    max_j = float(j_stat.max()) if netw.n else 0.0
    hot_idx = np.argsort(j_stat)[::-1][:50]
    hotspots = []
    for nd in hot_idx:
        if j_stat[nd] < j_limit_a_mm2:
            break
        hotspots.append({
            "layer": netw.node_layer[nd],
            "x_mm": round(float(grid.xs[netw.node_ix[nd]]), 3),
            "y_mm": round(float(grid.ys[netw.node_iy[nd]]), 3),
            "j_a_mm2": round(float(j_stat[nd]), 2),
        })

    # ---- per-component drops
    drops = []
    violations: list[str] = []
    worst = None
    for s in sinks:
        if not s["nodes"]:
            v_pad = None
        else:
            v_pad = float(min(V[nd] for nd in s["nodes"]))
        drop = (vsrc - v_pad) if v_pad is not None else None
        delivered_ok = None
        if v_pad is not None and s["v_min"] is not None:
            delivered_ok = v_pad >= s["v_min"]
            if not delivered_ok and s["i_a"] is not None:
                violations.append(
                    f"{s['ref']}: delivered {v_pad:.3f} V < rail min {s['v_min']:.3f} V "
                    f"(drop {drop*1000:.1f} mV at {s['i_a']:.3f} A)")
        if not s["connected"] and s["i_a"] is not None:
            violations.append(f"{s['ref']}: pads not connected to the source copper "
                              f"({net}) — open PDN path")
        d = {"ref": s["ref"], "i_a": s["i_a"], "v_pad": (round(v_pad, 4) if v_pad is not None else None),
             "drop_v": (round(drop, 5) if drop is not None else None),
             "v_min": s["v_min"], "within_tolerance": delivered_ok,
             "connected": s["connected"]}
        drops.append(d)
        if drop is not None and (worst is None or drop > worst["drop_v"]):
            worst = d
    if hotspots:
        h0 = hotspots[0]
        violations.append(
            f"current density exceeds {j_limit_a_mm2:.1f} A/mm² in {len(hotspots)}+ "
            f"cell(s) — worst {h0['j_a_mm2']:.1f} A/mm² on {h0['layer']} at "
            f"({h0['x_mm']}, {h0['y_mm']}) mm")

    # ---- I2R map (thermal handoff seam — see contracts/ecad/boardphys.md `pdn`)
    via_p = []
    for ve in netw.via_edges:
        dvv = V[ve["a"]] - V[ve["b"]]
        p = dvv * dvv / ve["r"]
        # the via edge power was already folded into cell_p halves; report it separately
        # AND remove it from the two end cells so the map does not double-count.
        cell_p[ve["a"]] -= p / 2.0
        cell_p[ve["b"]] -= p / 2.0
        via_p.append({"id": ve["id"], "at": [round(ve["at"][0], 3), round(ve["at"][1], 3)],
                      "i_a": round(float(abs(dvv) / ve["r"]), 4),
                      "r_ohm": round(ve["r"], 6), "p_w": float(p)})
    cell_p = np.maximum(cell_p, 0.0)
    total_w = float(cell_p.sum() + sum(v["p_w"] for v in via_p))
    order = np.argsort(cell_p)[::-1]
    rows = []
    kept_w = 0.0
    for nd in order[:I2R_MAX_CELL_ROWS]:
        p = float(cell_p[nd])
        if p <= 1e-12:
            break
        rows.append([netw.node_layer[nd],
                     round(float(grid.xs[netw.node_ix[nd]]), 3),
                     round(float(grid.ys[netw.node_iy[nd]]), 3),
                     round(p, 9)])
        kept_w += p
    i2r_map = {
        "cell_size_mm": round(grid.res, 4),
        "copper_thickness_mm": {l: netw.thick[l] for l in items["cu_ids"]},
        "columns": ["layer", "x_mm", "y_mm", "p_w"],
        "cells": rows,
        "vias": [{**v, "p_w": round(v["p_w"], 9)} for v in via_p],
        "total_w": round(total_w, 9),
        "omitted_w": round(max(0.0, total_w - kept_w - sum(v["p_w"] for v in via_p)), 9),
    }

    return {
        "ok": True, "net": net, "vsrc": vsrc,
        "sources": src_refs_used,
        "resolution_mm": round(grid.res, 4),
        "n_nodes": int(netw.n),
        "drops": drops,
        "worst_drop": worst,
        "max_j_a_mm2": round(max_j, 2),
        "j_limit_a_mm2": float(j_limit_a_mm2),
        "hotspots": hotspots,
        "violations": violations,
        "unknown_sinks": unknown_sinks,   # refs with no current metadata (honest gap)
        "i2r_map": i2r_map,
        "elapsed_s": round(time.time() - t0, 3),
    }


# ── boardphys sidecar extension (consumed by the thermal handoff) ────────────────

def pdn_block(results: list[dict]) -> dict:
    """Shape solved nets into the boardphys `pdn` extension block (aicad.pdn/1)."""
    nets = []
    for r in results:
        if not r.get("ok"):
            continue
        nets.append({
            "net": r["net"], "vsrc": r["vsrc"], "sources": r["sources"],
            "worst_drop": r["worst_drop"], "drops": r["drops"],
            "max_j_a_mm2": r["max_j_a_mm2"], "j_limit_a_mm2": r["j_limit_a_mm2"],
            "hotspots": r["hotspots"], "violations": r["violations"],
            "unknown_sinks": r["unknown_sinks"],
            "i2r_map": r["i2r_map"],
        })
    return {"schema": PDN_SCHEMA, "generated_by": "services.ecad.pdn.solve_pdn", "nets": nets}


def attach_pdn(boardphys: dict, results: list[dict]) -> dict:
    """Merge PDN results onto an aicad.boardphys/1 sidecar dict (in place; returned).

    Adds/replaces the optional top-level ``pdn`` key — an ADDITIVE extension per the
    contract's append-only rule (contracts/ecad/boardphys.md)."""
    boardphys["pdn"] = pdn_block(results)
    return boardphys
