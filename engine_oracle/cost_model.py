"""Cost model for **made** parts: material cost + machining cost (+ optional live metal prices).

Made-part cost (``aicad.sourcing/1`` §3 ``cost_model`` object) is::

    material_cost = volume × density × rate($/kg)      # raw stock
    machine_cost  = cycle_time_s / 3600 × machine_rate  # existing CAM cycle time
    setup_cost    = setup_time_s / 3600 × machine_rate  # amortized over the run quantity
    unit_cost     = material_cost + machine_cost + setup_cost

Contract: ``contracts/sourcing_schema.md`` — a ``QuoteResult``/cost object folds into a ``made``
:class:`~engine.bom.BomLine` (``unit_cost``→``unit_cost``, ``ext_cost``→``ext_cost``).

**No fabricated numbers in a load-bearing slot** (standing supervisor ruling). Every rate in the
seed table below carries a ``citation`` + ``asof`` provenance field, and the whole table is
user-overridable via config (``paths.CONFIG`` → ``cost_model``). An *unknown* material is a
contract-shaped unknown — :func:`material_cost` raises :class:`CostModelError` and
:func:`estimate_made_cost` returns ``{"ok": false, "reason", "hint", "unit_cost": null}`` (§0
degradation) — never a guessed numeral. The optional live-price feed (Commodities-API) is a
graceful no-op with no key and never runs in CI.

SEAM(T-F2 merge): a unified material registry (``engine_oracle/materials/registry.json``, on the
unmerged ``task/T-F2-source-helpers`` branch) will become the source of densities + prices. Until
it lands this local seed is authoritative; the join is a future follow-up. Do NOT depend on that
registry here — it is not in this tree.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from engine import paths

HTTP_TIMEOUT = 5.0
CURRENCY = "USD"
DEFAULT_MACHINE_RATE_PER_HR = 75.0
#: default per-part setup time (seconds) amortized across the run quantity — a config-overridable
#: shop constant, not a per-part measurement.
DEFAULT_SETUP_TIME_S = 900.0  # 15 min


class CostModelError(Exception):
    """Raised when a cost cannot be computed (e.g. an unknown material). Carries ``.message``."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# --- Seed table (provenance-carrying) ---------------------------------------------------------
# ``density_g_cm3`` is a physical constant mirroring the FEA material library in
# services/mcp/cad_tools.py::_MATERIALS (single source of the same numbers). ``rate_usd_per_kg`` is
# an order-of-magnitude commodity/stock price with a citation + asof, NOT a precise market quote —
# for a precise price, override via config or fold in a live feed (:func:`live_metal_price`).
_MATERIAL_SEED: dict[str, dict[str, Any]] = {
    "steel": {"density_g_cm3": 7.85, "rate_usd_per_kg": 2.0,
              "citation": "hot-rolled carbon-steel bar, US mill, order-of-magnitude", "asof": "2026-01"},
    "stainless": {"density_g_cm3": 8.0, "rate_usd_per_kg": 5.5,
                  "citation": "304 stainless bar stock, distributor list, order-of-magnitude", "asof": "2026-01"},
    "aluminum": {"density_g_cm3": 2.70, "rate_usd_per_kg": 3.5,
                 "citation": "6061 aluminium bar stock, distributor list, order-of-magnitude", "asof": "2026-01"},
    "titanium": {"density_g_cm3": 4.43, "rate_usd_per_kg": 35.0,
                 "citation": "Ti-6Al-4V bar stock, distributor list, order-of-magnitude", "asof": "2026-01"},
    "brass": {"density_g_cm3": 8.5, "rate_usd_per_kg": 8.0,
              "citation": "C360 brass bar stock, distributor list, order-of-magnitude", "asof": "2026-01"},
    "abs": {"density_g_cm3": 1.04, "rate_usd_per_kg": 3.0,
            "citation": "ABS pellet/stock, order-of-magnitude", "asof": "2026-01"},
    "pla": {"density_g_cm3": 1.24, "rate_usd_per_kg": 25.0,
            "citation": "PLA 3D-printer filament, retail spool, order-of-magnitude", "asof": "2026-01"},
    "cedar_lumber": {"density_g_cm3": 0.38, "rate_usd_per_kg": 1.2,
                     "citation": "western red cedar dimensional lumber, retail, order-of-magnitude", "asof": "2026-01"},
}

