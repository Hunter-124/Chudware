# OMP provider/model switching vs. CAD routing

Scope: `scratch/omp-src/packages/ai` + `packages/catalog` (pinned `6ae7cdbf`) vs. CAD's
`services/agent/{config.py,routing.py,tiers.py,model_manager.py,model_catalog.py}`.

## A. OMP: model id → provider/api/baseUrl/credentials resolution

**Catalog data shapes** (`packages/catalog/src/`):
- `provider-models/descriptors.ts:62` `CATALOG_PROVIDERS` — the master provider table: one entry
  per provider (`id`, `defaultModel`, model list, auth kind, etc.). `:490` derives `KnownProvider`
  as a union of every `id`; `:498-521` derive `PROVIDER_DESCRIPTORS` (flattened per-model
  descriptors) and `DEFAULT_MODEL_PER_PROVIDER`. `:523-525` `getCatalogProviderEntry(id)` — the
  provider-level lookup a caller uses before touching model-level data.
- `models.ts:32-34` `getBundledModel<TApi>(provider, modelId)` — looks up `getModelRegistry()`
  (a `Map<Provider, Map<modelId, Model>>` built from bundled `models.json` at build time,
  see `build.ts`), returns the static `Model` object (context window, maxTokens, api family,
  pricing, thinking support, `transport` field). `:41-43` `getBundledModels(provider)` for the
  full per-provider list.
- `model-manager.ts` `createModelManager()`/`resolveProviderModels()` (`:75-210`) — the *dynamic*
  resolution layer used at runtime instead of the static bundle alone. Precedence documented at
  `:105-108`: **static → models.dev → cache → dynamic-endpoint**, later sources override earlier
  ones by model id (`mergeModelSources`/`mergeDynamicModels`, `:290+`). `ModelManagerOptions`
  (`:29-50`) carries `providerId`, optional `staticModels` override, `cacheDbPath`/`cacheTtlMs`
  (default 2h, `:8`), `dynamicModelsAuthoritative` flag (a successful live fetch *replaces* the
  static catalog, pruning stale ids — `:164-166` `retainModelIds`), and `fetchDynamicModels`/
  `modelsDev` hooks. `ModelRefreshStrategy` (`:14`) is `"online"|"offline"|"online-if-uncached"`;
  the online-if-uncached fast path (`:142-144`) short-circuits when a fresh authoritative cache's
  static-fingerprint matches, to avoid re-merging ~800ms of catalog data on every cold start.
  Cache is a local SQLite/db file (`model-cache.ts`, `getAgentDbPath`-rooted), not just memory —
  this is what lets `pi -p hi` skip network on every invocation once warm.
- Provider **registration** ties an id to request/stream behavior: `providers/register-builtins.ts`
  registers each `ProviderDefinition` (base URL, auth kind, request/response transformers) keyed by
  the same `Provider` id used in the catalog. `Model.baseUrl` on a resolved model can override the
  provider-level default per-model (used for e.g. self-hosted/gateway endpoints) — see
  `openai-codex-responses.ts:898,2063,2109,2253` reading `model.baseUrl || CODEX_BASE_URL` (per
  porting-docs `02-openai-codex.md:31`).

