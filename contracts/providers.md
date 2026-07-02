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

Resolution precedence (both ports): explicit arg → `AI_CAD_PROVIDER` env → `config.json`
`provider` → default (local loopback openai-compatible). Config file:
`%APPDATA%/AI-CAD/config.json` (Windows) / `$XDG_DATA_HOME/AI-CAD/config.json` (POSIX).

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
  or env `AI_CAD_ENABLE_OPENROUTER_PKCE=1`. Disabled → helper returns a contract-shaped
  `{ok: false, reason, hint}`, never a traceback.
- The network exchange is transport-injectable so CI exercises the full flow with a fake
  transport and NO network/secrets.

### 4.2 Prohibited auth (BINDING — CI-enforced)

NEVER, in any port: reuse a **consumer** Claude Pro/Max or Gemini subscription OAuth token in
AI-CAD. Anthropic blocks it server-side and has issued takedowns; Google mass-bans (incl.
paying subscribers). A CI assertion greps the tree and fails if code references consumer
OAuth token-reuse endpoints for Claude/Gemini (`test_prohibited_auth.py`). Sanctioned
subscription-linked routes (Anthropic Agent-SDK metered credit, GitHub Copilot SDK, an
OpenAI Partner-Network login) are separate, human-gated, and out of scope here.

### 4.3 AI-CAD Cloud relay (config seam ONLY — human-gated)

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
- Sources, most specific first: explicit call arg → env `AI_CAD_TOOLS_ALLOW`
  (comma-separated tool names and/or `family:<name>`) → config `tools.allow`. `AI_CAD_TOOLS_DENY_RISK`
  (comma-separated risk classes) additionally denies whole risk classes.
- Enforcement point: the dispatch wrapper around `mcp.call_tool(name, args)`. A disallowed
  call returns a **contract-shaped** `{"ok": false, "reason": ..., "hint": ...}` — never a
  traceback, never a silent success.
- The DAG node contract (T-A1 `dag_schema`) passes explicit per-node `tools:` allow-lists;
  this registry is what those lists bind against. Per-context lists arrive later; the seam is
  frozen now.

## 7. RESERVED: `routing` (T-H9 — do NOT spec here)

Reserved for T-H9 (per-task LoRA routing). T-H9 will add an `adapter` field and a
per-insertion-point routing table (which model/LoRA answers at which agent insertion point).
This section is a placeholder so T-H9 appends without renegotiating v1; leaving it empty is
intentional. No other task defines `routing`.

## 8. Conformance

- Python adapters conform today: `anthropic`, `openai`, `gemini`, `llamacpp` (inherits
  `openai`), `openrouter` (inherits `openai` + PKCE auth), `mock`/`mock-roles`. Conformance is
  tested in `agent/test_providers_conformance.py` (neutral schema round-trip + offline-safety,
  incl. `provider_offline_safe`).
- C++ mirrors the same neutral layer; it is developed against the Python engine as the
  differential oracle (CONTRIBUTING.md). New neutral-layer changes land in Python first, then
  the C++ port matches.
