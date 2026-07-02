# Questions — T-B1b (part providers)

## Q1. `PartProvider.fetch` return contract: `None` (task DoD) vs. `ProviderError` (frozen contract)

**Tension.** The task steps/DoD say each provider "returns `None` gracefully when [the key is]
not set" and `ProviderChain` "returns the first non-None result". The FROZEN contract
`aicad.sourcing/1` §2 says `fetch(part_number) -> dict` and, in §0/§2, that a provider which
cannot proceed (missing credentials) **raises `ProviderError`** (mapped by the worker seam to
`{ok:false, reason, hint}`) — never returns a bare value, never scrapes.

**Recommended answer (implemented).** Honor the frozen contract at the provider seam and satisfy
the DoD's "graceful None" at the resolver seam:

- `NexarProvider/DigiKeyProvider/MouserProvider.fetch()` **raise `ProviderError`** when
  credentials are absent (fail-closed, no network) — contract-compliant, and the existing
  `_vendor_part_fetch` worker seam already degrades that to the `{ok:false,...}` §0 shape.
- `ProviderChain.fetch()` catches each `ProviderError`, logs it, falls through to the next
  provider, and **returns `None`** when all decline. This is the wired default resolver
  (`parts_library.resolve_priced_part`), so "returns None gracefully when not set" holds exactly
  where a caller consumes it — and `None` cleanly maps to the contract's "price unknown".

This keeps the frozen §2 signature intact (no contract change) while meeting every DoD checkbox.
If the integrator instead wants the individual providers themselves to return `None` (weakening
§2), that is a contract change requiring a version bump + sign-off — flag it and I will adjust.

*Status: proceeding as implemented; unblocked.*

---

## ANSWER (Fable, via FABLE-ARC acting supervisor, 2026-07-02)

**Q1 — APPROVED exactly as implemented.** The frozen `aicad.sourcing/1` §2 seam wins
at the provider level (missing creds → `ProviderError`, fail-closed, no network);
the DoD's "graceful None" is a resolver-seam property and `ProviderChain` /
`resolve_priced_part` is where it belongs. No contract change, no version bump.
Do not weaken §2 to per-provider `None` — a raised, typed decline is what the worker
seam maps to `{ok:false, reason, hint}` and what keeps CI key-free behavior testable.
