// Copilot AI settings (T-RF6 split of legacy_chat.cpp): the shared AI settings sections
// (provider/model/auth, generation, privacy, manage-models — ui::drawAiSettingsSection /
// drawProviderModelPicker, consumed by the Settings modal AI tab and the in-column settings
// overlay), the searchable model dropdown, model availability probing, and provider-usability
// gating (providerUsable / cloud+moonshot key presence).

#include "copilot_internal.h"

namespace ui {

void refreshModelAvailability(AppState& st, bool force) {
    // Discover models on the LOCAL endpoint regardless of which provider is currently active, so
    // local models always show up in the picker WITHOUT the user first switching the Provider
    // dropdown to "Local". Previously this early-returned whenever a cloud provider (OpenRouter /
    // DashScope / ...) was active — st.settings.baseUrl is then a remote URL — which is exactly why
    // local models were invisible until the user "poked around in settings" and selected Local.
    auto isLoopback = [](const char* u) {
        std::string s = u ? u : "";
        return s.find("localhost") != std::string::npos ||
               s.find("127.0.0.1") != std::string::npos ||
               s.find("0.0.0.0") != std::string::npos || s.find("[::1]") != std::string::npos;
    };
    // When the active base_url IS loopback, probe it (honors a custom local port). Otherwise a cloud
    // provider is active, so its base_url isn't the local one — fall back to the default LM Studio
    // endpoint so local discovery still happens. (A non-default local port is only auto-found while
    // the Local provider is the active one; the default covers the common case.)
    bool activeLoopback = isLoopback(st.settings.baseUrl);
    std::string localUrl = activeLoopback ? std::string(st.settings.baseUrl)
                                          : std::string("http://localhost:1234/v1");
    std::string localKey = activeLoopback ? std::string(st.settings.apiKey) : std::string();
    static double lastProbe = -1e9;
    // Failure backoff (freeze fix): when the local endpoint is DOWN, each probe burns ~5s in a
    // curl connect-fail INSIDE the worker, holding the WorkerClient mutex. At the old flat 5s
    // cadence the worker was busy-failing near-100% of the time, so any SYNC worker.call from
    // the GL thread (CAM/ECAD panels on workspace switch) blocked behind it — the app appeared
    // hard-frozen and had to be force-killed. Back off to 60s after a failed probe (force — the
    // user clicking refresh in the picker — still bypasses); a success restores the 5s cadence.
    static bool lastProbeFailed = false;
    const double cooldown = lastProbeFailed ? 60.0 : 5.0;
    double now = ImGui::GetTime();
    if (!force && (st.modelProbeInFlight.load() || (now - lastProbe) < cooldown)) return;
    lastProbe = now;
    st.modelProbeInFlight.store(true);
    nlohmann::json p = {{"provider", "openai"},
                        {"base_url", localUrl},
                        {"api_key", localKey}};
    st.rpc.call("agent_list_models", p, [&st](const core::RpcResult& r) {
        ProviderModels pm;
        pm.checked = true;
        if (r.ok && r.result.is_object()) {
            pm.ok = r.result.value("ok", false);
            pm.error = r.result.value("error", std::string());
            if (r.result.contains("models") && r.result["models"].is_array())
                for (const auto& m : r.result["models"])
                    if (m.is_string()) pm.models.push_back(m.get<std::string>());
            // LM Studio reports which models are actually loaded (vs merely downloaded); when
            // present, the picker shows only those. Absent => every listed model is available.
            pm.loadedKnown = r.result.value("loaded_known", false);
            if (r.result.contains("loaded") && r.result["loaded"].is_array())
                for (const auto& m : r.result["loaded"])
                    if (m.is_string()) pm.loaded.push_back(m.get<std::string>());
        } else {
            pm.error = r.err.empty() ? "core unavailable" : r.err;
        }
        lastProbeFailed = !pm.ok;   // drives the 60s down-endpoint backoff above
        st.providerModels[ui::PROV_LOCAL] = pm;
        st.providerModels[ui::PROV_LLAMACPP] = pm;  // same local endpoint family
        st.modelProbeInFlight.store(false);
    }, 12000);
}

// A row in the OpenRouter model list (curated preset or live-discovered free model).
struct OpenRouterModel {
    std::string id;
    std::string label;
    bool vision = false;
    bool free = false;
};
static std::vector<OpenRouterModel> openRouterModels(AppState& st);  // fwd (defined just below)

// Probe OpenRouter's public /models catalog and cache the list of currently-FREE model ids (the
// endpoint exposes per-model pricing). Stored in providerModels[PROV_CLOUDCOMPAT] so the OpenRouter
// category can list every free model live instead of from a stale hardcoded set. Listing needs no
// key (the catalog is public); running a free model still needs the OpenRouter key. Throttled — the
// catalog changes slowly.
static void refreshOpenRouterModels(AppState& st, bool force) {
    static double last = -1e9;
    double now = ImGui::GetTime();
    if (!force && (now - last) < 60.0) return;
    last = now;
    nlohmann::json p = {{"provider", "openai"},
                        {"base_url", "https://openrouter.ai/api/v1"},
                        {"api_key", st.settings.cloudKey}};
    st.rpc.call("agent_list_models", p, [&st](const core::RpcResult& r) {
        ProviderModels pm;
        pm.checked = true;
        if (r.ok && r.result.is_object()) {
            pm.ok = r.result.value("ok", false);
            if (r.result.contains("models") && r.result["models"].is_array())
                for (const auto& m : r.result["models"])
                    if (m.is_string()) pm.models.push_back(m.get<std::string>());
            if (r.result.contains("free") && r.result["free"].is_array())
                for (const auto& m : r.result["free"])
                    if (m.is_string()) pm.free.push_back(m.get<std::string>());
        } else {
            pm.error = r.err.empty() ? "core unavailable" : r.err;
        }
        st.providerModels[ui::PROV_CLOUDCOMPAT] = pm;
    }, 12000);
}

// The OpenRouter model list shown in the picker/manage UI: the curated presets PLUS every live
// free model the catalog probe found (deduped). `free`/`vision` flags drive the row status.
static std::vector<OpenRouterModel> openRouterModels(AppState& st) {
    refreshOpenRouterModels(st, false);
    std::vector<OpenRouterModel> out;
    std::set<std::string> seen;
    for (const auto& p : ui::cloudPresets()) {
        if (std::strcmp(ui::cloudServiceKey(p.baseUrl), "openrouter") != 0) continue;
        out.push_back({p.id, p.label, p.vision,
                       p.id.find(":free") != std::string::npos});
        seen.insert(p.id);
    }
    for (const auto& id : st.providerModels[ui::PROV_CLOUDCOMPAT].free)
        if (seen.insert(id).second) out.push_back({id, id, false, true});
    return out;
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
        default: return s.apiKey;  // local OpenAI-compatible optional key
    }
}

// Availability helpers (defined with the model-dropdown section below; forward-declared so the
// "Manage models" panel inside drawAiSettingsSection can call them).
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
static nlohmann::json jsonOptObj(const nlohmann::json& obj, const char* key) {
    if (!obj.is_object()) return nlohmann::json::object();
    auto it = obj.find(key);
    if (it == obj.end() || !it->is_object()) return nlohmann::json::object();
    return *it;
}

