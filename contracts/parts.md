# Contract: Unified Parts Library — `aicad.parts/1`

> Version 1 (FROZEN). The canonical seam every catalog adapter, search
> index, and insertion tool shares. Append-only per `contracts/README.md`: changes to an
> existing field's meaning require maintainer sign-off + a version bump. New OPTIONAL fields
> may be added additively without a bump.
>
> Producers: `engine_oracle/parts_hub/` (registry + index), adapter modules under
> `engine_oracle/parts_hub/adapters/`. Consumers: `services/mcp/parts_tools.py`
> (`part_search`, `part_get`), the reserved `part_place` tool and vendor adapters (§6).
>
> **Compliance is normative.** No catalog rewrites — adapters WRAP existing surfaces.

## 0. Conventions

- **Domain vocabulary** (`domain` field): the eight reserved strings that identify what
  kind of part a record describes. New domains may be added without a version bump; these
  eight are reserved:

  | domain | meaning | implemented |
  |---|---|---|
  | `fastener` | Metric/imperial threaded fasteners (ISO/DIN) | v1 |
  | `bearing` | Rolling-element bearings (ISO 15 deep-groove, etc.) | v1 |
  | `mech_family` | Core parametric families (`shcs`, `hex_nut`, `washer`, `bearing`, …) | v1 |
  | `section` | Structural sections (AISC, lumber) | SEAM (§6.1) |
  | `fitting` | Piping fittings | SEAM (§6.1) |
  | `hydraulic` | ISO 1219 hydraulic components | SEAM (§6.1) |
  | `ecad` | Electronic components / KiCad symbols | SEAM (§6.1) |
  | `mech_family` | Additional parametric domains beyond core | extensible |
  | `catalog` | Third-party CAD catalog models (GrabCAD, TraceParts) acquired via the user's own authenticated browser session — the §6.3 "paid catalog provider" seam, populated by the browser-session mechanism instead of/alongside a paid API | v1, additive domain |

- **part_id scheme**: `"<domain>:<catalog_id>"` — stable, globally unique, round-trippable
  via `get(part_id)`. Examples: `"fastener:ISO4762:M8"`, `"bearing:ISO15:6205"`,
  `"mech_family:shcs:M5"`. Colons within `catalog_id` are allowed; a `part_id` is split on
  the FIRST colon only.
- **Degradation**: a provider that cannot proceed returns
  `{"ok": false, "reason": <str>, "hint": <str>}` (§0 shape) — never a traceback, never a
  silent success. CI passes with no network and no native core.
- **Provenance required**: every `PartRecord` carries `license` and `provenance` (§1).
  Clean-room lineage per the existing structural-section and ECAD-library precedents.

## 1. PartRecord envelope (frozen)

A `PartRecord` is the unified representation of one standard-part catalog entry. It is
returned by `CatalogProvider.search()` / `.get()` and stored verbatim in the FTS index.

```json
{
  "part_id":     "fastener:ISO4762:M8",
  "domain":      "fastener",
  "title":       "ISO 4762 M8 Socket Head Cap Screw",
  "standard":    "ISO 4762",
  "designation": "M8",
  "params": {
    "thread": "M8",
    "pitch_mm": 1.25,
    "head_d_mm": 13.0,
    "head_h_mm": 8.0
  },
  "geometry_ref": {
    "kind":   "core_family",
    "family": "shcs",
    "size":   "M8"
  },
  "sourcing":    [],
  "provenance":  "ISO 4762 clean-room dimensional data; engine_oracle/data/bolts/",
  "license":     "clean-room"
}
```

| field | type | required | meaning |
|---|---|---|---|
| `part_id` | `str` | yes | Globally unique stable id: `"<domain>:<catalog_id>"` |
| `domain` | `str` | yes | One of the reserved domain strings (§0) |
| `title` | `str` | yes | Human-readable description suitable for search display |
| `standard` | `str \| null` | no | Governing standard, e.g. `"ISO 4762"`, `"AISC360"` |
| `designation` | `str \| null` | no | Size/part designation within the standard, e.g. `"M8"`, `"6205"`, `"W8x31"` |
| `params` | `dict` | yes | Nominal dimensional/electrical parameters (domain-specific keys) |
| `geometry_ref` | `dict \| null` | no | Instantiation recipe (§2) — null when no geometry is available |
| `sourcing` | `list[dict]` | yes | Vendor sourcing refs (aicad.sourcing/1 `VendorPart` dicts, or `[]` if none) |
| `provenance` | `str` | yes | Human-readable citation for the source data |
| `license` | `str` | yes | License keyword: `"clean-room"` \| `"public-domain"` \| `"user-supplied"` |

## 2. geometry_ref (frozen recipe vocabulary)

`geometry_ref` names the instantiation path `part_place` (§6.2) routes to. It is a RECIPE, not
geometry — the record carries no inline mesh or B-rep. Reserved `kind` values:

