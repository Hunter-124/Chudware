"""Exercise the full agent loop deterministically with the mock provider (no API key).

Proves the orchestration end to end: code extraction -> kernel build -> render ->
image feedback -> visual-confirm -> done.
"""
from __future__ import annotations

from pathlib import Path

from agent import loop
from agent.providers.mock_provider import MockProvider

SAMPLE = (Path(__file__).parent.parent / "engine" / "samples" / "mounting_plate.py").read_text(
    encoding="utf-8"
)


def test_happy_path() -> None:
    scripted = [
        f"Here is the first version.\n```python\n{SAMPLE}\n```\n"
        '```json\n{"done": false, "notes": "initial build; will verify render"}\n```',
        # Second turn: model has seen the render and confirms (no new code).
        '```json\n{"done": true, "notes": "iso/top show the boss, bore, and four '
        'counterbores; proportions match."}\n```',
    ]
    run = loop.run("a mounting plate with a central boss and four counterbored holes",
                   MockProvider(scripted), name="agent_mock")
    print("[happy] success:", run.success, "iterations:", run.iterations)
    assert run.success, "agent run should succeed"
    assert run.iterations == 2, f"expected 2 iterations, got {run.iterations}"
    assert run.contact_sheet and Path(run.contact_sheet).exists(), "contact sheet missing"
    assert run.log[-1]["done"] is True, "final iteration should be done"


def test_error_recovery() -> None:
    """First attempt is broken code; the loop must surface the error and recover."""
    broken = "result = Box(10, 10, 10) - Cylinder()  # missing required radius/height args"
    scripted = [
        f"```python\n{broken}\n```\n```json\n{{\"done\": false, \"notes\": \"attempt 1\"}}\n```",
        f"Sorry, fixing it.\n```python\n{SAMPLE}\n```\n"
        '```json\n{"done": false, "notes": "fixed the constructor args"}\n```',
        '```json\n{"done": true, "notes": "render matches"}\n```',
    ]
    run = loop.run("a mounting plate", MockProvider(scripted), name="agent_recover")
    print("[recover] success:", run.success, "iterations:", run.iterations)
    for e in run.log:
        print("   ", {k: e[k] for k in ("index", "ok", "done")})
    assert run.log[0]["ok"] is False, "first attempt should fail to build"
    assert run.log[0]["error"], "failed iteration should carry a traceback"
    assert run.success, "loop should recover and end with a valid model"
    assert run.log[-1]["done"] is True, "final iteration should be done"
    assert run.iterations == 3, f"expected 3 iterations, got {run.iterations}"


def test_session_refines_prior_work() -> None:
    """A follow-up turn must CONTINUE the conversation (refine the existing part), not
    restart from scratch — this is the cross-turn iteration the GUI relies on."""
    scripted = [
        # turn 1: build a 20mm cube, then confirm after seeing the render
        '```python\nresult = Box(20, 20, 20)\n```\n```json\n{"done": false, "notes": "v1"}\n```',
        '```json\n{"done": true, "notes": "cube looks right"}\n```',
        # turn 2: make it taller — the model should modify the prior code
        '```python\nresult = Box(20, 20, 40)\n```\n```json\n{"done": false, "notes": "taller"}\n```',
        '```json\n{"done": true, "notes": "now 40mm tall"}\n```',
    ]
    provider = MockProvider(scripted)
    session = loop.Session(name="agent_session")

    r1 = loop.run_turn(session, "a 20mm cube", provider)
    assert r1.success and r1.turn == 1, "turn 1 should succeed"
    msgs_after_t1 = len(session.messages)
    assert session.last_code and "20, 20, 20" in session.last_code

    r2 = loop.run_turn(session, "make it 40mm tall", provider)
    assert r2.success and r2.turn == 2, "turn 2 should succeed as the second turn"
    # The conversation carried over (did not reset) and grew.
    assert len(session.messages) > msgs_after_t1, "turn 2 must continue the same conversation"
    # The refine turn's opening user message is the REFINE prompt, not a fresh FIRST_USER.
    refine_msg = session.messages[msgs_after_t1]
    refine_text = refine_msg["content"][0]["text"]
    assert "Follow-up request" in refine_text, "turn 2 should open with the refine prompt"
    assert "40, 40" not in (session.last_code or ""), "sanity"
    assert "20, 20, 40" in (session.last_code or ""), "turn 2 should produce the taller cube"
    # A diff vs the prior code should have been computed for the changed iteration.
    diffs = [e["diff"] for e in r2.log if e.get("diff")]
    assert diffs and "20, 20, 40" in diffs[0], "refine iteration should carry a unified diff"
    print("[session] turn1 iters:", r1.iterations, " turn2 iters:", r2.iterations,
          " diff captured:", bool(diffs))


