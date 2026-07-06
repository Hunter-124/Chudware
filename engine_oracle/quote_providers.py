"""Quote-out adapters: on-demand manufacturing quotes from approved APIs (Xometry, JLCPCB).

Producer of ``aicad.sourcing/1`` §4 ``QuoteResult`` records. Same no-scrape / config-only-keys
policy as the part providers (``engine_oracle/parts_providers.py``): a quote adapter POSTs the
geometry to the supplier's *approved* instant-quote API using the customer's own credentials, or —
with no key configured — degrades to the §0 contract shape ``{"ok": false, "reason", "hint"}``
without ever touching the network. CI passes with no keys and no network.

- :class:`XometryProvider` — mechanical (STEP → CNC/sheet-metal instant quote).
- :class:`JLCPCBProvider` — PCB fab (Gerber bundle → board quote).

Keys are configuration (``paths.CONFIG`` → ``quote_providers.<key>.api_key``) with an env-var
fallback (``XOMETRY_API_KEY`` / ``JLCPCB_KEY``), never committed. The single HTTP seam
(:func:`_http_post_json`) is what tests monkeypatch to exercise the "key present → real quote"
path fully offline. Uses only stdlib ``urllib`` — no third-party dependency, so no licenses.md row.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Optional

from engine import paths

HTTP_TIMEOUT = 8.0
CURRENCY = "USD"


class QuoteError(Exception):
    """Raised by a quote adapter when a quote cannot proceed. Carries ``.message``."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# --- config / credentials (config-only keys; env fallback) ------------------------------------

def _load_config() -> dict[str, Any]:
    try:
        cfg_path = paths.CONFIG
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 (user-editable config; never hard-fail)
        return {}
    return {}


def _provider_config(key: str) -> dict[str, Any]:
    section = _load_config().get("quote_providers")
    if isinstance(section, dict):
        sub = section.get(key)
        if isinstance(sub, dict):
            return sub
    return {}


def _resolve_key(key: str, env_name: str) -> str:
    """API key from config (``quote_providers.<key>.api_key``) then the ``env_name`` fallback."""
    val = _provider_config(key).get("api_key")
    if not val:
        val = os.environ.get(env_name)
    return str(val).strip() if val else ""


# --- JLCPCB Open API signed-request auth (T-BL24) --------------------------------------------
# JLCPCB's Open API (https://api.jlcpcb.com/ · https://jlcpcb.com/help/catalog/213-JLCPCB-API)
# authenticates EACH request with an HMAC-SHA256 signature, NOT a static bearer token. Creds are
# a triple issued per developer account: appId + accessKey + secretKey (Erik obtained a real
# accesskey/secretkey pair — T-S1). Scheme (public sources — reverse-eng SDK + JLCPCB help —
# agree on HMAC-SHA256 + the "JOP" header; the exact string-to-sign ordering/separators should be
# CONFIRMED against the authenticated official docs before the first live call, hence the pinned,
# isolated _jlcpcb_sign below is the ONE line to adjust if they differ):
#   string_to_sign = METHOD.upper() \n path \n timestamp \n nonce \n body
#   signature      = base64( HMAC_SHA256(secretKey, string_to_sign) )
#   Authorization  = JOP appid="..", accesskey="..", nonce="..", timestamp="..", signature=".."

def _resolve_jlcpcb_creds() -> dict[str, str]:
    """appId/accessKey/secretKey from config (``quote_providers.jlcpcb.{appid,accesskey,secretkey}``)
    then env fallback (``JLCPCB_APP_ID`` / ``JLCPCB_ACCESS_KEY`` / ``JLCPCB_SECRET_KEY``) — mirrors
    ``parts_providers._resolve_creds``. Absent creds resolve to ``""`` so ``quote()`` fails closed
    (degraded shape, no network) exactly like every other approved-API provider."""
    cfg = _provider_config("jlcpcb")
    env_map = {"appid": "JLCPCB_APP_ID", "accesskey": "JLCPCB_ACCESS_KEY",
               "secretkey": "JLCPCB_SECRET_KEY"}
    out: dict[str, str] = {}
    for field, env_name in env_map.items():
        val = cfg.get(field) or os.environ.get(env_name)
        out[field] = str(val).strip() if val else ""
    return out


