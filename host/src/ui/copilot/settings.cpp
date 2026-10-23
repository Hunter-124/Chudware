// Copilot AI settings: the shared AI settings sections
// (provider/model/auth, generation, privacy, manage-models — ui::drawAiSettingsSection, called
// once per section by the Settings modal's Providers tab), the searchable model dropdown, model
// availability probing, and provider-usability gating (providerUsable / cloud+moonshot key
// presence).

#include "copilot_internal.h"

#include "../web_capabilities.h"    // ui::webGapReason — subscription sign-in is not a web path
#include "../chudcode_descriptors.h" // ui::parseChudcode* — Chudcode worker payload parsing
#ifdef __EMSCRIPTEN__
#include "core/jobs.h"
#endif
#include "../provider_usage.h"     // shared subscription status cache and single-flight poller

#include <filesystem>
#include <fstream>

namespace ui {

namespace {

// ---- agent_list_models probe scheduling ----------------------------------------------------
// FREEZE FIX — do not regress: probing a DOWN endpoint burns ~5s in a curl connect-fail INSIDE
// the worker while it holds WorkerClient::callMtx_, so any SYNC worker call from the GL thread
// (CAM/ECAD panels on workspace switch) parks behind it and the app looks hard-frozen. At the
// old flat 5s cadence the worker was busy-failing ~100% of the time. Two rules keep that bounded
// now that more than one endpoint is probed:
//   * cooldowns are PER PROVIDER (5s after a success, 60s after a failure) — a dead local server
//     must not suppress the cloud probe, nor an unkeyed cloud provider the local one;
//   * at most ONE agent_list_models is in flight process-wide, so the worker never queues two
//     connect-fails back to back. The per-provider cooldowns make the probes alternate rather
//     than stack: whichever is off cooldown when the slot frees up goes next.
struct ProbeTiming {
    double last = -1e9;     // latest dispatch/completion; cooldown includes a cold slow response
    bool failed = false;    // last completed probe failed -> 60s backoff
    bool inFlight = false;  // this provider's probe is outstanding
    const char* wire = nullptr;
    std::string baseUrl;
    std::string apiKey;
};
// GL-thread only: written from refreshModelAvailability and from the AsyncRpc completion, which
// core::AsyncRpc runs on the GL thread inside drain().
std::unordered_map<int, ProbeTiming> s_probeTiming;

bool startupCatalogFollowupReady(const AppState& st) {
    static double interactiveSince = -1.0;
    if (!st.workerStarted || st.workerBootPending || !st.startupPhase.empty()) {
        interactiveSince = -1.0;
        return false;
    }
    const double now = ImGui::GetTime();
    if (interactiveSince < 0.0) interactiveSince = now;
    return now - interactiveSince >= 1.5;
}

bool anyProbeInFlight() {
    for (const auto& kv : s_probeTiming)
        if (kv.second.inFlight) return true;
    return false;
}

// The wire provider string agent_list_models understands for a ui::ProviderIndex. The C++
// handler (core/api/libchudware.cpp) branches on exactly "openai" | "llamacpp" | "xai" |
// "gemini" | "anthropic" and answers {"ok":false,"error":"unknown provider"} for anything else,
// so the slots with no branch return nullptr and are NEVER probed: Cursor and Kimi Code are
// subscription-proxied with no list endpoint here, and ZenMux is bridge-routed (its catalog
// arrives via refreshBridgeModelCatalog / chudcode_models_list). Probing them would only paint a
// bogus "unknown provider" into providerModels and make the provider look broken.
const char* listModelsWireProvider(int provider) {
    switch (provider) {
        case ui::PROV_ANTHROPIC:   return "anthropic";
        case ui::PROV_OPENAI:      return "openai";
        case ui::PROV_GEMINI:      return "gemini";
        case ui::PROV_XAI:         return "xai";
        case ui::PROV_CLOUDCOMPAT: return "openai";    // OpenAI-compatible API + a cloud base_url
        case ui::PROV_LOCAL:       return "openai";    // local OpenAI-compatible server
        case ui::PROV_LLAMACPP:    return "llamacpp";  // served by the PROV_LOCAL probe below
        case ui::PROV_FREELLMAPI:  return "freellmapi"; // agent_list_models (libchudware.cpp)
                                                        // has a matching "freellmapi" branch —
                                                        // GET's the router's live /v1/models.
        default:                   return nullptr;     // PROV_CURSOR / PROV_KIMISUB / PROV_ZENMUX
    }
}

}  // namespace

// Defined below (they need the WASM-safe JSON accessors / the key buffers declared further down).
static void parseProbeRows(int provider, const nlohmann::json& result, ProviderModels& pm);
static char* providerKeyBuf(Settings& s, int provider);

void refreshModelAvailability(AppState& st, bool force) {
    if (ui::webBuild()) {
        // Tenant cells own provider discovery and credentials. A desktop endpoint
        // probe sends api_key (even when empty), which the browser contract refuses,
        // and localhost would name the cell rather than the user's machine.
        // Request model metadata on the existing credential-free catalog lane;
        // a catalog refusal remains a refusal, never endpoint-availability proof.
        refreshBridgeModelCatalog(st, force);
        return;
    }
    if (!force && (!st.workerStarted || st.workerBootPending || !st.startupPhase.empty()))
        return;
    // One async agent_list_models round-trip. `slots` are the providerModels entries its answer
    // fills (the local endpoint feeds PROV_LOCAL and PROV_LLAMACPP — one endpoint family).
    // Borrowed pointers, not strings: this runs EVERY frame from three call sites, so nothing is
    // allocated until a probe actually clears the gates below.
    // Returns false when the probe was skipped (no C++ branch / already in flight / on cooldown).
    auto dispatch = [&st, force](int provider, const char* baseUrl, const char* apiKey,
                                 std::initializer_list<int> slots) -> bool {
        const char* wire = listModelsWireProvider(provider);
        if (!wire) return false;
        ProbeTiming& t = s_probeTiming[provider];
        if (t.inFlight) return false;               // never two probes for the same provider
        if (anyProbeInFlight()) {
            // Freeze guard: one worker probe at a time. A FORCED refresh must not be swallowed
            // by an unrelated in-flight probe — clear the cooldown so the next frame dispatches.
            if (force) { t.last = -1e9; t.failed = false; }
            return false;
        }
        const double now = ImGui::GetTime();
        if (!force && (now - t.last) < (t.failed ? 60.0 : 5.0)) return false;
        // Two UI provider slots can resolve to the same OpenAI-compatible endpoint.
        // Reuse that answer rather than issuing the identical request under another slot.
        if (!force) {
            for (const auto& [otherProvider, other] : s_probeTiming) {
                if (otherProvider == provider || !other.wire || other.inFlight ||
                    std::strcmp(other.wire, wire) != 0 ||
                    other.baseUrl != (baseUrl ? baseUrl : "") ||
                    other.apiKey != (apiKey ? apiKey : "") ||
                    now - other.last >= (other.failed ? 60.0 : 5.0)) continue;
                if (t.last != other.last) {
                    for (const int slot : slots)
                        st.providerModels[slot] = st.providerModels[otherProvider];
                    t.last = other.last;
                    t.failed = other.failed;
                    t.wire = wire;
                    t.baseUrl = baseUrl ? baseUrl : "";
                    t.apiKey = apiKey ? apiKey : "";
                }
                return false;
            }
        }
        t.last = now;
        t.wire = wire;
        t.baseUrl = baseUrl ? baseUrl : "";
        t.apiKey = apiKey ? apiKey : "";
        t.inFlight = true;
        st.modelProbeInFlight.store(true);
        nlohmann::json p = {{"provider", wire},
                            {"base_url", baseUrl ? baseUrl : ""},
                            {"api_key", apiKey ? apiKey : ""}};
        st.rpc.call("agent_list_models", std::move(p),
                    [&st, provider, slots = std::vector<int>(slots)](const core::RpcResult& r) {
            s_probeTiming[provider].last = ImGui::GetTime();
            if (core::workerLaneBusy(r)) {
                // A running agent turn holds the lane. Transient, not a provider failure: keep
                // the last model list and backoff state, and retry on the normal 5 s cadence.
                s_probeTiming[provider].inFlight = false;
                st.modelProbeInFlight.store(anyProbeInFlight());
                return;
            }
            ProviderModels pm;
            pm.checked = true;
            if (r.ok && r.result.is_object()) {
                pm.ok = r.result.value("ok", false);
                pm.error = r.result.value("error", std::string());
                if (r.result.contains("models") && r.result["models"].is_array())
                    for (const auto& m : r.result["models"])
                        if (m.is_string()) pm.models.push_back(m.get<std::string>());
                parseProbeRows(provider, r.result, pm);   // richer model_rows metadata
                // LM Studio reports which models are actually loaded (vs merely downloaded); when
                // present, the picker shows only those. Absent => every listed model is available.
                pm.loadedKnown = r.result.value("loaded_known", false);
                if (r.result.contains("loaded") && r.result["loaded"].is_array())
                    for (const auto& m : r.result["loaded"])
                        if (m.is_string()) pm.loaded.push_back(m.get<std::string>());
            } else {
                pm.error = r.err.empty() ? "core unavailable" : r.err;
            }
            ProbeTiming& done = s_probeTiming[provider];
            done.inFlight = false;
            done.failed = !pm.ok;   // drives THIS provider's 60s backoff above
            for (size_t i = 0; i < slots.size(); ++i) {
                const int slot = slots[i];
                if (slot < 0 || slot >= ui::PROV_COUNT) continue;
                if (i + 1 == slots.size()) st.providerModels[slot] = std::move(pm);
                else st.providerModels[slot] = pm;
            }
            st.modelProbeInFlight.store(anyProbeInFlight());
        }, 12000);
        return true;
    };

    Settings& s = st.settings;
    // (1) The ACTIVE provider when it is a cloud one. Cloud lists are LIVE (the backend asks the
    // provider's /v1/models, through the Chudcode service when the credential is an
    // OAuth/subscription token), which makes a brand-new release appear with no code change.
    // Send that provider's OWN credential, and its base_url only where one is meaningful: the
    // cloud-compat slot's URL picks the service (OpenRouter / DashScope / DeepSeek / Moonshot),
    // while Anthropic / OpenAI / Gemini / xAI each have a fixed endpoint the backend defaults to
    // and st.settings.baseUrl may still hold a stale local URL from a previous provider. An EMPTY
    // api_key is the NORMAL, supported case for Anthropic subscription users: their OAuth token
    // lives in the bridge auth db and the C++ branch resolves it from there.
    if (ui::providerIsCloud(s.provider))
        dispatch(s.provider,
                 (s.provider == ui::PROV_CLOUDCOMPAT || s.provider == ui::PROV_FREELLMAPI)
                     ? s.baseUrl : "",
                 providerKeyBuf(s, s.provider), {s.provider});

    // (2) The LOCAL endpoint, regardless of which provider is active, so local models always show
    // up in the picker WITHOUT the user first switching the Provider dropdown to "Local".
    // Previously this early-returned whenever a cloud provider (OpenRouter / DashScope / ...) was
    // active — st.settings.baseUrl is then a remote URL — which is exactly why local models were
    // invisible until the user "poked around in settings" and selected Local.
    // When the active base_url IS loopback, probe it (honors a custom local port). Otherwise a
    // cloud provider is active, so its base_url isn't the local one — fall back to the default LM
    // Studio endpoint so local discovery still happens. (A non-default local port is only
    // auto-found while the Local provider is the active one; the default covers the common case.)
    const bool activeLoopback = ui::baseUrlIsLoopback(s.baseUrl);
    dispatch(ui::PROV_LOCAL,
             activeLoopback ? s.baseUrl : "http://localhost:1234/v1",
             activeLoopback ? s.apiKey : "",
             {ui::PROV_LOCAL, ui::PROV_LLAMACPP});
}


// The active provider's API-key buffer (cloud providers keep separate keys; local reuses the
// optional apiKey buffer). Moonshot (api.moonshot.ai) uses keys.moonshot, not the generic
// cloud_compat slot, so the active cloud preset can keep OpenRouter/DashScope/DeepSeek keys intact.
static char* providerKeyBuf(Settings& s, int provider) {
    switch (provider) {
        case ui::PROV_GEMINI: return s.geminiKey;
        case ui::PROV_ANTHROPIC: return s.anthropicKey;
        case ui::PROV_OPENAI: return s.openaiKey;
        case ui::PROV_CLOUDCOMPAT:
            if (std::string(s.baseUrl).find("moonshot.ai") != std::string::npos)
                return s.moonshotKey;
            return s.cloudKey;  // DashScope / DeepSeek / OpenRouter
        case ui::PROV_XAI: return s.xaiKey;
        case ui::PROV_FREELLMAPI: return s.freellmapiKey;
        default: return s.apiKey;  // local OpenAI-compatible optional key
    }
}

// Availability helpers (defined with the model-dropdown section below; forward-declared so the
// provider/model section inside drawAiSettingsSection can call them).
// providerUsable is public (copilot.h) — used by Settings role/Eng.Firm pickers too.
bool providerUsable(AppState& st, int provider);  // def below; also declared in copilot.h
static bool localModelLoaded(const ProviderModels& pm, const std::string& id);

// WASM-safe JSON accessors: present-but-null / wrong-type fields must not throw
// (WASM builds disable exception catching → page abort on type_error.302).
static std::string jsonOptStr(const nlohmann::json& obj, const char* key) {
    if (!obj.is_object()) return {};
    auto it = obj.find(key);
    return it != obj.end() && it->is_string() ? it->get<std::string>() : std::string();
}
static bool jsonOptBool(const nlohmann::json& obj, const char* key, bool dflt = false) {
    if (!obj.is_object()) return dflt;
    auto it = obj.find(key);
    if (it == obj.end() || it->is_null()) return dflt;
    return it->is_boolean() ? it->get<bool>() : dflt;
}
static int jsonOptInt(const nlohmann::json& obj, const char* key, int dflt = 0) {
    if (!obj.is_object()) return dflt;
    auto it = obj.find(key);
    if (it == obj.end() || it->is_null()) return dflt;
    return it->is_number() ? (int)it->get<double>() : dflt;
}
static nlohmann::json jsonOptObj(const nlohmann::json& obj, const char* key) {
    if (!obj.is_object()) return nlohmann::json::object();
    auto it = obj.find(key);
    if (it == obj.end() || !it->is_object()) return nlohmann::json::object();
    return *it;
}

// agent_list_models `model_rows` -> ProviderModels::rows, ORDER PRESERVED: cloud list endpoints
// answer newest-first, so keeping their order is what puts a just-released model at the TOP of
// the picker. Rows carry the capability metadata the static catalog can only guess at (context /
// max_output / vision / tools / reasoning_efforts / auto_source). Id-less rows are dropped, and
// duplicates are collapsed so a merged bridge+direct list can't show one model twice.
static void parseProbeRows(int provider, const nlohmann::json& result, ProviderModels& pm) {
    if (!result.is_object()) return;
    auto rows = result.find("model_rows");
    if (rows == result.end() || !rows->is_array()) return;
    std::set<std::string> seen;
    pm.rows.reserve(rows->size());
    for (const auto& row : *rows) {
        if (!row.is_object()) continue;
        std::string id = jsonOptStr(row, "id");
        if (id.empty() || !seen.insert(id).second) continue;
        std::vector<std::string> efforts;
        auto ef = row.find("reasoning_efforts");
        if (ef != row.end() && ef->is_array())
            for (const auto& e : *ef)
                if (e.is_string()) efforts.push_back(e.get<std::string>());
        pm.rows.push_back(ui::modelInfoFromProbe(
            provider, id, jsonOptStr(row, "label"), jsonOptInt(row, "context"),
            jsonOptInt(row, "max_output"), jsonOptBool(row, "vision", false),
            jsonOptBool(row, "tools", true), efforts, jsonOptStr(row, "auto_source")));
    }
}

// The model rows the picker should show for ONE provider (copilot.h). Live probe rows win when
// this provider's agent_list_models answered — that is the whole point of the probe: a model
// released after this build ships still appears, in the API's newest-first order. Anything else
// (probe never ran, unreachable, unkeyed, or answered with flat ids only) falls back to the
// static builtinModels() catalog filtered to the provider. Local/llama.cpp are NOT routed here:
// they keep their own flat `models` + loaded-vs-downloaded path.
std::vector<ModelInfo> providerPickerModels(AppState& st, int provider) {
    std::vector<ModelInfo> out;
    if (provider >= 0 && provider < ui::PROV_COUNT && ui::providerIsCloud(provider)) {
        const ProviderModels& pm = st.providerModels[provider];
        if (pm.checked && pm.ok && !pm.rows.empty()) return pm.rows;
    }
    for (const auto& m : ui::builtinModels())
        if (m.provider == provider) out.push_back(m);
    return out;
}

// Every provider card routes its credential copy through here. The AI tab never holds
// credentials: Settings -> Providers validates a key and the Chudcode auth store persists it, so
// this is the pointer to that tab. The browser build has no provider credential path; it says so.
static void drawValidatedCredentialHint(AppState& st, const char* detail) {
    if (const char* why = ui::webGapReason("account_signin")) {
        dimText(why);
        return;
    }
    dimText(detail);
    if (ImGui::SmallButton("Manage validated credentials##provider_credentials")) {
        st.openSettings = true;
        st.openSettingsTab = 3;
    }
}

// Free AI (FreeLLMAPI, github.com/tashfeenahmed/freellmapi) provider card — reused from BOTH
// the classic Settings -> AI provider picker and the live Settings -> Providers tab. Endpoint
// and model controls remain here; credentials never do. All keys are validated and stored by
// the Chudcode auth flow in Settings -> Providers.
// Returns true when a non-credential field changed (caller saves).
bool drawFreeAiProviderCard(AppState& st) {
    Settings& s = st.settings;
    const float S = ui::uiScale;
    bool changed = false;
    const bool active = (s.provider == ui::PROV_FREELLMAPI);
    dimText("Aggregates free tiers from ~28 cloud AI providers behind one OpenAI-compatible "
            "endpoint + one unified key. Runs on YOUR machine (or a box you control) and "
            "forwards requests to real cloud providers over the internet \xe2\x80\x94 blocked "
            "in Offline (ITAR) mode regardless of how local its own endpoint looks.");
    if (active) {
        ImGui::TextUnformatted("Router endpoint (base URL)");
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##flabase", s.baseUrl, sizeof(s.baseUrl))) {
            changed = true;
            refreshModelAvailability(st, true);
        }
    } else {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusOk));
        ImGui::TextUnformatted("Not the active chat provider \xe2\x80\x94 switch now if desired.");
        ImGui::PopStyleColor();
        if (ImGui::SmallButton("Use Free AI as provider##fla_activate")) {
            s.provider = ui::PROV_FREELLMAPI;
            if (!s.baseUrl[0] || std::strstr(s.baseUrl, "1234") || std::strstr(s.baseUrl, "moonshot") ||
                std::strstr(s.baseUrl, "dashscope") || std::strstr(s.baseUrl, "deepseek") ||
                std::strstr(s.baseUrl, "openrouter"))
                std::strncpy(s.baseUrl, "http://localhost:3001/v1", sizeof(s.baseUrl) - 1);
            if (!s.model[0] || std::strstr(s.model, ".gguf") || std::strstr(s.model, "gemini") ||
                std::strstr(s.model, "claude"))
                std::strncpy(s.model, "auto", sizeof(s.model) - 1);
            s.vision = true;
            saveSettings(s);
            refreshModelAvailability(st, true);
        }
    }
    drawValidatedCredentialHint(
        st, "Free AI credentials are added under Settings \xe2\x86\x92 Providers, where the router validates them before AuthStorage persists them.");
    if (active) {
        if (ImGui::Checkbox("Model supports vision (send screenshots)", &s.vision)) changed = true;
        const ProviderModels& flapm = st.providerModels[ui::PROV_FREELLMAPI];
        const char* msg; ImU32 col;
        if (!flapm.checked) { msg = "checking router\xe2\x80\xa6"; col = ui::Palette::toU32(ui::palette.textDim); }
        else if (flapm.ok) { msg = "router reachable"; col = ui::Palette::toU32(ui::palette.statusOk); }
        else { msg = "router unreachable \xe2\x80\x94 install/start it below"; col = ui::Palette::toU32(ui::palette.statusErr); }
        ImGui::PushStyleColor(ImGuiCol_Text, col);
        ImGui::TextUnformatted(msg);
        ImGui::PopStyleColor();
    }

    ImGui::Dummy(ImVec2(0, 8 * S));
    ImGui::Separator();
    ImGui::Dummy(ImVec2(0, 4 * S));
    ImGui::TextUnformatted("Don't have FreeLLMAPI installed yet?");
    dimText("One-liner (Docker) sets up ~/freellmapi and starts the router. A native desktop "
            "tray app (macOS/Windows) is also available from Releases. Either way, open the "
            "dashboard afterward, add your free provider keys under Keys, then add the unified "
            "key through Settings \xe2\x86\x92 Providers so it is validated before storage.");
    static const std::string kInstallCmd = "curl -fsSL https://freellmapi.co/install.sh | bash";
    if (ImGui::SmallButton("Copy install command##fla"))
        ImGui::SetClipboardText(kInstallCmd.c_str());
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", kInstallCmd.c_str());
    ImGui::SameLine(0, 6.0f * S);
    if (ImGui::SmallButton("Download desktop app##fla"))
        ui::openExternalHttpUrl("https://github.com/tashfeenahmed/freellmapi/releases/latest");
    ImGui::SameLine(0, 6.0f * S);
    if (ImGui::SmallButton("Install guide##fla"))
        ui::openExternalHttpUrl("https://github.com/tashfeenahmed/freellmapi#quick-start");
    ImGui::SameLine(0, 6.0f * S);
    {
        std::string dashUrl = (active && s.baseUrl[0]) ? s.baseUrl : "http://localhost:3001/v1";
        const size_t v1 = dashUrl.rfind("/v1");
        if (v1 != std::string::npos && v1 == dashUrl.size() - 3) dashUrl.resize(v1);
        if (ImGui::SmallButton("Open dashboard##fla")) ui::openExternalHttpUrl(dashUrl);
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Opens %s in your default browser (Keys page has the unified key "
                              "once the router is running).", dashUrl.c_str());
    }
    return changed;
}

