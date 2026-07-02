# HANDOFF — GUI overhaul + AI-experience merge (session 2026-07-01/02)

For the next agent (or Erik auditing by hand). Companion to `docs/HANDOFF_AI_EXPERIENCE.md`
(that lane's TODOs 1–3 + 5 are **DONE**, see §4 below; its §TODO-4 was retargeted here).
Everything in this session is **UNCOMMITTED** on `master` unless stated. Swarm rules in
`AGENTS.md` apply (disjoint write-sets, leaf agents never git, REFACTOR_LOG paragraphs).

## 0. TL;DR state

| Lane | State | Verify status |
|---|---|---|
| Glass core API (`ui_theme`, `app_state`) | DONE, on disk | compiles green (verified 2026-07-01 22:47) |
| GUI styling wave (9 agents, `host/src`) | DONE, on disk | build green + smoke; visual fix round 2 was in flight at handoff time |
| E3 in-app AI hooks (`host/`) | workflow phase after verify — CHECK §2 | — |
| Settings reorg + dead-code removal | workflow phases after E3 — CHECK §2 | — |
| AI-experience lane (recon/tests/eval/gate) | **DONE**, on disk | gate.sh/ctest/pytest run by gate node; REFACTOR_LOG paragraph appended |
| Scoped commits | **NOT DONE** — see §6 | — |

## 1. Locked design decisions (do not re-litigate; also in agent memory)

1. Viewport spans full width BETWEEN solid ribbon (top) and solid status bar (bottom);
   doc tabs, HUD, FeatureManager, Copilot float over it.
2. FeatureManager (left) + Copilot (right) = PINNED full-height rounded liquid-glass
   columns, ~12dp margins, resize via invisible edge hit-zones, NO visible splitter bars.
3. dynamicColor (auto max-contrast glass tint + text vs live backdrop) defaults **ON**;
   Settings → Appearance pins fixed per-group tints (global/featureManager/copilot/hud).
4. Docked layout DELETED outright — no `panelsOverlay` setting, no fallback toggle.
5. (Orchestrator source-of-truth call, user-delegated) HANDOFF_AI_EXPERIENCE §TODO-4 E3
   hooks land in **host/ only**; the ui/imgui (`app/`) mirror is intentionally skipped.

## 2. The workflow (monolithic DAG) — how to resume / audit

- Script: `/home/hunter/.claude/projects/-home-hunter-Repositories-CAD-host-build-linux-bin/fe85a258-9ed8-48d7-bb47-639f5b1059cb/workflows/scripts/gui-overhaul-dag-wf_b02e1448-8d5.js`
- Run ID `wf_b02e1448-8d5`; transcripts (incl. FULL audit JSONs, per-agent) in
  `/home/hunter/.claude/projects/-home-hunter-Repositories-CAD/fe85a258-9ed8-48d7-bb47-639f5b1059cb/subagents/workflows/wf_b02e1448-8d5/`
- Phases: Audit(7) → Core API → Styling(9, disjoint ownership) → Integrate → Build 1 →
  Verify (screenshot rounds ×3 w/ fix agents) → E3 hooks → Reorg (settings + dead code)
  → Build 2 → Final shots. Parallel branch: Recon lane (done).
- If it died mid-run: resume with
  `Workflow({scriptPath: <script above>, resumeFromRunId: "wf_b02e1448-8d5"})` —
  completed agents replay from cache; only in-flight/edited ones re-run.
- **Where did it stop?** `ls /tmp/claude-1000/-home-hunter-Repositories-CAD/fe85a258-9ed8-48d7-bb47-639f5b1059cb/scratchpad/`:
  `final_*.png` present ⇒ everything ran. `smoke_e3.png` ⇒ E3 done. Only `r2_*/fx_r2_*` ⇒
  stopped in verify; E3/Reorg/dead-code DID NOT run — they are open TODOs.
  Also `grep -c panelsOverlay -r host/src` must be 0 (decision 4 enforcement).

## 3. GUI lane — what landed (audit-verified highlights) and what to audit

Rebuild + look: `cmake --build host/build-linux && cd host/build-linux/bin &&
./AI-CAD --ui-screenshot /tmp/shot.png` (scenarios: `AI_CAD_SHOT=m1|sketch|fea|appearance|drawing|asm|multidoc`).
Screenshots for before/after audit: durable copies in `scratch/gui-overhaul-shots/`
(gitignored): `r2_*` = verify round 2, `fx2_*` = post-fix; session scratchpad
(`/tmp/claude-1000/-home-hunter-Repositories-CAD/fe85a258-9ed8-48d7-bb47-639f5b1059cb/scratchpad/`,
dies with the session) has ~190 more incl. reviewer crops (`rv_*`, `adv_*`, `crop_*`).
NOTE: the OLD-app `base_*.png` baselines were deleted by a review agent; to regenerate the
"before" look, build the pre-overhaul tree at commit `5bffe9d` (e.g. stash the working
tree, build, `--ui-screenshot`, unstash) — or trust the written audit descriptions.

Landed (spot-check each):
- Colored adaptive glass API + `AppearanceSettings` (config.json `"appearance"`), per-group
  styles, dynamic color. Files: `host/src/ui_theme.{h,cpp}`, `host/src/core/app_state.{h,cpp}`.
- Overlay layout: full-bleed viewport, pinned glass columns, splitters invisible, docked
  path deleted (`host/src/app.cpp`, `ui/layout.cpp`, `ui/feature_tree.cpp`, `ui/legacy_chat.cpp`).
  KNOWN TRAP fixed(?): `layout.cpp:243-244` used to force `resetLayout` when LeftPane
  undocked — verify it's gone.
- Settings → Appearance tab + `AI_CAD_SHOT=appearance` scenario (`ui/settings_modal.cpp`,
  `ui/scenarios.cpp`).
- Contrast fixes per audit: sketch ink was fixed near-black `sketchInk` on a live render
  (P0 — the actual "black on black"); HUD idle fade 0.20 washed icons out; view-selector
  dark lines over dark models; sim chart hardcoded IM_COL32 text. All were assigned; the
  full 16-finding list w/ file:line is audit `viewport-text` in the run transcripts.
- Widget modernization (sliders/checkboxes/label columns/uiScale) across PM forms,
  dialogs, sim forms, ribbon spacing, SDF icon quality.

## 4. AI-experience lane — DONE (audit summary)

New files: `services/mcp/recon_tools.py` (+ registration in `server.py`),
`services/agent/prompts/shared/cheatsheets/reverse_engineer.md` (`_RE_GUIDE`),
`services/mcp/test_gsl_tools.py` (9), `test_recon_tools.py` (4), +5 appended in
`test_cad_tools.py`, `tools/gsl_eval.py`, `docs/RECOGNITION_RESIDUAL.md`.
- Acceptance (spec §TODO-1) passed: plate ≥97% w/ hole+pattern recovered, editability
  proof, shaft ≥95%, honest STL smoke. Re-run:
  `python3 -m pytest services/mcp/test_gsl_tools.py services/mcp/test_recon_tools.py -q`
- Eval verdict (GNN go/no-go, `docs/RECOGNITION_RESIDUAL.md`): adapter justified ONLY for
  ribbed_bracket / shelled_enclosure / swept_pipe; deterministic sufficient elsewhere.
- Lane's own handoff paragraph + gotchas: tail of `docs/REFACTOR_LOG.md` (pytest package-
  shadowing shim, hole-placement probe, fillet bound uses MEDIAN bbox dim, restage
  `services/dist/AI-CAD/libaicad.so` after any core rebuild).

