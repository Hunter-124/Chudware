"""Workspace file API and mesh fetch for the WASM client.

All paths are relative to the operator's workspace root, which is also the
engine cell's /work; the gateway never exposes host paths to the browser.
Every access is clamped beneath that root through no-follow descriptor walks,
and a final-component symlink is always refused. Uploads are admitted and
bounded before multipart parsing. Mesh fetches serve only documents the engine
cell published on this workspace's /engine bridge.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import stat
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

try:
    from . import cells, engine_bridge
    from .config import settings
    from .limits import (
        bounded_form,
        enforce_upload_size,
        list_open_directory,
        read_bounded_body,
        storage_write_lock,
        store_upload,
        stream_open_file,
    )
except ImportError:  # uvicorn app:app from the package directory
    import cells
    import engine_bridge
    from config import settings
    from limits import (
        bounded_form,
        enforce_upload_size,
        list_open_directory,
        read_bounded_body,
        storage_write_lock,
        store_upload,
        stream_open_file,
    )

log = logging.getLogger("chudware-web.workspace")

router = APIRouter()

# Durable recent-files list for the WASM client (its config.json lives in /tmp).
_RECENTS_NAME = ".chudware_recents.json"
_RECENTS_MAX = 10
# Copilot chat store. The client PUTs a complete flat v2 envelope and expects
# GET to return it verbatim.
_CHAT_NAME = ".chudware_chat_history.json"
_CHAT_MAX_BODY = 5 * 1024 * 1024
_CHAT_MAX_SESSIONS = 1000
_CHAT_MAX_SESSION_ID = 256


def _root() -> Path:
    """The operator's workspace root, created on first use."""
    root = settings.workspace_root
    root.mkdir(parents=True, exist_ok=True)
    return root.absolute()


def _resolve(root: Path, rel: str, *, must_exist: bool = True) -> Path:
    """Clamp a client-relative path under ``root``.

    Containment is checked on the FULLY RESOLVED path, and a final-component
    symlink is always rejected, so a symlink planted inside the workspace cannot
    be used to escape it (e.g. upload through link -> /etc, or rename over one).
    """
    rel = (rel or "").strip().replace("\\", "/")
    if any(ord(char) < 32 or ord(char) == 127 for char in rel):
        raise HTTPException(status_code=400, detail="bad path")
    if rel.startswith("/") or (len(rel) > 1 and rel[1] == ":"):
        raise HTTPException(status_code=400, detail="absolute paths not allowed")
    if any(part in {"..", ""} for part in rel.split("/")) and rel != "":
        raise HTTPException(status_code=400, detail="bad path")
    if not rel:
        return root
    candidate = root / rel
    if candidate.is_symlink():
        raise HTTPException(status_code=400, detail="symlink not allowed")
    target = candidate.resolve()
    if target != root and root not in target.parents:
        raise HTTPException(status_code=400, detail="path escapes workspace")
    if must_exist and not target.exists():
        raise HTTPException(status_code=404, detail="not found")
    return target


def _content_length(request: Request) -> int | None:
    """Best-effort Content-Length; None when absent (chunked) or malformed."""
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


@router.get("/workspace/list")
def workspace_list(dir: str = "") -> dict[str, Any]:
    root = _root()
    directory = dir.strip().replace("\\", "/").removeprefix("./")
    if directory == ".":
        directory = ""
    dirs, files = list_open_directory(cells.directory_beneath(root, directory), directory, 4000)
    return {"root": ".", "workspace_key": cells.resolve_workspace().workspace_key,
            "dir": directory, "dirs": dirs[:2000], "files": files[:2000]}


@router.get("/workspace/download")
def workspace_download(path: str) -> StreamingResponse:
    fd, st = cells.open_beneath(_root(), path, max_bytes=settings.max_upload_bytes)
    return stream_open_file(fd, st, filename=Path(path).name, headers={"Cache-Control": "no-store"})


@router.post("/workspace/upload")
async def workspace_upload(request: Request) -> dict[str, Any]:
    """Admit and bound multipart input before the parser buffers any bytes."""
    root = await run_in_threadpool(_root)
    cap = enforce_upload_size(_content_length(request))
    async with bounded_form(request, cap) as form:
        file = form.get("file")
        directory = form.get("dir", "")
        if not isinstance(file, UploadFile) or not isinstance(directory, str):
            raise HTTPException(status_code=422, detail="file and directory required")
        if len(directory) > 4096:
            raise HTTPException(status_code=400, detail="directory too long")
        return await run_in_threadpool(_store_upload, root, file, directory, cap)


def _store_upload(root: Path, file: UploadFile, directory: str, cap: int) -> dict[str, Any]:
    with storage_write_lock(root):
        base = _resolve(root, directory)
        if not base.is_dir():
            raise HTTPException(status_code=400, detail="not a directory")
        name = Path((file.filename or "upload.bin").replace("\\", "/")).name
        if name.startswith(".") or not name:
            raise HTTPException(status_code=400, detail="bad filename")
        relative = str((base / name).relative_to(root))
        size = store_upload(root, relative, file, cap=cap)
        return {"ok": True, "path": relative,
                "workspace_key": cells.resolve_workspace().workspace_key, "bytes": size}


