"""Server-side marketplace previews rendered by the shared C++ daemon."""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import math
import re
import struct
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from config import settings
from db import tx
from limits import api_writes
from marketplace_authz import MarketplaceError, marketplace_enabled_or_404, require_user
from marketplace_schema import RENDERABLE_EXTENSIONS


log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/studio", tags=["marketplace-studio"])

RENDER_BUDGET_SECONDS = 45.0
RPC_TIMEOUT_SECONDS = 12.0
CLOSE_RESERVE_SECONDS = 2.0
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_DIMS_RE = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s+X\s+"
    r"([0-9]+(?:\.[0-9]+)?)\s+X\s+"
    r"([0-9]+(?:\.[0-9]+)?)\s+MM\s*$",
    re.IGNORECASE,
)


class _RenderFailure(RuntimeError):
    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def _report(listing_id: str, *, ok: bool, reason: str, detail: str = "", **extra: Any) -> dict:
    report: dict[str, Any] = {
        "listing_id": listing_id,
        "ok": ok,
        "reason": reason,
        "rendered": 0,
        "assets": [],
    }
    if detail:
        report["detail"] = detail
    report.update(extra)
    return report


def _canonical_listing_id(listing_id: object) -> str:
    try:
        return str(uuid.UUID(str(listing_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise _RenderFailure("invalid_listing_id", "listing id is not a UUID") from exc


def _listing_root(listing_id: str) -> Path:
    configured = settings.listing_asset_root(listing_id)
    marketplace_root = settings.marketplace_root.resolve()
    try:
        relative = configured.relative_to(settings.marketplace_root)
    except ValueError as exc:
        raise _RenderFailure("unsafe_asset_path", "listing storage escapes marketplace storage") from exc
    current = settings.marketplace_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise _RenderFailure("unsafe_asset_path", "symlinked listing storage is refused")
    configured.mkdir(parents=True, exist_ok=True)
    resolved = configured.resolve()
    if resolved != marketplace_root and marketplace_root not in resolved.parents:
        raise _RenderFailure("unsafe_asset_path", "listing storage escapes marketplace storage")
    return resolved


def _assert_contained(root: Path, candidate: Path) -> Path:
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise _RenderFailure("unsafe_asset_path", "asset path escapes its listing directory")
    return resolved


def _stored_asset_path(
    listing_id: str,
    listing_root: Path,
    rel_path: str,
    *,
    must_exist: bool,
) -> Path:
    from marketplace import asset_path

    rel = Path(rel_path)
    if rel.is_absolute() or not rel.parts or ".." in rel.parts:
        raise _RenderFailure("unsafe_asset_path", "asset path is not a safe relative path")
    try:
        candidate = Path(asset_path(listing_id, rel_path))
    except Exception as exc:
        raise _RenderFailure("unsafe_asset_path", "asset path is outside listing storage") from exc
    current = listing_root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise _RenderFailure("unsafe_asset_path", "symlinked marketplace assets are refused")
    resolved = _assert_contained(listing_root, candidate)
    if must_exist and not resolved.is_file():
        raise _RenderFailure("source_asset_missing", "the primary source file is missing")
    return resolved


def _output_path(listing_root: Path, run_id: str, filename: str) -> Path:
    # engine_bridge.path_violation() guards the browser bridge, not this
    # server-initiated daemon request, so this containment check is authoritative.
    candidate = listing_root / "renders" / run_id / filename
    resolved = _assert_contained(listing_root, candidate)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def _rel_path(listing_root: Path, path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(listing_root).as_posix()
    except ValueError as exc:
        raise _RenderFailure("unsafe_asset_path", "render path escapes listing storage") from exc


def _png_dimensions(path: Path) -> tuple[int, int]:
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
    except OSError as exc:
        raise _RenderFailure("render_output_missing", "engine did not write the preview") from exc
    if len(header) != 24 or header[:8] != _PNG_SIGNATURE or header[12:16] != b"IHDR":
        raise _RenderFailure("invalid_png", "engine output is not a PNG")
    width, height = struct.unpack(">II", header[16:24])
    if width < 1 or height < 1:
        raise _RenderFailure("invalid_png", "engine output has invalid PNG dimensions")
    return width, height


def _file_record(listing_root: Path, path: Path, *, role: str, sort_order: int) -> dict:
    width, height = _png_dimensions(path)
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise _RenderFailure("render_output_missing", "engine preview could not be read") from exc
    return {
        "id": str(uuid.uuid4()),
        "path": path,
        "rel_path": _rel_path(listing_root, path),
        "filename": path.name,
        "role": role,
        "sort_order": sort_order,
        "bytes": size,
        "sha256": digest.hexdigest(),
        "width": width,
        "height": height,
    }


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _bbox(value: object) -> list[float] | None:
    if isinstance(value, dict):
        value = value.get("size")
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    out: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not math.isfinite(number) or number < 0:
            return None
        out.append(number)
    return out


def _geometry_facts(opened: dict, render_replies: list[dict]) -> dict:
    facts: dict[str, Any] = {}

    bbox = _bbox(opened.get("bbox_mm")) or _bbox(opened.get("bbox"))
    if bbox is None:
        for reply in render_replies:
            bbox = _bbox(reply.get("bbox_mm")) or _bbox(reply.get("bbox"))
            if bbox is not None:
                break
            dims = reply.get("dims")
            match = _DIMS_RE.match(dims) if isinstance(dims, str) else None
            if match:
                bbox = [float(match.group(index)) for index in (1, 2, 3)]
                break
    if bbox is not None:
        facts["bbox_mm"] = bbox

    part_count = _positive_int(opened.get("part_count"))
    if part_count is None:
        part_count = _positive_int(opened.get("body_count"))
    rebuild = opened.get("rebuild")
    if part_count is None and isinstance(rebuild, dict):
        components = rebuild.get("components")
        if isinstance(components, list) and components:
            part_count = len(components)
    tess = opened.get("tess")
    if part_count is None and isinstance(tess, dict):
        bodies = tess.get("bodies")
        if isinstance(bodies, list) and bodies:
            part_count = len(bodies)
    if part_count is not None:
        facts["part_count"] = part_count

    triangle_count = _positive_int(opened.get("triangle_count"))
    if triangle_count is None:
        triangle_count = _positive_int(opened.get("triangles"))
    if triangle_count is None and isinstance(tess, dict):
        triangle_count = _positive_int(tess.get("triangle_count"))
        if triangle_count is None:
            triangle_count = _positive_int(tess.get("triangles"))
    if triangle_count is not None:
        facts["triangle_count"] = triangle_count

    return facts


def _cleanup_run(listing_root: Path, run_id: str) -> None:
    try:
        run_root = _assert_contained(listing_root, listing_root / "renders" / run_id)
    except _RenderFailure:
        return
    for filename in ("hero.png", "front.png", "right.png", "top.png", "contact-sheet.png"):
        try:
            _assert_contained(listing_root, run_root / filename).unlink(missing_ok=True)
        except (_RenderFailure, OSError):
            log.warning("marketplace render cleanup failed under %s", run_root)
    try:
        run_root.rmdir()
    except OSError:
        pass


class _DaemonSession:
    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.doc_id = ""
        self._request_id = 0

    def _timeout(self, *, reserve_close: bool) -> float:
        remaining = self.deadline - asyncio.get_running_loop().time()
        if reserve_close:
            remaining -= CLOSE_RESERVE_SECONDS
        if remaining <= 0:
            raise _RenderFailure("render_budget_exceeded", "preview rendering exceeded its time budget")
        return min(RPC_TIMEOUT_SECONDS, remaining)

    async def connect(self) -> None:
        try:
            async with asyncio.timeout(self._timeout(reserve_close=True)):
                self.reader, self.writer = await asyncio.open_unix_connection(
                    str(settings.webd_socket),
                    limit=settings.ws_max_message_bytes,
                )
        except (OSError, asyncio.TimeoutError) as exc:
            raise _RenderFailure("engine_unavailable", "preview engine is unavailable") from exc

    async def call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        reserve_close: bool = True,
    ) -> dict:
        if self.reader is None or self.writer is None:
            raise _RenderFailure("engine_unavailable", "preview engine is unavailable")
        self._request_id += 1
        request_id = self._request_id
        encoded = (
            json.dumps(
                {"method": method, "params": params, "id": request_id},
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        try:
            async with asyncio.timeout(self._timeout(reserve_close=reserve_close)):
                self.writer.write(encoded)
                await self.writer.drain()
                raw = await self.reader.readline()
        except asyncio.TimeoutError as exc:
            raise _RenderFailure("rpc_timeout", f"preview engine timed out during {method}") from exc
        except OSError as exc:
            raise _RenderFailure("engine_unavailable", "preview engine connection failed") from exc
        except ValueError as exc:
            raise _RenderFailure("malformed_reply", "preview engine reply exceeded its limit") from exc
        if not raw or not raw.endswith(b"\n"):
            raise _RenderFailure("malformed_reply", "preview engine returned an incomplete reply")
        try:
            envelope = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _RenderFailure("malformed_reply", "preview engine returned invalid JSON") from exc
        if not isinstance(envelope, dict) or envelope.get("id") != request_id:
            raise _RenderFailure("malformed_reply", "preview engine returned a mismatched reply")
        if "error" in envelope:
            error = envelope["error"]
            if isinstance(error, dict):
                message = str(error.get("message") or error.get("reason") or "engine error")
            else:
                message = str(error or "engine error")
            raise _RenderFailure("engine_error", message[:500])
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise _RenderFailure("malformed_reply", "preview engine returned no result object")
        if result.get("ok") is False:
            detail = str(result.get("reason") or result.get("message") or result.get("error") or "render failed")
            raise _RenderFailure("engine_error", detail[:500])
        return result

    async def close(self) -> None:
        writer = self.writer
        if writer is None:
            return
        try:
            if self.doc_id:
                remaining = self.deadline - asyncio.get_running_loop().time()
                if remaining > 0:
                    try:
                        await self.call("doc_close", {"doc_id": self.doc_id}, reserve_close=False)
                    except BaseException:
                        log.warning("marketplace render could not confirm doc_close for %s", self.doc_id)
                else:
                    self._request_id += 1
                    writer.write(
                        json.dumps(
                            {
                                "method": "doc_close",
                                "params": {"doc_id": self.doc_id},
                                "id": self._request_id,
                            },
                            separators=(",", ":"),
                        ).encode("utf-8")
                        + b"\n"
                    )
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, asyncio.CancelledError):
                pass


async def _open_document(session: _DaemonSession, source_path: Path, extension: str) -> dict:
    if extension == ".step+":
        opened = await session.call("doc_open", {"path": str(source_path)})
    else:
        # The C++ symbol is doc_import_file, but its verified daemon wire name is import_file.
        opened = await session.call(
            "import_file",
            {
                "path": str(source_path),
                "into": "",
                "as_solid": False,
                "format": extension.lstrip("."),
            },
        )
    doc_id = opened.get("doc_id")
    if not isinstance(doc_id, str) or not doc_id:
        raise _RenderFailure("malformed_reply", "preview engine did not return a document id")
    session.doc_id = doc_id
    return opened


async def _render_files(
    session: _DaemonSession,
    listing_root: Path,
    run_id: str,
) -> tuple[list[dict], list[dict]]:
    specs = (
        ("hero.png", "iso", 1200, 900, "thumbnail", 0),
        ("front.png", "front", 800, 600, "gallery", 10),
        ("right.png", "right", 800, 600, "gallery", 20),
        ("top.png", "top", 800, 600, "gallery", 30),
    )
    records: list[dict] = []
    replies: list[dict] = []
    for filename, view, width, height, role, sort_order in specs:
        path = _output_path(listing_root, run_id, filename)
        reply = await session.call(
            "doc_render_view",
            {
                "doc_id": session.doc_id,
                "out_path": str(path),
                "view": {"view": view, "width": width, "height": height},
                "width": width,
                "height": height,
            },
        )
        replies.append(reply)
        records.append(_file_record(listing_root, path, role=role, sort_order=sort_order))

    contact_path = _output_path(listing_root, run_id, "contact-sheet.png")
    contact_reply = await session.call(
        "doc_render_contact_sheet",
        {
            "doc_id": session.doc_id,
            "out_path": str(contact_path),
            "views": ["front", "right", "top", "iso"],
        },
    )
    replies.append(contact_reply)
    records.append(_file_record(listing_root, contact_path, role="gallery", sort_order=40))
    return records, replies


def _replace_assets(listing_id: str, records: list[dict], geometry: dict) -> None:
    old_rel_paths: list[str] = []
    with tx() as cur:
        cur.execute(
            "SELECT rel_path FROM mk_assets WHERE listing_id = %s AND kind = 'render' FOR UPDATE",
            (listing_id,),
        )
        old_rel_paths = [str(row["rel_path"]) for row in cur.fetchall()]
        cur.execute(
            "DELETE FROM mk_assets WHERE listing_id = %s AND kind = 'render'",
            (listing_id,),
        )
        cur.execute(
            """
            UPDATE mk_assets
            SET role = CASE WHEN kind = 'image' THEN 'gallery' ELSE '' END
            WHERE listing_id = %s AND role = 'thumbnail'
            """,
            (listing_id,),
        )
        for record in records:
            cur.execute(
                """
                INSERT INTO mk_assets
                    (id, listing_id, kind, role, filename, rel_path, content_type,
                     format, bytes, sha256, width, height, sort_order, gated)
                VALUES (%s, %s, 'render', %s, %s, %s, 'image/png',
                        'png', %s, %s, %s, %s, %s, FALSE)
                """,
                (
                    record["id"],
                    listing_id,
                    record["role"],
                    record["filename"],
                    record["rel_path"],
                    record["bytes"],
                    record["sha256"],
                    record["width"],
                    record["height"],
                    record["sort_order"],
                ),
            )

        updates = ["thumbnail_asset_id = %s"]
        params: list[Any] = [records[0]["id"]]
        for column in ("bbox_mm", "part_count", "triangle_count"):
            if column in geometry:
                updates.append(f"{column} = %s")
                params.append(geometry[column])
        params.append(listing_id)
        cur.execute(
            f"UPDATE mk_listings SET {', '.join(updates)} WHERE id = %s",
            tuple(params),
        )

        listing_root = _listing_root(listing_id)
        new_paths = {record["path"].resolve() for record in records}
        for rel_path in old_rel_paths:
            try:
                old_path = _stored_asset_path(
                    listing_id, listing_root, rel_path, must_exist=False
                )
                if old_path not in new_paths:
                    old_path.unlink(missing_ok=True)
            except (_RenderFailure, OSError):
                log.warning("marketplace render refused or failed to unlink an old asset")


def _load_primary_source(listing_id: str) -> dict | None:
    with tx() as cur:
        cur.execute(
            """
            SELECT l.id, l.seller_email, a.id AS asset_id, a.filename, a.rel_path
            FROM mk_listings l
            LEFT JOIN LATERAL (
                SELECT id, filename, rel_path
                FROM mk_assets
                WHERE listing_id = l.id AND kind = 'source'
                ORDER BY (role = 'primary') DESC, sort_order, created_at, id
                LIMIT 1
            ) a ON TRUE
            WHERE l.id = %s
            """,
            (listing_id,),
        )
        row = cur.fetchone()
    return dict(row) if row is not None else None


async def _render_once(listing_id: str) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + RENDER_BUDGET_SECONDS
    row = _load_primary_source(listing_id)
    if row is None:
        return _report(listing_id, ok=False, reason="listing_not_found", detail="listing not found")
    if row.get("asset_id") is None:
        return _report(
            listing_id,
            ok=False,
            reason="source_asset_missing",
            detail="upload a primary source model before rendering previews",
        )

    extension = Path(str(row["filename"])).suffix.lower()
    if extension not in RENDERABLE_EXTENSIONS:
        return _report(
            listing_id,
            ok=False,
            reason="unsupported_format",
            detail="this source format needs a seller-supplied preview image",
        )

    listing_root = _listing_root(listing_id)
    source_path = _stored_asset_path(
        listing_id, listing_root, str(row["rel_path"]), must_exist=True
    )
    run_id = str(uuid.uuid4())
    records: list[dict] = []
    session = _DaemonSession(deadline)
    try:
        await session.connect()
        opened = await _open_document(session, source_path, extension)
        records, render_replies = await _render_files(session, listing_root, run_id)
        geometry = _geometry_facts(opened, render_replies)
        _replace_assets(listing_id, records, geometry)
        assets = [
            {
                "id": record["id"],
                "role": record["role"],
                "filename": record["filename"],
                "width": record["width"],
                "height": record["height"],
            }
            for record in records
        ]
        return _report(
            listing_id,
            ok=True,
            reason="ok",
            rendered=len(records),
            assets=assets,
            geometry=geometry,
        )
    except _RenderFailure:
        _cleanup_run(listing_root, run_id)
        raise
    except Exception:
        _cleanup_run(listing_root, run_id)
        raise
    finally:
        await session.close()


async def _safe_render(listing_id: object) -> dict:
    raw_id = str(listing_id)
    try:
        canonical = _canonical_listing_id(listing_id)
        return await _render_once(canonical)
    except _RenderFailure as exc:
        log.warning("marketplace preview for %s failed: %s", raw_id, exc.reason)
        return _report(raw_id, ok=False, reason=exc.reason, detail=exc.detail)
    except Exception:
        log.exception("marketplace preview for %s failed unexpectedly", raw_id)
        return _report(
            raw_id,
            ok=False,
            reason="render_failed",
            detail="preview rendering failed",
        )


class _RenderWorker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._thread: threading.Thread | None = None

    def _start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="marketplace-render",
                daemon=True,
            )
            self._thread.start()
        if not self._ready.wait(timeout=2.0):
            raise RuntimeError("marketplace render worker did not start")

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._semaphore = asyncio.Semaphore(1)
        self._ready.set()
        loop.run_forever()

    async def _serialized(self, listing_id: object) -> dict:
        semaphore = self._semaphore
        if semaphore is None:
            return _report(str(listing_id), ok=False, reason="render_failed", detail="render worker unavailable")
        async with semaphore:
            return await _safe_render(listing_id)

    def submit(self, listing_id: object) -> concurrent.futures.Future[dict]:
        self._start()
        loop = self._loop
        if loop is None:
            raise RuntimeError("marketplace render worker unavailable")
        return asyncio.run_coroutine_threadsafe(self._serialized(listing_id), loop)


_WORKER = _RenderWorker()
_BACKGROUND_RENDERS: set[concurrent.futures.Future[dict]] = set()
_BACKGROUND_LOCK = threading.Lock()


def render_listing_previews(listing_id: str) -> dict:
    """Render and atomically register the complete preview set, never raising."""
    try:
        future = _WORKER.submit(listing_id)
        return future.result(timeout=RENDER_BUDGET_SECONDS + 1.0)
    except concurrent.futures.TimeoutError:
        future.cancel()
        return _report(
            str(listing_id),
            ok=False,
            reason="render_budget_exceeded",
            detail="preview rendering exceeded its time budget",
        )
    except Exception:
        log.exception("marketplace preview submission failed for %s", listing_id)
        return _report(
            str(listing_id),
            ok=False,
            reason="render_failed",
            detail="preview rendering failed",
        )


def queue_render(listing_id: str) -> concurrent.futures.Future[dict]:
    """Queue a best-effort preview without delaying listing publication."""
    future: concurrent.futures.Future[dict] = concurrent.futures.Future()
    with _BACKGROUND_LOCK:
        _BACKGROUND_RENDERS.add(future)

    def completed(done: concurrent.futures.Future[dict]) -> None:
        with _BACKGROUND_LOCK:
            _BACKGROUND_RENDERS.discard(done)
        try:
            report = done.result()
            if not report.get("ok"):
                log.warning(
                    "queued marketplace preview for %s ended: %s",
                    listing_id,
                    report.get("reason", "render_failed"),
                )
        except concurrent.futures.CancelledError:
            log.warning("queued marketplace preview for %s was cancelled", listing_id)
        except Exception:
            log.exception("queued marketplace preview for %s crashed", listing_id)

    future.add_done_callback(completed)

    def launch() -> None:
        try:
            submitted = _WORKER.submit(listing_id)
        except Exception:
            log.exception("marketplace preview queue failed for %s", listing_id)
            if not future.cancelled():
                future.set_result(
                    _report(
                        str(listing_id),
                        ok=False,
                        reason="render_failed",
                        detail="preview rendering could not be queued",
                    )
                )
            return

        def forward(done: concurrent.futures.Future[dict]) -> None:
            if future.cancelled():
                done.cancel()
                return
            try:
                future.set_result(done.result())
            except concurrent.futures.CancelledError:
                future.cancel()
            except Exception as exc:
                future.set_exception(exc)

        submitted.add_done_callback(forward)

    try:
        threading.Thread(
            target=launch,
            name="marketplace-render-queue",
            daemon=True,
        ).start()
    except Exception:
        log.exception("marketplace preview launcher failed for %s", listing_id)
        if not future.cancelled():
            future.set_result(
                _report(
                    str(listing_id),
                    ok=False,
                    reason="render_failed",
                    detail="preview rendering could not be queued",
                )
            )
    return future


@router.post("/listings/{listing_id}/render")
async def render_listing(listing_id: str, request: Request) -> dict:
    marketplace_enabled_or_404()
    user = require_user(request)
    api_writes.enforce(user.email, "marketplace_render")
    try:
        canonical = _canonical_listing_id(listing_id)
    except _RenderFailure as exc:
        raise MarketplaceError(404, "listing_not_found", "listing not found") from exc

    with tx() as cur:
        cur.execute("SELECT seller_email FROM mk_listings WHERE id = %s", (canonical,))
        listing = cur.fetchone()
    if listing is None or (listing["seller_email"] != user.email and user.role != "admin"):
        raise MarketplaceError(404, "listing_not_found", "listing not found")

    try:
        future = _WORKER.submit(canonical)
        return await asyncio.wrap_future(future)
    except Exception:
        log.exception("marketplace preview route failed for %s", canonical)
        return _report(
            canonical,
            ok=False,
            reason="render_failed",
            detail="preview rendering failed",
        )