## 5. NOT YET IMPLEMENTED — productive backlog (in priority order)

1. **[user-requested, queued as session Task #1] FeatureManager tab strip → top.** Move the
   vertical icon-tab buttons (Explorer/FeatureManager/PropertyManager/Configurations/
   Simulation, `ui::drawLeftPane` in `host/src/ui/layout.cpp`) from the bubble's left edge
   to a horizontal icon row across the bubble TOP; tab bodies then get full bubble width
   (kills the left gutter dead space). Verify with `AI_CAD_SHOT=m1` shot.
2. **E3 hooks / settings reorg / dead-code removal IF the run died before them** (§2 tells
   you how to check). Their full specs live in the workflow script phases `E3 hooks`,
   `Reorg` — decision-complete, execute as written (E3: import-summary toast,
   "Reverse-engineer import" copilot chip gated on an import feature in the tree,
   `ai_reverse_engineer`/`ai_explain_part` palette commands, host/ only).
3. **Final human-eye screenshot pass.** The DAG's reviewers are good but low-res; open the
   app live, exercise: sketch dims over a dark part, FEA legend, Appearance tab live-apply,
   bubble edge-resize hit-zones, collapse-to-pill (if the wave shipped it — audit said "if
   cheap"), bubble width persistence (may have been deferred — grep REFACTOR_LOG + the
   integrator summary for "defer").
4. **Interactive input verification.** Headless shots can't click: invisible resize zones,
   PM slider ctrl-click exact entry, chip firing the agent run. 10-minute manual pass.
5. **Windows/MSVC build check.** All agents were instructed Windows-safe, but nothing here
   compiles MSVC. Known prior trap: `host/extern/fix_math.h` `/FI` wiring (see agent memory
   `windows-linux-gui-parity`). Have the Windows machine build before calling parity.
6. **Audit P2 leftovers.** Each audit capped at 45 findings; P2s (triad dot labels on raw
   overlay roles, legend captions on pre-v2 glassBackground, toast level-tag contrast,
   hardcoded sim/flow/CAM overlay literals) may have been skipped under time pressure —
   full lists in the run transcript dir (§2), grep the audit agents' `agent-*.jsonl`.
7. **Settings audit follow-ups** (from audit `settings`, full text in transcripts): the AI
   provider/model picker is drawn in THREE places (modal AI tab, AI dock tab, composer
   popover) sharing `drawProviderModelPicker` — reorg was told to unify entry points; the
   composer "Permissions" popup checkboxes didn't SAVE (fixed?); engine-routing env flags
   are read-only UI in General. Verify the reorg actually consolidated these.
8. **GNN adapter training** for the 3 GO families — gated on `tools/gsl_gnn/README.md`
   procedure; NOT started, needs `docs/RECOGNITION_RESIDUAL.md` (exists now).
9. **`corpora/sw_fixtures/*.step`** absent → the SW-fixture eval path is dormant. Export
   STEPs from the SLDPRT fixtures to activate it (`tools/gsl_eval.py` picks them up).
10. **Firecrawl**: no `FIRECRAWL_API_KEY` in env; `tools/scrape_knowledge.py` falls back to
    urllib. Set the key to use Firecrawl for future knowledge scraping.
11. **Idea (unassigned): per-document glass accent.** With colored glass landed, a cheap
    win is tinting each doc tab's pill + its bubbles' accent from a per-document hue so
    multi-doc sessions read at a glance. API supports it (per-group styles); needs a
    per-doc hue field + Appearance toggle.
12. **Idea (unassigned): luminance-variance scrim boost.** `adaptiveSurface` uses mean
    luminance; busy high-contrast backdrops (FEA fringes) still fight text. The glass-api
    audit proposed adding variance sampling to auto-raise scrim alpha — check if the core
    agent shipped it (grep `variance` in `ui_theme.cpp`); if not it's a small follow-up.

## 6. Commit plan (approved by user: commit when gates pass; agents never git)

Preconditions: build green, smoke shot OK, `AI_CAD_SHOT=m1|sketch|fea|appearance` shots
eyeballed, recon pytest green, `scripts/gate.sh` (feature_surface_audit + parity_audit
failures are PRE-EXISTING baseline).
- Commit A (GUI lane): `host/src/**` + `docs/HANDOFF_GUI_OVERHAUL.md`.
- Commit B (AI-experience lane): `services/mcp/{recon_tools.py,server.py,test_*.py}`,
  `services/agent/prompts/shared/cheatsheets/reverse_engineer.md`, `tools/gsl_eval.py`,
  `docs/RECOGNITION_RESIDUAL.md`, `docs/GEOMETRY_LLM_REPRESENTATION_REVIEW.md` header note.
- **NEVER scoop the OTHER lane's dirty files**: `ui/abstract/panel_spec.*`,
  `services/mcp/cad_tools.py`, `README.md`, `docs/REFACTOR_LOG.md` (append-only; leave
  uncommitted for that lane), `docs/HANDOFF_AI_EXPERIENCE.md`.
- Append a GUI-lane paragraph to `docs/REFACTOR_LOG.md` before committing (don't commit it).

## 7. Session gotchas (new this session)

- Subagent session-limit deaths mid-wave are RECOVERABLE: the workflow journal caches
  completed agents; stop → resume re-runs only the lost ones. Check `git diff --stat`
  before resuming to see what half-landed.
- `host/build-linux` ninja must not run concurrently from two agents — the DAG serializes
  builds into dedicated phases; keep it that way when editing the script.
- The `--ui-screenshot` + `AI_CAD_SHOT` harness works headless on this box and is the
  fastest GUI verification loop; reviewers can Read the PNGs directly.
- Scratchpad is session-scoped tmp — screenshots there do NOT survive; copy anything worth
  keeping into the repo or re-capture.
