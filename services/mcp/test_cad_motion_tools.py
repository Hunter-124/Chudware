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
from services.mcp.cad_motion_tools import (
    _axis_rotation16, _mat_mul16, _pose_check, _turn_center_2d, steer_pose_sweep,
)

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


def test_pose_check_rejects_local_steering_about_a_requested_world_axis():
    point = [31.0, -558.8, 263.0]
    base = [1, 0, 0, point[0], 0, 0, -1, point[1],
            0, 1, 0, point[2], 0, 0, 0, 1]
    requested = {"point": point, "dir": [0, 0, 1]}
    world_pose = _mat_mul16(_axis_rotation16(point, requested["dir"], 35), base)
    local_pose = _mat_mul16(base, _axis_rotation16([0, 0, 0], requested["dir"], 35))
    assert _pose_check("Hub", base, world_pose, requested, 35)["ok"]
    rejected = _pose_check("Hub", base, local_pose, requested, 35)
    assert not rejected["ok"]
    assert not rejected["rotation_ok"]
    assert rejected["invariant_coordinate"] == {"axis": "z", "ok": False}


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
    doc = cb.call("doc_open", {"path": rover["path"], "detach_path": True})["doc_id"]
    assert doc
    cb.call("doc_save", {"doc_id": doc,
                        "path": str(Path(rover["path"]).with_name(f"{doc}.step+"))})
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
    """A measured concentric-mate axis must reproduce the explicit-axis turning radius."""
    by_axis = _call("steering_turning_radius", {
        "doc_id": rover["doc_id"], "steps": 3,
        "steer": {"components": ["wheel_FL", "wheel_FR"],
                  "axis": _axis("FL", "FR"), "angle_deg": 30}})
    assert by_axis["ok"], by_axis
    by_mate = _call("steering_turning_radius", {
        "doc_id": rover["doc_id"], "steps": 3,
        "steer": {"components": ["wheel_FL", "wheel_FR"],
                  "mate_ids": [rover["mates"]["FL"], rover["mates"]["FR"]],
                  "angle_deg": 30}})
    assert by_mate["ok"], by_mate
    # Compare the consumer's geometry, not an arbitrary point along the axis.
    # Mate selectors carry OCCT surface centroids rounded to 1e-6 mm, then
    # transported through solved placements; their coordinates are not exact datums.
    assert by_mate["turning_radius_mm"] == pytest.approx(
        by_axis["turning_radius_mm"], rel=1e-6)


def test_check_interference_names_the_first_steer_angle_that_clashes(rover):
    """A steering lock is geometry, not a number someone types: the sweep reports the
    first sampled angle at which a wheel actually overlaps the chassis."""
    clean = _call("steering_turning_radius", {
        "doc_id": rover["doc_id"], "steps": 3, "check_interference": True,
        "steer": {"components": ["wheel_FL", "wheel_FR"],
                  "axis": _axis("FL", "FR"), "angle_deg": 30}})
    assert clean["ok"], clean
    assert clean["interference"]["checked"] is True
    assert clean["interference"]["complete"] is True
    assert clean["interference"]["frames_checked"] == 3
    for frame in clean["trajectory"]:
        assert frame["interference"] is False
        assert frame["interference_complete"] is True
        report = frame["interference_report"]
        assert report["pairs_checked"] == report["pairs_total"] == 36
        # A threshold proof is enough even for the 1 mm wheel/kingpin gap:
        # there must be no invented exact minimum in that non-overlapping pair.
        pin, wheel = rover["ids"]["FL"]
        clearance = next(c for c in report["clearances"]
                         if {c["component_a"], c["component_b"]} == {pin, wheel})
        assert "distance" not in clearance
        assert clearance["distance_status"] == "lower_bound"
    assert clean["first_interference_deg"] is None, clean["interference"]

    locked = _call("steering_turning_radius", {
        "doc_id": rover["doc_id"], "steps": 5, "check_interference": True,
        "steer": {"components": ["wheel_FL", "wheel_FR"],
                  "axis": _axis("FL", "FR"), "angle_deg": 60}})
    assert locked["ok"], locked
    assert locked["interference"]["complete"] is True
    assert locked["interference"]["frames_checked"] == 5
    assert [f["interference"] for f in locked["trajectory"]] == [
        False, False, False, False, True]
    assert locked["first_interference_deg"] == pytest.approx(60.0, abs=1e-6)
    pair = locked["interference"]["detail"][0]
    assert {pair["a"], pair["b"]} == {"chassis", "wheel_FL"}, pair
    assert rover["ids"]["FL"][1] in (pair["component_a"], pair["component_b"])
    assert pair["overlap_volume_mm3"] > 0
    assert locked["trajectory"][-1]["collision_detail"][0] == pair


