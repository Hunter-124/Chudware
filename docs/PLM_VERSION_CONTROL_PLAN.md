# PLM Version Control Plan (Pillar 19, Workstream R)

Status: DESIGN APPROVED FOR TASK CUT — Hunter directive 2026-07-03.
Supersedes backlog proposals T-BL15 (in-app document version control) and
T-BL10 (PDM-lite). This document answers T-BL15's open questions and is the
source of truth for workstream R. Detailed schemas freeze in `contracts/plm.md`
(T-R1) — this doc fixes intent, UX model, and architecture direction.

> **T-R1 CONTRACT FROZEN (2026-07-03).** The detailed schemas are now frozen in
> [`contracts/plm.md`](../contracts/plm.md) (`aicad.plm/1`). The OSS survey +
> git-binding verdict live in [`docs/PLM_OSS_SURVEY.md`](PLM_OSS_SURVEY.md):
> **git binding = dulwich (Apache-2.0 arm) primary, subprocess `git` optional
> accelerator, pygit2 declined** (lightweight + cross-platform + Fedora/Windows
> parity, no per-platform native wheel, in-process-clean under the guardrails).
> §3.1 below stands; this note records the resolved binding.

## 0. Design principles (ordered — #1 wins conflicts)

1. **Ease of use is the #1 priority.** Users must never have to *learn* version
   control. History happens automatically and safely; the user makes decisions
   only at moments that already feel natural (naming a milestone, releasing a
   design). Anything that needs a manual to understand is out of v1 or behind
   an Advanced disclosure. Zero configuration on day one: a project is a
   folder, the vault is built in, there is nothing to set up.
2. **Manual controls first, AI assist behind them.** The buttons are the
   primary interface. AI (Copilot) can draft, suggest, and — if the user turns
   it on — manage versioning automatically, but every AI action is visible,
   attributed, and reversible (Pillar-14 transparency rule; autonomy dial
   precedent). AI-managed mode is an assist tier, never an exclusive mode.
3. **Local-first, lightweight.** Full functionality with no account, no server,
   no network. A project's entire history lives in a hidden per-project store.
   Self-hosting = pointing a project at any git remote the user controls.
   A company-hosted (SaaS) remote is the same seam, later, behind the
   Pillar-12 human gate.
4. **One obvious structure.** The tool imposes the layout; users never design
   a vault. No configurable lifecycle state machines, no admin screens, no
   check-in policies to author.
5. **Teach by showing.** The history timeline reads like a story: rendered
   thumbnails + plain-language change summaries (semantic `doc_diff` +
   narration layer), never hashes or raw diffs.

## 1. What the user sees (the whole mental model)

Three nouns, two verbs. Nothing else in v1.

- **Project** — a folder that groups related documents (parts, assemblies,
  drawings, ECAD, CAM jobs, requirements, deliverables). Opening the folder
  opens the project. NEW concept: today docs are independent
  (`host/src/ui/doc_tabs.*` has tabs but no grouping).
- **History** — automatic. Every explicit save silently records a restorable
  state. Baseline promise: *"you can always go back."* No init, no staging,
  no tracked-file decisions. (Autosave keeps its existing separate
  crash-recovery ring from T-Q2 — history entries are cut on explicit save,
  so history stays meaningful, not noisy.)
