// ui::makeWebView — backend for the embedded browser (see web_view_backend.h).
//
// The vendor sign-in browser is the in-process CEF off-screen backend
// (cef/cef_web_view.h). The old worker-side JPEG-screencast backend
// (ScreencastWebView) was removed when the migration to CEF landed (2026-07-07).
//
// A build WITHOUT CHUDWARE_ENABLE_CEF has no browser engine at all, so makeWebView
// returns a NullWebView that renders an honest "requires the CEF build" message
// instead of failing to compile — the CEF SDK is a heavy, provisioned dependency
// (docs/cef_webview.md), so SDK-less dev/CI builds still link, they just don't
// carry the embedded browser.
#include "web_view_backend.h"

#ifdef CHUDWARE_ENABLE_CEF
#include "cef/cef_web_view.h"  // CefWebView
#else
#include "imgui.h"
#include "ui_theme.h"
#endif

namespace ui {

#ifndef CHUDWARE_ENABLE_CEF
namespace {
// Build guard: compiled without the CEF backend → no embedded browser. Draw a
// clear message rather than a black panel; every accessor is inert.
class NullWebView final : public IWebViewBackend {
public:
    void open(AppState&, const std::string&, bool) override {}
    void attach(AppState&, const std::string&) override {}
    void close(AppState&) override {}
    void update(AppState&) override {}
    bool draw(AppState&, ImVec2 avail) override {
        ImDrawList* dl  = ImGui::GetWindowDrawList();
        const ImVec2 p0 = ImGui::GetCursorScreenPos();
        const char* msg = "Embedded browser requires the CEF build (CHUDWARE_ENABLE_CEF).";
        const ImVec2 ts = ImGui::CalcTextSize(msg);
        dl->AddText(ImVec2(p0.x + (avail.x - ts.x) * 0.5f, p0.y + avail.y * 0.5f),
                    Palette::toU32(palette.textDisabled), msg);
        ImGui::SetCursorScreenPos(p0);
        ImGui::Dummy(avail);
        return false;
    }
    const std::string& url() const override        { return s_; }
    const std::string& loginState() const override { return unknown_; }
    bool active() const override                   { return false; }
    bool starting() const override                 { return false; }
    const std::string& lastError() const override  { return s_; }
    bool available() const override                { return false; }

private:
    std::string s_;
    std::string unknown_ = "unknown";
};
}  // namespace
#endif

std::unique_ptr<IWebViewBackend> makeWebView() {
#ifdef CHUDWARE_ENABLE_CEF
    return std::make_unique<CefWebView>();
#else
    return std::make_unique<NullWebView>();
#endif
}

}  // namespace ui
