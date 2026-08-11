"""Multi-user concurrency + undo policy against a private chudware-hostd.

Covers the web-native multi-user wave (P2):
  * hello ``user: {id, display}`` stamped onto ``doc.changed`` fanout
  * multi-writer CAS (``stale_revision`` + retry → no lost updates)
  * non-gui ``doc_undo`` → ``undo_denied`` (kill-switch ``CHUDWARE_UNDO_AGENTS=1``)
  * gui ``doc_save`` fans out ``doc.history_cleared`` and empties undo

Harness mirrors ``test_hostd_registry.py`` / ``test_live_preview.py`` (scratch
``CHUDWARE_INSTANCE_KEY``, connect-or-spawn via ``DaemonClient``, dual clients,
``subscribe {"doc_ids": "*"}``). Box ops match ``test_multiagent_overlap.py``.

Run (after a rebuilt hostd lands the wave features)::

    python -m pytest services/mcp/test_multiuser_undo.py -q

Or with an explicit binary::

    CHUDWARE_HOSTD_BIN=/path/to/chudware-hostd \\
      python -m pytest services/mcp/test_multiuser_undo.py -q

Do NOT point this at a live chudwareweb daemon — each test spawns its own
instance key. Orchestrator runs this after the daemon rebuild; do not gate
on a pre-wave binary.
"""
from __future__ import annotations

import os
import signal
import time
import uuid
from pathlib import Path

import pytest

from services.mcp import daemon_bridge
from services.mcp.daemon_bridge import DaemonClient

HOSTD = daemon_bridge.resolve_hostd_bin()

pytestmark = pytest.mark.skipif(HOSTD is None, reason="chudware-hostd not built")

# Minimal valid mutating op (same shape as test_multiagent_overlap / registry).
_BOX_N = [0]


def _box_ops(name: str) -> list:
    # Fresh offset so successive bosses aren't swallowed by an earlier solid
    # (buried bosses roll back and would not advance revision).
    _BOX_N[0] += 1
    return [{"op": "add_feature",
             "feature": {"type": "box", "name": name,
                         "params": {"length": 5, "width": 5, "height": 5,
                                    "at": [20 * _BOX_N[0], 0, 0]},
                         "refs": {}}}]


@pytest.fixture()
def scratch_daemon(monkeypatch):
    """Private instance key + short linger; SIGKILL any daemon we started."""
    key = "pytest-mu-" + uuid.uuid4().hex[:8]
    monkeypatch.setenv("CHUDWARE_INSTANCE_KEY", key)
    monkeypatch.setenv("CHUDWARE_HOSTD_LINGER_SECS", "3")
    monkeypatch.setenv("CHUDWARE_HOSTD_HOLD_SECS", "1")
    monkeypatch.setenv("CHUDWARE_AUTOSAVE_INTERVAL_S", "0")
    monkeypatch.delenv("CHUDWARE_HOSTD_SOCKET", raising=False)
    # Default: agent/mcp undo is denied. Individual tests may re-enable.
    monkeypatch.delenv("CHUDWARE_UNDO_AGENTS", raising=False)
    pids: list = []
    yield pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _pid(c: DaemonClient) -> int:
    return c.call("daemon_status")["pid"]


def _hello(c: DaemonClient, client: str, user_id: str, display: str) -> dict:
    """Re-hello so the daemon stores client kind + user identity on this conn."""
    return c.call("hello", {
        "client": client,
        "pid": os.getpid(),
        "proto": 1,
        "user": {"id": user_id, "display": display},
    })


def _gui(user_id: str, display: str) -> DaemonClient:
    c = DaemonClient()
    c.connect()
    _hello(c, "gui", user_id, display)
    c.call("subscribe", {"doc_ids": "*"})
    return c


def _mcp_client() -> DaemonClient:
    """Default connect already hellos as client=mcp; re-hello without user."""
    c = DaemonClient()
    c.connect()
    c.call("hello", {"client": "mcp", "pid": os.getpid(), "proto": 1})
    return c


def _events(c: DaemonClient, name: str) -> list:
    c.call("ping")
    return [e for e in c.poll_events() if e.get("event") == name]


def _wait_events(c: DaemonClient, name: str, timeout_s: float = 2.0) -> list:
    out: list = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        c.call("ping")
        out += [e for e in c.poll_events() if e.get("event") == name]
        if out:
            return out
        time.sleep(0.05)
    return out


def _user_of(event: dict) -> dict:
    data = event.get("data") or {}
    user = data.get("user") or {}
    return {
        "id": str(user.get("id") or ""),
        "display": str(user.get("display") or ""),
    }


