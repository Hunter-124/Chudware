"""FIFO queue, admission, idle sleep and persistent workspaces.

All mutable state lives on the event loop thread; the only interleaving points are the
awaits on podman and the broker. Time comes from an injectable ``clock`` so sleep is
testable without sleeping. A session passes through::

    queued -> starting -> active -> ending -> sleeping (workspace kept on disk)

There is no total-time limit: a session that keeps being used runs indefinitely.
After ``idle_seconds`` without activity the container is stopped (the slot and its
RAM/CPU are freed) but the visitor's workspace -- ``<workspace_root>/<sid>/{work,data}``,
named by their signed cookie -- stays on disk, and the same cookie later *resumes* it
in a fresh container. Workspaces are never deleted by a timer; only the visitor (own
cookie) or the admin can delete one. Sleeping likewise happens on explicit end,
shutdown, container exit and quota excess (files always kept).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import heapq
import logging
import os
import shutil
import socket
import statistics
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .broker import Usage
from .config import DemoConfig
from .cookies import SID_RE
from .podman import NAME_PREFIX, ContainerInfo, PodmanError, SessionSpec

log = logging.getLogger("chudware-demo.manager")

QUEUED, STARTING, ACTIVE, ENDING = "queued", "starting", "active", "ending"
SLEEPING = "sleeping"   # no container; the workspace is on disk, resumable by cookie
TOMBSTONE_TTL = 3600.0
TOMBSTONE_MAX = 2000

CapacityProbe = Callable[[], tuple["int | None", "int | None"]]
EndHook = Callable[[str], Awaitable[None]]


class JoinRefused(Exception):
    def __init__(self, code: str, status: int, retry_after: int | None = None):
        super().__init__(code)
        self.code, self.status, self.retry_after = code, status, retry_after


@dataclass
class Entry:
    sid: str
    ip_tag: str
    joined_at: float
    last_poll: float
    state: str = QUEUED
    container: str | None = None
    port: int | None = None
    run_dir: Path | None = None
    activated_at: float | None = None
    last_activity: float | None = None
    end_reason: str | None = None
    end_requested: str | None = None
    usage: Usage = field(default_factory=Usage)
    start_task: asyncio.Task | None = field(default=None, repr=False)
    destroy_task: asyncio.Task | None = field(default=None, repr=False)
    workspace_bytes: int = 0
    quota_checked_at: float = 0.0
    had_workspace: bool = False   # workspace existed before this start attempt


@dataclass
class Dormant:
    """A sleeping workspace: on disk under workspace_root, no container."""
    sid: str
    ip_tag: str = ""
    last_seen: float = 0.0
    reason: str | None = None
    workspace_bytes: int = 0
    usage: Usage = field(default_factory=Usage)


def host_capacity(disk_path: Path) -> tuple[int | None, int | None]:
    """(MemAvailable bytes, free bytes on ``disk_path``'s filesystem); None if unknown."""
    mem = None
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    mem = int(line.split()[1]) * 1024
                    break
    except (OSError, ValueError, IndexError):
        pass
    try:
        disk = shutil.disk_usage(disk_path).free
    except OSError:
        disk = None
    return mem, disk


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


class DemoManager:
    def __init__(self, cfg: DemoConfig, runtime: Any, broker: Any, *,
                 clock: Callable[[], float] = time.time,
                 capacity_probe: CapacityProbe | None = None,
                 port_free: Callable[[int], bool] = _port_free):
        self.cfg, self.runtime, self.broker, self.clock = cfg, runtime, broker, clock
        self._capacity_probe = capacity_probe
        self._port_free = port_free
        self.entries: dict[str, Entry] = {}
        self.queue: list[str] = []
        self.ip_index: dict[str, str] = {}
        self.dormant: dict[str, Dormant] = {}
        self.tombstones: collections.OrderedDict[str, tuple[str, float]] = collections.OrderedDict()
        self.recent: collections.deque[float] = collections.deque(maxlen=20)
        self.capacity_blocked = False
        self.storage_blocked = False
        self._joins: dict[str, collections.deque[float]] = {}
        self._end_hooks: list[EndHook] = []
        self.event_hook: Callable[[str], None] | None = None   # metrics, set by the app
        self._kick = asyncio.Event()
        self._change = asyncio.Event()
        self._loop_task: asyncio.Task | None = None
        self._disk_path: Path = cfg.state_dir

    # -- lifecycle -----------------------------------------------------------------
    def add_end_hook(self, hook: EndHook) -> None:
        self._end_hooks.append(hook)

    def session_dir(self, sid: str) -> Path:
        return self.cfg.session_root / sid

    # -- persistent workspaces ------------------------------------------------
    def workspace_dir(self, sid: str) -> Path:
        return self.cfg.workspace_root / sid

    def ensure_workspace(self, sid: str) -> Path:
        root = self.workspace_dir(sid)
        for sub in ("work", "data", "host"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        # The container runtime requires its mounts owned by the container uid
        # with mode 0700 (podman's :U does the chown at start; the mode is ours).
        # After the first start work/data belong to the mapped subuid, so chmod
        # there is best-effort only -- the mode set at creation already persists.
        for sub in ("work", "data", "host"):
            with contextlib.suppress(OSError):
                (root / sub).chmod(0o700)
        with contextlib.suppress(OSError):
            root.chmod(0o700)
        return root

    async def _measure_workspace(self, sid: str) -> int:
        """Workspace size. On the real host the files are owned by the container's
        mapped subuid at mode 0700, so only the runtime (``podman unshare``) can
        walk them; the plain walk is the fallback (tests, first creation)."""
        fn = getattr(self.runtime, "workspace_usage", None)
        if fn is not None:
            try:
                measured = await fn(self.workspace_dir(sid))
            except Exception:  # noqa: BLE001 - fall back to whatever we can see
                measured = None
            if measured is not None:
                return measured
        return self.workspace_bytes(sid)

    def workspace_bytes(self, sid: str) -> int:
        """Bytes in the visitor-visible mounts (work+data); symlinks not followed."""
        total = 0
        for sub in ("work", "data"):
            for dirpath, _dirs, files in os.walk(self.workspace_dir(sid) / sub):
                for name in files:
                    with contextlib.suppress(OSError):
                        total += os.lstat(os.path.join(dirpath, name)).st_size
        return total

    def stored_bytes(self) -> int:
        known = {sid: d.workspace_bytes for sid, d in self.dormant.items()}
        known.update({sid: e.workspace_bytes for sid, e in self.entries.items()
                      if e.state in (STARTING, ACTIVE, ENDING)})
        return sum(known.values())

    def scan_dormant(self) -> None:
        """Rediscover sleeping workspaces left on disk (restart, upgrade)."""
        root = self.cfg.workspace_root
        if not root.is_dir():
            return
        for child in sorted(root.iterdir()):
            if not child.is_dir() or not SID_RE.fullmatch(child.name):
                continue
            if child.name in self.entries or child.name in self.dormant:
                continue
            self.dormant[child.name] = Dormant(
                sid=child.name, last_seen=child.stat().st_mtime,
                workspace_bytes=self.workspace_bytes(child.name),
                usage=Usage.load(child / "host" / "usage.json"))

    async def delete_workspace(self, sid: str) -> bool:
        """Explicitly erase a sleeping workspace (visitor or admin request only)."""
        if not SID_RE.fullmatch(sid):
            return False
        entry = self.entries.get(sid)
        if entry is not None and entry.state in (STARTING, ACTIVE, ENDING):
            return False
        if entry is not None:            # queued: leave the queue first
            await self._destroy(entry, "workspace_deleted")
        self.dormant.pop(sid, None)
        removed = False
        fn = getattr(self.runtime, "remove_workspace", None)
        if fn is not None:               # files are the container subuid's; unshare rm
            with contextlib.suppress(Exception):
                removed = await fn(self.workspace_dir(sid))
        if not removed:
            await asyncio.to_thread(shutil.rmtree, self.workspace_dir(sid), True)
        self.tombstones[sid] = ("workspace_deleted", self.clock())
        self._changed()
        return True

    async def startup(self, *, run_loop: bool = True) -> None:
        root = self.cfg.session_root
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        storage = await self.runtime.storage_path()
        if storage is not None:
            self._disk_path = storage
        else:
            self._disk_path = root
        await self.adopt()
        self.scan_dormant()
        for dormant in self.dormant.values():
            dormant.workspace_bytes = await self._measure_workspace(dormant.sid)
        keep = set(self.entries)
        for child in root.iterdir():
            if child.name not in keep and child.is_dir():
                log.info("removing stale session directory %s", child.name[:8])
                shutil.rmtree(child, ignore_errors=True)
        if run_loop:
            self._loop_task = asyncio.create_task(self._run_forever(), name="demo-manager")

    async def shutdown(self) -> None:
        """Graceful stop: every session is ended, so no container outlives its reaper."""
        if self._loop_task is not None:
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
            self._loop_task = None
        for entry in list(self.entries.values()):
            if entry.start_task is not None and not entry.start_task.done():
                entry.end_requested = "shutdown"
                entry.start_task.cancel()
        await asyncio.gather(*(self._destroy(e, "shutdown") for e in list(self.entries.values())),
                             return_exceptions=True)
        await self.wait_idle()

    async def wait_idle(self) -> None:
        """Await every in-flight start/destroy task (tests and shutdown)."""
        while True:
            tasks = [t for e in self.entries.values() for t in (e.start_task, e.destroy_task)
                     if t is not None and not t.done()]
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_forever(self) -> None:
        next_reconcile = time.monotonic() + self.cfg.reconcile_seconds
        while True:
            try:
                await self.tick()
                if time.monotonic() >= next_reconcile:
                    next_reconcile = time.monotonic() + self.cfg.reconcile_seconds
                    await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive any single failure
                log.exception("manager tick failed")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._kick.wait(), timeout=self.cfg.tick_seconds)
            self._kick.clear()

    def _changed(self) -> None:
        previous, self._change = self._change, asyncio.Event()
        previous.set()

    # -- visitor-facing API ---------------------------------------------------------
    def _occupied(self) -> int:
        return sum(1 for e in self.entries.values() if e.state in (STARTING, ACTIVE, ENDING))

    def join(self, sid: str, ip_tag: str, raw_ip: str = "") -> Entry:
        now = self.clock()
        existing = self.entries.get(sid)
        if existing is not None:
            if existing.state == ENDING:
                raise JoinRefused("session_ending", 409, 5)
            if existing.state == QUEUED:
                existing.last_poll = now
            return existing
        holder = self.ip_index.get(ip_tag)
        if holder is not None and holder != sid and holder in self.entries:
            raise JoinRefused("ip_limit", 409)
        window = self._joins.setdefault(ip_tag, collections.deque())
        while window and window[0] <= now - self.cfg.join_window:
            window.popleft()
        if len(window) >= self.cfg.join_limit:
            raise JoinRefused("rate_limited", 429, max(1, int(window[0] + self.cfg.join_window - now) + 1))
        if len(self.queue) >= self.cfg.max_queue:
            raise JoinRefused("queue_full", 503, 60)
        window.append(now)
        self.tombstones.pop(sid, None)
        entry = Entry(sid=sid, ip_tag=ip_tag, joined_at=now, last_poll=now,
                      had_workspace=sid in self.dormant or self.workspace_dir(sid).is_dir())
        self.entries[sid] = entry
        self.queue.append(sid)
        self.ip_index[ip_tag] = sid
        log.info("join sid=%s ip=%s queued=%d", sid[:8], raw_ip or ip_tag[:8], len(self.queue))
        self._changed()
        self._kick.set()
        return entry

    def touch(self, sid: str) -> None:
        entry = self.entries.get(sid)
        if entry is not None and entry.state == ACTIVE:
            entry.last_activity = self.clock()

    def admitted(self, sid: str | None) -> Entry | None:
        """The entry for ``sid`` only while its container is up and serving."""
        entry = self.entries.get(sid) if sid else None
        return entry if entry is not None and entry.state == ACTIVE else None

    async def end(self, sid: str, reason: str = "ended") -> bool:
        entry = self.entries.get(sid)
        if entry is None:
            return False
        if entry.state == STARTING and entry.start_task is not None and not entry.start_task.done():
            entry.end_requested = reason
            entry.start_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await entry.start_task
            return True
        await self._destroy(entry, reason)
        return True

    def status(self, sid: str | None) -> dict[str, Any]:
        """The contract's status document; asking while queued counts as polling."""
        now = self.clock()
        out: dict[str, Any] = {
            "state": "none", "position": None, "queue_length": len(self.queue),
            "eta_seconds": None, "idle_expires_at": None, "hard_expires_at": None,
            "warn": False, "note": None,
            "active_sessions": sum(1 for e in self.entries.values() if e.state in (STARTING, ACTIVE)),
            "max_sessions": self.cfg.max_active,
            "free_ai_available": self.broker.free_ai_available(),
            "claude_available": self.broker.claude_available(),
            "has_workspace": False, "workspace_bytes": 0, "last_seen_at": None,
        }
        if sid is None:
            return out
        entry = self.entries.get(sid)
        if entry is None or entry.state == ENDING:
            dormant = self.dormant.get(sid)
            if dormant is not None:
                out["state"] = SLEEPING
                out["note"] = dormant.reason
                out["has_workspace"] = True
                out["workspace_bytes"] = dormant.workspace_bytes
                out["last_seen_at"] = dormant.last_seen
                return out
            tomb = self.tombstones.get(sid)
            out["state"] = "expired"
            out["note"] = (entry.end_reason if entry is not None else tomb[0] if tomb else None)
            return out
        out["state"] = entry.state
        out["has_workspace"] = sid in self.dormant or self.workspace_dir(sid).is_dir()
        out["workspace_bytes"] = entry.workspace_bytes
        if entry.state == QUEUED:
            entry.last_poll = now
            out["position"] = self.queue.index(sid) + 1
            if self.storage_blocked and not out["has_workspace"]:
                out["note"] = "storage"
            elif self.capacity_blocked and self._occupied() < self.cfg.max_active:
                out["note"] = "capacity"
            else:
                out["eta_seconds"] = self._eta(out["position"])
        elif entry.state == ACTIVE:
            # No hard expiry any more; hard_expires_at stays in the document as null.
            idle_end = entry.last_activity + self.cfg.idle_seconds
            out["idle_expires_at"] = idle_end
            out["warn"] = idle_end - now <= self.cfg.warn_seconds
        return out

    async def wait_for_change(self, sid: str, timeout: float) -> None:
        """Hold a status request while queued/starting; the held request keeps the place.

        Returns on a state/position change or after ``timeout`` seconds. A browser may
        throttle a hidden tab's timers to once a minute, but an in-flight request is not
        a timer, so the queue entry cannot time out behind a backgrounded tab.
        """
        entry = self.entries.get(sid)
        if entry is None or entry.state not in (QUEUED, STARTING):
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        seen = (entry.state, self.queue.index(sid) if sid in self.queue else None)
        while True:
            entry.last_poll = self.clock()
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._change.wait(), min(remaining, 5.0))
            current = self.entries.get(sid)
            if current is not entry or (entry.state, self.queue.index(sid) if sid in self.queue else None) != seen:
                return

    # -- estimates -----------------------------------------------------------------
    def _typical_duration(self) -> float:
        if len(self.recent) >= 3:
            typical = statistics.median(self.recent)
        else:
            typical = float(self.cfg.idle_seconds)
        return min(max(typical, 60.0), 4 * 3600.0)

    def _eta(self, position: int) -> int:
        """Rough seconds until the ``position``-th queued visitor is admitted.

        Current occupants free their slot when they go idle; every visitor ahead
        in the queue is assumed to hold a slot for the typical (median recent) session.
        """
        now = self.clock()
        typical = self._typical_duration()
        free_at: list[float] = []
        for entry in self.entries.values():
            if entry.state == ACTIVE:
                end = entry.last_activity + self.cfg.idle_seconds
                free_at.append(max(0.0, end - now))
            elif entry.state == STARTING:
                free_at.append(self.cfg.start_timeout + typical)
            elif entry.state == ENDING:
                free_at.append(5.0)
        free_at += [0.0] * max(0, self.cfg.max_active - len(free_at))
        heapq.heapify(free_at)
        admitted_at = 0.0
        for _ in range(position):
            admitted_at = heapq.heappop(free_at)
            heapq.heappush(free_at, admitted_at + typical)
        return int(admitted_at)

    # -- background work -------------------------------------------------------------
    def _capacity_ok(self) -> bool:
        probe = self._capacity_probe or (lambda: host_capacity(self._disk_path))
        mem, disk = probe()
        starting = sum(1 for e in self.entries.values() if e.state == STARTING)
        ok = (mem is not None and disk is not None
              and mem - starting * self.cfg.start_mem_reserve >= self.cfg.min_mem_bytes
              and disk >= self.cfg.min_disk_bytes)
        if ok == self.capacity_blocked:
            log.warning("host capacity %s (MemAvailable=%s, disk free=%s)",
                        "low, holding the queue" if not ok else "recovered", mem, disk)
        self.capacity_blocked = not ok
        return ok

    async def tick(self) -> None:
        now = self.clock()
        for sid in list(self.queue):
            entry = self.entries[sid]
            if now - entry.last_poll > self.cfg.queue_poll_timeout:
                log.info("queue timeout sid=%s", sid[:8])
                await self._destroy(entry, "queue_timeout")
        due: list[tuple[Entry, str]] = []
        for entry in self.entries.values():
            if entry.state != ACTIVE:
                continue
            if now - entry.last_activity >= self.cfg.idle_seconds:
                due.append((entry, "idle"))
            elif now - entry.quota_checked_at >= self.cfg.quota_check_seconds:
                entry.quota_checked_at = now
                entry.workspace_bytes = await self._measure_workspace(entry.sid)
                if entry.workspace_bytes > self.cfg.workspace_quota_bytes:
                    due.append((entry, "quota_exceeded"))
        if due:
            await asyncio.gather(*(self._destroy(e, why) for e, why in due))
        while self.queue and self._occupied() < self.cfg.max_active:
            if not self._capacity_ok():
                break
            head = self.entries[self.queue[0]]
            # Only brand-new workspaces count against the stored-total cap; a
            # returning visitor's bytes are already on disk and already counted.
            self.storage_blocked = (head.sid not in self.dormant
                                    and not self.workspace_dir(head.sid).is_dir()
                                    and self.stored_bytes() >= self.cfg.workspaces_total_bytes)
            if self.storage_blocked:
                break
            entry = self.entries[self.queue.pop(0)]
            entry.state = STARTING
            entry.container = NAME_PREFIX + entry.sid
            entry.start_task = asyncio.create_task(self._start_entry(entry), name=f"start-{entry.sid[:8]}")
            self._changed()
        if not self.queue:
            self.capacity_blocked = False
            self.storage_blocked = False
        for ip_tag in [k for k, w in self._joins.items() if not w or w[-1] <= now - self.cfg.join_window]:
            del self._joins[ip_tag]
        for sid in [s for s, (_, t) in self.tombstones.items() if now - t > TOMBSTONE_TTL]:
            del self.tombstones[sid]

    def _alloc_port(self) -> int:
        used = {e.port for e in self.entries.values() if e.port is not None}
        for port in range(self.cfg.port_base, self.cfg.port_base + self.cfg.port_count):
            if port not in used and self._port_free(port):
                return port
        raise RuntimeError("no free loopback port for a session container")

    def _spec(self, entry: Entry, started: int) -> SessionSpec:
        return SessionSpec(sid=entry.sid, port=entry.port, run_dir=entry.run_dir,
                           started=started, ip_tag=entry.ip_tag,
                           workspace_dir=self.workspace_dir(entry.sid))

    async def _start_entry(self, entry: Entry) -> None:
        try:
            entry.port = self._alloc_port()
            session_dir = self.session_dir(entry.sid)
            session_dir.mkdir(parents=True, exist_ok=True)
            session_dir.chmod(0o700)
            entry.run_dir = session_dir / "run"
            entry.had_workspace = self.workspace_dir(entry.sid).is_dir()
            workspace = self.ensure_workspace(entry.sid)
            self.dormant.pop(entry.sid, None)
            entry.usage = Usage.load(workspace / "host" / "usage.json")
            entry.workspace_bytes = await self._measure_workspace(entry.sid)
            entry.quota_checked_at = self.clock()
            await self.broker.open_session(entry.sid, entry.run_dir, entry.usage,
                                           workspace / "host" / "usage.json",
                                           entry.ip_tag)
            spec = self._spec(entry, int(self.clock()))
            await self.runtime.start(spec)
            if not await self.runtime.wait_ready(spec, self.cfg.start_timeout):
                raise PodmanError("container did not become healthy in time")
        except asyncio.CancelledError:
            await asyncio.shield(self._destroy(entry, entry.end_requested or "shutdown"))
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("session %s failed to start: %s", entry.sid[:8], exc)
            await self._destroy(entry, "start_failed")
            return
        if entry.end_requested:
            await self._destroy(entry, entry.end_requested)
            return
        now = self.clock()
        entry.activated_at = entry.last_activity = now
        entry.state = ACTIVE
        log.info("session active sid=%s port=%s", entry.sid[:8], entry.port)
        if self.event_hook is not None:
            self.event_hook("session_resumed" if entry.had_workspace else "session_started")
        self._changed()

    async def _destroy(self, entry: Entry, reason: str) -> None:
        if entry.destroy_task is None:
            entry.destroy_task = asyncio.ensure_future(self._destroy_impl(entry, reason))
        await asyncio.shield(entry.destroy_task)

    async def _destroy_impl(self, entry: Entry, reason: str) -> None:
        sid = entry.sid
        entry.state = ENDING
        entry.end_reason = reason
        if sid in self.queue:
            self.queue.remove(sid)
        self._changed()
        log.info("ending session sid=%s reason=%s", sid[:8], reason)
        with contextlib.suppress(Exception):
            await self.broker.close_session(sid)
        for hook in self._end_hooks:
            try:
                await hook(sid)
            except Exception:  # noqa: BLE001
                log.exception("end hook failed")
        if entry.container is not None:
            try:
                await self.runtime.remove(entry.container)
            except Exception as exc:  # noqa: BLE001 - reconcile removes it as a stray
                log.error("could not remove container %s: %s", entry.container, exc)
        await asyncio.to_thread(shutil.rmtree, self.session_dir(sid), True)
        now = self.clock()
        if entry.activated_at is not None:
            self.recent.append(max(0.0, now - entry.activated_at))
        self.entries.pop(sid, None)
        if self.ip_index.get(entry.ip_tag) == sid:
            del self.ip_index[entry.ip_tag]
        # The workspace is NOT removed: the session sleeps and the same cookie
        # resumes it. Only a session that never started (queued, or a start that
        # failed before a workspace existed) leaves just a tombstone.
        if self.workspace_dir(sid).is_dir() and (entry.had_workspace or entry.activated_at is not None):
            entry.workspace_bytes = await self._measure_workspace(sid)
            self.dormant[sid] = Dormant(sid=sid, ip_tag=entry.ip_tag, last_seen=now,
                                        reason=reason, workspace_bytes=entry.workspace_bytes,
                                        usage=entry.usage)
        else:
            if self.workspace_dir(sid).is_dir():   # created by a start that never ran
                await asyncio.to_thread(shutil.rmtree, self.workspace_dir(sid), True)
            self.tombstones[sid] = (reason, now)
        while len(self.tombstones) > TOMBSTONE_MAX:
            self.tombstones.popitem(last=False)
        if self.event_hook is not None:
            self.event_hook(f"slept:{reason}")
        self._changed()
        self._kick.set()

    # -- reconciliation with podman ---------------------------------------------------
    async def reconcile(self) -> None:
        """Mark sessions whose container vanished as ended; remove untracked containers."""
        active_before = {sid for sid, e in self.entries.items() if e.state == ACTIVE}
        try:
            infos = await self.runtime.list_labelled()
        except PodmanError as exc:
            log.error("reconcile: %s", exc)
            return
        running = {i.name for i in infos if i.running}
        for sid in active_before:
            entry = self.entries.get(sid)
            if entry is not None and entry.state == ACTIVE and entry.container not in running:
                log.warning("container for sid=%s is gone", sid[:8])
                await self._destroy(entry, "container_exited")
        tracked = {e.container for e in self.entries.values() if e.container}
        for info in infos:
            if info.name not in tracked:
                log.warning("removing untracked container %s", info.name)
                with contextlib.suppress(PodmanError):
                    await self.runtime.remove(info.name)

    async def adopt(self) -> None:
        """After a restart: re-attach healthy labelled containers, remove the rest.

        Adoption needs the cookie key to be stable (else no visitor could prove ownership
        of an adopted session) and the container's own labels to be complete.
        """
        try:
            infos = await self.runtime.list_labelled()
        except PodmanError as exc:
            log.error("adopt: could not list containers: %s", exc)
            return
        now = self.clock()
        for info in infos:
            reason = self._reject_reason(info, now)
            spec = None
            if reason is None:
                spec = SessionSpec(sid=info.sid, port=info.port, run_dir=self.session_dir(info.sid) / "run",
                                   started=info.started, ip_tag=info.ip_tag)
                if not await self.runtime.healthy(spec):
                    reason = "not healthy"
            if reason is not None:
                log.warning("not adopting %s: %s", info.name, reason)
                with contextlib.suppress(PodmanError):
                    await self.runtime.remove(info.name)
                if SID_RE.fullmatch(info.sid):
                    shutil.rmtree(self.session_dir(info.sid), ignore_errors=True)
                continue
            session_dir = self.session_dir(info.sid)
            entry = Entry(sid=info.sid, ip_tag=info.ip_tag, joined_at=now, last_poll=now,
                          state=ACTIVE, container=info.name, port=info.port,
                          run_dir=spec.run_dir, activated_at=float(info.started), last_activity=now,
                          had_workspace=self.workspace_dir(info.sid).is_dir(),
                          usage=Usage.load(self.workspace_dir(info.sid) / "host" / "usage.json"))
            self.entries[info.sid] = entry
            self.ip_index[info.ip_tag] = info.sid
            try:
                await self.broker.open_session(
                    info.sid, spec.run_dir, entry.usage,
                    self.workspace_dir(info.sid) / "host" / "usage.json", entry.ip_tag)
            except Exception as exc:  # noqa: BLE001
                log.error("adopted %s but its provider socket failed: %s", info.name, exc)
                await self._destroy(entry, "adopt_failed")
                continue
            log.info("adopted %s (started %ds ago)", info.name, int(now - info.started))

    # -- admin snapshot ---------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        """Live sessions + sleeping workspaces, for /admin. Ids are truncated:
        the full sid is a bearer credential (it is the cookie's payload)."""
        now = self.clock()
        sessions = []
        for e in self.entries.values():
            sessions.append({
                "sid": e.sid[:8], "state": e.state, "ip_tag": e.ip_tag[:8],
                "joined_at": e.joined_at, "activated_at": e.activated_at,
                "last_activity": e.last_activity,
                "idle_in_seconds": (max(0, int(e.last_activity + self.cfg.idle_seconds - now))
                                    if e.state == ACTIVE and e.last_activity else None),
                "workspace_bytes": e.workspace_bytes, "port": e.port,
                "ai_requests": e.usage.requests, "ai_tokens": e.usage.tokens,
                "claude_usd": round(e.usage.claude_micros / 1_000_000, 4),
            })
        workspaces = [{
            "sid": d.sid[:8], "full_sid": d.sid, "last_seen": d.last_seen,
            "reason": d.reason, "workspace_bytes": d.workspace_bytes,
            "ai_requests": d.usage.requests, "ai_tokens": d.usage.tokens,
            "claude_usd": round(d.usage.claude_micros / 1_000_000, 4),
        } for d in sorted(self.dormant.values(), key=lambda d: -d.last_seen)]
        return {"sessions": sessions, "workspaces": workspaces,
                "stored_workspaces": len(self.dormant) + sum(
                    1 for e in self.entries.values() if e.had_workspace and e.sid not in self.dormant),
                "stored_bytes": self.stored_bytes(),
                "capacity_blocked": self.capacity_blocked,
                "storage_blocked": self.storage_blocked}

    def _reject_reason(self, info: ContainerInfo, now: float) -> str | None:
        if not self.cfg.cookie_key_persistent:
            return "cookie key is not persistent, so no visitor could reclaim it"
        if not SID_RE.fullmatch(info.sid) or info.name != NAME_PREFIX + info.sid:
            return "unusable labels"
        if info.sid in self.entries:
            return "already tracked"
        if not info.running:
            return f"state is {info.state}"
        if info.port is None or info.started is None or not info.ip_tag:
            return "incomplete labels"
        if info.ip_tag in self.ip_index:
            return "another adopted session already holds this client"
        if self._occupied() >= self.cfg.max_active:
            return "over capacity"
        if not (self.session_dir(info.sid) / "run").is_dir():
            return "its session directory is missing"
        return None
