# Adapter Serving Runbook (T-H9)

Per-task LoRA serving with vLLM, per-insertion-point routing, canary rollout, promotion, and
rollback. This is the operator guide **and** the run-instructions for the live-gated
Definition-of-Done items (which need a real GPU + vLLM and cannot run in CI).

- **Contract:** `contracts/providers.md` §7 (`routing`).
- **Code:** `services/serving/routing.py` (table + resolve + canary), `services/serving/promotion.py`
  (gate + rollback), `services/agent/providers/vllm_provider.py` (the `vllm` provider),
  `services/mcp/serving_tools.py` (MCP surface).
- **Adapters:** DVC-versioned under `training/adapters/<job_id>/` (T-H5/H6). An `adapter_version`
  is that opaque id (e.g. `IP-13/v3`), NOT a semver.

The design invariant everywhere: **the fallback is always deterministic-path or frontier**
(kill-switch). A serving failure or a canary-excluded call degrades to it with no user-facing
error. Nothing below can route a call onto an experimental adapter as a fallback.

---

## 1. Serve two adapters from one vLLM instance  `[~] live — needs GPU + vLLM`

vLLM is an **external** OpenAI-compatible server (Apache-2.0; we never import it — see
`contracts/licenses.md`). Launch one instance with `--enable-lora` and register each
insertion point's adapter as a named LoRA module:

```bash
# On the serving box (Linux GPU host; Windows via WSL2 GPU per WSL2_WINDOWS_SETUP.md).
# adapter names here == the routing table's adapter_version strings.
vllm serve Qwen/Qwen3-4B \
    --enable-lora \
    --max-lora-rank 32 \
    --lora-modules \
        "IP-13/v3=$PWD/training/adapters/job_ip13_v3" \
        "IP-1/v2=$PWD/training/adapters/job_ip1_v2" \
    --port 8000
```

- The LoRA is selected **by the request `model` field** = the served adapter name. The `vllm`
  provider does exactly this: `VLLMProvider(base_model="Qwen/Qwen3-4B", adapter="IP-13/v3")`
  sends `model="IP-13/v3"`. Base model is requested by its own name.
- Pull the adapters first: `python -m tools.flywheel.dvc_init --repo training pull adapters/<job_id>`.
- Verify the SDK provider path locally (no GPU): `pip install openai==2.41.0 &&
  python -m pytest services/agent/test_vllm_provider.py -q` → 9 passed (in CI `openai` is absent → the
  5 SDK cases skip, like the whole openai/anthropic/openrouter family).

### Local mode (P13 serving posture, no GPU)

Merged-GGUF-per-task via llama.cpp / LM Studio: set the route's `provider` to `llamacpp` and
point `base_url` at the loopback endpoint. Same routing table, same offline-safety rule.

## 2. Routing table in `config.json`

The routing table lives under the top-level `"routing"` key of the same `config.json` as
provider settings, so every flip is one single-file edit, no restart:

```jsonc
{
  "provider": "vllm",
  "vllm": {"model": "Qwen/Qwen3-4B", "base_url": "http://localhost:8000/v1"},
  "routing": {
    "IP-13": {
      "provider": "vllm", "model": "Qwen/Qwen3-4B", "adapter_version": "IP-13/v3",
      "fallback": {"mode": "frontier", "provider": "anthropic", "model": "claude-opus-4-8"},
      "canary": {"pct": 0, "opt_in": [], "salt": "IP-13"},
      "history": []
    }
  }
}
```

Read it any time with the MCP tool `serving_status()` — this is the read-only "which brain
answered" data the Settings surface renders (§6).

## 3. Canary flip 0% → 10% → 0%  (`[~]` live tag; logic CI-tested)

```
serving_set_canary(insertion_point="IP-13", pct=10)   # admit ~10% of non-opt-in users
serving_set_canary(insertion_point="IP-13", pct=0)    # rollback the canary; nobody admitted
```

- Bucketing is deterministic (`SHA-256(salt,ip,user)`), so a user is stably in/out and the
  percentage is reproducible; flipping the pct changes *who is admitted*, never the bucket
  assignment. No restart — the tool rewrites the `routing` subtree in place.
