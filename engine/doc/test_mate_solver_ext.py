"""SW-parity ADVANCED + MECHANICAL mate tests (engine.doc.mate_solver).

Companion to test_mate_solver.py (the seven standard mates). Each test builds a small 2- (or 3-)
component assembly, runs solve_mates, asserts CONVERGENCE and that the specific constraint the new
mate encodes actually holds at the solution.

Advanced : width, symmetric, distance_limit, angle_limit, profile_center
Mechanical: gear, rack_pinion, screw, hinge, slot, cam_follower

House style mirrors test_mate_solver.py: plain functions + asserts, a _xform helper, MateGeom
inputs in each component's LOCAL frame, and a main() that runs them all.

Coupling note: gear/rack/screw measure the on-axis angle theta = (w - w0)·axis and axial travel
s = (t - t0)·d. The driver component is pinned to a known absolute pose (via concentric + a driving
mate or a fixed start), so the coupled component's resulting angle/translation is deterministic and
checked against the closed-form ratio/pitch.
"""

from __future__ import annotations

import math

import numpy as np

from engine.doc.mate_solver import MateGeom, solve_mates, _rodrigues


def _ident16():
    return [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


def _xform(t, w=(0, 0, 0)):
    R = _rodrigues(np.asarray(w, dtype=float))
    M = np.eye(4)
    M[:3, :3] = R
    M[:3, 3] = np.asarray(t, dtype=float)
    return [float(v) for v in M.reshape(16)]


def _R(transforms, cid):
    return np.asarray(transforms[cid], dtype=float).reshape(4, 4)[:3, :3]


def _t(transforms, cid):
    return np.asarray(transforms[cid], dtype=float).reshape(4, 4)[:3, 3]


# ===================================================================== ADVANCED MATES


def test_width_centers_tab():
    """A tab on a floating block is centered between two jaw planes on the fixed base.

    Base (fixed): two parallel jaw planes at x = 0 and x = 20 (normals +x). Block (floating): a tab
    reference point at its local origin. The width mate must drive the tab to x = 10 (mid-plane),
    regardless of the block's arbitrary start x.
    """
    comps = [
        {"id": "k1", "name": "base", "transform": _ident16(), "fixed": True, "suppressed": False},
        {"id": "k2", "name": "block",
         "transform": _xform((37.0, 4.0, 1.0)), "fixed": False, "suppressed": False},
    ]
    mates = [
        {"id": "m1", "type": "width", "align": "aligned", "suppressed": False,
         "refs": [{"component": "k2"}, {"component": "k1"}, {"component": "k1"}], "value": None},
    ]
    geom = {
        ("m1", 0): MateGeom("point", point=[0, 0, 0]),               # tab ref on block (local origin)
        ("m1", 1): MateGeom("plane", point=[0, 0, 0], normal=[1, 0, 0]),    # jaw 1 (x=0)
        ("m1", 2): MateGeom("plane", point=[20, 0, 0], normal=[1, 0, 0]),   # jaw 2 (x=20)
    }
    res = solve_mates(comps, mates, geom)
    assert res.converged, (res.max_residual, res.summary)
    tb = _t(res.transforms, "k2")
    assert abs(tb[0] - 10.0) < 1e-5, f"tab not centered: x={tb[0]} (want 10)"
    print(f"[ext] width: tab centered at x={tb[0]:.5f} (want 10) maxres={res.max_residual:.2e}")


def test_symmetric_mirrors_about_plane():
    """Two points symmetric about the YZ plane (x=0) on a fixed base.

    Point A (on fixed base) sits at world x = +8. Point B (on a floating block, local origin) must
    land at x = -8 so the YZ plane bisects them.
    """
    comps = [
        {"id": "k1", "name": "base", "transform": _ident16(), "fixed": True, "suppressed": False},
        {"id": "k2", "name": "block",
         "transform": _xform((25.0, 3.0, -2.0)), "fixed": False, "suppressed": False},
    ]
    mates = [
        {"id": "m1", "type": "symmetric", "align": "aligned", "suppressed": False,
         "refs": [{"component": "k1"}, {"component": "k2"}, {"component": "k1"}], "value": None},
    ]
    geom = {
        ("m1", 0): MateGeom("point", point=[8, 0, 0]),                  # point A (fixed base, x=+8)
        ("m1", 1): MateGeom("point", point=[0, 0, 0]),                  # point B (block local origin)
        ("m1", 2): MateGeom("plane", point=[0, 0, 0], normal=[1, 0, 0]),  # symmetry plane = YZ (x=0)
    }
    res = solve_mates(comps, mates, geom)
    assert res.converged, (res.max_residual, res.summary)
    tb = _t(res.transforms, "k2")
    assert abs(tb[0] - (-8.0)) < 1e-5, f"point B not mirrored: x={tb[0]} (want -8)"
    print(f"[ext] symmetric: B mirrored to x={tb[0]:.5f} (want -8) maxres={res.max_residual:.2e}")


def _distance_limit_case(start_z):
    comps = [
        {"id": "k1", "name": "base", "transform": _ident16(), "fixed": True, "suppressed": False},
        {"id": "k2", "name": "slider",
         "transform": _xform((0.0, 0.0, start_z)), "fixed": False, "suppressed": False},
    ]
    mates = [
        {"id": "m1", "type": "distance_limit", "align": "aligned", "suppressed": False,
         "refs": [{"component": "k1"}, {"component": "k2"}],
         "value": {"min": 5.0, "max": 12.0}},
    ]
    geom = {
        ("m1", 0): MateGeom("plane", point=[0, 0, 0], normal=[0, 0, 1]),
        ("m1", 1): MateGeom("point", point=[0, 0, 0]),
    }
    return comps, mates, geom


def test_distance_limit_clamps_and_frees():
    """distance_limit holds the gap within [min,max]=[5,12]; inside the range it removes no DOF.

    Three starts: above max (30 -> clamps to 12), below min (1 -> clamps to 5), inside (8 -> the
    mate is inactive, the slider is NOT moved and 3 DOF remain).
    """
    # above max -> clamp down to 12.
    res_hi = solve_mates(*_distance_limit_case(30.0))
    assert res_hi.converged, res_hi.summary
    z_hi = _t(res_hi.transforms, "k2")[2]
    assert abs(z_hi - 12.0) < 1e-5, f"above-max not clamped to 12: z={z_hi}"

    # below min -> clamp up to 5.
    res_lo = solve_mates(*_distance_limit_case(1.0))
    assert res_lo.converged, res_lo.summary
    z_lo = _t(res_lo.transforms, "k2")[2]
    assert abs(z_lo - 5.0) < 1e-5, f"below-min not clamped to 5: z={z_lo}"

    # inside the range -> inactive: slider stays put, no DOF removed (still 6 free).
    res_in = solve_mates(*_distance_limit_case(8.0))
    assert res_in.converged, res_in.summary
    z_in = _t(res_in.transforms, "k2")[2]
    assert abs(z_in - 8.0) < 1e-9, f"inside-range slider moved: z={z_in}"
    assert res_in.dof == 6, f"inside range should remove no DOF, dof={res_in.dof}"
    print(f"[ext] distance_limit: clamp-hi z={z_hi:.4f} clamp-lo z={z_lo:.4f} "
          f"free z={z_in:.4f} (dof={res_in.dof})")


def test_angle_limit_clamps():
    """angle_limit holds the angle between two directions within [min,max] degrees.

    Base dir +x (fixed). Block dir local +x starting rotated 80deg about z. Range [0,45] -> clamps
    to 45deg.
    """
    comps = [
        {"id": "k1", "name": "base", "transform": _ident16(), "fixed": True, "suppressed": False},
        {"id": "k2", "name": "arm",
         "transform": _xform((0.0, 0.0, 0.0), (0.0, 0.0, math.radians(80.0))),
         "fixed": False, "suppressed": False},
    ]
    mates = [
        {"id": "m1", "type": "angle_limit", "align": "aligned", "suppressed": False,
         "refs": [{"component": "k1"}, {"component": "k2"}],
         "value": {"min": 0.0, "max": 45.0}},
    ]
    geom = {
        ("m1", 0): MateGeom("axis", point=[0, 0, 0], direction=[1, 0, 0]),
        ("m1", 1): MateGeom("axis", point=[0, 0, 0], direction=[1, 0, 0]),
    }
    res = solve_mates(comps, mates, geom)
    assert res.converged, (res.max_residual, res.summary)
    R = _R(res.transforms, "k2")
    d = R @ np.array([1.0, 0, 0])
    ang = math.degrees(math.acos(float(np.clip(d[0], -1, 1))))  # angle to +x
    assert abs(ang - 45.0) < 1e-3, f"angle not clamped to 45: {ang}"
    print(f"[ext] angle_limit: clamped to {ang:.4f} deg (want 45)")


def test_profile_center_coincides_centroids():
    """profile_center drives the floating profile's centroid onto the fixed profile's centroid."""
    comps = [
        {"id": "k1", "name": "base", "transform": _ident16(), "fixed": True, "suppressed": False},
        {"id": "k2", "name": "prof",
         "transform": _xform((22.0, -7.0, 13.0)), "fixed": False, "suppressed": False},
    ]
    mates = [
        {"id": "m1", "type": "profile_center", "align": "aligned", "suppressed": False,
         "refs": [{"component": "k1"}, {"component": "k2"}], "value": None},
    ]
    geom = {
        ("m1", 0): MateGeom("point", point=[4, 5, 6]),    # fixed profile centroid (world 4,5,6)
        ("m1", 1): MateGeom("point", point=[0, 0, 0]),    # floating profile centroid (local origin)
    }
    res = solve_mates(comps, mates, geom)
    assert res.converged, (res.max_residual, res.summary)
    tb = _t(res.transforms, "k2")
    assert np.max(np.abs(tb - np.array([4, 5, 6.0]))) < 1e-5, f"centroids not coincident: {tb}"
    print(f"[ext] profile_center: centroid at {tb} (want [4 5 6])")


# ===================================================================== MECHANICAL MATES


def _pinion_axis_geom(comp_axis_radius=1.0):
    return MateGeom("axis", point=[0, 0, 0], direction=[0, 0, 1], radius=comp_axis_radius)


def test_gear_ratio_couples_rotations():
    """gear: driver pinion A is turned phi about Z; gear-coupled wheel B must turn -ratio*phi.

    Driver A: FIXED at a known rotation phi=30deg about Z (its absolute shaft angle). Wheel B:
    floating, concentric to a parallel Z axis at x=10, gear(ratio=2) to A. Expect theta_B =
    -2*theta_A (meshing gears counter-rotate). Deterministic: A is fixed so its angle is exact.
    """
    phi = math.radians(30.0)
    ratio = 2.0
    comps = [
        {"id": "k0", "name": "frame", "transform": _ident16(), "fixed": True, "suppressed": False},
        {"id": "kA", "name": "pinion",
         "transform": _xform((0, 0, 0), (0.0, 0.0, phi)), "fixed": True, "suppressed": False},
        {"id": "kB", "name": "wheel",
         "transform": _xform((10, 0, 0), (0.0, 0.0, 0.0)), "fixed": False, "suppressed": False},
    ]
    mates = [
        # pin B's axis on a parallel Z axis at x=10 (its rotation bearing)
        {"id": "m1", "type": "concentric", "align": "aligned", "suppressed": False,
         "refs": [{"component": "k0"}, {"component": "kB"}], "value": None},
        # gear couple B to A
        {"id": "m2", "type": "gear", "align": "aligned", "suppressed": False,
         "refs": [{"component": "kA"}, {"component": "kB"}],
         "value": {"ratio": ratio}},
    ]
    geom = {
        # B's bearing axis = Z through x=10 (frame ref); B's own axis = local Z through origin
        ("m1", 0): MateGeom("axis", point=[10, 0, 0], direction=[0, 0, 1]),
        ("m1", 1): MateGeom("axis", point=[0, 0, 0], direction=[0, 0, 1]),
        ("m2", 0): MateGeom("axis", point=[0, 0, 0], direction=[0, 0, 1]),
        ("m2", 1): MateGeom("axis", point=[0, 0, 0], direction=[0, 0, 1]),
    }
    res = solve_mates(comps, mates, geom)
    assert res.converged, (res.max_residual, res.summary)
    # measure each rotor's signed angle about Z from its rotation vector (rotation IS about Z here).
    thA = float(_rotvec(res.transforms, "kA")[2])
    thB = float(_rotvec(res.transforms, "kB")[2])
    assert abs(thA - phi) < 1e-9, f"driver angle {thA} != {phi}"
    assert abs(thB + ratio * thA) < 1e-4, f"gear ratio violated: thB={thB} thA={thA} ratio={ratio}"
    print(f"[ext] gear: thA={math.degrees(thA):.3f} thB={math.degrees(thB):.3f} "
          f"(thB = -{ratio}*thA) maxres={res.max_residual:.2e}")


def test_rack_pinion_couples_rotation_to_translation():
    """rack_pinion: pinion A turned phi about Z; rack B slides s = pitch_radius * phi along +x.

    A: FIXED at phi=20deg about Z (known shaft angle). B (rack): floating, kept on its rail by a
    coincident plane (holds y), coupled by rack_pinion(pitch_radius=r). Expect B's x-travel == r*phi.
    """
    phi = math.radians(20.0)
    r = 5.0
    comps = [
        {"id": "k0", "name": "frame", "transform": _ident16(), "fixed": True, "suppressed": False},
        {"id": "kA", "name": "pinion",
         "transform": _xform((0, 0, 0), (0.0, 0.0, phi)), "fixed": True, "suppressed": False},
        {"id": "kB", "name": "rack",
         "transform": _xform((0, -10, 0)), "fixed": False, "suppressed": False},
    ]
    mates = [
        # keep the rack on its rail: coincident plane holds y (rack body slides in x)
        {"id": "m1", "type": "coincident", "align": "aligned", "suppressed": False,
         "refs": [{"component": "k0"}, {"component": "kB"}], "value": None},
        {"id": "m2", "type": "rack_pinion", "align": "aligned", "suppressed": False,
         "refs": [{"component": "kA"}, {"component": "kB"}],
         "value": {"pitch_radius": r}},
    ]
    geom = {
        # rail plane normal +y at y=-10 to pin the rack's transverse position
        ("m1", 0): MateGeom("plane", point=[0, -10, 0], normal=[0, 1, 0]),
        ("m1", 1): MateGeom("plane", point=[0, 0, 0], normal=[0, -1, 0]),
        # rack travel axis = world +x
        ("m2", 0): MateGeom("axis", point=[0, 0, 0], direction=[0, 0, 1]),  # pinion axis Z
        ("m2", 1): MateGeom("axis", point=[0, 0, 0], direction=[1, 0, 0]),  # rack travel dir +x
    }
    res = solve_mates(comps, mates, geom)
    assert res.converged, (res.max_residual, res.summary)
    tB = _t(res.transforms, "kB")
    thA = float(_rotvec(res.transforms, "kA")[2])
    expected = r * thA
    assert abs(thA - phi) < 1e-9, f"driver angle {thA} != {phi}"
    assert abs(tB[0] - expected) < 1e-4, f"rack travel {tB[0]} != r*phi {expected}"
    print(f"[ext] rack_pinion: thA={math.degrees(thA):.3f}deg rack_x={tB[0]:.4f} "
          f"(want r*phi={expected:.4f})")


def test_screw_pitch_couples_rotation_to_advance():
    """screw: bolt B turns 90deg in a fixed nut; it advances dz = pitch * theta / (2*pi) on its axis.

    Nut A: fixed (identity). Bolt B: concentric to Z (axis pinned) + a parallel mate that turns its
    +x marker onto world +y (an unambiguous 90deg turn about Z) + screw(pitch). The only remaining
    DOF is the axial z, which the screw drives to pitch*theta/(2pi).
    """
    phi = math.radians(90.0)
    pitch = 4.0  # mm/rev (lead)
    comps = [
        {"id": "k0", "name": "nut", "transform": _ident16(), "fixed": True, "suppressed": False},
        {"id": "kB", "name": "bolt",
         "transform": _xform((0, 0, 0), (0.0, 0.0, 0.0)), "fixed": False, "suppressed": False},
    ]
    mates = [
        {"id": "m1", "type": "concentric", "align": "aligned", "suppressed": False,
         "refs": [{"component": "k0"}, {"component": "kB"}], "value": None},
        # turn the bolt 90deg: its local +x marker becomes world +y (parallel, sign-unique)
        {"id": "m2", "type": "parallel", "align": "aligned", "suppressed": False,
         "refs": [{"component": "k0"}, {"component": "kB"}], "value": None},
        {"id": "m3", "type": "screw", "align": "aligned", "suppressed": False,
         "refs": [{"component": "k0"}, {"component": "kB"}],
         "value": {"pitch": pitch}},
    ]
    geom = {
        ("m1", 0): _pinion_axis_geom(), ("m1", 1): _pinion_axis_geom(),
        ("m2", 0): MateGeom("axis", point=[0, 0, 0], direction=[0, 1, 0]),  # frame ref +y
        ("m2", 1): MateGeom("axis", point=[0, 0, 0], direction=[1, 0, 0]),  # bolt marker +x -> +y
        ("m3", 0): MateGeom("axis", point=[0, 0, 0], direction=[0, 0, 1]),  # nut axis (A, fixed)
        ("m3", 1): MateGeom("axis", point=[0, 0, 0], direction=[0, 0, 1]),  # bolt axis (B)
    }
    res = solve_mates(comps, mates, geom)
    assert res.converged, (res.max_residual, res.summary)
    tB = _t(res.transforms, "kB")
    thB = float(_rotvec(res.transforms, "kB")[2])
    expected = pitch * thB / (2.0 * math.pi)
    assert abs(thB - phi) < 1e-4, f"bolt angle {thB} != {phi}"
    assert abs(tB[2] - expected) < 1e-4, f"screw advance {tB[2]} != pitch*th/2pi {expected}"
    print(f"[ext] screw: thB={math.degrees(thB):.3f}deg dz={tB[2]:.5f} "
          f"(want pitch*th/2pi={expected:.5f})")


def test_hinge_pins_axis_and_limits_swing():
    """hinge: door B pinned concentric to the frame's Z axis, swing clamped to [0,90] deg.

    Door starts open 140deg -> hinge clamps it to 90deg while keeping the axis collinear.
    """
    comps = [
        {"id": "k1", "name": "frame", "transform": _ident16(), "fixed": True, "suppressed": False},
        {"id": "k2", "name": "door",
         "transform": _xform((0, 0, 0), (0.0, 0.0, math.radians(140.0))),
         "fixed": False, "suppressed": False},
    ]
    mates = [
        {"id": "m1", "type": "hinge", "align": "aligned", "suppressed": False,
         "refs": [{"component": "k1"}, {"component": "k2"}],
         "value": {"min": 0.0, "max": 90.0}},
    ]
    geom = {
        ("m1", 0): MateGeom("axis", point=[0, 0, 0], direction=[0, 0, 1]),
        ("m1", 1): MateGeom("axis", point=[0, 0, 0], direction=[0, 0, 1]),
        # swing markers (perp to axis): frame +x, door local +x
        ("m1", 2): MateGeom("axis", point=[0, 0, 0], direction=[1, 0, 0]),
        ("m1", 3): MateGeom("axis", point=[0, 0, 0], direction=[1, 0, 0]),
    }
    res = solve_mates(comps, mates, geom)
    assert res.converged, (res.max_residual, res.summary)
    R = _R(res.transforms, "k2")
    axis = R @ np.array([0, 0, 1.0])
    assert abs(abs(axis[2]) - 1.0) < 1e-5, f"hinge axis not on Z: {axis}"
    d = R @ np.array([1.0, 0, 0])
    swing = math.degrees(math.acos(float(np.clip(d[0], -1, 1))))
    assert abs(swing - 90.0) < 1e-2, f"swing not clamped to 90: {swing}"
    print(f"[ext] hinge: axis_z={axis[2]:+.4f} swing={swing:.3f}deg (want 90)")


def test_slot_pin_on_centerline_slides_free():
    """slot: a pin on the floating block is held on the fixed slot centerline but slides along it.

    Slot centerline: the world x-axis (through origin, dir +x). Pin: block local origin, starting
    off the line at (6, 9, -4). The slot mate must drive the pin onto the line (y=z=0) while the
    along-x position stays free (we then drag along x to confirm).
    """
    comps = [
        {"id": "k1", "name": "plate", "transform": _ident16(), "fixed": True, "suppressed": False},
        {"id": "k2", "name": "pin",
         "transform": _xform((6.0, 9.0, -4.0)), "fixed": False, "suppressed": False},
    ]
    mates = [
        {"id": "m1", "type": "slot", "align": "aligned", "suppressed": False,
         "refs": [{"component": "k2"}, {"component": "k1"}], "value": None},
    ]
    geom = {
        ("m1", 0): MateGeom("point", point=[0, 0, 0]),                       # pin (block local origin)
        ("m1", 1): MateGeom("axis", point=[0, 0, 0], direction=[1, 0, 0]),   # slot centerline (world x)
    }
    res0 = solve_mates(comps, mates, geom)
    assert res0.converged, (res0.max_residual, res0.summary)
    p0 = _t(res0.transforms, "k2")
    # on the x-axis: y and z driven to 0.
    assert abs(p0[1]) < 1e-5 and abs(p0[2]) < 1e-5, f"pin not on slot line: {p0}"

    # drag along the slot to x=18: stays on the line, slides freely.
    res1 = solve_mates(comps, mates, geom, warm=res0.params,
                       drag={"component": "k2", "target": [18.0, 0.0, 0.0]})
    p1 = _t(res1.transforms, "k2")
    assert abs(p1[1]) < 1e-4 and abs(p1[2]) < 1e-4, f"drag left the slot line: {p1}"
    assert p1[0] > 8.0, f"pin did not slide along slot: x={p1[0]}"
    print(f"[ext] slot: seated y={p0[1]:.2e} z={p0[2]:.2e} ; slid to x={p1[0]:.3f}")


def test_cam_follower_tangent_to_cam():
    """cam_follower (best-effort): roller follower stays tangent to a circular cam lobe.

    Cam: fixed cylinder, axis = world Z through origin, radius R=10. Follower: a roller (sphere/
    point centre, radius rf=2) on a floating arm. Tangency => centre-to-axis distance == R+rf=12.
    Start the follower out at (20, 5, 0) -> it pulls in to a 12 mm centre distance. (The mate is a
    best-effort local-curvature tangency; from a moderate offset like this it converges robustly.)
    """
    R, rf = 10.0, 2.0
    comps = [
        {"id": "k1", "name": "cam", "transform": _ident16(), "fixed": True, "suppressed": False},
        {"id": "k2", "name": "follower",
         "transform": _xform((20.0, 5.0, 0.0)), "fixed": False, "suppressed": False},
    ]
    mates = [
        {"id": "m1", "type": "cam_follower", "align": "aligned", "suppressed": False,
         "refs": [{"component": "k2"}, {"component": "k1"}], "value": None},
    ]
    geom = {
        ("m1", 0): MateGeom("sphere", point=[0, 0, 0], radius=rf),                 # follower centre
        ("m1", 1): MateGeom("axis", point=[0, 0, 0], direction=[0, 0, 1], radius=R),  # cam
    }
    res = solve_mates(comps, mates, geom)
    assert res.converged, (res.max_residual, res.summary)
    pf = _t(res.transforms, "k2")
    dist = math.hypot(pf[0], pf[1])   # perp distance of centre to the Z axis
    assert abs(dist - (R + rf)) < 1e-4, f"follower not tangent: centre dist={dist} (want {R+rf})"
    print(f"[ext] cam_follower: centre dist={dist:.5f} (want {R+rf}) maxres={res.max_residual:.2e}")


def _rotvec(transforms, cid):
    """Recover the rotation vector w from a component's solved transform (log map)."""
    from engine.doc.mate_solver import _mat_to_rotvec
    R = _R(transforms, cid)
    return _mat_to_rotvec(R)


def main() -> None:
    test_width_centers_tab()
    test_symmetric_mirrors_about_plane()
    test_distance_limit_clamps_and_frees()
    test_angle_limit_clamps()
    test_profile_center_coincides_centroids()
    test_gear_ratio_couples_rotations()
    test_rack_pinion_couples_rotation_to_translation()
    test_screw_pitch_couples_rotation_to_advance()
    test_hinge_pins_axis_and_limits_swing()
    test_slot_pin_on_centerline_slides_free()
    test_cam_follower_tangent_to_cam()
    print("\nMATE SOLVER EXT TESTS PASSED")


if __name__ == "__main__":
    main()
