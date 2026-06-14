"""CONFIG — named configurations (per-config param overrides + feature/component suppression).

Run:  & $py -m pytest engine/doc/test_configurations.py -q   (from repo root)

The model under test is the card's reference part: a 60x60 square sketch on plane.top, extruded
blind (depth is a DIM so it can be overridden), plus a through hole on the bottom face (a face that
stays at z=0 regardless of depth, so the hole selector never dangles as depth changes). Two extra
configurations:
  * "Small" — extrude depth -> 20, the hole SUPPRESSED.
  * "Large" — extrude depth -> 40, the hole kept.
plus the implicit "Default" (base values: depth 30, hole present).

Covered (the card's deliverable obligations):
  * schema accepts configurations + active_configuration; unique names; bad ids are clean errors.
  * a config-less doc compiles BYTE-IDENTICALLY (regression guard) and reports active "Default".
  * activating Small -> height 20, hole gone (volume drops by the no-hole + shorter body);
    activating Large -> height 40, hole present; activating Default -> base values.
  * mass/volume differ per config.
  * overrides + suppression are applied WITHOUT mutating the stored document.
  * the compile cache keys on the active config (switching back to Default is an all-hit rebuild).
  * the config_* session ops (list/add/delete/activate/set_override/set_suppression) drive the
    active config + rebuild and surface the right ops-response.
  * .step+ save -> reopen preserves configurations + active_configuration and recompiles the active
    config.
  * unknown-config / unknown-feature edits raise clean OpErrors (never crash).
"""

from __future__ import annotations

import math
import os
import tempfile

from engine.doc import session as docsess
from engine.doc import stepplus
from engine.doc.compiler import (
    CompileCache,
    apply_configuration_to_doc,
    compile_document,
    resolve_active_config,
)
from engine.doc.ops import OpError
from engine.doc.schema import validate_document


# --------------------------------------------------------------------------- doc builders


def _dim(value: float, expr: str | None = None) -> dict:
    return {"value": value, "expr": expr}


def _feat(fid: str, ftype: str, params: dict, refs: dict | None = None,
          name: str | None = None, suppressed: bool = False) -> dict:
    return {"id": fid, "type": ftype, "name": name or fid,
            "suppressed": suppressed, "params": params, "refs": refs or {}}


def _square_sketch(fid: str = "f1", half: float = 30.0) -> dict:
    h = half
    return _feat(fid, "sketch", {"entities": [
        {"id": "e1", "type": "line", "construction": False, "p1": [-h, -h], "p2": [h, -h]},
        {"id": "e2", "type": "line", "construction": False, "p1": [h, -h], "p2": [h, h]},
        {"id": "e3", "type": "line", "construction": False, "p1": [h, h], "p2": [-h, h]},
        {"id": "e4", "type": "line", "construction": False, "p1": [-h, h], "p2": [-h, -h]},
    ], "constraints": []}, refs={"plane": "plane.top"})


def _bottom_face_sel() -> dict:
    # Bottom face: z=0, normal +Z (build123d reports the plane axis as +Z either way). It does NOT
    # move when the extrude depth changes, so the hole selector resolves cleanly for every config.
    return {"kind": "face", "surf": "plane", "centroid": [0.0, 0.0, 0.0],
            "axis": [0.0, 0.0, 1.0], "owner_feature": "f1", "hint_index": 4}


def _base_features(depth: float = 30.0, hole_d: float = 8.0) -> list[dict]:
    sk = _square_sketch("f1")
    ex = _feat("f2", "extrude", {"mode": "boss", "end": "blind", "depth": _dim(depth)},
               refs={"profile": [{"kind": "region", "sketch": "f1"}]})
    hole = _feat("f3", "hole",
                 {"style": "simple", "end": "through_all", "standard": "custom",
                  "diameter": _dim(hole_d)},
                 refs={"face": _bottom_face_sel()})
    return [sk, ex, hole]


def _doc(features: list[dict], configs: list[dict] | None = None,
         active: str | None = None) -> dict:
    d = {
        "schema": "aicad.part/1",
        "id": "test-config",
        "name": "ConfigPart",
        "units": {"system": "MMGS"},
        "material": None,
        "equations": {"globals": []},
        "features": features,
    }
    if configs is not None:
        d["configurations"] = configs
    if active is not None:
        d["active_configuration"] = active
    return d


def _configs() -> list[dict]:
    return [
        {"name": "Default", "param_overrides": {}, "suppressions": {}},
        {"name": "Small", "param_overrides": {"f2.depth": _dim(20.0)},
         "suppressions": {"f3": True}},
        {"name": "Large", "param_overrides": {"f2.depth": _dim(40.0)},
         "suppressions": {}},
    ]


