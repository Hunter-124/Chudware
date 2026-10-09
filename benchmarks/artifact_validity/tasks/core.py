"""Core CAD / simulation tasks C1-C3, graded only from the reopened ``deliverable.step+``.

C1  plate with a 4 × 4 grid of Ø6 through holes — exact hole enumeration from native
    cylinder faces and their end circles (replaces the volume-only hole count, which
    accepted 15 holes: V16 = 95 476.1063 mm³ vs V15 = 95 758.8499 mm³ differ by 0.296 %).
C2  edit-and-preserve-intent on a hashed native bushing fixture: bore Ø20 -> Ø25 while the
    flange, hub, bolt circle and the feature history (ids, params, sketch relations) stay.
C3  steel cantilever static study persisted in the part, compared with beam theory.

Each check is decorated with :func:`check` (stable id; a raise is ERROR, never a vanished
check) and starts with :func:`gate` (missing/empty/unopenable artifact -> FAIL, grader
engine down -> ERROR). Oracle derivations live in each task's ``notes`` (human-only).
"""
from __future__ import annotations

import functools
import math
from pathlib import Path
from typing import Any, Callable

from benchmarks.artifact_validity import geometry_checks as gc
from benchmarks.artifact_validity.evidence import Evidence, build_evidence, gate, sha256_file
from benchmarks.artifact_validity.tasks.schema import CheckResult, Status, Task, check

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

Z: gc.Vec = (0.0, 0.0, 1.0)


def _result(cid: str, ok: bool, measured: Any, expected: Any, tol: Any,
            detail: str = "") -> CheckResult:
    return CheckResult(cid, Status.PASS if ok else Status.FAIL, measured=measured,
                       expected=expected, tolerance=tol, detail=detail)


def _gated(cid: str) -> Callable[[Callable[[Evidence, str], CheckResult]], Callable]:
    """``@check(cid)`` plus the artifact gate, so every body starts on an opened document."""

    def deco(body: Callable[[Evidence, str], CheckResult]):
        @check(cid)
        @functools.wraps(body)
        def run(ev: Evidence) -> CheckResult:
            return gate(ev, cid) or body(ev, cid)

        return run

    return deco


def _rel(measured: float, expected: float) -> float:
    return abs(measured - expected) / abs(expected)


# ============================================================================ shared

def _single_sound_solid(cid: str, ev: Evidence) -> CheckResult:
    s = gc.solid_bodies(ev)
    if s["unknown"]:
        return CheckResult(cid, Status.ERROR, measured=s,
                           detail="body validity/closure flags unavailable from the engine")
    return _result(cid, s["count"] == 1 and s["sound"], s, "exactly 1 valid closed solid",
                   None, "" if s["count"] == 1 else f"{s['count']} bodies")


def _history(cid: str, ev: Evidence) -> CheckResult:
    faults = gc.history_faults(ev)
    return _result(cid, not faults,
                   [f"{f.get('id')}:{f.get('type')}" for f in ev.features],
                   "parametric feature history, every feature rebuilt", None,
                   "; ".join(faults))


# ============================================================================ C1

C1_PLATE = (100.0, 100.0, 10.0)
C1_HOLE_D = 6.0
C1_GRID = (-30.0, -10.0, 10.0, 30.0)
C1_CENTERS = [(x, y) for x in C1_GRID for y in C1_GRID]
C1_TOL_MM = 0.05
#: 100*100*10 - 16 * pi * 3^2 * 10
C1_VOLUME = C1_PLATE[0] * C1_PLATE[1] * C1_PLATE[2] - 16 * math.pi * 9.0 * 10.0
C1_VOLUME_REL = 0.005


def _c1_frame(ev: Evidence) -> tuple[dict, list[gc.Cylinder]]:
    """The plate box and every bore (any axis) of the deliverable."""
    return gc.part_bbox(ev), gc.bores(ev)


def _c1_through_z(ev: Evidence) -> tuple[dict, list[gc.Cylinder], list[gc.Cylinder]]:
    bb, bores = _c1_frame(ev)
    zlo, zhi = bb["min"][2], bb["max"][2]
    through = [b for b in gc.along(bores, Z) if gc.spans(b, zlo, zhi, C1_TOL_MM)]
    return bb, bores, through


