"""Async per-user session manager (podman containers)."""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

try:
    from .auth import User
    from .config import Settings, settings
except ImportError:
    from auth import User
    from config import Settings, settings

log = logging.getLogger("billet-web.sessions")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def email_slug(email: str) -> str:
    local = email.strip().lower().split("@", 1)[0]
    slug = _SLUG_RE.sub("-", local).strip("-")
    return (slug or "user")[:48]


def mem_available_bytes() -> int | None:
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    return int(parts[1]) * 1024
    except OSError:
        return None
    return None


def mem_available_gib() -> float | None:
    b = mem_available_bytes()
    if b is None:
        return None
    return b / (1024**3)


@dataclass
class Session:
    sid: str
    email: str
    display: str
    slug: str
    container: str
    host_port: int
    heavy: bool
    status: str = "starting"  # starting | ready | stopping | stopped | error
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    container_id: str | None = None

    def touch(self) -> None:
        self.last_activity = time.time()

    def portal_status(self) -> str:
        if self.status == "ready":
            return "running"
        if self.status in {"starting", "stopping", "stopped", "error"}:
            return self.status
        return self.status

    def to_dict(self) -> dict[str, Any]:
        return {
            "sid": self.sid,
            "email": self.email,
            "display": self.display,
            "slug": self.slug,
            "container": self.container,
            "host_port": self.host_port,
            "heavy": self.heavy,
            "status": self.portal_status(),
            "error": self.error,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "stream_path": f"/stream/{self.sid}/",
            "stream_url": f"/stream/{self.sid}/",
        }


class PodmanError(RuntimeError):
    pass


