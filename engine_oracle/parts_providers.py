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
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from typing import Any, Callable

from engine import paths, vendor_parts
from engine.vendor_parts import VendorPart

_log = logging.getLogger(__name__)

#: Default per-request HTTP timeout (seconds). Providers are called from the agent loop and
#: MUST never block indefinitely (see contracts/sourcing_schema.md §0 degradation).
HTTP_TIMEOUT = 5.0


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


def _resolve_creds(key: str, env_map: dict[str, str]) -> dict[str, str]:
    """Resolve provider credentials from config first, then environment fallback.

    API keys are **configuration** (``%APPDATA%/AI-CAD/config.json`` → ``parts_providers.<key>``),
    per contracts/sourcing_schema.md and CONTRIBUTING.md — never committed and never *required*
    for CI. ``env_map`` maps each credential field name to the environment variable that may
    supply it as a convenience fallback (e.g. ``{"client_id": "NEXAR_CLIENT_ID"}``). Absent
    credentials resolve to empty strings so a provider can fail closed with a ProviderError.
    """
    cfg = _provider_config(key)
    out: dict[str, str] = {}
    for field_name, env_name in env_map.items():
        val = cfg.get(field_name)
        if not val:
            val = os.environ.get(env_name)
        out[field_name] = str(val).strip() if val else ""
    return out


# --- HTTP seam (stdlib urllib; no third-party dependency, so no licenses.md ledger row). -----
# These small helpers are the single point tests monkeypatch to run fully offline. Every call
# is bounded by HTTP_TIMEOUT and any transport/parse failure degrades to a ProviderError (never
# a raw traceback, never a network call without configured credentials — §0 of the contract).

def _http_json(req: urllib.request.Request, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed hosts)
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # 4xx/5xx
        raise ProviderError(f"HTTP {exc.code} from {req.host}: {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ProviderError(f"network error contacting {req.host}: {exc}") from exc
    try:
        return json.loads(body) if body.strip() else {}
    except json.JSONDecodeError as exc:
        raise ProviderError(f"invalid JSON response from {req.host}") from exc


def _http_post_form(url: str, fields: dict[str, str], *, headers: dict[str, str] | None = None,
                    timeout: float = HTTP_TIMEOUT) -> dict:
    data = urllib.parse.urlencode(fields).encode("utf-8")
    hdrs = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
    return _http_json(urllib.request.Request(url, data=data, method="POST", headers=hdrs), timeout)


def _http_post_json(url: str, payload: dict, *, headers: dict[str, str] | None = None,
                    timeout: float = HTTP_TIMEOUT) -> dict:
    data = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})}
    return _http_json(urllib.request.Request(url, data=data, method="POST", headers=hdrs), timeout)


def _http_get_json(url: str, *, headers: dict[str, str] | None = None,
                   timeout: float = HTTP_TIMEOUT) -> dict:
    hdrs = {"Accept": "application/json", **(headers or {})}
    return _http_json(urllib.request.Request(url, method="GET", headers=hdrs), timeout)


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


# ---------------------------------------------------------------------------------------------
# Electronic-component distributor providers (T-B1b).
#
# Nexar (Octopart GraphQL) is the primary aggregator; Digi-Key and Mouser are the two direct
# distributor APIs. All three use a supplier's *approved* API with the customer's own
# credentials — NO retailer scraping (contracts/sourcing_schema.md §0, CONTRIBUTING.md). Each
# maps a manufacturer part number (MPN) to a priced :class:`VendorPart` record, filling the §1
# sourcing fields. With no credentials configured they raise a clear ProviderError (fail closed,
# never a network call); the :class:`ProviderChain` translates that into a graceful fall-through.
# ---------------------------------------------------------------------------------------------


def _price_breaks(prices: list[dict]) -> list[dict]:
    """Normalize an offer's price list into ascending ``[{"qty", "unit_price"}]`` tiers."""
    tiers = [
        {"qty": int(p["quantity"]), "unit_price": float(p["price"])}
        for p in (prices or [])
        if p.get("quantity") is not None and p.get("price") is not None
    ]
    return sorted(tiers, key=lambda b: b["qty"])


