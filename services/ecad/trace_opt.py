"""Post-route PCB trace optimization (corner rounding, teardrops, impedance width).

Public entry point for Wave 2b::

    optimize_traces(doc: dict, opts: dict | None = None) -> dict

Returns ``{"doc": <deep-copied board>, "report": {...}}``. Pure: never mutates the
input. Every geometry/width change is DRC-guarded and reverted on a new clearance
(or other error-severity) violation. Designed to be deterministic and idempotent —
a second call on the result reports zero changes.

Track dict shapes match :mod:`engine.ecad.schema` / :mod:`engine.ecad.ops` /
:mod:`engine.ecad.fab.gerber`::

    segment: {id, net, layer, width, kind:'segment', p1, p2}
    arc:     {id, net, layer, width, kind:'arc', p1, p2, center, a0, a1, radius?}
"""
from __future__ import annotations

import copy
import math
from typing import Any

from services.ecad import drc
from services.ecad import schema
from services.ecad.impedance import _stackup_geometry, track_impedance
from services.ecad.stackup import StackupInfeasible, solve_se_width

# --------------------------------------------------------------------------- constants

_EPS = 1e-9
_END_EPS = 1e-4  # endpoint match tolerance (mm)
_ANGLE_90_TOL_DEG = 15.0  # "about 90°" window for corner detection
_DEFAULT_CORNER_MULT = 3.0  # radius = mult * track width
_DEFAULT_TEARDROP_LEN_MULT = 1.5
_DEFAULT_TEARDROP_WIDTH_MULT = 1.6
_DEFAULT_Z0_TOL = 0.10  # ±10%
_MIN_LEG_FOR_FILLET = 1e-3


# --------------------------------------------------------------------------- helpers


def _pt(p: Any) -> tuple[float, float]:
    return (float(p[0]), float(p[1]))


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _near(a: tuple[float, float], b: tuple[float, float], eps: float = _END_EPS) -> bool:
    return _dist(a, b) <= eps


def _unit(dx: float, dy: float) -> tuple[float, float] | None:
    L = math.hypot(dx, dy)
    if L < _EPS:
        return None
    return (dx / L, dy / L)


def _ang_deg(dx: float, dy: float) -> float:
    """atan2 degrees in (-180, 180]."""
    return math.degrees(math.atan2(dy, dx))


def _norm_angle(a: float) -> float:
    """Normalize degrees to (-180, 180]."""
    a = (a + 180.0) % 360.0 - 180.0
    if a <= -180.0:
        a += 360.0
    return a


def _clearance_errors(board: dict) -> list[dict]:
    """Error-severity DRC hits (clearance + size rules). Unrouted airwires are warnings."""
    return [v for v in drc.run_drc(board) if v.get("severity") == "error"]


def _next_track_id(board: dict) -> str:
    return schema.next_id(board, "t")


def _seg_endpoints(t: dict) -> tuple[tuple[float, float], tuple[float, float]] | None:
    if t.get("kind", "segment") != "segment":
        return None
    return _pt(t.get("p1", [0, 0])), _pt(t.get("p2", [0, 0]))


def _is_teardrop(t: dict) -> bool:
    if t.get("role") == "teardrop":
        return True
    meta = t.get("meta") or {}
    return bool(meta.get("teardrop"))


def _is_corner_arc(t: dict) -> bool:
    if t.get("kind") != "arc":
        return False
    meta = t.get("meta") or {}
    return bool(meta.get("corner_round")) or t.get("role") == "corner_round"


def _mark(t: dict, **flags: Any) -> dict:
    meta = dict(t.get("meta") or {})
    meta.update(flags)
    t = dict(t)
    t["meta"] = meta
    return t


# --------------------------------------------------------------------------- public


