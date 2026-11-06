"""Tests for services.plm.ecad_diff (ECAD semantic diff) and the two dispatch
points that route ECAD documents to it instead of the part/asm doc_diff shape:

  * ecad_diff() on in-memory canonical PCB docs — add/remove/change coverage
    for nets and components,
    and untouched sections (footprints/tracks) staying empty
  * doc_diff_tools.doc_diff() dispatch: ECAD schema -> ecad_diff, part/asm
    schema -> unchanged feature-tree diff (byte-identical old behavior)
  * compare.semantic_compare() dispatch + summary (no git vocabulary)
  * compare.generic_compare()'s new office_extract text-diff path: capped
    unified diff via a monkeypatched extractor (no real office files), graceful
    fallback when extraction is unavailable/fails, and untouched behavior for
    plain-text and non-office binary items

Snapshot integration tests exercise the native KiCad importer when available.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.mcp import core_bridge, doc_diff_tools, office_tools
from services.plm import compare as compare_mod
from services.plm import ecad_diff
from services.plm import doctypes, snapshot, worker_rpc
from services.plm.vault import Vault, VaultError

_GIT_VOCAB = {
    "git", "commit", "repository", "repo", "branch", "merge", "rebase",
    "head", "ref", "stage", "staging", "push", "pull", "sha", "hash",
}


def _no_git_vocab(text: str) -> None:
    words = set(text.lower().replace(".", " ").replace(",", " ").replace(";", " ").split())
    leaked = words & _GIT_VOCAB
    assert not leaked, f"git vocabulary leaked: {leaked} in {text!r}"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _pcb_a() -> dict:
    return {
        "schema": "aicad.pcb/1",
        "id": "uuid-pcb-1",
        "name": "Controller",
        "nets": [
            {"id": "n1", "name": "GND"},
            {"id": "n2", "name": "VCC"},
        ],
        "components": [
            {"id": "c1", "ref": "U1", "value": "ATmega328"},
            {"id": "c2", "ref": "R1", "value": "10k"},
        ],
        "footprints": [{"id": "fp1", "ref": "U1"}],
        "tracks": [{"id": "t1", "net": "GND", "layer": "F.Cu", "width": 0.25}],
    }


def _pcb_b() -> dict:
    return {
        "schema": "aicad.pcb/1",
        "id": "uuid-pcb-1",
        "name": "Controller",
        "nets": [
            {"id": "n1", "name": "GND_ANALOG"},   # changed
            {"id": "n3", "name": "3V3"},            # added
            # n2 removed
        ],
        "components": [
            {"id": "c1", "ref": "U1", "value": "ATmega32U4"},  # changed
            {"id": "c3", "ref": "C1", "value": "100nF"},        # added
            # c2 removed
        ],
        "footprints": [{"id": "fp1", "ref": "U1"}],   # unchanged
        "tracks": [{"id": "t1", "net": "GND", "layer": "F.Cu", "width": 0.25}],  # unchanged
    }


def _part() -> dict:
    return {
        "schema": "aicad.part/1",
        "id": "uuid-part-1",
        "name": "Bracket",
        "features": [{"id": "f1", "op": "box", "params": {"wall": 3.0}}],
    }


# ---------------------------------------------------------------------------
# is_ecad_schema
# ---------------------------------------------------------------------------

def test_is_ecad_schema_true_for_pcb_sch_ecad():
    for head in ("aicad.pcb", "aicad.sch", "aicad.ecad"):
        assert ecad_diff.is_ecad_schema({"schema": f"{head}/1"})


def test_is_ecad_schema_false_for_part_asm_and_missing():
    assert not ecad_diff.is_ecad_schema({"schema": "aicad.part/1"})
    assert not ecad_diff.is_ecad_schema({"schema": "aicad.asm/1"})
    assert not ecad_diff.is_ecad_schema({})
    assert not ecad_diff.is_ecad_schema(None)


# ---------------------------------------------------------------------------
# ecad_diff() core
# ---------------------------------------------------------------------------

def test_nets_added_removed_changed():
    diff = ecad_diff.ecad_diff(_pcb_a(), _pcb_b())

    added_ids = {r["id"] for r in diff["nets_added"]}
    removed_ids = {r["id"] for r in diff["nets_removed"]}
    changed_ids = {r["id"] for r in diff["nets_changed"]}
    assert added_ids == {"n3"}
    assert removed_ids == {"n2"}
    assert changed_ids == {"n1"}

    added = diff["nets_added"][0]
    assert added["id"] == "n3" and "3V3" in added["summary"] and "added" in added["summary"]

    removed = diff["nets_removed"][0]
    assert removed["id"] == "n2" and "VCC" in removed["summary"] and "removed" in removed["summary"]

    changed = diff["nets_changed"][0]
    assert changed["id"] == "n1" and "changed" in changed["summary"]
    assert changed["changes"] == [{"field": "name", "old": "GND", "new": "GND_ANALOG"}]


def test_components_added_removed_changed():
    diff = ecad_diff.ecad_diff(_pcb_a(), _pcb_b())

    added_ids = {r["id"] for r in diff["components_added"]}
    removed_ids = {r["id"] for r in diff["components_removed"]}
    changed_ids = {r["id"] for r in diff["components_changed"]}
    assert added_ids == {"c3"}
    assert removed_ids == {"c2"}
    assert changed_ids == {"c1"}

    changed = diff["components_changed"][0]
    assert changed["id"] == "c1"
    assert {"field": "value", "old": "ATmega328", "new": "ATmega32U4"} in changed["changes"]
    assert "U1" in changed["summary"]  # label uses ref, not raw id


def test_untouched_sections_stay_empty():
    diff = ecad_diff.ecad_diff(_pcb_a(), _pcb_b())
    for kind in ("added", "removed", "changed"):
        assert diff[f"footprints_{kind}"] == []
        assert diff[f"tracks_{kind}"] == []


def test_identical_docs_produce_empty_diff():
    diff = ecad_diff.ecad_diff(_pcb_a(), _pcb_a())
    for section in ("nets", "components", "footprints", "tracks"):
        for kind in ("added", "removed", "changed"):
            assert diff[f"{section}_{kind}"] == []




def test_missing_section_is_schema_agnostic():
    """A schematic-style doc with no footprints/tracks key still diffs cleanly
    (empty lists, not a KeyError) — sections are looked up defensively."""
    sch_a = {"schema": "aicad.sch/1", "nets": [{"id": "n1", "name": "GND"}]}
    sch_b = {"schema": "aicad.sch/1", "nets": [{"id": "n1", "name": "GND2"}]}
    diff = ecad_diff.ecad_diff(sch_a, sch_b)
    assert {r["id"] for r in diff["nets_changed"]} == {"n1"}
    assert diff["footprints_added"] == diff["footprints_removed"] == diff["footprints_changed"] == []


def test_summarize_ecad_diff_no_changes():
    assert ecad_diff.summarize_ecad_diff(ecad_diff.ecad_diff(_pcb_a(), _pcb_a())) == "No design changes."


def test_summarize_ecad_diff_no_git_vocab_and_nonempty():
    diff = ecad_diff.ecad_diff(_pcb_a(), _pcb_b())
    summary = ecad_diff.summarize_ecad_diff(diff)
    assert summary != "No design changes."
    _no_git_vocab(summary)


# ---------------------------------------------------------------------------
# doc_diff_tools.doc_diff dispatch
# ---------------------------------------------------------------------------

def test_doc_diff_dispatches_ecad_to_ecad_diff():
    result = doc_diff_tools.doc_diff(_pcb_a(), _pcb_b())
    # ecad_diff shape, not the part/asm feature-tree shape
    assert "nets_added" in result
    assert "features_added" not in result
    assert {r["id"] for r in result["nets_added"]} == {"n3"}


def test_doc_diff_part_behavior_unchanged():
    a = _part()
    b = _part()
    b["features"][0]["params"]["wall"] = 5.0
    result = doc_diff_tools.doc_diff(a, b)
    assert result["features_changed"] == ["f1"]
    assert result["params_changed"] == [
        {"feature": "f1", "param": "wall", "old": 3.0, "new": 5.0}]
    assert "nets_added" not in result


# ---------------------------------------------------------------------------
# compare.semantic_compare dispatch
# ---------------------------------------------------------------------------

def test_semantic_compare_ecad_nonempty_semantic_delta():
    out = compare_mod.semantic_compare(_pcb_a(), _pcb_b())
    diff = out["doc_diff"]
    assert diff["nets_added"] or diff["nets_removed"] or diff["nets_changed"]
    assert diff["components_added"] or diff["components_removed"] or diff["components_changed"]
    assert out["summary"] != "No design changes."
    _no_git_vocab(out["summary"])


def test_semantic_compare_part_asm_still_uses_doc_diff():
    a = _part()
    b = _part()
    b["features"][0]["params"]["wall"] = 5.0
    out = compare_mod.semantic_compare(a, b)
    assert out["doc_diff"]["features_changed"] == ["f1"]
    assert "5" in out["summary"]
    _no_git_vocab(out["summary"])



def test_kicad_identities_survive_import_reordering():
    a = {"schema": "aicad.pcb/1",
         "nets": [{"name": "GND"}, {"name": "VCC"}],
         "tracks": [{"id": "t1", "uuid": "track-a", "p1": [0, 0], "p2": [1, 0],
                     "kicad_original": {"id": "t1"}},
                    {"id": "t2", "uuid": "track-b", "p1": [1, 0], "p2": [2, 0]}]}
    b = {"schema": "aicad.pcb/1",
         "nets": [{"name": "GND"}, {"name": "SIGNAL"}],
         "tracks": [{"id": "t1", "uuid": "track-b", "p1": [1, 0], "p2": [2, 0]},
                    {"id": "t2", "uuid": "track-a", "p1": [0, 0], "p2": [1, 0],
                     "kicad_original": {"id": "t2"}}]}
    diff = ecad_diff.ecad_diff(a, b)
    assert not diff["tracks_added"] and not diff["tracks_removed"] and not diff["tracks_changed"]
    assert [entry["id"] for entry in diff["nets_added"]] == ["SIGNAL"]
    assert [entry["id"] for entry in diff["nets_removed"]] == ["VCC"]


@pytest.mark.parametrize(("path", "expected"), [
    ("board.kicad_pcb", True), ("BOARD.KICAD_PCB", True),
    ("sheet.KICAD_SCH", True), ("sheet.kicad_sch.backup", False),
    ("board.kicad_pro", False),
])
def test_kicad_suffix_detection(path, expected):
    assert doctypes.is_kicad_file(path) is expected
    assert (doctypes.detect_doc_type(path) == "kicad") is expected


def test_kicad_schematic_entities_are_semantically_compared():
    a = {"schema": "aicad.sch/1",
         "symbols": [{"id": "s1", "uuid": "symbol", "ref": "R1", "value": "10k"}],
         "wires": [{"id": "w1", "uuid": "wire", "pts": [[0, 0], [1, 0]]}]}
    b = {"schema": "aicad.sch/1",
         "symbols": [{"id": "s2", "uuid": "symbol", "ref": "R1", "value": "20k"}],
         "wires": [{"id": "w2", "uuid": "wire", "pts": [[0, 0], [2, 0]]}]}
    diff = compare_mod.semantic_compare(a, b)
    assert [item["id"] for item in diff["doc_diff"]["symbols_changed"]] == ["symbol"]
    assert [item["id"] for item in diff["doc_diff"]["wires_changed"]] == ["wire"]
    assert "R1" in diff["summary"]


def test_kicad_outline_and_via_changes_are_not_lost():
    a = {"schema": "aicad.pcb/1",
         "outline": {"shapes": [{"uuid": "edge", "type": "line",
                                 "p1": [0, 0], "p2": [10, 0]}]},
         "vias": [{"id": "v1", "uuid": "via", "at": [1, 1], "diameter": 0.8}]}
    b = {"schema": "aicad.pcb/1",
         "outline": {"shapes": [{"uuid": "edge", "type": "line",
                                 "p1": [0, 0], "p2": [20, 0]}]},
         "vias": [{"id": "v1", "uuid": "via", "at": [2, 1], "diameter": 0.8}]}
    diff = ecad_diff.ecad_diff(a, b)
    assert [item["id"] for item in diff["outline_changed"]] == ["edge"]
    assert [item["id"] for item in diff["vias_changed"]] == ["via"]


def test_outline_without_uuid_still_reports_changes():
    a = {"schema": "aicad.pcb/1",
         "outline": {"shapes": [{"type": "line", "p1": [0, 0], "p2": [10, 0]}]}}
    b = {"schema": "aicad.pcb/1",
         "outline": {"shapes": [{"type": "line", "p1": [0, 0], "p2": [20, 0]}]}}
    assert [item["id"] for item in ecad_diff.ecad_diff(a, b)["outline_changed"]] == ["outline-1"]


def _kicad_board(end: int) -> bytes:
    return f'''(kicad_pcb (version 20221018) (generator chudware-test)
      (general (thickness 1.6))
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
      (net 0 "") (net 1 "GND")
      (segment (start 10 10) (end {end} 10) (width 0.25) (layer "F.Cu") (net 1)
        (uuid "10000000-0000-4000-8000-000000000001"))
      (gr_line (start 0 0) (end 40 0) (layer "Edge.Cuts") (width 0.05)
        (uuid "10000000-0000-4000-8000-000000000002"))
    )'''.encode()


def _kicad_schematic(end: int) -> bytes:
    return f'''(kicad_sch (version 20221018) (generator chudware-test)
      (uuid "20000000-0000-4000-8000-000000000001")
      (lib_symbols (symbol "Device:R"
        (symbol "R_0_1" (rectangle (start -1 -1) (end 1 1)
          (stroke (width 0)) (fill (type none))))))
      (symbol (lib_id "Device:R") (at 20 10 0) (unit 1)
        (property "Reference" "R1") (property "Value" "10k")
        (uuid "20000000-0000-4000-8000-000000000004"))
      (wire (pts (xy 10 10) (xy {end} 10))
        (uuid "20000000-0000-4000-8000-000000000002"))
      (label "GND" (at 10 10 0)
        (uuid "20000000-0000-4000-8000-000000000003"))
    )'''.encode()


@pytest.mark.parametrize(("name", "content", "section"), [
    ("controller.KICAD_PCB", _kicad_board, "tracks"),
    ("controller.KICAD_SCH", _kicad_schematic, "wires"),
])
def test_kicad_native_snapshots_compare_and_restore(tmp_path, name, content, section):
    if not core_bridge.available():
        pytest.skip("native core is required to import KiCad snapshots")
    try:
        from PIL import Image  # noqa: F401
        have_pillow = True
    except ImportError:
        have_pillow = False
    working = tmp_path / name
    before, after = content(20), content(25)
    working.write_bytes(before)
    assert doctypes.detect_doc_type(str(working)) == "kicad"
    vault = Vault.open(tmp_path)
    try:
        item = next(it for it in vault.manifest()["items"] if it["path"] == name)
        iid = item["item_id"]
        assert item["kind"] == "ecad" and not compare_mod.is_generic_item(vault, iid)
        a = vault.checkpoint("before")["commit"]
        working.write_bytes(after)
        b = vault.checkpoint("after")["commit"]
        prefix = snapshot.item_prefix(iid) + "file/" + name
        assert vault._bind.read_tree(a)[prefix] == before
        assert vault._bind.read_tree(b)[prefix] == after
        doc_a, doc_b = compare_mod.docs_at(vault, a, b, iid)
        semantic = compare_mod.semantic_compare(doc_a, doc_b)
        assert len(semantic["doc_diff"][f"{section}_changed"]) == 1
        assert not semantic["doc_diff"][f"{section}_added"]
        assert not semantic["doc_diff"][f"{section}_removed"]
        visual = compare_mod.visual_compare(doc_a, doc_b, tmp_path / "visual")
        if have_pillow:
            assert not visual["degraded"]
            assert visual["counts"]["added"] and visual["counts"]["removed"]
            assert Path(visual["diff_2d"]).is_file()
        else:
            assert visual["degraded"]
        vault.restore(a, iid)
        assert working.read_bytes() == before
    finally:
        vault.close()
    result = worker_rpc._plm_compare(
        {"path": str(tmp_path), "item_id": iid, "commit_a": a, "commit_b": b,
         "visual": True, "out_dir": str(tmp_path / "worker-visual")}, None)
    assert result["ok"], result
    assert result["doc_diff"][f"{section}_changed"]
    if have_pillow:
        assert result["eval_compare"]["counts"]["added"] and result["eval_compare"]["counts"]["removed"]
    else:
        assert result["eval_compare"]["degraded"]


def test_native_schematic_symbol_value_change(tmp_path):
    if not core_bridge.available():
        pytest.skip("native core is required to import KiCad snapshots")
    path = tmp_path / "schematic.kicad_sch"
    original = _kicad_schematic(20)
    path.write_bytes(original)
    vault = Vault.open(tmp_path)
    try:
        iid = vault.manifest()["items"][0]["item_id"]
        a = vault.checkpoint("before")["commit"]
        path.write_bytes(original.replace(b'"10k"', b'"22k"'))
        b = vault.checkpoint("after")["commit"]
        doc_a, doc_b = compare_mod.docs_at(vault, a, b, iid)
        diff = compare_mod.semantic_compare(doc_a, doc_b)["doc_diff"]
        changed = diff["symbols_changed"]
        assert len(changed) == 1 and "R1" in changed[0]["summary"]
        assert {"field": "value", "old": "10k", "new": "22k"} in changed[0]["changes"]
    finally:
        vault.close()


@pytest.mark.parametrize(("name", "content", "section"), [
    ("board.kicad_pcb", _kicad_board, "tracks"),
    ("sheet.kicad_sch", _kicad_schematic, "wires"),
])
def test_mcp_compare_imports_kicad_history(tmp_path, name, content, section):
    pytest.importorskip("mcp")
    if not core_bridge.available():
        pytest.skip("native core is required to import KiCad snapshots")
    from services.mcp.plm_tools import register_plm_tools

    captured = {}

    class Capture:
        def tool(self, *args, **kwargs):
            def register(fn):
                captured[fn.__name__] = fn
                return fn
            return register

    register_plm_tools(Capture())
    working = tmp_path / name
    working.write_bytes(content(20))
    vault = Vault.open(tmp_path)
    try:
        iid = vault.manifest()["items"][0]["item_id"]
        a = vault.checkpoint("before")["commit"]
        working.write_bytes(content(25))
        b = vault.checkpoint("after")["commit"]
    finally:
        vault.close()
    result = json.loads(captured["plm_compare"](
        path=str(tmp_path), item_id=iid, commit_a=a, commit_b=b))
    assert result["ok"], result
    assert result["doc_diff"][f"{section}_changed"]
    assert "text_diff" not in result


def test_kicad_snapshots_are_registered_after_project_open(tmp_path):
    vault = Vault.open(tmp_path)
    try:
        working = tmp_path / "new.kicad_pcb"
        raw = _kicad_board(20)
        working.write_bytes(raw)
        _, _, untracked = vault.working_changes(vault.manifest(), kind="ecad")
        assert untracked == [{"path": working.name, "change": "U"}]
        commit = vault.checkpoint("new board")["commit"]
        item = next(it for it in vault.manifest()["items"] if it["path"] == working.name)
        assert vault._bind.read_tree(commit)[snapshot.item_prefix(item["item_id"])
                                             + "file/" + working.name] == raw
    finally:
        vault.close()


def test_snapshot_import_closes_session_when_tree_is_invalid(monkeypatch):
    calls = []

    def call(method, params):
        calls.append((method, params))
        if method == "ecad_open":
            assert Path(params["path"]).suffix == ".kicad_sch"
            assert Path(params["path"]).read_bytes() == b"(kicad_sch)"
            return {"doc_id": "temporary", "tree": {}}
        assert method == "ecad_close" and params == {"doc_id": "temporary"}
        return {"closed": True}

    monkeypatch.setattr(core_bridge, "call", call)
    with pytest.raises(VaultError, match="native importer"):
        compare_mod._import_kicad_snapshot("design.kicad_sch", b"(kicad_sch)")
    assert [method for method, _ in calls] == ["ecad_open", "ecad_close"]


def test_snapshot_import_closes_each_temporary_session(monkeypatch):
    calls = []
    paths = []

    def call(method, params):
        calls.append(method)
        if method == "ecad_open":
            path = Path(params["path"])
            paths.append(path)
            tree = {"schema": "aicad.pcb/1", "tracks": [{"id": path.read_text()}]}
            return {"doc_id": f"temporary-{len(paths)}", "tree": tree}
        assert method == "ecad_close"
        assert params["doc_id"] == f"temporary-{len(paths)}"
        return {"closed": True}

    monkeypatch.setattr(core_bridge, "call", call)
    a = {"items/i/file/board.kicad_pcb": b"old"}
    b = {"items/i/file/board.kicad_pcb": b"new"}
    assert compare_mod._document_at(a, "i")["tracks"][0]["id"] == "old"
    assert compare_mod._document_at(b, "i")["tracks"][0]["id"] == "new"
    assert calls == ["ecad_open", "ecad_close", "ecad_open", "ecad_close"]
    assert paths[0] != paths[1] and all(not path.exists() for path in paths)

# ---------------------------------------------------------------------------
# generic_compare office_extract path
# ---------------------------------------------------------------------------

def _make_fake_extractor(calls: list):
    """Stand-in office_extract: records every call and treats the file's raw
    bytes (minus a leading marker byte that forces compare.py's binary-content
    gate — mirrors a real docx/xlsx zip never being plain UTF-8 text) as
    already-extracted text. Lets the test drive the diff/cap/degrade logic
    without any real .docx/.xlsx binary."""
    def _fake_extract(path: str) -> dict:
        calls.append(path)
        data = Path(path).read_bytes()[1:]  # drop the leading marker byte
        return {"ok": True, "kind": Path(path).suffix.lstrip("."), "text": data.decode("utf-8")}
    return _fake_extract


def _office_item(tmp_path, monkeypatch, name: str, before: bytes, after: bytes):
    (tmp_path / name).write_bytes(before)
    v = Vault.open(tmp_path)
    item = v.add_item(name)
    a = v.history(item["item_id"])[0]["commit"]
    (tmp_path / name).write_bytes(after)
    v.checkpoint("edited " + name)
    b = v.history(item["item_id"])[0]["commit"]
    return v, item["item_id"], a, b


def test_generic_compare_office_diff_via_monkeypatched_extractor(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(office_tools, "extract_office", _make_fake_extractor(calls))
    # leading NUL marker byte forces compare.py's binary-content gate (_as_text
    # rejects any content containing "\x00") so the new office path is the one
    # actually exercised, not the pre-existing plain-text branch.
    v, item_id, a, b = _office_item(
        tmp_path, monkeypatch, "report.docx",
        b"\x00line one\nline two\n", b"\x00line one\nline two changed\nline three\n")
    try:
        assert compare_mod.is_generic_item(v, item_id)
        out = compare_mod.generic_compare(v, a, b, item_id)
        assert len(calls) == 2  # confirms the office extractor path actually ran
        assert out["mode"] == "generic"
        assert "text_diff" in out
        assert "line three" in out["text_diff"]
        assert "+line three" in out["text_diff"]
        _no_git_vocab(out["summary"])
    finally:
        v.close()


def test_generic_compare_office_diff_capped_at_400_lines(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(office_tools, "extract_office", _make_fake_extractor(calls))
    before = "\n".join(f"row {i}" for i in range(500)) + "\n"
    after = "\n".join(f"row {i}-x" for i in range(500)) + "\n"
    v, item_id, a, b = _office_item(
        tmp_path, monkeypatch, "sheet.xlsx",
        b"\x00" + before.encode("utf-8"), b"\x00" + after.encode("utf-8"))
    try:
        out = compare_mod.generic_compare(v, a, b, item_id)
        assert len(calls) == 2
        diff_lines = out["text_diff"].splitlines()
        assert len(diff_lines) <= 401  # 400 + truncation note
        assert "truncated" in diff_lines[-1]
    finally:
        v.close()


def test_generic_compare_office_extraction_failure_degrades_to_summary(tmp_path, monkeypatch):
    def _boom(path: str) -> dict:
        raise RuntimeError("simulated extractor crash")

    monkeypatch.setattr(office_tools, "extract_office", _boom)
    v, item_id, a, b = _office_item(
        tmp_path, monkeypatch, "deck.pptx", b"\x00\x01binary-before", b"\x00\x01binary-after")
    try:
        out = compare_mod.generic_compare(v, a, b, item_id)
        assert "text_diff" not in out
        assert out["summary"]
    finally:
        v.close()


def test_generic_compare_unsupported_binary_extension_keeps_summary_only(tmp_path, monkeypatch):
    v, item_id, a, b = _office_item(
        tmp_path, monkeypatch, "blob.bin", b"\x00\x01old", b"\x00\x01new-longer")
    try:
        out = compare_mod.generic_compare(v, a, b, item_id)
        assert "text_diff" not in out
        assert out["summary"]
    finally:
        v.close()


def test_generic_compare_plain_text_path_unchanged(tmp_path, monkeypatch):
    """Regression guard: the pre-existing plain-text diff path (byte-identical
    behavior) is untouched by the new office branch."""
    v, item_id, a, b = _office_item(
        tmp_path, monkeypatch, "spec.md", b"line one\nline two\n",
        b"line one\nline two changed\nline three\n")
    try:
        out = compare_mod.generic_compare(v, a, b, item_id)
        assert "line three" in out["text_diff"]
        _no_git_vocab(out["summary"])
    finally:
        v.close()
