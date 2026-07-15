# mega-6 harness fixes (Fixer6 = StaticOwl)

Live mission: MegaRun6 = ComprehensiveBandicoot (`reports/runs/mega-6/**`).
Baseline commit: `563fb2c0` (first process where full Fixer5 wave binds).
Scope is harness enablement only; no grading changes.
No C++.

Owner priorities:
1. Conserve SuperGrok — cheap models take leaf work (auto_pick, no per-node frontier inject).
2. Produce BUILD outputs (parts/board/firmware).

## Live fingerprints that drove fixes

### Attempt-1 openrouter auth wipe (CRITICAL)
- Wall ~4s, **19/19 nodes `provider_auth`**.
- `auto_pick` depth0 `min_rank≥3` selected `openrouter` free (`nvidia/nemotron-ultra:free`)
  over `xai-sub` because free models are rank 3–4 and cost-ranked cheaper.
- Host had **no OpenRouter API key** → HTTP 401 on every complete.
- No construction-time key gate; no fallback to injected xAI.
- 0 tools, 0 model_assignment events (nodes died before productive work).
- Artifacts: `mega-6/attempt-1-openrouter-auth-wipe/` (MegaRun6).

### mega-5 join fingerprint (still open for process rebind)
- Child join: `executed=true`, `handoff=null`, `tokens_spent=0`.
- Child ran tools (`skill_activate`/`cad_guide`) but parent saw no structured handoff
  and no real token accounting.

---

## Fix 1 — keyless OpenRouter never auto_picked / never constructed

**Fingerprint:** attempt-1 19/19 `provider_auth` from keyless free openrouter.

**Fix:**
- `role_models._enabled_providers`: only add OpenRouter candidates when
  `openrouter.api_key` / `openrouter_api_key` / `OPENROUTER_API_KEY` is present.
- Explicit `enabled_providers` lists also drop keyless openrouter entries.
- `config.get_provider("openrouter")` raises `ValueError` if no key (never builds
  a client that will 401).

**Regression:**
- `test_keyless_openrouter_excluded_from_enabled_providers`
- `test_keyless_openrouter_excluded_from_explicit_list`
- `test_auto_pick_depth0_without_openrouter_key_picks_xai`
- `test_openrouter_construction_requires_key`
- `test_openrouter_enabled_when_key_present`

---

## Fix 2 — child join handoff + real tokens_used

**Fingerprint:** mega-5 join `handoff=null`, `spent=0` despite child tool calls.

**Fix (`mission_dispatch._run_subagent`):**
- Prefer `result.tokens_used` / `usage.total_tokens` from the child turn over the
  tool-count estimate; set `tokens_used_estimate=false` when real usage present.
- When child omits a formal handoff envelope but has calls or success, **synthesise**
  a structured handoff (`status`, `summary`, `outputs.calls`) so the parent join is
  never blind.
- Surface `provider_usage_recorded` on the join result.

**Also (`agent_turn`):** partial max_iters / phase_step_cap finishes with tools now
emit a synthesised handoff (not just bare calls).

**Regression:** `test_join_synthesises_handoff_when_child_omits_envelope`

---

## Fix 3 — provider token usage recorded (auditable spend)

**Fingerprint:** mega-5 `provider_usage_recorded=false`; SuperGrok spend unobservable.

**Fix:**
- New `services/agent/providers/token_usage.py` — context-var last-call stash +
  turn accumulator (mirrors `logging_provider` sidecars; does not change frozen
  `complete_ex` return signature).
- `openai_provider`, `xai_sub_provider`, `anthropic_provider` record usage from
  response bodies after each complete.
- `agent_turn.run_turn` begins a turn accumulator, logs `provider_usage` ledger
  events per complete, and attaches `usage` / `provider_usage_recorded` /
  `tokens_used` onto every `TurnResult.result` via `_with_usage`.

**Regression:**
- `test_token_usage_stash_and_with_usage`
- `test_extract_openai_usage_dict`
- `test_agent_turn_records_usage_in_result`

---

## Fix 4 — BUILD enablement (new_part/add_features + tool floor)

**Fingerprint:** mega-5 BUILD DL1 `no_code` / zero `new_part`/`add_features`;
child tool budgets too tight for bootstrap → mutate → measure.

**Fix (`mission_dispatch._run_subagent`):**
- Geometry roles (`feature_modeler`, `geometry_designer`, `sketcher`,
  `assembly_fit_engineer`, `layout_engineer`) and build-class phases get
  `max_tool_calls` floor **≥ 8** (re-asserted after capability phase_cap so A/B
  caps cannot starve mutators).
- If a geometry role allowlist loses `new_part`/`add_features`, re-seed them.

