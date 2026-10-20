// ui::webProviderKey* — the WASM build's per-account AI-provider credential client.
//
// Why this exists at all: desktop provider login writes credentials into machine-local stores
// consumed by Chudcode. The web build has no per-user machine store: its engine worker is one
// chudware-hostd shared by every tenant, so a credential written there would belong to the
// operator and every tenant at once. The web flow therefore never writes host-local auth.
//
// The gateway's answer is a per-account credential store: POST/GET/DELETE
// /api/providers/keys (Webservice/gateway/provider_keys.py). A key posted there is validated
// against the provider, sealed with AES-256-GCM bound to the account, and kept in
// PostgreSQL. P0 browser containment currently disables agent RPCs; storing a key
// does not enable shared-daemon execution. Desktop credential paths are separate.
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
// The name must equal the wire provider sent with an agent turn, because engine_bridge.py uses
// it to resolve the stored credential. Empty for subscription/hosted-account slots, local
// endpoints, and services without a gateway validation endpoint; drawing a key field for those
// would promise a store that rejects the write.
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
