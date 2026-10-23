# Contract: Deliverables — `deliverables/1`

> The agent must produce everything a professional engineer ships — presentations, reports,
> calc packages, spreadsheets, manuals — **inside the one application**. This contract freezes
> the MCP tool seam for that: five spec-driven tools that turn a JSON spec into a real office
> artifact, a provenance rule that guarantees **the model never types a load-bearing number**,
> and an artifact registry the rest of the system reads.
>
> **Contract-first.** This file is frozen BEFORE the code (`services/deliverables/`).
> Append-only per the `contracts/README.md` frozen-seam policy; changes need maintainer sign-off
> and a version bump (`deliverables/2`).
>
> **Open-source-first (master plan §3.6, CONTRIBUTING license guardrails).** We do NOT write
> format parsers/writers. We wrap established libraries (§7) and build only the engineering layer
> — templates and provenance — on top. Everything degrades in a **contract-shaped** way when an
> optional subprocess converter is absent; CI passes without pandoc or LibreOffice installed.

## 1. Overview

Five MCP tools, all registered by `services/mcp/server.py`, all returning the flat MCP result
envelope of §6 (`{ok, …}` on success, `{ok:false, reason, hint}` on failure — never a traceback):

| tool | signature | role |
|---|---|---|
| `make_presentation` | `(spec)` → artifact record | Build a slide deck (`.pptx`) from a spec. |
| `make_report` | `(spec)` → artifact record | Build a paginated report (`.docx` or `.pdf`) from a spec. |
| `make_workbook` | `(spec)` → artifact record | Build a spreadsheet (`.xlsx`) with **live formulas** from a spec. |
| `render_document` | `(path)` → preview | Render any supported file to downscaled preview PNGs + a `resource_link`. |
| `list_artifacts` | `(run_id?)` → records | Read the artifact registry (optionally filtered to one run). |

`make_manual` is **reserved** for later scope (technical-manual assembly) — it is named here so
the tool family and registry `kind` enum have a slot for it, but it is **not specified** by
`deliverables/1`. Do not implement it under this contract.

Design law for the whole family (so a local 4B model can drive it reliably):

1. **Structured in / structured out.** Input is a JSON spec; output is a JSON artifact record.
   No judgment is hidden in prose the model has to parse back.
2. **The model composes structure and prose; code fills the numbers.** The model authors the
   outline, headings, and narrative. Every load-bearing engineering value is a *slot* that the
   template engine resolves from attached tool results (§4). A bare numeral in a load-bearing
   slot is a **validation error**, not a value.
3. **Explicit units, explicit references.** Every provenance ref names its source tool and result
   key; units travel with the value.

## 2. The spec envelope (shared input to every `make_*`)

Every `make_*` takes exactly one object, `spec`:

```json
{
  "kind": "presentation",            // "presentation" | "report" | "workbook"; must match the tool
  "title": "Bracket — Design Review", // free prose, model-authored
  "template": "smoke_deck",          // template id (§8); one smoke template per format ships now
  "format": "pptx",                  // optional; per-kind default in §2.1–2.3
  "run_id": "run_2026-07-02_bracket",// optional; groups artifacts from one agent run
  "outline": [ /* blocks, §2.4 */ ],
  "attached_results": { /* result-id → tool result, §4 */ }
}
```

- `kind` must match the tool (`make_report` rejects `kind:"workbook"` with `invalid_spec`).
- `template` selects a registered template (§8). Unknown template → `invalid_spec`.
- `format` is optional; when present it must be in the tool's allowed set (§2.1–2.3) else
  `unsupported_format`.
- `run_id` is opaque; when omitted the engine assigns one and returns it on the record.
- `outline` and `attached_results` are the load-bearing parts, defined below.

### 2.1 `make_presentation` — allowed formats

`format` ∈ { `pptx` }; default `pptx`. Engine: python-pptx.

### 2.2 `make_report` — allowed formats

`format` ∈ { `docx`, `pdf` }; default `docx`. Engines: python-docx for `docx`;
WeasyPrint/reportlab (in-process, no subprocess) for `pdf`.

