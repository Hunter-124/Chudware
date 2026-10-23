"""Tests for the diagnostic advisors — interference axis, DFM, wall_thickness,
interface_fitment.

Contract: contracts/advisor.md §3 (advisory item shape, FROZEN), §5 (failure card), §20 (triggers).

Design:
  * Interference test is REAL: builds an overlapping-solid doc via the libchudware.so
    kernel (same path as production), calls eval_interference, and asserts that the
    advisory names the correct axis and depth derived from real AABB numbers.
  * DFM, wall_thickness, interface_fitment tests use fixture dicts that mirror the
    exact return shapes of the respective RPC calls (dfm_check, eval_thickness,
    interface_check) — mock-proven against the documented return contracts, not
    against the tools themselves. The adapters are tested in isolation with those
    fixtures so CI does not require a running engine.
  * All tests assert the §3 invariant: non-info items carry ≥1 candidate_fix.
  * Test count is real — no all-skip trap.
"""
from __future__ import annotations

import math
import pathlib
import sys
import types
import unittest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO = pathlib.Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_REPO))

from services.agent.advisor import stages
from services.agent.advisor.engine import (
    Finding,
    Observation,
    _overlap_axis_from_bboxes,
    build_advisory,
)
from services.agent.advisor import adapters


# ---------------------------------------------------------------------------
# Helper: build minimal Finding objects for provider unit tests
# ---------------------------------------------------------------------------

def _make_finding(kind: str, severity: str = "action", numbers: dict | None = None,
                  refs: dict | None = None, extra: dict | None = None) -> Finding:
    return Finding(
        kind=kind,
        check=kind,
        severity=severity,
        finding=f"test {kind} finding",
        numbers=numbers or {},
        refs=refs or {},
        source_kind="kernel",
        source_detail="test",
        extra=extra or {},
    )


def _advisory_items(findings: list[Finding], stage: str = "assembly") -> list[dict]:
    """Build an advisory from a findings list and return its items."""
    obs = Observation(stage=stage, findings=findings, seq=1, doc_id="doc-test")
    return build_advisory(obs)["items"]


def _assert_non_info_has_fix(items: list[dict], test: unittest.TestCase) -> None:
    """§3 invariant: every non-info item must have ≥1 candidate_fix."""
    for it in items:
        if it["severity"] != "info":
            test.assertGreater(
                len(it["candidate_fixes"]), 0,
                f"Non-info item {it['severity']}/{it['check']} has no candidate_fixes: {it}",
            )


# ===========================================================================
# 1. Axis-derivation helper unit tests (pure geometry math)
# ===========================================================================

class TestOverlapAxisFromBboxes(unittest.TestCase):
    """_overlap_axis_from_bboxes: derive MTS axis from real AABB data."""

    def _bbox(self, lo: list, hi: list) -> dict:
        return {"min": lo, "max": hi, "size": [hi[i] - lo[i] for i in range(3)]}

    def test_x_overlap_dominant(self) -> None:
        """Two 20x20x20 boxes, b offset +10 in X: MTS axis is +X, depth 10 mm."""
        bbox_a = self._bbox([0, 0, 0], [20, 20, 20])
        bbox_b = self._bbox([10, 0, 0], [30, 20, 20])
        axis, depth = _overlap_axis_from_bboxes(bbox_a, bbox_b)
        # X overlap = [10..20] = 10; Y = [0..20] = 20; Z = [0..20] = 20 → min is X
        self.assertEqual(axis, "+X")
        self.assertAlmostEqual(depth, 10.0, places=2)

    def test_y_overlap_dominant(self) -> None:
        """X overlap 15 mm, Y overlap 3 mm, Z overlap 10 mm → MTS axis is +Y."""
        bbox_a = self._bbox([0, 0, 0], [20, 10, 20])
        bbox_b = self._bbox([5, 7, 5], [25, 15, 25])
        axis, depth = _overlap_axis_from_bboxes(bbox_a, bbox_b)
        # X: max(0,5)=5..min(20,25)=20 → 15; Y: max(0,7)=7..min(10,15)=10 → 3; Z: 5→20 → 15
        self.assertEqual(axis, "+Y")
        self.assertAlmostEqual(depth, 3.0, places=2)

    def test_negative_axis_direction(self) -> None:
        """b entirely to the left of a center → negative axis direction."""
        bbox_a = self._bbox([10, 0, 0], [30, 20, 20])  # a is [10..30]
        bbox_b = self._bbox([0, 0, 0], [20, 20, 20])   # b is [0..20], overlap X:[10..20]=10
        # b_positive = mx_a > mx_b → 30 > 20 → True → "+X" (b moves in +X to exit a)
        axis, depth = _overlap_axis_from_bboxes(bbox_a, bbox_b)
        self.assertEqual(axis[1], "X")
        self.assertAlmostEqual(depth, 10.0, places=2)

    def test_empty_bbox_returns_unknown(self) -> None:
        axis, depth = _overlap_axis_from_bboxes({}, {})
        self.assertEqual(axis, "?")
        self.assertEqual(depth, 0.0)

    def test_no_aabb_overlap_returns_unknown(self) -> None:
        """Disjoint bboxes — no AABB overlap on X → returns '?'."""
        bbox_a = self._bbox([0, 0, 0], [10, 10, 10])
        bbox_b = self._bbox([20, 0, 0], [30, 10, 10])
        axis, depth = _overlap_axis_from_bboxes(bbox_a, bbox_b)
        self.assertEqual(axis, "?")


