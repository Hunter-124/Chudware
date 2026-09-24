#include "onboarding.h"

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <string>
#include <utility>

#include <nlohmann/json.hpp>

#include "imgui.h"

#include "../core/app_state.h"
#include "../ui_theme.h"
#include "settings_modal.h"
#include "web_capabilities.h"
#include "metrics.h"
#include "widgets/kit.h"
#include "chudcode_descriptors.h"

namespace ui {
namespace {

struct WizardState {
    bool gateRead = false;
    bool requestOpen = false;
    bool open = false;
    int completedVersion = 0;
    int step = 0;
    int chosenWorkspace = -1;
    bool accountActionBusy = false;
    OnboardingAccountSummary account;
    char key[256] = "";
    int keyProvider = 0;
    bool keyBusy = false;
    bool keyError = false;
    std::string keyStatus;
    unsigned long long keyGeneration = 0;
    bool testBusy = false;
    bool testError = false;
    std::string testReply;
    unsigned long long testGeneration = 0;
    std::string persistError;
};

WizardState s_wizard;
OnboardingAccountLoginSeam s_accountLogin;

struct KeyProvider {
    const char* label;
    const char* id;
    const char* consoleUrl;
};

constexpr KeyProvider kKeyProviders[] = {
    {"Anthropic", "anthropic", "https://console.anthropic.com/settings/keys"},
    {"OpenAI", "openai", "https://platform.openai.com/api-keys"},
    {"Google Gemini", "google", "https://aistudio.google.com/apikey"},
    {"xAI", "xai", "https://console.x.ai"},
    {"OpenRouter", "openrouter", "https://openrouter.ai/keys"},
    {"Moonshot", "moonshot", "https://platform.moonshot.ai/console/api-keys"},
};

struct OAuthProvider {
    const char* label;
    const char* id;
};

constexpr OAuthProvider kOAuthProviders[] = {
    {"Claude", "anthropic"},
    {"ChatGPT / Codex", "openai-codex"},
    {"xAI", "xai-oauth"},
    {"Cursor", "cursor"},
    {"Kimi Code", "kimi-code"},
};

struct WorkspaceChoice {
    const char* label;
    const char* detail;
    const char* icon;
    const char* configName;
    AppState::Workspace workspace;
};

constexpr WorkspaceChoice kWorkspaces[] = {
    {"Model", "Parametric parts & assemblies", "ws_model", "model", AppState::Workspace::Model},
    {"Drawing", "Production drawings & sheets", "ws_drawing", "drawing",
     AppState::Workspace::Drawing},
    {"Simulate", "Structural & motion studies", "ws_simulate", "simulate",
     AppState::Workspace::Simulate},
    {"Electronics", "Schematics & PCB layout", "ws_electronics", "electronics",
     AppState::Workspace::Electronics},
    {"Manufacture", "CAM setup, toolpaths & shop", "ws_manufacture", "manufacture",
     AppState::Workspace::Manufacture},
    {"PLM", "Projects, BOMs & revisions", "history", "plm", AppState::Workspace::PLM},
    {"Parts Library", "Reusable components & suppliers", "ws_parts", "parts_library",
     AppState::Workspace::PartsLibrary},
    {"AI", "Copilot command center", "ai_panel", "ai", AppState::Workspace::AI},
    {"Reverse Engineering", "Scans, meshes & reconstruction", "file_mesh",
     "reverse_engineering", AppState::Workspace::ReverseEng},
    {"Firmware", "Build, emulate & inspect firmware", "file_code", "firmware",
     AppState::Workspace::Firmware},
};

std::string jsonString(const nlohmann::json& value, const char* key) {
    if (!value.is_object()) return {};
    const auto it = value.find(key);
    return it != value.end() && it->is_string() ? it->get<std::string>() : std::string();
}

std::string rpcError(const core::RpcResult& r, const char* absentExplanation) {
    if (!r.ok && !r.err.empty()) return r.err;
    if (r.result.is_object()) {
        for (const char* key : {"detail", "error", "message", "reason"}) {
            std::string text = jsonString(r.result, key);
            if (!text.empty()) return text;
        }
        const auto nested = r.result.find("provider_error");
        if (nested != r.result.end()) {
            if (nested->is_string()) return nested->get<std::string>();
            if (nested->is_object()) {
                for (const char* key : {"detail", "error", "message", "reason"}) {
                    std::string text = jsonString(*nested, key);
                    if (!text.empty()) return text;
                }
            }
        }
    }
    return absentExplanation;
}

int readCompletedVersion() {
    std::ifstream in(configPath());
    if (!in) return 0;
    try {
        nlohmann::json config;
        in >> config;
        if (!config.is_object()) return 0;
        const auto onboarding = config.find("onboarding");
        if (onboarding == config.end() || !onboarding->is_object()) return 0;
        const auto version = onboarding->find("completed_version");
        if (version == onboarding->end()) return 0;
        if (version->is_number_integer()) return version->get<int>();
        if (version->is_string()) return std::stoi(version->get<std::string>());
    } catch (...) {
        // A corrupt or incompatible gate is safely treated as incomplete; the merge writer below
        // remains the only code in this module that mutates config.json.
    }
    return 0;
}

void dimWrapped(const char* text) {
    ImGui::PushStyleColor(ImGuiCol_Text, Palette::toU32(palette.textDim));
    ImGui::TextWrapped("%s", text);
    ImGui::PopStyleColor();
}

bool beginCard(const char* id, float height) {
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(14.0f * uiScale, 12.0f * uiScale));
    const bool visible = ImGui::BeginChild(
        id, ImVec2(-1.0f, height * uiScale),
        ImGuiChildFlags_Border | ImGuiChildFlags_AlwaysUseWindowPadding,
        ImGuiWindowFlags_NoScrollbar);
    ImGui::PopStyleVar();
    return visible;
}

void endCard() { ImGui::EndChild(); }

void showAccountSummary() {
    if (!s_wizard.account.signedIn) {
        dimWrapped("Not signed in. Your plan and Chudware credit balance will appear here after sign-in.");
        return;
    }
    ImGui::Text("Plan: %s", s_wizard.account.plan.empty() ? "unknown" : s_wizard.account.plan.c_str());
    ImGui::Text("Credit balance: %lld credits", s_wizard.account.creditBalance);
    if (!s_wizard.account.detail.empty()) dimWrapped(s_wizard.account.detail.c_str());
}

void refreshAccount(AppState& st) {
    if (!s_accountLogin || s_wizard.accountActionBusy) return;
    OnboardingAccountSummary refreshed = s_wizard.account;
    if (s_accountLogin(st, OnboardingAccountAction::Refresh, refreshed))
        s_wizard.account = std::move(refreshed);
}

void drawAccountStep(AppState& st) {
    ImGui::TextUnformatted("Sign in to Chudware");
    dimWrapped("Connect this desktop to your Chudware account. Authorization happens in your system browser, never inside the app.");
    ImGui::Dummy(ImVec2(0, 8.0f * uiScale));
    if (beginCard("##onboarding_account", 155.0f)) {
        showAccountSummary();
        ImGui::Dummy(ImVec2(0, 8.0f * uiScale));
        if (!s_accountLogin) {
            ImGui::PushStyleColor(ImGuiCol_Text, Palette::toU32(palette.statusWarn));
            ImGui::TextWrapped("Account sign-in is not available in this build yet. You can continue without it; a later update will connect this desktop to your Chudware account.");
            ImGui::PopStyleColor();
        } else if (!s_wizard.account.signedIn) {
            if (ImGui::Button("Open Chudware sign-in in browser", ImVec2(-1, 0))) {
                s_wizard.accountActionBusy = true;
                OnboardingAccountSummary refreshed = s_wizard.account;
                const bool accepted = s_accountLogin(st, OnboardingAccountAction::BeginLogin, refreshed);
                s_wizard.account = std::move(refreshed);
                s_wizard.accountActionBusy = false;
                if (!accepted && s_wizard.account.detail.empty())
                    s_wizard.account.detail = "Chudware sign-in could not be started.";
            }
        } else if (ImGui::Button("Refresh account", ImVec2(-1, 0))) {
            refreshAccount(st);
        }
    }
    endCard();
}

void beginValidatedKeySave(AppState& st) {
    const std::string key = s_wizard.key;
    if (key.empty() || s_wizard.keyBusy) return;
    const std::string provider = kKeyProviders[s_wizard.keyProvider].id;
    s_wizard.keyBusy = true;
    s_wizard.keyError = false;
    s_wizard.keyStatus = "Checking with " + std::string(kKeyProviders[s_wizard.keyProvider].label) + "\xe2\x80\xa6";
    const auto generation = ++s_wizard.keyGeneration;
    st.rpc.call(kChudcodeValidateKeyMethod,
                nlohmann::json{{"providerId", provider}, {"apiKey", key}},
                [&st, provider, key, generation](const core::RpcResult& validation) {
        if (generation != s_wizard.keyGeneration) return;
        const bool valid = validation.ok &&
                           chudcodeKeyValidationAccepted(validation.result, provider);
        if (!valid) {
            s_wizard.keyBusy = false;
            s_wizard.keyError = true;
            s_wizard.keyStatus = rpcError(
                validation, "The provider rejected the key without returning an explanation.");
            return;
        }
        st.rpc.call(kChudcodeSetKeyMethod,
                    nlohmann::json{{"providerId", provider}, {"apiKey", key}},
                    [provider, generation](const core::RpcResult& saved) {
            if (generation != s_wizard.keyGeneration) return;
            s_wizard.keyBusy = false;
            const bool ok = saved.ok && chudcodeKeySaveAccepted(saved.result, provider);
            if (!ok) {
                s_wizard.keyError = true;
                s_wizard.keyStatus = rpcError(
                    saved, "The validated key could not be stored by the authentication service.");
                return;
            }
            s_wizard.keyError = false;
            s_wizard.keyStatus = "Key validated and saved securely.";
            std::memset(s_wizard.key, 0, sizeof(s_wizard.key));
        });
    }, 20000);
}

void drawPowerStep(AppState& st) {
    refreshAccount(st);
    const ExternalProviderSignInStatus oauth = pollExternalProviderSignIn(st);

    ImGui::TextUnformatted("Choose how the copilot is powered");
    dimWrapped("Use Chudware credits, an AI subscription you already pay for, or your own provider API key.");
    ImGui::Dummy(ImVec2(0, 6.0f * uiScale));

    // This card is intentionally absent unless account truth says the current plan permits pooled keys.
    if (s_wizard.account.signedIn && s_wizard.account.pooledKeysAllowed) {
        if (beginCard("##onboarding_credits", 112.0f)) {
            ImGui::TextUnformatted("Use Chudware credits");
            ImGui::Text("Balance: %lld credits", s_wizard.account.creditBalance);
            if (ImGui::Button("Use credits")) {
                OnboardingAccountSummary refreshed = s_wizard.account;
                s_accountLogin(st, OnboardingAccountAction::UseCredits, refreshed);
                s_wizard.account = std::move(refreshed);
            }
            ImGui::SameLine();
            if (ImGui::Button("Top up in browser"))
                openExternalHttpUrl("https://app.chudware.com/account/billing");
        }
        endCard();
        ImGui::Dummy(ImVec2(0, 6.0f * uiScale));
    }

    if (beginCard("##onboarding_oauth", 142.0f)) {
        ImGui::TextUnformatted("Sign in to an AI account you already pay for");
        dimWrapped("One click opens the provider in your system browser.");
        for (const auto& provider : kOAuthProviders) {
            ImGui::PushID(provider.id);
            if (oauth.active && oauth.provider != provider.id) ImGui::BeginDisabled();
            if (ImGui::SmallButton(provider.label))
                beginExternalProviderSignIn(st, provider.id);
            if (oauth.active && oauth.provider != provider.id) ImGui::EndDisabled();
            ImGui::PopID();
            ImGui::SameLine();
        }
        ImGui::NewLine();
        if (!oauth.provider.empty()) {
            if (oauth.connected) {
                ImGui::PushStyleColor(ImGuiCol_Text, Palette::toU32(palette.statusOk));
                ImGui::Text("Connected: %s", oauth.provider.c_str());
                ImGui::PopStyleColor();
            } else if (!oauth.error.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, Palette::toU32(palette.statusErr));
                ImGui::TextWrapped("%s", oauth.error.c_str());
                ImGui::PopStyleColor();
                if (!oauth.url.empty() && ImGui::SmallButton("Open sign-in link again"))
                    openExternalHttpUrl(oauth.url);
            } else {
                const char* progress = !oauth.progress.empty() ? oauth.progress.c_str()
                                      : (oauth.browserOpened ? "Browser opened; waiting for approval\xe2\x80\xa6"
                                                             : "Starting sign-in\xe2\x80\xa6");
                dimWrapped(progress);
            }
        }
    }
    endCard();
    ImGui::Dummy(ImVec2(0, 6.0f * uiScale));

