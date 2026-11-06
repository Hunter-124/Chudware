"""Watched parts-library scanner tests.

Pure-metadata: dummy STEP text files (no geometry) so this runs without OCP/build123d.
Mirrors test_vendor_parts.py by monkeypatching the registry paths to a temp dir.
"""
from __future__ import annotations

import hashlib
import json
import pytest
import os
import tempfile
import time
from pathlib import Path

from services.parts import parts_library, vendor_parts


_STEP = "ISO-10303-21;\nEND-ISO-10303-21;\n"


def _isolate_registry(root: Path) -> None:
    vendor_parts.paths.VENDOR_PARTS = root / "vendor_parts"
    vendor_parts.INDEX = vendor_parts.paths.VENDOR_PARTS / "index.json"


def test_scan_registers_and_is_idempotent() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _isolate_registry(root)
        lib = root / "library"
        (lib / "fasteners").mkdir(parents=True)
        (lib / "fasteners" / "M3x10.step").write_text(_STEP, encoding="utf-8")
        (lib / "washers").mkdir(parents=True)
        (lib / "washers" / "flat_washer.step").write_text(_STEP, encoding="utf-8")
        (lib / "loose.step").write_text(_STEP, encoding="utf-8")  # no subfolder -> "library"

        first = parts_library.scan_library_folder(str(lib))
        assert first["scanned"] == 3
        assert first["added"] == 3
        assert first["updated"] == 0

        rows = vendor_parts.list_parts()
        assert len(rows) == 3
        # supplier inferred from top-level subfolder, "library" when none
        suppliers = {r["supplier"] for r in rows}
        assert suppliers == {"fasteners", "washers", "library"}

        # specs parsed from filename "M3x10"
        screw = vendor_parts.get_part("fasteners", "M3x10")
        assert screw["specs"]["thread"] == "M3"
        assert screw["specs"]["length"] == 10
        assert screw["source_mtime"] > 0
        # registered in place (no copy) -> path points inside the library folder
        assert Path(screw["cad_path"]).resolve() == (lib / "fasteners" / "M3x10.step").resolve()

        # Re-scan with nothing changed -> idempotent (no dupes, nothing added/updated).
        second = parts_library.scan_library_folder(str(lib))
        assert second["scanned"] == 3
        assert second["added"] == 0
        assert second["updated"] == 0
        assert len(vendor_parts.list_parts()) == 3


def test_scan_detects_changed_mtime() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _isolate_registry(root)
        lib = root / "library"
        lib.mkdir(parents=True)
        f = lib / "part.step"
        f.write_text(_STEP, encoding="utf-8")

        assert parts_library.scan_library_folder(str(lib))["added"] == 1

        # Bump mtime into the future so the change is unambiguous on coarse-resolution FS.
        future = time.time() + 100
        os.utime(f, (future, future))

        res = parts_library.scan_library_folder(str(lib))
        assert res["added"] == 0
        assert res["updated"] == 1
        assert len(vendor_parts.list_parts()) == 1  # still a single record


def test_worker_method_registered() -> None:
    assert "vendor_part_scan_library" in vendor_parts.WORKER_METHODS


def test_parse_specs_from_name() -> None:
    assert parts_library.parse_specs_from_name("M3x10") == {"thread": "M3", "length": 10}
    assert parts_library.parse_specs_from_name("M6-1.0x20") == {
        "thread": "M6", "pitch": 1.0, "length": 20}
    assert parts_library.parse_specs_from_name("flat_washer") == {}


def test_scan_batches_index_persistence_and_hub_rebuild(tmp_path, monkeypatch) -> None:
    _isolate_registry(tmp_path)
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in ("Bracket-A.step", "Bracket-B.step", "Bracket-C.step"):
        (lib / name).write_text(_STEP, encoding="utf-8")

    loads: list[int] = []
    writes: list[int] = []
    rebuilds: list[int] = []
    load_index = vendor_parts._load_index
    write_index = vendor_parts._write_index

    def _load() -> dict[str, dict]:
        loads.append(1)
        return load_index()

    def _write(index: dict[str, dict]) -> None:
        writes.append(1)
        write_index(index)

    monkeypatch.setattr(vendor_parts, "_load_index", _load)
    monkeypatch.setattr(vendor_parts, "_write_index", _write)
    import services.parts as ph
    monkeypatch.setattr(ph, "rebuild_index", lambda: rebuilds.append(1))

    result = parts_library.scan_library_folder(str(lib))

    assert result["scanned"] == 3
    assert result["added"] == 3
    assert [part["part_number"] for part in result["parts"]] == [
        "Bracket-A", "Bracket-B", "Bracket-C"]
    assert loads == [1, 1], "registration reloads under the lock to avoid stale snapshots"
    assert writes == [1], "a scan must persist the vendor index once"
    assert rebuilds == [1], "a multi-file scan must rebuild PartsHub exactly once"


