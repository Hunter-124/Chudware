"""Geometry query ownership, ambiguity and bounded readback contracts."""
import json

from services.mcp import cad_eval_tools as ev, gsl_tools as gsl


class Tools:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


def test_owner_is_applied_in_engine_before_selection_and_pages(monkeypatch):
    calls = []
    tree = {"revision": 3, "tree": {"features": [{"id": "f1", "name": "Plate"}]},
            "owner_map": {"f1": {"faces": [[1, 2], [2, 3]], "edges": [[1, 4]]}}}
    def call(method, params):
        calls.append((method, params))
        if method == "doc_tree":
            return tree
        assert method == "doc_query"
        assert params["query"] == {"of": "face", "side": "top", "within": [[1, 2], [2, 3]]}
        return {"count": 2, "matches": [{"sid": "F123abc"}, {"sid": "F123abc"}],
                "ambiguous_sids": {"F123abc": 2}}
    monkeypatch.setattr(ev.cb, "call", call)
    monkeypatch.setattr(ev, "_doc", lambda d: d)
    tools = Tools()
    ev.register_cad_eval_tools(tools)
    out = json.loads(tools.tools["query_geometry"](query={"of": "face", "side": "top", "feature": "Plate"},
                                                   doc_id="d", offset=1, page_size=1))
    assert out["ok"] and out["owner"]["id"] == "f1"
    assert out["total_count"] == 2 and out["next_offset"] is None
    assert len(out["matches"]) == 1 and out["ambiguous_sids"] == {"F123abc": 2}


def test_owner_edge_empty_and_duplicate_names(monkeypatch):
    tree = {"tree": {"features": [{"id": "f1", "name": "Rib"},
                                   {"id": "f2", "name": "Rib"}]},
            "owner_map": {"f1": {"faces": [[1, 1]], "edges": []}}}
    def call(method, params):
        if method == "doc_tree":
            return tree
        assert params["query"] == {"of": "edge", "pick": "longest", "within": []}
        return {"count": 0, "matches": []}
    monkeypatch.setattr(ev.cb, "call", call)
    monkeypatch.setattr(ev, "_doc", lambda d: d)
    tools = Tools()
    ev.register_cad_eval_tools(tools)
    query = tools.tools["query_geometry"]
    out = json.loads(query(query={"of": "edge", "pick": "longest"}, owner="f1", doc_id="d"))
    assert out["ok"] and out["total_count"] == 0
    out = json.loads(query(query={"of": "edge"}, owner="Rib", doc_id="d"))
    assert not out["ok"] and "ambiguous" in out["error"].lower()


def test_sid_lookup_rejects_collisions_and_bad_pagination(monkeypatch):
    monkeypatch.setattr(ev, "_doc", lambda d: d)
    monkeypatch.setattr(ev.cb, "call", lambda method, params: {
        "matches": [{"sid": "F123abc"}, {"sid": "F123abc"}]})
    tools = Tools()
    ev.register_cad_eval_tools(tools)
    lookup = tools.tools["query_geometry"]
    out = json.loads(lookup(query="F123abc", doc_id="d"))
    assert not out["ok"] and "ambiguous" in out["error"].lower()
    out = json.loads(lookup(query={"of": "face"}, doc_id="d", page_size=101))
    assert not out["ok"] and "page_size" in out["error"]


def test_adjacency_and_face_edges_are_bounded_and_preserve_sids(monkeypatch):
    def call(method, params):
        if method == "doc_adjacency":
            return {"faces": [{"ord": i, "sid": "F123abc", "neighbors": list(range(105))}
                              for i in range(3)],
                    "edges": [{"between": [0, 1], "between_sids": ["F123abc", "F456def"]}
                              for _ in range(3)]}
        assert method == "doc_face_edges" and params == {"doc_id": "d", "body_id": 2, "topo_id": 3}
        return {"edges": [1, 2, 3]}
    monkeypatch.setattr(gsl.cb, "call", call)
    monkeypatch.setattr(gsl, "_doc", lambda d: d)
    tools = Tools()
    gsl.register_gsl_tools(tools)
    out = json.loads(tools.tools["query_adjacency"](doc_id="d", page_size=1))
    assert out["total_faces"] == 3 and out["next_offset"] == 1
    assert out["faces"][0]["sid"] == "F123abc" and out["faces"][0]["neighbors_truncated"]
    assert len(out["faces"][0]["neighbors"]) == 100 and out["edges"][0]["between_sids"][1] == "F456def"
    out = json.loads(tools.tools["doc_face_edges"](2, 3, doc_id="d", offset=1, page_size=1))
    assert out["edges"] == [2] and out["total_count"] == 3 and out["next_offset"] == 2
