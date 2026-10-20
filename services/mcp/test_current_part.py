"""The agent's "current part" — the CAD equivalent of a coding agent's open file.

An agent should not have to name a document it just created. These pin the three
rules that make that true without ever editing someone else's part:

  1. The part THIS process created/opened stays current however many other
     documents (the app's untitled Part1, another agent's part) are open.
  2. A current part that has been closed underneath us is dropped, not forwarded
     as a dead handle.
  3. A sub-agent's part is its own: it never becomes its parent's current part.
  4. The one part open ENGINE-WIDE is not automatically the caller's: every MCP
     client sharing a chudware-hostd shares one document list, so a peer's part
     is refused with an actionable error instead of silently adopted.

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
    """Drive `doc_list` from a mutable list; reset the process's current part AND its
    cross-process claim set (both are module-level, so an unreset entry from another test
    would make the sole-document adopt below permissive for reasons this test never set up)."""
    docs: list[dict] = []

    def _call(method, args=None):
        assert method == "doc_list", f"unexpected engine call {method}"
        return {"docs": list(docs)}

    monkeypatch.setattr(cad_common.cb, "call", _call)
    monkeypatch.setitem(cad_common._CUR, "doc_id", None)
    monkeypatch.setattr(cad_common, "_PROCESS_DOCS", set())
    monkeypatch.setattr(cad_common, "_TOUCHED_DOCS", [])
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


def test_stale_engine_id_is_refused_with_a_reopen_path(open_docs):
    """2026-09-28 c221: after an app restart the agent passed the transcript's
    doc-cpp00000003. It must never land on the one part still open, and the refusal
    must name the way back (reopen by path), not "pass one of the ids above"."""
    open_docs.append({"doc_id": "doc-cpp00000002", "name": "sixaxis_arm"})

    with pytest.raises(cad_common.DocRefError) as exc:
        cad_common._doc("doc-cpp00000003")
    assert exc.value.code == "doc_not_found"
    assert "import_part" in exc.value.detail["hint"]

    open_docs.clear()
    with pytest.raises(cad_common.DocRefError) as none_open:
        cad_common._doc("doc-cpp00000003")
    assert "import_part" in none_open.value.detail["hint"]


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


# --- rule 4: the sole open document is not automatically the CALLER's ----------------
# `doc_list` is ENGINE-GLOBAL: every MCP client attached to the same chudware-hostd sees
# the same documents. "Exactly one part open" is therefore also what a fresh cowork swarm
# member sees the instant a PEER created its first part, so the adopt above has to ask
# whose part it is. The wire carries exactly one attribution field for that — the daemon's
# `origin_kind` (gui|mcp|agent|test|unknown), the KIND of client that minted the doc — and
# no minting client id, pid or session, so "another AI client's part" is the finest
# distinction available. Reproduced live across two real processes on one private daemon.


@pytest.fixture
def daemon_mode(monkeypatch):
    """Force the shared-engine (daemon) verdict. Only daemon mode can see another
    client's documents at all; the legacy path lists this process's own in-process
    engine, which is why it adopts unconditionally."""
    def _set(on: bool) -> None:
        monkeypatch.setattr(cad_common.cb, "active_daemon_mode", lambda: on)
    return _set


def test_sole_part_from_another_ai_client_is_not_adopted(open_docs, daemon_mode):
    """The hijack: client A called new_part("Bracket"), so exactly one doc is open
    engine-wide. Client B's very first bare call must NOT silently land in it."""
    daemon_mode(True)
    open_docs.append({"doc_id": "doc-cpp00000001", "name": "Bracket",
                      "origin_kind": "mcp"})

    with pytest.raises(cad_common.DocRefError) as exc:
        cad_common._doc(None)

    assert exc.value.code == "doc_owned_by_other_client"
    msg = str(exc.value)
    # The error has to be actionable on the model's next turn, not just a refusal.
    assert "new_part(" in msg and "list_parts()" in msg
    assert 'doc_id="doc-cpp00000001"' in msg
    # Refusing must not leave a claim behind, or the retry would adopt it anyway.
    assert cad_common._CUR["doc_id"] is None
    assert "doc-cpp00000001" not in cad_common._PROCESS_DOCS


def test_sole_part_this_process_created_is_still_adopted(open_docs, daemon_mode):
    """No regression for the lone client: the doc is ours (a `new_part` recorded it),
    so a bare call still resolves even once `_CUR` is gone — a sub-agent's
    `doc_scope()` unwinding, or a `close_part` of a different part, clears it."""
    daemon_mode(True)
    open_docs.append({"doc_id": "doc-cpp00000001", "name": "Bracket",
                      "origin_kind": "mcp"})
    cad_common.note_touched_doc("doc-cpp00000001")

    assert cad_common._doc(None) == "doc-cpp00000001"