// Shared AI settings, split by section so Settings can glass-group them.
// ProviderModel = provider + model + auth/status; Generation = effort/tokens/window.
bool drawAiSettingsSection(AppState& st, bool busy, AiSettingsSection section) {
    (void)busy;
    Settings& s = st.settings;
    const float S = ui::uiScale;
    bool changed = false;
    refreshModelAvailability(st, false);

    // Explicit provider switches choose the newest non-dated flagship in the live catalog.
    // A model already saved by the user is never migrated behind their back.
    auto newest = [](const char* provider, const char* family, const char* fallback) {
        std::string selected;
        int bestMajor = -1, bestMinor = -1;
        const size_t stemLen = std::strlen(family);
        for (const auto& row : ui::bridgeModelCatalog()) {
            if (row.provider != provider || row.deprecated ||
                row.id.compare(0, stemLen, family) != 0) continue;
            const char* pos = row.id.c_str() + stemLen;
            if (!std::isdigit(static_cast<unsigned char>(*pos))) continue;
            char* end = nullptr;
            const long major = std::strtol(pos, &end, 10);
            long minor = 0;
            if ((*end == '-' || *end == '.') &&
                std::isdigit(static_cast<unsigned char>(end[1]))) {
                pos = end + 1;
                minor = std::strtol(pos, &end, 10);
            }
            // Exclude dated/specialized IDs; the canonical alias is stable across refreshes.
            if (std::strcmp(family, "gpt-") == 0) {
                if (std::strcmp(end, "-sol") != 0) continue;
            } else if (std::strcmp(family, "gemini-") == 0) {
                if (std::strcmp(end, "-pro-preview") != 0 &&
                    std::strcmp(end, "-pro") != 0) continue;
            } else if (*end != '\0') continue;
            if (major > 100 || minor > 100) continue;
            if (major > bestMajor || (major == bestMajor && minor > bestMinor)) {
                selected = row.id; bestMajor = static_cast<int>(major);
                bestMinor = static_cast<int>(minor);
            }
        }
        return selected.empty() ? std::string(fallback) : selected;
    };
    auto selectModel = [&](const std::string& model) {
        std::strncpy(s.model, model.c_str(), sizeof(s.model) - 1);
        s.model[sizeof(s.model) - 1] = '\0';
    };
    auto drawProviderModel = [&]() {
    // ---- provider ----
    ImGui::TextUnformatted("Provider");
    ImGui::SetNextItemWidth(-1);
    const char* provs[ui::PROV_COUNT];
    for (int i = 0; i < ui::PROV_COUNT; ++i) provs[i] = ui::providerLabel(i);
    int prevProvider = s.provider;
    if (ImGui::Combo("##provider", &s.provider, provs, ui::PROV_COUNT) &&
        s.provider != prevProvider) {
        changed = true;
        // Set sensible per-provider defaults on switch.
        if (s.provider == ui::PROV_GEMINI)
            selectModel(newest("google", "gemini-", "gemini-3.1-pro-preview"));
        else if (s.provider == ui::PROV_ANTHROPIC)
            selectModel(newest("anthropic", "claude-opus-", "claude-opus-5-5"));
        else if (s.provider == ui::PROV_OPENAI)
            selectModel(newest("openai", "gpt-", "gpt-4o"));
        else if (s.provider == ui::PROV_LOCAL) {
            std::strncpy(s.baseUrl, "http://localhost:1234/v1", sizeof(s.baseUrl) - 1);
            if (std::strstr(s.model, ".gguf") || std::strstr(s.model, "gemini") ||
                std::strstr(s.model, "claude"))
                std::strncpy(s.model, "gemma-4-e4b", sizeof(s.model) - 1);
            s.vision = true;
        } else if (s.provider == ui::PROV_LLAMACPP) {
            std::strncpy(s.baseUrl, "http://127.0.0.1:1234/v1", sizeof(s.baseUrl) - 1);
            if (!std::strstr(s.model, ".gguf"))
                std::strncpy(s.model, "%APPDATA%\\Chudware\\models\\model.gguf",
                             sizeof(s.model) - 1);
            s.vision = false;
        } else if (s.provider == ui::PROV_CLOUDCOMPAT) {
            // OpenRouter is the paid-model hub going forward — default the cloud slot to it.
            const auto& cp = ui::cloudPresets();
            if (!cp.empty()) {
                std::strncpy(s.model, cp[0].id.c_str(), sizeof(s.model) - 1);
                std::strncpy(s.baseUrl, cp[0].baseUrl.c_str(), sizeof(s.baseUrl) - 1);
                s.vision = cp[0].vision;
            }
        } else if (s.provider == ui::PROV_XAI) {
            std::strncpy(s.model, "grok-4.5", sizeof(s.model) - 1);
            s.vision = true;
        } else if (s.provider == ui::PROV_CURSOR) {
            std::strncpy(s.model, "composer-2.5", sizeof(s.model) - 1);
            s.vision = true;
        } else if (s.provider == ui::PROV_KIMISUB) {
            std::strncpy(s.model, "k3", sizeof(s.model) - 1);
            s.vision = true;
        } else if (s.provider == ui::PROV_ZENMUX) {
            std::strncpy(s.model, "x-ai/grok-4.5-free", sizeof(s.model) - 1);
            s.vision = true;
        } else if (s.provider == ui::PROV_FREELLMAPI) {
            if (!s.baseUrl[0] || std::strstr(s.baseUrl, "1234") || std::strstr(s.baseUrl, "moonshot") ||
                std::strstr(s.baseUrl, "dashscope") || std::strstr(s.baseUrl, "deepseek") ||
                std::strstr(s.baseUrl, "openrouter"))
                std::strncpy(s.baseUrl, "http://localhost:3001/v1", sizeof(s.baseUrl) - 1);
            if (!s.model[0] || std::strstr(s.model, ".gguf") || std::strstr(s.model, "gemini") ||
                std::strstr(s.model, "claude"))
                std::strncpy(s.model, "auto", sizeof(s.model) - 1);
            s.vision = true;
        }
        // Populate the newly selected provider's list NOW rather than at the next timer tick:
        // force skips that provider's cooldown. Still fully async — no worker call blocks here.
        refreshModelAvailability(st, true);
    }

    ImGui::Dummy(ImVec2(0, 4 * S));
    const bool cloud = ui::providerIsCloud(s.provider);

    // ---- model ----
    if (s.provider == ui::PROV_CLOUDCOMPAT) {
        // Cloud OpenAI-compatible API: OpenRouter (paid-model hub) + Alibaba DashScope + DeepSeek.
        // A preset bundles the service base URL + model id; credentials edited below.
        ImGui::TextUnformatted("Service / model preset");
        dimText("OpenRouter (one key, many models — paid hub), Alibaba Cloud (DashScope) Qwen, "
                "and the official DeepSeek API. CLOUD providers — Offline (ITAR) mode refuses them.");
        const auto& cps = ui::cloudPresets();
        std::vector<const char*> plabels;
        plabels.push_back("(choose a preset\xe2\x80\xa6)");
        for (const auto& p : cps) plabels.push_back(p.label.c_str());
        int psel = 0;
        for (int i = 0; i < (int)cps.size(); ++i)
            if (s.model[0] && cps[i].id == s.model) { psel = i + 1; break; }
        ImGui::SetNextItemWidth(-1);
        if (ImGui::Combo("##cloudpreset", &psel, plabels.data(), (int)plabels.size()) &&
            psel > 0 && psel - 1 < (int)cps.size()) {
            const auto& p = cps[psel - 1];
            std::strncpy(s.model, p.id.c_str(), sizeof(s.model) - 1);
            std::strncpy(s.baseUrl, p.baseUrl.c_str(), sizeof(s.baseUrl) - 1);
            s.vision = p.vision;
            changed = true;
        }
        if (psel > 0 && psel - 1 < (int)cps.size() && ImGui::IsItemHovered())
            ImGui::SetTooltip("%s", cps[psel - 1].note.c_str());
        ImGui::Dummy(ImVec2(0, 4 * S));
        ImGui::TextUnformatted("Model id");
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##cloudmodel", s.model, sizeof(s.model))) changed = true;
    } else if (s.provider == ui::PROV_FREELLMAPI) {
        // Free AI (FreeLLMAPI) — the router's live /v1/models is huge and shifts weekly
        // (339 endpoints across 28 providers), so this mirrors the LOCAL branch below (probed
        // list + free-text fallback) rather than the fixed static catalog cloud providers use.
        // A quick-preset row fills the router's own stable virtual "meta" models first.
        ImGui::TextUnformatted("Quick pick");
        dimText("\"auto\" lets the router choose the best available free model per request with "
                "automatic failover \xe2\x80\x94 the recommended default. Or pick a concrete model "
                "id below once you know which free-tier models you want.");
        const auto& flap = ui::freellmapiPresets();
        std::vector<const char*> plabels;
        plabels.push_back("(choose\xe2\x80\xa6)");
        for (const auto& p : flap) plabels.push_back(p.label.c_str());
        int psel = 0;
        for (int i = 0; i < (int)flap.size(); ++i)
            if (s.model[0] && flap[i].id == s.model) { psel = i + 1; break; }
        ImGui::SetNextItemWidth(-1);
        if (ImGui::Combo("##flapreset", &psel, plabels.data(), (int)plabels.size()) &&
            psel > 0 && psel - 1 < (int)flap.size()) {
            std::strncpy(s.model, flap[psel - 1].id.c_str(), sizeof(s.model) - 1);
            s.vision = flap[psel - 1].vision;
            changed = true;
        }
        if (psel > 0 && psel - 1 < (int)flap.size() && ImGui::IsItemHovered())
            ImGui::SetTooltip("%s", flap[psel - 1].note.c_str());
        ImGui::Dummy(ImVec2(0, 4 * S));
        ImGui::TextUnformatted("Model id");
        const ProviderModels& flapm = st.providerModels[ui::PROV_FREELLMAPI];
        if (flapm.ok && !flapm.models.empty()) {
            std::vector<const char*> ids;
            for (const auto& id : flapm.models) ids.push_back(id.c_str());
            int cur = -1;
            for (size_t i = 0; i < ids.size(); ++i)
                if (s.model[0] && std::strcmp(ids[i], s.model) == 0) cur = (int)i;
            std::string customLabel;
            if (cur < 0) {
                customLabel = std::string(s.model[0] ? s.model : "(select)") + "  (custom)";
                ids.push_back(customLabel.c_str());
                cur = (int)ids.size() - 1;
            }
            ImGui::SetNextItemWidth(-1);
            if (ImGui::Combo("##flapmodel", &cur, ids.data(), (int)ids.size())) {
                if (cur >= 0 && (size_t)cur < flapm.models.size()) {
                    std::strncpy(s.model, flapm.models[cur].c_str(), sizeof(s.model) - 1);
                    changed = true;
                }
            }
            dimText(("live from " + std::string(s.baseUrl) + "/models").c_str());
        } else {
            ImGui::SetNextItemWidth(-1);
            if (ImGui::InputText("##flapmodeltext", s.model, sizeof(s.model))) changed = true;
        }
    } else if (cloud) {
        // This provider's LIVE list when its probe answered (newest first — a Claude release
        // that postdates this build shows up at the top with no catalog edit), else the
        // known-good static catalog. `avail` must outlive the combo: ids/labels point into it.
        const std::vector<ui::ModelInfo> avail = ui::providerPickerModels(st, s.provider);
        std::vector<const char*> ids, labels;
        for (const auto& m : avail) { ids.push_back(m.id.c_str()); labels.push_back(m.label.c_str()); }
        int cur = -1;
        for (size_t i = 0; i < ids.size(); ++i)
            if (s.model[0] && std::strcmp(ids[i], s.model) == 0) cur = (int)i;
        std::string customLabel;
        if (cur < 0) {  // present whatever is configured as a trailing custom entry
            customLabel = std::string(s.model[0] ? s.model : "(choose a model)") + "  (custom)";
            labels.push_back(customLabel.c_str());
            cur = (int)labels.size() - 1;
        }
        ImGui::TextUnformatted("Model");
        ImGui::SetNextItemWidth(-1);
        if (ImGui::Combo("##modelpick", &cur, labels.data(), (int)labels.size())) {
            if (cur >= 0 && cur < (int)ids.size()) {
                std::strncpy(s.model, ids[cur], sizeof(s.model) - 1);
                changed = true;
            }
        }
    } else {
        // Local: list discovered models when the endpoint is up; else a free-text field.
        const ProviderModels& pm = st.providerModels[s.provider == ui::PROV_LLAMACPP
                                                          ? ui::PROV_LLAMACPP : ui::PROV_LOCAL];
        bool useGguf = (s.provider == ui::PROV_LLAMACPP);
        // Experimental: one-click presets for the research-backed open-weight local models
        // (Qwen2.5-Coder / Qwen2.5-VL / DeepSeek-R1). Fills the model id + vision flag + a default
        // base URL; the weights must already be loaded in the local server.
        if (!useGguf) {
            ImGui::TextUnformatted("Quick preset");
            dimText("Open-weight, self-hostable models recommended for the copilot (ITAR-safe). "
                    "Fills the model id, vision flag, and a default base URL. The weights must "
                    "already be loaded in your local server.");
            const auto& presets = ui::localPresets();
            std::vector<const char*> plabels;
            plabels.push_back("(choose a preset…)");
            for (const auto& p : presets) plabels.push_back(p.label.c_str());
            int psel = 0;
            ImGui::SetNextItemWidth(-1);
            if (ImGui::Combo("##localpreset", &psel, plabels.data(), (int)plabels.size()) &&
                psel > 0 && psel - 1 < (int)presets.size()) {
                const auto& p = presets[psel - 1];
                std::strncpy(s.model, p.id.c_str(), sizeof(s.model) - 1);
                if (!p.baseUrl.empty())
                    std::strncpy(s.baseUrl, p.baseUrl.c_str(), sizeof(s.baseUrl) - 1);
                s.vision = p.vision;
                changed = true;
                refreshModelAvailability(st, true);  // re-probe for the (possibly new) endpoint
            }
            if (psel > 0 && psel - 1 < (int)presets.size() && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", presets[psel - 1].note.c_str());
            ImGui::Dummy(ImVec2(0, 4 * S));
        }
        ImGui::TextUnformatted(useGguf ? "GGUF model path" : "Model");
        if (!useGguf && pm.ok && !pm.models.empty()) {
            std::vector<const char*> ids;
            for (const auto& id : pm.models) ids.push_back(id.c_str());
            int cur = -1;
            for (size_t i = 0; i < ids.size(); ++i)
                if (s.model[0] && std::strcmp(ids[i], s.model) == 0) cur = (int)i;
            std::string customLabel;
            if (cur < 0) { customLabel = std::string(s.model[0] ? s.model : "(select)") + "  (custom)";
                           ids.push_back(customLabel.c_str()); cur = (int)ids.size() - 1; }
            ImGui::SetNextItemWidth(-1);
            if (ImGui::Combo("##localmodel", &cur, ids.data(), (int)ids.size())) {
                if (cur >= 0 && (size_t)cur < pm.models.size()) {
                    std::strncpy(s.model, pm.models[cur].c_str(), sizeof(s.model) - 1);
                    changed = true;
                }
            }
        } else {
            ImGui::SetNextItemWidth(-1);
            if (ImGui::InputText("##localmodeltext", s.model, sizeof(s.model))) changed = true;
        }
    }

    // ---- credentials / connection ----
    ImGui::Dummy(ImVec2(0, 6 * S));
    if (s.provider == ui::PROV_CLOUDCOMPAT) {
        // Cloud OpenAI-compatible endpoint. Credentials are intentionally absent from this AI
        // tab: Settings -> Providers validates them before the Chudcode auth store persists them.
        const char* svc = ui::cloudServiceKey(s.baseUrl);
        const bool isOR = (std::strcmp(svc, "openrouter") == 0);
        const bool isMS = (std::strcmp(svc, "moonshot") == 0);
        const bool bridgeAuth = !ui::cloudServiceUsesLocalKey(svc);
        ImGui::TextUnformatted(bridgeAuth
            ? "Service endpoint (identifies the bridge provider)"
            : (isOR ? "OpenRouter endpoint"
                    : (isMS ? "Moonshot endpoint" : "Endpoint (base URL)")));
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##cloudbase", s.baseUrl, sizeof(s.baseUrl))) changed = true;
        if (ImGui::Checkbox("Model supports vision (send screenshots)", &s.vision))
            changed = true;
        drawValidatedCredentialHint(
            st, bridgeAuth
                    ? "This service uses the agent credential store. Add or replace its credential under Settings \xe2\x86\x92 Providers."
                    : "API keys are no longer pasted into the AI tab. Add one under Settings \xe2\x86\x92 Providers; it is saved only after the provider validates it.");
    } else if (s.provider == ui::PROV_FREELLMAPI) {
        if (drawFreeAiProviderCard(st)) changed = true;
    } else if (s.provider == ui::PROV_XAI && ui::webGapReason("account_signin")) {
        // Web: the device-code flow would sign the ONE shared engine process into an xAI
        // account, so the button is not drawn at all.
        dimText(ui::webGapReason("account_signin"));
        if (ImGui::Checkbox("Model supports vision (send screenshots)", &s.vision)) changed = true;
    } else if (s.provider == ui::PROV_XAI) {
        // xAI: SuperGrok / X Premium+ device-code OAuth AND/OR classic API key (console.x.ai).
        // Sign-in uses subscription_login{provider:"xai",device:true}; GUI polls subscription_status.
        static bool s_xaiLoginBusy = false;
        static bool s_xaiConnected = false;
        static bool s_xaiBrowserOpened = false;
        static std::string s_xaiEmail;
        static std::string s_xaiError;
        static std::string s_xaiUserCode;
        static std::string s_xaiVerifyUrl;
        static std::string s_xaiStage;
        static double s_xaiLastPoll = -1e9;

        auto pollXaiStatus = [&](bool force) {
            const double now = ImGui::GetTime();
            if (force || now - s_xaiLastPoll >= 1.5) {
                s_xaiLastPoll = now;
                ui::providerUsagePollStatus(st, 1.5, force);
            }
            const auto& login = ui::providerUsageLogin();
            const auto it = login.find("xai");
            if (it == login.end()) return;
            const auto& x = it->second;
            s_xaiConnected = x.connected;
            s_xaiEmail = x.email;
            s_xaiLoginBusy = x.inProgress;
            s_xaiBrowserOpened = x.browserOpened;
            s_xaiUserCode = x.userCode;
            s_xaiVerifyUrl = x.verifyUrl;
            s_xaiStage = x.stage;
            if (!x.lastError.empty())
                s_xaiError = x.lastError;
            else if (s_xaiConnected)
                s_xaiError.clear();
        };
        pollXaiStatus(false);

        dimText("SuperGrok / X Premium+ subscription (device-code OAuth) or API key from console.x.ai.");
        if (s_xaiLoginBusy) {
            // Opening browser… then show the device code + fallback link.
            if (s_xaiUserCode.empty() && s_xaiVerifyUrl.empty()) {
                ImGui::TextUnformatted("Opening browser\xe2\x80\xa6");
            } else {
                if (s_xaiBrowserOpened)
                    ImGui::TextUnformatted("Browser opened \xe2\x80\x94 approve access, then wait\xe2\x80\xa6");
                else
                    ImGui::TextUnformatted("Could not auto-open browser \xe2\x80\x94 open the link below:");
                if (!s_xaiUserCode.empty()) {
                    ImGui::Text("Code: %s", s_xaiUserCode.c_str());
                    if (ImGui::IsItemHovered())
                        ImGui::SetTooltip("Click 'Copy code' to copy this to the clipboard.");
                    ImGui::SameLine();
                    if (ImGui::SmallButton("Copy code##xai"))
                        ImGui::SetClipboardText(s_xaiUserCode.c_str());
                }
                const std::string fallbackUrl =
                    s_xaiVerifyUrl.empty()
                        ? (s_xaiUserCode.empty()
                               ? "https://accounts.x.ai/oauth2/device"
                               : ("https://accounts.x.ai/oauth2/device?user_code=" +
                                  s_xaiUserCode))
                        : s_xaiVerifyUrl;
                ImGui::TextWrapped("%s", fallbackUrl.c_str());
                if (ImGui::SmallButton("Copy URL##xai"))
                    ImGui::SetClipboardText(fallbackUrl.c_str());
                ImGui::SameLine();
                if (ImGui::SmallButton("Open in browser##xai")) {
                    // Best-effort local open when the worker process is headless.
                    if (!ui::openExternalHttpUrl(fallbackUrl))
                        s_xaiError = "Could not open the sign-in link in your browser.";
                }
            }
            pollXaiStatus(false);
        } else if (s_xaiConnected) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusOk));
            if (!s_xaiEmail.empty())
                ImGui::Text("Connected as %s", s_xaiEmail.c_str());
            else
                ImGui::TextUnformatted("Connected via SuperGrok / X Premium+");
            ImGui::PopStyleColor();
            if (ImGui::SmallButton("Sign out##xai")) {
                st.rpc.call("subscription_logout", nlohmann::json{{"provider", "xai"}},
                            [](const core::RpcResult&) {});
                s_xaiConnected = false;
                s_xaiEmail.clear();
                s_xaiError.clear();
                s_xaiUserCode.clear();
                s_xaiVerifyUrl.clear();
                s_xaiStage.clear();
                s_xaiBrowserOpened = false;
                s_xaiLastPoll = -1e9;
                ui::providerUsageInvalidate();
            }
        } else {
            if (ImGui::Button("Sign in with xAI", ImVec2(-1, 0))) {
                s_xaiLoginBusy = true;
                s_xaiError.clear();
                s_xaiUserCode.clear();
                s_xaiVerifyUrl.clear();
                s_xaiStage = "start";
                s_xaiBrowserOpened = false;
                st.rpc.call("subscription_login",
                            nlohmann::json{{"provider", "xai"}, {"device", true}},
                            [](const core::RpcResult& r) {
                    if (!r.ok || core::workerLaneBusy(r)) {
                        s_xaiLoginBusy = false;
                        s_xaiError = userFacingRpcError(r, "Sign-in didn't start. Try again.");
                    } else if (r.result.is_object() && jsonOptBool(r.result, "ok", true) == false) {
                        s_xaiLoginBusy = false;
                        auto reason = jsonOptStr(r.result, "reason");
                        auto hint = jsonOptStr(r.result, "hint");
                        s_xaiError = reason.empty() ? "login failed" : reason;
                        if (!hint.empty()) s_xaiError += " — " + hint;
                    }
                    s_xaiLastPoll = -1e9;
                    ui::providerUsageInvalidate();
                });
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip(
                    "Opens the accounts.x.ai device-code flow in your browser.\n"
                    "Approve on phone/browser; status flips to Connected when done.");
        }
        if (!s_xaiError.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.statusErr));
            ImGui::TextWrapped("%s", s_xaiError.c_str());
            ImGui::PopStyleColor();
            // Honest Heavy-gating surface: if the reason mentions API-enabled / Heavy, keep the
            // console.x.ai hint visible (subscription_login already includes it).
        }

        drawValidatedCredentialHint(
            st, "Use the system-browser xAI sign-in above, or add an xAI API key under Settings \xe2\x86\x92 Providers. Keys are saved only after xAI validates them.");
        if (ImGui::Checkbox("Model supports vision (send screenshots)", &s.vision)) changed = true;
    } else if (s.provider == ui::PROV_CURSOR || s.provider == ui::PROV_KIMISUB) {
        // Subscription-only providers: no API key field — the credential is an OAuth grant.
        if (s.provider == ui::PROV_KIMISUB) {
            dimText("Kimi Code (agent service). Sign in under Settings \xe2\x86\x92 Providers; "
                    "the service resolves the credential from its own auth store — no Moonshot "
                    "API key required for this path.");
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
            ImGui::TextUnformatted("Wire provider: kimi-code \xc2\xb7 model: k3 (default)");
            ImGui::PopStyleColor();
        } else {
            dimText("Cursor (agent service). Sign in under Settings \xe2\x86\x92 Providers; "
                    "the service resolves the credential from its own auth store — no API key.");
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
            ImGui::TextUnformatted("Wire provider: cursor \xc2\xb7 model: composer-2.5 (default)");
            ImGui::PopStyleColor();
        }
        if (ImGui::Checkbox("Model supports vision (send screenshots)", &s.vision)) changed = true;
    } else if (s.provider == ui::PROV_ZENMUX) {
        // Chudcode-service provider — the credential lives in the service's own auth store,
        // wired up via Settings → Providers sign-in (drawOmpProviderManager), not config.json.
        dimText("ZenMux (agent service). Sign in under Settings \xe2\x86\x92 Providers; no API "
                "key field here — the service resolves the credential from its own auth store.");
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
        ImGui::TextUnformatted("Wire provider: zenmux \xc2\xb7 model: x-ai/grok-4.5-free (default)");
        ImGui::PopStyleColor();
        if (ImGui::Checkbox("Model supports vision (send screenshots)", &s.vision)) changed = true;
    } else if (cloud) {
        drawValidatedCredentialHint(
            st, "API keys are managed under Settings \xe2\x86\x92 Providers and persisted only after provider validation.");
    } else {
        ImGui::TextUnformatted("Base URL");
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##baseurl", s.baseUrl, sizeof(s.baseUrl))) {
            changed = true;
            refreshModelAvailability(st, true);  // re-probe the new endpoint
        }
        drawValidatedCredentialHint(
            st, "If this local endpoint requires authentication, add its supported credential under Settings \xe2\x86\x92 Providers. This AI tab never writes keys.");
        if (ImGui::Checkbox("Model supports vision (send screenshots)", &s.vision)) changed = true;
        // Local availability status.
        const ProviderModels& pm = st.providerModels[s.provider == ui::PROV_LLAMACPP
                                                          ? ui::PROV_LLAMACPP : ui::PROV_LOCAL];
        const char* msg; ImU32 col;
        if (!pm.checked) { msg = "checking endpoint…"; col = ui::Palette::toU32(ui::palette.textDim); }
        else if (pm.ok) { msg = "endpoint reachable"; col = ui::Palette::toU32(ui::palette.statusOk); }
        else { msg = "endpoint unreachable — start your local model server"; col = ui::Palette::toU32(ui::palette.statusErr); }
        ImGui::PushStyleColor(ImGuiCol_Text, col);
        ImGui::TextUnformatted(msg);
        ImGui::PopStyleColor();
    }

    };

    auto drawGeneration = [&]() {
    // kit fields own the label column, the stacked layout on narrow panes and the "Auto"
    // placeholder: a raw InputInt read "0" for the automatic setting, which looked like a
    // zero-token cap / zero-token window.
    {
        ui::kit::FieldGroup group("ai_generation", "Model context window");

        const char* effLabels[8] = {};
        const char* effValues[8] = {};
        ui::ModelInfo probe;
        const ui::ModelInfo* probeP = nullptr;
        if (const ui::BridgeModelMeta* bm = ui::bridgeModelMetaForCad(s.provider, s.model,
                                                                      s.baseUrl)) {
            probe = ui::modelInfoFromProbe(s.provider, s.model, bm->name, bm->contextWindow,
                                           bm->maxTokens, bm->vision, bm->tools,
                                           bm->reasoningEfforts, "catalog");
            probeP = &probe;
        }
        int nEff = ui::reasoningEffortOptions(s.provider, s.model, effLabels, effValues, 8, probeP);
        if (nEff > 0) {
            int effIdx = -1;
            for (int i = 0; i < nEff; ++i)
                if (std::strcmp(s.reasoningEffort, effValues[i]) == 0) effIdx = i;
            if (ui::kit::fieldEnum("effort", "Reasoning effort", &effIdx, effLabels, nEff,
                    ui::kit::FieldOpts()
                        .withEnumStyle(ui::kit::EnumStyle::Combo)
                        .withHint("Default")
                        .withTip("How much hidden reasoning the model spends before answering. "
                                 "Higher = better planning on hard parts, slower and more "
                                 "tokens.")) &&
                effIdx >= 0 && effIdx < nEff) {
                std::strncpy(s.reasoningEffort, effValues[effIdx], sizeof(s.reasoningEffort) - 1);
                changed = true;
            }
        } else {
            // Settings search has a "Reasoning effort" entry pointing here, and the chat toolbar
            // has the same dial: say why the control is absent instead of showing nothing and
            // letting the user conclude the search lied.
            ui::kit::fieldReadout("effort_na", "Reasoning effort", "not offered by this model");
        }

        // 0 = automatic is stored as "absent": the field shows the "Auto" placeholder, clearing it
        // stores 0 again, and a typed value keeps the old clamp range.
        bool hasMt = s.maxTokens > 0;
        int  mt    = hasMt ? s.maxTokens : 512;
        if (ui::kit::fieldInt("maxtokens", "Max response tokens", &mt,
                ui::kit::FieldOpts()
                    .withPresent(&hasMt)
                    .withHint("Auto")
                    .withRange(512, 200000)
                    .withTip("Output token cap per model call. Leave empty for automatic. Raise "
                             "this if the agent reports \"budget exhausted\" or \"no JSON ops\" "
                             "\xe2\x80\x94 a reasoning model spent its budget thinking before "
                             "emitting the build."))) {
            s.maxTokens = hasMt ? mt : 0;
            changed = true;
        }

        bool hasCw = s.contextWindow > 0;
        int  cw    = hasCw ? s.contextWindow : 2048;
        if (ui::kit::fieldInt("ctxwindow", "Model context window", &cw,
                ui::kit::FieldOpts()
                    .withPresent(&hasCw)
                    .withHint("Auto")
                    .withRange(2048, 2000000)
                    .withTip("The model's total token context window. Leave empty for automatic "
                             "(from the model catalog). Set your LOCAL server's real window so "
                             "the agent trims the prompt to fit instead of overrunning it and "
                             "breaking the run."))) {
            s.contextWindow = hasCw ? cw : 0;
            changed = true;
        }
    }

    };

    // Two sections, two calls: the Settings Providers tab wraps each in its own glass card and
    // owns the header, so this switch deliberately has no "draw everything" case.
    switch (section) {
    case AiSettingsSection::ProviderModel:
        drawProviderModel();
        break;
    case AiSettingsSection::Generation:
        drawGeneration();
        break;
    }
    return changed;
}

