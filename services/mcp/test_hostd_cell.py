"""Real scratch hostd cells: adopted gateway FD, principals, paths and event privacy.

No connect-or-spawn desktop helper, provider calls, or existing daemon is used.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import tempfile
import uuid
import threading

import pytest

from services.mcp import daemon_bridge

HOSTD = daemon_bridge.resolve_hostd_bin()
ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.name != "posix" or HOSTD is None,
                                reason="built POSIX hostd required")
BOX_OPS = [{"op": "add_feature", "feature": {"type": "box", "name": "b1",
            "params": {"length": 10, "width": 10, "height": 10}, "refs": {}}}]
MESH_PLY = ("ply\nformat ascii 1.0\nelement vertex 4\n"
            "property float x\nproperty float y\nproperty float z\n"
            "element face 2\nproperty list uchar int vertex_indices\nend_header\n"
            "0 0 0\n1 0 0\n1 1 0\n0 1 0\n3 0 1 2\n3 0 2 3\n")


class Wire:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(30)
        self.sock.connect(str(path))
        self.buffer = b""
        self.events = []
        self.next_id = 0

    def close(self):
        self.sock.close()

    def line(self):
        while b"\n" not in self.buffer:
            data = self.sock.recv(65536)
            if not data:
                raise EOFError("cell connection closed")
            self.buffer += data
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def rpc(self, method, params=None):
        self.next_id += 1
        ident = self.next_id
        self.sock.sendall((json.dumps({"id": ident, "method": method,
                                      "params": params or {}}) + "\n").encode())
        while True:
            item = self.line()
            if "event" in item:
                self.events.append(item)
                continue
            assert item.get("id") == ident, item
            return item

    def call(self, method, params=None):
        item = self.rpc(method, params)
        assert "error" not in item, item
        return item["result"]

    def denied(self, method, params, code):
        assert self.rpc(method, params)["error"]["code"] == code

    def drain(self):
        self.call("ping")
        events, self.events = self.events, []
        return events

    def closed(self):
        self.sock.settimeout(5)
        try:
            while True:
                self.events.append(self.line())
        except (EOFError, ConnectionResetError):
            return
        finally:
            self.sock.settimeout(30)


class Cell:
    def __init__(self, root, env_extra=None, hostd_bin=None):
        self.state, self.work = root / "state", root / "work"
        self.state.mkdir(mode=0o700)
        self.work.mkdir()
        self.gateway = root / "gateway.sock"
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(self.gateway))
        self.listener.listen()
        self.workspace = "user:" + str(uuid.uuid4())
        self.wires = []
        env = {key: os.environ[key] for key in ("PATH", "LD_LIBRARY_PATH", "CHUDWARE_LIBCHUDWARE")
               if key in os.environ}
        env.update(HOME=str(self.state), XDG_DATA_HOME=str(self.state),
                   XDG_CONFIG_HOME=str(self.state / "config"),
                   CHUDWARE_CELL_METHODS_FILE=str(ROOT / "contracts/cell_methods.json"),
                   CHUDWARE_HOSTD_LINGER_SECS="600", CHUDWARE_HOSTD_GUI_EXIT_GRACE_S="600")
        env.update(env_extra or {})
        self.proc = subprocess.Popen([str(hostd_bin or HOSTD), "--cell", "--gateway-listen-fd",
                                      str(self.listener.fileno()), "--cell-state", str(self.state),
                                      "--cell-work", str(self.work)], env=env,
                                     pass_fds=(self.listener.fileno(),),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.listener.close()

    def wire(self, path=None):
        wire = Wire(path or self.gateway)
        self.wires.append(wire)
        return wire

    def principal(self, actor, right="editor", epoch=0):
        return {"workspace_key": self.workspace, "actor_key": actor, "right": right,
                "epoch": epoch, "connection_nonce": uuid.uuid4().hex}

    def connect(self, actor, right="editor", epoch=0):
        wire = self.wire()
        params = {"client": "gui", "proto": 1, "principal": self.principal(actor, right, epoch)}
        deadline = time.monotonic() + 60
        while True:
            hello = wire.call("hello", params)
            if hello["state"] == "ready":
                return wire
            assert hello["state"] != "failed", hello
            assert self.proc.poll() is None, "scratch hostd exited during engine load"
            assert time.monotonic() < deadline, "scratch engine did not load"
            time.sleep(0.05)

    def internal(self, actor):
        actor_hex = hashlib.sha256(actor.encode()).hexdigest()[:32]
        return self.wire(self.state / "actors" / actor_hex / "run/hostd.sock")


    def operator(self, wait_ready=True):
        deadline = time.monotonic() + 60
        while True:
            try:
                wire = self.wire(self.state / "run/operator.sock")
                break
            except (FileNotFoundError, ConnectionRefusedError):
                assert self.proc.poll() is None, "scratch cell exited before operator listener"
                assert time.monotonic() < deadline
                time.sleep(0.02)
        wire.call("hello", {"client": "control", "operator": True, "proto": 1})
        while wait_ready and not wire.call("cell_census")["complete"]:
            assert self.proc.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.05)
        return wire

    def stop(self):
        for wire in self.wires:
            wire.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)


@pytest.fixture
def cell():
    # Actor listener suffixes include a 32-character identity digest. pytest's
    # descriptive temporary paths can exceed AF_UNIX's fixed sun_path bound.
    with tempfile.TemporaryDirectory(prefix="cw-cell-", dir="/tmp") as directory:
        scratch = Cell(Path(directory))
        try:
            yield scratch
        finally:
            scratch.stop()


def test_gateway_principal_and_internal_identity(cell):
    actor, viewer = str(uuid.uuid4()), str(uuid.uuid4())
    unauthenticated = cell.wire()
    unauthenticated.denied("hello", {"client": "gui", "proto": 1}, "cell_refused")
    owner = cell.connect(actor, "owner")
    read = cell.connect(viewer, "viewer")
    doc = owner.call("doc_new", {"name": "principals"})["doc_id"]
    assert read.call("attach_doc", {"doc_id": doc, "mode": "rw"})["mode"] == "ro"
    read.denied("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS}, "forbidden_right")
    read.denied("doc_new", {"name": "forbidden"}, "forbidden_right")
    internal = cell.internal(viewer)
    internal.call("hello", {"client": "agent", "proto": 1,
                           "principal": cell.principal(actor, "owner")})
    assert internal.call("attach_doc", {"doc_id": doc, "mode": "rw"})["mode"] == "ro"
    internal.denied("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS}, "forbidden_right")
    internal.denied("principal_update", {"actor_key": viewer, "right": "owner", "epoch": 1},
                    "cell_refused")
    for method in ("doc_unclassified_method", "agent_unclassified_method", "agent_spawn",
                   "daemon_status", "recovery_list", "mfg_status"):
        owner.denied(method, {}, "cell_refused")
    assert owner.call("ping")["ok"]


def test_document_events_require_attachment_and_attribute_actor(cell):
    actor, other = str(uuid.uuid4()), str(uuid.uuid4())
    author = cell.connect(actor)
    reader = cell.connect(other, "viewer")
    stranger = cell.connect(str(uuid.uuid4()))
    reader.call("subscribe", {"doc_ids": "*"})
    stranger.call("subscribe", {"doc_ids": "*"})
    doc = author.call("doc_new", {"name": "private-events"})["doc_id"]
    assert not [e for e in reader.drain() if e.get("event", "").startswith("doc.")]
    reader.call("attach_doc", {"doc_id": doc, "mode": "rw"})
    author.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    changed = [e for e in reader.drain() if e.get("event") == "doc.changed"]
    assert changed and changed[-1]["data"]["actor_key"] == actor
    assert changed[-1]["data"]["doc_id"] == doc
    assert not [e for e in stranger.drain() if e.get("event", "").startswith("doc.")]


def test_mesh_reference_rights_dispatch_and_events(cell):
    actor = str(uuid.uuid4())
    author = cell.connect(actor)
    viewer = cell.connect(str(uuid.uuid4()), "viewer")
    stranger = cell.connect(str(uuid.uuid4()))
    viewer.call("subscribe", {"doc_ids": "*"})
    stranger.call("subscribe", {"doc_ids": "*"})
    doc = author.call("doc_new", {"kind": "part", "name": "reference-events"})["doc_id"]
    assert viewer.call("attach_doc", {"doc_id": doc, "mode": "rw"})["mode"] == "ro"
    (cell.work / "reference.ply").write_text(MESH_PLY)
    assert viewer.call("doc_mesh_ref_list", {"doc_id": doc})["refs"] == []

    added = author.call("doc_mesh_ref_add", {"doc_id": doc, "path": "reference.ply"})
    ref = added["id"]
    assert added["ok"] and added["triangles"] == 2
    summary = next(row for row in author.call("doc_list")["docs"] if row["doc_id"] == doc)
    assert summary["feature_count"] == 0 and summary["empty"] is False
    events = [e for e in viewer.drain() if e.get("event") == "doc.changed"]
    assert len(events) == 1
    assert events[0]["data"]["doc_id"] == doc
    assert events[0]["data"]["revision"] == added["revision"]
    assert events[0]["data"]["method"] == "doc_mesh_ref_add"
    assert events[0]["data"]["actor_key"] == actor
    assert isinstance(events[0]["data"]["tree"], dict)
    assert not [e for e in stranger.drain() if e.get("event", "").startswith("doc.")]

    refs = viewer.call("doc_mesh_ref_list", {"doc_id": doc})
    assert refs["revision"] == added["revision"] and refs["refs"][0]["id"] == ref
    page = viewer.call("doc_mesh_ref_mesh", {"doc_id": doc, "id": ref,
                       "offset_tris": 0, "max_tris": 1, "expected_revision": refs["revision"]})
    assert page["total_tris"] == 2 and page["next_offset_tris"] == 1
    assert len(page["indices"]) == 3
    page = viewer.call("doc_mesh_ref_mesh", {"doc_id": doc, "id": ref,
                       "offset_tris": page["next_offset_tris"], "max_tris": 1,
                       "expected_revision": refs["revision"]})
    assert len(page["indices"]) == 3 and page["next_offset_tris"] is None
    assert not [e for e in viewer.drain() if e.get("event") == "doc.changed"]

    for method, fields in (
        ("doc_mesh_ref_add", {"path": "reference.ply"}),
        ("doc_mesh_ref_update", {"id": ref, "visible": False}),
        ("doc_mesh_ref_remove", {"id": ref}),
    ):
        viewer.denied(method, {"doc_id": doc, **fields}, "forbidden_right")

    updated = author.call("doc_mesh_ref_update", {"doc_id": doc, "id": ref, "visible": False})
    events = [e for e in viewer.drain() if e.get("event") == "doc.changed"]
    assert len(events) == 1 and events[0]["data"]["method"] == "doc_mesh_ref_update"
    assert events[0]["data"]["revision"] == updated["revision"]
    viewer.denied("doc_mesh_ref_mesh", {"doc_id": doc, "id": ref, "offset_tris": 1,
                  "expected_revision": refs["revision"]}, "stale_revision")
    assert viewer.call("doc_mesh_ref_list", {"doc_id": doc})["refs"][0]["visible"] is False
    removed = author.call("doc_mesh_ref_remove", {"doc_id": doc, "id": ref})
    events = [e for e in viewer.drain() if e.get("event") == "doc.changed"]
    assert len(events) == 1 and events[0]["data"]["method"] == "doc_mesh_ref_remove"
    assert events[0]["data"]["revision"] == removed["revision"]
    assert viewer.call("doc_mesh_ref_list", {"doc_id": doc})["refs"] == []
    assert not [e for e in stranger.drain() if e.get("event", "").startswith("doc.")]


def test_mesh_reference_add_confines_workspace_and_actor_paths(cell, tmp_path):
    actor, other = str(uuid.uuid4()), str(uuid.uuid4())
    author = cell.connect(actor)
    cell.connect(other)
    doc = author.call("doc_new", {"kind": "part", "name": "reference-paths"})["doc_id"]
    outside = tmp_path / "outside.ply"
    outside.write_text(MESH_PLY)
    (cell.work / "escape").symlink_to(tmp_path, target_is_directory=True)
    (cell.work / "dangling").symlink_to(tmp_path / "missing", target_is_directory=True)
    other_root = cell.state / "actors" / hashlib.sha256(other.encode()).hexdigest()[:32]
    (other_root / "other.ply").write_text(MESH_PLY)
    for path in (str(outside), "../outside.ply", "escape/outside.ply",
                 "dangling/outside.ply", str(other_root / "other.ply")):
        author.denied("doc_mesh_ref_add", {"doc_id": doc, "path": path}, "cell_refused")
    assert author.call("doc_mesh_ref_list", {"doc_id": doc})["refs"] == []

    own_root = cell.state / "actors" / hashlib.sha256(actor.encode()).hexdigest()[:32]
    (own_root / "reference.ply").write_text(MESH_PLY)
    (own_root / "escape").symlink_to(tmp_path, target_is_directory=True)
    internal = cell.internal(actor)
    internal.call("hello", {"client": "agent", "proto": 1})
    internal.denied("doc_mesh_ref_add", {"doc_id": doc, "path": "/actor/escape/outside.ply"},
                    "cell_refused")
    added = internal.call("doc_mesh_ref_add", {"doc_id": doc, "path": "/actor/reference.ply"})
    assert added["ok"] and added["triangles"] == 2
    assert author.call("doc_mesh_ref_list", {"doc_id": doc})["refs"][0]["id"] == added["id"]


def test_presentation_is_workspace_visible_without_broadening_other_events(cell):
    author = cell.connect(str(uuid.uuid4()))
    reader = cell.connect(str(uuid.uuid4()), "viewer")
    reader.call("subscribe", {"doc_ids": "*"})
    doc = author.call("doc_new", {"name": "shared-presentation"})["doc_id"]
    author.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    assert not [e for e in reader.drain() if e.get("event", "").startswith("doc.")]
    reader.denied("doc_present", {"doc_id": doc}, "forbidden_right")
    assert author.call("doc_present", {"doc_id": doc})["presented"] is True
    events = reader.drain()
    assert [e["data"] for e in events if e["event"] == "doc.presented"] == [
        {"doc_id": doc, "name": "shared-presentation", "revision": 1}
    ]
    assert next(r for r in reader.call("doc_list")["docs"] if r["doc_id"] == doc)["presented"]
    with tempfile.TemporaryDirectory(prefix="cw-cell-other-", dir="/tmp") as directory:
        other = Cell(Path(directory))
        try:
            outsider = other.connect(str(uuid.uuid4()))
            outsider.denied("doc_present", {"doc_id": doc}, "not_found")
        finally:
            other.stop()


def test_paths_are_workspace_relative_and_symlinks_cannot_escape(cell, tmp_path):
    owner = cell.connect(str(uuid.uuid4()), "owner")
    doc = owner.call("doc_new", {"name": "paths"})["doc_id"]
    owner.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
    for path in (str(tmp_path / "outside.step+"), "../outside.step+"):
        owner.denied("doc_save", {"doc_id": doc, "path": path}, "cell_refused")
    (cell.work / "escape").symlink_to(tmp_path, target_is_directory=True)
    owner.denied("doc_save", {"doc_id": doc, "path": "escape/outside.step+"}, "cell_refused")
    (cell.work / "missing-escape").symlink_to(tmp_path / "not-created", target_is_directory=True)
    owner.denied("doc_open", {"path": "missing-escape/outside.step+"}, "cell_refused")
    owner.denied("doc_save", {"doc_id": doc, "path": "missing-escape/outside.step+"}, "cell_refused")
    for dest in (str(tmp_path / "outside.zip"), "../outside.zip", "escape/outside.zip",
                 "missing-escape/outside.zip"):
        owner.denied("export_reveng_bundle", {"session_id": "unresolved", "dest": dest},
                     "cell_refused")
    (cell.work / "future").symlink_to(cell.work / "not-created", target_is_directory=True)
    # Runtime paths receive the same confinement as document paths, including
    # paths nested in turn options. Nested absolute references never become a
    # general escape hatch, even when they point into the workspace.
    for key in ("omp_session_file", "omp_session_dir"):
        owner.denied("agent_admin_turn", {key: str(tmp_path / "outside")}, "cell_refused")
        owner.denied("agent_admin_turn", {"options": {key: str(cell.work / "inside")}},
                     "cell_refused")
        owner.denied("agent_admin_turn", {"options": {key: "../outside"}}, "cell_refused")
    owner.call("doc_save", {"doc_id": doc, "path": "future/inside.step+"})
    assert (cell.work / "not-created/inside.step+").is_file()
    saved = owner.call("doc_save", {"doc_id": doc, "path": "part.step+"})
    assert (cell.work / "part.step+").is_file()
    for key in ("path", "saved_path", "file_path"):
        if key in saved:
            assert saved[key] == "part.step+"
    assert not (tmp_path / "outside.step+").exists()
    viewer = cell.connect(str(uuid.uuid4()), "viewer")
    opened = viewer.call("doc_open", {"path": "part.step+"})
    read_doc = opened["doc_id"]
    assert viewer.call("attach_doc", {"doc_id": read_doc, "mode": "rw"})["mode"] == "ro"
    rendered = viewer.call("doc_render_png", {"doc_id": read_doc, "out_path": "preview.png"})
    assert rendered["ok"] and rendered["path"] == "preview.png"
    assert (cell.work / "preview.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    viewer.denied("doc_render_png", {"doc_id": read_doc, "out_path": "../preview.png"},
                  "cell_refused")
    viewer.denied("doc_apply_ops", {"doc_id": read_doc, "ops": BOX_OPS}, "forbidden_right")


def test_embedded_component_references_remain_relative(cell):
    owner = cell.connect(str(uuid.uuid4()), "owner")
    part = owner.call("doc_new", {"name": "source"})["doc_id"]
    owner.call("doc_apply_ops", {"doc_id": part, "ops": BOX_OPS})
    owner.call("doc_save", {"doc_id": part, "path": "source.step+"})
    assembly = owner.call("doc_new", {"name": "assembly", "kind": "assembly"})["doc_id"]
    inserted = owner.call("doc_apply_ops", {"doc_id": assembly, "ops": [
        {"op": "asm_insert_component", "source": {"path": "source.step+"}}]})
    assert inserted["tree"]["components"][0]["source"]["path"] == "source.step+"
    for reference in (str(cell.work / "source.step+"), "../source.step+",
                      "folder/../source.step+", "source.step+\x00ignored"):
        owner.denied("doc_apply_ops", {"doc_id": assembly, "ops": [
            {"op": "asm_insert_component", "source": {"path": reference}}]}, "cell_refused")


def test_revoke_and_role_update_close_connections_and_cap_reconnect(cell):
    actor, other = str(uuid.uuid4()), str(uuid.uuid4())
    victim = cell.connect(actor)
    internal = cell.internal(actor)
    internal.call("hello", {"client": "agent", "proto": 1})
    admin = cell.connect(other, "owner")
    victim.call("subscribe", {"doc_ids": "*"})
    assert admin.call("principal_revoke", {"actor_key": actor, "epoch": 1})["ok"]
    victim.closed()
    internal.closed()
    stale = cell.wire()
    stale.denied("hello", {"client": "gui", "proto": 1,
                          "principal": cell.principal(actor, epoch=0)}, "revoked")
    assert admin.call("ping")["ok"]
    assert admin.call("principal_update", {"actor_key": actor, "right": "viewer", "epoch": 2})["ok"]
    read = cell.connect(actor, "viewer", 2)
    read.denied("doc_new", {"name": "no-upgrade"}, "forbidden_right")
    stale.denied("hello", {"client": "gui", "proto": 1,
                          "principal": cell.principal(actor, "editor", 2)}, "revoked")


def test_cell_refuses_engine_without_cache_refresh_export(tmp_path):
    compiler = shutil.which("c++")
    if compiler is None or not hasattr(os, "uname") or os.uname().sysname != "Linux":
        pytest.skip("Linux C++ compiler required for incompatible-engine ABI regression")
    source = tmp_path / "old_engine.cpp"
    source.write_text(r'''
        #include <cstdlib>
        #include <cstring>
        extern "C" int chudware_call(const char*, const char*, char** out) {
            *out = strdup("{\"code\":\"unknown_method\",\"message\":\"old engine\"}");
            return 1;
        }
        extern "C" void chudware_free(char* p) { free(p); }
        extern "C" const char* chudware_abi_version() { return "1"; }
    ''')
    library = tmp_path / "libold_engine.so"
    subprocess.run([compiler, "-shared", "-fPIC", str(source), "-o", str(library)], check=True)
    with tempfile.TemporaryDirectory(prefix="cw-cell-abi-", dir="/tmp") as directory:
        scratch = Cell(Path(directory), {"CHUDWARE_LIBCHUDWARE": str(library)})
        try:
            assert scratch.proc.wait(timeout=30) != 0
            logs = "\n".join(p.read_text(errors="replace") for p in (scratch.state / "logs").glob("*")
                             if p.is_file())
            assert "cell engine lacks cache refresh publication export" in logs
        finally:
            scratch.stop()


def test_operator_is_gateway_private_control_only_and_creates_no_actor(cell):
    operator = cell.operator()
    census = operator.call("cell_census")
    assert census["schema"] == 1 and census["admission"] == "open"
    assert census["complete"] and census["quiescent"]
    assert census["sessions"] == census["rpc_inflight"] == census["active"] == 0
    assert not (cell.state / "actors").exists()
    assert set(census) == {"schema", "admission", "generation", "sessions", "rpc_inflight",
                          "native", "workers", "active", "complete", "quiescent", "exiting"}
    for method in ("doc_new", "principal_update", "daemon_status", "worker_activity"):
        operator.denied(method, {}, "cell_refused")
    gateway = cell.wire()
    gateway.denied("hello", {"client": "control", "operator": True}, "cell_refused")
    gateway.close()
    private = cell.wire(cell.state / "run/operator.sock")
    private.denied("hello", {"client": "gui", "principal": cell.principal("fake")}, "cell_refused")
    private.close()
    owner = cell.connect("owner", "owner")
    for method in ("cell_census", "cell_drain", "worker_activity", "daemon_status"):
        owner.denied(method, {}, "cell_refused")
    internal = cell.internal("owner")
    internal.denied("hello", {"client": "control", "operator": True}, "cell_refused")
    internal.call("hello", {"client": "agent"})
    internal.denied("cell_census", {}, "cell_refused")
    assert operator.call("cell_census")["sessions"] == 2


def test_stale_drain_fails_closed_on_cell_actor_sockets():
    # Touch only the private executable watched by this scratch cell.
    with tempfile.TemporaryDirectory(prefix="cw-cell-drain-", dir="/tmp") as directory:
        root = Path(directory)
        binary = root / "chudware-hostd"
        shutil.copy2(HOSTD, binary)
        libname = "libchudware.dylib" if sys.platform == "darwin" else "libchudware.so"
        library = os.environ.get("CHUDWARE_LIBCHUDWARE") or str(Path(HOSTD).resolve().parent / libname)
        scratch = Cell(root, {"CHUDWARE_HOSTD_STALECHECK_S": "1",
                              "CHUDWARE_LIBCHUDWARE": library}, binary)
        try:
            owner = scratch.connect("owner", "owner")
            internal = scratch.internal("owner")
            internal.call("hello", {"client": "agent"})
            operator = scratch.operator()
            time.sleep(1.2)
            old = binary.stat().st_mtime_ns
            os.utime(binary, ns=(old + 2_000_000_000, old + 2_000_000_000))

            deadline = time.monotonic() + 10
            while True:
                probe = scratch.internal("owner")
                reply = probe.rpc("hello", {"client": "agent"})
                probe.close()
                if "error" in reply:
                    assert reply["error"]["code"] == "daemon_draining", reply
                    assert reply["error"]["reason"] == "stale_binary"
                    break
                assert time.monotonic() < deadline, "cell did not start stale drain"
                time.sleep(0.05)

            # Actor sockets cannot masquerade as the operator to escape the
            # drain; already-authenticated connections retain their rights.
            spoof = scratch.internal("owner")
            spoof.denied("hello", {"client": "control"}, "daemon_draining")
            owner.call("ping")
            internal.call("ping")
            assert operator.call("cell_census")["sessions"] >= 2
            fresh_operator = scratch.operator()
            fresh_operator.denied("doc_new", {}, "cell_refused")
        finally:
            scratch.stop()

def test_pending_handshake_is_counted_and_accepted_work_continues_during_drain(cell):
    operator = cell.operator()
    pending = cell.wire()
    deadline = time.monotonic() + 5
    while operator.call("cell_census")["sessions"] != 1:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    closed = operator.call("cell_drain", {"admission": "closed", "exit": "when_quiescent"})
    assert closed["sessions"] == 1 and not closed["quiescent"] and not closed["exiting"]
    # Registration won the close race: hello and subsequent work still complete.
    assert pending.call("hello", {"client": "gui", "principal": cell.principal("accepted", "owner")})["cell"]
    assert pending.call("ping")["ok"]
    internal = cell.internal("accepted")
    internal.call("hello", {"client": "agent"})
    assert internal.call("ping")["ok"]
    new = cell.wire()
    new.denied("hello", {"client": "gui", "principal": cell.principal("new")}, "daemon_draining")
    # Principal control retains narrow authority during maintenance.
    control = cell.wire()
    control.call("hello", {"client": "control", "principal": cell.principal("manager", "owner")})
    control.denied("doc_new", {}, "cell_refused")
    assert control.call("principal_update", {"actor_key": "accepted", "right": "owner", "epoch": 1})["ok"]
    control.close()
    internal.close()
    pending.close()
    assert cell.proc.wait(timeout=10) == 0


def test_new_connection_close_race_is_counted_or_refused(cell):
    operator = cell.operator()
    barrier = threading.Barrier(17)
    accepted, refused, failures = [], [], []
    lock = threading.Lock()

    def connect(index):
        try:
            barrier.wait()
            wire = cell.wire()
            reply = wire.rpc("hello", {"client": "gui", "principal": cell.principal(f"race-{index}")})
            with lock:
                if "result" in reply:
                    accepted.append(wire)
                else:
                    assert reply["error"]["code"] == "daemon_draining"
                    refused.append(wire)
        except BaseException as exc:
            with lock:
                failures.append(exc)

    threads = [threading.Thread(target=connect, args=(index,)) for index in range(16)]
    for thread in threads:
        thread.start()
    barrier.wait()
    operator.call("cell_drain", {"admission": "closed", "exit": "none"})
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert not failures
    assert len(accepted) + len(refused) == 16
    census = operator.call("cell_census")
    assert census["sessions"] == len(accepted)
    assert all(wire.call("ping")["ok"] for wire in accepted)


def test_cell_reopen_is_generation_fenced_and_normal_work_resumes(cell):
    operator = cell.operator()
    before = operator.call("cell_census")
    closed = operator.call("cell_drain", {"admission": "closed", "exit": "none"})
    assert closed["generation"] == before["generation"] + 1
    assert operator.call("cell_drain", {"admission": "closed", "exit": "none"})["generation"] == closed["generation"]
    operator.denied("cell_drain", {"admission": "open"}, "stale_generation")
    operator.denied("cell_drain", {"admission": "open", "generation": before["generation"]}, "stale_generation")
    opened = operator.call("cell_drain", {"admission": "open", "generation": closed["generation"]})
    assert opened["admission"] == "open" and opened["generation"] == closed["generation"] + 1
    assert cell.connect("after-maintenance").call("ping")["ok"]


def test_dirty_document_pins_idle_and_gc_until_explicit_save():
    with tempfile.TemporaryDirectory(prefix="cw-cell-dirty-", dir="/tmp") as directory:
        scratch = Cell(Path(directory), {"CHUDWARE_HOSTD_GUI_EXIT_GRACE_S": "1",
                                        "CHUDWARE_DOC_GC_GRACE_S": "1"})
        try:
            owner = scratch.connect("dirty-owner", "owner")
            doc = owner.call("doc_new", {"name": "must-keep"})["doc_id"]
            owner.call("doc_apply_ops", {"doc_id": doc, "ops": BOX_OPS})
            operator = scratch.operator()
            assert operator.call("cell_census")["native"]["dirty_documents"] == 1
            owner.close()
            time.sleep(3)
            assert scratch.proc.poll() is None
            census = operator.call("cell_census")
            assert census["sessions"] == 0 and not census["quiescent"]
            assert census["native"]["dirty_documents"] == 1
            resumed = scratch.connect("dirty-owner", "owner")
            assert any(row["doc_id"] == doc for row in resumed.call("doc_list")["docs"])
            resumed.call("doc_save", {"doc_id": doc, "path": "must-keep.step+"})
            assert operator.call("cell_census")["native"]["dirty_documents"] == 0
            resumed.close()
            assert scratch.proc.wait(timeout=10) == 0
            assert (scratch.work / "must-keep.step+").is_file()
        finally:
            scratch.stop()


def test_profiler_pins_native_self_exit_and_actor_control_can_finish_it(cell):
    owner = cell.connect("profiler-owner", "owner")
    operator = cell.operator()
    owner.call("prof_start")
    assert operator.call("cell_census")["native"]["profiler"] == 1
    operator.call("cell_drain", {"admission": "closed", "exit": "when_quiescent"})
    owner.close()
    time.sleep(2)
    assert cell.proc.poll() is None
    assert operator.call("cell_census")["native"]["profiler"] == 1
    internal = cell.internal("profiler-owner")
    internal.call("hello", {"client": "agent"})
    internal.call("prof_stop")
    internal.close()
    assert cell.proc.wait(timeout=10) == 0


def test_engine_without_activity_capability_is_unknown_and_cannot_self_exit(tmp_path):
    compiler = shutil.which("c++")
    if compiler is None or os.uname().sysname != "Linux":
        pytest.skip("Linux C++ compiler required for legacy activity capability regression")
    source = tmp_path / "legacy_activity.cpp"
    source.write_text(r'''
        #include <cstdlib>
        #include <cstring>
        extern "C" int chudware_call(const char*, const char*, char** out) {
            *out = strdup("{\"code\":\"unknown_method\",\"message\":\"legacy engine\"}");
            return 1;
        }
        extern "C" void chudware_free(char* p) { free(p); }
        extern "C" const char* chudware_abi_version() { return "1"; }
        extern "C" void chudware_set_cache_refresh_sink(
            void (*)(const char*, const char*, void*), void*) {}
    ''')
    library = tmp_path / "liblegacy_activity.so"
    subprocess.run([compiler, "-shared", "-fPIC", str(source), "-o", str(library)], check=True)
    with tempfile.TemporaryDirectory(prefix="cw-cell-legacy-", dir="/tmp") as directory:
        scratch = Cell(Path(directory), {"CHUDWARE_LIBCHUDWARE": str(library),
                                        "CHUDWARE_HOSTD_GUI_EXIT_GRACE_S": "1"})
        try:
            customer = scratch.connect("legacy-owner")
            operator = scratch.operator(wait_ready=False)
            customer.close()
            closed = operator.call("cell_drain", {"admission": "closed", "exit": "when_quiescent"})
            assert not closed["complete"] and not closed["quiescent"] and not closed["exiting"]
            time.sleep(3)
            assert scratch.proc.poll() is None
            census = operator.call("cell_census")
            assert census["sessions"] == 0 and not census["complete"] and not census["quiescent"]
        finally:
            scratch.stop()


def test_private_principal_control_is_narrow_and_creates_no_actor_listener(cell):
    operator = cell.operator()
    control = cell.wire(cell.state / "run/operator.sock")
    control.call("hello", {"client": "control", "principal": cell.principal("control-owner", "owner")})
    assert not (cell.state / "actors").exists()
    assert operator.call("cell_census")["sessions"] == 0
    for method in ("doc_new", "cell_census", "cell_drain", "daemon_status"):
        control.denied(method, {}, "cell_refused")
    closed = operator.call("cell_drain", {"admission": "closed", "exit": "none"})
    assert control.call("principal_revoke", {"actor_key": "retired-actor", "epoch": 1})["ok"]
    fresh = cell.wire(cell.state / "run/operator.sock")
    fresh.call("hello", {"client": "control", "principal": cell.principal("second-control", "owner")})
    assert fresh.call("principal_update", {"actor_key": "retired-actor", "right": "viewer", "epoch": 2})["ok"]
    assert not (cell.state / "actors").exists()
    operator.call("cell_drain", {"admission": "open", "generation": closed["generation"]})
    assert cell.connect("retired-actor", "viewer", 2).call("ping")["ok"]
