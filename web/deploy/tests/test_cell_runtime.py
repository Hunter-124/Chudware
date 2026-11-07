import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading

import pytest

DEPLOY = Path(__file__).resolve().parents[1]
CELL = DEPLOY / "cell"
sys.path.insert(0, str(CELL))
import cell_runtime as runtime
from _cell_test_support import disposable_quota_plan, owner_roots


def load(name):
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(CELL / name))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def private_json(path, data):
    path.write_text(json.dumps(data))
    path.chmod(0o600)


@pytest.fixture
def layout():
    # AF_UNIX paths are limited to ~107 bytes; pytest's tmp roots can exceed that.
    tmp_path = Path(tempfile.mkdtemp(prefix="cw-", dir="/tmp")).resolve()
    cells = tmp_path / "cells"
    cells.mkdir(mode=0o700)
    work = tmp_path / "workspace"
    work.mkdir()
    key = "user:01234567-0123-4567-89ab-0123456789ab"
    cell_id = hashlib.sha256(key.encode()).hexdigest()[:32]
    cell = cells / cell_id
    cell.mkdir(mode=0o700)
    (cell / "state").mkdir(mode=0o700)
    cfg = {"schema": "chudware-cell-runtime-v1", "cells_root": str(cells),
           "allowed_roots": [str(work)], "egress_hosts": ["provider.example"]}
    private_json(cell / "cell.json", {"schema": "chudware-cell-v1", "cell_id": cell_id,
                                      "workspace_key": key, "root": str(work)})
    config = tmp_path / "runtime.json"
    private_json(config, cfg)
    yield tmp_path, cell, work, cell_id, cfg, config
    shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.fixture
def quota_layout():
    """The disposable plan's first owner as a launchable cell; its second owner is the sibling tenant.

    Kernel-quota admission needs root-provisioned evidence, never implicit sudo
    or an admission bypass. Entries the test adds to plan directories are removed.
    """
    plan, proof_root = disposable_quota_plan()
    key, work, state = owner_roots(plan["owners"][0])
    _, sibling_work, sibling_state = owner_roots(plan["owners"][1])
    cell = state.parent
    metadata = cell / "cell.json"
    previous = metadata.read_bytes() if metadata.exists() else None
    touched = (cell, work, state, sibling_state)
    before = {directory: set(os.listdir(directory)) for directory in touched}
    base = Path(tempfile.mkdtemp(prefix="cw-", dir="/tmp")).resolve()
    try:
        cfg = {"schema": "chudware-cell-runtime-v1", "cells_root": str(cell.parent),
               "allowed_roots": [str(work)], "egress_hosts": ["provider.example"],
               "quota_proof_root": proof_root}
        config = base / "runtime.json"
        private_json(config, cfg)
        private_json(metadata, {"schema": "chudware-cell-v1", "cell_id": cell.name,
                                "workspace_key": key, "root": str(work)})
        yield base, cell, work, cell.name, cfg, config, (sibling_work, sibling_state)
    finally:
        for directory in touched:
            for name in set(os.listdir(directory)) - before[directory]:
                path = directory / name
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        if previous is not None:
            metadata.write_bytes(previous)
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.parametrize("value", ["", "a" * 31, "A" * 32, "g" * 32, "../" + "a" * 32, "a" * 32 + ".socket", None])
def test_rejects_nonhex_ids(value):
    with pytest.raises(ValueError):
        runtime.hex_id(value)


def test_root_metadata_cannot_escape_or_redirect(layout):
    base, cell, work, cell_id, cfg, _ = layout
    runtime.cell_config(cell_id, cfg)
    data = json.loads((cell / "cell.json").read_text())
    for root in (str(base), str(work) + "/../workspace", str(work) + "/link"):
        if root.endswith("/link"):
            (work / "link").symlink_to(base, target_is_directory=True)
        private_json(cell / "cell.json", {**data, "root": root})
        with pytest.raises((ValueError, OSError)):
            runtime.cell_config(cell_id, cfg)
    private_json(cell / "cell.json", data)
    (cell / "cell.json").chmod(0o644)
    with pytest.raises(ValueError):
        runtime.cell_config(cell_id, cfg)


def test_open_path_refuses_symlinked_ancestor(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "child").mkdir()
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        runtime.open_path(str(tmp_path / "linked/child"))


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.0.1", "169.254.169.254",
    "100.100.100.200", "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fc00::1", "ff02::1", "::ffff:8.8.8.8"])
