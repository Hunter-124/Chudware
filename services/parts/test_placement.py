"""Cell catalog placement uses verified project-local references."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from services.agent import request_context
from services.mcp import core_bridge, parts_tools
from services.parts import parts_library, vendor_parts, worker_rpc, gui_rpc, placement
from services.parts.test_parts_library import _bundle


class _MCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def register(fn):
            self.tools[fn.__name__] = fn
            return fn
        return register


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    work = tmp_path / 'work'
    work.mkdir()
    private = tmp_path / 'actor' / 'vendor_parts'
    monkeypatch.setattr(vendor_parts.paths, 'VENDOR_PARTS', private, raising=False)
    monkeypatch.setattr(vendor_parts, 'INDEX', private / 'index.json')
    monkeypatch.setattr(vendor_parts, '_rebuild_parts_index', lambda: None)
    monkeypatch.setattr(request_context, 'in_cell', lambda: True)
    monkeypatch.setattr(request_context, 'cell_path', lambda raw, **kw: (work / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve())
    monkeypatch.setattr(placement, '_workspace_root', lambda: work)
    manifest, root, data = _bundle(work)
    result = worker_rpc._part_library_ingest({'manifest_path': str(manifest), 'artifact_root': str(root)}, lambda _: None)
    assert result['ok']
    part_id = data['records'][0]['part_id']
    acquired = worker_rpc._part_acquire({'part_id': part_id, 'offline': True}, lambda _: None)
    assert acquired['ok']
    source = Path(acquired['part']['geometry_ref']['path'])
    source_bytes = source.read_bytes()
    monkeypatch.setattr('services.net.safe_http.download', lambda *a, **k: pytest.fail('offline placement made a network request'))
    return work, source, source_bytes, data['records'][0]


def test_offline_worker_gui_mcp_and_assembly_use_relative_project_reference(catalog, monkeypatch):
    work, source, source_bytes, original = catalog
    calls = []

    def native(method, params):
        calls.append((method, params))
        if method == 'part_place':
            rec = params['record']
            ref = rec['geometry_ref']['path']
            assert not Path(ref).is_absolute() and '..' not in Path(ref).parts
            assert str(source) not in json.dumps(params)
            assert (work / ref).read_bytes() == source_bytes
            assert rec['part_id'] == original['part_id']
            assert rec['artifact'] == original['artifact']
            return {'ok': True, 'doc_id': 'doc1', 'detail': {'name': 'Bolt'}}
        if method == 'doc_save':
            Path(params['path']).write_text('saved')
            return {'path': params['path']}
        assert method == 'doc_close'
        return {'ok': True}

    monkeypatch.setattr(core_bridge, 'call', native)
    pid = original['part_id']
    assert worker_rpc._part_place({'part_id': pid, 'include_live': False}, lambda _: None)['ok']
    assert gui_rpc._parts_insert_into_doc({'item_id': pid, 'include_live': False}, lambda _: None)['ok']
    mcp = _MCP()
    parts_tools.register_parts_tools(mcp)
    assert json.loads(mcp.tools['part_place'](pid, include_live=False))['ok']
    assert gui_rpc._parts_prepare_for_assembly({'item_id': pid, 'include_live': False,
                                                  'project_dir': str(work)}, lambda _: None)['ok']
    refs = [p['record']['geometry_ref']['path'] for method, p in calls if method == 'part_place']
    assert len(refs) == 4 and len(set(refs)) == 1
    assert source.read_bytes() == source_bytes
    assert (work / refs[0]).stat().st_mode & 0o222 == 0


@pytest.mark.parametrize('destination', ['conflict', 'symlink', 'directory_symlink'])
def test_conflicting_and_symlink_destinations_are_never_replaced(catalog, destination):
    work, source, source_bytes, original = catalog
    digest = original['artifact']['sha256']
    parent = work / '.chudware' / 'catalog' / digest
    outside = work / 'outside'
    outside.write_bytes(b'other')
    if destination == 'directory_symlink':
        parent.parent.mkdir(parents=True)
        parent.symlink_to(work, target_is_directory=True)
    else:
        parent.mkdir(parents=True)
        target = parent / 'model.step'
        if destination == 'symlink':
            target.symlink_to(outside)
        else:
            target.write_bytes(b'other')
            target.chmod(0o444)
    result = worker_rpc._part_place({'part_id': original['part_id'], 'include_live': False}, lambda _: None)
    assert not result['ok']
    assert str(source) not in result['reason']
    assert outside.read_bytes() == b'other'
    assert source.read_bytes() == source_bytes
    if destination == 'conflict':
        assert target.read_bytes() == b'other'
    else:
        assert (parent if destination == 'directory_symlink' else target).is_symlink()


def test_tampered_cache_cannot_be_placed(catalog):
    work, source, source_bytes, original = catalog
    source.write_bytes(b'tampered')
    result = worker_rpc._part_place({'part_id': original['part_id'], 'include_live': False}, lambda _: None)
    assert not result['ok']
    assert not (work / '.chudware' / 'catalog' / original['artifact']['sha256'] / 'model.step').exists()


def test_parametric_recipe_is_unchanged(catalog):
    recipe = {'part_id': 'motor:example', 'geometry_ref': {
        'kind': 'parametric_stepper_motor', 'spec': {'size': 42}}}
    assert placement.placement_record(recipe) is recipe


def test_mcp_catalog_edit_autosaves_as_instance_without_mutating_cache(tmp_path, monkeypatch):
    """An authored catalog instance must not grant autosave access to its digest object."""
    if not core_bridge.available():
        pytest.skip("native core unavailable")
    from services.agent.agent_turn import _bind_tool

    private = tmp_path / "vendor_parts"
    monkeypatch.setattr(vendor_parts.paths, "VENDOR_PARTS", private, raising=False)
    monkeypatch.setattr(vendor_parts, "INDEX", private / "index.json")
    monkeypatch.setattr(vendor_parts, "_rebuild_parts_index", lambda: None)
    monkeypatch.setattr(request_context, "in_cell", lambda: False)
    monkeypatch.setenv("CHUDWARE_PROJECT_SAVE_DIR", str(tmp_path / "project"))
    monkeypatch.setenv("CHUDWARE_AUTOSAVE_DIR", str(tmp_path / "autosave"))
    manifest, root, data = _bundle(tmp_path, artifact=False)
    seed = core_bridge.call("doc_new", {"kind": "part", "name": "Pinned source"})["doc_id"]
    instance = reopened = None
    try:
        built = core_bridge.call("doc_apply_ops", {"doc_id": seed, "ops": [
            {"op": "add_feature", "feature": {"type": "box", "name": "Editable",
             "params": {"length": 20, "width": 10, "height": 5, "centered": False}}}
        ]})
        feature = built["id_map"]["$0"]
        source = root / "fixture.step+"
        core_bridge.call("doc_save", {"doc_id": seed, "path": str(source)})
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        record = data["records"][0]
        record["artifact"] = {"path": source.name, "format": "step+", "size_bytes": len(content),
                              "sha256": digest, "rights": record["rights"], "notice": "Fixture"}
        manifest.write_text(json.dumps(data), encoding="utf-8")
        parts_library.ingest_manifest(str(manifest), artifact_root=str(root))
        acquired = parts_library.acquire_part(record["part_id"], offline=True)
        cached = Path(acquired["geometry_ref"]["path"])
        assert cached.read_bytes() == content
        monkeypatch.setattr("services.parts.get",
                            lambda part_id, **kw: parts_library.acquire_part(part_id, offline=True))
        mcp = _MCP()
        parts_tools.register_parts_tools(mcp)
        placed = json.loads(mcp.tools["part_place"](record["part_id"], include_live=False))
        assert placed["ok"]
        instance = placed["doc_id"]
        edited = _bind_tool("add_features")(doc_id=instance, ops=[
            {"op": "edit_feature", "id": feature, "params": {"length": 25}}
        ])
        edited = json.loads(edited) if isinstance(edited, str) else edited
        assert edited["ok"]
        assert cached.read_bytes() == content
        saved = tmp_path / "saved-instance.step+"
        result = _bind_tool("save_part")(doc_id=instance, path=str(saved))
        result = json.loads(result) if isinstance(result, str) else result
        assert result["ok"]
        reopened = core_bridge.call("doc_open", {"path": str(saved)})["doc_id"]
        tree = core_bridge.call("doc_tree", {"doc_id": reopened})["tree"]
        assert tree["features"][0]["params"]["length"] == 25
        meta = core_bridge.call("doc_get_meta", {"doc_id": reopened, "key": "parts_catalog"})["value"]
        assert meta["part_record"]["artifact"]["sha256"] == digest
        assert source.read_bytes() == cached.read_bytes() == content
        assert parts_library.acquire_part(record["part_id"], offline=True)["geometry_ref"]["path"] == str(cached)
    finally:
        for doc_id in (seed, instance, reopened):
            if doc_id:
                core_bridge.call("doc_close", {"doc_id": doc_id})
