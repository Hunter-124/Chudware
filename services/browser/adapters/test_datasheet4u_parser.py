"""Fixture-based parser test for the Datasheet4U search adapter.

DS4U_SEARCH_HTML holds two VERBATIM result rows from a real Datasheet4U search
page fetched live 2026-07-10 from ``/share_search.php?sWord=lm358`` (HTTP 200).
Results are a server-rendered table (no JSON-LD); the adapter reads the
``<a class="line" href="/datasheets/...">`` part anchor, the ``mfr-name`` span and
the Description cell.

Offline style mirrors test_fastenal_parser.py: session.browser_fetch monkeypatched.
"""
from __future__ import annotations

import importlib

import pytest


DS4U_SEARCH_HTML = r"""<tr>
      <td data-label="Manufacturer"><img src="/logo/STMicroelectronics.gif" alt="STMicroelectronics Logo" loading="lazy" style="width: min(150px, 25vw); height:auto; object-fit: contain; border:1px solid black; vertical-align:bottom;">
	  <span class="mfr-name">STMicroelectronics</span></td>
 	  <td data-label="Part Number"><a class="line" href="/datasheets/STMicroelectronics/LM358/1495631">LM358</a></td>
	  <td data-label="Description">Low-power dual operational amplifiers</td>
    </tr>
<tr>
      <td data-label="Manufacturer"><img src="/logo/ON Semiconductor.gif" alt="ON Semiconductor Logo" loading="lazy" style="width: min(150px, 25vw); height:auto; object-fit: contain; border:1px solid black; vertical-align:bottom;">
	  <span class="mfr-name">onsemi</span></td>
 	  <td data-label="Part Number"><a class="line" href="/datasheets/ON-Semiconductor/LM358N/49950">LM358N</a></td>
	  <td data-label="Description">Single Supply Dual Operational Amplifiers</td>
    </tr>"""


@pytest.fixture()
def ds4u_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    from engine import paths as engine_paths
    importlib.reload(engine_paths)
    from services.browser import profiles, session
    importlib.reload(profiles)
    importlib.reload(session)
    from services.browser import adapters as adapters_pkg
    importlib.reload(adapters_pkg)
    from services.browser.adapters import datasheet4u
    importlib.reload(datasheet4u)
    yield session, datasheet4u
    monkeypatch.delenv("APPDATA", raising=False)
    importlib.reload(engine_paths)


def test_datasheet4u_search_parses_table_rows(ds4u_mod, monkeypatch):
    session, datasheet4u = ds4u_mod
    monkeypatch.setattr(session, "browser_fetch",
                        lambda url, s: {"ok": True, "content": DS4U_SEARCH_HTML})

    hits = datasheet4u.ADAPTER.search("lm358")

    assert len(hits) == 2
    assert all(h["ok"] is True and h["site"] == "datasheet4u" for h in hits)

    first = hits[0]
    assert first["part_number"] == "LM358"
    assert first["manufacturer"] == "STMicroelectronics"
    assert first["url"] == "https://www.datasheet4u.com/datasheets/STMicroelectronics/LM358/1495631"
    assert first["description"] == "Low-power dual operational amplifiers"


def test_datasheet4u_search_url_is_share_search_endpoint():
    from services.browser.adapters import datasheet4u
    assert "share_search.php?sWord=" in datasheet4u.SEARCH_URL_TMPL