class NexarProvider(PartProvider):
    """Nexar / Octopart supply data via the approved GraphQL API (no scraping).

    OAuth2 ``client_credentials`` grant against ``identity.nexar.com``; the bearer token then
    authorizes a ``supSearchMpn`` query on ``api.nexar.com/graphql``. Credentials come from
    config (``parts_providers.nexar.{client_id,client_secret}``) or the ``NEXAR_CLIENT_ID`` /
    ``NEXAR_CLIENT_SECRET`` environment fallback. Absent credentials → :class:`ProviderError`.
    """

    key = "nexar"
    TOKEN_URL = "https://identity.nexar.com/connect/token"
    API_URL = "https://api.nexar.com/graphql"
    QUERY = (
        "query PriceByMpn($q: String!) {"
        "  supSearchMpn(q: $q, limit: 1) {"
        "    results {"
        "      part {"
        "        mpn manufacturer { name }"
        "        sellers { company { name }"
        "          offers { inventoryLevel moq prices { price quantity currency } factoryLeadDays } }"
        "        bestDatasheet { url }"
        "      }"
        "    }"
        "  }"
        "}"
    )

    def _creds(self) -> tuple[str, str]:
        c = _resolve_creds(self.key, {"client_id": "NEXAR_CLIENT_ID",
                                      "client_secret": "NEXAR_CLIENT_SECRET"})
        return c["client_id"], c["client_secret"]

    def _get_token(self, client_id: str, client_secret: str) -> str:
        """Exchange client credentials for a bearer token (monkeypatched in tests)."""
        resp = _http_post_form(self.TOKEN_URL, {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "supply.domain",
        })
        token = str(resp.get("access_token") or "").strip()
        if not token:
            raise ProviderError("Nexar returned no access token (check client_id/secret)")
        return token

    def _post_graphql(self, token: str, mpn: str) -> dict:
        """Run the MPN supply query (monkeypatched in tests)."""
        return _http_post_json(self.API_URL,
                               {"query": self.QUERY, "variables": {"q": mpn}},
                               headers={"Authorization": f"Bearer {token}"})

    @staticmethod
    def _cheapest_offer(offers: list[dict]) -> dict | None:
        best: dict | None = None
        best_price: float | None = None
        for off in offers or []:
            unit = min((float(p["price"]) for p in (off.get("prices") or [])
                        if p.get("price") is not None), default=None)
            if unit is None:
                continue
            if best_price is None or unit < best_price:
                best, best_price = off, unit
        return best

    def _map(self, mpn: str, data: dict) -> dict | None:
        root = data.get("data") or {}
        # Real field is `supSearchMpn`; accept `supply` too for forward/back tolerance.
        supply = root.get("supSearchMpn") or root.get("supply") or {}
        results = supply.get("results") or []
        if not results:
            return None
        part = (results[0] or {}).get("part") or {}
        manufacturer = ((part.get("manufacturer") or {}).get("name") or "").strip()
        part_number = str(part.get("mpn") or mpn)
        vp = VendorPart(
            supplier="Nexar",
            part_number=part_number,
            description=(f"{manufacturer} {part_number}".strip()),
            product_url=str((part.get("bestDatasheet") or {}).get("url") or ""),
            specs={"manufacturer": manufacturer} if manufacturer else {},
            source="api",
        )
        sellers = part.get("sellers") or []
        if sellers:
            seller = sellers[0] or {}
            seller_name = ((seller.get("company") or {}).get("name") or "").strip()
            if seller_name:
                vp.supplier = seller_name
            best = self._cheapest_offer(seller.get("offers") or [])
            if best is not None:
                breaks = _price_breaks(best.get("prices") or [])
                vp.price_breaks = breaks
                if breaks:
                    vp.unit_price = breaks[0]["unit_price"]
                for p in best.get("prices") or []:
                    if p.get("currency"):
                        vp.currency = str(p["currency"])
                        break
                inv = best.get("inventoryLevel")
                vp.stock = int(inv) if inv is not None else None
                moq = best.get("moq")
                vp.moq = int(moq) if moq else None
                lead = best.get("factoryLeadDays")
                vp.lead_time_days = int(lead) if lead is not None else None
        vp.fetched_ts = time.time()
        return asdict(vp)

    def fetch(self, part_number: str) -> dict:
        mpn = (part_number or "").strip()
        if not mpn:
            raise ProviderError("a manufacturer part number (MPN) is required")
        client_id, client_secret = self._creds()
        if not client_id or not client_secret:
            raise ProviderError(
                "Nexar provider is not configured. Add 'parts_providers.nexar' with "
                "'client_id' and 'client_secret' to %APPDATA%/AI-CAD/config.json (or set "
                "NEXAR_CLIENT_ID / NEXAR_CLIENT_SECRET). AI-CAD does NOT scrape Octopart; it "
                "uses the approved Nexar GraphQL API with your own credentials. "
                "See docs/PARTS_LIBRARY.md."
            )
        token = self._get_token(client_id, client_secret)
        rec = self._map(mpn, self._post_graphql(token, mpn))
        if rec is None:
            raise ProviderError(f"Nexar returned no supply results for MPN '{mpn}'")
        return rec