def test_scanned_step_is_searchable_via_parts_hub(tmp_path, monkeypatch) -> None:
    """A dropped local STEP becomes searchable by title/part-number after scan,
    and part_place routes it as import_file (editable)."""
    _isolate_registry(tmp_path)
    lib = tmp_path / "lib"
    lib.mkdir()
    step = lib / "CustomBracket-A1.step"
    step.write_text(_STEP, encoding="utf-8")

    # Avoid full hub rebuild thrash against the real bolts tables during unit test:
    # rebuild_index is still invoked; we just ensure the adapter sees the registry.
    rebuilt: list[int] = []

    def _rebuild():
        rebuilt.append(1)
        return 0

    monkeypatch.setattr("services.parts.rebuild_index", _rebuild, raising=False)
    # parts_library imports rebuild lazily inside the function; patch the hub module.
    import services.parts as ph
    monkeypatch.setattr(ph, "rebuild_index", _rebuild)

    result = parts_library.scan_library_folder(str(lib))
    assert result["added"] == 1
    assert rebuilt, "scan must refresh the PartsHub index"

    from services.parts.adapters.local_library_adapter import LocalLibraryAdapter
    adapter = LocalLibraryAdapter()
    recs = list(adapter.iter_records())
    assert recs, "local library adapter saw no records after scan"
    assert any("CustomBracket-A1" in (r.get("title") or "") for r in recs)
    hits = adapter.search("CustomBracket")
    assert hits and hits[0]["geometry_ref"]["kind"] == "import_file"
    assert hits[0]["geometry_ref"]["path"] == str(step.resolve())

    # Removal invalidates: delete the file → adapter drops it.
    step.unlink()
    assert list(adapter.iter_records()) == []




def _bundle(tmp_path, *, artifact=True):
    rights = {"redistribute": True, "license_expression": "CC-BY-4.0",
              "attribution": "Example Publisher"}
    record = {
        "part_id": "fastener:example:bolt", "domain": "fastener", "title": "Example Bolt",
        "standard": "ISO 4762", "designation": "M8", "params": {"length_mm": 20},
        "aliases": ["specialbolt"], "sourcing": [], "provenance": "Publisher data",
        "license": "CC-BY-4.0", "units": "mm", "geometry_quality": "nominal",
        "references": [{"identifier": "ISO 4762", "kind": "identifier"}],
        "rights": rights, "geometry_ref": None, "custom_metadata": {"retained": True},
    }
    root = tmp_path / "source"
    root.mkdir()
    if artifact:
        content = _STEP.encode()
        (root / "objects").mkdir()
        (root / "objects" / "bolt.step").write_bytes(content)
        record["artifact"] = {"path": "objects/bolt.step", "format": "step",
                              "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
                              "rights": dict(rights), "notice": "Model attribution"}
    data = {"schema": "aicad.parts-library/1", "library_id": "example",
            "revision": "r1", "records": [record]}
    manifest = tmp_path / "source.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    return manifest, root, data


def test_publish_ingest_index_acquire_and_offline_corruption(tmp_path, monkeypatch):
    from services.parts.adapters.local_library_adapter import LocalLibraryAdapter
    from services.parts.index import PartsIndex
    _isolate_registry(tmp_path)
    monkeypatch.setattr("services.parts.get", lambda *args, **kwargs: None)
    monkeypatch.setattr(vendor_parts, "_rebuild_parts_index", lambda: None)
    manifest, root, data = _bundle(tmp_path)
    out1, out2 = tmp_path / "published1", tmp_path / "published2"
    parts_library.publish_library(str(manifest), str(root), str(out1))
    parts_library.publish_library(str(manifest), str(root), str(out2))
    assert (out1 / "manifest.json").read_bytes() == (out2 / "manifest.json").read_bytes()
    assert (out1 / "notices/NOTICE.txt").read_bytes() == (out2 / "notices/NOTICE.txt").read_bytes()
    result = parts_library.ingest_manifest(str(out1 / "manifest.json"), artifact_root=str(out1))
    assert result == {"ok": True, "count": 1, "library_id": "example", "revision": "r1"}
    adapter = LocalLibraryAdapter()
    rec = adapter.get("fastener:example:bolt")
    assert rec["rights"] == data["records"][0]["rights"]
    assert rec["references"] == data["records"][0]["references"]
    assert rec["custom_metadata"] == {"retained": True}
    index = PartsIndex()
    index.rebuild(adapter.iter_records())
    assert index.search("specialbolt")[0]["part_id"] == rec["part_id"]
    acquired = parts_library.acquire_part(rec["part_id"], offline=True)
    path = Path(acquired["geometry_ref"]["path"])
    assert path.read_bytes() == _STEP.encode()
    assert not path.is_relative_to(root)
    path.write_text("corrupt")
    monkeypatch.setattr("services.net.safe_http.download",
                        lambda *a, **kw: pytest.fail("offline requested network"))
    with pytest.raises(ValueError, match="corrupt"):
        parts_library.acquire_part(rec["part_id"], offline=True)


