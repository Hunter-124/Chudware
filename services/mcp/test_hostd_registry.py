"""Hostd doc-registry tests: attach modes, edit-holds, sequential writers,
event bus, _foreign_edits piggyback, daemon-side autosave + recovery.

Each test uses a scratch CHUDWARE_INSTANCE_KEY (its own daemon, its own
hostd_autosave/<key> dir) so a live dev/installed daemon is never touched.
Skips when no chudware-hostd binary is built (mirrors test_daemon_bridge.py).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import tempfile
import threading
import time
import uuid

import pytest

from services.mcp import daemon_bridge
from services.mcp.daemon_bridge import DaemonClient

HOSTD = daemon_bridge.resolve_hostd_bin()

pytestmark = pytest.mark.skipif(HOSTD is None, reason="chudware-hostd not built")

# A minimal valid mutating op (verified against the engine's add_feature schema).
BOX_OPS = [
    {
        "op": "add_feature",
        "feature": {
            "type": "box",
            "name": "b1",
            "params": {"length": 10, "width": 10, "height": 10},
            "refs": {},
        },
    }
]


_BOX_N = [0]


def _box_ops(name: str) -> list:
    # Each box lands at a fresh offset so it's never swallowed by an earlier one
    # (a fully-buried boss gets rolled back and would NOT advance the revision).
    _BOX_N[0] += 1
    return [
        {
            "op": "add_feature",
            "feature": {
                "type": "box",
                "name": name,
                "params": {
                    "length": 5,
                    "width": 5,
                    "height": 5,
                    "at": [20 * _BOX_N[0], 0, 0],
                },
                "refs": {},
            },
        }
    ]


@pytest.fixture()
def scratch_daemon(monkeypatch):
    """Fresh instance key + fast linger/hold/autosave; kills the daemon on teardown."""
    key = "pytest-reg-" + uuid.uuid4().hex[:8]
    monkeypatch.setenv("CHUDWARE_INSTANCE_KEY", key)
    monkeypatch.setenv("CHUDWARE_HOSTD_LINGER_SECS", "3")
    monkeypatch.setenv("CHUDWARE_HOSTD_HOLD_SECS", "1")
    monkeypatch.setenv("CHUDWARE_AUTOSAVE_INTERVAL_S", "1")
    monkeypatch.delenv("CHUDWARE_HOSTD_SOCKET", raising=False)
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



def test_doc_list_other_writer_distinguishes_writer_groups(scratch_daemon):
    owner = DaemonClient(writer_group="group-a")
    sibling = DaemonClient(writer_group="group-a")
    outsider = DaemonClient(writer_group="group-b")
    plain = DaemonClient(writer_group="")
    owner.connect()
    scratch_daemon.append(_pid(owner))
    for client in (sibling, outsider, plain):
        client.connect()
    try:
        doc = owner.call("doc_new", {"name": "other-writer"})["doc_id"]

        def other_writer(client):
            row = next(
                row for row in client.call("doc_list")["docs"] if row["doc_id"] == doc
            )
            return row["other_writer"]

        assert other_writer(owner) is False
        assert other_writer(sibling) is False
        assert other_writer(outsider) is True
        assert other_writer(plain) is True
        assert sibling.call("attach_doc", {"doc_id": doc, "mode": "rw"})["ok"]
        assert other_writer(owner) is False
        assert other_writer(sibling) is False
        for client in (outsider, plain):
            assert client.call("attach_doc", {"doc_id": doc, "mode": "rw"})["ok"]
            assert other_writer(owner) is True
            assert other_writer(sibling) is True
            assert other_writer(client) is True
            assert client.call("detach_doc", {"doc_id": doc})["ok"]
            assert other_writer(owner) is False
            assert other_writer(sibling) is False
    finally:
        for client in (owner, sibling, outsider, plain):
            client.close()

# ------------------------------------------------------- sequential writers


def test_two_writers_serialize_without_implicit_cas(scratch_daemon):
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    doc = a.call("doc_new", {"name": "cas"})["doc_id"]
    # B becomes the second writer at revision 0.
    assert b.call("attach_doc", {"doc_id": doc, "mode": "rw"}).get("ok") is True

    assert a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})["revision"] == 1
    # Explicit revision guards still work, but the daemon never invents one.
    stale = b.call("doc_apply_ops", {"doc_id": doc, "base_revision": 0, "ops": _box_ops("b2")})
    assert stale.get("_is_error") is True and stale.get("code") == "stale_revision"
    accepted = b.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("b3")})
    assert accepted.get("_is_error") is None and accepted.get("revision") == 2


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


def test_writer_group_parallel_edits_do_not_race_overlap_cas(scratch_daemon):
    """A process's parallel connections are one writer, so overlapping calls on
    a group-only doc must not both inject the same stale base_revision."""
    a = DaemonClient(writer_group="g1")
    b = DaemonClient(writer_group="g1")
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    doc = a.call("doc_new", {"name": "group-parallel"})["doc_id"]
    b.call("attach_doc", {"doc_id": doc, "mode": "rw"})
    for batch in range(3):
        ready = threading.Barrier(2)
        results = [None, None]
        failures = []

        def edit(index, client, ops):
            try:
                ready.wait(timeout=5)
                results[index] = client.call("doc_apply_ops", {"doc_id": doc, "ops": ops})
            except Exception as exc:
                failures.append(exc)

        threads = [
            threading.Thread(
                target=edit,
                args=(index, client, _box_ops(f"parallel-{batch}-{index}")),
                daemon=True,
            )
            for index, client in enumerate((a, b))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(thread.is_alive() for thread in threads), "parallel edits did not finish"
        assert not failures, failures
        assert all(result and result.get("_is_error") is None for result in results), results
        assert sorted(result["revision"] for result in results) == [2 * batch + 1, 2 * batch + 2]
        assert all("_foreign_edits" not in result for result in results), results


def test_writer_group_is_one_writer_and_keeps_connection_fanout(scratch_daemon):
    """Group peers need neither overlap CAS nor foreign-edit warnings, but each
    subscribed connection still receives the other member's live doc.changed."""
    a = DaemonClient(writer_group="g1")
    b = DaemonClient(writer_group="g1")
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    doc = a.call("doc_new", {"name": "group-solo"})["doc_id"]
    attached = b.call("attach_doc", {"doc_id": doc, "mode": "rw"})
    assert attached.get("ok") is True, attached
    assert "warning" not in attached, attached
    a.call("subscribe", {"doc_ids": [doc]})
    b.call("subscribe", {"doc_ids": [doc]})
    assert a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})["revision"] == 1
    changed = _events(b, "doc.changed")
    assert [e["data"]["revision"] for e in changed] == [1], changed
    assert not _events(a, "doc.changed")

    # B has not read a revision-bearing response since revision 0. The shared
    # writer's own edits still land and are never labelled foreign.
    edited = b.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("group-b")})
    assert edited.get("_is_error") is None, edited
    assert edited["revision"] == 2
    assert "_foreign_edits" not in edited, edited
    assert [e["data"]["revision"] for e in _events(a, "doc.changed")] == [2]
    assert not _events(b, "doc.changed")


def test_writer_group_and_outsider_edits_are_sequential(scratch_daemon):
    a = DaemonClient(writer_group="g1")
    b = DaemonClient(writer_group="g1")
    c = DaemonClient(writer_group="")
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    c.connect()
    doc = a.call("doc_new", {"name": "group-cas"})["doc_id"]
    assert b.call("attach_doc", {"doc_id": doc, "mode": "rw"}).get("ok") is True
    outsider = c.call("attach_doc", {"doc_id": doc, "mode": "rw"})
    assert "another writer" in (outsider.get("warning") or ""), outsider
    assert a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})["revision"] == 1
    # Group members share attribution but all writers may submit sequentially.
    edited = b.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("group-cas-b")})
    assert edited.get("_is_error") is None, edited
    assert edited["revision"] == 2
    assert "_foreign_edits" not in edited, edited
    foreign = c.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("group-cas-c")})
    assert foreign.get("_is_error") is None and foreign["revision"] == 3, foreign
    assert foreign["_foreign_edits"][0]["revision"] == 2, foreign

    # A group member's read still catches up attribution for the entire group.
    assert a.call("doc_tree", {"doc_id": doc})["_foreign_edits"][0]["revision"] == 3
    caught_up = b.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("group-retry")})
    assert caught_up.get("_is_error") is None, caught_up
    assert caught_up["revision"] == 4


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

    # CHUDWARE_HOSTD_HOLD_SECS=1: the hold expires and the accept-loop tick emits
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


def test_writer_group_hold_allows_members_and_blocks_outsider(scratch_daemon, monkeypatch):
    monkeypatch.setenv("CHUDWARE_HOSTD_HOLD_SECS", "30")
    a = DaemonClient(writer_group="g1")
    b = DaemonClient(writer_group="g1")
    c = DaemonClient(writer_group="")
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    c.connect()
    doc = a.call("doc_new", {"name": "group-hold"})["doc_id"]
    b.call("attach_doc", {"doc_id": doc, "mode": "rw"})
    assert a.call("edit_hold", {"doc_id": doc}).get("ok") is True
    assert b.call("edit_hold", {"doc_id": doc}).get("ok") is True
    edited = b.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    assert edited.get("_is_error") is None, edited
    for method, params in (
        ("edit_hold", {"doc_id": doc}),
        ("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("blocked")}),
    ):
        blocked = c.call(method, params)
        assert blocked.get("_is_error") is True, blocked
        assert blocked.get("code") == "busy_preview", blocked
    # A's lease is group-owned for editing/releasing, connection-owned for lifetime.
    assert b.call("edit_release", {"doc_id": doc}).get("ok") is True
    assert c.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("released")}).get(
        "_is_error"
    ) is None


