"""Bounded validation of downloaded geometry before it is exposed for import.

ZIPs are inspected without extracting paths. A successful ZIP result names the
validated member so callers can extract only that member to their own destination.
"""
from __future__ import annotations

import re
import shutil
import stat
import struct
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath

MAX_CAD_BYTES = 200 * 1024 * 1024
MAX_ZIP_MEMBERS = 4096
_FORMATS = {".step": "step", ".stp": "step", ".iges": "iges", ".igs": "iges",
            ".stl": "stl", ".3mf": "3mf", ".obj": "obj", ".fcstd": "fcstd", ".zip": "zip"}
_RANK = {"step": 0, "iges": 1, "3mf": 2, "stl": 3, "obj": 4, "fcstd": 5}
_ECAD_EXTENSIONS = {".kicad_mod", ".kicad_sym", ".kicad_pcb", ".lib", ".schlib",
                    ".pcblib", ".intlib", ".lbr", ".bxl", ".brd", ".sch", ".p", ".s"}
_OBJ_VERTEX = re.compile(rb"^\s*v\s+[-+\d.]", re.MULTILINE)
_OBJ_FACE = re.compile(rb"^\s*f\s+\S+\s+\S+\s+\S+", re.MULTILINE)


def _result(ok: bool, fmt: str, reason: str = "", **extra) -> dict:
    return {"ok": ok, "format": fmt, "reason": reason, **extra}


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    entries = archive.infolist()
    if len(entries) > MAX_ZIP_MEMBERS:
        raise ValueError("ZIP has too many members")
    total = 0
    seen: set[str] = set()
    for entry in entries:
        name = entry.filename.replace("\\", "/")
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or ".." in path.parts or
                (path.parts and ":" in path.parts[0]) or
                stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1):
            raise ValueError("ZIP contains an unsafe or encrypted member")
        canonical = path.as_posix()
        if canonical in seen:
            raise ValueError("ZIP contains duplicate member paths")
        seen.add(canonical)
        total += entry.file_size
        if entry.file_size > MAX_CAD_BYTES or total > MAX_CAD_BYTES:
            raise ValueError("ZIP uncompressed size exceeds limit")
    return entries


def _validate_stream(stream, fmt: str, size: int, requested_format: str | None = None,
                     requested_member: str | None = None) -> dict:
    if not size:
        return _result(False, fmt, "empty file")
    if size > MAX_CAD_BYTES:
        return _result(False, fmt, "file exceeds size limit")
    prefix = stream.read(min(size, 65536))
    stream.seek(0)
    stripped = prefix.lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
    if stripped.startswith((b"<!doctype html", b"<html", b"%pdf", b"{", b"[")):
        return _result(False, fmt, "HTML, PDF or non-CAD content")
    if fmt == "step":
        valid = stripped.startswith(b"iso-10303-21;") and b"header;" in stripped
    elif fmt == "iges":
        # IGES records are 80 columns; column 73 identifies the Start section.
        line = prefix.splitlines()[0] if prefix else b""
        valid = len(line) >= 80 and line[72:73] == b"S" and line[73:80].strip().isdigit()
    elif fmt == "stl":
        valid = (stripped.startswith(b"solid") and b"facet" in stripped and b"vertex" in stripped)
        if not valid and size >= 84 and len(prefix) >= 84:
            triangles = struct.unpack("<I", prefix[80:84])[0]
            valid = triangles > 0 and 84 + 50 * triangles == size
    elif fmt == "obj":
        vertex = face = False
        start_of_line = True
        while chunk := stream.readline(65536):
            if start_of_line:
                vertex = vertex or bool(_OBJ_VERTEX.match(chunk))
                face = face or bool(_OBJ_FACE.match(chunk))
                if vertex and face:
                    break
            start_of_line = chunk.endswith(b"\n")
        valid = vertex and face
    elif fmt in {"zip", "3mf", "fcstd"}:
        try:
            with zipfile.ZipFile(stream) as archive:
                entries = _safe_members(archive)
                if fmt == "3mf":
                    names = {entry.filename.replace("\\", "/").lower(): entry for entry in entries}
                    models = [entry for name, entry in names.items()
                              if name.startswith("3d/") and name.endswith(".model")]
                    valid = False
                    if "[content_types].xml" in names and models:
                        with archive.open(models[0]) as model:
                            valid = bool(re.search(rb"<(?:\w+:)?model(?:\s|>)", model.read(65536), re.I))
                    return _result(bool(valid), fmt, "" if valid else "ZIP is not a 3MF model")
                if fmt == "fcstd":
                    doc = next((entry for entry in entries if entry.filename == "Document.xml"), None)
                    valid = False
                    if doc is not None and doc.file_size <= 32 * 1024 * 1024:
                        with archive.open(doc) as source:
                            root = ET.parse(source).getroot()
                        valid = (root.tag == "Document" and
                                 any(child.tag == "Objects" for child in root))
                    return _result(valid, fmt, "" if valid else "ZIP is not a FreeCAD document")
                requested = (requested_format or "").lower()
                requested = {"stp": "step", "igs": "iges"}.get(requested, requested)
                candidates = [(entry, _FORMATS.get(Path(entry.filename).suffix.lower()))
                              for entry in entries if not entry.is_dir()]
                candidates = [(entry, kind) for entry, kind in candidates
                              if kind in _RANK and (not requested or kind == requested) and
                              (requested_member is None or entry.filename == requested_member)]
                candidates.sort(key=lambda pair: (_RANK[pair[1]], pair[0].filename))
                matches = []
                for entry, kind in candidates:
                    with archive.open(entry) as member:
                        checked = _validate_stream(member, kind, entry.file_size)
                    if checked["ok"]:
                        matches.append((entry, kind))
                stems = {PurePosixPath(entry.filename.replace("\\", "/")).stem.casefold()
                         for entry, _ in matches}
                if matches and len(stems) == 1:
                    return _result(True, matches[0][1], member=matches[0][0].filename)
                if len(matches) > 1:
                    return _result(False, fmt, "ZIP contains multiple CAD models; select a member",
                                   candidates=[entry.filename for entry, _ in matches])
                return _result(False, fmt, "ZIP contains no valid supported CAD member")
        except (OSError, ValueError, EOFError, RuntimeError, zipfile.BadZipFile,
                NotImplementedError, ET.ParseError) as exc:
            return _result(False, fmt, f"Invalid ZIP: {exc}")
    else:
        return _result(False, fmt, "unsupported CAD format")
    return _result(bool(valid), fmt, "" if valid else f"invalid {fmt.upper()} content")


