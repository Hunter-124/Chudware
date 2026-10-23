"""Tests for suggest_dimension_scheme.

Tests run in offline mode (no bridge / no live doc) using the built-in 4-hole
plate sketch fixture.  The bridge-unavailable path returns a synthetic entity
list and notes the offline mode.

Run:
  python -m pytest services/mcp/test_dimension_scheme.py -v
"""
from __future__ import annotations

import json
import sys
import types
import unittest
import unittest.mock

# ---------------------------------------------------------------------------
# Stubs so imports succeed without libchudware.so or live MCP server
# ---------------------------------------------------------------------------

def _install_stubs():
    """Install lightweight stubs so dimension_scheme_tools + deps import without libchudware.so.

    Returns a `restore()` callable. The stubs are needed ONLY at import time (every test
    here exercises pure offline logic — no bridge call at run time), so the module wraps
    its imports in `try: … finally: restore()` below. That matters for whole-dir pytest
    sweeps: this module used to `setdefault` a fake `services.mcp.core_bridge` / `mcp` SDK
    and — worse — mutate the REAL `services.mcp` package's attributes, which leaked into
    sibling test modules (test_cad_tools' volume-baseline + render-caption saw a stubbed
    core_bridge and failed). `restore()` removes exactly the keys/attrs this installed and
    leaves any pre-existing real module untouched, so nothing leaks past import.
    """
    added: list = []            # sys.modules keys we newly created
    attr_undo: list = []        # (module, attr, had_it, original) to undo attribute writes

    def _set(key: str, mod):
        """Install `mod` at sys.modules[key] only if absent; track it for restore."""
        if key not in sys.modules:
            sys.modules[key] = mod
            added.append(key)
        return sys.modules[key]

    def _setattr(mod, attr, value):
        attr_undo.append((mod, attr, hasattr(mod, attr), getattr(mod, attr, None)))
        setattr(mod, attr, value)

    # core_bridge stub. Issue #17: cad_tools does `from .core_bridge import CoreError` at
    # import time, so the stub must carry CoreError or a full-dir collection aborts.
    cb = types.ModuleType("services.mcp.core_bridge")
    cb.available = lambda: False
    cb.call = lambda method, params=None: {}
    cb.CoreError = type("CoreError", (Exception,), {})
    _set("services.mcp.core_bridge", cb)
    _set("core_bridge", cb)
    _set("services.mcp.core_bridge", cb)

    # mcp.server.fastmcp stub
    class _FakeMCP:
        def __init__(self): self.tools_reg = {}
        def tool(self):
            def decorator(fn):
                self.tools_reg[fn.__name__] = fn
                return fn
            return decorator

    mcp_mod = types.ModuleType("mcp")
    server_mod = types.ModuleType("mcp.server")
    fastmcp_mod = types.ModuleType("mcp.server.fastmcp")
    fastmcp_mod.FastMCP = _FakeMCP
    _set("mcp", mcp_mod)
    _set("mcp.server", server_mod)
    _set("mcp.server.fastmcp", fastmcp_mod)

    # services.mcp.cad_tools stubs for _dumps / _err / _doc
    ct = types.ModuleType("services.mcp.cad_tools")
    ct._dumps = json.dumps
    ct._err = lambda e: json.dumps({"ok": False, "error": str(e)})
    ct._doc = lambda d: d
    _set("services.mcp.cad_tools", ct)
    ms = types.ModuleType("services.mcp")
    mcp_server_pkg = _set("services.mcp", ms)

    # fit_narration stub — gsl_tools imports _format_fit at module level
    fn_mod = types.ModuleType("services.mcp.fit_narration")
    fn_mod._format_fit = lambda fit_result: ""  # no-op for offline tests
    _set("services.mcp.fit_narration", fn_mod)
    # Wire submodule attributes ONLY on a services.mcp package WE created — never clobber a
    # real, already-imported services.mcp (that leak is what broke sibling tests).
    if "services.mcp" in added:
        _setattr(mcp_server_pkg, "fit_narration", fn_mod)
        _setattr(mcp_server_pkg, "cad_tools", ct)
        _setattr(mcp_server_pkg, "core_bridge", cb)

    # engine stub (for research imports in fits.py / material_lookup.py)
    eng = types.ModuleType("engine")
    eng_materials = types.ModuleType("engine.materials")
    eng_materials.density = lambda name: None
    # paths re-homed to services.paths — stub it there (sys.modules
    # fallback covers `from services import paths`).
    eng_paths = types.ModuleType("services.paths")
    import pathlib
    eng_paths.CONFIG = pathlib.Path("/nonexistent/config.json")
    eng.materials = eng_materials
    _set("engine", eng)
    _set("engine.materials", eng_materials)
    _set("services.paths", eng_paths)

    def restore():
        for key in added:
            sys.modules.pop(key, None)
        for mod, attr, had_it, original in attr_undo:
            if had_it:
                setattr(mod, attr, original)
            else:
                try:
                    delattr(mod, attr)
                except AttributeError:
                    pass

    return restore


