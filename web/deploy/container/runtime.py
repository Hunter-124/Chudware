#!/usr/bin/env python3
"""Single-operator container entrypoint; never a replacement for quota cell launchers."""
from __future__ import annotations

import ctypes
import fcntl
import hashlib
import http.client
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

PAYLOAD = Path("/opt/chudware")
HOME = Path("/data")
WORK = Path("/work")
STATE = Path("/state")
RUN = Path("/run/chudware")
IMAGE_MARKER = PAYLOAD / "container-runtime-v1"
WEB_OPTIONS = frozenset({
    "CHUDWARE_WEB_OPERATOR", "CHUDWARE_WEB_ALLOWED_HOSTS", "CHUDWARE_WEB_MAX_UPLOAD_BYTES",
    "CHUDWARE_WEB_WS_MAX_MESSAGE_BYTES", "CHUDWARE_WEB_MAX_ENGINE_BRIDGES",
})


class Stop:
    requested = False

    def signal(self, _signum, _frame):
        # Python's deferred signal handler only records intent. Child management,
        # descriptor cleanup and waits happen in the ordinary control flow.
        self.requested = True


class Supervisor:
    """Own direct children and the listener across clean idle exits and crashes."""

    def __init__(self, hostd, gateway, environment, listener, stop, *, restart_delay=1.0,
                 shutdown_timeout=25.0, reap_orphans=False):
        self.hostd = hostd
        self.gateway = gateway
        self.environment = environment
        self.listener = listener
        self.stop = stop
        self.restart_delay = restart_delay
        self.shutdown_timeout = shutdown_timeout
        self.reap_orphans = reap_orphans
        self.children = {}

    def spawn(self, name, command):
        child = subprocess.Popen(command, env=self.environment, cwd=str(WORK),
                                 pass_fds=(self.listener.fileno(),) if name == "hostd" else (),
                                 close_fds=True, start_new_session=True)
        self.children[name] = child
        return child

    def adopted(self, *, force=False):
        """PID 1 reaps only its own direct children, never kill(-1)/global discovery."""
        if not self.reap_orphans:
            return False
        managed = {child.pid for child in self.children.values()}
        try:
            pids = Path(f"/proc/self/task/{os.getpid()}/children").read_text().split()
        except FileNotFoundError:
            return False
        live = False
        for raw in pids:
            pid = int(raw)
            if pid in managed:
                continue
            try:
                reaped, _ = os.waitpid(pid, os.WNOHANG)
                if not reaped:
                    # A still-unreaped child cannot have its PID reused here.
                    os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)
                    live = True
            except (ChildProcessError, ProcessLookupError):
                pass
        return live

    def shutdown(self):
        for child in self.children.values():
            if child.poll() is None:
                try:
                    child.terminate()
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + self.shutdown_timeout
        while True:
            active = [child for child in self.children.values() if child.poll() is None]
            adopted = self.adopted(force=time.monotonic() >= deadline)
            if not active and not adopted:
                break
            if time.monotonic() >= deadline:
                for child in active:
                    try:
                        # Each child started its own session. No caller's or
                        # unrelated process group is ever signalled.
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            time.sleep(0.05)
        for child in self.children.values():
            child.wait()
        self.children.clear()

    def run(self, infrastructure_alive=lambda: True):
        restart_at = 0.0
        try:
            if self.stop.requested:
                return
            self.spawn("gateway", self.gateway)
            while not self.stop.requested:
                gateway = self.children["gateway"]
                if gateway.poll() is not None:
                    raise RuntimeError(f"gateway exited ({gateway.returncode}); inspect its startup error above")
                if not infrastructure_alive():
                    raise RuntimeError("private egress broker stopped; refusing unsandboxed fallback")
                hostd = self.children.get("hostd")
                if hostd is not None and hostd.poll() is not None:
                    print(f"container: hostd exited ({hostd.returncode}); restarting the cell", file=sys.stderr)
                    hostd.wait()
                    del self.children["hostd"]
                    restart_at = time.monotonic() + self.restart_delay
                self.adopted()
                if "hostd" not in self.children and time.monotonic() >= restart_at:
                    self.spawn("hostd", self.hostd)
                time.sleep(0.05)
        finally:
            self.shutdown()


