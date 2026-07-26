"""Unified parts-library MCP tools — aicad.parts/1 (T-P1 + T-P2 + T-P3 + T-X3 + V2).

Parts Library V2 additions (workstream 5): ``parts_taxonomy`` (category browse w/
effective facets), ``part_search`` faceted/taxonomy-routed upgrade, ``part_source``
(deterministic sourcing fan-out — services/parts/source.py),
``vendors_health`` (adapter canary sweep) and ``cam_tool_import`` (cutting-tool
part record → CAM tool library). The dict-returning implementations are
module-level (search_parts_v2, taxonomy_browse, part_source_impl,
vendors_health_impl, cam_tool_import_impl) so the built-in copilot's mission
dispatch reuses them directly.

Exposes these tools over the federated parts hub:

- ``part_search(query, domain?, limit?)`` — FTS5-ranked search across all indexed providers.
  T-P2: domain filter extended to section | fitting | hydraulic | ecad.
  T-X3: domain "catalog" added — GrabCAD/TraceParts results via the user's own
  authenticated browser session (services/browser/adapters, contracts/parts.md §6.3's
  reserved "paid catalog provider" seam, browser-session mechanism instead).
  Live vendor results (ecad_vendor, browser catalog adapters) carry source:"live" in each
  PartRecord and are returned as a fallback when the local index has no hits.
- ``part_get(part_id)`` — retrieve a single PartRecord by its stable part_id. For a
  "catalog" part_id this is also what triggers the actual browser download (T-X3);
  search() only returns lightweight not-yet-downloaded stubs.
- ``part_place(part_id, doc?, params?)`` — T-P3: insert a standard part into a doc,
  routing geometry_ref to the correct domain path.  Always returns editable doc-schema
  ops (R1 parity).  Every domain: core_family/generate_model/import_file → new mech part
  doc; symbol_footprint → sch_add_symbol; section_profile → structural element;
  fitting_id → piping fitting; fmu_component → hydraulic component.

The hub is lazy-initialized on the first call; subsequent calls reuse the in-memory FTS index.
No network, no keys, no core dependency for the fastener/bearing adapter (they run on the
bundled clean-room YAML tables). The core-families adapter degrades gracefully when libchudware
is absent. All T-P2 adapters degrade gracefully (empty results, not tracebacks) when their
backing data or credentials are absent — CI-safe. T-X3's browser catalog providers degrade
the SAME way: no saved browser login profile for a site = that provider contributes zero
results, silently — existing part_search/part_place behavior when no profile exists is
unchanged.

Source attribution: each PartRecord's ``source`` field is "live" for live vendor results and
absent (local) for index results. ``part_search`` also reports ``has_live`` in the response
envelope so callers can distinguish mixed result sets.

See contracts/parts.md for the full PartRecord envelope spec.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field
from mcp.server.fastmcp import FastMCP

# Ensure repo root is on sys.path so engine_oracle is importable from the MCP server process.
_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


from mcp_server import result_serving as _rs

def _ok(payload) -> str:
    return json.dumps({"ok": True, **payload} if isinstance(payload, dict) else {"ok": True, "result": payload},
                      ensure_ascii=False)


def _err(reason: str, hint: str = "") -> str:
    return json.dumps({"ok": False, "reason": str(reason), "hint": str(hint)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Parts Library V2 — module-level implementations (dict in / dict out).
#
# The MCP tools below json.dumps these; the built-in copilot's mission
# dispatch layer (services/agent/mission_dispatch.py) imports them directly so
# both surfaces share ONE implementation. Every function degrades gracefully
# ({ok: false, reason, hint}) when a backing module is absent — CI-safe.
# ---------------------------------------------------------------------------

def _parse_json_dict(raw, what: str) -> tuple[dict | None, str | None]:
    """Parse an MCP JSON-string dict param. Returns (dict, None) or (None, error)."""
    if raw is None or raw == "" or raw == {}:
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    try:
        obj = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        return None, (f"{what} is not valid JSON ({exc}). Got: "
                      f"{str(raw)[:80]!r}")
    if not isinstance(obj, dict):
        return None, (f"{what} must be a JSON OBJECT, got {type(obj).__name__} "
                      f"— e.g. '{{\"bore_mm\": 12}}' (or a real dict)")
    return obj, None


# ---------------------------------------------------------------------------
# Input dialect — these tools own the argument shapes models actually write.
#
# services/agent/agent_turn.py::_reconcile_kwargs inspects a tool's signature and
# REFUSES to call it with a kwarg the signature does not name, so a fixed
# signature can never learn a new spelling. Every tool below therefore takes a
# var-keyword parameter (_signature_spec then reports var_kw=True and passes the
# call through untouched) and normalizes in-body. Two rules, no exceptions:
#   * every rewrite is ECHOED back as `interpreted` (house convention — see
#     services/mcp/cad_doc_tools.py::_interpreted_notes) so the model learns the
#     canonical spelling instead of leaning on the alias forever;
#   * genuinely ambiguous input is a teaching error naming the accepted forms,
#     never a silent guess.
#
# The var-kw ANNOTATION matters: FastMCP's func_metadata does not special-case
# VAR_KEYWORD and turns a bare **kwargs into a REQUIRED schema field literally
# named "kwargs", which breaks every external MCP call including the canonical
# one. Annotated[Any, Field(default=None)] makes that field optional; FastMCP
# then hands the body kwargs={"kwargs": None}, which _clean_kwargs drops.
# ---------------------------------------------------------------------------

_VarKw = Annotated[Any, Field(default=None)]

_MAX_INTERPRETED_NOTES = 12


def _interpreted_notes(notes) -> list:
    """De-duplicate and cap the echo list (a wide call must not bury the result)."""
    seen: list = []
    for n in notes or ():
        if n and n not in seen:
            seen.append(n)
    if len(seen) > _MAX_INTERPRETED_NOTES:
        extra = len(seen) - _MAX_INTERPRETED_NOTES
        seen = seen[:_MAX_INTERPRETED_NOTES] + [f"(+{extra} more of the same kind)"]
    return seen


def _clean_kwargs(kwargs) -> dict:
    """Drop FastMCP's synthetic ``kwargs`` field and explicit-unset (None) args."""
    return {k: v for k, v in (kwargs or {}).items()
            if k != "kwargs" and v is not None}


def _norm_key(name) -> str:
    return str(name).lower().replace("_", "").replace("-", "").replace(" ", "")


def _affix_target(norm: str, free) -> str | None:
    """The single free parameter whose name is an affix of *norm* (or vice versa).

    Mirrors agent_turn._map_arg_name on purpose: giving a tool **kwargs switches
    that reconciler OFF for it, so the affix tolerance it used to provide
    (``search_query`` -> ``query``, ``price`` -> ``price_max``) has to be
    re-provided here or widening a tool would REMOVE tolerance."""
    hits = []
    for p in free:
        np = _norm_key(p)
        if min(len(np), len(norm)) < 3:
            continue  # 1-2 char names collide by accident, never by intent
        if (norm.startswith(np) or norm.endswith(np)
                or np.startswith(norm) or np.endswith(norm)):
            hits.append(p)
    return hits[0] if len(hits) == 1 else None


def _absorb(kwargs, params, aliases: dict, notes: list,
            used=()) -> tuple[dict, dict]:
    """Split caller kwargs into ``({real parameter: value}, leftover)``.

    Every rewrite is appended to *notes*. ``used`` names parameters the caller
    already filled explicitly, so an alias never silently overwrites them."""
    params = tuple(params)
    taken = set(used)
    mapped: dict = {}
    leftover: dict = {}
    for name, value in _clean_kwargs(kwargs).items():
        norm = _norm_key(name)
        if name in params:
            target = name
        elif norm in aliases:
            target = aliases[norm]
        else:
            target = _affix_target(norm, [p for p in params if p not in taken])
        if target is None or target in taken:
            leftover[name] = value
            continue
        mapped[target] = value
        taken.add(target)
        if target != name:
            notes.append(f"interpreted {name}=... as {target}=...")
    return mapped, leftover


def _unknown_args_error(tool: str, leftover: dict, params, examples: str) -> dict:
    """Teaching error for kwargs a tool genuinely has no place for."""
    return {"ok": False,
            "reason": (f"{tool}() got argument(s) "
                       + ", ".join(repr(k) for k in sorted(leftover))
                       + " that it has no place for — the call was NOT made."),
            "hint": f"{tool} accepts: " + ", ".join(params) + ". " + examples}


def _part_id_hint(part_id: str = "") -> str:
    """Teach the id VOCABULARY — 'check it with part_get' teaches nothing."""
    return (("" if not part_id else f"{part_id!r} matched no record. ")
            + "part ids are '<domain>:<catalog_id>' — 'fastener:ISO4762:M8', "
              "'bearing:ISO 15:6205', 'mech_family:shcs:M5', 'ecad:Device:R', "
              "'section:AISC:W8x31'. Discover one with part_search(query='...') "
              "— or describe the part: part_search(part_type='bushing', "
              "bore_mm=20, fit='sliding') — or part_source(need='...'). "
              "Never invent an id.")


