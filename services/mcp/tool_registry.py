"""MCP tool registry — tags every tool with {family, risk} and enforces a
per-context allow-list at DISPATCH.

Contract: ``contracts/providers.md`` §6. This module is the ONE enforcement point;
the individual ``*_tools.py`` modules are never edited. ``server.py`` wraps each
``register_*_tools(mcp)`` call site with :func:`view`, which:

  1. records every ``@mcp.tool()`` registration under its family + a derived risk class
     into a process-wide :func:`catalog`, and
  2. wraps each tool function in ONE shared guard so that, at call time, a tool not in
     the active allow-list returns a contract-shaped ``{"ok": False, "reason", "hint"}``
     (never a traceback, never a silent success) instead of running.

Default policy = everything allowed (no behavior change vs. today). The DAG node contract
(T-A1 ``dag_schema``) passes explicit per-node ``tools:`` allow-lists that bind here later.
"""
from __future__ import annotations

import inspect
import logging
import os
from functools import wraps
from services.agent.request_context import current_context, in_cell, owner_key

_LOG = logging.getLogger(__name__)

# Risk classes (contract §6). Order = increasing sensitivity.
RISK_READ = "read"
RISK_WRITE = "write"
RISK_SPEND = "spend"
RISK_EXTERNAL = "external"
RISK_MOTION = "motion"
RISK_SAFETY = "safety"
RISKS = (RISK_READ, RISK_WRITE, RISK_SPEND, RISK_EXTERNAL, RISK_MOTION, RISK_SAFETY)

# Name-pattern risk classifier. Checked most-sensitive first so `export_*`/`*_post`
# outrank a generic write verb. A tool that matches nothing defaults to WRITE
# (fail-safe: treat unknown as mutating, never as free read).
_EXTERNAL_PREFIXES = ("export_", "submit_", "import_", "request_")
_EXTERNAL_SUFFIXES = ("_post", "_export")
_READ_PREFIXES = ("describe_", "query_", "measure_", "list_", "get_", "check_",
                  "render_", "inspect_", "review_", "compare_", "draft_", "fit_",
                  "wall_", "contact_")
_READ_SUFFIXES = ("_status",)
_READ_EXACT = {"hover", "cad_guide", "intake_status", "ecad_status", "cam_job_tree",
              # cad_skill reads one SKILL.md out of the skills catalog — same
              # zero-risk knowledge class as cad_guide, no document is touched.
              "cad_skill",
              "cam_materials", "cam_op_types", "cam_tool_types", "cam_post_dialects",
              "list_parts", "list_configurations", "list_world_bodies",
              "list_standard_parts",
              # Design ledger (T-W1): ledger_get reads the ledger; the name starts with
              # "ledger_" not "get_", so the prefix classifier misses it — explicit entry needed.
              # T-DP11: ledger_events returns raw events for the GUI transparency stream.
              "ledger_get", "ledger_events",
              # Typed manufacturing planning and cited-reference reads.
              "mfg_plan_get", "mfg_plan_check",
              "machining_ref_search", "machining_ref_get", "machining_ref_search_pdf",
              # Workspace (T-W16): status/events are read-only poll tools.
              "workspace_status", "workspace_doc_events",
              # ECAD symbol discovery is a pure library query — no document is touched. It
              # starts with "ecad_" so no prefix rule catches it. (ecad_netlist / ecad_bom /
              # ecad_plot stay unclassified -> RISK_WRITE: they can write artifacts to disk.)
              "ecad_symbols",
              # Office read tools (T-office): office_extract is the frozen worker-RPC-shared
              # extraction contract; the rest are read-only per-format structured reads. None
              # match the get_/describe_/etc prefix classifier (all start with "office_"), so
              # they need explicit entries. office_edit_* is left unclassified -> RISK_WRITE.
              "office_extract", "office_read_pptx", "office_read_xlsx", "office_read_csv",
              "office_read_pdf", "office_read_docx", "office_read_doc",
              # General-purpose filesystem read tools (fs_tools.py, family "files"): none
              # match a read prefix, so classify explicitly. fs_write/fs_edit/run_shell
              # stay unclassified -> RISK_WRITE.
              "fs_read", "fs_list", "fs_glob", "fs_grep",
              # Observation-only CAD file/job tools whose names match no read prefix:
              # without these they defaulted to WRITE, which also made the progress
              # watchdog count a file listing or a job wait as modeling progress.
              "browse_models", "read_native_file", "wait_job",
              # Topology capability discovery is a pure catalog query; run_topology_optimization
              # writes study/jobs/artifacts and stays unclassified -> RISK_WRITE.
              "topology_capabilities",
              # Manufacturing observations. Their mfg_* namespace deliberately
              # carries no lexical authority; read access is exact-membership.
              "mfg_device_list", "mfg_device_describe", "mfg_device_capability",
              "mfg_telemetry_snapshot", "mfg_gate_report", "mfg_run_list",
              "mfg_run_describe", "mfg_journal_query", "mfg_approval_inspect",
              # Research tools (research_tools.py): read-only lookups, not mutators —
              # no "research_" prefix in _READ_PREFIXES so classify explicitly.
              "research_web_search", "research_fetch_page",
              # Deterministic printability / G-code analysis (print_tools.py, dfm_check):
              # they measure a part or a file and touch no document.
              "dfm_check", "optimize_print_orientation", "verify_gcode",
              # Printer-media inventory hashes the files on a mounted stick and changes
              # nothing. Its twin publish_print_media writes/retires files on the media
              # and stays unclassified -> RISK_WRITE.
              "print_media_inventory"}

