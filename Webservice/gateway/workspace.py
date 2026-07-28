"""Per-account workspace file API + aicm mesh fetch for the web-native client.

/workspace/* is rooted at the CALLER's per-account tree
(``settings.user_workspace_root(user_slug(email))``), created on demand. Any
``*.ua.edu`` account is auto-approved, so the workspace MUST be tenant-
isolated: every path resolution is clamped under the caller's own root, the
copilot chat store and recents list are per-account files inside that root,
and mutating endpoints carry per-account rate limits, upload caps, and disk
quotas. An admin may opt into the LEGACY single-tenant shared tree
(``settings.workspace_root`` itself) with ``?scope=shared`` — the legacy
content is never moved or deleted, and non-admins can never address it.

The daemon reads and writes the SAME paths directly (doc_open/doc_save take
server-side paths) — bytes only cross the wire for import/export.

/files/aicm serves tessellation payloads the daemon publishes for its clients:
either plain files (tmp chudware_cpp_docs / state dir / the caller's own
workspace root) or POSIX shm objects (aicm-shm://name -> /dev/shm/<name>).
Enforcement is server-side; the allowlist is the security boundary, the client
clamp is only UX.
"""

from __future__ import annotations
import json

import logging
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

try:
    from .auth import User, get_current_user
    from .config import settings
    from .limits import (
        api_writes,
        enforce_disk_quota,
        enforce_upload_size,
        uploads,
        user_slug,
    )
except ImportError:  # uvicorn app:app from package dir
    from auth import User, get_current_user
    from config import settings
    from limits import (
        api_writes,
        enforce_disk_quota,
        enforce_upload_size,
        uploads,
        user_slug,
    )

log = logging.getLogger("chudware-web.workspace")

router = APIRouter()

# POSIX shm objects published by aicm_handoff (shm_object_name: "chudware_" + sanitized key).
_SHM_NAME_RE = re.compile(r"^chudware_[A-Za-z0-9_.\-]{1,200}$")
# Revision-stable AICM keys end in _r<digits> (aicm_handoff::key_for). Immutable for a year.
_AICM_REVID_RE = re.compile(r"_r\d+")


def _aicm_cache_headers(name: str) -> dict[str, str]:
    """Revisioned doc meshes are content-addressed; non-revisioned stay no-store."""
    if name and _AICM_REVID_RE.search(name):
        return {"Cache-Control": "private, max-age=31536000, immutable"}
    return {"Cache-Control": "no-store"}

_SHM_URI_PREFIX = "aicm-shm://"

# Durable recent-files list for the WASM client (config.json lives in /tmp there).
_RECENTS_NAME = ".chudware_recents.json"
_RECENTS_MAX = 10
# Per-account copilot chat store (same shape as client chat_history.json: root[key] = entry).
_CHAT_NAME = ".chudware_chat_history.json"
_CHAT_MAX_BODY = 5 * 1024 * 1024  # reject PUT bodies larger than 5 MiB


def _ws_root() -> Path:
    """Legacy single-tenant shared tree. Only reachable via admin scope=shared."""
    root = settings.workspace_root
    if not root or not Path(root).is_dir():
        raise HTTPException(status_code=503, detail="workspace root not configured")
    return Path(root).resolve()


def _caller_root(request: Request, scope: str = "") -> tuple[User, Path]:
    """Resolve the one root this caller is allowed to address.

    Default is the account's own tree, created on demand. ``scope=shared`` is
    an admin-only escape hatch onto the legacy shared tree; any other scope
    value is rejected so a typo cannot silently widen access.
    """
    user = get_current_user(request)
    if scope == "shared":
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="scope=shared requires admin")
        return user, _ws_root()
    if scope:
        raise HTTPException(status_code=400, detail="unknown scope")
    _ws_root()  # base must still be configured (503 on misconfig)
    root = settings.user_workspace_root(user_slug(user.email))
    root.mkdir(parents=True, exist_ok=True)
    return user, root.resolve()


def _resolve(root: Path, rel: str, *, must_exist: bool = True) -> Path:
    """Clamp a client-relative path under ``root`` (the caller's own tree).

    Containment is checked on the FULLY RESOLVED path, and a final-component
    symlink is always rejected, so a symlink planted inside the jail cannot be
    used to escape it (e.g. upload through link -> /etc, or rename over one).
    """
    rel = (rel or "").strip().replace("\\", "/")
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
async def workspace_list(request: Request, dir: str = "", scope: str = "") -> dict[str, Any]:
    _, root = _caller_root(request, scope)
    base = _resolve(root, dir)
    if not base.is_dir():
        raise HTTPException(status_code=400, detail="not a directory")
    dirs: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    for p in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if p.name.startswith("."):
            continue
        rel = str(p.relative_to(root))
        try:
            st = p.stat()
        except OSError:
            continue
        if p.is_dir():
            dirs.append({"name": p.name, "path": rel})
        else:
            files.append({"name": p.name, "path": rel, "size": st.st_size,
                          "mtime": int(st.st_mtime)})
    # "root" is the caller's OWN absolute jail root. The WASM client contract
    # (host/src/core/fs_backend.h) builds daemon doc_open/doc_save paths as
    # `root + entry path`, so blanking it silently breaks opening any file on
    # the web client. Telling a user their own directory is not the security
    # boundary — engine_bridge.py is, and it rejects any daemon RPC whose path
    # falls outside this same root.
    return {"root": str(root), "dir": str(base.relative_to(root)) if base != root else "",
            "dirs": dirs[:2000], "files": files[:2000]}