- **Checkpoint** — *"this state matters."* One button, one optional text box.
  Copilot pre-drafts the description from the semantic diff ("Increased wall
  3→5 mm, added 4× M6 bolt circle"); the user can edit or ignore it.
- **Release** — *"this revision is what we build."* Locks that revision,
  stamps the rev letter (A, B, C…) on the drawing title block, freezes the
  BOM, pins the assembly's component references. Lifecycle is fixed and
  minimal: **In work → Released → Superseded**. Releasing rev B automatically
  supersedes rev A. No custom states.
- **Restore / Compare** — restore any history entry (restore is itself a new
  history entry — history is never rewritten, so restore is always safe);
  compare any two states side-by-side in 3D (`eval_compare`) plus a
  plain-language change list (`doc_diff`).

Explicitly **not** user-facing in v1: branches, merges, staging, conflicts,
locks administration. Variants are served by the existing configurations
feature (`contracts/doc_schema.md §9`) and copy-as-new-item, not by branching.

## 2. AI-managed mode

Default ships as **AI-assisted** (suggestions only); user can dial up to
**AI-managed** or down to **manual-only** per project:

- **Manual-only:** buttons only. Auto-history still runs (that's safety, not
  policy).
- **AI-assisted (default):** Copilot drafts checkpoint/release descriptions,
  suggests when a checkpoint is worth cutting (feature-count/time/magnitude
  heuristics + `doc_diff`), and flags stale references (assembly using a
  superseded part rev).
- **AI-managed:** Copilot additionally cuts checkpoints itself at sensible
  boundaries (end of a copilot task, before a risky operation, after a big
  edit session) and keeps descriptions current. Every AI-cut entry is labeled
  with the AI attribution mark in the timeline and is one click to discard.

All three tiers use the same seam: `plm_*` MCP tools (T-R2), so the built-in
copilot and external MCP clients get identical capability. AI actions route
through the existing telemetry envelope (T-H1) like any other AI artifact.

## 3. Architecture

### 3.1 Storage: git as a hidden engine

Decision: **snapshot-based versioning on the canonical serialization, stored
in a per-project git repository the user never sees.** Not op-log replay:
`contracts/doc_ops.md` ops are transient (no persistent op log; geometry is
derived; sessions carry state), while T-H2 gave every doc kind a canonical,
diff-stable JSON form — snapshots are already cheap and semantic diff already
exists. Op-log capture can be layered later for finer-grained history without
changing the user model.

Why git (hidden): content-addressed dedup + delta compression on canonical
JSON is excellent; sync to *any* remote gives self-hosting for free; battle-
tested; zero server required locally. The user-facing vocabulary never says
git. T-R1's OSS survey picks the binding (subprocess git vs pygit2/libgit2 vs
dulwich — CONTRIBUTING.md license guardrails apply) and surveys prior art
(Ondsel/FreeCAD PDM attempts, DVC, git-LFS locking) before code.

### 3.2 Project layout

```
MyProject/                     # what the user sees: a normal folder
  Bracket.step+                # working files, exactly as today
  MainAssembly.step+
  Controller.ecad+
  .aicadvault/                 # hidden: git repo (shadow tree, below)
```

Shadow tree committed to the vault (per history entry):

```
manifest.json                  # aicad.plm/1: item registry, part numbers,
                               # rev letters, lifecycle, release pins, BOM freezes
items/<item_id>/document.json  # canonical parametric JSON (exploded from container)
items/<item_id>/meta.json
items/<item_id>/thumbnail.png
items/<item_id>/geometry.step  # ONLY at releases (derived data; recomputable)
items/<item_id>/summary.txt    # plain-language change summary (doc_diff + narration)
```

Containers (`.step+`/`.ecad+` are ZIPs — git-hostile) are **exploded on
check-in, re-assembled on restore**. Checkpoints store parametric JSON +
thumbnail only (tiny); releases additionally freeze the geometry snapshot and
BOM so a released rev is exact even if the rebuild engine evolves.

### 3.3 Sharing spectrum (one mechanism end to end)

| Tier | What it is | v1? |
|---|---|---|
| Local | per-project hidden vault, no network | yes (T-R2) |
| Shared folder / USB | vault sync to a filesystem remote | yes (T-R7) |
| Self-hosted | any git remote the user controls (bare repo over SSH, Gitea, GitHub…) | yes (T-R7) |
| AI-CAD Cloud | our hosted remote + accounts + web view | seam only (config entry), Pillar-12 human gate before any implementation |

Team concurrency v1 = **advisory edit locks + sync, no merging** (merging
feature trees is where every PLM gets complicated). "Sync" is one button:
pull others' entries, push yours. If two people edited the same item anyway,
the loser's state is preserved as a copy-as-new-item ("Bracket (Sarah's
edits)") — never a merge UI, never data loss. Locks live in a small manifest
at the remote updated by atomic push (push-rejection as compare-and-swap);
UI shows "Sarah is editing this part."

### 3.4 Items, part numbers, BOM

`manifest.json` carries an item registry: stable item_id per document,
auto-assigned human part numbers (`PRJ-0001`, editable), rev letters
incremented at release. Assemblies reference items; at release the reference
set is pinned to exact revs (BOM freeze snapshot, feeding the existing
`bom_rollup` machinery). A where-used index (which assemblies/drawings/CAM
jobs consume this item) is derived from the manifest and powers stale-ref
flags and the Project Hub explorer.

### 3.5 Relationship to existing state (T-BL15's open questions, answered)