@_gated("C1.solid")
def c1_solid(ev: Evidence, cid: str) -> CheckResult:
    return _single_sound_solid(cid, ev)


@_gated("C1.plate_size")
def c1_plate_size(ev: Evidence, cid: str) -> CheckResult:
    size = gc.part_bbox(ev)["size"]
    return _result(cid, gc.within(size, C1_PLATE, C1_TOL_MM), [round(s, 4) for s in size],
                   list(C1_PLATE), C1_TOL_MM, "bbox X×Y×Z in mm (10 mm thickness along Z)")


@_gated("C1.hole_count")
def c1_hole_count(ev: Evidence, cid: str) -> CheckResult:
    _bb, bores, through = _c1_through_z(ev)
    others = [b.to_json() for b in bores if b not in through]
    return _result(cid, len(through) == 16 and not others,
                   {"through_z": len(through), "other_bores": others}, 16, 0,
                   "exactly 16 Z-axis through bores and no other bore")


@_gated("C1.hole_diameter")
def c1_hole_diameter(ev: Evidence, cid: str) -> CheckResult:
    _bb, _bores, through = _c1_through_z(ev)
    ds = sorted(round(b.diameter, 4) for b in through)
    ok = bool(ds) and all(abs(d - C1_HOLE_D) <= C1_TOL_MM for d in ds)
    return _result(cid, ok, ds, C1_HOLE_D, C1_TOL_MM,
                   "" if ds else "no Z-axis through bore found")


@_gated("C1.hole_positions")
def c1_hole_positions(ev: Evidence, cid: str) -> CheckResult:
    bb, _bores, through = _c1_through_z(ev)
    cx = (bb["min"][0] + bb["max"][0]) / 2.0
    cy = (bb["min"][1] + bb["max"][1]) / 2.0
    found = []
    for b in through:
        p = b.center_in_plane(Z)
        found.append((p[0] - cx, p[1] - cy))
    m = gc.match_points(found, C1_CENTERS, C1_TOL_MM)
    ok = m["matched"] == 16 and not m["missing"] and not m["extra"]
    return _result(cid, ok, m, "XY offsets from plate centre in {-30,-10,10,30}²",
                   C1_TOL_MM)


@_gated("C1.volume")
def c1_volume(ev: Evidence, cid: str) -> CheckResult:
    v = gc.volume_mm3(ev)
    return _result(cid, _rel(v, C1_VOLUME) <= C1_VOLUME_REL, round(v, 3),
                   round(C1_VOLUME, 4), f"{C1_VOLUME_REL:.1%}",
                   "secondary invariant only; hole count/size/position are checked natively")


@_gated("C1.history")
def c1_history(ev: Evidence, cid: str) -> CheckResult:
    return _history(cid, ev)


C1 = Task(
    id="C1", version=1, domain="cad",
    prompt=(
        "Model a flat rectangular plate 100 mm along X, 100 mm along Y and 10 mm thick "
        "along Z. Drill 16 through holes of diameter 6 mm, all parallel to Z, as a 4 x 4 "
        "grid centred on the plate: the hole centres are at X and Y offsets of -30, -10, "
        "+10 and +30 mm from the centre of the plate (20 mm pitch). Build it as an "
        "editable parametric part and save it in Chudware's native format as "
        "`deliverable.step+` in the working directory."
    ),
    checks=(c1_solid, c1_plate_size, c1_hole_count, c1_hole_diameter, c1_hole_positions,
            c1_volume, c1_history),
    budget_s=900,
    notes=(
        "Oracle: V = 100*100*10 - 16*pi*3^2*10 = 95 476.1063 mm^3 (secondary; 15 holes "
        "would be 95 758.8499 mm^3, only 0.296 % away, so count/size/position are measured "
        "natively). A hole counts only when it is a concave cylinder wall parallel to Z "
        "whose same-radius end circles sit on both plate faces and whose wall area equals "
        "a full revolution over the plate thickness. Centres are the axis/mid-plane "
        "crossings relative to the plate bbox centre; diameters and centres +-0.05 mm."
    ),
)


# ============================================================================ C2

