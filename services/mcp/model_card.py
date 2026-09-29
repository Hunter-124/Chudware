"""Text thumbnails of CAD files — what a model sees where a person sees a picture.

A card is read straight from the file, never by opening a document: a ``.step+``
container already carries its feature tree (``document.json``), its last rebuild
(``cache.json``: revision, volume, per-feature status, body names) and its display
mesh (``cache.aicm``), so a directory of parts can be summarised in milliseconds. The
thumbnail is three shaded ASCII silhouettes (top / front / right) rasterised from that
mesh; plain meshes (STL) get the same, other exchange formats a header summary.

Cards are cached by (path, mtime, size), so repeated listings are free.
"""

from __future__ import annotations

import json
import re
import threading
import zipfile
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

#: Nearness ramp, far → near. A denser glyph is closer to the viewer.
SHADES = " .:-=+*#%@"
VIEWS_LEGEND = (
    "thumbnail views are shaded silhouettes, denser glyph = nearer the viewer: "
    "top looks down -Z (X right, Y up); front looks along +Y (X right, Z up); "
    "right looks along -X (Y right, Z up). Terminal glyphs are ~2x taller than wide."
)
_VIEW_MAX_COLS = 28
_VIEW_MAX_ROWS = 12
_SAMPLES = 40_000
_MAX_MESH_BYTES = 96 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 64 * 1024 * 1024
_MAX_ASCII_STL_BYTES = 48 * 1024 * 1024
_CACHE_MAX = 512
_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_CACHE_LOCK = threading.Lock()


