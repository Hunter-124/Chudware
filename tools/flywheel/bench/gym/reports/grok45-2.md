# Design Gym — grok45-2 report

**Execution mode: `REAL`** (tiers: xai:grok-4-5, 3 cells)

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
| C1|xai:grok-4-5 | 0.000 | 3 |

- per-challenge: C1=0.00
- per-tier: xai:grok-4-5=0.00
- **overall pass rate: 0.000**

## Failure taxonomy

| class | count |
|---|---|
| wrong_question | 0 |
| incoherent_geometry | 3 |
| tool_misuse | 0 |
| context_loss | 0 |
| abstraction_skip | 0 |
| grind | 3 |
| advisory_ignored | 0 |
| execution_stall | 0 |

### Evidence (top refs per fired class)
- **incoherent_geometry** (3):
  - C1/xai:grok-4-5/s1: acceptance criteria failed: AC2, AC3, AC4 (evidence seqs [])
  - C1/xai:grok-4-5/s2: acceptance criteria failed: AC2, AC3, AC4 (evidence seqs [])
  - C1/xai:grok-4-5/s3: acceptance criteria failed: AC1, AC2, AC3, AC4 (evidence seqs [])
- **grind** (3):
  - C1/xai:grok-4-5/s3: 3 attempts on '?' with no changed hypothesis (evidence seqs [10, 17, 76])
  - C1/xai:grok-4-5/s3: 3 attempts on '?' with no changed hypothesis (evidence seqs [21, 66, 92])
  - C1/xai:grok-4-5/s3: 3 attempts on '?' with no changed hypothesis (evidence seqs [25, 36, 62])

## Recommended dial adjustments (config/data only)

- **incoherent_geometry** ×3 → knob `AI_CAD_COUNCIL_FEATURE_THRESHOLD`: lower the build-strategy-council feature threshold so more parts get the four-seat review before build (more machinery for weaker models)
- **grind** ×3 → knob `gym grind_threshold / anti-grind replan gate`: the anti-grind rule (2 attempts then replan) is the lever; ensure the replan gate is enabled so a 3rd identical attempt cannot happen

## Lead-owned findings (prompt/workflow — NOT applied here)

_none._
