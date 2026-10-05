# Adapter Serving Runbook (T-H9)

Per-task LoRA serving with vLLM and the standalone per-insertion-point routing,
canary, promotion and rollback library/tools. The live recipes need a real GPU +
vLLM. Unit-test coverage below is not evidence of a current live serving run.

- **Contract:** `contracts/providers.md` §7 (`routing`).
- **Code:** `services/serving/routing.py` (table + resolve + canary), `services/serving/promotion.py`
  (gate + rollback), `services/agent/providers/vllm_provider.py` (the `vllm` provider),
  `services/mcp/serving_tools.py` (MCP surface).
- **Adapters:** DVC-versioned under `training/adapters/<job_id>/` (T-H5/H6). That directory
  is produced by the training/flywheel pipeline (or `dvc_init ... pull`) and is not committed;
  it is absent until adapters exist. An `adapter_version` is that opaque id (e.g. `IP-13/v3`),
  NOT a semver.

The resolver invariant: **the fallback is always deterministic-path or frontier**
and cannot carry an experimental adapter. `RoutingTable.resolve()` returns that
decision when the adapter is unhealthy or a caller is canary-excluded. A caller must
execute the decision, respect offline/provider policy, and handle serving failures;
the routing table is not automatically wired into desktop agent-turn execution.

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
- Provider contract tests (no GPU):
  `python -m pytest services/agent/test_vllm_provider.py -q`. SDK-dependent cases
  skip if `openai` is unavailable; install the project's pinned requirements to
  exercise them. This is not a vLLM-server connectivity test or a pass-count claim.

### Local mode (P13 serving posture, no GPU)

Merged-GGUF-per-task via llama.cpp / LM Studio: set the route's `provider` to `llamacpp` and
point `base_url` at the loopback endpoint. Same routing table, same offline-safety rule.

## 2. Routing table in `config.json`

The table lives under top-level `"routing"` in the provider `config.json`.
The MCP tools reload and rewrite that subtree per call. A separate serving caller
must reload it to observe flips; changing it does not change `services/agent/routing.yaml`
role assignments or prove that an active desktop turn adopted an adapter.

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

Inspect the configured routes with `serving_status()`. It reports configuration,
not a log proving which model actually answered a previous call (§6a).

## 3. Canary flip 0% → 10% → 0% (routing logic; live serving requires a caller)

```
serving_set_canary(insertion_point="IP-13", pct=10)   # admit ~10% of non-opt-in users
serving_set_canary(insertion_point="IP-13", pct=0)    # rollback the canary; nobody admitted
```

- Bucketing is deterministic (`SHA-256(salt,ip,user)`), so a user is stably in/out and the
  percentage is reproducible; flipping the pct changes *who is admitted*, never the bucket
  assignment. The tool rewrites the `routing` subtree in place.
- `RouteDecision.telemetry_tag()` supplies a `model` block containing the selected
  adapter; the serving caller must splice it into `ai_artifact` events. The helper
  alone does not guarantee that every desktop response is tagged.
- Logic coverage: `python -m pytest services/serving/test_routing.py -q`
  (`test_canary_flip_live_zero_ten_zero`, `test_every_canary_response_is_tagged`).

## 4. Rollback (routing-table change)

```
serving_rollback(insertion_point="IP-13")                    # -> previous adapter in history
serving_rollback(insertion_point="IP-13", to_version="IP-13/v2")  # -> explicit version
```

A rollback changes the route, not the DVC-versioned adapter artifacts. With no
prior version, `adapter_version=None` makes `resolve()` choose the configured
fallback. Ordinary mutating-tool policy still applies; this is not an Ask/Plan
exception. Coverage: `test_rollback_*` in `services/serving/test_promotion.py`.

## 5. Fallback / kill-switch (resolver coverage)

When a serving caller catches an adapter failure, it can re-resolve with
`adapter_healthy=False` to obtain the deterministic/frontier fallback.
Canary-excluded decisions use the same fallback. The resolver does not issue
provider calls or guarantee a successful/error-free fallback, and must not bypass
offline-mode restrictions. Tests cover `test_unhealthy_adapter_degrades_without_error`,
`test_non_opt_in_user_excluded_at_zero_pct` and `test_rollback_with_no_history_drops_to_base`.

## 6. Promotion (HUMAN-GATED — Erik-ratified 2026-07-02)

There is **no auto-promote path**. Promotion requires BOTH automated evidence AND a
human's manual sign-off per promotion (`SignOff` with a non-empty `approver` and an existing
`report_ref`; the criteria were ratified by Erik on 2026-07-02):

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

Coverage for metrics-alone refusal, missing/invalid sign-off, missing report,
ineligible evidence and promotion with evidence + sign-off lives in
`services/serving/test_promotion.py` and `services/mcp/test_serving_tools.py`.

## 6a. Settings visibility — "which brain answered"

`serving_status()` exposes configured per-IP provider/model/adapter_version/canary%/
fallback to agents and MCP clients; it is not response-history telemetry.
No desktop GUI section renders this table today: Settings has a "Model routing" search entry
(`host/src/ui/settings_modal.cpp`), but it only lands on the Providers tab. Task
history: `work/tasks/done/T-H9.md`.

## 7. Two-OS reachability  `[~] live`

Linux GPU host: serve directly. Windows: WSL2 GPU passthrough (mirror
`tools/flywheel/WSL2_WINDOWS_SETUP.md`). In both cases the app reaches vLLM as a normal
loopback OpenAI-compatible provider — offline-safe only on a loopback `base_url` (the `vllm`
provider is classified like `llamacpp`/`openai`, NOT the never-offline cloud set).

## 8. Smoke test (operator, post-deploy)

1. `serving_status()` → confirm the configured routes and versions.
2. In an explicit serving caller, resolve IP-13, invoke the selected provider and attach
   `decision.telemetry_tag()`; confirm the real response and recorded adapter.
3. Flip `serving_set_canary("IP-13", 10)` then `serving_set_canary("IP-13", 0)`;
   reload the table and check sampled decisions.
4. Stop only the vLLM process owned by this smoke. Confirm the caller handles the failure,
   re-resolves unhealthy, and either executes a policy-permitted fallback or reports refusal.
5. `serving_rollback("IP-13")` → reload and confirm the previous adapter decision.
