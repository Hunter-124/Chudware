from __future__ import annotations

import io
import json
import logging
import socket
import ssl

import pytest

from fixtures.mfg.printers import VirtualPrinter
from services.manufacturing.printers import (
    Artifact,
    AuthConfig,
    AuthorizationRequired,
    ConfigurationError,
    EndpointConfig,
    MoonrakerAdapter,
    OctoPrintAdapter,
    ProtocolError,
    PrusaLinkAdapter,
)
from services.manufacturing.printers.model import Redactor
from services.manufacturing.printers import http_client
from services.manufacturing.printers.model import _address_class
from services.manufacturing.printers.websocket import JsonWebSocket


def config(server: VirtualPrinter, auth: AuthConfig | None = None) -> EndpointConfig:
    return EndpointConfig(
        server.base_url,
        auth or AuthConfig("trusted_host"),
        endpoint_is_physical=True,
        origin="live",
        timeout_s=0.5,
        telemetry_ttl_ms=1_000,
        address_class="loopback",
    )


@pytest.mark.parametrize(('address', 'expected'), [
    ('127.0.0.1', 'loopback'), ('10.0.0.1', 'local'), ('172.16.0.1', 'local'),
    ('192.168.0.1', 'local'), ('169.254.1.1', 'local'), ('8.8.8.8', 'public'),
    ('0.1.2.3', 'invalid'), ('224.0.0.1', 'invalid'), ('255.255.255.255', 'invalid'),
    ('::', 'invalid'), ('::1', 'loopback'), ('ff02::1', 'invalid'),
    ('fc00::1', 'local'), ('fe80::1', 'local'), ('2001:4860:4860::8888', 'public'),
    ('::ffff:127.0.0.1', 'loopback'), ('::ffff:192.168.1.1', 'local'),
    ('::ffff:224.0.0.1', 'invalid'),
])
def test_http_address_classes_match_native_transport(address, expected):
    assert _address_class(address) == expected


def test_dns_endpoints_require_commissioned_class():
    with pytest.raises(ConfigurationError, match='requires address_class'):
        EndpointConfig('http://printer.example', AuthConfig('trusted_host'), False)
    with pytest.raises(ConfigurationError, match='requires address_class'):
        http_client.HttpClient('http://printer.example', AuthConfig('trusted_host'), 1)
    with pytest.raises(ConfigurationError, match='does not match'):
        EndpointConfig('http://127.0.0.1', AuthConfig('trusted_host'), False, address_class='public')


class _HttpSocket:
    def __init__(self, connections, traffic):
        self.connections = connections
        self.traffic = traffic
        self.closed = False

    def settimeout(self, timeout):
        self.timeout = timeout

    def setsockopt(self, *args):
        pass

    def connect(self, sockaddr):
        self.connections.append(sockaddr)

    def sendall(self, data):
        self.traffic.append(data)

    def makefile(self, *args):
        return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}')

    def close(self):
        self.closed = True


@pytest.mark.parametrize('secure', [False, True])
def test_http_pins_authorized_socket_retains_hostname_and_rechecks_dns(monkeypatch, secure):
    connections, traffic, lookups, sni = [], [], [], []
    answers = iter(['192.168.1.20', '127.0.0.1'])

    def resolve(host, port, *args):
        lookups.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (next(answers), port))]

    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    monkeypatch.setattr(socket, 'socket', lambda *args: _HttpSocket(connections, traffic))
    monkeypatch.setenv('http_proxy', 'http://forbidden-proxy.invalid:8080')
    monkeypatch.setenv('https_proxy', 'http://forbidden-proxy.invalid:8080')
    scheme = 'https' if secure else 'http'
    client = http_client.HttpClient(f'{scheme}://printer.example', AuthConfig('trusted_host'), 1,
                                  address_class='local')
    if secure:
        context = ssl.create_default_context()

        def wrap(raw, *, server_hostname):
            sni.append(server_hostname)
            return raw

        monkeypatch.setattr(context, 'wrap_socket', wrap)
        for handler in client._opener.handlers:
            if isinstance(handler, http_client._PinnedHTTPSHandler):
                handler._context = context
    assert client.request_json('GET', '/status') == {}
    assert connections == [('192.168.1.20', 443 if secure else 80)]
    assert b'Host: printer.example\r\n' in b''.join(traffic)
    assert sni == (['printer.example'] if secure else [])
    with pytest.raises(ProtocolError) as refused:
        client.request_json('GET', '/status')
    assert refused.value.reason_code == 'address_refused'
    assert len(lookups) == 2
    assert len(connections) == 1


