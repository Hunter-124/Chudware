# CAD (Billet) — LLM Provider Sign-In, Model Management & Cost UI

Design spec (read-only). Reuses OMP's provider/auth stack (`scratch/omp-src`, pinned `6ae7cdb`)
via the proposed bun bridge sidecar (`docs/omp-integration/00-OPTIONS-AND-DECISIONS.md:189-197`
— bun bridge confirmed end-to-end importing `@oh-my-pi/pi-catalog` + `stream`). No bun bridge
exists today; CAD's live auth surface is the Python `subscription_*` RPC family
(`services/agent/worker_rpc.py:272-280`) with tokens in `config.json`. This spec defines the
target UX and the minimal bridge contract to drive it; Phase 0 below keeps the existing Python
paths working while the bridge lands.

Priorities: **Claude stack (Anthropic API key + Claude Pro/Max OAuth) and Kimi K3
(kimi-code OAuth device flow + Moonshot API key) first.**

---

## 1. Where it lives

The Settings modal already has the right home — do **not** invent a new window.

- **Settings → Providers tab (tab 3)** is the canonical surface
  (`host/src/ui/settings_modal.cpp:4442-4481`). Today: `Provider & Model` glass section
  (`ui::drawAiSettingsSection(st, busy, AiSettingsSection::ProviderModel)`) + `Accounts`
  section (`drawSubscriptionAccounts`, settings_modal.cpp:622). The new design **replaces the
  contents of this tab** with a unified provider manager (list + detail), keeping the same
  chrome: `settingsTabBodyBegin` (settings_modal.cpp:175), `settingsGlassSectionBegin/End`
  (:158), `settingsSectionHeader` (:146), all spacing in `sc`/`ui::uiScale`/`ui::metrics::U`.
- **Command search entries** (settings_modal.cpp:3368-3384) get new rows for the new sections
  ("Sign in", "Add API key", "Usage & cost", "Model routing") so palette search lands here.
- **Copilot composer**: the existing model chip / `drawModelDropdown`
  (copilot/settings.cpp:1271) stays the quick-switcher; it gains cost/capability sublabels
  (§3.4) and an "unauthenticated" affordance that jumps to Settings→Providers (set
  `st.openSettings = true; st.openSettingsTab = 3;` — same pattern as
  `drawAISettingsBody` buttons, copilot/settings.cpp:1012-1021).
- **In-column quick-settings overlay** (`drawAISettingsBody`, copilot/settings.cpp:922) stays
  session-knobs only; its "Providers" button already jumps to tab 3. No sign-in UI here.
- **AI workspace v2** (`host/src/ui/ai/ai_workspace.cpp:18-20`) explicitly wiped inference
  settings — do not add sign-in there.

Scale conventions to match: label+widget pairs inside glass children (labels never orphaned by
scroll — settings.cpp:639-641 comment), right-block reservation math for trailing actions
(settings_modal.cpp:692-703), plan chips drawn as stroked rects (composer.cpp:1802-1813),
status colors via `ui::palette.statusOk/statusWarn/statusErr/textDim` (settings.cpp:362-374).

## 2. Data model (shared, UI-agnostic)

One JSON-shaped model produced by the bridge; both ImGui and web render it. Mirrors OMP types:

