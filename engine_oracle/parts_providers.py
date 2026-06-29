"""Pluggable supplier-provider seam for fetching vendor CAD into the registry.

Policy: **no scraping**. Providers either use a supplier's *approved* API (with the
customer's own credentials/certificate) or rely on user-downloaded files registered through
:func:`engine.vendor_parts.register_local_cad`. A provider that lacks configured credentials
raises a clear, actionable :class:`ProviderError` instead of touching the supplier website.

Each provider's ``fetch(part_number)`` is expected to land a CAD file on disk and register it
via the vendor-part registry, returning the resulting record. Credentials live in
``%APPDATA%/AI-CAD/config.json`` under a ``parts_providers`` section, e.g.::

    {
      "parts_providers": {
        "mcmaster": {
          "enabled": false,
          "api_url": "https://api.mcmaster.com",
          "client_cert": "C:/path/to/client.pfx",
          "username": "you@example.com",
          "password": "..."
        }
      }
    }

See ``docs/PARTS_LIBRARY.md`` for the full policy and setup instructions.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from engine import paths, vendor_parts


class ProviderError(Exception):
    """Raised by a provider when a fetch cannot proceed.

    Always carries a human-readable :attr:`message`.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _load_config() -> dict[str, Any]:
    cfg_path = paths.CONFIG
    try:
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 (config is user-editable; never hard-fail on parse)
        return {}
    return {}


def _provider_config(key: str) -> dict[str, Any]:
    section = _load_config().get("parts_providers")
    if isinstance(section, dict):
        sub = section.get(key)
        if isinstance(sub, dict):
            return sub
    return {}


class PartProvider:
    """Base class for supplier providers."""

    #: short stable identifier used for dispatch + config lookup
    key: str = "base"

    def name(self) -> str:
        return self.key

    def fetch(self, part_number: str) -> dict:  # pragma: no cover - abstract
        raise NotImplementedError


class _UnconfiguredProvider(PartProvider):
    """A provider scaffold that always raises until properly wired."""

    display = "supplier"

    def fetch(self, part_number: str) -> dict:
        raise ProviderError(
            f"{self.display} provider is not yet configured. No website scraping is "
            f"performed. Download the CAD for '{part_number}' from {self.display} and "
            f"register it with vendor_part_register_local_cad, or wire an approved API "
            f"adapter. See docs/PARTS_LIBRARY.md."
        )


def _call_mcmaster_api(cfg: dict[str, Any], part_number: str) -> dict:
    """The single intentional stub: the authenticated McMaster API call.

    Credentials are present/enabled, but we cannot exercise the real API here. Wire this to
    the approved McMaster-Carr Product Information API once an account + client certificate
    are validated. It must download the CAD to a temp path and then register it via
    ``vendor_parts.register_local_cad(supplier="McMaster-Carr", part_number=..., cad_path=...,
    source="api")``, returning that record.
    """
    raise ProviderError(
        "McMaster-Carr API adapter is not wired yet. Credentials are configured, but the "
        "approved Product Information API call must be implemented before automated fetch "
        "can run. Download the CAD manually and register it with "
        "vendor_part_register_local_cad, or wait for the API adapter."
    )


class McMasterProvider(PartProvider):
    """McMaster-Carr via the official Product Information API (no scraping).

    Requires an approved API account and client certificate. When credentials are absent or
    disabled, :meth:`fetch` raises a :class:`ProviderError` explaining the requirement.
    """

    key = "mcmaster"

    def fetch(self, part_number: str) -> dict:
        part_number = (part_number or "").strip()
        if not part_number:
            raise ProviderError("a McMaster-Carr part number is required")

        cfg = _provider_config(self.key)
        enabled = bool(cfg.get("enabled"))
        have_creds = bool(cfg.get("api_url")) and bool(cfg.get("username")) and (
            bool(cfg.get("password")) or bool(cfg.get("client_cert")))

        if not enabled or not have_creds:
            raise ProviderError(
                "McMaster-Carr provider is not configured. McMaster requires an approved "
                "Product Information API account with a client certificate and login "
                "credentials. AI-CAD does NOT scrape mcmaster.com. To enable it, add a "
                "'parts_providers.mcmaster' section to %APPDATA%/AI-CAD/config.json with "
                "'enabled': true, 'api_url', 'username', 'password', and 'client_cert' "
                "(see docs/PARTS_LIBRARY.md). Until then, download the CAD for "
                f"'{part_number}' from mcmaster.com and register it with "
                "vendor_part_register_local_cad."
            )

        # Credentials present + enabled: this is the path that WOULD call the approved API.
        return _call_mcmaster_api(cfg, part_number)


class GrabCadProvider(_UnconfiguredProvider):
    key = "grabcad"
    display = "GrabCAD"


class TracePartsProvider(_UnconfiguredProvider):
    key = "traceparts"
    display = "TraceParts"


_PROVIDERS: dict[str, PartProvider] = {
    p.key: p for p in (McMasterProvider(), GrabCadProvider(), TracePartsProvider())
}
# Common supplier-name aliases -> provider key.
_ALIASES = {
    "mcmaster": "mcmaster",
    "mcmaster-carr": "mcmaster",
    "mcmaster carr": "mcmaster",
    "grabcad": "grabcad",
    "grab cad": "grabcad",
    "traceparts": "traceparts",
    "trace parts": "traceparts",
}


def get_provider(supplier: str) -> PartProvider:
    """Resolve a supplier name (or provider key) to a registered provider."""
    s = (supplier or "").strip().lower()
    key = _ALIASES.get(s, s)
    prov = _PROVIDERS.get(key)
    if prov is None:
        known = ", ".join(sorted(_PROVIDERS))
        raise ProviderError(
            f"no provider for supplier '{supplier}'. Known providers: {known}. "
            f"For unsupported suppliers, register a downloaded CAD file with "
            f"vendor_part_register_local_cad."
        )
    return prov


def fetch_part(supplier: str, part_number: str) -> dict:
    """Dispatch to the right provider and return the registered record (or raise)."""
    return get_provider(supplier).fetch(part_number)


EmitFn = Callable[[dict], None]


def _vendor_part_fetch(params: dict, _emit: EmitFn) -> dict:
    supplier = str(params.get("supplier") or "")
    part_number = str(params.get("part_number") or "")
    try:
        rec = fetch_part(supplier, part_number)
        return {"ok": True, "part": rec}
    except ProviderError as exc:
        return {"ok": False, "error": exc.message}


WORKER_METHODS = {
    "vendor_part_fetch": _vendor_part_fetch,
}