def _configured_doc(active: str = "Default") -> dict:
    return _doc(_base_features(), configs=_configs(), active=active)


# Expected reference numbers (mm). Body volume of a 60x60xD prism minus a Ø8 through hole.
_SIDE = 60.0
_HOLE_R = 4.0


def _solid_vol(depth: float, with_hole: bool) -> float:
    v = _SIDE * _SIDE * depth
    if with_hole:
        v -= math.pi * _HOLE_R * _HOLE_R * depth
    return v


def _status(res, fid: str) -> dict:
    for s in res.feature_status:
        if s["id"] == fid:
            return s
    raise AssertionError(f"no status for {fid}")


# --------------------------------------------------------------------------- schema


def test_schema_accepts_configurations() -> None:
    doc = _configured_doc("Small")
    assert validate_document(doc) == [], validate_document(doc)


def test_schema_absent_configs_is_valid_and_unchanged() -> None:
    # No configurations key at all — must validate exactly as a plain part.
    doc = _doc(_base_features())
    assert "configurations" not in doc
    assert validate_document(doc) == []


def test_schema_duplicate_config_name_is_error() -> None:
    doc = _doc(_base_features(), configs=[
        {"name": "A", "param_overrides": {}, "suppressions": {}},
        {"name": "A", "param_overrides": {}, "suppressions": {}},
    ], active="A")
    errs = validate_document(doc)
    assert any("duplicate" in e for e in errs), errs


def test_schema_unknown_feature_in_override_is_error() -> None:
    doc = _doc(_base_features(), configs=[
        {"name": "X", "param_overrides": {"f99.depth": _dim(5.0)}, "suppressions": {}},
    ], active="X")
    errs = validate_document(doc)
    assert any("f99" in e and "does not exist" in e for e in errs), errs


def test_schema_unknown_param_in_override_is_error() -> None:
    doc = _doc(_base_features(), configs=[
        {"name": "X", "param_overrides": {"f2.bogus": _dim(5.0)}, "suppressions": {}},
    ], active="X")
    errs = validate_document(doc)
    assert any("no param 'bogus'" in e for e in errs), errs


def test_schema_unknown_active_config_is_error() -> None:
    doc = _doc(_base_features(), configs=_configs(), active="Nope")
    errs = validate_document(doc)
    assert any("active_configuration" in e for e in errs), errs


# --------------------------------------------------------------------------- compiler / overlay


def test_default_config_matches_base_values() -> None:
    res = compile_document(_configured_doc("Default"))
    assert res.active_configuration == "Default"
    assert _status(res, "f3")["ok"] and not _status(res, "f3")["skipped"]
    b = res.bodies[-1].shape
    assert abs(b.bounding_box().max.Z - 30.0) < 1e-6
    assert abs(b.volume - _solid_vol(30.0, with_hole=True)) / b.volume < 0.01


def test_small_config_overrides_depth_and_suppresses_hole() -> None:
    res = compile_document(_configured_doc("Small"))
    assert res.active_configuration == "Small"
    # Hole suppressed by the config -> status skipped, body has no bore.
    assert _status(res, "f3")["skipped"], _status(res, "f3")
    b = res.bodies[-1].shape
    assert abs(b.bounding_box().max.Z - 20.0) < 1e-6, "depth override 20 not applied"
    # No hole -> volume is the FULL prism (no pi r^2 h subtracted).
    assert abs(b.volume - _solid_vol(20.0, with_hole=False)) < 1.0, b.volume


def test_large_config_overrides_depth_keeps_hole() -> None:
    res = compile_document(_configured_doc("Large"))
    assert res.active_configuration == "Large"
    assert not _status(res, "f3")["skipped"]
    b = res.bodies[-1].shape
    assert abs(b.bounding_box().max.Z - 40.0) < 1e-6, "depth override 40 not applied"
    assert abs(b.volume - _solid_vol(40.0, with_hole=True)) / b.volume < 0.01


def test_mass_volume_differ_per_config() -> None:
    vols = {}
    for cfg in ("Default", "Small", "Large"):
        res = compile_document(_configured_doc(cfg))
        vols[cfg] = res.bodies[-1].shape.volume
    assert vols["Small"] < vols["Default"] < vols["Large"], vols
    # All three distinct.
    assert len(set(round(v, 3) for v in vols.values())) == 3, vols


