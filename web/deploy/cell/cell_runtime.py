"""Linux tenant-cell launch and bounded proxy primitives (stdlib only)."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import socket
import stat
import time

HEX = re.compile(r"[0-9a-f]{32}\Z")
HOST = re.compile(r"(?=.{1,253}\Z)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z")
FLAGS = ["--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
         "--unshare-net", "--unshare-cgroup-try", "--die-with-parent", "--new-session",
         "--cap-drop", "ALL", "--clearenv"]
BASE_ENV = {"PATH": "/opt/chudware/bin:/opt/chudware/venv/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
            "LD_LIBRARY_PATH": "/opt/chudware/bin", "PYTHONPATH": "/opt/chudware",
            "CHUDWARE_CELL": "1", "CHUDWARE_FS_ROOT": "/work", "CHUDWARE_PROJECT_DIR": "/work",
            "CHUDWARE_PROJECT_SAVE_DIR": "/work", "CHUDWARE_AGENT_PROMPTS_DIR": "/opt/chudware/agent/prompts",
            "HTTPS_PROXY": "http://127.0.0.1:18080", "HTTP_PROXY": "http://127.0.0.1:18080",
            "https_proxy": "http://127.0.0.1:18080", "http_proxy": "http://127.0.0.1:18080",
            "NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1"}
ACTOR_ENV = frozenset({"CHUDWARE_ACTOR_KEY", "CHUDWARE_WORKSPACE_KEY", "CHUDWARE_ACTOR_HEX",
    "CHUDWARE_CELL_RIGHT", "CHUDWARE_CELL_EPOCH", "CHUDWARE_HOSTD_WORKER_LANE", "CHUDWARE_WRITER_GROUP"})
BRIDGE_ENV = ("OMP_CAD_BRIDGE_TRANSPORT", "OMP_CAD_BRIDGE_ENDPOINT", "OMP_CAD_BRIDGE_TOKEN",
              "OMP_CAD_ASSET_STAGING_DIR", "OMP_CAD_ASSET_JOURNAL")


def actor_environment():
    """Only the actor's scoped identity may cross an actor boundary."""
    return {key: os.environ[key] for key in ACTOR_ENV if key in os.environ}


# Public-demo AI lane. The trusted front door bind-mounts ONE per-session unix socket here;
# it speaks OpenAI-compatible HTTP for an allowlist of free models and holds the only key.
DEMO_SOCKET = "/run/chudware-demo/provider.sock"
DEMO_PORT = 18081
RUNTIME_MARKER = "/run/chudware/runtime.json"
BRIDGE_TOKEN = re.compile(r"[A-Za-z0-9_-]{8,256}\Z")


def demo_requested(environ=None, marker=RUNTIME_MARKER):
    """True when this launcher belongs to a demo container (a flag, never a capability).

    hostd clears its environment before it starts the first actor, so there the container
    supervisor's private runtime marker is the source of truth; a launcher already inside an
    actor inherits the flag its parent actor was given. Whether the lane is usable is
    decided separately by the presence of the mounted socket.
    """
    environ = os.environ if environ is None else environ
    if environ.get("CHUDWARE_ACTOR_SANDBOX") == "1":
        return environ.get("CHUDWARE_WEB_DEMO") == "1"
    try:
        return read_json(marker).get("demo") is True
    except (OSError, ValueError, AttributeError):
        return False


def demo_socket_present(path=DEMO_SOCKET):
    try:
        return stat.S_ISSOCK(os.lstat(path).st_mode)
    except OSError:
        return False