def optimize_traces(doc: dict, opts: dict | None = None) -> dict:
    """Post-route optimization pass: corner rounding, teardrops, impedance width.

    Parameters
    ----------
    doc:
        ``aicad.pcb/1`` board document. Never mutated.
    opts:
        Optional knobs::

            corner_radius_mult: float = 3.0   # arc radius = mult * track width
            corner_radius: float | None       # absolute radius override (mm)
            teardrop_length_mult: float = 1.5
            teardrop_width_mult: float = 1.6
            targets: {net_name: z0_ohm}       # impedance targets
            z0_tolerance: float = 0.10        # relative band (±)
            min_trace: float | None           # floor for solved widths
            skip_corners / skip_teardrops / skip_impedance: bool

    Returns
    -------
    dict
        ``{"doc": <new board>, "report": {"corners_rounded", "teardrops",
        "nets_tuned", "drc_ok", "reverted", ...}}``
    """
    opts = dict(opts or {})
    if not schema.is_board(doc):
        return {
            "doc": copy.deepcopy(doc),
            "report": {
                "corners_rounded": 0,
                "teardrops": 0,
                "nets_tuned": 0,
                "drc_ok": False,
                "reverted": 0,
                "error": "not a board document",
            },
        }

    board = copy.deepcopy(doc)
    report = {
        "corners_rounded": 0,
        "teardrops": 0,
        "nets_tuned": 0,
        "drc_ok": True,
        "reverted": 0,
        "z0": {},
        "skipped": [],
    }

    # Impedance first so later corner radii use the final widths.
    if not opts.get("skip_impedance", False):
        _pass_impedance(board, opts, report)

    if not opts.get("skip_corners", False):
        _pass_corner_rounding(board, opts, report)

    if not opts.get("skip_teardrops", False):
        _pass_teardrops(board, opts, report)

    errs = _clearance_errors(board)
    report["drc_ok"] = len(errs) == 0
    report["drc_errors"] = len(errs)
    return {"doc": board, "report": report}


# --------------------------------------------------------------------------- impedance


def _target_map(board: dict, opts: dict) -> dict[str, float]:
    """Resolve per-net target Z0 from opts['targets'] and net / net-class fields."""
    out: dict[str, float] = {}
    raw = opts.get("targets") or {}
    for name, spec in raw.items():
        if isinstance(spec, (int, float)):
            out[str(name)] = float(spec)
        elif isinstance(spec, dict) and "z0" in spec:
            out[str(name)] = float(spec["z0"])

    classes = board.get("net_classes", {}) or {}
    for n in board.get("nets", []):
        if not isinstance(n, dict):
            continue
        name = n.get("name")
        if not name or name in out:
            continue
        if n.get("target_z0") is not None:
            out[str(name)] = float(n["target_z0"])
            continue
        if n.get("z0") is not None:
            out[str(name)] = float(n["z0"])
            continue
        cls = classes.get(n.get("class") or "default") or {}
        if cls.get("target_z0") is not None:
            out[str(name)] = float(cls["target_z0"])
        elif cls.get("z0") is not None:
            out[str(name)] = float(cls["z0"])
    return out


def _pass_impedance(board: dict, opts: dict, report: dict) -> None:
    tol = float(opts.get("z0_tolerance", _DEFAULT_Z0_TOL))
    targets = _target_map(board, opts)
    if not targets:
        return

    min_trace = opts.get("min_trace")
    if min_trace is None:
        min_trace = float(board.get("rules", {}).get("track_width_min", 0.1) or 0.1)
    min_trace = float(min_trace)

    for net in sorted(targets):
        target = float(targets[net])
        tracks = [t for t in board.get("tracks", []) if t.get("net") == net]
        if not tracks:
            report["skipped"].append(f"impedance:{net}:no_tracks")
            continue
        try:
            cur = track_impedance(board, net)
        except ValueError:
            report["skipped"].append(f"impedance:{net}:no_geometry")
            continue
        z0 = float(cur["z0"])
        report["z0"][net] = {"before": z0, "target": target}
        if target <= 0:
            continue
        if abs(z0 - target) / target <= tol + 1e-12:
            report["z0"][net]["after"] = z0
            report["z0"][net]["tuned"] = False
            continue

        layer = str(cur.get("layer") or tracks[0].get("layer") or "F.Cu")
        geo = _stackup_geometry(board, layer)
        try:
            new_w = solve_se_width(
                target, geo["kind"], geo["h"], geo["t"], geo["er"],
                min_trace=min_trace, max_trace=float(opts.get("max_trace", 5.0)),
            )
        except StackupInfeasible as e:
            report["skipped"].append(f"impedance:{net}:infeasible:{e.binding}")
            report["z0"][net]["after"] = z0
            report["z0"][net]["tuned"] = False
            continue

        # Round for determinism / stable re-runs.
        new_w = round(float(new_w), 6)
        # Skip if already at the solved width (idempotent).
        if all(abs(float(t.get("width", 0)) - new_w) <= 1e-6 for t in tracks):
            report["z0"][net]["after"] = z0
            report["z0"][net]["tuned"] = False
            continue

        before_tracks = copy.deepcopy(board.get("tracks", []))
        before_errs = { _viol_key(v) for v in _clearance_errors(board) }
        for t in board.get("tracks", []):
            if t.get("net") == net:
                t["width"] = new_w
        after_errs = { _viol_key(v) for v in _clearance_errors(board) }
        new_errs = after_errs - before_errs
        if new_errs:
            board["tracks"] = before_tracks
            report["reverted"] += 1
            report["skipped"].append(f"impedance:{net}:drc_revert")
            report["z0"][net]["after"] = z0
            report["z0"][net]["tuned"] = False
            continue

        try:
            z_after = float(track_impedance(board, net)["z0"])
        except ValueError:
            z_after = z0
        report["z0"][net]["after"] = z_after
        report["z0"][net]["width"] = new_w
        report["z0"][net]["tuned"] = True
        report["nets_tuned"] += 1


