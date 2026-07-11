// Parts Library — vendors (GUI V2 phase B): the old full right pane is gone. This TU owns
//   * drawVendorStrip  — compact fixed footer of the filters pane: status dots + "N/M signed
//     in", health summary, "Connect vendor accounts", "Vendors…" (popup) and "Test vendors".
//     ALL background polling (login watch, download queue, health) lives here so it keeps
//     running whether or not the popup is open.
//   * drawVendorsPopup — floating glass window with the vendor sign-in list, API-provider
//     credential editors, download queue and health-check results table.
//   * openVendorWizard — opens the embedded-browser sign-in wizard via
//     login_wizard_start; a Playwright/embed setup failure → headed vendor_login fallback.
//   * startHealthCheck/pollHealthStatus — vendors_health_check/_status flow (single-flight,
//     2 s backoff while running).
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>

#include "imgui.h"

#include "core/app_state.h"
#include "ui_theme.h"

#include "parts_internal.h"
#include "parts_widgets.h"

namespace ui {
namespace parts {

// ---- health check flow -------------------------------------------------------------

void startHealthCheck(AppState& st) {
    if (!S().healthSupported || S().healthStarting || S().healthRunning) return;
    S().healthStarting = true;
    st.rpc.call("vendors_health_check", {}, [&st](const core::RpcResult& r) {
        S().healthStarting = false;
        if (!r.ok) {
            if (r.err.find("unknown method") != std::string::npos) {
                S().healthSupported = false;
            } else {
                st.toast(ui::ToastLevel::Error, "Health check failed to start: " + r.err);
            }
            return;
        }
        if (!r.result.is_object() || !r.result.value("ok", false)) {
            st.toast(ui::ToastLevel::Error, "Health check failed to start: " +
                     (r.result.is_object() ? r.result.value("error", std::string("unknown"))
                                           : std::string("bad response")));
            return;
        }
        S().healthRunning  = true;
        S().lastHealthPoll = 0.0;  // poll immediately on the next frame
    });
}

void pollHealthStatus(AppState& st) {
    if (!S().healthSupported || S().healthPolling) return;
    S().healthPolling  = true;
    S().lastHealthPoll = ImGui::GetTime();
    st.rpc.call("vendors_health_status", {}, [](const core::RpcResult& r) {
        S().healthPolling = false;
        if (!r.ok) {
            if (r.err.find("unknown method") != std::string::npos)
                S().healthSupported = false;
            S().healthRunning = false;
            return;
        }
        if (!r.result.is_object() || !r.result.value("ok", false)) return;
        S().healthRunning = r.result.value("running", false);
        S().healthChecked = true;
        std::vector<HealthRow> rows;
        if (r.result.contains("results"))
            for (auto& x : r.result["results"]) {
                HealthRow h;
                h.site      = x.value("site", "");
                h.ok        = x.value("ok", false);
                h.loggedIn  = x.value("logged_in", false);
                h.expected  = x.value("expected", false);
                h.latencyMs = x.value("latency_ms", 0.0);
                h.hitCount  = x.value("hit_count", 0);
                h.error     = x.value("error", "");
                rows.push_back(std::move(h));
            }
        S().healthResults = std::move(rows);
    });
}

// ---- phase C: vendor sign-in wizard --------------------------------------------------
// Launches the embedded-browser login wizard (parts_browser_panel.cpp); startLoginWizard
// falls back to the old headed vendor_login flow when the worker lacks the embed backend.

void openVendorWizard(AppState& st) { startLoginWizard(st); }

// ---- compact strip (fixed footer of the filters pane) --------------------------------

void drawVendorStrip(AppState& st, float paneW) {
    const float sc = ui::uiScale;
    (void)paneW;

    // -- background polling (always runs while the parts workspace is visible) --------
    bool anyLoginBusy = false;
    int  signedIn     = 0;
    for (auto& v : S().vendors) {
        if (v.loginInProgress) anyLoginBusy = true;
        if (v.loggedIn) ++signedIn;
    }
    // While a headed login window is open, poll status so the dot flips green the moment
    // the user closes the window. Edge-triggered + backed off (2 s).
    if (anyLoginBusy && !S().vendorsLoading &&
        ImGui::GetTime() - S().lastVendorPoll > 2.0)
        loadVendors(st, /*force=*/true);
    // Poll the download queue while active jobs exist (backed off; async).
    if (S().queueActive && ImGui::GetTime() - S().lastQueuePoll > 2.5)
        pollQueue(st);
    // Poll health while the backend reports running (single-flight + 2 s backoff).
    // Defer during the embedded login wizard — parallel browser_fetch sweeps starve it.
    if (S().healthRunning && !browserPanelActive() &&
        ImGui::GetTime() - S().lastHealthPoll > 2.0)
        pollHealthStatus(st);

    ImGui::Separator();

    // -- status dot row + "N/M signed in" ---------------------------------------------
    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        ImVec2 p = ImGui::GetCursorScreenPos();
        const float d   = 7.0f * sc;           // dot diameter
        const float gap = 4.0f * sc;
        float x = p.x + d * 0.5f;
        float cy = p.y + 5.0f * sc + d * 0.5f;
        int i = 0;
        for (auto& v : S().vendors) {
            const float* role = v.loggedIn ? palette.statusOk : palette.textDisabled;
            if (!v.loginError.empty()) role = palette.statusErr;
            dl->AddCircleFilled(ImVec2(x, cy), d * 0.5f, Palette::toU32(role));
            // Invisible hover target per dot for the tooltip.
            ImGui::SetCursorScreenPos(ImVec2(x - d * 0.5f, cy - d * 0.5f));
            ImGui::PushID(i++);
            ImGui::InvisibleButton("##dot", ImVec2(d, d));
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("%s — %s", v.label.c_str(),
                                  !v.loginError.empty() ? v.loginError.c_str()
                                  : v.loggedIn          ? "signed in"
                                                        : "not signed in");
            ImGui::PopID();
            x += d + gap;
        }
        ImGui::SetCursorScreenPos(ImVec2(x + 4.0f * sc, p.y + 3.0f * sc));
        if (!S().vendors.empty())
            ImGui::TextDisabled("%d/%d signed in", signedIn, (int)S().vendors.size());
        else
            ImGui::TextDisabled("no vendors");
    }