    if (beginCard("##onboarding_apikey", 166.0f)) {
        ImGui::TextUnformatted("Paste an API key");
        ImGui::SetNextItemWidth(190.0f * uiScale);
        if (ImGui::BeginCombo("##onboarding_key_provider", kKeyProviders[s_wizard.keyProvider].label)) {
            for (int i = 0; i < static_cast<int>(std::size(kKeyProviders)); ++i) {
                const bool selected = s_wizard.keyProvider == i;
                if (ImGui::Selectable(kKeyProviders[i].label, selected)) {
                    s_wizard.keyProvider = i;
                    s_wizard.keyStatus.clear();
                    s_wizard.keyError = false;
                }
                if (selected) ImGui::SetItemDefaultFocus();
            }
            ImGui::EndCombo();
        }
        ImGui::SameLine();
        if (ImGui::SmallButton("Where do I get one?"))
            openExternalHttpUrl(kKeyProviders[s_wizard.keyProvider].consoleUrl);
        ImGui::SetNextItemWidth(-1.0f);
        ImGui::InputTextWithHint("##onboarding_key", "Paste API key\xe2\x80\xa6", s_wizard.key,
                                 sizeof(s_wizard.key), ImGuiInputTextFlags_Password);
        if (s_wizard.keyBusy) ImGui::BeginDisabled();
        if (ImGui::Button(s_wizard.keyBusy ? "Validating\xe2\x80\xa6" : "Validate and save") &&
            s_wizard.key[0] != 0 && !s_wizard.keyBusy)
            beginValidatedKeySave(st);
        if (s_wizard.keyBusy) ImGui::EndDisabled();
        if (!s_wizard.keyStatus.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                                  Palette::toU32(s_wizard.keyError ? palette.statusErr : palette.statusOk));
            ImGui::TextWrapped("%s", s_wizard.keyStatus.c_str());
            ImGui::PopStyleColor();
        }
    }
    endCard();
}

