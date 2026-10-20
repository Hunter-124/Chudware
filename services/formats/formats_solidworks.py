"""SolidWorks (.sldprt/.sldasm) and Parasolid (.x_t/.x_b) import.

Fidelity is explicit and ordered:

1. **SolidWorks COM -> STEP** (Windows, when SolidWorks is installed): exact analytic B-rep.
2. **Portable saved-display tessellation** (SolidWorks 2015+): decode the modern chunk
   container, recover SolidWorks' triangle strips, preserve assembly hierarchy/transforms, and
   return first-class mesh bodies. The C++ production importer sews the same strips into baked
   native B-rep bodies. This geometry is usable and editable, but is an approximation rather than
   the original parametric feature tree.
3. Legacy OLE files and Parasolid without COM fail with an actionable STEP-export message.

The portable codec mirrors ``core/doc/solidworks_tessellation.cpp``. Keep the marker, stream,
triangle-strip, unit, and row-vector transform rules in lockstep.
"""

from __future__ import annotations

import os
import tempfile
import struct
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Capability detection
# ---------------------------------------------------------------------------
def _win32com_available() -> bool:
    try:
        import win32com.client  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def solidworks_available() -> bool:
    """True iff this machine looks like it can COM-automate SolidWorks (pywin32 + SW registered).

    Does NOT launch SolidWorks — just checks pywin32 import and the SldWorks COM registration in the
    registry, so it is cheap to call as a feature gate."""
    if not _win32com_available():
        return False
    try:
        import winreg
        for hive in (winreg.HKEY_CLASSES_ROOT,):
            try:
                winreg.OpenKey(hive, r"SldWorks.Application").Close()
                return True
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        pass
    return False


# ---------------------------------------------------------------------------
# Primary path — SolidWorks COM → STEP
# ---------------------------------------------------------------------------
# SolidWorks document type ids (swDocumentTypes_e).
_SW_DOCTYPE = {".sldprt": 1, ".sldasm": 2, ".slddrw": 3}  # PART / ASSEMBLY / DRAWING


def sw_com_export_step(src_path: str | os.PathLike, out_step: str | None = None) -> str:
    """Open *src_path* in SolidWorks via COM and export it to a STEP file; return the STEP path.

    Raises RuntimeError with an actionable message if SolidWorks/pywin32 are unavailable or the
    automation fails. SolidWorks is opened **invisibly** and the document is closed afterwards."""
    if not _win32com_available():
        raise RuntimeError("SolidWorks COM export needs pywin32 (win32com), which is not installed.")

    import pythoncom  # noqa: F401  (ensures COM is initialised on this thread)
    import win32com.client as win32

    src = str(Path(src_path).resolve())
    ext = Path(src).suffix.lower()
    doctype = _SW_DOCTYPE.get(ext, 1)
    if out_step is None:
        out_step = str(Path(tempfile.mkdtemp(prefix="sw_")) / (Path(src).stem + ".step"))

    swApp = None
    opened = False
    try:
        swApp = win32.Dispatch("SldWorks.Application")
        swApp.Visible = False
        # OpenDoc6(filename, type, options, config, errors, warnings) — use the simpler OpenDoc.
        errors = win32.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0) if hasattr(win32, "VARIANT") else 0
        try:
            model = swApp.OpenDoc6(src, doctype, 1, "", 0, 0)  # 1 = swOpenDocOptions_Silent
        except Exception:
            model = swApp.OpenDoc(src, doctype)
        if model is None:
            raise RuntimeError(f"SolidWorks could not open '{src}'")
        opened = True
        ok = model.SaveAs3(out_step, 0, 0) if hasattr(model, "SaveAs3") else model.SaveAs(out_step)
        # SaveAs* returns nonzero/True-ish on success across versions; verify by file presence.
        if not os.path.exists(out_step) or os.path.getsize(out_step) == 0:
            raise RuntimeError(f"SolidWorks STEP export produced no file (SaveAs returned {ok!r}).")
        return out_step
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"SolidWorks COM export failed: {exc}") from exc
    finally:
        try:
            if swApp is not None and opened:
                swApp.CloseDoc(src)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Experimental path — OLE2 container probe
