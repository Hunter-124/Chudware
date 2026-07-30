"""Regression: the general lane must be able to REACH mission_start.

Live 2026-07-29 (case bench, kimi-code/k3, run-30513252767546): the user asked for
a custom OptiPlex 7040 case. The model published a task_list, then reasoned
"manifest shape - unknown schema, but likely {deliverables: [...]}", never risked
the mission_start call, narrated its next step twice instead, and the turn ended
`no_code` with 28 of its 30 tool calls unspent.

Two harness defects made that outcome inevitable:

1. ``mission_start``'s no-manifest refusal named ``run_mission(request, provider,
   intake_fn=...)`` — a Python API no model can call — and never stated the §16
   manifest shape. There was no reachable path from the refusal to a valid call.
2. In ``run_turn``, the fabricated-finish gate ran BEFORE the "you described the
   step instead of taking it" push, so a mid-thought NARRATION (which claims
   nothing) consumed the fabrication gate's single nudge; the next narration then
   ended the turn.
"""
from __future__ import annotations

import json

from services.agent.pipeline.manifest import (
    KNOWN_CHECK_TOOLS,
    VALID_KINDS,
    blocking_ambiguities,
    manifest_template,
    manifest_valid,
    validate_manifest,
)


def _mission_start():
    from services.mcp.mission_tools import register_mission_tools

    class _Reg:
        def __init__(self):
            self.tools = {}

        def tool(self):
            def deco(fn):
                self.tools[fn.__name__] = fn
                return fn
            return deco

    reg = _Reg()
    register_mission_tools(reg)
    return reg.tools["mission_start"]


# ---------------------------------------------------------------------------
# 1. the advertised template must actually be usable
# ---------------------------------------------------------------------------

def test_manifest_template_validates_clean():
    """A template the harness hands a model MUST pass its own validator.

    Guards the template against a new §16 rule silently making the advertised
    shape unusable — which would be the original bug with extra steps.
    """
    findings = validate_manifest(manifest_template())
    assert findings == [], [str(f) for f in findings]
    assert manifest_valid(findings)
    assert blocking_ambiguities(findings) == []


def test_manifest_template_compiles_a_mission_graph():
    from services.dag.mission_template import build_mission_graph

    graph = build_mission_graph(manifest_template(), graph_id="t")
    assert graph.get("nodes"), graph


def test_no_manifest_refusal_is_actionable():
    """The refusal must carry the shape, not point at an uncallable Python API."""
    out = _mission_start()(request="build me a PC case")
    assert out["ok"] is False
    tpl = out.get("manifest_template")
    assert isinstance(tpl, dict) and tpl.get("deliverables"), out
    assert validate_manifest(tpl) == []
    # The legal vocabularies must ride along: a model cannot guess either set.
    assert set(out.get("valid_kinds") or []) == set(VALID_KINDS)
    assert set(out.get("known_check_tools") or []) == set(KNOWN_CHECK_TOOLS)
    # And it must not send the model after an API it cannot reach.
    assert "run_mission(" not in out["reason"]


def test_invalid_manifest_refusal_also_carries_the_template():
    out = _mission_start()(request="x", manifest={"deliverables": []})
    assert out["ok"] is False
    assert validate_manifest(out["manifest_template"]) == []


def test_filled_template_actually_plans_the_mission():
    """End to end: bare call -> template -> filled -> compiled graph."""
    ms = _mission_start()
    tpl = ms(request="build me a PC case")["manifest_template"]
    tpl["request_verbatim"] = "build me a PC case"
    dl = tpl["deliverables"][0]
    dl["title"] = "Case shell"
    dl["acceptance_criteria"][0]["text"] = "the shell is a closed solid"
    dl["acceptance_criteria"][0]["check"]["pass_rule"] = "mass.volume_mm3 > 0"
    out = ms(request="build me a PC case", manifest=tpl)
    assert out["ok"] is True, out
    assert out["graph"]["nodes"]
    assert out["deliverables"] == ["DL1"]


def test_prompt_note_tells_the_model_it_may_author_the_manifest():
    """The note the model reads must not read as a dead end.

    The live reasoning quoted the OLD note verbatim before giving up on the call.
    """
    from services.agent.tool_guidance.mission import ENTRIES

    note = ENTRIES["mission_start"]["prompt_note"]
    assert "template" in note.lower(), note
    assert "yourself" in note.lower(), note