void beginRealTest(AppState& st) {
    if (s_wizard.testBusy) return;
    s_wizard.testBusy = true;
    s_wizard.testError = false;
    s_wizard.testReply.clear();
    const auto generation = ++s_wizard.testGeneration;
    nlohmann::json params = {
        {"question", "Reply with exactly five words: Copilot is ready to help."},
        {"doc_id", ""},
        {"options", {{"mode", "ask"}, {"advisor", false}, {"max_output_tokens", 32}}},
    };
    st.rpc.call("agent_ask_v2", std::move(params), [generation](const core::RpcResult& r) {
        if (generation != s_wizard.testGeneration) return;
        s_wizard.testBusy = false;
        if (!r.ok || !r.result.is_object() || r.result.contains("code")) {
            s_wizard.testError = true;
            s_wizard.testReply = rpcError(r, "The provider did not return a completion.");
            return;
        }
        s_wizard.testReply = jsonString(r.result, "answer");
        if (s_wizard.testReply.empty()) {
            s_wizard.testError = true;
            s_wizard.testReply = "The provider returned an empty reply.";
        }
    }, 300000);
}

void drawTestStep(AppState& st, bool appBusy) {
    ImGui::TextUnformatted("Test it");
    dimWrapped("Send one tiny real completion through the provider you configured. The reply below comes from the provider; it is never simulated.");
    ImGui::Dummy(ImVec2(0, 10.0f * uiScale));
    if ((appBusy || s_wizard.testBusy)) ImGui::BeginDisabled();
    if (ImGui::Button(s_wizard.testBusy ? "Waiting for the provider\xe2\x80\xa6" : "Run real copilot test",
                      ImVec2(-1, 42.0f * uiScale)) && !appBusy && !s_wizard.testBusy)
        beginRealTest(st);
    if ((appBusy || s_wizard.testBusy)) ImGui::EndDisabled();
    ImGui::Dummy(ImVec2(0, 10.0f * uiScale));
    if (!s_wizard.testReply.empty()) {
        if (beginCard("##onboarding_test_reply", 170.0f)) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                                  Palette::toU32(s_wizard.testError ? palette.statusErr : palette.statusOk));
            ImGui::TextUnformatted(s_wizard.testError ? "Provider error" : "Copilot replied");
            ImGui::PopStyleColor();
            ImGui::Separator();
            ImGui::TextWrapped("%s", s_wizard.testReply.c_str());
        }
        endCard();
    } else {
        dimWrapped("No test has been run yet.");
    }
}

