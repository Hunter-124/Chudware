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

## 8. Portable rights-aware libraries — `aicad.parts-library/1` (append-only)

`services.parts.parts_library` publishes and ingests portable batches into the existing
parts registry and search index; it is not a second catalog or a deployed storage service.
A publisher may host the resulting files on an optional static HTTPS mirror.

The manifest has `schema`, `library_id`, nonempty string `revision`, and `records`.
Each record has a stable `part_id` of `<domain>:<library_id>:<local_id>`; each
component uses letters, digits, `_`, `.` or `-`. IDs must not collide with bundled or
unrelated records. Preserve `standard`, `designation`, `aliases`, `params`, `sourcing`,
`provenance` and `license` when known. `units` (nonempty string or object),
`geometry_quality` (nonempty description), `references` (array), and `rights` are
required. Each reference has `identifier` and `kind` (`dimensional` or `identifier`);
include edition, URL and locator when available. An identifier-only reference does
not establish dimensions or standard compliance. State geometry fidelity and evidence
honestly; a standard designation alone is not certification.

Example metadata-only record, using the producer's field names:

```json
{
  "schema": "aicad.parts-library/1",
  "library_id": "example",
  "revision": "r1",
  "records": [{
    "part_id": "fastener:example:bolt", "domain": "fastener",
    "title": "Example Bolt", "standard": "ISO 4762", "designation": "M8",
    "aliases": ["specialbolt"], "params": {"length_mm": 20}, "sourcing": [],
    "provenance": "Publisher data", "license": "CC-BY-4.0", "units": "mm",
    "geometry_quality": "nominal",
    "references": [{"identifier": "ISO 4762", "kind": "identifier"}],
    "rights": {"redistribute": true, "license_expression": "CC-BY-4.0",
               "attribution": "Example Publisher"}
  }]
}
```

Metadata-only records are searchable but cannot be placed without geometry. A
record may instead supply a supported portable non-file `geometry_ref` recipe, or
an `artifact` (not both). An artifact has a confined relative `path`, supported
`format`, exact positive `size_bytes`, lowercase `sha256`, separate `rights`, and
optional text `notice`. Artifact rights are independent: metadata permission never
authorizes model redistribution. Portable metadata must not contain absolute local
paths, file URLs or credential-bearing URLs.

`publish_library(manifest_path, artifact_root, output_dir)` validates the manifest,
checks exact artifact bytes and affirmative redistribution rights for both metadata
and artifacts, and emits deterministic `manifest.json`, objects and `notices/NOTICE.txt`
to a separate empty directory. Publication accepts the producer's known license
allowlist and requires nonempty attribution. Do not publish vendor models without
their own redistribution rights. `ingest_manifest` can register a local manifest,
optionally verifying and caching objects from `artifact_root`; `sync_manifest` explicitly
fetches a credential-free HTTPS manifest pinned by SHA-256. Mirrors are optional
credential-free HTTPS bases; ordinary search/get does not sync the library.

A namespace is bound to its approved source identity: the resolved local manifest path
or the sync URL. A different source is refused unless the caller explicitly authorizes
`replace_namespace=True`. This changes the approved source for that namespace; it is
not permission to redistribute content. MCP exposes `part_library_ingest`,
`part_library_sync`, and `part_acquire` (not `part_library_acquire`).
`part_acquire(part_id, offline=True)` verifies cached size and digest without a network
fetch and fails on missing or corrupt objects; online acquisition fetches only explicitly
and verifies before publication to cache. `include_live=False` enables offline lookup.

Mechanical placement produces a detached native document from the selected record,
retaining its identity, provenance, rights and artifact hash where available. Later
registry updates do not mutate that placed copy; save the native part in the project
before referencing it in an assembly. In a cell, a verified manifest artifact is
materialized as a read-only, content-addressed project-local reference at
`.chudware/catalog/<sha256>/model.<format>`; an existing object must be immutable
and match the recorded digest and size. Cell paths remain subject to admission;
use `part_acquire` rather than `part_get(acquire=True)` inside a cell.

## 9. Parts Library contract revision 2 — browse, previews and source acquisition

This revision supersedes conflicting historical statements in §§3–7, including
§4 ranking and §7.4 unknown-category handling; the `aicad.parts/1` PartRecord
schema identifier is unchanged. Earlier text remains as the frozen v1 history.
Current producers are `services/parts/` (hub, index and adapters) and
`services/browser/adapters/`; native part generation and placement live in
`core/parts/`. `services/mcp/parts_tools.py` exposes search, get, placement,
sourcing, acquisition and library tools, not only the two tools listed in §5.
Section, fitting, hydraulic and ECAD adapters and `part_place` are implemented;
their older descriptions as reserved seams are historical.

### Search and browse

`PartsIndex.search` builds coverage-aware FTS tiers, dropping term groups absent
from the corpus and relaxing quorum when needed. Each tier scores an exact
normalized designation +1000, standard prefix +100, a large quorum bonus and
weighted negative BM25; the best score per part is retained and ties sort by
part ID. A real query with no matches returns no records, not arbitrary browse
results. GUI search applies additional relevance scoring and filters.