@pytest.mark.parametrize("disconnect", [False, True], ids=["detach", "disconnect"])
@pytest.mark.parametrize("holder_leaves", [False, True], ids=["peer-leaves", "holder-leaves"])
def test_writer_group_member_leaving_preserves_remaining_state(
    scratch_daemon, monkeypatch, disconnect, holder_leaves
):
    monkeypatch.setenv("CHUDWARE_HOSTD_HOLD_SECS", "30")
    a = DaemonClient(writer_group="g1")
    b = DaemonClient(writer_group="g1")
    c = DaemonClient(writer_group="")
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    c.connect()
    doc = a.call("doc_new", {"name": "group-leaving"})["doc_id"]
    b.call("attach_doc", {"doc_id": doc, "mode": "rw"})
    c.call("attach_doc", {"doc_id": doc, "mode": "rw"})
    c.call("subscribe", {"doc_ids": [doc]})
    assert a.call("edit_hold", {"doc_id": doc}).get("ok") is True
    # Refresh from B must not transfer the lifetime owner away from A.
    assert b.call("edit_hold", {"doc_id": doc}).get("ok") is True
    assert a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})["revision"] == 1
    leaving, remaining = (a, b) if holder_leaves else (b, a)
    if disconnect:
        leaving.close()
        time.sleep(0.4)
    else:
        assert leaving.call("detach_doc", {"doc_id": doc}).get("ok") is True
    c.call("ping")
    events = c.poll_events()
    assert not [e for e in events if e.get("event") == "doc.closed"], (
        "a remaining group member is still a writer"
    )
    if disconnect and not holder_leaves:
        assert not [
            e for e in events
            if e.get("event") == "doc.lease_change"
            and e["data"].get("state") == "disconnected"
        ], "a non-holder disconnect must not announce the group's lease as released"
    # In both cases the remaining connection retains the group's seen revision.
    edited = remaining.call(
        "doc_apply_ops", {"doc_id": doc, "ops": _box_ops("remaining-member")}
    )
    assert edited.get("_is_error") is None, edited
    assert edited["revision"] == 2
    blocked = c.call("edit_hold", {"doc_id": doc})
    if holder_leaves:
        assert blocked.get("ok") is True, blocked
    else:
        assert blocked.get("code") == "busy_preview", blocked
        assert remaining.call("edit_release", {"doc_id": doc}).get("ok") is True


def test_writer_group_doc_close_preserves_other_member(scratch_daemon):
    a = DaemonClient(writer_group="g1")
    b = DaemonClient(writer_group="g1")
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    doc = a.call("doc_new", {"name": "group-close"})["doc_id"]
    b.call("attach_doc", {"doc_id": doc, "mode": "rw"})
    closed = a.call("doc_close", {"doc_id": doc})
    assert closed.get("detached") is True, closed
    assert b.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS}).get("revision") == 1


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


def test_human_dirty_tracks_revision_and_guarded_saves(scratch_daemon, tmp_path):
    gui = _gui_client(scratch_daemon)
    agent = DaemonClient()
    agent.connect()
    gui.call("subscribe", {"doc_ids": "*"})
    doc = gui.call("doc_new", {"name": "dirty-provenance"})["doc_id"]
    changed = agent.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    assert changed["revision"] == 1
    event = _events(gui, "doc.changed")[-1]["data"]
    assert event["source_kind"] == "mcp" and event["human_dirty"] is False
    row = next(row for row in gui.call("doc_list")["docs"] if row["doc_id"] == doc)
    assert row["origin_kind"] == "gui" and row["human_dirty"] is False
    assert row["saved_revision"] == -1
    agent.call("detach_doc", {"doc_id": doc})
    gui.call("doc_tree", {"doc_id": doc})
    human = gui.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("human")})
    assert human["revision"] == 2
    row = next(row for row in gui.call("doc_list")["docs"] if row["doc_id"] == doc)
    assert row["human_dirty"] is True
    skipped = agent.call("doc_save", {"doc_id": doc, "path": str(tmp_path / "guarded.step+"),
                                      "if_no_human_dirty": True})
    assert skipped.get("code") == "human_dirty", skipped
    snapshot = gui.call("doc_save", {"doc_id": doc, "path": str(tmp_path / "snapshot.step+"),
                                      "preserve_path": True})
    assert snapshot["human_dirty"] is True and snapshot["saved_revision"] == 2
    row = next(row for row in gui.call("doc_list")["docs"] if row["doc_id"] == doc)
    assert row["human_dirty"] is True and row["saved_revision"] == -1
    saved = gui.call("doc_save", {"doc_id": doc, "path": str(tmp_path / "saved.step+")})
    assert saved["saved_revision"] == 2 and saved["human_dirty"] is False
    gui.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("derived"), "origin": "system"})
    row = next(row for row in gui.call("doc_list")["docs"] if row["doc_id"] == doc)
    assert row["human_dirty"] is False and row["saved_revision"] == 2


def test_metadata_only_human_edit_stays_dirty_until_saved(scratch_daemon, tmp_path):
    gui = _gui_client(scratch_daemon)
    observer = DaemonClient()
    observer.connect()
    observer.call("subscribe", {"doc_ids": "*"})
    doc = gui.call("doc_new", {"name": "metadata-only"})["doc_id"]
    gui.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    saved = gui.call("doc_save", {"doc_id": doc, "path": str(tmp_path / "metadata.step+")})
    _events(observer, "doc.changed.notice")
    edit = gui.call("doc_set_meta", {"doc_id": doc, "key": "inspection", "value": {"x": 1}})
    assert edit["revision"] == saved["saved_revision"]
    assert edit["metadata_revision"] > saved["metadata_revision"]
    notices = _events(observer, "doc.changed.notice")
    notice = next(event["data"] for event in notices if event["data"].get("doc_id") == doc)
    assert notice["method"] == "doc_set_meta" and notice["human_dirty"] is True
    assert notice["metadata_revision"] == edit["metadata_revision"]
    row = next(row for row in gui.call("doc_list")["docs"] if row["doc_id"] == doc)
    assert row["human_dirty"] is True and row["saved_metadata_revision"] == saved["metadata_revision"]
    saved_again = gui.call("doc_save", {"doc_id": doc})
    assert saved_again["metadata_revision"] == edit["metadata_revision"]
    assert saved_again["human_dirty"] is False
    same_name = gui.call("doc_set_name", {"doc_id": doc, "name": "metadata-only"})
    assert same_name["metadata_revision"] == edit["metadata_revision"]
    row = next(row for row in gui.call("doc_list")["docs"] if row["doc_id"] == doc)
    assert row["human_dirty"] is False
    renamed = gui.call("doc_set_name", {"doc_id": doc, "name": "renamed"})
    assert renamed["revision"] == saved["saved_revision"]
    assert renamed["metadata_revision"] > saved_again["metadata_revision"]
    row = next(row for row in gui.call("doc_list")["docs"] if row["doc_id"] == doc)
    assert row["human_dirty"] is True
    assert gui.call("doc_snapshot", {"doc_id": doc})["tree"]["name"] == "renamed"


def test_external_rewrite_reloads_clean_doc_and_conflicts_with_human_edits(
    scratch_daemon, tmp_path
):
    gui = _gui_client(scratch_daemon)
    gui.call("subscribe", {"doc_ids": "*"})
    target = tmp_path / "watched.step+"
    doc = gui.call("doc_new", {"name": "watched"})["doc_id"]
    gui.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    assert gui.call("doc_save", {"doc_id": doc, "path": str(target)})["human_dirty"] is False
    source = DaemonClient()
    source.connect()
    replacement_doc = source.call("doc_new", {"name": "replacement"})["doc_id"]
    source.call("doc_apply_ops", {"doc_id": replacement_doc, "ops": _box_ops("external")})
    replacement = tmp_path / "replacement.step+"
    source.call("doc_save", {"doc_id": replacement_doc, "path": str(replacement)})
    backup = tmp_path / "backup.step+"
    shutil.copyfile(replacement, backup)
    _events(gui, "doc.changed")
    os.replace(replacement, target)

    deadline = time.monotonic() + 20
    reloaded = None
    while time.monotonic() < deadline and reloaded is None:
        for event in _events(gui, "doc.changed"):
            data = event["data"]
            if data.get("doc_id") == doc and data.get("method") == "doc_reload":
                reloaded = data
                break
        if reloaded is None:
            time.sleep(0.2)
    assert reloaded is not None, "external stable rewrite did not reload"
    assert reloaded["source_kind"] == "system" and reloaded["human_dirty"] is False
    assert reloaded["tree"]["name"] == "replacement"

    local = gui.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("local")})
    assert local["revision"] > reloaded["revision"]
    staging = tmp_path / "external-again.step+"
    shutil.copyfile(backup, staging)
    os.replace(staging, target)
    deadline = time.monotonic() + 20
    conflicts = []
    while time.monotonic() < deadline and not conflicts:
        conflicts = [event for event in _events(gui, "doc.disk_conflict")
                     if event["data"].get("doc_id") == doc]
        if not conflicts:
            time.sleep(0.2)
    assert len(conflicts) == 1 and conflicts[0]["data"]["path"] == str(target)
    stamp = conflicts[0]["data"]["observed_stamp"]
    assert set(stamp) == {"size", "mtime_ns", "ino"}
    time.sleep(5)
    assert not _events(gui, "doc.disk_conflict"), "one disk stamp must not repeatedly notify"
    row = next(row for row in gui.call("doc_list")["docs"] if row["doc_id"] == doc)
    assert row["human_dirty"] is True and row["revision"] == local["revision"]


