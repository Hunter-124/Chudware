// Toast notification system (W1-B).
//
// A tiny transient-notification queue + renderer. Any code holding an AppState& can post a toast
// via st.toast(level, msg); the queue lives on AppState (see core/app_state.h) and is drawn once
// per frame, top-right of the viewport, by ui::drawToasts() in app.cpp after the main panels.
//
// Rendered as stacked rounded rectangles in theme-role colors, newest on top, auto-expiring
// (Info 4s, Error 7s, Warn between), max 5 visible, click-to-dismiss. No dependency on any
// subsystem — failure paths across the codebase route here so silent swallows become visible.
#pragma once

#include <string>
#include <vector>

struct AppState;

namespace ui {

enum class ToastLevel { Info, Warn, Error };

// One queued toast. born = ImGui::GetTime() when posted; ttl is the level's lifetime in seconds.
struct Toast {
    ToastLevel level = ToastLevel::Info;
    std::string msg;
    double born = 0.0;   // seconds (ImGui clock) when posted
    double ttl = 4.0;    // lifetime in seconds (Info 4, Warn 5, Error 7)
};

// Default time-to-live for a level (seconds).
inline double toastTtl(ToastLevel l) {
    switch (l) {
        case ToastLevel::Error: return 7.0;
        case ToastLevel::Warn:  return 5.0;
        default:                return 4.0;
    }
}

// Draw + age the toast queue. Called once per frame from app.cpp after the panels are drawn.
// Expired toasts are removed; a clicked toast is dismissed. No-op when the queue is empty.
void drawToasts(AppState& st);

}  // namespace ui
