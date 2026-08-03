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


def test_invalid_manifest_refusal_also_carries_a_worked_shape():
    """The refusal must show what right looks like, not just what is wrong.

    This used to assert a generic `manifest_template`. `manifest_draft` supersedes
    it: same guarantee, but expressed as the CALLER'S OWN manifest with the blanks
    marked, so the next completion edits its work instead of re-deriving it.
    """
    out = _mission_start()(request="x", manifest={"deliverables": []})
    assert out["ok"] is False
    draft = out["manifest_draft"]
    assert isinstance(draft, dict) and draft["schema"] == "aicad.deliverables/1"
    # The legal vocabularies still ride along -- a model cannot guess either set.
    assert set(out["valid_kinds"]) == set(VALID_KINDS)
    assert set(out["known_check_tools"]) == set(KNOWN_CHECK_TOOLS)


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

    # Live run run-25836956409821: the final checklist is a promise despite
    # beginning with a noun phrase, not one of the action-verb openers.
    assert promise("DFM passes now (1.09 mm min web). Final look + verify "
                   "battery + export:")

    # Delivered colon payloads remain complete answers, not trailing promises.
    assert not promise("DFM passes now (1.09 mm min web). Final look: battery "
                       "verified and export saved to /tmp/case.step.")


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


#: Verbatim from live run-22629074806645 (K3): after real research + fs_write the
#: model promised the build and the turn was reported CONFIRMED with nothing made.
_PROMISE_AFTER_WORK = (
    "Reference doc saved. Now updating the task list and starting the model."
)


def test_promise_detector_sees_a_subject_dropped_coordinated_tail():
    """Verbatim from live probe-holster-1 (K3, general lane).

    The promise sits after "and", so every anchored opener missed it and the
    turn finished CONFIRMED after three calls with no part. The table pins both
    directions: coordinated promises detected, ordinary reports left alone.
    """
    from services.agent.agent_turn import _announces_next_step as promise

    assert promise(
        "I've confirmed the Ruger LCP 3701 specs and will now load the "
        "modeling packs to create the part.")
    assert promise("Specs confirmed, now loading the modeling packs.")
    assert promise(
        "Reference doc saved. Now updating the task list and starting the model.")

    # Reports that merely CONTAIN a future tense or a coordinating conjunction
    # must still be accepted as answers.
    assert not promise(
        "The bracket is done and will fit the rail with 0.4 mm clearance.")
    assert not promise("I measured the bore and the wall is 2.4 mm thick.")
    assert not promise(
        "The holster will need a retention ridge, which I added as f6.")
    assert not promise(
        "Exported /tmp/holster.stl and verified the mesh is manifold.")


def test_promise_detector_sees_a_temporal_lead_in():
    """Verbatim from live probe-holster-6: "Now let me …" finished the turn CONFIRMED
    because every opener was anchored and `now` pushed the promise one word right."""
    from services.agent.agent_turn import _announces_next_step as promise

    assert promise("Battery is clean — all applicable checks score 1.0. "
                   "Now let me confirm the finished dimensions.")
    assert promise("Next let me check it.")
    assert promise("Then I'll cut the pocket.")

    # The same lead-ins in a REPORT stay answers.
    assert not promise("Now the part measures 131 x 26 x 91 mm.")
    assert not promise("Finally the wall came out at 2.4 mm.")
    assert not promise(
        "First the shell, then the cavity: both are in the tree as f1 and f2.")


def _run_allowed(replies, allowed):
    """Like _run, but with the allowlist the live lane actually carries."""
    from services.agent import agent_turn as at

    p = _ScriptedProvider(replies)
    calls: list[str] = []

    def dispatch(tool, args):
        calls.append(tool)
        return {"ok": True, "doc_id": "doc-1"}

    res = at.run_turn(
        "design me a custom PC case for a Dell OptiPlex 7040",
        p, tool_dispatch=dispatch,
        tools_text=", ".join(allowed), allowed_tools=list(allowed),
        max_tool_calls=30, max_tokens=8192,
    )
    return res, calls, p


