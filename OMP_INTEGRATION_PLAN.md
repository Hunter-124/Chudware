# OMP Integration — Live Execution Plan (main project, ~3h push)

Status board + contracts for wiring Hunter's private OMP platform (`Hunter-124/omp`) into
CAD's built-in copilot and firmware IDE. OMP is the reusable AI backbone for CAD,
CloudCastle, and future products; CAD keeps domain adapters and the native C++ fast path.
Detail + research live in `docs/omp-integration/` (`00-OPTIONS-AND-DECISIONS.md`,
`01-DECISIONS.md`, `research/`). This file is the top-level, always-current plan.

## Landed (on master)
- **Private-backbone pin** `vendor/omp/OMP_VENDOR.lock` — clean commit from
  `https://github.com/Hunter-124/omp.git`, deterministic and zero local patches.
- **Vendoring** `scripts/vendor_omp.py` + `vendor/omp/` — private OMP is the default source;
  seven runtime packages include `pi-agent-core`; prebuilt `pi-natives` comes from npm at the
  matching platform version.
- **Bridge** `vendor/omp/bridge` — bun sidecar: `complete`, provider/model catalog,
  `cost_summary`, `auth_*`, `memory_*`, `shake`, snap rendering, STT, and provider logos.
  Verified via bun stdio against private OMP: 4,500 models and 78 providers, with pricing and
  disabled-credential metadata complete. Speculative zero-caller compact/cache-warming RPCs
  were deleted under YAGNI; platform APIs are added when a real consumer ships.
- **Generated provider routing** — vendoring derives 61 bridge provider IDs from the private
  catalog, minus six CAD-native transports plus the `anthropic-sub` tenancy alias. Python and
  C++ consume generated outputs; seven private providers no longer require hand edits.
- **First harness extraction** — private `pi-agent-core` owns shake selection/mutation and a
  product-neutral JSON adapter; CAD keeps only JSON-RPC and image-block conversion. The
  duplicated Python fenced/XML/tool-result algorithm and fake `artifact://shake` link are gone.
- **C++ core** `core/agent/omp_bridge_client.{h,cpp}` + `providers.cpp` (52 OMP ids routed) +
  `config.cpp` + 12 `libchudware` C-ABI methods + `CompleteResult.usage`. `chudware_agent`/`chudware` build clean.
- **Provider UI** `settings_modal.cpp` (Settings→Providers: key+OAuth sign-in) + `copilot/settings.cpp`
  (model cost sublabels + session cost strip). Full `Chudware` builds+links.
- **Memory** `services/agent/{omp_bridge.py,project_memory.py,compaction.py}` — mnemopi/snapcompact
  hybrid, opt-in (`CHUDWARE_MEMORY_BACKEND`/`CHUDWARE_COMPACTION_BACKEND`), SQLite fallback.
- **Firmware** `core/firmware/hardware.cpp` (avrdude/OpenOCD-GDB/simavr/Renode/serial) + 12 RPCs +
  `fw_workspace.cpp` unified Arduino-style IDE. `test_firmware` 24/24; live Renode.

### Remaining — platform extraction, in dependency order
1. Move universal harness contracts and implementations from `services/agent` into private OMP:
   tool policy/budgets, events, roles/model assignment, compaction/memory coordination, durable
   inter-agent communication, and observability. Do not move CAD geometry/document tools.
2. Make CAD consume those contracts through the bridge; delete each Python/C++ duplicate only
   after the private implementation and CAD adapter pass the same behavior gates.
3. Keep the native C++ provider path where measured latency warrants it; OMP owns provider/auth
   semantics and catalog data, CAD owns the thin transport adapter.
4. Standalone OMP UI is owned by a concurrent OMP session. CAD must expose stable data contracts
   and consume the platform, not duplicate that UI work.
5. Cache-warming idle scheduler, installer bundling of bun/pi-natives, and remaining firmware
   breadth follow after the harness boundary is clean.

## Architecture (data flow)
```
host ImGui/WASM UI ──libchudware C-ABI (chudware_call)──▶ core/api/libchudware.cpp
   core ──OmpBridgeClient (stdio JSON-RPC)──▶ vendor/omp/bridge (bun) ──▶ OMP packages ──▶ vendor APIs
```
Provider completions run in the C++ core; the bridge is the OMP reuse seam. Memory/compaction
(Python services) call the bridge directly from Python where they run.

## RPC CONTRACT (freeze — every slice codes to these names)

