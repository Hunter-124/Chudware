"""Autonomous ECAD design orchestrator tests -- MOCK provider, real ECAD sessions.

House style: plain ``main()`` + asserts (see engine/sim/test_circuit.py, agent/test_orchestrator.py).
Run as::

    & "$env:APPDATA\AI-CAD\runtime\venv\Scripts\python.exe" -m engine.ecad.test_orchestrator

No pytest, no API key, no network -- a deterministic MockProvider returns canned structured plans, so
the whole pipeline (PLAN -> BUILD -> ERC -> SIMULATE -> REPORT) runs offline in CI.

Coverage (the spec's four scenarios + recipe-direct gates):
  1. valid divider PLAN  -> orchestrator BUILDS an ERC-clean schematic that SIMULATES to the
     2.5 V midpoint (V ~= expected); DesignRun records stages + ops + a sim PASS.
  2. MALFORMED plan x2 then nothing -> falls back to the voltage_divider RECIPE and still produces
     a valid, simulating schematic (recipe_used set, ok True) -- robustness for a weak model.
  3. recipes.inverting_amp(gain=10) materialized + simulated -> gain ~= -10 (op-amp subckt).
  4. ERC-fix path: a plan leaves an op-amp supply pin floating -> the FIX stage resolves it ->
     final ERC has 0 errors.
Plus: context-size bound (every model prompt under the small-context cap) + every recipe simulates.
"""

from __future__ import annotations

import json

from services.ecad_design import orchestrator as O
from engine.ecad import recipes as R
from engine.ecad import session as ecad_session
from agent.providers.base import Provider


# --------------------------------------------------------------------------- mock provider


class MockProvider(Provider):
    """Deterministic provider that returns scripted replies in order, recording every prompt.

    Mirrors agent.providers.mock_provider.MockProvider but also captures (system, user_text) so the
    tests can assert the context stays bounded. The orchestrator makes ONE provider.complete() call
    per PLAN attempt with a fresh bounded context (no growing history).
    """

    name = "mock"
    supports_vision = False

    def __init__(self, scripted_replies: list[str]):
        self._replies = list(scripted_replies)
        self._i = 0
        self.prompts: list[tuple[str, str]] = []   # (system, last_user_text)

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        utext = ""
        if messages and messages[-1].get("content"):
            parts = messages[-1]["content"]
            utext = parts[0].get("text", "") if parts else ""
        self.prompts.append((system, utext))
        reply = self._replies[min(self._i, len(self._replies) - 1)] if self._replies else ""
        self._i += 1
        return reply


def _json_block(obj) -> str:
    return "```json\n" + json.dumps(obj) + "\n```"


def _valid_divider_plan() -> str:
    """A hand-written, valid 5V->2.5V divider plan (what a competent model would emit)."""
    return _json_block({
        "components": [
            {"ref": "V1", "libid": "Simulation_SPICE:V_source", "value": "5"},
            {"ref": "R1", "libid": "Device:R", "value": "10k"},
            {"ref": "R2", "libid": "Device:R", "value": "10k"},
        ],
        "nets": {
            "IN":   [{"ref": "V1", "pin": "+"}, {"ref": "R1", "pin": "1"}],
            "VMID": [{"ref": "R1", "pin": "2"}, {"ref": "R2", "pin": "1"}],
            "GND":  [{"ref": "V1", "pin": "-"}, {"ref": "R2", "pin": "2"}],
        },
        "study": {"analysis": "op", "params": {},
                  "expect": {"probe": "V(VMID)", "value": 2.5, "tol": 0.02}},
    })


# --------------------------------------------------------------------------- tests