# Explicit overrides for names the classifier would mislabel. name -> risk.
_RISK_OVERRIDES: dict[str, str] = {
    # nothing metered/spend today; add here when a paid-compute tool lands.
    # submit_bug_report writes one LOCAL markdown file (Documents/Chudware/
    # Bug-Reports/) — the `submit_` prefix would auto-classify EXTERNAL
    # (network egress), which offline/local-only policies would refuse.
    "submit_bug_report": RISK_WRITE,
    # box_sync uploads project files to Box.com — real network egress. The name
    # matches no external prefix/suffix, so the WRITE default would let
    # offline/local-only policies run it. box_status stays read via "_status".
    "box_sync": RISK_EXTERNAL,
    # browser_profiles is a mixed list/refresh/delete surface. Keep the whole
    # tool behind write policy even though action="list" alone is read-only.
    "browser_profiles": RISK_WRITE,
    # Every successful animation writes a film and poster; it is never observation-only.
    "render_animation": RISK_WRITE,
}

# Motion and safety classifications are explicit membership only. A newly added
# manufacturing tool therefore retains the fail-safe WRITE default until a
# human deliberately adds it here; no name pattern can mint physical authority.
_MOTION_TOOLS = frozenset({
    "request_mfg_run_start",
    "request_mfg_hold",
    "request_mfg_abort",
})
_SAFETY_TOOLS = frozenset()  # Deliberately empty: models get no safety-class tool.


def classify_risk(name: str) -> str:
    """Derive the risk class for a tool *name* (contract §6)."""
    if name in _SAFETY_TOOLS:
        return RISK_SAFETY
    if name in _MOTION_TOOLS:
        return RISK_MOTION
    if name in _RISK_OVERRIDES:
        return _RISK_OVERRIDES[name]
    if name in _READ_EXACT:
        return RISK_READ
    if name.startswith(_EXTERNAL_PREFIXES) or name.endswith(_EXTERNAL_SUFFIXES):
        return RISK_EXTERNAL
    if name.startswith(_READ_PREFIXES) or name.endswith(_READ_SUFFIXES):
        return RISK_READ
    return RISK_WRITE


