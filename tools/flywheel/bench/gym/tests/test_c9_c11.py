"""Acceptance tests for gym challenges C9, C10, C11.

Grades scripted seed ledgers directly (no full runner / network). Seed1 must PASS
every AC; seed2/3 must produce the expected FAIL criteria and/or taxonomy classes.
"""
from __future__ import annotations

from tools.flywheel.bench.gym.challenges import c9_edit_from_ref as c9
from tools.flywheel.bench.gym.challenges import c10_fix_interference as c10
from tools.flywheel.bench.gym.challenges import c11_datasheet_dims as c11
from tools.flywheel.bench.gym.taxonomy import classify_ledger


def _ledger_from_script(turns: list[dict]) -> dict:
    """Build a minimal ledger.json-shaped doc from scripted ``_kind`` turns.

    Mirrors runner.replay_turns_into_ledger: strip _kind/_actor, assign seq.
    """
    events: list[dict] = []
    seq = 1
    for turn in turns:
        payload = dict(turn)
        kind = payload.pop("_kind", "decision")
        payload.pop("_actor", None)
        events.append({
            "seq": seq,
            "ts": f"2026-01-01T00:00:{seq:02d}Z",
            "actor": "feature_modeler",
            "kind": kind,
            "payload": payload,
        })
        seq += 1
    return {"events": events}


def _grade(challenge, seed: int, *, division: str = "local") -> tuple[dict, dict]:
    scripts = challenge.scripts[seed]
    turns: list[dict] = []
    for _dl, dl_turns in scripts.items():
        turns.extend(dl_turns)
    ledger = _ledger_from_script(turns)
    ctx = {"challenge": challenge.id, "seed": seed, "tier": "local-24k",
           "division": division}
    return challenge.grade(ledger, ctx), ledger


def _results_by_id(graded: dict) -> dict[str, dict]:
    return {c["id"]: c for c in graded["criteria"]}


# ---------------------------------------------------------------------------
# C9 — edit from reference
# ---------------------------------------------------------------------------

class TestC9EditFromRef:
    def test_module_exports_challenge(self):
        assert c9.CHALLENGE.id == "C9"
        assert set(c9.CHALLENGE.scripts.keys()) == {1, 2, 3}
        assert c9.CHALLENGE.modifies == "C1"
        assert "bore → 22" in c9.CHALLENGE.request or "22 H7" in c9.CHALLENGE.request
        assert "contact_sheet" in c9.MANIFEST.get("request_open", "") or \
               "screenshot" in c9.MANIFEST.get("request_open", "")

    def test_seed1_all_pass_local(self):
        graded, _ = _grade(c9.CHALLENGE, 1, division="local")
        by = _results_by_id(graded)
        for ac in ("AC1", "AC2", "AC3", "AC4", "AC5"):
            assert by[ac]["result"] == "PASS", f"{ac}: {by[ac]['detail']}"
        assert graded["passed"] is True
        assert "soft-skip" in by["AC5"]["detail"].lower() or \
               "not open" in by["AC5"]["detail"].lower()

    def test_seed1_open_requires_vision_and_passes(self):
        graded, _ = _grade(c9.CHALLENGE, 1, division="open")
        by = _results_by_id(graded)
        assert by["AC5"]["result"] == "PASS", by["AC5"]["detail"]
        assert graded["passed"] is True

    def test_seed1_open_fails_without_vision(self):
        turns = [dict(t) for t in c9.CHALLENGE.scripts[1]["DL1"]
                 if t.get("tool") not in c9._VISION_TOOLS]
        ledger = _ledger_from_script(turns)
        graded = c9.CHALLENGE.grade(ledger, {"division": "open", "seed": 1})
        by = _results_by_id(graded)
        assert by["AC5"]["result"] == "FAIL"
        assert graded["passed"] is False

    def test_seed2_read_before_mutate_fails(self):
        graded, ledger = _grade(c9.CHALLENGE, 2, division="local")
        by = _results_by_id(graded)
        assert by["AC4"]["result"] == "FAIL", by["AC4"]["detail"]
        assert graded["passed"] is False
        classes = {c.failure_class for c in classify_ledger(
            ledger, manifest=c9.CHALLENGE.manifest, graded=graded)}
        assert "tool_misuse" in classes, f"expected tool_misuse, got {classes}"

    def test_seed3_context_loss_fires(self):
        graded, ledger = _grade(c9.CHALLENGE, 3, division="local")
        by = _results_by_id(graded)
        assert by["AC1"]["result"] == "PASS"
        classes = {c.failure_class for c in classify_ledger(
            ledger, manifest=c9.CHALLENGE.manifest, graded=graded)}
        assert "context_loss" in classes, f"expected context_loss, got {classes}"

    def test_ac1_wrong_bore_fails(self):
        turns = [dict(t) for t in c9.CHALLENGE.scripts[1]["DL1"]]
        for t in turns:
            if t.get("id") == "DIM1":
                t["value"] = 20.0
        ledger = _ledger_from_script(turns)
        graded = c9.CHALLENGE.grade(ledger, {"division": "local"})
        assert _results_by_id(graded)["AC1"]["result"] == "FAIL"


# ---------------------------------------------------------------------------
# C10 — find-and-fix interference
# ---------------------------------------------------------------------------

