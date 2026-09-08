"""Kinematic-evidence motion tools: the steer drive and the turning-radius geometry.

A real steering assembly positions its uprights with concentric/coincident mates about
measured kingpin axes and owns NO angle/distance mate, so ``simulate_motion`` has nothing
to drive and ``steering_turning_radius`` used to refuse the whole mission with "no drivable
(dimensional) mate found". The ``steer`` spec drives the named components about that
kingpin axis on a detached clone instead, and the radius is read back out of the POSED
wheel axle lines. These tests pin that against closed-form Ackermann on an assembly of the
same shape (four wheels, each mated concentric to a kingpin pin), and keep the original
dimensional-mate path working.

Run:  python -m pytest services/mcp/test_cad_motion_tools.py -v
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import pytest

from services.mcp import core_bridge as cb
from services.mcp.cad_motion_tools import _turn_center_2d

# The test vehicle, in mm. Wheels are Ø600 x 200 discs whose kingpin bore runs vertically
# through the wheel centre, so the closed-form Ackermann geometry is exactly the one the
# assembly realises (zero scrub, zero caster).
WHEELBASE = 2000.0
TRACK = 1600.0
WHEEL_DIA = 600.0
WHEEL_WIDTH = 200.0
STATIONS = {"FL": (1000.0, 800.0), "FR": (1000.0, -800.0),
            "RL": (-1000.0, 800.0), "RR": (-1000.0, -800.0)}


# --------------------------------------------------------------------------- pure geometry

def test_axle_lines_of_a_parallel_steer_meet_on_the_rear_axle():
    """Two rear wheels plus two front wheels at the SAME angle: the turn centre sits on
    the rear axle at wheelbase/tan(steer) — the textbook bicycle-model radius — and the
    residual reports that a parallel steer is not a consistent Ackermann pose."""
    d = math.radians(30.0)
    lines = [((0.0, TRACK / 2), (0.0, 1.0)), ((0.0, -TRACK / 2), (0.0, 1.0))]
    for lat in (TRACK / 2, -TRACK / 2):
        lines.append(((WHEELBASE, lat), (-math.sin(d), math.cos(d))))
    tc = _turn_center_2d(lines)
    assert tc is not None
    assert tc["center"][0] == pytest.approx(0.0, abs=1e-9)
    assert tc["center"][1] == pytest.approx(WHEELBASE / math.tan(d), rel=1e-12)
    # Parallel steer cannot satisfy both front wheels; the miss is (track/2)*sin(steer).
    assert tc["residual_mm"] == pytest.approx(TRACK / 2 * math.sin(d), rel=1e-9)


def test_equal_and_opposite_rear_steer_moves_the_turn_centre_to_mid_wheelbase():
    """4WS: the axle lines meet abeam the vehicle centre, halving the radius."""
    d = math.radians(30.0)
    lines = []
    for lat in (TRACK / 2, -TRACK / 2):
        lines.append(((WHEELBASE, lat), (-math.sin(d), math.cos(d))))
        lines.append(((0.0, lat), (math.sin(d), math.cos(d))))
    tc = _turn_center_2d(lines)
    assert tc is not None
    assert tc["center"][0] == pytest.approx(WHEELBASE / 2, rel=1e-12)
    assert tc["center"][1] == pytest.approx(WHEELBASE / 2 / math.tan(d), rel=1e-12)


def test_straight_ahead_axle_lines_have_no_turn_centre():
    """Parallel axle lines put the centre at infinity; None is the only honest answer."""
    lines = [((x, lat), (0.0, 1.0))
             for x in (0.0, WHEELBASE) for lat in (TRACK / 2, -TRACK / 2)]
    assert _turn_center_2d(lines) is None


# ------------------------------------------------------------------------------- fixtures

def _call(name: str, args: dict) -> dict:
    content, structured = asyncio.run(_server().mcp.call_tool(name, args))
    text = structured.get("result") if isinstance(structured, dict) else None
    assert isinstance(text, str), (name, structured)
    return json.loads(text)


def _server():
    from services.mcp import server  # noqa: PLC0415 — importing registers ~470 tools
    return server


def _part(name: str, ops: list, out_dir: Path) -> str:
    doc = cb.call("doc_new", {"kind": "part", "name": name})["doc_id"]
    r = cb.call("doc_apply_ops", {"doc_id": doc, "ops": ops})
    failed = [s for s in (r.get("rebuild") or {}).get("feature_status", [])
              if not s.get("ok")]
    assert not failed, failed
    path = str(out_dir / f"{name}.step+")
    cb.call("doc_save", {"doc_id": doc, "path": path})
    cb.call("doc_close", {"doc_id": doc})
    return path


@pytest.fixture(scope="module")
def rover(tmp_path_factory):
    """A two-axle steering assembly with NO dimensional mate.

    Each wheel is mated `concentric` to a fixed kingpin pin — exactly the topology that
    leaves the uprights posed by the mate solver with nothing to drive. Returns the saved
    path plus the component-id map.
    """
    if not cb.available():
        pytest.skip("libchudware not staged; engine-backed MCP tools unavailable")
    out = tmp_path_factory.mktemp("rover")
    wheel = _part("tm_wheel", [
        cb.feat("cylinder", "Disc", {"radius": cb.dim(WHEEL_DIA / 2),
                                     "height": cb.dim(WHEEL_WIDTH)}),
        # Lay the disc over so its spin axis is the vehicle's lateral direction.
        cb.feat("transform", "LayOver", {"rotate": [1, 0, 0], "angle": 90}),
        cb.feat("cylinder", "KingpinBore", {"radius": cb.dim(25), "height": cb.dim(900),
                                            "mode": "cut"}),
    ], out)
    pin = _part("tm_kingpin", [
        cb.feat("cylinder", "Pin", {"radius": cb.dim(24), "height": cb.dim(500)}),
    ], out)
    # Narrow enough that a 30 deg steer clears it and a 60 deg steer does not.
    chassis = _part("tm_chassis", [
        cb.feat("box", "Frame", {"length": cb.dim(WHEELBASE), "width": cb.dim(1000),
                                 "height": cb.dim(100)}),
    ], out)

    asm = _call("new_assembly", {"name": "tm_rover"})["doc_id"]
    assert _call("asm_add_component", {"doc_id": asm, "path": chassis,
                                       "name": "chassis", "fixed": True})["ok"]
    ids, mates = {}, {}
    for tag, (x, y) in STATIONS.items():
        p = _call("asm_add_component", {"doc_id": asm, "path": pin, "fixed": True,
                                        "name": f"kingpin_{tag}", "at": [x, y, 0]})
        w = _call("asm_add_component", {"doc_id": asm, "path": wheel,
                                        "name": f"wheel_{tag}", "at": [x, y, 0]})
        assert p["ok"] and w["ok"], (p, w)
        ids[tag] = (p["component"], w["component"])
    for tag, (pid, wid) in ids.items():
        m = _call("asm_mate", {
            "doc_id": asm, "mate_type": "concentric",
            "a": {"component": pid, "query": {"of": "face", "surf": "round"}},
            "b": {"component": wid, "query": {"of": "face", "surf": "round",
                                              "pick": "smallest"}}})
        assert m["ok"] and m["converged"], m
        mates[tag] = m["mate"]
    path = str(out / "tm_rover.step+")
    assert _call("save_part", {"doc_id": asm, "path": path})["ok"]
    tree = cb.call("doc_tree", {"doc_id": asm})["tree"]
    assert not [m for m in tree.get("mates", [])
                if m.get("type") in ("angle", "distance")], \
        "the fixture must own NO drivable dimensional mate"
    return {"doc_id": asm, "path": path, "ids": ids, "mates": mates}


def _axis(*tags) -> list:
    return [{"point": [STATIONS[t][0], STATIONS[t][1], 0.0], "dir": [0, 0, 1]}
            for t in tags]


def _reopened(rover) -> str:
    doc = cb.call("doc_open", {"path": rover["path"]})["doc_id"]
    assert doc
    return doc


# ---------------------------------------------------------------------- the steer drive

def test_a_mateless_steering_assembly_is_told_how_to_be_driven(rover):
    """The refusal that killed the mission must name the escape hatch, not just say no."""
    out = _call("steering_turning_radius", {"doc_id": rover["doc_id"]})
    assert out["ok"] is False
    assert "no drivable (dimensional) mate found" in out["error"]
    assert "steer=" in out["error"] and "kingpin" in out["error"]


def test_steer_spec_reproduces_the_closed_form_ackermann_radius(rover):
    """Both uprights rotated 30 deg about their kingpin axes: the radius read out of the
    posed axle lines is the bicycle-model wheelbase/tan(steer), and every wheel's radius
    is the distance from the turn centre to that wheel."""
    out = _call("steering_turning_radius", {
        "doc_id": rover["doc_id"], "steps": 3,
        "steer": {"components": ["wheel_FL", "wheel_FR"],
                  "axis": _axis("FL", "FR"), "angle_deg": 30}})
    assert out["ok"], out
    expected = WHEELBASE / math.tan(math.radians(30.0))
    assert out["turning_radius_mm"] == pytest.approx(expected, rel=0.01)
    assert out["turning_radius_m"] == pytest.approx(out["turning_radius_mm"] / 1000.0)
    assert out["turning_radius_ft"] == pytest.approx(out["turning_radius_mm"] / 304.8)
    assert out["wheelbase_mm"] == pytest.approx(WHEELBASE, rel=1e-6)
    assert out["track_mm"] == pytest.approx(TRACK, rel=1e-6)

    kin = out["tightest"]["kinematic"]
    assert out["tightest"]["radius_source"] == "wheel axle lines"
    # The turn centre is on the rear axle line (station 0) — a 2WS invariant.
    assert kin["turn_center_station_mm"] == pytest.approx(0.0, abs=1.0)
    # Left steer, so the LEFT wheel is inner and turns the tighter circle.
    assert kin["inner"]["name"] == "wheel_FL"
    assert kin["outer"]["name"] == "wheel_FR"
    assert kin["inner"]["radius_mm"] < kin["outer"]["radius_mm"]
    assert kin["inner"]["radius_mm"] == pytest.approx(
        math.hypot(WHEELBASE, expected - TRACK / 2), rel=1e-3)
    assert kin["outer"]["radius_mm"] == pytest.approx(
        math.hypot(WHEELBASE, expected + TRACK / 2), rel=1e-3)
    for w in kin["wheels"]:
        assert w["radius_ft"] == pytest.approx(w["radius_mm"] / 304.8)
        if w["axle"] == "front":
            assert w["steer_angle_deg"] == pytest.approx(30.0, abs=0.01)
            assert w["requested_deg"] == pytest.approx(30.0)
        else:
            assert w["steer_angle_deg"] == pytest.approx(0.0, abs=0.01)
    # Parallel steer is geometrically inconsistent, and the report says so rather than
    # averaging it away.
    assert kin["axle_line_residual_mm"] == pytest.approx(
        TRACK / 2 * math.sin(math.radians(30.0)), rel=1e-3)


def test_a_true_ackermann_pair_has_a_consistent_turn_centre(rover):
    """Per-wheel `angles_deg`: the inner/outer pair that satisfies Ackermann exactly makes
    every axle line pass through one point (residual ~0) and lands the textbook
    wheelbase/tan(inner) + track/2 radius."""
    inner = 30.0
    outer = math.degrees(math.atan2(
        WHEELBASE, WHEELBASE / math.tan(math.radians(inner)) + TRACK))
    out = _call("steering_turning_radius", {
        "doc_id": rover["doc_id"], "steps": 3,
        "steer": {"components": ["wheel_FL", "wheel_FR"],
                  "axis": _axis("FL", "FR"), "angles_deg": [inner, outer]}})
    assert out["ok"], out
    kin = out["tightest"]["kinematic"]
    assert kin["axle_line_residual_mm"] < 0.01
    expected = WHEELBASE / math.tan(math.radians(inner)) + TRACK / 2
    assert out["turning_radius_mm"] == pytest.approx(expected, rel=1e-4)
    # …and it agrees with the closed-form cross-check computed from the same angles.
    assert out["tightest"]["centerline_radius_mm"] == pytest.approx(expected, rel=1e-4)
    assert abs(out["tightest"]["ackermann_error_deg"]) < 0.01


def test_four_wheel_steer_halves_the_turning_radius(rover):
    """Equal-and-opposite rear steer: the turn centre moves to mid-wheelbase and the
    radius halves — the whole point of 4WS, and the reason the rear axle is not assumed
    to be the reference point."""
    two = _call("steering_turning_radius", {
        "doc_id": rover["doc_id"], "steps": 3,
        "steer": {"components": ["wheel_FL", "wheel_FR"],
                  "axis": _axis("FL", "FR"), "angle_deg": 30}})
    four = _call("steering_turning_radius", {
        "doc_id": rover["doc_id"], "steps": 3,
        "steer": {"components": ["wheel_FL", "wheel_FR"],
                  "axis": _axis("FL", "FR"), "angle_deg": 30},
        "rear_steer": {"components": ["wheel_RL", "wheel_RR"],
                       "axis": _axis("RL", "RR"), "angle_deg": -30}})
    assert two["ok"] and four["ok"], (two.get("error"), four.get("error"))
    assert four["turning_radius_mm"] == pytest.approx(
        WHEELBASE / 2 / math.tan(math.radians(30.0)), rel=0.01)
    assert four["turning_radius_mm"] == pytest.approx(
        two["turning_radius_mm"] / 2.0, rel=0.01)
    kin = four["tightest"]["kinematic"]
    assert kin["turn_center_station_mm"] == pytest.approx(WHEELBASE / 2, rel=0.01)
    assert {w["axle"] for w in kin["wheels"]} == {"front", "rear"}
    rear = [w for w in kin["wheels"] if w["axle"] == "rear"]
    assert all(w["steer_angle_deg"] == pytest.approx(-30.0, abs=0.01) for w in rear)


def test_the_kingpin_axis_can_come_from_the_concentric_mate(rover):
    """`mate_id` instead of `axis`: the concentric mate already carries the measured
    kingpin axis, so naming it must give the identical answer to spelling the axis out."""
    by_axis = _call("steering_turning_radius", {
        "doc_id": rover["doc_id"], "steps": 3,
        "steer": {"components": ["wheel_FL", "wheel_FR"],
                  "axis": _axis("FL", "FR"), "angle_deg": 30}})
    by_mate = _call("steering_turning_radius", {
        "doc_id": rover["doc_id"], "steps": 3,
        "steer": {"components": ["wheel_FL", "wheel_FR"],
                  "mate_ids": [rover["mates"]["FL"], rover["mates"]["FR"]],
                  "angle_deg": 30}})
    assert by_mate["ok"], by_mate
    assert by_mate["turning_radius_mm"] == pytest.approx(
        by_axis["turning_radius_mm"], rel=1e-6)
    sources = [a["source"] for a in by_mate["driver"]["axes"]]
    assert all("mate" in s for s in sources), sources
    for got, want in zip(by_mate["driver"]["axes"], _axis("FL", "FR")):
        assert got["point"] == pytest.approx(want["point"], abs=1e-6)


def test_check_interference_names_the_first_steer_angle_that_clashes(rover):
    """A steering lock is geometry, not a number someone types: the sweep reports the
    first sampled angle at which a wheel actually overlaps the chassis."""
    clean = _call("steering_turning_radius", {
        "doc_id": rover["doc_id"], "steps": 3, "check_interference": True,
        "steer": {"components": ["wheel_FL", "wheel_FR"],
                  "axis": _axis("FL", "FR"), "angle_deg": 30}})
    assert clean["ok"], clean
    assert clean["interference"]["checked"] is True
    assert clean["first_interference_deg"] is None, clean["interference"]

    locked = _call("steering_turning_radius", {
        "doc_id": rover["doc_id"], "steps": 5, "check_interference": True,
        "steer": {"components": ["wheel_FL", "wheel_FR"],
                  "axis": _axis("FL", "FR"), "angle_deg": 60}})
    assert locked["ok"], locked
    assert locked["first_interference_deg"] == pytest.approx(60.0, abs=1e-6)
    pair = locked["interference"]["detail"][0]
    assert {pair["a"], pair["b"]} == {"chassis", "wheel_FL"}, pair


def test_the_steer_drive_never_touches_the_callers_document(rover):
    """The drive AUTHORS poses (`asm_edit_component` + `fixed`), so it must land on a
    detached clone: the live document keeps its revision and its mate-owned poses."""
    doc = _reopened(rover)
    try:
        before = cb.call("doc_tree", {"doc_id": doc})
        rev = before["revision"]
        poses = {c["id"]: c.get("transform") for c in before["tree"]["components"]}
        out = _call("steering_turning_radius", {
            "doc_id": doc, "steps": 3,
            "steer": {"components": ["wheel_FL", "wheel_FR"],
                      "axis": _axis("FL", "FR"), "angle_deg": 30}})
        assert out["ok"], out
        after = cb.call("doc_tree", {"doc_id": doc})
        assert after["revision"] == rev
        assert {c["id"]: c.get("transform")
                for c in after["tree"]["components"]} == poses
        assert all(not c.get("fixed") for c in after["tree"]["components"]
                   if c["name"].startswith("wheel_"))
    finally:
        cb.call("doc_close", {"doc_id": doc})


def test_sweep_envelope_takes_the_same_steer_spec(rover):
    """The envelope of a steering sweep needs no dimensional mate either, and it covers
    the whole 0..lock range rather than only the extreme."""
    out = _call("sweep_envelope", {
        "doc_id": rover["doc_id"], "steps": 4, "components": ["wheel_FL"],
        "steer": {"components": ["wheel_FL", "wheel_FR"],
                  "axis": _axis("FL", "FR"), "angle_deg": 45}})
    assert out["ok"], out
    assert out["driver"]["type"] == "steer"
    assert out["driver"]["stop"] == pytest.approx(45.0)
    assert out["mate_id"] is None
    size = out["envelope"]["size"]
    # At 45 deg the disc's lateral half-extent is sin45*R + cos45*(width/2).
    lateral = 2 * (math.sin(math.radians(45)) * WHEEL_DIA / 2 +
                   math.cos(math.radians(45)) * WHEEL_WIDTH / 2)
    assert size[1] == pytest.approx(lateral, rel=2e-3), size
    assert size[1] > WHEEL_WIDTH, "a steer sweep must widen the lateral envelope"
    assert size[2] == pytest.approx(WHEEL_DIA, rel=1e-3), size
    assert len(out["per_step"]) == 4


def test_sweep_envelope_carries_nonidentity_wheel_pose_about_kingpin(tmp_path):
    """Steering the hub must not inject free axle spin into its carried wheel."""
    if not cb.available():
        pytest.skip("libchudware not staged; engine-backed MCP tools unavailable")
    wheel = _part("carried_wheel", [
        cb.feat("cylinder", "Disc", {"radius": cb.dim(260), "height": cb.dim(120)}),
    ], tmp_path)
    hub = _part("carried_hub", [
        cb.feat("cylinder", "Hub", {"radius": cb.dim(30), "height": cb.dim(100)}),
        cb.feat("transform", "Axle", {"rotate": [1, 0, 0], "angle": 90}),
    ], tmp_path)
    asm = cb.call("doc_new", {"kind": "assembly", "name": "CarriedSteering"})["doc_id"]
    point = [31.0, 558.8, 263.0]
    hub_pose = [1, 0, 0, point[0], 0, 1, 0, point[1], 0, 0, 1, point[2], 0, 0, 0, 1]
    # The wheel's local spin axis is Z, placed on the hub's world -Y axle.
    # An identity component pose misses the solver's rotation-vector nullspace bug.
    wheel_pose = [1, 0, 0, point[0], 0, 0, -1, point[1], 0, 1, 0, point[2], 0, 0, 0, 1]
    try:
        cb.call("doc_apply_ops", {"doc_id": asm, "with_tess": False, "ops": [
            {"op": "asm_insert_component", "source": {"path": hub}, "name": "Hub",
             "fixed": True, "transform": hub_pose},
            {"op": "asm_insert_component", "source": {"path": wheel}, "name": "Wheel",
             "transform": wheel_pose},
        ]})
        mate = _call("asm_mate", {
            "doc_id": asm, "mate_type": "concentric",
            "a": {"component": "k1", "query": {"of": "face", "surf": "round"}},
            "b": {"component": "k2", "query": {"of": "face", "surf": "round"}},
        })
        assert mate["ok"] and mate["converged"], mate
        cb.call("doc_save", {"doc_id": asm, "path": str(tmp_path / "carried.step+")})
        before = cb.call("doc_tree", {"doc_id": asm})
        out = _call("sweep_envelope", {
            "doc_id": asm, "components": ["Wheel"], "steps": 5,
            "steer": {"components": ["Hub"],
                      "axis": {"point": point, "dir": [0, 0, 1]}, "angle_deg": 35},
        })
        assert out["ok"], out
        assert out["pose_check"]["ok"], out
        for frame in out["per_step"]:
            box = frame["components"]["k2"]
            assert box["min"][2] == pytest.approx(3.0, abs=1e-6), frame
            assert box["max"][2] == pytest.approx(523.0, abs=1e-6), frame
        for frame in out["pose_check"]["frames"]:
            checks = {b["component"]: b for b in frame["bodies"]}
            for cid in ("k1", "k2"):
                assert checks[cid]["axis_point"] == point
                assert checks[cid]["axis_dir"] == [0, 0, 1]
                assert checks[cid]["translation_ok"] and checks[cid]["rotation_ok"]
                assert checks[cid]["invariant_coordinate"] == {"axis": "z", "ok": True}
        assert cb.call("doc_tree", {"doc_id": asm}) == before
    finally:
        cb.call("doc_close", {"doc_id": asm})


_AX_FL = {"point": [1000.0, 800.0, 0.0], "dir": [0, 0, 1]}


@pytest.mark.parametrize("spec,fragment", [
    ({"components": ["wheel_FL"], "angle_deg": 30}, "kingpin axis"),
    ({"components": ["wheel_FL"], "axis": {"point": [0, 0, 0]}, "angle_deg": 30},
     "3-number `point`"),
    ({"components": ["wheel_FL"], "axis": {"point": [0, 0, 0], "dir": [0, 0, 0]},
      "angle_deg": 30}, "zero-length vector"),
    ({"components": ["nope"], "axis": _AX_FL, "angle_deg": 30}, "not found"),
    ({"components": ["wheel_FL"], "axis": _AX_FL, "steer_degrees": 30}, "unknown key"),
    ({"components": ["wheel_FL", "wheel_FR"], "axis": _AX_FL, "angles_deg": [30]},
     "1 angle(s) for 2 component(s)"),
    ({"components": ["wheel_FL"], "axis": _AX_FL, "angle_deg": 120},
     "not a finite angle"),
    ({"components": ["wheel_FL"], "mate_id": "m99", "angle_deg": 30}, "is not a mate"),
    ({"components": ["wheel_FL"], "axis": _AX_FL}, "give the steer angle"),
])
def test_a_steer_spec_that_would_move_nothing_is_refused(rover, spec, fragment):
    """Every one of these silently steered nothing (or lied) in a hand-rolled attempt."""
    out = _call("steering_turning_radius",
                {"doc_id": rover["doc_id"], "steps": 2, "steer": spec})
    assert out["ok"] is False, out
    assert fragment in out["error"], out["error"]


def test_steer_and_a_mate_sweep_are_not_both_accepted(rover):
    """Two different drives in one call is an ambiguity, not a merge."""
    out = _call("steering_turning_radius", {
        "doc_id": rover["doc_id"], "steer_mate_id": "m1", "stop": 30.0,
        "steer": {"components": ["wheel_FL"], "axis": _axis("FL")[0], "angle_deg": 30}})
    assert out["ok"] is False
    assert "one or the other" in out["error"]


# ------------------------------------------------------- the original dimensional path

def test_the_dimensional_mate_path_still_works(rover):
    """Add an angle mate and the classic drive is unchanged: simulate_motion sweeps it and
    the closed-form Ackermann numbers come back off the measured inner steer angle."""
    doc = _reopened(rover)
    try:
        wheel_fl = rover["ids"]["FL"][1]
        m = _call("asm_mate", {
            "doc_id": doc, "mate_type": "angle", "value": 0.0,
            "a": {"component": "k1", "query": {"of": "face", "side": "left"}},
            "b": {"component": wheel_fl,
                  "query": {"of": "face", "surf": "round", "pick": "largest"}}})
        assert m["ok"] and m["converged"], m
        out = _call("steering_turning_radius", {
            "doc_id": doc, "steer_mate_id": m["mate"], "start": 0.0, "stop": 30.0,
            "steps": 3, "wheelbase_mm": WHEELBASE, "track_mm": TRACK})
        assert out["ok"], out
        assert out["mate_id"] == m["mate"]
        assert out["driver"]["type"] == "angle" and out["driver"]["unit"] == "deg"
        assert out["steered_components"] == ["wheel_FL"]
        row = out["tightest"]
        assert row["radius_source"] == "closed-form Ackermann"
        assert row["kinematic"] is None, "one steered wheel yields no axle-line fit"
        # The closed form, evaluated on the angle the sweep actually reached.
        di = math.radians(row["inner_steer_deg"])
        assert row["inner_steer_deg"] == pytest.approx(30.0, abs=0.05)
        assert row["centerline_radius_mm"] == pytest.approx(
            WHEELBASE / math.tan(di) + TRACK / 2, rel=1e-9)
        assert row["inner_wheel_radius_mm"] == pytest.approx(
            WHEELBASE / math.sin(di), rel=1e-9)
        assert out["turning_radius_mm"] == pytest.approx(row["centerline_radius_mm"])
        # Three identical non-steered wheels are not one axle, and the tool says so
        # instead of inventing a wheelbase from their centroid.
        assert any("do not share one longitudinal station" in w
                   for w in out["warnings"]), out["warnings"]
    finally:
        cb.call("doc_close", {"doc_id": doc})


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