# ---------------------------------------------------------------------------
def ole2_streams(path: str | os.PathLike) -> list[str]:
    """List the OLE2/Compound-File stream paths inside a .sldprt/.sldasm (reverse-engineering aid).

    Returns [] if olefile is unavailable or the file is not an OLE2 container."""
    try:
        import olefile
    except Exception:  # noqa: BLE001
        return []
    if not olefile.isOleFile(str(path)):
        return []
    with olefile.OleFileIO(str(path)) as ole:
        return ["/".join(parts) for parts in ole.listdir()]


def sldprt_extract_thumbnail(path: str | os.PathLike) -> bytes | None:
    """Return the embedded preview image bytes (PNG/BMP) from a .sldprt/.sldasm, or None.

    SolidWorks stores a preview under a 'PreviewPNG' / 'Preview Bitmap' style stream. This is the
    most reliably extractable artifact and is useful for shell thumbnails even when geometry can't
    be read."""
    try:
        import olefile
    except Exception:  # noqa: BLE001
        return None
    if not olefile.isOleFile(str(path)):
        return None
    with olefile.OleFileIO(str(path)) as ole:
        candidates = []
        for parts in ole.listdir():
            name = "/".join(parts).lower()
            if "preview" in name or "thumbnail" in name or name.endswith("png"):
                candidates.append(parts)
        for parts in candidates:
            try:
                data = ole.openstream(parts).read()
                if data and len(data) > 16:
                    return data
            except Exception:  # noqa: BLE001
                continue
    return None


# ---------------------------------------------------------------------------
# Portable modern container -> saved display triangle strips
# ---------------------------------------------------------------------------
_CHUNK_MARKER = b"\x14\x00\x06\x00\x08\x00"
_STRIP_MARKER = struct.pack("<III", 4, 8, 2)
_ARRAY_MARKER = struct.pack("<III", 12, 100, 2)

# In lockstep with the native Limits: retain the existing 512 MiB stream ceiling
# as an aggregate ceiling, rather than simplifying engineering geometry. Logical
# placed geometry is charged at the native 24-byte vertex / 12-byte triangle cost
# in both ports, including meshes which the native reader shares between instances.
_MAX_FILE_BYTES = 512 * 1024 * 1024
_MAX_STREAM_BYTES = 512 * 1024 * 1024
_MAX_STREAMS = 4096
_MAX_INSTANCES = 65536
_MAX_WORK = 1024 * 1024
_MAX_VERTICES = 16 * 1024 * 1024
_MAX_TRIANGLES = 16 * 1024 * 1024
_MAX_MESH_BYTES = 512 * 1024 * 1024


def _charge(used: int, amount: int, limit: int, kind: str) -> int:
    if used > limit or amount > limit - used:
        raise ValueError(f"SolidWorks {kind} budget exceeded")
    return used + amount


class _MeshBudget:
    def __init__(self) -> None:
        self.vertices = 0
        self.triangles = 0
        self.bytes = 0

    def add(self, vertices: int, triangles: int) -> None:
        self.vertices = _charge(self.vertices, vertices, _MAX_VERTICES, "aggregate vertex")
        self.triangles = _charge(self.triangles, triangles, _MAX_TRIANGLES, "aggregate triangle")
        self.bytes = _charge(self.bytes, vertices * 24 + triangles * 12,
                             _MAX_MESH_BYTES, "aggregate mesh byte")


def _inflate_raw(data: memoryview, expanded: int, name: str) -> bytes:
    # Never let a false small claim first allocate the real (potentially enormous)
    # output. One surplus byte proves a mismatch; each inflater call is also bounded.
    decoder = zlib.decompressobj(-15)
    raw = bytearray()
    for offset in range(0, len(data), 64 * 1024):
        pending = data[offset:offset + 64 * 1024]
        while pending:
            piece = decoder.decompress(pending, min(64 * 1024, expanded - len(raw) + 1))
            if len(piece) > expanded - len(raw):
                raise ValueError(f"SolidWorks stream {name!r} exceeds its claimed expanded size")
            raw.extend(piece)
            pending = decoder.unconsumed_tail
            if decoder.eof:
                break
        if decoder.eof:
            break
    if not decoder.eof or len(raw) != expanded:
        raise ValueError(f"SolidWorks stream {name!r} failed raw-deflate decoding")
    return bytes(raw)


