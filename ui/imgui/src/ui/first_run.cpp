// See first_run.h.
#include "first_run.h"

#include "bootstrap.h"
#include "gallery_panel.h"
#include "toast.h"

#include "app/app_state.h"

#include "imgui.h"

#include <nlohmann/json.hpp>

#include <atomic>
#include <filesystem>
#include <fstream>
#include <thread>

namespace ui {

namespace {

std::atomic<bool> gRepairInProgress{false};
bool gChecked = false;   // maybeOpenFirst run has run its one-time config.json probe
bool gDialogOpen = false;
int  gConsentChoice = 0;  // 0=off, 1=meta, 2=full — index into kConsentValues
bool gOfferSample = true;

constexpr const char* kConsentValues[3] = {"off", "meta", "full"};
constexpr const char* kConsentLabels[3] = {
    "Off - no telemetry (default)",
    "Meta - anonymous timings/model names only, never geometry",
    "Full - includes geometry references (opt in for team dogfooding)",
};

std::string configPath() { return appdataRoot() + "/config.json"; }

// Best-effort read; a missing/corrupt file is treated as an empty config (fresh install), same
// as services/agent/config.py:load_config()'s except-swallow.
nlohmann::json readConfig() {
    std::ifstream f(configPath());
    if (!f) return nlohmann::json::object();
    try {
        nlohmann::json j;
        f >> j;
        return j.is_object() ? j : nlohmann::json::object();
    } catch (...) {
        return nlohmann::json::object();
    }
}

// Read-merge-write: never clobbers keys this dialog doesn't own (provider/api_key/etc. set by
// other flows) — same "config.json is ambient user state" contract as the Python side.
void writeConsent(const std::string& tier) {
    nlohmann::json cfg = readConfig();
    cfg["telemetry_consent"] = tier;
    std::ofstream f(configPath());
    if (f) f << cfg.dump(2);
}

int indexOfTier(const std::string& tier) {
    for (int i = 0; i < 3; ++i)
        if (tier == kConsentValues[i]) return i;
    return 0;  // unknown/absent -> off, matches config.py:consent_tier()'s safe default
}

// T-U9: samples/mech_part/bracket.aicm (T-Q3) — a real, R1-editable engine sample, opened via
// the worker (requestOpenSample -> doc_open) rather than the old docs/contracts/fixtures/box.aicm
// demo-scene fixture (s.scene->loadAicm(), a legacy non-editable preview path). Same
// relative-candidate search style as scenarios.cpp's setupPickShot()/findBoxAicm().
bool findSampleAicm(std::string& out) {
    const char* candidates[] = {
        "samples/mech_part/bracket.aicm",
        "../samples/mech_part/bracket.aicm",
        "../../samples/mech_part/bracket.aicm",
        "../../../samples/mech_part/bracket.aicm",
    };
    for (const char* c : candidates) {
        if (std::filesystem::exists(c)) { out = std::filesystem::absolute(c).string(); return true; }
    }
    return false;
}

}  // namespace

void maybeOpenFirstRun(app::AppState& /*s*/) {
    if (gChecked) return;
    gChecked = true;
    if (!readConfig().contains("telemetry_consent")) {
        gDialogOpen = true;
        ImGui::OpenPopup("Welcome to AI-CAD##firstrun");
    }
}

void drawFirstRunDialog(app::AppState& s) {
    if (!gDialogOpen) return;

    const ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(ImVec2(vp->Pos.x + vp->Size.x * 0.5f, vp->Pos.y + vp->Size.y * 0.5f),
                            ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
    ImGui::SetNextWindowSize(ImVec2(480, 0), ImGuiCond_Appearing);

    bool stayOpen = true;
    if (ImGui::BeginPopupModal("Welcome to AI-CAD##firstrun", &stayOpen,
                               ImGuiWindowFlags_AlwaysAutoResize | ImGuiWindowFlags_NoSavedSettings)) {
        ImGui::TextWrapped("Welcome! Before you start, a couple of one-time settings.");
        ImGui::Dummy(ImVec2(0, 8));

        ImGui::SeparatorText("Telemetry");
        ImGui::TextWrapped(
            "Help improve AI-CAD by sharing anonymous usage data? This is OFF by default and "
            "never includes your part geometry unless you choose Full.");
        for (int i = 0; i < 3; ++i)
            ImGui::RadioButton(kConsentLabels[i], &gConsentChoice, i);
        ImGui::Dummy(ImVec2(0, 8));

        ImGui::SeparatorText("Sample project");
        ImGui::Checkbox("Open a sample part to explore", &gOfferSample);
        ImGui::Dummy(ImVec2(0, 12));

        if (ImGui::Button("Get started", ImVec2(140, 0))) {
            writeConsent(kConsentValues[gConsentChoice]);
            triggerRuntimeRepair(false);  // silent first-run bootstrap, non-blocking
            if (gOfferSample) {
                std::string aicm;
                if (findSampleAicm(aicm)) {
                    // Real, R1-editable open via the worker (doc_open) — queued and retried by
                    // ui::tickGalleryOpenRequest() every frame until the engine/worker has
                    // finished starting (it's mid-bootstrap right after first-run closes).
                    requestOpenSample(aicm);
                }
                openGalleryPanel();  // show the full gallery too, in case another sample fits better
            }
            gDialogOpen = false;
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Skip", ImVec2(100, 0))) {
            writeConsent(kConsentValues[0]);  // explicit "off" record so this dialog never reopens
            gDialogOpen = false;
            ImGui::CloseCurrentPopup();
        }
        ImGui::EndPopup();
    }
    if (!stayOpen) gDialogOpen = false;
}

void triggerRuntimeRepair(bool force) {
    bool expected = false;
    if (!gRepairInProgress.compare_exchange_strong(expected, true)) {
        postToast(ToastLevel::Warn, "Runtime setup is already in progress");
        return;
    }
    postToast(ToastLevel::Info,
              force ? "Repairing runtime... this can take a few minutes"
                    : "Setting up the runtime... this can take a few minutes");
    std::thread([force]() {
        std::string log;
        const bool ok = force ? repairRuntime(log) : ensureRuntime(log);
        postToast(ok ? ToastLevel::Info : ToastLevel::Error,
                  ok ? ("Runtime ready (" + log + ")") : ("Runtime setup failed: " + log));
        gRepairInProgress.store(false);
    }).detach();
}

bool runtimeRepairInProgress() { return gRepairInProgress.load(); }

int currentConsentIndex() { return indexOfTier(readConfig().value("telemetry_consent", "off")); }

void setConsentIndex(int idx) {
    if (idx < 0 || idx > 2) return;
    writeConsent(kConsentValues[idx]);
}

const char* consentLabel(int idx) { return (idx >= 0 && idx < 3) ? kConsentLabels[idx] : ""; }

}  // namespace ui