def model_selectors(*names: str):
    """Declare parameters that select model resources, not harness context.

    Apply below ``@mcp.tool()`` (or to a mission handler). Omitted selectors
    retain the callable's own defaults/required-argument behavior; explicit
    model values still pass through. No wrapper is introduced, and the shared
    registry guard preserves this metadata through ``functools.wraps``.
    """
    def declare(fn):
        fn._cad_model_selectors = frozenset(names)
        return fn
    return declare


def context_doc_unless(*names: str):
    """Declare arguments that replace the run-context document.

    Apply below ``@mcp.tool()``. If the caller gives one of these arguments, the harness
    does not inject the run-context ``doc_id``. No wrapper is introduced, and the shared
    registry guard preserves this metadata through ``functools.wraps``.
    """
    def declare(fn):
        fn._cad_context_doc_unless = frozenset(names)
        return fn
    return declare


# --------------------------------------------------------------------------- catalog
# Process-wide tag catalog: tool name -> {"family": str, "risk": str}.
_CATALOG: dict[str, dict] = {}


def catalog() -> dict[str, dict]:
    """The live {name: {family, risk}} tag catalog."""
    return _CATALOG


# --- warm/clear semantics -------------------------------------------------
# ``_WARM_BASELINE`` is the set of catalog entries a FULL ``services.mcp.server``
# import contributes, captured by :func:`mark_warm_baseline` from server.py's module
# body. It is a *memory of content*, never an "already warmed" latch: it survives
# :func:`clear_catalog` precisely so a later :func:`warm_catalog` can rebuild the
# catalog without re-importing (a re-import is a no-op once the module is in
# ``sys.modules``, which is what used to make warming permanently dead).
_WARM_BASELINE: dict[str, dict] | None = None
# True once the baseline has been applied to the CURRENT catalog generation.
# ``clear_catalog`` starts a new generation and resets this.
_WARM_APPLIED = False
# The import is attempted at most once per process; a failed import (no ``mcp`` SDK)
# is deterministic and must not be retried on every family resolution.
_WARM_IMPORT_TRIED = False
# The exception that stopped the baseline import, if it did not complete. A partial
# import registers every family DECLARED BEFORE the failing one and none after, so the
# catalog looks plausible (415 of 487 tools) while ~70 real tools read as nonexistent.
# Callers that must distinguish "no tools" from "some tools" ask for this.
_WARM_IMPORT_ERROR: BaseException | None = None


class _NullMCP:
    """Throwaway stand-in for FastMCP when warming lazily-registered families
    outside a server process (``register_*_tools`` only needs ``.tool()``)."""

    def tool(self, *args, **kwargs):
        def deco(fn):
            return fn
        return deco


def mark_warm_baseline() -> None:
    """Record the current catalog as the canonical "fully warmed" contents.

    Called once from ``services/mcp/server.py``'s module body, after every
    ``register_*_tools(view(mcp, ...))`` call site has run. This decouples
    :func:`warm_catalog` from the import side effect: whoever imports server.py
    first (a test module, the MCP entry point, ``warm_catalog`` itself) hands the
    registry a replayable snapshot, so warming still works for every later caller
    even after a :func:`clear_catalog`.
    """
    global _WARM_BASELINE, _WARM_APPLIED
    _WARM_BASELINE = {name: dict(meta) for name, meta in _CATALOG.items()}
    _WARM_APPLIED = True


