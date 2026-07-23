"""Stackup advisor — impedance-target-driven layer-stackup + trace-geometry solver (T-FIN12).

Given a copper layer count, per-net-class impedance targets (e.g. 50R single-ended, 90R/100R
differential) and a fab-capability profile (min trace/space, dielectric menu), solve:

  * the **stackup**: layer order, prepreg/core heights, reference-plane assignment — taken from
    the fab's published controlled-impedance menu (fab houses build FIXED stackups; we pick from
    the menu rather than invent unbuildable dielectric heights);
  * the **trace geometries** (width, and diff-pair gap) that hit each impedance target on every
    signal layer, by **numerically inverting the validated IPC-2141A closed forms in**
    :mod:`engine.ecad.impedance` (the forward math is REUSED, never reimplemented — the same
    ``microstrip_z0`` / ``stripline_z0`` / ``diff_pair_z0`` that ``ecad_check_impedance`` uses,
    and the same :func:`engine.ecad.impedance._stackup_geometry` layer classification, so a
    solved geometry round-trips through the checker exactly).

Inversion is a plain bisection: every model here is strictly monotone DECREASING in trace width
(wider trace -> lower Z0), so the root is unique and bracketed by [min_trace, max_trace].

Feasibility is honest: when a target cannot be met the solver raises :class:`StackupInfeasible`
naming the BINDING constraint — e.g. ``min_trace`` (the target impedance needs a trace narrower
than the fab can etch), ``max_trace`` (target lower than the widest sane trace can reach), or
``min_space`` (diff pair cannot couple tightly enough). No silent clamping.

Fab profiles live as data files in ``engine/ecad/fab/profiles/*.json`` (JLC-style 4/6-layer
menus + a generic 2-layer). Each carries the published stackup layers, copper thicknesses,
min trace/space, and candidate diff-pair gaps.

Output feeds the board document directly (:func:`apply_solution`): the stackup replaces
``board['stackup']`` and each target becomes/updates a ``net_classes`` entry with the solved
``track_width`` (and ``diff_gap`` for pairs), which the autorouter (``autoroute._track_width`` /
``_diff_gap``) and DRC (``drc._clearance_for``) already honour.

GPL-clean: stdlib + engine.ecad.impedance (numpy/scipy) only.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any

from services.ecad import schema
from services.ecad.impedance import (
    _stackup_geometry,
    diff_pair_z0,
    microstrip_z0,
    stripline_z0,
)

PROFILE_DIR = Path(__file__).resolve().parent / "fab" / "profiles"

# Round-trip tolerance for the bisection (ohm). The closed forms are only good to ~5%, so
# solving tighter than 0.01 ohm is free and keeps round-trips through ecad_check_impedance exact.
_Z_TOL = 0.01
_MAX_ITER = 200


class StackupInfeasible(ValueError):
    """An impedance target cannot be met within the fab's capability.

    ``binding`` names the constraint that binds (``min_trace`` / ``max_trace`` / ``min_space``),
    ``detail`` carries the numbers so callers can report honestly."""

    def __init__(self, message: str, binding: str, detail: dict | None = None):
        super().__init__(message)
        self.binding = binding
        self.detail = detail or {}


# --------------------------------------------------------------------------- fab profiles


def list_profiles() -> list[str]:
    """Names of every bundled fab-capability profile (sorted)."""
    return sorted(p.stem for p in PROFILE_DIR.glob("*.json"))


def get_profile(name: str) -> dict:
    """Load a bundled fab profile by name. Raises ``ValueError`` naming the available menu."""
    p = PROFILE_DIR / f"{name}.json"
    if not p.is_file():
        raise ValueError(f"unknown fab profile {name!r}; available: {', '.join(list_profiles())}")
    return json.loads(p.read_text(encoding="utf-8"))


def profiles_for_layers(copper_layers: int) -> list[dict]:
    """All bundled profiles whose menu builds the requested copper-layer count."""
    return [get_profile(n) for n in list_profiles()
            if get_profile(n).get("copper_layers") == int(copper_layers)]


# --------------------------------------------------------------------------- inversion core


def _forward_se(w: float, kind: str, h: float, t: float, er: float) -> float:
    if kind == "stripline":
        return stripline_z0(w, h, t, er)["z0"]
    return microstrip_z0(w, h, t, er)["z0"]


def _forward_diff(w: float, s: float, kind: str, h: float, t: float, er: float) -> float:
    return diff_pair_z0(w, s, h, t, er, kind=kind)["z0_diff"]


def _bisect_width(z_of_w, target: float, w_lo: float, w_hi: float) -> float | None:
    """Solve ``z_of_w(w) == target`` for the strictly-decreasing ``z_of_w`` on [w_lo, w_hi].

    Returns None when the target is not bracketed (caller names the binding constraint)."""
    z_lo = z_of_w(w_lo)   # narrowest -> HIGHEST impedance
    z_hi = z_of_w(w_hi)   # widest    -> LOWEST impedance
    if target > z_lo + _Z_TOL or target < z_hi - _Z_TOL:
        return None
    for _ in range(_MAX_ITER):
        mid = 0.5 * (w_lo + w_hi)
        z = z_of_w(mid)
        if abs(z - target) <= _Z_TOL:
            return mid
        if z > target:
            w_lo = mid   # too narrow (Z too high) -> widen
        else:
            w_hi = mid
    return 0.5 * (w_lo + w_hi)


def solve_se_width(target_z0: float, kind: str, h: float, t: float, er: float,
                   min_trace: float, max_trace: float = 5.0) -> float:
    """Trace width (mm) whose single-ended Z0 equals ``target_z0`` on this layer geometry.

    Raises :class:`StackupInfeasible` naming ``min_trace`` / ``max_trace`` when out of range."""
    z_at_min = _forward_se(min_trace, kind, h, t, er)
    if target_z0 > z_at_min + _Z_TOL:
        raise StackupInfeasible(
            f"{target_z0:.1f} ohm single-ended needs a trace narrower than the fab minimum: "
            f"even at min_trace={min_trace:.3f} mm this {kind} only reaches "
            f"{z_at_min:.1f} ohm (h={h:.4f} mm, er={er:.2f}). Binding constraint: min_trace.",
            binding="min_trace",
            detail={"target": target_z0, "z_at_min_trace": z_at_min,
                    "min_trace": min_trace, "kind": kind, "h": h, "er": er})
    z_at_max = _forward_se(max_trace, kind, h, t, er)
    if target_z0 < z_at_max - _Z_TOL:
        raise StackupInfeasible(
            f"{target_z0:.1f} ohm single-ended is below what even a {max_trace:.1f} mm-wide "
            f"{kind} reaches ({z_at_max:.1f} ohm) on this dielectric (h={h:.4f} mm). "
            f"Binding constraint: max_trace.",
            binding="max_trace",
            detail={"target": target_z0, "z_at_max_trace": z_at_max, "max_trace": max_trace})
    w = _bisect_width(lambda w: _forward_se(w, kind, h, t, er), target_z0, min_trace, max_trace)
    assert w is not None  # bracketing checked above
    return w


def solve_diff_geometry(target_zdiff: float, kind: str, h: float, t: float, er: float,
                        min_trace: float, min_space: float,
                        gap_candidates: list[float] | None = None,
                        max_trace: float = 5.0) -> tuple[float, float]:
    """(width, gap) in mm whose differential impedance equals ``target_zdiff``.

    Tries each candidate edge-to-edge gap (fab-preferred order, filtered to >= min_space) and
    solves the width by bisection at that gap; the first gap giving a manufacturable width wins.
    Raises :class:`StackupInfeasible` naming the binding constraint when no gap works."""
    gaps = [g for g in (gap_candidates or []) if g >= min_space - 1e-9]
    if min_space not in gaps:
        gaps.append(min_space)
    last_reason: StackupInfeasible | None = None
    for s in gaps:
        z_at_min = _forward_diff(min_trace, s, kind, h, t, er)
        if target_zdiff > z_at_min + _Z_TOL:
            last_reason = StackupInfeasible(
                f"{target_zdiff:.1f} ohm differential needs a trace narrower than "
                f"min_trace={min_trace:.3f} mm at gap {s:.3f} mm (reaches only "
                f"{z_at_min:.1f} ohm). Binding constraint: min_trace.",
                binding="min_trace",
                detail={"target": target_zdiff, "z_at_min_trace": z_at_min, "gap": s})
            continue
        w = _bisect_width(lambda w: _forward_diff(w, s, kind, h, t, er),
                          target_zdiff, min_trace, max_trace)
        if w is None:
            last_reason = StackupInfeasible(
                f"{target_zdiff:.1f} ohm differential not reachable at gap {s:.3f} mm within "
                f"[{min_trace:.3f}, {max_trace:.1f}] mm width. Binding constraint: max_trace.",
                binding="max_trace", detail={"target": target_zdiff, "gap": s})
            continue
        return w, s
    if last_reason is not None:
        raise last_reason
    raise StackupInfeasible(
        f"no diff-pair gap >= min_space={min_space:.3f} mm available. "
        f"Binding constraint: min_space.",
        binding="min_space", detail={"min_space": min_space})


# --------------------------------------------------------------------------- target parsing


def _normalize_targets(targets: dict) -> dict[str, dict]:
    """Normalize ``{name: 50}`` / ``{name: {'type':'diff','z0':100,...}}`` into a canonical
    ``{name: {'type','z0'}}`` dict. Unknown types are rejected loudly."""
    out: dict[str, dict] = {}
    for name, spec in (targets or {}).items():
        if isinstance(spec, (int, float)):
            spec = {"type": "se", "z0": float(spec)}
        if not isinstance(spec, dict) or "z0" not in spec:
            raise ValueError(f"target {name!r}: expected a number or {{'type','z0'}} dict")
        kind = str(spec.get("type", "se")).lower()
        if kind in ("se", "single", "single_ended", "single-ended"):
            kind = "se"
        elif kind in ("diff", "differential", "pair", "diff_pair"):
            kind = "diff"
        else:
            raise ValueError(f"target {name!r}: unknown type {spec.get('type')!r} "
                             "(use 'se' or 'diff')")
        out[str(name)] = {"type": kind, "z0": float(spec["z0"])}
    return out


# --------------------------------------------------------------------------- the solver


def solve_stackup(copper_layers: int, targets: dict, profile: str | dict = "auto") -> dict:
    """Solve stackup + trace geometries for the given impedance targets.

    Args:
      copper_layers: requested copper-layer count (must match a fab profile's menu).
      targets: ``{class_name: z0}`` or ``{class_name: {'type':'se'|'diff', 'z0': ohms}}``.
      profile: a profile name (see :func:`list_profiles`), a loaded profile dict, or ``"auto"``
        to pick the first bundled profile with the requested layer count.

    Returns ``{profile, stackup, layer_roles, signal_layers, geometries}`` where ``geometries``
    maps each class name to ``{type, z0_target, layers: {layer_id: {width, gap?, kind, z0}},
    track_width, diff_gap?}`` — ``track_width`` is the solved width on the PRIMARY routing layer
    (outermost signal layer), the value wired into the net class.

    Raises :class:`StackupInfeasible` (with ``.binding``) when any target cannot be met.
    """
    copper_layers = int(copper_layers)
    if isinstance(profile, dict):
        prof = profile
    elif profile in (None, "", "auto"):
        cands = profiles_for_layers(copper_layers)
        if not cands:
            raise ValueError(
                f"no bundled fab profile builds {copper_layers} copper layers; "
                f"available profiles: {', '.join(list_profiles())}")
        prof = cands[0]
    else:
        prof = get_profile(str(profile))
    if int(prof.get("copper_layers", 0)) != copper_layers:
        raise ValueError(
            f"profile {prof.get('name')!r} builds {prof.get('copper_layers')} copper layers, "
            f"not {copper_layers}; pick a matching profile ({', '.join(list_profiles())})")

    norm = _normalize_targets(targets)
    if not norm:
        raise ValueError("no impedance targets given")

    layers = copy.deepcopy(prof["stackup_layers"])
    roles = list(prof.get("layer_roles") or ["signal"] * copper_layers)
    cu_ids = schema.copper_layer_ids(copper_layers)
    signal_layers = [cu for cu, role in zip(cu_ids, roles) if role == "signal"]
    if not signal_layers:
        raise ValueError(f"profile {prof.get('name')!r} has no signal layers")

    min_trace = float(prof.get("min_trace", 0.09))
    min_space = float(prof.get("min_space", 0.09))
    max_trace = float(prof.get("max_trace", 5.0))
    gap_menu = [float(g) for g in prof.get("diff_gaps", [])]

    # A board-shaped dict so the SAME layer classifier the impedance checker uses drives us.
    board_like = {"stackup": {"copper_layers": copper_layers,
                              "thickness": float(prof.get("board_thickness", 1.6)),
                              "layers": layers}}

    geometries: dict[str, dict] = {}
    for name, spec in norm.items():
        per_layer: dict[str, dict] = {}
        for cu in signal_layers:
            geo = _stackup_geometry(board_like, cu)   # REUSED classifier: kind/h/t/er
            kind, h, t, er = geo["kind"], geo["h"], geo["t"], geo["er"]
            if spec["type"] == "se":
                w = solve_se_width(spec["z0"], kind, h, t, er, min_trace, max_trace)
                z = _forward_se(w, kind, h, t, er)
                per_layer[cu] = {"width": round(w, 4), "kind": kind, "z0": round(z, 2),
                                 "h": h, "er": er, "ref_planes": geo["ref_planes"]}
            else:
                w, s = solve_diff_geometry(spec["z0"], kind, h, t, er,
                                           min_trace, min_space, gap_menu, max_trace)
                z = _forward_diff(w, s, kind, h, t, er)
                per_layer[cu] = {"width": round(w, 4), "gap": round(s, 4), "kind": kind,
                                 "z0": round(z, 2), "h": h, "er": er,
                                 "ref_planes": geo["ref_planes"]}
        primary = per_layer[signal_layers[0]]
        entry: dict[str, Any] = {"type": spec["type"], "z0_target": spec["z0"],
                                 "layers": per_layer,
                                 "track_width": primary["width"]}
        if spec["type"] == "diff":
            entry["diff_gap"] = primary["gap"]
        geometries[name] = entry

    return {
        "profile": prof.get("name"),
        "stackup": {"copper_layers": copper_layers,
                    "thickness": float(prof.get("board_thickness", 1.6)),
                    "layers": layers},
        "layer_roles": dict(zip(cu_ids, roles)),
        "signal_layers": signal_layers,
        "min_trace": min_trace, "min_space": min_space,
        "via": dict(prof.get("via", {})),
        "geometries": geometries,
    }


# --------------------------------------------------------------------------- board wiring


def apply_solution(board: dict, solution: dict, net_map: dict[str, list[str]] | None = None,
                   clearance: float | None = None) -> dict:
    """Write a solved stackup + net classes into a board document dict (mutates ``board``).

    * ``board['stackup']`` is replaced by the solved stackup (layer order + heights).
    * Each geometry becomes/updates a ``board['net_classes']`` entry with the solved
      ``track_width`` (+ ``diff_gap`` for pairs) and the profile's via dims — the exact keys
      ``autoroute._track_width`` / ``_via_dims`` / ``_diff_gap`` and DRC ``_clearance_for`` read,
      so routing and rule checks honour the solve with no further wiring.
    * ``net_map`` optionally assigns existing nets to the new classes:
      ``{'diff_100': ['USB_DP', 'USB_DN'], ...}``.

    Returns ``{classes: [...], assigned: {net: class}}`` for reporting.
    """
    board["stackup"] = copy.deepcopy(solution["stackup"])
    ncs = board.setdefault("net_classes", {})
    via = solution.get("via", {})
    clr = float(clearance) if clearance is not None else max(
        float(solution.get("min_space", 0.09)),
        float(board.get("rules", {}).get("clearance", 0.0)))
    for name, geo in solution["geometries"].items():
        cls = dict(ncs.get(name, {}))
        cls["clearance"] = clr
        cls["track_width"] = float(geo["track_width"])
        if geo["type"] == "diff":
            cls["diff_gap"] = float(geo["diff_gap"])
        if via.get("diameter"):
            cls["via_diameter"] = float(via["diameter"])
        if via.get("drill"):
            cls["via_drill"] = float(via["drill"])
        cls["z0_target"] = float(geo["z0_target"])   # provenance for SI pack / reports
        ncs[name] = cls
    assigned: dict[str, str] = {}
    if net_map:
        known = {n.get("name"): n for n in board.get("nets", []) if isinstance(n, dict)}
        for cls_name, nets in net_map.items():
            if cls_name not in ncs:
                raise ValueError(f"net_map references unknown class {cls_name!r}")
            for net in nets or []:
                if net not in known:
                    board.setdefault("nets", []).append({"name": net, "class": cls_name})
                else:
                    known[net]["class"] = cls_name
                assigned[net] = cls_name
    return {"classes": sorted(solution["geometries"]), "assigned": assigned}
