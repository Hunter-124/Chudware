"""slice_part / verify_gcode / list_print_profiles against the real engine and slicer.

These exercise the promise the tools make to an agent: a part sliced with the vendor
preset for the named printer, oriented by measurement, is print-ready; a part sliced in a
pose that leaves plastic in mid-air is NOT, whatever the slicer exit code says.
Skipped when libchudware or a stock PrusaSlicer install is unavailable.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest

from services.mcp import core_bridge as cb
from services.mcp.test_print_placement import _raw_stl_topology, write_sliver_box


def _slicer_installed() -> bool:
    try:
        from services.manufacturing.slicer import prusaslicer as ps
        from services.manufacturing.slicer.contract import SliceRefusal
    except Exception:  # noqa: BLE001
        return False
    return not isinstance(ps.discover(), SliceRefusal)


pytestmark = [
    pytest.mark.skipif(not cb.available(), reason="libchudware not staged"),
    pytest.mark.skipif(not _slicer_installed(), reason="no stock PrusaSlicer installed"),
]


def _call(tool: str, **args) -> dict:
    from mcp.server.fastmcp import FastMCP
    from services.mcp.print_tools import register_print_tools

    mcp = FastMCP("print-tools-test")
    register_print_tools(mcp)
    _content, structured = asyncio.run(mcp.call_tool(tool, args))
    return json.loads(structured["result"])


def _part(ops: list) -> str:
    doc = cb.call("doc_new", {"kind": "part",
                              "name": f"print_{uuid.uuid4().hex[:8]}"})["doc_id"]
    cb.call("doc_apply_ops", {"doc_id": doc, "ops": ops})
    return doc


def _tee() -> str:
    """A 10x20x20 stem under a 30x20x5 cap: two 10 mm one-sided shelves."""
    return _part([
        cb.feat("box", "Stem", {"length": cb.dim(10), "width": cb.dim(20),
                                "height": cb.dim(20)}),
        cb.feat("box", "Cap", {"length": cb.dim(30), "width": cb.dim(20),
                               "height": cb.dim(5), "at": [0, 0, 12.5]}),
    ])


def test_list_print_profiles_resolves_the_plain_model_not_a_variant():
    result = _call("list_print_profiles", printer="ender 3", material="PLA")
    assert result["ok"], result
    assert result["selected"] == "Creality Ender-3 (0.4 mm nozzle)", result
    assert "PLA" in result["default_filament"], result


def test_auto_orientation_finds_a_pose_that_needs_no_support(tmp_path):
    result = _call("slice_part", doc_id=_tee(), printer="ender 3", material="PLA",
                   output_dir=str(tmp_path), name="tee")
    assert result["ok"], result
    assert result["print_ready"] is True, result
    # Cap-down or T-silhouette-down: either way nothing overhangs. Upright (as modelled)
    # leaves two 10 mm shelves and must not be the pick.
    assert result["orientation"]["build_dir"] != pytest.approx([0, 0, 1], abs=1e-6)
    assert result["settings"]["support_material"] == "0", result["settings"]
    assert result["gcode_check"]["status"] == "pass", result["gcode_check"]
    gcode = Path(result["gcode_path"])
    assert gcode.is_file() and gcode.stat().st_size > 0
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert manifest["printer"]["name"] == "Creality Ender-3 (0.4 mm nozzle)"
    assert manifest["print_ready"] is True and manifest["gcode_sha256"]
    assert result["stats"]["print_time_s"] > 0 and result["stats"]["filament_g"] > 0


def test_as_is_without_support_is_not_print_ready_and_says_why(tmp_path):
    result = _call("slice_part", doc_id=_tee(), printer="ender 3", orientation="as_is",
                   supports="off", output_dir=str(tmp_path), name="tee_floating")
    assert result["ok"], result
    assert result["print_ready"] is False, result
    issues = {row["check"]: row for row in result["gcode_check"]["issues"]}
    assert issues["floating_extrusion"]["status"] == "fail", result["gcode_check"]


def test_as_is_with_auto_support_enables_support_and_prints(tmp_path):
    result = _call("slice_part", doc_id=_tee(), printer="ender 3", orientation="as_is",
                   output_dir=str(tmp_path), name="tee_supported")
    assert result["ok"], result
    assert result["settings"]["support_material"] == "1", result["settings"]
    assert result["print_ready"] is True, result


def test_part_larger_than_the_bed_is_refused_before_slicing(tmp_path):
    doc = _part([cb.feat("box", "Plate", {
        "length": cb.dim(320), "width": cb.dim(320), "height": cb.dim(4)})])
    result = _call("slice_part", doc_id=doc, printer="ender 3", output_dir=str(tmp_path))
    assert result["ok"] is False and result["reason"] == "does_not_fit_printer", result
    assert not list(tmp_path.glob("*.gcode"))


def test_unknown_slicer_override_is_refused_with_a_close_match(tmp_path):
    doc = _part([cb.feat("box", "Block", {
        "length": cb.dim(20), "width": cb.dim(20), "height": cb.dim(10)})])
    result = _call("slice_part", doc_id=doc, printer="ender 3", output_dir=str(tmp_path),
                   overrides={"fill_densty": "20%"})
    assert result["ok"] is False and result["reason"] == "invalid_override", result
    assert "fill_density" in json.dumps(result["refusal"]), result


def test_verify_gcode_flags_a_temperature_the_user_did_not_ask_for(tmp_path):
    doc = _part([cb.feat("box", "Block", {
        "length": cb.dim(20), "width": cb.dim(20), "height": cb.dim(6)})])
    sliced = _call("slice_part", doc_id=doc, printer="ender 3", nozzle_temp_c=215,
                   bed_temp_c=60, output_dir=str(tmp_path), name="block")
    assert sliced["ok"] and sliced["print_ready"], sliced
    good = _call("verify_gcode", path=sliced["gcode_path"], printer="ender 3",
                 material="PLA", nozzle_temp_c=215, bed_temp_c=60)
    assert good["ok"] is True, good
    wrong = _call("verify_gcode", path=sliced["gcode_path"], printer="ender 3",
                  material="PLA", nozzle_temp_c=230, bed_temp_c=60)
    rows = {row["check"]: row for row in wrong["checks"]}
    assert wrong["ok"] is False and rows["temperatures"]["status"] == "fail", wrong


def test_copies_arrange_a_part_modelled_far_from_the_origin(tmp_path):
    doc = _part([cb.feat("box", "Far", {"length": cb.dim(9), "width": cb.dim(9),
                                        "height": cb.dim(8), "at": [400, -300, 250]})])
    result = _call("slice_part", doc_id=doc, printer="ender 3", copies=3,
                   output_dir=str(tmp_path), name="far")
    assert result["ok"], result
    assert result["print_ready"] is True, result


def _box(length, width, height) -> str:
    return _part([cb.feat("box", "Box", {"length": cb.dim(length), "width": cb.dim(width),
                                         "height": cb.dim(height)})])


def test_sequential_plate_prints_each_object_to_completion_in_height_order(tmp_path):
    low, tall = _box(20, 20, 6), _box(12, 12, 30)
    result = _call("slice_part", parts=[{"doc_id": tall, "orientation": "as_is"},
                                        {"doc_id": low, "copies": 2}],
                   printer="ender 3", complete_individual_objects=True,
                   output_dir=str(tmp_path), name="small_parts")
    assert result["ok"] and result["print_ready"], result
    objects = result["plate"]["objects"]
    assert [o["height_mm"] for o in objects] == [6.0, 6.0, 30.0]
    assert result["plate"]["min_object_gap_mm"] >= 55
    # The toolpath itself prints the objects one after another in the planned order.
    gcode = Path(result["gcode_path"]).read_text()
    starts = [gcode.index(f"; printing object {o['gcode_label']}", gcode.index(";LAYER_CHANGE"))
              for o in objects]
    assert starts == sorted(starts)
    check = _call("verify_gcode", path=result["gcode_path"], printer="ender 3", material="PLA")
    rows = {row["check"]: row for row in check["checks"]}
    assert rows["sequential_clearance"]["status"] == "pass", rows["sequential_clearance"]
    assert rows["sequential_clearance"]["metrics"]["object_count"] == 3
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert manifest["effective"]["complete_objects"] == "1"
    assert [p["copies"] for p in manifest["plate"]["parts"]] == [1, 2]
    assert manifest["print_ready"] is True and manifest["source"]["kind"] == "plate"


def test_sequential_plate_with_two_objects_above_the_gantry_is_refused(tmp_path):
    first, second = _box(12, 12, 30), _box(12, 12, 28)
    result = _call("slice_part", parts=[{"doc_id": first, "orientation": "as_is"},
                                        {"doc_id": second, "orientation": "as_is"}],
                   printer="ender 3", complete_individual_objects=True,
                   output_dir=str(tmp_path), name="too_tall")
    assert result["ok"] is False and result["reason"] == "too_tall_for_sequential", result
    assert len(result["objects"]) == 2
    assert not list(tmp_path.glob("*.gcode"))


def test_a_sub_micron_vertex_pair_reaches_the_slicer_unmerged(tmp_path):
    """The 6-axis wrist case: PrusaSlicer used to delete two collapsed facets on load."""
    mesh = write_sliver_box(tmp_path / "sliver.stl")
    result = _call("slice_part", path=str(mesh), printer="ender 3", build_dir=[0, 1, 0],
                   supports="off", output_dir=str(tmp_path), name="sliver")
    assert result["ok"], result
    assert _raw_stl_topology(tmp_path / ".sliver.slice" / "sliver.oriented.stl") == (10, 0)
    repairs = result["slicer_mesh_repairs"]
    assert repairs["available"] and repairs["volumes"] == 1, repairs
    assert not any(repairs["counts"].values()), repairs
    manifest = json.loads(Path(result["manifest_path"]).read_text())
    assert manifest["slicer_mesh_repairs"] == repairs


def test_a_plate_position_that_would_merge_vertices_is_refused_before_slicing(tmp_path):
    mesh = str(write_sliver_box(tmp_path / "sliver.stl"))
    result = _call("slice_part", parts=[{"path": mesh, "build_dir": [0, 1, 0]},
                                        {"path": mesh, "build_dir": [0, 1, 0]}],
                   printer="ender 3", supports="off", output_dir=str(tmp_path), name="slivers")
    assert result["ok"] is False and result["reason"] == "slicer_mesh_precision_loss", result
    assert result["placement"] == "plate" and result["collapsed_triangles"] == 2
    assert not list(tmp_path.glob("*.gcode"))