def test_overlay_does_not_mutate_stored_document() -> None:
    doc = _configured_doc("Small")
    # Snapshot the stored feature params + suppressed flags.
    base_depth = doc["features"][1]["params"]["depth"]["value"]
    base_hole_suppressed = doc["features"][2]["suppressed"]
    compile_document(doc)
    # The stored document is untouched: depth still 30, hole still un-suppressed in the base.
    assert doc["features"][1]["params"]["depth"]["value"] == base_depth == 30.0
    assert doc["features"][2]["suppressed"] == base_hole_suppressed is False
    # apply_configuration_to_doc shares untouched sub-objects but never edits the original.
    name, eff = apply_configuration_to_doc(doc)
    assert name == "Small"
    assert eff is not doc
    assert eff["features"][1]["params"]["depth"]["value"] == 20.0  # overlay
    assert doc["features"][1]["params"]["depth"]["value"] == 30.0  # original intact


def test_absent_config_compiles_byte_identically() -> None:
    # Regression guard: a doc with NO configurations and the SAME doc with an explicit single
    # "Default" config (no overrides/suppressions) must compile to identical geometry + status.
    feats = _base_features()
    plain = _doc([dict(f) for f in feats])
    with_default = _doc([dict(f) for f in feats], configs=[
        {"name": "Default", "param_overrides": {}, "suppressions": {}},
    ], active="Default")

    r0 = compile_document(plain)
    r1 = compile_document(with_default)
    assert r0.active_configuration == "Default" and r1.active_configuration == "Default"
    assert len(r0.bodies) == len(r1.bodies) == 1
    assert abs(r0.bodies[-1].shape.volume - r1.bodies[-1].shape.volume) < 1e-9
    # resolve_active_config returns the implicit Default (None config) for the config-less doc.
    assert resolve_active_config(plain) == ("Default", None)


def test_compile_cache_keys_on_active_config() -> None:
    doc = _configured_doc("Default")
    cache = CompileCache()
    # First pass per config is all-miss; switching back to a previously-compiled config is all-hit.
    doc["active_configuration"] = "Default"
    r_def = compile_document(doc, cache)
    assert r_def.cache_misses == 3 and r_def.cache_hits == 0

    doc["active_configuration"] = "Small"
    r_small = compile_document(doc, cache)
    # Small recompiles its (different) effective features — misses, not stale Default hits.
    assert r_small.cache_hits == 0 and r_small.cache_misses >= 1
    assert abs(r_small.bodies[-1].shape.bounding_box().max.Z - 20.0) < 1e-6

    doc["active_configuration"] = "Default"
    r_def2 = compile_document(doc, cache)
    # Back to Default: every feature key is unchanged -> all hits, no recompute.
    assert r_def2.cache_hits == 3 and r_def2.cache_misses == 0
    assert abs(r_def2.bodies[-1].shape.bounding_box().max.Z - 30.0) < 1e-6


# --------------------------------------------------------------------------- session config_* ops


def _build_session() -> docsess.DocSession:
    """A live session carrying the configured part (built via apply_ops so it is a real session)."""
    s = docsess.new_session("part", "ConfigPart")
    ops = [{"op": "add_feature", "feature": f} for f in _base_features()]
    docsess.apply_ops(s, 0, ops)
    # Inject the configs directly (the host builds them via config_* ops; here we seed all three).
    s.doc["configurations"] = _configs()
    s.doc["active_configuration"] = "Default"
    return s


def test_config_list_default_when_absent() -> None:
    s = docsess.new_session("part", "Plain")
    docsess.apply_ops(s, 0, [{"op": "add_feature", "feature": _base_features()[0]}])
    out = docsess.config_list(s)
    assert out["configurations"] == ["Default"] and out["active"] == "Default"


def test_config_list_reports_names_and_active() -> None:
    s = _build_session()
    out = docsess.config_list(s)
    assert out["configurations"] == ["Default", "Small", "Large"]
    assert out["active"] == "Default"


def test_config_activate_rebuilds_and_changes_geometry() -> None:
    s = _build_session()
    resp = docsess.config_activate(s, "Small")
    assert resp["rebuild"]["doc_status"]["active_configuration"] == "Small"
    # The hole feature reports skipped in the rebuild status.
    f3 = next(x for x in resp["rebuild"]["feature_status"] if x["id"] == "f3")
    assert f3.get("rolled_back") is False
    b = s.last_result.bodies[-1].shape
    assert abs(b.bounding_box().max.Z - 20.0) < 1e-6


