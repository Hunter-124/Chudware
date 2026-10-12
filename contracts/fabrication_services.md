# Contract: Fabrication Services — quote-your-own-geometry (`aicad.fab/1`)

> Version 1 (DESIGN / SCOPING — NOT YET IMPLEMENTED). Defines the seam for **instant-quote
> cut/machine services** (SendCutSend, OSH Cut, Xometry, Fictiv, Protolabs, …). Producers:
> a future `engine_oracle/fab_hub/` + `services/browser/fab/` layer. Consumers:
> the Parts Library / Manufacture GUI.
>
> **Append-only per `contracts/README.md`.** This document is a scoping deliverable: it fixes
> the RPC surface, envelopes, taxonomy placement, and phasing so implementation can land
> incrementally without a redesign. Field meanings, once a v1 lands, follow the same
> frozen-seam rule as `contracts/parts.md` and `contracts/research_tools.md` §1.
>
> **This contract deliberately does NOT touch `taxonomy.json` or any existing contract.**
> See §3 for why fab services must stay out of a node's `sites` array.

## 1. Problem statement — why fab services do not fit `part_search`

The existing vendor layer (`services/browser/adapters/`, `contracts/parts.md`) is
**stock-catalog search**: the user queries a vendor's *inventory* and acquires an existing
SKU. The whole shape is `part_search(query) → [PartRecord] → part_get(part_id) → part_place`
(`SourceAdapter.search()` / `.acquire()`). There is a fixed catalog of things that already
exist; the user is *finding* one.

Fabrication services are **structurally different — there is nothing to search**:

| axis | stock-catalog vendor (existing) | fabrication service (this contract) |
|---|---|---|
| input | a text query (`"M8 socket head"`) | the **user's own geometry** (a doc / DXF / STEP) |
| what the vendor returns | catalog SKUs that match | a **price + lead time** for making *that* part |
| user choices | pick a result | material · thickness · finish · quantity · process |
| output artifact | a downloaded CAD model | a quote (and later, a placed order) |
| adapter verb | `search` / `acquire` | `quote` / `order_handoff` |

A fab service quotes **your geometry**, not its catalog. So it CANNOT be a
`SourceAdapter` and MUST NOT be routed through `part_search`. It gets its own tool family
(`fab_*`), its own registry, and its own envelope (`aicad.fab/1`), reusing the existing
**provenance**, **degradation**, and **geometry-export** primitives named below.

## 2. Proposed RPC / MCP contract

All tools return JSON. Success carries `ok:true`; every failure is the §4 degradation shape
(`{ok:false, reason, hint, ask_user?}`) — never a traceback (matches
`services/browser/adapters/__init__.py`).

### 2.1 `fab_services()` — enumerate services + capabilities

No geometry, no network mutation. Reads a static capability registry (see §3.2).

```json
{
  "ok": true,
  "services": [
    {
      "key": "sendcutsend",
      "label": "SendCutSend",
      "processes": ["laser_cut", "waterjet", "cnc_bend"],
      "instant_quote_api": false,
      "login_required": true,
      "materials": {
        "laser_cut": [
          {"material": "aluminum_5052", "thickness_mm": [0.5, 1.0, 1.6, 2.0, 3.0, 6.0],
           "finishes": ["none", "deburr", "anodize_black"]},
          {"material": "steel_mild", "thickness_mm": [0.9, 1.5, 3.0, 6.0], "finishes": ["none", "powdercoat"]}
        ]
      },
      "upload_url": "https://sendcutsend.com/",
      "dfm": {"min_feature_mm": 0.5, "max_sheet_mm": [1219, 813]}
    },
    {
      "key": "xometry",
      "label": "Xometry",
      "processes": ["cnc_mill", "cnc_turn", "print_sls", "print_fdm", "sheet_metal"],
      "instant_quote_api": true,
      "login_required": true,
      "materials": {"cnc_mill": [{"material": "aluminum_6061", "finishes": ["as_machined", "anodize_ii"]}]},
      "upload_url": "https://get.xometry.com/",
      "dfm": {"min_wall_mm": 0.8}
    }
  ]
}
```

Reserved process vocabulary (`process` field): `laser_cut` · `waterjet` · `plasma_cut` ·
`cnc_bend` (sheet bending) · `cnc_mill` · `cnc_turn` · `print_fdm` · `print_sls` ·
`print_sla` · `print_mjf`. Per-process **export format** (§2.4) is fixed by geometry
dimensionality, not by the service.

