"""Reverse-Engineering session manager — CRUD for ``aicad.reveng_session/1``.

Owns the on-disk session directory tree defined in ``contracts/reveng.md`` §1 and the
``aicad.reveng_session/1`` manifest (§2). Every RE workspace run lives under a session
root::

    <AICAD_REVENG_DIR>/<session_id>/
      session.json          # aicad.reveng_session/1 manifest
      images/  frames/  sparse/  dense/  splat/  mesh/  datasheet/  exports/

``AICAD_REVENG_DIR`` defaults to ``$XDG_DATA_HOME/AI-CAD/reveng`` (Windows:
``%LOCALAPPDATA%/AI-CAD/reveng``), matching the artifact-path convention used by
``services/dag/executor.py`` and the vision ingest layer.

This module is **pure stdlib** (no OpenCV / Pillow / Open3D) so the MCP server always
imports it cleanly — it only manages JSON + directories, never geometry.

Honesty (contract §0): single-photo tiers force ``approximate=True``; the default
pipeline is ``scan_mesh_ai_parametric`` (honest scan → mesh → AI → parametric).
"""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA: str = "aicad.reveng_session/1"

#: Subdirectories created under every session root (contract §1).
SESSION_SUBDIRS: tuple[str, ...] = (
    "images",
    "frames",
    "sparse",
    "dense",
    "splat",
    "mesh",
    "datasheet",
    "exports",
)

#: ``status`` enum (contract §2).
STATUSES: tuple[str, ...] = (
    "capturing",
    "reconstructing",
    "modeling",
    "review",
    "done",
    "error",
)

#: Stage rail keys + their per-stage badge values.
STAGE_KEYS: tuple[str, ...] = (
    "capture",
    "scale",
    "reconstruct",
    "model",
    "review",
    "deliver",
)
STAGE_STATES: tuple[str, ...] = ("idle", "pending", "running", "done", "error")

#: Single-photo reconstruction tiers that are always tagged ``approximate`` (contract §0).
APPROXIMATE_TIERS: tuple[str, ...] = ("triposr", "trellis")

_SESSION_ID_RE = re.compile(r"^re_[0-9a-f]{8}$")


# --------------------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------------------
class RevEngSessionError(Exception):
    """Raised for invalid session ids, missing sessions, or bad manifest state."""


# --------------------------------------------------------------------------------------
# Roots & ids
# --------------------------------------------------------------------------------------
def default_reveng_root() -> Path:
    """Return the session-storage root, honouring ``AICAD_REVENG_DIR``.

    Falls back to ``$XDG_DATA_HOME/AI-CAD/reveng`` (or ``~/.local/share/...``) on
    POSIX and ``%LOCALAPPDATA%/AI-CAD/reveng`` on Windows (contract §0).
    """
    override = os.environ.get("AICAD_REVENG_DIR")
    if override:
        return Path(override).expanduser()

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "AI-CAD" / "reveng"

    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "AI-CAD" / "reveng"


def new_session_id() -> str:
    """``re_`` + 8 lowercase hex chars (contract §2)."""
    return "re_" + secrets.token_hex(4)


def is_valid_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID_RE.match(session_id or ""))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------------------
# Manifest scaffolding
# --------------------------------------------------------------------------------------
def _blank_manifest(session_id: str, name: str, part_ref: Optional[str]) -> dict[str, Any]:
    now = _now_iso()
    return {
        "schema": SCHEMA,
        "session_id": session_id,
        "name": name,
        "created_at": now,
        "updated_at": now,
        "status": "capturing",
        "part_ref": part_ref,
        "stages": {k: ("pending" if k == "capture" else "idle") for k in STAGE_KEYS},
        "scale": {
            "method": None,
            "mm_per_unit": 1.0,
            "residual_pct": None,
            "confirmed": False,
        },
        "artifacts": {
            "images": 0,
            "sparse_dir": None,
            "mesh_path": None,
            "splat_path": None,
            "doc_id": None,
        },
        "honesty": {
            "pipeline": "scan_mesh_ai_parametric",
            "single_photo_tier": None,
            "approximate": False,
        },
        "provenance": [],
    }


