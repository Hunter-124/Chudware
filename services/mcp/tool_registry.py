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
(``dag_schema``) passes explicit per-node ``tools:`` allow-lists that bind here later.
"""
from __future__ import annotations

import contextlib
import inspect
import logging
import os
from contextvars import ContextVar
from functools import wraps
from typing import NamedTuple

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
              # Design ledger: ledger_get reads the ledger; the name starts with
              # "ledger_" not "get_", so the prefix classifier misses it — explicit entry needed.
              # ledger_events returns raw events for the GUI transparency stream.
              "ledger_get", "ledger_events",
              # Typed manufacturing planning and cited-reference reads.
              "mfg_plan_get", "mfg_plan_check",
              "machining_ref_search", "machining_ref_get", "machining_ref_search_pdf",
              # Workspace: status/events are read-only poll tools.
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
              "browse_models", "read_native_file", "wait_job", "scad_inspect",
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
              "dfm_check", "dfm_reinforce", "optimize_print_orientation", "verify_gcode",
              # Printer-media inventory hashes the files on a mounted stick and changes
              # nothing. Its twin publish_print_media writes/retires files on the media
              # and stays unclassified -> RISK_WRITE.
              "print_media_inventory",
              # Read-only analyses; conditional writes are evaluated by the dispatch guard.
              "interface_check", "stackup_analyze", "verify_battery", "design_review",
              "assembly_dof", "fastener_check", "tool_access_check", "disassembly_check",
              "mate_fit_check", "bom_consistency_check",
              "measure", "recommend_fit", "cam_audit", "piping_spec_check",
              "ecad_run_drc", "ecad_run_erc", "ecad_run_dfm", "ecad_run_sicheck",
              "gear_check", "bearing_life", "find_stress_risers", "fea_probe",
              "eval_curvature", "eval_geometry_analysis", "doc_face_edges",
              "harness_get", "harness_check", "harness_report",
              "harness_electrical_check", "harness_bus_check", "harness_cable_model",
              "harness_to_bus_links"}

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
    "part_library_sync": RISK_EXTERNAL,
    "part_acquire": RISK_EXTERNAL,
    # browser_profiles is a mixed list/refresh/delete surface. Keep the whole
    # tool behind write policy even though action="list" alone is read-only.
    "browser_profiles": RISK_WRITE,
    # Every successful animation writes a film and poster; it is never observation-only.
    "render_animation": RISK_WRITE,
    # inspect_native_files is named like a read but writes its summary to output_path.
    "inspect_native_files": RISK_WRITE,
}


# Arguments that turn an otherwise read-classified call into a write. When one is supplied
# (not None / empty) the guard treats THAT call as RISK_WRITE for the viewer-right check,
# the deny-risk policy and the workspace write lease.
_WRITES_WHEN: dict[str, frozenset[str]] = {
    # save_path writes the PNG into the project; display_quality persists on the document.
    "render_part": frozenset({"save_path", "display_quality"}),
    "render_views": frozenset({"save_path"}),
    "contact_sheet": frozenset({"save_path"}),
    "render_study_setup": frozenset({"save_path"}),
    # The verdict is written back into the requirements doc for each req_id.
    "design_review": frozenset({"req_ids"}),
    # With a doc_id the check is saved into the network's piping_network study.
    "piping_spec_check": frozenset({"doc_id"}),
    # Explicit checks can invoke solvers that persist studies or modify assemblies.
    "verify_battery": frozenset({"checks"}),
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


def result_contract(text: str):
    """Declare concise result guidance on the producer, below ``@mcp.tool()``."""
    def declare(fn):
        fn._cad_result_contract = text
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
# The explicit policy is held in a ContextVar and bound per run: every concurrent role
# agent runs in its own context, so one agent's allow-list (or its enable_tools widening)
# can never overwrite another's. A context with no explicit policy falls back to the
# environment (CHUDWARE_TOOLS_ALLOW / CHUDWARE_TOOLS_DENY_RISK); an allow-list of ``None``
# after that means EVERYTHING is allowed (default).
class _Policy(NamedTuple):
    allow: frozenset[str] | None
    deny_risk: frozenset[str] | None


_POLICY: ContextVar[_Policy | None] = ContextVar("chudware_tool_policy", default=None)


def set_policy(allow=None, deny_risk=None) -> None:
    """Bind an explicit allow-list policy to the current run's context (DAG node / tests).

    *allow*: iterable of tool names and/or ``"family:<name>"`` tokens, or ``None`` = allow
    all. *deny_risk*: iterable of risk-class names to deny wholesale, or ``None`` = deny none.
    The explicit policy replaces the environment's for this context only.
    """
    _POLICY.set(_Policy(
        frozenset(allow) if allow is not None else None,
        frozenset(deny_risk) if deny_risk is not None else None,
    ))


def reset_policy() -> None:
    """Drop the current context's explicit policy and fall back to environment (tests)."""
    _POLICY.set(None)