`parts_search` accepts nonnegative `offset` (default 0) and `limit` (default
50, capped at 200). Category, facets, price, material, vendor and source
filters and sorting precede pagination. Empty unscoped browsing uses stable,
diverse category/family ordering. The response includes `count` for the page,
`offset`, `total` for matching collected candidates, `total_exact`, and
`has_more` within those candidates. `total_exact=false` warns of incomplete
coverage, including the 20,000 candidate cap, live search, deferred native
ECAD browsing or provider failures; it is not a promise of a complete count.
Unknown `category_id` returns `ok:false`, `reason:"unknown_category"` and a
hint, rather than silently broadening the search.

GUI result items include `price` (0.0 when unknown), `currency` (empty when
unknown), USD-only `price_usd`, `kind`, `thumbnail_url`, `mpn`, `manufacturer`,
`stock` (-1 when unknown), `model_formats`, `family` and `designation`.
Unknown strings are empty, formats are an empty list, and an unknown library
vendor is empty rather than “Library”. Display names are concise human titles;
search aliases are not appended to them.

### Preview pipeline

`parts_thumbnail` and `parts_thumbnails` return preview provenance `cad_render`
for a native render or `product_image` for a vendor product photo. Logos and
favicons are not part previews. Missing previews return an unsuccessful entry
with a reason; batches are chunked rather than truncated.
`services.parts.thumbs.resolve_previews(items, budget_s=20.0, allow_network=True)`
accepts `{item_id, image_url}` requests and returns one ordered result per
request: `{item_id, ok, data, mime, provenance, reason}`. It uses bounded,
cached rendering and image fetching; `data` is bytes or `None`.

`part_search` defaults to `previews=True`. Its result rows have `index` labels
matching a PNG contact sheet of up to 16 rows, with title, vendor, designation
and either an aspect-preserved image tagged CAD render/Product photo or “No
preview”. `preview_sheet` carries `pictured`, `omitted` and `reason`; pictured
indices include tiles with no image. `previews=False` omits the image. Sheet
generation never acquires a model; preview appearance is not proof of identity
or dimensions.

### Browser source records and artifacts

`make_hit` normalizes source search hits to `site`, `title`, `url`, `kind`,
`sku`, `mpn`, `manufacturer`, `price`, `currency`, `stock`, `material`,
`thumbnail`, `model:{available,formats}` and `license`, plus optional source
fields. Kinds are `product`, `series`, `category`, `document`, `model`,
`project`, `page`. Unknown scalar values are null. Thumbnail URLs are absolute
HTTP(S) product/model images, not logos. The parts provider preserves known
metadata in `params` and creates sourcing entries when price is known.

Failures use `{ok:false, site, op, reason, hint, url, status, retry_after_s,
ask_user}`; unknown optional values are null. `FAIL_REASONS` comprises
`blocked`, `captcha`, `login_required`, `not_found`, `parse_failed`, `timeout`,
`transport_failed`, `policy_refused`, `unsupported`, `terms_required`,
`rate_limited`, `credentials_required` and `internal_error`. Enriched failures
may retain an original reason and other details. `credentials_required`
indicates missing API credentials, not browser sign-in. Search failures are
returned as notice entries, not disguised as successful hits.

Successful acquisition identifies `artifact_path`, `artifact_kind` (`cad`,
`document`, `ecad` or `archive`), `format` and provenance. Only validated CAD
can become import geometry; documents remain datasheets. `validate_cad` checks
file content, including STEP, IGES and STL, and safely inspects supported ZIP
members; it rejects empty, HTML and PDF masquerading as CAD. The validator also
supports other formats listed in `services/browser/cad_validate.py`.

DigiKey, Mouser and Nexar have official API search integrations configured
with the user's credentials in Settings. DigiKey and Mouser can fall back to
their public site when credentials are absent; Nexar reports
`credentials_required`. Source results and model availability depend on the
provider and access, not on an assumed CAD download.

## 10. Standalone section, fitting and seal placement (additive)

AS568 seal records use the `oring_as568` core family and an `AS568-xxx` size.
Their nominal inch dimensions are converted to millimetres without rounding,
and the resulting part is a circular sketch revolved into an editable torus.

With no target document, `section_profile` and `fitting_id` can create new
editable mechanical part documents. AISC and dressed-lumber sections use a
sketch and extrusion; `length_mm` is editable and defaults to three times the
section depth. The curated AISC table lacks rolled fillet radii and flange
taper, so generated steel profiles use sharp corners and parallel flanges, and
the idealized profile area need not equal the catalog's rolled-section area.

The curated AISC section table has been checked against the
[AISC Shapes Database v16.0](https://www.aisc.org/aisc/publications/steel-construction-manual/aisc-shapes-database-v160/).
Its corrected dimensional **and analysis** properties can change results
relative to older catalogs; rerun affected structural analyses.

Fitting geometry resolves the precise catalog ID against the native B16 table.
Elbow, tee, reducer and flange models use the catalog dimensions and pipe
schedule tables. When no schedule is supplied they use schedule 40, recorded
as an assumption. These are nominal generated models, not certification of
rolled, welded, threaded or sealing-face details. Missing catalog or geometry
data returns a failure; it must not substitute a different fitting or size.

Passing a structural or piping schema document retains the existing schema
placement path. Structural lookup and takeoff accept the catalog's `AISC360:`
and `NDS:` section-name prefixes; an NDS placement defaults to lumber material
unless a material is supplied. Fitting identity, including reducer secondary
NPS and flange class, comes from the catalog ID; conflicting overrides fail. Hydraulic schema
placement likewise uses the ISO 1219 component identity, rather than defaulting
an unspecified component type to a pump.