def warm_catalog() -> None:
    """Ensure the catalog holds every family a full server import registers.

    Semantics: *reset-to-warm*. Idempotent, never raises, and — unlike the old
    one-shot version — genuinely re-registers after :func:`clear_catalog`.

    ``_CATALOG`` is normally populated as an import side-effect of
    ``services/mcp/server.py``. The live GUI worker process never imports server.py,
    so without this, ``family:X`` resolution (roles.py ``concrete_tools``,
    tool_categories._expand_token) sees a near-empty catalog. Callers that resolve
    families from the catalog should call this first.

    Two failure modes this deliberately does NOT have any more:
      * it no longer latches on "have I run once" — it latches on "is the baseline
        present in the CURRENT catalog generation", so a clear/warm pair restores
        the full catalog (previously the families only server.py registers —
        ascii2d, office, power_budget, heatsink, ecad_thermal — were unrecoverable);
      * it no longer depends on the import *side effect*, so a module that imported
        server.py earlier for its own reasons cannot turn warming into a no-op.

    Existing entries are never overwritten: a caller that deliberately retagged a
    tool keeps its tag.
    """
    global _WARM_APPLIED, _WARM_IMPORT_TRIED, _WARM_IMPORT_ERROR
    if _WARM_APPLIED:
        return
    if _WARM_BASELINE is None and not _WARM_IMPORT_TRIED:
        _WARM_IMPORT_TRIED = True
        try:
            # side-effect: registers every family AND calls mark_warm_baseline().
            import services.mcp.server  # noqa: F401
        except Exception as exc:  # noqa: BLE001 — best-effort; a heavy/failed import must never break role loading
            _WARM_IMPORT_ERROR = exc
    if _WARM_BASELINE:
        for name, meta in _WARM_BASELINE.items():
            _CATALOG.setdefault(name, dict(meta))
    else:
        # server.py unavailable (no `mcp` SDK in this interpreter). The org family is
        # pure-Python and role cards reference it, so register it directly rather than
        # leaving family:org unresolvable.
        try:
            from services.mcp.org_tools import register_org_tools

            register_org_tools(view(_NullMCP(), "org"))
        except Exception:  # noqa: BLE001 — best-effort, same contract as above
            pass
    _WARM_APPLIED = True


def warm_import_error() -> BaseException | None:
    """Why the baseline ``services.mcp.server`` import did not complete, if it did not.

    ``None`` after a clean warm (or before any warm attempt). Not-``None`` means the
    catalog is a PREFIX of the real tool surface: trust it for "this tool exists", never
    for "this tool does not".
    """
    return _WARM_IMPORT_ERROR


def clear_catalog() -> None:
    """Reset to a genuine CLEAN ROOM: only the always-on meta-tools remain.

    Semantics: *clean room*, not reset-to-warm. Callers that build a controlled
    catalog (``services/agent/test_roles.py::_build_catalog``,
    ``test_registration_catalog``) get exactly what they register afterwards and
    nothing else — no implicit replay of the server baseline.

    It also disarms :func:`warm_catalog`'s applied-flag, so a caller that wants the
    full catalog back says so explicitly by calling ``warm_catalog()``; that now
    restores everything instead of being a permanent no-op.
    """
    global _WARM_APPLIED
    _CATALOG.clear()
    _WARM_APPLIED = False
    ensure_meta_tools()


def record(name: str, family: str, risk: str | None = None) -> dict:
    entry = {"family": family, "risk": risk or classify_risk(name)}
    _CATALOG[name] = entry
    return entry

_META_TOOLS = (
    ("enable_tools", "workspace", RISK_READ),
    ("list_tool_categories", "workspace", RISK_READ),
    ("design_recipe", "workspace", RISK_READ),
)
for _meta_name, _meta_fam, _meta_risk in _META_TOOLS:
    record(_meta_name, _meta_fam, _meta_risk)


def ensure_meta_tools() -> None:
    """Idempotently re-record enable_tools / list_tool_categories (tests)."""
    for name, fam, risk in _META_TOOLS:
        record(name, fam, risk)


# --------------------------------------------------------------------------- policy
# Active allow-list policy. ``None`` for either field means "fall back to env"; an
# allow-list of ``None`` (after env) means EVERYTHING is allowed (default).
_ACTIVE_ALLOW: set[str] | None = None
_ACTIVE_DENY_RISK: set[str] | None = None
_POLICY_SET = False