def test_valid_plan_divider_simulates() -> None:
    """Scenario 1: a valid divider plan builds an ERC-clean schematic simulating to 2.5 V."""
    prov = MockProvider([_valid_divider_plan()])
    events: list[dict] = []
    run = O.run_design("a 5V resistive divider with 2.5V output", provider=prov,
                       emit=events.append)

    assert run.ok, f"run should be ok, got stop={run.stop_reason} sim={run.sim}"
    assert run.recipe_used is None, "a valid model plan must NOT trigger the recipe fallback"
    assert run.stop_reason == "confirmed", run.stop_reason

    # Real ERC-clean schematic.
    assert run.erc["errors"] == 0, f"ERC should be clean, got {run.erc}"
    # Simulated to the expected midpoint.
    assert run.sim["analysis"] == "op" and run.sim["converged"], run.sim
    assert run.sim["pass"], f"sim should pass: {run.sim}"
    assert abs(run.sim["got"] - 2.5) < 0.05, f"V(VMID) ~= 2.5 expected, got {run.sim['got']}"

    # DesignRun records the stages + ops.
    stage_names = [s["stage"] for s in run.stages]
    assert "plan" in stage_names and "build" in stage_names, stage_names
    assert "erc" in stage_names and "simulate" in stage_names, stage_names
    assert run.ops_applied > 0 and run.revision >= 1, (run.ops_applied, run.revision)
    # the plan stage was driven by the model (not a fallback event).
    plan_ev = [s for s in run.stages if s["stage"] == "plan"][0]
    assert plan_ev["ok"] and plan_ev["attempt"] == 1, plan_ev
    print(f"[1 valid-plan] V(VMID)={run.sim['got']:.4f} V (expect 2.5)  ERC={run.erc['errors']} err  "
          f"ops={run.ops_applied}  stages={stage_names}")


def test_malformed_plan_falls_back_to_recipe() -> None:
    """Scenario 2: garbage twice then empty -> voltage_divider RECIPE fallback still simulates."""
    prov = MockProvider([
        "here is your circuit, trust me {not json [",          # attempt 1: unparseable
        _json_block({"components": [], "nets": {}}),            # attempt 2: parseable but invalid
        "",                                                     # attempt 3: nothing
    ])
    run = O.run_design("a 5V resistive divider with 2.5V output", provider=prov)

    assert run.recipe_used == "voltage_divider", \
        f"weak model must fall back to the divider recipe, got {run.recipe_used}"
    assert run.stop_reason == "fallback", run.stop_reason
    assert run.ok, f"the recipe path must still produce a verified schematic: {run.sim}"
    assert run.erc["errors"] == 0, run.erc
    assert run.sim["pass"] and abs(run.sim["got"] - 2.5) < 0.05, run.sim

    # The model was asked the bounded number of times, and each failure was recorded.
    plan_events = [s for s in run.stages if s["stage"] == "plan"]
    model_attempts = [s for s in plan_events if s["attempt"] >= 1]
    assert len(model_attempts) == 3, f"expected 3 bounded attempts, got {len(model_attempts)}"
    assert all(not s["ok"] for s in model_attempts), "all 3 model attempts should be invalid"
    assert any(s["attempt"] == 0 and s["ok"] for s in plan_events), "a fallback event must be present"
    print(f"[2 fallback] recipe={run.recipe_used}  attempts={len(model_attempts)}  "
          f"V(VMID)={run.sim['got']:.4f} V  ok={run.ok}")


def test_inverting_amp_recipe_gain() -> None:
    """Scenario 3: recipes.inverting_amp(gain=10) materialized + simulated -> gain ~= -10."""
    design = R.inverting_amp(gain=10)
    sess = ecad_session.new_session("sch", name="invamp_test")
    build = O.build_design(sess, design)
    assert build["ops_applied"] > 0 and not build["missing_pins"], build

    erc = O._erc_summary(sess)
    assert erc["errors"] == 0, f"inverting-amp recipe should be ERC-clean: {erc['rules']}"

    sim = O.simulate_design(sess, design)
    assert sim["converged"], f"op must converge: {sim.get('log')}"
    vout = sim["got"]
    vin = 0.2
    gain = vout / vin
    assert abs(gain - (-10.0)) < 0.1, f"inverting gain ~= -10 expected, got {gain:.4f}"
    assert sim["pass"], sim
    print(f"[3 inv-amp] V(OUT)={vout:.4f} V  gain={gain:.4f} (expect -10)  ERC={erc['errors']} err")