// Shared AI settings, split by section so Settings can glass-group them.
// ProviderModel = provider + model + auth/status; Generation = effort/tokens/window;
// Privacy = offline + thoughts; ManageModels = chat-picker visibility.
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
        // Set sensible per-provider defaults on switch.
        if (s.provider == ui::PROV_GEMINI && (!s.model[0] || std::strstr(s.model, "gpt") ||
                                              std::strstr(s.model, "claude")))
            std::strncpy(s.model, "gemini-2.5-pro", sizeof(s.model) - 1);
        else if (s.provider == ui::PROV_ANTHROPIC)
            std::strncpy(s.model, "claude-opus-4-8", sizeof(s.model) - 1);
        else if (s.provider == ui::PROV_OPENAI)
            std::strncpy(s.model, "gpt-5.6-sol", sizeof(s.model) - 1);
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
            std::strncpy(s.model, "kimi-k3", sizeof(s.model) - 1);
            s.vision = true;
        }
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
    } else if (cloud) {
        // Known-good model list for this provider, plus the current value if it's custom.
        std::vector<const char*> ids, labels;
        for (const auto& m : ui::builtinModels())
            if (m.provider == s.provider) { ids.push_back(m.id.c_str()); labels.push_back(m.label.c_str()); }
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
        // Cloud OpenAI-compatible: needs BOTH an endpoint (set by the preset, editable) and a key.
        // OpenRouter is first-class — key status names the active service. Moonshot uses its own
        // keys.moonshot buffer (via providerKeyBuf) so generic cloud_compat keys are preserved.
        const char* svc = ui::cloudServiceKey(s.baseUrl);
        const bool isOR = (std::strcmp(svc, "openrouter") == 0);
        const bool isMS = (std::strcmp(svc, "moonshot") == 0);
        ImGui::TextUnformatted(isOR ? "OpenRouter endpoint"
                                    : (isMS ? "Moonshot endpoint" : "Endpoint (base URL)"));
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##cloudbase", s.baseUrl, sizeof(s.baseUrl))) changed = true;
        ImGui::TextUnformatted(isOR ? "OpenRouter API key"
                                    : (isMS ? "Moonshot (Kimi) API key" : "API key"));
        char* key = providerKeyBuf(s, s.provider);
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##provkey", key, 256, ImGuiInputTextFlags_Password)) changed = true;
        if (ImGui::Checkbox("Model supports vision (send screenshots)", &s.vision)) changed = true;
        bool haveKey = key[0] != '\0';
        ImGui::PushStyleColor(ImGuiCol_Text, haveKey ? ui::Palette::toU32(ui::palette.statusOk)
                                                     : ui::Palette::toU32(ui::palette.statusWarn));
        if (isOR)
            ImGui::TextUnformatted(haveKey ? "OpenRouter key set — models available (blocked in Offline mode)"
                                           : "no OpenRouter key — add one at openrouter.ai/keys");
        else if (isMS)
            ImGui::TextUnformatted(haveKey
                ? "Moonshot key set — Kimi available (blocked in Offline mode)"
                : "no Moonshot key — add one at platform.moonshot.ai");
        else
            ImGui::TextUnformatted(haveKey ? "key set — cloud API available (blocked in Offline mode)"
                                           : "no API key — add your OpenRouter / DashScope / DeepSeek key");
        ImGui::PopStyleColor();
        // Always surface the Moonshot key slot even when another cloud service is active, so
        // credit can be parked without switching the default provider.
        if (!isMS) {
            ImGui::Dummy(ImVec2(0, 6 * S));
            ImGui::TextUnformatted("Moonshot (Kimi) API key (dormant)");
            ImGui::SetNextItemWidth(-1);
            if (ImGui::InputText("##moonshotkey", s.moonshotKey, sizeof(s.moonshotKey),
                                 ImGuiInputTextFlags_Password))
                changed = true;
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
            ImGui::TextUnformatted(s.moonshotKey[0]
                ? "key stored \xe2\x80\x94 Moonshot is not the active provider"
                : "optional \xe2\x80\x94 store a key without switching providers");
            ImGui::PopStyleColor();
        }
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
            double now = ImGui::GetTime();
            if (!force && (now - s_xaiLastPoll) < 1.5) return;
            s_xaiLastPoll = now;
            st.rpc.call("subscription_status", nlohmann::json::object(),
                        [](const core::RpcResult& r) {
                if (!r.ok || !r.result.is_object()) return;
                auto providers = jsonOptObj(r.result, "providers");
                auto x = jsonOptObj(providers, "xai");
                s_xaiConnected = jsonOptBool(x, "connected", false);
                s_xaiEmail = jsonOptStr(x, "email");
                s_xaiLoginBusy = jsonOptBool(x, "login_in_progress", false);
                s_xaiBrowserOpened = jsonOptBool(x, "browser_opened", false);
                s_xaiUserCode = jsonOptStr(x, "user_code");
                s_xaiVerifyUrl = jsonOptStr(x, "verification_url");
                if (s_xaiVerifyUrl.empty())
                    s_xaiVerifyUrl = jsonOptStr(x, "verification_uri");
                if (!s_xaiUserCode.empty() &&
                    (s_xaiVerifyUrl.empty() ||
                     s_xaiVerifyUrl.find("user_code=") == std::string::npos)) {
                    const std::string base = s_xaiVerifyUrl.empty()
                        ? "https://accounts.x.ai/oauth2/device"
                        : s_xaiVerifyUrl;
                    const char sep =
                        (base.find('?') == std::string::npos) ? '?' : '&';
                    s_xaiVerifyUrl = base + sep + "user_code=" + s_xaiUserCode;
                }
                s_xaiStage = jsonOptStr(x, "login_stage");
                if (auto err = jsonOptStr(x, "last_error"); !err.empty())
                    s_xaiError = err;
                else if (s_xaiConnected)
                    s_xaiError.clear();
            });
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
#if defined(_WIN32)
                    std::system(("start \"\" \"" + fallbackUrl + "\"").c_str());
#elif defined(__APPLE__)
                    std::system(("open '" + fallbackUrl + "'").c_str());
#else
                    std::system(("xdg-open '" + fallbackUrl + "' >/dev/null 2>&1 &").c_str());
#endif
                }
            }
            pollXaiStatus(true);
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
                    if (!r.ok) {
                        s_xaiLoginBusy = false;
                        s_xaiError = r.err.empty() ? "login failed" : r.err;
                    } else if (r.result.is_object() && jsonOptBool(r.result, "ok", true) == false) {
                        s_xaiLoginBusy = false;
                        auto reason = jsonOptStr(r.result, "reason");
                        auto hint = jsonOptStr(r.result, "hint");
                        s_xaiError = reason.empty() ? "login failed" : reason;
                        if (!hint.empty()) s_xaiError += " — " + hint;
                    }
                    s_xaiLastPoll = -1e9;
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

        ImGui::Dummy(ImVec2(0, 4 * S));
        ImGui::TextUnformatted("API key (console.x.ai — optional if signed in)");
        char* key = providerKeyBuf(s, s.provider);
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##xaikey", key, 256, ImGuiInputTextFlags_Password)) changed = true;
        bool haveKey = key[0] != '\0';
        ImGui::PushStyleColor(ImGuiCol_Text,
            (haveKey || s_xaiConnected) ? ui::Palette::toU32(ui::palette.statusOk)
                                        : ui::Palette::toU32(ui::palette.statusWarn));
        if (haveKey)
            ImGui::TextUnformatted("API key set — uses provider 'xai' (blocked in Offline mode)");
        else if (s_xaiConnected)
            ImGui::TextUnformatted("OAuth connected — uses provider 'xai-sub' (blocked in Offline mode)");
        else
            ImGui::TextUnformatted("Sign in with SuperGrok or paste an API key from console.x.ai");
        ImGui::PopStyleColor();
        if (ImGui::Checkbox("Model supports vision (send screenshots)", &s.vision)) changed = true;
    } else if (s.provider == ui::PROV_CURSOR || s.provider == ui::PROV_KIMISUB) {
        // Subscription-only providers: OAuth credential lives in subscription_auth — no API key.
        if (s.provider == ui::PROV_KIMISUB) {
            dimText("Kimi Code subscription (OAuth via subscription_auth). Sign in under "
                    "Accounts; no Moonshot API key required for this path.");
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
            ImGui::TextUnformatted("Wire provider: kimi-sub \xc2\xb7 model: kimi-k3 (default)");
            ImGui::PopStyleColor();
        } else {
            dimText("Cursor subscription (OAuth via subscription_auth). Sign in under Accounts; "
                    "no API key field.");
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
            ImGui::TextUnformatted("Wire provider: cursor \xc2\xb7 model: composer-2.5");
            ImGui::PopStyleColor();
        }
        if (ImGui::Checkbox("Model supports vision (send screenshots)", &s.vision)) changed = true;
    } else if (cloud) {
        ImGui::TextUnformatted("API key");
        char* key = providerKeyBuf(s, s.provider);
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##provkey", key, 256, ImGuiInputTextFlags_Password)) changed = true;
        bool haveKey = key[0] != '\0';
        ImGui::PushStyleColor(ImGuiCol_Text, haveKey ? ui::Palette::toU32(ui::palette.statusOk)
                                                     : ui::Palette::toU32(ui::palette.statusWarn));
        ImGui::TextUnformatted(haveKey ? "key set — model available"
                                       : "no API key — add one to use this provider");
        ImGui::PopStyleColor();
    } else {
        ImGui::TextUnformatted("Base URL");
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##baseurl", s.baseUrl, sizeof(s.baseUrl))) {
            changed = true;
            refreshModelAvailability(st, true);  // re-probe the new endpoint
        }
        if (s.provider == ui::PROV_LOCAL) {
            ImGui::TextUnformatted("API key (optional)");
            ImGui::SetNextItemWidth(-1);
            if (ImGui::InputText("##localkey", s.apiKey, sizeof(s.apiKey),
                                 ImGuiInputTextFlags_Password)) changed = true;
        }
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
    // ---- Generation: reasoning effort + token budgets ----
    // Labels share the row with their widgets so scroll never orphans an InputInt.
    {
        const float labelW = 180.0f * S;
        const float fieldW = 160.0f * S;
        auto labeledField = [&](const char* label) {
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted(label);
            ImGui::SameLine(labelW);
            ImGui::SetNextItemWidth(fieldW);
        };

        const char* effLabels[8] = {};
        const char* effValues[8] = {};
        int nEff = ui::reasoningEffortOptions(s.provider, s.model, effLabels, effValues, 8);
        if (nEff > 0) {
            labeledField("Reasoning effort");
            int effIdx = 0;
            for (int i = 0; i < nEff; ++i)
                if (std::strcmp(s.reasoningEffort, effValues[i]) == 0) effIdx = i;
            if (ImGui::Combo("##effort", &effIdx, effLabels, nEff)) {
                std::strncpy(s.reasoningEffort, effValues[effIdx], sizeof(s.reasoningEffort) - 1);
                changed = true;
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("How much hidden reasoning the model spends before answering.\n"
                                  "Higher = better planning on hard parts, slower + more tokens.");
            ImGui::Dummy(ImVec2(0, 4 * S));
        }

        // Max response tokens (0 = auto). Keep label on the same row as the InputInt so a scroll
        // boundary can never leave a bare "0" field without its caption (Settings AI tab bug).
        labeledField("Max response tokens");
        int mt = s.maxTokens;  // 0 = auto
        if (ImGui::InputInt("##maxtokens", &mt, 1024, 4096)) {
            if (mt < 0) mt = 0;
            if (mt > 0 && mt < 512) mt = 512;
            if (mt > 200000) mt = 200000;
            s.maxTokens = mt;
            changed = true;
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Output token cap per model call. 0 = automatic.\n"
                              "Raise this if the agent reports \"budget exhausted\" or \"no JSON ops\"\n"
                              "— a reasoning model spent its budget thinking before emitting the build.");
        ImGui::SameLine(0.0f, 8.0f * S);
        ImGui::AlignTextToFramePadding();
        if (mt == 0) dimText("0 = automatic");

        ImGui::Dummy(ImVec2(0, 4 * S));
        labeledField("Model context window");
        int cw = s.contextWindow;  // 0 = auto
        if (ImGui::InputInt("##ctxwindow", &cw, 1024, 8192)) {
            if (cw < 0) cw = 0;
            if (cw > 0 && cw < 2048) cw = 2048;
            if (cw > 2000000) cw = 2000000;
            s.contextWindow = cw;
            changed = true;
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("The model's total token context window. 0 = automatic\n"
                              "(cloud large; local 8192). Set your LOCAL server's real window so the\n"
                              "agent trims the prompt to fit instead of overrunning it and breaking the run.");
        ImGui::SameLine(0.0f, 8.0f * S);
        ImGui::AlignTextToFramePadding();
        if (cw == 0) dimText("0 = automatic");
    }

    };

    auto drawManageModels = [&]() {
        dimText("Uncheck a model to hide it from the composer's model dropdown. Only available "
                "models (keyed cloud / loaded local) appear there; this just trims that list.");
        ImGui::Dummy(ImVec2(0, 2 * S));
        const auto& pal2 = ui::palette;
        int rowuid = 0;
        // One toggle row: [x] Label .................... status
        auto visRow = [&](const std::string& id, const std::string& label,
                          const char* status, ImU32 statusCol) {
            ImGui::PushID(rowuid++);
            bool show = (s.hiddenModels.count(id) == 0);
            if (ImGui::Checkbox("##vis", &show)) {
                if (show) s.hiddenModels.erase(id);
                else      s.hiddenModels.insert(id);
                changed = true;
            }
            ImGui::SameLine();
            ImGui::TextUnformatted(label.c_str());
            if (status && status[0]) {
                float tw = ImGui::CalcTextSize(status).x;
                ImGui::SameLine(ImGui::GetContentRegionMax().x - tw);
                ImGui::PushStyleColor(ImGuiCol_Text, statusCol);
                ImGui::TextUnformatted(status);
                ImGui::PopStyleColor();
            }
            ImGui::PopID();
        };
        const ImU32 okCol = ui::Palette::toU32(pal2.statusOk);
        const ImU32 dimCol = ui::Palette::toU32(pal2.textDim);
        const ImU32 warnCol = ui::Palette::toU32(pal2.statusWarn);
        dimText("Each provider is a category: the checkbox bulk-toggles the whole provider on/off; "
                "expand it to hide individual models. Off = hidden from the composer's model picker.");
        ImGui::Dummy(ImVec2(0, 2 * S));

        // One collapsible category header: [x] bulk-on/off checkbox + an expander showing the model
        // count. Returns whether the body (the model rows) should be drawn.
        auto catHeader = [&](const std::string& key, int n) -> bool {
            bool enabled = st.settings.disabledProviders.count(key) == 0;
            ImGui::PushID(("cat_" + key).c_str());
            if (ImGui::Checkbox("##en", &enabled)) {
                if (enabled) st.settings.disabledProviders.erase(key);
                else         st.settings.disabledProviders.insert(key);
                changed = true;
            }
            ImGui::PopID();
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip(enabled ? "Turn this whole provider OFF (hide all its models)"
                                          : "Turn this provider back ON");
            ImGui::SameLine();
            char hdr[128];
            std::snprintf(hdr, sizeof(hdr), "%s  (%d)%s###hdr_%s", ui::categoryLabel(key), n,
                          enabled ? "" : "   - off", key.c_str());
            return ImGui::CollapsingHeader(hdr);
        };
        auto catEnabled = [&](const std::string& key) {
            return st.settings.disabledProviders.count(key) == 0;
        };

        // Built-in cloud providers (Gemini / Anthropic / OpenAI / xAI / Cursor / Kimi Code).
        for (int pv : {ui::PROV_GEMINI, ui::PROV_ANTHROPIC, ui::PROV_OPENAI, ui::PROV_XAI,
                       ui::PROV_CURSOR, ui::PROV_KIMISUB}) {
            std::string key = ui::providerCatKey(pv);
            int n = 0;
            for (const auto& m : ui::builtinModels()) if (m.provider == pv) ++n;
            if (catHeader(key, n)) {
                bool en = catEnabled(key);
                if (!en) ImGui::BeginDisabled();
                ImGui::Indent(14 * S);
                bool keyed = providerUsable(st, pv);
                for (const auto& m : ui::builtinModels()) {
                    if (m.provider != pv) continue;
                    visRow(m.id, m.label, keyed ? "available" : "needs key", keyed ? okCol : warnCol);
                }
                ImGui::Unindent(14 * S);
                if (!en) ImGui::EndDisabled();
            }
        }
        // Cloud services as their own categories. OpenRouter is discovered LIVE so its category
        // lists every currently-free model (plus curated paid presets); Alibaba/DeepSeek/Moonshot
        // use curated presets. Moonshot is listed here for visibility even when not the active
        // provider (models appear in the chat picker only once keys.moonshot is set).
        for (const char* svc : {"openrouter", "alibaba", "deepseek", "moonshot"}) {
            std::vector<OpenRouterModel> rows;
            if (std::strcmp(svc, "openrouter") == 0) {
                rows = openRouterModels(st);
            } else {
                for (const auto& p : ui::cloudPresets())
                    if (std::strcmp(ui::cloudServiceKey(p.baseUrl), svc) == 0)
                        rows.push_back({p.id, p.label, p.vision, false});
            }
            if (rows.empty()) continue;
            if (catHeader(svc, (int)rows.size())) {
                bool en = catEnabled(svc);
                if (!en) ImGui::BeginDisabled();
                ImGui::Indent(14 * S);
                if (std::strcmp(svc, "openrouter") == 0)
                    dimText("Free models discovered live from OpenRouter (cost $0; still need a key "
                            "to run). Paid presets are listed too.");
                else if (std::strcmp(svc, "moonshot") == 0)
                    dimText("Moonshot (Kimi) is configured but not the active provider by default. "
                            "Set keys.moonshot below / in Providers; pick a Kimi model when you want it.");
                for (const auto& r : rows)
                    visRow(r.id, r.label, r.free ? "free" : (r.vision ? "vision" : "paid"),
                           r.free ? okCol : dimCol);
                ImGui::Unindent(14 * S);
                if (!en) ImGui::EndDisabled();
            }
        }
        // Local discovered models (deduped across the local provider pair).
        {
            const ProviderModels& lpm = st.providerModels[ui::PROV_LOCAL];
            std::vector<std::string> ids;
            std::set<std::string> seen;
            for (const auto& id : lpm.models) if (seen.insert(id).second) ids.push_back(id);
            if (catHeader("local", (int)ids.size())) {
                bool en = catEnabled("local");
                if (!en) ImGui::BeginDisabled();
                ImGui::Indent(14 * S);
                if (ids.empty()) {
                    dimText("No local models discovered - start your local model server.");
                } else {
                    for (const auto& id : ids) {
                        bool loaded = localModelLoaded(lpm, id);
                        visRow(id, id,
                               lpm.loadedKnown ? (loaded ? "loaded" : "not loaded") : "available",
                               loaded ? okCol : dimCol);
                    }
                }
                ImGui::Unindent(14 * S);
                if (!en) ImGui::EndDisabled();
            }
        }
    };

    auto drawPrivacy = [&]() {
    // ---- privacy / offline ----
    // Keep labels short enough for narrow columns / high uiScale (CalcTextSize fit).
    if (ImGui::Checkbox("Offline mode (local only)", &s.offlineMode))
        changed = true;
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Refuses any cloud provider (Gemini, Anthropic, OpenAI, xAI, OpenRouter).\n"
                          "Only a local loopback endpoint is allowed. Enforced before every run.");
    // Session-local (not persisted): matches the Copilot overlay Permissions tab.
    ImGui::Checkbox("Show assistant thoughts", &gShowThinking);
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Show the AI's reasoning steps inline in the conversation.");
    dimText("Telemetry is not collected \xe2\x80\x94 Chudware does not phone home.");

    };

    switch (section) {
    case AiSettingsSection::ProviderModel:
        drawProviderModel();
        break;
    case AiSettingsSection::Generation:
        drawGeneration();
        break;
    case AiSettingsSection::Privacy:
        drawPrivacy();
        break;
    case AiSettingsSection::ManageModels:
        drawManageModels();
        break;
    case AiSettingsSection::All:
        drawProviderModel();
        ImGui::Dummy(ImVec2(0, 8 * S));
        ImGui::SeparatorText("Generation");
        drawGeneration();
        ImGui::Dummy(ImVec2(0, 6 * S));
        if (ImGui::CollapsingHeader("Manage models (chat picker)"))
            drawManageModels();
        ImGui::Dummy(ImVec2(0, 6 * S));
        ImGui::SeparatorText("Privacy");
        drawPrivacy();
        break;
    }
    return changed;
}

