// chudware-hostd system tray: Windows notification area (Shell_NotifyIconW) or
// Linux + libappindicator3.
//
// The tray runs on a dedicated thread (a Win32 message loop or GTK) so the daemon
// accept loop stays free. Status updates and Quit are marshalled onto that thread.
// When the feature is compiled out or unavailable at runtime, every entry point is
// a no-op.

#include "tray_icon.h"

#include <string>

#if defined(_WIN32)

#include <windows.h>
#include <shellapi.h>

#include <atomic>
#include <cstdlib>
#include <future>
#include <mutex>
#include <thread>

namespace hostd {
namespace {

constexpr UINT kIconMsg = WM_APP + 1;    // notification-area icon callback
constexpr UINT kStatusMsg = WM_APP + 2;  // traySetStatus -> refresh the tooltip
constexpr UINT kIconId = 1;
constexpr UINT kQuitCmd = 1;
constexpr wchar_t kWindowClass[] = L"ChudwareHostdTray";

struct TrayState {
    std::mutex mu;
    std::thread thr;
    std::atomic<bool> started{false};
    HWND hwnd = nullptr;  // guarded by mu; set while the message loop runs
    std::wstring tip = L"Chudware hostd";
    void (*quitFn)(void*) = nullptr;
    void* quitUd = nullptr;
    HICON icon = nullptr;     // tray thread only
    bool ownsIcon = false;    // tray thread only
    UINT taskbarCreated = 0;  // Explorer restarted: the icon must be re-added
};

TrayState g;

bool envDisabled() {
    const char* v = std::getenv("CHUDWARE_HOSTD_TRAY");
    if (!v || !*v) return false;
    return v[0] == '0' || v[0] == 'n' || v[0] == 'N' || v[0] == 'f' || v[0] == 'F';
}

std::wstring widen(const std::string& s) {
    if (s.empty()) return {};
    const int n = MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), nullptr, 0);
    std::wstring out(n > 0 ? n : 0, L'\0');
    if (n > 0) MultiByteToWideChar(CP_UTF8, 0, s.data(), (int)s.size(), out.data(), n);
    return out;
}

NOTIFYICONDATAW iconData(HWND hwnd) {
    NOTIFYICONDATAW nid{};
    nid.cbSize = sizeof(nid);
    nid.hWnd = hwnd;
    nid.uID = kIconId;
    return nid;
}

void copyTip(NOTIFYICONDATAW& nid) {
    std::lock_guard<std::mutex> lk(g.mu);
    wcsncpy_s(nid.szTip, g.tip.c_str(), _TRUNCATE);
}

void addIcon(HWND hwnd) {
    NOTIFYICONDATAW nid = iconData(hwnd);
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP;
    nid.uCallbackMessage = kIconMsg;
    nid.hIcon = g.icon;
    copyTip(nid);
    Shell_NotifyIconW(NIM_ADD, &nid);
    nid.uVersion = NOTIFYICON_VERSION_4;
    Shell_NotifyIconW(NIM_SETVERSION, &nid);
}

void refreshTip(HWND hwnd) {
    NOTIFYICONDATAW nid = iconData(hwnd);
    nid.uFlags = NIF_TIP | NIF_SHOWTIP;
    copyTip(nid);
    Shell_NotifyIconW(NIM_MODIFY, &nid);
}

void showMenu(HWND hwnd) {
    std::wstring tip;
    {
        std::lock_guard<std::mutex> lk(g.mu);
        tip = g.tip;
    }
    HMENU menu = CreatePopupMenu();
    if (!menu) return;
    AppendMenuW(menu, MF_STRING | MF_GRAYED, 0, tip.c_str());
    AppendMenuW(menu, MF_SEPARATOR, 0, nullptr);
    AppendMenuW(menu, MF_STRING, kQuitCmd, L"Quit hostd");
    POINT pt{};
    GetCursorPos(&pt);
    // Without foreground the menu never dismisses on an outside click; the WM_NULL
    // afterwards is the documented companion for the same TrackPopupMenu quirk.
    SetForegroundWindow(hwnd);
    const UINT cmd = TrackPopupMenu(menu, TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON,
                                    pt.x, pt.y, 0, hwnd, nullptr);
    PostMessageW(hwnd, WM_NULL, 0, 0);
    DestroyMenu(menu);
    if (cmd != kQuitCmd) return;
    void (*fn)(void*) = nullptr;
    void* ud = nullptr;
    {
        std::lock_guard<std::mutex> lk(g.mu);
        fn = g.quitFn;
        ud = g.quitUd;
    }
    if (fn) fn(ud);
}

