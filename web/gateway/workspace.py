"""Authorized workspace file API + aicm mesh fetch for the web-native client.

The caller's explicitly selected team or personal tree is the path jail.
Without a selection, verified University of Alabama accounts default to the
configured live Box-backed team; all others keep their isolated personal tree.
Team membership, institutional verification and owner funding are rechecked by
orgs.resolve_team_bucket. Chats remain per-account even in a shared workspace;
file recents belong to the selected tree. Rate limits remain per-account.
Client paths are workspace-relative; the gateway never exposes host mount paths.
Mesh fetches use cell-local document grants, revalidated against live membership
and epoch. Upload admission runs before multipart parsing and bounds the stream.
"""

from __future__ import annotations
import json
import os

import logging
import secrets
import stat
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.datastructures import UploadFile
from starlette.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse, JSONResponse

try:
    from . import cells, engine_bridge, orgs
    from .auth import User, get_current_user
    from .config import settings
    from .limits import (
        api_writes,
        enforce_disk_quota,
        storage_write_lock,
        dir_size_bytes,
        enforce_upload_size,
        uploads,
        bounded_form,
        read_bounded_body,
        stream_open_file,
        store_upload,
        list_open_directory,
    )
except ImportError:  # uvicorn app:app from package dir
    import cells
    import engine_bridge
    from auth import User, get_current_user
    import orgs
    from config import settings
    from limits import (
        api_writes,
        enforce_disk_quota,
        storage_write_lock,
        dir_size_bytes,
        enforce_upload_size,
        uploads,
        bounded_form,
        read_bounded_body,
        stream_open_file,
        store_upload,
        list_open_directory,
    )

log = logging.getLogger("chudware-web.workspace")

router = APIRouter()

# Durable recent-files list for the WASM client (config.json lives in /tmp there).
_RECENTS_NAME = ".chudware_recents.json"
_RECENTS_MAX = 10
# Per-account copilot chat store. The current client PUTs a complete flat v2
# envelope and expects GET to return it verbatim.
_CHAT_NAME = ".chudware_chat_history.json"
_CHAT_MAX_BODY = 5 * 1024 * 1024  # reject PUT bodies larger than 5 MiB
_CHAT_MAX_SESSIONS = 1000
_CHAT_MAX_SESSION_ID = 256


def _quota_target(request: Request, user: User) -> tuple[str, str]:
    team = getattr(request.state, "workspace_team", None)
    if team:
        return team.owner_email, team.owner_plan
    return user.email, request.state.workspace_principal.plan


def _caller_root(request: Request, scope: str = "") -> tuple[User, Path]:
    """Revalidate the selected principal and its funding before every file access."""
    user = get_current_user(request)
    if scope == "shared":
        raise HTTPException(status_code=403, detail="shared scope unavailable")
    if scope and scope != "personal":
        raise HTTPException(status_code=400, detail="unknown scope")
    team = orgs.selected_workspace_team(request, user.email, scope)
    principal = cells.resolve_workspace(user.email, team or "")
    if request.method not in {"GET", "HEAD"}:
        if principal.right not in {"owner", "manager", "editor"}:
            raise HTTPException(status_code=403, detail="forbidden_right")
        if team:
            request.state.workspace_team = orgs.resolve_team_bucket(user.email, team, write=True)
    elif team:
        request.state.workspace_team = orgs.resolve_team_bucket(user.email, team, write=False)
    request.state.workspace_principal = principal
    root = principal.root if team else cells.personal_root(user.email)
    root.mkdir(parents=True, exist_ok=True)
    return user, root.absolute()


@router.post("/workspace/selection")
def workspace_selection(request: Request, body: dict[str, Any]) -> JSONResponse:
    user = get_current_user(request)
    selection = str(body.get("team") or "")
    if selection != "personal":
        orgs.resolve_team_bucket(user.email, selection, write=False)
    response = JSONResponse({"ok": True, "team": selection})
    response.set_cookie("chudware_workspace", selection, httponly=True,
                        secure=request.url.scheme == "https", samesite="lax", path="/")
    return response


