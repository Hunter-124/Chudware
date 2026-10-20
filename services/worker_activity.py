"""Host-only, non-starting census of a worker's live registries and processes."""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
import threading
from contextvars import copy_context
from typing import Any
from contextlib import contextmanager

# Parents publish children before releasing their reservations. Read the parents
# first; never import a registry or call its lazy singleton getter here.
_REGISTRIES = (
    "services.agent.chudcode_host",
    "services.agent.worker_rpc",
    "services.mcp.reveng_common",
    "services.piping.worker_rpc",
    "services.parts.gui_rpc",
    "services.parts.hub",
    "services.mcp.cad_common",
    "services.plm.live_client",
    "services.plm.box_mirror",
    "services.browser.embed",
    "services.browser.queue",
    "services.plm.gitscm",
    "services.reveng.gaussian_jobs",
    "services.sim.jobs",
)
_KIND = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MAX_PROCESSES = 65536
_BACKGROUND_LOCK = threading.Lock()
_BACKGROUND_THREADS: dict[threading.Thread, Any] = {}
_DESKTOP_SDK_OBSERVED = False


@contextmanager
def registry_lock(lock):
    """A census never waits behind a background writer's registry lock."""
    if not lock.acquire(blocking=False):
        raise RuntimeError("registry_busy")
    try:
        yield
    finally:
        lock.release()


def note_sdk_launch(*, sandboxed: bool) -> None:
    global _DESKTOP_SDK_OBSERVED
    if not sandboxed:
        # Observation only, not adoption/reaping: closed desktop sessions may
        # still have escaped tools, so absence of a live session cannot prove zero.
        _DESKTOP_SDK_OBSERVED = True


def start_background_thread(target, *, args=(), name: str) -> threading.Thread:
    """Reserve an existing detached writer before starting it, preserving scope."""

    context = copy_context()
    thread = threading.Thread(target=context.run, args=(target, *args),
                              name=name, daemon=True)
    return start_registered_thread(thread)


def start_registered_thread(thread: threading.Thread) -> threading.Thread:
    """Start an already-context-bound thread after reserving its actual lifetime."""
    from services.agent.request_context import owner_key

    with _BACKGROUND_LOCK:
        for finished in list(_BACKGROUND_THREADS):
            if finished.ident is not None and not finished.is_alive():
                _BACKGROUND_THREADS.pop(finished)
        _BACKGROUND_THREADS[thread] = owner_key()
    try:
        thread.start()
    except BaseException:
        with _BACKGROUND_LOCK:
            _BACKGROUND_THREADS.pop(thread, None)
        raise
    return thread


def _background_count() -> int:
    context = sys.modules.get("services.agent.request_context")
    owner = context.owner_key() if context is not None else None
    with registry_lock(_BACKGROUND_LOCK):
        for thread in list(_BACKGROUND_THREADS):
            if thread.ident is not None and not thread.is_alive():
                _BACKGROUND_THREADS.pop(thread)
        return sum(stored_owner == owner for stored_owner in _BACKGROUND_THREADS.values())


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    start: str
    group: int = 0


def _bounded_bytes(path: Path, maximum: int = 32768) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("oversized_process_metadata")
    return data


def _process(pid: int) -> Process | None:
    if sys.platform == "win32":
        from services.windows_process import process_start
        start = process_start(pid)
        return Process(pid, 0, str(start)) if start is not None else None
    try:
        fields = _bounded_bytes(Path(f"/proc/{pid}/stat")).rsplit(b")", 1)[1].split()
    except FileNotFoundError:
        return None
    if fields[0] in (b"Z", b"X"):
        return None
    return Process(pid, int(fields[1]), fields[19].decode("ascii"), int(fields[2]))


def process_identity(proc: Any) -> tuple[int, str] | None:
    """Capture a Popen's kernel identity, not just its reusable PID."""
    if proc is None or proc.poll() is not None:
        return None
    record = _process(proc.pid)
    return (record.pid, record.start) if record is not None else None


