"""Nested profile-tree MCP tool — ``engine_profile_tree``.

Context-frugal view of the in-process sampling profiler + phase registry: returns the
ROOT + top-N hot children within a token budget, with ``path`` drill-down and a
``depth`` cap (like a file tree). Backed by the C++ RPCs:

  * ``prof_tree``         — nested call tree over folded stacks (samples)
  * ``prof_phases_tree``  — nested study -> phase -> subphase (wall-clock ms)

Registration is deferred behind :func:`register_profile_tools` so ``server.py`` wires
us in with a one-line call.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from . import core_bridge as cb


def _dumps(payload: Any) -> str:
    return json.dumps(payload, indent=2, default=str)


def _err(exc: BaseException | str, **extra: Any) -> str:
    msg = str(exc)
    out: dict[str, Any] = {"ok": False, "error": msg, **extra}
    return _dumps(out)


def _estimate_tokens(obj: Any) -> int:
    """Rough token estimate: ~4 chars per token on compact JSON."""
    try:
        s = json.dumps(obj, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001
        s = str(obj)
    return max(1, (len(s) + 3) // 4)


def _trim_to_budget(node: dict, budget_tokens: int, top: int) -> dict:
    """Keep node + hottest children until the estimate exceeds *budget_tokens*.

    Children beyond ``top`` are dropped (hot-first order already applied by the engine).
    If even the bare node exceeds the budget, return a stub with name/totals only.
    """
    if not isinstance(node, dict):
        return node
    children = list(node.get("children") or [])
    if top > 0:
        children = children[:top]

    bare = {k: v for k, v in node.items() if k != "children"}
    bare["children"] = []
    if _estimate_tokens(bare) >= budget_tokens:
        # Extreme: strip everything but the headline counters.
        return {
            "name": node.get("name", ""),
            "path": node.get("path", ""),
            "self_samples": node.get("self_samples", 0),
            "total_samples": node.get("total_samples", 0),
            "self_ms": node.get("self_ms"),
            "total_ms": node.get("total_ms"),
            "pct_of_parent": node.get("pct_of_parent", 0.0),
            "children": [],
            "truncated": True,
        }

    kept: list[dict] = []
    elided = 0
    for ch in children:
        # Recurse with a shared residual budget (greedy hot-first).
        residual = budget_tokens - _estimate_tokens({**bare, "children": kept})
        if residual < 32:
            elided += 1
            continue
        # Cap child fan-out tighter so the root stays readable.
        child_top = max(1, top // 2) if top > 0 else 4
        trimmed = _trim_to_budget(ch if isinstance(ch, dict) else {}, residual, child_top)
        trial = {**bare, "children": kept + [trimmed]}
        if _estimate_tokens(trial) > budget_tokens:
            elided += 1
            continue
        kept.append(trimmed)
    out = {**bare, "children": kept}
    remaining = max(0, len(children) - len(kept))
    if remaining or elided:
        out["children_elided"] = remaining + elided + int(node.get("children_elided") or 0)
    return out


def register_profile_tools(mcp) -> None:
    """Register ``engine_profile_tree`` on the given FastMCP instance."""

    @mcp.tool()
    def engine_profile_tree(
        source: str = "samples",
        path: str = "",
        depth: int = 1,
        top: int = 8,
        budget_tokens: int = 800,
        reset: bool = False,
    ) -> str:
        """Nested profile tree (file-tree style) — root + hot children, path drill-down.

        Prefer this over ``engine_profile(action="report")`` when you need a *scoped*
        read: start at the root (depth=1, top=8) to see the hottest first-level frames /
        studies, then re-call with ``path="hot_frame"`` (or ``path="domain/solve"``) to
        drill without ingesting the whole trace.

        Args:
          source: ``"samples"`` (default) — call-tree from the sampling profiler
                  (``prof_tree``). ``"phases"`` — study->phase->subphase wall-clock tree
                  (``prof_phases_tree``; always-on, no start needed).
          path:   slash-separated path into the tree (e.g. ``"main/foo"`` or
                  ``"fea_static/solve"``). Empty = whole tree root.
          depth:  nesting cap from the selected node (0 = node only, 1 = +direct
                  children, -1 = unlimited). Default 1 for a frugal first read.
          top:    max children per level (hot-first). Default 8.
          budget_tokens: soft token budget for the returned JSON; excess hot children
                  are elided (``children_elided`` counts them). Default 800.
          reset:  when source=phases, clear the phase registry AFTER building the tree
                  (same semantics as ``engine_profile(action="reset_phases")``).

        Returns JSON: ``{ok, path, depth, top, root:{name,self_*,total_*,pct_of_parent,
        path,children:[...]}, summary?, completed?, kept?}``.
        """
        try:
            src = (source or "samples").strip().lower()
            params: dict[str, Any] = {
                "path": path or "",
                "depth": int(depth),
                "top": int(top) if int(top) > 0 else 0,
            }
            if src in ("phases", "phase", "phases_tree"):
                params["reset"] = bool(reset)
                raw = cb.call("prof_phases_tree", params)
            elif src in ("samples", "sample", "call", "tree", "folded"):
                raw = cb.call("prof_tree", params)
            else:
                return _err(
                    ValueError(
                        f"unknown source {source!r}; use 'samples' (call tree) or "
                        f"'phases' (study/phase tree)"
                    )
                )

            if not isinstance(raw, dict):
                return _err("engine returned non-object profile tree")
            if not raw.get("ok", False):
                # Honest miss: no samples / unsupported / path not found.
                return _dumps(raw)

            budget = max(64, int(budget_tokens) if budget_tokens else 800)
            root = raw.get("root")
            if isinstance(root, dict):
                raw = dict(raw)
                raw["root"] = _trim_to_budget(root, budget, int(top) if int(top) > 0 else 8)
                raw["budget_tokens"] = budget
                raw["approx_tokens"] = _estimate_tokens(raw["root"])
            return _dumps(raw)
        except Exception as e:  # noqa: BLE001
            return _err(e)
