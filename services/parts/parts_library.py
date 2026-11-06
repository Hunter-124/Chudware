"""Watched "parts library" folder -> auto-register into the vendor-part registry.

Users (or the host) drop CAD files into ``%APPDATA%/Chudware/parts_library``; scanning the
folder registers every new or changed file into the same registry that ``vendor_parts``
owns (and that the C++ core reads via ``index.json``). This module is a thin scanner on
top of :mod:`services.parts.vendor_parts`; that module remains the registry owner.

Layout convention::

    parts_library/
        McMaster-Carr/91251A542.step   -> supplier="McMaster-Carr"
        fasteners/M3x10.step           -> supplier="fasteners"
        loose_part.step                -> supplier="library"   (no top-level subfolder)

Re-scanning skips unchanged files, updates changed files, and removes registry entries
for deleted files under the scanned root.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from urllib.parse import quote, urljoin, urlsplit
import re
from pathlib import Path
from typing import Any, Callable

from services import paths
from services.parts import vendor_parts
from services.parts.vendor_parts import CAD_EXTS, slug


# "M3x10", "M3x0.5x10", "M6-1.0x20" -> thread / pitch / length where obvious.
_THREAD_RE = re.compile(
    r"\b(?P<thread>M\d+(?:\.\d+)?)"
    r"(?:[x\-](?P<pitch>\d+(?:\.\d+)?))?"     # optional metric pitch
    r"(?:[x\-](?P<length>\d+(?:\.\d+)?))\b",  # length
    re.IGNORECASE,
)
# Bare "M3x10" (thread x length, no pitch).
_THREAD_SHORT_RE = re.compile(
    r"\b(?P<thread>M\d+(?:\.\d+)?)[x\-](?P<length>\d+(?:\.\d+)?)\b", re.IGNORECASE)


def parse_specs_from_name(stem: str) -> dict[str, Any]:
    """Best-effort spec extraction from a filename stem.

    Recognizes common fastener shorthand like ``M3x10`` or ``M6-1.0x20``. Returns an empty
    dict when nothing obvious is found (never raises).
    """
    specs: dict[str, Any] = {}
    m = _THREAD_RE.search(stem)
    if m and m.group("length"):
        specs["thread"] = m.group("thread").upper()
        if m.group("pitch"):
            specs["pitch"] = float(m.group("pitch"))
        specs["length"] = float(m.group("length"))
        return specs
    m = _THREAD_SHORT_RE.search(stem)
    if m:
        specs["thread"] = m.group("thread").upper()
        specs["length"] = float(m.group("length"))
    return specs


def _iter_cad_files(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in CAD_EXTS:
            yield p


def _supplier_for(path: Path, root: Path) -> str:
    """Infer supplier from the top-level subfolder under ``root``; else "library"."""
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return "library"
    parts = rel.parts
    if len(parts) >= 2:  # at least one subfolder before the filename
        return parts[0]
    return "library"


def _index_by_path(index: dict[str, dict]) -> dict[str, dict]:
    """Map resolved cad_path -> record for fast already-registered lookup."""
    out: dict[str, dict] = {}
    for rec in index.values():
        cp = rec.get("cad_path")
        if cp:
            try:
                out[str(Path(cp).resolve())] = rec
            except OSError:
                out[str(cp)] = rec
    return out


def scan_library_folder(folder: str | None = None) -> dict:
    """Recursively scan ``folder`` (default: the watched parts-library dir) and register
    every new or changed CAD file into the vendor-part registry. Deleted files under the
    scanned root are reconciled without removing entries belonging to other roots.

    Idempotent: files already registered with the same path + mtime are skipped. Returns
    ``{"scanned": N, "added": M, "updated": K, "parts": [records...]}``.
    """
    root = Path(folder).expanduser().resolve() if folder else paths.parts_library_dir()
    if not folder:
        root.mkdir(parents=True, exist_ok=True)
    if not root.exists():
        raise FileNotFoundError(f"parts library folder not found: {root}")

    index = vendor_parts._load_index()  # noqa: SLF001 (intentional: same package)
    by_path = _index_by_path(index)
    scanned = added = updated = 0
    pending: list[dict[str, Any]] = []

    for f in _iter_cad_files(root):
        scanned += 1
        resolved = str(f.resolve())
        mtime = f.stat().st_mtime
        existing = by_path.get(resolved)
        if existing is not None and float(existing.get("source_mtime") or 0.0) == mtime:
            continue  # unchanged -> skip (idempotent)

        supplier = _supplier_for(f, root)
        part_number = f.stem
        specs = parse_specs_from_name(f.stem)
        # Register WITHOUT copying: the library folder is itself the canonical store, so we
        # point cad_path at the file in place (keeps scans cheap + avoids duplicate bytes).
        pending.append({
            "supplier": supplier,
            "part_number": part_number,
            "description": (existing or {}).get("description") or f.stem,
            "cad_path": resolved,
            "specs": specs,
            "source": "library",
            "copy_file": False,
            "source_mtime": mtime,
        })
        if existing is None:
            added += 1
        else:
            updated += 1

    # Persist and rebuild once; the registry reloads under its writer lock.
    touched = vendor_parts.register_local_cads(pending, index=index, reconcile_root=root)

    return {"scanned": scanned, "added": added, "updated": updated, "parts": touched}


EmitFn = Callable[[dict], None]


def _vendor_part_scan_library(params: dict, _emit: EmitFn) -> dict:
    folder = params.get("folder")
    return scan_library_folder(str(folder) if folder else None)


_SCHEMA = "aicad.parts-library/1"
_LICENSES = frozenset({"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause",
                       "CC0-1.0", "CC-BY-4.0", "CC-BY-SA-4.0",
                       "CERN-OHL-P-2.0", "CERN-OHL-W-2.0", "CERN-OHL-S-2.0"})
_MAX_MANIFEST = 16 * 1024 * 1024
_MAX_ARTIFACT = 1024 * 1024 * 1024


def _object_path(value: str) -> str:
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or "%" in value or "?" in value or "#" in value or value.startswith("/")
            or any(part in ("", ".", "..") for part in value.split("/"))
            or any(ord(c) < 32 for c in value)):
        raise ValueError("artifact path must be a confined relative path")
    for component in value.split("/"):
        stem = component.split(".", 1)[0].upper()
        if (component.endswith((".", " ")) or any(c in component for c in '<>|*"')
                or stem in {"CON", "PRN", "AUX", "NUL",
                            *(f"COM{i}" for i in range(1, 10)),
                            *(f"LPT{i}" for i in range(1, 10))}):
            raise ValueError("artifact path is not portable")
    return value


def _rights(value: Any, *, publish: bool = False) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("redistribute"), bool):
        raise ValueError("rights must explicitly state redistribute")
    if not isinstance(value.get("license_expression"), str) or not value["license_expression"]:
        raise ValueError("rights require a license_expression")
    if not isinstance(value.get("attribution"), str):
        raise ValueError("rights require attribution")
    if publish and (not value["redistribute"] or value["license_expression"] not in _LICENSES
                    or not value["attribution"].strip()):
        raise ValueError("publication requires known redistributable rights and attribution")

def _portable(value: Any) -> None:
    """Reject local paths and credential-bearing URLs anywhere in portable metadata."""
    if isinstance(value, dict):
        for item in value.values():
            _portable(item)
    elif isinstance(value, list):
        for item in value:
            _portable(item)
    elif isinstance(value, str):
        if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", value):
            raise ValueError("portable metadata cannot contain absolute local paths")
        for match in re.finditer(r'[A-Za-z][A-Za-z0-9+.-]*://[^\s<>"\']+', value):
            url = urlsplit(match.group())
            if url.scheme.lower() == "file" or url.username is not None or url.password is not None:
                raise ValueError("portable metadata cannot contain file URLs or credentials")



def _manifest(path: Path, *, publish: bool = False) -> dict:
    if path.stat().st_size > _MAX_MANIFEST:
        raise ValueError("manifest exceeds size limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    return _validate_manifest(data, publish=publish)


def _validate_manifest(data: Any, *, publish: bool = False) -> dict:
    if not isinstance(data, dict) or data.get("schema") != _SCHEMA:
        raise ValueError("unsupported parts library schema")
    _portable(data)
    library_id = data.get("library_id")
    if not isinstance(library_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", library_id):
        raise ValueError("invalid library_id")
    if not isinstance(data.get("revision"), str) or not data["revision"]:
        raise ValueError("revision is required")
    records = data.get("records")
    if not isinstance(records, list):
        raise ValueError("records must be a list")
    seen = set()
    object_paths = {}
    for rec in records:
        if not isinstance(rec, dict):
            raise ValueError("record must be an object")
        domain, part_id = rec.get("domain"), rec.get("part_id")
        if (not isinstance(domain, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", domain)
                or not isinstance(part_id, str)
                or not part_id.startswith(f"{domain}:{library_id}:")
                or not re.fullmatch(r"[A-Za-z0-9_.-]+", part_id[len(domain) + len(library_id) + 2:])):
            raise ValueError("part_id must be <domain>:<library_id>:<local_id>")
        if part_id in seen:
            raise ValueError(f"duplicate part_id: {part_id}")
        seen.add(part_id)
        if (not isinstance(rec.get("title"), str) or not rec["title"]
                or not isinstance(rec.get("params"), dict)
                or not isinstance(rec.get("sourcing"), list)
                or not isinstance(rec.get("provenance"), str) or not rec["provenance"]
                or not isinstance(rec.get("license"), str) or not rec["license"]):
            raise ValueError(f"invalid PartRecord: {part_id}")
        if not isinstance(rec.get("units"), (str, dict)) or not rec["units"]:
            raise ValueError(f"units required: {part_id}")
        if not isinstance(rec.get("geometry_quality"), str) or not rec["geometry_quality"]:
            raise ValueError(f"geometry_quality required: {part_id}")
        refs = rec.get("references")
        if not isinstance(refs, list):
            raise ValueError("references must be a list")
        for ref in refs:
            if (not isinstance(ref, dict) or not isinstance(ref.get("identifier"), str)
                    or not ref["identifier"] or ref.get("kind") not in ("dimensional", "identifier")):
                raise ValueError("invalid reference")
        _rights(rec.get("rights"), publish=publish)
        geom = rec.get("geometry_ref")
        if geom is not None and (not isinstance(geom, dict) or geom.get("kind") not in
                                 {"core_family", "generate_model", "symbol_footprint",
                                  "section_profile", "fitting_id", "fmu_component"}):
            raise ValueError("portable geometry_ref must be a supported non-file recipe")
        artifact = rec.get("artifact")
        if artifact is not None:
            if not isinstance(artifact, dict):
                raise ValueError("artifact must be an object")
            _object_path(artifact.get("path"))
            if artifact.get("format") not in set(CAD_EXTS.values()):
                raise ValueError("unsupported artifact format")
            size = artifact.get("size_bytes")
            if type(size) is not int or not 0 < size <= _MAX_ARTIFACT:
                raise ValueError("invalid artifact size")
            if not isinstance(artifact.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]):
                raise ValueError("invalid artifact sha256")
            old_artifact = object_paths.setdefault(artifact["path"],
                                                   (artifact["sha256"], size))
            if old_artifact != (artifact["sha256"], size):
                raise ValueError("artifact path collision")
            _rights(artifact.get("rights"), publish=publish)
            if "notice" in artifact and not isinstance(artifact["notice"], str):
                raise ValueError("artifact notice must be text")
            if geom is not None:
                raise ValueError("artifact records must not contain a portable geometry recipe")
    return data


def _verify(path: Path, artifact: dict) -> None:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            size += len(chunk)
            if size > artifact["size_bytes"]:
                raise ValueError("artifact size mismatch")
            digest.update(chunk)
    if size != artifact["size_bytes"] or digest.hexdigest() != artifact["sha256"]:
        raise ValueError("artifact size or SHA-256 mismatch")


def _source(root: Path, rel: str) -> Path:
    root = root.resolve()
    path = (root / _object_path(rel)).resolve()
    if not path.is_relative_to(root):
        raise ValueError("artifact path escapes root")
    return path


def _cache_path(artifact: dict) -> Path:
    return Path(paths.VENDOR_PARTS) / "objects" / artifact["sha256"] / ("model." + artifact["format"])


def _copy_verified(source: Path, dest: Path, artifact: dict) -> None:
    _verify(source, artifact)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=dest.parent, prefix=".object-")
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as src:
            shutil.copyfileobj(src, out)
            out.flush()
            os.fsync(out.fileno())
        _verify(Path(name), artifact)
        os.replace(name, dest)
    finally:
        Path(name).unlink(missing_ok=True)


def _mirrors(values: list[str] | None) -> list[str]:
    if values is not None and not isinstance(values, list):
        raise ValueError("mirrors must be a list")
    result = []
    for value in values or []:
        if not isinstance(value, str):
            raise ValueError("mirror URL must be a string")
        u = urlsplit(value)
        if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError("mirrors must be public credential-free HTTPS base URLs")
        result.append(value.rstrip("/") + "/")
    return result


def ingest_manifest(manifest_path: str, *, artifact_root: str | None = None,
                    mirrors: list[str] | None = None, replace_namespace: bool = False) -> dict:
    return _ingest_manifest(manifest_path, artifact_root=artifact_root, mirrors=mirrors,
                            replace_namespace=replace_namespace,
                            source_identity="file:" + str(Path(manifest_path).resolve()))


def _ingest_manifest(manifest_path: str, *, artifact_root: str | None,
                     mirrors: list[str] | None, replace_namespace: bool,
                     source_identity: str) -> dict:
    if type(replace_namespace) is not bool:
        raise ValueError("replace_namespace must be a boolean")
    data = _manifest(Path(manifest_path))
    bases = _mirrors(mirrors)
    prepared = []
    for record in data["records"]:
        artifact = record.get("artifact")
        cached = ""
        if artifact and artifact_root is not None:
            src = _source(Path(artifact_root), artifact["path"])
            _verify(src, artifact)
            cached = str(_cache_path(artifact))
        prepared.append((record, cached))
    # Check existing built-in records without consulting live providers.
    from services.parts import get
    snapshot = vendor_parts._load_index()
    for record, _ in prepared:
        part_id = record["part_id"]
        existing = get(part_id, include_live=False)
        key = "manifest::" + part_id
        if existing is not None and key not in snapshot:
            raise ValueError(f"part_id collision: {part_id}")
    with vendor_parts._registry_lock():
        current = vendor_parts._load_index()
        namespace_rows = [row for row in current.values()
                          if row.get("source") == "manifest"
                          and row.get("library_id") == data["library_id"]]
        if any(row.get("source_identity") != source_identity for row in namespace_rows):
            if not replace_namespace:
                raise ValueError(f"library namespace source mismatch: {data['library_id']}")
            for row in namespace_rows:
                row["source_identity"] = source_identity
        updates = []
        for record, cached in prepared:
            part_id = record["part_id"]
            key = "manifest::" + part_id
            old = current.get(key, {})
            if old and (old.get("library_id") != data["library_id"] or
                        (old.get("part_record") or {}).get("part_id") != part_id):
                raise ValueError(f"registry collision: {part_id}")
            for other_key, row in current.items():
                if other_key != key and (row.get("part_record") or {}).get("part_id") == part_id:
                    raise ValueError(f"part_id collision: {part_id}")
            if not cached and record.get("artifact") is not None and (
                    old.get("part_record", {}).get("artifact") == record["artifact"]):
                cached = old.get("cad_path", "")
            merged_record = dict(old.get("part_record", {}))
            for field in ("standard", "designation", "aliases", "geometry_ref", "artifact"):
                merged_record.pop(field, None)
            merged_record.update(record)
            row = dict(old)
            row.update({"supplier": data["library_id"], "part_number": part_id,
                        "description": record["title"], "source": "manifest",
                        "library_id": data["library_id"], "revision": data["revision"],
                        "source_identity": source_identity,
                        "part_record": merged_record, "mirrors": bases, "cad_path": cached,
                        "cad_format": (record.get("artifact") or {}).get("format", "")})
            updates.append((key, row, record, cached))
        _validate_manifest({**data, "records": [row["part_record"] for _, row, _, _ in updates]})
        for key, row, record, cached in updates:
            if cached and artifact_root is not None:
                _copy_verified(_source(Path(artifact_root), record["artifact"]["path"]),
                               Path(cached), record["artifact"])
            current[key] = row
        vendor_parts._write_index(current)
    vendor_parts._rebuild_parts_index()
    return {"ok": True, "count": len(prepared), "library_id": data["library_id"],
            "revision": data["revision"]}


def sync_manifest(url: str, *, expected_sha256: str, mirrors: list[str] | None = None,
                  replace_namespace: bool = False) -> dict:
    if type(replace_namespace) is not bool:
        raise ValueError("replace_namespace must be a boolean")
    from services.net import safe_http
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("expected_sha256 must be a lowercase SHA-256")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("manifest URL must be credential-free HTTPS")
    if mirrors is not None:
        _mirrors(mirrors)
    _authorize_network("part_library_sync")
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "manifest.json"
        response = safe_http.download(url, dest, expected_sha256=expected_sha256,
                                      max_bytes=_MAX_MANIFEST, schemes=("https",))
        if response.status_code >= 400:
            raise OSError(f"manifest download HTTP {response.status_code}")
        bases = _mirrors(mirrors if mirrors is not None else [urljoin(response.url, ".")])
        return _ingest_manifest(str(dest), artifact_root=None, mirrors=bases,
                                replace_namespace=replace_namespace, source_identity="url:" + url)


def _authorize_network(tool: str) -> None:
    from services.mcp.tool_registry import deny_reason
    reason = deny_reason(tool)
    if reason:
        raise PermissionError(reason)


def acquire_part(part_id: str, *, offline: bool = False) -> dict:
    from services.parts import get
    from services.parts.adapters.local_library_adapter import _vendor_row_to_record
    key = "manifest::" + part_id
    row = vendor_parts._load_index().get(key)
    if not row:
        record = get(part_id, include_live=False)
        if not record and not offline:
            _authorize_network("part_acquire")
            record = get(part_id, include_live=True)
        if not record:
            raise KeyError(f"unknown part: {part_id}")
        if not record.get("geometry_ref"):
            raise ValueError(f"part has no geometry: {part_id}")
        return record
    artifact = row["part_record"].get("artifact")
    if not artifact:
        if not row["part_record"].get("geometry_ref"):
            raise ValueError(f"part has no geometry: {part_id}")
        return dict(row["part_record"])
    dest = _cache_path(artifact)
    verified = False
    if dest.is_file():
        try:
            _verify(dest, artifact)
        except (OSError, ValueError):
            if offline:
                raise ValueError(f"cached artifact is corrupt: {part_id}")
        else:
            verified = True
            if row.get("cad_path") == str(dest):
                return _vendor_row_to_record(row)
    if offline and not verified:
        raise FileNotFoundError(f"artifact unavailable offline: {part_id}")
    if not verified:
        _authorize_network("part_acquire")
        from services.net import safe_http
        errors = []
        for base in _mirrors(row.get("mirrors")):
            try:
                object_url = "/".join(quote(component, safe="") for component in artifact["path"].split("/"))
                response = safe_http.download(urljoin(base, object_url), dest,
                                              expected_sha256=artifact["sha256"],
                                              expected_size=artifact["size_bytes"],
                                              max_bytes=artifact["size_bytes"], schemes=("https",))
                if response.status_code >= 400:
                    raise OSError(f"HTTP {response.status_code}")
                _verify(dest, artifact)
                break
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
        else:
            raise FileNotFoundError(f"artifact unavailable for {part_id}: {'; '.join(errors)}")
    with vendor_parts._registry_lock():
        current = vendor_parts._load_index()
        latest = current.get(key)
        if not latest or latest.get("part_record", {}).get("artifact") != artifact:
            raise ValueError("artifact changed during acquisition")
        latest["cad_path"] = str(dest)
        vendor_parts._write_index(current)
        row = latest
    vendor_parts._rebuild_parts_index()
    return _vendor_row_to_record(row)


def publish_library(manifest_path: str, artifact_root: str, output_dir: str) -> dict:
    data = _manifest(Path(manifest_path), publish=True)
    output = Path(output_dir).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("publication output directory must be empty")
    if output == Path(artifact_root).resolve() or output == Path(manifest_path).resolve().parent:
        raise ValueError("publication output must be separate from source files")
    sources = []
    for rec in data["records"]:
        artifact = rec.get("artifact")
        if artifact:
            src = _source(Path(artifact_root), artifact["path"])
            _verify(src, artifact)
            sources.append((src, artifact))
    # Preflight files and directories as they would collide on a portable filesystem.
    published_paths = {"manifest.json": ("manifest.json", None),
                       "notices/notice.txt": ("notices/NOTICE.txt", None)}
    for _, artifact in sources:
        original = artifact["path"]
        path = original.casefold()
        if path == "notices" or path.startswith("notices/"):
            raise ValueError("artifact path collides with publication metadata")
        previous = published_paths.get(path)
        if previous is not None and previous != (original, artifact["sha256"]):
            raise ValueError("artifact path collision")
        published_paths[path] = (original, artifact["sha256"])
    names = sorted(published_paths)
    for i, path in enumerate(names):
        if any(other.startswith(path + "/") for other in names[i + 1:]):
            raise ValueError("publication file/directory collision")
    for src, artifact in sources:
        _copy_verified(src, _source(output, artifact["path"]), artifact)
    output.mkdir(parents=True, exist_ok=True)
    (output / "manifest.json").write_text(json.dumps(data, sort_keys=True, indent=2,
                                                      ensure_ascii=False) + "\n", encoding="utf-8")
    notices = []
    for rec in sorted(data["records"], key=lambda item: item["part_id"]):
        notices.append(f"{rec['part_id']}: {rec['rights']['license_expression']} — {rec['rights']['attribution']}")
        if rec.get("artifact"):
            art = rec["artifact"]
            notices.append(f"{rec['part_id']} artifact: {art['rights']['license_expression']} — "
                           f"{art['rights']['attribution']}" + (f"\n{art['notice']}" if art.get("notice") else ""))
    notice_dir = output / "notices"
    notice_dir.mkdir(exist_ok=True)
    (notice_dir / "NOTICE.txt").write_text("\n".join(notices) + "\n", encoding="utf-8")
    return {"ok": True, "count": len(data["records"]), "library_id": data["library_id"],
            "revision": data["revision"], "manifest_path": str(output / "manifest.json")}
