"""Per-session throwaway containers through the rootless podman CLI.

Everything here is argument lists handed to ``subprocess`` (never a shell). The flag
set is the measured-working minimum for the image's nested bubblewrap actor sandbox;
web/demo/README.md explains each flag and the SELinux tradeoff.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .config import DemoConfig
from .cookies import SID_RE

log = logging.getLogger("chudware-demo.podman")

LABEL = "chudware.demo"
LABEL_SID = "chudware.demo.sid"
LABEL_PORT = "chudware.demo.port"
LABEL_STARTED = "chudware.demo.started"
LABEL_IP = "chudware.demo.ip"
NAME_PREFIX = "chudware-demo-"
INNER_PORT = 8300
# Where the per-session provider socket lives inside the container (contract).
PROVIDER_MOUNT = "/run/chudware-demo"
CONTAINER_UID = "10001:10001"
_ENV_KEEP = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "XDG_RUNTIME_DIR", "XDG_DATA_HOME",
             "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "DBUS_SESSION_BUS_ADDRESS",
             "REGISTRY_AUTH_FILE")


class PodmanError(RuntimeError):
    pass


@dataclass(frozen=True)
class SessionSpec:
    sid: str
    port: int
    run_dir: Path          # host directory bind-mounted at /run/chudware-demo
    started: int           # epoch seconds, stored in a label so a restart can adopt
    ip_tag: str
    # Persistent per-visitor directory (<root>/<sid>/{work,data}); None keeps the
    # old ephemeral tmpfs layout (only for containers adopted from before this).
    workspace_dir: Path | None = None

    def __post_init__(self) -> None:
        if not SID_RE.fullmatch(self.sid):
            raise ValueError("malformed session id")

    @property
    def name(self) -> str:
        return NAME_PREFIX + self.sid


@dataclass(frozen=True)
class ContainerInfo:
    name: str
    state: str
    sid: str
    port: int | None
    started: int | None
    ip_tag: str

    @property
    def running(self) -> bool:
        return self.state == "running"


def build_run_args(cfg: DemoConfig, spec: SessionSpec) -> list[str]:
    """The complete ``podman run`` argv for one session container."""
    def tmpfs(path: str, size: str, mode: str = "0700") -> list[str]:
        # ``U`` chowns the fresh tmpfs to the container user (podman's spelling of
        # compose's uid=/gid=). The image's runtime refuses a /data /work /state that
        # is not owned by uid 10001 with mode 0700.
        return ["--tmpfs", f"{path}:rw,nosuid,nodev,U,mode={mode},size={size}"]

    env = {
        "CHUDWARE_WEB_CELL_BACKEND": "container",
        "CHUDWARE_CONTAINER_RUNTIME": "1",
        "CHUDWARE_WEB_OPERATOR": cfg.operator,
        # The proxy always addresses the container as 127.0.0.1:<port>, so that is the
        # only Host the inner gateway needs to answer to.
        "CHUDWARE_WEB_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "CHUDWARE_WEB_HOME": "/data",
        "CHUDWARE_WORKSPACE_ROOT": "/work",
        "CHUDWARE_WEB_APP": "/opt/chudware/app",
        "CHUDWARE_WEB_DEMO": "1",
    }
    args = [
        "run", "-d", "--rm", "--name", spec.name,
        "--pull=never", "--no-healthcheck",
        "--log-driver=k8s-file", "--log-opt=max-size=5m",
        "--label", f"{LABEL}=1",
        "--label", f"{LABEL_SID}={spec.sid}",
        "--label", f"{LABEL_PORT}={spec.port}",
        "--label", f"{LABEL_STARTED}={spec.started}",
        "--label", f"{LABEL_IP}={spec.ip_tag}",
        "--user", CONTAINER_UID,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        # Nested bwrap must mount a fresh /proc, which masked parent paths prevent, and
        # the SELinux container_t label denies. Measured on the target host: this pair
        # with podman's DEFAULT seccomp profile is sufficient; seccomp is NOT relaxed.
        "--security-opt", "systempaths=unconfined",
        "--security-opt", "label=disable",
    ]
    for opt in cfg.extra_security_opts:
        args += ["--security-opt", opt]
    args += [
        "--cpus", cfg.cpus,
        "--memory", cfg.memory, "--memory-swap", cfg.memory,
        "--pids-limit", str(cfg.pids_limit),
        "--shm-size", cfg.shm_size,
        "--ulimit", "nofile=8192:8192", "--ulimit", "core=0",
    ]
    if spec.workspace_dir is not None:
        # Persistent workspace: the visitor's saved files (/work) and cell data
        # (/data) survive the container. ``U`` chowns the bind to the container's
        # mapped uid (same trick as tmpfs above); nosuid/nodev as for tmpfs.
        for sub, mount in (("work", "/work"), ("data", "/data")):
            args += ["--volume", f"{spec.workspace_dir / sub}:{mount}:rw,nosuid,nodev,U"]
    else:
        args += tmpfs("/data", cfg.tmpfs_data)
        args += tmpfs("/work", cfg.tmpfs_work)
    args += tmpfs("/state", cfg.tmpfs_state)
    args += tmpfs("/run", cfg.tmpfs_run)
    args += tmpfs("/tmp", cfg.tmpfs_tmp, mode="1777")
    # Read-write so the front door can (re)create provider.sock; the directory is mode
    # 0711 on the host, so the container user can traverse it but never write to it.
    args += ["--volume", f"{spec.run_dir}:{PROVIDER_MOUNT}:rw,nosuid,nodev,noexec"]
    args += ["--network", "pasta" + (":" + ",".join(cfg.pasta_options) if cfg.pasta_options else "")]
    args += ["--publish", f"127.0.0.1:{spec.port}:{INNER_PORT}/tcp"]
    for key, value in env.items():
        args += ["--env", f"{key}={value}"]
    args.append(cfg.image)
    return args


def _podman_env() -> dict[str, str]:
    """A scrubbed environment: the front door's own secrets never reach podman/conmon."""
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    env.update({k: v for k, v in os.environ.items() if k.startswith("CONTAINERS_")})
    return env


