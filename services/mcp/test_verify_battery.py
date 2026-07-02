"""Tests for the verify-battery API and gap verifiers."""
from __future__ import annotations

import json
import pytest


# ── groundedness checker tests (no engine needed) ──────────────────────────

def test_groundedness_exact_match():
    from tools.translator.groundedness import check_groundedness
    result = check_groundedness("The part weighs 150 g and is 50 mm tall.",
                                {"mass": 150.0, "height": 50.0})
    assert result["score"] == pytest.approx(1.0, abs=0.01)
    assert len(result["matched"]) == 2


def test_groundedness_unit_conversion():
    from tools.translator.groundedness import check_groundedness
    # 0.05 m == 50 mm  — cross-unit match
    result = check_groundedness("The height is 0.05 m.", {"height": 50.0})
    assert result["score"] == pytest.approx(1.0, abs=0.01)


def test_groundedness_ksi_mpa():
    from tools.translator.groundedness import check_groundedness
    # 250 MPa == 36.26 ksi — cross-unit match
    result = check_groundedness("Yield stress 36.26 ksi.", {"yield_mpa": 250.0})
    assert result["score"] == pytest.approx(1.0, abs=0.05)


def test_groundedness_spurious():
    from tools.translator.groundedness import check_groundedness
    # A numeral in the text with NO state match = hallucination risk -> spurious
    result = check_groundedness("The part is 999 mm tall.", {"height": 50.0})
    assert result["score"] < 0.5
    assert len(result["spurious"]) == 1
    assert result["spurious"][0]["extracted"] == 999.0
    # the uncited state slot is informational, never penalized
    assert result["unmentioned_state"][0]["slot"] == "height"


def test_groundedness_vacuous():
    from tools.translator.groundedness import check_groundedness
    result = check_groundedness("The design looks great.", {})
    assert result["score"] == pytest.approx(1.0)


def test_groundedness_attached_unit():
    from tools.translator.groundedness import check_groundedness
    # No space between number and unit ("50mm") must still parse
    result = check_groundedness("Plate is 50mm x 3in.", {"width": 50.0, "thickness": 76.2})
    assert result["score"] == pytest.approx(1.0, abs=0.01)
    assert len(result["matched"]) == 2


def test_groundedness_no_false_unit_from_word():
    from tools.translator.groundedness import check_groundedness
    # "3 more" must NOT read as "3 m" (3000 mm); bare 3 still matches slot 3.0
    result = check_groundedness("Add 3 more ribs.", {"rib_count": 3.0})
    assert result["score"] == pytest.approx(1.0)
    assert result["matched"][0]["unit"] == ""


# ── unit_ranges tests (no engine needed) ────────────────────────────────────

def test_unit_ranges_clean():
    from services.verify.unit_ranges import check_unit_ranges
    # Typical steel bracket: volume ~50000 mm³, mass ~400 g
    state = {"volume": 50000.0, "mass": 400.0}
    assert check_unit_ranges(state) == []


def test_unit_ranges_ksi_confusion():
    from services.verify.unit_ranges import check_unit_ranges
    # A blatant out-of-range: stress of 1e7 MPa (clearly wrong)
    state = {"max_stress": 1e7}
    violations = check_unit_ranges(state)
    assert len(violations) >= 1
    assert violations[0]["field"] == "max_stress"


def test_unit_ranges_density_ok():
    from services.verify.unit_ranges import check_unit_ranges
    state = {"density": 7.85}  # steel g/cm³
    assert check_unit_ranges(state) == []


def test_unit_ranges_density_bad():
    from services.verify.unit_ranges import check_unit_ranges
    state = {"density": 7850.0}  # steel in kg/m³ — out of g/cm³ range
    violations = check_unit_ranges(state)
    assert len(violations) == 1


# ── req_join gap verifier tests (no engine needed) ──────────────────────────

def test_req_join_all_pass():
    from services.verify.battery import _gap_req_join
    r = json.loads(_gap_req_join(requirements=[
        {"id": "R1", "status": "pass"},
        {"id": "R2", "status": "verified"},
    ]))
    assert r["ok"] is True
    assert r["score"] == pytest.approx(1.0)


def test_req_join_partial():
    from services.verify.battery import _gap_req_join
    r = json.loads(_gap_req_join(requirements=[
        {"id": "R1", "status": "pass"},
        {"id": "R2", "status": "fail"},
    ]))
    assert r["ok"] is False
    assert r["score"] == pytest.approx(0.5)
    assert "R2" in r["unmet"]


