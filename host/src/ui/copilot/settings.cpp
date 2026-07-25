// Copilot AI settings (T-RF6 split of legacy_chat.cpp): the shared AI settings sections
// (provider/model/auth, generation, privacy, manage-models — ui::drawAiSettingsSection /
// drawProviderModelPicker, consumed by the Settings modal AI tab and the in-column settings
// overlay), the searchable model dropdown, model availability probing, and provider-usability
// gating (providerUsable / cloud+moonshot key presence).

#include "copilot_internal.h"

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
    double last = -1e9;     // ImGui::GetTime() when this provider's probe was DISPATCHED
    bool failed = false;    // last completed probe failed -> 60s backoff
    bool inFlight = false;  // this provider's probe is outstanding
};
// GL-thread only: written from refreshModelAvailability and from the AsyncRpc completion, which
// core::AsyncRpc runs on the GL thread inside drain().
std::unordered_map<int, ProbeTiming> s_probeTiming;

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
// arrives via refreshBridgeModelCatalog / agent_models_list). Probing them would only paint a
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
        default:                   return nullptr;     // PROV_CURSOR / PROV_KIMISUB / PROV_ZENMUX
    }
}

}  // namespace

// Defined below (they need the WASM-safe JSON accessors / the key buffers declared further down).
static void parseProbeRows(int provider, const nlohmann::json& result, ProviderModels& pm);
static char* providerKeyBuf(Settings& s, int provider);

