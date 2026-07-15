# MegaRun6 Progress

- Run ID: `mega-6-fc-rocket-20260714`
- Baseline commit: `563fb2c0` (full Fixer5 wave bound)
- Conservation: **auto_pick-first** — no per-node frontier injection; xAI only as fallback / where min_rank demands
- Effort: maximum / org layer ON / max depth 8 / xAI semaphore=3
- Auth: SuperGrok live for hard nodes; GPU free for local leaf models (ollama llama3.2)
- Runtime ledger: `~/.local/share/AI-CAD/cache/runs/mega-6-fc-rocket-20260714/ledger.json`
- Fixer peer: StaticOwl (Fixer6) owns services/agent|dag|mcp + gym + mega-6/fixes.md
- Driver: `reports/runs/mega-6/run_mega.py`

## Pre-flight

- `spawn_probe.json` verdict **PASS** — required roles have `delegate` + rendered `budget_tokens`
- `auto_pick_smoke.json` verdict **PASS** — depth-2 low-difficulty → `ollama/llama3.2:latest` (non-xai)
- Attempt-1 (openrouter keyless wipe): 19/19 `provider_auth` in ~4s — saved under `attempt-1-openrouter-auth-wipe/`
- Attempt-2: driver pins `enabled_providers=[ollama, xai-sub]` (no keyless openrouter); smoke shows depth0→xai-sub, depth2→ollama

## Snapshot 1 — ~100s (attempt-2)

- INTAKE done (~37s); SPECIFY 5-way fan-out live
- Tools: part_search×8, ledger_get×7, …
- 1 fail: n_spec_DL5 `no_code`
- 0 model_assignment events (process pre-Fix6 rebind)
- 0 provider_auth

## Snapshot 2 — ~5m

- SPECIFY 4/5 done (DL1/2/3/4); DL5 no_code
- Live stage_log entered specify then plan/build/verify/deliver (Fixer5 live cursor bound)
- provider_usage×70 all `xai-sub/grok-4.5` (depth-0 min_rank=3 filters ollama rank1)
- 0 timeouts under semaphore=3

## Snapshot 3 — ~10m

- DAG: 15 started; **11 done, 2 failed**; BUILD DL1+DL2 in flight
- Failures: n_spec_DL5 no_code; n_organize no_code
- Done: intake, SPECIFY DL1–4, n_spec_done, DELIVER DL3/4/5, PLAN layout, PLAN mission
- **BUILD signal:** `new_part`×1+, `preflight_ops`×5+, `describe_part`, `buildgraph_plan`
- Delegation: **0** `delegate` calls / depth 0
- provider_usage: **120** turns, all SuperGrok, ~1.49M total_tokens
- xAI timeouts: **0**

## Snapshot 4 — terminal (wall 888.68s)

- **Mission:** `blocked` / `deliver_incomplete`; `phases_completed=["intake","specify","plan"]`
- **DAG:** 19/19 started; **17 done, 2 failed** (`n_spec_DL5`, `n_organize` no_code)
- **Tools:** 170 total — `new_part`×2, `preflight_ops`×12, `verify_battery`×3, `check_interference`×1, `buildgraph_plan`×2; **0** `add_features` / board / fw / `delegate`
- **provider_usage:** 192 × `xai-sub/grok-4.5` = **2,545,663** total tokens (first auditable spend)
- **model_assignment events:** 0 (Fix6 unbound this process)
- **xAI timeouts:** 0 (vs mega-5's 7)
- **Wall vs mega-5:** 888.68s vs 1438.65s (−38%)
- **Docs created:** `doc-cpp0000008c` (skeleton), `doc-cpp00000091` (airframe) — both empty solids
- **Findings:** `findings.md` (six verdicts + grok delta + TOP5)
- **Fixer6:** `fixes.md` Fixes 1–7; Fix6–7 need relaunch

## Owner note (conservation)

Cheap-leaf routing is **implemented** (smoke PASS) but **not exercised on live DAG nodes** because every mission template node runs at depth 0 with min_rank≥3, so auto_pick correctly prefers xai-sub (rank 5) over ollama (rank 1). Cheap leaves require either (a) depth≥2 `delegate` spawns or (b) leaf task_class/min_rank on VERIFY/DELIVER nodes. SuperGrok wins this cycle: no openrouter 401 after attempt-1, 0 read timeouts, −38% wall, measured 2.55M tokens.