# Install stubs, import the offline modules-under-test, then REMOVE the stubs so a
# whole-dir sweep never inherits a fake core_bridge / mcp SDK.
_restore_stubs = _install_stubs()
try:
    from services.research.fits import fit_calc, recommend_fit  # noqa: E402
    # Import dimension_scheme internals directly (not via MCP)
    from services.mcp.dimension_scheme_tools import (  # noqa: E402
        _plan_from_entities,
        _make_fixture_sketch as _dim_fixture,
        _make_bore_sketch,
        _it_grade_for_process,
        _bilateral_tolerance,
        _find_bolt_patterns,
        _drawing_plan_from_features,
        _process_verdict,
        register_dimension_scheme_tools,
    )
finally:
    _restore_stubs()


def get_profile(name: str) -> dict:
    """The DfM profile for `name`, resolved through the PRODUCT's own lookup.

    Deliberately NOT bound at import time: `_install_stubs()` above registers a fake
    `core_bridge` whose `call()` returns `{}`, so a module-level `from services.mcp import
    core_bridge as _cb` captured that stub in a clean process and this helper handed back
    `None`. It only ever produced a real profile when some earlier test in the same
    process had already imported the real core_bridge — an order-dependent false green.
    Resolving lazily via `dimension_scheme_tools._get_profile` (a) always sees the real
    bridge and (b) raises a teaching ValueError instead of returning None.
    """
    from services.mcp.dimension_scheme_tools import _get_profile
    return _get_profile(name)