def test_sole_gui_part_is_still_adopted(open_docs, daemon_mode):
    """The whole point of the adopt: the human has one part open in the viewport and
    the agent's bare `describe_part()` should act on it. A gui-origin doc is the
    USER's, not a peer agent's private working part."""
    daemon_mode(True)
    open_docs.append({"doc_id": "doc-cpp00000002", "name": "UserPart",
                      "origin_kind": "gui"})

    assert cad_common._doc(None) == "doc-cpp00000002"


def test_legacy_mode_adopts_the_sole_part_whatever_its_origin(open_docs, daemon_mode):
    """Legacy (in-process) mode has no shared engine, so every doc `doc_list` can
    return was made by THIS process. There is no peer to steal from; never refuse."""
    daemon_mode(False)
    open_docs.append({"doc_id": "doc-cpp00000001", "name": "Bracket",
                      "origin_kind": "mcp"})

    assert cad_common._doc(None) == "doc-cpp00000001"


def test_explicit_doc_id_reaches_another_clients_part_and_claims_it(open_docs, daemon_mode):
    """Only the IMPLICIT resolution is scoped. Naming a peer's part explicitly is a
    deliberate act and keeps working — and it makes that part this caller's default,
    so the follow-up bare call does not then refuse the doc it was just handed."""
    daemon_mode(True)
    open_docs.append({"doc_id": "doc-cpp00000001", "name": "Bracket",
                      "origin_kind": "mcp"})

    assert cad_common._doc("doc-cpp00000001") == "doc-cpp00000001"
    assert cad_common._doc("Bracket") == "doc-cpp00000001"
    assert cad_common._doc(None) == "doc-cpp00000001"


def test_two_process_hijack_is_refused_then_own_part_resolves(open_docs, daemon_mode):
    """The full cowork sequence, with each process's module state modelled explicitly
    (`_CUR` + `_PROCESS_DOCS`/`_TOUCHED_DOCS` are what differ between two servers
    sharing one daemon; the engine listing is what they share).

    A creates Bracket -> B refuses -> B makes its own part -> both resolve their own.
    """
    daemon_mode(True)

    # --- process A
    open_docs.append({"doc_id": "doc-cpp00000001", "name": "Bracket",
                      "origin_kind": "mcp"})
    cad_common._CUR["doc_id"] = "doc-cpp00000001"
    cad_common.note_touched_doc("doc-cpp00000001")
    a_state = (cad_common._CUR["doc_id"], set(cad_common._PROCESS_DOCS),
               list(cad_common._TOUCHED_DOCS))

    # --- process B: its own interpreter, so its own current part and claim set
    cad_common._CUR["doc_id"] = None
    cad_common._PROCESS_DOCS.clear()
    cad_common._TOUCHED_DOCS.clear()
    with pytest.raises(cad_common.DocRefError) as exc:
        cad_common._doc(None)
    assert exc.value.code == "doc_owned_by_other_client"

    # B does what the error told it to: make its own part.
    open_docs.append({"doc_id": "doc-cpp00000007", "name": "Housing",
                      "origin_kind": "mcp"})
    cad_common._CUR["doc_id"] = "doc-cpp00000007"
    cad_common.note_touched_doc("doc-cpp00000007")
    assert cad_common._doc(None) == "doc-cpp00000007"

    # --- back in A: two docs are open now, and A still lands in its own.
    cad_common._CUR["doc_id"] = a_state[0]
    cad_common._PROCESS_DOCS.clear()
    cad_common._PROCESS_DOCS.update(a_state[1])
    cad_common._TOUCHED_DOCS[:] = a_state[2]
    assert cad_common._doc(None) == "doc-cpp00000001"


def test_a_claim_dies_with_its_document_so_a_recycled_id_is_not_adopted(open_docs, daemon_mode):
    """Engine doc_ids are a per-daemon counter and reset when the daemon restarts, so a
    claim outliving its document would eventually name a PEER's fresh "doc-cpp00000001"
    and hand the adopt the false positive it exists to prevent. Every `_doc()` reconciles
    the claim set against what is actually open, so it cannot."""
    daemon_mode(True)
    open_docs.append({"doc_id": "doc-cpp00000001", "name": "Bracket",
                      "origin_kind": "mcp"})
    cad_common.note_touched_doc("doc-cpp00000001")     # our own part
    assert cad_common._doc(None) == "doc-cpp00000001"

    # Our part closes and the daemon restarts, so the id counter starts over.
    open_docs.clear()
    cad_common._CUR["doc_id"] = None
    with pytest.raises(cad_common.DocRefError) as gone:
        cad_common._doc(None)                          # this call prunes the dead claim
    assert gone.value.code == "no_open_documents"
    assert "doc-cpp00000001" not in cad_common._PROCESS_DOCS

    # A peer now mints a part that reuses the id. It is not ours just because we once
    # held that string.
    open_docs.append({"doc_id": "doc-cpp00000001", "name": "PeerPart",
                      "origin_kind": "mcp"})
    with pytest.raises(cad_common.DocRefError) as exc:
        cad_common._doc(None)
    assert exc.value.code == "doc_owned_by_other_client"


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


