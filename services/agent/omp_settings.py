"""Settings → AI policy over OMP's own settings.

Chudware keeps no copy of an OMP setting. This module decides which rows of OMP's settings
panel Settings → AI shows and under which Chudware tab, which of them Chudware overrides for
CAD sessions, and where that override lives. Labels, descriptions, choices, order and
validation stay OMP's (the extension reads them from OMP's registry).

Two scopes, both plain OMP config:

* ``omp``: the user's own OMP config (shared with their other OMP sessions), written
  through OMP's settings API by the extension.
* ``chudware``: one stable OMP config file passed as ``--config`` to every Chudware OMP
  process. It holds Chudware's defaults for CAD sessions and the user's Chudware-only
  choice for those keys. OMP runtimes watch ``--config`` files, so an edit applies live.
  The per-session overlay (``omp_host._cad_session_overlay``) is passed after it and wins.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

from services.agent import request_context as tenancy
from services.agent.omp_bridge import BridgeError

#: Chudware AI tab -> OMP settings tab -> OMP group -> the only keys shown from it
#: (None: every row of the group). Settings appear in OMP's panel order: OMP's group
#: order, then its row order. ``behavior`` and ``developer`` have no OMP source; ``memory``
#: is chosen per backend (``_MEMORY_GROUPS``).
_TIERS = frozenset({"tier.openai", "tier.anthropic", "tier.google", "tier.subagent", "tier.advisor"})
_Groups = dict[str, frozenset[str] | None]
SOURCES: dict[str, dict[str, _Groups]] = {
    "models": {"model": {
        "Thinking": None,
        "Sampling": _TIERS,
        "Retry & Fallback": None,
        "Vision": frozenset({"images.describeForTextModels"}),
    }},
    "context": {"context": {"General": None, "Compaction": None, "Rules (TTSR)": None}},
    "memory": {},  # the group of the selected backend, see _groups
    "tools": {"tools": {"Todos": None, "Output Limits": None, "Execution": None}},
    "coding": {
        "files": {"Editing": None, "Reading": None, "Read Summaries": None, "LSP": None},
        "shell": {"Bash": None, "Eval & Runtimes": None},
    },
    "agents": {"tasks": {"Subagents": None}},
    "providers": {"providers": {"Services": None, "Protocol": None, "Timeouts": None, "Privacy": None}},
}

#: The group of OMP's Memory tab that configures each project-memory backend Chudware can
#: select (``config.memory_backend``); backends without one show no OMP rows.
_MEMORY_GROUPS = {"mnemopi": "Mnemopi", "hindsight": "Hindsight", "sharpshooter": "Sharpshooter"}

#: Rows of the groups above that Settings → AI never shows.
EXCLUDED = frozenset({
    # Chudware's own controls drive these (compaction method, task list) or pin them per session.
    "compaction.enabled", "compaction.supersedeReads", "compaction.dropUseless",
    "compaction.experimentalContextManagement", "todo.eager", "task.maxRuntimeMs",
    "mnemopi.scoping", "async.enabled",
    # The composer chooses the thinking level for every turn.
    "defaultThinkingLevel", "providers.autoThinkingMaxEffort",
    # Terminal rendering and print mode: Chudware draws its own transcript.
    "hideThinkingBlock", "expandThinkingBlocks", "read.renderMarkdown", "read.toolResultPreview",
    "async.widgets", "async.printWake", "branchSummary.enabled",
    # Tool availability is Chudware's tool policy, not an OMP switch.
    "bash.enabled",
    # A provider-flagged request shape that also adds a tool outside that policy.
    "externalThinking",
    "task.completionProbe",
    # Speech and voice are not CAD features.
    "live.voice", "tts.localVoice", "tts.localSpeed",
    "speech.enabled", "speech.mode", "speech.enhanced", "speech.voice", "speech.speed",
})

#: OMP settings Chudware overrides for CAD sessions, with the default it ships (scope
#: ``chudware``). LSP is off: OMP's ``lsp.enabled`` gates the lsp tool and discovery only,
#: so diagnostics on write (which would start language servers inside CAD projects) is off
#: with it. ``task.maxEffort`` is the measured CAD sub-agent default: xhigh doubled the
#: output tokens of a turn.
CHUDWARE_DEFAULTS: dict[str, bool | str] = {
    "lsp.enabled": False,
    "lsp.diagnosticsOnWrite": False,
    "task.maxEffort": "high",
}

#: What Chudware adds to OMP's own description of a setting.
_NOTES = {"lsp.enabled": "Applies to new chats only."}

_HEADER = ("# Chudware's defaults for CAD sessions and the choices made in Settings → AI for them.\n"
           "# Passed as --config to every Chudware OMP process; OMP applies edits live.\n")
_LIVE_SECONDS = 5.0
_lock = threading.Lock()


class Refused(ValueError):
    """The request is not one Settings → AI may make."""


def _groups(backend: str) -> dict[str, dict[str, _Groups]]:
    group = _MEMORY_GROUPS.get(backend)
    return {**SOURCES, "memory": {"memory": {group: None}} if group else {}}


def sources(backend: str) -> list[dict[str, Any]]:
    """The OMP tabs and groups to read for ``config_list``, in Chudware tab order."""
    return [{"tab": omp_tab, "groups": list(groups)}
            for omp_tabs in _groups(backend).values() for omp_tab, groups in omp_tabs.items()]


def classify(row: Mapping[str, Any], backend: str) -> str | None:
    """The Chudware tab for one OMP row, or None when Settings → AI does not expose it."""
    key = row.get("key")
    if key in EXCLUDED:
        return None
    for tab, omp_tabs in _groups(backend).items():
        groups = omp_tabs.get(row.get("tab"))
        if groups is not None and row.get("group") in groups:
            only = groups[row["group"]]
            return tab if only is None or key in only else None
    return None


def scope_of(key: str) -> str:
    return "chudware" if key in CHUDWARE_DEFAULTS else "omp"


def _choice(value: Any) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def _shape(row: Mapping[str, Any], tab: str) -> dict[str, Any]:
    key = row["key"]
    entry = {name: row[name] for name in
             ("key", "group", "label", "description", "type", "options", "current", "default", "source")}
    entry.update(tab=tab, scope=scope_of(key))
    if key in CHUDWARE_DEFAULTS:
        entry["default"] = _choice(CHUDWARE_DEFAULTS[key])
    if key in _NOTES:
        entry["description"] = f"{entry['description']} {_NOTES[key]}"
    return entry


def present(rows: list[Mapping[str, Any]], backend: str) -> list[dict[str, Any]]:
    """The extension's ``config_list`` rows as Settings → AI entries.

    OMP hides a row whose own condition fails (a reserve margin without usage-aware
    fallback); a memory backend's rows are shown because the backend is selected, even
    though OMP's condition looks at the backend of the runtime that answers.
    """
    shown = []
    for row in rows:
        tab = classify(row, backend)
        if tab is not None and (row["visible"] or row["tab"] == "memory"):
            shown.append(_shape(row, tab))
    return shown


# --------------------------------------------------------------------------- Chudware layer

def omp_dir() -> Path:
    """Where Chudware keeps the OMP config files it generates (the actor's root in a cell)."""
    from services import paths

    return (tenancy.actor_state_root() if tenancy.in_cell() else Path(paths.ROOT)) / "omp"


def layer_path() -> Path:
    return omp_dir() / "cad-settings.yml"


def _lookup(tree: Mapping[str, Any], key: str) -> Any:
    node: Any = tree
    for part in key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _assign(tree: dict[str, Any], key: str, value: Any) -> None:
    *parents, leaf = key.split(".")
    node = tree
    for part in parents:
        child = node.get(part)
        if not isinstance(child, dict):
            child = node[part] = {}
        node = child
    node[leaf] = value


def _load(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _store(path: Path, tree: Mapping[str, Any]) -> None:
    content = _HEADER + yaml.safe_dump(dict(tree), sort_keys=True, allow_unicode=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".cad-settings-", suffix=".yml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        # OMP's watcher follows a replaced file as well as an edited one.
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _fill(tree: dict[str, Any]) -> bool:
    """Add the Chudware defaults the layer lacks (never touching a chosen value)."""
    absent = [key for key in CHUDWARE_DEFAULTS if _lookup(tree, key) is None]
    for key in absent:
        _assign(tree, key, CHUDWARE_DEFAULTS[key])
    return bool(absent)


def ensure_layer() -> str:
    """Create the layer with Chudware's defaults, or add any default it lacks; its path."""
    path = layer_path()
    with _lock:
        tree = _load(path)
        if _fill(tree) or not path.is_file():
            _store(path, tree)
    return str(path)


def _write_layer(key: str, value: Any) -> None:
    path = layer_path()
    with _lock:
        tree = _load(path)
        _fill(tree)
        _assign(tree, key, value)
        _store(path, tree)


def layer_value(key: str) -> Any:
    """The value the layer holds for ``key`` (None if it holds none)."""
    with _lock:
        return _lookup(_load(layer_path()), key)


# --------------------------------------------------------------------------- operations

Call = Callable[[str, dict[str, Any]], dict[str, Any]]


def _row(call: Call, key: str) -> dict[str, Any]:
    try:
        return call("config_get", {"keys": [key]})["settings"][0]
    except BridgeError as exc:
        if exc.code in {"unsupported", "invalid_request"}:
            raise Refused(f"{key} is not an AI setting Chudware exposes") from None
        raise


def apply(call: Call, key: str, value: str, backend: str) -> dict[str, Any]:
    """Set one setting (``"default"``: back to OMP's default, or to Chudware's) and return
    the entry as it now takes effect in the service runtime."""
    tab = classify(_row(call, key), backend)
    if tab is None:
        raise Refused(f"{key} is not an AI setting Chudware exposes")
    try:
        if scope_of(key) == "omp":
            return _shape(call("config_set", {"key": key, "value": value}), tab)
        typed = (CHUDWARE_DEFAULTS[key] if value == "default"
                 else call("config_parse", {"key": key, "value": value})["value"])
    except BridgeError as exc:
        if exc.code == "invalid_request":
            raise Refused(f"{value!r} is not a valid value for {key}") from None
        raise
    _write_layer(key, typed)
    # The service runtime applies the edited file within a moment; answer with what it now uses.
    deadline = time.monotonic() + _LIVE_SECONDS
    while True:
        row = _row(call, key)
        if row["current"] == _choice(typed) or time.monotonic() >= deadline:
            return _shape(row, tab)
        time.sleep(0.1)
