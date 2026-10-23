"""``material_web_lookup(material, property?, source?)`` — OPT-IN, rate-limited, cited
point-lookups of material properties from external materials-info websites.

Posture (be factual): this tool was added as a deliberate project decision on
2026-07-08. It is:

* **OPT-IN** — env-gated behind ``CHUDWARE_ENABLE_MATERIAL_WEB`` and **DISABLED by
  default**. With the flag unset every call returns a ``disabled`` degradation and
  performs no network I/O.
* **Rate-limited** — all fetches go through ``services.browser.session.browser_fetch``,
  which already enforces a per-site delay, exponential backoff, and a session ceiling.
* A **point-lookup** — a single named material at a time, never bulk/enumeration.

It **never bypasses CAPTCHAs or bot-walls**: if ``browser_fetch`` returns ``ok:False``
(including ``captcha_or_2fa_detected`` / ``bot_walled``) this module returns an honest
degradation and does not retry-hammer. Known bot-walled sources (MatWeb) are refused
without any fetch.

MatWeb, MakeItFrom, and Matmatch are named in ``contracts/research_tools.md`` as
prohibiting programmatic access; this path therefore exists only under the project's
accepted-risk override recorded in ``contracts/license_debt.md``. The compliant default
remains the first-party bundled registry served by ``material_lookup``.

Return shape matches ``material_lookup`` (the §1 envelope from ``.common``), with
``confidence="web"`` and a ``citation`` / ``source_url`` pointing at the fetched page.
"""
from __future__ import annotations

import os
import re
from typing import Optional
from urllib.parse import quote_plus

from services.browser import session as bsession

from .common import envelope, fail

# --- property model (mirrors material_lookup's keys) --------------------------------
_PROP_UNITS = {
    "E_mpa": ("MPa", "Young's modulus"),
    "yield_mpa": ("MPa", "yield strength"),
    "ultimate_mpa": ("MPa", "ultimate tensile strength"),
    "density_g_cm3": ("g/cm^3", "density"),
    "nu": (None, "Poisson's ratio"),
}
_PROP_ALIASES = {
    "e": "E_mpa", "modulus": "E_mpa", "youngs_modulus": "E_mpa", "young's modulus": "E_mpa",
    "yield": "yield_mpa", "yield_strength": "yield_mpa",
    "ultimate": "ultimate_mpa", "uts": "ultimate_mpa", "tensile": "ultimate_mpa",
    "tensile_strength": "ultimate_mpa",
    "density": "density_g_cm3", "rho": "density_g_cm3",
    "poisson": "nu", "poissons_ratio": "nu", "nu": "nu",
}

_ENABLE_ENV = "CHUDWARE_ENABLE_MATERIAL_WEB"


# --- source registry ----------------------------------------------------------------
def _makeitfrom_search(q: str) -> str:
    return "https://www.makeitfrom.com/search?q=" + quote_plus(q)


def _makeitfrom_material(slug: str) -> str:
    return "https://www.makeitfrom.com/material-properties/" + slug


WEB_SOURCES: dict = {
    "makeitfrom": {
        "key": "makeitfrom",
        "label": "MakeItFrom",
        "reachable": True,
        "bot_walled": False,
        "search_url": _makeitfrom_search,
        "material_url": _makeitfrom_material,
        "license_note": (
            "MakeItFrom prohibits programmatic access per contracts/research_tools.md; "
            "used only under the project's accepted-risk override (contracts/license_debt.md)."
        ),
    },
    "matweb": {
        "key": "matweb",
        "label": "MatWeb",
        "reachable": False,
        "bot_walled": True,
        "search_url": lambda q: "https://www.matweb.com/search/QuickText.aspx?SearchText=" + quote_plus(q),
        "material_url": None,
        "license_note": (
            "MatWeb serves a CAPTCHA to automated sessions and prohibits programmatic "
            "access; this tool never bypasses bot-walls. Refused without fetching."
        ),
    },
}


def _resolve_prop(property: Optional[str]) -> Optional[str]:
    if not property:
        return None
    p = str(property).strip().lower()
    return _PROP_ALIASES.get(p, p if p in _PROP_UNITS else None)


# --- MakeItFrom parsing --------------------------------------------------------------
# Exact MakeItFrom property-name -> our key. Exact match avoids "Shear Modulus" bleeding
# into "Elastic ... Modulus".
_NAME_MAP = {
    "Elastic (Young's, Tensile) Modulus": "E_mpa",
    "Tensile Strength: Ultimate (UTS)": "ultimate_mpa",
    "Tensile Strength: Yield (Proof)": "yield_mpa",
    "Density": "density_g_cm3",
    "Poisson's Ratio": "nu",
}

