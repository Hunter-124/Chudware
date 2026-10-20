"""Connection-bound public HTTP, without environment proxies.

Every hop resolves once, rejects *all* unsafe answers, and connects to a numeric
sockaddr from that snapshot. TLS still verifies the original hostname. Wire and
decoded bytes are independently bounded; timeouts are idle, not run lifetimes.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urljoin, urlsplit


class OutboundPolicyError(ValueError):
    pass


class ResponseTooLarge(ValueError):
    pass


class HTTPStatusError(RuntimeError):
    def __init__(self, response):
        self.response = response
        super().__init__(f"HTTP {response.status_code} at {response.url}")


def public_address(value: str) -> bool:
    try:
        if "%" in value:
            return False
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address):
            if address.ipv4_mapped:
                address = address.ipv4_mapped
            elif address.sixtofour or address.teredo:
                return False
        return (address.is_global and not address.is_multicast and not address.is_reserved
                and not address.is_unspecified and not address.is_loopback
                and not address.is_link_local)
    except ValueError:
        return False


def _parse_url(url: str, schemes):
    try:
        parsed = urlsplit(url)
        port = parsed.port
        host = parsed.hostname
    except (ValueError, TypeError) as exc:
        raise OutboundPolicyError("invalid URL") from exc
    if (parsed.scheme not in schemes or not host or parsed.username is not None
            or parsed.password is not None or any(ord(c) < 33 for c in url)
            or "%" in host or "\\" in url or port == 0):
        raise OutboundPolicyError("requires a credential-free public HTTP(S) URL")
    return parsed


def _cell_mode():
    from services.agent.request_context import in_cell
    return in_cell()


def resolve_public(url: str, *, schemes=("http", "https")):
    parsed = _parse_url(url, schemes)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not public_address(str(literal)):
        raise OutboundPolicyError("destination address is not public")
    if _cell_mode():
        # The fixed broker owns DNS and address pinning in the network namespace.
        # No environment-selected proxy, no local DNS, and no direct fallback.
        if parsed.scheme != "https" or port != 443:
            raise OutboundPolicyError("cell egress requires HTTPS on port 443")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None:
            raise OutboundPolicyError("cell egress requires an allowlisted hostname")
        return parsed, []
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM,
                                     proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise OutboundPolicyError("destination DNS resolution failed") from exc
    if not answers or any(family not in (socket.AF_INET, socket.AF_INET6)
                          or not public_address(sockaddr[0])
                          for family, _, _, _, sockaddr in answers):
        raise OutboundPolicyError("destination DNS includes a non-public address")
    return parsed, answers


def validate_public_url(url: str, *, schemes=("http", "https")) -> None:
    resolve_public(url, schemes=schemes)


def _connect(address, timeout):
    family, kind, proto, _, sockaddr = address
    sock = socket.socket(family, kind, proto)
    try:
        sock.settimeout(timeout)
        sock.connect(sockaddr)
        return sock
    except BaseException:
        sock.close()
        raise


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(self, parsed, address, timeout):
        super().__init__(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                         timeout=timeout)
        self._parsed = parsed
        self._address = address

    def connect(self):
        if self._address is None:
            self.sock = _connect((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP,
                                  "", ("127.0.0.1", 18080)), self.timeout)
            try:
                self.sock.sendall(f"CONNECT {self.host}:{self.port} HTTP/1.1\r\n"
                                  f"Host: {self.host}:{self.port}\r\n\r\n".encode("ascii"))
                response = http.client.HTTPResponse(self.sock)
                response.begin()
                status = response.status
                response.close()
                if status != 200:
                    raise OutboundPolicyError(f"cell egress refused destination (HTTP {status})")
            except BaseException:
                self.sock.close()
                raise
        else:
            self.sock = _connect(self._address, self.timeout)
        if self._parsed.scheme == "https":
            try:
                self.sock = ssl.create_default_context().wrap_socket(
                    self.sock, server_hostname=self.host)
            except BaseException:
                self.sock.close()
                raise


@dataclass
class Response:
    status_code: int
    headers: dict[str, str]
    url: str
    content: bytes = b""
    bytes_written: int = 0

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise HTTPStatusError(self)


def _body(response, output, max_bytes, max_wire_bytes):
    declared = response.getheader("Content-Length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError as exc:
            raise OutboundPolicyError("invalid Content-Length") from exc
        if length < 0 or length > max_wire_bytes:
            raise ResponseTooLarge("wire response exceeds byte budget")
    encoding = (response.getheader("Content-Encoding") or "identity").strip().lower()
    if encoding not in ("identity", "gzip", "deflate"):
        raise OutboundPolicyError("unsupported Content-Encoding")
    decoder = None if encoding == "identity" else zlib.decompressobj(
        16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
    wire = decoded = 0
    while True:
        chunk = response.read1(min(65536, max_wire_bytes - wire + 1))
        if not chunk:
            break
        wire += len(chunk)
        if wire > max_wire_bytes:
            raise ResponseTooLarge("wire response exceeds byte budget")
        try:
            data = decoder.decompress(chunk, max_bytes - decoded + 1) if decoder else chunk
        except zlib.error as exc:
            raise OutboundPolicyError("invalid compressed response") from exc
        decoded += len(data)
        if decoded > max_bytes or (decoder and decoder.unconsumed_tail):
            raise ResponseTooLarge("decoded response exceeds byte budget")
        output.write(data)
    if declared is not None and wire != length:
        raise OutboundPolicyError("truncated response")
    if decoder and (not decoder.eof or decoder.unused_data):
        raise OutboundPolicyError("incomplete or concatenated compressed response")
    return decoded


def request(url: str, *, method="GET", headers: Mapping[str, str] | None = None,
            body: bytes | None = None, max_bytes=8 * 1024 * 1024,
            max_wire_bytes: int | None = None, idle_timeout=15.0, max_redirects=10,
            schemes=("http", "https"), destination: Path | None = None,
            headers_for_url: Callable[[str], Mapping[str, str]] | None = None,
            before_hop: Callable[[int, str], None] | None = None) -> Response:
    import io
    if max_bytes <= 0 or idle_timeout <= 0:
        raise ValueError("positive byte and idle budgets required")
    max_wire_bytes = max_bytes if max_wire_bytes is None else max_wire_bytes
    current = url
    base_headers = dict(headers or {})
    for hop in range(max_redirects + 1):
        if before_hop:
            before_hop(hop, current)
        parsed, addresses = resolve_public(current, schemes=schemes)
        # No library connection pool or hostname-based reconnect: retries (if a
        # caller elects to retry) must pass through resolution/policy again.
        conn = _PinnedConnection(parsed, addresses[0] if addresses else None, idle_timeout)
        hop_headers = {k: v for k, v in base_headers.items()
                       if k.lower() not in ("host", "connection", "accept-encoding")}
        hop_headers.update({"Accept-Encoding": "gzip, deflate", "Connection": "close"})
        if headers_for_url:
            hop_headers.update(headers_for_url(current))
        try:
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            conn.request(method, target, body=body, headers=hop_headers)
            raw = conn.getresponse()
            response = Response(raw.status, {k.lower(): v for k, v in raw.getheaders()}, current)
            if raw.status in (301, 302, 303, 307, 308):
                location = raw.getheader("Location")
                if not location:
                    raise OutboundPolicyError("redirect omitted Location")
                next_url = urljoin(current, location)
                if urlsplit(next_url).netloc != parsed.netloc:
                    base_headers = {k: v for k, v in base_headers.items()
                                    if k.lower() not in ("authorization", "cookie", "referer")}
                if raw.status == 303 or (raw.status in (301, 302) and method == "POST"):
                    method, body = "GET", None
                current = next_url
                continue
            if destination is None:
                output = io.BytesIO()
                response.bytes_written = _body(raw, output, max_bytes, max_wire_bytes)
                response.content = output.getvalue()
            elif raw.status < 400:
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with destination.open("wb") as output:
                        response.bytes_written = _body(raw, output, max_bytes, max_wire_bytes)
                except BaseException:
                    destination.unlink(missing_ok=True)
                    raise
            else:
                # Error bodies are bounded too; never write one over an artifact.
                output = io.BytesIO()
                response.bytes_written = _body(raw, output, min(max_bytes, 65536),
                                               min(max_wire_bytes, 65536))
                response.content = output.getvalue()
            return response
        except http.client.HTTPException as exc:
            raise OSError("invalid HTTP response") from exc
        finally:
            conn.close()
    raise OutboundPolicyError("too many redirects")


def get(url: str, **kwargs) -> Response:
    return request(url, **kwargs)


def download(url: str, destination: Path, **kwargs) -> Response:
    return request(url, destination=Path(destination), **kwargs)


def safe_download_path(directory: Path, url: str, *, default="download.bin",
                       filename: str | None = None) -> Path:
    """Choose one confined, portable filename; URL query/fragment bytes are never names.

    An explicit catalog/Content-Disposition name is subject to the same rules as
    the decoded URL leaf, even when the caller has added a site-ID prefix.
    """
    from urllib.parse import unquote
    from pathlib import PureWindowsPath

    def checked_name(value: str) -> str:
        if (not isinstance(value, str) or not value or value in (".", "..")
                or any(c in value for c in '/\\:<>|?*')
                or any(ord(c) < 32 or ord(c) == 127 for c in value)
                or PureWindowsPath(value).drive or value.rstrip(". ") != value):
            raise OutboundPolicyError("unsafe download filename")
        # Windows reserves these names with every extension, on every platform.
        stem = value.split(".", 1)[0].rstrip(" ").upper()
        if (stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                or (len(stem) == 4 and stem[:3] in {"COM", "LPT"}
                    and stem[3] in "123456789¹²³")):
            raise OutboundPolicyError("unsafe download filename")
        return value

    try:
        if any(ord(c) < 32 or ord(c) == 127 for c in url):
            raise OutboundPolicyError("unsafe download filename")
        leaf = unquote(urlsplit(url).path.rsplit("/", 1)[-1], errors="strict")
        name = checked_name(leaf or default)
        if not leaf:
            checked_name(unquote(name, errors="strict"))
        if filename is not None:
            # Validate encoded separators as well as the literal catalog name;
            # preserve safe percent signs, Unicode and the selected extension.
            checked_name(unquote(filename, errors="strict"))
            name = checked_name(filename)
    except (UnicodeError, ValueError) as exc:
        raise OutboundPolicyError("unsafe download filename") from exc
    root = directory.resolve()
    result = root / name
    if result.resolve().parent != root:
        raise OutboundPolicyError("download destination escapes directory")
    return result
