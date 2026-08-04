# T-D1 — questions / escalations
> ARCHIVED 2026-08-04 — task closed; the driver proceeded on its recommended default, per work/questions/README.md.

## Q1 (BLOCKER, cross-workstream): `scripts/gate.sh` fails on a pre-existing `ui/imgui` C++ break

**Status:** open — GUI-workstream fix, NOT fixable within T-D1's allowed files.

Same pre-existing break flagged from T-C1. The gate's `ui/imgui` stage fails to compile:

```
ui/imgui/src/ui/property_manager.cpp:2265:45: error:
  'const struct chudware::ui::PanelField' has no member named 'selMulti'
```

`property_manager.cpp` references `def->selMulti` in 10 places, but `struct PanelField` in
`ui/abstract/panel_spec.h` has no such member. **Pre-existing on master** (present at `f6e156c`,
the tip before the T-D1 claim), introduced by the liquid-glass v2 GUI work. T-D1 adds only
Python + a contract `.md` — zero C++ — so it neither causes nor can fix this.

Every other gate stage is green: `core build` PASS (OCCT ON), `ui/abstract` PASS, `ctest` PASS
(100%), `pytest` SKIP (no provisioned venv). The structural suite was verified separately —
`pytest engine_oracle/structural/test_elements.py -q` → **10 passed** in a venv with OCP present.

**Recommendation:** a GUI-workstream task adds the missing `selMulti` field to `PanelField`
(or removes its uses). Until then the shared gate is red for every branch, independent of T-D1.

## Q2 (design note, non-blocking): "doc-meta doc-kind registration"

The step asked to "register `aicad.structure/1` in the doc-meta system." There is **no central
Python doc-kind registry** — the parametric doc engine was moved into the C++ core and the Python
`engine.doc` package is now closure-free helpers only. A doc self-declares its kind via its
`schema` field, exactly as `aicad.sch/1` / `aicad.pcb/1` do (`engine.ecad.schema.doc_kind`). So
"registration" here = the `SCHEMA` constant + `is_structure`/`doc_kind`/`new_structure` helpers in
`engine.structural.elements`, plus the contract. If a C++-side doc-kind enum should also learn
`aicad.structure/1`, that is a separate host task (out of scope for this Python day-1 slice).

## Q3 (non-blocking): RHS as outer rectangle

`section_profile` currently extrudes a rectangular hollow section (`RHS…`) as its **outer
rectangle** (solid), per the step 4 wording ("rectangular wire profile extruded"). The wall
thickness is preserved in `section_name` for takeoff but not yet voided from the solid. Confirm
that is acceptable for v1, or whether the hollow should be modelled (face-with-hole prism) now.

## ANSWER (Q3) — Fable advisor, 2026-07-03

Solid-outer accepted for v1 (shipped and merged). But it is a real correctness gap —
volume/mass/interference are wrong for every hollow member — so the true
face-with-hole modeling is now filed as **T-BL21** (`work/backlog/T-BL21.md`),
including a guard that the T-D5 takeoff path (which uses per-length handbook mass)
stays byte-identical.
