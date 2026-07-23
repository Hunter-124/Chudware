# Contract: Office documents — `office/1` (T-office)

> Read + edit **existing** office files a user attaches or the agent produces elsewhere —
> `.pptx` / `.xlsx` / `.xlsm` / `.csv` / `.pdf` / `.doc` / `.docx`. This is the complement to
> `contracts/deliverables.md` (`deliverables/1`): that contract authors *fresh* artifacts from a
> spec; this one opens, reads, and edits *pre-existing* documents in place. Open-source-first —
> no format parser is written here; every tool wraps an already-vetted library (§4).
>
> `office_extract` is **frozen**: it is the read seam the host AI-tab `docs_panel` uses to show
> document previews, exposed identically as an MCP tool and a worker RPC (§2). The rest of the
> `office_*` family is a normal (non-frozen) MCP tool surface, append-only per
> `contracts/README.md`.

## 1. Envelope

Every tool returns the flat §0/§6 MCP result envelope — `{ok:true, …}` on success,
`{ok:false, reason, hint}` on failure, **never a traceback**. A missing backing library
degrades to `reason:"engine_missing"`; a missing `soffice`/`libreoffice` binary (legacy `.doc`
only) degrades to `reason:"converter_missing"` (same code `deliverables/1` uses); a PDF op that
would require real text-content reflow degrades to `reason:"unsupported_op"`.

## 2. `office_extract` — frozen read contract

```
office_extract({path}) -> {ok, kind, text, sheets?}
```

- `kind` — the dispatched format: one of `pptx`, `xlsx`, `xlsm`, `csv`, `pdf`, `doc`, `docx`
  (extension-derived; `.doc` is converted through LibreOffice first but still reports `kind:"doc"`).
- `text` — a flattened, human-readable rendering of the document's content (slide text, sheet
  values as comma-joined rows, paragraph text, PDF page text, …).
- `sheets` — present **only** for `xlsx`/`xlsm`: `[{name, rows:[[...]]}]`, one entry per
  worksheet, cell values JSON-coerced (non-primitive types like `datetime` are stringified).

Both paths below call the **same** module-level `extract_office(path) -> dict` in
`services/mcp/office_tools.py` — the read logic lives exactly once:

1. **MCP tool** — `services/mcp/office_tools.py::register_office_tools`, family `"office"`,
   registered in `services/mcp/server.py`. Agent/external MCP client surface.
2. **Worker RPC** — `services/agent/worker_rpc.py` `OFFICE_METHODS["office_extract"]`, merged
   into `WORKER_METHODS` (the same newline-delimited-JSON worker seam every other GUI-facing
   RPC uses). The host's AI-tab document panel (`docs_panel`) calls this via
   `st.rpc.call("office_extract", {path})` to render an inline preview without a bespoke
   per-format C++ parser.

## 3. Full tool surface (`services/mcp/office_tools.py`, family `"office"`)

Risk classification (contract §6): `office_extract` + every `office_read_*` tool is
`RISK_READ` (explicit `_READ_EXACT` entries in `tool_registry.py` — none match the
`get_`/`describe_`/… prefix classifier because they all start with `office_`); every
`office_edit_*` tool defaults to `RISK_WRITE` (no override needed).

| tool | signature | returns |
|---|---|---|
| `office_extract` | `(path)` | `{ok, kind, text, sheets?}` — §2, frozen |
| `office_read_pptx` | `(path)` | `{ok, slides:[{index, title, text_runs, notes}]}` |
| `office_edit_pptx` | `(path, ops, out_path?)` | `{ok, path, slides_changed}` — `ops:[{slide, shape_index, text}]` |
| `office_read_xlsx` | `(path, sheet?)` | `{ok, sheets:[{name, rows}]}` (one sheet if `sheet` given) |
| `office_edit_xlsx` | `(path, sheet, cells, out_path?)` | `{ok, path, cells_changed, hint?}` — `cells:{"B2": 214.8, "C3": "=SUM(B2:B5)"}` |
| `office_read_csv` | `(path, limit?)` | `{ok, header, rows}` |
| `office_edit_csv` | `(path, rows, header?, out_path?)` | `{ok, path, row_count}` — full-file rewrite |
| `office_read_pdf` | `(path, page_range?)` | `{ok, pages:[{index, text}], page_count}` — `page_range` is 1-based ("1-3,5"), `index` in the result is 0-based |
| `office_edit_pdf` | `(path, ops, out_path?)` | `{ok, path, pages}` — page-level `ops` only, §4 |
| `office_read_docx` | `(path)` | `{ok, paragraphs, tables}` |
| `office_edit_docx` | `(path, ops, out_path?)` | `{ok, path, paragraphs_changed}` — `ops:[{paragraph_index, text}]` or `[{op:"append_paragraph", text}]` |
| `office_read_doc` | `(path)` | `{ok, paragraphs, tables}` — legacy `.doc` via LibreOffice→`.docx`, then read as docx |
| `office_edit_doc` | `(path, ops, out_path?)` | `{ok, path, paragraphs_changed, hint}` — lossy LibreOffice round-trip, §4 |

`out_path` on every edit tool is optional; omitted, the edit saves back over `path` in place.

## 4. Write limits (honest degrade, not silent data loss)

- **`.doc`** — there is no maintained pure-Python `.doc` writer. Every edit is a **lossy**
  round-trip: `soffice --headless --convert-to docx` → apply `office_edit_docx`-shape ops via
  python-docx → `soffice --headless --convert-to doc` back over the destination. Legacy
  `.doc`-only features (old field codes, some macros) may not survive an OOXML round-trip — this
  is inherent to the format and is always surfaced via the success `hint`, not hidden.
- **`.pptx`** — edits are shape/text/table **content only** (`text_frame.text` replacement,
  table cell text). Slide master/theme layout, animations, and embedded video are out of scope —
  python-pptx manipulates the shape tree, not the renderer; there is no auto-reflow of
  overflowing text.
- **`.pdf`** — `office_edit_pdf` is **page-level only**: rotate, delete, merge, set metadata.
  There is no safe pure-Python path to rewrite PDF content-stream text in place (true
  industry-wide, not a gap specific to this tool). Any op that implies text/content reflow
  (`edit_text`, `replace_text`, `reflow`) returns `{ok:false, reason:"unsupported_op", hint:
  "PDF text content cannot be edited directly — extract to docx, edit, then re-export to PDF
  via render_document"}` — the correct redirect is `deliverables/1`'s `render_document`.
- **`.xlsm`** — `keep_vba=True` is applied automatically (by extension) on both load and save,
  so the `vbaProject.bin` blob round-trips unmodified. Macro **code** is opaque — never read or
  edited, only cell/sheet data. `office_edit_xlsx` on an `.xlsm` file returns this caveat in
  its success `hint`.

## 5. Libraries (see `contracts/licenses.md` for the full ledger)

Reuses the already-pinned `deliverables/1` stack against arbitrary existing files (not just
fresh templates): python-pptx (MIT), openpyxl (MIT), python-docx (MIT), pypdfium2 (BSD, read
only). One new dependency: **pypdf** (BSD-3) for PDF metadata + page-level edits, since
pypdfium2 is render/extract-only. Legacy `.doc` and the `.doc` write round-trip go through the
LibreOffice-headless subprocess bridge already wired for `deliverables/1`'s `render_document`
(`services/deliverables/engines.py::soffice_bin`) — MPL-2.0, PATH-discovered, never bundled.