def _rol(value: int, shift: int) -> int:
    shift &= 7
    return value if not shift else ((value << shift) | (value >> (8 - shift))) & 0xFF


def _modern_streams(path: str | os.PathLike, assembly: bool) -> dict[str, bytes]:
    with open(path, "rb") as source:
        size = os.fstat(source.fileno()).st_size
        if size > _MAX_FILE_BYTES:
            raise ValueError("SolidWorks input file byte budget exceeded")
        data = source.read(size + 1)
        if len(data) != size:
            raise ValueError("SolidWorks input file changed while reading")
    if len(data) < 22:
        raise ValueError("SolidWorks file is too short to contain a modern document")
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        raise ValueError(
            "legacy SolidWorks OLE documents need SolidWorks COM or a STEP export; "
            "portable tessellation import supports SolidWorks 2015 and newer")
    if _CHUNK_MARKER not in data[:64]:
        raise ValueError("unrecognized SolidWorks container (expected a 2015+ chunk stream)")

    def wanted(name: str) -> bool:
        if not assembly:
            return name == "Contents/DisplayLists"
        return (name == "swXmlContents/COMPINSTANCETREE" or
                (name.startswith("FaceTessellations/") and
                 name != "FaceTessellations/Directory"))

    streams: dict[str, bytes] = {}
    expanded_bytes = 0
    key = data[7]
    pos = 0
    while True:
        marker = data.find(_CHUNK_MARKER, pos)
        if marker < 0:
            break
        if marker < 4 or marker + 26 > len(data):
            pos = marker + 1
            continue
        chunk = marker - 4
        f1, compressed, expanded, name_size = struct.unpack_from("<IIII", data, chunk + 0x0E)
        if name_size > 512 or compressed > 64 * 1024 * 1024:
            pos = marker + 1
            continue
        name_at = chunk + 0x1E
        payload_at = name_at + name_size
        if payload_at > len(data):
            pos = marker + 1
            continue
        decoded = bytes(_rol(b, key) for b in data[name_at:payload_at])
        try:
            name = decoded.decode("ascii")
        except UnicodeDecodeError:
            pos = marker + 1
            continue
        if not name or any(ord(c) < 0x20 for c in name):
            pos = marker + 1
            continue
        if f1 < 65536:
            pos = marker + len(_CHUNK_MARKER)
            continue
        end = payload_at + compressed
        if end > len(data):
            pos = marker + 1
            continue
        if wanted(name) and name not in streams:
            _charge(len(streams), 1, _MAX_STREAMS, "stream count")
            if compressed == 0:
                streams[name] = b""
            else:
                if expanded <= 0 or expanded > 512 * 1024 * 1024:
                    raise ValueError(f"SolidWorks stream {name!r} has an invalid expanded size")
                expanded_bytes = _charge(expanded_bytes, expanded, _MAX_STREAM_BYTES,
                                         "aggregate expanded byte")
                raw = _inflate_raw(memoryview(data)[payload_at:end], expanded, name)
                streams[name] = raw
        pos = end
    return streams


def _face_arrays(data: bytes) -> list[tuple[int, int]]:
    candidates: list[tuple[int, int]] = []
    pos = 0
    while True:
        pos = data.find(_ARRAY_MARKER, pos)
        if pos < 0:
            break
        count = struct.unpack_from("<I", data, pos + 12)[0]
        end = pos + 16 + count * 12
        if count <= 16 * 1024 * 1024 and end <= len(data):
            candidates.append((pos, count))
        pos += 1
    grouped: list[tuple[int, int]] = []
    i = 0
    while i + 3 < len(candidates):
        a, normals, uv, extra = candidates[i:i + 4]
        if (a[1] == normals[1] and uv[1] == extra[1] == 0 and
                a[0] + 16 + a[1] * 12 <= normals[0] and
                normals[0] + 16 + normals[1] * 12 <= uv[0] < extra[0]):
            grouped.extend((a, normals, uv, extra))
            i += 4
        else:
            i += 1
    return grouped


def _strip_headers(data: bytes) -> dict[int, int]:
    headers: dict[int, int] = {}
    pos = 0
    while True:
        pos = data.find(_STRIP_MARKER, pos)
        if pos < 0:
            break
        count = struct.unpack_from("<I", data, pos + 12)[0]
        end = pos + 16 + count * 4
        if count <= 16 * 1024 * 1024 and end <= len(data):
            headers[end] = pos
        pos += 1
    return headers


