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

import copy
import json
import os
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


def save_config(cfg: dict) -> None:
    """Read-merge-write a partial update into config.json (creating it on first run).

    Deep-merges into whatever is already on disk — recursing into dict-valued keys,
    replacing scalars/lists outright — so one caller (e.g. a first-run consent prompt
    setting telemetry_consent) never clobbers keys another caller owns (provider/api_key/
    etc.), including nested siblings under a shared parent key (e.g. {"keys": {...}}) —
    the same non-destructive contract the C++ GUI's first-run dialog follows when it
    writes this file directly (ui/imgui/src/ui/first_run.cpp).
    """
    current = load_config()
    _deep_merge(current, cfg)
    paths.CONFIG.parent.mkdir(parents=True, exist_ok=True)
    paths.CONFIG.write_text(json.dumps(current, indent=2), encoding="utf-8")


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
DEFAULT_PLAN_PREFERENCE = "encourage"
DEFAULT_SUBAGENT_PREFERENCE = "option"
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
    """Return plan preference string (default encourage). Honors legacy plan_mode bool."""
    c = cfg if cfg is not None else load_config()
    if not isinstance(c, dict):
        return DEFAULT_PLAN_PREFERENCE
    if "plan_preference" in c:
        return _parse_preference(c.get("plan_preference"), DEFAULT_PLAN_PREFERENCE)
    agent = c.get(AGENT_CONFIG_KEY) if isinstance(c.get(AGENT_CONFIG_KEY), dict) else {}
    if isinstance(agent, dict) and "plan_preference" in agent:
        return _parse_preference(agent.get("plan_preference"), DEFAULT_PLAN_PREFERENCE)
    # Legacy bool
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
    "advisor", "cad", "cam", "ecad", "em", "gsl", "intake", "ledger",
    "mission", "parts", "plm", "recon", "research", "reveng", "sim",
    "workspace",
)


# ---------------------------------------------------------------------------
# Per-tool options (Settings → AI → Tools)
# agent.tool_options: { "run_fea_static": {"timeout_s": 180, "prompt_note": "..."}, ... }
# Missing key / missing field = stock behavior.
# ---------------------------------------------------------------------------
TOOL_OPTION_KEYS = (
    "enabled", "timeout_s", "max_calls_per_turn", "param_defaults", "prompt_note",
)

_TOOL_OPTION_DEFAULTS: dict = {
    "enabled": True,
    "timeout_s": None,
    "max_calls_per_turn": None,
    "param_defaults": {},
    "prompt_note": "",
}


def get_tool_options(cfg: dict | None, name: str) -> dict:
    """Merged per-tool options for *name*. Unknown stored keys ignored."""
    out = {
        "enabled": True,
        "timeout_s": None,
        "max_calls_per_turn": None,
        "param_defaults": {},
        "prompt_note": "",
    }
    if not name:
        return out
    try:
        c = cfg if isinstance(cfg, dict) else load_config()
    except Exception:  # noqa: BLE001
        c = cfg if isinstance(cfg, dict) else {}
    agent = c.get("agent") if isinstance(c, dict) and isinstance(c.get("agent"), dict) else {}
    raw_root = agent.get("tool_options") if isinstance(agent, dict) else None
    if not isinstance(raw_root, dict):
        return out
    entry = raw_root.get(name)
    if not isinstance(entry, dict):
        return out
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
    return out


def get_tool_families(cfg: dict | None = None) -> dict[str, bool]:
    """Return family→enabled map. Defaults all known families to True."""
    c = cfg if isinstance(cfg, dict) else load_config()
    agent = c.get("agent") if isinstance(c.get("agent"), dict) else {}
    raw = agent.get("tool_families") if isinstance(agent.get("tool_families"), dict) else {}
    out: dict[str, bool] = {}
    for fam in DEFAULT_TOOL_FAMILIES:
        v = raw.get(fam, True)
        out[fam] = bool(v) if not isinstance(v, bool) else v
    # preserve any extra keys user added
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
# Precedence is ENV > config.json > default: CHUDWARE_MEMORY_BACKEND /
# CHUDWARE_COMPACTION_BACKEND still win when set (per-process override; the GUI
# comboboxes write config.json only).

MEMORY_BACKEND_KEY = "memory_backend"
COMPACTION_BACKEND_KEY = "compaction_backend"
CACHE_WARMING_KEY = "cache_warming"
MEMORY_LEARN_KEY = "memory_learn"

DEFAULT_MEMORY_BACKEND: str = "sqlite"
DEFAULT_COMPACTION_BACKEND: str = "builtin"
DEFAULT_CACHE_WARMING: bool = False
DEFAULT_MEMORY_LEARN: bool = True

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


def _attach_catalog_meta(provider: Provider, caps: dict) -> Provider:
    """Stamp resolved catalog fields onto the provider for downstream consumers."""
    try:
        provider.context_window = int(caps.get("context") or 0) or None  # type: ignore[attr-defined]
        provider.max_output_tokens = int(caps.get("max_output") or 0) or None  # type: ignore[attr-defined]
        provider.reasoning_efforts = list(caps.get("reasoning_efforts") or [])  # type: ignore[attr-defined]
        provider.catalog_auto_source = str(caps.get("auto_source") or "")  # type: ignore[attr-defined]
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
                            "kimi-sub", "claude-cli", "codex-cli"):
        # Subscription-auth providers (§4.4) and CLI-subscription drivers always talk to
        # a vendor cloud endpoint (the CLI is just a local front-end).
        raise OfflineModeError(
            f"offline mode is enabled (config '{OFFLINE_SETTING}': true): the subscription "
            f"provider {name!r} uses a vendor cloud endpoint and is refused. Use a local "
            f"(loopback) model server instead.")

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

        oc = cfg.get("openrouter", {}) or {}
        keys_map = cfg.get("keys") or {}
        key = (
            (oc.get("api_key") if isinstance(oc, dict) else None)
            or cfg.get("api_key")
            or cfg.get("openrouter_api_key")
            or (keys_map.get("openrouter") if isinstance(keys_map, dict) else None)
            or os.environ.get("OPENROUTER_API_KEY")
        )
        if not key:
            # mega-6: never construct a keyless OpenRouter client — complete would
            # 401 as provider_auth and kill every depth-0 auto_pick free leaf.
            raise ValueError(
                "openrouter provider requires an API key "
                "(config openrouter.api_key / keys.openrouter / OPENROUTER_API_KEY); "
                "auto_pick must skip keyless openrouter"
            )
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

    if name == "kimi-sub":
        # Subscription auth (§4.4): Kimi For Coding device-code OAuth.
        from .providers.kimi_sub_provider import KimiSubProvider, DEFAULT_MODEL

        return KimiSubProvider(model=cfg.get("model", DEFAULT_MODEL))

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