def test_broker_refuses_nonpublic_and_metadata(address):
    calls = []
    with pytest.raises(ValueError):
        runtime.connect_public("provider.example", 443, {"provider.example"},
            resolver=lambda *a, **kw: [(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 0, "", (address, 443))],
            connector=lambda *args: calls.append(args))
    assert calls == []


def test_broker_pins_dns_and_rejects_mixed_answers():
    resolutions = []
    connections = []
    class Connection:
        def settimeout(self, seconds):
            pass
        def connect(self, address):
            connections.append(address)
        def close(self):
            pass
    def resolve(*args, **kwargs):
        resolutions.append(args)
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443))]
    runtime.connect_public("provider.example", 443, {"provider.example"}, resolve, lambda *a: Connection())
    assert len(resolutions) == 1
    assert connections == [("8.8.8.8", 443)]
    with pytest.raises(ValueError):
        runtime.connect_public("provider.example", 443, {"provider.example"},
            lambda *a, **k: [*resolve(), (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 443))],
            lambda *a: Connection())
    assert connections == [("8.8.8.8", 443)]


def test_optional_allowlist_preserves_public_research_and_denies_empty_policy():
    addresses = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443))]
    class Connection:
        def settimeout(self, value):
            pass
        def connect(self, address):
            assert address == ("8.8.8.8", 443)
        def close(self):
            pass
    runtime.connect_public("research.example", 443, None, lambda *a, **k: addresses, lambda *a: Connection())
    with pytest.raises(ValueError):
        runtime.connect_public("research.example", 443, [], lambda *a, **k: addresses, lambda *a: Connection())


@pytest.mark.parametrize("address", ["168.63.129.16", "64:ff9b::7f00:1", "64:ff9b:1::a00:1"])
def test_public_looking_platform_or_translation_addresses_are_not_egress(address):
    assert not runtime.public_address(address)


@pytest.mark.parametrize("raw_request", [b"GET http://provider.example/ HTTP/1.1\r\n\r\n",
    b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n", b"CONNECT metadata.google.internal:443 HTTP/1.1\r\n\r\n",
    b"CONNECT provider.example:80 HTTP/1.1\r\n\r\n", b"CONNECT provider.example:443 HTTP/1.1\r\nHost: other.example:443\r\n\r\n",
    b"CONNECT provider.example:443 HTTP/1.1\r\nContent-Length: 1\r\n\r\nx"])
def test_connect_parser_denies_forbidden_requests(raw_request):
    broker = load("chudware-egress-broker")
    client, server = socket.socketpair()
    thread = threading.Thread(target=broker.handle, args=(server, {"provider.example"}))
    thread.start()
    try:
        client.sendall(raw_request)
        assert client.recv(4096).startswith(b"HTTP/1.1 403")
    finally:
        client.close()
        thread.join(timeout=2)
    assert not thread.is_alive()


def test_redirect_tunnel_to_private_target_is_refused(monkeypatch):
    broker = load("chudware-egress-broker")
    targets = []
    def public_connect(host, port, hosts):
        targets.append(host)
        if host != "provider.example":
            raise ValueError("redirect destination refused")
        proxy, remote = socket.socketpair()
        def response():
            remote.sendall(b"HTTP/1.1 302 Found\r\nLocation: https://169.254.169.254/latest/meta-data/\r\nContent-Length: 0\r\n\r\n")
            remote.shutdown(socket.SHUT_WR)
            remote.close()
        threading.Thread(target=response).start()
        return proxy
    monkeypatch.setattr(broker, "connect_public", public_connect)
    for target, expected in [("provider.example", b"200"), ("169.254.169.254", b"403")]:
        client, server = socket.socketpair()
        thread = threading.Thread(target=broker.handle, args=(server, {"provider.example"}))
        thread.start()
        client.sendall(f"CONNECT {target}:443 HTTP/1.1\r\n\r\n".encode())
        assert expected in client.recv(4096).split(b"\r\n")[0]
        client.close()
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert targets == ["provider.example", "169.254.169.254"]


def test_relay_byte_and_idle_bounds():
    client, left = socket.socketpair()
    right, remote = socket.socketpair()
    try:
        client.sendall(b"12345")
        with pytest.raises(ValueError, match="byte limit"):
            runtime.relay(left, right, byte_limit=4, idle=1)
    finally:
        for sock in (client, left, right, remote):
            sock.close()
    client, left = socket.socketpair()
    right, remote = socket.socketpair()
    try:
        with pytest.raises(TimeoutError, match="idle limit"):
            runtime.relay(left, right, idle=0.01)
    finally:
        for sock in (client, left, right, remote):
            sock.close()