# --------------------------------------------------------------------------------------
# Session directory model
# --------------------------------------------------------------------------------------
class RevEngSession:
    """Handle to one on-disk RE session directory + its manifest.

    Construct via :func:`create` or :func:`load`. All mutators persist the manifest and
    bump ``updated_at`` atomically (write-temp-then-rename).
    """

    def __init__(self, root: Path, manifest: dict[str, Any]) -> None:
        self.root = Path(root)
        self.manifest = manifest

    # -- identity -----------------------------------------------------------------
    @property
    def session_id(self) -> str:
        return self.manifest["session_id"]

    @property
    def manifest_path(self) -> Path:
        return self.root / "session.json"

    def subdir(self, name: str) -> Path:
        """Absolute path to a named session subdirectory (contract §1)."""
        if name not in SESSION_SUBDIRS:
            raise RevEngSessionError(f"unknown session subdir: {name!r}")
        return self.root / name

    # -- persistence --------------------------------------------------------------
    def _write(self) -> None:
        self.manifest["updated_at"] = _now_iso()
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.manifest, indent=2, sort_keys=False), encoding="utf-8")
        os.replace(tmp, self.manifest_path)

    def save(self) -> "RevEngSession":
        self._write()
        return self

    # -- mutators -----------------------------------------------------------------
    def set_status(self, status: str) -> "RevEngSession":
        if status not in STATUSES:
            raise RevEngSessionError(f"invalid status: {status!r}")
        self.manifest["status"] = status
        return self.save()

    def set_stage(self, stage: str, state: str) -> "RevEngSession":
        if stage not in STAGE_KEYS:
            raise RevEngSessionError(f"unknown stage: {stage!r}")
        if state not in STAGE_STATES:
            raise RevEngSessionError(f"invalid stage state: {state!r}")
        self.manifest["stages"][stage] = state
        return self.save()

    def update_scale(
        self,
        *,
        method: Optional[str] = None,
        mm_per_unit: Optional[float] = None,
        residual_pct: Optional[float] = None,
        confirmed: Optional[bool] = None,
    ) -> "RevEngSession":
        scale = self.manifest["scale"]
        if method is not None:
            scale["method"] = method
        if mm_per_unit is not None:
            scale["mm_per_unit"] = float(mm_per_unit)
        if residual_pct is not None:
            scale["residual_pct"] = float(residual_pct)
        if confirmed is not None:
            scale["confirmed"] = bool(confirmed)
        return self.save()

    def update_artifacts(self, **kwargs: Any) -> "RevEngSession":
        """Merge known artifact keys; unknown keys are rejected to keep the schema stable."""
        artifacts = self.manifest["artifacts"]
        for key, value in kwargs.items():
            if key not in artifacts:
                raise RevEngSessionError(f"unknown artifact key: {key!r}")
            artifacts[key] = value
        return self.save()

    def set_single_photo_tier(self, tier: Optional[str]) -> "RevEngSession":
        """Set the single-photo reconstruction tier.

        Any known single-photo tier force-sets ``approximate=True`` (contract §0 honesty).
        Passing ``None`` clears the tier and restores the honest default pipeline.
        """
        honesty = self.manifest["honesty"]
        honesty["single_photo_tier"] = tier
        if tier is None:
            honesty["approximate"] = False
        else:
            honesty["approximate"] = tier.lower() in APPROXIMATE_TIERS
        return self.save()

    def add_provenance(self, entry: dict[str, Any]) -> "RevEngSession":
        """Append an entry to the cumulative provenance chain (contract §2)."""
        self.manifest["provenance"].append(entry)
        return self.save()

    # -- serialization ------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.manifest))


# --------------------------------------------------------------------------------------
# CRUD entry points
# --------------------------------------------------------------------------------------
def session_dir(session_id: str, root: Optional[Path] = None) -> Path:
    if not is_valid_session_id(session_id):
        raise RevEngSessionError(f"invalid session id: {session_id!r}")
    return (Path(root) if root else default_reveng_root()) / session_id


def create(
    name: str,
    *,
    part_ref: Optional[str] = None,
    root: Optional[Path] = None,
    session_id: Optional[str] = None,
) -> RevEngSession:
    """Create a new session directory tree + manifest and return the handle."""
    if not name or not name.strip():
        raise RevEngSessionError("session name is required")

    sid = session_id or new_session_id()
    if not is_valid_session_id(sid):
        raise RevEngSessionError(f"invalid session id: {sid!r}")

    base = Path(root) if root else default_reveng_root()
    sroot = base / sid
    if sroot.exists():
        raise RevEngSessionError(f"session already exists: {sid}")

    sroot.mkdir(parents=True, exist_ok=False)
    for sub in SESSION_SUBDIRS:
        (sroot / sub).mkdir(parents=True, exist_ok=True)

    sess = RevEngSession(sroot, _blank_manifest(sid, name.strip(), part_ref))
    return sess.save()


def load(session_id: str, root: Optional[Path] = None) -> RevEngSession:
    """Load an existing session by id; raise if missing or malformed."""
    sroot = session_dir(session_id, root)
    mpath = sroot / "session.json"
    if not mpath.exists():
        raise RevEngSessionError(f"session not found: {session_id}")
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RevEngSessionError(f"corrupt manifest for {session_id}: {exc}") from exc
    if manifest.get("schema") != SCHEMA:
        raise RevEngSessionError(
            f"unexpected manifest schema {manifest.get('schema')!r} (want {SCHEMA})"
        )
    return RevEngSession(sroot, manifest)


def load_or_create(
    session_id: str,
    *,
    name: Optional[str] = None,
    root: Optional[Path] = None,
) -> RevEngSession:
    try:
        return load(session_id, root)
    except RevEngSessionError:
        return create(name or session_id, root=root, session_id=session_id)


def exists(session_id: str, root: Optional[Path] = None) -> bool:
    if not is_valid_session_id(session_id):
        return False
    return (session_dir(session_id, root) / "session.json").exists()


def list_sessions(root: Optional[Path] = None) -> list[dict[str, Any]]:
    """Return lightweight summaries for every session under ``root`` (newest first)."""
    base = Path(root) if root else default_reveng_root()
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for child in base.iterdir():
        if not child.is_dir() or not is_valid_session_id(child.name):
            continue
        mpath = child / "session.json"
        if not mpath.exists():
            continue
        try:
            m = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append(
            {
                "session_id": m.get("session_id", child.name),
                "name": m.get("name"),
                "status": m.get("status"),
                "created_at": m.get("created_at"),
                "updated_at": m.get("updated_at"),
            }
        )
    out.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
    return out


def delete(session_id: str, root: Optional[Path] = None) -> bool:
    """Recursively remove a session directory. Returns ``True`` if it existed."""
    import shutil

    sroot = session_dir(session_id, root)
    if not sroot.exists():
        return False
    shutil.rmtree(sroot)
    return True
