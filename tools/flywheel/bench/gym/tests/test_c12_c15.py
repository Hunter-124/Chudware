"""Unit tests for C12–C15 ground-truth ACs against scripted seeds.

Builds synthetic ledgers from each challenge's scripts (same shape the gym runner
replays via ledger.append) and asserts every AC for seed1 PASS, plus labeled
failure seeds for the designed ACs. No network, no runner mission.
"""
from __future__ import annotations

import importlib


def _load(mod_name: str):
    return importlib.import_module(
        f"tools.flywheel.bench.gym.challenges.{mod_name}"
    )


def _ledger_from_scripts(scripts: dict[str, list[dict]], *, seed: int = 1) -> dict:
    """Materialize scripted turns into a ledger doc with monotonic seqs."""
    events: list[dict] = []
    seq = 1
    for _dl_id, turns in scripts.items():
        for turn in turns:
            payload = dict(turn)
            kind = payload.pop("_kind", "decision")
            payload.pop("_actor", None)
            events.append({
                "seq": seq,
                "ts": f"2026-07-14T00:00:{seq:02d}Z",
                "actor": "feature_modeler",
                "kind": kind,
                "payload": payload,
            })
            seq += 1
    return {"events": events, "seed": seed}


def _grade(ch, ledger: dict, *, seed: int) -> dict:
    ctx = {"seed": seed, "manifest": ch.manifest, "challenge": ch.id}
    return ch.grade(ledger, ctx)


def _by_id(graded: dict) -> dict[str, dict]:
    return {c["id"]: c for c in graded["criteria"]}


def _assert_all_pass(graded: dict, *, label: str) -> None:
    bad = [c for c in graded["criteria"] if c["result"] != "PASS"]
    assert not bad, f"{label}: expected all PASS, got {bad}"
    assert graded["passed"] is True, label


def _assert_ids(graded: dict, expect: dict[str, str], *, label: str) -> None:
    got = _by_id(graded)
    for ac_id, result in expect.items():
        assert ac_id in got, f"{label}: missing {ac_id}"
        assert got[ac_id]["result"] == result, (
            f"{label}: {ac_id} expected {result}, got {got[ac_id]['result']} "
            f"({got[ac_id]['detail']})"
        )


# ---------------------------------------------------------------------------
# C12 undo / checkpoint recovery
# ---------------------------------------------------------------------------

def test_c12_seed1_all_acs_pass():
    mod = _load("c12_undo_recovery")
    ch = mod.CHALLENGE
    assert ch.id == "C12"
    assert getattr(ch, "inject_faults", None), "C12 must declare inject_faults"
    faults = ch.inject_faults
    assert isinstance(faults, list) and faults
    assert faults[0]["after_build_calls"] == 2
    assert faults[0]["fault"] == "bad_extrude"
    lg = _ledger_from_scripts(ch.scripts[1], seed=1)
    graded = _grade(ch, lg, seed=1)
    _assert_all_pass(graded, label="C12 seed1")


def test_c12_seed2_grind_fails_ac5():
    mod = _load("c12_undo_recovery")
    ch = mod.CHALLENGE
    lg = _ledger_from_scripts(ch.scripts[2], seed=2)
    graded = _grade(ch, lg, seed=2)
    # The battery PASS precedes three more add_features: it no longer describes the final
    # geometry, so AC1 ("battery PASS on final geometry") fails as stale (P2-8).
    _assert_ids(graded, {
        "AC1": "FAIL",
        "AC2": "PASS",
        "AC3": "PASS",
        "AC4": "PASS",
        "AC5": "FAIL",
    }, label="C12 seed2 grind")
    assert graded["passed"] is False


def test_c12_seed3_no_recovery_fails_ac2_ac3_ac4():
    mod = _load("c12_undo_recovery")
    ch = mod.CHALLENGE
    lg = _ledger_from_scripts(ch.scripts[3], seed=3)
    graded = _grade(ch, lg, seed=3)
    got = _by_id(graded)
    assert got["AC1"]["result"] == "PASS", got["AC1"]
    assert got["AC2"]["result"] == "FAIL", got["AC2"]
    assert got["AC3"]["result"] == "FAIL", got["AC3"]
    assert got["AC4"]["result"] == "FAIL", got["AC4"]
    # AC5: no recovery → no post-recovery grind groups → PASS
    assert got["AC5"]["result"] == "PASS", got["AC5"]
    assert graded["passed"] is False


