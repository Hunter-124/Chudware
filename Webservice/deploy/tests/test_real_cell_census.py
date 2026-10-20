"""Real pinned SDK/native census lifecycle in explicitly authorized scratch cells.

Uses the same CHUDWARE_TEST_CELL_STORAGE_ROOT/CHUDWARE_TEST_CELL_QUOTA_HELPER
prerequisites as test_two_tenant_cells. The service-manager seam observes only
fixture-owned Popen PID/start-time lifetimes, not installed product units. Local
official-wire responses drive the actual SDK; activity replies are never replaced.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shlex
import socket
import struct
import time
import uuid

import pytest

from _two_tenant_support import (
    A, BYOK, box, disposable_postgres, gateway_harness, real_payload, two_tenants,
)
from test_two_tenant_cells import start_provider_turn


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start: str
    executable: tuple[int, int]


def _process(pid):
    root = Path("/proc") / str(pid)
    try:
        fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] in {"Z", "X"}:
            return None
        executable = (root / "exe").stat()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return ProcessIdentity(pid, fields[19], (executable.st_dev, executable.st_ino))


def _owned_processes(cell):
    """Follow every owning thread: hostd workers fork on a non-main thread."""
    pending, seen, live = [cell.proc.pid], set(), {}
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        assert len(seen) <= 512, "owned process census exceeded fixture bound"
        root = Path("/proc") / str(pid)
        try:
            record = _process(pid)
            if record is None:
                continue
            live[pid] = record
            tasks = list((root / "task").iterdir())
            assert len(tasks) <= 4096, "owned thread census exceeded fixture bound"
            for task in tasks:
                try:
                    pending.extend(int(value) for value in (task / "children").read_text().split())
                except FileNotFoundError:
                    pass  # Only an already-owned thread disappeared during observation.
        except (FileNotFoundError, ProcessLookupError):
            pass
    return live


def _sdk_identity(h, cell):
    pinned = (h.payload.path / "bin/chud").stat()
    executable = (pinned.st_dev, pinned.st_ino)
    candidates = [record for record in _owned_processes(cell).values()
                  if record.executable == executable]
    assert len(candidates) == 1, ("expected one actual pinned SDK root", candidates, h.log(cell.principal.cell_id))
    return candidates[0]


def _same_sdk(h, cell, identity):
    assert _sdk_identity(h, cell) == identity, "census disposed, respawned or lost the persistent SDK"


def _operator(cell, method, params=None):
    """Connect only to the existing private native listener; never activate a cell."""
    path = cell.directory / "state/run/operator.sock"
    assert path.is_socket(), f"owned native operator listener missing: {path}"
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(10)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            connection.connect(f"/proc/self/fd/{directory}/operator.sock")
        finally:
            os.close(directory)
        with connection.makefile("rwb") as stream:
            for ident, command, arguments in (
                (-1, "hello", {"client": "control", "operator": True, "proto": 1}),
                (1, method, params or {}),
            ):
                stream.write(json.dumps({"id": ident, "method": command, "params": arguments}).encode() + b"\n")
                stream.flush()
                raw = stream.readline(65537)
                assert len(raw) <= 65536 and raw.endswith(b"\n"), (command, raw)
                response = json.loads(raw)
                assert response.get("id") == ident and "error" not in response, response
                assert isinstance(response.get("result"), dict), response
            return response["result"]


def _census(cell):
    value = _operator(cell, "cell_census")
    assert value["schema"] == 1 and value["admission"] in {"open", "closed"}, value
    counts = [value[key] for key in ("generation", "sessions", "rpc_inflight", "active")]
    counts += [value["native"][key] for key in ("cache_verification", "profiler", "dirty_documents")]
    counts += [value["workers"][key] for key in ("busy", "background", "unknown")]
    assert all(type(count) is int and count >= 0 for count in counts), value
    assert all(type(value[key]) is bool for key in ("complete", "quiescent", "exiting")), value
    assert value["active"] == value["sessions"] + value["rpc_inflight"] + sum(value["native"].values()) + sum(value["workers"].values()), value
    assert value["quiescent"] == (value["complete"] and value["active"] == 0), value
    assert not value["workers"]["unknown"] or not value["complete"], value
    return value


def _await_census(h, cell, predicate, *, timeout=20):
    """Wait on authoritative native round trips, not an arbitrary sleep interval."""
    deadline, recent = time.monotonic() + timeout, []
    while True:
        assert cell.proc.poll() is None, ("owned cell exited before the required census", recent, h.log(cell.principal.cell_id))
        value = _census(cell)
        recent = (recent + [value])[-4:]
        if predicate(value):
            return value
        assert time.monotonic() < deadline, ("native census never reached the required lifecycle state", recent, h.log(cell.principal.cell_id))


def _idle(value, *, sessions, dirty=0):
    return (value["complete"] and not value["exiting"]
            and value["sessions"] == sessions and value["rpc_inflight"] == 0
            and value["native"] == {"cache_verification": 0, "profiler": 0, "dirty_documents": dirty}
            and value["workers"] == {"busy": 0, "background": 0, "unknown": 0}
            and value["active"] == sessions + dirty)


def _close_socket(h, ws):
    ws.close()
    h.sockets.remove(ws)  # Existing harness cleanup closes only still-owned live handles.


def _complete_provider_turn(h, ws, ident, row):
    h.provider_wire.release(BYOK[A])
    response = ws.response(ident, timeout=45)
    assert "result" in response and response["result"].get("ok") is not False, response
    assert row.closed.wait(10) and row.completed, "actual upstream stream did not complete"


@pytest.fixture
def census_cell(two_tenants):
    h = two_tenants
    if h.payload.chud is None or h.payload.sdk_blocked:
        pytest.skip("real SDK census blocked: " + (h.payload.chud_blocked if h.payload.chud is None else h.payload.sdk_blocked))
    ws = h.socket(A)
    return h, ws, h.running[h.principal(A).cell_id]


def test_held_real_turn_then_idle_sdk_pins_only_sessions_and_dirty_docs(census_cell):
    h, ws, cell = census_cell
    before = _await_census(h, cell, lambda value: _idle(value, sessions=1))
    assert not before["quiescent"] and before["admission"] == "open", before
    doc, _ = box(ws, "real-sdk-census-dirty")
    handles = ws.ok("agent_handles_new")
    ident, row = start_provider_turn(h, ws, handles, A)
    sdk = _sdk_identity(h, cell)
    try:
        held = _census(cell)
        assert held["sessions"] == 1 and held["rpc_inflight"] == 1, held
        assert held["native"] == {"cache_verification": 0, "profiler": 0, "dirty_documents": 1}, held
        assert held["workers"] == {"busy": 1, "background": 0, "unknown": 0}, held
        assert held["complete"] and held["active"] == 4, held
        assert not held["quiescent"] and not held["exiting"], held
        fenced = _operator(cell, "cell_drain", {"admission": "closed", "exit": "when_quiescent", "generation": held["generation"]})
        assert fenced["admission"] == "closed" and fenced["generation"] == held["generation"] + 1, fenced
        assert fenced["rpc_inflight"] == fenced["workers"]["busy"] == 1, fenced
        assert not fenced["quiescent"] and not fenced["exiting"], fenced
        assert not row.completed and ident not in ws.replies and cell.proc.poll() is None
        _same_sdk(h, cell, sdk)
        _complete_provider_turn(h, ws, ident, row)
    finally:
        h.provider_wire.release(BYOK[A])

    stats = ws.ok("chudcode_session_stats", {"session_id": handles["session_id"]})
    assert stats["userMessages"] >= 1 and stats["assistantMessages"] >= 1, stats
    # Two independent raw-state/process attestations must not count a cached SDK
    # as a job, nor leave it permanently unknown after its actual turn terminal.
    for _ in range(2):
        parked = _await_census(h, cell, lambda value: _idle(value, sessions=1, dirty=1))
        assert parked["admission"] == "closed" and not parked["quiescent"], parked
        _same_sdk(h, cell, sdk)

    # Exercise both actual CAD connection owners: picked-context preflight on
    # the persistent request thread and a live SDK host tool on its own lease.
    continued = ws.ok("agent_handles_new", {"session_id": handles["session_id"]})
    feature = ws.ok("doc_tree", {"doc_id": doc})["tree"]["features"][0]["id"]
    offset = len(h.provider_wire.records)
    h.provider_wire.queue_tool_call(BYOK[A], name="write", arguments={
        "i": "Listing current CAD parts", "path": "xd://list_parts", "content": "{}"})
    cad_turn = ws.send("agent_admin_turn", {**continued, "provider": "openai", "model": "gpt-4o-mini",
        "request": "List the live CAD parts, then answer READY.", "workspace_dir": "/work",
        "scope": {"doc_id": doc}, "context_refs": [{"kind": "feature", "doc_id": doc, "id": feature}]})
    scripted = h.provider_wire.wait_request(BYOK[A], offset=offset, primary=True)
    assert scripted.tool_call is not None, scripted
    cad_result = ws.response(cad_turn, timeout=60)
    assert "result" in cad_result and cad_result["result"].get("ok") is not False, cad_result
    assert scripted.closed.wait(10) and scripted.completed, scripted
    final = h.provider_wire.wait_request(
        BYOK[A], offset=h.provider_wire.records.index(scripted) + 1, primary=True)
    assert final.closed.wait(10) and final.completed and final.tool_call is None, final
    outputs = [item for item in final.body.get("input", [])
               if isinstance(item, dict) and item.get("type") == "function_call_output"]
    assert outputs and doc in json.dumps(outputs), ("actual CAD tool result missing from SDK continuation", final.body)
    stats = ws.ok("chudcode_session_stats", {"session_id": handles["session_id"]})
    assert stats["userMessages"] >= 2 and stats["toolCalls"] >= 1 and stats["toolResults"] >= 1, stats
    for _ in range(2):
        parked = _await_census(h, cell, lambda value: _idle(value, sessions=1, dirty=1))
        assert parked["admission"] == "closed" and not parked["quiescent"], parked
        _same_sdk(h, cell, sdk)

    _close_socket(h, ws)
    dirty = _await_census(h, cell, lambda value: _idle(value, sessions=0, dirty=1))
    assert not dirty["quiescent"] and dirty["active"] == 1, dirty
    _same_sdk(h, cell, sdk)
    reopened = _operator(cell, "cell_drain", {"admission": "open", "exit": "none", "generation": dirty["generation"]})
    assert reopened["admission"] == "open" and reopened["generation"] == dirty["generation"] + 1, reopened
    resumed = h.socket(A)
    observer = h.socket(A)
    assert any(item["doc_id"] == doc for item in resumed.ok("doc_list")["docs"])
    resumed.ok("doc_save", {"doc_id": doc, "path": "real-sdk-census-dirty.step+"})
    assert (cell.principal.root / "real-sdk-census-dirty.step+").is_file()
    saved = _await_census(h, cell, lambda value: _idle(value, sessions=2))
    assert not saved["quiescent"] and saved["active"] == 2, saved
    _same_sdk(h, cell, sdk)
    closed = _operator(cell, "cell_drain", {"admission": "closed", "exit": "none", "generation": saved["generation"]})
    assert closed["sessions"] == 2 and not closed["quiescent"] and not closed["exiting"], closed
    _close_socket(h, resumed)
    independent = _await_census(h, cell, lambda value: _idle(value, sessions=1))
    assert independent["active"] == 1 and not independent["quiescent"], independent
    assert observer.ok("ping")["ok"], "closing another turn/browser must not close this user connection"
    _same_sdk(h, cell, sdk)
    _close_socket(h, observer)
    quiet = _await_census(h, cell, lambda value: _idle(value, sessions=0))
    assert quiet["quiescent"] and quiet["active"] == 0 and quiet["admission"] == "closed", quiet
    _same_sdk(h, cell, sdk)
    acknowledged = _operator(cell, "cell_drain", {"admission": "closed", "exit": "when_quiescent", "generation": quiet["generation"]})
    assert acknowledged["complete"] and acknowledged["quiescent"] and acknowledged["active"] == 0, acknowledged
    assert acknowledged["generation"] == quiet["generation"], acknowledged
    assert cell.proc.wait(timeout=20) == 0, h.log(cell.principal.cell_id)


@contextmanager
def _detached_sdk_child(cell):
    """A real SDK bash child blocks only on our socket; EOF always releases it."""
    name = ".census-" + uuid.uuid4().hex[:12] + ".sock"
    path = cell.principal.root / name
    directory = os.open(cell.principal.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    with socket.socket(socket.AF_UNIX) as listener:
        try:
            # Bind through the owned directory fd, avoiding AF_UNIX's host path
            # length limit without exposing an unrelated /tmp listener to actors.
            listener.bind(f"/proc/self/fd/{directory}/{name}")
            path.chmod(0o600)
            listener.listen(1)
            listener.settimeout(45)
            child_code = '''
import json, os, socket, sys
from pathlib import Path
ancestry = []
pid = os.getpid()
for _ in range(32):
    root = Path('/proc') / str(pid)
    fields = (root / 'stat').read_text().rsplit(')', 1)[1].split()
    executable = (root / 'exe').stat()
    ancestry.append({'pid': pid, 'parent': int(fields[1]), 'start': fields[19],
                     'session': int(fields[3]), 'executable': [executable.st_dev, executable.st_ino]})
    if pid == 1:
        break
    pid = int(fields[1])
else:
    raise RuntimeError('owned child ancestry exceeded bound')
with socket.socket(socket.AF_UNIX) as connection:
    connection.connect(sys.argv[1])
    connection.sendall(json.dumps(ancestry).encode() + b'\\n')
    os.write(int(sys.argv[2]), b'1')
    os.close(int(sys.argv[2]))
    if connection.recv(1):
        connection.sendall(b'done\\n')
'''
            launcher = f'''
import os, subprocess, sys
read_fd, write_fd = os.pipe()
try:
    subprocess.Popen([sys.executable, '-I', '-S', '-c', {child_code!r}, {('/work/' + name)!r}, str(write_fd)],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, pass_fds=(write_fd,), start_new_session=True)
finally:
    os.close(write_fd)
try:
    assert os.read(read_fd, 1) == b'1', 'detached child never reported its actual SDK ancestry'
finally:
    os.close(read_fd)
print('owned-detached-child-ready')
'''
            command = "/opt/chudware/venv/bin/python3 -I -S -c " + shlex.quote(launcher)
            yield listener, command
        finally:
            # Closing our sole server endpoint releases even a failed assertion's
            # child, without signalling any SDK, worker or product process.
            listener.close()
            path.unlink(missing_ok=True)
            os.close(directory)


def test_detached_live_sdk_descendant_is_not_exempt_then_really_finishes(census_cell):
    h, ws, cell = census_cell
    handles = ws.ok("agent_handles_new")
    ident, row = start_provider_turn(h, ws, handles, A)
    sdk = _sdk_identity(h, cell)
    try:
        _complete_provider_turn(h, ws, ident, row)
    finally:
        h.provider_wire.release(BYOK[A])
    _await_census(h, cell, lambda value: _idle(value, sessions=1))
    _same_sdk(h, cell, sdk)

    with _detached_sdk_child(cell) as (listener, command):
        offset = len(h.provider_wire.records)
        h.provider_wire.queue_tool_call(BYOK[A], name="bash", arguments={"command": command})
        turn = ws.send("agent_admin_turn", {**handles, "provider": "openai", "model": "gpt-4o-mini",
                       "request": "Run the supplied local fixture command, then answer READY.", "workspace_dir": "/work"})
        scripted = h.provider_wire.wait_request(BYOK[A], offset=offset, primary=True)
        assert scripted.tool_call is not None and scripted.path == "/v1/responses", scripted
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(15)
            host_pid, _, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
            with connection.makefile("rb") as child_stream:
                ancestry = json.loads(child_stream.readline(32769))
                assert isinstance(ancestry, list) and 1 < len(ancestry) <= 32, ancestry
                assert ancestry[0]["session"] == ancestry[0]["pid"], ancestry
                assert any(item["start"] == sdk.start and tuple(item["executable"]) == sdk.executable
                           for item in ancestry[1:]), ("detached fixture was not an actual pinned SDK descendant", ancestry, sdk)
                child = _process(host_pid)
                assert child is not None and child.start == ancestry[0]["start"], (host_pid, ancestry)
                # The launcher exits as soon as this socket is connected. A
                # single /proc children walk can miss that reparenting, so
                # attest the live peer's actual isolated SDK namespaces.
                for namespace in ("pid", "mnt"):
                    owned_namespace = (Path("/proc") / str(sdk.pid) / "ns" / namespace).stat()
                    child_namespace = (Path("/proc") / str(host_pid) / "ns" / namespace).stat()
                    outside_namespace = (Path("/proc/self/ns") / namespace).stat()
                    assert (child_namespace.st_dev, child_namespace.st_ino) == (
                        owned_namespace.st_dev, owned_namespace.st_ino), "gate peer is outside the fixture-owned actor"
                    assert (child_namespace.st_dev, child_namespace.st_ino) != (
                        outside_namespace.st_dev, outside_namespace.st_ino), "fixture actor namespace is not isolated"
                terminal = ws.response(turn, timeout=60)
                assert "result" in terminal and terminal["result"].get("ok") is not False, terminal
                assert scripted.closed.wait(10) and scripted.completed
                final = h.provider_wire.wait_request(
                    BYOK[A], offset=h.provider_wire.records.index(scripted) + 1, primary=True)
                assert final.closed.wait(10) and final.completed and final.tool_call is None
                assert _process(host_pid) == child, "owned detached child ended before the census"
                stats = ws.ok("chudcode_session_stats", {"session_id": handles["session_id"]})
                assert stats["toolCalls"] >= 1 and stats["toolResults"] >= 1, stats
                _same_sdk(h, cell, sdk)
                fenced = _operator(cell, "cell_drain", {"admission": "closed", "exit": "none"})
                assert fenced["admission"] == "closed" and not fenced["quiescent"] and not fenced["exiting"], fenced
                _close_socket(h, ws)
                live = _await_census(h, cell, lambda value: value["sessions"] == 0 and value["rpc_inflight"] == 0
                                     and value["workers"]["busy"] == 0 and value["workers"]["background"] > 0
                                     and value["workers"]["unknown"] > 0)
                assert live["native"] == {"cache_verification": 0, "profiler": 0, "dirty_documents": 0}, live
                assert not live["complete"] and live["active"] == sum(live["workers"].values()), live
                assert not live["quiescent"] and not live["exiting"], live
                assert _process(host_pid) == child and cell.proc.poll() is None
                _same_sdk(h, cell, sdk)
                connection.sendall(b"1")
                assert child_stream.readline(16) == b"done\n", "owned child did not finish its actual socket gate"
                assert child_stream.read(1) == b"", "owned child kept its work socket open after completion"

    for _ in range(2):
        quiet = _await_census(h, cell, lambda value: _idle(value, sessions=0))
        assert quiet["quiescent"] and quiet["admission"] == "closed", quiet
        _same_sdk(h, cell, sdk)
    assert _process(host_pid) != child, "completed owned child is still live"
    acknowledged = _operator(cell, "cell_drain", {"admission": "closed", "exit": "when_quiescent", "generation": quiet["generation"]})
    assert acknowledged["complete"] and acknowledged["quiescent"] and acknowledged["active"] == 0, acknowledged
    assert cell.proc.wait(timeout=20) == 0, h.log(cell.principal.cell_id)
