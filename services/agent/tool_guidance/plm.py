"""tool_guidance.plm — curated options for the plm tool family."""
from __future__ import annotations

from typing import Any

ENTRIES: dict[str, dict[str, Any]] = {
    "plm_init": {
        "prompt_note": (
            "First call on a project folder — every other plm_* tool takes "
            "the same path. Idempotent: safe to re-call just to fetch the "
            "manifest."
        ),
    },
    "plm_open": {
        "prompt_note": (
            "Identical to plm_init (one delegates to the other) — use either "
            "to open-or-create a vault; both are zero-config and idempotent."
        ),
    },
    "plm_save": {
        "prompt_note": (
            "Snapshot the whole project after an edit batch. Use "
            "plm_checkpoint for a named return point, plm_release to ship a "
            "revision."
        ),
    },
    "plm_checkpoint": {
        "prompt_note": (
            "Always pass a plain-language message — it is how the point is "
            "found later. item_id scopes the checkpoint to one item."
        ),
    },
    "plm_release": {
        "prompt_note": (
            "On reason='preflight_failed' read problems, fix them, or re-call "
            "with confirm=true. Released items freeze — to edit again use "
            "plm_item_set(start_new_rev=True)."
        ),
    },
    "plm_item_set": {
        "prompt_note": (
            "The only way to edit a released item again: start_new_rev=True "
            "opens a working rev. Calling with neither part_number nor "
            "start_new_rev errors out."
        ),
    },
    "plm_item_add": {
        "prompt_note": (
            "Only for non-design files (specs, datasheets, sim folders) — "
            "design docs auto-track on save. Pass file_path or folder_path; "
            "file_path wins if both are set."
        ),
    },
    "plm_link_repo": {
        "prompt_note": (
            "Links a repo by reference only — nothing is copied in; the "
            "version pin lands at release. Use plm_item_add when the sources "
            "should be versioned in the vault."
        ),
    },
    "plm_plan": {
        "prompt_note": (
            "list first to learn entity ids, then create/update/delete with "
            "data; update and delete need id. Entries version and restore "
            "with the project."
        ),
    },
    "plm_where_used": {
        "prompt_note": (
            "Run before a release or new rev to see the blast radius; "
            "transitive=True walks the whole consumer chain. Unknown item_id "
            "returns invalid_item."
        ),
    },
    "plm_history": {
        "prompt_note": (
            "Newest first; pass limit to cap tokens on long projects. "
            "Commit ids from here drive plm_compare and plm_restore."
        ),
        # limit=0 -> `limit or None` -> unbounded vault history; long projects
        # accumulate hundreds of entries, each with message + summary.
        "param_defaults": {"limit": 50},
    },
    "plm_restore": {
        "prompt_note": (
            "Non-destructive — the restore is itself a new entry and can be "
            "undone. Get commit ids from plm_history; item_id scopes the "
            "restore to one item."
        ),
    },
    "plm_status": {
        "prompt_note": (
            "Cheap orientation call before release/sync: items with revision "
            "+ lifecycle, which are dirty (unsaved), head, ai_mode, remotes, "
            "locks."
        ),
    },
    "plm_compare": {
        "prompt_note": (
            "Get both commit ids from plm_history first. visual=True rebuilds "
            "both versions in the C++ engine and renders PNGs — slow, "
            "degrades gracefully without the engine."
        ),
        "timeout_s": 240,
    },
    "plm_set_ai_mode": {
        "prompt_note": (
            "Per-project setting persisted in the vault. managed lets the "
            "copilot write checkpoints itself; assisted (default) only "
            "drafts and suggests."
        ),
    },
    "plm_timeline": {
        "prompt_note": (
            "AI-VC checkpoints for AI-authored documents — NOT the project "
            "vault timeline (that is plm_history). Empty doc_id lists every "
            "AI-VC chain."
        ),
    },
    "plm_rollback": {
        "prompt_note": (
            "Undo for AI-authored docs only; for vault state use plm_restore. "
            "Get ids from plm_timeline first; '-1' targets the newest "
            "checkpoint. Silent — never asks the user."
        ),
    },
    "plm_rollback_check": {
        "prompt_note": (
            "Non-destructive preflight for plm_rollback — same target syntax, "
            "changes nothing. Returns what a rollback WOULD discard "
            "(discarded_messages/turns), whether geometry moves "
            "(geometry_rollback), and whether the backing file was modified "
            "outside this session (file.externally_modified; null = unknown). "
            "Call it before any rollback the user did not explicitly confirm."
        ),
    },
    "plm_add_remote": {
        "prompt_note": (
            "Register before plm_sync/plm_lock. folder/ssh/https work via the "
            "url string; chudware-cloud is config-only. Folder remotes take "
            "a bare repo path in url."
        ),
    },
    "plm_sync": {
        "prompt_note": (
            "Needs a remote registered via plm_add_remote first. Same-item "
            "collisions are copied under a new name, never merged — check "
            "conflicts_copied in the result."
        ),
    },
    "plm_lock": {
        "prompt_note": (
            "Advisory, team-visible; reason='locked' means someone holds it "
            "— do not edit around it. Lock before editing a shared item, "
            "plm_unlock after."
        ),
    },
    "plm_unlock": {
        "prompt_note": (
            "Releases only a lock you hold — always pair with plm_lock once "
            "the edit is saved so teammates are not blocked."
        ),
    },
    "plm_graph_brief": {
        "prompt_note": (
            "One-node dossier (identity, degree, edge kinds, neighbors) — "
            "the cheap orientation call before expanding anything in the "
            "project graph."
        ),
    },
    "plm_graph_changes": {
        "prompt_note": (
            "Diffs the project graph against its last cached build — run "
            "after edits to see what your change actually touched."
        ),
    },
    "plm_graph_configure": {
        "prompt_note": (
            "Get/set what the graph builder ingests (office content, linked "
            "projects); op='get' first — the build reads this config."
        ),
    },
    "plm_graph_keyword": {
        "prompt_note": (
            "Tag graph nodes/paths with keywords so plm_graph_search and "
            "watch overlays can slice by topic; op='add' by default."
        ),
    },
    "plm_graph_links": {
        "prompt_note": (
            "List/remove hand- or AI-authored overlay links; the add half "
            "is cad_graph_link — pair suggestions (plm_graph_suggest) with "
            "explicit links for relationships that matter."
        ),
    },
    "plm_graph_neighbors": {
        "prompt_note": (
            "Ego graph around node_id within depth hops — widen depth only "
            "when depth=1 misses the dependency you are tracing."
        ),
    },
    "plm_graph_search": {
        "prompt_note": (
            "Substring/kind/keyword search over the project graph — the "
            "entry point for finding a node_id before brief/neighbors."
        ),
    },
    "plm_graph_suggest": {
        "prompt_note": (
            "Heuristic overlay-link suggestions; review before linking — "
            "suggestions are candidates, not facts."
        ),
    },
    "plm_graph_watch": {
        "prompt_note": (
            "Manage the watch overlay (keywords/node ids of interest) so "
            "later graph diffs flag what you actually care about."
        ),
    },
}
