#include "platform/window.h"

#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>
#define GLFW_EXPOSE_NATIVE_WIN32
#include <GLFW/glfw3native.h>

#include <windows.h>
#include <windowsx.h>
#include <dwmapi.h>

namespace platform {
namespace {

WNDPROC g_prev = nullptr;
WindowChromeLayout* g_layout = nullptr;

bool isMaximized(HWND h) {
    WINDOWPLACEMENT wp{};
    wp.length = sizeof(wp);
    return GetWindowPlacement(h, &wp) && wp.showCmd == SW_SHOWMAXIMIZED;
}

LRESULT CALLBACK proc(HWND hWnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
    case WM_NCCALCSIZE:
        if (wParam == TRUE) {
            if (isMaximized(hWnd)) {
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
        const WindowChromeLayout* L = g_layout;
        RECT rc;
        GetWindowRect(hWnd, &rc);
        const int x = GET_X_LPARAM(lParam), y = GET_Y_LPARAM(lParam);
        const int relX = x - rc.left, relY = y - rc.top;
        const int H = L ? L->titleBarHeight : 0;
        const int B = L ? L->resizeBorder : 0;
        const bool maxed = isMaximized(hWnd);

        if (L && relY < H &&
            ((relX >= L->menuLeft && relX < L->menuRight) ||
             (relX >= L->controlsLeft && relX < L->controlsRight)))
            return HTCLIENT;

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

        if (relY < H) return HTCAPTION;
        return HTCLIENT;
    }
    }
    return CallWindowProcW(g_prev, hWnd, msg, wParam, lParam);
}

}  // namespace

bool initGlfwPlatform() { return glfwInit() != 0; }

void applyBorderlessWindowHints(bool screenshot) {
    (void)screenshot;
    // Windows strips the frame in installWindowChrome; keep a normal decorated hint at create.
}

void installWindowChrome(GLFWwindow* win, WindowChromeLayout* layout) {
    g_layout = layout;
    HWND hwnd = glfwGetWin32Window(win);
    if (!hwnd) return;
    g_prev = reinterpret_cast<WNDPROC>(GetWindowLongPtrW(hwnd, GWLP_WNDPROC));
    SetWindowLongPtrW(hwnd, GWLP_WNDPROC, reinterpret_cast<LONG_PTR>(proc));

    MARGINS m{0, 0, 0, 1};
    DwmExtendFrameIntoClientArea(hwnd, &m);

    SetWindowPos(hwnd, nullptr, 0, 0, 0, 0,
                 SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
}

void tickWindowChrome(GLFWwindow*, WindowChromeLayout*) {}

}  // namespace platform