def test_external_rewrite_preserves_unsaved_agent_revision(scratch_daemon, tmp_path):
    gui = _gui_client(scratch_daemon)
    gui.call("subscribe", {"doc_ids": "*"})
    path = tmp_path / "agent-work.step+"
    doc = gui.call("doc_new", {"name": "agent-work"})["doc_id"]
    gui.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    gui.call("doc_save", {"doc_id": doc, "path": str(path)})
    agent = DaemonClient()
    agent.connect()
    edited = agent.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("unsaved-agent")})
    assert edited["revision"] == 2
    row = next(row for row in gui.call("doc_list")["docs"] if row["doc_id"] == doc)
    assert row["dirty"] is True and row["human_dirty"] is False

    replacement = tmp_path / "incoming.step+"
    incoming = agent.call("doc_new", {"name": "incoming"})["doc_id"]
    agent.call("doc_apply_ops", {"doc_id": incoming, "ops": _box_ops("incoming")})
    agent.call("doc_save", {"doc_id": incoming, "path": str(replacement)})
    _events(gui, "doc.changed")
    os.replace(replacement, path)
    deadline = time.monotonic() + 20
    reloaded = None
    while time.monotonic() < deadline and reloaded is None:
        reloaded = next((event["data"] for event in _events(gui, "doc.changed")
                         if event["data"].get("doc_id") == doc and
                         event["data"].get("method") == "doc_reload"), None)
        if reloaded is None:
            time.sleep(0.2)
    assert reloaded is not None and reloaded["tree"]["name"] == "incoming"
    entries = gui.call("recovery_list", {"scope": "ai"})["entries"]
    assert any(entry.get("doc_id") == doc and entry.get("revision") == 2 and
               entry.get("human_dirty") is False for entry in entries), entries


def test_study_mutation_announces_studies_changed(scratch_daemon):
    """An agent's persisted study (FEA results, a simulate_motion sweep) bumps the revision
    with no tree, so it must arrive as doc.studies_changed — never as a doc.changed the GUI
    mirror would reject — or an open Simulation tree never lists it."""
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    b.call("subscribe", {"doc_ids": "*"})
    doc = a.call("doc_new", {"name": "studies"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    time.sleep(0.2)
    _events(b, "doc.changed")  # drain the geometry edit

    added = a.call("sim_study_add", {"doc_id": doc, "study": {"type": "motion", "name": "M"}})
    time.sleep(0.2)
    b.call("ping")
    events = [e for e in b.poll_events()
              if e.get("event") in ("doc.changed", "doc.studies_changed")]
    assert [e["event"] for e in events] == ["doc.studies_changed"], events
    d = events[0]["data"]
    assert d["doc_id"] == doc and d["revision"] == added["revision"]
    assert d["method"] == "sim_study_add"

    # Listing is a read: no announcement. The writer never hears its own edit.
    a.call("subscribe", {"doc_ids": "*"})
    a.call("sim_study_list", {"doc_id": doc})
    a.call("sim_study_delete", {"doc_id": doc, "study_id": added["study_id"]})
    time.sleep(0.2)
    assert not _events(a, "doc.studies_changed")
    assert [e["data"]["method"] for e in _events(b, "doc.studies_changed")] == [
        "sim_study_delete"]


def test_doc_snapshot_served_immediately_after_mint(scratch_daemon):
    """A freshly minted doc must answer doc_snapshot with a REAL empty-tree snapshot,
    before any edit ever lands. This is what lets a GUI mirror tab prime itself the
    frame it appears (engine_events primeMirrorFromSnapshot) instead of falling through
    to doc_activate — which takes the doc's exclusive per-doc engine mutex and therefore
    queues behind the agent's first add_features batch. Live run lexus_f_keychain
    2026-07-29: the tab appeared instantly but only became usable once the agent's first
    geometry committed."""
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    doc = a.call("doc_new", {"name": "mintsnap"})["doc_id"]

    # The spectator (a different client, exactly like the GUI mirror) reads it lock-free.
    snap = b.call("doc_snapshot", {"doc_id": doc})
    assert snap.get("has_snapshot") is True, snap
    assert snap["doc_id"] == doc
    assert snap["revision"] == 0
    # Shape parity with a real doc.changed push — the host parses ONE schema.
    for key in (
        "source_client",
        "source_kind",
        "method",
        "user",
        "tree",
        "owner_map",
        "datums",
        "rebuild",
        "feature_status",
        "aicm_path",
    ):
        assert key in snap, (key, sorted(snap))
    assert snap["method"] == "doc_new"
    # Empty part: a named tree with no features, and a bodiless-but-loadable AICM.
    assert snap["tree"].get("features") == []
    assert snap["tree"].get("name") == "mintsnap"
    assert snap["feature_status"] == []
    assert snap["aicm_path"], "host requires a non-empty aicm_path to park a snapshot"


def test_doc_snapshot_mint_seed_yields_to_real_geometry(scratch_daemon):
    """The mint seed must never shadow geometry: the first real push carries a higher
    revision and replaces it wholesale."""
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    doc = a.call("doc_new", {"name": "mintseed"})["doc_id"]
    seed = b.call("doc_snapshot", {"doc_id": doc})
    assert seed["revision"] == 0 and seed["tree"]["features"] == []

    a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    live = b.call("doc_snapshot", {"doc_id": doc})
    assert live.get("has_snapshot") is True, live
    assert live["revision"] == 1
    assert live["method"] == "doc_apply_ops"
    assert len(live["tree"]["features"]) == 1
    assert live["aicm_path"] != seed["aicm_path"]


def test_doc_snapshot_skips_internal_doc(scratch_daemon):
    """A doc minted {"internal": true} (the agent's preflight shadow clone) is never
    announced, so it must not become spectatable through the snapshot seed either."""
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    doc = a.call("doc_new", {"name": "shadow", "internal": True})["doc_id"]
    assert b.call("doc_snapshot", {"doc_id": doc}).get("has_snapshot") is False
    edited = a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    assert not edited.get("_is_error"), edited
    assert b.call("doc_snapshot", {"doc_id": doc}).get("has_snapshot") is False


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
    final via doc.closed last_writer_detached. The GUI no longer DESTROYS the
    mirror tab on this reason (see the last_writer_detached suite at the end of
    this file and host/src/core/engine_events.cpp): the doc survives the detach,
    so the tab is marked final/agent-done instead. The event must still fire —
    without it a crashed agent's mirror stays 'agent still working' forever."""
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
    assert any(
        e["data"].get("doc_id") == doc
        and e["data"].get("reason") == "last_writer_detached"
        for e in closed
    ), closed


def test_doc_close_degrades_to_detach_with_other_writer(scratch_daemon):
    """Every other attachment blocks close, even when force is requested."""
    a, b = DaemonClient(), DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    b.connect()
    doc = a.call("doc_new", {"name": "coedit"})["doc_id"]
    assert b.call("attach_doc", {"doc_id": doc, "mode": "rw"}).get("ok") is True

    # A closes while B still holds rw -> degrade to detach, doc survives for B.
    r = a.call("doc_close", {"doc_id": doc})
    assert r.get("detached") is True, r
    assert b.call("doc_tree", {"doc_id": doc}).get("_is_error") is None, (
        "doc must survive"
    )
    assert (
        b.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS}).get("_is_error")
        is None
    )

    # Force cannot destroy another client's live document.
    assert a.call("doc_close", {"doc_id": doc, "force": True}).get("detached") is True
    assert any(e.get("doc_id") == doc for e in b.call("doc_list", {})["docs"])


def test_gc_retires_only_undelivered_docs(scratch_daemon, monkeypatch, tmp_path):
    """The load-bearing 'no data loss, no nag' guarantee behind agent-finished auto-close:
    when an abandoned doc is GC'd, one SAVED to a real user file closes clean (NO recovery
    entry -> the user is never nagged about already-delivered AI work), while an unsaved doc
    IS retired to the orphan tier (recoverable). Discriminates ship from regression."""
    monkeypatch.setenv("CHUDWARE_DOC_GC_GRACE_S", "1")  # reap abandoned docs fast
    # mcp-origin docs use the longer agent grace; pin it short so this test still
    # exercises GC retirement within a few seconds.
    monkeypatch.setenv("CHUDWARE_DOC_GC_AGENT_GRACE_S", "1")
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
    assert saved not in ids, (
        "a doc saved to a real file must NOT flood the recovery prompt"
    )