_TAGS = re.compile(r"<[^>]+>")


def _strip(html: str) -> str:
    # collapse <sup>3</sup> into inline "3" so "g/cm<sup>3</sup>" -> "g/cm3"
    html = re.sub(r"<sup>(.*?)</sup>", r"\1", html, flags=re.S)
    return re.sub(r"\s+", " ", _TAGS.sub("", html)).strip()


def _to_value(key: str, raw: str) -> Optional[float]:
    """Parse the human metric value text (e.g. '69 GPa 10 x 106 psi') into our unit."""
    if key == "nu":
        m = re.match(r"([-\d.]+)", raw)
        return float(m.group(1)) if m else None
    m = re.match(r"([-\d.]+)\s*(GPa|MPa|kg/m3|g/cm3|g/cm\^?3)", raw)
    if not m:
        return None
    val, unit = float(m.group(1)), m.group(2)
    if key == "density_g_cm3":
        return val if unit.startswith("g/cm") else None
    # stresses/moduli -> MPa
    if unit == "GPa":
        return val * 1000.0
    if unit == "MPa":
        return val
    return None


def _parse_makeitfrom(html: str) -> dict:
    """Extract {key: value} from a MakeItFrom material-properties page.

    Each property is a ``<div class="mech|therm|other"><p>NAME</p> (data-bars)* <p>VALUE</p></div``
    block. We split on the block opening tags, read NAME from the first ``<p>`` and take
    the first later ``<p>`` that carries a recognizable metric unit."""
    props: dict = {}
    starts = [(m.start(), m.group(1)) for m in re.finditer(r'<div class="(mech|therm|other)">', html)]
    starts.append((len(html), None))
    for i in range(len(starts) - 1):
        seg = html[starts[i][0]:starts[i + 1][0]]
        ps = re.findall(r"<p>(.*?)</p>", seg, re.S)
        if not ps:
            continue
        name = _strip(ps[0])
        key = _NAME_MAP.get(name)
        if key is None or key in props:
            continue
        for cand in ps[1:]:
            val = _to_value(key, _strip(cand))
            if val is not None:
                props[key] = val
                break
    return props


def _first_material_link(html: str) -> Optional[str]:
    m = re.search(r'/material-properties/([A-Za-z0-9._-]+)', html)
    return m.group(1) if m else None


# MakeItFrom exposes stable per-material detail slugs but no headless-reachable
# search endpoint (live-probe 2026-07-08: /search and /explore return errors),
# so we resolve a named alloy to its slug directly. Curated slugs for common
# engineering/robotics-rover alloys (verified against live detail pages); anything
# else falls back to a best-effort heuristic then, last, the (currently broken)
# on-site search — so this keeps working if MakeItFrom ever restores search.
_COMMON_SLUGS: dict[str, str] = {
    "6061-t6": "6061-T6-Aluminum",
    "6063-t6": "6063-T6-Aluminum",
    "7075-t6": "7075-T6-Aluminum",
    "2024-t3": "2024-T3-Aluminum",
    "5052-h32": "5052-H32-Aluminum",
    "5083-h116": "5083-H116-Aluminum",
    "3003-h14": "3003-H14-Aluminum",
    "aisi 4130": "Normalized-4130-Cr-Mo-Steel",
    "4130": "Normalized-4130-Cr-Mo-Steel",
    "aisi 4140": "Normalized-4140-Cr-Mo-Steel",
    "4140": "Normalized-4140-Cr-Mo-Steel",
    "aisi 1018": "Cold-Drawn-1018-Carbon-Steel",
    "1018": "Cold-Drawn-1018-Carbon-Steel",
    "304 stainless": "Annealed-304-Stainless-Steel",
    "304": "Annealed-304-Stainless-Steel",
    "316 stainless": "Annealed-316-Stainless-Steel",
    "316": "Annealed-316-Stainless-Steel",
    # NOTE: only slugs verified against live MakeItFrom detail pages (2026-07-08,
    # sequential/rate-limited) are listed. A36 and Ti-6Al-4V are intentionally
    # absent — their MakeItFrom slug wasn't confirmable and both are already served
    # authoritatively by the first-party bundled registry, so an unknown name here
    # falls through to an honest not_found rather than a 404-ing guess.
}

