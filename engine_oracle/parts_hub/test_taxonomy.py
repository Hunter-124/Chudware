"""Tests for the Parts Library V2 taxonomy (taxonomy.json + taxonomy.py).

Covers: real-file schema/id validation, facet inheritance + override-by-key,
match_facets semantics (range with unit suffix, enum case-insensitivity, text
substring, missing-key behavior, list-valued params), route() merging, and the
corrupt-file fallback tree.
"""
from __future__ import annotations

import json

import pytest

from engine_oracle.parts_hub import taxonomy as tax


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Each test sees a freshly loaded taxonomy (and leaves none behind)."""
    tax._reset_cache()
    yield
    tax._reset_cache()


# ---------------------------------------------------------------------------
# Real taxonomy.json — schema + id uniqueness
# ---------------------------------------------------------------------------

def _all_nodes(doc):
    stack = list(doc["categories"])
    while stack:
        node = stack.pop()
        yield node
        stack.extend(node.get("children") or [])


class TestRealTaxonomyFile:
    def test_loads_real_file_not_fallback(self):
        doc = tax.load_taxonomy()
        ids = {n["id"] for n in _all_nodes(doc)}
        assert "fasteners" in ids, "real taxonomy.json did not load (fallback tree?)"

    def test_version_and_shape(self):
        doc = tax.load_taxonomy()
        assert doc["version"] == 1
        assert isinstance(doc["categories"], list)
        assert len(doc["categories"]) == 18, "product catalog defines 18 top folders (17 part folders + vendors)"

    def test_ids_unique_and_dotted(self):
        doc = tax.load_taxonomy()
        seen = set()
        for node in _all_nodes(doc):
            nid = node["id"]
            assert nid not in seen, f"duplicate id {nid!r}"
            seen.add(nid)
        assert len(seen) >= 60, "taxonomy should be a generous catalog (>=60 nodes)"

    def test_child_ids_prefixed_by_parent(self):
        doc = tax.load_taxonomy()

        def _walk(node):
            for child in node.get("children") or []:
                assert child["id"].startswith(node["id"] + "."), \
                    f"{child['id']!r} not under {node['id']!r}"
                _walk(child)

        for cat in doc["categories"]:
            _walk(cat)

    def test_facet_types_legal(self):
        doc = tax.load_taxonomy()
        for node in _all_nodes(doc):
            for f in node.get("facets") or []:
                assert f["type"] in ("range", "enum", "text"), f"{node['id']}: {f}"
                if f["type"] == "enum":
                    assert isinstance(f["values"], list) and f["values"]

    def test_every_node_has_label(self):
        for node in _all_nodes(tax.load_taxonomy()):
            assert node.get("label"), f"{node['id']} missing label"

    def test_validate_rejects_duplicate_ids(self):
        bad = {"version": 1, "categories": [
            {"id": "a", "label": "A"}, {"id": "a", "label": "A2"}]}
        with pytest.raises(ValueError, match="duplicate"):
            tax._validate(bad)

    def test_validate_rejects_illegal_facet_type(self):
        bad = {"version": 1, "categories": [
            {"id": "a", "label": "A",
             "facets": [{"key": "x", "label": "X", "type": "slider"}]}]}
        with pytest.raises(ValueError, match="illegal type"):
            tax._validate(bad)


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

class TestNavigation:
    def test_get_node(self):
        node = tax.get_node("fasteners.bolts_screws.socket_head")
        assert node is not None
        assert node["label"] == "Socket Head"

    def test_get_node_unknown(self):
        assert tax.get_node("no.such.category") is None

    def test_parent_of(self):
        parent = tax.parent_of("fasteners.bolts_screws.socket_head")
        assert parent is not None and parent["id"] == "fasteners.bolts_screws"
        assert tax.parent_of("fasteners") is None
        assert tax.parent_of("no.such") is None

    def test_descendant_ids(self):
        ids = tax.descendant_ids("fasteners.nuts")
        assert "fasteners.nuts" in ids                # includes self
        assert "fasteners.nuts.hex" in ids
        assert "fasteners.bolts_screws.hex" not in ids
        assert tax.descendant_ids("no.such") == set()


# ---------------------------------------------------------------------------
# Facet inheritance
# ---------------------------------------------------------------------------

class TestFacetInheritance:
    def test_leaf_inherits_root_material(self):
        """socket_head defines no material facet — it inherits fasteners' enum."""
        facets = {f["key"]: f for f in tax.effective_facets("fasteners.bolts_screws.socket_head")}
        assert "material" in facets, "material facet must inherit from the fasteners root"
        assert facets["material"]["type"] == "enum"
        assert "titanium" in facets["material"]["values"]
        # And the bolts_screws-level facets came along too.
        assert "thread" in facets and "length_mm" in facets

    def test_child_overrides_by_key(self):
        """cutting_tools/end_mills defines its own material enum — it must override
        the fallback, not duplicate: exactly one 'material' facet, carbide values."""
        facets = [f for f in tax.effective_facets("tools.cutting_tools.end_mills")
                  if f["key"] == "material"]
        assert len(facets) == 1
        assert set(facets[0]["values"]) == {"HSS", "carbide", "cobalt"}

    def test_order_root_first(self):
        keys = [f["key"] for f in tax.effective_facets("fasteners.bolts_screws.hex")]
        assert keys.index("material") < keys.index("thread"), \
            "inherited (root) facets must precede child-level facets"

    def test_unknown_id_empty(self):
        assert tax.effective_facets("no.such") == []


