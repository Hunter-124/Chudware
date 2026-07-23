# Contract: Providers & Session Layer — v1

> The frozen NEUTRAL seam every agent surface shares. Three implementations conform to
> it: the Python adapters (`services/agent/providers/`), the C++ port
> (`core/agent/providers.{h,cpp}` + `core/agent/run.cpp`), and external MCP clients that
> ride the user's own subscription. Adding a provider = one contract-conformant adapter.
> Append-only: any change to a frozen section needs integrator sign-off + a version bump
> (see `contracts/README.md`). Pillar 12 of `docs/AI_TOOLING_MASTER_PLAN.md` is the source
> of intent; the **Prohibited auth** list below is binding and CI-tested.

## 1. Message schema (frozen)

A conversation is `system: str` + `messages: list[Message]`.

```
Message = {"role": "user" | "assistant", "content": [Part, ...]}
Part    = {"type": "text",  "text": str}
        | {"type": "image", "path": str}          # local PNG path, base64'd by the adapter
```

- `role` is exactly `"user"` or `"assistant"`. Gemini's wire `"model"` role is an adapter
  detail: adapters map `assistant`↔`model` internally; the neutral layer never uses `model`.
- Text-only messages MAY be collapsed by an adapter to its most-compatible wire form (e.g.
  OpenAI plain-string `content`); this is an adapter concern, invisible to callers.
- Image parts carry a filesystem `path`, NOT bytes. Each adapter reads + base64-encodes at
  send time into its provider block (Anthropic `source`, OpenAI `image_url` data-URI,
  Gemini `inline_data`). Media type is `image/png`.
- Helper constructors are normative: `text_part(text)`, `image_part(path)`, `user(parts)`,
  `assistant(parts)` (`services/agent/providers/base.py`); C++ mirrors `text_part()`,
  `image_part()`, `user_message()` (`core/agent/providers.h`).

## 2. Adapter interface (frozen)

```python
class Provider:
    name: str                      # stable id: "anthropic"|"openai"|"gemini"|"llamacpp"
                                   #            |"openrouter"|"mock"|"mock-roles"
    supports_vision: bool = True   # False -> loop sends stats-only feedback (no screenshot)
    offline_safe: bool = False     # opt-in: True asserts the adapter makes NO network call

    def complete(self, system, messages, max_tokens=8000) -> str
    def complete_ex(self, system, messages, max_tokens=8000) -> tuple[str, str]  # (text, thinking)
```

- `complete_ex` returns `(reply_text, thinking_text)`. `thinking_text` is the model's
  extended-reasoning trace when the backend exposes one, else `""`. `complete` == the
  `text` half. The loop surfaces `thinking` in the run timeline.
- C++ mirror: `CompleteResult {text, thinking, error}` from
  `complete(provider, cfg, system, messages, max_tokens)` (`core/agent/providers.h`);
  request/reply staging is `BuiltRequest {url, headers, body, body_json}` /
  `ParsedReply {text, thinking}`.

### 2.1 Streaming (frozen shape, optional to implement)

The neutral contract is request/response: `complete_ex` returns the full reply. Streaming is
an OPTIONAL adapter capability that MUST NOT change the return contract — a streaming adapter
accumulates deltas internally and still returns the same `(text, thinking)` tuple. Incremental
UI updates travel over the existing event stream (`contracts/events.md` `agent_output`), never
by changing this signature. No adapter is required to stream; non-streaming is conformant.

### 2.2 Thinking channel (frozen)

Reasoning traces are returned OUT-OF-BAND in the `thinking` slot, never inlined into `text`.
Adapters extract it from their native shape: Anthropic `thinking` blocks; Gemini parts with
`thought: true`; OpenAI-compatible `reasoning_content`/`reasoning` (used as a `text` fallback
only when `content` is empty). Callers treat `thinking` as advisory display, never as ops.

## 3. Token budgets (frozen defaults)

`call_budget_for` (C++ `core/agent/run.cpp:48-63`) is the reference:

| provider class | base `max_tokens` |
|---|---|
| local / unknown (llamacpp, local openai, mock) | 4096 |
| cloud (anthropic, gemini, openai, openrouter) | 8192 |
| `+ reasoning_effort in {high, max}` | + 4096 headroom |

- An explicit `provider_cfg.max_tokens` overrides (clamped to ≥ 512).
- The output budget is separately clamped against the provider's context window by
  `fit_call_budget` (C++ `core/agent/config.cpp`); context windows: anthropic 200000,
  gemini 1048576, openai 128000, else 8192.
