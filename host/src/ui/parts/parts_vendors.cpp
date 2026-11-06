// Parts Library vendor status, account management, download queue and health checks.
// The compact footer keeps polling while the Sources popup is closed.
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <initializer_list>
#include <string>
#include <memory>
#include <utility>
#include <vector>

#include "imgui.h"

#include "core/app_state.h"
#include "ui_theme.h"
#include "ui/metrics.h"
#include "ui/widgets/kit.h"
#ifdef CHUDWARE_ENABLE_CEF
#include "ui/widgets/cef/cef_rpc_bridge.h"
#include "ui/widgets/cef/cef_web_view.h"
#endif

#include "parts_internal.h"
#include "parts_widgets.h"

namespace ui {
namespace parts {

// Defined in parts_browser_panel.cpp with external linkage so the vendor pair shares
// one definition each. Deliberately NOT in parts_internal.h — they are private to
// these two TUs; promote them if a third ever needs them.
//   jstr: null-tolerant string read. nlohmann's value(key, "") THROWS type_error.302
//         on a present-but-NULL key, and an exception raised inside an st.rpc drain
//         callback aborts the GUI — every string read of a worker response goes
//         through this.
//   sitePrefersDesktopLogin: the one bot-check rule (Magento invisible reCAPTCHA
//         rejects the in-panel OSR CEF); it used to be duplicated inline here.
std::string jstr(const nlohmann::json& j, const char* key, const std::string& dflt);
bool sitePrefersDesktopLogin(const std::string& site);

#ifdef CHUDWARE_ENABLE_CEF
// Resolve the same login URLs used by the wizard, then export the matching
// domains from its panel profile without opening or navigating a browser.
void restoreVendorSessions(AppState& st) {
    struct Pending {
        size_t remaining = 0;
        std::vector<std::pair<std::string, std::vector<std::string>>> sites;
    };
    auto pending = std::make_shared<Pending>();
    for (const auto& vendor : S().vendors)
        if (vendor.loggedIn && !vendor.id.empty())
            ++pending->remaining;
    if (!pending->remaining) return;
    for (const auto& vendor : S().vendors) {
        if (!vendor.loggedIn || vendor.id.empty()) continue;
        const std::string site = vendor.id;
        cef::rpc_start_url(st, site,
            [&st, pending, site](bool ok, const std::string& url,
                                 const std::string&, const std::vector<std::string>& domains,
                                 const std::string&) {
                if (ok && !url.empty() && !domains.empty()) pending->sites.emplace_back(site, domains);
                if (--pending->remaining == 0)
                    CefWebView::restoreVendorSessions(st, pending->sites);
            });
    }
}
#endif

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
                     (r.result.is_object() ? jstr(r.result, "error", "unknown")
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
    st.rpc.call("vendors_health_status", {}, [&st](const core::RpcResult& r) {
        S().healthPolling = false;
        // EVERY exit below must leave the state machine consistent: startHealthCheck
        // and both trigger buttons gate on !healthRunning, so a branch that returns
        // with it still set wedges the widget on "Testing…" until the app restarts.
        if (!r.ok) {
            if (r.err.find("unknown method") != std::string::npos)
                S().healthSupported = false;
            else
                st.toast(ui::ToastLevel::Error, "Vendor health check failed: " + r.err);
            S().healthRunning = false;
            return;
        }
        if (!r.result.is_object() || !r.result.value("ok", false)) {
            // Transport succeeded but the sweep answered {"ok": false, "error": …}
            // (gui_rpc's own except-branch). Same terminal state as a transport error.
            S().healthRunning = false;
            st.toast(ui::ToastLevel::Error, "Vendor health check failed: " +
                     (r.result.is_object() ? jstr(r.result, "error", "unknown")
                                           : std::string("bad response")));
            return;
        }
        S().healthRunning = r.result.value("running", false);
        S().healthChecked = true;
        std::vector<HealthRow> rows;
        if (r.result.contains("results"))
            for (auto& x : r.result["results"]) {
                HealthRow h;
                h.site      = jstr(x, "site", "");
                h.ok        = x.value("ok", false);
                h.expected  = x.value("expected", false);
                h.latencyMs = x.value("latency_ms", 0.0);
                h.error     = jstr(x, "error", "");
                rows.push_back(std::move(h));
            }
        S().healthResults = std::move(rows);
    });
}

namespace {

// palette role → ImVec4 for PushStyleColor. Every colour in this TU is a role lookup
// (scripts/gui_lint.py --rule theme bans literals); this keeps the call sites one line
// instead of the four-line ImVec4 construction that used to be copied per status word.
inline ImVec4 roleV4(const float role[4], float a = 1.0f) {
    return ImVec4(role[0], role[1], role[2], role[3] * a);
}

// One-line coloured status word. The vendor/provider/queue/health rows all print a bare
// state word; painting each by role is what makes a failed row readable at a glance
// instead of dissolving into the surrounding dim chrome.
void textRole(const float role[4], const char* s) {
    ImGui::PushStyleColor(ImGuiCol_Text, roleV4(role));
    ImGui::TextUnformatted(s);
    ImGui::PopStyleColor();
}

}  // namespace

float vendorStripHeight(float) {
    return ImGui::GetFrameHeight() + 8.0f * ui::uiScale;
}

// ---- compact strip (fixed footer of the filters pane) --------------------------------

void drawVendorStrip(AppState& st, float paneW) {
    const float sc = ui::uiScale;
    (void)paneW;

    // -- background polling (always runs while the parts workspace is visible) --------
#ifdef CHUDWARE_ENABLE_CEF
    static bool sessionsRestored = false;
    if (!sessionsRestored && S().vendorsLoaded && S().vendorsError.empty()) {
        sessionsRestored = true;
        restoreVendorSessions(st);
    }
#endif
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

    const float rowW = ImGui::GetContentRegionAvail().x;
    const float buttonW = ImGui::CalcTextSize("Sources…").x +
                          2.0f * ImGui::GetStyle().FramePadding.x;
    const float gap = 6.0f * sc;
    const float statusW = std::max(0.0f, rowW - buttonW - gap);
    const ImVec2 p = ImGui::GetCursorScreenPos();
    const float rowH = ImGui::GetFrameHeight();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    int active = 0, failed = 0;
    for (const auto& job : S().queue) {
        if (job.status == "pending" || job.status == "running") ++active;
        else if (job.status == "error") ++failed;
    }
    std::string status;
    if (!S().vendorsError.empty()) status = "Sources offline";
    else if (!S().vendorsLoaded && S().vendors.empty()) status = "Checking sources…";
    else status = std::to_string(signedIn) + "/" + std::to_string(S().vendors.size()) +
                  " signed in";
    if (!S().queueError.empty()) status = "Queue error · " + status;
    else if (active || failed)
        status = "Q " + std::to_string(active) + "/" + std::to_string(failed) +
                 " failed · " + status;

    const float dotD = 6.0f * sc, dotGap = 3.0f * sc;
    const int dots = std::min((int)S().vendors.size(),
        statusW >= 100.0f * sc ? 2 : (statusW >= 45.0f * sc ? 1 : 0));
    float x = p.x;
    for (int i = 0; i < dots; ++i) {
        const auto& v = S().vendors[(size_t)i];
        const float* role = !v.loginError.empty() ? palette.statusErr
                           : v.loggedIn ? palette.statusOk : palette.textDisabled;
        dl->AddCircleFilled(ImVec2(x + dotD * 0.5f, p.y + rowH * 0.5f),
                            dotD * 0.5f, Palette::toU32(role));
        x += dotD + dotGap;
    }
    const float textW = std::max(0.0f, statusW - (x - p.x));
    const std::string visible = ui::kit::elide(status.c_str(), textW);
    dl->AddText(ImVec2(x, p.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f),
                Palette::toU32(!S().queueError.empty() || failed ? palette.statusErr
                                                                  : palette.textDim),
                visible.c_str());
    ImGui::Dummy(ImVec2(statusW, rowH));
    if (ImGui::IsItemHovered()) {
        if (!S().queueError.empty())
            ImGui::SetTooltip("%s\nQueue status: %s", status.c_str(), S().queueError.c_str());
        else
            ImGui::SetTooltip("%s\nQueue: %d active, %d failed", status.c_str(), active, failed);
    }
    ImGui::SameLine(0.0f, gap);
    const bool wizardOpen = browserPanelActive();
    ImGui::BeginDisabled(wizardOpen);
    if (ImGui::Button("Sources…", ImVec2(buttonW, rowH)))
        S().vendorsPopupOpen = !S().vendorsPopupOpen;
    ImGui::EndDisabled();
    if (wizardOpen && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
        ImGui::SetTooltip("Close the sign-in wizard to manage sources.");
}

// ---- floating "Vendor Accounts" popup ------------------------------------------------

namespace {

void drawVendorSignInList(AppState& st) {
    const float sc = ui::uiScale;
    sectionHeader("Vendor Sites", /*firstInPane=*/true, "vendor_globe");

    if (S().vendorsLoading && S().vendors.empty()) {
        ContentInset inset;
        spinner("Checking");
    } else if (S().vendors.empty()) {
        ContentInset inset;
        ImGui::TextDisabled("No vendor adapters registered.");
    } else {
        // A logo lane is reserved for EVERY row as soon as any one favicon is resident
        // (S().thumbs["vendorlogo:"+id], prefetched elsewhere — this file only draws what is
        // already there). Reserving per-row instead would leave the "[+]" column ragged; with
        // nothing cached at all the list falls back to exactly its old presentation.
        const float logoSz = 18.0f * sc;
        bool anyLogo = false;
        for (auto& lv : S().vendors) {
            auto it = S().thumbs.find("vendorlogo:" + lv.id);
            if (it != S().thumbs.end() && it->second.tex) { anyLogo = true; break; }
        }
        const float logoLane = anyLogo ? logoSz + 6.0f * sc : 0.0f;
        for (auto& v : S().vendors) {
            ImGui::PushID(v.id.c_str());
            // Row rect measured exactly as borderRowSurface measures it, so a failed
            // sign-in can be washed red over the WHOLE row: a broken vendor has to be
            // visible without reading the state word at the bottom of the row.
            const ImVec2 rmn = ImGui::GetCursorScreenPos();
            const ImVec2 rmx(rmn.x + ImGui::GetContentRegionAvail().x,
                             rmn.y + ImGui::GetFrameHeight() * 2.0f);
            borderRowSurface(false, 2.0f, 7.0f);
            if (!v.loginError.empty())
                ui::glassCardWash(ImGui::GetWindowDrawList(), rmn, rmx, 7.0f,
                                  palette.statusErr, 0.18f);
            ContentInset inset;   // row content, inside the full-bleed surface above
            // Favicon lead-in, painted straight into the draw list at the row origin with the
            // row's content indented past it — so the state word on the row's SECOND line
            // clears the logo too instead of tucking under it.
            if (logoLane > 0.0f) {
                auto lg = S().thumbs.find("vendorlogo:" + v.id);
                if (lg != S().thumbs.end() && lg->second.tex) {
                    lg->second.lastUse = ImGui::GetTime();  // LRU: keep on-screen logos resident
                    const ImVec2 lp = ImGui::GetCursorScreenPos();
                    const float  ly = lp.y + (ImGui::GetTextLineHeight() - logoSz) * 0.5f;
                    ImGui::GetWindowDrawList()->AddImage(
                        (ImTextureID)(intptr_t)lg->second.tex,
                        ImVec2(lp.x, ly), ImVec2(lp.x + logoSz, ly + logoSz));
                }
                ImGui::Indent(logoLane);
            }
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
            const char* lbl = v.loginInProgress ? "..." : (v.loggedIn ? "Re-login" : "Sign in");
            // Right-aligned on the row's content edge, measured: the old fixed 80 px left
            // "Sign in" and "Re-login" ending at different x and ignored the right inset.
            ImGui::SameLine();
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x -
                                 ImGui::CalcTextSize(lbl).x -
                                 ImGui::GetStyle().FramePadding.x * 2.0f -
                                 ui::metrics::partsPadX());
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palette.button[0], palette.button[1],
                       palette.button[2], 0.80f));
            // Magento + invisible reCAPTCHA (Anaheim) scores the in-panel OSR CEF
            // as a bot under software GL — open real desktop Chromium for those.
            // Everyone else gets the embedded CEF wizard first. One rule, one
            // definition (parts_browser_panel.cpp).
            const bool preferDesktop = sitePrefersDesktopLogin(v.id);
            // A row already mid sign-in must LOOK unavailable: this button used to
            // stay live while startLoginWizard's active guard ate the click.
            ImGui::BeginDisabled(v.loginInProgress);
            if (ImGui::SmallButton(lbl)) {
                if (preferDesktop) {
                    startVendorLogin(st, v);
                    st.toast(ui::ToastLevel::Info,
                             "System Chrome opened (no automation). Allow cookies, sign in, "
                             "wait for My Account, then close the window.");
                } else {
                    startLoginWizard(st, v.id);
                }
            }
            ImGui::EndDisabled();
            if (v.loginInProgress && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                ImGui::SetTooltip("Sign-in already in progress");
            ImGui::PopStyleColor();

            // The state word carries the row's state: one dim colour for all four made
            // "Login failed" read exactly like "Signed in".
            if (v.loginInProgress)
                textRole(palette.statusWarn, "  Waiting for browser sign-in...");
            else if (!v.loginError.empty())
                textRole(palette.statusErr, "  Login failed (hover)");
            else
                textRole(v.loggedIn ? palette.statusOk : palette.textDisabled,
                         v.loggedIn ? "  Signed in" : "  Not signed in");
            if (!v.loginError.empty() && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", v.loginError.c_str());

            ImGui::EndGroup();
            if (logoLane > 0.0f) ImGui::Unindent(logoLane);
            ImGui::Spacing();
            ImGui::PopID();
        }
    }
}

void drawApiProviders(AppState& st) {
    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    sectionHeader("API Providers", /*firstInPane=*/false, "settings");

    if (S().apiProviders.empty()) {
        ContentInset inset;
        ImGui::TextDisabled("No API providers available.");
        return;
    }
    for (auto& a : S().apiProviders) {
        ImGui::PushID(a.id.c_str());
        borderRowSurface(false, a.editing ? (2.2f + (float)a.fields.size()) : 1.6f, 7.0f);
        ContentInset inset;   // row content, inside the full-bleed surface above
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
        const char* albl = a.editing ? "Close" : (a.configured ? "Edit" : "Set up");
        ImGui::SameLine();
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x -
                             ImGui::CalcTextSize(albl).x -
                             ImGui::GetStyle().FramePadding.x * 2.0f -
                             ui::metrics::partsPadX());
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(palette.button[0], palette.button[1],
                   palette.button[2], 0.80f));
        if (ImGui::SmallButton(albl))
            a.editing = !a.editing;
        ImGui::PopStyleColor();

        textRole(a.configured ? palette.statusOk : palette.textDisabled,
                 a.configured ? "  Configured" : "  Not configured");

        if (a.editing) {
            for (size_t i = 0; i < a.fields.size() && i < 2; ++i) {
                ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x -
                                        ui::metrics::partsPadX());
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
    sectionHeader("Download Queue", /*firstInPane=*/false, "save");
    if (!S().queueError.empty()) {
        ContentInset inset;
        textRole(palette.statusErr, ("Queue status unavailable: " + S().queueError).c_str());
    }

    if (S().queue.empty()) {
        ContentInset inset;
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::TextUnformatted(S().queueError.empty() ? "No downloads pending."
                                                       : "Queue status is unavailable.");
        ImGui::PopStyleColor();
        if (ImGui::SmallButton("Refresh"))
            pollQueue(st);
        return;
    }
    {
        ContentInset inset;
        if (ImGui::SmallButton("Refresh"))
            pollQueue(st);
    }
    ImGui::Spacing();
    for (auto& q : S().queue) {
        ImGui::PushID(q.id.c_str());
        // Status-coloured wash over the row rect (measured as borderRowSurface measures
        // it): the queue is scanned, not read, so state has to be a colour first.
        const float* qt = q.status == "done"    ? palette.statusOk
                        : q.status == "running" ? palette.accent
                        : q.status == "error"   ? palette.statusErr
                                                : palette.statusWarn;
        const ImVec2 qmn = ImGui::GetCursorScreenPos();
        const ImVec2 qmx(qmn.x + ImGui::GetContentRegionAvail().x,
                         qmn.y + ImGui::GetFrameHeight() * 1.8f);
        borderRowSurface(false, 1.8f, 7.0f);
        ui::coloredRowWash(ImGui::GetWindowDrawList(), qmn, qmx, 6.0f, qt, 0.28f);
        ContentInset inset;   // row content, inside the full-bleed surface above
        ImGui::BeginGroup();
        std::string label = q.query;
        if (label.size() > 34) label = label.substr(0, 31) + "...";
        ImGui::Text("%-34.34s", label.c_str());
        ImGui::SameLine();
        ImGui::TextDisabled("%s", q.site.c_str());
        if (q.status == "done") {
            textRole(palette.statusOk, "  Done");
            if (!q.artifactPath.empty() && q.artifactKind == "cad" && q.cadValidated) {
                ImGui::SameLine();
                if (q.importing) {
                    spinner("Importing");
                } else if (ImGui::SmallButton("Import into doc")) {
                    importQueuedArtifact(st, q.id, q.artifactPath);
                }
            } else if (!q.artifactPath.empty() && q.artifactKind == "cad" &&
                       !q.cadValidated) {
                ImGui::SameLine();
                ImGui::TextDisabled("Import unavailable");
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("This CAD file has not passed import validation.");
            }
        } else if (q.status == "error") {
            textRole(palette.statusErr, "  Failed");
            if (!q.error.empty() && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", q.error.c_str());
        } else if (q.status == "cancelled") {
            // Cancelled is a non-success terminal state, so it takes the middle role
            // rather than dissolving into the row chrome like a caption.
            textRole(palette.statusWarn, "  Cancelled");
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
        if (q.status == "error" && (!q.askUser.empty() || !q.candidates.empty())) {
            ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x);
            if (!q.askUser.empty()) ImGui::TextUnformatted(q.askUser.c_str());
            if (q.importing) ImGui::TextDisabled("Selecting…");
            ImGui::BeginDisabled(q.importing);
            ImGui::BeginDisabled(!q.canSelect);
            for (size_t i = 0; i < q.candidates.size(); ++i) {
                const auto& candidate = q.candidates[i];
                ImGui::PushID((int)i);
                const auto it = q.candidateLabels.find(candidate);
                const std::string& fullLabel =
                    it == q.candidateLabels.end() ? candidate : it->second;
                const std::string label = ui::kit::elide(
                    fullLabel.c_str(), ImGui::GetContentRegionAvail().x);
                if (ImGui::Selectable(label.c_str()))
                    selectQueuedArtifact(st, q.id, candidate);
                if (!q.canSelect && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                    ImGui::SetTooltip("The original search result is unavailable");
                else if (label != fullLabel && ImGui::IsItemHovered())
                    ImGui::SetTooltip("%s", fullLabel.c_str());
                ImGui::PopID();
            }
            ImGui::EndDisabled();
            if (!q.candidates.empty() && ImGui::Button("Do not download"))
                dismissQueuedChoice(q.id);
            ImGui::EndDisabled();
            ImGui::PopTextWrapPos();
        }
        ImGui::Spacing();
        ImGui::PopID();
    }
}

void drawHealthTable(AppState& st) {
    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    sectionHeader("Vendor Health", /*firstInPane=*/false, "health_check");
    const float sc = ui::uiScale;

    if (!S().healthSupported) {
        ContentInset inset;
        ImGui::TextDisabled("Health check unavailable — the worker does not support it yet.");
        return;
    }
    // The popup can no longer be on screen while the wizard owns the results column
    // (drawVendorsPopup stands down), so "busy" is the only reachable gate here.
    const bool busy = S().healthStarting || S().healthRunning;
    // Everything below is plain content (no full-bleed row surface), so one scope puts the
    // button, the states and every result row on the section's text line.
    ContentInset inset;
    ImGui::BeginDisabled(busy);
    if (ImGui::SmallButton(busy ? "Running…" : "Run health check"))
        startHealthCheck(st);
    ImGui::EndDisabled();
    if (busy && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
        ImGui::SetTooltip("A health check is already running.");
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
        // State reads as a coloured dot rather than a bracket glyph: green ok, amber
        // expected sign-in-required failure, red real error.
        const float* role = h.ok ? palette.statusOk
                          : h.expected ? palette.statusWarn
                                       : palette.statusErr;
        const float dotD  = 8.0f * sc;
        const float lineH = ImGui::GetTextLineHeight();
        ImDrawList* dl    = ImGui::GetWindowDrawList();
        const ImVec2 c    = ImGui::GetCursorScreenPos();
        dl->AddCircleFilled(ImVec2(c.x + dotD * 0.5f, c.y + lineH * 0.5f), dotD * 0.5f,
                            Palette::toU32(role));
        ImGui::Dummy(ImVec2(dotD, lineH));
        ImGui::SameLine(0.0f, 6.0f * sc);
        ImGui::TextUnformatted(h.site.c_str());
        char rt[32];
        if (h.ok) std::snprintf(rt, sizeof(rt), "%.0f ms", h.latencyMs);
        else      std::snprintf(rt, sizeof(rt), "%s",
                                h.expected ? "sign-in required" : "error");
        // Right-aligned on the section's content edge, measured: the old fixed 78 px left
        // "12 ms" and "sign-in required" ending at different x.
        ImGui::SameLine();
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x -
                             ImGui::CalcTextSize(rt).x - ui::metrics::partsPadX());
        textRole(palette.textDim, rt);
        if (!h.error.empty() && ImGui::IsItemHovered())
            ImGui::SetTooltip("%s", h.error.c_str());
        ImGui::PopID();
    }
}

}  // namespace

void drawVendorsPopup(AppState& st) {
    // The embedded wizard owns the results column — never float this on top of it.
    if (!S().vendorsPopupOpen || browserPanelActive()) return;
    const float sc = ui::uiScale;
    const float popW = 420.0f * sc;

    ImGui::SetNextWindowSize(ImVec2(popW, 540.0f * sc), ImGuiCond_FirstUseEver);
    ui::pushGlassWindowStyle();
    if (ImGui::Begin("Vendor Accounts##parts_vendors_popup", &S().vendorsPopupOpen,
                     ImGuiWindowFlags_NoCollapse)) {
        if (ImGui::Button("Connect accounts"))
            startLoginWizard(st);
        drawVendorSignInList(st);
        drawApiProviders(st);
        drawDownloadQueue(st);
        drawHealthTable(st);
    }
    ImGui::End();
    ui::popGlassWindowStyle();
}

}  // namespace parts
}  // namespace ui
