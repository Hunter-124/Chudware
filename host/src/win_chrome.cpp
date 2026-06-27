#include "win_chrome.h"

#ifdef _WIN32

#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>
#define GLFW_EXPOSE_NATIVE_WIN32
#include <GLFW/glfw3native.h>

#include <windows.h>
#include <windowsx.h>  // GET_X_LPARAM / GET_Y_LPARAM
#include <dwmapi.h>

namespace chrome {
namespace {

WNDPROC g_prev = nullptr;       // GLFW/ImGui's WndProc, chained for everything we don't handle
HitLayout* g_layout = nullptr;  // owned by the GUI; updated each frame

bool isMaximized(HWND h) {
    WINDOWPLACEMENT wp{};
    wp.length = sizeof(wp);
    return GetWindowPlacement(h, &wp) && wp.showCmd == SW_SHOWMAXIMIZED;
}

LRESULT CALLBACK proc(HWND hWnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
    case WM_NCCALCSIZE:
        // Returning 0 with the proposed rect unchanged makes the client area cover the
        // entire window: no OS title bar, no border. We keep WS_THICKFRAME (set by GLFW for
        // a resizable window) so the OS still drives resize/snap via WM_NCHITTEST below.
        if (wParam == TRUE) {
            if (isMaximized(hWnd)) {
                // A maximized thick-frame window overhangs the work area by the frame size;
                // inset so content isn't clipped and the taskbar stays visible.
                NCCALCSIZE_PARAMS* p = reinterpret_cast<NCCALCSIZE_PARAMS*>(lParam);
                int fx = GetSystemMetrics(SM_CXFRAME) + GetSystemMetrics(SM_CXPADDEDBORDER);
                int fy = GetSystemMetrics(SM_CYFRAME) + GetSystemMetrics(SM_CXPADDEDBORDER);
                p->rgrc[0].left += fx;
                p->rgrc[0].right -= fx;
                p->rgrc[0].top += fy;
                p->rgrc[0].bottom -= fy;
            }
            return 0;
        }
        break;

    case WM_NCHITTEST: {
        const HitLayout* L = g_layout;
        RECT rc;
        GetWindowRect(hWnd, &rc);
        const int x = GET_X_LPARAM(lParam), y = GET_Y_LPARAM(lParam);
        const int relX = x - rc.left, relY = y - rc.top;
        const int H = L ? L->titleBarHeight : 0;
        const int B = L ? L->resizeBorder : 0;
        const bool maxed = isMaximized(hWnd);

        // Interactive bits of the title bar (menus, window buttons) always take the click.
        if (L && relY < H &&
            ((relX >= L->menuLeft && relX < L->menuRight) ||
             (relX >= L->controlsLeft && relX < L->controlsRight)))
            return HTCLIENT;

        // Resize edges (disabled while maximized).
        if (!maxed) {
            const bool l = relX < B, r = x >= rc.right - B, t = relY < B, b = y >= rc.bottom - B;
            if (t && l) return HTTOPLEFT;
            if (t && r) return HTTOPRIGHT;
            if (b && l) return HTBOTTOMLEFT;
            if (b && r) return HTBOTTOMRIGHT;
            if (l) return HTLEFT;
            if (r) return HTRIGHT;
            if (t) return HTTOP;
            if (b) return HTBOTTOM;
        }

        // The rest of the bar is draggable caption (also enables double-click-to-maximize).
        if (relY < H) return HTCAPTION;
        return HTCLIENT;
    }
    }
    return CallWindowProcW(g_prev, hWnd, msg, wParam, lParam);
}

}  // namespace

void install(GLFWwindow* win, HitLayout* layout) {
    g_layout = layout;
    HWND hwnd = glfwGetWin32Window(win);
    if (!hwnd) return;
    g_prev = reinterpret_cast<WNDPROC>(GetWindowLongPtrW(hwnd, GWLP_WNDPROC));
    SetWindowLongPtrW(hwnd, GWLP_WNDPROC, reinterpret_cast<LONG_PTR>(proc));

    // Restore the standard drop shadow on the now-borderless window. A 1px bottom margin is
    // the lightest extension that brings the shadow back without painting into our chrome.
    MARGINS m{0, 0, 0, 1};
    DwmExtendFrameIntoClientArea(hwnd, &m);

    // Force the frame to be recomputed now that WM_NCCALCSIZE strips it.
    SetWindowPos(hwnd, nullptr, 0, 0, 0, 0,
                 SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
}

}  // namespace chrome

#else  // ---------------------------------------------------------------------
       // Non-Windows: keep the standard OS-drawn title bar / frame for now (no
       // borderless custom chrome). The GUI still draws its own title-bar widgets;
       // making the window borderless + draggable on X11/Wayland is a later polish.
struct GLFWwindow;
namespace chrome {
void install(GLFWwindow*, HitLayout*) {}
}  // namespace chrome

#endif  // _WIN32