def validate_cad(path: str | Path, *, requested_format: str | None = None,
                 requested_member: str | None = None) -> dict:
    """Return a format and validation reason; never treat a document as geometry."""
    try:
        path = Path(path)
    except (TypeError, ValueError) as exc:
        return _result(False, "", str(exc))
    fmt = _FORMATS.get(path.suffix.lower(), "")
    if not fmt:
        return _result(False, fmt, "unsupported CAD format")
    if ((requested_format is not None and not isinstance(requested_format, str)) or
            (requested_member is not None and not isinstance(requested_member, str))):
        return _result(False, fmt, "invalid CAD format or member selection")
    if requested_member is not None and fmt != "zip":
        return _result(False, fmt, "member selection requires a ZIP archive")
    requested = (requested_format or "").lower()
    requested = {"stp": "step", "igs": "iges"}.get(requested, requested)
    if requested and fmt != "zip" and requested != fmt:
        return _result(False, fmt, "requested CAD format is unavailable")
    try:
        with path.open("rb") as stream:
            return _validate_stream(stream, fmt, path.stat().st_size, requested_format, requested_member)
    except (OSError, ValueError) as exc:
        return _result(False, fmt, str(exc))


def classify_artifact(path: str | Path, kind: str | None = None, *,
                      requested_format: str | None = None, requested_member: str | None = None) -> dict:
    """Classify an acquired file without mistaking PDFs or ECAD archives for CAD."""
    try:
        path = Path(path)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "artifact_kind": None, "format": "", "reason": str(exc)}
    fmt = _FORMATS.get(path.suffix.lower(), path.suffix.lower().lstrip("."))
    if kind is not None and (not isinstance(kind, str) or kind not in {"cad", "document", "ecad", "archive"}):
        return {"ok": False, "artifact_kind": kind, "format": fmt, "reason": "unknown artifact kind"}
    if ((requested_format is not None and not isinstance(requested_format, str)) or
            (requested_member is not None and not isinstance(requested_member, str))):
        return {"ok": False, "artifact_kind": kind, "format": fmt, "reason": "invalid CAD format or member selection"}
    try:
        size = path.stat().st_size
        if size <= 0 or size > MAX_CAD_BYTES:
            return {"ok": False, "artifact_kind": kind, "format": fmt,
                    "reason": "empty file" if not size else "file exceeds size limit"}
        with path.open("rb") as stream:
            prefix = stream.read(256).lstrip()
        is_pdf = prefix.startswith(b"%PDF-")
        if kind == "document" or (kind is None and is_pdf):
            if (fmt == "pdf" or is_pdf) and not is_pdf:
                return {"ok": False, "artifact_kind": "document", "format": fmt,
                        "reason": "invalid PDF content"}
            if kind == "document" and fmt in _FORMATS.values() and not is_pdf:
                return {"ok": False, "artifact_kind": "document", "format": fmt,
                        "reason": "document kind conflicts with CAD or archive format"}
            return {"ok": True, "artifact_kind": "document", "format": "pdf" if is_pdf else fmt,
                    "reason": ""}
        if fmt == "zip":
            with zipfile.ZipFile(path) as archive:
                entries = _safe_members(archive)
                ecad = any(Path(entry.filename).suffix.lower() in _ECAD_EXTENSIONS
                           for entry in entries if not entry.is_dir())
            if kind == "ecad" and not ecad:
                return {"ok": False, "artifact_kind": kind, "format": "zip",
                        "reason": "ZIP contains no recognized ECAD files"}
            if kind in {"archive", "ecad"}:
                return {"ok": True, "artifact_kind": kind, "format": "zip", "reason": ""}
            checked = validate_cad(path, requested_format=requested_format, requested_member=requested_member)
            if checked["ok"]:
                return {**checked, "artifact_kind": "cad", "cad_validated": True}
            if kind == "cad" or requested_format or requested_member:
                return {**checked, "artifact_kind": "cad", "cad_validated": False}
            if checked.get("candidates"):
                return {"ok": True, "artifact_kind": "archive", "format": "zip",
                        "reason": checked["reason"], "candidates": checked["candidates"]}
            return {"ok": True, "artifact_kind": "ecad" if ecad else "archive",
                    "format": "zip", "reason": ""}
        if kind == "cad" and fmt == "sldprt":
            if requested_member is not None or (requested_format and requested_format.lower() != fmt):
                return {"ok": False, "artifact_kind": "cad", "format": fmt, "cad_validated": False,
                        "reason": "requested CAD format or member is unavailable"}
            if prefix.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
                return {"ok": True, "artifact_kind": "cad", "format": fmt, "cad_validated": False,
                        "reason": "proprietary CAD content not validated"}
            return {"ok": False, "artifact_kind": "cad", "format": fmt,
                    "cad_validated": False, "reason": "invalid proprietary CAD container"}
        if kind == "ecad" and path.suffix.lower() in _ECAD_EXTENSIONS:
            if prefix.lower().startswith((b"<!doctype html", b"<html", b"%pdf")):
                return {"ok": False, "artifact_kind": kind, "format": fmt, "reason": "non-ECAD content"}
            return {"ok": True, "artifact_kind": kind, "format": fmt, "reason": ""}
        if kind in {"archive", "ecad"}:
            return {"ok": False, "artifact_kind": kind, "format": fmt,
                    "reason": "unsupported archive format"}
        checked = validate_cad(path, requested_format=requested_format, requested_member=requested_member)
        return {**checked, "artifact_kind": "cad" if checked["ok"] or kind == "cad" else None,
                "cad_validated": checked["ok"]}
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        return {"ok": False, "artifact_kind": kind, "format": fmt, "reason": str(exc)}