### 2.3 `make_workbook` — allowed formats

`format` ∈ { `xlsx` }; default `xlsx`. Engine: openpyxl.

### 2.4 Outline blocks

`outline` is an ordered list of blocks. The template decides how a block maps to slides / sections
/ sheets, so the same outline shape works across formats. Every block is:

```json
{
  "type": "section",                 // template-defined block type (e.g. title, section, table, chart, sheet)
  "heading": "Static FEA — 1200 N load",   // free prose slot (model-authored)
  "body": ["The bracket was evaluated under the worst-case 1200 N tip load."],  // free prose
  "slots": {                         // named slots; load-bearing ones MUST be provenance refs (§4)
    "max_stress": { "from": { "tool": "run_fea_static", "key": "max_von_mises_mpa" }, "unit": "MPa" },
    "min_fos":    { "from": { "tool": "run_fea_static", "key": "min_fos" }, "round": 2 }
  },
  "rows": [ /* table/workbook blocks only, §2.5 */ ]
}
```

- `heading`, `body`, and any caption/narrative text are **free prose** — model-authored, allowed
  to be literal strings (§4 slot taxonomy).
- `slots` maps a slot name to its value. A slot whose name is declared **load-bearing** by the
  template (§4) MUST be a provenance ref object; a literal there is a `provenance_violation`.
- `rows` (table and workbook blocks) is a list of row objects whose cells follow the same slot
  rule — a numeric-engineering cell is a provenance ref, a label cell is free prose.

## 3. Artifact record (frozen output schema)

Every `make_*` returns this record on success. It is what `describe_project`, the preview
pane, and AI telemetry consume — **do not change its shape without a version bump.**

```json
{
  "ok": true,
  "artifact_id": "art_report_01J9Z8Q3K7",  // opaque, stable, unique (ULID/uuid string)
  "kind": "report",                          // "presentation" | "report" | "workbook" | "manual"(reserved)
  "path": "artifacts/bracket-review.docx",  // path to the produced file, relative to the project dir
  "created_ts": "2026-07-02T18:22:05Z",     // ISO-8601 UTC
  "run_id": "run_2026-07-02_bracket",        // the grouping run id (assigned if the spec omitted it)
  "source_refs": [                           // the provenance actually consumed (traceable lineage)
    { "tool": "run_fea_static", "key": "max_von_mises_mpa", "result": "fea1" },
    { "tool": "run_fea_static", "key": "min_fos", "result": "fea1" }
  ],
  "spec_ref": "artifacts/art_report_01J9Z8Q3K7.spec.json"  // the exact spec, persisted for reproducibility
}
```

- `path` and `spec_ref` are project-relative so the record is portable with the project dir.
- `source_refs` is the **resolved** provenance set — the `{tool, key, result}` triples the
  template engine actually pulled — so lineage is auditable without re-parsing the spec.
- `spec_ref` points at the verbatim spec written next to the artifact; regenerating from it
  reproduces the file byte-for-byte given the same engine version.

## 4. Provenance rules (the soul of the contract)

The premise: **an engineering deliverable is only trustworthy if every load-bearing number
traces to a tool result.** The spec therefore carries an *attached tool-results map*, and
load-bearing slots reference it instead of stating a number.

### 4.1 Attached tool results

`spec.attached_results` is a map from an opaque **result id** to a captured tool result:

```json
"attached_results": {
  "fea1": {
    "tool": "run_fea_static",
    "ts": "2026-07-02T18:20:41Z",
    "payload": { "max_von_mises_mpa": 182.4, "max_disp_mm": 0.31, "min_fos": 1.51 }
  },
  "mass1": {
    "tool": "eval_mass",
    "ts": "2026-07-02T18:19:03Z",
    "payload": { "mass_g": 214.8, "volume_mm3": 27692.0, "material": "AL_6061_T6" }
  }
}
```

Each entry carries the source `tool` name, the capture `ts`, and the raw `payload` (the tool's
own result object). The map is the model's evidence bag; slots draw from it.

