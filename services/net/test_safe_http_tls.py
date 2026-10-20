"""Real, disposable TLS handshakes through the public-address-pinned transport.

Only the private test connector maps approved fixture IPs onto loopback. The
production resolver, connection class, TLS verification and CAD consumers run
unchanged; no browser, public network or shared credential profile is used.
"""
from __future__ import annotations

import hashlib
import socket
import ssl
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from services.net import safe_http as http


STEP = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
PUBLIC_IPS = {"public.fixture": "93.184.216.34", "downloads.fixture": "1.1.1.1"}


def _certificate(subject, key, issuer, issuer_key, *, ca=False, names=()):
    now = datetime.now(timezone.utc)
    builder = (x509.CertificateBuilder()
               .subject_name(subject).issuer_name(issuer)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - timedelta(minutes=5))
               .not_valid_after(now + timedelta(days=1))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=0 if ca else None),
                              critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                              critical=False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                   issuer_key.public_key()), critical=False)
               .add_extension(x509.KeyUsage(
                   digital_signature=True, content_commitment=False,
                   key_encipherment=not ca, data_encipherment=False, key_agreement=False,
                   key_cert_sign=ca, crl_sign=ca, encipher_only=False, decipher_only=False),
                   critical=True))
    if not ca:
        builder = (builder.add_extension(x509.SubjectAlternativeName(
            [x509.DNSName(name) for name in names]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False))
    return builder.sign(issuer_key, hashes.SHA256())


@pytest.fixture(scope="module")
def tls_pki():
    roots = {}
    for label in ("trusted", "untrusted"):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Fixture {label} CA")])
        roots[label] = (key, _certificate(subject, key, subject, key, ca=True))
    with tempfile.TemporaryDirectory(prefix="chudware-tls-fixture-") as directory:
        root = Path(directory)
        certificates = {}
        for mode in ("valid", "first_hostname_only", "wrong_hostname", "untrusted"):
            issuer_key, issuer = roots["untrusted" if mode == "untrusted" else "trusted"]
            names = (("wrong.fixture",) if mode == "wrong_hostname" else
                     ("public.fixture",) if mode == "first_hostname_only" else tuple(PUBLIC_IPS))
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
            cert = _certificate(subject, key, issuer.subject, issuer_key, names=names)
            cert_path, key_path = root / f"{mode}.pem", root / f"{mode}.key"
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()))
            key_path.chmod(0o600)
            certificates[mode] = (cert_path, key_path)
        yield roots["trusted"][1].public_bytes(serialization.Encoding.PEM), certificates


@pytest.fixture
def tls_fixture(request, monkeypatch, tls_pki):
    trusted_root, certificates = tls_pki
    mode = getattr(request, "param", "valid")
    seen, server_names, connections, resolutions = [], [], [], []
    addresses = dict(PUBLIC_IPS)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            seen.append((self.headers.get("Host"), self.path))
            if self.path in {"/redirect.step", "/private.step"}:
                host = "127.0.0.1" if self.path == "/private.step" else "downloads.fixture"
                self.send_response(302)
                self.send_header("Location", f"https://{host}:{self.server.server_port}/model.step")
                payload = b""
            else:
                self.send_response(200)
                if self.path == "/static":
                    payload = (b'<meta name="license" content="CC-BY-4.0">'
                               b'<a href="/model.stl">STL</a><a href="/model.step">STEP</a>')
                elif self.path == "/shell":
                    payload = (b'<div id="app">JavaScript required</div>'
                               b'<script src="http://127.0.0.1:1/private.js"></script>')
                else:
                    payload = STEP
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    cert_path, key_path = certificates[mode]
    server_context.load_cert_chain(cert_path, key_path)
    server_context.set_servername_callback(lambda _sock, name, _ctx: server_names.append(name))
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original_connect = http._connect
    client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_context.load_verify_locations(cadata=trusted_root.decode("ascii"))
    assert client_context.check_hostname
    assert client_context.verify_mode == ssl.CERT_REQUIRED

    def resolve(host, port, **_):
        # Unknown names cannot escape the fixture via real DNS.
        assert host in addresses
        assert port == server.server_port
        resolutions.append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
                 (addresses[host], port))]

    def connect(address, timeout):
        # Fail before opening any socket unless this is one approved numeric pin.
        assert address[4][0] in PUBLIC_IPS.values()
        assert address[4][1] == server.server_port
        connections.append(address[4])
        return original_connect((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                                 "", ("127.0.0.1", server.server_port)), timeout)

    monkeypatch.setattr(http.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(http, "_connect", connect)
    monkeypatch.setattr(http, "_cell_mode", lambda: False)
    # Substitute only the trust store, not TLS: wrap_socket performs a real verified handshake.
    monkeypatch.setattr(http.ssl, "create_default_context", lambda: client_context)
    try:
        yield SimpleNamespace(url=f"https://public.fixture:{server.server_port}",
                              port=server.server_port, seen=seen, server_names=server_names,
                              connections=connections, resolutions=resolutions, addresses=addresses)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_pinned_tls_download_verifies_hostname_and_exact_payload(tls_fixture, tmp_path):
    fixture = tls_fixture
    destination = tmp_path / "model.step"
    response = http.download(fixture.url + "/model.step", destination,
                             schemes=("https",), idle_timeout=3)
    assert response.status_code == 200
    assert response.bytes_written == len(STEP)
    assert destination.read_bytes() == STEP
    assert fixture.server_names == ["public.fixture"]
    assert fixture.seen == [(f"public.fixture:{fixture.port}", "/model.step")]
    assert fixture.resolutions == [("public.fixture", fixture.port)]
    assert fixture.connections == [(PUBLIC_IPS["public.fixture"], fixture.port)]


@pytest.mark.parametrize("tls_fixture", ["wrong_hostname", "untrusted"], indirect=True)
@pytest.mark.parametrize("consumer", ["transport", "cad"])
def test_invalid_tls_cannot_publish_cad(tls_fixture, tmp_path, monkeypatch, consumer):
    fixture = tls_fixture
    url = fixture.url + "/model.step"
    if consumer == "transport":
        with pytest.raises(ssl.SSLCertVerificationError):
            http.download(url, tmp_path / "model.step", schemes=("https",), idle_timeout=3)
    else:
        from services.mcp.parts_tools import fetch_cad_model_impl
        monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
        monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)
        result = fetch_cad_model_impl(url, import_part=False)
        assert result["ok"] is False
        assert "artifact_path" not in result
    assert fixture.server_names == ["public.fixture"]
    assert fixture.seen == []  # TLS fails before the server accepts any HTTP/CAD request.
    assert not any(path.is_file() for path in tmp_path.rglob("*"))


