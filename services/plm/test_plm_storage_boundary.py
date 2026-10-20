"""The vault storage boundary: the RECIPE is tracked, SOLVED STATE is not.

This file exists to pin an invariant, not to describe today's code: a future
producer must not be able to start committing build artifacts into every
revision of history without turning this test red.

Tracked (recipe + small metadata): ``document.json``, per-item metadata, the
timeline thumbnail, the change summary, verbatim-preserved unknown container
entries, the manifest — plus the frozen ``geometry.step``, and ONLY in release
commits (contract §2/§5).

Never tracked (solved state, regenerable from the recipe): the engine's
``cache.*`` display/build caches, cached simulation results
(``studies/<id>/results.*``, contracts/stepplus.md §1), and the live container's
own ``geometry.step`` — doc_save embeds an AP214 snapshot on every successful
rebuild, and it is derived + recomputable, so history carries one only where
``Vault.release`` freezes its own bytes (contract §5.4).
"""
from __future__ import annotations

import io
import json
import re
import struct
import zipfile

import pytest

from services.plm import snapshot
from services.plm.vault import Vault

# Every shadow-tree path form a NON-release commit is allowed to contain.
_WHITELIST = (
    re.compile(r"^manifest\.json$"),
    re.compile(r"^planning/[^/]+\.json$"),
    re.compile(r"^ai_vc/.+$"),
    re.compile(r"^items/[^/]+/document\.json$"),
    re.compile(r"^items/[^/]+/meta\.json$"),
    re.compile(r"^items/[^/]+/thumbnail\.png$"),
    re.compile(r"^items/[^/]+/summary\.txt$"),
    re.compile(r"^items/[^/]+/extra/.+$"),
)

_STUDY_DEF = json.dumps({"schema": "aicad.study/1", "id": "s1", "type": "static",
                         "material": "steel", "mesh_density": 0.6,
                         "fixtures": [{"face": "f1"}],
                         "loads": [{"face": "f2", "N": 500}]}).encode("utf-8")


def _part(wall: float = 3.0) -> dict:
    return {"schema": "aicad.part/1", "id": "uuid-part-1", "name": "Bracket",
            "features": [{"id": "f1", "params": {"wall": wall}}],
            "studies": [{"id": "s1", "type": "static"}]}


def _solved_extras() -> dict[str, bytes]:
    """Container entries a live .step+ carries that are SOLVED STATE."""
    return {
        "cache.json": b'{"engine":"x","doc_hash":"y"}',
        "cache.aicm": b"AICM1" + b"\x00" * 4096,
        "cache.brep": b"BREP" + b"\x00" * 2048,
        # the AP214 snapshot doc_save embeds on every successful rebuild: derived,
        # recomputable, and release-only in history (contracts/plm.md §5.4)
        "geometry.step": b"ISO-10303-21;\n" + b"L" * 4096,
        "studies/s1/results.npz": b"AICRES1" + b"\x00" * 8192,
        "studies/s1/results.external.json": b'{"schema":"aicad.sim_result_ref/1"}',
        "studies/s1.json": _STUDY_DEF,
        "studies/s1/overlay.aicm": b"AICM1overlay",
        "netlist.json": b'{"nets":[]}',
    }


