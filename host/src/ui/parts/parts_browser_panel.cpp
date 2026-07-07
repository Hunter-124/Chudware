// Parts Library — embedded vendor-browser panel + sign-in wizard (GUI V2 phase C).
//
// drawPartsLibraryWorkspace swaps the results column for this panel while
// browserPanelActive() is true. The panel hosts a ui::WebView attached to the worker's
// login-wizard embed session (login_wizard_* RPCs) with a top bar: current site + step,
// clipped URL, login-state chip, and Skip / Next / Close actions.
//
// The worker AUTO-ADVANCES the wizard when a site reaches logged_in — the embed_id
// changes under us, so the 2 s status poll watches for it and re-attaches the WebView.
// All RPC is async via st.rpc (GL-freeze history); polls are single-flight + backed off.
#include <algorithm>
#include <cstdio>
#include <string>
#include <vector>

#include "imgui.h"

#include "core/app_state.h"
#include "ui_theme.h"

#include "ui/widgets/web_view.h"

#include "parts_internal.h"
#include "parts_widgets.h"

namespace ui {
namespace parts {

namespace {

struct WizardState {
    ui::WebView web;
    bool active     = false;   // login_wizard session live on the worker
    bool starting   = false;   // login_wizard_start in flight
    bool singleSite = false;   // launched from one vendor row's "Sign in"
    std::string currentSite;
    std::string embedId;
    std::vector<std::string> done, skipped, remaining;
    bool   statusPending = false, actionPending = false;
    double lastStatusAt = 0.0;
};

WizardState& W() {
    static WizardState w;
    return w;
}

std::string vendorLabel(const std::string& site) {
    for (const auto& v : S().vendors)
        if (v.id == site) return v.label;
    return site;
}

void closeWizard(AppState& st, bool sendStop) {
    auto& w = W();
    if (sendStop)
        st.rpc.call("login_wizard_stop", {}, [](const core::RpcResult&) {});
    w.web.close(st);
    w.active = w.starting = false;
    w.currentSite.clear();
    w.embedId.clear();
    w.done.clear();
    w.skipped.clear();
    w.remaining.clear();
    S().browserWizardRequested = false;
    loadVendors(st, /*force=*/true);
}

void finishWizard(AppState& st) {
    auto& w    = W();
    const int n = (int)w.done.size();
    if (n > 0) {
        char buf[64];
        std::snprintf(buf, sizeof(buf), "Signed into %d vendor%s", n, n == 1 ? "" : "s");
        st.toast(ui::ToastLevel::Success, buf);
    } else {
        st.toast(ui::ToastLevel::Info, "Vendor sign-in finished");
    }
    closeWizard(st, /*sendStop=*/true);
}

// Shared parser for the status-shaped responses of login_wizard_status / _next.
void applyWizardStatus(AppState& st, const nlohmann::json& j) {
    auto& w = W();
    if (!w.active) return;

    if (j.contains("done") && j["done"].is_array()) {
        std::vector<std::string> nd;
        for (const auto& s : j["done"])
            if (s.is_string()) nd.push_back(s.get<std::string>());
        // Toast every newly signed-in site (the worker auto-advances on logged_in).
        for (const auto& s : nd)
            if (std::find(w.done.begin(), w.done.end(), s) == w.done.end())
                st.toast(ui::ToastLevel::Success, "✓ " + vendorLabel(s) + " signed in");
        w.done = std::move(nd);
    }
    if (j.contains("skipped") && j["skipped"].is_array()) {
        w.skipped.clear();
        for (const auto& s : j["skipped"])
            if (s.is_string()) w.skipped.push_back(s.get<std::string>());
    }
    if (j.contains("remaining") && j["remaining"].is_array()) {
        w.remaining.clear();
        for (const auto& s : j["remaining"])
            if (s.is_string()) w.remaining.push_back(s.get<std::string>());
    }
    w.currentSite = j.value("current_site", w.currentSite);

    if (j.value("all_done", false) || !j.value("active", true)) {
        finishWizard(st);
        return;
    }
    const std::string embed = j.value("embed_id", "");
    if (!embed.empty() && embed != w.embedId) {   // wizard advanced → new session
        w.embedId = embed;
        w.web.attach(st, embed);
    }
}

void pollWizardStatus(AppState& st) {
    auto& w = W();
    if (w.statusPending) return;
    w.statusPending = true;
    w.lastStatusAt  = ImGui::GetTime();
    st.rpc.call("login_wizard_status", {}, [&st](const core::RpcResult& r) {
        W().statusPending = false;
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) return;
        applyWizardStatus(st, r.result);
    });
}

void wizardNext(AppState& st, bool skip) {
    auto& w = W();
    if (w.actionPending) return;
    w.actionPending = true;
    st.rpc.call("login_wizard_next", nlohmann::json{{"skip", skip}},
                [&st](const core::RpcResult& r) {
        W().actionPending = false;
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", false)) {
            std::string why = r.ok && r.result.is_object()
                                  ? r.result.value("reason", std::string("wizard step failed"))
                                  : r.err;
            st.toast(ui::ToastLevel::Error, "Wizard: " + why);
            return;
        }
        applyWizardStatus(st, r.result);
    });
}

// Login-state chip: borderRow-style pill, colored by state.
void loginStateChip(const std::string& state) {
    const float sc = ui::uiScale;
    const float* role = palette.textDisabled;
    const char*  txt  = "Loading";
    if (state == "logged_in") {
        role = palette.statusOk;
        txt  = "Signed in";
    } else if (state == "login_page") {
        role = palette.statusWarn;
        txt  = "Login page";
    } else if (state == "challenge") {
        role = palette.statusErr;
        txt  = "Challenge — solve it here";
    }
    ImDrawList* dl  = ImGui::GetWindowDrawList();
    const ImVec2 ts = ImGui::CalcTextSize(txt);
    const ImVec2 p  = ImGui::GetCursorScreenPos();
    const float  h  = ts.y + 4.0f * sc;
    const float  wd = ts.x + 14.0f * sc;
    dl->AddRectFilled(p, ImVec2(p.x + wd, p.y + h),
                      ImGui::ColorConvertFloat4ToU32(ImVec4(role[0], role[1], role[2], 0.14f)),
                      h * 0.5f);
    dl->AddRect(p, ImVec2(p.x + wd, p.y + h),
                ImGui::ColorConvertFloat4ToU32(ImVec4(role[0], role[1], role[2], 0.55f)),
                h * 0.5f, 0, 1.0f * sc);
    dl->AddText(ImVec2(p.x + 7.0f * sc, p.y + 2.0f * sc),
                ImGui::ColorConvertFloat4ToU32(ImVec4(role[0], role[1], role[2], 1.0f)), txt);
    ImGui::Dummy(ImVec2(wd, h));
}

}  // namespace

