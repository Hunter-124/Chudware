"""services/browser/adapters/hdsc.py — HDSC (Huada Semiconductor) source adapter (Parts Library V2).

Huada Semiconductor (hdsc.com.cn) is a Chinese MCU vendor — the HC32 Cortex-M
family. Product pages are static HTML linking direct datasheet PDFs; the site is
CN-hosted and can be slow. The default :class:`~._retail.RetailAdapter` flow
applies: JSON-LD for search (best effort), spec-table + ``find_datasheet_url``
grabbing the datasheet PDF on the detail page. Open catalog — no login.
Challenge pages degrade to ``login_required`` — never solved.

v1 best-effort, not live-verified (T-X6 gates live tests).
"""
from __future__ import annotations

from . import register
from ._retail import RetailAdapter

SITE = "hdsc"
BASE_URL = "https://www.hdsc.com.cn/en"
SEARCH_URL_TMPL = BASE_URL + "/search?q={query}"
LICENSE_NOTE = (
    "HDSC datasheets remain Huada Semiconductor's own manufacturer documents, "
    "fetched here for the user's own engineering reference only. This adapter "
    "never redistributes or LLM-generates page content — only a small "
    "spec/part record copied with provenance (source URL + fetch timestamp) and "
    "the manufacturer's datasheet saved for the user. See contracts/license_debt.md."
)

_LOGIN_WALL_MARKERS = ("Sign In", "Log In", "登录", 'id="login"')
_CHALLENGE_MARKERS = ("Access Denied", "Reference #", "Pardon Our Interruption")


class HdscAdapter(RetailAdapter):
    key = SITE
    base_url = BASE_URL
    search_url_tmpl = SEARCH_URL_TMPL
    license_note = LICENSE_NOTE
    login_wall_markers = _LOGIN_WALL_MARKERS
    challenge_markers = _CHALLENGE_MARKERS


ADAPTER = register(HdscAdapter())