def test_http_mixed_dns_answers_refuse_before_socket_creation(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, port, *args: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (address, port))
        for address in ('192.168.1.20', '8.8.8.8')])
    monkeypatch.setattr(socket, 'socket', lambda *args: pytest.fail('unauthorized DNS reached socket creation'))
    client = http_client.HttpClient('http://printer.example', AuthConfig('trusted_host'), 1,
                                  address_class='local')
    with pytest.raises(ProtocolError) as refused:
        client.request('GET', '/status')
    assert refused.value.reason_code == 'address_refused'


def test_http_request_cannot_leave_commissioned_origin(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args: pytest.fail('foreign request reached DNS'))
    client = http_client.HttpClient('http://printer.example', AuthConfig('trusted_host'), 1,
                                  address_class='local')
    with pytest.raises(ProtocolError) as refused:
        client.request('GET', 'https://other.example/status')
    assert refused.value.reason_code == 'address_refused'



def test_authorized_connect_retries_exact_numeric_addresses_and_closes_failed_socket(monkeypatch):
    connections, traffic, sockets = [], [], []
    addresses = [('192.168.1.20', 80), ('192.168.1.21', 80)]
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', address) for address in addresses])

    def make_socket(*args):
        raw = _HttpSocket(connections, traffic)
        if not sockets:
            def fail(address):
                connections.append(address)
                raise ConnectionRefusedError('fixture refusal')
            raw.connect = fail
        sockets.append(raw)
        return raw

    monkeypatch.setattr(socket, 'socket', make_socket)
    client = http_client.HttpClient('http://printer.example', AuthConfig('trusted_host'), 1,
                                  address_class='local')
    assert client.request_json('GET', '/status') == {}
    assert connections == addresses
    assert sockets[0].closed