### libchudware C-ABI methods (host UI ↔ core) — S-CORE implements, S-UI consumes
Each proxies to the bridge method in parens; params/results are JSON strings.
- `agent_provider_list`            → bridge `auth_list_providers`
- `agent_provider_state`{provider?} → `auth_state`
- `agent_provider_validate_key`{provider,apiKey} → `auth_validate_key`
- `agent_provider_set_key`{provider,apiKey} → `auth_set_key`
- `agent_provider_remove_key`{provider,account?} → `auth_remove_key`
- `agent_provider_begin_oauth`{provider} → `auth_begin_oauth`
- `agent_provider_oauth_poll`{handle} → `auth_oauth_poll`
- `agent_provider_oauth_manual`{handle,code} → `auth_oauth_manual`
- `agent_provider_oauth_cancel`{handle} → `auth_oauth_cancel`
- `agent_provider_logout`{provider,account?} → `auth_logout`
- `agent_models_list`{provider?} → bridge `models_list`
- `agent_cost_summary`{events?} → bridge `cost_summary`

### bridge methods (Python/core ↔ bun) — S-BRIDGE implements
- Providers/auth/models/cost: DONE (above).
- Memory: `memory_remember`{root,content,kind?,source?,importance?} → id;
  `memory_recall`{root,query,limit?,tokenBudget?,kind?} → hits[];
  `memory_stats`{root} → {...}. (mnemopi-backed; embeddings off by default.)
- Compaction: `compact`{messages,keepRecentTokens?,priorArchive?,mode?} →
  {keptMessages,summary,shortSummary,archive?,checksum,frames?}. Safety: per-frame checksum,
  recoverable side-channel, text fallback when no vision/native.

## SLICES (disjoint file ownership — run in parallel)
| Slice | Owns (files) | Deliverable | Verify |
|-------|--------------|-------------|--------|
| **S-BRIDGE** | `vendor/omp/bridge/src/*` | `memory_*` + `compact` bridge methods | `bun run` smoke |
| **S-PYMEM** | `services/agent/project_memory.py`, `services/agent/compaction.py` (+ small shims) | project_memory recall/remember + compaction route to bridge with safety, API unchanged | `pytest services/agent -q -k "memory or compact"` |
| **S-CORE** | `core/api/libchudware.cpp`, `core/agent/config.cpp`, `core/agent/providers.cpp/.h` (provider-id set + usage thread) | 12 libchudware proxy methods; wire OMP provider ids so signed-in providers drive live copilot; thread usage into CompleteResult | build `chudware_agent` |
| **S-UI-PROV** | `host/src/ui/settings_modal.cpp` (Settings→Providers), `host/src/ui/copilot/*` (model picker cost) | sign-in panel (key + Claude/Kimi OAuth), model picker w/ cost, per-session cost strip | build host; drive panel |
| **S-FW-CORE** | `core/firmware/hardware.cpp/.h`, `core/firmware/provision.cpp`, `services/mcp/firmware_tools.py`, `contracts/firmware_rpc.md` (§append) | avrdude (AVR flash) + OpenOCD/GDB debug + simavr + Renode + serial monitor RPCs | build; `pytest -k firmware` |
| **S-FW-UI** | `host/src/ui/firmware/fw_workspace.cpp` | unified board→build→flash→debug/serial + editor + AI copilot view | build host; drive panel |

Dependencies: S-UI-PROV consumes S-CORE's C-ABI names (frozen above → can build in parallel, stub if a
method 404s). S-CORE's provider-id set uses OMP catalog ids (from `listProviders`). Everything else disjoint.

## 3-hour order
1. Dispatch all 6 slices in parallel (this batch).
2. Orchestrator integrates any shared dispatch/CMake glue, runs builds (`chudware_agent`, host), fixes reds.
3. Priority to get RUNNING: S-CORE + S-UI-PROV (sign in → use provider in GUI). Then S-FW, S-PYMEM.
4. Commit + push each green slice. Smoke: launch app, sign into a provider, run a copilot turn.

## Guardrails
- Keep CAD governance authoritative (tool_registry gate, roles, guardrails). Bridge = reuse seam only.
- No `temperature`; keys `name/model/max_tokens/reasoning_effort`. Secrets never cross into UI.
- TS rules: no `any`/inline-cast-access/tiny-wrappers. Cite OMP file:line when reusing.
- Cross-OS + local/webui parity (WASM shares the ImGui tree; desktop-only RPC gated).
