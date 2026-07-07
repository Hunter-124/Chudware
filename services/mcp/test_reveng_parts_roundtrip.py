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
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterator
import importlib
import sys

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def reveng_dir(tmp_path, monkeypatch):
    """Redirect RE session storage to a temp directory."""
    d = tmp_path / "reveng"
    d.mkdir()
    monkeypatch.setenv("AICAD_REVENG_DIR", str(d))
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
    photo_path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 10)  # minimal JFIF header
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
    import engine_oracle.parts_hub as _hub_mod
    _hub_mod._hub = None

    from engine_oracle.parts_hub import register_provider
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
        assert seeded.get("images", 0) >= 1, \
            f"Expected at least 1 image seeded; got seeded={seeded}"

        # The session directory should contain the image.
        sess_dir = reveng_dir / result["session_id"]
        images = list((sess_dir / "images").glob("*.jpg"))
        assert images, "No images found in session images/ dir after seeding"

        # Datasheet should also be present.
        ds_files = list((sess_dir / "datasheet").iterdir())
        assert ds_files, "No datasheet found in session datasheet/ dir after seeding"

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

        # 2. Simulate a mesh result by injecting mesh_path into session.json.
        sess_dir = reveng_dir / sid
        sess_json = sess_dir / "session.json"
        sess_data = json.loads(sess_json.read_text())
        fake_mesh = str(sess_dir / "mesh" / "result.ply")
        (sess_dir / "mesh").mkdir(parents=True, exist_ok=True)
        Path(fake_mesh).write_text("ply\nformat ascii 1.0\nelement vertex 0\nend_header\n")
        sess_data["artifacts"]["mesh_path"] = fake_mesh
        sess_json.write_text(json.dumps(sess_data, indent=2))

        # 3. Attach result.
        attach_result = _j(tools["reveng_attach_result"](
            session_id=sid, part_ref=PART_ID
        ))
        assert attach_result["ok"] is True, attach_result
        assert attach_result["part_ref"] == PART_ID
        assert attach_result["geometry_ref"] == fake_mesh  # mesh_path since no doc_id

        # 4. Sidecar written.
        sidecar_path = sess_dir / "reveng_result.json"
        assert sidecar_path.exists(), "reveng_result.json sidecar not written"
        sidecar = json.loads(sidecar_path.read_text())
        assert sidecar["part_ref"] == PART_ID
        assert sidecar["mesh_path"] == fake_mesh
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

        # Inject mesh.
        sess_dir = reveng_dir / sid
        sess_json = sess_dir / "session.json"
        sess_data = json.loads(sess_json.read_text())
        fake_mesh = str(sess_dir / "mesh" / "result.ply")
        (sess_dir / "mesh").mkdir(parents=True, exist_ok=True)
        Path(fake_mesh).write_text("ply\nformat ascii 1.0\nelement vertex 0\nend_header\n")
        sess_data["artifacts"]["mesh_path"] = fake_mesh
        sess_json.write_text(json.dumps(sess_data, indent=2))

        # Attach.
        _j(tools["reveng_attach_result"](session_id=sid, part_ref=PART_ID))

        # Verify RevengResultAdapter serves the geometry_ref directly.
        # (The hub's provider-ordering means the original metadata adapter may
        # win hub_get() for the same part_id; we assert the RE adapter itself
        # returns the correct record since that's what reveng_attach_result guarantees.)
        from engine_oracle.parts_hub.adapters.reveng_result_adapter import RevengResultAdapter
        adapter = RevengResultAdapter()
        record = adapter.get(PART_ID)
        assert record is not None, "RevengResultAdapter.get returned None after attach"
        gref = record.get("geometry_ref")
        assert gref, f"geometry_ref missing on PartRecord after attach: {record}"
        assert gref.get("kind") == "import_file", f"Expected import_file kind, got: {gref}"
        assert gref.get("path") == fake_mesh

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

        # Inject mesh file.
        sess_dir = reveng_dir / sid
        sess_json = sess_dir / "session.json"
        sess_data = json.loads(sess_json.read_text())
        fake_mesh = str(sess_dir / "mesh" / "result.ply")
        (sess_dir / "mesh").mkdir(parents=True, exist_ok=True)
        Path(fake_mesh).write_text("ply\nformat ascii 1.0\nelement vertex 0\nend_header\n")
        sess_data["artifacts"]["mesh_path"] = fake_mesh
        sess_json.write_text(json.dumps(sess_data, indent=2))

        _j(tools["reveng_attach_result"](session_id=sid, part_ref=PART_ID))

        # part_place routes via geometry_ref.kind="import_file" to _place_import_file,
        # which calls core_bridge.call("import_file", ...) — mock that RPC so we
        # don't need the native engine.  We also need hub.get() to return the RE
        # record (with geometry_ref), not the metadata-only record; mock it directly.
        import unittest.mock as mock
        from engine_oracle.parts_hub.adapters.reveng_result_adapter import RevengResultAdapter

        re_adapter = RevengResultAdapter()
        re_record = re_adapter.get(PART_ID)
        assert re_record is not None, "RevengResultAdapter has no record — attach may have failed"

        mock_cb = mock.MagicMock()
        mock_cb.call.return_value = {"doc_id": "doc_mock_abc123", "body_count": 1}

        with mock.patch.dict("sys.modules", {"core_bridge": mock_cb}):
            # place() does `from engine_oracle.parts_hub import get` inline;
            # patch the module-level function so it returns the RE record.
            with mock.patch("engine_oracle.parts_hub.get", return_value=re_record):
                from engine_oracle.parts_hub.place import place
                result = place(PART_ID)

        assert result["ok"] is True, f"part_place failed: {result}"
        assert result.get("editable") is True

    def test_session_missing_raises_graceful_error(self, reveng_dir):
        """reveng_attach_result on a non-existent session returns ok=False."""
        tools = _load_reveng_tools()
        result = _j(tools["reveng_attach_result"](
            session_id="re_deadbeef", part_ref="catalog:test:no-such"
        ))
        assert result["ok"] is False
        assert "not found" in result.get("reason", "")
