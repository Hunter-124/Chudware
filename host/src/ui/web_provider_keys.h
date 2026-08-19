// ui::webProviderKey* — the WASM build's per-account AI-provider credential client.
//
// Why this exists at all: on the desktop, "provider login" writes a credential into the
// machine's own store (config.json keys.*, subscription_auth, or the OMP bridge auth db) and
// the agent worker reads it back from the same process. The web build has neither. Its engine
// worker is ONE chudware-hostd shared by every tenant, so a credential written there belongs
// to the operator and to everybody else at once, and Settings -> Providers' OMP flow
// (agent_provider_* RPCs) is refused server-side for any account that is not an admin.
//
// The gateway's answer is a per-account credential store: POST/GET/DELETE
// /api/providers/keys (Webservice/gateway/provider_keys.py). A key posted there is validated
// against the provider, sealed with AES-256-GCM bound to the account, kept in PostgreSQL, and
// resolved server-side for that account's agent turns only (engine_bridge.py _actor_for_turn,
// key_source="byok"). Until this module existed, nothing inside the app called that endpoint —
// the only client was the account portal's own JS page — so a web user had no reachable way to
// give the app a provider credential.
//
// Everything here is a no-op outside __EMSCRIPTEN__: `available()` answers false and the
// desktop credential paths are untouched by construction.
#pragma once

#include <string>
#include <vector>

namespace ui {

// One row of GET /api/providers/keys. `hint` is the last four characters the gateway keeps for
// display; the key itself is never returned by the endpoint and never held here.
struct WebProviderKey {
    std::string provider;  // endpoint provider name (see webProviderKeyName)
    std::string hint;
    std::string state;     // "validated"
};

// True only in the WASM build: /api/providers/keys is the gateway's endpoint, and a desktop
// build has no gateway to ask.
bool webProviderKeysAvailable();

// The /api/providers/keys provider name for an AI-tab selection, or "" when this slot has no
// per-account key path.
//
// The name MUST equal the wire provider the composer sends with an agent turn, because that is
// the string engine_bridge.py resolves the stored credential by. Empty for the subscription and
// OMP-bridge slots (cursor / kimi-code / zenmux), for local endpoints, and for the cloud-slot
// services the gateway has no validation endpoint for (deepseek, alibaba, bedrock, azure,
// antigravity, aimlapi, and an unclassified endpoint) — drawing a key field for those would
// promise a store that will reject the write.
std::string webProviderKeyName(int provider, const char* baseUrl);

// This account's validated credentials, refreshed lazily in the background. The first call
// kicks the GET; callers may render an empty list for a frame or two.
const std::vector<WebProviderKey>& webProviderKeys();

// Has this account posted a validated credential for `provider`? Drives both the AI tab's
// status line and providerUsable(), so a BYOK-only web user sees their models.
bool webProviderKeyPresent(const std::string& provider);

// Validate-and-store / remove one credential for the signed-in account. Both are async; poll
// webProviderKeyBusy() and read webProviderKeyStatus() for the outcome.
void webProviderKeySave(const std::string& provider, const std::string& key);
void webProviderKeyDelete(const std::string& provider);

// A request is in flight. Save performs live provider I/O server-side, so this can be seconds.
bool webProviderKeyBusy();

// Outcome of the last save/delete: the provider's own rejection text on failure, a short
// confirmation on success, empty before the first attempt. Cleared by the next attempt.
const std::string& webProviderKeyStatus();
bool webProviderKeyStatusIsError();

}  // namespace ui