// Full form: Provider + Generation + (Manage models unless slim) + Privacy.
bool drawProviderModelPicker(AppState& st, bool busy, bool slim) {
    if (slim) {
        // Compact: provider/model/auth + generation + privacy (no manage-models dump).
        bool changed = false;
        changed |= drawAiSettingsSection(st, busy, AiSettingsSection::ProviderModel);
        ImGui::Dummy(ImVec2(0, 6 * ui::uiScale));
        ImGui::SeparatorText("Generation");
        changed |= drawAiSettingsSection(st, busy, AiSettingsSection::Generation);
        ImGui::Dummy(ImVec2(0, 6 * ui::uiScale));
        ImGui::SeparatorText("Privacy");
        changed |= drawAiSettingsSection(st, busy, AiSettingsSection::Privacy);
        return changed;
    }
    return drawAiSettingsSection(st, busy, AiSettingsSection::All);
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
    try {
        nlohmann::json j = nlohmann::json::object();
        {
            std::ifstream in(configPath());
            if (in) {
                nlohmann::json old;
                in >> old;
                if (old.is_object()) j = std::move(old);
            }
        }
        if (!j.contains("agent") || !j["agent"].is_object())
            j["agent"] = nlohmann::json::object();
        j["agent"]["max_subagent_depth"] = depth;
        const std::string path = configPath();
        const std::string tmp = path + ".tmp";
        std::ofstream(tmp) << j.dump(2);
        std::filesystem::rename(tmp, path);
    } catch (...) {}
}