def _place_id_hint(part_id: str = "") -> str:
    return (_part_id_hint(part_id)
            + f" If {part_id!r} is a body or a document you already modelled, "
              "part_place is the WRONG tool: build or edit geometry with "
              "add_features(doc_id=..., ops=[...]), or insert a saved part file "
              "into an assembly with asm_add_component(path=...).")


_PLACE_PARAMS_HINT = (
    'params takes a real dict OR a JSON object string. Examples: '
    'params={"length_mm": 25} · params=\'{"pos": {"x": 10, "y": 20}, "ref": "R1"}\'. '
    'Flat placement keywords work too and are folded into params for you: '
    'part_place(part_id="ecad:Device:R", location={"x": 10, "y": 20}).')

_PLACE_DOC_HINT = (
    'doc is the DOCUMENT ITSELF (a dict, or json.dumps(doc)) — never a doc id. '
    'Mechanical parts need no doc at all: part_place("fastener:ISO4762:M8") '
    'creates the document. ECAD/structural/piping/hydraulic placements need the '
    'open doc: part_place("ecad:Device:R", doc=sch_doc, '
    'params={"pos": {"x": 10, "y": 20}}).')

# geometry_ref kinds that CREATE their own document (core/parts/place.cpp::route).
_MECH_KINDS = frozenset({"core_family", "generate_model", "import_file"})


# Descriptive-kwarg keys whose NAME adds nothing to a free-text search — the
# value alone is the head noun (part_type="bushing" -> "bushing").
_HEAD_NOUN_KEYS = frozenset({
    "parttype", "type", "kind", "category", "class", "component", "item",
    "part", "product", "family", "style", "shape", "object",
})
# Trailing key tokens that are a UNIT, not a word: bore_mm=20 -> "bore 20mm".
_UNIT_TOKENS = frozenset({"mm", "cm", "m", "in", "inch", "deg", "kg", "g",
                          "psi", "bar", "nm", "mpa", "rpm"})


def _term_value(value) -> str:
    """One search-text token (or phrase) for a descriptive kwarg value."""
    if isinstance(value, bool):
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(t for t in (_term_value(v) for v in value) if t)
    if isinstance(value, dict):
        lo, hi = value.get("min"), value.get("max")
        if lo is not None or hi is not None:
            return f"{_term_value(lo)}-{_term_value(hi)}".strip("-")
        return " ".join(f"{k} {_term_value(v)}".strip() for k, v in value.items())
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _descriptive_terms(leftover: dict) -> str:
    """Fold engineering-attribute kwargs into free-text search terms.

    The model is DESCRIBING the part it needs rather than naming it, and the
    parts index is FTS5 over titles/designations, so the attributes belong in
    the query text. They are deliberately NOT turned into ``facets``: a facet
    query whose key is absent from a record's params REJECTS that record
    (services/parts/taxonomy.py::match_facets), so guessing facet keys out of
    free-form kwargs would silently return zero hits."""
    head: list[str] = []
    rest: list[str] = []
    for name, value in leftover.items():
        words = [w for w in str(name).replace("-", "_").split("_") if w]
        unit = ""
        if len(words) > 1 and words[-1].lower() in _UNIT_TOKENS:
            unit = words[-1].lower()
            words = words[:-1]
        if isinstance(value, bool):
            if value:
                rest.append(" ".join(words))
            continue
        text = _term_value(value)
        if not text:
            continue
        if unit:
            text = f"{text}{unit}"
        if _norm_key(name) in _HEAD_NOUN_KEYS:
            head.append(text)
        else:
            rest.append(" ".join(words + [text]))
    return " ".join(t for t in head + rest if t).strip()


_SEARCH_PARAMS = ("query", "domain", "limit", "category_id", "facets",
                  "price_max", "vendors", "in_stock", "detail")

_SEARCH_ALIASES = {
    "description": "query", "text": "query", "q": "query", "search": "query",
    "name": "query", "keywords": "query", "keyword": "query", "need": "query",
    "terms": "query", "searchtext": "query", "searchstring": "query",
    "prompt": "query", "title": "query",
    "category": "category_id", "categoryid": "category_id",
    "node": "category_id", "nodeid": "category_id", "taxonomy": "category_id",
    "maxresults": "limit", "max": "limit", "n": "limit", "topk": "limit",
    "count": "limit", "numresults": "limit",
    "maxprice": "price_max", "budget": "price_max", "pricelimit": "price_max",
    "vendor": "vendors", "supplier": "vendors", "suppliers": "vendors",
    "site": "vendors", "sites": "vendors",
    "filters": "facets", "facet": "facets",
    "instock": "in_stock", "available": "in_stock",
    "detaillevel": "detail", "verbosity": "detail",
}

_TAXONOMY_ALIASES = {
    "categoryid": "node_id", "category": "node_id", "id": "node_id",
    "node": "node_id", "path": "node_id", "taxonomy": "node_id",
    "levels": "depth", "maxdepth": "depth",
    "facets": "include_facets", "withfacets": "include_facets",
}

_SOURCE_ALIASES = {
    "query": "need", "description": "need", "text": "need", "q": "need",
    "search": "need", "requirement": "need", "requirements": "need",
    "want": "need", "prompt": "need", "name": "need", "part": "need",
    "spec": "constraints", "specs": "constraints", "criteria": "constraints",
    "budget": "constraints", "notes": "constraints",
    "maxresults": "limit", "max": "limit", "n": "limit", "count": "limit",
    "topk": "limit",
}

_MATCH_ALIASES = {
    "id": "part_id", "part": "part_id", "record": "part_id",
    "name": "title", "description": "title", "product": "title",
    "query": "title", "text": "title",
    "partnumber": "mpn", "mfrpartnumber": "mpn", "pn": "mpn", "sku": "mpn",
    "manufacturerpartnumber": "mpn",
    "manufacturer": "brand", "maker": "brand", "make": "brand",
    "maxresults": "limit", "n": "limit", "topk": "limit", "count": "limit",
}

_REVENG_ALIASES = {
    "id": "part_id", "part": "part_id",
    "link": "url", "href": "url", "page": "url", "producturl": "url",
    "sourceurl": "url", "pageurl": "url",
    "name": "title", "description": "title", "product": "title",
    "partnumber": "mpn", "pn": "mpn", "sku": "mpn",
    "manufacturer": "brand", "maker": "brand", "make": "brand",
}

_HEALTH_ALIASES = {
    "site": "sites", "vendor": "sites", "vendors": "sites", "keys": "sites",
    "adapter": "sites", "adapters": "sites", "source": "sites",
    "sources": "sites",
}

_CAMTOOL_ALIASES = {
    "id": "part_id", "part": "part_id", "tool": "part_id", "toolid": "part_id",
    "toolnumber": "number", "num": "number", "slot": "number",
    "toolmaterial": "material", "mat": "material",
}

_PARTGET_ALIASES = {
    "id": "part_id", "part": "part_id", "partnumber": "part_id",
    "record": "part_id", "partrecord": "part_id", "identifier": "part_id",
}

_LOGIN_ALIASES = {
    "vendor": "site", "source": "site", "adapter": "site", "key": "site",
    "sitekey": "site", "name": "site", "domain": "site",
}

_SCRAPE_ALIASES = {
    "vendor": "site", "source": "site", "adapter": "site", "sitekey": "site",
    "search": "query", "text": "query", "q": "query", "description": "query",
    "keywords": "query", "terms": "query", "part": "query", "need": "query",
}

_IMPORT_ALIASES = {
    "path": "artifact_path", "file": "artifact_path",
    "filepath": "artifact_path", "filename": "artifact_path",
    "source": "artifact_path", "artifact": "artifact_path",
    "localpath": "artifact_path", "step": "artifact_path",
}

_PLACE_ALIASES = {
    "id": "part_id", "part": "part_id", "partnumber": "part_id",
    "record": "part_id", "partrecord": "part_id", "component": "part_id",
    "docid": "doc", "document": "doc", "targetdoc": "doc", "sheet": "doc",
    "schematic": "doc", "board": "doc", "into": "doc",
    "parameters": "params", "options": "params", "overrides": "params",
    "placement": "params", "settings": "params", "props": "params",
    "properties": "params", "config": "params",
}

# Flat placement keywords -> the canonical key inside ``params``.
_PLACE_PARAM_KEYS = {
    "location": "pos", "position": "pos", "at": "pos", "origin": "pos",
    "coordinates": "pos", "xy": "pos", "xyz": "pos", "point": "pos",
    "orientation": "rotation", "rotation": "rotation", "angle": "rotation",
    "rotate": "rotation",
    "size": "size", "dimensions": "size", "extents": "size",
}