```jsonc
ProviderRow {                      // one per provider id (OMP "anthropic", "kimi-code", ...)
  id, label,                       // id = OMP registry id; label = display name
  authKinds: ["api_key","oauth"],  // from OMP ProviderDefinition (docs/adding-a-provider.md:73-84)
  keyless: false,                  // ollama/llama.cpp/lm-studio special case (docs/providers.md:25)
  state: "signed_out"|"key_set"|"oauth_connected"|"error"|"disabled",
  origin: "runtime"|"config"|"oauth"|"api_key"|"env"|"fallback"|null,
                                   // OMP CredentialOriginKind, auth-storage.ts:101
  envVar: null,                    // when origin == "env" (auth-storage.ts:110-111)
  accounts: [ { id, email, displayName, plan, orgName, active, disabledCause } ],
                                   // OMP OAuthAccountSummary auth-storage.ts:782-792
  usage: {                         // OMP UsageReport usage.ts:98-113, per active account
    plan, windows: [ { id, label, windowLabel, usedFraction, status, resetsAt } ],
                                   // UsageLimit + resolveUsedFraction usage.ts:54-64,120-129
    notes: []                      // provider-wide disclaimers usage.ts:104-110
  } | null,
  error: null,                     // last validate/login error text
  consoleUrl,                      // vendor key console deep link (per-provider static table)
  modelsAvailable: int
}
ModelRow {                         // OMP Model, catalog/src/types.ts:694-808
  id, provider, name,
  reasoning: bool,                 // :716
  input: ["text","image"],         // :717 — vision = input contains "image"
  supportsTools: bool,             // :729
  contextWindow: int|null,         // :742
  maxTokens: int|null,             // :743
  cost: { input, output, cacheRead, cacheWrite },   // $/M tokens, :734-739
  hidden: bool                     // Settings::hiddenModels (settings.cpp:1063-1064)
}
CostSummary {                      // per-session + per-part rollup
  session: { usd, tokensIn, tokensOut, byModel: [ { model, usd, tokensIn, tokensOut } ] },
  part:    { docId, usd, tokensIn, tokensOut },   // keyed by open document
  today:   { usd }                              // from bridge cost history
}
```

Secrets never cross into the UI: the bridge returns booleans/labels (`key_set`,
`email`, `plan`), never key bytes. OMP already enforces this split — `StoredAuthCredential`
(auth-storage.ts:135-140) holds the secret row; `OAuthAccountSummary`/`OAuthLoginIdentity`
(auth-storage.ts:739-745, 782-792) are the display-safe slices. Credential persistence moves
from `config.json` to the bridge's SQLite auth store (`SqliteAuthCredentialStore`,
auth-storage.ts:6007+; OMP keeps it at `~/.omp/agent/agent.db` — CAD uses its own data dir)
with an OS-keychain seal as a follow-up (§6).

## 3. UX design

### 3.1 Settings → Providers tab (new layout)

Two-pane inside the existing tab body: left = provider list, right = selected provider detail.
Left pane width `220*sc`, right pane fills. (Single-column fallback under `640*sc` width:
list collapses to a combo.)

```
┌ Settings ─ [Appearance][General][AI][Providers*][Experimental] ───────────┐
│ API providers, sign-in, and model routing. Credentials stored securely     │
│ by the auth bridge (never in config.json).                                 │
│ ┌────────────────────┬───────────────────────────────────────────────────┐ │
│ │ Claude (Anthropic) │  Claude (Anthropic)                    [Disable]  │ │
│ │ ● OAuth — Max plan │  ┌─ Sign-in ────────────────────────────────────┐│ │
│ │   5h ▓▓▓░░ 62%     │  │ ● OAuth connected — hunter@… (Max plan)      ││ │
│ │   7d ▓░░░░ 18%     │  │   org: Personal · active account             ││ │
│ │                    │  │   [Switch account ▾] [Add account] [Sign out]││ │
│ │ Kimi Code          │  │   ── or ──                                   ││ │
│ │ ○ signed out       │  │   API key  [••••••••••••sk-ant-…] [Validate] ││ │
│ │                    │  │   Get a key → console.anthropic.com/…        ││ │
│ │ OpenAI             │  └───────────────────────────────────────────────┘│ │
│ │ ● key set          │  ┌─ Usage ───────────────────────────────────────┐│ │
│ │                    │  │ 5-hour window  ▓▓▓▓▓▓▓░░░ 62% · resets 14:00  ││ │
│ │ Google Gemini      │  │ 7-day window   ▓▓░░░░░░░░ 18% · resets Mon   ││ │
│ │ ○ signed out       │  │ Observed spend (OMP-measured only): $4.12    ││ │
│ │                    │  └───────────────────────────────────────────────┘│ │
│ │ Moonshot (Kimi)    │  ┌─ Models (12) ────────────────────────────────┐│ │
│ │ ● key set          │  │ ☑ claude-opus-4-8    ctx 200k tools vision…  ││ │
│ │                    │  │   $15/$75 per Mtok (cache $1.50/$18.75)      ││ │
│ │ …                  │  │ ☑ claude-sonnet-4-6  ctx 200k tools vision   ││ │
│ │                    │  │ ☐ claude-haiku-4-5   ctx 200k tools          ││ │
│ │                    │  └───────────────────────────────────────────────┘│ │
│ ├────────────────────┴───────────────────────────────────────────────────┤ │
│ │ Session $1.87 · 142k tok ▾   Part bushing_flanged $0.43 · Today $6.20 │ │
│ └────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```

