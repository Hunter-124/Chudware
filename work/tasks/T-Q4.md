---
id: T-Q4
title: User manual auto-generation — cad_guide + inventory → living docs
workstream: Q
owner: opus-flint
status: review
branch: task/T-Q4-prod
day: 4
depends_on: []
---

## Context

PROJECT-LEAD ADDITION: docs rot. We already generate a 757-row UI inventory (T-G12) and
have rich cad_guide topics + the deliverables engine — compose them into a regenerable
user manual instead of hand-writing one.

## Files / areas touched

- tools/manual_gen.py: compose cad_guide topics + ui_inventory output + contracts
  summaries into docs/manual/ markdown; render to PDF via the deliverables engine
  (make_report path) as the distributable
- CI-able check: manual regen is clean (no drift) — advisory gate stage

## Definition of done

- [x] docs/manual/ regenerates deterministically; PDF renders via deliverables.
      `tools/manual_gen.py` writes 13 deterministic markdown files (index, overview, commands,
      9× guide-<topic>, contracts) — NO timestamps/wallclock, sorted iteration; drift-CLEAN under
      BOTH mcp-venv and system python3 (the interpreter the gate uses). PDF renders via the new
      `user_manual` make_report template: VERIFIED end-to-end — a real 10.5 KB PDF + a DOCX
      produced by `make_report` with reportlab/python-docx present (proven in a throwaway venv;
      those engines are declared in requirements.txt but absent from this box's venvs, so the
      render DEGRADES contract-shaped — `{ok:false, reason, hint}`, never a crash — exactly the
      sanctioned optional-engine posture; `test_spec_validates_without_render_engine` proves the
      spec/provenance path is correct independent of the render engine).
- [x] Coverage assert: every ribbon command in the inventory appears in the manual (name +
      one-liner, auto-stubbed where no guide text exists). `coverage()` → 225/225, 0 missing.
      Only 3/225 commands (fillet/chamfer/shell) have a real cad_guide cheatsheet; the rest get a
      visibly generic stub `_Available from the {tab} ▸ {group} ribbon group._` derived ONLY from
      the inventory — the no-fabrication rule (driver note): the manual never states a capability
      nobody wrote down. Enforced by `test_no_fabrication_autostub`.
- [x] Drift check script + advisory wiring. `python -m tools.manual_gen --check` (exit 1 on drift
      or missing coverage); wired into `scripts/gate.sh` as `stage_manual_gen` — NON-FATAL advisory
      (records PASS/FAIL like the check.py stage, never sets fail_fatal), so "someone added a ribbon
      command and forgot to regen" is flagged without blocking the build.
- [x] gate; `graphify update .` run. graphify updated; gate advisory stage green (drift CLEAN).
      Change is Python + docs + gate.sh only — no C++, so the buildable gate stages are baseline.

## Closeout / verification (opus-flint, 2026-07-03)

`make_manual` stays RESERVED (deliverables/1 §1); the manual ships as a `make_report` template
`user_manual` (report-kind, `lb_slots={}` — a manual is all free prose, no load-bearing numbers),
mirroring the T-F15 `assembly_manual` precedent. No contract change; no new deps.

TEST INVOCATION (pure Python, no core / no AI_CAD_LIBAICAD):
  `~/.local/share/AI-CAD/mcp-venv/bin/python -m pytest tools/test_manual_gen.py -q` → 8 passed
  (determinism, 225/225 coverage, drift-clean, no-fabrication ×2, render-degrades-cleanly,
   spec-shape, spec-validates-without-engine).
  `python -m tools.manual_gen` → wrote 13 files, coverage 225/225.
  `python -m tools.manual_gen --check` (system python3, the gate's interpreter) → drift CLEAN.

NOTE for the integrator: reportlab + python-docx are declared in requirements.txt but not
installed in mcp-venv (same provisioning gap as PyNite/openai were) — the PDF/DOCX render is
proven correct (10.5 KB PDF in a scratch venv) and degrades cleanly where absent. Installing them
in mcp-venv makes the render live in-app; not required for merge (advisory stage checks the
markdown drift, which needs no engine).

## Driver notes (Fable)

- make_manual was RESERVED in the deliverables registry (T-F15 question ruling) — this
  task is why. Land the template through the frozen registry path, provenance rules
  intact (generated sections tagged; no fabricated prose about unimplemented features —
  the inventory is the truth source).
