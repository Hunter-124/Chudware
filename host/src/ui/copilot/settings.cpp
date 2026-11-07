// Copilot AI settings, model discovery, and the shared authenticated picker policy.

#include "copilot_internal.h"

#include "../web_capabilities.h"    // ui::webGapReason — subscription sign-in is not a web path
#include "../omp_descriptors.h" // ui::parseOmp* — Omp worker payload parsing
#include "../settings_modal.h"       // SettingsRoute deep links
#ifdef __EMSCRIPTEN__
#include "core/jobs.h"
#endif
#include "../provider_usage.h"     // shared subscription status cache and single-flight poller

#include <chrono>
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
// arrives via refreshBridgeModelCatalog / omp_models_list). Probing them would only paint a
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
    // provider's /v1/models, through the Omp service when the credential is an
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
    // Policy-hidden generations (ui::isLegacyGenerationModel) are never offered; the caller
    // still shows a saved hidden selection as its trailing "(custom)" entry.
    if (provider >= 0 && provider < ui::PROV_COUNT && ui::providerIsCloud(provider)) {
        const ProviderModels& pm = st.providerModels[provider];
        if (pm.checked && pm.ok && !pm.rows.empty()) {
            for (const auto& m : pm.rows)
                if (!ui::isLegacyGenerationModel(m.id)) out.push_back(m);
            return out;
        }
    }
    for (const auto& m : ui::builtinModels())
        if (m.provider == provider && !ui::isLegacyGenerationModel(m.id)) out.push_back(m);
    return out;
}

// Every provider card routes its credential copy through here. The AI tab never holds
// credentials: Settings -> Providers stores keys through the OMP auth store, so
// this is the pointer to that tab. The browser build has no provider credential path; it says so.
static void drawValidatedCredentialHint(AppState& st, const char* detail) {
    if (const char* why = ui::webGapReason("account_signin")) {
        dimText(why);
        return;
    }
    dimText(detail);
    if (ImGui::SmallButton("Manage credentials##provider_credentials")) {
        st.openSettings = true;
        st.openSettingsTab = 3;
    }
}

// Free AI (FreeLLMAPI, github.com/tashfeenahmed/freellmapi) provider card — reused from BOTH
// the classic Settings -> AI provider picker and the live Settings -> Providers tab. Endpoint
// and model controls remain here; credentials never do. Keys are stored by
// the OMP auth flow in Settings -> Providers.
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
            flushConfigWrites();
            refreshModelAvailability(st, true);
        }
    }
    drawValidatedCredentialHint(
        st, "Free AI credentials are managed under Settings \xe2\x86\x92 Providers.");
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
            "key through Settings \xe2\x86\x92 Providers.");
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
        // Local endpoint defaults are retained; online models are selected by exact catalog identity.
        if (s.provider == ui::PROV_LOCAL) {
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
        // tab: Settings -> Providers stores them through the OMP auth store.
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
                    : "API keys are managed under Settings \xe2\x86\x92 Providers.");
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
            st, "Use the system-browser xAI sign-in above, or add an xAI API key under Settings \xe2\x86\x92 Providers.");
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
        // Omp-service provider — the credential lives in the service's own auth store,
        // wired up via Settings → Providers sign-in (drawOmpProviderManager), not config.json.
        dimText("ZenMux (agent service). Sign in under Settings \xe2\x86\x92 Providers; no API "
                "key field here — the service resolves the credential from its own auth store.");
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
        ImGui::TextUnformatted("Wire provider: zenmux \xc2\xb7 model: x-ai/grok-4.5-free (default)");
        ImGui::PopStyleColor();
        if (ImGui::Checkbox("Model supports vision (send screenshots)", &s.vision)) changed = true;
    } else if (cloud) {
        drawValidatedCredentialHint(
            st, "API keys are managed under Settings \xe2\x86\x92 Providers.");
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
    if (!s.offlineMode) {
        const ui::BridgeModelMeta* meta = s.composerFollowRoles ? nullptr :
            ui::bridgeModelMeta(s.ompProvider, s.model);
        ui::kit::FieldGroup group("ai_generation", "Model capabilities");
        const std::string context = meta && meta->contextWindow > 0
            ? std::to_string(meta->contextWindow) : "unknown";
        const std::string output = meta && meta->maxTokens > 0
            ? std::to_string(meta->maxTokens) : "unknown";
        ui::kit::fieldReadout("ctx_readout", "Context window", context.c_str());
        ui::kit::fieldReadout("output_readout", "Max response tokens", output.c_str());
        ui::kit::fieldReadout("effort_readout", "Thinking",
                              "Set per model in the composer or model assignments");
        return;
    }
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
    case AiSettingsSection::ProviderModel: {
        const int previousProvider = s.provider;
        const std::string previousModel = s.model;
        drawProviderModel();
        if (s.provider != previousProvider || previousModel != s.model)
            s.composerFollowRoles = false;
        break;
    }
    case AiSettingsSection::Generation:
        drawGeneration();
        break;
    }
    return changed;
}

