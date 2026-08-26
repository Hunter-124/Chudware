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

        // Interactive bits of the title bar (menus, window buttons, workspace tabs) always
        // take the click. Fine-grained spans are checked first; legacy coarse spans are the
        // fallback for code that hasn't been migrated yet.
        if (L && relY < H) {
            if (L->hitAny(relX)) return HTCLIENT;
            if ((relX >= L->menuLeft  && relX < L->menuRight) ||
                (relX >= L->controlsLeft && relX < L->controlsRight))
                return HTCLIENT;
        }

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
       // Non-Windows: keep the custom borderless chrome. On Linux/X11, hand title-bar
       // and border grabs to the window manager so it owns snap, resize, and tear-off.
       // Other backends retain the manual GLFW fallback below.
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>
#if defined(__linux__) && !defined(__EMSCRIPTEN__)
#define GLFW_EXPOSE_NATIVE_X11
#include <GLFW/glfw3native.h>
#ifdef None
#undef None
#endif
#endif

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
// Manual-fallback maximized tear-off state: keep the window maximized until a deliberate
// downward pull (dy > kTearoff), then restore it under the cursor Windows-style.
bool g_maxedPending = false;
double g_grabRelX = 0.5;   // fractional x of the grab across the maximized width (0..1)
double g_grabCy = 0.0;     // cursor y within the caption at grab

#if defined(__linux__) && !defined(__EMSCRIPTEN__)
Display* g_xDisplay = nullptr;
Window g_xWindow = 0;
Atom g_moveResizeAtom = 0;
#endif

bool maximized(GLFWwindow* w) { return glfwGetWindowAttrib(w, GLFW_MAXIMIZED) != 0; }

// Screen-space cursor tracking used only by the manual fallback. The relative term
// compensates as the window moves under a held-still cursor.
void cursorScreen(GLFWwindow* w, double& sx, double& sy) {
    double cx, cy;
    glfwGetCursorPos(w, &cx, &cy);
    int wx, wy;
    glfwGetWindowPos(w, &wx, &wy);
    sx = wx + cx;
    sy = wy + cy;
}

// Standard _NET_WM_MOVERESIZE directions from the EWMH specification.
enum WmDirection {
    kWmSizeTopLeft = 0,
    kWmSizeTop = 1,
    kWmSizeTopRight = 2,
    kWmSizeRight = 3,
    kWmSizeBottomRight = 4,
    kWmSizeBottom = 5,
    kWmSizeBottomLeft = 6,
    kWmSizeLeft = 7,
    kWmMove = 8,
};

int wmDirection(int edge) {
    if ((edge & (kTop | kLeft)) == (kTop | kLeft)) return kWmSizeTopLeft;
    if (edge == kTop) return kWmSizeTop;
    if ((edge & (kTop | kRight)) == (kTop | kRight)) return kWmSizeTopRight;
    if (edge == kRight) return kWmSizeRight;
    if ((edge & (kBottom | kRight)) == (kBottom | kRight)) return kWmSizeBottomRight;
    if (edge == kBottom) return kWmSizeBottom;
    if ((edge & (kBottom | kLeft)) == (kBottom | kLeft)) return kWmSizeBottomLeft;
    return kWmSizeLeft;
}

bool beginWmMoveResize(GLFWwindow* w, int direction, double cx, double cy) {
#if defined(__linux__) && !defined(__EMSCRIPTEN__)
    if (!g_xDisplay || !g_xWindow || !g_moveResizeAtom) return false;

    int wx = 0, wy = 0;
    glfwGetWindowPos(w, &wx, &wy);

    XEvent event{};
    event.xclient.type = ClientMessage;
    event.xclient.display = g_xDisplay;
    event.xclient.window = g_xWindow;
    event.xclient.message_type = g_moveResizeAtom;
    event.xclient.format = 32;
    event.xclient.data.l[0] = static_cast<long>(wx + cx);
    event.xclient.data.l[1] = static_cast<long>(wy + cy);
    event.xclient.data.l[2] = direction;
    event.xclient.data.l[3] = 1;  // left mouse button
    event.xclient.data.l[4] = 1;  // normal application source

    XUngrabPointer(g_xDisplay, CurrentTime);
    const bool sent =
        XSendEvent(g_xDisplay, DefaultRootWindow(g_xDisplay), False,
                   SubstructureRedirectMask | SubstructureNotifyMask, &event) != 0;
    XFlush(g_xDisplay);
    return sent;
#else
    (void)w;
    (void)direction;
    (void)cx;
    (void)cy;
    return false;
#endif
}

}  // namespace

