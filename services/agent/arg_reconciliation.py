"""Argument-name reconciliation shared by BOTH tool-dispatch paths.

Two callers reach the same tool functions with different calling conventions:

- the built-in agent (``services/agent/agent_turn.py::_bind_tool`` /
  ``_reconcile_kwargs``) resolves a tool to its raw python callable and calls
  ``fn(**kwargs)`` directly, bypassing FastMCP's ``ToolManager`` entirely;
- external MCP clients (``services/mcp/server.py::_install_arg_teaching``) go
  through ``ToolManager.call_tool``, which validates arguments against a
  pydantic model built from the same function's signature.

Both need to answer the same question for an argument name that is NOT a real
parameter: is it an unambiguous synonym of one (accept it, echo what it was
interpreted as), or a genuine mistake (reject it, teach the real parameter
set)? That decision — the synonym tables, the name-matching heuristics, and
the shape of the teaching payload — used to be implemented twice and had
already drifted (``export_part(format=...)`` was fixed for the agent path and
never ported to the MCP path). This module is the ONE place it lives now;
both dispatch paths import it, so a fix here reaches both by construction.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

# Explicit argument-name synonyms the affix heuristic in `resolve_unknown` can
# never reach ("format" shares no prefix/suffix with "fmt"). Live 2026-07-27
# (claude-opus, flyswatter run): export_part(format="step") was rejected
# "no parameter of that name and no unambiguous synonym" even though `format`
# IS the canonical English name of that parameter. Keyed by the NORMALISED
# supplied name -> normalised target; applied only when exactly one free
# parameter matches the target and its annotation admits the value, so a
# synonym can never outvote a real parameter or force a wrong-typed call.
ARG_SYNONYMS: dict[str, str] = {
    "format": "fmt",
    "fileformat": "fmt",
}

# The spellings of `n_modes` (a COUNT) that the affix heuristic cannot reach. Shared by the
# three solvers that take it; see the run_fea_modal note in TOOL_ARG_SYNONYMS below.
_N_MODES_SPELLINGS: dict[str, str] = {
    "nummodes": "nmodes",
    "nummode": "nmodes",
    "modecount": "nmodes",
    "modescount": "nmodes",
    "numberofmodes": "nmodes",
    "numofmodes": "nmodes",
}

# Synonyms that are only unambiguous for ONE tool. "type" is far too generic
# to map globally, but export_part(fmt, path, doc_id) has no other parameter
# a str `type=` could mean. Every entry below was added only after READING the
# tool's real signature — a synonym whose target is not a real parameter turns
# an honest refusal into a silently wrong call, which is strictly worse.
TOOL_ARG_SYNONYMS: dict[str, dict[str, str]] = {
    "export_part": {"type": "fmt"},
    # run_cfd_flow(velocity, aoa, fluid, cells, n_steps, doc_id, background).
    # Live 2026-07-29 (claude-sonnet-4-5): inlet_velocity_mps=5.0 was refused.
    # `velocity` is the only speed slot, so every inlet/free-stream spelling of
    # it is unambiguous HERE; globally "speed" could mean a feed rate, hence
    # tool-scoped.
    "run_cfd_flow": {
        "inletvelocitymps": "velocity",
        "inletvelocity": "velocity",
        "freestreamvelocity": "velocity",
        "flowvelocity": "velocity",
        "airspeed": "velocity",
        "speed": "velocity",
        "angleofattack": "aoa",
        "medium": "fluid",
        "iterations": "nsteps",
    },
    # ecad_find_part(query, limit[, category]). One free-text slot and one
    # integer slot, so search/count spellings cannot be misrouted.
    "ecad_find_part": {
        "partnumber": "query",
        "mpn": "query",
        "keyword": "query",
        "keywords": "query",
        "searchterm": "query",
        "search": "query",
        "term": "query",
        "q": "query",
        "component": "query",
        "maxresults": "limit",
        "maxhits": "limit",
        "topk": "limit",
        "count": "limit",
    },
    # ecad_new_schematic(name). Live 2026-07-29: title='Decoupling Capacitor
    # Demo' was refused. When `name` is FREE a title IS the name; when the
    # model supplied both, `name` is taken and the redundant label is dropped
    # by drop_descriptive() instead.
    "ecad_new_schematic": {"title": "name"},
    # apply_ascii_edits(kind, edits_json, doc_id, layer, sketch_id, grid_json,
    # index_json, dry_run). Live 2026-07-29: edits=[...] refused by a tool
    # literally named apply_ascii_EDITS. The affix rule already reaches
    # edits_json; what blocked it was the `str` annotation against a decoded
    # list — see bind_value().
    "apply_ascii_edits": {
        "edits": "editsjson",
        "ops": "editsjson",
        "changes": "editsjson",
        "editlist": "editsjson",
    },
    # ecad_simulate(analysis, params, probes, doc_id, backend). Live 2026-07-29:
    # kind='s-param' was refused. `analysis` IS the discriminator `kind` names,
    # and an alias on a discriminator is only safe if a wrong value fails LOUDLY
    # — it does: ecad_sim_tools.py:213 returns "unknown analysis <x>" listing
    # op/dc/tran/ac/four/mc/temp/noise, so kind='s-param' gets told the real
    # vocabulary instead of a parameter list. NOTE this does NOT make that live
    # call work, and must not: ecad_simulate is a SPICE circuit simulator with no
    # S-parameter analysis and no port concept (see the capability-gap note in
    # test_ecad_simulate_still_refuses_the_s_parameter_port_arguments).
    "ecad_simulate": {"kind": "analysis", "analysistype": "analysis",
                      "simtype": "analysis", "solver": "backend"},
    # run_shell(command: str, cwd: str = "", timeout_s: int = SHELL_DEFAULT_TIMEOUT_S)
    # — fs_tools.py:762. Live 2026-07-29 (claude-sonnet-4-5): cmd='ls -la
    # /tmp/sweep/work/' was refused. `cmd` is the universal abbreviation of
    # `command`, and the affix rule cannot reach it ("cmd" is neither a prefix
    # nor a suffix of "command"). There is exactly ONE command slot and the
    # other two parameters are a directory and an int timeout, so no str
    # spelling of "the thing to run" can be misrouted HERE. Tool-scoped because
    # globally `cmd`/`script` could name a subcommand word or a file path.
    # DELIBERATELY EXCLUDED: `shell=`. "shell" names the INTERPRETER
    # (bash/sh/zsh), a different thing from the command line; run_shell has no
    # interpreter slot, so binding shell='bash' would execute `bash` and report
    # success. Same reasoning as QUANTITY_MISMATCH below: a name meaning
    # something else is a refusal, not an alias.
    "run_shell": {
        "cmd": "command",
        "cmdline": "command",
        "commandline": "command",
        "script": "command",
    },
    # run_fea_modal(fixtures, material, n_modes=6, mesh_fineness, doc_id, background) —
    # cad_sim_tools.py:546. Live 2026-07-29 (claude-sonnet-4-5, sim suite):
    # run_fea_modal(num_modes=3) was refused. `num_modes` is the obvious English spelling of
    # `n_modes` and denotes the SAME quantity — a COUNT of modes, no unit and no scale to
    # convert — so this is a spelling alias, not a QUANTITY_MISMATCH.
    # The affix rule in resolve_unknown cannot reach it: "nummodes" is neither a prefix nor a
    # suffix of "nmodes". It DOES already reach `modes` ("nmodes".endswith("modes")) and
    # `n_mode` ("nmodes".startswith("nmode")), so neither is listed — an entry that duplicates
    # the heuristic only hides which rule is doing the work.
    # Tool-scoped for all three solvers that take n_modes; each has exactly ONE mode-count
    # slot, so no spelling of "how many modes" can be misrouted.
    "run_fea_modal": _N_MODES_SPELLINGS,
    # run_fea_frequency_response(..., n_modes=6, ...) — cad_sim_tools.py:657.
    "run_fea_frequency_response": _N_MODES_SPELLINGS,
    # run_fea_buckling(fixtures, load, material, n_modes=4, doc_id) — cad_sim_tools.py:1327.
    "run_fea_buckling": _N_MODES_SPELLINGS,
    # make_drawing(doc_id, views, sections, sheet, hole_callouts, model_items, title,
    # source_doc, template) — cad_drawing_tools.py:407, signature read 2026-07-29 AFTER
    # DrawingArgAliases removed the **kwargs that had made this tool varkw (a varkw tool
    # bypasses this whole layer on both paths, so entries here would have been dead code).
    # Exactly ONE document slot and ONE paper-size slot, so every spelling below is
    # unambiguous HERE; globally "source"/"paper" could mean almost anything, hence
    # tool-scoped. `source_doc`/`template` are now REAL parameters and need no entry.
    # NOT listed because the affix rule in resolve_unknown already reaches them (measured,
    # not assumed — an entry that duplicates the heuristic only hides which rule is doing
    # the work): source_document -> source_doc, sheet_format -> sheet.
    # `template` is deliberately absent: a template value that is NOT a paper size must be
    # refused, and only the tool can inspect the value to tell the two apart.
    # `source` IS listed, and the reason is a trap worth recording: make_drawing carries
    # BOTH `source_doc` AND `source_doc_id` as real parameters (two spellings of the one
    # slot), so "source" affix-matches TWO free params, resolve_unknown sees len(affix)==2
    # and correctly refuses. A working spelling silently became a refusal the moment the
    # second alias was declared. The explicit synonym runs BEFORE the affix step and names
    # doc_id, which is unambiguous, so it repairs the coverage without loosening the rule.
    "make_drawing": {
        "source": "docid",
        "fromdoc": "docid",
        "modeldoc": "docid",
        "partdoc": "docid",
        "sourcepart": "docid",
        "papersize": "sheet",
        "paper": "sheet",
    },
    # export_drawing(fmt, doc_id, path, filename) — cad_drawing_tools.py:627. One
    # output-name slot and one format slot. NOT listed (affix already reaches them):
    # save_path -> path, file -> filename.
    # DELIBERATELY EXCLUDED: `output`. output="dxf" reads as the FORMAT to plenty of
    # models, fmt and path are both str so annotation_admits cannot tell them apart, and
    # binding it to path would silently write a file named "dxf.pdf" — a wrong-slot bind
    # is invisible, which is the same failure class as a wrong-unit alias.
    "export_drawing": {
        "outfile": "path",
        "outputfile": "path",
        "dest": "path",
        "destination": "path",
        "saveas": "path",
        "filetype": "fmt",
        "extension": "fmt",
        "ext": "fmt",
        "type": "fmt",
    },
}

# Argument names that are PURELY a human-readable label: they name the call for
# a reader and can never change what the tool computes. When a model adds one
# to an otherwise-correct call, refusing the whole call is the worse failure —
# `drop_descriptive` ignores it with a visible note instead.
#
# This list is deliberately closed and hand-audited. NEVER add anything that
# could carry semantics (geometry, quantities, units, refs, ids, flags,
# materials, selections) and never derive membership from a heuristic like
# "ends in _name": an ignored VALUE is invisible to the model, a refusal is
# not. Keyed by normalised name (see `norm_arg`).
DESCRIPTIVE_ARGS: frozenset[str] = frozenset({
    "studyname", "title", "label", "caption", "comment", "note", "notes",
    "description", "displayname", "friendlyname",
})

# The DELIBERATE NON-ALIASES. Each key looks like a spelling of the parameter it
# names, but denotes a different QUANTITY — an absolute length where the real
# parameter wants a dimensionless factor, a diameter where it wants a radius.
# Aliasing one of these would run a different computation and report success:
# the single worst outcome this module can produce, and strictly worse than the
# honest refusal. So the resolver hard-refuses them, and `unknown_arg_payload`
# spends the refusal teaching the real parameter, its scale, and an example.
#
# Live 2026-07-29 (claude-sonnet-4-5, general lane): run_fea_static(...,
# mesh_size_mm=<n>) was refused as unknown_argument next to an accepted-params
# list containing `mesh_fineness (optional)` — an invitation to alias it. But
# cad_sim_tools.py:75-76,284-285 defines mesh_fineness as a 0..1 float (preset
# words "coarse"=0.25 / "medium"=0.5 / "fine"=0.75 / "finest"=1.0), i.e. a
# density factor with no millimetre in it. mesh_size_mm=2.0 bound there would
# mesh at fineness 2.0, not at a 2 mm element edge.
#
# Keyed by NORMALISED supplied name -> (target parameter, what the supplied name
# means and why it does not convert, a correct example argument). A note is only
# emitted when *target* is genuinely a parameter of the tool being called, so
# this table can never invent advice about a parameter that is not there.
QUANTITY_MISMATCH: dict[str, tuple[str, str, str]] = {}

_MESH_SIZE_WHY = (
    "an ABSOLUTE element edge length in millimetres, while `mesh_fineness` is a "
    "DIMENSIONLESS 0..1 density factor (0.25=\"coarse\", 0.5=\"medium\", "
    "0.75=\"fine\", 1.0=\"finest\"). There is no mm-to-fineness conversion, so "
    "the number cannot be carried across — choose a fineness instead"
)
for _n in ("meshsizemm", "meshsize", "meshelementsize", "meshelementsizemm",
           "elementsize", "elementsizemm", "meshedgelengthmm", "meshedgelength",
           "meshresolutionmm", "targetelementsizemm"):
    QUANTITY_MISMATCH[_n] = ("mesh_fineness", _MESH_SIZE_WHY, "mesh_fineness=0.75")
del _n


def quantity_mismatch_notes(tool: str, params: Sequence[str],
                            unmapped: Sequence[str]) -> list[dict]:
    """Teaching entries for refused names that are a UNIT/QUANTITY mismatch.

    One entry per name in *unmapped* that is in `QUANTITY_MISMATCH` AND whose
    target really is a parameter of *tool* — never a guess about a parameter the
    tool does not have.
    """
    out: list[dict] = []
    by_norm = {norm_arg(p): p for p in params}
    for name in unmapped:
        entry = QUANTITY_MISMATCH.get(norm_arg(name))
        if not entry:
            continue
        target, why, example = entry
        real = by_norm.get(norm_arg(target))
        if real is None:
            continue
        out.append({
            "supplied": name,
            "parameter": real,
            "note": (f"{name!r} is {why}. {tool} has no size-based meshing knob at "
                     f"all; re-issue with e.g. {tool}(..., {example})."),
        })
    return out


def synonyms_for(tool: str) -> dict[str, str]:
    """The global synonym table plus *tool*'s own overrides, merged."""
    merged = dict(ARG_SYNONYMS)
    merged.update(TOOL_ARG_SYNONYMS.get(tool) or {})
    return merged