@pytest.mark.parametrize("failure,failed_step", [
    ("pair", 1), ("pair", 2), ("geometry", 1), ("unfinished_pair", 1),
    ("missing_accounting", 1), ("missing_complete", 1),
    ("failed_reply", 1), ("exception", 1),
])
def test_incomplete_steer_check_preserves_poses_and_known_collision(
        rover, monkeypatch, failure, failed_step):
    """An incomplete check cannot certify a sweep or erase a real full-lock hit."""
    original = cb.call
    sample = 0

    def interrupted_check(op, args=None, **kwargs):
        nonlocal sample
        if op != "eval_interference":
            return original(op, args, **kwargs)
        i = sample
        sample += 1
        if i == failed_step and failure == "exception":
            raise cb.CoreError("overlap evaluation failed", code="geometry_failed",
                               detail={"operation": "common"})
        report = original(op, args, **kwargs)
        if i != failed_step:
            return report
        if failure == "pair":
            report["complete"] = False
            report["pairs_checked"] -= 1
            report["pair_failures"] = [{
                "component_a": rover["ids"]["FL"][0],
                "component_b": rover["ids"]["FL"][1], "reason": "common_failed"}]
        elif failure == "geometry":
            report["complete"] = False
            report["geometry_failures"] = [{
                "component": rover["ids"]["FL"][0], "reason": "missing_geometry"}]
        elif failure == "unfinished_pair":
            report["pairs_checked"] -= 1
        elif failure == "missing_accounting":
            del report["pairs_total"]
        elif failure == "missing_complete":
            del report["complete"]
        elif failure == "failed_reply":
            return {"ok": False, "error": "overlap evaluation failed"}
        return report

    monkeypatch.setattr(cb, "call", interrupted_check)
    out = _call("steering_turning_radius", {
        "doc_id": rover["doc_id"], "steps": 3, "check_interference": True,
        "steer": {"components": ["wheel_FL", "wheel_FR"],
                  "axis": _axis("FL", "FR"), "angle_deg": 60}})
    assert out["ok"] is False
    assert out["interference"]["checked"] is True
    assert out["interference"]["complete"] is False
    assert out["interference"]["frames_checked"] == 2
    assert [f["value"] for f in out["trajectory"]] == [0, 30, 60]
    assert all(f["converged"] and f["pose_check"]["ok"] for f in out["trajectory"])
    failed = out["trajectory"][failed_step]
    assert failed["interference_complete"] is False
    assert failed["interference"] is (True if failed_step == 2 else None)
    assert [f["i"] for f in out["interference"]["check_failures"]] == [failed_step]
    if failure == "pair":
        assert out["interference"]["pair_failures"][0]["i"] == failed_step
        assert failed["pair_failures"][0]["reason"] == "common_failed"
    elif failure == "geometry":
        assert out["interference"]["geometry_failures"][0]["i"] == failed_step
        assert failed["geometry_failures"][0]["component"] == rover["ids"]["FL"][0]
    elif failure == "exception":
        assert failed["interference_report"]["code"] == "geometry_failed"
        assert failed["interference_report"]["detail"]["operation"] == "common"
    assert out["interference"]["first_collision_step"] == 2
    assert out["interference"]["first_collision_value"] == 60
    assert out["trajectory"][-1]["interference"] is True
    hit = out["interference"]["detail"][0]
    assert {hit["a"], hit["b"]} == {"chassis", "wheel_FL"}
    assert hit["overlap_volume_mm3"] > 0
    assert rover["ids"]["FL"][1] in (hit["component_a"], hit["component_b"])