// ---- public: engagement flag ----------------------------------------------------------

bool browserPanelActive() { return S().browserWizardRequested; }

// ---- public: start the wizard (multi-vendor when site empty, else that one site) --------

void startLoginWizard(AppState& st, const std::string& site) {
    auto& w = W();
    if (w.starting || w.active) return;
    w.starting                 = true;
    w.singleSite               = !site.empty();
    S().browserWizardRequested = true;   // engage the panel immediately (shows "starting")

    nlohmann::json p = nlohmann::json::object();
    if (!site.empty()) p["sites"] = nlohmann::json::array({site});

    st.rpc.call("login_wizard_start", p, [&st, site](const core::RpcResult& r) {
        auto& w    = W();
        w.starting = false;
        const bool ok = r.ok && r.result.is_object() && r.result.value("ok", false);
        if (!ok) {
            // Embed backend missing / failed → old headed vendor_login fallback.
            S().browserWizardRequested = false;
            if (!site.empty()) {
                for (auto& v : S().vendors)
                    if (v.id == site && !v.loginInProgress) {
                        startVendorLogin(st, v);
                        return;
                    }
                st.toast(ui::ToastLevel::Error, "Sign-in unavailable for " + site);
                return;
            }
            for (auto& v : S().vendors) {
                if (v.loggedIn || v.loginInProgress) continue;
                startVendorLogin(st, v);
                return;
            }
            st.toast(ui::ToastLevel::Info, "All vendor sites are already signed in.");
            return;
        }
        if (r.result.value("all_done", false)) {
            S().browserWizardRequested = false;
            st.toast(ui::ToastLevel::Info, "All vendor sites are already signed in.");
            return;
        }
        w.active = true;
        w.done.clear();
        w.skipped.clear();
        w.remaining.clear();
        if (r.result.contains("sites") && r.result["sites"].is_array())
            for (const auto& s : r.result["sites"])
                if (s.is_string()) w.remaining.push_back(s.get<std::string>());
        w.currentSite = r.result.value("current_site", "");
        w.embedId.clear();
        const std::string embed = r.result.value("embed_id", "");
        if (!embed.empty()) {
            w.embedId = embed;
            w.web.attach(st, embed);
        }
        w.lastStatusAt = ImGui::GetTime();
    });
}