void drawAISettingsBody(AppState& st, bool busy) {
    Settings& s = st.settings;
    const float S = ui::uiScale;
    const ui::AdaptiveSurface& surf = chatSurf();
    bool changed = false;
    (void)busy;
    if (ImGui::IsKeyPressed(ImGuiKey_Escape, false) && !ImGui::IsAnyItemActive()) {
        st.showAISettingsOverlay = false;
        st.showSessionsPicker = false;
        return;
    }

    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(ui::metrics::U(1.0f), ui::metrics::U(0.85f)));
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(ui::metrics::U(1.5f), ui::metrics::U(0.75f)));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);

    ImGui::PushFont(st.fonts.medium);
    ImGui::TextUnformatted("Quick settings");
    ImGui::PopFont();
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
    ImGui::TextWrapped("Session knobs. Full providers, roles, and prompts live in Settings.");
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, ui::metrics::U(1.0f)));

    ImGui::BeginChild("##ai_ov_body", ImVec2(0, 0), ImGuiChildFlags_None);
    {
        static const char* kPrefLabels[] = {"Off", "Option", "Encourage", "Require"};
        auto prefCombo = [&](const char* label, const char* id, int* pref) {
            int v = (*pref < 0 || *pref > 3) ? 0 : *pref;
            ImGui::TextUnformatted(label);
            ImGui::SetNextItemWidth(-1);
            if (ImGui::Combo(id, &v, kPrefLabels, 4)) {
                *pref = v;
                if (pref == &s.planPreference) s.planMode = (v != 0);
                changed = true;
            }
        };
        prefCombo("Plan preference", "##q_plan_pref", &s.planPreference);
        prefCombo("Subagent preference", "##q_sub_pref", &s.subagentPreference);
        prefCombo("Advisor preference", "##q_adv_pref", &s.advisorPreference);
        if (ImGui::Checkbox("Proactive advisor", &s.proactiveAdvisor)) changed = true;
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip(
                "Opt-in mid-run guidance on failures. Cheap notes by default; "
                "rare short advisor call only on severe streaks.");

        ImGui::Dummy(ImVec2(0, ui::metrics::U(1.0f)));
        ImGui::Separator();
        ImGui::Dummy(ImVec2(0, ui::metrics::U(0.5f)));

        if (ImGui::Checkbox("Vision (send screenshots)", &s.vision)) changed = true;
        if (ImGui::Checkbox("Offline mode (local only)", &s.offlineMode)) changed = true;
        ImGui::Checkbox("Show assistant thoughts", &gShowThinking);

        ImGui::Dummy(ImVec2(0, ui::metrics::U(0.75f)));
        ImGui::TextUnformatted("Refine-pass cap");
        ImGui::SetNextItemWidth(-1);
        int iters = s.maxIters;
        if (ImGui::SliderInt("##q_miters", &iters, 0, 64, iters == 0 ? "automatic" : "%d")) {
            s.maxIters = (iters < 0) ? 0 : iters;
            changed = true;
        }

        static int s_maxDepth = -1;
        if (s_maxDepth < 0) s_maxDepth = loadMaxSubagentDepth();
        ImGui::TextUnformatted("Max subagent nesting");
        ImGui::SetNextItemWidth(-1);
        int depth = s_maxDepth;
        if (ImGui::SliderInt("##q_depth", &depth, 0, 4, "%d")) {
            if (depth < 0) depth = 0;
            if (depth > 4) depth = 4;
            s_maxDepth = depth;
            saveMaxSubagentDepth(depth);
        }

        ImGui::Dummy(ImVec2(0, ui::metrics::U(0.75f)));
        if (ImGui::Checkbox("Project memory", &s.memoryEnabled)) changed = true;
        if (s.memoryEnabled) {
            ImGui::TextUnformatted("Memory token budget");
            ImGui::SetNextItemWidth(-1);
            if (ImGui::SliderInt("##q_membudget", &s.memoryTokenBudget, 100, 2000, "%d"))
                changed = true;
        }

        ImGui::Dummy(ImVec2(0, ui::metrics::U(1.25f)));
        ImGui::Separator();
        ImGui::Dummy(ImVec2(0, ui::metrics::U(0.5f)));
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        ImGui::TextUnformatted("Open full Settings");
        ImGui::PopStyleColor();
        if (ImGui::Button("AI##q_open_ai", ImVec2(-1, 0))) {
            st.openSettings = true;
            st.openSettingsTab = 2;
            st.showAISettingsOverlay = false;
        }
        if (ImGui::Button("Providers##q_open_prov", ImVec2(-1, 0))) {
            st.openSettings = true;
            st.openSettingsTab = 3;
            st.showAISettingsOverlay = false;
        }
        if (ImGui::Button("Prompts##q_open_prompts", ImVec2(-1, 0))) {
            st.openSettings = true;
            st.openSettingsTab = 2;  // AI (Prompts absorbed)
            st.showAISettingsOverlay = false;
        }
    }
    ImGui::EndChild();

    if (changed) saveSettings(s);

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

