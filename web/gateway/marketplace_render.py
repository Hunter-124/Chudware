"""Read-only seller-cell previews; no shared engine or ambient credentials."""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import os
import secrets
import struct
import threading
import uuid
import zlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, Request

try:
    from . import cells, engine_bridge, deployment_drain
    from .config import settings
    from .db import tx
    from .limits import api_writes, enforce_disk_quota, storage_write_lock, store_upload
    from .marketplace_authz import marketplace_enabled_or_404, require_user, MarketplaceError
    from .marketplace_schema import RENDERABLE_EXTENSIONS
except ImportError:
    import cells, engine_bridge, deployment_drain
    from config import settings
    from db import tx
    from limits import api_writes, enforce_disk_quota, storage_write_lock, store_upload
    from marketplace_authz import marketplace_enabled_or_404, require_user, MarketplaceError
    from marketplace_schema import RENDERABLE_EXTENSIONS

router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])
log = logging.getLogger("chudware.marketplace.render")
_MAX_PNG_BYTES = 8 << 20
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="seller-preview")
_jobs: dict[str, concurrent.futures.Future] = {}
_jobs_lock = threading.Lock()


def _staged_path(listing_id: str, asset_id: str, filename: str) -> str:
    listing_id, asset_id = str(uuid.UUID(str(listing_id))), str(uuid.UUID(str(asset_id)))
    if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise HTTPException(400, "invalid asset filename")
    return f".marketplace/{listing_id}/{asset_id}/{filename}"


def stage_listing_asset(seller_email: str, listing_id: str, asset_id: str,
                        source_relative: str, filename: str) -> str:
    """Snapshot renderable uploads into the seller cell without changing download storage."""
    if not filename.lower().endswith(RENDERABLE_EXTENSIONS):
        return ""
    relative = _staged_path(listing_id, asset_id, filename)
    principal = cells.resolve_workspace(seller_email)
    fd, info = cells.open_beneath(settings.listing_asset_root(str(uuid.UUID(str(listing_id)))),
                                  source_relative, max_bytes=settings.marketplace_max_asset_bytes)
    with os.fdopen(fd, "rb") as source, storage_write_lock(principal.root):
        store_upload(principal.root, relative, SimpleNamespace(file=source, size=info.st_size),
                     cap=settings.marketplace_max_asset_bytes, email=seller_email,
                     plan=principal.plan, create_parents=True)
    return relative


def cleanup_listing_asset_stage(seller_email: str, listing_id: str, asset_id: str,
                                filename: str) -> None:
    relative = _staged_path(listing_id, asset_id, filename)
    try:
        parent = cells.directory_beneath(cells.personal_root(seller_email), relative.rsplit("/", 1)[0])
        try:
            os.unlink(filename, dir_fd=parent)
        finally:
            os.close(parent)
    except HTTPException as exc:
        if exc.status_code != 404:
            log.warning("could not remove staged marketplace asset %s", asset_id)
    except FileNotFoundError:
        pass


def _mkdir_beneath(root: Path, relative: str = "") -> int:
    """Create and pin a hierarchy without following even its configured ancestors."""
    path = root.absolute()
    parts = list(path.parts[1:]) + (relative.split("/") if relative else [])
    if any(part in {"", ".", ".."} or "\\" in part for part in parts):
        raise HTTPException(403, "unsafe preview directory")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_png(root: Path, relative: str) -> tuple[bytes, int, int]:
    fd, info = cells.open_beneath(root, relative, max_bytes=_MAX_PNG_BYTES)
    with os.fdopen(fd, "rb") as file:
        data = file.read(_MAX_PNG_BYTES + 1)
    if len(data) != info.st_size or len(data) > _MAX_PNG_BYTES or len(data) < 33:
        raise HTTPException(502, "invalid preview image")
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[8:16] != b"\x00\x00\x00\rIHDR":
        raise HTTPException(502, "invalid preview image")
    width, height = struct.unpack(">II", data[16:24])
    if not 0 < width <= 4096 or not 0 < height <= 4096:
        raise HTTPException(502, "oversized preview dimensions")
    offset, image_data, ended = 8, False, False
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset:offset + 4], "big")
        kind = data[offset + 4:offset + 8]
        if length > _MAX_PNG_BYTES or offset + length + 12 > len(data):
            raise HTTPException(502, "invalid preview image")
        chunk = data[offset + 4:offset + 8 + length]
        checksum = int.from_bytes(data[offset + 8 + length:offset + 12 + length], "big")
        if zlib.crc32(chunk) & 0xffffffff != checksum:
            raise HTTPException(502, "invalid preview image")
        image_data |= kind == b"IDAT"
        offset += length + 12
        if kind == b"IEND":
            ended = length == 0 and offset == len(data)
            break
    if not image_data or not ended:
        raise HTTPException(502, "invalid preview image")
    return data, width, height


