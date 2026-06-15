# GUI Sizing Tooling — linter + metrics

> Companion to [GUI_STYLE_GUIDE.md](GUI_STYLE_GUIDE.md). Two pieces of machine-enforcement so the
> sizing standard is checkable, the backlog is enumerable, and dimensions live in one place:
> `scripts/gui_lint.py` (the "typechecker") and `host/src/ui/metrics.h` (the central tokens).
> A third audit script, `scripts/feature_surface_audit.py`, checks that every visible ribbon command
> routes to a real handler or form.

## 0. `scripts/check.py` — one-command standards gate

Runs all three deterministic source-level gates and exits non-zero if any fail. This is the
pre-commit / CI entry point (stdlib-only — no engine/OCCT/venv needed, runs in ~1s):

```
python scripts/check.py
```
It runs `gui_lint --rule all` + `feature_surface_audit` + `parity_audit`. The engine/agent pytest
suites are **not** part of this gate (they need the runtime venv); run them separately:
`<venv>/python -X utf8 -m pytest engine/doc agent -q`.

**Install as a git pre-commit hook** (optional — copy the sample, or point git at it):
```
cp scripts/hooks/pre-commit .git/hooks/pre-commit      # or: git config core.hooksPath scripts/hooks
```

## 1. `scripts/gui_lint.py` — the sizing/icon/theme linter

Stdlib-only Python. Catches the exact bug classes the 2026-06 GUI audit found.

```
python scripts/gui_lint.py                 # lint host/src (default), text output
python scripts/gui_lint.py --rule icons    # one rule: icons | theme | scale | all
python scripts/gui_lint.py --report md     # markdown report (docs / PR comment)
python scripts/gui_lint.py --update-baseline scripts/gui_lint_baseline.json
python scripts/gui_lint.py --baseline scripts/gui_lint_baseline.json   # ignore known findings
```
Exit code: `0` clean (after baseline), `1` violations, `2` bad invocation. CI/pre-commit friendly.

### Rules

| rule | what it catches | precision |
|---|---|---|
| **icons** | every `getIcon/treeIcon/asmIcon/hasIcon("name")` and every `ribbon_def.cpp` row icon field that names an icon NOT in `host/assets/icons/atlas.json` (the bug that rendered OK/Cancel as a magenta "?") | **deterministic** — trust it |
| **theme** | `applyTheme(..., 1.0f)` literal scale (collapses padding to 100% on HiDPI + clobbers `ui::uiScale`); any `io.FontGlobalScale =` (runtime font blur) | **deterministic** |
| **scale** | raw pixel literals passed to ImGui geometry calls (`ImVec2`, `SameLine`, `Indent`, `SetNextItemWidth`, `AddLine` thickness, `AddRect*` rounding, …) that are NOT `* uiScale`/`px()`/`U()` and not derived from a pre-scaled metric (`CalcTextSize`/`GetFrameHeight`/`GetStyle()`/…) | **heuristic** — triage (see below) |
| **scale-ws** | the same scale check, but for files in `SCALE_WORLDSPACE_FILES` (sketch/ECAD/drawing canvases drawn in world coords + the base `ui_theme.cpp` style). Reported for audit but **non-gating** — these literals are world-space art, not DPI chrome. | informational |

As of 2026-06-14 the chrome `scale` debt is **0** (all screen-space panels swept); the ~225 remaining
findings are `scale-ws` (world-space art) and do not fail the gate. To add a file to the world-space
allowlist, edit `SCALE_WORLDSPACE_FILES` in `gui_lint.py`; to fix a single mis-flagged art literal in
any file, append `// gui-lint: allow` to its line.

### The `scale` rule needs triage (important)

The `scale` rule is a regex heuristic. It flags ~every unscaled numeric in a geometry call, which
means **world/sheet/model-space coordinates get flagged even though they are correct** (they are not
screen pixels and must NOT be `* uiScale`). Distribution at the time of writing (517 findings):

- **World/sheet-space drawing — mostly NOT real violations, suppress per-case:**
  `sketch_mode.cpp` (157), `drawing_panel.cpp` (81), `ecad/ecad_canvas.cpp` (37),
  `viewport_panel.cpp` (36, HUD is chrome / gizmos are world), `section_tool.cpp` (11).
- **Chrome / panels — mostly REAL DPI-sizing violations, fix → `metrics`/`* uiScale`:**
  `settings_modal.cpp` (29), `widgets/ribbon.cpp` (21), `asm_panels.cpp` (21),
  `view_selector.cpp` (17), `doc_tabs.cpp` (16), `ui_theme.cpp` (10), `status_bar.cpp` (4),
  `sim_panel.cpp` (4), `activity_panel.cpp` (4), `mouse_gestures.cpp` (3), `evaluate_dialogs.cpp` (2).
  (`title_bar.cpp`, `left_pane.cpp`, `property_manager.cpp`, `config_panel.cpp` are part of the
  in-flight layout rework — fix them as that work lands, not in parallel.)

**Suppress a legitimate world/sheet-space literal** with a trailing comment:
```cpp
dl->AddLine(a, b, col, 2.0f);   // gui-lint: allow world-space sheet stroke
```

### Baseline / ratchet workflow

Once the chrome files are triaged and fixed, snapshot whatever remains (the accepted world-space
set) into a baseline so CI fails on **new** violations without drowning in the accepted ones:
```
python scripts/gui_lint.py --update-baseline scripts/gui_lint_baseline.json
```
The baseline keys on `(file, rule, message)` — stable across line moves — so editing a file does not
spuriously invalidate it. Burn the baseline down over time; never let it grow.

> Note: do not commit a baseline taken from a tree with uncommitted work in flight — it will bake in
> transient findings. Generate it after the layout rework has landed and the chrome sweep is done.

## 2. `host/src/ui/metrics.h` — central sizing tokens

The single place GUI dimensions are defined, so they can be **changed/recalculated in one spot** and
so the linter recognizes `px()`/`U()` as proper scaling. All accessors return DPI-scaled px.

```cpp
#include "ui/metrics.h"
ImGui::Indent(ui::metrics::treeIndent());          // 14px * uiScale
ImGui::SameLine(0, ui::metrics::U(1));             // one 4px grid unit
dl->AddLine(a, b, col, ui::metrics::stroke(2));    // hairline-safe, never < 1px
float lblCol = ui::metrics::pmLabelCol();          // PM two-column label width
```

Primitives: `scale()`, `px(logical)`, `U(n=1)`, `stroke(t)`, `rounding(r)`. Named tokens for icon
tiers, tree, PM, ribbon, bars, and dock targets — see the header. **Do not** wrap a value already
derived from `CalcTextSize`/`GetFrameHeight`/`GetStyle()` in `px()` (those are already scaled —
double-scaling is the classic bug).

The fix sweep routes raw literals through these tokens (or `px()`/`U()`), so a future re-spacing is a
one-file edit, not a 25-file hunt.

## 3. Recommended wiring (future)

- **Pre-commit / CI:** run `python scripts/gui_lint.py --rule icons --rule theme` as a hard gate
  (deterministic, zero false positives), and `--rule scale --baseline …` as a ratchet.
- **Feature-surface gate:** run `python scripts/feature_surface_audit.py`; any non-stretch ribbon
  command that would fall through to the generic "no PM form yet" log is a release blocker.
- **Startup assert (debug):** a `validateIconNames()` that checks the static icon tables against the
  atlas at launch, so a bad name trips in dev (complements the linter's static check). See
  GUI_AUDIT_FINDINGS §5.4.