def _process_snapshot() -> dict[int, Process]:
    if sys.platform == "win32":
        from services.windows_process import process_tree
        return {pid: Process(pid, parent, str(start))
                for pid, (parent, start) in process_tree(os.getpid(), _MAX_PROCESSES).items()}
    if sys.platform != "linux":
        raise RuntimeError("process_census_unavailable")
    records: dict[int, Process] = {}
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            if len(records) >= _MAX_PROCESSES:
                raise RuntimeError("process_census_oversized")
            record = _process(int(entry.name))
            if record is not None:
                records[record.pid] = record
    if os.getpid() not in records:
        raise RuntimeError("process_census_incomplete")
    return records


def _argv(pid: int) -> tuple[str, ...]:
    return tuple(os.fsdecode(part) for part in
                 _bounded_bytes(Path(f"/proc/{pid}/cmdline")).split(b"\0") if part)


def _same_executable(pid: int, path: str) -> bool:
    # Names/argv alone are not executable attestation. Namespace payload and
    # launcher binaries are immutable; match the actual mapped executable inode.
    actual = os.stat(f"/proc/{pid}/exe")
    expected = os.stat(path)
    return (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino)


def _descendants(records: dict[int, Process], roots: set[int]) -> set[int]:
    children: dict[int, list[int]] = {}
    for record in records.values():
        children.setdefault(record.parent, []).append(record.pid)
    result: set[int] = set()
    pending = list(roots)
    while pending:
        pid = pending.pop()
        if pid in result or pid not in records:
            continue
        result.add(pid)
        pending.extend(children.get(pid, ()))
    return result


def _launcher_ancestors(records: dict[int, Process]) -> set[int]:
    """Attest only this worker's exact namespace-init chain, not its siblings."""
    own = os.getpid()
    allowed = {own}
    pid = records[own].parent
    init_command: tuple[str, ...] | None = None
    while pid in records:
        command = _argv(pid)
        if init_command is None:
            if (len(command) < 6 or command[:5] != (
                    "/usr/bin/python3", "-I", "-S",
                    "/opt/chudware/bin/chudware-actor-init", "--")
                    or command[5] != "/opt/chudware/bin/chudware-worker"
                    or not _same_executable(pid, "/usr/bin/python3")):
                raise RuntimeError("launcher_unattested")
            init_command = command
        elif (not _same_executable(pid, "/usr/bin/bwrap")
              or len(command) < len(init_command)
              or command[-len(init_command):] != init_command):
            raise RuntimeError("launcher_unattested")
        allowed.add(pid)
        if pid == 1:
            return allowed
        pid = records[pid].parent
    raise RuntimeError("launcher_unattested")


def _sdk_process_chain(client: Any, records: dict[int, Process]) -> tuple[set[int], tuple[int, str]]:
    identity = getattr(client, "_chudware_activity_identity", None)
    command = getattr(client, "_chudware_activity_sdk_argv", ())
    if (not isinstance(identity, tuple) or len(identity) != 2
            or not command or not all(isinstance(arg, str) for arg in command)):
        raise RuntimeError("sdk_process_unattested")
    root = records.get(identity[0])
    if root is None or root.start != identity[1]:
        raise RuntimeError("sdk_process_unattested")
    sandboxed = bool(getattr(client, "_chudware_activity_sandboxed", False))
    if not sandboxed:
        if not _same_executable(root.pid, command[0]):
            raise RuntimeError("sdk_process_unattested")
        return {root.pid}, (root.pid, root.start)

    init = ("/usr/bin/python3", "-I", "-S",
            "/opt/chudware/bin/chudware-actor-init", "--", *command)
    children: dict[int, list[int]] = {}
    for record in records.values():
        children.setdefault(record.parent, []).append(record.pid)

    candidates: list[int] = []

    def chain(pid: int, depth: int, sdk_parent: bool = False) -> set[int]:
        if depth > 8:
            return set()
        if sdk_parent and _same_executable(pid, command[0]):
            # cli.ts changes process.title, so argv is not an identity for chud.
            # Only the exact init's single immutable executable child qualifies.
            # Never walk its descendants, including detached tools.
            candidates.append(pid)
            return {pid}
        argv = _argv(pid)
        is_init = argv == init and _same_executable(pid, "/usr/bin/python3")
        wrapper = is_init or (
            len(argv) >= len(init) and argv[-len(init):] == init
            and _same_executable(pid, "/usr/bin/bwrap"))
        if not wrapper:
            return set()
        found = set()
        for child in children.get(pid, ()):
            found.update(chain(child, depth + 1, sdk_parent=is_init))
        return {pid, *found} if found else set()

    allowed = chain(root.pid, 0)
    if not allowed or len(candidates) != 1:
        raise RuntimeError("sdk_process_unattested")
    sdk = records[candidates[0]]
    return allowed, (sdk.pid, sdk.start)


