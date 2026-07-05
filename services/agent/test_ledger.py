"""Tests for services/agent/ledger.py — aicad.ledger/1.

DoD coverage:
  - append / replay round-trip: state reconstructible from events alone
  - ledger_render determinism + budget compliance at 200/800/4000 tokens
  - provenance required on dimension events
  - MCP tool smoke test (ledger_get / ledger_note)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Path / import bootstrap (no install required; works from repo root)
# ---------------------------------------------------------------------------
_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# Use a temp cache root so tests never touch real user data
_TMP_CACHE = tempfile.mkdtemp(prefix="test_ledger_cache_")
os.environ["AICAD_CACHE_ROOT"] = _TMP_CACHE

from services.agent.ledger import (  # noqa: E402
    SCHEMA,
    Ledger,
    _materialize,
    ledger_render,
    run_ledger_path,
)

_CHARS_PER_TOKEN = 4  # must match ledger.py


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _fresh(suffix: str = "") -> tuple[str, Path]:
    """Return (run_id, ledger_path) for a throwaway ledger."""
    run_id = f"test_run_{suffix}_{os.getpid()}"
    path = Path(_TMP_CACHE) / "runs" / run_id / "ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return run_id, path


# ---------------------------------------------------------------------------
# §1  Schema / initial state
# ---------------------------------------------------------------------------

class TestLedgerSchema(unittest.TestCase):
    def test_empty_ledger_has_correct_schema(self):
        run_id, path = _fresh("schema")
        ledger = Ledger(run_id, path)
        self.assertEqual(ledger.state["schema"], SCHEMA)
        self.assertEqual(ledger.state["schema"], "aicad.ledger/1")
        self.assertEqual(ledger.state["run_id"], run_id)
        self.assertIsInstance(ledger.state["brief"], dict)
        for section in ("requirements", "decisions", "dimensions",
                        "questions", "stage_log", "artifacts"):
            self.assertIsInstance(ledger.state[section], list)

    def test_events_start_empty(self):
        run_id, path = _fresh("events")
        ledger = Ledger(run_id, path)
        self.assertEqual(ledger.events, [])


# ---------------------------------------------------------------------------
# §2  Append / replay round-trip
# ---------------------------------------------------------------------------

class TestAppendReplay(unittest.TestCase):
    def _populate(self, ledger: Ledger) -> None:
        ledger.set_brief("human", "Shaft Coupler", "Design a shaft coupler for Ø20 shafts",
                         ["bore fits H7/g6", "length <= 60 mm"])
        ledger.add_requirement("human", "R1", "Bore fit H7/g6 on Ø20", source="user")
        ledger.add_decision("foreman", "D1", "layout",
                            "Use H7/g6 sliding fit", "standard fit for moderate-speed shafts")
        ledger.upsert_dimension("foreman", "DIM1", "bore_diameter", 20.02, "mm",
                                "sliding fit on shaft S1 (H7/g6)",
                                provenance="ISO 286 H7 table",
                                tolerance={"fit": "H7", "upper": 0.021, "lower": 0.0},
                                status="confirmed")
        ledger.add_question("human", "Q1", "What is the shaft material?", blocking=False)
        ledger.log_stage("foreman", "feature", entered="2026-07-04T10:00:00Z")
        ledger.add_artifact("loop", "render", "art_abc123", "/tmp/part_grid.png",
                            stage="feature", note="iter=0")

    def test_event_count(self):
        run_id, path = _fresh("count")
        ledger = Ledger(run_id, path)
        self._populate(ledger)
        self.assertEqual(len(ledger.events), 7)

    def test_seq_assigned_sequentially(self):
        run_id, path = _fresh("seq")
        ledger = Ledger(run_id, path)
        self._populate(ledger)
        seqs = [e["seq"] for e in ledger.events]
        self.assertEqual(seqs, list(range(7)))

    def test_replay_state_equals_materialized(self):
        """Core DoD: state rebuilt from events equals the live state."""
        run_id, path = _fresh("replay")
        ledger = Ledger(run_id, path)
        self._populate(ledger)

        # Rebuild state from events ONLY (drop the materialized state)
        replayed = _materialize(ledger.events, run_id)

        self.assertEqual(replayed["schema"], ledger.state["schema"])
        self.assertEqual(replayed["brief"], ledger.state["brief"])
        self.assertEqual(replayed["decisions"], ledger.state["decisions"])
        self.assertEqual(replayed["dimensions"], ledger.state["dimensions"])
        self.assertEqual(replayed["requirements"], ledger.state["requirements"])
        self.assertEqual(replayed["questions"], ledger.state["questions"])
        self.assertEqual(replayed["stage_log"], ledger.state["stage_log"])
        self.assertEqual(replayed["artifacts"], ledger.state["artifacts"])

    def test_reload_from_disk_replay(self):
        """Loading from disk rebuilds state from events (round-trip through JSON)."""
        run_id, path = _fresh("disk_reload")
        ledger = Ledger(run_id, path)
        self._populate(ledger)

        # Load fresh instance from disk
        ledger2 = Ledger.load(run_id, path)
        self.assertEqual(len(ledger2.events), 7)
        self.assertEqual(ledger2.state["brief"]["title"], "Shaft Coupler")
        self.assertEqual(ledger2.state["decisions"][0]["id"], "D1")
        # Replay: drop stored state, rebuild, compare
        replayed = _materialize(ledger2.events, run_id)
        self.assertEqual(replayed["dimensions"], ledger2.state["dimensions"])

    def test_stored_file_has_events_and_state(self):
        """ledger.json contains both 'events' and 'state' keys."""
        run_id, path = _fresh("file_structure")
        ledger = Ledger(run_id, path)
        ledger.set_brief("human", "Test", "desc", [])
        raw = json.loads(path.read_text())
        self.assertIn("events", raw)
        self.assertIn("state", raw)
        self.assertEqual(len(raw["events"]), 1)


# ---------------------------------------------------------------------------
# §3  Dimension provenance enforcement
# ---------------------------------------------------------------------------

class TestDimensionProvenance(unittest.TestCase):
    def test_missing_provenance_raises(self):
        run_id, path = _fresh("prov_missing")
        ledger = Ledger(run_id, path)
        with self.assertRaises(ValueError) as ctx:
            ledger.upsert_dimension(
                "foreman", "DIM1", "bore_diameter", 20.02, "mm",
                "sliding fit", provenance=""  # empty → should raise
            )
        self.assertIn("provenance", str(ctx.exception))

    def test_none_provenance_raises(self):
        run_id, path = _fresh("prov_none")
        ledger = Ledger(run_id, path)
        with self.assertRaises((ValueError, TypeError)):
            ledger.upsert_dimension(
                "foreman", "DIM1", "bore_diameter", 20.02, "mm",
                "sliding fit", provenance=None
            )

    def test_provenance_in_event_not_state(self):
        """Provenance lives on the event payload, not in the materialized state."""
        run_id, path = _fresh("prov_location")
        ledger = Ledger(run_id, path)
        ledger.upsert_dimension(
            "foreman", "DIM1", "bore_diameter", 20.02, "mm",
            "sliding fit", provenance="ISO 286 H7 table",
            tolerance={"fit": "H7", "upper": 0.021, "lower": 0.0}
        )
        # Event payload has provenance
        self.assertEqual(ledger.events[0]["payload"]["provenance"], "ISO 286 H7 table")
        # Materialized state dimension does NOT have provenance key
        dim = ledger.state["dimensions"][0]
        self.assertNotIn("provenance", dim)

    def test_valid_provenance_succeeds(self):
        run_id, path = _fresh("prov_valid")
        ledger = Ledger(run_id, path)
        ev = ledger.upsert_dimension(
            "foreman", "DIM1", "bore_diameter", 20.02, "mm",
            "sliding fit on shaft S1", provenance="calc: fit_calc(d=20, fit='H7/g6')"
        )
        self.assertEqual(ev["kind"], "dimension")
        self.assertEqual(ledger.state["dimensions"][0]["value"], 20.02)


# ---------------------------------------------------------------------------
# §4  ledger_render — determinism + budget compliance
# ---------------------------------------------------------------------------

class TestLedgerRender(unittest.TestCase):
    def _make_populated_ledger(self) -> Ledger:
        run_id, path = _fresh("render")
        ledger = Ledger(run_id, path)
        ledger.set_brief("human", "Widget", "Make a widget", ["fits in box", "weighs < 100g"])
        ledger.add_requirement("human", "R1", "Mass < 100g", source="user")
        ledger.add_decision("foreman", "D1", "layout", "Use aluminium 6061",
                            "good strength/weight ratio")
        ledger.upsert_dimension("foreman", "DIM1", "height", 50.0, "mm",
                                "fits in 55mm cavity", provenance="user answer")
        ledger.upsert_dimension("foreman", "DIM2", "width", 30.0, "mm",
                                "standard shelf width", provenance="table: DIN 645")
        ledger.add_question("human", "Q1", "What finish?", blocking=False)
        ledger.log_stage("foreman", "feature")
        ledger.add_artifact("loop", "render", "art_001", "/tmp/render.png", "feature")
        return ledger

    def test_render_contains_schema_header(self):
        ledger = self._make_populated_ledger()
        out = ledger_render(ledger, 4000)
        self.assertIn("[LEDGER schema=aicad.ledger/1", out)
        self.assertIn("[/LEDGER]", out)

    def test_render_deterministic(self):
        """Same ledger produces identical output on repeated calls."""
        ledger = self._make_populated_ledger()
        out1 = ledger_render(ledger, 800)
        out2 = ledger_render(ledger, 800)
        self.assertEqual(out1, out2)

    def test_render_200_token_budget(self):
        """Output fits within 200-token budget (chars)."""
        ledger = self._make_populated_ledger()
        out = ledger_render(ledger, 200)
        # +N more entries line is allowed; total chars <= budget * _CHARS_PER_TOKEN + slack
        budget_chars = 200 * _CHARS_PER_TOKEN
        # Allow a small slack for the [/LEDGER] footer line
        self.assertLessEqual(len(out), budget_chars + 50,
                             f"Render exceeded 200-token budget: {len(out)} chars")

    def test_render_800_token_budget(self):
        """At 800 tokens, a normally-sized ledger fits without truncation."""
        ledger = self._make_populated_ledger()
        out = ledger_render(ledger, 800)
        budget_chars = 800 * _CHARS_PER_TOKEN
        self.assertLessEqual(len(out), budget_chars + 50)

    def test_render_4000_token_budget_includes_all_sections(self):
        """Large budget: all non-empty sections appear."""
        ledger = self._make_populated_ledger()
        out = ledger_render(ledger, 4000)
        self.assertIn("Mission Brief", out)
        self.assertIn("Widget", out)
        self.assertIn("Decisions", out)
        self.assertIn("Dimensions", out)
        self.assertIn("Open Questions", out)
        self.assertIn("Requirements", out)

    def test_render_whole_entry_truncation(self):
        """When budget is tight, dropped sections show a (+N more entries) line."""
        ledger = self._make_populated_ledger()
        # Very tight budget — only brief should fit
        out = ledger_render(ledger, 50)
        if "(+" in out:
            self.assertIn("more entries", out)

    def test_render_priority_order_brief_first(self):
        """Brief appears before decisions in the output."""
        ledger = self._make_populated_ledger()
        out = ledger_render(ledger, 4000)
        brief_pos = out.find("Mission Brief")
        decision_pos = out.find("Decisions")
        if decision_pos != -1:
            self.assertLess(brief_pos, decision_pos)

    def test_render_questions_before_decisions(self):
        """Open questions appear before decisions (priority order)."""
        ledger = self._make_populated_ledger()
        out = ledger_render(ledger, 4000)
        q_pos = out.find("Open Questions")
        d_pos = out.find("Decisions")
        if q_pos != -1 and d_pos != -1:
            self.assertLess(q_pos, d_pos)


# ---------------------------------------------------------------------------
# §5  Upsert helpers (idempotency)
# ---------------------------------------------------------------------------

class TestUpsertHelpers(unittest.TestCase):
    def test_upsert_dimension_updates_existing(self):
        run_id, path = _fresh("upsert_dim")
        ledger = Ledger(run_id, path)
        ledger.upsert_dimension("foreman", "DIM1", "bore", 20.0, "mm",
                                "initial", provenance="user answer")
        ledger.upsert_dimension("foreman", "DIM1", "bore", 20.02, "mm",
                                "updated after calc", provenance="calc: fit_calc")
        # Two events, but only one dimension in state (upserted)
        self.assertEqual(len(ledger.events), 2)
        self.assertEqual(len(ledger.state["dimensions"]), 1)
        self.assertEqual(ledger.state["dimensions"][0]["value"], 20.02)

    def test_add_decision_upserts_by_id(self):
        run_id, path = _fresh("upsert_dec")
        ledger = Ledger(run_id, path)
        ledger.add_decision("foreman", "D1", "layout", "text v1", "rationale v1")
        ledger.add_decision("foreman", "D1", "layout", "text v2", "rationale v2")
        self.assertEqual(len(ledger.state["decisions"]), 1)
        self.assertEqual(ledger.state["decisions"][0]["text"], "text v2")

    def test_question_answer(self):
        run_id, path = _fresh("q_answer")
        ledger = Ledger(run_id, path)
        ledger.add_question("human", "Q1", "What is the material?")
        ledger.answer_question("foreman", "Q1", "Aluminium 6061")
        q = ledger.state["questions"][0]
        self.assertEqual(q["status"], "answered")
        self.assertEqual(q["answer"], "Aluminium 6061")


# ---------------------------------------------------------------------------
# §6  MCP tool smoke tests
# ---------------------------------------------------------------------------

class TestMCPTools(unittest.TestCase):
    """Smoke-test ledger_get and ledger_note without a live MCP server."""

    def setUp(self):
        # Register tools using the real view() so risk classification is exercised
        from services.mcp import tool_registry as reg

        class _FakeMCP:
            def __init__(self):
                self._tools = {}
            def tool(self, *a, **kw):
                def decorator(fn):
                    self._tools[fn.__name__] = fn
                    return fn
                return decorator

        self._fake_mcp = _FakeMCP()
        view = reg.view(self._fake_mcp, "ledger")
        from services.mcp.ledger_tools import register_ledger_tools
        register_ledger_tools(view)
        self._tools = self._fake_mcp._tools

    def test_tools_registered(self):
        self.assertIn("ledger_get", self._tools)
        self.assertIn("ledger_note", self._tools)

    def test_ledger_get_risk_is_read(self):
        from services.mcp.tool_registry import classify_risk, catalog
        # classify_risk should return "read" after our _READ_EXACT addition
        self.assertEqual(classify_risk("ledger_get"), "read")

    def test_ledger_note_risk_is_write(self):
        from services.mcp.tool_registry import classify_risk
        self.assertEqual(classify_risk("ledger_note"), "write")

    def test_ledger_get_returns_rendered(self):
        run_id, path = _fresh("mcp_get")
        # Pre-populate a ledger
        ledger = Ledger(run_id, path)
        ledger.set_brief("human", "MCP test", "test summary", [])

        fn = self._tools["ledger_get"]
        result = fn(run_id=run_id, budget_tokens=800)
        self.assertTrue(result.get("ok"), result)
        self.assertIn("rendered", result)
        self.assertIn("aicad.ledger/1", result["rendered"])
        self.assertEqual(result["event_count"], 1)

    def test_ledger_get_section_filter(self):
        run_id, path = _fresh("mcp_get_sec")
        ledger = Ledger(run_id, path)
        ledger.set_brief("human", "Section test", "summary", ["criterion 1"])

        fn = self._tools["ledger_get"]
        result = fn(run_id=run_id, section="brief")
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["section"], "brief")
        self.assertEqual(result["data"]["title"], "Section test")

    def test_ledger_get_invalid_section(self):
        run_id, path = _fresh("mcp_get_badsec")
        fn = self._tools["ledger_get"]
        result = fn(run_id=run_id, section="__invalid__")
        self.assertFalse(result.get("ok"))

    def test_ledger_note_decision(self):
        run_id, path = _fresh("mcp_note_dec")
        fn = self._tools["ledger_note"]
        result = fn(
            run_id=run_id,
            kind="decision",
            payload={"id": "D1", "stage": "layout", "text": "Use H7/g6",
                     "rationale": "standard fit"},
            actor="foreman",
        )
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["kind"], "decision")

    def test_ledger_note_dimension_requires_provenance(self):
        run_id, path = _fresh("mcp_note_dim_noprov")
        fn = self._tools["ledger_note"]
        result = fn(
            run_id=run_id,
            kind="dimension",
            payload={"id": "DIM1", "name": "bore", "value": 20.0, "unit": "mm",
                     "driver": "fit", "status": "proposed"},
            # no provenance key
        )
        self.assertFalse(result.get("ok"), f"Expected failure, got: {result}")
        self.assertIn("provenance", result.get("error", ""))

    def test_ledger_note_dimension_with_provenance(self):
        run_id, path = _fresh("mcp_note_dim_prov")
        fn = self._tools["ledger_note"]
        result = fn(
            run_id=run_id,
            kind="dimension",
            payload={"id": "DIM1", "name": "bore", "value": 20.02, "unit": "mm",
                     "driver": "H7/g6 sliding fit", "status": "proposed",
                     "provenance": "ISO 286 H7 table"},
        )
        self.assertTrue(result.get("ok"), result)

    def test_ledger_note_invalid_kind(self):
        run_id, path = _fresh("mcp_note_bad_kind")
        fn = self._tools["ledger_note"]
        result = fn(run_id=run_id, kind="__bogus__", payload={})
        self.assertFalse(result.get("ok"))

    def test_ledger_note_persists_and_get_reflects(self):
        """ledger_note write followed by ledger_get read reflects the change."""
        run_id, path = _fresh("mcp_round_trip")
        note_fn = self._tools["ledger_note"]
        get_fn = self._tools["ledger_get"]

        note_fn(run_id=run_id, kind="brief",
                payload={"title": "Round-trip test", "summary": "s", "success_criteria": []})
        result = get_fn(run_id=run_id, budget_tokens=800)
        self.assertTrue(result.get("ok"))
        self.assertIn("Round-trip test", result["rendered"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