// Quick-settings helpers + overlay body (Copilot gear). Full providers/roles/prompts live in Settings.
int loadMaxSubagentDepth() {
    // agent.max_subagent_depth — nesting levels of subagent-spawning-subagent
    // (not concurrency). Root chat = depth 0. Default 2, GUI clamp 0-4.
    // Enforced by AgentCoord (delegation.py / mission_dispatch.py).
    int depth = 2;
    try {
        std::ifstream in(configPath());
        if (in) {
            nlohmann::json j;
            in >> j;
            if (j.is_object()) {
                auto agent = j.value("agent", nlohmann::json::object());
                if (agent.is_object() && agent.contains("max_subagent_depth") &&
                    agent["max_subagent_depth"].is_number_integer()) {
                    depth = agent["max_subagent_depth"].get<int>();
                }
            }
        }
    } catch (...) {}
    if (depth < 0) depth = 0;
    if (depth > 4) depth = 4;
    return depth;
}
void saveMaxSubagentDepth(int depth) {
    if (depth < 0) depth = 0;
    if (depth > 4) depth = 4;
    updateConfigJson([&](nlohmann::json& j) {
        if (!j.contains("agent") || !j["agent"].is_object())
            j["agent"] = nlohmann::json::object();
        j["agent"]["max_subagent_depth"] = depth;
    });
}

