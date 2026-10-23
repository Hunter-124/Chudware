# Contract: Sourcing Schema — `aicad.sourcing/1`

> Version 1 (FROZEN, workstream B day 1). This is the canonical shape of priced BOM lines,
> the supplier-provider interface, and quote records. Append-only per `contracts/README.md`:
> changes to an existing field's meaning require maintainer sign-off + a version bump. New
> OPTIONAL fields may be added additively without a bump.
>
> Producers: `core/sourcing/providers.cpp` (providers), the BOM roll-up, quote adapters
> (Xometry / JLCPCB). Consumers: cost-model, sourcing UI, the local sourcing model.
>
> **Compliance is normative.** No website scraping. Providers use a supplier's *approved* API
> with the customer's own credentials, or user-registered downloads. Provider API keys are
> configuration (`%APPDATA%/Chudware/config.json`), never committed.
> and `CONTRIBUTING.md`.

## 0. Conventions

- **Money** is a plain `float` in the record's `currency` (ISO-4217 string, e.g. `"USD"`). No
  implicit currency conversion — a consumer mixing currencies must convert explicitly.
- **Quantities** are integers ≥ 1. **Time** (`fetched_ts`) is epoch seconds (UTC), matching
  `time.time()`, the same convention as `VendorPart.added_ts`.
- **Unknown vs. zero:** `null`/`None` means *not known / not fetched*; `0` is a real value
  (e.g. `stock: 0` = confirmed out of stock). Consumers MUST NOT treat `None` as `0`.
- **Degradation:** a provider/quote adapter that cannot proceed returns a contract-shaped
  failure `{"ok": false, "reason": <str>, "hint": <str>}` (or raises `ProviderError`, which
  the worker seam maps to the same shape) — never a traceback, never a network call without
  configured credentials. CI passes with no network and no API keys.

## 1. VendorPart (extended)

`VendorPart` (`services/parts/vendor_parts.py`) is the cached supplier-part record. The sourcing
fields below are **additive and OPTIONAL** — every pre-sourcing record and any code constructing
a `VendorPart` without price data stays valid, so the registry `index.json` needs **no
schema-version bump**. Fields default to `None`/empty so "no price feed" is byte-compatible with
today's records.

```json
{
  "supplier": "McMaster-Carr",
  "part_number": "91251A542",
  "description": "Alloy steel socket head screw",
  "cad_path": ".../91251A542.step",
  "cad_format": "step",
  "product_url": "https://www.mcmaster.com/91251A542",
  "specs": {"thread": "M3", "length": "10 mm"},
  "source": "manual",
  "added_ts": 1735689600.0,
  "source_mtime": 0.0,

  "unit_price": 0.42,
  "currency": "USD",
  "price_breaks": [{"qty": 1, "unit_price": 0.42}, {"qty": 100, "unit_price": 0.31}],
  "stock": 5000,
  "lead_time_days": 3,
  "moq": 1,
  "fetched_ts": 1735689600.0
}
```

| field            | type              | default | meaning |
|------------------|-------------------|---------|---------|
| `unit_price`     | `float \| null`   | `null`  | price for a single unit at qty 1, in `currency` |
| `currency`       | `str`             | `"USD"` | ISO-4217 code all prices in this record are quoted in |
| `price_breaks`   | `list[dict]`      | `[]`    | ascending qty tiers, each `{"qty": int, "unit_price": float}`; the first tier's `qty` is the price floor (often `1` or `moq`) |
| `stock`          | `int \| null`     | `null`  | units available now; `null` unknown, `0` out of stock |
| `lead_time_days` | `int \| null`     | `null`  | business days to ship when not in stock |
| `moq`            | `int \| null`     | `null`  | minimum order quantity |
| `fetched_ts`     | `float \| null`   | `null`  | epoch seconds when price/stock was last fetched; drives staleness checks |

Fields marshal via `dataclasses.asdict` (existing `register_local_cad` path) and appear in the
auto-generated dataclass `repr`; no bespoke serializer. `price_breaks` entries SHOULD be sorted
by ascending `qty`; a consumer resolving a unit price for order quantity `q` picks the highest
tier whose `qty <= q`, falling back to `unit_price`.

## 2. PartProvider interface

`PartProvider` (`core/sourcing/providers.cpp`) is the pluggable supplier seam. A provider
resolves a supplier part number to a registered `VendorPart` record, optionally with pricing.

