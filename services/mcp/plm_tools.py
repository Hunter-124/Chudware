"""MCP tools: plm_* — the PLM vault surface (contract §7, aicad.plm/1).

Registers the append-only ``plm_*`` tool family that the frozen ``contracts/plm.md``
§7 specifies. This is the user/agent-driven surface (the AI-authored twin lives on
the in-process ``services.plm.client.PLMVaultClient`` seam, §7.1).

Every tool returns the §0 flat envelope: ``{ok:true, …}`` on success,
``{ok:false, reason:"<code>", hint:"<prose>"}`` on failure — NEVER a traceback.

Ease-of-use (contract §8): no user-facing git vocabulary. ``hint`` strings use
plain language; the machine ``commit`` field is an internal id, not rendered to
users as-is.

Registration: ``register_plm_tools(mcp)`` → adds plm_init, plm_open, plm_save,
plm_checkpoint, plm_release, plm_history, plm_restore, plm_status, plm_compare,
plm_set_ai_mode (T-R3), plm_item_set + plm_where_used (T-R4),
plm_timeline + plm_rollback (AI-VC), and T-R7 remotes/locks:
plm_add_remote, plm_sync, plm_lock, plm_unlock.

plm_where_used note: §7 reserves this name for T-R6's Hub; the T-R4 task card
ships the thin envelope early ({ok, consumers} over the derived index in
services/plm/whereused.py). Flagged in the T-R4 work note for integrator
review — dropping to in-process-only is a two-line change if §7 is read
strictly.
"""
from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP


def _err(reason: str, hint: str = "") -> str:
    return json.dumps({"ok": False, "reason": reason, "hint": hint})


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields}, indent=2, sort_keys=True)


