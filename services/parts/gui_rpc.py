"""GUI-shaped worker RPCs for the Parts Library workspace (host/src/ui/parts/).

The desktop workspace speaks a flat item/category/vendor vocabulary (name, vendor,
part_number, price_usd, …) rather than the PartRecord envelope, and needs a few
verbs the MCP surface exposes only to agents (vendor login, the fetch queue).
This module adapts :mod:`services.parts` + :mod:`services.browser` into
that vocabulary. Registered append-only from :mod:`.worker_rpc`.

Every method degrades gracefully (ok:false envelope, never a raw traceback), and
never blocks the worker loop: vendor login runs on a daemon thread and the GUI
polls ``vendor_status``.
"""
from __future__ import annotations

import base64
import hashlib
import json
import mmap
import re
import os
import sqlite3
import threading
import time
import urllib.parse
import tempfile
from pathlib import Path
from typing import Any, Callable
from contextvars import copy_context
from services.agent.request_context import owner_key

# ---- vocabulary ------------------------------------------------------------------

_DOMAIN_LABELS: dict[str, str] = {
    "fastener":    "Fasteners",
    "bearing":     "Bearings",
    "mech_family": "Mechanical Families",
    "section":     "Structural Sections",
    "fitting":     "Pipe Fittings",
    "hydraulic":   "Hydraulics",
    "ecad":        "Electronics (ECAD)",
    "motor":       "Electric Motors",
    "motion":      "Motion Control",
    "power_supply": "Power Supplies",
    "battery":     "Batteries",
    "seal":        "Seals & Gaskets",
    "electromech": "Electro-Mechanical",
}

# Friendly labels for browser-catalog sites; adapters may also carry a ``label``
# attribute which wins over this map.
_SITE_LABELS: dict[str, str] = {
    "mcmaster":         "McMaster-Carr",
    "grabcad":          "GrabCAD",
    "traceparts":       "TraceParts",
    "amazon":           "Amazon Business",
    "jlcpcb":           "JLCPCB",
    "pcbway":           "PCBWay",
    "misumi":           "MISUMI",
    "lcsc":             "LCSC",
    "digikey":          "DigiKey",
    "snapeda":          "SnapEDA",
    "content3d":        "3D ContentCentral",
    "3dcontentcentral": "3D ContentCentral",
    "homedepot":        "Home Depot",
    "lowes":            "Lowe's",
    "acehardware":      "Ace Hardware",
    "grainger":         "Grainger",
    "fastenal":         "Fastenal",
    "harborfreight":    "Harbor Freight",
    "tractorsupply":    "Tractor Supply",
    "onlinemetals":     "OnlineMetals",
    "metalsdepot":      "Metals Depot",
    "printables":       "Printables",
    "makerworld":       "MakerWorld",
}

# API pricing providers (the engine_oracle/parts_providers.py chain was deleted —
# the C++ sourcing RPC owns pricing; this pane still shows/configures the
# credential slots) and the credential fields each needs. Shown in the vendor pane as
# "API" sources so the user can configure them in-app instead of hand-editing config.json.
_API_PROVIDERS: dict[str, dict[str, Any]] = {
    "nexar":   {"label": "Nexar (Octopart)", "fields": ["client_id", "client_secret"],
                "env": {"client_id": "NEXAR_CLIENT_ID", "client_secret": "NEXAR_CLIENT_SECRET"}},
    "digikey_api": {"label": "DigiKey API", "config_key": "digikey",
                    "fields": ["client_id", "client_secret"],
                    "env": {"client_id": "DIGIKEY_CLIENT_ID", "client_secret": "DIGIKEY_CLIENT_SECRET"}},
    "mouser":  {"label": "Mouser API", "fields": ["api_key"],
                "env": {"api_key": "MOUSER_API_KEY"}},
}


def _resolve_creds(key: str, env_map: dict[str, str]) -> dict[str, str]:
    """Resolve provider credentials from config first, then environment fallback.

    Local home after parts_providers.py was deleted: config.json →
    ``parts_providers.<key>`` (same section ``_vendor_creds_set`` writes), then env.
    Absent credentials resolve to empty strings.
    """
    cfg: dict = {}
    try:
        from services import paths
        cfg_path = paths.find_config()
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            cfg = (data.get("parts_providers") or {}).get(key) or {}
            if not isinstance(cfg, dict):
                cfg = {}
    except Exception:  # noqa: BLE001 — config is best-effort
        cfg = {}
    out: dict[str, str] = {}
    for field_name, env_name in env_map.items():
        val = cfg.get(field_name) or os.environ.get(env_name)
        out[field_name] = str(val).strip() if val else ""
    return out


def _site_label(key: str, adapter: Any = None) -> str:
    lbl = getattr(adapter, "label", None)
    return lbl or _SITE_LABELS.get(key, key.replace("_", " ").title())


# Registrable-domain per browser-catalog site, for pulling the vendor's live logo (favicon).
_SITE_DOMAINS: dict[str, str] = {
    "mcmaster": "mcmaster.com", "grabcad": "grabcad.com", "traceparts": "traceparts.com",
    "amazon": "amazon.com", "jlcpcb": "jlcpcb.com", "pcbway": "pcbway.com",
    "misumi": "misumi.com", "lcsc": "lcsc.com", "digikey": "digikey.com",
    "snapeda": "snapeda.com", "content3d": "3dcontentcentral.com",
    "3dcontentcentral": "3dcontentcentral.com", "homedepot": "homedepot.com",
    "lowes": "lowes.com", "acehardware": "acehardware.com", "grainger": "grainger.com",
    "fastenal": "fastenal.com", "harborfreight": "harborfreight.com",
    "tractorsupply": "tractorsupply.com", "onlinemetals": "onlinemetals.com",
    "metalsdepot": "metalsdepot.com",
    "printables": "printables.com", "makerworld": "makerworld.com",
}


def _site_domain(site: str) -> str:
    """Registrable domain for a site key — the static map first, then the browser
    adapter's BASE_URL netloc, else ``<site>.com``. Feeds the live favicon URL."""
    if site in _SITE_DOMAINS:
        return _SITE_DOMAINS[site]
    try:
        import importlib
        import urllib.parse
        mod = importlib.import_module(f"services.browser.adapters.{site}")
        base = getattr(mod, "BASE_URL", "")
        host = urllib.parse.urlparse(base).netloc
        if host:
            return host[4:] if host.startswith("www.") else host
    except Exception:  # noqa: BLE001 — no adapter module / no BASE_URL
        pass
    return f"{site}.com"


# ---- record → GUI item mapping ----------------------------------------------------

def _first_price(rec: dict) -> tuple[float, str]:
    """Best-effort unit price from sourcing rows, then params."""
    for row in [*(rec.get("sourcing") or []), rec.get("params") or {}]:
        if not isinstance(row, dict):
            continue
        for key in ("price", "unit_price", "price_usd"):
            try:
                value = row.get(key)
                if value is not None and float(value) > 0:
                    currency = "USD" if key == "price_usd" else str(row.get("currency") or "").upper()
                    return float(value), currency
            except (TypeError, ValueError):
                continue
    return 0.0, ""


def _params_summary(params: dict, limit: int = 6) -> str:
    bits: list[str] = []
    for k, v in params.items():
        if k != "description" and isinstance(v, (str, int, float)) and str(v):
            bits.append(f"{k}={v}")
        if len(bits) >= limit:
            break
    return "  ".join(bits)


def _vendor_of(rec: dict) -> str:
    pid = rec.get("part_id", "")
    if rec.get("domain") == "catalog" and pid.startswith("catalog:"):
        # part_id is "catalog:<site>::<url>" — site is the token before "::"
        site = pid[len("catalog:"):].split("::", 1)[0]
        return _site_label(site)
    params = rec.get("params") or {}
    for v in [params.get("vendor"), params.get("supplier"),
              *(row.get("vendor") for row in rec.get("sourcing") or []
                if isinstance(row, dict))]:
        if v and str(v).lower() != "library":
            return str(v)
    return ""


def _material_of(rec: dict) -> str:
    params = rec.get("params") or {}
    for k in ("material", "material_name", "grade", "alloy"):
        v = params.get(k)
        if v:
            return str(v)
    return ""