def test_active_relay_outlives_idle_budget():
    import time
    client, left = socket.socketpair()
    right, remote = socket.socketpair()
    failures = []
    def pump():
        try:
            runtime.relay(left, right, byte_limit=100, idle=0.5)
        except Exception as exc:
            failures.append(exc)
    thread = threading.Thread(target=pump)
    thread.start()
    try:
        remote.settimeout(2)
        started = time.monotonic()
        for _ in range(8):
            client.sendall(b"x")
            assert remote.recv(1) == b"x"
            time.sleep(0.1)
        assert time.monotonic() - started > 0.5
        client.shutdown(socket.SHUT_WR)
        remote.shutdown(socket.SHUT_WR)
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert failures == []
    finally:
        for sock in (client, left, right, remote):
            sock.close()
        thread.join(timeout=2)


def test_worker_bootstrap_isolated_argv_and_environment(monkeypatch):
    worker = load("chudware-worker")
    monkeypatch.setenv("CHUDWARE_CELL", "1")
    monkeypatch.setenv("CHUDWARE_ACTOR_SANDBOX", "1")
    monkeypatch.setenv("CHUDWARE_ACTOR_HEX", "a" * 32)
    monkeypatch.setenv("XDG_DATA_HOME", "/actor/data")
    monkeypatch.setenv("OPERATOR_ENV_SENTINEL", "not-tenant-data")
    monkeypatch.setenv("PYTHONPATH", "/unapproved")
    monkeypatch.setenv("LD_PRELOAD", "/unapproved.so")
    executable, argv, environment = worker.worker_argv(["--module", "worker"])
    assert executable == "/opt/chudware/venv/bin/python"
    assert argv == [executable, "-I", "-c", worker.BOOTSTRAP, "--module", "worker"]
    assert environment["PYTHONPATH"] == "/opt/chudware"
    assert environment["XDG_DATA_HOME"] == "/actor/data"
    assert environment["CHUDWARE_ACTOR_SANDBOX"] == "1"
    assert "OPERATOR_ENV_SENTINEL" not in environment and "LD_PRELOAD" not in environment
    monkeypatch.delenv("CHUDWARE_CELL")
    with pytest.raises(ValueError):
        worker.worker_argv(["--module", "worker"])


def test_unit_resource_and_fixed_argv_policy():
    for name in ("chudware-cell@.service", "chudware-egress-broker@.service"):
        text = (DEPLOY / name).read_text()
        assert "RuntimeMaxSec" not in text and "LimitAS" not in text
        for key in ("MemoryMax=", "MemorySwapMax=0", "CPUQuota=", "TasksMax=", "IOWeight="):
            assert key in text
        command = next(row.partition("=")[2] for row in text.splitlines() if row.startswith("ExecStart="))
        assert command.split()[1:] == ["%i"]
        assert "$" not in command and "%" not in command.replace("%i", "")
    socket_unit = (DEPLOY / "chudware-cell@.socket").read_text()
    assert "SocketMode=0600" in socket_unit and "Accept=no" in socket_unit


def test_worker_environment_carries_no_operator_credentials_or_endpoints(monkeypatch):
    worker = load("chudware-worker")
    monkeypatch.setenv("CHUDWARE_CELL", "1")
    for name, value in (("OPENAI_API_KEY", "operator-key-must-not-enter-cell"),
                        ("ANTHROPIC_API_KEY", "operator-key-must-not-enter-cell"),
                        ("ANTHROPIC_BASE_URL", "http://169.254.169.254"),
                        ("OPENAI_BASE_URL", "http://169.254.169.254"),
                        ("HTTP_PROXY", "http://169.254.169.254"), ("NO_PROXY", "*")):
        monkeypatch.setenv(name, value)
    _, _, environment = worker.worker_argv([])
    assert not {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                "OPENAI_BASE_URL"} & environment.keys()
    assert environment["HTTP_PROXY"] == "http://127.0.0.1:18080"
    assert environment["NO_PROXY"] == environment["no_proxy"] == "127.0.0.1,localhost,::1"


# --- public demo lane ---------------------------------------------------------------------


@pytest.fixture
def demo_tmp():
    """A short, symlink-free directory: AF_UNIX paths are limited to ~107 bytes and the
    launcher's no-follow walks refuse a symlinked ancestor."""
    path = Path(tempfile.mkdtemp(prefix="cw-", dir="/tmp")).resolve()
    yield path
    shutil.rmtree(path, ignore_errors=True)


