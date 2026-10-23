"""services.plm.assist — assist tiers, checkpoint heuristics, stale refs.

The policy layer for the PLM's three assist tiers (contract §4 ``ai_mode``),
with **manual always primary** (contract §8.4):

* ``manual``   — buttons only. Auto-history still runs (safety, not policy;
  contract §8.5). No drafting, no suggestions, no AI-cut entries.
* ``assisted`` — the DEFAULT. Deterministic drafting of checkpoint/release
  messages, checkpoint-worthiness suggestions, stale-reference flags. Nothing
  is cut without the user pressing the button.
* ``managed``  — opt-in. The copilot may additionally CUT checkpoints itself at
  meaningful boundaries (end of a copilot task / milestone). Every AI-cut
  entry is attributed (§3.4 ``ai`` block) and one-click discardable.

Everything here is deterministic policy — no learned calls. The only AI-ish
output is the templated message drafting in :mod:`summarize`, which is itself
deterministic.
"""
from __future__ import annotations

from typing import Any

from services.plm import manifest as manifest_mod

MODE_MANUAL = "manual"
MODE_ASSISTED = "assisted"
MODE_MANAGED = "managed"
MODES = manifest_mod.AI_MODES  # {"manual", "assisted", "managed"}

DEFAULT_MODE = MODE_ASSISTED

# The assist actions and the minimum tier each one needs.
_ACTION_TIERS: dict[str, set[str]] = {
    # deterministic drafting of checkpoint / release messages
    "draft_message": {MODE_ASSISTED, MODE_MANAGED},
    # "this looks checkpoint-worthy" nudges
    "suggest_checkpoint": {MODE_ASSISTED, MODE_MANAGED},
    # flag assemblies consuming a superseded component revision
    "flag_stale_refs": {MODE_ASSISTED, MODE_MANAGED},
    # the copilot cutting a checkpoint BY ITSELF — managed only
    "auto_checkpoint": {MODE_MANAGED},
}


def allowed(mode: str | None, action: str) -> bool:
    """Return True when *action* is permitted at assist tier *mode*.

    Unknown modes degrade to the manual floor (nothing allowed); unknown
    actions are never allowed. Manual buttons are always available regardless
    of mode — this gate covers only AI-initiated behavior.
    """
    if mode not in MODES:
        mode = MODE_MANUAL
    return mode in _ACTION_TIERS.get(action, set())


# ---------------------------------------------------------------------------
# checkpoint-worthiness heuristics (assisted+)
# ---------------------------------------------------------------------------

# Tunables — deliberately conservative so suggestions stay rare and meaningful.
_SUGGEST_MIN_FEATURES_TOUCHED = 3      # added+removed+changed features
_SUGGEST_MIN_PARAMS_CHANGED = 4        # distinct parameter edits
_SUGGEST_MAGNITUDE_RATIO = 0.5         # |new-old| / |old| for any numeric param
_SUGGEST_MIN_SECONDS = 30 * 60         # long session with any change at all
_SUGGEST_MIN_OPS = 25                  # many editing operations since last entry


def suggest_checkpoint(diff: dict[str, Any], *,
                       seconds_since_last: float | None = None,
                       ops_since_last: int | None = None) -> dict[str, Any]:
    """Deterministic "is this worth a checkpoint?" heuristic (plan §2).

    Signals (any one fires the suggestion):
      * several features added/removed/changed,
      * several parameters edited,
      * a large-magnitude parameter change (≥50% swing),
      * a long editing stretch or many ops since the last named entry.

    Returns ``{suggest: bool, reasons: [plain-language strings]}``. Purely a
    suggestion — nothing is cut here (assisted tier never cuts).
    """
    reasons: list[str] = []

    touched = (len(diff.get("features_added", []) or [])
               + len(diff.get("features_removed", []) or [])
               + len(diff.get("features_changed", []) or []))
    if touched >= _SUGGEST_MIN_FEATURES_TOUCHED:
        reasons.append("several features changed since the last named point")

    params = diff.get("params_changed", []) or []
    if len(params) >= _SUGGEST_MIN_PARAMS_CHANGED:
        reasons.append("many dimensions were edited")

    for rec in params:
        old, new = rec.get("old"), rec.get("new")
        if (isinstance(old, (int, float)) and isinstance(new, (int, float))
                and not isinstance(old, bool) and not isinstance(new, bool)
                and old != 0 and abs(new - old) / abs(old) >= _SUGGEST_MAGNITUDE_RATIO):
            reasons.append("a dimension changed by a large amount")
            break

    has_any_change = bool(touched or params or diff.get("mate_deltas")
                          or diff.get("sketch_deltas") or diff.get("suppression_flips"))
    if has_any_change and seconds_since_last is not None \
            and seconds_since_last >= _SUGGEST_MIN_SECONDS:
        reasons.append("it has been a while since the last named point")
    if has_any_change and ops_since_last is not None \
            and ops_since_last >= _SUGGEST_MIN_OPS:
        reasons.append("a lot of editing has happened")

    return {"suggest": bool(reasons), "reasons": reasons}


# ---------------------------------------------------------------------------
# stale-reference detection (assisted+)
# ---------------------------------------------------------------------------