def norm_arg(name: str) -> str:
    """Fold an argument name for matching: case/underscore/dash/space are noise."""
    return str(name).lower().replace("_", "").replace("-", "").replace(" ", "")


def annotation_admits(ann: str, value: Any) -> bool:
    """True when *value* is plausibly what annotation *ann* asks for.

    The synonym mapper's safety catch: describe_project(run_id="m1") must NOT be
    rewritten to run=<str> when ``run`` is annotated ``Optional[dict]`` — a
    silently wrong call is worse than an honest teaching error. Ambiguous or
    absent annotations admit everything (we only ever REFUSE on a clear clash).

    *ann* accepts EITHER vocabulary: a lowercased python ``repr(annotation)``
    string (the agent-turn path, e.g. ``"optional[dict]"``) or a JSON-Schema
    type name (the MCP path, e.g. ``"object"``) — both map onto the same five
    kinds below, so one function serves both callers.
    """
    if not ann or value is None:
        return True
    kinds = {
        "dict": ("dict" in ann or "mapping" in ann or "object" in ann),
        "list": ("list" in ann or "sequence" in ann or "tuple" in ann or "array" in ann),
        "str": ("str" in ann or "string" in ann),
        "num": ("int" in ann or "float" in ann or "number" in ann or "integer" in ann),
        "bool": ("bool" in ann or "boolean" in ann),
    }
    hit = [k for k, v in kinds.items() if v]
    if len(hit) != 1:
        return True  # union / unannotated / unrecognised → cannot judge
    kind = hit[0]
    if kind == "dict":
        return isinstance(value, dict)
    if kind == "list":
        return isinstance(value, (list, tuple))
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "str":
        return isinstance(value, str)
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value)
            return True
        except ValueError:
            return False
    return False


