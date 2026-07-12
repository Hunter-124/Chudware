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


# ----------------------------------------------------------- ownership/origin


def test_origin_kind_in_doc_list(scratch_daemon):
    """doc_list stamps each entry with the minting client's kind (user vs AI)."""
    a = DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    doc = a.call("doc_new", {"name": "origin"})["doc_id"]
    entry = next(e for e in a.call("doc_list", {})["docs"] if e.get("doc_id") == doc)
    # A DaemonClient registers kind "mcp" (its hello sends {"client": "mcp"}).
    assert entry.get("origin_kind") == "mcp", entry


def test_disconnect_emits_last_writer_detached(scratch_daemon):
    """An agent (last rw writer) disconnecting tells ro viewers their mirror is
    final via doc.closed last_writer_detached — the signal the GUI auto-closes on.
    Without it a crashed agent's mirror tab would live forever."""
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    b.call("subscribe", {"doc_ids": "*"})
    doc = a.call("doc_new", {"name": "abandoned"})["doc_id"]
    # B is a read-only viewer (the GUI mirror), so A's rw is the only writer.
    b.call("attach_doc", {"doc_id": doc, "mode": "ro"})
    a.close()
    time.sleep(0.4)
    closed = _events(b, "doc.closed")
    assert any(e["data"].get("doc_id") == doc and
               e["data"].get("reason") == "last_writer_detached" for e in closed), closed


def test_doc_close_degrades_to_detach_with_other_writer(scratch_daemon):
    """A co-edited doc must not be destroyed by one client's doc_close: it degrades
    to a detach unless force:true. Protects a user-adopted doc from an agent's
    close_part (and vice versa)."""
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    doc = a.call("doc_new", {"name": "coedit"})["doc_id"]
    assert b.call("attach_doc", {"doc_id": doc, "mode": "rw"}).get("ok") is True

    # A closes while B still holds rw -> degrade to detach, doc survives for B.
    r = a.call("doc_close", {"doc_id": doc})
    assert r.get("detached") is True, r
    assert b.call("doc_tree", {"doc_id": doc}).get("_is_error") is None, "doc must survive"
    assert b.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS}).get("_is_error") is None

    # A can still FORCE the close even though B holds rw (explicit override).
    assert a.call("doc_close", {"doc_id": doc, "force": True}).get("_is_error") is None
    time.sleep(0.1)
    assert all(e.get("doc_id") != doc for e in b.call("doc_list", {})["docs"]), "doc gone"