int loadSubagentToolCallBudget() {
    // agent.subagent_tool_call_budget is a soft convergence warning for delegated subagents.
    // Default 0 lets the harness derive today's 4-16 call range from each token grant; the
    // 0-64 GUI range preserves automatic behavior while leaving deliberate manual headroom.
    int budget = 0;
    try {
        std::ifstream in(configPath());
        if (in) {
            nlohmann::json j;
            in >> j;
            if (j.is_object()) {
                auto agent = j.value("agent", nlohmann::json::object());
                if (agent.is_object() && agent.contains("subagent_tool_call_budget") &&
                    agent["subagent_tool_call_budget"].is_number_integer()) {
                    budget = agent["subagent_tool_call_budget"].get<int>();
                }
            }
        }
    } catch (...) {}
    if (budget < 0) budget = 0;
    if (budget > 64) budget = 64;
    return budget;
}
void saveSubagentToolCallBudget(int budget) {
    if (budget < 0) budget = 0;
    if (budget > 64) budget = 64;
    updateConfigJson([&](nlohmann::json& j) {
        if (!j.contains("agent") || !j["agent"].is_object())
            j["agent"] = nlohmann::json::object();
        j["agent"]["subagent_tool_call_budget"] = budget;
    });
}

void drawAISettingsBody(AppState& st, bool busy) {
    Settings& s = st.settings;
    const ui::AdaptiveSurface& surf = chatSurf();
    bool changed = false;
    (void)busy;
    if (ImGui::IsKeyPressed(ImGuiKey_Escape, false) && !ImGui::IsAnyItemActive()) {
        st.showAISettingsOverlay = false;
        st.showSessionsPicker = false;
        return;
    }

    // NO nested BeginChild here. The pane host owns the ONE scroll region this view renders into
    // and paints its pill scrollbar + top fade for it; the old "##ai_ov_body" child swallowed all
    // of the body's overflow, so the host's bar never engaged and the view simply ended mid-air
    // with the deep links unreachable. The host also owns the horizontal gutter (cpMenuGutter
    // window padding), so everything below is full-width (-1) with no self-indent.
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(ui::metrics::U(1.0f), ui::metrics::U(0.85f)));
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(ui::metrics::U(1.5f), ui::metrics::U(0.75f)));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    // The menu views run at the docked-pane rhythm whatever the host pushed around us.
    ui::kit::pushDensity(ui::kit::Density::Cozy);

    cpMenuHeader(st.fonts, "Quick settings",
                 "Session knobs. Full providers, roles and prompts live in Settings.");

    // Two-column rows: caption left in ONE shared column (sized from the widest caption, capped
    // so the control keeps most of the row), control right. Small enumerations are kit segmented
    // controls; when the remainder cannot hold every segment unelided the control drops under its
    // caption at full width — decided per row from the measured need. Segments are equal width,
    // so a narrow column can still be too small for {"Off","Option","Encourage","Require"} even at
    // full width; then the row becomes a full-width combo under its caption (popup bounded by
    // kit::constrainNextComboPopup, so it never grows past its frame or flips left). An option
    // name is never elided.
    static const char* kRowLabels[] = {"Plan", "Task list", "Subagents", "Advisor",
                                       "Max nesting", "Project memory"};
    float labelCol = 0.0f;
    for (const char* l : kRowLabels) labelCol = std::max(labelCol, ImGui::CalcTextSize(l).x);
    auto segRow = [&](const char* id, const char* label, const char* tip,
                      const char* const* items, int n, int* sel) -> bool {
        const float x0    = ImGui::GetCursorScreenPos().x;
        const float avail = ImGui::GetContentRegionAvail().x;
        const float gap   = ui::kit::padX();
        const float col   = std::min(labelCol, avail * 0.45f);
        float widest = 0.0f;
        for (int i = 0; i < n; ++i) widest = std::max(widest, ImGui::CalcTextSize(items[i]).x);
        const float need = (widest + ui::kit::padX() * 2.0f) * (float)n;
        const bool  fits = avail - col - gap >= need;
        // Full width only has to clear the segment's own inset on both sides (kit::segmented
        // elides at segW - 2 * stroke(2)), plus a hairline of breathing room.
        const float needFull =
            (widest + ui::metrics::stroke(2.0f) * 2.0f + ui::metrics::px(4.0f)) * (float)n;
        const bool  segOk = fits || avail >= needFull;
        ImGui::AlignTextToFramePadding();
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted(label);
        ImGui::PopStyleColor();
        if (tip && ImGui::IsItemHovered()) ui::kit::tooltip(tip);
        float w = avail;
        if (fits) {
            ImGui::SameLine(0.0f, 0.0f);
            ImGui::SetCursorScreenPos(ImVec2(x0 + col + gap, ImGui::GetCursorScreenPos().y));
            w = avail - col - gap;
        }
        if (segOk) return ui::kit::segmented(id, items, n, sel, w);
        bool changed = false;
        const int cur = (*sel >= 0 && *sel < n) ? *sel : 0;
        ImGui::SetNextItemWidth(w);
        ui::kit::constrainNextComboPopup(ImGui::GetCursorScreenPos().x, w, n);
        if (ImGui::BeginCombo(id, items[cur])) {
            for (int i = 0; i < n; ++i)
                if (ImGui::Selectable(items[i], i == cur) && i != cur) {
                    *sel = i;
                    changed = true;
                }
            ImGui::EndCombo();
        }
        if (tip && ImGui::IsItemHovered()) ui::kit::tooltip(tip);
        return changed;
    };
    // Checkboxes flow two-up when the card is wide enough and wrap otherwise, so three booleans
    // cost one or two lines instead of three.
    auto checkFlow = [&](bool first, const char* id, const char* label, bool* v,
                         const char* tip) -> bool {
        if (!first) {
            const float labelW = ImGui::GetTextLineHeight() + ui::metrics::U(2.0f) +
                                 ImGui::CalcTextSize(label).x;
            const float lastR  = ImGui::GetItemRectMax().x;
            const float sp     = ui::kit::padX() * 2.0f;
            const float right  = ImGui::GetCursorScreenPos().x + ImGui::GetContentRegionAvail().x;
            if (lastR + sp + labelW <= right) ImGui::SameLine(0.0f, sp);
        }
        return ui::kit::checkbox(id, label, v, tip);
    };

    // ---- Autonomy ----
    if (ui::kit::cardBegin("q_card_autonomy", "Autonomy", "ai_panel")) {
        static const char* kPrefLabels[] = {"Off", "Option", "Encourage", "Require"};
        auto prefRow = [&](const char* label, const char* id, const char* tip, int* pref) {
            int v = (*pref < 0 || *pref > 3) ? 0 : *pref;
            if (segRow(id, label, tip, kPrefLabels, 4, &v)) {
                *pref = v;
                if (pref == &s.planPreference) s.planMode = (v != 0);
                changed = true;
            }
        };
        prefRow("Plan", "##q_plan_pref", "Plan preference", &s.planPreference);
        prefRow("Task list", "##q_tasklist_pref", "Task list preference",
                &s.taskListPreference);
        prefRow("Subagents", "##q_sub_pref", "Subagent preference", &s.subagentPreference);
        prefRow("Advisor", "##q_adv_pref", "Advisor preference", &s.advisorPreference);
        if (ui::kit::checkbox("q_proactive", "Proactive advisor", &s.proactiveAdvisor,
                              "Opt-in mid-run guidance on failures. Cheap notes by default; "
                              "rare short advisor call only on severe streaks."))
            changed = true;
    }
    ui::kit::cardEnd();
    ui::kit::gapRow();

    // ---- Session ----
    if (ui::kit::cardBegin("q_card_session", "Session", "activity")) {
        if (checkFlow(true, "q_vision", "Vision", &s.vision,
                      "Send viewport screenshots so the model can see the part."))
            changed = true;
        if (checkFlow(false, "q_offline", "Offline (local only)", &s.offlineMode,
                      "Refuse any cloud provider; only a local endpoint is used."))
            changed = true;
        checkFlow(false, "q_thoughts", "Show thoughts", &gShowThinking,
                  "Show the assistant's reasoning inline (this session only).");
    }
    ui::kit::cardEnd();
    ui::kit::gapRow();

    // ---- Limits + memory ----
    if (ui::kit::cardBegin("q_card_limits", "Limits & memory", "settings")) {
        static int s_maxDepth = -1;
        if (s_maxDepth < 0) s_maxDepth = loadMaxSubagentDepth();
        static const char* kDepthLabels[] = {"0", "1", "2", "3", "4"};
        int depth = std::max(0, std::min(4, s_maxDepth));
        if (segRow("##q_depth", "Max nesting",
                   "Max subagent nesting: how many levels deep a subagent may spawn its own "
                   "subagents (0 = no subagents).",
                   kDepthLabels, 5, &depth)) {
            s_maxDepth = depth;
            saveMaxSubagentDepth(depth);
        }

        static const char* kMemoryBackends[] = {"off", "sharpshooter", "mnemo"};
        static const char* kMemoryLabels[] = {"Off", "Decisions", "Full recall"};
        int cur = 1;
        for (int i = 0; i < 3; ++i)
            if (s.memoryBackend == kMemoryBackends[i]) cur = i;
        if (segRow("##q_memory", "Project memory", "Project memory, kept per folder.",
                   kMemoryLabels, 3, &cur)) {
            s.memoryBackend = kMemoryBackends[cur];
            s.memoryEnabled = cur != 0;
            changed = true;
        }
    }
    ui::kit::cardEnd();

    cpMenuSection("Full settings");
    {
        // One row of three equal buttons — navigation out of the pane, not another field group.
        const float gap = ui::metrics::cpMenuRowGap();
        const float chipW = std::max(ui::metrics::px(48.0f),
                                     (ImGui::GetContentRegionAvail().x - 2.0f * gap) / 3.0f);
        auto deepLink = [&](const char* label, int tab) {
            if (ui::kit::secondaryButton(label, true, nullptr, ImVec2(chipW, 0.0f))) {
                st.openSettings = true;
                st.openSettingsTab = tab;
                st.showAISettingsOverlay = false;
            }
        };
        deepLink("AI##q_open_ai", 2);
        ImGui::SameLine(0.0f, gap);
        deepLink("Providers##q_open_prov", 3);
        ImGui::SameLine(0.0f, gap);
        deepLink("Prompts##q_open_prompts", 2);   // AI tab (Prompts absorbed)
    }
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuLabelGap()));
    cpMenuHint("Opens the full Settings window.");

    if (changed) saveSettings(s);

    ui::kit::popDensity();
    ImGui::PopStyleColor();
    ImGui::PopStyleVar(2);
}