LRESULT CALLBACK trayWndProc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    if (msg == kIconMsg) {
        // NOTIFYICON_VERSION_4: the event is LOWORD(lParam).
        const UINT event = LOWORD(lp);
        if (event == WM_CONTEXTMENU || event == WM_LBUTTONUP) showMenu(hwnd);
        return 0;
    }
    if (msg == kStatusMsg) {
        refreshTip(hwnd);
        return 0;
    }
    if (g.taskbarCreated != 0 && msg == g.taskbarCreated) {
        addIcon(hwnd);
        return 0;
    }
    if (msg == WM_CLOSE) {
        DestroyWindow(hwnd);
        return 0;
    }
    if (msg == WM_DESTROY) {
        NOTIFYICONDATAW nid = iconData(hwnd);
        Shell_NotifyIconW(NIM_DELETE, &nid);
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

void trayThreadMain(std::promise<bool>* ready) {
    HINSTANCE inst = GetModuleHandleW(nullptr);
    // Icon resource 1 is assets/app.ico, embedded by hostd/hostd.rc.
    g.icon = static_cast<HICON>(LoadImageW(inst, MAKEINTRESOURCEW(1), IMAGE_ICON,
                                           GetSystemMetrics(SM_CXSMICON),
                                           GetSystemMetrics(SM_CYSMICON), LR_DEFAULTCOLOR));
    g.ownsIcon = g.icon != nullptr;
    if (!g.icon) g.icon = LoadIconW(nullptr, IDI_APPLICATION);

    WNDCLASSEXW wc{};
    wc.cbSize = sizeof(wc);
    wc.lpfnWndProc = trayWndProc;
    wc.hInstance = inst;
    wc.lpszClassName = kWindowClass;
    RegisterClassExW(&wc);
    g.taskbarCreated = RegisterWindowMessageW(L"TaskbarCreated");
    // A hidden top-level window rather than HWND_MESSAGE: only top-level windows
    // receive the TaskbarCreated broadcast sent when Explorer restarts.
    HWND hwnd = CreateWindowExW(0, kWindowClass, L"Chudware hostd", WS_OVERLAPPED, 0, 0, 0, 0,
                                nullptr, nullptr, inst, nullptr);
    if (!hwnd) {
        if (g.ownsIcon) DestroyIcon(g.icon);
        g.icon = nullptr;
        UnregisterClassW(kWindowClass, inst);
        ready->set_value(false);
        return;
    }
    addIcon(hwnd);
    {
        std::lock_guard<std::mutex> lk(g.mu);
        g.hwnd = hwnd;
    }
    ready->set_value(true);

    MSG msg;
    while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }

    {
        std::lock_guard<std::mutex> lk(g.mu);
        g.hwnd = nullptr;
    }
    if (g.ownsIcon) DestroyIcon(g.icon);
    g.icon = nullptr;
    g.ownsIcon = false;
    UnregisterClassW(kWindowClass, inst);
}

}  // namespace

bool trayStart(const std::string&) {
    if (envDisabled()) return false;
    bool expected = false;
    if (!g.started.compare_exchange_strong(expected, true)) return true;
    std::promise<bool> ready;
    std::future<bool> created = ready.get_future();
    try {
        g.thr = std::thread(trayThreadMain, &ready);
    } catch (...) {
        g.started.store(false);
        return false;
    }
    if (!created.get()) {
        g.thr.join();
        g.started.store(false);
        return false;
    }
    return true;
}

void traySetStatus(TrayStatus, const std::string& tooltip) {
    // Windows reports status through the tooltip and the menu header; the icon keeps
    // the brand mark.
    if (!g.started.load()) return;
    std::lock_guard<std::mutex> lk(g.mu);
    if (!tooltip.empty()) g.tip = widen(tooltip);
    if (g.hwnd) PostMessageW(g.hwnd, kStatusMsg, 0, 0);
}

void traySetQuitHandler(void (*fn)(void*), void* user) {
    std::lock_guard<std::mutex> lk(g.mu);
    g.quitFn = fn;
    g.quitUd = user;
}

