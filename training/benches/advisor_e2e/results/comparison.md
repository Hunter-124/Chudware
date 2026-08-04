# T-W11 — advisory loop end-to-end benchmark

Mission: Design a bearing mount bracket. It carries a 20 mm shaft on a g6 sliding fit, so the bore must be sized for a clearance fit on a 20 g6 shaft. The base has 4x M5 clearance holes for bolting down. Material is aluminium and it will be machined. It has to survive a 500 N side load on the bore with a safety factor above 1.

## Comparison (deterministic scoring only)

| loop | tier | model | seed | ok | stop | iters | tool fails | score | tokens | wall s |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | anthropic-sub/claude-opus-5 | claude-opus-5 | 1 | yes | confirmed | 23 | 3/23 | 1.00 | 700916 | 235.51 |
| advisory | anthropic-sub/claude-opus-5 | claude-opus-5 | 1 | yes | done | 94 | 11/94 | 1.00 | 367231 | 1001.03 |
| baseline | kimi-sub/k3-256k | k3-256k | 1 | yes | confirmed | 32 | 1/32 | 0.00 | 397821 | 703.96 |
| advisory | kimi-sub/k3-256k | k3-256k | 1 | yes | done | 48 | 9/48 | 0.00 | 198257 | 1778.2 |

## Per-criterion detail

| loop | tier | seed | bore fit | hole pattern | manifold | FEA |
|---|---|---|---|---|---|---|
| baseline | anthropic-sub/claude-opus-5 | 1 | PASS (20.01) | PASS (4/4) | PASS | PASS (FoS 15.0) |
| advisory | anthropic-sub/claude-opus-5 | 1 | PASS (20.01) | PASS (4/4) | PASS | PASS (FoS 15.0) |
| baseline | kimi-sub/k3-256k | 1 | FAIL (None) | FAIL (0/4) | FAIL | n/a (FoS None) |
| advisory | kimi-sub/k3-256k | 1 | FAIL (None) | FAIL (0/4) | FAIL | n/a (FoS None) |

## Gap ledger — what to actually fix

