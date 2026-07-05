# Design Gym — cycle-1 report

**Execution mode: `MOCK`** (tiers: local-24k, frontier-200k, 36 cells)

> HONESTY NOTE: this cycle ran in MOCK mode. The mission pipeline drives INTAKE + manifest validation + DAG compile for real, but the per-node reasoning is REPLAYED from scripted turns (no tool-executing agent_fn, no provisioned model, no C++ core in this environment — see the probe below). Pass rates and taxonomy counts are the harness + classifier exercised end to end on labeled fixtures. A model-behaviour DELTA from a dial change is only measurable once a real model is provisioned; structural-dial deltas (council threshold, budgets) ARE real.

> TIER IS A PASS-THROUGH DIMENSION IN MOCK MODE: scripted turns are tier-invariant, so a challenge's `local-24k` and `frontier-200k` rows are identical by construction. The two-tier matrix proves the harness runs per-tier; it does NOT show tier-dependent model behaviour (that needs a real model). Read the tier split as scaffolding, not signal.

| probe | value |
|---|---|
| mode | `mock` |
| real_agent_fn | `False` |
| frontier_key | `False` |
| local_model | `True` |
| core_available | `False` |

## Pass rates — challenge x tier

| challenge×tier | pass rate | n |
|---|---|---|
| C1|frontier-200k | 1.000 | 3 |
| C1|local-24k | 1.000 | 3 |
| C2|frontier-200k | 1.000 | 3 |
| C2|local-24k | 1.000 | 3 |
| C3|frontier-200k | 0.667 | 3 |
| C3|local-24k | 0.667 | 3 |
| C4|frontier-200k | 1.000 | 3 |
| C4|local-24k | 1.000 | 3 |
| C5|frontier-200k | 0.667 | 3 |
| C5|local-24k | 0.667 | 3 |
| C6|frontier-200k | 1.000 | 3 |
| C6|local-24k | 1.000 | 3 |

- per-challenge: C1=1.00, C2=1.00, C3=0.67, C4=1.00, C5=0.67, C6=1.00
- per-tier: frontier-200k=0.89, local-24k=0.89
- **overall pass rate: 0.889**

## Failure taxonomy

| class | count |
|---|---|
| wrong_question | 4 |
| incoherent_geometry | 4 |
| tool_misuse | 6 |
| context_loss | 4 |
| abstraction_skip | 2 |
| grind | 4 |
| advisory_ignored | 2 |

### Evidence (top refs per fired class)
- **tool_misuse** (6):
  - C1/local-24k/s2: used measure_part for AC2 but check tool is fit_report (evidence seqs [4])
  - C1/frontier-200k/s2: used measure_part for AC2 but check tool is fit_report (evidence seqs [4])
  - C4/local-24k/s3: used measure_part for AC3 but check tool is check_interference (evidence seqs [5])
  - C4/frontier-200k/s3: used measure_part for AC3 but check tool is check_interference (evidence seqs [5])
- **context_loss** (4):
  - C1/local-24k/s3: cited nonexistent ref(s): DIM99 (evidence seqs [7])
  - C1/frontier-200k/s3: cited nonexistent ref(s): DIM99 (evidence seqs [7])
  - C6/local-24k/s2: cited nonexistent ref(s): DIM88 (evidence seqs [7])
  - C6/frontier-200k/s2: cited nonexistent ref(s): DIM88 (evidence seqs [7])
- **advisory_ignored** (2):
  - C2/local-24k/s2: advisory ADV1 (petg_snap_marginal) contradicted without dispute (evidence seqs [8, 9])
  - C2/frontier-200k/s2: advisory ADV1 (petg_snap_marginal) contradicted without dispute (evidence seqs [8, 9])
- **abstraction_skip** (2):
  - C2/local-24k/s3: ladder validator bounced: geometry_plan emitted before function_map omissions checklist (evidence seqs [8])
  - C2/frontier-200k/s3: ladder validator bounced: geometry_plan emitted before function_map omissions checklist (evidence seqs [8])
- **incoherent_geometry** (4):
  - C3/local-24k/s2: acceptance criteria failed: AC2, AC4 (evidence seqs [5])
  - C3/frontier-200k/s2: acceptance criteria failed: AC2, AC4 (evidence seqs [5])
  - C5/local-24k/s2: acceptance criteria failed: AC2, AC4 (evidence seqs [5])
  - C5/frontier-200k/s2: acceptance criteria failed: AC2, AC4 (evidence seqs [5])