### 4.2 Slot resolution

A **load-bearing slot** is a provenance ref:

```json
{ "from": { "tool": "run_fea_static", "key": "max_von_mises_mpa", "result": "fea1" },
  "unit": "MPa", "round": 1 }
```

Resolution: locate the attached result — by `result` id when given, otherwise the single
`attached_results` entry whose `tool` matches (multiple candidates without a `result` id →
`slot_unresolved`) — then read `payload[key]`. `key` may be a dotted path into a nested payload.
Optional `unit` is carried onto the rendered value; optional `round` sets decimal places. The
resolved numeral is what the template writes into the file — the model never authored it.

### 4.3 Slot taxonomy — load-bearing vs free prose

| class | examples | rule |
|---|---|---|
| **Load-bearing** (numeric engineering value) | masses, stresses, margins / factors of safety, costs, dimensions, temperatures, pressures, frequencies, material properties, counts of engineering significance | MUST be a provenance ref (§4.2). A literal (bare numeral) here → `provenance_violation`. |
| **Free prose** (model-authored) | titles, headings, section narrative, recommendations, captions, table **label** cells | Literal strings allowed; no provenance required. |

The **template** declares which slot names in each block type are load-bearing (a fixed list per
template), so the model cannot silently downgrade a value slot to prose. This is the testable form
of "the model never types a number": a spec that puts `"max_stress": 182.4` (a literal) into a
load-bearing slot fails validation before any file is written.

### 4.4 Failure modes (never silent)

- Literal in a load-bearing slot → `{ok:false, reason:"provenance_violation", hint:"…"}`.
- Provenance ref whose `result` id is absent, whose `tool` matches nothing/many, or whose `key`
  is missing from the payload → `{ok:false, reason:"slot_unresolved", hint:"…"}`.
- Resolution failures are **errors, not blank cells** — a report is never emitted with a hole
  where a number should be.

## 5. Worked examples

### 5.1 `make_report` — full round trip

Input spec (a 4B-model-shaped spec: model wrote the structure and prose, code fills the numbers):

```json
{
  "kind": "report",
  "title": "Bracket — Static Strength Report",
  "template": "smoke_report",
  "format": "docx",
  "run_id": "run_2026-07-02_bracket",
  "outline": [
    { "type": "title", "heading": "Bracket — Static Strength Report",
      "body": ["Prepared for the day-7 design review."] },
    { "type": "section", "heading": "Result summary",
      "body": ["Under the worst-case 1200 N tip load the bracket keeps a positive margin."],
      "slots": {
        "max_stress": { "from": { "tool": "run_fea_static", "key": "max_von_mises_mpa", "result": "fea1" }, "unit": "MPa", "round": 1 },
        "min_fos":    { "from": { "tool": "run_fea_static", "key": "min_fos", "result": "fea1" }, "round": 2 },
        "mass":       { "from": { "tool": "eval_mass", "key": "mass_g", "result": "mass1" }, "unit": "g", "round": 1 }
      } }
  ],
  "attached_results": {
    "fea1":  { "tool": "run_fea_static", "ts": "2026-07-02T18:20:41Z",
               "payload": { "max_von_mises_mpa": 182.4, "min_fos": 1.51, "max_disp_mm": 0.31 } },
    "mass1": { "tool": "eval_mass", "ts": "2026-07-02T18:19:03Z",
               "payload": { "mass_g": 214.8, "material": "AL_6061_T6" } }
  }
}
```

Returned record:

```json
{
  "ok": true,
  "artifact_id": "art_report_01J9Z8Q3K7",
  "kind": "report",
  "path": "artifacts/bracket-static-strength-report.docx",
  "created_ts": "2026-07-02T18:22:05Z",
  "run_id": "run_2026-07-02_bracket",
  "source_refs": [
    { "tool": "run_fea_static", "key": "max_von_mises_mpa", "result": "fea1" },
    { "tool": "run_fea_static", "key": "min_fos", "result": "fea1" },
    { "tool": "eval_mass", "key": "mass_g", "result": "mass1" }
  ],
  "spec_ref": "artifacts/art_report_01J9Z8Q3K7.spec.json"
}
```

