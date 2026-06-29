"""Supplier-provider seam tests.

No network. Asserts the provider seam fails closed (clear ProviderError) when credentials
are absent or disabled, and that the stubbed API path is reached only when configured.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from engine import parts_providers
from engine.parts_providers import ProviderError


def _point_config_at(tmp: Path, data: dict | None) -> None:
    cfg = tmp / "config.json"
    if data is not None:
        cfg.write_text(json.dumps(data), encoding="utf-8")
    parts_providers.paths.CONFIG = cfg


def test_mcmaster_no_config_raises_clear_error() -> None:
    with tempfile.TemporaryDirectory() as td:
        _point_config_at(Path(td), None)  # no config file at all
        prov = parts_providers.McMasterProvider()
        with pytest.raises(ProviderError) as ei:
            prov.fetch("91251A542")
        msg = ei.value.message
        assert "McMaster" in msg
        assert "scrape" in msg.lower() or "scraping" in msg.lower()
        assert "config.json" in msg
        assert "91251A542" in msg


def test_mcmaster_disabled_raises() -> None:
    with tempfile.TemporaryDirectory() as td:
        _point_config_at(Path(td), {"parts_providers": {"mcmaster": {
            "enabled": False, "api_url": "https://api.mcmaster.com",
            "username": "u", "password": "p"}}})
        with pytest.raises(ProviderError):
            parts_providers.McMasterProvider().fetch("123")


def test_mcmaster_enabled_with_creds_hits_stub() -> None:
    with tempfile.TemporaryDirectory() as td:
        _point_config_at(Path(td), {"parts_providers": {"mcmaster": {
            "enabled": True, "api_url": "https://api.mcmaster.com",
            "username": "u", "password": "p", "client_cert": "c.pfx"}}})
        # Credentials present -> reaches the intentional API stub (not wired yet).
        with pytest.raises(ProviderError) as ei:
            parts_providers.McMasterProvider().fetch("91251A542")
        assert "not wired" in ei.value.message.lower()


def test_other_providers_not_configured() -> None:
    for supplier in ("grabcad", "traceparts"):
        with pytest.raises(ProviderError) as ei:
            parts_providers.get_provider(supplier).fetch("X")
        assert "not yet configured" in ei.value.message


def test_unknown_supplier_raises() -> None:
    with pytest.raises(ProviderError) as ei:
        parts_providers.get_provider("acme-bolts")
    assert "no provider" in ei.value.message.lower()


def test_supplier_alias_resolves() -> None:
    prov = parts_providers.get_provider("McMaster-Carr")
    assert isinstance(prov, parts_providers.McMasterProvider)


def test_worker_method_soft_fails() -> None:
    with tempfile.TemporaryDirectory() as td:
        _point_config_at(Path(td), None)
        res = parts_providers._vendor_part_fetch(
            {"supplier": "mcmaster", "part_number": "91251A542"}, lambda e: None)
        assert res["ok"] is False
        assert "McMaster" in res["error"]
    assert "vendor_part_fetch" in parts_providers.WORKER_METHODS


def main() -> None:
    # Allow running without pytest for parity with the other test modules.
    import traceback
    fns = [v for k, v in globals().items() if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
        except Exception:  # noqa: BLE001
            failed += 1
            traceback.print_exc()
    if failed:
        raise SystemExit(f"{failed} provider test(s) failed")
    print("PARTS PROVIDER TESTS PASSED")


if __name__ == "__main__":
    main()
