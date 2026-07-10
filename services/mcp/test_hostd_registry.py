"""Phase-3 hostd doc-registry tests: attach modes, edit-holds, overlap CAS,
event bus, _foreign_edits piggyback, daemon-side autosave + recovery.

Each test uses a scratch AICAD_INSTANCE_KEY (its own daemon, its own
hostd_autosave/<key> dir) so a live dev/installed daemon is never touched.
Skips when no aicad-hostd binary is built (mirrors test_daemon_bridge.py).
"""
from __future__ import annotations

import os
import signal
import threading
import time
import uuid

import pytest

from mcp_server import daemon_bridge
from mcp_server.daemon_bridge import DaemonClient

HOSTD = daemon_bridge.resolve_hostd_bin()

pytestmark = pytest.mark.skipif(HOSTD is None, reason="aicad-hostd not built")

# A minimal valid mutating op (verified against the engine's add_feature schema).
BOX_OPS = [{"op": "add_feature",
            "feature": {"type": "box", "name": "b1",
                        "params": {"length": 10, "width": 10, "height": 10},
                        "refs": {}}}]


_BOX_N = [0]


def _box_ops(name: str) -> list:
    # Each box lands at a fresh offset so it's never swallowed by an earlier one
    # (a fully-buried boss gets rolled back and would NOT advance the revision).
    _BOX_N[0] += 1
    return [{"op": "add_feature",
             "feature": {"type": "box", "name": name,
                         "params": {"length": 5, "width": 5, "height": 5,
                                    "at": [20 * _BOX_N[0], 0, 0]},
                         "refs": {}}}]


@pytest.fixture()
def scratch_daemon(monkeypatch):
    """Fresh instance key + fast linger/hold/autosave; kills the daemon on teardown."""
    key = "pytest-reg-" + uuid.uuid4().hex[:8]
    monkeypatch.setenv("AICAD_INSTANCE_KEY", key)
    monkeypatch.setenv("AICAD_HOSTD_LINGER_SECS", "3")
    monkeypatch.setenv("AICAD_HOSTD_HOLD_SECS", "1")
    monkeypatch.setenv("AI_CAD_AUTOSAVE_INTERVAL_S", "1")
    monkeypatch.delenv("AICAD_HOSTD_SOCKET", raising=False)
    pids: list = []
    yield pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _pid(c: DaemonClient) -> int:
    return c.call("daemon_status")["pid"]


def _events(c: DaemonClient, name: str) -> list:
    """Buffered push events of one type (a cheap call first drains the socket)."""
    c.call("ping")
    return [e for e in c.poll_events() if e.get("event") == name]


# ------------------------------------------------------------- attach modes


def test_ro_attach_rejects_mutation(scratch_daemon):
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    doc = a.call("doc_new", {"name": "ro-test"})["doc_id"]

    assert b.call("attach_doc", {"doc_id": doc, "mode": "ro"}).get("ok") is True
    r = b.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    assert r.get("_is_error") is True
    assert r.get("code") == "read_only"

    # Reads stay fine from an ro attach.
    assert "docs" in b.call("doc_list", {})
    assert b.call("doc_tree", {"doc_id": doc}).get("_is_error") is None


def test_second_rw_attach_warns(scratch_daemon):
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    doc = a.call("doc_new", {"name": "two-writers"})["doc_id"]
    r = b.call("attach_doc", {"doc_id": doc, "mode": "rw"})
    assert r.get("ok") is True, "second rw attach must SUCCEED (advisory-plus)"
    assert "another writer" in (r.get("warning") or "")


# -------------------------------------------------------------- overlap CAS


def test_two_writer_cas_stale_revision(scratch_daemon):
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    doc = a.call("doc_new", {"name": "cas"})["doc_id"]
    # B becomes the second writer at revision 0.
    assert b.call("attach_doc", {"doc_id": doc, "mode": "rw"}).get("ok") is True

    # A edits -> revision 1. B's next apply WITHOUT base_revision gets B's
    # last-seen (0) injected by the daemon -> engine stale_revision CAS fires.
    r_a = a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    assert r_a.get("revision") == 1
    r_b = b.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("b2")})
    assert r_b.get("_is_error") is True
    assert r_b.get("code") == "stale_revision"

    # CAS-retry path: B re-reads (advancing its last-seen) and the retry lands.
    b.call("doc_tree", {"doc_id": doc})
    r_b2 = b.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("b3")})
    assert r_b2.get("_is_error") is None
    assert r_b2.get("revision") == 2


