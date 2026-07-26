"""test_dag_tool_dialect.py — the argument dialect the DAG MCP tools accept.

Companion to test_mcp_tools.py, which covers the happy path with mocked executors.
This file covers the *input shapes* recorded agent runs actually produced, and the
teaching errors returned when the input is genuinely unusable.

The dispatch path under test is the BUILT-IN agent one:

    from services.agent.agent_turn import _bind_tool
    fn = _bind_tool("dag_plan"); fn(deliverable=..., components=...)

``_reconcile_kwargs`` rejects unknown kwargs before the call unless the target
declares a var-keyword parameter, so every widening lives in the tool body and is
only reachable once ``_signature_spec`` reports ``var_kw``.  There is also one
assertion for the FastMCP path, because a *bare* ``**kwargs`` registers fine and
then breaks every FastMCP call with "kwargs Field required".

Hermetic: XDG_DATA_HOME is redirected per test, so real checkpoint DBs land in
tmp_path rather than the user's data directory.
"""
from __future__ import annotations

import json

import pytest

from services.agent import agent_turn as at
from services.dag import mcp_tools as MT


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    """Runs land in tmp_path, and the session's last-plan memory starts empty."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    saved = dict(MT._LAST)
    MT._LAST.update({"graph_json": None, "graph_from": "", "run_id": ""})
    yield
    MT._LAST.update(saved)


@pytest.fixture()
def plan():
    return at._bind_tool("dag_plan")


@pytest.fixture()
def run():
    return at._bind_tool("dag_run")


def _call(fn, **kwargs) -> dict:
    """Call through the reconciler exactly as the built-in agent loop does."""
    bound, _notes, arg_error = at._reconcile_kwargs(fn.__name__, fn, dict(kwargs))
    assert arg_error is None, f"reconciler rejected the call outright: {arg_error}"
    return json.loads(fn(**bound))


GRAPH = {
    "schema": "aicad.dag/1",
    "id": "m1",
    "name": "Mission",
    "nodes": [{
        "id": "n1", "type": "plan", "name": "Step",
        "inputs": {"request": "x"}, "tools": None,
        "end_condition": "done", "verify": None,
        "on_fail": "pause", "autonomy": "auto",
    }],
    "edges": [],
}


# ---------------------------------------------------------------------------
# The mechanic every widening depends on
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool", ["dag_plan", "dag_run", "dag_status",
                                  "dag_approve", "dag_abort", "dag_replan"])
def test_tool_owns_its_own_argument_dialect(tool):
    """var_kw must be True or the reconciler drops aliases before the call."""
    assert at._signature_spec(at._bind_tool(tool))["var_kw"] is True


# ---------------------------------------------------------------------------
# dag_plan — semantic fields instead of one brief string
# ---------------------------------------------------------------------------

def test_semantic_fields_are_synthesized_into_a_brief(plan):
    """Recorded call: dag_plan(deliverable=, components=, constraints=)."""
    out = _call(
        plan,
        deliverable="flanged_bushing_design",
        components=["sliding_fit_bore", "4hole_M4_bolt_circle", "flange_interface"],
        constraints=["20mm_shaft_fit", "M4_bolt_circle_positioning"],
    )
    assert out["ok"] is True
    brief = out["brief"]
    # Stable order: deliverable, then components, then constraints.
    assert brief == (
        "Deliverable: flanged_bushing_design. "
        "Components: sliding_fit_bore, 4hole_M4_bolt_circle, flange_interface. "
        "Constraints: 20mm_shaft_fit, M4_bolt_circle_positioning."
    )
    # ...and the synthesis is echoed, never silent.
    assert any("synthesized" in note and brief in note for note in out["interpreted"])


def test_sequence_is_appended_after_constraints(plan):
    """Recorded call: the same plus sequence= — order stays deliverable→sequence."""
    out = _call(
        plan,
        deliverable="flanged_bushing_design",
        components=["sliding_fit_bore", "4hole_M4_bolt_circle", "flange"],
        constraints=["20mm_shaft", "M4_bolt_circle"],
        sequence=["flange_design", "bore_dimensions", "assembly_validation"],
    )
    assert out["ok"] is True
    assert out["brief"] == (
        "Deliverable: flanged_bushing_design. "
        "Components: sliding_fit_bore, 4hole_M4_bolt_circle, flange. "
        "Constraints: 20mm_shaft, M4_bolt_circle. "
        "Sequence: flange_design, bore_dimensions, assembly_validation."
    )


def test_constraints_may_be_a_dict(plan):
    """Recorded call: constraints={"shaft_diameter": "20mm", ...}."""
    out = _call(
        plan,
        deliverable="flanged_bushing_design",
        components=["bushing_body", "flange_with_bolt_circle"],
        constraints={"shaft_diameter": "20mm", "bolt_circle": "4-hole_M4",
                     "fit_type": "sliding_fit"},
    )
    assert out["ok"] is True
    assert out["brief"].endswith(
        "Constraints: shaft_diameter 20mm, bolt_circle 4-hole_M4, fit_type sliding_fit."
    )


def test_field_order_is_stable_regardless_of_call_order(plan):
    """Passing the fields in a different order must not reorder the brief."""
    out = _call(
        plan,
        constraints=["c"],
        sequence=["s"],
        components=["b"],
        deliverable="a",
    )
    assert out["brief"] == "Deliverable: a. Components: b. Constraints: c. Sequence: s."



def test_extra_field_beside_an_explicit_brief_is_appended_not_dropped(plan):
    """A brief plus a stray semantic field must lose neither."""
    out = _call(plan, brief="Design a bracket", description="anodised black")
    assert out["ok"] is True
    assert out["brief"] == "Design a bracket description: anodised black."
    assert any("appended the extra field" in n for n in out["interpreted"])


def test_conflicting_autonomy_spellings_are_reported_not_dropped(plan):
    out = _call(plan, brief="Design a bracket", autonomy_default="auto", autonomy="manual")
    assert out["ok"] is True
    assert any("ignored autonomy='manual'" in n for n in out["interpreted"])


def test_unknown_extra_fields_are_folded_in_not_dropped(plan):
    out = _call(plan, deliverable="bracket", material="6061-T6")
    assert out["ok"] is True
    assert out["brief"] == "Deliverable: bracket. material: 6061-T6."


@pytest.mark.parametrize("spelling", [
    "mission_request", "mission_spec", "request", "goal",
    "objective", "task", "description", "spec",
])
def test_direct_spellings_of_brief(plan, spelling):
    """Recorded calls used mission_request= and mission_spec=; all eight are accepted."""
    text = ("Design a flanged bushing with a sliding-fit bore for a 20 mm shaft "
            "and a 4-hole M4 bolt circle on the flange.")
    out = _call(plan, **{spelling: text})
    assert out["ok"] is True
    assert out["brief"] == text
    assert out["interpreted"] == [f"{spelling}= read as brief="]


def test_empty_dag_plan_teaches_both_forms(plan):
    """Recorded call: dag_plan() with nothing at all."""
    out = _call(plan)
    assert out["ok"] is False
    hint = out["hint"]
    assert "brief=" in hint                     # form 1, named
    assert "deliverable=" in hint               # form 2, named
    assert 'dag_plan(brief="' in hint           # concrete example of form 1
    assert "components=[" in hint               # concrete example of form 2
    assert out["accepted_forms"] == [
        "brief=<one string>",
        "deliverable=/components=/constraints=/sequence=",
    ]


def test_blank_brief_string_is_treated_as_absent(plan):
    assert _call(plan, brief="   ")["ok"] is False


# ---------------------------------------------------------------------------
# dag_plan — the canonical call must not have moved
# ---------------------------------------------------------------------------

# Captured from `git show HEAD:services/dag/mcp_tools.py` before this widening.
# run_id and the runs directory are the only volatile parts.
CANONICAL_BASELINE = """{
  "ok": true,
  "run_id": "<RID>",
  "status": "paused",
  "checkpoint_db": "<RUNS>/<RID>.db",
  "graph_json": {},
  "hint": "Pass graph_json to dag_run to execute the planned mission. Review and edit the graph before running in production.",
  "next_actions": [
    "dag_run(graph_json=<result.graph_json>)"
  ]
}"""


def _normalize(raw: str, run_id: str, runs_dir: str) -> str:
    return raw.replace(runs_dir, "<RUNS>").replace(run_id, "<RID>")


def test_canonical_dag_plan_output_is_byte_identical(plan, tmp_path):
    raw = plan(brief="Design an aluminium bracket and run interference check")
    parsed = json.loads(raw)
    runs = str(tmp_path / "Chudware" / "runs")
    assert _normalize(raw, parsed["run_id"], runs) == CANONICAL_BASELINE
    # No echo keys leak into a call that needed no rewriting.
    assert "interpreted" not in parsed and "brief" not in parsed


@pytest.mark.anyio
async def test_canonical_dag_plan_works_through_fastmcp():
    """A bare **kwargs registers fine and then fails EVERY FastMCP call with
    "kwargs Field required" — this is the assertion that catches that."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("dag-dialect-test")
    MT.register_dag_tools(mcp)
    MT.register_dag_replan_tools(mcp)

    assert {t.name for t in await mcp.list_tools()} >= {
        "dag_plan", "dag_run", "dag_status", "dag_approve", "dag_abort", "dag_replan"}

    _content, result = await mcp.call_tool(
        "dag_plan", {"brief": "Design an aluminium bracket and run interference check"})
    assert json.loads(result["result"])["ok"] is True