def set_policy(allow=None, deny_risk=None) -> None:
    """Set the active allow-list policy explicitly (DAG node / tests).

    *allow*: iterable of tool names and/or ``"family:<name>"`` tokens, or ``None`` = allow
    all. *deny_risk*: iterable of risk-class names to deny wholesale, or ``None`` = deny none.
    """
    global _ACTIVE_ALLOW, _ACTIVE_DENY_RISK, _POLICY_SET
    _ACTIVE_ALLOW = set(allow) if allow is not None else None
    _ACTIVE_DENY_RISK = set(deny_risk) if deny_risk is not None else None
    _POLICY_SET = True


def reset_policy() -> None:
    """Drop the explicit policy and fall back to environment (tests)."""
    global _ACTIVE_ALLOW, _ACTIVE_DENY_RISK, _POLICY_SET
    _ACTIVE_ALLOW = _ACTIVE_DENY_RISK = None
    _POLICY_SET = False


def _env_allow() -> set[str] | None:
    raw = os.environ.get("CHUDWARE_TOOLS_ALLOW", "").strip()
    if not raw:
        return None
    return {t.strip() for t in raw.split(",") if t.strip()}


def _env_deny_risk() -> set[str] | None:
    raw = os.environ.get("CHUDWARE_TOOLS_DENY_RISK", "").strip()
    if not raw:
        return None
    return {t.strip().lower() for t in raw.split(",") if t.strip()}


def _current_allow() -> set[str] | None:
    if _POLICY_SET and _ACTIVE_ALLOW is not None:
        return _ACTIVE_ALLOW
    if _POLICY_SET and _ACTIVE_ALLOW is None:
        return None
    return _env_allow()


def _current_deny_risk() -> set[str] | None:
    if _POLICY_SET and _ACTIVE_DENY_RISK is not None:
        return _ACTIVE_DENY_RISK
    if _POLICY_SET and _ACTIVE_DENY_RISK is None:
        return None
    return _env_deny_risk()


def is_risk_denied(risk: str) -> bool:
    """Whether the active explicit or environment policy denies a risk class."""
    denied = _current_deny_risk()
    return denied is not None and risk in denied


def is_allowed(name: str) -> bool:
    """True if *name* may be dispatched under the active policy (default: all allowed)."""
    return deny_reason(name) is None


# Marker shared by deny_reason and refusal: the policy itself could not be evaluated.
_POLICY_FAILURE = "tool policy could not be evaluated"


def _policy_failure(name: str, source: str, exc: BaseException) -> str:
    """Fail-closed reason for a settings/family policy that raised while evaluating."""
    _LOG.error("%s for %r (%s): %s: %s; refusing the call",
               _POLICY_FAILURE, name, source, type(exc).__name__, exc)
    return (f"tool {name!r} refused: {_POLICY_FAILURE} "
            f"({source}: {type(exc).__name__}: {exc})")