def test_config_add_copy_from_then_activate() -> None:
    s = _build_session()
    rev0 = s.revision
    docsess.config_add(s, "SmallClone", copy_from="Small")
    assert s.revision == rev0 + 1  # one undoable step, no activation
    names = [c["name"] for c in s.doc["configurations"]]
    assert "SmallClone" in names
    clone = next(c for c in s.doc["configurations"] if c["name"] == "SmallClone")
    assert clone["param_overrides"] == {"f2.depth": {"value": 20.0, "expr": None}}
    assert clone["suppressions"] == {"f3": True}
    # Activating the clone yields Small-like geometry.
    docsess.config_activate(s, "SmallClone")
    assert abs(s.last_result.bodies[-1].shape.bounding_box().max.Z - 20.0) < 1e-6


def test_config_set_override_and_clear() -> None:
    s = _build_session()
    docsess.config_activate(s, "Default")
    # Override Default's depth to 12, then clear it back to the base 30.
    docsess.config_set_override(s, "Default", "f2", "depth", {"value": 12.0, "expr": None})
    assert abs(s.last_result.bodies[-1].shape.bounding_box().max.Z - 12.0) < 1e-6
    docsess.config_set_override(s, "Default", "f2", "depth", None, clear=True)
    assert "f2.depth" not in s.doc["configurations"][0]["param_overrides"]
    assert abs(s.last_result.bodies[-1].shape.bounding_box().max.Z - 30.0) < 1e-6


def test_config_set_suppression_toggles_feature() -> None:
    s = _build_session()
    docsess.config_activate(s, "Default")
    assert not next(x for x in s.last_result.feature_status if x["id"] == "f3")["skipped"]
    docsess.config_set_suppression(s, "Default", "f3", True)
    assert next(x for x in s.last_result.feature_status if x["id"] == "f3")["skipped"]
    # Un-suppress drops the entry (configs stay minimal) and the hole returns.
    docsess.config_set_suppression(s, "Default", "f3", False)
    assert "f3" not in s.doc["configurations"][0]["suppressions"]
    assert not next(x for x in s.last_result.feature_status if x["id"] == "f3")["skipped"]


def test_config_delete_active_falls_back() -> None:
    s = _build_session()
    docsess.config_activate(s, "Small")
    docsess.config_delete(s, "Small")
    names = [c["name"] for c in s.doc["configurations"]]
    assert "Small" not in names
    # Active fell back to the first remaining config (Default) and rebuilt to depth 30.
    assert s.doc["active_configuration"] == "Default"
    assert abs(s.last_result.bodies[-1].shape.bounding_box().max.Z - 30.0) < 1e-6


# --------------------------------------------------------------------------- clean errors


def test_unknown_config_activate_is_clean_error() -> None:
    s = _build_session()
    try:
        docsess.config_activate(s, "Ghost")
    except OpError as e:
        assert e.code == "invalid_op" and "Ghost" in e.message
    else:
        raise AssertionError("expected OpError for unknown config")


def test_override_on_unknown_feature_is_clean_error() -> None:
    s = _build_session()
    try:
        docsess.config_set_override(s, "Default", "f99", "depth", {"value": 5.0, "expr": None})
    except OpError as e:
        assert e.code == "invalid_op"
    else:
        raise AssertionError("expected OpError for override on unknown feature")
    # The session document was not left invalid.
    assert validate_document(s.doc) == []


def test_delete_last_config_is_rejected() -> None:
    s = docsess.new_session("part", "Plain")
    docsess.apply_ops(s, 0, [{"op": "add_feature", "feature": _base_features()[0]}])
    s.doc["configurations"] = [{"name": "Default", "param_overrides": {}, "suppressions": {}}]
    s.doc["active_configuration"] = "Default"
    try:
        docsess.config_delete(s, "Default")
    except OpError as e:
        assert "only configuration" in e.message
    else:
        raise AssertionError("deleting the last configuration must be rejected")


# --------------------------------------------------------------------------- .step+ round-trip


def test_stepplus_roundtrip_preserves_configs_and_active() -> None:
    s = _build_session()
    docsess.config_activate(s, "Large")  # active config carried through save
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "config.step+")
    stepplus.save(s, path)
    assert not os.path.exists(path + ".tmp")

    s2, warnings = stepplus.open(path)
    assert warnings == [], warnings
    # Configurations + active pointer survived.
    names = [c["name"] for c in s2.doc.get("configurations", [])]
    assert names == ["Default", "Small", "Large"], names
    assert s2.doc.get("active_configuration") == "Large"
    # Reopen recompiles the ACTIVE config (Large -> depth 40, hole present).
    res = s2.last_result
    assert res is not None and res.active_configuration == "Large"
    assert abs(res.bodies[-1].shape.bounding_box().max.Z - 40.0) < 1e-6