The report reads "Max von Mises 182.4 MPa · min FoS 1.51 · mass 214.8 g" — every number resolved
from `attached_results`, none typed by the model.

### 5.2 `make_presentation`

Same envelope; `kind:"presentation"`, `format` in `{pptx}` (default `pptx`), template
`smoke_deck`. Blocks map to slides (a `title` block → title slide; each `section` → a content
slide with its heading, body bullets, and resolved value slots). Returns a `kind:"presentation"`
record pointing at the `.pptx`.

### 5.3 `make_workbook`

`kind:"workbook"`, `format` in `{xlsx}` (default `xlsx`), template `smoke_workbook`. Blocks of
`type:"sheet"` carry `rows`; a cell is either a free-prose label, a resolved value slot, or a
**live formula** string (e.g. `"=SUM(B2:B5)"`) written as a real openpyxl formula so the sheet
recomputes when opened. The smoke template ships one sheet with at least one live formula.
Returns a `kind:"workbook"` record pointing at the `.xlsx`.

### 5.4 `render_document`

```json
render_document({ "path": "artifacts/bracket-static-strength-report.docx" })
```

→

```json
{
  "ok": true,
  "kind": "docx",
  "page_count": 1,
  "pages": [
    { "index": 1, "image": { "path": "artifacts/.previews/…_p1.png", "w": 791, "h": 1024, "bytes": 143902 } }
  ],
  "resource_link": "file:///…/artifacts/bracket-static-strength-report.docx"
}
```

- **Office formats** (`pptx`/`docx`/`xlsx`/`odt`/…) → PDF via **LibreOffice headless**
  (subprocess) → per-page PNG via **pypdfium2**.
- **Native paths** (no subprocess) for `pdf` (pypdfium2), `svg`/`png` (in-process rasterize/pass),
  `csv`/`md` (rendered to a preview image in-process).
- Every preview PNG is downscaled to **≤ 1024 px on the long edge and < 1 MB**; the full file is
  offered as an MCP `resource_link` so external MCP clients get previews too, without inlining a
  large binary.
- LibreOffice absent → `{ok:false, reason:"converter_missing", hint:"install libreoffice"}` (the
  office file is still on disk; only the preview degrades). Unknown extension →
  `{ok:false, reason:"unsupported_format", hint:"…"}`. Missing file →
  `{ok:false, reason:"artifact_not_found", hint:"…"}`.

### 5.5 `list_artifacts`

```json
list_artifacts()                             // all records
list_artifacts({ "run_id": "run_2026-07-02_bracket" })   // one run
```

→ `{ "ok": true, "count": 3, "artifacts": [ <record>, <record>, <record> ] }`. Records are the
exact §3 shape, newest first. An unknown `run_id` returns `count:0` with an empty list — **not**
an error.

## 6. Error codes

Every tool returns `{ "ok": false, "reason": "<snake_case_code>", "hint": "<actionable>" }` on
failure — never a traceback, never a partial file with a hole. `reason` is one of:

| reason | when | example hint |
|---|---|---|
| `invalid_spec` | malformed spec: `kind` mismatch, unknown `template`, missing required field, bad block shape | `"outline[1] missing 'type'"` |
| `provenance_violation` | a literal (bare numeral) sits in a load-bearing slot (§4.3) | `"slot 'max_stress' must reference a tool result, not a literal"` |
| `slot_unresolved` | a provenance ref names a missing/ambiguous result id or a key absent from the payload | `"result 'fea1' has no key 'min_fos'"` |
| `unsupported_format` | requested `format` not allowed for the tool, or `render_document` given an unknown extension | `"make_report supports docx, pdf"` |
| `converter_missing` | an optional subprocess converter (LibreOffice / pandoc) is not on PATH | `"install libreoffice"` |
| `artifact_not_found` | `render_document`/registry lookup for a path/id that does not exist | `"no file at artifacts/foo.pdf"` |

