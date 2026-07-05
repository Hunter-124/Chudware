# Contract: Unified Parts Library — `aicad.parts/1`

> Version 1 (FROZEN, workstream P day 1). The canonical seam every catalog adapter, search
> index, and insertion tool shares. Append-only per `contracts/README.md`: changes to an
> existing field's meaning require integrator sign-off + a version bump. New OPTIONAL fields
> may be added additively without a bump.
>
> Producers: `engine_oracle/parts_hub/` (registry + index), adapter modules under
> `engine_oracle/parts_hub/adapters/`. Consumers: `services/mcp/parts_tools.py`
> (`part_search`, `part_get`), future T-P3 `part_place`, T-P2 vendor adapters.
>
> **Compliance is normative.** No catalog rewrites — adapters WRAP existing surfaces.
> See `docs/AI_TOOLING_MASTER_PLAN.md` §Pillar 17 for intent.

## 0. Conventions

- **Domain vocabulary** (`domain` field): the eight reserved strings that identify what
  kind of part a record describes. New domains may be added without a version bump; these
  eight are reserved through T-P3:

  | domain | meaning | implemented |
  |---|---|---|
  | `fastener` | Metric/imperial threaded fasteners (ISO/DIN) | v1 (T-P1) |
  | `bearing` | Rolling-element bearings (ISO 15 deep-groove, etc.) | v1 (T-P1) |
  | `mech_family` | Core parametric families (`shcs`, `hex_nut`, `washer`, `bearing`, …) | v1 (T-P1) |
  | `section` | Structural sections (AISC, lumber — T-D3) | SEAM (T-P2) |
  | `fitting` | Piping fittings (T-E1) | SEAM (T-P2) |
  | `hydraulic` | ISO 1219 hydraulic components (T-E5) | SEAM (T-P2) |
  | `ecad` | Electronic components / KiCad symbols (T-B4) | SEAM (T-P2) |
  | `mech_family` | Additional parametric domains beyond core | extensible |
  | `catalog` | Third-party CAD catalog models (GrabCAD, TraceParts) acquired via the user's own authenticated browser session (T-X2/T-X3) — the §6.3 "paid catalog provider" seam, populated by the browser-session mechanism instead of/alongside a paid API | v1 (T-X3), additive domain |

- **part_id scheme**: `"<domain>:<catalog_id>"` — stable, globally unique, round-trippable
  via `get(part_id)`. Examples: `"fastener:ISO4762:M8"`, `"bearing:ISO15:6205"`,
  `"mech_family:shcs:M5"`. Colons within `catalog_id` are allowed; a `part_id` is split on
  the FIRST colon only.
- **Degradation**: a provider that cannot proceed returns
  `{"ok": false, "reason": <str>, "hint": <str>}` (§0 shape) — never a traceback, never a
  silent success. CI passes with no network and no native core.
- **Provenance required**: every `PartRecord` carries `license` and `provenance` (§1).
  Clean-room lineage per T-B4/T-D3 precedents.

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

`geometry_ref` names the instantiation path T-P3 will route to. It is a RECIPE, not
geometry — the record carries no inline mesh or B-rep. Reserved `kind` values:

| kind | meaning | fields |
|---|---|---|
| `core_family` | `parts_make` RPC on the C++ core (T-P1 adapters emit this) | `family`, `size`, `length_mm?` |
| `generate_model` | `spec_sheet_matcher.generate_model()` (BOLTS-derived) | `match` dict |
| `symbol_footprint` | KiCad symbol + PCB footprint | `library`, `libid`, `footprint` |
| `section_profile` | Structural-section sweep (T-D3) | `standard`, `designation` |
| `fitting_id` | Piping fitting catalog key (T-E1) | `fitting_id` |
| `fmu_component` | FMU/ISO-1219 hydraulic component (T-E5) | `component_id` |
| `import_file` | Local CAD file already on disk (T-X3: browser-acquired STEP/IGES) — routes through the same `import_file` core RPC `import_part` uses | `path` |

## 3. CatalogProvider protocol (frozen)

Every catalog adapter implements this protocol. T-P1 ships two adapters; T-P2 adds the
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
        Live-vendor providers (T-P2) implement their own network-backed search here."""

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
  (e.g. live-search-only vendors, T-P2) raise `NotImplementedError` — the index skips them
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

### 6.1 T-P2 — all-domain adapters + live vendor search

T-P2 adds `CatalogProvider` adapters for `section`, `fitting`, `hydraulic`, `ecad`
domains, plus live-vendor search providers (Nexar, DigiKey, Mouser — already in
`engine_oracle/parts_providers.py`). Live providers implement `search()` only
(no `iter_records()`); the hub falls back to live search when the local index has no hits
and a live provider covers the query domain.

### 6.2 T-P3 — `part_place()` + agent wiring

T-P3 adds `part_place(part_id, position?)` MCP tool that routes `geometry_ref` to the
correct instantiation path (C++ `parts_make`, structural sweep, ECAD placement) and returns
the doc_id. It also wires the parts search into the agent context guide
(standard-before-custom bias, cad_guide update).

### 6.3 T-BL12 — paid catalog providers

TraceParts / Werk24 and similar paid catalogs join as `CatalogProvider` adapters
(live-search, no `iter_records`) behind the same seam when volume justifies spend.
**Populated instead by T-X2/T-X3 (Hunter directive 2026-07-04):** `services/browser/
adapters/parts_provider.py`'s `BrowserCatalogAdapter` wraps GrabCAD/TraceParts as
`domain="catalog"` live-search providers using the user's own authenticated browser
session rather than a paid API — the parked `work/backlog/T-BL12.md` plan is a
different mechanism for an overlapping catalog and is untouched by this. Both
mechanisms can coexist behind this same seam if the paid-API plan is ever revived.