C2_FIXTURE = "c2_bushing.step+"
#: Bytes the grader was written against; regenerate via fixtures/build_c2_bushing.py.
C2_FIXTURE_SHA256 = "a5b660031cb2a4ef4b1b6d012f1fe1b2c3ec0b6cad0192b6726bc81a227dca66"
C2_BORE_ID = "f5"
C2_BORE_FROM, C2_BORE_TO = 20.0, 25.0
C2_FLANGE_D, C2_FLANGE_T = 70.0, 8.0
C2_HUB_D, C2_LENGTH = 36.0, 30.0
C2_BOLT_D, C2_PCD, C2_BOLTS = 6.6, 52.0, 4
C2_BOLT_CENTERS = [(C2_PCD / 2 * math.cos(math.radians(a)), C2_PCD / 2 * math.sin(math.radians(a)))
                   for a in (0.0, 90.0, 180.0, 270.0)]
C2_DIA_TOL = 0.02
C2_POS_TOL = 0.05
#: flange + hub - Ø25 through bore - 4 bolt holes through the flange
C2_VOLUME = (math.pi * 35.0 ** 2 * 8.0 + math.pi * 18.0 ** 2 * 22.0
             - math.pi * 12.5 ** 2 * 30.0 - 4 * math.pi * 3.3 ** 2 * 8.0)
C2_VOLUME_REL = 0.005


@functools.lru_cache(maxsize=1)
def _c2_reference() -> dict:
    """The pristine fixture's feature history, read from the suite's own copy (never the
    workspace copy the agent may have edited), after verifying its declared digest."""
    path = FIXTURES_DIR / C2_FIXTURE
    digest = sha256_file(path)
    if digest != C2_FIXTURE_SHA256:
        raise RuntimeError(f"fixture {path.name} sha256 {digest} != declared "
                           f"{C2_FIXTURE_SHA256}; regenerate and re-declare it")
    ref = build_evidence(path)
    if ref.doc_id is None or not ref.features:
        raise RuntimeError(f"fixture {path.name} did not open: "
                           f"{ref.engine_error or ref.open_error or ref.probe_errors}")
    return {"features": [{k: f.get(k) for k in ("id", "type", "name", "params", "refs")}
                         for f in ref.features]}


def _on_axis(c: gc.Cylinder) -> bool:
    p = c.center_in_plane(Z)
    return math.hypot(p[0], p[1]) <= C2_POS_TOL


def _c2_cylinders(ev: Evidence) -> tuple[list[gc.Cylinder], list[gc.Cylinder]]:
    cyl = gc.along(gc.cylinders(ev), Z)
    return [c for c in cyl if c.kind == "bore"], [c for c in cyl if c.kind == "boss"]


def _c2_span(c: gc.Cylinder, lo: float, hi: float) -> bool:
    s = c.span
    return (s is not None and abs(s[0] - lo) <= C2_DIA_TOL and abs(s[1] - hi) <= C2_DIA_TOL
            and abs(c.length_mm - (hi - lo)) <= C2_DIA_TOL)


@_gated("C2.fixture_declared")
def c2_fixture_declared(ev: Evidence, cid: str) -> CheckResult:
    """Grader integrity: the reference fixture is the declared one (mismatch raises -> ERROR)."""
    feats = _c2_reference()["features"]
    return _result(cid, True, C2_FIXTURE_SHA256, C2_FIXTURE_SHA256, 0,
                   f"{len(feats)} reference features")


@_gated("C2.solid")
def c2_solid(ev: Evidence, cid: str) -> CheckResult:
    return _single_sound_solid(cid, ev)


@_gated("C2.envelope")
def c2_envelope(ev: Evidence, cid: str) -> CheckResult:
    bb = gc.part_bbox(ev)
    want_min = (-C2_FLANGE_D / 2, -C2_FLANGE_D / 2, 0.0)
    want_max = (C2_FLANGE_D / 2, C2_FLANGE_D / 2, C2_LENGTH)
    ok = gc.within(bb["min"], want_min, C2_POS_TOL) and gc.within(bb["max"], want_max,
                                                                    C2_POS_TOL)
    return _result(cid, ok, {"min": [round(v, 4) for v in bb["min"]],
                             "max": [round(v, 4) for v in bb["max"]]},
                   {"min": list(want_min), "max": list(want_max)}, C2_POS_TOL,
                   "part stays where the fixture put it")


