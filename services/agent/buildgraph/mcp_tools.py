"""mcp_tools.py — MCP tool handlers for buildgraph_plan and buildgraph_status.

Contract: contracts/advisor.md §13 (FROZEN).
Registered by: services/mcp/server.py (register_buildgraph_tools).
MCP freshness: both tools must appear in server instructions (services/mcp/server.py).

Tools:
  buildgraph_plan(plan_json)
      Consume a loop.py BUILD PLAN or aicad.dag/1 mission JSON, run the
      LLM-free planner, validate, and return the aicad.buildgraph/1 graph +
      any advisory findings.

  buildgraph_status(run_id)
      Return node statuses for a previously-planned run.
      Reads from the lightweight sidecar written by buildgraph_plan.

Persistence:  buildgraph_plan writes a <run_id>.buildgraph.json sidecar into
the runs directory (mirrors the dag executor sidecar pattern).
buildgraph_status reads it back.

Returns {ok, ...} following the project _dumps/_err convention from services/dag/mcp_tools.py.
"""
from __future__ import annotations

import ast
import difflib
import json
import os
import platform
import uuid
from pathlib import Path
from typing import Annotated, Any, Optional

from services.mcp.tool_registry import model_selectors

# FastMCP's func_metadata does not special-case VAR_KEYWORD: a bare ``**kwargs``
# becomes a REQUIRED pydantic field literally named "kwargs", so every FastMCP
# call (including the canonical one) fails with "kwargs Field required". An
# annotation carrying a default makes the field optional; the built-in agent path
# (_bind_tool -> fn) still sees var_kw=True and passes everything through.
try:  # pragma: no cover - pydantic ships with mcp
    from pydantic import Field as _PydField

    VarKw = Annotated[Any, _PydField(default=None)]
except Exception:  # noqa: BLE001
    VarKw = Any


# ---------------------------------------------------------------------------
# Helpers — mirrors services/dag/mcp_tools.py
# ---------------------------------------------------------------------------

def _dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2)


def _err(reason: str, hint: str = "", **extra: Any) -> str:
    out: dict[str, Any] = {"ok": False, "reason": reason}
    if hint:
        out["hint"] = hint
    out.update(extra)
    return _dumps(out)


def _runs_dir() -> Path:
    """Platform default runs directory (mirrors executor.default_runs_dir)."""
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
        return Path(appdata) / "Chudware" / "runs"
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(xdg) / "Chudware" / "runs"


def _sidecar_path(run_id: str, runs_dir: Path) -> Path:
    return runs_dir / f"{run_id}.buildgraph.json"


def _write_sidecar(run_id: str, runs_dir: Path, data: dict) -> None:
    runs_dir.mkdir(parents=True, exist_ok=True)
    _sidecar_path(run_id, runs_dir).write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


