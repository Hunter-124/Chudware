"""The WI-15 ref guardrail must not block a datum the KERNEL resolves.

This is a false-block regression, caught from a live Opus run. ``cad_guide('sketch')``
states the sketch plane must be spelled ``"plane.top"`` / ``"plane.front"`` /
``"plane.right"`` and that ``"XY"``/``"XZ"``/``"YZ"`` are rejected. A model obeyed
that exactly and got:

    HARD guardrail: dangling feature/face refs — cannot apply. Missing: plane.top.
    Fix the refs (no waiver for kernel integrity). ... name datum planes front/top/right.

The guard was checking a kernel datum against the document's FEATURE tree, where it
can never appear. The bare forms only survived by accident, through the
plain-English heuristic in ``_looks_like_geom_query``. So our own documented
spelling was unbuildable, with no waiver, in a HARD gate — the worst combination.

The parity test below is the real defence: it reads the reserved set straight out of
``core/doc/validate.cpp`` so the Python copy cannot silently drift from the kernel.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from services.agent.guardrails import (
    RESERVED_DATUMS,
    _refs_hint,
    check_refs_resolve_ids,
    pre_dispatch_refs,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
VALIDATE_CPP = REPO / "core" / "doc" / "validate.cpp"


def _engine_reserved_datums() -> set[str]:
    """Parse ``RESERVED_DATUMS`` out of the kernel — the single source of truth."""
    src = VALIDATE_CPP.read_text()
    m = re.search(r"const std::set<string> RESERVED_DATUMS = \{(.*?)\};", src, re.S)
    assert m, "RESERVED_DATUMS moved or was renamed in core/doc/validate.cpp"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def test_reserved_datum_parity_with_the_kernel():
    """Drift here re-creates a waiver-less block on a name the engine accepts."""
    if not VALIDATE_CPP.is_file():
        pytest.skip("kernel source not present in this checkout")
    assert set(RESERVED_DATUMS) == _engine_reserved_datums()


class TestReservedDatumsResolve:
    @pytest.mark.parametrize("datum", sorted(RESERVED_DATUMS))
    def test_every_reserved_datum_resolves_as_a_string_ref(self, datum):
        ops = [{"op": "add_feature",
                "feature": {"type": "sketch", "refs": {"plane": datum}}}]
        assert check_refs_resolve_ids(ops, set(), set()) == []

    @pytest.mark.parametrize("datum", sorted(RESERVED_DATUMS))
    def test_every_reserved_datum_resolves_as_a_datum_selector(self, datum):
        ops = [{"op": "add_feature", "feature": {
            "type": "sketch",
            "refs": {"plane": {"kind": "datum", "feature": datum}}}}]
        assert check_refs_resolve_ids(ops, set(), set()) == []

    def test_reserved_datum_in_top_level_refs_resolves(self):
        """Models flatten the op; the guard scans top-level refs too."""
        ops = [{"op": "add_feature", "type": "sketch",
                "refs": {"plane": "plane.top"}}]
        assert check_refs_resolve_ids(ops, set(), set()) == []

    def test_a_genuinely_dangling_ref_is_still_blocked(self):
        """The guard must stay sharp — this is kernel integrity, not politeness."""
        ops = [{"op": "add_feature",
                "feature": {"type": "extrude", "refs": {"profile": "f99"}}}]
        assert check_refs_resolve_ids(ops, set(), set()) == ["f99"]

    def test_an_invented_plane_name_is_still_blocked(self):
        ops = [{"op": "add_feature",
                "feature": {"type": "sketch", "refs": {"plane": "plane.diagonal"}}}]
        assert check_refs_resolve_ids(ops, set(), set()) == ["plane.diagonal"]


class TestOpusSketchBatch:
    """The verbatim batch from the live run, minus the doc it targeted."""

    OPS = [
        {"op": "add_feature", "type": "sketch", "refs": {"plane": "plane.top"}},
        *[{"op": "sketch_add_entity", "sketch": "$0",
           "entity": {"type": "line", "p1": p1, "p2": p2}}
          for p1, p2 in [([50, 0], [9.9, 9.9]), ([9.9, 9.9], [0, 50]),
                         ([0, 50], [-9.9, 9.9]), ([-9.9, 9.9], [-50, 0]),
                         ([-50, 0], [-9.9, -9.9]), ([-9.9, -9.9], [0, -50]),
                         ([0, -50], [9.9, -9.9]), ([9.9, -9.9], [50, 0])]],
        {"op": "add_feature", "type": "extrude", "refs": {"profile": "$0"},
         "params": {"distance": 4, "symmetric": True}},
    ]

    def test_batch_is_not_hard_blocked(self):
        assert check_refs_resolve_ids(self.OPS, set(), set()) == []

    def test_pre_dispatch_lets_it_through(self):
        verdict = pre_dispatch_refs(
            "add_features", {"doc_id": "doc-x", "ops": self.OPS},
            known=(set(), set()))
        assert verdict is None, (
            "the documented sketch-plane spelling must never be hard-blocked")


class TestRefsHint:
    def test_empty_document_hint_names_the_real_datums(self):
        hint = _refs_hint((set(), set()))
        assert "plane.top" in hint and "axis.z" in hint

    def test_hint_no_longer_advises_the_bare_spelling_alone(self):
        """It used to say 'name datum planes front/top/right' — which is not
        what cad_guide documents, so following it contradicted the guide."""
        hint = _refs_hint((set(), set()))
        assert "front/top/right" not in hint

    def test_populated_document_hint_lists_features_and_datums(self):
        hint = _refs_hint(({"f1"}, {"Plate"}))
        assert "f1" in hint and "Plate" in hint and "plane.top" in hint
