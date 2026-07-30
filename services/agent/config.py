"""Provider configuration — resolve which backend + key to use.

Order of precedence: explicit arg → CHUDWARE_PROVIDER env var → %APPDATA%/Chudware/config.json →
default. The env var outranks config.json on purpose: it is a per-process override (tests use
CHUDWARE_PROVIDER=mock to guarantee no live model is called), while config.json is ambient user
state. config.json shape:
    {
      "provider": "anthropic",          // or "openai" / "llamacpp"
      "model": "claude-opus-4-8",
      "api_key": "sk-ant-...",          // optional; else read from env
      "openai": {"base_url": "http://localhost:1234/v1"},
      "llamacpp": {"model_path": "%APPDATA%/Chudware/models/model.gguf"},
      "compliance_mode": "off",         // "off" | "redact" | "local_only" (WS3)
      "offline_mode": false             // legacy hard local-only (still honored)
    }
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import threading
from collections.abc import Iterable
from urllib.parse import urlparse

from services import paths

from .providers.base import Provider

from . import model_catalog


# --------------------------------------------------------------------------- load/save cache
# config.json is read on EVERY tool dispatch (get_tool_options / is_tool_family_enabled
# call load_config() with cfg=None at least twice per tool call), so re-reading and
# re-parsing the file on every call is wasted work. Cache keyed on the resolved path
# (str(paths.CONFIG) — tests monkeypatch this to a tmp_path fixture, see test_effort.py)
# plus its mtime_ns; either changing invalidates and reparses. Mirrors
# advisor/triggers.py's _ensure_cache_fresh pattern. load_config() returns a deep copy
# so callers (notably save_config below, which mutates its result in place) can never
# corrupt the cached dict.
_cfg_cache: dict | None = None
_cfg_cache_path: str | None = None
_cfg_cache_mtime_ns: int | None = None


def load_config() -> dict:
    global _cfg_cache, _cfg_cache_path, _cfg_cache_mtime_ns
    path = paths.CONFIG
    path_key = str(path)
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        mtime_ns = None
    if (
        _cfg_cache is not None
        and _cfg_cache_path == path_key
        and _cfg_cache_mtime_ns == mtime_ns
    ):
        return copy.deepcopy(_cfg_cache)
    cfg: dict = {}
    if mtime_ns is not None:
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cfg = {}
    _cfg_cache, _cfg_cache_path, _cfg_cache_mtime_ns = cfg, path_key, mtime_ns
    return copy.deepcopy(cfg)


# --------------------------------------------------------------------- cross-process lock
# config.json is read-modify-written by SEVERAL processes: this worker, the C++ GUI
# (host/src/core/app_state.cpp and friends) and the engine (core/agent/auth.cpp). Without
# serialization, "A reads → B writes → A writes" silently drops B's keys — the class of bug
# behind the 2026-07-14 account wipe. Every writer takes the same advisory lock on
# <config.json>.lock; services/agent/providers/sub_auth.py and core/agent/auth.cpp already
# use that exact path, so this interoperates with them rather than inventing a second scheme.
_CFG_LOCK_SUFFIX = ".lock"
_cfg_write_lock = threading.RLock()
_cfg_lock_depth = 0


@contextlib.contextmanager
def config_lock():
    """Serialize a config.json read-modify-write against every other process and thread.

    Re-entrant. ``flock`` is per open-file-description, so a nested acquire in this process
    on a second fd would self-deadlock against itself; the recursive mutex already provides
    in-process exclusion, so only the OUTERMOST acquisition touches the lock file. Mirrors
    the depth-aware StoreLock in core/agent/auth.cpp.

    Degrades to the process lock alone where ``fcntl`` is unavailable (Windows): the GUI
    there takes the same lock via LockFileEx, and losing cross-process exclusion is strictly
    better than refusing to save.
    """
    global _cfg_lock_depth
    with _cfg_write_lock:
        if _cfg_lock_depth > 0:
            _cfg_lock_depth += 1
            try:
                yield
            finally:
                _cfg_lock_depth -= 1
            return
        try:
            import fcntl  # noqa: PLC0415 — POSIX only
        except ImportError:
            fcntl = None  # type: ignore[assignment]
        paths.CONFIG.parent.mkdir(parents=True, exist_ok=True)
        lock_p = paths.CONFIG.with_name(paths.CONFIG.name + _CFG_LOCK_SUFFIX)
        _cfg_lock_depth += 1
        try:
            if fcntl is None:
                yield
                return
            with open(lock_p, "a+", encoding="utf-8") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        finally:
            _cfg_lock_depth -= 1


def read_config_uncached() -> dict:
    """Parse config.json from disk, bypassing the mtime cache.

    save_config MUST NOT merge onto a cached snapshot: the cache is keyed on mtime_ns, and
    another process can land a write whose mtime we already recorded. Inside the lock the
    file is quiescent, so one honest read is both correct and cheap.
    """
    try:
        return json.loads(paths.CONFIG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing/corrupt config starts empty, as load_config does
        return {}

def _deep_merge(base: dict, updates: dict) -> dict:
    """Recursively merge dict-valued keys of *updates* into *base* in place;
    scalars/lists in *updates* replace outright. Lets save_config's read-merge-write
    honor its documented contract for nested writes (e.g. {"keys": {...}}), not just
    top-level ones."""
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def save_config(cfg: dict, *, replace: Iterable[str] = ()) -> None:
    """Read-merge-write a partial update into config.json (creating it on first run).

    Deep-merges into whatever is already on disk — recursing into dict-valued keys,
    replacing scalars/lists outright — so one caller (e.g. a first-run consent prompt
    setting telemetry_consent) never clobbers keys another caller owns (provider/api_key/
    etc.), including nested siblings under a shared parent key (e.g. {"keys": {...}}) —
    the same non-destructive contract the C++ GUI's first-run dialog follows when it
    writes this file directly (ui/imgui/src/ui/first_run.cpp).

    *replace* names top-level keys the caller owns WHOLESALE: they are dropped before
    the merge so the incoming value is authoritative. Needed when the caller's dict is
    the complete value of that key (e.g. the full ai_roles map) — a deep merge would
    otherwise union each nested entry with the old one and silently resurrect fields
    the caller deliberately removed. Everything not named here still merges.

    The whole read-merge-write runs under :func:`config_lock`, and the read bypasses the
    mtime cache — otherwise a concurrent writer (the C++ GUI, the engine) can land between
    our read and our write and have its keys silently reverted. The write itself is
    tmp+replace so no reader ever observes a half-written file.
    """
    with config_lock():
        current = read_config_uncached()
        for key in replace:
            current.pop(key, None)
        _deep_merge(current, cfg)
        paths.CONFIG.parent.mkdir(parents=True, exist_ok=True)
        # Per-process tmp name: the lock already serializes well-behaved writers, but a
        # shared ".tmp" means any writer that skips the lock races us into a rename of a
        # file we already moved (FileNotFoundError) or, worse, publishes our half-file.
        tmp = paths.CONFIG.with_name("%s.tmp.%d" % (paths.CONFIG.name, os.getpid()))
        try:
            tmp.write_text(json.dumps(current, indent=2), encoding="utf-8")
            os.replace(tmp, paths.CONFIG)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()  # no-op after a successful replace; cleans up a failed write


def workspace_dir(cfg: dict | None = None) -> "Path":
    """Resolve the GUI workspace directory (Settings ``workspace_dir``).

    Mirrors host ``defaultWorkspaceDir`` / ``AppState::workspaceDir``:
      1. config.json ``workspace_dir`` when set (absolute or ~-expanded)
      2. existing ``~/Documents/Chudware`` (legacy alias wins)
      3. else ``~/Documents/Chudware``

    Always returns a Path; creates the directory when missing (best-effort).
    """
    from pathlib import Path  # noqa: PLC0415 — keep module import surface small

    cfg = cfg if cfg is not None else load_config()
    raw = (cfg or {}).get("workspace_dir") or ""
    if isinstance(raw, str) and raw.strip():
        d = Path(os.path.expanduser(raw.strip())).expanduser()
    else:
        home = Path(os.path.expanduser("~"))
        docs = home / "Documents"
        legacy = docs / "Chudware"
        if legacy.is_dir():
            d = legacy
        else:
            d = docs / "Chudware"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


# CAD/ECAD/doc extensions surfaced first in the working-directory roster so the
# agent sees the parts before build detritus. Order = priority.
_WORKSPACE_EXT_PRIORITY = (
    ".step+", ".ecad+", ".step", ".stp", ".dxf", ".dwg", ".stl",
    ".pdf", ".md", ".csv",
)


def list_workspace_files(root: "Path", limit: int = 200) -> list[str]:
    """Relative POSIX paths of interesting files under *root* (recursive).

    Skips dot-directories (``.plm``, ``.aicadvault``, ``.git``, …). CAD/ECAD/doc
    files sort first (``_WORKSPACE_EXT_PRIORITY``), everything else after, each
    group lexicographic. Truncates to *limit*, appending ``"… (+N more)"`` when
    more remain. Best-effort — an unreadable tree yields ``[]``.
    """
    from pathlib import Path  # noqa: PLC0415

    root = Path(root)
    prio: list[str] = []
    rest: list[str] = []
    try:
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(root)
            if any(part.startswith(".") for part in rel.parts):
                continue
            posix = rel.as_posix()
            name = p.name.lower()
            if any(name.endswith(ext) for ext in _WORKSPACE_EXT_PRIORITY):
                prio.append(posix)
            else:
                rest.append(posix)
    except OSError:
        return []

    def _key(rel: str) -> tuple[int, str]:
        low = rel.lower()
        for i, ext in enumerate(_WORKSPACE_EXT_PRIORITY):
            if low.endswith(ext):
                return (i, rel)
        return (len(_WORKSPACE_EXT_PRIORITY), rel)

    ordered = sorted(prio, key=_key) + sorted(rest)
    if len(ordered) <= limit:
        return ordered
    extra = len(ordered) - limit
    return ordered[:limit] + [f"… (+{extra} more)"]

# --------------------------------------------------------------------------- agent nesting depth
# GUI Settings → Permissions writes ``agent.max_subagent_depth`` (int, default 2,
# GUI clamp 0–4). Root chat = depth 0. 0 means depth-1 subagents may not spawn
# further subagents. Enforced in delegation.DelegationTree.request_delegation.

DEFAULT_MAX_SUBAGENT_DEPTH: int = 2
AGENT_CONFIG_KEY = "agent"
MAX_SUBAGENT_DEPTH_KEY = "max_subagent_depth"


def get_max_subagent_depth(cfg: dict | None = None) -> int:
    """Return ``agent.max_subagent_depth`` (int ≥ 0, default 2).

    Nested config shape written by the copilot Permissions tab::

        {"agent": {"max_subagent_depth": 2}}

    Non-int / missing values fall back to :data:`DEFAULT_MAX_SUBAGENT_DEPTH`.
    Negative values clamp to 0 (no subagent may spawn further subagents).
    """
    c = cfg if cfg is not None else load_config()
    if not isinstance(c, dict):
        return DEFAULT_MAX_SUBAGENT_DEPTH
    agent = c.get(AGENT_CONFIG_KEY)
    if not isinstance(agent, dict):
        return DEFAULT_MAX_SUBAGENT_DEPTH
    raw = agent.get(MAX_SUBAGENT_DEPTH_KEY, DEFAULT_MAX_SUBAGENT_DEPTH)
    try:
        depth = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_SUBAGENT_DEPTH
    return max(0, depth)

# --------------------------------------------------------------------------- corporate org mode
# GUI Settings → Agents writes ``agent.corporate_mode`` (bool) and
# ``agent.corporate`` object. When on, complex missions organize as a company
# (director + departments + heads + workers) instead of flat delegation.
# See aicad.corporate/1.

CORPORATE_MODE_KEY = "corporate_mode"
CORPORATE_KEY = "corporate"
DEFAULT_CORPORATE_MODE: bool = False
DEFAULT_CORPORATE_MAX_DEPARTMENTS: int = 3
DEFAULT_CORPORATE_TEAM_LEADS: bool = True
DEFAULT_CORPORATE_MEETING_CADENCE: int = 5
DEFAULT_CORPORATE_DIRECTOR_REVIEW: bool = True


def corporate_mode_enabled(cfg: dict | None = None) -> bool:
    """Return ``agent.corporate_mode`` (bool, default False).

    Nested config shape written by the copilot Agents tab::

        {"agent": {"corporate_mode": true, "corporate": {...}}}
    """
    c = cfg if cfg is not None else load_config()
    if not isinstance(c, dict):
        return DEFAULT_CORPORATE_MODE
    agent = c.get(AGENT_CONFIG_KEY)
    if not isinstance(agent, dict):
        return DEFAULT_CORPORATE_MODE
    return bool(agent.get(CORPORATE_MODE_KEY, DEFAULT_CORPORATE_MODE))


def get_corporate_config(cfg: dict | None = None) -> dict:
    """Return normalized ``agent.corporate`` settings.

    Keys (with defaults):
      max_departments (int, default 3, clamped ≥ 1)
      team_leads (bool, default True)
      meeting_cadence (int, default 5, clamped ≥ 1) — worker completions
        between department-head meetings
      director_review (bool, default True)
    """
    c = cfg if cfg is not None else load_config()
    agent: dict = {}
    if isinstance(c, dict):
        raw_agent = c.get(AGENT_CONFIG_KEY)
        if isinstance(raw_agent, dict):
            agent = raw_agent
    raw = agent.get(CORPORATE_KEY) if isinstance(agent.get(CORPORATE_KEY), dict) else {}

    def _int(key: str, default: int, *, min_v: int = 1) -> int:
        try:
            v = int(raw.get(key, default))  # type: ignore[union-attr]
        except (TypeError, ValueError):
            v = default
        return max(min_v, v)

    def _bool(key: str, default: bool) -> bool:
        if key not in raw:  # type: ignore[operator]
            return default
        return bool(raw.get(key))  # type: ignore[union-attr]

    return {
        "max_departments": _int("max_departments", DEFAULT_CORPORATE_MAX_DEPARTMENTS),
        "team_leads": _bool("team_leads", DEFAULT_CORPORATE_TEAM_LEADS),
        "meeting_cadence": _int("meeting_cadence", DEFAULT_CORPORATE_MEETING_CADENCE),
        "director_review": _bool("director_review", DEFAULT_CORPORATE_DIRECTOR_REVIEW),
    }

# --------------------------------------------------------------------------- advisor (foreman)
# GUI Settings / host options write ``agent.advisor`` (bool, default True).
# Env ``CHUDWARE_ADVISOR`` still honored (0/false disables). Used by loop.py and
# MissionDispatch so every agent/subagent can reach the advisor.

ADVISOR_KEY = "advisor"
DEFAULT_ADVISOR: bool = True


def advisor_enabled(cfg: dict | None = None) -> bool:
    """Return whether the foreman advisor is on (default True).

    Precedence:
      1. Env ``CHUDWARE_ADVISOR`` — when set to 0/false/False, force off.
         When set to 1/true/True, force on (overrides config).
      2. ``agent.advisor`` in config.json (bool, default True).
    """
    import os

    env = os.environ.get("CHUDWARE_ADVISOR")
    if env is not None and str(env).strip() != "":
        return str(env).strip().lower() not in ("0", "false", "no", "off")
    agent = _agent_block(cfg)
    if ADVISOR_KEY in agent:
        return bool(agent.get(ADVISOR_KEY))
    return DEFAULT_ADVISOR


# --------------------------------------------------------------------------- preference enums
# Shared with host Settings: off | option | encourage | require
VALID_PREFERENCES = frozenset({"off", "option", "encourage", "require"})
# Default Off: main chat should tool-call directly instead of forcing a
# plan/intake breakdown (Settings → AI → Harness "Plan preference").
DEFAULT_PLAN_PREFERENCE = "off"
DEFAULT_SUBAGENT_PREFERENCE = "option"
# Default Encourage: the copilot Task-list panel is only useful if the model
# actually publishes to it, and before this knob existed the panel read
# "No task list yet" on every general-lane run.
DEFAULT_TASK_LIST_PREFERENCE = "encourage"
DEFAULT_ADVISOR_PREFERENCE = "encourage"
DEFAULT_PROACTIVE_ADVISOR = False
DEFAULT_MEMORY_ENABLED = True
DEFAULT_MEMORY_TOKEN_BUDGET = 400


def _parse_preference(raw, default: str) -> str:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return "encourage" if raw else "off"
    if isinstance(raw, (int, float)):
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return default
        return ("off", "option", "encourage", "require")[n] if 0 <= n <= 3 else default
    s = str(raw).strip().lower()
    if s in ("0", "false", "no"):
        return "off"
    if s in ("1", "optional"):
        return "option"
    if s in ("2", "recommended", "true", "yes", "on"):
        return "encourage"
    if s in ("3", "required", "always"):
        return "require"
    return s if s in VALID_PREFERENCES else default


def get_plan_preference(cfg: dict | None = None) -> str:
    """Return plan preference string (default off). Honors legacy plan_mode bool."""
    c = cfg if cfg is not None else load_config()
    if not isinstance(c, dict):
        return DEFAULT_PLAN_PREFERENCE
    if "plan_preference" in c:
        return _parse_preference(c.get("plan_preference"), DEFAULT_PLAN_PREFERENCE)
    agent = c.get(AGENT_CONFIG_KEY) if isinstance(c.get(AGENT_CONFIG_KEY), dict) else {}
    if isinstance(agent, dict) and "plan_preference" in agent:
        return _parse_preference(agent.get("plan_preference"), DEFAULT_PLAN_PREFERENCE)
    # Legacy bool — only honor when plan_preference was never set.
    if "plan_mode" in c:
        return "encourage" if bool(c.get("plan_mode")) else "off"
    return DEFAULT_PLAN_PREFERENCE


def get_subagent_preference(cfg: dict | None = None) -> str:
    c = cfg if cfg is not None else load_config()
    agent = _agent_block(c if isinstance(c, dict) else None)
    if "subagent_preference" in agent:
        return _parse_preference(agent.get("subagent_preference"), DEFAULT_SUBAGENT_PREFERENCE)
    if isinstance(c, dict) and "subagent_preference" in c:
        return _parse_preference(c.get("subagent_preference"), DEFAULT_SUBAGENT_PREFERENCE)
    return DEFAULT_SUBAGENT_PREFERENCE


def get_task_list_preference(cfg: dict | None = None) -> str:
    """off|option|encourage|require — how hard to push the model to publish a
    live task list (Settings → AI → Harness "Task list preference")."""
    c = cfg if cfg is not None else load_config()
    agent = _agent_block(c if isinstance(c, dict) else None)
    if "task_list_preference" in agent:
        return _parse_preference(agent.get("task_list_preference"), DEFAULT_TASK_LIST_PREFERENCE)
    if isinstance(c, dict) and "task_list_preference" in c:
        return _parse_preference(c.get("task_list_preference"), DEFAULT_TASK_LIST_PREFERENCE)
    return DEFAULT_TASK_LIST_PREFERENCE



DEFAULT_ROLE_ADOPTION = "supervised"  # supervised | free


def get_role_adoption_mode(cfg: dict | None = None) -> str:
    """Subagent role adoption: supervised (default) or free.

    supervised — only the main/root agent may adopt_role; subagents request via advisor.
    free — subagents may adopt_role themselves (still depth-capped for spawns).
    """
    try:
        from services.agent.role_catalog import get_role_adoption_mode as _g
        return _g(cfg)
    except Exception:  # noqa: BLE001
        c = cfg if cfg is not None else load_config()
        agent = c.get("agent") if isinstance(c, dict) and isinstance(c.get("agent"), dict) else {}
        raw = str((agent or {}).get("role_adoption") or c.get("role_adoption") or DEFAULT_ROLE_ADOPTION)
        raw = raw.strip().lower()
        return "free" if raw in ("free", "free_reign", "free-reign") else "supervised"


def get_advisor_preference(cfg: dict | None = None) -> str:
    """off|option|encourage|require. Env CHUDWARE_ADVISOR=0 forces off."""
    import os

    env = os.environ.get("CHUDWARE_ADVISOR")
    if env is not None and str(env).strip() != "":
        if str(env).strip().lower() in ("0", "false", "no", "off"):
            return "off"
    c = cfg if cfg is not None else load_config()
    agent = _agent_block(c if isinstance(c, dict) else None)
    if "advisor_preference" in agent:
        return _parse_preference(agent.get("advisor_preference"), DEFAULT_ADVISOR_PREFERENCE)
    if isinstance(c, dict) and "advisor_preference" in c:
        return _parse_preference(c.get("advisor_preference"), DEFAULT_ADVISOR_PREFERENCE)
    # Fall back to bool agent.advisor
    if not advisor_enabled(c if isinstance(c, dict) else None):
        return "off"
    return DEFAULT_ADVISOR_PREFERENCE


def proactive_advisor_enabled(cfg: dict | None = None) -> bool:
    """Opt-in mid-run proactive advisor (default False).

    Separate from advisor_preference (tone/frequency). Hard gate for the
    agent_turn proactive inject path. Env ``CHUDWARE_PROACTIVE_ADVISOR`` forces
    on/off when set. Also off when advisor preference is off.
    """
    import os

    env = os.environ.get("CHUDWARE_PROACTIVE_ADVISOR")
    if env is not None and str(env).strip() != "":
        if str(env).strip().lower() in ("0", "false", "no", "off"):
            return False
        if str(env).strip().lower() in ("1", "true", "yes", "on"):
            # Still respect advisor preference off / CHUDWARE_ADVISOR=0.
            return get_advisor_preference(cfg) != "off"
    if get_advisor_preference(cfg) == "off":
        return False
    c = cfg if cfg is not None else load_config()
    agent = _agent_block(c if isinstance(c, dict) else None)
    if "proactive_advisor" in agent:
        return bool(agent.get("proactive_advisor"))
    if isinstance(c, dict) and "proactive_advisor" in c:
        return bool(c.get("proactive_advisor"))
    return DEFAULT_PROACTIVE_ADVISOR



# ---------------------------------------------------------------------------
# Tool families (Settings → AI → Tools)
# agent.tool_families: { "cad": true, "em": false, ... }
# Missing key = enabled (backward compatible). Env CHUDWARE_TOOLS_ALLOW still wins
# at the registry layer when set.
# ---------------------------------------------------------------------------
DEFAULT_TOOL_FAMILIES = (
    "advisor", "cad", "cam", "ecad", "em", "files", "gsl", "intake", "ledger",
    "mission", "office", "org", "parts", "plm", "recon", "research", "reveng",
    "shell", "sim", "workspace",
)

# ---------------------------------------------------------------------------
# Per-role default tool families (Settings → AI → Tools)
# agent.role_tools: { "default": ["cad", "files", ...], "task": [...], ... }
# Keys match Settings → AI → Roles harness roles (advisor/task/planning/…).
# Missing key → DEFAULT_ROLE_TOOLS[role]. Empty list = no family defaults
# (SHARED/AGENT floors in apply_tool_policy still apply).
# ---------------------------------------------------------------------------
HARNESS_ROLE_KEYS = (
    "advisor", "task", "planning", "vision", "default", "plm", "redaction",
    "sketch2d", "model3d", "memory", "cam", "sim", "ecad", "drawing", "reveng",
)

# Sensible default loadouts — lean specialists, broad "default" (main chat).
DEFAULT_ROLE_TOOLS: dict[str, tuple[str, ...]] = {
    "advisor": ("advisor", "research", "ledger", "gsl"),
    "task": ("cad", "sim", "cam", "parts", "gsl", "files", "shell", "office"),
    "planning": ("mission", "advisor", "ledger", "research", "gsl", "workspace"),
    "vision": ("gsl", "cad"),
    "default": (
        "cad", "files", "shell", "office", "plm", "research", "parts", "gsl",
        "advisor", "workspace", "ledger", "sim", "cam", "ecad", "reveng",
        "mission",
    ),
    "plm": ("plm", "files", "office", "ledger"),
    "redaction": (),
    "sketch2d": ("cad", "gsl", "ecad"),
    "model3d": ("cad", "sim", "parts", "gsl"),
    "memory": ("ledger", "plm", "files"),
    "cam": ("cam", "cad", "parts"),
    "sim": ("sim", "cad", "gsl"),
    "ecad": ("ecad", "parts", "research", "em"),
    "drawing": ("cad", "office", "gsl"),
    "reveng": ("reveng", "research", "cad", "parts"),
}

# YAML role card id → harness role for role_tools lookup.
YAML_ROLE_TO_HARNESS: dict[str, str] = {
    "general_agent": "default",
    "advisor": "advisor",
    "orchestrator": "planning",
    "department_manager": "planning",
    "overseer": "planning",
    "project_organizer": "planning",
    "intake_analyst": "planning",
    "clarifier": "planning",
    "spec_writer": "planning",
    "design_thinker": "planning",
    "layout_engineer": "model3d",
    "feature_architect": "model3d",
    "feature_modeler": "task",
    "geometry_designer": "model3d",
    "sketcher": "sketch2d",
    "sketch2d": "sketch2d",
    "model3d": "model3d",
    "assembly_fit_engineer": "task",
    "linkage_planner": "task",
    "cam_programmer": "cam",
    "sim_engineer": "sim",
    "ecad_engineer": "ecad",
    "firmware_engineer": "ecad",
    "drawing_detailer": "drawing",
    "re_modeler": "reveng",
    "re_datasheet_reader": "reveng",
    "re_geometry_verifier": "reveng",
    "sourcing_specialist": "task",
    "reviewer": "advisor",
    "fixer": "task",
    "memory": "memory",
}

# Main-chat agent uses a full tool surface (no 14-tool cap / no lean packs) when
# agent.direct_agent_full_tools is true (default). Settings → AI → Harness.
DEFAULT_DIRECT_AGENT_FULL_TOOLS = True
# Agent-mode auto-route to mission_start / agent_mission_v1 on build+analyze
# phrasing. Default off — main chat stays on the direct general_agent turn.
DEFAULT_AUTO_MISSION_ROUTING = False


def get_role_tools(role: str, cfg: dict | None = None) -> list[str]:
    """Return the default tool-family list for a harness *role*.

    Precedence: ``agent.role_tools.<role>`` (list) > :data:`DEFAULT_ROLE_TOOLS`.
    Unknown roles fall back to ``default``. Families not in
    :data:`DEFAULT_TOOL_FAMILIES` are kept (forward-compatible) but blank/non-
    string entries are dropped.
    """
    role_s = (role or "default").strip().lower() or "default"
    defaults = list(DEFAULT_ROLE_TOOLS.get(role_s, DEFAULT_ROLE_TOOLS["default"]))
    try:
        c = cfg if isinstance(cfg, dict) else load_config()
    except Exception:  # noqa: BLE001
        c = cfg if isinstance(cfg, dict) else {}
    agent = c.get("agent") if isinstance(c, dict) and isinstance(c.get("agent"), dict) else {}
    raw_root = agent.get("role_tools") if isinstance(agent, dict) else None
    if not isinstance(raw_root, dict):
        return defaults
    # Only the explicit role key overrides that role's defaults. Do NOT fall through
    # to config's "default" list for other roles — that would clobber specialist
    # loadouts when the user only customized the main-chat Default role.
    if role_s not in raw_root:
        return defaults
    entry = raw_root.get(role_s)
    if entry is None:
        return defaults
    if not isinstance(entry, (list, tuple)):
        return defaults
    out: list[str] = []
    seen: set[str] = set()
    for item in entry:
        if not isinstance(item, str):
            continue
        fam = item.strip().lower()
        if not fam or fam in seen:
            continue
        seen.add(fam)
        out.append(fam)
    return out


def get_all_role_tools(cfg: dict | None = None) -> dict[str, list[str]]:
    """Full harness-role → families map (defaults filled for every known role)."""
    return {k: get_role_tools(k, cfg) for k in HARNESS_ROLE_KEYS}


def harness_role_for_yaml(role_id: str) -> str:
    """Map a YAML role-card id to a harness role key for :func:`get_role_tools`."""
    rid = (role_id or "").strip().lower()
    if not rid:
        return "default"
    if rid in HARNESS_ROLE_KEYS:
        return rid
    return YAML_ROLE_TO_HARNESS.get(rid, "default")


def direct_agent_full_tools(cfg: dict | None = None) -> bool:
    """When True, main-chat ``general_agent`` / ``default`` get an uncapped tool surface."""
    try:
        env = os.environ.get("CHUDWARE_DIRECT_AGENT_FULL_TOOLS")
        if env is not None and str(env).strip() != "":
            return str(env).strip().lower() not in ("0", "false", "no", "off")
    except Exception:  # noqa: BLE001
        pass
    try:
        agent = _agent_block(cfg)
        if "direct_agent_full_tools" in agent:
            return bool(agent.get("direct_agent_full_tools"))
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_DIRECT_AGENT_FULL_TOOLS


def auto_mission_routing(cfg: dict | None = None) -> bool:
    """When True, Agent mode may auto-route build+analyze phrasing to a mission."""
    try:
        env = os.environ.get("CHUDWARE_AUTO_MISSION_ROUTING")
        if env is not None and str(env).strip() != "":
            return str(env).strip().lower() not in ("0", "false", "no", "off")
    except Exception:  # noqa: BLE001
        pass
    try:
        agent = _agent_block(cfg)
        if "auto_mission_routing" in agent:
            return bool(agent.get("auto_mission_routing"))
    except Exception:  # noqa: BLE001
        pass
    return DEFAULT_AUTO_MISSION_ROUTING


# ---------------------------------------------------------------------------
# Per-tool options (Settings → AI → Tools)
# agent.tool_options: { "run_fea_static": {"timeout_s": 180, "prompt_note": "..."}, ... }
# Missing key / missing field = stock behavior.
# Layering per-field: stock defaults < built-in curated guidance
# (services/agent.tool_guidance) < config.json entry (the user always wins).
#
# Keys (extend in place — do not invent a parallel config tree):
#   enabled              bool — kill-switch for this tool name
#   timeout_s            int  — per-call wall timeout (0/absent → global default)
#   max_calls_per_turn   int  — per-turn call cap (0/absent → global default)
#   param_defaults       dict — kwargs merged under the model's call (model wins)
#   prompt_note          str  — AI usage guidance appended as "Note: …" in the
#                               tools-slot schema (tool_schemas.render_tools_block)
#   description_override str  — replaces the MCP docstring in the tools-slot
#                               (included tool context); empty = use live docstring
#   schema_trim          bool|null — per-tool override of the tier's
#                               tool_schema_trim dial (None = inherit tier)
# ---------------------------------------------------------------------------
TOOL_OPTION_KEYS = (
    "enabled", "timeout_s", "max_calls_per_turn", "param_defaults", "prompt_note",
    "description_override", "schema_trim",
)

_TOOL_OPTION_DEFAULTS: dict = {
    "enabled": True,
    "timeout_s": None,
    "max_calls_per_turn": None,
    "param_defaults": {},
    "prompt_note": "",
    "description_override": "",
    "schema_trim": None,
}


def get_tool_options(cfg: dict | None, name: str) -> dict:
    """Merged per-tool options for *name*. Unknown stored keys ignored.

    Layering (per-field): stock defaults < built-in curated guidance
    (``services.agent.tool_guidance``) < the user's config.json entry —
    the user always wins per-field."""
    out = {
        "enabled": True,
        "timeout_s": None,
        "max_calls_per_turn": None,
        "param_defaults": {},
        "prompt_note": "",
        "description_override": "",
        "schema_trim": None,
    }
    if not name:
        return out

    def _apply(entry: dict) -> None:
        if not isinstance(entry, dict):
            return
        if "enabled" in entry:
            out["enabled"] = bool(entry.get("enabled"))
        if "timeout_s" in entry:
            try:
                v = entry.get("timeout_s")
                out["timeout_s"] = None if v in (None, "", 0, "0") else max(1, int(v))
            except (TypeError, ValueError):
                pass
        if "max_calls_per_turn" in entry:
            try:
                v = entry.get("max_calls_per_turn")
                out["max_calls_per_turn"] = (
                    None if v in (None, "", 0, "0") else max(1, int(v))
                )
            except (TypeError, ValueError):
                pass
        if "param_defaults" in entry and isinstance(entry.get("param_defaults"), dict):
            out["param_defaults"] = dict(entry.get("param_defaults") or {})
        if "prompt_note" in entry and entry.get("prompt_note") is not None:
            out["prompt_note"] = str(entry.get("prompt_note") or "")
        if "description_override" in entry and entry.get("description_override") is not None:
            out["description_override"] = str(entry.get("description_override") or "")
        if "schema_trim" in entry:
            v = entry.get("schema_trim")
            if v is None or v == "":
                out["schema_trim"] = None
            else:
                out["schema_trim"] = bool(v)

    try:
        from services.agent.tool_guidance import guidance_for  # noqa: PLC0415
        _apply(guidance_for(name))
    except Exception:  # noqa: BLE001 — the curated layer is optional
        pass
    try:
        c = cfg if isinstance(cfg, dict) else load_config()
    except Exception:  # noqa: BLE001
        c = cfg if isinstance(cfg, dict) else {}
    agent = c.get("agent") if isinstance(c, dict) and isinstance(c.get("agent"), dict) else {}
    raw_root = agent.get("tool_options") if isinstance(agent, dict) else None
    if not isinstance(raw_root, dict):
        return out
    _apply(raw_root.get(name))
    return out


# ---------------------------------------------------------------------------
# Global tool-dispatch defaults (Settings → AI → Tools "Advanced")
# agent.tool_timeout_s_default      int seconds, 0 = no global timeout (default 0)
# agent.tool_max_calls_default      int, 0 = unlimited (default 0)
# agent.supervision_interval_calls  int tool calls between forced proactive-advisor
#                                    verification passes, 0 = never force (default 4)
# Precedence ENV > config.json > default. Per-tool ``agent.tool_options.<tool>``
# entries (see get_tool_options above) always win over these globals — callers
# only fall back to them when the per-tool value is absent/0. Never throw:
# any parse failure (bad env, missing/malformed config) yields the default.
# ---------------------------------------------------------------------------

def tool_timeout_s_default(cfg: dict | None = None) -> int:
    """Global default per-tool timeout in seconds (``agent.tool_timeout_s_default``).

    0 means "no global timeout" — only an explicit per-tool
    ``agent.tool_options.<tool>.timeout_s`` applies (today's behavior).
    Precedence: env ``CHUDWARE_TOOL_TIMEOUT_S`` > config.json > default (0).
    Negative values clamp to 0; unparseable env/config values fall back to 0.
    """
    try:
        env = os.environ.get("CHUDWARE_TOOL_TIMEOUT_S")
        if env is not None and str(env).strip() != "":
            return max(0, int(str(env).strip()))
    except (TypeError, ValueError):
        pass
    try:
        agent = _agent_block(cfg)
        return max(0, int(agent.get("tool_timeout_s_default", 0)))
    except Exception:  # noqa: BLE001
        return 0


def tool_max_calls_default(cfg: dict | None = None) -> int:
    """Global default max-calls-per-turn cap (``agent.tool_max_calls_default``).

    0 means "unlimited" — only an explicit per-tool
    ``agent.tool_options.<tool>.max_calls_per_turn`` applies (today's behavior).
    Precedence: env ``CHUDWARE_TOOL_MAX_CALLS`` > config.json > default (0).
    Negative values clamp to 0; unparseable env/config values fall back to 0.
    """
    try:
        env = os.environ.get("CHUDWARE_TOOL_MAX_CALLS")
        if env is not None and str(env).strip() != "":
            return max(0, int(str(env).strip()))
    except (TypeError, ValueError):
        pass
    try:
        agent = _agent_block(cfg)
        return max(0, int(agent.get("tool_max_calls_default", 0)))
    except Exception:  # noqa: BLE001
        return 0


def tool_loop_guard_enabled(cfg: dict | None = None) -> bool:
    """Repeated-identical-tool-call detector kill-switch (``agent.tool_loop_guard_enabled``).

    Default True (ported from OMP's ToolCallLoopGuard — see tool_loop_guard.py).
    Precedence: env ``CHUDWARE_TOOL_LOOP_GUARD`` > config.json > default (True).
    """
    try:
        env = os.environ.get("CHUDWARE_TOOL_LOOP_GUARD")
        if env is not None and str(env).strip() != "":
            return str(env).strip().lower() not in ("0", "false", "no", "off")
    except Exception:  # noqa: BLE001
        pass
    try:
        agent = _agent_block(cfg)
        return bool(agent.get("tool_loop_guard_enabled", True))
    except Exception:  # noqa: BLE001
        return True


def tool_loop_guard_threshold(cfg: dict | None = None) -> int:
    """Consecutive identical (tool, args) repeats that trip the loop guard
    (``agent.tool_loop_guard_threshold``). Default 3; clamped to >= 1.
    Precedence: env ``CHUDWARE_TOOL_LOOP_GUARD_THRESHOLD`` > config.json > default (3).
    """
    try:
        env = os.environ.get("CHUDWARE_TOOL_LOOP_GUARD_THRESHOLD")
        if env is not None and str(env).strip() != "":
            return max(1, int(str(env).strip()))
    except (TypeError, ValueError):
        pass
    try:
        agent = _agent_block(cfg)
        return max(1, int(agent.get("tool_loop_guard_threshold", 3)))
    except Exception:  # noqa: BLE001
        return 3


def supervision_interval_calls(cfg: dict | None = None) -> int:
    """Tool calls between forced advisor checks (``agent.supervision_interval_calls``).

    Drives ``proactive_advisor._REQUIRE_EVERY_N``: with advisor preference
    ``require``, a verification pass is force-injected every N tool calls.
    0 disables require-mode entirely (never force-inject).
    Precedence: env ``CHUDWARE_SUPERVISION_INTERVAL`` > config.json > default (4).
    Negative values clamp to 0; unparseable env/config values fall back to 4.
    """
    try:
        env = os.environ.get("CHUDWARE_SUPERVISION_INTERVAL")
        if env is not None and str(env).strip() != "":
            return max(0, int(str(env).strip()))
    except (TypeError, ValueError):
        pass
    try:
        agent = _agent_block(cfg)
        return max(0, int(agent.get("supervision_interval_calls", 4)))
    except Exception:  # noqa: BLE001
        return 4


def get_tool_families(cfg: dict | None = None) -> dict[str, bool]:
    """Return family→enabled map.

    When ``agent.role_tools`` is present, a family is enabled if ANY harness
    role lists it (union). Legacy ``agent.tool_families`` still wins as an
    explicit False kill-switch for a family. Missing everything → all on.
    """
    c = cfg if isinstance(cfg, dict) else load_config()
    agent = c.get("agent") if isinstance(c.get("agent"), dict) else {}
    raw = agent.get("tool_families") if isinstance(agent.get("tool_families"), dict) else {}
    role_tools_raw = agent.get("role_tools") if isinstance(agent.get("role_tools"), dict) else None

    # Union of role assignments when role_tools is configured.
    role_union: set[str] | None = None
    if isinstance(role_tools_raw, dict) and role_tools_raw:
        role_union = set()
        for role_key in HARNESS_ROLE_KEYS:
            for fam in get_role_tools(role_key, c):
                role_union.add(fam)

    out: dict[str, bool] = {}
    for fam in DEFAULT_TOOL_FAMILIES:
        if fam in raw and isinstance(raw[fam], bool):
            out[fam] = raw[fam]
        elif role_union is not None:
            out[fam] = fam in role_union
        else:
            out[fam] = True
    for k, v in raw.items():
        if k not in out:
            out[str(k)] = bool(v)
    return out


def is_tool_family_enabled(family: str, cfg: dict | None = None) -> bool:
    """True when *family* is enabled (unknown family defaults True)."""
    if not family:
        return True
    fams = get_tool_families(cfg)
    return bool(fams.get(family, True))


def memory_enabled(cfg: dict | None = None) -> bool:
    c = cfg if cfg is not None else load_config()
    if isinstance(c, dict) and "memory_enabled" in c:
        return bool(c.get("memory_enabled"))
    agent = _agent_block(c if isinstance(c, dict) else None)
    if "memory_enabled" in agent:
        return bool(agent.get("memory_enabled"))
    return DEFAULT_MEMORY_ENABLED


def get_memory_token_budget(cfg: dict | None = None) -> int:
    c = cfg if cfg is not None else load_config()
    raw = None
    if isinstance(c, dict) and "memory_token_budget" in c:
        raw = c.get("memory_token_budget")
    else:
        agent = _agent_block(c if isinstance(c, dict) else None)
        raw = agent.get("memory_token_budget", DEFAULT_MEMORY_TOKEN_BUDGET)
    try:
        b = int(raw)
    except (TypeError, ValueError):
        b = DEFAULT_MEMORY_TOKEN_BUDGET
    return max(50, min(4000, b))


# --------------------------------------------------------------------------- memory & learning backends
# GUI Settings → AI → Memory & Learning writes these under ``agent`` (omp port):
#   agent.memory_backend      "sqlite" | "mnemopi"   (default "sqlite")
#   agent.compaction_backend  "builtin" | "omp"      (default "builtin")
#   agent.cache_warming       bool                   (default False)
#   agent.memory_learn        bool                   (default True)
#   agent.memory_model        "online" | "local"     (default "online"; mnemopi LLM)
#   agent.autolearn_enabled / autolearn_auto_continue / autolearn_min_tool_calls
#     — OMP Auto-Learn sidecar (Settings → AI → Harness → Memory & Learning)
# Precedence is ENV > config.json > default: CHUDWARE_MEMORY_BACKEND /
# CHUDWARE_COMPACTION_BACKEND still win when set (per-process override; the GUI
# comboboxes write config.json only).

MEMORY_BACKEND_KEY = "memory_backend"
COMPACTION_BACKEND_KEY = "compaction_backend"
CACHE_WARMING_KEY = "cache_warming"
MEMORY_LEARN_KEY = "memory_learn"
MEMORY_MODEL_KEY = "memory_model"
AUTOLEARN_ENABLED_KEY = "autolearn_enabled"
AUTOLEARN_AUTO_CONTINUE_KEY = "autolearn_auto_continue"
AUTOLEARN_MIN_TOOL_CALLS_KEY = "autolearn_min_tool_calls"

DEFAULT_MEMORY_BACKEND: str = "sqlite"
DEFAULT_COMPACTION_BACKEND: str = "builtin"
DEFAULT_CACHE_WARMING: bool = False
DEFAULT_MEMORY_LEARN: bool = True
DEFAULT_MEMORY_MODEL: str = "online"
DEFAULT_AUTOLEARN_ENABLED: bool = False
DEFAULT_AUTOLEARN_AUTO_CONTINUE: bool = False
DEFAULT_AUTOLEARN_MIN_TOOL_CALLS: int = 5
VALID_MEMORY_MODELS = frozenset({"online", "local"})

_MEMORY_BACKEND_MNEMOPI = frozenset({"mnemopi", "omp", "bridge"})
_COMPACTION_BACKEND_OMP = frozenset({"omp", "bridge", "mnemopi"})


def memory_backend(cfg: dict | None = None) -> str:
    """Return ``"sqlite"`` (default) or ``"mnemopi"``.

    Precedence ENV > config.json > default (omp port): CHUDWARE_MEMORY_BACKEND
    wins when set; else ``agent.memory_backend``; else the default.
    """
    env = (os.environ.get("CHUDWARE_MEMORY_BACKEND") or "").strip().lower()
    if env:
        return "mnemopi" if env in _MEMORY_BACKEND_MNEMOPI else DEFAULT_MEMORY_BACKEND
    agent = _agent_block(cfg)
    raw = agent.get(MEMORY_BACKEND_KEY)
    if isinstance(raw, str) and raw.strip().lower() in _MEMORY_BACKEND_MNEMOPI:
        return "mnemopi"
    return DEFAULT_MEMORY_BACKEND


def compaction_backend(cfg: dict | None = None) -> str:
    """Return ``"builtin"`` (default) or ``"omp"``.

    Precedence ENV > config.json > default (omp port): CHUDWARE_COMPACTION_BACKEND
    wins when set; else ``agent.compaction_backend``; else the legacy nested
    ``agent.compaction.backend``; else the default.
    """
    env = (os.environ.get("CHUDWARE_COMPACTION_BACKEND") or "").strip().lower()
    if env:
        return "omp" if env in _COMPACTION_BACKEND_OMP else DEFAULT_COMPACTION_BACKEND
    agent = _agent_block(cfg)
    raw = agent.get(COMPACTION_BACKEND_KEY)
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in _COMPACTION_BACKEND_OMP:
            return "omp"
        if s in ("builtin", "local"):
            return DEFAULT_COMPACTION_BACKEND
    # Legacy nested key honored for back-compat (pre-flat-key configs).
    comp = agent.get(COMPACTION_KEY)
    backend = comp.get("backend") if isinstance(comp, dict) else None
    if isinstance(backend, str) and backend.strip().lower() in _COMPACTION_BACKEND_OMP:
        return "omp"
    return DEFAULT_COMPACTION_BACKEND


def _compaction_nested(cfg: dict | None) -> dict:
    """The nested ``agent.compaction`` block ({} when absent/malformed)."""
    comp = _agent_block(cfg).get(COMPACTION_KEY)
    return comp if isinstance(comp, dict) else {}


def compaction_snap_frames(cfg: dict | None = None) -> bool:
    """``agent.compaction.snap_frames`` (bool, default True) — render archived
    history to OMP snap PNG frames when the backend is ``omp`` and the active
    provider has vision. Off = text-only archives even under the omp backend."""
    raw = _compaction_nested(cfg).get("snap_frames")
    return bool(raw) if isinstance(raw, bool) else True


def compaction_snap_dir(cfg: dict | None = None) -> str | None:
    """``agent.compaction.snap_dir`` (str, default None = snap_store.default_dir)."""
    raw = _compaction_nested(cfg).get("snap_dir")
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def compaction_snap_max_frames(cfg: dict | None = None) -> int:
    """``agent.compaction.snap_max_frames`` (int, default 80 = OMP's
    MAX_FRAMES_DEFAULT). Upper bound on rendered frames per snap."""
    try:
        v = _compaction_nested(cfg).get("snap_max_frames")
        return max(1, int(v)) if v is not None else 80
    except (TypeError, ValueError):
        return 80


def cache_warming_enabled(cfg: dict | None = None) -> bool:
    """Return ``agent.cache_warming`` (bool, default False).

    When on, provider prompt caches are kept warm between runs.
    """
    agent = _agent_block(cfg)
    return bool(agent.get(CACHE_WARMING_KEY, DEFAULT_CACHE_WARMING))


def memory_learn_enabled(cfg: dict | None = None) -> bool:
    """Return ``agent.memory_learn`` (bool, default True).

    Learning switch: recall still works when off; nothing new is written.
    """
    agent = _agent_block(cfg)
    return bool(agent.get(MEMORY_LEARN_KEY, DEFAULT_MEMORY_LEARN))


def memory_model(cfg: dict | None = None) -> str:
    """Return ``agent.memory_model`` (``online`` | ``local``, default online).

    OMP ``providers.memoryModel`` port — which tiny/LLM path mnemopi uses for
    fact extraction when the bridge enables LLM (today the bridge runs
    embeddings/llm off and degrades to FTS; the knob is still persisted so the
    UI matches OMP and future bridge options honor it).
    """
    agent = _agent_block(cfg)
    raw = agent.get(MEMORY_MODEL_KEY, DEFAULT_MEMORY_MODEL)
    s = str(raw or DEFAULT_MEMORY_MODEL).strip().lower()
    return s if s in VALID_MEMORY_MODELS else DEFAULT_MEMORY_MODEL


def autolearn_enabled(cfg: dict | None = None) -> bool:
    """Return ``agent.autolearn_enabled`` (OMP Auto-Learn master flag, default off)."""
    agent = _agent_block(cfg)
    return bool(agent.get(AUTOLEARN_ENABLED_KEY, DEFAULT_AUTOLEARN_ENABLED))


def autolearn_auto_continue(cfg: dict | None = None) -> bool:
    """Return ``agent.autolearn_auto_continue`` (capture-at-stop, default off)."""
    agent = _agent_block(cfg)
    return bool(agent.get(AUTOLEARN_AUTO_CONTINUE_KEY, DEFAULT_AUTOLEARN_AUTO_CONTINUE))


def autolearn_min_tool_calls(cfg: dict | None = None) -> int:
    """Return ``agent.autolearn_min_tool_calls`` (default 5, clamped ≥ 1)."""
    agent = _agent_block(cfg)
    try:
        n = int(agent.get(AUTOLEARN_MIN_TOOL_CALLS_KEY, DEFAULT_AUTOLEARN_MIN_TOOL_CALLS))
    except (TypeError, ValueError):
        n = DEFAULT_AUTOLEARN_MIN_TOOL_CALLS
    return max(1, n)


# --------------------------------------------------------------------------- compaction / session cmds
# Shared with host + session_cmds (omp port). Nested under ``agent`` unless noted.
#
#   agent.compaction_strategy ∈ "off"|"snapcompact"|"shake"|"summary"  (default "off")
#   agent.compaction.supersede_reads bool (default true)
#   agent.compaction.drop_useless bool (default true)
#   agent.compaction.keep_recent_tokens int (default 20000)
#   agent.prewalk bool (default false)
#   agent.prewalk_target str (default "@smol")
#   agent.handoff_save_to_disk bool (default false)

COMPACTION_STRATEGY_KEY = "compaction_strategy"
COMPACTION_KEY = "compaction"
PREWALK_KEY = "prewalk"
PREWALK_TARGET_KEY = "prewalk_target"
HANDOFF_SAVE_TO_DISK_KEY = "handoff_save_to_disk"

DEFAULT_COMPACTION_STRATEGY: str = "off"
VALID_COMPACTION_STRATEGIES = frozenset({"off", "snapcompact", "shake", "summary"})
DEFAULT_COMPACTION_SUPERSEDE_READS: bool = True
DEFAULT_COMPACTION_DROP_USELESS: bool = True
DEFAULT_COMPACTION_KEEP_RECENT_TOKENS: int = 20_000
DEFAULT_PREWALK: bool = False
DEFAULT_PREWALK_TARGET: str = "@smol"
DEFAULT_HANDOFF_SAVE_TO_DISK: bool = False


def _agent_block(cfg: dict | None = None) -> dict:
    c = cfg if cfg is not None else load_config()
    if not isinstance(c, dict):
        return {}
    agent = c.get(AGENT_CONFIG_KEY)
    return agent if isinstance(agent, dict) else {}


def get_compaction_strategy(cfg: dict | None = None) -> str:
    """Return ``agent.compaction_strategy`` (default ``\"off\"``).

    Values: ``off`` | ``snapcompact`` | ``shake`` | ``summary``.
    Unknown values fall back to :data:`DEFAULT_COMPACTION_STRATEGY`.
    """
    agent = _agent_block(cfg)
    raw = agent.get(COMPACTION_STRATEGY_KEY, DEFAULT_COMPACTION_STRATEGY)
    s = str(raw or DEFAULT_COMPACTION_STRATEGY).strip().lower()
    return s if s in VALID_COMPACTION_STRATEGIES else DEFAULT_COMPACTION_STRATEGY


def get_compaction_config(cfg: dict | None = None) -> dict:
    """Return normalized ``agent.compaction`` nested settings.

    Keys (with defaults):
      supersede_reads (bool, default True)
      drop_useless (bool, default True)
      keep_recent_tokens (int, default 20000, clamped ≥ 0)
    """
    agent = _agent_block(cfg)
    raw = agent.get(COMPACTION_KEY)
    raw = raw if isinstance(raw, dict) else {}

    def _bool(key: str, default: bool) -> bool:
        if key not in raw:
            return default
        return bool(raw.get(key))

    try:
        keep = int(raw.get("keep_recent_tokens", DEFAULT_COMPACTION_KEEP_RECENT_TOKENS))
    except (TypeError, ValueError):
        keep = DEFAULT_COMPACTION_KEEP_RECENT_TOKENS
    keep = max(0, keep)

    return {
        "supersede_reads": _bool("supersede_reads", DEFAULT_COMPACTION_SUPERSEDE_READS),
        "drop_useless": _bool("drop_useless", DEFAULT_COMPACTION_DROP_USELESS),
        "keep_recent_tokens": keep,
    }


def prewalk_enabled(cfg: dict | None = None) -> bool:
    """Return ``agent.prewalk`` (bool, default False)."""
    agent = _agent_block(cfg)
    return bool(agent.get(PREWALK_KEY, DEFAULT_PREWALK))


def get_prewalk_target(cfg: dict | None = None) -> str:
    """Return ``agent.prewalk_target`` (str, default ``\"@smol\"``)."""
    agent = _agent_block(cfg)
    raw = agent.get(PREWALK_TARGET_KEY, DEFAULT_PREWALK_TARGET)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_PREWALK_TARGET
    return str(raw).strip()


def handoff_save_to_disk(cfg: dict | None = None) -> bool:
    """Return ``agent.handoff_save_to_disk`` (bool, default False)."""
    agent = _agent_block(cfg)
    return bool(agent.get(HANDOFF_SAVE_TO_DISK_KEY, DEFAULT_HANDOFF_SAVE_TO_DISK))


BUG_REPORTS_KEY = "bug_reports"
DEFAULT_BUG_REPORTS = True


def bug_reports_enabled(cfg: dict | None = None) -> bool:
    """Return ``agent.bug_reports`` (bool, default True).

    Settings → AI → Tools "Send bug reports": the agent gets the
    ``submit_bug_report`` tool and may file a local markdown report
    (Documents/Chudware/Bug-Reports/) when it hits tool errors that look
    like our bug. Reports never leave the machine.
    """
    agent = _agent_block(cfg)
    raw = agent.get(BUG_REPORTS_KEY, DEFAULT_BUG_REPORTS)
    return bool(raw) if isinstance(raw, bool) else DEFAULT_BUG_REPORTS


def bugreport_floor(cfg: dict | None = None) -> frozenset:
    """Tool names unioned onto every advertised/allowed list when bug reports
    are on (empty frozenset when off) — the Settings toggle is the gate."""
    return frozenset({"submit_bug_report"}) if bug_reports_enabled(cfg) else frozenset()






DEFAULT_LOCAL_MODEL = "gemma-4-e4b"
DEFAULT_LOCAL_BASE_URL = "http://localhost:1234/v1"


# --------------------------------------------------------------------------- offline mode (C35)
# LOCAL-ONLY / OFFLINE MODE — the ITAR-compliance feature. When config.json "offline_mode" is
# true NO technical data may leave the machine: only providers whose endpoint is a loopback host
# (localhost / 127.0.0.1 / ::1) are allowed; cloud providers (Anthropic, OpenAI) are refused
# outright. Enforced here at provider resolution AND re-asserted in the agent loop before every
# run (defense in depth). The error always names the setting so the user knows what to change.

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}
OFFLINE_SETTING = "offline_mode"


def offline_enabled(cfg: dict | None = None) -> bool:
    cfg = cfg if cfg is not None else load_config()
    return bool(cfg.get(OFFLINE_SETTING, False))

# --------------------------------------------------------------------------- compliance mode (WS3)
# ITAR-class outbound redaction. config.json "compliance_mode":
#   "off"        — no wrapping (default)
#   "redact"     — wrap cloud providers: deterministic + optional local-LLM scrub,
#                  rehydrate inbound; images blocked outbound
#   "local_only" — refuse ALL non-loopback providers (same enforcement as offline_mode)
# offline_mode remains the legacy hard switch and still forces local-only refusal;
# compliance_mode=local_only is the named WS3 equivalent. See docs/REDACTION_DESIGN.md.

COMPLIANCE_SETTING = "compliance_mode"
_VALID_COMPLIANCE_MODES = {"off", "redact", "local_only"}


def compliance_mode(cfg: dict | None = None) -> str:
    """Return normalized compliance_mode: ``off`` | ``redact`` | ``local_only``."""
    cfg = cfg if cfg is not None else load_config()
    raw = cfg.get(COMPLIANCE_SETTING, "off")
    s = str(raw or "off").strip().lower().replace("-", "_")
    if s in ("localonly", "local"):
        return "local_only"
    return s if s in _VALID_COMPLIANCE_MODES else "off"


def local_only_enabled(cfg: dict | None = None) -> bool:
    """True when non-loopback providers must be refused (offline_mode OR local_only)."""
    cfg = cfg if cfg is not None else load_config()
    if offline_enabled(cfg):
        return True
    return compliance_mode(cfg) == "local_only"



_VALID_CONSENT_TIERS = {"off", "meta", "full"}


def consent_tier(cfg: dict) -> str:
    """Return the telemetry consent tier for the given config.

    - "off"  — no telemetry uploaded (events still written locally)
    - "meta" — metadata only (timings, model name, hashes; no geometry)
    - "full" — full telemetry including geometry references

    Offline mode hard-overrides to "off" regardless of any configured value.
    Unknown values default to "off" (external-default, safe).
    """
    if cfg.get(OFFLINE_SETTING):
        return "off"
    tier = cfg.get("telemetry_consent", "off")
    return tier if tier in _VALID_CONSENT_TIERS else "off"


def _host_is_loopback(base_url: str | None) -> bool:
    """True if *base_url*'s host is a loopback address (the only egress allowed offline)."""
    if not base_url:
        return False
    host = (urlparse(base_url).hostname or "").lower()
    return host in _LOOPBACK_HOSTS


class OfflineModeError(RuntimeError):
    """Raised when offline mode would be violated by the resolved provider/endpoint."""


def provider_offline_safe(provider: Provider) -> bool:
    """True if *provider* keeps all traffic on the local machine (loopback endpoint).

    Cloud SDK providers (Anthropic, OpenAI cloud) talk to remote APIs and are never offline-safe.
    A local OpenAI-compatible endpoint (LM Studio etc.) is safe only when its base_url is loopback.
    The mock provider is always safe (it makes no network calls — used by tests).
    """
    # A provider may explicitly declare it makes no network calls (test doubles, in-process
    # backends). This is the general, name-independent escape hatch for offline-safety.
    if getattr(provider, "offline_safe", False):
        return True
    name = getattr(provider, "name", "")
    if name in ("mock", "mock-roles", "base"):
        return True
    if name in ("anthropic", "gemini", "openrouter", "xai",
                "anthropic-sub", "codex-sub", "cursor-sub", "xai-sub", "kimi-sub",
                "claude-cli", "codex-cli"):
        return False  # always cloud (incl. subscription-auth + CLI-subscription providers)
    base_url = getattr(provider, "base_url", None)
    if name == "openai":
        # No base_url means the real OpenAI cloud endpoint; otherwise check the host.
        return _host_is_loopback(base_url)
    if name == "llamacpp":
        return _host_is_loopback(base_url or DEFAULT_LOCAL_BASE_URL)
    if name == "vllm":
        # vLLM serves LoRA adapters locally (T-H9); offline-safe only when its endpoint is
        # loopback — same rule as openai/llamacpp, NOT the never-offline cloud set.
        return _host_is_loopback(base_url or "http://localhost:8000/v1")
    # Unknown provider with a loopback base_url is allowed; anything else is refused.
    return _host_is_loopback(base_url)


def _catalog_caps(provider: str, model: str, cfg: dict) -> dict:
    """Resolve context/vision/effort options from the model catalog.

    User config overrides (context_window, max_output_tokens, nested *.vision,
    reasoning_efforts) always win. Network is NOT used here — only the on-disk
    models.dev cache (if present) + heuristics. Live discovery is owned by the
    on-connect probe (agent_list_models / discover_models).
    """
    try:
        return model_catalog.resolve_capabilities(provider, model or "", cfg)
    except Exception:  # noqa: BLE001 — never break provider construction
        return {
            "context": 0,
            "max_output": 0,
            "vision": True,
            "tools": True,
            "reasoning_efforts": [],
            "auto_source": "heuristic",
            "label": model or "",
        }


# model_catalog's `auto_source` vocabulary, verified by reading model_catalog.py:
#   "user"        — a user config override supplied the number (resolve_capabilities:711-739)
#   "live"        — a live /models (or Gemini) endpoint probe reported it (:262-276, :328)
#   "models.dev"  — the curated models.dev catalog row (:414)
#   "heuristic"   — a hardcoded per-provider / static-allowlist constant (:345, :653)
#   "+"-joined composites — _merge_row (:453-482) and merge_catalog (:531) build e.g.
#                   "live+models.dev", "live+heuristic", "models.dev+heuristic".
# The GUI trust order is user > catalog > probe > heuristic, so a composite must be
# reported as its LEAST-trusted component: "live+heuristic" is only as good as the
# heuristic part. Anything unrecognised also degrades to "heuristic" rather than
# guessing upward — a fabricated provenance is worse than an honest "unknown".
_WINDOW_SOURCE_MAP = {
    "user": "user",
    "models.dev": "catalog",
    "live": "probe",
    "heuristic": "heuristic",
}
# Least-trusted first: the first rank present in a composite wins.
_WINDOW_SOURCE_RANK = ("heuristic", "probe", "catalog", "user")


def _window_source_from_caps(caps: dict) -> str:
    """Map ``caps["auto_source"]`` onto the frozen GUI set {user,catalog,probe,heuristic}.

    Composites collapse to their least-trusted component; unknown or missing values
    collapse to "heuristic" so the meter can never present a guess as a measurement.
    """
    raw = str(caps.get("auto_source") or "")
    mapped = {
        _WINDOW_SOURCE_MAP[part]
        for part in (p.strip().lower() for p in raw.split("+"))
        if part in _WINDOW_SOURCE_MAP
    }
    for rank in _WINDOW_SOURCE_RANK:
        if rank in mapped:
            return rank
    return "heuristic"


def _attach_catalog_meta(provider: Provider, caps: dict) -> Provider:
    """Stamp resolved catalog fields onto the provider for downstream consumers."""
    # A GUESS is the one thing worth replacing. When model_catalog admits it only
    # had a heuristic (no user override, no curated row, no live probe) and the
    # provider is served by the OMP sidecar, the sidecar's own curated row is
    # strictly better information and is exactly what the GUI already trusts.
    # Live 2026-07-29: kimi-code/k3 resolved to the LOCAL default context=8192
    # (real 1,048,576), max_output=0 (real 131,072), vision=False (k3 takes
    # images), so the context meter measured a 1M window against an 8192 ruler.
    # Never touches "user"/"catalog"/"probe" — an override must stay an override.
    if _window_source_from_caps(caps) == "heuristic":
        try:
            from .providers import omp_bridge_provider as _obp  # noqa: PLC0415

            if isinstance(provider, _obp.OmpBridgeProvider):
                real = _obp.catalog_caps(getattr(provider, "name", ""),
                                         getattr(provider, "model", ""))
                if real:
                    caps = {**caps, **real, "auto_source": "models.dev"}
        except Exception:  # noqa: BLE001 — never break provider construction
            pass
    try:
        provider.context_window = int(caps.get("context") or 0) or None  # type: ignore[attr-defined]
        provider.max_output_tokens = int(caps.get("max_output") or 0) or None  # type: ignore[attr-defined]
        provider.reasoning_efforts = list(caps.get("reasoning_efforts") or [])  # type: ignore[attr-defined]
        provider.catalog_auto_source = str(caps.get("auto_source") or "")  # type: ignore[attr-defined]
        provider.vision = bool(caps.get("vision"))  # type: ignore[attr-defined]
        # Provenance of context_window, mapped onto the frozen GUI vocabulary: a
        # heuristic per-provider constant must not outrank a curated per-model row.
        provider.context_window_source = _window_source_from_caps(caps)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    return provider



def assert_offline_safe(provider: Provider, cfg: dict | None = None) -> None:
    """Defense-in-depth guard the agent loop calls before every run when offline mode is set.

    No-op when offline mode is off. When on, raises :class:`OfflineModeError` (naming the setting)
    if *provider* is not offline-safe.
    """
    if not offline_enabled(cfg):
        return
    if not provider_offline_safe(provider):
        name = getattr(provider, "name", "?")
        base = getattr(provider, "base_url", None) or "(cloud endpoint)"
        raise OfflineModeError(
            f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the resolved provider "
            f"{name!r} -> {base} is NOT local-only, so the run is refused. Switch to a local "
            f"(loopback) model server or disable offline mode in Settings."
        )


def get_provider(name: str | None = None, cfg: dict | None = None,
                 role: str | None = None) -> Provider:
    """Resolve the configured provider, enforcing offline/local-only at two points: cloud
    providers are refused by name BEFORE construction, and the resolved provider is re-checked
    against ``provider_offline_safe`` AFTER (catches the empty-name local default + edge cases).

    *role* (optional) is a routing.yaml role tag (e.g. ``"overseer"``, ``"fixer"``, or a DAG
    node name) — see ``_resolve_via_routing``. When *name* / ``CHUDWARE_PROVIDER`` is not given,
    routing.yaml's role->source assignment (contract §14) is consulted BEFORE falling back to
    config.json's flat ``provider`` field, so a source registered into routing.yaml (e.g. the
    bundled llama.cpp runtime's ``register_routing_source``) actually gets picked up here.

    When ``compliance_mode == "redact"``, the resolved provider is wrapped in
    :class:`services.agent.redaction.RedactingProvider` so outbound text is scrubbed and
    inbound replies rehydrated. ``local_only`` (and legacy ``offline_mode``) refuse every
    non-loopback provider before the wrap.
    """
    cfg = cfg if cfg is not None else load_config()
    # local_only / offline: refuse non-loopback via the existing offline assert path.
    # For local_only without offline_mode, temporarily treat as offline for the assert.
    provider = _resolve_provider(name, cfg, role=role)
    if local_only_enabled(cfg):
        # Reuse offline assert machinery; synthesize a cfg that trips offline_enabled.
        if offline_enabled(cfg):
            assert_offline_safe(provider, cfg)
        else:
            assert_offline_safe(provider, {**cfg, OFFLINE_SETTING: True})
    else:
        assert_offline_safe(provider, cfg)

    # WS3 redact wrap — applied AFTER offline/local-only gates so we never wrap a
    # provider that should have been refused.
    if compliance_mode(cfg) == "redact":
        from .redaction import wrap_provider
        provider = wrap_provider(provider, cfg=cfg)
    return provider


# Routing-source `kind` -> provider `name` understood by `_resolve_provider` below.
# lmstudio/ollama have no dedicated provider class: both speak the OpenAI-compatible
# /v1/chat/completions protocol, so they route through the generic "openai" provider
# with the source's own base_url.
_ROUTING_KIND_TO_PROVIDER_NAME = {
    "anthropic": "anthropic",
    "openai": "openai",
    "openrouter": "openrouter",
    "gemini": "gemini",
    "xai": "xai",
    "llamacpp": "llamacpp",
    "vllm": "vllm",
    "mock": "mock",
    "lmstudio": "openai",
    "ollama": "openai",
    "claude-cli": "claude-cli",
    "codex-cli": "codex-cli",
}

# cfg sub-key each provider name reads its base_url from (see _resolve_provider below).
_PROVIDER_BASE_URL_CFG_KEY = {
    "openai": "openai",
    "llamacpp": "llamacpp",
    "vllm": "vllm",
    "openrouter": "openrouter",
}

# Provider ids served by the OMP bun sidecar (vendor/omp/bridge/src/bridge.ts) instead of a
# native Python SDK adapter. Kept verbatim in sync with the C++ source of truth,
# core/agent/providers.cpp:1131-1146 (omp_bridge_provider_ids()) — re-sync by copying that
# set over whenever the vendored OMP catalog is re-pulled and provider coverage shifts.
#
# It deliberately EXCLUDES every id CAD serves natively (openai, gemini, xai/xai-sub,
# moonshot, cursor-sub, llamacpp, vllm, mock), so routing a bridge id can never shadow a
# loopback endpoint or an already-working native adapter.
OMP_BRIDGE_PROVIDER_IDS = frozenset({
    "aimlapi", "alibaba-coding-plan", "amazon-bedrock", "anthropic", "anthropic-sub",
    "azure", "baseten", "cerebras",
    "cloudflare-ai-gateway", "coreweave", "deepseek", "devin", "firepass", "fireworks",
    "github-copilot", "gitlab-duo", "gitlab-duo-agent", "google-antigravity",
    "google-gemini-cli", "google-vertex", "groq", "huggingface", "kilo", "kimi-code",
    "minimax", "minimax-cn", "minimax-code", "minimax-code-cn", "mistral", "nanogpt",
    "novita", "nvidia", "ollama-cloud", "openai-codex", "opencode", "opencode-go",
    "opencode-zen", "openrouter", "qianfan", "qwen-portal", "sakana", "synthetic",
    "together", "umans", "venice", "vercel-ai-gateway", "wafer-serverless", "xiaomi",
    "xiaomi-token-plan-ams", "xiaomi-token-plan-cn", "xiaomi-token-plan-sgp", "zai",
    "zenmux", "zhipu-coding-plan",
})


# Persisted provider ids that were renamed to a bridge id. _resolve_provider normalizes the
# name through this table, but a config written before the rename still files the nested
# section and the keys map under the OLD id — so per-provider lookups try both, newest first.
# core/agent/config.cpp performs the same kimi-sub -> kimi-code rename on its side.
_PROVIDER_ID_ALIASES = {"kimi-sub": "kimi-code"}


def _provider_cfg_names(name: str) -> tuple[str, ...]:
    """Config ids *name*'s settings may be filed under: the current id, then pre-rename aliases."""
    return (name, *(old for old, new in _PROVIDER_ID_ALIASES.items() if new == name))


def _provider_cfg_section(cfg: dict, name: str) -> dict:
    """Nested per-provider config block for *name* as a plain copy (empty dict when unset)."""
    for key in _provider_cfg_names(name):
        section = cfg.get(key)
        if isinstance(section, dict):
            return dict(section)
    return {}


def _openrouter_api_key(cfg: dict) -> str | None:
    """Every place an OpenRouter credential may live, newest-wins order.

    OpenRouter has exactly ONE credential form in CAD: an API key. The blessed PKCE
    login (providers/oauth_pkce.py) mints a key and stores it like any other, so unlike
    Claude Pro/Kimi-For-Coding there is no OAuth-only sign-in that could authenticate a
    keyless client. Single source of truth for both the native adapter branch and the
    OMP-bridge branch's keyless refusal below.
    """
    oc = cfg.get("openrouter")
    keys_map = cfg.get("keys")
    return (
        (oc.get("api_key") if isinstance(oc, dict) else None)
        or cfg.get("api_key")
        or cfg.get("openrouter_api_key")
        or (keys_map.get("openrouter") if isinstance(keys_map, dict) else None)
        or os.environ.get("OPENROUTER_API_KEY")
    )


def _refuse_keyless_openrouter() -> None:
    """mega-6: never construct a keyless OpenRouter client — complete would 401 as
    provider_auth and kill every depth-0 auto_pick free leaf."""
    raise ValueError(
        "openrouter provider requires an API key "
        "(config openrouter.api_key / keys.openrouter / OPENROUTER_API_KEY); "
        "auto_pick must skip keyless openrouter"
    )


def _resolve_via_routing(role: str | None, cfg: dict) -> tuple[str, str | None, str | None] | None:
    """Resolve *role* against routing.yaml (contract §14). Returns
    ``(provider_name, model_id, base_url)`` or ``None`` when routing.yaml has no
    configured sources / can't resolve the role — the caller falls back to
    config.json in that case. Never raises: routing.yaml is a best-effort,
    user-editable file and a malformed/missing one must not break provider
    resolution."""
    try:
        from .routing import load_routing  # noqa: PLC0415 — avoid import cost/cycles
        # Re-parse routing.yaml from disk each call (not the process-lifetime cache
        # in get_routing_table()) — the file is user-editable and can also be mutated
        # at runtime by local_runtime.register_routing_source(), so a stale cache would
        # silently ignore a source registered after the first resolution in-process.
        table = load_routing()
        resolution = table.resolve(role or "default")
        if resolution is None:
            return None
        source = table.get_source(resolution.source_id)
        kind = source.kind if source is not None else ""
        provider_name = _ROUTING_KIND_TO_PROVIDER_NAME.get(kind)
        if provider_name is None:
            return None
        base_url = source.base_url if source is not None else None
        return provider_name, resolution.model_id, base_url
    except Exception:  # noqa: BLE001 — routing.yaml must never block provider resolution
        return None


def _resolve_provider(name: str | None, cfg: dict, role: str | None = None) -> Provider:
    routed_model: str | None = None
    routed_base_url: str | None = None
    if not name and not os.environ.get("CHUDWARE_PROVIDER"):
        routed = _resolve_via_routing(role, cfg)
        if routed is not None:
            name, routed_model, routed_base_url = routed

    if routed_base_url or routed_model:
        # Overlay the routing.yaml decision onto a shallow cfg copy so the existing
        # per-provider branches below (which read cfg.get("model") / cfg[kind]["base_url"])
        # pick it up without needing their own routing-awareness.
        cfg = dict(cfg)
        if routed_model:
            cfg["model"] = routed_model
        sub_key = _PROVIDER_BASE_URL_CFG_KEY.get((name or "").lower())
        if routed_base_url and sub_key:
            cfg[sub_key] = dict(cfg.get(sub_key) or {})
            cfg[sub_key].setdefault("base_url", routed_base_url)

    name = (name or os.environ.get("CHUDWARE_PROVIDER") or cfg.get("provider") or "").lower()
    # Pre-rename persisted ids (kimi-sub -> kimi-code) normalize before every lookup below,
    # exactly as core/agent/config.cpp's resolve_provider does on its side.
    name = _PROVIDER_ID_ALIASES.get(name, name)
    offline = offline_enabled(cfg) or compliance_mode(cfg) == "local_only"

    # Offline mode: refuse cloud providers OUTRIGHT before constructing them (no client, no key
    # lookup). A local OpenAI-compatible endpoint is checked against its base_url below.
    if offline and name == "gemini":
        raise OfflineModeError(
            f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the Google Gemini "
            f"cloud provider is refused. Use a local (loopback) model server instead.")
    if offline and name == "openrouter":
        raise OfflineModeError(
            f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the OpenRouter "
            f"cloud provider is refused. Use a local (loopback) model server instead.")
    if offline and name == "xai":
        raise OfflineModeError(
            f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the xAI "
            f"cloud provider is refused. Use a local (loopback) model server instead.")
    if offline and name == "moonshot":
        raise OfflineModeError(
            f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the Moonshot (Kimi) "
            f"cloud provider is refused. Use a local (loopback) model server instead.")
    if offline and name in ("anthropic", "openai") and name != "":
        if name == "anthropic":
            raise OfflineModeError(
                f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the Anthropic "
                f"cloud provider is refused. Use a local (loopback) model server instead.")
        # openai: only allowed if a loopback base_url is configured.
        if not _host_is_loopback((cfg.get("openai", {}) or {}).get("base_url")):
            raise OfflineModeError(
                f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the OpenAI cloud "
                f"provider is refused. Configure a local (loopback) base_url, or disable offline "
                f"mode in Settings.")
    if offline and name in ("llamacpp", "llama.cpp", "llama-cpp"):
        lcfg = dict(cfg.get("llamacpp", {}) or {})
        host = str(lcfg.get("host") or "127.0.0.1")
        port = int(lcfg.get("port") or 1234)
        base_url = lcfg.get("base_url") or f"http://{host}:{port}/v1"
        if not _host_is_loopback(base_url):
            raise OfflineModeError(
                f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the managed "
                f"llama.cpp endpoint {base_url!r} is not loopback, so it is refused before "
                f"probing the server.")
    if offline and name == "vllm":
        vc = dict(cfg.get("vllm", {}) or {})
        base_url = vc.get("base_url") or "http://localhost:8000/v1"
        if not _host_is_loopback(base_url):
            raise OfflineModeError(
                f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the vLLM serving "
                f"endpoint {base_url!r} is not loopback, so it is refused before construction.")
    if offline and name in ("anthropic-sub", "codex-sub", "cursor-sub", "xai-sub",
                            "claude-cli", "codex-cli"):
        # Subscription-auth providers (§4.4) and CLI-subscription drivers always talk to
        # a vendor cloud endpoint (the CLI is just a local front-end).
        raise OfflineModeError(
            f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the subscription "
            f"provider {name!r} uses a vendor cloud endpoint and is refused. Use a local "
            f"(loopback) model server instead.")
    if offline and name in OMP_BRIDGE_PROVIDER_IDS:
        # The OMP sidecar is a local process, but every id it serves is a vendor cloud
        # endpoint it calls on our behalf — refuse by name, before any child is spawned.
        raise OfflineModeError(
            f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the cloud provider "
            f"{name!r} is routed through the OMP sidecar to a vendor API endpoint and is "
            f"refused. Use a local (loopback) model server instead.")

    if not name:
        # Nothing configured: default to a local LM Studio (OpenAI-compatible) endpoint
        # running gemma-4-e4b, so the app is testable out of the box on this machine.
        from .providers.openai_provider import OpenAIProvider

        oai = cfg.get("openai", {}) or {}
        model = cfg.get("model") or DEFAULT_LOCAL_MODEL
        caps = _catalog_caps("openai", model, cfg)
        # Nested openai.vision wins when set; else catalog (local defaults conservative).
        vision = oai["vision"] if "vision" in oai else caps.get("vision", True)
        prov = OpenAIProvider(
            api_key=cfg.get("api_key") or os.environ.get("OPENAI_API_KEY") or "lm-studio",
            model=model,
            base_url=oai.get("base_url") or DEFAULT_LOCAL_BASE_URL,
            supports_vision=bool(vision),
            timeout=float(oai.get("timeout", 120.0)),
        )
        return _attach_catalog_meta(prov, caps)


    if name == "mock":
        # Deterministic mock provider for tests (no API key, no network). Scripted replies load
        # from the JSON file named by CHUDWARE_MOCK_REPLIES (a list[str], or a {role: list[str]} map
        # consumed by a role-sniffing provider) — keeps the subprocess worker test self-contained.
        from .providers.mock_provider import MockProvider

        replies: list[str] = []
        by_role: dict | None = None
        path = os.environ.get("CHUDWARE_MOCK_REPLIES")
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                by_role = data
            elif isinstance(data, list):
                replies = data
        if by_role is not None:
            from .providers.mock_provider import RoleScriptedProvider
            return RoleScriptedProvider(by_role)
        return MockProvider(replies or [""])

    if name in OMP_BRIDGE_PROVIDER_IDS:
        # Cloud providers served by the OMP bun sidecar instead of a raw vendor SDK: the
        # sidecar owns CAD's shared credential store, so an OAuth-only subscription (Claude
        # Pro/Max, Kimi For Coding, …) completes here with no api_key at all, and no pip
        # package has to be installed for the provider to work. Deliberately ahead of the
        # native cloud branches below so a bridge id wins over a same-named SDK adapter.
        from .providers.omp_bridge_provider import OmpBridgeProvider

        aliases = _provider_cfg_names(name)
        pc = _provider_cfg_section(cfg, name)
        keys_map = cfg.get("keys") if isinstance(cfg.get("keys"), dict) else {}
        # cfg["api_key"] is the flat single-provider slot the native branches read; it belongs
        # to whoever cfg["provider"] names, and handing another provider's key to the sidecar
        # would suppress its AuthStorage (OAuth) lookup entirely. Vendor env vars are left to
        # the sidecar, which consults them itself.
        flat_provider = str(cfg.get("provider") or "").lower()
        flat_key = cfg.get("api_key") if flat_provider in ("", *aliases) else None
        key = (
            pc.get("api_key")
            or flat_key
            or next((keys_map[k] for k in aliases if keys_map.get(k)), None)
        )
        if name == "openrouter":
            # mega-6, re-armed: openrouter joined OMP_BRIDGE_PROVIDER_IDS, which shadows the
            # native branch below and with it the keyless refusal. The sidecar's "no apiKey
            # -> resolve from AuthStorage" fallback only rescues OAuth subscriptions;
            # OpenRouter has no OAuth-only form (see _openrouter_api_key), so a keyless
            # construction here is a guaranteed 401 deferred to request time.
            # The legacy flat slot is forwarded too, so the credential we actually send and
            # the guard agree. Env stays the sidecar's own job.
            key = key or cfg.get("openrouter_api_key")
            if not _openrouter_api_key(cfg):
                _refuse_keyless_openrouter()
        model = cfg.get("model") or pc.get("model") or ""
        caps = _catalog_caps(name, model, cfg)
        vision = pc["vision"] if "vision" in pc else caps.get("vision", True)
        prov = OmpBridgeProvider(
            provider=name,
            model=model,
            api_key=key,
            base_url=pc.get("base_url"),
            reasoning_effort=cfg.get("reasoning_effort"),
            supports_vision=bool(vision),
            timeout=pc.get("timeout"),
        )
        return _attach_catalog_meta(prov, caps)

    if name == "anthropic":
        from .providers.anthropic_provider import AnthropicProvider, DEFAULT_MODEL

        key = cfg.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")
        return AnthropicProvider(
            api_key=key,
            model=cfg.get("model", DEFAULT_MODEL),
            reasoning_effort=cfg.get("reasoning_effort"),
        )

    if name == "gemini":
        from .providers.gemini_provider import GeminiProvider, DEFAULT_MODEL

        key = cfg.get("api_key") or os.environ.get("GEMINI_API_KEY") or os.environ.get(
            "GOOGLE_API_KEY")
        model = cfg.get("model", DEFAULT_MODEL)
        caps = _catalog_caps("gemini", model, cfg)
        # Effort preference stays user-owned; option set is catalog-derived.
        prov = GeminiProvider(api_key=key, model=model,
                              reasoning_effort=cfg.get("reasoning_effort"))
        return _attach_catalog_meta(prov, caps)

    if name == "openai":
        from .providers.openai_provider import OpenAIProvider, DEFAULT_MODEL

        key = cfg.get("api_key") or os.environ.get("OPENAI_API_KEY")
        oai = cfg.get("openai", {}) or {}
        model = cfg.get("model", DEFAULT_MODEL)
        caps = _catalog_caps("openai", model, cfg)
        vision = oai["vision"] if "vision" in oai else caps.get("vision", True)
        prov = OpenAIProvider(api_key=key, model=model,
                              base_url=oai.get("base_url"),
                              supports_vision=bool(vision),
                              timeout=float(oai.get("timeout", 120.0)),
                              reasoning_effort=cfg.get("reasoning_effort"))
        return _attach_catalog_meta(prov, caps)

    if name == "openrouter":
        # OpenAI-compatible endpoint pinned to OpenRouter. Key is BYO (OPENROUTER_API_KEY)
        # or minted by the blessed PKCE login and stored as api_key (contract §4.1).
        # mega-16: also honour config.keys.openrouter (GUI keys map).
        from .providers.openrouter_provider import OpenRouterProvider, DEFAULT_MODEL

        oc = cfg.get("openrouter") or {}
        key = _openrouter_api_key(cfg)
        if not key:
            _refuse_keyless_openrouter()
        model = cfg.get("model", DEFAULT_MODEL)
        caps = _catalog_caps("openrouter", model, cfg)
        vision = (
            oc["vision"] if isinstance(oc, dict) and "vision" in oc
            else caps.get("vision", True)
        )
        timeout = float(oc.get("timeout", 120.0)) if isinstance(oc, dict) else 120.0
        prov = OpenRouterProvider(api_key=key, model=model,
                                  supports_vision=bool(vision),
                                  timeout=timeout,
                                  reasoning_effort=cfg.get("reasoning_effort"))
        return _attach_catalog_meta(prov, caps)

    if name == "xai":
        # Classic API-key path (console.x.ai). OpenAI-compatible at https://api.x.ai/v1.
        from .providers.xai_provider import XAIProvider, DEFAULT_MODEL, BASE_URL

        key = cfg.get("api_key") or os.environ.get("XAI_API_KEY")
        xc = cfg.get("xai", {}) or {}
        model = cfg.get("model", DEFAULT_MODEL)
        caps = _catalog_caps("xai", model, cfg)
        vision = xc["vision"] if "vision" in xc else caps.get("vision", True)
        prov = XAIProvider(
            api_key=key,
            model=model,
            supports_vision=bool(vision),
            timeout=float(xc.get("timeout", 120.0)),
            base_url=xc.get("base_url") or BASE_URL,
            reasoning_effort=cfg.get("reasoning_effort"),
        )
        return _attach_catalog_meta(prov, caps)

    if name == "moonshot":
        # Moonshot AI (Kimi K3) — OpenAI-compatible at https://api.moonshot.ai/v1.
        # Key precedence mirrors openrouter: cfg.moonshot.api_key / cfg.api_key /
        # keys.moonshot / MOONSHOT_API_KEY.
        from .providers.moonshot_provider import MoonshotProvider, DEFAULT_MODEL, BASE_URL

        mc = cfg.get("moonshot", {}) or {}
        keys_map = cfg.get("keys") or {}
        key = (
            (mc.get("api_key") if isinstance(mc, dict) else None)
            or cfg.get("api_key")
            or (keys_map.get("moonshot") if isinstance(keys_map, dict) else None)
            or os.environ.get("MOONSHOT_API_KEY")
        )
        if not key:
            raise ValueError(
                "moonshot provider requires an API key "
                "(config moonshot.api_key / keys.moonshot / MOONSHOT_API_KEY)")
        model = cfg.get("model", DEFAULT_MODEL)
        caps = _catalog_caps("moonshot", model, cfg)
        vision = mc["vision"] if isinstance(mc, dict) and "vision" in mc else caps.get("vision", True)
        prov = MoonshotProvider(
            api_key=key,
            model=model,
            supports_vision=bool(vision),
            timeout=float(mc.get("timeout", 300.0)) if isinstance(mc, dict) else 300.0,
            base_url=mc.get("base_url") or BASE_URL,
            reasoning_effort=cfg.get("reasoning_effort"),
        )
        return _attach_catalog_meta(prov, caps)


    if name == "anthropic-sub":
        # Subscription auth (§4.4): uses the user's Claude Pro/Max login, not an api_key.
        from .providers.anthropic_sub_provider import AnthropicSubProvider, DEFAULT_MODEL

        return AnthropicSubProvider(model=cfg.get("model", DEFAULT_MODEL))

    if name == "codex-sub":
        # Subscription auth (§4.4): uses the user's ChatGPT Plus/Pro login.
        from .providers.codex_sub_provider import CodexSubProvider, DEFAULT_MODEL

        return CodexSubProvider(model=cfg.get("model", DEFAULT_MODEL))

    if name == "cursor-sub":
        # Subscription auth (§4.4): uses the user's Cursor Pro login.
        from .providers.cursor_sub_provider import CursorSubProvider, DEFAULT_MODEL

        return CursorSubProvider(model=cfg.get("model", DEFAULT_MODEL))

    if name == "xai-sub":
        # Subscription auth (§4.4): SuperGrok / X Premium+ device-code OAuth.
        from .providers.xai_sub_provider import XAISubProvider, DEFAULT_MODEL

        return XAISubProvider(model=cfg.get("model", DEFAULT_MODEL))

    if name == "claude-cli":
        # Headless Claude Code CLI — subscription-backed, never offline-safe.
        from .providers.claude_cli_provider import ClaudeCliProvider, DEFAULT_MODEL

        cc = dict(cfg.get("claude_cli", {}) or {})
        return ClaudeCliProvider(
            model=cfg.get("model") or cc.get("model") or DEFAULT_MODEL,
            binary=cc.get("binary") or "claude",
            timeout=float(cc.get("timeout", 300.0)),
        )

    if name == "codex-cli":
        # Headless Codex CLI — subscription-backed, never offline-safe.
        from .providers.codex_cli_provider import CodexCliProvider, DEFAULT_MODEL

        cc = dict(cfg.get("codex_cli", {}) or {})
        return CodexCliProvider(
            model=cfg.get("model") or cc.get("model") or DEFAULT_MODEL,
            binary=cc.get("binary") or "codex",
            timeout=float(cc.get("timeout", 300.0)),
        )
    if name in ("llamacpp", "llama.cpp", "llama-cpp"):
        from .providers.llamacpp_provider import LlamaCppProvider, DEFAULT_MODEL

        lcfg = dict(cfg.get("llamacpp", {}) or {})
        lcfg.setdefault("model", cfg.get("model") or DEFAULT_MODEL)
        # Keep the endpoint explicit in the provider object so offline-safety and diagnostics name it.
        lcfg.setdefault("base_url", DEFAULT_LOCAL_BASE_URL)
        return LlamaCppProvider(lcfg, model=lcfg["model"])

    if name == "vllm":
        # T-H9: per-task LoRA serving. OpenAI-compatible vLLM instance; the served LoRA is
        # picked by the `adapter` field. Enters as just another providers/1 adapter.
        from .providers.vllm_provider import (VLLMProvider, DEFAULT_BASE_URL,
                                              DEFAULT_MODEL as VLLM_DEFAULT_MODEL)

        vc = dict(cfg.get("vllm", {}) or {})
        model = vc.get("model") or cfg.get("model") or VLLM_DEFAULT_MODEL
        caps = _catalog_caps("vllm", model, cfg)
        vision = vc["vision"] if "vision" in vc else caps.get("vision", False)
        prov = VLLMProvider(
            base_model=model,
            adapter=vc.get("adapter"),
            api_key=cfg.get("api_key") or os.environ.get("OPENAI_API_KEY"),
            base_url=vc.get("base_url") or DEFAULT_BASE_URL,
            supports_vision=bool(vision),
            timeout=float(vc.get("timeout", 120.0)),
        )
        return _attach_catalog_meta(prov, caps)


    raise ValueError(f"Unknown provider: {name!r}")