**Auth resolution** (`packages/ai/src/`):
- `auth-storage.ts` — `AuthStorage` is the credential store (SQLite-backed, `getAgentDbPath`).
  `AuthStorage.getApiKey(provider, {forceRefresh, sessionId, credentialId})` resolves a stored
  API key or OAuth access token for a provider, honoring per-session credential stickiness
  (`:1691-1692` in CHANGELOG: gateway keys resolution on `promptCacheKey`/`sessionId` so repeated
  turns in one conversation keep the same credential until it's unusable).
  `:5421-5423` `AuthStorage.resolver(provider, options)` builds an `ApiKeyResolver` implementing
  the **a/b/c auth-retry policy** directly: (a) initial resolve = session credential, (b) force-
  refresh same account (`:4176-4178`), (c) rotate to a sibling credential on failure
  (`:5320-5323` `rotateSessionCredential`). `markUsageLimitReached` (`:1232`/`:1203-1205`)
  additionally tracks per-account usage-limit exhaustion **scoped by model family**
  (gemini*/gemma* → Google, claude* → Anthropic, gpt*/openai/* → OpenAI) so one exhausted Gemini
  counter doesn't block a healthy Claude credential on the same OAuth pool.
- `auth-retry.ts:1-66` — `ApiKeyResolver`/driver: first resolution reuses a preflighted `seed`,
  subsequent resolutions defer to the resolver so callers don't need their own retry state
  machine. `:252-255` keeps this module structurally typed against `OAuthAccess` (no runtime
  import of `auth-storage`) to avoid a circular dependency between the credential store and the
  retry driver.
- `auth-gateway/server.ts` — the actual forward-proxy: accepts provider-native wire formats
  (OpenAI chat-completions, Anthropic Messages, etc. — see `route.module` dispatch around
  `:191-500`), parses into canonical `Context`, calls `streamSimple()`, translates back to the
  matching wire shape. `:272-275` documents the a/b/c policy is driven **server-side** here per
  request (so unauthenticated clients — macOS usage widget, containerized omp, llm-git — never see
  a bearer token). `:1970` (CHANGELOG) — `POST /v1/pi/stream` skips wire translation entirely,
  accepting `{modelId, context, options}` and returning canonical `AssistantMessageEvent` SSE;
  this is the transport CAD's bun bridge would most plausibly target (no per-wire-format parsing
  needed).
- `provider-details.ts` (exported via `index.ts:10`) plus `error/rate-limit.ts` — provider-specific
  usage-limit detection regexes (`USAGE_LIMIT_PATTERN`, CHANGELOG `:1203`) used both by
  `auth-storage.markUsageLimitReached` and `auth-gateway`'s error classifier
  (`error/gateway.ts` → `classifyGatewayError`, called at every `auth-gateway/server.ts` catch site,
  e.g. `:420-422`, `:481-482`, `:496-498`).

## B. Switching provider/model mid-session

- **Model swap is just a different `Model` object** passed into `streamSimple()`/the dispatch
  layer per turn — OMP has no persistent "provider session" object threading state through turns;
  the canonical `Context` (system + message history) is provider-agnostic and gets *re-encoded*
  for whichever provider/model is dispatched next.
- **Cross-provider history replay** is the load-bearing piece and lives in
  `providers/transform-messages.ts` (864 lines). Key mechanisms:
  - `deduplicateToolCallIds` (`:46-127`) rewrites tool-call ids that collide after normalization —
    necessary because different providers have incompatible id grammars/lengths
    (`:280-282`: OpenAI Responses ids can be 450+ chars with `|`; Anthropic requires
    `^[a-zA-Z0-9_-]+$` ≤ 64 chars, `MAX_TOOL_CALL_ID_LENGTH:21`). `appendDuplicateSuffix`
    (`:23-36`) special-cases Responses' composite `callId|itemId` ids, applying the suffix to
    *each* pipe-delimited segment so round-tripping through `normalizeResponsesToolCallId` doesn't
    silently collapse the rewritten id back onto the original.
  - Thinking/reasoning content is **demoted** when replayed against a model that didn't produce it
    (`:394-396`, `:503-513`, `:606-609`): every thinking signature is stripped on a provider hop
    because Anthropic's signatures are cryptographically end-turn-bound and can't be replayed
    against a different backend; the demoted content survives as plain text wrapped in the
    *target* model's own thinking-dialect fence (e.g. Gemini's ` ```thinking ` block,
    `dialect/demotion.ts`) so the model reads it as reasoning context, not literal transcript.
    A dedicated whitespace fix (`:606-609`) prevents demoted trailing whitespace from violating
    Anthropic's "final assistant content cannot end with trailing whitespace" rule when hopping
    back the other direction.
  - `transformProviderContext` (CHANGELOG `:392`) is model-aware: `(context, model) => Context`,
    so a transform can gate on the *dispatch* model's capabilities (vision input, provider, API
    family) — critical for a mid-session swap where the new model may lack a capability the old
    history assumes (e.g. image blocks, native tool-calling dialect).
- **Pitfalls documented in CHANGELOG** worth carrying into a port/fork decision:
  - `:1231` Ollama-specific reasoning toggle (`think: false`) must be sent explicitly when
    reasoning is disabled, or some backends keep "thinking" on by inertia.
  - `:1690-1693` gateway session/credential stickiness must track `sessionId`/`promptCacheKey`
    identity *before* credential lookup — get this wrong and a provider switch mid-session can
    silently reuse a credential/cache key belonging to the previous provider.
  - `:1938-1940` schema/grammar builders historically mis-handled `{}` (empty JSON Schema) across
    *every* provider identically — a reminder that provider-specific grammar-constrained decoding
    (llama.cpp, Bedrock, etc.) is a second axis of "switching," independent of chat wire format.
  - Stateful transports (OpenAI Codex's `previous_response_id` chaining, CHANGELOG `:1138`) drop
    out of the chain entirely on any history mutation/option change or provider swap and fall back
    to full-context replay — meaning full-replay-compatibility (the transform-messages work above)
    is a hard requirement, not just an optimization path.

## C. Reasoning effort handling

- `catalog/src/effort.ts` — the entire vocabulary: `Effort` const enum
  (`Minimal|Low|Medium|High|XHigh|Max`, `:2-9`) and `THINKING_EFFORTS` ordered array (`:11-18`).
  This is a **provider-agnostic scale**; each provider's registration (`registry/*.ts`) maps it
  onto whatever the vendor's own reasoning/thinking API expects (Anthropic budget tokens, OpenAI
  `reasoning.effort`, Gemini `thinkingBudget`, etc. — see per-provider files under
  `providers/openai-responses.ts`, `providers/google.ts`, `providers/anthropic-messages-server.ts`).
  `xai-oauth-effort-strip.test.ts` and `requires-effort.test.ts` (test names only, not read in
  depth) suggest some providers must have the effort param stripped entirely for certain
  auth modes (xAI OAuth) — a provider-specific quirk, not a generic rule.
- Because effort is a single small enum threaded through the canonical `Context`/dispatch options
  rather than embedded per-message, switching models mid-session trivially keeps whatever effort
  level was selected — the *provider adapter* is responsible for clamping/mapping it (e.g. a model
  that doesn't support `xhigh` degrades to its own max) at dispatch time, not the caller.

## D. Fallback/degrade ladders and rate-limit handling

- **Auth-classified failures** (401/invalid-token/expired) use the a/b/c ladder above:
  same-credential retry → force-refresh same account → rotate to sibling credential → surface
  "please log in again" (`porting-docs/00-MASTER-PLAN.md:109-114`, mirrored in
  `provider-layer/00-provider-layer.md:87-91`). This ladder is **not** applied to non-auth errors
  (rate limit, 5xx, content policy) — those propagate immediately to the provider-level retry
  layer instead (`utils/retry.ts`, `utils/retry-after.ts` honoring `Retry-After` headers).
- **Usage-limit (429/quota) handling** is distinct from the generic HTTP retry policy: it's
  classified via `error/rate-limit.ts` regexes, tracked per-account via
  `AuthStorage.markUsageLimitReached`, and **rotates to a sibling credential** rather than backing
  off the same one (CHANGELOG `:1702-1705`), with `retryAtMs` hints from `UsageLimitMarkResult`
  (`:1232`) letting the caller wait out a short sibling block (60s post-401, 5-min usage-probe)
  instead of blindly adopting the provider's own multi-hour retry-after.
- Provider-level transient-error retry (TLS blips, `tls: bad record MAC`, CHANGELOG `:698`) is
  handled *inside the provider loop* before ever surfacing to the session/error-banner layer —
  i.e. there's a three-tier structure: (1) transport-level retry inside one provider call,
  (2) auth a/b/c ladder across credentials for the *same* provider, (3) no built-in cross-provider
  fallback (OMP does not automatically fail over from Anthropic to OpenAI on exhaustion — fallback
  across providers is a caller/UX decision, not something `streamSimple` does itself).
- `ProviderHttpError` typed error (CHANGELOG `:1137`) with per-provider subclasses
  (`CodexApiError`, `AuthGatewayError`, `GoogleApiError`, `GeminiCliApiError`, `OllamaApiError`,
  `BedrockApiError`) carries `status`/`headers`/`code` uniformly so every retry/classification site
  (auth-retry, rate-limit, auth-gateway) shares one shape instead of ad-hoc `Error & {status?}`
  hacks — this uniformity is exactly what CAD's routing/config layer currently lacks (see below).

## CAD side: config.py / routing.py / tiers.py / model_manager.py / model_catalog.py

- `config.py:925` `_resolve_provider(name, cfg, role)` — the CAD analogue of OMP's
  provider-registration + catalog lookup **combined into one function**. Precedence
  (`:1-6` docstring): explicit arg → `CHUDWARE_PROVIDER` env → `config.json` → default.
  `:897-923` `_resolve_via_routing(role, cfg)` layers `routing.yaml` resolution *underneath* that
  precedence (only consulted when no explicit name/env var is set, `:928-931`), translating a
  routing `kind` (`anthropic|openai|openrouter|gemini|xai|llamacpp|vllm|mock|lmstudio|ollama|
  claude-cli|codex-cli`, `:873-886` `_ROUTING_KIND_TO_PROVIDER_NAME`) into the provider-name
  branches below (`:1044+`), and overlaying the routed `model`/`base_url` onto a shallow `cfg`
  copy (`:933-943`) so each provider branch's existing `cfg.get("model")`/`cfg[kind]["base_url"]`
  reads pick it up without per-branch routing-awareness. `:945` name resolution is
  case-lowered once; `:946-1002` offline-mode refusal happens **before constructing any client**
  (no key lookup even attempted) — stricter than OMP's runtime dispatch, but this is
  compliance-mode logic special to CAD, not general provider-switching plumbing.
- **Provider construction is one big `if/elif` ladder over `name`** (`:1044` onward through
  `:1249` `raise ValueError(f"Unknown provider: {name!r}")`), each branch importing its own
  `Provider` subclass and doing its own `cfg.get("api_key") or os.environ.get(...)` credential
  read inline. There is no shared credential-resolution abstraction analogous to `AuthStorage`:
  every provider branch re-derives its own key precedence by hand.
- `providers/base.py:29-47` `Provider` — the seam: `complete(system, messages, max_tokens) -> str`
  and `complete_ex(...) -> (text, thinking)`. This is **synchronous, one-shot**, no streaming
  event protocol, no `Context`-style canonical message representation — messages are plain
  `{"role", "content":[{"type":"text"|"image",...}]}` dicts (`:1-27`), and each provider adapter
  converts inline. There is no cross-provider history-replay transform layer at all: CAD has never
  needed one because there is no cross-provider mid-session hop today (provider is fixed for the
  process/session via `_resolve_provider`, not swapped turn-to-turn).
- `routing.py` `RoutingTable.resolve()` (`:330-359`) *is* CAD's fallback ladder, but it operates at
  the **tier/preference** level, not the auth/credential level: `_try_prefs` (`:361-370`) walks a
  role's preference list (`strongest|cheapest|strong|cheap_local|frontier|explicit:<src>/<model>`)
  against `available_sources()` (`:313-326`, an optimistic `{source_id: bool}` map the *caller*
  must inject — there is no automatic health/liveness probing built into RoutingTable itself), and
  falls through to a single-source "collapse" pick (`:346-354`) if no preference resolves. This is
  a **model/tier fallback**, entirely orthogonal to OMP's auth a/b/c ladder — CAD has no equivalent
  of "same-provider-different-credential" rotation; each configured `Source` in routing.yaml is
  implicitly one credential.
- **No first-class rate-limit/retry-after ladder at the routing/config layer.** 429 handling is
  scattered and duplicated per-provider-implementation instead of centralized:
  - `agent_turn.py:338-403` `classify_provider_error` — generic status-code sniffing (429/5xx →
    retryable) at the *agent turn* level, downstream of provider construction.
  - `providers/xai_sub_provider.py:217-1697` — a hand-rolled multi-account rotation ladder
    (`_is_rotatable_account_error`, `mark_account_blocked`, per-account 403/429 fallthrough) that
    is essentially a bespoke, provider-specific reimplementation of what OMP's
    `AuthStorage.markUsageLimitReached` + auth-retry does generically for *all* providers.
  - `providers/kimi_sub_provider.py:319-323`, `usage.py:571-702` (`_RateLimited`), each provider
    module has its own ad hoc `Retry-After`/429-freeze handling with no shared type or policy.
  - `scripts/diag_providers.py:167-216` — a diagnostic-only single-retry-with-fixed-backoff, not
    production policy.
  - No equivalent of OMP's `ProviderHttpError` common shape: CAD errors carry status via duck-typed
    `.status_code`/`.code` attributes set ad hoc per call site (e.g. `xai_sub_provider.py:1530,1595`).
- `model_manager.py` — **not a runtime model-resolution manager at all**; it's a GGUF
  download/provisioning module for the two bundled local llama.cpp models (`_REGISTRY` at
  `:95-138`, `provision()`/`get_model_path()`). It has zero relationship to OMP's
  `model-manager.ts` (dynamic catalog merge/cache); the naming collision is coincidental. There is
  no CAD analogue of OMP's static→models.dev→cache→dynamic merge precedence or TTL'd catalog cache.
- `model_catalog.py:1-43` — closer to OMP's *catalog* half: `discover_models`/`merge_catalog`/
  `resolve_capabilities` with documented precedence (`:7-8`) `user override > live endpoint field >
  models.dev > id heuristic > provider default`, backed by a 7-day `models.dev` snapshot cache
  (`ensure_models_dev_cache`, `:102-140`, default `max_age_s=7*24*3600` vs. OMP's 2h TTL) and an
  in-process memo keyed on path+mtime (`:143-186`, same pattern as `config.py`'s `_cfg_cache`).
  `discover_openai_compatible`/`discover_gemini` (`:218-331`) are live `/models` probes, roughly
  analogous to OMP's `fetchDynamicModels` hook, but there's no persistent on-disk model cache with
  `dynamicModelsAuthoritative` pruning semantics — CAD re-derives from the 7-day models.dev
  snapshot plus a live probe every call, with no SQLite-backed short-TTL merge cache layer.
- `tiers.py` maps context-length → tier profile (budgets, structured-output mode, deliberation
  level) — this has no OMP counterpart; it's a CAD-specific prompt/turn-assembly concern, not
  provider/model routing. `LOCAL_TIER_MODELS` (`:261-264`) pins specific bundled GGUF ids per tier.

## Recommendation table

| Feature | OMP location | CAD counterpart | Rec | Effort | Hard deps/risks |
|---|---|---|---|---|---|
| Catalog data (context/maxTokens/pricing/api family per model) | `catalog/src/models.ts:32`, `provider-models/descriptors.ts:62` `CATALOG_PROVIDERS` | `model_catalog.py` (`ModelRow`, models.dev merge) | SIDECAR | M | Bundled `models.json` build step (`catalog/scripts/generate-models.ts`) is bun/TS-only; re-vendor script must run under bun periodically |
| Dynamic catalog resolution + short-TTL SQLite cache (`resolveProviderModels`) | `catalog/src/model-manager.ts:75-210` | `model_catalog.py` discover_* + 7-day cache, no SQLite merge | PORT (algorithm only) | M | Precedence/merge logic is straightforward to reimplement in Python; SQLite cache schema is bun-native (`model-cache.ts`) — don't vendor, reproduce the *policy* |
| Credential store + a/b/c auth-retry policy (`AuthStorage`, `auth-retry.ts`) | `ai/src/auth-storage.ts`, `auth-retry.ts` | Ad hoc per-provider key reads in `config.py`'s `_resolve_provider` branches; bespoke rotation in `xai_sub_provider.py` | SIDECAR (real reuse pays off here — this is the single biggest pain-relief item) | L | `AuthStorage` is deeply coupled to `@oh-my-pi/pi-utils` (`getAgentDbPath`) and OMP's own SQLite schema/session model; a sidecar bridge must own credential storage end-to-end or duplicate translation at every call |
| Usage-limit/429 sibling rotation (`markUsageLimitReached`, `error/rate-limit.ts`) | `auth-storage.ts:1232/1203-1205` | Per-provider hand-rolled (`xai_sub_provider.py`, `kimi_sub_provider.py`, `usage.py:_RateLimited`) | SIDECAR (same reasoning as above) or PORT the *pattern* (model-family-scoped blocking) if going pure-Python | M–L | Requires a multi-account credential pool concept CAD doesn't currently model per role |
| Typed provider HTTP error shape (`ProviderHttpError` + subclasses) | `ai/src/error/*` | Duck-typed `.status_code`/`.code` set ad hoc | PORT | S | Pure design pattern, zero OMP runtime dependency — cheapest, highest-leverage fix regardless of sidecar/fork decision |
| Cross-provider history replay (tool-call id rewrite, thinking demotion) | `ai/src/providers/transform-messages.ts` (864 lines) | None — CAD never swaps provider mid-session today | KEEP (no-op) unless CAD adopts mid-session model swapping; then SIDECAR | L if ever needed | This is the most intricate, most provider-quirk-laden code in OMP; a Python port would need to independently track every vendor's tool-call-id grammar and thinking-signature rules — high ongoing maintenance cost. Only worth it if CAD actually wants live provider hot-swap in one conversation |
| Reasoning-effort enum + per-provider mapping | `catalog/src/effort.ts` + per-registry mapping | `cfg.get("reasoning_effort")` passed straight to `AnthropicProvider` (`config.py:1051`), no shared enum/clamping | PORT | S | Trivial: define the same 6-level enum in Python, add a per-provider clamp/map function; no OMP runtime needed |
| Routing/tier preference fallback (`RoutingTable.resolve`) | n/a (OMP has no tier-preference routing layer; it's single-model-per-request) | `routing.py:330-436` (already good, KEEP) | KEEP | — | CAD's tier-preference routing is arguably *more* structured than anything in OMP for this axis; no gap here |
| auth-gateway forward-proxy / `POST /v1/pi/stream` | `ai/src/auth-gateway/server.ts` | None | SIDECAR (only if CAD wants a shared multi-client credential-holding proxy, e.g. for a future web/mobile companion) | L | Bun-only HTTP server; only worth standing up if there's a second client consuming the same credentials — not needed for a single desktop app |

## Summary of biggest findings

OMP's provider layer is built around three independent axes CAD currently conflates or skips:
(1) a **catalog** resolving model→capabilities via static bundle → models.dev → cached dynamic
probe (`model-manager.ts:105-210`, 2h TTL, SQLite-cached) — CAD's `model_catalog.py` does the
capability-merge half but has no persistent short-TTL cache, re-deriving on every call; (2) a
**credential store with a generic a/b/c retry ladder** (`auth-storage.ts`/`auth-retry.ts`:
retry-same → force-refresh-same-account → rotate-sibling-credential, plus model-family-scoped
usage-limit tracking) that CAD reimplements piecemeal and inconsistently per provider
(`xai_sub_provider.py`, `kimi_sub_provider.py`, `usage.py` each have their own 429/rotation logic,
no shared type); and (3) a **cross-provider history-replay transform** (`transform-messages.ts`,
864 lines: tool-call-id rewriting for incompatible id grammars, thinking-signature stripping on
provider hops) that CAD doesn't need today because it never swaps providers mid-session — `Provider`
is resolved once per process via `_resolve_provider` (`config.py:925`).

The user's "pain switching providers" almost certainly maps to axis (2): CAD has no single place
credential resolution/rotation/rate-limit backoff lives — it's copy-pasted per provider file with
inconsistent shapes (`.status_code` vs `.code`, ad hoc `_RateLimited` exceptions, bespoke
account-rotation only for xAI). Highest-leverage, lowest-risk fix is a straight **PORT** of OMP's
*design* (typed `ProviderHttpError`, a shared credential-resolver interface implementing the a/b/c
policy, model-family-scoped usage-limit tracking) into Python — this needs no bun sidecar at all.
A full **SIDECAR** of `AuthStorage`+`auth-retry` only pays off if CAD wants OMP's actual multi-
account OAuth pool management (many linked accounts per provider) rather than CAD's current
one-credential-per-source model; that's a bigger scope decision the user should make explicitly.
The cross-provider transform-messages layer is the single most complex piece in OMP's `ai` package
and should stay **KEEP/no-op** unless the product goal explicitly becomes "hot-swap provider
mid-conversation" — porting or sidecaring it without that requirement is pure sunk cost.
</content>
