# mega-16 harness fixes (Fixer16 = BoldSilverfish)

Baseline: `c269e326`. Pair: MegaRun16=`NeutralCatfish` owns `reports/runs/mega-16/**` +
roadmap; Fixer16 owns routing/rank fixes + gym + this file.

Axes targeted:
- **Y** auto_pick routes frontier nodes to openrouter **paid** tier when xai is
  spending-limit-blocked (no refusal loops, no ollama-all collapse).
- **W / X** remain mega-15 bound (wire/power force-inject + post-wire ERC + battery
  adapters) — not re-touched here; live exercise needs a working frontier.
- Target: ≥8 live AC passes. Regression: A–V hold.
- Frontier for this cycle: `openrouter:deepseek/deepseek-chat-v3.1` (PAID, no `:free`).
- xAI SuperGrok stays blocked — **do not** attempt grok-4.5 revalidation.

Criteria were **not** weakened. Battery never weakened.

---

## Fix 1 — Register paid deepseek as rank-5 frontier (axis Y)

**Fingerprint (mega-15 a1/a2):** xAI both accounts `personal-team-blocked:spending-limit`;
OpenRouter free pool 429 free-models-per-day; local 4B could not reach placement. No
paid openrouter id was ranked ≥ frontier floor, so hard nodes either looped xai or
collapsed to ollama.

**Fix:**
1. `model_rankings.yaml`: ship `deepseek/deepseek-chat-v3.1` + stem alias
   `deepseek-chat-v3.1` at **rank 5**, provider openrouter.
2. `role_models._FRONTIER_RANK_FLOOR_IDS` + measured-merge stems include deepseek
   so gym demotion cannot drop below floor 5.
3. `_enabled_providers`: when any openrouter key is present, offer paid
   `deepseek/deepseek-chat-v3.1` (and `qwen/qwen3-coder-30b-a3b-instruct`) **before**
   free-tier candidates.
4. `auto_pick_model`: floor rank for deepseek ids even if unranked in the passed table.
5. `provider_for_tier("openrouter:deepseek/deepseek-chat-v3.1")` +
   `config.get_provider("openrouter")` honour `config.keys.openrouter` (GUI keys map)
   in addition to `openrouter.api_key` / env.

**Regression:** `test_mega16_fixes.py` (provider_for_tier, shipped rank, measured floor).

---

## Fix 2 — xAI spending-limit → provider_unavailable for auto_pick (axis Y)

**Fingerprint (mega-15 a1 ledger):** every hard node raised
`XaiTierDeniedError` / `personal-team-blocked:spending-limit` classified as
generic 403/`provider_auth` fatal — no fall-through to next ranked provider;
mission burned on xai retry loops.

**Fix:**
1. `role_models.mark_provider_unavailable` / `is_provider_unavailable` /
   `is_provider_unavailable_error` — process-local set; marking `xai` or `xai-sub`
   blocks **both**.
2. `auto_pick_model` + `_enabled_providers` skip unavailable providers.
3. `agent_turn.classify_provider_error`: spending-limit / tier-denied markers
   return `stop_reason=provider_unavailable` (non-retryable) **before** generic
   403→`provider_auth`.
4. On that stop reason, `mark_provider_unavailable(provider)` so subsequent nodes
   pick openrouter paid instead of re-hitting SuperGrok.

**Regression:** spending-limit classifier + mark/skip auto_pick tests in
`test_mega16_fixes.py`.

---

## Fix 3 — Depth-0 hard → openrouter paid; leaf → ollama (axis Y contract)

**Acceptance cases (unit):**
| Case | Expected |
|---|---|
| depth-0 hard / analysis min_rank≥4, xai blocked | `openrouter` + `deepseek/deepseek-chat-v3.1`, rank≥5 |
| leaf / simple min_rank=1, xai blocked | `ollama` + `llama3.2*` |
| analysis min_rank=4 with only openrouter+ollama | openrouter paid, **not** ollama |

---

## Test command

```text
python3 -m pytest services/agent/test_mega16_fixes.py \
  services/agent/test_mega11_legs.py \
  services/agent/test_mega11_grading.py -q
```

**Result (this session):** `80 passed`.

---

## Files touched

| Path | Change |
|---|---|
| `services/agent/model_rankings.yaml` | deepseek paid rank 5 + stem alias |
| `services/agent/role_models.py` | floor ids, unavailable set, keys.openrouter, paid candidates, auto_pick skip |
| `services/agent/agent_turn.py` | spending-limit → provider_unavailable + mark |
| `services/agent/config.py` | get_provider openrouter reads keys.openrouter |
| `services/agent/tiers.py` | provider_for_tier openrouter keys map + write oc |
| `services/agent/test_mega16_fixes.py` | new unit suite |
| `reports/runs/mega-16/fixes.md` | this log |

---

## Ready for MegaRun16 primary launch

Fixes 1–3 are **bound and unit-green**. DM `NeutralCatfish` `fixes bound`.

Expected on primary (frontier `openrouter:deepseek/deepseek-chat-v3.1` PAID,
spend guard $8; xai left blocked):
- Hard/analysis nodes auto_pick openrouter paid deepseek (rank 5), not xai loop,
  not ollama-all
- Leaves stay ollama
- mega-15 W inject + post-wire ERC + battery adapters exercise live on paid model
- Path to **≥8 live AC PASS** while A–V hold
- grok-4.5 revalidation remains **deferred** (xai spending-limit)