    // -- one-line health summary --------------------------------------------------------
    if (!S().healthSupported) {
        ImGui::TextDisabled("Health check unavailable (update the worker)");
    } else if (S().healthStarting || (S().healthRunning && S().healthResults.empty())) {
        spinner("Testing vendors");
    } else if (S().healthChecked && !S().healthResults.empty()) {
        int ok = 0, needLogin = 0, failing = 0;
        for (auto& h : S().healthResults) {
            if (h.ok) ++ok;
            else if (h.expected) ++needLogin;
            else ++failing;
        }
        if (failing > 0)
            ImGui::TextDisabled("Health: %d ok · %d need sign-in · %d failing",
                                ok, needLogin, failing);
        else
            ImGui::TextDisabled("Health: %d ok · %d need sign-in", ok, needLogin);
    } else {
        ImGui::TextDisabled(" ");
    }

    // -- buttons -------------------------------------------------------------------------
    if (borderRow("##connect_vendors", "Connect vendor accounts", "browser_panel",
                  /*selected=*/false, ImVec2(0, 22.0f * sc), /*centerText=*/true))
        openVendorWizard(st);

    const float half = (ImGui::GetContentRegionAvail().x - 4.0f * sc) * 0.5f;
    if (borderRow("##vendors_popup", "Vendors…", nullptr, S().vendorsPopupOpen,
                  ImVec2(half, 20.0f * sc), /*centerText=*/true))
        S().vendorsPopupOpen = !S().vendorsPopupOpen;
    ImGui::SameLine(0.0f, 4.0f * sc);
    const bool healthBusy = S().healthStarting || S().healthRunning;
    if (borderRow("##test_vendors", healthBusy ? "Testing…" : "Test vendors", nullptr,
                  /*selected=*/false, ImVec2(half, 20.0f * sc), /*centerText=*/true) &&
        !healthBusy && !browserPanelActive())
        startHealthCheck(st);
}

// ---- floating "Vendor Accounts" popup ------------------------------------------------

namespace {

void drawVendorSignInList(AppState& st) {
    const float sc = ui::uiScale;
    sectionHeader("Vendor Sites", /*firstInPane=*/true);

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
            // Magento + invisible reCAPTCHA (Anaheim) scores the in-panel OSR CEF
            // as a bot under software GL — open real desktop Chromium for those.
            // Everyone else gets the embedded CEF wizard first.
            const bool preferDesktop =
                (v.id == "anaheimautomation");
            if (!v.loginInProgress && ImGui::SmallButton(lbl)) {
                if (preferDesktop) {
                    startVendorLogin(st, v);
                    st.toast(ui::ToastLevel::Info,
                             "System Chrome opened (no automation). Allow cookies, sign in, "
                             "wait for My Account, then close the window.");
                } else {
                    startLoginWizard(st, v.id);
                }
            }
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
}

void drawApiProviders(AppState& st, float paneW) {
    const float sc = ui::uiScale;
    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    sectionHeader("API Providers");

    if (S().apiProviders.empty()) {
        ImGui::TextDisabled("No API providers available.");
        return;
    }
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

void drawDownloadQueue(AppState& st) {
    const float sc = ui::uiScale;
    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    sectionHeader("Download Queue");

    if (S().queue.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::TextUnformatted("No downloads pending.");
        ImGui::PopStyleColor();
        if (ImGui::SmallButton("Refresh"))
            pollQueue(st);
        return;
    }
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

void drawHealthTable(AppState& st) {
    const float sc = ui::uiScale;
    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    sectionHeader("Vendor Health");

    if (!S().healthSupported) {
        ImGui::TextDisabled("Health check unavailable — the worker does not support it yet.");
        return;
    }
    const bool busy = S().healthStarting || S().healthRunning;
    if (ImGui::SmallButton(busy ? "Running…" : "Run health check") && !busy &&
        !browserPanelActive())
        startHealthCheck(st);
    if (busy && S().healthResults.empty()) {
        spinner("Testing");
        return;
    }
    if (S().healthResults.empty()) {
        ImGui::TextDisabled("Not tested this session.");
        return;
    }
    ImGui::Spacing();
    for (auto& h : S().healthResults) {
        ImGui::PushID(h.site.c_str());
        // Badge: green ok, amber expected sign-in-required failure, red real error.
        const float* role = h.ok ? palette.statusOk
                          : h.expected ? palette.statusWarn
                                       : palette.statusErr;
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(role[0], role[1], role[2], 1.0f));
        ImGui::TextUnformatted(h.ok ? "[+]" : (h.expected ? "[!]" : "[x]"));
        ImGui::PopStyleColor();
        ImGui::SameLine();
        ImGui::TextUnformatted(h.site.c_str());
        ImGui::SameLine();
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                             ImGui::GetContentRegionAvail().x - 78.0f * sc);
        if (h.ok)
            ImGui::TextDisabled("%.0f ms", h.latencyMs);
        else
            ImGui::TextDisabled("%s", h.expected ? "sign-in required" : "error");
        if (!h.error.empty() && ImGui::IsItemHovered())
            ImGui::SetTooltip("%s", h.error.c_str());
        ImGui::PopID();
    }
}

}  // namespace

void drawVendorsPopup(AppState& st) {
    if (!S().vendorsPopupOpen) return;
    const float sc = ui::uiScale;
    const float popW = 420.0f * sc;

    ImGui::SetNextWindowSize(ImVec2(popW, 540.0f * sc), ImGuiCond_FirstUseEver);
    ui::pushGlassWindowStyle();
    if (ImGui::Begin("Vendor Accounts##parts_vendors_popup", &S().vendorsPopupOpen,
                     ImGuiWindowFlags_NoCollapse)) {
        drawVendorSignInList(st);
        drawApiProviders(st, popW);
        drawDownloadQueue(st);
        drawHealthTable(st);
    }
    ImGui::End();
    ui::popGlassWindowStyle();
}

}  // namespace parts
}  // namespace ui
