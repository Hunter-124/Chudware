"""File-drop bucket API (/api/bucket/*) — personal and shared team trees.

Two roots, and a request addresses exactly one of them:

* **personal** (``?scope=personal``) — ``cells.personal_bucket_root(email)``,
  created on demand, reachable only by that account.
* **team** (``?team=<uuid>``) — ``bucket_root/orgs/<org>/teams/<team>``, shared
  by the team's members plus the organization's owner/admin. Membership is
  re-resolved from PostgreSQL on every request (``orgs.resolve_team_bucket``),
  so a removal, a downgrade, or a suspension takes effect immediately; the
  quota that governs it belongs to the organization's owner, while rate limits
  stay keyed on the acting account.
Legacy shared trees are not exposed; every request has a personal/team identity.

Every path is clamped under whichever of those roots was resolved, and the two
uuid components of a team root are validated as canonical UUID text before they
are joined onto a filesystem path. The selected team tree is shared with
/workspace/* and the engine. Verified university identities default to the
configured Box-backed team when no explicit request/browser selection exists.
Filesystem handlers are synchronous FastAPI routes: remote I/O and quota scans
belong in the threadpool rather than blocking every tenant's event loop.

The page itself is served by ``portal_serve.py`` — this module is API only.
"""

from __future__ import annotations

import logging
import shutil
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.datastructures import UploadFile
from starlette.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

try:
    from . import cells, orgs
    from .config import settings
    from .auth import User, get_current_user
    from .accounts import get_account
    from .limits import (
        api_writes,
        enforce_disk_quota,
        storage_write_lock,
        enforce_upload_size,
        uploads,
        bounded_form,
        stream_open_file,
        store_upload,
        list_open_directory,
    )
except ImportError:  # uvicorn app:app from package dir
    import cells
    import orgs  # type: ignore
    from config import settings
    from auth import User, get_current_user
    from accounts import get_account
    from limits import (
        api_writes,
        enforce_disk_quota,
        storage_write_lock,
        enforce_upload_size,
        uploads,
        bounded_form,
        stream_open_file,
        store_upload,
        list_open_directory,
    )

log = logging.getLogger("chudware-web.bucket")

router = APIRouter()


@dataclass(frozen=True, slots=True)
class _Scope:
    """The one tree this request may address, and who pays for it."""

    user: User
    root: Path
    kind: str  # personal | team | shared
    team: orgs.TeamBucket | None

    def quota_target(self) -> tuple[str, str]:
        """(email, plan) whose storage limit governs writes into this tree.

        For a team tree that is the organization's owner — the shared quota is
        the owner's plan, not whichever member happens to be uploading.
        """
        if self.team is not None:
            return self.team.owner_email, self.team.owner_plan
        account = get_account(self.user.email)
        return self.user.email, getattr(account, "plan", "trial")

    def context(self) -> dict[str, Any]:
        return {
            "scope": self.kind,
            "team": None if self.team is None else self.team.context(),
        }


def _bucket_root() -> Path:
    """Require the configured base for personal/team bucket storage."""
    root = settings.bucket_root
    if not root or not Path(root).is_dir():
        raise HTTPException(status_code=503, detail="bucket root not configured")
    return Path(root).resolve()


def _caller_scope(
    request: Request, scope: str = "", team: str = "", *, write: bool = False
) -> _Scope:
    """Resolve the one root this caller is allowed to address.

    Default is the account's personal tree; team selections are revalidated.
    The selectors are mutually exclusive; legacy shared storage is not exposed.
    """
    user = get_current_user(request)
    team = (team or "").strip()
    if scope and team:
        raise HTTPException(status_code=400, detail="scope and team are exclusive")
    if scope == "shared":
        raise HTTPException(status_code=403, detail="shared scope unavailable")
    if scope and scope != "personal":
        raise HTTPException(status_code=400, detail="unknown scope")
    team = orgs.selected_workspace_team(request, user.email, scope, team)
    _bucket_root()  # base must still be configured (503 on misconfig)
    if team:
        bucket = orgs.resolve_team_bucket(user.email, team, write=write)
        principal = cells.resolve_workspace(user.email, team)
        if write and principal.right not in {"owner", "manager", "editor"}:
            raise HTTPException(status_code=403, detail="forbidden_right")
        root = principal.root
        root.mkdir(parents=True, exist_ok=True)
        return _Scope(user=user, root=root.absolute(), kind="team", team=bucket)
    root = cells.personal_bucket_root(user.email)
    root.mkdir(parents=True, exist_ok=True)
    fd = cells.directory_beneath(root)
    os.close(fd)
    return _Scope(user=user, root=root.absolute(), kind="personal", team=None)


