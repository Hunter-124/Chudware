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
import hashlib
import gzip
import os
import struct
import time
import zipfile
from pathlib import Path

import pytest

from services.plm import cad_graph
from services.plm import snapshot


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


def _graph_zip(entries: list[tuple[str, bytes]], compression=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def _graph_zip_u32(raw: bytes, field: int, value: int) -> bytes:
    """Patch document.json's central-directory field, without expanding a bomb."""
    data = bytearray(raw)
    central = data.index(b"PK\x01\x02")
    struct.pack_into("<I", data, central + field, value)
    return bytes(data)


@pytest.mark.parametrize("boundary", ["member", "ratio", "count"])
def test_container_admission_refuses_before_opening_members(tmp_path, monkeypatch, boundary):
    raw = _graph_zip([("document.json", b"{}")])
    if boundary == "member":
        raw = _graph_zip_u32(raw, 24, snapshot._MAX_MEMBER_BYTES + 1)
    elif boundary == "ratio":
        monkeypatch.setattr(snapshot, "_MAX_COMPRESSION_RATIO", 8)
        raw = _graph_zip([("document.json", b"{" + b" " * 4096 + b"}")])
    else:
        monkeypatch.setattr(snapshot, "_MAX_ZIP_ENTRIES", 3)
        raw = _graph_zip([("document.json", b"{}"), ("a", b""), ("b", b""), ("c", b"")])
    path = tmp_path / "hostile.step+"
    path.write_bytes(raw)
    opened = []

    def forbidden_open(self, member, *args, **kwargs):
        opened.append(member)
        raise AssertionError("member must not be inflated before admission")

    monkeypatch.setattr(zipfile.ZipFile, "open", forbidden_open)
    assert cad_graph._read_container(path) == (None, [], None)
    assert opened == []


@pytest.mark.parametrize("names", [
    ["DOCUMENT.JSON"],
    ["document.json", "DOCUMENT.JSON"],
    ["document.json", "document.json"],
])
def test_container_exact_document_name_and_unique_members(tmp_path, names):
    entries = [(name, b"{}") for name in names]
    if names == ["document.json", "document.json"]:
        with pytest.warns(UserWarning, match="Duplicate name"):
            raw = _graph_zip(entries)
    else:
        raw = _graph_zip(entries)
    path = tmp_path / "alias.step+"
    path.write_bytes(raw)
    assert cad_graph._read_container(path) == (None, [], None)


@pytest.mark.parametrize("corruption", ["understated", "crc", "truncated"])
def test_container_corrupt_streams_keep_invalid_result_semantics(tmp_path, corruption):
    raw = _graph_zip([("document.json", b'{"name":"' + b"a" * 4096 + b'"}')])
    if corruption == "understated":
        raw = _graph_zip_u32(raw, 24, 1)
    elif corruption == "crc":
        raw = _graph_zip_u32(raw, 16, 0)
    else:
        raw = raw[:-12]
    path = tmp_path / "corrupt.ecad+"
    path.write_bytes(raw)
    assert cad_graph._read_container(path) == (None, [], None)


def test_container_streaming_preserves_document_hash_studies_and_graph(tmp_path, monkeypatch):
    doc = _part_a()
    doc_bytes = json.dumps(doc, indent=2).encode("utf-8")
    path = tmp_path / "Bracket.step+"
    path.write_bytes(_graph_zip([
        ("document.json", doc_bytes),
        ("geometry.step", b" " * 65536),
        ("cache.brep", b" " * 65536),
        ("studies/static.json", b"{}"),
        ("studies/nested/result.json", b"{}"),
        ("studies/thermal.json", b"{}"),
    ]))
    monkeypatch.setattr(snapshot, "_MAX_MEMBER_BYTES", len(doc_bytes))
    monkeypatch.setattr(snapshot, "_READ_CHUNK_BYTES", 64)
    requests = []
    opened = []
    real_read = zipfile.ZipExtFile.read
    real_open = zipfile.ZipFile.open

    def bounded_read(self, n=-1):
        requests.append(n)
        assert 0 < n <= 64
        return real_read(self, n)

    def recipe_only_open(self, member, *args, **kwargs):
        name = member.filename if isinstance(member, zipfile.ZipInfo) else member
        opened.append(name)
        assert name == "document.json"
        return real_open(self, member, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", bounded_read)
    monkeypatch.setattr(zipfile.ZipFile, "open", recipe_only_open)
    parsed, studies, content_hash = cad_graph._read_container(path)
    assert parsed == doc
    assert studies == ["static", "thermal"]
    assert content_hash == hashlib.blake2b(doc_bytes, digest_size=8).hexdigest()
    assert len(requests) > 1
    assert opened == ["document.json"]
    graph = cad_graph.build_graph(tmp_path, force=True)
    assert graph["ok"] is True
    assert "Bracket" in {node["label"] for node in graph["nodes"]}
    assert any(node["kind"] == "feature" and node["label"] == "Base" for node in graph["nodes"])


def test_container_actual_stream_budget_refuses_without_partial_graph(tmp_path, monkeypatch):
    doc = _part_a()
    _write(tmp_path, "Bracket.step+", doc)
    budget = max(2048, len(json.dumps(doc, indent=2).encode("utf-8")))
    raw = _graph_zip([("document.json", b'{"name":"' + b"b" * (2 * budget) + b'"}')])
    # The claimed size fits, but the real compressed member does not. Keep the
    # CRC honest so zipfile must refuse the dishonest stream rather than parse it.
    raw = _graph_zip_u32(raw, 24, budget // 2)
    (tmp_path / "Bomb.step+").write_bytes(raw)
    monkeypatch.setattr(snapshot, "_MAX_MEMBER_BYTES", budget)
    monkeypatch.setattr(snapshot, "_READ_CHUNK_BYTES", 128)
    requests = []
    real_read = zipfile.ZipExtFile.read

    def bounded_read(self, n=-1):
        requests.append(n)
        assert 0 < n <= 128
        return real_read(self, n)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", bounded_read)
    assert cad_graph._read_container(tmp_path / "Bomb.step+") == (None, [], None)
    assert requests
    graph = cad_graph.build_graph(tmp_path, force=True)
    assert graph["ok"] is True
    labels = {node["label"] for node in graph["nodes"]}
    assert "Bracket" in labels
    assert "Bomb" not in labels
    assert not any(node.get("path") == "Bomb.step+" for node in graph["nodes"])


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

    cache = project / ".plm" / "graph.json.gz"
    assert cache.is_file()


def test_get_graph_rpc_shape(project: Path):
    out = cad_graph.get_graph(project, force=True)
    assert out["ok"] is True
    assert isinstance(out["nodes"], list)
    assert isinstance(out["edges"], list)
    assert out["generated_at"]
    assert out["truncated"] is False
    assert out["node_count"] == len(out["nodes"])
    assert out["edge_count"] == len(out["edges"])
    assert isinstance(out.get("filter"), dict)
    assert out["filter"]["include_archive"] is False


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
    cache = project / ".plm" / "graph.json.gz"
    if cache.exists():
        cache.unlink()
    cad_graph.on_ai_vc_checkpoint(project_dir=str(project), doc_id="uuid-part-a")
    assert cache.is_file()
    cad_graph.on_ai_vc_checkpoint(project_dir="/no/such/path", doc_id=None)
    cad_graph.on_ai_vc_checkpoint(project_dir=None, doc_id="x")


def test_rpc_methods_registered():
    from services.plm.worker_rpc import CAD_GRAPH_METHODS
    assert set(CAD_GRAPH_METHODS) == {
        "cad_graph_get", "cad_graph_blast", "cad_graph_path",
        "cad_graph_link_add", "cad_graph_link_remove", "cad_graph_link_list",
        "cad_graph_assembly_tree",
        "cad_graph_keyword_add", "cad_graph_keyword_remove",
        "cad_graph_keyword_list",
        "cad_graph_search", "cad_graph_neighbors",
        "cad_graph_watch_add", "cad_graph_watch_remove",
        "cad_graph_watch_list", "cad_graph_changes",
        "cad_graph_config_get", "cad_graph_config_set",
    }
    from services.plm import worker_rpc
    for name in CAD_GRAPH_METHODS:
        assert name in worker_rpc.WORKER_METHODS, name


def test_rpc_get_and_blast(project: Path):
    from services.plm.worker_rpc import CAD_GRAPH_METHODS

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


# ---------------------------------------------------------------------------
# 1A — expression-driven links, equation nodes, mate values, cross-doc derive
# ---------------------------------------------------------------------------


def _expr_doc() -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "11111111-1111-4111-8111-111111111111",
        "name": "P",
        "equations": {"globals": [
            {"name": "W", "expr": "6", "value": 6.0},
            {"name": "W2", "expr": "W*2", "value": 12.0},
        ]},
        "features": [
            {"id": "f1", "type": "box",
             "params": {"width": {"value": 12, "expr": "W2"}}},
        ],
    }


def test_equation_nodes_and_expr_links():
    g = cad_graph.build_graph(None, open_docs={"d1": _expr_doc()}, force=True)
    nodes = {n["id"]: n for n in g["nodes"]}
    eqs = [n for n in nodes.values() if n["kind"] == "equation"]
    assert {n["label"] for n in eqs} == {"W", "W2"}, eqs
    # feature width expr "W2" → DERIVES_FROM the W2 equation node
    df = [e for e in g["edges"] if e["kind"] == "DERIVES_FROM"]
    assert any(e["meta"].get("dim", "").endswith("width")
               and nodes[e["dst"]]["label"] == "W2"
               and e["origin"] == "extracted" for e in df), df
    # W2 expr "W*2" → DERIVES_FROM the W equation node
    assert any(nodes[e["src"]]["label"] == "W2"
               and nodes[e["dst"]]["label"] == "W"
               and e["meta"].get("via") == "equation" for e in df), df


def test_doc_meta_units_and_material():
    """meta.un normalizes doc.units.system; meta.mat carries the doc's
    material.key (doc_schema.md §0/§1). Both are optional and omitted when
    the source doc has no usable value."""
    ips_doc = {
        "schema": "aicad.part/1",
        "id": "33333333-3333-4333-8333-333333333333",
        "name": "MatPart",
        "units": {"system": "IPS"},
        "material": {"key": "alu_6061"},
        "features": [],
    }
    g = cad_graph.build_graph(None, open_docs={"d1": ips_doc}, force=True)
    node = next(n for n in g["nodes"] if n["kind"] == "part")
    assert node["meta"]["un"] == "ips"
    assert node["meta"]["mat"] == ["alu_6061"]

    mmgs_null_mat_doc = {
        "schema": "aicad.part/1",
        "id": "44444444-4444-4444-8444-444444444444",
        "name": "NoMatPart",
        "units": {"system": "MMGS"},
        "material": None,
        "features": [],
    }
    g2 = cad_graph.build_graph(None, open_docs={"d2": mmgs_null_mat_doc}, force=True)
    node2 = next(n for n in g2["nodes"] if n["kind"] == "part")
    assert node2["meta"]["un"] == "mmgs"
    assert "mat" not in node2["meta"]  # null material.key → omitted, not []

    # no units/material fields at all → both keys absent from meta
    g3 = cad_graph.build_graph(None, open_docs={"d3": _expr_doc()}, force=True)
    node3 = next(n for n in g3["nodes"] if n["kind"] == "part")
    assert "un" not in node3["meta"]
    assert "mat" not in node3["meta"]


def test_mate_value_edge_meta():
    asm = {
        "schema": "aicad.asm/1", "id": "22222222-2222-4222-8222-222222222222",
        "name": "A",
        "components": [
            {"id": "k1", "name": "x", "source": {"path": "x.step+"}},
            {"id": "k2", "name": "y", "source": {"path": "y.step+"}},
        ],
        "mates": [
            {"id": "m1", "type": "distance",
             "value": {"value": 25, "expr": None},
             "refs": [{"component": "k1"}, {"component": "k2"}]},
        ],
    }
    g = cad_graph.build_graph(None, open_docs={"d1": asm}, force=True)
    mate = next(n for n in g["nodes"] if n["kind"] == "mate")
    assert mate["meta"]["value"] == 25
    medges = [e for e in g["edges"] if e["kind"] == "MATES"]
    assert medges and all(e["meta"].get("value") == 25 for e in medges), medges


def test_cross_doc_derives_from_resolved():
    doc_a = {
        "schema": "aicad.part/1", "id": "aaaaaaaa-1111-4111-8111-111111111111",
        "name": "Master",
        "features": [{"id": "f1", "type": "box", "params": {"w": 10}}],
    }
    doc_b = {
        "schema": "aicad.part/1", "id": "bbbbbbbb-2222-4222-8222-222222222222",
        "name": "Derived",
        "features": [{"id": "f1", "type": "box", "params": {"w": 10},
                      "derived_from": {
                          "doc_id": "aaaaaaaa-1111-4111-8111-111111111111",
                          "feature": "f1"}}],
    }
    g = cad_graph.build_graph(
        None, open_docs={"a": doc_a, "b": doc_b}, force=True)
    nodes = {n["id"]: n for n in g["nodes"]}
    df = [e for e in g["edges"] if e["kind"] == "DERIVES_FROM"
          and e["meta"].get("via") == "derived_from"]
    assert df, [e for e in g["edges"] if e["kind"] == "DERIVES_FROM"]
    assert any(nodes[e["dst"]]["label"] == "Master" for e in df), df


def test_shared_value_edges_now_inferred(project: Path):
    g = cad_graph.build_graph(project, force=True)
    shared = [e for e in g["edges"] if e["kind"] == "REFERENCES"
              and e["meta"].get("via") == "shared_value"]
    assert shared, "expected shared-value REFERENCES edges"
    assert all(e["origin"] == "inferred" for e in shared), shared


# ---------------------------------------------------------------------------
# 1D — assembly_advice best-practice advisor
# ---------------------------------------------------------------------------


def _adv_graph() -> dict:
    nodes = [{"id": "project:root", "kind": "project", "label": "p"}]
    edges = []

    def N(nid, kind, label):
        nodes.append({"id": nid, "kind": kind, "label": label})

    def E(s, d, k):
        edges.append({"src": s, "dst": d, "kind": k, "meta": {},
                      "origin": "extracted"})

    # assembly A: 5 components, 3 tightly mated (i1,i2,i3), 2 loose
    N("doc:A", "assembly", "MainAsm")
    for i in range(1, 6):
        N(f"instance:doc:A:i{i}", "doc", f"Comp{i}")
        E("doc:A", f"instance:doc:A:i{i}", "CONTAINS")
    for mi, (a, b) in enumerate([(1, 2), (2, 3), (1, 3)], start=1):
        N(f"mate:doc:A:m{mi}", "mate", "coincident")
        E("doc:A", f"mate:doc:A:m{mi}", "CONTAINS")
        E(f"mate:doc:A:m{mi}", f"instance:doc:A:i{a}", "MATES")
        E(f"mate:doc:A:m{mi}", f"instance:doc:A:i{b}", "MATES")
    # flat assembly B: 13 components, no mates
    N("doc:B", "assembly", "FlatAsm")
    for i in range(1, 14):
        N(f"instance:doc:B:j{i}", "doc", f"Part{i}")
        E("doc:B", f"instance:doc:B:j{i}", "CONTAINS")
    # multi-body part C: 3 bodies
    N("doc:C", "part", "Chunk")
    for i in range(1, 4):
        N(f"body:doc:C:b{i}", "body", f"Body{i}")
        E("doc:C", f"body:doc:C:b{i}", "CONTAINS")
    return {"nodes": nodes, "edges": edges}


def test_assembly_advice_suggestions():
    out = cad_graph.assembly_advice(_adv_graph())
    assert out["ok"] is True
    kinds = {s["kind"] for s in out["suggestions"]}
    assert "extract_subassembly" in kinds, out
    assert "group_into_subassemblies" in kinds, out
    assert "split_multibody" in kinds, out
    ex = next(s for s in out["suggestions"]
              if s["kind"] == "extract_subassembly")
    assert len(ex["components"]) == 3
    assert out["narrative"]


def test_assembly_advice_clean():
    g = {"nodes": [{"id": "doc:A", "kind": "assembly", "label": "A"}],
         "edges": []}
    out = cad_graph.assembly_advice(g)
    assert out["ok"] and out["suggestions"] == []


# ---------------------------------------------------------------------------
# Large-tree scale: archive exclusion, truncation, cache fast-path
# ---------------------------------------------------------------------------


def test_archive_dirs_excluded_by_default(tmp_path: Path):
    """_archive_stale / archive / .trash never contribute nodes by default."""
    live = {
        "schema": "aicad.part/1", "id": "live-1", "name": "Live",
        "features": [], "bodies": [], "equations": {"globals": []},
    }
    stale = {
        "schema": "aicad.part/1", "id": "stale-1", "name": "Stale",
        "features": [], "bodies": [], "equations": {"globals": []},
    }
    _write(tmp_path, "Live.step+", live)
    arch = tmp_path / "_archive_stale" / "old"
    arch.mkdir(parents=True)
    (arch / "Stale.step+").write_bytes(_container(stale))
    trash = tmp_path / ".trash"
    trash.mkdir()
    (trash / "Gone.step+").write_bytes(_container(stale))

    g = cad_graph.build_graph(tmp_path, force=True)
    labels = {n["label"] for n in g["nodes"]}
    assert "Live" in labels
    assert "Stale" not in labels
    assert g["filter"]["include_archive"] is False
    assert "_archive_stale" in g["filter"]["exclude_dirs"]

    # include_archive keeps them and tags meta.archive
    g2 = cad_graph.build_graph(tmp_path, force=True, include_archive=True)
    labels2 = {n["label"] for n in g2["nodes"]}
    assert "Stale" in labels2
    stale_nodes = [n for n in g2["nodes"]
                   if n.get("path") and "_archive_stale" in n["path"]]
    assert stale_nodes
    assert all((n.get("meta") or {}).get("archive") for n in stale_nodes
               if n["kind"] in ("part", "assembly", "doc", "drawing", "board"))


def test_nested_aicadvault_skipped_working_tree_kept(tmp_path: Path):
    """Nested .aicadvault is pruned; CAD files beside it still graph."""
    nested = tmp_path / "sub_vault"
    nested.mkdir()
    (nested / ".aicadvault").mkdir()
    # decoy inside the shadow tree — must NOT appear
    decoy = {
        "schema": "aicad.part/1", "id": "decoy", "name": "Decoy",
        "features": [], "bodies": [], "equations": {"globals": []},
    }
    (nested / ".aicadvault" / "Decoy.step+").write_bytes(_container(decoy))
    # real working-tree part beside the shadow dir — MUST appear
    real = {
        "schema": "aicad.part/1", "id": "real", "name": "RealNested",
        "features": [], "bodies": [], "equations": {"globals": []},
    }
    (nested / "RealNested.step+").write_bytes(_container(real))

    g = cad_graph.build_graph(tmp_path, force=True)
    labels = {n["label"] for n in g["nodes"]}
    assert "RealNested" in labels
    assert "Decoy" not in labels


def test_get_graph_truncation_caps(project: Path):
    """Honest truncated:true + counts when max_nodes/max_edges bind."""
    full = cad_graph.get_graph(project, force=True, max_nodes=0, max_edges=0)
    assert full["truncated"] is False
    n_full = full["node_count"]
    assert n_full >= 3

    tiny = cad_graph.get_graph(project, force=False, max_nodes=2, max_edges=1)
    assert tiny["truncated"] is True
    assert tiny["node_count"] == n_full
    assert len(tiny["nodes"]) == 2
    assert len(tiny["edges"]) <= 1
    # every returned edge endpoints exist in the truncated node set
    ids = {n["id"] for n in tiny["nodes"]}
    for e in tiny["edges"]:
        assert e["src"] in ids and e["dst"] in ids


def test_cache_fast_path_serves_without_rescan(project: Path):
    """Warm cache with matching filter is served on the next non-force build."""
    g1 = cad_graph.build_graph(project, force=True)

    g2 = cad_graph.build_graph(project, force=False)
    assert g2["nodes"] == g1["nodes"]
    assert g2["edges"] == g1["edges"]
    assert g2["filter"] == g1["filter"]

    # flipping include_archive must miss the default cache
    g3 = cad_graph.build_graph(project, force=False, include_archive=True)
    assert g3["filter"]["include_archive"] is True


def test_path_is_archive_helper():
    assert cad_graph.path_is_archive("_archive_stale/foo/Bar.step+")
    assert cad_graph.path_is_archive("a/archive/b.step+")
    assert cad_graph.path_is_archive(".trash/x.step+")
    assert not cad_graph.path_is_archive("01_Design/CAD_Build/Part.step+")
    assert not cad_graph.path_is_archive(None)


@pytest.mark.parametrize("limit", ["MAX_CACHE_BYTES", "MAX_CACHE_EXPANDED_BYTES"])
def test_oversized_cache_bypassed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit):
    """Stored and expanded byte limits reject an unsafe cache without losing the graph."""
    _write(tmp_path, "Part.step+", {
        "schema": "aicad.part/1", "id": "p-oversized", "name": "Part",
        "features": [{"id": "f1", "type": "box", "name": "Box",
                      "params": {"w": 1}}],
    })
    plm = tmp_path / ".plm"
    plm.mkdir()
    bloated = plm / "graph.json.gz"
    bloated.write_bytes(gzip.compress(json.dumps({"nodes": [{"id": "unsafe"}],
                                                "pad": "x" * 1000}).encode()))
    monkeypatch.setattr(cad_graph, limit, 50)
    assert cad_graph.load_cached_graph(tmp_path) is None

    g = cad_graph.build_graph(tmp_path, force=False)
    assert g["ok"] is True
    assert any(n["kind"] == "part" for n in g["nodes"])


def test_corrupt_deflate_cache_rebuilds_project_graph(project: Path):
    expected = cad_graph.build_graph(project, force=True)
    # Valid gzip header followed by a reserved DEFLATE block type: zlib.error,
    # not gzip.BadGzipFile, EOFError or JSONDecodeError.
    (project / ".plm" / "graph.json.gz").write_bytes(
        bytes.fromhex("1f8b0800000000000003") + b"\x07" + b"\x00" * 8)
    assert cad_graph.load_cached_graph(project) is None
    rebuilt = cad_graph.build_graph(project)
    assert rebuilt["nodes"] == expected["nodes"]
    assert rebuilt["edges"] == expected["edges"]


def test_cache_fingerprint_includes_capped_files_and_unreadable_containers(tmp_path, monkeypatch):
    monkeypatch.setattr(cad_graph, "MAX_FILE_NODES", 2)
    for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
        (tmp_path / name).write_text(name)
    (tmp_path / "Broken.step+").write_bytes(b"not yet a valid container")
    graph = cad_graph.build_graph(tmp_path)
    assert graph["files_truncated"] == 2
    cached = cad_graph._load_cache_if_valid(tmp_path)
    assert cached is not None
    assert cached["nodes"] == graph["nodes"]
    assert cached["edges"] == graph["edges"]
    # An old-mtime addition still invalidates and changes the visible capped set.
    stamp = (tmp_path / "a.txt").stat()
    (tmp_path / "0.txt").write_text("new first file")
    os.utime(tmp_path / "0.txt", ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    changed = cad_graph.build_graph(tmp_path)
    assert "file:0.txt" in {node["id"] for node in changed["nodes"]}
    assert changed["files_truncated"] == 3


def test_restoring_container_permission_invalidates_cache_and_change_probe(tmp_path, monkeypatch):
    _write(tmp_path, "Part.step+", {
        "schema": "aicad.part/1", "id": "permission-part", "name": "Part",
        "features": [{"id": "f1", "type": "box", "params": {"w": 1}}],
    })
    path = tmp_path / "Part.step+"
    real_read = cad_graph._read_container

    def permission_aware_read(candidate):
        # Deterministic even when the test runner can bypass filesystem permissions.
        if candidate.stat().st_mode & 0o400 == 0:
            return None, [], None
        return real_read(candidate)

    monkeypatch.setattr(cad_graph, "_read_container", permission_aware_read)
    path.chmod(0o000)
    try:
        before = cad_graph.get_graph(tmp_path)
        assert not any(node["kind"] == "part" for node in before["nodes"])
        stamp = path.stat()
        assert cad_graph._load_cache_if_valid(tmp_path) is not None
        path.chmod(0o600)
        assert path.stat().st_mtime_ns == stamp.st_mtime_ns
        assert cad_graph._load_cache_if_valid(tmp_path) is None
        after = cad_graph.get_graph(
            tmp_path, if_mtimes_max=before["sources_mtime_max"],
            if_sources_count=before["sources_count"])
        assert after.get("unchanged") is not True
        assert "doc:Part.step+" in {node["id"] for node in after["nodes"]}
        assert after["sources_mtime_max"] == before["sources_mtime_max"]
        assert after["sources_count"] == before["sources_count"]
    finally:
        path.chmod(0o600)


def test_meta_bounding_caps():
    """Pathological feature/mate meta is depth/list/string-capped in the graph."""
    long_s = "Z" * 400
    deep = {"a": {"b": {"c": {"d": {"e": {"f": "too-deep"}}}}}}
    long_list = [{"i": i, "s": long_s} for i in range(20)]
    doc = {
        "schema": "aicad.asm/1",
        "id": "bbbbbbbb-2222-4222-8222-222222222222",
        "name": "FatAsm",
        "equations": {
            "globals": [
                {"name": "L", "expr": long_s, "value": 1.0, "unit": "mm"},
            ],
        },
        "features": [
            {
                "id": "f1", "type": "extrude", "name": "FatFeat",
                "params": {
                    "depth": {"value": 1.0, "unit": "mm"},
                    "note": long_s,
                    "blob": deep,
                    "tags": long_list,
                },
            },
        ],
        "components": [
            {"id": "k1", "name": "C1", "source": {"path": "X.step+"},
             "transform": _ID},
        ],
        "mates": [
            {
                "id": "m1", "type": "distance",
                "value": 10.0, "unit": "mm",
                "refs": long_list,
                "extra": deep,
            },
        ],
    }
    g = cad_graph.build_graph(None, open_docs={"d1": doc}, force=True)
    assert g["ok"] is True

    def _check(v: object, depth: int = 0) -> None:
        assert depth <= 5, f"depth exceeded at {v!r}"
        if isinstance(v, str):
            # 256 chars + optional ellipsis
            assert len(v) <= 257, f"string too long: {len(v)}"
        elif isinstance(v, dict):
            assert len(v) <= 16
            for vv in v.values():
                _check(vv, depth + 1)
        elif isinstance(v, list):
            assert len(v) <= 8
            for vv in v:
                _check(vv, depth + 1)

    for n in g["nodes"]:
        _check(n.get("meta") or {})
    for e in g["edges"]:
        _check(e.get("meta") or {})

    # equation expr must be truncated
    eqs = [n for n in g["nodes"] if n["kind"] == "equation"]
    assert eqs
    expr = eqs[0]["meta"].get("expr")
    assert isinstance(expr, str)
    assert len(expr) <= 257
    assert expr.endswith("…")

    mates = [n for n in g["nodes"] if n["kind"] == "mate"]
    assert mates
    mrefs = mates[0]["meta"].get("refs")
    if isinstance(mrefs, list):
        assert len(mrefs) <= 8


# ---------------------------------------------------------------------------
# file nodes + keyword web + cross-project (2026-07-27 addendum)
# ---------------------------------------------------------------------------


def test_file_nodes_and_extension_policy(project: Path):
    (project / "Specs").mkdir()
    (project / "Specs" / "datasheet.pdf").write_bytes(b"%PDF-1.4 fake")
    (project / "notes.md").write_text("# notes\n")
    (project / "build.log").write_text("not a graph file\n")
    (project / "firmware" / "main.c").write_text("int main;\n")  # not in _FILE_EXTS
    g = cad_graph.build_graph(project, force=True)
    nodes = {n["id"]: n for n in g["nodes"]}
    fids = [nid for nid in nodes if nodes[nid]["kind"] == "file"]
    assert "file:Specs/datasheet.pdf" in fids
    assert "file:notes.md" in fids
    assert not any("build.log" in f or "main.c" in f for f in fids)
    pdf = nodes["file:Specs/datasheet.pdf"]
    assert pdf["meta"]["ext"] == "pdf"
    assert pdf["meta"]["folder"] == "Specs"
    assert pdf["meta"]["size_bytes"] > 0
    assert "text_preview" not in pdf["meta"]  # office_content default off
    # every node carries its owning project root
    for n in g["nodes"]:
        assert (n.get("meta") or {}).get("project") == str(project)
    # project CONTAINS file nodes
    assert any(e["src"] == "project:root"
               and e["dst"] == "file:notes.md" and e["kind"] == "CONTAINS"
               for e in g["edges"])


def test_file_node_cap(project: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cad_graph, "MAX_FILE_NODES", 2)
    for i in range(5):
        (project / f"f{i}.md").write_text(f"# {i}\n")
    g = cad_graph.build_graph(project, force=True)
    files = [n for n in g["nodes"] if n["kind"] == "file"]
    assert len(files) == 2
    assert g.get("files_truncated") == 3


def test_keyword_overlay_merges(project: Path):
    from services.plm import keywords as kw
    (project / "readme.md").write_text("# r\n")
    r1 = kw.add_keyword(project, "readme.md", "Docs")           # by path
    r2 = kw.add_keyword(project, "doc:Bracket.step+", "load-bearing",
                        origin="ai")                            # by node id
    assert r1["ok"] and r2["ok"]
    kw.add_keyword(project, "no/such.node", "ghost")            # dropped endpoint
    g = cad_graph.get_graph(project, force=True)
    nodes = {n["id"]: n for n in g["nodes"]}
    assert "kw:docs" in nodes and nodes["kw:docs"]["kind"] == "keyword"
    assert "kw:load-bearing" in nodes
    assert "kw:ghost" not in nodes
    tagged = [(e["src"], e["dst"], e["origin"]) for e in g["edges"]
              if e["kind"] == "TAGGED"]
    assert ("kw:docs", "file:readme.md", "manual") in tagged
    assert ("kw:load-bearing", "doc:Bracket.step+", "ai") in tagged
    assert nodes["file:readme.md"]["meta"]["keywords"] == ["docs"]
    assert nodes["doc:Bracket.step+"]["meta"]["keywords"] == ["load-bearing"]


def test_office_content_gated_extraction(project: Path):
    from services.plm import graph_config as gcfg
    (project / "BRK-0042.pdf").write_bytes(b"%PDF-1.4 fake")
    (project / "table.csv").write_text("part,qty\nBRK-0042,4\n")
    # off by default: no preview, no mention edges
    g = cad_graph.build_graph(project, force=True)
    csv_node = {n["id"]: n for n in g["nodes"]}["file:table.csv"]
    assert "text_preview" not in csv_node["meta"]
    assert not any((e.get("meta") or {}).get("via") == "office_mention"
                   for e in g["edges"])
    # on: csv extracts (stdlib), mention edge resolves by part-number stem
    r = gcfg.set_config(project, office_content=True)
    assert r["ok"]
    g = cad_graph.get_graph(project)   # config mtime invalidates cache
    nodes = {n["id"]: n for n in g["nodes"]}
    csv_node = nodes["file:table.csv"]
    assert csv_node["meta"].get("text_preview")
    assert csv_node["meta"].get("word_count") > 0
    mentions = [e for e in g["edges"]
                if (e.get("meta") or {}).get("via") == "office_mention"]
    assert any(e["src"] == "file:table.csv"
               and e["dst"] == "file:BRK-0042.pdf"
               and e["origin"] == "inferred"
               and e["meta"].get("term") == "brk-0042" for e in mentions)


def test_graph_diff(project: Path):
    g1 = cad_graph.build_graph(project, force=True)
    (project / "new.md").write_text("# new\n")
    g2 = cad_graph.build_graph(project, force=True)
    d = cad_graph.graph_diff(g1, g2)
    added = {n["id"] for n in d["nodes_added"]}
    removed = {n["id"] for n in d["nodes_removed"]}
    assert added == {"file:new.md"}
    assert removed == set()
    assert any(e["dst"] == "file:new.md" for e in d["edges_added"])
    d_same = cad_graph.graph_diff(g2, g2)
    assert not d_same["nodes_added"] and not d_same["edges_added"]


def test_search_and_neighbors(project: Path):
    from services.plm import keywords as kw
    kw.add_keyword(project, "doc:Plate.step+", "sheet-metal")
    g = cad_graph.get_graph(project, force=True)
    s = cad_graph.search_graph(g, q="plate")
    assert any(n["id"] == "doc:Plate.step+" for n in s["nodes"])
    s2 = cad_graph.search_graph(g, kinds=["part"], keyword="sheet-metal")
    assert [n["id"] for n in s2["nodes"]] == ["doc:Plate.step+"]
    s3 = cad_graph.search_graph(g, q="zzz-no-hit")
    assert s3["nodes"] == [] and s3["edges"] == []
    nb = cad_graph.neighbors(g, "doc:Plate.step+", depth=1)
    assert nb["ok"] and any(n["id"] == "kw:sheet-metal" for n in nb["nodes"])
    missing = cad_graph.neighbors(g, "doc:nope", depth=1)
    assert missing["ok"] is False and missing["reason"] == "not_found"


def test_cross_project_merge(tmp_path: Path):
    from services.plm import graph_config as gcfg
    from services.plm import keywords as kw
    pa = tmp_path / "projA"
    pb = tmp_path / "projB"
    pa.mkdir()
    pb.mkdir()
    _write(pa, "Bracket.step+", _part_a())
    _write(pb, "Other.step+", _part_b())
    r = gcfg.add_linked_project(pa, str(pb))
    assert r["ok"]
    kw.add_keyword(pa, "doc:Bracket.step+", "shared-tag")
    kw.add_keyword(pb, "doc:Other.step+", "shared-tag")
    g = cad_graph.get_graph(pa, linked=True)
    assert g["ok"]
    assert g.get("linked_projects") == [str(pb)]
    nodes = {n["id"]: n for n in g["nodes"]}
    # shared keyword merges into ONE node spanning both projects
    assert "kw:shared-tag" in nodes
    tagged = [(e["src"], e["dst"]) for e in g["edges"] if e["kind"] == "TAGGED"]
    assert ("kw:shared-tag", "doc:Bracket.step+") in tagged
    assert ("kw:shared-tag", "doc:Other.step+") in tagged
    # colliding project node from projB got prefixed
    bkey = gcfg.project_key(pb)
    assert f"{bkey}/project:root" in nodes
    projs = {(n.get("meta") or {}).get("project") for n in g["nodes"]}
    assert projs == {str(pa), str(pb)}
    # single-project graph unchanged (no linked key, no merged nodes)
    g_single = cad_graph.get_graph(pa)
    assert "linked_projects" not in g_single
    assert not any(n["id"].startswith(f"{bkey}/") for n in g_single["nodes"])


def test_qualified_overlay_link_resolves(tmp_path: Path):
    from services.plm import graph_config as gcfg
    from services.plm import graph_links as gl
    pa = tmp_path / "projA"
    pb = tmp_path / "projB"
    pa.mkdir()
    pb.mkdir()
    _write(pa, "Bracket.step+", _part_a())
    _write(pb, "Other.step+", _part_b())
    gcfg.add_linked_project(pa, str(pb))
    bkey = gcfg.project_key(pb)
    r = gl.add_link(pa, "doc:Bracket.step+", f"{bkey}/doc:Other.step+",
                    "REFERENCES", note="cross-project fit", origin="ai")
    assert r["ok"]
    # invisible in the single-project graph (endpoint not local)
    g_single = cad_graph.get_graph(pa)
    assert not any((e.get("meta") or {}).get("note") == "cross-project fit"
                   for e in g_single["edges"])
    # resolved in the merged graph
    g = cad_graph.get_graph(pa, linked=True)
    cross = [e for e in g["edges"]
             if (e.get("meta") or {}).get("note") == "cross-project fit"]
    assert len(cross) == 1
    assert cross[0]["src"] == "doc:Bracket.step+"
    assert cross[0]["dst"] == "doc:Other.step+"
    assert cross[0]["origin"] == "ai"


def test_invalidate_keeps_prev_baseline(project: Path):
    g1 = cad_graph.get_graph(project)  # warm cache
    assert cad_graph.load_cached_graph(project) is not None
    cad_graph.invalidate_cache(project)
    prev = project / ".plm" / "graph.prev.json.gz"
    assert prev.is_file()
    assert not (project / ".plm" / "graph.json.gz").exists()
    old = cad_graph.load_cached_graph(project)
    assert old is not None
    assert len(old.get("nodes") or []) == len(g1["nodes"])
    (project / "delta.md").write_text("# d\n")
    g2 = cad_graph.get_graph(project, force=True)
    d = cad_graph.graph_diff(old, g2)
    assert "file:delta.md" in {n["id"] for n in d["nodes_added"]}


def _source_mtimes_reference(root: Path, *, skip_names, source_states=None):
    """The pre-optimisation pathlib scan, kept verbatim as the equality oracle."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_names]
        for fn in filenames:
            lower = fn.lower()
            if not lower.endswith((".step+", ".ecad+")):
                ext = lower.rsplit(".", 1)[-1] if "." in lower else ""
                if not ext or f".{ext}" not in cad_graph._FILE_EXTS:
                    continue
            fp = Path(dirpath) / fn
            try:
                rel = str(fp.relative_to(root)).replace("\\", "/")
                st = fp.stat()
            except OSError:
                continue
            out[f"file:{rel}"] = st.st_mtime
            if source_states is not None:
                source_states[f"file:{rel}"] = cad_graph._source_state(st)
    om = cad_graph._overlay_mtime(root, source_states)
    if om > 0:
        out["overlay:graph_links"] = om
    return out


@pytest.mark.parametrize("relative_root", [False, True])
def test_source_mtimes_fast_matches_pathlib_scan(tmp_path, monkeypatch, relative_root):
    for rel in ("a.txt", "Top.step+", "sub/b.md", "sub/deep/er/c.txt",
                "sub/deep/Board.ecad+", "sp ace/d.txt", "noext", "sub/skip.bin",
                "node_modules/x.txt", "sub/.git/y.txt", "_archive/old.txt"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(rel)
    (tmp_path / ".plm").mkdir(exist_ok=True)
    (tmp_path / ".plm" / "keywords.json").write_text("{}")
    if relative_root:
        monkeypatch.chdir(tmp_path)
        root = Path(".")
    else:
        root = tmp_path
    for include_archive in (False, True):
        skip = cad_graph._skip_dir_names(include_archive=include_archive)
        want_states, got_states = {}, {}
        want = _source_mtimes_reference(root, skip_names=skip, source_states=want_states)
        got = cad_graph._source_mtimes_fast(root, skip_names=skip, source_states=got_states)
        assert got == want
        assert got_states == want_states
        assert "file:sub/deep/er/c.txt" in got and "file:node_modules/x.txt" not in got


def test_get_graph_fingerprint_reuses_single_scan(tmp_path, monkeypatch):
    for rel in ("a.txt", "sub/b.md", "sub/c.txt"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(rel)
    real = cad_graph._source_mtimes_fast
    calls = []

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(cad_graph, "_source_mtimes_fast", counting)
    for _ in ("cold build", "warm cache"):
        calls.clear()
        out = cad_graph.get_graph(tmp_path)
        assert len(calls) == 1
        fresh = real(tmp_path, skip_names=cad_graph._skip_dir_names())
        assert out["sources_mtime_max"] == max(fresh.values())
        assert out["sources_count"] == sum(
            1 for k in fresh if not k.startswith("overlay:"))
    probe = cad_graph.get_graph(tmp_path, if_mtimes_max=out["sources_mtime_max"],
                                if_sources_count=out["sources_count"])
    assert probe.get("unchanged") is True