class TestBoltPatternRule(unittest.TestCase):
    """The core rule: bolt patterns use datum-offset + centre-to-centre only."""

    def setUp(self):
        # 4-hole plate: 40mm spacing, 5mm holes
        self.entities = _dim_fixture(num_holes=4, bolt_spacing_mm=40.0, hole_dia_mm=5.0)

    def test_fixture_has_four_circles(self):
        self.assertEqual(len(self.entities), 4)
        for e in self.entities:
            self.assertEqual(e["type"], "circle")

    def test_bolt_pattern_detected(self):
        groups = _find_bolt_patterns(self.entities)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 4)

    def test_plan_has_datum_offset(self):
        plan = _plan_from_entities(self.entities, "cnc_mill", it_grade=7)
        types_emitted = [item["dim_type"] for item in plan]
        self.assertIn("datum_offset", types_emitted, "Plan must include a datum_offset dim")

    def test_plan_has_centre_to_centre(self):
        plan = _plan_from_entities(self.entities, "cnc_mill", it_grade=7)
        types_emitted = [item["dim_type"] for item in plan]
        self.assertIn("centre_to_centre", types_emitted)

    def test_no_edge_to_hole_chain(self):
        """Verify plan never emits a chain that would be 'edge-to-hole-to-hole'.
        Implementation rule: only datum_offset + centre_to_centre, never
        consecutive 'length' dims on hole entities (which would imply chaining).
        """
        plan = _plan_from_entities(self.entities, "cnc_mill", it_grade=7)
        hole_lengths = [item for item in plan
                        if item["dim_type"] == "length" and "hole" in item.get("why_critical", "")]
        self.assertEqual(len(hole_lengths), 0,
                         "Holes must not generate 'length' dims (would imply edge-to-hole chain)")

    def test_datum_offset_needs_confirm(self):
        """Datum offset is function-critical -> needs_user_confirm=True."""
        plan = _plan_from_entities(self.entities, "cnc_mill", it_grade=7)
        datum_items = [item for item in plan if item["dim_type"] == "datum_offset"]
        self.assertTrue(len(datum_items) >= 1)
        for item in datum_items:
            self.assertTrue(item["needs_user_confirm"],
                            "datum_offset dims must have needs_user_confirm=True")

    def test_centre_to_centre_no_confirm(self):
        """Centre-to-centre dims are deterministic -> needs_user_confirm=False."""
        plan = _plan_from_entities(self.entities, "cnc_mill", it_grade=7)
        ctc_items = [item for item in plan if item["dim_type"] == "centre_to_centre"]
        self.assertTrue(len(ctc_items) >= 1)
        for item in ctc_items:
            self.assertFalse(item["needs_user_confirm"],
                             "centre_to_centre dims should not require user confirmation")

    def test_diameter_needs_confirm(self):
        """Hole diameter dims are mating features -> needs_user_confirm=True."""
        plan = _plan_from_entities(self.entities, "cnc_mill", it_grade=7)
        dia_items = [item for item in plan if item["dim_type"] == "diameter"]
        self.assertTrue(len(dia_items) >= 1)
        for item in dia_items:
            self.assertTrue(item["needs_user_confirm"],
                            "diameter dims on holes are function-critical, need confirm")

    def test_priority_ordering(self):
        """Datum offset has lower priority number (higher priority) than auxiliary dims."""
        plan = _plan_from_entities(self.entities, "cnc_mill", it_grade=7)
        datum_pri = [item["priority"] for item in plan if item["dim_type"] == "datum_offset"]
        ctc_pri = [item["priority"] for item in plan if item["dim_type"] == "centre_to_centre"]
        self.assertTrue(all(d < c for d in datum_pri for c in ctc_pri),
                        "Datum offset must have lower priority number than centre-to-centre")

    def test_tolerance_it_grade_present(self):
        """Each item's default_tolerance carries an IT grade reference."""
        plan = _plan_from_entities(self.entities, "cnc_mill", it_grade=7)
        for item in plan:
            tol = item.get("default_tolerance", {})
            self.assertIn("it_grade", tol, f"Item {item['entity_ref']} missing it_grade in tolerance")
            self.assertEqual(tol["it_grade"], "IT7")

    def test_fdm_uses_it12(self):
        grade = _it_grade_for_process("fdm")
        self.assertEqual(grade, 12)

    def test_cnc_uses_it7(self):
        grade = _it_grade_for_process("cnc_mill")
        self.assertEqual(grade, 7)

    def test_sheet_metal_uses_it10(self):
        grade = _it_grade_for_process("sheet_metal")
        self.assertEqual(grade, 10)

    def test_bilateral_tolerance_structure(self):
        tol = _bilateral_tolerance(20.0, 7)
        self.assertIn("it_grade", tol)
        self.assertIn("upper_mm", tol)
        self.assertIn("lower_mm", tol)
        self.assertIn("band_mm", tol)
        self.assertIn("source", tol)
        self.assertEqual(tol["it_grade"], "IT7")
        # IT7 at 20mm = 21µm; bilateral: upper=+10µm, lower=-11µm
        self.assertAlmostEqual(tol["upper_mm"], 0.010, places=6)
        self.assertAlmostEqual(tol["lower_mm"], -0.011, places=6)
        self.assertAlmostEqual(tol["band_mm"], 0.021, places=6)

    def test_source_citation_present(self):
        tol = _bilateral_tolerance(20.0, 7)
        self.assertIn("citation", tol["source"])
        self.assertIn("ISO 286", tol["source"]["citation"])


