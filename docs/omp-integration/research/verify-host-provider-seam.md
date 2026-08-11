# Verify: Host↔Provider Seam (C++ agent core → LLM completion)

## Headline correction to the plan

**The plan's row #1/#18 premise ("SIDECAR bridge `complete` op plugs into the provider
layer") needs a precise target: there is NO Python chokepoint for completions at all.**
`services/agent/providers/*.py` (`get_provider`/`_resolve_provider`/`complete_ex`) is a
**parallel, largely-dead-for-GUI-runs implementation**. The live path for every Design/Micro
agent run (`agent_run_v2` → `agent_run`/`agent_run_design`/`agent_run_micro`) is **100% C++**:
config resolution (`core/agent/config.cpp::resolve_provider`) AND the actual outbound HTTPS
call (`core/agent/providers.cpp::complete()` via libcurl/WinHTTP, `core/agent/http.cpp`) both
happen natively in the core process. Python is never invoked mid-run for a completion.
The one exception (**Cursor**) is currently a **hard refusal**, not a working Python fallback —
see below. This is the single most important correction: a bun "bridge" `complete` op must
plug into **`core/agent/providers.cpp`** (C++, libcurl), not `services/agent/config.py`.

## 1. C++ agent run loop — confirmed

- `core/agent/run.cpp:1` — header comment: "Mirrors agent/orchestrator/run.py (P53b)". This
  is a byte-parity C++ port of the whole orchestrator, not a thin shim.
- `run.cpp:372-397` — `make_provider(provider_cfg)` returns a `ProviderFn` closure. For a real
  provider it is: `return [name, cfg](system, messages, max_tokens) { CompleteResult cr =
  complete(name, cfg, system, messages, max_tokens); ... }` (`:385-389`) — i.e. it calls
  `chudware::agent::complete` (declared `core/agent/providers.h:176`) directly, **in-process, no RPC**.
  Role-scripted mock replies (`:374-384`) are the only branch that skips `complete()`.
  **CONFIRMED**, corrects the "python worker RPC in the loop" framing.
- `run.cpp:722/2411/3028` — `run_design`, `run_assembly_design`, `run_mission` each call
  `ProviderFn provider = make_provider(provider_cfg);` once per run/probe and thread it through
  every stage (intake, plan, modeler, vision-review, fix). **ONE provider instance for the whole
  run** (comment at `:721-722`: "ONE provider instance for the whole run (per-role scripted
  queues must persist)"). This is the single chokepoint per run.
- `run.cpp:740` — `supports_vision` is read straight off `provider_cfg` (`provider_cfg.value("supports_vision", false)`) to decide whether screenshots are attached — confirms callers depend on this exact key (item 4 below).
- `run.cpp:99-116` (`call_budget_for`) — reads `provider_cfg["max_tokens"]` (explicit override,
  clamped ≥512) else derives a base budget (4096 local / 8192 cloud) keyed on `provider_cfg["name"]`
  and bumped by `provider_cfg["reasoning_effort"]`. **CONFIRMED** exact `max_tokens` contract.

## 2. Where the C++ core enters — RPC surface (`core/api/libchudware.cpp`)

- `libchudware.cpp:1856` `agent_run`, `:2134` `agent_run_design`, `:2150` `agent_run_micro` —
  HOST-ergonomic entries `{request, doc_id, provider?, options?}`. These are the ONLY
  worker-RPC methods a GUI Design/Micro send reaches (host aliases `agent_run_v2`→`agent_run`
  at `:1061-1062`).
- `libchudware.cpp:1330-1337` `agent_resolve_provider` — `{name?, cfg, env}` → calls
  `chudware::agent::resolve_provider(name, cfg, env)` (native C++, `core/agent/config.cpp`), NOT a
  Python worker call. Used by the host to preview/validate a provider descriptor before a run
  (and by `agent_run` internally to resolve `provider_cfg` when the host passes bare config).
