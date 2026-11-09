"""FIFO queue, admission, expiry and cleanup of demo sessions.

All mutable state lives on the event loop thread; the only interleaving points are the
awaits on podman and the broker. Time comes from an injectable ``clock`` so expiry is
testable without sleeping. A session passes through::

    queued -> starting -> active -> ending -> (gone, remembered as a tombstone)

and is *destroyed* (container removed, host directory deleted, provider socket closed,
proxy connections closed) whenever it leaves ``active``/``starting`` for any reason.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import heapq
import logging
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
        self.tombstones: collections.OrderedDict[str, tuple[str, float]] = collections.OrderedDict()
        self.recent: collections.deque[float] = collections.deque(maxlen=20)
        self.capacity_blocked = False
        self._joins: dict[str, collections.deque[float]] = {}
        self._end_hooks: list[EndHook] = []
        self._kick = asyncio.Event()
        self._change = asyncio.Event()
        self._loop_task: asyncio.Task | None = None
        self._disk_path: Path = cfg.state_dir

    # -- lifecycle -----------------------------------------------------------------
    def add_end_hook(self, hook: EndHook) -> None:
        self._end_hooks.append(hook)

    def session_dir(self, sid: str) -> Path:
        return self.cfg.session_root / sid

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
        entry = Entry(sid=sid, ip_tag=ip_tag, joined_at=now, last_poll=now)
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
            "ai_remaining_today": self.broker.ai_remaining_today(),
            "claude_available": self.broker.claude_available(),
        }
        if sid is None:
            return out
        entry = self.entries.get(sid)
        if entry is None or entry.state == ENDING:
            tomb = self.tombstones.get(sid)
            out["state"] = "expired"
            out["note"] = (entry.end_reason if entry is not None else tomb[0] if tomb else None)
            return out
        out["state"] = entry.state
        if entry.state == QUEUED:
            entry.last_poll = now
            out["position"] = self.queue.index(sid) + 1
            if self.capacity_blocked and self._occupied() < self.cfg.max_active:
                out["note"] = "capacity"
            else:
                out["eta_seconds"] = self._eta(out["position"])
        elif entry.state == ACTIVE:
            hard_end = entry.activated_at + self.cfg.hard_seconds
            idle_end = entry.last_activity + self.cfg.idle_seconds
            out["hard_expires_at"] = hard_end
            out["idle_expires_at"] = idle_end
            out["warn"] = (hard_end - now <= self.cfg.warn_seconds
                           or idle_end - now <= self.cfg.warn_seconds)
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
            typical = self.cfg.hard_seconds / 2
        return min(max(typical, 60.0), float(self.cfg.hard_seconds))

    def _eta(self, position: int) -> int:
        """Rough seconds until the ``position``-th queued visitor is admitted.

        Current occupants free their slot at min(hard, idle) expiry; every visitor ahead
        in the queue is assumed to hold a slot for the typical (median recent) session.
        """
        now = self.clock()
        typical = self._typical_duration()
        free_at: list[float] = []
        for entry in self.entries.values():
            if entry.state == ACTIVE:
                end = min(entry.activated_at + self.cfg.hard_seconds,
                          entry.last_activity + self.cfg.idle_seconds)
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
            if now - entry.activated_at >= self.cfg.hard_seconds:
                due.append((entry, "hard_limit"))
            elif now - entry.last_activity >= self.cfg.idle_seconds:
                due.append((entry, "idle"))
        if due:
            await asyncio.gather(*(self._destroy(e, why) for e, why in due))
        while self.queue and self._occupied() < self.cfg.max_active:
            if not self._capacity_ok():
                break
            entry = self.entries[self.queue.pop(0)]
            entry.state = STARTING
            entry.container = NAME_PREFIX + entry.sid
            entry.start_task = asyncio.create_task(self._start_entry(entry), name=f"start-{entry.sid[:8]}")
            self._changed()
        if not self.queue:
            self.capacity_blocked = False
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
                           started=started, ip_tag=entry.ip_tag)

    async def _start_entry(self, entry: Entry) -> None:
        try:
            entry.port = self._alloc_port()
            session_dir = self.session_dir(entry.sid)
            session_dir.mkdir(parents=True, exist_ok=True)
            session_dir.chmod(0o700)
            entry.run_dir = session_dir / "run"
            await self.broker.open_session(entry.sid, entry.run_dir, entry.usage, session_dir / "usage.json",
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
        self.tombstones[sid] = (reason, now)
        while len(self.tombstones) > TOMBSTONE_MAX:
            self.tombstones.popitem(last=False)
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
                          usage=Usage.load(session_dir / "usage.json"))
            self.entries[info.sid] = entry
            self.ip_index[info.ip_tag] = info.sid
            try:
                await self.broker.open_session(info.sid, spec.run_dir, entry.usage, session_dir / "usage.json",
                                               entry.ip_tag)
            except Exception as exc:  # noqa: BLE001
                log.error("adopted %s but its provider socket failed: %s", info.name, exc)
                await self._destroy(entry, "adopt_failed")
                continue
            log.info("adopted %s (started %ds ago)", info.name, int(now - info.started))

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
        if now - info.started >= self.cfg.hard_seconds:
            return "past its hard lifetime"
        if not (self.session_dir(info.sid) / "run").is_dir():
            return "its session directory is missing"
        return None