void install(GLFWwindow* win, HitLayout* layout) {
    (void)layout;
    // The creation hint is the reliable path; keep this as a safeguard if a WM ignored it.
    glfwSetWindowAttrib(win, GLFW_DECORATED, GLFW_FALSE);

#if defined(__linux__) && !defined(__EMSCRIPTEN__)
    g_xDisplay = nullptr;
    g_xWindow = 0;
    g_moveResizeAtom = 0;
    if (glfwGetPlatform() == GLFW_PLATFORM_X11) {
        Display* display = glfwGetX11Display();
        const Window window = display ? glfwGetX11Window(win) : 0;
        if (display && window) {
            g_xDisplay = display;
            g_xWindow = window;
            g_moveResizeAtom = XInternAtom(display, "_NET_WM_MOVERESIZE", False);
        }
    }
#endif
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
            layout->hitAny((int)cx) ||
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
            // A successful EWMH request transfers this entire grab to the WM. Leave g_mode
            // untouched so tick() never also runs the client-side resize loop.
            if (!beginWmMoveResize(win, wmDirection(edge), cx, cy)) {
                g_mode = Mode::Resize;
                g_edge = edge;
                cursorScreen(win, g_startSx, g_startSy);
                glfwGetWindowPos(win, &g_startX, &g_startY);
                g_startW = ww;
                g_startH = wh;
            }
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
                // The WM handles normal movement, snap previews, and maximized tear-off.
                // Only backends without usable X11 native handles enter the manual fallback.
                if (!beginWmMoveResize(win, kWmMove, cx, cy)) {
                    g_mode = Mode::Drag;
                    cursorScreen(win, g_startSx, g_startSy);
                    glfwGetWindowPos(win, &g_startX, &g_startY);
                    if (maxed) {
                        // Fallback tear-off stays put until the user pulls downward.
                        g_maxedPending = true;
                        g_grabRelX = ww > 0 ? cx / (double)ww : 0.5;
                        g_grabCy = cy;
                    } else {
                        g_maxedPending = false;
                    }
                }
            }
        }
    }

    if (down && g_mode != Mode::None) {
        double sx, sy;
        cursorScreen(win, sx, sy);
        const int dx = (int)(sx - g_startSx), dy = (int)(sy - g_startSy);
        if (g_mode == Mode::Drag) {
            if (g_maxedPending) {
                // Still snapped: wait for a deliberate DOWNWARD pull before tearing off. A
                // sideways/upward drag leaves the window maximized (motion ignored).
                const int kTearoff = 12;
                if (dy > kTearoff) {
                    glfwRestoreWindow(win);
                    int rw = 0, rh = 0;
                    glfwGetWindowSize(win, &rw, &rh);
                    // Drop the restored window under the cursor: same fractional x across its width,
                    // and the caption grab-point pinned under the pointer (clamped into the caption).
                    double gy = g_grabCy < 0 ? 0 : (g_grabCy > H ? H : g_grabCy);
                    const int nx = (int)(sx - g_grabRelX * rw);
                    const int ny = (int)(sy - gy);
                    glfwSetWindowPos(win, nx, ny);
                    // Re-anchor the drag origin to NOW so the rest of the drag tracks 1:1.
                    g_startX = nx;
                    g_startY = ny;
                    g_startSx = sx;
                    g_startSy = sy;
                    g_maxedPending = false;
                }
            } else {
                glfwSetWindowPos(win, g_startX + dx, g_startY + dy);
            }
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

    if (!down) {
        g_mode = Mode::None;
        g_maxedPending = false;
    }
    g_prevDown = down;
}

}  // namespace chrome

#endif  // _WIN32