def test_reference_only_missing_offline_and_reingest_metadata(tmp_path, monkeypatch):
    from services.parts.adapters.local_library_adapter import LocalLibraryAdapter
    _isolate_registry(tmp_path)
    monkeypatch.setattr("services.parts.get", lambda *a, **kw: None)
    monkeypatch.setattr(vendor_parts, "_rebuild_parts_index", lambda: None)
    manifest, root, data = _bundle(tmp_path, artifact=False)
    parts_library.ingest_manifest(str(manifest))
    rec = LocalLibraryAdapter().get("fastener:example:bolt")
    assert rec["geometry_ref"] is None and rec["units"] == "mm"
    with pytest.raises(ValueError, match="no geometry"):
        parts_library.acquire_part(rec["part_id"], offline=True)
    del data["records"][0]["custom_metadata"]
    manifest.write_text(json.dumps(data))
    parts_library.ingest_manifest(str(manifest))
    assert LocalLibraryAdapter().get(rec["part_id"])["custom_metadata"] == {"retained": True}


def test_remote_artifact_offline_and_verified_download(tmp_path, monkeypatch):
    _isolate_registry(tmp_path)
    monkeypatch.setattr("services.parts.get", lambda *a, **kw: None)
    monkeypatch.setattr(vendor_parts, "_rebuild_parts_index", lambda: None)
    manifest, root, data = _bundle(tmp_path)
    parts_library.ingest_manifest(str(manifest), mirrors=["https://parts.example/bundle/"])
    with pytest.raises(FileNotFoundError, match="offline"):
        parts_library.acquire_part("fastener:example:bolt", offline=True)
    calls = []
    def download(url, dest, **kwargs):
        calls.append((url, kwargs))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((root / "objects/bolt.step").read_bytes())
        return type("Response", (), {"status_code": 200})()
    monkeypatch.setattr("services.net.safe_http.download", download)
    record = parts_library.acquire_part("fastener:example:bolt")
    assert calls[0][1]["expected_sha256"] == data["records"][0]["artifact"]["sha256"]
    assert calls[0][1]["expected_size"] == len(_STEP.encode())
    assert Path(record["geometry_ref"]["path"]).is_file()
    assert parts_library.acquire_part(record["part_id"], offline=True)["geometry_ref"] == record["geometry_ref"]


def test_sync_pins_manifest_and_uses_origin_as_mirror(tmp_path, monkeypatch):
    _isolate_registry(tmp_path)
    monkeypatch.setattr("services.parts.get", lambda *a, **kw: None)
    monkeypatch.setattr(vendor_parts, "_rebuild_parts_index", lambda: None)
    manifest, _, _ = _bundle(tmp_path)
    payload = manifest.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    def download(url, dest, **kwargs):
        assert url == "https://parts.example/r1/manifest.json"
        assert kwargs["expected_sha256"] == digest
        assert kwargs["max_bytes"] == parts_library._MAX_MANIFEST
        dest.write_bytes(payload)
        return type("Response", (), {"status_code": 200,
                                     "url": "https://parts.example/releases/r2/manifest.json"})()
    monkeypatch.setattr("services.net.safe_http.download", download)
    result = parts_library.sync_manifest("https://parts.example/r1/manifest.json",
                                         expected_sha256=digest)
    assert result["count"] == 1
    assert vendor_parts._load_index()["manifest::fastener:example:bolt"]["mirrors"] == [
        "https://parts.example/releases/r2/"]
    with pytest.raises(ValueError, match="HTTPS"):
        parts_library.sync_manifest("http://parts.example/manifest.json",
                                    expected_sha256=digest, mirrors=["https://parts.example/"])