def test_c12_ac_graders_cover_recovery_tools_and_seq():
    """Direct AC unit checks: plm_restore path + post-recovery seq ordering."""
    mod = _load("c12_undo_recovery")
    ch = mod.CHALLENGE
    events = [
        {"seq": 1, "kind": "tool_call", "payload": {
            "tool": "plm_checkpoint", "node": "n_build_DL1", "args": {"name": "pre"},
            "hypothesis": "checkpoint before risk"}},
        {"seq": 2, "kind": "tool_call", "payload": {
            "tool": "plm_restore", "node": "n_build_DL1", "args": {"rev": 1},
            "hypothesis": "restore checkpoint"}},
        {"seq": 3, "kind": "decision", "payload": {
            "id": "recovery", "text": "restore from plm checkpoint after bad feature",
            "node": "n_build_DL1"}},
        {"seq": 4, "kind": "dimension", "payload": {
            "id": "DIM_post", "name": "bore", "value": 20.0}},
        {"seq": 5, "kind": "artifact", "payload": {
            "kind": "battery", "id": "b1", "verdict": "PASS"}},
    ]
    graded = _grade(ch, {"events": events}, seed=1)
    _assert_all_pass(graded, label="C12 plm_restore path")


# ---------------------------------------------------------------------------
# C13 sketch repair
# ---------------------------------------------------------------------------

def test_c13_seed1_all_acs_pass():
    mod = _load("c13_sketch_repair")
    ch = mod.CHALLENGE
    assert ch.id == "C13"
    lg = _ledger_from_scripts(ch.scripts[1], seed=1)
    graded = _grade(ch, lg, seed=1)
    _assert_all_pass(graded, label="C13 seed1")


def test_c13_seed2_unconstrained_fails_ac2_ac3_ac4():
    mod = _load("c13_sketch_repair")
    ch = mod.CHALLENGE
    lg = _ledger_from_scripts(ch.scripts[2], seed=2)
    graded = _grade(ch, lg, seed=2)
    got = _by_id(graded)
    assert got["AC1"]["result"] == "PASS", got["AC1"]  # health present
    assert got["AC2"]["result"] == "FAIL", got["AC2"]  # dof != 0
    assert got["AC3"]["result"] == "FAIL", got["AC3"]  # no constrain after
    assert got["AC4"]["result"] == "FAIL", got["AC4"]  # battery FAIL
    assert got["AC5"]["result"] == "PASS", got["AC5"]  # describe_sketch present
    assert graded["passed"] is False


def test_c13_seed3_misuse_fails_health_and_describe():
    mod = _load("c13_sketch_repair")
    ch = mod.CHALLENGE
    lg = _ledger_from_scripts(ch.scripts[3], seed=3)
    graded = _grade(ch, lg, seed=3)
    got = _by_id(graded)
    assert got["AC1"]["result"] == "FAIL", got["AC1"]  # no sketch_health
    # AC3 fails because no health to order after
    assert got["AC3"]["result"] == "FAIL", got["AC3"]
    assert got["AC5"]["result"] == "FAIL", got["AC5"]  # no describe_sketch
    assert graded["passed"] is False


def test_c13_describe_sketch_and_dof_zero_contract():
    mod = _load("c13_sketch_repair")
    ch = mod.CHALLENGE
    events = [
        {"seq": 1, "kind": "tool_call", "payload": {
            "tool": "check_sketch", "args": {"sketch_id": "SK1"}}},
        {"seq": 2, "kind": "tool_call", "payload": {
            "tool": "sketch_auto_constrain", "args": {"sketch_id": "SK1"}}},
        {"seq": 3, "kind": "decision", "payload": {
            "id": "sketch_status", "dof": 0, "text": "fully constrained"}},
        {"seq": 4, "kind": "tool_call", "payload": {
            "tool": "describe_sketch", "args": {"sketch_id": "SK1"}}},
        {"seq": 5, "kind": "artifact", "payload": {
            "kind": "battery", "verdict": "PASS",
            "checks": [{"name": "check_sketch", "verdict": "PASS"}]}},
    ]
    graded = _grade(ch, {"events": events}, seed=1)
    _assert_all_pass(graded, label="C13 minimal contract")