- **CORRECTION to plan's "worker RPC" framing**: `worker_rpc.py`/`services/agent/worker_rpc.py`
  in this repo is a GUI-facing table for **subscription auth/session-management RPCs**
  (`subscription_status/login/logout/switch/usage/ensure_fresh`, `session_compact/shake`,
  `skills_*`, `triggers_*`, `ai_roles_*`, `bench_*`) — it does **not** carry chat completions.
  Confirmed by reading the full method list registered there (`services/agent/worker_rpc.py:1-300`)
  and its own docstring (`:1-19`): "Single registration point for the worker-RPC tables ... over
  services.agent.providers" for **auth**, not completion.

## 3. In-process C++ vs Python worker — confirmed, with one real exception

**Completions happen in-process in C++** for: `anthropic`, `anthropic-sub`, `openai`, `llamacpp`,
`xai`, `xai-sub`, `moonshot`, `gemini`, `mock` (`core/agent/providers.cpp:1107-1533`, dispatch by
`provider` string). All of these build the wire request, POST via libcurl (`core/agent/http.cpp`),
and parse the response — zero Python involvement.

**Cursor is the sole gap**, and it is currently a dead end, not a working fallback:
- `core/agent/config.cpp:516-523` (`resolve_provider`) returns a hard `refusal(...)`:
  *"Cursor has no C++ chat completions transport ... Cursor Design runs route through the Python
  worker — this refusal firing means that routing was bypassed or the worker is down."*
- `core/agent/providers.cpp:1522-1526` (`complete()`) — same refusal if reached anyway:
  *"cursor: no C++ chat completions transport (Connect-RPC/protobuf only). Pick xAI, Claude, or
  OpenAI."*
