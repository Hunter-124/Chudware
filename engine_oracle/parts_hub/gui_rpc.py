"""GUI-shaped worker RPCs for the Parts Library workspace (host/src/ui/parts/).

The desktop workspace speaks a flat item/category/vendor vocabulary (name, vendor,
part_number, price_usd, …) rather than the PartRecord envelope, and needs a few
verbs the MCP surface exposes only to agents (vendor login, the fetch queue).
This module adapts :mod:`engine_oracle.parts_hub` + :mod:`services.browser` into
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
import threading
import urllib.request
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

# API pricing providers (engine_oracle/parts_providers.py) and the credential
# fields each needs. Shown in the vendor pane as "API" sources so the user can
# configure them in-app instead of hand-editing config.json.
_API_PROVIDERS: dict[str, dict[str, Any]] = {
    "nexar":   {"label": "Nexar (Octopart)", "fields": ["client_id", "client_secret"],
                "env": {"client_id": "NEXAR_CLIENT_ID", "client_secret": "NEXAR_CLIENT_SECRET"}},
    "digikey_api": {"label": "DigiKey API", "config_key": "digikey",
                    "fields": ["client_id", "client_secret"],
                    "env": {"client_id": "DIGIKEY_CLIENT_ID", "client_secret": "DIGIKEY_CLIENT_SECRET"}},
    "mouser":  {"label": "Mouser API", "fields": ["api_key"],
                "env": {"api_key": "MOUSER_API_KEY"}},
}


def _site_label(key: str, adapter: Any = None) -> str:
    lbl = getattr(adapter, "label", None)
    return lbl or _SITE_LABELS.get(key, key.replace("_", " ").title())


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
        from engine_oracle.parts_hub import taxonomy as _tax
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
        from engine_oracle.parts_hub import taxonomy as _tax
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
        from engine_oracle.parts_hub.taxonomy import match_facets as _match_facets
        items = [i for i, r in zip(items, records)
                 if _match_facets(r.get("params") or {}, facets_q, facet_defs)]
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


def _parts_search(params: dict, _emit: Callable) -> dict:
    """Instant local parts search.

    By default (``live`` unset) this behaves like the legacy call: local FTS plus a
    synchronous browser fan-out. The Parts Library GUI now passes ``live=False`` to
    get ONLY the instant local layer, then streams live vendor hits separately via
    ``parts_search_live_start`` / ``parts_search_live_poll`` so results paint as they
    arrive instead of blocking on the slowest vendor."""
    try:
        from engine_oracle.parts_hub import search as _search
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
        return {"ok": True, "count": len(items), "has_live": has_live,
                "results": items, "notices": notices}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_search failed: {exc}"}


# ---- streaming live vendor search -------------------------------------------------
#
# The live fan-out scrapes each vendor through the CEF session (seconds per site).
# Rather than block the search RPC on the slowest vendor, the GUI kicks off a job
# here that fans out across the category's fitting vendors IN PARALLEL (one thread
# per site — each site has its own profile lock + rate limit, so cross-site calls
# are safe) and polls for results as each vendor lands.

_live_jobs: dict[str, dict] = {}
_live_lock = threading.Lock()
_LIVE_JOB_CAP = 8  # keep the most-recent N jobs; drop stale tokens


def _run_live_site(site: str, rt: dict, limit: int, p: dict, job: dict) -> None:
    from engine_oracle.parts_hub import search as _search
    try:
        recs = _search(rt["vendor_query"], domain="catalog", limit=limit, sites=[site])
    except Exception:  # noqa: BLE001 — one vendor failing must not sink the others
        recs = []
    prefix = f"catalog:{site}::"
    recs = [r for r in recs if str(r.get("part_id", "")).startswith(prefix)
            or r.get("ok") is False]
    clean, notices = _split_notices(recs)
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
        job = {"results": [], "notices": [], "seen_ids": set(), "seen_notices": set(),
               "pending": len(sites), "done": not sites, "cancelled": False}
        with _live_lock:
            _live_jobs[token] = job
            # Evict oldest tokens beyond the cap (dicts preserve insertion order).
            while len(_live_jobs) > _LIVE_JOB_CAP:
                _live_jobs.pop(next(iter(_live_jobs)))
        for s in sites:
            threading.Thread(target=_run_live_site, args=(s, rt, limit, p, job),
                             name=f"parts-live-{s}", daemon=True).start()
        return {"ok": True, "token": token, "sites": sites, "done": job["done"]}
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
            if done:
                _live_jobs.pop(token, None)
        return {"ok": True, "results": new_items, "notices": notices,
                "cursor": new_cursor, "done": done}
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
        from engine_oracle.parts_hub.place import _place_import_file
        path = str((params or {}).get("artifact_path", "")).strip()
        if not path:
            return {"ok": False, "reason": "artifact_path required"}
        return _place_import_file({"kind": "import_file", "path": path}, None,
                                  (params or {}).get("params") or None)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_import_artifact failed: {exc}"}


def _parts_insert_into_doc(params: dict, _emit: Callable) -> dict:
    try:
        from engine_oracle.parts_hub.place import place as _place
        part_id = str((params or {}).get("item_id", "")).strip()
        if not part_id:
            return {"ok": False, "reason": "item_id required"}
        doc = (params or {}).get("doc") or None
        extra = (params or {}).get("params") or None
        return _place(part_id, doc=doc, params=extra)
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
            from engine_oracle.parts_providers import _resolve_creds
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

        from engine import paths
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
    from engine import paths
    d = paths.ROOT / "parts_thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


_BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _fetch_image_bytes(image_url: str, part_id: str) -> bytes | None:
    """Fetch a product image, defeating the common hotlink guard by sending a
    browser User-Agent and a same-origin Referer derived from the part's product
    URL (part_id = ``catalog:<site>::<product_url>``). Many vendor CDNs 403 a bare
    request but serve the byte stream once a plausible Referer is present."""
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
    try:
        req = urllib.request.Request(image_url, headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
            data = resp.read(4 * 1024 * 1024)
            if data:
                return data
    except Exception:  # noqa: BLE001 — hotlink-blocked / transient; fall through to CEF
        pass
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


def _render_local_part_png(part_id: str, tmp_png) -> bytes | None:
    """Render a standard (mech_family) library part's actual 3D CAD model to a PNG,
    via the native core: ``parts_make`` (instantiate the parametric family+size — the
    "template part") → ``doc_render_png`` → ``doc_close``. Returns the PNG bytes, or
    None if the core is unavailable / the render failed (caller falls back to the 2D
    domain glyph). Uses the RAW core RPCs, never the ``make_standard_part`` MCP wrapper
    — that wrapper publishes/autosaves and would clobber the user's live doc."""
    if not part_id.startswith("mech_family:"):
        return None
    # part_id == "mech_family:<family>:<size>"; family keys never contain a colon, so split on
    # the FIRST colon exactly like CoreFamiliesAdapter.get() maps a part_id back to (family, size).
    sub = part_id[len("mech_family:"):].split(":", 1)
    if len(sub) != 2 or not sub[0] or not sub[1]:
        return None
    family, size = sub
    try:
        import pathlib as _pathlib  # noqa: PLC0415
        import sys as _sys  # noqa: PLC0415
        svc = _pathlib.Path(__file__).resolve().parents[2] / "services" / "mcp"
        if str(svc) not in _sys.path:
            _sys.path.insert(0, str(svc))
        import core_bridge as cb  # type: ignore  # noqa: PLC0415
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
        rr = cb.call("doc_render_png", {"doc_id": doc_id, "out_path": str(tmp_png)})
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