def _viol_key(v: dict) -> tuple:
    return (v.get("rule"), tuple(v.get("items") or []), v.get("message"))


# --------------------------------------------------------------------------- corner rounding


def _pass_corner_rounding(board: dict, opts: dict, report: dict) -> None:
    # Iterate until no more eligible corners (each fillet reduces the set).
    # Cap iterations for safety; small boards finish in one or two sweeps.
    for _ in range(64):
        joint = _find_one_corner(board, opts)
        if joint is None:
            break
        if _apply_fillet(board, joint, opts, report):
            report["corners_rounded"] += 1
        else:
            # Mark this joint as non-retryable by recording endpoint pair in a
            # soft blacklist on the report so we don't spin forever.
            report.setdefault("_failed_joints", set()).add(joint["key"])
            # If apply failed (geometry/DRC), skip it next time via key filter.
            if joint["key"] in report.get("_failed_joints", set()):
                # force progress: temporarily tag one segment so _find skips it
                t0 = board["tracks"][joint["i"]]
                t0["meta"] = dict(t0.get("meta") or {})
                t0["meta"]["_no_fillet"] = True


def _corner_radius(width: float, opts: dict) -> float:
    if opts.get("corner_radius") is not None:
        return max(float(opts["corner_radius"]), _MIN_LEG_FOR_FILLET)
    mult = float(opts.get("corner_radius_mult", _DEFAULT_CORNER_MULT))
    return max(mult * float(width), _MIN_LEG_FOR_FILLET)


