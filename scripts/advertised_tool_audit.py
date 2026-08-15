#!/usr/bin/env python3
"""advertised_tool_audit — every tool the harness NAMES to the model must be reachable.

The defect class this closes was proven live in run-42508173312214: `mission_start`'s
success hint said "execute with dag_run(graph_json=<graph>)" and `dag_run`'s own note said
an `interrupted` result means dag_approve/dag_abort — while none of the three was in any
CATEGORY_PACKS pack or any role's base loadout. The model read the instruction, could not
comply, and said so verbatim: "The tool note says the graph compiles but does not execute -
I need dag_run to execute it, which isn't in my current loadout." That single instance was
fixed (6e3f2878, `CATEGORY_PACKS["delegate"]`); this gate sweeps the class.

Two independent failure directions, each scanned on the surface where it can be judged
without guessing:

  TIER A — advertised but unreachable.  A mention that IS a registered tool yet appears in
  NO pack and no role loadout. Scanned across EVERY string literal in services/agent/** and
  services/mcp/**, because there is nothing to curate: the name is a real tool and it is
  genuinely out of reach, wherever the sentence lives.

  TIER B — advertised but nonexistent.  A tool-call-shaped mention that resolves to nothing
  the model can call (not a registered tool, not a tool_guidance pseudo-tool, not a
  MissionDispatch handler). Prose legitimately names Python methods, engine RPCs, CMake
  functions and parameter keys too, so this direction is scanned ONLY on the declared
  model-facing advertisement surfaces — ``prompt_note``/``hint``/``note``/``guidance``
  values, the strings that are rendered into the tools block or returned in a tool result —
  and every legitimate non-tool mention on those surfaces is enumerated in
  :data:`NON_TOOL_MENTIONS` with its reason. A gate that cries wolf gets disabled, so the
  allowlist is the price of Tier B existing at all.

Reachability, deliberately GENEROUS (the union of every route a model could take), so a
finding is never an artifact of a narrow definition:

  * ``resolve_categories([pack])`` for each pack in CATEGORY_PACKS, plus META_TOOLS;
  * the copilot-mode floors (shared / agent / subagent defaults);
  * every role card's explicit ``tool:`` allowlist entries;
  * the FULL registry expansion of every ``family:X`` reachable from a role card or from
    config.DEFAULT_ROLE_TOOLS (uncapped — roles.py trims to MAX_TOOLS_PER_ROLE per role,
    but a tool trimmed out of one role's slot is still advertised by another).

Bindability follows the note in CATEGORY_PACKS["delegate"]: the corporate_*/coordinate_*
surface is NOT MCP-registered — those are MissionDispatch bound methods resolved through
MissionDispatch.resolve, a layer agent_turn._bind_tool never consults. Reading them out of
the ``_handlers`` dict literal is what keeps this gate from repeating the false-negative
that once deleted them from the pack.

Usage:
  python scripts/advertised_tool_audit.py     # text report, exit 0 (clean) / 1 (defects)

Exit code: 0 = every advertised tool reachable, 1 = defect found, 2 = parse/invocation
error (which INCLUDES the extractor matching nothing: a gate that can pass by finding zero
candidates verifies nothing).
"""
from __future__ import annotations

import ast
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_ROOTS = (
    os.path.join("services", "agent"),
    os.path.join("services", "mcp"),
)
MISSION_DISPATCH = os.path.join(REPO, "services", "agent", "mission_dispatch.py")

# The registry must actually be populated for a finding to mean anything. warm_catalog()
# is best-effort by contract (it swallows a missing `mcp` SDK), so a near-empty catalog
# would silently turn every mention into a Tier B "defect" AND every Tier A check into a
# no-op. 420 tools are registered today; anything under this floor is a broken invocation.
MIN_CATALOG = 200

# A snake_case identifier — the shape every tool name in this codebase has. The underscore
# is required: it is what separates a tool name from ordinary prose ("access", "batch",
# "coverage" all matched a no-underscore pattern and drowned the signal 15:1).
_NAME = r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+"