# Friendly aliases → canonical seed keys (mirrors cad_tools._MATERIAL_ALIASES where they overlap).
_MATERIAL_ALIASES: dict[str, str] = {
    "mild steel": "steel", "carbon steel": "steel", "a36": "steel", "1018": "steel",
    "ss": "stainless", "304": "stainless", "stainless steel": "stainless", "ss304": "stainless",
    "aluminium": "aluminum", "al": "aluminum", "6061": "aluminum", "alu": "aluminum",
    "alu_6061": "aluminum", "aluminum_6061": "aluminum",
    "ti": "titanium", "ti-6al-4v": "titanium", "grade5": "titanium",
    "cedar": "cedar_lumber", "lumber": "cedar_lumber", "wood": "cedar_lumber",
}

#: Public seeded $/kg table (task contract shape ``dict[str, float]``), derived from the seed +
#: any config override. User-overridable; see :func:`_config_overrides`.
MATERIAL_RATES: dict[str, float] = {k: float(v["rate_usd_per_kg"]) for k, v in _MATERIAL_SEED.items()}
#: Public density table (g/cm³), same derivation.
MATERIAL_DENSITY: dict[str, float] = {k: float(v["density_g_cm3"]) for k, v in _MATERIAL_SEED.items()}


# --- Config override (user-editable; never hard-fails) ----------------------------------------

def _config_overrides() -> dict[str, Any]:
    """Read the ``cost_model`` section of ``paths.CONFIG`` (``%APPDATA%/AI-CAD/config.json``).

    Shape (all optional)::

        {"cost_model": {"material_rates": {"aluminum": 4.1, "unobtainium": 1e6},
                        "densities": {"unobtainium": 19.3},
                        "machine_rate_per_hr": 90.0,
                        "setup_time_s": 600}}

    A new material added here is priceable immediately (rate + density both supplied). Never
    hard-fails on a missing/garbled config — an unreadable file yields no overrides.
    """
    try:
        cfg_path = paths.CONFIG
        if not cfg_path.exists():
            return {}
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 (user-editable config; never hard-fail)
        return {}
    section = data.get("cost_model") if isinstance(data, dict) else None
    return section if isinstance(section, dict) else {}


def _resolve_key(material_name: Any) -> str:
    """Canonical seed key for a material name (case/alias-insensitive), or raise CostModelError."""
    key = str(material_name or "").strip().lower()
    key = _MATERIAL_ALIASES.get(key, key)
    over = _config_overrides()
    rates = over.get("material_rates") if isinstance(over.get("material_rates"), dict) else {}
    if key in _MATERIAL_SEED or key in (rates or {}):
        return key
    known = sorted(set(_MATERIAL_SEED) | set(rates or {}))
    raise CostModelError(
        f"unknown material {material_name!r}; no seeded rate/density and no config override. "
        f"Known: {known}. Add a 'cost_model.material_rates'/'cost_model.densities' entry to "
        f"{paths.CONFIG} to price it — a cost is never fabricated for an unknown material."
    )


def rate_per_kg(material_name: Any) -> float:
    """Resolved $/kg for a material (config override wins over the seed). Raises on unknown."""
    key = _resolve_key(material_name)
    over = _config_overrides().get("material_rates")
    if isinstance(over, dict) and over.get(key) is not None:
        return float(over[key])
    return float(_MATERIAL_SEED[key]["rate_usd_per_kg"])