# ---------------------------------------------------------------------------
# route() merging
# ---------------------------------------------------------------------------

class TestRoute:
    def test_route_shape(self):
        r = tax.route("fasteners.bolts_screws.socket_head")
        assert set(r) == {"domains", "sites", "query_terms"}

    def test_route_inherits_and_extends(self):
        r = tax.route("fasteners.bolts_screws.socket_head")
        assert "fastener" in r["domains"] and "mech_family" in r["domains"]
        assert "mcmaster" in r["sites"] and "fastenal" in r["sites"]
        # query_terms accumulate root→leaf, parent terms first.
        assert "fastener" in r["query_terms"]
        assert "socket head cap screw" in r["query_terms"]
        assert r["query_terms"].index("fastener") < r["query_terms"].index("socket head cap screw")

    def test_route_child_sites_extend_not_replace(self):
        """cutting_tools adds mcmaster — parent tools sites remain present."""
        r = tax.route("tools.cutting_tools.end_mills")
        assert "harborfreight" in r["sites"]  # from tools
        assert "mcmaster" in r["sites"]       # added by cutting_tools

    def test_route_dedup(self):
        r = tax.route("tools.cutting_tools")
        assert len(r["sites"]) == len(set(r["sites"]))

    def test_route_unknown_id_empty(self):
        r = tax.route("no.such")
        assert r == {"domains": [], "sites": [], "query_terms": []}


# ---------------------------------------------------------------------------
# match_facets
# ---------------------------------------------------------------------------

_DEFS = [
    {"key": "length_mm", "label": "Length", "type": "range", "unit": "mm"},
    {"key": "thread", "label": "Thread", "type": "enum",
     "values": ["M3", "M4", "M5", "M6", "M8"]},
    {"key": "head", "label": "Head", "type": "text"},
]