@pytest.mark.parametrize("path", ["/absolute.step", "../escape.step", "objects/%2e%2e/x.step",
                                  "https://host/file.step", "C:\\part.step"])
def test_ingest_rejects_unsafe_artifact_paths(tmp_path, path):
    manifest, _, data = _bundle(tmp_path)
    data["records"][0]["artifact"]["path"] = path
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        parts_library.ingest_manifest(str(manifest))


@pytest.mark.parametrize("mutation", ["rights", "artifact_rights", "traversal", "hash", "size"])
def test_manifest_rejects_unsafe_publication(tmp_path, mutation):
    manifest, root, data = _bundle(tmp_path)
    record = data["records"][0]
    if mutation == "rights":
        record["rights"]["redistribute"] = False
    elif mutation == "artifact_rights":
        record["artifact"]["rights"]["license_expression"] = "unknown"
    elif mutation == "traversal":
        record["artifact"]["path"] = "../secret.step"
    elif mutation == "hash":
        record["artifact"]["sha256"] = "0" * 64
    else:
        record["artifact"]["size_bytes"] += 1
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        parts_library.publish_library(str(manifest), str(root), str(tmp_path / "out"))


def test_manifest_rejects_unrelated_namespaced_collision(tmp_path, monkeypatch):
    _isolate_registry(tmp_path)
    monkeypatch.setattr("services.parts.get", lambda *a, **kw: {"part_id": a[0]})
    manifest, _, _ = _bundle(tmp_path, artifact=False)
    with pytest.raises(ValueError, match="collision"):
        parts_library.ingest_manifest(str(manifest))


def test_namespace_source_replacement_and_geometry_removal(tmp_path, monkeypatch):
    from services.parts.adapters.local_library_adapter import LocalLibraryAdapter
    _isolate_registry(tmp_path)
    monkeypatch.setattr("services.parts.get", lambda *a, **kw: None)
    monkeypatch.setattr(vendor_parts, "_rebuild_parts_index", lambda: None)
    manifest, root, data = _bundle(tmp_path)
    parts_library.ingest_manifest(str(manifest), artifact_root=str(root))
    key = "manifest::fastener:example:bolt"
    before = vendor_parts._load_index()
    other = tmp_path / "other.json"
    data["records"][0]["title"] = "Replacement"
    other.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="source mismatch"):
        parts_library.ingest_manifest(str(other), artifact_root=str(root))
    assert vendor_parts._load_index() == before
    with pytest.raises(ValueError, match="boolean"):
        parts_library.ingest_manifest(str(other), artifact_root=str(root), replace_namespace="true")
    parts_library.ingest_manifest(str(other), artifact_root=str(root), replace_namespace=True)
    data["records"][0].pop("artifact")
    data["records"][0]["geometry_ref"] = {"kind": "core_family", "family": "bolt", "size": "M8"}
    data["records"][0].pop("custom_metadata")
    other.write_text(json.dumps(data))
    parts_library.ingest_manifest(str(other))
    row = vendor_parts._load_index()[key]
    assert "artifact" not in row["part_record"]
    assert row["cad_path"] == ""
    record = parts_library.acquire_part("fastener:example:bolt", offline=True)
    assert record["geometry_ref"]["kind"] == "core_family"
    assert LocalLibraryAdapter().get(record["part_id"])["custom_metadata"] == {"retained": True}
    data["records"][0].pop("geometry_ref")
    other.write_text(json.dumps(data))
    parts_library.ingest_manifest(str(other))
    with pytest.raises(ValueError, match="no geometry"):
        parts_library.acquire_part(record["part_id"], offline=True)


def test_denied_external_acquisition_preserves_offline_reuse(tmp_path, monkeypatch):
    from services.mcp import tool_registry
    import services.parts as parts
    _isolate_registry(tmp_path)
    monkeypatch.setattr(parts, "get", lambda *a, **kw: None)
    monkeypatch.setattr(vendor_parts, "_rebuild_parts_index", lambda: None)
    manifest, root, _ = _bundle(tmp_path)
    parts_library.ingest_manifest(str(manifest), mirrors=["https://parts.example/"])
    monkeypatch.setattr("services.net.safe_http.download",
                        lambda *a, **kw: pytest.fail("denied network reached"))
    tool_registry.set_policy(deny_risk=["external"])
    try:
        with pytest.raises(PermissionError, match="external"):
            parts_library.acquire_part("fastener:example:bolt")
        with pytest.raises(PermissionError, match="external"):
            parts_library.acquire_part("missing:example:part")
        with pytest.raises(PermissionError, match="external"):
            parts_library.sync_manifest("https://parts.example/manifest.json", expected_sha256="0" * 64)
        parts_library.ingest_manifest(str(manifest), artifact_root=str(root))
        assert parts_library.acquire_part("fastener:example:bolt")["geometry_ref"]["kind"] == "import_file"
    finally:
        tool_registry.reset_policy()