- Every adapter-served response is telemetry-tagged: `RouteDecision.telemetry_tag()` threads
  the selected adapter into the `ai_artifact` `model.adapter` slot (T-H1). Canary responses
  (`is_canary`) are distinguishable from fully-promoted production traffic.
- CI proof of the logic: `python -m pytest services/serving/test_routing.py -q`
  (`test_canary_flip_live_zero_ten_zero`, `test_every_canary_response_is_tagged`).

## 4. Rollback in < 1 minute

```
serving_rollback(insertion_point="IP-13")                    # -> previous adapter in history
serving_rollback(insertion_point="IP-13", to_version="IP-13/v2")  # -> explicit version
```

A rollback is a pure routing-table flip. Nothing is overwritten (adapters are DVC-versioned and
immutable), no confirmation is required — rolling back is never dangerous. If there is no prior
version, the route drops to the base model and `resolve()` serves the kill-switch fallback.
CI proof: `test_rollback_*` in `services/serving/test_promotion.py`.

## 5. Fallback / kill-switch (proven)

When the adapter is unavailable, the caller re-resolves with `adapter_healthy=False` and gets
the deterministic/frontier fallback — no user-facing error. Canary-excluded traffic runs the
same safe fallback (the incumbent). CI proof: `test_unhealthy_adapter_degrades_without_error`,
`test_non_opt_in_user_excluded_at_zero_pct`, `test_rollback_with_no_history_drops_to_base`.

## 6. Promotion (HUMAN-GATED — Erik-ratified 2026-07-02)

There is **no auto-promote path**. Promotion requires BOTH automated evidence AND Erik's
manual sign-off per promotion:

```
# 1. Automated evidence from a real T-H7 shadow log (writes the report Erik reviews):
serving_evaluate(insertion_point="IP-13", shadow_log="shadow.jsonl",
                 min_calls=100, report_out="evidence_IP-13.txt")
#    -> {eligible: true} iff challenger beats-or-matches incumbent on BOTH
#       verifier-score AND accept-rate over >= 100 shadow jobs. Necessary, NOT sufficient.

# 2. Erik reviews evidence_IP-13.txt and signs off by supplying approver + report_ref:
serving_promote(insertion_point="IP-13", adapter_version="IP-13/v4",
                report_ref="evidence_IP-13.txt", approver="erik",
                shadow_log="shadow.jsonl")
#    -> refused unless the report exists, the evidence is eligible, AND a valid sign-off is
#       supplied. On success: routing flips to IP-13/v4 at 100%, previous version kept for rollback.
```

CI proves metrics-alone-are-not-sufficient (`test_metrics_alone_are_not_sufficient`),
no-signoff-refused, report-missing-refused, ineligible-refused, and the happy path
(`services/serving/test_promotion.py`, `services/mcp/test_serving_tools.py`).

## 6a. Settings visibility — "which brain answered"

The read-only data is `serving_status()` (per-IP provider/model/adapter_version/canary%/
fallback). Wiring it into the desktop Settings panel is a C++ read-side seam: the imgui and
legacy settings panels (`ui/imgui/.../settings_modal.cpp`, `host/src/ui/settings_modal.cpp`)
have a "Model routing" section and diverged duplicate copies (GUI-parity churn), so per the
first-claimer / hot-spot rule this task exposes the DATA and leaves the panel line to the
G-wave owner. See `work/questions/T-H9.md`.

## 7. Two-OS reachability  `[~] live`

Linux GPU host: serve directly. Windows: WSL2 GPU passthrough (mirror
`tools/flywheel/WSL2_WINDOWS_SETUP.md`). In both cases the app reaches vLLM as a normal
loopback OpenAI-compatible provider — offline-safe only on a loopback `base_url` (the `vllm`
provider is classified like `llamacpp`/`openai`, NOT the never-offline cloud set).

## 8. Smoke test (operator, post-deploy)

1. `serving_status()` → confirm the two routes + adapter versions.
2. Route a call at IP-13 with the adapter healthy → response tagged `model.adapter="IP-13/v3"`.
3. `serving_set_canary("IP-13", 10)` → ~10% admitted; `serving_set_canary("IP-13", 0)` → 0%.
4. Kill the vLLM process → a call degrades to the frontier fallback, no user error.
5. `serving_rollback("IP-13")` → previous version restored in seconds.