def test_gc_purges_internal_docs_instead_of_retiring_them(scratch_daemon, monkeypatch):
    """An INTERNAL doc ({"internal":true} — the agent preflight advisor's shadow clone)
    must never reach the recovery tier. The clone's own `finally` closes it in the normal
    case, but when the agent's worker DIES mid-preflight only doc-GC is left; retiring it
    then put `<part>_pf` entries in the "Recover?" prompt, and a user who accepted four of
    them got a tab strip of half-built shadow copies of their real part (2026-07-30).

    The unsaved NORMAL doc in the same sweep is the control: it must still be retired, so
    this cannot pass by GC simply having stopped working."""
    monkeypatch.setenv("CHUDWARE_DOC_GC_GRACE_S", "1")
    monkeypatch.setenv("CHUDWARE_DOC_GC_AGENT_GRACE_S", "1")
    keep = DaemonClient()
    keep.connect()
    scratch_daemon.append(_pid(keep))
    a = DaemonClient()
    a.connect()

    clone = a.call("doc_new", {"name": "widget_pf", "internal": True})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": clone, "ops": BOX_OPS})
    normal = a.call("doc_new", {"name": "widget"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": normal, "ops": _box_ops("n")})

    a.close()  # worker death: neither doc was closed by its owner

    deadline = time.time() + 8
    ids: set = set()
    while time.time() < deadline:
        time.sleep(0.5)
        ids = {e.get("doc_id") for e in keep.call("recovery_list").get("entries", [])}
        if normal in ids:
            break
    assert normal in ids, "control: an unsaved abandoned doc MUST still be retired"
    assert clone not in ids, (
        "an internal preflight clone MUST be purged, never recovered"
    )
    names = {e.get("name") for e in keep.call("recovery_list").get("entries", [])}
    assert not any((n or "").endswith("_pf") for n in names), names


def test_gc_preserves_imports_even_when_source_exists(scratch_daemon, monkeypatch, tmp_path):
    """The source file alone does not prove an imported document is reproducible."""
    monkeypatch.setenv("CHUDWARE_DOC_GC_GRACE_S", "1")
    monkeypatch.setenv("CHUDWARE_DOC_GC_AGENT_GRACE_S", "1")
    keep = DaemonClient()
    keep.connect()
    scratch_daemon.append(_pid(keep))
    a = DaemonClient()
    a.connect()

    src_doc = a.call("doc_new", {"name": "frame_src"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": src_doc, "ops": BOX_OPS})
    source = str(tmp_path / "frame.step")
    a.call("export_file", {"doc_id": src_doc, "path": source, "format": "step"})
    assert os.path.isfile(source)
    a.call("doc_close", {"doc_id": src_doc})

    probe = a.call("import_file", {"path": source})["doc_id"]
    placed = a.call("import_file", {"path": source})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": placed, "ops": _box_ops("authored")})

    a.close()

    deadline = time.time() + 8
    ids: set = set()
    open_ids: set = {probe}
    while time.time() < deadline:
        time.sleep(0.5)
        ids = {e.get("doc_id") for e in keep.call("recovery_list").get("entries", [])}
        open_ids = {d.get("doc_id") for d in keep.call("doc_list", {}).get("docs", [])}
        if placed in ids and probe in ids and probe not in open_ids:
            break
    assert placed in ids, "an import with authored work must be retired"
    assert probe not in open_ids, "GC must close the abandoned import"
    assert probe in ids, "the source path does not prove its imported state was delivered"


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


def test_foreign_edits_surface_in_tool_json(scratch_daemon, monkeypatch):
    """Foreign-edit records remain structured and the response is valid JSON."""
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "1")
    monkeypatch.setattr(daemon_bridge, "_clients", threading.local())
    monkeypatch.setattr(daemon_bridge, "_group_session_established", threading.Event())
    from services.mcp import cad_tools, core_bridge

    a = DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))
    doc = core_bridge.call("doc_new", {"name": "note"})["doc_id"]  # via daemon route
    core_bridge.call("doc_tree", {"doc_id": doc})
    a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})  # foreign writer

    r = core_bridge.call("doc_tree", {"doc_id": doc})
    assert "_foreign_edits" not in r, "bridge must strip the marker from results"
    payload = json.loads(cad_tools._dumps({"ok": True}))
    assert payload["foreign_edits"][0]["doc_id"] == doc
    assert "foreign_edits" not in json.loads(cad_tools._dumps({"ok": True}))


# ------------------------------------------------------- autosave + recovery


