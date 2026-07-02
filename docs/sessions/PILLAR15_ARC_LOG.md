# Pillar 15 arc log — T-F14 → T-F15 → T-F16 (FABLE-ARC)

**CONTINUATION PLAN (read first):** If Fable usage runs out mid-arc, the continuation
is the same FABLE-ARC prompt (the one that started this session) run by an Opus
session with the first line changed to "You are OPUS-ARC, acting arc driver". Resume
from this log + the task files (`work/tasks/T-F14.md` / `T-F15.md` / `T-F16.md`),
their `## HANDOFF` sections, and the branch state described below. Nothing else is
needed.

**Arc goal:** agents ship professional engineering documents, previewed in-app.
Finish line (after human merges): one agent conversation produces (1) a design-review
deck with rendered views, mass props, FEA margins; (2) a calc-package PDF with cited
material data; (3) a BOM workbook with live formulas — all previewable in-app as
artifact cards, every number traceable, listed by `list_artifacts`. Demo script goes
in this log's final section.

**Merge order (binding): F14 → F15 → F16.** Each task branches FROM the previous
task's branch, never from master after F14. Never merge to master; never set done;
stop each task at `status: review`.

**Roles:** arc driver (this session) does architecture, contract wording, briefs,
diff review, bookkeeping ONLY. All implementation via Opus subagents, which delegate
grunt work to Sonnet/Haiku. Budget law: Fable ~9% weekly remaining, Opus ~40%.

**Branches:**
- T-F14: `task/T-F14-deliverables-scaffold`, worktree `../CAD-T-F14` (base: master @ ab62054)
- T-F15: `task/T-F15-...` — branch from T-F14's branch (not yet created)
- T-F16: `task/T-F16-...` — branch from T-F15's branch (not yet created)

**Standing rules for every dispatch (paste into briefs):** see work/FOREMAN.md
"Context & cost discipline" + Known pitfalls; graphify-first (host/src not indexed);
host/ deprecated — UI work in ui/imgui + ui/abstract; licenses MIT/BSD/Apache
in-process, pandoc/LibreOffice SUBPROCESS ONLY, AGPL never; CI green without
subprocess converters (contract-shaped degradation, no tracebacks); provenance rule
§3.3 — bare numeral in a load-bearing slot is a validation ERROR with a test;
gate.sh ui/imgui selMulti failure is PRE-EXISTING, never chase; commit+push every
~15-20 min; write outputs to worktree files as you go.

---

## Log (append-only: what landed + hash, decisions + why, next dispatch)

### 2026-07-02 — arc start

- Claimed T-F14 (owner fable-arc, status doing) on master @ ab62054; worktree
  `../CAD-T-F14` on `task/T-F14-deliverables-scaffold`.
- **Decisions (FABLE-ARC, pre-dispatch):**
  - Contract id `deliverables/1`; five tools this task: make_presentation,
    make_report, make_workbook, render_document, list_artifacts (make_manual is
    T-F15+ scope).
  - Artifact record schema (frozen in contract): `{artifact_id, kind, path,
    created_ts, run_id, source_refs[], spec_ref}` per T-F14 driver notes.
  - Slot mechanics: spec JSON carries an attached tool-results map; load-bearing
    slots reference results as `{"slot":"max_stress","from":{"tool":"run_fea_static",
    "key":"max_von_mises_mpa"}}`; validator rejects bare numerals in load-bearing
    slots (that is the testable form of "the model never types a number").
  - Engines: python-pptx / python-docx / openpyxl / matplotlib / WeasyPrint or
    reportlab / pypdfium2 in-process; pandoc + LibreOffice headless subprocess-only,
    discovered on PATH, `{ok:false, reason:"converter_missing", hint:...}` when absent.
  - Previews ≤1024 px / <1 MB, resource_link to the full file.
- **Dispatch plan:** D1 (Opus) drafts contracts/deliverables.md → FABLE-ARC reviews
  wording → D2 (Opus) implements services/deliverables/ + MCP registration +
  installer/provision.py + tests + DoD pass, delegating sweeps/scaffolds to Sonnet.
- Next dispatch: D1 contract draft (see brief in Task tool call, this session).

### 2026-07-02 — front widened (Erik-authorized)

- Erik authorized parallel work beyond the Pillar 15 arc for throughput/cache
  efficiency. FABLE-ARC claimed and dispatched four additional autonomous Opus
  drivers on zero-dependency tasks (claims atomic via work.py; FOREMAN check-in on
  master @ db3d8a4):
  - opus-ivy → T-F1 (research tools contract + engine/research), worktree ../CAD-T-F1
  - opus-rook → T-F3 (interaction contract + ask_user + in-app dialog), ../CAD-T-F3;
    owns agent_panel.cpp hot-spot as first claimer, briefed: dialog in its own
    component files, minimal append-only panel wiring
  - opus-sage → T-F5 (image ingestion v1), ../CAD-T-F5
  - opus-pike → T-F9 (providers contract; T-H9 routing stub reserved), ../CAD-T-F9