// Has the user hidden this model from the chat picker (Settings → AI → Manage models)?
static bool modelHidden(AppState& st, const std::string& id) {
    return st.settings.hiddenModels.count(id) != 0;
}

// File-static cache of subscription_status "connected" flags so the model picker can show
// signed-in providers (xAI / Claude / Cursor / Codex / Kimi) without requiring an API key.
// On web (__EMSCRIPTEN__), also merges GET /api/providers host-shared key/subscription
// presence so the picker matches the daemon's signed-in accounts (WASM IDBFS has no real keys).
namespace {
struct SubConnectedCache {
    bool xai = false;
    bool claude = false;
    bool cursor = false;
    bool codex = false;
    bool kimi = false;
    // Host-shared API-key providers (web only; filled from GET /api/providers).
    bool hostGemini = false;
    bool hostAnthropic = false;
    bool hostOpenai = false;
    bool hostCloud = false;     // openrouter / cloud_compat
    bool hostMoonshot = false;
    bool hostXaiKey = false;    // xAI API key (distinct from xai-sub OAuth)
    bool hostFetched = false;   // web: /api/providers completed at least once
    double lastPoll = -1e9;
    bool inFlight = false;
};
static SubConnectedCache s_subConn;
#ifdef __EMSCRIPTEN__
// File-scope so emscripten_fetch callbacks (no-capture lambdas) can reset on retry.
static bool s_hostProvidersFetchStarted = false;
static int s_hostProvidersFetchFails = 0;
#endif

#ifdef __EMSCRIPTEN__
// One-shot async GET /api/providers — names only (no secrets). Marks which host providers
// are signed in so providerUsable() can show them without local key buffers.
static void wasmKickHostProvidersFetch() {
    if (s_subConn.hostFetched || s_hostProvidersFetchStarted) return;
    s_hostProvidersFetchStarted = true;
    emscripten_fetch_attr_t attr;
    emscripten_fetch_attr_init(&attr);
    std::strcpy(attr.requestMethod, "GET");
    attr.attributes = EMSCRIPTEN_FETCH_LOAD_TO_MEMORY;
    attr.onsuccess = [](emscripten_fetch_t* f) {
        try {
            if (f->status >= 200 && f->status < 300 && f->data && f->numBytes > 0) {
                auto j = nlohmann::json::parse(f->data, f->data + f->numBytes);
                if (j.is_object()) {
                    // Reset host bits then apply.
                    s_subConn.hostGemini = s_subConn.hostAnthropic = s_subConn.hostOpenai = false;
                    s_subConn.hostCloud = s_subConn.hostMoonshot = s_subConn.hostXaiKey = false;
                    auto applySub = [](const std::string& id) {
                        if (id == "xai" || id == "xai-sub") s_subConn.xai = true;
                        else if (id == "claude" || id == "anthropic-sub") s_subConn.claude = true;
                        else if (id == "cursor" || id == "cursor-sub") s_subConn.cursor = true;
                        else if (id == "codex" || id == "codex-sub") s_subConn.codex = true;
                        else if (id == "kimi" || id == "kimi-sub") s_subConn.kimi = true;
                    };
                    auto applyKey = [](const std::string& id) {
                        if (id == "gemini") s_subConn.hostGemini = true;
                        else if (id == "anthropic") s_subConn.hostAnthropic = true;
                        else if (id == "openai") s_subConn.hostOpenai = true;
                        else if (id == "openrouter" || id == "cloud" || id == "cloud_compat")
                            s_subConn.hostCloud = true;
                        else if (id == "moonshot") s_subConn.hostMoonshot = true;
                        else if (id == "xai") s_subConn.hostXaiKey = true;
                    };
                    if (j.contains("providers") && j["providers"].is_array()) {
                        for (const auto& p : j["providers"]) {
                            if (!p.is_object()) continue;
                            std::string id = p.value("id", p.value("name", std::string()));
                            if (id.empty()) continue;
                            std::string auth = p.value("auth", std::string());
                            const bool isSub = (auth == "subscription") ||
                                (auth.empty() && id.find("-sub") != std::string::npos);
                            if (isSub) applySub(id);
                            else applyKey(id);
                        }
                    }
                    s_subConn.hostFetched = true;
                }
            }
        } catch (...) {
        }
        emscripten_fetch_close(f);
    };
    attr.onerror = [](emscripten_fetch_t* f) {
        // Allow a few retries on transient failure; then stop.
        if (++s_hostProvidersFetchFails < 5) s_hostProvidersFetchStarted = false;
        emscripten_fetch_close(f);
    };
    emscripten_fetch(&attr, "/api/providers");
}
#endif

static void pollSubConnected(AppState& st) {
    const double now = ImGui::GetTime();
#ifdef __EMSCRIPTEN__
    wasmKickHostProvidersFetch();
#endif
    if (s_subConn.inFlight || (now - s_subConn.lastPoll) < 2.0) return;
    s_subConn.lastPoll = now;
    s_subConn.inFlight = true;
    st.rpc.call("subscription_status", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_subConn.inFlight = false;
        if (!r.ok || !r.result.is_object()) return;
        auto providers = jsonOptObj(r.result, "providers");
        auto connected = [](const nlohmann::json& providers, const char* key) {
            auto p = jsonOptObj(providers, key);
            return jsonOptBool(p, "connected", false);
        };
        // OR with host-fetched bits so a slow/failed RPC doesn't clear web presence.
        s_subConn.xai    = connected(providers, "xai")    || s_subConn.xai;
        s_subConn.claude = connected(providers, "claude") || s_subConn.claude;
        s_subConn.cursor = connected(providers, "cursor") || s_subConn.cursor;
        s_subConn.codex  = connected(providers, "codex")  || s_subConn.codex;
        s_subConn.kimi   = connected(providers, "kimi")   || s_subConn.kimi;
    });
}
}  // namespace

