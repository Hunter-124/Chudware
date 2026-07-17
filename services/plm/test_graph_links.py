"""services.plm.test_graph_links — manual/AI CAD graph overlay (1B)."""
from __future__ import annotations

import io
import json
import struct
import zipfile
from pathlib import Path

import pytest

from services.plm import cad_graph, graph_links


def test_add_list_remove_roundtrip(tmp_path: Path):
    r = graph_links.add_link(tmp_path, "a", "b", "REFERENCES",
                             note="hi", origin="manual")
    assert r["ok"] and r["link"]["id"].startswith("l")
    links = graph_links.list_links(tmp_path)
    assert len(links) == 1
    assert links[0]["src"] == "a" and links[0]["kind"] == "REFERENCES"
    assert links[0]["note"] == "hi" and links[0]["origin"] == "manual"

    rm = graph_links.remove_link(tmp_path, r["link"]["id"])
    assert rm["ok"] and rm["removed"] == r["link"]["id"]
    assert graph_links.list_links(tmp_path) == []


def test_missing_file_is_empty(tmp_path: Path):
    assert graph_links.list_links(tmp_path) == []


def test_invalid_kind_rejected(tmp_path: Path):
    r = graph_links.add_link(tmp_path, "a", "b", "NOPE")
    assert r["ok"] is False and r["reason"] == "invalid_kind"
    assert graph_links.list_links(tmp_path) == []


def test_self_link_rejected(tmp_path: Path):
    r = graph_links.add_link(tmp_path, "a", "a", "REFERENCES")
    assert r["ok"] is False and r["reason"] == "invalid_link"


def test_remove_unknown(tmp_path: Path):
    r = graph_links.remove_link(tmp_path, "ldeadbeef")
    assert r["ok"] is False and r["reason"] == "not_found"


def test_ai_origin_persisted(tmp_path: Path):
    r = graph_links.add_link(tmp_path, "a", "b", "MATES", origin="ai")
    assert r["link"]["origin"] == "ai"


# --- merge into build_graph ------------------------------------------------

_ID = [1.0 if i % 5 == 0 else 0.0 for i in range(16)]


def _container(doc: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("document.json", json.dumps(doc))
    return buf.getvalue()


def test_overlay_merges_into_graph(tmp_path: Path):
    doc = {
        "schema": "aicad.part/1", "id": "uuid-p",
        "name": "P",
        "features": [{"id": "f1", "type": "box", "params": {"w": 1}}],
    }
    (tmp_path / "P.step+").write_bytes(_container(doc))
    g = cad_graph.build_graph(tmp_path, force=True)
    ids = [n["id"] for n in g["nodes"]]
    src, dst = ids[0], ids[1]

    graph_links.add_link(tmp_path, src, dst, "REFERENCES",
                         note="manual test", origin="manual")
    g2 = cad_graph.build_graph(tmp_path, force=True)
    manual = [e for e in g2["edges"] if e["origin"] == "manual"]
    assert manual, "expected the manual overlay link merged into the graph"
    assert manual[0]["meta"].get("note") == "manual test"
    assert manual[0]["meta"].get("link_id")


def test_overlay_link_rpc(tmp_path: Path):
    from engine_oracle.parts_hub.cad_graph_rpc import CAD_GRAPH_METHODS

    def _emit(*_a, **_k):
        pass

    add = CAD_GRAPH_METHODS["cad_graph_link_add"](
        {"project_dir": str(tmp_path), "src": "a", "dst": "b",
         "kind": "REFERENCES", "note": "n", "origin": "ai"}, _emit)
    assert add["ok"] and add["link"]["origin"] == "ai"

    lst = CAD_GRAPH_METHODS["cad_graph_link_list"](
        {"project_dir": str(tmp_path)}, _emit)
    assert lst["ok"] and len(lst["links"]) == 1

    rm = CAD_GRAPH_METHODS["cad_graph_link_remove"](
        {"project_dir": str(tmp_path), "link_id": add["link"]["id"]}, _emit)
    assert rm["ok"]
    lst2 = CAD_GRAPH_METHODS["cad_graph_link_list"](
        {"project_dir": str(tmp_path)}, _emit)
    assert lst2["links"] == []