// ---- public: the panel -------------------------------------------------------------------

void drawBrowserPanel(AppState& st) {
    auto& w        = W();
    const float sc = ui::uiScale;

    // Background work: WebView frame/input polling + wizard status watch (2 s, single-flight;
    // the worker auto-advances on logged_in — embed_id changes are picked up here).
    w.web.update(st);
    if (w.active && !w.statusPending && ImGui::GetTime() - w.lastStatusAt > 2.0)
        pollWizardStatus(st);

    // ---- top bar (28dp): title + step, chip, clipped URL, right-aligned actions ----------
    const float barH = 28.0f * sc;
    ImGui::BeginChild("##wiz_bar", ImVec2(0, barH), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    {
        const float availW = ImGui::GetContentRegionAvail().x;
        ImGui::SetCursorPosY((barH - ImGui::GetFrameHeight() * 0.8f) * 0.5f - 1.0f * sc);

        // Right-aligned buttons first (compute reserved width).
        const bool showSkipNext = w.active && !w.singleSite;
        float btnW  = 0.0f;
        auto bsz    = [&](const char* t) {
            return ImGui::CalcTextSize(t).x + ImGui::GetStyle().FramePadding.x * 2.0f;
        };
        if (showSkipNext) btnW += bsz("Skip") + bsz("Next") + 8.0f * sc;
        btnW += bsz("Close") + 4.0f * sc;

        // Title.
        if (S().fonts && S().fonts->medium) ImGui::PushFont(S().fonts->medium);
        std::string title = "Vendor sign-in";
        if (!w.currentSite.empty()) title += " — " + vendorLabel(w.currentSite);
        ImGui::TextUnformatted(title.c_str());
        if (S().fonts && S().fonts->medium) ImGui::PopFont();

        // Step K/N.
        if (w.active) {
            const int doneN = (int)(w.done.size() + w.skipped.size());
            const int total = doneN + (int)w.remaining.size();
            if (total > 0) {
                ImGui::SameLine(0.0f, 8.0f * sc);
                ImGui::TextDisabled("step %d/%d", std::min(doneN + 1, total), total);
            }
        }

        // Login-state chip.
        ImGui::SameLine(0.0f, 10.0f * sc);
        loginStateChip(w.web.loginState());

        // Clipped dim URL (fills the gap up to the buttons).
        const std::string& u = w.web.url();
        if (!u.empty()) {
            ImGui::SameLine(0.0f, 10.0f * sc);
            const float urlW =
                availW - ImGui::GetCursorPosX() - btnW - 12.0f * sc;
            if (urlW > 30.0f * sc) {
                const ImVec2 p = ImGui::GetCursorScreenPos();
                ImGui::GetWindowDrawList()->PushClipRect(
                    p, ImVec2(p.x + urlW, p.y + barH), true);
                ImGui::TextDisabled("%s", u.c_str());
                ImGui::GetWindowDrawList()->PopClipRect();
            }
        }

        // Buttons.
        ImGui::SameLine();
        ImGui::SetCursorPosX(availW - btnW);
        if (showSkipNext) {
            if (ImGui::SmallButton(w.actionPending ? "..." : "Skip") && !w.actionPending)
                wizardNext(st, /*skip=*/true);
            ImGui::SameLine(0.0f, 4.0f * sc);
            if (ImGui::SmallButton("Next") && !w.actionPending)
                wizardNext(st, /*skip=*/false);
            ImGui::SameLine(0.0f, 4.0f * sc);
        }
        if (ImGui::SmallButton("Close")) {
            closeWizard(st, /*sendStop=*/true);
            ImGui::EndChild();
            return;
        }
    }
    ImGui::EndChild();

    // ---- body: the embedded page ----------------------------------------------------------
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    if (w.starting && !w.web.active()) {
        // login_wizard_start still in flight.
        ImDrawList* dl  = ImGui::GetWindowDrawList();
        const ImVec2 p0 = ImGui::GetCursorScreenPos();
        dl->AddRectFilled(p0, ImVec2(p0.x + avail.x, p0.y + avail.y),
                          Palette::toU32(palette.panelBg), 6.0f * sc);
        const char* msg = "Starting sign-in wizard…";
        const ImVec2 ts = ImGui::CalcTextSize(msg);
        dl->AddText(ImVec2(p0.x + (avail.x - ts.x) * 0.5f, p0.y + avail.y * 0.5f),
                    Palette::toU32(palette.textDisabled), msg);
        ImGui::Dummy(avail);
        return;
    }
    w.web.draw(st, avail);
}

}  // namespace parts
}  // namespace ui