| kind | meaning | fields |
|---|---|---|
| `core_family` | `parts_make` RPC on the C++ core (core-family adapters emit this) | `family`, `size`, `length_mm?` |
| `generate_model` | `spec_sheet_matcher.generate_model()` (BOLTS-derived) | `match` dict |
| `symbol_footprint` | KiCad symbol + PCB footprint | `library`, `libid`, `footprint` |
| `section_profile` | Structural-section sweep | `standard`, `designation` |
| `fitting_id` | Piping fitting catalog key | `fitting_id` |
| `fmu_component` | FMU/ISO-1219 hydraulic component | `component_id` |
| `import_file` | Local CAD file already on disk (browser-acquired STEP/IGES) — routes through the same `import_file` core RPC `import_part` uses | `path` |

## 3. CatalogProvider protocol (frozen)

Every catalog adapter implements this protocol. v1 ships two adapters; §6.1 adds the
remaining four domain adapters plus live-vendor providers behind the same seam.

```python
class CatalogProvider:
    domain: str          # the domain string this provider covers
    provider_id: str     # short stable id: "bolts_fastener", "core_families", …

    def search(self, query: str, *,
               domain: str | None = None,
               filters: dict | None = None) -> list[dict]:
        """Full-text / fuzzy search. Returns PartRecord dicts, ranked best-first.
        Empty query with domain filter = enumerate all records for that domain.
        Live-vendor providers (§6.1) implement their own network-backed search here."""

    def get(self, part_id: str) -> dict | None:
        """Return the PartRecord for part_id, or None if not found."""

    def count(self) -> int:
        """Return the number of records this provider can enumerate. Used for index build."""

    def iter_records(self) -> Iterator[dict]:
        """Yield every PartRecord for index population. Must be finite and reproducible."""
```

- `search` MUST return an empty list (not raise) when nothing matches.
- `get` MUST return `None` (not raise) for unknown `part_id`.
- `iter_records` is called once at index build time; providers that can't enumerate
  (e.g. live-search-only vendors, §6.1) raise `NotImplementedError` — the index skips them
  and falls back to live search at query time.
- All methods MUST degrade gracefully when their backing data is absent (missing data dir,
  absent core, no credentials) — return empty results, never a traceback.

## 4. Search ranking (frozen)

The FTS hub applies a three-tier ranking before returning results:

1. **Exact designation match** — `designation` exactly equals the query (normalized,
   case-insensitive): score += 1000.
2. **Standard prefix match** — `standard` starts with the query token: score += 100.
3. **FTS BM25 rank** — SQLite FTS5 BM25 score (negative, inverted): score += abs(bm25).

Results are returned sorted descending by total score. The caller may pass `limit`
(default 20) to cap results.

## 5. MCP tools (frozen surface)

`services/mcp/parts_tools.py` registers two tools:

- **`part_search(query, domain?, limit?)`** — full-text search across all indexed providers.
  Returns a list of `PartRecord` dicts, ranked per §4.
- **`part_get(part_id)`** — retrieve a single `PartRecord` by its stable `part_id`.
  Returns `{"ok": false, "reason": ...}` when not found.

## 6. RESERVED seams

### 6.1 All-domain adapters + live vendor search

Adds `CatalogProvider` adapters for `section`, `fitting`, `hydraulic`, `ecad`
domains, plus live-vendor search providers (Nexar, DigiKey, Mouser — already in
`core/sourcing/providers.cpp`). Live providers implement `search()` only
(no `iter_records()`); the hub falls back to live search when the local index has no hits
and a live provider covers the query domain.

### 6.2 `part_place()` + agent wiring

Adds a `part_place(part_id, position?)` MCP tool that routes `geometry_ref` to the
correct instantiation path (C++ `parts_make`, structural sweep, ECAD placement) and returns
the doc_id. It also wires the parts search into the agent context guide
(standard-before-custom bias, cad_guide update).

### 6.3 Paid catalog providers

TraceParts / Werk24 and similar paid catalogs join as `CatalogProvider` adapters
(live-search, no `iter_records`) behind the same seam when volume justifies spend.
**Populated instead by the browser-session mechanism (2026-07-04):**
`services/browser/adapters/parts_provider.py`'s `BrowserCatalogAdapter` wraps GrabCAD/TraceParts as
`domain="catalog"` live-search providers using the user's own authenticated browser
session rather than a paid API. A paid-API adapter would be a different mechanism for an
overlapping catalog; both mechanisms can coexist behind this same seam.

## 7. Parts Library V2 — taxonomy, faceted search, vendor health (additive)

> Added 2026-07-07 (workstream 1, Parts Library V2). Everything in this section is
> ADDITIVE per §0 — no frozen field changes, no version bump. Producers:
> `services/parts/taxonomy.json` + `taxonomy.py` + `gui_rpc.py`.
> Consumers: the Parts Library workspace GUI (`host/src/ui/parts/`) and the
> vendor-adapter workstream (`services/browser/`).