def test_steer_check_connection_loss_aborts_without_replaying_motion(rover, monkeypatch):
    original = cb.call
    checks = 0
    lost = False
    later_mutations = []

    def disconnected_check(op, args=None, **kwargs):
        nonlocal checks, lost
        if lost and op == "doc_apply_ops":
            later_mutations.append(op)
        if op == "eval_interference":
            checks += 1
            if checks == 2:
                lost = True
                raise cb.CoreError("engine disconnected", code="engine_connection_lost")
        return original(op, args, **kwargs)

    monkeypatch.setattr(cb, "call", disconnected_check)
    with pytest.raises(cb.CoreError) as exc:
        steer_pose_sweep(
            rover["doc_id"],
            {"components": ["wheel_FL", "wheel_FR"],
             "axis": _axis("FL", "FR"), "angle_deg": 60},
            None, 3, True, "test_steer_connection_loss")
    assert exc.value.code == "engine_connection_lost"
    assert later_mutations == []


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


@pytest.mark.parametrize("hub_angle", [0, 90, -90])
def test_sweep_envelope_carries_nonidentity_wheel_pose_about_kingpin(tmp_path, hub_angle):
    """A world-Z steer preserves every wheel point's height, regardless of hub frame."""
    if not cb.available():
        pytest.skip("libchudware not staged; engine-backed MCP tools unavailable")
    wheel = _part("carried_wheel", [
        cb.feat("cylinder", "Disc", {"radius": cb.dim(260), "height": cb.dim(120)}),
    ], tmp_path)
    hub = _part("carried_hub", [
        cb.feat("cylinder", "Hub", {"radius": cb.dim(30), "height": cb.dim(100)}),
        cb.feat("transform", "Axle", {"rotate": [1, 0, 0], "angle": 90 - hub_angle}),
    ], tmp_path)
    asm = cb.call("doc_new", {"kind": "assembly", "name": "CarriedSteering"})["doc_id"]
    point = [31.0, 558.8, 263.0]
    c, s = math.cos(math.radians(hub_angle)), math.sin(math.radians(hub_angle))
    hub_pose = [1, 0, 0, point[0], 0, c, -s, point[1], 0, s, c, point[2], 0, 0, 0, 1]
    # The wheel's local spin axis is Z, placed on the hub's world -Y axle.
    # A free axle spin must not turn the source bbox's square corners into a
    # fictitious larger wheel before the steering has even begun.
    spin = math.radians(37)
    cs, ss = math.cos(spin), math.sin(spin)
    wheel_pose = [cs, -ss, 0, point[0], 0, 0, -1, point[1],
                  ss, cs, 0, point[2], 0, 0, 0, 1]
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
        static = next(b["bbox"] for b in _call(
            "list_world_bodies", {"doc_id": asm})["bodies"] if b["component"] == "k2")
        out = _call("sweep_envelope", {
            "doc_id": asm, "components": ["Wheel"], "steps": 5,
            "steer": {"components": ["Hub"],
                      "axis": {"point": point, "dir": [0, 0, 1]}, "angle_deg": 35},
        })
        assert out["ok"], out
        assert out["pose_check"]["ok"], out
        assert out["per_step"][0]["components"]["k2"]["min"] == pytest.approx(
            static["min"], abs=1e-5)
        assert out["per_step"][0]["components"]["k2"]["max"] == pytest.approx(
            static["max"], abs=1e-5)
        for frame in out["per_step"]:
            box = frame["components"]["k2"]
            initial = out["per_step"][0]["components"]["k2"]
            assert box["min"][2] == pytest.approx(initial["min"][2], abs=1e-6), frame
            assert box["max"][2] == pytest.approx(initial["max"][2], abs=1e-6), frame
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


