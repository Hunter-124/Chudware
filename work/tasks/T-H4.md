---
id: T-H4
title: Verifier battery API + gap verifiers (groundedness, manifold, unit sanity, dim coverage)
workstream: H
owner: sonnet-tomo
status: review
branch: task/T-H4-verifier-battery
day: 12
depends_on: []
---

## Context

Pillar 16 / verifier inventory (`docs/CUSTOM_MODEL_INTEGRATION_SURVEY.md` §4). 30
deterministic checks exist; 13 are subsecond. This task (a) wraps them behind one
`verify(doc, checks[]) → {scores{}, findings[]}` battery API usable as an RL reward
vector, a CI gate, and T-F12's design_review body (coordinate — T-F12 owns the
narrated verdict, this task owns the scoring substrate); (b) builds the gap verifiers:
standalone GSL groundedness checker (model text → slot-match numerals vs state; also
the IP-7 inference output gate), `is_manifold()` + STEP export/re-import round-trip
check, unit-sanity range validator (the ksi/MPa 6.9× guard), drawing
dimension-coverage check, requirements join checker (req statuses vs verifier
evidence). Tolerance stack stays T-BL08. Note: run_cfd_accurate is Linux/Mac only —
the battery must tag per-check platform support, never assume it in rewards.

## Files / areas touched

- `services/` verify battery module + MCP tool `verify_battery`
- groundedness checker under `tools/translator/` (shared with T-F10 harness — one
  implementation, two callers)
- manifold/round-trip + unit-sanity + dim-coverage + req-join checkers near their
  domains (core/doc, drawing, requirements)
- per-check metadata: cost class (subsecond/seconds/minutes), platform, reward shape

## Definition of done

- [x] verify_battery runs any subset, returns a stable scores vector + findings list;
      deterministic across repeated runs (unit tests + real-engine smoke: identical
      scores/findings across runs, order-independent)
- [x] All five gap verifiers implemented with tests (groundedness: T-F10 fixtures do
      not exist yet — T-F10 unclaimed; validated with its own unit matrix instead,
      see work/questions/T-H4.md §1)
- [x] Battery callable from Python in <50 ms overhead over the underlying checks
      (test_verify_overhead_under_50ms: stubbed checks → elapsed IS overhead; real
      5-check run incl. STEP round-trip = 38 ms total)
- [x] `graphify update .` run (AST rebuild, 19307 nodes). Gate: exit 0 — sole FAIL is
      the PRE-EXISTING ui/imgui `PanelField::selMulti` break (FOREMAN-documented, not
      this branch); core/ctest SKIP (no build dir in worktree — env-only); pytest
      stage SKIP (no runtime venv) so the suite was run manually in the MCP venv:
      `services/mcp/test_verify_battery.py` 23/23 passed.

## Driver notes (Fable, 2026-07-02)

- **The battery is a DISPATCHER — zero reimplementation.** Wrap existing callables:
  `services/mcp/cad_tools.py` — check_sketch :1189, measure_part :1342,
  query_geometry :1174, measure_between :1368, check_interference :1423,
  check_feature_overlap :1455, check_symmetry :1495, wall_thickness :1550,
  draft_analysis :1583, run_fea_static :1901, run_fea_modal :2005, run_cfd_flow
  :2068, run_cfd_accurate :2142 (LINUX/MAC ONLY — tag it), solve_mates :2393,
  review_drawing :2540; `recon_tools.py` compare_parts :1119; `ecad_tools.py` erc
  :888, sim :933, drc :1108, dfm :1149, impedance :1916/:2050; `cam_tools.py`
  cam_simulate :227; `eval_check` (BRepCheck + free/tiny-edge census) at
  `core/doc/session.cpp:6140` — currently only reachable via measure_part; expose
  it directly.
- **Call the underlying functions, not MCP-over-wire** — the <50 ms overhead budget
  means direct Python calls sharing the session context (the pattern gsl_eval.py
  uses: instantiate the tool registry in-process, `tools/gsl_eval.py:65-81`).
- **Per-check metadata table** `{name, cost: subsecond|seconds|minutes, platform:
  any|posix, reward_shape: binary|scalar|findings}` — drives which checks an RL
  loop may request; deterministic ordering of the findings list.
- **Groundedness checker:** ONE implementation under `tools/translator/`, shared
  with the T-F10 eval harness (parse numerals+units from text → slot-match vs a
  state serialization; unit-normalized compare). If T-F10 is `doing`, coordinate
  via work/questions/T-H4.md — whoever lands first owns the module, the other
  consumes.
- **Round-trip verifier:** export STEP (`export_part`) → re-import (`import_part`)
  → `compare_parts` match_pct ≥ threshold + eval_check clean. Manifold = eval_check
  free_edges==0 wrapper with an explicit name.
- **Unit-sanity:** range table per quantity kind (length mm 1e-3..1e5, stress MPa
  0.01..5000, density…) — catches the ksi/MPa 6.9× class; table lives in the
  contract-adjacent constants, cited by T-H3's quantity validation too.
- **T-F12 relationship:** T-F12 (design_review verify battery for DAG nodes) will
  NARRATE what this returns. You own scoring; do not add narration here.