### 2.2 `fab_quote(...)` — the core: quote the user's geometry

```
fab_quote(geometry_ref, service, process, material,
          thickness_mm=None, finish="none", quantity=1, units="metric", opts=None)
```

- **`geometry_ref`** — the user's geometry, mirroring the `import_file` recipe in
  `contracts/parts.md` §2. Either:
  - `{"kind": "doc",  "doc_id": "<open doc>"}`  — the tool exports it (§2.4), or
  - `{"kind": "file", "path": "<local .step/.dxf>"}` — already exported on disk.
- **`service`** — a `key` from `fab_services()`; **`process`** — one of its `processes`.
- **`material` / `thickness_mm` / `finish` / `quantity`** — chosen from that service+process
  material menu. `thickness_mm` required for sheet processes (`laser_cut`/`waterjet`/`cnc_bend`),
  ignored for 3D processes.

**Success (a service with a real instant-quote API — §5 v2):**

```json
{
  "ok": true,
  "service": "xometry", "process": "cnc_mill",
  "price": 214.50, "currency": "USD",
  "per_unit": 42.90, "quantity": 5, "min_order": 1,
  "lead_time_days": 7,
  "quote_url": "https://xometry.com/quote/AB12CD",
  "geometry_export": {"format": "step", "path": "/…/part.step"},
  "dfm_flags": [],
  "provenance": {
    "ok": true, "source_url": "https://xometry.com/quote/AB12CD",
    "fetched_ts": "2026-07-08T12:00:00Z",
    "session_profile_used": "xometry",
    "license_note": "Quote copied with provenance; binding terms live on the service's site."
  }
}
```

`provenance` reuses `provenance_envelope(...)` verbatim
(`services/browser/adapters/__init__.py`) — `{source_url, fetched_ts, session_profile_used,
license_note}` plus additive keys. A price is a **copied-with-provenance quote**, never a
committed order (§4).

**Degradation — service with NO public instant-quote API (SendCutSend, OSH Cut):**
these require their own uploader UI. The honest path is the vendor-login pattern applied to
fab: export the file, then hand the user off to the service's upload page under their own
login. `fab_quote` returns a new sibling of the existing approval family (§4):

```json
{
  "ok": false, "reason": "upload_handoff_required",
  "hint": "SendCutSend has no instant-quote API — open its uploader to get a price.",
  "geometry_export": {"format": "dxf", "path": "/…/part.dxf"},
  "ask_user": {
    "id": "fab_upload.sendcutsend", "kind": "approval", "required": true, "suggested": true,
    "prompt": "Open SendCutSend and upload your exported part for an instant quote?",
    "why": "SendCutSend quotes are only available through their own uploader.",
    "proposal": "Export the part to /…/part.dxf and open sendcutsend.com for you to upload it."
  }
}
```

Other degradations reuse the existing helpers unchanged: `login_required(service, url)` when
no saved session exists; `terms_confirmation_required(...)` for a per-quote terms click.

### 2.3 `fab_order_handoff(...)` — quote → order (human-in-the-loop ONLY)

```
fab_order_handoff(service, quote_url=None, geometry_ref=None)
```

**Never places an automated purchase.** It opens the service's checkout under the user's own
login (same headed-browser mechanism as `vendor_login` in
`services/mcp/parts_tools.py`) and returns an approval envelope. The user completes payment
themselves. Returns `{ok:false, reason:"order_handoff", ask_user:{kind:"approval", …}}` —
the tool's job ends at "browser open on the checkout page".

### 2.4 How geometry reaches the service (export formats — real vs. gap)

Geometry is exported from the doc via the **existing** primitive
`services/formats/formats.py::export_document(doc, path, fmt=…)`.

| process class | export format | status of the export primitive |
|---|---|---|
| `cnc_mill` · `cnc_turn` · all `print_*` | **STEP** | ✅ EXISTS — `export_document(fmt="step")` → `build123d.export_step` (`formats.py` FormatInfo "step", `can_export=True`) |
| `laser_cut` · `waterjet` · `plasma_cut` · `cnc_bend` | **DXF / DWG** (2D flat pattern) | ✅ EXISTS — native `export_flat_dxf {doc_id, path, format:"dxf"\|"dwg"\|"pdf"}` writes the blank on OUTLINE, bends on BEND and holes on CUTS (contracts/worker_rpc.md §2D export); `export_file` also writes whole parts as `dxf`/`dwg`. The Python `export_document` helper itself still refuses DXF/DWG. |