def runtime_marker(tmp_path, value, mode=0o600):
    path = tmp_path / "runtime.json"
    path.write_text(value if isinstance(value, str) else json.dumps(value))
    path.chmod(mode)
    return str(path)


def test_demo_is_requested_by_the_private_supervisor_marker(demo_tmp):
    tmp_path = demo_tmp
    assert runtime.demo_requested({}, runtime_marker(tmp_path, {"demo": True})) is True
    assert runtime.demo_requested({}, runtime_marker(tmp_path, {"schema": "chudware-container-v1"})) is False
    assert runtime.demo_requested({}, str(tmp_path / "missing.json")) is False


@pytest.mark.parametrize("marker", [
    {"demo": "1"}, {"demo": 1}, {"demo": False}, [], "not json", '{"demo": tru',
])
def test_only_a_literal_true_marker_enables_demo(demo_tmp, marker):
    tmp_path = demo_tmp
    assert runtime.demo_requested({}, runtime_marker(tmp_path, marker)) is False


def test_a_marker_other_users_can_write_is_not_trusted(demo_tmp):
    tmp_path = demo_tmp
    assert runtime.demo_requested({}, runtime_marker(tmp_path, {"demo": True}, mode=0o666)) is False
    assert runtime.demo_requested({}, runtime_marker(tmp_path, {"demo": True}, mode=0o644)) is False


def test_first_level_launcher_ignores_an_environment_flag(demo_tmp):
    """hostd clears its environment; nothing a tenant can set at that level enables the lane."""
    tmp_path = demo_tmp
    marker = runtime_marker(tmp_path, {"schema": "chudware-container-v1"})
    assert runtime.demo_requested({"CHUDWARE_WEB_DEMO": "1"}, marker) is False


def test_nested_launcher_inherits_the_flag_from_its_parent_actor(demo_tmp):
    tmp_path = demo_tmp
    marker = runtime_marker(tmp_path, {"demo": True})
    nested = {"CHUDWARE_ACTOR_SANDBOX": "1"}
    assert runtime.demo_requested({**nested, "CHUDWARE_WEB_DEMO": "1"}, marker) is True
    # Inside an actor the supervisor's marker is not visible, and is never consulted.
    assert runtime.demo_requested(nested, marker) is False
    assert runtime.demo_requested({**nested, "CHUDWARE_WEB_DEMO": "true"}, marker) is False


def test_lane_socket_is_bound_only_in_a_demo_container_and_only_as_a_socket(demo_tmp):
    tmp_path = demo_tmp
    path = tmp_path / "provider.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    pins = []
    try:
        assert runtime.demo_lane_args(pins, False, str(path)) == []
        assert runtime.demo_lane_args(pins, True, str(tmp_path / "absent.sock")) == []
        assert pins == []
        args = runtime.demo_lane_args(pins, True, str(path))
        assert len(pins) == 1 and not os.get_inheritable(pins[0])
        assert args == ["--ro-bind", f"/proc/self/fd/{pins[0]}", runtime.DEMO_SOCKET]
        assert runtime.demo_socket_present(str(path)) and not runtime.demo_socket_present(str(tmp_path))
    finally:
        for fd in pins:
            os.close(fd)
        listener.close()


def test_lane_path_that_is_not_a_socket_is_refused_without_leaking_a_descriptor(demo_tmp):
    tmp_path = demo_tmp
    path = tmp_path / "provider.sock"
    path.write_text("not a socket")
    before = set(os.listdir("/proc/self/fd"))
    with pytest.raises(ValueError, match="must be a socket"):
        runtime.demo_lane_args([], True, str(path))
    assert set(os.listdir("/proc/self/fd")) == before
    assert not runtime.demo_socket_present(str(path))


BRIDGE = {"OMP_CAD_BRIDGE_TRANSPORT": "unix",
          "OMP_CAD_BRIDGE_ENDPOINT": "/actor/run/omp-cad-ab12cd34/omp-worker-1-2-0123456789ab.sock",
          "OMP_CAD_BRIDGE_TOKEN": "0123456789abcdef" * 4,
          "OMP_CAD_ASSET_STAGING_DIR": "", "OMP_CAD_ASSET_JOURNAL": "/actor/data/x/Sessions/s/j.jsonl"}


def test_extension_bridge_crosses_the_actor_boundary_unchanged():
    assert runtime.bridge_environment({**BRIDGE, "UNRELATED": "dropped"}) == BRIDGE
    assert runtime.bridge_environment({"PATH": "/usr/bin"}) == {}