def taxonomy_browse(node_id: str = "", depth: int = 2,
                    include_facets: bool = True) -> dict:
    """Browse the parts category taxonomy: one node + its children + effective facets."""
    try:
        from services.parts import taxonomy as _tax
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"taxonomy unavailable: {exc}",
                "hint": "services/parts/taxonomy.py must be importable."}
    try:
        depth = max(1, min(int(depth or 1), 6))
        doc = _tax.load_taxonomy()
        node_id = str(node_id or "").strip()

        def _child_entry(n: dict, level: int) -> dict:
            entry: dict = {"id": n["id"], "label": n.get("label", n["id"]),
                           "has_children": bool(n.get("children"))}
            if level < depth and n.get("children"):
                entry["children"] = [_child_entry(c, level + 1)
                                     for c in n["children"]]
            return entry

        if not node_id:
            roots = doc.get("categories", [])
            return {"ok": True,
                    "node": {"id": "", "label": "Parts Library", "path": []},
                    "children": [_child_entry(c, 1) for c in roots],
                    "facets": [], "breadcrumbs": []}

        node = _tax.get_node(node_id)
        if node is None:
            return {"ok": False, "reason": f"unknown category_id: {node_id!r}",
                    "hint": "Call parts_taxonomy() with no node_id to list roots."}
        crumbs: list[dict] = []
        cur = node_id
        chain: list[dict] = []
        while cur:
            n = _tax.get_node(cur)
            if n is None:
                break
            chain.append({"id": n["id"], "label": n.get("label", n["id"])})
            parent = _tax.parent_of(cur)
            cur = parent["id"] if parent else ""
        crumbs = list(reversed(chain))
        out: dict = {
            "ok": True,
            "node": {"id": node["id"], "label": node.get("label", node["id"]),
                     "description": node.get("description", ""),
                     "path": [c["id"] for c in crumbs]},
            "children": [_child_entry(c, 1) for c in node.get("children") or []],
            "breadcrumbs": crumbs,
        }
        out["facets"] = _tax.effective_facets(node_id) if include_facets else []
        return out
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"parts_taxonomy failed: {exc}", "hint": ""}


def search_parts_v2(query: str = "", domain: str = "", limit: int = 20,
                    category_id: str = "", facets=None, price_max: float = 0.0,
                    vendors: str = "", in_stock: bool = False,
                    detail: str = "summary", **kwargs) -> dict:
    """V2 faceted, taxonomy-routed part search. Backward-compatible envelope
    (ok/count/has_live/results) plus facet_summary + applied.

    Default ``detail='summary'`` projects each hit to
    ``{part_id,title,domain,vendor,price,designation,score}``; full PartRecords
    stay behind ``part_get`` / ``detail='full'``.

    INPUT DIALECT (every rewrite comes back as ``interpreted``):
      * ``description`` / ``text`` / ``q`` / ``search`` / ``name`` / ``keywords``
        are read as ``query``;
      * ANY other descriptive keyword is folded into the query as search TEXT —
        ``search_parts_v2(part_type="bushing", fit="sliding", bore_mm=20)``
        becomes ``query="bushing fit sliding bore 20mm"``. The synthesized query
        is echoed in ``interpreted`` and in ``hint``.
    """
    notes: list[str] = []
    supplied = {"query": query, "domain": domain, "limit": limit,
                "category_id": category_id, "facets": facets,
                "price_max": price_max, "vendors": vendors,
                "in_stock": in_stock, "detail": detail}
    used = [k for k, v in supplied.items()
            if k not in ("limit", "detail") and v not in (None, "", 0, 0.0, False)]
    mapped, leftover = _absorb(kwargs, _SEARCH_PARAMS, _SEARCH_ALIASES, notes, used)
    supplied.update(mapped)
    query = "" if supplied["query"] is None else str(supplied["query"]).strip()
    domain, category_id = supplied["domain"], supplied["category_id"]
    facets, vendors, detail = supplied["facets"], supplied["vendors"], supplied["detail"]
    limit, price_max = supplied["limit"], supplied["price_max"]
    in_stock = supplied["in_stock"]

    if leftover:
        terms = _descriptive_terms(leftover)
        keys = ", ".join(sorted(leftover))
        if terms:
            query = f"{query} {terms}".strip()
            notes.append(
                f"folded descriptive argument(s) {keys} into the search text: "
                f"query={query!r}. part_search takes ONE free-text query — "
                f"attributes are matched as TEXT, not as filters. For exact "
                f"filtering, find the category with parts_taxonomy() and pass "
                f"category_id=... plus facets={{...}} keyed by ITS facet keys.")
        else:
            notes.append(f"ignored empty argument(s) {keys}")
    if not query and not str(category_id or "").strip() \
            and not str(domain or "").strip() and not facets:
        return {"ok": False,
                "reason": "part_search has nothing to search for — no query, no "
                          "descriptive arguments, no category_id, no domain.",
                "hint": "Pass free text: part_search(query='M8 socket head cap "
                        "screw'). Or DESCRIBE the part and the attributes are "
                        "folded into the query for you: "
                        "part_search(part_type='bushing', fit='sliding', "
                        "bore_mm=20). Browse categories with parts_taxonomy().",
                "interpreted": _interpreted_notes(notes)}

    facets_q, err = _parse_json_dict(facets, "facets")
    if err:
        return {"ok": False, "reason": err,
                "hint": 'Pass facets as a JSON dict string \'{"od_mm": 25.4}\' or '
                        'a real dict {"od_mm": 25.4}. The keys must be the facet '
                        'keys parts_taxonomy(node_id) lists for your category_id '
                        '— a key no record carries matches nothing.',
                "interpreted": _interpreted_notes(notes)}
    try:
        from services.parts import search as _search
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"part_search failed: {exc}",
                "hint": "Check that services/parts is importable."}
    try:
        limit = max(1, min(int(limit or 20), 200))
        category_id = str(category_id or "").strip()
        dom: str | list[str] | None = (domain or "").strip() or None
        route_sites: list[str] = []
        query_terms: list[str] = []
        facet_defs: list[dict] = []
        if category_id:
            # Taxonomy routing — mirrors services/parts/gui_rpc._parts_search.
            from services.parts import taxonomy as _tax
            if _tax.get_node(category_id) is not None:
                routing = _tax.route(category_id)
                dom = routing["domains"] or dom
                route_sites = routing["sites"]
                query_terms = routing["query_terms"]
                facet_defs = _tax.effective_facets(category_id)

        records = list(_search(query, domain=dom, limit=limit,
                               sites=route_sites or None))

        if category_id and route_sites:
            # Category-routed live catalog merge, restricted to the node's sites.
            vendor_query = " ".join([query] + [t for t in query_terms if t]).strip()
            try:
                # Restrict the live fan-out to the category's fitting vendors so a
                # query never fetches from a vendor that can't stock it (no
                # McMaster for microcontrollers). The prefix filter below stays as
                # a belt-and-braces guard. Inherits the hub's live wall budget.
                cat_records = _search(vendor_query, domain="catalog", limit=limit,
                                      sites=route_sites)
            except Exception:  # noqa: BLE001 — live layer optional
                cat_records = []
            prefixes = tuple(f"catalog:{s}::" for s in route_sites)
            seen_ids = {str(r.get("part_id", "")) for r in records}
            for rec in cat_records:
                pid = str(rec.get("part_id", ""))
                if pid.startswith(prefixes) and pid not in seen_ids:
                    seen_ids.add(pid)
                    records.append(rec)
        # Drop non-record stubs (login_required pass-throughs) into notices.
        notices: list[dict] = []
        clean: list[dict] = []
        seen_notice: set = set()
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
        records = clean

        if facets_q:
            # Facet post-filter with param-key alias tolerance (taxonomy keys like
            # "bore_mm" vs local record params like "bore" — source.facet_params_view).
            from services.parts.source import facet_params_view as _fpv
            from services.parts.taxonomy import match_facets as _mf
            records = [r for r in records
                       if _mf(_fpv(r.get("params") or {}, facets_q.keys()),
                              facets_q, facet_defs)]

        from services.parts.source import _first_price, _vendor_of
        try:
            price_max = float(price_max or 0.0)
        except (TypeError, ValueError):
            price_max = 0.0
        if price_max > 0:
            records = [r for r in records
                       if 0 < _first_price(r) <= price_max]
        wanted_vendors = [v.strip().lower() for v in (vendors or "").split(",")
                          if v.strip()]
        if wanted_vendors:
            records = [r for r in records
                       if any(w in _vendor_of(r).lower() for w in wanted_vendors)]
        if in_stock:
            # Honest stock filter: drop only records with KNOWN zero stock —
            # unknown stock (None / no sourcing rows) passes (contract: None != 0).
            def _known_out_of_stock(rec: dict) -> bool:
                rows = [row for row in (rec.get("sourcing") or [])
                        if isinstance(row, dict) and row.get("stock") is not None]
                return bool(rows) and all(int(row["stock"]) == 0 for row in rows)
            records = [r for r in records if not _known_out_of_stock(r)]

        # facet_summary: top value counts per facet key over THIS result set.
        summary_keys = [f.get("key") for f in facet_defs if f.get("key")] or \
            sorted(facets_q.keys())
        from services.parts.source import facet_params_view as _fpv2
        facet_summary: dict[str, list] = {}
        for key in summary_keys:
            counts: dict[str, int] = {}
            for r in records:
                v = _fpv2(r.get("params") or {}, [key]).get(key)
                if v is None:
                    continue
                counts[str(v)] = counts.get(str(v), 0) + 1
            if counts:
                top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
                facet_summary[key] = [{"value": v, "count": c} for v, c in top]

        payload = {
            "ok": True,
            "count": len(records),
            "has_live": any(r.get("source") == "live" for r in records),
            "results": records,
            "notices": notices,
            "facet_summary": facet_summary,
            "applied": {"query": query, "domain": dom, "category_id": category_id,
                        "facets": facets_q, "price_max": price_max,
                        "vendors": wanted_vendors, "in_stock": bool(in_stock),
                        "sites": route_sites},
        }
        if notes:
            payload["interpreted"] = _interpreted_notes(notes)
            # `hint` is never budget-dropped (result_serving._NEVER_DROP), so the
            # synthesized query reaches the model even on a big result set.
            payload["hint"] = (f"Searched for query={query!r} — refine that string "
                               f"and pass it as query=... next time.")
        opts = _rs.ServeOpts(
            detail=detail or "summary",
            budget_tokens=1500 if (detail or "summary").strip().lower() != "full" else None,
        )
        # Project in-place via projector; return dict (MCP tool serializes).
        return _rs.project_part_search(payload, opts)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"part_search failed: {exc}",
                "hint": "Check that services/parts is importable."}


