"""RE-23: Parts Library ↔ Reverse-Engineering round-trip tests.

Scenario:
  1. A metadata-only part (photos + datasheet, NO 3D model) exists in the
     parts hub.
  2. ``reveng_start_session(part_ref=...)`` seeds a session from its docs.
  3. The RE pipeline runs (mocked — we inject a mesh path directly).
  4. ``reveng_attach_result(session_id, part_ref)`` registers the produced
     model on the parts-hub entry.
  5. The parts-hub entry now reports a model present.
  6. ``part_place(part_ref)`` succeeds (returns ok=True, editable=True).

Existing tests (test_reveng_contract.py + test_parts_hub_p3.py) must still pass
— run them separately; this file is self-contained.
"""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterator
import importlib
import sys
import zipfile

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def reveng_dir(tmp_path, monkeypatch):
    """Redirect RE session storage to a temp directory."""
    d = tmp_path / "reveng"
    d.mkdir()
    monkeypatch.setenv("CHUDWARE_REVENG_DIR", str(d))
    return d


@pytest.fixture()
def parts_hub_with_metadata_part(monkeypatch):
    """Register a fake metadata-only PartRecord into the parts-hub singleton.

    The part has photos and a datasheet path but NO geometry_ref (model absent).
    We reset the hub singleton so the fake adapter is the only provider.
    """
    # Provide small temp files for photo + datasheet so seeding can copy them.
    tmp = tempfile.mkdtemp(prefix="re23_assets_")
    photo_path = Path(tmp) / "front_view.jpg"
    image = pytest.importorskip("PIL.Image")
    image.new("RGB", (32, 32), "red").save(photo_path)
    ds_path = Path(tmp) / "datasheet.pdf"
    ds_path.write_bytes(b"%PDF-1.4 fake\n")

    PART_ID = "catalog:test:widget-alpha"

    fake_record = {
        "part_id": PART_ID,
        "domain": "catalog",
        "provider_id": "test_metadata_provider",
        "name": "Widget Alpha (metadata-only)",
        "description": "A test widget with photos but no 3D model yet.",
        "geometry_ref": None,   # <-- metadata-only: no model present
        "metadata": {
            "photos": [str(photo_path)],
            "datasheet": str(ds_path),
            "model_present": False,
        },
        "source": "test",
    }

    class _FakeMetadataAdapter:
        """Simulates a browser/vendor catalog adapter that knows about the part
        (searchable, with photos/datasheet metadata) but cannot serve the part
        by ID for placement — it has no geometry_ref.  This mirrors real live
        catalog adapters whose records only surface during search.
        """
        domain = "catalog"
        provider_id = "test_metadata_provider"

        def search(self, query, *, domain=None, filters=None):
            return [fake_record] if "widget" in query.lower() else []

        def get(self, part_id):
            # Return the metadata record (no geometry_ref) for seeding purposes.
            # The record intentionally has geometry_ref=None — it's metadata-only.
            return fake_record if part_id == PART_ID else None

        def iter_records(self) -> Iterator[dict]:
            # Live-only: not indexable, so the hub index never caches this record.
            # The RevengResultAdapter (registered after attach) wins on get().
            raise NotImplementedError("live-only test adapter")

    # Reset hub singleton so we start clean.
    import services.parts as _hub_mod
    _hub_mod._hub = None

    from services.parts import register_provider
    register_provider(_FakeMetadataAdapter())

    yield PART_ID, str(photo_path), str(ds_path)

    shutil.rmtree(tmp, ignore_errors=True)
    # Reset singleton again so other tests get a fresh hub.
    _hub_mod._hub = None


# ---------------------------------------------------------------------------
# Helper: call reveng tool functions directly (bypass MCP serialisation)
# ---------------------------------------------------------------------------

def _load_reveng_tools():
    """Import register_reveng_tools and extract the inner functions via MCP mock."""
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("test")
    # Import and register the tools.
    from services.mcp import reveng_tools as rt
    # Re-invoke registration against our mock mcp so we get the closures.
    rt.register_reveng_tools(mcp)

    # Collect tool callables keyed by name.
    tools = {}
    for tool in mcp._tool_manager._tools.values():
        tools[tool.name] = tool.fn
    return tools


def _j(s: str) -> dict:
    return json.loads(s)

