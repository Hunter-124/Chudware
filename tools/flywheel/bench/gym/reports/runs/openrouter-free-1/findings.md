# Findings — openrouter free diags (gemma-4-31b-it:free + qwen3-coder:free)

Cycle dir: `reports/runs/openrouter-free-1/` (diag suite, not a gym C-matrix cycle).
Artifacts: `reports/diag/diag-latest.json` (and timestamped siblings), plus
`diag_summary.json` when a full pass completes.

## What we ran

```
python3 -m tools.flywheel.bench.gym.cli diag \
  --tiers openrouter:google/gemma-4-31b-it:free,openrouter:qwen/qwen3-coder:free
```

Retry machinery is landed (`diag/transport.py` `complete_with_retry` +
`finalize_axis_result` → `score=None` when >50% transport errors).

## Observed outcomes (honest)

### 1) Config key resolution bug (first attempts → false 0.0 scores)

- Ambient `config.json` has `provider=gemini` and top-level `api_key` (Gemini).
- OpenRouter key lives at `keys.openrouter`.
- `provider_for_tier("openrouter:…")` prefers `openrouter.api_key` → top-level
  `api_key` → `OPENROUTER_API_KEY`, and **does not read `keys.openrouter`**.
- Result: `AuthenticationError 401 Missing Authentication header` on every task,
  scored as **0.0** (auth is not classified as transport → not nulled).
- **Tooling fix:** `provider_for_tier` should consult `cfg["keys"]["openrouter"]`
  before the Gemini top-level `api_key`. Until then, free-tier diags need
  `OPENROUTER_API_KEY` exported or `openrouter.api_key` set.

### 2) After correct key: upstream free-tier rate limit (429)

Smoke with `OPENROUTER_API_KEY=keys.openrouter`:

```
RateLimitError 429 — qwen/qwen3-coder:free is temporarily rate-limited upstream.
Please retry shortly, or add your own key…
```

This **is** a transport-class error. With retry machinery + finalize:

- Exhausted retries → task `error: true`
- If >50% tasks transport-error → axis `score=null` (untested) — honest.

Nulls under free-tier rate limit are **not** model incapacity.

### 3) Offline mode footgun

Default config has `offline_mode: true`. OpenRouter is refused with
`OfflineModeError` unless the diag process forces offline off. Gym/diag entry
should either document this or auto-disable offline for explicit cloud tiers.

## Tooling improvements

1. **Read `keys.openrouter` in `provider_for_tier`** (and mirror for other
   providers that store keys under `keys.*`). Prevents silent 401→0.0.
2. **Classify 401-from-missing-key separately** from model failure in diag
   finalize (config_error axis note), so leaderboard never treats auth setup as
   capability 0.0.
3. **Cloud-tier diag entry**: if tier is `openrouter:`/`xai:`, force
   `offline_mode=false` for that process or fail with a clear preflight message
   before any paid/free call.

## Status for leaderboard

- No trustworthy free-model capability scores this wave.
- Expected honest state under current free-tier pressure: **null / transport**,
  not 0.0.
- Re-run when OpenRouter free capacity recovers, with `OPENROUTER_API_KEY` set
  from `keys.openrouter` and offline disabled.