class PrivateListener:
    def __init__(self, directory, name, cells):
        self.parent = cells.directory_beneath(directory)
        self.name = name
        self.identity = None
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            try:
                previous = os.stat(name, dir_fd=self.parent, follow_symlinks=False)
            except FileNotFoundError:
                previous = None
            if previous is not None:
                if (not stat.S_ISSOCK(previous.st_mode) or previous.st_uid != os.getuid()
                        or previous.st_mode & 0o077):
                    raise RuntimeError(f"unsafe stale socket: {directory / name}")
                os.unlink(name, dir_fd=self.parent)
            self.socket.bind(f"/proc/self/fd/{self.parent}/{name}")
            os.chmod(name, 0o600, dir_fd=self.parent, follow_symlinks=False)
            self.socket.listen(64)
            info = os.stat(name, dir_fd=self.parent, follow_symlinks=False)
            self.identity = (info.st_dev, info.st_ino)
        except BaseException:
            self.close()
            raise

    def close(self):
        self.socket.close()
        if self.parent is not None:
            try:
                info = os.stat(self.name, dir_fd=self.parent, follow_symlinks=False)
                if self.identity == (info.st_dev, info.st_ino):
                    os.unlink(self.name, dir_fd=self.parent)
            except FileNotFoundError:
                pass
            finally:
                os.close(self.parent)
                self.parent = None


def clean_environment(source):
    # Explicit allowlist, not ambient env with a few token names removed.
    options = {key: source[key] for key in WEB_OPTIONS if key in source}
    return {
        "PATH": "/opt/chudware/bin:/opt/chudware/venv/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "LD_LIBRARY_PATH": "/opt/chudware/bin",
        "HOME": "/state/home", "XDG_DATA_HOME": "/state/data", "XDG_CONFIG_HOME": "/state/config",
        "XDG_CACHE_HOME": "/state/cache", "XDG_RUNTIME_DIR": "/state/run", "TMPDIR": "/tmp",
        "CHUDWARE_CELL": "1", "CHUDWARE_FS_ROOT": "/work", "CHUDWARE_NATIVE_ROOT": "/work",
        "CHUDWARE_PROJECT_DIR": "/work", "CHUDWARE_PROJECT_SAVE_DIR": "/work",
        "CHUDWARE_AGENT_PROMPTS_DIR": "/opt/chudware/agent/prompts",
        "CHUDWARE_MATERIALS_REGISTRY": "/opt/chudware/core/data/materials/registry.json",
        "CHUDWARE_MACHINES_REGISTRY": "/opt/chudware/services/research/data/machines.json",
        "CHUDWARE_WORKER_PYTHON": "/opt/chudware/venv/bin/python3",
        "CHUDWARE_WEB_CELL_BACKEND": "container", "CHUDWARE_WEB_HOME": "/data",
        "CHUDWARE_WORKSPACE_ROOT": "/work", "CHUDWARE_WEB_APP": "/opt/chudware/app",
        "HTTPS_PROXY": "http://127.0.0.1:18080", "HTTP_PROXY": "http://127.0.0.1:18080",
        "https_proxy": "http://127.0.0.1:18080", "http_proxy": "http://127.0.0.1:18080",
        "NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1",
        **options,
    }


def guard_container(cells):
    if os.getpid() != 1 or os.getuid() == 0:
        raise RuntimeError("run this entrypoint as the image's non-root PID 1, not on the host or with init:true")
    if not any(path.is_file() and not path.is_symlink()
               for path in (Path("/.dockerenv"), Path("/run/.containerenv"))):
        raise RuntimeError("container boundary marker missing; use the supplied Docker/Podman image")
    try:
        fd, info = cells.open_beneath(IMAGE_MARKER.parent, IMAGE_MARKER.name, max_bytes=128)
    except Exception as exc:
        raise RuntimeError("container image marker unavailable; rebuild and run the supplied image") from exc
    with os.fdopen(fd, "rb") as handle:
        if info.st_uid != 0 or info.st_mode & 0o022 or handle.read() != b"chudware-container-v1\n":
            raise RuntimeError("invalid container image marker; rebuild the supplied image")


def private_directory(path, cells):
    try:
        fd = cells.directory_beneath(path)
    except Exception as exc:
        raise RuntimeError(f"required private volume/directory unavailable: {path}; check the supplied volume mounts") from exc
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise RuntimeError(f"{path} must be owned by uid {os.getuid()} with mode 0700; repair the named volume permissions")
    return fd