def deny_reason(name: str) -> str | None:
    """None if allowed; else a human reason string for the refusal.

    A Settings or family policy that raises refuses the call (fail closed): a broken
    config read must never re-enable a tool the user switched off.
    """
    if in_cell():
        owner_key()
        if current_context().get("right") == "viewer" and classify_risk(name) != RISK_READ:
            return "forbidden_right"
    # Meta-tools are always permitted (request more surface / list packs / recipes).
    if name in ("enable_tools", "list_tool_categories", "design_recipe"):
        return None
    # Per-tool enabled flag (Settings → AI → Tools options).
    try:
        from services.agent.config import get_tool_options  # lazy
        tool_enabled = get_tool_options(None, name).get("enabled", True)
    except Exception as exc:  # noqa: BLE001 — fail closed; the reason names the error
        return _policy_failure(name, "per-tool settings", exc)
    if not tool_enabled:
        return f"tool '{name}' disabled in Settings → AI → Tools"
    # Config agent.tool_families — disabled families blocked unless env/policy allow
    # list is active (CHUDWARE_TOOLS_ALLOW still wins via _current_allow below).
    fam = (_CATALOG.get(name) or {}).get("family")
    if fam:
        try:
            from services.agent.config import is_tool_family_enabled  # lazy: avoid import cycles
            family_enabled = is_tool_family_enabled(str(fam))
        except Exception as exc:  # noqa: BLE001 — fail closed; the reason names the error
            return _policy_failure(name, "tool-family settings", exc)
        if not family_enabled:
            # If an explicit allow-list is active, family filter still applies unless
            # the tool/family token is in the allow set (checked later). When allow is
            # None (default open), family disable is enough to deny.
            allow = _current_allow()
            if allow is None or (name not in allow and f"family:{fam}" not in allow):
                return (f"tool {name!r} family {fam!r} is disabled in "
                        f"config agent.tool_families")
    if name in _SAFETY_TOOLS:
        return f"tool {name!r} is safety-class: no model dispatch path exists"
    if name in _MOTION_TOOLS:
        motion_allow = _current_allow()
        if motion_allow is None or name not in motion_allow:
            return (f"tool {name!r} is motion-class and requires explicit membership "
                    f"(CHUDWARE_TOOLS_ALLOW / DAG tools list); default-open does not apply")
    risk = (_CATALOG.get(name) or {}).get("risk") or classify_risk(name)
    deny_risk = _current_deny_risk()
    if deny_risk and risk in deny_risk:
        return (f"tool {name!r} is risk-class {risk!r}, which is denied by the active "
                f"policy (CHUDWARE_TOOLS_DENY_RISK)")
    allow = _current_allow()
    if allow is None:
        return None  # default: everything allowed
    family = (_CATALOG.get(name) or {}).get("family")
    if name in allow or (family and f"family:{family}" in allow):
        return None
    return (f"tool {name!r} is not in the active allow-list "
            f"(set CHUDWARE_TOOLS_ALLOW or the DAG node tools: list to permit it)")


def _family_of(name: str) -> str:
    return str((_CATALOG.get(name) or {}).get("family") or "")


def refusal(name: str) -> dict:
    """The contract-shaped refusal payload for a disallowed dispatch (contract §6.1).

    Always machine-readable: ``ok``/``reason``/``family``/``hint``. When the block came
    from the per-family Settings toggle the hint names the family and the exact place to
    turn it back on, because that is the only refusal a *user* (not a DAG node) causes.
    """
    reason = deny_reason(name) or f"tool {name!r} is not permitted"
    fam = _family_of(name)
    if _POLICY_FAILURE in reason:
        hint = ("The tool policy (Settings -> AI -> Tools / config.json agent.tool_options, "
                "agent.tool_families) raised while being evaluated, so the call was refused "
                "instead of running unchecked. Report the error in `reason`; retrying will "
                "not help until that configuration error is fixed.")
    elif "agent.tool_families" in reason and fam:
        hint = (f"Tool family {fam!r} is turned OFF. Re-enable it in the app under "
                f"Settings -> AI -> Tools (or set agent.tool_families.{fam} = true in "
                f"config.json), then retry the same call. No other tool in family "
                f"{fam!r} will run until then — do not retry it in a loop.")
    elif "Settings -> AI -> Tools" in reason or "Settings → AI → Tools" in reason:
        hint = (f"Tool {name!r} is switched off per-tool. Re-enable it in the app under "
                f"Settings -> AI -> Tools, then retry the same call.")
    else:
        hint = ("This tool is gated by the per-context allow-list. Permit it via the DAG "
                "node's tools: list, or CHUDWARE_TOOLS_ALLOW / CHUDWARE_TOOLS_DENY_RISK.")
    out = {"ok": False, "reason": reason, "hint": hint}
    if fam:
        out["family"] = fam
    return out