def extend_allow(names) -> None:
    """Union *names* into the current run's allow-list (``enable_tools``).

    A run with no allow-list is unrestricted, so there is nothing to widen. The widened
    list is bound to this context only; other runs keep theirs.
    """
    current = _current_allow()
    if current is not None:
        set_policy(allow=current | frozenset(names), deny_risk=_current_deny_risk())


def bind_run_policy():
    """Give the calling run a private copy of the current policy; return the release token.

    ``set_policy`` / ``extend_allow`` calls made until :func:`release_run_policy` are
    discarded at release, so a turn that narrows or widens its tools cannot leak that into
    the next turn on the same thread.
    """
    return _POLICY.set(_POLICY.get())


def release_run_policy(token) -> None:
    """Drop everything the run bound since :func:`bind_run_policy` returned *token*."""
    _POLICY.reset(token)


@contextlib.contextmanager
def policy_scope():
    """:func:`bind_run_policy` / :func:`release_run_policy` as a ``with`` block."""
    token = bind_run_policy()
    try:
        yield
    finally:
        release_run_policy(token)


def _env_allow() -> frozenset[str] | None:
    raw = os.environ.get("CHUDWARE_TOOLS_ALLOW", "").strip()
    if not raw:
        return None
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


def _env_deny_risk() -> frozenset[str] | None:
    raw = os.environ.get("CHUDWARE_TOOLS_DENY_RISK", "").strip()
    if not raw:
        return None
    return frozenset(t.strip().lower() for t in raw.split(",") if t.strip())


def _current_allow() -> frozenset[str] | None:
    policy = _POLICY.get()
    return policy.allow if policy is not None else _env_allow()


def _current_deny_risk() -> frozenset[str] | None:
    policy = _POLICY.get()
    return policy.deny_risk if policy is not None else _env_deny_risk()


def is_risk_denied(risk: str) -> bool:
    """Whether the active explicit or environment policy denies a risk class."""
    denied = _current_deny_risk()
    return denied is not None and risk in denied


def is_allowed(name: str) -> bool:
    """True if *name* may be dispatched under the active policy (default: all allowed)."""
    return deny_reason(name) is None


# Marker shared by deny_reason and refusal: the policy itself could not be evaluated.
_POLICY_FAILURE = "tool policy could not be evaluated"
_META_TOOL_NAMES = ("enable_tools", "list_tool_categories", "design_recipe")


def _policy_failure(name: str, source: str, exc: BaseException) -> str:
    """Fail-closed reason for a settings/family policy that raised while evaluating."""
    _LOG.error("%s for %r (%s): %s: %s; refusing the call",
               _POLICY_FAILURE, name, source, type(exc).__name__, exc)
    return (f"tool {name!r} refused: {_POLICY_FAILURE} "
            f"({source}: {type(exc).__name__}: {exc})")


def _risk_of(name: str) -> str:
    return (_CATALOG.get(name) or {}).get("risk") or classify_risk(name)


