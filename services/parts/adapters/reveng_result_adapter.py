"""CatalogProvider adapter for Reverse-Engineering produced models (RE-23).

When ``reveng_attach_result`` links a completed RE session to a parts-hub
``part_ref`` it writes a ``reveng_result.json`` sidecar into the session
directory.  This adapter scans all session directories under
``$CHUDWARE_REVENG_DIR`` and exposes each sidecar as a PartRecord with
``geometry_ref.kind = "import_file"`` so that ``part_place`` can route it
through the existing ``_place_import_file`` placer.

The adapter is *live-only* (``iter_records`` raises ``NotImplementedError``).
It is registered on first attach via the parts-hub provider registry
(idempotent — the registry deduplicates by ``provider_id``).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

from services.agent import request_context

PROVIDER_ID = "reveng_results"
SIDECAR_NAME = "reveng_result.json"


def _reveng_dir() -> Path:
    if request_context.in_cell():
        return request_context.actor_state_root() / "reveng"
    base = os.environ.get("CHUDWARE_REVENG_DIR")
    if base:
        return Path(base).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    return Path(xdg) / "Chudware" / "reveng"


def _load_sidecar(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _sidecar_to_record(sidecar: dict) -> dict | None:
    """Convert a reveng_result.json sidecar into a PartRecord."""
    part_ref = sidecar.get("part_ref")
    mesh_path = sidecar.get("mesh_path")
    doc_id = sidecar.get("doc_id")
    session_id = sidecar.get("session_id", "")
    if not part_ref:
        return None
    # A live doc_id is provenance, not a filename. New attachments carry an
    # explicit file reference; only old mesh-only sidecars can use their mesh.
    gref = sidecar.get("geometry_ref")
    if ("geometry_ref" not in sidecar and not doc_id and not sidecar.get("step_path")
            and isinstance(mesh_path, str) and mesh_path):
        gref = {"kind": "import_file", "path": mesh_path}
    if not isinstance(gref, dict) or gref.get("kind") != "import_file":
        gref = {}
    else:
        path = gref.get("path")
        if not isinstance(path, str) or not path:
            gref = {}
        elif request_context.in_cell() and (
                Path(path).is_absolute() or ".." in Path(path).parts or
                "\\" in path or ":" in path):
            gref = {}
    return {
        "part_id": part_ref,
        "domain": "reveng",
        "provider_id": PROVIDER_ID,
        "name": sidecar.get("name", part_ref),
        "title": str(sidecar.get("name") or part_ref),
        "family": str(sidecar.get("family") or ""),
        "description": f"Reverse-engineered model from session {session_id}",
        "geometry_ref": gref,
        "metadata": {
            "session_id": session_id,
            "mesh_path": mesh_path,
            "doc_id": doc_id,
            "geometry_source": sidecar.get("geometry_source"),
            "model_present": bool(gref),
        },
        "source": "reveng",
    }


class RevengResultAdapter:
    """Live-only CatalogProvider that exposes RE-produced models as PartRecords.

    Registered once per process into the parts-hub singleton.  Uses the
    ``provider_id`` sentinel so PartsHub.register() deduplication keeps only
    one instance (ProviderRegistry deduplicates on ``provider_id``).
    """

    domain: str = "reveng"
    provider_id: str = PROVIDER_ID

    # ------------------------------------------------------------------
    # CatalogProvider protocol
    # ------------------------------------------------------------------

    def search(self, query: str, *,
               domain: str | None = None,
               filters: dict | None = None) -> list[dict]:
        if domain and domain != "reveng":
            return []
        q = query.lower()
        results = []
        base = _reveng_dir()
        if not base.is_dir():
            return []
        for session_dir in base.iterdir():
            sidecar_path = session_dir / SIDECAR_NAME
            if not sidecar_path.is_file():
                continue
            sc = _load_sidecar(sidecar_path)
            if sc is None:
                continue
            rec = _sidecar_to_record(sc)
            if rec is None:
                continue
            # Simple substring match on part_ref / name / session_id.
            haystack = " ".join([
                rec.get("part_id", ""),
                rec.get("name", ""),
                sc.get("session_id", ""),
            ]).lower()
            if not q or q in haystack:
                results.append(rec)
        return results

    def get(self, part_id: str) -> dict | None:
        """Return a PartRecord by part_id (exact match on sidecar part_ref)."""
        if not part_id:
            return None
        base = _reveng_dir()
        if not base.is_dir():
            return None
        for session_dir in base.iterdir():
            sidecar_path = session_dir / SIDECAR_NAME
            if not sidecar_path.is_file():
                continue
            sc = _load_sidecar(sidecar_path)
            if sc and sc.get("part_ref") == part_id:
                return _sidecar_to_record(sc)
        return None

    def iter_records(self) -> Iterator[dict]:
        """Live-only provider — not indexable."""
        raise NotImplementedError("RevengResultAdapter is live-only")
