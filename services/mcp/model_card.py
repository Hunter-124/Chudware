"""Facts about a CAD file, read straight from the file — never by opening a document.

A ``.step+`` container already carries its feature tree (``document.json``), its last
rebuild (``cache.json``: revision, volume, per-feature status, body names) and its display
mesh (``cache.aicm``, used here only for the bounding box), so a directory of parts is
summarised in milliseconds. STL gets its bbox and triangle count, STEP its header. The
picture of the part is ``model_thumbs`` (a real engine render); this is the caption.

``content_key`` is blake2b-8 of the raw ``document.json`` — the same key the PLM graph
publishes as ``meta.dh`` and the GUI's thumbnail cache is named by, so both share renders.
Cards are cached by (path, mtime, size), so repeated listings are free.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import zipfile
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

NATIVE_FORMATS = ("step+", "draw+", "cam+")
#: 3D exchange formats the engine's doc_open imports (and so can render a thumbnail of).
EXCHANGE_3D_FORMATS = ("step", "stp", "iges", "igs", "brep", "stl", "3mf", "obj", "ply",
                       "glb", "gltf")
_MAX_HASHED_BYTES = 32 * 1024 * 1024
_MAX_MESH_BYTES = 96 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
_MAX_ASCII_STL_BYTES = 48 * 1024 * 1024
_CACHE_MAX = 512
_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def model_card(path: str | Path) -> dict[str, Any]:
    """Summary card for one CAD file; never raises (a broken file is a card too)."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError as exc:
        return {"file": str(p), "ok": False, "summary": f"unreadable: {exc}"}
    key = (str(p), st.st_mtime_ns, st.st_size)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return dict(hit)
    card = _build(p, st)
    with _CACHE_LOCK:
        _CACHE[key] = card
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return dict(card)


def summary_line(path: str | Path) -> Optional[str]:
    """One-line card summary; None for a file that has no card."""
    return model_card(path).get("summary")


def _build(p: Path, st) -> dict[str, Any]:
    fmt = p.suffix.lower().lstrip(".")
    card: dict[str, Any] = {
        "file": str(p),
        "format": fmt,
        "size_kb": round(st.st_size / 1024, 1),
        "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc)
        .astimezone()
        .isoformat(timespec="minutes"),
    }
    try:
        if fmt in NATIVE_FORMATS:
            _native(p, card)
        elif fmt == "stl":
            _stl(p, card)
        elif fmt in ("step", "stp"):
            _step_header(p, card)
        if fmt in EXCHANGE_3D_FORMATS and "content_key" not in card:
            card["content_key"] = _file_key(p, st)
    except Exception as exc:  # noqa: BLE001 - a corrupt file is still a listable file
        card["error"] = f"{type(exc).__name__}: {exc}"
    card["summary"] = _summary(card)
    return card


