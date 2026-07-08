"""Tests for the OPT-IN external material web lookup. All offline: the MakeItFrom parser
runs against a trimmed committed fixture and browser_fetch is monkeypatched. The opt-in
gate and the bot-wall refusal are asserted to perform NO fetch."""
from __future__ import annotations

import os

import pytest

from services.browser import session as bsession
from . import material_web_lookup as mwl

_ENV = "AI_CAD_ENABLE_MATERIAL_WEB"
_FIXTURE = os.path.join(os.path.dirname(__file__), "_testdata_makeitfrom_6061t6.html")


def _load_fixture() -> str:
    with open(_FIXTURE, encoding="utf-8") as f:
        return f.read()


def _fail_if_fetched(*a, **k):  # pragma: no cover - only fires the test if wrongly called
    raise AssertionError("browser_fetch must NOT be called")


def test_disabled_by_default_makes_no_fetch(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)
    monkeypatch.setattr(bsession, "browser_fetch", _fail_if_fetched)
    out = mwl.material_web_lookup("6061-T6")
    assert out["ok"] is False
    assert out["reason"] == "disabled"


def test_matweb_bot_walled_no_fetch(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    monkeypatch.setattr(bsession, "browser_fetch", _fail_if_fetched)
    out = mwl.material_web_lookup("6061-T6", source="matweb")
    assert out["ok"] is False
    assert out["reason"] == "bot_walled"


def test_makeitfrom_parser_offline(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    html = _load_fixture()

    def fake_fetch(url, site):
        if "search" in url:
            return {"ok": True, "status": 200,
                    "content": '<a href="/material-properties/6061-T6-Aluminum">6061-T6</a>'}
        return {"ok": True, "status": 200, "content": html}

    monkeypatch.setattr(bsession, "browser_fetch", fake_fetch)
    out = mwl.material_web_lookup("6061-T6")
    assert out["ok"] is True
    props = out["properties"]
    assert props["E_mpa"] == pytest.approx(69000, rel=1e-3)
    assert props["density_g_cm3"] == pytest.approx(2.7, rel=1e-3)
    assert props["ultimate_mpa"] == pytest.approx(310, rel=1e-3)
    assert props["yield_mpa"] == pytest.approx(270, rel=1e-3)
    assert props["nu"] == pytest.approx(0.33, rel=1e-2)
    assert out["source_url"]
    assert out["confidence"] == "web"
    assert out["citation"]


def test_makeitfrom_single_property(monkeypatch):
    monkeypatch.setenv(_ENV, "1")
    html = _load_fixture()

    def fake_fetch(url, site):
        if "search" in url:
            return {"ok": True, "status": 200,
                    "content": '<a href="/material-properties/6061-T6-Aluminum">x</a>'}
        return {"ok": True, "status": 200, "content": html}

    monkeypatch.setattr(bsession, "browser_fetch", fake_fetch)
    out = mwl.material_web_lookup("6061-T6", property="modulus")
    assert out["ok"] is True
    assert out["property"] == "E_mpa"
    assert out["value"] == pytest.approx(69000, rel=1e-3)
    assert out["units"] == "MPa"


def test_botwall_reason_propagates(monkeypatch):
    monkeypatch.setenv(_ENV, "1")

    def fake_fetch(url, site):
        return {"ok": False, "reason": "captcha_or_2fa_detected"}

    monkeypatch.setattr(bsession, "browser_fetch", fake_fetch)
    out = mwl.material_web_lookup("6061-T6")
    assert out["ok"] is False
    assert out["reason"] == "captcha_or_2fa_detected"
