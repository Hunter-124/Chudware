"""Tests for canonical serialization (services/plm/canonical.py) and
doc-level diffing (services/mcp/doc_diff_tools.py).

Both modules are pure Python with no engine/bridge dependency and can be
exercised with minimal document dicts.

Run:
    pytest services/mcp/test_doc_canonical.py -v
"""
from __future__ import annotations

import json
import math
import sys
import os
import types

# Allow running as `python services/mcp/test_doc_canonical.py` from any CWD
# and also support `pytest` launched from the repo root.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# doc_diff_tools imports `from mcp.server.fastmcp import FastMCP` at module level for MCP
# tool registration. The pure-Python doc_diff logic doesn't need FastMCP at all, so when the
# real `mcp` SDK genuinely isn't installed, inject a minimal stub rather than fail collection.
#
# Fix: this used to stub unconditionally whenever "mcp.server.fastmcp" wasn't ALREADY in
# sys.modules — i.e. whenever this file happened to be the first to touch `mcp` in the
# process — permanently replacing FastMCP with the bare `object` class in sys.modules for
# every test collected afterward in the same session (confirmed live:
# `services/mcp/server.py`'s real `FastMCP(...)` call then raised `TypeError: object() takes no
# arguments`, and anything importing `Image` from fastmcp got `ImportError`). Try the real SDK
# first; only stub on a genuine ImportError, so an environment that HAS mcp installed (this
# one) never gets the fake `object`-as-FastMCP substituted in.
try:
    import mcp.server.fastmcp  # noqa: F401
except ImportError:
    _stub_mcp = sys.modules.get("mcp") or types.ModuleType("mcp")
    _stub_server = types.ModuleType("mcp.server")
    _stub_fastmcp = types.ModuleType("mcp.server.fastmcp")
    _stub_fastmcp.FastMCP = object  # type: ignore[attr-defined]
    _stub_mcp.server = _stub_server  # type: ignore[attr-defined]
    _stub_server.fastmcp = _stub_fastmcp  # type: ignore[attr-defined]
    sys.modules.setdefault("mcp", _stub_mcp)
    sys.modules["mcp.server"] = _stub_server
    sys.modules["mcp.server.fastmcp"] = _stub_fastmcp

from services.plm.canonical import canonical_dump  # noqa: E402
from services.mcp.doc_diff_tools import doc_diff  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doc(features=None, mates=None):
    """Return a minimal document dict."""
    d = {}
    if features is not None:
        d["features"] = features
    if mates is not None:
        d["mates"] = mates
    return d


def _feature(fid, ftype="extrude", params=None, suppressed=False):
    """Return a minimal feature dict."""
    f = {"id": fid, "type": ftype, "suppressed": suppressed}
    if params is not None:
        f["params"] = params
    return f


# ---------------------------------------------------------------------------
# canonical_dump: byte-stability
# ---------------------------------------------------------------------------

class TestCanonicalDumpByteStability:
    def test_already_canonical_string_is_stable(self):
        """Parsing a canonical string and re-serializing yields the same string."""
        doc = {"x": 1.5, "y": "hello", "z": [True, None]}
        s = canonical_dump(doc)
        assert canonical_dump(json.loads(s)) == s


def test_saved_oblique_axis_remains_collinear_with_pivot_centers():
    direction = [12.7, 25.4, 152.4]
    axis = [value / math.hypot(*direction) for value in direction]
    document = _doc(mates=[{"id": "m1", "type": "angle", "value": {"axis": axis}}])
    saved = json.loads(canonical_dump(document))["mates"][0]["value"]["axis"]
    cross = [
        saved[1] * direction[2] - saved[2] * direction[1],
        saved[2] * direction[0] - saved[0] * direction[2],
        saved[0] * direction[1] - saved[1] * direction[0],
    ]
    assert math.hypot(*cross) / math.hypot(*direction) < 1e-15


def test_diff_reports_small_joint_axis_changes_without_mutating_source():
    axis = [0.08192319205190406, 0.16384638410380797, 0.9830783046228486]
    original = {"id": "m1", "type": "angle", "value": {"axis": axis}}
    rounded = {"id": "m1", "type": "angle", "value": {"axis": [round(v, 6) for v in axis]}}
    delta = doc_diff(_doc(mates=[original]), _doc(mates=[rounded]))
    assert delta["mate_deltas"] == [
        {"id": "m1", "kind": "changed", "old": original, "new": rounded},
    ]
    delta["mate_deltas"][0]["old"]["value"]["axis"][0] = 0.0
    assert original["value"]["axis"][0] == 0.08192319205190406


# ---------------------------------------------------------------------------
# canonical_dump: sorted keys
# ---------------------------------------------------------------------------

