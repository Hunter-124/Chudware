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
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FutureTimeout, as_completed
from typing import Any, Callable

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
}

# API pricing providers (the engine_oracle/parts_providers.py chain was deleted in
# T-RF18 — the C++ sourcing RPC owns pricing; this pane still shows/configures the
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

    Local home after parts_providers.py was deleted (T-RF18): config.json →
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
    for row in rec.get("sourcing") or []:
        if isinstance(row, dict):
            p = row.get("price") or row.get("unit_price") or row.get("price_usd")
            try:
                if p is not None and float(p) > 0:
                    return float(p), str(row.get("currency", "USD"))
            except (TypeError, ValueError):
                continue
    params = rec.get("params") or {}
    for k in ("price_usd", "price", "unit_price"):
        try:
            v = params.get(k)
            if v is not None and float(v) > 0:
                return float(v), "USD"
        except (TypeError, ValueError):
            continue
    return 0.0, "USD"


def _params_summary(params: dict, limit: int = 6) -> str:
    bits: list[str] = []
    for k, v in params.items():
        if isinstance(v, (str, int, float)) and str(v):
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
    for k in ("vendor", "manufacturer", "supplier"):
        v = params.get(k)
        if v:
            return str(v)
    if rec.get("source") == "live":
        return "Vendor API"
    return "Library"


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
    if rec.get("standard"):
        desc = f"{rec['standard']}  {desc}".strip()
    return {
        "id":          rec.get("part_id", ""),
        "name":        rec.get("title", ""),
        "vendor":      _vendor_of(rec),
        "part_number": rec.get("designation") or rec.get("part_id", "").rsplit(":", 1)[-1],
        "description": desc,
        "unit":        "ea",
        "price_usd":   price if currency == "USD" else 0.0,
        "material":    _material_of(rec),
        "domain":      rec.get("domain", ""),
        "standard":    rec.get("standard") or "",
        "source":      rec.get("source", "local") or "local",
        "downloaded":  bool(gref.get("kind") == "import_file"),
        "placeable":   gref.get("kind") is not None,
        "datasheet":   str(params.get("datasheet_url", "") or params.get("datasheet", "")),
        "image_url":   str(params.get("image_url", "") or params.get("thumbnail", "")),
        "license":     rec.get("license", ""),
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

    if category_id:
        # V2 taxonomy routing — unknown ids degrade to an unrouted search.
        from . import taxonomy as _tax
        if _tax.get_node(category_id) is not None:
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

    vendor_query = " ".join([query] + [t for t in query_terms if t]).strip()
    return {"query": query, "category_id": category_id, "domain": domain, "site": site,
            "route_sites": route_sites, "facet_defs": facet_defs,
            "vendor_query": vendor_query}


def _split_notices(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separate real records from login_required/unavailable stubs (no part_id or
    title). Stubs become actionable per-site notices instead of blank rows."""
    notices: list[dict] = []
    seen_notice: set = set()
    clean: list[dict] = []
    for r in records:
        pid = str(r.get("part_id", "") or "")
        title = str(r.get("title", "") or "").strip()
        if pid and title:
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
            return out
        st = fn()
        if isinstance(st, dict):
            for k in ("building", "built", "count", "source", "age_s"):
                if k in st:
                    out[k] = st[k]
    except Exception:  # noqa: BLE001 — status is advisory, never fatal
        return dict(_INDEX_STATUS_FALLBACK)
    return out


def _parts_index_status(params: dict, _emit: Callable) -> dict:
    """Worker method ``parts_index_status`` → parts-index build/freshness state.

    Lets the GUI paint a "building index…" affordance instead of an empty pane while
    the FTS index warms."""
    return {"ok": True, **_index_status()}



def _parts_search(params: dict, _emit: Callable) -> dict:
    """Instant local parts search.

    By default (``live`` unset) this behaves like the legacy call: local FTS plus a
    synchronous browser fan-out. The Parts Library GUI now passes ``live=False`` to
    get ONLY the instant local layer, then streams live vendor hits separately via
    ``parts_search_live_start`` / ``parts_search_live_poll`` so results paint as they
    arrive instead of blocking on the slowest vendor."""
    try:
        from services.parts import search as _search
        p = params or {}
        limit = max(1, min(int(p.get("limit", 50)), 200))
        want_live = bool(p.get("live", True)) and str(p.get("source", "")).lower() != "local"
        rt = _compute_routing(p)

        records = _search(rt["query"], domain=rt["domain"], limit=limit,
                          include_live=want_live)
        if rt["site"]:
            prefix = f"catalog:{rt['site']}::"
            records = [r for r in records if str(r.get("part_id", "")).startswith(prefix)]

        if want_live and rt["category_id"] and rt["route_sites"]:
            # Legacy blocking fan-out (only when the caller opted into live here).
            try:
                cat_records = _search(rt["vendor_query"], domain="catalog", limit=limit,
                                      sites=rt["route_sites"])
            except Exception:  # noqa: BLE001 — live layer optional
                cat_records = []
            prefixes = tuple(f"catalog:{s}::" for s in rt["route_sites"])
            seen_ids = {str(r.get("part_id", "")) for r in records}
            for rec in cat_records:
                pid = str(rec.get("part_id", ""))
                if pid.startswith(prefixes) and pid not in seen_ids:
                    seen_ids.add(pid)
                    records.append(rec)

        records, notices = _split_notices(records)
        items = _filter_items(records, p, rt["facet_defs"])
        has_live = any(i["source"] == "live" for i in items)
        idx = _index_status()
        return {"ok": True, "count": len(items), "has_live": has_live,
                "results": items, "notices": notices,
                "index_building": bool(idx.get("building", False)),
                "index_count": int(idx.get("count", -1))}
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
_LIVE_JOB_CAP = 8  # keep the most-recent N jobs; drop stale tokens
# Six simultaneous CEF navigations regularly miss the native 10 s navigation
# deadline on ordinary supplier pages. Three completed every probe in the live
# vendor sweep and prevents rapid searches from creating an unbounded fetch fanout.
_LIVE_FETCH_CONCURRENCY = 3
_live_fetch_slots = threading.BoundedSemaphore(_LIVE_FETCH_CONCURRENCY)

# ---- live vendor result cache -----------------------------------------------------
#
# A live fan-out is seconds per vendor. Re-running the same query 30s later used to
# re-scrape everything, so the pane felt broken on a back-and-forth browse. This is a
# bounded on-disk cache of the RAW record list each vendor returned, keyed on
# (site, vendor_query, limit) — deliberately NOT on the GUI post-filters, so changing
# price/material/vendor/source filters on the same query is still a cache hit and the
# filters are re-applied fresh on every read.
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
    return hashlib.blake2b(f"{site}\n{query}\n{limit}".encode("utf-8"),
                           digest_size=16).hexdigest()


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
    # The one-site hub returns failure/timeout/login notices in-band. Caching one
    # would turn a transient busy/challenge/HTTP failure into a 15-minute false
    # result. Empty clean searches are cacheable; every non-empty row must be a
    # usable PartRecord.
    if any(not isinstance(r, dict)
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
    from services.parts import search as _search
    # Cache probe first (cheap, off-lock): a hit skips the scrape entirely, so this
    # vendor never flashes "loading" and goes straight to "done".
    cached = _vendor_cache_get(site, rt["vendor_query"], limit)
    with _live_lock:
        if job.get("cancelled"):
            return  # superseded/evicted before this site even started — skip the scrape
        v = job.get("vendors", {}).get(site)
        if v is not None:
            v["status"] = "done" if cached is not None else "loading"
    site_status = "done"
    if cached is not None:
        recs = cached
    else:
        try:
            recs = _search(rt["vendor_query"], domain="catalog", limit=limit, sites=[site])
        except Exception:  # noqa: BLE001 — one vendor failing must not sink the others
            recs = []
            site_status = "error"
        else:
            # Only a clean scrape is cacheable; an exception path must stay uncached.
            _vendor_cache_put(site, rt["vendor_query"], limit, recs)
    with _live_lock:
        if job.get("cancelled"):
            return  # cancelled while the scrape was in flight — don't bother parsing
    prefix = f"catalog:{site}::"
    recs = [r for r in recs if str(r.get("part_id", "")).startswith(prefix)
            or r.get("ok") is False]
    for rec in recs:
        if rec.get("ok") is False and not rec.get("site"):
            rec["site"] = site
    clean, notices = _split_notices(recs)
    if notices:
        site_status = "error"
    items = _filter_items(clean, p, rt["facet_defs"])
    with _live_lock:
        if job.get("cancelled"):
            return
        seen = job["seen_ids"]
        for it in items:
            if it["id"] in seen:
                continue
            seen.add(it["id"])
            job["results"].append(it)
        for nt in notices:
            key = (nt["site"], nt["reason"])
            if key not in job["seen_notices"]:
                job["seen_notices"].add(key)
                job["notices"].append(nt)
        job["pending"] -= 1
        if job["pending"] <= 0:
            job["done"] = True
        v = job.get("vendors", {}).get(site)
        if v is not None:
            v["status"] = site_status


def _run_live_site(site: str, rt: dict, limit: int, p: dict, job: dict) -> None:
    """Run one vendor scrape inside the process-wide CEF concurrency budget."""
    with _live_fetch_slots:
        _run_live_site_inner(site, rt, limit, p, job)


def _parts_search_live_start(params: dict, _emit: Callable) -> dict:
    """Begin a background live vendor fan-out for the current query. Returns a token
    the GUI polls with ``parts_search_live_poll``. No-op (done immediately) when the
    category routes to no live vendors."""
    try:
        import uuid
        p = params or {}
        limit = max(1, min(int(p.get("limit", 50)), 200))
        rt = _compute_routing(p)
        sites = list(rt["route_sites"])
        token = uuid.uuid4().hex
        vendors = {s: {"site": s, "label": _site_label(s), "domain": _site_domain(s),
                       "status": "queued"} for s in sites}
        job = {"results": [], "notices": [], "seen_ids": set(), "seen_notices": set(),
               "pending": len(sites), "done": not sites, "cancelled": False,
               "vendors": vendors}
        with _live_lock:
            # A new search supersedes anything still running. The RPC carries no
            # client/session identity, so cancel EVERY older live job: their workers
            # bail out instead of scraping vendors nobody will look at, and marking
            # them done makes the GUI's next poll drain-and-drop the stale token.
            for old in _live_jobs.values():
                if not old.get("done"):
                    old["cancelled"] = True
                    old["done"] = True
            _live_jobs[token] = job
            # Evict oldest tokens beyond the cap (dicts preserve insertion order).
            while len(_live_jobs) > _LIVE_JOB_CAP:
                evicted = _live_jobs.pop(next(iter(_live_jobs)))
                evicted["cancelled"] = True  # stop its workers, not just the token
        for s in sites:
            threading.Thread(target=_run_live_site, args=(s, rt, limit, p, job),
                             name=f"parts-live-{s}", daemon=True).start()
        return {"ok": True, "token": token, "sites": sites,
                "vendors": list(vendors.values()), "done": job["done"]}
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
        if not part_id.startswith("catalog:"):
            return {"ok": True, "downloaded": True,
                    "note": "local library part — nothing to download; use Insert to place it"}
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


def _parts_queue_status(params: dict, _emit: Callable) -> dict:
    try:
        from services.browser.queue import queue_status
        return queue_status()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_queue_status failed: {exc}"}


def _parts_import_artifact(params: dict, _emit: Callable) -> dict:
    """Import a finished fetch-queue artifact (STEP/IGES) as a new editable doc."""
    try:
        from services.mcp import core_bridge as cb  # _place_import_file ported to core (T-RF19)
        path = str((params or {}).get("artifact_path", "")).strip()
        if not path:
            return {"ok": False, "reason": "artifact_path required"}
        return cb.call("part_import_file", {"path": path})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_import_artifact failed: {exc}"}


def _parts_insert_into_doc(params: dict, _emit: Callable) -> dict:
    try:
        from services.mcp import core_bridge as cb  # place.place ported to core (T-RF19)
        from services.parts import get as _hub_get
        part_id = str((params or {}).get("item_id", "")).strip()
        if not part_id:
            return {"ok": False, "reason": "item_id required"}
        record = _hub_get(part_id)
        if record is None:
            return {"ok": False, "reason": f"unknown part_id: {part_id}"}
        return cb.call("part_place", {"part_id": part_id, "record": record,
                                      "doc": (params or {}).get("doc") or None,
                                      "params": (params or {}).get("params") or None})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_insert_into_doc failed: {exc}"}


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

            t = threading.Thread(target=_run, name=f"vendor-login-{site}", daemon=True)
            _login_threads[site] = t
            t.start()
        return {"ok": True, "site": site, "in_progress": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"vendor_login failed: {exc}"}


def _vendor_creds_set(params: dict, _emit: Callable) -> dict:
    """Persist API-provider credentials to the app config (never logged, never committed)."""
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

            t = threading.Thread(target=_run, name="vendors-health-check", daemon=True)
            _health_thread = t
            t.start()
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

def _thumb_cache_dir():
    from services import paths
    d = paths.ROOT / "parts_thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---- thumbnail tuning knobs / disk cache ---------------------------------------------
#
# Every knob is read PER CALL (never memoised into a module global) so a worker can be
# retuned from the environment without a restart.

_THUMB_MAX_BATCH = 32            # entries honoured by one parts_thumbnails call
_THUMB_DIR_MAX_ENTRIES = 20000   # cache-dir bound before the .miss sweep kicks in
_THUMB_TRIM_EVERY_S = 600.0      # at most one sweep per 10 minutes
_THUMB_MISS_SUFFIX = ".miss"
_THUMB_MISS_REASON = "no preview available (cached)"

# THE RENDER PATH IS NOT THREAD-SAFE. `_render_local_part_png` drives the native core as a
# FOUR-CALL sequence around one doc_id (parts_make → doc_apply_ops → doc_render_png →
# doc_close). In daemon mode (the default) services/mcp/daemon_bridge.py serialises each
# INDIVIDUAL rpc behind DaemonClient._lock — one outstanding request per connection — so
# running renders 8-wide buys nothing and merely interleaves four-call sequences on one
# socket. Worse, in the legacy in-process mode (CHUDWARE_ENGINE_DAEMON=0, and the STICKY
# fallback core_bridge drops into for the rest of the session whenever hostd is
# unreachable) core_bridge.call() invokes `dll.chudware_call` with NO lock at all — the
# `_lock` there guards only the one-time DLL load — so concurrent renders would race OCCT
# inside this process. Renders therefore run strictly one at a time behind this lock;
# network fetches, which touch nothing shared, stay fully parallel.
_THUMB_RENDER_LOCK = threading.Lock()

_thumb_trim_lock = threading.Lock()
_thumb_last_trim = 0.0


def _thumb_env_num(name: str, default: float, lo: float, hi: float) -> float:
    """A positive numeric env knob clamped to [lo, hi]. Unset/garbage/<=0 → default."""
    try:
        val = float(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default
    if val <= 0:
        return default
    return max(lo, min(hi, val))


def _thumb_workers() -> int:
    """Fan-out width for a batch. CHUDWARE_THUMB_WORKERS, default 8, clamped 1..16."""
    return int(_thumb_env_num("CHUDWARE_THUMB_WORKERS", 8.0, 1.0, 16.0))


def _thumb_batch_budget_s() -> float:
    """Wall budget for one parts_thumbnails call. CHUDWARE_THUMB_BATCH_BUDGET_S, default 20 s."""
    return _thumb_env_num("CHUDWARE_THUMB_BATCH_BUDGET_S", 20.0, 0.05, 300.0)


def _thumb_fetch_timeout_s() -> float:
    """urllib timeout for one image GET. CHUDWARE_THUMB_FETCH_TIMEOUT_S, default 4.0 s."""
    return _thumb_env_num("CHUDWARE_THUMB_FETCH_TIMEOUT_S", 4.0, 0.05, 60.0)


def _thumb_miss_ttl_s() -> float:
    """How long a negative marker suppresses a retry. CHUDWARE_THUMB_MISS_TTL_S, default 24 h."""
    return _thumb_env_num("CHUDWARE_THUMB_MISS_TTL_S", 86400.0, 1.0, 365.0 * 86400.0)


def _thumb_cache_paths(part_id: str):
    """(image, miss-marker) paths for a part id. The sha1 key is shared with
    gen_standard_thumbs.py's cache warmer — never change it without wiping the dir."""
    h = hashlib.sha1(part_id.encode()).hexdigest()
    d = _thumb_cache_dir()
    return d / (h + ".img"), d / (h + _THUMB_MISS_SUFFIX)


def _thumb_cached(part_id: str) -> dict | None:
    """The on-disk answer for a part: a positive hit, a fresh negative marker, or None
    for a genuine miss. Two stat()s at worst — this is why the batch handler can resolve
    every cached entry SYNCHRONOUSLY before it even builds a thread pool."""
    try:
        img, miss = _thumb_cache_paths(part_id)
    except Exception:  # noqa: BLE001 — unwritable cache dir → behave as a miss
        return None
    try:
        if img.exists():
            return {"ok": True, "image_b64": base64.b64encode(img.read_bytes()).decode()}
    except Exception:  # noqa: BLE001 — truncated / racing write → re-produce below
        pass
    try:
        if (time.time() - miss.stat().st_mtime) < _thumb_miss_ttl_s():
            return {"ok": False, "reason": _THUMB_MISS_REASON}
        miss.unlink(missing_ok=True)   # expired — let the part be tried again
    except (FileNotFoundError, NotADirectoryError):
        pass
    except Exception:  # noqa: BLE001 — a cache problem must never fail a thumbnail
        pass
    return None


def _thumb_store(img_path, data: bytes) -> None:
    """Publish a fresh image and clear any stale negative marker. Best-effort: a full or
    read-only cache dir costs a re-fetch next time, never this call's result."""
    try:
        img_path.write_bytes(data)
    except Exception:  # noqa: BLE001
        return
    try:
        img_path.with_suffix(_THUMB_MISS_SUFFIX).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _thumb_mark_miss(miss_path) -> None:
    """Touch the zero-byte negative marker. Without it a part with nothing to show
    re-runs its 2-4 s render (or a doomed CDN fetch) on every single worker restart."""
    try:
        miss_path.touch(exist_ok=True)
        os.utime(miss_path, None)
    except Exception:  # noqa: BLE001
        return
    _thumb_trim_cache_dir()


def _thumb_trim_cache_dir() -> None:
    """Bound the cache dir: past _THUMB_DIR_MAX_ENTRIES entries drop the oldest 20% of
    `.miss` markers — never a real image (those are expensive renders), and at most one
    sweep per 10 minutes so a batch of misses cannot turn into a batch of scans."""
    global _thumb_last_trim
    now = time.monotonic()
    with _thumb_trim_lock:
        if _thumb_last_trim and (now - _thumb_last_trim) < _THUMB_TRIM_EVERY_S:
            return
        _thumb_last_trim = now

    def _mtime(p) -> float:
        try:
            return p.stat().st_mtime
        except OSError:   # vanished mid-sweep — sort it to the front, unlink is tolerant
            return 0.0

    try:
        entries = list(_thumb_cache_dir().iterdir())
        if len(entries) <= _THUMB_DIR_MAX_ENTRIES:
            return
        misses = [p for p in entries if p.name.endswith(_THUMB_MISS_SUFFIX)]
        misses.sort(key=_mtime)
        for p in misses[: max(1, len(misses) // 5)]:
            p.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 — a failed sweep must never fail a thumbnail
        pass


_BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")


def _looks_like_image(body: bytes) -> bool:
    """True if these bytes open with a decodable raster magic. Used to salvage an image
    that arrived attached to an HTTP ERROR status — see :func:`_http_image_get`."""
    if not body:
        return False
    if body.startswith(_IMAGE_MAGIC):
        return True
    return body[:4] == b"RIFF" and body[8:12] == b"WEBP"


def _http_image_get(url: str, headers: dict) -> tuple[bytes | None, int]:
    """One bounded HTTP GET for an image → ``(bytes|None, http_status)``; status 0 means
    the request never produced a response (DNS/TCP/TLS/timeout).

    The subtle part is the ERROR path. Google's s2 favicon service answers **HTTP 404 with
    a perfectly valid 726-byte generic-globe PNG in the body** (measured: thk.com,
    3dcontentcentral.com, traceparts.com all 404 + a decodable PNG; mcmaster.com 200).
    ``urlopen`` turns any 4xx/5xx into an HTTPError and the old code dropped the exception
    on the floor, throwing that usable image away — which is precisely why those three
    vendors fell back to a lettered tile. So on an error status we still READ the body
    (same 4 MiB cap) and hand it back when it carries an image magic. A non-image error
    body (an HTML 404 page, an empty response) yields None and the caller decides.
    """
    try:
        req = urllib.request.Request(url, headers=headers)
        # Short timeout ON PURPOSE: the old 8 s burned the GUI's whole patience budget on a
        # dead CDN before the CEF fallback even started, so the card sat empty for 8+ s.
        with urllib.request.urlopen(req, timeout=_thumb_fetch_timeout_s()) as resp:  # noqa: S310
            body = resp.read(4 * 1024 * 1024)
            return (body or None), int(getattr(resp, "status", 200) or 200)
    except urllib.error.HTTPError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        try:
            body = exc.read(4 * 1024 * 1024) or b""
        except Exception:  # noqa: BLE001 — error carried no readable body
            body = b""
        return (body if _looks_like_image(body) else None), code
    except Exception:  # noqa: BLE001 — DNS/TCP/TLS/timeout; the caller may try CEF
        return None, 0


def _gstatic_favicon_url(image_url: str) -> str:
    """The gstatic faviconV2 equivalent of a Google **s2** favicon url, or ``""`` when
    ``image_url`` is not one.

    Measured: for domains s2 has no icon for it 404s with its generic globe, while
    faviconV2 sometimes still serves the vendor's REAL icon at 200 (traceparts.com →
    a 1075-byte JPEG). Worth exactly one extra request before settling for the globe."""
    import urllib.parse

    try:
        pu = urllib.parse.urlparse(image_url)
    except ValueError:
        return ""
    host = (pu.hostname or "").lower()
    if not (host == "google.com" or host.endswith(".google.com")):
        return ""
    if not pu.path.startswith("/s2/favicons"):
        return ""
    q = urllib.parse.parse_qs(pu.query)
    domain = (q.get("domain") or [""])[0].strip()
    if not domain:
        domain = urllib.parse.urlparse((q.get("url") or [""])[0].strip()).hostname or ""
    if not domain:
        return ""
    size = (q.get("sz") or [""])[0].strip() or "64"
    return ("https://t3.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON"
            "&fallback_opts=TYPE,SIZE,URL"
            f"&url=https://{urllib.parse.quote(domain, safe='')}&size={size}")


def _fetch_image_bytes(image_url: str, part_id: str) -> bytes | None:
    """Fetch a product image or vendor favicon, defeating the common hotlink guard by
    sending a browser User-Agent and a same-origin Referer derived from the part's product
    URL (part_id = ``catalog:<site>::<product_url>``). Many vendor CDNs 403 a bare request
    but serve the byte stream once a plausible Referer is present.

    Three ordered attempts, at most two of them HTTP:
      1. ``image_url`` itself. A 200 body wins outright; a 4xx/5xx body wins too **if it
         is an image** (Google's s2 favicons 404 with a valid globe PNG — see
         :func:`_http_image_get`).
      2. For an s2 favicon url that 404'd, ONE retry against gstatic's faviconV2, which
         measurably still has the real icon for some of those domains. A 200 there beats
         the globe; otherwise the globe stands, because any icon beats a lettered tile.
      3. The CEF session (real browser fingerprint + saved cookies) — the thing that
         defeats CDNs which 403 a plain hotlink. Skipped when we already have bytes, and
         skipped on a bodyless 404: that asset is simply gone and CEF would spend seconds
         collecting the same 404.
    """
    import urllib.parse

    referer = ""
    if "::" in part_id:
        prod_url = part_id.split("::", 1)[1]
        if prod_url.startswith(("http://", "https://")):
            referer = prod_url
    if not referer:
        pu = urllib.parse.urlparse(image_url)
        if pu.scheme and pu.netloc:
            referer = f"{pu.scheme}://{pu.netloc}/"

    headers = {"User-Agent": _BROWSER_UA,
               "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8"}
    if referer:
        headers["Referer"] = referer

    data, status = _http_image_get(image_url, headers)
    if data and status == 200:
        return data
    if status == 404:
        alt_url = _gstatic_favicon_url(image_url)
        if alt_url:   # exactly one alternate attempt — never a provider loop
            alt_data, alt_status = _http_image_get(alt_url, headers)
            if alt_data and alt_status == 200:
                return alt_data          # the vendor's REAL icon
            data = data or alt_data      # both generic: keep whichever globe we got
    if data:
        return data   # an image body that came with an error status — still an image
    if status == 404:
        # Genuinely nothing there. CEF would re-request the SAME url from the SAME CDN and
        # collect the same 404 seconds later. Every OTHER failure (403 hotlink guard, 5xx,
        # timeout) still falls through — those are exactly what the CEF session fixes.
        return None
    # Fallback: pull the image through the CEF session, which carries the vendor's
    # real browser fingerprint + saved cookies — this is what defeats the CDNs that
    # 403 a plain hotlink. site = the catalog part_id's vendor segment.
    site = ""
    if part_id.startswith("catalog:"):
        site = part_id[len("catalog:"):].split("::", 1)[0]
    try:
        import tempfile
        from pathlib import Path as _Path
        from services.browser.cef_client import client as _cef_client
        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as tf:
            dest = tf.name
        r = _cef_client().download(image_url, dest, profile=site)
        if isinstance(r, dict) and r.get("ok"):
            data = _Path(dest).read_bytes()
            _Path(dest).unlink(missing_ok=True)
            return data or None
        _Path(dest).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 — CEF unavailable / download failed; GUI shows a glyph
        pass
    return None


def _family_material(family: str) -> tuple[list[float], float, float]:
    """(base color, metallic, roughness) for a realistic look. Most mechanical hardware is
    steel; a few families read better as brass. (The thumbnail rasterizer currently shades on
    base color only — metallic/roughness are stored for when it gains PBR.)"""
    fam = family.lower()
    if "brass" in fam or "bronze" in fam:
        return ([0.80, 0.62, 0.28], 0.9, 0.35)
    return ([0.66, 0.67, 0.69], 0.9, 0.38)   # steel grey


def _fastener_thread_op(family: str, size: str) -> dict | None:
    """A `thread` op for a threaded fastener family, else None. External for screws/bolts/studs/
    rod (threads the shank = the smallest-radius cylinder, since heads are larger Ø), internal for
    nuts (threads the sole bore cylinder). cut_thread degrades to the plain cylinder on any failure,
    so a mis-resolved face never breaks the thumbnail."""
    fam = family.lower()
    external = fam in ("shcs", "bhcs", "fhcs", "hex_bolt", "socket_set_screw", "threaded_rod") \
        or any(k in fam for k in ("screw", "bolt", "stud", "rod"))
    internal = fam == "hex_nut" or "nut" in fam
    if not (external or internal):
        return None
    face: dict = {"of": "face", "surf": "cylindrical"}
    if not internal:
        face["pick"] = "inner"   # smallest-radius cylinder = the shank (heads are larger Ø)
    # add_feature keeps only type/params/refs (normalize_feature flattens both to top level for the
    # thread handler): scalar options under params, the face selector under refs.target_face.
    return {"op": "add_feature", "feature": {
        "type": "thread",
        "params": {"size": size, "mode": "cut",
                   "style": "internal" if internal else "external"},
        "refs": {"target_face": face},
    }}


def _resolve_core_family(part_id: str) -> tuple[str, str] | None:
    """(family, size) for a parametric ``core_family`` part, else None. Fast path parses a
    ``mech_family:<family>:<size>`` id directly; every OTHER local id (``fastener:ISO 4762:M8``,
    ``bearing:…``, …) is resolved via its PartRecord's ``geometry_ref`` — those catalog parts carry
    ``geometry_ref.kind == "core_family"`` with the same {family,size}, so they render identically.
    This is why bolt/nut thumbnails must NOT be gated on the ``mech_family:`` prefix alone."""
    if part_id.startswith("mech_family:"):
        # family keys never contain a colon, so split on the FIRST colon (as CoreFamiliesAdapter).
        sub = part_id[len("mech_family:"):].split(":", 1)
        if len(sub) == 2 and sub[0] and sub[1]:
            return sub[0], sub[1]
        return None
    if part_id.startswith(("catalog:", "vendorlogo:")):
        return None   # live vendor product / a logo — not a local parametric part
    try:
        from services import parts as parts_hub  # noqa: PLC0415
        rec = parts_hub.get(part_id)
    except Exception:  # noqa: BLE001 — unknown / non-local id
        return None
    if not isinstance(rec, dict):
        return None
    gref = rec.get("geometry_ref") or {}
    if gref.get("kind") != "core_family":
        return None
    fam, sz = gref.get("family"), gref.get("size")
    return (str(fam), str(sz)) if fam and sz else None


def _render_local_part_png(part_id: str, tmp_png) -> bytes | None:
    """Render a standard parametric (``core_family``) library part's actual 3D CAD model to a PNG,
    via the native core: ``parts_make`` (instantiate the family+size — the "template part") →
    material + threads → ``doc_render_png`` (clean) → ``doc_close``. Returns the PNG bytes, or None
    if the part isn't a core_family part / the core is unavailable / the render failed (caller falls
    back to the 2D domain glyph). Uses the RAW core RPCs, never the ``make_standard_part`` MCP
    wrapper — that wrapper publishes/autosaves and would clobber the user's live doc."""
    fam_sz = _resolve_core_family(part_id)
    if fam_sz is None:
        return None
    family, size = fam_sz
    try:
        import pathlib as _pathlib  # noqa: PLC0415
        # MUST be the PACKAGE module (`services.mcp.core_bridge`), never a bare
        # `import core_bridge` off a sys.path hack: a bare import gives the module an empty
        # __package__, so core_bridge.call()'s own `from .daemon_bridge import DaemonError`
        # (core_bridge.py:377, taken whenever daemon mode is on — the DEFAULT) raises
        # "attempted relative import with no known parent package". That ImportError was
        # swallowed by the render's broad `except` below and every uncached part fell back
        # to the flat glyph — the "thumbnails only load for some parts" bug. Same defect the
        # placer had (see test_x3_browser_catalog.py's regression guard).
        from services.mcp import core_bridge as cb  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — core bridge unavailable → glyph fallback
        return None
    doc_id = ""
    try:
        r = cb.call("parts_make", {"family": family, "size": size})
        if not isinstance(r, dict) or not r.get("ok"):
            return None
        doc_id = str(r.get("doc_id") or "")
        if not doc_id:
            return None
        # Make the (cached) thumbnail look like the real part: a realistic material, and — for
        # threaded fasteners — real modeled threads via the built-in thread tool. Best-effort and
        # scoped to THIS render doc; placement is unchanged. A failure renders the plain part.
        color, metal, rough = _family_material(family)
        ops: list[dict] = [{"op": "set_appearance", "id": "b1",
                            "color": color, "metallic": metal, "roughness": rough}]
        thread_op = _fastener_thread_op(family, size)
        if thread_op is not None:
            ops.append(thread_op)
        try:
            cb.call("doc_apply_ops", {"doc_id": doc_id, "ops": ops})
        except Exception:  # noqa: BLE001 — material/thread best-effort; render the plain part
            pass
        # annotate=False → clean product shot (no face labels / triad / orientation tint).
        rr = cb.call("doc_render_png",
                     {"doc_id": doc_id, "out_path": str(tmp_png), "annotate": False})
        if not isinstance(rr, dict) or not rr.get("ok"):
            return None
        p = _pathlib.Path(str(tmp_png))
        data = p.read_bytes() if p.exists() else None
        try:
            p.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return data or None
    except Exception:  # noqa: BLE001 — render failed → glyph fallback
        return None
    finally:
        if doc_id:
            try:
                cb.call("doc_close", {"doc_id": doc_id})
            except Exception:  # noqa: BLE001
                pass


def _thumb_produce(part_id: str, image_url: str, deadline: float | None = None) -> dict:
    """The CACHE-MISS half of one preview: vendor image first, else a render of the part's
    own 3D model. Writes the positive cache entry on success and the negative marker on a
    definitive failure. ``deadline`` is a ``time.monotonic()`` stamp bounding how long this
    may block for the serialised render slot; None blocks (the singular RPC's behaviour).

    Callers must have consulted :func:`_thumb_cached` first."""
    img_path, miss_path = _thumb_cache_paths(part_id)
    if image_url.startswith(("http://", "https://")):
        data = _fetch_image_bytes(image_url, part_id)
        if data:
            _thumb_store(img_path, data)
            return {"ok": True, "image_b64": base64.b64encode(data).decode()}
    # No usable vendor image → render the 3D model of any local parametric (core_family)
    # part: mech_family:/fastener:/bearing:/… all resolve through _render_local_part_png.
    # Resolve the family FIRST so that only a genuine render pays for the global render
    # lock — a catalog: id resolves to None in microseconds and must never queue behind
    # someone else's 3 s engine round-trip.
    try:
        renderable = _resolve_core_family(part_id) is not None
    except Exception:  # noqa: BLE001 — unknown id → not a render, just answer below
        renderable = False
    if renderable:
        wait = -1.0 if deadline is None else max(0.0, deadline - time.monotonic())
        if not _THUMB_RENDER_LOCK.acquire(timeout=wait):
            # Out of budget waiting our turn. NOT cached as a miss: this is a scheduling
            # answer, not a statement about the part.
            return {"ok": False, "reason": "timeout"}
        try:
            data = _render_local_part_png(part_id, str(img_path) + ".png")
        finally:
            _THUMB_RENDER_LOCK.release()
    else:
        # Still routed through the renderer: it answers None immediately for a
        # non-core_family id, and ONE call site keeps the two branches honest.
        data = _render_local_part_png(part_id, str(img_path) + ".png")
    if data:
        _thumb_store(img_path, data)
        return {"ok": True, "image_b64": base64.b64encode(data).decode()}
    _thumb_mark_miss(miss_path)
    return {"ok": False, "reason": "no preview available"}


def _thumb_resolve_one(part_id: str, image_url: str, deadline: float | None = None) -> dict:
    """THE single-entry preview implementation, shared by ``parts_thumbnail`` (singular)
    and ``parts_thumbnails`` (batch) so the two can never drift: disk cache — positive
    image or fresh negative marker — and only then the produce path."""
    if not part_id:
        return {"ok": False, "reason": "item_id required"}
    hit = _thumb_cached(part_id)
    if hit is not None:
        return hit
    return _thumb_produce(part_id, image_url, deadline)


def _parts_thumbnail(params: dict, _emit: Callable) -> dict:
    """Best-effort PNG/JPEG preview for a part, base64-encoded, cached on disk.

    Sources: (a) disk cache, (b) a rendered 3D CAD-model screenshot for standard
    ``mech_family`` library parts (parts_make → doc_render_png, cached by part_id),
    (c) a vendor image_url passed by the GUI. The GUI falls back to a domain glyph
    when this returns ok:false.

    Kept as the GUI's fallback for workers that predate ``parts_thumbnails``; it is a thin
    wrapper over :func:`_thumb_resolve_one`, which the batch method also uses.
    """
    try:
        p = params or {}
        return _thumb_resolve_one(str(p.get("item_id", "")).strip(),
                                  str(p.get("image_url", "")).strip())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_thumbnail failed: {exc}"}


def _parts_thumbnails(params: dict, _emit: Callable) -> dict:
    """BATCH previews — ``{"items":[{item_id,image_url},…]}`` → ``{"ok":true, "images":
    [{item_id, ok, image_b64, reason},…]}``, exactly one entry per input, in input order.

    Why it exists: the GUI used to issue one ``parts_thumbnail`` per card down a single
    FIFO dispatch thread, so a grid of thirty uncached products fetched thirty images
    strictly one after another and the page filled in over half a minute. Here the genuine
    misses fan out across a per-call pool, and every disk-cache hit is answered
    synchronously before the pool is even built — a warm grid comes back in milliseconds
    having created no threads at all.

    The envelope stays ok:true whenever the CALL worked; a per-item failure is reported in
    that item's own ok/reason so one dead CDN cannot blank the whole page.
    """
    try:
        raw = (params or {}).get("items") or []
        if not isinstance(raw, list):
            return {"ok": False, "reason": "items must be a list"}
        # Excess is IGNORED rather than rejected: a GUI that over-asks should still get its
        # first screenful instead of an error card on every tile.
        reqs: list[tuple[str, str]] = []
        for ent in raw[:_THUMB_MAX_BATCH]:
            if isinstance(ent, dict):
                reqs.append((str(ent.get("item_id", "")).strip(),
                             str(ent.get("image_url", "")).strip()))
            else:
                reqs.append(("", ""))   # keep the 1:1 index mapping the GUI relies on
        results: list[dict | None] = [None] * len(reqs)

        # Pass 1 — the free answers. A disk hit (image, or a fresh negative marker) costs
        # two stat()s, so a batch that is 90% cached must not spend a millisecond of it
        # waiting behind a pool slot occupied by a slow CDN.
        pending: list[int] = []
        for i, (pid, _url) in enumerate(reqs):
            if not pid:
                results[i] = {"ok": False, "reason": "item_id required"}
                continue
            hit = _thumb_cached(pid)
            if hit is not None:
                results[i] = hit
            else:
                pending.append(i)

        # Pass 2 — the genuine misses, fanned out. The pool is created PER CALL and shut
        # down in the finally: no module-level pool outliving the worker, and no unbounded
        # thread creation (width is clamped 1..16 and never exceeds the miss count).
        if pending:
            budget = _thumb_batch_budget_s()
            deadline = time.monotonic() + budget
            ex = ThreadPoolExecutor(max_workers=max(1, min(_thumb_workers(), len(pending))),
                                    thread_name_prefix="parts-thumb")
            try:
                futs = {ex.submit(_thumb_produce, reqs[i][0], reqs[i][1], deadline): i
                        for i in pending}
                try:
                    for fut in as_completed(futs, timeout=budget):
                        i = futs[fut]
                        try:
                            results[i] = fut.result()
                        except Exception as exc:  # noqa: BLE001 — one bad item, not a bad call
                            results[i] = {"ok": False, "reason": f"thumbnail failed: {exc}"}
                except _FutureTimeout:
                    pass   # anything still None below is reported as reason:"timeout"
            finally:
                # wait=False + cancel_futures: queued work is dropped and anything still
                # running is ABANDONED rather than joined, so this call can never outlast
                # its wall budget. A straggler is harmless — it is itself bounded (a short
                # urllib timeout, then the CEF download), it only writes into the disk
                # cache, and the next request for that id is then a hit.
                ex.shutdown(wait=False, cancel_futures=True)

        images: list[dict] = []
        for i, (pid, _url) in enumerate(reqs):
            r = results[i] or {"ok": False, "reason": "timeout"}
            images.append({"item_id": pid,
                           "ok": bool(r.get("ok")),
                           "image_b64": str(r.get("image_b64") or ""),
                           "reason": str(r.get("reason") or "")})
        return {"ok": True, "images": images}
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
    "parts_queue_status":    _parts_queue_status,
    "parts_insert_into_doc": _parts_insert_into_doc,
    "parts_import_artifact": _parts_import_artifact,
    "vendor_status":         _vendor_status,
    "vendor_login":          _vendor_login,
    "vendor_creds_set":      _vendor_creds_set,
    "parts_thumbnail":       _parts_thumbnail,   # singular: legacy / old-worker fallback
    "parts_thumbnails":      _parts_thumbnails,  # batch: what the GUI grid uses
}
