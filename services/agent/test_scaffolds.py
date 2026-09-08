"""test_scaffolds.py — capability dial wiring (WI-2/3/4/6-extras/7/8/10 + WI-17).

Mock provider + ledger only. For each dial: A fires / C never fires.
Plus C-freedom snapshot: zero forced capability_dial events on a mock C turn.
"""
from __future__ import annotations

from services.agent import agent_turn
from services.agent import guardrails as gr
from services.agent import scaffolds as sc
from services.agent.capability import profile_for_letter


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

class ListLedger:
    def __init__(self):
        self.events = []

    def append(self, actor, kind, payload):
        ev = {"actor": actor, "kind": kind, "payload": payload}
        self.events.append(ev)
        return ev


class ScriptedProvider:
    name = "scripted"

    def __init__(self, replies):
        self._replies = list(replies)

    def complete(self, system, messages, max_tokens=8000):
        return self._replies.pop(0) if self._replies else '{"done":true,"handoff":{}}'


def _kinds(ledger, kind):
    return [e for e in ledger.events if e["kind"] == kind]


def _dials(ledger, dial=None):
    out = _kinds(ledger, "capability_dial")
    if dial is not None:
        out = [e for e in out if e["payload"].get("dial") == dial]
    return out


def _forced(ledger):
    return [e for e in _kinds(ledger, "capability_dial")
            if e["payload"].get("forced")]


def _tool_json(tool, args=None):
    import json
    return "```json\n" + json.dumps({"tool": tool, "args": args or {}}) + "\n```"


def _done_json(handoff=None):
    import json
    return "```json\n" + json.dumps({
        "done": True,
        "handoff": handoff or {
            "waivers": [{"check": "verify_battery", "reason": "test"}]
        },
    }) + "\n```"


# ===========================================================================
# WI-2 — verification cadence
# ===========================================================================

def test_wi2_a_auto_verify_after_every_mutation():
    ledger = ListLedger()
    cap = profile_for_letter("A")
    # new_part is mutating but not dimensioned (avoids WI-7 calc gate) and
    # not aggressive-checkpoint (avoids WI-6 A fillet/hole path).
    provider = ScriptedProvider([
        _tool_json("new_part", {"name": "boss"}),
        _done_json(),
    ])
    seen = []

    def dispatch(tool, args):
        seen.append(tool)
        return {"ok": True, "tool": tool}

    res = agent_turn.run_turn(
        "build a boss", provider,
        allowed_tools=["new_part", "describe_part", "check_sketch", "verify_battery"],
        tool_dispatch=dispatch, ledger=ledger, actor="feature_modeler",
        capability=cap, phase="build", max_tool_calls=6,
    )
    assert res.success is True
    assert "describe_part" in seen  # auto-inject after mutation
    assert any(e["payload"].get("tool") == "describe_part"
               for e in _kinds(ledger, "auto_verify_inject"))
    assert _dials(ledger, "verify_cadence")
    assert all(e["payload"].get("forced") for e in _dials(ledger, "verify_cadence"))

def test_wi2_b_injects_only_at_feature_group_boundary():
    ledger = ListLedger()
    cap = profile_for_letter("B")
    # sketch ops first (no boundary yet with prev=None), then solid extrude (boundary).
    provider = ScriptedProvider([
        _tool_json("add_features", {
            "doc_id": "d1",
            "ops": [{"op": "sketch_circle"}],
        }),
        _tool_json("add_features", {
            "doc_id": "d1",
            "ops": [{"op": "extrude", "params": {"depth": 5}}],
        }),
        _done_json(),
    ])
    seen = []

    def dispatch(tool, args):
        seen.append(tool)
        return {"ok": True}

    agent_turn.run_turn(
        "sketch then extrude", provider,
        allowed_tools=["add_features", "describe_part", "check_sketch"],
        tool_dispatch=dispatch, ledger=ledger, actor="feature_modeler",
        capability=cap, phase="build", max_tool_calls=8,
    )
    # First mutation (sketch, prev=None) should NOT inject; second (sketch→feature) should.
    injects = _kinds(ledger, "auto_verify_inject")
    assert len(injects) >= 1
    assert _dials(ledger, "verify_cadence")