**Consequence for v1:** laser/waterjet jobs take the flat pattern from `export_flat_dxf`
(DWG needs LibreDWG, provisioned by `scripts/provision_libredwg.py`); a part without a
Flatten feature has no flat pattern and must fall back to STEP upload.

## 3. Taxonomy placement — a `custom_fab` category ALONGSIDE stock catalogs

### 3.1 Fab services MUST NOT appear in a taxonomy node's `sites` array

`contracts/parts.md` §7.2 says a node's `sites` are the vendor adapters its `part_search`
routing fans out to. That field is **validated**:
`services/parts/test_taxonomy.py::test_every_routed_site_is_a_registered_adapter`
(line 304) asserts `referenced_sites ⊆ registered SourceAdapter keys`. A fab service is **not**
a registered `SourceAdapter` (it has no `search`/`acquire`), so listing `"sendcutsend"` in any
`sites` array would break that test. **Do not put fab services in `sites`.**

### 3.2 Recommended: a separate registry / field

Two compatible options; **(A) is preferred** because it keeps `taxonomy.json` untouched:

- **(A) Standalone registry module** — `services/parts/registry.py` holds the fab
  service catalog (the `fab_services()` payload of §2.1). No taxonomy edit; `fab_services()`
  reads this module directly. A parallel `test_every_fab_service_is_registered` guards it,
  mirroring the `sites` test but over the fab registry — never crossing into `sites`.
- **(B) A new OPTIONAL node field** — if fab services should surface under a taxonomy branch
  (e.g. a `custom_fab` root, or a `fab_services: ["sendcutsend", …]` array on relevant nodes),
  it MUST be a **new field name distinct from `sites`**, and the `sites`-validation test stays
  valid because it only inspects `sites`. Add a *separate* validator over `fab_services`.

Either way: `custom_fab` sits **beside** the stock-catalog taxonomy, never inside its
`sites` routing. `part_search` never returns a fab service; `fab_services()` never returns a
catalog SKU.

## 4. Provenance & safety

- **Copied-with-provenance, never auto-ordered.** Every `fab_quote` price carries a
  `provenance_envelope` (`source_url` + `fetched_ts` + `session_profile_used` +
  `license_note`). Binding commercial terms live on the service's site; a quote is a
  time-stamped *copy*, not an agreement.
- **No automated purchase — ever.** Ordering is a human-in-the-loop handoff (§2.3) under the
  user's own login. The agent may open the checkout; only the user pays.
- **Login / terms walls are surfaced, not bypassed.** Reuse `login_required`,
  `terms_confirmation_required`, and the new `upload_handoff_required` — all `{ok:false,…,
  ask_user:{kind:"approval"}}`. The agent NEVER accepts terms or logs in on the user's behalf.
- **DFM constraints (future validation).** `fab_services()` exposes per-service `dfm`
  (min feature size, min wall, max sheet size). v2 should validate exported geometry against
  the chosen service's `dfm` and return `dfm_flags` (e.g. "feature < min_feature_mm",
  "part exceeds max_sheet_mm") rather than sending an un-manufacturable file for quote.

## 5. Phasing

**v1 — list + export + browser upload handoff (no quote API).**
- `fab_services()` from a static registry (§3.2A).
- `fab_quote(...)` exports geometry via `export_document` (STEP; DXF pending §2.4) and returns
  `upload_handoff_required` — opens the service's uploader under the user's login.
- `fab_order_handoff(...)` opens checkout; user completes it.
- Honest and useful with zero paid API integration — it is the `vendor_login` pattern for fab.

**v2 — real instant-quote API where a service offers one.**
- Services WITH partner APIs (Xometry, Fictiv, Protolabs) → `fab_quote` returns a real
  `{price, lead_time_days, quote_url}` with provenance.
- Services WITHOUT an API (SendCutSend, OSH Cut) stay on the v1 upload-handoff path — this is
  a per-service capability (`instant_quote_api` flag in §2.1), not a global mode.
- Add real **DXF flat-pattern export** in `formats.py` for laser/waterjet.
- Add DFM pre-validation (§4) so quotes are only requested for manufacturable geometry.