| bucket | tool | n | legs | why | verbatim error |
|---|---|---|---|---|---|
| **guidance_gap** | `add_features` | 1 | advisory/kimi-sub/k3-256k | required parameter omitted | resulting document failed validation: features[10](f11).refs.edges: required for chamfer |
| **teaching** | `buildgraph_plan` | 3 | advisory/anthropic-sub/claude-opus-5, advisory/kimi-sub/k3-256k, baseline/kimi-sub/k3-256k | domain/geometry teaching the model should absorb | {"ok": false, "reason": "buildgraph_plan needs a plan: no plan_json was passed and the other arguments carry no build description", "hint": "Accepted  |
| **teaching** | `fit_report` | 3 | advisory/anthropic-sub/claude-opus-5, advisory/kimi-sub/k3-256k | domain/geometry teaching the model should absorb | nothing measured yet |
| **teaching** | `delegate` | 2 | advisory/anthropic-sub/claude-opus-5 | domain/geometry teaching the model should absorb | tool 'delegate' is ADVERTISED to you but could not be resolved in this run: it is a mission-scoped tool (it needs a live mission run — ledger, run_id, |
| **teaching** | `dag_status` | 2 | advisory/anthropic-sub/claude-opus-5, advisory/kimi-sub/k3-256k | domain/geometry teaching the model should absorb | {"ok": false, "reason": "dag_status needs run_id — the UUID4 a run is identified by, and nothing in this session has started one yet.\n  Two-call sequ |
| **teaching** | `add_features` | 1 | baseline/anthropic-sub/claude-opus-5 | domain/geometry teaching the model should absorb | batch rejected, nothing was kept — f9 (GussetRib): box boss added no material — the fuse with the current body gained zero volume, so this primitive l |
| **teaching** | `run_fea_static` | 1 | baseline/anthropic-sub/claude-opus-5 | domain/geometry teaching the model should absorb | loads[0]: unknown key 'vector' in this boundary-condition entry — it is not a spelling of any accepted key, and guessing would run a load you did not  |
| **teaching** | `plm_checkpoint` | 1 | baseline/anthropic-sub/claude-opus-5 | domain/geometry teaching the model should absorb | plm_checkpoint is missing required argument(s): path. Call it with every required field, e.g. plm_checkpoint(path='<path>'). Repeating the same empty  |
| **teaching** | `research_eng_calc` | 1 | advisory/anthropic-sub/claude-opus-5 | domain/geometry teaching the model should absorb | {"ok": false, "schema": "aicad.calc/1", "reason": "bad_query", "hint": "calc 'beam': wrong params — beam() missing 3 required positional arguments: 's |
| **teaching** | `research_eng_calc` | 1 | advisory/anthropic-sub/claude-opus-5 | domain/geometry teaching the model should absorb | {"ok": false, "reason": "bad_query", "hint": "formula 'safety_factor' needs params ['strength_mpa', 'stress_mpa']; missing ['strength_mpa', 'stress_mp |
| **teaching** | `describe_part` | 1 | advisory/anthropic-sub/claude-opus-5 | domain/geometry teaching the model should absorb | no such document 'bearing_mount_bracket': it matches no open document's id or name. Open parts: doc-cpp00000001="tw11_baseline_s1_ada3", doc-cpp000000 |
| **teaching** | `buildgraph_status` | 1 | advisory/anthropic-sub/claude-opus-5 | domain/geometry teaching the model should absorb | {"ok": false, "reason": "Build graph run 'tw11-advisory-anthropicsubclaudeopus5-s1-ecd97c' not found", "hint": "Runs that DO exist in /home/hunter/.lo |
| **teaching** | `dfm_check` | 1 | advisory/anthropic-sub/claude-opus-5 | domain/geometry teaching the model should absorb | Unknown process 'cnc_milling'. Known processes: ['fdm', 'cnc_mill', 'lathe', 'sheet_metal', 'laser', 'injection']. |
| **teaching** | `research_eng_calc` | 1 | advisory/kimi-sub/k3-256k | domain/geometry teaching the model should absorb | {"ok": false, "schema": "aicad.calc/1", "reason": "bad_query", "hint": "calc 'beam': wrong params — beam() missing 4 required positional arguments: 's |
| **teaching** | `buildgraph_status` | 1 | advisory/kimi-sub/k3-256k | domain/geometry teaching the model should absorb | {"ok": false, "reason": "Build graph run 'tw11-advisory-kimisubk3256k-s1-d41cbb' not found", "hint": "Runs that DO exist in /home/hunter/.local/share/ |
| **teaching** | `add_features` | 1 | advisory/kimi-sub/k3-256k | domain/geometry teaching the model should absorb | HARD guardrail: dangling feature/face refs — cannot apply. Missing: E2bb85a. Fix the refs (no waiver for kernel integrity). Refs that exist: BasePlate |
| **teaching** | `add_features` | 1 | advisory/kimi-sub/k3-256k | domain/geometry teaching the model should absorb | resulting document failed validation: features[10](f11).refs.on: unknown ref slot for box — box accepts refs: at, axis |
| **teaching** | `add_features` | 1 | advisory/kimi-sub/k3-256k | domain/geometry teaching the model should absorb | resulting document failed validation: features[10](f11).refs.at: selector must be an object — a selector slot accepts: a geometry QUERY object (e.g. { |

## Subscription spend

```
anthropic:  5h=9.0% *7d=6.0% cap=15.0% headroom=+9.0pts
kimi-code:  5h=2.0% *total=11.0% cap=18.0% headroom=+7.0pts
```

## Per-loop totals

| loop | legs | mean score | tool calls | tool failures | fail rate | tokens | advisories |
|---|---|---|---|---|---|---|---|
| advisory | 2 | 0.50 | 142 | 20 | 14.1% | 565,488 | 0 |
| baseline | 2 | 0.50 | 55 | 4 | 7.3% | 1,098,737 | 0 |