# Backticked (`add_features`) or call-shaped (`add_features(`) mention. Tier B uses the
# tight form with no space before "(" — "requires project_id (plus ...)" is a parameter
# being described, not a call.
MENTION_TIGHT = re.compile(rf"`({_NAME})`|\b({_NAME})\(")
# Tier A tolerates the space, since a false positive there is impossible by construction.
MENTION_LOOSE = re.compile(rf"`({_NAME})`|\b({_NAME})\s*\(")
# Prose that advertises a call without punctuation: "prefer plm_rollback", "wait for the
# holder to coordinate_release", "coordinate via coordinate_progress". The captured group
# is the whole comma/slash/and-joined RUN of names following the cue, because the list
# forms are where the defects hide: "those are dfm_check, tolerance_stackup and
# review_drawing" names a nonexistent tool in the second slot only (the real one is
# stackup_analyze), and a single-name pattern reads only the first. Requiring an
# underscore in _NAME is what keeps ordinary prose after a cue ("use the sids it just
# listed") out of the run.
MENTION_CUED = re.compile(
    rf"\b(?:prefer|call|use|run|then|via|try|to|are)\s+"
    rf"({_NAME}(?:\s*(?:,|/|\bor\b|\band\b|\bthen\b)\s*{_NAME})*)",
    re.I,
)
_NAME_RE = re.compile(_NAME)

# Dict keys / kwargs whose string values are MODEL-FACING advertisement: rendered into the
# tools block (prompt_note, via tool_schemas.render_tools_block) or returned inside a tool
# result the model reads next turn (hint/note/guidance/alternatives).
ADVERTISEMENT_KEYS = frozenset({
    "prompt_note", "hint", "hints", "note", "guidance", "next", "try", "suggest", "fix",
})

# Legitimate non-tool mentions on the advertisement surfaces. Each entry is
# name -> (reason, allowed repo-relative files or ("*",) for anywhere). Scoping to a file
# is not pedantry: `doc_checkpoint`/`doc_revert` are safe to NAME only in the one hint that
# also says "those are engine-internal; the tools YOU can call are ...", and were being
# advertised bare in the undo/redo prompt_notes until this gate found them.
NON_TOOL_MENTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "doc_checkpoint": (
        "engine RPC (core/doc/session.h), never MCP-registered; may be named only where "
        "the same hint says it is engine-internal and points at plm_timeline/plm_rollback",
        ("services/mcp/cad_common.py",),
    ),
    "doc_revert": (
        "engine RPC (core/doc/session.h), never MCP-registered; same scoping as "
        "doc_checkpoint",
        ("services/mcp/cad_common.py",),
    ),
    "add_executable": (
        "CMake command — fw_sources appends compilable paths to the project CMakeLists",
        ("services/agent/tool_guidance/firmware.py",),
    ),
    "add_need": (
        "req_update action= value, not a tool (req_update(action='add_need', ...))",
        ("services/agent/tool_guidance/cad.py",),
    ),
    "add_requirement": (
        "req_update action= value, not a tool",
        ("services/agent/tool_guidance/cad.py",),
    ),
    "begin_login": (
        "oauth_pkce module function; that hint is host/Settings-facing (provider sign-in), "
        "not a model tool result",
        ("services/agent/providers/oauth_pkce.py",),
    ),
    "complete_login": (
        "oauth_pkce module function; same host-facing hint as begin_login",
        ("services/agent/providers/oauth_pkce.py",),
    ),
    "bench_categories": (
        "engine.worker RPC method named in a host-facing worker_rpc error hint",
        ("services/agent/worker_rpc.py",),
    ),
    "subscription_accounts": (
        "engine.worker RPC method named in a host-facing provider-login hint",
        ("services/agent/providers/subscription_login.py",),
    ),
    "self_intersection": (
        "response field of check_geometry_health, not a tool",
        ("services/agent/tool_guidance/cad.py",),
    ),
    "volume_mm3": (
        "response field of check_geometry_health, not a tool",
        ("services/agent/tool_guidance/cad.py",),
    ),
    "drill_min": ("ecad DRC rule key", ("*",)),
    "power_in": ("power_budget input key", ("*",)),
    "power_out": ("power_budget response field (paired with power_in)", ("*",)),
    "sim_params": ("simulation parameter dict key", ("*",)),
    "snap_fit": ("dfm_check feature-class value", ("*",)),
    "want_fullwave": ("EM solver selection flag", ("*",)),
}