def test_req_join_empty():
    from services.verify.battery import _gap_req_join
    r = json.loads(_gap_req_join(requirements=[]))
    assert r["ok"] is True


# ── check_metadata tests ─────────────────────────────────────────────────────

def test_check_metadata_structure():
    from services.verify.battery import check_metadata
    meta = check_metadata()
    assert isinstance(meta, list)
    assert len(meta) >= 20  # at least the checks from driver notes
    names = {m["name"] for m in meta}
    assert "check_sketch" in names
    assert "run_cfd_accurate" in names
    assert "groundedness" in names
    # platform tagging
    cfd_acc = next(m for m in meta if m["name"] == "run_cfd_accurate")
    assert cfd_acc["platform"] == "posix"
    assert cfd_acc["cost"] == "minutes"


# ── battery API contract tests (no engine — mock the _get_fn dispatch) ──────

def test_verify_returns_stable_structure(monkeypatch):
    """verify() returns {scores, findings, elapsed_ms, skipped} with sorted keys."""
    import services.verify.battery as bat

    # Mock _get_fn to avoid needing the engine
    def mock_get_fn(module_key, fn_name):
        def stub(**kwargs):
            return json.dumps({"ok": True, "summary": "stub"})
        return stub
    monkeypatch.setattr(bat, "_get_fn", mock_get_fn)

    result = bat.verify(doc_id="fake-doc", checks=["check_interference"])
    assert "scores" in result
    assert "findings" in result
    assert "elapsed_ms" in result
    assert "skipped" in result
    assert list(result["scores"].keys()) == sorted(result["scores"].keys())


def test_verify_deterministic(monkeypatch):
    """Two runs with same inputs return identical scores."""
    import services.verify.battery as bat

    def mock_get_fn(module_key, fn_name):
        def stub(**kwargs):
            return json.dumps({"ok": True})
        return stub
    monkeypatch.setattr(bat, "_get_fn", mock_get_fn)

    r1 = bat.verify(doc_id="x", checks=["check_interference", "check_symmetry"])
    r2 = bat.verify(doc_id="x", checks=["check_interference", "check_symmetry"])
    assert r1["scores"] == r2["scores"]


def test_verify_skips_posix_on_windows(monkeypatch):
    """run_cfd_accurate is skipped on non-posix platforms."""
    import sys
    import services.verify.battery as bat
    monkeypatch.setattr(sys, "platform", "win32")

    def mock_get_fn(module_key, fn_name):
        return lambda **kw: json.dumps({"ok": True})
    monkeypatch.setattr(bat, "_get_fn", mock_get_fn)

    result = bat.verify(doc_id="x", checks=["run_cfd_accurate"])
    assert "run_cfd_accurate" in result["skipped"]
    assert "run_cfd_accurate" not in result["scores"]


def test_verify_unknown_check(monkeypatch):
    """Unknown check names produce score=0 finding."""
    import services.verify.battery as bat
    result = bat.verify(doc_id="x", checks=["nonexistent_check"])
    assert result["scores"]["nonexistent_check"] == 0.0
    assert any("nonexistent_check" in f.get("check", "") for f in result["findings"])


def test_verify_findings_deterministic(monkeypatch):
    """Findings list (not just scores) is identical across repeated runs."""
    import services.verify.battery as bat

    def mock_get_fn(module_key, fn_name):
        def stub(**kwargs):
            return json.dumps({"ok": False, "summary": f"fail-{fn_name}"})
        return stub
    monkeypatch.setattr(bat, "_get_fn", mock_get_fn)

    checks = ["check_interference", "check_symmetry", "check_sketch"]
    r1 = bat.verify(doc_id="x", checks=checks)
    r2 = bat.verify(doc_id="x", checks=list(reversed(checks)))  # order-independent
    assert r1["scores"] == r2["scores"]
    assert r1["findings"] == r2["findings"]


def test_verify_overhead_under_50ms(monkeypatch):
    """DoD: battery overhead over the underlying checks is <50 ms (reward-loop viable).

    With every check stubbed to a no-op, elapsed_ms IS the dispatcher overhead.
    """
    import services.verify.battery as bat

    def mock_get_fn(module_key, fn_name):
        return lambda **kw: json.dumps({"ok": True})
    monkeypatch.setattr(bat, "_get_fn", mock_get_fn)

    checks = ["check_interference", "check_symmetry", "check_sketch",
              "measure_part", "ecad_run_erc", "cam_simulate"]
    bat.verify(doc_id="x", checks=checks)          # warm the signature cache path
    result = bat.verify(doc_id="x", checks=checks)
    assert result["elapsed_ms"] < 50.0