def extract_cad(path: str | Path, *, requested_format: str | None = None,
                requested_member: str | None = None, dest_dir: str | Path | None = None) -> dict:
    """Extract only the validated member to a unique file, retaining the archive path."""
    checked = validate_cad(path, requested_format=requested_format, requested_member=requested_member)
    if not checked["ok"]:
        return {**checked, "artifact_kind": "cad"}
    path = Path(path)
    member = checked.get("member")
    if not member:
        return {**checked, "artifact_kind": "cad", "artifact_path": str(path)}
    dest = None
    try:
        folder = Path(dest_dir) if dest_dir is not None else path.parent
        folder.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path) as archive:
            _safe_members(archive)
            with archive.open(member) as source, tempfile.NamedTemporaryFile(
                    mode="wb", dir=folder, prefix=".cad-member-",
                    suffix=Path(member).suffix.lower(), delete=False) as target:
                dest = Path(target.name)
                shutil.copyfileobj(source, target, length=1024 * 1024)
        extracted = validate_cad(dest)
        if not extracted["ok"]:
            dest.unlink(missing_ok=True)
            return {**extracted, "artifact_kind": "cad"}
        return {**extracted, "artifact_kind": "cad", "artifact_path": str(dest),
                "archive_path": str(path), "member": member}
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, KeyError) as exc:
        if dest is not None:
            dest.unlink(missing_ok=True)
        return {"ok": False, "artifact_kind": "cad", "format": checked["format"], "reason": str(exc)}