// Quick-settings helpers + overlay body (Copilot gear). Full providers/roles/prompts live in Settings.
//
// Max nesting depth and the subagent tool-call budget are drawn every frame by TWO surfaces
// (Quick settings and Settings → AI → Harness). Each used to keep its own load-once static, so a
// change in one showed in the other only after a restart. They now share ONE process-wide cache
// per knob: the saver refreshes it, and it is re-read from config.json every few seconds so an
// external writer shows up too. GUI thread only.
namespace {
struct SharedAgentInt {
    int value = 0;
    bool valid = false;
    std::chrono::steady_clock::time_point at{};
    bool fresh() const {
        return valid && std::chrono::steady_clock::now() - at < std::chrono::seconds(3);
    }
    void set(int v) {
        value = v;
        valid = true;
        at = std::chrono::steady_clock::now();
    }
};
SharedAgentInt g_maxSubagentDepth;
SharedAgentInt g_subagentToolCallBudget;
}  // namespace

int loadMaxSubagentDepth() {
    if (g_maxSubagentDepth.fresh()) return g_maxSubagentDepth.value;
    // agent.max_subagent_depth — nesting levels of subagent-spawning-subagent
    // (not concurrency). Root chat = depth 0. Default 2, GUI clamp 0-4.
    // Enforced by AgentCoord (delegation.py / mission_dispatch.py).
    int depth = 2;
    try {
        const nlohmann::json j = readConfigJson();
        if (j.is_object()) {
            auto agent = j.value("agent", nlohmann::json::object());
            if (agent.is_object() && agent.contains("max_subagent_depth") &&
                agent["max_subagent_depth"].is_number_integer()) {
                depth = agent["max_subagent_depth"].get<int>();
            }
        }
    } catch (...) {}
    if (depth < 0) depth = 0;
    if (depth > 4) depth = 4;
    g_maxSubagentDepth.set(depth);
    return depth;
}
void saveMaxSubagentDepth(int depth) {
    if (depth < 0) depth = 0;
    if (depth > 4) depth = 4;
    g_maxSubagentDepth.set(depth);  // the other surface sees it on its very next frame
    updateConfigJson([&](nlohmann::json& j) {
        if (!j.contains("agent") || !j["agent"].is_object())
            j["agent"] = nlohmann::json::object();
        j["agent"]["max_subagent_depth"] = depth;
    });
}