void refreshModelAvailability(AppState& st, bool force) {
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
        t.last = now;
        t.inFlight = true;
        st.modelProbeInFlight.store(true);
        nlohmann::json p = {{"provider", wire},
                            {"base_url", baseUrl ? baseUrl : ""},
                            {"api_key", apiKey ? apiKey : ""}};
        st.rpc.call("agent_list_models", std::move(p),
                    [&st, provider, slots = std::vector<int>(slots)](const core::RpcResult& r) {
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
    // provider's /v1/models, through the OMP bridge when the credential is an OAuth/subscription
    // token), which is what makes a brand-new release appear in the picker with no code change.
    // Send that provider's OWN credential, and its base_url only where one is meaningful: the
    // cloud-compat slot's URL picks the service (OpenRouter / DashScope / DeepSeek / Moonshot),
    // while Anthropic / OpenAI / Gemini / xAI each have a fixed endpoint the backend defaults to
    // and st.settings.baseUrl may still hold a stale local URL from a previous provider. An EMPTY
    // api_key is the NORMAL, supported case for Anthropic subscription users: their OAuth token
    // lives in the bridge auth db and the C++ branch resolves it from there.
    if (ui::providerIsCloud(s.provider))
        dispatch(s.provider,
                 s.provider == ui::PROV_CLOUDCOMPAT ? s.baseUrl : "",
                 providerKeyBuf(s, s.provider), {s.provider});

    // (2) The LOCAL endpoint, regardless of which provider is active, so local models always show
    // up in the picker WITHOUT the user first switching the Provider dropdown to "Local".
    // Previously this early-returned whenever a cloud provider (OpenRouter / DashScope / ...) was
    // active — st.settings.baseUrl is then a remote URL — which is exactly why local models were
    // invisible until the user "poked around in settings" and selected Local.
    auto isLoopback = [](const char* u) {
        std::string v = u ? u : "";
        return v.find("localhost") != std::string::npos ||
               v.find("127.0.0.1") != std::string::npos ||
               v.find("0.0.0.0") != std::string::npos || v.find("[::1]") != std::string::npos;
    };
    // When the active base_url IS loopback, probe it (honors a custom local port). Otherwise a
    // cloud provider is active, so its base_url isn't the local one — fall back to the default LM
    // Studio endpoint so local discovery still happens. (A non-default local port is only
    // auto-found while the Local provider is the active one; the default covers the common case.)
    const bool activeLoopback = isLoopback(s.baseUrl);
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
        } else if (s.provider == ui::PROV_ZENMUX) {
            std::strncpy(s.model, "x-ai/grok-4.5-free", sizeof(s.model) - 1);
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
    } else if (s.provider == ui::PROV_ZENMUX) {
        // OMP-bridge provider — the credential lives in the bridge's own auth db, wired up via
        // Settings → Providers sign-in (drawOmpProviderManager), not a local config.json key.
        dimText("ZenMux (OMP bridge). Sign in under Settings \xe2\x86\x92 Providers; no API key "
                "field here — the bridge resolves the credential from its own auth db.");
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(ui::palette.textDim));
        ImGui::TextUnformatted("Wire provider: zenmux \xc2\xb7 model: x-ai/grok-4.5-free (default)");
        ImGui::PopStyleColor();
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

    switch (section) {
    case AiSettingsSection::ProviderModel:
        drawProviderModel();
        break;
    case AiSettingsSection::Generation:
        drawGeneration();
        break;
    case AiSettingsSection::All:
        drawProviderModel();
        ImGui::Dummy(ImVec2(0, 8 * S));
        ImGui::SeparatorText("Generation");
        drawGeneration();
        break;
    }
    return changed;
}

// Full form: Provider + Generation. (Single-call convenience; slim is a legacy no-op flag.)
bool drawProviderModelPicker(AppState& st, bool busy, bool slim) {
    (void)slim;
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
    updateConfigJson([&](nlohmann::json& j) {
        if (!j.contains("agent") || !j["agent"].is_object())
            j["agent"] = nlohmann::json::object();
        j["agent"]["max_subagent_depth"] = depth;
    });
}

void drawAISettingsBody(AppState& st, bool busy) {
    Settings& s = st.settings;
    const auto& pal = ui::palette;
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

    cpMenuHeader(st.fonts, "Quick settings",
                 "Session knobs. Full providers, roles and prompts live in Settings.");

    // Fields sit on adaptive GLASS, not on ImGui's opaque dark FrameBg: with the default frame the
    // combo/slider VALUES rendered near-invisible (dark ink on a dark frame over a bright
    // viewport). Derive the frame from the surface ink, exactly like the composer toolbar chips.
    const ImVec4 fieldBg (surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.08f);
    const ImVec4 fieldHov(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.14f);
    const ImVec4 fieldAct(surf.textPrimary.x, surf.textPrimary.y, surf.textPrimary.z, 0.18f);
    // A combo POPUP is an opaque themed window, not glass — its ink must contrast with popupBg,
    // not with the viewport backdrop (the glass tone can land dark-on-dark in there).
    const ImVec4 popBg(pal.popupBg[0], pal.popupBg[1], pal.popupBg[2], pal.popupBg[3]);
    auto pushField = [&]() {
        ImGui::PushStyleColor(ImGuiCol_FrameBg, fieldBg);
        ImGui::PushStyleColor(ImGuiCol_FrameBgHovered, fieldHov);
        ImGui::PushStyleColor(ImGuiCol_FrameBgActive, fieldAct);
        ImGui::PushStyleColor(ImGuiCol_Button, fieldBg);        // combo caret well == frame
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, fieldHov);
        ImGui::PushStyleColor(ImGuiCol_ButtonActive, fieldAct);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::metrics::cpMenuChipRound());
        ImGui::PushStyleVar(ImGuiStyleVar_GrabRounding, ui::metrics::cpMenuChipRound());
    };
    auto popField = []() { ImGui::PopStyleVar(2); ImGui::PopStyleColor(7); };
    // The ONE vertical gap this body owns: control -> next label. No ad-hoc Dummy() stacks, so the
    // groups here share the rhythm of the Sessions / Tasks / Artifacts menus.
    auto ctlGap = []() { ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuCtlGap())); };

    cpMenuSection("Autonomy");
    static const char* kPrefLabels[] = {"Off", "Option", "Encourage", "Require"};
    auto prefCombo = [&](const char* label, const char* id, int* pref) {
        const int v = (*pref < 0 || *pref > 3) ? 0 : *pref;
        cpMenuLabel(label);
        ImGui::SetNextItemWidth(-1);
        pushField();
        // BeginCombo rather than Combo so the popup rows can carry their own ink (see popBg).
        if (ImGui::BeginCombo(id, kPrefLabels[v])) {
            ImGui::PushStyleColor(ImGuiCol_Text, ui::maxContrastTextOn(popBg));
            for (int i = 0; i < 4; ++i) {
                if (ImGui::Selectable(kPrefLabels[i], i == v)) {
                    *pref = i;
                    if (pref == &s.planPreference) s.planMode = (i != 0);
                    changed = true;
                }
            }
            ImGui::PopStyleColor();
            ImGui::EndCombo();
        }
        popField();
        ctlGap();
    };
    prefCombo("Plan preference", "##q_plan_pref", &s.planPreference);
    prefCombo("Subagent preference", "##q_sub_pref", &s.subagentPreference);
    prefCombo("Advisor preference", "##q_adv_pref", &s.advisorPreference);
    pushField();
    if (ImGui::Checkbox("Proactive advisor", &s.proactiveAdvisor)) changed = true;
    popField();
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip(
            "Opt-in mid-run guidance on failures. Cheap notes by default; "
            "rare short advisor call only on severe streaks.");

    cpMenuSection("Session");
    pushField();
    if (ImGui::Checkbox("Vision (send screenshots)", &s.vision)) changed = true;
    if (ImGui::Checkbox("Offline mode (local only)", &s.offlineMode)) changed = true;
    ImGui::Checkbox("Show assistant thoughts", &gShowThinking);
    popField();

    cpMenuSection("Limits");
    // Both limit sliders get the SAME chrome (rounding, full width, unit inside the value): the
    // cap used to render as a left-edge grab and the nesting one as a centred filled pill purely
    // because their ranges and formats differed. 0 passes stays the "agent decides" sentinel and
    // spells itself out rather than showing a bare 0.
    cpMenuLabel("Refine-pass cap");
    ImGui::SetNextItemWidth(-1);
    pushField();
    int iters = s.maxIters;
    if (ImGui::SliderInt("##q_miters", &iters, 0, 64, iters == 0 ? "automatic" : "%d passes")) {
        s.maxIters = (iters < 0) ? 0 : iters;
        changed = true;
    }
    popField();
    ctlGap();

    static int s_maxDepth = -1;
    if (s_maxDepth < 0) s_maxDepth = loadMaxSubagentDepth();
    cpMenuLabel("Max subagent nesting");
    ImGui::SetNextItemWidth(-1);
    pushField();
    int depth = s_maxDepth;
    if (ImGui::SliderInt("##q_depth", &depth, 0, 4, "%d levels")) {
        if (depth < 0) depth = 0;
        if (depth > 4) depth = 4;
        s_maxDepth = depth;
        saveMaxSubagentDepth(depth);
    }
    popField();

    cpMenuSection("Memory");
    pushField();
    if (ImGui::Checkbox("Project memory", &s.memoryEnabled)) changed = true;
    popField();
    if (s.memoryEnabled) {
        ctlGap();
        cpMenuLabel("Memory token budget");
        ImGui::SetNextItemWidth(-1);
        pushField();
        if (ImGui::SliderInt("##q_membudget", &s.memoryTokenBudget, 100, 2000, "%d tokens"))
            changed = true;
        popField();
    }

    cpMenuSection("Full settings");
    {
        // One row of three equal chips instead of three stacked full-width buttons — the stack
        // read as another form field group rather than as navigation out of the pane.
        const float gap = ui::metrics::cpMenuRowGap();
        const float chipW = std::max(ui::metrics::px(48.0f),
                                     (ImGui::GetContentRegionAvail().x - 2.0f * gap) / 3.0f);
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, ui::metrics::cpMenuChipRound());
        ImGui::PushStyleColor(ImGuiCol_Button, fieldBg);
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, fieldHov);
        ImGui::PushStyleColor(ImGuiCol_ButtonActive, fieldAct);
        auto deepLink = [&](const char* label, int tab) {
            if (ImGui::Button(label, ImVec2(chipW, 0.0f))) {
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
        ImGui::PopStyleColor(3);
        ImGui::PopStyleVar();
    }
    ImGui::Dummy(ImVec2(0, ui::metrics::cpMenuLabelGap()));
    cpMenuHint("Opens the full Settings window.");

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
    // A SUCCESSFUL live probe is proof of a working credential — and for a subscription/OAuth
    // user it is the ONLY proof, because their token lives in the OMP bridge auth db rather than
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
        // ZenMux — OMP-bridge credential lives in the bridge's own auth db (Settings → Providers),
        // not a locally-polled subscription/key flag this file can cheaply check. Always offered;
        // an unsigned-in bridge surfaces a normal auth error from the backend on send, same class
        // as any other unconfigured provider.
        case ui::PROV_ZENMUX:      return true;
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
        case ui::PROV_ZENMUX:    prov = "zenmux"; break;
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

// One-time model-picker default seeding: on first run keep only the top 3 models per provider
// enabled (the rest go into hiddenModels) so a fresh install's composer picker isn't flooded.
// Runs once — settings.modelDefaultsSeeded flips and the user's explicit choices win after that.
// The ACTIVE model is never hidden. Bridge (OMP) seeding needs the async catalog; when it hasn't
// arrived yet we still seed the builtin half so the picker is sane on builtin alone.
static void seedModelDefaultsOnce(AppState& st) {
    if (st.settings.modelDefaultsSeeded) return;
    const std::string active = st.settings.model;
    auto hideUnlessActive = [&](const std::string& id) {
        if (id != active) st.settings.hiddenModels.insert(id);
    };
    // (a) Built-in catalog, in catalog order: first 3 per provider stay enabled.
    {
        std::map<int, int> kept;
        for (const auto& m : ui::builtinModels()) {
            int& n = kept[m.provider];
            if (n < 3) { ++n; continue; }
            hideUnlessActive(m.id);
        }
    }
    // (b) Bridge (OMP) catalog, pre-sorted per provider (non-deprecated first, newest first):
    // the first 3 non-deprecated rows stay enabled; everything else — including ALL deprecated
    // rows — hides. Skipped while the catalog is empty (first async fetch not back yet).
    if (!ui::bridgeModelCatalog().empty()) {
        std::map<std::string, int> kept;
        for (const auto& m : ui::bridgeModelCatalog()) {
            int& n = kept[m.provider];
            if (!m.deprecated && n < 3) { ++n; continue; }
            hideUnlessActive(m.id);
        }
    }
    st.settings.hiddenModels.erase(active);  // the active model always stays visible
    st.settings.modelDefaultsSeeded = true;
    saveSettings(st.settings);
}

// Poll agent_provider_list so the composer knows which OMP providers are signed in WITHOUT the
// user having opened Settings -> Providers. Fills st.signedInBridgeProviders (same set the
// Providers panel writes) so the dropdown mirrors the Providers tab. TTL'd; degrades silently.
static void refreshSignedInProviders(AppState& st, bool force) {
    static double s_lastPoll = -1e9;
    static bool s_inFlight = false;
    if (s_inFlight) return;
    const double now = ImGui::GetTime();
    if (!force && (now - s_lastPoll) < 5.0) return;
    s_lastPoll = now;
    s_inFlight = true;
    st.rpc.call("agent_provider_list", nlohmann::json::object(), [&st](const core::RpcResult& r) {
        s_inFlight = false;
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", true)) return;
        const auto it = r.result.find("providers");
        if (it == r.result.end() || !it->is_array()) return;
        std::set<std::string> live;
        for (const auto& p : *it) {
            if (!p.is_object()) continue;
            const std::string id = p.value("id", std::string());
            const std::string state = p.value("state", std::string());
            if (!id.empty() && !state.empty() && state != "signed_out") live.insert(id);
        }
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
    refreshBridgeModelCatalog(st, false);  // OMP catalog sublabels (ctx/cost/capability chips)
    refreshSignedInProviders(st, false);  // mirror Settings->Providers signed-in set into the picker
    seedModelDefaultsOnce(st);             // first-run top-3-per-provider seeding; no-op after

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
    // providers, sourced from the Settings -> Providers catalog (ui::bridgeModelCatalog()). Each OMP
    // catalog provider id maps to a composer slot + base_url; providers with no working native
    // transport (e.g. Cursor) map to slot -1 and are excluded so they can't cause provider_error.
    struct Route { int slot; const char* baseUrl; };
    auto routeFor = [](const std::string& p) -> Route {
        if (p == "anthropic") return {ui::PROV_ANTHROPIC, ""};
        if (p == "openai") return {ui::PROV_OPENAI, ""};
        if (p == "google" || p == "gemini") return {ui::PROV_GEMINI, ""};
        if (p == "xai") return {ui::PROV_XAI, "https://api.x.ai/v1"};
        if (p == "kimi-code") return {ui::PROV_KIMISUB, ""};
        if (p == "zenmux") return {ui::PROV_ZENMUX, ""};
        if (p == "moonshot") return {ui::PROV_CLOUDCOMPAT, "https://api.moonshot.ai/v1"};
        if (p == "openrouter") return {ui::PROV_CLOUDCOMPAT, "https://openrouter.ai/api/v1"};
        if (p == "deepseek") return {ui::PROV_CLOUDCOMPAT, "https://api.deepseek.com/v1"};
        if (p == "alibaba" || p == "alibaba-coding-plan")
            return {ui::PROV_CLOUDCOMPAT, "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"};
        return {-1, ""};  // cursor + any unrouted provider: no working composer transport -> excluded
    };

    const auto& catalog = ui::bridgeModelCatalog();
    if (!catalog.empty()) {
        for (const auto& m : catalog) {
            Route r = routeFor(m.provider);
            if (r.slot < 0) continue;                                        // no transport (cursor/…)
            if (st.signedInBridgeProviders.count(m.provider) == 0) continue; // provider not signed in
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
            ? "No models available — sign in to a subscription (ZenMux / xAI / Claude / Cursor), "
              "add an API key, or start a local model server (gear below)."
            : "No models match your search.");
        ImGui::PopStyleColor();
    }
    ImGui::EndChild();

    // No providers signed in yet -> nothing routable; point the user at Settings to add models.
    if (st.signedInBridgeProviders.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, ui::Palette::toU32(pal.textDim));
        ImGui::TextUnformatted("Sign in to a provider in Settings -> Providers to add models.");
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
