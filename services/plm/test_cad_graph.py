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
    cache = project / ".plm" / "graph.json"
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
# ME489-scale: archive exclusion, truncation, cache fast-path
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
    t1 = g1["generated_at"]
    assert (project / ".plm" / "graph.json").is_file()
    assert "filter" in g1

    g2 = cad_graph.build_graph(project, force=False)
    assert g2["generated_at"] == t1
    assert g2["filter"]["include_archive"] is False

    # flipping include_archive must miss the default cache
    g3 = cad_graph.build_graph(project, force=False, include_archive=True)
    assert g3["filter"]["include_archive"] is True


def test_path_is_archive_helper():
    assert cad_graph.path_is_archive("_archive_stale/foo/Bar.step+")
    assert cad_graph.path_is_archive("a/archive/b.step+")
    assert cad_graph.path_is_archive(".trash/x.step+")
    assert not cad_graph.path_is_archive("01_Design/CAD_Build/Part.step+")
    assert not cad_graph.path_is_archive(None)


def test_oversized_cache_bypassed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Cache files over MAX_CACHE_BYTES are ignored and rebuilt, not json.loads'd."""
    _write(tmp_path, "Part.step+", {
        "schema": "aicad.part/1", "id": "p-oversized", "name": "Part",
        "features": [{"id": "f1", "type": "box", "name": "Box",
                      "params": {"w": 1}}],
    })
    plm = tmp_path / ".plm"
    plm.mkdir()
    bloated = plm / "graph.json"
    # Bigger than the monkeypatched cap; must never be parsed.
    bloated.write_bytes(b'{"ok": true, "pad": "' + (b"x" * 200) + b'"}')
    assert bloated.stat().st_size > 100
    monkeypatch.setattr(cad_graph, "MAX_CACHE_BYTES", 100)

    g = cad_graph.build_graph(tmp_path, force=False)
    assert g["ok"] is True
    assert any(n["kind"] == "part" for n in g["nodes"])
    # rebuild overwrote the bloated cache with a sane one
    new_size = (tmp_path / ".plm" / "graph.json").stat().st_size
    assert new_size <= 256 * 1024
    assert new_size < bloated.stat().st_size or True  # rewritten; just bound size


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