def demo_lane_args(pins, demo, path=DEMO_SOCKET):
    """bwrap arguments that bind the front door's lane socket at the same path in the actor.

    Nothing is bound outside a demo container or while the socket is absent (the lane is then
    simply unavailable and AI refuses); a non-socket at that path is refused outright. The
    pinned descriptor, not the path, is what bwrap mounts.
    """
    if not demo:
        return []
    try:
        fd = os.open(path, os.O_PATH | os.O_NOFOLLOW)
    except OSError:
        return []
    if not stat.S_ISSOCK(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("demo provider lane must be a socket")
    os.set_inheritable(fd, False)
    pins.append(fd)
    return ["--ro-bind", "/proc/self/fd/" + str(fd), DEMO_SOCKET]


def bridge_environment(environ=None):
    """Extension-bridge coordinates for an OMP process launched from inside an actor.

    The bridge socket lives beneath /actor, which every nested sandbox of the same actor shares,
    so only paths there (or the workspace, for the session journal) and a well-formed token may
    cross; anything else is refused rather than dropped.
    """
    environ = os.environ if environ is None else environ
    values = {key: environ[key] for key in BRIDGE_ENV if key in environ}
    if not values:
        return {}

    def beneath(value, roots):
        parts = value.split("/")
        return (value.startswith("/") and "\x00" not in value and ".." not in parts
                and any(value == root or value.startswith(root + "/") for root in roots))

    if (values.get("OMP_CAD_BRIDGE_TRANSPORT") != "unix"
            or not beneath(values.get("OMP_CAD_BRIDGE_ENDPOINT", ""), ("/actor",))
            or not BRIDGE_TOKEN.fullmatch(values.get("OMP_CAD_BRIDGE_TOKEN", ""))
            or any(values.get(key) and not beneath(values[key], ("/actor", "/work"))
                   for key in ("OMP_CAD_ASSET_STAGING_DIR", "OMP_CAD_ASSET_JOURNAL"))):
        raise ValueError("extension bridge outside the actor namespace")
    return values


def hex_id(value):
    if not isinstance(value, str) or not HEX.fullmatch(value):
        raise ValueError("invalid cell/actor id")
    return value


def open_path(path, directory=True):
    """Pin an absolute path, refusing symlinks in EVERY component, including races."""
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise ValueError("absolute path required")
    parts = path.split("/")[1:]
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError("noncanonical path")
    fd = os.open("/", os.O_PATH | os.O_DIRECTORY)
    try:
        for index, part in enumerate(parts):
            flags = os.O_NOFOLLOW | os.O_CLOEXEC
            flags |= os.O_PATH if directory or index < len(parts) - 1 else os.O_RDONLY
            if directory or index < len(parts) - 1:
                flags |= os.O_DIRECTORY
            nxt = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


def private(fd, mode=None):
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o077 or (mode and stat.S_IMODE(info.st_mode) != mode):
        raise ValueError("cell metadata must be service-owned and private")


def read_json(path):
    fd = open_path(path, False)
    try:
        private(fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("regular config required")
        data = os.read(fd, 65537)
        if len(data) > 65536:
            raise ValueError("oversized config")
        return json.loads(data)
    finally:
        os.close(fd)


def configuration():
    cfg = read_json(os.environ.get("CHUDWARE_CELL_CONFIG", str(Path.home() / ".config/chudware/cell-runtime.json")))
    if cfg.get("schema") != "chudware-cell-runtime-v1" or not isinstance(cfg.get("allowed_roots"), list) or not cfg["allowed_roots"]:
        raise ValueError("invalid runtime configuration")
    for path in [cfg["cells_root"], *cfg["allowed_roots"]]:
        fd = open_path(path)
        os.close(fd)
    hosts = cfg.get("egress_hosts")
    if hosts is not None and (not isinstance(hosts, list) or any(not isinstance(h, str) or not HOST.fullmatch(h) or h.endswith(".") for h in hosts)):
        raise ValueError("invalid optional exact-host egress allowlist")
    return cfg


def cell_config(cell_id, cfg):
    hex_id(cell_id)
    cell = cfg["cells_root"] + "/" + cell_id
    fd = open_path(cell)
    try:
        private(fd, 0o700)
    finally:
        os.close(fd)
    info = read_json(cell + "/cell.json")
    if info.get("schema") != "chudware-cell-v1" or info.get("cell_id") != cell_id:
        raise ValueError("invalid cell metadata")
    key = info.get("workspace_key")
    if not isinstance(key, str) or hashlib.sha256(key.encode()).hexdigest()[:32] != cell_id:
        raise ValueError("workspace identity mismatch")
    root = info.get("root")
    if not isinstance(root, str) or not any(root == base or root.startswith(base + "/") for base in cfg["allowed_roots"]):
        raise ValueError("workspace outside allowed roots")
    fd = open_path(root)
    os.close(fd)
    return cell, root


def quota_roots(cell_id, cfg, cell, work, pins):
    """Mount the same descriptors checked against independent root quota proof."""
    from storage_quota import DEFAULT_PROOF_ROOT, admit
    work_fd, state_fd = admit(cell_id, work, cell + "/state",
                              cfg.get("quota_proof_root", DEFAULT_PROOF_ROOT))
    for fd in (work_fd, state_fd):
        os.set_inheritable(fd, False)
        pins.append(fd)
    private(state_fd, 0o700)
    return "/proc/self/fd/" + str(work_fd), "/proc/self/fd/" + str(state_fd)


def pinned(path, pins):
    fd = open_path(path)
    os.set_inheritable(fd, False)
    pins.append(fd)
    return "/proc/self/fd/" + str(fd)


def rootfs(payload, pins):
    args = ["/usr/bin/bwrap", *FLAGS, "--tmpfs", "/", "--dir", "/opt", "--dir", "/usr", "--dir", "/etc",
            "--ro-bind", pinned(payload, pins), "/opt/chudware"]
    # Runtime binaries and dynamic libraries, never host /etc, /home, /run or /var.
    for path in ("/usr/bin", "/usr/lib", "/usr/lib64"):
        if os.path.isdir(path):
            args += ["--ro-bind", pinned(path, pins), path]
    for name in ("bin", "lib", "lib64"):
        if os.path.exists("/" + name):
            args += ["--symlink", "usr/" + name, "/" + name]
    for path in ("/etc/ssl/certs", "/etc/pki/tls/certs", "/etc/pki/ca-trust/extracted"):
        if os.path.isdir(path) and not os.path.islink(path):
            args += ["--ro-bind", pinned(path, pins), path]
    args += ["--dir", "/dev"]
    for name in ("null", "zero", "urandom"):
        args += ["--dev-bind", "/dev/" + name, "/dev/" + name]
    args += ["--proc", "/proc", "--tmpfs", "/tmp", "--dir", "/run", "--chdir", "/work"]
    return args


def set_environment(args, values):
    for key, value in values.items():
        args += ["--setenv", key, value]


def close_extra_fds(keep=()):
    for name in os.listdir("/proc/self/fd"):
        fd = int(name)
        if fd > 2 and fd not in keep:
            try:
                os.close(fd)
            except OSError:
                pass


def run_sandbox(args, pins, keep=()):
    """Pins survive mount setup only; trusted in-cell init closes them before work."""
    close_extra_fds((*pins, *keep))
    for fd in (*pins, *keep):
        os.set_inheritable(fd, True)
    os.execve(args[0], args, {})


def public_address(value):
    address = ipaddress.ip_address(value.split("%")[0])
    if str(address) == "168.63.129.16":  # Azure platform/metadata wire-server, not public research
        return False
    if address.version == 6 and (address in ipaddress.ip_network("64:ff9b::/96") or address in ipaddress.ip_network("64:ff9b:1::/48")):
        return False  # NAT64 could translate a seemingly public IPv6 target into private IPv4
    # Reject mapped/transition IPv6 as well as all special/private/reserved ranges.
    return (address.is_global and not address.is_multicast and not address.is_reserved
            and not address.is_unspecified and not address.is_loopback and not address.is_link_local
            and not getattr(address, "ipv4_mapped", None) and not getattr(address, "sixtofour", None)
            and not getattr(address, "teredo", None))


def connect_public(host, port, hosts, resolver=socket.getaddrinfo, connector=socket.socket):
    if (hosts is not None and host not in hosts) or not HOST.fullmatch(host) or port != 443:
        raise ValueError("destination refused")
    addresses = resolver(host, port, type=socket.SOCK_STREAM)
    if not addresses or any(not public_address(row[4][0]) for row in addresses):
        raise ValueError("nonpublic DNS answer")
    last = None
    for family, kind, proto, _, address in addresses:
        connection = connector(family, kind, proto)
        try:
            connection.settimeout(10)
            connection.connect(address)  # numeric sockaddr only: NEVER re-resolve hostname
            return connection
        except OSError as exc:
            last = exc
            connection.close()
    raise OSError("public destination unreachable") from last


def relay(left, right, byte_limit=67108864, idle=120):
    """Bound memory, total bytes and inactivity; active streams have no lifetime cap."""
    selector = selectors.DefaultSelector()
    buffers = {left: bytearray(), right: bytearray()}
    peers = {left: right, right: left}
    reads = {left: True, right: True}
    total = 0
    last = time.monotonic()
    for sock in peers:
        sock.setblocking(False)
    try:
        while any(reads.values()) or any(buffers.values()):
            for sock in peers:
                events = (selectors.EVENT_READ if reads[sock] and len(buffers[peers[sock]]) < 65536 else 0)
                if buffers[sock]:
                    events |= selectors.EVENT_WRITE
                try:
                    selector.unregister(sock)
                except KeyError:
                    pass
                if events:
                    selector.register(sock, events)
            remaining = None if idle is None else idle - (time.monotonic() - last)
            if remaining is not None and remaining <= 0:
                raise TimeoutError("proxy idle limit")
            for key, events in selector.select(remaining):
                sock = key.fileobj
                if events & selectors.EVENT_READ:
                    try:
                        data = sock.recv(16384 if byte_limit is None else min(16384, byte_limit - total + 1))
                    except BlockingIOError:
                        continue
                    if not data:
                        reads[sock] = False
                        if not buffers[peers[sock]]:
                            peers[sock].shutdown(socket.SHUT_WR)
                    else:
                        if byte_limit is not None:
                            total += len(data)
                            if total > byte_limit:
                                raise ValueError("proxy byte limit")
                        buffers[peers[sock]].extend(data)
                        last = time.monotonic()
                if events & selectors.EVENT_WRITE:
                    try:
                        count = sock.send(buffers[sock])
                    except BlockingIOError:
                        continue
                    del buffers[sock][:count]
                    if count:
                        last = time.monotonic()
                    if not buffers[sock] and not reads[peers[sock]]:
                        sock.shutdown(socket.SHUT_WR)
    finally:
        selector.close()