def _denial(name: str, risk: str | None = None) -> tuple[str, str] | None:
    """``(code, reason)`` when *name* may not be dispatched, else None.

    *risk* is this call's risk class and defaults to the catalog's; the guard passes WRITE
    for a call that supplies a writing argument (see _WRITES_WHEN). The catalog entry, the
    risk, the policy and the config are each read once per dispatch.

    A Settings or family policy that raises refuses the call (fail closed): a broken
    config read must never re-enable a tool the user switched off.
    """
    entry = _CATALOG.get(name) or {}
    risk = risk or entry.get("risk") or classify_risk(name)
    fam = entry.get("family")
    if in_cell():
        owner_key()
        if current_context().get("right") == "viewer" and risk != RISK_READ:
            return "forbidden_right", "forbidden_right"
    # Meta-tools are always permitted (request more surface / list packs / recipes).
    if name in _META_TOOL_NAMES:
        return None
    # Per-tool enabled flag (Settings → AI → Tools options).
    try:
        from services.agent import config  # lazy: avoid import cycles
        cfg = config.load_config()  # one config read, shared by the tool and family lookups
        tool_enabled = config.get_tool_options(cfg, name).get("enabled", True)
    except Exception as exc:  # noqa: BLE001 — fail closed; the reason names the error
        return "policy_unevaluable", _policy_failure(name, "per-tool settings", exc)
    if not tool_enabled:
        return "tool_disabled", f"tool '{name}' disabled in Settings → AI → Tools"
    allow = _current_allow()
    # Config agent.tool_families — disabled families blocked unless env/policy allow
    # list is active (CHUDWARE_TOOLS_ALLOW still wins via the allow-list below).
    if fam:
        try:
            family_enabled = config.is_tool_family_enabled(str(fam), cfg)
        except Exception as exc:  # noqa: BLE001 — fail closed; the reason names the error
            return "policy_unevaluable", _policy_failure(name, "tool-family settings", exc)
        # If an explicit allow-list is active, the family filter still applies unless the
        # tool/family token is in the allow set. With no allow-list (default open), a
        # disabled family is enough to deny.
        if not family_enabled and (
                allow is None or (name not in allow and f"family:{fam}" not in allow)):
            return "family_disabled", (f"tool {name!r} family {fam!r} is disabled in "
                                       f"config agent.tool_families")
    if name in _SAFETY_TOOLS:
        return "safety_class", f"tool {name!r} is safety-class: no model dispatch path exists"
    if name in _MOTION_TOOLS and (allow is None or name not in allow):
        return "motion_class", (f"tool {name!r} is motion-class and requires explicit membership "
                                f"(CHUDWARE_TOOLS_ALLOW / DAG tools list); default-open does not apply")
    deny_risk = _current_deny_risk()
    if deny_risk and risk in deny_risk:
        return "risk_denied", (f"tool {name!r} is risk-class {risk!r}, which is denied by the "
                               f"active policy (CHUDWARE_TOOLS_DENY_RISK)")
    if allow is None:
        return None  # default: everything allowed
    if name in allow or (fam and f"family:{fam}" in allow):
        return None
    return "not_allowed", (f"tool {name!r} is not in the active allow-list "
                           f"(set CHUDWARE_TOOLS_ALLOW or the DAG node tools: list to permit it)")


def deny_reason(name: str, risk: str | None = None) -> str | None:
    """None if allowed; else a human reason string for the refusal.

    *risk* overrides the catalog risk class for this one call (see :func:`_denial`).
    """
    denial = _denial(name, risk)
    return denial[1] if denial is not None else None


def _family_of(name: str) -> str:
    return str((_CATALOG.get(name) or {}).get("family") or "")


_REFUSAL_HINTS = {
    "policy_unevaluable": (
        "The tool policy (Settings -> AI -> Tools / config.json agent.tool_options, "
        "agent.tool_families) raised while being evaluated, so the call was refused "
        "instead of running unchecked. Report the error in `error`; retrying will "
        "not help until that configuration error is fixed."),
    "forbidden_right": (
        "This session holds the viewer right, which runs read-class tools only. Ask the "
        "document owner for edit rights; retrying this call will fail identically."),
}