def test_wi2_c_never_auto_verifies():
    ledger = ListLedger()
    cap = profile_for_letter("C")
    provider = ScriptedProvider([
        _tool_json("add_features", {
            "doc_id": "d1",
            "ops": [{"op": "extrude", "params": {"depth": 10}}],
        }),
        _done_json(),
    ])
    seen = []

    def dispatch(tool, args):
        seen.append(tool)
        return {"ok": True}

    agent_turn.run_turn(
        "build freeform", provider,
        allowed_tools=["add_features", "describe_part"],
        tool_dispatch=dispatch, ledger=ledger, actor="feature_modeler",
        capability=cap, phase="build", max_tool_calls=6,
    )
    assert seen == ["add_features"]  # no describe_part inject
    assert _kinds(ledger, "auto_verify_inject") == []
    assert _dials(ledger, "verify_cadence") == []


# ===========================================================================
# WI-3 — plan format
# ===========================================================================

def test_wi3_a_rigid_missing_slot_fails():
    cap = profile_for_letter("A")
    plan = {"name": "x", "parts": [{"name": "p", "dimensions": "10x10x5",
                                    "features": ["hole"]}],
            # missing dimensions/datums/build_order/risks top-level
            }
    errs = sc.validate_plan_slots(plan, cap)
    assert errs
    assert any("datums" in e or "build_order" in e or "dimensions" in e
               for e in errs)


def test_wi3_a_rigid_complete_passes():
    cap = profile_for_letter("A")
    plan = {
        "name": "bushing",
        "parts": [{"name": "body", "dimensions": {"od": 20, "id": 10, "h": 15},
                   "features": ["bore 10"]}],
        "dimensions": {"od": 20, "id": 10, "height": 15},
        "datums": [{"id": "A", "kind": "axis"}],
        "build_order": ["revolve", "fillet"],
        "risks": ["thin wall"],
    }
    assert sc.validate_plan_slots(plan, cap) == []


def test_wi3_b_structured_requires_fields():
    cap = profile_for_letter("B")
    assert sc.validate_plan_slots({"name": "x"}, cap)
    ok = {
        "datums": [{"id": "A"}],
        "build_order": ["base", "cut"],
        "risks": [],
    }
    assert sc.validate_plan_slots(ok, cap) == []


def test_wi3_c_freeform_accepts_minimal_and_no_template_in_prompt():
    cap = profile_for_letter("C")
    assert sc.validate_plan_slots({"parts": [{"name": "p"}]}, cap) == []
    assert sc.validate_plan_slots(None, cap) == []
    prompt = sc.plan_prompt_for(cap, "BASE")
    assert sc.RIGID_PLAN_MARKER not in prompt
    assert sc.STRUCTURED_PLAN_MARKER not in prompt
    assert sc.playbook_enforcement_for(cap) == "off"


def test_wi3_a_prompt_contains_rigid_marker():
    cap = profile_for_letter("A")
    prompt = sc.plan_prompt_for(cap)
    assert sc.RIGID_PLAN_MARKER in prompt
    assert sc.playbook_enforcement_for(cap) == "step_locked"


# ===========================================================================
# WI-4 — phase caps
# ===========================================================================

def test_wi4_a_phase_cap_is_subagent_soft_warning():
    ledger = ListLedger()
    cap = profile_for_letter("A")
    # A intake derives a soft threshold of 2. A third proposal is replaced by
    # the convergence warning, then the sub-agent still dispatches later work.
    provider = ScriptedProvider([
        _tool_json("measure_part", {}),
        _tool_json("measure_part", {}),
        _tool_json("measure_part", {}),  # replaced by the one warning
        _tool_json("measure_part", {}),
        _done_json(),
    ])

    def dispatch(tool, args):
        return {"ok": True}

    res = agent_turn.run_turn(
        "measure then converge", provider,
        allowed_tools=["measure_part"],
        tool_dispatch=dispatch, ledger=ledger, actor="reviewer",
        capability=cap, phase="intake",
        max_tool_calls=50,
        subagent_id="dg_phase_soft",
    )
    assert res.stop_reason == "confirmed"
    assert len(res.result["calls"]) == 3
    dials = _dials(ledger, "phase_caps")
    assert dials
    assert dials[0]["payload"].get("forced") is True