def bind_value(target: str, ann: str, value: Any) -> Any:
    """The value that should actually be bound to *target*.

    One case only: a ``*_json`` parameter annotated ``str`` is a JSON DOCUMENT
    slot, and a model that already has the document in structured form sends
    the list/dict itself (live 2026-07-29: ``apply_ascii_edits(edits=[{...}])``).
    Renaming alone is not enough — the agent path would hand a list to a str
    parameter and the MCP path's pydantic model would reject it outright
    ("edits_json: Input should be a valid string"). Serialising here gives both
    paths exactly what the signature declares.

    Everything else is returned untouched: this is a spelling/encoding fix, not
    a type coercion layer.
    """
    if isinstance(value, (list, dict)) and str(target).endswith("_json"):
        a = str(ann or "").lower()
        if ("str" in a or "string" in a) and "dict" not in a and "list" not in a:
            try:
                return json.dumps(value)
            except (TypeError, ValueError):
                return value
    return value


def resolve_unknown(supplied: str, value: Any, params: Sequence[str],
                    ann: Mapping[str, str], used: set,
                    synonyms: Mapping[str, str] | None = None) -> str | None:
    """Map an unknown argument onto a real parameter, or None.

    Only UNAMBIGUOUS mappings are made: an exact match ignoring case and
    underscores, an explicit synonym (``synonyms_for(tool)``) whose target is
    exactly one free parameter, or an affix relation (one name is a
    prefix/suffix of the other, e.g. shaft_diameter -> diameter) that exactly
    ONE free parameter satisfies and whose annotation admits the supplied
    value. *params* is every parameter the tool accepts; *used* is the subset
    already bound by this call (so a synonym can never steal a slot a real
    argument already filled).

    Names in `QUANTITY_MISMATCH` are refused unconditionally unless the tool has
    a parameter of exactly that name: they denote a different quantity from the
    parameter they resemble, so no later rule may bind them."""
    free = [p for p in params if p not in used]
    ns = norm_arg(supplied)
    if not ns:
        return None
    exact = [p for p in free if norm_arg(p) == ns]
    if exact:
        return exact[0] if len(exact) == 1 else None
    if ns in QUANTITY_MISMATCH:
        return None  # different QUANTITY, never a spelling — see the table
    if synonyms:
        want = synonyms.get(ns)
        if want:
            cand = [p for p in free if norm_arg(p) == want]
            if len(cand) == 1 and annotation_admits(
                    ann.get(cand[0], ""), bind_value(cand[0], ann.get(cand[0], ""), value)):
                return cand[0]
    affix = []
    for p in free:
        np = norm_arg(p)
        if min(len(np), len(ns)) < 3:
            continue  # 1–2 char names collide by accident, never by intent
        if ns.startswith(np) or ns.endswith(np) or np.startswith(ns) or np.endswith(ns):
            affix.append(p)
    if len(affix) != 1:
        return None
    target = affix[0]
    at = ann.get(target, "")
    return target if annotation_admits(at, bind_value(target, at, value)) else None