// ============================ searchable model dropdown ============================
// A short provider tag shown as the dim sublabel next to each model (like the reference's
// "Copilot").
static const char* providerShort(int provider) {
    switch (provider) {
        case ui::PROV_ANTHROPIC: return "Claude";
        case ui::PROV_OPENAI:    return "OpenAI";
        case ui::PROV_LOCAL:     return "Local";
        case ui::PROV_LLAMACPP:  return "llama.cpp";
        case ui::PROV_GEMINI:    return "Gemini";
        case ui::PROV_XAI:       return "xAI";
        case ui::PROV_CLOUDCOMPAT: return "Cloud";
        case ui::PROV_CURSOR:    return "Cursor";
        case ui::PROV_KIMISUB:   return "kimi code";
        case ui::PROV_ZENMUX:    return "ZenMux";
    }
    return "";
}

// A discovered local model is selectable only when the endpoint can actually serve it: when LM
// Studio reports loaded state we require the model to be loaded; otherwise (servers without that
// endpoint, e.g. vLLM/llama.cpp, which only list what they serve) every listed id is available.
static bool localModelLoaded(const ProviderModels& pm, const std::string& id) {
    if (!pm.loadedKnown) return true;
    return std::find(pm.loaded.begin(), pm.loaded.end(), id) != pm.loaded.end();
}

// Has the user hidden this model from the chat picker (Settings → Providers model list)?
static bool modelHidden(AppState& st, const std::string& id) {
    return st.settings.hiddenModels.count(id) != 0;
}

// File-static cache of subscription_status "connected" flags so the model picker can show
// signed-in providers (xAI / Claude / Cursor / Codex / Kimi) without requiring an API key.
namespace {
struct SubConnectedCache {
    bool xai = false;
    bool claude = false;
    bool cursor = false;
    bool codex = false;
    bool kimi = false;
};
static SubConnectedCache s_subConn;

static void pollSubConnected(AppState& st) {
    // The browser build has no provider sign-in path; the shared engine's subscription state
    // belongs to the operator, so the web client never polls it.
    if (ui::webBuild()) return;
    ui::providerUsagePollStatus(st, 2.0, false);
    const auto& login = ui::providerUsageLogin();
    auto connected = [&login](const char* key) {
        const auto it = login.find(key);
        return it != login.end() && it->second.connected;
    };
    s_subConn.xai    = connected("xai")    || s_subConn.xai;
    s_subConn.claude = connected("claude") || s_subConn.claude;
    s_subConn.cursor = connected("cursor") || s_subConn.cursor;
    s_subConn.codex  = connected("codex")  || s_subConn.codex;
    s_subConn.kimi   = connected("kimi")   || s_subConn.kimi;
}
}  // namespace

// Does this provider have credentials / a reachable endpoint / a signed-in subscription,
// so its models can be selected? Public (copilot.h) so Settings role pickers share it.
bool providerUsable(AppState& st, int provider) {
    pollSubConnected(st);
    // A SUCCESSFUL live probe is proof of a working credential — and for a subscription/OAuth
    // user it is the ONLY proof, because their token lives in the Chudcode auth store rather than
    // in a local key buffer or subscription_auth. agent_list_models reports ok=true in exactly
    // that case (see the anthropic branch in libchudware.cpp); keying availability off the local
    // key alone is what hid Anthropic from those users. Limited to providers whose list endpoint
    // REQUIRES auth — OpenRouter/DashScope serve /models publicly, so a cloud-compat probe would
    // prove nothing, and local/llama.cpp already resolve through providerModels below.
    switch (provider) {
        case ui::PROV_ANTHROPIC:
        case ui::PROV_OPENAI:
        case ui::PROV_GEMINI:
        case ui::PROV_XAI:
            if (st.providerModels[provider].ok) return true;
            break;
        default: break;
    }
    switch (provider) {
        case ui::PROV_GEMINI:
            return st.settings.geminiKey[0] != 0;
        case ui::PROV_ANTHROPIC:
            return st.settings.anthropicKey[0] != 0 || s_subConn.claude;
        case ui::PROV_OPENAI:
            return st.settings.openaiKey[0] != 0 || s_subConn.codex;
        case ui::PROV_CLOUDCOMPAT:
            return st.settings.cloudKey[0] != 0 || st.settings.moonshotKey[0] != 0;
        case ui::PROV_XAI:
            return st.settings.xaiKey[0] != 0 || s_subConn.xai;
        case ui::PROV_LOCAL:       return st.providerModels[ui::PROV_LOCAL].ok;
        case ui::PROV_LLAMACPP:    return st.providerModels[ui::PROV_LLAMACPP].ok;
        case ui::PROV_CURSOR:
            return s_subConn.cursor || st.signedInBridgeProviders.count("cursor") != 0;
        case ui::PROV_KIMISUB:     return s_subConn.kimi;
        // ZenMux — Chudcode-service credential lives in the service's auth store (Settings → Providers),
        // not a locally-polled subscription/key flag this file can cheaply check. Always offered;
        // an unsigned-in bridge surfaces a normal auth error from the backend on send, same class
        // as any other unconfigured provider.
        case ui::PROV_ZENMUX:      return true;
    }
    return false;
}

// Both gate picker rows on "is there a credential for this cloud slot at all".
bool cloudCompatKeyPresent(AppState& st) {
    pollSubConnected(st);
    return st.settings.cloudKey[0] != 0;
}

bool moonshotKeyPresent(AppState& st) {
    pollSubConnected(st);
    return st.settings.moonshotKey[0] != 0;
}

// ============================ Chudcode model catalog + session cost (S-UI-PROV) ===
// Chudcode's catalog (capabilities + $/Mtok cost) via `chudcode_models_list`,
// and the per-session cost strip via `chudcode_session_stats`. The worker
// re-discovers models in the background; poll briefly until the fresh service is ready.
// Both degrade permanently on unknown_method and keep the last good data on soft failures.

namespace {

std::vector<ui::BridgeModelMeta> s_bridgeModels;
bool s_bridgeCatalogAvailable = true;  // false after unknown_method — stop polling, no sublabels
bool s_bridgeCatalogInFlight = false;
bool s_bridgeCatalogRefreshing = false;
double s_bridgeCatalogLastFetch = -1e9;

// True when the RPC is permanently absent (unknown method / older core) — mirrors
// usageIsAbsent in composer.cpp.
bool bridgeRpcAbsent(const core::RpcResult& r) {
    if (!r.ok) {
        const std::string& e = r.err;
        if (e.find("unknown") != std::string::npos || e.find("not found") != std::string::npos ||
            e.find("not_found") != std::string::npos || e.find("method") != std::string::npos)
            return true;
        return false;
    }
    if (!r.result.is_object()) return false;
    if (r.result.contains("code")) {
        const std::string c = jsonOptStr(r.result, "code");
        if (c == "unknown_method" || c == "method_not_found" || c == "not_implemented")
            return true;
    }
    if (!r.result.value("ok", true)) {
        const std::string reason = r.result.value("reason", std::string());
        if (reason == "unknown_method" || reason == "method_not_found" ||
            reason == "not_implemented")
            return true;
    }
    return false;
}

// "$3" / "$1.5" / "$18.75", trimmed to meaningful digits.
std::string fmtUsd(double v) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "$%.4g", v);
    return buf;
}

// Lowercased copy for case-insensitive substring matching below.
std::string toLowerCopy(const std::string& s) {
    std::string out(s.size(), '\0');
    std::transform(s.begin(), s.end(), out.begin(),
                   [](unsigned char c) { return (char)std::tolower(c); });
    return out;
}