- **Undo/redo** (`doc_undo`/`doc_redo`): session-scoped, unchanged. History
  persists across sessions; undo does not. The history panel makes the
  distinction visible ("Session changes" vs "Project history").
- **Session `revision` counter / `doc_checkpoint` RPC:** transient rebuild
  plumbing; not reused as storage. Vault snapshots serialize via the T-H2
  canonical writers.
- **DAG orchestrator checkpoint store:** separate concern (agent-run state,
  not user-doc history). They share the canonical serialization contract,
  nothing else.
- **Autosave (T-Q2):** unchanged as the crash-recovery ring; history entries
  are cut on explicit save so the timeline stays curated. Recovery flow can
  offer "restore from history" alongside "recover autosave."
- **Configurations:** remain the variants mechanism; PLM versions the whole
  document including its configurations.

## 4. GUI surfaces

Two integration levels, both on the T-U1 style kit:

**In every editor (T-R5)** — part studio, assembly, drawing, ECAD
schematic/PCB, CAM workspace, and the U7 viewers (piping/hydraulic/
structural):
- **History panel:** vertical timeline — thumbnail, when, who (user/AI),
  plain-language summary; hover = larger preview; right-click = restore /
  compare / checkpoint-from-here.
- **Doc-tab revision badge:** current rev letter + lifecycle chip (e.g. "B ·
  In work"); released docs open read-only with an obvious "start rev C" action.
- **Checkpoint / Release buttons** in the main toolbar with the Copilot-
  drafted message inline.
- **Compare view:** side-by-side 3D (`eval_compare` overlays) + change list;
  drawings/ECAD compare as overlaid 2D.

**Project Hub (T-R6)** — the master GUI, a full-screen workspace (a doc-tab
kind of its own, like the CAM workspace) and the app's landing surface when a
project is open:
- item grid with live thumbnails, part numbers, rev + lifecycle chips
- project activity timeline (all items interleaved; filter by item/person/AI)
- release board (what's In work / Released / Superseded; one-click release
  flow with pre-flight checks: stale refs, failing verify battery, unsaved docs)
- where-used explorer (item → consuming assemblies/drawings/CAM jobs)
- team presence + lock indicators, Sync button (when a remote is configured)
- Copilot dock for AI-managed actions and "what changed since Tuesday?" queries

Onboarding: a guided tour (T-Q3 machinery) + auto-manual section (T-Q4) ship
with T-R6; the empty-state of the Hub teaches the three nouns in one sentence
each.

## 5. Workstream R task map

| Task | Content | Depends on |
|---|---|---|
| T-R1 | OSS survey (git binding, LFS/locks, prior CAD-PDM art) + freeze `contracts/plm.md` (aicad.plm/1 manifest, shadow-tree layout, lifecycle, lock + remote protocol, AI-assist seams) | — |
| T-R2 | Vault core: `services/plm/` — project init/open, auto-history on save, checkpoint/release/restore, exploded-container storage, `plm_*` MCP tools | T-R1 |
| T-R3 | Change intelligence: doc_diff→plain-language summaries, Copilot-drafted messages, AI-assist/AI-managed policies, compare (semantic + eval_compare visual), stale-ref detection | T-R2 |
| T-R4 | Items & releases: part numbers/rev letters, release pinning + BOM freeze, where-used index, drawing title-block rev stamp | T-R2 |
| T-R5 | In-editor GUI: history panel, rev badges, checkpoint/release toolbar, compare view — every doc kind, on the T-U1 kit | T-R2, T-R3, T-U1 |
| T-R6 | Project Hub: full-screen master workspace (grid, activity, release board, where-used, Copilot dock) + guided tour + manual section | T-R4, T-R5 |
| T-R7 | Sharing & team sync v1: remote profiles (folder/SSH/any git host), one-button Sync, advisory locks, copy-as-variant conflict guard, SaaS remote seam (config-only, Pillar-12 gate) | T-R2, T-R4 |

Sequencing intent: R1→R2 are the spine; R3/R4 parallelize after R2; R5 after
R3; R6 is the capstone; R7 can run parallel to R5/R6.

## 6. Non-goals (v1)

- Branching/merging of feature trees (variants = configurations or copy-as-item)
- Real-time co-editing of one document
- Custom lifecycle states, approval workflows, e-signatures (later, behind
  Advanced, if ever)
- ECO/change-order management
- Server implementation for AI-CAD Cloud (seam reserved only; Pillar-12 human
  gate before any build)