def test_turning_radius_is_independent_of_hub_component_frame(tmp_path):
    """Equivalent hub geometry with identity/Rx(±90) frames has the same wheel axes."""
    if not cb.available():
        pytest.skip("libchudware not staged; engine-backed MCP tools unavailable")
    wheel = _part("radius_wheel", [
        cb.feat("cylinder", "Disc", {"radius": cb.dim(260), "height": cb.dim(120)}),
    ], tmp_path)
    radii = []
    for hub_angle in (0, 90, -90):
        hub = _part(f"radius_hub_{hub_angle}", [
            cb.feat("cylinder", "Hub", {"radius": cb.dim(30), "height": cb.dim(100)}),
            cb.feat("transform", "Axle", {"rotate": [1, 0, 0], "angle": 90 - hub_angle}),
        ], tmp_path)
        asm = cb.call("doc_new", {"kind": "assembly", "name": "RadiusFrames"})["doc_id"]
        try:
            c, s = math.cos(math.radians(hub_angle)), math.sin(math.radians(hub_angle))
            ops = []
            for tag, (x, y) in STATIONS.items():
                ops.extend([
                    {"op": "asm_insert_component", "source": {"path": hub},
                     "name": f"Hub_{tag}", "fixed": True,
                     "transform": [1, 0, 0, x, 0, c, -s, y, 0, s, c, 263, 0, 0, 0, 1]},
                    {"op": "asm_insert_component", "source": {"path": wheel},
                     "name": f"Wheel_{tag}", "fixed": False,
                     "transform": [1, 0, 0, x, 0, 0, -1, y, 0, 1, 0, 263, 0, 0, 0, 1]},
                ])
            cb.call("doc_apply_ops", {"doc_id": asm, "ops": ops, "with_tess": False})
            for i, tag in enumerate(STATIONS):
                mate = _call("asm_mate", {
                    "doc_id": asm, "mate_type": "concentric",
                    "a": {"component": f"k{2 * i + 1}", "query": {"of": "face", "surf": "round"}},
                    "b": {"component": f"k{2 * i + 2}", "query": {"of": "face", "surf": "round"}},
                })
                assert mate["ok"] and mate["converged"], mate
            cb.call("doc_save", {"doc_id": asm,
                                "path": str(tmp_path / f"radius_{hub_angle}.step+")})
            out = _call("steering_turning_radius", {
                "doc_id": asm, "steps": 3,
                "steer": {"components": ["Hub_FL", "Hub_FR"],
                          "wheels": ["Wheel_FL", "Wheel_FR"], "angle_deg": 35,
                          "axis": _axis("FL", "FR")},
                "rear_steer": {"components": ["Hub_RL", "Hub_RR"],
                               "wheels": ["Wheel_RL", "Wheel_RR"], "angle_deg": 0,
                               "axis": _axis("RL", "RR")},
            })
            assert out["ok"], out
            assert out["tightest"]["radius_source"] == "wheel axle lines", out
            radii.append(out["tightest"]["kinematic"]["turning_radius_mm"])
        finally:
            cb.call("doc_close", {"doc_id": asm})
    assert radii[1:] == pytest.approx([radii[0], radii[0]], abs=1e-9, rel=0)


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


# ------------------------------------------------------- synchronized native joint drives

