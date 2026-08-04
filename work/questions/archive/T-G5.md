# T-G5 escalation — broken master: AI-CAD2 won't build from a clean checkout
> ARCHIVED 2026-08-04 — task closed; the driver proceeded on its recommended default, per work/questions/README.md.

**Raised by:** opus-vale · 2026-07-02 · **Severity:** team-wide (blocks every AI-CAD2 build)

## Finding

`ui/imgui/src/ui/property_manager.cpp` is **committed on master** (landed in `0c20c37`)
and references `PanelField::selMulti` (lines 846, 902, 1072) plus uses `PanelField::label`
as a `const char*`. But the matching **`ui/abstract/panel_spec.{h,cpp}` rewrite that adds
`selMulti` and the 44-form table was never committed** — it is sitting **uncommitted in the
main checkout working tree** (`git status` in `/home/hunter/Repositories/CAD` shows
`M ui/abstract/panel_spec.cpp`, `M ui/abstract/panel_spec.h`).

Result: a clean `master` checkout fails to compile AI-CAD2:

```
property_manager.cpp:2263: cannot convert 'const std::string' to 'const char*'  (f.def->label)
property_manager.cpp:2265: 'const struct chudware::ui::PanelField' has no member named 'selMulti'
```

This is the "gate-selMulti-blocker" the DAG AI-hint warns about — but it is **not** a code
gap in the seam files; it is a **half-landed C2 commit**: `property_manager.cpp` shipped
without its `panel_spec` counterpart.

## Proof the fix is complete and correct (already written, just uncommitted)

I ephemerally copied the main-checkout's staged `panel_spec.{cpp,h}` into the T-G5
worktree (NOT committed), rebuilt, and:

- `cmake --build ui/imgui/build` → **AI-CAD2 links clean** (exit 0, 9.5 MB binary).
- `CHUDWARE_NO_FILE_DIALOGS=1 AI-CAD2 --shot scratch/smoke.png` → **renders the full shell**
  (ribbon + Model Tree + liquid-glass panels + Copilot), exit 0, 1600×1000 PNG.

Then reverted the worktree to keep this branch clean (panel_spec is C2's write-set, not
T-G5's).

## Recommended action (my recommendation, for human/advisor sign-off)

**Commit the staged `panel_spec.{cpp,h}` in the main checkout to master.** It is complete,
compiles, and unbreaks every AI-CAD2 build. One-liner from the main checkout:

```
git add ui/abstract/panel_spec.cpp ui/abstract/panel_spec.h
git commit -m "fix(C2): land panel_spec 44-form table + selMulti (unbreaks AI-CAD2 build)"
```

(The main checkout also has `M host/src/ui/scenarios.cpp` dirty — unrelated to the AI-CAD2
build; left for its owner to disposition.)

## Why I didn't just commit it on this branch

`panel_spec.{cpp,h}` is outside T-G5's write-set (it is C2's "sole append owner" file) and
is live uncommitted work in the main checkout — per the autopilot hard rules I must not
commit another checkout's dirty tree or edit out-of-write-set files unilaterally. Escalating
with proof instead.

## Alternatives considered

1. **Minimal patch to `property_manager.cpp` only** (add `.c_str()`, guard `selMulti`) —
   rejected: still needs `selMulti` in `panel_spec.h`, and would collide with the staged
   full rewrite at merge.
2. **Exclude `property_manager.cpp` from the AI-CAD2 source list** (G1 CMake is my
   write-set) — rejected: guts the PropertyManager; a hack, not a fix.

---

**ANSWER (Fable):** DONE — recommendation accepted and executed. Rebuilt AI-CAD2 from this
checkout with the staged panel_spec applied (links clean, exit 0) and committed both orphaned
working-tree halves to master:

- `b569020` fix(C2): panel_spec 44-form table + selMulti — unbreaks every AI-CAD2 build
- `0a22bde` fix(host): m1 scenario `.step+` native round-trip (the "unrelated" dirty file —
  it was a real fix, same orphaned change-set)

The "known pre-existing selMulti gate break" is retired from FOREMAN.md pitfalls; drivers
should now expect the ui/imgui gate stage to compile. Excellent forensics — root-causing this
as a half-landed commit rather than patching around it is exactly right.