class TestFitsIntegration(unittest.TestCase):
    """Cross-check that fit_calc and recommend_fit integrate well with the dimension scheme."""

    def test_sliding_to_fit_calc_pipeline(self):
        """recommend_fit -> fit_calc pipeline produces a press/clearance result."""
        rec = recommend_fit("sliding")
        self.assertTrue(rec["ok"])
        # Take first recommended class
        first_class = rec["answer"][0]["recommended_classes"][0]
        # Compute band for 20mm
        result = fit_calc(20.0, first_class)
        self.assertTrue(result["ok"], f"fit_calc failed: {result}")

    def test_fit_calc_result_integrates_with_confirm_flag(self):
        """fit_calc result fields map cleanly to ledger dimension entry."""
        result = fit_calc(20.0, "H7/g6")
        self.assertTrue(result["ok"])
        ans = result["answer"]
        # Fields needed for ledger dimension entry (contracts/advisor.md §2)
        self.assertIn("hole", ans)
        self.assertIn("shaft", ans)
        self.assertIn("fit_character", ans)
        self.assertIn("clearance", ans)

    def test_press_fit_flag(self):
        result = fit_calc(50.0, "H7/p6")
        self.assertTrue(result["ok"])
        self.assertEqual(result["answer"]["fit_character"], "press")