- **grind** (4):
  - C3/local-24k/s3: 3 attempts on 'n_build_DL1' with no changed hypothesis (evidence seqs [6, 7, 8])
  - C3/frontier-200k/s3: 3 attempts on 'n_build_DL1' with no changed hypothesis (evidence seqs [6, 7, 8])
  - C6/local-24k/s3: 3 attempts on 'n_build_DL1' with no changed hypothesis (evidence seqs [7, 8, 9])
  - C6/frontier-200k/s3: 3 attempts on 'n_build_DL1' with no changed hypothesis (evidence seqs [7, 8, 9])
- **wrong_question** (4):
  - C4/local-24k/s2: asked a question with no bearing: 'What color should the hinge be?' (evidence seqs [8])
  - C4/local-24k/s2: missed a critical blocking ambiguity: 'pin material / process not specified' (evidence seqs [1])
  - C4/frontier-200k/s2: asked a question with no bearing: 'What color should the hinge be?' (evidence seqs [8])
  - C4/frontier-200k/s2: missed a critical blocking ambiguity: 'pin material / process not specified' (evidence seqs [1])

## Recommended dial adjustments (config/data only)

- **wrong_question** ×4 → knob `playbook_enforcement / clarifier batch gate`: keep step_locked on local tiers; ensure the clarifier playbook's blocking-ambiguity check is enforced before Phase 2 (config, not prose)
- **incoherent_geometry** ×4 → knob `AI_CAD_COUNCIL_FEATURE_THRESHOLD`: lower the build-strategy-council feature threshold so more parts get the four-seat review before build (more machinery for weaker models)
- **tool_misuse** ×6 → knob `tiers.yaml max_tools_per_role / tool_schema_trim`: tighten the per-role tool allowlist so the wrong tool is not in scope; keep preflight on (config), and enable the tool-choice trigger pack
- **context_loss** ×4 → knob `tiers.yaml budgets.ledger / budgets.state`: raise the ledger/state slot budget so cited refs stay in the render (shrink history first, never advisory) — assembly-time budget assertion
- **abstraction_skip** ×2 → knob `deliberation (ladder mandatory) + ladder validator max-bounce`: confirm deliberation=full pins the design-cognition ladder mandatory; the validator bounce is working — no relaxation warranted
- **grind** ×4 → knob `gym grind_threshold / anti-grind replan gate`: the anti-grind rule (2 attempts then replan) is the lever; ensure the replan gate is enabled so a 3rd identical attempt cannot happen
- **advisory_ignored** ×2 → knob `trigger-pack enablement + advisory slot budget`: keep the foreman advisory injection on (deliberation=full) and ensure the advisory slot budget is not shrunk below its floor

## Applied dials + measured deltas

- applied `gym.grind_threshold`: tighten anti-grind sensitivity 3 -> 2 attempts
- applied `AI_CAD_COUNCIL_FEATURE_THRESHOLD`: lower council feature threshold 5 -> 3 (more parts get the four-seat review before build)

```json
{
  "grind_threshold_3_to_2": {
    "grind_before": 4,
    "grind_after": 6,
    "delta": 2,
    "measurable": true,
    "note": "deterministic reclassification of identical ledgers \u2014 a real delta. The +delta is C3/seed3's 2-attempt n_verify_DL1 node, which fires grind only once the threshold drops to 2: the assess-adjust-rerun loop actually reclassifying a cell."
  },
  "council_threshold_5_to_3": {
    "measurable": false,
    "note": "applied to an in-process os.environ var ONLY (evaporates at process exit; persists to no config file) \u2014 pass-rate delta needs a real model in BUILD (mock replay does not convene councils) \u2014 pending-provision"
  }
}
```

## Lead-owned findings (prompt/workflow — NOT applied here)

- **wrong_question** ×4: clarifier role card prompt: strengthen 'ask about the blocking ambiguity FIRST, suppress cosmetic questions' wording
- **tool_misuse** ×6: role card prompt: add the 'each AC names its check tool; use THAT tool' reminder to the relevant worker cards
- **advisory_ignored** ×2: worker role card prompt: 'if you disagree with an advisory, file a replan_request — do not silently proceed'