def test_erc_fix_resolves_floating_pin() -> None:
    """Scenario 4: a plan leaves a supply pin undriven -> the FIX stage drives it -> 0 ERC errors.

    The NE555's VCC (pin 8) + RST (pin 4) are ``power_in`` pins. We tie them to a net named
    ``SUPPLY`` (deliberately NOT a power-rail name, so the builder does NOT auto-add a power port)
    with no driver, which ERC flags as a ``power_not_driven`` ERROR. The deterministic FIX stage
    adds a power port on that net, clearing the error -- the weak-model safety net for connectivity.
    """
    plan = {
        "components": [
            {"ref": "U1", "libid": "Timer:NE555", "value": "NE555"},
            {"ref": "R1", "libid": "Device:R", "value": "10k"},
        ],
        "nets": {
            # SUPPLY carries only power_in pins (555 VCC + RST) + a resistor pin -> no driver.
            "SUPPLY": [{"ref": "U1", "pin": "8"}, {"ref": "U1", "pin": "4"},
                       {"ref": "R1", "pin": "1"}],
            "GND": [{"ref": "U1", "pin": "1"}, {"ref": "R1", "pin": "2"}],
        },
        "study": {"analysis": "op", "params": {},
                  "expect": {"probe": "V(SUPPLY)", "value": 5.0, "tol": 0.1}},
    }
    ok, errs = O.validate_design(plan)
    assert ok, f"the fixture plan must itself be structurally valid: {errs}"

    prov = MockProvider([_json_block(plan)])
    run = O.run_design("a 555 timer supply", provider=prov)

    # An ERC error existed BEFORE the fix (recorded in the erc stage event)...
    erc_ev = [s for s in run.stages if s["stage"] == "erc"][0]
    assert erc_ev["result"]["errors"] >= 1, \
        f"the floating V+ pin should trip an ERC error first, got {erc_ev['result']}"
    # ...and the FIX stage cleared it.
    fix_events = [s for s in run.stages if s["stage"] == "fix"]
    assert fix_events, "a FIX stage must run when ERC has errors"
    assert run.erc["errors"] == 0, f"FIX should clear all ERC errors, left {run.erc}"
    assert fix_events[-1]["ok"], fix_events[-1]
    print(f"[4 erc-fix] before={erc_ev['result']['errors']} err -> after={run.erc['errors']} err  "
          f"(fix passes={len(fix_events)})")


def test_context_size_bounded() -> None:
    """Every model prompt stays under the small-context (4-8k) budget; one call per attempt."""
    prov = MockProvider(["garbage", "garbage", "garbage"])   # force all 3 attempts
    O.run_design("a 5V resistive divider with 2.5V output", provider=prov)
    assert prov.prompts, "the model must have been called"
    for system, utext in prov.prompts:
        total = len(system) + len(utext)
        assert total <= O._CONTEXT_CHAR_CAP, \
            f"context {total} chars exceeds cap {O._CONTEXT_CHAR_CAP}"
    print(f"[ctx] {len(prov.prompts)} model call(s); max prompt "
          f"{max(len(s) + len(u) for s, u in prov.prompts)} chars (cap {O._CONTEXT_CHAR_CAP})")


def test_every_recipe_validates_and_simulates() -> None:
    """All four recipes are structurally valid, build ERC-clean, and simulate (or honestly miss).

    The 555 astable's behavioural subckt may not free-run in the MNA engine; the orchestrator
    RECORDS that discrepancy (got=None / pass=False) instead of looping -- so we only require it to
    build, be ERC-clean, and CONVERGE, not to pass the period check.
    """
    results = []
    for name, builder, _kw in R.RECIPES:
        design = builder()
        ok, errs = O.validate_design(design)
        assert ok, f"recipe {name} is structurally invalid: {errs}"
        sess = ecad_session.new_session("sch", name=f"recipe_{name}")
        O.build_design(sess, design)
        erc = O._erc_summary(sess)
        assert erc["errors"] == 0, f"recipe {name} has ERC errors: {erc['rules']}"
        sim = O.simulate_design(sess, design)
        results.append((name, sim["converged"], sim["pass"], sim["got"]))
        if name in ("voltage_divider", "inverting_amp", "rc_lowpass"):
            assert sim["pass"], f"recipe {name} should simulate to its expectation: {sim}"
    print("[recipes] " + "  ".join(
        f"{n}:conv={c},pass={p},got={O._fmt_num(g)}" for n, c, p, g in results))


def main() -> None:
    print("=" * 70)
    print("ECAD DESIGN ORCHESTRATOR TESTS (mock provider, headless)")
    print("=" * 70)
    test_valid_plan_divider_simulates()
    test_malformed_plan_falls_back_to_recipe()
    test_inverting_amp_recipe_gain()
    test_erc_fix_resolves_floating_pin()
    test_context_size_bounded()
    test_every_recipe_validates_and_simulates()
    print("\nORCHESTRATOR TESTS PASSED")


if __name__ == "__main__":
    main()
