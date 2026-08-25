"""File-drop bucket API (/api/bucket/*) — personal and shared team trees.

Three roots, and a request addresses exactly one of them:

* **personal** (default) — ``settings.user_bucket_root(user_slug(email))``,
  created on demand, reachable only by that account.
* **team** (``?team=<uuid>``) — ``bucket_root/orgs/<org>/teams/<team>``, shared
  by the team's members plus the organization's owner/admin. Membership is
  re-resolved from PostgreSQL on every request (``orgs.resolve_team_bucket``),
  so a removal, a downgrade, or a suspension takes effect immediately; the
  quota that governs it belongs to the organization's owner, while rate limits
  stay keyed on the acting account.
* **legacy shared** (``?scope=shared``) — the pre-tenant single-tenant tree,
  admin-only, never moved or deleted.

Every path is clamped under whichever of those roots was resolved, and the two
uuid components of a team root are validated as canonical UUID text before they
are joined onto a filesystem path. Independent of /workspace/* (the CAD
engine's live doc tree + .aicm mesh cache): keeping uploads out of that tree
avoids colliding with autosave/mesh cache housekeeping there.

The page itself is served by ``portal_serve.py`` — this module is API only.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

try:
    from . import orgs
    from .config import settings
    from .auth import User, get_current_user
    from .accounts import get_account
    from .limits import (
        api_writes,
        enforce_disk_quota,
        enforce_upload_size,
        uploads,
        user_slug,
    )
except ImportError:  # uvicorn app:app from package dir
    import orgs  # type: ignore
    from config import settings
    from auth import User, get_current_user
    from accounts import get_account
    from limits import (
        api_writes,
        enforce_disk_quota,
        enforce_upload_size,
        uploads,
        user_slug,
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
    """Legacy single-tenant shared tree. Only reachable via admin scope=shared."""
    root = settings.bucket_root
    if not root or not Path(root).is_dir():
        raise HTTPException(status_code=503, detail="bucket root not configured")
    return Path(root).resolve()


def _caller_scope(
    request: Request, scope: str = "", team: str = "", *, write: bool = False
) -> _Scope:
    """Resolve the one root this caller is allowed to address.

    Default is the account's own tree, created on demand. ``team=<uuid>``
    selects a shared team tree the caller is currently entitled to, and
    ``scope=shared`` is the admin-only escape hatch onto the legacy tree. The
    two selectors are mutually exclusive and any other scope value is rejected,
    so neither a typo nor a combination can silently widen access.
    """
    user = get_current_user(request)
    team = (team or "").strip()
    if scope and team:
        raise HTTPException(status_code=400, detail="scope and team are exclusive")
    if scope == "shared":
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="scope=shared requires admin")
        return _Scope(user=user, root=_bucket_root(), kind="shared", team=None)
    if scope:
        raise HTTPException(status_code=400, detail="unknown scope")
    _bucket_root()  # base must still be configured (503 on misconfig)
    if team:
        bucket = orgs.resolve_team_bucket(user.email, team, write=write)
        root = bucket.root
        root.mkdir(parents=True, exist_ok=True)
        return _Scope(user=user, root=root.resolve(), kind="team", team=bucket)
    root = settings.user_bucket_root(user_slug(user.email))
    root.mkdir(parents=True, exist_ok=True)
    return _Scope(user=user, root=root.resolve(), kind="personal", team=None)


def _resolve(root: Path, rel: str, *, must_exist: bool = True) -> Path:
    """Clamp a client-relative path under ``root`` (the resolved scope's tree).

    Containment is checked on the FULLY RESOLVED path, and a final-component
    symlink is always rejected, so a symlink planted inside the jail cannot be
    used to escape it.
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
async def bucket_list(
    request: Request, dir: str = "", scope: str = "", team: str = ""
) -> dict[str, Any]:
    resolved = _caller_scope(request, scope, team)
    root = resolved.root
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
    # Caller's OWN absolute jail root — same WASM client contract as
    # /workspace/list (host/src/core/fs_backend.h). The scope block is
    # additive: existing consumers keep reading root/dir/dirs/files.
    return {"root": str(root), "dir": str(base.relative_to(root)) if base != root else "",
            "dirs": dirs[:5000], "files": files[:5000], **resolved.context()}


@router.get("/api/bucket/download")
async def bucket_download(
    request: Request, path: str, scope: str = "", team: str = ""
) -> FileResponse:
    resolved = _caller_scope(request, scope, team)
    target = _resolve(resolved.root, path)
    if not target.is_file():
        raise HTTPException(status_code=400, detail="not a file")
    return FileResponse(str(target), filename=target.name,
                        headers={"Cache-Control": "no-store"})


@router.post("/api/bucket/upload")
async def bucket_upload(
    request: Request,
    files: list[UploadFile] = File(...),
    dir: str = Form(""),
    scope: str = "",
    team: str = "",
) -> dict[str, Any]:
    """Accept flat files or a whole dropped/selected folder tree.

    Each UploadFile's filename may be a nested relative path
    (``sub/dir/name.ext``, forward-slashed) when the client uploaded a
    folder — the browser File API and our frontend both preserve that via
    ``File.webkitRelativePath`` / ``FormData.append(name, file, path)``.
    Path segments are clamped under the target dir the same way ``_resolve``
    clamps client dir paths; anything that tries to escape is skipped rather
    than aborting the whole batch, so one bad entry in a big folder drop
    doesn't lose everything else.
    """
    resolved = _caller_scope(request, scope, team, write=True)
    user, root = resolved.user, resolved.root
    # Rate limits stay per acting account even on a shared tree: one member
    # must not be able to spend the whole team's request budget.
    api_writes.enforce(user.email, "writes")
    uploads.enforce(user.email, "uploads")
    declared = _content_length(request)
    cap = enforce_upload_size(declared)
    quota_email, quota_plan = resolved.quota_target()
    enforce_disk_quota(
        quota_email,
        quota_plan,
        root,
        incoming_bytes=declared or 0,
    )
    base = _resolve(root, dir, must_exist=False)
    base.mkdir(parents=True, exist_ok=True)
    uploaded: list[dict[str, Any]] = []
    skipped: list[str] = []
    total = 0
    for file in files:
        raw = (file.filename or "").strip().replace("\\", "/").lstrip("/")
        parts = [p for p in raw.split("/") if p not in ("", ".")]
        if not parts or any(p == ".." for p in parts):
            skipped.append(file.filename or "")
            continue
        candidate = base.joinpath(*parts)
        if candidate.is_symlink():
            skipped.append(raw)
            continue
        target = candidate.resolve()
        if target != root and root not in target.parents:
            skipped.append(raw)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        try:
            with target.open("wb") as out:
                while chunk := await file.read(1 << 20):
                    size += len(chunk)
                    total += len(chunk)
                    # Chunked uploads have no Content-Length — enforce the cap
                    # DURING streaming and leave no partial file behind.
                    if size > cap or total > cap:
                        raise HTTPException(
                            status_code=413,
                            detail=f"upload too large — {cap // (1024 * 1024)} MiB max",
                        )
                    out.write(chunk)
            enforce_disk_quota(quota_email, quota_plan, root)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        log.info("bucket upload: %s (%d bytes) by %s", target, size, user.email)
        uploaded.append({"path": str(target.relative_to(root)), "bytes": size})
    if not uploaded and skipped:
        raise HTTPException(status_code=400, detail=f"no valid files (skipped: {', '.join(skipped[:5])})")
    return {"ok": True, "uploaded": uploaded, "skipped": skipped}


@router.post("/api/bucket/mkdir")
async def bucket_mkdir(
    request: Request, body: dict[str, Any], scope: str = "", team: str = ""
) -> dict[str, Any]:
    resolved = _caller_scope(request, scope, team, write=True)
    user, root = resolved.user, resolved.root
    api_writes.enforce(user.email, "writes")
    target = _resolve(root, str(body.get("path", "")), must_exist=False)
    target.mkdir(parents=True, exist_ok=True)
    log.info("bucket mkdir: %s by %s", target, user.email)
    return {"ok": True, "path": str(target.relative_to(root))}


@router.post("/api/bucket/rename")
async def bucket_rename(
    request: Request, body: dict[str, Any], scope: str = "", team: str = ""
) -> dict[str, Any]:
    resolved = _caller_scope(request, scope, team, write=True)
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
async def bucket_delete(
    request: Request, body: dict[str, Any], scope: str = "", team: str = ""
) -> dict[str, Any]:
    resolved = _caller_scope(request, scope, team, write=True)
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
