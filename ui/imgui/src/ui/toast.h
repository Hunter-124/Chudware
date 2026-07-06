// Toast notification system — transient queue + renderer.
// Post via ui::postToast(); draw once per frame via ui::drawToasts() (after main panels).
#pragma once

#include <mutex>
#include <string>
#include <vector>

namespace app { struct AppState; }

namespace ui {

enum class ToastLevel { Info, Warn, Error };

struct Toast {
    ToastLevel level = ToastLevel::Info;
    std::string msg;
    double born = 0.0;
    double ttl  = 4.0;
    // Optional action affordance (T-R5): a clickable label rendered under the
    // message (e.g. "Back to rev B (discard draft)"). Clicking it dismisses the
    // toast and publishes actionId — poll with takeToastAction().
    std::string actionLabel;
    int actionId = 0;
};

inline double toastTtl(ToastLevel l) {
    switch (l) {
    case ToastLevel::Error: return 7.0;
    case ToastLevel::Warn:  return 5.0;
    default:                return 4.0;
    }
}

void postToast(ToastLevel level, std::string msg);
// Toast with a clickable action row. Longer ttl by default so the affordance
// is not missed; the click publishes actionId (see takeToastAction).
void postToastAction(ToastLevel level, std::string msg,
                     std::string actionLabel, int actionId, double ttl = 12.0);
// One-shot poll: the actionId of a toast action clicked since the last call
// (0 = none). Call once per frame from the feature that posted the action.
int takeToastAction();
void drawToasts(app::AppState& st);

} // namespace ui