def test_agent_slots_do_not_fall_back_to_process_current(monkeypatch):
    monkeypatch.setattr(cad_common._CUR, "_slots", {None: {"doc_id": "process-part"}})
    with cad_common.agent_doc_context("A", scope="turn"):
        assert cad_common.current_doc_id() is None
        cad_common._CUR["doc_id"] = "a-part"
    with cad_common.agent_doc_context("B", scope="turn"):
        assert cad_common.current_doc_id() is None
        cad_common._CUR["doc_id"] = "b-part"
    with cad_common.agent_doc_context("A", scope="turn"):
        assert cad_common.current_doc_id() == "a-part"
    assert cad_common.current_doc_id() == "process-part"


def test_agent_current_resolution_and_doc_scope_restore_own_slot(open_docs, monkeypatch):
    monkeypatch.setattr(cad_common._CUR, "_slots", {None: {"doc_id": None}})
    open_docs += [
        {"doc_id": "doc-cpp00000001", "name": "A"},
        {"doc_id": "doc-cpp00000002", "name": "B"},
    ]
    with cad_common.agent_doc_context("A", scope="turn"):
        cad_common._CUR["doc_id"] = "doc-cpp00000001"
        with cad_common.doc_scope():
            cad_common._CUR["doc_id"] = "doc-cpp00000002"
            with cad_common.agent_doc_context("B", scope="turn"):
                assert cad_common.current_doc_id() is None
                cad_common._CUR["doc_id"] = "doc-cpp00000002"
        assert cad_common._doc(None) == "doc-cpp00000001"
    with cad_common.agent_doc_context("B", scope="turn"):
        assert cad_common._doc(None) == "doc-cpp00000002"
    assert cad_common._CUR["doc_id"] is None


def test_root_agent_slot_is_distinct_from_standalone_mcp(monkeypatch):
    monkeypatch.setattr(cad_common._CUR, "_slots", {None: {"doc_id": "mcp-part"}})
    with cad_common.agent_doc_context(None, scope="turn"):
        assert cad_common.current_doc_id() is None
        cad_common._CUR["doc_id"] = "root-part"
    assert cad_common.current_doc_id() == "mcp-part"
    with cad_common.agent_doc_context(None, scope="turn"):
        assert cad_common.current_doc_id() == "root-part"


def test_same_agent_name_in_separate_turns_has_separate_current_part(monkeypatch):
    monkeypatch.setattr(cad_common._CUR, "_slots", {None: {"doc_id": None}})
    with cad_common.agent_doc_context("Builder", scope="turn-one"):
        cad_common._CUR["doc_id"] = "first-part"
    with cad_common.agent_doc_context("Builder", scope="turn-two"):
        assert cad_common.current_doc_id() is None


def test_foreign_edit_notes_are_consumed_only_by_receiving_agent(monkeypatch):
    from services.mcp import daemon_bridge

    bridge = cad_common.cb
    monkeypatch.setattr(bridge, "_FOREIGN_EDITS_PENDING", {})

    class FakeClient:
        def call(self, method, params):
            return {
                "ok": True,
                "_foreign_edits": [{
                    "doc_id": params["doc_id"], "revision": 8,
                    "source_kind": "gui", "method": "doc_apply_ops",
                }],
            }

    monkeypatch.setattr(daemon_bridge, "client", lambda: FakeClient())
    with cad_common.agent_doc_context("A", scope="turn"):
        result = bridge._call_via_daemon("doc_tree", {"doc_id": "a-part"})
        assert result == {"ok": True}
    with cad_common.agent_doc_context("B", scope="turn"):
        assert "\nnote:" not in cad_common._dumps({"ok": True})
        bridge._call_via_daemon("doc_tree", {"doc_id": "b-part"})
        note = cad_common._dumps({"ok": True})
        assert "b-part" in note and "a-part" not in note
    with cad_common.agent_doc_context("A", scope="turn"):
        note = cad_common._dumps({"ok": True})
        assert "a-part" in note and "b-part" not in note
        assert "\nnote:" not in cad_common._dumps({"ok": True})
    assert bridge.consume_foreign_edits() == []