@_gated("C2.bore")
def c2_bore(ev: Evidence, cid: str) -> CheckResult:
    bores, _ = _c2_cylinders(ev)
    central = [b for b in bores if _on_axis(b)]
    meas = [b.to_json() for b in central]
    ok = (len(central) == 1 and abs(central[0].diameter - C2_BORE_TO) <= C2_DIA_TOL
          and _c2_span(central[0], 0.0, C2_LENGTH))
    return _result(cid, ok, meas, f"one coaxial Ø{C2_BORE_TO} bore through z 0..{C2_LENGTH}",
                   C2_DIA_TOL)


@_gated("C2.flange_hub")
def c2_flange_hub(ev: Evidence, cid: str) -> CheckResult:
    _, bosses = _c2_cylinders(ev)
    outer = [b for b in bosses if _on_axis(b)]
    flange = [b for b in outer if abs(b.diameter - C2_FLANGE_D) <= C2_DIA_TOL
              and _c2_span(b, 0.0, C2_FLANGE_T)]
    hub = [b for b in outer if abs(b.diameter - C2_HUB_D) <= C2_DIA_TOL
           and _c2_span(b, C2_FLANGE_T, C2_LENGTH)]
    ok = len(flange) == 1 and len(hub) == 1 and len(outer) == 2
    return _result(cid, ok, [b.to_json() for b in outer],
                   {"flange": f"Ø{C2_FLANGE_D} z 0..{C2_FLANGE_T}",
                    "hub": f"Ø{C2_HUB_D} z {C2_FLANGE_T}..{C2_LENGTH}"}, C2_DIA_TOL)


@_gated("C2.bolt_circle")
def c2_bolt_circle(ev: Evidence, cid: str) -> CheckResult:
    bores, _ = _c2_cylinders(ev)
    ring = [b for b in bores if not _on_axis(b)]
    sized = [b for b in ring if abs(b.diameter - C2_BOLT_D) <= C2_DIA_TOL
             and _c2_span(b, 0.0, C2_FLANGE_T)]
    centres = [b.center_in_plane(Z)[:2] for b in sized]
    m = gc.match_points(centres, C2_BOLT_CENTERS, C2_POS_TOL)
    ok = len(ring) == C2_BOLTS and len(sized) == C2_BOLTS and m["matched"] == C2_BOLTS
    return _result(cid, ok, {"off_axis_bores": [b.to_json() for b in ring], "match": m},
                   f"{C2_BOLTS} × Ø{C2_BOLT_D} through the flange on PCD {C2_PCD} at "
                   "0/90/180/270°", {"diameter": C2_DIA_TOL, "position": C2_POS_TOL})


@_gated("C2.feature_readback")
def c2_feature_readback(ev: Evidence, cid: str) -> CheckResult:
    """The edit went through the existing history: same features (ids/types/names/refs),
    every param — sketch entities and relations included — unchanged except the bore
    feature's diameter, which reads back as 25."""
    ref = _c2_reference()["features"]
    faults = gc.history_faults(ev)
    got = {f.get("id"): f for f in ev.features}
    want_ids = [f["id"] for f in ref]
    extra = [f"{f.get('id')}:{f.get('type')}" for f in ev.features if f.get("id") not in want_ids]
    if extra:
        faults.append(f"features added beyond the fixture history: {extra}")
    for r in ref:
        f = got.get(r["id"])
        if f is None:
            faults.append(f"{r['id']} ({r['type']} {r['name']!r}) missing")
            continue
        if f.get("suppressed"):
            faults.append(f"{r['id']} suppressed")
        for key in ("type", "name", "refs"):
            if f.get(key) != r[key]:
                faults.append(f"{r['id']}.{key} changed: {r[key]!r} -> {f.get(key)!r}")
        p_ref, p_got = dict(r["params"] or {}), dict(f.get("params") or {})
        if r["id"] == C2_BORE_ID:
            d = gc.param_value(f, "diameter")
            if not isinstance(d, (int, float)) or abs(float(d) - C2_BORE_TO) > 1e-6:
                faults.append(f"{r['id']}.diameter reads back {d!r}, expected {C2_BORE_TO}")
            p_ref.pop("diameter", None)
            p_got.pop("diameter", None)
        if p_got != p_ref:
            changed = sorted(k for k in set(p_ref) | set(p_got) if p_ref.get(k) != p_got.get(k))
            faults.append(f"{r['id']} params changed: {changed}")
    return _result(cid, not faults, [f"{f.get('id')}:{f.get('type')}" for f in ev.features],
                   f"fixture history {want_ids} with {C2_BORE_ID}.diameter={C2_BORE_TO}",
                   None, "; ".join(faults))