def plausible_targets(supplied: str, params: Sequence[str], used: set,
                      synonyms: Mapping[str, str] | None = None) -> list[str]:
    """Free parameters *supplied* could conceivably have been meant for.

    Deliberately LOOSER than `resolve_unknown`: it ignores the annotation guard
    and the "exactly one candidate" rule, because it answers the opposite
    question. `resolve_unknown` asks "may I bind this?"; this asks "is there any
    chance this belongs somewhere?" — used by `drop_descriptive` to refuse to
    ignore an argument that might have had a home.
    """
    ns = norm_arg(supplied)
    if not ns:
        return []
    want = (synonyms or {}).get(ns)
    out = []
    for p in params:
        if p in used:
            continue  # already filled by the model's own explicit argument
        np = norm_arg(p)
        if np == ns or (want and np == want):
            out.append(p)
        elif min(len(np), len(ns)) >= 3 and (
                ns.startswith(np) or ns.endswith(np)
                or np.startswith(ns) or np.endswith(ns)):
            out.append(p)
    return out


def drop_descriptive(supplied: str, value: Any, params: Sequence[str], used: set,
                     synonyms: Mapping[str, str] | None = None) -> bool:
    """True when *supplied* is a purely-cosmetic label safe to IGNORE.

    Live 2026-07-29 (claude-sonnet-4-5): ``ecad_new_schematic(name='x',
    title='X Demo')`` and ``run_cfd_flow(study_name='air_flow_5mps', ...)``
    were refused outright because the model attached a human label to an
    otherwise-correct call. Refusing a correct call over a helpful label is the
    worse failure — but a SILENT drop is worse still, so every caller must
    surface the drop in its response (see the note-emitting call sites in
    ``agent_turn._reconcile_kwargs`` and ``server._install_arg_teaching``).

    Four conditions, all required — when any is unmet the caller refuses
    exactly as before:

    1. the name is on the closed `DESCRIPTIVE_ARGS` allowlist (never a
       heuristic);
    2. the value is a plain string — a dict/list/number/bool carries structure,
       a quantity or a flag, i.e. semantics, whatever it is called;
    3. no FREE parameter could plausibly have received it (`plausible_targets`)
       — a value routed to the wrong slot, or ignored when a real slot was
       standing empty, is invisible; a refusal is not;
    4. `resolve_unknown` already declined it (the caller's ordering), so this
       can never outvote a real mapping.
    """
    if norm_arg(supplied) not in DESCRIPTIVE_ARGS:
        return False
    if not isinstance(value, str):
        return False
    return not plausible_targets(supplied, params, used, synonyms)