async def _render_in_cell(principal, seller_email: str, source: str, output_dir: str):
    principal = replace(principal, right="viewer")
    reader, writer = await cells.connect_cell(principal)
    token = None
    nonce = principal.hello()
    ident = 0
    token = cells.register(principal, lambda epoch: writer.close())
    async def call(method, params):
        nonlocal ident
        current = await asyncio.to_thread(cells.resolve_workspace, seller_email)
        if current.workspace_key != principal.workspace_key or current.epoch != principal.epoch:
            raise HTTPException(403, "revoked")
        rule = cells.method_policy().get(method)
        if not rule or rule["cell"] != "allow" or rule["right"] not in cells.RIGHTS["viewer"]:
            raise HTTPException(403, "cell_refused")
        ident += 1
        writer.write(json.dumps({"id": ident, "method": method, "params": params},
                                separators=(",", ":"), allow_nan=False).encode() + b"\n")
        await writer.drain()
        for _ in range(128):
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=60)
            except (ValueError, asyncio.LimitOverrunError) as exc:
                raise HTTPException(502, "oversized cell reply") from exc
            if not raw:
                raise HTTPException(502, "cell disconnected")
            reply = engine_bridge.parse_cell_frame(raw)
            if not reply or reply.get("id") != ident:
                continue
            result = reply.get("result")
            if "error" in reply or not isinstance(result, dict) or result.get("ok", True) is False:
                raise HTTPException(502, "cell preview request failed")
            return result
        raise HTTPException(502, "invalid cell replies")
    try:
        await call("hello", {"client": "gui", "proto": 1, "principal": nonce})
        opened = await call("doc_open", {"path": source, "detach_path": True})
        doc = opened.get("doc_id")
        if not isinstance(doc, str) or not 0 < len(doc) <= 180:
            raise HTTPException(502, "invalid cell document")
        outputs = [("doc_render_png", "thumbnail", "thumbnail.png", {}),
                   ("doc_render_contact_sheet", "contact_sheet", "contact-sheet.png",
                    {"views": ["iso", "front", "right", "top"]})]
        for method, role, name, extra in outputs:
            await call(method, {"doc_id": doc, "out_path": f"{output_dir}/{name}", **extra})
        # Ignore all returned paths: only gateway-minted output names are readable.
        return [(role, name) for _, role, name, _ in outputs]
    finally:
        if token:
            cells.unregister(principal, token)
        writer.close()
        await writer.wait_closed()


