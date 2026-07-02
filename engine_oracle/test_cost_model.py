"""Tests for :mod:`engine.cost_model` and :mod:`engine.quote_providers` (§3/§4 of
``aicad.sourcing/1``), plus the :func:`engine.bom.resolve_costs` fold-in.

Fully offline: no network, no API keys, no DLL. The `_http_get_json` (cost_model) and
`_http_post_json` (quote_providers) seams are monkeypatched where a "key present" path is
exercised; with no key configured neither seam is ever called.
"""
from __future__ import annotations

import pytest

from engine import bom, cost_model, quote_providers


# --- cost_model: material_cost -----------------------------------------------------------------

def test_material_cost_plausible():
    volume = 1000.0  # mm3 == 1 cm3
    steel = cost_model.material_cost(volume, "steel")
    aluminum = cost_model.material_cost(volume, "aluminum")
    cedar = cost_model.material_cost(volume, "cedar")  # alias for cedar_lumber

    assert steel > 0
    assert aluminum > 0
    assert cedar > 0
    assert steel > aluminum

    # hand-computed: 1000mm3 -> 1cm3 -> mass_kg = 7.85 g/cm3 / 1000 = 7.85e-3 kg; rate 2.0 $/kg
    assert steel == pytest.approx(7.85e-3 * 2.0)
    assert steel == pytest.approx(0.0157)


def test_material_cost_unknown_raises():
    with pytest.raises(cost_model.CostModelError):
        cost_model.material_cost(1000.0, "unobtainium")
    with pytest.raises(cost_model.CostModelError):
        cost_model.material_cost(-1.0, "steel")


# --- cost_model: machining_cost ------------------------------------------------------------------

def test_machining_cost():
    assert cost_model.machining_cost(3600) == pytest.approx(75.0)
    assert cost_model.machining_cost(600, 90) == pytest.approx(15.0)
    assert cost_model.machining_cost(0) == pytest.approx(0.0)


# --- cost_model: estimate_made_cost --------------------------------------------------------------

def test_estimate_made_cost_success():
    result = cost_model.estimate_made_cost(
        volume_mm3=50000.0, material="aluminum", cycle_time_s=1200.0, quantity=10,
    )
    assert result["ok"] is True
    assert result["source"] == "model"
    assert result["currency"] == "USD"
    assert result["unit_cost"] == pytest.approx(
        result["material_cost"] + result["machine_cost"] + result["setup_cost"]
    )
    assert result["provenance"]["citation"]


def test_estimate_made_cost_unknown_material():
    result = cost_model.estimate_made_cost(
        volume_mm3=50000.0, material="unobtainium", cycle_time_s=1200.0,
    )
    assert result["ok"] is False
    assert result["unit_cost"] is None
    assert "reason" in result
    assert "hint" in result
    assert "material_cost" not in result


def test_estimate_made_cost_missing_input():
    result = cost_model.estimate_made_cost(
        volume_mm3=50000.0, material="aluminum", cycle_time_s=None,
    )
    assert result["ok"] is False
    assert result["unit_cost"] is None
    assert "reason" in result
    assert "cycle_time_s" in result["reason"]
    assert "hint" in result


# --- cost_model: live_metal_price -----------------------------------------------------------------

def test_live_metal_price_no_key(monkeypatch):
    monkeypatch.delenv("COMMODITIES_API_KEY", raising=False)
    assert cost_model.live_metal_price("ALUM") is None


def test_live_metal_price_mocked(monkeypatch):
    monkeypatch.setenv("COMMODITIES_API_KEY", "test-key")

    def fake_http_get_json(url, timeout=cost_model.HTTP_TIMEOUT):
        return {"data": {"rates": {"ALUM": 0.0004}}}

    monkeypatch.setattr(cost_model, "_http_get_json", fake_http_get_json)
    price = cost_model.live_metal_price("ALUM")
    assert price == pytest.approx(2500.0)


# --- quote_providers: no-key stub (§0 degradation) ------------------------------------------------