@_gated("C2.volume")
def c2_volume(ev: Evidence, cid: str) -> CheckResult:
    v = gc.volume_mm3(ev)
    return _result(cid, _rel(v, C2_VOLUME) <= C2_VOLUME_REL, round(v, 3),
                   round(C2_VOLUME, 4), f"{C2_VOLUME_REL:.1%}", "secondary invariant")


C2 = Task(
    id="C2", version=1, domain="cad",
    prompt=(
        "The working directory contains `c2_bushing.step+`, a native, parametric flanged "
        "bushing with a 20 mm through bore. Re-bore it for a 25 mm shaft: make the bore a "
        "25 mm diameter through bore by editing the part's existing feature history (keep "
        "its features, sketches and sketch relations; do not rebuild the part or stack an "
        "extra cut on top). Everything else must stay exactly as it is: flange diameter "
        "and thickness, hub, overall length, and the bolt holes (count, size, bolt-circle "
        "diameter and angular positions). Save the edited part in Chudware's native format "
        "as `deliverable.step+` in the working directory; leave `c2_bushing.step+` as the "
        "untouched input."
    ),
    checks=(c2_fixture_declared, c2_solid, c2_envelope, c2_bore, c2_flange_hub,
            c2_bolt_circle, c2_feature_readback, c2_volume),
    fixtures=(C2_FIXTURE,),
    budget_s=900,
    notes=(
        "Fixture (sha256 declared above; builder fixtures/build_c2_bushing.py): f1 "
        "FlangeSketch circle r35 + diameter relation 70, f2 Flange extrude 8 (z 0..8), f3 "
        "HubSketch circle r18 + diameter 36 at offset 8, f4 Hub extrude 22 (z 8..30), f5 "
        "Bore hole Ø20 through_all, f6 BoltHoles bolt_circle 4 × Ø6.6 PCD 52 start 0°. "
        "Oracle after the edit: one coaxial Ø25 bore spanning z 0..30; bosses Ø70 over z "
        "0..8 and Ø36 over z 8..30; exactly 4 off-axis Ø6.6 bores over z 0..8 at "
        "(±26,0),(0,±26); bbox (-35,-35,0)..(35,35,30). V = pi*35^2*8 + pi*18^2*22 - "
        "pi*12.5^2*30 - 4*pi*3.3^2*8 = 37 360.41 mm^3 (secondary). Feature readback "
        "compares every fixture feature's type/name/refs/params (sketch entities and "
        "relations included) with the pristine fixture; only f5.diameter may change, and "
        "no feature may be added."
    ),
)


# ============================================================================ C3

C3_L, C3_B, C3_H = 120.0, 20.0, 8.0          # length X, width Y, thickness Z (mm)
C3_F = 500.0                                  # N, -Z at the free end
C3_E, C3_NU, C3_YIELD = 200_000.0, 0.3, 250.0  # MPa
C3_I = C3_B * C3_H ** 3 / 12.0                # 853.333 mm^4
C3_DEFLECTION = C3_F * C3_L ** 3 / (3.0 * C3_E * C3_I)   # 1.6875 mm
C3_STRESS = C3_F * C3_L * (C3_H / 2.0) / C3_I              # 281.25 MPa
C3_ROOT_MOMENT = C3_F * C3_L                               # 60 000 N·mm
C3_GEOM_TOL = 0.05
C3_STUDY_TYPES = ("fea_static", "fea_adaptive")
C3_NONSINGULAR = "node_averaged_excluding_fixture_ring2"


def _c3_study(ev: Evidence) -> dict | None:
    hits = [s for s in ev.studies if s.get("type") in C3_STUDY_TYPES]
    return hits[-1] if hits else None