def _find_one_corner(board: dict, opts: dict) -> dict | None:
    """Find one ~90° segment-segment joint eligible for fillet (deterministic order)."""
    tracks = board.get("tracks", [])
    # Index segment endpoints by (net, layer, rounded xy) -> list of (idx, end 'p1'|'p2')
    buckets: dict[tuple, list[tuple[int, str]]] = {}
    for i, t in enumerate(tracks):
        if t.get("kind", "segment") != "segment":
            continue
        if _is_teardrop(t):
            continue
        if (t.get("meta") or {}).get("_no_fillet"):
            continue
        ep = _seg_endpoints(t)
        if ep is None:
            continue
        net = t.get("net")
        layer = t.get("layer")
        for end_name, p in (("p1", ep[0]), ("p2", ep[1])):
            key = (net, layer, round(p[0], 4), round(p[1], 4))
            buckets.setdefault(key, []).append((i, end_name))

    candidates: list[dict] = []
    for key, items in buckets.items():
        if len(items) != 2:
            continue
        (i, ei), (j, ej) = items[0], items[1]
        if i == j:
            continue
        # Stable order by track id.
        if str(tracks[i].get("id", "")) > str(tracks[j].get("id", "")):
            i, ei, j, ej = j, ej, i, ei
        ti, tj = tracks[i], tracks[j]
        pi = _seg_endpoints(ti)
        pj = _seg_endpoints(tj)
        if pi is None or pj is None:
            continue
        # Corner point C and the two far endpoints A, D.
        C = pi[0] if ei == "p1" else pi[1]
        A = pi[1] if ei == "p1" else pi[0]
        D = pj[1] if ej == "p1" else pj[0]
        # Incoming direction A->C, outgoing C->D (or treat as undirected path A-C-D).
        u1 = _unit(C[0] - A[0], C[1] - A[1])
        u2 = _unit(D[0] - C[0], D[1] - C[1])
        if u1 is None or u2 is None:
            continue
        # Angle between -u1 (approach) and u2 (leave) ... use u1 and u2 along path A->C->D.
        # Turning angle from direction u1 to u2:
        dot = u1[0] * u2[0] + u1[1] * u2[1]
        # clamp for acos
        dot = max(-1.0, min(1.0, dot))
        turn_deg = math.degrees(math.acos(dot))  # 0 = straight, 90 = right angle
        if abs(turn_deg - 90.0) > _ANGLE_90_TOL_DEG:
            continue
        # Require both legs long enough for the fillet radius.
        w = min(float(ti.get("width", 0.25)), float(tj.get("width", 0.25)))
        r = _corner_radius(w, opts)
        len_i = _dist(A, C)
        len_j = _dist(C, D)
        if len_i < r + _MIN_LEG_FOR_FILLET or len_j < r + _MIN_LEG_FOR_FILLET:
            continue
        cross = u1[0] * u2[1] - u1[1] * u2[0]
        candidates.append({
            "i": i, "j": j, "ei": ei, "ej": ej,
            "A": A, "C": C, "D": D, "u1": u1, "u2": u2,
            "cross": cross, "r": r, "width": w,
            "net": ti.get("net"), "layer": ti.get("layer"),
            "key": (ti.get("id"), tj.get("id"), round(C[0], 4), round(C[1], 4)),
        })

    if not candidates:
        return None
    candidates.sort(key=lambda c: (str(c["net"]), str(c["layer"]), c["key"]))
    return candidates[0]


def _apply_fillet(board: dict, joint: dict, opts: dict, report: dict) -> bool:
    """Replace a 90° corner with an arc; DRC-guarded. Returns True on success."""
    tracks = board["tracks"]
    i, j = joint["i"], joint["j"]
    ti, tj = tracks[i], tracks[j]
    A, C, D = joint["A"], joint["C"], joint["D"]
    u1, u2 = joint["u1"], joint["u2"]
    r = float(joint["r"])
    cross = float(joint["cross"])

    # Inward unit normal relative to the turn at C along path A->C->D.
    if cross >= 0:
        # left (CCW) turn: rotate u1 by +90°
        n = (-u1[1], u1[0])
        ccw = True
    else:
        n = (u1[1], -u1[0])
        ccw = False

    p_start = (C[0] - u1[0] * r, C[1] - u1[1] * r)
    p_end = (C[0] + u2[0] * r, C[1] + u2[1] * r)
    center = (C[0] - u1[0] * r + n[0] * r, C[1] - u1[1] * r + n[1] * r)

    a0 = _ang_deg(p_start[0] - center[0], p_start[1] - center[1])
    a1 = _ang_deg(p_end[0] - center[0], p_end[1] - center[1])
    # Ensure a0->a1 sweeps the short 90° arc in the correct direction.
    if ccw:
        # a1 should be a0 + ~90 (mod 360)
        da = (a1 - a0) % 360.0
        if da > 180.0:
            # swap endpoints / reverse so sweep is the interior fillet
            p_start, p_end = p_end, p_start
            a0, a1 = a1, a0
            da = (a1 - a0) % 360.0
        a1 = a0 + da
    else:
        # CW: a1 = a0 - ~90
        da = (a0 - a1) % 360.0
        if da > 180.0:
            p_start, p_end = p_end, p_start
            a0, a1 = a1, a0
            da = (a0 - a1) % 360.0
        a1 = a0 - da

    # Snapshot for revert.
    before = copy.deepcopy(tracks)
    before_errs = { _viol_key(v) for v in _clearance_errors(board) }

    # Shorten the two segments to the fillet tangency points.
    def _rewrite_seg(t: dict, keep_far: tuple[float, float], new_near: tuple[float, float]) -> dict:
        nt = dict(t)
        # Preserve orientation: whichever end was the corner becomes new_near.
        p1, p2 = _pt(t["p1"]), _pt(t["p2"])
        if _near(p1, keep_far):
            nt["p1"] = [keep_far[0], keep_far[1]]
            nt["p2"] = [new_near[0], new_near[1]]
        elif _near(p2, keep_far):
            nt["p2"] = [keep_far[0], keep_far[1]]
            nt["p1"] = [new_near[0], new_near[1]]
        else:
            # Fall back: assign by distance.
            if _dist(p1, keep_far) <= _dist(p2, keep_far):
                nt["p1"] = [keep_far[0], keep_far[1]]
                nt["p2"] = [new_near[0], new_near[1]]
            else:
                nt["p2"] = [keep_far[0], keep_far[1]]
                nt["p1"] = [new_near[0], new_near[1]]
        return nt

    # Far ends: A for ti, D for tj (path A-C-D).
    new_ti = _rewrite_seg(ti, A, p_start)
    new_tj = _rewrite_seg(tj, D, p_end)
    # Drop degenerate segments.
    def _ok_seg(t: dict) -> bool:
        return _dist(_pt(t["p1"]), _pt(t["p2"])) > _MIN_LEG_FOR_FILLET

    new_tracks: list[dict] = []
    for k, t in enumerate(tracks):
        if k == i:
            if _ok_seg(new_ti):
                new_tracks.append(new_ti)
        elif k == j:
            if _ok_seg(new_tj):
                new_tracks.append(new_tj)
        else:
            new_tracks.append(t)

    arc_id = _next_track_id({**board, "tracks": new_tracks})
    arc = {
        "id": arc_id,
        "net": joint["net"],
        "layer": joint["layer"],
        "width": float(joint["width"]),
        "kind": "arc",
        "p1": [float(p_start[0]), float(p_start[1])],
        "p2": [float(p_end[0]), float(p_end[1])],
        "center": [float(center[0]), float(center[1])],
        "a0": float(round(a0, 6)),
        "a1": float(round(a1, 6)),
        "radius": float(r),
        "role": "corner_round",
        "meta": {"corner_round": True},
    }
    new_tracks.append(arc)
    # Deterministic track order: by id.
    new_tracks.sort(key=lambda t: str(t.get("id", "")))
    board["tracks"] = new_tracks

    after_errs = { _viol_key(v) for v in _clearance_errors(board) }
    if after_errs - before_errs:
        board["tracks"] = before
        report["reverted"] += 1
        return False
    return True