@pytest.fixture
def motion_joint_chain(tmp_path):
    """An off-origin Z hinge carrying an X hinge: the second drive is not world-fixed."""
    if not cb.available():
        pytest.skip("libchudware not staged; engine-backed MCP tools unavailable")
    part = _part("motion_joint_body", [
        cb.feat("box", "Body", {"length": cb.dim(4), "width": cb.dim(6),
                                "height": cb.dim(8)}),
    ], tmp_path)
    # Native creation mints an independent document without MCP current-doc/reset
    # policy; the module-scoped rover and any other open documents must survive.
    created = cb.call("doc_new", {
        "kind": "assembly", "name": "motion_joint_chain", "resume_if_open": False})
    assert isinstance(created.get("doc_id"), str) and created["doc_id"], created
    doc = created["doc_id"]
    try:
        ids = {}
        for name, point, fixed in (
                ("base", [-20, -40, -10], True),
                ("carrier", [31, 18, 7], False),
                ("rotor", [60, 18, 7], False)):
            added = _call("asm_add_component", {
                "doc_id": doc, "path": part, "name": name, "at": point, "fixed": fixed})
            assert added["ok"], added
            ids[name] = added["component"]
        mates = {}
        for name, reference, moving, axis, point in (
                ("steer", "base", "carrier", [0, 0, 1], [31, 18, 7]),
                ("roll", "carrier", "rotor", [1, 0, 0], [60, 18, 7])):
            added = _call("asm_mate", {
                "doc_id": doc, "mate_type": "angle",
                "a": {"component": ids[reference], "query": {"of": "face", "side": "top"}},
                "b": {"component": ids[moving], "query": {"of": "face", "side": "top"}},
                "value": {"value": 0, "axis": axis, "axis_point": point},
            })
            assert added["ok"] and added["converged"], added
            mates[name] = added["mate"]
        yield {"doc_id": doc, "components": ids, "mates": mates, "directory": tmp_path}
    finally:
        cb.call("doc_close", {"doc_id": doc})


def test_multi_driver_motion_carries_joint_frame_and_reuses_checked_start(motion_joint_chain):
    """World placement must equal Rz(steer)*Rx(roll), not two independent world spins."""
    doc = motion_joint_chain["doc_id"]
    ids, mates = motion_joint_chain["components"], motion_joint_chain["mates"]
    before = cb.call("doc_tree", {"doc_id": doc})
    # Descendant first deliberately: declaration order is not kinematic dependency order.
    out = _call("simulate_motion", {
        "doc_id": doc, "steps": 3, "loop": True, "check_interference": True,
        "drivers": [{"mate_id": mates["roll"], "start": 15, "stop": 75},
                    {"mate_id": mates["steer"], "start": 0, "stop": 30}],
    })
    assert out["ok"], out
    assert out["pose_check"]["ok"], out
    assert out["mate_id"] == mates["roll"]
    assert {d["mate_id"]: d["unit"] for d in out["drivers"]} == {
        mates["roll"]: "deg", mates["steer"]: "deg"}
    frames = out["trajectory"]
    assert [f["i"] for f in frames] == [0, 1, 2, 3]
    for frame, steer, roll in zip(frames[:3], (0, 15, 30), (15, 45, 75)):
        assert frame["value"] == pytest.approx(roll)
        assert frame["driver_values"] == pytest.approx({
            mates["roll"]: roll, mates["steer"]: steer})
        a, b = math.radians(steer), math.radians(roll)
        ca, sa, cbeta, sbeta = math.cos(a), math.sin(a), math.cos(b), math.sin(b)
        poses = {body["component"]: body["transform"] for body in frame["bodies"]}
        assert poses[ids["base"]] == pytest.approx(
            [1, 0, 0, -20, 0, 1, 0, -40, 0, 0, 1, -10, 0, 0, 0, 1], abs=1e-6)
        assert poses[ids["carrier"]] == pytest.approx(
            [ca, -sa, 0, 31, sa, ca, 0, 18, 0, 0, 1, 7, 0, 0, 0, 1], abs=1e-6)
        assert poses[ids["rotor"]] == pytest.approx([
            ca, -sa * cbeta, sa * sbeta, 31 + 29 * ca,
            sa, ca * cbeta, -ca * sbeta, 18 + 29 * sa,
            0, sbeta, cbeta, 7,
            0, 0, 0, 1,
        ], abs=1e-6)
        assert frame["interference_complete"] is True
        assert frame["interference"] is False
    for body in frames[0]["bodies"]:
        assert out["reference_transforms"][body["component"]] == pytest.approx(
            body["transform"], abs=1e-6)
    # Reuse all evidence and both values, not a fabricated fourth solve at scalar start.
    assert frames[-1] == {**frames[0], "i": 3}
    assert out["pose_check"]["frames"][-1] == {
        **out["pose_check"]["frames"][0], "i": 3}
    assert out["interference"]["complete"] is True
    assert out["interference"]["frames_checked"] == 3
    assert out["interference"]["pair_failures"] == []
    assert cb.call("doc_tree", {"doc_id": doc}) == before