class TestProcessDifferentiation(unittest.TestCase):
    """DoD 3: machined bore vs printed hole produce different dimension schemes.

    The differences are sourced from engine_oracle/dfm/process_profiles.py:
    - cnc_mill: hole_comp_mm=0.0, IT grade 7 (±~0.010 mm at 20mm)
    - fdm:      hole_comp_mm=0.20, IT grade 12 (±~0.090 mm at 20mm)

    A bore modeled at 20mm nominal should be:
    - CNC: suggested_value=20.0 (no comp), IT7 tolerance band ~0.021 mm
    - FDM: suggested_value=20.20 (+0.20 comp so the PRINTED hole finishes at 20mm), IT12 ~0.180 mm
    """

    NOMINAL_MM = 20.0

    def _bore_plan(self, process: str) -> list[dict]:
        entities = _make_bore_sketch(self.NOMINAL_MM)
        it_grade = _it_grade_for_process(process)
        return _plan_from_entities(entities, process, it_grade,
                                   nominal_fallback_mm=self.NOMINAL_MM)

    # ── IT-grade differs ─────────────────────────────────────────────────────────

    def test_cnc_uses_it7_fdm_uses_it12(self):
        """CNC→IT7, FDM→IT12: a different precision tier, not a rounding diff."""
        self.assertEqual(_it_grade_for_process("cnc_mill"), 7)
        self.assertEqual(_it_grade_for_process("fdm"), 12)

    def test_tolerance_band_differs_by_process(self):
        """IT7 at 20mm = 0.021 mm band; IT12 at 20mm = 0.209 mm band — a ~10× spread.
        Values computed from ISO 286-1:2010 Table 1 via _it_value_um().
        IT7:  mult=16, D=sqrt(18*30)≈23.2 → i≈1.307 → 16*1.307≈20.9→21µm → 0.021mm.
        IT12: mult=160 → 160*1.307≈209µm → 0.209mm.
        """
        tol_cnc = _bilateral_tolerance(self.NOMINAL_MM, 7)
        tol_fdm = _bilateral_tolerance(self.NOMINAL_MM, 12)
        # CNC band: IT7 at 20mm → 21µm = 0.021 mm
        self.assertAlmostEqual(tol_cnc["band_mm"], 0.021, places=6)
        # FDM band: IT12 at 20mm → 209µm = 0.209 mm
        self.assertAlmostEqual(tol_fdm["band_mm"], 0.209, places=6)
        # The FDM band is materially wider — not a float-precision tie
        self.assertGreater(tol_fdm["band_mm"], tol_cnc["band_mm"] * 5)

    # ── hole_comp_mm differs ─────────────────────────────────────────────────────

    def test_cnc_bore_no_hole_comp(self):
        """CNC mill has hole_comp_mm=0 — the modeled diameter equals the nominal."""
        plan = self._bore_plan("cnc_mill")
        dia_items = [i for i in plan if i["dim_type"] == "diameter"]
        self.assertTrue(len(dia_items) >= 1, "expected at least one diameter dim")
        for item in dia_items:
            self.assertAlmostEqual(item["hole_comp_mm"], 0.0, places=6,
                                   msg="CNC has no hole compensation")
            # suggested_value = nominal (no comp) — verify it equals NOMINAL_MM
            sv = item.get("suggested_value")
            if sv is not None:
                self.assertAlmostEqual(sv, self.NOMINAL_MM, places=3,
                                       msg="CNC: suggested_value should be the nominal diameter")

    def test_fdm_bore_gets_positive_hole_comp(self):
        """FDM has hole_comp_mm=0.20 — the modeled diameter is 0.20mm larger than nominal
        so the printed hole finishes at the nominal size after shrinkage."""
        plan = self._bore_plan("fdm")
        dia_items = [i for i in plan if i["dim_type"] == "diameter"]
        self.assertTrue(len(dia_items) >= 1, "expected at least one diameter dim")
        for item in dia_items:
            self.assertAlmostEqual(item["hole_comp_mm"], 0.20, places=6,
                                   msg="FDM hole_comp_mm must be 0.20 (from process_profiles)")
            sv = item.get("suggested_value")
            if sv is not None:
                # FDM: model the hole 0.20 mm larger so the FINISHED hole is NOMINAL_MM
                self.assertAlmostEqual(sv, self.NOMINAL_MM + 0.20, places=3,
                                       msg="FDM: suggested_value should be nominal + hole_comp")

    def test_machined_vs_printed_schemes_differ(self):
        """The two plans differ in both IT grade string and suggested_value — end-to-end proof."""
        cnc_plan = self._bore_plan("cnc_mill")
        fdm_plan = self._bore_plan("fdm")

        cnc_grades = {i["default_tolerance"]["it_grade"] for i in cnc_plan}
        fdm_grades = {i["default_tolerance"]["it_grade"] for i in fdm_plan}
        self.assertIn("IT7", cnc_grades, "CNC plan must carry IT7 tolerance")
        self.assertIn("IT12", fdm_grades, "FDM plan must carry IT12 tolerance")
        self.assertNotEqual(cnc_grades, fdm_grades,
                            "CNC and FDM must produce different IT grade sets")

        cnc_comp = {i["hole_comp_mm"] for i in cnc_plan}
        fdm_comp = {i["hole_comp_mm"] for i in fdm_plan}
        self.assertNotEqual(cnc_comp, fdm_comp,
                            "CNC and FDM must produce different hole_comp_mm values")

    def test_hole_comp_sourced_from_process_profiles(self):
        """The hole_comp values match process_profiles.PROFILES — they are never invented.
        get_profile() nests DFM params under a 'params' key; extract from there."""
        cnc_prof = get_profile("cnc_mill")
        fdm_prof = get_profile("fdm")

        def _get_comp(prof: dict) -> float:
            params = prof.get("params") or prof
            return float(params.get("hole_comp_mm") or 0.0)

        expected_cnc_comp = _get_comp(cnc_prof)
        expected_fdm_comp = _get_comp(fdm_prof)

        cnc_plan = self._bore_plan("cnc_mill")
        fdm_plan = self._bore_plan("fdm")

        for item in cnc_plan:
            self.assertAlmostEqual(item["hole_comp_mm"], expected_cnc_comp, places=6,
                                   msg="CNC hole_comp must match process_profiles")
        for item in fdm_plan:
            self.assertAlmostEqual(item["hole_comp_mm"], expected_fdm_comp, places=6,
                                   msg="FDM hole_comp must match process_profiles")

    # ── the lookup must fail loudly, never degrade to "no compensation" ──────────

    def test_unknown_process_profile_raises_teaching_error(self):
        """A process the DfM oracle does not know must raise, naming it AND the menu.

        Returning None/{} here is the defect class this repo is eliminating: the caller
        reads params.hole_comp_mm off it and either explodes with an AttributeError two
        frames later or — worse — plans an uncompensated hole that silently prints
        0.20 mm undersize.
        """
        from services.mcp.dimension_scheme_tools import _get_profile
        with self.assertRaises(ValueError) as ctx:
            _get_profile("lost_wax")
        msg = str(ctx.exception)
        self.assertIn("lost_wax", msg, "the error must name the requested process")
        for known in ("fdm", "cnc_mill"):
            self.assertIn(known, msg, "the error must list the available processes")

    def test_planner_does_not_swallow_an_unresolvable_process(self):
        """_plan_from_entities must propagate the teaching error, not plan comp=0.0.

        A silent 0.0 is indistinguishable from a legitimate CNC plan, so an agent asking
        for a misspelled process would get a confidently wrong scheme.
        """
        entities = _make_bore_sketch(self.NOMINAL_MM)
        with self.assertRaises(ValueError) as ctx:
            _plan_from_entities(entities, "lost_wax", it_grade=9,
                                nominal_fallback_mm=self.NOMINAL_MM)
        self.assertIn("lost_wax", str(ctx.exception))

    def test_offline_mode_still_plans_with_zero_compensation(self):
        """With NO engine bridge the planner degrades (comp 0.0) instead of raising —
        that offline path is this module's documented fixture mode and must survive the
        loud-lookup change above."""
        import services.mcp.core_bridge as cb
        with unittest.mock.patch.object(cb, "available", lambda: False):
            plan = _plan_from_entities(_make_bore_sketch(self.NOMINAL_MM), "fdm",
                                       it_grade=12, nominal_fallback_mm=self.NOMINAL_MM)
        self.assertTrue(plan, "offline mode must still emit a plan")
        for item in plan:
            self.assertAlmostEqual(item["hole_comp_mm"], 0.0, places=6)


