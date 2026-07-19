"""Dirty-scan stamp cache for plm overview/status (warm vault speed).

Covers:
  * second overview reuses the process-wide stamp cache (no re-explode)
  * external content edit (new mtime) still surfaces as dirty
  * vault mutation RPCs invalidate the cache so HEAD baselines stay correct
"""
from __future__ import annotations

import io
import json
import zipfile

from services.plm import vault as vault_mod
from services.plm.vault import Vault


def _container(doc: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("thumbnail.png", b"\x89PNG\r\n_stub_thumbnail")
        zf.writestr("document.json", json.dumps(doc, indent=2))
    return buf.getvalue()


def _part(wall: float = 3.0) -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "uuid-part-1",
        "name": "Bracket",
        "features": [{"id": "f1", "params": {"wall": wall}}],
    }


def _write(project_dir, name: str, doc: dict) -> None:
    (project_dir / name).write_bytes(_container(doc))


def test_dirty_scan_cache_warm_hit_and_mtime_miss(tmp_path):
    _write(tmp_path, "Bracket.step+", _part(3.0))
    vault_mod._invalidate_dirty_cache(tmp_path)

    v1 = Vault.open(tmp_path)
    try:
        # seed leaves working == HEAD → clean
        dirty0 = v1._dirty_items(v1.manifest())
        assert dirty0 == []
        root_key = vault_mod._vault_cache_key(tmp_path)
        cached = vault_mod._dirty_scan_cache.get(root_key)
        assert cached is not None
        assert cached["items"], "full scan should stamp every live item"
        stamped = dict(cached["items"])
    finally:
        v1.close()

    # Fresh Vault (mirrors worker_rpc _with_vault) must reuse process cache.
    v2 = Vault.open(tmp_path)
    try:
        dirty1 = v2._dirty_items(v2.manifest())
        assert dirty1 == []
        cached2 = vault_mod._dirty_scan_cache[root_key]
        # same stamp entries → warm hit path (no full re-compare needed)
        assert cached2["items"] == stamped

        # External edit changes mtime/size → must recompute and report dirty.
        _write(tmp_path, "Bracket.step+", _part(9.0))
        dirty2 = v2._dirty_items(v2.manifest())
        assert len(dirty2) == 1
        iid = dirty2[0]
        assert cached2["items"][iid]["dirty"] is True
        assert cached2["items"][iid]["stamp"] != stamped[iid]["stamp"]
    finally:
        v2.close()


def test_dirty_scan_cache_invalidated_on_save(tmp_path):
    _write(tmp_path, "Bracket.step+", _part(3.0))
    vault_mod._invalidate_dirty_cache(tmp_path)

    v = Vault.open(tmp_path)
    try:
        # Make working dirty, warm the cache with that verdict.
        _write(tmp_path, "Bracket.step+", _part(5.0))
        dirty = v._dirty_items(v.manifest())
        assert dirty
        root_key = vault_mod._vault_cache_key(tmp_path)
        assert root_key in vault_mod._dirty_scan_cache

        # save() must drop the process cache so the new HEAD is the baseline.
        v.save()
        assert root_key not in vault_mod._dirty_scan_cache

        clean = v._dirty_items(v.manifest())
        assert clean == []
        # Cache rebuilt against post-save HEAD.
        assert vault_mod._dirty_scan_cache[root_key]["head"] == v._bind.head_commit()
        for ent in vault_mod._dirty_scan_cache[root_key]["items"].values():
            assert ent["dirty"] is False
    finally:
        v.close()


def test_dirty_scan_cache_invalidated_on_add_untrack(tmp_path):
    _write(tmp_path, "Bracket.step+", _part(3.0))
    (tmp_path / "spec.md").write_text("v1\n")
    vault_mod._invalidate_dirty_cache(tmp_path)

    v = Vault.open(tmp_path)
    try:
        v._dirty_items(v.manifest())  # warm
        root_key = vault_mod._vault_cache_key(tmp_path)
        assert root_key in vault_mod._dirty_scan_cache

        item = v.add_item("spec.md")
        assert root_key not in vault_mod._dirty_scan_cache

        v._dirty_items(v.manifest())  # warm again
        assert root_key in vault_mod._dirty_scan_cache
        v.untrack_item(item["item_id"])
        assert root_key not in vault_mod._dirty_scan_cache
    finally:
        v.close()
