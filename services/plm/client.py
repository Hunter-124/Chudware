"""services.plm.client — the in-process VaultClient seam (contract §7.1).

``services/agent/plm_bridge.py`` (T-W18) imports :class:`PLMVaultClient` lazily
behind the ``AICAD_PLM_BRIDGE=1`` feature flag and duck-types it against its
``VaultClient`` Protocol. This class is the AI-authored twin of the MCP
``plm_checkpoint`` tool: it is the ONLY path that writes the §3.4 ``ai``
attribution block (from ``run_id`` / ``artifact_id``), keeping the MCP envelope
deliberately un-widened (contract §7.1).

Surface (frozen, must satisfy the Protocol verbatim):
    is_available() -> bool
    checkpoint(project_id, message, item_id=None, author_kind="ai",
               run_id=None, artifact_id=None) -> dict
    project_id_for_run(run_id) -> str | None

``project_id_for_run`` is BEST-EFFORT (contract §7.1): v1 keeps a small
run→project map that callers may populate; a ``None`` return simply causes the
bridge to queue the link. It is NOT a mandated registry.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

_MODEL_DEFAULT = "claude-fable-5"
_PROVIDER_DEFAULT = "anthropic"


class PLMVaultClient:
    """Thin in-process client over :class:`services.plm.vault.Vault` (contract §7.1)."""

    def __init__(self, provider: str = _PROVIDER_DEFAULT, model: str = _MODEL_DEFAULT) -> None:
        self._provider = provider
        self._model = model
        # best-effort run→project map (contract §7.1 — NOT a mandated registry)
        self._run_projects: dict[str, str] = {}
        # project_id → project folder path, learned as projects are opened
        self._project_paths: dict[str, str] = {}

    # -- registry helpers (best-effort) -----------------------------------

    def register_project(self, project_id: str, project_dir: str) -> None:
        """Associate a project_id with its folder so checkpoints can open it."""
        self._project_paths[project_id] = str(project_dir)

    def register_run(self, run_id: str, project_id: str) -> None:
        """Best-effort: associate a run with a project (contract §7.1)."""
        self._run_projects[run_id] = project_id

    def project_id_for_run(self, run_id: str) -> str | None:
        """Best-effort run→project lookup; ``None`` is a valid answer (§7.1)."""
        return self._run_projects.get(run_id)

    def ai_mode_for_project(self, project_id: str) -> str | None:
        """The project's assist tier (manual|assisted|managed), or None when
        the project is unknown/unreadable. Used by the PLM bridge to honor the
        tier before cutting AI entries (T-R3; plan §2)."""
        project_dir = self._project_paths.get(project_id)
        if project_dir is None or not Path(project_dir).is_dir():
            return None
        try:
            from services.plm.vault import Vault

            v = Vault.open(project_dir)
            try:
                return v.ai_mode()
            finally:
                v.close()
        except Exception:
            return None

    # -- VaultClient Protocol ---------------------------------------------

    def is_available(self) -> bool:
        """True only when the vault module is importable and functional.

        Never raises on unavailability — reports False (contract §7.1).
        """
        try:
            from services.plm.vault import Vault  # noqa: F401

            return True
        except Exception:
            return False

    def checkpoint(
        self,
        project_id: str,
        message: str,
        item_id: str | None = None,
        author_kind: str = "ai",
        run_id: str | None = None,
        artifact_id: str | None = None,
    ) -> dict[str, Any]:
        """Cut an AI-authored checkpoint, writing the §3.4 ai block.

        Returns the same success shape as the MCP ``plm_checkpoint`` tool:
        ``{ok, commit, entry}``; ``{ok:false, reason, hint}`` on failure.
        """
        from services.plm import assist, history
        from services.plm.vault import Vault, VaultError

        project_dir = self._project_paths.get(project_id)
        if project_dir is None or not Path(project_dir).is_dir():
            return {"ok": False, "reason": "invalid_project",
                    "hint": "that project could not be found"}

        # Assist-tier floor (T-R3, plan §2 / contract §8.4): an AI-authored
        # entry may only be cut when the project opted into AI-managed mode.
        # Manual buttons (author_kind="user") are always available.
        if author_kind == "ai":
            mode = self.ai_mode_for_project(project_id)
            if not assist.allowed(mode, "auto_checkpoint"):
                return {"ok": False, "reason": "invalid_mode",
                        "hint": "AI-managed versioning is turned off for this project"}

        author: dict[str, Any]
        ai_block: dict[str, Any] | None
        if author_kind == "ai":
            author = {"kind": "ai", "id": "agent", "display": "AI Assistant"}
            ai_block = history.make_ai_block(
                provider=self._provider, model=self._model,
                run_id=run_id, artifact_id=artifact_id,
            )
        else:
            author = {"kind": "user", "id": "user", "display": "User"}
            ai_block = None

        try:
            v = Vault.open(project_dir)
        except VaultError as e:
            return {"ok": False, "reason": e.reason, "hint": e.hint}
        try:
            res = v.checkpoint(message, author=author, ai=ai_block, item_id=item_id)
        except VaultError as e:
            return {"ok": False, "reason": e.reason, "hint": e.hint}
        finally:
            v.close()
        return {"ok": True, "commit": res["commit"], "entry": res["entry"]}
