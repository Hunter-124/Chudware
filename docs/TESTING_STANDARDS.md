# AI-CAD Testing & Acceptance Standards

> The standard for "is this feature actually done?" — the tiered gates a change must pass and the
> per-feature definition of done. Pairs with [SW_PARITY_SPEC.md](SW_PARITY_SPEC.md) (*what* to build)
> and the checker scripts in `scripts/`. Principle: **every standard ships with a checker**, so it
> holds the first time and stays held.

## The four tiers

| tier | what | how to run | gate |
|---|---|---|---|
| **0 — Static checkers** (deterministic) | machine-verified invariants of the source | `python scripts/check.py` (runs icons + theme + feature_surface + parity) | **hard CI gate** — must be 0/clean |
| **0b — Heuristic checker** (ratcheting) | GUI sizing scale rule | `python scripts/gui_lint.py --rule scale --baseline scripts/gui_lint_baseline.json` | no NEW findings vs baseline |
| **1 — Engine tests** | pure-Python engine correctness | `<venv> -m pytest engine/doc -X utf8` · ECAD script runners `<venv> -m engine.ecad.test_*` · `engine/sim` | all green, 0 failures |
| **2 — Headless host scenarios** | the C++ app launches, renders, runs a scripted flow, exits 0 | `AI_CAD_SHOT=<scenario> dist\AI-CAD\AI-CAD.exe --ui-screenshot` (knobs: `AI_CAD_THEME` dark/light, `AI_CAD_SCALE`, `AI_CAD_AICM`, `AI_CAD_ECAD_KIND`) | exit 0; the captured shot looks right |
| **3 — Manual SW-vocabulary checklist** | a SolidWorks expert exercises the flow by name | `docs/*_CHECKLIST.md` | owner sign-off |

### The checkers (Tier 0)
- **`gui_lint.py`** — `icons` (every referenced icon exists in `atlas.json`) + `theme`
  (`applyTheme(.,1.0f)` / `FontGlobalScale` bans) are deterministic and currently CLEAN; `scale`
  (raw px literals) is heuristic and ratchets via a baseline. See [GUI_TOOLING.md](GUI_TOOLING.md).
- **`feature_surface_audit.py`** — every non-`stretch` ribbon command routes to a real handler/PM
  form (no silent "[ribbon] … no PM form yet" fall-through). The ribbon is a user promise.
- **`parity_audit.py`** — drift gate: any capability marked *done* in `SW_PARITY_SPEC.md` whose
  evidence cannot be found in code fails. Keeps the spec honest.

### Headless scenarios (Tier 2)
Driven by `AI_CAD_SHOT`. Existing scenarios (extend this list as features land — a new feature SHOULD
add one): `ribbon`, `tree`, `sketch`, `assembly`/`asm`, `m1`, `plan`, `chat`, `activity`, `fea`,
`evalmass`, `pick`, `settings`, `ecad` (+ `AI_CAD_ECAD_KIND=` pcb/3d/ai/aiboard/aiboard3d/select/sim/
saveopen/…). A scenario passing = the worker + host wiring for that flow is intact end-to-end.

---

## Definition of Done (per feature)

A feature claims *done* in the parity spec only when **all applicable** gates hold:

1. **Engine compiler** registered — C++ feature dispatch in `core/doc/document.cpp` (+ validation in
   `core/doc/validate.cpp`) with a matching `core/tests/` doctest, or (legacy) doc op in
   `engine/doc/ops.py` — the geometry/behavior actually computes.
2. **Engine test** covering the happy path + at least one edge/degenerate case (`engine/doc/test_*.py`).
3. **Worker RPC** exposed where the host needs it (`engine/worker.py` `_METHODS`), JSON-clean
   (no raw bytes — base64 binary; the worker serializes results).
4. **Command surface**: a ribbon command (`ribbon_def.cpp`) wired to a handler / PM form
   (`pm_forms.cpp` / dispatcher) — `feature_surface_audit.py` clean. No enabled placeholder commands.
5. **Headless scenario** (Tier 2) exits 0, dark + light.
6. **`parity_audit.py`** shows no drift for the capability.
7. **Checklist** entry in the relevant `docs/*_CHECKLIST.md` for the owner's manual pass.
8. **GUI**: any new UI obeys [GUI_STYLE_GUIDE.md](GUI_STYLE_GUIDE.md) — `gui_lint` clean for the
   touched files (sizing via `metrics.h`, valid icon names, correct scale plumbing).

A capability that satisfies 1–3 but not 4–5 is **partial** (engine-only); 0 of these is **missing**;
exceeding SolidWorks (e.g. native ECAD + SPICE) is **beyond**.

---

## Cadence & gates

- **Per change:** build (`build.bat host` / default = compile+sync dist) → Tier 0 checkers → Tier 1
  pytest → Tier 2 headless → commit. Hand the owner a Tier 3 checklist for the wave.
- **One build owner per build dir** (exe-lock); never two concurrent host builds in the same tree.
- **Sub-agents must not run git**; the orchestrator commits. Append-only shared files
  (`ribbon_def.cpp` kCmds, `pm_forms.cpp` kForms, `worker.py` _METHODS, `features/__init__.py`,
  `widgets/ribbon.cpp` kRibbonTabs) — never reorder/delete rows.
- **Version bumps** are additive (a new schema string or optional fields byte-identical for older
  docs); only Drawings + Configurations needed a doc-schema bump historically.

## Recommended CI wiring (the "first time right" enforcement)
```
# hard gates (deterministic, zero false positives)
python scripts/check.py
# or run individually (note: --rule accepts one value; do not pass --rule twice):
python scripts/gui_lint.py --rule icons
python scripts/gui_lint.py --rule theme
python scripts/feature_surface_audit.py
python scripts/parity_audit.py
<venv> -m pytest engine/doc -X utf8
# ratchet (heuristic)
python scripts/gui_lint.py --rule scale --baseline scripts/gui_lint_baseline.json
```
Pre-commit runs the fast deterministic checkers; CI adds pytest + the headless smoke
(`AI_CAD_SHOT=ribbon` exit 0) on the built exe.

## Standards to reach for (new conditions to add)
- **Behavioral assertions** in Tier 2: today most scenarios assert "exit 0 (renders/runs)". Grow them
  to assert *outcomes* (e.g. an extrude scenario asserts body count / volume via the worker), so a
  silent regression in geometry is caught headlessly, not only by eye.
- **A parity-scenario suite**: one headless scenario per P0/P1 capability in `SW_PARITY_GAPS.md`, so
  `parity_audit` (static presence) is backed by behavioral proof.
- **Golden-image diffs** for key shots (optional) to catch visual regressions in the GUI sweep.