# --- refusal SHAPING -------------------------------------------------------------------
# FastMCP builds each tool's output schema from its return annotation and validates the
# return value against it.  365 of the registered tools declare ``-> str``, so handing
# back the refusal *dict* raised ``pydantic ValidationError`` -> ``ToolError``: the model
# saw an opaque schema crash ("Input should be a valid string") instead of "that family is
# disabled".  The guard therefore renders the refusal into the shape the tool PROMISED,
# decided once at registration time (zero per-call cost).
_STR_ANNOTATIONS = frozenset({"str", "builtins.str", "typing.Text", "Text"})
_DICT_ANNOTATIONS = frozenset({"dict", "builtins.dict", "Any", "typing.Any", "object"})
_LIST_ANNOTATIONS = frozenset({"list", "builtins.list", "tuple", "builtins.tuple"})


def _annotation_token(fn) -> str:
    """The declared return annotation as a bare token.

    Tool modules use ``from __future__ import annotations``, so the annotation is usually
    already a *string*; when it is a real object we take its ``__name__``.  Subscripted
    generics (``dict[str, Any]``, ``Optional[str]``) reduce to their head token.
    """
    ann = getattr(fn, "__annotations__", {}).get("return", None)
    if ann is None:
        return ""
    text = ann if isinstance(ann, str) else (getattr(ann, "__name__", None) or str(ann))
    text = text.strip()
    for cut in ("[", "|"):
        if cut in text:
            text = text.split(cut, 1)[0].strip()
    return text


def _refusal_shaper(fn):
    """Return ``shape(payload) -> value`` conforming to *fn*'s declared output schema.

    * declared ``str`` (or nothing / ``Any`` / ``dict``) — JSON text / the dict itself;
      both are machine-readable and both validate.
    * declared ``list`` — a one-element list holding the JSON text.
    * anything else (``Image``, ``int``, a model class) cannot carry a refusal at all, so
      the guard raises instead. FastMCP renders a raised error as a normal tool error with
      our message, which is still actionable — never a pydantic schema traceback.
    """
    token = _annotation_token(fn)
    if token in _STR_ANNOTATIONS:
        return lambda payload: _json_dumps(payload)
    if token in _LIST_ANNOTATIONS:
        return lambda payload: [_json_dumps(payload)]
    if token == "" or token in _DICT_ANNOTATIONS:
        return lambda payload: payload
    return _RaiseShaper()


def _json_dumps(payload: dict) -> str:
    import json  # stdlib, deferred: refusals are the cold path
    return json.dumps(payload, ensure_ascii=False)


class _RaiseShaper:
    """Shaper for tools whose declared return type cannot hold a refusal payload."""

    def __call__(self, payload: dict):
        raise _tool_error(str(payload.get("reason") or "tool refused")
                          + " " + str(payload.get("hint") or ""))


def _tool_error(message: str) -> Exception:
    try:
        from mcp.server.fastmcp.exceptions import ToolError  # deferred: optional dep
        return ToolError(message)
    except Exception:  # noqa: BLE001 — mcp package absent (unit tests / DAG-only runs)
        return RuntimeError(message)


# --------------------------------------------------------------------------- guard + view
def _workspace_lease_refusal(doc_id: str, name: str) -> dict | None:
    """Check write-lease for a mutating doc_id-bearing tool call.

    Returns None to proceed, or a contract-shaped refusal dict if another
    agent holds the write lease — or if the lease check itself raised: an
    unverifiable lease refuses the mutation (fail closed) rather than letting
    it run unchecked.  Import is deferred to avoid circular imports
    (workspace.py imports from interaction.py, not from tool_registry.py).

    Read tools (RISK_READ) are never checked — this is the hot path for reads
    and the check is gated on risk class first so there is zero overhead for
    read-only calls.
    """
    if classify_risk(name) == RISK_READ:
        return None  # read tools never need a lease check
    try:
        from services.agent.workspace import check_write_lease  # deferred import
        return check_write_lease(doc_id, name)  # None or refusal dict
    except Exception as exc:  # noqa: BLE001 — refuse in-shape; never crash dispatch
        _LOG.error("write-lease check failed for %r on doc %r: %s: %s; refusing the call",
                   name, doc_id, type(exc).__name__, exc)
        return {
            "ok": False,
            "reason": (f"doc {doc_id!r} write-lease check failed for tool {name!r} "
                       f"({type(exc).__name__}: {exc}); the mutation was refused "
                       f"rather than run unchecked."),
            "hint": ("Call workspace_status(run_id=...) to inspect the lease registry. "
                     "This call cannot run until the write-lease check succeeds."),
        }