def vendors_health_impl(sites: str = "") -> dict:
    """Canary health sweep over the browser vendor adapters (CSV site filter)."""
    try:
        from services.browser.health import run_health_check as _run
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"vendor health module unavailable: {exc}",
                "hint": "services/browser/health.py ships with the browser workstream."}
    try:
        site_list = [s.strip().lower() for s in (sites or "").split(",") if s.strip()] or None
        rows = _run(sites=site_list) or []
        out = []
        for row in rows:
            r = dict(row)
            r["needs_human_login"] = bool(
                not r.get("ok") and (
                    r.get("error") in ("login_required", "needs_human_login") or
                    r.get("deferred") or
                    (r.get("expected") and not r.get("logged_in"))))
            out.append(r)
        return {"ok": True, "count": len(out), "sites": out,
                "needs_human_login": sorted(r["site"] for r in out
                                            if r["needs_human_login"])}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"vendors_health failed: {exc}", "hint": ""}


# Cutting-tool material vocabulary the CAM engine understands.
_TOOL_MATERIALS = ("carbide", "hss", "cobalt")


def _guess_tool_type(rec: dict) -> tuple[str, float]:
    """(cam tool type, tip_angle) from a cutting-tool part record."""
    text = " ".join([str(rec.get("part_id", "")), str(rec.get("title", "")),
                     str((rec.get("params") or {}).get("type", ""))]).lower()
    params = rec.get("params") or {}
    if "tap" in text and "taper" not in text:
        return "tap", 0.0
    if "ream" in text:
        return "reamer", 0.0
    if "drill" in text or "jobber" in text or "stub" in text:
        try:
            tip = float(params.get("tip_angle") or params.get("point_angle") or 118.0)
        except (TypeError, ValueError):
            tip = 118.0
        return "drill", tip
    if "ball" in text:
        return "endmill_ball", 0.0
    if "chamfer" in text:
        return "chamfer_mill", 0.0
    if "face mill" in text or "facemill" in text:
        return "face_mill", 0.0
    return "endmill_flat", 0.0


def cam_tool_import_impl(part_id: str, number: int = 0, material: str = "", *,
                         _get_fn=None, _add_tool_fn=None,
                         _existing_numbers_fn=None) -> dict:
    """Import a cutting-tool part record from the parts library into the current
    CAM job's tool library. Test seams: _get_fn/_add_tool_fn/_existing_numbers_fn."""
    part_id = str(part_id or "").strip()
    if not part_id:
        return {"ok": False, "reason": "part_id is required",
                "hint": "Find cutting tools with part_search(category_id="
                        "'tools.cutting_tools.end_mills', ...) or part_source()."}
    if _get_fn is None:
        try:
            from services.parts import get as _get_fn
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"parts hub unavailable: {exc}", "hint": ""}
    try:
        rec = _get_fn(part_id)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"part_get failed: {exc}", "hint": ""}
    if rec is None:
        return {"ok": False, "reason": f"part_id not found: {part_id!r}",
                "hint": "Use part_search/part_source to discover valid part_ids."}

    params = rec.get("params") or {}
    ttype, tip_angle = _guess_tool_type(rec)

    def _num(*keys, default=None):
        for k in keys:
            v = params.get(k)
            if v is None:
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
        return default

    diameter = _num("tool_diameter_mm", "diameter_mm", "diameter", "d", default=6.0)
    flutes = int(_num("flutes", default=2) or 2)
    corner_radius = _num("corner_radius_mm", "corner_radius", default=0.0) or 0.0
    mat = str(params.get("material", "")).strip().lower()
    tool_material = mat if mat in _TOOL_MATERIALS else \
        (str(material or "").strip().lower() or "carbide")
    if tool_material not in _TOOL_MATERIALS:
        tool_material = "carbide"

    if _add_tool_fn is None or _existing_numbers_fn is None:
        # Prefer an ALREADY-LOADED cam_tools module (mcp_server.* or
        # services.mcp.*) so we share its _CAM current-job state; fall back to a
        # same-package import.
        _ct = sys.modules.get("mcp_server.cam_tools") or \
            sys.modules.get("services.mcp.cam_tools")
        if _ct is None:
            try:
                from . import cam_tools as _ct  # noqa: PLC0415
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "reason": f"CAM tools unavailable: {exc}",
                        "hint": "cam_tool_import needs the CAM MCP module."}
        _add_tool_fn = _add_tool_fn or _ct.add_tool_from_dict
        _existing_numbers_fn = _existing_numbers_fn or _ct.existing_tool_numbers

    try:
        number = int(number or 0)
        if number <= 0:
            nums = _existing_numbers_fn()
            number = (max(nums) + 1) if nums else 1
        tool = {
            "number": number, "type": ttype, "diameter": diameter,
            "flutes": flutes, "corner_radius": corner_radius,
            "tool_material": tool_material, "tip_angle": tip_angle,
            "name": str(rec.get("title", "") or f"T{number} {ttype}")[:80],
        }
        result = _add_tool_fn(tool)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"cam_tool_import failed: {exc}",
                "hint": "Create/open a CAM job first (cam_new_job), then retry."}

    from services.parts.source import _first_price, _vendor_of
    machining = {
        "part_id": part_id,
        "vendor": _vendor_of(rec),
        "price": _first_price(rec) or None,
        "coating": params.get("coating"),
        "shank_diameter_mm": _num("shank_diameter_mm"),
        "loc_mm": _num("loc_mm", "length_of_cut_mm"),
        "provenance": rec.get("provenance", ""),
        "source": rec.get("source", "local") or "local",
    }
    out: dict = {
        "ok": True, "tool_number": number, "tool": tool,
        "machining": {k: v for k, v in machining.items() if v not in (None, "")},
        "result": result,
        "note": ("Feeds/speeds do NOT live on the tool record — they resolve from "
                 "the CuttingDB (material x tool x op). Use feeds_speeds(...) / "
                 "cam_check_feeds_envelope(...) or pin rpm/feed per operation."),
    }
    if ttype in ("endmill_flat", "endmill_ball") and diameter:
        out["op_params_suggestion"] = {
            "stepover": round(0.4 * diameter, 3),
            "stepdown": round(0.5 * diameter, 3),
            "basis": "conservative 40%D stepover / 50%D stepdown roughing default; "
                     "verify with feeds_speeds + cam_check_feeds_envelope",
        }
    return out


def part_source_impl(need: str, constraints: str = "", limit: int = 5) -> dict:
    """Deterministic sourcing fan-out (services/parts/source.py)."""
    try:
        from services.parts.source import source as _source
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"sourcing engine unavailable: {exc}",
                "hint": "services/parts/source.py must be importable."}
    try:
        return _source(need, constraints or None, limit)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"part_source failed: {exc}", "hint": ""}