@pytest.fixture()
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# dag_run — graph shapes
# ---------------------------------------------------------------------------

def test_dag_run_accepts_a_json_string_graph(run):
    out = _call(run, graph_json=json.dumps(GRAPH))
    assert out["ok"] is True
    assert out["interpreted"] == [
        "graph_json: parsed the JSON string you passed into a graph object"]


@pytest.mark.parametrize("spelling", ["graph", "plan", "json", "graph_dict"])
def test_dag_run_graph_spellings(run, spelling):
    out = _call(run, **{spelling: GRAPH})
    assert out["ok"] is True
    assert out["interpreted"] == [f"{spelling}= read as graph_json="]


def test_dag_run_wraps_a_bare_node_list(run):
    out = _call(run, nodes=GRAPH["nodes"])
    assert out["ok"] is True
    assert "wrapped it into an aicad.dag/1 document" in out["interpreted"][0]


def test_dag_run_rejects_unparseable_json_with_a_fix(run):
    out = _call(run, graph_json="{not json")
    assert out["ok"] is False
    assert "not valid JSON" in out["reason"]
    assert "dag_plan" in out["hint"]


def test_empty_dag_run_names_the_tool_that_produces_a_graph(run):
    """Recorded call: dag_run() with nothing, and nothing planned yet."""
    out = _call(run)
    assert out["ok"] is False
    hint = out["hint"]
    assert "dag_plan" in hint                          # the producer, named
    assert 'p = dag_plan(brief="' in hint              # step 1 of the sequence
    assert 'dag_run(graph_json=p["graph_json"])' in hint   # step 2
    assert out["next_actions"] == ["dag_plan(brief='<what you want built>')"]