def model_card(path: str | Path, *, thumbnail: bool = True) -> dict[str, Any]:
    """Summary card for one CAD file; never raises (a broken file is a card too)."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError as exc:
        return {"file": str(p), "ok": False, "summary": f"unreadable: {exc}"}
    key = (str(p), st.st_mtime_ns, st.st_size, thumbnail)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            return dict(hit)
    card = _build(p, st, thumbnail)
    with _CACHE_LOCK:
        _CACHE[key] = card
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return dict(card)


def _build(p: Path, st, thumbnail: bool) -> dict[str, Any]:
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
        if fmt in ("step+", "draw+", "cam+"):
            _native(p, card, thumbnail)
        elif fmt == "stl":
            _stl(p, card, thumbnail)
        elif fmt in ("step", "stp"):
            _step_header(p, card)
    except Exception as exc:  # noqa: BLE001 - a corrupt file is still a listable file
        card["error"] = f"{type(exc).__name__}: {exc}"
    card["summary"] = _summary(card)
    return card


# --------------------------------------------------------------------------- native


def _native(p: Path, card: dict[str, Any], thumbnail: bool) -> None:
    with zipfile.ZipFile(p) as zf:
        names = set(zf.namelist())
        meta = _zip_json(zf, "meta.json", names) or {}
        card["kind"] = meta.get("kind") or {"draw+": "drawing", "cam+": "cam"}.get(card["format"])
        card["name"] = meta.get("name")
        doc = None
        if "document.json" in names and zf.getinfo("document.json").file_size <= _MAX_DOCUMENT_BYTES:
            doc = _zip_json(zf, "document.json", names)
        if isinstance(doc, dict):
            _document_fields(doc, card)
        cache = _zip_json(zf, "cache.json", names)
        if isinstance(cache, dict):
            _cache_fields(cache, card)
        if "cache.aicm" in names and zf.getinfo("cache.aicm").file_size <= _MAX_MESH_BYTES:
            _aicm_fields(zf.read("cache.aicm"), card, thumbnail)
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
        types = Counter(str(f.get("type") or "?") for f in features)
        card["feature_types"] = dict(types.most_common(6))
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
        card["mate_types"] = dict(Counter(str(m.get("type") or "?") for m in mates).most_common(4))
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
        if card.get("kind") != "assembly":
            card["body_names"] = [str(b.get("name")) for b in bodies[:6] if isinstance(b, dict)]


def _aicm_fields(raw: bytes, card: dict[str, Any], thumbnail: bool) -> None:
    import numpy as np

    from services.sim.tessellate import read_aicm

    bodies = [b for b in read_aicm(raw).get("bodies") or [] if b.get("n_tris")]
    if not bodies:
        return
    offsets = np.cumsum([0] + [len(b["pos"]) for b in bodies[:-1]])
    pos = np.concatenate([np.asarray(b["pos"], dtype=np.float64) for b in bodies])
    tris = np.concatenate(
        [np.asarray(b["idx"], dtype=np.int64) + off for b, off in zip(bodies, offsets)]
    )
    _mesh_fields(pos, tris, card, thumbnail)
    card.setdefault("bodies", len(bodies))


# --------------------------------------------------------------------------- meshes


def _stl(p: Path, card: dict[str, Any], thumbnail: bool) -> None:
    import numpy as np

    size = p.stat().st_size
    with open(p, "rb") as fh:
        head = fh.read(84)
    count = int.from_bytes(head[80:84], "little") if len(head) == 84 else -1
    if count >= 0 and size == 84 + 50 * count:
        record = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
        data = np.fromfile(p, dtype=record, count=count, offset=84)
        verts = data["v"].reshape(-1, 3).astype(np.float64)
    elif size <= _MAX_ASCII_STL_BYTES:
        text = p.read_text(encoding="ascii", errors="ignore")
        nums = re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", text)
        verts = np.asarray(nums, dtype=np.float64).reshape(-1, 3)
        verts = verts[: len(verts) // 3 * 3]
    else:
        card["geometry"] = "ASCII STL too large to preview"
        return
    card["kind"] = "mesh"
    card["units"] = "unitless (mm assumed)"
    if not len(verts):
        return
    tris = np.arange(len(verts), dtype=np.int64).reshape(-1, 3)
    _mesh_fields(verts, tris, card, thumbnail)


def _mesh_fields(pos, tris, card: dict[str, Any], thumbnail: bool) -> None:
    lo, hi = pos.min(axis=0), pos.max(axis=0)
    card["bbox_mm"] = [round(float(v), 2) for v in hi - lo]
    card["bbox_min_mm"] = [round(float(v), 2) for v in lo]
    card["triangles"] = int(len(tris))
    if thumbnail:
        card["thumbnail"] = mesh_views(pos, tris)


def mesh_views(pos, tris) -> dict[str, list[str]]:
    """Top / front / right shaded silhouettes of a triangle mesh."""
    import numpy as np

    corners = pos[tris]  # (M, 3, 3)
    area = 0.5 * np.linalg.norm(
        np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]), axis=1
    )
    total = float(area.sum())
    if total > 0 and np.isfinite(total):
        rng = np.random.default_rng(0)
        pick = rng.choice(len(tris), size=_SAMPLES, p=area / total)
        r1 = np.sqrt(rng.random(_SAMPLES))[:, None]
        r2 = rng.random(_SAMPLES)[:, None]
        tri = corners[pick]
        samples = (1 - r1) * tri[:, 0] + r1 * (1 - r2) * tri[:, 1] + r1 * r2 * tri[:, 2]
        points = np.concatenate([samples, pos])
    else:
        points = pos
    x, y, z = points[:, 0], points[:, 1], points[:, 2]
    return {
        "top": _raster(x, y, z),       # viewer above: +Z is nearest
        "front": _raster(x, z, -y),    # viewer at -Y
        "right": _raster(y, z, x),     # viewer at +X
    }


def _raster(u, v, near) -> list[str]:
    import numpy as np

    u0, u1, v0, v1 = float(u.min()), float(u.max()), float(v.min()), float(v.max())
    du, dv = max(u1 - u0, 1e-9), max(v1 - v0, 1e-9)
    # A glyph is ~2x taller than wide: rows = cols * (dv/du) / 2.
    cols = _VIEW_MAX_COLS
    rows = round(cols * dv / du / 2)
    if rows > _VIEW_MAX_ROWS:
        rows = _VIEW_MAX_ROWS
        cols = max(3, min(_VIEW_MAX_COLS, round(rows * 2 * du / dv)))
    rows = max(1, rows)
    ci = np.clip(((u - u0) / du * cols).astype(int), 0, cols - 1)
    ri = np.clip(((v1 - v) / dv * rows).astype(int), 0, rows - 1)
    cell = ri * cols + ci
    depth = np.full(rows * cols, -np.inf)
    np.maximum.at(depth, cell, near)
    filled = np.isfinite(depth)
    shade = np.zeros(rows * cols, dtype=int)
    if filled.any():
        lo, hi = float(depth[filled].min()), float(depth[filled].max())
        span = hi - lo
        levels = len(SHADES) - 1
        norm = (depth[filled] - lo) / span if span > 0 else np.ones(int(filled.sum()))
        shade[filled] = 1 + np.minimum(levels - 1, (norm * levels).astype(int))
    grid = np.array(list(SHADES))[shade].reshape(rows, cols)
    return ["".join(row).rstrip() for row in grid]


# --------------------------------------------------------------------------- STEP


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


# --------------------------------------------------------------------------- summary


def _summary(card: dict[str, Any]) -> str:
    parts: list[str] = []
    kind = card.get("kind") or card["format"]
    name = card.get("name")
    parts.append(f"{kind} {name!r}" if name else str(kind))
    if card.get("material"):
        parts.append(str(card["material"]))
    elif card.get("body_materials"):
        parts.append("/".join(card["body_materials"][:3]))
    if card.get("components"):
        unique = card.get("unique_parts")
        parts.append(
            f"{card['components']} components"
            + (f" ({unique} unique)" if unique and unique != card["components"] else "")
        )
    if card.get("mates"):
        parts.append(f"{card['mates']} mates")
    if card.get("bodies") and card.get("kind") != "assembly":
        parts.append(f"{card['bodies']} bod{'y' if card['bodies'] == 1 else 'ies'}")
    if card.get("features"):
        top = ", ".join(
            f"{t}×{n}" if n > 1 else t for t, n in list(card["feature_types"].items())[:4]
        )
        parts.append(f"{card['features']} features ({top})")
    if card.get("bbox_mm"):
        parts.append("×".join(_num(v) for v in card["bbox_mm"]) + " mm")
    if card.get("volume_cm3"):
        parts.append(f"{card['volume_cm3']:g} cm³")
    if card.get("triangles") and card.get("kind") == "mesh":
        parts.append(f"{card['triangles']} triangles")
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


def summary_line(path: str | Path) -> Optional[str]:
    """One-line card summary (no thumbnail); None for a file that has no card."""
    return model_card(path, thumbnail=False).get("summary")
