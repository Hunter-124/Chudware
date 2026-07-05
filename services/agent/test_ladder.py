"""Tests for the design cognition ladder runtime (T-W25).

Contract: contracts/advisor.md §22 (aicad.ladder/1 — FROZEN), §19 (councils),
§21 (linked dimensions). Playbooks: services/agent/playbooks/ladder_*.yaml
(AUTHORED, loaded verbatim). Role: services/agent/roles/design_thinker.yaml.

The ONE non-negotiable is the FRESH-CONTEXT rule: the rung executor's context
contains ONLY {previous rung artifact(s) per the §22 scoping table, a scoped
ledger slice, the rung template}. No prior-rung chat, no full conversation, and
— critically — no artifact from a rung the §22 table does not name for THIS rung
(rung C sees geometry_plan ONLY, not the function_map above it).

``test_fresh_context_no_leak`` and ``test_fresh_context_scoping`` are written
FIRST and prove that rule two ways: (1) a planted sentinel from an upstream rung
leaks into NO downstream rung's assembled context; (2) each rung's context
contains exactly the artifacts §22 names for it and excludes the others.

Runs headless: no FastMCP, no libaicad, no network, no live LLM (a scripted mock
provider supplies rung artifacts). The fixture spec is a "bearing mount bracket".
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from services.agent.ledger import Ledger  # noqa: E402
from services.agent import ladder  # noqa: E402
from services.agent import ladder_validate as lv  # noqa: E402
from services.agent import ladder_compile as lc  # noqa: E402
from services.agent.roles import load_role, load_playbook  # noqa: E402


# --------------------------------------------------------------------------- fixtures

# A unique sentinel string that a real conversation might carry between turns.
# It is deliberately planted into rung A's reasoning/prior "chat" so a leak is
# detectable: if any downstream rung's assembled context contains it, the
# fresh-context rule has been violated.
SENTINEL = "SENTINEL-DO-NOT-LEAK-a1b2c3d4e5"


def _bearing_bracket_ledger(path: Path) -> Ledger:
    """A 'bearing mount bracket' spec captured in the ledger (brief + a couple ACs
    + one spec dimension). This is the ONLY spec the ladder ever sees for rung A —
    rung A reads it via its scoped ledger slice, never a chat transcript."""
    lg = Ledger("bracket_run", path=path)
    lg.set_brief(
        "planner",
        title="Bearing mount bracket",
        summary="Bracket that locates a 6202 bearing and bolts to a base plate.",
        success_criteria=["locate 6202 bearing", "bolt to base plate", "carry radial load"],
    )
    lg.add_requirement("planner", "AC1", "Locate a 6202 bearing (bore 15, OD 35).")
    lg.add_requirement("planner", "AC2", "Mount to base plate via 4x M6.")
    lg.upsert_dimension(
        "planner", "DIM7", "bearing_bore", 15.0, "mm",
        driver="spec", provenance="spec: 6202 bearing bore", status="confirmed",
    )
    return lg


# The four rung artifacts a scripted design_thinker would emit for the bracket.
# Kept minimal but VALID against every §22 validator so the happy-path ladder run
# reaches rung D and compiles. Each is the ``artifact`` payload of a handoff.

FUNCTION_MAP = {
    "functions": [
        {"id": "FN1", "statement": "locate the 6202 bearing", "driven_by": ["AC1"]},
        {"id": "FN2", "statement": "fasten the bracket to the base plate", "driven_by": ["AC2"]},
    ],
    "features": [
        {"id": "FT1", "name": "bearing seat FOR 6202", "purpose": "locate bearing OR",
         "serves": ["FN1"], "interfaces_with": ["6202"], "criticality": "critical"},
        {"id": "FT2", "name": "mounting holes", "purpose": "bolt to base plate",
         "serves": ["FN2"], "interfaces_with": ["base plate"], "criticality": "important"},
    ],
    "omissions_check": [
        {"class": "mounting", "considered": "yes", "why": "mounting holes", "ft_refs": ["FT2"]},
        {"class": "location/alignment", "considered": "yes", "why": "bearing seat locates", "ft_refs": ["FT1"]},
        {"class": "retention", "considered": "yes", "why": "press fit retains bearing", "ft_refs": ["FT1"]},
        {"class": "sealing", "considered": "no", "why": "dry indoor application", "ft_refs": []},
        {"class": "venting", "considered": "no", "why": "no enclosed cavity", "ft_refs": []},
        {"class": "clearance/service-access", "considered": "yes", "why": "bolt access from front", "ft_refs": ["FT2"]},
        {"class": "strength/stiffening", "considered": "yes", "why": "gusseted web", "ft_refs": ["FT1"]},
        {"class": "manufacturability aids", "considered": "yes", "why": "draft + fillets on seat", "ft_refs": ["FT1"]},
    ],
    "defects": [],
}

GEOMETRY_PLAN = {
    "elements": [
        {"id": "G1", "kind": "profile_op", "op": "revolve", "description": "bearing seat bore",
         "params": [{"name": "bore_d", "basis": "spec: DIM7"}, {"name": "seat_d", "basis": "derived: bore_d + 2*wall"}],
         "realizes": ["FT1"], "orientation_hint": "axis horizontal, bore facing +X"},
        {"id": "G2", "kind": "profile_op", "op": "hole", "description": "4x M6 mounting holes",
         "params": [{"name": "hole_d", "basis": "spec: M6 clearance"}],
         "realizes": ["FT2"], "orientation_hint": "through the base flange, +Z"},
    ],
    "shared": [],
    "conflicts": [],
    "defects": [],
}

LINKAGE_MAP = {
    "datums": [
        {"id": "D1", "kind": "plane", "defined_by": "base flange underside", "why": "mates to base plate; inspection datum A"},
        {"id": "D2", "kind": "axis", "defined_by": "bearing bore axis", "why": "locates seat + mounting pattern"},
    ],
    "links": [
        {"from": "G1.seat_d", "to": "G1.bore_d", "relation": "= bore_d + 2*wall", "why": "min wall around the seat"},
    ],
    "driving_dims": [
        {"name": "seat_d", "owner": "G1", "derived_from": ["DIM7"]},
    ],
    "constraint_plans": ["G1: geometric (concentric to D2) then driving (bore_d)"],
    "reference_tree": "D1 -> base flange -> G2; D2 -> G1",
    "defects": [],
}

BUILD_PATHWAY = {
    "steps": [
        {"n": 1, "op": "datum", "elements": [], "sketch_on": "world", "orientation": "as-modeled",
         "why": "establish D1/D2 first", "risks": []},
        {"n": 2, "op": "revolve", "elements": ["G1"], "sketch_on": "D2", "orientation": "axis +X",
         "why": "bearing seat is the base feature the datum scheme lives on", "risks": []},
        {"n": 3, "op": "hole", "elements": ["G2"], "sketch_on": "D1", "orientation": "+Z",
         "why": "holes pierce the flange which exists after the seat body", "risks": ["edge distance"]},
    ],
    "orientation_rationale": "bore horizontal so D2 is a sketch axis and the flange lies on D1",
    "alternates_considered": [
        {"summary": "bore vertical", "rejected_because": "flange sketch plane would need a datum offset"},
    ],
    "defects": [],
}

RUNG_ARTIFACTS = {
    "function_map": FUNCTION_MAP,
    "geometry_plan": GEOMETRY_PLAN,
    "linkage_map": LINKAGE_MAP,
    "build_pathway": BUILD_PATHWAY,
}


class ScriptedLadderProvider:
    """A mock provider that returns the correct rung artifact based on which rung the
    fresh prompt asks for. It records every ``system`` + ``messages`` it is handed so
    the fresh-context tests can inspect exactly what each rung's context contained.

    It also plants ``SENTINEL`` into the rung-A reply's reasoning so a leak downstream
    is detectable.
    """

    supports_vision = False

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.replies: list[dict] = []  # what we returned, per rung

    def complete(self, system: str, messages: list[dict], max_tokens: int = 8000) -> str:
        # Flatten the assembled prompt (system + all message text) — this IS the rung's
        # entire context window. Recording it is how the tests prove fresh-context.
        flat = system + "\n" + json.dumps(messages)
        self.calls.append({"system": system, "messages": messages, "flat": flat})
        rung = ladder.rung_of_prompt(system, messages)
        artifact = RUNG_ARTIFACTS[rung]
        reasoning = f"reasoning for {rung}"
        if rung == "function_map":
            # Plant the sentinel in rung A's private reasoning — it must never reach B/C/D.
            reasoning = f"{reasoning} {SENTINEL}"
        reply = json.dumps({
            "schema": "aicad.handoff/1",
            "role": "design_thinker",
            "status": "done",
            "summary": f"emitted {rung}",
            "outputs": {"rung": rung, "artifact": artifact, "reasoning_summary": reasoning},
        })
        self.replies.append({"rung": rung, "reply": reply})
        return reply


# --------------------------------------------------------------------------- base

class _LadderTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ladder_test_"))
        self.ledger = _bearing_bracket_ledger(self.tmp / "ledger.json")


# --------------------------------------------------------------------------- FRESH CONTEXT (written first)

class TestFreshContext(_LadderTestCase):
    """The one non-negotiable. Proven two ways."""

    def _run_and_capture(self):
        prov = ScriptedLadderProvider()
        result = ladder.run_ladder(self.ledger, prov, deliberation="full")
        # index the recorded calls by rung
        by_rung = {}
        for call in prov.calls:
            r = ladder.rung_of_prompt(call["system"], call["messages"])
            by_rung.setdefault(r, []).append(call)
        return result, prov, by_rung

    def test_fresh_context_no_leak(self):
        """A sentinel planted in rung A's REPLY appears in NO downstream rung's
        assembled INPUT context. Proven positively: the sentinel really did enter the
        system (it is in rung A's reply), and then it is absent from every rung's input
        — so the assertion cannot pass vacuously if the planting silently broke."""
        _result, prov, by_rung = self._run_and_capture()
        # Positive control: the sentinel WAS emitted in rung A's reply.
        a_replies = [r["reply"] for r in prov.replies if r["rung"] == "function_map"]
        self.assertTrue(a_replies, "rung A never produced a reply")
        self.assertTrue(any(SENTINEL in r for r in a_replies),
                        "positive control failed: sentinel not in rung A reply — the "
                        "leak test would be vacuous; fix the fixture before trusting it")
        # It must appear in NO rung's INPUT context (system + messages).
        for rung, calls in by_rung.items():
            for call in calls:
                self.assertNotIn(
                    SENTINEL, call["flat"],
                    f"rung {rung!r} context leaked the rung-A sentinel — fresh-context violated",
                )

    def test_ledger_slice_excludes_prior_artifacts_at_any_budget(self):
        """The fresh-context rule must NOT rest on token-budget truncation. After a full
        run, the downstream rungs' ledger slices exclude the Artifacts section, so a
        prior rung's artifact (the function_map text) cannot re-enter via the ledger at
        ANY budget."""
        from services.agent.ledger import ledger_render
        prov = ScriptedLadderProvider()
        ladder.run_ladder(self.ledger, prov, deliberation="full")
        # High-budget raw render (worst case for leakage).
        big = ledger_render(self.ledger, 100000)
        stripped = ladder._strip_artifacts_section(big)
        self.assertNotIn("locate the 6202 bearing", stripped,
                         "function_map text must not survive in a downstream ledger slice")
        # And the executor's actual downstream slice is clean too.
        c_slice = ladder._scoped_ledger_slice(self.ledger, "linkage_map")
        self.assertNotIn("locate the 6202 bearing", c_slice)

    def test_fresh_context_scoping(self):
        """Each rung's context contains EXACTLY the artifacts §22 names for it, and
        EXCLUDES the ones it does not. The critical cases (advisor-flagged):
          - rung C (linkage) sees geometry_plan ONLY, NOT function_map;
          - rung D (pathway) sees geometry_plan AND linkage_map, NOT function_map.
        """
        _result, _prov, by_rung = self._run_and_capture()

        # Unique probe strings present in each artifact so we can detect their presence.
        probe = {
            "function_map": "locate the 6202 bearing",          # FN1 statement
            "geometry_plan": "bearing seat bore",               # G1 description
            "linkage_map": "min wall around the seat",          # link why
        }

        def ctx(rung: str) -> str:
            calls = by_rung.get(rung, [])
            self.assertTrue(calls, f"rung {rung!r} never ran")
            return "\n".join(c["flat"] for c in calls)

        # Rung A (function_map): sees the spec (ledger), no prior artifact.
        a = ctx("function_map")
        self.assertNotIn(probe["function_map"], a)   # it EMITS the map; doesn't receive it
        self.assertIn("bearing mount bracket", a.lower())  # the spec (brief) is present

        # Rung B (geometry_plan): sees function_map (+ envelope).
        b = ctx("geometry_plan")
        self.assertIn(probe["function_map"], b, "rung B must receive the function_map")

        # Rung C (linkage_map): geometry_plan ONLY — NOT the function_map.
        c = ctx("linkage_map")
        self.assertIn(probe["geometry_plan"], c, "rung C must receive the geometry_plan")
        self.assertNotIn(probe["function_map"], c,
                         "rung C must NOT receive the function_map (§22: geometry_plan only)")

        # Rung D (build_pathway): geometry_plan AND linkage_map — NOT the function_map.
        d = ctx("build_pathway")
        self.assertIn(probe["geometry_plan"], d, "rung D must receive the geometry_plan")
        self.assertIn(probe["linkage_map"], d, "rung D must receive the linkage_map")
        self.assertNotIn(probe["function_map"], d,
                         "rung D must NOT receive the function_map (§22: geometry_plan + linkage_map)")

    def test_context_struct_cannot_hold_chat(self):
        """Fresh-context by construction: the RungInput carries only typed artifacts +
        a ledger slice + the rung template. It has no field for a running conversation."""
        ri = ladder.RungInput(
            rung="geometry_plan",
            prior_artifacts={"function_map": FUNCTION_MAP},
            ledger_slice="[LEDGER ...]",
            playbook=load_playbook("ladder_geometry"),
        )
        # The struct's fields are exactly these four — no 'history'/'messages'/'conversation'.
        fields = set(vars(ri).keys())
        self.assertEqual(fields, {"rung", "prior_artifacts", "ledger_slice", "playbook"})


# --------------------------------------------------------------------------- HAPPY PATH

class TestHappyPath(_LadderTestCase):
    def test_full_ladder_runs_all_four_rungs(self):
        prov = ScriptedLadderProvider()
        result = ladder.run_ladder(self.ledger, prov, deliberation="full")
        self.assertEqual(result.status, "done", f"ladder did not complete: {result}")
        self.assertIn("function_map", result.artifacts)
        self.assertIn("geometry_plan", result.artifacts)
        self.assertIn("linkage_map", result.artifacts)
        self.assertIn("build_pathway", result.artifacts)
        # Every rung artifact was written to the ledger as an artifact/ladder event.
        kinds = [e["kind"] for e in self.ledger.events]
        self.assertIn("artifact", kinds)

    def test_ladder_artifacts_are_ledger_events(self):
        prov = ScriptedLadderProvider()
        ladder.run_ladder(self.ledger, prov, deliberation="full")
        arts = [e for e in self.ledger.events if e["kind"] == "artifact"]
        rungs = {e["payload"].get("id") or e["payload"].get("rung") for e in arts}
        # each of the four rung artifacts recorded
        self.assertGreaterEqual(len(arts), 4)


# --------------------------------------------------------------------------- VALIDATORS

class TestValidators(unittest.TestCase):
    # ---- rung A: omissions_check completeness -------------------------------
    def test_A_omissions_all_eight_classes(self):
        findings = lv.validate_function_map(FUNCTION_MAP)
        self.assertEqual(lv.blockers(findings), [], f"clean function_map bounced: {findings}")

    def test_A_missing_omission_class_bounces(self):
        bad = json.loads(json.dumps(FUNCTION_MAP))
        bad["omissions_check"] = bad["omissions_check"][:5]  # drop 3 classes
        findings = lv.validate_function_map(bad)
        self.assertTrue(lv.blockers(findings), "incomplete omissions_check must bounce")

    def test_A_coverage_every_ac_served(self):
        bad = json.loads(json.dumps(FUNCTION_MAP))
        # remove FN2 so AC2 is unserved
        bad["functions"] = [f for f in bad["functions"] if f["id"] != "FN2"]
        findings = lv.validate_function_map(bad, acceptance_criteria=["AC1", "AC2"])
        self.assertTrue(lv.blockers(findings), "an unserved AC must bounce")

    # ---- rung B: coverage, orphans, conflicts -------------------------------
    def test_B_clean_passes(self):
        findings = lv.validate_geometry_plan(GEOMETRY_PLAN, FUNCTION_MAP)
        self.assertEqual(lv.blockers(findings), [], f"clean geometry_plan bounced: {findings}")

    def test_B_uncovered_feature_bounces(self):
        bad = json.loads(json.dumps(GEOMETRY_PLAN))
        bad["elements"] = [g for g in bad["elements"] if g["id"] != "G2"]  # FT2 now uncovered
        findings = lv.validate_geometry_plan(bad, FUNCTION_MAP)
        self.assertTrue(lv.blockers(findings), "an uncovered feature must bounce")

    def test_B_orphan_geometry_bounces(self):
        bad = json.loads(json.dumps(GEOMETRY_PLAN))
        bad["elements"].append({"id": "G9", "kind": "primitive", "op": "extrude",
                                "description": "orphan", "params": [], "realizes": [],
                                "orientation_hint": ""})
        findings = lv.validate_geometry_plan(bad, FUNCTION_MAP)
        self.assertTrue(lv.blockers(findings), "orphan geometry (realizes nothing) must bounce")

    def test_B_unresolved_conflict_bounces(self):
        bad = json.loads(json.dumps(GEOMETRY_PLAN))
        bad["conflicts"] = [{"pair": ["G1", "G2"], "overlap_concern": "share space",
                             "resolution_needed": ""}]
        findings = lv.validate_geometry_plan(bad, FUNCTION_MAP)
        self.assertTrue(lv.blockers(findings), "a conflict with no resolution must bounce before D")

    # ---- rung C: circular links, parseable relations, owner+basis, datum use -
    def test_C_clean_passes(self):
        findings = lv.validate_linkage_map(LINKAGE_MAP, GEOMETRY_PLAN)
        self.assertEqual(lv.blockers(findings), [], f"clean linkage_map bounced: {findings}")

    def test_C_circular_link_bounces(self):
        bad = json.loads(json.dumps(LINKAGE_MAP))
        bad["links"] = [
            {"from": "G1.a", "to": "G1.b", "relation": "= b + 1", "why": "x"},
            {"from": "G1.b", "to": "G1.a", "relation": "= a + 1", "why": "y"},
        ]
        findings = lv.validate_linkage_map(bad, GEOMETRY_PLAN)
        self.assertTrue(lv.blockers(findings), "a circular link must bounce")

    def test_C_unparseable_relation_bounces(self):
        bad = json.loads(json.dumps(LINKAGE_MAP))
        bad["links"] = [{"from": "G1.seat_d", "to": "G1.bore_d",
                         "relation": "make it a bit bigger", "why": "z"}]
        findings = lv.validate_linkage_map(bad, GEOMETRY_PLAN)
        self.assertTrue(lv.blockers(findings), "a non-arithmetic relation must bounce")

    def test_C_driving_dim_missing_owner_bounces(self):
        bad = json.loads(json.dumps(LINKAGE_MAP))
        bad["driving_dims"] = [{"name": "seat_d", "owner": "", "derived_from": ["DIM7"]}]
        findings = lv.validate_linkage_map(bad, GEOMETRY_PLAN)
        self.assertTrue(lv.blockers(findings), "a driving dim with no owner must bounce")

    def test_C_unused_datum_bounces(self):
        bad = json.loads(json.dumps(LINKAGE_MAP))
        bad["datums"].append({"id": "D9", "kind": "point", "defined_by": "nowhere", "why": "unused"})
        findings = lv.validate_linkage_map(bad, GEOMETRY_PLAN)
        self.assertTrue(lv.blockers(findings), "an unused datum must bounce")

    # ---- rung D: build-once, exists-before-use ------------------------------
    def test_D_clean_passes(self):
        findings = lv.validate_build_pathway(BUILD_PATHWAY, GEOMETRY_PLAN, LINKAGE_MAP)
        self.assertEqual(lv.blockers(findings), [], f"clean build_pathway bounced: {findings}")

    def test_D_element_built_twice_bounces(self):
        bad = json.loads(json.dumps(BUILD_PATHWAY))
        bad["steps"].append({"n": 4, "op": "revolve", "elements": ["G1"], "sketch_on": "D2",
                             "orientation": "axis +X", "why": "again", "risks": []})
        findings = lv.validate_build_pathway(bad, GEOMETRY_PLAN, LINKAGE_MAP)
        self.assertTrue(lv.blockers(findings), "an element built twice must bounce")

    def test_D_element_never_built_bounces(self):
        bad = json.loads(json.dumps(BUILD_PATHWAY))
        bad["steps"] = [s for s in bad["steps"] if "G2" not in s["elements"]]
        findings = lv.validate_build_pathway(bad, GEOMETRY_PLAN, LINKAGE_MAP)
        self.assertTrue(lv.blockers(findings), "an element never built must bounce")

    def test_D_sketch_on_missing_datum_bounces(self):
        bad = json.loads(json.dumps(BUILD_PATHWAY))
        bad["steps"][1]["sketch_on"] = "D_NONEXISTENT"
        findings = lv.validate_build_pathway(bad, GEOMETRY_PLAN, LINKAGE_MAP)
        self.assertTrue(lv.blockers(findings), "a sketch_on referencing an undefined datum must bounce")


# --------------------------------------------------------------------------- BOUNCE LOOP

class TestBounceLoop(_LadderTestCase):
    def test_two_bounces_then_escalate(self):
        """A rung that keeps emitting an invalid artifact bounces at most twice, then
        the ladder escalates (council if available, else escalate) — never a third
        identical try."""
        calls = {"n": 0}

        class AlwaysBadB:
            supports_vision = False

            def complete(self, system, messages, max_tokens=8000):
                rung = ladder.rung_of_prompt(system, messages)
                if rung == "geometry_plan":
                    calls["n"] += 1
                    bad = json.loads(json.dumps(GEOMETRY_PLAN))
                    bad["elements"] = []  # every FT uncovered → always bounces
                    art = bad
                else:
                    art = RUNG_ARTIFACTS[rung]
                return json.dumps({
                    "schema": "aicad.handoff/1", "role": "design_thinker",
                    "status": "done", "summary": "x",
                    "outputs": {"rung": rung, "artifact": art, "reasoning_summary": "r"},
                })

        result = ladder.run_ladder(self.ledger, AlwaysBadB(), deliberation="full")
        self.assertEqual(result.status, "escalated")
        self.assertEqual(result.escalated_rung, "geometry_plan")
        # 1 initial attempt + 2 bounces = 3 calls to rung B, then stop.
        self.assertEqual(calls["n"], 3, "rung must run exactly 3 times (attempt + 2 bounces)")


# --------------------------------------------------------------------------- COMPILE

class TestCompile(unittest.TestCase):
    def test_compiles_to_buildgraph(self):
        graph = lc.compile_pathway(BUILD_PATHWAY)
        self.assertEqual(graph["schema"], "aicad.buildgraph/1")
        self.assertEqual(len(graph["nodes"]), len(BUILD_PATHWAY["steps"]))
        # graph validates cleanly through the T-W15 validator (no cycles/dangling)
        from services.agent.buildgraph.schema import validate_graph
        findings = validate_graph(graph)
        blockers = [i for adv in findings for i in adv.get("items", [])
                    if i.get("severity") == "blocker"]
        self.assertEqual(blockers, [], f"compiled graph has blockers: {blockers}")

    def test_why_becomes_edge_why(self):
        graph = lc.compile_pathway(BUILD_PATHWAY)
        # the 'why' of step 3 ("holes pierce the flange...") must survive onto its node.
        node3 = [n for n in graph["nodes"] if "G2" in n.get("refs", {}).get("features", [])][0]
        self.assertIn("pierce", node3["why"])

    def test_lossless_round_trip(self):
        """LOSSLESS: every field of every step is reconstructable from the graph.
        op, elements, sketch_on, orientation, why, risks, and n all survive."""
        graph = lc.compile_pathway(BUILD_PATHWAY)
        restored = lc.decompile_graph(graph)
        self.assertEqual(restored["steps"], BUILD_PATHWAY["steps"],
                         "round-trip must reconstruct the FULL step list")
        self.assertEqual(restored["orientation_rationale"], BUILD_PATHWAY["orientation_rationale"])
        self.assertEqual(restored["alternates_considered"], BUILD_PATHWAY["alternates_considered"])

    def test_requires_edges_follow_sequence(self):
        graph = lc.compile_pathway(BUILD_PATHWAY)
        nodes = {n["id"]: n for n in graph["nodes"]}
        order = [n["id"] for n in graph["nodes"]]
        # step 2 requires step 1, step 3 requires step 2 (linear default from sequence)
        self.assertIn(order[0], nodes[order[1]]["requires"])
        self.assertIn(order[1], nodes[order[2]]["requires"])


# --------------------------------------------------------------------------- COUNCIL HOOK

class TestCouncilHook(_LadderTestCase):
    def test_council_hook_present_and_flagged_off(self):
        """The build-strategy council (§19, T-W23) critiques rung D before compile.
        T-W23 is unmerged, so the hook must be PRESENT but feature-flagged: with the
        flag off (default) the ladder runs to completion without a live council."""
        prov = ScriptedLadderProvider()
        result = ladder.run_ladder(self.ledger, prov, deliberation="full")
        self.assertEqual(result.status, "done")
        # the seam exists and reports it was skipped (not wired)
        self.assertIn(result.council, ("skipped", "flagged_off", None))

    def test_council_invoked_when_flag_on(self):
        """When a council callback is provided, rung D output is handed to it before
        compile (seats receive geometry_plan + build_pathway, per §19 — not chat)."""
        seen = {}

        def council_cb(*, geometry_plan, build_pathway):
            seen["gp"] = geometry_plan
            seen["bp"] = build_pathway
            return {"schema": "aicad.council/1", "kind": "build_strategy",
                    "synthesis": {"decision": "proceed"}, "must_haves": []}

        prov = ScriptedLadderProvider()
        result = ladder.run_ladder(self.ledger, prov, deliberation="full",
                                   council_cb=council_cb)
        self.assertEqual(result.status, "done")
        self.assertIn("gp", seen, "council must receive the geometry_plan")
        self.assertIn("bp", seen, "council must receive the build_pathway")
        # §19: seats do NOT receive the function_map / chat.
        self.assertNotIn("locate the 6202 bearing", json.dumps(seen))


# --------------------------------------------------------------------------- MINIMAL MODE

class TestMinimalMode(_LadderTestCase):
    def test_minimal_validators_still_run(self):
        """§22: mandatory at full, optional at minimal — but validators ALWAYS run.
        A minimal-mode run that emits a bad rung D still gets validated."""
        class DirectD:
            supports_vision = False

            def complete(self, system, messages, max_tokens=8000):
                # frontier-style: emit rung D artifact directly
                bad = json.loads(json.dumps(BUILD_PATHWAY))
                bad["steps"].append({"n": 4, "op": "revolve", "elements": ["G1"],
                                     "sketch_on": "D2", "orientation": "x", "why": "dup", "risks": []})
                return json.dumps({
                    "schema": "aicad.handoff/1", "role": "design_thinker",
                    "status": "done", "summary": "x",
                    "outputs": {"rung": "build_pathway", "artifact": bad, "reasoning_summary": "r"},
                })

        # In minimal mode with a pre-supplied geometry_plan + linkage_map, only rung D runs,
        # and its output is still validated (build-once catches the duplicate).
        result = ladder.run_ladder(
            self.ledger, DirectD(), deliberation="minimal",
            seed_artifacts={"geometry_plan": GEOMETRY_PLAN, "linkage_map": LINKAGE_MAP},
        )
        # validation caught the duplicate → bounced → escalated (mock never fixes it)
        self.assertEqual(result.status, "escalated")


# --------------------------------------------------------------------------- VERBATIM LOAD (DoD-1)

class TestVerbatimLoad(unittest.TestCase):
    def test_role_and_playbooks_load(self):
        card = load_role("design_thinker")
        self.assertEqual(card.id, "design_thinker")
        for pb_id in ("ladder_functions", "ladder_geometry", "ladder_linkage", "ladder_pathway"):
            pb = load_playbook(pb_id)
            self.assertTrue(pb.steps, f"{pb_id} has no steps")


if __name__ == "__main__":
    unittest.main()