def test_wi4_c_has_only_derived_soft_warning():
    ledger = ListLedger()
    cap = profile_for_letter("C")
    provider = ScriptedProvider([
        *[_tool_json("measure_part", {}) for _ in range(6)],
        _tool_json("measure_part", {}),  # first proposal after warning
        _done_json(),
    ])

    def dispatch(tool, args):
        return {"ok": True}

    res = agent_turn.run_turn(
        "measure", provider,
        allowed_tools=["measure_part"],
        tool_dispatch=dispatch, ledger=ledger, actor="reviewer",
        capability=cap, phase="build", max_tool_calls=5,
        subagent_id="dg_no_phase_cap",
    )
    assert res.stop_reason == "confirmed"
    assert _dials(ledger, "phase_caps") == []
    assert len(res.result["calls"]) == 6


# ===========================================================================
# WI-6 A-extras — aggressive checkpoint scope
# ===========================================================================
def test_wi6_a_checkpoint_on_fillet():
    ledger = ListLedger()
    cap = profile_for_letter("A")
    ckpts = []

    def ckpt_fn(*_a, **kwargs):
        ckpts.append(kwargs or _a)
        return {"ok": True, "checkpoint": "c1"}

    msg = sc.pre_dispatch_aggressive_checkpoint(
        "add_features",
        {"doc_id": "d1", "ops": [{"op": "add_feature",
                                  "feature": {"type": "fillet", "params": {"radius": 1}}}]},
        profile=cap, ledger=ledger, actor="feature_modeler",
        checkpoint_fn=ckpt_fn,
    )
    assert msg is None  # ok, checkpoint succeeded
    assert ckpts  # checkpoint was taken
    assert _dials(ledger, "auto_checkpoint")
    assert _kinds(ledger, "auto_checkpoint")  # guardrails emit too


def test_wi6_aggressive_checkpoint_failure_blocks_once_then_waives():
    ledger = ListLedger()
    cap = profile_for_letter("A")
    gr.reset_checkpoint_failures()
    args = {
        "doc_id": "permanent-failure",
        "ops": [{"op": "add_feature",
                 "feature": {"type": "fillet", "params": {"radius": 1}}}],
    }

    def fail_checkpoint(*_args):
        return {"ok": False, "error": "checkpoint backend unavailable"}

    first = sc.pre_dispatch_aggressive_checkpoint(
        "add_features", args, profile=cap, ledger=ledger,
        actor="feature_modeler", checkpoint_fn=fail_checkpoint,
    )
    second = sc.pre_dispatch_aggressive_checkpoint(
        "add_features", args, profile=cap, ledger=ledger,
        actor="feature_modeler", checkpoint_fn=fail_checkpoint,
    )

    assert first is not None and "blocked" in first
    assert "Retry the same call once" in first
    assert second is None
    waivers = [
        event for event in _kinds(ledger, "hard_guardrail")
        if event["payload"].get("name") == "auto_checkpoint"
        and event["payload"].get("outcome") == "waived"
    ]
    assert waivers
    assert "UNPROTECTED" in waivers[-1]["payload"]["reason"]
    gr.reset_checkpoint_failures()

def test_wi6_c_no_aggressive_on_fillet():
    ledger = ListLedger()
    cap = profile_for_letter("C")
    ckpts = []

    def ckpt_fn(**kwargs):
        ckpts.append(kwargs)
        return {"ok": True}

    msg = sc.pre_dispatch_aggressive_checkpoint(
        "add_features",
        {"doc_id": "d1", "ops": [{"op": "add_feature",
                                  "feature": {"type": "fillet", "params": {"radius": 1}}}]},
        profile=cap, ledger=ledger, actor="feature_modeler",
        checkpoint_fn=ckpt_fn,
    )
    assert msg is None
    assert ckpts == []
    assert _dials(ledger, "auto_checkpoint") == []


# ===========================================================================
# WI-7 — calc gate
# ===========================================================================