def test_websocket_upgrade_rejects_rebound_dns_before_sending_credentials(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, port, *args: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', port))])
    monkeypatch.setattr(socket, 'socket', lambda *args: pytest.fail('rebound websocket created socket'))
    websocket = JsonWebSocket('http://printer.example', '/websocket', {'X-Api-Key': 'secret'},
                              1, Redactor(), address_class='local')
    with pytest.raises(ProtocolError) as refused:
        websocket.__enter__()
    assert refused.value.reason_code == 'address_refused'

def by_field(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(row["field"]): row for row in rows}


def level(report: dict[str, object], name: str) -> dict[str, object]:
    return next(item for item in report["levels"] if item["level"] == name)  # type: ignore[index,union-attr]


def test_moonraker_probe_reports_only_transport_and_monitor() -> None:
    with VirtualPrinter("moonraker") as server:
        adapter = MoonrakerAdapter(config(server))
        report = adapter.probe()
        assert level(report, "monitor")["truth"] == "verified"
        assert level(report, "transport")["truth"] == "verified"
        assert level(report, "execute") == {
            "level": "execute",
            "truth": "assumed",
            "evidence_ref": [],
            "emulator_stage": "E3",
            "missing_level": "execute",
            "why": "physical execute requires an attended hardware pilot",
        }
        assert level(report, "safety_integration")["truth"] == "unsupported"
        assert report["max_advertised"] == "transport"
        rows = adapter.snapshot()
        assert {row["src"] for row in rows} == {"simulated"}  # loopback overrides the physical flag
        assert all(row["seq"] is None for row in rows)  # hostd's journal owns durable sequence stamping


def test_moonraker_upload_start_authorization_and_transitions() -> None:
    artifact = Artifact(b"G28\nM104 S215\n")
    with VirtualPrinter("moonraker") as server:
        adapter = MoonrakerAdapter(config(server))
        adapter.probe()
        remote = adapter.upload(artifact)
        assert remote["remote_path"] == artifact.remote_name
        start_requests = len(server.requests)
        with pytest.raises(AuthorizationRequired, match="arbiter-issued"):
            adapter.start(remote)
        assert len(server.requests) == start_requests

        adapter.start(remote, authorization_token="opaque-arbiter-token")
        assert by_field(adapter.snapshot())["device_state"]["v"] == "running"
        adapter.pause()
        paused = by_field(adapter.snapshot())
        assert paused["device_state"]["v"] == "hold"
        assert paused["job_state"]["v"] == "held"
        adapter.resume()
        assert by_field(adapter.snapshot())["job_state"]["v"] == "running"
        adapter.cancel()
        cancelled = by_field(adapter.snapshot())
        assert cancelled["device_state"]["v"] == "idle"
        assert cancelled["job_state"]["v"] == "aborted"


def test_moonraker_gap_is_unknown_and_polling_fallback_remains_live() -> None:
    with VirtualPrinter("moonraker") as server:
        adapter = MoonrakerAdapter(config(server))
        adapter.probe()
        before = by_field(adapter.snapshot())
        assert before["progress"]["state"] == "ok"
        server.telemetry_gap = True
        after = by_field(adapter.snapshot())
        for field in ("progress", "nozzle_temperature_c", "nozzle_target_c"):
            assert after[field]["state"] == "unknown"
            assert after[field]["v"] is None
            assert after[field]["why"]
            assert after[field]["last_ok"] is not None
        event = next(adapter.events(poll_interval_s=0.001))
        assert event["event"] == "snapshot"
        assert event["transport"] == "polling"


def test_moonraker_jsonrpc_websocket_subscription() -> None:
    with VirtualPrinter("moonraker") as server:
        server.moonraker_websocket = True
        adapter = MoonrakerAdapter(config(server))
        adapter.probe()
        stream = adapter.events(poll_interval_s=0.001)
        try:
            event = next(stream)
            assert event["transport"] == "websocket"
            rows = by_field(event["rows"])
            assert rows["device_state"]["v"] == "idle"
            assert rows["progress"]["v"] == 0.5
        finally:
            stream.close()


def test_octoprint_matches_moonraker_canonical_rows_and_commands() -> None:
    artifact = Artifact(b"G1 X1\n")
    with VirtualPrinter("moonraker") as moon_server, VirtualPrinter("octoprint") as octo_server:
        moon = MoonrakerAdapter(config(moon_server))
        octo = OctoPrintAdapter(config(octo_server))
        moon.probe()
        octo_report = octo.probe()
        assert level(octo_report, "monitor")["truth"] == "verified"
        moon_remote = moon.upload(artifact)
        octo_remote = octo.upload(artifact)
        moon.start(moon_remote, authorization_token="authz")
        octo.start(octo_remote, authorization_token="authz")

        moon_rows = by_field(moon.snapshot())
        octo_rows = by_field(octo.snapshot())
        assert moon_rows.keys() == octo_rows.keys()
        for field in moon_rows:
            assert moon_rows[field]["v"] == octo_rows[field]["v"]
            assert moon_rows[field]["state"] == octo_rows[field]["state"]
        octo.pause()
        assert by_field(octo.snapshot())["job_state"]["v"] == "held"
        octo.resume()
        assert by_field(octo.snapshot())["job_state"]["v"] == "running"
        octo.cancel()
        assert by_field(octo.snapshot())["job_state"]["v"] == "aborted"
        assert next(octo.events(poll_interval_s=0.001))["transport"] == "polling"


def test_prusalink_probe_marks_absent_firmware_routes_unsupported() -> None:
    with VirtualPrinter("prusalink") as server:
        adapter = PrusaLinkAdapter(config(server))
        report = adapter.probe()
        operations = report["operations"]
        assert operations["snapshot"]["truth"] == "verified"
        for command in ("upload", "start", "pause", "resume", "cancel"):
            assert operations[command]["truth"] == "unsupported"
            assert operations[command]["why"]
        assert report["raw"]["routes"]["job"] == 404
        assert report["raw"]["routes"]["storage"] == 404
        rows = by_field(adapter.snapshot())
        assert rows["device_state"]["v"] == "idle"
        assert rows["progress"]["state"] == "unknown"
        assert rows["progress"]["v"] is None


def test_prusalink_uses_only_capability_probed_routes() -> None:
    artifact = Artifact(b"G1 X2\n")
    with VirtualPrinter("prusalink") as server:
        server.prusa_storage_endpoint = True
        server.prusa_job_endpoint = True
        adapter = PrusaLinkAdapter(config(server))
        operations = adapter.probe()["operations"]
        assert operations["upload"]["truth"] == "verified"
        for command in ("start", "pause", "resume", "cancel"):
            assert operations[command]["truth"] == "assumed"
            assert operations[command]["missing_level"] == "execute"
        remote = adapter.upload(artifact)
        with pytest.raises(AuthorizationRequired):
            adapter.start(remote)
        adapter.start(remote, authorization_token="authz")
        assert by_field(adapter.snapshot())["job_state"]["v"] == "running"
        adapter.pause()
        assert by_field(adapter.snapshot())["job_state"]["v"] == "held"
        adapter.resume()
        assert by_field(adapter.snapshot())["job_state"]["v"] == "running"
        adapter.cancel()
        assert by_field(adapter.snapshot())["job_state"]["v"] == "aborted"


def test_octoprint_rejects_vendor_supplied_path_traversal() -> None:
    with VirtualPrinter("octoprint") as server:
        server.octoprint_upload_path_override = "../api/job"
        adapter = OctoPrintAdapter(config(server))
        adapter.probe()
        with pytest.raises(ProtocolError, match="unsafe remote path"):
            adapter.upload(Artifact(b"G1 X4\n"))
        assert not any(request["path"].endswith("/api/job") and request["method"] == "POST" for request in server.requests)


def test_websocket_rejects_interleaved_data_and_excessive_continuations() -> None:
    websocket = JsonWebSocket("http://localhost", "/websocket", {}, 0.1, Redactor(), address_class="loopback")
    interleaved = iter([(False, 0x1, b"{"), (True, 0x1, b"{}")])
    websocket._recv_frame = lambda: next(interleaved)  # type: ignore[method-assign]
    with pytest.raises(ProtocolError, match="interleaved"):
        websocket.recv_json()

    excessive = iter([(False, 0x1, b"{")] + [(False, 0x0, b"")] * 256)
    websocket._recv_frame = lambda: next(excessive)  # type: ignore[method-assign]
    with pytest.raises(ProtocolError, match="frame limit"):
        websocket.recv_json()


@pytest.mark.parametrize(
    ("kind", "adapter_type", "path"),
    [
        ("moonraker", MoonrakerAdapter, "/printer/info"),
        ("octoprint", OctoPrintAdapter, "/api/version"),
        ("prusalink", PrusaLinkAdapter, "/api/version"),
    ],
)
def test_malformed_json_is_refused(kind: str, adapter_type: type, path: str) -> None:
    with VirtualPrinter(kind) as server:
        server.malformed_paths.add(path)
        with pytest.raises(ProtocolError, match="malformed JSON"):
            adapter_type(config(server)).probe()


def test_html_error_response_is_refused() -> None:
    with VirtualPrinter("moonraker") as server:
        server.html_error_paths.add("/printer/info")
        with pytest.raises(ProtocolError, match=r"status 500.*content-type=text/html"):
            MoonrakerAdapter(config(server)).probe()


def test_secrets_never_appear_in_rows_events_reports_errors_or_logs(caplog: pytest.LogCaptureFixture) -> None:
    secret = "not-for-output-9fd94f"
    caplog.set_level(logging.DEBUG)
    with VirtualPrinter("moonraker", api_key=secret) as server:
        adapter = MoonrakerAdapter(config(server, AuthConfig("api_key", api_key=secret)))
        report = adapter.probe()
        rows = adapter.snapshot()
        event = next(adapter.events(poll_interval_s=0.001))
        rendered = json.dumps({"report": report, "rows": rows, "event": event}, sort_keys=True)
        assert secret not in rendered
        assert secret not in "\n".join(server.log_lines)
        assert secret not in "\n".join(record.getMessage() for record in caplog.records)

    with pytest.raises(ConfigurationError):
        AuthConfig("trusted_host", api_key=secret)
