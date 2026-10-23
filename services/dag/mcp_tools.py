"""mcp_tools.py — MCP tool handlers for the five DAG orchestrator tools.

Contract: contracts/dag_schema.md §9–§11.
Registered by: services/mcp/server.py (append-only block).
Resume path is executor.resume() — ONE implementation.

Tools:
  dag_plan    Run the mission-planner node and return a graph JSON that the caller
              can hand directly to dag_run. Calls node_runner with a "plan" node
              against the live agent loop.

  dag_run     Start (or resume) a DAG run from a graph JSON.
              - If run_id is omitted: starts a new run (executor.run()).
              - If run_id is supplied: resumes a paused run (executor.resume()).

  dag_status  Return the current state of a run: status, current node IDs, gate-
              pending flag, and node-level verdicts.  Reads the persisted
              .graph.json sidecar + checkpoint DB so it is restart-safe.

  dag_approve Resume a gate-paused run with decision="approve".
              MCP twin of executor.resume(run_id, decision="approve") — no separate
              gate logic here, one implementation.

  dag_abort   Cancel a run by injecting decision="reject" into the current gate
              (or marking the run failed if not currently paused).

All tools return a JSON string {ok, ...} following the project's _dumps/_err convention.
Never call the LLM directly — dag_plan delegates to executor.run() which uses node_runner,
which has its own agent_fn injection point.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Optional

from pydantic import Field

from services.dag import session
from services.dag.executor import default_runs_dir as _runs_dir
from services.dag.run_ids import validate_run_id
from services.agent.request_context import in_cell
from .storage import list_run_ids, resolve_run
from services.mcp.tool_registry import model_selectors

# Var-keyword parameters below are annotated ``Annotated[Any, Field(default=None)]``
# rather than left bare.  FastMCP's func_metadata turns a bare ``**kwargs`` into a
# REQUIRED pydantic field literally named "kwargs", which makes every FastMCP call —
# including the canonical documented one — fail with "kwargs Field required".  The
# annotation makes that field optional; FastMCP then passes {"kwargs": None}, which
# each tool pops before reading its dialect.  agent_turn._signature_spec still sees
# VAR_KEYWORD, so the built-in agent path keeps passing aliases through untouched.
_VAR_KW = Annotated[Any, Field(default=None)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2)


def _err(reason: str, hint: str = "", **extra: Any) -> str:
    if in_cell() and reason == "not_found":
        return _dumps({"ok": False, "reason": "not_found", "code": "not_found"})
    out: dict[str, Any] = {"ok": False, "reason": reason}
    if hint:
        out["hint"] = hint
    out.update(extra)
    return _dumps(out)


def _cell_control_run(run_id: Any, kwargs: dict) -> str:
    """Resolve explicit handles before desktop coercion, aliases or validation."""
    if run_id is None:
        run_id, _ = _pop_alias(kwargs, "run_id", "runid", "run", "id", "run_uuid",
                             "mission_id", "job_id")
        if run_id is None:
            run_id = session.last_run()
    return resolve_run(run_id).run_id


def _load_run_status(run_id: str, runs_dir: Path) -> dict[str, Any] | None:
    """Read the persisted graph sidecar + infer run status from checkpoint existence.

    Returns a status dict or None if the run is unknown.
    This is the restart-safe path: we read .graph.json from disk and check
    whether the DB still exists.  LangGraph checkpoint introspection is NOT
    used here because it requires re-opening the DB with a compiled graph —
    instead dag_status reports from the graph topology + the sidecar.

    Honest caveat: node-level status comes from the last executor.run()/resume()
    return value, which is NOT persisted by dag_status itself.  Only the graph
    topology is restart-safe; per-node verdicts are in-session only until
    a narration DB is added.  dag_status documents this limitation.
    """
    if in_cell():
        record = resolve_run(run_id)
        run_id, db_path, graph_path = record.run_id, record.db, record.graph
    else:
        run_id = validate_run_id(run_id)
        db_path = runs_dir / f"{run_id}.db"
        graph_path = runs_dir / f"{run_id}.graph.json"
    if not db_path.exists():
        return None
    graph_json: dict = {}
    if graph_path.exists():
        try:
            graph_json = json.loads(graph_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {
        "run_id": run_id,
        "checkpoint_db": str(db_path),
        "graph_json": graph_json,
    }

# ---------------------------------------------------------------------------
# Input dialect — the tools own their own argument vocabulary
# ---------------------------------------------------------------------------
#
# Every DAG tool below takes **kwargs.  That is deliberate: services/agent/
# agent_turn.py::_reconcile_kwargs inspects the target signature and REJECTS
# unknown keyword arguments before the call, so a tool with a fixed signature
# can never see an alias.  Declaring **kwargs sets ``var_kw`` on the signature
# spec, which makes the reconciler pass the model's spelling through untouched
# and lets the tool normalize it here, where the meaning is known.
#
# Two rules, borrowed from services/mcp/cad_doc_tools.py:
#   * every rewrite is ECHOED back as ``interpreted``: [...] so the model learns
#     the canonical spelling instead of leaning on the alias forever;
#   * genuinely ambiguous or empty input is a TEACHING ERROR naming the accepted
#     forms with a concrete example — never a silent guess.

_MAX_INTERPRETED_NOTES = 12

# Session memory so a plan → run → status chain does not force the model to
# copy UUIDs and whole graph documents back and forth.  Reuse is always echoed.
# The slot itself lives in services.dag.session because mission_start and
# MissionDispatch.dag_plan also PRODUCE plans; a dict private to this module
# made their graphs invisible to dag_run.  This is the same object, not a copy.
_LAST: dict[str, Any] = session.LAST


def _interpreted_notes(notes: list) -> list:
    """De-duplicate and cap the echo list (house convention: cad_doc_tools)."""
    seen: list = []
    for n in notes:
        if n and n not in seen:
            seen.append(n)
    if len(seen) > _MAX_INTERPRETED_NOTES:
        extra = len(seen) - _MAX_INTERPRETED_NOTES
        seen = seen[:_MAX_INTERPRETED_NOTES] + [f"(+{extra} more of the same kind)"]
    return seen


def _norm_key(key: Any) -> str:
    """Fold a keyword spelling to its comparison form ("graphJSON" -> "graphjson")."""
    return "".join(ch for ch in str(key).lower() if ch.isalnum())


def _pop_alias(kwargs: dict, *names: str) -> tuple[Any, str]:
    """Pop the first present spelling of one parameter.  Canonical name wins.

    Returns ``(value, spelling_used)``; ``(None, "")`` when absent or None-valued.
    """
    index: dict[str, str] = {}
    for key in kwargs:
        index.setdefault(_norm_key(key), key)
    for name in names:
        key = index.get(_norm_key(name))
        if key is not None and kwargs.get(key) is not None:
            return kwargs.pop(key), key
    return None, ""


def _render_value(val: Any) -> str:
    """Flatten a model-supplied field into one readable clause."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, bool):
        return "yes" if val else "no"
    if isinstance(val, dict):
        return ", ".join(f"{k} {_render_value(v)}".strip()
                         for k, v in val.items() if _render_value(v))
    if isinstance(val, (set, frozenset)):
        val = sorted(val, key=str)
    if isinstance(val, (list, tuple)):
        return ", ".join(part for part in (_render_value(v) for v in val) if part)
    return str(val)