class TestCanonicalDumpSortedKeys:
    def test_top_level_keys_sorted(self):
        doc = {"z": 1, "a": 2, "m": 3}
        s = canonical_dump(doc)
        # Parse back and check insertion order matches sorted order
        parsed = json.loads(s)
        assert list(parsed.keys()) == ["a", "m", "z"]

    def test_nested_keys_sorted(self):
        doc = {"outer": {"z": 1, "a": 2}}
        parsed = json.loads(canonical_dump(doc))
        assert list(parsed["outer"].keys()) == ["a", "z"]

    def test_keys_sorted_in_output_string(self):
        """Key order in the raw JSON string must be alphabetical."""
        doc = {"z": 1, "a": 2, "m": 3}
        s = canonical_dump(doc)
        pos_a = s.index('"a"')
        pos_m = s.index('"m"')
        pos_z = s.index('"z"')
        assert pos_a < pos_m < pos_z

    def test_indent_is_2(self):
        """Output must use indent=2 per the canonical policy."""
        doc = {"k": 1}
        s = canonical_dump(doc)
        assert '  "k"' in s  # 2-space indent


# ---------------------------------------------------------------------------
# doc_diff: empty (identical docs)
# ---------------------------------------------------------------------------

class TestDocDiffEmpty:
    def test_identical_empty_doc(self):
        doc = _doc()
        delta = doc_diff(doc, doc)
        assert delta["features_added"] == []
        assert delta["features_removed"] == []
        assert delta["features_changed"] == []
        assert delta["params_changed"] == []
        assert delta["sketch_deltas"] == []
        assert delta["mate_deltas"] == []
        assert delta["suppression_flips"] == []

    def test_identical_doc_with_features(self):
        doc = _doc(features=[_feature("f1", params={"depth": {"value": 10.0, "expr": None}})])
        delta = doc_diff(doc, doc)
        assert all(not v for v in delta.values())

    def test_all_keys_present_in_result(self):
        delta = doc_diff(_doc(), _doc())
        expected_keys = {
            "features_added", "features_removed", "features_changed",
            "params_changed", "sketch_deltas", "mate_deltas", "suppression_flips",
        }
        assert set(delta.keys()) == expected_keys


# ---------------------------------------------------------------------------
# doc_diff: features_added
# ---------------------------------------------------------------------------

class TestDocDiffFeaturesAdded:
    def test_single_feature_added(self):
        base = _doc(features=[])
        new = _doc(features=[_feature("f1", params={"depth": {"value": 10.0, "expr": None}})])
        delta = doc_diff(base, new)
        assert delta["features_added"] == ["f1"]
        assert delta["features_removed"] == []

    def test_multiple_features_added_sorted(self):
        base = _doc(features=[])
        new = _doc(features=[
            _feature("f3", params={}),
            _feature("f1", params={}),
            _feature("f2", params={}),
        ])
        delta = doc_diff(base, new)
        assert delta["features_added"] == ["f1", "f2", "f3"]


# ---------------------------------------------------------------------------
# doc_diff: features_removed
# ---------------------------------------------------------------------------

class TestDocDiffFeaturesRemoved:
    def test_single_feature_removed(self):
        base = _doc(features=[_feature("f1", params={"depth": {"value": 10.0, "expr": None}})])
        new = _doc(features=[])
        delta = doc_diff(base, new)
        assert delta["features_removed"] == ["f1"]
        assert delta["features_added"] == []

    def test_multiple_features_removed_sorted(self):
        base = _doc(features=[
            _feature("f2", params={}),
            _feature("f1", params={}),
        ])
        new = _doc(features=[])
        delta = doc_diff(base, new)
        assert delta["features_removed"] == ["f1", "f2"]


# ---------------------------------------------------------------------------
# doc_diff: params_changed
# ---------------------------------------------------------------------------

class TestDocDiffParamsChanged:
    def test_single_param_changed(self):
        f_old = _feature("f1", params={"depth": {"value": 10.0, "expr": None}})
        f_new = _feature("f1", params={"depth": {"value": 15.0, "expr": None}})
        delta = doc_diff(_doc(features=[f_old]), _doc(features=[f_new]))
        assert len(delta["params_changed"]) == 1
        change = delta["params_changed"][0]
        assert change["feature"] == "f1"
        assert change["param"] == "depth"
        assert change["old"] == {"value": 10.0, "expr": None}
        assert change["new"] == {"value": 15.0, "expr": None}

    def test_param_added(self):
        f_old = _feature("f1", params={})
        f_new = _feature("f1", params={"depth": {"value": 5.0, "expr": None}})
        delta = doc_diff(_doc(features=[f_old]), _doc(features=[f_new]))
        assert any(c["param"] == "depth" for c in delta["params_changed"])

    def test_param_removed(self):
        f_old = _feature("f1", params={"depth": {"value": 5.0, "expr": None}})
        f_new = _feature("f1", params={})
        delta = doc_diff(_doc(features=[f_old]), _doc(features=[f_new]))
        assert any(c["param"] == "depth" for c in delta["params_changed"])

    def test_multiple_params_changed_sorted(self):
        f_old = _feature("f1", params={
            "width": {"value": 5.0, "expr": None},
            "depth": {"value": 10.0, "expr": None},
        })
        f_new = _feature("f1", params={
            "width": {"value": 8.0, "expr": None},
            "depth": {"value": 12.0, "expr": None},
        })
        delta = doc_diff(_doc(features=[f_old]), _doc(features=[f_new]))
        params = [c["param"] for c in delta["params_changed"]]
        assert params == sorted(params)


