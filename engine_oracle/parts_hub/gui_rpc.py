"""GUI-shaped worker RPCs for the Parts Library workspace (host/src/ui/parts/).

The desktop workspace speaks a flat item/category/vendor vocabulary (name, vendor,
part_number, price_usd, …) rather than the PartRecord envelope, and needs a few
verbs the MCP surface exposes only to agents (vendor login, the scrape queue).
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

def _parts_search(params: dict, _emit: Callable) -> dict:
    try:
        from engine_oracle.parts_hub import search as _search
        p = params or {}
        query       = str(p.get("query", "")).strip()
        category    = str(p.get("category", "")).strip()
        category_id = str(p.get("category_id", "")).strip()
        facets_q    = p.get("facets") if isinstance(p.get("facets"), dict) else {}
        limit       = max(1, min(int(p.get("limit", 50)), 200))

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

        records = _search(query, domain=domain, limit=limit)
        if site:
            prefix = f"catalog:{site}::"
            records = [r for r in records if str(r.get("part_id", "")).startswith(prefix)]

        if category_id and route_sites:
            # Category-routed live catalog search: restrict catalog records to the
            # taxonomy's sites (unknown/absent adapters simply never match), with
            # the node's query_terms appended to the vendor query.
            vendor_query = " ".join([query] + [t for t in query_terms if t]).strip()
            try:
                cat_records = _search(vendor_query, domain="catalog", limit=limit)
            except Exception:  # noqa: BLE001 — live layer optional
                cat_records = []
            prefixes = tuple(f"catalog:{s}::" for s in route_sites)
            seen_ids = {str(r.get("part_id", "")) for r in records}
            for rec in cat_records:
                pid = str(rec.get("part_id", ""))
                if pid.startswith(prefixes) and pid not in seen_ids:
                    seen_ids.add(pid)
                    records.append(rec)

        # Drop non-record stubs (e.g. login_required pass-throughs from the live
        # catalog layer) — they have no part_id/title and would render as blank,
        # ID-colliding rows.  Surface them as actionable notices instead.
        notices: list[dict] = []
        _seen_notice: set = set()
        _clean: list[dict] = []
        for r in records:
            pid = str(r.get("part_id", "") or "")
            title = str(r.get("title", "") or "").strip()
            if pid and title:
                _clean.append(r)
                continue
            n_site = str(r.get("site", "") or "")
            if not n_site and pid.startswith("catalog:"):
                n_site = pid[len("catalog:"):].split("::", 1)[0]
            n_reason = str(r.get("reason", "") or "unavailable")
            if (n_site, n_reason) not in _seen_notice:
                _seen_notice.add((n_site, n_reason))
                notices.append({"site": n_site, "reason": n_reason,
                                "hint": str(r.get("hint", "") or "")})
        records = _clean

        items = [_record_to_item(r) for r in records]

        # ---- post-filters (GUI settings) ----
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
            # Facet post-filter (V2): match on the underlying record params using
            # the category's effective facet definitions (empty defs → text match).
            from engine_oracle.parts_hub.taxonomy import match_facets as _match_facets
            pairs = [(i, r) for i, r in zip(items, records)
                     if _match_facets(r.get("params") or {}, facets_q, facet_defs)]
            items = [i for i, _ in pairs]

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

        has_live = any(i["source"] == "live" for i in items)
        return {"ok": True, "count": len(items), "has_live": has_live,
                "results": items, "notices": notices}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_search failed: {exc}"}


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
    """Import a finished scrape-queue artifact (STEP/IGES) as a new editable doc."""
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


def _parts_thumbnail(params: dict, _emit: Callable) -> dict:
    """Best-effort PNG/JPEG preview for a part, base64-encoded, cached on disk.

    v1 sources: (a) disk cache, (b) a vendor image_url passed by the GUI from the
    search item. Parametric-geometry renders for library parts are a follow-up —
    the GUI falls back to a domain glyph when this returns ok:false.
    """
    try:
        part_id = str((params or {}).get("item_id", "")).strip()
        image_url = str((params or {}).get("image_url", "")).strip()
        if not part_id:
            return {"ok": False, "reason": "item_id required"}
        cache = _thumb_cache_dir() / (hashlib.sha1(part_id.encode()).hexdigest() + ".img")
        if cache.exists():
            return {"ok": True, "image_b64": base64.b64encode(cache.read_bytes()).decode()}
        if image_url.startswith(("http://", "https://")):
            req = urllib.request.Request(image_url, headers={"User-Agent": "AI-CAD parts library"})
            with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
                data = resp.read(2 * 1024 * 1024)
            if data:
                cache.write_bytes(data)
                return {"ok": True, "image_b64": base64.b64encode(data).decode()}
        return {"ok": False, "reason": "no preview available"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_thumbnail failed: {exc}"}


GUI_WORKER_METHODS: dict[str, Callable] = {
    "parts_list_categories": _parts_list_categories,
    "parts_taxonomy":        _parts_taxonomy,
    "parts_search":          _parts_search,
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
