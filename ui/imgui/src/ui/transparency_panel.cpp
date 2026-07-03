// T-U8 Model & telemetry transparency panel — see transparency_panel.h.
//
// Architecture (pattern library §0/§1 — mirrors requirements_panel.cpp):
//   - View-model: aicad::ui::TransparencyModel (ui/abstract/transparency_view.h)
//   - Scaffold: ui::drawPanelScaffold / ui::drawPanelEmptyState (T-U1 kit)
//   - Worker RPC: s.rpc.call("serving_status") via AsyncRpc (read-only)
//   - Canary opt-in: s.worker.call("serving_set_canary") — synchronous toggle
//   - Consent: currentConsentIndex() / setConsentIndex() from first_run.h
//
// Sections:
//   1. Consent tier  — radio buttons (off/meta/full); disabled when offline forced-off
//   2. Which brain   — per-insertion-point provider/model/adapter table; "not serving"
//      when routes empty or worker unreachable
//   3. Canary opt-in — per-route toggle when canary_pct > 0; shows opt_in_count
//   4. Sidecars      — crash bundle dir (T-Q2) + logging sidecar dir (T-H7)
//
// Erik-ratified rules:
//   - Consent default OFF; offline forces OFF (radio disabled, not hidden).
//   - Canary opt-in default OFF. UI never calls serving_promote / serving_evaluate.
//   - serving_set_canary is only offered per-route when canary_pct > 0.
//   - Style: theme tokens only — no literal ImVec4 / IM_COL32.
#include "transparency_panel.h"
#include "panel_scaffold.h"
#include "transparency_view.h"   // ui/abstract/ (no ImGui includes there)
#include "first_run.h"
#include "settings_modal.h"      // ui::appSettings().offlineMode

#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/tokens.h"
#include "async_rpc.h"           // core::RpcResult

#include "imgui.h"
#include <nlohmann/json.hpp>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

// Platform paths for sidecar dirs
#include "platform/paths.h"

// POSIX filesystem for sidecar scan
#ifndef _WIN32
#  include <dirent.h>
#  include <sys/stat.h>
#endif