class DigiKeyProvider(PartProvider):
    """Digi-Key Product Information API v4 (OAuth2 client_credentials, no scraping).

    Scaffolded and wired; runs in graceful stub mode (raises :class:`ProviderError`) until a
    ``DIGIKEY_CLIENT_ID`` / ``DIGIKEY_CLIENT_SECRET`` key pair is configured. The mapping seam
    is documented so activating it is a config change plus a response-mapper, not a redesign.
    """

    key = "digikey"
    TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
    API_URL = "https://api.digikey.com/products/v4/search/keyword"

    def _creds(self) -> tuple[str, str]:
        c = _resolve_creds(self.key, {"client_id": "DIGIKEY_CLIENT_ID",
                                      "client_secret": "DIGIKEY_CLIENT_SECRET"})
        return c["client_id"], c["client_secret"]

    def fetch(self, part_number: str) -> dict:
        mpn = (part_number or "").strip()
        if not mpn:
            raise ProviderError("a manufacturer part number (MPN) is required")
        client_id, client_secret = self._creds()
        if not client_id or not client_secret:
            msg = (
                "Digi-Key provider is not configured. Add 'parts_providers.digikey' with "
                "'client_id' and 'client_secret' to %APPDATA%/AI-CAD/config.json (or set "
                "DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET). AI-CAD does NOT scrape digikey.com; "
                "it uses the approved Product Information API v4. See docs/PARTS_LIBRARY.md."
            )
            _log.warning("DigiKeyProvider stub: %s", msg)
            raise ProviderError(msg)
        # Credentials present: OAuth token + keyword search would run here (wired in a later
        # task once a live key is validated). Fail closed rather than emit unverified data.
        raise ProviderError(
            "Digi-Key API adapter is not wired yet. Credentials are configured, but the "
            "approved v4 search + response mapping must be validated against a live key before "
            "automated fetch can run."
        )


class MouserProvider(PartProvider):
    """Mouser Search API v2 — ``SearchByPartNumber`` (API-key auth, no scraping).

    Scaffolded and wired; graceful stub (raises :class:`ProviderError`) until ``MOUSER_API_KEY``
    is configured.
    """

    key = "mouser"
    API_URL = "https://api.mouser.com/api/v2/search/partnumber"

    def _api_key(self) -> str:
        return _resolve_creds(self.key, {"api_key": "MOUSER_API_KEY"})["api_key"]

    def fetch(self, part_number: str) -> dict:
        mpn = (part_number or "").strip()
        if not mpn:
            raise ProviderError("a manufacturer part number (MPN) is required")
        if not self._api_key():
            msg = (
                "Mouser provider is not configured. Add 'parts_providers.mouser' with "
                "'api_key' to %APPDATA%/AI-CAD/config.json (or set MOUSER_API_KEY). AI-CAD does "
                "NOT scrape mouser.com; it uses the approved Search API v2. "
                "See docs/PARTS_LIBRARY.md."
            )
            _log.warning("MouserProvider stub: %s", msg)
            raise ProviderError(msg)
        raise ProviderError(
            "Mouser API adapter is not wired yet. An API key is configured, but the approved "
            "SearchByPartNumber response mapping must be validated against a live key before "
            "automated fetch can run."
        )


class ProviderChain:
    """Try priced providers in priority order and return the first success.

    Iterates providers (default Nexar → Digi-Key → Mouser); each :class:`ProviderError`
    (missing credentials, unwired adapter, no results, transport failure) is logged and the
    chain falls through to the next provider. Returns the first §1 record dict, or ``None`` when
    every provider declines — the graceful "no price feed" state that consumers treat as unknown
    pricing (per §0, ``None`` is *not known*, never a guessed value).
    """

    def __init__(self, providers: list[PartProvider]):
        self.providers = list(providers)

    def fetch(self, part_number: str) -> dict | None:
        for prov in self.providers:
            try:
                return prov.fetch(part_number)
            except ProviderError as exc:
                _log.info("provider %s declined '%s': %s", prov.key, part_number, exc.message)
                continue
        return None


def default_pricing_chain() -> ProviderChain:
    """The default MPN → priced-VendorPart resolver: Nexar → Digi-Key → Mouser."""
    return ProviderChain([NexarProvider(), DigiKeyProvider(), MouserProvider()])


_PROVIDERS: dict[str, PartProvider] = {
    p.key: p for p in (McMasterProvider(), GrabCadProvider(), TracePartsProvider(),
                       NexarProvider(), DigiKeyProvider(), MouserProvider())
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
    "nexar": "nexar",
    "octopart": "nexar",
    "digikey": "digikey",
    "digi-key": "digikey",
    "digi key": "digikey",
    "mouser": "mouser",
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