def refusal(name: str, denial: tuple[str, str] | None = None) -> dict:
    """The contract-shaped refusal payload for a disallowed dispatch (contract §6.1).

    Always machine-readable: ``ok``/``error``/``code``/``hint``/``family``, with ``reason``
    kept as an alias of ``error``. When the block came from the per-family Settings toggle
    the hint names the family and the exact place to turn it back on, because that is the
    only refusal a *user* (not a DAG node) causes.

    *denial* is the ``(code, reason)`` verdict the caller already reached for this dispatch;
    omit it to evaluate the policy here.
    """
    code, reason = denial or _denial(name) or ("not_permitted", f"tool {name!r} is not permitted")
    fam = _family_of(name)
    if code == "family_disabled":
        hint = (f"Tool family {fam!r} is turned OFF. Re-enable it in the app under "
                f"Settings -> AI -> Tools (or set agent.tool_families.{fam} = true in "
                f"config.json), then retry the same call. No other tool in family "
                f"{fam!r} will run until then — do not retry it in a loop.")
    elif code == "tool_disabled":
        hint = (f"Tool {name!r} is switched off per-tool. Re-enable it in the app under "
                f"Settings -> AI -> Tools, then retry the same call.")
    elif code in _REFUSAL_HINTS:
        hint = _REFUSAL_HINTS[code]
    else:
        hint = ("This tool is gated by the per-context allow-list. Permit it via the DAG "
                "node's tools: list, or CHUDWARE_TOOLS_ALLOW / CHUDWARE_TOOLS_DENY_RISK.")
    out = {"ok": False, "error": reason, "code": code, "reason": reason, "hint": hint}
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
# Families whose ``doc_id`` names a CAD part, so an omitted one means "the current part" (the
# rule cad_common._doc applies). ECAD/CAM tools resolve an omitted id against their own
# session, which the lease registry does not track.
_CURRENT_DOC_FAMILIES = frozenset({"cad", "sim"})


class _CallShape:
    """What the guard reads from a tool's call arguments, resolved once at registration."""

    __slots__ = ("signature", "doc_index", "has_doc_id", "writers", "name")

    def __init__(self, fn, name: str):
        self.name = name
        try:
            self.signature = inspect.signature(fn)
        except (TypeError, ValueError):
            self.signature = None
        params = list(self.signature.parameters.values()) if self.signature else []
        self.has_doc_id = any(p.name == "doc_id" for p in params)
        self.doc_index = next(
            (i for i, p in enumerate(params)
             if p.name == "doc_id" and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)),
            None)
        self.writers = _WRITES_WHEN.get(name, frozenset())

    def doc_argument(self, args, kwargs):
        """``(raw doc_id, applies)``: the caller's doc_id however it was passed, and whether
        this tool takes a doc_id at all (an omitted one then means the current document)."""
        if "doc_id" in kwargs:
            return kwargs["doc_id"], True
        if self.doc_index is not None and self.doc_index < len(args):
            return args[self.doc_index], True
        return None, self.has_doc_id

    def escalates(self, args, kwargs) -> bool:
        """Whether this call supplies an argument that makes it a write (_WRITES_WHEN)."""
        if not self.writers or self.signature is None:
            return False
        try:
            given = self.signature.bind_partial(*args, **kwargs).arguments
        except TypeError:
            return False  # malformed call: the tool reports that itself
        if self.name == "verify_battery":
            checks = given.get("checks") or ()
            return any(
                isinstance(check, str) and (
                    check.startswith(("run_fea_", "run_cfd_")) or
                    check in {"solve_mates", "cam_simulate", "ecad_simulate"}
                ) for check in checks
            )
        return any(given.get(p) not in (None, "", [], {}) for p in self.writers)


def _lease_doc_id(name: str, raw) -> str | None:
    """The canonical doc_id a call targets — the key the lease registry uses.

    A call may name its document by id, recovery alias or plain name, or omit it for the
    current part. Only cad_common._doc knows all of those, so the lease is checked on its
    answer, never on the raw argument. When nothing resolves (the tool then fails with its
    own teaching error) the raw spelling is still checked, so an id the registry knows but
    the engine no longer lists cannot slip through.
    """
    if raw in (None, "") and _family_of(name) not in _CURRENT_DOC_FAMILIES:
        return None
    from services.mcp import cad_common  # deferred: heavy, needed only under a workspace guard
    try:
        return cad_common._doc(raw or None)
    except cad_common.DocRefError:
        return str(raw) if raw else None