def test_wi7_a_rejects_dimensioned_without_calc():
    ledger = ListLedger()
    cap = profile_for_letter("A")
    provider = ScriptedProvider([
        _tool_json("add_features", {
            "doc_id": "d1",
            "ops": [{"op": "hole", "params": {"diameter": 5.5, "depth": 10}}],
        }),
        _done_json(),
    ])

    def dispatch(tool, args):
        return {"ok": True}

    res = agent_turn.run_turn(
        "add hole", provider,
        allowed_tools=["add_features", "fit_calc"],
        tool_dispatch=dispatch, ledger=ledger, actor="feature_modeler",
        capability=cap, phase="build", max_tool_calls=4,
    )
    # First call blocked by calc gate
    assert res.result["calls"]
    first = res.result["calls"][0]
    assert first["ok"] is False
    assert "calc" in str(first["result"]).lower() or "calc" in str(first.get("error", "")).lower()


def test_wi7_dimensioned_mutator_roles_never_hit_an_unsatisfiable_hard_gate():
    from services.agent.roles import load_all_roles
    from services.agent.tool_categories import CATEGORY_PACKS

    dimensioned_mutators = {
        "add_features", "doc_apply_ops", "hole", "counterbore",
        "countersink", "bolt_circle",
    }
    research_pack = set(CATEGORY_PACKS["research"])
    assert research_pack.intersection(sc.CALC_TOOLS) == {
        "list_calcs", "research_eng_calc", "research_unit_convert",
    }

    checked = set()
    args = {"ops": [{"op": "extrude", "params": {"depth": 12}}]}
    for role_id, card in load_all_roles().items():
        allowed = set(card.concrete_tools)
        if not allowed.intersection(dimensioned_mutators):
            continue
        checked.add(role_id)
        if allowed.intersection(sc.CALC_TOOLS):
            continue
        ledger = ListLedger()
        message = sc.pre_dispatch_calc_gate(
            "add_features", args, profile=profile_for_letter("A"),
            ledger=ledger, actor=role_id, effective_allow_set=allowed,
        )
        assert message is None, f"{role_id} was hard-blocked without a calc remedy"
        advisory = _dials(ledger, "math_scratchpad")
        assert advisory
        assert advisory[-1]["payload"]["forced"] is False
        assert advisory[-1]["payload"]["reason"] == "no_callable_calc"
        assert "enable_tools(categories=['research'])" in (
            advisory[-1]["payload"]["remedy"]
        )

    assert checked == {
        "feature_modeler", "fixer", "model3d",
        "re_modeler", "sketch2d", "sketcher",
    }

def test_wi7_a_allows_after_calc():
    ledger = ListLedger()
    cap = profile_for_letter("A")
    # Use extrude (dimensioned, not aggressive-checkpoint) so only the calc gate applies.
    provider = ScriptedProvider([
        _tool_json("fit_calc", {"nominal": 20, "fit": "H7/g6"}),
        _tool_json("add_features", {
            "doc_id": "d1",
            "ops": [{"op": "extrude", "params": {"depth": 12}}],
        }),
        _done_json(),
    ])
    seen = []

    def dispatch(tool, args):
        seen.append(tool)
        return {"ok": True, "answer": {"clearance": [0.01, 0.04]}}

    res = agent_turn.run_turn(
        "calc then extrude", provider,
        allowed_tools=["add_features", "fit_calc", "describe_part"],
        tool_dispatch=dispatch, ledger=ledger, actor="geometry_designer",
        capability=cap, phase="specify", max_tool_calls=6,
    )
    assert "fit_calc" in seen
    assert "add_features" in seen
    af = [c for c in res.result["calls"] if c["tool"] == "add_features"]
    assert af and af[0]["ok"] is True

def test_wi7_c_never_rejects_missing_calc():
    ledger = ListLedger()
    cap = profile_for_letter("C")
    provider = ScriptedProvider([
        _tool_json("add_features", {
            "doc_id": "d1",
            "ops": [{"op": "hole", "params": {"diameter": 5.5, "depth": 10}}],
        }),
        _done_json(),
    ])

    def dispatch(tool, args):
        return {"ok": True}

    res = agent_turn.run_turn(
        "hole freeform", provider,
        allowed_tools=["add_features"],
        tool_dispatch=dispatch, ledger=ledger, actor="feature_modeler",
        capability=cap, phase="build", max_tool_calls=4,
    )
    assert res.result["calls"][0]["ok"] is True
    assert _dials(ledger, "math_scratchpad") == []