# ---------------------------------------------------------------------------
# C14 interface layout
# ---------------------------------------------------------------------------

def test_c14_seed1_all_acs_pass():
    mod = _load("c14_interface_layout")
    ch = mod.CHALLENGE
    assert ch.id == "C14"
    lg = _ledger_from_scripts(ch.scripts[1], seed=1)
    graded = _grade(ch, lg, seed=1)
    _assert_all_pass(graded, label="C14 seed1")


def test_c14_seed2_mismatch_and_clash():
    mod = _load("c14_interface_layout")
    ch = mod.CHALLENGE
    lg = _ledger_from_scripts(ch.scripts[2], seed=2)
    graded = _grade(ch, lg, seed=2)
    got = _by_id(graded)
    assert got["AC1"]["result"] == "FAIL", got["AC1"]  # pattern mismatch
    assert got["AC2"]["result"] == "FAIL", got["AC2"]  # interface fail
    assert got["AC3"]["result"] == "FAIL", got["AC3"]  # clashes != 0
    assert got["AC4"]["result"] == "FAIL", got["AC4"]  # mates not ok
    assert got["AC5"]["result"] == "FAIL", got["AC5"]  # batteries FAIL
    assert graded["passed"] is False


def test_c14_seed3_skipped_interface_mates():
    mod = _load("c14_interface_layout")
    ch = mod.CHALLENGE
    lg = _ledger_from_scripts(ch.scripts[3], seed=3)
    graded = _grade(ch, lg, seed=3)
    got = _by_id(graded)
    assert got["AC1"]["result"] == "PASS", got["AC1"]  # patterns match
    assert got["AC2"]["result"] == "FAIL", got["AC2"]
    assert got["AC3"]["result"] == "FAIL", got["AC3"]
    assert got["AC4"]["result"] == "FAIL", got["AC4"]
    assert got["AC5"]["result"] == "FAIL", got["AC5"]  # only one part battery
    assert graded["passed"] is False


def test_c14_matching_pattern_and_mates_contract():
    mod = _load("c14_interface_layout")
    ch = mod.CHALLENGE
    events = [
        {"seq": 1, "kind": "decision", "payload": {
            "id": "bolt_pattern", "pattern_mm": 28.0}},
        {"seq": 2, "kind": "decision", "payload": {
            "id": "bolt_pattern_mate", "pattern_mm": 28.0}},
        {"seq": 3, "kind": "tool_call", "payload": {
            "tool": "solve_mates", "ok": True}},
        {"seq": 4, "kind": "decision", "payload": {
            "id": "mates_ok", "mates_ok": True}},
        # Checks of the SOLVED assembly: recorded after the last mutation (solve_mates).
        {"seq": 5, "kind": "tool_call", "payload": {
            "tool": "interface_check", "ok": True,
            "result": {"pass": True, "verdict": "PASS"}}},
        {"seq": 6, "kind": "artifact", "payload": {
            "kind": "interference", "clashes": 0}},
        {"seq": 7, "kind": "artifact", "payload": {
            "kind": "battery", "verdict": "PASS", "part": "bracket", "id": "b_br"}},
        {"seq": 8, "kind": "artifact", "payload": {
            "kind": "battery", "verdict": "PASS", "part": "cover", "id": "b_cv"}},
    ]
    graded = _grade(ch, {"events": events}, seed=1)
    _assert_all_pass(graded, label="C14 minimal contract")
    # The same checks recorded BEFORE solving describe a superseded state.
    stale = [events[0], events[1], events[4], events[5], events[2], events[3],
             events[6], events[7]]
    by = _by_id(_grade(ch, {"events": stale}, seed=1))
    assert by["AC2"]["result"] == "FAIL" and by["AC3"]["result"] == "FAIL"


# ---------------------------------------------------------------------------
# C15 ambiguity / clarify
# ---------------------------------------------------------------------------

def test_c15_seed1_all_acs_pass():
    mod = _load("c15_ambiguity_clarify")
    ch = mod.CHALLENGE
    assert ch.id == "C15"
    assert any(a.get("blocking") for a in ch.manifest.get("ambiguities", []))
    lg = _ledger_from_scripts(ch.scripts[1], seed=1)
    graded = _grade(ch, lg, seed=1)
    _assert_all_pass(graded, label="C15 seed1")