@pytest.mark.parametrize("invalid", ["duplicate", "suppressed", "nonfinite", "missing_bound"])
def test_multi_driver_motion_refuses_invalid_drive_set(motion_joint_chain, invalid):
    doc = motion_joint_chain["doc_id"]
    mid = motion_joint_chain["mates"]["steer"]
    drivers = [{"mate_id": mid, "start": 0, "stop": 30}]
    if invalid == "duplicate":
        drivers.append({"mate_id": mid, "start": 0, "stop": -30})
    elif invalid == "suppressed":
        cb.call("doc_apply_ops", {"doc_id": doc, "with_tess": False, "ops": [
            {"op": "asm_edit_mate", "id": mid, "suppressed": True}]})
    elif invalid == "nonfinite":
        drivers[0]["stop"] = float("inf")
    else:
        del drivers[0]["start"]
    before = cb.call("doc_tree", {"doc_id": doc})
    out = _call("simulate_motion", {"doc_id": doc, "drivers": drivers, "steps": 2})
    assert out["ok"] is False, out
    assert not out.get("trajectory"), out
    assert cb.call("doc_tree", {"doc_id": doc}) == before


def test_multi_driver_motion_refuses_nondimensional_mate(rover):
    before = cb.call("doc_tree", {"doc_id": rover["doc_id"]})
    out = _call("simulate_motion", {
        "doc_id": rover["doc_id"], "steps": 2,
        "drivers": [{"mate_id": rover["mates"]["FL"], "start": 0, "stop": 30}],
    })
    assert out["ok"] is False, out
    assert not out.get("trajectory"), out
    assert cb.call("doc_tree", {"doc_id": rover["doc_id"]}) == before


def test_multi_driver_motion_missing_geometry_is_not_clearance_evidence(motion_joint_chain):
    doc = motion_joint_chain["doc_id"]
    missing = cb.call("doc_apply_ops", {"doc_id": doc, "with_tess": False, "ops": [{
        "op": "asm_insert_component", "name": "unavailable_guard", "fixed": True,
        "source": {"path": str(motion_joint_chain["directory"] / "absent_guard.step+")},
    }]})
    missing_id = missing["id_map"]["$0"]
    before = cb.call("doc_tree", {"doc_id": doc})
    out = _call("simulate_motion", {
        "doc_id": doc, "steps": 3, "loop": True, "check_interference": True,
        "drivers": [{"mate_id": mid, "start": 0, "stop": 30}
                    for mid in motion_joint_chain["mates"].values()],
    })
    assert out["ok"] is False, out
    assert out["interference"]["complete"] is False
    assert out["interference"]["frames_checked"] == 0
    assert missing_id in {f["component"] for f in out["interference"]["geometry_failures"]}
    assert out["trajectory"] == []
    assert cb.call("doc_tree", {"doc_id": doc}) == before


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