def _resolve(root: Path, rel: str, *, must_exist: bool = True) -> Path:
    """Clamp a client-relative path under ``root`` (the caller's own tree).

    Containment is checked on the FULLY RESOLVED path, and a final-component
    symlink is always rejected, so a symlink planted inside the jail cannot be
    used to escape it (e.g. upload through link -> /etc, or rename over one).
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
def workspace_list(request: Request, dir: str = "", scope: str = "") -> dict[str, Any]:
    _, root = _caller_root(request, scope)
    directory = dir.strip().replace("\\", "/").removeprefix("./")
    if directory == ".":
        directory = ""
    dirs, files = list_open_directory(cells.directory_beneath(root, directory), directory, 4000)
    return {"root": ".", "workspace_key": request.state.workspace_principal.workspace_key,
            "dir": directory, "dirs": dirs[:2000], "files": files[:2000]}


@router.get("/workspace/download")
def workspace_download(request: Request, path: str, scope: str = "") -> StreamingResponse:
    _, root = _caller_root(request, scope)
    fd, st = cells.open_beneath(root, path, max_bytes=settings.max_upload_bytes)
    return stream_open_file(fd, st, filename=Path(path).name, headers={"Cache-Control": "no-store"})


@router.post("/workspace/upload")
async def workspace_upload(request: Request) -> dict[str, Any]:
    """Admit and bound multipart input before the parser buffers any bytes."""
    user, root = await run_in_threadpool(_caller_root, request, request.query_params.get("scope", ""))
    api_writes.enforce(user.email, "writes")
    uploads.enforce(user.email, "uploads")
    cap = enforce_upload_size(_content_length(request))
    quota_email, plan = _quota_target(request, user)
    # The destination is multipart data, so replacement credit is known only
    # after parsing. Check current storage before accepting any input; exact
    # replacement-delta admission is repeated under the write lock.
    await run_in_threadpool(enforce_disk_quota, quota_email, plan, root)
    async with bounded_form(request, cap) as form:
        file = form.get("file")
        directory = form.get("dir", "")
        if not isinstance(file, UploadFile) or not isinstance(directory, str):
            raise HTTPException(status_code=422, detail="file and directory required")
        if len(directory) > 4096:
            raise HTTPException(status_code=400, detail="directory too long")
        return await run_in_threadpool(
            _store_upload, request, user, root, file, directory, cap, quota_email, plan
        )


def _store_upload(request: Request, user: User, root: Path, file: UploadFile,
                  directory: str, cap: int, quota_email: str, plan: str) -> dict[str, Any]:
    with storage_write_lock(root):
        base = _resolve(root, directory)
        if not base.is_dir():
            raise HTTPException(status_code=400, detail="not a directory")
        name = Path((file.filename or "upload.bin").replace("\\", "/")).name
        if name.startswith(".") or not name:
            raise HTTPException(status_code=400, detail="bad filename")
        relative = str((base / name).relative_to(root))
        size = store_upload(root, relative, file, cap=cap, email=quota_email, plan=plan)
        return {"ok": True, "path": relative,
                "workspace_key": request.state.workspace_principal.workspace_key, "bytes": size}


@router.post("/workspace/mkdir")
def workspace_mkdir(request: Request, body: dict[str, Any], scope: str = "") -> dict[str, Any]:
    
    user, root = _caller_root(request, scope)
    with storage_write_lock(root):
        api_writes.enforce(user.email, "writes")
        target = _resolve(root, str(body.get("path", "")), must_exist=False)
        target.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "path": str(target.relative_to(root))}


@router.post("/workspace/rename")
def workspace_rename(request: Request, body: dict[str, Any], scope: str = "") -> dict[str, Any]:
    
    user, root = _caller_root(request, scope)
    with storage_write_lock(root):
        api_writes.enforce(user.email, "writes")
        src = _resolve(root, str(body.get("src", "")))
        dst = _resolve(root, str(body.get("dst", "")), must_exist=False)
        if dst.exists():
            raise HTTPException(status_code=409, detail="destination exists")
        src.rename(dst)
        return {"ok": True, "path": str(dst.relative_to(root))}


@router.post("/workspace/delete")
def workspace_delete(request: Request, body: dict[str, Any], scope: str = "") -> dict[str, Any]:
    
    user, root = _caller_root(request, scope)
    with storage_write_lock(root):
        api_writes.enforce(user.email, "writes")
        target = _resolve(root, str(body.get("path", "")))
        if target == root:
            raise HTTPException(status_code=400, detail="refusing to delete root")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return {"ok": True}


@router.post("/workspace/copy")
def workspace_copy(request: Request, body: dict[str, Any], scope: str = "") -> dict[str, Any]:
    """Copy a file or directory within the caller's workspace under quota."""
    user, root = _caller_root(request, scope)
    with storage_write_lock(root):
        api_writes.enforce(user.email, "writes")
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
            incoming = dir_size_bytes(src)
        else:
            if not src.is_file():
                raise HTTPException(status_code=400, detail="not a file")
            if dst.exists() and dst.is_dir():
                dst = _resolve(root, str((dst / src.name).relative_to(root)), must_exist=False)
            if dst.exists():
                raise HTTPException(status_code=409, detail="destination exists")
            incoming = src.stat().st_size
    
        quota_email, plan = _quota_target(request, user)
        enforce_disk_quota(quota_email, plan, root, incoming_bytes=incoming)
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
            # Bind enforcement to actual copied bytes if the source changed during
            # the copy. Rejection removes the whole destination before returning.
            enforce_disk_quota(quota_email, plan, root)
        except BaseException:
            if dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst, ignore_errors=True)
            else:
                dst.unlink(missing_ok=True)
            raise
        log.info("workspace copy: %s -> %s by %s", src, dst, user.email)
        return {"ok": True, "path": str(dst.relative_to(root))}


@router.get("/workspace/recents")
def workspace_recents_get(request: Request, scope: str = "") -> dict[str, Any]:
    """Load the caller's durable recent-files list (<root>/.chudware_recents.json)."""
    _, root = _caller_root(request, scope)
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



