"""C20 — mate solver unit tests (engine.doc.mate_solver over engine.doc.nsolve).

Run:  & $py -m engine.doc.test_mate_solver   (from repo root)

House style: plain main() with asserts (engine/test_kernel_aliases.py).

Scenarios:
  1. peg-in-hole: concentric + coincident leaves exactly 1 rotational DOF; the peg snaps onto the
     hole axis with its base plane on the base's top plane.
  2. over-definition: add a redundant concentric on the same pair -> reported redundant (consistent,
     not conflicting); a contradictory distance -> conflicting.
  3. align flip: anti vs aligned coincident flips the component to the opposite side.
  4. drag along the free DOF: soft-pull the peg's translation; the concentric+coincident mates stay
     exact (peg stays on the axis, base plane coincident) while the peg slides toward the cursor
     ALONG the single free translational direction (none here — fully positioned), and a 1-DOF
     translational case slides exactly.
"""

from __future__ import annotations

import math

import numpy as np

from engine.doc.mate_solver import MateGeom, solve_mates, _rodrigues, _rot_jac, _mat_to_rotvec


def _ident16():
    return [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


def _xform(t, w=(0, 0, 0)):
    R = _rodrigues(np.asarray(w, dtype=float))
    M = np.eye(4)
    M[:3, :3] = R
    M[:3, 3] = np.asarray(t, dtype=float)
    return [float(v) for v in M.reshape(16)]


def test_rotation_jacobian_matches_fd():
    """Analytic _rot_jac must match a central finite difference (sanity on the exp-map deriv)."""
    rng = np.random.default_rng(0)
    for _ in range(20):
        w = rng.normal(size=3) * 0.7
        v = rng.normal(size=3)
        J = _rot_jac(w, v)
        eps = 1e-6
        Jfd = np.zeros((3, 3))
        for i in range(3):
            dw = np.zeros(3); dw[i] = eps
            f1 = _rodrigues(w + dw) @ v
            f0 = _rodrigues(w - dw) @ v
            Jfd[:, i] = (f1 - f0) / (2 * eps)
        err = np.max(np.abs(J - Jfd))
        assert err < 1e-5, f"rot jac mismatch {err}"
    # log/exp round trip
    for _ in range(20):
        w = rng.normal(size=3) * 0.7
        w2 = _mat_to_rotvec(_rodrigues(w))
        assert np.max(np.abs(w - w2)) < 1e-9, (w, w2)
    print("[mate] rotation jacobian matches FD; log/exp round-trips")


def _peg_in_hole(align_peg="aligned"):
    """Base: fixed, top face at z=0 normal +Z, hole axis along z at origin. Peg: floating, base
    face at its local z=0 normal +Z (or -Z), cylinder axis local z. Mate concentric(axes) +
    coincident(base-top-plane to peg-base-plane)."""
    components = [
        {"id": "k1", "name": "base", "transform": _ident16(), "fixed": True, "suppressed": False},
        {"id": "k2", "name": "peg",
         "transform": _xform((30.0, -10.0, 25.0), (0.3, -0.2, 0.5)),  # arbitrary start
         "fixed": False, "suppressed": False},
    ]
    mates = [
        {"id": "m1", "type": "concentric", "align": "aligned", "suppressed": False,
         "refs": [{"component": "k1"}, {"component": "k2"}], "value": None},
        {"id": "m2", "type": "coincident", "align": align_peg, "suppressed": False,
         "refs": [{"component": "k1"}, {"component": "k2"}], "value": None},
    ]
    geom = {
        # base hole axis: through origin, +z; cylinder r=4
        ("m1", 0): MateGeom("axis", point=[0, 0, 0], direction=[0, 0, 1], radius=4.0),
        # peg axis (local): through local origin, +z; r=3.9
        ("m1", 1): MateGeom("axis", point=[0, 0, 0], direction=[0, 0, 1], radius=3.9),
        # base top plane z=0 normal +z
        ("m2", 0): MateGeom("plane", point=[0, 0, 0], normal=[0, 0, 1]),
        # peg base plane local z=0 normal -z (so it seats face-down on the base top)
        ("m2", 1): MateGeom("plane", point=[0, 0, 0], normal=[0, 0, -1]),
    }
    return components, mates, geom


def test_peg_in_hole_one_dof():
    comps, mates, geom = _peg_in_hole()
    res = solve_mates(comps, mates, geom)
    assert res.converged, (res.max_residual, res.summary)
    # exactly 1 DOF left (rotation about the shared axis).
    assert res.dof == 1, f"expected 1 DOF, got {res.dof} ({res.summary})"
    # peg transform: axis collinear with z, base plane at z=0.
    M = np.asarray(res.transforms["k2"], dtype=float).reshape(4, 4)
    R, t = M[:3, :3], M[:3, 3]
    peg_axis_world = R @ np.array([0, 0, 1.0])
    # axis aligned with +/- z
    assert abs(abs(peg_axis_world[2]) - 1.0) < 1e-6, peg_axis_world
    # peg local origin (on axis) lands on the z axis (x=y=0)
    assert abs(t[0]) < 1e-5 and abs(t[1]) < 1e-5, t
    # base plane of peg (local z=0, point local origin) coincident with base top z=0
    assert abs(t[2]) < 1e-5, f"peg base should seat at z=0, got z={t[2]}"
    # all mates ok
    sts = {m.mate_id: m.status for m in res.mate_status}
    assert sts == {"m1": "ok", "m2": "ok"}, sts
    print(f"[mate] peg-in-hole: dof={res.dof} maxres={res.max_residual:.2e} "
          f"peg_z={t[2]:.4f} status={sts}")


def test_align_flip():
    """Aligned vs anti coincident flips the peg's seated orientation.

    Peg local plane normal is -z. Aligned: faces seat (world normals oppose) so the peg's world
    plane-normal = -(base +z) = -z, i.e. peg local +z -> world +z. Anti: peg world plane-normal =
    +z, i.e. peg local +z -> world -z. So the peg's world axis (local +z) flips sign."""
    comps, mates, geom = _peg_in_hole(align_peg="aligned")
    res_a = solve_mates(comps, mates, geom)
    Ma = np.asarray(res_a.transforms["k2"], dtype=float).reshape(4, 4)
    peg_axis_a = (Ma[:3, :3] @ np.array([0, 0, 1.0]))[2]

    comps2, mates2, geom2 = _peg_in_hole(align_peg="anti")
    res_b = solve_mates(comps2, mates2, geom2)
    Mb = np.asarray(res_b.transforms["k2"], dtype=float).reshape(4, 4)
    peg_axis_b = (Mb[:3, :3] @ np.array([0, 0, 1.0]))[2]

    assert res_a.converged and res_b.converged, (res_a.summary, res_b.summary)
    # The peg's world axis z-component flips sign between aligned and anti.
    assert peg_axis_a * peg_axis_b < 0, (peg_axis_a, peg_axis_b)
    print(f"[mate] align flip: aligned peg_axis_z={peg_axis_a:+.3f} "
          f"anti peg_axis_z={peg_axis_b:+.3f}")


def test_over_definition_redundant_and_conflicting():
    comps, mates, geom = _peg_in_hole()
    # redundant: a second identical concentric.
    mates_r = mates + [{"id": "m3", "type": "concentric", "align": "aligned",
                        "suppressed": False,
                        "refs": [{"component": "k1"}, {"component": "k2"}], "value": None}]
    geom_r = dict(geom)
    geom_r[("m3", 0)] = MateGeom("axis", point=[0, 0, 0], direction=[0, 0, 1], radius=4.0)
    geom_r[("m3", 1)] = MateGeom("axis", point=[0, 0, 0], direction=[0, 0, 1], radius=3.9)
    res_r = solve_mates(comps, mates_r, geom_r)
    sts_r = {m.mate_id: m.status for m in res_r.mate_status}
    assert "redundant" in sts_r.values(), sts_r
    assert not res_r.over_defined, sts_r
    print(f"[mate] over-define (redundant concentric): {sts_r} over={res_r.over_defined}")

    # conflicting: two contradictory plane distances on the same pair (z = 5 AND z = 12).
    mates_c = [
        {"id": "m1", "type": "concentric", "align": "aligned", "suppressed": False,
         "refs": [{"component": "k1"}, {"component": "k2"}], "value": None},
        {"id": "m2", "type": "distance", "align": "aligned", "suppressed": False,
         "refs": [{"component": "k1"}, {"component": "k2"}], "value": {"value": 5.0, "expr": None}},
        {"id": "m3", "type": "distance", "align": "aligned", "suppressed": False,
         "refs": [{"component": "k1"}, {"component": "k2"}], "value": {"value": 12.0, "expr": None}},
    ]
    geom_c = {
        ("m1", 0): MateGeom("axis", point=[0, 0, 0], direction=[0, 0, 1], radius=4.0),
        ("m1", 1): MateGeom("axis", point=[0, 0, 0], direction=[0, 0, 1], radius=3.9),
        ("m2", 0): MateGeom("plane", point=[0, 0, 0], normal=[0, 0, 1]),
        ("m2", 1): MateGeom("plane", point=[0, 0, 0], normal=[0, 0, -1]),
        ("m3", 0): MateGeom("plane", point=[0, 0, 0], normal=[0, 0, 1]),
        ("m3", 1): MateGeom("plane", point=[0, 0, 0], normal=[0, 0, -1]),
    }
    res_c = solve_mates(comps, mates_c, geom_c)
    sts_c = {m.mate_id: m.status for m in res_c.mate_status}
    assert res_c.over_defined, sts_c
    assert "conflicting" in sts_c.values(), sts_c
    print(f"[mate] over-define (conflicting distances 5 vs 12): {sts_c} over={res_c.over_defined}")


def test_drag_along_free_dof_keeps_mates_exact():
    """A single coincident plane-plane mate leaves 1 translational DOF along the plane normal-free
    axes; drag the component and verify the coincident plane stays exact while it slides."""
    comps = [
        {"id": "k1", "name": "base", "transform": _ident16(), "fixed": True, "suppressed": False},
        {"id": "k2", "name": "slider",
         "transform": _xform((5.0, 5.0, 20.0)), "fixed": False, "suppressed": False},
    ]
    mates = [
        {"id": "m1", "type": "coincident", "align": "anti", "suppressed": False,
         "refs": [{"component": "k1"}, {"component": "k2"}], "value": None},
    ]
    geom = {
        ("m1", 0): MateGeom("plane", point=[0, 0, 0], normal=[0, 0, 1]),
        ("m1", 1): MateGeom("plane", point=[0, 0, 0], normal=[0, 0, -1]),
    }
    # solve baseline: slider seats on z=0 plane, free to slide in x,y + rotate about z (3 DOF).
    res0 = solve_mates(comps, mates, geom)
    assert res0.converged, res0.summary
    M0 = np.asarray(res0.transforms["k2"], dtype=float).reshape(4, 4)
    assert abs(M0[2, 3]) < 1e-6, f"slider should seat at z=0, got {M0[2,3]}"

    # drag toward (40, -15, 99): z target is impossible (plane holds it), x/y free.
    warm = res0.params
    res1 = solve_mates(comps, mates, geom, warm=warm,
                       drag={"component": "k2", "target": [40.0, -15.0, 99.0]})
    M1 = np.asarray(res1.transforms["k2"], dtype=float).reshape(4, 4)
    t1 = M1[:3, 3]
    # coincident plane STILL exact: z stays 0.
    assert abs(t1[2]) < 1e-5, f"drag broke coincident: z={t1[2]}"
    # slid toward the cursor in x/y (moved off the start 5,5 toward 40,-15).
    assert t1[0] > 6.0 and t1[1] < 4.0, f"drag did not follow cursor: {t1}"
    print(f"[mate] drag along free DOF: seated z={t1[2]:.2e} slid to "
          f"x={t1[0]:.2f} y={t1[1]:.2f} (target 40,-15)")


def main() -> None:
    test_rotation_jacobian_matches_fd()
    test_peg_in_hole_one_dof()
    test_align_flip()
    test_over_definition_redundant_and_conflicting()
    test_drag_along_free_dof_keeps_mates_exact()
    print("\nMATE SOLVER TESTS PASSED")


if __name__ == "__main__":
    main()