Implementations MAY add more codes in a later version; the six above are the frozen minimum every
caller can rely on.

## 7. Engines & license table (verbatim policy — no judgment calls)

Open-source-first: we wrap these; we write no format parser/writer. The bundle/subprocess/never
split is the CONTRIBUTING license guardrail applied to this module — **adding any engine requires
a row here and in `contracts/licenses.md`.**

| engine | license | column | role |
|---|---|---|---|
| python-pptx | MIT | **in-process** | `.pptx` decks |
| python-docx | MIT | **in-process** | `.docx` reports |
| openpyxl | MIT | **in-process** | `.xlsx` workbooks (live formulas) |
| matplotlib | PSF | **in-process** | charts/figures embedded in artifacts |
| WeasyPrint / reportlab | BSD | **in-process** | HTML/CSS → PDF and direct PDF reports |
| pypdfium2 (pdfium) | BSD | **in-process** | PDF → PNG page rasterization for previews |
| cairosvg | LGPL-3.0 | **optional in-process** | best-effort SVG → PNG previews; import-only, feature-detected, never bundled or required; absent → LibreOffice path → `converter_missing` |
| pandoc | GPL | **subprocess only** | optional markup conversion; discovered on PATH |
| LibreOffice headless | MPL-2.0 | **subprocess only** | office → PDF for `render_document`; discovered on PATH |
| ~~PyMuPDF / pymupdf4llm~~ | ~~AGPL~~ | **NEVER** | forbidden — AGPL; use pypdfium2 instead |

Rules:

- In-process engines are permissive (MIT/BSD/PSF) and always available — the `make_*` tools and
  the native-format paths of `render_document` never need a subprocess.
- **Optional in-process** engines (currently only cairosvg, LGPL) are import-only and
  feature-detected: used when present in the user's environment, never bundled, never a hard
  dependency, never in `requirements.txt`/installer deps (the CONTRIBUTING LGPL dynamic-link
  posture, same as Netgen). Every path they serve degrades per §6 when they are absent.
- Subprocess-only engines (GPL pandoc, MPL LibreOffice) are **mere aggregation**: invoked as
  external processes discovered on PATH, never linked, never bundled into our code. The installer
  offers to install them; the source offer obligation is the installer's.
- **CI must pass with neither pandoc nor LibreOffice installed.** Any path that needs one degrades
  to `converter_missing` (§6); tests mock the subprocess. Never PyMuPDF/pymupdf4llm (AGPL) — the
  PDF→PNG path is pypdfium2, full stop.

## 8. Templates & scope fence

A **template** is a named, versioned recipe that maps outline blocks to a file via one engine. The
tool selects it by `spec.template`; the template declares its block types and its load-bearing slot
names (§4.3). Templates are looked up through a generic registry (name → builder) so new templates
**slot in without any change to this contract.**

`deliverables/1` ships exactly **one smoke template per format**, to prove the seam end to end:

| template | format | contents |
|---|---|---|
| `smoke_deck` | pptx | a title slide + one content slide |
| `smoke_report` | docx | a one-page report (title + one result section) |
| `smoke_workbook` | xlsx | a one-sheet workbook with at least one **live formula** |

Out of scope for this contract (named so the boundary is explicit):

- Real engineering templates — design-review deck, calc package, BOM workbook — are separate
  work. They register against the same generic template registry; no contract change is required.
- The in-app preview pane is separate work; it consumes the §3 artifact record and `render_document`.

A `cad_guide('deliverables')` topic (registered by the implementation) documents this
tool family for agents at runtime.

## 9. Versioning & freeze

- Contract id: **`deliverables/1`**. Frozen 2026-07-02.
- Append-only. The §3 artifact record, the §4 provenance rules, and the §6 reason-code set are the
  load-bearing seams other components (narration, preview pane, telemetry) build on — a breaking change to any of them
  requires maintainer sign-off and `deliverables/2`.
- Adding a template (§8) or an in-process engine row (§7, with the matching `contracts/licenses.md`
  row) is a compatible, non-versioning change.