# --------------------------------------------------------------------------- reachability
def scan_files() -> list[str]:
    """Repo-relative Python sources on the two scanned trees, tests excluded.

    Test modules name tools freely in fixtures and assertions; they are not an
    advertisement surface.
    """
    out: list[str] = []
    for root in SCAN_ROOTS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(REPO, root)):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "tests")]
            for fn in filenames:
                if not fn.endswith(".py") or fn.startswith("test_"):
                    continue
                # Always forward-slash, on every platform. These strings are
                # compared against the hand-written paths in NON_TOOL_MENTIONS
                # and printed in findings, so a backslash here makes every
                # allowlist entry miss on Windows and the gate reports ten
                # phantom unreachable tools while Linux and macOS pass on the
                # identical catalogue. Repo-relative paths are identifiers here,
                # not filesystem paths.
                rel = os.path.relpath(os.path.join(dirpath, fn), REPO)
                out.append(rel.replace(os.sep, "/"))
    return sorted(out)


def dispatch_handlers() -> set[str]:
    """Tool names MissionDispatch.resolve binds (the ``_handlers`` dict literal keys).

    Read statically rather than by constructing a dispatcher: a MissionDispatch needs a
    run id + ledger, and the keys are plain string literals.
    """
    with open(MISSION_DISPATCH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    names: set[str] = set()
    for node in ast.walk(tree):
        # `self._handlers: dict[str, Callable[..., Any]] = { ... }` is an AnnAssign;
        # accept the bare form too so a dropped annotation cannot blind the gate.
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = list(node.targets)
        else:
            continue
        if not any(isinstance(t, ast.Attribute) and t.attr == "_handlers" for t in targets):
            continue
        if isinstance(node.value, ast.Dict):
            for key in node.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    names.add(key.value)
    return names


def mission_dispatch_advertised() -> set[str]:
    """Tools the mission lane ADVERTISES outside CATEGORY_PACKS and the role cards.

    Two routes, both literal-scanned out of mission_dispatch.py:

      * ``MissionDispatch.TOOLS`` — the mission-scoped tool set (adopt_role,
        request_role_change, the parts/scrape verbs);
      * the Settings → AI → Harness preference grants in
        ``_apply_session_preferences`` (``add.add("task_list")``). ``task_list`` is
        granted THERE on purpose — the docstring at that site explains that spending a
        MAX_TOOLS_PER_ROLE seat on it in general_agent.yaml evicted ``plm_checkpoint``
        off the tail of ``family: plm``. Scanning the grant call rather than hard-coding
        the name means the next preference-granted tool is picked up for free.
    """
    with open(MISSION_DISPATCH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "TOOLS" for t in node.targets):
            for elt in getattr(node.value, "elts", ()):
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    names.add(elt.value)
        elif isinstance(node, ast.FunctionDef) and node.name == "_apply_session_preferences":
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr in ("add", "update")
                        and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id == "add"):
                    for arg in sub.args:
                        for lit in ast.walk(arg):
                            if isinstance(lit, ast.Constant) and isinstance(lit.value, str):
                                names.add(lit.value)
    return names