async def run_command(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Run ``argv`` (no shell); returns (rc, stdout, stderr).

    Output goes to temp FILES, not pipes: ``podman run -d`` hands its stdio to conmon,
    which keeps them open for the container's whole life, so a pipe never reaches EOF.
    """
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.DEVNULL, stdout=out_f, stderr=err_f,
                env=_podman_env())
        except OSError as exc:
            raise PodmanError(f"cannot run {argv[0]}: {exc}") from None
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()
            raise PodmanError(f"podman timed out: {' '.join(argv[1:4])}") from None
        out_f.seek(0)
        err_f.seek(0)
        return (rc, out_f.read().decode("utf-8", "replace"),
                err_f.read().decode("utf-8", "replace"))


class PodmanRuntime:
    """The runtime interface the session manager depends on (fakeable in tests)."""

    def __init__(self, cfg: DemoConfig, *, exec_fn=None, http_client: httpx.AsyncClient | None = None):
        self.cfg = cfg
        self._exec = exec_fn or run_command
        self._http = http_client

    async def _podman(self, *args: str, timeout: float = 60.0) -> tuple[int, str, str]:
        return await self._exec([self.cfg.podman_bin, *args], timeout)

    async def start(self, spec: SessionSpec) -> None:
        rc, out, err = await self._podman(*build_run_args(self.cfg, spec), timeout=120)
        if rc != 0:
            raise PodmanError(f"podman run failed (rc={rc}): {(err or out).strip()[-400:]}")
        log.info("started container %s (%s) port=%s", spec.name, out.strip()[:12], spec.port)

    async def is_running(self, name: str) -> bool:
        rc, out, _ = await self._podman("ps", "--filter", f"name=^{name}$",
                                        "--filter", "status=running", "--format", "{{.Names}}",
                                        timeout=20)
        return rc == 0 and name in out.split()

    async def logs_tail(self, name: str, lines: int = 40) -> str:
        try:
            rc, out, err = await self._podman("logs", "--tail", str(lines), name, timeout=15)
        except PodmanError:
            return ""
        return (out + err).strip() if rc == 0 else ""

    async def remove(self, name: str) -> None:
        rc, out, err = await self._podman("rm", "-f", "--time", "3", name, timeout=45)
        if rc != 0 and "no such container" not in (err + out).lower():
            raise PodmanError(f"podman rm {name} failed (rc={rc}): {(err or out).strip()[-300:]}")

    async def list_labelled(self) -> list[ContainerInfo]:
        template = "\t".join([
            "{{.Names}}", "{{.State}}", f'{{{{index .Labels "{LABEL_SID}"}}}}',
            f'{{{{index .Labels "{LABEL_PORT}"}}}}', f'{{{{index .Labels "{LABEL_STARTED}"}}}}',
            f'{{{{index .Labels "{LABEL_IP}"}}}}'])
        rc, out, err = await self._podman("ps", "-a", "--filter", f"name=^{NAME_PREFIX}",
                                          "--format", template, timeout=30)
        if rc != 0:
            raise PodmanError(f"podman ps failed (rc={rc}): {err.strip()[-300:]}")
        infos = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 6 or not parts[0].startswith(NAME_PREFIX):
                continue
            name, state, sid, port, started, ip_tag = parts

            def number(raw: str) -> int | None:
                return int(raw) if raw.isdigit() else None

            infos.append(ContainerInfo(name, state.lower(), sid, number(port), number(started), ip_tag))
        return infos

    async def workspace_usage(self, workspace_dir: Path) -> int | None:
        """Bytes under work+data, measured inside the caller's user namespace.

        The container's files are owned by its mapped subuid at mode 0700, so the
        front-door user cannot walk them directly; ``podman unshare`` can. None
        means "could not measure" and the caller falls back to a plain walk.
        """
        targets = [str(workspace_dir / sub) for sub in ("work", "data")
                   if (workspace_dir / sub).is_dir()]
        if not targets:
            return 0
        try:
            rc, out, _ = await self._podman("unshare", "du", "-sb", *targets, timeout=60)
        except PodmanError:
            return None
        if rc != 0:
            return None
        total = 0
        for line in out.splitlines():
            head = line.split("\t", 1)[0].strip()
            if head.isdigit():
                total += int(head)
        return total

    async def remove_workspace(self, workspace_dir: Path) -> bool:
        """Erase a workspace whose files belong to the container's mapped subuid."""
        if not workspace_dir.is_dir():
            return True
        try:
            rc, _, _ = await self._podman("unshare", "rm", "-rf", str(workspace_dir), timeout=120)
        except PodmanError:
            return False
        return rc == 0 and not workspace_dir.is_dir()

    async def storage_path(self) -> Path | None:
        try:
            rc, out, _ = await self._podman("info", "--format", "{{.Store.GraphRoot}}", timeout=30)
        except PodmanError:
            return None
        path = Path(out.strip()) if rc == 0 and out.strip() else None
        return path if path and path.is_dir() else None

    async def healthy(self, spec: SessionSpec, *, timeout: float = 3.0) -> bool:
        """One /healthz probe through the published loopback port."""
        client = self._http or httpx.AsyncClient(timeout=timeout, trust_env=False)
        try:
            resp = await client.get(f"http://127.0.0.1:{spec.port}/healthz", timeout=timeout)
            body = resp.json() if resp.status_code == 200 else {}
        except (httpx.HTTPError, ValueError, OSError):
            return False
        finally:
            if self._http is None:
                await client.aclose()
        if not isinstance(body, dict) or body.get("ok") is not True:
            return False
        if body.get("demo") is not True:
            log.warning("container %s answers /healthz without demo=true; image predates demo mode",
                        spec.name)
        return True

    async def wait_ready(self, spec: SessionSpec, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        next_alive = time.monotonic() + 5
        while time.monotonic() < deadline:
            if await self.healthy(spec, timeout=2.0):
                return True
            if time.monotonic() >= next_alive:
                if not await self.is_running(spec.name):
                    log.warning("container %s exited before becoming healthy", spec.name)
                    return False
                next_alive = time.monotonic() + 5
            await asyncio.sleep(1.0)
        tail = await self.logs_tail(spec.name)
        if tail:
            log.warning("container %s never became healthy; last output:\n%s", spec.name, tail)
        return False


__all__ = ["ContainerInfo", "PodmanError", "PodmanRuntime", "SessionSpec", "build_run_args",
           "run_command"]