def capture_sdk_identity(client: Any) -> tuple[int, str]:
    """Capture the owned executable child at ready, before a prompt can spawn work."""
    _allowed, identity = _sdk_process_chain(client, _process_snapshot())
    return identity


def _idle_sdk_processes(client: Any, records: dict[int, Process]) -> set[int]:
    allowed, identity = _sdk_process_chain(client, records)
    if getattr(client, "_chudware_activity_sdk_identity", None) != identity:
        raise RuntimeError("sdk_process_unattested")
    return allowed


def snapshot() -> dict[str, Any]:
    """Return the private protocol envelope; failures preserve every known count."""
    by_kind: dict[str, int] = {}
    failures: set[str] = set()
    processes: list[Any] = []
    idle_clients: list[Any] = []
    for name in _REGISTRIES:
        module = sys.modules.get(name)
        if module is None:
            continue
        try:
            probe = module.worker_activity()
            counts = probe["by_kind"]
            if not isinstance(counts, dict):
                raise ValueError("invalid_registry_probe")
            for kind, count in counts.items():
                if (not isinstance(kind, str) or not _KIND.fullmatch(kind)
                        or type(count) is not int or count < 0):
                    raise ValueError("invalid_registry_probe")
            for kind, count in counts.items():
                if count:
                    by_kind[kind] = by_kind.get(kind, 0) + count
            processes.extend(probe.get("processes", ()))
            idle_clients.extend(probe.get("idle_clients", ()))
            if probe.get("complete", True) is not True:
                failures.add("sdk_unattested" if name.endswith("chudcode_host")
                             else "registry_probe_failed")
        except Exception:
            failures.add("registry_probe_failed")
    try:
        background = _background_count()
        if background:
            by_kind["background_threads"] = background
    except Exception:
        failures.add("registry_probe_failed")

    try:
        records = _process_snapshot()
        context = sys.modules.get("services.agent.request_context")
        in_cell = context is not None and context.in_cell()
        if in_cell:
            context.process_owner_key()
            relevant = set(records)
            try:
                allowed = _launcher_ancestors(records)
            except Exception:
                allowed = {os.getpid()}
                failures.add("launcher_unattested")
        else:
            relevant = set(records) if sys.platform == "win32" else _descendants(records, {os.getpid()})
            allowed = {os.getpid()}
            if _DESKTOP_SDK_OBSERVED or idle_clients:
                # An idle SDK alone is not a job, but its tools could have
                # escaped to host PID1 without attested namespace containment.
                failures.add("process_ownership_unattested")
        for proc in processes:
            identity = process_identity(proc)
            if identity is not None:
                record = records.get(identity[0])
                if record is not None and record.start == identity[1]:
                    allowed.add(record.pid)
        for client in idle_clients:
            try:
                allowed.update(_idle_sdk_processes(client, records))
            except Exception:
                failures.add("sdk_unattested")
        unknown = len(relevant - allowed)
        if unknown:
            by_kind["unclassified_process"] = unknown
            failures.add("unclassified_process")
    except Exception:
        failures.add("process_census_failed")
    result: dict[str, Any] = {"ok": not failures, "active": sum(by_kind.values()),
                              "by_kind": by_kind, "complete": not failures}
    if failures:
        result["reason"] = sorted(failures)[0]
    return result