- `host/src/ui/copilot/composer.cpp:287-290` — **contradicts the "routes through Python worker"
  comment**: *"T-RF5: single agent entry — every Design send goes to agent_run_v2 ... A cursor
  pick now reaches the core's resolve_provider, which returns a classified pre-run refusal."*
  **DISAGREEMENT FLAGGED**: the config.cpp comment describes an *intended* Python-worker route
  for Cursor that does not exist today; the GUI comment confirms Cursor Design runs currently
  just fail with a refusal card. There is no live "Python does completions for provider X" path
  in the current tree — Cursor's actual Connect-RPC driver (`services/agent/providers/
  cursor_sub_provider.py`, 1200+ lines) is reachable only from Python-only surfaces (advisor
  tests, MCP tools, subscription usage polling), never from the live GUI agent run.
- CLI-driven providers (`claude_cli_provider.py`, `codex_cli_provider.py`, `codex_sub_provider.py`,
  `kimi_sub_provider.py`) are **also absent** from both `config.cpp::resolve_provider` and
  `providers.cpp::complete()` — grepped, no matches. These Python provider classes exist but are
  not wired to the live C++ run loop at all today; `kimi-sub` model coverage in `providers.cpp`
  is actually served through the openai-compatible branch (`:1334-1370`, treated as an
  OpenAI-shaped endpoint), not through `kimi_sub_provider.py`. `codex_sub`/CLI providers have
  **no native completion path** currently — presumably GUI-hidden/unreachable for Design runs
  (not verified further; out of scope for the seam question, flagged for follow-up).

**Conclusion for the bridge**: the single best insertion point is
**`core/agent/providers.cpp::complete()`** (native C++), specifically as a new branch
(e.g. `provider == "sidecar"`) that shells a request to the bun sidecar over stdio/socket and
returns a `CompleteResult`, OR equivalently a new `ProviderFn` alternative wired into
`make_provider()` in `run.cpp:372`. Routing through Python (`services/agent/config.py`) would be
building on the *dead* path for GUI-driven Design/Micro runs — it only serves non-GUI Python
call sites (advisor, MCP tools, tests, subscription usage).

## 4. Attributes/kwargs callers depend on — CONFIRMED / CORRECTED

| Claim | Verdict | Evidence |
|---|---|---|
| `name` | CONFIRMED | `provider_cfg.value("name", ...)` read throughout run.cpp (`:108,809,935,939,982,2253,2379,2383`) and dispatched on in `providers.cpp::complete()` (`:1107,1334,1468,1522`) and `config.cpp::resolve_provider` (`if (name == "anthropic")...`, `:370-533`). |
| `supports_vision` | CONFIRMED | `run.cpp:740` (`provider_cfg.value("supports_vision", false)`), `run.cpp:752/756-ish` (review_provider vision flag from nested descriptor), set by `config.cpp` per-provider (e.g. `:482` xai `true`, `:513` moonshot `oai_vision`, `:529` llamacpp `false`). Python `Provider.supports_vision` class attr (`base.py:33`) is the Python-side mirror, unused by the live C++ path. |
| catalog meta (context/vision/tools/reasoning_efforts) | CONFIRMED, but lives in **`agent_list_models`** (`libchudware.cpp:1339-1413`, `heuristic_meta` lambda), a SEPARATE RPC from completion — used by the GUI model picker, not by the run loop itself. Not part of `provider_cfg` at call time. |
| `temperature` kwarg | **CORRECTED — does not exist.** Grepped whole repo: no provider (`complete`/`complete_ex` in Python, or `complete()` in C++) takes or forwards a `temperature` parameter. Anthropic "adaptive" models explicitly **reject** temperature/top_p (`core/agent/providers.cpp:509-511`: "Adaptive models reject temperature/top_p/budget_tokens; effort rides on output_config"). The only tunable is `reasoning_effort` (string: off/low/medium/high/max/xhigh), mapped per-backend. A bridge implementation MUST NOT assume a `temperature` kwarg is threaded through. |
| `_mega6_degraded` | **CORRECTED — does not exist anywhere in the repo.** Zero grep hits for `_mega6_degraded` (or `mega6`) in CAD or vendored OMP paths searched. This looks like a fabricated/misremembered symbol from the earlier research round; drop it from the contract. |
| `max_tokens` | CONFIRMED | `provider_cfg["max_tokens"]` explicit override in `call_budget_for` (`run.cpp:104-107`, clamped ≥512, no ceiling) and threaded as the last positional arg through `complete(name, cfg, system, messages, max_tokens)` (`providers.h:176-177`). Python mirror: `Provider.complete(..., max_tokens: int = 8000)` (`base.py:35`) — same name, same units (output tokens), separate implementation. |

## Summary of verdicts

- CONFIRMED: C++-only, in-process completion path (`run.cpp::make_provider` → `providers.cpp::complete()` → libcurl); ONE `ProviderFn` per run is the chokepoint; `agent_resolve_provider`/`agent_run*` are the only relevant RPCs; `name`/`supports_vision`/`max_tokens` contract.
- CORRECTED: no Python "worker RPC does completions" path exists for GUI runs — `services/agent/worker_rpc.py` only carries auth/session/skills RPCs, not chat completion; `services/agent/config.py::get_provider/_resolve_provider` is a parallel, GUI-run-dead implementation.
- CORRECTED: `temperature` kwarg does not exist in either language's provider contract.
- CORRECTED: `_mega6_degraded` does not exist anywhere in the repo — drop from spec.
- FLAGGED DISAGREEMENT: `config.cpp` comment claims Cursor "routes through the Python worker"; `composer.cpp` comment (more recent, T-RF5) confirms Cursor Design runs simply hit a refusal today — no working Python fallback exists in the live GUI path.
- Best bridge insertion point: a new provider branch inside `core/agent/providers.cpp::complete()` (and/or a new `ProviderFn` variant wired into `run.cpp::make_provider`), talking to the bun sidecar directly from C++ — NOT via `services/agent/config.py`.
