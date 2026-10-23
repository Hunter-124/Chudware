"""``material_lookup(material, property?)`` — material properties, bundled-first with a
runtime-fetch-with-citation fallback. Contract ``contracts/research_tools.md`` §1.

The answer comes from the **unified first-party registry** (native ``material_get``
RPC, bundle-safe handbook nominal values) whenever possible —
``confidence:"bundled"``, ``source_url:null`` (the citation rides in ``provenance``).
On a miss, offline mode refuses network access. Online calls first try the
disabled-by-default ``material_web_lookup`` accepted-risk path
(``CHUDWARE_ENABLE_MATERIAL_WEB``; ``contracts/license_debt.md``), then return cited
``web_search`` candidates for ``fetch_page``. Values are never fabricated and
CAPTCHAs/bot-walls are never bypassed.
"""
from __future__ import annotations

from typing import Optional

from .common import BUNDLED, envelope, fail, is_offline

# property key -> (SI unit label, human phrase)
_PROP_UNITS = {
    "E_mpa": ("MPa", "Young's modulus"),
    "yield_mpa": ("MPa", "yield strength"),
    "ultimate_mpa": ("MPa", "ultimate tensile strength"),
    "endurance_mpa": ("MPa", "endurance limit"),
    "density_g_cm3": ("g/cm^3", "density"),
    "nu": (None, "Poisson's ratio"),
}
# friendly aliases a caller might pass for a property
_PROP_ALIASES = {
    "e": "E_mpa", "modulus": "E_mpa", "youngs_modulus": "E_mpa", "young's modulus": "E_mpa",
    "yield": "yield_mpa", "yield_strength": "yield_mpa",
    "ultimate": "ultimate_mpa", "uts": "ultimate_mpa", "tensile": "ultimate_mpa",
    "tensile_strength": "ultimate_mpa",
    "endurance": "endurance_mpa", "fatigue": "endurance_mpa",
    "density": "density_g_cm3", "rho": "density_g_cm3",
    "poisson": "nu", "poissons_ratio": "nu", "nu": "nu",
}


def _resolve_prop(property: Optional[str]) -> Optional[str]:
    if not property:
        return None
    p = str(property).strip().lower()
    return _PROP_ALIASES.get(p, p if p in _PROP_UNITS else None)


def material_lookup(material: str, property: Optional[str] = None) -> dict:
    """Look up material properties by name/alias. With ``property`` set, returns that one
    quantity (SI unit on ``units``); without it, the full mechanical property set. Bundled
    registry data is served first (``confidence:"bundled"``); a miss falls through to
    ``web_search`` when online, else a §6 ``not_found`` degradation."""
    if not material or not str(material).strip():
        return fail("bad_query", "Provide a material name (e.g. '6061-T6', 'A36 steel').")

    # engine_oracle/materials was ported to core; resolve via the C++ RPC.
    from services.mcp import core_bridge as _cb  # noqa: PLC0415
    rec = _cb.call("material_get", {"name": material}).get("result")
    if rec is not None:
        mech = rec.get("mechanical", {})
        prov = rec.get("provenance", {})
        citation = prov.get("source")
        if property is not None:
            pk = _resolve_prop(property)
            if pk is None or pk not in mech:
                return fail("not_found",
                            f"{rec['name']} has no property {property!r}; known: {sorted(_PROP_UNITS)}.")
            unit, phrase = _PROP_UNITS[pk]
            val = mech[pk]
            ans = f"{rec['name']} {phrase} is {val}" + (f" {unit}" if unit else "")
            return envelope(
                ans, units=unit, source_url=prov.get("url"), quote=None,
                confidence=BUNDLED, material=rec["key"], property=pk, value=val,
                provenance=prov, citation=citation,
            )
        # full property set
        ans = f"{rec['name']} ({rec.get('category','material')}): " + ", ".join(
            f"{k}={mech[k]}" for k in ("E_mpa", "yield_mpa", "ultimate_mpa", "density_g_cm3") if k in mech
        )
        return envelope(
            ans, units=None, source_url=prov.get("url"), quote=None, confidence=BUNDLED,
            material=rec["key"], properties=mech, provenance=prov, citation=citation,
        )

    # --- miss: bundled data does not have it -----------------------------------------
    if is_offline():
        return fail("offline",
                    f"{material!r} is not in the bundled registry and offline mode blocks a live "
                    "lookup. Add it to engine/materials/registry.json or disable offline_mode.")
    # Opt-in structured web lookup (OFF by default). When enabled via
    # CHUDWARE_ENABLE_MATERIAL_WEB it returns parsed, cited property values for the
    # named alloy from a reachable materials-info site (MakeItFrom); disabled it
    # returns fail() with zero network I/O, so we fall straight through to the
    # cited web_search candidates below. Explicit accepted-risk opt-in — see that module
    # and contracts/license_debt.md row 17. Never bypasses bot-walls (MatWeb).
    from .material_web_lookup import material_web_lookup
    web = material_web_lookup(material, property)
    if web.get("ok"):
        return web
    # Online: hand back cited candidate sources to fetch_page — never fabricate a value.
    from .web_search import web_search
    q = f"{material} {property or 'material mechanical properties'}".strip()
    out = web_search(q)
    if out.get("ok"):
        out["note"] = (f"{material!r} not in bundled registry; returning cited search candidates — "
                       "fetch_page one and quote it, do not recall a value.")
        out["material_query"] = material
    return out
