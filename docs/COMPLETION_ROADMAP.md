# AI-CAD Completion Roadmap

> The throughline from "where we are" to "done." Three chapters, driven by per-chapter workflows and
> subagents. Living tracker — update as chapters land. (2026-06-14)

## The three chapters

1. **GUI overhaul** — make the app professional + SolidWorks-faithful.
2. **SolidWorks-parity standards & testing** — define the feature/acceptance conditions and the
   machine-checked standards we build to.
3. **Implementation** — close every gap the first two chapters surface, to the standard they set.

These are not strictly sequential: Chapter 2 produces the *definition of done* that Chapter 3
executes against, and Chapter 1 is both its own deliverable and a worked example of the standards
(`GUI_STYLE_GUIDE.md` + `gui_lint.py`).

---

## Chapter 1 — GUI overhaul

| piece | state | artifacts |
|---|---|---|
| Foundation (named ribbon-tab + left-pane enums, unified dispatcher, PCB index fix) | ✅ `abd60ec` | |
| Design-system docs (standards + audit) | ✅ `513d019` | `GUI_STYLE_GUIDE.md`, `GUI_AUDIT_FINDINGS.md` |
| User-visible fixes (OK/Cancel icons, stepper overflow, settings scale, tree DPI, dock clamp) | ✅ `38013de`,`dd54040` | |
| Sizing tooling (linter + metrics + tooling doc) | ✅ `d015fcc` | `scripts/gui_lint.py`, `host/src/ui/metrics.h`, `GUI_TOOLING.md` |
| Sizing fix plan (curated triage) | ✅ `53035f3` | `GUI_SIZING_FIXPLAN.md` |
| **Chrome sizing sweep (~120 fixes)** | ⏳ queued — needs stable tree | per `GUI_SIZING_FIXPLAN.md`; metrics tokens staged |
| **Tabbed left pane + Waves 2-5 (ECAD tabs, file explorer, copy/paste, menus)** | 🔒 parallel agent in-flight | `GUI_REWORK_PLAN.md`, `left_pane.cpp/.h` |
| Better assets + animations | ⬜ TODO | `host/assets/make_icons.py` (atlas regen), motion polish |

## Chapter 2 — SolidWorks-parity standards & testing  ← **active**

| piece | state | artifacts |
|---|---|---|
| Parity catalog → spec → gap backlog (12-area workflow) | ⏳ running (`sw-parity-standards`) | → `SW_PARITY_SPEC.md`, `SW_PARITY_GAPS.md` |
| Parity audit harness | ⏳ in workflow | → `scripts/parity_audit.py` |
| Feature-surface audit (ribbon → handler) | ✅ parallel agent | `scripts/feature_surface_audit.py` |
| GUI sizing typechecker | ✅ | `scripts/gui_lint.py` (icons+theme deterministic) |
| Live SolidWorks observation (computer-use) | ⬜ deferred — would hijack the user's input devices while the parallel agent runs; do when the machine is free |

**Standards posture:** every standard ships with a *checker* so it holds the first time
(`gui_lint`, `feature_surface_audit`, `parity_audit`). The deterministic rules (icon names, theme
scale, feature-surface) become CI gates; heuristic rules (sizing) ratchet via a baseline.

## Chapter 3 — Implementation

Driven by `SW_PARITY_GAPS.md` once it lands. Plan: one **per-area workflow** (find → implement →
verify) per P0/P1 gap cluster, opus where judgment-heavy. Gated on a **stable, buildable tree** (the
parallel agent's host/engine work committed) so builds aren't entangled. Known large items already
scoped: Spline-on-Surface (engine projection infra), bulletproof cross-part copy/paste (GUI Wave 4).

---

## Coordination (two agents, one working tree)

A parallel "slave-driver" agent is working the **layout rework + a managed llama.cpp provider +
audit tooling** in the same tree (uncommitted: `left_pane.*`, `app.cpp`, `title_bar.cpp`,
`property_manager.*`, `config_panel.*`, `feature_tree.*`, `app_state.*`, `pm_forms.cpp`,
`settings_modal.cpp`, `CMakeLists.txt`, `agent/providers/llamacpp_provider.py`,
`installer/llamacpp.py`, `engine/paths.py`, `agent/config.py`, and it co-edits `gui_lint.py` +
shared docs).

**My rules while it runs:** read-only across its files; commit only NEW / solely-mine files
(`scripts/gui_lint.py` superseded by its co-edits, `metrics.h`, `docs/*` I author); **no host/engine
build** (shared build dir); defer build-dependent execution (the chrome sweep, Chapter 3) until the
tree is committed/stable. Work that is always safe: read-only analysis, new docs, new scripts.

## Sequencing logic

1. **Now:** Chapter 2 (collision-free standards/audit) ▸ this is the active workflow.
2. **On tree-stable:** execute the GUI chrome sizing sweep (Chapter 1 tail) + start Chapter 3 from
   the gap backlog, per-area workflows, build+verify+commit each.
3. **Continuous:** keep the checkers green; ratchet the sizing baseline down; regenerate assets.