def test_single_writer_no_cas_injection(scratch_daemon):
    """One writer: passthrough stays byte-identical — no base_revision means no
    stale_revision even after many edits."""
    a = DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    doc = a.call("doc_new", {"name": "solo"})["doc_id"]
    for i in range(3):
        r = a.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops(f"s{i}")})
        assert r.get("_is_error") is None, r


# --------------------------------------------------------------- edit holds


def test_edit_hold_blocks_then_expires(scratch_daemon):
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    doc = a.call("doc_new", {"name": "hold"})["doc_id"]
    b.call("subscribe", {"doc_ids": "*"})

    assert a.call("edit_hold", {"doc_id": doc}).get("ok") is True
    r = b.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    assert r.get("_is_error") is True
    assert r.get("code") == "busy_preview"
    assert r.get("retry_ms") == 500

    # AICAD_HOSTD_HOLD_SECS=1: the hold expires and the accept-loop tick emits
    # doc.lease_change; B's mutation then proceeds.
    time.sleep(1.8)
    lease = _events(b, "doc.lease_change")
    assert any(e["data"].get("state") == "expired" for e in lease), lease
    r2 = b.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("after")})
    assert r2.get("_is_error") is None, r2


def test_edit_release_and_holder_passthrough(scratch_daemon):
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    doc = a.call("doc_new", {"name": "hold2"})["doc_id"]
    assert a.call("edit_hold", {"doc_id": doc}).get("ok") is True
    # The holder itself keeps editing (refreshes the hold).
    assert a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS}).get("revision") == 1
    assert a.call("edit_release", {"doc_id": doc}).get("ok") is True
    # Released: B may edit immediately.
    r = b.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("b")})
    assert r.get("_is_error") is None, r


# ---------------------------------------------------------------- event bus


def test_doc_changed_event_reaches_subscriber(scratch_daemon):
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    b.call("subscribe", {"doc_ids": "*"})

    doc = a.call("doc_new", {"name": "events"})["doc_id"]
    time.sleep(0.2)
    opened = _events(b, "doc.opened")
    assert any(e["data"].get("doc_id") == doc for e in opened), opened

    a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    time.sleep(0.2)
    changed = _events(b, "doc.changed")
    assert changed, "subscriber must receive doc.changed for a foreign edit"
    d = changed[-1]["data"]
    assert d["doc_id"] == doc and d["revision"] == 1
    assert d["method"] == "doc_apply_ops"
    assert "aicm_path" in d  # live-preview payload for the GUI

    # The editing client itself must NOT receive its own doc.changed.
    a.call("subscribe", {"doc_ids": "*"})
    a.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("x2")})
    time.sleep(0.2)
    assert not _events(a, "doc.changed")


def test_doc_closed_event(scratch_daemon):
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    b.call("subscribe", {"doc_ids": "*"})
    doc = a.call("doc_new", {"name": "closer"})["doc_id"]
    a.call("doc_close", {"doc_id": doc})
    time.sleep(0.2)
    closed = _events(b, "doc.closed")
    assert any(e["data"].get("doc_id") == doc for e in closed), closed


def test_disconnect_releases_hold(scratch_daemon):
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    doc = a.call("doc_new", {"name": "dc"})["doc_id"]
    b.call("subscribe", {"doc_ids": "*"})
    assert a.call("edit_hold", {"doc_id": doc}).get("ok") is True
    a.close()
    time.sleep(0.4)
    lease = _events(b, "doc.lease_change")
    assert any(e["data"].get("state") == "disconnected" for e in lease), lease
    # B can edit right away — the dead client's hold died with it.
    r = b.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    assert r.get("_is_error") is None, r


# ------------------------------------------------------------ foreign edits


def test_foreign_edits_piggyback(scratch_daemon):
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    doc = a.call("doc_new", {"name": "foreign"})["doc_id"]
    b.call("doc_tree", {"doc_id": doc})  # B's last-seen = rev 0

    a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})  # A -> rev 1

    r = b.call("doc_tree", {"doc_id": doc})
    fe = r.get("_foreign_edits")
    assert fe and fe[0]["doc_id"] == doc and fe[0]["revision"] == 1, r.keys()
    assert fe[0]["method"] == "doc_apply_ops"
    # One-shot: B is now caught up; the next call carries no stale flag.
    assert "_foreign_edits" not in b.call("doc_tree", {"doc_id": doc})


