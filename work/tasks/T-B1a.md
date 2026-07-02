---
id: T-B1a
title: Freeze sourcing_schema.md and extend VendorPart schema
workstream: B
owner: opus-wren
status: review
branch: task/T-B1a-sourcing-schema
day: 1
depends_on: [T-S1]
---

## Context

The morning deliverable for day 1 in workstream B is the contract freeze. `contracts/sourcing_schema.md` defines the canonical shape of priced BOM lines, provider interface, and quote records. `VendorPart` in `engine_oracle/vendor_parts.py` gains additive price/stock fields (`unit_price`, `currency`, `price_breaks[]`, `stock`, `lead_time_days`, `moq`, `fetched_ts`) — additive means no existing data breaks, no schema-version bump needed. All provider implementations on day 1 PM (T-B1b) conform to this schema.

## Files / areas touched

- `contracts/sourcing_schema.md` — new frozen contract
- `engine_oracle/vendor_parts.py` — VendorPart dataclass/schema, additive fields only
- `engine_oracle/parts_providers.py` — existing provider pattern; new fields must fit the interface

## Steps

1. Run `graphify query "VendorPart vendor parts schema"` to understand the current VendorPart shape.
2. Run `graphify explain "parts_providers.py"` to understand the existing provider interface.
3. Draft `contracts/sourcing_schema.md`: VendorPart extended schema, `PartProvider` interface contract, `BomLine` (made vs. bought, quantity, unit, resolved VendorPart or cost-model result), `QuoteRequest` / `QuoteResult` for Xometry/JLCPCB.
4. Add fields to `VendorPart` in `vendor_parts.py`: `unit_price: float | None = None`, `currency: str = "USD"`, `price_breaks: list[dict] = field(default_factory=list)`, `stock: int | None = None`, `lead_time_days: int | None = None`, `moq: int | None = None`, `fetched_ts: float | None = None`.
5. Update `__repr__` and any serialization helpers to include new fields.
6. Run existing vendor_parts tests: `python -m pytest engine_oracle/test_vendor_parts.py -q` — must still pass.

## Definition of done

- [x] `contracts/sourcing_schema.md` exists and covers VendorPart, PartProvider interface, BomLine, QuoteRequest/Result
- [x] VendorPart in `vendor_parts.py` has all seven new fields with sensible defaults
- [x] `python -m pytest engine_oracle/test_vendor_parts.py -q` passes without modification to existing tests

## AI hints

- `graphify query "VendorPart parts providers sourcing"` — map existing schema and provider code
- Relevant files: `engine_oracle/vendor_parts.py`, `engine_oracle/parts_providers.py`, `engine_oracle/parts_library.py`
- Keep new fields optional (default None) so existing code constructing VendorPart without price data still works
