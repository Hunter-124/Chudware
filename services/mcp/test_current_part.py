"""The agent's "current part" — the CAD equivalent of a coding agent's open file.

An agent should not have to name a document it just created. These pin the three
rules that make that true without ever editing someone else's part:

  1. The part THIS process created/opened stays current however many other
     documents (the app's untitled Part1, another agent's part) are open.
  2. A current part that has been closed underneath us is dropped, not forwarded
     as a dead handle.
  3. A sub-agent's part is its own: it never becomes its parent's current part.

Plus the inline-profile teaching error, the other half of the reported failure:
`revolve` given a bare point list used to surface as "refs.profile: required".

Run: python3 -m pytest services/mcp/test_current_part.py -q
"""
from __future__ import annotations

import pytest

from services.mcp import cad_common
from services.mcp.cad_doc_tools import _normalize_profile_feature


@pytest.fixture
def open_docs(monkeypatch):
    """Drive `doc_list` from a mutable list; reset the process's current part."""
    docs: list[dict] = []

    def _call(method, args=None):
        assert method == "doc_list", f"unexpected engine call {method}"
        return {"docs": list(docs)}

    monkeypatch.setattr(cad_common.cb, "call", _call)
    monkeypatch.setitem(cad_common._CUR, "doc_id", None)
    return docs


def test_current_part_survives_other_clients_documents(open_docs):
    """The reported failure: the app has an untitled Part1 open, the agent makes
    its own part, and its very next add_features died as "Ambiguous: 2 parts are
    open". The agent's own part is not ambiguous — it is the one it created."""
    open_docs += [{"doc_id": "doc-cpp00000001", "name": "Part1"},
                  {"doc_id": "doc-cpp00000002", "name": "mudbucket_shell"}]
    cad_common._CUR["doc_id"] = "doc-cpp00000002"

    assert cad_common._doc(None) == "doc-cpp00000002"


def test_closed_current_part_is_dropped_not_forwarded(open_docs):
    """The human closed our part. Forwarding the dead handle only defers the
    failure to a confusing "no such document" — re-resolve instead."""
    open_docs.append({"doc_id": "doc-cpp00000001", "name": "Part1"})
    cad_common._CUR["doc_id"] = "doc-cpp00000009"

    assert cad_common._doc(None) == "doc-cpp00000001"
    assert cad_common._CUR["doc_id"] == "doc-cpp00000001"


def test_no_claim_and_several_open_teaches_new_part(open_docs):
    """Nothing claimed yet IS genuinely ambiguous — but the fix is to make your
    own part, not to guess at one of the user's."""
    open_docs += [{"doc_id": "doc-cpp00000001", "name": "Part1"},
                  {"doc_id": "doc-cpp00000002", "name": "bracket"}]

    with pytest.raises(cad_common.DocRefError) as exc:
        cad_common._doc(None)
    assert "new_part" in str(exc.value)
    assert "doc-cpp00000001" in str(exc.value)


def test_sole_open_part_is_adopted(open_docs):
    """One part open and nothing claimed: act on what the user is looking at."""
    open_docs.append({"doc_id": "doc-cpp00000001", "name": "Part1"})

    assert cad_common._doc(None) == "doc-cpp00000001"


def test_explicit_doc_id_still_wins(open_docs):
    open_docs += [{"doc_id": "doc-cpp00000001", "name": "Part1"},
                  {"doc_id": "doc-cpp00000002", "name": "mudbucket_shell"}]
    cad_common._CUR["doc_id"] = "doc-cpp00000002"

    assert cad_common._doc("Part1") == "doc-cpp00000001"


def test_doc_scope_restores_the_parents_part(open_docs):
    """A sub-agent may create and work in its own part; the parent's next edit
    must still land in the parent's part."""
    cad_common._CUR["doc_id"] = "doc-cpp00000001"
    with cad_common.doc_scope():
        cad_common._CUR["doc_id"] = "doc-cpp00000002"
    assert cad_common._CUR["doc_id"] == "doc-cpp00000001"


def test_doc_scope_restores_after_an_exception(open_docs):
    cad_common._CUR["doc_id"] = "doc-cpp00000001"
    with pytest.raises(RuntimeError), cad_common.doc_scope():
        cad_common._CUR["doc_id"] = "doc-cpp00000002"
        raise RuntimeError("sub-agent blew up")
    assert cad_common._CUR["doc_id"] == "doc-cpp00000001"


def test_inline_point_list_profile_teaches_the_sketch_op():
    """The second half of the reported failure: a bare polyline under
    params.profile fell through every normalizer and resurfaced from the kernel
    as "refs.profile: required for revolve", which does not say what to do."""
    feat = {"type": "revolve", "name": "mudbucket_shell", "angle": 360, "axis": "y",
            "profile": [[335, 0], [345, 0], [455, 1200], [445, 1200], [335, 0]]}

    with pytest.raises(ValueError) as exc:
        _normalize_profile_feature(feat, "revolve", [])
    msg = str(exc.value)
    assert "INLINE geometry" in msg
    assert '"type":"sketch"' in msg
    assert "refs.profile" in msg
    # The suggested plane must CONTAIN the axis, or following the advice yields a
    # zero-volume revolve — the next dead end. axis.y is normal to plane.front.
    assert "plane.right" in msg and "plane.front" not in msg


def test_named_sketch_profile_is_still_interpreted():
    """Naming a sketch must keep working — the teaching error is only for
    coordinates."""
    notes: list[str] = []
    out = _normalize_profile_feature(
        {"type": "revolve", "name": "Rev", "profile": "f1", "axis": "z"},
        "revolve", notes)
    assert out["refs"]["profile"] == [{"kind": "region", "sketch": "f1", "hint_index": 0}]
    assert out["refs"]["axis"] == "axis.z"


def test_selector_list_profile_is_not_mistaken_for_geometry():
    """refs-shaped selectors written at the top level are dicts, not coordinates;
    they must not trip the inline-geometry error."""
    feat = {"type": "revolve", "name": "Rev", "axis": "z",
            "profile": [{"kind": "region", "sketch": "f1", "hint_index": 0}]}
    _normalize_profile_feature(feat, "revolve", [])  # must not raise