def _guard(fn, name):
    """Wrap *fn* so a disallowed dispatch returns :func:`refusal` instead of running.

    ONE shared guard for every tool — this is the single dispatch chokepoint. The wrapper
    preserves *fn*'s signature (via functools.wraps' ``__wrapped__``) so FastMCP still
    introspects the real parameters, and matches its sync/async-ness.

    Every refusal leaving this wrapper — allow-list, disabled family, per-tool toggle, or
    workspace write-lease — is rendered by :func:`_refusal_shaper` into the shape *fn*
    declared it returns, so FastMCP's output-schema validation can never turn a refusal
    into a ``ToolError: 1 validation error`` crash (see _refusal_shaper).

    T-W16 extension: mutating tools that carry a ``doc_id`` keyword argument are also
    checked against the active workspace lease registry.  Read tools are never checked
    (classify_risk fast-path).  If no workspace session is active the check is a no-op.
    """
    shape = _refusal_shaper(fn)

    if inspect.iscoroutinefunction(fn):
        @wraps(fn)
        async def aguard(*args, **kwargs):
            if not is_allowed(name):
                return shape(refusal(name))
            doc_id = kwargs.get("doc_id")
            if doc_id:
                denial = _workspace_lease_refusal(doc_id, name)
                if denial is not None:
                    return shape(denial)
            return await fn(*args, **kwargs)
        aguard._refusal_shaper = shape  # type: ignore[attr-defined]  # tests/introspection
        return aguard

    @wraps(fn)
    def guard(*args, **kwargs):
        if not is_allowed(name):
            return shape(refusal(name))
        doc_id = kwargs.get("doc_id")
        if doc_id:
            denial = _workspace_lease_refusal(doc_id, name)
            if denial is not None:
                return shape(denial)
        return fn(*args, **kwargs)
    guard._refusal_shaper = shape  # type: ignore[attr-defined]  # tests/introspection
    return guard


class _FamilyView:
    """A thin proxy over a FastMCP-like object that stamps registrations with *family*.

    Delegates every attribute to the wrapped ``mcp`` EXCEPT ``tool``: that is intercepted so
    each registered function is tagged into the catalog and wrapped in the dispatch guard.
    """

    def __init__(self, mcp, family: str):
        self._mcp = mcp
        self._family = family

    def tool(self, *t_args, **t_kwargs):
        inner = self._mcp.tool(*t_args, **t_kwargs)
        family = self._family

        def deco(fn):
            name = t_kwargs.get("name") or getattr(fn, "__name__", "?")
            record(name, family)
            guarded = _guard(fn, name)
            # A description given only as a registration kwarg must also travel on the
            # callable: on-demand host-tool loads read inspect.getdoc(fn), and Chudcode
            # rejects the WHOLE set_host_tools call when one tool's description is empty
            # (a prompt that auto-loaded the inspect category died before any model call).
            if t_kwargs.get("description") and not inspect.getdoc(guarded):
                guarded.__doc__ = t_kwargs["description"]
            return inner(guarded)
        return deco

    def __getattr__(self, item):
        return getattr(self._mcp, item)


def view(mcp, family: str) -> _FamilyView:
    """Wrap *mcp* so ``register_*_tools(view(mcp, family))`` tags + guards its tools."""
    return _FamilyView(mcp, family)