def _resolve(root: Path, rel: str, *, must_exist: bool = True) -> Path:
    """Clamp a client-relative path under ``root`` (the resolved scope's tree).

    Containment is checked on the FULLY RESOLVED path, and a final-component
    symlink is always rejected, so a symlink planted inside the jail cannot be
    used to escape it.
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
        raise HTTPException(status_code=400, detail="path escapes bucket")
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


@router.get("/api/bucket/list")
def bucket_list(
    request: Request, dir: str = "", scope: str = "", team: str = ""
) -> dict[str, Any]:
    resolved = _caller_scope(request, scope, team)
    root = resolved.root
    directory = dir.strip().replace("\\", "/").removeprefix("./")
    if directory == ".":
        directory = ""
    dirs, files = list_open_directory(cells.directory_beneath(root, directory), directory, 10000)
    return {"root": ".", "dir": directory,
            "dirs": dirs[:5000], "files": files[:5000], **resolved.context()}


@router.get("/api/bucket/download")
def bucket_download(
    request: Request, path: str, scope: str = "", team: str = ""
) -> StreamingResponse:
    resolved = _caller_scope(request, scope, team)
    fd, st = cells.open_beneath(resolved.root, path, max_bytes=settings.max_upload_bytes)
    return stream_open_file(fd, st, filename=Path(path).name, headers={"Cache-Control": "no-store"})


@router.post("/api/bucket/upload")
async def bucket_upload(request: Request) -> dict[str, Any]:
    """Admit and bound the complete multipart stream before parser buffering."""
    resolved = await run_in_threadpool(
        _caller_scope, request, request.query_params.get("scope", ""),
        request.query_params.get("team", ""), write=True
    )
    api_writes.enforce(resolved.user.email, "writes")
    uploads.enforce(resolved.user.email, "uploads")
    cap = enforce_upload_size(_content_length(request))
    quota_email, quota_plan = resolved.quota_target()
    await run_in_threadpool(enforce_disk_quota, quota_email, quota_plan, resolved.root)
    async with bounded_form(request, cap, max_files=1000) as form:
        files = form.getlist("files")
        directory = form.get("dir", "")
        if not files or any(not isinstance(file, UploadFile) for file in files):
            raise HTTPException(status_code=422, detail="files required")
        if not isinstance(directory, str) or len(directory) > 4096:
            raise HTTPException(status_code=400, detail="bad directory")
        return await run_in_threadpool(_store_uploads, resolved, files, directory, cap)


def _store_uploads(resolved: _Scope, files: list[UploadFile], dir: str, cap: int) -> dict[str, Any]:
    with storage_write_lock(resolved.root):
        user, root = resolved.user, resolved.root
        quota_email, quota_plan = resolved.quota_target()
        base = _resolve(root, dir, must_exist=False)
        uploaded: list[dict[str, Any]] = []
        skipped: list[str] = []
        total = 0
        for file in files:
            raw = (file.filename or "").strip().replace("\\", "/")
            parts = [p for p in raw.split("/") if p not in ("", ".")]
            if not parts or raw.startswith("/") or (len(raw) > 1 and raw[1] == ":") or any(p == ".." for p in parts):
                skipped.append(file.filename or "")
                continue
            relative = str(base.joinpath(*parts).relative_to(root))
            try:
                size = store_upload(root, relative, file, cap=cap, email=quota_email,
                                    plan=quota_plan, total=total, create_parents=True)
            except HTTPException as exc:
                if exc.status_code in {400, 403}:
                    skipped.append(raw)
                    continue
                raise
            total += size
            uploaded.append({"path": relative, "bytes": size})
        if not uploaded and skipped:
            raise HTTPException(status_code=400, detail=f"no valid files (skipped: {', '.join(skipped[:5])})")
        return {"ok": True, "uploaded": uploaded, "skipped": skipped}


@router.post("/api/bucket/mkdir")
def bucket_mkdir(request: Request, body: dict[str, Any], scope: str = "", team: str = "") -> dict[str, Any]:
    
    resolved = _caller_scope(request, scope, team, write=True)
    with storage_write_lock(resolved.root):
        user, root = resolved.user, resolved.root
        api_writes.enforce(user.email, "writes")
        target = _resolve(root, str(body.get("path", "")), must_exist=False)
        target.mkdir(parents=True, exist_ok=True)
        log.info("bucket mkdir: %s by %s", target, user.email)
        return {"ok": True, "path": str(target.relative_to(root))}


@router.post("/api/bucket/rename")
def bucket_rename(request: Request, body: dict[str, Any], scope: str = "", team: str = "") -> dict[str, Any]:
    
    resolved = _caller_scope(request, scope, team, write=True)
    with storage_write_lock(resolved.root):
        user, root = resolved.user, resolved.root
        api_writes.enforce(user.email, "writes")
        src = _resolve(root, str(body.get("src", "")))
        # _resolve maps an empty/missing rel onto the scope root itself, which list
        # and mkdir legitimately want. Rename does not: with src == root,
        # ``src.parent / new_name`` lands one level ABOVE the jail -- in the shared
        # users/ (or orgs/) parent, next to every other tenant's bucket -- and the
        # rename then MOVES the caller's entire bucket there. A single
        # ``{"src": ""}`` was enough. bucket_delete already refuses the root; this
        # is the same guard for the same reason.
        if src == root:
            raise HTTPException(status_code=400, detail="cannot rename bucket root")
        new_name = Path(str(body.get("name", ""))).name
        if not new_name or new_name.startswith("."):
            raise HTTPException(status_code=400, detail="bad name")
        dst = src.parent / new_name
        # Containment is re-checked on the destination rather than assumed from
        # src: every write helper in this module clamps the path it is about to
        # touch, and a rename touches two.
        if root not in dst.parents:
            raise HTTPException(status_code=400, detail="path escapes bucket")
        if dst.is_symlink():
            raise HTTPException(status_code=400, detail="symlink not allowed")
        if dst.exists():
            raise HTTPException(status_code=409, detail="already exists")
        src.rename(dst)
        log.info("bucket rename: %s -> %s by %s", src, dst, user.email)
        return {"ok": True, "path": str(dst.relative_to(root))}


@router.post("/api/bucket/delete")
def bucket_delete(request: Request, body: dict[str, Any], scope: str = "", team: str = "") -> dict[str, Any]:
    
    resolved = _caller_scope(request, scope, team, write=True)
    with storage_write_lock(resolved.root):
        user, root = resolved.user, resolved.root
        api_writes.enforce(user.email, "writes")
        target = _resolve(root, str(body.get("path", "")))
        if target == root:
            raise HTTPException(status_code=400, detail="cannot delete bucket root")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        log.info("bucket delete: %s by %s", target, user.email)
        return {"ok": True}
