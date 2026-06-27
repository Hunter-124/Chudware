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

// Windows drives drag/resize through the WndProc hit-test, so the per-frame tick is a no-op.
void tick(GLFWwindow*, HitLayout*) {}

}  // namespace chrome

#else  // ---------------------------------------------------------------------
       // Non-Windows (X11/XWayland): borderless window whose own ImGui title bar is the
       // only chrome. The OS frame is removed (GLFW_DECORATED=false, set at create time);
       // this code reimplements what the Windows WndProc gets for free — caption drag,
       // edge/corner resize, and double-click-to-maximize — by polling the mouse each
       // frame and moving/sizing the window via GLFW. Needs a backend that lets a client
       // position itself: app.cpp forces GLFW_PLATFORM_X11 (Wayland forbids self-move).
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

namespace chrome {
namespace {

enum Edge { kLeft = 1, kRight = 2, kTop = 4, kBottom = 8 };
enum class Mode { None, Drag, Resize };

Mode g_mode = Mode::None;
int g_edge = 0;
double g_startSx = 0, g_startSy = 0;  // cursor screen pos at grab
int g_startX = 0, g_startY = 0, g_startW = 0, g_startH = 0;
bool g_prevDown = false;
double g_lastCaptionClick = -1.0;

bool maximized(GLFWwindow* w) { return glfwGetWindowAttrib(w, GLFW_MAXIMIZED) != 0; }

// Cursor position in SCREEN space = window position + window-relative cursor. Stable to use
// across moves: as the window moves under a held-still cursor, the relative term compensates.
void cursorScreen(GLFWwindow* w, double& sx, double& sy) {
    double cx, cy;
    glfwGetCursorPos(w, &cx, &cy);
    int wx, wy;
    glfwGetWindowPos(w, &wx, &wy);
    sx = wx + cx;
    sy = wy + cy;
}

}  // namespace

void install(GLFWwindow* win, HitLayout* layout) {
    (void)layout;
    // Belt-and-suspenders: app.cpp sets the GLFW_DECORATED=false hint before window creation
    // (the reliable path); assert it here too in case a WM ignored the hint.
    glfwSetWindowAttrib(win, GLFW_DECORATED, GLFW_FALSE);
}

void tick(GLFWwindow* win, HitLayout* layout) {
    if (!win || !layout) return;
    const bool down = glfwGetMouseButton(win, GLFW_MOUSE_BUTTON_LEFT) == GLFW_PRESS;
    double cx, cy;
    glfwGetCursorPos(win, &cx, &cy);
    int ww, wh;
    glfwGetWindowSize(win, &ww, &wh);
    const int H = layout->titleBarHeight;
    const int B = layout->resizeBorder > 0 ? layout->resizeBorder : 6;
    const bool maxed = maximized(win);

    // Press transition: decide what this drag is (resize edge / caption drag / nothing).
    if (down && !g_prevDown && g_mode == Mode::None) {
        const bool inMenu =
            (cx >= layout->menuLeft && cx < layout->menuRight) ||
            (cx >= layout->controlsLeft && cx < layout->controlsRight);
        int edge = 0;
        if (!maxed) {
            if (cx < B) edge |= kLeft;
            if (cx >= ww - B) edge |= kRight;
            if (cy < B) edge |= kTop;
            if (cy >= wh - B) edge |= kBottom;
        }
        if (edge) {
            g_mode = Mode::Resize;
            g_edge = edge;
            cursorScreen(win, g_startSx, g_startSy);
            glfwGetWindowPos(win, &g_startX, &g_startY);
            g_startW = ww;
            g_startH = wh;
        } else if (cy < H && !inMenu) {
            // Draggable caption. A second click within 400ms toggles maximize.
            const double now = glfwGetTime();
            if (g_lastCaptionClick >= 0 && now - g_lastCaptionClick < 0.4) {
                if (maxed)
                    glfwRestoreWindow(win);
                else
                    glfwMaximizeWindow(win);
                g_lastCaptionClick = -1.0;
            } else {
                g_lastCaptionClick = now;
                if (!maxed) {
                    g_mode = Mode::Drag;
                    cursorScreen(win, g_startSx, g_startSy);
                    glfwGetWindowPos(win, &g_startX, &g_startY);
                }
            }
        }
    }

    if (down && g_mode != Mode::None) {
        double sx, sy;
        cursorScreen(win, sx, sy);
        const int dx = (int)(sx - g_startSx), dy = (int)(sy - g_startSy);
        if (g_mode == Mode::Drag) {
            glfwSetWindowPos(win, g_startX + dx, g_startY + dy);
        } else {  // Resize
            const int MINW = 640, MINH = 420;
            int nx = g_startX, ny = g_startY, nw = g_startW, nh = g_startH;
            if (g_edge & kRight) nw = g_startW + dx;
            if (g_edge & kBottom) nh = g_startH + dy;
            if (g_edge & kLeft) {
                nw = g_startW - dx;
                if (nw < MINW) nw = MINW;
                nx = g_startX + g_startW - nw;  // keep the right edge fixed
            }
            if (g_edge & kTop) {
                nh = g_startH - dy;
                if (nh < MINH) nh = MINH;
                ny = g_startY + g_startH - nh;  // keep the bottom edge fixed
            }
            if (nw < MINW) nw = MINW;
            if (nh < MINH) nh = MINH;
            glfwSetWindowPos(win, nx, ny);
            glfwSetWindowSize(win, nw, nh);
        }
    }

    if (!down) g_mode = Mode::None;
    g_prevDown = down;
}

}  // namespace chrome

#endif  // _WIN32
