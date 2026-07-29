"""tool_guidance — built-in curated per-tool options (the shipping default layer).

Each family module defines ``ENTRIES: dict[str, dict]`` keyed by tool name with a
subset of the ``agent.tool_options`` keys (``config.TOOL_OPTION_KEYS``), minus
``enabled`` / ``description_override`` / ``schema_trim``:

  prompt_note        str  — one-line AI usage guidance → "Note: …" in the tools
                            slot (tool_schemas.render_tools_block)
  timeout_s          int  — per-call wall timeout; only for genuinely slow tools
  max_calls_per_turn int  — per-turn cap; only for clearly loop-prone tools
  param_defaults     dict — kwargs merged under the model's call (model wins)

``config.get_tool_options`` fills fields the user's config.json entry leaves
unset from this table — the user always wins per-field.  Content is authored
per family from the live tool registry (``scratch/tool_catalog_dump.json`` is
the authoring snapshot); ``test_tool_guidance.py`` validates that every key
resolves to a real registry tool or a known pseudo-tool and that only the
allowed option keys are used.
"""
from __future__ import annotations

import importlib
import logging
from typing import Any

# Guidance family modules. Normally one per live tool_registry family; "firmware"
# is an authoring split — the fw_* tools are registry-tagged "ecad" (they share the
# board seam) but their guidance is its own module, and no fw_* key exists in
# ecad.py, so each tool still has exactly one owner.
_FAMILIES = (
    "advisor", "cad", "cam", "ecad", "em", "files", "firmware", "gsl",
    "intake", "ledger", "mission", "office", "org", "parts", "plm", "recon",
    "research", "reveng", "shell", "sim", "tasklist", "workspace",
)

# Agent pseudo-tools with no live registry callable outside a mission
# dispatcher (hand-written schemas in tool_schemas._FALLBACK_SCHEMAS).
# task_list is deliberately NOT registered in services/mcp/server.py: it
# publishes to the in-app copilot Task-list panel, which means nothing to an
# external MCP client. It resolves for agent dispatch through reviewer_exec's
# "tasklist" module_key.
PSEUDO_TOOLS: frozenset[str] = frozenset({
    "enable_tools", "list_tool_categories", "design_recipe",
    "delegate", "adopt_role", "task_list",
})

# Option keys a guidance entry may set (config.TOOL_OPTION_KEYS minus the
# user-only ones: enabled is a kill-switch, description_override / schema_trim
# stay user-facing — fix docstrings at source instead).
GUIDANCE_KEYS: frozenset[str] = frozenset({
    "prompt_note", "timeout_s", "max_calls_per_turn", "param_defaults",
})

TOOL_GUIDANCE: dict[str, dict[str, Any]] = {}


def _load() -> None:
    for fam in _FAMILIES:
        try:
            mod = importlib.import_module(f"{__name__}.{fam}")
        except ModuleNotFoundError:
            continue  # family module not authored yet
        except Exception:  # noqa: BLE001 — a broken family must not break dispatch
            logging.getLogger(__name__).warning(
                "tool_guidance: family %r failed to load", fam, exc_info=True)
            continue
        entries = getattr(mod, "ENTRIES", None)
        if isinstance(entries, dict):
            for name, entry in entries.items():
                if isinstance(name, str) and isinstance(entry, dict):
                    TOOL_GUIDANCE[name] = dict(entry)


_load()


def guidance_for(name: str) -> dict[str, Any]:
    """Curated default options for *name* (empty dict when none). Never raises."""
    try:
        return dict(TOOL_GUIDANCE.get(name) or {})
    except Exception:  # noqa: BLE001
        return {}