class SessionBusy(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SessionManager:
    """Owns live sessions; one container per user (reconnect reuses)."""

    def __init__(self, cfg: Settings | None = None) -> None:
        self.cfg = cfg or settings
        self._sessions: dict[str, Session] = {}
        self._by_email: dict[str, str] = {}  # email → sid
        self._ports_in_use: set[int] = set()
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task | None = None
        self.podman_bin = "podman"

    async def start_reaper(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop(), name="session-reaper")

    async def stop_reaper(self) -> None:
        if self._reaper_task and not self._reaper_task.done():
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except asyncio.CancelledError:
                pass
        self._reaper_task = None

    async def _reaper_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
                await self.reap_idle()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("reaper tick failed")

    async def reap_idle(self) -> list[str]:
        now = time.time()
        timeout = self.cfg.idle_timeout_sec
        to_stop: list[str] = []
        async with self._lock:
            for sid, sess in list(self._sessions.items()):
                if sess.status in {"stopped", "stopping", "error"}:
                    continue
                if (now - sess.last_activity) > timeout:
                    to_stop.append(sid)
        stopped: list[str] = []
        for sid in to_stop:
            log.info("idle reaper stopping session %s", sid)
            try:
                await self.stop(sid)
                stopped.append(sid)
            except Exception:  # noqa: BLE001
                log.exception("failed to reap session %s", sid)
        return stopped

    def list_sessions(self) -> list[Session]:
        return list(self._sessions.values())

    def get(self, sid: str) -> Session | None:
        return self._sessions.get(sid)

    def get_for_user(self, email: str) -> Session | None:
        sid = self._by_email.get(email.strip().lower())
        if not sid:
            return None
        return self._sessions.get(sid)

    def active_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.status in {"starting", "ready"})

    def heavy_count(self) -> int:
        return sum(
            1 for s in self._sessions.values() if s.heavy and s.status in {"starting", "ready"}
        )

    def touch(self, sid: str) -> None:
        sess = self._sessions.get(sid)
        if sess:
            sess.touch()

    def portal_payload(self, sess: Session | None, *, message: str | None = None) -> dict[str, Any]:
        if sess is None:
            return {
                "status": "none",
                "sid": None,
                "stream_path": None,
                "stream_url": None,
                "heavy": False,
                "error": None,
                "message": message,
            }
        d = sess.to_dict()
        return {
            "status": d["status"],
            "sid": d["sid"],
            "stream_path": d["stream_path"],
            "stream_url": d["stream_url"],
            "heavy": d["heavy"],
            "error": d["error"],
            "message": message or d["error"],
            "host_port": d["host_port"],
            "container": d["container"],
        }

    async def _container_running(self, name: str) -> bool:
        rc, out, _ = await self.run_podman(
            ["ps", "--filter", f"name=^{name}$", "--filter", "status=running",
             "--format", "{{.ID}}"],
            timeout=15,
        )
        return rc == 0 and bool(out.strip())

    async def _drop_session(self, sid: str) -> None:
        async with self._lock:
            sess = self._sessions.pop(sid, None)
            if sess:
                self._ports_in_use.discard(sess.host_port)
                if self._by_email.get(sess.email) == sid:
                    del self._by_email[sess.email]

    async def start(self, user: User) -> Session:
        email = user.email.strip().lower()
        existing = self.get_for_user(email)
        if existing and existing.status in {"starting", "ready"}:
            # A record can outlive its container (killed out-of-band, host
            # reboot); verify before handing the user a dead seat.
            if existing.status == "starting" or await self._container_running(existing.container):
                existing.touch()
                log.info("reconnect user %s → session %s", email, existing.sid)
                return existing
            log.warning("session %s container %s is gone; starting fresh",
                        existing.sid, existing.container)
            await self._drop_session(existing.sid)
        async with self._lock:
            # re-check under the lock: a concurrent start may have won
            existing = self.get_for_user(email)
            if existing and existing.status in {"starting", "ready"}:
                existing.touch()
                return existing

            if self.cfg.refuse_new_flag.exists():
                raise SessionBusy("host refusing new sessions (REFUSE_NEW flag)")

            if self.active_count() >= self.cfg.max_sessions:
                raise SessionBusy(f"max sessions reached ({self.cfg.max_sessions})")

            avail = mem_available_gib()
            if avail is not None and avail < self.cfg.min_host_ram_gib:
                raise SessionBusy(
                    f"insufficient host RAM ({avail:.1f} GiB available, "
                    f"need ≥{self.cfg.min_host_ram_gib})"
                )

            want_heavy = bool(user.heavy) and self.heavy_count() < self.cfg.max_heavy
            slug = email_slug(email)
            host_port = self._alloc_port()
            sid = uuid.uuid4().hex[:12]
            container = f"billet-sess-{slug}"
            home = self.cfg.homes_dir / slug
            home.mkdir(parents=True, exist_ok=True)
            self.cfg.plm_dir.mkdir(parents=True, exist_ok=True)

            sess = Session(
                sid=sid,
                email=email,
                display=user.display,
                slug=slug,
                container=container,
                host_port=host_port,
                heavy=want_heavy,
                status="starting",
            )
            self._sessions[sid] = sess
            self._by_email[email] = sid
            self._ports_in_use.add(host_port)

        try:
            await self._ensure_no_stale_container(container)
            cid = await self._podman_run(sess, user, home)
            sess.container_id = cid
            ready = await self._wait_ready(sess)
            if ready:
                sess.status = "ready"
            else:
                sess.status = "error"
                sess.error = "stream port did not become healthy in time"
                log.error("session %s healthcheck failed; cleaning up", sid)
                await self._podman_stop(sess.container)
                async with self._lock:
                    self._ports_in_use.discard(host_port)
                    if self._by_email.get(email) == sid:
                        del self._by_email[email]
                    self._sessions.pop(sid, None)
        except Exception as exc:  # noqa: BLE001
            sess.status = "error"
            sess.error = str(exc) or type(exc).__name__
            log.exception("session start failed for %s", email)
            await self._podman_stop(sess.container)
            async with self._lock:
                self._ports_in_use.discard(host_port)
                if self._by_email.get(email) == sid:
                    del self._by_email[email]
                self._sessions.pop(sid, None)
            raise
        return sess

    async def stop(self, sid: str, *, email: str | None = None) -> Session | None:
        async with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                return None
            if email and sess.email != email.strip().lower():
                raise PermissionError("not your session")
            sess.status = "stopping"
            container = sess.container
            host_port = sess.host_port
            owner = sess.email

        try:
            await self._podman_stop(container)
        finally:
            async with self._lock:
                sess = self._sessions.get(sid)
                if sess:
                    sess.status = "stopped"
                self._ports_in_use.discard(host_port)
                if self._by_email.get(owner) == sid:
                    del self._by_email[owner]
                self._sessions.pop(sid, None)
        return sess

    async def stop_for_user(self, email: str) -> Session | None:
        sess = self.get_for_user(email)
        if not sess:
            return None
        return await self.stop(sess.sid, email=email)

    async def run_podman(self, args: list[str], *, timeout: float = 120.0) -> tuple[int, str, str]:
        """Execute podman; returns (rc, stdout, stderr). Monkeypatch this in tests.

        Output goes to temp FILES, not pipes: `podman run -d` hands its stdio fds
        to conmon, which keeps them open for the container's whole lifetime, so a
        pipe-based communicate() never sees EOF and hangs until timeout.
        """
        with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
            proc = await asyncio.create_subprocess_exec(
                self.podman_bin,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=out_f,
                stderr=err_f,
            )
            try:
                rc = await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
                raise PodmanError(f"podman timed out: {' '.join(args[:4])}") from exc
            out_f.seek(0)
            err_f.seek(0)
            out = out_f.read().decode("utf-8", errors="replace")
            err = err_f.read().decode("utf-8", errors="replace")
        return rc, out, err

    async def _ensure_no_stale_container(self, name: str) -> None:
        rc, out, _ = await self.run_podman(
            ["ps", "-a", "--filter", f"name=^{name}$", "--format", "{{.ID}}"],
            timeout=30,
        )
        if rc == 0 and out.strip():
            log.info("removing stale container %s", name)
            await self.run_podman(["rm", "-f", name], timeout=60)

    async def adopt_running(self) -> int:
        """Rebuild session records from live billet-sess-* containers.

        Session state is in-memory; after a gateway restart (systemd
        Restart=on-failure) running seats would otherwise be orphaned —
        unreachable, unreaped, and double-started on the next login.
        """
        rc, out, _ = await self.run_podman(
            [
                "ps",
                "--filter", "name=^billet-sess-",
                "--filter", "status=running",
                "--format",
                '{{.ID}}\t{{index .Labels "billet.sid"}}\t{{index .Labels "billet.email"}}\t'
                '{{index .Labels "billet.display"}}\t{{index .Labels "billet.slug"}}\t'
                '{{index .Labels "billet.port"}}\t{{index .Labels "billet.heavy"}}\t{{.Names}}',
            ],
            timeout=30,
        )
        if rc != 0:
            log.warning("adopt_running: podman ps failed rc=%s", rc)
            return 0
        adopted = 0
        async with self._lock:
            for line in out.strip().splitlines():
                parts = line.split("\t")
                if len(parts) != 8:
                    continue
                cid, sid, email, display, slug, port_s, heavy_s, name = parts
                if not sid or not email or sid in self._sessions:
                    continue
                try:
                    port = int(port_s)
                except ValueError:
                    continue
                self._sessions[sid] = Session(
                    sid=sid,
                    email=email,
                    display=display or email,
                    slug=slug or email_slug(email),
                    container=name,
                    host_port=port,
                    heavy=heavy_s == "1",
                    status="ready",
                    container_id=cid,
                )
                self._by_email[email] = sid
                self._ports_in_use.add(port)
                adopted += 1
        if adopted:
            log.info("adopted %d running session container(s)", adopted)
        return adopted

    async def _podman_run(self, sess: Session, user: User, home: Path) -> str:
        encoder = "nvh264enc" if sess.heavy else "x264enc"
        env = {
            "BILLET_USER_EMAIL": user.email,
            "BILLET_USER_DISPLAY": user.display,
            "AICAD_USER_ID": user.email,
            "AICAD_USER_DISPLAY": user.display,
            "STREAM_BACKEND": self.cfg.stream_backend,
            "SELKIES_ENCODER": encoder,
            "BILLET_HEAVY": "1" if sess.heavy else "0",
            "DISPLAY": ":0",
            "BILLET_PLM_DIR": "/plm",
        }
        args: list[str] = [
            "run",
            "-d",
            "--name",
            sess.container,
            "--replace",
            "--memory",
            self.cfg.session_memory,
            "--memory-swap",
            self.cfg.session_memory,
            "--cpus",
            str(self.cfg.session_cpus),
            "--pids-limit",
            str(self.cfg.session_pids_limit),
            "--label",
            f"billet.sid={sess.sid}",
            "--label",
            f"billet.email={sess.email}",
            "--label",
            f"billet.display={sess.display}",
            "--label",
            f"billet.slug={sess.slug}",
            "--label",
            f"billet.port={sess.host_port}",
            "--label",
            f"billet.heavy={'1' if sess.heavy else '0'}",
            "-p",
            f"127.0.0.1:{sess.host_port}:8080",
            "-v",
            f"{home}:/home/user:U",
            "-v",
            f"{self.cfg.plm_dir}:/plm:U",
        ]
        for k, v in env.items():
            args.extend(["-e", f"{k}={v}"])
        if sess.heavy:
            args.extend(
                [
                    "--device",
                    "nvidia.com/gpu=all",
                    "--security-opt",
                    "label=disable",
                    "-e",
                    "NVIDIA_DRIVER_CAPABILITIES=all",
                ]
            )
        args.append(self.cfg.session_image)

        rc, out, err = await self.run_podman(args, timeout=180)
        if rc != 0:
            raise PodmanError(f"podman run failed (rc={rc}): {err or out}")
        cid = out.strip().splitlines()[-1].strip() if out.strip() else ""
        log.info(
            "started container %s (%s) port=%s heavy=%s",
            sess.container,
            cid[:12] if cid else "?",
            sess.host_port,
            sess.heavy,
        )
        return cid

    async def _podman_stop(self, name: str) -> None:
        rc, out, err = await self.run_podman(["stop", "-t", "15", name], timeout=60)
        if rc != 0:
            log.warning("podman stop %s rc=%s err=%s", name, rc, err or out)
        await self.run_podman(["rm", "-f", name], timeout=30)

    async def _wait_ready(self, sess: Session) -> bool:
        url = f"http://127.0.0.1:{sess.host_port}/"
        deadline = time.time() + self.cfg.healthcheck_timeout_sec
        interval = self.cfg.healthcheck_interval_sec
        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.time() < deadline:
                try:
                    resp = await client.get(url)
                    if resp.status_code < 500:
                        sess.touch()
                        return True
                except (httpx.HTTPError, OSError):
                    pass
                await asyncio.sleep(interval)
        return False

    def _alloc_port(self) -> int:
        base = self.cfg.stream_port_base
        for n in range(0, 64):
            port = base + n
            if port not in self._ports_in_use:
                return port
        raise SessionBusy("no free stream ports")


manager = SessionManager()