```python
class PartProvider:
    key: str                      # short stable id for dispatch + config lookup

    def name(self) -> str: ...

    def fetch(self, part_number: str) -> dict:
        """Land CAD on disk (if any), register it via vendor_parts.register_local_cad,
        and return the resulting VendorPart record as a dict. Populate the §1 sourcing
        fields when the approved API returns pricing/stock. Raise ProviderError (never a
        traceback, never a scrape) when credentials are absent or the fetch cannot proceed."""
```

Contract:
- `key` is unique per provider and is what `get_provider(supplier)` / `_ALIASES` resolve to.
- `fetch` is the ONLY required method. It returns a §1 record dict, or raises `ProviderError`
  with a human-readable `.message`. It MUST NOT scrape; missing credentials → `ProviderError`.
- The worker seam `_vendor_part_fetch` wraps `fetch` into `{"ok": true, "part": <record>}` on
  success and `{"ok": false, "error": <message>}` on `ProviderError` (existing behavior; the
  §0 degradation shape). New price fields ride inside `part` with no seam change.
- Pricing is OPTIONAL for a provider: a CAD-only provider returns a record with the §1 fields
  at their defaults (`None`/`[]`), which is valid.

## 3. BomLine

A single line of a priced bill of materials. Each line is either **made** (produced from a
part/assembly document via a cost model) or **bought** (resolved to a `VendorPart`).

```json
{
  "line_id": "L1",
  "kind": "bought",
  "quantity": 8,
  "unit": "ea",
  "part_ref": "McMaster-Carr::91251A542",
  "vendor_part": { VendorPart, see §1 },
  "cost_model": null,
  "unit_cost": 0.31,
  "ext_cost": 2.48,
  "currency": "USD"
}
```

| field         | type                  | meaning |
|---------------|-----------------------|---------|
| `line_id`     | `str`                 | opaque id, unique within the BOM (`"L1"…`) |
| `kind`        | `"made" \| "bought"`  | how the line is sourced |
| `quantity`    | `int` (≥1)            | count required for one parent assembly |
| `unit`        | `str`                 | unit of measure; `"ea"` for discrete parts |
| `part_ref`    | `str \| null`         | for `bought`: `"<supplier>::<part_number>"` (the `VendorPart.key`); else `null` |
| `vendor_part` | `VendorPart \| null`  | for `bought`: the resolved §1 record; else `null` |
| `cost_model`  | `object \| null`      | for `made`: the cost-model result (process, material, machine/setup/run costs); else `null` |
| `unit_cost`   | `float \| null`       | resolved cost of one unit at `quantity` (from the matching `price_breaks` tier, or the cost model); `null` if unpriced |
| `ext_cost`    | `float \| null`       | extended cost = `unit_cost * quantity`; `null` if unpriced |
| `currency`    | `str`                 | ISO-4217 for `unit_cost`/`ext_cost` |

Rules: exactly one of `vendor_part` (`bought`) / `cost_model` (`made`) is non-null, matching
`kind`. `unit_cost` for a `bought` line is resolved against `vendor_part.price_breaks` for
`quantity` (falling back to `unit_price`). An unpriced line keeps `unit_cost`/`ext_cost` `null`
rather than guessing.

## 4. QuoteRequest / QuoteResult

For on-demand manufacturing quotes from approved APIs (Xometry, JLCPCB). Adapters live behind the
same no-scrape / config-only-keys policy as providers; missing credentials degrade per §0.

### QuoteRequest

```json
{
  "provider": "xometry",
  "process": "cnc_milling",
  "part_file": ".../bracket.step",
  "material": "alu_6061",
  "finish": "as_machined",
  "quantity": 25,
  "unit": "ea",
  "options": {"tolerance": "iso_2768_m"}
}
```

| field       | type          | meaning |
|-------------|---------------|---------|
| `provider`  | `str`         | quote-adapter key, e.g. `"xometry"`, `"jlcpcb"` |
| `process`   | `str`         | manufacturing process, e.g. `"cnc_milling"`, `"sheet_metal"`, `"pcb_fab"`, `"pcb_assembly"` |
| `part_file` | `str`         | path to the geometry sent for quoting (STEP for mechanical, Gerber/BOM bundle for PCB) |
| `material`  | `str \| null` | material key (mechanical) or board spec (PCB); `null` = provider default |
| `finish`    | `str \| null` | surface finish / solder-mask spec; `null` = provider default |
| `quantity`  | `int` (≥1)    | quote quantity |
| `unit`      | `str`         | unit of measure, `"ea"` |
| `options`   | `object`      | free-form provider-specific options (tolerance, layers, lead-time tier, …) |