@router.post("/workspace/mkdir")
def workspace_mkdir(body: dict[str, Any]) -> dict[str, Any]:
    root = _root()
    with storage_write_lock(root):
        target = _resolve(root, str(body.get("path", "")), must_exist=False)
        target.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "path": str(target.relative_to(root))}


@router.post("/workspace/rename")
def workspace_rename(body: dict[str, Any]) -> dict[str, Any]:
    root = _root()
    with storage_write_lock(root):
        src = _resolve(root, str(body.get("src", "")))
        dst = _resolve(root, str(body.get("dst", "")), must_exist=False)
        if dst.exists():
            raise HTTPException(status_code=409, detail="destination exists")
        src.rename(dst)
        return {"ok": True, "path": str(dst.relative_to(root))}


@router.post("/workspace/delete")
def workspace_delete(body: dict[str, Any]) -> dict[str, Any]:
    root = _root()
    with storage_write_lock(root):
        target = _resolve(root, str(body.get("path", "")))
        if target == root:
            raise HTTPException(status_code=400, detail="refusing to delete root")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return {"ok": True}


@router.post("/workspace/copy")
def workspace_copy(body: dict[str, Any]) -> dict[str, Any]:
    """Copy a file or directory within the workspace."""
    root = _root()
    with storage_write_lock(root):
        src = _resolve(root, str(body.get("src", "")))
        dst = _resolve(root, str(body.get("dst", "")), must_exist=False)
        recursive = bool(body.get("recursive", True))
        if src == root:
            raise HTTPException(status_code=400, detail="refusing to copy root")
        if dst == root or dst == src:
            raise HTTPException(status_code=400, detail="bad destination")
        if src.is_dir():
            if not recursive:
                raise HTTPException(status_code=400, detail="directory copy requires recursive=true")
            if dst.exists():
                raise HTTPException(status_code=409, detail="destination exists")
        else:
            if not src.is_file():
                raise HTTPException(status_code=400, detail="not a file")
            if dst.exists() and dst.is_dir():
                dst = _resolve(root, str((dst / src.name).relative_to(root)), must_exist=False)
            if dst.exists():
                raise HTTPException(status_code=409, detail="destination exists")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        except BaseException:
            # Never leave a partial destination behind.
            if dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst, ignore_errors=True)
            else:
                dst.unlink(missing_ok=True)
            raise
        return {"ok": True, "path": str(dst.relative_to(root))}


def _recent_path(root: Path, value: str) -> str:
    value = value.strip().replace("\\", "/")
    if not value or len(value) > 4096 or value.startswith("aicm-shm:"):
        raise HTTPException(status_code=400, detail="invalid recent path")
    target = _resolve(root, value, must_exist=False)
    if target == root:
        raise HTTPException(status_code=400, detail="recent path must name a file")
    return str(target.relative_to(root))


@router.get("/workspace/recents")
def workspace_recents_get() -> dict[str, Any]:
    """Load the durable recent-files list (<root>/.chudware_recents.json)."""
    root = _root()
    try:
        fd, _ = cells.open_beneath(root, _RECENTS_NAME, max_bytes=256 * 1024)
        with os.fdopen(fd, "rb") as file:
            data = json.loads(file.read(256 * 1024))
    except Exception as e:
        log.warning("recents read failed: %s", e)
        return {"files": []}
    files: list[str] = []
    raw = data.get("files", []) if isinstance(data, dict) else []
    if isinstance(raw, list):
        for e in raw:
            if isinstance(e, str) and e.strip():
                try:
                    files.append(_recent_path(root, e))
                except HTTPException:
                    continue
            if len(files) >= _RECENTS_MAX:
                break
    return {"files": files}


@router.put("/workspace/recents")
def workspace_recents_put(body: dict[str, Any]) -> dict[str, Any]:
    """Persist the recent-files list inside the workspace root."""
    root = _root()
    raw = body.get("files", []) if isinstance(body, dict) else []
    files: list[str] = []
    if isinstance(raw, list):
        seen: set[str] = set()
        for e in raw:
            if not isinstance(e, str):
                continue
            p = _recent_path(root, e)
            if not p or p in seen:
                continue
            seen.add(p)
            files.append(p)
            if len(files) >= _RECENTS_MAX:
                break
    _store_json(root, _RECENTS_NAME, {"files": files})
    return {"ok": True, "files": files}