def _mesh_from_face_stream(data: bytes, expected_faces: int | None = None,
                           budget: _MeshBudget | None = None):
    import numpy as np
    from services.formats.mesh_body import MeshBody

    arrays = _face_arrays(data)
    if not arrays or len(arrays) % 4:
        raise ValueError("saved SolidWorks display list contains no complete face arrays")
    face_count = len(arrays) // 4
    if expected_faces is not None and face_count != expected_faces:
        raise ValueError(
            f"SolidWorks tessellation face-count mismatch: {expected_faces} vs {face_count}")
    headers = _strip_headers(data)
    if budget is None:
        budget = _MeshBudget()
    budget.add(sum(arrays[face * 4][1] for face in range(face_count)), 0)
    verts: list[list[float]] = []
    tris: list[tuple[int, int, int]] = []
    # The second array of each group is SolidWorks' per-vertex NORMAL array. It is
    # deliberately NOT read (mirrors core/doc/solidworks_tessellation.cpp): on real parts it
    # is garbage — a turned spacer authored (0,0,±1) for every one of its 2208 vertices, and
    # another part's signs disagreed with the triangle winding on 1040 of 1176 triangles.
    # Smooth shading normals are derived from the triangle soup instead
    # (MeshBody.vertex_normals / mesh_to_shape). The four-array stride still locates positions.
    for face in range(face_count):
        offset, count = arrays[face * 4]
        header = headers.get(offset)
        if header is None:
            raise ValueError("SolidWorks face is missing its triangle-strip table")
        strip_count = struct.unpack_from("<I", data, header + 12)[0]
        lengths = list(struct.unpack_from(
            f"<{strip_count}I", data, header + 16)) if strip_count else []
        if sum(lengths) != count:
            raise ValueError("SolidWorks triangle-strip lengths do not cover the face")
        points = (np.frombuffer(data, dtype="<f4", count=count * 3, offset=offset + 16)
                  .reshape(-1, 3).astype(np.float64) * 1000.0)
        base = len(verts)
        verts.extend(points.tolist())
        cursor = 0
        for length in lengths:
            for j in range(max(0, length - 2)):
                ia, ib, ic = base + cursor + j, base + cursor + j + 1, base + cursor + j + 2
                if j & 1:
                    ia, ib = ib, ia
                a, b, c = points[cursor + j:cursor + j + 3]
                if float(np.dot(np.cross(b - a, c - a), np.cross(b - a, c - a))) > 1e-20:
                    budget.add(0, 1)
                    tris.append((ia, ib, ic))
            cursor += length
    if not tris:
        raise ValueError("saved SolidWorks display list contains no non-degenerate triangles")
    return MeshBody(np.asarray(verts, dtype=np.float32),
                    np.asarray(tris, dtype=np.uint32))


def _assembly_block_ranges(data: bytes) -> list[tuple[int, int]]:
    starts: list[int] = []
    marker = 56
    while True:
        marker = data.find(_STRIP_MARKER, marker)
        if marker < 0:
            break
        start = marker - 56
        if (struct.unpack_from("<I", data, start)[0] == len(starts) and
                struct.unpack_from("<I", data, start + 24)[0] == 1 and
                all(struct.unpack_from("<I", data, start + at)[0] == 0
                    for at in (28, 32, 40, 44, 48, 52)) and
                0 < struct.unpack_from("<I", data, start + 12)[0] < 16 * 1024 * 1024):
            starts.append(start)
        marker += 1
    return [(start, starts[i + 1] if i + 1 < len(starts) else len(data))
            for i, start in enumerate(starts)]