### QuoteResult

```json
{
  "ok": true,
  "provider": "xometry",
  "quantity": 25,
  "unit_price": 18.40,
  "ext_price": 460.00,
  "currency": "USD",
  "lead_time_days": 7,
  "price_breaks": [{"qty": 25, "unit_price": 18.40}, {"qty": 100, "unit_price": 12.90}],
  "quote_id": "XOM-abc123",
  "quote_url": "https://xometry.com/quote/abc123",
  "expires_ts": 1735776000.0,
  "fetched_ts": 1735689600.0
}
```

| field            | type            | meaning |
|------------------|-----------------|---------|
| `ok`             | `bool`          | success flag; on `false` the payload is `{"ok": false, "reason": str, "hint": str}` (§0) |
| `provider`       | `str`           | echoes the request's `provider` |
| `quantity`       | `int`           | quantity this result prices |
| `unit_price`     | `float \| null` | per-unit price at `quantity`, in `currency` |
| `ext_price`      | `float \| null` | extended price = `unit_price * quantity` |
| `currency`       | `str`           | ISO-4217 code |
| `lead_time_days` | `int \| null`   | business days to ship |
| `price_breaks`   | `list[dict]`    | qty tiers `{"qty", "unit_price"}`, same shape as §1 |
| `quote_id`       | `str \| null`   | provider quote reference for follow-up / ordering |
| `quote_url`      | `str \| null`   | link to the quote on the provider |
| `expires_ts`     | `float \| null` | epoch seconds when the quote expires |
| `fetched_ts`     | `float`         | epoch seconds when the quote was obtained |

A `QuoteResult` can be folded into a `made` `BomLine` (§3): `unit_price`→`unit_cost`,
`ext_price`→`ext_cost`, with `cost_model` recording `{"source": "quote", "provider", "quote_id"}`.
```


## Amendment — 2026-08-03

### `vendor_part_register`

The sourcing worker accepts a `vendor_part_register` request with the following
parameters:

```json
{
  "supplier": "<required non-empty string>",
  "part_number": "<required non-empty string>",
  "cad_path": "<optional string>",
  "description": "<optional string>",
  "product_url": "<optional string>",
  "specs": {},
  "source": "<optional string>",
  "unit_price": null,
  "currency": "USD",
  "price_breaks": [{"qty": 1, "unit_price": 1.25}],
  "stock": null,
  "lead_time_days": null,
  "moq": null,
  "fetched_ts": null,
  "provenance": {
    "method": "browser_scrape",
    "source_url": "https://supplier.example/part",
    "session_profile_used": "default",
    "license_note": "source terms apply",
    "scraped": true
  }
}
```

`supplier` and `part_number` are required. All other keys are optional. The
result is `{"ok": true, "part": <upserted record>}` on success or
`{"ok": false, "error": "<reason>"}` on validation or registration failure.
`price_breaks` entries require numeric `qty` and `unit_price` values.

Vendor index records are keyed by
`slug(supplier) + "::" + slug(part_number)`. A component lookup first uses that
supplier-qualified key. If the component has no supplier, or the qualified key
misses, lookup scans the index in lexicographic key order and returns the first
record whose `part_number` slug matches. Thus an ambiguous supplier-less lookup
is deterministic; a supplied exact supplier always takes precedence.

Registration is an upsert. Supplied optional keys overwrite the current record,
while omitted keys retain their current values. In particular, a price-only
upsert does not blank an existing `cad_path`, `cad_format`, `source_mtime`, or
`specs`; a new price-only record legitimately has an empty `cad_path` and null
`cad_format` and `source_mtime`. `added_ts` is written once at creation and
preserved, while `updated_ts` is written on every upsert. Records include a
`provenance` object, defaulting to `{}`. `currency` defaults to `"USD"` only
when a record is created. A supplied `fetched_ts` is retained verbatim; when it
is omitted from an upsert that supplies `unit_price`, `price_breaks`, `stock`,
`lead_time_days`, or `moq`, it defaults to the current epoch time. Other
upserts preserve the existing `fetched_ts`.

A scraped price MUST carry `provenance.scraped: true` and a `fetched_ts` that
records when it was obtained. A scraped price is explicitly not a vendor-quoted
figure and MUST remain distinguishable from API-sourced or formally quoted
pricing through its provenance.