def _publish_previews(listing_id, seller_email, principal, output_dir, outputs):
    previews = []
    listing_root = settings.listing_asset_root(listing_id)
    destination = f"renders/{secrets.token_hex(16)}"
    parent = _mkdir_beneath(listing_root, destination)
    names = []
    try:
        for role, name in outputs:
            data, width, height = _read_png(principal.root, f"{output_dir}/{name}")
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                         0o600, dir_fd=parent)
            names.append(name)
            with os.fdopen(fd, "wb") as file:
                file.write(data)
            previews.append({"id": str(uuid.uuid4()), "role": role, "filename": name,
                             "rel_path": f"{destination}/{name}", "bytes": len(data),
                             "width": width, "height": height, "sha256": hashlib.sha256(data).hexdigest()})
        current = cells.resolve_workspace(seller_email)
        if current.epoch != principal.epoch or current.workspace_key != principal.workspace_key:
            raise HTTPException(403, "revoked")
        with tx() as cur:
            cur.execute("SELECT status FROM accounts WHERE uid=%s FOR SHARE", (principal.account_uid,))
            account = cur.fetchone()
            cur.execute("SELECT epoch FROM cell_principal_epochs WHERE workspace_key=%s AND actor_key=%s FOR SHARE",
                        (principal.workspace_key, principal.actor_key))
            epoch = cur.fetchone()
            if not account or account["status"] != "active" or not epoch or epoch["epoch"] != principal.epoch:
                raise HTTPException(403, "revoked")
            cur.execute("SELECT seller_email,thumbnail_asset_id FROM mk_listings WHERE id=%s FOR UPDATE",
                        (listing_id,))
            listing = cur.fetchone()
            if not listing or listing["seller_email"] != seller_email:
                raise HTTPException(404, "listing_not_found")
            cur.execute("SELECT email FROM mk_sellers WHERE email=%s FOR UPDATE", (seller_email,))
            cur.execute("SELECT id,role,rel_path FROM mk_assets WHERE listing_id=%s AND kind='render'", (listing_id,))
            old = cur.fetchall()
            old_ids = [row["id"] for row in old]
            thumbnail = listing["thumbnail_asset_id"]
            keep_thumbnail = thumbnail is not None and thumbnail not in old_ids
            if old_ids:
                cur.execute("DELETE FROM mk_assets WHERE listing_id=%s AND kind='render'", (listing_id,))
            cur.execute("SELECT COALESCE(sum(a.bytes),0) used FROM mk_assets a "
                        "JOIN mk_listings l ON l.id=a.listing_id WHERE l.seller_email=%s", (seller_email,))
            if int(cur.fetchone()["used"]) + sum(p["bytes"] for p in previews) > settings.marketplace_seller_quota_bytes:
                raise HTTPException(413, "seller asset quota exceeded")
            for preview in previews:
                role = "gallery" if preview["role"] == "thumbnail" and keep_thumbnail else preview["role"]
                cur.execute("INSERT INTO mk_assets(id,listing_id,kind,role,filename,rel_path,content_type,format,"
                            "bytes,sha256,width,height,gated) VALUES(%s,%s,'render',%s,%s,%s,'image/png','png',"
                            "%s,%s,%s,%s,FALSE)", (preview["id"], listing_id, role, preview["filename"],
                            preview["rel_path"], preview["bytes"], preview["sha256"], preview["width"], preview["height"]))
            if not keep_thumbnail:
                thumbnail = previews[0]["id"]
                cur.execute("UPDATE mk_listings SET thumbnail_asset_id=%s WHERE id=%s", (thumbnail, listing_id))
        for obsolete in old:
            try:
                directory, name = obsolete["rel_path"].rsplit("/", 1)
                old_parent = cells.directory_beneath(listing_root, directory)
                try:
                    os.unlink(name, dir_fd=old_parent)
                finally:
                    os.close(old_parent)
            except (HTTPException, OSError, ValueError):
                log.warning("could not remove obsolete preview asset %s", obsolete["id"])
        return {"listing_id": listing_id, "ok": True, "asset_ids": [p["id"] for p in previews],
                "thumbnail_asset_id": str(thumbnail)}
    except BaseException:
        for name in names:
            os.unlink(name, dir_fd=parent)
        raise
    finally:
        os.close(parent)


def render_listing_previews(listing_id: str) -> dict:
    with deployment_drain.work():
        return _render_listing_previews(listing_id)


