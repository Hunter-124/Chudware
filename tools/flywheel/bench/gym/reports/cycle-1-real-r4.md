# Design Gym — cycle-1-real-r4 report

**Execution mode: `REAL`** (tiers: local-24k, 3 cells)

| probe | value |
|---|---|
| mode | `real` |
| real_agent_fn | `True` |
| frontier_key | `False` |
| local_model | `True` |
| core_available | `True` |

## Pass rates — challenge x tier

| challenge×tier | pass rate | n |
|---|---|---|
| C1|local-24k | 0.000 | 3 |

- per-challenge: C1=0.00
- per-tier: local-24k=0.00
- **overall pass rate: 0.000**

## Failure taxonomy

| class | count |
|---|---|
| wrong_question | 0 |
| incoherent_geometry | 3 |
| tool_misuse | 0 |
| context_loss | 0 |
| abstraction_skip | 0 |
| grind | 0 |
| advisory_ignored | 0 |
| execution_stall | 0 |

### Evidence (top refs per fired class)
- **incoherent_geometry** (3):
  - C1/local-24k/s1: acceptance criteria failed: AC1, AC2, AC3, AC4 (evidence seqs [])
  - C1/local-24k/s2: acceptance criteria failed: AC1, AC2, AC3, AC4 (evidence seqs [])
  - C1/local-24k/s3: acceptance criteria failed: AC1, AC2, AC3, AC4 (evidence seqs [])

## Recommended dial adjustments (config/data only)

- **incoherent_geometry** ×3 → knob `AI_CAD_COUNCIL_FEATURE_THRESHOLD`: lower the build-strategy-council feature threshold so more parts get the four-seat review before build (more machinery for weaker models)

## Lead-owned findings (prompt/workflow — NOT applied here)

_none._