def dropped_note(supplied: str, tool: str) -> str:
    """The one wording both dispatch paths use for an ignored label."""
    return (f"ignored {supplied!r}: {tool} has no such parameter and it is a "
            f"purely descriptive label — the call ran WITHOUT it")


def attach_warnings(result: Any, warnings: Sequence[str]) -> Any:
    """Merge *warnings* into a tool result under a ``warnings`` key.

    Tool results come back as a dict, a JSON object string, or free text
    depending on the tool; a drop must be visible in all three, so text falls
    back to an appended ``[warning]`` line rather than being dropped itself.
    Appends to any ``warnings`` the tool already produced instead of clobbering.
    """
    warnings = list(warnings)
    if not warnings:
        return result

    def _merge(obj: dict) -> dict:
        out = dict(obj)
        existing = out.get("warnings")
        if isinstance(existing, list):
            out["warnings"] = [*existing, *warnings]
        elif isinstance(existing, str):
            out["warnings"] = [existing, *warnings]
        else:
            out["warnings"] = warnings
        return out

    if isinstance(result, dict):
        return _merge(result)
    if isinstance(result, str):
        stripped = result.lstrip()
        if stripped.startswith("{"):
            try:
                obj = json.loads(stripped)
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                return json.dumps(_merge(obj), indent=2, default=str)
        return result + "".join(f"\n[warning] {w}" for w in warnings)
    if isinstance(result, (list, tuple)):
        return [*result, *(f"[warning] {w}" for w in warnings)]
    return [result, *(f"[warning] {w}" for w in warnings)]