class TestSketchHealthText(unittest.TestCase):
    """DoD 4 (offline portion): sketch_health_text() renders the three key states.

    The live-bridge tests (≥3 states against real engine) are in test_gsl_tools.py.
    These offline tests verify the renderer logic using synthetic health dicts.
    """

    def _make_health(self, *, dof: int = 0, status: str = "full",
                     open_endpoints: list | None = None,
                     mat_regions: int | None = None,
                     conflicting: list | None = None,
                     redundant: list | None = None,
                     schedule: list | None = None,
                     dof_narration: str = "") -> dict:
        closure: dict = {"open_endpoints": open_endpoints or [],
                         "closed_chains_ok": not bool(open_endpoints)}
        if mat_regions is not None:
            closure["material_regions"] = mat_regions
        return {
            "ok": True,
            "status": status,
            "dof": dof,
            "dof_narration": dof_narration,
            "closure": closure,
            "conflicting": conflicting or [],
            "redundant": redundant or [],
            "schedule": schedule or [],
            "free_variables": [],
        }

    def _text(self, **kw) -> str:
        from services.mcp.gsl_tools import sketch_health_text
        return sketch_health_text(self._make_health(**kw))

    # ── State A: fully constrained + closed ──────────────────────────────────────

    def test_state_a_fully_defined(self):
        """State A: closed profile, DOF 0 → reads 'CLOSED' and 'fully constrained'."""
        t = self._text(dof=0, status="full", open_endpoints=[], mat_regions=1)
        self.assertIn("CLOSED", t)
        self.assertIn("1 material region", t)
        self.assertIn("fully constrained", t)
        self.assertNotIn("OPEN", t)
        self.assertNotIn("CONFLICT", t)

    # ── State B: open profile + underconstrained ─────────────────────────────────

    def test_state_b_open_underconstrained(self):
        """State B: open profile + DOF>0 → reports dangling endpoints + DOF narration + schedule."""
        sched = [
            {"order": 1, "kind": "geometric", "action": "coincident",
             "why": "close the open chain", "handles": ["e1.p2", "e2.p1"]},
            {"order": 2, "kind": "dimension", "action": "add driving dimension",
             "why": "e1 is under-constrained in x, y", "entity": "e1"},
        ]
        t = self._text(dof=3, status="under",
                       open_endpoints=["e1.p2", "e2.p1"],
                       dof_narration="line e1 x, y; line e2 x",
                       schedule=sched)
        self.assertIn("OPEN", t)
        self.assertIn("e1.p2", t)
        self.assertIn("e2.p1", t)
        self.assertIn("sketch_auto_constrain", t)
        self.assertIn("DOF: 3", t)
        self.assertIn("line e1 x, y; line e2 x", t)
        # Schedule should list both steps
        self.assertIn("1. [geometric] coincident", t)
        self.assertIn("2. [dimension] add driving dimension", t)
        self.assertIn("close the open chain", t)

    # ── State C: conflicting / over-defined ──────────────────────────────────────

    def test_state_c_conflicting(self):
        """State C: conflicting constraints → reports OVER-CONSTRAINED + lists conflicts."""
        t = self._text(dof=-1, status="over",
                       conflicting=[{"id": "c5", "type": "distance",
                                     "suggestion": "remove conflicting constraint c5 (distance)"}])
        self.assertIn("OVER-CONSTRAINED", t)
        self.assertIn("CONFLICT c5", t)
        self.assertIn("distance", t)
        self.assertIn("remove conflicting constraint", t)

    # ── Schedule handles-vs-entity rendering ─────────────────────────────────────

    def test_schedule_handles_rendered(self):
        """Schedule step with handles list renders the handles inline."""
        sched = [{"order": 1, "kind": "geometric", "action": "coincident",
                  "why": "close chain", "handles": ["e3.p2", "e4.p1"]}]
        t = self._text(dof=1, status="under", schedule=sched)
        self.assertIn("e3.p2", t)
        self.assertIn("e4.p1", t)

    def test_empty_schedule_when_fully_defined(self):
        """No schedule emitted when DOF=0 and status=full → just says 'nothing to do'."""
        t = self._text(dof=0, status="full", schedule=[])
        self.assertIn("nothing to do", t)

    def test_redundant_constraint_listed(self):
        """Redundant constraints appear in the output with their id, type, suggestion."""
        t = self._text(dof=0, status="full",
                       redundant=[{"id": "c9", "type": "horizontal",
                                   "suggestion": "remove redundant constraint c9 (horizontal)"}])
        self.assertIn("REDUNDANT c9", t)
        self.assertIn("horizontal", t)

    # ── Redundancy economy (live 2026-07-30) ─────────────────────────────────────

    def test_all_identical_schedule_rows_collapse_to_one(self):
        """Live 2026-07-30, 3-rectangle F profile (12 lines, dof 40): the schedule printed
        12 rows whose kind, action and why were identical apart from the entity id —
        ~1150 chars carrying one instruction and a list of 12 entities. It must collapse
        into ONE row that names the count, the order SPAN and the entities, so a model
        applying the schedule still knows it stands for steps 2..13."""
        sched = [{"order": i + 2, "kind": "dimension",
                  "action": "add driving dimension / geometric relation for e%d" % (i + 1),
                  "why": "e%d is under-constrained in p1 x, p1 y, p2 x, p2 y" % (i + 1),
                  "entity": "e%d" % (i + 1)}
                 for i in range(12)]
        t = self._text(dof=40, status="under", mat_regions=1, schedule=sched)
        rows = [ln for ln in t.split("\n") if ln.startswith("  ") and "[dimension]" in ln]
        self.assertEqual(len(rows), 1, t)
        row = rows[0]
        self.assertIn("x12", row)          # the count survives
        self.assertIn("2-13.", row)        # the order span survives
        self.assertIn("e1", row)
        self.assertIn("+4", row)           # 8 named + "… +4"
        self.assertIn("under-constrained in p1 x", row)  # the remedy survives, once

    def test_distinct_schedule_remedies_are_never_merged(self):
        """Collapsing is keyed on the remedy with the entity id factored out — two rows
        that ask for DIFFERENT things must both survive, in order."""
        sched = [
            {"order": 1, "kind": "geometric", "action": "coincident",
             "why": "close the open chain", "handles": ["e1.p2", "e2.p1"]},
            {"order": 2, "kind": "dimension", "action": "add driving dimension for e1",
             "why": "e1 is under-constrained in x", "entity": "e1"},
            {"order": 3, "kind": "dimension", "action": "add radius dimension for e2",
             "why": "e2 has a free radius", "entity": "e2"},
        ]
        t = self._text(dof=4, status="under", schedule=sched)
        self.assertIn("1. [geometric] coincident", t)
        self.assertIn("2. [dimension] add driving dimension for e1", t)
        self.assertIn("3. [dimension] add radius dimension for e2", t)

    def test_dof_narration_is_bounded_but_still_names_the_count(self):
        """The under-constrained signal is real and must survive — the 40-slot ENUMERATION
        is what does not. Keep dof, status and a bounded sample; say how many were elided."""
        narration = "; ".join("line e%d p1 x, p1 y, p2 x, p2 y" % (i + 1) for i in range(12))
        t = self._text(dof=40, status="under", mat_regions=1, dof_narration=narration)
        self.assertIn("DOF: 40 free", t)
        self.assertIn("line e1 p1 x", t)      # sample survives
        self.assertIn("+8 more", t)           # elision is counted, not silent
        self.assertNotIn("line e12", t)       # the tail is not enumerated



