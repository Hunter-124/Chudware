"""services/browser/adapters — the SourceAdapter contract + registry (T-X2).

Every adapter searches a source site (under the user's own saved browser
session, T-X1) for candidate parts/documents and acquires a chosen result,
landing the file locally with a provenance envelope styled on the
``aicad.calc/1`` envelope (``contracts/advisor.md`` §7) and the ``research_*``
answer envelope (``contracts/research_tools.md`` §1): every acquired artifact
carries ``{source_url, fetched_ts, session_profile_used, license_note}``.

Adapters are DATA-heavy, CODE-light — nearly everything that differs between
GrabCAD and TraceParts lives in each module's site config (URLs, regexes,
license note), not in divergent control flow. No adapter imports Playwright
directly; only :mod:`services.browser.session`'s three entry points.

This package intentionally does NOT import ``services.agent`` (workstream W's
surface, out of scope here) — the ``login_required`` question below matches
the frozen ``contracts/interaction.md`` §1/§2 shape by hand so adapters stay
decoupled from the agent loop's implementation.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from engine import paths
from engine.research.common import now_ts

from ..profiles import slug


class SourceAdapter(Protocol):
    key: str

    def search(self, query: str) -> list[dict]: ...

    def acquire(self, result: dict) -> dict: ...


_REGISTRY: dict[str, "SourceAdapter"] = {}


def register(adapter: "SourceAdapter") -> "SourceAdapter":
    """Register ``adapter`` under its ``key`` and return it (use as a decorator-ish call
    at module load: ``ADAPTER = register(GrabCADAdapter())``)."""
    _REGISTRY[adapter.key] = adapter
    return adapter


def get_adapter(key: str) -> "SourceAdapter | None":
    return _REGISTRY.get(key)


def all_adapters() -> list["SourceAdapter"]:
    return list(_REGISTRY.values())


def provenance_envelope(*, source_url: str, session_profile_used: str, license_note: str,
                        artifact_path: str | None = None, fetched_ts: str | None = None,
                        **extra: Any) -> dict:
    """The T-X2 acquisition envelope. Additive fields pass through ``extra`` and sit
    alongside the required four, matching the "helpers add fields, never rename them"
    rule shared by ``contracts/advisor.md`` §7 and ``contracts/research_tools.md`` §1."""
    env: dict[str, Any] = {
        "ok": True,
        "source_url": source_url,
        "fetched_ts": fetched_ts or now_ts(),
        "session_profile_used": session_profile_used,
        "license_note": license_note,
    }
    if artifact_path is not None:
        env["artifact_path"] = artifact_path
    env.update(extra)
    return env


def login_required(site: str, url: str) -> dict:
    """The clean "sign in needed" degradation shape — an `approval`-kind question per
    ``contracts/interaction.md`` §1/§2, embedded in an ``ok:false`` envelope. Never
    raised; a caller decides how/whether to route ``ask_user`` to the user."""
    question = {
        "id": f"browser_login.{slug(site)}",
        "kind": "approval",
        "prompt": f"Sign into {site} to continue? A browser window will open for you.",
        "suggested": True,
        "required": True,
        "why": f"{url} requires an active {site} session; none is saved yet.",
        "proposal": f"Open a browser window for you to log into {site}.",
    }
    return {"ok": False, "reason": "login_required", "hint": question["prompt"], "ask_user": question}


def terms_confirmation_required(site: str, url: str, terms_summary: str) -> dict:
    """Same shape as :func:`login_required` for a per-download terms click-through this
    adapter must not accept on the user's behalf (T-X2 TraceParts case)."""
    question = {
        "id": f"browser_terms.{slug(site)}",
        "kind": "approval",
        "prompt": f"Accept {site}'s terms to download this file?",
        "suggested": False,
        "required": True,
        "why": f"{url} — {terms_summary}",
        "proposal": f"Accept the terms shown on {url} to proceed with this download.",
    }
    return {"ok": False, "reason": "terms_confirmation_required", "hint": question["prompt"],
            "ask_user": question}


def is_login_wall(content: str, markers: tuple[str, ...]) -> bool:
    """True when fetched HTML looks like a login page rather than the requested content.

    A simple substring heuristic (each site config supplies its own marker
    strings) — real markup verification is T-X6's gated integration-test job,
    not something this v1 can confirm against a live site without a login.
    """
    return any(marker in content for marker in markers)


def download_dir(site: str) -> Path:
    """Local landing directory for artifacts acquired from ``site``.

    A sibling of ``vendor_parts`` under the app data dir (``engine.paths``) —
    T-X3 wires acquired files into the VendorPart registry proper; this is
    just where the bytes land first.
    """
    d = paths.BROWSER_DOWNLOADS / slug(site)
    d.mkdir(parents=True, exist_ok=True)
    return d
