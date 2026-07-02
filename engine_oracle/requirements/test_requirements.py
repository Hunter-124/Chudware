"""Unit tests for engine_oracle.requirements (aicad.req/1 schema + pure logic)."""
from __future__ import annotations

import json

import pytest

from engine_oracle.requirements import RequirementsDoc, VALID_VERIFIERS
from engine_oracle.requirements.requirements import (
    Quantity,
    VerifyRef,
    StakeholderNeed,
    AuditEntry,
    Trace,
    Requirement,
    _check_no_numbers_in_text,
    _sort_key,
)


# ---------------------------------------------------------------------------
# Quantity
# ---------------------------------------------------------------------------

class TestQuantity:
    def test_roundtrip(self):
        q = Quantity(param="bore_dia", op="==", value=22.0, unit="mm",
                     tol={"plus": 0.0, "minus": 0.013})
        assert Quantity.from_dict(q.to_dict()) == q

    def test_format_eq(self):
        q = Quantity("bore_dia", "==", 22.0, "mm", tol={"plus": 0.0, "minus": 0.013})
        s = q.format_short()
        assert "bore_dia" in s
        assert "22" in s
        assert "mm" in s

    def test_format_range(self):
        q = Quantity("gap", "range", 0.1, "mm", value_max=0.3)
        s = q.format_short()
        assert "0.1" in s
        assert "0.3" in s

    def test_format_gte(self):
        q = Quantity("load", ">=", 50.0, "N")
        assert "≥" in q.format_short()


# ---------------------------------------------------------------------------
# VerifyRef
# ---------------------------------------------------------------------------

class TestVerifyRef:
    def test_valid_verifier(self):
        vr = VerifyRef("measure_between")
        vr.validate()  # should not raise

    def test_invalid_verifier_raises(self):
        vr = VerifyRef("nonexistent_tool")
        with pytest.raises(ValueError, match="Unknown verifier"):
            vr.validate()

    def test_all_valid_verifiers_pass(self):
        for name in VALID_VERIFIERS:
            VerifyRef(name).validate()

    def test_roundtrip_with_args(self):
        vr = VerifyRef("measure_between", args="bore inner diameter")
        assert VerifyRef.from_dict(vr.to_dict()) == vr

    def test_roundtrip_no_args(self):
        vr = VerifyRef("fit_report")
        d = vr.to_dict()
        assert "args" not in d
        assert VerifyRef.from_dict(d) == vr


# ---------------------------------------------------------------------------
# _check_no_numbers_in_text
# ---------------------------------------------------------------------------

class TestNoNumbers:
    def test_pure_prose_ok(self):
        _check_no_numbers_in_text("The bore accepts a standard press-fit bearing.")

    def test_number_in_text_raises(self):
        with pytest.raises(ValueError, match="quantity"):
            _check_no_numbers_in_text("The bore must be 22 mm wide.")

    def test_decimal_in_text_raises(self):
        with pytest.raises(ValueError):
            _check_no_numbers_in_text("Load of 50.5 N is required.")

    def test_ordinal_word_ok(self):
        # "first", "two" etc. are words, not numerals
        _check_no_numbers_in_text("The bracket must withstand first-cycle loading.")


# ---------------------------------------------------------------------------
# RequirementsDoc — basic lifecycle
# ---------------------------------------------------------------------------

class TestRequirementsDoc:
    def test_new_doc(self):
        doc = RequirementsDoc.new("My Brief")
        assert doc.title == "My Brief"
        assert doc.revision == 1
        assert len(doc.audit) >= 1

    def test_add_need(self):
        doc = RequirementsDoc.new("T")
        n = doc.add_need("Part fits the housing.", source="chat")
        assert n.id == "N1"
        assert len(doc.stakeholder_needs) == 1

    def test_add_requirement(self):
        doc = RequirementsDoc.new("T")
        n = doc.add_need("Fits housing.")
        r = doc.add_requirement(
            need_id=n.id,
            kind="dimensional",
            text="Bore fits standard bearing with press fit.",
            verify_by=[{"verifier": "measure_between"}],
            quantity={"param": "bore_dia", "op": "==", "value": 22.0, "unit": "mm"},
        )
        assert r.id == "R1"
        assert r.status == "proposed"

    def test_validate_unknown_need_id_raises(self):
        doc = RequirementsDoc.new("T")
        doc.add_need("Need.")
        doc.requirements.append(Requirement(
            id="R1", need="N99",  # unknown
            kind="dimensional",
            text="Bore fits standard bearing.",
            verify_by=[VerifyRef("measure_between")],
        ))
        with pytest.raises(ValueError, match="unknown need"):
            doc.validate()

    def test_set_status_user_confirmed(self):
        doc = RequirementsDoc.new("T")
        n = doc.add_need("Need.")
        doc.add_requirement(n.id, "dimensional", "Bore fits.",
                            [{"verifier": "measure_between"}],
                            {"param": "d", "op": "==", "value": 10.0, "unit": "mm"})
        doc.set_status("R1", "user_confirmed", who="user")
        assert doc.requirements[0].status == "user_confirmed"

    def test_set_status_agent_cannot_user_confirm(self):
        doc = RequirementsDoc.new("T")
        n = doc.add_need("Need.")
        doc.add_requirement(n.id, "dimensional", "Bore fits.",
                            [{"verifier": "measure_between"}],
                            {"param": "d", "op": "==", "value": 10.0, "unit": "mm"})
        with pytest.raises(ValueError, match="user_confirmed"):
            doc.set_status("R1", "user_confirmed", who="agent")

    def test_set_status_verified_with_evidence(self):
        doc = RequirementsDoc.new("T")
        n = doc.add_need("Need.")
        doc.add_requirement(n.id, "dimensional", "Bore fits.",
                            [{"verifier": "measure_between"}],
                            {"param": "d", "op": "==", "value": 10.0, "unit": "mm"})
        doc.set_status("R1", "user_confirmed", who="user")
        ev = [{"verifier": "measure_between", "result": {"ok": True}}]
        doc.set_status("R1", "verified", who="agent", evidence=ev)
        r = doc.requirements[0]
        assert r.status == "verified"
        assert len(r.trace.evidence) == 1

    def test_agent_cannot_waive(self):
        doc = RequirementsDoc.new("T")
        n = doc.add_need("Need.")
        doc.add_requirement(n.id, "dimensional", "Bore fits.",
                            [{"verifier": "measure_between"}])
        with pytest.raises(ValueError):
            doc.set_status("R1", "waived", who="agent")

    def test_full_roundtrip(self):
        doc = RequirementsDoc.new("RT Test")
        n = doc.add_need("System must fit the enclosure.", source="doc")
        doc.add_requirement(
            need_id=n.id,
            kind="dimensional",
            text="PCB fits within the enclosure without interference.",
            verify_by=[{"verifier": "check_interference"}, {"verifier": "measure_between"}],
            quantity={"param": "board_width", "op": "<=", "value": 100.0, "unit": "mm"},
        )
        doc.set_status("R1", "user_confirmed", who="user")
        d = doc.to_dict()
        doc2 = RequirementsDoc.from_dict(d)
        assert doc2.to_dict() == d

    def test_schema_check_on_from_dict(self):
        d = RequirementsDoc.new("T").to_dict()
        d["schema"] = "aicad.req/2"
        with pytest.raises(ValueError, match="schema"):
            RequirementsDoc.from_dict(d)


