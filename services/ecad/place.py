"""Connectivity-aware deterministic PCB placer (Wave 2a E1).

Public entry points consumed by Wave 2b ``board_from_schematic``:

  * :func:`place_board` — pure: deep-copies the board, seats every footprint, returns
    ``{"doc": <placed board>, "report": {...}}``.
  * :func:`autoplace` — convenience wrapper that applies the result to a live
    :class:`engine.ecad.session.EcadSession` via :func:`session.swap_doc`.

Algorithm (deterministic for a fixed ``opts['seed']``, default 0):

  1. Classify footprints: locked/hinted, connectors (edge), ICs/hosts, decoupling caps,
     remaining passives/other.
  2. Cluster non-locked components by shared-net degree (weighted adjacency over nets that
     are not pure power rails used only as global flood fills — GND/VSS still count when a
     two-pin passive shares them with an IC, so decoupling affinity is preserved).
  3. Seed a board outline bbox (from ``outline`` graphics if present, else a default size
     scaled to component count) and place:
       - locked / hint positions first (honoured exactly; locked never moves),
       - ICs near the geometric centre, ordered by cluster rank then ref,
       - each decoupling capacitor adjacent to the IC power pad it decouples
         (C between a power rail and GND that is shared with an IC power pin),
       - connectors / headers / USB / GPS on the outer board edge,
       - remaining passives near the highest-degree neighbour already placed.
  4. Enforce no courtyard overlap by spreading on collision (deterministic spiral search
     keyed by seed + ref).
  5. Report ratsnest length, moved count, residual overlaps, and cluster stats.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any

from services.ecad import schema
from services.ecad.drc import ratsnest

# ── constants ──────────────────────────────────────────────────────────────────

_DEFAULT_BOARD_W = 80.0
_DEFAULT_BOARD_H = 60.0
_EDGE_INSET_MM = 2.0
_DECAP_OFFSET_MM = 2.5          # radial offset from IC power pad to cap centre
_DECAP_MAX_RADIUS_MM = 5.0      # acceptance radius used by tests / report
_SPIRAL_STEP_MM = 0.5
_SPIRAL_MAX_RINGS = 80
_CLEARANCE_PAD_MM = 0.15        # extra gap beyond courtyard AABB
_GRID_PITCH = 12.7              # matches sync.py baseline for comparison helpers
_GRID_COLS = 8

_GND_RE = re.compile(
    r"^(GND|VSS|AGND|DGND|PGND|GNDA|GNDD|0|VSSA)$", re.IGNORECASE)
_RAIL_RE = re.compile(
    r"^(V(CC|DD|BAT|IN|OUT|SYS|BUS)\w*|\+?\d+V\d*|\d+V\d+|"
    r"\+\d+(\.\d+)?V?|VCORE\w*|VIO\w*|AVDD\w*|DVDD\w*|VBAT|VDDA)$",
    re.IGNORECASE)
_CONN_RE = re.compile(
    r"(usb|header|conn|connector|jack|plug|gps|antenna|swd|jtag|edge)",
    re.IGNORECASE)
_CONN_REF_RE = re.compile(r"^(J|P|X|CON|USB|GPS)\d*", re.IGNORECASE)
_CAP_REF_RE = re.compile(r"^C\d", re.IGNORECASE)
_IC_REF_RE = re.compile(r"^(U|IC|MCU|FPGA|Q)\d*", re.IGNORECASE)
_IC_LIB_RE = re.compile(
    r"(mcu|fpga|cpu|soc|regulator|opamp|amplifier|sensor|imu|qfp|qfn|soic|sop|tssop|bga)",
    re.IGNORECASE)


# ── geometry helpers ───────────────────────────────────────────────────────────

def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _fp_extent_local(fp: dict) -> tuple[float, float, float, float]:
    """Local-frame AABB ``(xmin, ymin, xmax, ymax)`` of courtyard / pads."""
    xs: list[float] = []
    ys: list[float] = []
    for g in fp.get("graphics", []) or []:
        if g.get("layer") not in ("F.CrtYd", "B.CrtYd"):
            continue
        gtype = g.get("type")
        if gtype == "line":
            for key in ("p1", "p2"):
                p = g.get(key)
                if p and len(p) >= 2:
                    xs.append(float(p[0])); ys.append(float(p[1]))
        elif gtype == "rect":
            p1 = g.get("p1") or g.get("start")
            p2 = g.get("p2") or g.get("end")
            if p1 and p2:
                xs.extend([float(p1[0]), float(p2[0])])
                ys.extend([float(p1[1]), float(p2[1])])
        elif gtype == "circle":
            c = g.get("center") or [0.0, 0.0]
            r = float(g.get("radius", 0.0))
            xs.extend([float(c[0]) - r, float(c[0]) + r])
            ys.extend([float(c[1]) - r, float(c[1]) + r])
        elif gtype == "poly" and g.get("points"):
            for p in g["points"]:
                xs.append(float(p[0])); ys.append(float(p[1]))
    if not xs:
        for pad in fp.get("pads", []) or []:
            px, py = float(pad["pos"][0]), float(pad["pos"][1])
            size = pad.get("size") or [0.5, 0.5]
            w, h = float(size[0]), float(size[1] if len(size) > 1 else size[0])
            xs.extend([px - w / 2.0, px + w / 2.0])
            ys.extend([py - h / 2.0, py + h / 2.0])
    if not xs:
        return (-1.0, -1.0, 1.0, 1.0)
    return (min(xs), min(ys), max(xs), max(ys))


def _fp_half_size(fp: dict) -> tuple[float, float]:
    xmin, ymin, xmax, ymax = _fp_extent_local(fp)
    return ((xmax - xmin) / 2.0 + _CLEARANCE_PAD_MM,
            (ymax - ymin) / 2.0 + _CLEARANCE_PAD_MM)


def _aabb_overlap(ax: float, ay: float, ahx: float, ahy: float,
                  bx: float, by: float, bhx: float, bhy: float) -> bool:
    return (abs(ax - bx) < (ahx + bhx)) and (abs(ay - by) < (ahy + bhy))


def _ratsnest_len_mm(board: dict) -> float:
    total = 0.0
    for aw in ratsnest(board):
        p1, p2 = aw["p1"], aw["p2"]
        total += math.hypot(float(p1[0]) - float(p2[0]),
                            float(p1[1]) - float(p2[1]))
    return float(total)


def _board_bbox(board: dict, n_fp: int) -> tuple[float, float, float, float]:
    """Return ``(xmin, ymin, xmax, ymax)`` of the placeable board area."""
    shapes = (board.get("outline") or {}).get("shapes") or []
    xs: list[float] = []
    ys: list[float] = []
    for sh in shapes:
        for key in ("p1", "p2", "start", "end", "center"):
            p = sh.get(key) if isinstance(sh, dict) else None
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                xs.append(float(p[0])); ys.append(float(p[1]))
        for p in (sh.get("points") or []) if isinstance(sh, dict) else []:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                xs.append(float(p[0])); ys.append(float(p[1]))
    if xs and ys:
        return (min(xs), min(ys), max(xs), max(ys))
    # Default size grows gently with part count so edge placement has room.
    cols = max(4, int(math.ceil(math.sqrt(max(n_fp, 1)))))
    w = max(_DEFAULT_BOARD_W, cols * 8.0)
    h = max(_DEFAULT_BOARD_H, cols * 6.0)
    ox, oy = board.get("origin") or [0.0, 0.0]
    return (float(ox), float(oy), float(ox) + w, float(oy) + h)


# ── classification ─────────────────────────────────────────────────────────────

def _is_gnd_net(name: Any) -> bool:
    return isinstance(name, str) and bool(_GND_RE.match(name.strip()))


def _is_rail_net(name: Any) -> bool:
    return isinstance(name, str) and bool(_RAIL_RE.match(name.strip()))


def _is_power_net(name: Any) -> bool:
    return _is_gnd_net(name) or _is_rail_net(name)


def _fp_nets(fp: dict) -> set[str]:
    out: set[str] = set()
    for pad in fp.get("pads", []) or []:
        n = pad.get("net")
        if isinstance(n, str) and n:
            out.add(n)
    return out


def _is_cap(fp: dict) -> bool:
    libid = str(fp.get("libid") or "")
    fpid = str(fp.get("fpid") or "")
    ref = str(fp.get("ref") or "")
    stem = libid.split(":")[-1] if libid else ""
    if stem and (stem == "C" or stem.startswith(("C_", "CP", "C0", "C1"))):
        return True
    if "Capacitor" in fpid or fpid.startswith("C_"):
        return True
    return bool(_CAP_REF_RE.match(ref))


def _is_connector(fp: dict) -> bool:
    libid = str(fp.get("libid") or "")
    fpid = str(fp.get("fpid") or "")
    ref = str(fp.get("ref") or "")
    value = str(fp.get("value") or "")
    blob = " ".join([libid, fpid, ref, value])
    if _CONN_RE.search(blob):
        return True
    if _CONN_REF_RE.match(ref):
        return True
    return False


def _is_ic(fp: dict) -> bool:
    if _is_cap(fp) or _is_connector(fp):
        return False
    libid = str(fp.get("libid") or "")
    fpid = str(fp.get("fpid") or "")
    ref = str(fp.get("ref") or "")
    if _IC_REF_RE.match(ref):
        return True
    if _IC_LIB_RE.search(libid) or _IC_LIB_RE.search(fpid):
        return True
    # Many pads + non-passive footprint ⇒ treat as host IC.
    n_pads = len(fp.get("pads") or [])
    return n_pads >= 6


def _is_decoupling_cap(fp: dict) -> bool:
    """True if this is a two-terminal cap between a power rail and GND."""
    if not _is_cap(fp):
        return False
    nets = [p.get("net") for p in (fp.get("pads") or []) if p.get("net")]
    if len(nets) < 2:
        return False
    a, b = nets[0], nets[1]
    return ((_is_rail_net(a) and _is_gnd_net(b))
            or (_is_rail_net(b) and _is_gnd_net(a)))


def _cap_rail_net(fp: dict) -> str | None:
    for p in fp.get("pads") or []:
        n = p.get("net")
        if _is_rail_net(n):
            return str(n)
    return None


def _locked_or_hint(fp: dict) -> tuple[bool, list[float] | None]:
    """Return ``(locked, hint_xy)``. Locked positions are never moved; hints preferred."""
    locked = bool(fp.get("locked") or fp.get("fixed") or fp.get("place_locked"))
    hint = (fp.get("place_hint") or fp.get("hint_pos") or fp.get("hint")
            or (fp.get("fields") or {}).get("place_hint"))
    if isinstance(hint, (list, tuple)) and len(hint) >= 2:
        return locked, [float(hint[0]), float(hint[1])]
    # A non-origin pos with locked flag is treated as the locked position.
    pos = fp.get("pos")
    if locked and isinstance(pos, (list, tuple)) and len(pos) >= 2:
        return True, [float(pos[0]), float(pos[1])]
    return locked, None


# ── clustering ─────────────────────────────────────────────────────────────────

def _shared_net_weight(nets_a: set[str], nets_b: set[str]) -> float:
    shared = nets_a & nets_b
    if not shared:
        return 0.0
    w = 0.0
    for n in shared:
        if _is_gnd_net(n):
            w += 0.25
        elif _is_rail_net(n):
            w += 1.5
        else:
            w += 1.0
    return w


def _cluster_order(fps: list[dict], seed: int) -> list[str]:
    """Deterministic order of footprint ids by connectivity cluster rank.

    Builds a weighted adjacency on shared nets, picks seeds by descending degree
    (tie-break ref, then id), then BFS/Prim-style growth so tightly-coupled parts
    are visited contiguously. ``seed`` only perturbs equal-degree ties via a
    stable hash of (seed, id) — never random.
    """
    by_id = {fp.get("id") or fp.get("ref") or str(i): fp
             for i, fp in enumerate(fps)}
    ids = sorted(by_id.keys(), key=lambda i: (str(by_id[i].get("ref") or ""), i))
    nets_of = {i: _fp_nets(by_id[i]) for i in ids}
    deg: dict[str, float] = {i: 0.0 for i in ids}
    adj: dict[str, list[tuple[float, str]]] = {i: [] for i in ids}
    for a_i, a in enumerate(ids):
        for b in ids[a_i + 1:]:
            w = _shared_net_weight(nets_of[a], nets_of[b])
            if w <= 0.0:
                continue
            deg[a] += w
            deg[b] += w
            adj[a].append((w, b))
            adj[b].append((w, a))
    for i in ids:
        adj[i].sort(key=lambda t: (-t[0], str(by_id[t[1]].get("ref") or ""), t[1]))

    def _tie(i: str) -> tuple:
        # seed-stable secondary key
        h = (hash((seed, i)) & 0xFFFFFFFF)
        return (-deg[i], str(by_id[i].get("ref") or ""), h, i)

    remaining = set(ids)
    order: list[str] = []
    while remaining:
        start = min(remaining, key=_tie)
        # Prim growth from start
        frontier: list[tuple[float, str, str]] = []  # (-w, ref, id)
        visited = {start}
        order.append(start)
        remaining.discard(start)
        for w, nb in adj[start]:
            if nb in remaining:
                frontier.append((-w, str(by_id[nb].get("ref") or ""), nb))
        frontier.sort()
        while frontier:
            _, _, cur = frontier.pop(0)
            if cur not in remaining:
                continue
            visited.add(cur)
            order.append(cur)
            remaining.discard(cur)
            for w, nb in adj[cur]:
                if nb in remaining:
                    frontier.append((-w, str(by_id[nb].get("ref") or ""), nb))
            frontier.sort()
    return order


# ── placement core ─────────────────────────────────────────────────────────────

def _power_pads_of(fp: dict) -> list[tuple[str, tuple[float, float]]]:
    """``(net, world_xy)`` for pads on a power rail (not GND)."""
    out: list[tuple[str, tuple[float, float]]] = []
    for pad in fp.get("pads") or []:
        net = pad.get("net")
        if not _is_rail_net(net):
            continue
        try:
            wx, wy = schema.pad_world(fp, pad)
        except Exception:  # noqa: BLE001
            pos = fp.get("pos") or [0.0, 0.0]
            wx, wy = float(pos[0]), float(pos[1])
        out.append((str(net), (float(wx), float(wy))))
    out.sort(key=lambda t: (t[0], t[1][0], t[1][1]))
    return out


def _set_pos(fp: dict, xy: list[float]) -> None:
    fp["pos"] = [float(xy[0]), float(xy[1])]


def _spiral_candidates(cx: float, cy: float, seed: int, key: str,
                       step: float = _SPIRAL_STEP_MM,
                       rings: int = _SPIRAL_MAX_RINGS):
    """Yield candidate positions in a deterministic spiral around (cx, cy)."""
    yield (cx, cy)
    # Phase offset from seed+key so different parts don't all try the same ray first.
    phase = ((hash((seed, key)) & 0xFFFF) / 0xFFFF) * 2.0 * math.pi
    for ring in range(1, rings + 1):
        r = ring * step
        n = max(6, int(round(2.0 * math.pi * ring)))
        for k in range(n):
            ang = phase + (2.0 * math.pi * k) / n
            yield (cx + r * math.cos(ang), cy + r * math.sin(ang))


def _collides(xy: tuple[float, float], hx: float, hy: float,
              placed: list[tuple[float, float, float, float]]) -> bool:
    x, y = xy
    for px, py, phx, phy in placed:
        if _aabb_overlap(x, y, hx, hy, px, py, phx, phy):
            return True
    return False


def _find_free(cx: float, cy: float, hx: float, hy: float,
               placed: list[tuple[float, float, float, float]],
               seed: int, key: str,
               bbox: tuple[float, float, float, float] | None = None,
               prefer_inside: bool = True) -> tuple[float, float]:
    xmin, ymin, xmax, ymax = bbox if bbox else (-1e9, -1e9, 1e9, 1e9)
    best_outside: tuple[float, float] | None = None
    for (x, y) in _spiral_candidates(cx, cy, seed, key):
        if _collides((x, y), hx, hy, placed):
            continue
        inside = (xmin + hx <= x <= xmax - hx) and (ymin + hy <= y <= ymax - hy)
        if prefer_inside and not inside:
            if best_outside is None:
                best_outside = (x, y)
            continue
        return (x, y)
    if best_outside is not None:
        return best_outside
    # Last resort: push far along +x by part index hash (still deterministic).
    off = 5.0 + (hash((seed, key)) & 0xFF) * 0.1
    return (cx + off, cy)


def _edge_slot(bbox: tuple[float, float, float, float], index: int, n_edge: int,
               hx: float, hy: float, seed: int) -> tuple[float, float]:
    """Deterministic position on the board perimeter for the *index*-th connector.

    The footprint centre is kept close to the outer edge (half-size + small inset
    along the outward normal) so connectors read as edge-mounted regardless of
    body size.
    """
    xmin, ymin, xmax, ymax = bbox
    # Usable perimeter along the board edge midlines (centres travel here).
    # Keep a small corner clearance so large parts don't hang past the corner.
    corner = max(hx, hy, _EDGE_INSET_MM)
    w = max(xmax - xmin - 2.0 * corner, 1.0)
    h = max(ymax - ymin - 2.0 * corner, 1.0)
    peri = 2.0 * (w + h)
    jitter = ((seed * 17 + index * 3) % 1000) / 1000.0 * (peri / max(n_edge, 1)) * 0.05
    s = (index + 0.5) / max(n_edge, 1) * peri + jitter
    s = s % peri
    # Centres sit half-extent inside the board from each edge.
    if s <= w:
        return (xmin + corner + s, ymin + hy)
    s -= w
    if s <= h:
        return (xmax - hx, ymin + corner + s)
    s -= h
    if s <= w:
        return (xmax - corner - s, ymax - hy)
    s -= w
    return (xmin + hx, ymax - corner - s)


def _count_courtyard_overlaps(board: dict) -> int:
    """AABB courtyard overlap count (fast local check; DFM is used in tests)."""
    fps = board.get("footprints") or []
    boxes: list[tuple[float, float, float, float, str]] = []
    for fp in fps:
        pos = fp.get("pos") or [0.0, 0.0]
        hx, hy = _fp_half_size(fp)
        boxes.append((float(pos[0]), float(pos[1]), hx, hy,
                      str(fp.get("id") or fp.get("ref") or "")))
    n = 0
    for i in range(len(boxes)):
        ax, ay, ahx, ahy, _ = boxes[i]
        for j in range(i + 1, len(boxes)):
            bx, by, bhx, bhy, _ = boxes[j]
            if _aabb_overlap(ax, ay, ahx, ahy, bx, by, bhx, bhy):
                n += 1
    return n


def _dfm_courtyard_overlaps(board: dict) -> int:
    try:
        from services.mcp import core_bridge as cb
        viols = cb.call("ecad_dfm", {"board": board}).get("violations", [])
    except Exception:  # noqa: BLE001 — core-less fallback: the local courtyard scan
        return _count_courtyard_overlaps(board)
    return sum(1 for v in viols if v.get("rule") == "courtyard_overlap")


# ── public API ─────────────────────────────────────────────────────────────────

def place_board(doc: dict, opts: dict | None = None) -> dict:
    """Connectivity-aware deterministic placement.

    Pure: *doc* is never mutated. Returns
    ``{"doc": <new board>, "report": {"ratsnest_len_mm", "moved", "overlaps", ...}}``.

    ``opts`` keys (all optional):

      * ``seed`` (int, default 0) — deterministic tie-break seed
      * ``board_size`` ([w, h]) — override default placeable area when no outline
      * ``decap_offset_mm`` (float) — radial offset of decap from IC power pad
      * ``skip_locked`` (bool, default True) — honour locked/hint positions
    """
    opts = dict(opts or {})
    seed = int(opts.get("seed", 0))
    decap_off = float(opts.get("decap_offset_mm", _DECAP_OFFSET_MM))
    honour_locked = bool(opts.get("skip_locked", True))

    work = copy.deepcopy(doc)
    if not schema.is_board(work):
        return {
            "doc": work,
            "report": {
                "ratsnest_len_mm": 0.0, "moved": 0, "overlaps": 0,
                "n_footprints": 0, "seed": seed, "error": "not a board document",
            },
        }

    fps: list[dict] = list(work.get("footprints") or [])
    n_fp = len(fps)
    if n_fp == 0:
        return {
            "doc": work,
            "report": {
                "ratsnest_len_mm": 0.0, "moved": 0, "overlaps": 0,
                "n_footprints": 0, "seed": seed,
            },
        }

    # Snapshot original positions for "moved" accounting.
    orig_pos = {
        (fp.get("id") or fp.get("ref") or str(i)): list(fp.get("pos") or [0.0, 0.0])
        for i, fp in enumerate(fps)
    }

    if opts.get("board_size") and len(opts["board_size"]) >= 2:
        ox, oy = work.get("origin") or [0.0, 0.0]
        bbox = (float(ox), float(oy),
                float(ox) + float(opts["board_size"][0]),
                float(oy) + float(opts["board_size"][1]))
    else:
        bbox = _board_bbox(work, n_fp)
    xmin, ymin, xmax, ymax = bbox
    cx = 0.5 * (xmin + xmax)
    cy = 0.5 * (ymin + ymax)

    # Index helpers.
    id_of = {id(fp): (fp.get("id") or fp.get("ref") or f"fp{i}")
             for i, fp in enumerate(fps)}
    by_key: dict[str, dict] = {}
    for i, fp in enumerate(fps):
        by_key[id_of[id(fp)]] = fp

    # Classify.
    locked_fps: list[dict] = []
    connectors: list[dict] = []
    ics: list[dict] = []
    decaps: list[dict] = []
    others: list[dict] = []
    for fp in fps:
        locked, hint = _locked_or_hint(fp)
        if honour_locked and (locked or hint is not None):
            # Stash hint onto a private field for the seat pass.
            if hint is not None:
                fp["_place_xy"] = list(hint)
            locked_fps.append(fp)
            continue
        if _is_connector(fp):
            connectors.append(fp)
        elif _is_decoupling_cap(fp):
            decaps.append(fp)
        elif _is_ic(fp):
            ics.append(fp)
        else:
            others.append(fp)

    # Deterministic sort within each class.
    def _ref_key(fp: dict) -> tuple:
        return (str(fp.get("ref") or ""), str(fp.get("id") or ""))

    connectors.sort(key=_ref_key)
    ics.sort(key=_ref_key)
    decaps.sort(key=_ref_key)
    others.sort(key=_ref_key)
    locked_fps.sort(key=_ref_key)

    # Connectivity order across movable (non-locked) parts for residual seating.
    movable = ics + decaps + connectors + others
    cluster_ids = _cluster_order(movable, seed) if movable else []
    cluster_rank = {k: i for i, k in enumerate(cluster_ids)}

    placed_boxes: list[tuple[float, float, float, float]] = []  # x,y,hx,hy
    placed_keys: set[str] = set()
    decap_targets: dict[str, tuple[float, float]] = {}  # cap key -> target pad world

    def _seat(fp: dict, xy: tuple[float, float], *, force: bool = False) -> None:
        key = id_of[id(fp)]
        hx, hy = _fp_half_size(fp)
        if force:
            x, y = xy
        else:
            x, y = _find_free(xy[0], xy[1], hx, hy, placed_boxes, seed, key, bbox)
        _set_pos(fp, [x, y])
        placed_boxes.append((x, y, hx, hy))
        placed_keys.add(key)

    # 1) Locked / hinted first — honour exact position (force seat).
    for fp in locked_fps:
        if "_place_xy" in fp:
            xy = (float(fp["_place_xy"][0]), float(fp["_place_xy"][1]))
            del fp["_place_xy"]
        else:
            pos = fp.get("pos") or [0.0, 0.0]
            xy = (float(pos[0]), float(pos[1]))
        _seat(fp, xy, force=True)

    # 2) ICs near centre, ordered by cluster rank then ref.
    ics_ordered = sorted(ics, key=lambda f: (
        cluster_rank.get(id_of[id(f)], 10**9), _ref_key(f)))
    # Arrange ICs on a small ring around the board centre.
    for i, fp in enumerate(ics_ordered):
        if len(ics_ordered) == 1:
            target = (cx, cy)
        else:
            ang = (2.0 * math.pi * i) / len(ics_ordered) + (seed % 7) * 0.01
            ring = min(12.0, 0.2 * min(xmax - xmin, ymax - ymin))
            target = (cx + ring * math.cos(ang), cy + ring * math.sin(ang))
        _seat(fp, target)

    # Map rail -> list of IC power pad world positions (after IC seating).
    # Include locked ICs too — a locked MCU still owns the rails its decaps need.
    rail_pads: dict[str, list[tuple[float, float, str]]] = {}
    for fp in list(ics_ordered) + [f for f in locked_fps if _is_ic(f)]:
        key = id_of[id(fp)]
        for net, pxy in _power_pads_of(fp):
            rail_pads.setdefault(net, []).append((pxy[0], pxy[1], key))
    for net in rail_pads:
        rail_pads[net].sort()

    # 3) Decoupling caps adjacent to the IC power pin they share.
    # Assign each cap to the nearest unused power pad on its rail (1:1 greedy, sorted).
    used_pad_idx: dict[str, int] = {n: 0 for n in rail_pads}
    for fp in decaps:
        key = id_of[id(fp)]
        rail = _cap_rail_net(fp)
        target = (cx, cy)
        if rail and rail in rail_pads and rail_pads[rail]:
            pads = rail_pads[rail]
            idx = used_pad_idx.get(rail, 0) % len(pads)
            used_pad_idx[rail] = idx + 1
            px, py, ic_key = pads[idx]
            # Offset outward from IC centre so the cap sits beside the pin, not on it.
            ic_fp = by_key.get(ic_key)
            if ic_fp is not None:
                icx, icy = float(ic_fp["pos"][0]), float(ic_fp["pos"][1])
                dx, dy = px - icx, py - icy
                norm = math.hypot(dx, dy) or 1.0
                # Place just outside the pad, radially away from IC body.
                target = (px + decap_off * dx / norm, py + decap_off * dy / norm)
            else:
                target = (px + decap_off, py)
            decap_targets[key] = (px, py)
        else:
            # Fallback: near highest-degree already-placed neighbour.
            nets = _fp_nets(fp)
            best = None  # (weight, ref, x, y) — max weight, then min ref
            for other in fps:
                ok = id_of[id(other)]
                if ok not in placed_keys:
                    continue
                w = _shared_net_weight(nets, _fp_nets(other))
                if w <= 0:
                    continue
                op = other.get("pos") or [0.0, 0.0]
                cand = (w, str(other.get("ref") or ""), float(op[0]), float(op[1]))
                if best is None or (cand[0], cand[1]) > (best[0], best[1]):
                    best = cand
            if best is not None:
                target = (best[2] + decap_off, best[3])
        _seat(fp, target)

    # 4) Connectors on the outer edge. Prefer the edge slot; only spiral along the
    #    perimeter if the nominal seat collides (search still starts at the slot).
    n_edge = max(len(connectors), 1)
    for i, fp in enumerate(connectors):
        hx, hy = _fp_half_size(fp)
        target = _edge_slot(bbox, i, n_edge, hx, hy, seed)
        # Soft-force: try free near the edge first; if spiral drifts inside, snap
        # back to the closest edge of the bbox after seating.
        _seat(fp, target)
        pos = fp.get("pos") or [target[0], target[1]]
        x, y = float(pos[0]), float(pos[1])
        # Snap to nearest edge if collision resolution pushed the part inland.
        d_left, d_right = abs(x - xmin), abs(x - xmax)
        d_bot, d_top = abs(y - ymin), abs(y - ymax)
        edge_dist = min(d_left, d_right, d_bot, d_top)
        edge_budget = max(hx, hy) + _EDGE_INSET_MM + 1.0
        if edge_dist > edge_budget:
            # Re-seat forced on the nominal edge slot (may overlap — rare; DFM will count).
            # Prefer a free slot along the same edge first.
            if d_left == edge_dist:
                snap = (xmin + hx, y)
            elif d_right == edge_dist:
                snap = (xmax - hx, y)
            elif d_bot == edge_dist:
                snap = (x, ymin + hy)
            else:
                snap = (x, ymax - hy)
            # Remove the just-added box and re-seat forced near the edge.
            if placed_boxes:
                placed_boxes.pop()
            placed_keys.discard(id_of[id(fp)])
            _seat(fp, snap, force=False)

    # 5) Remaining passives / other — near best already-placed neighbour by net weight.
    others_ordered = sorted(others, key=lambda f: (
        cluster_rank.get(id_of[id(f)], 10**9), _ref_key(f)))
    for fp in others_ordered:
        key = id_of[id(fp)]
        nets = _fp_nets(fp)
        best = None  # (weight, -dist_proxy, x, y)
        for other in fps:
            ok = id_of[id(other)]
            if ok not in placed_keys:
                continue
            w = _shared_net_weight(nets, _fp_nets(other))
            if w <= 0:
                continue
            op = other.get("pos") or [0.0, 0.0]
            cand = (w, str(other.get("ref") or ""), float(op[0]), float(op[1]))
            if best is None or cand > best[:2] + best[2:]:
                # compare weight then ref for stability
                if best is None or (cand[0], cand[1]) > (best[0], best[1]):
                    best = cand
        if best is not None:
            # Small deterministic offset so passives fan around their anchor.
            ang = ((hash((seed, key)) & 0xFFFF) / 0xFFFF) * 2.0 * math.pi
            target = (best[2] + 3.0 * math.cos(ang), best[3] + 3.0 * math.sin(ang))
        else:
            # Empty connectivity — grid near centre.
            rank = cluster_rank.get(key, 0)
            target = (cx + (rank % 4) * 4.0 - 6.0,
                      cy + (rank // 4) * 4.0 - 6.0)
        _seat(fp, target)

    # Final pass: if anything somehow unplaced, seat at origin spiral.
    for fp in fps:
        key = id_of[id(fp)]
        if key in placed_keys:
            continue
        _seat(fp, (cx, cy))

    # Strip any private staging keys.
    for fp in fps:
        fp.pop("_place_xy", None)

    moved = 0
    for i, fp in enumerate(fps):
        key = fp.get("id") or fp.get("ref") or str(i)
        old = orig_pos.get(key) or [0.0, 0.0]
        new = fp.get("pos") or [0.0, 0.0]
        if abs(float(old[0]) - float(new[0])) > 1e-9 or abs(float(old[1]) - float(new[1])) > 1e-9:
            moved += 1

    # Optional: ensure outline exists so edge checks have a reference (do not invent copper).
    # Report metrics.
    rn_len = _ratsnest_len_mm(work)
    overlaps = _dfm_courtyard_overlaps(work)

    # Decap proximity stats (for report / debugging).
    decap_ok = 0
    decap_dist: list[float] = []
    for fp in decaps:
        key = id_of[id(fp)]
        pos = fp.get("pos") or [0.0, 0.0]
        if key in decap_targets:
            d = _dist((float(pos[0]), float(pos[1])), decap_targets[key])
        else:
            # distance to nearest IC power pad on the cap's rail
            rail = _cap_rail_net(fp)
            d = math.inf
            if rail and rail in rail_pads:
                for px, py, _ in rail_pads[rail]:
                    d = min(d, _dist((float(pos[0]), float(pos[1])), (px, py)))
        if math.isfinite(d):
            decap_dist.append(float(d))
            if d <= _DECAP_MAX_RADIUS_MM:
                decap_ok += 1

    report = {
        "ratsnest_len_mm": float(rn_len),
        "moved": int(moved),
        "overlaps": int(overlaps),
        "n_footprints": int(n_fp),
        "n_ics": len(ics),
        "n_decaps": len(decaps),
        "n_connectors": len(connectors),
        "n_locked": len(locked_fps),
        "decap_within_radius": int(decap_ok),
        "decap_radius_mm": float(_DECAP_MAX_RADIUS_MM),
        "decap_dists_mm": [round(d, 4) for d in decap_dist],
        "bbox": [float(xmin), float(ymin), float(xmax), float(ymax)],
        "seed": seed,
        "cluster_order": list(cluster_ids),
    }
    return {"doc": work, "report": report}


def grid_place_board(doc: dict, opts: dict | None = None) -> dict:
    """Baseline sync.py-style 12.7 mm × 8-col grid placement (for comparison / tests).

    Pure. Same return shape as :func:`place_board`.
    """
    opts = dict(opts or {})
    work = copy.deepcopy(doc)
    fps = sorted(work.get("footprints") or [],
                 key=lambda f: (str(f.get("ref") or ""), str(f.get("id") or "")))
    origin = work.get("origin") or [0.0, 0.0]
    ox, oy = float(origin[0]), float(origin[1])
    pitch = float(opts.get("pitch", _GRID_PITCH))
    cols = int(opts.get("cols", _GRID_COLS))
    for i, fp in enumerate(fps):
        locked, hint = _locked_or_hint(fp)
        if locked and hint is not None:
            _set_pos(fp, hint)
            continue
        if hint is not None and opts.get("skip_locked", True):
            _set_pos(fp, hint)
            continue
        _set_pos(fp, [ox + (i % cols) * pitch, oy + (i // cols) * pitch])
    rn = _ratsnest_len_mm(work)
    return {
        "doc": work,
        "report": {
            "ratsnest_len_mm": float(rn),
            "moved": len(fps),
            "overlaps": _dfm_courtyard_overlaps(work),
            "n_footprints": len(fps),
            "method": "grid",
        },
    }


def autoplace(session: Any, opts: dict | None = None) -> dict:
    """Run :func:`place_board` on ``session.doc`` and write back via ``session.swap_doc``.

    Returns the placement report merged with the session ops-response fields
    (``revision``, ``compile``, …). Wave 2b ``board_from_schematic`` should call
    :func:`place_board` for a pure transform, or this helper when a live session
    already owns the board document.
    """
    from services.mcp.ecad_common import ecad_session as esess
    from services.mcp.ecad_common import OpError

    if session is None:
        raise OpError("invalid_op", "autoplace requires a live EcadSession")
    doc = getattr(session, "doc", None)
    if not isinstance(doc, dict) or not schema.is_board(doc):
        raise OpError("invalid_op", "autoplace requires a pcb session document")

    result = place_board(doc, opts)
    placed = result["doc"]
    report = result["report"]
    base_rev = getattr(session, "revision", None)
    warnings = [
        f"autoplace: moved {report.get('moved', 0)}/{report.get('n_footprints', 0)} "
        f"footprints; ratsnest={report.get('ratsnest_len_mm', 0):.2f} mm; "
        f"overlaps={report.get('overlaps', 0)}"
    ]
    resp = esess.swap_doc(session, placed, base_revision=base_rev, warnings=warnings)
    # Surface the placer report alongside the ops-response.
    resp = dict(resp)
    resp["place_report"] = report
    return resp
