# Parts library & vendor-part registry

How AI-CAD sources standard/vendor parts (fasteners, nuts, washers, bearings, vendor CAD)
and makes them available to the design/agent flow.

There are **three** ways a part can enter a design, in order of preference:

1. **Parametric catalog** — the C++ core's `parts_make` RPC (`core/parts`) generates common
   hardware (screws, nuts, washers, etc.) *parametrically* from dimensions. No download, no
   license concerns. This is the other developer's piece; prefer it when a part is a standard
   parametric primitive. The registry below is for *vendor CAD* that is not parametric.
2. **Watched parts-library folder** — drop CAD files into a folder and they auto-register.
3. **Provider seam** — fetch CAD from a supplier via that supplier's **approved API** using
   the customer's own credentials. No website scraping, ever.

All non-parametric paths land in the **vendor-part registry**.

## The registry

`engine/vendor_parts.py` owns the registry. It writes
`%APPDATA%/AI-CAD/vendor_parts/index.json`, keyed by `"<supplier-slug>::<part-slug>"`. Each
record is a `VendorPart`:

| field          | meaning                                                        |
|----------------|----------------------------------------------------------------|
| `supplier`     | e.g. `McMaster-Carr`, `fasteners`, `library`                   |
| `part_number`  | e.g. `91251A542`, `M3x10`                                      |
| `description`  | free text                                                      |
| `cad_path`     | absolute path to the cached/registered CAD file                |
| `cad_format`   | `step` / `iges` / `brep` / `stl` (from `CAD_EXTS`)             |
| `product_url`  | optional supplier URL                                          |
| `specs`        | dict, e.g. `{"thread": "M3", "length": 10}`                   |
| `source`       | `manual` / `library` / `api`                                  |
| `added_ts`     | registration time                                              |
| `source_mtime` | *(optional, added for the watched folder)* on-disk mtime when last registered |

The **C++ core reads this same `index.json`** (`doc_vendor_part_insert`) and inserts a part as
a frozen `import` feature. `source_mtime` is an optional additive field the core ignores —
the schema/shape stays compatible.

Worker RPCs (registered in `vendor_parts.WORKER_METHODS`):

- `vendor_part_register_local_cad {supplier, part_number, cad_path, description?, product_url?, specs?, source?, copy_file?}`
- `vendor_part_list {supplier?, query?}`
- `vendor_part_context {limit?, query?}` — compact card for a local LLM
- `vendor_part_scan_library {folder?}` — scan the watched folder (below)
- `vendor_part_fetch {supplier, part_number}` — provider seam (below)

## Watched "parts library" folder + auto-register

Default path: `%APPDATA%/AI-CAD/parts_library` (`engine/paths.PARTS_LIBRARY`, created on
access via `paths.parts_library_dir()`). `engine/parts_library.scan_library_folder(folder?)`
recursively scans for CAD files (the `CAD_EXTS` set) and registers each one.

Inference rules:

- **supplier** — the *top-level subfolder* under the library root, else `"library"`:
  - `parts_library/McMaster-Carr/91251A542.step` → supplier `McMaster-Carr`
  - `parts_library/fasteners/M3x10.step` → supplier `fasteners`
  - `parts_library/loose_part.step` → supplier `library`
- **part_number** — the filename stem.
- **specs** — parsed from the stem where obvious: `M3x10` → `{thread:"M3", length:10}`,
  `M6-1.0x20` → `{thread:"M6", pitch:1.0, length:20}`. No match → empty `specs`.

Files in the library folder are registered **in place** (`copy_file=False`) — the folder is
itself the canonical store.

**Idempotent:** a file already registered with the same path *and* the same mtime is skipped.
A file whose mtime changed is re-registered. Returns
`{"scanned": N, "added": M, "updated": K, "parts": [records]}`.

```jsonc
// vendor_part_scan_library
{ "scanned": 3, "added": 3, "updated": 0, "parts": [ /* records */ ] }
```

## Provider seam

`engine/parts_providers.py` defines a small `PartProvider` base (`name()`, `fetch(part_number)`)
and a provider registry. `fetch()` either lands a CAD file and registers it (returning the
record) or raises `ProviderError` (which always carries `.message`).

Dispatch: `vendor_part_fetch {supplier, part_number}` → `get_provider(supplier).fetch(...)`.
Supplier names are alias-normalized (`"McMaster-Carr"` → `mcmaster`). The worker method
soft-fails to `{"ok": false, "error": "<message>"}` on `ProviderError`.

Shipped providers:

- **`McMasterProvider`** — official McMaster-Carr Product Information API (details below).
- **`GrabCadProvider`**, **`TracePartsProvider`** — scaffolds that raise
  `"<supplier> provider is not yet configured"` until wired.

### Configuring McMaster-Carr

McMaster-Carr exposes an **approved Product Information API**. It requires an *approved API
account* plus a *client certificate* and login credentials. **AI-CAD does not — and will not —
scrape mcmaster.com.** Without configured credentials, `McMasterProvider.fetch()` raises a
`ProviderError` telling you to either configure the API or download the CAD manually and call
`vendor_part_register_local_cad`.

Add a `parts_providers` section to `%APPDATA%/AI-CAD/config.json`:

```jsonc
{
  "parts_providers": {
    "mcmaster": {
      "enabled": false,                          // flip to true once validated
      "api_url": "https://api.mcmaster.com",     // your approved endpoint
      "client_cert": "C:/path/to/client.pfx",    // certificate issued to your account
      "username": "you@example.com",
      "password": "..."
    }
  }
}
```

`fetch()` proceeds to the API path only when `enabled` is true **and** `api_url`, `username`,
and (`password` or `client_cert`) are present. The authenticated request itself is the single
intentional stub — `_call_mcmaster_api()` raises `NotImplementedError("wire to approved
McMaster API once credentials validated")` because it cannot be tested against real
credentials here. Wire that one function (download CAD → `register_local_cad(...,
source="api")`) once an account + certificate are validated.

## Policy: no scraping

Vendor CAD enters AI-CAD only via **approved supplier APIs** (with the customer's own
credentials) or **user-provided downloads** registered through the registry. No provider in
this module fetches from a supplier website. Unconfigured providers fail closed with an
actionable message rather than falling back to scraping.
