"""Phase-4 live-preview integration test (the GUI-side money test, scriptable).

Simulates the GUI's daemon event path without a GL context: a "gui" client
subscribes to "*" (exactly what host/src/core/engine_events.cpp does per frame)
while an "mcp" client builds a part. Asserts the preview arrives as push events
carrying the mesh path — and that NO handoff files are written (the .step+
file handoff is legacy-only; daemon mode replaces it).

Edge cases covered:
  * gui holds edit_hold -> mcp mutation gets retriable busy_preview
  * mcp edits while gui is subscribed/attached ro -> gui gets doc.changed with
    source_kind == "mcp" and an aicm_path
  * ro gui attach upgrades to rw (the GUI's first-edit auto-upgrade path)

Scratch AICAD_INSTANCE_KEY per test = private daemon, never touches a live one.
"""
from __future__ import annotations

import os
import signal
import time
import uuid
from pathlib import Path

import pytest

from mcp_server import daemon_bridge
from mcp_server.daemon_bridge import DaemonClient

HOSTD = daemon_bridge.resolve_hostd_bin()

pytestmark = pytest.mark.skipif(HOSTD is None, reason="aicad-hostd not built")

BOX = [{"op": "add_feature",
        "feature": {"type": "box", "name": "b1",
                    "params": {"length": 10, "width": 10, "height": 10},
                    "refs": {}}}]
BOX2 = [{"op": "add_feature",
         "feature": {"type": "box", "name": "b2",
                     "params": {"length": 5, "width": 5, "height": 5,
                                "at": [30, 0, 0]},
                     "refs": {}}}]

HANDOFF_DIR = Path(os.path.expanduser("~/.local/share/AI-CAD/handoff"))


def _handoff_snapshot() -> set:
    if not HANDOFF_DIR.is_dir():
        return set()
    return {(p.name, p.stat().st_mtime_ns) for p in HANDOFF_DIR.iterdir()}


@pytest.fixture()
def scratch_daemon(monkeypatch):
    key = "pytest-lp-" + uuid.uuid4().hex[:8]
    monkeypatch.setenv("AICAD_INSTANCE_KEY", key)
    monkeypatch.setenv("AICAD_HOSTD_LINGER_SECS", "3")
    monkeypatch.setenv("AICAD_HOSTD_HOLD_SECS", "2")
    monkeypatch.delenv("AICAD_HOSTD_SOCKET", raising=False)
    pids: list = []
    yield pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _gui_client() -> DaemonClient:
    """A subscriber that identifies as the GUI (hello re-send sets the kind)."""
    c = DaemonClient()
    c.connect()
    c.call("hello", {"client": "gui", "pid": os.getpid(), "proto": 1})
    c.call("subscribe", {"doc_ids": "*"})
    return c


def _wait_events(c: DaemonClient, name: str, timeout_s: float = 2.0) -> list:
    """Poll (cheap ping drains the socket) until >=1 event of `name` arrives."""
    out: list = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        c.call("ping")
        out += [e for e in c.poll_events() if e.get("event") == name]
        if out:
            return out
        time.sleep(0.05)
    return out


# ------------------------------------------------------------ the money test


def test_live_preview_events_no_handoff_files(scratch_daemon):
    before = _handoff_snapshot()

    mcp = DaemonClient()
    mcp.connect()
    scratch_daemon.append(mcp.call("daemon_status")["pid"])
    gui = _gui_client()

    # Agent builds a part.
    r = mcp.call("doc_new", {"name": "LivePreviewPart"})
    doc = r["doc_id"]
    assert doc.startswith("doc-cpp")

    opened = _wait_events(gui, "doc.opened")
    assert opened and opened[0]["data"]["doc_id"] == doc
    assert opened[0]["data"]["source_kind"] == "mcp"

    r2 = mcp.call("doc_apply_ops", {"doc_id": doc, "ops": BOX})
    assert not r2.get("_is_error"), r2

    changed = _wait_events(gui, "doc.changed")
    assert changed, "gui must receive doc.changed within 2s of the agent's edit"
    d = changed[0]["data"]
    assert d["doc_id"] == doc
    assert d["source_kind"] == "mcp"
    assert d.get("aicm_path"), "doc.changed must carry the mesh path (live preview)"
    assert Path(d["aicm_path"]).is_file()

    # The daemon path writes ZERO handoff files (legacy .step+ flow only).
    assert _handoff_snapshot() == before, "daemon mode must not touch the handoff dir"


# ------------------------------------------------------------- edge cases


def test_gui_hold_gives_mcp_busy_preview(scratch_daemon):
    mcp = DaemonClient()
    mcp.connect()
    scratch_daemon.append(mcp.call("daemon_status")["pid"])
    gui = _gui_client()

    doc = mcp.call("doc_new", {"name": "HoldPart"})["doc_id"]
    _wait_events(gui, "doc.opened")

    # GUI opens a PropertyManager -> edit_hold (mirrors property_manager.cpp).
    h = gui.call("edit_hold", {"doc_id": doc})
    assert h.get("ok") is True

    r = mcp.call("doc_apply_ops", {"doc_id": doc, "ops": BOX})
    assert r.get("_is_error") and r.get("code") == "busy_preview", r
    assert r.get("retry_ms")

    # Release (PM close) -> the agent's retry succeeds.
    gui.call("edit_release", {"doc_id": doc})
    r2 = mcp.call("doc_apply_ops", {"doc_id": doc, "ops": BOX})
    assert not r2.get("_is_error"), r2


def test_ro_gui_sees_mcp_edits_and_can_upgrade(scratch_daemon):
    mcp = DaemonClient()
    mcp.connect()
    scratch_daemon.append(mcp.call("daemon_status")["pid"])
    gui = _gui_client()

    doc = mcp.call("doc_new", {"name": "RoViewerPart"})["doc_id"]
    _wait_events(gui, "doc.opened")

    # GUI mirror tab: attach read-only (engine_events.cpp openMirrorTab).
    a = gui.call("attach_doc", {"doc_id": doc, "mode": "ro"})
    assert a.get("ok") is True

    r = mcp.call("doc_apply_ops", {"doc_id": doc, "ops": BOX})
    assert not r.get("_is_error"), r
    changed = _wait_events(gui, "doc.changed")
    assert changed and changed[0]["data"]["source_kind"] == "mcp"

    # ro viewer can run the mirror-activation rebuild (doc_rebuild allowlisted)...
    rb = gui.call("doc_rebuild", {"doc_id": doc})
    assert not rb.get("_is_error"), rb
    # ...but not mutate.
    m = gui.call("doc_apply_ops", {"doc_id": doc, "ops": BOX2})
    assert m.get("_is_error") and m.get("code") == "read_only"

    # First-edit auto-upgrade (engine_backend.cpp): attach rw, then retry works.
    up = gui.call("attach_doc", {"doc_id": doc, "mode": "rw"})
    assert up.get("ok") is True
    m2 = gui.call("doc_apply_ops", {"doc_id": doc, "ops": BOX2})
    assert not m2.get("_is_error"), m2