def _workspace_lease_refusal(name: str, raw_doc_id) -> dict | None:
    """Check the write lease for a mutating doc_id-bearing tool call.

    Returns None to proceed, or a contract-shaped refusal dict if another agent holds the
    write lease on the document the call resolves to — or if the lease check itself raised:
    an unverifiable lease refuses the mutation (fail closed) rather than letting it run
    unchecked. Imports are deferred to avoid circular imports (workspace.py imports from
    interaction.py, not from tool_registry.py). Without a bound workspace guard there is
    nothing to enforce, so no document is resolved.
    """
    try:
        from services.agent import workspace  # deferred import
        if not workspace.write_guard_active():
            return None
        doc_id = _lease_doc_id(name, raw_doc_id)
        return workspace.check_write_lease(doc_id, name) if doc_id else None
    except Exception as exc:  # noqa: BLE001 — refuse in-shape; never crash dispatch
        _LOG.error("write-lease check failed for %r on doc %r: %s: %s; refusing the call",
                   name, raw_doc_id, type(exc).__name__, exc)
        reason = (f"doc {raw_doc_id!r} write-lease check failed for tool {name!r} "
                  f"({type(exc).__name__}: {exc}); the mutation was refused "
                  f"rather than run unchecked.")
        return {
            "ok": False,
            "error": reason,
            "code": "write_lease_unverifiable",
            "reason": reason,
            "hint": ("Call workspace_status(run_id=...) to inspect the lease registry. "
                     "This call cannot run until the write-lease check succeeds."),
        }


def _admit(name: str, call: _CallShape, args, kwargs) -> dict | None:
    """The refusal payload for this call, or None to run it.

    The call's risk is resolved once — the catalog's, or WRITE when the call supplies an
    argument that writes — and feeds both the policy verdict and the lease check.
    """
    risk = RISK_WRITE if call.escalates(args, kwargs) else _risk_of(name)
    denial = _denial(name, risk)
    if denial is not None:
        return refusal(name, denial)
    if risk != RISK_READ:
        raw, applies = call.doc_argument(args, kwargs)
        if applies:
            return _workspace_lease_refusal(name, raw)
    return None


def _guard(fn, name):
    """Wrap *fn* so a disallowed dispatch returns :func:`refusal` instead of running.

    ONE shared guard for every tool — this is the single dispatch chokepoint. The wrapper
    preserves *fn*'s signature (via functools.wraps' ``__wrapped__``) so FastMCP still
    introspects the real parameters, and matches its sync/async-ness.

    Every refusal leaving this wrapper — allow-list, disabled family, per-tool toggle, or
    workspace write-lease — is rendered by :func:`_refusal_shaper` into the shape *fn*
    declared it returns, so FastMCP's output-schema validation can never turn a refusal
    into a ``ToolError: 1 validation error`` crash (see _refusal_shaper).

    Workspace extension: a non-read call to a tool that takes a ``doc_id`` is checked against
    the active workspace lease registry on the CANONICAL id the call resolves to (name,
    alias, positional or omitted included). Read calls are never checked. If no workspace
    session is active the check is a no-op.
    """
    shape = _refusal_shaper(fn)
    call = _CallShape(fn, name)

    if inspect.iscoroutinefunction(fn):
        @wraps(fn)
        async def aguard(*args, **kwargs):
            from .core_bridge import agent_tool_scope
            from ._envelopes import tool_reply
            with agent_tool_scope():
                refused = _admit(name, call, args, kwargs)
                if refused is not None:
                    return shape(refused)
                return tool_reply(await fn(*args, **kwargs), name)
        aguard._refusal_shaper = shape  # type: ignore[attr-defined]  # tests/introspection
        return aguard

    @wraps(fn)
    def guard(*args, **kwargs):
        from .core_bridge import agent_tool_scope
        from ._envelopes import tool_reply
        with agent_tool_scope():
            refused = _admit(name, call, args, kwargs)
            if refused is not None:
                return shape(refused)
            return tool_reply(fn(*args, **kwargs), name)
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

    def tool(self, *t_args, risk=None, **t_kwargs):
        if risk is not None and risk not in RISKS:
            raise ValueError(f"unknown tool risk: {risk!r}")
        inner = self._mcp.tool(*t_args, **t_kwargs)
        family = self._family

        def deco(fn):
            name = t_kwargs.get("name") or getattr(fn, "__name__", "?")
            record(name, family, risk)
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