def density_g_cm3(material_name: Any) -> float:
    """Resolved density (g/cm³) for a material (config override wins over the seed).

    Raises :class:`CostModelError` if the material is known only by a config *rate* with no
    density (can't compute mass without it) — a contract-shaped unknown, never a guessed density.
    """
    key = _resolve_key(material_name)
    over = _config_overrides().get("densities")
    if isinstance(over, dict) and over.get(key) is not None:
        return float(over[key])
    seed = _MATERIAL_SEED.get(key)
    if seed is not None:
        return float(seed["density_g_cm3"])
    raise CostModelError(
        f"material {material_name!r} has a configured rate but no density; add "
        f"'cost_model.densities.{key}' to {paths.CONFIG} so mass (and material cost) can be computed."
    )


# --- Core calculators -------------------------------------------------------------------------

def material_cost(volume_mm3: float, material_name: str) -> float:
    """Raw-stock material cost = mass(kg) × $/kg, where mass = volume × density.

    ``volume_mm3`` is the part (or stock) volume in mm³; 1 cm³ = 1000 mm³. Raises
    :class:`CostModelError` for an unknown material (never a fabricated number) or a negative
    volume. Returns a ``float`` in USD.
    """
    v = float(volume_mm3)
    if v < 0:
        raise CostModelError(f"volume_mm3 must be >= 0, got {volume_mm3!r}")
    mass_kg = (v / 1000.0) * density_g_cm3(material_name) / 1000.0  # cm³ → g → kg
    return mass_kg * rate_per_kg(material_name)


def machining_cost(cycle_time_s: float, machine_rate_per_hr: float = DEFAULT_MACHINE_RATE_PER_HR) -> float:
    """Machining cost = cycle time (hours) × machine rate ($/hr). ``cycle_time_s`` in seconds."""
    t = float(cycle_time_s)
    if t < 0:
        raise CostModelError(f"cycle_time_s must be >= 0, got {cycle_time_s!r}")
    return (t / 3600.0) * float(machine_rate_per_hr)


def _default_machine_rate() -> float:
    over = _config_overrides().get("machine_rate_per_hr")
    try:
        return float(over) if over is not None else DEFAULT_MACHINE_RATE_PER_HR
    except (TypeError, ValueError):
        return DEFAULT_MACHINE_RATE_PER_HR


def _default_setup_time_s() -> float:
    over = _config_overrides().get("setup_time_s")
    try:
        return float(over) if over is not None else DEFAULT_SETUP_TIME_S
    except (TypeError, ValueError):
        return DEFAULT_SETUP_TIME_S


