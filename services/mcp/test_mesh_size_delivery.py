"""The sim_mesh consumer sees increasing resolution, not a feature-band plateau."""
import json

import pytest

from services.mcp import core_bridge as cb


@pytest.mark.skipif(not cb.available(), reason="native mesh bridge unavailable")
def test_thin_floor_pocket_mesh_count_is_monotone(tmp_path):
    from services.agent.agent_turn import _bind_tool

    new_part = _bind_tool("new_part")
    add_features = _bind_tool("add_features")
    doc = json.loads(new_part(name="size_delivery_thin_floor"))["doc_id"]
    try:
        block = json.loads(add_features(doc_id=doc, ops=[{
            "op": "add_feature", "feature": {"type": "box", "name": "Stock",
            "params": {"length": 20, "width": 20, "height": 10}}}]))
        assert block["ok"], block
        pocket = json.loads(add_features(doc_id=doc, ops=[{
            "op": "pocket", "name": "ThinFloor", "face": "top", "shape": "rect",
            "width": 10, "height": 10, "depth": 9.5}]))
        assert pocket["ok"], pocket
        assert pocket["changes"]["volume_delta_mm3"] == pytest.approx(-950)
        path = str(tmp_path / "thin_floor.step")
        cb.call("doc_export_step", {"doc_id": doc, "path": path})
        previous = 0
        for h in (8.0, 6.0, 4.0, 2.8):
            mesh = cb.call("sim_mesh", {"step_path": path, "target_size_mm": h, "order": 2})
            assert mesh["ok"], mesh
            assert mesh["n_elems"] >= previous, (h, previous, mesh["n_elems"])
            assert mesh["jacobian_min"] > 0
            assert mesh["mesh_quality"]["size_band"]["applied_floor_mm"] == 0
            previous = mesh["n_elems"]
    finally:
        cb.call("doc_close", {"doc_id": doc})