// Infer a release date embedded in a model id/name, e.g.:
//   "claude-3-5-sonnet-20241022" -> 2024-10-22 (bare YYYYMMDD token)
//   "gpt-4o-2024-08-06"          -> 2024-08-06 (YYYY-MM-DD token)
// Deliberately conservative: accept only an unambiguous YYYYMMDD run or hyphenated YYYY-MM-DD.
// Skip rather than guess when nothing self-contained is found.
void inferReleaseDate(const std::string& id, const std::string& name, std::string& outIso,
                      long& outSort) {
    outIso.clear();
    outSort = 0;
    auto isDigit = [](char c) { return c >= '0' && c <= '9'; };
    auto tryParse = [&](const std::string& s) -> bool {
        // YYYY-MM-DD (10 chars, hyphens at 4/7)
        for (size_t i = 0; i + 10 <= s.size(); ++i) {
            if (isDigit(s[i]) && isDigit(s[i + 1]) && isDigit(s[i + 2]) && isDigit(s[i + 3]) &&
                s[i + 4] == '-' && isDigit(s[i + 5]) && isDigit(s[i + 6]) && s[i + 7] == '-' &&
                isDigit(s[i + 8]) && isDigit(s[i + 9])) {
                // Reject if immediately flanked by another digit (avoid mis-slicing a longer run).
                if (i > 0 && isDigit(s[i - 1])) continue;
                if (i + 10 < s.size() && isDigit(s[i + 10])) continue;
                int y = (s[i] - '0') * 1000 + (s[i + 1] - '0') * 100 + (s[i + 2] - '0') * 10 +
                        (s[i + 3] - '0');
                int mo = (s[i + 5] - '0') * 10 + (s[i + 6] - '0');
                int d = (s[i + 8] - '0') * 10 + (s[i + 9] - '0');
                if (y < 2015 || y > 2035 || mo < 1 || mo > 12 || d < 1 || d > 31) continue;
                char buf[16];
                std::snprintf(buf, sizeof(buf), "%04d-%02d-%02d", y, mo, d);
                outIso = buf;
                outSort = (long)y * 10000 + mo * 100 + d;
                return true;
            }
        }
        // Bare YYYYMMDD (8-digit run, not part of a longer digit run).
        for (size_t i = 0; i + 8 <= s.size(); ++i) {
            bool allDigit = true;
            for (int k = 0; k < 8; ++k) if (!isDigit(s[i + k])) { allDigit = false; break; }
            if (!allDigit) continue;
            if (i > 0 && isDigit(s[i - 1])) continue;
            if (i + 8 < s.size() && isDigit(s[i + 8])) continue;
            int y = std::stoi(s.substr(i, 4));
            int mo = std::stoi(s.substr(i + 4, 2));
            int d = std::stoi(s.substr(i + 6, 2));
            if (y < 2015 || y > 2035 || mo < 1 || mo > 12 || d < 1 || d > 31) continue;
            char buf[16];
            std::snprintf(buf, sizeof(buf), "%04d-%02d-%02d", y, mo, d);
            outIso = buf;
            outSort = (long)y * 10000 + mo * 100 + d;
            return true;
        }
        return false;
    };
    if (tryParse(id)) return;
    tryParse(name);
}

// Conservative, substring-based heuristic flagging clearly-superseded/legacy model ids
// (case-insensitive). Deliberately narrow — false negatives (an old model left unflagged) are
// fine, false positives (a current flagship hidden) are not. Update this list as families age
// out; never flag a current flagship by broad prefix match.
bool isDeprecatedModelId(const std::string& id) {
    static const char* kLegacySubstrings[] = {
        "gpt-3.5", "gpt-4-0314", "gpt-4-0613", "gpt-4-32k", "text-davinci", "davinci",
        "babbage", "ada-", "claude-1", "claude-2", "claude-instant", "claude-3-opus",
        "gemini-1.0", "gemini-pro-vision", "palm", "deprecated",
    };
    const std::string lo = toLowerCopy(id);
    for (const char* sub : kLegacySubstrings)
        if (lo.find(sub) != std::string::npos) return true;
    // claude-3-sonnet / claude-3-haiku are flagged, but claude-3-5-sonnet / claude-3-5-haiku
    // (current generation) are NOT: the "-5-" between "claude-3" and "sonnet"/"haiku" means
    // "claude-3-sonnet"/"claude-3-haiku" is never a substring of those newer ids, so a plain
    // substring check is already exact here.
    if (lo.find("claude-3-sonnet") != std::string::npos) return true;
    if (lo.find("claude-3-haiku") != std::string::npos) return true;
    return false;
}

// One catalog payload (`chudcode_models_list` "models") -> sorted rows. Shared by the live fetch
// and the on-disk copy below so both produce byte-identical metadata.
std::vector<ui::BridgeModelMeta> parseBridgeCatalogRows(const nlohmann::json& models) {
    std::vector<ui::BridgeModelMeta> rows;
    if (!models.is_array()) return rows;
    for (const auto& m : models) {
        if (!m.is_object()) continue;
        ui::BridgeModelMeta b;
        b.id = m.value("id", std::string());
        if (b.id.empty()) continue;
        b.name = m.value("name", b.id);
        b.provider = m.value("provider", std::string());
        b.vision = m.value("vision", false);
        b.tools = m.value("supportsTools", false);
        b.reasoning = m.value("reasoning", false);
        const auto ef = m.find("reasoningEfforts");
        if (ef != m.end() && ef->is_array())
            for (const auto& e : *ef)
                if (e.is_string()) b.reasoningEfforts.push_back(e.get<std::string>());
        if (m.contains("contextWindow") && m["contextWindow"].is_number())
            b.contextWindow = m["contextWindow"].get<int>();
        if (m.contains("maxTokens") && m["maxTokens"].is_number())
            b.maxTokens = m["maxTokens"].get<int>();
        const auto c = m.find("cost");
        if (c != m.end() && c->is_object()) {
            b.hasCost = true;
            b.costIn = c->value("input", 0.0);
            b.costOut = c->value("output", 0.0);
            b.costCacheRead = c->value("cacheRead", 0.0);
            b.costCacheWrite = c->value("cacheWrite", 0.0);
        }
        // Pricing provenance: the bridge marks a row `priced` only when the catalog carries
        // a real non-zero price. `hasCost` cannot answer that — `cost` is an object even
        // when every field is zero, which is why an unpriced model used to render as "free".
        // A bridge predating the field gets the bridge's own rule applied locally instead of
        // every model silently losing its price.
        if (m.contains("priced") && m["priced"].is_boolean())
            b.priced = m["priced"].get<bool>();
        else
            b.priced = b.costIn > 0.0 || b.costOut > 0.0 ||
                       b.costCacheRead > 0.0 || b.costCacheWrite > 0.0;
        inferReleaseDate(b.id, b.name, b.releaseDate, b.releaseSort);
        b.deprecated = isDeprecatedModelId(b.id);
        rows.push_back(std::move(b));
    }
    // Newest-first per provider: provider ASC, deprecated last, then newest release first,
    // then name ASC as a stable tiebreak for same-day/unknown-date rows.
    std::stable_sort(rows.begin(), rows.end(),
                     [](const ui::BridgeModelMeta& a, const ui::BridgeModelMeta& b) {
        if (a.provider != b.provider) return a.provider < b.provider;
        if (a.deprecated != b.deprecated) return !a.deprecated;  // non-deprecated first
        if (a.releaseSort != b.releaseSort) return a.releaseSort > b.releaseSort;
        return a.name < b.name;
    });
    return rows;
}

// ---- persisted catalog --------------------------------------------------------------------
// The live catalog only arrives after worker boot + startup phases, so a restart used to spend
// its first seconds on raw model ids ("gpt-6.1-sol" flipping to "GPT-6.1-Sol") and an id-pattern
// context window (128k flipping to 272k — red over-limit on a restored long session). The last
// successful payload is kept beside config.json and seeded before the first lookup; the live
// fetch replaces it wholesale exactly as before. A missing/corrupt file just means "cold".
std::string bridgeCatalogCachePath() { return appdataAppDir() + "/model_catalog_cache.json"; }

void saveBridgeCatalogCache(const nlohmann::json& models) {
    if (!models.is_array() || models.empty()) return;
    const std::string path = bridgeCatalogCachePath();
    const std::string tmp  = path + ".tmp";
    {
        std::ofstream f(tmp, std::ios::binary | std::ios::trunc);
        if (!f) return;
        f << nlohmann::json{{"models", models}}.dump();
        if (!f) return;
    }
    std::error_code ec;
    std::filesystem::rename(tmp, path, ec);
    if (ec) std::filesystem::remove(tmp, ec);
}

void ensureBridgeCatalogSeeded() {
    static bool s_seeded = false;
    if (s_seeded) return;
    s_seeded = true;
    if (!s_bridgeModels.empty()) return;
    std::ifstream f(bridgeCatalogCachePath(), std::ios::binary);
    if (!f) return;
    const nlohmann::json j = nlohmann::json::parse(f, nullptr, /*allow_exceptions=*/false);
    if (!j.is_object()) return;
    const auto it = j.find("models");
    if (it != j.end()) s_bridgeModels = parseBridgeCatalogRows(*it);
}

}  // namespace

void refreshBridgeModelCatalog(AppState& st, bool force) {
    ensureBridgeCatalogSeeded();   // before any clear below, so a stale seed can never resurface
    if (!s_bridgeCatalogAvailable || s_bridgeCatalogInFlight) return;
    const double now = ImGui::GetTime();
    const bool followupReady = startupCatalogFollowupReady(st);
    if (!force && (!st.workerStarted || st.workerBootPending || !st.startupPhase.empty()))
        return;
    if (!force && s_bridgeCatalogLastFetch > -1e9 && !followupReady) return;
    // During Chudcode's background discovery, follow up promptly rather than waiting
    // five minutes after showing the old list. Steady-state polling stays at five minutes.
    if (!force && (now - s_bridgeCatalogLastFetch) < (s_bridgeCatalogRefreshing ? 5.0 : 300.0))
        return;
    s_bridgeCatalogLastFetch = now;
    s_bridgeCatalogInFlight = true;
    st.rpc.call("chudcode_models_list", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_bridgeCatalogInFlight = false;
        s_bridgeCatalogLastFetch = ImGui::GetTime();
        if (r.ok && r.result.is_object() && r.result.value("ok", true))
            s_bridgeCatalogRefreshing = r.result.value("refreshing", false);
        if (bridgeRpcAbsent(r)) {
            s_bridgeCatalogAvailable = false;
            s_bridgeCatalogRefreshing = false;
            s_bridgeModels.clear();
            return;
        }
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", true)) return;  // soft fail
        const auto it = r.result.find("models");
        if (it == r.result.end() || !it->is_array()) return;
        s_bridgeModels = parseBridgeCatalogRows(*it);
        if (!s_bridgeModels.empty()) saveBridgeCatalogCache(*it);
    }, 15000);
}

const std::vector<ui::BridgeModelMeta>& bridgeModelCatalog() {
    ensureBridgeCatalogSeeded();
    return s_bridgeModels;
}

const ui::BridgeModelMeta* bridgeModelMeta(const std::string& providerId,
                                               const std::string& modelId) {
    ensureBridgeCatalogSeeded();
    for (const auto& m : s_bridgeModels) {
        if (m.id != modelId) continue;
        if (providerId.empty() || m.provider == providerId) return &m;
    }
    return nullptr;
}

const ui::BridgeModelMeta* bridgeModelMetaForCad(int cadProvider, const std::string& modelId,
                                                     const char* cloudBaseUrl) {
    // CAD ProviderIndex -> Chudcode catalog provider id (the service-routed subset; local endpoints
    // have no catalog rows and simply get no sublabel).
    std::string prov;
    switch (cadProvider) {
        case ui::PROV_ANTHROPIC: prov = "anthropic"; break;
        case ui::PROV_KIMISUB:   prov = "kimi-code"; break;
        case ui::PROV_OPENAI:    prov = "openai"; break;
        case ui::PROV_XAI:       prov = "xai"; break;
        case ui::PROV_GEMINI:    prov = "google"; break;
        case ui::PROV_ZENMUX:    prov = "zenmux"; break;
        case ui::PROV_CURSOR:    prov = "cursor"; break;
        case ui::PROV_CLOUDCOMPAT: {
            // The shared cloud slot resolves by base_url (Moonshot is the prioritized one).
            const char* svc = ui::cloudServiceKey(cloudBaseUrl ? cloudBaseUrl : "");
            if (std::strcmp(svc, "cloud") != 0) prov = svc;
            break;
        }
        default: break;
    }
    if (!prov.empty())
        if (const ui::BridgeModelMeta* m = bridgeModelMeta(prov, modelId)) return m;
    // Fallback: bare id match covers cloud-slot services whose provider id differs from ours.
    return bridgeModelMeta("", modelId);
}

bool bridgeProviderIsLocalFree(const std::string& providerId) {
    // Mirrors the bridge's KEYLESS_PROVIDERS (auth-context.ts): runtimes serving from your own
    // hardware, where an absent catalog price means $0 and not "unknown". Exact match on
    // purpose — "ollama-cloud" is a paid hosted subscription whose rows carry no price either,
    // and calling that "free" is exactly the fabrication this set exists to avoid.
    return providerId == "ollama" || providerId == "llama-cpp" || providerId == "lm-studio";
}

std::string bridgeModelSublabel(const ui::BridgeModelMeta& m) {
    // "ctx 200k \xc2\xb7 $3/$15 \xc2\xb7 tools vision" — Chudcode model-browser row format.
    std::string out;
    if (m.contextWindow > 0) {
        char buf[24];
        if (m.contextWindow >= 1000000)
            std::snprintf(buf, sizeof(buf), "ctx %dM", m.contextWindow / 1000000);
        else
            std::snprintf(buf, sizeof(buf), "ctx %dk", m.contextWindow / 1000);
        out = buf;
    }
    // Price, or the honest absence of one. `hasCost` is true for every catalog row, so it can
    // only gate the FORMAT — `priced` decides whether there is a number to show at all.
    if (!out.empty()) out += " \xc2\xb7 ";
    if (m.priced)
        out += fmtUsd(m.costIn) + "/" + fmtUsd(m.costOut);
    else if (bridgeProviderIsLocalFree(m.provider))
        out += "free";
    else
        out += "no price data";
    std::string caps;
    if (m.tools) caps = "tools";
    if (m.vision) caps += caps.empty() ? "vision" : " vision";
    if (m.reasoning) caps += caps.empty() ? "reasoning" : " reasoning";
    if (!caps.empty()) {
        if (!out.empty()) out += " \xc2\xb7 ";
        out += caps;
    }
    return out;
}

std::string bridgeModelCostDetail(const ui::BridgeModelMeta& m) {
    // Tooltip line: full precision + cache pricing (the catalog keeps cache cost off the row).
    std::string out;
    if (m.priced) {
        out = "in " + fmtUsd(m.costIn) + "/M \xc2\xb7 out " + fmtUsd(m.costOut) + "/M";
        if (m.costCacheRead > 0.0 || m.costCacheWrite > 0.0)
            out += " \xc2\xb7 cache " + fmtUsd(m.costCacheRead) + "/" + fmtUsd(m.costCacheWrite);
    } else if (bridgeProviderIsLocalFree(m.provider)) {
        out = "runs locally \xe2\x80\x94 no per-token charge";
    } else {
        out = "no price in the model catalog \xe2\x80\x94 spend can't be computed for this model";
    }
    if (m.maxTokens > 0) {
        char buf[24];
        std::snprintf(buf, sizeof(buf), "%smax out %dk", out.empty() ? "" : " \xc2\xb7 ",
                      m.maxTokens / 1000);
        out += buf;
    }
    return out;
}