### 7.1 `PartRecord.category_id` (new OPTIONAL field)

| field | type | required | meaning |
|---|---|---|---|
| `category_id` | `str \| null` | no | Dotted taxonomy node id (§7.2) the record belongs to, e.g. `"fasteners.bolts_screws.socket_head"`. Absent on legacy records; adapters may set it when they can classify a hit. |

### 7.2 Taxonomy node schema (`services/parts/taxonomy.json`)

Top-level document: `{"version": 1, "categories": [<node>, ...]}` — 11 root folders.
Node ids are dotted paths (`parent.child`), globally unique.

```json
{
  "id": "fasteners.bolts_screws.socket_head",
  "label": "Socket Head",
  "icon": "cat_fasteners",
  "description": "agent-facing hint",
  "domains": ["fastener"],
  "sites": ["mcmaster", "fastenal"],
  "query_terms": ["socket head cap screw"],
  "facets": [
    {"key": "thread", "label": "Thread", "type": "enum", "values": ["M3", "M8"]},
    {"key": "length_mm", "label": "Length", "type": "range", "unit": "mm"}
  ],
  "children": []
}
```

Inheritance (root→leaf): `domains` / `sites` / `query_terms` EXTEND down the tree
(parent values first, deduplicated); `facets` inherit with a child definition
OVERRIDING its parent by `key`. Facet types:

| type | definition | query shape | matching |
|---|---|---|---|
| `range` | `{key,label,type,unit?}` | `{min?,max?}` (or scalar = exact) | numeric parse with unit-suffix tolerance (`"6.35 mm"` → 6.35); list-valued params match if ANY element is in range |
| `enum` | `{key,label,type,values[]}` | value or list of values | case-insensitive membership |
| `text` | `{key,label,type}` | substring | case-insensitive substring |

A queried facet whose key is missing from a record's `params` is a NON-match; facets
not queried never exclude anything. Missing/corrupt `taxonomy.json` degrades to a
built-in flat fallback tree (the seven legacy §0 domains) — never a traceback.

### 7.3 `parts_taxonomy` worker RPC (new)

`parts_taxonomy({category_id?})` →

```json
{"ok": true, "version": 1, "tree": [
  {"id": "...", "label": "...", "icon": "...", "description": "...",
   "facets": [/* EFFECTIVE (inherited+merged) facet defs */], "children": [/* ... */]}
]}
```

Without `category_id`: `tree` is the list of the 11 root categories. With one:
`tree` is `[that subtree]`. Unknown ids → `{"ok": false, "reason": ...}`.

### 7.4 `parts_search` — new OPTIONAL params (GUI RPC)

- `category_id` (str): taxonomy routing. The node's merged `domains` become the index
  domain filter (list → SQL `IN`); its `sites` restrict `domain="catalog"` records to
  `part_id` prefixes `catalog:<site>::`; its `query_terms` are appended to the vendor
  query for the catalog leg. Unknown `category_id` degrades to an unrouted search
  (ok:true). Sites without a registered adapter simply never match — routing tolerates
  not-yet-existing adapters.
- `facets` (dict): `{facet_key: query}` post-filter applied to record `params` via
  §7.2 matching, using the category's effective facet definitions (no definition →
  text matching).
- The legacy `category` param keeps its exact pre-V2 semantics (fallback when no
  `category_id` is given).

`PartsHub.search()` / `PartsIndex.search()` / `engine_oracle.parts_hub.search()` now
accept `domain: str | list[str] | None` (list → SQL `IN`) — additive signature widening.

### 7.5 `vendors_health_check` / `vendors_health_status` worker RPCs (new)

Same daemon-thread + poll idiom as `vendor_login` / `vendor_status`:

- `vendors_health_check({sites?: [str]})` → `{"ok": true, "started": bool}`
  (`already: true` when a sweep is in flight). Runs one canary search per adapter via
  a lazy import of `services.browser.health` (`run_health_checks(sites=None)`, singular
  `run_health_check` also accepted); if that module is absent →
  `{"ok": false, "error": "health module not available"}`.
- `vendors_health_status({})` →
  `{"ok": true, "running": bool, "results": [...], "started_ts": float, "finished_ts": float}`
  with each result row `{"site", "ok", "latency_ms", "logged_in", "hit_count", "error"}`
  (extra additive keys allowed).

### 7.6 New catalog sites (additive site keys)

Nine site keys join the §6.3 browser-catalog vocabulary (labels in
`gui_rpc._SITE_LABELS`); adapters land separately and absent adapters degrade to
"never matches": `grainger`, `fastenal`, `homedepot`, `lowes`, `acehardware`,
`harborfreight`, `tractorsupply`, `onlinemetals`, `metalsdepot`.