def _record_to_item(rec: dict) -> dict:
    price, currency = _first_price(rec)
    params = rec.get("params") or {}
    gref = rec.get("geometry_ref") or {}
    desc = _params_summary(params)
    description = rec.get("description") or params.get("description")
    if description:
        desc = f"{description}  {desc}".strip()
    if rec.get("standard"):
        desc = f"{rec['standard']}  {desc}".strip()
    title = str(rec.get("title") or "")
    if gref.get("kind") == "core_family" and gref.get("family") == "hobby_servo":
        # Normalize older servo records that carry a descriptive sentence as title.
        desc = f"{title}  {desc}".strip()
        title = f"Hobby Servo {rec.get('designation') or gref.get('size') or ''}".strip()
    length = (rec.get("insert_params") or {}).get("length_mm")
    if length is not None and gref.get("kind") == "core_family":
        family_label = {"shcs": "Socket Head Cap Screw", "bhcs": "Button Head Cap Screw",
                        "fhcs": "Flat Head Cap Screw", "hex_bolt": "Hex Head Bolt"}
        title = f"{family_label.get(gref.get('family'), title)} {rec.get('designation')} × {length:g} mm"
    part_id = str(rec.get("part_id") or "")
    part_number = rec.get("designation") or ""
    if not part_number and part_id.startswith("catalog:") and "::" in part_id:
        part_number = urllib.parse.urlsplit(part_id.split("::", 1)[1]).path.rsplit("/", 1)[-1]
    if not part_number:
        part_number = part_id.rsplit(":", 1)[-1]
    if length is not None:
        part_number = f"{rec.get('designation')}×{length:g}"
    if params.get("model_file"):
        part_number = str(params["model_file"])
    designation = str(rec.get("designation") or "")
    family = str(rec.get("family") or "")
    if not family and gref.get("kind") == "core_family" and designation:
        suffix = " " + designation
        if title.endswith(suffix):
            family = title[:-len(suffix)]
    stock = params.get("stock")
    if stock is None:
        stock = next((row.get("stock") for row in rec.get("sourcing") or []
                      if isinstance(row, dict) and row.get("stock") is not None), None)
    try:
        stock = int(stock) if stock is not None else -1
    except (TypeError, ValueError, OverflowError):
        stock = -1
    formats = params.get("model_formats") or []
    if not isinstance(formats, list):
        formats = []
    image_url = str(params.get("image_url") or params.get("thumbnail") or "")
    return {
        "id":          rec.get("part_id", ""),
        "name":        title,
        "vendor":      _vendor_of(rec),
        "part_number": part_number,
        "description": desc,
        "unit":        "ea",
        "price_usd":   price if currency == "USD" else 0.0,
        "price":       price,
        "currency":    currency,
        "family":      family,
        "designation": designation,
        "kind":        "" if rec.get("domain") == "library" else str(params.get("kind") or ""),
        "thumbnail_url": image_url,
        "mpn":         str(params.get("mpn") or ""),
        "manufacturer": str(params.get("manufacturer") or ""),
        "stock":       stock,
        "model_formats": [str(fmt) for fmt in formats],
        "material":    _material_of(rec),
        "domain":      rec.get("domain", ""),
        "standard":    rec.get("standard") or "",
        "source":      rec.get("source", "local") or "local",
        "downloaded":  bool(gref.get("kind") == "import_file" and
                            Path(str(gref.get("path") or "")).is_file()),
        "downloadable": bool(rec.get("artifact") and
                             not (gref.get("kind") == "import_file" and
                                  Path(str(gref.get("path") or "")).is_file())),
        "placeable":   bool(gref.get("kind") and
                            (gref.get("kind") != "import_file" or
                             Path(str(gref.get("path") or "")).is_file())),
        "datasheet":   str(params.get("datasheet_url", "") or params.get("datasheet", "")),
        "image_url":   image_url,
        "license":     rec.get("license", ""),
        "file_name":   str(params.get("model_file") or ""),
        "insert_params": rec.get("insert_params") or {},
        "score":       rec.get("score", 0),
        "sizes":       rec.get("sizes", []),
    }


# ---- categories -------------------------------------------------------------------

def _parts_list_categories(params: dict, _emit: Callable) -> dict:
    try:
        cats: list[dict] = [
            {"id": dom, "label": lbl, "parent_id": ""}
            for dom, lbl in _DOMAIN_LABELS.items()
        ]
        cats.append({"id": "catalog", "label": "Vendor Catalogs", "parent_id": ""})
        try:
            from services.browser import adapters as _adapters
            for a in _adapters.all_adapters():
                cats.append({"id": f"catalog:{a.key}", "label": _site_label(a.key, a),
                             "parent_id": "catalog"})
        except Exception:  # noqa: BLE001 — browser layer optional
            pass
        return {"ok": True, "categories": cats}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_list_categories failed: {exc}"}


# ---- taxonomy (Parts Library V2) ---------------------------------------------------

def _parts_taxonomy(params: dict, _emit: Callable) -> dict:
    """Category taxonomy tree for the GUI — nodes carry EFFECTIVE (inherited) facets.

    ``{category_id?}`` → ``{ok, version, tree}``; with a category_id, ``tree`` is
    ``[that subtree]``. Unknown ids degrade to ok:false (never a traceback).
    """
    try:
        from . import taxonomy as _tax
        cid = str((params or {}).get("category_id", "")).strip()
        doc = _tax.load_taxonomy()
        tree = _tax.flat_tree_for_gui(cid or None)
        if cid and not tree:
            return {"ok": False, "reason": f"unknown category_id: {cid!r}"}
        return {"ok": True, "version": int(doc.get("version", 1)), "tree": tree}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_taxonomy failed: {exc}"}


# ---- search -----------------------------------------------------------------------

_COMMUNITY_SITES = ("printables", "makerworld")


from .search_relevance import (descriptive_relevant as _descriptive_relevant,
                               live_relevant as _live_relevant, score_record as _search_score)
from .taxonomy import sites_for_query as _routed_sites


def _family_size_options(records: list[dict]) -> None:
    """Attach the actual cached core catalog sizes to parametric hits."""
    families = {str((r.get("geometry_ref") or {}).get("family") or "")
                for r in records if (r.get("geometry_ref") or {}).get("kind") == "core_family"}
    if not families:
        return
    from services.parts import _get_hub
    for provider in _get_hub()._registry.get_by_domain("mech_family"):
        if getattr(provider, "provider_id", "") != "core_families":
            continue
        options = {str(entry.get("key") or entry.get("family") or ""): entry.get("sizes", [])
                   for entry in provider._load_catalog() if isinstance(entry, dict)}
        for rec in records:
            family = str((rec.get("geometry_ref") or {}).get("family") or "")
            if family in families:
                rec["sizes"] = options.get(family, [])
        break