def estimate_made_cost(
    *,
    volume_mm3: Optional[float],
    material: Optional[str],
    cycle_time_s: Optional[float],
    quantity: int = 1,
    process: str = "cnc_milling",
    machine_rate_per_hr: Optional[float] = None,
    setup_time_s: Optional[float] = None,
) -> dict:
    """Assemble the ``aicad.sourcing/1`` §3 ``cost_model`` object for one made part.

    Returns a dict that folds directly onto a made :class:`~engine.bom.BomLine`. On success::

        {"ok": true, "source": "model", "process", "material",
         "material_cost", "machine_cost", "setup_cost", "unit_cost", "currency",
         "inputs": {volume_mm3, cycle_time_s, quantity, machine_rate_per_hr, setup_time_s},
         "provenance": {rate_usd_per_kg, density_g_cm3, citation, asof}}

    When an input is missing or the material is unknown it degrades to the §0 contract shape
    ``{"ok": false, "source": "model", "reason", "hint", "unit_cost": null, "currency": ...}`` —
    the caller keeps the line's ``unit_cost``/``ext_cost`` null (price *unknown*), never guessed.

    ``setup_cost`` amortizes a one-time setup across ``quantity``; ``machine_cost`` is per-unit run
    time. ``unit_cost = material_cost + machine_cost + setup_cost``.
    """
    qty = quantity if isinstance(quantity, int) and quantity >= 1 else 1
    rate = _default_machine_rate() if machine_rate_per_hr is None else float(machine_rate_per_hr)
    setup_s = _default_setup_time_s() if setup_time_s is None else float(setup_time_s)

    if volume_mm3 is None or material is None or cycle_time_s is None:
        missing = [n for n, v in (("volume_mm3", volume_mm3), ("material", material),
                                  ("cycle_time_s", cycle_time_s)) if v is None]
        return {
            "ok": False, "source": "model", "process": process, "material": material,
            "unit_cost": None, "currency": CURRENCY,
            "reason": f"cost model missing input(s): {', '.join(missing)}",
            "hint": "provide part volume (measure), material, and CAM cycle_time_s (cam_simulate) "
                    "so the made-part cost can be computed without guessing.",
        }
    try:
        mat = material_cost(volume_mm3, material)
        run = machining_cost(cycle_time_s, rate)
        setup = machining_cost(setup_s, rate) / qty
        key = _resolve_key(material)
        seed = _MATERIAL_SEED.get(key, {})
        prov = {
            "rate_usd_per_kg": rate_per_kg(material),
            "density_g_cm3": density_g_cm3(material),
            "citation": seed.get("citation", "config override" if key not in _MATERIAL_SEED else ""),
            "asof": seed.get("asof", ""),
        }
    except CostModelError as e:
        return {
            "ok": False, "source": "model", "process": process, "material": material,
            "unit_cost": None, "currency": CURRENCY, "reason": e.message,
            "hint": "add a 'cost_model' entry (rate + density) to the config to price this material.",
        }

    unit = mat + run + setup
    return {
        "ok": True, "source": "model", "process": process, "material": material,
        "material_cost": round(mat, 6), "machine_cost": round(run, 6),
        "setup_cost": round(setup, 6), "unit_cost": round(unit, 6), "currency": CURRENCY,
        "inputs": {"volume_mm3": float(volume_mm3), "cycle_time_s": float(cycle_time_s),
                   "quantity": qty, "machine_rate_per_hr": rate, "setup_time_s": setup_s},
        "provenance": prov,
    }


# --- Optional live metal price (Commodities-API) ----------------------------------------------
# Graceful no-op with no key; NEVER runs in CI (no key configured, no network). The HTTP call is
# isolated behind :func:`_http_get_json` so a test can monkeypatch it; absent a key we return None
# (rate unknown) rather than a fabricated number, and any transport failure also yields None.

_COMMODITIES_URL = "https://commodities-api.com/api/latest"


def _commodities_key() -> str:
    over = _config_overrides()
    key = over.get("commodities_api_key") if isinstance(over, dict) else None
    if not key:
        key = os.environ.get("COMMODITIES_API_KEY")
    return str(key).strip() if key else ""


def _http_get_json(url: str, timeout: float = HTTP_TIMEOUT) -> dict:
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed host)
        body = resp.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip() else {}


def live_metal_price(symbol: str, base: str = "USD") -> Optional[float]:
    """Live commodity price for ``symbol`` (e.g. ``"ALUM"``, ``"COPPER"``, ``"TIMBER"``) in ``base``.

    Returns the price as a ``float``, or ``None`` when no API key is configured, on any network/
    parse failure, or when the response has no usable rate — a graceful no-op, never a guess.
    Commodities-API returns ``data.rates[symbol]`` as *units of symbol per 1 base*; the price per
    unit is its reciprocal. Config: ``cost_model.commodities_api_key`` or ``COMMODITIES_API_KEY``.
    """
    key = _commodities_key()
    if not key:
        return None
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    url = f"{_COMMODITIES_URL}?{urllib.parse.urlencode({'access_key': key, 'base': base, 'symbols': sym})}"
    try:
        payload = _http_get_json(url)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return None
    rates = (payload.get("data") or {}).get("rates") if isinstance(payload, dict) else None
    if not isinstance(rates, dict) or sym not in rates:
        return None
    try:
        per_base = float(rates[sym])
    except (TypeError, ValueError):
        return None
    if per_base == 0:
        return None
    return 1.0 / per_base