def _render_listing_previews(listing_id: str) -> dict:
    canonical = str(uuid.UUID(str(listing_id)))
    with tx() as cur:
        cur.execute("SELECT seller_email FROM mk_listings WHERE id=%s", (canonical,))
        listing = cur.fetchone()
        if not listing:
            raise MarketplaceError(404, "listing_not_found", "listing not found")
        cur.execute("SELECT id,filename,rel_path FROM mk_assets WHERE listing_id=%s AND kind IN ('source','export') "
                    "ORDER BY (role='primary') DESC, (kind='source') DESC, sort_order,id", (canonical,))
        assets = cur.fetchall()
    asset = next((row for row in assets if row["filename"].lower().endswith(RENDERABLE_EXTENSIONS)), None)
    if asset is None:
        return {"listing_id": canonical, "ok": False, "reason": "unsupported_format",
                "detail": "Upload a renderable CAD file or a seller preview image."}
    seller_email = listing["seller_email"]
    principal = None
    output_dir = ""
    try:
        # Also stages existing uploads, so the cutover does not strand published listings.
        source = stage_listing_asset(seller_email, canonical, str(asset["id"]), asset["rel_path"], asset["filename"])
        principal = cells.resolve_workspace(seller_email)
        output_dir = f".marketplace/{canonical}/renders/{secrets.token_hex(16)}"
        fd = _mkdir_beneath(principal.root, output_dir)
        os.close(fd)
        with storage_write_lock(principal.root):
            enforce_disk_quota(seller_email, principal.plan, principal.root)
        outputs = asyncio.run(_render_in_cell(principal, seller_email, source, output_dir))
        with storage_write_lock(principal.root):
            enforce_disk_quota(seller_email, principal.plan, principal.root)
        return _publish_previews(canonical, seller_email, principal, output_dir, outputs)
    except (HTTPException, OSError, TimeoutError) as exc:
        log.warning("seller-cell preview failed for %s: %s", canonical, exc)
        return {"listing_id": canonical, "ok": False,
                "reason": "cell_refused" if isinstance(exc, HTTPException) and exc.status_code == 503 else "render_failed",
                "detail": str(exc.detail) if isinstance(exc, HTTPException) else "Seller cell preview unavailable."}
    finally:
        if principal and output_dir:
            try:
                fd = cells.directory_beneath(principal.root, output_dir)
                try:
                    for filename in ("thumbnail.png", "contact-sheet.png"):
                        try:
                            os.unlink(filename, dir_fd=fd)
                        except FileNotFoundError:
                            pass
                finally:
                    os.close(fd)
                parent = cells.directory_beneath(principal.root, output_dir.rsplit("/", 1)[0])
                try:
                    os.rmdir(output_dir.rsplit("/", 1)[1], dir_fd=parent)
                finally:
                    os.close(parent)
            except (HTTPException, OSError):
                log.warning("could not remove temporary seller preview %s", canonical)


def queue_render(listing_id: str) -> concurrent.futures.Future:
    canonical = str(uuid.UUID(str(listing_id)))
    with _jobs_lock:
        existing = _jobs.get(canonical)
        if existing and not existing.done():
            return existing
        admission = deployment_drain.controller().acquire()
        def accepted_render():
            try:
                with admission.bind():
                    return render_listing_previews(canonical)
            finally:
                admission.close()
        try:
            future = _executor.submit(accepted_render)
        except BaseException:
            admission.close()
            raise
        _jobs[canonical] = future
    def completed(job):
        admission.close()
        with _jobs_lock:
            if _jobs.get(canonical) is job:
                _jobs.pop(canonical, None)
        error = None if job.cancelled() else job.exception()
        if error:
            log.warning("seller preview render failed for %s", canonical,
                        exc_info=(type(error), error, error.__traceback__))
    future.add_done_callback(completed)
    return future


async def wait_pending():
    """Retain accepted queued previews until their actual executor completion."""
    with _jobs_lock:
        pending = list(_jobs.values())
    if pending:
        await asyncio.gather(*(asyncio.wrap_future(job) for job in pending), return_exceptions=True)


@router.post("/listings/{listing_id}/render")
async def render_listing(listing_id: str, request: Request) -> dict:
    marketplace_enabled_or_404()
    user = require_user(request)
    api_writes.enforce(user.email, "marketplace_render")
    try:
        canonical = str(uuid.UUID(listing_id))
    except (ValueError, TypeError, AttributeError):
        raise MarketplaceError(404, "listing_not_found", "listing not found") from None
    with tx() as cur:
        cur.execute("SELECT seller_email FROM mk_listings WHERE id=%s", (canonical,))
        listing = cur.fetchone()
    if listing is None or (listing["seller_email"] != user.email and user.role != "admin"):
        raise MarketplaceError(404, "listing_not_found", "listing not found")
    return await asyncio.to_thread(render_listing_previews, canonical)
