"""Nexar/Octopart product search across electronics distributors."""
from __future__ import annotations

from . import fail, register
from . import _vendor_api

SITE = "nexar"
BASE_URL = "https://octopart.com"
LICENSE_NOTE = ("Nexar/Octopart product and seller data is used with provenance for the user's "
                "own sourcing decisions. Datasheets remain their manufacturers' documents.")


class NexarAdapter:
    key = SITE
    label = "Nexar (Octopart)"
    hosts = ("octopart.com", "nexar.com")
    result_kinds = ("product",)
    cad_acquisition = False
    login_required = False
    api = True

    def search(self, query: str) -> list[dict]:
        creds = _vendor_api.credentials("nexar")
        if not creds:
            return [fail(SITE, "search", "credentials_required",
                         "Configure Nexar API credentials in Settings.", url=BASE_URL)]
        return _vendor_api.search(SITE, query, creds)

    def acquire(self, result: dict) -> dict:
        return _vendor_api.acquire_datasheet(SITE, result, LICENSE_NOTE)


ADAPTER = register(NexarAdapter())
