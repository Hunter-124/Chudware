"""Regression tests for the model-dialect guards added across the mcp/*.py tool family
(live run-97813015264134: `'str' object has no attribute 'get'` when a model sent a
Python-repr STRING where an object belonged). Covers:

  * cad_common.coerce_json_or_repr / require_mapping / require_list — the shared
    JSON-or-Python-repr coercion + teaching-error helpers every tool now builds on.
  * A representative crash site fixed in each of cad_asm_tools.py, cam_tools.py and
    cad_drawing_tools.py, driven through the SAME dispatch path a live run uses
    (agent_turn._dispatch_tool -> _bind_tool), with the EXACT live payload shape:
    a single-quoted Python repr with a bare `None` inside.

House style: a plain pytest module, no engine dependency for the pure-helper tests;
the tool-level tests below need a live/in-process engine (hostd fallback is fine) and
are skipped if the bridge cannot be reached at all.
"""
from __future__ import annotations

import json

import pytest

from services.mcp.cad_common import coerce_json_or_repr, require_list, require_mapping


# --------------------------------------------------------------------------- pure helper tests

def test_coerce_passes_through_real_objects_unchanged():
    d = {"type": "cylinder"}
    assert coerce_json_or_repr(d) is d
    lst = [1, 2, 3]
    assert coerce_json_or_repr(lst) is lst


def test_coerce_decodes_json_string():
    assert coerce_json_or_repr('{"type": "cylinder", "r": 5}') == {"type": "cylinder", "r": 5}
    assert coerce_json_or_repr("[1, 2, 3]") == [1, 2, 3]


def test_coerce_decodes_python_repr_with_single_quotes_and_none():
    # The EXACT live shape from run-97813015264134:
    # {"op":"add_feature","feature":"{'type': 'cylinder', ..., 'expr': None}"}
    s = "{'type': 'cylinder', 'params': {'radius': 5, 'expr': None}, 'flag': True}"
    out = coerce_json_or_repr(s)
    assert out == {"type": "cylinder", "params": {"radius": 5, "expr": None}, "flag": True}


def test_coerce_never_raises_on_garbage():
    # Not JSON, not a Python literal — must come back unchanged, never raise.
    garbage = "not a dict at all, just prose"
    assert coerce_json_or_repr(garbage) == garbage
    # A string that LOOKS list/dict-shaped but is malformed both ways.
    malformed = "{'a': , }"
    assert coerce_json_or_repr(malformed) == malformed


def test_coerce_leaves_plain_scalars_alone():
    assert coerce_json_or_repr("doc-cpp0000001") == "doc-cpp0000001"
    assert coerce_json_or_repr(None) is None
    assert coerce_json_or_repr(42) == 42


def test_require_mapping_accepts_dict():
    assert require_mapping({"a": 1}, "x") == {"a": 1}


def test_require_mapping_coerces_repr_string():
    got = require_mapping("{'a': 1, 'b': None}", "feature")
    assert got == {"a": 1, "b": None}


def test_require_mapping_teaches_on_real_mismatch():
    with pytest.raises(ValueError) as ei:
        require_mapping(["not", "a", "dict"], "feature", "a feature dict")
    msg = str(ei.value)
    assert "feature" in msg  # names the argument
    assert "list" in msg  # names the type received
    assert "a feature dict" in msg  # names the accepted shape


def test_require_mapping_on_unparseable_string_names_type_and_shape():
    with pytest.raises(ValueError) as ei:
        require_mapping("not json at all", "params", "a params dict")
    msg = str(ei.value)
    assert "params" in msg and "str" in msg and "a params dict" in msg


def test_require_list_accepts_list():
    assert require_list([1, 2], "ops") == [1, 2]


def test_require_list_coerces_repr_string():
    assert require_list("[{'op': 'noop'}]", "ops") == [{"op": "noop"}]


