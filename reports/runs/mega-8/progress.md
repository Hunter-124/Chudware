# MegaRun8 Progress

- Run ID: `mega-8-fc-rocket-20260714`
- Baseline commit: `7ef7b577` (cycle-8 wave: op-vocab + capability separation + leaf envelope + deliver_partial)
- Conservation: **auto_pick-first** — no per-node frontier injection; xAI only as fallback / where min_rank demands
- Effort: maximum / org layer ON / max depth 8 / xAI semaphore=3
- Auth: SuperGrok **`17d17fb1-247a-4800-ba97-e7b5150bc53a`** active
- Fixer peer: CollectiveElk (Fixer8) — Fixes 1–6 in `fixes.md`
- Driver: `reports/runs/mega-8/run_mega.py`
- Focus axes A–D: see `findings.md`

## Status

- [x] Preflight: subscription account 17d17fb1 active
- [x] Preflight: spawn_probe PASS (PLAN/layout no exec + delegate; leaf keeps exec)
- [x] Preflight: auto_pick smoke PASS (depth-2 → ollama)
- [x] Mission attempt-1 (pid 2410582) — archived `attempt-1-pre-fixer8-rebind/`
- [x] Mission attempt-2 (pid 2453640, wall 926s, Fix 1–3) — archived `attempt-2-pre-fix4-5/`
- [x] Mission attempt-3 (pid 2544922, wall 1003.84s, Fix 1–5) — primary OUT + `attempt-3-fix4-5/`
- [x] Graded A–D + findings.md + roadmap append

- [x] Mission attempt-4 relaunch (Fixer8 F7–F10 bound) — starting
## Axis verdicts (honest)

| Axis | Verdict | One-liner |
|---|---|---|
| A accepted ops + cheatsheet | **FAIL** | 0 accepted ops; a2 unknown `cylinder` without observed cheatsheet |
| B delegate / typed refusal | **PASS** | delegate×1 + spawn; layout/plan gate; SPECIFY not false-gated |
| C leaf oneshot + miss | **PASS** | oneshot_ok×3 + miss×8 telemetry (a2 & a3) |
| D terminal stop_reason | **PASS** | `needs_correction` / `verify_failed: [AC1…AC9]` (not running+empty) |

## Preflight detail

- `subscription_accounts.json` — 2 xai-sub; active=`17d17fb1-…`
- `spawn_probe.json` — capability_separation_ok=True; leaf_exec=True; verdict PASS
- `auto_pick_smoke.json` — leaf_depth2_non_xai=True; verdict PASS

## Attempt notes

### Attempt-1 (pre-Fixer8)
- Fingerprints drove Fix1–3 (oneshot string crash; run_id pack; SPECIFY gate over-apply)

### Attempt-2 (primary for A partial + B/C/D)
- BUILD: new_part + add_features(cylinder) → unknown op; ok wrongly true (pre-Fix5)
- delegate×1 + spawn source-parts; oneshot_ok×3; terminal verify_failed

### Attempt-3 (Fix4/5 bind)
- new_part×1, add_features×0; delegate×1 + feature_modeler spawn; oneshot_ok×3
- terminal same class; specify exit_gate clean first time

## Defects DM'd to Fixer8

See findings fingerprints F1–F10; open: thin unknown-op error, cheatsheet unobserved, typed-refusal success path, BUILD sequence a3 skip add_features.