# ===========================================================================
# WI-8 — example retrieval
# ===========================================================================

def test_wi8_a_injects_reference_build():
    ledger = ListLedger()
    cap = profile_for_letter("A")
    text = sc.reference_build_context(
        cap, "make a flanged bushing with bolt circle",
        task_class="simple_part", phase="specify",
        ledger=ledger, actor="test",
    )
    assert sc.REFERENCE_BUILD_MARKER in text
    assert _dials(ledger, "example_retrieval")
    assert _dials(ledger, "example_retrieval")[0]["payload"]["forced"] is True


def test_wi8_c_no_reference_build_by_default():
    ledger = ListLedger()
    cap = profile_for_letter("C")
    text = sc.reference_build_context(
        cap, "make a flanged bushing with bolt circle",
        task_class="simple_part", phase="specify",
        ledger=ledger, actor="test",
    )
    assert text == ""
    assert sc.REFERENCE_BUILD_MARKER not in text
    assert _dials(ledger, "example_retrieval") == []


def test_wi8_c_agent_turn_context_clean():
    """C turn request must not contain [REFERENCE_BUILD]."""
    ledger = ListLedger()
    cap = profile_for_letter("C")
    captured = []

    class CapturingProvider:
        name = "cap"

        def complete(self, system, messages, max_tokens=8000):
            captured.append(messages)
            return _done_json()

    agent_turn.run_turn(
        "make a flanged bushing", CapturingProvider(),
        allowed_tools=["measure_part"],
        tool_dispatch=lambda t, a: {"ok": True},
        ledger=ledger, capability=cap, phase="specify",
    )
    blob = str(captured)
    assert sc.REFERENCE_BUILD_MARKER not in blob


def test_wi8_a_agent_turn_context_has_reference():
    ledger = ListLedger()
    cap = profile_for_letter("A")
    captured = []

    class CapturingProvider:
        name = "cap"

        def complete(self, system, messages, max_tokens=8000):
            captured.append(messages)
            return _done_json()

    agent_turn.run_turn(
        "make a flanged bushing", CapturingProvider(),
        allowed_tools=["measure_part"],
        tool_dispatch=lambda t, a: {"ok": True},
        ledger=ledger, capability=cap, phase="specify",
        task_class="simple_part",
    )
    blob = str(captured)
    assert sc.REFERENCE_BUILD_MARKER in blob


# ===========================================================================
# WI-10 — refine passes
# ===========================================================================

def test_wi10_refine_limit_counts_only_post_settlement_reworks():
    cap_zero = profile_for_letter("A")
    assert sc.refine_pass_limit(cap_zero) == 0
    assert sc.should_stop_refine(cap_zero, 0) is False
    assert sc.should_stop_refine(cap_zero, 1) is True

    cap_one = profile_for_letter("B")
    assert sc.refine_pass_limit(cap_one) == 1
    assert sc.should_stop_refine(cap_one, 1) is False
    assert sc.should_stop_refine(cap_one, 2) is True


def test_wi10_c_refine_untouched():
    cap = profile_for_letter("C")
    assert sc.refine_pass_limit(cap) is None
    assert sc.should_stop_refine(cap, 100) is False


# --- WI-10 wiring into the live turn loop ----------------------------------
# The dial was dead for its whole life: only the two tests above called it. Wiring it
# needed a definition of "refine pass" that CANNOT truncate a build in progress, so a
# round is only counted when geometry is re-worked AFTER the model itself declared the
# build finished (verify_battery / save_part / export_part). These pin that.

def _fence(tool, **args):
    import json as _json
    return "```json\n" + _json.dumps({"tool": tool, "args": args}) + "\n```"


def _refine_turn(letter, replies, ledger=None):
    led = ledger if ledger is not None else ListLedger()
    seen = []
    res = agent_turn.run_turn(
        "build a holster", ScriptedProvider(replies),
        allowed_tools=["new_part", "add_features", "describe_part",
                       "verify_battery", "save_part"],
        tool_dispatch=lambda t, a: seen.append(t) or {"ok": True, "doc_id": "d1"},
        ledger=led, capability=profile_for_letter(letter), phase="build",
        task_class="simple_part", max_tool_calls=24,
    )
    return res, seen, led


