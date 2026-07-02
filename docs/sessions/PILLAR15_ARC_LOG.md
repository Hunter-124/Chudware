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