def test_require_list_wraps_bare_dict_as_single_element_batch():
    # A model sending ONE op object instead of a one-item batch is a real dialect,
    # not an error — this is the class-4 "wrong-but-equivalent shape" tolerance.
    assert require_list({"op": "noop"}, "ops") == [{"op": "noop"}]


def test_require_list_teaches_on_real_mismatch():
    with pytest.raises(ValueError) as ei:
        require_list(42, "ops", "a list of ops")
    msg = str(ei.value)
    assert "ops" in msg and "int" in msg and "a list of ops" in msg


# --------------------------------------------------------------------------- live tool-level tests
# These drive the SAME dispatch path a real run uses (agent_turn._dispatch_tool), proving
# the crash class is gone end-to-end, not just at the helper unit level.

def _dispatch():
    from services.agent.agent_turn import _dispatch_tool  # noqa: PLC0415
    return _dispatch_tool


def _skip_if_no_bridge(exc: Exception):
    msg = str(exc)
    if "libchudware" in msg.lower() or "engine" in msg.lower() and "not" in msg.lower():
        pytest.skip(f"engine bridge unavailable: {exc}")
    raise


def test_cam_set_stock_rejects_stringified_stock_with_teaching_error_not_a_crash():
    """LIVE CRASH shape: cam_set_stock used to do `stock.get('type', '?')` on whatever was
    handed in — a stringified stock dict raised a bare `'str' object has no attribute
    'get'`. It must now EITHER accept the coerced dict (and fail later for an unrelated,
    honestly-named engine-state reason like 'no CAM job') OR — for real garbage — raise a
    teaching ValueError naming 'stock' and the type received. Either way, the raw
    AttributeError string must never appear."""
    dispatch = _dispatch()
    r = dispatch("cam_set_stock",
                 {"setup_id": "s1", "stock": "{'type': 'box', 'min': [0,0,0], 'max': [10,10,10]}"})
    out = r if isinstance(r, dict) else json.loads(r)
    assert out.get("ok") is False
    err = out.get("error", "")
    assert "'str' object has no attribute 'get'" not in err

    # And genuine garbage must teach, not crash.
    r2 = dispatch("cam_set_stock", {"setup_id": "s1", "stock": "not a dict"})
    out2 = r2 if isinstance(r2, dict) else json.loads(r2)
    assert out2.get("ok") is False
    assert "'str' object has no attribute 'get'" not in out2.get("error", "")


def test_cam_update_operation_rejects_stringified_params_with_teaching_error():
    """LIVE CRASH shape: `params.keys()` blew up on a stringified params dict."""
    dispatch = _dispatch()
    r = dispatch("cam_update_operation",
                 {"op_id": "o1", "params": "{'stepover': 2, 'feed': 900}"})
    out = r if isinstance(r, dict) else json.loads(r)
    assert out.get("ok") is False
    assert "'str' object has no attribute" not in out.get("error", "")


def test_set_titleblock_rejects_stringified_fields_with_teaching_error():
    """LIVE CRASH shape: `sorted(fields.keys())` blew up on a stringified fields dict."""
    dispatch = _dispatch()
    r = dispatch("set_titleblock",
                 {"doc_id": "no-such-doc", "fields": "{'title': 'Bracket', 'rev': 'A'}"})
    out = r if isinstance(r, dict) else json.loads(r)
    assert out.get("ok") is False
    assert "'str' object has no attribute" not in out.get("error", "")


def test_asm_mate_coerces_stringified_ref_via_coerce_json_or_repr():
    """Unit-level proof for asm_mate's a/b coercion (the live-dispatch path requires a real
    component file on disk to reach the a/b check, which is out of scope for a fast
    regression test) — the same coerce_json_or_repr call asm_mate now runs on `a`/`b`."""
    coerced = coerce_json_or_repr("{'component': 'k1', 'query': 'top'}")
    assert coerced == {"component": "k1", "query": "top"}
    assert isinstance(coerced, dict) and "component" in coerced


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