def test_wi10_a_multi_feature_first_build_is_never_truncated():
    """The regression this cap must never cause: A allows ONE stable pass, and a part
    that takes six features to build is still ONE pass. Interleaved describe_part is
    mid-build inspection, not a finished build, so it must not settle the geometry."""
    replies = []
    replies.append(_fence("new_part", name="holster"))
    for i in range(6):
        replies.append(_fence("add_features", doc_id="d1", ops=[{"op": "add_feature"}]))
        replies.append(_fence("describe_part", doc_id="d1"))
    replies.append('{"done":true,"handoff":{"status":"done","summary":"built"}}')
    res, seen, led = _refine_turn("A", replies)

    assert seen.count("add_features") == 6, seen
    assert res.stop_reason != agent_turn._REFINE_CAP, res.stop_reason
    assert _dials(led, "refine_passes") == []


def test_wi10_a_rework_after_a_declared_finish_trips_the_cap():
    """A (refine_passes=0): verify the build, then go back and re-cut it -> capped.

    The cap is enforced BEFORE the re-work runs. It used to fire after the
    mutator had already re-shaped the geometry and cleared the settled flag, so
    the turn ended holding an unverified, possibly unsaved revision while the
    stop detail claimed the verified build was kept — and a prior productive
    call let the harness synthesise status:done on top of that dirty state.
    """
    replies = [
        _fence("new_part", name="holster"),
        _fence("add_features", doc_id="d1", ops=[{"op": "add_feature"}]),
        _fence("verify_battery", doc_id="d1"),
        _fence("add_features", doc_id="d1", ops=[{"op": "add_feature"}]),
        _fence("add_features", doc_id="d1", ops=[{"op": "add_feature"}]),
    ]
    res, seen, led = _refine_turn("A", replies)

    assert res.stop_reason == agent_turn._REFINE_CAP, res.stop_reason
    # The re-work that tripped it was REFUSED, so only the original build ran.
    assert seen.count("add_features") == 1, seen
    # What the turn keeps is genuinely settled: the last geometry mutation is
    # followed by a settler (verify_battery), never left dirty.
    assert seen[-1] in agent_turn._GEOMETRY_SETTLERS, seen
    dial = _dials(led, "refine_passes")
    assert len(dial) == 1, dial
    assert dial[0]["payload"]["reason"] == "refine_cap"
    assert dial[0]["payload"]["limit"] == 0
    assert res.success is True
    assert res.result["handoff"]["status"] == "done"
    assert res.result["handoff"]["outputs"]["refinement_capped"] is True


def test_wi10_b_allows_one_rework_before_capping():
    """B (refine_passes=1): one re-do after a declared finish is the budget."""
    replies = [
        _fence("new_part", name="holster"),
        _fence("add_features", doc_id="d1", ops=[{"op": "add_feature"}]),
        _fence("save_part", doc_id="d1"),
        _fence("add_features", doc_id="d1", ops=[{"op": "add_feature"}]),
        _fence("save_part", doc_id="d1"),
        _fence("add_features", doc_id="d1", ops=[{"op": "add_feature"}]),
        _fence("add_features", doc_id="d1", ops=[{"op": "add_feature"}]),
    ]
    res, seen, led = _refine_turn("B", replies)

    assert res.stop_reason == agent_turn._REFINE_CAP, res.stop_reason
    # One re-work is the budget: build, settle, re-work, settle. The SECOND
    # re-work is refused before it can run.
    assert seen.count("add_features") == 2, seen
    assert seen[-1] in agent_turn._GEOMETRY_SETTLERS, seen
    assert _dials(led, "refine_passes")[0]["payload"]["limit"] == 1
    assert res.success is True
    assert res.result["handoff"]["status"] == "done"


