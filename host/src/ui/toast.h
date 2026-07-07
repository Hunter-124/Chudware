// Toast notification system (W1-B).
//
// A tiny transient-notification queue + renderer. Any code holding an AppState& can post a toast
// via st.toast(level, msg); the queue lives on AppState (see core/app_state.h) and is drawn once
// per frame, top-left of the viewport below the ribbon, by ui::drawToasts() in app.cpp after the
// main panels.
//
// Rendered as stacked opaque cards with a bold severity-colored outline (error=red, warn=amber,
// success=green, info=neutral), a drawn severity icon, newest on top, auto-expiring (Info 4s,
// Error 7s, Warn between). Identical consecutive toasts collapse into one card with an xN count
// badge; at most kToastMaxVisible cards show at once with a "+N more" summary row for the rest.
// Single click anywhere on a card copies its message to the clipboard and dismisses it instantly.
// No dependency on any subsystem — failure paths across the codebase route here so silent
// swallows become visible.
#pragma once

#include <functional>
#include <string>
#include <vector>

struct AppState;

namespace ui {

enum class ToastLevel { Info, Success, Warn, Error };

// One queued toast. born = ImGui::GetTime() when first seen by drawToasts; ttl is the level's
// lifetime in seconds. count > 1 means identical consecutive toasts were collapsed into this one.
struct Toast {
    ToastLevel level = ToastLevel::Info;
    std::string msg;
    double born = 0.0;   // seconds (ImGui clock) when posted; 0 = not yet stamped
    double ttl = 4.0;    // lifetime in seconds (Info/Success 4, Warn 5, Error 7)
    int count = 1;       // collapsed-duplicate count (rendered as an xN badge when > 1)
    // Optional single action button (additive — empty label = a plain toast, unchanged behaviour).
    // Rendered as a small button on the card; clicking runs action(st) then dismisses the toast.
    // Used e.g. for the auto-branch "Discard draft" affordance. Toasts carrying an action do NOT
    // collapse with duplicates and get a longer ttl so the action stays reachable.
    std::string actionLabel;
    std::function<void(AppState&)> action;
};

// Default time-to-live for a level (seconds).
inline double toastTtl(ToastLevel l) {
    switch (l) {
        case ToastLevel::Error: return 7.0;
        case ToastLevel::Warn:  return 5.0;
        default:                return 4.0;  // Info + Success
    }
}

// Max simultaneously visible toast cards; older ones wait behind a "+N more" row.
inline constexpr int kToastMaxVisible = 4;

// Draw + age the toast queue. Called once per frame from app.cpp after the panels are drawn.
// Expired toasts are removed; a clicked toast is dismissed (click is swallowed so it doesn't
// fall through to the viewport). No-op when the queue is empty.
void drawToasts(AppState& st);

// Post a toast carrying a single action button (additive over st.toast). The action runs on the
// GL thread when the user clicks the button; the toast then dismisses. Used for reversible-action
// affordances like the auto-branch "Discard draft". The callback must NOT synchronously post a
// toast (the toast queue lock is held while it runs) — use st.rpc.call / async work, which posts
// from its completion callback on a later frame.
void toastWithAction(AppState& st, ToastLevel level, std::string msg,
                     std::string actionLabel, std::function<void(AppState&)> action);

}  // namespace ui
