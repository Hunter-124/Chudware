# MegaRun8 Progress

- Run ID: `mega-8-fc-rocket-20260714`
- Baseline: `7ef7b577` + Fixer8 Fixes 1–11; **engine daemon rebuilt ~22:20** (cycle-8 registry live)
- Conservation: auto_pick-first; xAI semaphore=3; account **`17d17fb1-…`**
- Fixer peer: CollectiveElk (Fixer8)
- Driver: `reports/runs/mega-8/run_mega.py`

## Status

- [x] Preflight PASS (account, spawn_probe cap-sep, auto_pick smoke)
- [x] Attempt-1 archived (pre-Fixer8)
- [x] Attempt-2 archived (Fix 1–3; A fail stale engine)
- [x] Attempt-3 archived (Fix 1–5; A fail stale engine)
- [x] Attempt-4 primary (pid 2668492, wall 693.46s, live engine + F7–F10)
- [x] Graded A–D + findings.md + roadmap append

## Axis verdicts (attempt-4 primary)

| Axis | Verdict | One-liner |
|---|---|---|
| A accepted ops | **PASS** | add_features accepted ×5 (cylinder/shell/cone) after new_part; real solids |
| B delegate/gate | **PASS** | delegate×1 + spawn; layout/plan gate; no SPECIFY false-gate |
| C leaf oneshot | **PASS** | oneshot_ok×10 miss×1 |
| D terminal | **PASS** | `failed` / `dag_error: string or blob too big` (explicit; Fix11 residual) |

## Attempt-4 product highlights

- `new_part`×2 → `add_features` ok batches with `op:add_feature` + cylinder/shell/cone
- One cone param reject then successful retry
- model_assignment 11 ollama / 9 xai
- Terminal interrupted by SqliteSaver blob size after productive BUILD/VERIFY/DELIVER work

## Open residual for Fixer8

- Fix11 bind (blob crash) — landed, not re-proven
- Typed-refusal success path still unused
- Cheatsheet inject event still 0 (no bare unknown-op this run)