def test_repeated_promise_is_escalated_then_refused_not_banked_as_success():
    res, calls, p = _run_allowed(
        [
            '```json\n{"tool": "fs_write", "args": {"path": "ref.md", '
            '"content": "specs"}}\n```',
            _PROMISE_AFTER_WORK,
            _PROMISE_AFTER_WORK,
            _PROMISE_AFTER_WORK,
        ],
        ["task_list", "fs_write", "new_part"],
    )
    steer = "\n".join(p.seen)
    assert "[UNFINISHED]" in steer, steer[-800:]
    # The identical-reply guard used to fall through to a bare handoff, so the
    # promise itself became the answer. It must escalate with the vocabulary...
    assert "[STILL UNFINISHED]" in steer, steer[-800:]
    assert "new_part" in steer.split("[STILL UNFINISHED]")[1]
    # ...and then end honestly rather than claiming the work was done.
    assert res.success is False, (res.stop_reason, res.result)
    assert res.stop_reason == "no_code", res.stop_reason


def test_blank_finish_after_real_work_is_never_a_confirmed_answer():
    """An empty reply used to be accepted as a done handoff with an empty summary."""
    res, _calls, p = _run(
        [
            '```json\n{"tool": "new_part", "args": {"name": "shell"}}\n```',
            "",
            "",
            "",
        ],
        tools_result={"ok": True, "doc_id": "doc-1"},
    )
    assert res.success is False, (res.stop_reason, res.result)
    assert "[STILL UNFINISHED]" in "\n".join(p.seen)


def test_escalation_still_accepts_the_answer_that_follows_it():
    """The escalation must remain a push, never a gate that rejects real delivery."""
    res, calls, p = _run(
        [
            '```json\n{"tool": "new_part", "args": {"name": "shell"}}\n```',
            _PROMISE_AFTER_WORK,
            _PROMISE_AFTER_WORK,
            "Done. The shell is a closed solid, 305 x 210 x 180 mm, 2 mm wall.",
        ],
        tools_result={"ok": True, "doc_id": "doc-1"},
    )
    assert "[STILL UNFINISHED]" in "\n".join(p.seen)
    # The delivered report is NOT refused as another promise. (A modeling done
    # envelope can still be held by the verify_battery guardrail — a different,
    # deliberate gate — so assert only that the promise path released it.)
    assert res.stop_reason != "no_code", (res.stop_reason, res.result)

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


# ===========================================================================
# Bounce on meaning, never on boilerplate.
#
# Live 2026-07-30 (run-4530697933837, OptiPlex case, K3): the model authored a
# reasonable manifest and got eight errors back. NOT ONE named the real problem.
# Six were "deliverable at index N is missing an 'id'" -- unsatisfiable, because
# the entries were strings and you cannot add a key to a string -- and the
# validator's isinstance(d, dict) guard then skipped the kind/acceptance_criteria
# rules entirely, so the findings never mentioned them. The other two were a
# constant and a value already passed in as `request`. The mission never started.
# ===========================================================================

_LIVE_REQUEST = (
    "Design a manufacturable custom case for a Dell OptiPlex 7040 SFF that reuses "
    "the stock PSU (8.5 x 2.5 x 3.25 in), fits the proprietary motherboard and a "
    "full-profile GPU with riser."
)
# Verbatim from the run's args_preview.
_LIVE_MANIFEST = {
    "deliverables": [
        "component dimensions and clearance assumptions",
        "enclosure layout plan",
        "parametric case CAD",
        "fabrication plan for CNC and 3D printed parts",
        "prefab BOM under $40",
        "assembly and fit-check notes",
    ],
    "constraints": ["reuse stock PSU", "fit full-profile GPU"],
}


def test_mechanical_fields_are_repaired_not_bounced():
    from services.agent.pipeline import normalize_manifest

    m, interpreted = normalize_manifest(_LIVE_MANIFEST, request=_LIVE_REQUEST)
    assert m["schema"] == "aicad.deliverables/1"
    assert m["request_verbatim"] == _LIVE_REQUEST
    assert [d["id"] for d in m["deliverables"]] == [f"DL{i}" for i in range(1, 7)]
    assert all(isinstance(d, dict) for d in m["deliverables"])
    assert m["deliverables"][0]["title"].startswith("component dimensions")
    # Every repair is disclosed -- a silent fill is how a mission drifts from the ask.
    assert len(interpreted) == 4, interpreted
    assert any("request_verbatim" in n and "paraphrase" in n for n in interpreted)