void trayStop() {
    // Quit runs on the tray thread and only signals server->stop(); trayStop itself
    // runs on the main thread after server.run returns. Never join from the tray thread.
    HWND hwnd = nullptr;
    {
        std::lock_guard<std::mutex> lk(g.mu);
        hwnd = g.hwnd;
    }
    if (g.thr.joinable() && std::this_thread::get_id() == g.thr.get_id()) {
        if (hwnd) PostMessageW(hwnd, WM_CLOSE, 0, 0);
        return;
    }
    if (hwnd) PostMessageW(hwnd, WM_CLOSE, 0, 0);
    if (g.thr.joinable()) g.thr.join();
    g.started.store(false);
    std::lock_guard<std::mutex> lk(g.mu);
    g.quitFn = nullptr;
    g.quitUd = nullptr;
}

}  // namespace hostd

#elif defined(CHUDWARE_HOSTD_HAS_TRAY) && CHUDWARE_HOSTD_HAS_TRAY

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <mutex>
#include <thread>
#include <vector>

#include <gdk-pixbuf/gdk-pixbuf.h>
#include <gtk/gtk.h>
#include <libappindicator/app-indicator.h>

namespace fs = std::filesystem;

namespace hostd {
namespace {

struct TrayState {
    std::mutex mu;
    std::thread thr;
    std::atomic<bool> started{false};
    std::atomic<bool> stopReq{false};
    AppIndicator* ind = nullptr;
    GtkWidget* menu = nullptr;
    GtkWidget* statusItem = nullptr;
    GdkPixbuf* base = nullptr;
    GdkPixbuf* idlePb = nullptr;
    GdkPixbuf* busyPb = nullptr;
    GdkPixbuf* errorPb = nullptr;
    std::string tip = "Chudware hostd";
    TrayStatus status = TrayStatus::Idle;
    void (*quitFn)(void*) = nullptr;
    void* quitUd = nullptr;
    std::string iconCacheDir;
};

TrayState g;

bool envDisabled() {
    const char* v = std::getenv("CHUDWARE_HOSTD_TRAY");
    if (!v || !*v) return false;
    return v[0] == '0' || v[0] == 'n' || v[0] == 'N' || v[0] == 'f' || v[0] == 'F';
}

bool hasDisplay() {
    const char* d = std::getenv("DISPLAY");
    if (d && *d) return true;
    const char* w = std::getenv("WAYLAND_DISPLAY");
    return w && *w;
}

// Multiply RGB toward a target tint while keeping alpha. Produces status-colored
// variants of the same logo so ImGui-style multiply tinting works from one PNG.
GdkPixbuf* tintPixbuf(GdkPixbuf* src, double tr, double tg, double tb) {
    if (!src) return nullptr;
    GdkPixbuf* out = gdk_pixbuf_copy(src);
    if (!out) return nullptr;
    const int w = gdk_pixbuf_get_width(out);
    const int h = gdk_pixbuf_get_height(out);
    const int stride = gdk_pixbuf_get_rowstride(out);
    const int ch = gdk_pixbuf_get_n_channels(out);
    guchar* pixels = gdk_pixbuf_get_pixels(out);
    for (int y = 0; y < h; ++y) {
        guchar* row = pixels + y * stride;
        for (int x = 0; x < w; ++x) {
            guchar* p = row + x * ch;
            const double a = ch >= 4 ? p[3] / 255.0 : 1.0;
            if (a < 0.01) continue;
            // Lift dark logo pixels toward the tint color.
            const double r = p[0] / 255.0;
            const double g = p[1] / 255.0;
            const double b = p[2] / 255.0;
            const double lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
            const double mix = 0.35 + 0.65 * lum;  // keep some original shading
            p[0] = (guchar)std::min(255.0, (r * (1.0 - mix) + tr * mix) * 255.0);
            p[1] = (guchar)std::min(255.0, (g * (1.0 - mix) + tg * mix) * 255.0);
            p[2] = (guchar)std::min(255.0, (b * (1.0 - mix) + tb * mix) * 255.0);
        }
    }
    return out;
}

std::string findLogoPng(const std::string& selfBinaryPath) {
    std::vector<fs::path> cands;
    std::error_code ec;
    if (!selfBinaryPath.empty()) {
        fs::path exe = fs::path(selfBinaryPath).parent_path();
        cands.push_back(exe / "assets" / "logo_icon.png");
        cands.push_back(exe / ".." / "assets" / "logo_icon.png");
        cands.push_back(exe / ".." / ".." / "assets" / "logo_icon.png");
        cands.push_back(exe / ".." / ".." / ".." / "assets" / "logo_icon.png");
    }
    if (const char* home = std::getenv("HOME"); home && *home) {
        cands.push_back(fs::path(home) / ".local" / "share" / "Chudware" / "assets" / "logo_icon.png");
        cands.push_back(fs::path(home) / ".local" / "share" / "Chudware" / "assets" / "logo_icon.png");
    }
    cands.push_back("assets/logo_icon.png");
    for (const auto& p : cands) {
        fs::path canon = fs::weakly_canonical(p, ec);
        if (ec) canon = p;
        if (fs::is_regular_file(canon, ec)) return canon.string();
    }
    return {};
}

bool writePixbufPng(GdkPixbuf* pb, const fs::path& path) {
    if (!pb) return false;
    GError* err = nullptr;
    const gboolean ok = gdk_pixbuf_save(pb, path.c_str(), "png", &err, nullptr);
    if (!ok) {
        if (err) g_error_free(err);
        return false;
    }
    return true;
}

void applyStatusLocked() {
    if (!g.ind) return;
    GdkPixbuf* pb = g.idlePb;
    const char* name = "chudware-hostd-idle";
    switch (g.status) {
        case TrayStatus::Busy:
            pb = g.busyPb ? g.busyPb : g.idlePb;
            name = "chudware-hostd-busy";
            break;
        case TrayStatus::Error:
            pb = g.errorPb ? g.errorPb : g.idlePb;
            name = "chudware-hostd-error";
            break;
        case TrayStatus::Idle:
        default:
            break;
    }
    if (pb && !g.iconCacheDir.empty()) {
        fs::path out = fs::path(g.iconCacheDir) / (std::string(name) + ".png");
        if (writePixbufPng(pb, out)) {
            // AppIndicator wants an icon name under a theme path, or an absolute
            // path via app_indicator_set_icon_full on some builds. Use full path.
            app_indicator_set_icon_full(g.ind, out.c_str(), "Chudware hostd");
        }
    }
    if (g.statusItem) {
        gtk_menu_item_set_label(GTK_MENU_ITEM(g.statusItem), g.tip.c_str());
    }
    app_indicator_set_title(g.ind, g.tip.c_str());
}

gboolean idleApplyStatus(gpointer) {
    std::lock_guard<std::mutex> lk(g.mu);
    applyStatusLocked();
    return G_SOURCE_REMOVE;
}

void onQuitActivate(GtkMenuItem*, gpointer) {
    void (*fn)(void*) = nullptr;
    void* ud = nullptr;
    {
        std::lock_guard<std::mutex> lk(g.mu);
        fn = g.quitFn;
        ud = g.quitUd;
    }
    if (fn) fn(ud);
}

gboolean idleQuitGtk(gpointer) {
    gtk_main_quit();
    return G_SOURCE_REMOVE;
}

void trayThreadMain(std::string logoPath, std::string cacheDir) {
    // gtk_init wants argc/argv; pass empty.
    int argc = 0;
    char** argv = nullptr;
    if (!gtk_init_check(&argc, &argv)) {
        g.started.store(false);
        return;
    }

    GError* err = nullptr;
    GdkPixbuf* base = gdk_pixbuf_new_from_file_at_size(logoPath.c_str(), 64, 64, &err);
    if (!base) {
        if (err) g_error_free(err);
        // Last resort: 16x16 solid teal square so the tray still appears.
        base = gdk_pixbuf_new(GDK_COLORSPACE_RGB, TRUE, 8, 32, 32);
        if (base) {
            gdk_pixbuf_fill(base, 0x2EC4B6FFu);  // teal
        }
    }
    if (!base) {
        g.started.store(false);
        return;
    }

    GdkPixbuf* idlePb = tintPixbuf(base, 0.18, 0.77, 0.71);   // teal
    GdkPixbuf* busyPb = tintPixbuf(base, 0.35, 0.78, 1.00);   // bright cyan
    GdkPixbuf* errorPb = tintPixbuf(base, 0.95, 0.30, 0.30);  // red

    std::error_code ec;
    fs::create_directories(cacheDir, ec);

    GtkWidget* menu = gtk_menu_new();
    GtkWidget* statusItem = gtk_menu_item_new_with_label("Chudware hostd");
    gtk_widget_set_sensitive(statusItem, FALSE);
    gtk_menu_shell_append(GTK_MENU_SHELL(menu), statusItem);
    gtk_menu_shell_append(GTK_MENU_SHELL(menu), gtk_separator_menu_item_new());
    GtkWidget* quitItem = gtk_menu_item_new_with_label("Quit hostd");
    g_signal_connect(quitItem, "activate", G_CALLBACK(onQuitActivate), nullptr);
    gtk_menu_shell_append(GTK_MENU_SHELL(menu), quitItem);
    gtk_widget_show_all(menu);

    AppIndicator* ind = app_indicator_new("chudware-hostd", "application-default-icon",
                                          APP_INDICATOR_CATEGORY_APPLICATION_STATUS);
    app_indicator_set_status(ind, APP_INDICATOR_STATUS_ACTIVE);
    app_indicator_set_menu(ind, GTK_MENU(menu));
    app_indicator_set_title(ind, "Chudware hostd");

    {
        std::lock_guard<std::mutex> lk(g.mu);
        g.base = base;
        g.idlePb = idlePb;
        g.busyPb = busyPb;
        g.errorPb = errorPb;
        g.menu = menu;
        g.statusItem = statusItem;
        g.ind = ind;
        g.iconCacheDir = cacheDir;
        applyStatusLocked();
    }

    gtk_main();

    {
        std::lock_guard<std::mutex> lk(g.mu);
        if (g.idlePb) g_object_unref(g.idlePb);
        if (g.busyPb) g_object_unref(g.busyPb);
        if (g.errorPb) g_object_unref(g.errorPb);
        if (g.base) g_object_unref(g.base);
        g.idlePb = g.busyPb = g.errorPb = g.base = nullptr;
        g.ind = nullptr;
        g.menu = nullptr;
        g.statusItem = nullptr;
    }
    g.started.store(false);
}

}  // namespace

bool trayStart(const std::string& selfBinaryPath) {
    if (envDisabled() || !hasDisplay()) return false;
    bool expected = false;
    if (!g.started.compare_exchange_strong(expected, true)) return true;

    const std::string logo = findLogoPng(selfBinaryPath);
    if (logo.empty()) {
        // Still try — thread paints a solid fallback square.
    }

    std::string cache = "/tmp/chudware-hostd-tray";
    if (const char* xdg = std::getenv("XDG_RUNTIME_DIR"); xdg && *xdg) {
        cache = std::string(xdg) + "/chudware-hostd-tray";
    }

    try {
        g.thr = std::thread(trayThreadMain, logo.empty() ? std::string() : logo, cache);
    } catch (...) {
        g.started.store(false);
        return false;
    }
    return true;
}

void traySetStatus(TrayStatus status, const std::string& tooltip) {
    if (!g.started.load()) return;
    {
        std::lock_guard<std::mutex> lk(g.mu);
        g.status = status;
        if (!tooltip.empty()) g.tip = tooltip;
    }
    g_idle_add(idleApplyStatus, nullptr);
}

void traySetQuitHandler(void (*fn)(void*), void* user) {
    std::lock_guard<std::mutex> lk(g.mu);
    g.quitFn = fn;
    g.quitUd = user;
}

void trayStop() {
    // Quit menu runs on the GTK thread and only signals server->stop().
    // trayStop itself MUST run on the main thread after server.run returns —
    // never join the GTK thread from inside gtk_main.
    if (g.thr.joinable() && std::this_thread::get_id() == g.thr.get_id()) {
        // Mis-wired quit handler: request loop exit without joining self.
        g_idle_add(idleQuitGtk, nullptr);
        return;
    }
    if (!g.started.load() && !g.thr.joinable()) return;
    g.stopReq.store(true);
    if (g.started.load()) {
        g_idle_add(idleQuitGtk, nullptr);
    }
    if (g.thr.joinable()) {
        g.thr.join();
    }
    g.started.store(false);
    g.stopReq.store(false);
    std::lock_guard<std::mutex> lk(g.mu);
    g.quitFn = nullptr;
    g.quitUd = nullptr;
}

}  // namespace hostd

#else  // no tray support

namespace hostd {

bool trayStart(const std::string&) { return false; }
void traySetStatus(TrayStatus, const std::string&) {}
void traySetQuitHandler(void (*)(void*), void*) {}
void trayStop() {}

}  // namespace hostd

#endif