# A brief written as one string.  Any of these spellings IS the brief.
_BRIEF_DIRECT = ("brief", "mission_request", "mission_spec", "request", "goal",
                 "objective", "task", "description", "spec", "prompt", "text")

# A brief written as semantic fields.  Order here is the order they are joined
# into the synthesized brief, and it never changes.
_BRIEF_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Deliverable", ("deliverable", "deliverables", "outcome", "product", "part")),
    ("Components", ("components", "component", "parts", "features", "elements",
                    "subassemblies")),
    ("Constraints", ("constraints", "constraint", "requirements", "requirement",
                     "criteria")),
    ("Sequence", ("sequence", "steps", "step", "order", "stages", "phases")),
)


def _synthesize_brief(kwargs: dict, notes: list) -> str:
    """Join the semantic plan fields left in *kwargs* into one brief string.

    Consumes every remaining key: the four known fields first, in the fixed
    _BRIEF_FIELDS order, then anything else in sorted order so no detail the
    model bothered to write is silently dropped.  Appends one echo line to
    *notes* naming the fields it used.  Returns "" when nothing usable is left.
    """
    clauses: list[str] = []
    used: list[str] = []
    for label, names in _BRIEF_FIELDS:
        val, spelling = _pop_alias(kwargs, *names)
        text = _render_value(val)
        if not text:
            continue
        clauses.append(f"{label}: {text}.")
        used.append(spelling)
    for key in sorted(kwargs):
        text = _render_value(kwargs[key])
        if not text:
            continue
        clauses.append(f"{key}: {text}.")
        used.append(key)
    kwargs.clear()
    if not clauses:
        return ""
    brief = " ".join(clauses)
    notes.append(
        "brief= was not supplied — synthesized one from "
        + ", ".join(used)
        + f": {brief!r}. Pass brief=<one string> to control the wording exactly."
    )
    return brief


_PLAN_FORMS_HINT = (
    "dag_plan takes the mission either way:\n"
    "  (1) one string  — dag_plan(brief=\"Design an aluminium bracket and run an "
    "interference check\")\n"
    "  (2) semantic fields — dag_plan(deliverable=\"flanged bushing\", "
    "components=[\"sliding-fit bore\", \"4-hole M4 bolt circle\"], "
    "constraints=[\"20 mm shaft\", \"sliding fit\"], "
    "sequence=[\"model the bushing\", \"add the flange\", \"check the fit\"])\n"
    "Form (2) is joined into a brief for you and echoed back as `interpreted`. "
    "brief also answers to mission_request / mission_spec / request / goal / "
    "objective / task / description / spec."
)


def _coerce_graph(val: Any, notes: list) -> tuple[dict | None, str]:
    """Accept a graph document as a dict OR as a JSON string.

    Returns ``(graph, error_reason)``; exactly one is truthy.
    """
    if isinstance(val, dict):
        return val, ""
    if isinstance(val, str):
        text = val.strip()
        if not text:
            return None, "graph_json was an empty string"
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            return None, (
                f"graph_json was a string but not valid JSON ({exc}). Pass the graph "
                "as an object, or as a JSON string that parses to one."
            )
        if not isinstance(parsed, dict):
            return None, (
                f"graph_json parsed to a {type(parsed).__name__}, not an aicad.dag/1 "
                "object. Expected {schema, id, name, nodes, edges}."
            )
        notes.append("graph_json: parsed the JSON string you passed into a graph object")
        return parsed, ""
    return None, (
        f"graph_json must be an aicad.dag/1 object or a JSON string, got "
        f"{type(val).__name__}"
    )


def _remember_run(run_id: Any) -> None:
    session.remember_run(run_id)


def _remember_result(result: Any) -> None:
    """Record both the run id and the live gate state an executor return revealed."""
    if isinstance(result, dict):
        session.remember_run(result.get("run_id"))
        session.remember_result(result)


def _resolve_run_id(kwargs: dict, tool: str, notes: list) -> tuple[str, str]:
    """Pull a run_id out of *kwargs*, falling back to this session's last run.

    Returns ``(run_id, error_reason)``; exactly one is truthy.
    """
    val, spelling = _pop_alias(kwargs, "run_id", "runid", "run", "id", "run_uuid",
                               "mission_id", "job_id")
    if isinstance(val, str) and val.strip():
        if _norm_key(spelling) != "runid":
            notes.append(f"{spelling}= read as run_id=")
        try:
            return validate_run_id(val.strip()), ""
        except ValueError as exc:
            return "", str(exc)
    if val is not None:
        return "", (f"run_id must be the UUID4 string from dag_run's response, got "
                    f"{type(val).__name__}")
    last = session.last_run()
    if last:
        notes.append(
            f"run_id was not supplied — used {last!r}, the most recent run started in "
            "this session. Pass run_id= explicitly to address a different run."
        )
        try:
            return validate_run_id(last), ""
        except ValueError as exc:
            return "", str(exc)
    return "", (
        f"{tool} needs run_id — the UUID4 a run is identified by, and nothing in this "
        "session has started one yet.\n"
        "  Two-call sequence: r = dag_run(graph_json=<graph>)  ->  "
        f"{tool}(run_id=r['run_id'])\n"
        "  Start from a brief instead: p = dag_plan(brief=\"...\"); "
        "r = dag_run(graph_json=p['graph_json'])"
    )