# ---------------------------------------------------------------------------
# Markdown projection
# ---------------------------------------------------------------------------

class TestMarkdownProjection:
    def _make_doc(self):
        doc = RequirementsDoc.new("Bearing Bracket")
        n = doc.add_need("Bracket bore accepts the standard bearing.")
        doc.add_requirement(
            need_id=n.id,
            kind="dimensional",
            text="Bore accepts standard press-fit bearing.",
            verify_by=[{"verifier": "measure_between", "args": "bore diameter"},
                       {"verifier": "fit_report"}],
            quantity={"param": "bore_dia", "op": "==", "value": 22.0, "unit": "mm",
                      "tol": {"plus": 0.0, "minus": 0.013}},
        )
        return doc

    def test_contains_title(self):
        md = self._make_doc().to_markdown()
        assert "Bearing Bracket" in md

    def test_contains_schema(self):
        md = self._make_doc().to_markdown()
        assert "aicad.req/1" in md

    def test_contains_req_id(self):
        md = self._make_doc().to_markdown()
        assert "R1" in md

    def test_contains_quantity_short_form(self):
        md = self._make_doc().to_markdown()
        assert "bore_dia" in md
        assert "22" in md
        assert "mm" in md

    def test_contains_verifiers(self):
        md = self._make_doc().to_markdown()
        assert "measure_between" in md
        assert "fit_report" in md

    def test_audit_trail_present(self):
        md = self._make_doc().to_markdown()
        assert "Audit Trail" in md

    def test_ordering_numeric(self):
        """R10 should sort after R9, not after R1 lexicographically."""
        doc = RequirementsDoc.new("Sort test")
        n = doc.add_need("A need.")
        for i in range(12):
            doc.requirements.append(Requirement(
                id=f"R{i + 1}",
                need=n.id,
                kind="functional",
                text=f"Requirement number {i + 1} in prose.",
                verify_by=[VerifyRef("measure_between")],
            ))
        md = doc.to_markdown()
        pos_r9 = md.index("| R9 |")
        pos_r10 = md.index("| R10 |")
        assert pos_r9 < pos_r10, "R9 should appear before R10 in the table"

    def test_projection_is_deterministic(self):
        doc = self._make_doc()
        assert doc.to_markdown() == doc.to_markdown()


# ---------------------------------------------------------------------------
# _sort_key
# ---------------------------------------------------------------------------

class TestSortKey:
    def test_numeric_sort(self):
        ids = ["R10", "R2", "R1", "R20"]
        assert sorted(ids, key=_sort_key) == ["R1", "R2", "R10", "R20"]

    def test_mixed_prefix(self):
        ids = ["N2", "N1", "N10"]
        assert sorted(ids, key=_sort_key) == ["N1", "N2", "N10"]


# ---------------------------------------------------------------------------
# Example module smoke test
# ---------------------------------------------------------------------------

class TestExample:
    def test_example_runs(self):
        from engine_oracle.requirements.example import build_example_brief, simulate_verification
        doc = build_example_brief()
        doc.validate()
        doc = simulate_verification(doc)
        r1 = next(r for r in doc.requirements if r.id == "R1")
        assert r1.status == "verified"
        assert len(r1.trace.evidence) == 1
        # Round-trip
        d = doc.to_dict()
        doc2 = RequirementsDoc.from_dict(d)
        assert doc2.to_dict() == d
