"""Tests for PLM items & releases (contract aicad.plm/1 §4/§5).

Covers:
  * items auto-register on first entry; part numbers editable (unique);
    rev letters move ONLY at release; lifecycle transitions enforced
    (no edit-in-place of Released — "start a new revision" copies forward)
  * releasing an assembly pins component revs + writes a BOM freeze that
    bom_rollup reproduces exactly later, even after components change
  * release pre-flight blocks on unsaved docs / stale refs / never-released
    components; overridable with explicit confirmation (warn, don't wall)
  * drawing title block: stamp orders carry the released rev letter through
    the existing drawing_set_titleblock path; superseded watermark helper
  * where-used answers item → consumers on a 3-level project
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from services.plm import assist, items, manifest, release, whereused
from services.plm.vault import Vault, VaultError

# ---------------------------------------------------------------------------
# fixtures — REAL doc_schema shapes (components[].source.path, drawing source)
# ---------------------------------------------------------------------------

_ID = [16.0 if i % 5 == 0 else 0.0 for i in range(16)]  # identity-ish 4x4 row-major


def _container(doc: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("thumbnail.png", b"\x89PNG\r\n_stub")
        zf.writestr("document.json", json.dumps(doc, indent=2))
    return buf.getvalue()


def _part(name: str, uuid: str, wall: float = 3.0) -> dict:
    return {"schema": "aicad.part/1", "id": uuid, "name": name,
            "features": [{"id": "f1", "name": "Base", "type": "box",
                          "params": {"wall": wall}}]}


def _assembly(name: str, uuid: str, component_paths: list[str]) -> dict:
    comps = []
    for i, p in enumerate(component_paths, start=1):
        comps.append({"id": f"k{i}", "name": p.split(".")[0],
                      "source": {"path": p}, "transform": _ID,
                      "fixed": i == 1, "suppressed": False})
    return {"schema": "aicad.asm/1", "id": uuid, "name": name,
            "components": comps, "mates": []}


def _drawing(name: str, uuid: str, source_path: str) -> dict:
    return {"schema": "aicad.drawing/1", "id": uuid, "name": name,
            "source": source_path,
            "sheets": [{"id": "s1", "size": "A3",
                        "views": [{"id": "v1", "type": "base",
                                   "orientation": "front", "scale": 1.0,
                                   "position": [100, 100], "show_hidden": True}]}]}


def _write(project_dir, name, doc):
    (project_dir / name).write_bytes(_container(doc))


def _three_level_project(tmp_path):
    """PartA ← SubAsm (2× PartA) ← Main (1× SubAsm); Sheet details PartA."""
    _write(tmp_path, "PartA.step+", _part("PartA", "uuid-a"))
    _write(tmp_path, "SubAsm.step+",
           _assembly("SubAsm", "uuid-s", ["PartA.step+", "PartA.step+"]))
    _write(tmp_path, "Main.step+", _assembly("Main", "uuid-m", ["SubAsm.step+"]))
    _write(tmp_path, "Sheet.step+", _drawing("Sheet", "uuid-d", "PartA.step+"))
    v = Vault.open(tmp_path)
    by_path = {it["path"]: it["item_id"] for it in v.manifest()["items"]}
    return v, by_path


# ---------------------------------------------------------------------------
# item registry: part numbers, kinds, rev letters
# ---------------------------------------------------------------------------

def test_items_autoregister_with_schema_kinds(tmp_path):
    v, by_path = _three_level_project(tmp_path)
    try:
        m = v.manifest()
        kinds = {it["path"]: it["kind"] for it in m["items"]}
        assert kinds["PartA.step+"] == "part"
        assert kinds["SubAsm.step+"] == "assembly"
        assert kinds["Main.step+"] == "assembly"
        assert kinds["Sheet.step+"] == "drawing"  # schema wins over .step+ suffix
        numbers = [it["part_number"] for it in m["items"]]
        assert len(set(numbers)) == len(numbers)  # unique
        assert all(n and "-" in n for n in numbers)
    finally:
        v.close()


def test_part_number_editable_and_unique(tmp_path):
    v, by_path = _three_level_project(tmp_path)
    try:
        a = by_path["PartA.step+"]
        s = by_path["SubAsm.step+"]
        out = v.set_part_number(a, "GBX-1000")
        assert out["part_number"] == "GBX-1000"
        # persisted
        assert manifest.find_item(v.manifest(), a)["part_number"] == "GBX-1000"
        # uniqueness enforced
        with pytest.raises(VaultError) as e:
            v.set_part_number(s, "GBX-1000")
        assert e.value.reason == "preflight_failed"
        # malformed rejected
        with pytest.raises(VaultError) as e2:
            v.set_part_number(s, "has spaces")
        assert e2.value.reason == "invalid_mode"
    finally:
        v.close()


def test_rev_letters_move_only_at_release(tmp_path):
    _write(tmp_path, "PartA.step+", _part("PartA", "uuid-a", 3.0))
    v = Vault.open(tmp_path)
    try:
        iid = v.status()["items"][0]["item_id"]
        # saves and checkpoints never move the rev letter
        _write(tmp_path, "PartA.step+", _part("PartA", "uuid-a", 4.0))
        v.save("auto")
        v.checkpoint("named point")
        assert manifest.find_item(v.manifest(), iid)["rev"] is None
        # release stamps A
        assert v.release(iid, "first release")["rev"] == "A"
        item = manifest.find_item(v.manifest(), iid)
        assert item["rev"] == "A" and item["lifecycle"] == manifest.LIFECYCLE_RELEASED
    finally:
        v.close()


# ---------------------------------------------------------------------------
# lifecycle enforcement: no edit-in-place of Released
# ---------------------------------------------------------------------------

def test_released_item_is_edit_locked(tmp_path):
    _write(tmp_path, "PartA.step+", _part("PartA", "uuid-a", 3.0))
    v = Vault.open(tmp_path)
    try:
        iid = v.status()["items"][0]["item_id"]
        v.release(iid, "rev A")
        _write(tmp_path, "PartA.step+", _part("PartA", "uuid-a", 9.0))
        with pytest.raises(VaultError) as e:
            v.save("auto")
        assert e.value.reason == "locked"
        assert "new revision" in e.value.hint
        # "start rev B" unlocks; the edit then saves fine
        out = v.start_new_rev(iid)
        assert out["working_rev"] == "B"
        assert out["item"]["lifecycle"] == manifest.LIFECYCLE_IN_WORK
        v.save("auto")
        # ... and the next release stamps B
        assert v.release(iid, "rev B")["rev"] == "B"
    finally:
        v.close()


def test_release_of_released_item_rejected(tmp_path):
    _write(tmp_path, "PartA.step+", _part("PartA", "uuid-a"))
    v = Vault.open(tmp_path)
    try:
        iid = v.status()["items"][0]["item_id"]
        v.release(iid, "rev A")
        with pytest.raises(VaultError) as e:
            v.release(iid, "again")
        assert e.value.reason == "invalid_mode"
        assert "new revision" in e.value.hint
    finally:
        v.close()


def test_restore_is_exempt_from_released_lock(tmp_path):
    _write(tmp_path, "PartA.step+", _part("PartA", "uuid-a", 3.0))
    v = Vault.open(tmp_path)
    try:
        iid = v.status()["items"][0]["item_id"]
        cp = v.checkpoint("before release")
        v.release(iid, "rev A")
        # you can ALWAYS go back (contract §8.3), even across a release
        res = v.restore(cp["commit"])
        assert res["entry"]["kind"] == "restore"
    finally:
        v.close()


def test_start_new_rev_idempotent_in_work(tmp_path):
    _write(tmp_path, "PartA.step+", _part("PartA", "uuid-a"))
    v = Vault.open(tmp_path)
    try:
        iid = v.status()["items"][0]["item_id"]
        out = v.start_new_rev(iid)  # already In work — harmless
        assert out["item"]["lifecycle"] == manifest.LIFECYCLE_IN_WORK
        assert out["working_rev"] == "A"  # first release will stamp A
    finally:
        v.close()


# ---------------------------------------------------------------------------
# release pre-flight (warn, don't wall)
# ---------------------------------------------------------------------------

def test_preflight_blocks_on_unsaved_changes_and_confirm_overrides(tmp_path):
    v, by_path = _three_level_project(tmp_path)
    try:
        a = by_path["PartA.step+"]
        _write(tmp_path, "PartA.step+", _part("PartA", "uuid-a", 7.0))  # dirty
        with pytest.raises(VaultError) as e:
            release.release(v, a, "rev A")
        assert e.value.reason == "preflight_failed"
        kinds = {p["kind"] for p in e.value.problems}
        assert "unsaved_changes" in kinds
        for p in e.value.problems:
            _assert_no_git_vocab(p["hint"])
        # explicit confirmation proceeds (ease-of-use: warn, don't wall)
        res = release.release(v, a, "rev A anyway", confirm=True)
        assert res["rev"] == "A"
        assert res["preflight"]["ok"] is False
    finally:
        v.close()


def test_preflight_warns_on_unreleased_components(tmp_path):
    v, by_path = _three_level_project(tmp_path)
    try:
        s = by_path["SubAsm.step+"]
        pf = release.preflight(v, s)
        kinds = [p["kind"] for p in pf["problems"]]
        assert "unreleased_component" in kinds  # PartA never released
    finally:
        v.close()


def test_preflight_verify_hook(tmp_path):
    _write(tmp_path, "PartA.step+", _part("PartA", "uuid-a"))
    v = Vault.open(tmp_path)
    try:
        iid = v.status()["items"][0]["item_id"]
        pf = release.preflight(v, iid, verify_hook=lambda item: {"ok": False,
                                                                 "hint": "wall too thin"})
        assert {"kind": "verify_failed", "hint": "wall too thin"} in pf["problems"]
        ok = release.preflight(v, iid, verify_hook=lambda item: {"ok": True})
        assert ok["ok"] is True
    finally:
        v.close()


# ---------------------------------------------------------------------------
# assembly release: ref pinning + BOM freeze round-trip
# ---------------------------------------------------------------------------

def test_assembly_release_pins_and_freezes(tmp_path):
    v, by_path = _three_level_project(tmp_path)
    try:
        a, s = by_path["PartA.step+"], by_path["SubAsm.step+"]
        release.release(v, a, "part rev A")
        res = release.release(v, s, "assembly rev A", confirm=True)  # warn: drawing unreleased is not a problem; confirm anyway
        assert res["rev"] == "A"
        fz = release.latest_freeze(v.manifest(), s)
        assert fz is not None
        assert fz["rev"] == "A"
        assert fz["commit"], "freeze commit pin must be backfilled non-null"
        assert fz["pins"] == [{"item_id": a, "rev": "A", "qty": 2,
                               "part_number": manifest.find_item(v.manifest(), a)["part_number"],
                               "path": "PartA.step+"}]
    finally:
        v.close()


def test_bom_freeze_reproduces_after_component_changes(tmp_path):
    v, by_path = _three_level_project(tmp_path)
    try:
        a, s = by_path["PartA.step+"], by_path["SubAsm.step+"]
        release.release(v, a, "part rev A")
        release.release(v, s, "assembly rev A", confirm=True)
        frozen_before = release.frozen_bom(v, s)

        # now the component moves on: new rev, edited, re-released
        v.start_new_rev(a)
        _write(tmp_path, "PartA.step+", _part("PartA", "uuid-a", 8.0))
        v.save("auto")
        release.release(v, a, "part rev B", confirm=True)
        assert manifest.find_item(v.manifest(), a)["rev"] == "B"

        frozen_after = release.frozen_bom(v, s)
        assert frozen_after["rows"] == frozen_before["rows"]  # frozen means frozen
        assert frozen_after["pins"] == frozen_before["pins"]
        # bom_rollup consumes the frozen rows and reproduces the SAME rollup
        # (no all-skip: the import must work in the gate venv)
        assert "rollup" in frozen_before
        assert frozen_after["rollup"] == frozen_before["rollup"]
        assert any("rev A" in r["item"] for r in frozen_after["rows"])
    finally:
        v.close()


def test_stale_ref_flagged_after_component_rerelease(tmp_path):
    v, by_path = _three_level_project(tmp_path)
    try:
        a, s, mn = by_path["PartA.step+"], by_path["SubAsm.step+"], by_path["Main.step+"]
        release.release(v, a, "part rev A")
        release.release(v, s, "assembly rev A", confirm=True)
        # component re-released → SubAsm's freeze now pins a superseded rev
        v.start_new_rev(a)
        _write(tmp_path, "PartA.step+", _part("PartA", "uuid-a", 8.0))
        v.save("auto")
        release.release(v, a, "part rev B", confirm=True)

        flags = assist.stale_refs(v.manifest())
        assert flags and flags[0]["pinned_rev"] == "A" and flags[0]["current_rev"] == "B"
        # ... and the pre-flight of ANY release now lists it
        pf = release.preflight(v, mn)
        assert "stale_ref" in [p["kind"] for p in pf["problems"]]
    finally:
        v.close()


# ---------------------------------------------------------------------------
# drawing title-block stamp + superseded watermark
# ---------------------------------------------------------------------------

def test_release_emits_stamp_orders_for_drawings(tmp_path):
    v, by_path = _three_level_project(tmp_path)
    try:
        a = by_path["PartA.step+"]
        res = release.release(v, a, "part rev A")
        orders = res["stamp_orders"]
        assert len(orders) == 1
        assert orders[0]["drawing_path"] == "Sheet.step+"
        assert orders[0]["fields"]["rev"] == "A"
        assert orders[0]["fields"]["part_number"]
    finally:
        v.close()


def test_apply_stamp_orders_through_engine_path(tmp_path):
    v, by_path = _three_level_project(tmp_path)
    try:
        a = by_path["PartA.step+"]
        res = release.release(v, a, "part rev A")
        calls = []

        def fake_call(method, params):
            calls.append((method, params))
            return {"ok": True}

        results = release.apply_stamp_orders(
            res["stamp_orders"],
            resolve_doc_id=lambda path: "live-doc-7" if path == "Sheet.step+" else None,
            call=fake_call)
        assert results == [{"drawing_path": "Sheet.step+", "applied": True}]
        assert calls[0][0] == "drawing_set_titleblock"
        assert calls[0][1]["doc_id"] == "live-doc-7"
        assert calls[0][1]["fields"]["rev"] == "A"

        # unresolved drawing (not open) is reported, never raises
        results2 = release.apply_stamp_orders(
            res["stamp_orders"], resolve_doc_id=lambda path: None, call=fake_call)
        assert results2[0]["applied"] is False
    finally:
        v.close()


def test_superseded_watermark(tmp_path):
    _write(tmp_path, "PartA.step+", _part("PartA", "uuid-a", 3.0))
    v = Vault.open(tmp_path)
    try:
        iid = v.status()["items"][0]["item_id"]
        v.release(iid, "rev A")
        v.start_new_rev(iid)
        _write(tmp_path, "PartA.step+", _part("PartA", "uuid-a", 5.0))
        v.save("auto")
        v.release(iid, "rev B")
        item = manifest.find_item(v.manifest(), iid)
        assert release.watermark_for(item, "A") == "SUPERSEDED"
        assert release.watermark_for(item, "B") is None
    finally:
        v.close()


# ---------------------------------------------------------------------------
# where-used on the 3-level project
# ---------------------------------------------------------------------------

def test_where_used_direct_and_transitive(tmp_path):
    v, by_path = _three_level_project(tmp_path)
    try:
        a, s, mn = by_path["PartA.step+"], by_path["SubAsm.step+"], by_path["Main.step+"]
        d = by_path["Sheet.step+"]
        m = v.manifest()
        docs = whereused.docs_at_head(v)

        direct = whereused.where_used(m, docs, a)
        got = {(r["item_id"], r["via"], r["count"]) for r in direct}
        assert (s, "component", 2) in got       # 2× PartA in SubAsm
        assert (d, "source", 1) in got          # the drawing details PartA
        assert all(r["level"] == 1 for r in direct)
        assert not any(r["item_id"] == mn for r in direct)  # not direct

        full = whereused.where_used(m, docs, a, transitive=True)
        ids = {r["item_id"] for r in full}
        assert ids == {s, d, mn}                # 3-level closure
        assert {r["item_id"]: r["level"] for r in full}[mn] == 2

        # a leaf-consumer question: who uses SubAsm?
        assert {r["item_id"] for r in whereused.where_used(m, docs, s)} == {mn}
        # unknown item → KeyError for the tool layer to map to invalid_item
        with pytest.raises(KeyError):
            whereused.where_used(m, docs, "i00000000")
    finally:
        v.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_GIT_VOCAB = {
    "git", "commit", "repository", "repo", "branch", "merge", "rebase",
    "head", "ref", "stage", "staging", "push", "pull", "sha", "hash",
}


def _assert_no_git_vocab(text: str) -> None:
    words = set(text.lower().replace(".", " ").replace(",", " ").replace(":", " ").split())
    leaked = words & _GIT_VOCAB
    assert not leaked, f"git vocabulary leaked: {leaked} in {text!r}"


def test_item_view_never_emits_null_strings():
    """Items predating part_number/rev stamping have None there; item_view must emit
    "" not null — a null string field aborts the GUI's strict json parse (live crash
    2026-07-09 opening swasi_test.step+)."""
    legacy = {"item_id": "i1", "path": "a.step+", "kind": "part",
              "lifecycle": "Released"}  # no part_number, no rev
    view = items.item_view(legacy)
    for key in ("part_number", "rev", "working_rev"):
        assert view[key] == "", f"{key} must be '' for legacy items, got {view[key]!r}"
    assert all(v is not None for v in view.values())