def _file_key(p: Path, st) -> str:
    """Thumbnail key of an exchange file: its bytes, or (size, mtime, path) when large."""
    digest = hashlib.blake2b(digest_size=8)
    if st.st_size <= _MAX_HASHED_BYTES:
        digest.update(p.read_bytes())
    else:
        digest.update(f"{p.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode())
    return f"{p.suffix.lower().lstrip('.')}-{digest.hexdigest()}"


# --------------------------------------------------------------------------- native


def _native(p: Path, card: dict[str, Any]) -> None:
    with zipfile.ZipFile(p) as zf:
        names = set(zf.namelist())
        meta = _zip_json(zf, "meta.json", names) or {}
        card["kind"] = meta.get("kind") or {"draw+": "drawing", "cam+": "cam"}.get(card["format"])
        card["name"] = meta.get("name")
        if "document.json" in names and zf.getinfo("document.json").file_size <= _MAX_DOCUMENT_BYTES:
            raw = zf.read("document.json")
            card["content_key"] = hashlib.blake2b(raw, digest_size=8).hexdigest()
            try:
                doc = json.loads(raw)
            except ValueError:
                doc = None
            if isinstance(doc, dict):
                _document_fields(doc, card)
        cache = _zip_json(zf, "cache.json", names)
        if isinstance(cache, dict):
            _cache_fields(cache, card)
        if "cache.aicm" in names and zf.getinfo("cache.aicm").file_size <= _MAX_MESH_BYTES:
            _aicm_bbox(zf.read("cache.aicm"), card)
        elif card.get("kind") in ("part", "assembly"):
            card["geometry"] = "no cached mesh in the file (import_part to measure it)"


def _zip_json(zf: zipfile.ZipFile, name: str, names: set[str]) -> Any:
    if name not in names:
        return None
    try:
        return json.loads(zf.read(name))
    except ValueError:
        return None


def _document_fields(doc: dict[str, Any], card: dict[str, Any]) -> None:
    card["name"] = card.get("name") or doc.get("name")
    schema = str(doc.get("schema") or "")
    if schema.startswith("aicad.asm"):
        card["kind"] = "assembly"
    elif schema.startswith("aicad.part"):
        card["kind"] = card.get("kind") or "part"
    features = [f for f in doc.get("features") or [] if isinstance(f, dict)]
    if features:
        card["features"] = len(features)
        card["feature_types"] = dict(Counter(str(f.get("type") or "?") for f in features).most_common(6))
        suppressed = sum(1 for f in features if f.get("suppressed"))
        if suppressed:
            card["suppressed_features"] = suppressed
    components = [c for c in doc.get("components") or [] if isinstance(c, dict)]
    if components:
        card["components"] = len(components)
        sources = {
            (c.get("source") or {}).get("path") or (c.get("source") or {}).get("embedded")
            or c.get("name")
            for c in components
            if isinstance(c.get("source"), dict)
        }
        card["unique_parts"] = len({s for s in sources if s}) or None
        suppressed = sum(1 for c in components if c.get("suppressed"))
        if suppressed:
            card["suppressed_components"] = suppressed
    mates = [m for m in doc.get("mates") or [] if isinstance(m, dict)]
    if mates:
        card["mates"] = len(mates)
    material = doc.get("material")
    if isinstance(material, dict) and material.get("key"):
        card["material"] = material["key"]
    body_materials = doc.get("body_materials")
    if isinstance(body_materials, dict) and body_materials:
        card["body_materials"] = sorted(
            {str(v.get("key")) for v in body_materials.values() if isinstance(v, dict)}
        )
    configurations = doc.get("configurations")
    if isinstance(configurations, list) and len(configurations) > 1:
        card["configurations"] = len(configurations)


def _cache_fields(cache: dict[str, Any], card: dict[str, Any]) -> None:
    response = cache.get("response") if isinstance(cache.get("response"), dict) else {}
    if isinstance(response.get("revision"), int):
        card["revision"] = response["revision"]
    volume = response.get("volume")
    if isinstance(volume, (int, float)) and volume > 0:
        card["volume_cm3"] = round(volume / 1000.0, 2)
    status = (response.get("rebuild") or {}).get("feature_status")
    if isinstance(status, list):
        failing = [s.get("id") for s in status if isinstance(s, dict) and s.get("ok") is False]
        if failing:
            card["failing_features"] = failing[:8]
    bodies = (response.get("tess") or {}).get("bodies")
    if isinstance(bodies, list) and bodies:
        card["bodies"] = len(bodies)


def _aicm_bbox(raw: bytes, card: dict[str, Any]) -> None:
    import numpy as np

    from services.sim.tessellate import read_aicm

    bodies = [b for b in read_aicm(raw).get("bodies") or [] if b.get("n_tris")]
    if bodies:
        pos = np.concatenate([np.asarray(b["pos"], dtype=np.float64) for b in bodies])
        _bbox(pos, card)
        card.setdefault("bodies", len(bodies))


def _bbox(pos, card: dict[str, Any]) -> None:
    lo, hi = pos.min(axis=0), pos.max(axis=0)
    card["bbox_mm"] = [round(float(v), 2) for v in hi - lo]
    card["bbox_min_mm"] = [round(float(v), 2) for v in lo]


# --------------------------------------------------------------------------- other formats


def stl_vertices(p: Path):
    """(3·M, 3) float64 triangle corners of a binary or ASCII STL; None if too large."""
    import numpy as np

    size = p.stat().st_size
    with open(p, "rb") as fh:
        head = fh.read(84)
    count = int.from_bytes(head[80:84], "little") if len(head) == 84 else -1
    if count >= 0 and size == 84 + 50 * count:
        record = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
        verts = np.fromfile(p, dtype=record, count=count, offset=84)["v"].reshape(-1, 3)
        return verts.astype(np.float64)
    if size > _MAX_ASCII_STL_BYTES:
        return None
    text = p.read_text(encoding="ascii", errors="ignore")
    nums = re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", text)
    verts = np.asarray(nums, dtype=np.float64).reshape(-1, 3)
    return verts[: len(verts) // 3 * 3]


def _stl(p: Path, card: dict[str, Any]) -> None:
    verts = stl_vertices(p)
    if verts is None:
        card["geometry"] = "ASCII STL too large to summarise"
        return
    card["kind"] = "mesh"
    card["units"] = "unitless (mm assumed)"
    card["triangles"] = len(verts) // 3
    if p.stat().st_size <= _MAX_MESH_BYTES:
        card["content_key"] = "stl-" + hashlib.blake2b(p.read_bytes(), digest_size=8).hexdigest()
    if len(verts):
        _bbox(verts, card)


def _step_header(p: Path, card: dict[str, Any]) -> None:
    with open(p, "rb") as fh:
        head = fh.read(8192).decode("latin-1", errors="ignore")
    schema = re.search(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']+)'", head)
    if schema:
        card["schema"] = schema.group(1)
    name = re.search(r"FILE_NAME\s*\(\s*'([^']*)'", head)
    if name and name.group(1):
        card["name"] = name.group(1)
    system = re.search(r"FILE_NAME\s*\((?:[^;]*?,){5}\s*'([^']*)'", head)
    if system and system.group(1):
        card["authoring_system"] = system.group(1)


# --------------------------------------------------------------------------- captions


def dims_text(card: dict[str, Any]) -> Optional[str]:
    bbox = card.get("bbox_mm")
    return "×".join(_num(v) for v in bbox) + " mm" if bbox else None


def contents_text(card: dict[str, Any]) -> Optional[str]:
    """Components/mates for an assembly, features for a part."""
    if card.get("components"):
        unique = card.get("unique_parts")
        text = f"{card['components']} components" + (
            f" ({unique} unique)" if unique and unique != card["components"] else ""
        )
        return text + (f" · {card['mates']} mates" if card.get("mates") else "")
    if card.get("features"):
        return f"{card['features']} features"
    if card.get("triangles"):
        return f"{card['triangles']} triangles"
    return None


def _summary(card: dict[str, Any]) -> str:
    kind = card.get("kind") or card["format"]
    name = card.get("name")
    parts: list[str] = [f"{kind} {name!r}" if name else str(kind)]
    if card.get("material"):
        parts.append(str(card["material"]))
    elif card.get("body_materials"):
        parts.append("/".join(card["body_materials"][:3]))
    if card.get("bodies") and card.get("kind") != "assembly":
        parts.append(f"{card['bodies']} bod{'y' if card['bodies'] == 1 else 'ies'}")
    contents = contents_text(card)
    if contents:
        if card.get("features") and not card.get("components"):
            top = ", ".join(
                f"{t}×{n}" if n > 1 else t for t, n in list(card["feature_types"].items())[:4]
            )
            contents += f" ({top})"
        parts.append(contents)
    dims = dims_text(card)
    if dims:
        parts.append(dims)
    if card.get("volume_cm3"):
        parts.append(f"{card['volume_cm3']:g} cm³")
    if card.get("schema"):
        parts.append(str(card["schema"]))
    if card.get("revision") is not None:
        parts.append(f"rev {card['revision']}")
    parts.append(f"{card['size_kb']:g} KB")
    if card.get("failing_features"):
        parts.append(f"⚠ {len(card['failing_features'])} failing feature(s)")
    if card.get("error"):
        parts.append(f"⚠ unreadable ({card['error']})")
    return " · ".join(parts)


def _num(value: float) -> str:
    return f"{value:.0f}" if abs(value) >= 10 else f"{value:.1f}"