def match_model_impl(part_id: str = "", title: str = "", mpn: str = "",
                     brand: str = "", limit: int = 8) -> dict:
    """Product → CAD-model matcher (aicad.match_model/1, contracts/sourcing.md §2a).

    Builds the product dict from a PartRecord (when ``part_id`` is given) or from
    the plain title/mpn/brand args, then fans out across the CAD-model-yielding
    source adapters and ranks candidates by IDENTITY confidence. Deterministic —
    no LLM inside. Imports lazily at call time so tests can monkeypatch
    ``model_matcher.match_model``.
    """
    part_id = str(part_id or "").strip()
    product: dict
    if part_id:
        try:
            from services.parts import get as _get  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"parts hub unavailable: {exc}",
                    "hint": "services/parts must be importable."}
        try:
            rec = _get(part_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "reason": f"part_get failed: {exc}", "hint": ""}
        if rec is None:
            return {"ok": False, "reason": f"part_id not found: {part_id!r}",
                    "hint": "Use part_search/part_source to discover valid part_ids."}
        product = dict(rec)
    else:
        product = {k: v for k, v in
                   {"title": str(title or "").strip(),
                    "mpn": str(mpn or "").strip(),
                    "brand": str(brand or "").strip()}.items() if v}
    if not product:
        return {"ok": False, "reason": "no product to match",
                "hint": "Pass a part_id, or a title/mpn/brand describing the product."}
    try:
        from services.parts import model_matcher as _mm  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"model matcher unavailable: {exc}",
                "hint": "services/parts/model_matcher.py must be importable."}
    try:
        return _mm.match_model(product, limit=max(1, min(int(limit or 8), 50)))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"match_model failed: {exc}", "hint": ""}


def reverse_engineer_product_impl(part_id: str = "", url: str = "", title: str = "",
                                  mpn: str = "", brand: str = "") -> dict:
    """RE hand-off (contracts/sourcing.md §2b): harvest a product's images +
    documents and seed a reverse-engineering session pre-populated with them.

    Calls the ``reveng_start_from_product`` worker path. Best-effort: zero media
    still stands up a session (the user adds their own photos). Imports lazily at
    call time so tests can monkeypatch ``worker_rpc._reveng_start_from_product``.
    """
    seed = {k: v for k, v in
            {"part_id": str(part_id or "").strip(),
             "url": str(url or "").strip(),
             "title": str(title or "").strip(),
             "mpn": str(mpn or "").strip(),
             "brand": str(brand or "").strip()}.items() if v}
    if not seed.get("part_id") and not seed.get("url"):
        return {"ok": False, "reason": "no reference to reverse-engineer",
                "hint": "Pass a part_id or a url pointing at the product page."}
    try:
        from services.reveng import worker_rpc as _re  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"reverse-engineer suite unavailable: {exc}",
                "hint": "services/reveng/worker_rpc.py must be importable."}
    try:
        return _re._reveng_start_from_product(seed, None)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"reverse_engineer_product failed: {exc}",
                "hint": ""}