# --------------------------------------------------------------------------- teardrops


def _pad_terminals(board: dict) -> list[dict]:
    """World-position pad terminals: {id, net, at, layers, size}."""
    out: list[dict] = []
    n_cu = int(board.get("stackup", {}).get("copper_layers", 2))
    all_cu = set(schema.copper_layer_ids(n_cu))
    for fp in board.get("footprints", []):
        for pad in fp.get("pads", []):
            net = pad.get("net")
            if not net:
                continue
            at = schema.pad_world(fp, pad)
            ptype = pad.get("type", "smd")
            if ptype in ("tht", "np_tht", "connector"):
                layers = set(all_cu)
            else:
                layers = {ly for ly in pad.get("layers", []) if ly in all_cu} or {"F.Cu"}
            size = pad.get("size") or [1.0, 1.0]
            out.append({
                "id": pad.get("id") or f"{fp.get('id')}:{pad.get('number')}",
                "net": net,
                "at": (float(at[0]), float(at[1])),
                "layers": layers,
                "size": (float(size[0]), float(size[1])),
            })
    for v in board.get("vias", []):
        net = v.get("net")
        if not net:
            continue
        at = _pt(v.get("at", [0, 0]))
        layers = set(v.get("layers") or ["F.Cu", "B.Cu"])
        dia = float(v.get("diameter", 0.8))
        out.append({
            "id": v.get("id", "via"),
            "net": net,
            "at": at,
            "layers": layers,
            "size": (dia, dia),
        })
    out.sort(key=lambda t: (str(t["net"]), str(t["id"])))
    return out