# Aluminum temper designation like "6061-T6", "5052-H32" -> "<desig>-Aluminum".
_AL_DESIG_RE = re.compile(r"^\d{4}[- ]?[THOF]\d{1,3}$", re.IGNORECASE)


def _resolve_slug(material: str) -> Optional[str]:
    """Best-effort MakeItFrom detail slug for a named material (no fetch)."""
    m = str(material).strip()
    # An explicit slug or URL passed straight through.
    if "/material-properties/" in m:
        return _first_material_link(m)
    key = re.sub(r"\s+", " ", m).strip().lower()
    if key in _COMMON_SLUGS:
        return _COMMON_SLUGS[key]
    norm = key.replace(" ", "-")
    if _AL_DESIG_RE.match(norm):
        return f"{norm.upper()}-Aluminum"
    return None


# --- public API ---------------------------------------------------------------------
def material_web_lookup(material: str, property: Optional[str] = None,
                        source: str = "makeitfrom") -> dict:
    """OPT-IN external web point-lookup of material properties. Off by default; see the
    module docstring. Returns the ``material_lookup`` envelope with ``confidence="web"``
    on success, or an honest ``fail(...)`` degradation otherwise (never raises)."""
    # 1. OPT-IN GATE — must be first, no I/O before it.
    if os.environ.get(_ENABLE_ENV) not in ("1", "true", "yes"):
        return fail("disabled",
                    "External web material lookup is opt-in; set "
                    f"{_ENABLE_ENV}=1 to enable. See engine/materials/registry.json for "
                    "the built-in first-party data.")

    if not material or not str(material).strip():
        return fail("bad_query", "Provide a material name (e.g. '6061-T6', 'A36 steel').")

    src = WEB_SOURCES.get(str(source).strip().lower())
    if src is None:
        return fail("bad_source", f"Unknown source {source!r}; known: {sorted(WEB_SOURCES)}.")

    # 2. Refuse known bot-walled sources WITHOUT fetching.
    if src.get("bot_walled"):
        return fail("bot_walled",
                    "MatWeb serves a CAPTCHA to automated sessions; this tool never "
                    "bypasses bot-walls. Use the first-party registry (material_lookup) "
                    "or a source that permits access.")

    site = src["key"]

    # 3. Locate the detail page. Resolve the slug directly (curated map / heuristic)
    #    since MakeItFrom has no headless-reachable search; only if that misses do we
    #    fall back to the on-site search (currently broken, kept for forward-compat).
    slug = _resolve_slug(material)
    if slug is None:
        search_url = src["search_url"](material)
        sres = bsession.browser_fetch(search_url, site)
        if not sres.get("ok"):
            return fail("not_found",
                        f"No {src['label']} slug known for {material!r} and on-site search is "
                        f"unavailable ({sres.get('reason')}). Pass an exact MakeItFrom slug "
                        f"(e.g. '6061-T6-Aluminum') or a named alloy in the curated set.")
        slug = _first_material_link(sres.get("content", "") or "")
        if not slug:
            return fail("not_found", f"No {src['label']} material page found for {material!r}.")

    detail_url = src["material_url"](slug)
    dres = bsession.browser_fetch(detail_url, site)
    if not dres.get("ok"):
        return fail(dres.get("reason", "browser_fetch_failed"),
                    f"{src['label']} page unavailable ({dres.get('reason')}); not retrying.")

    # 4. Parse.
    props = _parse_makeitfrom(dres.get("content", "") or "")
    if not props:
        return fail("parse_failed",
                    f"Fetched {detail_url} but parsed no known properties.")

    citation = f"{src['label']} — {detail_url}"

    if property is not None:
        pk = _resolve_prop(property)
        if pk is None:
            return fail("not_found",
                        f"Unknown property {property!r}; known: {sorted(_PROP_UNITS)}.")
        if pk not in props:
            return fail("not_found",
                        f"{src['label']} page for {material!r} had no {property!r}.")
        unit, phrase = _PROP_UNITS[pk]
        val = props[pk]
        ans = f"{material} {phrase} is {val}" + (f" {unit}" if unit else "")
        return envelope(
            ans, units=unit, source_url=detail_url, confidence="web",
            material=material, property=pk, value=val, citation=citation,
            source=site,
        )

    ans = f"{material} ({src['label']}): " + ", ".join(f"{k}={v}" for k, v in props.items())
    return envelope(
        ans, units=None, source_url=detail_url, confidence="web",
        material=material, properties=props, citation=citation, source=site,
    )
