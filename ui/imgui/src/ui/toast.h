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
};

inline double toastTtl(ToastLevel l) {
    switch (l) {
    case ToastLevel::Error: return 7.0;
    case ToastLevel::Warn:  return 5.0;
    default:                return 4.0;
    }
}

void postToast(ToastLevel level, std::string msg);
void drawToasts(app::AppState& st);

} // namespace ui
