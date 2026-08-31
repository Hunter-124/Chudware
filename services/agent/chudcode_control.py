"""Private authenticated control channel for the long-lived services worker."""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import socket
import stat
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Self

log = logging.getLogger("chudware.worker.control")

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024
CLIENT_TIMEOUT_SECONDS = 2.0
MAX_CONNECTIONS = 4
MAX_AUTH_FAILURES = 8
AUTH_FAILURE_WINDOW_SECONDS = 60.0

ControlHandler = Callable[[str, dict], dict]


class ControlServer:
    """One-request-per-connection authenticated loopback control server."""

    def __init__(
        self,
        handler: ControlHandler,
        runtime_dir: Path | str,
        *,
        platform: str | None = None,
        request_limit: int = MAX_REQUEST_BYTES,
        response_limit: int = MAX_RESPONSE_BYTES,
        client_timeout: float = CLIENT_TIMEOUT_SECONDS,
        max_connections: int = MAX_CONNECTIONS,
    ) -> None:
        self._handler = handler
        self._platform = platform or ("nt" if os.name == "nt" else "posix")
        self._runtime_dir = None if self._platform == "nt" else Path(runtime_dir)
        self._request_limit = max(256, int(request_limit))
        self._response_limit = max(256, int(response_limit))
        self._client_timeout = max(0.05, float(client_timeout))
        self._max_connections = max(1, int(max_connections))
        self._slots = threading.BoundedSemaphore(self._max_connections)
        self._token = secrets.token_hex(32)
        self._socket: socket.socket | None = None
        self._socket_path: Path | None = None
        self._socket_inode: int | None = None
        self._endpoint = ""
        self._transport = ""
        self._stop = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._clients: set[socket.socket] = set()
        self._client_threads: set[threading.Thread] = set()
        self._state_lock = threading.Lock()
        self._auth_failures: deque[float] = deque()

    def start(self) -> Self:
        if self._socket is not None:
            return self
        self._stop.clear()
        if self._platform == "nt":
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", 0))
                listener.listen(self._max_connections)
            except Exception:
                listener.close()
                raise
            host, port = listener.getsockname()[:2]
            self._transport = "tcp"
            self._endpoint = f"{host}:{port}"
        else:
            assert self._runtime_dir is not None
            self._runtime_dir.mkdir(parents=True, exist_ok=True)
            path = self._runtime_dir / f"chudcode-worker-{os.getppid()}.sock"
            self._remove_stale_socket(path)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                listener.bind(str(path))
                os.chmod(path, 0o600)
                listener.listen(self._max_connections)
                mode = stat.S_IMODE(path.stat().st_mode)
                if mode != 0o600:
                    raise PermissionError("control socket permissions are not private")
            except Exception:
                listener.close()
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            self._socket_path = path
            self._socket_inode = path.stat().st_ino
            self._transport = "unix"
            self._endpoint = str(path)
        listener.settimeout(0.2)
        self._socket = listener
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="chudcode-control-accept",
            daemon=True,
        )
        self._accept_thread.start()
        return self

    def info(self) -> dict:
        if self._socket is None:
            raise RuntimeError("control server is not running")
        return {
            "transport": self._transport,
            "endpoint": self._endpoint,
            "token": self._token,
        }

    def stop(self) -> None:
        self._stop.set()
        listener = self._socket
        self._socket = None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        with self._state_lock:
            clients = tuple(self._clients)
        for conn in clients:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        thread = self._accept_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        with self._state_lock:
            client_threads = tuple(self._client_threads)
        for client_thread in client_threads:
            if client_thread is not threading.current_thread():
                client_thread.join(timeout=1.0)
        self._unlink_owned_socket()

    def _remove_stale_socket(self, path: Path) -> None:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(mode):
            raise FileExistsError("refusing to replace non-socket control endpoint")
        path.unlink()

    def _unlink_owned_socket(self) -> None:
        path = self._socket_path
        inode = self._socket_inode
        self._socket_path = None
        self._socket_inode = None
        if path is None or inode is None:
            return
        try:
            if path.lstat().st_ino == inode:
                path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning(
                "failed to remove worker control socket: %s", type(exc).__name__
            )

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._socket
            if listener is None:
                break
            try:
                conn, _address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if not self._stop.is_set():
                    log.warning("worker control accept failed")
                break
            if not self._slots.acquire(blocking=False):
                conn.close()
                continue
            thread = threading.Thread(
                target=self._serve_client,
                args=(conn,),
                name="chudcode-control-client",
                daemon=True,
            )
            with self._state_lock:
                self._clients.add(conn)
                self._client_threads.add(thread)
            thread.start()

    def _serve_client(self, conn: socket.socket) -> None:
        current = threading.current_thread()
        try:
            conn.settimeout(self._client_timeout)
            try:
                request = self._read_request(conn)
                response = self._dispatch(request)
            except TimeoutError:
                response = self._error("timeout", "control request timed out")
            except RequestError as exc:
                response = self._error(exc.code, exc.public_message)
            except Exception:  # noqa: BLE001 - redact all handler failures before wire/log
                log.error("worker control request failed")
                response = self._error("internal", "control request failed")
            self._send_response(conn, response)
        finally:
            with self._state_lock:
                self._clients.discard(conn)
                self._client_threads.discard(current)
            try:
                conn.close()
            except OSError:
                pass
            self._slots.release()

    def _read_request(self, conn: socket.socket) -> dict:
        data = bytearray()
        while len(data) <= self._request_limit:
            chunk = conn.recv(min(4096, self._request_limit + 1 - len(data)))
            if not chunk:
                raise RequestError(
                    "malformed", "request must be one newline-terminated JSON object"
                )
            newline = chunk.find(b"\n")
            if newline >= 0:
                data.extend(chunk[:newline])
                if len(data) > self._request_limit:
                    raise RequestError(
                        "request_too_large", "control request exceeded limit"
                    )
                break
            data.extend(chunk)
        else:
            raise RequestError("request_too_large", "control request exceeded limit")
        if len(data) > self._request_limit:
            raise RequestError("request_too_large", "control request exceeded limit")
        try:
            decoded = data.decode("utf-8")
            request = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RequestError("malformed", "invalid control request") from None
        if not isinstance(request, dict):
            raise RequestError("malformed", "control request must be an object")
        return request

    def _dispatch(self, request: dict) -> dict:
        supplied = request.get("token")
        authenticated = isinstance(supplied, str) and hmac.compare_digest(
            supplied, self._token
        )
        if not authenticated:
            limited = self._record_auth_failure()
            if limited:
                raise RequestError("rate_limited", "too many unauthorized attempts")
            raise RequestError("unauthorized", "unauthorized")
        action = request.get("action")
        if action not in {"cancel", "steer", "status"}:
            raise RequestError("invalid_action", "unsupported control action")
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise RequestError("malformed", "control params must be an object")
        result = self._handler(action, params)
        if not isinstance(result, dict):
            raise TypeError("control handler returned a non-object")
        return {"ok": True, "result": result}

    def _record_auth_failure(self) -> bool:
        now = time.monotonic()
        cutoff = now - AUTH_FAILURE_WINDOW_SECONDS
        with self._state_lock:
            while self._auth_failures and self._auth_failures[0] < cutoff:
                self._auth_failures.popleft()
            limited = len(self._auth_failures) >= MAX_AUTH_FAILURES
            if not limited:
                self._auth_failures.append(now)
            return limited

    @staticmethod
    def _error(code: str, message: str) -> dict:
        return {"ok": False, "error": {"code": code, "message": message}}

    def _send_response(self, conn: socket.socket, response: dict) -> None:
        try:
            payload = json.dumps(
                response, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        except (TypeError, ValueError):
            payload = json.dumps(
                self._error("internal", "control response was not serializable"),
                separators=(",", ":"),
            ).encode("utf-8")
        if len(payload) + 1 > self._response_limit:
            payload = json.dumps(
                self._error("response_too_large", "control response exceeded limit"),
                separators=(",", ":"),
            ).encode("utf-8")
        try:
            conn.sendall(payload + b"\n")
        except (OSError, TimeoutError):
            return


class RequestError(Exception):
    def __init__(self, code: str, public_message: str) -> None:
        super().__init__(code)
        self.code = code
        self.public_message = public_message