def _feature_count(resp: dict) -> int:
    rebuild = resp.get("rebuild") or {}
    fs = rebuild.get("feature_status")
    if isinstance(fs, list):
        return len(fs)
    tree = resp.get("tree")
    if isinstance(tree, dict):
        feats = tree.get("features") or tree.get("children") or []
        if isinstance(feats, list):
            return len(feats)
    return int(resp.get("revision") or 0)


# -------------------------------------------------------------- scenario 1+2


def test_two_gui_users_interleave_and_stamp_user(scratch_daemon):
    """A creates; B sees doc.opened. Interleaved applies fan out doc.changed
    with user.id/display matching the writer. CAS loser gets stale_revision;
    retry lands — final feature count equals total applied (no lost updates)."""
    a = _gui("a@x", "Alice")
    scratch_daemon.append(_pid(a))
    b = _gui("b@x", "Bob")

    # A mints the doc; B must receive doc.opened.
    doc = a.call("doc_new", {"name": "MultiUserPart"})["doc_id"]
    opened = _wait_events(b, "doc.opened")
    assert any((e.get("data") or {}).get("doc_id") == doc for e in opened), opened

    # Second writer attach (advisory-plus) so CAS injection is live.
    assert b.call("attach_doc", {"doc_id": doc, "mode": "rw"}).get("ok") is True

    # Drain any open noise so the next changed events are clean.
    _events(a, "doc.changed")
    _events(b, "doc.changed")

    # --- A writes at rev 0 → rev 1; B (still last-seen 0) receives the event ---
    r_a = a.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("alice1")})
    assert not r_a.get("_is_error"), r_a
    assert r_a.get("revision") == 1
    changed_b = _wait_events(b, "doc.changed")
    assert changed_b, "B must receive A's doc.changed"
    d = changed_b[-1]["data"]
    assert d.get("doc_id") == doc and d.get("revision") == 1
    assert _user_of(changed_b[-1]) == {"id": "a@x", "display": "Alice"}, changed_b[-1]

    # --- B applies without resync → stale_revision (CAS) ---
    r_b = b.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("bob_stale")})
    assert r_b.get("_is_error") is True, r_b
    assert r_b.get("code") == "stale_revision", r_b

    # CAS-retry: resync last-seen via doc_tree, then apply lands as rev 2.
    b.call("doc_tree", {"doc_id": doc})
    r_b2 = b.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("bob1")})
    assert not r_b2.get("_is_error"), r_b2
    assert r_b2.get("revision") == 2

    changed_a = _wait_events(a, "doc.changed")
    assert changed_a, "A must receive B's doc.changed"
    d2 = changed_a[-1]["data"]
    assert d2.get("doc_id") == doc and d2.get("revision") == 2
    assert _user_of(changed_a[-1]) == {"id": "b@x", "display": "Bob"}, changed_a[-1]

    # --- Interleave more writes (each resyncs first so CAS stays happy) ---
    a.call("doc_tree", {"doc_id": doc})
    r_a2 = a.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("alice2")})
    assert not r_a2.get("_is_error"), r_a2
    assert r_a2.get("revision") == 3
    ch_b2 = _wait_events(b, "doc.changed")
    assert ch_b2 and _user_of(ch_b2[-1]) == {"id": "a@x", "display": "Alice"}

    b.call("doc_tree", {"doc_id": doc})
    r_b3 = b.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("bob2")})
    assert not r_b3.get("_is_error"), r_b3
    assert r_b3.get("revision") == 4
    ch_a2 = _wait_events(a, "doc.changed")
    assert ch_a2 and _user_of(ch_a2[-1]) == {"id": "b@x", "display": "Bob"}

    # Final: 4 successful applies (1 stale was not applied) — no lost updates.
    tree = a.call("doc_tree", {"doc_id": doc})
    assert not tree.get("_is_error"), tree
    final_rev = int(tree.get("revision") or r_b3.get("revision") or 0)
    assert final_rev == 4, f"lost update? revision={final_rev} tree={tree!r}"
    n = _feature_count(r_b3) or _feature_count(tree)
    if n:
        assert n == 4, f"lost update? feature_count={n} tree={tree!r} last={r_b3!r}"


# -------------------------------------------------------------- scenario 3