@pytest.mark.parametrize("name", ["Manifest.json", "manifest.json/child.step",
                                   "notices", "NOTICES/object.step"])
def test_publication_rejects_portable_metadata_collisions_before_copy(tmp_path, name):
    manifest, root, data = _bundle(tmp_path)
    artifact = data["records"][0]["artifact"]
    artifact["path"] = name
    dest = root / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_STEP.encode())
    manifest.write_text(json.dumps(data))
    output = tmp_path / "output"
    with pytest.raises(ValueError):
        parts_library.publish_library(str(manifest), str(root), str(output))
    assert not output.exists()


def test_publication_rejects_case_and_ancestor_object_collisions(tmp_path, monkeypatch):
    import copy
    manifest, root, data = _bundle(tmp_path)
    second = copy.deepcopy(data["records"][0])
    second["part_id"] = "fastener:example:other"
    second["artifact"]["path"] = "objects/BOLT.step"
    (root / "objects/BOLT.step").write_bytes(_STEP.encode())
    data["records"].append(second)
    manifest.write_text(json.dumps(data))
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="collision"):
        parts_library.publish_library(str(manifest), str(root), str(output))
    assert not output.exists()
    second["artifact"]["path"] = "Objects/BOLT.step/child.step"
    source = parts_library._source
    monkeypatch.setattr(parts_library, "_source",
                        lambda base, rel: source(base, "objects/bolt.step") if
                        rel == "Objects/BOLT.step/child.step" else source(base, rel))
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="collision"):
        parts_library.publish_library(str(manifest), str(root), str(output))
    assert not output.exists()


def test_object_url_encodes_unicode_and_spaces_once(tmp_path, monkeypatch):
    _isolate_registry(tmp_path)
    monkeypatch.setattr("services.parts.get", lambda *a, **kw: None)
    monkeypatch.setattr(vendor_parts, "_rebuild_parts_index", lambda: None)
    manifest, root, data = _bundle(tmp_path)
    data["records"][0]["artifact"]["path"] = "objects/shaft é bracket.step"
    manifest.write_text(json.dumps(data))
    parts_library.ingest_manifest(str(manifest), mirrors=["https://parts.example/bundle/"])
    urls = []
    def download(url, dest, **kwargs):
        urls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(_STEP.encode())
        return type("Response", (), {"status_code": 200})()
    monkeypatch.setattr("services.net.safe_http.download", download)
    parts_library.acquire_part("fastener:example:bolt")
    assert urls == ["https://parts.example/bundle/objects/shaft%20%C3%A9%20bracket.step"]


def test_scad_artifact_is_published_ingested_and_reused(tmp_path, monkeypatch):
    _isolate_registry(tmp_path)
    monkeypatch.setattr("services.parts.get", lambda *a, **kw: None)
    monkeypatch.setattr(vendor_parts, "_rebuild_parts_index", lambda: None)
    manifest, root, data = _bundle(tmp_path)
    content = b"cube([10, 20, 30]);\n"
    (root / "objects/model.scad").write_bytes(content)
    artifact = data["records"][0]["artifact"]
    artifact.update(path="objects/model.scad", format="scad", size_bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest())
    manifest.write_text(json.dumps(data))
    output = tmp_path / "output"
    parts_library.publish_library(str(manifest), str(root), str(output))
    parts_library.ingest_manifest(str(output / "manifest.json"), artifact_root=str(output))
    record = parts_library.acquire_part("fastener:example:bolt", offline=True)
    assert Path(record["geometry_ref"]["path"]).suffix == ".scad"
    assert Path(record["geometry_ref"]["path"]).read_bytes() == content


def main() -> None:
    test_scan_registers_and_is_idempotent()
    test_scan_detects_changed_mtime()
    test_worker_method_registered()
    test_parse_specs_from_name()
    print("PARTS LIBRARY TESTS PASSED")


if __name__ == "__main__":
    main()
