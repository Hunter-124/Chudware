"""services/browser/adapters/wch.py — WCH (Nanjing Qinheng Microelectronics) source adapter (Parts Library V2).

WCH is a Chinese MCU / USB-bridge vendor (CH340 USB-UART bridges, CH32V RISC-V
MCUs). Product pages are static HTML with direct datasheet PDF links, so the
default :class:`~._retail.RetailAdapter` flow works: JSON-LD for search (best
effort), spec-table + ``find_datasheet_url`` grabbing the datasheet PDF on the
detail page. Open catalog — no login. Challenge pages degrade to
``login_required`` — never solved.

v1 best-effort, not live-verified (T-X6 gates live tests).
"""
from __future__ import annotations

from . import register
from ._retail import RetailAdapter

SITE = "wch"
BASE_URL = "https://www.wch-ic.com"
SEARCH_URL_TMPL = BASE_URL + "/en/search?q={query}"
LICENSE_NOTE = (
    "WCH datasheets remain WCH / Nanjing Qinheng's own manufacturer documents, "
    "fetched here for the user's own engineering reference only. This adapter "
    "never redistributes or LLM-generates page content — only a small "
    "spec/part record copied with provenance (source URL + fetch timestamp) and "
    "the manufacturer's datasheet saved for the user. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In", "Log In", 'id="login"')
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")


class WchAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS


ADAPTER = register(WchAdapter())