def _known_run_ids(base: Path, limit: int = 5) -> list[str]:
    """Run ids that actually exist on disk — so a 'not found' error can name them."""
    if in_cell():
        return list_run_ids(limit)
    try:
        dbs = sorted(base.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    return [p.stem for p in dbs[:limit]]


def _prep_resume(tool: str, run_id: Any, graph_json: Any, autonomy_default: str,
                 kwargs: dict) -> tuple[str, Any, str, list, str | None]:
    """Normalize the (run_id, graph_json, autonomy_default) trio the resume tools share.

    Returns ``(run_id, graph_json, autonomy_default, notes, error_json)``.
    ``error_json`` is a ready-to-return _err() payload, or None when good to go.
    """
    notes: list = []
    if in_cell():
        try:
            run_id = _cell_control_run(run_id, kwargs)
        except KeyError:
            return "", graph_json, autonomy_default, notes, _err("not_found")
    if kwargs:
        if graph_json is None:
            alt, spelling = _pop_alias(kwargs, "graph_json", "graph", "plan", "json",
                                       "graph_dict", "dag", "graph_doc")
            if alt is not None:
                graph_json = alt
                notes.append(f"{spelling}= read as graph_json=")
        aut_alt, aut_spelling = _pop_alias(kwargs, "autonomy_default", "autonomy",
                                           "autonomy_level", "default_autonomy")
        if isinstance(aut_alt, str) and aut_alt.strip():
            if autonomy_default == "approve":
                autonomy_default = aut_alt.strip()
                notes.append(f"{aut_spelling}= read as autonomy_default=")
            else:
                notes.append(f"ignored {aut_spelling}={aut_alt!r} — autonomy_default="
                             f"{autonomy_default!r} was set explicitly")

    if isinstance(run_id, str) and run_id.strip():
        run_id = run_id.strip()
    else:
        run_id, id_error = _resolve_run_id(kwargs, tool, notes)
        if id_error:
            known = _known_run_ids(_runs_dir())
            return "", graph_json, autonomy_default, notes, _err(
                id_error,
                hint=(("Runs already on disk (most recent first): " + ", ".join(known))
                      if known else "No runs exist on disk yet either."),
                known_run_ids=known,
            )

    try:
        run_id = validate_run_id(run_id)
    except ValueError as exc:
        return "", graph_json, autonomy_default, notes, _err(
            str(exc), hint="Use the run_id exactly as returned by dag_run."
        )

    if graph_json is not None:
        graph_json, coerce_error = _coerce_graph(graph_json, notes)
        if coerce_error:
            return run_id, None, autonomy_default, notes, _err(
                coerce_error,
                hint=(f"{tool} can also be called WITHOUT graph_json — the executor then "
                      "loads the graph from the sidecar written when the run started."),
            )
    return run_id, graph_json, autonomy_default, notes, None


# ---------------------------------------------------------------------------
# Registration function (called from server.py)
# ---------------------------------------------------------------------------

def register_dag_tools(mcp) -> None:
    """Register the five DAG orchestrator MCP tools onto *mcp*.

    Follows the same register_*_tools(mcp) pattern as hydraulic_tools / cost_tools.
    Appended to services/mcp/server.py (append-only).
    """

    @mcp.tool()
    def dag_plan(
        brief: Optional[str] = None,
        graph_name: str = "mission",
        autonomy_default: str = "approve",
        **kwargs: _VAR_KW,
    ) -> str:
        """Decompose a mission description into an aicad.dag/1 graph JSON.

        Runs a 'plan' node through the mission-planner agent loop and returns the
        resulting graph JSON (ready to hand to dag_run).  The plan node calls the
        live agent with the brief as its request and expects the agent to produce
        a JSON graph document.

        TWO WAYS TO DESCRIBE THE MISSION — both are first-class:

          (1) ONE STRING:
                dag_plan(brief="Design an aluminium bracket and run an interference check")
              brief also answers to mission_request / mission_spec / request / goal /
              objective / task / description / spec / prompt / text.

          (2) SEMANTIC FIELDS, which are joined into a brief for you:
                dag_plan(deliverable="flanged bushing",
                         components=["sliding-fit bore", "4-hole M4 bolt circle"],
                         constraints=["20 mm shaft", "sliding fit"],
                         sequence=["model the bushing", "add the flange", "check fit"])
              -> brief "Deliverable: flanged bushing. Components: sliding-fit bore,
                 4-hole M4 bolt circle. Constraints: 20 mm shaft, sliding fit.
                 Sequence: model the bushing, add the flange, check fit."
              The order is always deliverable, components, constraints, sequence, then
              any other field you passed, in sorted order — nothing is dropped.  Each
              field takes a string, a list, or a dict ({"shaft": "20 mm"} reads as
              "shaft 20 mm").  Aliases: deliverables/outcome/product/part,
              component/parts/features/elements, constraint/requirements/criteria,
              steps/order/stages/phases.

        Whenever the brief is synthesized or read from an alias, the response carries
        the exact string as `brief` plus an `interpreted`:[…] line explaining the
        rewrite — nothing is guessed silently.  Calling dag_plan() with no mission at
        all is an error naming both forms.

        Args:
            brief:             Plain-English mission description (form 1 above).
            graph_name:        Human label for the resulting graph (default: "mission").
                               Also accepts name / label / title / mission_name.
            autonomy_default:  Run-level autonomy floor — "auto" | "approve" | "manual".
                               "approve" (default) gates every node for human oversight.
                               Also accepts autonomy / autonomy_level.

        Returns:
            {ok, run_id, graph_json, status, checkpoint_db}
            graph_json is the aicad.dag/1 graph produced by the planner; hand it
            straight to dag_run (or omit it there — dag_run reuses this one).
            Plus {brief, interpreted} when any input was rewritten.
            On failure: {ok: false, reason, hint}.
        """
        # FastMCP hands the var-kw field through as {"kwargs": None}; drop it.
        kwargs.pop("kwargs", None)
        try:
            from services.dag import executor
            from services.dag.graph_loader import validate_graph
        except ImportError as exc:
            return _err(f"DAG executor unavailable: {exc}",
                        hint="Ensure services/dag is on PYTHONPATH.")

        notes: list[str] = []
        if kwargs:
            if not (isinstance(brief, str) and brief.strip()):
                # Only claim a brief spelling when there is no brief yet — otherwise
                # the extra field is content, and the leftover pass folds it in.
                alt, spelling = _pop_alias(kwargs, *_BRIEF_DIRECT)
                text = _render_value(alt)
                if text:
                    brief = text
                    notes.append(f"{spelling}= read as brief=")
            name_alt, name_spelling = _pop_alias(
                kwargs, "graph_name", "name", "label", "title", "mission_name")
            if isinstance(name_alt, str) and name_alt.strip():
                if graph_name == "mission":
                    graph_name = name_alt.strip()
                    notes.append(f"{name_spelling}= read as graph_name=")
                else:
                    notes.append(f"ignored {name_spelling}= — graph_name= was set explicitly")
            aut_alt, aut_spelling = _pop_alias(
                kwargs, "autonomy_default", "autonomy", "autonomy_level", "default_autonomy")
            if isinstance(aut_alt, str) and aut_alt.strip():
                if autonomy_default == "approve":
                    autonomy_default = aut_alt.strip()
                    notes.append(f"{aut_spelling}= read as autonomy_default=")
                else:
                    notes.append(
                        f"ignored {aut_spelling}={aut_alt!r} — autonomy_default="
                        f"{autonomy_default!r} was set explicitly")

        if not (isinstance(brief, str) and brief.strip()):
            # Everything still in kwargs is mission content described field-by-field.
            brief = _synthesize_brief(kwargs, notes)
        else:
            brief = brief.strip()
            leftover = _synthesize_brief(dict(kwargs), [])
            if leftover:
                brief = f"{brief} {leftover}"
                notes.append(f"appended the extra field(s) you passed to the brief: {leftover!r}")
            kwargs.clear()

        if not brief:
            return _err(
                "dag_plan needs a mission to plan — no brief and no plan fields were given.",
                hint=_PLAN_FORMS_HINT,
                accepted_forms=["brief=<one string>",
                                "deliverable=/components=/constraints=/sequence="],
            )

        # Build a minimal single-node "plan" graph whose output is the real graph.
        plan_graph: dict[str, Any] = {
            "schema": "aicad.dag/1",
            "id":     f"plan-{graph_name}",
            "name":   f"Planner: {graph_name}",
            "nodes": [
                {
                    "id":            "plan",
                    "type":          "plan",
                    "name":          "Mission planner",
                    "inputs":        {"request": brief},
                    "tools":         None,
                    "end_condition": "Plan decomposed into actionable DAG nodes.",
                    "verify":        None,
                    "on_fail":       "pause",
                    "autonomy":      "auto",
                }
            ],
            "edges": [],
        }

        try:
            result = executor.run(
                plan_graph,
                autonomy_default=autonomy_default,
            )
        except Exception as exc:
            return _err(f"Planner run failed: {exc}",
                        hint="Check the brief and retry, or supply a graph JSON directly to dag_run.",
                        brief=brief)

        # Extract the graph JSON from the planner's output if present.
        node_output = result.get("nodes", {}).get("plan", {}).get("output", {})
        graph_json_out = node_output.get("data", {}).get("agent_result") or {}

        _remember_run(result.get("run_id"))
        session.remember_graph(graph_json_out, source=str(result.get("run_id") or ""))

        out: dict[str, Any] = {
            "ok": True,
            "run_id": result["run_id"],
            "status": result.get("status", "done"),
            "checkpoint_db": result.get("checkpoint_db", ""),
            "graph_json": graph_json_out,
            "hint": (
                "Pass graph_json to dag_run to execute the planned mission. "
                "Review and edit the graph before running in production."
            ),
            "next_actions": [
                "dag_run(graph_json=<result.graph_json>)",
            ],
        }
        if notes:
            # Say exactly what was planned, in the words the planner actually saw.
            out["brief"] = brief
            out["interpreted"] = _interpreted_notes(notes)
        return _dumps(out)

    @mcp.tool()
    @model_selectors("run_id")
    def dag_run(
        graph_json: Any = None,
        run_id: Optional[str] = None,
        autonomy_default: str = "approve",
        initial_state: Optional[dict] = None,
        **kwargs: _VAR_KW,
    ) -> str:
        """Start or resume a DAG run from an aicad.dag/1 graph document.

        - If run_id is omitted: starts a new run (executor.run()).
        - If run_id is supplied and the matching checkpoint exists: resumes a
          paused run (executor.resume()).  This is the same surface as dag_approve
          but without fixing the decision — pass decision="approve"|"reject" via
          dag_approve / dag_abort instead when you want to resume a gate.
        - If run_id is supplied and NO checkpoint exists yet, the run is STARTED
          under that id (echoed as `interpreted`).  mission_start hands back
          run_id="mission" for a graph that has never run, so treating a named
          first start as a failed resume made the compiled mission unreachable.

        WHERE THE GRAPH COMES FROM: dag_plan or mission_start produces it.  The
        normal sequences are
            p = dag_plan(brief="...")            ->  dag_run(graph_json=p["graph_json"])
            m = mission_start(request=..., manifest=...)
                                                ->  dag_run(graph_json=m["graph"])
        Calling dag_run() with no graph reuses the graph from the most recent
        dag_plan OR mission_start in this session (and says so in `interpreted`);
        if nothing has been planned yet, it is an error naming both producers.

        ACCEPTED GRAPH FORMS (every rewrite is echoed as `interpreted`):
          graph_json={...}                  the aicad.dag/1 object itself
          graph_json='{"schema": ...}'      the same object as a JSON string — parsed
          graph= / plan= / json= /          other spellings of graph_json
            graph_dict= / dag= / graph_doc=
          nodes=[...], edges=[...]          bare node/edge lists, wrapped into a
                                            {schema, id, name, nodes, edges} document

        Args:
            graph_json:        aicad.dag/1 graph document, or a JSON string of one,
                               or omitted to reuse the last planned graph.
            run_id:            UUID4 of an existing paused run (omit for a new run).
                               Also accepts run / run_uuid.
            autonomy_default:  Run-level autonomy floor — "auto" | "approve" | "manual".
                               Also accepts autonomy / autonomy_level.
            initial_state:     Extra key→value pairs merged into the initial DAG state.
                               Also accepts state / initial_inputs.

        Returns:
            {ok, run_id, status, nodes, checkpoint_db, interrupted?}
            interrupted present and non-empty → run is paused at a gate; call dag_approve.
            Plus {interpreted} when any input was rewritten.
            On failure: {ok: false, reason, hint}.
        """
        kwargs.pop("kwargs", None)   # FastMCP var-kw placeholder
        try:
            from services.dag import executor
        except ImportError as exc:
            return _err(f"DAG executor unavailable: {exc}")

        notes: list[str] = []
        if kwargs:
            if graph_json is None:
                alt, spelling = _pop_alias(kwargs, "graph_json", "graph", "plan", "json",
                                           "graph_dict", "dag", "graph_doc", "document",
                                           "graph_json_str")
                if alt is not None:
                    graph_json = alt
                    notes.append(f"{spelling}= read as graph_json=")
            if run_id is None:
                rid, rid_spelling = _pop_alias(kwargs, "run_id", "run", "run_uuid")
                if isinstance(rid, str) and rid.strip():
                    run_id = rid.strip()
                    if _norm_key(rid_spelling) != "runid":
                        notes.append(f"{rid_spelling}= read as run_id=")
            aut_alt, aut_spelling = _pop_alias(
                kwargs, "autonomy_default", "autonomy", "autonomy_level", "default_autonomy")
            if isinstance(aut_alt, str) and aut_alt.strip():
                if autonomy_default == "approve":
                    autonomy_default = aut_alt.strip()
                    notes.append(f"{aut_spelling}= read as autonomy_default=")
                else:
                    notes.append(f"ignored {aut_spelling}={aut_alt!r} — autonomy_default="
                                 f"{autonomy_default!r} was set explicitly")
            if initial_state is None:
                st_alt, st_spelling = _pop_alias(kwargs, "initial_state", "state",
                                                 "initial_inputs")
                if isinstance(st_alt, dict):
                    initial_state = st_alt
                    if _norm_key(st_spelling) != "initialstate":
                        notes.append(f"{st_spelling}= read as initial_state=")
            if graph_json is None:
                nodes_alt, nodes_spelling = _pop_alias(kwargs, "nodes", "node_list", "steps")
                if isinstance(nodes_alt, list) and nodes_alt:
                    edges_alt, _ = _pop_alias(kwargs, "edges", "edge_list")
                    graph_json = {
                        "schema": "aicad.dag/1",
                        "id": "adhoc",
                        "name": "Ad-hoc graph",
                        "nodes": nodes_alt,
                        "edges": edges_alt if isinstance(edges_alt, list) else [],
                    }
                    notes.append(
                        f"{nodes_spelling}= was a bare node list — wrapped it into an "
                        "aicad.dag/1 document {schema, id, name, nodes, edges}"
                    )

        if graph_json is None:
            remembered = session.planned_graph()
            if remembered:
                graph_json = remembered
                notes.append(
                    "graph_json was not supplied — reused the graph produced by the most "
                    f"recent dag_plan/mission_start in this session ({session.planned_from()!r}). "
                    "Pass graph_json= explicitly to run a different graph."
                )
            else:
                return _err(
                    "dag_run needs graph_json — an aicad.dag/1 graph document — and no "
                    "graph has been planned in this session yet.",
                    hint=(
                        "Two tools PRODUCE a graph; either one makes dag_run runnable:\n"
                        "  1. p = dag_plan(brief=\"Design an aluminium bracket and run an "
                        "interference check\")  ->  dag_run(graph_json=p[\"graph_json\"])\n"
                        "  2. m = mission_start(request=..., manifest=...)  ->  "
                        "dag_run(graph_json=m[\"graph\"])\n"
                        "Hand-authoring one instead? graph_json={\"schema\": \"aicad.dag/1\", "
                        "\"id\": \"m1\", \"name\": \"Mission\", \"nodes\": [...], \"edges\": [...]} "
                        "— a JSON string of that object is accepted too."
                    ),
                    next_actions=["dag_plan(brief='<what you want built>')",
                                  "mission_start(request='<the ask>', manifest=<aicad.deliverables/1>)"],
                )

        graph_json, coerce_error = _coerce_graph(graph_json, notes)
        if coerce_error:
            return _err(
                coerce_error,
                hint=("Get a valid graph from dag_plan(brief='...') and pass its "
                      "graph_json straight through."),
            )


        if run_id:
            try:
                run_id = validate_run_id(run_id)
            except ValueError as exc:
                return _err(str(exc), hint="Use a simple identifier or omit run_id.")
        # A run_id naming a run that has never checkpointed is a FIRST START under
        # a chosen id, not a resume of something missing.  mission_start returns
        # run_id="mission" for a freshly compiled graph, so routing that to
        # executor.resume() answered the documented sequence with "Run not found".
        if in_cell():
            try:
                starting_named = bool(run_id) and not resolve_run(run_id).db.exists()
            except KeyError:
                starting_named = bool(run_id)
        else:
            starting_named = bool(run_id) and not (_runs_dir() / f"{run_id}.db").exists()
        if starting_named:
            notes.append(
                f"run_id={run_id!r} has no checkpoint yet — started a NEW run under that "
                "id instead of resuming. Pass a run_id from a previous dag_run response "
                "to resume one."
            )

        try:
            from services.agent.online_turn import current_tool_session_id
            parent_session_id = current_tool_session_id()
            if in_cell():
                from services.agent.chudcode_host import get_default_chudcode_host
                if not parent_session_id:
                    raise KeyError("not_found")
                get_default_chudcode_host().session_launch_params(parent_session_id)
            state = dict(initial_state or {})
            if parent_session_id:
                state["session_id"] = parent_session_id
            if run_id and not starting_named:
                result = executor.resume(
                    run_id,
                    graph_json=graph_json,
                    autonomy_default=autonomy_default,
                )
            else:
                result = executor.run(
                    graph_json,
                    initial_state=state,
                    run_id=run_id or None,
                    autonomy_default=autonomy_default,
                )
        except KeyError:
            return _err("not_found", code="not_found")
        except FileNotFoundError as exc:
            return _err(f"Run not found: {exc}",
                        hint="Omit run_id to start a new run, or verify the run_id is correct.")
        except Exception as exc:
            return _err(
                f"DAG run failed: {exc}",
                hint=(
                    "graph_json must be {schema: 'aicad.dag/1', id, name, nodes: [...], "
                    "edges: [...]}, and every node needs id/type/name/inputs/end_condition/"
                    "on_fail/autonomy. Call dag_plan(brief='...') to have a valid graph "
                    "built for you rather than repairing this one by hand."
                ),
                graph_keys=sorted(graph_json) if isinstance(graph_json, dict) else None,
            )

        _remember_result(result)
        interrupted = result.get("interrupted", [])
        out: dict[str, Any] = {
            "ok": True,
            "run_id": result["run_id"],
            "status": result.get("status", "unknown"),
            "nodes": result.get("nodes", {}),
            "checkpoint_db": result.get("checkpoint_db", ""),
        }
        if interrupted:
            out["interrupted"] = interrupted
            out["hint"] = "Run paused at gate. Call dag_approve to proceed or dag_abort to cancel."
            out["next_actions"] = [
                f"dag_approve(run_id='{result['run_id']}')",
                f"dag_abort(run_id='{result['run_id']}')",
                f"dag_status(run_id='{result['run_id']}')",
            ]
        else:
            out["hint"] = f"Run finished with status={result.get('status', 'unknown')!r}."
            out["next_actions"] = [
                f"dag_status(run_id='{result['run_id']}')",
            ]
        if notes:
            out["interpreted"] = _interpreted_notes(notes)
        return _dumps(out)

    @mcp.tool()
    @model_selectors("run_id")
    def dag_status(
        run_id: Optional[str] = None,
        runs_dir: Optional[str] = None,
        **kwargs: _VAR_KW,
    ) -> str:
        """Return the current state of a DAG run.

        Reads the persisted .graph.json sidecar and checkpoint DB to determine
        whether the run exists and what its graph topology looks like.
        Restart-safe: survives app restart (reads from disk, not from in-memory state).

        NOTE: per-node status verdicts are only fully available in the same session
        as the run/resume call; dag_status reports topology from the graph sidecar
        and checkpoint existence from the DB.  A future mission-control
        narration DB will provide full cross-restart node verdicts.

        Args:
            run_id:    UUID4 of the run to inspect.  Omit it to inspect the most
                       recent run started in this session (the reuse is echoed as
                       `interpreted`).  Also accepts run / id / run_uuid.
            runs_dir:  Override the default runs directory (tests / custom installs).

        Returns:
            {ok, run_id, db_exists, graph_name, node_count, node_ids,
             gate_pending, gate_pending_known, status, checkpoint_db}
            gate_pending is TRI-STATE: true / false when this process ran or resumed
            the run (then gate_pending_known is true and gate_nodes names the gates),
            and null when it did not — a run started before a restart or in another
            process is genuinely unknown, never reported as false.
            A run_id that was planned but never executed says so, and points at
            dag_run rather than listing unrelated run ids.
            On unknown run: {ok: false, reason, hint, known_run_ids}.
        """
        kwargs.pop("kwargs", None)   # FastMCP var-kw placeholder
        if in_cell():
            try:
                run_id = _cell_control_run(run_id, kwargs)
            except KeyError:
                return _err("not_found")
        notes: list[str] = []
        if kwargs:
            dir_alt, dir_spelling = _pop_alias(kwargs, "runs_dir", "run_dir", "dir", "path")
            if isinstance(dir_alt, str) and dir_alt and runs_dir is None:
                runs_dir = dir_alt
                notes.append(f"{dir_spelling}= read as runs_dir=")
        if not (isinstance(run_id, str) and run_id.strip()):
            run_id = None
        if run_id is None:
            run_id, id_error = _resolve_run_id(kwargs, "dag_status", notes)
            if id_error:
                known = _known_run_ids(Path(runs_dir) if runs_dir else _runs_dir())
                return _err(
                    id_error,
                    hint=(("Runs already on disk (most recent first): "
                           + ", ".join(known)) if known else
                          "No runs exist on disk yet either."),
                    known_run_ids=known,
                    next_actions=["dag_plan(brief='<what you want built>')"],
                )
        else:
            run_id = run_id.strip()

        try:
            run_id = validate_run_id(run_id)
        except ValueError as exc:
            return _err(str(exc), hint="Use the run_id exactly as returned by dag_run.")

        base = _runs_dir() if in_cell() else Path(runs_dir) if runs_dir else _runs_dir()
        try:
            info = _load_run_status(run_id, base)
        except KeyError:
            return _err("not_found", code="not_found")
        if in_cell() and info is None:
            return _err("not_found", code="not_found")
        if info is None:
            known = _known_run_ids(base)
            planned = session.planned_graph()
            if planned is not None and run_id in (session.planned_from(),
                                                  str(planned.get("id") or "")):
                # The id is real, the run just has not been executed yet.  Listing
                # five unrelated run ids here (live 2026-08-06) told the model its
                # own mission id was wrong and sent it hunting for a different one.
                return _err(
                    f"Run {run_id!r} is PLANNED but has not been started — nothing has "
                    f"executed it yet, so there is no {run_id}.db to report on.",
                    hint=(f"Start it: dag_run(run_id={run_id!r}) runs the graph already "
                          "planned in this session under that id. Then dag_status "
                          f"(run_id={run_id!r}) will report node state."),
                    planned_node_count=len(planned.get("nodes") or []),
                    next_actions=[f"dag_run(run_id={run_id!r})"],
                )
            return _err(
                f"Run {run_id!r} not found — no {run_id}.db under {base}",
                hint=(
                    ("The runs that DO exist there (most recent first): "
                     + ", ".join(known) + ". Pass one of those as run_id.")
                    if known else
                    (f"That directory holds no runs at all. Start one first: "
                     "p = dag_plan(brief='...'); dag_run(graph_json=p['graph_json']) "
                     "— then pass the run_id it returns.")
                ),
                known_run_ids=known,
            )

        run_id = info["run_id"]
        graph = info.get("graph_json", {})
        nodes = graph.get("nodes", [])
        node_ids = [n["id"] for n in nodes]

        # gate_pending used to be hardcoded False and commented as "conservative".
        # It is not conservative: `"gate_pending": false` on a run sitting at a gate
        # reads as "nothing to approve", and the caller's next decision is exactly
        # whether to call dag_approve. Whoever ran or resumed the run in THIS
        # process recorded what it saw; use that. Cross-restart it is genuinely
        # unknown, and unknown is reported as null plus gate_pending_known=false —
        # never as a boolean the reader cannot tell apart from an observation.
        observed = session.run_result(run_id)

        out: dict[str, Any] = {
            "ok": True,
            "run_id": run_id,
            "db_exists": True,
            "graph_name": graph.get("name", ""),
            "node_count": len(nodes),
            "node_ids": node_ids,
            "gate_pending": observed["gate_pending"] if observed else None,
            "gate_pending_known": observed is not None,
            "status": observed["status"] if observed else "unknown_after_restart",
            "checkpoint_db": info["checkpoint_db"],
        }
        if observed:
            out["gate_nodes"] = observed["gates"]
            out["hint"] = (
                f"Observed in this session: status={observed['status']!r}, "
                + (f"paused at {', '.join(observed['gates']) or 'a gate'} — "
                   "dag_approve to continue, dag_abort to cancel."
                   if observed["gate_pending"] else
                   "not waiting at a gate.")
            )
            out["next_actions"] = (
                [f"dag_approve(run_id='{run_id}')", f"dag_abort(run_id='{run_id}')"]
                if observed["gate_pending"] else
                [f"mission_status(run_id='{run_id}')"]
            )
        else:
            out["hint"] = (
                "This process never ran this run, so whether it is gate-paused is "
                "UNKNOWN (gate_pending is null, not false). Per-node status lives in "
                "the session that called dag_run/dag_approve; to learn the current "
                "state, resume it with dag_run(run_id=...) and read interrupted/status."
            )
            out["next_actions"] = [
                f"dag_run(run_id='{run_id}')  # resume and report real status",
                f"dag_abort(run_id='{run_id}')    # to cancel the run",
            ]
        if notes:
            out["interpreted"] = _interpreted_notes(notes)
        return _dumps(out)

    @mcp.tool()
    @model_selectors("run_id")
    def dag_approve(
        run_id: Optional[str] = None,
        graph_json: Any = None,
        autonomy_default: str = "approve",
        **kwargs: _VAR_KW,
    ) -> str:
        """Resume a gate-paused DAG run with decision='approve'.

        MCP twin of executor.resume(run_id, decision='approve').  This is ONE
        implementation — dag_approve delegates directly to executor.resume() and
        carries no gate logic of its own.

        Args:
            run_id:            UUID4 of the paused run (from dag_run's response).
                               Omit it to approve the most recent run started in this
                               session — the reuse is echoed as `interpreted`.
                               Also accepts run / id / run_uuid.
            graph_json:        The original aicad.dag/1 graph document, as an object or
                               as a JSON string.  If omitted, the executor loads it from
                               the persisted sidecar written at run start (restart-safe
                               path).  Also accepts graph / plan / json / graph_dict.
            autonomy_default:  Must match the autonomy_default used in dag_run.
                               Also accepts autonomy / autonomy_level.

        Returns:
            Same shape as dag_run: {ok, run_id, status, nodes, checkpoint_db, interrupted?}.
            Plus {interpreted} when any input was rewritten.
            On failure: {ok: false, reason, hint}.
        """
        kwargs.pop("kwargs", None)   # FastMCP var-kw placeholder
        try:
            from services.dag import executor
        except ImportError as exc:
            return _err(f"DAG executor unavailable: {exc}")

        run_id, graph_json, autonomy_default, notes, arg_error = _prep_resume(
            "dag_approve", run_id, graph_json, autonomy_default, kwargs)
        if arg_error is not None:
            return arg_error

        try:
            result = executor.resume(
                run_id,
                graph_json=graph_json,
                decision="approve",
                autonomy_default=autonomy_default,
            )
        except KeyError:
            return _err("not_found", code="not_found")
        except FileNotFoundError as exc:
            known = _known_run_ids(_runs_dir())
            return _err(
                str(exc),
                hint=(("run_id must name a run that exists. On disk right now: "
                       + ", ".join(known) + ".") if known else
                      "No runs exist on disk. Start one: p = dag_plan(brief='...'); "
                      "dag_run(graph_json=p['graph_json'])."),
                known_run_ids=known,
            )
        except Exception as exc:
            return _err(
                f"dag_approve failed: {exc}",
                hint=(f"dag_approve only resumes a run that is PAUSED at a gate. Call "
                      f"dag_status(run_id='{run_id}') first; if the run already finished "
                      "there is nothing to approve, and if it never gated, re-run it with "
                      "autonomy_default='approve'."),
            )

        _remember_result(result)
        interrupted = result.get("interrupted", [])
        out: dict[str, Any] = {
            "ok": True,
            "run_id": result["run_id"],
            "status": result.get("status", "unknown"),
            "nodes": result.get("nodes", {}),
            "checkpoint_db": result.get("checkpoint_db", ""),
        }
        if interrupted:
            out["interrupted"] = interrupted
            out["hint"] = "Another gate was reached. Call dag_approve again or dag_abort to cancel."
            out["next_actions"] = [
                f"dag_approve(run_id='{run_id}')",
                f"dag_abort(run_id='{run_id}')",
            ]
        else:
            out["hint"] = f"Run resumed and finished with status={result.get('status', 'unknown')!r}."
        if notes:
            out["interpreted"] = _interpreted_notes(notes)
        return _dumps(out)

    @mcp.tool()
    @model_selectors("run_id")
    def dag_abort(
        run_id: Optional[str] = None,
        graph_json: Any = None,
        autonomy_default: str = "approve",
        **kwargs: _VAR_KW,
    ) -> str:
        """Cancel a gate-paused DAG run by injecting decision='reject'.

        If the run is currently suspended at a gate, this resumes it with
        decision='reject', which causes the gate node to mark its node status
        as 'paused' and terminate the run.

        Args:
            run_id:            UUID4 of the paused run to cancel.  Omit it to abort the
                               most recent run started in this session — the reuse is
                               echoed as `interpreted`.  Also accepts run / id / run_uuid.
            graph_json:        The original graph document as an object or JSON string
                               (or omit to use the sidecar).  Also accepts graph / plan /
                               json / graph_dict.
            autonomy_default:  Must match the autonomy_default used in dag_run.
                               Also accepts autonomy / autonomy_level.

        Returns:
            {ok, run_id, status, nodes, checkpoint_db}
            status will be 'paused' or 'failed' after abort.
            Plus {interpreted} when any input was rewritten.
            On failure: {ok: false, reason, hint}.
        """
        kwargs.pop("kwargs", None)   # FastMCP var-kw placeholder
        try:
            from services.dag import executor
        except ImportError as exc:
            return _err(f"DAG executor unavailable: {exc}")

        run_id, graph_json, autonomy_default, notes, arg_error = _prep_resume(
            "dag_abort", run_id, graph_json, autonomy_default, kwargs)
        if arg_error is not None:
            return arg_error

        try:
            result = executor.resume(
                run_id,
                graph_json=graph_json,
                decision="reject",
                autonomy_default=autonomy_default,
            )
        except KeyError:
            return _err("not_found", code="not_found")
        except FileNotFoundError as exc:
            known = _known_run_ids(_runs_dir())
            return _err(
                str(exc),
                hint=(("run_id must name a run that exists. On disk right now: "
                       + ", ".join(known) + ".") if known else
                      "No runs exist on disk, so there is nothing to abort."),
                known_run_ids=known,
            )
        except Exception as exc:
            return _err(
                f"dag_abort failed: {exc}",
                hint=(f"dag_abort only rejects a run that is PAUSED at a gate. Call "
                      f"dag_status(run_id='{run_id}') first — a finished or never-gated "
                      "run cannot be aborted."),
            )

        _remember_result(result)
        out = {
            "ok": True,
            "run_id": result["run_id"],
            "status": result.get("status", "unknown"),
            "nodes": result.get("nodes", {}),
            "checkpoint_db": result.get("checkpoint_db", ""),
            "hint": "Run aborted (decision=reject injected at gate).",
            "next_actions": [
                "Inspect nodes for the gate that was rejected, then re-run with a corrected graph.",
            ],
        }
        if notes:
            out["interpreted"] = _interpreted_notes(notes)
        return _dumps(out)


def register_dag_replan_tools(mcp) -> None:
    """Register the dag_replan MCP tool onto *mcp*.

    Appended per the server.py append-only contract.
    """

    @mcp.tool()
    @model_selectors("run_id")
    def dag_replan(
        run_id: Optional[str] = None,
        splice_nodes: Optional[list] = None,
        splice_edges: Optional[list] = None,
        after_node_id: Optional[str] = None,
        decision: str = "approve",
        autonomy_default: str = "approve",
        **kwargs: _VAR_KW,
    ) -> str:
        """Splice new nodes into a running DAG run and resume execution.

        Validates the splice (every spliced node must have end_condition + a back-edge
        with retry_budget), merges the new nodes into the persisted graph sidecar,
        then resumes the run via executor.resume().

        Answered gate questions are NOT re-asked: the interaction ledger is stored in the
        SqliteSaver checkpoint and replayed automatically on resume.

        Args:
            run_id:          UUID4 of the paused (or mid-run) DAG run to replan.
                             Omit it to replan the most recent run of this session
                             (echoed as `interpreted`).  Also accepts run / id.
            splice_nodes:    List of aicad.dag/1 node dicts to insert.
                             Each MUST have: id, type, name, inputs, on_fail, autonomy,
                             AND a non-empty end_condition.
                             Also accepts nodes / new_nodes / node_list; a single node
                             dict is wrapped into a one-element list for you.
            splice_edges:    List of aicad.dag/1 edge dicts.
                             MUST include at least one back-edge (edge with 'retry_budget')
                             for EACH spliced node. May also include forward edges.
                             Also accepts edges / new_edges / edge_list; a single edge
                             dict is wrapped for you.
            after_node_id:   Existing node id after which the splice is inserted.
                             Also accepts after / after_node / insert_after / parent.
            decision:        Gate decision for resume — 'approve' (default) | 'reject'.
            autonomy_default: Run-level autonomy floor for the resumed run.
                             Also accepts autonomy / autonomy_level.

        Returns:
            {ok, run_id, status, nodes, checkpoint_db, spliced_node_ids}
            Plus {interpreted} when any input was rewritten.
            On validation failure: {ok: false, reason, hint}.
        """
        kwargs.pop("kwargs", None)   # FastMCP var-kw placeholder
        if in_cell():
            try:
                run_id = _cell_control_run(run_id, kwargs)
            except KeyError:
                return _err("not_found")
        try:
            from services.dag.replan import dag_replan as _replan, ReplannError
            from services.dag.graph_loader import GraphValidationError
            from services.dag import executor
        except ImportError as exc:
            return _err(f"dag_replan unavailable: {exc}",
                        hint="Ensure services/dag is on PYTHONPATH.")

        notes: list[str] = []
        if kwargs:
            if splice_nodes is None:
                alt, spelling = _pop_alias(kwargs, "splice_nodes", "nodes", "new_nodes",
                                           "node_list", "node")
                if alt is not None:
                    splice_nodes = alt
                    if _norm_key(spelling) != "splicenodes":
                        notes.append(f"{spelling}= read as splice_nodes=")
            if splice_edges is None:
                alt, spelling = _pop_alias(kwargs, "splice_edges", "edges", "new_edges",
                                           "edge_list", "edge")
                if alt is not None:
                    splice_edges = alt
                    if _norm_key(spelling) != "spliceedges":
                        notes.append(f"{spelling}= read as splice_edges=")
            if after_node_id is None:
                alt, spelling = _pop_alias(kwargs, "after_node_id", "after_node", "after",
                                           "insert_after", "parent", "parent_node_id")
                if isinstance(alt, str) and alt.strip():
                    after_node_id = alt.strip()
                    if _norm_key(spelling) != "afternodeid":
                        notes.append(f"{spelling}= read as after_node_id=")
            aut_alt, aut_spelling = _pop_alias(kwargs, "autonomy_default", "autonomy",
                                               "autonomy_level", "default_autonomy")
            if isinstance(aut_alt, str) and aut_alt.strip():
                if autonomy_default == "approve":
                    autonomy_default = aut_alt.strip()
                    notes.append(f"{aut_spelling}= read as autonomy_default=")
                else:
                    notes.append(f"ignored {aut_spelling}={aut_alt!r} — autonomy_default="
                                 f"{autonomy_default!r} was set explicitly")

        if isinstance(splice_nodes, dict):
            splice_nodes = [splice_nodes]
            notes.append("splice_nodes: wrapped the single node dict into a one-element list")
        if isinstance(splice_edges, dict):
            splice_edges = [splice_edges]
            notes.append("splice_edges: wrapped the single edge dict into a one-element list")
        if splice_edges is None:
            splice_edges = []

        if isinstance(run_id, str) and run_id.strip():
            run_id = run_id.strip()
        else:
            run_id, id_error = _resolve_run_id(kwargs, "dag_replan", notes)
            if id_error:
                return _err(id_error, known_run_ids=_known_run_ids(_runs_dir()))

        try:
            run_id = validate_run_id(run_id)
        except ValueError as exc:
            return _err(str(exc), hint="Use the run_id exactly as returned by dag_run.")

        missing = [name for name, val in (("splice_nodes", splice_nodes),
                                          ("after_node_id", after_node_id))
                   if not val]
        if missing:
            return _err(
                f"dag_replan is missing {', '.join(missing)} — a splice needs BOTH the "
                "new nodes and the existing node they go after.",
                hint=(
                    "Full shape:\n"
                    "  dag_replan(run_id='<uuid from dag_run>',\n"
                    "             after_node_id='<id of an EXISTING node in the graph>',\n"
                    "             splice_nodes=[{'id': 'refit', 'type': 'act', "
                    "'name': 'Refit bore', 'inputs': {}, 'on_fail': 'pause', "
                    "'autonomy': 'approve', 'end_condition': 'Bore fits the shaft.'}],\n"
                    "             splice_edges=[{'from': 'refit', 'to': 'refit', "
                    "'retry_budget': 2}])\n"
                    "Every spliced node needs a non-empty end_condition, and splice_edges "
                    "needs one back-edge with retry_budget per spliced node. "
                    f"dag_status(run_id='{run_id}') lists the node ids you can splice after."
                ),
            )

        # Step 1: validate + merge splice
        try:
            merged_graph = _replan(
                run_id=run_id,
                splice_nodes=splice_nodes,
                splice_edges=splice_edges,
                after_node_id=after_node_id,
            )
        except KeyError:
            return _err("not_found", code="not_found")
        except FileNotFoundError as exc:
            return _err(str(exc),
                        hint="Verify run_id matches an existing run (from dag_run).")
        except ReplannError as exc:
            return _err(
                f"Splice validation failed: {exc}",
                hint=(
                    "Every spliced node must have a non-empty 'end_condition' and "
                    "at least one back-edge with 'retry_budget' in splice_edges."
                ),
            )
        except GraphValidationError as exc:
            return _err(
                f"Merged graph failed aicad.dag/1 validation: {exc}",
                hint="Fix the spliced nodes/edges to conform to the schema.",
            )

        # Step 2: resume with the merged graph
        try:
            result = executor.resume(
                run_id,
                graph_json=merged_graph,
                decision=decision,
                autonomy_default=autonomy_default,
            )
        except KeyError:
            return _err("not_found", code="not_found")
        except FileNotFoundError as exc:
            return _err(str(exc),
                        hint="Checkpoint DB missing — the run may have already completed.")
        except Exception as exc:
            return _err(f"Resume after replan failed: {exc}",
                        hint="Check that the run is currently paused at a gate.")

        _remember_result(result)
        spliced_ids = [n["id"] for n in splice_nodes]
        interrupted = result.get("interrupted", [])
        out: dict[str, Any] = {
            "ok": True,
            "run_id": result["run_id"],
            "status": result.get("status", "unknown"),
            "nodes": result.get("nodes", {}),
            "checkpoint_db": result.get("checkpoint_db", ""),
            "spliced_node_ids": spliced_ids,
        }
        if interrupted:
            out["interrupted"] = interrupted
            out["hint"] = (
                "Replan applied and run is paused at a gate. "
                "Call dag_approve to continue."
            )
            out["next_actions"] = [
                f"dag_approve(run_id='{run_id}')",
                f"dag_abort(run_id='{run_id}')",
            ]
        else:
            out["hint"] = (
                f"Replan applied. Run resumed and finished with "
                f"status={result.get('status', 'unknown')!r}."
            )
            out["next_actions"] = [
                f"dag_status(run_id='{run_id}')",
            ]
        if notes:
            out["interpreted"] = _interpreted_notes(notes)
        return _dumps(out)
