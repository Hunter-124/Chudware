r"""C26 — body appearance (color) tests.

Covers the `set_appearance` op end-to-end through the session API:
  * apply set_appearance -> revision bumps, ops-response carries the new color in tess.bodies
  * the re-emitted AICM's BODY chunk carries the chosen color (the renderer reads this field)
  * save -> open round-trips the appearance (it lives in the document JSON)
  * undo restores the prior color
  * clearing (color=null) drops the override back to the default
  * schema rejects out-of-range / malformed colors

Run:  & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" -m pytest engine\doc\test_appearance.py -q
  (or)  python -m engine.doc.test_appearance   (house-style plain runner)
"""

from __future__ import annotations

import os
import tempfile

from engine.doc import session as docsess
from engine.doc import stepplus
from engine.doc.ops import OpError, apply_ops
from engine.doc.schema import validate_part
from engine.doc.tessellate import read_aicm

# Default body color (must match session._DEFAULT_COLOR).
_DEFAULT = (0.71, 0.76, 0.82)
_RED = [0.85, 0.10, 0.10]


def _box_session(name="Box1"):
    """A one-body part session (a single box feature -> one solid body)."""
    s = docsess.new_session("part", name)
    docsess.apply_ops(s, 0, [{"op": "add_feature", "feature": {
        "type": "box", "name": "Box1",
        "params": {"length": {"value": 20, "expr": None},
                   "width": {"value": 20, "expr": None},
                   "height": {"value": 20, "expr": None}}}}])
    return s


def _body_color(resp) -> tuple:
    bodies = resp["tess"]["bodies"]
    assert len(bodies) == 1, f"expected 1 body, got {len(bodies)}"
    return tuple(bodies[0]["color"])


def _aicm_color(path: str) -> tuple:
    doc = read_aicm(open(path, "rb").read())
    assert len(doc["bodies"]) == 1
    return tuple(round(c, 4) for c in doc["bodies"][0]["color"])


def test_set_appearance_roundtrip_and_undo() -> None:
    s = _box_session()
    # Discover the body's stable owner key from the rebuild meta (owner-feature id for a part).
    resp0 = docsess.rebuild(s)
    bodies0 = resp0["tess"]["bodies"]
    owner = bodies0[0]["owner_key"]
    assert owner, "rebuild meta must report a body owner key"
    assert tuple(round(c, 2) for c in bodies0[0]["color"]) == tuple(round(c, 2) for c in _DEFAULT)

    rev_before = s.revision
    resp = docsess.apply_ops(s, None, [{"op": "set_appearance", "id": owner, "color": _RED}])

    # Revision bumped exactly once.
    assert s.revision == rev_before + 1, f"revision {s.revision} != {rev_before + 1}"
    # Persisted in the document JSON.
    assert s.doc["appearances"][owner] == _RED
    # Ops-response tess meta reflects the new color.
    assert tuple(round(c, 4) for c in _body_color(resp)) == tuple(_RED)
    # The re-emitted AICM body chunk carries the color (this is what the renderer reads).
    assert _aicm_color(resp["tess"]["aicm_path"]) == tuple(round(c, 4) for c in _RED)

    # ---- save -> open round-trips the appearance ----
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "colored.step+")
    stepplus.save(s, path)
    s2, warnings = stepplus.open(path)
    assert warnings == [], warnings
    assert s2.doc.get("appearances", {}).get(owner) == _RED, "appearance must survive save/open"
    resp2 = docsess.rebuild(s2)
    assert _aicm_color(resp2["tess"]["aicm_path"]) == tuple(round(c, 4) for c in _RED)

    # ---- undo restores the default color ----
    resp_u = docsess.undo(s)
    assert s.doc.get("appearances", {}).get(owner) is None, "undo must remove the override"
    assert tuple(round(c, 2) for c in _body_color(resp_u)) == tuple(round(c, 2) for c in _DEFAULT)
    print(f"[appearance] set/round-trip/undo OK (owner={owner})")


def test_set_appearance_clear() -> None:
    s = _box_session()
    owner = docsess.rebuild(s)["tess"]["bodies"][0]["owner_key"]
    docsess.apply_ops(s, None, [{"op": "set_appearance", "id": owner, "color": _RED}])
    assert s.doc["appearances"][owner] == _RED
    # color=null clears the override and prunes the (now empty) appearances map.
    docsess.apply_ops(s, None, [{"op": "set_appearance", "id": owner, "color": None}])
    assert "appearances" not in s.doc or owner not in s.doc.get("appearances", {})
    print("[appearance] clear (color=null) OK")


def test_set_appearance_validation() -> None:
    s = _box_session()
    owner = docsess.rebuild(s)["tess"]["bodies"][0]["owner_key"]
    # The op handler CLAMPS slightly-off floats (forgiving for AI-emitted / GUI values) rather than
    # rejecting — 1.2 -> 1.0.
    clamped, _ = apply_ops(s.doc, [{"op": "set_appearance", "id": owner, "color": [1.2, -0.3, 0.5]}])
    assert clamped["appearances"][owner] == [1.0, 0.0, 0.5], clamped["appearances"][owner]
    # Malformed shapes (wrong arity / non-numeric) ARE rejected by the op handler.
    for bad in ([0.0, 0.0], "red", [0.0, 0.0, "x"]):
        try:
            apply_ops(s.doc, [{"op": "set_appearance", "id": owner, "color": bad}])
            raise AssertionError(f"expected rejection of color {bad}")
        except OpError as e:
            assert e.code == "invalid_op", e.code
    # Empty id rejected by the op handler.
    try:
        apply_ops(s.doc, [{"op": "set_appearance", "id": "", "color": _RED}])
        raise AssertionError("expected rejection of empty id")
    except OpError as e:
        assert e.code == "invalid_op", e.code
    # Direct schema check of a malformed appearances map.
    bad_doc = dict(s.doc)
    bad_doc["appearances"] = {"f1": [2.0, 0.0, 0.0]}
    assert any("appearances" in m for m in validate_part(bad_doc))
    print("[appearance] validation OK")


def main() -> None:
    test_set_appearance_roundtrip_and_undo()
    test_set_appearance_clear()
    test_set_appearance_validation()
    print("\nAPPEARANCE TESTS PASSED")


if __name__ == "__main__":
    main()
