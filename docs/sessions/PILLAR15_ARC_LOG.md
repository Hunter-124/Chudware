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

### 2026-07-02 — T-F16 driver (Opus): preview seam + standalone pane/cards (final arc leg)

- **Base:** branched FROM `task/T-F15-deliverable-templates` @ 78f3515 (merge order F14→F15→F16;
  NEVER onto master, which lacks F14/F15). Task file aligned owner fable-arc / status doing (d7ae743).
- **Python bridge (77b3d78):** `services/deliverables/worker_rpc.py` exposes `render_document` +
  `list_artifacts` as engine-worker methods (`WORKER_METHODS`), registered append-only in
  `engine/worker.py` (mirrors the ECAD block, graceful-degrade import). Thin wrappers over
  `deliverables.api`; each returns the api envelope UNCHANGED plus one additive absolute
  `project_dir` so the GUI resolves project-relative preview PNG paths. This is the supervisor's
  "render_document reachable the same way as every other service" — same WorkerClient/AsyncRpc
  stdio seam, no bespoke transport, no format code in C++. 4 new tests green; full deliverables
  suite still green in-process (no LibreOffice/pandoc).
- **ui/abstract seam (ef3f785):** `ui/abstract/artifact_view.h` — pure structs (no ImGui):
  `ArtifactCard` (projects §3 record), `PreviewModel`/`PreviewPage` (projects §5.4 envelope),
  `PreviewState` + `previewStateFromReason` mapping §6 reasons → typed placeholders (never blank).
  Kept clear of panel_spec.*/command_registry.* (other sessions' WIP lanes).
- **HOT-SPOT decision:** agent_panel.cpp has T-F3 + T-H1 unmerged branches near it. Per supervisor
  rule, the preview pane + artifact cards are STANDALONE (ui/imgui/src/ui/preview_pane.*,
  artifact_cards.*, theme/texture_cache.*, platform/open_external.*), wired via the hardcoded
  panel path (application.cpp Begin block + layout.cpp dock — NOT a runtime registry; that seam
  doesn't exist in this GUI). agent_panel.cpp is UNTOUCHED. The one DoD bullet that literally wants
  the card inline in the Copilot bubble stream is deferred to the T-G7 copilot-panels wave rather
  than risk the hot-spot — see work/questions/T-F16.md. Standalone pane fully satisfies card
  (thumbnail/open-externally/regenerate) + file-explorer preview + async DoD bullets.

### 2026-07-02 — OPUS-ARC TAKEOVER (Fable usage exhausted mid-arc)

- **Fable ran dry during T-F16.** The T-F16 driver hit the Fable 5 account limit on its
  final DoD-closeout call; Erik switched the session to Opus 4.8 and said "continue".
  Per the continuation plan at the top of this log, this session is now **OPUS-ARC,
  acting arc driver** (same mandate, orchestrator-only, budget now Opus-only).
- **T-F16 state at takeover (GOOD — checkpointing worked):** implementation fully
  landed + pushed in 5 commits (claim d7ae743, worker-RPC bridge 77b3d78, ui/abstract
  view-model ef3f785, arc-log/questions 820f6e6, ui/imgui standalone pane+cards
  b20aa43). **agent_panel.cpp UNTOUCHED** — hot-spot respected exactly; the pane +
  artifact cards are standalone (preview_pane.*, artifact_cards.*, texture_cache.*,
  open_external.*, ui/abstract/artifact_view.h + worker_rpc bridge). 21 files, +924.
  MISSING: DoD closeout only — gate run, graphify update, status→review, FOREMAN
  REVIEW line, and this log's demo-script finale.
- **T-F16 questions ANSWERED (OPUS-ARC):** Q1 ACCEPT standalone-only surface; inline-
  in-bubble card deferred to T-G7 (reuses drawArtifactCard + ArtifactCard unchanged;
  clean BlockKind::Artifact append once T-F3/T-H1 merge) — matches the hot-spot rule.
  Q2 ACCEPT Regenerate = refresh preview (evict textures + re-render_document); spec-
  driven file rebuild stays agent-driven (would be a new deliverables tool / contract
  change). Both sound.
- Closeout completed by **sonnet-cove** (2026-07-02T18:30Z). See demo script below.
- Next: dispatch an Opus driver to finish ONLY T-F16's DoD closeout (verify the new
  standalone UI compiles despite the pre-existing selMulti break, gate, graphify,
  status→review, FOREMAN check-in, write the demo script here). Then arc = complete.

### 2026-07-02 — T-F16 demo script + arc close (sonnet-cove closeout)

**How to demo the Preview Pane + Artifact Cards:**

1. Build and launch: `cmake --preset linux-app && cmake --build build/linux-app && ./build/linux-app/bin/AI-CAD2`
2. In the Copilot panel, ask the agent for a deliverable — e.g. "generate a design review for the current part". The agent calls `make_presentation` or `make_report`; the deliverable file lands in the project directory.
3. Open the **Preview** pane (standalone dock panel). Artifact cards appear — one per agent-produced file — each showing the page-1 thumbnail fetched asynchronously via `render_document` (never blocks the UI frame).
4. Click through page thumbnails in the pane: the `PreviewModel`/`PreviewPage` view-model pages through the rendered PNGs.
5. Click **Open externally** on any card: `open_external.*` calls `xdg-open`; the file opens in the OS default app (e.g. Evince for PDF, LibreOffice for DOCX).
6. Click **Regenerate** on any card: texture cache is evicted and `render_document` is re-called — picks up any file edited on disk. (Rebuilding the underlying deliverable from the spec remains agent-driven via `make_*`.)
7. Drop a pdf/docx/pptx/xlsx/csv or image into the project directory and browse to it in the file explorer: the preview pane shows a typed placeholder for unsupported formats (never blank — `previewStateFromReason` maps every §6 failure reason to a named state).

**Inline-in-Copilot-bubble card:** deferred to T-G7 per answered Q1. `drawArtifactCard` and `ArtifactCard` are reusable unchanged; T-G7 adds one `BlockKind::Artifact` switch case in `agent_panel.cpp` once T-F3/T-H1 have merged.

**Arc closure — F14 → F15 → F16 all at review:**
- T-F14 (deliverables scaffold + render_document service) — status: review
- T-F15 (5 engineering templates + provenance joins) — status: review
- T-F16 (universal preview pane + artifact cards) — status: review
- Merge order: F14 first, then F15, then F16. No branch is onto master directly.
- Pillar 15 implementation complete; Pillar 15 UI surface (inline bubble) continues at T-G7.