// Does this provider have credentials / a reachable endpoint / a signed-in subscription,
// so its models can be selected? Public (copilot.h) so Settings role pickers share it.
bool providerUsable(AppState& st, int provider) {
    pollSubConnected(st);
    switch (provider) {
        case ui::PROV_GEMINI:
            return st.settings.geminiKey[0] != 0 || s_subConn.hostGemini;
        case ui::PROV_ANTHROPIC:
            return st.settings.anthropicKey[0] != 0 || s_subConn.claude || s_subConn.hostAnthropic;
        case ui::PROV_OPENAI:
            return st.settings.openaiKey[0] != 0 || s_subConn.codex || s_subConn.hostOpenai;
        case ui::PROV_CLOUDCOMPAT:
            return st.settings.cloudKey[0] != 0 || st.settings.moonshotKey[0] != 0 ||
                   s_subConn.hostCloud || s_subConn.hostMoonshot;
        case ui::PROV_XAI:
            return st.settings.xaiKey[0] != 0 || s_subConn.xai || s_subConn.hostXaiKey;
        case ui::PROV_LOCAL:       return st.providerModels[ui::PROV_LOCAL].ok;
        case ui::PROV_LLAMACPP:    return st.providerModels[ui::PROV_LLAMACPP].ok;
        case ui::PROV_CURSOR:      return s_subConn.cursor;
        case ui::PROV_KIMISUB:     return s_subConn.kimi;
    }
    return false;
}

bool cloudCompatKeyPresent(AppState& st) {
    pollSubConnected(st);
    return st.settings.cloudKey[0] != 0 || s_subConn.hostCloud;
}

bool moonshotKeyPresent(AppState& st) {
    pollSubConnected(st);
    return st.settings.moonshotKey[0] != 0 || s_subConn.hostMoonshot;
}

// ============================ OMP bridge model catalog + session cost (S-UI-PROV) ===
// Static OMP catalog (capabilities + $/Mtok cost) via the libchudware `agent_models_list` C-ABI
// (bridge `models_list`), and the per-session cost strip via `agent_cost_summary` (a pure
// calculator — the UI supplies the session's token events, the bridge prices them). Both
// degrade permanently on unknown_method (sibling slice absent / older core) and quietly keep
// the last good data on soft failures.

namespace {

std::vector<ui::BridgeModelMeta> s_bridgeModels;
bool s_bridgeCatalogAvailable = true;  // false after unknown_method — stop polling, no sublabels
bool s_bridgeCatalogInFlight = false;
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
        const std::string c = r.result.value("code", std::string());
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

// "$3" / "$1.5" / "$18.75" — OMP formatCostPair trims to meaningful digits (model-browser.ts:271).
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
// Deliberately conservative: only an unambiguous 8-digit YYYYMMDD run or a hyphenated
// YYYY-MM-DD run is accepted (both self-contained — no external "year context" needed, so
// the "-MMDD suffix + surrounding year" case never arises in practice for the ids OMP emits;
// skip rather than guess when nothing unambiguous is found). Sets outIso="" / outSort=0 on
// failure.
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

}  // namespace

void refreshBridgeModelCatalog(AppState& st, bool force) {
    if (!s_bridgeCatalogAvailable || s_bridgeCatalogInFlight) return;
    const double now = ImGui::GetTime();
    // Catalog changes slowly (only on a bridge/OMP update) — 5-min TTL like the usage polls.
    if (!force && (now - s_bridgeCatalogLastFetch) < 300.0) return;
    s_bridgeCatalogLastFetch = now;
    s_bridgeCatalogInFlight = true;
    st.rpc.call("agent_models_list", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_bridgeCatalogInFlight = false;
        if (bridgeRpcAbsent(r)) {
            s_bridgeCatalogAvailable = false;
            s_bridgeModels.clear();
            return;
        }
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", true)) return;  // soft fail
        const auto it = r.result.find("models");
        if (it == r.result.end() || !it->is_array()) return;
        std::vector<ui::BridgeModelMeta> rows;
        for (const auto& m : *it) {
            if (!m.is_object()) continue;
            ui::BridgeModelMeta b;
            b.id = m.value("id", std::string());
            if (b.id.empty()) continue;
            b.name = m.value("name", b.id);
            b.provider = m.value("provider", std::string());
            b.vision = m.value("vision", false);
            b.tools = m.value("supportsTools", false);
            b.reasoning = m.value("reasoning", false);
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
        s_bridgeModels = std::move(rows);
    }, 15000);
}

const std::vector<ui::BridgeModelMeta>& bridgeModelCatalog() { return s_bridgeModels; }