void drawWorkspaceStep() {
    ImGui::TextUnformatted("Pick a starting workspace");
    kit::pushDensity(kit::Density::Roomy);
    kit::dimText(
        "Choose where Chudware should take you when setup finishes. You can switch workspaces at any time.");
    kit::gapRow();

    char workspaceCount[32];
    std::snprintf(workspaceCount, sizeof(workspaceCount), "%zu workspaces",
                  std::size(kWorkspaces));
    kit::sectionHeader("Choose one", nullptr, workspaceCount, -1, /*first=*/true);

    // Keep two balanced columns while both lines can remain intact. On narrower windows the
    // cards become one scrollable column rather than clipping a capability label or destination.
    float widestText = 0.0f;
    for (const WorkspaceChoice& choice : kWorkspaces) {
        widestText = std::max(widestText, ImGui::CalcTextSize(choice.label).x);
        widestText = std::max(widestText, ImGui::CalcTextSize(choice.detail).x);
    }
    const float twoLineH =
        ImGui::GetTextLineHeight() * 2.0f + metrics::U(0.5f);
    const float selectedMetaW =
        ImGui::CalcTextSize("Selected").x + metrics::U(1.5f);
    const float minimumCardW =
        kit::padX() * 2.0f + twoLineH + metrics::U(1.5f) + widestText + selectedMetaW;
    const float availableW = ImGui::GetContentRegionAvail().x;
    const int columns =
        availableW >= minimumCardW * 2.0f + kit::rowGap() ? 2 : 1;

    const ImGuiTableFlags tableFlags =
        ImGuiTableFlags_SizingStretchSame | ImGuiTableFlags_NoPadOuterX |
        ImGuiTableFlags_NoSavedSettings;
    const float cellGap = kit::rowGap() * 0.5f;
    ImGui::PushStyleVar(ImGuiStyleVar_CellPadding, ImVec2(cellGap, cellGap));
    if (ImGui::BeginTable("##onboarding_workspaces", columns, tableFlags)) {
        for (int i = 0; i < static_cast<int>(std::size(kWorkspaces)); ++i) {
            ImGui::TableNextColumn();
            const bool selected = s_wizard.chosenWorkspace == i;
            kit::RowStyle style;
            style.icon = kWorkspaces[i].icon;
            style.iconTone = selected ? kit::Tone::Accent : kit::Tone::Neutral;
            style.secondary = kWorkspaces[i].detail;
            style.meta = selected ? "Selected" : nullptr;
            style.tooltip = kWorkspaces[i].detail;
            style.selected = selected;
            if (kit::row(kWorkspaces[i].configName, kWorkspaces[i].label, style))
                s_wizard.chosenWorkspace = i;
        }
        ImGui::EndTable();
    }
    ImGui::PopStyleVar();
    kit::popDensity();
}