class TestMatchFacets:
    def test_range_plain_number(self):
        assert tax.match_facets({"length_mm": 20}, {"length_mm": {"min": 10, "max": 30}}, _DEFS)
        assert not tax.match_facets({"length_mm": 50}, {"length_mm": {"min": 10, "max": 30}}, _DEFS)

    def test_range_unit_suffix(self):
        assert tax.match_facets({"length_mm": "6.35 mm"}, {"length_mm": {"min": 6, "max": 7}}, _DEFS)
        assert not tax.match_facets({"length_mm": "6.35 mm"}, {"length_mm": {"min": 8}}, _DEFS)

    def test_range_int_param_and_open_bounds(self):
        assert tax.match_facets({"length_mm": 100}, {"length_mm": {"min": 50}}, _DEFS)
        assert tax.match_facets({"length_mm": 100}, {"length_mm": {"max": 200}}, _DEFS)

    def test_enum_case_insensitive(self):
        assert tax.match_facets({"thread": "m8"}, {"thread": "M8"}, _DEFS)
        assert tax.match_facets({"thread": "M8"}, {"thread": ["m6", "m8"]}, _DEFS)
        assert not tax.match_facets({"thread": "M8"}, {"thread": ["M3", "M4"]}, _DEFS)

    def test_text_substring(self):
        assert tax.match_facets({"head": "Socket Head Cap"}, {"head": "socket"}, _DEFS)
        assert not tax.match_facets({"head": "Hex"}, {"head": "socket"}, _DEFS)

    def test_missing_key_nonmatch_only_when_queried(self):
        # Facet queried but param absent → non-match.
        assert not tax.match_facets({"thread": "M8"}, {"length_mm": {"min": 5}}, _DEFS)
        # Facet NOT queried → its absence is irrelevant.
        assert tax.match_facets({"thread": "M8"}, {"thread": "M8"}, _DEFS)

    def test_list_valued_params(self):
        assert tax.match_facets({"thread": ["M6", "M8"]}, {"thread": "m8"}, _DEFS)
        assert tax.match_facets({"length_mm": ["5 mm", "25 mm"]},
                                {"length_mm": {"min": 20, "max": 30}}, _DEFS)

    def test_empty_query_matches_everything(self):
        assert tax.match_facets({}, {}, _DEFS)
        assert tax.match_facets({"x": 1}, {"thread": None, "head": "  "}, _DEFS)

    def test_undefined_facet_falls_back_to_text(self):
        assert tax.match_facets({"color": "Matte Black"}, {"color": "black"}, [])


# ---------------------------------------------------------------------------
# GUI projection
# ---------------------------------------------------------------------------

class TestGuiTree:
    def test_roots(self):
        roots = tax.flat_tree_for_gui()
        assert len(roots) == 18
        for node in roots:
            for key in ("id", "label", "icon", "description", "facets", "children"):
                assert key in node

    def test_effective_facets_embedded(self):
        roots = tax.flat_tree_for_gui("fasteners")
        bolts = next(c for c in roots[0]["children"] if c["id"] == "fasteners.bolts_screws")
        leaf = next(c for c in bolts["children"] if c["id"].endswith("socket_head"))
        assert any(f["key"] == "material" for f in leaf["facets"]), \
            "GUI nodes must carry EFFECTIVE (inherited) facets"

    def test_subtree_and_unknown(self):
        sub = tax.flat_tree_for_gui("fasteners.nuts")
        assert len(sub) == 1 and sub[0]["id"] == "fasteners.nuts"
        assert tax.flat_tree_for_gui("no.such") == []


# ---------------------------------------------------------------------------
# Corrupt / missing file fallback
# ---------------------------------------------------------------------------

class TestFallback:
    def _load_with_path(self, monkeypatch, path):
        tax._reset_cache()
        monkeypatch.setattr(tax, "TAXONOMY_PATH", path)
        return tax.load_taxonomy()

    def test_missing_file_falls_back(self, monkeypatch, tmp_path):
        doc = self._load_with_path(monkeypatch, tmp_path / "nope.json")
        ids = {c["id"] for c in doc["categories"]}
        assert ids == set(tax._FALLBACK_DOMAINS), "fallback = legacy flat domains"

    def test_corrupt_json_falls_back(self, monkeypatch, tmp_path):
        p = tmp_path / "broken.json"
        p.write_text("{this is not json", encoding="utf-8")
        doc = self._load_with_path(monkeypatch, p)
        assert "fastener" in {c["id"] for c in doc["categories"]}

    def test_schema_invalid_falls_back(self, monkeypatch, tmp_path):
        p = tmp_path / "dup.json"
        p.write_text(json.dumps({"version": 1, "categories": [
            {"id": "x", "label": "X"}, {"id": "x", "label": "X2"}]}), encoding="utf-8")
        doc = self._load_with_path(monkeypatch, p)
        assert "fastener" in {c["id"] for c in doc["categories"]}

    def test_fallback_still_navigable(self, monkeypatch, tmp_path):
        self._load_with_path(monkeypatch, tmp_path / "nope.json")
        assert tax.get_node("fastener") is not None
        assert tax.route("fastener")["domains"] == ["fastener"]
        assert tax.effective_facets("fastener") == []