def test_crash_recovery_prompts_via_orphan_tier(scratch_daemon):
    """A crash survivor without a bound path waits for explicit recovery_open."""
    a = DaemonClient()
    a.connect()
    pid = _pid(a)
    doc = a.call("doc_new", {"name": "recover-me"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    time.sleep(2.5)  # CHUDWARE_AUTOSAVE_INTERVAL_S=1 -> at least one tick saved it
    # A metadata-only change leaves the geometry revision unchanged but must be
    # present in the next crash snapshot.
    rev = next(row["revision"] for row in a.call("doc_list", {})["docs"]
               if row["doc_id"] == doc)
    a.call("doc_set_meta", {"doc_id": doc, "key": "fixture", "value": {"note": "after autosave"}})
    assert next(row["revision"] for row in a.call("doc_list", {})["docs"]
                if row["doc_id"] == doc) == rev
    time.sleep(2.5)

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
    assert b.call("doc_get_meta", {"doc_id": new_id, "key": "fixture"})["value"] == {
        "note": "after autosave"}
    # Consumed: a second recovery_open must not duplicate it.
    assert b.call("recovery_list", {}).get("entries") == []


def _reference_only_part(client, tmp_path, name):
    source = tmp_path / (name + ".ply")
    source.write_text("ply\nformat ascii 1.0\nelement vertex 3\n"
                      "property float x\nproperty float y\nproperty float z\n"
                      "element face 1\nproperty list uchar int vertex_indices\nend_header\n"
                      "0 0 0\n1 0 0\n0 1 0\n3 0 1 2\n")
    doc = client.call("doc_new", {"kind": "part", "name": name})["doc_id"]
    added = client.call("doc_mesh_ref_add", {"doc_id": doc, "path": str(source)})
    assert added.get("ok") is True and added["triangles"] == 1, added
    summary = next(row for row in client.call("doc_list", {})["docs"] if row["doc_id"] == doc)
    assert summary["feature_count"] == 0 and summary["empty"] is False
    source.unlink()  # Both recovery paths must use the embedded mesh, not this file.
    return doc, added["id"]


def _assert_recovered_reference(client, entries, old_doc, ref):
    entry = next((row for row in entries if row.get("doc_id") == old_doc), None)
    assert entry is not None, entries
    opened = client.call("recovery_open", {"id": entry["id"]}).get("opened") or []
    recovered = next((row for row in opened if row.get("original_doc_id") == old_doc), None)
    assert recovered is not None, opened
    new_doc = recovered["doc_id"]
    refs = client.call("doc_mesh_ref_list", {"doc_id": new_doc})
    assert len(refs["refs"]) == 1 and refs["refs"][0]["id"] == ref
    mesh = client.call("doc_mesh_ref_mesh", {"doc_id": new_doc, "id": ref})
    assert mesh["ok"] and len(mesh["vertices_mm"]) == 9 and len(mesh["indices"]) == 3


def test_reference_only_part_survives_daemon_crash_recovery(scratch_daemon, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("CHUDWARE_NATIVE_ROOT", raising=False)
    a = DaemonClient()
    a.connect()
    pid = _pid(a)
    scratch_daemon.append(pid)
    doc, ref = _reference_only_part(a, tmp_path, "reference-crash")
    autosave = tmp_path / "state" / "Chudware" / "hostd_autosave" / os.environ["CHUDWARE_INSTANCE_KEY"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        # A committed snapshot and its sidecar have the same generation stem.
        if any(p.with_suffix(".step+").exists() and json.loads(p.read_text()).get("doc_id") == doc
               for p in autosave.glob("*/*.json")):
            break
        time.sleep(0.2)
    else:
        pytest.fail("reference-only part was not autosaved")

    os.kill(pid, signal.SIGKILL)
    time.sleep(0.3)
    b = DaemonClient()
    b.connect()
    scratch_daemon.append(_pid(b))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if b.call("daemon_status").get("recovery") == "done":
            break
        time.sleep(0.2)
    else:
        pytest.fail("daemon recovery did not complete")
    entries = b.call("recovery_list", {}).get("entries") or []
    entry = next(row for row in entries if row.get("doc_id") == doc)
    assert entry["same_session"] is False
    assert isinstance(entry["metadata_revision"], int)
    _assert_recovered_reference(b, entries, doc, ref)


def test_reference_only_part_survives_gc_retirement(scratch_daemon, monkeypatch, tmp_path):
    monkeypatch.delenv("CHUDWARE_NATIVE_ROOT", raising=False)
    monkeypatch.setenv("CHUDWARE_DOC_GC_GRACE_S", "1")
    monkeypatch.setenv("CHUDWARE_DOC_GC_AGENT_GRACE_S", "1")
    keep, author = DaemonClient(), DaemonClient()
    keep.connect()
    scratch_daemon.append(_pid(keep))
    author.connect()
    doc, ref = _reference_only_part(author, tmp_path, "reference-gc")
    author.close()
    deadline = time.monotonic() + 20
    entries = []
    while time.monotonic() < deadline:
        entries = keep.call("recovery_list", {}).get("entries") or []
        if any(row.get("doc_id") == doc for row in entries):
            break
        time.sleep(0.2)
    entry = next(row for row in entries if row.get("doc_id") == doc)
    assert entry["same_session"] is True
    assert isinstance(entry["metadata_revision"], int)
    _assert_recovered_reference(keep, entries, doc, ref)


def test_recover_autosaved_uses_daemon_remap(scratch_daemon, monkeypatch):
    """After a crash, cad_tools.recover_autosaved (daemon mode) restores the
    survivor from the orphan tier (recovery_open) and aliases the agent's old
    handle to the freshly-minted id."""
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "1")
    monkeypatch.setattr(daemon_bridge, "_clients", threading.local())
    monkeypatch.setattr(daemon_bridge, "_group_session_established", threading.Event())
    from services.mcp import cad_tools, core_bridge

    doc = core_bridge.call("doc_new", {"name": "alias-me"})["doc_id"]
    pid = daemon_bridge.client().call("daemon_status")["pid"]
    core_bridge.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    time.sleep(2.5)
    os.kill(pid, signal.SIGKILL)
    time.sleep(0.3)

    monkeypatch.setattr(daemon_bridge, "_clients", threading.local())  # fresh connection
    monkeypatch.setattr(daemon_bridge, "_group_session_established", threading.Event())
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


def test_parked_client_does_not_pin_the_daemon(scratch_daemon):
    """A connected-but-silent non-GUI client must NOT keep the daemon alive forever.

    Regression: idle-exit gated on `status_.clients > 0`, so ONE socket that said hello and
    then went quiet (a crashed tool, an editor's MCP server parked all day) vetoed every exit
    path. Measured on a real machine: a daemon alive 5.5 h in the draining state with a tray
    icon the user could not dismiss. A client now counts only while it is WORKING — an engine
    call in flight, an attached doc, or traffic inside the idle window."""
    parked = DaemonClient()
    parked.connect()
    pid = _pid(parked)
    scratch_daemon.append(pid)
    # Never closed, never used again — exactly the shape that used to pin the daemon.
    assert _wait_daemon_gone(pid, timeout=25), "a parked client still pins the daemon"


def test_attached_client_still_pins_the_daemon(scratch_daemon):
    """The control for the test above: a client HOLDING a doc is real work, not parked, so
    the daemon must stay up for it even with no traffic at all."""
    a = DaemonClient()
    a.connect()
    pid = _pid(a)
    scratch_daemon.append(pid)
    a.call("doc_new", {"name": "held-open"})  # implicit rw attach
    time.sleep(8)  # >2x the fixture's 3s linger
    assert not _wait_daemon_gone(pid, timeout=0.1), (
        "daemon dropped a client holding a doc"
    )
    assert a.call("daemon_status").get("pid") == pid


def test_drain_exits_on_its_deadline_despite_a_stuck_client(
    scratch_daemon, monkeypatch
):
    """`daemon_drain` is a STOP request and must terminate. A client that never disconnects
    used to veto it forever (the same 5.5 h tray icon); the drain now carries a deadline
    (CHUDWARE_HOSTD_DRAIN_GRACE_S) after which stragglers are dropped and the daemon exits."""
    monkeypatch.setenv("CHUDWARE_HOSTD_DRAIN_GRACE_S", "4")
    # Long linger + a doc attachment so ONLY the drain deadline can end this daemon.
    monkeypatch.setenv("CHUDWARE_HOSTD_LINGER_SECS", "600")
    stuck = DaemonClient()
    stuck.connect()
    pid = _pid(stuck)
    scratch_daemon.append(pid)
    stuck.call("doc_new", {"name": "stuck-writer"})  # attached: pins until the deadline
    r = stuck.call("daemon_drain", {})
    assert r.get("ok") is True and r.get("deadline_secs") == 4, r
    assert _wait_daemon_gone(pid, timeout=25), "drain never reached its deadline"


def test_clean_shutdown_preserves_docs(scratch_daemon):
    """A normal drain preserves uncommitted work without opening a tab."""
    a = DaemonClient()
    a.connect()
    pid = _pid(a)
    scratch_daemon.append(pid)
    doc = a.call("doc_new", {"name": "discard-on-quit"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})

    assert a.call("daemon_drain", {}).get("ok") is True
    a.close()  # last client gone -> drained daemon exits cleanly
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

    # Nothing is auto-opened, but unbound work remains available to agents.
    info = b.call("hello", {"client": "test", "pid": os.getpid(), "proto": 1})
    assert not (info.get("id_remap") or {}), info.get("id_remap")
    names = [d["name"] for d in b.call("doc_list", {})["docs"]]
    assert "discard-on-quit" not in names, names
    entries = b.call("recovery_list", {}).get("entries") or []
    entry = next(e for e in entries if e.get("name") == "discard-on-quit")
    assert entry["human_dirty"] is False


def _recovered_daemon(scratch_daemon, old_pid: int) -> DaemonClient:
    # A killed daemon's exiting children can briefly hold the old socket; retry the
    # respawn like the GUI's reconnect loop does.
    deadline = time.monotonic() + 30
    while True:
        c = DaemonClient()
        try:
            c.connect()  # respawns a fresh daemon
            break
        except daemon_bridge.DaemonError:
            c.close()
            assert time.monotonic() < deadline, "no fresh daemon after the old one died"
            time.sleep(0.3)
    pid = _pid(c)
    assert pid != old_pid
    scratch_daemon.append(pid)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if c.call("daemon_status").get("recovery") == "done":
            return c
        time.sleep(0.2)
    pytest.fail("daemon recovery did not complete")


def test_recovered_work_survives_sigterm_and_a_second_crash(scratch_daemon, monkeypatch):
    """Live 2026-11-01: the GPU fell off the bus, the session teardown SIGTERMed hostd,
    and the 28 parts the user had just recovered were gone. Termination from outside
    (SIGTERM) is not the user discarding their work: unsaved docs stay recoverable. And
    a recovered doc is unsaved work again — dirty, snapshotted before its orphan
    artifact is deleted — so a crash right after Recover cannot lose it."""
    # No periodic tick can rescue either step: only the SIGTERM final snapshot and the
    # recovery_open snapshot put the doc on disk.
    monkeypatch.setenv("CHUDWARE_AUTOSAVE_INTERVAL_S", "600")
    a = DaemonClient()
    a.connect()
    pid = _pid(a)
    scratch_daemon.append(pid)
    deadline = time.monotonic() + 60
    while a.call("daemon_status").get("recovery") != "done":
        assert time.monotonic() < deadline, "daemon recovery did not complete"
        time.sleep(0.2)
    doc = a.call("doc_new", {"name": "survive-sigterm"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})

    os.kill(pid, signal.SIGTERM)
    assert _wait_daemon_gone(pid), "SIGTERMed daemon did not exit"
    a.close()

    b = _recovered_daemon(scratch_daemon, pid)
    entries = b.call("recovery_list", {}).get("entries") or []
    entry = next((e for e in entries if e.get("name") == "survive-sigterm"), None)
    assert entry is not None, entries
    opened = b.call("recovery_open", {"id": entry["id"]}).get("opened") or []
    assert len(opened) == 1, opened
    row = next(d for d in b.call("doc_list", {})["docs"] if d["doc_id"] == opened[0]["doc_id"])
    assert row["dirty"] is True, row  # unsaved work: quit prompt + autosave + GC retire
    assert row["native_source_path"] is None, row  # never a save destination in the tier

    pid_b = _pid(b)
    os.kill(pid_b, signal.SIGKILL)  # crash immediately after Recover
    assert _wait_daemon_gone(pid_b)
    b.close()

    c = _recovered_daemon(scratch_daemon, pid_b)
    entries = c.call("recovery_list", {}).get("entries") or []
    assert any(e.get("name") == "survive-sigterm" for e in entries), entries


def test_doc_close_preserves_dirty_autosave(scratch_daemon):
    """Closing a dirty document retains its authored revision across a crash."""
    a = DaemonClient()
    a.connect()
    pid = _pid(a)
    scratch_daemon.append(pid)
    doc = a.call("doc_new", {"name": "closed-for-good"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    time.sleep(2.5)  # CHUDWARE_AUTOSAVE_INTERVAL_S=1 -> the autosave file exists
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
    entry = next(e for e in entries if e.get("name") == "closed-for-good")
    assert entry["human_dirty"] is False


def test_doc_gc_retires_abandoned_docs(scratch_daemon, monkeypatch):
    """An abandoned document is retired after the GC grace and then closed."""
    monkeypatch.setenv("CHUDWARE_DOC_GC_GRACE_S", "1")
    monkeypatch.setenv("CHUDWARE_DOC_GC_AGENT_GRACE_S", "1")
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
    assert any(
        e.get("data", {}).get("doc_id") == doc
        and e.get("data", {}).get("reason") == "gc"
        for e in evts
    ), evts
    # ...and the work is RETIRED, not lost: it waits in the recovery tier.
    entries = b.call("recovery_list", {}).get("entries") or []
    assert any(e.get("name") == "abandoned" for e in entries), entries


def test_doc_discard_then_gc_preserves_dirty_work(scratch_daemon, monkeypatch):
    """Discarding an agent presentation never discards its authored work."""
    monkeypatch.setenv("CHUDWARE_DOC_GC_GRACE_S", "1")
    monkeypatch.setenv("CHUDWARE_DOC_GC_AGENT_GRACE_S", "1")
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
    entries = b.call("recovery_list", {}).get("entries") or []
    assert any(e.get("doc_id") == doc and not e["human_dirty"] for e in entries)


def test_doc_discard_retains_dirty_work(scratch_daemon):
    """Closing the final dirty view retires the document rather than losing it."""
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
    entries = a.call("recovery_list", {}).get("entries") or []
    assert any(e.get("doc_id") == doc and not e["human_dirty"] for e in entries)


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
    monkeypatch.setenv("CHUDWARE_DOC_GC_GRACE_S", "1")
    monkeypatch.setenv("CHUDWARE_DOC_GC_AGENT_GRACE_S", "1")
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


def test_agent_origin_survives_base_gc_grace(scratch_daemon, monkeypatch):
    """H3: mcp/agent-origin docs use CHUDWARE_DOC_GC_AGENT_GRACE_S (default 30 min),
    not the 90 s base grace. A disconnecting MCP client must leave its part
    listed past the short base window so the GUI can still open it."""
    monkeypatch.setenv("CHUDWARE_DOC_GC_GRACE_S", "1")
    monkeypatch.setenv("CHUDWARE_DOC_GC_AGENT_GRACE_S", "8")  # > base, still test-fast
    keep = DaemonClient()
    keep.connect()
    scratch_daemon.append(_pid(keep))
    a = DaemonClient()
    a.connect()  # kind=mcp
    doc = a.call("doc_new", {"name": "agent-built"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    a.close()  # last attach gone -> orphan countdown starts with AGENT grace

    time.sleep(3.0)  # well past base 1 s, well under agent 8 s
    ids = [d["doc_id"] for d in keep.call("doc_list", {})["docs"]]
    assert doc in ids, "agent-origin doc was GC'd at base grace instead of agent grace"

    # After the agent grace expires, GC may retire it.
    deadline = time.time() + 12
    while time.time() < deadline:
        time.sleep(0.5)
        ids = [d["doc_id"] for d in keep.call("doc_list", {})["docs"]]
        if doc not in ids:
            break
    assert doc not in ids, "agent-origin doc never GC'd after agent grace"


def test_no_python_autosave_timer_in_daemon_mode(scratch_daemon, monkeypatch):
    monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "1")
    from services.mcp import cad_tools

    cad_tools.start_periodic_autosave()
    time.sleep(0.1)
    names = [t.name for t in threading.enumerate()]
    assert "chudware-autosave" not in names, names
    assert cad_tools._PERIODIC_TIMER[0] is None


def test_instance_key_parity_with_cpp(monkeypatch):
    """The default (no CHUDWARE_INSTANCE_KEY) rendezvous: Python must hash the hostd
    path to the SAME instance key the C++ daemon computes for itself, or the MCP
    and the GUI split into two engines and live doc mirroring silently dies.
    Every other test here pins CHUDWARE_INSTANCE_KEY, which is exactly how the
    truncated-vs-textbook FNV basis mismatch stayed invisible. Isolated from any
    live daemon via a scratch XDG_RUNTIME_DIR (both sides derive the socket dir
    from it), so the path-derived key lands on a fresh socket."""
    monkeypatch.delenv("CHUDWARE_INSTANCE_KEY", raising=False)
    monkeypatch.delenv("CHUDWARE_HOSTD_SOCKET", raising=False)
    # pytest's runtime directory can exceed AF_UNIX's 108-byte path limit on
    # CI. Use a short, private namespace without changing the derived key.
    with tempfile.TemporaryDirectory(prefix="cw-reg-", dir="/tmp") as runtime_dir:
        monkeypatch.setenv("XDG_RUNTIME_DIR", runtime_dir)
        monkeypatch.setenv("CHUDWARE_HOSTD_LINGER_SECS", "3")
        c = DaemonClient()
        pid = None
        try:
            c.connect()  # spawns hostd; connect only succeeds if the keys agree
            pid = int(c.call("daemon_status", {})["pid"])
            assert c.server_info.get("state") in ("ready", "loading")
            sock = c._sock_path
            assert sock and sock.startswith(runtime_dir) and os.path.exists(sock), sock
        finally:
            try:
                c.close()
            except Exception:
                pass
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass


# ------------------------------------- last-writer detach: the doc SURVIVES
#
# The general lane spawns a FRESH `python -m services.worker_main` per turn, so
# the agent's last rw attach drops at the END OF EVERY TURN and hostd announces
# doc.closed{reason:"last_writer_detached"} every single time. host/src/core/
# engine_events.cpp's doc.closed branch therefore KEEPS the mirror tab on that
# reason (marking it agent-done) instead of destroying it — which is only safe
# because the document genuinely stays alive in the daemon. These tests pin that
# invariant: if hostd ever starts really deleting the doc on last-writer detach,
# the GUI would show a tab for a document that no longer exists, the part would
# vanish from the viewport, and the Artifacts panel would lose it. Deleting
# these tests removes the only guard against that.


def _gui_client(pids: list) -> DaemonClient:
    """A connection that hostd sees as the GUI (kind "gui"), not as an agent.

    DaemonClient.connect() hardcodes {"client": "mcp"} in its hello; re-sending
    hello re-stamps the kind (core/hostd/server.cpp: c.kind = params["client"]),
    which is how the other tests here register kind "test". Kind matters: only a
    non-rw viewer keeps this connection out of the writer count whose emptying
    triggers last_writer_detached."""
    c = DaemonClient()
    c.connect()
    pids.append(_pid(c))
    c.call("hello", {"client": "gui", "pid": os.getpid(), "proto": 1})
    return c


def _abandoned_agent_doc(gui: DaemonClient, name: str) -> str:
    """Mint a doc from a separate `mcp` client, edit it, mirror it read-only from
    `gui`, then CLOSE the agent's socket — exactly one general-lane turn ending.
    Returns the doc id; the doc must outlive the agent."""
    agent = DaemonClient()
    agent.connect()
    doc = agent.call("doc_new", {"name": name})["doc_id"]
    assert (
        agent.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops(name)}).get(
            "_is_error"
        )
        is None
    )
    # The GUI mirror is a read-only viewer, so the agent's rw is the last writer.
    assert gui.call("attach_doc", {"doc_id": doc, "mode": "ro"}).get("ok") is True
    agent.close()
    time.sleep(0.4)
    return doc


def test_abandoned_agent_doc_stays_in_doc_list(scratch_daemon):
    """Protects host/src/core/engine_events.cpp's doc.closed branch keeping the
    mirror tab on last_writer_detached: the tab is only legitimate while the doc
    is still in the registry, with its name and revision intact."""
    gui = _gui_client(scratch_daemon)
    doc = _abandoned_agent_doc(gui, "survives-detach")

    entry = next(
        (e for e in gui.call("doc_list", {})["docs"] if e.get("doc_id") == doc), None
    )
    assert entry is not None, "hostd deleted an agent doc on last-writer detach"
    assert entry.get("name") == "survives-detach", entry
    assert entry.get("revision") == 1, entry
    assert entry.get("origin_kind") == "mcp", entry


def test_abandoned_agent_doc_announces_last_writer_detached(scratch_daemon):
    """The announcement still happens — it just does not mean deletion. The GUI
    branches on this exact string in host/src/core/engine_events.cpp's doc.closed
    handler (keep the tab, mark it agent-done) and falls through to the real
    close path for every other reason, so the spelling is load-bearing."""
    gui = _gui_client(scratch_daemon)
    gui.call("subscribe", {"doc_ids": "*"})
    doc = _abandoned_agent_doc(gui, "announced-detach")

    closed = [e for e in _events(gui, "doc.closed") if e["data"].get("doc_id") == doc]
    assert closed, "no doc.closed for the abandoned agent doc"
    assert closed[-1]["data"].get("reason") == "last_writer_detached", closed[-1]


def test_abandoned_agent_doc_still_queryable(scratch_daemon):
    """Not a tombstone: the kept mirror tab must be able to render. doc_tree is
    what the GUI pulls for a tab it keeps alive after the doc.closed branch in
    host/src/core/engine_events.cpp declines to destroy it."""
    gui = _gui_client(scratch_daemon)
    doc = _abandoned_agent_doc(gui, "queryable-detach")

    tree = gui.call("doc_tree", {"doc_id": doc})
    assert tree.get("_is_error") is None, tree
    assert tree.get("revision") == 1, tree


def test_explicit_doc_close_waits_for_read_only_holder(scratch_daemon):
    """A read-only attachment keeps the document alive when its writer closes."""
    gui = _gui_client(scratch_daemon)
    gui.call("subscribe", {"doc_ids": "*"})
    agent = DaemonClient()
    agent.connect()
    doc = agent.call("doc_new", {"name": "really-closed"})["doc_id"]
    assert gui.call("attach_doc", {"doc_id": doc, "mode": "ro"}).get("ok") is True
    assert agent.call("doc_close", {"doc_id": doc}).get("detached") is True
    assert gui.call("doc_tree", {"doc_id": doc}).get("_is_error") is None
    assert any(e.get("doc_id") == doc for e in gui.call("doc_list", {})["docs"])


def test_explicit_presentation_tracks_live_document_without_edit_custody(scratch_daemon, tmp_path):
    owner, peer, viewer = DaemonClient(), DaemonClient(), DaemonClient()
    owner.connect()
    scratch_daemon.append(_pid(owner))
    peer.connect()
    viewer.connect()
    try:
        viewer.call("subscribe", {"doc_ids": "*"})
        doc = owner.call("doc_new", {"name": "presentation"})["doc_id"]
        hidden = owner.call("doc_new", {"name": "hidden"})["doc_id"]
        internal = owner.call("doc_new", {"name": "shadow", "internal": True})["doc_id"]

        def row(did):
            return next(r for r in viewer.call("doc_list")["docs"] if r["doc_id"] == did)

        assert row(doc)["presented"] is False
        assert row(hidden)["presented"] is False
        assert not _events(viewer, "doc.presented")
        # An AI client's first edit presents the document by itself (see the next test).
        assert owner.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})["revision"] == 1
        assert row(doc)["presented"] is True and row(hidden)["presented"] is False
        assert [e["data"] for e in _events(viewer, "doc.presented")] == [
            {"doc_id": doc, "name": "presentation", "revision": 1}]
        path = tmp_path / "source.step+"
        assert owner.call("doc_save", {"doc_id": doc, "path": str(path)}).get("_is_error") is None
        before = path.read_bytes()

        # Another writer (or ro observer) can present the explicit id without a hold.
        assert peer.call("attach_doc", {"doc_id": doc, "mode": "ro"})["ok"]
        for _ in range(2):
            result = peer.call("doc_present", {"doc_id": doc})
            assert result == {"ok": True, "doc_id": doc, "name": "presentation",
                              "revision": 1, "presented": True}
        events = _events(viewer, "doc.presented")
        assert [e["data"] for e in events] == [
            {"doc_id": doc, "name": "presentation", "revision": 1}
        ] * 2
        assert row(doc)["presented"] is True
        assert row(hidden)["presented"] is False
        assert path.read_bytes() == before
        for bad in ("doc-cppffffffff", internal):
            assert peer.call("doc_present", {"doc_id": bad}).get("_is_error") is True
        assert peer.call("doc_present", {}).get("_is_error") is True
    finally:
        for client in (owner, peer, viewer):
            client.close()


def test_an_ai_edit_presents_its_document_and_only_the_latest_stays_presented(scratch_daemon):
    ai, other_ai, viewer, gui = DaemonClient(), DaemonClient(), DaemonClient(), DaemonClient()
    ai.connect()
    scratch_daemon.append(_pid(ai))
    other_ai.connect()
    viewer.connect()
    gui.connect()
    gui.call("hello", {"client": "gui", "proto": 1})     # re-stamp the kind: a human's client
    try:
        viewer.call("subscribe", {"doc_ids": "*"})
        first = ai.call("doc_new", {"name": "first"})["doc_id"]
        second = ai.call("doc_new", {"name": "second"})["doc_id"]
        shadow = ai.call("doc_new", {"name": "shadow", "internal": True})["doc_id"]
        mine = gui.call("doc_new", {"name": "mine"})["doc_id"]

        def rows():
            return {r["doc_id"]: r for r in viewer.call("doc_list")["docs"]}

        def presented():
            return [e["data"]["doc_id"] for e in _events(viewer, "doc.presented")]

        # Creating a document, reading it and a human's own edits present nothing.
        ai.call("doc_tree", {"doc_id": first})
        assert gui.call("doc_apply_ops", {"doc_id": mine, "ops": BOX_OPS})["revision"] == 1
        assert presented() == []
        assert not any(r["presented"] for r in rows().values())

        # The first AI edit presents; the rest of that build is silent. (presented() returns
        # the events that arrived since its last call.)
        revisions = {}

        def edit(client, doc, name):
            reply = client.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops(name)})
            assert reply.get("_is_error") is None and reply.get("ok") is not False, reply
            revisions[doc] = revisions.get(doc, 0) + 1
            assert reply["revision"] == revisions[doc], reply

        edit(ai, first, "a1")
        edit(ai, first, "a2")
        assert presented() == [first]
        # An edit to a second part moves the presentation, from any AI client.
        edit(other_ai, second, "b1")
        assert presented() == [second]
        # Going back to the first part brings it forward again.
        edit(ai, first, "a3")
        edit(ai, first, "a4")
        assert presented() == [first]
        table = rows()
        assert table[first]["presented_seq"] > table[second]["presented_seq"] > 0
        assert table[mine]["presented"] is False and table[mine]["presented_seq"] == 0
        # An internal document is invisible and never presents.
        edit(ai, shadow, "s1")
        assert shadow not in rows() and presented() == []
    finally:
        for client in (ai, other_ai, viewer, gui):
            client.close()


