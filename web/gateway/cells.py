"""The operator's engine cell: admission, filesystem pinning and activation.

The browser never reaches a shared daemon. Its engine is a hostd running inside
an OS-isolated cell (web/deploy/cell): a socket-activated systemd user unit,
``chudware-cell@<id>``, that sandboxes hostd with bubblewrap and mounts only the
operator's workspace as /work. The gateway prepares the cell directory, writes
its ``cell.json`` identity, asks the user manager to start the socket, and
connects to ``gw.sock``. Every path step is a no-follow descriptor walk, so a
symlink in the workspace or cell tree can never redirect the gateway.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import socket
import stat
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException

try:
    from .config import settings
    from .identity import current_operator
except ImportError:  # uvicorn app:app from the package directory
    from config import settings
    from identity import current_operator

CELL_ID = re.compile(r"[0-9a-f]{32}\Z")
RIGHTS = {"viewer": {"read"}, "editor": {"read", "write", "run"},
          "manager": {"read", "write", "run", "manage"},
          "owner": {"read", "write", "run", "manage"}}
POLICY_PATH = Path(__file__).resolve().parents[2] / "contracts" / "cell_methods.json"


@dataclass(frozen=True)
class CellPrincipal:
    workspace_key: str
    actor_key: str
    right: str
    epoch: int
    cell_id: str
    root: Path

    def hello(self) -> dict:
        return {"workspace_key": self.workspace_key, "actor_key": self.actor_key,
                "right": self.right, "epoch": self.epoch,
                "connection_nonce": secrets.token_hex(16)}


def method_policy() -> dict:
    # The native/worker inventory is the sole authority, never a second list.
    try:
        with POLICY_PATH.open("rb") as handle:
            raw = handle.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("policy too large")
        value = json.loads(raw)
        if not isinstance(value, dict) or any(
            not isinstance(name, str) or not isinstance(row, dict)
            or row.get("cell") not in {"allow", "refuse"}
            or row.get("right") not in {"read", "write", "run", "manage"}
            or row.get("scope") not in {"workspace", "actor"}
            for name, row in value.items()
        ):
            raise ValueError("invalid policy")
        return value
    except (OSError, ValueError, RecursionError):
        raise HTTPException(503, "cell policy unavailable") from None


def directory_beneath(root: Path, relative: str = "") -> int:
    """Pin a directory using a no-follow walk, including the configured root."""
    root = Path(root).absolute()
    parts = root.parts[1:]
    if relative:
        if (relative.startswith("/") or "\\" in relative
                or any(ord(char) < 32 or ord(char) == 127 for char in relative)):
            raise HTTPException(400, "bad relative path")
        extra = relative.split("/")
        if any(p in {"", ".", ".."} for p in extra):
            raise HTTPException(400, "bad relative path")
        parts += tuple(extra)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except FileNotFoundError:
        os.close(fd)
        raise HTTPException(404, "not found") from None
    except OSError:
        os.close(fd)
        raise HTTPException(403, "unsafe filesystem path") from None


def open_beneath(root: Path, relative: str, *, max_bytes: int | None = None):
    parts = relative.split("/")
    if not relative or relative.startswith("/") or "\\" in relative or any(
        ord(char) < 32 or ord(char) == 127 for char in relative
    ) or any(
        p in {"", ".", ".."} for p in parts
    ):
        raise HTTPException(400, "bad relative path")
    parent = directory_beneath(root, "/".join(parts[:-1]))
    try:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=parent)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or (max_bytes is not None and info.st_size > max_bytes):
            os.close(fd)
            raise HTTPException(403, "unsafe or oversized file")
        return fd, info
    except FileNotFoundError:
        raise HTTPException(404, "not found") from None
    except OSError:
        raise HTTPException(403, "unsafe filesystem path") from None
    finally:
        os.close(parent)


def resolve_workspace() -> CellPrincipal:
    """The operator's principal: owner of the whole workspace, epoch 0."""
    operator = current_operator()
    root = settings.workspace_root
    root.mkdir(parents=True, exist_ok=True)
    os.close(directory_beneath(root))
    key = operator.workspace_key
    return CellPrincipal(key, operator.name, "owner", 0,
                         hashlib.sha256(key.encode()).hexdigest()[:32], root.absolute())


def prepare_cell(principal: CellPrincipal) -> Path:
    if not CELL_ID.fullmatch(principal.cell_id):
        raise HTTPException(403, "cell id refused")
    if not principal.root.is_relative_to(settings.workspace_root.absolute()):
        raise HTTPException(403, "cell root refused")
    os.close(directory_beneath(principal.root))
    base = settings.cells_root.absolute()
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = directory_beneath(base)
    try:
        try:
            os.mkdir(principal.cell_id, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
    finally:
        os.close(parent)
    directory = base / principal.cell_id
    fd = directory_beneath(directory)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise HTTPException(403, "unsafe cell directory")
        expected = {"schema": "chudware-cell-v1", "cell_id": principal.cell_id,
                    "workspace_key": principal.workspace_key, "root": str(principal.root)}
        try:
            config_fd = os.open("cell.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        except FileNotFoundError:
            config_fd = None
        if config_fd is not None:
            with os.fdopen(config_fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise HTTPException(403, "unsafe cell configuration")
                raw = handle.read(8193)
            if len(raw) > 8192 or json.loads(raw) != expected:
                raise HTTPException(403, "cell configuration mismatch")
        else:
            temp = ".cell-" + secrets.token_hex(12)
            config_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o600, dir_fd=fd)
            try:
                with os.fdopen(config_fd, "w") as handle:
                    json.dump(expected, handle, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.rename(temp, "cell.json", src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                try:
                    os.unlink(temp, dir_fd=fd)
                except FileNotFoundError:
                    pass
    except (OSError, ValueError):
        raise HTTPException(403, "cell configuration refused") from None
    finally:
        os.close(fd)
    return directory


async def start_cell(cell_id: str) -> None:
    if not CELL_ID.fullmatch(cell_id):
        raise HTTPException(403, "cell id refused")
    process = await asyncio.create_subprocess_exec(
        "systemctl", "--user", "start", f"chudware-cell@{cell_id}.socket",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(15):
            result = await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()
        raise HTTPException(503, "cell activation unavailable") from None
    if result:
        raise HTTPException(503, "cell activation unavailable")


def _connect_pinned(directory: Path, filename: str = "gw.sock") -> socket.socket:
    parent = directory_beneath(directory)
    fd = None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        fd = os.open(filename, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        info = os.fstat(fd)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise HTTPException(403, "unsafe cell socket")
        sock.settimeout(10)
        sock.connect(f"/proc/self/fd/{fd}")
        sock.setblocking(False)
        return sock
    except BaseException:
        sock.close()
        raise
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


async def connect_cell(principal: CellPrincipal):
    directory = prepare_cell(principal)
    await start_cell(principal.cell_id)
    sock = await asyncio.to_thread(_connect_pinned, directory)
    return await asyncio.open_unix_connection(sock=sock, limit=settings.ws_max_message_bytes)