def _read_sidecar(run_id: str, runs_dir: Path) -> dict | None:
    path = _sidecar_path(run_id, runs_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Input dialect — the shapes models actually send (gym ledger evidence)
# ---------------------------------------------------------------------------

# Spellings of plan_json.
_PLAN_ALIASES = ("plan", "plan_dict", "graph", "json", "build_plan", "buildplan",
                 "plan_spec", "buildgraph", "payload", "data", "args", "arguments",
                 "input", "schema")

# Semantic fields a model writes when it DESCRIBES the build instead of
# pre-serializing our internal schema.  That is a reasonable thing to do, so the
# tool synthesizes the plan and echoes exactly what it built.
_DELIVERABLE_KEYS = ("deliverable", "goal", "objective", "brief", "description",
                     "task", "request", "summary", "title", "name", "product",
                     "part", "assembly", "mission_request", "mission_spec",
                     "mission_brief", "user_request", "prompt", "ask")
_COMPONENT_KEYS = ("components", "parts", "items", "pieces", "subassemblies",
                   "assemblies", "bodies", "modules", "elements")
_SEQUENCE_KEYS = ("sequence", "steps", "order", "operations", "ops", "features",
                  "build_order", "stages", "phases", "plan_steps", "tasks",
                  "actions")
_CONSTRAINT_KEYS = ("constraints", "requirements", "specs", "spec", "notes",
                    "tolerances", "criteria", "limits", "material")
_MISSION_KEYS = ("mission", "mission_name", "label", "run_label")
_RUNS_DIR_KEYS = ("runs_dir", "runs_directory", "out_dir", "output_dir")

PLAN_SHAPES_HINT = (
    "Accepted forms: "
    '(1) BUILD PLAN — plan_json={"name":"bracket","parts":[{"name":"bracket",'
    '"features":["base plate 80x60x10","4x M5 clearance holes","fillet outer corners"]}]}; '
    '(2) mission graph — plan_json={"schema":"aicad.dag/1","nodes":[...]}; '
    "(3) just describe it — buildgraph_plan(deliverable=\"steel bracket\", "
    'components=["bracket","pin"], sequence=["base plate","holes"], '
    'constraints=["6061-T6","+/-0.1mm"]). '
    "A JSON string of form (1) or (2) is parsed for you; a list of step strings "
    "becomes the feature list of one part."
)


def _first(fields: dict, keys: tuple) -> tuple[str, Any]:
    """First present, non-empty (key, value) among *keys*."""
    for key in keys:
        if key in fields:
            val = fields[key]
            if val not in (None, "", [], {}, ()):
                return key, val
    return "", None


def _strip_fence(text: str) -> str:
    """Drop a ```json … ``` wrapper models put around JSON."""
    body = text.strip()
    if not body.startswith("```"):
        return body
    lines = body.splitlines()
    if len(lines) < 2:
        return body
    lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _as_labels(value: Any) -> list[str]:
    """Coerce a step/component collection into a list of human labels."""
    from services.agent.buildgraph.planner import label_of  # noqa: PLC0415

    if value in (None, "", [], {}):
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return [label_of(value)]
    return [label_of(v, f"step_{i + 1}") for i, v in enumerate(value)]


def _plan_from_semantics(fields: dict, mission: str,
                         interpreted: list) -> Optional[dict]:
    """Synthesize a BUILD PLAN from deliverable/components/sequence/constraints.

    Returns None when *fields* carries no build description at all.
    """
    from services.agent.buildgraph.planner import label_of  # noqa: PLC0415

    d_key, deliverable = _first(fields, _DELIVERABLE_KEYS)
    c_key, components = _first(fields, _COMPONENT_KEYS)
    s_key, sequence = _first(fields, _SEQUENCE_KEYS)
    k_key, constraints = _first(fields, _CONSTRAINT_KEYS)
    if not (deliverable or components or sequence):
        return None

    name = label_of(deliverable, mission or "build_plan") if deliverable \
        else (mission or "build_plan")
    steps = _as_labels(sequence)

    parts: list[dict] = []
    if components is not None:
        if isinstance(components, (str, dict)):
            components = [components]
        for i, comp in enumerate(components if isinstance(components, (list, tuple)) else []):
            if isinstance(comp, dict):
                c_name = (comp.get("name") or comp.get("part") or comp.get("label")
                          or comp.get("component") or comp.get("id"))
                p_steps = comp.get("features") or comp.get("steps") \
                    or comp.get("operations") or comp.get("ops")
                parts.append({
                    "name": label_of(c_name, f"part_{i + 1}") if c_name is not None
                            else label_of(comp, f"part_{i + 1}"),
                    "features": _as_labels(p_steps),
                })
            else:
                parts.append({"name": label_of(comp, f"part_{i + 1}"), "features": []})

    if not parts:
        parts = [{"name": name, "features": steps}]
        steps_used = bool(steps)
    elif len(parts) == 1 and not parts[0]["features"] and steps:
        parts[0]["features"] = steps
        steps_used = True
    else:
        steps_used = False

    plan: dict[str, Any] = {"name": name, "parts": parts}
    if constraints not in (None, "", [], {}):
        plan["constraints"] = _as_labels(constraints)
    if steps and not steps_used:
        # Several components AND a global order: which step belongs to which part
        # is genuinely ambiguous, so it is recorded, never guessed.
        plan["sequence"] = steps

    told = []
    if d_key:
        told.append(f"{d_key}= -> plan name {name!r}")
    if c_key:
        told.append(f"{c_key}= -> {len(parts)} part(s)")
    if s_key:
        told.append(f"{s_key}= -> {len(steps)} step(s)"
                    + ("" if steps_used else " kept as plan.sequence (not attributed "
                                             "to a part — say which part each step "
                                             "belongs to for real edges)"))
    if k_key:
        told.append(f"{k_key}= -> plan.constraints")
    interpreted.append("synthesized a BUILD PLAN from your description: "
                       + "; ".join(told))
    return plan


def _is_plan_shaped(plan: dict) -> bool:
    return (
        isinstance(plan.get("parts"), list)
        or plan.get("schema") in ("aicad.dag/1", "aicad.buildgraph/1")
        or plan.get("format") == "aicad.buildgraph/1"
        or isinstance(plan.get("nodes"), list)
    )


def _plan_from_list(items: list, mission: str,
                    interpreted: list) -> tuple[Optional[dict], str]:
    """Promote a bare list to a plan dict when the intent is unambiguous."""
    from services.agent.buildgraph.planner import label_of  # noqa: PLC0415

    if not items:
        return None, "plan_json was an empty list — nothing to plan"
    dicts = [i for i in items if isinstance(i, dict)]
    if len(items) == 1 and dicts and (_is_plan_shaped(dicts[0])
                                      or _first(dicts[0], _DELIVERABLE_KEYS)[0]
                                      or _first(dicts[0], _COMPONENT_KEYS)[0]):
        interpreted.append("unwrapped the single-element list around the plan")
        return dicts[0], ""
    if len(dicts) == len(items):
        if all(i.get("id") and (i.get("requires") or i.get("deps") or i.get("kind"))
               for i in dicts):
            interpreted.append(f"read the list as {len(dicts)} buildgraph node(s)")
            return {"schema": "aicad.buildgraph/1", "nodes": items}, ""
        interpreted.append(f"read the list as {len(dicts)} part(s)")
        return {"parts": items}, ""
    if all(isinstance(i, str) for i in items):
        name = mission or "build_plan"
        interpreted.append(
            f"read the list of {len(items)} strings as the build steps of one part "
            f"named {name!r} (pass components=[...] if they were separate parts)"
        )
        return {"name": name,
                "parts": [{"name": name,
                           "features": [label_of(i) for i in items]}]}, ""
    return None, ("plan_json was a mixed list — cannot tell parts from steps. "
                  "Wrap it: {\"parts\": [...]} or pass components=/sequence=")


def coerce_plan(plan_json: Any, extras: dict, mission: str,
                interpreted: list) -> tuple[Optional[dict], str]:
    """Normalize whatever arrived into a planner-ready plan dict.

    Returns ``(plan, "")`` on success or ``(None, reason)`` when the input is
    genuinely ambiguous — never a silent guess: every rewrite appends a line to
    *interpreted*, which the tool echoes back to the model.
    """
    plan = plan_json
    if plan in (None, "", {}, []):
        for key in _PLAN_ALIASES:
            if key not in extras:
                continue
            candidate = extras[key]
            if candidate in (None, "", {}, []):
                continue
            if key == "schema" and isinstance(candidate, str):
                continue  # schema="aicad.dag/1" names a shape, not a plan
            plan = extras.pop(key)
            interpreted.append(f"{key}= read as plan_json")
            break

    if isinstance(plan, (bytes, bytearray)):
        plan = plan.decode("utf-8", "replace")

    if isinstance(plan, str):
        text = _strip_fence(plan)
        if text[:1] in ("{", "["):
            try:
                plan = json.loads(text)
                interpreted.append("parsed plan_json from a JSON string")
            except ValueError as exc:
                # Models mix quote styles ({"a": 1, 'b': 2}) and write Python
                # tuples; that is still an unambiguous literal, so parse it as
                # one rather than bouncing a recoverable call.
                try:
                    plan = ast.literal_eval(text)
                except (ValueError, SyntaxError, TypeError, MemoryError,
                        RecursionError):
                    return None, (f"plan_json was a JSON string that failed to "
                                  f"parse: {exc}")
                interpreted.append(
                    "parsed plan_json from a Python literal (it was not valid JSON)")
        elif text:
            interpreted.append(
                f"plan_json was prose, read as the deliverable: {text[:80]!r}")
            plan = {"deliverable": text}
        else:
            plan = None

    if isinstance(plan, tuple):
        plan = list(plan)
    if isinstance(plan, list):
        plan, why = _plan_from_list(plan, mission, interpreted)
        if plan is None:
            return None, why

    if isinstance(plan, dict):
        if len(plan) == 1:
            only_key = next(iter(plan))
            if only_key in _PLAN_ALIASES:
                interpreted.append(f"unwrapped the single {only_key!r} key")
                return coerce_plan(plan[only_key], extras, mission, interpreted)
        parts = plan.get("parts")
        if isinstance(parts, dict) and parts:
            # {"parts": {"flanged_bushing": ["hub", "flange", "bore"]}} — a
            # name→contents map instead of the list of part objects.  The
            # mapping is unambiguous, so read it rather than refusing.
            promoted: list[dict] = []
            for pname, body in parts.items():
                if isinstance(body, dict):
                    promoted.append({"name": pname, **body})
                elif isinstance(body, list):
                    promoted.append({"name": pname, "features": body})
                elif body not in (None, ""):
                    promoted.append({"name": pname, "features": [body]})
                else:
                    promoted.append({"name": pname})
            plan = {**plan, "parts": promoted}
            interpreted.append(
                f"read 'parts' as a name→contents map of {len(promoted)} part(s)")
        if _is_plan_shaped(plan):
            if isinstance(plan.get("nodes"), list) and not plan.get("schema") \
                    and plan.get("format") != "aicad.buildgraph/1" \
                    and not isinstance(plan.get("parts"), list):
                plan = {**plan, "schema": "aicad.buildgraph/1"}
                interpreted.append(
                    "plan had bare 'nodes' — tagged it schema='aicad.buildgraph/1'")
            return plan, ""
        synthesized = _plan_from_semantics(plan, mission, interpreted)
        if synthesized is not None:
            return synthesized, ""
        keys = ", ".join(sorted(str(k) for k in plan)[:12]) or "(empty object)"
        return None, (f"plan_json has no 'parts', no schema='aicad.dag/1' and no "
                      f"build description — keys were: {keys}")

    if plan is None:
        synthesized = _plan_from_semantics(extras, mission, interpreted)
        if synthesized is not None:
            for key in (*_DELIVERABLE_KEYS, *_COMPONENT_KEYS, *_SEQUENCE_KEYS,
                        *_CONSTRAINT_KEYS):
                extras.pop(key, None)
            return synthesized, ""
        return None, ("buildgraph_plan needs a plan: no plan_json was passed and "
                      "the other arguments carry no build description")

    return None, (f"plan_json must be an object, a JSON string or a list, got "
                  f"{type(plan).__name__}")


def _known_runs(base: Path, limit: int = 8) -> list[str]:
    """Run ids with a sidecar in *base*, most recent first."""
    try:
        found = [(p.stat().st_mtime, p.name[: -len(".buildgraph.json")])
                 for p in base.glob("*.buildgraph.json")]
    except Exception:  # noqa: BLE001
        return []
    found.sort(key=lambda row: row[0], reverse=True)
    return [name for _, name in found[:limit]]

# ---------------------------------------------------------------------------
# Registration function
# ---------------------------------------------------------------------------

def register_buildgraph_tools(mcp, runs_dir: Optional[Path] = None) -> None:
    """Register buildgraph_plan and buildgraph_status MCP tools onto *mcp*.

    Follows the same register_*_tools(mcp) pattern as services/dag/mcp_tools.py.
    Called from services/mcp/server.py during tool registration.

    Args:
        mcp:       FastMCP / services.mcp instance.
        runs_dir:  Override for tests; defaults to platform default.
    """
    _base_runs_dir = runs_dir  # captured in closures

    @mcp.tool()
    def buildgraph_plan(
        plan_json: Any = None,
        mission: str = "",
        runs_dir_override: Optional[str] = None,
        **kwargs: VarKw,
    ) -> str:
        """Consume a BUILD PLAN, a dag/1 mission, or a plain description of the
        build, and return an aicad.buildgraph/1 graph.

        The planner is LLM-free: it infers prerequisite edges from declarative rules
        (datum→sketch, boss→pierce, parts→mates, pattern→seed, etc.) and emits
        aicad.advisory/1 findings for any ambiguity — it never guesses.

        Args:
            plan_json:
                Accepted forms — every rewrite is echoed back in ``interpreted``:
                  * BUILD PLAN dict: {"name": …, "parts": [{"name": …,
                    "features": ["base plate 80x60x10", "4x M5 holes"]}]}.
                    A feature entry may be a string OR a dict
                    ({"type": "hole", "dia": 5}) — dicts are labelled for you.
                  * aicad.dag/1 mission dict: {"schema": "aicad.dag/1", "nodes": […]}.
                  * aicad.buildgraph/1 dict (nodes with id/requires); bare
                    {"nodes": […]} is tagged for you.
                  * A JSON STRING of any of the above (``` fences tolerated) —
                    parsed for you.
                  * A list: of dicts → parts (or nodes when they carry id+requires);
                    of strings → the build steps of ONE part.
                  * Prose → read as the deliverable (see the semantic form below).
                Spellings accepted for this argument: plan, plan_dict, graph, json,
                build_plan, plan_spec, payload, data, args, input.
            mission:
                Optional human label for the resulting graph.
            runs_dir_override:
                Optional filesystem path to store the buildgraph sidecar.
                Defaults to the platform runs dir (~/.local/share/Chudware/runs/).
            **kwargs:
                SEMANTIC FORM — describe the build instead of pre-serializing a
                plan, and the tool synthesizes the BUILD PLAN and echoes it back
                in ``plan_used``:
                  buildgraph_plan(deliverable="steel bracket",
                                  components=["bracket", "pin"],
                                  sequence=["base plate", "4x M5 holes"],
                                  constraints=["6061-T6", "+/-0.1mm"])
                deliverable | goal | objective | brief | description | task,
                components | items | pieces | subassemblies | modules,
                sequence | steps | order | operations | features | stages,
                constraints | requirements | specs | tolerances | material.

        Returns:
            {ok, run_id, graph, advisories, sidecar_path}
            graph conforms to aicad.buildgraph/1.
            advisories is a list of aicad.advisory/1 items (may be empty = clean plan).
            ``interpreted`` (present only when the input was rewritten) lists every
            rewrite in plain words, and ``plan_used`` is the exact plan planned from.
            On failure: {ok: false, reason, hint}.
        """
        interpreted: list[str] = []
        extras = dict(kwargs)
        # FastMCP materialises the var-kw field itself: it hands back
        # {"kwargs": None} for a plain call, and {"kwargs": {...}} when a client
        # nests the extra arguments (unknown TOP-level args never reach a FastMCP
        # tool at all, so the nested form is the only way to send the semantic
        # fields over FastMCP; the built-in agent path passes them through flat).
        _nested = extras.pop("kwargs", None)
        if isinstance(_nested, dict):
            extras.update(_nested)

        if not mission:
            m_key, m_val = _first(extras, _MISSION_KEYS)
            if m_key and isinstance(m_val, str):
                mission = m_val
                extras.pop(m_key, None)
                interpreted.append(f"{m_key}= read as mission")
        if runs_dir_override is None:
            r_key, r_val = _first(extras, _RUNS_DIR_KEYS)
            if r_key and isinstance(r_val, str):
                runs_dir_override = r_val
                extras.pop(r_key, None)
                interpreted.append(f"{r_key}= read as runs_dir_override")

        plan, why = coerce_plan(plan_json, extras, mission, interpreted)
        if plan is None:
            return _err(why, hint=PLAN_SHAPES_HINT,
                        received={"plan_json": type(plan_json).__name__,
                                  "other_args": sorted(str(k) for k in kwargs
                                                       if k != "kwargs")})
        leftover = sorted(str(k) for k in extras)
        if leftover:
            interpreted.append("ignored unrecognised argument(s): "
                               + ", ".join(leftover))
        try:
            from services.agent.buildgraph.planner import build_graph
            from services.agent.buildgraph.schema import BuildGraphCycleError
        except ImportError as exc:
            return _err(
                f"buildgraph package unavailable: {exc}",
                hint="Ensure services/agent/buildgraph is on PYTHONPATH.",
            )

        try:
            graph, advisories = build_graph(plan, mission=mission)
        except BuildGraphCycleError as exc:
            return _err(
                f"Dependency cycle in build plan: {exc}",
                hint="Remove the circular prerequisite reference from the plan.",
            )
        except Exception as exc:
            return _err(
                f"Build graph planning failed: {exc}",
                hint=PLAN_SHAPES_HINT,
                interpreted=interpreted,
                plan_used=plan,
            )

        run_id = str(uuid.uuid4())
        base = (
            Path(runs_dir_override) if runs_dir_override
            else (_base_runs_dir or _runs_dir())
        )
        sidecar_data = {"run_id": run_id, "graph": graph, "advisories": advisories}
        try:
            _write_sidecar(run_id, base, sidecar_data)
            sidecar_str = str(_sidecar_path(run_id, base))
        except Exception:  # noqa: BLE001
            sidecar_str = ""  # non-fatal

        blocker_count = sum(
            1 for adv in advisories
            for item in adv.get("items", [])
            if item.get("severity") == "blocker"
        )
        out: dict[str, Any] = {
            "ok": True,
            "run_id": run_id,
            "graph": graph,
            "advisories": advisories,
            "sidecar_path": sidecar_str,
            "node_count": len(graph.get("nodes", [])),
            "advisory_count": len(advisories),
            "blocker_count": blocker_count,
            "hint": (
                "Use buildgraph_status(run_id) to inspect node statuses. "
                "Review any advisory findings before executing — blockers indicate "
                "missing prerequisites or cycles."
            ),
            "next_actions": [
                f"buildgraph_status(run_id='{run_id}')",
            ],
        }
        if interpreted:
            # Never a silent rewrite: say what was read and from what, and show the
            # exact plan it was turned into so the model can correct it.
            out["interpreted"] = interpreted
            out["plan_used"] = plan
        return _dumps(out)

    @mcp.tool()
    @model_selectors("run_id")
    def buildgraph_status(
        run_id: str,
        runs_dir_override: Optional[str] = None,
    ) -> str:
        """Return the current node statuses of a buildgraph run.

        Reads the sidecar file written by buildgraph_plan.  Restart-safe.

        Args:
            run_id:            UUID4 returned by buildgraph_plan.
            runs_dir_override: Override the default runs directory.

        Returns:
            {ok, run_id, schema, mission, nodes, advisory_count}
            Each node entry: {id, kind, label, status, requires, why}.
            On unknown run: {ok: false, reason, hint, known_run_ids, did_you_mean}
            — ``known_run_ids`` are the runs that DO exist (most recent first) and
            ``did_you_mean`` is a spelling suggestion only; nothing is auto-resolved.
        """
        base = (
            Path(runs_dir_override) if runs_dir_override
            else (_base_runs_dir or _runs_dir())
        )
        data = _read_sidecar(run_id, base)
        if data is None:
            known = _known_runs(base)
            close = difflib.get_close_matches(str(run_id), known, n=1, cutoff=0.5)
            if known:
                hint = (
                    f"Runs that DO exist in {base} (most recent first): "
                    + ", ".join(repr(k) for k in known) + ". "
                )
                if close:
                    hint += (f"Closest to {run_id!r} is {close[0]!r} — call "
                             f"buildgraph_status(run_id='{close[0]}') if that is the "
                             f"one you meant (not resolved for you). ")
                hint += "A run_id comes from the buildgraph_plan call that created it."
            else:
                hint = (
                    f"No buildgraph runs exist in {base} at all — nothing has been "
                    "planned yet in this session. Call buildgraph_plan(...) first "
                    "and use the run_id it returns."
                )
            return _err(
                f"Build graph run {run_id!r} not found",
                hint=hint,
                known_run_ids=known,
                did_you_mean=(close[0] if close else ""),
            )

        graph = data.get("graph", {})
        advisories = data.get("advisories", [])
        nodes = graph.get("nodes", [])

        node_summaries = [
            {
                "id":       n.get("id"),
                "kind":     n.get("kind"),
                "label":    n.get("label"),
                "status":   n.get("status", "pending"),
                "requires": n.get("requires", []),
                "why":      n.get("why", ""),
            }
            for n in nodes
        ]

        return _dumps({
            "ok": True,
            "run_id": run_id,
            "schema": graph.get("schema", "aicad.buildgraph/1"),
            "mission": graph.get("mission", ""),
            "node_count": len(nodes),
            "nodes": node_summaries,
            "advisory_count": len(advisories),
            "next_actions": [
                f"# Start execution: iterate ready_nodes() from executor",
            ],
        })