# ---------------------------------------------------------------------------
# 2. a mid-thought narration must not spend the fabricated-finish nudge
# ---------------------------------------------------------------------------

class _ScriptedProvider:
    name = "kimi-code"
    model = "k3"

    def __init__(self, replies):
        self._replies = list(replies)
        self.i = 0
        self.seen: list[str] = []

    def complete(self, system, messages, max_tokens=8000):
        # Record the harness's steering so a test can assert WHICH nudge fired.
        for m in messages:
            content = m.get("content")
            if isinstance(content, list) and content:
                self.seen.append(str(content[0].get("text") or ""))
            elif isinstance(content, str):
                self.seen.append(content)
        r = self._replies[min(self.i, len(self._replies) - 1)]
        self.i += 1
        return r


_TASK_LIST = (
    '```json\n{"tool": "task_list", "args": {"title": "case", '
    '"items": [{"id": "1", "title": "plan", "status": "running"}]}}\n```'
)
#: Verbatim from the live run: the reply that killed the turn.
_NARRATION = (
    "Task list is live. Now compiling the monolithic mission DAG with a full "
    "deliverable manifest."
)


def _run(replies, tools_result=None):
    from services.agent import agent_turn as at

    p = _ScriptedProvider(replies)
    calls: list[str] = []

    def dispatch(tool, args):
        calls.append(tool)
        return tools_result if tools_result is not None else {"ok": True}

    res = at.run_turn(
        "design me a custom PC case for a Dell OptiPlex 7040",
        p, tool_dispatch=dispatch,
        tools_text="task_list, mission_start, new_part",
        max_tool_calls=30, max_tokens=8192,
    )
    return res, calls, p


def test_narration_after_meta_tools_gets_the_continue_nudge_not_the_work_nudge():
    """The live-fatal sequence: task_list (meta) then a narration.

    Pre-fix the fabricated-finish gate answered first with [NO WORK PERFORMED]
    and a second narration ended the turn. The narration must instead be pushed
    to TAKE the step, leaving the fabrication gate's nudge unspent.
    """
    res, calls, p = _run([_TASK_LIST, _NARRATION, _NARRATION])
    steer = "\n".join(p.seen)
    assert "[UNFINISHED]" in steer, steer[-800:]
    assert "task_list" in calls
    # A narration is never accepted as a finish, whichever gate saw it.
    assert res.success is False, (res.stop_reason, res.result)

def test_narration_then_real_work_finishes_instead_of_no_code():
    """The recovery the live turn never got: the push lands and work happens.

    Asserted on the OUTCOME that was broken — real work ran and the turn did not
    die `no_code` — not on `success`, which a later domain guardrail
    (verify_battery et al.) may legitimately withhold for its own reasons.
    """
    res, calls, _ = _run([
        _TASK_LIST,
        _NARRATION,
        '```json\n{"tool": "new_part", "args": {"name": "case_shell"}}\n```',
        '{"done": true, "handoff": {"status": "done", '
        '"summary": "case_shell created", "outputs": {"doc_id": "doc-1"}}}',
    ], tools_result={"ok": True, "doc_id": "doc-1"})
    assert "new_part" in calls, calls
    assert res.stop_reason != "no_code", (res.stop_reason, res.result)

def test_repeated_identical_narration_still_terminates():
    """The nudge allowance must not become a loop.

    An identical reply proves the push is not landing, so it falls through to the
    honest-finish gates rather than being re-asked until the budget dies.
    """
    res, calls, p = _run([_TASK_LIST] + [_NARRATION] * 12)
    assert res.success is False
    assert res.stop_reason == "no_code", (res.stop_reason, res.result)
    # Bounded: far fewer completions than the 30-call tool budget.
    assert p.i <= 8, p.i
    ho = (res.result or {}).get("handoff") or {}
    assert ho.get("status") != "done", ho


# ---------------------------------------------------------------------------
# 3. a trailing-colon promise is not delivered content
# ---------------------------------------------------------------------------

