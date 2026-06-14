r"""Revolved-surface feature — surf_revolve compiles to an OPEN surface of revolution.

Run:  & $py -m pytest engine\doc\test_surf_revolve.py -q   (from repo root)

Covers the surface-of-revolution compiler (surfacing batch):
1. A rectangle profile in plane.front (the XZ plane) revolved 360° about axis.z compiles to a
   single SURFACE body (kind=="surface") — a tube WALL with NO end caps.
2. mass_props of that surface returns an AREA report: is_surface True, area_mm2 ≈ the analytic
   area of the revolved wire (two cylinders + two annular discs), volume_mm3 ≈ 0, mass_g ≈ 0.

GEOMETRY: plane.front == Plane.XZ (x_dir=+X, y_dir=+Z, origin at 0,0,0), so a sketch point (u,v)
maps to world (u, 0, v). The rectangle u∈[R0,R0+w], v∈[Z0,Z0+h] therefore lies in the XZ plane at
radii [R0,R0+w] from the Z axis and heights [Z0,Z0+h]. Revolving its OUTER WIRE a full turn about
axis.z sweeps the four surfaces the four edges trace:
  - two CYLINDERS at r=R0 and r=R0+w, each lateral area 2π·r·h;
  - two ANNULAR DISCS at z=Z0 and z=Z0+h (the radial top/bottom edges), each π·((R0+w)²−R0²).
With R0=10, w=5, h=20:  2π·10·20 + 2π·15·20 + 2·π·(15²−10²) = (400+600+250)·π = 1250π ≈ 3926.99 mm².

Harness mirrors engine/doc/test_extrude_ends.py + engine/doc/test_surface_bodykind.py.
"""

from __future__ import annotations

import math

from engine.doc.compiler import compile_document
from engine.doc.evaluate import mass_props
from engine.doc.schema import validate_part

from engine.doc.features import surf_revolve  # noqa: F401  (registers the compiler)


# --------------------------------------------------------------------------- helpers

def _dim(value: float, expr: str | None = None) -> dict:
    return {"value": value, "expr": expr}


def _feat(fid: str, ftype: str, params: dict, refs: dict | None = None,
          name: str | None = None) -> dict:
    return {"id": fid, "type": ftype, "name": name or fid,
            "suppressed": False, "params": params, "refs": refs or {}}


def _doc(features: list[dict]) -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "test-doc",
        "name": "test",
        "units": {"system": "MMGS"},
        "material": None,
        "equations": {"globals": []},
        "features": features,
    }


def _assert_valid(doc: dict) -> None:
    errs = validate_part(doc)
    assert errs == [], "doc must validate before compile:\n" + "\n".join(errs)


def _status(res, fid: str) -> dict:
    for s in res.feature_status:
        if s["id"] == fid:
            return s
    raise AssertionError(f"no status for {fid}")


def _rect_sketch(fid: str, x0: float, y0: float, w: float, h: float, plane_ref) -> dict:
    return _feat(fid, "sketch", {
        "entities": [
            {"id": "e1", "type": "line", "construction": False,
             "p1": [x0, y0], "p2": [x0 + w, y0]},
            {"id": "e2", "type": "line", "construction": False,
             "p1": [x0 + w, y0], "p2": [x0 + w, y0 + h]},
            {"id": "e3", "type": "line", "construction": False,
             "p1": [x0 + w, y0 + h], "p2": [x0, y0 + h]},
            {"id": "e4", "type": "line", "construction": False,
             "p1": [x0, y0 + h], "p2": [x0, y0]},
        ],
        "constraints": [],
    }, refs={"plane": plane_ref})


def _region_sel(sketch_id: str, cx: float, cy: float, area: float) -> dict:
    return {"kind": "region", "sketch": sketch_id, "centroid": [cx, cy],
            "area_or_len": area, "hint_index": 0}


# --------------------------------------------------------------------------- test 1: cylinder wall

def test_surf_revolve_full_turn_is_surface() -> None:
    """Rectangle r∈[10,15], z∈[0,20] in plane.front revolved 360° about axis.z → an open
    surface body (kind=="surface") whose area is the analytic revolved-wire area (no caps)."""
    R0, W, Z0, H = 10.0, 5.0, 0.0, 20.0
    doc = _doc([
        _rect_sketch("f1", R0, Z0, W, H, "plane.front"),
        _feat("f2", "surf_revolve", {"angle": _dim(360.0)},
              refs={"profile": [_region_sel("f1", R0 + W / 2, Z0 + H / 2, W * H)],
                    "axis": "axis.z"}),
    ])
    _assert_valid(doc)
    res = compile_document(doc)

    s = _status(res, "f2")
    assert s["ok"], f"surf_revolve failed: {s['error']}"
    assert len(res.bodies) == 1, f"expected 1 surface body; got {len(res.bodies)}"
    bo = res.bodies[0]
    assert bo.kind == "surface", f"BodyOut.kind should be 'surface'; got {bo.kind!r}"

    # mass_props returns an AREA report — no enclosed volume / mass.
    mp = mass_props(bo.shape)
    assert mp.get("is_surface") is True, f"mass_props should flag is_surface; got {mp}"
    assert mp["area_mm2"] > 0.0, f"surface area should be > 0; got {mp['area_mm2']}"
    assert abs(mp["volume_mm3"]) < 1e-6, f"surface volume should be ~0; got {mp['volume_mm3']}"
    assert abs(mp["mass_g"]) < 1e-6, f"surface mass should be ~0; got {mp['mass_g']}"

    # Analytic area of the revolved OUTER WIRE (no caps):
    #   two cylinders (r=R0, r=R0+W, height H) + two annular discs (z=Z0, z=Z0+H).
    r_in, r_out = R0, R0 + W
    cyl = 2.0 * math.pi * r_in * H + 2.0 * math.pi * r_out * H
    discs = 2.0 * math.pi * (r_out * r_out - r_in * r_in)
    expected = cyl + discs  # = 1250π ≈ 3926.99
    assert abs(mp["area_mm2"] - expected) / expected < 0.01, \
        f"surf_revolve area {mp['area_mm2']:.2f} vs analytic revolved-wire area {expected:.2f}"

    # Sanity: a SOLID revolve of the same rectangle (annulus cross-section) would enclose
    # volume π(r_out²−r_in²)·H ≈ 7854 mm³ — the surface body has none of it.
    print(f"[surf_revolve] kind={bo.kind} area={mp['area_mm2']:.2f} "
          f"(expected {expected:.2f}, vol={mp['volume_mm3']:.3g})")


# --------------------------------------------------------------------------- main

def main() -> None:
    test_surf_revolve_full_turn_is_surface()
    print("\nSURF-REVOLVE TESTS PASSED")


if __name__ == "__main__":
    main()
