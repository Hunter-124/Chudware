"""Gateway-owned BYOK authority, served only on identity-bound cell Unix sockets.

The ASGI application is deliberately not a router in the public gateway. Provider
wire bytes pass through unchanged; credentials are resolved only on this host.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
import socket
import stat
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import HTTPException
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse

try:
    from . import accounts, authn, cells, orgs, provider_keys
    from .config import settings
    from .db import tx
    from .plans import limits_for
except ImportError:
    import accounts, authn, cells, orgs, provider_keys
    from config import settings
    from db import tx
    from plans import limits_for

SUPPORTED_PROVIDERS = frozenset({"anthropic", "openai", "xai", "openrouter", "gemini"})
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024  # Non-SSE only; streams never accumulate here.
MAX_HEADER_BYTES = 32 * 1024
MAX_HEADER_COUNT = 64
MAX_ACTIVE_REQUESTS = 64
MAX_ACTOR_REQUESTS = 4
MAX_CELL_LISTENERS = 1024
_GRANT_DOMAIN = b"chudware-cell-provider-grant-v1\0"
_CLAIM_KEYS = frozenset({"v", "cell", "workspace", "actor", "uid", "epoch", "right", "team",
                         "provider", "source", "role"})


class BrokerRefusal(Exception):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _grant_key() -> bytes:
    return hmac.digest(provider_keys._key_bytes(), _GRANT_DOMAIN + b"mac-key", "sha256")


def _live_identity(claims: dict, *, resolve_key: bool = False):
    """Resolve current authority, including role/epoch/deletion, not cached admission."""
    try:
        with tx() as cur:
            cur.execute("SELECT email,status,role FROM accounts WHERE uid=%s", (claims["uid"],))
            row = cur.fetchone()
        if not row or row["status"] != "active" or row["role"] != claims["role"]:
            raise BrokerRefusal(401, "provider_grant_revoked")
        email = row["email"]
        current = cells.resolve_workspace(email, claims["team"])
        actual = (current.cell_id, current.workspace_key, current.actor_key, current.account_uid,
                  current.epoch, current.right)
        expected = tuple(claims[k] for k in ("cell", "workspace", "actor", "uid", "epoch", "right"))
        if actual != expected or "run" not in cells.RIGHTS.get(current.right, set()):
            raise BrokerRefusal(401, "provider_grant_revoked")
        account = accounts.get_account(email)
        university = bool(current.team_id and current.team_id == settings.university_team_id
                          and orgs.university_eligible(email))
        if (not limits_for(account.plan).cloud or
                (account.plan == "trial" and authn.trial_expired(email))) and not university:
            raise BrokerRefusal(401, "provider_grant_revoked")
        if resolve_key:
            key = provider_keys.read_validated_key(email, claims["provider"])
            if not key:
                raise BrokerRefusal(401, "provider_grant_revoked")
            return key
        if not provider_keys.has_validated_key(email, claims["provider"]):
            raise BrokerRefusal(401, "provider_grant_revoked")
    except BrokerRefusal:
        raise
    except (HTTPException, ValueError):
        raise BrokerRefusal(401, "provider_grant_revoked") from None


def mint_grant(email: str, principal: cells.CellPrincipal, provider: str) -> str:
    """Same live identity tuple produces the same token, independent of key rotation."""
    if provider not in SUPPORTED_PROVIDERS:
        raise BrokerRefusal(403, "credential_isolation_unsupported")
    account = accounts.get_account(email)
    if not account or account.uid != principal.account_uid:
        raise BrokerRefusal(401, "provider_grant_revoked")
    claims = {"v": 1, "cell": principal.cell_id, "workspace": principal.workspace_key,
              "actor": principal.actor_key, "uid": principal.account_uid, "epoch": principal.epoch,
              "right": principal.right, "team": principal.team_id, "provider": provider,
              "source": "byok", "role": account.role}
    _live_identity(claims)
    raw = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "cpg1." + _b64(raw) + "." + _b64(hmac.digest(_grant_key(), _GRANT_DOMAIN + raw, "sha256"))


def _decode_grant(token: str, cell_id: str, workspace_key: str, provider: str) -> dict:
    try:
        if not isinstance(token, str) or len(token) > 4096:
            raise ValueError
        prefix, payload, signature = token.split(".")
        raw = base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
        signed = base64.b64decode(signature + "=" * (-len(signature) % 4), altchars=b"-_", validate=True)
        if (prefix != "cpg1" or _b64(raw) != payload or _b64(signed) != signature
                or not hmac.compare_digest(
                    signed, hmac.digest(_grant_key(), _GRANT_DOMAIN + raw, "sha256"))):
            raise ValueError
        claims = json.loads(raw)
        if (not isinstance(claims, dict) or claims.keys() != _CLAIM_KEYS
                or type(claims["v"]) is not int or claims["v"] != 1
                or type(claims["epoch"]) is not int or claims["epoch"] < 0
                or any(not isinstance(claims[k], str) for k in _CLAIM_KEYS - {"v", "epoch"})
                or claims["cell"] != cell_id or claims["workspace"] != workspace_key
                or claims["provider"] != provider or claims["source"] != "byok"):
            raise ValueError
        return claims
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise BrokerRefusal(401, "invalid_provider_grant") from None


def _route(method: str, path: str, query: bytes) -> tuple[str, str]:
    # No arbitrary URL, encoded slash, traversal, query credential, or redirect lane.
    if len(path) > 512 or len(query) > 512 or "%" in path or "\\" in path or ".." in path:
        raise BrokerRefusal(404, "provider_route_refused")
    parts = path.split("/", 2)
    provider = parts[1] if len(parts) == 3 else ""
    tail = "/" + parts[2] if len(parts) == 3 else ""
    if provider not in SUPPORTED_PROVIDERS:
        raise BrokerRefusal(404, "provider_route_refused")
    allowed = False
    if provider == "gemini":
        if method == "GET" and (tail == "/models" or re.fullmatch(r"/models/[A-Za-z0-9._-]+", tail)):
            allowed = not query
        elif method == "POST" and re.fullmatch(r"/models/[A-Za-z0-9._-]+:streamGenerateContent", tail):
            allowed = query == b"alt=sse"
        elif method == "POST" and re.fullmatch(r"/models/[A-Za-z0-9._-]+:generateContent", tail):
            allowed = not query
        tail = "/v1beta" + tail
    else:
        if method == "GET" and (tail == "/v1/models" or re.fullmatch(r"/v1/models/[A-Za-z0-9._-]+", tail)):
            allowed = not query
            if provider == "openrouter" and tail == "/v1/models":
                allowed = query in (b"", b"output_modalities=decisions", b"output_modalities=rerank")
        if provider == "openrouter" and method == "GET" and tail in {
                "/v1/images/models", "/v1/embeddings/models", "/v1/videos/models"}:
            allowed = not query
        post_paths = {"/v1/messages", "/v1/messages/count_tokens"} if provider == "anthropic" else {
            "/v1/responses", "/v1/chat/completions"}
        if method == "POST" and tail in post_paths:
            # The pinned runtime's builtin Anthropic search uses this exact
            # vendor SDK query. It is not a general query or credential lane.
            allowed = not query or (provider == "anthropic" and tail == "/v1/messages"
                                    and query == b"beta=true")
        if provider == "openrouter":
            tail = "/api" + tail
    if not allowed:
        raise BrokerRefusal(404, "provider_route_refused")
    return provider, tail


def _request_headers(raw: list[tuple[bytes, bytes]]) -> tuple[str, dict[str, str]]:
    if len(raw) > MAX_HEADER_COUNT or sum(len(k) + len(v) for k, v in raw) > MAX_HEADER_BYTES:
        raise BrokerRefusal(431, "provider_headers_too_large")
    tokens = []
    headers = {"accept-encoding": "identity", "user-agent": "chudware-provider-broker/1"}
    allowed = {"accept", "content-type", "anthropic-version", "anthropic-beta", "openai-beta"}
    seen = set()
    for name, value in raw:
        key = name.decode("ascii").lower()
        if key in seen:
            raise BrokerRefusal(400, "duplicate_provider_header")
        seen.add(key)
        text = value.decode("latin-1")
        if key == "authorization":
            if not text.startswith("Bearer "):
                raise BrokerRefusal(401, "invalid_provider_grant")
            tokens.append(text[7:])
        elif key in {"x-api-key", "x-goog-api-key"}:
            tokens.append(text)
        elif key in allowed:
            headers[key] = text
    if len(tokens) != 1:
        raise BrokerRefusal(401, "invalid_provider_grant")
    return tokens[0], headers


class _OwnedServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        # This embedded listener is not the process owner; the public gateway is.
        yield


@dataclass
class _Listener:
    workspace_key: str
    path: Path
    inode: int
    server: _OwnedServer
    task: asyncio.Task


class ProviderBroker:
    def __init__(self, *, upstream_origins: dict[str, str] | None = None):
        self.origins = {name: str(httpx.URL(spec.url).copy_with(path="/"))[:-1]
                        for name, spec in provider_keys.VALIDATION_SPECS.items()
                        if name in SUPPORTED_PROVIDERS}
        for provider, origin in (upstream_origins or {}).items():
            parsed = urlsplit(origin)
            if (provider not in SUPPORTED_PROVIDERS or parsed.scheme != "http"
                    or parsed.hostname not in {"127.0.0.1", "::1"} or parsed.port is None
                    or parsed.username or parsed.password or parsed.path not in {"", "/"}
                    or parsed.query or parsed.fragment):
                raise ValueError("fixture upstream must be a fixed loopback HTTP origin")
            self.origins[provider] = origin.rstrip("/")
        self.client = httpx.AsyncClient(trust_env=False, follow_redirects=False,
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=())),
            timeout=httpx.Timeout(connect=15, read=None, write=30, pool=5),
            limits=httpx.Limits(max_connections=MAX_ACTIVE_REQUESTS, max_keepalive_connections=16))
        self.listeners: dict[str, _Listener] = {}
        self.lock = asyncio.Lock()
        self.active = 0
        self.actors: dict[tuple[str, str], int] = {}
        self.transfers: set[asyncio.Task] = set()
        self.closed = False
        self.owner_loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        current = asyncio.get_running_loop()
        if self.closed or (self.owner_loop is not None and self.owner_loop is not current):
            raise HTTPException(503, "provider broker lifecycle unavailable")
        self.owner_loop = current

    def mint_grant(self, email, principal, provider):
        return mint_grant(email, principal, provider)

    async def ensure_cell(self, principal: cells.CellPrincipal, directory: Path) -> None:
        if self.owner_loop is None:
            await self.start()
        if asyncio.get_running_loop() is not self.owner_loop:
            if self.owner_loop.is_closed() or not self.owner_loop.is_running():
                raise HTTPException(503, "provider broker lifecycle unavailable")
            pending = asyncio.run_coroutine_threadsafe(self.ensure_cell(principal, directory), self.owner_loop)
            await asyncio.wrap_future(pending)
            return
        async with self.lock:
            if self.closed or not cells.CELL_ID.fullmatch(principal.cell_id):
                raise HTTPException(503, "provider broker unavailable")
            existing = self.listeners.get(principal.cell_id)
            if existing:
                if existing.workspace_key != principal.workspace_key or existing.task.done():
                    raise HTTPException(503, "provider broker unavailable")
                return
            if len(self.listeners) >= MAX_CELL_LISTENERS:
                raise HTTPException(503, "provider broker capacity reached")
            directory = Path(directory).absolute()
            parent = cells.directory_beneath(directory)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            path = directory / "provider.sock"
            inode = None
            task = None
            try:
                info = os.fstat(parent)
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise HTTPException(403, "unsafe provider directory")
                try:
                    info = os.stat("provider.sock", dir_fd=parent, follow_symlinks=False)
                except FileNotFoundError:
                    info = None
                if info:
                    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                        raise HTTPException(403, "unsafe provider socket")
                    # Never replace another live gateway's listener.
                    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    probe.settimeout(1)
                    try:
                        probe.connect(str(path))
                    except ConnectionRefusedError:
                        os.unlink("provider.sock", dir_fd=parent)
                    else:
                        raise HTTPException(503, "provider socket already owned")
                    finally:
                        probe.close()
                sock.bind(f"/proc/self/fd/{parent}/provider.sock")
                os.chmod("provider.sock", 0o600, dir_fd=parent, follow_symlinks=False)
                inode = os.stat("provider.sock", dir_fd=parent, follow_symlinks=False).st_ino
                sock.setblocking(False)
                async def application(scope, receive, send):
                    await self._serve(principal.cell_id, principal.workspace_key, scope, receive, send)
                config = uvicorn.Config(application, interface="asgi3", lifespan="off", http="h11",
                    access_log=False, log_config=None, log_level=None, proxy_headers=False,
                    server_header=False, date_header=False, limit_concurrency=MAX_ACTIVE_REQUESTS,
                    h11_max_incomplete_event_size=MAX_HEADER_BYTES, timeout_graceful_shutdown=5)
                server = _OwnedServer(config)
                task = asyncio.create_task(server.serve(sockets=[sock]))
                async with asyncio.timeout(10):
                    while not server.started:
                        if task.done():
                            await task
                            raise HTTPException(503, "provider broker unavailable")
                        await asyncio.sleep(0.01)
                self.listeners[principal.cell_id] = _Listener(principal.workspace_key, path, inode, server, task)
            except BaseException:
                if task is not None:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                sock.close()
                if inode is not None:
                    self._unlink(path, inode)
                raise
            finally:
                os.close(parent)

    @staticmethod
    def _unlink(path: Path, inode: int):
        try:
            info = path.lstat()
            if stat.S_ISSOCK(info.st_mode) and info.st_ino == inode and info.st_uid == os.getuid():
                path.unlink()
        except FileNotFoundError:
            pass

    async def _serve(self, cell_id, workspace_key, scope, receive, send):
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        actor = None
        started = False
        children = []
        own_task = asyncio.current_task()
        self.transfers.add(own_task)
        try:
            if self.closed:
                raise BrokerRefusal(503, "provider_broker_unavailable")
            if b"%" in scope.get("raw_path", b""):
                raise BrokerRefusal(404, "provider_route_refused")
            provider, tail = _route(request.method, scope["path"], scope.get("query_string", b""))
            token, headers = _request_headers(scope["headers"])
            if request.headers.get("content-encoding", "identity").lower() != "identity":
                raise BrokerRefusal(415, "provider_encoding_refused")
            claims = _decode_grant(token, cell_id, workspace_key, provider)
            # Reserve before any DB/thread work or untrusted body production.
            actor = (workspace_key, claims["actor"])
            if self.active >= MAX_ACTIVE_REQUESTS or self.actors.get(actor, 0) >= MAX_ACTOR_REQUESTS:
                actor = None
                raise BrokerRefusal(429, "provider_concurrency_exceeded")
            self.active += 1
            self.actors[actor] = self.actors.get(actor, 0) + 1
            async def revalidate():
                while True:
                    await asyncio.sleep(1)
                    await asyncio.to_thread(_live_identity, claims)

            async def upload():
                length = request.headers.get("content-length")
                if length and (not length.isdecimal() or int(length) > MAX_BODY_BYTES):
                    raise BrokerRefusal(413, "provider_body_too_large")
                body = bytearray()
                stream = request.stream().__aiter__()
                while True:
                    try:
                        async with asyncio.timeout(30):
                            chunk = await anext(stream)
                    except StopAsyncIteration:
                        return bytes(body)
                    if len(body) + len(chunk) > MAX_BODY_BYTES:
                        raise BrokerRefusal(413, "provider_body_too_large")
                    body.extend(chunk)

            await asyncio.to_thread(_live_identity, claims)
            watch = asyncio.create_task(revalidate())
            upload_task = asyncio.create_task(upload())
            children = [watch, upload_task]
            done, _ = await asyncio.wait(children, return_when=asyncio.FIRST_COMPLETED)
            if watch in done:
                watch.result()
            body = upload_task.result()
            if request.method == "GET" and body:
                raise BrokerRefusal(400, "provider_body_refused")
            # Revalidate after a slow upload and resolve the newest rotated key.
            key = await asyncio.to_thread(_live_identity, claims, resolve_key=True)
            spec = provider_keys.VALIDATION_SPECS[provider]
            headers[spec.header] = spec.prefix + key
            if provider == "anthropic":
                headers.setdefault("anthropic-version", "2023-06-01")
            url = self.origins[provider] + tail
            if scope.get("query_string"):
                url += "?" + scope["query_string"].decode("ascii")

            async def transfer():
                nonlocal started
                async with self.client.stream(request.method, url, headers=headers, content=body) as response:
                    # Never forward an upstream error body that could reflect a credential.
                    if not 200 <= response.status_code < 300:
                        status = response.status_code if 400 <= response.status_code <= 599 else 502
                        raise BrokerRefusal(status, "provider_upstream_refused")
                    if (len(response.headers.raw) > MAX_HEADER_COUNT or
                            sum(len(k) + len(v) for k, v in response.headers.raw) > MAX_HEADER_BYTES):
                        raise BrokerRefusal(502, "provider_response_headers_too_large")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise BrokerRefusal(502, "provider_response_encoding_refused")
                    content_type = response.headers.get("content-type", "application/octet-stream")
                    sse = content_type.split(";", 1)[0].strip().lower() == "text/event-stream"
                    response_length = response.headers.get("content-length")
                    if not sse and response_length and int(response_length) > MAX_RESPONSE_BYTES:
                        raise BrokerRefusal(502, "provider_response_too_large")
                    await send({"type": "http.response.start", "status": response.status_code,
                                "headers": [(b"content-type", content_type.encode("latin-1")),
                                            (b"cache-control", b"no-store")]})
                    started = True
                    total = 0
                    async for chunk in response.aiter_raw():
                        if not sse:
                            total += len(chunk)
                            if total > MAX_RESPONSE_BYTES:
                                raise BrokerRefusal(502, "provider_response_too_large")
                        # Split transport buffers without ever accumulating SSE history.
                        for offset in range(0, len(chunk), 65536):
                            await send({"type": "http.response.body", "body": chunk[offset:offset + 65536],
                                        "more_body": True})
                    await send({"type": "http.response.body", "body": b"", "more_body": False})

            async def disconnect():
                while True:
                    if (await receive())["type"] == "http.disconnect":
                        return


            children += [asyncio.create_task(transfer()), asyncio.create_task(disconnect())]
            transfer_task = children[-2]
            done, _ = await asyncio.wait([watch, transfer_task, children[-1]],
                                         return_when=asyncio.FIRST_COMPLETED)
            for child in done:
                child.result()
            if transfer_task not in done:
                raise BrokerRefusal(401, "provider_grant_revoked")
        except BrokerRefusal as exc:
            if not started:
                await JSONResponse({"error": {"code": exc.code}}, status_code=exc.status)(scope, receive, send)
            else:
                # An incomplete SSE is a failure, never a fabricated provider terminal.
                raise ConnectionError("provider stream closed") from None
        except (ClientDisconnect, ConnectionError, asyncio.CancelledError):
            raise
        except Exception:
            if not started:
                await JSONResponse({"error": {"code": "provider_broker_unavailable"}}, status_code=503)(scope, receive, send)
            else:
                raise ConnectionError("provider stream closed") from None
        finally:
            for child in children:
                child.cancel()
            cancelled_during_cleanup = False
            if children:
                cleanup = asyncio.gather(*children, return_exceptions=True)
                # Keep the census pinned until actual child cleanup finishes,
                # including when shutdown cancels an already-unwinding request.
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        cancelled_during_cleanup = True
            if actor:
                self.active -= 1
                count = self.actors[actor] - 1
                if count:
                    self.actors[actor] = count
                else:
                    del self.actors[actor]
            self.transfers.discard(own_task)
            if cancelled_during_cleanup:
                raise asyncio.CancelledError

    async def close(self):
        if self.owner_loop is not None and asyncio.get_running_loop() is not self.owner_loop:
            if self.owner_loop.is_closed() or not self.owner_loop.is_running():
                raise HTTPException(503, "provider broker lifecycle unavailable")
            pending = asyncio.run_coroutine_threadsafe(self.close(), self.owner_loop)
            await asyncio.wrap_future(pending)
            return
        self.closed = True
        for transfer in list(self.transfers):
            transfer.cancel()
        if self.transfers:
            await asyncio.gather(*list(self.transfers), return_exceptions=True)
        for listener in self.listeners.values():
            listener.server.should_exit = True
        await asyncio.gather(*(row.task for row in self.listeners.values()), return_exceptions=True)
        for listener in self.listeners.values():
            self._unlink(listener.path, listener.inode)
        self.listeners.clear()
        await self.client.aclose()


_broker: ProviderBroker | None = None


def get_broker() -> ProviderBroker:
    if _broker is None or _broker.owner_loop is None:
        raise HTTPException(503, "provider broker lifecycle unavailable")
    return _broker


def active_request_count() -> int:
    """Conservative drain census: uploads, header waits and live provider streams."""
    return _broker.active if _broker is not None else 0


async def startup():
    global _broker
    if _broker is None:
        _broker = ProviderBroker()
    await _broker.start()


async def ensure_cell(principal, directory):
    await get_broker().ensure_cell(principal, directory)


async def shutdown():
    global _broker
    if _broker is not None:
        await _broker.close()
        _broker = None