def _jlcpcb_sign(method: str, path: str, timestamp: str, nonce: str, body: str,
                 secret_key: str) -> str:
    """base64(HMAC-SHA256(secretKey, METHOD\\npath\\ntimestamp\\nnonce\\nbody)).

    Pure + deterministic — the T-BL24 test pins a known-input → known-signature fixture. This is
    the single authoritative definition of the string-to-sign; adjust ONLY here if JLCPCB's
    authenticated docs specify a different field order/separator."""
    string_to_sign = "\n".join([method.upper(), path, timestamp, nonce, body])
    digest = hmac.new(secret_key.encode("utf-8"), string_to_sign.encode("utf-8"),
                      hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _jlcpcb_auth_header(creds: dict[str, str], method: str, path: str, body: str,
                        *, timestamp: Optional[str] = None, nonce: Optional[str] = None) -> str:
    """Build the ``JOP ...`` Authorization header for a signed JLCPCB request. ``timestamp``/
    ``nonce`` are injectable so the signing is unit-testable deterministically; in production they
    default to epoch-ms + a fresh uuid4 hex (single-use, replay-resistant)."""
    ts = timestamp or str(int(time.time() * 1000))
    nc = nonce or uuid.uuid4().hex
    sig = _jlcpcb_sign(method, path, ts, nc, body, creds["secretkey"])
    return (f'JOP appid="{creds["appid"]}", accesskey="{creds["accesskey"]}", '
            f'nonce="{nc}", timestamp="{ts}", signature="{sig}"')


# --- HTTP seam (stdlib urllib; monkeypatched in tests, so CI stays offline) -------------------

def _http_post_json(url: str, payload: dict, *, headers: Optional[dict[str, str]] = None,
                    timeout: float = HTTP_TIMEOUT) -> dict:
    data = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "Accept": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, method="POST", headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed hosts)
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise QuoteError(f"HTTP {exc.code} from {req.host}: {exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise QuoteError(f"network error contacting {req.host}: {exc}") from exc
    try:
        return json.loads(body) if body.strip() else {}
    except json.JSONDecodeError as exc:
        raise QuoteError(f"invalid JSON response from {req.host}") from exc


def _degraded(provider: str, reason: str, hint: str) -> dict:
    """The §0 contract-shaped failure a caller gets when a quote cannot proceed."""
    return {"ok": False, "provider": provider, "reason": reason, "hint": hint}


def _price_breaks(tiers: Any) -> list[dict]:
    out = []
    for t in tiers or []:
        if isinstance(t, dict) and t.get("qty") is not None and t.get("unit_price") is not None:
            out.append({"qty": int(t["qty"]), "unit_price": float(t["unit_price"])})
    return sorted(out, key=lambda b: b["qty"])


class QuoteProvider:
    """Base class for quote-out adapters."""

    key: str = "base"
    env_var: str = ""

    def name(self) -> str:
        return self.key


class XometryProvider(QuoteProvider):
    """Xometry instant-quote API for mechanical parts (STEP → CNC / sheet-metal quote).

    No scraping. With no ``XOMETRY_API_KEY`` (or ``quote_providers.xometry.api_key`` in config)
    :meth:`quote` returns the §0 degraded shape without a network call. With a key it POSTs the
    part and normalizes the response into an ``aicad.sourcing/1`` §4 ``QuoteResult``.
    """

    key = "xometry"
    env_var = "XOMETRY_API_KEY"
    API_URL = "https://api.xometry.com/v1/quote"

    def quote(self, step_path: str, material: Optional[str] = None, quantity: int = 1,
              *, process: str = "cnc_milling", finish: Optional[str] = None,
              options: Optional[dict] = None) -> dict:
        qty = quantity if isinstance(quantity, int) and quantity >= 1 else 1
        api_key = _resolve_key(self.key, self.env_var)
        if not api_key:
            return _degraded(
                self.key,
                "Xometry API key not configured (no quote_providers.xometry.api_key and no "
                "XOMETRY_API_KEY). AI-CAD does not scrape; an approved instant-quote API key is "
                "required to quote out.",
                f"add 'quote_providers.xometry.api_key' to {paths.CONFIG} (or set XOMETRY_API_KEY) "
                "then retry request_quote.",
            )
        if not step_path:
            return _degraded(self.key, "no STEP file to quote", "export the part to STEP first.")
        payload = {
            "part_file": str(step_path), "process": process, "quantity": qty,
            "material": material, "finish": finish, "options": options or {},
        }
        try:
            raw = _http_post_json(self.API_URL, payload,
                                  headers={"Authorization": f"Bearer {api_key}"})
        except QuoteError as e:
            return _degraded(self.key, e.message, "check the Xometry API key and that the part is quotable.")
        return _normalize_result(self.key, qty, raw)


class JLCPCBProvider(QuoteProvider):
    """JLCPCB board-fab quote API (Gerber bundle → PCB quote), HMAC-SHA256 signed (T-BL24).

    No scraping. JLCPCB's Open API signs EVERY request (see :func:`_jlcpcb_sign`); the scaffold-era
    static ``JLCPCB_KEY`` bearer is gone. With no appid/accesskey/secretkey configured, :meth:`quote`
    returns the §0 degraded shape without a network call; with the credential triple it POSTs the
    signed Gerber quote and normalizes into a §4 ``QuoteResult``.
    """

    key = "jlcpcb"
    #: DEPRECATED (T-BL24): the scaffold-era static-bearer env var. JLCPCB's Open API requires
    #: per-request HMAC signing, so this is no longer read for auth — kept only so the deprecation
    #: is discoverable; quote() surfaces a clear message pointing at the new appid/accesskey/secretkey.
    env_var = "JLCPCB_KEY"
    API_URL = "https://api.jlcpcb.com/v1/quote"

    def quote(self, gerber_zip: str, layers: int = 2, quantity: int = 5,
              *, finish: Optional[str] = None, options: Optional[dict] = None) -> dict:
        qty = quantity if isinstance(quantity, int) and quantity >= 1 else 1
        creds = _resolve_jlcpcb_creds()
        if not (creds["appid"] and creds["accesskey"] and creds["secretkey"]):
            return _degraded(
                self.key,
                "JLCPCB Open API credentials not configured. The Open API requires per-request "
                "HMAC-SHA256 signing with an appid + accesskey + secretkey triple (the static "
                "JLCPCB_KEY bearer is no longer supported). No scraping.",
                f"add quote_providers.jlcpcb.{{appid,accesskey,secretkey}} to {paths.CONFIG} (or set "
                "JLCPCB_APP_ID / JLCPCB_ACCESS_KEY / JLCPCB_SECRET_KEY) then retry request_quote.",
            )
        if not gerber_zip:
            return _degraded(self.key, "no Gerber bundle to quote", "export the board Gerbers first.")
        opts = {"layers": int(layers), **(options or {})}
        payload = {
            "part_file": str(gerber_zip), "process": "pcb_fab", "quantity": qty,
            "finish": finish, "options": opts,
        }
        # Sign over the EXACT body _http_post_json will send (both default-serialize the same dict →
        # identical bytes) and the request PATH only (not scheme/host).
        body = json.dumps(payload)
        path = urllib.parse.urlparse(self.API_URL).path
        auth = _jlcpcb_auth_header(creds, "POST", path, body)
        try:
            raw = _http_post_json(self.API_URL, payload, headers={"Authorization": auth})
        except QuoteError as e:
            return _degraded(self.key, e.message,
                             "check the JLCPCB Open API credentials (appid/accesskey/secretkey), "
                             "the request signing, and the Gerber bundle.")
        return _normalize_result(self.key, qty, raw)


def _normalize_result(provider: str, quantity: int, raw: dict) -> dict:
    """Normalize a supplier response into an ``aicad.sourcing/1`` §4 ``QuoteResult`` dict.

    Tolerant of common field spellings. A response that carries no usable unit price degrades to
    the §0 shape rather than inventing a number.
    """
    if not isinstance(raw, dict):
        return _degraded(provider, "empty/invalid quote response", "retry, or check the API status.")
    if raw.get("ok") is False:  # supplier already reported a failure
        return {"ok": False, "provider": provider,
                "reason": str(raw.get("reason") or raw.get("error") or "quote declined by provider"),
                "hint": str(raw.get("hint") or "check the request parameters and try again.")}

    breaks = _price_breaks(raw.get("price_breaks"))
    unit_price = raw.get("unit_price")
    if unit_price is None:
        unit_price = raw.get("unitPrice")
    if unit_price is None and breaks:
        # Highest tier whose qty <= quantity, else the first tier.
        pick = [b for b in breaks if b["qty"] <= quantity]
        unit_price = (pick[-1] if pick else breaks[0])["unit_price"]
    if unit_price is None:
        return _degraded(provider, "quote response carried no unit price",
                         "the provider returned no price; retry or check the part/board is quotable.")

    unit_price = float(unit_price)
    ext = raw.get("ext_price")
    ext_price = float(ext) if ext is not None else unit_price * quantity
    lead = raw.get("lead_time_days", raw.get("leadTimeDays"))
    return {
        "ok": True,
        "provider": provider,
        "quantity": quantity,
        "unit_price": unit_price,
        "ext_price": round(ext_price, 6),
        "currency": str(raw.get("currency") or CURRENCY),
        "lead_time_days": int(lead) if lead is not None else None,
        "price_breaks": breaks,
        "quote_id": raw.get("quote_id") or raw.get("quoteId"),
        "quote_url": raw.get("quote_url") or raw.get("quoteUrl"),
        "expires_ts": raw.get("expires_ts") or raw.get("expiresTs"),
        "fetched_ts": time.time(),
    }


# --- dispatch ---------------------------------------------------------------------------------

_PROVIDERS: dict[str, type[QuoteProvider]] = {
    "xometry": XometryProvider,
    "jlcpcb": JLCPCBProvider,
}
_ALIASES: dict[str, str] = {
    "xom": "xometry", "cnc": "xometry", "mechanical": "xometry",
    "jlc": "jlcpcb", "pcb": "jlcpcb", "board": "jlcpcb",
}


def get_quote_provider(vendor: str) -> QuoteProvider:
    """Resolve a vendor name/alias to a quote provider instance, or raise :class:`QuoteError`."""
    key = str(vendor or "").strip().lower()
    key = _ALIASES.get(key, key)
    cls = _PROVIDERS.get(key)
    if cls is None:
        raise QuoteError(f"unknown quote vendor {vendor!r}; known: {sorted(_PROVIDERS)}")
    return cls()


def request_quote(part_file: str, vendor: str, quantity: int = 1, *,
                  material: Optional[str] = None, layers: int = 2,
                  finish: Optional[str] = None, options: Optional[dict] = None) -> dict:
    """Dispatch a quote request to the right adapter, returning a §4 ``QuoteResult`` (or §0 failure).

    ``part_file`` is a STEP path for Xometry (mechanical) or a Gerber bundle for JLCPCB (PCB).
    A missing key / unknown vendor / provider failure all degrade to the §0 ``{"ok": false, ...}``
    shape — never a traceback, never a network call without a configured key.
    """
    try:
        provider = get_quote_provider(vendor)
    except QuoteError as e:
        return {"ok": False, "provider": str(vendor), "reason": e.message,
                "hint": f"use one of {sorted(_PROVIDERS)}."}
    if isinstance(provider, JLCPCBProvider):
        return provider.quote(part_file, layers=layers, quantity=quantity,
                              finish=finish, options=options)
    return provider.quote(part_file, material=material, quantity=quantity,
                          finish=finish, options=options)
