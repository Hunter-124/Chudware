"""Tests for services.plm.ecad_diff (ECAD semantic diff) and the two dispatch
points that route ECAD documents to it instead of the part/asm doc_diff shape:

  * ecad_diff() on in-memory canonical PCB docs — add/remove/change coverage
    for nets and components, float-noise suppression (6dp, like doc_diff),
    and untouched sections (footprints/tracks) staying empty
  * doc_diff_tools.doc_diff() dispatch: ECAD schema -> ecad_diff, part/asm
    schema -> unchanged feature-tree diff (byte-identical old behavior)
  * compare.semantic_compare() dispatch + summary (no git vocabulary)
  * compare.generic_compare()'s new office_extract text-diff path: capped
    unified diff via a monkeypatched extractor (no real office files), graceful
    fallback when extraction is unavailable/fails, and untouched behavior for
    plain-text and non-office binary items

Runs under the gate venv (dulwich installed). No engine C++ dependency.
"""
from __future__ import annotations

from pathlib import Path

from services.mcp import doc_diff_tools, office_tools
from services.plm import compare as compare_mod
from services.plm import ecad_diff
from services.plm.vault import Vault

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


def test_float_noise_below_6dp_suppressed_like_doc_diff():
    a = _pcb_a()
    b = _pcb_a()
    # 7th decimal place noise only — rounds away to the same 6dp value.
    b["tracks"][0]["width"] = 0.2500001
    diff = ecad_diff.ecad_diff(a, b)
    assert diff["tracks_changed"] == []

    # a genuine (>= 6dp) change is still caught.
    b2 = _pcb_a()
    b2["tracks"][0]["width"] = 0.5
    diff2 = ecad_diff.ecad_diff(a, b2)
    assert {r["id"] for r in diff2["tracks_changed"]} == {"t1"}


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