def test_wi10_c_rework_is_never_capped():
    """C / frontier keeps its existing budget-only bounds — the dial must be inert."""
    replies = [_fence("new_part", name="holster")]
    for _ in range(5):
        replies.append(_fence("add_features", doc_id="d1", ops=[{"op": "add_feature"}]))
        replies.append(_fence("verify_battery", doc_id="d1"))
    replies.append('{"done":true,"handoff":{"status":"done","summary":"built"}}')
    res, seen, led = _refine_turn("C", replies)

    assert res.stop_reason != agent_turn._REFINE_CAP, res.stop_reason
    assert seen.count("add_features") == 5, seen
    assert _dials(led, "refine_passes") == []


def test_wi10_refine_cap_has_its_own_error_detail():
    """Reusing max_iters would blame a tool-call budget that never ran out."""
    detail = agent_turn.error_detail_for_stop(
        agent_turn._REFINE_CAP, tool_calls_made=4, budget=24).lower()
    assert "refinement limit" in detail
    # It must say the re-work did NOT run — the old wording ("the verified
    # build is kept") was a lie while the cap fired post-mutation.
    assert "not run" in detail
    assert "retained" in detail
    assert "tool call" not in detail, "a refine cap is not a tool-call budget"
    assert "24" not in detail, "the tool-call budget was never reached"


# ===========================================================================
# C-freedom snapshot (WI-18)
# ===========================================================================

def test_c_freedom_zero_forced_scaffold_events():
    """Mock C turn: zero capability_dial forced events and zero auto_verify_inject."""
    ledger = ListLedger()
    cap = profile_for_letter("C")
    provider = ScriptedProvider([
        _tool_json("add_features", {
            "doc_id": "d1",
            "ops": [{"op": "hole", "params": {"diameter": 5.5, "depth": 12}}],
        }),
        _tool_json("add_features", {
            "doc_id": "d1",
            "ops": [{"op": "add_feature",
                     "feature": {"type": "fillet", "params": {"radius": 1}}}],
        }),
        _done_json(),
    ])

    def dispatch(tool, args):
        return {"ok": True}

    agent_turn.run_turn(
        "freeform part with hole and fillet", provider,
        allowed_tools=["add_features", "describe_part", "fit_calc"],
        tool_dispatch=dispatch, ledger=ledger, actor="feature_modeler",
        capability=cap, phase="build", max_tool_calls=8,
        task_class="simple_part",
    )
    forced = sc.forced_scaffold_events(ledger)
    assert forced == [], f"C must have zero forced scaffold events, got {forced}"
    assert _kinds(ledger, "auto_verify_inject") == []
    assert _forced(ledger) == []


# ===========================================================================
# Helpers / unit
# ===========================================================================

def test_stage_appropriate_verify_tool():
    assert sc.stage_appropriate_verify_tool(
        "add_features", {"ops": [{"op": "sketch_circle"}]}) == "check_sketch"
    assert sc.stage_appropriate_verify_tool(
        "add_features", {"ops": [{"op": "extrude"}]}) == "describe_part"
    assert sc.stage_appropriate_verify_tool("asm_mate", {}) == "describe_assembly"


def test_is_dimensioned_feature_call():
    assert sc.is_dimensioned_feature_call(
        "add_features", {"ops": [{"op": "hole", "params": {"diameter": 5}}]})
    assert sc.is_dimensioned_feature_call("hole", {"diameter": 5})
    assert not sc.is_dimensioned_feature_call("describe_part", {})
    assert not sc.is_dimensioned_feature_call(
        "add_features", {"ops": [{"op": "sketch_line"}]})


def test_phase_cap_values():
    a = profile_for_letter("A")
    b = profile_for_letter("B")
    c = profile_for_letter("C")
    assert sc.phase_cap(a, "build") == 20
    assert sc.phase_cap(b, "build") == 40
    assert sc.phase_cap(c, "build") is None
    assert sc.phase_cap(a, "intake") == 2


def test_ops_need_aggressive_checkpoint():
    assert sc.ops_need_aggressive_checkpoint(
        "add_features",
        {"ops": [{"feature": {"type": "fillet"}}]})
    assert sc.ops_need_aggressive_checkpoint(
        "add_features",
        {"ops": [{"op": "hole"}]})
    assert not sc.ops_need_aggressive_checkpoint(
        "add_features",
        {"ops": [{"op": "extrude"}]})


