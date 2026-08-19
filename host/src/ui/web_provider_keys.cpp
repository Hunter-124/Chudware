// See web_provider_keys.h. One emscripten_fetch client for /api/providers/keys, mirroring the
// GET pattern already proven in copilot/settings.cpp's wasmKickHostProvidersFetch().
#include "web_provider_keys.h"

#include "model_registry.h"  // ui::PROV_*, ui::cloudServiceKey

#ifdef __EMSCRIPTEN__
#include <cstring>  // std::strcpy into emscripten_fetch_attr_t::requestMethod

#include <emscripten/fetch.h>

#include <nlohmann/json.hpp>
#endif

namespace ui {
namespace {

std::vector<WebProviderKey> g_keys;
std::string g_status;
bool g_statusIsError = false;
bool g_busy = false;

#ifdef __EMSCRIPTEN__
// The list is refetched after every mutation rather than patched locally: the gateway owns the
// validation state and the hint, and guessing them here is how a UI starts lying about what is
// actually stored.
bool g_listStale = true;
bool g_listInFlight = false;
int g_listFails = 0;

// emscripten_fetch callbacks are no-capture function pointers and the fetch does not own the
// body it is handed, so the POST payload lives at file scope until its callback fires. One
// request at a time (g_busy), so one buffer suffices.
std::string g_requestBody;

const char* const kJsonHeaders[] = {"Content-Type", "application/json", nullptr};

void kickList();

// FastAPI reports every refusal as {"detail": ...}; surface the provider's own rejection text
// (provider_keys.py::_safe_provider_error already redacted the key) instead of a bare code.
std::string detailOr(const emscripten_fetch_t* f, const char* fallback) {
    if (f && f->data && f->numBytes > 0) {
        try {
            auto j = nlohmann::json::parse(f->data, f->data + f->numBytes);
            if (j.is_object()) {
                if (auto it = j.find("detail"); it != j.end() && it->is_string())
                    return it->get<std::string>();
            }
        } catch (...) {
        }
    }
    return fallback;
}

void finishMutation(const std::string& message, bool isError) {
    g_busy = false;
    g_status = message;
    g_statusIsError = isError;
    g_listStale = true;
}

void kickList() {
    if (g_listInFlight || !g_listStale) return;
    g_listInFlight = true;
    emscripten_fetch_attr_t attr;
    emscripten_fetch_attr_init(&attr);
    std::strcpy(attr.requestMethod, "GET");
    attr.attributes = EMSCRIPTEN_FETCH_LOAD_TO_MEMORY;
    attr.withCredentials = true;  // the chudware_sid session cookie is the identity
    attr.onsuccess = [](emscripten_fetch_t* f) {
        g_listInFlight = false;
        if (f->status >= 200 && f->status < 300 && f->data && f->numBytes > 0) {
            try {
                auto j = nlohmann::json::parse(f->data, f->data + f->numBytes);
                auto rows = j.is_object() ? j.find("keys") : j.end();
                if (rows != j.end() && rows->is_array()) {
                    std::vector<WebProviderKey> next;
                    next.reserve(rows->size());
                    for (const auto& row : *rows) {
                        if (!row.is_object()) continue;
                        WebProviderKey k;
                        k.provider = row.value("provider", std::string());
                        k.hint = row.value("hint", std::string());
                        k.state = row.value("state", std::string());
                        if (!k.provider.empty()) next.push_back(std::move(k));
                    }
                    g_keys.swap(next);
                    g_listStale = false;
                    g_listFails = 0;
                }
            } catch (...) {
                // A malformed body is a server bug, not a reason to spin: fall through to the
                // retry budget below rather than refetching every frame.
                if (++g_listFails >= 5) g_listStale = false;
            }
        } else if (++g_listFails >= 5) {
            g_listStale = false;
        }
        emscripten_fetch_close(f);
    };
    attr.onerror = [](emscripten_fetch_t* f) {
        g_listInFlight = false;
        if (++g_listFails >= 5) g_listStale = false;  // stop after a few transient failures
        emscripten_fetch_close(f);
    };
    emscripten_fetch(&attr, "/api/providers/keys");
}
#endif  // __EMSCRIPTEN__

}  // namespace

bool webProviderKeysAvailable() {
#ifdef __EMSCRIPTEN__
    return true;
#else
    return false;
#endif
}

std::string webProviderKeyName(int provider, const char* baseUrl) {
    switch (provider) {
        case PROV_ANTHROPIC: return "anthropic";
        case PROV_OPENAI:    return "openai";
        case PROV_GEMINI:    return "gemini";
        case PROV_XAI:       return "xai";
        case PROV_CLOUDCOMPAT: {
            // The cloud slot is one field shared by every base_url-keyed service, so the stored
            // credential has to be named after the service actually selected — that is the wire
            // provider the composer sends and therefore the name engine_bridge.py resolves by.
            // Only the two the gateway can validate are offered.
            const std::string key = cloudServiceKey(baseUrl ? baseUrl : "");
            if (key == "openrouter") return "openrouter";
            if (key == "moonshot") return "moonshot";
            return "";
        }
        default: return "";
    }
}

const std::vector<WebProviderKey>& webProviderKeys() {
#ifdef __EMSCRIPTEN__
    kickList();
#endif
    return g_keys;
}

bool webProviderKeyPresent(const std::string& provider) {
    if (provider.empty()) return false;
    for (const WebProviderKey& k : webProviderKeys())
        if (k.provider == provider) return true;
    return false;
}

void webProviderKeySave(const std::string& provider, const std::string& key) {
#ifdef __EMSCRIPTEN__
    if (g_busy || provider.empty() || key.empty()) return;
    g_busy = true;
    g_status.clear();
    g_statusIsError = false;
    g_requestBody = nlohmann::json{{"provider", provider}, {"key", key}}.dump();

    emscripten_fetch_attr_t attr;
    emscripten_fetch_attr_init(&attr);
    std::strcpy(attr.requestMethod, "POST");
    attr.attributes = EMSCRIPTEN_FETCH_LOAD_TO_MEMORY;
    attr.withCredentials = true;
    attr.requestHeaders = kJsonHeaders;
    attr.requestData = g_requestBody.data();
    attr.requestDataSize = g_requestBody.size();
    attr.onsuccess = [](emscripten_fetch_t* f) {
        finishMutation("Validated and stored for this account. The key is never shown again.",
                       false);
        emscripten_fetch_close(f);
    };
    attr.onerror = [](emscripten_fetch_t* f) {
        // 400 carries the provider's own rejection, 429 the validation rate limit, 503 a gateway
        // that has no encryption key configured. Each is actionable, so none may be flattened.
        const char* fallback = "Could not store the key.";
        if (f->status == 429)
            fallback = "Too many validation attempts — wait a minute and try again.";
        else if (f->status == 503)
            fallback = "This deployment has no provider-key storage configured.";
        else if (f->status == 401 || f->status == 403)
            fallback = "Sign in again — this session is no longer authorized.";
        finishMutation(detailOr(f, fallback), true);
        emscripten_fetch_close(f);
    };
    emscripten_fetch(&attr, "/api/providers/keys");
#else
    (void)provider;
    (void)key;
#endif
}

void webProviderKeyDelete(const std::string& provider) {
#ifdef __EMSCRIPTEN__
    if (g_busy || provider.empty()) return;
    // Provider names come from webProviderKeyName's fixed set, so the path segment needs no
    // escaping — but refuse anything else rather than build a URL from an unchecked string.
    for (char c : provider)
        if (!((c >= 'a' && c <= 'z') || c == '-')) return;
    g_busy = true;
    g_status.clear();
    g_statusIsError = false;
    const std::string url = "/api/providers/keys/" + provider;

    emscripten_fetch_attr_t attr;
    emscripten_fetch_attr_init(&attr);
    std::strcpy(attr.requestMethod, "DELETE");
    attr.attributes = EMSCRIPTEN_FETCH_LOAD_TO_MEMORY;
    attr.withCredentials = true;
    attr.onsuccess = [](emscripten_fetch_t* f) {
        finishMutation("Removed.", false);
        emscripten_fetch_close(f);
    };
    attr.onerror = [](emscripten_fetch_t* f) {
        finishMutation(detailOr(f, "Could not remove the key."), true);
        emscripten_fetch_close(f);
    };
    emscripten_fetch(&attr, url.c_str());
#else
    (void)provider;
#endif
}

bool webProviderKeyBusy() { return g_busy; }

const std::string& webProviderKeyStatus() { return g_status; }

bool webProviderKeyStatusIsError() { return g_statusIsError; }

}  // namespace ui
