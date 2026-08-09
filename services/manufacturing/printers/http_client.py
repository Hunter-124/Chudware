"""Small strict HTTP client for local printer APIs, using only the stdlib."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import json
import logging
import secrets
import socket
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import (
    HTTPBasicAuthHandler,
    HTTPDigestAuthHandler,
    HTTPPasswordMgrWithDefaultRealm,
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from .model import AuthConfig, ProtocolError, Redactor

_LOG = logging.getLogger(__name__)
_MAX_ERROR_BYTES = 512
_MAX_RESPONSE_BYTES = 1 << 20


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    @property
    def content_type(self) -> str:
        return self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _transport_reason(error: BaseException) -> str:
    reason = error.reason if isinstance(error, URLError) else error
    text = str(reason).lower()
    if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in text:
        return "timeout"
    if (
        isinstance(reason, ConnectionRefusedError)
        or getattr(reason, "errno", None) == errno.ECONNREFUSED
        or "connection refused" in text
    ):
        return "connect_refused"
    return "transport_error"


def _read_bounded(stream: Any) -> bytes:
    body = stream.read(_MAX_RESPONSE_BYTES + 1)
    if len(body) > _MAX_RESPONSE_BYTES:
        raise ProtocolError(
            f"printer HTTP response exceeded {_MAX_RESPONSE_BYTES} bytes",
            reason_code="response_too_large",
        )
    return body


class HttpClient:
    def __init__(self, base_url: str, auth: AuthConfig, timeout_s: float, api_key_header: str = "X-Api-Key") -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.auth = auth
        self.timeout_s = timeout_s
        self.api_key_header = api_key_header
        self.redactor = Redactor(auth.secret_values())
        if auth.mode == "digest":
            passwords = HTTPPasswordMgrWithDefaultRealm()
            passwords.add_password(None, self.base_url, auth.username or "", auth.password or "")
            self._opener = build_opener(
                _NoRedirect(), HTTPDigestAuthHandler(passwords), HTTPBasicAuthHandler(passwords)
            )
        else:
            self._opener = build_opener(_NoRedirect())

    def auth_headers(self) -> dict[str, str]:
        if self.auth.mode == "api_key":
            return {self.api_key_header: self.auth.api_key or ""}
        return {}

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        expected: Iterable[int] = (200,),
    ) -> HttpResponse:
        relative = path.lstrip("/")
        url = urljoin(self.base_url, relative)
        merged = {"Accept": "application/json", **self.auth_headers(), **dict(headers or {})}
        request = Request(url, data=body, headers=merged, method=method.upper())
        allowed = frozenset(expected)
        try:
            with self._opener.open(request, timeout=self.timeout_s) as response:
                result = HttpResponse(response.status, dict(response.headers.items()), _read_bounded(response))
        except HTTPError as exc:
            result = HttpResponse(exc.code, dict(exc.headers.items()), _read_bounded(exc))
        except (URLError, TimeoutError, OSError) as exc:
            raise ProtocolError(
                f"printer HTTP transport failed: {self.redactor.text(exc)}",
                reason_code=_transport_reason(exc),
            ) from exc
        _LOG.debug("printer HTTP %s %s -> %d", method.upper(), "/" + relative.split("?", 1)[0], result.status)
        if result.status not in allowed:
            excerpt = self.redactor.text(result.body[:_MAX_ERROR_BYTES].decode("utf-8", errors="replace"))
            raise ProtocolError(
                f"printer HTTP {method.upper()} refused with status {result.status}; "
                f"content-type={result.content_type or 'missing'}; body={excerpt!r}",
                reason_code=f"http_{result.status}",
            )
        return result

    def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Any | None = None,
        expected: Iterable[int] = (200,),
        allow_empty: bool = False,
    ) -> Any:
        body = None
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        response = self.request(method, path, body=body, headers=headers, expected=expected)
        if allow_empty and not response.body:
            return {}
        return self.decode_json(response, method=method)

    def decode_json(self, response: HttpResponse, *, method: str) -> Any:
        if response.content_type != "application/json":
            raise ProtocolError(
                f"printer HTTP {method.upper()} returned non-JSON content-type "
                f"{response.content_type or 'missing'}",
                reason_code="malformed_body",
            )
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(
                f"printer HTTP {method.upper()} returned malformed JSON",
                reason_code="malformed_body",
            ) from exc
        if not isinstance(value, (dict, list)):
            raise ProtocolError(
                f"printer HTTP {method.upper()} JSON root must be an object or array",
                reason_code="malformed_body",
            )
        return self.redactor.value(value)

    def multipart(
        self,
        path: str,
        *,
        fields: Mapping[str, str],
        file_field: str,
        filename: str,
        data: bytes,
        expected: Iterable[int],
    ) -> HttpResponse:
        boundary = "chudware-" + secrets.token_hex(16)
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode("ascii"),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )
        safe_filename = filename.replace('"', "_").replace("\r", "_").replace("\n", "_")
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{file_field}"; filename="{safe_filename}"\r\n'.encode("utf-8"),
                b"Content-Type: application/octet-stream\r\n\r\n",
                data,
                b"\r\n",
                f"--{boundary}--\r\n".encode("ascii"),
            ]
        )
        return self.request(
            "POST",
            path,
            body=b"".join(chunks),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            expected=expected,
        )