const ui::BridgeModelMeta* bridgeModelMeta(const std::string& providerId,
                                               const std::string& modelId) {
    for (const auto& m : s_bridgeModels) {
        if (m.id != modelId) continue;
        if (providerId.empty() || m.provider == providerId) return &m;
    }
    return nullptr;
}

const ui::BridgeModelMeta* bridgeModelMetaForCad(int cadProvider, const std::string& modelId,
                                                     const char* cloudBaseUrl) {
    // CAD ProviderIndex -> OMP catalog provider id (the bridge-routed subset; local endpoints
    // have no catalog rows and simply get no sublabel).
    std::string prov;
    switch (cadProvider) {
        case ui::PROV_ANTHROPIC: prov = "anthropic"; break;
        case ui::PROV_KIMISUB:   prov = "kimi-code"; break;
        case ui::PROV_OPENAI:    prov = "openai"; break;
        case ui::PROV_XAI:       prov = "xai"; break;
        case ui::PROV_GEMINI:    prov = "google"; break;
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
    // Fallback: bare id match (covers cloud-slot services whose OMP id differs from ours).
    return bridgeModelMeta("", modelId);
}

std::string bridgeModelSublabel(const ui::BridgeModelMeta& m) {
    // "ctx 200k \xc2\xb7 $3/$15 \xc2\xb7 tools vision" — OMP model-browser.ts row format.
    std::string out;
    if (m.contextWindow > 0) {
        char buf[24];
        if (m.contextWindow >= 1000000)
            std::snprintf(buf, sizeof(buf), "ctx %dM", m.contextWindow / 1000000);
        else
            std::snprintf(buf, sizeof(buf), "ctx %dk", m.contextWindow / 1000);
        out = buf;
    }
    if (m.hasCost) {
        if (!out.empty()) out += " \xc2\xb7 ";
        if (m.costIn == 0.0 && m.costOut == 0.0)
            out += "free";
        else
            out += fmtUsd(m.costIn) + "/" + fmtUsd(m.costOut);
    }
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
    // Tooltip line: full precision + cache pricing (OMP keeps cache cost off the row).
    std::string out;
    if (m.hasCost) {
        out = "in " + fmtUsd(m.costIn) + "/M \xc2\xb7 out " + fmtUsd(m.costOut) + "/M";
        if (m.costCacheRead > 0.0 || m.costCacheWrite > 0.0)
            out += " \xc2\xb7 cache " + fmtUsd(m.costCacheRead) + "/" + fmtUsd(m.costCacheWrite);
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
    int tokensIn = 0, tokensOut = 0;
};
double s_costUsd = 0;
int s_costTin = 0, s_costTout = 0;
std::vector<CostModelRow> s_costRows;
bool s_costAvailable = true;   // false after unknown_method
bool s_costInFlight = false;
bool s_costHasData = false;
double s_costLastFetch = -1e9;
// Fingerprint of the events array last priced (turn count + total tokens) — events only grow
// within a conversation, and a conversation switch changes the totals, so this refires exactly
// when the visible session's completed turns change (plus a 30s refresh while open).
long long s_costFingerprint = -1;

}  // namespace

void drawSessionCostStrip(AppState& st) {
    if (!s_costAvailable) return;
    const float sc = ui::uiScale;

    // Rebuild the session's token events from completed assistant turns: ChatMsg.model is the
    // model id at run start, and rowKind-0 activity rows carry per-stage tokens (the same sum
    // the chat footer shows, chat_log.cpp). agent_cost_summary is a pure calculator, so the
    // full event list is re-sent each poll — no accumulation state to drift.
    nlohmann::json events = nlohmann::json::array();
    long long fp = 0;
    for (const auto& m : st.chat) {
        if (m.fromUser || m.running || m.model.empty()) continue;
        int tin = 0, tout = 0;
        for (const auto& a : m.activity) {
            if (a.rowKind != 0) continue;
            tin += a.tokensIn;
            tout += a.tokensOut;
        }
        if (tin <= 0 && tout <= 0) continue;
        // The bridge prices by (provider, model); recover the OMP provider id from the catalog.
        const ui::BridgeModelMeta* meta = bridgeModelMeta("", m.model);
        if (!meta || meta->provider.empty()) continue;
        fp += 1 + tin + tout;
        events.push_back({{"provider", meta->provider},
                          {"model", m.model},
                          {"input", tin},
                          {"output", tout}});
    }
    const double now = ImGui::GetTime();
    if (!s_costInFlight && (fp != s_costFingerprint || (now - s_costLastFetch) > 30.0)) {
        s_costFingerprint = fp;
        s_costLastFetch = now;
        s_costInFlight = true;
        st.rpc.call("agent_cost_summary", nlohmann::json{{"events", events}},
                    [](const core::RpcResult& r) {
            s_costInFlight = false;
            if (bridgeRpcAbsent(r)) {
                s_costAvailable = false;
                s_costHasData = false;
                return;
            }
            if (!r.ok || !r.result.is_object() || !r.result.value("ok", true)) return;
            const auto total = r.result.find("total");
            if (total != r.result.end() && total->is_object()) {
                s_costUsd = total->value("usd", 0.0);
                const auto tok = total->find("tokens");
                if (tok != total->end() && tok->is_object()) {
                    s_costTin = tok->value("input", 0);
                    s_costTout = tok->value("output", 0);
                }
            }
            s_costRows.clear();
            const auto pm = r.result.find("perModel");
            if (pm != r.result.end() && pm->is_array()) {
                for (const auto& row : *pm) {
                    if (!row.is_object()) continue;
                    CostModelRow cr;
                    cr.provider = row.value("provider", std::string());
                    cr.model = row.value("model", std::string());
                    cr.usd = row.value("usd", 0.0);
                    const auto tok = row.find("tokens");
                    if (tok != row.end() && tok->is_object()) {
                        cr.tokensIn = tok->value("input", 0);
                        cr.tokensOut = tok->value("output", 0);
                    }
                    s_costRows.push_back(std::move(cr));
                }
            }
            s_costHasData = true;
        }, 15000);
    }
    if (!s_costHasData) return;

    // Compact one-liner: "Session $1.87 \xc2\xb7 142k tok". Click = per-model breakdown popover.
    char line[96];
    const long long tokTotal = (long long)s_costTin + (long long)s_costTout;
    char tokBuf[24];
    if (tokTotal >= 1000000)
        std::snprintf(tokBuf, sizeof(tokBuf), "%.1fM tok", tokTotal / 1.0e6);
    else
        std::snprintf(tokBuf, sizeof(tokBuf), "%lldk tok", tokTotal / 1000);
    std::snprintf(line, sizeof(line), "Session %s \xc2\xb7 %s", fmtUsd(s_costUsd).c_str(), tokBuf);
    ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
    ImGui::TextUnformatted(line);
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("OMP-observed spend (catalog pricing \xc3\x97 session tokens) \xe2\x80\x94 "
                          "not a bill. Click for the per-model breakdown.");
    if (ImGui::IsItemClicked()) ImGui::OpenPopup("##session_cost_detail");
    if (ImGui::BeginPopup("##session_cost_detail")) {
        ImGui::TextUnformatted("Session cost by model");
        ImGui::Separator();
        if (s_costRows.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
            ImGui::TextUnformatted("No priced turns yet this session.");
            ImGui::PopStyleColor();
        }
        for (const auto& row : s_costRows) {
            ImGui::PushID(row.model.c_str());
            ImGui::TextUnformatted(row.model.c_str());
            ImGui::SameLine(220.0f * sc);
            ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
            ImGui::Text("%d in / %d out", row.tokensIn, row.tokensOut);
            ImGui::PopStyleColor();
            ImGui::SameLine(380.0f * sc);
            ImGui::TextUnformatted(fmtUsd(row.usd).c_str());
            ImGui::PopID();
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
    // OMP catalog sublabel (ctx / cost / capabilities) for bridge-routed providers.
    const ui::BridgeModelMeta* meta = bridgeModelMetaForCad(provider, id, st.settings.baseUrl);
    const std::string sublabel = meta ? bridgeModelSublabel(*meta) : std::string();
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayShort)) {
        // Full model name + provider for the composer overlay picker; cost detail (cache
        // pricing, max output) rides the tooltip per the OMP model-browser convention.
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
        // Right-aligned dim "ctx 200k \xc2\xb7 $3/$15 \xc2\xb7 tools vision" (OMP model-browser row).
        const float sw = ImGui::CalcTextSize(sublabel.c_str()).x;
        if (mn.x + gut + lw + 8 * sc + 60.0f * sc < mx.x - sw - 8 * sc)
            dl->AddText(ImVec2(mx.x - sw - 8 * sc, midY - fh * 0.5f),
                        ui::Palette::toU32(pal.textDim), sublabel.c_str());
    }
    ImGui::PopID();
    return clicked && usable;
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
    refreshBridgeModelCatalog(st, false);  // OMP catalog sublabels (ctx/cost/capability chips)

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

    // Usable built-in cloud models (provider has a key), minus any the user hid or whose provider
    // category is bulk-disabled.
    for (const auto& m : ui::builtinModels()) {
        if (catOff(ui::providerCatKey(m.provider))) continue;
        if (!providerUsable(st, m.provider) || !matchq(m.label, providerShort(m.provider), m.id)) continue;
        if (modelHidden(st, m.id)) continue;
        ++shown;
        if (modelRow(st, m.provider, m.id, m.label, true, uid++)) {
            st.settings.provider = m.provider;
            std::strncpy(st.settings.model, m.id.c_str(), sizeof(st.settings.model) - 1);
            picked = true;
        }
    }
    // Cloud OpenAI-compatible service presets (OpenRouter / Alibaba / DeepSeek). Shown when a cloud
    // key is configured and the service category isn't bulk-disabled. Picking one points the active
    // Cloud API provider at that service's endpoint + model.
    if (cloudCompatKeyPresent(st)) {
        // OpenRouter: the live list (all free models + curated paid), minus any the user hid.
        if (!catOff("openrouter")) {
            for (const auto& r : openRouterModels(st)) {
                if (modelHidden(st, r.id) || !matchq(r.label, "openrouter free", r.id)) continue;
                ++shown;
                if (modelRow(st, ui::PROV_CLOUDCOMPAT, r.id, r.label, true, uid++)) {
                    st.settings.provider = ui::PROV_CLOUDCOMPAT;
                    std::strncpy(st.settings.model, r.id.c_str(), sizeof(st.settings.model) - 1);
                    std::strncpy(st.settings.baseUrl, "https://openrouter.ai/api/v1",
                                 sizeof(st.settings.baseUrl) - 1);
                    st.settings.vision = r.vision;
                    picked = true;
                }
            }
        }
        // Alibaba / DeepSeek curated presets.
        for (const auto& p : ui::cloudPresets()) {
            std::string key = ui::cloudServiceKey(p.baseUrl);
            if (key == "openrouter") continue;  // handled above (live)
            if (key == "moonshot") continue;    // handled below via its own key gate
            if (catOff(key)) continue;
            if (modelHidden(st, p.id) || !matchq(p.label, key, p.id)) continue;
            ++shown;
            if (modelRow(st, ui::PROV_CLOUDCOMPAT, p.id, p.label, true, uid++)) {
                st.settings.provider = ui::PROV_CLOUDCOMPAT;
                std::strncpy(st.settings.model, p.id.c_str(), sizeof(st.settings.model) - 1);
                std::strncpy(st.settings.baseUrl, p.baseUrl.c_str(), sizeof(st.settings.baseUrl) - 1);
                st.settings.vision = p.vision;
                picked = true;
            }
        }
    }
    // Moonshot (Kimi) — first-class wire provider gated on its OWN key (keys.moonshot), so Kimi
    // shows in the composer picker whenever a Moonshot key is set, even with no generic cloud key.
    if (moonshotKeyPresent(st) && !catOff("moonshot")) {
        for (const auto& p : ui::cloudPresets()) {
            if (std::strcmp(ui::cloudServiceKey(p.baseUrl), "moonshot") != 0) continue;
            if (modelHidden(st, p.id) || !matchq(p.label, "moonshot kimi", p.id)) continue;
            ++shown;
            if (modelRow(st, ui::PROV_CLOUDCOMPAT, p.id, p.label, true, uid++)) {
                st.settings.provider = ui::PROV_CLOUDCOMPAT;
                std::strncpy(st.settings.model, p.id.c_str(), sizeof(st.settings.model) - 1);
                std::strncpy(st.settings.baseUrl, p.baseUrl.c_str(), sizeof(st.settings.baseUrl) - 1);
                st.settings.vision = p.vision;
                picked = true;
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
                std::string bu = st.settings.baseUrl;
                bool loop = bu.find("localhost") != std::string::npos ||
                            bu.find("127.0.0.1") != std::string::npos ||
                            bu.find("0.0.0.0") != std::string::npos ||
                            bu.find("[::1]") != std::string::npos;
                if (!loop)
                    std::strncpy(st.settings.baseUrl,
                                 lp == ui::PROV_LLAMACPP ? "http://127.0.0.1:1234/v1"
                                                         : "http://localhost:1234/v1",
                                 sizeof(st.settings.baseUrl) - 1);
                picked = true;
            }
        }
    }
    if (shown == 0) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDisabled));
        ImGui::TextWrapped(q.empty()
            ? "No models available — sign in to a subscription (xAI / Claude / Cursor), "
              "add an API key, or start a local model server (gear below)."
            : "No models match your search.");
        ImGui::PopStyleColor();
    }
    ImGui::EndChild();

    // Unavailable models (no key / not signed in / endpoint down) are hidden from the list above;
    // surface a single muted hint pointing at Settings so they're still discoverable.
    bool anyUnavailable = false;
    for (const auto& m : ui::builtinModels())
        if (!providerUsable(st, m.provider)) { anyUnavailable = true; break; }
    if (anyUnavailable) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
        ImGui::TextUnformatted(
            "Other providers need a sign-in or API key \xE2\x80\x94 add one in Settings.");
        ImGui::PopStyleColor();
    }

    // Footer: per-session cost strip (agent_cost_summary; hidden while the bridge is absent).
    ImGui::Dummy(ImVec2(0, 2 * sc));
    drawSessionCostStrip(st);

    if (picked) {
        // CRITICAL: persist the pick to config.json NOW. The in-app run (agent_run_v2) resolves its
        // provider from config.json in the C++ core — NOT from in-memory st.settings — so a pick that
        // only updates st.settings would leave the run using the STALE persisted provider. That's the
        // bug where selecting a local model (gemma) still routed to the previously-saved cloud
        // endpoint and 403'd. Saving here keeps config.json == what the picker shows before any run.
        saveSettings(st.settings);
        ImGui::CloseCurrentPopup();
    }
}

// ============================ session slash commands (/compact /shake /handoff /btw /prewalk) ===

}  // namespace ui