def _wait_for_snapshot(root, doc_id, revision):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        for sidecar in root.glob("*/*.json"):
            if not sidecar.with_suffix(".step+").exists():
                continue
            meta = json.loads(sidecar.read_text())
            if meta.get("doc_id") == doc_id and meta.get("revision") == revision:
                return meta
        time.sleep(0.1)
    pytest.fail(f"no durable snapshot for {doc_id} at revision {revision}")


def test_recovery_exact_discard_and_human_scope(scratch_daemon):
    gui = _gui_client(scratch_daemon)
    agent = DaemonClient()
    agent.connect()
    human = gui.call("doc_new", {"name": "human-recovery"})["doc_id"]
    gui.call("doc_apply_ops", {"doc_id": human, "ops": BOX_OPS})
    ai = agent.call("doc_new", {"name": "ai-recovery"})["doc_id"]
    agent.call("doc_apply_ops", {"doc_id": ai, "ops": _box_ops("ai")})
    assert gui.call("doc_close", {"doc_id": human}).get("detached") is False
    assert agent.call("doc_discard", {"doc_id": ai}).get("discarded") is True

    entries = gui.call("recovery_list", {})["entries"]
    h = next(e for e in entries if e["doc_id"] == human)
    a = next(e for e in entries if e["doc_id"] == ai)
    assert h["human_dirty"] is True and h["origin_kind"] == "gui"
    assert a["human_dirty"] is False and a["origin_kind"] == "mcp"
    assert {"id", "name", "path", "origin_kind", "human_dirty",
            "revision", "captured_at"} <= h.keys()
    assert {e["id"] for e in agent.call("recovery_list", {"scope": "ai"})["entries"]} == {a["id"]}
    assert gui.call("recovery_discard", {"all": True}).get("_is_error") is True
    assert gui.call("recovery_discard", {"ids": [h["id"]]})["discarded"] == 1
    assert [e["id"] for e in gui.call("recovery_list", {})["entries"]] == [a["id"]]


