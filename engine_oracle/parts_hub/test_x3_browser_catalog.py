"""Tests for T-X3: the import_file placer + browser catalog provider hub wiring.

No real core, no real Playwright/network: core_bridge.call and the browser
adapters are monkeypatched/faked directly.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))
_SVC_MCP = _REPO / "services" / "mcp"
if str(_SVC_MCP) not in sys.path:
    sys.path.insert(0, str(_SVC_MCP))


# ── _place_import_file (place.py) ────────────────────────────────────────────

def test_place_import_file_missing_path():
    from engine_oracle.parts_hub.place import _place_import_file
    r = _place_import_file({"kind": "import_file"}, None, None)
    assert r["ok"] is False
    assert "path" in r["reason"]


def test_place_import_file_file_not_found():
    from engine_oracle.parts_hub.place import _place_import_file
    r = _place_import_file({"kind": "import_file", "path": "/no/such/file.step"}, None, None)
    assert r["ok"] is False
    assert "not found" in r["reason"]


def test_place_import_file_success(tmp_path, monkeypatch):
    fake_step = tmp_path / "widget.step"
    fake_step.write_text("ISO-10303-21;")

    import core_bridge as cb  # type: ignore
    monkeypatch.setattr(cb, "call", lambda verb, params: {"doc_id": "doc-abc", "body_count": 1})

    from engine_oracle.parts_hub.place import _place_import_file
    r = _place_import_file({"kind": "import_file", "path": str(fake_step)}, None, None)

    assert r["ok"] is True
    assert r["editable"] is True
    assert r["doc_id"] == "doc-abc"
    assert r["entity_id"] == "doc-abc"
    assert "reverse_engineer" in r["detail"]["hint"]


def test_place_import_file_rpc_failure(tmp_path, monkeypatch):
    fake_step = tmp_path / "widget.step"
    fake_step.write_text("ISO-10303-21;")

    import core_bridge as cb  # type: ignore

    def _raise(verb, params):
        raise RuntimeError("no core")

    monkeypatch.setattr(cb, "call", _raise)

    from engine_oracle.parts_hub.place import _place_import_file
    r = _place_import_file({"kind": "import_file", "path": str(fake_step)}, None, None)
    assert r["ok"] is False
    assert "import_file RPC failed" in r["reason"]


def test_place_dispatches_import_file_kind():
    """place.py's public dispatch table must route the new kind."""
    from engine_oracle.parts_hub.place import _PLACERS
    assert "import_file" in _PLACERS


# ── hub wiring (bootstrap registers browser catalog providers) ──────────────

def _make_hub():
    from engine_oracle.parts_hub.hub import PartsHub
    hub = PartsHub()
    hub.bootstrap()
    return hub


def test_bootstrap_never_raises_when_browser_package_present():
    """bootstrap() must succeed even though services.browser has no saved profiles here."""
    hub = _make_hub()
    assert hub is not None


def test_bootstrap_registers_browser_catalog_providers():
    hub = _make_hub()
    provider_ids = {getattr(p, "provider_id", None) for p in hub._registry.providers()}
    assert "browser_grabcad" in provider_ids
    assert "browser_traceparts" in provider_ids


@pytest.mark.live
def test_zero_profile_search_contributes_nothing(tmp_path, monkeypatch):
    """DoD: no saved browser profile anywhere -> catalog providers contribute zero
    results, silently -- part_search behavior for an otherwise-empty query is
    unaffected by T-X3 (same shape as before this task: an empty live_results list
    from these providers, no ask_user noise).

    LIVE-NETWORK (gated 2026-07-07): this test makes a real vendor search, so it
    belongs behind the `live` marker like the other live-adapter tests — it was
    running in the deterministic suite and doing a ~2min Amazon round-trip.
    FLAG for the parts team: the ``catalog_results == []`` assertion is now
    OUTDATED. Two design changes invalidate it: (1) anonymous public-catalog
    browsing is allowed (profiles auto-created; live-probe 2026-07-07), and (2)
    browser_fetch now waits for the SPA DOM to render before snapshotting
    (session.py DEFAULT_RENDER_SETTLE_S), so Amazon returns filler/suggestion
    cards even for a nonsense query. Redefine the intended behavior — likely
    "no ask_user noise / no error" rather than "zero results" — then re-enable."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    import importlib
    from engine import paths as engine_paths
    importlib.reload(engine_paths)

    hub = _make_hub()
    results = hub.search("zzz_definitely_not_a_real_part_zzz")
    catalog_results = [r for r in results if isinstance(r, dict) and r.get("domain") == "catalog"]
    assert catalog_results == []

    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)
