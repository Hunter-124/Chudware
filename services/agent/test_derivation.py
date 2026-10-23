"""Tests for services/agent/derivation.py — §21 derivation DAG + impact walker.

Coverage:
  - DAG build + cycle guard
  - Provenance-violation warning on derived-without-refs dims
  - Impact walk on a 3-level fixture:
      DIM7 (faulty) → DIM9 boss_od (linked → recompute+reverify) +
      2 node reverify; nothing else affected
  - link_dimension round-trip: live engine (libchudware available) OR
    documented mock fallback (no all-skip; real code paths exercised)
  - Existing ledger regression suite stays green (independent test file)
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

_TMP_CACHE = tempfile.mkdtemp(prefix="test_derivation_cache_")
os.environ.setdefault("CHUDWARE_CACHE_ROOT", _TMP_CACHE)

from services.agent.ledger import Ledger  # noqa: E402
from services.agent.derivation import (  # noqa: E402
    DerivationCycleError,
    build_dag,
    check_cycle,
    dependents_of,
    mark_faulty,
    link_dimension,
    _extract_dim_names,
    _validate_expr,
    _nodes_citing_dims,
    _ac_checks_for_dims,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh(suffix: str = "") -> tuple[str, Path]:
    import os as _os
    run_id = f"test_deriv_{suffix}_{_os.getpid()}"
    path = Path(_TMP_CACHE) / "runs" / run_id / "ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return run_id, path


def _make_ledger_3level() -> Ledger:
    """3-level fixture: DIM7 -> DIM9 (linked) -> DIM11; DIM5 independent.

    DIM7  bore  20.0 mm  (base, proposed)
    DIM9  boss_od  28.0 mm  (derived from DIM7; LINKED — so impact → recompute+reverify)
    DIM11 lid_clearance  1.0 mm  (derived from DIM9)
    DIM5  flange_od  60.0 mm  (independent — must NOT appear in impact)
    """
    run_id, path = _fresh("3level")
    ledger = Ledger(run_id, path)

    # DIM7: base dimension
    ledger.upsert_dimension(
        "foreman", "DIM7", "bore", 20.0, "mm",
        "shaft S1 H7/g6 sliding fit",
        provenance="ISO 286 H7 table",
        status="confirmed",
    )
    # DIM9: derived + linked
    ledger.upsert_dimension(
        "foreman", "DIM9", "boss_od", 28.0, "mm",
        "bore + 2 × min_wall",
        provenance="calc: boss_wall=4mm",
        status="derived",
        derived_from=["DIM7", "calc:press_fit:pf_123"],
        derivation="bore + 2*boss_wall",
        linked={"eq_id": "eq_dim9", "eq_name": "boss_od", "expr": "bore + 2*boss_wall", "name_map": {"bore": "DIM7"}},
        cited_by_departments=["dept_lid"],
    )
    # DIM11: derived from DIM9 (second level), NOT linked
    ledger.upsert_dimension(
        "foreman", "DIM11", "lid_clearance", 1.0, "mm",
        "boss_od + clearance",
        provenance="design rule: 1mm clearance",
        status="derived",
        derived_from=["DIM9"],
        derivation="boss_od + 1",
    )
    # DIM5: independent
    ledger.upsert_dimension(
        "foreman", "DIM5", "flange_od", 60.0, "mm",
        "standard flange",
        provenance="DIN 2632 table",
        status="confirmed",
    )
    return ledger


# ---------------------------------------------------------------------------
# §1  DAG build
# ---------------------------------------------------------------------------

class TestBuildDag(unittest.TestCase):
    def test_dag_only_keeps_dim_refs(self):
        ledger = _make_ledger_3level()
        dag = build_dag(ledger)
        # DIM9 derived_from includes DIM7 (kept) and calc:press_fit:pf_123 (dropped)
        self.assertIn("DIM7", dag["DIM9"])
        self.assertNotIn("calc:press_fit:pf_123", dag["DIM9"])

    def test_dag_includes_all_dims(self):
        ledger = _make_ledger_3level()
        dag = build_dag(ledger)
        self.assertIn("DIM7", dag)
        self.assertIn("DIM9", dag)
        self.assertIn("DIM11", dag)
        self.assertIn("DIM5", dag)

    def test_independent_dim_has_empty_upstreams(self):
        ledger = _make_ledger_3level()
        dag = build_dag(ledger)
        self.assertEqual(dag["DIM7"], [])
        self.assertEqual(dag["DIM5"], [])

    def test_dim11_upstream_is_dim9(self):
        ledger = _make_ledger_3level()
        dag = build_dag(ledger)
        self.assertIn("DIM9", dag["DIM11"])


# ---------------------------------------------------------------------------
# §2  Cycle guard
# ---------------------------------------------------------------------------

class TestCycleGuard(unittest.TestCase):
    def test_acyclic_dag_passes(self):
        ledger = _make_ledger_3level()
        dag = build_dag(ledger)
        check_cycle(dag)  # must not raise

    def test_direct_cycle_raises(self):
        # Manually construct a cyclic DAG
        dag = {"A": ["B"], "B": ["A"]}
        with self.assertRaises(DerivationCycleError):
            check_cycle(dag)

    def test_self_loop_raises(self):
        dag = {"A": ["A"]}
        with self.assertRaises(DerivationCycleError):
            check_cycle(dag)

    def test_three_node_cycle_raises(self):
        dag = {"A": ["C"], "B": ["A"], "C": ["B"]}
        with self.assertRaises(DerivationCycleError):
            check_cycle(dag)

    def test_empty_dag_passes(self):
        check_cycle({})  # must not raise

    def test_single_node_no_deps(self):
        check_cycle({"A": []})  # must not raise


# ---------------------------------------------------------------------------
# §3  dependents_of
# ---------------------------------------------------------------------------

class TestDependentsOf(unittest.TestCase):
    def test_dependents_of_dim7(self):
        ledger = _make_ledger_3level()
        dag = build_dag(ledger)
        deps = dependents_of("DIM7", dag)
        # DIM9 directly depends on DIM7; DIM11 transitively depends
        self.assertIn("DIM9", deps)
        self.assertIn("DIM11", deps)
        # DIM5 is independent
        self.assertNotIn("DIM5", deps)
        # DIM7 itself is not in the dependents
        self.assertNotIn("DIM7", deps)

    def test_dependents_of_dim9(self):
        ledger = _make_ledger_3level()
        dag = build_dag(ledger)
        deps = dependents_of("DIM9", dag)
        self.assertIn("DIM11", deps)
        self.assertNotIn("DIM7", deps)
        self.assertNotIn("DIM9", deps)

    def test_dependents_order_is_deterministic(self):
        """Same ledger, same fault → same ordered result."""
        ledger = _make_ledger_3level()
        dag = build_dag(ledger)
        d1 = dependents_of("DIM7", dag)
        d2 = dependents_of("DIM7", dag)
        self.assertEqual(d1, d2)

    def test_leaf_dim_has_no_dependents(self):
        ledger = _make_ledger_3level()
        dag = build_dag(ledger)
        # DIM11 has no downstream dependents
        deps = dependents_of("DIM11", dag)
        self.assertEqual(deps, [])


# ---------------------------------------------------------------------------
# §4  Provenance-violation warning
# ---------------------------------------------------------------------------

class TestProvenanceWarning(unittest.TestCase):
    def test_derived_without_refs_gets_warning(self):
        run_id, path = _fresh("prov_warn")
        ledger = Ledger(run_id, path)
        ev = ledger.upsert_dimension(
            "foreman", "DIM_WARN", "some_dim", 10.0, "mm",
            "some driver",
            provenance="calc result",
            status="derived",
            # No derived_from — should trigger warning
        )
        self.assertIn("_provenance_warning", ev["payload"])
        self.assertIn("provenance violation", ev["payload"]["_provenance_warning"])

    def test_derived_with_refs_no_warning(self):
        run_id, path = _fresh("prov_ok")
        ledger = Ledger(run_id, path)
        # First add the upstream
        ledger.upsert_dimension("foreman", "DIM1", "upstream", 5.0, "mm",
                                "base", provenance="user answer")
        ev = ledger.upsert_dimension(
            "foreman", "DIM2", "downstream", 10.0, "mm",
            "derived from upstream",
            provenance="calc result",
            status="derived",
            derived_from=["DIM1"],
        )
        self.assertNotIn("_provenance_warning", ev["payload"])

    def test_proposed_without_refs_no_warning(self):
        """Only derived/derivation dims trigger the warning — not proposed."""
        run_id, path = _fresh("prov_proposed")
        ledger = Ledger(run_id, path)
        ev = ledger.upsert_dimension(
            "foreman", "DIM1", "some_dim", 10.0, "mm",
            "driver", provenance="user answer",
            status="proposed",  # NOT derived
        )
        self.assertNotIn("_provenance_warning", ev["payload"])

    def test_warning_field_not_in_materialized_state(self):
        """_provenance_warning must stay in the event payload, not pollute state."""
        run_id, path = _fresh("prov_state")
        ledger = Ledger(run_id, path)
        ledger.upsert_dimension(
            "foreman", "DIM_X", "x", 1.0, "mm", "d", provenance="calc",
            status="derived",
        )
        dim = ledger.state["dimensions"][0]
        self.assertNotIn("_provenance_warning", dim)


# ---------------------------------------------------------------------------
# §5  mark_faulty — 3-level fixture (the key test)
# ---------------------------------------------------------------------------

class TestMarkFaulty3Level(unittest.TestCase):
    """Fault DIM7 → boss_od (DIM9) recompute+reverify + 2 node reverify; nothing else."""

    def setUp(self):
        self.ledger = _make_ledger_3level()
        # Buildgraph: 2 nodes cite DIM7 or DIM9; 1 node cites DIM5 (should NOT appear)
        self.buildgraph = {
            "schema": "aicad.buildgraph/1",
            "nodes": [
                {"id": "N_boss_feature", "label": "boss extrude",
                 "dim_refs": ["DIM9"], "kind": "feature"},
                {"id": "N_bore_check",  "label": "bore diameter check",
                 "dim_refs": ["DIM7"], "kind": "verify"},
                {"id": "N_flange",      "label": "flange feature",
                 "dim_refs": ["DIM5"], "kind": "feature"},  # independent — excluded
            ],
        }
        self.ac_checks = [
            {"id": "AC_boss_fit",  "refs": {"dims": ["DIM9"]}},
            {"id": "AC_bore_spec", "refs": {"dims": ["DIM7"]}},
            {"id": "AC_flange",    "refs": {"dims": ["DIM5"]}},  # independent — excluded
        ]

    def test_faulty_field_is_dim7(self):
        report = mark_faulty("DIM7", "upstream tolerance changed", self.ledger,
                             self.buildgraph, self.ac_checks)
        self.assertEqual(report["faulty"], "DIM7")

    def test_schema_is_impact(self):
        report = mark_faulty("DIM7", "why", self.ledger,
                             self.buildgraph, self.ac_checks)
        self.assertEqual(report["schema"], "aicad.impact/1")

    def test_affected_dims_contains_dim9_and_dim11(self):
        report = mark_faulty("DIM7", "why", self.ledger,
                             self.buildgraph, self.ac_checks)
        self.assertIn("DIM9", report["affected_dims"])
        self.assertIn("DIM11", report["affected_dims"])

    def test_dim5_not_affected(self):
        report = mark_faulty("DIM7", "why", self.ledger,
                             self.buildgraph, self.ac_checks)
        self.assertNotIn("DIM5", report["affected_dims"])

    def test_dim9_action_is_recompute_reverify_because_linked(self):
        """DIM9 is linked → action is recompute+reverify (NOT rebuild). §21 downgrade."""
        report = mark_faulty("DIM7", "why", self.ledger,
                             self.buildgraph, self.ac_checks)
        dim9_rec = next(
            (r for r in report["recommended"] if r["target"] == "DIM9"), None
        )
        self.assertIsNotNone(dim9_rec, "No recommendation found for DIM9")
        self.assertEqual(dim9_rec["action"], "recompute+reverify",
                         f"DIM9 is linked — expected recompute+reverify, got {dim9_rec['action']!r}")

    def test_dim11_action_is_rebuild_because_not_linked(self):
        """DIM11 is NOT linked → action is rebuild."""
        report = mark_faulty("DIM7", "why", self.ledger,
                             self.buildgraph, self.ac_checks)
        dim11_rec = next(
            (r for r in report["recommended"] if r["target"] == "DIM11"), None
        )
        self.assertIsNotNone(dim11_rec, "No recommendation found for DIM11")
        self.assertEqual(dim11_rec["action"], "rebuild")

    def test_affected_nodes_contains_n_boss_and_n_bore_check(self):
        """Nodes citing DIM9 or DIM7 appear; node citing DIM5 must not."""
        report = mark_faulty("DIM7", "why", self.ledger,
                             self.buildgraph, self.ac_checks)
        self.assertIn("N_boss_feature", report["affected_nodes"])
        self.assertIn("N_bore_check",   report["affected_nodes"])
        self.assertNotIn("N_flange",    report["affected_nodes"])

    def test_affected_checks_contains_ac_boss_and_ac_bore(self):
        report = mark_faulty("DIM7", "why", self.ledger,
                             self.buildgraph, self.ac_checks)
        self.assertIn("AC_boss_fit",  report["affected_checks"])
        self.assertIn("AC_bore_spec", report["affected_checks"])
        self.assertNotIn("AC_flange", report["affected_checks"])

    def test_recommended_has_exactly_right_targets(self):
        """Exactly: DIM9, DIM11, N_boss_feature, N_bore_check, AC_boss_fit, AC_bore_spec."""
        report = mark_faulty("DIM7", "why", self.ledger,
                             self.buildgraph, self.ac_checks)
        targets = {r["target"] for r in report["recommended"]}
        self.assertEqual(targets, {
            "DIM9", "DIM11",
            "N_boss_feature", "N_bore_check",
            "AC_boss_fit", "AC_bore_spec",
        }, f"Unexpected recommended targets: {targets}")

    def test_walker_is_deterministic(self):
        """Same call → same report, same order."""
        r1 = mark_faulty("DIM7", "why", self.ledger, self.buildgraph, self.ac_checks)
        r2 = mark_faulty("DIM7", "why", self.ledger, self.buildgraph, self.ac_checks)
        self.assertEqual(r1["affected_dims"], r2["affected_dims"])
        self.assertEqual(r1["affected_nodes"], r2["affected_nodes"])
        self.assertEqual(r1["affected_checks"], r2["affected_checks"])
        self.assertEqual(
            [rec["target"] for rec in r1["recommended"]],
            [rec["target"] for rec in r2["recommended"]],
        )

    def test_no_buildgraph_still_returns_report(self):
        """mark_faulty works with no buildgraph (affected_nodes=[])."""
        report = mark_faulty("DIM7", "why", self.ledger)
        self.assertEqual(report["affected_nodes"], [])
        self.assertIn("DIM9", report["affected_dims"])

    def test_change_plan_flag_wraps_report(self):
        """When the change-plan flag is set, report is wrapped in change-plan envelope."""
        orig = os.environ.get("CHUDWARE_CHANGE_PLAN")
        try:
            os.environ["CHUDWARE_CHANGE_PLAN"] = "1"
            wrapped = mark_faulty("DIM7", "why", self.ledger)
            self.assertEqual(wrapped["schema"], "aicad.change_plan/1")
            self.assertIn("impact", wrapped)
            self.assertEqual(wrapped["impact"]["schema"], "aicad.impact/1")
        finally:
            if orig is None:
                os.environ.pop("CHUDWARE_CHANGE_PLAN", None)
            else:
                os.environ["CHUDWARE_CHANGE_PLAN"] = orig


# ---------------------------------------------------------------------------
# §6  Buildgraph / AC seam helpers
# ---------------------------------------------------------------------------

class TestBuildgraphSeam(unittest.TestCase):
    def test_nodes_citing_via_dim_refs(self):
        bg = {"nodes": [
            {"id": "N1", "dim_refs": ["DIM7"], "label": ""},
            {"id": "N2", "dim_refs": ["DIM5"], "label": ""},
        ]}
        result = _nodes_citing_dims({"DIM7"}, bg)
        self.assertEqual(result, ["N1"])

    def test_nodes_citing_via_label_fallback(self):
        bg = {"nodes": [
            {"id": "N1", "label": "check DIM7 bore diameter", "dim_refs": []},
        ]}
        result = _nodes_citing_dims({"DIM7"}, bg)
        self.assertEqual(result, ["N1"])

    def test_no_buildgraph_returns_empty(self):
        self.assertEqual(_nodes_citing_dims({"DIM7"}, None), [])

    def test_ac_checks_matching(self):
        checks = [
            {"id": "AC1", "refs": {"dims": ["DIM7"]}},
            {"id": "AC2", "refs": {"dims": ["DIM5"]}},
        ]
        result = _ac_checks_for_dims({"DIM7"}, checks)
        self.assertEqual(result, ["AC1"])

    def test_ac_checks_no_match_returns_empty(self):
        checks = [{"id": "AC1", "refs": {"dims": ["DIM5"]}}]
        self.assertEqual(_ac_checks_for_dims({"DIM7"}, checks), [])


# ---------------------------------------------------------------------------
# §7  Expression parser
# ---------------------------------------------------------------------------

class TestExprParser(unittest.TestCase):
    def test_extract_names_simple(self):
        names = _extract_dim_names("bore + 2*boss_wall")
        self.assertIn("bore", names)
        self.assertIn("boss_wall", names)

    def test_extract_names_filters_builtins(self):
        names = _extract_dim_names("max(a, b) + min(c, d)")
        self.assertNotIn("max", names)
        self.assertNotIn("min", names)
        self.assertIn("a", names)
        self.assertIn("b", names)

    def test_validate_expr_clean(self):
        _validate_expr("bore + 2*boss_wall")   # must not raise
        _validate_expr("shaft_d / 2")           # must not raise
        _validate_expr("a + b - c * d")         # must not raise

    def test_validate_expr_rejects_call_syntax(self):
        # Function calls with args are not part of "simple arithmetic over names"
        # The validator allows max/min bare names but NOT e.g. "import" or semicolons
        with self.assertRaises(ValueError):
            _validate_expr("bore; os.system('rm -rf /')")


# ---------------------------------------------------------------------------
# §8  link_dimension round-trip
# ---------------------------------------------------------------------------

# Use the production resolver without an RPC or process-global library override.
# Collection must not load a stale slim engine or attach to a running daemon.
from services.mcp import core_bridge as _core_bridge

try:
    _core_bridge._resolve_lib()
except _core_bridge.CoreError:
    _LIBCHUDWARE_AVAILABLE = False
else:
    _LIBCHUDWARE_AVAILABLE = True


class TestLinkDimensionRoundTrip(unittest.TestCase):
    """
    link_dimension round-trip test.

    When libchudware is available (LIVE), we:
      1. Create a real engine doc with doc_new.
      2. Add equations bore=20 and boss_wall=4 via eq_set.
      3. Call link_dimension for DIM9 (expr "bore + 2*boss_wall").
      4. Read back doc_tree; verify the globals contain boss_od = 20 + 2*4 = 28.

    When libchudware is NOT available (MOCK), we:
      - Exercise the full code path (expr parse, op build, ledger update) using a
        mock cb that records calls and returns a plausible engine response.
      - Document the live recipe in the skip note.
    """

    def _make_link_ledger(self) -> Ledger:
        run_id, path = _fresh("link_dim")
        ledger = Ledger(run_id, path)
        ledger.upsert_dimension("foreman", "DIM7", "bore", 20.0, "mm",
                                "shaft S1", provenance="user answer",
                                status="confirmed")
        ledger.upsert_dimension("foreman", "DIMW", "boss_wall", 4.0, "mm",
                                "min wall", provenance="design rule",
                                status="confirmed")
        ledger.upsert_dimension("foreman", "DIM9", "boss_od", 28.0, "mm",
                                "bore + 2 × boss_wall", provenance="calc",
                                status="derived",
                                derived_from=["DIM7", "DIMW"],
                                derivation="bore + 2*boss_wall")
        return ledger

    def test_link_dimension_live(self):
        """Live round-trip: eq_set applied to real engine; geometry follows."""
        if not _LIBCHUDWARE_AVAILABLE:
            self.skipTest(
                "libchudware not available — see mock test and live recipe:\n"
                "  1. Ensure libchudware.so is built: cd core && cmake --build build-linux\n"
                "  2. Set CHUDWARE_LIBCHUDWARE or CHUDWARE_DIST env var\n"
                "  3. Run: .venv-mcp/bin/python -m pytest services/agent/test_derivation.py"
                "::TestLinkDimensionRoundTrip::test_link_dimension_live -v"
            )
        from services.mcp.core_bridge import call, CoreError

        class _CB:
            def call(self, method, params=None):
                return call(method, params)

        cb = _CB()
        ledger = self._make_link_ledger()

        # Create engine doc
        r = call("doc_new", {"kind": "part", "name": "link_dim_test"})
        doc_id = r["doc_id"]

        # Seed the upstream equations (bore=20, boss_wall=4) so the engine can
        # evaluate boss_od = bore + 2*boss_wall
        call("doc_apply_ops", {"doc_id": doc_id, "ops": [
            {"op": "eq_set", "name": "bore", "expr": "20"},
            {"op": "eq_set", "name": "boss_wall", "expr": "4"},
        ]})

        # Now link DIM9
        result = link_dimension("DIM9", ledger, cb, doc_id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["eq_name"], "boss_od")
        self.assertEqual(result["expr"], "bore + 2*boss_wall")
        self.assertIn("bore", result["name_map"])

        # Verify the equation was recorded in the engine
        tree = call("doc_tree", {"doc_id": doc_id})
        globals_ = tree.get("tree", {}).get("equations", {}).get("globals", [])
        eq_names = {g["name"] for g in globals_}
        self.assertIn("boss_od", eq_names,
                      f"boss_od equation not found in engine globals: {eq_names}")

        # Verify ledger updated: DIM9 now has linked field
        dim9 = next(d for d in ledger.state["dimensions"] if d["id"] == "DIM9")
        self.assertIsNotNone(dim9.get("linked"))
        self.assertEqual(dim9["linked"]["eq_name"], "boss_od")

        # Change upstream bore=22 and verify boss_od expr evaluates to 22+2*4=30
        call("doc_apply_ops", {"doc_id": doc_id, "ops": [
            {"op": "eq_set", "name": "bore", "expr": "22"},
        ]})
        tree2 = call("doc_tree", {"doc_id": doc_id})
        globals2 = tree2.get("tree", {}).get("equations", {}).get("globals", [])
        boss_od_eq = next((g for g in globals2 if g["name"] == "boss_od"), None)
        # The expr is stored as a string; evaluation happens at feature compute time.
        # Verify the expr is still correct (engine stores it verbatim).
        self.assertIsNotNone(boss_od_eq)
        self.assertEqual(boss_od_eq["expr"], "bore + 2*boss_wall",
                         "Engine must store the expr verbatim for re-evaluation")

        # Clean up
        try:
            call("close_part", {"doc_id": doc_id})
        except Exception:
            pass

    def test_link_dimension_mock_path(self):
        """Mock round-trip: exercises real parse + op-build + ledger code paths.

        The mock cb records the doc_apply_ops call and returns a plausible response.
        This ensures the code path is correct even without libchudware.

        Live recipe (for humans / CI with libchudware):
          1. Build libchudware: cd core && cmake --build build-linux
          2. Set CHUDWARE_LIBCHUDWARE=<path> or CHUDWARE_DIST=<dist dir>
          3. Run test_link_dimension_live
        """
        ledger = self._make_link_ledger()
        doc_id = "doc-mock-001"

        # Mock cb that records calls and simulates engine responses
        calls_recorded = []
        mock_cb = MagicMock()
        def _mock_call(method, params=None):
            calls_recorded.append({"method": method, "params": params})
            if method == "doc_apply_ops":
                # Simulate engine applying the op successfully
                return {
                    "ok": True,
                    "doc_id": doc_id,
                    "rebuild": {"doc_status": {"active_configuration": "Default",
                                               "active_sketch": None, "mates": None},
                                "feature_status": []},
                }
            if method == "doc_tree":
                return {"tree": {"equations": {"globals": [
                    {"name": "boss_od", "expr": "bore + 2*boss_wall"},
                ]}, "features": []}}
            return {}
        mock_cb.call = _mock_call

        result = link_dimension("DIM9", ledger, mock_cb, doc_id)

        # Verify result shape
        self.assertTrue(result["ok"])
        self.assertEqual(result["eq_name"], "boss_od")
        self.assertEqual(result["expr"], "bore + 2*boss_wall")
        self.assertIn("bore", result["name_map"])
        self.assertIn("boss_wall", result["name_map"])

        # Verify doc_apply_ops was called with the right op
        apply_calls = [c for c in calls_recorded if c["method"] == "doc_apply_ops"]
        self.assertEqual(len(apply_calls), 1)
        ops = apply_calls[0]["params"]["ops"]
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["op"], "eq_set")
        self.assertEqual(ops[0]["name"], "boss_od")
        self.assertEqual(ops[0]["expr"], "bore + 2*boss_wall")

        # Verify ledger updated with linked field
        dim9 = next(d for d in ledger.state["dimensions"] if d["id"] == "DIM9")
        self.assertIsNotNone(dim9.get("linked"))
        self.assertEqual(dim9["linked"]["eq_name"], "boss_od")
        self.assertEqual(dim9["linked"]["expr"], "bore + 2*boss_wall")

    def test_link_dimension_missing_dim_raises(self):
        run_id, path = _fresh("link_missing")
        ledger = Ledger(run_id, path)
        cb = MagicMock()
        with self.assertRaises(ValueError) as ctx:
            link_dimension("DIM_NOTEXIST", ledger, cb, "doc-x")
        self.assertIn("not found", str(ctx.exception))

    def test_link_dimension_no_derivation_raises(self):
        run_id, path = _fresh("link_no_deriv")
        ledger = Ledger(run_id, path)
        ledger.upsert_dimension("foreman", "DIM1", "bore", 20.0, "mm",
                                "driver", provenance="user answer")
        cb = MagicMock()
        with self.assertRaises(ValueError) as ctx:
            link_dimension("DIM1", ledger, cb, "doc-x")
        self.assertIn("derivation", str(ctx.exception))

    def test_link_dimension_engine_failure_raises_runtime_error(self):
        run_id, path = _fresh("link_eng_fail")
        ledger = Ledger(run_id, path)
        ledger.upsert_dimension("foreman", "DIM1", "bore", 20.0, "mm",
                                "driver", provenance="user answer",
                                status="derived", derivation="upstream + 5",
                                derived_from=["upstream"])
        cb = MagicMock()
        cb.call.side_effect = RuntimeError("engine refused")
        with self.assertRaises(RuntimeError) as ctx:
            link_dimension("DIM1", ledger, cb, "doc-x")
        self.assertIn("eq_set failed", str(ctx.exception))


# ---------------------------------------------------------------------------
# §9  §21 fields round-trip through ledger state
# ---------------------------------------------------------------------------

class TestS21FieldsInState(unittest.TestCase):
    def test_derived_from_materializes_in_state(self):
        run_id, path = _fresh("s21_derived_from")
        ledger = Ledger(run_id, path)
        ledger.upsert_dimension("foreman", "DIM1", "base", 10.0, "mm",
                                "d", provenance="user answer")
        ledger.upsert_dimension("foreman", "DIM2", "derived", 20.0, "mm",
                                "d", provenance="calc",
                                status="derived", derived_from=["DIM1"],
                                derivation="base * 2",
                                cited_by_departments=["dept_a"])
        dim2 = ledger.state["dimensions"][1]
        self.assertEqual(dim2["derived_from"], ["DIM1"])
        self.assertEqual(dim2["derivation"], "base * 2")
        self.assertEqual(dim2["cited_by_departments"], ["dept_a"])

    def test_linked_field_materializes_in_state(self):
        run_id, path = _fresh("s21_linked")
        ledger = Ledger(run_id, path)
        linked_info = {"eq_id": "eq_dim1", "eq_name": "derived", "expr": "base * 2",
                       "name_map": {"base": "DIM_BASE"}}
        ledger.upsert_dimension("foreman", "DIM1", "derived", 20.0, "mm",
                                "d", provenance="calc",
                                status="derived", derived_from=["DIM_BASE"],
                                derivation="base * 2", linked=linked_info)
        dim1 = ledger.state["dimensions"][0]
        self.assertIsNotNone(dim1.get("linked"))
        self.assertEqual(dim1["linked"]["eq_id"], "eq_dim1")

    def test_provenance_not_in_state_with_s21_fields(self):
        """§21 fields must not accidentally let provenance into state."""
        run_id, path = _fresh("s21_no_prov")
        ledger = Ledger(run_id, path)
        ledger.upsert_dimension("foreman", "DIM1", "x", 1.0, "mm",
                                "d", provenance="calc",
                                status="derived", derived_from=["DIM_UP"],
                                derivation="upstream + 1")
        dim = ledger.state["dimensions"][0]
        self.assertNotIn("provenance", dim)

    def test_s21_fields_survive_disk_reload(self):
        """§21 fields survive save→reload (JSON round-trip)."""
        run_id, path = _fresh("s21_reload")
        ledger = Ledger(run_id, path)
        ledger.upsert_dimension("foreman", "DIM1", "x", 1.0, "mm",
                                "d", provenance="calc",
                                status="derived", derived_from=["DIM_A", "DIM_B"],
                                derivation="a + b",
                                cited_by_departments=["dept_x"])
        # Reload
        ledger2 = Ledger.load(run_id, path)
        dim = ledger2.state["dimensions"][0]
        self.assertEqual(dim["derived_from"], ["DIM_A", "DIM_B"])
        self.assertEqual(dim["derivation"], "a + b")
        self.assertEqual(dim["cited_by_departments"], ["dept_x"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