@pytest.mark.parametrize("change", [
    {"OMP_CAD_BRIDGE_TRANSPORT": "tcp"},
    {"OMP_CAD_BRIDGE_ENDPOINT": "/tmp/attacker.sock"},
    {"OMP_CAD_BRIDGE_ENDPOINT": "/actor/../state/run/hostd.sock"},
    {"OMP_CAD_BRIDGE_ENDPOINT": "/actorx/run/b.sock"},
    {"OMP_CAD_BRIDGE_ENDPOINT": "relative.sock"},
    {"OMP_CAD_BRIDGE_TOKEN": "short"},
    {"OMP_CAD_BRIDGE_TOKEN": "has space in it, not a token"},
    {"OMP_CAD_ASSET_JOURNAL": "/state/actors/other/journal.jsonl"},
    {"OMP_CAD_ASSET_STAGING_DIR": "/work/../etc"},
])
def test_extension_bridge_outside_the_actor_namespace_is_refused(change):
    with pytest.raises(ValueError, match="outside the actor namespace"):
        runtime.bridge_environment({**BRIDGE, **change})


@pytest.fixture
def launcher_world(demo_tmp, monkeypatch):
    """The real actor launcher's argv/environment assembly, with only the filesystem faked."""
    tmp_path = demo_tmp
    launcher = load("chudware-actor-sandbox")
    egress = tmp_path / "egress.sock"
    lane = tmp_path / "provider.sock"
    listeners = []
    for path in (egress, lane):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        listeners.append(sock)
    actor = tmp_path / "actor"
    actor.mkdir()
    real_os = os

    class Shim:
        def __getattr__(self, name):
            return getattr(real_os, name)

        def open(self, path, flags, *args, **kwargs):
            return real_os.open(str(egress) if path == "/run/egress.sock" else path, flags, *args, **kwargs)

    opened = []

    def pinned(path, pins):
        fd = real_os.open(actor, real_os.O_RDONLY | real_os.O_DIRECTORY)
        opened.append(fd)
        pins.append(fd)
        return f"/proc/self/fd/{fd}"

    monkeypatch.setattr(launcher, "os", Shim())
    monkeypatch.setattr(launcher, "rootfs", lambda payload, pins: ["bwrap"])
    monkeypatch.setattr(launcher, "pinned", pinned)
    monkeypatch.setattr(launcher, "open_path", lambda path: real_os.open("/", real_os.O_RDONLY))
    monkeypatch.setattr(launcher, "demo_lane_args",
                        lambda pins, demo: runtime.demo_lane_args(pins, demo, str(lane)))
    for name in ("CHUDWARE_ACTOR_SANDBOX", "CHUDWARE_ACTOR_HEX", "CHUDWARE_WEB_DEMO", *runtime.BRIDGE_ENV,
                 "PI_CODING_AGENT_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                 "SSL_CERT_FILE", "PI_PACKAGE_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CHUDWARE_CELL", "1")
    yield launcher
    for sock in listeners:
        sock.close()
    for fd in opened:
        try:
            real_os.close(fd)
        except OSError:
            pass


def setenv_values(args):
    return {args[i + 1]: args[i + 2] for i, value in enumerate(args) if value == "--setenv"}


def test_launcher_binds_the_lane_and_flags_demo_only_for_a_demo_container(launcher_world, monkeypatch):
    monkeypatch.setattr(launcher_world, "demo_requested", lambda: False)
    args, pins = launcher_world.actor_argv("a" * 32, "rw", ["/bin/true"])
    assert runtime.DEMO_SOCKET not in args and "CHUDWARE_WEB_DEMO" not in setenv_values(args)
    monkeypatch.setattr(launcher_world, "demo_requested", lambda: True)
    args, pins = launcher_world.actor_argv("a" * 32, "rw", ["/bin/true"])
    binds = [(args[i + 1], args[i + 2]) for i, value in enumerate(args) if value == "--ro-bind"]
    assert any(target == runtime.DEMO_SOCKET for _, target in binds)
    assert setenv_values(args)["CHUDWARE_WEB_DEMO"] == "1"


def test_launcher_forwards_the_extension_bridge_only_from_inside_an_actor(launcher_world, monkeypatch):
    monkeypatch.setattr(launcher_world, "demo_requested", lambda: False)
    for key, value in BRIDGE.items():
        monkeypatch.setenv(key, value)
    args, _ = launcher_world.actor_argv("a" * 32, "rw", ["/bin/true"])
    assert not set(BRIDGE) & setenv_values(args).keys()  # first level: never an env input
    monkeypatch.setenv("CHUDWARE_ACTOR_SANDBOX", "1")
    monkeypatch.setenv("CHUDWARE_ACTOR_HEX", "a" * 32)
    args, _ = launcher_world.actor_argv("a" * 32, "rw", ["/bin/true"])
    assert {key: setenv_values(args)[key] for key in BRIDGE} == BRIDGE
    monkeypatch.setenv("OMP_CAD_BRIDGE_ENDPOINT", "/tmp/outside.sock")
    with pytest.raises(ValueError, match="outside the actor namespace"):
        launcher_world.actor_argv("a" * 32, "rw", ["/bin/true"])


@pytest.fixture
def forwarder():
    import cell_forwarder
    return cell_forwarder


def record_lanes(monkeypatch, forwarder):
    lanes = []

    class Lane:
        def __init__(self, port, path, provider):
            self.port, self.path, self.provider, self.closed = port, path, provider, False

        def close(self):
            self.closed = True

    def start_lane(port, path, *, provider=False):
        lanes.append(Lane(port, path, provider))
        return lanes[-1]

    monkeypatch.setattr(forwarder, "_start_lane", start_lane)
    return lanes


def test_single_operator_cell_never_opens_the_provider_lane(monkeypatch, forwarder):
    lanes = record_lanes(monkeypatch, forwarder)
    monkeypatch.setattr(forwarder, "demo_requested", lambda environ=None: False)
    monkeypatch.setattr(forwarder, "demo_socket_present", lambda path=runtime.DEMO_SOCKET: True)
    assert len(forwarder.start()) == 1
    assert [(lane.port, lane.path) for lane in lanes] == [(18080, "/run/egress.sock")]


def test_demo_cell_without_a_mounted_socket_opens_no_provider_lane(monkeypatch, forwarder):
    lanes = record_lanes(monkeypatch, forwarder)
    monkeypatch.setattr(forwarder, "demo_requested", lambda environ=None: True)
    monkeypatch.setattr(forwarder, "demo_socket_present", lambda path=runtime.DEMO_SOCKET: False)
    forwarder.start()
    assert [lane.port for lane in lanes] == [18080]


def test_demo_cell_with_its_socket_opens_exactly_the_fixed_provider_lane(monkeypatch, forwarder):
    lanes = record_lanes(monkeypatch, forwarder)
    monkeypatch.setattr(forwarder, "demo_requested", lambda environ=None: True)
    monkeypatch.setattr(forwarder, "demo_socket_present", lambda path=runtime.DEMO_SOCKET: True)
    forwarder.start()
    assert [(lane.port, lane.path, lane.provider) for lane in lanes] == [
        (18080, "/run/egress.sock", False),
        (18081, "/run/chudware-demo/provider.sock", True)]


def test_container_supervisor_never_opens_the_provider_lane(monkeypatch, forwarder):
    lanes = record_lanes(monkeypatch, forwarder)
    monkeypatch.setattr(forwarder, "demo_requested", lambda environ=None: True)
    monkeypatch.setattr(forwarder, "demo_socket_present", lambda path=runtime.DEMO_SOCKET: True)
    forwarder.start(provider=False)
    assert [lane.port for lane in lanes] == [18080]


def test_a_failed_provider_bind_closes_the_egress_lane(monkeypatch, forwarder):
    lanes = record_lanes(monkeypatch, forwarder)
    original = forwarder._start_lane

    def fail_on_provider(port, path, *, provider=False):
        if provider:
            raise OSError("address in use")
        return original(port, path, provider=provider)

    monkeypatch.setattr(forwarder, "_start_lane", fail_on_provider)
    with pytest.raises(OSError):
        forwarder.start(provider=True)
    assert lanes and all(lane.closed for lane in lanes)


def test_provider_lane_relays_a_long_stream_to_the_front_door_socket(forwarder):
    """Loopback TCP in the actor -> the per-session unix socket, with no byte cap."""
    with tempfile.TemporaryDirectory(prefix="cw-lane-", dir="/tmp") as raw:
        path = str(Path(raw) / "p.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        lane = forwarder._start_lane(port, path, provider=True)
        payload = b"x" * (2 * 1024 * 1024)  # past the egress lane's 64 MiB-per-connection scale in spirit, still fast
        try:
            client = socket.create_connection(("127.0.0.1", port), timeout=5)
            peer, _ = server.accept()
            peer.settimeout(5)
            client.sendall(b"GET /v1/models HTTP/1.1\r\n\r\n")
            assert peer.recv(64).startswith(b"GET /v1/models")
            threading.Thread(target=peer.sendall, args=(payload,), daemon=True).start()
            received = bytearray()
            while len(received) < len(payload):
                chunk = client.recv(65536)
                assert chunk
                received.extend(chunk)
            assert bytes(received) == payload
            client.close()
            peer.close()
        finally:
            lane.close()
            server.close()


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap absent")
@pytest.mark.parametrize("access", ["ro", "rw"])
def test_real_bwrap_cell_and_actor_escape_boundaries(quota_layout, access):
    base, cell, work, cell_id, cfg, config, (sibling_work, sibling_state) = quota_layout
    payload = base / "payload"
    (payload / "bin").mkdir(parents=True)
    for source in CELL.iterdir():
        if source.is_file():
            shutil.copy2(source, payload / "bin" / source.name)
    actor = "a" * 32
    other_actor = "b" * 32
    (cell / "state/actors").mkdir(mode=0o700)
    for actor_id in (actor, other_actor):
        directory = cell / "state/actors" / actor_id
        directory.mkdir(mode=0o700)
        (directory / "secret").write_text(actor_id)
    (sibling_state / "secret").write_text("sibling-cell")
    gateway = base / "gateway-data"
    gateway.mkdir()
    (gateway / "secret").write_text("gateway")
    for name, target in (("sibling", sibling_state / "secret"), ("gateway", gateway / "secret"),
                         ("other-actor", cell / "state/actors" / other_actor / "secret")):
        (work / name).symlink_to(target)
    # These actual hostile links are refused at the quota boundary, not merely
    # hidden by bwrap or masked by an unrelated missing-proof refusal.
    from storage_quota import QuotaRefused, admit
    with pytest.raises(QuotaRefused, match="symlink"):
        admit(cell_id, str(work), str(cell / "state"), cfg["quota_proof_root"])
    for name in ("sibling", "gateway", "other-actor"):
        (work / name).unlink()
    verified = admit(cell_id, str(work), str(cell / "state"), cfg["quota_proof_root"])
    for descriptor in verified:
        os.close(descriptor)
    broker_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # Plan directories may exceed the AF_UNIX path limit; bind through a descriptor.
    cell_fd = os.open(cell, os.O_PATH | os.O_DIRECTORY)
    try:
        broker_socket.bind(f"/proc/self/fd/{cell_fd}/egress.sock")
        listener.bind(f"/proc/self/fd/{cell_fd}/gw.sock")
    finally:
        os.close(cell_fd)
    broker_socket.listen()
    broker_socket.settimeout(5)
    listener.listen()
    other_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    other_socket.bind(str(base / "other.sock"))
    other_socket.listen()
    host_tcp = socket.socket()
    host_tcp.bind(("127.0.0.1", 0))
    host_tcp.listen()
    host_port = host_tcp.getsockname()[1]
    denied = [str(Path.home()), "/run/user", str(gateway), str(sibling_state.parent), str(sibling_work),
              str(base / "other.sock"), str(cell / "gw.sock"), f"/proc/{os.getpid()}/environ"]
    probe = f'''#!/usr/bin/python3 -I
import errno, fcntl, os, pathlib, socket, struct, subprocess, sys
assert "OPERATOR_ENV_SENTINEL" not in os.environ
open_extra = []
for descriptor in os.listdir("/proc/self/fd"):
    fd = int(descriptor)
    try:
        os.fstat(fd)
    except OSError:
        continue
    if fd > 2:
        open_extra.append(fd)
assert sorted(open_extra) == ([] if sys.argv[-1] == "actor" else [3]), open_extra
# Even the owning actor cannot clear inheritance or change project IDs inside
# a user namespace. Readonly Ask/Plan lanes may reject the ioctl earlier.
quota_fd = os.open("/work", os.O_RDONLY | os.O_DIRECTORY)
attributes = bytearray(28)
fcntl.ioctl(quota_fd, 0x801C581F, attributes, True)
fields = list(struct.unpack("=IIIII8s", attributes))
assert fields[0] & 0x200 and fields[3] > 0
fields[0] &= ~0x200
fields[3] = 0
try:
    fcntl.ioctl(quota_fd, 0x401C5820, struct.pack("=IIIII8s", *fields))
except OSError as exc:
    assert exc.errno in (errno.EINVAL, errno.EPERM, errno.EACCES, errno.EROFS), exc
else:
    raise AssertionError("actor escaped project quota inheritance")
after = bytearray(28)
fcntl.ioctl(quota_fd, 0x801C581F, after, True)
assert after == attributes
os.close(quota_fd)
for name in {denied!r} + ["/work/sibling", "/work/gateway", "/work/other-actor"]:
    try:
        pathlib.Path(name).read_bytes() if not pathlib.Path(name).is_dir() else list(pathlib.Path(name).iterdir())
    except OSError:
        pass
    else:
        raise AssertionError("host access: " + name)
for address in [("127.0.0.1", {host_port}), ("8.8.8.8", 443)]:
    sock = socket.socket(); sock.settimeout(.2)
    assert sock.connect_ex(address) != 0
    sock.close()
result = subprocess.run(["/usr/bin/systemd-run", "--user", "--pipe", "/usr/bin/true"], capture_output=True)
assert result.returncode != 0
assert not pathlib.Path("/run/dbus/system_bus_socket").exists()
if sys.argv[-1] == "actor":
    assert not pathlib.Path("/state").exists()
    assert pathlib.Path("/actor/secret").read_text() == {actor!r}
    assert set(p.name for p in pathlib.Path("/dev").iterdir()) == {{"null", "zero", "urandom"}}
    try:
        pathlib.Path("/work/written").write_text("write")
    except OSError:
        assert {access!r} == "ro"
    else:
        assert {access!r} == "rw"
    pathlib.Path("/actor/own-write").write_text("own")
    proxy = socket.create_connection(("127.0.0.1", 18080), timeout=2)
    proxy.sendall(b"CONNECT 127.0.0.1:443 HTTP/1.1\\r\\n\\r\\n")
    assert proxy.recv(4096).startswith(b"HTTP/1.1 403")
    proxy.close()
    code = "import pathlib; assert not pathlib.Path('/state').exists(); assert pathlib.Path('/actor/secret').read_text()==" + repr({actor!r})
    if {access!r} == "ro":
        code += "; pathlib.Path('/work/nested-write').write_text('must-fail')"
        nested = subprocess.run(["/opt/chudware/bin/chudware-actor-sandbox", {actor!r}, "rw", "--", "/usr/bin/python3", "-I", "-S", "-c", code], capture_output=True)
        assert nested.returncode != 0 and b"Read-only file system" in nested.stderr
    else:
        subprocess.run(["/opt/chudware/bin/chudware-actor-sandbox", {actor!r}, "rw", "--", "/usr/bin/python3", "-I", "-S", "-c", code], check=True)
else:
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:18080"
    assert os.environ["NO_PROXY"] == os.environ["no_proxy"] == "127.0.0.1,localhost,::1"
    inherited = socket.socket(fileno=os.dup(3))
    assert inherited.family == socket.AF_UNIX and inherited.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
    inherited.close()
    subprocess.run(["/opt/chudware/bin/chudware-actor-sandbox", {actor!r}, {access!r}, "--", "/opt/chudware/bin/chudware-hostd", "actor"], check=True)
print("escape-probe-passed", flush=True)
'''
    (payload / "bin/chudware-hostd").write_text(probe)
    (payload / "bin/chudware-hostd").chmod(0o755)
    def reject():
        try:
            connection, _ = broker_socket.accept()
        except OSError:
            return
        with connection:
            connection.recv(8192)
            connection.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
    broker_thread = threading.Thread(target=reject)
    broker_thread.start()
    wrapper = "import os,sys;os.dup2(int(sys.argv[1]),3,inheritable=True);os.environ['LISTEN_PID']=str(os.getpid());os.execv(sys.argv[2],[sys.argv[2],sys.argv[3]])"
    try:
        result = subprocess.run([sys.executable, "-I", "-S", "-c", wrapper, str(listener.fileno()),
            str(payload / "bin/chudware-cell-launch"), cell_id], pass_fds=(listener.fileno(),),
            env={**os.environ, "CHUDWARE_CELL_CONFIG": str(config), "LISTEN_FDS": "1", "OPERATOR_ENV_SENTINEL": "must-not-leak"},
            capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.count("escape-probe-passed") == 2
        assert (cell / "state/actors" / actor / "own-write").read_text() == "own"
        assert (work / "written").exists() == (access == "rw")
    finally:
        for sock in (broker_socket, listener, other_socket, host_tcp):
            sock.close()
        broker_thread.join(timeout=6)