# ---------------------------------------------------------------------------
# doc_diff: suppression_flips
# ---------------------------------------------------------------------------

class TestDocDiffSuppressionFlips:
    def test_suppressed_false_to_true(self):
        f_old = _feature("f1", suppressed=False)
        f_new = _feature("f1", suppressed=True)
        delta = doc_diff(_doc(features=[f_old]), _doc(features=[f_new]))
        assert len(delta["suppression_flips"]) == 1
        flip = delta["suppression_flips"][0]
        assert flip["id"] == "f1"
        assert flip["old"] is False
        assert flip["new"] is True

    def test_suppressed_true_to_false(self):
        f_old = _feature("f1", suppressed=True)
        f_new = _feature("f1", suppressed=False)
        delta = doc_diff(_doc(features=[f_old]), _doc(features=[f_new]))
        assert len(delta["suppression_flips"]) == 1
        flip = delta["suppression_flips"][0]
        assert flip["old"] is True
        assert flip["new"] is False

    def test_no_suppression_flip_when_unchanged(self):
        f = _feature("f1", suppressed=True)
        delta = doc_diff(_doc(features=[f]), _doc(features=[f]))
        assert delta["suppression_flips"] == []


# ---------------------------------------------------------------------------
# doc_diff: sketch_deltas
# ---------------------------------------------------------------------------

class TestDocDiffSketchDeltas:
    def test_entity_added(self):
        e1 = {"id": "e1", "type": "line", "x1": 0.0, "y1": 0.0, "x2": 10.0, "y2": 0.0}
        e2 = {"id": "e2", "type": "line", "x1": 10.0, "y1": 0.0, "x2": 10.0, "y2": 5.0}

        f_old = _feature("f1", ftype="sketch", params={"entities": [e1], "constraints": []})
        f_new = _feature("f1", ftype="sketch", params={"entities": [e1, e2], "constraints": []})

        delta = doc_diff(_doc(features=[f_old]), _doc(features=[f_new]))
        assert len(delta["sketch_deltas"]) == 1
        sd = delta["sketch_deltas"][0]
        assert sd["feature"] == "f1"
        assert "e2" in sd["entities_added"]
        assert sd["entities_removed"] == []

    def test_entity_removed(self):
        e1 = {"id": "e1", "type": "line", "x1": 0.0, "y1": 0.0, "x2": 10.0, "y2": 0.0}
        e2 = {"id": "e2", "type": "line", "x1": 10.0, "y1": 0.0, "x2": 10.0, "y2": 5.0}

        f_old = _feature("f1", ftype="sketch", params={"entities": [e1, e2], "constraints": []})
        f_new = _feature("f1", ftype="sketch", params={"entities": [e1], "constraints": []})

        delta = doc_diff(_doc(features=[f_old]), _doc(features=[f_new]))
        sd = delta["sketch_deltas"][0]
        assert "e2" in sd["entities_removed"]
        assert sd["entities_added"] == []

    def test_no_sketch_delta_when_identical(self):
        e1 = {"id": "e1", "type": "line", "x1": 0.0, "y1": 0.0, "x2": 10.0, "y2": 0.0}
        f = _feature("f1", ftype="sketch", params={"entities": [e1], "constraints": []})
        delta = doc_diff(_doc(features=[f]), _doc(features=[f]))
        assert delta["sketch_deltas"] == []

    def test_sketch_delta_shows_all_sub_keys(self):
        """Even a minimal delta has all expected sub-keys."""
        e1 = {"id": "e1", "type": "line"}
        e2 = {"id": "e2", "type": "arc"}
        f_old = _feature("f1", ftype="sketch", params={"entities": [e1], "constraints": []})
        f_new = _feature("f1", ftype="sketch", params={"entities": [e2], "constraints": []})
        delta = doc_diff(_doc(features=[f_old]), _doc(features=[f_new]))
        sd = delta["sketch_deltas"][0]
        for key in ("entities_added", "entities_removed", "entities_changed",
                    "constraints_added", "constraints_removed", "constraints_changed"):
            assert key in sd