namespace ui {

namespace {

// ---- Panel state ------------------------------------------------------------
bool gTransOpen = false;
ui::PanelScaffoldState gTransScaffoldState;
aicad::ui::TransparencyModel gTransModel;

// ---- Filesystem helpers (POSIX only; Linux-first build) --------------------

static bool dirExists(const std::string& p) {
#ifndef _WIN32
    struct stat st {};
    return stat(p.c_str(), &st) == 0 && S_ISDIR(st.st_mode);
#else
    (void)p; return false;
#endif
}

static int countFilesInDir(const std::string& dir) {
#ifndef _WIN32
    DIR* d = opendir(dir.c_str());
    if (!d) return 0;
    int n = 0;
    struct dirent* ent;
    while ((ent = readdir(d)) != nullptr) {
        if (ent->d_name[0] == '.') continue;
        std::string full = dir + "/" + ent->d_name;
        struct stat st {};
        if (stat(full.c_str(), &st) == 0 && S_ISREG(st.st_mode)) ++n;
    }
    closedir(d);
    return n;
#else
    (void)dir; return 0;
#endif
}

// Scan the known sidecar directories and populate model.sidecars.
static void scanSidecars(aicad::ui::TransparencyModel& m) {
    m.sidecars.clear();
    const std::string root = platform::appdataRoot();

    auto addEntry = [&](const std::string& label, const std::string& subpath) {
        aicad::ui::SidecarEntry e;
        e.label  = label;
        e.path   = root + "/" + subpath;
        e.exists = dirExists(e.path);
        if (e.exists) {
            e.file_count = countFilesInDir(e.path);
            e.is_empty   = (e.file_count == 0);
        }
        m.sidecars.push_back(std::move(e));
    };

    addEntry("Crash bundles (T-Q2)",      "crashes");
    addEntry("Telemetry sidecars (T-H7)", "telemetry/sidecars");
    m.sidecars_scanned = true;
}

// ---- JSON parsing -----------------------------------------------------------

static void parseServingStatus(const nlohmann::json& res,
                               aicad::ui::TransparencyModel& m) {
    m.routes.clear();
    m.serving_reachable = res.value("ok", false);

    if (!res.contains("routes") || !res["routes"].is_array()) return;

    for (const auto& r : res["routes"]) {
        aicad::ui::ServingRouteRow row;
        row.insertion_point = r.value("insertion_point", std::string());
        row.provider        = r.value("provider",        std::string());
        row.model           = r.value("model",           std::string());
        row.adapter_version = r.value("adapter_version", std::string());
        row.canary_pct      = (float)r.value("canary_pct", 0.0);
        row.opt_in_count    = r.value("opt_in_count",    0);
        row.is_canary       = (row.canary_pct > 0.f);

        // Check if local machine_id is in the opt_in list (serving_status may
        // include the full list; opt_in_count is always present).
        row.user_opted_in = false;
        if (!m.machine_id.empty() && r.contains("opt_in") &&
                r["opt_in"].is_array()) {
            for (const auto& uid : r["opt_in"]) {
                if (uid.is_string() &&
                        uid.get<std::string>() == m.machine_id) {
                    row.user_opted_in = true;
                    break;
                }
            }
        }
        m.routes.push_back(std::move(row));
    }
}

// ---- Section renderers ------------------------------------------------------

// 1. Consent tier
static void drawConsentSection(app::AppState& s) {
    ImGui::TextColored(TVEC(txt1), "Telemetry Consent");
    ImGui::Separator();
    ImGui::Spacing();

    bool forced_off = gTransModel.offline_mode;

    if (forced_off) {
        ImGui::TextColored(TVEC(txt2),
            "Forced OFF \xe2\x80\x94 offline mode active.");
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("While offline mode is enabled the telemetry "
                              "tier is locked to OFF regardless of this setting.");
        ImGui::Spacing();
    }

    if (forced_off) ImGui::BeginDisabled();

    int consent = ui::currentConsentIndex();
    bool changed = false;
    for (int i = 0; i < 3; ++i) {
        changed |= ImGui::RadioButton(ui::consentLabel(i), &consent, i);
        if (i < 2) ImGui::SameLine();
    }
    if (changed) ui::setConsentIndex(consent);

    if (forced_off) ImGui::EndDisabled();

    ImGui::Spacing();
    ImGui::TextColored(TVEC(txt2),
        "Controls what data AI-CAD may send for improvement. OFF is the default.");
    ImGui::Spacing();
    (void)s;
}

// 2. Which-brain table
static void drawWhichBrainSection() {
    ImGui::TextColored(TVEC(txt1), "Active AI Brain");
    ImGui::Separator();
    ImGui::Spacing();

    if (!gTransModel.serving_reachable && gTransModel.routes.empty()) {
        ImGui::TextColored(TVEC(txt2),
            "Not serving \xe2\x80\x94 worker offline or no routes configured");
        ImGui::Spacing();
        return;
    }

    if (gTransModel.routes.empty()) {
        ImGui::TextColored(TVEC(txt2), "No model routes active");
        ImGui::Spacing();
        return;
    }

    // Table: insertion point | provider | model | adapter | canary%
    const float S = theme::S();
    ImGuiTableFlags tflags = ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                             ImGuiTableFlags_SizingStretchProp |
                             ImGuiTableFlags_ScrollY;
    float tableH = ImGui::GetTextLineHeightWithSpacing() *
                   (float)(gTransModel.routes.size() + 1.5f);
    if (tableH > 160.f * S) tableH = 160.f * S;

    if (ImGui::BeginTable("##brain_table", 4, tflags, ImVec2(0, tableH))) {
        ImGui::TableSetupScrollFreeze(0, 1);
        ImGui::TableSetupColumn("Endpoint", ImGuiTableColumnFlags_WidthStretch, 0.22f);
        ImGui::TableSetupColumn("Provider", ImGuiTableColumnFlags_WidthStretch, 0.18f);
        ImGui::TableSetupColumn("Model",    ImGuiTableColumnFlags_WidthStretch, 0.42f);
        ImGui::TableSetupColumn("Canary",   ImGuiTableColumnFlags_WidthStretch, 0.18f);
        ImGui::TableHeadersRow();

        for (const auto& r : gTransModel.routes) {
            ImGui::TableNextRow();
            ImGui::TableSetColumnIndex(0);
            ImGui::TextUnformatted(r.insertion_point.c_str());
            ImGui::TableSetColumnIndex(1);
            ImGui::TextUnformatted(r.provider.c_str());
            ImGui::TableSetColumnIndex(2);
            if (r.adapter_version.empty()) {
                ImGui::TextUnformatted(r.model.c_str());
            } else {
                ImGui::Text("%s  [%s]",
                    r.model.c_str(), r.adapter_version.c_str());
            }
            ImGui::TableSetColumnIndex(3);
            if (r.is_canary) {
                ImGui::TextColored(TVEC(warn), "%.0f%%", (double)r.canary_pct);
            } else {
                ImGui::TextColored(TVEC(txt2), "stable");
            }
        }
        ImGui::EndTable();
    }
    ImGui::Spacing();
}

// 3. Canary early-adopter opt-in
static void drawCanarySection(app::AppState& s) {
    // Only render if at least one route has canary_pct > 0
    bool any_canary = false;
    for (const auto& r : gTransModel.routes)
        if (r.is_canary) { any_canary = true; break; }
    if (!any_canary) return;

    ImGui::TextColored(TVEC(txt1), "Early-Adopter Canary Opt-In");
    ImGui::Separator();
    ImGui::Spacing();
    ImGui::TextColored(TVEC(txt2),
        "Opt in per endpoint to receive experimental model builds "
        "ahead of the stable track.");
    ImGui::Spacing();

    for (auto& r : gTransModel.routes) {
        if (!r.is_canary) continue;

        bool opted = r.user_opted_in;
        char label[256];
        std::snprintf(label, sizeof(label),
            "%s  (opt-in count: %d)##canary_%s",
            r.insertion_point.c_str(), r.opt_in_count,
            r.insertion_point.c_str());

        if (ImGui::Checkbox(label, &opted)) {
            // Build the opt_in list: just our machine_id when opting in,
            // empty when opting out. Server replaces the list for our slot.
            nlohmann::json req;
            req["insertion_point"] = r.insertion_point;
            req["pct"]             = (double)r.canary_pct;
            nlohmann::json optInList = nlohmann::json::array();
            if (opted && !gTransModel.machine_id.empty())
                optInList.push_back(gTransModel.machine_id);
            req["opt_in"] = optInList;

            nlohmann::json res;
            std::string err;
            if (s.worker.call("serving_set_canary", req, res, err, 5000)) {
                r.user_opted_in = opted;
                r.opt_in_count  = res.value("opt_in_count", r.opt_in_count);
                gTransScaffoldState.statusText = r.insertion_point +
                    (opted ? ": canary opted in" : ": canary opted out");
                gTransScaffoldState.statusIsError = false;
                std::printf("transparency: serving_set_canary %s opted=%d\n",
                            r.insertion_point.c_str(), (int)opted);
            } else {
                gTransScaffoldState.statusText    = "Canary toggle failed: " + err;
                gTransScaffoldState.statusIsError = true;
                std::printf("transparency: serving_set_canary failed: %s\n",
                            err.c_str());
            }
            std::fflush(stdout);
        }
    }

    ImGui::Spacing();
    ImGui::TextColored(TVEC(warn),
        "Canary builds may have reduced stability. "
        "Auto-promotion requires human sign-off.");
    ImGui::Spacing();
}

// 4. Telemetry sidecar dirs
static void drawSidecarSection() {
    ImGui::TextColored(TVEC(txt1), "Telemetry Sidecars");
    ImGui::Separator();
    ImGui::Spacing();

    if (!gTransModel.sidecars_scanned) {
        ImGui::TextColored(TVEC(txt2), "Scanning...");
        return;
    }

    for (const auto& e : gTransModel.sidecars) {
        ImGui::TextColored(e.exists ? TVEC(txt1) : TVEC(txt2),
                           "%s", e.label.c_str());
        ImGui::SameLine();
        if (!e.exists) {
            ImGui::TextColored(TVEC(txt2), "(not found)");
        } else if (e.is_empty) {
            ImGui::TextColored(TVEC(good), "(empty)");
        } else {
            ImGui::TextColored(TVEC(warn), "(%d file%s)",
                               e.file_count, e.file_count == 1 ? "" : "s");
        }
        ImGui::TextColored(TVEC(txt2), "%s", e.path.c_str());
        ImGui::Spacing();
    }
}

// ---- Main content draw (inside scaffold) ------------------------------------

static void drawContent(app::AppState& s) {
    // Sync offline_mode each frame for coherence
    gTransModel.offline_mode  = ui::appSettings().offlineMode;
    gTransModel.consent_index = ui::currentConsentIndex();

    // Scan sidecar dirs once on first draw (cheap stat calls, no worker needed).
    if (!gTransModel.sidecars_scanned)
        scanSidecars(gTransModel);

    // Trigger serving_status RPC if idle and worker is ready.
    if (gTransModel.state == aicad::ui::TransparencyPanelState::Idle &&
            s.workerStarted) {
        gTransModel.state = aicad::ui::TransparencyPanelState::Loading;
        gTransScaffoldState.statusText    = "Loading\xe2\x80\xa6";
        gTransScaffoldState.statusIsError = false;

        s.rpc.call("serving_status", nlohmann::json::object(),
            [](const core::RpcResult& r) {
                if (!r.ok) {
                    gTransModel.state             = aicad::ui::TransparencyPanelState::Error;
                    gTransModel.errorMsg          = r.err;
                    gTransModel.serving_reachable = false;
                    gTransScaffoldState.statusText    = "Serving status unavailable";
                    gTransScaffoldState.statusIsError = true;
                    return;
                }
                parseServingStatus(r.result, gTransModel);
                gTransModel.state = aicad::ui::TransparencyPanelState::Ready;
                const int nr = (int)gTransModel.routes.size();
                gTransScaffoldState.statusIsError = false;
                gTransScaffoldState.statusText =
                    nr == 0 ? "No active routes"
                            : std::to_string(nr) + " route" +
                              (nr == 1 ? "" : "s") + " active";
            });
    }

    // Loading placeholder
    if (gTransModel.state == aicad::ui::TransparencyPanelState::Loading) {
        // Show consent while loading — it doesn't depend on serving_status
        drawConsentSection(s);
        ImGui::Separator();
        ImGui::Spacing();
        ui::drawPanelEmptyState("Loading\xe2\x80\xa6", "Querying model serving status");
        return;
    }

    // Section 1 — Consent (always present)
    drawConsentSection(s);
    ImGui::Separator();
    ImGui::Spacing();

    // Section 2 — Which brain (draws "not serving" honestly when no routes)
    drawWhichBrainSection();

    // Section 3 — Canary opt-in (hidden when no canary routes)
    if (!gTransModel.routes.empty()) {
        ImGui::Separator();
        ImGui::Spacing();
        drawCanarySection(s);
    }

    // Section 4 — Sidecar dirs
    ImGui::Separator();
    ImGui::Spacing();
    drawSidecarSection();
}

} // anonymous namespace

// ---- Public API -------------------------------------------------------------

void drawTransparencyPanel(app::AppState& s) {
    ui::PanelScaffoldSpec spec;
    spec.id        = "transparency";
    spec.title     = "Model & Telemetry";
    spec.hasSearch = false;

    ui::drawPanelScaffold(spec, gTransScaffoldState, [&]() {
        drawContent(s);
    });
}

void openTransparencyPanel() {
    gTransOpen = true;
    // Reset so a fresh serving_status RPC fires on next draw.
    gTransModel = aicad::ui::TransparencyModel{};
    gTransScaffoldState = ui::PanelScaffoldState{};
    // Stable per-install identifier (optional; canary toggle no-ops if unset).
    const char* mid = std::getenv("AICAD_MACHINE_ID");
    if (mid && *mid) gTransModel.machine_id = mid;
}

bool* transparencyPanelOpen() {
    return &gTransOpen;
}

void seedTransparencyPanelForShot(const std::string& serving_status_json) {
    gTransOpen = true;
    gTransModel = aicad::ui::TransparencyModel{};
    gTransScaffoldState = ui::PanelScaffoldState{};

    try {
        nlohmann::json res = nlohmann::json::parse(serving_status_json);
        parseServingStatus(res, gTransModel);
        gTransModel.state             = aicad::ui::TransparencyPanelState::Ready;
        gTransScaffoldState.statusText = std::to_string(gTransModel.routes.size()) +
                                         " route(s) — fixture";
    } catch (...) {
        gTransModel.state    = aicad::ui::TransparencyPanelState::Error;
        gTransModel.errorMsg = "Shot fixture JSON parse error";
        gTransScaffoldState.statusText    = "Parse error";
        gTransScaffoldState.statusIsError = true;
    }

    // Scan real sidecar dirs so screenshot reflects on-disk state.
    scanSidecars(gTransModel);
}

} // namespace ui