# Parameters that are a tool's "everything else" bag: analysis settings, solver
# options and the like live INSIDE them as dict keys, not as top-level
# arguments. Live 2026-07-29: ecad_simulate(freq_start_hz=..., freq_stop_hz=...,
# freq_points=...) was refused with a correct but unhelpful list of five
# parameter names; those settings really do exist, one level down in `params`.
SETTINGS_BAG_PARAMS: tuple[str, ...] = ("params", "options", "opts", "config", "settings")


def unknown_arg_payload(tool: str, params: Sequence[str], required: Sequence[str],
                        supplied: Mapping[str, Any], unmapped: Sequence[str],
                        config_keys: Sequence[str] = ()) -> dict:
    """Teaching error for argument names that are not (and could not be
    unambiguously mapped onto) a parameter of *tool*. Shared payload SHAPE
    used by both dispatch paths — the anti-drift property this module exists
    for: the same bad call produces the same keys, not just "similar" ones.
    """
    good = {k: v for k, v in supplied.items() if k in params}
    corrected = [f"{k}={v!r}" for k, v in good.items()]
    corrected += [f"{p}=<{p}>" for p in params if p in required and p not in good]
    payload = {
        "ok": False,
        "status": "unknown_argument",
        "error": (
            f"{tool}() got unknown argument(s) "
            f"{', '.join(repr(u) for u in unmapped)} — no parameter of that name "
            f"and no unambiguous synonym. The call was NOT made (nothing was "
            f"dropped silently). Re-issue it using only the accepted parameters."
        ),
        "accepted_params": [
            f"{p} (required)" if p in required else f"{p} (optional)" for p in params
        ],
        "unknown_params": list(unmapped),
        "hint": f"{tool}({', '.join(corrected)})",
    }
    config_keys = set(config_keys)
    from_config = [u for u in unmapped if u in config_keys]
    if from_config:
        # Not the model's mistake: a param_default the user configured is not a
        # parameter of this tool. Say so, or the model burns the turn rephrasing.
        payload["from_config"] = from_config
        payload["error"] += (
            f" NOTE: {', '.join(repr(c) for c in from_config)} came from "
            f"agent.tool_options param_defaults (Settings → AI → Tools), NOT "
            f"from your call — you cannot fix it by rephrasing; report it."
        )
    bag = next((p for p in params
                if norm_arg(p) in SETTINGS_BAG_PARAMS and p not in supplied), None)
    if bag:
        # A pointer, NOT a mapping: we do not claim the rejected names belong in
        # there (guessing that would be the silent-wrong-call failure this whole
        # module exists to avoid), only that this tool keeps its per-analysis
        # settings one level down, which is where a model that flattened them
        # should look first.
        payload["settings_param"] = bag
        payload["error"] += (
            f" NOTE: {tool} takes its analysis/solver-specific settings as keys "
            f"inside the `{bag}` dict, not as top-level arguments — read the tool "
            f"description for the keys {bag} accepts before re-issuing."
        )
    mismatch = quantity_mismatch_notes(tool, params, unmapped)
    if mismatch:
        # The refusal is CORRECT here; what was missing is why. Without this the
        # accepted_params list ("mesh_fineness (optional)") reads as an
        # invitation to rename the argument and keep the millimetre value.
        payload["quantity_mismatch"] = mismatch
        payload["error"] += "".join(f" NOTE: {m['note']}" for m in mismatch)
    try:
        from services.agent.tool_hints import hint_for  # noqa: PLC0415
        extra = hint_for(tool)
        if extra:
            payload["tool_hint"] = extra
    except Exception:  # noqa: BLE001
        pass
    return payload