def test_trailing_colon_promise_is_not_a_finish():
    """Live 2026-07-29, second defect found while proving the first.

    After mission_start REJECTED its manifest, K3 replied "Manifest rejected -
    fixing schema with `kind`, `acceptance_criteria`, and `request_verbatim`:"
    and the turn reported success=True stop_reason=confirmed having built
    nothing: the colon rule assumed a colon introduces delivered content, but
    this one had no payload after it.
    """
    from services.agent.agent_turn import _announces_next_step as promise

    assert promise("Manifest rejected - fixing schema with `kind`, "
                   "`acceptance_criteria`, and `request_verbatim`:")
    # A colon WITH a payload still delivers — that exemption must survive.
    assert not promise("I'll explain: the bore is 12 mm H7 because the shaft "
                       "is 12 g6.")
    assert not promise("Here are the results: volume 1.2e5 mm^3, mass 940 g.")


def test_planning_narration_is_recognised_as_a_promise():
    """The verbatim live reply that ended the case-bench turn as no_code."""
    from services.agent.agent_turn import _announces_next_step as promise

    assert promise("Now compiling the monolithic mission DAG with a full "
                   "deliverable manifest.")
    assert promise("Task list is live. Now compiling the mission DAG.")
    # Reports still read as delivery.
    assert not promise("Done. The case shell is 305 x 210 x 180 mm, 2 mm wall.")


def test_promise_detector_never_blocks_a_real_answer():
    """The false-positive budget is ONE extra nudge, never a rejected answer.

    Documented tradeoff, measured not assumed: a report that reuses a planning
    gerund AND the word "now" ("Compiling the report took three passes and the
    numbers now agree.") does read as a promise. The cost is capped at one
    [UNFINISHED] push - the model's next reply still finishes the turn - because
    the promise branch only ever nudges and continues.
    """
    from services.agent.agent_turn import _announces_next_step as promise

    report = "Compiling the report took three passes and the numbers now agree."
    assert promise(report)  # the known, accepted over-detection

    # ... and it costs exactly one nudge: real work happened, so the very next
    # reply is accepted rather than the turn dying.
    res, calls, p = _run([
        '```json\n{"tool": "new_part", "args": {"name": "shell"}}\n```',
        report,
        "Done. The shell is a closed solid, 305 x 210 x 180 mm, 2 mm wall.",
    ], tools_result={"ok": True, "doc_id": "doc-1"})
    assert "new_part" in calls, calls
    assert res.stop_reason != "no_code", (res.stop_reason, res.result)
    assert "[UNFINISHED]" in "\n".join(p.seen)


# ---------------------------------------------------------------------------
# 4. the mission path must be EXECUTABLE, not just plannable
# ---------------------------------------------------------------------------

def test_mission_execution_tools_are_reachable():
    """`mission_start`'s success hint names dag_run — the lane must be able to get it.

    Live 2026-07-29 (case bench): mission_start compiled a 7-node graph, then the
    model said "I need dag_run to execute it, which isn't in my current loadout"
    — correctly: dag_run/dag_approve/dag_abort were in NO category pack and in no
    base loadout, so the whole mission path dead-ended at a graph nobody could
    run. All three bind fine; only the advertisement was missing.
    """
    from services.agent import agent_turn as at
    from services.agent.tool_categories import CATEGORY_PACKS, resolve_categories

    delegate = resolve_categories(["delegate"])
    for tool in ("dag_run", "dag_approve", "dag_abort"):
        assert tool in delegate, f"{tool} unreachable via any pack"
        # And it must actually resolve, or the advertisement is the other kind of lie.
        assert callable(at._bind_tool(tool)), tool
    # Reachability must survive the pack cap.
    assert len(delegate) <= 40, len(delegate)
    assert "dag_run" in CATEGORY_PACKS["delegate"]


def test_mission_start_hint_says_how_to_reach_dag_run():
    ms = _mission_start()
    tpl = ms(request="x")["manifest_template"]
    tpl["request_verbatim"] = "x"
    dl = tpl["deliverables"][0]
    dl["title"] = "t"
    dl["acceptance_criteria"][0]["text"] = "volume positive"
    dl["acceptance_criteria"][0]["check"]["pass_rule"] = "volume_mm3 > 0"
    hint = ms(request="x", manifest=tpl)["hint"]
    assert "dag_run" in hint
    assert "enable_tools" in hint and "delegate" in hint, hint