def test_normalizing_leaves_only_semantic_findings():
    """The whole point: what survives normalization is work only the model can do."""
    from services.agent.pipeline import normalize_manifest

    before = validate_manifest(_LIVE_MANIFEST)
    assert not any(f.rule in ("kind", "acceptance_criteria") for f in before), (
        "guard: pre-normalization findings never reached the real problem")

    m, _ = normalize_manifest(_LIVE_MANIFEST, request=_LIVE_REQUEST)
    after = validate_manifest(m)
    assert after, "still incomplete: kind/acceptance_criteria carry meaning"
    assert {f.rule for f in after} == {"kind", "acceptance_criteria"}, \
        [str(f) for f in after]


def test_string_deliverable_is_reported_as_a_shape_error():
    """Never ask a model to add a key to a string."""
    findings = validate_manifest({
        "schema": "aicad.deliverables/1", "request_verbatim": "x",
        "deliverables": ["a bracket"]})
    shape = [f for f in findings if f.rule == "deliverable_shape"]
    assert shape, [str(f) for f in findings]
    assert "not an object" in str(shape[0])
    assert not [f for f in findings if f.rule == "deliverable_id"]


def test_draft_returns_the_callers_own_deliverables_with_blanks_marked():
    ms = _mission_start()
    out = ms(request=_LIVE_REQUEST, manifest=_LIVE_MANIFEST)
    assert out["ok"] is False
    draft = out["manifest_draft"]
    # Its OWN six deliverables, not a generic one-item template to re-derive.
    assert len(draft["deliverables"]) == 6
    assert draft["deliverables"][2]["title"] == "parametric case CAD"
    dl = draft["deliverables"][0]
    assert dl["kind"].startswith("<REQUIRED:") and "part" in dl["kind"]
    assert dl["acceptance_criteria"][0]["check"]["tool"].startswith("<REQUIRED:")
    assert out["interpreted"] and out["valid_kinds"]


def test_a_manifest_needing_only_mechanical_repair_now_starts():
    """The payoff: boilerplate-only gaps must no longer cost a round trip."""
    ms = _mission_start()
    tpl = manifest_template()
    del tpl["schema"]                     # constant
    del tpl["request_verbatim"]           # already passed as `request`
    del tpl["deliverables"][0]["id"]      # positional
    dl = tpl["deliverables"][0]
    dl["title"] = "Case shell"
    dl["acceptance_criteria"][0]["text"] = "volume positive"
    dl["acceptance_criteria"][0]["check"]["pass_rule"] = "volume_mm3 > 0"

    out = ms(request="build me a PC case", manifest=tpl)
    assert out["ok"] is True, out.get("findings")
    assert out["deliverables"] == ["DL1"]
    assert out["interpreted"], "an auto-filled request_verbatim must be disclosed"


def test_draft_marks_a_criterion_that_names_no_check_tool():
    """The observed SECOND bounce (run-4433662287777): criteria written as prose.

    "DL1/AC1: criterion must name a 'check' with a tool" x4 says what is missing but
    not where to put it; the draft marks the gap in the model's own criterion.
    """
    from services.agent.pipeline import manifest_draft

    d = manifest_draft({
        "schema": "aicad.deliverables/1", "request_verbatim": "x",
        "deliverables": [{
            "id": "DL1", "kind": "part", "title": "Case shell",
            "acceptance_criteria": [{"id": "AC1", "text": "fits the PSU"}]}]})
    chk = d["deliverables"][0]["acceptance_criteria"][0]["check"]
    assert chk["tool"].startswith("<REQUIRED:") and "measure_part" in chk["tool"]
    assert chk["pass_rule"]
    # The model's own criterion text survives -- this is an edit, not a rewrite.
    assert d["deliverables"][0]["acceptance_criteria"][0]["text"] == "fits the PSU"


def test_draft_leaves_a_complete_criterion_untouched():
    from services.agent.pipeline import manifest_draft

    good = manifest_template()
    assert manifest_draft(good) == good