def _no_study(cid: str, ev: Evidence) -> CheckResult:
    return CheckResult(cid, Status.FAIL, measured=[s.get("type") for s in ev.studies],
                       expected=f"a persisted study of type {C3_STUDY_TYPES}",
                       detail="no static stress study saved in the deliverable")


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _vec(x: Any) -> gc.Vec | None:
    if not isinstance(x, (list, tuple)) or len(x) != 3:
        return None
    vals = [_num(c) for c in x]
    return None if any(v is None for v in vals) else (vals[0], vals[1], vals[2])


@_gated("C3.solid")
def c3_solid(ev: Evidence, cid: str) -> CheckResult:
    return _single_sound_solid(cid, ev)


@_gated("C3.bar_geometry")
def c3_bar_geometry(ev: Evidence, cid: str) -> CheckResult:
    bb = gc.part_bbox(ev)
    ok = (gc.within(bb["size"], (C3_L, C3_B, C3_H), C3_GEOM_TOL)
          and abs(bb["min"][0]) <= C3_GEOM_TOL and abs(bb["max"][0] - C3_L) <= C3_GEOM_TOL)
    return _result(cid, ok, {"min": [round(v, 4) for v in bb["min"]],
                             "size": [round(v, 4) for v in bb["size"]]},
                   {"x": [0.0, C3_L], "size": [C3_L, C3_B, C3_H]}, C3_GEOM_TOL)


@_gated("C3.study_saved")
def c3_study_saved(ev: Evidence, cid: str) -> CheckResult:
    st = _c3_study(ev)
    if st is None:
        return _no_study(cid, ev)
    res = st.get("results") if isinstance(st.get("results"), dict) else {}
    need = {k: _num(res.get(k)) for k in ("max_disp_mm", "max_von_mises_mpa")}
    ok = all(v is not None for v in need.values()) and isinstance(st.get("reactions"), dict)
    return _result(cid, ok, {"id": st.get("id"), "type": st.get("type"),
                             "name": st.get("name"), **need,
                             "reactions": isinstance(st.get("reactions"), dict)},
                   "solved study with results and reactions", None)


@_gated("C3.material")
def c3_material(ev: Evidence, cid: str) -> CheckResult:
    st = _c3_study(ev)
    if st is None:
        return _no_study(cid, ev)
    mat = st.get("material") if isinstance(st.get("material"), dict) else {}
    e, nu, sy = _num(mat.get("E_mpa")), _num(mat.get("nu")), _num(mat.get("yield_mpa"))
    ok = (e is not None and _rel(e, C3_E) <= 0.01 and nu is not None
          and abs(nu - C3_NU) <= 0.02 and sy is not None and _rel(sy, C3_YIELD) <= 0.02)
    return _result(cid, ok, {"name": mat.get("name"), "E_mpa": e, "nu": nu, "yield_mpa": sy},
                   {"E_mpa": C3_E, "nu": C3_NU, "yield_mpa": C3_YIELD},
                   {"E": "1%", "nu": 0.02, "yield": "2%"})


def _faces_of(ev: Evidence, queries: list) -> tuple[list[dict], list[str]]:
    faces: list[dict] = []
    bad: list[str] = []
    for q in queries:
        hit = gc.resolve_faces(ev, q)
        if not hit:
            bad.append(f"{q!r} resolves to no face")
        faces.extend(hit)
    return faces, bad


@_gated("C3.fixture")
def c3_fixture(ev: Evidence, cid: str) -> CheckResult:
    st = _c3_study(ev)
    if st is None:
        return _no_study(cid, ev)
    fixtures = st.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        return _result(cid, False, fixtures, "the x=0 end face", None, "no fixtures stored")
    faces, bad = _faces_of(ev, fixtures)
    wrong = [f.get("sid") for f in faces
             if not gc.planar_end_face(f, (-1, 0, 0), 0, 0.0, C3_GEOM_TOL)]
    area = sum(float(f.get("area_or_len") or 0.0) for f in faces)
    ok = bool(faces) and not bad and not wrong and abs(area - C3_B * C3_H) <= 0.5
    return _result(cid, ok, {"queries": fixtures, "faces": len(faces), "area_mm2": round(area, 3),
                             "off_target": wrong},
                   f"exactly the x=0 end face ({C3_B * C3_H:g} mm²) fixed", None, "; ".join(bad))