- Python callers pass `max_tokens` explicitly (default 8000). The 4096/8192 split is the
  C++ reference the Python side SHOULD converge on when it grows a budget resolver.

## 4. Auth kinds (frozen)

Every provider declares exactly one auth kind:

| kind | meaning | storage |
|---|---|---|
| `api_key` | BYO key (pros/enterprise). Anthropic/OpenAI/Gemini/OpenRouter direct. | `config.json` `api_key`, else provider env var (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`/`GOOGLE_API_KEY`, `OPENROUTER_API_KEY`) |
| `oauth_pkce` | "Sign in with OpenRouter" — the ONE blessed session login. | key minted by PKCE exchange, then stored like `api_key` |
| `none` / `local` | local model server (LM Studio / llama.cpp / Ollama), loopback endpoint. | none required (`"lm-studio"`/`"llama-cpp"` placeholder) |

Resolution precedence (both ports): explicit arg → `CHUDWARE_PROVIDER` env → `config.json`
`provider` → default (local loopback openai-compatible). Config file:
`%APPDATA%/Chudware/config.json` (Windows) / `$XDG_DATA_HOME/Chudware/config.json` (POSIX).

### 4.1 OpenRouter PKCE (blessed default session auth)

The one sanctioned third-party session login (OpenRouter documents it for third-party apps;
one login → hundreds of models → user-balance billing). Flow (S256 PKCE):

1. Generate `code_verifier` (43–128 url-safe chars) and
   `code_challenge = BASE64URL(SHA256(code_verifier))`.
2. Open `https://openrouter.ai/auth?callback_url=<cb>&code_challenge=<cc>&code_challenge_method=S256`.
3. Receive `code` on the callback.
4. `POST https://openrouter.ai/api/v1/auth/keys` with
   `{"code", "code_verifier", "code_challenge_method": "S256"}` → `{"key": "sk-or-..."}`.
5. Store the minted key via the normal `config.json` `api_key` mechanism; thereafter the
   `openrouter` adapter is an OpenAI-compatible client against
   `https://openrouter.ai/api/v1` (base_url).

- **Feature-flagged.** The PKCE login is inert unless enabled: config `openrouter_pkce: true`
  or env `CHUDWARE_ENABLE_OPENROUTER_PKCE=1`. Disabled → helper returns a contract-shaped
  `{ok: false, reason, hint}`, never a traceback.
- The network exchange is transport-injectable so CI exercises the full flow with a fake
  transport and NO network/secrets.

### 4.2 Prohibited auth (CI-enforced — see §4.4 for authorized exceptions)

By default, consumer Claude Pro/Max or Gemini subscription OAuth tokens MUST NOT appear in
Chudware code outside of explicitly allowlisted provider files. Gemini consumer OAuth remains
fully prohibited (Google mass-bans). A CI assertion greps the tree and fails if code
references consumer OAuth token-reuse endpoints for Claude/Gemini in non-allowlisted files
(`test_prohibited_auth.py`). The allowlist covers:
- This contract and the test itself (legitimate references)
- The subscription-auth provider modules under `services/agent/providers/` (§4.4)

### 4.4 Subscription auth (opt-in, owner-authorized — 2026-07-07)

Owner-authorized subscription-auth providers allow the app to authenticate using the user's
own paid Pro/Max/Plus subscription for Claude, Codex, and Cursor — the same first-party
OAuth flow those vendors' own CLI/IDE clients use. This is an **opt-in** feature: the user
must explicitly log in through their browser; no subscription token is ever sent without
the user initiating the flow.

Providers: `codex-sub` (OpenAI Codex/ChatGPT Plus), `anthropic-sub` (Anthropic Claude
Pro/Max), `cursor-sub` (Cursor Pro). Each lives in its own module under
`services/agent/providers/` and is allowlisted in `test_prohibited_auth.py`.

Credentials (access_token, refresh_token, expires_at_ms, account_id, email) are stored in
`config.json` under the `subscription_auth` key, keyed by provider_id. Tokens are
refreshed automatically before expiry (per-provider skew: Codex none, Anthropic 5min,
Cursor 5min). Auth retry ladder: retry same → force-refresh → re-login prompt.

These providers are NEVER offline-safe (cloud endpoints, §5).

### 4.3 Chudware Cloud relay (config seam ONLY — human-gated)

The company-hosted inference relay is **one config entry, no code path**: an
OpenAI-compatible provider with `base_url` = the relay endpoint and an account-tied auth
kind. It reuses the existing `openai` adapter unchanged. Rollout, quota, and billing are a
separate later project gated on the human driver (Pillar 12). This contract only reserves the
seam; do not implement a relay client.

## 5. Offline / ITAR mode (frozen invariant)

When `config.json` `offline_mode: true`, NO technical data may leave the machine. Only
loopback-endpoint providers are allowed; cloud providers are refused BEFORE construction and
re-checked after (defense in depth) — `provider_offline_safe` / `assert_offline_safe`
(`services/agent/config.py`). Frozen classification:

| provider | offline-safe? |
|---|---|
| `mock`, `mock-roles`, `base`, or `offline_safe=True` | always yes |
| `anthropic`, `gemini`, `openrouter` | never (cloud) |
| `openai`, `llamacpp` | only if `base_url` host is loopback (`localhost`/`127.0.0.1`/`::1`) |

Adapters MUST keep this invariant. New cloud providers (e.g. `openrouter`) MUST be added to
the never-offline-safe set.

## 6. Tool registry, tags & allow-listing (frozen)

Every MCP tool carries `{family, risk}`. Enforcement is ONE check at dispatch, not per-tool
edits — `services/mcp/tool_registry.py` + the wiring in `services/mcp/server.py`.

- **family** — the owning tool module: `cad | gsl | recon | intake | ecad | cam`
  (declared once per `register_*_tools()` call site).
- **risk class** — one of:
  | risk | meaning | examples (by name pattern) |
  |---|---|---|
  | `read` | inspects only, no state change | `describe_*`, `query_*`, `measure_*`, `list_*`, `get_*`, `check_*`, `render_*`, `hover`, `*_status` |
  | `write` | mutates local document / project state | `new_*`, `add_*`, `set_*`, `cam_add_*`, `ecad_*` edits |
  | `spend` | consumes metered/paid resource (model spend, cloud compute) | reserved; none today |
  | `external` | leaves the machine (export, network fetch, upload) | `export_*`, `submit_*`, `*_post` |

  Risk is derived by a name-pattern classifier + an explicit override table in the registry
  (the tool modules are NOT edited). Unknown/unclassified defaults to `write` (fail-safe:
  treated as mutating).

### 6.1 Allow-list semantics (frozen)

- **Default = everything allowed** (no behavior change vs. today). An allow-list is opt-in.
- Sources, most specific first: explicit call arg → env `CHUDWARE_TOOLS_ALLOW`
  (comma-separated tool names and/or `family:<name>`) → config `tools.allow`. `CHUDWARE_TOOLS_DENY_RISK`
  (comma-separated risk classes) additionally denies whole risk classes.
- Enforcement point: the dispatch wrapper around `mcp.call_tool(name, args)`. A disallowed
  call returns a **contract-shaped** `{"ok": false, "reason": ..., "hint": ...}` — never a
  traceback, never a silent success.
- The DAG node contract (T-A1 `dag_schema`) passes explicit per-node `tools:` allow-lists;
  this registry is what those lists bind against. Per-context lists arrive later; the seam is
  frozen now.

## 7. `routing` — per-insertion-point adapter routing (T-H9, additive)

Per-task LoRA serving (Pillar 16, `docs/CUSTOM_MODEL_INTEGRATION_SURVEY.md` §5.7-5.8). A
served model enters the app as *just another provider* under §2 — the `vllm` adapter
(`services/agent/providers/vllm_provider.py`) is an OpenAI-compatible client pinned to a vLLM
instance launched with `--enable-lora`; the LoRA is selected by the request `model` field (the
served adapter name). This is ADDITIVE: §1-§6 are unchanged, and the `adapter` field below is
an OPTIONAL concrete-provider attribute, NOT a change to the frozen §2 `Provider` interface.

### 7.1 The `adapter` field

A provider MAY expose `adapter: str | None` — the served LoRA-adapter identifier answering a
given call (`None` = base model). It is opaque routing state (the flywheel's DVC-versioned
adapter id / runner `job_id`, e.g. `"IP-13/v3"`), carried into telemetry (§7.5), never a
message-schema change. The neutral `complete_ex` signature is untouched.

### 7.2 Routing table (the seam)

`{insertion_point → RouteEntry}` decides which brain answers at each agent insertion point
(`IP-N`). Reference implementation: `services/serving/routing.py`. Persisted in the SAME
`config.json` as provider settings, under the top-level `"routing"` key (so a canary flip or
rollback is one single-file edit — no restart). Schema per entry:

```
RouteEntry = {
  "provider": str,              # e.g. "vllm" | "llamacpp" (merged-GGUF local serve)
  "model": str,                 # base model served at the endpoint
  "adapter_version": str|null,  # DVC-versioned LoRA id, or null = base model / fallback
  "fallback": Fallback,         # REQUIRED, kill-switch (§7.3)
  "canary": {"pct": 0..100, "opt_in": [user_id,...], "salt": str},
  "history": [str, ...]         # prior adapter_versions, for rollback
}
Fallback   = {"mode": "deterministic" | "frontier", "provider": str, "model": str}
```

`resolve(insertion_point, user_id, adapter_healthy) → RouteDecision` picks the concrete
`{provider, model, adapter}` for a call.

### 7.3 Kill-switch invariant (BINDING)

`fallback` is ALWAYS a deterministic-path OR frontier route — NEVER another experimental
adapter. A `Fallback` structurally has no `adapter_version` field, so "fall back onto another
LoRA" is unrepresentable by construction. The fallback doubles as (a) the incumbent for
canary-excluded traffic and (b) the degrade target when the adapter is unavailable
(`adapter_healthy=False`), which is how a live serving failure degrades WITHOUT a user-facing
error. New `vllm` provider is offline-safe only on a loopback endpoint (§5), like `llamacpp`.

### 7.4 Rollout ladder + promotion gate (Erik-ratified 2026-07-02, BINDING)

`shadow (T-H7) → canary (canary.pct % + per-user opt-in) → promote (100%, human-gated) → rollback`.

- **Promote** (`services/serving/promotion.promote`) requires BOTH, and there is NO
  auto-promote code path: (1) AUTOMATED EVIDENCE — the challenger beats-or-matches the
  incumbent on BOTH verifier-score AND accept-rate over ≥ N real shadow jobs (evidence is
  T-H7's `shadow_report` aggregation); AND (2) a HUMAN SIGN-OFF — a `SignOff{approver,
  report_ref}` referencing the signed-off evidence report. Passing metrics is NECESSARY, NOT
  SUFFICIENT: only a human supplying a valid sign-off promotes.
- **Rollback** (`promotion.rollback`) = flip `adapter_version` to a previous DVC-versioned
  adapter (or to base). Always safe, no confirmation, nothing overwritten — adapters live
  under DVC versioning, so a rollback is a pure routing flip (documented < 1 min).

### 7.5 Telemetry tag

Every response served by an adapter carries `model.adapter = adapter_version` in its
`ai_artifact` event (T-H1 schema, which already reserves the `"adapter"` slot in both the
Python `services/agent/providers/logging_provider.py` and C++ `core/agent/run.cpp` emit
paths). The single decided write-point is `RouteDecision.telemetry_tag()` — the selected
adapter is threaded from the routing decision, NOT introspected off the wrapped provider.
Canary responses (`RouteDecision.is_canary`) are thereby distinguishable in telemetry.

### 7.6 Human-gated seams (out of scope here)

Promotion sign-off (§7.4) and any Chudware Cloud relay interaction (§4.3) are human-gated. The
C++ read-side (`core/agent/run.cpp` provider selection + the `adapter` telemetry slot) follows
§8 (Python-first, C++ matches later); T-H9 lands the Python seam and the frozen schema above.

## 8. Conformance

- Python adapters conform today: `anthropic`, `openai`, `gemini`, `llamacpp` (inherits
  `openai`), `openrouter` (inherits `openai` + PKCE auth), `mock`/`mock-roles`. Conformance is
  tested in `agent/test_providers_conformance.py` (neutral schema round-trip + offline-safety,
  incl. `provider_offline_safe`).
- C++ mirrors the same neutral layer; it is developed against the Python engine as the
  differential oracle (CONTRIBUTING.md). New neutral-layer changes land in Python first, then
  the C++ port matches.

## §4.5 Subscription usage/identity (2026-07-14)
Per-account optional fields (absent = unknown, UI must degrade): `display_name`, `plan`,
`usage: [{window, label, used_fraction (0..1), resets_at (iso8601), status: ok|warn|blocked|unknown}]`.
Producers: `services/agent/providers/usage.py` fetchers — anthropic `GET api.anthropic.com/api/oauth/usage`
(+ profile), codex `GET chatgpt.com/backend-api/wham/usage` (ChatGPT-Account-Id header), cursor
`GET api2.cursor.sh/auth/usage`; xai: none (infer from 403 tier-gate; link dashboard). Poll <=5min,
never retry a 429'd usage fetch inside the window. Exposed via `subscription_status`/`subscription_accounts`
and new `subscription_usage {provider?} -> {ok, rows:[{provider, account_id, display_name, plan, usage}]}`.

## ai_roles thinking (additive)

`config.json` key `ai_roles` (WS2) role values may be:
- `"auto"` — model auto-pick; thinking defaults to `auto`
- `{ "provider", "model", "thinking?" }` — fixed model; optional thinking
- `{ "mode": "auto", "thinking?" }` — model auto with optional thinking

`thinking`: `auto` | `low` | `medium` | `high` (default `auto`).

Semantics (services/agent/role_models.py):
- Concrete level → that role's LLM calls use `reasoning_effort` at that level.
- `auto` on **default** (main chat) → prompt classifier (`classify_task` + effort map) assigns per prompt.
- `auto` on subagent roles (task/planning/advisor/plm/redaction/…) → dispatcher may pass optional `effort` on `delegate` / node; else complexity heuristics (rank/task_class/depth), default **medium**.
- Absent config → Auto → prior behavior plus these assignment paths.

RPCs `ai_roles_get` / `ai_roles_set` echo the full map including `thinking`.


## §4.4 / §4.5 — kimi-sub (Kimi For Coding subscription) — 2026-07-18

Provider id: `kimi-sub` (friendly id `kimi`; human label "Moonshot Kimi (Kimi For Coding)").
Module: `services/agent/providers/kimi_sub_provider.py` (`KimiSubProvider`,
`PROVIDER_ID = "kimi-sub"`, `DEFAULT_MODEL = "kimi-for-coding"`, `supports_vision = True`).

- **Auth:** OAuth 2.0 device authorization grant against `https://auth.kimi.com`
  (env override `KIMI_CODE_OAUTH_HOST` / `KIMI_OAUTH_HOST`). Client id
  `17e5f671-d194-4dfb-9706-5516cb48c098`. Device code: `POST /api/oauth/device_authorization`
  (form: `client_id`). Poll: `POST /api/oauth/token` with
  `grant_type=urn:ietf:params:oauth:grant-type:device_code` (handle
  `authorization_pending` / `slow_down` / `expired_token` / `access_denied`; default
  interval 5 s, TTL 15 min). Refresh: `POST /api/oauth/token` `grant_type=refresh_token`
  (when the response omits `refresh_token`, keep the old one). Expiry =
  `now_ms + expires_in*1000 − 5*60*1000` skew. Login surface:
  `login_device(open_browser=True, progress=None)` emits progress dicts with
  `user_code` + `verification_url` for the GUI device-code UX. Common headers via
  `get_kimi_common_headers()` (`User-Agent: KimiCLI/<ver>`, `X-Msh-Platform`,
  `X-Msh-Version`, `X-Msh-Device-Name`, `X-Msh-Device-Model`, `X-Msh-Os-Version`,
  `X-Msh-Device-Id` — device id persisted next to config.json as `kimi-device-id`).
- **Completions:** Anthropic-compatible `POST https://api.kimi.com/coding/v1/messages`
  (env override `KIMI_CODE_BASE_URL` for the `/coding/v1` base). Authorization Bearer
  access token + kimi common headers + `anthropic-version`; `max_tokens` REQUIRED;
  thinking uses kimi's adaptive variant.
- **Usage probe (§4.5):** `GET {base}/usages` (`GET /coding/v1/usages` on the default
  host) with Bearer + common headers. Fetcher: `services/agent/providers/usage.py`
  `fetch_kimi_usage` — payload shape `{usage, limits[]}` normalized into rows carrying
  `used` / `limit` / `remaining` / `resets` (plus the usual `used_fraction` /
  `resets_at` / `status` contract fields when present).
- **Ownership:** Python-owns-refresh (C++ read-only) — same as `anthropic-sub` /
  `codex-sub` / `cursor-sub` / `xai-sub`. Credentials under `config.json`
  `subscription_auth["kimi-sub"]`. Never offline-safe (§5).
