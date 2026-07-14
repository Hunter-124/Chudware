# Design Gym — cycle-1 report

**Execution mode: `REAL`** (tiers: local-24k, 3 cells)

| probe | value |
|---|---|
| mode | `real` |
| real_agent_fn | `True` |
| frontier_key | `True` |
| local_model | `True` |
| core_available | `True` |

## Pass rates — challenge x tier

| challenge×tier | pass rate | n |
|---|---|---|
| C1|local-24k | 1.000 | 3 |

- per-challenge: C1=1.00
- per-tier: local-24k=1.00
- **overall pass rate: 1.000**

## Failure taxonomy

| class | count |
|---|---|
| wrong_question | 0 |
| incoherent_geometry | 0 |
| tool_misuse | 1 |
| context_loss | 1 |
| abstraction_skip | 0 |
| grind | 0 |
| advisory_ignored | 0 |
| execution_stall | 0 |

### Evidence (top refs per fired class)
- **tool_misuse** (1):
  - C1/local-24k/s2: used measure_part for AC2 but check tool is fit_report (evidence seqs [5])
- **context_loss** (1):
  - C1/local-24k/s3: cited nonexistent ref(s): DIM99 (evidence seqs [8])

## Recommended dial adjustments (config/data only)

- **tool_misuse** ×1 → knob `tiers.yaml max_tools_per_role / tool_schema_trim`: tighten the per-role tool allowlist so the wrong tool is not in scope; keep preflight on (config), and enable the tool-choice trigger pack
- **context_loss** ×1 → knob `tiers.yaml budgets.ledger / budgets.state`: raise the ledger/state slot budget so cited refs stay in the render (shrink history first, never advisory) — assembly-time budget assertion

## Lead-owned findings (prompt/workflow — NOT applied here)

- **tool_misuse** ×1: role card prompt: add the 'each AC names its check tool; use THAT tool' reminder to the relevant worker cards