bool finishWizard(AppState& st) {
    const char* workspace = s_wizard.chosenWorkspace >= 0
                                ? kWorkspaces[s_wizard.chosenWorkspace].configName
                                : nullptr;
    if (!persistOnboardingCompletion(workspace)) {
        s_wizard.persistError = "Chudware could not save setup progress. Your existing config was left unchanged.";
        return false;
    }
    if (s_wizard.chosenWorkspace >= 0)
        st.activeWorkspace = kWorkspaces[s_wizard.chosenWorkspace].workspace;
    s_wizard.completedVersion = kOnboardingVersion;
    s_wizard.open = false;
    s_wizard.requestOpen = false;
    s_wizard.persistError.clear();
    ImGui::CloseCurrentPopup();
    return true;
}

void drawFooter(AppState& st) {
    ImGui::Separator();
    if (s_wizard.step > 0 && ImGui::Button("Back")) --s_wizard.step;
    if (s_wizard.step > 0) ImGui::SameLine();
    const char* skip = s_wizard.step == 3 ? "Skip and finish" : "Skip this step";
    if (ImGui::Button(skip)) {
        if (s_wizard.step < 3) ++s_wizard.step;
        else finishWizard(st);
    }
    ImGui::SameLine();
    const float continueWidth = 150.0f * uiScale;
    ImGui::SetCursorPosX(std::max(ImGui::GetCursorPosX(),
                                  ImGui::GetWindowContentRegionMax().x - continueWidth));
    const char* next = s_wizard.step == 3 ? "Start here" : "Continue";
    if (ImGui::Button(next, ImVec2(continueWidth, 0))) {
        if (s_wizard.step < 3) ++s_wizard.step;
        else finishWizard(st);
    }
    if (!s_wizard.persistError.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, Palette::toU32(palette.statusErr));
        ImGui::TextWrapped("%s", s_wizard.persistError.c_str());
        ImGui::PopStyleColor();
    }
}

}  // namespace