def test_foreign_edits_surface_in_tool_note(scratch_daemon, monkeypatch):
    """core_bridge strips _foreign_edits into a buffer; cad_tools._dumps appends
    the human-readable note."""
    monkeypatch.setenv("AI_CAD_ENGINE_DAEMON", "1")
    monkeypatch.setattr(daemon_bridge, "_client", None)
    from mcp_server import cad_tools, core_bridge

    a = DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    doc = core_bridge.call("doc_new", {"name": "note"})["doc_id"]  # via daemon route
    core_bridge.call("doc_tree", {"doc_id": doc})
    a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})  # foreign writer

    r = core_bridge.call("doc_tree", {"doc_id": doc})
    assert "_foreign_edits" not in r, "bridge must strip the marker from results"
    note = cad_tools._dumps({"ok": True})
    assert "edited part" in note and doc in note, note
    # Consumed: the next output is clean.
    assert "edited part" not in cad_tools._dumps({"ok": True})


# ------------------------------------------------------- autosave + recovery


def test_autosave_recovery_id_remap(scratch_daemon):
    a = DaemonClient()
    a.connect()
    pid = _pid(a)
    doc = a.call("doc_new", {"name": "recover-me"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    time.sleep(2.5)  # AI_CAD_AUTOSAVE_INTERVAL_S=1 -> at least one tick saved it

    os.kill(pid, signal.SIGKILL)  # hard crash: no final save, autosave is all we have
    time.sleep(0.3)

    b = DaemonClient()
    b.connect()  # respawns a fresh daemon
    pid2 = _pid(b)
    assert pid2 != pid
    scratch_daemon.append(pid2)
    # Wait for the recovery pass (runs right after the engine loads).
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if b.call("daemon_status").get("recovery") == "done":
            break
        time.sleep(0.2)
    info = b.call("hello", {"client": "test", "pid": os.getpid(), "proto": 1})
    remap = info.get("id_remap") or {}
    assert doc in remap, f"old id must be remapped, got {remap}"
    new_id = remap[doc]
    docs = {d["doc_id"]: d for d in b.call("doc_list", {})["docs"]}
    assert new_id in docs
    assert docs[new_id]["name"] == "recover-me"
    assert docs[new_id]["revision"] >= 0
    # Content survived: the box feature is in the recovered tree.
    tree = b.call("doc_tree", {"doc_id": new_id})
    assert "b1" in str(tree.get("tree")), "recovered doc lost its geometry"


def test_recover_autosaved_uses_daemon_remap(scratch_daemon, monkeypatch):
    """cad_tools.recover_autosaved in daemon mode consumes the hello id_remap
    into _DOC_ALIASES instead of re-opening files."""
    monkeypatch.setenv("AI_CAD_ENGINE_DAEMON", "1")
    monkeypatch.setattr(daemon_bridge, "_client", None)
    from mcp_server import cad_tools, core_bridge

    doc = core_bridge.call("doc_new", {"name": "alias-me"})["doc_id"]
    pid = daemon_bridge.client().call("daemon_status")["pid"]
    core_bridge.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    time.sleep(2.5)
    os.kill(pid, signal.SIGKILL)
    time.sleep(0.3)

    monkeypatch.setattr(daemon_bridge, "_client", None)  # fresh connection
    recovered = cad_tools.recover_autosaved()
    scratch_daemon.append(daemon_bridge.client().call("daemon_status")["pid"])
    assert any(r["original_doc_id"] == doc for r in recovered), recovered
    new_id = next(r["doc_id"] for r in recovered if r["original_doc_id"] == doc)
    # The agent's OLD handle still resolves through the alias chain.
    assert cad_tools._resolve_alias(doc) == new_id


def test_no_python_autosave_timer_in_daemon_mode(scratch_daemon, monkeypatch):
    monkeypatch.setenv("AI_CAD_ENGINE_DAEMON", "1")
    from mcp_server import cad_tools

    cad_tools.start_periodic_autosave()
    time.sleep(0.1)
    names = [t.name for t in threading.enumerate()]
    assert "aicad-autosave" not in names, names
    assert cad_tools._PERIODIC_TIMER[0] is None
