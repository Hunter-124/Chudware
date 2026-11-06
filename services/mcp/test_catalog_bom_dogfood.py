"""Saved catalog hardware keeps its identity through the assembly BOM."""
import math

import pytest

from services.mcp import core_bridge as cb
from services.mcp.test_cad_tools import _call

pytestmark = pytest.mark.skipif(not cb.available(), reason="native CAD engine unavailable")


def test_saved_catalog_fastener_keeps_identity_in_bom(tmp_path):
    part_id = "fastener:ISO 4762:M5"
    part_path = tmp_path / "m5.step+"
    asm_path = tmp_path / "assembly.step+"
    docs = []
    try:
        placed, _ = _call("part_place", {"part_id": part_id,
                                         "params": '{"length_mm":10}',
                                         "include_live": False})
        assert placed["ok"], placed
        part = placed["doc_id"]
        docs.append(part)
        cb.call("doc_save", {"doc_id": part, "path": str(part_path)})
        cb.call("doc_close", {"doc_id": part})
        docs.remove(part)

        part = cb.call("doc_open", {"path": str(part_path)})["doc_id"]
        docs.append(part)
        features = cb.call("doc_tree", {"doc_id": part})["tree"]["features"]
        assert {"hex socket profile", "hex socket"} <= {f.get("name") for f in features}
        # A real 4 mm across-flats, 2.5 mm deep hex recess removes this exact volume.
        volume = cb.call("eval_mass", {"doc_id": part})["volume_mm3"]
        envelope = math.pi * (2.5**2 * 10 + 4.25**2 * 5)
        assert volume == pytest.approx(envelope - 8 * math.sqrt(3) * 2.5, rel=1e-5)
        cb.call("doc_close", {"doc_id": part})
        docs.remove(part)

        assembly = cb.call("doc_new", {"name": "catalog BOM", "kind": "assembly"})["doc_id"]
        docs.append(assembly)
        cb.call("doc_save", {"doc_id": assembly, "path": str(asm_path)})
        inserted = cb.call("doc_apply_ops", {"doc_id": assembly, "ops": [
            {"op": "asm_insert_component", "name": "M5 screw", "fixed": True,
             "source": {"path": part_path.name}}
        ]})
        assert inserted.get("ok", True), inserted
        cb.call("doc_save", {"doc_id": assembly, "path": str(asm_path)})
        cb.call("doc_close", {"doc_id": assembly})
        docs.remove(assembly)
        assembly = cb.call("doc_open", {"path": str(asm_path)})["doc_id"]
        docs.append(assembly)

        result, _ = _call("bom_rollup", {"doc_path": str(asm_path)})
        assert result["ok"], result
        assert len(result["bom"]) == 1, result
        line = result["bom"][0]
        assert line["kind"] == "bought"
        assert line["part_ref"] == part_id
        assert line["quantity"] == 1
        assert line["mpn"] is None
        assert line["vendor_part"] is None
        assert line["unit_cost"] is None and line["ext_cost"] is None
        cb.call("doc_close", {"doc_id": assembly})
        docs.remove(assembly)
        closed_result, _ = _call("bom_rollup", {"doc_path": str(asm_path)})
        assert closed_result["ok"], closed_result
        assert closed_result["bom"] == result["bom"]
    finally:
        for doc in reversed(docs):
            cb.call("doc_close", {"doc_id": doc})