Left-pane row: provider label + one status line, color from state —
`statusOk` connected/key-set, `textDim` signed-out, `statusErr` error, `statusWarn` usage ≥85%
(mirror OMP's `PRIMARY_WINDOW_HOT_FRACTION = 0.85`, auth-storage.ts:68 — a nearly-exhausted 5h
window is exactly what the user needs to see before a mid-session block). When OAuth-connected,
show the two primary usage windows as thin `ImGui::ProgressBar` strips under the label.

Detail pane sections (all `settingsGlassSectionBegin`): **Sign-in**, **Usage**, **Models**,
footer strip = live cost summary (§3.5). Disable toggle maps to OMP `disabledProviders`
(docs/providers.md:181-204: hides models, keeps credentials — re-enable restores).

State → actions matrix:

| state | primary action | secondary |
|---|---|---|
| `signed_out` | `Sign in…` (OAuth) / `Add API key…` | console deep-link |
| `key_set` | `Re-validate`, `Remove key` | `Sign in…` (add OAuth) |
| `oauth_connected` | `Add account`, `Sign out` | `Re-authenticate` on 401 |
| `error` | `Retry` + error text | `Sign out`, copy diagnostics |
| `disabled` | `Enable` | — |

### 3.2 Add-API-key flow

Per-provider, inline in the Sign-in section (no modal):

```
API key   [••••••••••••••••••••]   [Validate & save]
          Get a key → console.anthropic.com/settings/keys   (opens browser)
          ◌ validating…   →   ✓ key accepted — 12 models available
                              ✗ invalid key (401) — check console.anthropic.com
```

- Password-flag `InputText` (existing convention, settings.cpp:573), 256-char buffer.
- Deep link: per-provider static `consoleUrl` in the bridge's provider table (OMP keeps these
  strings in each `registry/<vendor>.ts` def; surface them rather than hardcoding in C++).
- Validate = cheap request: OMP's `validateApiKeyAgainstModelsEndpoint`
  (registry/api-key-validation.ts:132-161, 15 s timeout at :30) for OpenAI-compatible
  (`GET /models`) and `validateAnthropicCompatibleApiKey` (:88-124) for Anthropic. Save only on
  success → `AuthStorage.setApiKey` → SQLite row (`ApiKeyCredential{type:"api_key",key,source:"login"}`,
  auth-storage.ts:81-85).
- Paste key in composer when provider unauthenticated → toast + jump to Settings (no inline
  secrets in the chat column).

### 3.3 OAuth flow (browser + loopback, with device + manual fallbacks)

OMP's substrate, reused wholesale:

- **PKCE**: `generatePKCE()` (registry/oauth/pkce.ts:5-18).
- **Loopback capture**: `OAuthCallbackFlow` (registry/oauth/callback-server.ts:58-401) —
  binds preferred port with random-port fallback (:31-53), serves `/callback` (:19) plus a
  `/launch` 302-redirector (:27) so the UI copies a *short* URL that can't be truncated
  (`OAuthAuthInfo.launchUrl`, oauth/types.ts:40-48 — UIs SHOULD prefer it as copy target).
  `login()` at callback-server.ts:139 runs the flow; `parseCallbackInput` (:406-431) powers
  the manual-paste fallback for headless/remote.