void setOnboardingAccountLoginSeam(OnboardingAccountLoginSeam seam) {
    s_accountLogin = std::move(seam);
}

bool persistOnboardingCompletion(const char* startingWorkspace) {
    const std::string workspace = startingWorkspace ? startingWorkspace : "";
    return updateConfigJson([&workspace](nlohmann::json& config) {
        nlohmann::json onboarding = nlohmann::json::object();
        const auto existing = config.find("onboarding");
        if (existing != config.end() && existing->is_object()) onboarding = *existing;
        onboarding["completed_version"] = kOnboardingVersion;
        if (!workspace.empty()) onboarding["starting_workspace"] = workspace;
        config["onboarding"] = std::move(onboarding);
    });
}

void forceOnboardingForScenario(int step) {
    s_wizard.requestOpen = true;
    s_wizard.open = true;
    s_wizard.step = std::clamp(step, 0, 3);
}

void drawOnboarding(AppState& st, bool appBusy) {
    // The gateway already authenticates web users. This wizard provisions desktop
    // account/provider access; it must never open while browser state is loading.
    if (webBuild()) return;

    if (!s_wizard.gateRead) {
        s_wizard.gateRead = true;
        s_wizard.completedVersion = readCompletedVersion();
        // A headless capture must show the surface under test. The wizard opens whenever the
        // config's completed version is stale, which covered two thirds of every PLM/graph
        // shot with a sign-in step nobody was verifying. Scenarios that DO want it call
        // forceOnboardingForScenario, which sets requestOpen directly and is unaffected.
        const char* shot = std::getenv("CHUDWARE_SHOT");
        const bool headlessShot = shot && *shot;
        if (s_wizard.completedVersion < kOnboardingVersion && !headlessShot)
            s_wizard.requestOpen = true;
    }
    if (s_wizard.requestOpen) {
        ImGui::OpenPopup("Welcome to Chudware");
        s_wizard.requestOpen = false;
        s_wizard.open = true;
    }
    if (!s_wizard.open) return;

    const float sc = uiScale;
    ImGuiViewport* viewport = ImGui::GetMainViewport();
    const float width = std::min(820.0f * sc, viewport->WorkSize.x - 40.0f * sc);
    const float height = std::min(690.0f * sc, viewport->WorkSize.y - 40.0f * sc);
    ImGui::SetNextWindowSize(ImVec2(width, height), ImGuiCond_Appearing);
    ImGui::SetNextWindowPos(viewport->GetCenter(), ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
    if (!ImGui::BeginPopupModal("Welcome to Chudware", nullptr,
                                ImGuiWindowFlags_NoResize | ImGuiWindowFlags_NoCollapse)) {
        return;
    }

    ImGui::PushStyleColor(ImGuiCol_Text, Palette::toU32(palette.accent));
    ImGui::Text("Step %d of 4", s_wizard.step + 1);
    ImGui::PopStyleColor();
    ImGui::SameLine();
    ImGui::ProgressBar((s_wizard.step + 1) / 4.0f, ImVec2(-1, 0), "");
    ImGui::Dummy(ImVec2(0, 8.0f * sc));

    const float footerReserve = 64.0f * sc;
    ImGui::BeginChild("##onboarding_body", ImVec2(0, -footerReserve),
                      ImGuiChildFlags_AlwaysUseWindowPadding);
    switch (s_wizard.step) {
        case 0: drawAccountStep(st); break;
        case 1: drawPowerStep(st); break;
        case 2: drawTestStep(st, appBusy); break;
        default: drawWorkspaceStep(); break;
    }
    ImGui::EndChild();
    drawFooter(st);
    ImGui::EndPopup();
}

}  // namespace ui