def test_streaming_events() -> None:
    """run_turn must stream structured progress events (status + per-iteration detail)."""
    events: list[dict] = []
    scripted = [
        '```python\nresult = Box(10, 10, 10)\n```\n```json\n{"done": false, "notes": "v1"}\n```',
        '```json\n{"done": true, "notes": "ok"}\n```',
    ]
    run = loop.run("a 10mm cube", MockProvider(scripted), name="agent_stream",
                   on_event=events.append)
    kinds = {e["kind"] for e in events}
    assert "status" in kinds and "iteration" in kinds, f"missing event kinds: {kinds}"
    iters = [e for e in events if e["kind"] == "iteration"]
    assert iters, "expected at least one iteration event"
    first = iters[0]
    assert first["code"] and "Box(10" in first["code"], "iteration event should carry code"
    assert first["ok"] is True and first["dims"], "iteration event should carry build result"
    assert first["contact_sheet"], "iteration event should carry a render path"
    print("[stream] events:", len(events), " kinds:", sorted(kinds),
          " iter dims:", first["dims"])


def test_assembly_multi_part() -> None:
    """A multi-part request must drive the assembly path: per-part build, a color-coded
    render, and a fit report streamed in the iteration events — first with an interference,
    then resolved on the refine pass."""
    events: list[dict] = []
    # v1 overlaps the peg through the base (interference); v2 lifts the peg clear.
    clash = ('parts = {\n'
             '    "base": Box(30, 30, 10),\n'
             '    "peg": Pos(0, 0, 2) * Cylinder(4, 20),\n'
             '}')
    fixed = ('parts = {\n'
             '    "base": Box(30, 30, 10),\n'
             '    "peg": Pos(0, 0, 20) * Cylinder(4, 20),\n'
             '}')
    scripted = [
        f"First pass.\n```python\n{clash}\n```\n"
        '```json\n{"done": false, "notes": "checking the fit"}\n```',
        f"The peg was clashing; lifting it clear.\n```python\n{fixed}\n```\n"
        '```json\n{"done": false, "notes": "moved the peg above the base"}\n```',
        '```json\n{"done": true, "notes": "peg now seats with clearance, no interference"}\n```',
    ]
    run = loop.run("a base plate with a peg that sits on top, no interference",
                   MockProvider(scripted), name="agent_asm", on_event=events.append)
    print("[assembly] success:", run.success, "is_assembly:", run.is_assembly,
          "iterations:", run.iterations)
    assert run.success, "assembly run should succeed"
    assert run.is_assembly, "run should be flagged as an assembly"
    assert run.contact_sheet and Path(run.contact_sheet).exists(), "assembly render missing"

    # The final fit report must be clean (no interference) after the fix.
    assert run.fit is not None and run.fit["ok"], f"final fit should be clean: {run.fit}"
    assert not run.fit["interferences"], "final assembly should have no interference"

    # The streamed iteration events must carry the per-part list + fit report, and the FIRST
    # build must have reported the interference (so the model could act on it).
    iters = [e for e in events if e["kind"] == "iteration"]
    assert iters and iters[0]["is_assembly"], "iteration events should be flagged assembly"
    assert iters[0]["parts"] and {p["name"] for p in iters[0]["parts"]} == {"base", "peg"}, \
        "iteration event should carry the named parts"
    assert iters[0]["fit"] and iters[0]["fit"]["interferences"], \
        "first build should report the peg/base interference"
    overlap = iters[0]["fit"]["interferences"][0]["volume"]
    assert overlap > 1.0, f"interference overlap should be a real volume, got {overlap}"
    # per-part STLs were exported (so the GUI/MCP can show/export individual bodies)
    assert all(p.get("stl_path") for p in run.result["parts"]), "each part needs an STL"
    print("[assembly] first-pass interference:", round(overlap, 1), "mm^3 ->",
          "final:", run.fit["summary"])


def test_stops_early_not_capped() -> None:
    """The loop must STOP as soon as the model confirms done — not run to the cap. This is the
    regression for 'it does N iterations every single time regardless'."""
    scripted = [
        '```python\nresult = Box(20, 20, 20)\n```\n```json\n{"done": false, "notes": "v1"}\n```',
        '```json\n{"done": true, "notes": "matches the request"}\n```',  # confirm, no new code
    ]
    run = loop.run("a 20mm cube", MockProvider(scripted), name="agent_early", max_iters=30)
    print("[early] iterations:", run.iterations, "cap:", 30, "converged:", run.converged,
          "reason:", run.stop_reason)
    assert run.iterations == 2, f"should stop at 2 (confirmed), not run the cap; got {run.iterations}"
    assert run.converged and run.stop_reason == "confirmed", "should record an early confirm"