- All four briefed with FOREMAN discipline + continuity + graphify rules; they run
  `work.py prompt <id>` themselves and finish at status: review + FOREMAN REVIEW line.
- Meanwhile D1 (T-F14 contract draft) still in flight on this branch.

### 2026-07-02 — contract frozen; D2 dispatched; question-handling authority

- D1 landed contracts/deliverables.md (4d5e060). FABLE-ARC review: matches all frozen
  decisions; one defect fixed in review — per-tool allowed-format sets were only in
  examples, now normative §2.1–2.3 (46ea6f2). Contract APPROVED/frozen at 46ea6f2.
- D2 (Opus) dispatched: full implementation (services/deliverables/, MCP registration,
  installer, tests, smoke templates, DoD pass) against the frozen contract.
- **Erik directive:** driver questions get answered by the arc session best-effort
  (ANSWER in the question file, noted in FOREMAN); anything genuinely needing Erik is
  DEFERRED into "## Questions for Erik" at the bottom of this log instead of blocking.

### 2026-07-02 — D2 landed (services/deliverables + MCP + installer + tests)

- **D2 (Opus) implemented the full scaffold** on `task/T-F14-deliverables-scaffold` (rebased
  onto origin/master bf1f718; server.py hot-spot had no divergence — clean append).
  Commits: module `7181b20`, MCP registration `20c8844`, installer `82d6c24`, tests `7eb6ba5`
  (bookkeeping commit follows).
  - `services/deliverables/` (7 impl modules + guide + mcp_tools + tests; repo-root symlink
    `deliverables/`): spec validator + provenance slot/cell resolution, engine wrappers
    (python-pptx/docx/openpyxl/reportlab in-process; pypdfium2 PDF→PNG; matplotlib csv/md;
    LibreOffice/pandoc PATH-discovered subprocess-only), generic template registry + three smoke
    templates (deck/report/workbook with a live `=B2*C2` formula), artifact registry (§3 records
    under `<project>/artifacts/registry.json`, spec persisted as `spec_ref`).
  - `server.py` append-only `register_deliverables_tools(mcp)`; `cad_guide('deliverables')` topic.
  - `requirements.txt` + `provision.py` engine pins + `converters_status()` PATH discovery.
- **Tests:** 20 passed (`pytest services/deliverables -q`), incl. §5.1 4B-spec per format
  (pptx/docx/pdf/xlsx), provenance_violation (literal in load-bearing slot/column), slot_unresolved
  ×3, render_document mocked-converter + converter_missing degradation (artifact untouched), native
  pdf/csv, registry round-trip + run_id filter. Green with neither pandoc nor LibreOffice installed
  (verified under `env -i`, empty PATH).
- **Gate:** `graphify update .` OK (AST-only, 19251 nodes). `scripts/gate.sh` — core build + ctest
  PASS; ui/imgui `PanelField::selMulti` failure is PRE-EXISTING (not chased). Stage-4 pytest
  (`engine/doc agent`) errors on `ModuleNotFoundError: No module named 'OCP'` — an unprovisioned
  system-python3.14 runtime (no build123d/OCP venv), NOT a D2 regression (D2 touches no engine/doc
  file; error even collects from a sibling worktree path). Deliverables suite is separate and green.
- **Repair-warning DoD check:** LibreOffice IS on PATH locally — `soffice --headless --convert-to
  pdf` succeeds for the docx/xlsx smoke artifacts (returncode 0, no repair prompt); pdf is native
  (reportlab). pptx builds valid (2 slides) via python-pptx.
- **Open question:** `work/questions/T-F14.md` Q1 — no permissive in-process SVG rasterizer in the
  §7 engine set; svg preview is best-effort (optional cairosvg → office fallback → converter_missing).
  Recommended answer: accept for `deliverables/1` (svg is not a make_* output format); add resvg/
  cairosvg §7 row later if native svg preview is wanted. Non-blocking.
- Next: F14 ready for human merge (order F14→F15→F16); T-F15 branches FROM this branch.

## Questions for Erik (batched — answer when convenient)

- (none yet)

### 2026-07-02 — wave-1 reviews: T-F1 + T-F3 landed; backfills T-B1a + T-E1

- T-F1 (opus-ivy) REVIEWED LGTM merge-ready: contracts/research_tools.md v1 +
  engine/research scaffold, 14/14 tests, gate fails pre-existing only (selMulti +
  engine/doc OCP). Branch task/T-F1-research-scaffold @ 4db86d3.
- T-F3 (opus-rook) REVIEWED LGTM merge-ready: contracts/interaction.md v1 +
  interaction.py + 3 transports + ui/abstract ask_form + ui/imgui ask_dialog,
  18/18 pytest, agent_panel.cpp hot-spot held to 1 include + 1 call. Branch
  task/T-F3-interaction-contract (my Q1-Q3 ANSWER commit 85c4fb7 on top).