// ---- per-session cost strip ----
namespace {

struct CostModelRow {
    std::string provider;
    std::string model;
    double usd = 0;
    long long tokensIn = 0, tokensOut = 0;
    bool priced = false;   // pricing provenance: false = no catalog price, NOT free
};
double s_costUsd = 0;
bool s_costHasUsd = false;         // the service reported a dollar figure at all
long long s_costTin = 0, s_costTout = 0;
std::vector<CostModelRow> s_costRows;
// Provenance of the aggregate over the models that actually MOVED tokens: s_costUsd is a
// FLOOR whenever s_costUnpriced is non-empty — those models' tokens are counted but add $0.
int s_costPricedModels = 0;
int s_costTotalModels = 0;
std::vector<std::string> s_costUnpriced;   // "<provider>/<model>", displayed verbatim
bool s_costAvailable = true;   // false after unknown_method
bool s_costInFlight = false;
bool s_costHasData = false;
double s_costLastFetch = -1e9;
// Session + completed-turn fingerprint last priced — a conversation switch or a finished
// turn refires immediately; otherwise a 30s TTL refreshes while the strip is visible.
std::string s_costSession;
long long s_costFingerprint = -1;

}  // namespace

void drawSessionCostStrip(AppState& st) {
    if (!s_costAvailable) return;
    const float sc = ui::uiScale;

    // The Chudcode host owns the session's accounting: chudcode_session_stats prices the
    // session it actually ran, so the UI no longer rebuilds token events and re-sends them
    // to a bridge calculator. Refire on a conversation switch or when a turn completes
    // (turn-count + local token fingerprint), plus a 30s TTL while the strip is visible.
#ifdef __EMSCRIPTEN__
    const std::string sess = cellAgentSessionHandle(st.activeConvId());
#else
    const std::string sess = st.activeConvId();
#endif
    if (sess.empty()) return;
    long long fp = 0;
    for (const auto& m : st.chat) {
        if (m.fromUser || m.running) continue;
        long long tin = 0, tout = 0;
        for (const auto& a : m.activity) {
            if (a.rowKind != 0) continue;
            tin += a.tokensIn;
            tout += a.tokensOut;
        }
        fp += 1 + tin + tout;
    }
    const double now = ImGui::GetTime();
    if (!s_costInFlight && (sess != s_costSession || fp != s_costFingerprint ||
                            (now - s_costLastFetch) > 30.0)) {
        if (sess != s_costSession) s_costHasData = false;   // never show another session's bill
        s_costSession = sess;
        s_costFingerprint = fp;
        s_costLastFetch = now;
        s_costInFlight = true;
        st.rpc.call("chudcode_session_stats", nlohmann::json{{"session_id", sess}},
                    [sess](const core::RpcResult& r) {
            s_costInFlight = false;
            if (bridgeRpcAbsent(r)) {
                s_costAvailable = false;
                s_costHasData = false;
                return;
            }
            if (s_costSession != sess) return;   // conversation switched mid-flight
            ui::ChudcodeSessionStats stats;
            if (!r.ok || !ui::parseChudcodeSessionStats(r.result, stats))
                return;   // soft fail — keep the last good data
            s_costUsd = stats.usd;
            s_costHasUsd = stats.hasCost;
            s_costTin = stats.tokensIn;
            s_costTout = stats.tokensOut;
            s_costRows.clear();
            s_costUnpriced.clear();
            s_costPricedModels = 0;
            s_costTotalModels = 0;
            for (const auto& m : stats.models) {
                CostModelRow cr;
                cr.provider = m.provider;
                cr.model = m.model;
                cr.usd = m.usd;
                cr.tokensIn = m.tokensIn;
                cr.tokensOut = m.tokensOut;
                cr.priced = m.priced;
                if (!cr.priced)
                    if (const ui::BridgeModelMeta* meta = bridgeModelMeta(cr.provider, cr.model))
                        cr.priced = meta->priced;   // stats predating the field
                s_costRows.push_back(std::move(cr));
            }
            // Provenance over the models that actually moved tokens — exactly the population
            // the strip talks about. Reconstructed locally so an older service that omits the
            // counters degrades to the same honest answer instead of implying completeness.
            for (const auto& row : s_costRows) {
                if (row.tokensIn <= 0 && row.tokensOut <= 0) continue;
                ++s_costTotalModels;
                if (row.priced) ++s_costPricedModels;
                else s_costUnpriced.push_back(row.provider.empty()
                                                  ? row.model
                                                  : row.provider + "/" + row.model);
            }
            // A session with no dollar figure and no tokens yet has nothing honest to show.
            s_costHasData = stats.hasCost || s_costTin > 0 || s_costTout > 0;
        }, 15000);
    }
    if (!s_costHasData) return;

    // Compact one-liner: "Session $1.87 \xc2\xb7 142k tok" when every model that spent tokens
    // has a catalog price, and "Session >= $1.87 \xc2\xb7 142k tok \xc2\xb7 3 of 4 models priced"
    // when some don't — their tokens are in the token total but add $0 to the dollars, so the
    // figure is a floor. ">=" is spelled in ASCII on purpose: U+2265 is not in the font atlas
    // (ui/glyph_ranges.h kBody) and would paint as notdef.
    const bool incomplete = !s_costUnpriced.empty();
    char line[192];
    const long long tokTotal = (long long)s_costTin + (long long)s_costTout;
    char tokBuf[24];
    if (tokTotal >= 1000000)
        std::snprintf(tokBuf, sizeof(tokBuf), "%.1fM tok", tokTotal / 1.0e6);
    else
        std::snprintf(tokBuf, sizeof(tokBuf), "%lldk tok", tokTotal / 1000);
    if (!s_costHasUsd)
        std::snprintf(line, sizeof(line), "Session %s \xc2\xb7 no cost reported", tokBuf);
    else if (incomplete)
        std::snprintf(line, sizeof(line),
                      "Session >= %s \xc2\xb7 %s \xc2\xb7 %d of %d models priced",
                      fmtUsd(s_costUsd).c_str(), tokBuf, s_costPricedModels, s_costTotalModels);
    else
        std::snprintf(line, sizeof(line), "Session %s \xc2\xb7 %s",
                      fmtUsd(s_costUsd).c_str(), tokBuf);
    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(incomplete ? ui::palette.statusWarn
                                                                      : ui::palette.textDim));
    ImGui::TextUnformatted(line);
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) {
        std::string tip;
        if (incomplete) {
            tip = "This is a FLOOR, not a bill: only " + std::to_string(s_costPricedModels) +
                  " of " + std::to_string(s_costTotalModels) +
                  " models that spent tokens have a price in the model catalog.\n"
                  "No price data for: ";
            const size_t kShow = 6;
            for (size_t i = 0; i < s_costUnpriced.size() && i < kShow; ++i) {
                if (i) tip += ", ";
                tip += s_costUnpriced[i];
            }
            if (s_costUnpriced.size() > kShow)
                tip += ", +" + std::to_string(s_costUnpriced.size() - kShow) + " more";
            tip += ".\nTheir tokens are counted in the token total but add $0 to the dollar "
                   "figure, so real spend is higher by an unknown amount.\n"
                   "The priced part is agent session accounting.\n"
                   "Click for the per-model breakdown.";
        } else if (!s_costHasUsd) {
            tip = "The agent service reported this session's token movement but no dollar figure.\n"
                  "Click for the per-model breakdown.";
        } else {
            tip = "Agent session accounting \xe2\x80\x94 what this session's turns "
                  "actually cost, not a bill.\nClick for the per-model breakdown.";
        }
        ImGui::SetTooltip("%s", tip.c_str());
    }
    if (ImGui::IsItemClicked()) ImGui::OpenPopup("##session_cost_detail");
    if (ImGui::BeginPopup("##session_cost_detail")) {
        ImGui::TextUnformatted("Session cost by model");
        ImGui::Separator();
        if (s_costRows.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
            ImGui::TextUnformatted("No turns with token counts yet this session.");
            ImGui::PopStyleColor();
        }
        for (const auto& row : s_costRows) {
            ImGui::PushID(row.model.c_str());
            ImGui::TextUnformatted(row.model.c_str());
            ImGui::SameLine(220.0f * sc);
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
            ImGui::Text("%lld in / %lld out", row.tokensIn, row.tokensOut);
            ImGui::PopStyleColor();
            ImGui::SameLine(380.0f * sc);
            // An unpriced row's usd is 0 because nothing priced it, not because it was free:
            // print the reason where the money would go instead of a fabricated $0.0000.
            if (row.priced) {
                ImGui::TextUnformatted(fmtUsd(row.usd).c_str());
            } else {
                const bool localFree = bridgeProviderIsLocalFree(row.provider);
                ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(
                    localFree ? ui::palette.textDim : ui::palette.statusWarn));
                ImGui::TextUnformatted(localFree ? "free (local)" : "no price data");
                ImGui::PopStyleColor();
            }
            ImGui::PopID();
        }
        if (incomplete) {
            ImGui::Separator();
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
            ImGui::TextUnformatted("Rows marked \"no price data\" contribute tokens but no "
                                   "dollars \xe2\x80\x94 the session total is a floor.");
            ImGui::PopStyleColor();
        }
        ImGui::EndPopup();
    }
}

// One model row: checkmark gutter (drawn — no check glyph in the font) + label + dim provider
// sublabel; an accent "Add key" when the provider isn't usable. Returns true when picked.
// `uid` makes the ImGui id unique per row — model ids can repeat across providers/endpoints
// (the same local endpoint backs both PROV_LOCAL and PROV_LLAMACPP), and two rows sharing an
// id string would trip ImGui's "conflicting ID" assert.
static bool modelRow(AppState& st, int provider, const std::string& id, const std::string& label,
                     bool usable, int uid) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const float rowH = ImGui::GetFrameHeight();
    const float fh = ImGui::GetFontSize();
    const float gut = fh + 8 * sc;
    bool current = (st.settings.provider == provider &&
                    std::strcmp(st.settings.model, id.c_str()) == 0);
    ImGui::PushID(uid);
    bool clicked = ImGui::Selectable("##mrow", current,
                                     usable ? 0 : ImGuiSelectableFlags_Disabled, ImVec2(0, rowH));
    // The caller applies the pick AFTER this returns, so the glyphs below would otherwise
    // paint from pre-click state: the row you just clicked drew unchecked for a frame.
    if (clicked && usable) current = true;
    // Chudcode catalog sublabel (ctx / cost / capabilities) for service-routed providers.
    const ui::BridgeModelMeta* meta = bridgeModelMetaForCad(provider, id, st.settings.baseUrl);
    const std::string sublabel = meta ? bridgeModelSublabel(*meta) : std::string();
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayShort)) {
        // Full model name + provider for the composer overlay picker; cost detail (cache
        // pricing, max output) rides the tooltip per the Chudcode model-browser convention.
        if (meta) {
            const std::string detail = bridgeModelCostDetail(*meta);
            if (!detail.empty())
                ImGui::SetTooltip("%s \xe2\x80\x94 %s\n%s", label.c_str(),
                                  providerShort(provider), detail.c_str());
            else
                ImGui::SetTooltip("%s \xe2\x80\x94 %s", label.c_str(), providerShort(provider));
        } else {
            ImGui::SetTooltip("%s \xe2\x80\x94 %s", label.c_str(), providerShort(provider));
        }
    }
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 mn = ImGui::GetItemRectMin(), mx = ImGui::GetItemRectMax();
    float midY = (mn.y + mx.y) * 0.5f;
    if (current) {
        float r = fh * 0.30f; ImVec2 c(mn.x + 5 * sc + r, midY);
        ImU32 col = ui::Palette::toU32(pal.accent); float th = ui::metrics::stroke(1.6f);
        dl->AddLine(ImVec2(c.x - r * 0.55f, c.y + r * 0.05f),
                    ImVec2(c.x - r * 0.10f, c.y + r * 0.50f), col, th);
        dl->AddLine(ImVec2(c.x - r * 0.10f, c.y + r * 0.50f),
                    ImVec2(c.x + r * 0.60f, c.y - r * 0.50f), col, th);
    }
    dl->AddText(ImVec2(mn.x + gut, midY - fh * 0.5f),
                usable ? ui::Palette::toU32(pal.text) : ui::Palette::toU32(pal.textDisabled),
                label.c_str());
    float lw = ImGui::CalcTextSize(label.c_str()).x;
    dl->AddText(ImVec2(mn.x + gut + lw + 8 * sc, midY - fh * 0.5f),
                ui::Palette::toU32(pal.textDim), providerShort(provider));
    if (!usable) {
        const char* hint = "Add key";
        float hw = ImGui::CalcTextSize(hint).x;
        dl->AddText(ImVec2(mx.x - hw - 8 * sc, midY - fh * 0.5f),
                    ui::Palette::toU32(pal.accent), hint);
    } else if (!sublabel.empty()) {
        // Right-aligned dim "ctx 200k \xc2\xb7 $3/$15 \xc2\xb7 tools vision" (Chudcode model row).
        const float sw = ImGui::CalcTextSize(sublabel.c_str()).x;
        if (mn.x + gut + lw + 8 * sc + 60.0f * sc < mx.x - sw - 8 * sc)
            dl->AddText(ImVec2(mx.x - sw - 8 * sc, midY - fh * 0.5f),
                        ui::Palette::toU32(pal.textDim), sublabel.c_str());
    }
    ImGui::PopID();
    return clicked && usable;
}

// Providers whose ONLY credential is dead (bridge state "error": no active key/OAuth grant but
// at least one revoked row on file). They are deliberately kept OUT of the usable set below —
// every request against them fails — so the picker names them instead, or the models would just
// vanish from the list with no reason given.
namespace {
struct DeadCredProvider {
    std::string label;
    std::string cause;
};
std::vector<DeadCredProvider> s_deadCredProviders;
}  // namespace

// Poll chudcode_provider_list so the composer knows which providers are signed in WITHOUT the
// user having opened Settings -> Providers. Fills st.signedInBridgeProviders (same set the
// Providers panel writes) so the dropdown mirrors the Providers tab. TTL'd; degrades silently.
static void refreshSignedInProviders(AppState& st, bool force) {
    // Browser credentials belong to the gateway account, never a shared SDK auth store.
    if (ui::webBuild()) return;
    static double s_lastPoll = -1e9;
    static bool s_inFlight = false;
    if (s_inFlight) return;
    const double now = ImGui::GetTime();
    if (!force && (now - s_lastPoll) < 5.0) return;
    s_lastPoll = now;
    s_inFlight = true;
    st.rpc.call("chudcode_provider_list", nlohmann::json::object(),
                [&st](const core::RpcResult& r) {
        s_inFlight = false;
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", true)) return;
        const auto it = r.result.find("providers");
        if (it == r.result.end() || !it->is_array()) return;
        std::set<std::string> live;
        std::vector<DeadCredProvider> dead;
        for (const auto& p : *it) {
            ui::ChudcodeProviderDesc d;
            if (!ui::parseChudcodeProviderDesc(p, d)) continue;
            // WHITELIST, not "anything but signed_out" (chudcodeProviderUsable): a provider
            // that still holds a working credential alongside a revoked one reports
            // key_set/oauth_connected — only those mean "requests can succeed". Treating
            // `error` as signed-in would advertise a provider whose every call fails.
            if (ui::chudcodeProviderUsable(d.state)) {
                live.insert(d.id);
            } else if (d.state == "error") {
                std::string cause = d.error;
                if (cause.empty()) cause = "credential disabled";
                dead.push_back({d.label, std::move(cause)});
            }
        }
        s_deadCredProviders = std::move(dead);
        st.signedInBridgeProviders = std::move(live);
    }, 15000);
}