def _cef_unavailable() -> bool:
    """Inspect the selected binary without spawning a desktop or CEF process."""
    from services.browser.driver import headless_backend
    if headless_backend() != "cef":
        return False
    from services.browser.cef_client import resolve_cef_bin
    binary = resolve_cef_bin()
    if binary is None or not binary.is_file():
        return True
    cache = binary.parent.parent / "CMakeCache.txt"
    if cache.is_file():
        config = cache.read_text(errors="replace")
        if "CHUDWARE_ENABLE_CEF:BOOL=OFF" in config:
            return True
        if "CHUDWARE_ENABLE_CEF:BOOL=ON" in config:
            return False
    with binary.open("rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as image:
        return b"this build has no CEF backend" in image or b"--cef-daemon" not in image


def _printables_file_names(hits: list[dict]) -> dict[str, str]:
    """Preview the file PrintablesAdapter.acquire selects, in one API request."""
    from services.browser.adapters import printables
    models = []
    for hit in hits:
        url = str(hit.get("url") or "")
        match = re.search(r"/model/(\d+)(?:[-/]|$)", urllib.parse.urlsplit(url).path)
        if match:
            models.append((url, match.group(1)))
    if not models:
        return {}
    selection = " ".join(
        f'p{i}:print(id:"{model_id}"){{stls{{id name fileSize}}}}'
        for i, (_, model_id) in enumerate(models))
    response = printables._query(f"query{{{selection}}}", {})
    data = response.get("data") or {}
    # Same CAD precedence and filename tie-break as PrintablesAdapter.acquire.
    priority = {".step": 0, ".stp": 0, ".iges": 1, ".igs": 1,
                ".3mf": 2, ".stl": 3, ".obj": 4, ".zip": 5}
    names = {}
    for i, (url, _) in enumerate(models):
        files = [file for file in ((data.get(f"p{i}") or {}).get("stls") or [])
                 if isinstance(file, dict) and
                 Path(str(file.get("name") or "")).suffix.lower() in priority]
        files.sort(key=lambda file: (
            priority[Path(str(file["name"])).suffix.lower()], str(file["name"])))
        if files:
            names[url] = str(files[0]["name"])
    return names


def _community_records(site: str, query: str) -> list[dict]:
    from services.browser.adapters import get_adapter
    from services.browser.adapters.parts_provider import BrowserCatalogAdapter
    adapter = get_adapter(site)
    if adapter is None:
        return [{"ok": False, "site": site, "reason": "unavailable",
                 "hint": "No search adapter is registered for this site."}]
    records = []
    provider = BrowserCatalogAdapter(adapter)
    hits = adapter.search(query)
    try:
        filenames = _printables_file_names(hits) if site == "printables" else {}
    except Exception:  # CAD file previews are optional; search results still matter
        filenames = {}
    for hit in hits:
        if hit.get("ok") is False:
            records.append({**hit, "site": site})
            continue
        if not hit.get("url") or not hit.get("title"):
            continue
        rec = provider.record_from_hit(hit)
        if filenames.get(hit["url"]):
            rec["params"]["model_file"] = filenames[hit["url"]]
        records.append(rec)
    return records


def _category_terms(node: dict) -> list[list[str]]:
    """Use this category's own hints and descendants, not broad ancestor hints."""
    terms = []
    for phrase in node.get("query_terms") or []:
        words = re.findall(r"[a-z0-9]+", str(phrase).lower())
        if words:
            terms.append(words)
    for child in node.get("children") or []:
        if isinstance(child, dict):
            terms.extend(_category_terms(child))
    return terms


def _matches_category(rec: dict, rt: dict) -> bool:
    terms = rt.get("category_terms") or []
    if not terms:
        return True
    if rt["category_id"] == "fasteners" and rec.get("domain") == "fastener":
        return True
    params = rec.get("params") or {}
    text = " ".join([str(rec.get("title") or ""), str(rec.get("family") or ""),
                     str((rec.get("geometry_ref") or {}).get("family") or ""),
                     *(str(alias) for alias in rec.get("aliases") or []),
                     *(str(value) for value in params.values())]).lower()
    words = set(re.findall(r"[a-z0-9]+", text))
    return any(all(word in words or word + "s" in words for word in phrase) for phrase in terms)


def _compute_routing(p: dict) -> dict:
    """Resolve a GUI search request into engine routing (domain / site / vendor
    fan-out list / facet defs). Shared by the instant local search and the
    streaming live fan-out so both agree on where a query is sent."""
    query       = str(p.get("query", "")).strip()
    category    = str(p.get("category", "")).strip()
    category_id = str(p.get("category_id", "")).strip()

    domain: str | list[str] | None = None
    site: str | None = None
    route_sites: list[str] = []
    query_terms: list[str] = []
    facet_defs: list[dict] = []
    category_terms: list[list[str]] = []

    if category_id:
        from . import taxonomy as _tax
        node = _tax.get_node(category_id)
        if node is None:
            raise ValueError(f"Unknown category: {category_id}")
        category_terms = _category_terms(node)
        routing = _tax.route(category_id)
        domain = routing["domains"] or None
        route_sites = routing["sites"]
        query_terms = routing["query_terms"]
        facet_defs = _tax.effective_facets(category_id)
    elif category.startswith("catalog:"):
        # Legacy flat-category vocabulary (kept working verbatim).
        domain, site = "catalog", category.split(":", 1)[1]
    elif category:
        domain = category

    if site:
        route_sites = [site]
    elif query and not category_id:
        route_sites = _routed_sites(query)
        if not category and not p.get("doc_id"):
            from .taxonomy import infer_category, route
            inferred = infer_category(query)
            if inferred:
                domain = route(inferred)["domains"] or None
    elif query and route_sites and domain != "catalog":
        from .taxonomy import prioritize_sites
        route_sites = prioritize_sites(route_sites)

    # Category hints are alternatives for browsing an empty category, not terms
    # to append to an already precise designation search.
    vendor_query = query or " ".join(t for t in query_terms if t).strip()
    return {"query": query, "category_id": category_id, "domain": domain, "site": site,
            "route_sites": route_sites, "facet_defs": facet_defs,
            "category_terms": category_terms, "vendor_query": vendor_query}


def _split_notices(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separate real records from structured failures and incomplete notice stubs."""
    notices: list[dict] = []
    seen_notice: set = set()
    clean: list[dict] = []
    for r in records:
        pid = str(r.get("part_id", "") or "")
        title = str(r.get("title", "") or "").strip()
        if r.get("ok") is not False and pid and title:
            clean.append(r)
            continue
        n_site = str(r.get("site", "") or "")
        if not n_site and pid.startswith("catalog:"):
            n_site = pid[len("catalog:"):].split("::", 1)[0]
        n_reason = str(r.get("reason", "") or "unavailable")
        if (n_site, n_reason) not in seen_notice:
            seen_notice.add((n_site, n_reason))
            notices.append({"site": n_site, "reason": n_reason,
                            "hint": str(r.get("hint", "") or "")})
    return clean, notices


def _filter_items(records: list[dict], p: dict, facet_defs: list[dict]) -> list[dict]:
    """Convert records → GUI items and apply the GUI post-filters (price / material /
    vendor / source / facets)."""
    facets_q = p.get("facets") if isinstance(p.get("facets"), dict) else {}
    items = [_record_to_item(r) for r in records]

    def _f(v: Any, dflt: float) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return dflt
    price_min = _f(p.get("price_min"), 0.0)
    price_max = _f(p.get("price_max"), 0.0)
    material  = str(p.get("material", "")).strip().lower()
    vendor    = str(p.get("vendor", "")).strip().lower()
    source    = str(p.get("source", "")).strip().lower()  # "", "local", "live"

    if facets_q:
        from .source import facet_params_view as _fpv
        from .taxonomy import match_facets as _match_facets
        items = [i for i, r in zip(items, records)
                 if _match_facets(_fpv(r.get("params") or {}, facets_q.keys()),
                                  facets_q, facet_defs)]
    if price_min > 0:
        items = [i for i in items if i["price_usd"] >= price_min]
    if price_max > 0:
        items = [i for i in items if 0 < i["price_usd"] <= price_max]
    if material:
        items = [i for i in items if material in i["material"].lower()]
    if vendor:
        items = [i for i in items if vendor in i["vendor"].lower()]
    if source in ("local", "live"):
        items = [i for i in items if i["source"] == source]
    return items


# ---- index status ------------------------------------------------------------------

_INDEX_STATUS_FALLBACK: dict[str, Any] = {
    "building": False, "built": True, "count": -1, "source": "unknown", "age_s": 0.0,
}


def _index_status() -> dict:
    """Best-effort ``{building, built, count, source, age_s}`` from the parts hub.

    Defensive on purpose: ``PartsHub.index_status`` is a newer addition, so an older
    hub (or any bootstrap failure) reports the "assume ready, count unknown" fallback
    rather than sinking the search that asked.
    """
    out = dict(_INDEX_STATUS_FALLBACK)
    try:
        from services.parts import _get_hub
        fn = getattr(_get_hub(), "index_status", None)
        if fn is None:
            st = {}
        else:
            st = fn()
        if isinstance(st, dict):
            for k in ("building", "built", "count", "source", "age_s",
                      "diagnostics", "native_ecad_available"):
                if k in st:
                    out[k] = st[k]
    except Exception as exc:  # noqa: BLE001 — status is advisory, never fatal
        out["diagnostics"] = [{"provider": "parts_hub", "operation": "status",
                               "error": f"{type(exc).__name__}: {exc}"}]
    from services.parts.worker_rpc import REGISTRATION_DIAGNOSTICS
    out["diagnostics"] = [*out.get("diagnostics", []), *REGISTRATION_DIAGNOSTICS]
    return out


def _parts_index_status(params: dict, _emit: Callable) -> dict:
    """Worker method ``parts_index_status`` → parts-index build/freshness state.

    Lets the GUI paint a "building index…" affordance instead of an empty pane while
    the FTS index warms."""
    return {"ok": True, **_index_status()}


# Bounds expensive filtering/sorting while making truncation visible to callers.
_SEARCH_CANDIDATE_CAP = 20000


def _sort_items(items: list[dict], sort: Any, *, query: str, combined: bool) -> None:
    mode = {"0": "relevance", "1": "price_asc", "2": "price_desc", "3": "name"}.get(
        str(sort), str(sort or "relevance").lower())
    if mode == "name":
        items.sort(key=lambda item: (item["name"].casefold(), item["id"]))
    elif mode in ("price_asc", "price_desc"):
        sign = 1 if mode == "price_asc" else -1
        items.sort(key=lambda item: (item["price_usd"] <= 0,
                                     sign * item["price_usd"], item["id"]))
    elif combined:
        items.sort(key=lambda item: (
            -item["score"], not (item["source"] == "local" and item["placeable"])))
    elif query:
        items.sort(key=lambda item: -item["score"])


def _parts_search(params: dict, _emit: Callable) -> dict:
    """Instant local parts search.

    The default returns only indexed local parts immediately. Callers stream public
    CAD models and available vendors with ``parts_search_live_start`` /
    ``parts_search_live_poll``; explicit ``live=True`` requests a blocking merge."""
    try:
        from services.parts import search as _search
        p = params or {}
        limit = max(1, min(int(p.get("limit", 50)), 200))
        offset = max(0, int(p.get("offset", 0)))
        want_live = (bool(p.get("live", False)) and p.get("include_live", True)
                     and str(p.get("source", "")).lower() != "local")
        try:
            rt = _compute_routing(p)
        except ValueError as exc:
            return {"ok": False, "reason": "unknown_category", "hint": str(exc)}

        records = _search(rt["query"], domain=rt["domain"],
                          limit=_SEARCH_CANDIDATE_CAP + 1, include_live=False)
        total_exact = len(records) <= _SEARCH_CANDIDATE_CAP
        records = records[:_SEARCH_CANDIDATE_CAP]
        if rt["site"]:
            prefix = f"catalog:{rt['site']}::"
            records = [r for r in records if str(r.get("part_id", "")).startswith(prefix)]

        _family_size_options(records)
        # FTS relaxes terms to synonyms. Exclude visibly unrelated one-token
        # matches and half-matched two-token titles from unfiltered searches.
        scored = []
        terms = rt["query"].lower().split()
        model_pattern = (re.compile(rf"(?<![a-z0-9]){re.escape(terms[0])}(?![a-z0-9])")
                         if len(terms) > 1 and re.fullmatch(r"[a-z]+\d+", terms[0])
                         else None)
        # Standard screw families accept the requested shaft length (M3x10).
        length_match = re.search(
            r"\b(m\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\b", rt["query"], re.I)
        thread_model = length_match.group(1).lower() if length_match else ""
        thread_pattern = (re.compile(rf"(?<![a-z0-9]){re.escape(thread_model)}(?![a-z0-9])")
                          if thread_model else None)
        tooth_variants = [re.compile(rf"(?<![a-z0-9]){t}(?![a-z0-9])")
                          for t in terms if re.fullmatch(r"\d+t", t)]
        for rec in records:
            if not p.get("facets") and not _descriptive_relevant(rec, rt["query"]):
                continue
            score = _search_score(rec, rt["query"])
            title = f"{rec.get('title') or ''} {rec.get('designation') or ''}".lower()
            identity = str(rec.get("designation") or rec.get("title") or "").lower()
            model_mismatch = model_pattern is not None and not model_pattern.search(identity)
            if tooth_variants and rec.get("designation") and not all(
                    variant.search(identity) for variant in tooth_variants):
                continue
            if thread_pattern is not None:
                if not thread_pattern.search(title):
                    continue
                score = min(95, score + 10)
            if rt["query"] and not p.get("facets") and (
                    score == 0 or
                    (len(terms) == 2 and score < 50) or
                    (len(terms) >= 3 and score < 40) or model_mismatch):
                continue
            rec["score"] = score
            gref = rec.get("geometry_ref") or {}
            if (length_match and gref.get("kind") == "core_family" and
                    gref.get("family") in {"shcs", "bhcs", "fhcs", "hex_bolt"} and
                    str(rec.get("designation") or "").lower() == thread_model):
                rec["insert_params"] = {"length_mm": float(length_match.group(2))}
            scored.append(rec)
        records = scored
        if want_live and rt["route_sites"]:
            cef_off = _cef_unavailable()
            for site in rt["route_sites"]:
                if cef_off and site != "printables":
                    continue
                try:
                    if site in _COMMUNITY_SITES:
                        records.extend(_community_records(site, rt["vendor_query"]))
                    else:
                        records.extend(_search(rt["vendor_query"], domain="catalog",
                                               limit=limit, sites=[site]))
                except Exception as exc:  # one optional vendor must not sink local results
                    records.append({"site": site, "reason": "internal_error", "hint": str(exc)})
            if cef_off and any(s != "printables" for s in rt["route_sites"]):
                records.append({"ok": False, "site": "browser",
                                "reason": "cef_unavailable",
                                "hint": "Browser vendors unavailable in this CEF-off build; public CAD search remains available."})

        records, notices = _split_notices(records)
        if want_live:
            records = [r for r in records if r.get("source") != "live"
                       or _live_relevant(r, rt["query"])]
        if rt["category_id"]:
            records = [rec for rec in records if _matches_category(rec, rt)]
        if want_live:
            for rec in records:
                if rec.get("source") == "live" or "score" not in rec:
                    rec["score"] = _search_score(rec, rt["query"])
        items = _filter_items(records, p, rt["facet_defs"])
        _sort_items(items, p.get("sort"), query=rt["query"], combined=want_live)
        idx = _index_status()
        diagnostics = list(idx.get("diagnostics") or [])
        native_deferred = (not rt["query"] and not rt["domain"] and
                           bool(idx.get("native_ecad_available")) and
                           str(p.get("source", "")).lower() != "live")
        if native_deferred:
            notices.append({"site": "KiCad", "reason": "native_browse_deferred",
                            "hint": "Select Electronics or search to include the installed KiCad catalog."})
            diagnostics.append({"provider": "ecad_kicad", "operation": "browse",
                                "reason": "native_browse_deferred",
                                "hint": "Select Electronics or search to include the installed KiCad catalog."})
        # External limits and failed provider searches/builds make the count incomplete.
        total_exact = (total_exact and not want_live and not native_deferred and not any(
            diagnostic.get("operation") in ("enumeration", "search")
            for diagnostic in diagnostics if isinstance(diagnostic, dict)))
        total = len(items)
        page = items[offset:offset + limit]
        has_live = any(i["source"] == "live" for i in page)
        return {"ok": True, "count": len(page), "total": total,
                "total_exact": total_exact, "offset": offset,
                "has_more": offset + len(page) < total,
                "has_live": has_live, "results": page, "notices": notices,
                "index_building": bool(idx.get("building", False)),
                "index_count": int(idx.get("count", -1)),
                "diagnostics": diagnostics}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_search failed: {exc}"}


# ---- streaming live vendor search -------------------------------------------------
#
# Rather than block the search RPC on the slowest vendor, the GUI kicks off a job
# here and polls for results as each vendor lands. Fetches are bounded three-wide:
# cross-site profile locks and rate limits remain independent, while the shared CEF
# renderer stays below the concurrency at which navigation deadlines became flaky.

_live_jobs: dict[str, dict] = {}
_live_lock = threading.Lock()
_live_threads: dict[threading.Thread, tuple[str, str] | None] = {}


def worker_activity() -> dict:
    from services.worker_activity import registry_lock

    owner = owner_key()
    with registry_lock(_live_lock):
        for thread in list(_live_threads):
            if thread.ident is not None and not thread.is_alive():
                _live_threads.pop(thread)
        live = sum(stored_owner == owner for stored_owner in _live_threads.values())
        reserved = sum(not job["done"] and job.get("_owner") == owner
                       for job in _live_jobs.values())
    from services.parts.thumbs import active_future_count
    thumbnails = active_future_count(owner)
    with registry_lock(_login_lock):
        login = sum(t.is_alive() or t.ident is None for t in _login_threads.values())
    with registry_lock(_health_lock):
        health = int(_health_thread is not None
                     and (_health_thread.is_alive() or _health_thread.ident is None))
    return {"by_kind": {"parts_search": reserved, "parts_search_threads": live,
                        "vendor_login": login, "vendor_health": health,
                        "parts_thumbnails": thumbnails}}
_LIVE_JOB_CAP = 8  # retain at most this many tokens per client
# Shared with the hub so concurrent GUI and agent searches obey the same cap.
from .hub import _LIVE_FETCH_CONCURRENCY, _live_fetch_slots, _live_slot_held

# ---- live vendor result cache -----------------------------------------------------
#
# A live fan-out is seconds per vendor. Re-running the same query 30s later used to
# re-scrape everything, so the pane felt broken on a back-and-forth browse. This is a
# bounded on-disk cache of the RAW record list each vendor returned, keyed on
# (site, query, limit, owner and profile state), not on the GUI filters.
#
# Every operation here is exception-proof: any sqlite/FS/JSON problem degrades to
# "no cache" (scrape as before), never to a failed search.

_VENDOR_CACHE_TABLE = "vendor_results"
_VENDOR_CACHE_TRIM_EVERY_S = 60.0
_vendor_cache_lock = threading.Lock()
_vendor_cache_ready = False
_vendor_cache_last_trim = 0.0


def _vendor_cache_enabled() -> bool:
    """Kill switch: ``CHUDWARE_VENDOR_CACHE=0`` disables reads *and* writes."""
    v = str(os.environ.get("CHUDWARE_VENDOR_CACHE", "1")).strip().lower()
    return v not in ("0", "false", "no", "off")


def _vendor_cache_env_num(name: str, default: float) -> float:
    try:
        val = float(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def _vendor_cache_ttl_s() -> float:
    return _vendor_cache_env_num("CHUDWARE_VENDOR_CACHE_TTL_S", 900.0)


def _vendor_cache_max_rows() -> int:
    return int(_vendor_cache_env_num("CHUDWARE_VENDOR_CACHE_MAX_ROWS", 2000.0))


def _vendor_cache_path() -> Any:
    from services import paths
    cache_dir = paths.ROOT / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "vendor_results.sqlite"


def _vendor_cache_conn() -> Any:
    """Open a short-lived connection, creating the schema once per process.

    One connection per operation: cache hits/misses happen at most once per vendor
    scrape, and the fan-out runs on daemon threads where a shared sqlite handle
    would need its own thread-affinity dance for no measurable gain.
    """
    global _vendor_cache_ready
    conn = sqlite3.connect(str(_vendor_cache_path()), timeout=2.0)
    if not _vendor_cache_ready:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_VENDOR_CACHE_TABLE} ("
            "key TEXT PRIMARY KEY, site TEXT, query TEXT, "
            "records_json TEXT, ts REAL, last_used REAL)")
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{_VENDOR_CACHE_TABLE}_last_used "
            f"ON {_VENDOR_CACHE_TABLE}(last_used)")
        conn.commit()
        _vendor_cache_ready = True
    return conn


def _vendor_cache_key(site: str, query: str, limit: int) -> str:
    # Observe identity/login and cookie-file metadata, never cookie contents.
    state: list[str] = [repr(owner_key())]
    try:
        from services.browser import profiles
        profile = profiles._profile_storage(site, create=False)
        state.append(str(profile))
        cookies = (profile / "Default" / "Cookies",
                   profile / "Default" / "Network" / "Cookies", profile / "Cookies")
        for path in (Path(str(profile) + ".json"), Path(str(profile) + ".cookies.json"),
                     *cookies, *(Path(str(path) + "-wal") for path in cookies)):
            try:
                stat = path.stat()
                state.append(f"{stat.st_mtime_ns}:{stat.st_size}")
            except OSError:
                state.append("-")
    except Exception:  # optional browser integration
        state.append("profile-unavailable")
    key = f"v3\n{site}\n{query}\n{limit}\n" + "\n".join(state)
    return hashlib.blake2b(key.encode("utf-8"), digest_size=16).hexdigest()


def _vendor_cache_get(site: str, query: str, limit: int) -> list[dict] | None:
    """Fresh cached records for this vendor query, or None (miss/expired/disabled)."""
    if not _vendor_cache_enabled():
        return None
    key = _vendor_cache_key(site, query, limit)
    try:
        with _vendor_cache_lock:
            conn = _vendor_cache_conn()
            try:
                row = conn.execute(
                    f"SELECT records_json, ts FROM {_VENDOR_CACHE_TABLE} WHERE key=?",
                    (key,)).fetchone()
                if row is None:
                    return None
                now = time.time()
                if now - float(row[1]) > _vendor_cache_ttl_s():
                    conn.execute(f"DELETE FROM {_VENDOR_CACHE_TABLE} WHERE key=?", (key,))
                    conn.commit()
                    return None
                recs = json.loads(row[0])
                if not isinstance(recs, list):
                    return None
                conn.execute(f"UPDATE {_VENDOR_CACHE_TABLE} SET last_used=? WHERE key=?",
                             (now, key))
                conn.commit()
                return [r for r in recs if isinstance(r, dict)]
            finally:
                conn.close()
    except Exception:  # noqa: BLE001 — cache is an optimisation, never a failure mode
        return None


def _vendor_cache_put(site: str, query: str, limit: int, records: list[dict]) -> None:
    """Store the raw records a vendor returned. Silent no-op on any failure."""
    if not _vendor_cache_enabled():
        return
    # Some adapters cannot distinguish a clean empty result from parse failure.
    # Cache only non-empty lists consisting entirely of usable records.
    if not records or any(not isinstance(r, dict)
           or r.get("ok") is False or r.get("reason")
           or not r.get("part_id")
           or not str(r.get("title") or "").strip()
           for r in records):
        return
    global _vendor_cache_last_trim
    key = _vendor_cache_key(site, query, limit)
    try:
        blob = json.dumps(records, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001 — unserialisable record → just don't cache it
        return
    try:
        now = time.time()
        with _vendor_cache_lock:
            conn = _vendor_cache_conn()
            try:
                conn.execute(
                    f"INSERT INTO {_VENDOR_CACHE_TABLE} "
                    "(key, site, query, records_json, ts, last_used) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET records_json=excluded.records_json, "
                    "ts=excluded.ts, last_used=excluded.last_used",
                    (key, site, query, blob, now, now))
                conn.commit()
                # LRU trim, at most once a minute and only ever after a write.
                if now - _vendor_cache_last_trim >= _VENDOR_CACHE_TRIM_EVERY_S:
                    _vendor_cache_last_trim = now
                    cap = _vendor_cache_max_rows()
                    (rows,) = conn.execute(
                        f"SELECT COUNT(*) FROM {_VENDOR_CACHE_TABLE}").fetchone()
                    if int(rows) > cap:
                        conn.execute(
                            f"DELETE FROM {_VENDOR_CACHE_TABLE} WHERE key IN "
                            f"(SELECT key FROM {_VENDOR_CACHE_TABLE} "
                            "ORDER BY last_used ASC LIMIT ?)", (int(rows) - cap,))
                        conn.commit()
            finally:
                conn.close()
    except Exception:  # noqa: BLE001
        return


def vendor_cache_stats() -> dict:
    """Diagnostics for the live-vendor result cache (rows, freshness, bounds)."""
    ttl = _vendor_cache_ttl_s()
    out: dict[str, Any] = {"ok": True, "enabled": _vendor_cache_enabled(),
                           "ttl_s": ttl, "max_rows": _vendor_cache_max_rows(),
                           "rows": 0, "fresh": 0, "stale": 0, "sites": {},
                           "bytes": 0, "path": ""}
    try:
        path = _vendor_cache_path()
        out["path"] = str(path)
        out["bytes"] = path.stat().st_size if path.exists() else 0
        cutoff = time.time() - ttl
        with _vendor_cache_lock:
            conn = _vendor_cache_conn()
            try:
                (rows,) = conn.execute(
                    f"SELECT COUNT(*) FROM {_VENDOR_CACHE_TABLE}").fetchone()
                (fresh,) = conn.execute(
                    f"SELECT COUNT(*) FROM {_VENDOR_CACHE_TABLE} WHERE ts>=?",
                    (cutoff,)).fetchone()
                out["rows"] = int(rows)
                out["fresh"] = int(fresh)
                out["stale"] = int(rows) - int(fresh)
                out["sites"] = {str(s): int(n) for s, n in conn.execute(
                    f"SELECT site, COUNT(*) FROM {_VENDOR_CACHE_TABLE} GROUP BY site")}
            finally:
                conn.close()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"vendor cache unavailable: {exc}",
                "hint": "The cache is optional — searches still work without it."}
    return out


def vendor_cache_purge(site: str | None = None) -> int:
    """Drop cached vendor results (one site, or all). Returns rows deleted, 0 on error."""
    try:
        with _vendor_cache_lock:
            conn = _vendor_cache_conn()
            try:
                if site:
                    cur = conn.execute(
                        f"DELETE FROM {_VENDOR_CACHE_TABLE} WHERE site=?", (site,))
                else:
                    cur = conn.execute(f"DELETE FROM {_VENDOR_CACHE_TABLE}")
                conn.commit()
                return int(cur.rowcount or 0)
            finally:
                conn.close()
    except Exception:  # noqa: BLE001
        return 0


def _run_live_site_inner(site: str, rt: dict, limit: int, p: dict, job: dict) -> None:
    site_status = "done"
    try:
        from services.parts import search as _search
        cached = _vendor_cache_get(site, rt["vendor_query"], limit)
        with _live_lock:
            if job.get("cancelled"):
                site_status = "cancelled"
                return
            vendor = job.get("vendors", {}).get(site)
            if vendor is not None:
                vendor["status"] = "done" if cached is not None else "loading"
        if cached is not None:
            recs = cached
        elif site == "printables":
            recs = _community_records(site, rt["vendor_query"])
        else:
            with _live_fetch_slots:
                with _live_lock:
                    cancelled = job.get("cancelled")
                if cancelled:
                    site_status = "cancelled"
                    return
                token = _live_slot_held.set(True)
                try:
                    if site in _COMMUNITY_SITES:
                        recs = _community_records(site, rt["vendor_query"])
                    else:
                        recs = _search(rt["vendor_query"], domain="catalog", limit=limit, sites=[site])
                finally:
                    _live_slot_held.reset(token)
        with _live_lock:
            if job.get("cancelled"):
                site_status = "cancelled"
                return
        prefix = f"catalog:{site}::"
        recs = [r for r in recs if str(r.get("part_id", "")).startswith(prefix)
                or r.get("ok") is False or r.get("reason")]
        for rec in recs:
            if not rec.get("site") and not rec.get("part_id"):
                rec["site"] = site
        clean, notices = _split_notices(recs)
        clean = [rec for rec in clean
                 if _live_relevant(rec, rt.get("query") or rt["vendor_query"])]
        for rec in clean:
            rec["score"] = _search_score(rec, rt.get("query") or rt["vendor_query"])
        if rt.get("category_id"):
            clean = [rec for rec in clean if _matches_category(rec, rt)]
        if notices:
            site_status = "error"
        items = _filter_items(clean, p, rt["facet_defs"])
        if cached is None and not notices:
            _vendor_cache_put(site, rt["vendor_query"], limit, recs)
        with _live_lock:
            if job.get("cancelled"):
                site_status = "cancelled"
                return
            for item in items:
                if item["id"] not in job["seen_ids"]:
                    job["seen_ids"].add(item["id"])
                    job["results"].append(item)
            for notice in notices:
                key = (notice["site"], notice["reason"])
                if key not in job["seen_notices"]:
                    job["seen_notices"].add(key)
                    job["notices"].append(notice)
    except Exception as exc:  # noqa: BLE001 — report this site's failure and finish the job
        site_status = "error"
        with _live_lock:
            if not job.get("cancelled"):
                key = (site, "internal_error")
                if key not in job["seen_notices"]:
                    job["seen_notices"].add(key)
                    job["notices"].append({"site": site, "reason": "internal_error",
                                           "hint": f"{type(exc).__name__}: {exc}"})
    finally:
        with _live_lock:
            job["pending"] -= 1
            if job["pending"] <= 0:
                job["done"] = True
            vendor = job.get("vendors", {}).get(site)
            if vendor is not None:
                vendor["status"] = site_status


def _run_live_site(site: str, rt: dict, limit: int, p: dict, job: dict) -> None:
    """Run one site; Printables' public API does not consume a browser slot."""
    _run_live_site_inner(site, rt, limit, p, job)


def _parts_search_live_start(params: dict, _emit: Callable) -> dict:
    """Begin a background live vendor fan-out for the current query. Returns a token
    the GUI polls with ``parts_search_live_poll``. No-op (done immediately) when the
    category routes to no live vendors."""
    try:
        import uuid
        p = params or {}
        limit = max(1, min(int(p.get("limit", 50)), 200))
        try:
            rt = _compute_routing(p)
        except ValueError as exc:
            return {"ok": False, "reason": "unknown_category", "hint": str(exc)}
        requested = list(dict.fromkeys(rt["route_sites"])) if p.get("include_live", True) else []
        cef_off = bool(requested) and _cef_unavailable()
        sites = [s for s in requested if not cef_off or s == "printables"]
        skipped = [s for s in requested if s not in sites]
        token = uuid.uuid4().hex
        vendors = {s: {"site": s, "label": _site_label(s), "domain": _site_domain(s),
                       "status": "queued"} for s in sites}
        job = {"results": [], "notices": [], "seen_ids": set(), "seen_notices": set(),
               "pending": len(sites), "done": not sites, "cancelled": False,
               "vendors": vendors, "_owner": owner_key(),
               "_client": (owner_key(), str(p["client"])) if p.get("client") else None}
        if skipped:
            job["notices"].append({
                "site": "browser", "reason": "cef_unavailable",
                "hint": f"Browser vendors unavailable in this CEF-off build ({len(skipped)} skipped); public CAD search remains available.",
            })
        with _live_lock:
            # Supersede only this client's searches; the default is the legacy global client.
            for old in _live_jobs.values():
                if old.get("_client") == job["_client"] and not old.get("done"):
                    old["cancelled"] = True
                    old["done"] = True
            _live_jobs[token] = job
            # Bound retained tokens per client without interrupting other clients.
            own_tokens = [key for key, value in _live_jobs.items()
                          if value.get("_client") == job["_client"]]
            for key in own_tokens[:-_LIVE_JOB_CAP]:
                evicted = _live_jobs.pop(key)
                evicted["cancelled"] = True
        for s in sites:
            context = copy_context()
            thread = threading.Thread(target=context.run,
                                      args=(_run_live_site, s, rt, limit, p, job),
                                      name=f"parts-live-{s}", daemon=True)
            with _live_lock:
                for finished in list(_live_threads):
                    if finished.ident is not None and not finished.is_alive():
                        _live_threads.pop(finished)
                _live_threads[thread] = job["_owner"]
            try:
                thread.start()
            except Exception as exc:  # noqa: BLE001 — other sites can still finish
                with _live_lock:
                    _live_threads.pop(thread, None)
                    job["pending"] -= 1
                    vendors[s]["status"] = "error"
                    job["notices"].append({"site": s, "reason": "internal_error",
                                           "hint": f"Failed to start search: {exc}"})
                    if job["pending"] <= 0:
                        job["done"] = True
        return {"ok": True, "token": token, "sites": sites,
                "vendors": list(vendors.values()), "notices": list(job["notices"]),
                "done": job["done"]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_search_live_start failed: {exc}"}


def _parts_search_live_poll(params: dict, _emit: Callable) -> dict:
    """Drain results a live job has accumulated since ``cursor``. Returns
    ``{ok, results, notices, cursor, done}``; unknown/expired token → done:true."""
    try:
        p = params or {}
        token = str(p.get("token", ""))
        cursor = max(0, int(p.get("cursor", 0)))
        with _live_lock:
            job = _live_jobs.get(token)
            if job is None:
                return {"ok": True, "results": [], "notices": [], "cursor": cursor,
                        "done": True}
            new_items = job["results"][cursor:]
            notices = list(job["notices"])
            done = job["done"]
            new_cursor = len(job["results"])
            # Snapshot per-vendor states BEFORE the pop so the terminal states ride the
            # same payload that carries done:true (else they'd be lost).
            vendors = list(job.get("vendors", {}).values())
            if done:
                _live_jobs.pop(token, None)
        return {"ok": True, "results": new_items, "notices": notices,
                "cursor": new_cursor, "vendors": vendors, "done": done}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_search_live_poll failed: {exc}"}


# ---- download / queue --------------------------------------------------------------

def _parts_download(params: dict, _emit: Callable) -> dict:
    try:
        part_id = str((params or {}).get("item_id", "")).strip()
        if not part_id:
            return {"ok": False, "reason": "item_id required"}
        if not (part_id.startswith("catalog:") and "::" in part_id):
            from services.parts.parts_library import acquire_part
            record = acquire_part(part_id, offline=not (params or {}).get("include_live", True))
            kind = (record.get("geometry_ref") or {}).get("kind")
            return {"ok": True, "downloaded": kind == "import_file", "part": record}
        if not (params or {}).get("include_live", True):
            return {"ok": False, "reason": "Live catalog downloads are unavailable offline."}
        rest = part_id[len("catalog:"):]
        if "::" not in rest:
            return {"ok": False, "reason": f"malformed catalog part_id: {part_id!r}"}
        site, url = rest.split("::", 1)
        from services.browser.queue import enqueue, ensure_worker_started
        ensure_worker_started()
        name = str((params or {}).get("name", "")).strip()
        return enqueue(site, url, preset={"ok": True, "url": url, "title": name or url})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_download failed: {exc}"}


def _parts_download_select(params: dict, _emit: Callable) -> dict:
    """Retry an ambiguous archive acquisition using a verified queue selection."""
    try:
        from services.browser.queue import ensure_worker_started, select_member
        p = params or {}
        result = select_member(p.get("job_id"), p.get("member"), p.get("format"))
        if result.get("ok"):
            ensure_worker_started()
        return result
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": "internal_error", "hint": str(exc)}


def _parts_queue_status(params: dict, _emit: Callable) -> dict:
    try:
        from services.browser.queue import queue_status
        return queue_status()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_queue_status failed: {exc}"}


def _parts_import_artifact(params: dict, _emit: Callable) -> dict:
    """Validate the selected CAD artifact before importing it into the core."""
    try:
        from services.mcp import core_bridge as cb
        from services.browser.cad_validate import extract_cad, validate_cad
        p = params or {}
        path = str(p.get("artifact_path", "")).strip()
        if not path:
            return {"ok": False, "reason": "artifact_path required"}
        from services.agent.request_context import cell_path
        path = str(cell_path(path))
        suffix = Path(path).suffix.lower()
        member = p.get("member") or None
        requested_format = p.get("format") or None
        if member is not None and not isinstance(member, str):
            return {"ok": False, "reason": "invalid_cad", "hint": "Invalid archive member."}
        if requested_format is not None and not isinstance(requested_format, str):
            return {"ok": False, "reason": "invalid_cad", "hint": "Invalid CAD format."}
        if requested_format:
            requested_format = requested_format.strip().lower().lstrip(".")
            if requested_format == "zip":
                requested_format = None

        def import_checked(checked: dict, cad_path: str) -> dict:
            if not checked.get("ok"):
                return {"ok": False, "reason": "invalid_cad",
                        "hint": str(checked.get("reason") or "CAD validation failed"),
                        "candidates": checked.get("candidates", [])}
            fmt = str(checked.get("format") or Path(cad_path).suffix.lower().lstrip("."))
            if fmt not in {"step", "stp", "iges", "igs", "stl", "3mf", "obj"}:
                return {"ok": False, "reason": "unsupported_import_format",
                        "hint": f"The downloaded {fmt or 'unknown'} format cannot be imported natively.",
                        "artifact_path": path, "format": fmt}
            return cb.call("part_import_file", {"path": cad_path,
                           "as_solid": fmt in {"stl", "3mf", "obj"}})

        if suffix == ".zip":
            with tempfile.TemporaryDirectory(prefix="chudware-part-") as directory:
                checked = extract_cad(path, requested_format=requested_format,
                                      requested_member=member, dest_dir=directory)
                return import_checked(checked, str(checked.get("artifact_path") or ""))
        # A member on an already extracted file is provenance, not another selection.
        checked = validate_cad(path, requested_format=requested_format)
        return import_checked(checked, path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_import_artifact failed: {exc}"}


def _parts_insert_into_doc(params: dict, _emit: Callable) -> dict:
    try:
        from services.parts.worker_rpc import _part_place
        part_id = str((params or {}).get("item_id", "")).strip()
        if not part_id:
            return {"ok": False, "reason": "item_id required"}
        placement = dict(params or {})
        placement["part_id"] = part_id
        return _part_place(placement, _emit)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_insert_into_doc failed: {exc}"}


def _parts_prepare_for_assembly(params: dict, emit: Callable) -> dict:
    """Create a library part, save an editable .step+ in the selected project, and return its path.

    The host applies asm_insert_component itself so it uses the same mirror, undo and
    tessellation path as Insert Component. Do not leave an unadopted part doc open.
    """
    from services.agent.request_context import root_value, cell_path
    project_dir = str((params or {}).get("project_dir") or
                      root_value("CHUDWARE_PROJECT_SAVE_DIR") or "").strip()
    if not project_dir:
        return {"ok": False, "reason": "No project folder is available for the component."}
    try:
        directory = cell_path(project_dir)
    except ValueError:
        return {"ok": False, "reason": "cell_refused", "code": "cell_refused"}
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            return {"ok": False, "reason": f"Project folder is not a directory: {directory}"}
    except OSError as exc:
        return {"ok": False, "reason": f"Could not create project folder: {exc}"}

    placed = _parts_insert_into_doc({"item_id": (params or {}).get("item_id"),
                                     "params": (params or {}).get("params"),
                                     "include_live": (params or {}).get("include_live", True)}, emit)
    if not placed.get("ok"):
        return placed
    doc_id = placed.get("doc_id")
    if not isinstance(doc_id, str) or not doc_id:
        return {"ok": False, "reason": "Part placement returned no document to save."}

    from services.mcp import core_bridge as cb
    from services.mcp.cad_common import _project_save_stem
    try:
        name = (placed.get("detail") or {}).get("name")
        stem = _project_save_stem(name if isinstance(name, str) else None, doc_id)
        path = directory / f"{stem}.step+"
        # A second placement must not overwrite a component another assembly already uses.
        if path.exists():
            path = directory / f"{stem}_{doc_id}.step+"
        saved = cb.call("doc_save", {"doc_id": doc_id, "path": str(path)})
        written = saved.get("path", str(path)) if isinstance(saved, dict) else str(path)
        if not Path(written).is_file():
            return {"ok": False, "reason": f"Part was not saved to the project: {written}"}
        return {"ok": True, "path": written}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"Could not save library part to the project: {exc}"}
    finally:
        try:
            cb.call("doc_close", {"doc_id": doc_id})
        except Exception:  # noqa: BLE001 — saved artifact remains usable
            pass


# ---- vendor status / login ----------------------------------------------------------

_login_lock = threading.Lock()
_login_threads: dict[str, threading.Thread] = {}
_login_results: dict[str, dict] = {}


def _login_in_progress(site: str) -> bool:
    t = _login_threads.get(site)
    return bool(t and t.is_alive())


def _vendor_status(params: dict, _emit: Callable) -> dict:
    try:
        sources: list[dict] = []
        try:
            from services.browser import adapters as _adapters
            from services.browser import profiles as _profiles
            # Only surface vendors whose login actually gates CAD/model/datasheet/
            # footprint downloads. Commerce/link-out sites (Ace, Amazon, Home Depot,
            # …) are intentionally hidden so users aren't asked to sign into them.
            try:
                _eligible = set(_adapters.login_wizard_sites())
            except Exception:  # noqa: BLE001 — fall back to the full registry
                _eligible = None
            for a in _adapters.all_adapters():
                key = a.key
                if _eligible is not None and key not in _eligible:
                    continue
                meta = _profiles.load_meta(key)
                last = _login_results.get(key) or {}
                sources.append({
                    "id":                key,
                    "label":             _site_label(key, a),
                    "kind":              "browser",
                    # has_login, NOT has_profile: anonymous fetches auto-create
                    # profile dirs — only a real browser_login/wizard sign-in
                    # (last_login_ts stamp) counts as logged in.
                    "logged_in":         _profiles.has_login(key),
                    "login_in_progress": _login_in_progress(key),
                    "login_error":       "" if last.get("ok", True) else str(
                        last.get("reason", "") or last.get("error", "")),
                    "last_login":        str(meta.get("last_login_ts", "")),
                    "last_used":         str(meta.get("last_used_ts", "")),
                })
        except Exception:  # noqa: BLE001 — browser layer optional
            pass

        api: list[dict] = []
        try:
            for pid, spec in _API_PROVIDERS.items():
                cfg_key = spec.get("config_key", pid)
                creds = _resolve_creds(cfg_key, spec["env"])
                api.append({
                    "id":         pid,
                    "label":      spec["label"],
                    "kind":       "api",
                    "fields":     spec["fields"],
                    "configured": all(creds.get(f) for f in spec["fields"]),
                })
        except Exception:  # noqa: BLE001
            pass

        return {"ok": True, "sources": sources, "api_providers": api}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"vendor_status failed: {exc}"}


def _vendor_login(params: dict, _emit: Callable) -> dict:
    """Start a HEADED login window on a daemon thread — returns immediately.

    The GUI polls ``vendor_status`` (login_in_progress / logged_in) to follow the
    flow; blocking the single worker RPC dispatch thread for up to 300 s here
    would freeze every other panel's calls (see memory: GL-thread RPC freeze).
    """
    from services.agent.request_context import in_cell
    if in_cell():
        return {"ok": False, "reason": "cell_refused", "code": "cell_refused"}
    try:
        site = str((params or {}).get("vendor", "") or (params or {}).get("site", "")).strip().lower()
        if not site:
            return {"ok": False, "reason": "vendor required"}
        with _login_lock:
            if _login_in_progress(site):
                return {"ok": True, "site": site, "in_progress": True, "already": True}

            def _run() -> None:
                try:
                    from services.browser.session import browser_login
                    _login_results[site] = browser_login(site)
                except Exception as exc:  # noqa: BLE001
                    _login_results[site] = {"ok": False, "reason": str(exc)}

            context = copy_context()
            t = threading.Thread(target=context.run, args=(_run,),
                                 name=f"vendor-login-{site}", daemon=True)
            _login_threads[site] = t
            try:
                t.start()
            except BaseException:
                _login_threads.pop(site, None)
                raise
        return {"ok": True, "site": site, "in_progress": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"vendor_login failed: {exc}"}


def _vendor_creds_set(params: dict, _emit: Callable) -> dict:
    """Persist API-provider credentials to the app config (never logged, never committed)."""
    from services.agent.request_context import in_cell
    if in_cell():
        return {"ok": False, "reason": "credential_isolation_unsupported",
                "code": "credential_isolation_unsupported"}
    try:
        p = params or {}
        pid = str(p.get("provider", "")).strip()
        creds = p.get("creds") or {}
        spec = _API_PROVIDERS.get(pid)
        if spec is None:
            return {"ok": False, "reason": f"unknown provider {pid!r}",
                    "hint": f"one of: {', '.join(_API_PROVIDERS)}"}
        if not isinstance(creds, dict):
            return {"ok": False, "reason": "creds must be an object"}
        clean = {k: str(v).strip() for k, v in creds.items() if k in spec["fields"] and str(v).strip()}
        if not clean:
            return {"ok": False, "reason": f"no valid fields; expected: {', '.join(spec['fields'])}"}

        from services import paths
        cfg_path = paths.find_config()
        data: dict = {}
        if cfg_path.exists():
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except Exception:  # noqa: BLE001
                data = {}
        section = data.setdefault("parts_providers", {})
        if not isinstance(section, dict):
            section = data["parts_providers"] = {}
        cfg_key = spec.get("config_key", pid)
        sub = section.setdefault(cfg_key, {})
        if not isinstance(sub, dict):
            sub = section[cfg_key] = {}
        sub.update(clean)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        try:
            os.chmod(cfg_path, 0o600)
        except OSError:
            pass
        return {"ok": True, "provider": pid, "fields_set": sorted(clean)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"vendor_creds_set failed: {exc}"}


# ---- vendor health (Parts Library V2) ------------------------------------------------
#
# Same daemon-thread + poll idiom as vendor_login above: vendors_health_check kicks
# off canary searches on a background thread and returns immediately; the GUI polls
# vendors_health_status. The actual canary logic lives in services/browser/health
# (another agent's module — imported lazily, absence degrades to ok:false).

_health_lock = threading.Lock()
_health_thread: threading.Thread | None = None
_health_state: dict[str, Any] = {
    "results": [], "started_ts": 0.0, "finished_ts": 0.0,
}


def _health_running() -> bool:
    return bool(_health_thread and _health_thread.is_alive())


def _vendors_health_check(params: dict, _emit: Callable) -> dict:
    """Start a per-adapter canary health sweep — returns {ok, started} immediately.

    ``{sites?: [..]}`` restricts the sweep; default is every registered adapter.
    Results shape (polled via vendors_health_status):
    ``[{site, ok, latency_ms, logged_in, hit_count, error}]``.
    """
    global _health_thread
    try:
        sites = (params or {}).get("sites") or None
        if sites is not None and not isinstance(sites, list):
            return {"ok": False, "error": "sites must be a list"}
        try:
            from services.browser import health as _health  # noqa: PLC0415
        except Exception:  # noqa: BLE001 — module lands with the adapter workstream
            return {"ok": False, "error": "health module not available"}
        try:
            from services.browser.embed import get_manager
            if get_manager().wizard_active():
                return {"ok": True, "started": False, "deferred": True,
                        "reason": "login_wizard_active"}
        except Exception:  # noqa: BLE001
            pass

        with _health_lock:
            if _health_running():
                return {"ok": True, "started": False, "already": True}

            import time as _time
            _health_state["results"] = []
            _health_state["started_ts"] = _time.time()
            _health_state["finished_ts"] = 0.0

            # Accept either spelling of the entry point (run_health_check landed first).
            _sweep = getattr(_health, "run_health_checks", None) or \
                getattr(_health, "run_health_check", None)
            if _sweep is None:
                return {"ok": False, "error": "health module not available"}

            def _run() -> None:
                try:
                    results = _sweep(sites=sites)
                    with _health_lock:
                        _health_state["results"] = list(results or [])
                except Exception as exc:  # noqa: BLE001
                    with _health_lock:
                        _health_state["results"] = [
                            {"site": s, "ok": False, "latency_ms": 0,
                             "logged_in": False, "hit_count": 0, "error": str(exc)}
                            for s in (sites or ["*"])
                        ]
                finally:
                    with _health_lock:
                        _health_state["finished_ts"] = _time.time()

            context = copy_context()
            t = threading.Thread(target=context.run, args=(_run,),
                                 name="vendors-health-check", daemon=True)
            _health_thread = t
            try:
                t.start()
            except BaseException:
                _health_thread = None
                raise
        return {"ok": True, "started": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"vendors_health_check failed: {exc}"}


def _vendors_health_status(params: dict, _emit: Callable) -> dict:
    """Poll the health sweep: {ok, running, results, started_ts, finished_ts}."""
    try:
        with _health_lock:
            return {
                "ok":          True,
                "running":     _health_running(),
                "results":     list(_health_state["results"]),
                "started_ts":  _health_state["started_ts"],
                "finished_ts": _health_state["finished_ts"],
            }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"vendors_health_status failed: {exc}"}


# ---- thumbnails ---------------------------------------------------------------------

def _parts_thumbnail(params: dict, emit: Callable) -> dict:
    """Return one preview using the same resolver and encoding as the batch RPC."""
    result = _parts_thumbnails({"items": [params or {}]}, emit)
    if not result.get("ok"):
        return result
    return result["images"][0]


def _parts_thumbnails(params: dict, _emit: Callable) -> dict:
    """Resolve ordered part previews; vendor footer icons use a separate path."""
    from services.parts.thumbs import batch_budget_s, resolve_previews, resolve_vendor_logos

    try:
        raw = (params or {}).get("items", [])
        if not isinstance(raw, list):
            return {"ok": False, "reason": "items must be a list"}
        parts, logos = [], []
        for index, item in enumerate(raw):
            entry = item if isinstance(item, dict) else {}
            item_id = str(entry.get("item_id") or "").strip()
            (logos if item_id.startswith("vendorlogo:") else parts).append((index, entry))
        deadline = time.monotonic() + batch_budget_s()
        results = [None] * len(raw)
        for entries, resolver in ((parts, resolve_previews), (logos, resolve_vendor_logos)):
            if not entries:
                continue
            images = resolver([item for _, item in entries],
                              budget_s=max(0.0, deadline - time.monotonic()))
            for (index, _), image in zip(entries, images):
                results[index] = {"item_id": image["item_id"], "ok": image["ok"],
                                  "image_b64": base64.b64encode(image["data"]).decode("ascii")
                                  if image["data"] is not None else "",
                                  "mime": image["mime"], "provenance": image["provenance"],
                                  "reason": image["reason"]}
        return {"ok": True, "images": results}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_thumbnails failed: {exc}"}


def _parts_match_model(params: dict, _emit: Callable) -> dict:
    """Match a product that lacks a 3D model to a CAD-repo model (contracts/sourcing.md
    §2a). Worker-RPC bridge to ``model_matcher.match_model`` for the GUI's "Find 3D
    model" action. ``{part_id?, title?, mpn?, brand?, limit?}`` → the match envelope
    (matches[], best, fallback). Only ``confidence:"exact"`` / ``attach:true`` should
    be placed without asking; strong/weak need user confirmation; a
    ``fallback.how == "reverse_engineer"`` routes to ``reveng_start_from_product``.
    """
    try:
        from .model_matcher import match_model as _match
        p = params or {}
        pid = str(p.get("part_id", "")).strip()
        title = str(p.get("title", "")).strip()
        mpn = str(p.get("mpn", "")).strip()
        brand = str(p.get("brand", "")).strip()
        # Build the product from the identity fields the GUI already holds. Only
        # enrich from the hub for a LOCAL (non-catalog) part_id when no identity was
        # passed — a catalog: id's get() would trigger a live browser acquire we
        # don't want just to match. A returned error envelope (ok:false) is ignored.
        if pid and not (title or mpn) and not pid.startswith("catalog:"):
            try:
                from services.parts import get as _get_record
                rec = _get_record(pid)
                if isinstance(rec, dict) and rec.get("ok") is not False and \
                        (rec.get("title") or rec.get("params")):
                    return _match(rec, limit=max(1, min(int(p.get("limit", 8)), 50)))
            except Exception:  # noqa: BLE001 — fall through to identity-field match
                pass
        product = {"part_id": pid, "title": title,
                   "params": {"mpn": mpn, "brand": brand}}
        limit = max(1, min(int(p.get("limit", 8)), 50))
        return _match(product, limit=limit)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_match_model failed: {exc}", "hint": ""}


GUI_WORKER_METHODS: dict[str, Callable] = {
    "parts_list_categories": _parts_list_categories,
    "parts_taxonomy":        _parts_taxonomy,
    "parts_search":          _parts_search,
    "parts_index_status":    _parts_index_status,
    "parts_search_live_start": _parts_search_live_start,
    "parts_search_live_poll":  _parts_search_live_poll,
    "parts_match_model":     _parts_match_model,
    "vendors_health_check":  _vendors_health_check,
    "vendors_health_status": _vendors_health_status,
    "parts_download":        _parts_download,
    "parts_download_select": _parts_download_select,
    "parts_queue_status":    _parts_queue_status,
    "parts_insert_into_doc": _parts_insert_into_doc,
    "parts_prepare_for_assembly": _parts_prepare_for_assembly,
    "parts_import_artifact": _parts_import_artifact,
    "vendor_status":         _vendor_status,
    "vendor_login":          _vendor_login,
    "vendor_creds_set":      _vendor_creds_set,
    "parts_thumbnail":       _parts_thumbnail,   # singular: legacy / old-worker fallback
    "parts_thumbnails":      _parts_thumbnails,  # batch: what the GUI grid uses
}