def test_gc_retires_only_undelivered_docs(scratch_daemon, monkeypatch, tmp_path):
    """The load-bearing 'no data loss, no nag' guarantee behind agent-finished auto-close:
    when an abandoned doc is GC'd, one SAVED to a real user file closes clean (NO recovery
    entry -> the user is never nagged about already-delivered AI work), while an unsaved doc
    IS retired to the orphan tier (recoverable). Discriminates ship from regression."""
    monkeypatch.setenv("AICAD_DOC_GC_GRACE_S", "1")  # reap abandoned docs fast
    keep = DaemonClient()
    keep.connect()  # stays connected so the daemon doesn't linger-exit mid-test
    scratch_daemon.append(_pid(keep))
    a = DaemonClient()
    a.connect()

    saved = a.call("doc_new", {"name": "delivered"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": saved, "ops": BOX_OPS})
    out = str(tmp_path / "delivered.step+")
    assert a.call("doc_save", {"doc_id": saved, "path": out}).get("_is_error") is None

    unsaved = a.call("doc_new", {"name": "scratch"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": unsaved, "ops": _box_ops("u")})

    a.close()  # both docs unattached -> GC grace (1s) -> retire only the undelivered one

    deadline = time.time() + 8
    ids: set = set()
    while time.time() < deadline:
        time.sleep(0.5)
        ids = {e.get("doc_id") for e in keep.call("recovery_list").get("entries", [])}
        if unsaved in ids:
            break
    assert unsaved in ids, "an unsaved abandoned doc MUST be retired for recovery"
    assert saved not in ids, "a doc saved to a real file must NOT flood the recovery prompt"


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


def test_crash_recovery_prompts_via_orphan_tier(scratch_daemon):
    """A hard CRASH is the only path that feeds the recovery tier. The survivor is
    NOT auto-opened (that silently resurrected zombie parts) — it waits in
    recovery_list until an explicit recovery_open, which mints a fresh id, carries
    both doc_id + original_doc_id, and rebinds the old handle in hello."""
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

    # NOT auto-opened: no id_remap at hello yet, doc absent from doc_list...
    info = b.call("hello", {"client": "test", "pid": os.getpid(), "proto": 1})
    assert not (info.get("id_remap") or {}), info.get("id_remap")
    assert "recover-me" not in [d["name"] for d in b.call("doc_list", {})["docs"]]
    # ...but present in the recovery tier with its real name preserved.
    entries = b.call("recovery_list", {}).get("entries") or []
    entry = next((e for e in entries if e.get("name") == "recover-me"), None)
    assert entry is not None, entries
    assert entry.get("doc_id") == doc, entry  # meta keeps the ORIGINAL id

    opened = b.call("recovery_open", {"all": True}).get("opened") or []
    o = next((x for x in opened if x.get("original_doc_id") == doc), None)
    assert o is not None, opened
    new_id = o["doc_id"]
    assert isinstance(new_id, str) and new_id, o  # GUI mirrors tabs from this
    docs = {d["doc_id"]: d for d in b.call("doc_list", {})["docs"]}
    assert docs[new_id]["name"] == "recover-me"
    tree = b.call("doc_tree", {"doc_id": new_id})
    assert "b1" in str(tree.get("tree")), "recovered doc lost its geometry"
    # Consumed: a second recovery_open must not duplicate it.
    assert b.call("recovery_list", {}).get("entries") == []


def test_recover_autosaved_uses_daemon_remap(scratch_daemon, monkeypatch):
    """After a crash, cad_tools.recover_autosaved (daemon mode) restores the
    survivor from the orphan tier (recovery_open) and aliases the agent's old
    handle to the freshly-minted id."""
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
    # include_orphaned=True == the explicit recover_parts path; startup would NOT
    # auto-open the tier (that is what recover_parts is for).
    recovered = cad_tools.recover_autosaved(include_orphaned=True)
    scratch_daemon.append(daemon_bridge.client().call("daemon_status")["pid"])
    assert any(r["original_doc_id"] == doc for r in recovered), recovered
    new_id = next(r["doc_id"] for r in recovered if r["original_doc_id"] == doc)
    # The agent's OLD handle still resolves through the alias chain.
    assert cad_tools._resolve_alias(doc) == new_id


# --------------------------------------------- doc lifecycle (GC + orphan tier)


def _wait_daemon_gone(pid: int, timeout: float = 20.0) -> bool:
    """True once `pid` no longer runs. A detached hostd whose parent is this
    pytest process lingers as a zombie after exit — treat state Z as gone."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().split(")")[-1].split()[0] == "Z":
                    return True
        except OSError:
            return True
        time.sleep(0.2)
    return False


def test_clean_shutdown_discards_docs(scratch_daemon):
    """A CLEAN shutdown discards unsaved work — recovery is crash-only. The doc
    must not auto-open AND must not land in the recovery tier on the next start
    (the GUI's quit warning is the front-line guard against data loss)."""
    a = DaemonClient()
    a.connect()
    pid = _pid(a)
    scratch_daemon.append(pid)
    doc = a.call("doc_new", {"name": "discard-on-quit"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})

    assert a.call("daemon_drain", {}).get("ok") is True
    a.close()  # last client gone -> drained daemon exits cleanly, discarding
    assert _wait_daemon_gone(pid), "drained daemon did not exit"

    b = DaemonClient()
    b.connect()  # fresh daemon
    pid2 = _pid(b)
    assert pid2 != pid
    scratch_daemon.append(pid2)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if b.call("daemon_status").get("recovery") == "done":
            break
        time.sleep(0.2)

    # Clean shutdown is not a crash: nothing auto-opens AND nothing is recoverable.
    info = b.call("hello", {"client": "test", "pid": os.getpid(), "proto": 1})
    assert not (info.get("id_remap") or {}), info.get("id_remap")
    names = [d["name"] for d in b.call("doc_list", {})["docs"]]
    assert "discard-on-quit" not in names, names
    entries = b.call("recovery_list", {}).get("entries") or []
    assert not any(e.get("name") == "discard-on-quit" for e in entries), entries


def test_doc_close_kills_autosave_artifact(scratch_daemon):
    """Explicitly closed docs stay closed: even a hard crash right after
    doc_close must not resurrect the doc (its autosave artifact is deleted)."""
    a = DaemonClient()
    a.connect()
    pid = _pid(a)
    scratch_daemon.append(pid)
    doc = a.call("doc_new", {"name": "closed-for-good"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    time.sleep(2.5)  # AI_CAD_AUTOSAVE_INTERVAL_S=1 -> the autosave file exists
    assert a.call("doc_close", {"doc_id": doc}).get("_is_error") is None

    os.kill(pid, signal.SIGKILL)  # crash AFTER the close
    time.sleep(0.3)

    b = DaemonClient()
    b.connect()
    scratch_daemon.append(_pid(b))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if b.call("daemon_status").get("recovery") == "done":
            break
        time.sleep(0.2)
    info = b.call("hello", {"client": "test", "pid": os.getpid(), "proto": 1})
    assert doc not in (info.get("id_remap") or {}), "closed doc resurrected"
    names = [d["name"] for d in b.call("doc_list", {})["docs"]]
    assert "closed-for-good" not in names, names
    entries = b.call("recovery_list", {}).get("entries") or []
    assert not any(e.get("name") == "closed-for-good" for e in entries), entries


def test_doc_gc_retires_abandoned_docs(scratch_daemon, monkeypatch):
    """A doc whose every client vanished WITHOUT discarding it (an app/agent crash)
    is RETIRED to the recovery tier after the GC grace, then closed to free memory
    — the work stays recoverable. (A clean app quit sends doc_discard first, so
    only crash-abandoned docs reach this path.)"""
    monkeypatch.setenv("AICAD_DOC_GC_GRACE_S", "1")
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    pid = _pid(a)
    scratch_daemon.append(pid)
    b.connect()  # keeps the daemon alive past a's disconnect
    b.call("subscribe", {"doc_ids": "*"})
    doc = a.call("doc_new", {"name": "abandoned"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    a.close()  # only client attached to the doc goes away (no doc_discard = crash-like)

    deadline = time.monotonic() + 20
    gone = False
    while time.monotonic() < deadline:
        ids = [d["doc_id"] for d in b.call("doc_list", {})["docs"]]
        if doc not in ids:
            gone = True
            break
        time.sleep(0.5)
    assert gone, "GC never closed the abandoned doc"
    # Subscribers heard about it...
    evts = _events(b, "doc.closed")
    assert any(e.get("data", {}).get("doc_id") == doc and
               e.get("data", {}).get("reason") == "gc" for e in evts), evts
    # ...and the work is RETIRED, not lost: it waits in the recovery tier.
    entries = b.call("recovery_list", {}).get("entries") or []
    assert any(e.get("name") == "abandoned" for e in entries), entries


def test_doc_discard_then_gc_leaves_no_recovery(scratch_daemon, monkeypatch):
    """The clean-exit path: a client that doc_discards its doc before leaving must
    NOT leave a recovery entry — that is what keeps a normal app quit from flooding
    the recovery prompt (contrast the crash path above)."""
    monkeypatch.setenv("AICAD_DOC_GC_GRACE_S", "1")
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    b.call("subscribe", {"doc_ids": "*"})
    doc = a.call("doc_new", {"name": "quit-clean"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    a.call("doc_discard", {"doc_id": doc})  # the app quitting cleanly
    a.close()
    time.sleep(3)  # well past the GC grace
    assert b.call("recovery_list", {}).get("entries") == []


def test_doc_discard_closes_abandoned_doc_no_recovery(scratch_daemon):
    """Explicit discard (GUI closing a tab) of a doc no other client holds closes
    it for good — NOT retired to the orphan tier. A deliberately-closed part must
    never flood back as unsaved-recovery on the next launch."""
    a = DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    doc = a.call("doc_new", {"name": "discard-me"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    # `a` minted the doc (implicit rw attach); discarding drops that last attach
    # and, finding no other holder, closes the doc.
    r = a.call("doc_discard", {"doc_id": doc})
    assert r.get("discarded") is True, r
    names = [d["name"] for d in a.call("doc_list", {})["docs"]]
    assert "discard-me" not in names, names
    assert a.call("recovery_list", {}).get("entries") == []


def test_doc_discard_keeps_doc_with_other_holder(scratch_daemon):
    """Discard while another client still holds the doc (an agent still building)
    only drops the caller's view — the doc lives on for the remaining client."""
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    doc = a.call("doc_new", {"name": "still-building"})["doc_id"]  # a: rw writer
    assert b.call("attach_doc", {"doc_id": doc, "mode": "ro"}).get("ok") is True
    # b (a viewer) closes its tab; a still holds the doc -> not reapable -> survives.
    r = b.call("doc_discard", {"doc_id": doc})
    assert r.get("discarded") is False, r
    names = [d["name"] for d in a.call("doc_list", {})["docs"]]
    assert "still-building" in names, names


def test_attached_doc_survives_gc(scratch_daemon, monkeypatch):
    """A doc still attached by ANY client (e.g. a GUI mirror tab) is never GC'd
    when another client disconnects."""
    monkeypatch.setenv("AICAD_DOC_GC_GRACE_S", "1")
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    doc = a.call("doc_new", {"name": "still-watched"})["doc_id"]
    assert b.call("attach_doc", {"doc_id": doc, "mode": "ro"}).get("ok") is True
    a.close()

    time.sleep(3.0)  # well past the 1 s grace
    ids = [d["doc_id"] for d in b.call("doc_list", {})["docs"]]
    assert doc in ids, "GC closed a doc that still had a viewer attached"


def test_no_python_autosave_timer_in_daemon_mode(scratch_daemon, monkeypatch):
    monkeypatch.setenv("AI_CAD_ENGINE_DAEMON", "1")
    from mcp_server import cad_tools

    cad_tools.start_periodic_autosave()
    time.sleep(0.1)
    names = [t.name for t in threading.enumerate()]
    assert "aicad-autosave" not in names, names
    assert cad_tools._PERIODIC_TIMER[0] is None
