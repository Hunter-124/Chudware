"""context_pack.py — Assemble a context pack for a DAG node's agent invocation.

Contract: contracts/dag_schema.md §2 (node object, inputs, tools).
A context pack is a compact dict containing ONLY what the node needs:
  - part_description: GSL describe_part output for the primary doc_id (if any)
  - node_inputs:      the node's own 'inputs' dict
  - allowed_tools:    list of MCP tool names the node is permitted to call
  - retry_remaining:  how many retries are left for this node (from node_state)
  - end_condition:    the node's declared end condition (if any)
  - node_id:          the node's id
  - node_type:        the node type
  - node_name:        the human-readable name
  - phase:            optional phase/stage used by skill and budget policy

Deliberately excludes full conversation history. The agent only sees what's
in the context pack — history isolation is the whole point.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

# Default retry budget (applies when a back-edge does not specify one).
DEFAULT_RETRY_BUDGET = 1

# Sentinel: all tools permitted (when node.tools is null/absent).
ALL_TOOLS = None

# ---------------------------------------------------------------------------
# Optional GSL describe_part hook
# ---------------------------------------------------------------------------

# Injectable hook so tests can stub out the live engine RPC. Signature:
#   describe_part_fn(doc_id: str) -> str
# Default: None — context pack skips the description (no engine available /
# doc_id not present on the node).  Set to a real implementation at process
# start via set_describe_part_fn() or pass as describe_fn= to assemble().
_describe_part_fn: Optional[Callable[[str], str]] = None


def set_describe_part_fn(fn: Callable[[str], str]) -> None:
    """Register the live GSL describe_part implementation.

    Called once at server startup so context packs can embed part descriptions
    without importing the MCP layer directly.

    Args:
        fn: A callable that takes a doc_id string and returns a GSL text string.
    """
    global _describe_part_fn  # noqa: PLW0603
    _describe_part_fn = fn


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def assemble(
    node: dict[str, Any],
    node_state: dict[str, Any],
    *,
    describe_fn: Optional[Callable[[str], str]] = None,
) -> dict[str, Any]:
    """Assemble a context pack for a single node invocation.

    Args:
        node:         The node object from the aicad.dag/1 graph document.
        node_state:   The current execution state for this node (from
                      state["node_states"][node_id]). Must contain at minimum
                      ``retry_remaining`` (int) and may be empty for the first
                      invocation — missing keys are defaulted safely.
        describe_fn:  Optional override for the GSL describe_part function.
                      Falls back to the module-level _describe_part_fn or skips
                      the description if neither is set.

    Returns:
        A flat dict with keys:
          node_id, node_type, node_name, phase,
          node_inputs, allowed_tools, retry_remaining,
          end_condition, part_description (str or None).
    """
    node_id: str = node["id"]
    node_type: str = node["type"]
    node_name: str = node["name"]
    node_inputs: dict[str, Any] = node.get("inputs") or {}
    tools_spec: Optional[list[str]] = node.get("tools")  # null/absent → ALL_TOOLS

    # Normalise the allowed-tools list.
    if tools_spec is None:
        allowed_tools = ALL_TOOLS
    else:
        allowed_tools = list(tools_spec)

    # Retry remaining: carry it from node_state if present; first run = budget_max.
    retry_remaining: int = node_state.get("retry_remaining", DEFAULT_RETRY_BUDGET)

    # End condition hint (passed to the agent system prompt).
    end_condition: Optional[str] = node.get("end_condition")
    phase = node.get("phase") or node_inputs.get("phase") or node_inputs.get("stage")

    # GSL part description — only fetched when the node specifies a doc_id and
    # a describe function is available.
    part_description: Optional[str] = None
    doc_id = node_inputs.get("doc_id")
    if doc_id:
        fn = describe_fn or _describe_part_fn
        if fn is not None:
            try:
                part_description = fn(doc_id)
            except Exception:  # noqa: BLE001
                # Degrade gracefully — the node can still run without a description.
                part_description = None

    pack = {
        "node_id": node_id,
        "node_type": node_type,
        "node_name": node_name,
        "node_inputs": node_inputs,
        "allowed_tools": allowed_tools,
        "retry_remaining": retry_remaining,
        "end_condition": end_condition,
        "phase": phase,
        "part_description": part_description,
    }
    # Promote difficulty routing fields so node_runner._resolve_role_settings
    # and role_models see them without digging.
    for key in ("task_class", "depth", "risk", "role", "domain_tags", "run_id", "session_id"):
        if key in node_inputs and node_inputs[key] is not None:
            pack[key] = node_inputs[key]
    # node_state may carry run_id on resume / re-entry.
    if "run_id" in node_state and node_state["run_id"] and "run_id" not in pack:
        pack["run_id"] = node_state["run_id"]
    if node_state.get("session_id") and "session_id" not in pack:
        pack["session_id"] = node_state["session_id"]
    if "role" in node_inputs and "role_id" not in pack:
        pack["role_id"] = node_inputs["role"]
    return pack