def register_plm_tools(mcp: FastMCP) -> None:
    """Register the plm_* tool family onto *mcp* (contract §7)."""

    # lazy import so the MCP server starts even if dulwich isn't provisioned;
    # tools degrade to a contract-shaped no_vault/reason envelope.
    def _vault_module():
        from services.plm.vault import Vault, VaultError

        return Vault, VaultError

    @mcp.tool()
    def plm_init(path: str) -> str:
        """Create a project vault at *path* (a folder), registering existing
        documents. Zero-config and idempotent: re-init on an existing vault just
        opens it. Returns ``{ok, project_id, manifest}``."""
        try:
            Vault, VaultError = _vault_module()
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        try:
            v = Vault.open(path)
            m = v.manifest()
            v.close()
        except VaultError as e:
            return _err(e.reason, e.hint)
        return _ok(project_id=m["project"]["id"], manifest=m)

    @mcp.tool()
    def plm_open(path: str) -> str:
        """Open a project folder, auto-creating history if absent (zero-config).
        Returns ``{ok, project_id, manifest}``."""
        return plm_init(path)  # open == init (both zero-config, idempotent)

    @mcp.tool()
    def plm_save(path: str, kind: str = "auto") -> str:
        """Record a history entry over the whole project (an explicit save).
        ``kind`` ∈ ``auto`` | ``checkpoint``. Returns ``{ok, commit, entry}``.
        (Releases go through ``plm_release``.)"""
        if kind not in ("auto", "checkpoint"):
            return _err("invalid_mode", "a save can only be a save or a named checkpoint")
        try:
            Vault, VaultError = _vault_module()
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        try:
            v = Vault.open(path)
            res = v.save(kind)
            v.close()
        except VaultError as e:
            return _err(e.reason, e.hint)
        return _ok(commit=res["commit"], entry=res["entry"])

    @mcp.tool()
    def plm_checkpoint(path: str, message: str = "", item_id: str = "") -> str:
        """Save a named checkpoint the user can return to. ``message`` describes
        the change in plain language. Returns ``{ok, commit, entry}``."""
        try:
            Vault, VaultError = _vault_module()
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        try:
            v = Vault.open(path)
            res = v.checkpoint(message, item_id=item_id or None)
            v.close()
        except VaultError as e:
            return _err(e.reason, e.hint)
        return _ok(commit=res["commit"], entry=res["entry"])

    @mcp.tool()
    def plm_release(path: str, item_id: str, message: str = "",
                    confirm: bool = False) -> str:
        """Release an item: checks first (unsaved changes, out-of-date
        component references, never-released components), then stamps the
        next revision letter, marks it Released, freezes its exact form and —
        for assemblies — pins every component to its exact revision and
        freezes the bill of materials. Problems found by the checks block the
        release unless ``confirm`` is true (they are warnings, not walls).
        Returns ``{ok, commit, rev, preflight, bom_freeze?, stamp_orders}``;
        on blocked pre-flight: ``{ok:false, reason:"preflight_failed",
        problems:[…]}``."""
        try:
            Vault, VaultError = _vault_module()
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        try:
            from services.plm import release as release_mod

            v = Vault.open(path)
            try:
                res = release_mod.release(v, item_id, message, confirm=confirm)
            finally:
                v.close()
        except VaultError as e:
            problems = getattr(e, "problems", None)
            if problems is not None:
                return json.dumps({"ok": False, "reason": e.reason,
                                   "hint": e.hint, "problems": problems})
            return _err(e.reason, e.hint)
        return _ok(commit=res["commit"], rev=res["rev"], entry=res["entry"],
                   preflight=res["preflight"],
                   bom_freeze=res.get("bom_freeze"),
                   stamp_orders=res["stamp_orders"])

    @mcp.tool()
    def plm_item_set(path: str, item_id: str, part_number: str = "",
                     start_new_rev: bool = False) -> str:
        """Edit an item's registry entry: give it a new part number (kept
        unique within the project) and/or start a new revision of a released
        item so it can be edited again ("start rev C" — released designs are
        never changed in place). Returns ``{ok, item, working_rev?}``."""
        try:
            Vault, VaultError = _vault_module()
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        if not part_number and not start_new_rev:
            return _err("invalid_mode", "nothing to change — give a part number "
                        "or ask to start a new revision")
        try:
            v = Vault.open(path)
            try:
                fields: dict[str, Any] = {}
                if part_number:
                    fields["item"] = v.set_part_number(item_id, part_number)
                if start_new_rev:
                    out = v.start_new_rev(item_id)
                    fields["item"] = out["item"]
                    fields["working_rev"] = out["working_rev"]
            finally:
                v.close()
        except VaultError as e:
            return _err(e.reason, e.hint)
        return _ok(**fields)

    @mcp.tool()
    def plm_item_add(path: str, file_path: str = "", folder_path: str = "",
                     doc_type: str = "", part_number: str = "") -> str:
        """Track a non-design file or folder as a versioned item — a
        specification, report, datasheet, spreadsheet, image, simulation folder
        or source tree. Design documents (.step+/.ecad+) are tracked
        automatically when saved; this is for everything else. ``doc_type`` is
        auto-detected when omitted and can be changed later. Returns
        ``{ok, item}``."""
        try:
            Vault, VaultError = _vault_module()
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        rel = file_path or folder_path
        if not rel:
            return _err("invalid_mode", "give a file or folder to track")
        try:
            v = Vault.open(path)
            try:
                item = v.add_item(rel, doc_type=doc_type or None,
                                  part_number=part_number or None)
            finally:
                v.close()
        except VaultError as e:
            return _err(e.reason, e.hint)
        return _ok(item=item)

    @mcp.tool()
    def plm_link_repo(path: str, url: str, name: str = "",
                      checkout_path: str = "") -> str:
        """Link an external source-code repository to the project by reference
        (the code is never copied in — only a pointer, pinned to a version at
        each release). Returns ``{ok, item}``."""
        try:
            Vault, VaultError = _vault_module()
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        try:
            v = Vault.open(path)
            try:
                item = v.link_repo(url, name, checkout_path=checkout_path or None)
            finally:
                v.close()
        except VaultError as e:
            return _err(e.reason, e.hint)
        return _ok(item=item)

    @mcp.tool()
    def plm_plan(path: str, op: str = "list", entity: str = "task",
                 data: dict | None = None, id: str = "") -> str:
        """Plan the work: read or change the project's tasks, issues and
        milestones (stored with the project, so they version and restore with
        it). ``op`` ∈ ``list`` | ``create`` | ``update`` | ``delete``;
        ``entity`` ∈ ``task`` | ``issue`` | ``milestone``. ``data`` carries the
        fields (title, status/priority, assignee, links to items/revisions, …).
        Returns ``{ok, items, entity?}``."""
        try:
            Vault, VaultError = _vault_module()
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        try:
            v = Vault.open(path)
            try:
                res = v.plan(op, entity, data=data or None, ent_id=id or None)
            finally:
                v.close()
        except VaultError as e:
            return _err(e.reason, e.hint)
        return _ok(**res)

    @mcp.tool()
    def plm_where_used(path: str, item_id: str, transitive: bool = False) -> str:
        """Answer "where is this used?": the assemblies, drawings and CAM jobs
        that consume an item (directly, or the whole chain with
        ``transitive``). Derived live from the project — never stale.
        Returns ``{ok, consumers}``."""
        try:
            Vault, VaultError = _vault_module()
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        try:
            from services.plm import whereused

            v = Vault.open(path)
            try:
                m = v.manifest()
                docs = whereused.docs_at_head(v)
                try:
                    consumers = whereused.where_used(m, docs, item_id,
                                                     transitive=transitive)
                except KeyError:
                    return _err("invalid_item", f"no such item: {item_id}")
            finally:
                v.close()
        except VaultError as e:
            return _err(e.reason, e.hint)
        return _ok(consumers=consumers)

    @mcp.tool()
    def plm_history(path: str, item_id: str = "", limit: int = 0) -> str:
        """Return the timeline (newest first), optionally filtered to one item.
        Each entry: ``{commit, kind, authored_at, author, message, thumbnail_ref,
        summary}``. Returns ``{ok, entries}``."""
        try:
            Vault, VaultError = _vault_module()
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        try:
            v = Vault.open(path)
            entries = v.history(item_id or None, limit or None)
            v.close()
        except VaultError as e:
            return _err(e.reason, e.hint)
        return _ok(entries=entries)

    @mcp.tool()
    def plm_restore(path: str, commit: str, item_id: str = "") -> str:
        """Return the project (or one item) to an earlier point. This never
        erases anything: it records the earlier state as a new, latest entry, so
        the restore itself can be undone. Returns ``{ok, commit, entry}``."""
        try:
            Vault, VaultError = _vault_module()
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        try:
            v = Vault.open(path)
            res = v.restore(commit, item_id or None)
            v.close()
        except VaultError as e:
            return _err(e.reason, e.hint)
        return _ok(commit=res["commit"], entry=res["entry"])

    @mcp.tool()
    def plm_status(path: str) -> str:
        """Return project state for the Hub/badges: items (with revision +
        lifecycle), which have unsaved changes, and the current mode.
        Returns ``{ok, items, head, dirty, ai_mode, remotes, locks}``."""
        try:
            Vault, VaultError = _vault_module()
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        try:
            v = Vault.open(path)
            st = v.status()
            v.close()
        except VaultError as e:
            return _err(e.reason, e.hint)
        return _ok(**st)

    @mcp.tool()
    def plm_compare(path: str, commit_a: str, commit_b: str, item_id: str,
                    visual: bool = False, out_dir: str = "") -> str:
        """Compare an item between two history points.

        Generic (non-CAD) items — docs, code, links — route to a
        content/text diff (``mode:"generic"``, plain-language summary, and a
        unified/office text diff when the item is single-file). CAD items
        (including ECAD boards/schematics) route to the semantic ``doc_diff``
        comparison, and — when ``visual`` is true — a side-by-side visual of
        the two versions (match percentage + rendered images; drawings also
        get a 2D overlay). The visual half degrades gracefully on machines
        without the 3D engine.
        Returns ``{ok, mode, summary, text_diff?}`` for generic items or
        ``{ok, doc_diff, summary, eval_compare?}`` for CAD items."""
        try:
            Vault, VaultError = _vault_module()
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        try:
            from services.plm import compare as plm_compare_mod

            v = Vault.open(path)
            try:
                if plm_compare_mod.is_generic_item(v, item_id):
                    fields: dict[str, Any] = dict(plm_compare_mod.generic_compare(
                        v, commit_a, commit_b, item_id))
                else:
                    doc_a, doc_b = plm_compare_mod.docs_at(v, commit_a, commit_b, item_id)
                    sem = plm_compare_mod.semantic_compare(doc_a, doc_b)
                    fields = {"doc_diff": sem["doc_diff"], "summary": sem["summary"]}
                    if visual:
                        import tempfile

                        od = out_dir or tempfile.mkdtemp(prefix="chudware_plm_compare_")
                        fields["eval_compare"] = plm_compare_mod.visual_compare(doc_a, doc_b, od)
            finally:
                v.close()
        except VaultError as e:
            return _err(e.reason, e.hint)
        return _ok(**fields)

    @mcp.tool()
    def plm_set_ai_mode(path: str, mode: str) -> str:
        """Set how much the AI helps with this project's history:
        ``manual`` (buttons only), ``assisted`` (drafts messages, suggests
        checkpoints, flags out-of-date references — the default), or
        ``managed`` (the copilot also records checkpoints itself at task
        boundaries; every AI entry is marked and one click undoes it).
        Returns ``{ok, ai_mode}``."""
        try:
            Vault, VaultError = _vault_module()
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        try:
            v = Vault.open(path)
            try:
                out = v.set_ai_mode(mode)
            finally:
                v.close()
        except VaultError as e:
            return _err(e.reason, e.hint)
        return _ok(ai_mode=out)

    # ------------------------------------------------------------------
    # AI-VC: silent agent version control (copilot chat + MCP rollback)
    # ------------------------------------------------------------------

    @mcp.tool()
    def plm_timeline(doc_id: str = "") -> str:
        """List AI-authored checkpoints for a document (newest first).

        Each checkpoint: ``{id, turn, label, time, features_count, kind,
        msg_idx, call}`` (aliases ``when`` / ``features`` also present).
        ``kind`` is ``prompt`` | ``tool``. Empty ``doc_id`` lists every
        AI-VC chain. Returns ``{ok, checkpoints}``.
        """
        try:
            from services.plm import ai_vc
        except ImportError:
            return _err("no_vault", "AI version history is not available on this machine")
        try:
            out = ai_vc.plm_timeline(doc_id or None)
        except Exception as e:  # noqa: BLE001
            return _err("internal", f"{type(e).__name__}: {e}")
        return _ok(**{k: v for k, v in out.items() if k != "ok"})

    @mcp.tool()
    def plm_rollback(to: str, doc_id: str = "") -> str:
        """Roll an AI-authored document back to an earlier checkpoint.

        ``to`` is a checkpoint id, ``turn:<n>``, ``-N`` (relative from the
        newest: ``-1`` = latest, ``-2`` = one before, …), ``msg:<idx>``
        (state before that user message), or ``call:<turn>:<i>`` (state
        before that tool call). Silent — never asks the user. Returns
        ``{ok, checkpoint_id, restored_turn, content_hash, via,
        chat_truncate_to}``.
        """
        try:
            from services.plm import ai_vc
        except ImportError:
            return _err("no_vault", "AI version history is not available on this machine")
        try:
            out = ai_vc.plm_rollback(doc_id or None, to)
        except Exception as e:  # noqa: BLE001
            return _err("internal", f"{type(e).__name__}: {e}")
        if not out.get("ok"):
            return _err(str(out.get("reason") or "internal"),
                        str(out.get("hint") or ""))
        return _ok(**{k: v for k, v in out.items() if k != "ok"})

    # ------------------------------------------------------------------
    # T-R7 remotes / locks (folder kind)
    # ------------------------------------------------------------------

    @mcp.tool()
    def plm_add_remote(path: str, kind: str = "folder", name: str = "origin",
                       url: str = "") -> str:
        """Register a remote profile for team sync. ``kind`` ∈ ``folder`` |
        ``ssh`` | ``https`` | ``chudware-cloud`` (config-only). Folder remotes use
        a bare repo path. Returns ``{ok, remote}``."""
        try:
            from services.plm import remote as remote_mod
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        out = remote_mod.add_remote(path, name, path=url or None, kind=kind,
                                    url=url or None)
        if not out.get("ok"):
            return _err(str(out.get("reason") or "internal"),
                        str(out.get("hint") or ""))
        return _ok(**{k: v for k, v in out.items() if k != "ok"})

    @mcp.tool()
    def plm_sync(path: str, remote_id: str = "origin") -> str:
        """One-button team sync: pull others' changes, publish yours. Same-item
        collisions become a copy under a new name (never a merge). Returns
        ``{ok, pulled, pushed, conflicts_copied}``."""
        try:
            from services.plm import remote as remote_mod
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        out = remote_mod.sync(path, remote=remote_id)
        if not out.get("ok"):
            return _err(str(out.get("reason") or "internal"),
                        str(out.get("hint") or ""))
        return _ok(**{k: v for k, v in out.items() if k != "ok"})

    @mcp.tool()
    def plm_lock(path: str, item_id: str, remote_id: str = "origin") -> str:
        """Advisory lock on an item via the shared project (compare-and-swap).
        Returns ``{ok, lock}`` or ``{ok:false, reason:"locked"}`` when held."""
        try:
            from services.plm import remote as remote_mod
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        out = remote_mod.plm_lock(path, item_id, remote=remote_id)
        if not out.get("ok"):
            return _err(str(out.get("reason") or "internal"),
                        str(out.get("hint") or ""))
        return _ok(**{k: v for k, v in out.items() if k != "ok"})

    @mcp.tool()
    def plm_unlock(path: str, item_id: str, remote_id: str = "origin") -> str:
        """Release an advisory lock you hold. Returns ``{ok}``."""
        try:
            from services.plm import remote as remote_mod
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        out = remote_mod.plm_unlock(path, item_id, remote=remote_id)
        if not out.get("ok"):
            return _err(str(out.get("reason") or "internal"),
                        str(out.get("hint") or ""))
        return _ok(**{k: v for k, v in out.items() if k != "ok"})
