"""spec-sketch bench (IP-13): does the produced sketch's solver land FULL, with realized
dimensions within tolerance of the requested spec?

Score per example (survey §6): dense solver reward — full == solver status "full" (every
degree of freedom pinned by a relation or driving dimension) AND every requested dimension
realized within `spec.tol` of the target. Specs (frozen fixtures) + baseline come from this
module; there is no generated corpus (unlike regen-fix) — the spec set itself is the frozen
dataset under `training/benches/spec_sketch/specs/`.

Two evaluation paths:
  * fixture mode (offline, default): the deterministic template filler builds a sketch
    feature for the spec and is scored STRUCTURALLY. Because there is no C++ constraint
    solver available offline, "solver status" is stood in for by an honest DOF-counting
    proxy (`solver_status_proxy`): every entity contributes its raw degrees of freedom
    (line=4, circle=3); every constraint removes the DOF it is documented to remove
    (coincident=2, horizontal/vertical=1, distance=1, diameter=1, fix=2). `full` iff the
    remainder is exactly 0. This is a stand-in, not the real solver — the real solver
    status only comes from the live-core path below.
  * live mode (core present): the same filler's sketch is built via `core_bridge`
    (`doc_new` + `doc_apply_ops`) and the REAL solver status is read from
    `doc_sketch_solve` (the same RPC the `check_sketch` MCP tool wraps) — note
    `services.verify.battery.verify(..., ["check_sketch"])`'s binary score only reflects
    whether the RPC call itself succeeded (check_sketch always returns `ok: True` on a
    successful solve regardless of full/under/over), so it cannot carry the full/under/over
    distinction on its own; we still call it (for `battery_scores`, consistent with
    "score through the battery") and additionally read `status` directly off the
    `doc_sketch_solve` result for the actual pass/fail. Skipped-with-reason (falls back to
    fixture scoring) when the core is unavailable — never a hardcoded sibling-worktree path.

Realized dimensions are always measured geometrically from the produced sketch's entity
coordinates (bounding boxes, point-to-point distances, circle radii) — never echoed back
from the input spec.
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Optional

from ..baseline import Baseline, pending_frontier
from ..dataset import DatasetSpec
from ..metrics import mean, pass_rate, residual as residual_metric
from ..driver import BENCHES_DIR, BenchDriver
from ..schema import BenchResult, ExampleScore

SPECS_SUBDIR = "spec_sketch"


# ---- deterministic parametric template filler --------------------------------

def _line(eid: str, p1: list[float], p2: list[float]) -> dict:
    return {"id": eid, "type": "line", "construction": False, "p1": list(p1), "p2": list(p2)}


def _circle(eid: str, center: list[float], radius: float) -> dict:
    return {"id": eid, "type": "circle", "construction": False, "center": list(center), "radius": radius}


def _coincident(cid: str, refs: list[str]) -> dict:
    return {"id": cid, "type": "coincident", "refs": refs}


def _hv(cid: str, ctype: str, refs: list[str]) -> dict:
    return {"id": cid, "type": ctype, "refs": refs}


def _fix(cid: str, refs: list[str]) -> dict:
    return {"id": cid, "type": "fix", "refs": refs}


def _distance(cid: str, refs: list[str], value: float) -> dict:
    return {"id": cid, "type": "distance", "refs": refs,
            "value": {"value": value, "expr": None}, "driven": False}


def _diameter(cid: str, refs: list[str], value: float) -> dict:
    return {"id": cid, "type": "diameter", "refs": refs,
            "value": {"value": value, "expr": None}, "driven": False}


def _build_rect_plate(dims: dict) -> tuple[list[dict], list[dict]]:
    w, h = float(dims["width"]), float(dims["height"])
    entities = [
        _line("e1", [0.0, 0.0], [w, 0.0]),
        _line("e2", [w, 0.0], [w, h]),
        _line("e3", [w, h], [0.0, h]),
        _line("e4", [0.0, h], [0.0, 0.0]),
    ]
    constraints = [
        _coincident("c1", ["e1.p2", "e2.p1"]), _coincident("c2", ["e2.p2", "e3.p1"]),
        _coincident("c3", ["e3.p2", "e4.p1"]), _coincident("c4", ["e4.p2", "e1.p1"]),
        _hv("c5", "horizontal", ["e1"]), _hv("c6", "horizontal", ["e3"]),
        _hv("c7", "vertical", ["e2"]), _hv("c8", "vertical", ["e4"]),
        _fix("c9", ["e1.p1"]),
        _distance("c10", ["e1.p1", "e1.p2"], w),
        _distance("c11", ["e2.p1", "e2.p2"], h),
    ]
    return entities, constraints


def _build_circle_hole(dims: dict) -> tuple[list[dict], list[dict]]:
    d = float(dims["diameter"])
    entities = [_circle("e1", [0.0, 0.0], d / 2.0)]
    constraints = [_diameter("c1", ["e1"], d), _fix("c2", ["e1.center"])]
    return entities, constraints


def _build_slotted_plate(dims: dict) -> tuple[list[dict], list[dict]]:
    w, h = float(dims["width"]), float(dims["height"])
    slot_len, slot_d = float(dims["slot_length"]), float(dims["slot_dia"])
    cy = h / 2.0
    cx1, cx2 = w / 2.0 - slot_len / 2.0, w / 2.0 + slot_len / 2.0
    entities = [
        _line("e1", [0.0, 0.0], [w, 0.0]), _line("e2", [w, 0.0], [w, h]),
        _line("e3", [w, h], [0.0, h]), _line("e4", [0.0, h], [0.0, 0.0]),
        _circle("e5", [cx1, cy], slot_d / 2.0), _circle("e6", [cx2, cy], slot_d / 2.0),
    ]
    constraints = [
        _coincident("c1", ["e1.p2", "e2.p1"]), _coincident("c2", ["e2.p2", "e3.p1"]),
        _coincident("c3", ["e3.p2", "e4.p1"]), _coincident("c4", ["e4.p2", "e1.p1"]),
        _hv("c5", "horizontal", ["e1"]), _hv("c6", "horizontal", ["e3"]),
        _hv("c7", "vertical", ["e2"]), _hv("c8", "vertical", ["e4"]),
        _fix("c9", ["e1.p1"]),
        _distance("c10", ["e1.p1", "e1.p2"], w),
        _distance("c11", ["e2.p1", "e2.p2"], h),
        _fix("c12", ["e5.center"]),
        _diameter("c13", ["e5"], slot_d), _diameter("c14", ["e6"], slot_d),
        _distance("c15", ["e5.center", "e6.center"], slot_len),
        _hv("c16", "horizontal", ["e5.center", "e6.center"]),
    ]
    return entities, constraints


def _build_l_bracket(dims: dict) -> tuple[list[dict], list[dict]]:
    w, h, t = float(dims["width"]), float(dims["height"]), float(dims["leg_width"])
    entities = [
        _line("e1", [0.0, 0.0], [w, 0.0]), _line("e2", [w, 0.0], [w, t]),
        _line("e3", [w, t], [t, t]), _line("e4", [t, t], [t, h]),
        _line("e5", [t, h], [0.0, h]), _line("e6", [0.0, h], [0.0, 0.0]),
    ]
    constraints = [
        _coincident("c1", ["e1.p2", "e2.p1"]), _coincident("c2", ["e2.p2", "e3.p1"]),
        _coincident("c3", ["e3.p2", "e4.p1"]), _coincident("c4", ["e4.p2", "e5.p1"]),
        _coincident("c5", ["e5.p2", "e6.p1"]), _coincident("c6", ["e6.p2", "e1.p1"]),
        _hv("c7", "horizontal", ["e1"]), _hv("c8", "vertical", ["e2"]),
        _hv("c9", "horizontal", ["e3"]), _hv("c10", "vertical", ["e4"]),
        _hv("c11", "horizontal", ["e5"]), _hv("c12", "vertical", ["e6"]),
        _fix("c13", ["e1.p1"]),
        _distance("c14", ["e1.p1", "e1.p2"], w),
        _distance("c15", ["e2.p1", "e2.p2"], t),
        _distance("c16", ["e3.p1", "e3.p2"], w - t),
        _distance("c17", ["e4.p1", "e4.p2"], h - t),
    ]
    return entities, constraints


def _build_bolt_circle_layout(dims: dict) -> tuple[list[dict], list[dict]]:
    bcd, hd = float(dims["bolt_circle_diameter"]), float(dims["hole_diameter"])
    n = int(dims["num_holes"])
    r = bcd / 2.0
    entities: list[dict] = []
    constraints: list[dict] = []
    for i in range(n):
        angle = 2.0 * math.pi * i / n
        eid = f"e{i + 1}"
        entities.append(_circle(eid, [r * math.cos(angle), r * math.sin(angle)], hd / 2.0))
        constraints.append(_diameter(f"c{2 * i + 1}", [eid], hd))
        constraints.append(_fix(f"c{2 * i + 2}", [f"{eid}.center"]))
    return entities, constraints


FAMILY_BUILDERS = {
    "rect_plate": _build_rect_plate,
    "circle_hole": _build_circle_hole,
    "slotted_plate": _build_slotted_plate,
    "l_bracket": _build_l_bracket,
    "bolt_circle_layout": _build_bolt_circle_layout,
}

# dims checked for in-tolerance scoring, per family (kept distinct from any non-measured
# spec keys, e.g. bolt_circle_layout's integer `num_holes`).
DIMS_CHECKED = {
    "rect_plate": ("width", "height"),
    "circle_hole": ("diameter",),
    "slotted_plate": ("width", "height", "slot_length", "slot_dia"),
    "l_bracket": ("width", "height", "leg_width"),
    "bolt_circle_layout": ("bolt_circle_diameter", "hole_diameter"),
}


def fill_template(spec: dict) -> dict:
    """Deterministic spec -> fully-constrained sketch feature (aicad.part/1 storage shape:
    params.entities / params.constraints / refs.plane — see contracts/fixtures/part_bracket.json).
    This is the kill-switch candidate: given a well-formed spec it always drives entities
    directly to the requested numeric dims and adds every constraint needed to pin them.
    """
    family = spec["family"]
    if family not in FAMILY_BUILDERS:
        raise ValueError(f"unknown sketch family {family!r}; known: {sorted(FAMILY_BUILDERS)}")
    entities, constraints = FAMILY_BUILDERS[family](spec["dims"])
    return {
        "id": "s1", "type": "sketch", "name": f"Sketch_{spec['id']}", "suppressed": False,
        "params": {"entities": entities, "constraints": constraints},
        "refs": {"plane": "plane.top"},
    }


# ---- offline "solver full" proxy (DOF counting, no C++ core needed) ----------

_ENTITY_DOF = {"line": 4, "circle": 3}
_CONSTRAINT_DOF_REMOVED = {
    "coincident": 2, "horizontal": 1, "vertical": 1, "distance": 1, "diameter": 1, "fix": 2,
}


def _dof_remaining(entities: list[dict], constraints: list[dict]) -> int:
    raw = sum(_ENTITY_DOF.get(e.get("type"), 0) for e in entities)
    removed = sum(_CONSTRAINT_DOF_REMOVED.get(c.get("type"), 0) for c in constraints)
    return raw - removed


def solver_status_proxy(sketch: dict) -> str:
    """Honest offline stand-in for the real solver status ('full'/'under'/'over'). NOT the
    real solver — it is a structural DOF count over the documented constraint vocabulary.
    The real status comes from the live-core `doc_sketch_solve` path in `score_example`."""
    ents = sketch["params"]["entities"]
    cons = sketch["params"]["constraints"]
    dof = _dof_remaining(ents, cons)
    if dof == 0:
        return "full"
    return "under" if dof > 0 else "over"


# ---- realized dimensions, measured geometrically from entity coordinates -----

def _line_points(entities: list[dict]) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for e in entities:
        if e["type"] == "line":
            pts.append(tuple(e["p1"]))
            pts.append(tuple(e["p2"]))
    return pts


def measure_dims(sketch: dict, family: str) -> dict[str, float]:
    """Compute the sketch's REALIZED dimensions from its entity coordinates — never echoes
    the input spec. This is what a real dims-vs-spec comparison must measure honestly."""
    entities = sketch["params"]["entities"]
    if family == "rect_plate":
        pts = _line_points(entities)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return {"width": max(xs) - min(xs), "height": max(ys) - min(ys)}
    if family == "circle_hole":
        c = next(e for e in entities if e["type"] == "circle")
        return {"diameter": 2.0 * c["radius"]}
    if family == "slotted_plate":
        pts = _line_points(entities)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        circles = [e for e in entities if e["type"] == "circle"]
        c1, c2 = circles[0], circles[1]
        slot_length = math.hypot(c1["center"][0] - c2["center"][0], c1["center"][1] - c2["center"][1])
        slot_dia = (2.0 * c1["radius"] + 2.0 * c2["radius"]) / 2.0
        return {"width": max(xs) - min(xs), "height": max(ys) - min(ys),
                "slot_length": slot_length, "slot_dia": slot_dia}
    if family == "l_bracket":
        pts = _line_points(entities)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        width, height = max(xs) - min(xs), max(ys) - min(ys)
        interior_xs = sorted({round(x, 9) for x in xs} - {round(min(xs), 9), round(max(xs), 9)})
        leg_width = interior_xs[0] if interior_xs else 0.0
        return {"width": width, "height": height, "leg_width": leg_width}
    if family == "bolt_circle_layout":
        circles = [e for e in entities if e["type"] == "circle"]
        radii = [math.hypot(c["center"][0], c["center"][1]) for c in circles]
        return {"bolt_circle_diameter": 2.0 * mean(radii),
                "hole_diameter": mean([2.0 * c["radius"] for c in circles])}
    raise ValueError(f"unknown sketch family {family!r}")


def _dims_in_tol(spec_dims: dict, realized: dict, family: str, tol: float) -> tuple[bool, dict]:
    diffs: dict[str, float] = {}
    ok = True
    for key in DIMS_CHECKED[family]:
        target = float(spec_dims[key])
        got = float(realized.get(key, float("nan")))
        diff = abs(got - target)
        diffs[key] = diff
        if not (diff <= tol):
            ok = False
    return ok, diffs


# ---- op-wire conversion for the live-core path --------------------------------

def _to_op_feature(sketch: dict) -> dict:
    """Sketch feature in the shape `core_bridge.call('doc_apply_ops', {'op':'add_feature'})`
    validates against. Verified empirically against the built core: the engine requires
    `params.entities` and `params.constraints` (the storage shape) — a flattened
    top-level entities/constraints fails validation ("required for sketch"). So the op
    feature is the storage feature, with the sketch plane under `refs`."""
    return {
        "id": sketch["id"], "type": "sketch", "name": sketch.get("name", sketch["id"]),
        "suppressed": False,
        "params": {
            "entities": sketch["params"]["entities"],
            "constraints": sketch["params"]["constraints"],
        },
        "refs": {"plane": "plane.top"},
    }


def _make_doc_id_opener():
    """Guarded live-core opener: None when the C++ core cannot be located/loaded (never a
    hardcoded sibling-worktree path — resolution is entirely core_bridge's, via
    $CHUDWARE_LIBCHUDWARE / $CHUDWARE_DIST / the build dirs)."""
    try:
        from services.mcp import core_bridge as cb  # noqa: local import, core-only path
    except Exception:
        return None
    if not cb.available():
        return None

    def _opener(op_feature: dict) -> str:
        r = cb.call("doc_new", {"kind": "part", "name": "spec_sketch_bench"})
        doc_id = r["doc_id"]
        cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": [{"op": "add_feature", "feature": op_feature}]})
        return doc_id

    return _opener


# ---- scoring --------------------------------------------------------------

def score_sketch(spec: dict, sketch: dict, *, mode: str = "fixture",
                 solver_status: Optional[str] = None,
                 detail_extra: Optional[dict[str, Any]] = None) -> ExampleScore:
    family = spec["family"]
    tol = float(spec.get("tol", 0.01))
    realized = measure_dims(sketch, family)
    in_tol, diffs = _dims_in_tol(spec["dims"], realized, family, tol)
    status = solver_status or solver_status_proxy(sketch)
    full = status == "full"
    passed = bool(full and in_tol)
    # 1.0 = full AND in-tol (survey §6 dense solver reward); 0.5 = got exactly one of the
    # two right (partial credit so a ranking metric over candidates is not all-or-nothing);
    # 0.0 = neither.
    score = 1.0 if passed else (0.5 if (full or in_tol) else 0.0)
    detail: dict[str, Any] = {
        "family": family, "mode": mode, "solver_status": status, "tol": tol,
        "dims_spec": spec["dims"], "dims_realized": realized, "dims_diff": diffs,
        "in_tol": in_tol,
    }
    if detail_extra:
        detail.update(detail_extra)
    return ExampleScore(example_id=spec["id"], score=score, passed=passed,
                        structure=family, label=True,
                        baselines={"deterministic": score}, detail=detail)


def score_example(spec: dict, *, doc_id_opener=None) -> ExampleScore:
    """Score one spec under the deterministic template-filler baseline.

    `doc_id_opener`, when provided (live mode), is callable(op_feature)->doc_id that loads
    the built sketch into the core and returns the doc id; scoring then reads the REAL
    solver status off `doc_sketch_solve` (same RPC `check_sketch` wraps) and additionally
    records `services.verify.battery.verify(...)`'s check_sketch score for the "score
    through the battery" record — see module docstring for why the battery's binary score
    alone can't carry full/under/over. Any core error falls back to fixture-mode scoring
    with the reason recorded (skip-with-reason, like gate.sh's OCCT skip).
    """
    sketch = fill_template(spec)
    if doc_id_opener is not None:
        try:
            op_feature = _to_op_feature(sketch)
            doc_id = doc_id_opener(op_feature)
            from services.mcp import core_bridge as cb  # noqa: local import, core-only path
            from services.verify.battery import verify  # noqa: local import, core-only path
            fid = op_feature["id"]
            battery = verify(doc_id, ["check_sketch"], sketch=fid)
            extra = {"battery_scores": battery.get("scores", {})}
            try:
                solved = cb.call("doc_sketch_solve", {"doc_id": doc_id, "sketch_fid": fid})
                status = solved.get("status", "under")
            except Exception as se:
                # doc_sketch_solve isn't in every core build; keep the (real) through-battery
                # check_sketch result and derive status from the DOF proxy, recording why.
                status = solver_status_proxy(sketch)
                extra["solver_status_source"] = "proxy (doc_sketch_solve unavailable)"
                extra["solver_note"] = f"{type(se).__name__}: {se}"[:120]
            return score_sketch(spec, sketch, mode="live", solver_status=status,
                                detail_extra=extra)
        except Exception as e:  # core error -> fall back to fixture scoring, record why
            return score_sketch(spec, sketch, mode="fixture",
                                detail_extra={"live_error": f"{type(e).__name__}: {e}"[:160]})
    return score_sketch(spec, sketch, mode="fixture")


def load_specs(specs_dir: str) -> list[dict]:
    specs = []
    for name in sorted(os.listdir(specs_dir)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(specs_dir, name), encoding="utf-8") as f:
            specs.append(json.load(f))
    return specs


def run(specs_dir: str, dataset_hash: str, *, split: str = "all",
        limit: Optional[int] = None, ids: Optional[list[str]] = None,
        doc_id_opener=None, frontier: Optional[Baseline] = None) -> BenchResult:
    specs = load_specs(specs_dir)
    if ids is not None:
        wanted = set(ids)
        specs = [s for s in specs if s["id"] in wanted]
    if limit is not None:
        specs = specs[:limit]
    examples = [score_example(s, doc_id_opener=doc_id_opener) for s in specs]

    det_mean = mean([e.score for e in examples])
    det_pass = pass_rate([e.passed for e in examples])
    in_tol_rate = pass_rate([bool(e.detail.get("in_tol")) for e in examples])
    full_solve_rate = pass_rate([e.detail.get("solver_status") == "full" for e in examples])

    per_family: dict[str, dict] = {}
    for e in examples:
        d = per_family.setdefault(e.structure, {"n": 0, "passed": 0})
        d["n"] += 1
        d["passed"] += int(e.passed)

    frontier = frontier or pending_frontier()
    baselines = [
        Baseline("deterministic", "deterministic", fn=lambda x: x).row(
            mean_score=det_mean, pass_rate=det_pass),
        frontier.row(),
    ]
    resid = ({"status": "pending", "baseline_mean": det_mean,
              "note": "frontier candidate pending; residual measured when it lands"}
             if not frontier.available else
             residual_metric(mean([e.baselines.get(frontier.name, 0.0) for e in examples]),
                             det_mean))

    return BenchResult(
        bench_id="spec_sketch", dataset_id="spec_sketch_specs", dataset_hash=dataset_hash,
        split=split, n_examples=len(examples), examples=examples, baselines=baselines,
        aggregate={
            "in_tol_rate": in_tol_rate, "full_solve_rate": full_solve_rate,
            "deterministic_mean": det_mean, "per_family": per_family,
            "success_bar": {"full_and_in_tol": det_pass >= 0.99},
        },
        residual=resid if isinstance(resid, dict) else {**resid, "status": "measured"},
    )


# ---- runner registration ---------------------------------------------------

def _run(smoke: bool, workdir: str) -> BenchResult:
    specs_dir = os.path.join(BENCHES_DIR, SPECS_SUBDIR, "specs")
    manifest_path = os.path.join(BENCHES_DIR, SPECS_SUBDIR, "spec.json")
    ds = DatasetSpec.load(manifest_path, specs_dir)
    doc_id_opener = _make_doc_id_opener()
    ids = None
    if smoke:
        # one example per family: small + still covers every held-out structure.
        seen: set[str] = set()
        ids = []
        for s in load_specs(specs_dir):
            if s["family"] not in seen:
                ids.append(s["id"])
                seen.add(s["family"])
    return run(specs_dir, ds.hash, split="smoke" if smoke else "all",
              ids=ids, doc_id_opener=doc_id_opener)


DRIVER = BenchDriver("spec_sketch", _run)  # discovered by runner auto-load (no register() call)