def reachable_tools() -> set[str]:
    """Every tool some lane actually PUTS IN FRONT of the model.

    Reachable means advertised, not merely dispatch-allowed: a tool the registry policy
    would run but no tools block ever names is a tool the model has no way to know about.
    """
    from services.agent import tool_categories as tc
    from services.agent import roles as role_mod
    from services.agent.copilot_mode_tools import (
        AGENT_DEFAULT_TOOLS,
        SHARED_DEFAULT_TOOLS,
        SUBAGENT_DEFAULT_TOOLS,
        SWARM_EXTRA_TOOLS,
    )

    out: set[str] = set(tc.META_TOOLS) | mission_dispatch_advertised()
    # Per pack, not one combined call: CATEGORY_EXPAND_CAP is a per-enable_tools ceiling,
    # and the model enables packs one at a time.
    for pack in tc.CATEGORY_PACKS:
        out |= tc.resolve_categories([pack])
    # SWARM_EXTRA_TOOLS is the one mode list that ADDS rather than restricts (it is
    # unioned onto an open allowlist in swarm mode), so it is a real route; ASK/PLAN/EDIT
    # only intersect a role's tools and can never make a tool reachable.
    out |= (set(SHARED_DEFAULT_TOOLS) | set(AGENT_DEFAULT_TOOLS)
            | set(SUBAGENT_DEFAULT_TOOLS) | set(SWARM_EXTRA_TOOLS))

    # Every role's ACTUAL advertised slot. `concrete_tools` — not the uncapped
    # `family:X` expansion — is what reaches the model: run_linear_role_turn builds
    # `allowed = set(card.concrete_tools) | SUBAGENT_DEFAULT_TOOLS` and feeds exactly
    # that to render_tools_block (mission_dispatch.py:2236, 2286-2291). A family token
    # in a role card widens the DISPATCH policy (roles.apply_tool_policy) but the slot
    # is still trimmed to MAX_TOOLS_PER_ROLE = 14, explicit entries first. That gap is
    # the whole defect: `dag_run` is in `family:mission`, `mission` is in
    # DEFAULT_ROLE_TOOLS["default"], and the model STILL reported "I need dag_run to
    # execute it, which isn't in my current loadout" (run-42508173312214). Counting
    # family expansion as reachable would have called that defect clean.
    for card in role_mod.load_all_roles().values():
        out |= set(card.concrete_tools)
    # Settings → AI → Tools "Send bug reports" floor: config-conditional, but a real
    # route when on (roles.apply_tool_policy and run_linear_role_turn both union it).
    try:
        from services.agent.config import bugreport_floor
        out |= set(bugreport_floor())
    except Exception:  # noqa: BLE001 — optional floor
        pass
    return out


