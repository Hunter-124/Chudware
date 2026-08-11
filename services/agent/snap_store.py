"""On-disk store for snap-compaction payloads (OG source + PNG frames).

Snap compaction (``agent.compaction``) renders archived history to PNG
frames via the OMP sidecar. The full original ("OG") source text and the decoded
frames must outlive the in-memory turn: later compaction rounds re-render with
appended content, and a mid-session provider switch re-renders for the new
provider — both without data loss. This module owns that persistence.

Layout, content-addressed by :func:`snap_id_for`::

    <root>/<snap_id>/source.txt      full OG source, verbatim
    <root>/<snap_id>/frames/00.png   base64-decoded frame payloads
    <root>/<snap_id>/meta.json       hashes, geometry, billing provenance

Writes are atomic (tmp dir + rename) and idempotent: re-putting an identical
payload dedups onto the existing directory. Every public function degrades to
``None``/``0`` on failure instead of raising — compaction must never die because
the snap cache is unreadable.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- constants

DEFAULT_MAX_BYTES = 512 * 1024 * 1024

_SNAP_ID_RE = re.compile(r"[0-9a-f]{16}")

# Optional per-frame provenance copied through from the OMP render into meta.json.
_FRAME_PASSTHROUGH_KEYS = ("variant", "font", "detail")


# --------------------------------------------------------------------------- paths

def default_dir(cfg: dict | None = None) -> Path:
    """Resolve the snap store root.

    Order: ``agent.compaction.snap_dir`` from config.json (loaded via
    ``services.agent.config.load_config`` when *cfg* is None), then the
    ``CHUDWARE_SNAP_DIR`` env var, then ``<appdata>/Chudware/snaps`` via the
    canonical :mod:`services.paths` resolver.
    """
    if cfg is None:
        try:
            from services.agent.config import load_config  # noqa: PLC0415

            cfg = load_config()
        except Exception:  # noqa: BLE001 — config unreadable; fall through
            cfg = {}
    agent = (cfg or {}).get("agent") or {}
    compaction = agent.get("compaction") or {} if isinstance(agent, dict) else {}
    configured = compaction.get("snap_dir") if isinstance(compaction, dict) else None
    if configured:
        return Path(str(configured)).expanduser()
    try:
        import services.paths as _paths  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — non-repo embedding; env/home fallbacks below
        _paths = None
    env = os.environ.get("CHUDWARE_SNAP_DIR")
    if env:
        return Path(env).expanduser()
    if _paths is not None:
        return _paths.ROOT / "snaps"
    return Path.home() / ".local" / "share" / "Chudware" / "snaps"


def snap_id_for(source: str, parent_id: str | None, model: str, api: str) -> str:
    """Content address for a snap payload: first 16 hex chars of the sha256 over
    ``parent_id \\0 model \\0 api \\0 source``."""
    payload = (parent_id or "") + "\0" + model + "\0" + api + "\0" + source
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- write

def put(
    source: str,
    frames: list[dict],
    *,
    parent_id: str | None,
    model: str,
    api: str,
    truncated_chars: int = 0,
    billed_tokens: int | None = None,
    root: Path | None = None,
) -> dict | None:
    """Persist OG source + decoded frames under ``<root>/<snap_id>/``.

    *frames* items carry ``data`` (base64 PNG), ``cols``, ``rows``, ``chars`` and
    optional ``variant``/``font``/``detail``; extra keys are ignored and frame
    hashes are recomputed over the decoded bytes. The directory is written to
    ``<snap_id>.tmp-<pid>`` then renamed into place; if the final dir already
    exists (content-addressed dedup) the tmp dir is discarded and the existing
    meta returned. Returns the meta dict, or None on any I/O failure.
    """
    try:
        root = Path(root) if root is not None else default_dir()
        snap_id = snap_id_for(source, parent_id, model, api)
        final = root / snap_id
        if final.is_dir():
            return _read_meta(final)
        tmp = root / f"{snap_id}.tmp-{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)  # stale tmp from a crashed put, same pid
        (tmp / "frames").mkdir(parents=True, exist_ok=True)
        (tmp / "source.txt").write_text(source, encoding="utf-8")
        frame_metas = []
        for i, frame in enumerate(frames):
            raw = base64.b64decode(frame.get("data") or b"")
            name = f"frames/{i:02d}.png"
            (tmp / name).write_bytes(raw)
            entry = {
                "file": name,
                "cols": frame.get("cols"),
                "rows": frame.get("rows"),
                "chars": frame.get("chars"),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
            for key in _FRAME_PASSTHROUGH_KEYS:
                if key in frame:
                    entry[key] = frame[key]
            frame_metas.append(entry)
        meta = {
            "snap_id": snap_id,
            "parent_id": parent_id,
            "created": time.time(),
            "model": model,
            "api": api,
            "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "chars": len(source),
            "truncated_chars": int(truncated_chars),
            "billed_tokens": billed_tokens,
            "frames": frame_metas,
        }
        # meta.json last: its presence marks a complete payload inside the tmp dir.
        (tmp / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        try:
            os.rename(tmp, final)
        except OSError:
            # Lost a same-payload race (rename onto a non-empty dir fails on POSIX):
            # the other writer's bytes are identical — keep theirs, discard ours.
            shutil.rmtree(tmp, ignore_errors=True)
            if final.is_dir():
                return _read_meta(final)
            raise
        return meta
    except Exception as exc:  # noqa: BLE001 — snap persistence must never break compaction
        log.warning("snap_store.put failed: %s", exc)
        return None


def _read_meta(snap_dir: Path) -> dict | None:
    """Best-effort read of an existing snap's meta.json (dedup path)."""
    try:
        return json.loads((snap_dir / "meta.json").read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("snap_store: unreadable meta at %s: %s", snap_dir, exc)
        return None


# --------------------------------------------------------------------------- read

def get(snap_id: str, *, root: Path | None = None) -> dict | None:
    """Load and validate a snap: meta identity, source sha256, frame presence.

    On success returns the meta dict plus ``dir`` (the snap dir), each frame
    gaining an absolute ``path``. Any validation failure returns None.
    """
    validated = _validated(snap_id, root)
    if validated is None:
        return None
    meta, snap_dir = validated
    meta["dir"] = snap_dir
    for frame in meta.get("frames") or []:
        frame["path"] = snap_dir / str(frame.get("file") or "")
    return meta


def source_of(snap_id: str, *, root: Path | None = None) -> str | None:
    """Just the validated OG source text (same validation as :func:`get`), or None."""
    validated = _validated(snap_id, root)
    if validated is None:
        return None
    _meta, snap_dir = validated
    try:
        return (snap_dir / "source.txt").read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("snap_store.source_of failed for %s: %s", snap_id, exc)
        return None


def _validated(snap_id: str, root: Path | None) -> tuple[dict, Path] | None:
    """Shared validation for get()/source_of(): meta.json parses and names this
    snap_id, source.txt matches its recorded sha256, every frame file exists."""
    try:
        root = Path(root) if root is not None else default_dir()
        snap_dir = (root / snap_id).resolve()
        meta = _read_meta(snap_dir)
        if not isinstance(meta, dict) or meta.get("snap_id") != snap_id:
            return None
        raw = (snap_dir / "source.txt").read_bytes()
        if hashlib.sha256(raw).hexdigest() != meta.get("source_sha256"):
            log.warning("snap_store: source hash mismatch for %s", snap_id)
            return None
        for frame in meta.get("frames") or []:
            if not (snap_dir / str(frame.get("file") or "")).is_file():
                log.warning(
                    "snap_store: missing frame %s for %s", frame.get("file"), snap_id
                )
                return None
        return meta, snap_dir
    except Exception as exc:  # noqa: BLE001
        log.warning("snap_store.get failed for %s: %s", snap_id, exc)
        return None


# --------------------------------------------------------------------------- gc

def gc(*, root: Path | None = None, max_bytes: int = DEFAULT_MAX_BYTES) -> int:
    """Evict whole snap dirs, oldest dir-mtime first, until the store fits
    ``max_bytes``. Returns bytes freed; never raises."""
    freed = 0
    try:
        root = Path(root) if root is not None else default_dir()
        if not root.is_dir():
            return 0
        snaps = []
        total = 0
        for entry in root.iterdir():
            if not entry.is_dir() or not _SNAP_ID_RE.fullmatch(entry.name):
                continue  # tmp dirs and foreign entries are not gc's business
            size = _dir_size(entry)
            snaps.append((entry.stat().st_mtime, entry, size))
            total += size
        snaps.sort(key=lambda item: item[0])
        for _mtime, snap_dir, size in snaps:
            if total <= max_bytes:
                break
            shutil.rmtree(snap_dir, ignore_errors=True)
            if snap_dir.exists():
                continue  # removal failed; don't count it as freed
            total -= size
            freed += size
    except Exception as exc:  # noqa: BLE001
        log.warning("snap_store.gc failed: %s", exc)
    return freed


def _dir_size(path: Path) -> int:
    """Recursive file-size sum; unreadable entries count as zero."""
    size = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                size += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return size