def write_json(directory, name, value, cells, *, replace=False):
    parent = cells.directory_beneath(directory)
    try:
        if not replace:
            try:
                fd, info = cells.open_beneath(directory, name, max_bytes=8192)
            except Exception as exc:
                if getattr(exc, "status_code", None) != 404:
                    raise
            else:
                with os.fdopen(fd, "rb") as handle:
                    if info.st_uid != os.getuid() or info.st_mode & 0o077 or json.load(handle) != value:
                        raise RuntimeError(f"{directory / name} identity mismatch; keep operator and volumes together")
                return
        temporary = "." + name + "-" + secrets.token_hex(12)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(value, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.rename(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
    finally:
        os.close(parent)


def require_payload(cells):
    files = [PAYLOAD / "bin" / name for name in (
        "chudware-hostd", "libchudware.so", "chudware-worker", "chudware-actor-sandbox",
        "chudware-actor-init", "cell_runtime.py", "cell_forwarder.py", "chudware-egress-broker")]
    files += [PAYLOAD / "venv/bin/python", PAYLOAD / "services/compiled_entry.py",
              PAYLOAD / "contracts/cell_methods.json", Path("/usr/bin/bwrap")]
    files += [PAYLOAD / "app" / name for name in ("index.html", "Chudware.js", "Chudware.wasm", "Chudware.data")]
    for path in files:
        if not path.is_file() or not path.stat().st_size:
            raise RuntimeError(f"required runtime payload missing: {path}; rebuild the complete native/worker/WASM image")
    for path in [PAYLOAD / "bin" / name for name in (
            "chudware-hostd", "chudware-worker", "chudware-actor-sandbox", "chudware-actor-init")]:
        if not os.access(path, os.X_OK):
            raise RuntimeError(f"runtime executable is not executable: {path}; rebuild the image")
    cells.method_policy()
    try:
        library = ctypes.CDLL(str(PAYLOAD / "bin/libchudware.so"))
    except OSError as exc:
        raise RuntimeError(f"native runtime dependency missing: {exc}; rebuild the image with its native library closure") from None
    for name in ("chudware_call", "chudware_free", "chudware_abi_version", "chudware_set_cache_refresh_sink"):
        if not hasattr(library, name):
            raise RuntimeError(f"native cell export missing: {name}; rebuild matching hostd and engine payloads")


def load_broker():
    loader = importlib.machinery.SourceFileLoader("container_egress_broker", str(PAYLOAD / "bin/chudware-egress-broker"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def probe_actor(environment, principal, stop):
    actor = hashlib.sha256(principal.actor_key.encode()).hexdigest()[:32]
    code = (
        'import os,pathlib,sys; '
        'assert os.environ.get("CHUDWARE_ACTOR_SANDBOX")=="1"; '
        'assert not pathlib.Path("/data").exists() and not pathlib.Path("/state").exists(); '
        'assert pathlib.Path("/work").is_dir() and pathlib.Path("/actor").is_dir(); '
        'sys.path.insert(0,"/opt/chudware"); import services.worker_main; '
        'from services.worker_main import _normalize_face_query; '
        'assert _normalize_face_query({"query":"top"},None)["query"]'
    )
    child = subprocess.Popen([str(PAYLOAD / "bin/chudware-actor-sandbox"), actor, "rw", "--",
                              str(PAYLOAD / "venv/bin/python"), "-I", "-c", code],
                             env=environment, cwd=str(WORK), close_fds=True, start_new_session=True)
    deadline = time.monotonic() + 90
    try:
        while child.poll() is None:
            if stop.requested or time.monotonic() >= deadline:
                raise RuntimeError("actor startup probe interrupted or timed out")
            time.sleep(0.05)
        if child.returncode:
            raise RuntimeError("actor sandbox startup failed; enable nested unprivileged user namespaces/bubblewrap "
                               "for this runtime (check seccomp, system-path masks and AppArmor/SELinux policy); no unsandboxed fallback is allowed. "
                               "Also inspect the worker dependency/import error above")
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
        child.wait()


def healthcheck(cells, settings):
    principal = cells.resolve_workspace()
    marker = cells.container_runtime(principal)
    require_payload(cells)
    sock = cells._connect_pinned(settings.cells_root / principal.cell_id,
                                 expected_socket=(marker["socket_device"], marker["socket_inode"]))
    with sock:
        sock.settimeout(5)
        sock.sendall(json.dumps({"id": 1, "method": "hello", "params": {
            "client": "gui", "proto": 1, "principal": principal.hello()}}).encode() + b"\n")
        with sock.makefile("rb") as reader:
            reply = json.loads(reader.readline(65537))
        if reply.get("id") != 1 or reply.get("result", {}).get("state") != "ready":
            raise RuntimeError("native cell is not ready")
    connection = http.client.HTTPConnection("127.0.0.1", 8300, timeout=5)
    try:
        connection.request("GET", "/healthz", headers={"Host": settings.allowed_hosts[0]})
        response = connection.getresponse()
        if response.status != 200 or json.loads(response.read(1024)) != {"ok": True}:
            raise RuntimeError("gateway is not ready")
    finally:
        connection.close()


def main():
    if sys.argv[1:] not in ([], ["healthcheck"]):
        raise RuntimeError("usage: runtime.py [healthcheck]")
    if os.environ.get("CHUDWARE_WEB_CELL_BACKEND") != "container":
        raise RuntimeError("CHUDWARE_WEB_CELL_BACKEND=container is required; the default systemd backend is unchanged")
    for key, expected in {"CHUDWARE_WEB_HOME": "/data", "CHUDWARE_WORKSPACE_ROOT": "/work",
                          "CHUDWARE_WEB_APP": "/opt/chudware/app"}.items():
        if os.environ.get(key, expected) != expected:
            raise RuntimeError(f"container layout requires {key}={expected}")
    environment = clean_environment(os.environ)
    os.environ.clear()
    os.environ.update(environment)
    os.umask(0o077)
    sys.path.insert(0, str(PAYLOAD / "web"))
    sys.path.insert(0, str(PAYLOAD / "bin"))
    from gateway import cells
    from gateway.config import settings
    if sys.argv[1:]:
        healthcheck(cells, settings)
        return
    guard_container(cells)
    stop = Stop()
    signal.signal(signal.SIGTERM, stop.signal)
    signal.signal(signal.SIGINT, stop.signal)
    require_payload(cells)
    for directory in (HOME, WORK, STATE):
        os.close(private_directory(directory, cells))
    state_fd = private_directory(STATE, cells)
    lock_fd = None
    listeners = []
    forwarders = []
    try:
        lock_fd = os.open("container.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
                          0o600, dir_fd=state_fd)
        lock_info = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid() or lock_info.st_mode & 0o077:
            raise RuntimeError("unsafe container state lock")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another runtime owns /state; do not share live volumes between containers") from None
        for name in ("home", "data", "config", "cache", "run", "actors"):
            try:
                os.mkdir(name, 0o700, dir_fd=state_fd)
            except FileExistsError:
                pass
            os.close(private_directory(STATE / name, cells))
        RUN.mkdir(mode=0o700, exist_ok=True)
        os.close(private_directory(RUN, cells))
        principal = cells.resolve_workspace()
        directory = cells.prepare_cell(principal)
        write_json(STATE, "container.json", {"schema": "chudware-container-state-v1",
                   "cell_id": principal.cell_id, "workspace_key": principal.workspace_key,
                   "root": str(principal.root)}, cells)
        actor = hashlib.sha256(principal.actor_key.encode()).hexdigest()[:32]
        actor_dir = STATE / "actors" / actor
        actor_dir.mkdir(mode=0o700, exist_ok=True)
        os.close(private_directory(actor_dir, cells))
        broker = PrivateListener(Path("/run"), "egress.sock", cells)
        listeners.append(broker)
        broker_module = load_broker()
        # Same CONNECT parser, public-address/DNS pinning and bounded relay as
        # systemd cells; actors receive only this socket in their network namespace.
        broker_thread = threading.Thread(target=broker_module.serve, args=(broker.socket, None), daemon=True)
        broker_thread.start()
        from cell_forwarder import start
        forwarders = start()
        environment["CHUDWARE_CELL_ID"] = principal.cell_id
        probe_actor(environment, principal, stop)
        gateway_socket = PrivateListener(directory, "gw.sock", cells)
        listeners.append(gateway_socket)
        write_json(RUN, "runtime.json", {"schema": "chudware-container-v1", "supervisor_pid": 1,
                   "uid": os.getuid(), "home": str(HOME), "root": str(WORK), "state": str(STATE),
                   "cell_id": principal.cell_id, "workspace_key": principal.workspace_key,
                   "socket_device": gateway_socket.identity[0], "socket_inode": gateway_socket.identity[1]},
                   cells, replace=True)
        hostd = [str(PAYLOAD / "bin/chudware-hostd"), "--cell", "--gateway-listen-fd",
                 str(gateway_socket.socket.fileno()), "--cell-state", str(STATE), "--cell-work", str(WORK)]
        gateway = [str(PAYLOAD / "venv/bin/python"), "-m", "uvicorn", "gateway.app:app",
                   "--app-dir", str(PAYLOAD / "web"), "--host", "0.0.0.0", "--port", "8300",
                   "--workers", "1", "--no-proxy-headers",
                   "--ws-max-size", str(settings.ws_max_message_bytes)]
        Supervisor(hostd, gateway, environment, gateway_socket.socket, stop,
                   reap_orphans=True).run(broker_thread.is_alive)
    finally:
        try:
            (RUN / "runtime.json").unlink(missing_ok=True)
        finally:
            for listener in reversed(listeners):
                listener.close()
            for listener in forwarders:
                listener.close()
            if lock_fd is not None:
                os.close(lock_fd)
            os.close(state_fd)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"container_refused: {exc}", file=sys.stderr)
        sys.exit(1)
