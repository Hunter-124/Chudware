"""Parametric PCB antenna design helpers (clean-room textbook equations).

Designs rectangular patch, quarter-wave monopole, and meandered inverted-F /
PIFA antennas from target frequency + substrate parameters, then emits real
board copper (tracks / zones / pads) placeable on an ``aicad.pcb/1`` document.

Feed widths for 50 Ω are synthesised via the existing IPC-2141 / CPWG closed
forms in :mod:`engine.ecad.impedance` (binary search on width).

References (standard textbook forms, not vendor tables):
  * Rectangular patch — Balanis *Antenna Theory* §14.2 / Pozar *Microwave
    Engineering* §14.2 (width from low-er effective medium; length from
    effective permittivity + Hammerstad length extension ΔL).
  * Quarter-wave monopole — L = v_f · c / (4 f).
  * Meandered IFA / PIFA — total copper path ≈ λ_g / 4 on the substrate,
    folded into a shorted inverted-F layout sized for 2.4 GHz class bands.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from services.ecad import impedance as _z0

# Vacuum speed of light (mm/s) — board geometry is millimetres throughout.
C0_MM_S = 299_792_458_000.0
C0 = 299_792_458.0  # m/s

SUPPORTED_KINDS = ("patch", "monopole", "ifa", "pifa")
FEED_KINDS = ("microstrip", "cpw", "cpwg")


# --------------------------------------------------------------------------- helpers


def _round(v: float, nd: int = 4) -> float:
    return round(float(v), nd)


def _seg(tid: str, net: str, layer: str, width: float,
         p1: Sequence[float], p2: Sequence[float]) -> dict:
    a = [_round(p1[0]), _round(p1[1])]
    b = [_round(p2[0]), _round(p2[1])]
    return {
        "id": tid,
        "net": net,
        "layer": layer,
        "width": _round(width, 4),
        "kind": "segment",
        "p1": a,
        "p2": b,
        "points": [a, b],
    }


def _zone(zid: str, net: str, layer: str, polygon: Sequence[Sequence[float]],
          *, fill: str = "solid", clearance: float = 0.3) -> dict:
    poly = [[_round(p[0]), _round(p[1])] for p in polygon]
    return {
        "id": zid,
        "net": net,
        "layer": layer,
        "polygon": poly,
        "clearance": float(clearance),
        "fill": fill,
        "min_width": 0.25,
        "filled_polys": [],
    }


def _pad(pid: str, net: str, pos: Sequence[float], size: Sequence[float],
         *, layer: str = "F.Cu", shape: str = "rect", ptype: str = "smd") -> dict:
    return {
        "id": pid,
        "type": ptype,
        "shape": shape,
        "pos": [_round(pos[0]), _round(pos[1])],
        "size": [_round(size[0]), _round(size[1])],
        "layers": [layer],
        "net": net,
    }


def _id_gen(prefix: str, start: int = 1) -> Callable[[], str]:
    n = start - 1

    def _next() -> str:
        nonlocal n
        n += 1
        return f"{prefix}{n}"

    return _next


# --------------------------------------------------------------------------- feed


def synthesize_feed_width(
    target_z0: float = 50.0,
    *,
    substrate_er: float = 4.4,
    substrate_h_mm: float = 1.6,
    copper_t_mm: float = 0.035,
    kind: str = "microstrip",
    gap_mm: Optional[float] = None,
    tol_ohm: float = 0.25,
    w_lo: float = 0.05,
    w_hi: float = 20.0,
    max_iter: int = 48,
) -> dict:
    """Binary-search a feed width that hits ``target_z0`` via impedance.py.

    For ``kind='microstrip'`` uses :func:`engine.ecad.impedance.microstrip_z0`.
    For ``kind in {'cpw','cpwg'}`` uses :func:`engine.ecad.impedance.coplanar_z0`
    with gap ``gap_mm`` (defaults to ``substrate_h_mm`` when omitted).

    Returns ``{width_mm, z0, eeff, model, kind, gap_mm?, iterations}``.
    """
    kind = (kind or "microstrip").lower()
    if kind not in FEED_KINDS:
        raise ValueError(f"feed kind must be one of {FEED_KINDS}, got {kind!r}")
    if target_z0 <= 0:
        raise ValueError("target_z0 must be > 0")
    h = float(substrate_h_mm)
    er = float(substrate_er)
    t = float(copper_t_mm)
    gap = float(gap_mm) if gap_mm is not None else h

    def _eval(w: float) -> dict:
        if kind == "microstrip":
            return _z0.microstrip_z0(w, h, t, er)
        return _z0.coplanar_z0(w, gap, h, t, er)

    # Impedance decreases with width for both models — search accordingly.
    lo, hi = float(w_lo), float(w_hi)
    z_lo = _eval(lo)["z0"]
    z_hi = _eval(hi)["z0"]
    if not (z_hi < target_z0 < z_lo or z_lo < target_z0 < z_hi or
            abs(z_lo - target_z0) < tol_ohm or abs(z_hi - target_z0) < tol_ohm):
        # Still attempt the search; clamp to bounds if target is out of range.
        pass

    best_w, best = lo, _eval(lo)
    for i in range(max_iter):
        mid = 0.5 * (lo + hi)
        r = _eval(mid)
        z = r["z0"]
        if abs(z - target_z0) < abs(best["z0"] - target_z0):
            best_w, best = mid, r
        if abs(z - target_z0) <= tol_ohm:
            best_w, best = mid, r
            break
        # wider → lower Z0 for microstrip/CPWG
        if z > target_z0:
            lo = mid
        else:
            hi = mid
    else:
        i = max_iter - 1

    out = {
        "width_mm": _round(best_w, 4),
        "z0": _round(best["z0"], 4),
        "eeff": _round(best["eeff"], 6),
        "model": best["model"],
        "kind": kind,
        "target_z0": float(target_z0),
        "substrate_er": er,
        "substrate_h_mm": h,
        "copper_t_mm": t,
        "iterations": i + 1,
    }
    if kind in ("cpw", "cpwg"):
        out["gap_mm"] = _round(gap, 4)
    return out


# --------------------------------------------------------------------------- patch


def design_patch(
    freq_hz: float,
    *,
    substrate_er: float = 4.4,
    substrate_h_mm: float = 1.6,
    copper_t_mm: float = 0.035,
    feed_kind: str = "microstrip",
    feed_z0: float = 50.0,
    inset_frac: float = 0.3,
) -> dict:
    """Rectangular microstrip patch (Balanis §14.2 / Pozar §14.2).

    Closed forms (c in m/s, dimensions returned in mm)::

        W   = c / (2 f √((εr + 1)/2))
        εeff = (εr+1)/2 + (εr-1)/2 · (1 + 12 h/W)^(-1/2)
        ΔL  = 0.412 h · (εeff+0.3)(W/h+0.264) / ((εeff-0.258)(W/h+0.8))
        L   = c / (2 f √εeff) − 2 ΔL

    Feed is a 50 Ω microstrip/CPW synthesised via :func:`synthesize_feed_width`.
    Inset feed distance is ``inset_frac · L`` from the radiating edge (classic
    50 Ω match heuristic; refined by full-wave later).
    """
    f = float(freq_hz)
    if f <= 0:
        raise ValueError("freq_hz must be > 0")
    er = float(substrate_er)
    h_m = float(substrate_h_mm) * 1e-3
    if er < 1.0 or h_m <= 0:
        raise ValueError("substrate_er >= 1 and substrate_h_mm > 0 required")

    # Width for efficient radiation (Balanis 14-6 / Pozar 14.2).
    w_m = C0 / (2.0 * f * math.sqrt((er + 1.0) / 2.0))
    # Effective permittivity (Hammerstad / Balanis 14-1).
    u = w_m / h_m
    eeff = (er + 1.0) / 2.0 + (er - 1.0) / 2.0 * (1.0 + 12.0 / u) ** -0.5
    # Length extension (Hammerstad, Balanis 14-2).
    dl_m = (0.412 * h_m
            * (eeff + 0.3) * (u + 0.264)
            / ((eeff - 0.258) * (u + 0.8)))
    # Resonant length (Balanis 14-7).
    l_m = C0 / (2.0 * f * math.sqrt(eeff)) - 2.0 * dl_m
    if l_m <= 0:
        raise ValueError("patch length non-positive — check freq/substrate")

    w_mm = w_m * 1e3
    l_mm = l_m * 1e3
    h_mm = float(substrate_h_mm)
    dl_mm = dl_m * 1e3
    feed = synthesize_feed_width(
        feed_z0, substrate_er=er, substrate_h_mm=h_mm,
        copper_t_mm=copper_t_mm, kind=feed_kind,
    )
    inset_mm = max(0.0, float(inset_frac) * l_mm)
    # Rough fractional bandwidth for thin substrates (Balanis-ish):
    # BW ≈ 3.77*(εr-1)/(εr^2) * (h/λ0) * (W/L)  (VSWR 2:1 order estimate)
    lam0_mm = C0_MM_S / f
    bw_frac = (3.77 * (er - 1.0) / (er * er)
               * (h_mm / lam0_mm) * (w_mm / l_mm))
    bw_frac = max(bw_frac, 0.005)

    dims = {
        "kind": "patch",
        "freq_hz": f,
        "freq_ghz": _round(f / 1e9, 6),
        "substrate_er": er,
        "substrate_h_mm": h_mm,
        "copper_t_mm": float(copper_t_mm),
        "width_mm": _round(w_mm, 4),
        "length_mm": _round(l_mm, 4),
        "delta_l_mm": _round(dl_mm, 4),
        "eeff": _round(eeff, 6),
        "inset_mm": _round(inset_mm, 4),
        "feed": feed,
        "bandwidth_frac": _round(bw_frac, 6),
        "bandwidth_hz": _round(bw_frac * f, 1),
        "formula": "Balanis §14.2 / Pozar §14.2 rectangular patch",
    }

    def geometry(origin: Sequence[float] = (0.0, 0.0),
                 *, layer: str = "F.Cu", ground_layer: str = "B.Cu",
                 net: str = "ANT", gnd_net: str = "GND",
                 id_prefix: str = "") -> dict:
        return _geometry_patch(dims, origin, layer=layer, ground_layer=ground_layer,
                               net=net, gnd_net=gnd_net, id_prefix=id_prefix)

    return {"dimensions": dims, "geometry": geometry, "kind": "patch"}


def _geometry_patch(dims: dict, origin: Sequence[float], *, layer: str,
                    ground_layer: str, net: str, gnd_net: str,
                    id_prefix: str) -> dict:
    ox, oy = float(origin[0]), float(origin[1])
    w = float(dims["width_mm"])
    l = float(dims["length_mm"])
    inset = float(dims["inset_mm"])
    fw = float(dims["feed"]["width_mm"])
    # Patch centred on origin; length along +y (radiating edges // x).
    x0, x1 = ox - w / 2.0, ox + w / 2.0
    y0, y1 = oy, oy + l
    # Inset feed: microstrip comes from y0-feed_len up into the patch by `inset`.
    feed_len = max(l * 0.35, 5.0)
    feed_y0 = y0 - feed_len
    feed_y1 = y0 + inset
    # Slot width around the inset feed (clearance from patch body).
    slot = max(fw * 0.6, 0.3)

    next_z = _id_gen(f"{id_prefix}z")
    next_t = _id_gen(f"{id_prefix}t")
    next_p = _id_gen(f"{id_prefix}p")

    # Patch as a solid zone (rectangle).
    patch_zone = _zone(next_z(), net, layer,
                       [[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
    # Ground plane under the patch (slightly larger).
    margin = max(w, l) * 0.15
    gnd_zone = _zone(next_z(), gnd_net, ground_layer,
                     [[x0 - margin, feed_y0 - margin],
                      [x1 + margin, feed_y0 - margin],
                      [x1 + margin, y1 + margin],
                      [x0 - margin, y1 + margin]])
    # 50 Ω feed track into the inset.
    feed_track = _seg(next_t(), net, layer, fw,
                      [ox, feed_y0], [ox, feed_y1])
    # Optional side clearance tracks (visual inset slots) — thin keepouts via
    # gap: two short tracks not needed; represent inset as a pad at feed end.
    feed_pad = _pad(next_p(), net, [ox, feed_y0], [fw, fw], layer=layer)

    tracks = [feed_track]
    # If inset > 0, cut two side slots in the patch by adding a notch polygon
    # alternative: leave solid patch + feed overlap (fab will short — better to
    # emit a notched polygon for the patch).
    if inset > 0.05:
        # Notched patch: U-shape around the inset feed.
        notch_half = fw / 2.0 + slot
        poly = [
            [x0, y0],
            [ox - notch_half, y0],
            [ox - notch_half, y0 + inset],
            [ox + notch_half, y0 + inset],
            [ox + notch_half, y0],
            [x1, y0],
            [x1, y1],
            [x0, y1],
        ]
        patch_zone = _zone(patch_zone["id"], net, layer, poly)

    return {
        "kind": "patch",
        "origin": [_round(ox), _round(oy)],
        "layer": layer,
        "ground_layer": ground_layer,
        "net": net,
        "gnd_net": gnd_net,
        "tracks": tracks,
        "zones": [patch_zone, gnd_zone],
        "pads": [feed_pad],
        "footprints": [],
        "vias": [],
        "bbox_mm": [
            _round(x0 - margin), _round(feed_y0 - margin),
            _round(x1 + margin), _round(y1 + margin),
        ],
    }


# --------------------------------------------------------------------------- monopole


def design_monopole(
    freq_hz: float,
    *,
    substrate_er: float = 4.4,
    substrate_h_mm: float = 1.6,
    copper_t_mm: float = 0.035,
    velocity_factor: Optional[float] = None,
    feed_kind: str = "microstrip",
    feed_z0: float = 50.0,
    trace_width_mm: Optional[float] = None,
) -> dict:
    """Quarter-wave monopole (PCB trace form).

    Free-space length L0 = c/(4 f). On a dielectric the effective length shortens
    by the velocity factor ``v_f`` (default 1/√εeff of a microstrip of the feed
    width on the given substrate; overridable)::

        L = v_f · c / (4 f)

    The radiator is a straight F.Cu track of width ``trace_width_mm`` (defaults
    to the 50 Ω feed width) over a partial ground plane that stops at the feed.
    """
    f = float(freq_hz)
    if f <= 0:
        raise ValueError("freq_hz must be > 0")
    er = float(substrate_er)
    h_mm = float(substrate_h_mm)
    feed = synthesize_feed_width(
        feed_z0, substrate_er=er, substrate_h_mm=h_mm,
        copper_t_mm=copper_t_mm, kind=feed_kind,
    )
    w_mm = float(trace_width_mm) if trace_width_mm is not None else float(feed["width_mm"])
    if velocity_factor is None:
        # Microstrip velocity factor from the feed eeff.
        vf = 1.0 / math.sqrt(max(feed["eeff"], 1.0))
    else:
        vf = float(velocity_factor)
        if not (0.1 <= vf <= 1.0):
            raise ValueError("velocity_factor must be in [0.1, 1.0]")
    l_mm = vf * C0_MM_S / (4.0 * f)
    # Rough BW for thin monopole ~ few percent.
    bw_frac = 0.08 * (h_mm / max(l_mm, 1e-6)) ** 0.5
    bw_frac = min(max(bw_frac, 0.02), 0.15)

    dims = {
        "kind": "monopole",
        "freq_hz": f,
        "freq_ghz": _round(f / 1e9, 6),
        "substrate_er": er,
        "substrate_h_mm": h_mm,
        "copper_t_mm": float(copper_t_mm),
        "length_mm": _round(l_mm, 4),
        "width_mm": _round(w_mm, 4),
        "velocity_factor": _round(vf, 6),
        "feed": feed,
        "bandwidth_frac": _round(bw_frac, 6),
        "bandwidth_hz": _round(bw_frac * f, 1),
        "formula": "L = vf·c/(4f) quarter-wave monopole",
    }

    def geometry(origin: Sequence[float] = (0.0, 0.0),
                 *, layer: str = "F.Cu", ground_layer: str = "B.Cu",
                 net: str = "ANT", gnd_net: str = "GND",
                 id_prefix: str = "") -> dict:
        return _geometry_monopole(dims, origin, layer=layer,
                                  ground_layer=ground_layer, net=net,
                                  gnd_net=gnd_net, id_prefix=id_prefix)

    return {"dimensions": dims, "geometry": geometry, "kind": "monopole"}


def _geometry_monopole(dims: dict, origin: Sequence[float], *, layer: str,
                       ground_layer: str, net: str, gnd_net: str,
                       id_prefix: str) -> dict:
    ox, oy = float(origin[0]), float(origin[1])
    l = float(dims["length_mm"])
    w = float(dims["width_mm"])
    fw = float(dims["feed"]["width_mm"])
    feed_len = max(l * 0.25, 4.0)
    gnd_w = max(l * 0.9, 10.0)
    gnd_h = max(l * 0.5, 8.0)

    next_t = _id_gen(f"{id_prefix}t")
    next_z = _id_gen(f"{id_prefix}z")
    next_p = _id_gen(f"{id_prefix}p")

    # Radiator: vertical track from feed point upward.
    rad = _seg(next_t(), net, layer, w, [ox, oy], [ox, oy + l])
    # Feed stub below the ground edge.
    feed = _seg(next_t(), net, layer, fw, [ox, oy - feed_len], [ox, oy])
    feed_pad = _pad(next_p(), net, [ox, oy - feed_len], [fw, fw], layer=layer)
    # Partial ground (B.Cu) under the feed, not under the radiator.
    gnd = _zone(next_z(), gnd_net, ground_layer,
                [[ox - gnd_w / 2.0, oy - feed_len - 1.0],
                 [ox + gnd_w / 2.0, oy - feed_len - 1.0],
                 [ox + gnd_w / 2.0, oy],
                 [ox - gnd_w / 2.0, oy]])

    return {
        "kind": "monopole",
        "origin": [_round(ox), _round(oy)],
        "layer": layer,
        "ground_layer": ground_layer,
        "net": net,
        "gnd_net": gnd_net,
        "tracks": [rad, feed],
        "zones": [gnd],
        "pads": [feed_pad],
        "footprints": [],
        "vias": [],
        "bbox_mm": [
            _round(ox - gnd_w / 2.0), _round(oy - feed_len - 1.0),
            _round(ox + gnd_w / 2.0), _round(oy + l),
        ],
    }


# --------------------------------------------------------------------------- IFA / PIFA


def design_ifa(
    freq_hz: float = 2.45e9,
    *,
    substrate_er: float = 4.4,
    substrate_h_mm: float = 1.6,
    copper_t_mm: float = 0.035,
    meander_turns: int = 3,
    feed_kind: str = "microstrip",
    feed_z0: float = 50.0,
    short_spacing_mm: Optional[float] = None,
) -> dict:
    """Meandered inverted-F antenna (IFA) / planar-IFA (PIFA-class) for ~2.4 GHz.

    Total radiating copper path is sized to a guided quarter-wave on the
    substrate::

        L_total ≈ λ_g / 4 = c / (4 f √εeff)

    folded into a shorted inverted-F: a vertical short to ground, a horizontal
    radiating arm (optionally meandered), and a feed tap between the short and
    the open end. Classic layout for 2.4 GHz ISM on FR-4.

    ``meander_turns`` folds the horizontal arm to shrink the bounding box;
    set 0 for a straight IFA.
    """
    f = float(freq_hz)
    if f <= 0:
        raise ValueError("freq_hz must be > 0")
    er = float(substrate_er)
    h_mm = float(substrate_h_mm)
    feed = synthesize_feed_width(
        feed_z0, substrate_er=er, substrate_h_mm=h_mm,
        copper_t_mm=copper_t_mm, kind=feed_kind,
    )
    eeff = float(feed["eeff"])
    # Guided quarter-wave total path length.
    l_total = C0_MM_S / (4.0 * f * math.sqrt(max(eeff, 1.0)))
    # Height of the IFA "F" (short length) ~ a few mm; rest is the arm.
    h_ifa = min(max(h_mm * 2.5, 3.0), l_total * 0.25)
    arm = max(l_total - h_ifa, l_total * 0.6)
    turns = max(0, int(meander_turns))
    # Meander pitch chosen so the unfolded length ≈ arm.
    if turns > 0:
        pitch = arm / (2.0 * turns + 1.0)
        meander_depth = min(arm * 0.22, 4.0)
        span = pitch * (2 * turns + 1)
    else:
        pitch = arm
        meander_depth = 0.0
        span = arm
    short_s = float(short_spacing_mm) if short_spacing_mm is not None else max(
        float(feed["width_mm"]) * 2.5, 1.5)
    w_trace = float(feed["width_mm"])
    # IFA bandwidth is moderate on FR-4 (~4–8%).
    bw_frac = 0.06
    dims = {
        "kind": "ifa",
        "freq_hz": f,
        "freq_ghz": _round(f / 1e9, 6),
        "substrate_er": er,
        "substrate_h_mm": h_mm,
        "copper_t_mm": float(copper_t_mm),
        "total_length_mm": _round(l_total, 4),
        "arm_length_mm": _round(arm, 4),
        "height_mm": _round(h_ifa, 4),
        "span_mm": _round(span, 4),
        "meander_turns": turns,
        "meander_pitch_mm": _round(pitch, 4),
        "meander_depth_mm": _round(meander_depth, 4),
        "short_spacing_mm": _round(short_s, 4),
        "trace_width_mm": _round(w_trace, 4),
        "eeff": _round(eeff, 6),
        "feed": feed,
        "bandwidth_frac": bw_frac,
        "bandwidth_hz": _round(bw_frac * f, 1),
        "formula": "L_total≈λg/4 meandered inverted-F (PIFA-class)",
    }

    def geometry(origin: Sequence[float] = (0.0, 0.0),
                 *, layer: str = "F.Cu", ground_layer: str = "B.Cu",
                 net: str = "ANT", gnd_net: str = "GND",
                 id_prefix: str = "") -> dict:
        return _geometry_ifa(dims, origin, layer=layer, ground_layer=ground_layer,
                             net=net, gnd_net=gnd_net, id_prefix=id_prefix)

    return {"dimensions": dims, "geometry": geometry, "kind": "ifa"}


def design_pifa(
    freq_hz: float = 2.45e9,
    *,
    substrate_er: float = 4.4,
    substrate_h_mm: float = 1.6,
    copper_t_mm: float = 0.035,
    meander_turns: int = 2,
    feed_kind: str = "microstrip",
    feed_z0: float = 50.0,
) -> dict:
    """Planar inverted-F (PIFA-class) — thin-board meandered IFA alias.

    On a PCB the classic 3-D PIFA collapses to a planar inverted-F; this is the
    same calculator as :func:`design_ifa` with a ``kind='pifa'`` tag and a
    slightly wider short patch for the ground return.
    """
    res = design_ifa(
        freq_hz, substrate_er=substrate_er, substrate_h_mm=substrate_h_mm,
        copper_t_mm=copper_t_mm, meander_turns=meander_turns,
        feed_kind=feed_kind, feed_z0=feed_z0,
    )
    dims = dict(res["dimensions"])
    dims["kind"] = "pifa"
    dims["formula"] = "L_total≈λg/4 planar inverted-F (PIFA-class PCB)"
    geom_fn = res["geometry"]

    def geometry(origin: Sequence[float] = (0.0, 0.0), **kw) -> dict:
        g = geom_fn(origin, **kw)
        g["kind"] = "pifa"
        return g

    return {"dimensions": dims, "geometry": geometry, "kind": "pifa"}


def _geometry_ifa(dims: dict, origin: Sequence[float], *, layer: str,
                  ground_layer: str, net: str, gnd_net: str,
                  id_prefix: str) -> dict:
    ox, oy = float(origin[0]), float(origin[1])
    h = float(dims["height_mm"])
    span = float(dims["span_mm"])
    turns = int(dims["meander_turns"])
    pitch = float(dims["meander_pitch_mm"])
    depth = float(dims["meander_depth_mm"])
    short_s = float(dims["short_spacing_mm"])
    w = float(dims["trace_width_mm"])
    fw = float(dims["feed"]["width_mm"])
    feed_len = max(h * 0.8, 3.0)

    next_t = _id_gen(f"{id_prefix}t")
    next_z = _id_gen(f"{id_prefix}z")
    next_p = _id_gen(f"{id_prefix}p")
    next_v = _id_gen(f"{id_prefix}v")

    tracks: List[dict] = []
    # Short via/stub at x=ox (left), from ground up to height h.
    short_top = (ox, oy + h)
    tracks.append(_seg(next_t(), gnd_net, layer, w, [ox, oy], list(short_top)))
    # Horizontal arm + meanders starting at short top going +x.
    pts: List[List[float]] = [[ox, oy + h]]
    x = ox
    y = oy + h
    if turns <= 0:
        pts.append([ox + span, y])
    else:
        direction = 1.0
        for i in range(turns):
            x += pitch
            pts.append([x, y])
            y = oy + h - direction * depth
            pts.append([x, y])
            x += pitch
            pts.append([x, y])
            y = oy + h
            pts.append([x, y])
            direction *= 1.0  # keep meander downward then back up already done
        # final stub to reach span
        if x < ox + span:
            pts.append([ox + span, oy + h])
    for a, b in zip(pts, pts[1:]):
        tracks.append(_seg(next_t(), net, layer, w, a, b))

    # Feed tap between short and open end (short_spacing from short).
    feed_x = ox + short_s
    tracks.append(_seg(next_t(), net, layer, fw,
                       [feed_x, oy - feed_len], [feed_x, oy + h]))
    # Connect feed vertical to the arm at y=oy+h (already crosses).
    feed_pad = _pad(next_p(), net, [feed_x, oy - feed_len], [fw, fw], layer=layer)

    # Via shorting the short-stub to ground plane.
    via = {
        "id": next_v(),
        "net": gnd_net,
        "at": [_round(ox), _round(oy)],
        "drill": 0.3,
        "diameter": 0.6,
        "layers": [layer, ground_layer],
        "type": "through",
    }
    # Ground pour under the feed / left of the antenna keepout.
    gnd_w = max(span * 1.2, 12.0)
    gnd = _zone(next_z(), gnd_net, ground_layer,
                [[ox - 2.0, oy - feed_len - 1.0],
                 [ox + gnd_w, oy - feed_len - 1.0],
                 [ox + gnd_w, oy],
                 [ox - 2.0, oy]])

    xs = [p[0] for t in tracks for p in (t["p1"], t["p2"])]
    ys = [p[1] for t in tracks for p in (t["p1"], t["p2"])]
    return {
        "kind": dims.get("kind", "ifa"),
        "origin": [_round(ox), _round(oy)],
        "layer": layer,
        "ground_layer": ground_layer,
        "net": net,
        "gnd_net": gnd_net,
        "tracks": tracks,
        "zones": [gnd],
        "pads": [feed_pad],
        "vias": [via],
        "footprints": [],
        "bbox_mm": [
            _round(min(xs) - 2.0), _round(min(ys) - 1.0),
            _round(max(xs) + 2.0), _round(max(ys) + 1.0),
        ],
    }


# --------------------------------------------------------------------------- public entry


def design_antenna(
    kind: str,
    freq_hz: float,
    *,
    substrate_er: float = 4.4,
    substrate_h_mm: float = 1.6,
    copper_t_mm: float = 0.035,
    feed_kind: str = "microstrip",
    feed_z0: float = 50.0,
    **opts: Any,
) -> dict:
    """Dispatch parametric antenna design.

    Parameters
    ----------
    kind:
        ``'patch'`` | ``'monopole'`` | ``'ifa'`` | ``'pifa'``.
    freq_hz:
        Target resonant frequency (Hz).
    substrate_er, substrate_h_mm, copper_t_mm:
        Board dielectric / copper.
    feed_kind, feed_z0:
        Feed line type and target impedance (Ω).
    **opts:
        Kind-specific options forwarded to the designer (``meander_turns``,
        ``velocity_factor``, ``inset_frac``, ``trace_width_mm``, …).

    Returns
    -------
    dict with keys:
      * ``kind`` — antenna class
      * ``dimensions`` — closed-form sizes + feed
      * ``geometry(origin=(0,0), **kw)`` — callable emitting board copper dicts
    """
    k = (kind or "").strip().lower()
    common = dict(
        substrate_er=substrate_er,
        substrate_h_mm=substrate_h_mm,
        copper_t_mm=copper_t_mm,
        feed_kind=feed_kind,
        feed_z0=feed_z0,
    )
    if k == "patch":
        return design_patch(freq_hz, inset_frac=opts.get("inset_frac", 0.3), **common)
    if k == "monopole":
        return design_monopole(
            freq_hz,
            velocity_factor=opts.get("velocity_factor"),
            trace_width_mm=opts.get("trace_width_mm"),
            **common,
        )
    if k == "ifa":
        return design_ifa(
            freq_hz,
            meander_turns=int(opts.get("meander_turns", 3)),
            short_spacing_mm=opts.get("short_spacing_mm"),
            **common,
        )
    if k == "pifa":
        return design_pifa(
            freq_hz,
            meander_turns=int(opts.get("meander_turns", 2)),
            **common,
        )
    raise ValueError(f"unknown antenna kind {kind!r}; expected one of {SUPPORTED_KINDS}")


def place_antenna_on_board(
    board: dict,
    design: dict,
    origin: Sequence[float] = (0.0, 0.0),
    *,
    layer: str = "F.Cu",
    ground_layer: str = "B.Cu",
    net: str = "ANT",
    gnd_net: str = "GND",
    id_prefix: str = "",
) -> dict:
    """Merge a design's geometry into an existing board document (in place).

    Ensures ``net`` / ``gnd_net`` exist, appends tracks/zones/pads/vias, and
    returns the geometry dict that was placed.
    """
    if not isinstance(board, dict):
        raise TypeError("board must be a pcb document dict")
    geom_fn = design.get("geometry")
    if not callable(geom_fn):
        raise ValueError("design must expose a geometry() callable")

    nets = board.setdefault("nets", [])
    names = {n.get("name") for n in nets if isinstance(n, dict)}
    for n in (net, gnd_net):
        if n not in names:
            nets.append({"name": n, "class": "default"})
            names.add(n)

    # Prefer board-local id allocation when schema is available.
    try:
        from services.ecad import schema as _schema

        def _pref(prefix: str) -> str:
            # next_id returns e.g. 't12'; strip the numeric so geometry ids stay unique
            return ""

        # Use schema.next_id by rewriting after emit if no prefix given.
        use_schema_ids = not id_prefix
    except Exception:  # noqa: BLE001
        use_schema_ids = False

    geom = geom_fn(origin, layer=layer, ground_layer=ground_layer,
                   net=net, gnd_net=gnd_net, id_prefix=id_prefix or "ant")

    if use_schema_ids:
        from services.ecad import schema as _schema

        # next_id only sees ids already on the board; stage each object onto the
        # board as we allocate so subsequent next_id calls stay unique.
        for t in geom.get("tracks", []):
            t["id"] = _schema.next_id(board, "t")
            board.setdefault("tracks", []).append(t)
        for z in geom.get("zones", []):
            z["id"] = _schema.next_id(board, "z")
            board.setdefault("zones", []).append(z)
        for v in geom.get("vias", []):
            v["id"] = _schema.next_id(board, "v")
            board.setdefault("vias", []).append(v)
        pads = geom.get("pads") or []
        if pads:
            fid = _schema.next_id(board, "fp")
            ox, oy = float(origin[0]), float(origin[1])
            local_pads = []
            for i, p in enumerate(pads):
                lp = dict(p)
                lp["id"] = str(i + 1)
                lp["pos"] = [_round(p["pos"][0] - ox), _round(p["pos"][1] - oy)]
                local_pads.append(lp)
            board.setdefault("footprints", []).append({
                "id": fid,
                "ref": f"ANT_{design.get('kind', 'X').upper()}",
                "fpid": f"Antenna:{design.get('kind', 'custom')}",
                "pos": [_round(ox), _round(oy)],
                "side": "top",
                "angle": 0.0,
                "pads": local_pads,
            })
            geom["footprints"] = [board["footprints"][-1]]
        return geom
    else:
        for p in geom.get("pads") or []:
            # Standalone pad list (not in a footprint) — attach via a footprint.
            pass
        pads = geom.get("pads") or []
        if pads:
            ox, oy = float(origin[0]), float(origin[1])
            board.setdefault("footprints", []).append({
                "id": f"{id_prefix or 'ant'}fp1",
                "ref": f"ANT_{design.get('kind', 'X').upper()}",
                "fpid": f"Antenna:{design.get('kind', 'custom')}",
                "pos": [_round(ox), _round(oy)],
                "side": "top",
                "angle": 0.0,
                "pads": [
                    {**p, "id": str(i + 1),
                     "pos": [_round(p["pos"][0] - ox), _round(p["pos"][1] - oy)]}
                    for i, p in enumerate(pads)
                ],
            })

    board.setdefault("tracks", []).extend(geom.get("tracks") or [])
    board.setdefault("zones", []).extend(geom.get("zones") or [])
    board.setdefault("vias", []).extend(geom.get("vias") or [])
    return geom


__all__ = [
    "SUPPORTED_KINDS",
    "FEED_KINDS",
    "C0",
    "C0_MM_S",
    "synthesize_feed_width",
    "design_patch",
    "design_monopole",
    "design_ifa",
    "design_pifa",
    "design_antenna",
    "place_antenna_on_board",
]