def _parts_thumbnail(params: dict, _emit: Callable) -> dict:
    """Best-effort PNG/JPEG preview for a part, base64-encoded, cached on disk.

    Sources: (a) disk cache, (b) a rendered 3D CAD-model screenshot for standard
    ``mech_family`` library parts (parts_make → doc_render_png, cached by part_id),
    (c) a vendor image_url passed by the GUI. The GUI falls back to a domain glyph
    when this returns ok:false.
    """
    try:
        part_id = str((params or {}).get("item_id", "")).strip()
        image_url = str((params or {}).get("image_url", "")).strip()
        if not part_id:
            return {"ok": False, "reason": "item_id required"}
        cache = _thumb_cache_dir() / (hashlib.sha1(part_id.encode()).hexdigest() + ".img")
        if cache.exists():
            return {"ok": True, "image_b64": base64.b64encode(cache.read_bytes()).decode()}
        # Standard/library part with no vendor image: render its 3D model on demand and
        # cache it (keyed by part_id, so subsequent views are instant cache hits).
        if part_id.startswith("mech_family:"):
            data = _render_local_part_png(part_id, str(cache) + ".png")
            if data:
                cache.write_bytes(data)
                return {"ok": True, "image_b64": base64.b64encode(data).decode()}
        if image_url.startswith(("http://", "https://")):
            data = _fetch_image_bytes(image_url, part_id)
            if data:
                cache.write_bytes(data)
                return {"ok": True, "image_b64": base64.b64encode(data).decode()}
        return {"ok": False, "reason": "no preview available"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_thumbnail failed: {exc}"}


def _parts_match_model(params: dict, _emit: Callable) -> dict:
    """Match a product that lacks a 3D model to a CAD-repo model (contracts/sourcing.md
    §2a). Worker-RPC bridge to ``model_matcher.match_model`` for the GUI's "Find 3D
    model" action. ``{part_id?, title?, mpn?, brand?, limit?}`` → the match envelope
    (matches[], best, fallback). Only ``confidence:"exact"`` / ``attach:true`` should
    be placed without asking; strong/weak need user confirmation; a
    ``fallback.how == "reverse_engineer"`` routes to ``reveng_start_from_product``.
    """
    try:
        from engine_oracle.parts_hub.model_matcher import match_model as _match
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
                from engine_oracle.parts_hub import get as _get_record
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
    "parts_thumbnail":       _parts_thumbnail,
}