@pytest.mark.skipif(os.name == "nt", reason="test uses POSIX symlink permissions")
@pytest.mark.parametrize("finish", ["save", "discard"])
def test_retained_recovery_source_is_superseded_after_save_or_discard(
    scratch_daemon, monkeypatch, tmp_path, finish
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CHUDWARE_AUTOSAVE_INTERVAL_S", "600")
    a = DaemonClient()
    a.connect()
    scratch_daemon.append(_pid(a))

    # A successful first recovery creates this daemon's private snapshot directory.
    warm = a.call("doc_new", {"name": "warm-recovery"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": warm, "ops": BOX_OPS})
    assert not a.call("doc_close", {"doc_id": warm})["detached"]
    entry = next(e for e in a.call("recovery_list", {})["entries"] if e["doc_id"] == warm)
    warm = a.call("recovery_open", {"id": entry["id"]})["opened"][0]["doc_id"]
    root = tmp_path / "state" / "Chudware" / "hostd_autosave" / os.environ["CHUDWARE_INSTANCE_KEY"]
    sidecar = next(p for p in root.glob("*/*.json")
                   if p.with_suffix(".step+").exists() and json.loads(p.read_text()).get("doc_id") == warm)
    session = sidecar.parent

    doc = a.call("doc_new", {"name": "retained-source"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("retained")})
    assert not a.call("doc_close", {"doc_id": doc})["detached"]
    source = next(e for e in a.call("recovery_list", {})["entries"] if e["doc_id"] == doc)

    # Reject the replacement snapshot while keeping the original orphan intact.
    session.rename(session.with_name(session.name + "-parked"))
    external = tmp_path / "external"
    external.mkdir()
    session.symlink_to(external, target_is_directory=True)
    recovered = a.call("recovery_open", {"id": source["id"]})["opened"][0]["doc_id"]
    assert any(e["id"] == source["id"] for e in a.call("recovery_list", {})["entries"])
    if finish == "save":
        result = a.call("doc_save", {"doc_id": recovered, "path": str(tmp_path / "saved.step+")})
        assert result.get("_is_error") is None
    else:
        assert a.call("doc_discard", {"doc_id": recovered})["discarded"] is True
    entries = a.call("recovery_list", {})["entries"]
    assert all(e["id"] != source["id"] for e in entries)
    if finish == "discard":
        assert any(e["doc_id"] == recovered for e in entries)


def test_gui_doc_close_preserves_agent_read_lease(scratch_daemon):
    gui = _gui_client(scratch_daemon)
    agent = DaemonClient()
    agent.connect()
    doc = gui.call("doc_new", {"name": "agent-reading-human-part"})["doc_id"]
    gui.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    assert agent.call("attach_doc", {"doc_id": doc, "mode": "ro"})["ok"]
    assert gui.call("doc_close", {"doc_id": doc})["detached"] is True
    assert agent.call("doc_tree", {"doc_id": doc}).get("_is_error") is None
    assert agent.call("doc_close", {"doc_id": doc})["detached"] is False
    entry = next(e for e in agent.call("recovery_list", {})["entries"] if e["doc_id"] == doc)
    assert entry["human_dirty"] is True and entry["origin_kind"] == "gui"


def test_metadata_only_human_work_is_recoverable(scratch_daemon):
    gui = _gui_client(scratch_daemon)
    doc = gui.call("doc_new", {"name": "metadata-only"})["doc_id"]
    assert gui.call("doc_set_meta", {"doc_id": doc, "key": "note",
                                     "value": "keep this"}).get("_is_error") is None
    assert gui.call("doc_close", {"doc_id": doc})["detached"] is False
    entry = next(e for e in gui.call("recovery_list", {})["entries"] if e["doc_id"] == doc)
    assert entry["human_dirty"] is True
    opened = gui.call("recovery_open", {"id": entry["id"]})["opened"][0]["doc_id"]
    assert gui.call("doc_get_meta", {"doc_id": opened, "key": "note"})["value"] == "keep this"


@pytest.mark.parametrize("changed_on_disk", [False, True])
def test_ai_recovery_restores_only_matching_base_stamp(
    scratch_daemon, monkeypatch, tmp_path, changed_on_disk
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
    a = DaemonClient()
    a.connect()
    pid = _pid(a)
    scratch_daemon.append(pid)
    doc = a.call("doc_new", {"name": "restore-ai"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    path = tmp_path / "deliverable.step+"
    assert a.call("doc_save", {"doc_id": doc, "path": str(path)}).get("_is_error") is None
    revision = a.call("doc_apply_ops", {"doc_id": doc, "ops": _box_ops("second")})["revision"]
    root = tmp_path / "state" / "Chudware" / "hostd_autosave" / os.environ["CHUDWARE_INSTANCE_KEY"]
    meta = _wait_for_snapshot(root, doc, revision)
    assert meta["human_dirty"] is False and meta["file_stamp"]
    os.kill(pid, signal.SIGKILL)
    assert _wait_daemon_gone(pid)
    a.close()
    if changed_on_disk:
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    b = _recovered_daemon(scratch_daemon, pid)
    entries = b.call("recovery_list", {"scope": "ai"})["entries"]
    matching = [e for e in entries if e["doc_id"] == doc]
    if changed_on_disk:
        assert matching and matching[0]["path"] == str(path)
        opened = b.call("recovery_open", {"id": matching[0]["id"]})["opened"][0]
        assert opened["disk_conflict"] is True
        row = next(d for d in b.call("doc_list", {})["docs"] if d["doc_id"] == opened["doc_id"])
        assert row["path"] == str(path) and row["human_dirty"] is False
    else:
        assert not matching, entries
        reopened = b.call("doc_open", {"path": str(path)})["doc_id"]
        assert "second" in str(b.call("doc_tree", {"doc_id": reopened})["tree"])


def test_saved_revision_supersedes_session_snapshot(scratch_daemon, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
    a = DaemonClient()
    a.connect()
    pid = _pid(a)
    scratch_daemon.append(pid)
    doc = a.call("doc_new", {"name": "saved-before-crash"})["doc_id"]
    revision = a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})["revision"]
    root = tmp_path / "state" / "Chudware" / "hostd_autosave" / os.environ["CHUDWARE_INSTANCE_KEY"]
    _wait_for_snapshot(root, doc, revision)
    result = a.call("doc_save", {"doc_id": doc, "path": str(tmp_path / "saved.step+")})
    assert result["saved_revision"] == revision
    os.kill(pid, signal.SIGKILL)
    assert _wait_daemon_gone(pid)
    a.close()
    b = _recovered_daemon(scratch_daemon, pid)
    assert all(e["doc_id"] != doc for e in b.call("recovery_list", {})["entries"])


@pytest.mark.skipif(os.name == "nt", reason="test uses POSIX symlink permissions")
def test_failed_migration_keeps_source_until_retry(scratch_daemon, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
    a = DaemonClient()
    a.connect()
    pid = _pid(a)
    scratch_daemon.append(pid)
    doc = a.call("doc_new", {"name": "migration-seed"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    seed = tmp_path / "seed.step+"
    a.call("doc_save", {"doc_id": doc, "path": str(seed)})
    assert a.call("daemon_drain", {})["ok"]
    a.close()
    assert _wait_daemon_gone(pid)
    root = tmp_path / "state" / "Chudware" / "hostd_autosave" / os.environ["CHUDWARE_INSTANCE_KEY"]
    session = root / "000-failed"
    session.mkdir(parents=True)
    source = session / "survivor.step+"
    shutil.copyfile(seed, source)
    (session / "survivor.json").write_text(json.dumps({
        "doc_id": "survivor", "name": "survivor", "human_dirty": True,
        "origin_kind": "gui", "revision": 1, "saved_revision": -1,
    }))
    orphan = root / "orphaned"
    saved_orphan = root / "saved-orphan-directory"
    orphan.rename(saved_orphan)
    external = tmp_path / "external"
    external.mkdir()
    orphan.symlink_to(external, target_is_directory=True)
    b = _recovered_daemon(scratch_daemon, pid)
    assert b.call("recovery_list", {}).get("code") == "recovery_unavailable"
    assert source.exists() and (session / "survivor.json").exists()
    pid_b = _pid(b)
    os.kill(pid_b, signal.SIGKILL)
    assert _wait_daemon_gone(pid_b)
    b.close()
    orphan.unlink()
    saved_orphan.rename(orphan)
    c = _recovered_daemon(scratch_daemon, pid_b)
    assert any(e["name"] == "survivor" for e in c.call("recovery_list", {})["entries"])


def test_migration_keeps_all_sessions_and_quarantines_corruption(
    scratch_daemon, monkeypatch, tmp_path
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CHUDWARE_ORPHAN_KEEP", "0")
    a = DaemonClient()
    a.connect()
    pid = _pid(a)
    scratch_daemon.append(pid)
    doc = a.call("doc_new", {"name": "seed"})["doc_id"]
    a.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    seed = tmp_path / "seed.step+"
    a.call("doc_save", {"doc_id": doc, "path": str(seed)})
    assert a.call("daemon_drain", {})["ok"]
    a.close()
    assert _wait_daemon_gone(pid)
    root = tmp_path / "state" / "Chudware" / "hostd_autosave" / os.environ["CHUDWARE_INSTANCE_KEY"]
    for session, name in (("000-old", "older-survivor"), ("001-new", "newer-survivor")):
        directory = root / session
        directory.mkdir(parents=True)
        shutil.copyfile(seed, directory / "doc.step+")
        (directory / "doc.json").write_text(json.dumps({
            "doc_id": name, "name": name, "human_dirty": True,
            "origin_kind": "gui", "revision": 1, "saved_revision": -1,
        }))
    corrupt = root / "002-corrupt"
    corrupt.mkdir()
    (corrupt / "broken.step+").write_bytes(b"not a zip")
    bad_meta = root / "003-corrupt-meta"
    bad_meta.mkdir()
    shutil.copyfile(seed, bad_meta / "doc.step+")
    (bad_meta / "doc.json").write_text("{broken")
    b = _recovered_daemon(scratch_daemon, pid)
    names = {e["name"] for e in b.call("recovery_list", {})["entries"]}
    assert {"older-survivor", "newer-survivor"} <= names
    assert (corrupt / "quarantine" / "broken.step+").read_bytes() == b"not a zip"
    assert (bad_meta / "quarantine" / "doc.step+").read_bytes() == seed.read_bytes()
    assert (bad_meta / "quarantine" / "doc.json").read_text() == "{broken"


def test_daemon_start_identity_changes_on_restart(scratch_daemon):
    a = DaemonClient()
    a.connect()
    old = a.call("daemon_status", {})
    scratch_daemon.append(old["pid"])
    hello = a.call("hello", {"client": "test", "pid": os.getpid(), "proto": 1})
    assert hello["daemon_pid"] == old["daemon_pid"] == old["pid"]
    assert hello["daemon_start_id"] == old["daemon_start_id"]
    assert a.call("daemon_drain", {})["ok"]
    a.close()
    assert _wait_daemon_gone(old["pid"])
    b = _recovered_daemon(scratch_daemon, old["pid"])
    assert b.call("daemon_status", {})["daemon_start_id"] != old["daemon_start_id"]
