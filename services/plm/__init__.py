"""services.plm — the PLM vault engine (Pillar 19, Workstream R, T-R2).

Implements the frozen ``contracts/plm.md`` (``aicad.plm/1``): a hidden,
git-backed, snapshot-based version-control vault at ``.aicadvault/`` per project.
Users never see git — the public surface is Project / History / Checkpoint /
Release / Restore (contract §1, §8).

Public API
----------
* :class:`services.plm.vault.Vault` — init/open/status of a project vault.
* :func:`services.plm.gitbind.open_binding` — the git binding (dulwich primary,
  subprocess-git optional accelerator; contract §7 OSS survey verdict).
* :class:`services.plm.client.PLMVaultClient` — the in-process seam that
  ``services/agent/plm_bridge.py`` (T-W18) imports (contract §7.1).

The ``plm_*`` MCP tools live in ``services/mcp/plm_tools.py``.

Security boundaries (2026-10 integration audit)
--------------------------------------------
Remote destinations must be approved locally with ``remote.add_remote``.
Approval lives in unversioned vault ``config.json``; a pulled manifest changing
the kind/URL is refused by sync, locks and live joins until explicitly approved.
Pre-existing destinations also need this one-time local approval; credentials
must never appear in URL userinfo, query tokens or versioned remote fields.
Rotate credentials previously shared in history (history is not rewritten).

Git uses validated names/revisions, fixed transport schemes, a minimal process
environment and disabled hooks/fsmonitor/external diff. Reserved project control
paths cannot be tracked/restored. Snapshot attachment budgets are 4096 members,
64 MiB/member, 256 MiB expanded aggregate and 1000:1 compression ratio; solved
state is skipped without opening it, leaving warm live caches untouched.
Box mirrors regular files through no-follow directory/file handles, hashing and
uploading the same bounded verified bytes. Live relays require a bearer token
even on loopback; native clients omit Origin and browser clients require an exact
``LivePlmServer.allowed_origins`` / CLI ``--origin`` allowlist.
"""
from __future__ import annotations

__all__ = ["Vault", "PLMVaultClient", "open_binding"]


def __getattr__(name: str):  # lazy re-export (avoid importing dulwich at package import)
    if name == "Vault":
        from services.plm.vault import Vault

        return Vault
    if name == "PLMVaultClient":
        from services.plm.client import PLMVaultClient

        return PLMVaultClient
    if name == "open_binding":
        from services.plm.gitbind import open_binding

        return open_binding
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