- Supervisor answers issued (Erik's best-effort directive): Q1 ledger/caller split
  approved (C++ run machine untouched until T-F4); Q2 normalize_dimension approved
  and declared canonical for T-F2's unit_convert (one implementation); Q3 local
  XDG mirroring approved. Repo-wide paths.py XDG divergence filed as GitHub issue
  (label: bug) — found independently by T-F1 and T-F3.
- Backfilled freed driver slots: opus-wren → T-B1a (sourcing schema), opus-lark →
  T-E1 (piping scaffold). Concurrent front stays at 5: D2 (T-F14 impl), T-F5,
  T-F9, T-B1a, T-E1.
- Note: a separate supervisor instance is running workstream G (T-G2 reviewed,
  T-G4 sonnet-oda) — WS-G is off-limits to this arc's backfills.

### 2026-07-02 — T-F15 driver (Opus): five engineering templates landed on the F14 branch

- **Base:** branched FROM `task/T-F14-deliverables-scaffold` @ 27e2433 (merge order F14→F15→F16;
  never onto master, which lacks F14). Task file aligned owner fable-arc / status doing (cf9f007).
- **Contract-compat decision (no `deliverables/2` needed):** all five templates register against
  the frozen generic registry (contract §8) and are driven by the THREE existing make_* tools —
  `make_manual` stays reserved and unimplemented. Recorded recommendation + rationale in
  `work/questions/T-F15.md` (accept manuals as a make_report template; "md→pdf" rendered directly
  via in-process docx/reportlab, no pandoc/markdown intermediate). Non-blocking.
- **Package split:** flat `templates.py` → `templates/` package (matches the task-file layout).
  Registry core + shared helpers in `templates/__init__.py`; smoke templates moved verbatim to
  `templates/smoke.py`. Shared `templates/_doc.py` Doc-ops model renders the 3 report templates to
  BOTH docx and pdf from one description (no per-format duplication). Commit f57d1fe.
- **Templates (all provenance-enforced, every load-bearing number from an attached tool result):**
  - `design_review` (make_presentation/pptx): title | views (embedded renders, missing→caption) |
    massprops | fea_margins | risks.
  - `calc_package` (make_report/docx|pdf): assumptions | material (props + Pillar-8 source+fetched_ts
    citation) | equation (ALL slots load-bearing) | results_table.
  - `bom_workbook` (make_workbook/xlsx): one sheet per assembly level; fixed cols
    Item|Qty|UnitCost|ExtCost|Source; Qty+UnitCost load-bearing; live `=B*C` + `=SUM` rollup.
  - `verification_report` (make_report/docx|pdf): requirements→evidence matrix; measured/limit/margin
    load-bearing.
  - `assembly_manual` (make_report/docx|pdf): section (manual prose) | step (render + torque/
    fastener callouts load-bearing).
- **joins.py:** provenance join layer — attach()/ref() + describe_part/FEA/material/CAM/drawing
  normalizers + `bom_rollup()` (emits qty/unit_cost as attached results + live formulas; graceful
  without WS-B sourcing). Pure data-shaping, no engine imports.
- **spec.py (backward-compat internal):** preserve model-authored template-defined block fields
  through validation (images/expr/verdict/req_id survive); support all-slots-load-bearing block
  types (equation). Existing T-F14 suite still 20/20 green.
- **guide.py:** cad_guide('deliverables') now catalogs each of the 5 spec schemas + a worked
  example (DoD). Commit 5991980.
- **Verification so far:** Opus smoke of all 5 templates end-to-end via joins PASS (resolved refs
  6/6/5/4/2; numerals present in files; negative provenance_violation per load-bearing slot).
  Golden-file pytest suite (test_templates.py) delegated to a Sonnet subagent; gate + graphify to
  follow. NO new dependencies (all engines already in contract §7 / licenses.md).

- **DoD verified (T-F15 review-ready):** golden-file suite `services/deliverables/test_templates.py`
  (12 tests) + existing suite = **32/32 green** in-process (no pandoc/LibreOffice). `graphify
  update .` OK (19361 nodes). `bash scripts/gate.sh`: core build PASS, ui/abstract PASS, ctest 100%
  PASS; the three RED stages are all PRE-EXISTING and untouched by this task — check.py
  feature_surface/parity_audit FAIL-RECORDED, engine/doc `No module named 'OCP'` pytest collection
  error (unprovisioned OCP runtime), ui/imgui `PanelField::selMulti` build break. No file under
  ui/ or engine/ was modified. Commits: f57d1fe (package+joins), 5991980 (guide+question),
  ece1b89 (arc), 17e826b (tests). Status → review; branch pushed. Next: F16 branches FROM this branch.
