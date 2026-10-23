"""Tests for two-division bench + Discipline Index + inject hook + leaderboard keys.

Covers the division registry, Discipline Index, instrumentation, leaderboard keys,
GAP-INJECT and the GAP-CP-EVENT reader.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tools.flywheel.bench.gym.challenges import (
    Challenge, AcceptanceCheck, load_challenges, load_challenge,
    tools_for_division, assert_division_eyes_allowed, VISION_TOOLS,
    _CHALLENGE_MODULES,
)
from tools.flywheel.bench.gym.runner import (
    run_cell, run_cycle, execution_probe, apply_inject_faults,
    default_tools_for_division, CellResult,
)
from tools.flywheel.bench.gym import report as report_mod
from tools.flywheel.bench.gym.report import compute_discipline_index, compute_instrumentation
from tools.flywheel.bench.gym.leaderboard import (
    aggregate, validate_leaderboard, SCHEMA, TASK_SUITE, discover_cells,
)
from tools.flywheel.bench.gym.taxonomy import misuse_fingerprints, misuse_error_class


# ---------------------------------------------------------------------------
# Registry + Challenge fields
# ---------------------------------------------------------------------------

class TestRegistryAndChallengeFields:
    def test_c9_c15_pre_registered(self):
        names = set(_CHALLENGE_MODULES)
        for mod in (
            "c9_edit_from_ref", "c10_fix_interference", "c11_datasheet_dims",
            "c12_undo_recovery", "c13_sketch_repair", "c14_interface_layout",
            "c15_ambiguity_clarify",
        ):
            assert mod in names

    def test_load_challenges_skips_missing_c9_plus(self):
        # C1–C8 always present; C9–C15 import-guarded
        chs = load_challenges()
        ids = {c.id for c in chs}
        assert "C1" in ids and "C8" in ids
        # missing modules must not raise
        assert isinstance(chs, list)

    def test_c1_defaults_dual_division_calibration(self):
        c1 = load_challenge("C1")
        assert "local" in c1.divisions and "open" in c1.divisions
        assert c1.calibration is True
        assert c1.eyes == "either"
        assert c1.inject_faults == []

    def test_challenge_accepts_division_kwargs(self):
        def _g(ledger, ctx):
            return "PASS", "ok"
        ch = Challenge(
            id="CX", name="x", lane="test", request="r",
            manifest={}, checks=[AcceptanceCheck("AC1", "t", _g)],
            divisions=("open",), calibration=False, eyes="vision",
            inject_faults=[{"after_build_calls": 1, "fault": "bad"}],
        )
        assert ch.divisions == ("open",)
        assert ch.eyes == "vision"
        assert ch.inject_faults[0]["fault"] == "bad"


# ---------------------------------------------------------------------------
# Division scheduling / refusal
# ---------------------------------------------------------------------------

class TestDivisionScheduling:
    def test_tools_for_division_strips_vision_on_local(self):
        tools = ["describe_part", "render_part", "inspect_part", "measure_part"]
        local = tools_for_division("local", tools)
        open_ = tools_for_division("open", tools)
        assert "render_part" not in local
        assert "inspect_part" not in local
        assert "describe_part" in local
        assert set(open_) == set(tools)
        assert VISION_TOOLS.isdisjoint(local)

    def test_assert_vision_eyes_refuses_local(self):
        def _g(ledger, ctx):
            return "PASS", "ok"
        ch = Challenge(
            id="CV", name="vision-only", lane="test", request="r",
            manifest={}, checks=[AcceptanceCheck("AC1", "t", _g)],
            divisions=("open",), eyes="vision",
        )
        with pytest.raises(ValueError, match="vision"):
            assert_division_eyes_allowed(ch, "local")
        assert_division_eyes_allowed(ch, "open")  # no raise

    def test_run_cell_emits_division(self, tmp_path):
        probe = execution_probe()
        ch = load_challenge("C1")
        cell = run_cell(ch, "local-24k", 1, cycle="div-test", probe=probe,
                        runs_dir=tmp_path, division="local")
        assert cell.division == "local"
        d = cell.to_dict()
        assert d["division"] == "local"
        # LOCAL tools_offered never includes vision
        assert VISION_TOOLS.isdisjoint(set(cell.tools_offered))
        assert "describe_part" in cell.tools_offered

    def test_run_cell_open_keeps_vision_tools(self, tmp_path):
        probe = execution_probe()
        ch = load_challenge("C1")
        cell = run_cell(ch, "frontier-200k", 1, cycle="div-test", probe=probe,
                        runs_dir=tmp_path, division="open")
        assert cell.division == "open"
        assert "render_part" in cell.tools_offered

    def test_run_cycle_filters_by_division(self, tmp_path):
        cr = run_cycle(
            cycle="div-matrix", tiers=["local-24k"],
            challenge_ids=["C1"], divisions=["local"],
            runs_dir=tmp_path,
        )
        assert cr.cells
        assert all(c.division == "local" for c in cr.cells)

    def test_run_cycle_skips_unsupported_division(self, tmp_path):
        # force open-only challenge
        def _g(ledger, ctx):
            return "PASS", "ok"
        # Use real C1 but schedule only open with local tier via divisions filter
        # and a synthetic open-only challenge is not in registry — instead verify
        # vision-eyes skip path via monkeypatch-like local Challenge in run_cell.
        ch = Challenge(
            id="CV", name="v", lane="t", request="r",
            manifest={"deliverables": [], "request_verbatim": "r"},
            checks=[AcceptanceCheck("AC1", "t", _g)],
            divisions=("open",), eyes="vision", seeds=(1,),
            scripts={1: {"DL1": []}},
        )
        probe = execution_probe()
        with pytest.raises(ValueError):
            run_cell(ch, "local-24k", 1, cycle="x", probe=probe,
                     runs_dir=tmp_path, division="local")


# ---------------------------------------------------------------------------
# Discipline Index + GAP-CP-EVENT reader
# ---------------------------------------------------------------------------

def _ledger(events: list[dict]) -> dict:
    out = []
    for i, e in enumerate(events):
        out.append({
            "seq": e.get("seq", i),
            "ts": "t",
            "actor": e.get("actor", "feature_modeler"),
            "kind": e["kind"],
            "payload": e.get("payload") or {},
        })
    return {"events": out}


class TestDisciplineIndex:
    def test_all_components_pass(self):
        ledger = _ledger([
            {"kind": "artifact", "payload": {"kind": "battery", "verdict": "PASS"}},
            {"kind": "artifact", "payload": {"kind": "interference", "clashes": 0}},
            {"kind": "tool_call", "payload": {"tool": "check_interference", "ok": True}},
            {"kind": "auto_checkpoint", "payload": {"trigger_tool": "boolean"}},
            {"kind": "hard_guardrail", "payload": {
                "name": "auto_checkpoint", "outcome": "passed", "reason": "ok"}},
            {"kind": "tool_call", "payload": {"tool": "boolean", "ok": True}},
            {"kind": "tool_call", "payload": {"tool": "sketch_health", "ok": True}},
            {"kind": "artifact", "payload": {"kind": "sketch_status", "dof": 0}},
        ])
        di = compute_discipline_index(
            ledger,
            mission={"stop_reason": "done", "status": "ok"},
            graded={"passed": True, "criteria": []},
            classifications=[],
        )
        assert di["n_applicable"] >= 4
        assert di["score"] == 1.0
        assert di["components"]["H1_verify_before_done"] == 1.0
        assert di["components"]["H4_checkpoint_before_destructive"] == 1.0
        assert di["components"]["H5_referential_integrity"] == 1.0
        assert di["components"]["H6_hard_invariant_clean"] == 1.0

    def test_na_skips_h3_h4(self):
        # part-only, no destructive ops, no assembly → H3/H4 N/A
        ledger = _ledger([
            {"kind": "artifact", "payload": {"kind": "battery", "verdict": "PASS"}},
            {"kind": "tool_call", "payload": {"tool": "add_features", "ok": True}},
        ])
        di = compute_discipline_index(
            ledger, mission={"status": "ok"}, graded={"passed": True},
            classifications=[],
        )
        assert di["components"]["H3_interference_if_assembly"] is None
        assert di["components"]["H4_checkpoint_before_destructive"] is None
        # score is mean of applicable only
        assert di["n_applicable"] == sum(
            1 for v in di["components"].values() if v is not None
        )
        assert di["score"] == 1.0  # H1=1 H5=1 H6=1 (H2 N/A too without sketch)

    def test_h5_context_loss_zeroes(self):
        ledger = _ledger([
            {"kind": "artifact", "payload": {"kind": "battery", "verdict": "PASS"}},
        ])
        di = compute_discipline_index(
            ledger, classifications=[{"failure_class": "context_loss"}],
        )
        assert di["components"]["H5_referential_integrity"] == 0.0
        assert di["score"] < 1.0

    def test_h4_reads_auto_checkpoint_event(self):
        # GAP-CP-EVENT: auto_checkpoint kind before destructive
        ledger = _ledger([
            {"kind": "auto_checkpoint", "payload": {"trigger_tool": "cut"}},
            {"kind": "tool_call", "payload": {"tool": "cut", "ok": True}},
            {"kind": "artifact", "payload": {"kind": "battery", "verdict": "PASS"}},
        ])
        di = compute_discipline_index(ledger, classifications=[])
        assert di["components"]["H4_checkpoint_before_destructive"] == 1.0

    def test_h4_fraction_partial(self):
        ledger = _ledger([
            {"kind": "auto_checkpoint", "payload": {"trigger_tool": "boolean"}},
            {"kind": "tool_call", "payload": {"tool": "boolean", "ok": True}},
            # second destructive without checkpoint after first still has prior cp
            {"kind": "tool_call", "payload": {"tool": "delete_feature", "ok": True}},
            {"kind": "tool_call", "payload": {"tool": "cut", "ok": True}},
        ])
        # all three have a prior checkpoint_idx at 0 → fraction 1.0
        di = compute_discipline_index(ledger, classifications=[])
        assert di["components"]["H4_checkpoint_before_destructive"] == 1.0

        ledger2 = _ledger([
            {"kind": "tool_call", "payload": {"tool": "cut", "ok": True}},  # no cp
            {"kind": "auto_checkpoint", "payload": {"trigger_tool": "boolean"}},
            {"kind": "tool_call", "payload": {"tool": "boolean", "ok": True}},
        ])
        di2 = compute_discipline_index(ledger2, classifications=[])
        assert di2["components"]["H4_checkpoint_before_destructive"] == 0.5

    def test_h6_verify_failed_battery(self):
        ledger = _ledger([
            {"kind": "artifact", "payload": {"kind": "battery", "verdict": "FAIL"}},
        ])
        di = compute_discipline_index(
            ledger,
            mission={"stop_reason": "verify_failed:AC1", "status": "failed"},
            classifications=[],
        )
        assert di["components"]["H6_hard_invariant_clean"] == 0.0


# ---------------------------------------------------------------------------
# GAP-INJECT hook
# ---------------------------------------------------------------------------

class TestInjectFaults:
    def test_apply_inject_after_n_build_calls(self, tmp_path):
        from services.agent.ledger import Ledger

        def _g(ledger, ctx):
            return "PASS", "ok"

        ch = Challenge(
            id="C12x", name="inject", lane="t", request="r",
            manifest={}, checks=[AcceptanceCheck("AC1", "t", _g)],
            inject_faults=[{
                "after_build_calls": 2,
                "fault": "bad_extrude",
                "feature": {"op": "extrude", "depth_mm": -999, "name": "corrupt"},
                "note": "inject corrupt extrude",
            }],
        )
        led = Ledger("inject-test", path=tmp_path / "inject-test.json")
        # 2 build mutating tool_calls
        led.append("m", "tool_call", {"tool": "add_features", "ok": True})
        led.append("m", "tool_call", {"tool": "extrude", "ok": True})
        applied = apply_inject_faults(led, ch)
        assert len(applied) == 1
        assert applied[0]["fault"] == "bad_extrude"
        kinds = [e["kind"] for e in led.events]
        assert "tool_call" in kinds
        # injected artifact present
        arts = [e for e in led.events
                if e["kind"] == "artifact"
                and (e.get("payload") or {}).get("kind") == "injected_fault"]
        assert arts

    def test_inject_skipped_when_build_count_low(self, tmp_path):
        from services.agent.ledger import Ledger

        def _g(ledger, ctx):
            return "PASS", "ok"
        ch = Challenge(
            id="C12y", name="inject", lane="t", request="r",
            manifest={}, checks=[AcceptanceCheck("AC1", "t", _g)],
            inject_faults=[{"after_build_calls": 5, "fault": "late"}],
        )
        led = Ledger("inject-test2", path=tmp_path / "inject-test2.json")
        led.append("m", "tool_call", {"tool": "add_features", "ok": True})
        assert apply_inject_faults(led, ch) == []


# ---------------------------------------------------------------------------
# Misuse fingerprints helper
# ---------------------------------------------------------------------------

class TestMisuseFingerprints:
    def test_bucket_regex(self):
        assert misuse_error_class("got an unexpected keyword argument 'foo'") == "bad-arg"
        assert misuse_error_class("face id does not exist") == "wrong-face-ref"
        assert misuse_error_class("must run verify_battery first") == "verify-skipped"
        assert misuse_error_class("something else entirely") == "other"

    def test_cluster_from_ledger(self):
        ledger = _ledger([
            {"kind": "tool_call", "payload": {
                "tool": "extrude", "ok": False, "error": "bad arg: depth"}},
            {"kind": "tool_call", "payload": {
                "tool": "extrude", "ok": False, "error": "invalid arg"}},
            {"kind": "tool_call", "payload": {
                "tool": "fillet", "ok": False, "error": "unknown feature id"}},
        ])
        fps = misuse_fingerprints(ledger)
        assert fps
        assert fps[0]["fingerprint"] == "extrude:bad-arg"
        assert fps[0]["count"] == 2


# ---------------------------------------------------------------------------
# Instrumentation block
# ---------------------------------------------------------------------------

class TestInstrumentation:
    def test_compute_instrumentation_shape(self):
        cells = [{
            "challenge": "C1", "tier": "local-24k", "seed": 1,
            "division": "local", "passed": True,
            "graded": {"passed": True, "criteria": []},
            "classifications": [],
            "ledger": _ledger([
                {"kind": "artifact", "payload": {"kind": "battery", "verdict": "PASS"}},
                {"kind": "tool_call", "payload": {"tool": "describe_part", "ok": True}},
                {"kind": "tool_call", "payload": {
                    "tool": "add_features", "ok": True, "args": {"a": 1}}},
                {"kind": "tool_call", "payload": {"tool": "describe_part", "ok": True}},
                {"kind": "tool_call", "payload": {
                    "tool": "fillet", "ok": True, "args": {"r": 2}}},
            ]),
            "mission": {"phases_completed": ["intake", "specify", "plan", "build"],
                        "stop_reason": "done", "status": "ok", "phase": "deliver"},
        }]
        inst = compute_instrumentation(cells)
        for key in ("phase_mortality", "misuse_fingerprints", "verify_efficacy",
                    "budget_curves", "checkpoint_util", "discipline_vs_success",
                    "discipline_index"):
            assert key in inst
        assert "local" in inst["discipline_index"]
        # GAP-USAGE flag when no tokens
        assert inst["budget_curves"].get("gap") in ("GAP-USAGE", "GAP-USAGE-partial", None)

    def test_build_report_includes_instrumentation(self, tmp_path):
        cr = run_cycle(
            cycle="inst-rep", tiers=["local-24k"], challenge_ids=["C1"],
            divisions=["local"], runs_dir=tmp_path,
        )
        # only seed 1 for speed — filter
        cr.cells = [c for c in cr.cells if c.seed == 1]
        rep = report_mod.build_report(cr)
        assert "instrumentation" in rep
        assert "discipline_index" in rep
        assert "phase_mortality" in rep["instrumentation"]
        assert "verify_efficacy" in rep["instrumentation"]


# ---------------------------------------------------------------------------
# Leaderboard additive keys
# ---------------------------------------------------------------------------

class TestLeaderboardAdditive:
    def test_task_suite_bumped(self):
        assert "C15" in TASK_SUITE or "C1..C15" in TASK_SUITE

    def test_aggregate_emits_division_keys(self):
        cells = [
            {
                "challenge": "C1", "tier": "local-24k", "model": "qwen3-4b",
                "mode": "mock", "seed": 1, "cycle": "c",
                "division": "local", "passed": True, "classifications": [],
                "discipline_index": 0.8, "ledger_path": "",
            },
            {
                "challenge": "C1", "tier": "local-24k", "model": "qwen3-4b",
                "mode": "mock", "seed": 2, "cycle": "c",
                "division": "local", "passed": False, "classifications": [
                    {"failure_class": "incoherent_geometry"}],
                "discipline_index": 0.4, "ledger_path": "",
            },
            {
                "challenge": "C1", "tier": "openrouter:anthropic/claude-opus-4",
                "model": "anthropic/claude-opus-4", "mode": "mock",
                "seed": 1, "cycle": "c", "division": "open", "passed": True,
                "classifications": [], "discipline_index": 0.9, "ledger_path": "",
            },
        ]
        doc = aggregate(
            cells=cells,
            cycle_reports=[{
                "cycle": "c",
                "tiers": ["local-24k", "openrouter:anthropic/claude-opus-4"],
                "execution_mode": "mock",
                "discipline_index": {"local": 0.6, "open": 0.9, "calibration_delta": -0.3},
                "instrumentation": {
                    "phase_mortality": {"rates": {"specify": 0.6}},
                    "misuse_fingerprints": [{"fingerprint": "extrude:bad-arg", "count": 2}],
                    "verify_efficacy": {"rate": 0.5},
                    "checkpoint_util": {"overall": 0.25},
                    "summary": {
                        "phase_mortality_top": {"specify": 0.6},
                        "top_misuse_fingerprint": "extrude:bad-arg",
                    },
                },
            }],
            diag=None,
            force_mock=True,
        )
        errs = validate_leaderboard(doc)
        assert errs == [], errs
        assert "C15" in doc["task_suite"] or "C1..C15" in doc["task_suite"]
        assert doc["task_suite"].endswith("-MOCK")

        local = next(m for m in doc["models"]
                     if m["kind"] == "local" and m["primary"]["cells_total"] > 0)
        assert "divisions" in local
        assert "local" in local["divisions"]
        assert "open" in local["divisions"]
        assert local["divisions"]["local"]["cells_total"] == 2
        assert "discipline_index" in local["divisions"]["local"]
        assert "calibration" in local
        assert "bridge_delta" in local["calibration"]
        assert "discipline_index" in local["axes"]
        assert "verify_efficacy" in local["axes"]
        assert "checkpoint_util" in local["axes"]
        assert local["axes"]["discipline_index"] is not None
        assert "instrumentation_summary" in local
        assert local["instrumentation_summary"]["top_misuse_fingerprint"] == "extrude:bad-arg"
        # per_challenge carries division
        assert all("division" in pc for pc in local["per_challenge"])

    def test_old_leaderboard_still_loads(self):
        """Old committed leaderboard.json (no additive keys) still parses."""
        path = Path("benchmarks/leaderboard/leaderboard.json")
        if not path.is_file():
            pytest.skip("no committed leaderboard")
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc.get("schema") == SCHEMA
        # validate is for producer shape; consumers tolerate missing additive keys.
        # Ensure we can re-aggregate without crash.
        assert isinstance(doc.get("models"), list)

    def test_discover_cells_reads_persisted_metadata(self, tmp_path):
        run = tmp_path / "runs" / "cyc" / "lossy-portable-name"
        run.mkdir(parents=True)
        (run / "cell.json").write_text(json.dumps({
            "challenge": "C1", "tier": "local-24k", "model": "qwen3-4b",
            "seed": 1, "division": "local", "cycle": "cyc", "mode": "mock",
            "run_id": "gym_C1_local_local-24k_s1", "passed": True,
            "graded": {"passed": True, "criteria": []}, "classifications": [],
        }))
        cells = discover_cells(tmp_path, cycles={"cyc"})
        assert len(cells) == 1
        assert cells[0]["division"] == "local"
        assert cells[0]["challenge"] == "C1"
        assert cells[0]["tier"] == "local-24k"

    def test_discover_cells_ignores_legacy_unidentified_grades(self, tmp_path):
        run = tmp_path / "runs" / "cyc" / "C1_local-24k_s1"
        run.mkdir(parents=True)
        (run / "graded.json").write_text(json.dumps({
            "challenge": "C1", "passed": False, "criteria": [],
        }))
        assert discover_cells(tmp_path, cycles={"cyc"}) == []
