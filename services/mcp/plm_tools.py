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
plm_set_ai_mode, plm_item_set + plm_where_used,
plm_timeline + plm_rollback + plm_rollback_check (AI-VC), remotes/locks,
and the authoritative live seam: plm_live_join/propose/poll/presence/leave.

plm_where_used note: §7 reserves this name for the Hub; this module ships
the thin envelope early ({ok, consumers} over the derived index in
services/plm/whereused.py). Dropping to in-process-only is a two-line change
if §7 is read strictly.
"""
from __future__ import annotations

import json
import os
from functools import wraps
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from ._envelopes import compact_err as _err, sorted_ok as _ok
from .cad_common import require_mapping

# A live general-lane replay ended with bare plm_checkpoint(), after earlier bare
# plm_open() and file-as-path plm_item_add() calls. ``path`` means the project root
# for this vault-backed family, so resolve the harness-owned context once here
# instead of making each model guess it.

def _resolve_project_path(path: str = "") -> tuple[str | None, str | None]:
    """Resolve an omitted vault path from the active agent project context."""
    if str(path).strip():
        return str(path), None
    from services.agent.project_context import resolve_project_root

    for key in ("CHUDWARE_PROJECT_DIR", "CHUDWARE_FS_ROOT"):
        start = os.environ.get(key, "").strip()
        if not start:
            continue
        root = resolve_project_root(start)
        if root.is_dir():
            return str(root), None
    return None, _err(
        "project_root_unavailable",
        "path was omitted and no project root could be resolved; pass "
        "path='<project-root>' or set CHUDWARE_PROJECT_DIR/CHUDWARE_FS_ROOT so "
        "services.agent.project_context.resolve_project_root() can resolve it",
    )


def _with_project_path(fn: Any) -> Any:
    """Default the first (project ``path``) argument without hiding tool errors."""
    @wraps(fn)
    def wrapped(path: str = "", *args: Any, **kwargs: Any) -> str:
        resolved, error = _resolve_project_path(path)
        if error is not None:
            return error
        return fn(resolved, *args, **kwargs)

    return wrapped


def register_plm_tools(mcp: FastMCP) -> None:
    """Register the plm_* tool family onto *mcp* (contract §7)."""

    # lazy import so the MCP server starts even if dulwich isn't provisioned;
    # tools degrade to a contract-shaped no_vault/reason envelope.
    def _vault_module():
        from services.plm.vault import Vault, VaultError

        return Vault, VaultError

    @mcp.tool()
    @_with_project_path
    def plm_init(path: str = "") -> str:
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
    @_with_project_path
    def plm_open(path: str = "") -> str:
        """Open a project folder, auto-creating history if absent (zero-config).
        Returns ``{ok, project_id, manifest}``."""
        return plm_init(path)  # open == init (both zero-config, idempotent)

    @mcp.tool()
    @_with_project_path
    def plm_save(path: str = "", kind: str = "auto") -> str:
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
    @_with_project_path
    def plm_checkpoint(path: str = "", message: str = "", item_id: str = "") -> str:
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
    @_with_project_path
    def plm_release(path: str = "", item_id: str = "", message: str = "",
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
    @_with_project_path
    def plm_item_set(path: str = "", item_id: str = "", part_number: str = "",
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
    @_with_project_path
    def plm_item_add(path: str = "", file_path: str = "", folder_path: str = "",
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
        project_path = Path(path)
        if project_path.is_file():
            from services.plm import doctypes
            from services.plm.vault import CONTAINER_ITEM_ADD_HINT

            if doctypes.detect_doc_type(str(project_path)) in doctypes.CONTAINER_DOC_TYPES:
                return _err("invalid_mode", CONTAINER_ITEM_ADD_HINT)
            ancestor = project_path.parent
            while not ancestor.is_dir() and ancestor != ancestor.parent:
                ancestor = ancestor.parent
            rel_to_ancestor = project_path.relative_to(ancestor).as_posix()
            corrected = (
                f"plm_item_add(path={json.dumps(str(ancestor))}, "
                f"file_path={json.dumps(rel_to_ancestor)})"
            )
            return _err(
                "invalid_project",
                f"{corrected}; path is the PROJECT ROOT for every plm_* tool, "
                "while file_path/folder_path is the thing being tracked",
            )
        rel = file_path or folder_path
        if not rel:
            worked = (
                f"plm_item_add(path={json.dumps(str(path))}, "
                'file_path="documents/spec.md")'
            )
            return _err(
                "invalid_mode",
                f"choose file_path or folder_path for the thing being tracked; "
                f"for example, {worked}. path is the PROJECT ROOT for every "
                "plm_* tool, while file_path/folder_path is the thing being tracked",
            )
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
    @_with_project_path
    def plm_link_repo(path: str = "", url: str = "", name: str = "",
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
    @_with_project_path
    def plm_plan(path: str = "", op: str = "list", entity: str = "task",
                 data: dict | str | None = None, id: str = "") -> str:
        """Plan the work: read or change the project's tasks, issues and
        milestones (stored with the project, so they version and restore with
        it). ``op`` ∈ ``list`` | ``create`` | ``update`` | ``delete``;
        ``entity`` ∈ ``task`` | ``issue`` | ``milestone``. ``data`` carries the
        fields (title, status/priority, assignee, links to items/revisions, …).
        Returns ``{ok, items, count, entity?, deleted_id?}`` — ``count`` is
        the number of remaining ``entity`` items, ``entity`` is the created/
        updated record, ``deleted_id`` confirms which id was removed."""
        # A stringified `data` (JSON or Python-repr encoded) used to reach
        # planning._build's bare `data.get("title", "")` unchanged and crash with
        # "'str' object has no attribute 'get'" — the LIVE CRASH class. Coerce/validate
        # here so the caller gets a named, fixable error instead of a transport failure.
        if data is not None:
            try:
                data = require_mapping(
                    data, "data",
                    "a dict of fields (title, status/priority, assignee, links to "
                    "items/revisions, …)")
            except ValueError as e:
                return _err("invalid_data", str(e))
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
        fields = dict(res)
        fields["count"] = len(fields.get("items") or [])
        if op == "delete" and "entity" not in fields:
            fields["deleted_id"] = id
        return _ok(**fields)

    @mcp.tool()
    @_with_project_path
    def plm_where_used(path: str = "", item_id: str = "",
                       transitive: bool = False) -> str:
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
    @_with_project_path
    def plm_history(path: str = "", item_id: str = "", limit: int = 0) -> str:
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
    @_with_project_path
    def plm_restore(path: str = "", commit: str = "", item_id: str = "") -> str:
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
    @_with_project_path
    def plm_status(path: str = "") -> str:
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
    @_with_project_path
    def plm_compare(path: str = "", commit_a: str = "", commit_b: str = "",
                    item_id: str = "", visual: bool = False, out_dir: str = "",
                    views: str = "") -> str:
        """Compare an item between two history points — including a RENDERED
        GRAPHICAL DIFF of the geometry that changed.

        Generic (non-CAD) items — docs, code, links — route to a
        content/text diff (``mode:"generic"``, plain-language summary, and a
        unified/office text diff when the item is single-file). CAD items route
        to the semantic ``doc_diff`` comparison, and — when ``visual`` is true —
        to real diff IMAGES you can look at:

        * parts/assemblies: ``diff_3d`` (added geometry green, removed red,
          unchanged grey, ISO), ``diff_3d_changes_only`` (the changes with no
          surrounding model), ``diff_3d_views`` (the same diff from ISO/FRONT/
          TOP on one shared camera so the frames register), plus
          ``match_pct`` / ``added_volume_mm3`` / ``removed_volume_mm3``.
        * drawings and ECAD boards/schematics: ``diff_2d``, a vector overlay of
          both sheets/layouts with per-stroke ``counts``. ECAD needs no engine.
        * ``diff_pixel``: registered pixel diff of the two plain renders — the
          universal fallback.
        * ``image_a`` / ``image_b`` / ``side_by_side``: the two plain renders.

        ``views`` optionally narrows the 3D view set (comma-separated, e.g.
        ``"ISO,FRONT"``). ``out_dir`` chooses where the PNGs land (a temp dir by
        default). Every returned path exists on disk; the visual half degrades to
        ``{degraded: true, reason, hint}`` on machines without the 3D engine and
        never fails the compare.
        Returns ``{ok, mode, summary, text_diff?}`` for generic items or
        ``{ok, doc_diff, summary, eval_compare?, out_dir?}`` for CAD items."""
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
                        want = [s.strip() for s in views.split(",") if s.strip()] or None
                        fields["eval_compare"] = plm_compare_mod.visual_compare(
                            doc_a, doc_b, od, want)
                        fields["out_dir"] = od
            finally:
                v.close()
        except VaultError as e:
            return _err(e.reason, e.hint)
        return _ok(**fields)

    @mcp.tool()
    @_with_project_path
    def plm_set_ai_mode(path: str = "", mode: str = "") -> str:
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

    @mcp.tool()
    def plm_rollback_check(to: str, doc_id: str = "", file_path: str = "",
                           chat_size: int = -1) -> str:
        """Preview a rollback WITHOUT applying it — the confirmation data.

        Same ``to`` forms as ``plm_rollback``. Returns ``{ok,
        discarded_checkpoints, discarded_turns, discarded_messages,
        geometry_rollback, chat_truncate_to, checkpoint_label,
        checkpoint_when, file:{path, provenance_known,
        externally_modified, current_mtime, recorded_mtime}}``.
        ``externally_modified`` is true only when the backing file's
        current hash differs from the checkpoint's recorded hash; null
        when provenance is unknown or unverifiable. ``file_path`` is the
        caller-supplied fallback for checkpoints recorded before file
        provenance existed; ``chat_size`` (<0 = unknown) drives the
        discarded-messages count."""
        try:
            from services.plm import ai_vc
        except ImportError:
            return _err("no_vault", "AI version history is not available on this machine")
        try:
            out = ai_vc.plm_rollback_check(
                doc_id or None, to,
                file_path=file_path or None,
                chat_size=chat_size if chat_size >= 0 else None)
        except Exception as e:  # noqa: BLE001
            return _err("internal", f"{type(e).__name__}: {e}")
        if not out.get("ok"):
            return _err(str(out.get("reason") or "internal"),
                        str(out.get("hint") or ""))
        return _ok(**{k: v for k, v in out.items() if k != "ok"})

    # ------------------------------------------------------------------
    # Remotes / locks (folder kind)
    # ------------------------------------------------------------------

    @mcp.tool()
    @_with_project_path
    def plm_add_remote(path: str = "", kind: str = "folder", name: str = "origin",
                       url: str = "", display_name: str = "",
                       account_id: str = "") -> str:
        """Register a team-sync profile with optional account identity.

        ``kind`` is ``folder`` | ``ssh`` | ``https`` | ``plm-server`` |
        ``chudware-cloud``. Tokens for live servers are supplied only at join.
        """
        try:
            from services.plm import remote as remote_mod
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        out = remote_mod.add_remote(
            path,
            name,
            path=url or None,
            kind=kind,
            url=url or None,
            display_name=display_name or None,
            account_id=account_id or None,
        )
        if not out.get("ok"):
            return _err(str(out.get("reason") or "internal"),
                        str(out.get("hint") or ""))
        return _ok(**{k: v for k, v in out.items() if k != "ok"})

    @mcp.tool()
    @_with_project_path
    def plm_sync(path: str = "", remote_id: str = "origin") -> str:
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
    @_with_project_path
    def plm_lock(path: str = "", item_id: str = "",
                 remote_id: str = "origin", force: bool = False) -> str:
        """Acquire an advisory item lock.

        By default another live holder is refused. ``force=True`` explicitly
        replaces that holder and returns the prior identity under ``overrode``.
        """
        try:
            from services.plm import remote as remote_mod
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        out = remote_mod.plm_lock(
            path, item_id, remote=remote_id, force=bool(force)
        )
        if not out.get("ok"):
            return _err(str(out.get("reason") or "internal"),
                        str(out.get("hint") or ""))
        return _ok(**{k: v for k, v in out.items() if k != "ok"})

    @mcp.tool()
    @_with_project_path
    def plm_unlock(path: str = "", item_id: str = "",
                   remote_id: str = "origin") -> str:
        """Release an advisory lock you hold. Returns ``{ok, item_id,
        remote_id}`` confirming which lock was released."""
        try:
            from services.plm import remote as remote_mod
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        out = remote_mod.plm_unlock(path, item_id, remote=remote_id)
        if not out.get("ok"):
            return _err(str(out.get("reason") or "internal"),
                        str(out.get("hint") or ""))
        return _ok(item_id=item_id, remote_id=remote_id,
                   **{k: v for k, v in out.items() if k != "ok"})

    # ------------------------------------------------------------------
    # Authoritative live co-editing (aicad.plm-live/1)
    # ------------------------------------------------------------------

    def _live_worker(method: str, payload: dict[str, Any]) -> str:
        from services.plm import worker_rpc

        result = worker_rpc.WORKER_METHODS[method](payload, lambda *_args: None)
        return json.dumps(result, indent=2, sort_keys=True)

    @mcp.tool()
    @_with_project_path
    def plm_live_join(
        path: str = "",
        remote_id: str = "origin",
        item_id: str = "",
        participant: dict[str, Any] | None = None,
        token: str = "",
        local_doc_id: str = "",
    ) -> str:
        """Join an authoritative live item session.

        ``participant`` is ``{id, display, account_id}``. ``token`` is used for
        this connection only and is never saved in the project profile. An
        optional ``local_doc_id`` attaches the already-open editor document.
        Returns the session id, authoritative sequence, and attached/replacement
        local document id.
        """
        return _live_worker("plm_live_join", {
            "path": path,
            "remote_id": remote_id,
            "item_id": item_id,
            "participant": participant,
            "token": token or None,
            "local_doc_id": local_doc_id or None,
        })

    @mcp.tool()
    @_with_project_path
    def plm_live_propose(
        path: str = "",
        remote_id: str = "origin",
        item_id: str = "",
        participant: dict[str, Any] | None = None,
        ops: list[dict[str, Any]] | None = None,
        client_op_id: str = "",
    ) -> str:
        """Propose one atomic operation batch and wait for its ordered commit.

        Returns the normal engine ``ops_response`` for DocMirror ingestion.
        Reusing ``client_op_id`` retries the same proposal idempotently.
        """
        return _live_worker("plm_live_propose", {
            "path": path,
            "remote_id": remote_id,
            "item_id": item_id,
            "participant": participant,
            "ops": ops,
            "client_op_id": client_op_id or None,
        })

    @mcp.tool()
    @_with_project_path
    def plm_live_poll(
        path: str = "",
        remote_id: str = "origin",
        item_id: str = "",
        participant: dict[str, Any] | None = None,
        limit: int = 100,
    ) -> str:
        """Drain complete, atomically applied live commit/snapshot events."""
        return _live_worker("plm_live_poll", {
            "path": path,
            "remote_id": remote_id,
            "item_id": item_id,
            "participant": participant,
            "limit": limit,
        })

    @mcp.tool()
    @_with_project_path
    def plm_live_presence(
        path: str = "",
        remote_id: str = "origin",
        item_id: str = "",
        participant: dict[str, Any] | None = None,
        cursor: dict[str, Any] | None = None,
        selection: dict[str, Any] | None = None,
        viewport: dict[str, Any] | None = None,
    ) -> str:
        """Publish bounded ephemeral cursor, selection, and viewport presence."""
        return _live_worker("plm_live_presence", {
            "path": path,
            "remote_id": remote_id,
            "item_id": item_id,
            "participant": participant,
            "cursor": cursor,
            "selection": selection,
            "viewport": viewport,
        })

    @mcp.tool()
    @_with_project_path
    def plm_live_leave(
        path: str = "",
        remote_id: str = "origin",
        item_id: str = "",
        participant: dict[str, Any] | None = None,
    ) -> str:
        """Leave a live item session and release its ephemeral presence."""
        return _live_worker("plm_live_leave", {
            "path": path,
            "remote_id": remote_id,
            "item_id": item_id,
            "participant": participant,
        })

    # ------------------------------------------------------------------
    # Box.com mirror (services/plm/box_mirror.py) — optional cloud copy of
    # the project's saved files. Live edits stay local; Box is kept up to
    # date by save-path hooks plus this manual surface.
    # ------------------------------------------------------------------

    @mcp.tool()
    @_with_project_path
    def box_sync(path: str = "") -> str:
        """Mirror the project's saved files up to the configured Box.com
        folder now (uploads new/changed files; never downloads). Requires Box
        mirroring to be enabled and connected in Settings → Integrations.
        Returns ``{ok, projects:[{project, uploaded, updated, unchanged}]}``."""
        try:
            from services.plm import box_mirror
        except ImportError:
            return _err("no_box", "the Box mirror is not available on this machine")
        out = box_mirror.sync_project(path)
        if not out.get("ok"):
            return _err(str(out.get("reason") or "internal"),
                        str(out.get("hint") or "; ".join(out.get("errors") or [])))
        return _ok(**{k: v for k, v in out.items() if k != "ok"})

    @mcp.tool()
    def box_status(path: str = "") -> str:
        """Box.com mirror status: whether mirroring is enabled/connected, the
        connected account, pending spooled projects, and — when ``path`` (or
        the active project) resolves — that project's last sync/error.
        Returns ``{ok, enabled, configured, connected?, account?, pending,
        project?}``."""
        try:
            from services.plm import box_mirror
        except ImportError:
            return _err("no_box", "the Box mirror is not available on this machine")
        resolved, _ = _resolve_project_path(path)
        out = box_mirror.status(resolved, live=True)
        return _ok(**{k: v for k, v in out.items() if k != "ok"})