def stale_refs(m: dict[str, Any]) -> list[dict[str, Any]]:
    """Flag assemblies whose frozen component pins point at superseded revs.

    Contract §5.1: a stale ref is "an assembly using a superseded component
    rev". The authoritative pin data is ``manifest.bom_freezes`` (written at
    assembly release). **Capability-checked:** on a manifest without
    release freeze data this returns ``[]`` rather than guessing.

    Returns records::

        {"assembly_item", "assembly_rev", "component_item", "pinned_rev",
         "current_rev", "hint"}
    """
    out: list[dict[str, Any]] = []
    freezes = m.get("bom_freezes") or []
    if not freezes:
        return out

    # latest freeze per assembly wins (freezes append per release)
    latest: dict[str, dict[str, Any]] = {}
    for fz in freezes:
        aid = fz.get("assembly_item")
        if aid:
            latest[aid] = fz

    for aid, fz in sorted(latest.items()):
        for pin in fz.get("pins", []) or []:
            comp = manifest_mod.find_item(m, pin.get("item_id", ""))
            if comp is None:
                continue
            pinned_rev = pin.get("rev")
            current_rev = comp.get("rev")
            if not pinned_rev or not current_rev or pinned_rev == current_rev:
                continue
            # the pinned rev must actually be superseded (not merely different)
            superseded = any(
                rr.get("rev") == pinned_rev
                and rr.get("lifecycle") == manifest_mod.LIFECYCLE_SUPERSEDED
                for rr in comp.get("released_revs", []) or []
            )
            if superseded:
                asm = manifest_mod.find_item(m, aid)
                out.append({
                    "assembly_item": aid,
                    "assembly_rev": fz.get("rev"),
                    "component_item": comp["item_id"],
                    "pinned_rev": pinned_rev,
                    "current_rev": current_rev,
                    "hint": (f"{_display(asm)} uses revision {pinned_rev} of "
                             f"{_display(comp)}, but revision {current_rev} "
                             "has replaced it"),
                })
    return out


def _display(item: dict[str, Any] | None) -> str:
    if not item:
        return "an item"
    return item.get("part_number") or item.get("path") or item.get("item_id", "an item")


# ---------------------------------------------------------------------------
# AI-managed hooks (managed only) — the copilot-loop seam
# ---------------------------------------------------------------------------

def should_auto_checkpoint(mode: str | None, trigger: str,
                           stage: str | None = None) -> bool:
    """Policy gate for the copilot loop / PLM bridge (services/agent seam).

    True only in ``managed`` mode AND for a milestone trigger the bridge's
    normative table (``services.agent.plm_bridge.CHECKPOINT_TRIGGERS``)
    enables. In ``manual`` / ``assisted`` the copilot never cuts entries.
    """
    if not allowed(mode, "auto_checkpoint"):
        return False
    try:
        from services.agent.plm_bridge import _trigger_enabled

        return _trigger_enabled(trigger, stage)
    except ImportError:  # bridge not present — still honor the tier gate
        return True


def auto_checkpoint(client: Any, project_id: str, message: str, *,
                    mode: str | None, run_id: str | None = None,
                    artifact_id: str | None = None,
                    item_id: str | None = None) -> dict[str, Any] | None:
    """Cut an AI-authored checkpoint at a task boundary — managed tier only.

    *client* is a §7.1 ``PLMVaultClient`` (or anything satisfying the bridge's
    ``VaultClient`` Protocol). Returns the checkpoint envelope, or ``None``
    when the tier forbids it (manual/assisted floors: no AI-cut entries).
    """
    if not allowed(mode, "auto_checkpoint"):
        return None
    return client.checkpoint(
        project_id=project_id, message=message, item_id=item_id,
        author_kind="ai", run_id=run_id, artifact_id=artifact_id,
    )


# ---------------------------------------------------------------------------
# AI-VC seam (agent/MCP silent checkpoints — separate from project vault)
# ---------------------------------------------------------------------------

def ai_vc_on_mutate(tool: str, args: dict | None, result: Any, **kwargs: Any) -> dict[str, Any] | None:
    """Route a successful AI mutation into the AI-VC store (silent, idempotent).

    Thin re-export so guardrails / agent_turn / MCP can share one import path
    without depending on the vault assist tier (AI-VC is always-on for agent
    docs; the managed-tier ``auto_checkpoint`` above remains for project vault).

    v2: pass ``before=True`` (or call :func:`ai_vc_before_tool`) to checkpoint
    BEFORE the mutating tool; :func:`ai_vc_on_prompt` for user-prompt starts.
    """
    from services.plm import ai_vc

    return ai_vc.on_ai_tool_success(tool, args, result, **kwargs)


def ai_vc_on_prompt(prompt: str, **kwargs: Any) -> dict[str, Any] | None:
    """v2: checkpoint before a user prompt turn (kind=prompt)."""
    from services.plm import ai_vc

    return ai_vc.on_prompt_start(prompt, **kwargs)


def ai_vc_before_tool(tool: str, args: dict | None, **kwargs: Any) -> dict[str, Any] | None:
    """v2: checkpoint before a mutating tool call (kind=tool)."""
    from services.plm import ai_vc

    return ai_vc.on_before_tool(tool, args, **kwargs)


def discard_entry(vault: Any, commit: str) -> dict[str, Any]:
    """One-click discard of a history entry (contract §8.4 reversibility).

    Restores the state just BEFORE *commit* as a new forward entry — history
    is never rewritten (§3.3), so the discard itself is also undoable.
    """
    from services.plm.vault import VaultError

    meta = vault._bind.commit_meta(commit)
    parents = meta.get("parents") or []
    if not parents:
        raise VaultError("not_found", "there is nothing before that entry to return to")
    return vault.restore(parents[0])