@router.get("/workspace/download")
async def workspace_download(request: Request, path: str, scope: str = "") -> FileResponse:
    _, root = _caller_root(request, scope)
    target = _resolve(root, path)
    if not target.is_file():
        raise HTTPException(status_code=400, detail="not a file")
    return FileResponse(str(target), filename=target.name,
                        headers={"Cache-Control": "no-store"})


@router.post("/workspace/upload")
async def workspace_upload(
    request: Request,
    file: UploadFile = File(...),
    dir: str = Form(""),
    scope: str = "",
) -> dict[str, Any]:
    user, root = _caller_root(request, scope)
    api_writes.enforce(user.email, "writes")
    uploads.enforce(user.email, "uploads")
    declared = _content_length(request)
    cap = enforce_upload_size(declared)
    enforce_disk_quota(root, declared or 0)
    base = _resolve(root, dir)
    if not base.is_dir():
        raise HTTPException(status_code=400, detail="not a directory")
    name = Path(file.filename or "upload.bin").name  # strip any client dirs
    if name.startswith(".") or not name:
        raise HTTPException(status_code=400, detail="bad filename")
    target = base / name
    if target.is_symlink():
        raise HTTPException(status_code=400, detail="symlink not allowed")
    size = 0
    try:
        with target.open("wb") as out:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                # Chunked uploads have no Content-Length — enforce the cap
                # DURING streaming and leave no partial file behind.
                if size > cap:
                    raise HTTPException(
                        status_code=413,
                        detail=f"upload too large — {cap // (1024 * 1024)} MiB max",
                    )
                out.write(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    log.info("workspace upload: %s (%d bytes) by %s", target, size, user.email)
    return {"ok": True, "path": str(target.relative_to(root)), "bytes": size}


@router.post("/workspace/mkdir")
async def workspace_mkdir(request: Request, body: dict[str, Any], scope: str = "") -> dict[str, Any]:
    user, root = _caller_root(request, scope)
    api_writes.enforce(user.email, "writes")
    target = _resolve(root, str(body.get("path", "")), must_exist=False)
    target.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "path": str(target.relative_to(root))}


@router.post("/workspace/rename")
async def workspace_rename(request: Request, body: dict[str, Any], scope: str = "") -> dict[str, Any]:
    user, root = _caller_root(request, scope)
    api_writes.enforce(user.email, "writes")
    src = _resolve(root, str(body.get("src", "")))
    dst = _resolve(root, str(body.get("dst", "")), must_exist=False)
    if dst.exists():
        raise HTTPException(status_code=409, detail="destination exists")
    src.rename(dst)
    return {"ok": True, "path": str(dst.relative_to(root))}


@router.post("/workspace/delete")
async def workspace_delete(request: Request, body: dict[str, Any], scope: str = "") -> dict[str, Any]:
    user, root = _caller_root(request, scope)
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
async def workspace_copy(request: Request, body: dict[str, Any], scope: str = "") -> dict[str, Any]:
    """Copy a file or (when recursive) a directory tree within the caller's workspace."""
    user, root = _caller_root(request, scope)
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
        # Ensure parent of dst exists; copytree creates dst itself.
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
    else:
        if not src.is_file():
            raise HTTPException(status_code=400, detail="not a file")
        if dst.exists() and dst.is_dir():
            dst = dst / src.name
            if root not in dst.resolve().parents and dst.resolve() != root:
                raise HTTPException(status_code=400, detail="path escapes workspace")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    log.info("workspace copy: %s -> %s by %s", src, dst, user.email)
    return {"ok": True, "path": str(dst.relative_to(root))}