def test_c15_seed2_hallucinated_dims_and_cosmetic_q():
    mod = _load("c15_ambiguity_clarify")
    ch = mod.CHALLENGE
    lg = _ledger_from_scripts(ch.scripts[2], seed=2)
    graded = _grade(ch, lg, seed=2)
    got = _by_id(graded)
    assert got["AC1"]["result"] == "FAIL", got["AC1"]  # no blocking Q / assumption
    assert got["AC2"]["result"] == "FAIL", got["AC2"]  # hallucinated dims
    assert got["AC3"]["result"] == "FAIL", got["AC3"]  # critical dims no provenance
    assert got["AC4"]["result"] == "PASS", got["AC4"]  # negative control: cosmetic Q
    assert got["AC5"]["result"] == "FAIL", got["AC5"]  # battery without assumptions
    assert graded["passed"] is False


def test_c15_seed3_assumptions_only_path():
    mod = _load("c15_ambiguity_clarify")
    ch = mod.CHALLENGE
    lg = _ledger_from_scripts(ch.scripts[3], seed=3)
    graded = _grade(ch, lg, seed=3)
    _assert_all_pass(graded, label="C15 seed3 assumptions-only")


def test_c15_hallucinated_dim_detector_unit():
    """AC2: dim without provenance and without assumption text overlap → FAIL."""
    mod = _load("c15_ambiguity_clarify")
    ch = mod.CHALLENGE
    events = [
        {"seq": 1, "kind": "question", "payload": {
            "id": "Q1", "text": "What shaft diameter?", "blocking": True,
            "bearing": "critical"}},
        {"seq": 2, "kind": "decision", "payload": {
            "id": "assumption_shaft", "text": "assume shaft 10 mm",
            "needs_confirm": True}},
        {"seq": 3, "kind": "dimension", "payload": {
            "id": "DIMX", "name": "bolt_hole_diameter", "value": 99.0,
            "provenance": ""}},  # no overlap with "shaft" assumption
        {"seq": 4, "kind": "artifact", "payload": {
            "kind": "battery", "verdict": "PASS"}},
    ]
    graded = _grade(ch, {"events": events}, seed=1)
    got = _by_id(graded)
    assert got["AC2"]["result"] == "FAIL", got["AC2"]
    assert "hallucinated" in got["AC2"]["detail"].lower() or "99" in got["AC2"]["detail"]


def test_c15_provenance_passes_when_linked():
    mod = _load("c15_ambiguity_clarify")
    ch = mod.CHALLENGE
    events = [
        {"seq": 1, "kind": "decision", "payload": {
            "id": "assumption_bolt", "text": "assume M4 bolt pattern",
            "needs_confirm": True}},
        {"seq": 2, "kind": "dimension", "payload": {
            "id": "DIM1", "name": "bolt_hole_d", "value": 4.5,
            "provenance": "assumption:assumption_bolt",
            "assumption_id": "assumption_bolt"}},
        {"seq": 3, "kind": "artifact", "payload": {
            "kind": "battery", "verdict": "PASS"}},
    ]
    graded = _grade(ch, {"events": events}, seed=3)
    got = _by_id(graded)
    assert got["AC1"]["result"] == "PASS", got["AC1"]
    assert got["AC2"]["result"] == "PASS", got["AC2"]
    assert got["AC3"]["result"] == "PASS", got["AC3"]
    assert got["AC5"]["result"] == "PASS", got["AC5"]


def test_modules_export_three_seeds_each():
    for name, cid in (
        ("c12_undo_recovery", "C12"),
        ("c13_sketch_repair", "C13"),
        ("c14_interface_layout", "C14"),
        ("c15_ambiguity_clarify", "C15"),
    ):
        mod = _load(name)
        ch = mod.CHALLENGE
        assert ch.id == cid
        assert set(ch.scripts.keys()) >= {1, 2, 3}, f"{cid} missing seeds"
        assert ch.seeds == (1, 2, 3) or set(ch.seeds) >= {1, 2, 3}
        assert len(ch.checks) == 5, f"{cid} should have 5 ACs"
        assert ch.manifest.get("deliverables"), f"{cid} manifest deliverables"