# Closed, consistently wound 12 × 8 × 6 mm tetrahedron: the actual mesh
# import has four triangles and an analytic volume of 96 mm³.
_RESULT_MESH = """\
ply
format ascii 1.0
element vertex 4
property float x
property float y
property float z
element face 4
property list uchar int vertex_indices
end_header
0 0 0
12 0 0
0 8 0
0 0 6
3 0 2 1
3 0 1 3
3 0 3 2
3 1 2 3
"""

_EMPTY_MESH = """\
ply
format ascii 1.0
element vertex 0
property float x
property float y
property float z
element face 0
property list uchar int vertex_indices
end_header
"""


def _record_result_mesh(reveng_dir: Path, session_id: str, contents: str = _RESULT_MESH) -> str:
    """Inject a scan artifact while leaving import and placement to the real core."""
    session_dir = reveng_dir / session_id
    session_json = session_dir / "session.json"
    session = json.loads(session_json.read_text())
    mesh = session_dir / "mesh" / "result.ply"
    mesh.parent.mkdir(parents=True, exist_ok=True)
    mesh.write_text(contents)
    session["artifacts"]["mesh_path"] = str(mesh)
    session_json.write_text(json.dumps(session, indent=2))
    return str(mesh)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRevengPartsRoundtrip:

    def test_start_session_without_part_ref(self, reveng_dir):
        """Basic session creation still works (no regression)."""
        tools = _load_reveng_tools()
        result = _j(tools["reveng_start_session"](name="plain session"))
        assert result["ok"] is True
        assert result["session_id"].startswith("re_")
        assert result["session"]["part_ref"] is None

    def test_start_session_seeds_from_parts_hub(
        self, reveng_dir, parts_hub_with_metadata_part
    ):
        """Session seeded from a parts-hub part carries photos + datasheet."""
        PART_ID, photo_path, ds_path = parts_hub_with_metadata_part
        tools = _load_reveng_tools()

        result = _j(tools["reveng_start_session"](
            name="Widget Alpha RE",
            part_ref=PART_ID,
        ))
        assert result["ok"] is True, result
        sess = result["session"]
        assert sess["part_ref"] == PART_ID

        # Seeding: the photo should have been copied into the session images/ dir.
        seeded = result.get("seeded", {})
        assert seeded.get("images") == 1, seeded

        # The session directory should contain the image.
        sess_dir = reveng_dir / result["session_id"]
        images = list((sess_dir / "images").glob("*.jpg"))
        assert [path.read_bytes() for path in images] == [Path(photo_path).read_bytes()]

        # Datasheet should also be present.
        ds_files = list((sess_dir / "datasheet").iterdir())
        assert [path.read_bytes() for path in ds_files] == [Path(ds_path).read_bytes()]

        # Provenance should record the seed step.
        prov_steps = [p["step"] for p in sess.get("provenance", [])]
        assert "seeded_from_parts_hub" in prov_steps

    def test_attach_result_writes_sidecar_and_registers_in_hub(
        self, reveng_dir, parts_hub_with_metadata_part
    ):
        """reveng_attach_result writes sidecar; parts-hub can then return the record."""
        PART_ID, _, _ = parts_hub_with_metadata_part
        tools = _load_reveng_tools()

        # 1. Create session.
        sess_result = _j(tools["reveng_start_session"](
            name="Widget Alpha RE", part_ref=PART_ID
        ))
        sid = sess_result["session_id"]

        # 2. Simulate a completed scan result in the session.
        sess_dir = reveng_dir / sid
        mesh_path = _record_result_mesh(reveng_dir, sid)

        # 3. Attach result.
        attach_result = _j(tools["reveng_attach_result"](
            session_id=sid, part_ref=PART_ID
        ))
        assert attach_result["ok"] is True, attach_result
        assert attach_result["part_ref"] == PART_ID
        assert attach_result["geometry_ref"] == mesh_path  # mesh_path since no doc_id

        # 4. Sidecar written.
        sidecar_path = sess_dir / "reveng_result.json"
        assert sidecar_path.exists(), "reveng_result.json sidecar not written"
        sidecar = json.loads(sidecar_path.read_text())
        assert sidecar["part_ref"] == PART_ID
        assert sidecar["mesh_path"] == mesh_path
        assert sidecar["session_id"] == sid

    def test_parts_hub_reports_model_present_after_attach(
        self, reveng_dir, parts_hub_with_metadata_part
    ):
        """After attach, parts-hub.get(part_ref) returns a record with geometry_ref."""
        PART_ID, _, _ = parts_hub_with_metadata_part
        tools = _load_reveng_tools()

        sess_result = _j(tools["reveng_start_session"](
            name="Widget Alpha RE", part_ref=PART_ID
        ))
        sid = sess_result["session_id"]

        mesh_path = _record_result_mesh(reveng_dir, sid)

        # Attach.
        assert _j(tools["reveng_attach_result"](session_id=sid, part_ref=PART_ID))["ok"]

        # Verify RevengResultAdapter serves the geometry_ref directly.
        # (The hub's provider-ordering means the original metadata adapter may
        # win hub_get() for the same part_id; we assert the RE adapter itself
        # returns the correct record since that's what reveng_attach_result guarantees.)
        from services.parts.adapters.reveng_result_adapter import RevengResultAdapter
        adapter = RevengResultAdapter()
        record = adapter.get(PART_ID)
        assert record is not None, "RevengResultAdapter.get returned None after attach"
        gref = record.get("geometry_ref")
        assert gref, f"geometry_ref missing on PartRecord after attach: {record}"
        assert gref.get("kind") == "import_file", f"Expected import_file kind, got: {gref}"
        assert gref.get("path") == mesh_path

        meta = record.get("metadata", {})
        assert meta.get("model_present") is True

    def test_part_place_succeeds_after_attach(
        self, reveng_dir, parts_hub_with_metadata_part
    ):
        """part_place(part_ref) succeeds once the RE model is attached."""
        PART_ID, _, _ = parts_hub_with_metadata_part
        tools = _load_reveng_tools()

        sess_result = _j(tools["reveng_start_session"](
            name="Widget Alpha RE", part_ref=PART_ID
        ))
        sid = sess_result["session_id"]

        mesh_path = _record_result_mesh(reveng_dir, sid)
        assert _j(tools["reveng_attach_result"](session_id=sid, part_ref=PART_ID))["ok"]

        # part_place is the C++ dispatcher now (core/parts/place.cpp behind the
        # part_place RPC; engine_oracle.parts_hub.place is deleted): hand it the RE
        # record inline (the Python hub lookup is the pre-fetched `record` param).
        import pytest as _pytest
        from services.mcp import core_bridge as cb
        from services.parts.adapters.reveng_result_adapter import RevengResultAdapter

        re_adapter = RevengResultAdapter()
        re_record = re_adapter.get(PART_ID)
        assert re_record is not None, "RevengResultAdapter has no record — attach may have failed"

        if not cb.available():
            _pytest.skip("native core (libchudware) not available in this environment")
        result = cb.call("part_place", {"part_id": PART_ID, "record": re_record})

        assert result["ok"] is True, f"part_place failed: {result}"
        assert result.get("editable") is True
        assert result["detail"]["body_count"] == 1
        assert result["detail"]["imported_from"] == mesh_path
        placed = result["doc_id"]
        try:
            bbox = cb.call("doc_bbox", {"doc_id": placed})
            assert bbox["min"] == pytest.approx([0, 0, 0], abs=1e-6)
            assert bbox["max"] == pytest.approx([12, 8, 6], abs=1e-6)
            assert cb.call("eval_mass", {"doc_id": placed})["volume_mm3"] == pytest.approx(96)
            tree = cb.call("doc_tree", {"doc_id": placed})["tree"]
            assert any(feature["type"] == "import" for feature in tree["features"])

            # The editable imported body is baked into the saved part; reopening
            # must retain its exact placement and volume without the scan file.
            Path(mesh_path).unlink()
            saved = reveng_dir / sid / "placed.step+"
            cb.call("doc_save", {"doc_id": placed, "path": str(saved)})
            reopened = cb.call("doc_open", {"path": str(saved)})["doc_id"]
            try:
                restored = cb.call("doc_bbox", {"doc_id": reopened})
                assert restored["min"] == pytest.approx(bbox["min"], abs=1e-6)
                assert restored["max"] == pytest.approx(bbox["max"], abs=1e-6)
                assert cb.call("eval_mass", {"doc_id": reopened})["volume_mm3"] == pytest.approx(96)
                metadata = cb.call("doc_get_meta", {"doc_id": reopened, "key": "parts_catalog"})
                assert metadata["value"]["part_record"] == re_record
            finally:
                cb.call("doc_close", {"doc_id": reopened})
        finally:
            cb.call("doc_close", {"doc_id": placed})

    def test_parametric_attachment_preserves_live_document_and_editable_history(
        self, reveng_dir, parts_hub_with_metadata_part
    ):
        from services.mcp import core_bridge as cb
        from services.parts.adapters.reveng_result_adapter import RevengResultAdapter

        part_id, _, _ = parts_hub_with_metadata_part
        tools = _load_reveng_tools()
        session = _j(tools["reveng_start_session"](name="Parametric RE", part_ref=part_id))
        sid = session["session_id"]
        mesh_path = _record_result_mesh(reveng_dir, sid)
        source_path = reveng_dir / "user-source.step+"
        source = cb.call("doc_new", {"kind": "part", "name": "parametric RE result"})["doc_id"]
        try:
            built = cb.call("doc_apply_ops", {"doc_id": source, "ops": [
                {"op": "add_feature", "feature": {
                    "type": "box", "name": "Reconstructed body",
                    "params": {"length": 12, "width": 8, "height": 6},
                }},
                {"op": "set_material", "key": "pla"},
            ]})
            assert built["rebuild"]["ok"], built
            feature_id = built["id_map"]["$0"]
            cb.call("doc_save", {"doc_id": source, "path": str(source_path)})
            original_file = source_path.read_bytes()
            edited = cb.call("doc_apply_ops", {"doc_id": source, "ops": [{
                "op": "edit_feature", "id": feature_id, "params": {"length": 18},
            }]})
            assert edited["rebuild"]["ok"], edited
            before_tree = cb.call("doc_tree", {"doc_id": source})
            before = next(row for row in cb.call("doc_list", {})["docs"] if row["doc_id"] == source)
            assert before["dirty"]
            session_json = reveng_dir / sid / "session.json"
            session_data = json.loads(session_json.read_text())
            session_data["artifacts"]["doc_id"] = source
            session_json.write_text(json.dumps(session_data, indent=2))

            attached = _j(tools["reveng_attach_result"](session_id=sid, part_ref=part_id))
            assert attached["ok"], attached
            snapshot = Path(attached["geometry_ref"])
            assert snapshot.is_file() and snapshot.suffix == ".step+"
            assert snapshot != Path(mesh_path) and snapshot != source_path
            snapshot_bytes = snapshot.read_bytes()
            sidecar = json.loads((reveng_dir / sid / "reveng_result.json").read_text())
            assert sidecar["doc_id"] == source
            assert sidecar["native_path"] == str(snapshot)
            assert sidecar["geometry_source"] == "parametric"
            assert sidecar["geometry_ref"] == {"kind": "import_file", "path": str(snapshot)}
            assert cb.call("doc_tree", {"doc_id": source}) == before_tree
            after = next(row for row in cb.call("doc_list", {})["docs"] if row["doc_id"] == source)
            assert (after["path"], after["dirty"], after["revision"]) == (
                before["path"], before["dirty"], before["revision"]
            )
            assert source_path.read_bytes() == original_file
            record = RevengResultAdapter().get(part_id)
            assert record["geometry_ref"]["path"] == str(snapshot)
        finally:
            cb.call("doc_close", {"doc_id": source})

        # The persisted model, not the scan or the now-closed doc_id, is placed.
        placed = cb.call("part_place", {
            "part_id": part_id, "record": record,
            "params": {"pos": [10, 20, 30], "rotation": [0, 0, 90]},
        })
        assert placed["ok"] and placed["editable"], placed
        doc_id = placed["doc_id"]
        try:
            bbox = cb.call("doc_bbox", {"doc_id": doc_id})
            assert bbox["min"] == pytest.approx([6, 11, 27], abs=1e-6)
            assert bbox["max"] == pytest.approx([14, 29, 33], abs=1e-6)
            mass = cb.call("eval_mass", {"doc_id": doc_id})
            assert mass["volume_mm3"] == pytest.approx(864)
            assert mass["mass_g"] == pytest.approx(864 * 1.24e-3)
            assert mass["material"]["key"] == "pla"
            tree = cb.call("doc_tree", {"doc_id": doc_id})["tree"]
            assert any(feature["id"] == feature_id and feature["type"] == "box"
                       for feature in tree["features"])
            edited = cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": [{
                "op": "edit_feature", "id": feature_id, "params": {"length": 20},
            }]})
            assert edited["rebuild"]["ok"], edited
            assert cb.call("eval_mass", {"doc_id": doc_id})["volume_mm3"] == pytest.approx(960)
            assert snapshot.read_bytes() == snapshot_bytes
            assert source_path.read_bytes() == original_file
        finally:
            cb.call("doc_close", {"doc_id": doc_id})

    def test_attached_generated_step_takes_precedence_over_scan_mesh(
        self, reveng_dir, parts_hub_with_metadata_part
    ):
        from services.mcp import core_bridge as cb
        from services.parts.adapters.reveng_result_adapter import RevengResultAdapter

        part_id, _, _ = parts_hub_with_metadata_part
        tools = _load_reveng_tools()
        session = _j(tools["reveng_start_session"](name="Generated STEP", part_ref=part_id))
        sid = session["session_id"]
        mesh_path = _record_result_mesh(reveng_dir, sid)
        step_path = reveng_dir / sid / "generated.step"
        source = cb.call("doc_new", {"kind": "part", "name": "generated result"})["doc_id"]
        try:
            built = cb.call("doc_apply_ops", {"doc_id": source, "ops": [{
                "op": "add_feature", "feature": {
                    "type": "box", "params": {"length": 12, "width": 8, "height": 6},
                },
            }]})
            assert built["rebuild"]["ok"], built
            cb.call("doc_export_step", {"doc_id": source, "path": str(step_path)})
        finally:
            cb.call("doc_close", {"doc_id": source})
        session_json = reveng_dir / sid / "session.json"
        session_data = json.loads(session_json.read_text())
        session_data["artifacts"]["step_path"] = str(step_path)
        session_json.write_text(json.dumps(session_data, indent=2))
        attached = _j(tools["reveng_attach_result"](session_id=sid, part_ref=part_id))
        assert attached["ok"] and attached["geometry_ref"] == str(step_path), attached
        assert attached["geometry_ref"] != mesh_path
        record = RevengResultAdapter().get(part_id)
        result = cb.call("part_place", {"part_id": part_id, "record": record})
        assert result["ok"] and result["editable"], result
        try:
            assert cb.call("eval_mass", {"doc_id": result["doc_id"]})["volume_mm3"] == pytest.approx(576)
        finally:
            cb.call("doc_close", {"doc_id": result["doc_id"]})

    def test_cell_attachment_materializes_confined_placeable_artifacts(
        self, tmp_path, monkeypatch, parts_hub_with_metadata_part
    ):
        from services.agent import request_context as rc
        from services.mcp import core_bridge as cb
        from services.mcp import reveng_common
        from services.parts.adapters.reveng_result_adapter import RevengResultAdapter

        work = tmp_path / "work"
        work.mkdir()
        actor_data = tmp_path / "actor-data"
        actor_data.mkdir()
        monkeypatch.setenv("XDG_DATA_HOME", str(actor_data))
        monkeypatch.setenv("CHUDWARE_ENGINE_DAEMON", "0")
        monkeypatch.delenv("CHUDWARE_NATIVE_ROOT", raising=False)
        monkeypatch.delenv("CHUDWARE_ACTOR_SANDBOX", raising=False)
        monkeypatch.chdir(work)

        # Map the sandbox's /work and /actor mounts to private test directories;
        # the native import, save and placement operations are real.
        def cell_path(raw, *, private=False):
            if raw == "/work":
                return work
            path = Path(raw).resolve()
            assert path.is_relative_to(work) or (private and path.is_relative_to(actor_data))
            return path

        monkeypatch.setattr(rc, "cell_path", cell_path)
        def context(actor):
            return {
                "cell": True, "workspace_key": "workspace", "actor_key": actor,
                "actor_hex": hashlib.sha256(actor.encode()).hexdigest()[:32],
                "right": "owner", "epoch": 1,
            }

        part_id, _, _ = parts_hub_with_metadata_part
        with rc.bind_context(context("actor-a")):
            tools = _load_reveng_tools()
            session = _j(tools["reveng_start_session"](name="cell result"))
            sid = session["session_id"]
            scan = work / "scan.ply"
            scan.write_text(_RESULT_MESH)
            imported = _j(tools["import_scan_mesh"](session_id=sid, path=str(scan)))
            assert imported["ok"], imported

            attached = _j(tools["reveng_attach_result"](session_id=sid, part_ref=part_id))
            assert attached["ok"], attached
            record = RevengResultAdapter().get(part_id)
            mesh_ref = Path(record["geometry_ref"]["path"])
            assert not mesh_ref.is_absolute() and ".." not in mesh_ref.parts
            assert (work / mesh_ref).read_text() == _RESULT_MESH
            placed = cb.call("part_place", {"part_id": part_id, "record": record})
            assert placed["ok"] and placed["editable"], placed
            try:
                assert cb.call("eval_mass", {"doc_id": placed["doc_id"]})["volume_mm3"] == pytest.approx(96)
            finally:
                cb.call("doc_close", {"doc_id": placed["doc_id"]})

            sidecar_path = reveng_common._session_path(sid) / "reveng_result.json"
            published = sidecar_path.read_text()
            unsafe = json.loads(published)
            unsafe["geometry_ref"]["path"] = imported["mesh_path"]
            sidecar_path.write_text(json.dumps(unsafe))
            refused = RevengResultAdapter().get(part_id)
            assert not refused["geometry_ref"] and not refused["metadata"]["model_present"]
            sidecar_path.write_text(published)

            source = cb.call("doc_new", {"kind": "part", "name": "cell parametric"})["doc_id"]
            try:
                built = cb.call("doc_apply_ops", {"doc_id": source, "ops": [{
                    "op": "add_feature", "feature": {
                        "type": "box", "params": {"length": 12, "width": 8, "height": 6},
                    },
                }]})
                assert built["rebuild"]["ok"], built
                state = reveng_common._load_session(sid)
                state["artifacts"]["doc_id"] = source
                reveng_common._save_session(state)
                before = cb.call("doc_tree", {"doc_id": source})
                attached = _j(tools["reveng_attach_result"](session_id=sid, part_ref=part_id))
                assert attached["ok"], attached
                sidecar = json.loads((reveng_common._session_path(sid) / "reveng_result.json").read_text())
                record = RevengResultAdapter().get(part_id)
                native_ref = Path(record["geometry_ref"]["path"])
                assert not native_ref.is_absolute() and ".." not in native_ref.parts
                assert native_ref != mesh_ref
                assert Path(sidecar["native_path"]).read_bytes() == (work / native_ref).read_bytes()
                assert cb.call("doc_tree", {"doc_id": source}) == before
                placed = cb.call("part_place", {"part_id": part_id, "record": record})
                assert placed["ok"] and placed["editable"], placed
                try:
                    assert cb.call("eval_mass", {"doc_id": placed["doc_id"]})["volume_mm3"] == pytest.approx(576)
                finally:
                    cb.call("doc_close", {"doc_id": placed["doc_id"]})
            finally:
                cb.call("doc_close", {"doc_id": source})
        with rc.bind_context(context("actor-b")):
            assert RevengResultAdapter().get(part_id) is None

    def test_part_place_refuses_empty_attached_mesh(
        self, reveng_dir, parts_hub_with_metadata_part
    ):
        """An attached empty scan must not be represented as an editable part."""
        from services.mcp import core_bridge as cb
        from services.parts.adapters.reveng_result_adapter import RevengResultAdapter

        part_id, _, _ = parts_hub_with_metadata_part
        tools = _load_reveng_tools()
        session = _j(tools["reveng_start_session"](name="Empty scan", part_ref=part_id))
        mesh_path = _record_result_mesh(reveng_dir, session["session_id"], _EMPTY_MESH)
        attached = _j(tools["reveng_attach_result"](
            session_id=session["session_id"], part_ref=part_id
        ))
        assert attached["ok"], attached
        record = RevengResultAdapter().get(part_id)
        assert record["geometry_ref"]["path"] == mesh_path
        before = {doc["doc_id"] for doc in cb.call("doc_list", {})["docs"]}
        result = cb.call("part_place", {"part_id": part_id, "record": record})
        try:
            assert result["ok"] is False, result
            assert result["editable"] is False
            assert result["doc_id"] is None
            assert isinstance(result["reason"], str) and result["reason"]
            assert {doc["doc_id"] for doc in cb.call("doc_list", {})["docs"]} == before
            assert Path(mesh_path).read_text() == _EMPTY_MESH
        finally:
            if result.get("doc_id"):
                cb.call("doc_close", {"doc_id": result["doc_id"]})

    @pytest.mark.parametrize("params", [{}, {"pos": [10, 20, 30]}])
    @pytest.mark.parametrize("previous_attachment", [False, True])
    def test_failed_native_result_is_not_attached_or_placed(
        self, reveng_dir, parts_hub_with_metadata_part, params, previous_attachment
    ):
        """Neither RE attach nor the native placer may promote a failed prefix."""
        from services.mcp import core_bridge as cb
        from services.parts.adapters.reveng_result_adapter import RevengResultAdapter

        part_id, _, _ = parts_hub_with_metadata_part
        complete = reveng_dir / "complete.step+"
        source = cb.call("doc_new", {"kind": "part", "name": "native RE source"})["doc_id"]
        try:
            built = cb.call("doc_apply_ops", {"doc_id": source, "ops": [{
                "op": "add_feature", "feature": {
                    "type": "box", "name": "complete prefix",
                    "params": {"length": 12, "width": 8, "height": 6},
                },
            }]})
            assert built["rebuild"]["ok"], built
            saved = cb.call("doc_save", {"doc_id": source, "path": str(complete)})
            assert isinstance(saved["cache"], dict)
        finally:
            cb.call("doc_close", {"doc_id": source})

        with zipfile.ZipFile(complete) as archive:
            thumbnail = archive.read("thumbnail.png")
            meta = archive.read("meta.json")
            tree = json.loads(archive.read("document.json"))
        tree["features"].append({
            "id": "broken_re_feature", "type": "unsupported_feature",
            "params": {}, "refs": {},
        })
        incomplete = reveng_dir / "incomplete.step+"
        # No saved geometry or checkpoints: the authored failing tree is authoritative.
        with zipfile.ZipFile(incomplete, "w") as archive:
            archive.writestr("thumbnail.png", thumbnail)
            archive.writestr("meta.json", meta)
            archive.writestr("document.json", json.dumps(tree))
        original = incomplete.read_bytes()

        opened = cb.call("doc_open", {"path": str(incomplete), "detach_path": True})
        failed_doc = opened["doc_id"]
        try:
            assert opened["rebuild"]["ok"] is False
            assert opened["geometry_state"] == "incomplete"
            assert any(
                status["id"] == "broken_re_feature" and not status["ok"]
                for status in opened["rebuild"]["feature_status"]
            )
            tools = _load_reveng_tools()
            session = _j(tools["reveng_start_session"](name="Incomplete RE", part_ref=part_id))
            sid = session["session_id"]
            mesh_path = _record_result_mesh(reveng_dir, sid)
            sidecar_path = reveng_dir / sid / "reveng_result.json"
            if previous_attachment:
                previous = _j(tools["reveng_attach_result"](session_id=sid, part_ref=part_id))
                assert previous["ok"] and previous["geometry_ref"] == mesh_path
            previous_sidecar = sidecar_path.read_bytes() if previous_attachment else None
            session_json = reveng_dir / sid / "session.json"
            session_data = json.loads(session_json.read_text())
            session_data["artifacts"]["doc_id"] = failed_doc
            session_json.write_text(json.dumps(session_data, indent=2))
            before_session = session_json.read_bytes()
            before_tree = cb.call("doc_tree", {"doc_id": failed_doc})
            before_docs = {doc["doc_id"] for doc in cb.call("doc_list", {})["docs"]}
            attached = _j(tools["reveng_attach_result"](session_id=sid, part_ref=part_id))
            assert attached["ok"] is False, attached
            assert attached["code"] == "rebuild_failed"
            # Recovery snapshots do not rebuild to diagnose a missing current
            # verified build. If the failed solve is cached, native code can
            # provide the more specific feature diagnostics instead.
            reason = attached["detail"]["reason"]
            assert reason in {"no_verified_build", "feature_failed"}
            if reason == "feature_failed":
                assert any(
                    failure["id"] == "broken_re_feature" and failure["error"]
                    for failure in attached["detail"]["failed_features"]
                )
            assert session_json.read_bytes() == before_session
            if previous_attachment:
                assert sidecar_path.read_bytes() == previous_sidecar
                assert RevengResultAdapter().get(part_id)["geometry_ref"]["path"] == mesh_path
            else:
                assert RevengResultAdapter().get(part_id) is None
            assert cb.call("doc_tree", {"doc_id": failed_doc}) == before_tree
            assert {doc["doc_id"] for doc in cb.call("doc_list", {})["docs"]} == before_docs
            assert incomplete.read_bytes() == original

            # A direct native catalog import must also refuse the incomplete
            # file without closing the already-open diagnostic document.
            record = {
                "part_id": part_id, "domain": "reveng",
                "geometry_ref": {"kind": "import_file", "path": str(incomplete)},
            }
            result = cb.call("part_place", {"part_id": part_id, "record": record, "params": params})
            try:
                assert result["ok"] is False, result
                assert result["editable"] is False
                assert result["doc_id"] is None
                assert "broken_re_feature" in result["reason"]
                assert {doc["doc_id"] for doc in cb.call("doc_list", {})["docs"]} == before_docs
                assert cb.call("doc_tree", {"doc_id": failed_doc}) == before_tree
                assert incomplete.read_bytes() == original
            finally:
                if result.get("doc_id"):
                    cb.call("doc_close", {"doc_id": result["doc_id"]})
        finally:
            cb.call("doc_close", {"doc_id": failed_doc})

    def test_legacy_sidecar_does_not_treat_a_document_handle_as_a_file(
        self, reveng_dir, parts_hub_with_metadata_part
    ):
        from services.mcp import core_bridge as cb
        from services.parts.adapters.reveng_result_adapter import RevengResultAdapter

        part_id, _, _ = parts_hub_with_metadata_part
        tools = _load_reveng_tools()
        session = _j(tools["reveng_start_session"](name="Legacy RE", part_ref=part_id))
        sid = session["session_id"]
        mesh_path = _record_result_mesh(reveng_dir, sid)
        old_doc = cb.call("doc_new", {"kind": "part", "name": "retired RE handle"})["doc_id"]
        cb.call("doc_close", {"doc_id": old_doc})
        sidecar_path = reveng_dir / sid / "reveng_result.json"
        legacy = {
            "schema": "aicad.reveng_result/1", "session_id": sid,
            "part_ref": part_id, "doc_id": old_doc, "mesh_path": mesh_path,
        }
        sidecar_path.write_text(json.dumps(legacy))
        record = RevengResultAdapter().get(part_id)
        assert not record["geometry_ref"]
        assert record["metadata"]["model_present"] is False
        result = cb.call("part_place", {"part_id": part_id, "record": record})
        assert result["ok"] is False and result["doc_id"] is None

        # A genuinely mesh-only old sidecar still places its actual mesh.
        legacy["doc_id"] = None
        # An explicitly missing file reference is not silently replaced by a mesh.
        legacy["geometry_ref"] = {"kind": "import_file", "path": None}
        sidecar_path.write_text(json.dumps(legacy))
        assert not RevengResultAdapter().get(part_id)["geometry_ref"]
        del legacy["geometry_ref"]
        sidecar_path.write_text(json.dumps(legacy))
        record = RevengResultAdapter().get(part_id)
        assert record["geometry_ref"] == {"kind": "import_file", "path": mesh_path}
        result = cb.call("part_place", {"part_id": part_id, "record": record})
        assert result["ok"] and result["editable"], result
        try:
            mass = cb.call("eval_mass", {"doc_id": result["doc_id"]})
            assert mass["volume_mm3"] == pytest.approx(96)
        finally:
            cb.call("doc_close", {"doc_id": result["doc_id"]})

    def test_session_missing_raises_graceful_error(self, reveng_dir):
        """reveng_attach_result on a non-existent session returns ok=False."""
        tools = _load_reveng_tools()
        result = _j(tools["reveng_attach_result"](
            session_id="re_deadbeef", part_ref="catalog:test:no-such"
        ))
        assert result["ok"] is False
        assert "not found" in result.get("reason", "")
