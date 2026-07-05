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
plm_set_ai_mode (T-R3).
Remote/lock tools (plm_add_remote, plm_sync, plm_lock, plm_unlock) are T-R7.
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
    def plm_release(path: str, item_id: str, message: str) -> str:
        """Release an item: stamp the next revision letter, mark it Released, and
        freeze its exact form. A release note (``message``) is required.
        Returns ``{ok, commit, rev}``."""
        try:
            Vault, VaultError = _vault_module()
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        try:
            v = Vault.open(path)
            res = v.release(item_id, message)
            v.close()
        except VaultError as e:
            return _err(e.reason, e.hint)
        return _ok(commit=res["commit"], rev=res["rev"], entry=res["entry"])

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
        """Compare an item between two history points: a plain-language change
        list plus the semantic detail, and — when ``visual`` is true — a
        side-by-side visual of the two versions (match percentage + rendered
        images; drawings also get a 2D overlay). The visual half degrades
        gracefully on machines without the 3D engine.
        Returns ``{ok, doc_diff, summary, eval_compare?}``."""
        try:
            Vault, VaultError = _vault_module()
        except ImportError:
            return _err("no_vault", "version history is not available on this machine")
        try:
            from services.plm import compare as plm_compare_mod

            v = Vault.open(path)
            try:
                doc_a, doc_b = plm_compare_mod.docs_at(v, commit_a, commit_b, item_id)
            finally:
                v.close()
            sem = plm_compare_mod.semantic_compare(doc_a, doc_b)
            fields: dict[str, Any] = {"doc_diff": sem["doc_diff"],
                                      "summary": sem["summary"]}
            if visual:
                import tempfile

                od = out_dir or tempfile.mkdtemp(prefix="aicad_plm_compare_")
                fields["eval_compare"] = plm_compare_mod.visual_compare(doc_a, doc_b, od)
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