def _container(doc: dict, extras: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("thumbnail.png", b"\x89PNG\r\n_stub_thumbnail")
        zf.writestr("document.json", json.dumps(doc, indent=2))
        for name, data in extras.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _write(project_dir, name, doc, extras):
    (project_dir / name).write_bytes(_container(doc, extras))


def _tree_at(project_dir, commit: str) -> dict[str, bytes]:
    v = Vault.open(str(project_dir))
    try:
        return v._bind.read_tree(commit)  # noqa: SLF001 — same package
    finally:
        v.close()


# ---------------------------------------------------------------------------
# the explode-level predicate
# ---------------------------------------------------------------------------

def test_explode_strips_caches_and_solved_study_results():
    raw = _container(_part(), _solved_extras())
    ex = snapshot.explode_container(raw)
    names = set(ex.extras)

    assert not [n for n in names if n.startswith("cache.")]
    assert "geometry.step" not in names
    assert "studies/s1/results.npz" not in names
    assert "studies/s1/results.external.json" not in names

    # the study DEFINITION is recipe — it must survive
    assert ex.extras["studies/s1.json"] == _STUDY_DEF
    assert "studies/s1/overlay.aicm" in names
    assert "netlist.json" in names


def test_is_derived_entry_predicate():
    derived = ["cache.json", "cache.aicm", "cache.brep", "geometry.step",
               "studies/s1/results.npz", "studies/abc/results.external.json"]
    kept = ["document.json", "thumbnail.png", "meta.json", "index.json",
            "netlist.json", "studies/s1.json", "studies/s1/overlay.aicm",
            "studies/s1/summary.json", "ai/memory.json",
            "drawings/d1.json", "caches.json"]
    for n in derived:
        assert snapshot.is_derived_entry(n) is True, n
    for n in kept:
        assert snapshot.is_derived_entry(n) is False, n


def test_reassemble_round_trips_the_recipe_without_solved_state():
    ex = snapshot.explode_container(_container(_part(), _solved_extras()))
    rebuilt = snapshot.reassemble_container(ex)
    with zipfile.ZipFile(io.BytesIO(rebuilt), "r") as zf:
        names = set(zf.namelist())
        doc = json.loads(zf.read("document.json").decode("utf-8"))
        assert zf.read("studies/s1.json") == _STUDY_DEF
    # recipe faithful, solved state simply absent -> "re-run the study"
    assert doc["features"] == _part()["features"]
    assert doc["studies"] == _part()["studies"]
    assert not [n for n in names if n.startswith("cache.")]
    assert "geometry.step" not in names  # the engine re-embeds it on the next save
    assert not [n for n in names
                if n.startswith("studies/") and n.rsplit("/", 1)[-1].startswith("results.")]


def _declared_size(raw: bytes, name: str, size: int) -> bytes:
    """Patch one ZIP directory record; payload stays a tiny bounded fixture."""
    data = bytearray(raw)
    pos = 0
    while (pos := data.find(b"PK\x01\x02", pos)) >= 0:
        name_size = struct.unpack_from("<H", data, pos + 28)[0]
        if data[pos + 46:pos + 46 + name_size].decode("utf-8") == name:
            struct.pack_into("<I", data, pos + 24, size)
            return bytes(data)
        pos += 46 + name_size
    raise AssertionError(f"ZIP member not found: {name}")


def test_zip_unknown_attachment_member_budget_rejects_before_read(tmp_path):
    raw = _container(_part(), {"supplier.bin": b"x" * 4096})
    oversized = _declared_size(raw, "supplier.bin", 64 * 1024 * 1024 + 1)
    path = tmp_path / "untrusted.ecad+"
    path.write_bytes(oversized)
    with pytest.raises(ValueError, match="member byte limit"):
        snapshot.explode_container(path)
    assert path.read_bytes() == oversized


def test_zip_compressed_unknown_attachment_enforces_configured_byte_budget(monkeypatch):
    # A genuine highly compressible member crosses a deliberately small test
    # budget; the production ceiling is separately pinned by directory metadata.
    raw = _container(_part(), {"supplier.bin": b"\0" * (1024 * 1024)})
    assert len(raw) < 16384
    monkeypatch.setattr(snapshot, "_MAX_MEMBER_BYTES", 64 * 1024)
    with pytest.raises(ValueError, match="member byte limit"):
        snapshot.explode_container(raw)


def test_zip_aggregate_budget_counts_all_preserved_unknowns(monkeypatch):
    raw = _container(_part(), {"first.bin": b"\0" * 4096, "second.bin": b"\0" * 4096})
    monkeypatch.setattr(snapshot, "_MAX_MEMBER_BYTES", 8192)
    monkeypatch.setattr(snapshot, "_MAX_INFLATED_BYTES", 8192)
    with pytest.raises(ValueError, match="aggregate byte limit"):
        snapshot.explode_container(raw)


def test_zip_entry_count_budget_includes_skipped_derived_members():
    raw = _container(_part(), {f"cache.{i}": b"" for i in range(4095)})
    with pytest.raises(ValueError, match="entry count limit"):
        snapshot.explode_container(raw)


def test_zip_dishonest_declared_attachment_size_fails_cleanly():
    raw = _container(_part(), {"supplier.bin": b"x" * 4096})
    malformed = _declared_size(raw, "supplier.bin", 1)
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        snapshot.explode_container(malformed)


def test_zip_attachment_roundtrip_leaves_live_warm_cache_bytes_untouched(tmp_path):
    attachments = {"vendor/notes.bin": b"vendor\0payload", "ai/checkpoint.json": b'{"revision":3}'}
    extras = {**_solved_extras(), **attachments}
    raw = _container(_part(), extras)
    # Warm solved state is neither inflated nor subject to attachment byte
    # budgets. A large declared cache size must not cold-rewrite the live file.
    raw = _declared_size(raw, "cache.brep", 512 * 1024 * 1024)
    path = tmp_path / "warm.step+"
    path.write_bytes(raw)
    exploded = snapshot.explode_container(path)
    for name, payload in attachments.items():
        assert exploded.extras[name] == payload
    restored = snapshot.explode_container(snapshot.reassemble_container(exploded))
    assert restored == exploded
    assert path.read_bytes() == raw
    with zipfile.ZipFile(path) as zf:
        assert zf.getinfo("cache.brep").file_size == 512 * 1024 * 1024
        assert zf.read("cache.json") == extras["cache.json"]

# ---------------------------------------------------------------------------
# the committed shadow tree — the actual boundary
# ---------------------------------------------------------------------------

def test_non_release_commit_contains_only_recipe_paths(tmp_path):
    _write(tmp_path, "bracket.step+", _part(3.0), _solved_extras())
    v = Vault.open(str(tmp_path))
    try:
        commit = v.save("auto")["commit"]
    finally:
        v.close()
    tree = _tree_at(tmp_path, commit)

    assert tree, "the commit must not be empty"
    unexpected = [p for p in sorted(tree)
                  if not any(rx.match(p) for rx in _WHITELIST)]
    assert not unexpected, f"non-recipe paths committed: {unexpected}"

    # and specifically, none of the three solved-state families
    assert not [p for p in tree if p.endswith("geometry.step")]
    assert not [p for p in tree if "/cache." in p or p.startswith("cache.")]
    assert not [p for p in tree
                if re.search(r"/studies/[^/]+/results\.", p)], sorted(tree)


def test_release_commit_is_the_only_place_geometry_step_appears(tmp_path):
    _write(tmp_path, "bracket.step+", _part(3.0), _solved_extras())
    v = Vault.open(str(tmp_path))
    try:
        v.save("auto")
        iid = v.manifest()["items"][0]["item_id"]
        rel = v.release(iid, "first production release",
                        geometry_step=b"ISO-10303-21;\n")
    finally:
        v.close()
    tree = _tree_at(tmp_path, rel["commit"])
    assert tree[snapshot.item_prefix(iid) + "geometry.step"] == b"ISO-10303-21;\n"
    # even a release never carries solved state
    assert not [p for p in tree if "cache." in p]
    assert not [p for p in tree if re.search(r"/studies/[^/]+/results\.", p)]
