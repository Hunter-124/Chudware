"""Fixture-based parser test for the Fastenal search adapter.

The FASTENAL_SEARCH_HTML fixture below is a VERBATIM slice of a real Fastenal
search page fetched live 2026-07-07 from
``https://www.fastenal.com/product?query=m3+screw`` (HTTP 200, title
"Sockets | Fastenal"). It contains two real product cards copied byte-for-byte:
one with a numeric SKU (924835414) and one with an alphanumeric SKU
(M42510040A20000) — both carry an anonymous "Online Price", which the live page
did show for an anonymous fetch. No JSON-LD is present; parsing is purely from
the server-rendered ``data-sku`` / ``product-description-grid`` markup.

Offline style mirrors test_retail_adapters.py: ``session.browser_fetch`` is
monkeypatched; no real Playwright, no network.
"""
from __future__ import annotations

import importlib

import pytest


# --- VERBATIM real slice (2 product cards) from the saved search page --------
FASTENAL_SEARCH_HTML = r"""<table><tbody><tr><td><div class="card h-100 border-0 rounded-0 list-item-detail me-0"><div class="row g-0 h-100"><div class="col-xs-12 col-md-4 p-2 ps-0"><div class="mt-2 border p-2 rounded"><a href="/product/details/924835414"><img src="https://img4.fastenal.com/infp360pmm/derivates/2/001/243/159/0135931.jpg" class="card-img" alt="..."></a></div><div class="form-check mt-1 align-items-end d-flex gap-2"><input class="form-check-input feco-form-check-input" type="checkbox" id="924835414" value=""><label class="form-check-label text-primary" for="924835414">Compare</label></div></div><input type="hidden" name="data-sku" value="924835414"><div class="col-xs-12 col-md-8 ml-n3 ml-md-0 feco-ml-0 feco-ml-sm-0 feco-pl-md-1 ie-feco-grid-mb"><div class="card-body ps-2 pe-2"><div class="card-title"><a class="text-decoration-none" href="/product/details/924835414"><div class="product-description-grid">M3-0.5 x 8mm L 2.5mm Hex Drive Class 12.9 Zinc Alloy Steel Socket Head Cap Screw</div></a></div><div class="card-text"><span class="fw-bold">SKU : </span><span>924835414</span></div><div class="card-text mt-1 row"><span class="fw-bold pe-1 col-auto">Mfg : </span><span class="card-text col ps-0 pe-0">FASTENAL APPROVED VENDOR</span></div><div class="ecom-card-icon-list-group mt-2"><ul class="nav d-flex gap-2"></ul></div></div></div></div></div></td><td><div class="mt-3">package of 100</div></td><td><div class="d-flex align-items-start mt-2 me-2 ms-2" style="max-width: 250px;"><div><div class="my-2"><p class="mb-0 fw-bold">Online Price</p><p class="mb-0"><span class="text-primary fw-bold">$19.83</span><span> / package of 100</span></p></div><div class="my-2"><p class="mb-0">Unit Price</p><p class="mb-0"><span class="text-primary fw-bold">$0.20</span><span> / each</span></p></div></div></div></td><tr><td><div class="card h-100 border-0 rounded-0 list-item-detail me-0"><div class="row g-0 h-100"><div class="col-xs-12 col-md-4 p-2 ps-0"><div class="mt-2 border p-2 rounded"><a href="/product/details/M42510040A20000"><img src="https://img4.fastenal.com/infp360pmm/derivates/2/001/263/139/M42580055A20000.jpg" class="card-img" alt="..."></a></div><div class="form-check mt-1 align-items-end d-flex gap-2"><input class="form-check-input feco-form-check-input" type="checkbox" id="M42510040A20000" value=""><label class="form-check-label text-primary" for="M42510040A20000">Compare</label></div></div><input type="hidden" name="data-sku" value="M42510040A20000"><div class="col-xs-12 col-md-8 ml-n3 ml-md-0 feco-ml-0 feco-ml-sm-0 feco-pl-md-1 ie-feco-grid-mb"><div class="card-body ps-2 pe-2"><div class="card-title"><a class="text-decoration-none" href="/product/details/M42510040A20000"><div class="product-description-grid">M3-0.5 x 40mm L 2mm Hex Drive A2-70 Plain Stainless Steel Flat Head Socket Cap Screw</div></a></div><div class="card-text"><span class="fw-bold">SKU : </span><span>M42510040A20000</span></div><div class="card-text mt-1 row"><span class="fw-bold pe-1 col-auto">Mfg : </span><span class="card-text col ps-0 pe-0">FASTENAL APPROVED VENDOR</span></div><div class="ecom-card-icon-list-group mt-2"><ul class="nav d-flex gap-2"><li class="nav-item cursor-pointer feco-border-sm"><img src="https://img2.fastenal.com/infp360pmm/derivates/3/001/146/958/Fastenal-REACH_x.jpg" alt="REACH" class="img-20x20"></li><li class="nav-item cursor-pointer feco-border-sm"><img src="https://img3.fastenal.com/infp360pmm/derivates/3/001/146/958/Fastenal-RoHS_x.jpg" alt="RoHS" class="img-20x20"></li></ul></div></div></div></div></div></td><td><div class="mt-3">each</div></td><td><div class="d-flex align-items-start mt-2 me-2 ms-2" style="max-width: 250px;"><div><div class="my-2"><p class="mb-0 fw-bold">Online Price</p><p class="mb-0"><span class="text-primary fw-bold">$0.19</span><span> / each</span></p></div></div></div></td>"""


@pytest.fixture()
def fastenal_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from engine import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import fastenal
    importlib.reload(fastenal)
    yield session, fastenal
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_fastenal_search_parses_server_rendered_cards(fastenal_mod, monkeypatch):
    session, fastenal = fastenal_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": FASTENAL_SEARCH_HTML})

    hits = fastenal.ADAPTER.search("m3 screw")

    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "fastenal" for h in hits)

    first = hits[0]
    assert first["title"] == (
        "M3-0.5 x 8mm L 2.5mm Hex Drive Class 12.9 Zinc Alloy Steel "
        "Socket Head Cap Screw")
    assert first["url"] == "https://www.fastenal.com/product/details/924835414"
    assert first["sku"] == "924835414"
    assert first["price"] == "$19.83"

    # Alphanumeric SKUs are real on Fastenal — must parse too.
    second = hits[1]
    assert second["sku"] == "M42510040A20000"
    assert second["url"].endswith("/product/details/M42510040A20000")
    assert "Flat Head Socket Cap Screw" in second["title"]
    assert second["price"] == "$0.19"


def test_fastenal_search_propagates_fetch_failure(fastenal_mod, monkeypatch):
    session, fastenal = fastenal_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": False, "reason": "no_profile", "hint": "x"})

    hits = fastenal.ADAPTER.search("m3 screw")
    assert hits == [{"ok": False, "reason": "no_profile", "hint": "x"}]