@_gated("C3.load")
def c3_load(ev: Evidence, cid: str) -> CheckResult:
    st = _c3_study(ev)
    if st is None:
        return _no_study(cid, ev)
    loads = st.get("loads")
    if not isinstance(loads, list) or not loads:
        return _result(cid, False, loads, "500 N -Z on the x=120 face", None, "no loads stored")
    total = [0.0, 0.0, 0.0]
    faults: list[str] = []
    for i, ld in enumerate(loads):
        if not isinstance(ld, dict):
            faults.append(f"loads[{i}] is not an object")
            continue
        vec = _vec(ld.get("force"))
        if vec is None:
            faults.append(f"loads[{i}] is not a force vector (pressure/other: {sorted(ld)})")
            continue
        total = [t + v for t, v in zip(total, vec)]
        faces, bad = _faces_of(ev, [ld.get("face")])
        faults += [f"loads[{i}]: {b}" for b in bad]
        faults += [f"loads[{i}] face {f.get('sid')} is not the x={C3_L:g} end face" for f in faces
                   if not gc.planar_end_face(f, (1, 0, 0), 0, C3_L, C3_GEOM_TOL)]
    err = math.dist(total, (0.0, 0.0, -C3_F))
    ok = not faults and err <= 0.01 * C3_F
    return _result(cid, ok, {"total_force_n": [round(t, 4) for t in total], "loads": loads},
                   [0.0, 0.0, -C3_F], "1% of |F|", "; ".join(faults))


@_gated("C3.tip_deflection")
def c3_tip_deflection(ev: Evidence, cid: str) -> CheckResult:
    st = _c3_study(ev)
    if st is None:
        return _no_study(cid, ev)
    d = _num((st.get("results") or {}).get("max_disp_mm"))
    ok = d is not None and _rel(d, C3_DEFLECTION) <= 0.05
    return _result(cid, ok, d, round(C3_DEFLECTION, 6), "5%")


@_gated("C3.reactions")
def c3_reactions(ev: Evidence, cid: str) -> CheckResult:
    st = _c3_study(ev)
    if st is None:
        return _no_study(cid, ev)
    rx = st.get("reactions") if isinstance(st.get("reactions"), dict) else {}
    rf, rm = _vec(rx.get("reaction_force_n")), _vec(rx.get("reaction_moment_nmm"))
    eq = _num(rx.get("equilibrium_error_pct"))
    faults: list[str] = []
    if rf is None or math.dist(rf, (0.0, 0.0, C3_F)) > 0.02 * C3_F:
        faults.append(f"reaction force {rf} != (0,0,+{C3_F:g}) within 2%")
    # About the world origin = the root section (x=0): |M_y| = F*L for a tip load at x=L.
    if rm is None or _rel(abs(rm[1]), C3_ROOT_MOMENT) > 0.02:
        faults.append(f"root moment M_y {rm[1] if rm else None} != ±{C3_ROOT_MOMENT:g} N·mm "
                      "within 2% (load not at x=120 on this geometry)")
    if eq is None or eq > 2.0:
        faults.append(f"equilibrium error {eq}% > 2%")
    return _result(cid, not faults, {"reaction_force_n": rf, "reaction_moment_nmm": rm,
                                     "equilibrium_error_pct": eq},
                   {"reaction_force_n": [0.0, 0.0, C3_F], "abs_My_nmm": C3_ROOT_MOMENT},
                   "2%", "; ".join(faults))


@_gated("C3.root_stress")
def c3_root_stress(ev: Evidence, cid: str) -> CheckResult:
    st = _c3_study(ev)
    if st is None:
        return _no_study(cid, ev)
    res = st.get("results") or {}
    s = _num(res.get("max_von_mises_mpa"))
    red = res.get("max_von_mises_reduction")
    excluded = _num(res.get("summary_nodes_excluded"))
    faults = []
    if red != C3_NONSINGULAR or not excluded:
        faults.append(f"stress reduction {red!r} (excluded nodes {excluded}) is not the "
                      f"non-singular {C3_NONSINGULAR!r} measure")
    if s is None or _rel(s, C3_STRESS) > 0.10:
        faults.append(f"von Mises {s} MPa outside 10% of {C3_STRESS:g}")
    return _result(cid, not faults, {"max_von_mises_mpa": s, "reduction": red},
                   round(C3_STRESS, 4), "10%", "; ".join(faults))


