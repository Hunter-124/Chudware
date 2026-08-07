"""Async per-user session manager (podman containers)."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

try:
    from .auth import User
    from .config import Settings, settings
    from .limits import session_starts, user_slug
except ImportError:
    from auth import User
    from config import Settings, settings
    from limits import session_starts, user_slug

log = logging.getLogger("chudware-web.sessions")


# AUDIT (CRITICAL, multi-tenant): _podman_run historically copied the HOST's
# paid provider keys (ai_providers.env -> Firecrawl/Gemini/Moonshot/OpenRouter)
# into every session container, where the seat owner reads them straight out of
# the container environment (`podman exec env`). Sharing is now an explicit
# operator opt-in, default OFF. config.py is owned by another workstream, so
# the flag is read here with the same _env_bool pattern used there.
_SHARE_PROVIDER_KEYS = os.environ.get(
    "CHUDWARE_WEB_SHARE_PROVIDER_KEYS", ""
).strip().lower() in {"1", "true", "yes", "on"}
if not _SHARE_PROVIDER_KEYS:
    log.warning(
        "AI provider keys withheld from session containers "
        "(set CHUDWARE_WEB_SHARE_PROVIDER_KEYS=1 to share host keys into seats)"
    )


def _new_sid() -> str:
    """Session ids are bearer handles in /stream/<sid>/ URLs; use full entropy."""
    return secrets.token_urlsafe(24)



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
    generation: int = 0

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
        # The global lock protects capacity reservations and indexes only.  A
        # per-account lock spans Podman I/O so lifecycle mutations for one seat
        # serialize without blocking unrelated accounts or host capacity reads.
        self._account_locks: dict[str, asyncio.Lock] = {}
        self._generations: dict[str, int] = {}
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
        idle_cutoff = now - self.cfg.idle_timeout_sec
        lifetime_cutoff = now - self.cfg.max_session_lifetime_sec
        to_stop: list[tuple[str, str, int]] = []
        async with self._lock:
            for sid, sess in list(self._sessions.items()):
                if sess.status not in {"starting", "ready"}:
                    continue
                # Capture the incarnation, not just the sid.  The stop claim
                # rechecks both cutoffs after it acquires the account lock, so
                # activity arriving between this scan and the claim wins.
                if (
                    sess.created_at < lifetime_cutoff
                    or sess.last_activity < idle_cutoff
                ):
                    to_stop.append((sid, sess.email, sess.generation))
        stopped: list[str] = []
        for sid, email, generation in to_stop:
            log.info("idle reaper considering session %s", sid)
            try:
                sess = await self._stop_serialized(
                    sid,
                    owner=email,
                    generation=generation,
                    idle_cutoff=idle_cutoff,
                    lifetime_cutoff=lifetime_cutoff,
                )
                if sess is not None:
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

    def portal_payload(
        self,
        sess: Session | None,
        *,
        message: str | None = None,
        include_internals: bool = False,
    ) -> dict[str, Any]:
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
        payload = {
            "status": d["status"],
            "sid": d["sid"],
            "stream_path": d["stream_path"],
            "stream_url": d["stream_url"],
            "heavy": d["heavy"],
            "error": d["error"],
            "message": message or d["error"],
        }
        # host_port / container are routing internals: useful on the admin
        # dashboard, but they tell a seat owner exactly how to reach the
        # loopback-published port of every other seat.
        if include_internals:
            payload["host_port"] = d["host_port"]
            payload["container"] = d["container"]
        return payload

    async def _container_running(self, name: str) -> bool:
        rc, out, _ = await self.run_podman(
            ["ps", "--filter", f"name=^{re.escape(name)}$", "--filter", "status=running",
             "--format", "{{.ID}}"],
            timeout=15,
        )
        return rc == 0 and bool(out.strip())

    def _account_lock(self, email: str) -> asyncio.Lock:
        """Return the lifecycle mutex for a normalized account identity."""
        lock = self._account_locks.get(email)
        if lock is None:
            lock = asyncio.Lock()
            self._account_locks[email] = lock
        return lock

    def _next_generation_locked(self, email: str) -> int:
        """Mint an account lifecycle generation while ``self._lock`` is held."""
        generation = self._generations.get(email, 0) + 1
        self._generations[email] = generation
        return generation

    def _is_current_locked(self, sess: Session, generation: int) -> bool:
        return (
            self._sessions.get(sess.sid) is sess
            and sess.generation == generation
            and self._by_email.get(sess.email) == sess.sid
        )

    async def _discard_if_current(self, sess: Session, generation: int) -> bool:
        """Release a reservation only when its exact incarnation still owns it."""
        async with self._lock:
            if not self._is_current_locked(sess, generation):
                return False
            self._sessions.pop(sess.sid, None)
            self._by_email.pop(sess.email, None)
            self._ports_in_use.discard(sess.host_port)
            return True

    async def start(self, user: User) -> Session:
        email = user.email.strip().lower()
        async with self._account_lock(email):
            existing = self.get_for_user(email)
            if existing is not None:
                # Both adoption and an in-memory reconnect must pass the HTTP
                # health probe.  Merely seeing a running container is not
                # enough to publish a dead stream as ready.
                healthy = False
                if existing.status in {"starting", "ready"}:
                    async with self._lock:
                        probe_claim = self._is_current_locked(
                            existing,
                            existing.generation,
                        )
                        if probe_claim:
                            existing.status = "starting"
                            existing.error = None
                    if (
                        probe_claim
                        and await self._container_running(existing.container)
                    ):
                        healthy = await self._wait_ready(existing)
                if healthy:
                    async with self._lock:
                        if self._is_current_locked(existing, existing.generation):
                            existing.status = "ready"
                            existing.error = None
                            existing.touch()
                            log.info(
                                "reconnect user %s → session %s",
                                email,
                                existing.sid,
                            )
                            return existing
                log.warning(
                    "session %s container %s is stale or unhealthy; starting fresh",
                    existing.sid,
                    existing.container,
                )
                await self._stop_locked(
                    existing.sid,
                    owner=email,
                    generation=existing.generation,
                )

            # The canonical full-address slug separates equal local parts at
            # different domains.  Legacy containers are adopted by their
            # chudware.email label and retain their old name until stopped.
            slug = user_slug(email)
            container = f"chudware-sess-{slug}"
            home = self.cfg.homes_dir / slug
            home.mkdir(parents=True, exist_ok=True)
            self.cfg.plm_dir.mkdir(parents=True, exist_ok=True)

            async with self._lock:
                if self.cfg.refuse_new_flag.exists():
                    raise SessionBusy("host refusing new sessions (REFUSE_NEW flag)")

                if self.active_count() >= self.cfg.max_sessions:
                    raise SessionBusy(f"max sessions reached ({self.cfg.max_sessions})")

                # Per-account seat cap: the global cap above is host capacity;
                # this also catches an old orphan not present in _by_email.
                user_active = sum(
                    1
                    for s in self._sessions.values()
                    if s.email == email and s.status in {"starting", "ready"}
                )
                if user_active >= self.cfg.max_sessions_per_user:
                    raise SessionBusy(
                        "per-user session limit reached "
                        f"({self.cfg.max_sessions_per_user})"
                    )

                avail = mem_available_gib()
                if avail is not None and avail < self.cfg.min_host_ram_gib:
                    raise SessionBusy(
                        f"insufficient host RAM ({avail:.1f} GiB available, "
                        f"need ≥{self.cfg.min_host_ram_gib})"
                    )

                # Rate-limit start/stop churn — every fresh seat costs podman
                # plus a healthcheck cycle.  The reservation below is atomic
                # with all global capacity checks; Podman I/O happens after.
                session_starts.enforce(email, "session starts")
                want_heavy = (
                    bool(user.heavy) and self.heavy_count() < self.cfg.max_heavy
                )
                host_port = self._alloc_port()
                sid = _new_sid()
                generation = self._next_generation_locked(email)
                sess = Session(
                    sid=sid,
                    email=email,
                    display=user.display,
                    slug=slug,
                    container=container,
                    host_port=host_port,
                    heavy=want_heavy,
                    status="starting",
                    generation=generation,
                )
                self._sessions[sid] = sess
                self._by_email[email] = sid
                self._ports_in_use.add(host_port)

            try:
                await self._ensure_no_stale_container(container)
                sess.container_id = await self._podman_run(sess, user, home)
                if await self._wait_ready(sess):
                    async with self._lock:
                        if not self._is_current_locked(sess, generation):
                            raise SessionBusy("session lifecycle changed during start")
                        sess.status = "ready"
                        sess.error = None
                    return sess

                async with self._lock:
                    owns_claim = self._is_current_locked(sess, generation)
                    if owns_claim:
                        sess.status = "error"
                        sess.error = "stream port did not become healthy in time"
                if not owns_claim:
                    raise SessionBusy("session lifecycle changed during healthcheck")
                log.error("session %s healthcheck failed; cleaning up", sid)
                try:
                    await self._podman_stop(container)
                finally:
                    await self._discard_if_current(sess, generation)
                return sess
            except Exception as exc:  # noqa: BLE001
                async with self._lock:
                    owns_claim = self._is_current_locked(sess, generation)
                    if owns_claim:
                        sess.status = "error"
                        sess.error = str(exc) or type(exc).__name__
                log.exception("session start failed for %s", email)
                if owns_claim:
                    try:
                        await self._podman_stop(container)
                    finally:
                        await self._discard_if_current(sess, generation)
                raise

    async def _stop_locked(
        self,
        sid: str,
        *,
        owner: str,
        generation: int,
        idle_cutoff: float | None = None,
        lifetime_cutoff: float | None = None,
    ) -> Session | None:
        """Claim and stop one exact session incarnation.

        The caller holds the account lock.  Cutoffs make this an idle-reaper
        claim and are revalidated atomically with the status transition.
        """
        async with self._lock:
            sess = self._sessions.get(sid)
            if (
                sess is None
                or sess.email != owner
                or sess.generation != generation
            ):
                return None
            if idle_cutoff is not None and lifetime_cutoff is not None:
                if sess.status not in {"starting", "ready"}:
                    return None
                if (
                    sess.created_at >= lifetime_cutoff
                    and sess.last_activity >= idle_cutoff
                ):
                    return None
            sess.status = "stopping"
            container = sess.container
            host_port = sess.host_port

        try:
            await self._podman_stop(container)
        finally:
            async with self._lock:
                current = self._sessions.get(sid)
                if current is sess and current.generation == generation:
                    current.status = "stopped"
                    self._sessions.pop(sid, None)
                    self._ports_in_use.discard(host_port)
                    if self._by_email.get(owner) == sid:
                        self._by_email.pop(owner, None)
        return sess

    async def _stop_serialized(
        self,
        sid: str,
        *,
        owner: str,
        generation: int,
        idle_cutoff: float | None = None,
        lifetime_cutoff: float | None = None,
    ) -> Session | None:
        async with self._account_lock(owner):
            return await self._stop_locked(
                sid,
                owner=owner,
                generation=generation,
                idle_cutoff=idle_cutoff,
                lifetime_cutoff=lifetime_cutoff,
            )

    async def stop(self, sid: str, *, email: str | None = None) -> Session | None:
        async with self._lock:
            sess = self._sessions.get(sid)
            if sess is None:
                return None
            if email and sess.email != email.strip().lower():
                raise PermissionError("not your session")
            owner = sess.email
            generation = sess.generation
        return await self._stop_serialized(
            sid,
            owner=owner,
            generation=generation,
        )

    async def stop_for_user(self, email: str) -> Session | None:
        normalized = email.strip().lower()
        sess = self.get_for_user(normalized)
        if sess is None:
            return None
        return await self.stop(sess.sid, email=normalized)

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
            ["ps", "-a", "--filter", f"name=^{re.escape(name)}$", "--format", "{{.ID}}"],
            timeout=30,
        )
        if rc == 0 and out.strip():
            log.info("removing stale container %s", name)
            await self.run_podman(["rm", "-f", name], timeout=60)

    async def adopt_running(self) -> int:
        """Rebuild healthy session records from labeled running containers.

        The email label is authoritative, which safely adopts legacy local-part
        container names without treating that collision-prone name as identity.
        Every candidate remains ``starting`` until its stream health probe
        succeeds.
        """
        rc, out, _ = await self.run_podman(
            [
                "ps",
                "--filter",
                "name=^chudware-sess-",
                "--filter",
                "status=running",
                "--format",
                '{{.ID}}\t{{index .Labels "chudware.sid"}}\t{{index .Labels "chudware.email"}}\t'
                '{{index .Labels "chudware.display"}}\t{{index .Labels "chudware.slug"}}\t'
                '{{index .Labels "chudware.port"}}\t{{index .Labels "chudware.heavy"}}\t{{.Names}}',
            ],
            timeout=30,
        )
        if rc != 0:
            log.warning("adopt_running: podman ps failed rc=%s", rc)
            return 0

        adopted = 0
        for line in out.strip().splitlines():
            parts = line.split("\t")
            if len(parts) != 8:
                continue
            cid, sid, raw_email, display, _legacy_slug, port_s, heavy_s, name = parts
            email = raw_email.strip().lower()
            if not sid or not email or email in {".", ".."} or not name:
                continue
            try:
                port = int(port_s)
                slug = user_slug(email)
            except (TypeError, ValueError):
                continue
            if not (1 <= port <= 65535):
                continue

            async with self._account_lock(email):
                reject_reason: str | None = None
                may_stop_rejected = True
                candidate: Session | None = None
                async with self._lock:
                    same_sid = self._sessions.get(sid)
                    same_email = self.get_for_user(email)
                    same_container = next(
                        (
                            sess
                            for sess in self._sessions.values()
                            if sess.container == name
                        ),
                        None,
                    )
                    if same_sid is not None:
                        reject_reason = f"sid {sid} is already tracked"
                        may_stop_rejected = same_sid.container != name
                    elif same_email is not None:
                        reject_reason = f"account {email} already has a session"
                        may_stop_rejected = same_email.container != name
                    elif same_container is not None:
                        reject_reason = f"container {name} is already tracked"
                        may_stop_rejected = False
                    elif port in self._ports_in_use:
                        reject_reason = f"stream port {port} is already reserved"
                    else:
                        generation = self._next_generation_locked(email)
                        candidate = Session(
                            sid=sid,
                            email=email,
                            display=display or email,
                            slug=slug,
                            # Keep a legacy name for this incarnation.  A later
                            # fresh start uses the canonical full-email name.
                            container=name,
                            host_port=port,
                            heavy=heavy_s == "1",
                            status="starting",
                            container_id=cid,
                            generation=generation,
                        )
                        self._sessions[sid] = candidate
                        self._by_email[email] = sid
                        self._ports_in_use.add(port)

                if reject_reason is not None:
                    log.warning("rejecting adopted container %s: %s", name, reject_reason)
                    if may_stop_rejected:
                        await self._podman_stop(name)
                    continue
                if candidate is None:
                    continue

                if await self._wait_ready(candidate):
                    async with self._lock:
                        if self._is_current_locked(candidate, generation):
                            candidate.status = "ready"
                            candidate.error = None
                            adopted += 1
                            continue

                log.warning("adopted container %s failed its health probe", name)
                await self._stop_locked(
                    sid,
                    owner=email,
                    generation=generation,
                )

        if adopted:
            log.info("adopted %d healthy running session container(s)", adopted)
        return adopted

    async def _podman_run(self, sess: Session, user: User, home: Path) -> str:
        encoder = "nvh264enc" if sess.heavy else "x264enc"
        env = {
            "CHUDWARE_USER_EMAIL": user.email,
            "CHUDWARE_USER_DISPLAY": user.display,
            "CHUDWARE_USER_ID": user.email,
            "CHUDWARE_USER_DISPLAY": user.display,
            "STREAM_BACKEND": self.cfg.stream_backend,
            "SELKIES_ENCODER": encoder,
            "CHUDWARE_HEAVY": "1" if sess.heavy else "0",
            "DISPLAY": ":0",
            "CHUDWARE_PLM_DIR": "/plm",
        }
        shared_project_mount: str | None = None
        if user.project:
            if user.project.startswith("/"):
                # Absolute HOST folder — bind-mounted read-only at
                # ~/Projects/<basename> inside the session (shared :z label; the
                # container user can't own host files, and demo users must not be
                # able to trash the real working tree).
                proj = Path(user.project)
                if proj.is_dir():
                    shared_project_mount = str(proj)
                    env["CHUDWARE_DEFAULT_PROJECT"] = proj.name
                else:
                    log.warning("default project dir missing on host: %s", proj)
            else:
                env["CHUDWARE_DEFAULT_PROJECT"] = user.project
        if user.doc:
            env["CHUDWARE_OPEN_DOC"] = user.doc
        # Shared remote-model provider credentials (host-owned file). AUDIT:
        # these are the HOST's paid keys; a seat owner can read its own
        # container's environment, so forwarding is an explicit opt-in only.
        if _SHARE_PROVIDER_KEYS:
            provider_vars = self.cfg.ai_provider_vars()
            for k, v in provider_vars.items():
                env.setdefault(k, v)
            if provider_vars:
                log.info("forwarding %d AI provider var(s) into %s",
                         len(provider_vars), sess.container)
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
            f"chudware.sid={sess.sid}",
            "--label",
            f"chudware.email={sess.email}",
            "--label",
            f"chudware.display={sess.display}",
            "--label",
            f"chudware.slug={sess.slug}",
            "--label",
            f"chudware.port={sess.host_port}",
            "--label",
            f"chudware.heavy={'1' if sess.heavy else '0'}",
            "-p",
            f"127.0.0.1:{sess.host_port}:8080",
            "-v",
            # :Z private relabel — one home per container; without it SELinux
            # blocks container_t from user_home_t and NOTHING persists.
            f"{home}:/home/user:U,Z",
            "-v",
            # :z shared relabel — /plm bare repos are used by every session.
            f"{self.cfg.plm_dir}:/plm:U,z",
        ]
        if shared_project_mount:
            name = Path(shared_project_mount).name
            args.extend([
                "-v",
                # ro: demo seats browse/open the real working tree but can't write it;
                # :z shared relabel — the same folder is mounted by every session.
                f"{shared_project_mount}:/home/user/Projects/{name}:ro,z",
            ])
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

    # ---- ops surface (admin dashboard) ------------------------------------

    async def container_stats(self) -> dict[str, dict[str, str]]:
        """One `podman stats` snapshot for all live session containers.

        Returns {container_name: {"cpu": "3.1%", "mem": "1.2GB / 5GB"}}.
        Text template instead of --format json: the JSON key casing changed
        across podman 4→5, the Go template names did not.
        """
        rc, out, _ = await self.run_podman(
            ["stats", "--no-stream", "--format", "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}"],
            timeout=20,
        )
        stats: dict[str, dict[str, str]] = {}
        if rc != 0:
            return stats
        for line in out.splitlines():
            parts = line.strip().split("|")
            if len(parts) == 3 and parts[0]:
                stats[parts[0]] = {"cpu": parts[1], "mem": parts[2]}
        return stats

    async def podman_bytes(self, args: list[str], *, timeout: float = 30.0) -> tuple[int, bytes]:
        """run_podman variant for binary stdout (screenshots)."""
        with tempfile.TemporaryFile() as out_f:
            proc = await asyncio.create_subprocess_exec(
                self.podman_bin,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=out_f,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                rc = await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                proc.kill()
                raise PodmanError(f"podman timed out: {' '.join(args[:4])}") from exc
            out_f.seek(0)
            data = out_f.read()
        return rc, data

    async def screenshot_png(self, sess: Session, *, width: int = 560) -> bytes | None:
        """Downscaled PNG of the session's X display (admin thumbnails)."""
        rc, data = await self.podman_bytes(
            ["exec", sess.container, "sh", "-c",
             f"DISPLAY=:0 xwd -root -silent | magick xwd:- -resize {int(width)}x png:-"],
            timeout=25,
        )
        if rc != 0 or not data.startswith(b"\x89PNG"):
            return None
        return data

    async def paste_text(self, sess: Session, text: str) -> tuple[bool, str]:
        """Type text into the session's focused widget via xdotool.

        argv-passed (no shell), so arbitrary user text is inert.
        """
        rc, _, err = await self.run_podman(
            ["exec", "-e", "DISPLAY=:0", sess.container,
             "xdotool", "type", "--clearmodifiers", "--delay", "12", "--", text],
            timeout=max(30.0, len(text) * 0.05),
        )
        if rc != 0:
            return False, (err or "xdotool failed").strip()[:200]
        sess.touch()
        return True, "ok"

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
