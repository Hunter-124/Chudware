import hashlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading

import pytest

DEPLOY = Path(__file__).resolve().parents[1]
CELL = DEPLOY / "cell"
sys.path.insert(0, str(CELL))
import cell_runtime as runtime


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