def test_continues_until_done() -> None:
    """When the part isn't right yet the loop must KEEP iterating (changing the design each
    pass) until the model is satisfied — not stop short."""
    scripted = [
        '```python\nresult = Box(10, 10, 10)\n```\n```json\n{"done": false, "notes": "p1"}\n```',
        '```python\nresult = Box(20, 10, 10)\n```\n```json\n{"done": false, "notes": "p2"}\n```',
        '```python\nresult = Box(30, 10, 10)\n```\n```json\n{"done": false, "notes": "p3"}\n```',
        '```json\n{"done": true, "notes": "now correct"}\n```',
    ]
    run = loop.run("a bar", MockProvider(scripted), name="agent_cont", max_iters=30)
    print("[continue] iterations:", run.iterations, "reason:", run.stop_reason)
    assert run.iterations == 4, f"should iterate while not done; got {run.iterations}"
    assert run.converged and run.stop_reason == "confirmed"


def test_stagnation_stops() -> None:
    """A model that rebuilds the SAME geometry without ever declaring done must be stopped by
    the stagnation guard, NOT run to the (high) cap."""
    scripted = [  # MockProvider repeats this forever: same code, never 'done'
        '```python\nresult = Box(12, 12, 12)\n```\n```json\n{"done": false, "notes": "again"}\n```',
    ]
    run = loop.run("a 12mm cube", MockProvider(scripted), name="agent_stag", max_iters=40)
    print("[stagnation] iterations:", run.iterations, "reason:", run.stop_reason,
          "converged:", run.converged)
    assert run.iterations < 40, "must not run to the cap when geometry stops changing"
    assert run.stop_reason == "stabilized" and run.converged, "should stop via stagnation"
    assert run.success, "the settled build is still a valid result"


def test_failure_giveup() -> None:
    """Repeated unfixable build failures must stop the turn at the failure limit, not burn the
    whole iteration budget."""
    scripted = [  # always-broken code, repeated by the mock
        "```python\nresult = Box() - Cylinder()\n```\n```json\n{\"done\": false, \"notes\": \"x\"}\n```",
    ]
    run = loop.run("broken", MockProvider(scripted), name="agent_fail", max_iters=40)
    print("[giveup] iterations:", run.iterations, "reason:", run.stop_reason,
          "success:", run.success)
    assert run.iterations < 40, "must give up well before the cap on repeated failures"
    assert run.stop_reason == "failed" and not run.success


def test_repeat_nudge_then_recovers() -> None:
    """When the model resubmits IDENTICAL code, the loop must send a pointed nudge (not just
    re-ask) and, if the model then changes the code, keep going to a real finish — rather than
    silently spinning or stopping short. This is the ATX-case 'stuck repeating' regression."""
    session = loop.Session(name="agent_nudge")
    scripted = [
        '```python\nresult = Box(10, 10, 10)\n```\n```json\n{"done": false, "notes": "v1"}\n```',
        '```python\nresult = Box(10, 10, 10)\n```\n```json\n{"done": false, "notes": "same"}\n```',
        '```python\nresult = Box(20, 10, 10)\n```\n```json\n{"done": false, "notes": "fixed"}\n```',
        '```json\n{"done": true, "notes": "good now"}\n```',
    ]
    run = loop.run_turn(session, "a bar", MockProvider(scripted), max_iters=30)
    print("[nudge] iterations:", run.iterations, "reason:", run.stop_reason)
    assert run.iterations == 4 and run.converged and run.stop_reason == "confirmed", \
        f"should nudge on repeat then converge; got {run.iterations}/{run.stop_reason}"
    allmsgs = " ".join(p.get("text", "") for m in session.messages for p in m["content"])
    assert "returned the SAME script" in allmsgs, "a repeated submission should trigger the nudge"


