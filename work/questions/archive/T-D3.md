# T-D3 Questions
> ARCHIVED 2026-08-04 — task closed; the driver proceeded on its recommended default, per work/questions/README.md.

## Q1 — AISC Shapes Database licensing: clean-room curated table chosen

**Driver (sonnet-mave):** The task says to download `aisc_shapes_v16.xlsx` (≈10 MB Excel workbook
from aisc.org). The AISC manual compilation itself has a non-trivial license that is not clearly
public-domain, and the repo precedent (T-B4 / opus-birch) is to **not vendor** a whole proprietary
database (GPL BOLTS was excluded for the same reason); instead clean-room a small curated table of
the shapes actually needed.

**Recommended answer (implemented):** Bundle a *small curated JSON table* of the AISC W-shapes
used by the structural workstream (W8×31 plus a range of common W/S/C sections), with every
property value cited to its public-domain source:
- AISC Steel Construction Manual Table 1-1 values re-stated as *dimensional facts* (same precedent
  as `analysis.py`'s `_MATERIALS` block, which already says "the numbers are facts, not the
  copyrighted tables").
- Cross-checked against the free CISC Handbook / NSAI / open structural textbooks to satisfy the
  "facts not tables" doctrine.
- A `licenses.md` row is added stating the posture.

**Why not download the full XLSX:** (a) requires a network hit inside the MCP venv at runtime —
unreliable in air-gapped or offline environments; (b) bundling the binary blob without a clear
MIT/CC0 grant is a license risk; (c) the structural workstream only exercises a handful of shapes
in tests and demos — a curated table of ~30 common shapes is sufficient.

**Scope for T-D4/T-D5:** if the compliance RAG (T-D4) needs the full 3000-row DB, that task can
add a `get_aisc_full_db()` loader that accepts a *user-supplied* AISC XLSX path (not bundled).
That keeps this repo GPL-clean while letting power users point at their own copy.

## ANSWER (Q1) — Erik via Fable, 2026-07-03

**Ratified** — the clean-room curated table stands (and remains the better engineering
choice: offline-safe, small, cited). Note the broader policy shift the same day: Erik
ruled that early personal-use builds may relax license compliance entirely, provided
every shortcut gets a row in `contracts/license_debt.md` (cleanroom-before-SaaS
register). So a future task MAY bundle the full AISC XLSX for personal-use builds if it
adds a debt row; the user-supplied-path loader from the "Scope for T-D4/T-D5" paragraph
remains the SaaS-clean end state.
