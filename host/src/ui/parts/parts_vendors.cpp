// Parts Library — right pane: vendor sign-in, API pricing credentials, download queue.
// GUI V2 phase A: rows use the border treatment (resting glass + hover/selected border).
#include <cmath>
#include <cstring>
#include <string>

#include "imgui.h"

#include "core/app_state.h"
#include "ui_theme.h"

#include "parts_internal.h"
#include "parts_widgets.h"

namespace ui {
namespace parts {

void drawVendorsPane(AppState& st, float paneW) {
    const float sc = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * sc, 4 * sc));
    ImGui::BeginChild("##parts_vendors", ImVec2(paneW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    sectionHeader("Vendor Sites", /*firstInPane=*/true);

    bool anyLoginBusy = false;
    for (auto& v : S().vendors)
        if (v.loginInProgress) anyLoginBusy = true;

    if (S().vendorsLoading && S().vendors.empty()) {
        spinner("Checking");
    } else if (S().vendors.empty()) {
        ImGui::TextDisabled("No vendor adapters registered.");
    } else {
        for (auto& v : S().vendors) {
            ImGui::PushID(v.id.c_str());
            borderRowSurface(false, 2.0f);
            ImGui::BeginGroup();

            ImVec4 dot = v.loggedIn
                ? ImVec4(palette.statusOk[0],  palette.statusOk[1],
                         palette.statusOk[2],  1.0f)
                : ImVec4(palette.statusErr[0], palette.statusErr[1],
                         palette.statusErr[2], 0.85f);
            if (v.loginInProgress)
                dot = ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f);
            ImGui::PushStyleColor(ImGuiCol_Text, dot);
            ImGui::TextUnformatted(v.loggedIn ? "[+]" : (v.loginInProgress ? "[~]" : "[ ]"));
            ImGui::PopStyleColor();
            ImGui::SameLine();
            ImGui::TextUnformatted(v.label.c_str());
            ImGui::SameLine();
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                                 ImGui::GetContentRegionAvail().x - 80.0f * sc);
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.button[0], palette.button[1],
                       palette.button[2], 0.80f));
            const char* lbl = v.loginInProgress ? "..." : (v.loggedIn ? "Re-login" : "Sign in");
            if (!v.loginInProgress && ImGui::SmallButton(lbl))
                startVendorLogin(st, v);
            ImGui::PopStyleColor();

            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                       palette.textDisabled[2], 1.0f));
            if (v.loginInProgress)
                ImGui::TextUnformatted("  Waiting for browser sign-in...");
            else if (!v.loginError.empty())
                ImGui::TextUnformatted("  Login failed (hover)");
            else
                ImGui::TextUnformatted(v.loggedIn ? "  Signed in" : "  Not signed in");
            ImGui::PopStyleColor();
            if (!v.loginError.empty() && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", v.loginError.c_str());

            ImGui::EndGroup();
            ImGui::Spacing();
            ImGui::PopID();
        }
    }

    // While a headed login window is open, poll status so the dot flips green
    // the moment the user closes the window. Edge-triggered + backed off (2 s).
    if (anyLoginBusy && !S().vendorsLoading &&
        ImGui::GetTime() - S().lastVendorPoll > 2.0)
        loadVendors(st, /*force=*/true);

    // ---- API pricing providers --------------------------------------------------
    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    sectionHeader("API Providers");

    if (S().apiProviders.empty()) {
        ImGui::TextDisabled("No API providers available.");
    } else {
        for (auto& a : S().apiProviders) {
            ImGui::PushID(a.id.c_str());
            borderRowSurface(false, a.editing ? (2.2f + (float)a.fields.size()) : 1.6f);
            ImGui::BeginGroup();

            ImVec4 dot = a.configured
                ? ImVec4(palette.statusOk[0],  palette.statusOk[1],
                         palette.statusOk[2],  1.0f)
                : ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                         palette.textDisabled[2], 0.9f);
            ImGui::PushStyleColor(ImGuiCol_Text, dot);
            ImGui::TextUnformatted(a.configured ? "[+]" : "[ ]");
            ImGui::PopStyleColor();
            ImGui::SameLine();
            ImGui::TextUnformatted(a.label.c_str());
            ImGui::SameLine();
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                                 ImGui::GetContentRegionAvail().x - 80.0f * sc);
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.button[0], palette.button[1],
                       palette.button[2], 0.80f));
            if (ImGui::SmallButton(a.editing ? "Close" : (a.configured ? "Edit" : "Set up")))
                a.editing = !a.editing;
            ImGui::PopStyleColor();

            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                       palette.textDisabled[2], 1.0f));
            ImGui::TextUnformatted(a.configured ? "  Configured" : "  Not configured");
            ImGui::PopStyleColor();

            if (a.editing) {
                for (size_t i = 0; i < a.fields.size() && i < 2; ++i) {
                    ImGui::SetNextItemWidth(paneW - 24.0f * sc);
                    ImGui::InputTextWithHint(
                        (std::string("##cred_") + a.fields[i]).c_str(),
                        a.fields[i].c_str(), a.fieldBuf[i], sizeof(a.fieldBuf[i]),
                        ImGuiInputTextFlags_Password);
                }
                if (a.saving) {
                    spinner("Saving");
                } else if (ImGui::SmallButton("Save")) {
                    saveApiCreds(st, a);
                }
            }

            ImGui::EndGroup();
            ImGui::Spacing();
            ImGui::PopID();
        }
    }

    // ---- Download queue ----------------------------------------------------------
    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    sectionHeader("Download Queue");

    // Poll while active jobs exist (backed off; async — never blocks the frame).
    if (S().queueActive && ImGui::GetTime() - S().lastQueuePoll > 2.5)
        pollQueue(st);

    if (S().queue.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::TextUnformatted("No downloads pending.");
        ImGui::PopStyleColor();
        if (ImGui::SmallButton("Refresh"))
            pollQueue(st);
    } else {
        if (ImGui::SmallButton("Refresh"))
            pollQueue(st);
        ImGui::Spacing();
        for (auto& q : S().queue) {
            ImGui::PushID(q.id.c_str());
            borderRowSurface(false, 1.8f);
            ImGui::BeginGroup();
            std::string label = q.query;
            if (label.size() > 34) label = label.substr(0, 31) + "...";
            ImGui::Text("%-34.34s", label.c_str());
            ImGui::SameLine();
            ImGui::TextDisabled("%s", q.site.c_str());
            if (q.status == "done") {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(palette.statusOk[0], palette.statusOk[1],
                           palette.statusOk[2], 1.0f));
                ImGui::TextUnformatted("  Done");
                ImGui::PopStyleColor();
                if (!q.artifactPath.empty()) {
                    ImGui::SameLine();
                    if (q.importing) {
                        spinner("Importing");
                    } else if (ImGui::SmallButton("Import into doc")) {
                        q.importing = true;
                        nlohmann::json p;
                        p["artifact_path"] = q.artifactPath;
                        st.rpc.call("parts_import_artifact", p,
                            [&st, id = q.id](const core::RpcResult& r) {
                                for (auto& j : S().queue)
                                    if (j.id == id) j.importing = false;
                                bool ok = r.ok && r.result.is_object() &&
                                          r.result.value("ok", false);
                                if (ok) {
                                    st.toast(ui::ToastLevel::Success,
                                        "Imported → " + r.result.value("doc_id", std::string("doc")));
                                } else {
                                    std::string why = r.ok
                                        ? r.result.value("reason", std::string("import failed"))
                                        : r.err;
                                    st.toast(ui::ToastLevel::Error, "Import failed: " + why);
                                }
                            });
                    }
                }
            } else if (q.status == "error") {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(palette.statusErr[0], palette.statusErr[1],
                           palette.statusErr[2], 1.0f));
                ImGui::TextUnformatted("  Failed");
                ImGui::PopStyleColor();
                if (!q.error.empty() && ImGui::IsItemHovered())
                    ImGui::SetTooltip("%s", q.error.c_str());
            } else if (q.status == "cancelled") {
                ImGui::TextDisabled("  Cancelled");
            } else {
                float ph = (float)(0.5 + 0.5 * sin(ImGui::GetTime() * 2.5));
                ImGui::PushStyleColor(ImGuiCol_PlotHistogram,
                    ImVec4(palette.accent[0], palette.accent[1],
                           palette.accent[2], 0.80f));
                ImGui::ProgressBar(ph, ImVec2(-1.0f, 4.0f * sc),
                                   q.status == "running" ? "downloading" : "queued");
                ImGui::PopStyleColor();
            }
            ImGui::EndGroup();
            ImGui::Spacing();
            ImGui::PopID();
        }
    }

    ImGui::EndChild();
}

}  // namespace parts
}  // namespace ui