def register_parts_tools(mcp: FastMCP) -> None:
    """Register part_search, part_get, and part_place MCP tools (append-only).

    T-X3's browser catalog providers register themselves lazily, inside
    ``services.parts.hub.PartsHub.bootstrap()`` (same timing as every
    other provider, including T-P2's live-vendor adapter) — NOT here, so this
    function keeps the hub's existing "first real call" laziness intact.
    """

    @mcp.tool()
    def part_search(query: str = "", domain: str = "", limit: int = 20,
                    category_id: str = "", facets: str = "",
                    price_max: float = 0.0, vendors: str = "",
                    in_stock: bool = False, detail: str = "summary",
                    **kwargs: _VarKw) -> str:
        """Search the unified standard-parts library (all domains federated), with
        V2 taxonomy routing + faceted filtering.

        Default returns COMPACT rows
        ``{part_id, title, domain, vendor, price, designation, score}`` plus
        ``facet_summary`` / ``applied``. Full PartRecords stay behind
        ``part_get(part_id)`` or ``detail='full'``.

        Ranking: exact designation match first (e.g. "M3"), then standard prefix,
        then FTS score. Cross-domain: "M3" surfaces fasteners AND ecad footprints.

        query       : free-text, e.g. "M8 socket head", "bearing 6205", "W8x31"
        domain      : optional legacy filter — fastener | bearing | mech_family |
                      section | fitting | hydraulic | ecad | catalog
        limit       : max results (default 20)
        category_id : taxonomy category (parts_taxonomy() lists them) — routes to
                      the right domains AND the category's vendor sites, e.g.
                      "structural_stock.metal_tube", "bearings_motion.ball_bearings"
        facets      : JSON dict string of facet filters keyed by the category's
                      facet keys, e.g. '{"od_mm": 25.4, "alloy": "4130"}' or
                      '{"bore_mm": {"min": 10, "max": 14}}' (range|enum|text)
        price_max   : keep only records with a known unit price <= this (0 = off)
        vendors     : CSV vendor filter, e.g. "mcmaster,grainger"
        in_stock    : drop records with KNOWN zero stock (unknown stock passes)
        detail      : 'summary' (default, compact rows) | 'full' (full PartRecords)

        DESCRIBE-THE-PART DIALECT: you do not have to phrase a query yourself.
        `description` / `text` / `q` / `search` / `name` / `keywords` are read as
        `query`, and ANY other keyword is folded into the search text:
          part_search(part_type="bushing", fit="sliding", shaft_diameter=20,
                      bolt_circle="PCD34", bolt_count=4)
            -> query="bushing fit sliding shaft diameter 20 bolt circle PCD34
                      bolt count 4"
        A `<name>_mm` / `_in` / `_deg` suffix becomes a unit on the value
        (bore_mm=20 -> "bore 20mm"). The query that was built is echoed back in
        `interpreted` and `hint` — read it and refine it. Attributes are matched
        as TEXT, not as filters; for exact filtering use category_id + facets.

        Examples:
          part_search("M8 socket head")
          part_search("tube", category_id="structural_stock.metal_tube",
                      facets='{"od_mm": 25.4, "alloy": "4130"}')
          part_search("bearing", category_id="bearings_motion.ball_bearings.deep_groove",
                      facets='{"bore_mm": 12}')
        """
        out = search_parts_v2(query, domain=domain, limit=limit,
                              category_id=category_id, facets=facets,
                              price_max=price_max, vendors=vendors,
                              in_stock=in_stock, detail=detail, **kwargs)
        # Token-budget wrap for the MCP string path (dict path stays projected).
        if out.get("ok"):
            opts = _rs.ServeOpts(
                detail=detail or "summary",
                budget_tokens=1500 if (detail or "summary").strip().lower() != "full" else None,
            )
            return _rs.serve(out, opts)
        return _rs.dumps(out)

    @mcp.tool()
    def parts_taxonomy(node_id: str = "", depth: int = 2,
                       include_facets: bool = True,
                       **kwargs: _VarKw) -> str:
        """Browse the parts-library category taxonomy (Parts Library V2).

        node_id "" lists the root categories (fasteners, bearings_motion,
        structural_stock, drivetrain, tools, electronics, fluid_power, vendors, ...).
        With a node_id, returns that node + children (to `depth`) + breadcrumbs +
        the node's EFFECTIVE facets (inherited root->leaf) so you know exactly
        which facet keys part_search(facets=...) accepts there.

        Workflow: parts_taxonomy() -> pick a category -> parts_taxonomy(node_id)
        for its facets -> part_search(query, category_id, facets).

        Accepts `category_id` / `category` / `node` / `id` as spellings of
        `node_id` (the rewrite is echoed as `interpreted`).
        """
        notes: list[str] = []
        params = ("node_id", "depth", "include_facets")
        mapped, leftover = _absorb(kwargs, params, _TAXONOMY_ALIASES, notes,
                                   ["node_id"] if node_id else [])
        if leftover:
            return json.dumps(_unknown_args_error(
                "parts_taxonomy", leftover, params,
                "Example: parts_taxonomy(node_id='bearings_motion.ball_bearings')."
                " To search by attributes use part_search(part_type=..., "
                "bore_mm=...) instead."), ensure_ascii=False)
        out = taxonomy_browse(mapped.get("node_id", node_id),
                              mapped.get("depth", depth),
                              mapped.get("include_facets", include_facets))
        if notes:
            out["interpreted"] = _interpreted_notes(notes)
        return json.dumps(out, ensure_ascii=False)

    @mcp.tool()
    def part_source(need: str = "", constraints: str = "", limit: int = 5,
                    **kwargs: _VarKw) -> str:
        """Source candidate parts for a plain-language need — deterministic fan-out
        (no LLM inside): taxonomy match -> facet extraction -> routed search ->
        scored candidates with an artifact_plan each.

        need        : what you need, with dimensions inline, e.g.
                      "4130 chromoly tube OD 25.4 wall 1.25", "bearing 12mm bore"
        constraints : extra requirements text (budget, material, vendor prefs)
        limit       : max candidates (default 5)

        Returns {ok, interpreted:{category_id, facets, terms}, candidates:[
        {part_id, title, vendor, price, specs, score, rationale,
         artifact_plan:{how: part_place|scrape_import|make_standard_part, site?,
         query?}}], coverage:{...}} — see contracts/sourcing.md. Follow the
        artifact_plan: part_place(part_id) for library records, scrape_enqueue(
        site, query)+import_artifact for catalog records, make_standard_part for
        standard hardware families.
        """
        notes: list[str] = []
        params = ("need", "constraints", "limit")
        mapped, leftover = _absorb(kwargs, params, _SOURCE_ALIASES, notes,
                                   ["need"] if need else [])
        need = str(mapped.get("need", need) or "")
        constraints = mapped.get("constraints", constraints)
        limit = mapped.get("limit", limit)
        if leftover:
            # Attributes describe the NEED — that is exactly what this tool eats.
            terms = _descriptive_terms(leftover)
            if terms:
                need = f"{need} {terms}".strip()
                notes.append(
                    f"folded descriptive argument(s) "
                    f"{', '.join(sorted(leftover))} into the need text: "
                    f"need={need!r}. part_source takes ONE plain-language need "
                    f"with the dimensions inline.")
        if not need.strip():
            return _err("part_source has nothing to source — `need` is empty.",
                        "Say what you need in plain language, dimensions inline: "
                        "part_source(need='4130 chromoly tube OD 25.4 wall 1.25') "
                        "or part_source(need='flanged bushing', bore_mm=20, "
                        "fit='sliding') — attributes are folded into the need.")
        out = part_source_impl(need, constraints, limit)
        if notes and isinstance(out, dict):
            # `interpreted` is already taken here by the sourcing engine's own
            # {category_id, facets, terms} read-back, so the arg rewrites get
            # their own key rather than clobbering it.
            out["interpreted_args"] = _interpreted_notes(notes)
        return json.dumps(out, ensure_ascii=False)

    @mcp.tool()
    def match_model(part_id: str = "", title: str = "", mpn: str = "",
                    brand: str = "", limit: int = 8,
                    **kwargs: _VarKw) -> str:
        """Match a discrete manufactured PRODUCT (has an MPN/brand identity, no 3D
        model, not standard hardware, not raw stock) to a CAD model in a source
        repo — deterministic identity fan-out (no LLM inside). Fired when a
        part_source candidate's artifact_plan.how == "find_model".

        part_id : a stored PartRecord id (part_get'd internally to read its
                  mpn/brand/title/source_url); OR leave blank and pass:
        title   : product title / description
        mpn     : manufacturer part number
        brand   : brand / manufacturer
        limit   : max candidate models (default 8)

        Returns {ok, product, matches:[{part_id, title, site, mpn, confidence,
        score, attach, artifact_plan}], best, fallback, coverage} — see
        contracts/sourcing.md §2a.

        CONFIDENCE TIERS + AUTO-PLACE POLICY (a wrong model silently corrupts a
        design — the bar is deliberately high):
          exact  — normalized-MPN equality; the ONLY tier with attach:true. Only
                   exact / attach:true matches may be AUTO-PLACED (follow the
                   match's artifact_plan, usually scrape_import → import_artifact).
          strong — brand match + part-number substring; attach:false. Surface to
                   the USER for one-click confirmation; never auto-place.
          weak   — title/spec token overlap only; attach:false. Tentative; must
                   never auto-place — confirm with the user first.

        When NO exact match exists, `best` is the top non-exact candidate for the
        user to confirm, and `fallback` carries a plan. If
        fallback.how == "reverse_engineer" (no confident repo match), call
        reverse_engineer_product(...) to harvest the product's photos + datasheet
        into a seeded RE session. reverse_engineer is OFFERED, never auto-started.

        Accepts `name`/`description`/`product` as `title`, `part_number`/`pn`/
        `sku` as `mpn`, `manufacturer`/`maker` as `brand`, `id`/`part` as
        `part_id` — every rewrite echoed as `interpreted`.
        """
        notes: list[str] = []
        params = ("part_id", "title", "mpn", "brand", "limit")
        given = [k for k, v in (("part_id", part_id), ("title", title),
                                ("mpn", mpn), ("brand", brand)) if v]
        mapped, leftover = _absorb(kwargs, params, _MATCH_ALIASES, notes, given)
        if leftover:
            return json.dumps(_unknown_args_error(
                "match_model", leftover, params,
                "match_model identifies ONE product: match_model(title='...', "
                "mpn='...', brand='...') or match_model(part_id='...'). To look "
                "for standard hardware by attributes use part_search instead."),
                ensure_ascii=False)
        out = match_model_impl(mapped.get("part_id", part_id),
                               mapped.get("title", title),
                               mapped.get("mpn", mpn),
                               mapped.get("brand", brand),
                               mapped.get("limit", limit))
        if notes and isinstance(out, dict):
            out["interpreted"] = _interpreted_notes(notes)
        return json.dumps(out, ensure_ascii=False)

    @mcp.tool()
    def reverse_engineer_product(part_id: str = "", url: str = "", title: str = "",
                                 mpn: str = "", brand: str = "",
                                 **kwargs: _VarKw) -> str:
        """Reverse-engineer hand-off (contracts/sourcing.md §2b): when match_model
        finds NO confident repo match (fallback.how == "reverse_engineer"), harvest
        EVERYTHING scrapable off the product page — all product photos / gallery
        images / og:image AND every datasheet/manual/spec document — and seed a
        reverse-engineering session pre-populated with them. The GUI opens the
        Reverse Engineering tab already loaded (activeWorkspace == "ReverseEng").

        part_id : a stored PartRecord id whose source_url points at the product; OR
        url     : the product page URL directly. (One of part_id / url is required.)
        title / mpn / brand : optional identity seed for the session name/research.

        Returns {ok, session_id, images_added, documents_ingested, source_url,
        activeWorkspace}. Best-effort: zero images/documents STILL starts a session
        (the user adds their own photos) — never a traceback. Reference-only URLs;
        nothing is redistributed.
        """
        notes: list[str] = []
        params = ("part_id", "url", "title", "mpn", "brand")
        given = [k for k, v in (("part_id", part_id), ("url", url),
                                ("title", title), ("mpn", mpn),
                                ("brand", brand)) if v]
        mapped, leftover = _absorb(kwargs, params, _REVENG_ALIASES, notes, given)
        if leftover:
            return json.dumps(_unknown_args_error(
                "reverse_engineer_product", leftover, params,
                "Example: reverse_engineer_product(url='https://.../product/123') "
                "or reverse_engineer_product(part_id='catalog:mcmaster::91290A115')."),
                ensure_ascii=False)
        out = reverse_engineer_product_impl(mapped.get("part_id", part_id),
                                            mapped.get("url", url),
                                            mapped.get("title", title),
                                            mapped.get("mpn", mpn),
                                            mapped.get("brand", brand))
        if notes and isinstance(out, dict):
            out["interpreted"] = _interpreted_notes(notes)
        return json.dumps(out, ensure_ascii=False)

    @mcp.tool()
    def vendors_health(sites: str = "", **kwargs: _VarKw) -> str:
        """Run a canary health check across the vendor source adapters.

        sites: CSV adapter keys to restrict (e.g. "mcmaster,grabcad"); blank = all.
        Returns per-site {site, ok, latency_ms, logged_in, hit_count, error,
        needs_human_login} — needs_human_login=true means the site works but a
        human must run vendor_login(site) first (record a question for the human;
        never fabricate results for a logged-out site).
        """
        notes: list[str] = []
        mapped, leftover = _absorb(kwargs, ("sites",), _HEALTH_ALIASES, notes,
                                   ["sites"] if sites else [])
        if leftover:
            return json.dumps(_unknown_args_error(
                "vendors_health", leftover, ("sites",),
                "sites is a CSV of adapter keys: "
                "vendors_health(sites='mcmaster,grabcad'); blank sweeps all."),
                ensure_ascii=False)
        sites = mapped.get("sites", sites)
        if isinstance(sites, (list, tuple)):
            sites = ",".join(str(s) for s in sites)
            notes.append("interpreted sites=[...] as a CSV string")
        out = vendors_health_impl(sites)
        if notes and isinstance(out, dict):
            out["interpreted"] = _interpreted_notes(notes)
        return json.dumps(out, ensure_ascii=False)

    @mcp.tool()
    def cam_tool_import(part_id: str = "", number: int = 0, material: str = "",
                        **kwargs: _VarKw) -> str:
        """Import a cutting-tool part (end mill / drill / tap from the parts
        library, e.g. category tools.cutting_tools.*) into the current CAM job's
        tool library.

        part_id  : the part record id from part_search/part_source
        number   : tool number; 0 = next free number in the job's library
        material : fallback tool material when the record has none (carbide|hss|cobalt)

        Maps record params -> CAM tool (tool_diameter_mm->diameter, flutes,
        material->tool_material; drills get tip_angle 118 when unstated) and
        returns {ok, tool_number, tool, machining:{vendor data + provenance},
        op_params_suggestion?}. NOTE: feeds/speeds live in the CuttingDB, not the
        tool record — use feeds_speeds()/cam_check_feeds_envelope() for cutting
        params. Requires a current CAM job (cam_new_job first).

        Accepts `id`/`part`/`tool` as `part_id` and `tool_number` as `number`.
        """
        notes: list[str] = []
        params = ("part_id", "number", "material")
        mapped, leftover = _absorb(kwargs, params, _CAMTOOL_ALIASES, notes,
                                   ["part_id"] if part_id else [])
        part_id = str(mapped.get("part_id", part_id) or "").strip()
        if leftover:
            return json.dumps(_unknown_args_error(
                "cam_tool_import", leftover, params,
                "cam_tool_import takes a LIBRARY part id: find the cutter first "
                "with part_search(category_id='tools.cutting_tools.end_mills', "
                "query='6mm 3 flute carbide'), then "
                "cam_tool_import(part_id=<that id>)."), ensure_ascii=False)
        if not part_id:
            return _err("cam_tool_import needs a part_id — none was given.",
                        _part_id_hint())
        out = cam_tool_import_impl(part_id, mapped.get("number", number),
                                   mapped.get("material", material))
        if isinstance(out, dict) and not out.get("ok") and \
                str(out.get("reason", "")).startswith("part_id not found"):
            out["hint"] = _part_id_hint(part_id)
        if notes and isinstance(out, dict):
            out["interpreted"] = _interpreted_notes(notes)
        return json.dumps(out, ensure_ascii=False)

    @mcp.tool()
    def part_get(part_id: str = "", **kwargs: _VarKw) -> str:
        """Retrieve a single PartRecord by its stable part_id (aicad.parts/1).

        part_id is in the form "<domain>:<catalog_id>", e.g.:
          "fastener:ISO4762:M8"    — ISO 4762 M8 socket head cap screw
          "bearing:ISO15:6205"     — ISO 15 6205 deep-groove ball bearing
          "mech_family:shcs:M5"   — core parametric shcs M5

        Returns the PartRecord dict on success, or {"ok": false, "reason": ...} when
        the part_id is unknown.

        Accepts `id` / `part` / `part_number` as spellings of `part_id`.
        """
        notes: list[str] = []
        mapped, leftover = _absorb(kwargs, ("part_id",), _PARTGET_ALIASES, notes,
                                   ["part_id"] if part_id else [])
        part_id = str(mapped.get("part_id", part_id) or "").strip()
        if leftover:
            return json.dumps(_unknown_args_error(
                "part_get", leftover, ("part_id",),
                "part_get fetches ONE known id. To find a part by its attributes "
                "use part_search(part_type='bushing', bore_mm=20, fit='sliding')."),
                ensure_ascii=False)
        if not part_id:
            return _err("part_get needs a part_id — none was given.",
                        _part_id_hint())
        try:
            from services.parts import get as _get
            rec = _get(part_id)
            if rec is None:
                return _err(f"part_id not found: {part_id!r}",
                            _part_id_hint(part_id))
            out = {"ok": True, "part": rec}
            if notes:
                out["interpreted"] = _interpreted_notes(notes)
            return json.dumps(out, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return _err(f"part_get failed: {exc}",
                        "Check that services/parts is importable.")

    @mcp.tool()
    def part_place(part_id: str = "", doc="", params="",
                   **kwargs: _VarKw) -> str:
        """Place a standard part into a document — universal insertion across all domains (T-P3).

        Looks up the PartRecord for ``part_id``, reads its geometry_ref.kind, and routes
        to the correct domain insertion path.  ALWAYS returns an editable document-schema
        op — never an opaque solid body.  Standard-before-custom: prefer part_search +
        part_place over add_features for standard hardware.

        part_id : stable part id, e.g. "fastener:ISO4762:M8", "ecad:Device:R",
                  "bearing:ISO 15:6205", "section:AISC:W8x31", "fitting:elbow:NPS1",
                  "hydraulic:pump:gear-1cc"
        doc     : the document that receives the placement — a real dict OR a JSON
                  string (json.dumps(doc)). NEVER a doc id: the document ITSELF.
                  Leave it out for mechanical parts (core_family / generate_model /
                  import_file), which create their own new document.
        params  : domain-specific placement overrides — a real dict OR a JSON
                  object string:
                  - mech:        {"length_mm": 25}
                  - ecad:        {"pos": {"x": 10, "y": 20}, "value": "1k", "ref": "R1"}
                  - structural:  {"kind": "beam", "length_mm": 3000, "material": "steel"}
                  - piping:      {"type": "elbow", "nps": 1.0}
                  - hydraulic:   {"type": "pump", "displacement_cc_rev": 2.5}
                  You may also pass placement intent as FLAT keywords and they are
                  folded into params for you (echoed as `interpreted`):
                  location/position/at/origin -> params["pos"],
                  orientation/rotation/angle  -> params["rotation"],
                  size/dimensions             -> params["size"];
                  any other keyword goes into params under its own name.

        Returns a PlacementResult dict:
          {ok, domain, doc_id, entity_id, editable: true, detail: {...}}
        or {"ok": false, "reason": ..., "hint": ...} on failure.

        Workflow:
          1. part_search("M8 socket head") → pick part_id "fastener:ISO4762:M8"
          2. part_place("fastener:ISO4762:M8") → creates editable mech part doc_id
          3. For ECAD: part_place("ecad:Device:R", doc=json.dumps(sch_doc), params='{"pos":...}')
        """
        notes: list[str] = []
        place_params = ("part_id", "doc", "params")
        given = [k for k, v in (("part_id", part_id), ("doc", doc),
                                ("params", params)) if v not in (None, "", {})]
        mapped, leftover = _absorb(kwargs, place_params, _PLACE_ALIASES,
                                   notes, given)
        part_id = mapped.get("part_id", part_id)
        doc = mapped.get("doc", doc)
        params = mapped.get("params", params)

        # params: a real dict, a JSON object string, or absent — all accepted.
        params_dict, err = _parse_json_dict(params, "params")
        if err:
            return _err(err, _PLACE_PARAMS_HINT)
        if isinstance(params, dict) and params:
            notes.append("read params as a dict (a JSON string is also accepted)")
        params_dict = dict(params_dict or {})

        # Flat placement intent (location=..., size=...) folds into params.
        for name, value in leftover.items():
            key = _PLACE_PARAM_KEYS.get(_norm_key(name), name)
            if key in params_dict:
                notes.append(f"ignored {name}=...: params[{key!r}] was given "
                             f"explicitly and wins")
                continue
            params_dict[key] = value
            notes.append(f"folded {name}=... into params[{key!r}]")

        # doc: a real dict, a JSON document string, or a bare doc id.
        doc_dict: dict | None = None
        doc_id_only = ""
        if isinstance(doc, dict):
            doc_dict = doc or None
            if doc_dict:
                notes.append("read doc as a document object "
                             "(a JSON string is also accepted)")
        elif isinstance(doc, str) and doc.strip():
            text = doc.strip()
            if text[:1] in ("{", "["):
                try:
                    parsed = json.loads(text)
                except Exception as exc:  # noqa: BLE001
                    return _err(f"doc is not valid JSON ({exc}). Got: "
                                f"{text[:80]!r}", _PLACE_DOC_HINT)
                if not isinstance(parsed, dict):
                    return _err(f"doc must be a JSON OBJECT, got "
                                f"{type(parsed).__name__}.", _PLACE_DOC_HINT)
                doc_dict = parsed
            else:
                doc_id_only = text
        elif doc not in (None, ""):
            return _err(f"doc must be a document object or a JSON string, got "
                        f"{type(doc).__name__}.", _PLACE_DOC_HINT)

        # place.py was ported to core/parts (T-RF19): look the record up via the
        # (Python) hub and hand it to the C++ placement dispatcher.
        from services.parts import get as _hub_get
        from . import core_bridge as cb
        part_id = str(part_id or "").strip()
        if not part_id:
            return _err("part_place needs a part_id — none was given.",
                        _place_id_hint())
        record = _hub_get(part_id)
        if record is None:
            return _err(f"unknown part_id: {part_id!r} — the parts library has no "
                        f"record with that id.", _place_id_hint(part_id))

        if doc_id_only:
            kind = str(((record or {}).get("geometry_ref") or {}).get("kind", ""))
            if kind in _MECH_KINDS:
                notes.append(
                    f"ignored doc={doc_id_only!r}: {part_id!r} is a {kind} part "
                    f"and part_place CREATES its document — `doc` only applies to "
                    f"ecad/structural/piping/hydraulic placements, where it must "
                    f"be the document OBJECT, not its id")
            else:
                return _err(
                    f"doc={doc_id_only!r} looks like a document ID, but part_place "
                    f"needs the document ITSELF ({part_id!r} places into a "
                    f"{kind or 'domain'} document).", _PLACE_DOC_HINT)
        try:
            result = cb.call("part_place", {"part_id": part_id, "record": record,
                                            "doc": doc_dict,
                                            "params": params_dict or None})
            if notes and isinstance(result, dict):
                result = dict(result)
                result["interpreted"] = _interpreted_notes(notes)
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            out = {"ok": False, "reason": f"part_place failed: {exc}",
                   "hint": _place_id_hint(part_id)}
            if notes:
                # The rewrite is now a SUSPECT — say what we read the call as
                # before the model re-sends the same spelling.
                out["interpreted"] = _interpreted_notes(notes)
            return json.dumps(out, ensure_ascii=False)

    # ---- Vendor-source tools (T-X2 / T-X7) ----
    # These expose the services/browser acquisition layer to the GUI's vendor sources pane.

    @mcp.tool()
    def vendor_status(**kwargs: _VarKw) -> str:
        """Return the login status of all registered vendor source adapters.

        Returns a list of source records:
          {key, label, logged_in, last_login, last_used}
        for each adapter registered in services.browser.adapters.

        The GUI uses this to populate the Vendor Sources pane in the Parts Library
        workspace.  No network I/O — reads only local profile metadata sidecar JSON.
        """
        try:
            from services.browser import adapters as _adapters
            from services.browser import profiles as _profiles

            # Friendly display names keyed by adapter key.
            _LABELS: dict[str, str] = {
                "mcmaster":   "McMaster-Carr",
                "grabcad":    "GrabCAD",
                "traceparts": "TraceParts",
                "amazon":     "Amazon Business",
                "jlcpcb":     "JLCPCB",
                "pcbway":     "PCBWay",
                "misumi":     "MISUMI",
                "lcsc":       "LCSC",
                "digikey":    "DigiKey",
                "snapeda":    "SnapEDA",
                "3dcontentcentral": "3D ContentCentral",
            }

            # logged_in must mean a REAL sign-in: anonymous fetches auto-create
            # profile dirs (session.browser_fetch -> profiles.ensure_profile), so
            # has_profile no longer implies login. has_login checks a stamped
            # last_login_ts from an actual browser_login/wizard sign-in.
            _has_login = getattr(_profiles, "has_login", _profiles.has_profile)
            sources = []
            for adapter in _adapters.all_adapters():
                key  = adapter.key
                meta = _profiles.load_meta(key)
                sources.append({
                    "key":        key,
                    "label":      _LABELS.get(key, key.title()),
                    "logged_in":  _has_login(key),
                    "last_login": str(meta.get("last_login_ts", "")),
                    "last_used":  str(meta.get("last_used_ts",  "")),
                })

            return json.dumps({"ok": True, "sources": sources}, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return _err(f"vendor_status failed: {exc}")

    @mcp.tool()
    def vendor_login(site: str = "", **kwargs: _VarKw) -> str:
        """Open a HEADED browser window so the user can log into a vendor site.

        Blocks until the user closes the window (up to 5 minutes).  The saved
        session profile (cookies, local storage) is stored locally under the app
        data directory and is NEVER uploaded or logged.

        site : adapter key, e.g. "mcmaster", "grabcad", "traceparts".
               Use vendor_status() to list valid keys.

        Returns:
          {ok, site, last_login_ts, challenge_detected}
        or {ok: false, reason: ...} on failure.

        NOTE: this call is HEADED (opens a real browser window) and will block the
        worker thread for up to 300 seconds while the user completes the login.
        The GUI displays a "Logging in…" spinner for the duration.
        """
        notes: list[str] = []
        mapped, leftover = _absorb(kwargs, ("site",), _LOGIN_ALIASES, notes,
                                   ["site"] if site else [])
        site = str(mapped.get("site", site) or "").strip().lower()
        if leftover:
            return json.dumps(_unknown_args_error(
                "vendor_login", leftover, ("site",),
                "vendor_login opens ONE vendor's login window: "
                "vendor_login(site='mcmaster'). vendor_status() lists the keys."),
                ensure_ascii=False)
        if not site:
            return _err("vendor_login needs a site — none was given.",
                        "site is an adapter key: 'mcmaster', 'grabcad', "
                        "'traceparts', 'misumi', 'digikey'. vendor_status() "
                        "lists every registered key and its login state.")
        try:
            from services.browser.session import browser_login as _login
            result = _login(site)
            if notes and isinstance(result, dict):
                result = dict(result)
                result["interpreted"] = _interpreted_notes(notes)
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return _err(f"vendor_login failed: {exc}",
                        f"Ensure Playwright is installed: pip install playwright && playwright install chromium")

    # ---- Scrape/download queue tools (T-X4) ----
    # Background queue: search a vendor site + download the first CAD artifact found,
    # running in a daemon thread so the GUI stays responsive.

    # Start the worker thread once at registration time (idempotent).
    try:
        from services.browser.queue import ensure_worker_started as _ensure_worker
        _ensure_worker()
    except Exception:  # noqa: BLE001
        pass  # graceful degradation: queue tools still work, just won't run jobs

    @mcp.tool()
    def scrape_enqueue(site: str = "", query: str = "",
                       **kwargs: _VarKw) -> str:
        """Enqueue a vendor search + CAD-model download in the background queue.

        The job runs asynchronously: the function returns immediately with a job id.
        Use scrape_queue_status() to poll progress.

        site  : adapter key, e.g. "mcmaster", "grabcad".  Must be logged in first
                (vendor_login).
        query : free-text search string, e.g. "6205 bearing", "M8 socket head".

        Returns {ok, id, status} or {ok: false, reason: ...}.

        The job lifecycle:
          pending → running (search + acquire in progress)
          → done (artifact_path set: local file path to the downloaded CAD file)
          → error (error message set; check vendor login status)
        """
        notes: list[str] = []
        given = [k for k, v in (("site", site), ("query", query)) if v]
        mapped, leftover = _absorb(kwargs, ("site", "query"), _SCRAPE_ALIASES,
                                   notes, given)
        site = str(mapped.get("site", site) or "").strip().lower()
        query = str(mapped.get("query", query) or "").strip()
        if leftover:
            terms = _descriptive_terms(leftover)
            if terms:
                query = f"{query} {terms}".strip()
                notes.append(
                    f"folded descriptive argument(s) "
                    f"{', '.join(sorted(leftover))} into the search text: "
                    f"query={query!r}")
        if not site or not query:
            missing = " and ".join(n for n, v in (("site", site), ("query", query))
                                   if not v)
            return _err(f"scrape_enqueue needs {missing}.",
                        "site is a vendor adapter key you are logged into "
                        "(vendor_status() lists them, vendor_login(site) signs "
                        "in); query is free text. Example: "
                        "scrape_enqueue(site='mcmaster', query='6205 bearing').")
        try:
            from services.browser.queue import enqueue as _enqueue
            out = _enqueue(site, query)
            if notes and isinstance(out, dict):
                out = dict(out)
                out["interpreted"] = _interpreted_notes(notes)
            return json.dumps(out, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return _err(f"scrape_enqueue failed: {exc}")

    @mcp.tool()
    def scrape_queue_status(**kwargs: _VarKw) -> str:
        """Return the current state of all scrape/download queue jobs.

        Returns {ok, jobs: [{id, site, query, status, artifact_path, error,
                              created_ts, started_ts, finished_ts, spec_data}]}.

        status values: pending | running | done | error | cancelled.
        artifact_path is set (local filesystem path) when status == "done".
        spec_data is a dict of vendor metadata (title, sku, price, source_url,
        fetched_ts, etc.) extracted from the acquire() provenance envelope; empty
        for non-done jobs.
        """
        try:
            from services.browser.queue import queue_status as _status
            return json.dumps(_status(), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return _err(f"scrape_queue_status failed: {exc}")

    @mcp.tool()
    def import_artifact(artifact_path: str = "", **kwargs: _VarKw) -> str:
        """Import a locally acquired CAD file (STEP/IGES) into a new editable part doc.

        This is the second step after a scrape/download queue job completes with
        status="done": take the artifact_path returned by scrape_queue_status and
        import it into the CAD kernel, creating an editable part document.

        artifact_path : local filesystem path to the acquired CAD file (e.g. a .step
                        file downloaded by the McMaster or GrabCAD adapter). Obtain it
                        from scrape_queue_status's artifact_path field on a done job.

        Returns a PlacementResult dict:
          {ok, domain: "import_file", doc_id, editable: true, detail: {imported_from, ...}}
        or {ok: false, reason: ..., hint: ...} on failure.

        After import you can call reverse_engineer(source=doc_id) for parametric
        feature recovery where the geometry supports it.
        """
        import os
        notes: list[str] = []
        mapped, leftover = _absorb(kwargs, ("artifact_path",), _IMPORT_ALIASES,
                                   notes, ["artifact_path"] if artifact_path else [])
        if leftover:
            return json.dumps(_unknown_args_error(
                "import_artifact", leftover, ("artifact_path",),
                "import_artifact takes ONE local file path — the artifact_path a "
                "done scrape_queue_status job reports."), ensure_ascii=False)
        path = str(mapped.get("artifact_path", artifact_path) or "").strip()
        if not path:
            return _err("import_artifact needs an artifact_path — none was given.",
                        "Run scrape_enqueue(site, query), poll "
                        "scrape_queue_status() until the job is 'done', then pass "
                        "that job's artifact_path here.")
        if not os.path.isfile(path):
            return _err(f"file not found: {path}",
                        "Verify the artifact_path from scrape_queue_status. "
                        "The file may have been cleaned up.")
        try:
            # _place_import_file was ported to core/parts (T-RF19).
            from . import core_bridge as cb
            result = cb.call("part_import_file", {"path": path})
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            return _err(f"import_artifact failed: {exc}",
                        "Ensure the native core (CHUDWARE_LIBCHUDWARE) is available.")