int loadSubagentToolCallBudget() {
    if (g_subagentToolCallBudget.fresh()) return g_subagentToolCallBudget.value;
    // agent.subagent_tool_call_budget is a soft convergence warning for delegated subagents.
    // Default 0 lets the harness derive today's 4-16 call range from each token grant; the
    // 0-64 GUI range preserves automatic behavior while leaving deliberate manual headroom.
    int budget = 0;
    try {
        const nlohmann::json j = readConfigJson();
        if (j.is_object()) {
            auto agent = j.value("agent", nlohmann::json::object());
            if (agent.is_object() && agent.contains("subagent_tool_call_budget") &&
                agent["subagent_tool_call_budget"].is_number_integer()) {
                budget = agent["subagent_tool_call_budget"].get<int>();
            }
        }
    } catch (...) {}
    if (budget < 0) budget = 0;
    if (budget > 64) budget = 64;
    g_subagentToolCallBudget.set(budget);
    return budget;
}
void saveSubagentToolCallBudget(int budget) {
    if (budget < 0) budget = 0;
    if (budget > 64) budget = 64;
    g_subagentToolCallBudget.set(budget);
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

    // ---- Model sizes ---- Same state and pickers as Settings → AI → Models.
    if (ui::kit::cardBegin("q_card_sizes", "Model sizes", "ai_panel")) {
        ui::drawQuickModelSizes(st, labelCol,
                                ImGui::ColorConvertFloat4ToU32(surf.textSecondary));
    }
    ui::kit::cardEnd();
    ui::kit::gapRow();

    // ---- Limits + memory ----
    if (ui::kit::cardBegin("q_card_limits", "Limits & memory", "settings")) {
        static const char* kDepthLabels[] = {"0", "1", "2", "3", "4"};
        // The shared cache (see loadMaxSubagentDepth) — also read by Settings → AI → Harness.
        int depth = std::max(0, std::min(4, loadMaxSubagentDepth()));
        if (segRow("##q_depth", "Max nesting",
                   "Max subagent nesting: how many levels deep a subagent may spawn its own "
                   "subagents (0 = no subagents).",
                   kDepthLabels, 5, &depth)) {
            saveMaxSubagentDepth(depth);
        }

        static const char* kMemoryBackends[] = {"off", "sharpshooter", "mnemopi", "local", "hindsight"};
        static const char* kMemoryLabels[] = {"Off", "Decisions", "Full recall", "Local", "Hindsight"};
        int cur = -1;
        for (int i = 0; i < 5; ++i)
            if (s.memoryBackend == kMemoryBackends[i]) cur = i;
        if (cur < 0) {
            dimText(("Unknown memory backend: " + s.memoryBackend).c_str());
        } else if (segRow("##q_memory", "Project memory", "Project memory, kept per folder.",
                          kMemoryLabels, 5, &cur)) {
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
        deepLink("AI##q_open_ai", SettingsAI);
        ImGui::SameLine(0.0f, gap);
        deepLink("Providers##q_open_prov", SettingsProviders);
        ImGui::SameLine(0.0f, gap);
        deepLink("Prompts##q_open_prompts", SettingsAiPrompts);
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



// ============================ Omp model catalog + session cost (S-UI-PROV) ===
// Omp's catalog (capabilities + $/Mtok cost) via `omp_models_list`,
// and the per-session cost strip via `omp_session_stats`. The worker
// re-discovers models in the background; poll briefly until the fresh service is ready.
// Soft failures retain the last confirmed data and retry. Stale disk metadata is not availability,
// with ONE exception: while a running agent turn holds hostd's service lane, nothing can be read
// live (hostd refuses every other agent-lane call), so the previous session's confirmed catalog
// and provider list stand in and are labelled as such.

namespace {

std::vector<ui::BridgeModelMeta> s_bridgeModels;
std::vector<ui::BridgeModelMeta> s_scenarioModels;
bool s_scenarioCatalog = false;
bool s_bridgeCatalogAvailable = true;  // false after an unknown-method response
bool s_bridgeCatalogInFlight = false;
bool s_bridgeCatalogRefreshing = false;
bool s_bridgeCatalogError = false;
bool s_bridgeCatalogLive = false;
// The last answer was "a running agent turn holds the service lane": a wait, not a failure.
bool s_bridgeCatalogDeferred = false;
// s_bridgeModels is the previous session's saved catalog, not yet replaced by a live read.
bool s_bridgeCatalogFromDisk = false;
bool s_providerListInFlight = false;
bool s_providerListError = false;
bool s_providerListLoaded = false;
bool s_providerListDeferred = false;
// st.signedInBridgeProviders / dead-credential notes came from the previous session's saved
// provider list, not yet replaced by a live read.
bool s_providerListFromDisk = false;
double s_bridgeCatalogLastFetch = -1e9;

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
// out; never flag a current flagship by broad prefix match. Claude / OpenAI GPT generations are
// NOT handled here: ui::isLegacyGenerationModel removes them from every list outright.
bool isDeprecatedModelId(const std::string& id) {
    static const char* kLegacySubstrings[] = {
        "text-davinci", "davinci", "babbage", "ada-",
        "gemini-1.0", "gemini-pro-vision", "palm", "deprecated",
    };
    const std::string lo = toLowerCopy(id);
    for (const char* sub : kLegacySubstrings)
        if (lo.find(sub) != std::string::npos) return true;
    return false;
}

// One catalog payload (`omp_models_list` "models") -> sorted rows. Shared by the live fetch
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
        b.visionKnown = m.contains("vision") && m["vision"].is_boolean();
        b.vision = m.value("vision", false);
        b.toolsKnown = (m.contains("toolCall") && m["toolCall"].is_boolean()) ||
                       (m.contains("supportsTools") && m["supportsTools"].is_boolean());
        b.tools = m.contains("toolCall") && m["toolCall"].is_boolean()
            ? m["toolCall"].get<bool>() : m.value("supportsTools", false);
        b.reasoningKnown = m.contains("reasoning") && m["reasoning"].is_boolean();
        b.reasoning = m.value("reasoning", false);
        if (m.contains("kind") && m["kind"].is_string()) b.kind = m["kind"].get<std::string>();
        auto ef = m.find("thinking_levels");
        if (ef == m.end()) ef = m.find("reasoningEfforts");
        b.reasoningEffortsKnown = ef != m.end() && ef->is_array();
        if (b.reasoningEffortsKnown)
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
    s_bridgeCatalogFromDisk = !s_bridgeModels.empty();
}

// The provider list as last confirmed by a successful omp_provider_list, kept beside the catalog
// cache. Written only from a complete live read, so it never persists an "unknown" availability.
std::string providerListCachePath() { return appdataAppDir() + "/provider_list_cache.json"; }

}  // namespace

std::string savedProviderListJson() {
    std::ifstream f(providerListCachePath(), std::ios::binary);
    if (!f) return {};
    const nlohmann::json j = nlohmann::json::parse(f, nullptr, /*allow_exceptions=*/false);
    if (!j.is_object()) return {};
    const auto it = j.find("providers");
    return it != j.end() && it->is_array() && !it->empty() ? it->dump() : std::string();
}

void saveProviderListJson(const std::string& providersJson) {
    // Both pollers (the picker and Settings -> Providers) report here every few seconds; only a
    // changed list touches the disk.
    static std::string s_lastWritten;
    if (providersJson == s_lastWritten) return;
    const nlohmann::json providers = nlohmann::json::parse(providersJson, nullptr, false);
    if (!providers.is_array() || providers.empty()) return;
    const std::string path = providerListCachePath();
    const std::string tmp  = path + ".tmp";
    {
        std::ofstream f(tmp, std::ios::binary | std::ios::trunc);
        if (!f) return;
        f << nlohmann::json{{"providers", providers}}.dump();
        if (!f) return;
    }
    std::error_code ec;
    std::filesystem::rename(tmp, path, ec);
    if (ec) std::filesystem::remove(tmp, ec);
    else s_lastWritten = providersJson;
}

// Web demo only: the server-filtered omp_models_list is the entire model universe. A saved or
// default selection (config.json from an earlier page, or "follow model assignments", which
// resolves server-side outside the allowlist) that is not one of the returned, usable rows is
// replaced by the first row the picker shows, so no stale or hardcoded model is ever sent.
static void reconcileDemoModelSelection(AppState& st) {
    if (!ui::webBuild() || !ui::webDemo()) return;
    const BridgeModelMeta* first = nullptr;
    bool selectionListed = false;
    for (const auto& m : s_bridgeModels) {
        if (!bridgePickerModelUsable(st, m)) continue;
        if (!first) first = &m;
        if (m.provider == st.settings.ompProvider && m.id == st.settings.model) selectionListed = true;
    }
    if (!first) return;
    bool changed = false;
    if (!selectionListed) {
        st.settings.provider = ui::PROV_OPENAI;   // legacy classification only, as the picker does
        st.settings.ompProvider = first->provider;
        std::strncpy(st.settings.model, first->id.c_str(), sizeof(st.settings.model) - 1);
        st.settings.model[sizeof(st.settings.model) - 1] = 0;
        st.settings.vision = first->vision;
        changed = true;
    }
    if (st.settings.composerFollowRoles) { st.settings.composerFollowRoles = false; changed = true; }
    if (changed) saveSettings(st.settings);
}

void refreshBridgeModelCatalog(AppState& st, bool force) {
    if (s_scenarioCatalog) return;
    ensureBridgeCatalogSeeded();
    if (s_bridgeCatalogInFlight) return;
    const double now = ImGui::GetTime();
    const bool followupReady = startupCatalogFollowupReady(st);
    if (!force && (!st.workerStarted || st.workerBootPending || !st.startupPhase.empty()))
        return;
    if (!force && s_bridgeCatalogLastFetch > -1e9 && !followupReady) return;
    // The catalog lists only providers that are signed in, so a sign-in, key or logout (made
    // here, in onboarding, or from the CLI) makes the cached catalog stale for exactly that
    // provider: the list says it is usable while the catalog has none of its models. Re-read at
    // the retry cadence when the signed-in set differs from the one the last read was made for,
    // not in five minutes.
    static std::set<std::string> s_readForProviders;
    const bool signedInChanged = st.signedInBridgeProviders != s_readForProviders;
    // Retry transient failures promptly; an absent method is retried less often in case the
    // worker becomes available later. A run holding the service lane answers at once, so waiting
    // for it polls at the retry cadence too. Successful steady-state polling stays at five minutes.
    const double interval = !s_bridgeCatalogAvailable ? 60.0 :
                            (s_bridgeCatalogRefreshing || s_bridgeCatalogError ||
                             s_bridgeCatalogDeferred || signedInChanged ? 5.0 : 300.0);
    if (!force && (now - s_bridgeCatalogLastFetch) < interval)
        return;
    s_readForProviders = st.signedInBridgeProviders;
    s_bridgeCatalogLastFetch = now;
    s_bridgeCatalogInFlight = true;
    st.rpc.call("omp_models_list", nlohmann::json::object(),
                [&st](const core::RpcResult& r) {
        s_bridgeCatalogInFlight = false;
        s_bridgeCatalogLastFetch = ImGui::GetTime();
        // A running agent turn holds hostd's agent lane and every other call on it is refused at
        // once. That is a wait, not a failed read: no error state, the last data stays, and the
        // poll above retries until the turn ends.
        s_bridgeCatalogDeferred = core::workerLaneBusy(r);
        if (s_bridgeCatalogDeferred) return;
        const bool success = r.ok && r.result.is_object() &&
                             (!r.result.contains("ok") || jsonOptBool(r.result, "ok"));
        const auto it = success ? r.result.find("models") : r.result.end();
        const bool valid = success && it != r.result.end() && it->is_array();
        const bool refreshFailed = success && r.result.contains("refreshError") &&
            r.result["refreshError"].is_string() &&
            !r.result["refreshError"].get<std::string>().empty();
        s_bridgeCatalogError = !valid || refreshFailed;
        s_bridgeCatalogRefreshing = valid && !refreshFailed &&
                                    jsonOptBool(r.result, "refreshing");
        s_bridgeCatalogAvailable = !core::workerMethodAbsent(r);
        if (s_bridgeCatalogError) return;  // retain the last confirmed snapshot, not a disk seed
        s_bridgeCatalogLive = true;
        s_bridgeCatalogFromDisk = false;
        s_bridgeModels = parseBridgeCatalogRows(*it);
        if (!s_bridgeModels.empty()) saveBridgeCatalogCache(*it);
        reconcileDemoModelSelection(st);
    }, 15000);
}

void setModelSettingsScenarioCatalog(AppState& st, std::vector<BridgeModelMeta> models) {
    const char* scenario = std::getenv("CHUDWARE_SHOT");
    if (!st.shot || !scenario || std::strcmp(scenario, "model_settings") != 0) return;
    s_scenarioModels = std::move(models);
    s_scenarioCatalog = true;
}

const std::vector<ui::BridgeModelMeta>& bridgeModelCatalog() {
    if (s_scenarioCatalog) return s_scenarioModels;
    ensureBridgeCatalogSeeded();
    return s_bridgeModels;
}

const ui::BridgeModelMeta* bridgeModelMeta(const std::string& providerId,
                                               const std::string& modelId) {
    for (const auto& m : bridgeModelCatalog()) {
        if (m.id != modelId) continue;
        if (providerId.empty() || m.provider == providerId) return &m;
    }
    return nullptr;
}

const ui::BridgeModelMeta* bridgeModelMetaForCad(int cadProvider, const std::string& modelId,
                                                     const char* cloudBaseUrl) {
    // CAD ProviderIndex -> Omp catalog provider id (the service-routed subset; local endpoints
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
    // "ctx 200k \xc2\xb7 $3/$15 \xc2\xb7 tools vision" — Omp model-browser row format.
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

    // The Omp host owns the session's accounting: omp_session_stats prices the
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
        st.rpc.call("omp_session_stats", nlohmann::json{{"session_id", sess}},
                    [sess](const core::RpcResult& r) {
            s_costInFlight = false;
            if (core::workerMethodAbsent(r)) {
                s_costAvailable = false;
                s_costHasData = false;
                return;
            }
            if (s_costSession != sess) return;   // conversation switched mid-flight
            ui::OmpSessionStats stats;
            if (!r.ok || !ui::parseOmpSessionStats(r.result, stats))
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
                     bool usable, int uid, const char* routeBaseUrl = nullptr,
                     const std::string& wireProvider = {}) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const float rowH = ImGui::GetFrameHeight();
    const float fh = ImGui::GetFontSize();
    const float gut = fh + 8 * sc;
    bool current = !st.settings.composerFollowRoles &&
                   std::strcmp(st.settings.model, id.c_str()) == 0 &&
                   (wireProvider.empty() ? st.settings.provider == provider
                                         : st.settings.ompProvider == wireProvider);
    ImGui::PushID(uid);
    bool clicked = ImGui::Selectable("##mrow", current,
                                     usable ? 0 : ImGuiSelectableFlags_Disabled, ImVec2(0, rowH));
    // The caller applies the pick AFTER this returns, so the glyphs below would otherwise
    // paint from pre-click state: the row you just clicked drew unchecked for a frame.
    if (clicked && usable) current = true;
    {
        ImGuiContext& g = *GImGui;
        IMGUI_TEST_ENGINE_ITEM_INFO(ImGui::GetItemID(), label.c_str(), g.LastItemData.StatusFlags |
            ImGuiItemStatusFlags_Checkable | (current ? ImGuiItemStatusFlags_Checked : 0));
    }
    // Omp catalog sublabel (ctx / cost / capabilities) for service-routed providers.
    const ui::BridgeModelMeta* meta = wireProvider.empty()
        ? bridgeModelMetaForCad(provider, id, routeBaseUrl ? routeBaseUrl : st.settings.baseUrl)
        : bridgeModelMeta(wireProvider, id);
    const std::string sublabel = meta ? bridgeModelSublabel(*meta) : std::string();
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayShort)) {
        // Full model name + provider for the composer overlay picker; cost detail (cache
        // pricing, max output) rides the tooltip per the Omp model-browser convention.
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
                ui::Palette::toU32(pal.textDim), wireProvider.empty() ? providerShort(provider) : wireProvider.c_str());
    if (!usable) {
        const char* hint = "Add key";
        float hw = ImGui::CalcTextSize(hint).x;
        dl->AddText(ImVec2(mx.x - hw - 8 * sc, midY - fh * 0.5f),
                    ui::Palette::toU32(pal.accent), hint);
    } else if (!sublabel.empty()) {
        // Right-aligned dim "ctx 200k \xc2\xb7 $3/$15 \xc2\xb7 tools vision" (Omp model row).
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

// One omp_provider_list `providers` array -> the picker's signed-in set and dead-credential
// notes. The live reply and the previous session's saved list both go through here, so they
// produce the same state.
void applyProviderList(AppState& st, const nlohmann::json& providers) {
    std::set<std::string> live;
    std::vector<DeadCredProvider> dead;
    for (const auto& p : providers) {
        ui::OmpProviderDesc d;
        if (!ui::parseOmpProviderDesc(p, d)) continue;
        // Unknown availability retains a previous confirmation but cannot create one.
        if (ui::ompProviderAvailableAfterRefresh(d, st.signedInBridgeProviders.count(d.id))) {
            live.insert(d.id);
        } else if (d.state == "error") {
            std::string cause = d.error;
            if (cause.empty()) cause = "credential disabled";
            dead.push_back({d.label, std::move(cause)});
        }
    }
    s_deadCredProviders = std::move(dead);
    st.signedInBridgeProviders = std::move(live);
}

// Cold start: the previous session's confirmed provider list stands in until a live read lands.
// It is only ever USED while a running agent turn holds the service lane (bridgePickerModelUsable).
void ensureProviderListSeeded(AppState& st) {
    static bool s_seeded = false;
    if (s_seeded) return;
    s_seeded = true;
    if (s_providerListLoaded || !st.signedInBridgeProviders.empty()) return;
    const nlohmann::json saved = nlohmann::json::parse(savedProviderListJson(), nullptr, false);
    if (!saved.is_array()) return;
    applyProviderList(st, saved);
    s_providerListFromDisk = !st.signedInBridgeProviders.empty() || !s_deadCredProviders.empty();
}
}  // namespace

// Poll omp_provider_list so the composer knows which providers are signed in WITHOUT the
// user having opened Settings -> Providers. Fills st.signedInBridgeProviders (same set the
// Providers panel writes) so the dropdown mirrors the Providers tab. TTL'd; degrades silently.
void refreshSignedInProviders(AppState& st, bool force) {
    // Browser credentials belong to the gateway account, never a shared SDK auth store.
    if (ui::webBuild()) return;
    ensureProviderListSeeded(st);
    static double s_lastPoll = -1e9;
    if (s_providerListInFlight) return;
    const double now = ImGui::GetTime();
    if (!force && (now - s_lastPoll) < 5.0) return;
    s_lastPoll = now;
    s_providerListInFlight = true;
    st.rpc.call("omp_provider_list", nlohmann::json::object(),
                [&st](const core::RpcResult& r) {
        s_providerListInFlight = false;
        // A running agent turn holds hostd's agent lane and every other call on it is refused at
        // once. That is a wait, not a failed read: no error state, what is shown stays, and the
        // 5 s poll asks again until the turn ends.
        s_providerListDeferred = core::workerLaneBusy(r);
        if (s_providerListDeferred) return;
        if (!r.ok || !r.result.is_object() ||
            (r.result.contains("ok") && !jsonOptBool(r.result, "ok"))) {
            s_providerListError = true;
            return;
        }
        const auto it = r.result.find("providers");
        if (it == r.result.end() || !it->is_array()) {
            s_providerListError = true;
            return;
        }
        s_providerListError = r.result.contains("refreshError") &&
            r.result["refreshError"].is_string() &&
            !r.result["refreshError"].get<std::string>().empty();
        s_providerListLoaded = true;
        s_providerListFromDisk = false;
        applyProviderList(st, *it);
        // Only a complete read is worth keeping for the next launch.
        if (!s_providerListError) saveProviderListJson(it->dump());
    }, 15000);
}

bool bridgePickerModelUsable(const AppState& st, const BridgeModelMeta& m) {
    // Web demo: the server already filtered omp_models_list to the free-model allowlist and
    // there is no per-user sign-in state, so the returned catalog is the whole truth (no
    // provider-list confirmation, no signed-in set, no generation policy over that allowlist).
    const bool demo = ui::webBuild() && ui::webDemo();
    // Claude < 5.1 / GPT < 6 are never offered, whatever the user's per-model show toggles say.
    // A saved selection of one still displays: pickers render the current value separately.
    if (!demo && ui::isLegacyGenerationModel(m.id)) return false;
    // Availability needs a live confirmation of the catalog AND the provider list. The one
    // exception: while a running agent turn holds the service lane nothing can be read live, so
    // the previous session's saved copies — confirmed when they were saved — stand in.
    const bool catalogConfirmed =
        s_bridgeCatalogLive || (s_bridgeCatalogDeferred && s_bridgeCatalogFromDisk);
    const bool providersConfirmed =
        demo || s_providerListLoaded || (s_providerListDeferred && s_providerListFromDisk);
    if (!s_scenarioCatalog && (!catalogConfirmed || !providersConfirmed)) return false;
    const std::string identity = m.provider + "/" + m.id;
    if (!demo && (ui::webBuild() || st.signedInBridgeProviders.count(m.provider) == 0)) return false;
    if (st.settings.hiddenModels.count(identity) ||
        (st.settings.hiddenModels.count(m.id) &&
         !st.settings.shownLegacyHiddenModels.count(identity))) return false;
    return st.settings.disabledProviders.count(m.provider) == 0;
}

bool pickerLocalModelUsable(const AppState& st, int provider, const std::string& id) {
    if (provider != ui::PROV_LOCAL && provider != ui::PROV_LLAMACPP) return false;
    const ProviderModels& pm = st.providerModels[provider];
    return pm.ok && localModelLoaded(pm, id) &&
           !st.settings.disabledProviders.count("local") &&
           !st.settings.hiddenModels.count(id);
}

// True while the catalog or the provider list has no live answer ONLY because a running agent
// turn holds the service lane. Not an error: what is saved from the last session (if anything)
// is shown, and everything refreshes when the turn ends.
bool modelPickerDeferred() {
    return !s_scenarioCatalog && !ui::webBuild() &&
           ((s_bridgeCatalogDeferred && !s_bridgeCatalogLive) ||
            (s_providerListDeferred && !s_providerListLoaded));
}

bool modelPickerLoading() {
    if (modelPickerDeferred()) return false;   // waiting on a run, not on a read
    return (!s_scenarioCatalog &&
            (s_bridgeCatalogInFlight || s_bridgeCatalogRefreshing ||
             (s_bridgeCatalogAvailable && s_bridgeCatalogLastFetch < 0.0))) ||
           s_providerListInFlight ||
           (!ui::webBuild() && !s_providerListLoaded && !s_providerListError);
}

bool modelPickerError() { return s_bridgeCatalogError || s_providerListError; }

// The model dropdown body (hosted in a popup): a search box with a compact glass gear on the
// same row (opens Settings), the usable models with a checkmark on the current one, and a muted
// hint for unkeyed providers. Picking a model sets provider+model and closes the popup.
void drawModelDropdown(AppState& st, bool busy) {
    (void)busy;
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    static char s_modelSearch[64] = "";
    refreshModelAvailability(st, false);
    refreshBridgeModelCatalog(st, false);  // Omp catalog sublabels (ctx/cost/capability chips)
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
    const bool demoModels = ui::webBuild() && ui::webDemo();  // exactly the server's rows
    ImGui::BeginChild("##modellist", ImVec2(0, 240 * sc), ImGuiChildFlags_None);
    if (!demoModels && matchq("Follow model assignments", "Settings", "assignments")) {
        if (ImGui::Selectable("Follow model assignments", st.settings.composerFollowRoles)) {
            st.settings.composerFollowRoles = true;
            saveSettings(st.settings);
            ImGui::CloseCurrentPopup();
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Use the configured chat assignment (Large in Simple mode).\n"
                              "Default follows the model last selected here.");
        ImGui::Separator();
    }

    auto catOff = [&](const std::string& key) {
        return st.settings.disabledProviders.count(key) != 0;
    };

    const auto& catalog = ui::bridgeModelCatalog();
    if (!catalog.empty()) {
        for (const auto& m : catalog) {
            if (!bridgePickerModelUsable(st, m)) continue;
            if (!matchq(m.name, m.provider, m.id)) continue;
            ++shown;
            if (modelRow(st, ui::PROV_OPENAI, m.id, m.name, true, uid++, nullptr, m.provider)) {
                st.settings.provider = ui::PROV_OPENAI; // Legacy offline/cloud classification only.
                st.settings.ompProvider = m.provider;
                std::strncpy(st.settings.model, m.id.c_str(), sizeof(st.settings.model) - 1);
                st.settings.model[sizeof(st.settings.model) - 1] = 0;
                st.settings.vision = m.vision;
                picked = true;
            }
        }
    }
    // The legacy endpoint probe has no OMP provider identity, so keep its rows out of
    // online selections. Both offline slots share one probe, so deduplicate by id.
    std::set<std::string> seenLocal;
    for (int lp : {ui::PROV_LOCAL, ui::PROV_LLAMACPP}) {
        if (demoModels || !st.settings.offlineMode || catOff("local")) break;
        const ProviderModels& pm = st.providerModels[lp];
        if (!pm.ok) continue;
        for (const auto& id : pm.models) {
            if (!pickerLocalModelUsable(st, lp, id)) continue;
            if (!seenLocal.insert(id).second) continue;
            if (!matchq(id, providerShort(lp), id)) continue;
            ++shown;
            if (modelRow(st, lp, id, id, true, uid++)) {
                st.settings.provider = lp;
                std::strncpy(st.settings.model, id.c_str(), sizeof(st.settings.model) - 1);
                if (!st.settings.ompProvider.empty()) st.settings.apiKey[0] = 0;
                st.settings.ompProvider.clear();
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
    const bool modelsLoading = modelPickerLoading() || st.modelProbeInFlight.load();
    const bool modelsDeferred = modelPickerDeferred();
    if (shown == 0) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDisabled));
        ImGui::TextWrapped(modelsDeferred ? "Models appear when the current run finishes." :
            modelsLoading ? "Loading models\xe2\x80\xa6" :
            modelPickerError() ? "Model availability could not be confirmed. Retry discovery." :
            q.empty() ? "No models available. Check your enabled providers or start a local model server."
                      : "No models match your search.");
        ImGui::PopStyleColor();
    } else if (modelsDeferred) {
        // What is listed was confirmed in the previous session; a running turn keeps the live
        // read waiting. Say so rather than presenting it as current.
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDisabled));
        ImGui::TextWrapped("Showing models from your last session. They refresh when the current run finishes.");
        ImGui::PopStyleColor();
    }
    if (modelPickerError()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.statusWarn));
        ImGui::TextWrapped("Could not refresh all provider information. Showing last confirmed models when available.");
        ImGui::PopStyleColor();
        if (ImGui::SmallButton("Retry model discovery")) {
            refreshBridgeModelCatalog(st, true);
            refreshSignedInProviders(st, true);
            refreshModelAvailability(st, true);
        }
    }
    ImGui::EndChild();

    // No providers signed in yet -> nothing routable; point the user at Settings to add models.
    // The web demo has no sign-in at all: its footer says which models those are instead.
    const bool webDemoPicker = ui::webBuild() && ui::webDemo();
    if (webDemoPicker || (st.signedInBridgeProviders.empty() && !modelsLoading && !modelsDeferred &&
                          !modelPickerError())) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
        if (webDemoPicker)
            ImGui::TextUnformatted(ui::webGapReason("account_signin"));
        else
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

    // Footer: per-session cost strip (omp_session_stats; hidden while the service is absent).
    ImGui::Dummy(ImVec2(0, 2 * sc));
    drawSessionCostStrip(st);

    if (picked) {
        st.settings.composerFollowRoles = false;
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