class TestC10FixInterference:
    def test_module_exports_challenge(self):
        assert c10.CHALLENGE.id == "C10"
        assert set(c10.CHALLENGE.scripts.keys()) == {1, 2, 3}
        assert "interference" in c10.CHALLENGE.request.lower() or \
               "clash" in c10.CHALLENGE.request.lower()

    def test_seed1_all_pass(self):
        graded, _ = _grade(c10.CHALLENGE, 1)
        by = _results_by_id(graded)
        for ac in ("AC1", "AC2", "AC3", "AC4", "AC5"):
            assert by[ac]["result"] == "PASS", f"{ac}: {by[ac]['detail']}"
        assert graded["passed"] is True

    def test_seed2_residual_clash_and_battery_fail(self):
        graded, ledger = _grade(c10.CHALLENGE, 2)
        by = _results_by_id(graded)
        assert by["AC2"]["result"] == "FAIL", by["AC2"]["detail"]
        assert by["AC3"]["result"] == "FAIL", by["AC3"]["detail"]
        assert graded["passed"] is False
        classes = {c.failure_class for c in classify_ledger(
            ledger, manifest=c10.CHALLENGE.manifest, graded=graded)}
        assert "incoherent_geometry" in classes, f"expected incoherent_geometry, got {classes}"

    def test_seed3_grind_fires(self):
        graded, ledger = _grade(c10.CHALLENGE, 3)
        by = _results_by_id(graded)
        assert by["AC1"]["result"] == "PASS"
        assert by["AC2"]["result"] == "PASS"
        classes = {c.failure_class for c in classify_ledger(
            ledger, manifest=c10.CHALLENGE.manifest, graded=graded)}
        assert "grind" in classes, f"expected grind, got {classes}"

    def test_ac5_fails_without_repair_after_detect(self):
        turns = [
            dict(t) for t in c10.CHALLENGE.scripts[1]["DL1"]
            if not (
                t.get("tool") == "add_features"
                and (t.get("args") or {}).get("ops", [{}])[0].get("op") == "edit_feature"
            )
        ]
        ledger = _ledger_from_script(turns)
        graded = c10.CHALLENGE.grade(ledger, {"seed": 1})
        assert _results_by_id(graded)["AC5"]["result"] == "FAIL"


# ---------------------------------------------------------------------------
# C11 — dimension from datasheet
# ---------------------------------------------------------------------------

class TestC11DatasheetDims:
    def test_module_exports_challenge(self):
        assert c11.CHALLENGE.id == "C11"
        assert set(c11.CHALLENGE.scripts.keys()) == {1, 2, 3}
        assert "6202" in c11.CHALLENGE.request
        assert "35" in c11.CHALLENGE.request
        assert "DATASHEET" in c11.CHALLENGE.request.upper() or \
               "datasheet" in c11.CHALLENGE.request.lower()

    def test_seed1_all_pass(self):
        graded, _ = _grade(c11.CHALLENGE, 1)
        by = _results_by_id(graded)
        for ac in ("AC1", "AC2", "AC3", "AC4", "AC5"):
            assert by[ac]["result"] == "PASS", f"{ac}: {by[ac]['detail']}"
        assert graded["passed"] is True

    def test_seed2_calc_before_seat_fails_and_tool_misuse(self):
        graded, ledger = _grade(c11.CHALLENGE, 2)
        by = _results_by_id(graded)
        assert by["AC2"]["result"] == "FAIL", by["AC2"]["detail"]
        assert graded["passed"] is False
        classes = {c.failure_class for c in classify_ledger(
            ledger, manifest=c11.CHALLENGE.manifest, graded=graded)}
        assert "tool_misuse" in classes, f"expected tool_misuse, got {classes}"

    def test_seed3_context_loss_and_empty_provenance(self):
        graded, ledger = _grade(c11.CHALLENGE, 3)
        by = _results_by_id(graded)
        assert by["AC5"]["result"] == "FAIL", by["AC5"]["detail"]
        assert graded["passed"] is False
        classes = {c.failure_class for c in classify_ledger(
            ledger, manifest=c11.CHALLENGE.manifest, graded=graded)}
        assert "context_loss" in classes, f"expected context_loss, got {classes}"

    def test_ac1_wrong_seat_fails(self):
        turns = [dict(t) for t in c11.CHALLENGE.scripts[1]["DL1"]]
        for t in turns:
            if t.get("id") == "DIM1":
                t["value"] = 30.0
        ledger = _ledger_from_script(turns)
        graded = c11.CHALLENGE.grade(ledger, {})
        assert _results_by_id(graded)["AC1"]["result"] == "FAIL"

    def test_ac3_hole_out_of_band_fails(self):
        turns = [dict(t) for t in c11.CHALLENGE.scripts[1]["DL1"]]
        for t in turns:
            if t.get("id") == "bolt_circle":
                t["hole_d"] = 4.5  # M4-ish, not M5
        ledger = _ledger_from_script(turns)
        graded = c11.CHALLENGE.grade(ledger, {})
        assert _results_by_id(graded)["AC3"]["result"] == "FAIL"


# ---------------------------------------------------------------------------
# Cross-challenge smoke
# ---------------------------------------------------------------------------

def test_all_five_acs_defined():
    for mod, cid in ((c9, "C9"), (c10, "C10"), (c11, "C11")):
        ids = [c.id for c in mod.CHALLENGE.checks]
        assert ids == ["AC1", "AC2", "AC3", "AC4", "AC5"], f"{cid}: {ids}"