def test_quote_stub_no_key(monkeypatch):
    monkeypatch.delenv("XOMETRY_API_KEY", raising=False)
    monkeypatch.delenv("JLCPCB_KEY", raising=False)

    def boom(*args, **kwargs):
        raise AssertionError("_http_post_json must not be called with no API key configured")

    monkeypatch.setattr(quote_providers, "_http_post_json", boom)

    xom = quote_providers.request_quote("/tmp/x.step", "xometry", 5)
    assert xom["ok"] is False
    assert "reason" in xom
    assert "hint" in xom

    jlc = quote_providers.request_quote("/tmp/b.zip", "jlcpcb", 10)
    assert jlc["ok"] is False
    assert "reason" in jlc
    assert "hint" in jlc


def test_quote_unknown_vendor():
    with pytest.raises(quote_providers.QuoteError):
        quote_providers.get_quote_provider("acme")
    result = quote_providers.request_quote("/tmp/x.step", "acme", 1)
    assert result["ok"] is False


# --- quote_providers: key present (mocked HTTP seam) -----------------------------------------------

def test_quote_present_key_mocked_xometry(monkeypatch):
    monkeypatch.setenv("XOMETRY_API_KEY", "test-key")

    def fake_post(url, payload, *, headers=None, timeout=quote_providers.HTTP_TIMEOUT):
        return {
            "unit_price": 18.4,
            "lead_time_days": 7,
            "price_breaks": [
                {"qty": 25, "unit_price": 18.4},
                {"qty": 100, "unit_price": 12.9},
            ],
            "quote_id": "XOM-1",
        }

    monkeypatch.setattr(quote_providers, "_http_post_json", fake_post)
    result = quote_providers.request_quote("/tmp/x.step", "xometry", 25)

    assert result["ok"] is True
    assert result["unit_price"] == pytest.approx(18.4)
    assert result["ext_price"] == pytest.approx(18.4 * 25)
    assert result["quantity"] == 25
    assert result["currency"] == "USD"
    assert result["price_breaks"]
    assert result["quote_id"] == "XOM-1"
    assert isinstance(result["fetched_ts"], float)


def test_quote_present_key_mocked_jlcpcb(monkeypatch):
    monkeypatch.setenv("JLCPCB_KEY", "test-key")

    def fake_post(url, payload, *, headers=None, timeout=quote_providers.HTTP_TIMEOUT):
        return {
            "unit_price": 2.0,
            "price_breaks": [{"qty": 5, "unit_price": 2.0}],
        }

    monkeypatch.setattr(quote_providers, "_http_post_json", fake_post)
    result = quote_providers.request_quote("/tmp/b.zip", "jlcpcb", 5)

    assert result["ok"] is True
    assert result["unit_price"] == pytest.approx(2.0)
    assert result["ext_price"] == pytest.approx(2.0 * 5)
    assert result["quantity"] == 5
    assert result["price_breaks"]


# --- bom.resolve_costs fold-in ----------------------------------------------------------------

def test_resolve_costs_made_line():
    made_line = bom.BomLine(line_id="L1", kind="made", quantity=4)
    bought_line = bom.BomLine(line_id="L2", kind="bought", quantity=2)
    lines = [made_line, bought_line]

    bom.resolve_costs(
        lines,
        cost_lookup=lambda ln: cost_model.estimate_made_cost(
            volume_mm3=50000.0, material="steel", cycle_time_s=600.0, quantity=ln.quantity,
        ),
    )

    assert lines[0].cost_model["ok"] is True
    assert lines[0].unit_cost is not None
    assert lines[0].ext_cost == pytest.approx(lines[0].unit_cost * 4)

    # bought line is untouched
    assert lines[1].cost_model is None
    assert lines[1].unit_cost is None
    assert lines[1].ext_cost is None


def test_resolve_costs_unknown_material_leaves_unit_cost_none():
    made_line = bom.BomLine(line_id="L3", kind="made", quantity=3)
    lines = [made_line]

    bom.resolve_costs(
        lines,
        cost_lookup=lambda ln: cost_model.estimate_made_cost(
            volume_mm3=50000.0, material="unobtainium", cycle_time_s=600.0, quantity=ln.quantity,
        ),
    )

    assert lines[0].cost_model is not None
    assert lines[0].cost_model["ok"] is False
    assert lines[0].unit_cost is None
    assert lines[0].ext_cost is None