def _recent_path(root: Path, value: str) -> str:
    value = value.strip().replace("\\", "/")
    if not value or len(value) > 4096 or value.startswith("aicm-shm:"):
        raise HTTPException(status_code=400, detail="invalid recent path")
    target = _resolve(root, value, must_exist=False)
    if target == root:
        raise HTTPException(status_code=400, detail="recent path must name a file")
    return str(target.relative_to(root))

@router.put("/workspace/recents")
def workspace_recents_put(request: Request, body: dict[str, Any], scope: str = "") -> dict[str, Any]:
    """Persist the caller's recent-files list inside their own workspace root."""
    user, root = _caller_root(request, scope)
    with storage_write_lock(root):
        api_writes.enforce(user.email, "writes")
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
        path = root / _RECENTS_NAME
        quota_email, plan = _quota_target(request, user)
        _store_json(root, path, {"files": files}, quota_email, plan)
        log.info("workspace recents: wrote %d entries for %s", len(files), user.email)
        return {"ok": True, "files": files}


def _chat_path(root: Path) -> Path:
    return root / _CHAT_NAME


def _load_chat_store(root: Path) -> dict[str, Any]:
    """Load the caller's own chat store; missing/corrupt → empty object."""
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


def _store_json(root: Path, path: Path, data: dict[str, Any], email: str, plan: str) -> None:
    with storage_write_lock(root):
        parent = cells.directory_beneath(root)
        try:
            try:
                previous_info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                previous = 0
            else:
                if not stat.S_ISREG(previous_info.st_mode):
                    raise HTTPException(status_code=400, detail="not a regular file")
                previous = previous_info.st_size
            # Compact JSON keeps storage bounded (ASCII escaping can expand UTF-8
            # by at most 3x), preserving every v2 session/message field.
            encoded = (json.dumps(data, separators=(",", ":")) + "\n").encode("utf-8")
            enforce_disk_quota(email, plan, root, incoming_bytes=len(encoded), replaced_bytes=previous)
            _atomic_write_json(parent, path.name, encoded)
        finally:
            os.close(parent)


def _atomic_write_json(parent: int, name: str, encoded: bytes) -> None:
    """Publish JSON using a pinned directory fd; never reopen a tenant path."""
    temporary = "." + name + "." + secrets.token_hex(16) + ".tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(encoded)
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


@router.get("/workspace/chat")
def workspace_chat_get(request: Request, scope: str = "") -> dict[str, Any]:
    """Return the caller's complete flat chat store, or an empty object if absent."""
    # Conversations are private even while collaborating on a shared CAD tree.
    _, root = _caller_root(request, "personal")
    return _load_chat_store(root)


@router.put("/workspace/chat")
async def workspace_chat_put(request: Request, scope: str = "") -> dict[str, Any]:
    """Atomically replace the caller's complete flat v2 chat store."""
    user, root = await run_in_threadpool(_caller_root, request, "personal")
    api_writes.enforce(user.email, "writes")
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
    quota_email, plan = _quota_target(request, user)
    await run_in_threadpool(_store_json, root, _chat_path(root), store, quota_email, plan)
    log.info(
        "workspace chat: replaced v2 store with %d session(s) for %s",
        len(store["sessions"]),
        user.email,
    )
    return {"ok": True, "sessions": len(store["sessions"])}

@router.get("/files/aicm")
def files_aicm(request: Request, workspace_key: str, doc_id: str = "",
               path: str = "", scope: str = "") -> StreamingResponse:
    """Serve only cell-granted meshes or relative files in a live workspace."""
    if request.query_params.get("shm") or path.startswith("aicm-shm:"):
        raise HTTPException(status_code=400, detail="shared mesh handoffs not allowed")
    if path:
        # Validate even with a grant: no obsolete absolute client path accepted.
        if path.startswith("/") or "\\" in path or (len(path) > 1 and path[1] == ":"):
            raise HTTPException(status_code=400, detail="absolute paths not allowed")
    if workspace_key.startswith("team:"):
        parts = workspace_key[5:].split("/")
        if len(parts) != 2 or not all(parts):
            raise HTTPException(status_code=400, detail="bad workspace key")
        team_id = parts[1]
    elif workspace_key.startswith("user:"):
        team_id = ""
    else:
        raise HTTPException(status_code=400, detail="bad workspace key")
    user = get_current_user(request)
    try:
        principal = cells.resolve_workspace(user.email, team_id)
    except HTTPException as exc:
        if exc.status_code in {403, 404}:
            raise HTTPException(status_code=403, detail="document not authorized") from None
        raise
    if principal.workspace_key != workspace_key:
        raise HTTPException(status_code=403, detail="document not authorized")
    if doc_id:
        opened = engine_bridge.mesh_document_open(
            principal.account_uid, workspace_key, doc_id, max_bytes=settings.max_upload_bytes
        )
        if opened is None:
            raise HTTPException(status_code=403, detail="document not authorized")
        fd, st = opened
    elif path:
        fd, st = cells.open_beneath(principal.root, path, max_bytes=settings.max_upload_bytes)
    else:
        raise HTTPException(status_code=400, detail="doc_id or relative path required")
    return stream_open_file(fd, st, headers={"Cache-Control": "no-store"})