- **Anthropic**: `AnthropicOAuthFlow` (registry/oauth/anthropic.ts:204-286), port **54545**
  (:19), claude.ai authorize URL + `user:inference` scopes (:14-25), identity (email/org/plan)
  resolved at login (:122-202) → feeds the account row label. Refresh:
  `refreshAnthropicToken` (:299-344); auto-refresh-with-skew handled by AuthStorage
  (`OAUTH_REFRESH_SKEW_MS 60s`, auth-storage.ts:643).
- **Kimi K3**: device-authorization grant, `loginKimi` (registry/oauth/kimi.ts:250-258) —
  `user_code` + `verification_uri` + 5 s poll (:18-19). This matches CAD's existing xAI
  device-code UX one-for-one (settings.cpp:393-478: code box + Copy code + Open in browser +
  fallback URL), so the Kimi row reuses that layout verbatim.
- **Controller contract**: the bridge maps `OAuthController` (oauth/types.ts:64-71) onto RPC
  events: `onAuth{url,launchUrl,instructions}` → "open browser" UI state; `onProgress` → status
  line; `onManualCodeInput` → paste field; `signal` → Cancel button.

ImGui flow (loopback providers — Claude):

```
[Sign in with Claude…]  →  phase: Starting
  ◌ Starting sign-in…
→ phase: AwaitingCallback
  Browser opened — approve access, then come back.        [Cancel]
  Didn't open? http://localhost:54545/launch  [Copy link] [Open]
  Trouble completing? Paste the redirect URL: [________________] [Submit]
→ phase: Exchanging (◌ Exchanging code…)
→ phase: Connected
  ✓ Connected — hunter@example.com (Claude Max · Personal org)
  [Add another account] [Done]
```