def _load_chat_store(root: Path) -> dict[str, Any]:
    """Load the chat store; missing/corrupt → empty object."""
    try:
        fd, _ = cells.open_beneath(root, _CHAT_NAME, max_bytes=_CHAT_MAX_BODY * 3 + 1)
        with os.fdopen(fd, "rb") as file:
            data = json.loads(file.read(_CHAT_MAX_BODY * 3 + 1))
    except Exception as e:
        log.warning("chat store read failed: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


def _validate_chat_envelope(body: dict[str, Any]) -> dict[str, Any]:
    """Validate one complete flat v2 store without rewriting its representation."""
    discriminators = [key for key in ("version", "schema") if key in body]
    if len(discriminators) != 1:
        raise HTTPException(
            status_code=400,
            detail="chat store must contain exactly one v2 discriminator (version or schema)",
        )
    discriminator = discriminators[0]
    expected = {discriminator, "active", "sessions"}
    if set(body) != expected:
        raise HTTPException(
            status_code=400,
            detail=f"chat store fields must be exactly {sorted(expected)}",
        )
    version = body[discriminator]
    if isinstance(version, bool) or not isinstance(version, int) or version != 2:
        raise HTTPException(status_code=400, detail="unsupported chat store schema")

    active = body["active"]
    if not isinstance(active, str) or len(active) > _CHAT_MAX_SESSION_ID:
        raise HTTPException(status_code=400, detail="active must be a bounded session id")
    sessions = body["sessions"]
    if not isinstance(sessions, list):
        raise HTTPException(status_code=400, detail="sessions must be an array")
    if len(sessions) > _CHAT_MAX_SESSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"too many chat sessions (max {_CHAT_MAX_SESSIONS})",
        )

    session_ids: set[str] = set()
    for index, session in enumerate(sessions):
        if not isinstance(session, dict):
            raise HTTPException(
                status_code=400,
                detail=f"sessions[{index}] must be an object",
            )
        session_id = session.get("id")
        if (
            not isinstance(session_id, str)
            or not session_id
            or len(session_id) > _CHAT_MAX_SESSION_ID
        ):
            raise HTTPException(
                status_code=400,
                detail=f"sessions[{index}].id must be a bounded non-empty string",
            )
        if session_id in session_ids:
            raise HTTPException(status_code=400, detail=f"duplicate session id: {session_id}")
        session_ids.add(session_id)
    if active and active not in session_ids:
        raise HTTPException(status_code=400, detail="active session is not present in sessions")
    return body


def _store_json(root: Path, name: str, data: dict[str, Any]) -> None:
    """Atomically replace ``<root>/<name>`` through a pinned directory fd."""
    # Compact JSON keeps storage bounded while preserving every field.
    encoded = (json.dumps(data, separators=(",", ":")) + "\n").encode("utf-8")
    with storage_write_lock(root):
        parent = cells.directory_beneath(root)
        temporary = "." + name + "." + secrets.token_hex(16) + ".tmp"
        try:
            try:
                previous = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(previous.st_mode):
                    raise HTTPException(status_code=400, detail="not a regular file")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=parent)
            with os.fdopen(fd, "wb") as file:
                file.write(encoded)
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            os.close(parent)


@router.get("/workspace/chat")
def workspace_chat_get() -> dict[str, Any]:
    """Return the complete flat chat store, or an empty object if absent."""
    return _load_chat_store(_root())


@router.put("/workspace/chat")
async def workspace_chat_put(request: Request) -> dict[str, Any]:
    """Atomically replace the complete flat v2 chat store."""
    root = await run_in_threadpool(_root)
    raw = await read_bounded_body(request, _CHAT_MAX_BODY, "chat body too large (max 5MB)")
    if not raw:
        raise HTTPException(status_code=400, detail="empty body")
    try:
        body = json.loads(raw)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {e}") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    store = _validate_chat_envelope(body)
    await run_in_threadpool(_store_json, root, _CHAT_NAME, store)
    return {"ok": True, "sessions": len(store["sessions"])}


@router.get("/files/aicm")
def files_aicm(workspace_key: str, doc_id: str = "", path: str = "",
               shm: str = "") -> StreamingResponse:
    """Serve only cell-granted meshes or relative files in the workspace."""
    if shm or path.startswith("aicm-shm:"):
        raise HTTPException(status_code=400, detail="shared mesh handoffs not allowed")
    if path.startswith("/") or "\\" in path or (len(path) > 1 and path[1] == ":"):
        raise HTTPException(status_code=400, detail="absolute paths not allowed")
    principal = cells.resolve_workspace()
    if workspace_key != principal.workspace_key:
        raise HTTPException(status_code=403, detail="document not authorized")
    if doc_id:
        opened = engine_bridge.mesh_document_open(
            workspace_key, doc_id, max_bytes=settings.max_upload_bytes
        )
        if opened is None:
            raise HTTPException(status_code=403, detail="document not authorized")
        fd, st = opened
    elif path:
        fd, st = cells.open_beneath(principal.root, path, max_bytes=settings.max_upload_bytes)
    else:
        raise HTTPException(status_code=400, detail="doc_id or relative path required")
    return stream_open_file(fd, st, headers={"Cache-Control": "no-store"})