def test_mcp_doc_undo_denied(scratch_daemon):
    """Non-gui (client=mcp) doc_undo is refused with undo_denied."""
    gui = _gui("a@x", "Alice")
    scratch_daemon.append(_pid(gui))
    mcp = _mcp_client()

    doc = gui.call("doc_new", {"name": "UndoDenied"})["doc_id"]
    r = gui.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("keep")})
    assert not r.get("_is_error"), r

    # MCP attaches rw so the call reaches the undo gate (not read_only).
    assert mcp.call("attach_doc", {"doc_id": doc, "mode": "rw"}).get("ok") is True
    denied = mcp.call("doc_undo", {"doc_id": doc})
    assert denied.get("_is_error") is True, denied
    assert denied.get("code") == "undo_denied", denied


def test_mcp_doc_undo_allowed_with_kill_switch(scratch_daemon, monkeypatch):
    """CHUDWARE_UNDO_AGENTS=1 on the DAEMON process restores agent/mcp undo.

    DaemonClient spawns hostd with the ambient env, so setenv BEFORE the first
    connect of this test (scratch_daemon already yielded a fresh instance key).
    """
    monkeypatch.setenv("CHUDWARE_UNDO_AGENTS", "1")

    gui = _gui("a@x", "Alice")
    scratch_daemon.append(_pid(gui))
    mcp = _mcp_client()

    doc = gui.call("doc_new", {"name": "UndoAgentsOn"})["doc_id"]
    r = gui.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("to-undo")})
    assert not r.get("_is_error"), r
    rev_after = int(r.get("revision") or 0)
    assert rev_after >= 1

    assert mcp.call("attach_doc", {"doc_id": doc, "mode": "rw"}).get("ok") is True
    undid = mcp.call("doc_undo", {"doc_id": doc})
    assert not undid.get("_is_error"), undid
    # Successful ops-response (revision present); never undo_denied under the kill-switch.
    assert undid.get("code") != "undo_denied"
    assert "revision" in undid, undid


# -------------------------------------------------------------- scenario 4


def test_gui_save_clears_history_for_all_clients(scratch_daemon, tmp_path):
    """gui doc_save → both subscribers get doc.history_cleared; further undo
    is empty / no-op / error (no history left)."""
    a = _gui("a@x", "Alice")
    scratch_daemon.append(_pid(a))
    b = _gui("b@x", "Bob")

    doc = a.call("doc_new", {"name": "SaveClearsHistory"})["doc_id"]
    _wait_events(b, "doc.opened")
    assert b.call("attach_doc", {"doc_id": doc, "mode": "rw"}).get("ok") is True

    r = a.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("pre-save")})
    assert not r.get("_is_error"), r
    # Prove undo works before save (gui is allowed).
    undid = a.call("doc_undo", {"doc_id": doc})
    assert not undid.get("_is_error"), undid
    # Re-apply so there is history again, then save.
    r2 = a.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("pre-save-2")})
    assert not r2.get("_is_error"), r2

    _events(a, "doc.history_cleared")
    _events(b, "doc.history_cleared")

    out = str(tmp_path / "cleared.step+")
    saved = a.call("doc_save", {"doc_id": doc, "path": out})
    assert not saved.get("_is_error"), saved
    assert Path(out).is_file(), f"doc_save did not write {out}: {saved!r}"

    # Contract: both gui clients receive doc.history_cleared {doc_id} (broadcast,
    # not except-origin like doc.changed).
    cleared_a = _wait_events(a, "doc.history_cleared", timeout_s=3.0)
    cleared_b = _wait_events(b, "doc.history_cleared", timeout_s=3.0)
    assert cleared_a, "writer A must receive doc.history_cleared after gui doc_save"
    assert cleared_b, "subscriber B must receive doc.history_cleared after gui doc_save"
    for ev in (cleared_a + cleared_b):
        data = ev.get("data") or {}
        assert data.get("doc_id") == doc, ev

    # Subsequent undo: empty history — structured error OR a no-op that does not
    # drop below the post-save revision / feature count.
    rev_saved = int(r2.get("revision") or 0)
    post = a.call("doc_undo", {"doc_id": doc})
    if post.get("_is_error"):
        code = str(post.get("code") or "")
        msg = str(post.get("message") or "").lower()
        assert (
            code in {"nothing_to_undo", "empty_history", "invalid_op", "no_history"}
            or "undo" in msg
            or "history" in msg
        ), post
    else:
        rev_post = int(post.get("revision") or 0)
        # No-op: revision stays at/after the save point (undo must not roll back).
        assert rev_post >= rev_saved, (
            f"post-save undo rolled revision back: saved={rev_saved} post={rev_post} {post!r}"
        )
