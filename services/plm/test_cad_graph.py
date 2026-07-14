"""services.plm.test_cad_graph — contracts/cad_graph.md v1.

Fixture project: 2 parts + assembly with mates + shared dim + BOM.
Asserts:
  * expected nodes/edges (kinds, INSTANCE_OF, MATES, REFERENCES)
  * blast_radius of a referenced dim returns dependents
  * cache invalidates on mtime bump
"""
from __future__ import annotations

import io
import json
import os
import time
import zipfile
from pathlib import Path

import pytest

from services.plm import cad_graph


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

_ID = [1.0 if i % 5 == 0 else 0.0 for i in range(16)]


def _container(doc: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("thumbnail.png", b"\x89PNG\r\n_stub")
        zf.writestr("document.json", json.dumps(doc, indent=2))
    return buf.getvalue()


def _write(project: Path, name: str, doc: dict) -> None:
    (project / name).write_bytes(_container(doc))


def _part_a() -> dict:
    """Bracket with wall dim + sketch + extrude feature chain."""
    return {
        "schema": "aicad.part/1",
        "id": "uuid-part-a",
        "name": "Bracket",
        "equations": {
            "globals": [
                {"name": "wall", "expr": "3", "value": 3.0, "unit": "mm"},
                {"name": "hole_d", "expr": "6", "value": 6.0, "unit": "mm"},
            ],
        },
        "features": [
            {
                "id": "sk1", "type": "sketch", "name": "Sketch1",
                "params": {
                    "entities": [{"id": "e1", "type": "line"}],
                    "constraints": [
                        {"id": "d1", "type": "distance",
                         "value": {"value": 3.0, "unit": "mm"}},
                    ],
                },
            },
            {
                "id": "f1", "type": "extrude", "name": "Base",
                "params": {"depth": {"value": 3.0, "unit": "mm"}, "wall": 3.0},
                "refs": {"sketch": "sk1"},
            },
            {
                "id": "f2", "type": "hole", "name": "MountHole",
                "params": {"diameter": {"value": 6.0, "unit": "mm"}},
                "refs": {"face": {"feature": "f1"}},
            },
        ],
        "bodies": [
            {"id": "b1", "name": "Body1", "owner_feature": "f1"},
        ],
    }


def _part_b() -> dict:
    """Plate sharing wall=3mm and hole_d=6mm with Bracket (REFERENCES)."""
    return {
        "schema": "aicad.part/1",
        "id": "uuid-part-b",
        "name": "Plate",
        "equations": {
            "globals": [
                {"name": "wall", "expr": "3", "value": 3.0, "unit": "mm"},
                {"name": "hole_d", "expr": "6", "value": 6.0, "unit": "mm"},
            ],
        },
        "features": [
            {
                "id": "sk1", "type": "sketch", "name": "Outline",
                "params": {"entities": [{"id": "e1", "type": "rect"}]},
            },
            {
                "id": "f1", "type": "extrude", "name": "PlateBody",
                "params": {"depth": {"value": 3.0, "unit": "mm"}, "wall": 3.0},
                "refs": {"sketch": "sk1"},
            },
        ],
        "bodies": [
            {"id": "b1", "name": "Body1", "owner_feature": "f1"},
        ],
    }


def _assembly() -> dict:
    return {
        "schema": "aicad.asm/1",
        "id": "uuid-asm-1",
        "name": "MainAsm",
        "components": [
            {
                "id": "k1", "name": "Bracket",
                "source": {"path": "Bracket.step+"},
                "transform": _ID, "fixed": True, "suppressed": False,
            },
            {
                "id": "k2", "name": "Plate",
                "source": {"path": "Plate.step+"},
                "transform": _ID, "fixed": False, "suppressed": False,
            },
            {
                "id": "k3", "name": "Bracket_2",
                "source": {"path": "Bracket.step+"},
                "transform": _ID, "fixed": False, "suppressed": False,
            },
        ],
        "mates": [
            {
                "id": "m1", "type": "coincident", "align": "aligned",
                "suppressed": False,
                "refs": [
                    {"component": "k1", "selector": {"face": 1}},
                    {"component": "k2", "selector": {"face": 2}},
                ],
            },
            {
                "id": "m2", "type": "concentric",
                "refs": [
                    {"component": "k1", "selector": {"edge": 1}},
                    {"component": "k2", "selector": {"edge": 1}},
                ],
            },
        ],
        "bom": [
            {"ref": "Bracket.step+", "quantity": 2},
            {"ref": "Plate.step+", "quantity": 1},
        ],
    }


@pytest.fixture
def project(tmp_path: Path) -> Path:
    _write(tmp_path, "Bracket.step+", _part_a())
    _write(tmp_path, "Plate.step+", _part_b())
    _write(tmp_path, "MainAsm.step+", _assembly())
    (tmp_path / "firmware").mkdir()
    (tmp_path / "firmware" / "platformio.ini").write_text("[env]\n")
    (tmp_path / "sim").mkdir()
    (tmp_path / "sim" / "static1").mkdir()
    (tmp_path / "sim" / "static1" / "study.json").write_text("{}")
    return tmp_path


# ---------------------------------------------------------------------------
# graph shape
# ---------------------------------------------------------------------------


def test_build_graph_nodes_and_edges(project: Path):
    g = cad_graph.build_graph(project, force=True)
    assert g["ok"] is True
    assert g["generated_at"]
    nodes = {n["id"]: n for n in g["nodes"]}
    edges = g["edges"]

    assert "project:root" in nodes
    assert nodes["project:root"]["kind"] == "project"

    part_nodes = [n for n in nodes.values() if n["kind"] == "part"]
    asm_nodes = [n for n in nodes.values() if n["kind"] == "assembly"]
    assert len(part_nodes) == 2
    assert len(asm_nodes) == 1
    assert {n["label"] for n in part_nodes} == {"Bracket", "Plate"}
    assert asm_nodes[0]["label"] == "MainAsm"

    for n in g["nodes"]:
        assert n["origin"] in ("extracted", "inferred"), n
        assert n["kind"] in cad_graph.NODE_KINDS, n
    for e in edges:
        assert e["origin"] in ("extracted", "inferred"), e
        assert e["kind"] in cad_graph.EDGE_KINDS, e

    feats = [n for n in nodes.values() if n["kind"] == "feature"]
    sketches = [n for n in nodes.values() if n["kind"] == "sketch"]
    assert len(feats) >= 3
    assert len(sketches) >= 2

    bodies = [n for n in nodes.values() if n["kind"] == "body"]
    assert len(bodies) >= 2

    mates = [n for n in nodes.values() if n["kind"] == "mate"]
    assert len(mates) == 2
    mate_edges = [e for e in edges if e["kind"] == "MATES"]
    assert len(mate_edges) >= 4

    inst_edges = [e for e in edges if e["kind"] == "INSTANCE_OF"]
    assert len(inst_edges) >= 3

    bom = [n for n in nodes.values() if n["kind"] == "bom_item"]
    assert len(bom) >= 2

    assert any(n["kind"] == "firmware" for n in nodes.values())
    assert any(n["kind"] == "sim_study" for n in nodes.values())

    dep = [e for e in edges if e["kind"] == "DEPENDS_ON"]
    assert dep, "expected feature dependency edges"

    refs = [e for e in edges if e["kind"] == "REFERENCES"]
    assert refs, "expected shared-dim REFERENCES edges"
    assert any(e.get("meta", {}).get("dim") in ("wall", "hole_d") for e in refs)

    cache = project / ".plm" / "graph.json"
    assert cache.is_file()


def test_get_graph_rpc_shape(project: Path):
    out = cad_graph.get_graph(project, force=True)
    assert out["ok"] is True
    assert isinstance(out["nodes"], list)
    assert isinstance(out["edges"], list)
    assert out["generated_at"]


def test_blast_radius_shared_dim(project: Path):
    g = cad_graph.build_graph(project, force=True)
    refs = [e for e in g["edges"] if e["kind"] == "REFERENCES"]
    assert refs, "need REFERENCES edges for blast test"
    src = refs[0]["src"]
    dst = refs[0]["dst"]
    out = cad_graph.blast_radius(src, g)
    assert out["ok"] is True
    assert dst in out["affected"], (
        f"blast of {src} should include co-referenced {dst}; got {out['affected']}"
    )

    sk_nodes = [n for n in g["nodes"]
                if n["kind"] == "sketch"
                and (n.get("path") or "").endswith("Bracket.step+")]
    assert sk_nodes
    sk_id = sk_nodes[0]["id"]
    blast = cad_graph.blast_radius(sk_id, g)
    assert blast["ok"]
    dep_feats = [
        e["src"] for e in g["edges"]
        if e["kind"] == "DEPENDS_ON" and e["dst"] == sk_id
    ]
    if dep_feats:
        assert any(f in blast["affected"] for f in dep_feats), blast["affected"]


def test_path_between_parts_via_assembly(project: Path):
    g = cad_graph.build_graph(project, force=True)
    parts = [n for n in g["nodes"] if n["kind"] == "part"]
    assert len(parts) == 2
    a, b = parts[0]["id"], parts[1]["id"]
    out = cad_graph.find_path(a, b, g)
    assert out["ok"] is True
    if out["path"]:
        assert out["path"][0] == a and out["path"][-1] == b


def test_cache_hit_and_mtime_invalidation(project: Path):
    g1 = cad_graph.build_graph(project, force=True)
    t1 = g1["generated_at"]
    g2 = cad_graph.build_graph(project, force=False)
    assert g2["generated_at"] == t1
    assert len(g2["nodes"]) == len(g1["nodes"])

    time.sleep(0.02)
    p = project / "Bracket.step+"
    p.write_bytes(p.read_bytes())
    now = time.time() + 1.0
    os.utime(p, (now, now))

    g3 = cad_graph.build_graph(project, force=False)
    assert g3["ok"] is True
    key = "file:Bracket.step+"
    assert key in g3.get("mtimes", {})
    assert g3["mtimes"][key] != g1["mtimes"][key] or g3["generated_at"] != t1


def test_open_docs_merge(tmp_path: Path):
    doc = {
        "schema": "aicad.part/1", "id": "open-1", "name": "Live",
        "features": [{"id": "f1", "type": "box", "name": "Box",
                      "params": {"w": 10}}],
    }
    g = cad_graph.build_graph(tmp_path, open_docs={"open-1": doc}, force=True)
    labels = {n["label"] for n in g["nodes"]}
    assert "Live" in labels
    assert any(n["kind"] == "feature" and n["label"] == "Box" for n in g["nodes"])


def test_on_ai_vc_checkpoint_hook(project: Path):
    cache = project / ".plm" / "graph.json"
    if cache.exists():
        cache.unlink()
    cad_graph.on_ai_vc_checkpoint(project_dir=str(project), doc_id="uuid-part-a")
    assert cache.is_file()
    cad_graph.on_ai_vc_checkpoint(project_dir="/no/such/path", doc_id=None)
    cad_graph.on_ai_vc_checkpoint(project_dir=None, doc_id="x")


def test_rpc_methods_registered():
    from engine_oracle.parts_hub.cad_graph_rpc import CAD_GRAPH_METHODS
    assert set(CAD_GRAPH_METHODS) == {
        "cad_graph_get", "cad_graph_blast", "cad_graph_path",
    }
    from engine_oracle.parts_hub import worker_rpc
    for name in CAD_GRAPH_METHODS:
        assert name in worker_rpc.WORKER_METHODS, name


def test_rpc_get_and_blast(project: Path):
    from engine_oracle.parts_hub.cad_graph_rpc import CAD_GRAPH_METHODS

    def _emit(*_a, **_k):
        pass

    out = CAD_GRAPH_METHODS["cad_graph_get"](
        {"project_dir": str(project), "force": True}, _emit)
    assert out["ok"] and out["nodes"] and out["edges"]

    nid = next(n["id"] for n in out["nodes"] if n["kind"] == "part")
    blast = CAD_GRAPH_METHODS["cad_graph_blast"](
        {"node_id": nid, "project_dir": str(project)}, _emit)
    assert blast["ok"] is True
    assert "affected" in blast

    path = CAD_GRAPH_METHODS["cad_graph_path"](
        {"src": nid, "dst": nid, "project_dir": str(project)}, _emit)
    assert path["ok"] and path["path"] == [nid]

    bad = CAD_GRAPH_METHODS["cad_graph_blast"]({}, _emit)
    assert bad["ok"] is False