@router.get("/workspace/recents")
async def workspace_recents_get(request: Request, scope: str = "") -> dict[str, Any]:
    """Load the caller's durable recent-files list (<root>/.chudware_recents.json)."""
    _, root = _caller_root(request, scope)
    path = root / _RECENTS_NAME
    if not path.is_file():
        return {"files": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("recents read failed: %s", e)
        return {"files": []}
    files: list[str] = []
    raw = data.get("files", []) if isinstance(data, dict) else []
    if isinstance(raw, list):
        for e in raw:
            if isinstance(e, str) and e.strip():
                files.append(e.strip())
            if len(files) >= _RECENTS_MAX:
                break
    return {"files": files}


@router.put("/workspace/recents")
async def workspace_recents_put(request: Request, body: dict[str, Any], scope: str = "") -> dict[str, Any]:
    """Persist the caller's recent-files list inside their own workspace root."""
    user, root = _caller_root(request, scope)
    api_writes.enforce(user.email, "writes")
    raw = body.get("files", []) if isinstance(body, dict) else []
    files: list[str] = []
    if isinstance(raw, list):
        seen: set[str] = set()
        for e in raw:
            if not isinstance(e, str):
                continue
            p = e.strip()
            if not p or p in seen:
                continue
            seen.add(p)
            files.append(p)
            if len(files) >= _RECENTS_MAX:
                break
    path = root / _RECENTS_NAME
    path.write_text(json.dumps({"files": files}, indent=2), encoding="utf-8")
    log.info("workspace recents: wrote %d entries for %s", len(files), user.email)
    return {"ok": True, "files": files}


def _chat_path(root: Path) -> Path:
    return root / _CHAT_NAME


def _load_chat_store(root: Path) -> dict[str, Any]:
    """Load the caller's own chat store; missing/corrupt → empty object."""
    path = _chat_path(root)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("chat store read failed: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON via tmp + replace so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


@router.get("/workspace/chat")
async def workspace_chat_get(request: Request, scope: str = "") -> dict[str, Any]:
    """Full chat_history store for the CALLER (<root>/.chudware_chat_history.json).

    Shape matches the WASM/desktop client file: root[docKey] = {active, conversations}.
    Missing file → empty object so clients can merge cleanly.
    """
    _, root = _caller_root(request, scope)
    return _load_chat_store(root)


@router.put("/workspace/chat")
async def workspace_chat_put(request: Request, scope: str = "") -> dict[str, Any]:
    """Merge a partial {key: entry} map into the CALLER's own chat store.

    Client PUTs one or a few keys; server deep-replaces only those keys
    (load → update → atomic write). The store lives inside the caller's own
    root, so no caller can ever read or overwrite another account's history.
    """
    user, root = _caller_root(request, scope)
    api_writes.enforce(user.email, "writes")
    declared = _content_length(request)
    if declared is not None and declared > _CHAT_MAX_BODY:
        raise HTTPException(status_code=413, detail="chat body too large (max 5MB)")
    raw = await request.body()
    if len(raw) > _CHAT_MAX_BODY:
        raise HTTPException(status_code=413, detail="chat body too large (max 5MB)")
    if not raw:
        raise HTTPException(status_code=400, detail="empty body")
    try:
        body = json.loads(raw)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {e}") from e
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    # Only accept object-valued entries (skip null / scalar noise).
    patch: dict[str, Any] = {}
    for k, v in body.items():
        if not isinstance(k, str) or not k:
            continue
        if isinstance(v, dict):
            patch[k] = v
    if not patch:
        raise HTTPException(status_code=400, detail="no valid chat entries in body")
    store = _load_chat_store(root)
    for k, v in patch.items():
        store[k] = v  # replace key wholesale (entry object)
    _atomic_write_json(_chat_path(root), store)
    log.info("workspace chat: merged %d key(s) for %s, store size %d",
             len(patch), user.email, len(store))
    return {"ok": True, "merged": list(patch.keys()), "keys": len(store)}


def _aicm_allowlist(caller_root: Path | None) -> list[Path]:
    dirs = [
        Path(tempfile.gettempdir()) / "chudware_cpp_docs",  # aicm_handoff publish_file
        Path.home() / ".local" / "share" / "Chudware",     # hostd state dir (autosave etc.)
    ]
    if caller_root is not None:
        # Only the caller's OWN tree — the legacy root also contains every
        # other account's users/<slug>/ subtree, so it is never allowlisted
        # for anyone but an admin who explicitly passed scope=shared.
        dirs.append(caller_root)
    return [d.resolve() for d in dirs if d.exists()]


@router.get("/files/aicm")
async def files_aicm(request: Request, path: str = "", shm: str = "", scope: str = "") -> FileResponse:
    """Serve one daemon-published tessellation payload (file or shm object)."""
    if path.startswith(_SHM_URI_PREFIX):
        shm, path = path[len(_SHM_URI_PREFIX):], ""
    if shm:
        if not _SHM_NAME_RE.match(shm):
            raise HTTPException(status_code=400, detail="bad shm name")
        target = Path("/dev/shm") / shm
        if not target.is_file():
            raise HTTPException(status_code=404, detail="shm object gone")
        return FileResponse(str(target), media_type="application/octet-stream",
                            headers=_aicm_cache_headers(shm))
    if not path:
        raise HTTPException(status_code=400, detail="path or shm required")
    try:
        _, caller_root = _caller_root(request, scope)
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        caller_root = None  # workspace unconfigured — shm/tmp payloads still work
    try:
        target = Path(path).resolve(strict=True)
    except OSError:
        raise HTTPException(status_code=404, detail="not found")
    if not any(target == d or d in target.parents for d in _aicm_allowlist(caller_root)):
        raise HTTPException(status_code=403, detail="path not allowlisted")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="not a file")
    return FileResponse(str(target), media_type="application/octet-stream",
                        headers=_aicm_cache_headers(target.name))