class TestDrawingDimensionScheme(unittest.TestCase):
    class Registry:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def decorate(function):
                self.tools[function.__name__] = function
                return function
            return decorate

    def _tools(self):
        registry = self.Registry()
        register_dimension_scheme_tools(registry)
        return registry.tools

    def test_unknown_and_omitted_process_are_refused_with_valid_list(self):
        for process, reason in ((None, "process_required"), ("lost_wax", "unknown_process")):
            resolved, error = _process_verdict(process, "suggest_drawing_dimension_scheme")
            self.assertIsNone(resolved)
            self.assertEqual(error["reason"], reason)
            self.assertIn("cnc_mill", error["valid_processes"])
            self.assertIn("fdm", error["valid_processes"])

    def test_sketch_entry_point_no_longer_falls_back_to_fdm(self):
        result = json.loads(self._tools()["suggest_dimension_scheme"](
            doc_id="doc-1", sketch_id="sk1", process="lost_wax"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unknown_process")
        self.assertNotEqual(result.get("process"), "fdm")

    def test_drawing_entry_point_reads_hole_semantics_and_requires_process(self):
        features = [{
            "id": "f-hole", "name": "Clamp threads", "type": "hole",
            "params": {"style": "tap", "thread": "M6x1.0", "depth": 14,
                       "end": "blind", "positions": [[-10, -10], [10, -10],
                                                     [10, 10], [-10, 10]]},
        }]
        plan = _drawing_plan_from_features(features, "cnc_mill", 7)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["annotation_kind"], "thread")
        self.assertEqual(plan[0]["suggested_tool"], "add_hole_callout")
        self.assertEqual(plan[0]["thread"], "M6x1.0")
        self.assertEqual(plan[0]["depth"], 14.0)

    def test_drawing_entry_point_returns_process_specific_feature_plan(self):
        import services.mcp.core_bridge as core_bridge

        def fake_call(method, params=None):
            if method == "dfm_known_processes":
                return {"ok": True, "result": [
                    "fdm", "cnc_mill", "lathe", "sheet_metal", "laser", "injection"]}
            if method == "doc_list":
                return {"docs": [{"doc_id": "doc-1", "name": "Plate"}]}
            if method == "doc_list_features":
                return {"features": [{
                    "id": "f-box", "name": "Stock", "type": "box",
                    "params": {"length": 60, "width": 40, "height": 10},
                }]}
            raise AssertionError(method)

        with unittest.mock.patch.object(core_bridge, "call", fake_call):
            result = json.loads(self._tools()["suggest_drawing_dimension_scheme"](
                doc_id="doc-1", process="cnc_mill"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["process"], "cnc_mill")
        self.assertEqual(result["it_grade"], "IT7")
        self.assertEqual({item["source_param"] for item in result["items"]},
                         {"length", "width", "height"})
        self.assertTrue(all(item["default_tolerance"]["it_grade"] == "IT7"
                            for item in result["items"]))

if __name__ == "__main__":
    unittest.main()