**Regression:**
- `test_feature_modeler_allowlist_includes_new_part`
- `test_build_role_tool_floor_at_least_eight`

---

## Fix 5 — local leaf failure degrades (never block)

**Fingerprint:** ollama leaf may need pull / connection refused under conservation
routing; must not kill the node when a free/openrouter or injected frontier is
available.

**Fix:**
- `node_runner._resolve_provider`: on ollama/local construct failure, try
  openrouter free **if keyed**, else fall through to injected / tier / ambient.
- `agent_turn.run_turn`: on complete-time local connection / model-not-found
  errors, one-shot degrade to openrouter free when keyed (marks
  `_mega6_degraded` to avoid loops); logs `provider_degrade`.

**Regression:** `test_resolve_provider_degrades_ollama_construct_fail_to_injected`

---

## Fix 6 — model_assignment events on every DAG node resolve

**Fingerprint:** live mega-6 attempt-2 mid-run: auto_pick smoke PASS (leaf→ollama,
depth0→xai-sub) but **0** `model_assignment` ledger/event rows. Assignment was
only emitted on delegate spawn (`mission_dispatch._run_subagent`), not on the
DAG `node_runner._resolve_provider` path that actually runs every node.

**Fix:**
- `_resolve_provider` emits `agent.model_assignment` via the live sink and
  appends a ledger `note` with the assignment table row after a successful
  auto/ai_roles pick.
- `_default_agent_fn` threads the node `ledger` into `context_pack` so the
  resolve path can write notes.

**Regression:** `test_resolve_provider_emits_model_assignment_event`

---

## Fix 7 — BUILD: nudge add_features after new_part; empty solid ≠ done

**Fingerprint (attempt-2 final):** BUILD DL1 tools_slot **had** `new_part`+`add_features`
(Fix4 enablement OK). Live sequence: `new_part` ok → `preflight_ops`×7 → max_iters
auto-complete with **zero `add_features`** → empty solid marked done. BUILD DL2
(assembly role) had no `new_part`/`add_features` in allowlist (assembly tools only)
and thrashed `ledger_get`. Mission: `blocked`/`deliver_incomplete`; phases
completed intake/specify/plan only. Wall 888s; provider_usage×192; 0 timeouts;
0 provider_auth (vs attempt-1 wipe).

**Fix:**
- After successful `new_part`, inject a hard `[BUILD enablement]` user message
  requiring `add_features` next (when allowlisted).
- After ≥2 `preflight_ops` with a prior `new_part` and no `add_features`, inject
  a stop-thrash nudge.
- Remove `new_part` from productive auto-complete set; if `new_part` ran and
  `add_features` is allowlisted but never succeeded, **do not** auto-complete as
  product success (`bootstrapped_only=true`, non-success so DAG can retry).
- `feature_modeler` system prompt: new_part alone is NOT done; cap preflight
  thrash before first add_features.

**Regression:** `test_new_part_without_add_features_not_auto_done`

---

## Verification

```text
python3 -m pytest services/agent/test_mega6_fixes.py -q
14 passed in 0.58s
```

Also: mega-5 suite non-regression earlier (28 passed with mega5+mega6).

## Live-run notes (attempt-2 final)

| Axis | attempt-1 | attempt-2 |
|---|---|---|
| provider_auth wipe | **19/19** openrouter 401 | **0** |
| wall | ~4s | **888.68s** |
| provider_usage | 0 | **192** rows (~xai-sub) |
| tools_slot SPECIFY tokens | n/a | **~601** (vs mega-5 2284) |
| stage_log live | n/a | intake→specify→plan/build/… |
| new_part | 0 | **2** (PLAN layout + BUILD DL1) |
| add_features | 0 | **0** (Fix7 targets this) |
| model_assignment | 0 | 0 (pre-Fix6 process) |
| delegate / join | 0 | 0 |
| mission | blocked auth | blocked deliver_incomplete |

- attempt-1 archived under `attempt-1-openrouter-auth-wipe/`.
- attempt-2 driver pinned `enabled_providers=[ollama, xai-sub]`; auto_pick_smoke
  **PASS**. Depth0 min_rank=3 filtered ollama rank1 → all depth0 on xai-sub
  (expected; cheap leaf needs depth≥2 delegate — none fired).
- BUILD DL1 allowlist confirmed mutators present; failure was **behavior**
  (preflight thrash), not enablement block — Fix7 addresses that.
- Python requires process re-import / relaunch for Fix6–7 binds.
- Watch-list next process: `add_features`≥1, non-xai leaf `model_assignment`,
  join handoff≠null with tokens_used, zero openrouter 401 without key.