# ------------------------------------------------------------------------------ extraction
def advertisement_strings(path: str) -> list[tuple[str, str, int]]:
    """``(key, text, lineno)`` for every model-facing advertisement string in *path*."""
    with open(os.path.join(REPO, path), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    out: list[tuple[str, str, int]] = []

    def collect(key: str, node: ast.AST, lineno: int) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                out.append((key, sub.value, lineno))

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value in ADVERTISEMENT_KEYS:
                    collect(str(k.value), v, node.lineno)
        elif isinstance(node, ast.keyword) and node.arg in ADVERTISEMENT_KEYS:
            collect(node.arg, node.value, getattr(node.value, "lineno", 0))
    return out


def all_strings(path: str) -> list[tuple[str, int, str | None]]:
    """``(text, lineno, owner)`` for every string literal that could reach a model.

    Two kinds of prose are excluded because they are provably NOT advertisements:

    * the MODULE docstring — internal authoring notes (org_tools.py's header explains
      why the family exists; profile_tools.py's names the tools it registers). Nothing
      renders it into a prompt.
    * *owner* carries the tool name that owns a FUNCTION docstring, so a tool naming
      ITSELF in its own docstring is dropped by the caller. That mention is
      self-referential: if the tool is not advertised, its docstring is not rendered
      either, so it can never be an instruction the model cannot follow.

    A function docstring naming a DIFFERENT tool stays in scope — that is the ordinary
    "then call X" advertisement, and the docstring IS the tool description the model
    reads.
    """
    with open(os.path.join(REPO, path), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    skip: set[int] = set()
    owner_of: dict[int, str] = {}
    mod_doc = ast.get_docstring(tree, clean=False)
    if mod_doc is not None and tree.body:
        first = tree.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            skip.add(id(first.value))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body:
            continue
        first = node.body[0]
        if not (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            continue
        # `@mcp.tool(name="request_raise")` renames the tool; the decorator wins.
        tool_name = node.name
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call):
                for kw in dec.keywords:
                    if (kw.arg == "name" and isinstance(kw.value, ast.Constant)
                            and isinstance(kw.value.value, str)):
                        tool_name = kw.value.value
        owner_of[id(first.value)] = tool_name
    return [
        (n.value, n.lineno, owner_of.get(id(n)))
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and len(n.value) >= 8 and id(n) not in skip
    ]


def mentions(text: str, *, tight: bool) -> set[str]:
    """Every tool-name-shaped mention in *text* (punctuated and cue-run forms)."""
    found: set[str] = set()
    for m in (MENTION_TIGHT if tight else MENTION_LOOSE).finditer(text):
        for grp in m.groups():
            if grp:
                found.add(grp)
    for m in MENTION_CUED.finditer(text):
        found.update(_NAME_RE.findall(m.group(1)))
    return found


# ----------------------------------------------------------------------------------- audit
def main() -> int:
    sys.path.insert(0, REPO)
    os.environ.setdefault("CHUDWARE_ENGINE_DAEMON", "0")
    try:
        from services.mcp import tool_registry as registry
        registry.warm_catalog()
        catalog = dict(registry.catalog() or {})
        from services.agent.tool_guidance import PSEUDO_TOOLS
        reach = reachable_tools()
        handlers = dispatch_handlers()
    except Exception as exc:  # noqa: BLE001 — an unusable import is an ERROR, not a pass
        print(f"advertised_tool_audit: ERROR resolving the tool surface: {exc!r}")
        return 2
    if len(catalog) < MIN_CATALOG:
        print(f"advertised_tool_audit: ERROR tool catalog holds only {len(catalog)} tools "
              f"(expected >= {MIN_CATALOG}) — the registry did not warm, so no finding "
              f"here would be trustworthy")
        return 2
    if not handlers:
        print("advertised_tool_audit: ERROR MissionDispatch._handlers parsed empty — the "
              "dispatcher-bound tools would all read as nonexistent")
        return 2

    callable_names = set(catalog) | set(PSEUDO_TOOLS) | handlers
    files = scan_files()
    if not files:
        print("advertised_tool_audit: ERROR no sources found under "
              f"{', '.join(SCAN_ROOTS)}")
        return 2

    unreachable: dict[str, list[str]] = {}   # tier A: real tool, no route to it
    nonexistent: dict[str, list[str]] = {}   # tier B: advertised, binds to nothing
    confirmed = 0                            # mentions that resolved to a reachable tool

    for path in files:
        try:
            for text, lineno, owner in all_strings(path):
                for name in mentions(text, tight=False):
                    if name not in callable_names or name == owner:
                        continue
                    if name in reach:
                        confirmed += 1
                    else:
                        unreachable.setdefault(name, []).append(f"{path}:{lineno}")
            for key, text, lineno in advertisement_strings(path):
                for name in mentions(text, tight=True):
                    if name in callable_names:
                        continue
                    allowed = NON_TOOL_MENTIONS.get(name)
                    if allowed and ("*" in allowed[1] or path in allowed[1]):
                        continue
                    nonexistent.setdefault(name, []).append(f"{path}:{lineno} [{key}]")
        except SyntaxError as exc:
            print(f"advertised_tool_audit: ERROR cannot parse {path}: {exc}")
            return 2

    # A gate must not be able to pass by finding nothing. If the extractor matched no
    # advertised tool at all, the patterns rotted (or the scan roots moved) and a clean
    # report would be a lie, not a clean tree.
    if confirmed == 0:
        print("advertised_tool_audit: ERROR the extractor matched ZERO advertised tool "
              "mentions across "
              f"{len(files)} files — the mention patterns or scan roots are stale")
        return 2

    print(f"advertised_tool_audit: {len(files)} files, {len(catalog)} registered tools, "
          f"{len(reach)} reachable, {confirmed} advertised mentions resolved")
    if not unreachable and not nonexistent:
        print("advertised_tool_audit: OK — every advertised tool is reachable")
        return 0

    for name in sorted(unreachable):
        sites = sorted(set(unreachable[name]))
        print(f"  UNREACHABLE  {name} — registered (family "
              f"{(catalog.get(name) or {}).get('family', '?')}) but in no category pack "
              f"and no role loadout; advertised at: {', '.join(sites[:4])}"
              + (f" (+{len(sites) - 4} more)" if len(sites) > 4 else ""))
    for name in sorted(nonexistent):
        sites = sorted(set(nonexistent[name]))
        print(f"  NO SUCH TOOL {name} — named as a call but binds to nothing "
              f"(not registered, not a pseudo-tool, not a MissionDispatch handler); "
              f"at: {', '.join(sites[:4])}"
              + (f" (+{len(sites) - 4} more)" if len(sites) > 4 else ""))
    total = len(unreachable) + len(nonexistent)
    print(f"\nadvertised_tool_audit: {total} advertised tool(s) the model cannot call. "
          f"Add the real ones to a CATEGORY_PACKS pack (or a role loadout); for a "
          f"deliberate non-tool mention, add it to NON_TOOL_MENTIONS with its reason.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