def test_dag_run_reuses_the_last_planned_graph(run):
    MT._LAST["graph_json"] = GRAPH
    MT._LAST["graph_from"] = "run-abc"
    out = _call(run)
    assert out["ok"] is True
    assert any("reused the graph produced by the most recent dag_plan" in n
               for n in out["interpreted"])
    assert any("run-abc" in n for n in out["interpreted"])


def test_dag_plan_feeds_dag_run_without_copying_the_graph(plan, run):
    """The two-call sequence with no graph plumbing, when the planner produced one."""
    planned = _call(plan, brief="Design a bracket")
    assert planned["ok"] is True
    # This planner run produced no graph, so there is still nothing to reuse.
    assert _call(run)["ok"] is False
    MT._LAST["graph_json"] = GRAPH          # as a live planner would have set it
    MT._LAST["graph_from"] = planned["run_id"]
    assert _call(run)["ok"] is True


def test_dag_run_failure_teaches_the_graph_shape(run):
    out = _call(run, graph_json={"nope": 1})
    assert out["ok"] is False
    assert "schema: 'aicad.dag/1'" in out["hint"]
    assert "end_condition" in out["hint"]
    assert out["graph_keys"] == ["nope"]


# ---------------------------------------------------------------------------
# The rest of the family: a required run_id the model routinely omits
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tool", ["dag_status", "dag_approve", "dag_abort", "dag_replan"])
def test_missing_run_id_teaches_the_two_call_sequence(tool):
    out = _call(at._bind_tool(tool))
    assert out["ok"] is False
    assert "dag_run(graph_json=<graph>)" in out["reason"]
    assert "dag_plan(brief=" in out["reason"]
    assert out["known_run_ids"] == []


@pytest.mark.parametrize("tool", ["dag_status", "dag_approve", "dag_abort"])
def test_run_id_falls_back_to_this_sessions_run(tool, run):
    started = _call(run, graph_json=GRAPH)
    out = _call(at._bind_tool(tool))
    # Not necessarily ok (approve/abort need a live gate), but it must have
    # ADDRESSED the session's run rather than refusing for a missing run_id.
    assert "needs run_id" not in json.dumps(out)
    assert started["run_id"] in json.dumps(out)


def test_dag_status_unknown_run_lists_the_runs_that_exist(run):
    started = _call(run, graph_json=GRAPH)
    out = _call(at._bind_tool("dag_status"), run_id="00000000-0000-4000-8000-000000000000")
    assert out["ok"] is False
    assert out["known_run_ids"] == [started["run_id"]]
    assert started["run_id"] in out["hint"]


def test_dag_status_accepts_run_as_a_spelling(run):
    started = _call(run, graph_json=GRAPH)
    out = _call(at._bind_tool("dag_status"), run=started["run_id"])
    assert out["ok"] is True
    assert out["interpreted"] == ["run= read as run_id="]


# ---------------------------------------------------------------------------
# dag_replan — four required positionals
# ---------------------------------------------------------------------------

def test_dag_replan_missing_splice_names_the_full_shape(run):
    started = _call(run, graph_json=GRAPH)
    out = _call(at._bind_tool("dag_replan"), run_id=started["run_id"])
    assert out["ok"] is False
    assert "splice_nodes" in out["reason"] and "after_node_id" in out["reason"]
    assert "end_condition" in out["hint"] and "retry_budget" in out["hint"]
    assert f"dag_status(run_id='{started['run_id']}')" in out["hint"]


def test_dag_replan_accepts_nodes_and_after_spellings(run):
    started = _call(run, graph_json=GRAPH)
    node = {"id": "refit", "type": "act", "name": "Refit", "inputs": {},
            "on_fail": "pause", "autonomy": "approve", "end_condition": "fits"}
    out = _call(at._bind_tool("dag_replan"), run_id=started["run_id"],
                nodes=node, edges={"from": "refit", "to": "refit", "retry_budget": 2},
                after="n1")
    # It gets past argument binding — whatever the splice verdict is, the failure
    # is no longer "missing splice_nodes / after_node_id".
    assert "is missing" not in out.get("reason", "")