Phases mirror `OauthCapture::Phase` (oauth_capture.h:58-65: Idle/StartingLogin/AwaitingCode/
Browsing/Success/Error) — keep that state machine shape, but **prefer the external browser +
loopback** as the default path (it's what OMP hardens) and keep the embedded-CEF modal
(oauth_capture.cpp) as an opt-in for web/WASM contexts where loopback can't bind. Multi-account:
"Add another account" is first-class (OMP rotates siblings on limit exhaustion,
auth-storage.ts ranking at :63-68; account list via `listOAuthAccounts` :782-792); before a
2nd login, wipe/isolate the browser session as `OauthCapture` already does
(oauth_capture.h:93-98).

Re-auth: on a definitive OAuth failure (`isDefinitiveOAuthFailure`, re-exported
auth-storage.ts:657) the row flips to `error` with "Session expired — sign in again" and the
composer model chip gets a red dot; one click re-runs 3.3.

### 3.4 Model picker (capabilities + cost)

Two surfaces, one data source (`ModelRow` list from bridge `listModels`):

1. **Composer dropdown** (`drawModelDropdown`, settings.cpp:1271; rows via `modelRow` :1225):
   add a right-aligned dim sublabel per row, formatted OMP-style:
   `ctx 200k · $3/$15 · tools vision` — cost = `$in/$out` per-Mtok pair, `free` when both zero
   (OMP `formatCostPair`, model-browser.ts:271-281); context as `200k`
   (`formatContext` :284-293). Cache-read/write cost goes in the row tooltip, not the line.
   Unauthenticated providers keep the existing dim + hint behavior (settings.cpp:1270 comment).
2. **Providers-tab Models section** (per provider): checkbox list = `hiddenModels` toggle
   (existing, settings.cpp:1063), each row showing name + capability chips
   (`reasoning`/`vision`/`tools` from catalog/src/types.ts:716-729) + context + cost pair.
   This replaces today's free-text/combo model id field for bridge-managed providers; the
   legacy combo stays for local/custom endpoints.

**Default + role routing.** CAD already has role pickers — `drawModelRolesSection` (AI→Roles
tab, settings_modal.cpp:3897-3900) and the Eng.Firm pickers. Changes: (a) role pickers consume
the bridge `ModelRow` list (same rows, grouped by provider, showing cost sublabels) instead of
hand-maintained id strings; (b) add an explicit **Default model** row at the top of the Roles
section = the provider/model chosen in Providers tab (shown read-only there, click to jump);
(c) per-role thinking-level select stays as-is. OMP precedent for role→model resolution and
display chips: `resolveRoleAssignments`/`formatRoleChip` (model-browser.ts:66-119, 260-269).

### 3.5 Cost display

- **Per turn**: existing footer in chat_log.cpp:378-388 (`tokens in/out · s`) gains `· $0.012`
  computed from the turn's model cost pair — only when the model has non-zero cost.
- **Per session / per part**: footer strip at the bottom of Settings→Providers and a compact
  line in the copilot column header: `Session $1.87 · Part bushing $0.43 · Today $6.20`
  (click = popover with per-model breakdown table). Per-part attribution keys on the active
  document id at request time (the bridge tags each request with `docId`; rollup is a bridge
  query, not UI bookkeeping). OMP records per-request cost to cost-history rows
  (`UsageCostHistoryEntry{costUsd, accountKey}`, usage.ts:163-172) — same shape, plus `docId`.
- **Subscription windows** (usage limits): reuse the existing composer's
  `drawCopilotUsageBox` (composer.cpp:1774-1858 — plan chip + per-window bars polling
  `subscription_usage`); the bridge version returns OMP `UsageReport` windows
  (Claude 5h/7d buckets, usage/claude.ts:57-75; Kimi usage provider usage/kimi.ts). Show the
  "OMP-observed spend only" disclaimer pattern from `UsageReport.notes` (usage.ts:104-110) —
  subscription plans don't bill per token, so label observed USD as *equivalent API cost* to
  avoid implying a bill.

## 4. Bridge / host API (minimal RPC surface)

One JSON-RPC method family `auth_*`/`models_*` served by the bun sidecar (spawned like CAD's
other workers, per 00-OPTIONS-AND-DECISIONS.md:47-50). C++ calls it through the same
`st.rpc.call` mechanism already used for `subscription_*` (settings.cpp:407). Web (WASM) reaches
it through hostd like every other method (contracts/web_parity.md:3-7) — no second transport.

| method | params | returns | OMP backing |
|---|---|---|---|
| `auth_list_providers` | `{}` | `{providers: ProviderRow[]}` (without usage) | registry defs + `SqliteAuthCredentialStore.listProviders` auth-storage.ts:7087; `CredentialOrigin` auth-storage.ts:108-112 |
| `auth_state` | `{provider}` | `{state, origin, accounts[], error}` | `listOAuthAccounts` auth-storage.ts:782; `StoredAuthCredential.disabledCause` :139 |
| `auth_validate_key` | `{provider, key}` | `{ok, error?, modelsAvailable?}` | api-key-validation.ts:88-161 |
| `auth_set_key` / `auth_remove_key` | `{provider, key}` / `{provider}` | `{ok}` | `ApiKeyCredential` auth-storage.ts:81-85 |
| `auth_begin_oauth` | `{provider}` | `{flowId}` (async) | `AuthStorage.login` auth-storage.ts:2589; per-vendor flow |
| `auth_oauth_event` (push/poll) | `{flowId}` | `{phase, url?, launchUrl?, progress?, needsManualCode?, done?, identity?, error?}` | `OAuthController` oauth/types.ts:64-71; `OAuthLoginIdentity` auth-storage.ts:739-745 |
| `auth_submit_manual_code` | `{flowId, input}` | `{ok}` | `parseCallbackInput` callback-server.ts:406-431 |
| `auth_cancel_oauth` | `{flowId}` | `{ok}` | controller `signal` oauth/types.ts:69 |
| `auth_logout` | `{provider, accountId?}` | `{ok}` | store delete; logout-account-selector.ts precedent |
| `auth_switch_account` | `{provider, accountId}` | `{ok}` | `getOAuthAccessAt`/selection auth-storage.ts:778-792 |
| `auth_usage` | `{provider?, force?}` | `{reports: UsageReport[]}` | usage/claude.ts, usage/kimi.ts; 5-min TTL auth-storage.ts:601 |
| `models_list` | `{provider?}` | `{models: ModelRow[]}` | catalog `Model` types.ts:694-808 |
| `cost_summary` | `{docId?, sinceMs?}` | `CostSummary` | usage.ts:163-180 (+ `docId` tag per request) |

Non-goals (YAGNI): no TUI, no `/login` slash commands, no broker mode (OMP's
auth-broker-gateway is for remote/headless fleets; CAD is single-user desktop — the
`REMOTE_REFRESH_SENTINEL` machinery auth-storage.ts:288-297 stays unused), no custom
`models.yml` provider editor in v1 (OMP supports it, docs/providers.md:284-331; CAD's
local/custom endpoints stay on the legacy path).

## 5. Local (ImGui) vs web-UI parity

The web app is **the same `host/src/ui` tree compiled to WASM** (contracts/web_parity.md:3) —
parity is structural, not a second implementation:

1. **One renderer.** All §3 panels are ordinary ImGui; WASM builds them from the same
   `settings_modal.cpp`/`copilot/settings.cpp` sources. Zero divergence by construction.
2. **One data source.** Both targets call the same `auth_*`/`models_*` RPCs through hostd.
   The bridge must run **daemon-side** (spawned/managed by hostd, like worker_bridge.cpp
   fallbacks), never in-process, so WASM's IDBFS sandbox never touches credentials — the same
   reasoning as the existing WASM host-providers fetch shim (settings.cpp:1090-1157).
3. **OAuth on web**: loopback can't bind in a browser tab → web uses (a) device-code flows
   (Kimi: works as-is), and (b) the embedded-CEF/redirect-modal pattern only on desktop; on
   web, `auth_begin_oauth` returns `launchUrl` and the UI opens it in a new browser tab and
   falls back to manual-code paste (`parseCallbackInput`) — OMP's own TUI does exactly this
   fallback for headless (login-dialog.ts `showManualInput`). Per web_parity rule 1, sign-in
   methods stay desktop-capable-first; any web-unsupported method is registered in
   `contracts/web_parity.md` with the entry gated per T-RF12 (`web_capabilities.cpp` —
   settings_modal.cpp:32 already gates sign-in on web).
4. **State sync**: UI holds no auth truth — it renders the last `auth_list_providers`/
   `auth_state` response and re-polls on section open + 5-min TTL (mirrors the existing
   `drawSubscriptionAccounts` poll discipline, settings_modal.cpp:626-632). Both targets
   therefore always agree; there is no local cache to drift.

## 6. Incremental build order

1. **P0 — Bridge skeleton + read-only state.** bun sidecar with `auth_list_providers`,
   `auth_state`, `models_list` for `anthropic` + `kimi-code` (+ `moonshot` API-key). Import
   CAD's existing credentials once (config.json tokens → SQLite store). Providers tab gains the
   list/detail shell rendering live state; legacy pickers untouched. *Shippable: truthful
   sign-in status for Claude/Kimi.*
2. **P1 — API-key sign-in.** `auth_validate_key`/`auth_set_key` + the §3.2 inline flow for
   Anthropic + Moonshot keys. Wire the validated state into `providerUsable` so the composer
   picker trusts bridge state. *Shippable: key-based Claude + Kimi copilot.*
3. **P2 — OAuth sign-in.** `auth_begin_oauth` family; Anthropic loopback flow (54545) and Kimi
   device flow; multi-account rows + switch + logout; re-auth on definitive failure. Retire the
   Python `subscription_login` path for claude/kimi (keep xAI/cursor until their bridge defs
   land). *Shippable: "sign into providers with my accounts" for the must-have stack.*
4. **P3 — Model management + routing.** Bridge-backed Models section + composer dropdown
   cost/capability sublabels; role pickers consume `models_list`; default-model row.
   Fix the known decorative-picker bug (GUI_POLISH_TRACKER O1) by threading the picked
   provider/model into `agent_ask_v2` payloads via `agent_resolve_provider`.
5. **P4 — Cost.** Per-turn `$` footer, session/part/today `cost_summary` strip + breakdown
   popover; usage-window bars from `auth_usage` in Providers tab and composer box.
6. **P5 — Hardening.** OS-keychain seal for the SQLite store (or age/file-key), remaining 42
   OMP providers enabled in batches behind the same ProviderRow contract, `models.yml` custom
   providers if/when needed, web_parity gate entries for any method still desktop-only.

---

## Appendix — reused OMP patterns (file:line)

| Pattern | Location |
|---|---|
| Credential record shapes (`ApiKeyCredential`/`OAuthCredential`/`StoredAuthCredential`) | packages/ai/src/auth-storage.ts:81-140 |
| Credential provenance for UI (`CredentialOrigin`) | auth-storage.ts:101-112 |
| Multi-account summaries (`OAuthAccountSummary`) | auth-storage.ts:778-792 |
| Login identity (email/org/plan, no tokens) | auth-storage.ts:734-745 |
| Login entry point (`AuthStorage.login`) | auth-storage.ts:2589 |
| Provider list from store | auth-storage.ts:7087 |
| Usage window hot-threshold 0.85 | auth-storage.ts:63-68 |
| Usage report TTL 5 min | auth-storage.ts:596-601 |
| OAuth refresh skew/lease | auth-storage.ts:630-647 |
| PKCE | registry/oauth/pkce.ts:5-18 |
| Loopback callback server, `/callback` + `/launch`, port fallback, manual-input mode | registry/oauth/callback-server.ts:17-53, 139, 406-431 |
| Anthropic flow (port 54545, scopes, identity, refresh) | registry/oauth/anthropic.ts:13-25, 204-286, 299-344 |
| Kimi device flow (user_code, 5 s poll, refresh) | registry/oauth/kimi.ts:15-20, 98-149, 174-258, 263-289 |
| OAuth controller callbacks (`onAuth/onProgress/onManualCodeInput`) | registry/oauth/types.ts:27-76 |
| `launchUrl` short-copy contract | registry/oauth/types.ts:33-50 |
| API-key validation (Anthropic + OpenAI-compat + /models) | registry/api-key-validation.ts:30, 48-83, 88-124, 132-161 |
| Model capabilities + cost fields | packages/catalog/src/types.ts:694-743 |
| Usage report / windows / used-fraction | packages/ai/src/usage.ts:54-129 |
| Per-request cost history | packages/ai/src/usage.ts:163-180 |
| Claude 5h/7d usage buckets | packages/ai/src/usage/claude.ts:57-75 |
| Cost/context display formats (`$in/$out`, `200k`) | packages/coding-agent/src/modes/components/model-browser.ts:271-293 |
| Role→model resolution + chips | model-browser.ts:66-119, 256-269 |
| OAuth provider list (`getOAuthProviders`) | registry/oauth/index.ts:171 |
| Manual-paste login fallback (TUI precedent) | packages/coding-agent/src/modes/components/login-dialog.ts |
| Auth-failure ladder (retry → refresh → rotate/re-login) | auth-storage.ts:5421-5423 |

## Appendix — CAD anchors

| Surface | Location |
|---|---|
| Settings modal tabs + Providers tab | host/src/ui/settings_modal.cpp:1-2, 4442-4481 |
| Glass section chrome / tab body / scale | settings_modal.cpp:144-186 |
| Subscription accounts (radio/switch/usage poll) | settings_modal.cpp:207-255, 622-720 |
| Shared provider/model/auth form | host/src/ui/copilot/settings.cpp:175-606 |
| xAI device-code login UX (Kimi precedent) | copilot/settings.cpp:390-552 |
| Quick-settings overlay + settings jump buttons | copilot/settings.cpp:922-1034 |
| Composer model dropdown | copilot/settings.cpp:1225-1400 |
| Composer usage box (plan chip + window bars) | copilot/composer.cpp:1650-1858 |
| Per-turn token footer | copilot/chat_log.cpp:378-388 |
| In-app CEF OAuth modal (multi-account, isolation) | host/src/ui/oauth_capture.h:1-105 |
| Web parity gate + T-RF12 sign-in gating | contracts/web_parity.md:1-33; settings_modal.cpp:32 |
| Existing Python auth RPC family (P0-P2 migration source) | services/agent/worker_rpc.py:47-280 |
| Native provider resolution seam | core/agent/config.cpp `resolve_provider`; core/api/libaicad.cpp:1330,1339 |
