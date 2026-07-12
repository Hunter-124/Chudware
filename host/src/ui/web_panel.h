// web_panel.h — the "Web" title-bar chip's full-window in-app browser.
//
// A general-purpose Chromium tab living inside Billet: the Web chip (title_bar.cpp)
// toggles st.showWeb, and app.cpp draws this panel over the whole central band while
// it is set, so the user can browse and tab straight back to modeling. The page stays
// alive between toggles (the backend is a function-local static, created lazily on the
// first open), so switching away and back does not reload.
//
// It reuses the exact same embedded-browser seam as the Parts Library vendor sign-in
// (ui::IWebViewBackend / ui::makeWebView → in-process CEF), so all texture upload,
// letterboxing, input forwarding and <select>/clipboard handling come for free.
#pragma once

struct AppState;

namespace ui {

// Draw the full-window web browser (top nav bar + letterboxed page). Call once per
// frame while st.showWeb is true; hosts its own borderless central-band window.
void drawWebBrowserPanel(AppState& st);

}  // namespace ui
