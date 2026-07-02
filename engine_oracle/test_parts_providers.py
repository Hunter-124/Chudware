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


# --------------------------------------------------------------------------------------------
# T-B1b: electronic-distributor providers (Nexar / Digi-Key / Mouser). Fully mocked HTTP; no
# network, no keys. Env-var fallbacks are cleared so CI is deterministic regardless of host env.
# --------------------------------------------------------------------------------------------

import contextlib
import os

_PROVIDER_ENV = ("NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET", "DIGIKEY_CLIENT_ID",
                 "DIGIKEY_CLIENT_SECRET", "MOUSER_API_KEY")


@contextlib.contextmanager
def _clean_env():
    saved = {k: os.environ.pop(k, None) for k in _PROVIDER_ENV}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# A representative Nexar `supSearchMpn` response. First seller = Digi-Key; its cheapest offer is
# the qty-1/qty-100 tier (min unit 0.31 < the 0.55 offer), so that offer's fields map onto the
# VendorPart.
_NEXAR_RESPONSE = {
    "data": {"supSearchMpn": {"results": [{"part": {
        "mpn": "LM317T",
        "manufacturer": {"name": "Texas Instruments"},
        "datasheetUrl": "https://example.com/lm317.pdf",
        "sellers": [
            {"company": {"name": "Digi-Key"}, "offers": [
                {"inventoryLevel": 5000, "moq": 1, "factoryLeadDays": 21, "prices": [
                    {"price": 0.42, "quantity": 1, "currency": "USD"},
                    {"price": 0.31, "quantity": 100, "currency": "USD"}]},
                {"inventoryLevel": 200, "moq": 10, "factoryLeadDays": 30, "prices": [
                    {"price": 0.55, "quantity": 10, "currency": "USD"}]},
            ]},
            {"company": {"name": "Mouser"}, "offers": []},
        ],
    }}]}},
}


def _nexar_mocked(response: dict = _NEXAR_RESPONSE) -> parts_providers.NexarProvider:
    """A NexarProvider whose OAuth + GraphQL seams are patched to run fully offline."""
    prov = parts_providers.NexarProvider()
    prov._get_token = lambda cid, secret: "test-token"          # type: ignore[method-assign]
    prov._post_graphql = lambda token, mpn: response            # type: ignore[method-assign]
    return prov


def test_nexar_maps_graphql_to_vendorpart_fields() -> None:
    with tempfile.TemporaryDirectory() as td, _clean_env():
        _point_config_at(Path(td), {"parts_providers": {"nexar": {
            "client_id": "cid", "client_secret": "secret"}}})
        rec = _nexar_mocked().fetch("LM317T")
        assert rec["supplier"] == "Digi-Key"          # first seller's company
        assert rec["part_number"] == "LM317T"
        assert rec["description"] == "Texas Instruments LM317T"
        assert rec["product_url"] == "https://example.com/lm317.pdf"
        assert rec["currency"] == "USD"
        assert rec["unit_price"] == 0.42              # qty-1 tier of the cheapest offer
        assert rec["price_breaks"] == [
            {"qty": 1, "unit_price": 0.42}, {"qty": 100, "unit_price": 0.31}]
        assert rec["stock"] == 5000
        assert rec["moq"] == 1
        assert rec["lead_time_days"] == 21
        assert rec["specs"] == {"manufacturer": "Texas Instruments"}
        assert isinstance(rec["fetched_ts"], float) and rec["fetched_ts"] > 0


def test_nexar_no_creds_raises_no_scrape() -> None:
    with tempfile.TemporaryDirectory() as td, _clean_env():
        _point_config_at(Path(td), None)             # no config at all + env cleared
        with pytest.raises(ProviderError) as ei:
            parts_providers.NexarProvider().fetch("LM317T")
        msg = ei.value.message
        assert "Nexar" in msg
        assert "scrape" in msg.lower()
        assert "NEXAR_CLIENT_ID" in msg


def test_nexar_empty_mpn_raises() -> None:
    with _clean_env():
        with pytest.raises(ProviderError):
            parts_providers.NexarProvider().fetch("   ")


def test_nexar_no_results_raises() -> None:
    with tempfile.TemporaryDirectory() as td, _clean_env():
        _point_config_at(Path(td), {"parts_providers": {"nexar": {
            "client_id": "cid", "client_secret": "secret"}}})
        prov = _nexar_mocked({"data": {"supSearchMpn": {"results": []}}})
        with pytest.raises(ProviderError) as ei:
            prov.fetch("NOSUCHPART")
        assert "no supply results" in ei.value.message.lower()


def test_nexar_env_fallback_supplies_creds() -> None:
    with tempfile.TemporaryDirectory() as td, _clean_env():
        _point_config_at(Path(td), None)             # no config file
        os.environ["NEXAR_CLIENT_ID"] = "cid"
        os.environ["NEXAR_CLIENT_SECRET"] = "secret"
        rec = _nexar_mocked().fetch("LM317T")         # mocked seams => no network
        assert rec["part_number"] == "LM317T"


def test_digikey_and_mouser_stub_when_keys_absent() -> None:
    with tempfile.TemporaryDirectory() as td, _clean_env():
        _point_config_at(Path(td), None)
        for prov in (parts_providers.DigiKeyProvider(), parts_providers.MouserProvider()):
            with pytest.raises(ProviderError) as ei:
                prov.fetch("LM317T")
            assert "not configured" in ei.value.message.lower()


def test_provider_chain_falls_through_to_none_without_creds() -> None:
    with tempfile.TemporaryDirectory() as td, _clean_env():
        _point_config_at(Path(td), None)
        assert parts_providers.default_pricing_chain().fetch("LM317T") is None


def test_provider_chain_returns_first_success() -> None:
    with tempfile.TemporaryDirectory() as td, _clean_env():
        _point_config_at(Path(td), {"parts_providers": {"nexar": {
            "client_id": "cid", "client_secret": "secret"}}})
        chain = parts_providers.ProviderChain([
            _nexar_mocked(), parts_providers.DigiKeyProvider(), parts_providers.MouserProvider()])
        rec = chain.fetch("LM317T")
        assert rec is not None and rec["part_number"] == "LM317T"


def test_provider_chain_skips_failing_then_succeeds() -> None:
    with tempfile.TemporaryDirectory() as td, _clean_env():
        _point_config_at(Path(td), {"parts_providers": {"nexar": {
            "client_id": "cid", "client_secret": "secret"}}})
        # Digi-Key (no creds) declines first; the mocked Nexar behind it succeeds.
        chain = parts_providers.ProviderChain([
            parts_providers.DigiKeyProvider(), _nexar_mocked()])
        rec = chain.fetch("LM317T")
        assert rec is not None and rec["supplier"] == "Digi-Key"


def test_electronic_provider_aliases_resolve() -> None:
    assert isinstance(parts_providers.get_provider("octopart"), parts_providers.NexarProvider)
    assert isinstance(parts_providers.get_provider("Digi-Key"), parts_providers.DigiKeyProvider)
    assert isinstance(parts_providers.get_provider("mouser"), parts_providers.MouserProvider)


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