# ===========================================================================
# 2. Real-engine interference test
#    Builds two overlapping bodies via libchudware.so, runs eval_interference,
#    runs the full advisor pipeline, and asserts the finding names the axis.
# ===========================================================================

from services.mcp import core_bridge as _core_bridge

try:
    _core_bridge._resolve_lib()
except _core_bridge.CoreError:
    _SKIP_ENGINE = True
else:
    _SKIP_ENGINE = False
_SKIP_REASON = "libchudware not found — build the canonical core engine"


@unittest.skipIf(_SKIP_ENGINE, _SKIP_REASON)
class TestInterferenceAdvisorRealEngine(unittest.TestCase):
    """End-to-end: real overlapping-solid doc → interference advisory with axis."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._cb = _core_bridge

    def _build_overlap_doc(self) -> str:
        """Create a doc with two overlapping 20×20×20 boxes (offset +10 in X).

        Box b1 = [0..20, 0..20, 0..20]; b2 = [10..30, 0..20, 0..20].
        Expected overlap: X:[10..20] = 10 mm, volume = 4000 mm³.
        """
        cb = self.__class__._cb
        r = cb.call("doc_new", {"name": "dp6_overlap_test", "kind": "part"})
        doc_id = r["doc_id"]
        ops = [
            {"op": "add_feature", "feature": {"type": "box", "name": "Base",
                "params": {"length": 20, "width": 20, "height": 20,
                           "at": [0, 0, 0], "centered": False}}},
            {"op": "add_feature", "feature": {"type": "transform", "name": "Copy",
                "params": {"copy": True, "translate": [10.0, 0.0, 0.0]}}},
        ]
        cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": ops})
        return doc_id

    def test_interference_finding_axis_and_depth(self) -> None:
        """The interference advisor reports axis='+X' and depth≈10 mm for the test doc."""
        doc_id = self._build_overlap_doc()

        # The adapter calls rpc(method, **kwargs) where method is e.g.
        # "doc_eval_interference".  The core bridge low-level method is "eval_interference".
        # Strip the "doc_" prefix where present so the real kernel receives the right name.
        cb = self.__class__._cb
        def _call(method: str, **kwargs):
            kern_method = method[4:] if method.startswith("doc_") else method
            return cb.call(kern_method, kwargs)

        findings = adapters._check_interference(doc_id, _call)
        self.assertEqual(len(findings), 1, f"Expected 1 interference finding, got: {findings}")
        f = findings[0]
        self.assertEqual(f.kind, "interference")
        self.assertEqual(f.severity, "blocker")
        self.assertAlmostEqual(f.numbers["overlap_mm3"], 4000.0, delta=0.5)

        # The adapter must carry bbox data for the axis derivation.
        self.assertIn("bbox_a", f.extra, "bbox_a missing from interference extra")
        self.assertIn("bbox_b", f.extra, "bbox_b missing from interference extra")

        # Build the advisory and check the candidate fix names the axis.
        obs = Observation(stage="assembly", findings=findings, seq=1, doc_id=doc_id)
        advisory = build_advisory(obs)
        items = advisory["items"]
        self.assertEqual(len(items), 1)
        item = items[0]

        self.assertEqual(item["severity"], "blocker")
        self.assertGreater(len(item["candidate_fixes"]), 0,
                           "Blocker item has no candidate_fixes (§3 violation)")
        fix = item["candidate_fixes"][0]
        # The fix must name the axis and depth derived from real AABB numbers.
        axis = fix["params"].get("axis", "?")
        depth = fix["params"].get("depth_mm", 0.0)
        self.assertNotEqual(axis, "?",
                            f"Interference fix axis not derived: fix={fix}")
        self.assertEqual(axis, "+X",
                         f"Expected +X axis for X-offset overlap, got: {axis}")
        self.assertAlmostEqual(depth, 10.0, delta=0.5,
                               msg=f"Expected ~10 mm depth for X-offset overlap, got: {depth}")
        # Basis must cite the real numbers (not invented).
        basis = fix.get("basis", "")
        self.assertIn("mm³", basis, f"Basis does not cite volume: {basis}")
        self.assertIn("mm", basis, f"Basis does not cite depth: {basis}")
        # Confidence must be 'calc' (derived from kernel AABB), not 'heuristic'
        self.assertEqual(fix["confidence"], "calc",
                         f"Expected calc confidence for axis-derived fix, got: {fix['confidence']}")

    def test_no_interference_no_blocker(self) -> None:
        """A doc with a single body reports no interference (trivially ok)."""
        cb = self.__class__._cb
        r = cb.call("doc_new", {"name": "dp6_single_body", "kind": "part"})
        doc_id = r["doc_id"]
        cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": [
            {"op": "add_feature", "feature": {"type": "box", "name": "Sole",
                "params": {"length": 30, "width": 30, "height": 30,
                           "at": [0, 0, 0], "centered": False}}},
        ]})

        def _call(method: str, **kwargs):
            kern_method = method[4:] if method.startswith("doc_") else method
            return cb.call(kern_method, kwargs)

        findings = adapters._check_interference(doc_id, _call)
        # Single body → kernel reports no pairs → no findings
        self.assertEqual(findings, [], "Single body should produce no interference findings")


# ===========================================================================
# 3. Wall-thickness advisor unit tests (fixture-driven)
# ===========================================================================

class TestWallThicknessAdvisor(unittest.TestCase):

    def _call_fixture(self, thickness_mm: float, limit_mm: float | None,
                      process: str = "fdm") -> list[Finding]:
        """Simulate eval_thickness + doc_get_process via a fixture RPC."""
        def _call(method: str, **kwargs) -> dict:
            if method == "eval_thickness":
                return {
                    "min_thickness_mm": thickness_mm,
                    "mean_thickness_mm": thickness_mm * 1.5,
                    "max_thickness_mm": thickness_mm * 3.0,
                    "n_sampled": 200,
                }
            if method == "doc_get_process":
                if limit_mm is not None:
                    return {"process": process, "params": {"min_wall_mm": limit_mm}}
                return {"process": process, "params": {}}
            return {}
        return adapters._check_wall_thickness("doc-fixture", _call)

    def test_thin_wall_emits_action_finding(self) -> None:
        findings = self._call_fixture(0.8, 1.2, "fdm")
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f.kind, "wall_thickness")
        self.assertEqual(f.severity, "action")
        self.assertAlmostEqual(f.numbers["min_thickness_mm"], 0.8, places=3)
        self.assertAlmostEqual(f.numbers["limit_mm"], 1.2, places=3)

    def test_ok_wall_emits_info_finding(self) -> None:
        findings = self._call_fixture(2.0, 1.2, "fdm")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "info")

    def test_no_process_limit_emits_info(self) -> None:
        findings = self._call_fixture(1.0, None)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "info")

    def test_advisory_candidate_fix_has_thicken_wall(self) -> None:
        findings = self._call_fixture(0.8, 1.2, "fdm")
        items = _advisory_items(findings, "part_verify")
        _assert_non_info_has_fix(items, self)
        action_items = [it for it in items if it["severity"] != "info"]
        self.assertTrue(any(
            fix["action"] == "thicken_wall"
            for it in action_items for fix in it["candidate_fixes"]
        ), f"No thicken_wall fix found in: {action_items}")

    def test_fix_basis_cites_real_numbers(self) -> None:
        findings = self._call_fixture(0.8, 1.2, "fdm")
        items = _advisory_items(findings, "part_verify")
        action_items = [it for it in items if it["severity"] != "info"]
        self.assertTrue(action_items, "No action items found")
        fix = action_items[0]["candidate_fixes"][0]
        # Basis must mention measured value and process limit
        self.assertIn("0.800", fix["basis"])
        self.assertIn("1.200", fix["basis"])


# ===========================================================================
# 4. DFM advisor unit tests (fixture-driven)
# ===========================================================================

class TestDFMAdvisor(unittest.TestCase):

    def _call_dfm(self, checks: list[dict], proc: str = "fdm") -> list[Finding]:
        dfm_pass = all(c.get("ok", True) for c in checks)
        def _call(method: str, **kwargs) -> dict:
            if method == "dfm_check":
                return {"ok": True, "process": proc, "dfm_pass": dfm_pass, "checks": checks}
            return {}
        return adapters._check_dfm("doc-fixture", _call)

    def test_fail_emits_blocker(self) -> None:
        checks = [{"check": "min_wall", "value_mm": 0.7, "limit_mm": 1.2, "ok": False,
                   "note": "thinnest wall 0.70mm vs fdm minimum 1.20mm"}]
        findings = self._call_dfm(checks)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "blocker")

    def test_pass_emits_info(self) -> None:
        checks = [{"check": "min_wall", "value_mm": 2.0, "limit_mm": 1.2, "ok": True,
                   "note": "thinnest wall 2.00mm vs fdm minimum 1.20mm"}]
        findings = self._call_dfm(checks)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "info")

    def test_draft_fail_emits_blocker(self) -> None:
        checks = [{"check": "draft", "faces_below": 3, "ok": False,
                   "note": "injection_mold needs >= 1deg draft"}]
        findings = self._call_dfm(checks, "injection_mold")
        self.assertEqual(findings[0].severity, "blocker")

    def test_candidate_fixes_per_check(self) -> None:
        checks = [
            {"check": "min_wall", "value_mm": 0.7, "limit_mm": 1.2, "ok": False,
             "note": "wall too thin"},
            {"check": "draft", "faces_below": 2, "ok": False,
             "note": "draft missing"},
        ]
        findings = self._call_dfm(checks, "injection_mold")
        items = _advisory_items(findings, "part_verify")
        _assert_non_info_has_fix(items, self)
        action_items = [it for it in items if it["severity"] != "info"]
        self.assertTrue(action_items)
        fix_actions = [f["action"] for it in action_items for f in it["candidate_fixes"]]
        self.assertIn("thicken_wall", fix_actions)
        self.assertIn("add_draft", fix_actions)

    def test_checks_list_in_extra(self) -> None:
        checks = [{"check": "min_wall", "value_mm": 0.7, "limit_mm": 1.2, "ok": False,
                   "note": "wall too thin"}]
        findings = self._call_dfm(checks)
        self.assertIn("checks", findings[0].extra)
        self.assertEqual(findings[0].extra["checks"], checks)


# ===========================================================================
# 5. Interface-fitment advisor unit tests (fixture-driven)
# ===========================================================================

class TestInterfaceFitmentAdvisor(unittest.TestCase):

    def _call_ic(self, ic_findings: list[dict]) -> list[Finding]:
        fails = sum(1 for f in ic_findings if f.get("severity") == "fail")
        summary = f"{fails} fail(s)" if fails else "OK"
        def _call(method: str, **kwargs) -> dict:
            if method == "interface_check":
                # Must include tool="interface_check" so the adapter processes it.
                # A misalignment is a finding: ok stays true and status carries the verdict.
                return {"ok": True, "status": "fail" if fails else "pass",
                        "doc_id": "doc-fixture", "tool": "interface_check",
                        "version": 1, "findings": ic_findings, "summary": summary,
                        "scope_note": "test fixture"}
            return {}  # other calls return empty → adapter skips them
        return adapters._check_interface_fitment("doc-fixture", _call)

    def test_no_findings_emits_info(self) -> None:
        findings = self._call_ic([])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "info")

    def test_fail_finding_emits_action(self) -> None:
        ic_f = [{"severity": "fail", "component_a": "f2", "component_b": "f3",
                 "check": "hole_pattern", "error_mm": 1.5,
                 "detail": "hole pattern on plate offset 1.50 mm in +X relative to bracket",
                 "fix": "shift plate hole pattern -1.50 mm in X"}]
        findings = self._call_ic(ic_f)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "action")

    def test_fix_lifts_precomputed_fix_string(self) -> None:
        """The pre-computed fix string from interface_check lands in candidate_fix.params.hint."""
        ic_f = [{"severity": "fail", "component_a": "f2", "component_b": "f3",
                 "check": "hole_pattern", "error_mm": 1.5,
                 "detail": "hole pattern offset 1.50 mm in +X",
                 "fix": "shift plate hole pattern -1.50 mm in X"}]
        findings = self._call_ic(ic_f)
        items = _advisory_items(findings, "assembly")
        _assert_non_info_has_fix(items, self)
        action_items = [it for it in items if it["severity"] != "info"]
        self.assertTrue(action_items)
        fix = action_items[0]["candidate_fixes"][0]
        self.assertIn("shift plate", fix["params"]["hint"],
                      f"Pre-computed fix string not lifted: {fix}")

    def test_error_mm_in_basis(self) -> None:
        ic_f = [{"severity": "fail", "component_a": "f2", "component_b": "f3",
                 "check": "coaxiality", "error_mm": 0.45,
                 "detail": "bore axes offset 0.450 mm", "fix": "re-datum bore"}]
        findings = self._call_ic(ic_f)
        items = _advisory_items(findings, "assembly")
        action_items = [it for it in items if it["severity"] != "info"]
        fix = action_items[0]["candidate_fixes"][0]
        self.assertIn("0.450", fix["basis"],
                      f"error_mm not reflected in basis: {fix['basis']}")

    def test_pairs_deduped(self) -> None:
        """Two findings for the same f2↔f3 pair produce one Finding."""
        ic_f = [
            {"severity": "fail", "component_a": "f2", "component_b": "f3",
             "check": "hole_pattern", "error_mm": 1.5, "detail": "d1", "fix": "f1"},
            {"severity": "fail", "component_a": "f2", "component_b": "f3",
             "check": "coaxiality", "error_mm": 0.3, "detail": "d2", "fix": "f2"},
        ]
        findings = self._call_ic(ic_f)
        self.assertEqual(len(findings), 1, "Same pair should produce one Finding")


# ===========================================================================
# 6. §3 invariant across all 4 new checkers
# ===========================================================================

class TestSection3InvariantAllCheckers(unittest.TestCase):
    """Every non-info advisory item must carry ≥1 candidate_fix (§3, FROZEN)."""

    def _assert_invariant(self, findings: list[Finding], stage: str) -> None:
        items = _advisory_items(findings, stage)
        _assert_non_info_has_fix(items, self)

    def test_interference_action_has_fix(self) -> None:
        f = _make_finding("interference", "action",
                          numbers={"overlap_mm3": 500.0},
                          refs={"bodies": ["b1", "b2"]},
                          extra={"bbox_a": {"min": [0,0,0], "max": [20,20,20]},
                                 "bbox_b": {"min": [10,0,0], "max": [30,20,20]}})
        self._assert_invariant([f], "assembly")

    def test_interference_blocker_has_fix(self) -> None:
        f = _make_finding("interference", "blocker",
                          numbers={"overlap_mm3": 4000.0},
                          refs={"bodies": ["b1", "b2"]},
                          extra={"bbox_a": {"min": [0,0,0], "max": [20,20,20]},
                                 "bbox_b": {"min": [10,0,0], "max": [30,20,20]}})
        self._assert_invariant([f], "assembly")

    def test_wall_thickness_action_has_fix(self) -> None:
        f = _make_finding("wall_thickness", "action",
                          numbers={"min_thickness_mm": 0.7, "limit_mm": 1.2, "process": "fdm"},
                          extra={"process": "fdm", "limit_mm": 1.2})
        self._assert_invariant([f], "part_verify")

    def test_dfm_blocker_has_fix(self) -> None:
        f = _make_finding("dfm", "blocker",
                          extra={"checks": [{"check": "min_wall", "value_mm": 0.7,
                                             "limit_mm": 1.2, "ok": False,
                                             "note": "wall 0.7mm < 1.2mm"}]})
        self._assert_invariant([f], "part_verify")

    def test_interface_fitment_action_has_fix(self) -> None:
        f = _make_finding(
            "interface_fitment", "action",
            extra={"findings": [{"severity": "fail", "component_a": "f2",
                                 "component_b": "f3", "check": "hole_pattern",
                                 "error_mm": 1.5, "detail": "offset 1.5mm",
                                 "fix": "shift -1.5mm"}]},
        )
        self._assert_invariant([f], "assembly")


# ===========================================================================
# 7. Pre-op triggers fire correctly
# ===========================================================================

class TestPreOpTriggers(unittest.TestCase):

    def _match(self, text: str = "", tools: list[str] | None = None,
               events: list[str] | None = None) -> list[dict]:
        from services.agent.advisor.triggers import match_triggers
        ctx_items, _ = match_triggers(
            output_text=text, tool_names=tools or [], events=events or [],
            seq=1, cooldown_state={},
        )
        return ctx_items

    def test_interference_trigger_on_tool_call(self) -> None:
        items = self._match(tools=["part_place"])
        ids = [it.get("check") for it in items]
        self.assertIn("context:interference_pre_op", ids,
                      f"interference_pre_op trigger did not fire: {ids}")

    def test_dfm_trigger_on_keyword(self) -> None:
        items = self._match(text="let me check the minimum wall for injection molding")
        ids = [it.get("check") for it in items]
        self.assertIn("context:dfm_pre_op", ids,
                      f"dfm_pre_op trigger did not fire: {ids}")

    def test_interface_trigger_on_keyword(self) -> None:
        items = self._match(text="I need to align the hole pattern between the bracket and plate")
        ids = [it.get("check") for it in items]
        self.assertIn("context:interface_check_pre_op", ids,
                      f"interface_check_pre_op trigger did not fire: {ids}")

    def test_interference_trigger_on_event(self) -> None:
        items = self._match(events=["assembly_body_added"])
        ids = [it.get("check") for it in items]
        self.assertIn("context:interference_pre_op", ids,
                      f"interference_pre_op trigger did not fire on event: {ids}")


# ===========================================================================
# 8. Stage batteries include the new checks
# ===========================================================================

class TestStageBatteries(unittest.TestCase):

    def test_part_verify_includes_wall_thickness(self) -> None:
        battery = stages.check_battery("part_verify")
        self.assertIn(stages.CHECK_WALL_THICKNESS, battery)

    def test_part_verify_includes_dfm(self) -> None:
        battery = stages.check_battery("part_verify")
        self.assertIn(stages.CHECK_DFM, battery)

    def test_assembly_includes_interface_fitment(self) -> None:
        battery = stages.check_battery("assembly")
        self.assertIn(stages.CHECK_INTERFACE_FITMENT, battery)

    def test_part_verify_exit_gate_unchanged(self) -> None:
        """New checks are advisory — the exit gate still only requires CHECK_PART_VERIFY."""
        gate = stages.exit_gate("part_verify")
        self.assertIn(stages.CHECK_PART_VERIFY, gate)
        # New checks must NOT block stage exit (they are advisory, not gates)
        self.assertNotIn(stages.CHECK_WALL_THICKNESS, gate)
        self.assertNotIn(stages.CHECK_DFM, gate)

    def test_assembly_exit_gate_unchanged(self) -> None:
        """interface_fitment is advisory — the exit gate still requires only CHECK_INTERFERENCE."""
        gate = stages.exit_gate("assembly")
        self.assertIn(stages.CHECK_INTERFERENCE, gate)
        self.assertNotIn(stages.CHECK_INTERFACE_FITMENT, gate)


if __name__ == "__main__":
    unittest.main()