def test_noop_change_warns() -> None:
    """A code edit that produces byte-identical geometry (cutter outside the body) must get
    the explicit no-op warning, so the model learns its feature touched nothing — this is
    the ATX-case 'standoff holes that cut nothing' regression."""
    session = loop.Session(name="agent_noop")
    scripted = [
        '```python\nresult = Box(20, 20, 20)\n```\n```json\n{"done": false, "notes": "v1"}\n```',
        # "adds a hole" 100mm away from a 20mm box -> same volume, same triangles
        '```python\nresult = Box(20, 20, 20)\nresult -= Pos(100, 0, 0) * Cylinder(3, 30)\n```\n'
        '```json\n{"done": false, "notes": "added hole"}\n```',
        '```python\nresult = Box(20, 20, 20)\nresult -= Cylinder(3, 30)\n```\n'
        '```json\n{"done": false, "notes": "moved the hole into the body"}\n```',
        '```json\n{"done": true, "notes": "hole visible now"}\n```',
    ]
    run = loop.run_turn(session, "a cube with a hole", MockProvider(scripted), max_iters=30)
    print("[noop] iterations:", run.iterations, "reason:", run.stop_reason)
    assert run.converged and run.stop_reason == "confirmed"
    allmsgs = " ".join(p.get("text", "") for m in session.messages for p in m["content"])
    assert "geometry is IDENTICAL" in allmsgs, "no-op change should trigger the warning"


PLAN_REPLY = """Here is the plan.
```json
{
  "name": "hex standoff",
  "summary": "extruded hex prism with a through bore",
  "parts": [
    {
      "name": "standoff",
      "purpose": "spaces a PCB off its mount",
      "dimensions": {"across_flats": 10, "height": 22, "bore_radius": 1.7},
      "features": [
        "M3 clearance bore r=1.7 through - cut a Cylinder",
        {"feature": "light top chamfer", "how": "chamfer top rim length=0.5"}
      ]
    }
  ],
  "fit": ""
}
```"""


def test_plan_mode() -> None:
    """plan_first must add a planning call before the first code pass, stream a "plan"
    event, store the plan on the session, and inject it into the opening user message."""
    session = loop.Session(name="agent_plan")
    scripted = [
        PLAN_REPLY,  # consumed by the planning call
        '```python\nresult = Box(10, 10, 22)\n```\n```json\n{"done": false, "notes": "v1"}\n```',
        '```json\n{"done": true, "notes": "matches the plan"}\n```',
    ]
    events: list = []
    run = loop.run_turn(session, "an M3 hex standoff, 22mm tall", MockProvider(scripted),
                        on_event=events.append, plan_first=True)
    print("[plan] success:", run.success, "iterations:", run.iterations)
    assert run.success and run.iterations == 2
    plans = [e for e in events if e.get("kind") == "plan"]
    assert len(plans) == 1, "exactly one plan event should stream"
    assert plans[0]["plan"]["parts"][0]["name"] == "standoff"
    assert "across_flats 10" in plans[0]["plan"]["parts"][0]["dimensions"]
    # dict-shaped feature entries are flattened to strings
    assert any("chamfer" in f for f in plans[0]["plan"]["parts"][0]["features"])
    assert session.plan and run.plan, "plan must persist on the session and the run"
    opening = session.messages[0]["content"][0]["text"]
    assert "BUILD PLAN" in opening and "across_flats 10" in opening, \
        "the opening design message must carry the plan"
    statuses = [e for e in events if e.get("kind") == "status" and e.get("phase") == "planning"]
    assert statuses, "a 'planning' status should stream before the plan"


def test_plan_garbage_falls_back() -> None:
    """An unparseable plan reply must not block the design - the loop proceeds planless."""
    session = loop.Session(name="agent_planbad")
    scripted = [
        "I think we should make it nice and sturdy.",  # no json at all
        '```python\nresult = Box(10, 10, 10)\n```\n```json\n{"done": false, "notes": "v1"}\n```',
        '```json\n{"done": true, "notes": "ok"}\n```',
    ]
    events: list = []
    run = loop.run_turn(session, "a small cube", MockProvider(scripted),
                        on_event=events.append, plan_first=True)
    print("[planbad] success:", run.success, "plan:", session.plan)
    assert run.success, "a failed plan must not fail the run"
    assert session.plan is None and run.plan is None
    assert not [e for e in events if e.get("kind") == "plan"]
    assert [e for e in events if e.get("phase") == "plan_failed"], \
        "the fallback should be announced as a status event"
    assert "BUILD PLAN" not in session.messages[0]["content"][0]["text"]


def main() -> None:
    test_happy_path()
    test_error_recovery()
    test_session_refines_prior_work()
    test_streaming_events()
    test_assembly_multi_part()
    test_stops_early_not_capped()
    test_continues_until_done()
    test_stagnation_stops()
    test_failure_giveup()
    test_repeat_nudge_then_recovers()
    test_noop_change_warns()
    test_plan_mode()
    test_plan_garbage_falls_back()
    print("\nAGENT LOOP MOCK TESTS PASSED (happy path + error recovery + sessions + streaming"
          " + assembly + convergence + plan mode)")


if __name__ == "__main__":
    main()