// The model dropdown body (hosted in a popup): a search box with a compact glass gear on the
// same row (opens Settings), the usable models with a checkmark on the current one, and a muted
// hint for unkeyed providers. Picking a model sets provider+model and closes the popup.
void drawModelDropdown(AppState& st, bool busy) {
    (void)busy;
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    static char s_modelSearch[64] = "";
    refreshModelAvailability(st, false);
    refreshBridgeModelCatalog(st, false);  // Chudcode catalog sublabels (ctx/cost/capability chips)
    refreshSignedInProviders(st, false);  // mirror Settings->Providers signed-in set into the picker

    if (ImGui::IsWindowAppearing()) { s_modelSearch[0] = 0; ImGui::SetKeyboardFocusHere(); }

    // Search + glass gear on one row: gear is smaller and sits to the right of a compressed field.
    const float gearSz = ImGui::GetFrameHeight() * 0.82f;
    const float gearGap = 4.0f * sc;
    const float searchW = std::max(40.0f * sc,
                                   ImGui::GetContentRegionAvail().x - gearSz - gearGap);
    ImGui::SetNextItemWidth(searchW);
    ImGui::InputTextWithHint("##modelsearch", "Search models", s_modelSearch, sizeof(s_modelSearch));
    ImGui::SameLine(0.0f, gearGap);
    {
        IconUV gear = ui::getIcon("settings", 20);
        const ImVec2 keep = ImGui::GetCursorScreenPos();
        // Align gear vertically with the search field.
        const float fieldH = ImGui::GetFrameHeight();
        if (gearSz < fieldH)
            ImGui::SetCursorScreenPos(ImVec2(keep.x, keep.y + (fieldH - gearSz) * 0.5f));
        const bool openSet = ImGui::InvisibleButton("##mdlgear", ImVec2(gearSz, gearSz));
        const bool gHov = ImGui::IsItemHovered();
        ImDrawList* gdl = ImGui::GetWindowDrawList();
        const ImVec2 gmn = ImGui::GetItemRectMin(), gmx = ImGui::GetItemRectMax();
        // Compact glass pill (theme helper — not a raw button chrome).
        ui::glassRowBackground(gdl, gmn, gmx, ui::metrics::rounding(4.0f),
                               /*stripe=*/false, /*selected=*/gHov, /*washAlpha=*/0.38f);
        const ImU32 ink = ui::Palette::toU32(gHov ? pal.text : pal.textDim);
        if (gear.valid) {
            const float isz = gearSz * 0.58f;
            const ImVec2 gc((gmn.x + gmx.x) * 0.5f, (gmn.y + gmx.y) * 0.5f);
            gdl->AddImage(gear.texId,
                          ImVec2(gc.x - isz * 0.5f, gc.y - isz * 0.5f),
                          ImVec2(gc.x + isz * 0.5f, gc.y + isz * 0.5f),
                          gear.uv0, gear.uv1, ink);
        } else {
            // Fallback: simple gear-ish cog via text when the atlas cell is missing.
            const char* gl = "\xE2\x9A\x99";  // ⚙
            const ImVec2 ts = ImGui::CalcTextSize(gl);
            gdl->AddText(ImVec2((gmn.x + gmx.x - ts.x) * 0.5f, (gmn.y + gmx.y - ts.y) * 0.5f),
                         ink, gl);
        }
        if (gHov) ImGui::SetTooltip("AI settings");
        if (openSet) { st.openSettings = true; st.openSettingsTab = 3; /* Providers */ ImGui::CloseCurrentPopup(); }
    }
    ImGui::Separator();

    auto lc = [](std::string s) {
        std::transform(s.begin(), s.end(), s.begin(),
                       [](unsigned char c) { return (char)std::tolower(c); });
        return s;
    };
    std::string q = lc(s_modelSearch);
    // Match label, provider short name, OR model id (type "k3" / "opus" / "grok").
    auto matchq = [&](const std::string& a, const std::string& b, const std::string& id = {}) {
        if (q.empty()) return true;
        if (lc(a).find(q) != std::string::npos) return true;
        if (lc(b).find(q) != std::string::npos) return true;
        if (!id.empty() && lc(id).find(q) != std::string::npos) return true;
        return false;
    };

    bool picked = false;
    int uid = 0;       // monotonic per-row id seed — keeps ImGui ids unique across sections
    int shown = 0;     // visible rows after availability + search filtering
    ImGui::BeginChild("##modellist", ImVec2(0, 240 * sc), ImGuiChildFlags_None);

    auto catOff = [&](const std::string& key) {
        return st.settings.disabledProviders.count(key) != 0;
    };

    // Composer models = the user's SELECTED (checked = NOT hidden) catalog models for SIGNED-IN
    // providers, sourced from the Settings -> Providers catalog (ui::bridgeModelCatalog()). Each
    // catalog provider id maps to a composer slot + base_url. Every online send runs through
    // Chudcode (agent_admin_turn), so a provider needs no native transport here — only a slot
    // whose wire name Chudcode recognises. Unknown catalog providers map to slot -1 and are
    // excluded so they can't cause provider_error.
    struct Route { int slot; const char* baseUrl; };
    auto routeFor = [](const std::string& p) -> Route {
        if (p == "anthropic") return {ui::PROV_ANTHROPIC, ""};
        if (p == "openai") return {ui::PROV_OPENAI, ""};
        if (p == "google" || p == "gemini") return {ui::PROV_GEMINI, ""};
        if (p == "xai") return {ui::PROV_XAI, "https://api.x.ai/v1"};
        if (p == "kimi-code") return {ui::PROV_KIMISUB, ""};
        if (p == "zenmux") return {ui::PROV_ZENMUX, ""};
        if (p == "cursor") return {ui::PROV_CURSOR, ""};
        if (p == "moonshot") return {ui::PROV_CLOUDCOMPAT, "https://api.moonshot.ai/v1"};
        if (p == "openrouter") return {ui::PROV_CLOUDCOMPAT, "https://openrouter.ai/api/v1"};
        if (p == "deepseek") return {ui::PROV_CLOUDCOMPAT, "https://api.deepseek.com/v1"};
        if (p == "alibaba" || p == "alibaba-coding-plan")
            return {ui::PROV_CLOUDCOMPAT, "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"};
        // Bridge-only catalog providers: no native C++ transport and no dedicated ProviderIndex,
        // so they ride the SHARED cloud slot. The URL here is a DISCRIMINATOR, not an endpoint —
        // ui::cloudServiceKey() maps it back to the hosted provider id pinned by
        // agentProviderOverride; Chudcode supplies the real endpoint and credential.
        if (p == "amazon-bedrock")
            return {ui::PROV_CLOUDCOMPAT, "https://bedrock-runtime.amazonaws.com"};
        if (p == "azure") return {ui::PROV_CLOUDCOMPAT, "https://api.openai.azure.com"};
        if (p == "google-antigravity") return {ui::PROV_CLOUDCOMPAT, "https://antigravity.google"};
        if (p == "aimlapi") return {ui::PROV_CLOUDCOMPAT, "https://api.aimlapi.com/v1"};
        if (p == "openai-codex") return {ui::PROV_CLOUDCOMPAT, "https://chatgpt.com/backend-api/codex"};
        return {-1, ""};  // unrouted catalog provider: no composer slot -> excluded
    };

    const auto& catalog = ui::bridgeModelCatalog();
    if (!catalog.empty()) {
        for (const auto& m : catalog) {
            Route r = routeFor(m.provider);
            if (r.slot < 0) continue;                                        // no composer slot
            // Signed-in providers only. The browser build has no provider credential path, so
            // it never lists online catalog models.
            if (ui::webBuild() || st.signedInBridgeProviders.count(m.provider) == 0) continue;
            std::string catk = (r.slot == ui::PROV_CLOUDCOMPAT) ? ui::cloudServiceKey(r.baseUrl)
                                                                : ui::providerCatKey(r.slot);
            if (catOff(catk)) continue;                                      // provider bulk-disabled
            if (m.deprecated) continue;
            if (modelHidden(st, m.id)) continue;                             // unchecked in Providers
            if (!matchq(m.name, providerShort(r.slot), m.id)) continue;
            ++shown;
            if (modelRow(st, r.slot, m.id, m.name, true, uid++)) {
                st.settings.provider = r.slot;
                std::strncpy(st.settings.model, m.id.c_str(), sizeof(st.settings.model) - 1);
                if (r.baseUrl[0])
                    std::strncpy(st.settings.baseUrl, r.baseUrl, sizeof(st.settings.baseUrl) - 1);
                else
                    st.settings.baseUrl[0] = 0;  // native/bridge-routed: leave base_url empty/default
                st.settings.vision = m.vision;
                picked = true;
            }
        }
    } else {
        // Bridge down / older core (empty catalog): fall back to the per-provider list so the
        // picker still works. Walk the builtin catalog only for its provider ORDER — the first
        // row of each provider block emits that provider's whole list via providerPickerModels,
        // which prefers the LIVE agent_list_models rows (newest first) over the static ones.
        // Gated on provider category + usable credential + search, as before.
        std::set<int> emittedProv;
        for (const auto& b : ui::builtinModels()) {
            if (!emittedProv.insert(b.provider).second) continue;
            if (catOff(ui::providerCatKey(b.provider))) continue;
            if (!providerUsable(st, b.provider)) continue;
            for (const auto& m : ui::providerPickerModels(st, b.provider)) {
                if (!matchq(m.label, providerShort(m.provider), m.id)) continue;
                if (modelHidden(st, m.id)) continue;
                ++shown;
                if (modelRow(st, m.provider, m.id, m.label, true, uid++)) {
                    st.settings.provider = m.provider;
                    std::strncpy(st.settings.model, m.id.c_str(), sizeof(st.settings.model) - 1);
                    picked = true;
                }
            }
        }
    }
    // Live-discovered local models (when a local endpoint is up). The probe assigns the same
    // model list to PROV_LOCAL and PROV_LLAMACPP (one endpoint family), so dedup by id to avoid
    // listing each model twice. Skip models that are downloaded but not loaded, and any the user
    // hid.
    std::set<std::string> seenLocal;
    for (int lp : {ui::PROV_LOCAL, ui::PROV_LLAMACPP}) {
        if (catOff("local")) break;
        const ProviderModels& pm = st.providerModels[lp];
        if (!pm.ok) continue;
        for (const auto& id : pm.models) {
            if (!seenLocal.insert(id).second) continue;     // already shown for the sibling provider
            if (!localModelLoaded(pm, id)) continue;        // downloaded but not loaded into memory
            if (modelHidden(st, id)) continue;
            if (!matchq(id, providerShort(lp), id)) continue;
            ++shown;
            if (modelRow(st, lp, id, id, true, uid++)) {
                st.settings.provider = lp;
                std::strncpy(st.settings.model, id.c_str(), sizeof(st.settings.model) - 1);
                // This pick may be switching AWAY from a cloud provider whose base_url is remote.
                // Point base_url back at the local endpoint so the run hits LM Studio, not the cloud
                // (a local model id sent to the cloud 403s). Preserve an already-loopback base_url so
                // a custom local port survives.
                bool loop = ui::baseUrlIsLoopback(st.settings.baseUrl);
                if (!loop)
                    std::strncpy(st.settings.baseUrl,
                                 lp == ui::PROV_LLAMACPP ? "http://127.0.0.1:1234/v1"
                                                         : "http://localhost:1234/v1",
                                 sizeof(st.settings.baseUrl) - 1);
                picked = true;
            }
        }
    }
    const bool modelsLoading = s_bridgeCatalogInFlight || st.modelProbeInFlight.load() ||
        (s_bridgeCatalogAvailable && s_bridgeCatalogLastFetch < 0.0);
    if (shown == 0) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDisabled));
        ImGui::TextWrapped(modelsLoading ? "Loading models\xe2\x80\xa6" : q.empty()
            ? "No models available — sign in to a subscription (ZenMux / xAI / Claude / Cursor), "
              "add an API key, or start a local model server (gear below)."
            : "No models match your search.");
        ImGui::PopStyleColor();
    }
    ImGui::EndChild();

    // No providers signed in yet -> nothing routable; point the user at Settings to add models.
    if (st.signedInBridgeProviders.empty() && !modelsLoading) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
        ImGui::TextUnformatted("Sign in to a provider in Settings -> Providers to add models.");
        ImGui::PopStyleColor();
    }

    // A provider whose only credential died is NOT in the usable set, so its models are absent
    // from the list above. Say so with the bridge's own cause instead of letting the model
    // silently disappear — the user's next move is to re-authenticate, not to keep looking.
    if (!s_deadCredProviders.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.statusWarn));
        const size_t kShow = 3;
        for (size_t i = 0; i < s_deadCredProviders.size() && i < kShow; ++i)
            ImGui::TextWrapped("%s credential is dead \xe2\x80\x94 %s. Re-authenticate in "
                               "Settings -> Providers to get its models back.",
                               s_deadCredProviders[i].label.c_str(),
                               s_deadCredProviders[i].cause.c_str());
        if (s_deadCredProviders.size() > kShow)
            ImGui::TextWrapped("+%d more provider(s) with a dead credential.",
                               (int)(s_deadCredProviders.size() - kShow));
        ImGui::PopStyleColor();
    }

    // Footer: per-session cost strip (chudcode_session_stats; hidden while the service is absent).
    ImGui::Dummy(ImVec2(0, 2 * sc));
    drawSessionCostStrip(st);

    if (picked) {
        // Persist the descriptor immediately so the next `agent_admin_turn` has matching endpoint
        // details; composer also pins provider/model/effort in that request. This keeps config.json
        // and the picker aligned instead of letting a stale saved cloud endpoint outlive a new local
        // selection.
        saveSettings(st.settings);
        ImGui::CloseCurrentPopup();
    }
}

// ============================ session slash commands (/compact /shake /handoff /btw /prewalk) ===

}  // namespace ui