def _pass_teardrops(board: dict, opts: dict, report: dict) -> None:
    len_mult = float(opts.get("teardrop_length_mult", _DEFAULT_TEARDROP_LEN_MULT))
    w_mult = float(opts.get("teardrop_width_mult", _DEFAULT_TEARDROP_WIDTH_MULT))

    terminals = _pad_terminals(board)
    # Existing teardrop stubs — skip those pad+layer combos (idempotent).
    existing: set[tuple] = set()
    for t in board.get("tracks", []):
        if not _is_teardrop(t):
            continue
        ep = _seg_endpoints(t)
        if ep is None:
            continue
        for p in ep:
            existing.add((t.get("net"), t.get("layer"), round(p[0], 3), round(p[1], 3)))

    # Collect candidate (terminal, track, end) junctions.
    candidates: list[dict] = []
    tracks = board.get("tracks", [])
    for ti, t in enumerate(tracks):
        if t.get("kind", "segment") != "segment":
            continue
        if _is_teardrop(t):
            continue
        ep = _seg_endpoints(t)
        if ep is None:
            continue
        net = t.get("net")
        layer = t.get("layer")
        for end_name, p in (("p1", ep[0]), ("p2", ep[1])):
            for term in terminals:
                if term["net"] != net:
                    continue
                if layer not in term["layers"]:
                    continue
                if not _near(p, term["at"], eps=max(_END_EPS, 0.05)):
                    continue
                # Direction from pad into the track.
                other = ep[1] if end_name == "p1" else ep[0]
                u = _unit(other[0] - p[0], other[1] - p[1])
                if u is None:
                    continue
                key = (net, layer, round(term["at"][0], 3), round(term["at"][1], 3))
                if key in existing:
                    continue
                candidates.append({
                    "ti": ti, "end": end_name, "term": term, "p": p, "u": u,
                    "width": float(t.get("width", 0.25)),
                    "net": net, "layer": layer, "key": key,
                    "track_id": t.get("id"),
                })

    # Unique per pad junction (one teardrop per pad+layer+net).
    seen: set[tuple] = set()
    uniq: list[dict] = []
    candidates.sort(key=lambda c: (str(c["net"]), str(c["layer"]), str(c["term"]["id"]),
                                   str(c["track_id"])))
    for c in candidates:
        if c["key"] in seen:
            continue
        seen.add(c["key"])
        uniq.append(c)

    for c in uniq:
        if _add_teardrop(board, c, len_mult, w_mult, report):
            report["teardrops"] += 1
            existing.add(c["key"])


def _add_teardrop(board: dict, c: dict, len_mult: float, w_mult: float,
                  report: dict) -> bool:
    """Add a short widening segment at a pad/via junction. DRC-guarded."""
    term = c["term"]
    pad_r = 0.5 * max(term["size"][0], term["size"][1])
    track_w = float(c["width"])
    length = max(len_mult * track_w, 0.5 * pad_r, 0.2)
    # Don't overshoot a short track: cap at half the track length.
    tracks = board["tracks"]
    t = tracks[c["ti"]]
    ep = _seg_endpoints(t)
    if ep is None:
        return False
    track_len = _dist(ep[0], ep[1])
    length = min(length, max(track_len * 0.45, 0.05))
    tear_w = max(track_w * w_mult, track_w + 0.05)
    # Cap teardrop width near pad size so we don't balloon past the pad.
    tear_w = min(tear_w, max(term["size"][0], term["size"][1]) * 1.05, track_w * 2.5)

    # If already as wide as target, skip (idempotent for re-tuned nets).
    if tear_w <= track_w + 1e-6:
        return False

    p0 = c["p"]
    u = c["u"]
    p1 = (p0[0], p0[1])
    p2 = (p0[0] + u[0] * length, p0[1] + u[1] * length)

    before = copy.deepcopy(board["tracks"])
    before_errs = { _viol_key(v) for v in _clearance_errors(board) }

    tid = _next_track_id(board)
    tear = {
        "id": tid,
        "net": c["net"],
        "layer": c["layer"],
        "width": float(round(tear_w, 6)),
        "kind": "segment",
        "p1": [float(round(p1[0], 6)), float(round(p1[1], 6))],
        "p2": [float(round(p2[0], 6)), float(round(p2[1], 6))],
        "role": "teardrop",
        "meta": {"teardrop": True, "pad": term["id"]},
    }
    board["tracks"] = list(board["tracks"]) + [tear]
    board["tracks"].sort(key=lambda t: str(t.get("id", "")))

    after_errs = { _viol_key(v) for v in _clearance_errors(board) }
    if after_errs - before_errs:
        board["tracks"] = before
        report["reverted"] += 1
        return False
    return True