@_gated("C3.mesh_evidence")
def c3_mesh_evidence(ev: Evidence, cid: str) -> CheckResult:
    st = _c3_study(ev)
    if st is None:
        return _no_study(cid, ev)
    mq = st.get("mesh_quality") if isinstance(st.get("mesh_quality"), dict) else {}
    prov = st.get("provenance") if isinstance(st.get("provenance"), dict) else {}
    plaus = st.get("plausibility") if isinstance(st.get("plausibility"), dict) else {}
    faults = []
    if mq.get("order") != 2:
        faults.append(f"element order {mq.get('order')!r} (quadratic tet10 required)")
    if mq.get("unfixed_components") != 0 or mq.get("connected_components") != 1:
        faults.append(f"mesh components {mq.get('connected_components')!r}, unfixed "
                      f"{mq.get('unfixed_components')!r}")
    jac, poor = _num(mq.get("jacobian_min")), _num(mq.get("pct_poor"))
    if jac is None or jac <= 0.0:
        faults.append(f"jacobian_min {jac}")
    if poor is None or poor > 5.0:
        faults.append(f"pct_poor {poor} > 5")
    if not prov.get("solver") or not prov.get("engine_build_id"):
        faults.append("solver provenance missing")
    if plaus.get("flag") != "ok":
        faults.append(f"plausibility flag {plaus.get('flag')!r}")
    return _result(cid, not faults, {"order": mq.get("order"), "n_elems": st.get("n_elems"),
                                     "jacobian_min": jac, "pct_poor": poor,
                                     "solver": prov.get("solver"),
                                     "plausibility": plaus.get("flag")},
                   "tet10, one fixed component, valid elements, solver provenance", None,
                   "; ".join(faults))


C3 = Task(
    id="C3", version=1, domain="sim",
    prompt=(
        "Create a straight rectangular steel cantilever bar as an editable native part: "
        "120 mm long along +X (from x = 0 to x = 120 mm), 20 mm wide along Y and 8 mm "
        "thick along Z. Run a linear static stress study on it: fully fix the end face at "
        "x = 0 and apply a 500 N force in the -Z direction on the free end face at "
        "x = 120 mm. Material: structural steel with E = 200 GPa, Poisson's ratio 0.3 and "
        "yield strength 250 MPa. The solved study and its results must be saved inside "
        "the part: save it in Chudware's native format as `deliverable.step+` in the "
        "working directory."
    ),
    checks=(c3_solid, c3_bar_geometry, c3_study_saved, c3_material, c3_fixture, c3_load,
            c3_tip_deflection, c3_reactions, c3_root_stress, c3_mesh_evidence),
    budget_s=1500,
    notes=(
        "Oracle (Euler-Bernoulli): I = b h^3/12 = 20*8^3/12 = 853.333 mm^4; tip deflection "
        "F L^3/(3 E I) = 500*120^3/(3*200000*853.333) = 1.6875 mm (5 %); root bending "
        "stress M c/I = 500*120*4/853.333 = 281.25 MPa (10 %); reactions (0,0,+500) N and "
        "|M_y| = 60 000 N mm about the root (2 %, which also ties the load position to "
        "this geometry). Stress is graded on the study's persisted non-singular reduction "
        "(node-averaged von Mises excluding two element rings around the ideal clamp, "
        "whose raw peak is a discretization singularity); with the excluded ring a few mm "
        "from the root, beam theory there is within ~5-7 % of 281.25 MPa. Shear "
        "deflection (~0.35 %) and clamp Poisson stiffening are inside the 5 % band. "
        "Fixture/load queries stored in the study are re-resolved on the reopened part: "
        "they must name only the x=0 face (160 mm^2) and only the x=120 face. "
        "Reference run (tet10, 9 216 elements, default mesh): 1.6328 mm, 264.0 MPa, "
        "reactions exact."
    ),
)


TASKS: tuple[Task, ...] = (C1, C2, C3)