def _tag(element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _portable_tessellations(path: str | os.PathLike) -> list[tuple[str, Any]]:
    import numpy as np
    from services.formats.mesh_body import MeshBody

    source = Path(path)
    assembly = source.suffix.lower() == ".sldasm"
    streams = _modern_streams(source, assembly)
    source_budget = _MeshBudget()
    if not assembly:
        display = streams.get("Contents/DisplayLists")
        if not display:
            raise ValueError(
                "SolidWorks part has no saved display tessellation; rebuild/save it or export STEP")
        _charge(0, 1, _MAX_INSTANCES, "instance count")
        mesh = _mesh_from_face_stream(display, budget=source_budget)
        mesh.name = source.stem
        return [(source.stem, mesh)]

    xml = streams.get("swXmlContents/COMPINSTANCETREE")
    if not xml:
        raise ValueError("SolidWorks assembly has no component tree")
    root = ET.fromstring(xml)
    files = {e.attrib.get("id"): e.attrib.get("swDocType", "")
             for e in root.iter() if _tag(e) == "swFile"}
    models = [e for e in root.iter() if _tag(e) == "swModel"]
    model_by_id = {e.attrib["id"]: e for e in models}
    part_models = [e for e in models if files.get(e.attrib.get("swFileRef")) == "PART"]

    block_meshes = []
    for name in sorted(streams):
        if not name.startswith("FaceTessellations/"):
            continue
        data = streams[name]
        ranges = _assembly_block_ranges(data)
        if not ranges:
            raise ValueError(f"SolidWorks assembly face stream {name!r} has no component blocks")
        for start, end in ranges:
            _charge(len(block_meshes), 1, _MAX_INSTANCES, "source mesh count")
            block = data[start:end]
            block_meshes.append(_mesh_from_face_stream(
                block, struct.unpack_from("<I", block, 12)[0], source_budget))
    if len(block_meshes) != len(part_models):
        active = {r.attrib.get("swModelRef")
                  for owner in models for r in owner
                  if _tag(r) == "swReference" and r.attrib.get("swSuppressed") != "YES"}
        active_parts = [m for m in part_models if m.attrib["id"] in active]
        if len(active_parts) == len(block_meshes):
            part_models = active_parts
        else:
            raise ValueError(
                f"SolidWorks assembly has {len(part_models)} part models but "
                f"{len(block_meshes)} saved component meshes")
    mesh_by_model = {m.attrib["id"]: mesh for m, mesh in zip(part_models, block_meshes)}
    configs = [e for e in root.iter() if _tag(e) == "swConfiguration"]
    recent = next((e for e in configs if e.attrib.get("swMostRecentConfiguration") == "YES"),
                  configs[0] if configs else None)
    if recent is None or recent.attrib.get("swModelRef") not in model_by_id:
        raise ValueError("SolidWorks assembly has no resolvable root configuration")

    # A memoized preflight counts DAG paths, not unique models. Charge geometry
    # before constructing any homogeneous coordinates or copying triangle arrays.
    costs: dict[str, tuple[int, int, int, int, int]] = {}
    counting: set[str] = set()

    def count(model_id: str, depth: int) -> tuple[int, int, int, int, int]:
        if depth > 64 or model_id in counting:
            raise ValueError("SolidWorks assembly component graph is cyclic or too deep")
        if model_id in costs:
            cost = costs[model_id]
            if depth + cost[4] > 64:
                raise ValueError("SolidWorks assembly component graph is cyclic or too deep")
            return cost
        if model_id not in model_by_id:
            raise ValueError(f"SolidWorks assembly references unknown model {model_id!r}")
        counting.add(model_id)
        instances = work = height = 0
        geometry = _MeshBudget()
        for ref in model_by_id[model_id]:
            if _tag(ref) != "swReference":
                continue
            work = _charge(work, 1, _MAX_WORK, "instance work")
            if ref.attrib.get("swSuppressed") == "YES":
                continue
            ref_id = ref.attrib.get("swModelRef", "")
            if ref_id in mesh_by_model:
                mesh = mesh_by_model[ref_id]
                instances = _charge(instances, 1, _MAX_INSTANCES, "instance count")
                geometry.add(len(mesh.verts), len(mesh.tris))
            else:
                child = count(ref_id, depth + 1)
                instances = _charge(instances, child[0], _MAX_INSTANCES, "instance count")
                work = _charge(work, child[1], _MAX_WORK, "instance work")
                geometry.add(child[2], child[3])
                height = max(height, child[4] + 1)
        counting.remove(model_id)
        cost = (instances, work, geometry.vertices, geometry.triangles, height)
        costs[model_id] = cost
        return cost

    count(recent.attrib["swModelRef"], 0)

    out: list[tuple[str, Any]] = []
    used: dict[str, int] = {}
    stack: set[str] = set()
    placed_budget = _MeshBudget()
    instance_count = work_count = 0

    def expand(model_id: str, parent: np.ndarray, prefix: str, depth: int) -> None:
        nonlocal instance_count, work_count
        if depth > 64 or model_id in stack:
            raise ValueError("SolidWorks assembly component graph is cyclic or too deep")
        stack.add(model_id)
        for ref in model_by_id[model_id]:
            if _tag(ref) != "swReference":
                continue
            work_count = _charge(work_count, 1, _MAX_WORK, "instance work")
            if ref.attrib.get("swSuppressed") == "YES":
                continue
            values = [float(x) for x in ref.attrib.get("swTransform", "").split()]
            if len(values) != 16:
                raise ValueError("SolidWorks component has a malformed transform")
            world = np.asarray(values, dtype=np.float64).reshape(4, 4) @ parent
            ref_id = ref.attrib.get("swModelRef", "")
            local = ref.attrib.get("swName", "Component")
            number = ref.attrib.get("swReferenceNumber", "")
            if number:
                local += f"-{number}"
            full = f"{prefix}/{local}" if prefix else local
            if ref_id in mesh_by_model:
                base: MeshBody = mesh_by_model[ref_id]
                instance_count = _charge(instance_count, 1, _MAX_INSTANCES, "instance count")
                placed_budget.add(len(base.verts), len(base.tris))
                hom = np.column_stack((base.verts.astype(np.float64) / 1000.0,
                                       np.ones(len(base.verts))))
                placed = hom @ world
                used[full] = used.get(full, 0) + 1
                name = full if used[full] == 1 else f"{full}-{used[full]}"
                # No normals to carry: the placed instance derives its own smooth normals
                # from the transformed triangle soup (MeshBody.vertex_normals).
                mesh = MeshBody((placed[:, :3] * 1000.0).astype(np.float32),
                                base.tris.copy(), name=name)
                out.append((name, mesh))
            else:
                if ref_id not in model_by_id:
                    raise ValueError(f"SolidWorks assembly references unknown model {ref_id!r}")
                expand(ref_id, world, full, depth + 1)
        stack.remove(model_id)

    expand(recent.attrib["swModelRef"], np.eye(4), "", 0)
    if not out:
        raise ValueError("SolidWorks assembly resolved no unsuppressed part instances")
    return out


def sldprt_extract_preview_mesh(path: str | os.PathLike):
    """Recover a modern part's saved display tessellation as a :class:`MeshBody`, if present."""
    if Path(path).suffix.lower() != ".sldprt":
        return None
    try:
        return _portable_tessellations(path)[0][1]
    except Exception:  # noqa: BLE001 — probe API remains fail-soft
        return None


# ---------------------------------------------------------------------------
# Public import entry points (called from services.formats.formats.import_path)
# ---------------------------------------------------------------------------
def _via_solidworks_com(path: str | os.PathLike) -> list[tuple[str, Any]] | None:
    """Try COM→STEP; return imported bodies, or None when SolidWorks is unusable."""
    if not solidworks_available():
        return None
    try:
        step = sw_com_export_step(path)
    except Exception:  # noqa: BLE001
        return None
    from services.formats.formats import import_path
    return import_path(step, "step")


def import_solidworks(path: str | os.PathLike) -> list[tuple[str, Any]]:
    """Import SolidWorks via exact COM→STEP, else portable saved-display tessellation."""
    bodies = _via_solidworks_com(path)
    if bodies:
        return bodies
    try:
        return _portable_tessellations(path)
    except Exception as exc:
        raise ValueError(
            f"cannot read SolidWorks file '{Path(path).name}': {exc}. "
            "Open/rebuild/save it in SolidWorks or export STEP for exact analytic geometry.") from exc


def import_parasolid(path: str | os.PathLike) -> list[tuple[str, Any]]:
    """Import a Parasolid .x_t/.x_b: only via SolidWorks COM (SW imports Parasolid), else error."""
    bodies = _via_solidworks_com(path)
    if bodies:
        return bodies
    raise ValueError(
        f"cannot read Parasolid file '{Path(path).name}' — no free Parasolid kernel. Open it in "
        "SolidWorks/NX and export STEP (.step), then import that.")