def test_tls_redirect_revalidates_and_pins_each_hostname(tls_fixture, tmp_path):
    fixture = tls_fixture
    destination = tmp_path / "model.step"
    response = http.download(fixture.url + "/redirect.step", destination,
                             schemes=("https",), idle_timeout=3)
    assert response.url == f"https://downloads.fixture:{fixture.port}/model.step"
    assert destination.read_bytes() == STEP
    assert fixture.seen == [(f"public.fixture:{fixture.port}", "/redirect.step"),
                            (f"downloads.fixture:{fixture.port}", "/model.step")]
    assert fixture.server_names == ["public.fixture", "downloads.fixture"]
    assert fixture.resolutions == [("public.fixture", fixture.port),
                                   ("downloads.fixture", fixture.port)]
    assert fixture.connections == [(PUBLIC_IPS["public.fixture"], fixture.port),
                                   (PUBLIC_IPS["downloads.fixture"], fixture.port)]


@pytest.mark.parametrize("tls_fixture", ["first_hostname_only"], indirect=True)
def test_tls_redirect_verifies_the_new_hostname_before_accepting_cad(tls_fixture, tmp_path):
    fixture = tls_fixture
    destination = tmp_path / "model.step"
    with pytest.raises(ssl.SSLCertVerificationError):
        http.download(fixture.url + "/redirect.step", destination,
                      schemes=("https",), idle_timeout=3)
    assert not destination.exists()
    assert fixture.seen == [(f"public.fixture:{fixture.port}", "/redirect.step")]
    assert fixture.server_names == ["public.fixture", "downloads.fixture"]
    assert fixture.resolutions == [("public.fixture", fixture.port),
                                   ("downloads.fixture", fixture.port)]
    assert fixture.connections == [(PUBLIC_IPS["public.fixture"], fixture.port),
                                   (PUBLIC_IPS["downloads.fixture"], fixture.port)]


@pytest.mark.parametrize("redirect", ["private_literal", "private_dns"])
def test_tls_redirect_to_private_destination_publishes_nothing(tls_fixture, tmp_path, redirect):
    fixture = tls_fixture
    route = "/private.step" if redirect == "private_literal" else "/redirect.step"
    if redirect == "private_dns":
        fixture.addresses["downloads.fixture"] = "127.0.0.1"
    destination = tmp_path / "model.step"
    with pytest.raises(http.OutboundPolicyError):
        http.download(fixture.url + route, destination, schemes=("https",), idle_timeout=3)
    assert not destination.exists()
    assert fixture.seen == [(f"public.fixture:{fixture.port}", route)]
    assert fixture.server_names == ["public.fixture"]
    assert fixture.connections == [(PUBLIC_IPS["public.fixture"], fixture.port)]


def test_static_https_cad_page_preserves_safe_download(tls_fixture, tmp_path, monkeypatch):
    from services.mcp.parts_tools import fetch_cad_model_impl
    fixture = tls_fixture
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)
    result = fetch_cad_model_impl(fixture.url + "/static", import_part=False)
    assert result["ok"] is True
    assert Path(result["artifact_path"]).read_bytes() == STEP
    assert result["provenance"]["sha256"] == hashlib.sha256(STEP).hexdigest()
    assert result["provenance"]["license"] == "CC-BY-4.0"
    assert fixture.seen == [(f"public.fixture:{fixture.port}", "/static"),
                            (f"public.fixture:{fixture.port}", "/model.step")]
    assert fixture.server_names == ["public.fixture", "public.fixture"]


def test_unknown_rendered_cad_page_refuses_with_manual_download_guidance(
        tls_fixture, tmp_path, monkeypatch):
    from services.browser import session
    from services.mcp.parts_tools import fetch_cad_model_impl
    fixture = tls_fixture
    monkeypatch.setenv("CHUDWARE_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("CHUDWARE_FS_ROOT", raising=False)
    # A safety tripwire, not a rendered response: this test must never launch a browser.
    monkeypatch.setattr(session, "browser_fetch",
                        lambda *_args, **_kwargs: pytest.fail("unrestricted rendering attempted"))
    url = fixture.url + "/shell"
    result = fetch_cad_model_impl(url, import_part=False)
    assert result["ok"] is False and result["reason"] == "no_cad_link"
    assert result["candidates"] == [url]
    assert url in result["detail"]
    assert "static CAD download link" in result["detail"]
    assert "not supported" in result["detail"]
    assert "manually" in result["detail"] and "drop" in result["detail"]
    assert "direct public HTTPS CAD URL" in result["detail"]
    assert "artifact_path" not in result
    assert fixture.seen == [(f"public.fixture:{fixture.port}", "/shell")]
    assert fixture.server_names == ["public.fixture"]
    assert not any(path.is_file() for path in tmp_path.rglob("*"))
