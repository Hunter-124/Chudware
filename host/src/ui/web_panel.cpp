// web_panel.cpp — the "Web" title-bar chip's full-window in-app browser.
//
// See web_panel.h. Toggled by st.showWeb (title_bar.cpp), drawn full-band by app.cpp.
// Reuses the ui::IWebViewBackend seam (in-process CEF) exactly like the Parts Library
// vendor sign-in panel, so texture upload / letterboxing / input forwarding / native
// <select> + clipboard all come from the shared backend.
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <string>

#include "imgui.h"

#include "core/app_state.h"
#include "ui_theme.h"
#include "ui/layout.h"                 // ui::overlayBand
#include "ui/web_panel.h"
#include "ui/web_capabilities.h"  // ui::webGapReason (web-build "desktop only" copy)
#include "ui/widgets/web_view_backend.h"

namespace ui {

namespace {

// Where a fresh Web tab lands. Overridable so a deployment can point it at an intranet
// start page; falls back to a plain, dependency-free search home.
std::string homeUrl() {
    if (const char* e = std::getenv("CHUDWARE_WEB_HOME")) {
        if (e[0]) return std::string(e);
    }
    if (const char* e = std::getenv("CHUDWARE_WEB_HOME")) {
        if (e[0]) return std::string(e);
    }
    return "https://www.google.com";
}

// Function-local static so the page survives toggling the Web chip off and back on —
// the backend (and its live CEF browser) is created once, on first activation, and
// kept alive in the background while the user is modeling.
struct WebPanel {
    std::unique_ptr<ui::IWebViewBackend> web;
    bool  opened = false;   // homeUrl() has been navigated at least once
    char  urlBuf[2048] = {0};
    bool  urlEditing = false;
};

WebPanel& P() {
    static WebPanel p;
    return p;
}

}  // namespace

void drawWebBrowserPanel(AppState& st) {
    WebPanel& w = P();

    // Lazily build the backend on first use — no CEF cost until the user opens Web once.
    if (!w.web) w.web = ui::makeWebView();

    const OverlayBand& band = overlayBand();
    if (!band.valid) return;

    const float sc = ui::uiScale;
    const float m  = 12.0f;
    // A browser wants the full width: fill to the band's right edge (unlike PLM/Parts,
    // which reserve the docked Copilot column).
    ImGui::SetNextWindowPos(ImVec2(band.minX + m, band.minY + m), ImGuiCond_Always);
    ImGui::SetNextWindowSize(ImVec2((band.maxX - band.minX) - 2.0f * m,
                                    (band.maxY - band.minY) - 2.0f * m),
                             ImGuiCond_Always);
    ImGui::Begin("##WebWorkspace", nullptr,
                 ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                 ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse |
                 ImGuiWindowFlags_NoBringToFrontOnFocus | ImGuiWindowFlags_NoNav |
                 ImGuiWindowFlags_NoSavedSettings);

    // If the backend can't host a browser, say so plainly instead of showing an inert
    // black rectangle. Web (WASM) build: the gap table owns the "desktop only" copy.
    // SDK-less desktop CI build → NullWebView: keep the build-flavor message.
    if (!w.web->available()) {
        if (const char* why = ui::webGapReason("web_browser")) {
            ImGui::TextWrapped("%s", why);
        } else {
            ImGui::TextWrapped(
                "The in-app browser isn't available in this build (compiled without the "
                "embedded Chromium runtime).");
        }
        if (ImGui::Button("Close")) st.showWeb = false;
        ImGui::End();
        return;
    }

    // First open: navigate to the home page. Kept out of the toggle path so re-opening
    // the chip returns to wherever the user browsed to, not the home page.
    if (!w.opened) {
        w.web->open(st, homeUrl(), /*isUrl=*/true);
        w.opened = true;
    }

    // Background work: frame + input polling (must run every visible frame).
    w.web->update(st);

    // ---- top bar (30dp): back / fwd / reload | editable URL | Home | Close ----------------
    const float barH = 30.0f * sc;
    ImGui::BeginChild("##web_bar", ImVec2(0, barH), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    {
        const float availW = ImGui::GetContentRegionAvail().x;
        const float fh = ImGui::GetFrameHeight();       // URL input height
        const float th = ImGui::GetTextLineHeight();     // small-button height
        auto vcenter   = [&](float h) { ImGui::SetCursorPosY((barH - h) * 0.5f); };
        auto bsz       = [&](const char* t) {
            return ImGui::CalcTextSize(t).x + ImGui::GetStyle().FramePadding.x * 2.0f;
        };

        // Right cluster (Home + Close), reserved up front so the URL field flexes.
        const float rightW = bsz("Home") + 6.0f * sc + bsz("Close") + 4.0f * sc;

        // Back / Forward / Reload. CEF exposes nav chrome; the null backend never gets here.
        vcenter(th);
        ImGui::BeginDisabled(!w.web->canGoBack());
        if (ImGui::SmallButton("<")) w.web->goBack(st);
        ImGui::EndDisabled();
        ImGui::SameLine(0.0f, 2.0f * sc); vcenter(th);
        ImGui::BeginDisabled(!w.web->canGoForward());
        if (ImGui::SmallButton(">")) w.web->goForward(st);
        ImGui::EndDisabled();
        ImGui::SameLine(0.0f, 2.0f * sc); vcenter(th);
        if (ImGui::SmallButton("Reload")) w.web->reload(st);

        // URL field flexes into the gap before the right cluster.
        ImGui::SameLine(0.0f, 8.0f * sc);
        const float urlW = availW - ImGui::GetCursorPosX() - rightW - 8.0f * sc;
        if (urlW > 60.0f * sc) {
            if (!w.urlEditing)   // mirror the live URL while the user isn't typing
                std::snprintf(w.urlBuf, sizeof(w.urlBuf), "%s", w.web->url().c_str());
            vcenter(fh);
            ImGui::SetNextItemWidth(urlW);
            const bool submit = ImGui::InputText(
                "##web_url", w.urlBuf, sizeof(w.urlBuf),
                ImGuiInputTextFlags_EnterReturnsTrue);
            w.urlEditing = ImGui::IsItemActive();
            if (submit && w.urlBuf[0]) {
                // Bare "example.com" (no scheme) → https:// so the address bar is forgiving.
                std::string u = w.urlBuf;
                if (u.find("://") == std::string::npos &&
                    u.find(' ') == std::string::npos && u.find('.') != std::string::npos)
                    u = "https://" + u;
                w.web->navigate(st, u);
                w.urlEditing = false;
            }
        }

        // Right cluster: Home + Close, right-aligned.
        ImGui::SameLine();
        ImGui::SetCursorPosX(availW - rightW);
        vcenter(th);
        if (ImGui::SmallButton("Home")) w.web->navigate(st, homeUrl());
        ImGui::SameLine(0.0f, 6.0f * sc);
        vcenter(th);
        if (ImGui::SmallButton("Close")) {
            st.showWeb = false;
            ImGui::EndChild();
            ImGui::End();
            return;
        }
    }
    ImGui::EndChild();

    // ---- body: the embedded page ----------------------------------------------------------
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    if (w.web->starting() && !w.web->active()) {
        ImDrawList* dl  = ImGui::GetWindowDrawList();
        const ImVec2 p0 = ImGui::GetCursorScreenPos();
        dl->AddRectFilled(p0, ImVec2(p0.x + avail.x, p0.y + avail.y),
                          Palette::toU32(palette.panelBg), 6.0f * sc);
        const char* msg = "Loading…";
        const ImVec2 tsz = ImGui::CalcTextSize(msg);
        dl->AddText(ImVec2(p0.x + (avail.x - tsz.x) * 0.5f, p0.y + (avail.y - tsz.y) * 0.5f),
                    Palette::toU32(palette.textDim), msg);
    } else if (!w.web->lastError().empty()) {
        ImGui::Spacing();
        ImGui::TextWrapped("Couldn't load the page: %s", w.web->lastError().c_str());
        if (ImGui::Button("Retry")) w.web->reload(st);
        ImGui::SameLine();
        if (ImGui::Button("Go Home")) w.web->navigate(st, homeUrl());
    } else {
        w.web->draw(st, avail);
    }

    ImGui::End();
}

}  // namespace ui
