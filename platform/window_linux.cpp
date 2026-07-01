#include "platform/window.h"

#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

namespace platform {
namespace {

enum Edge { kLeft = 1, kRight = 2, kTop = 4, kBottom = 8 };
enum class Mode { None, Drag, Resize };

Mode g_mode = Mode::None;
int g_edge = 0;
double g_startSx = 0, g_startSy = 0;
int g_startX = 0, g_startY = 0, g_startW = 0, g_startH = 0;
bool g_prevDown = false;
double g_lastCaptionClick = -1.0;

bool maximized(GLFWwindow* w) { return glfwGetWindowAttrib(w, GLFW_MAXIMIZED) != 0; }

void cursorScreen(GLFWwindow* w, double& sx, double& sy) {
    double cx, cy;
    glfwGetCursorPos(w, &cx, &cy);
    int wx, wy;
    glfwGetWindowPos(w, &wx, &wy);
    sx = wx + cx;
    sy = wy + cy;
}

}  // namespace

bool initGlfwPlatform() {
    glfwInitHint(GLFW_PLATFORM, GLFW_PLATFORM_X11);
    if (glfwInit()) return true;
    glfwInitHint(GLFW_PLATFORM, GLFW_ANY_PLATFORM);
    return glfwInit() != 0;
}

void applyBorderlessWindowHints(bool screenshot) {
    glfwWindowHint(GLFW_DECORATED, GLFW_FALSE);
    if (screenshot) {
        glfwWindowHint(GLFW_VISIBLE, GLFW_FALSE);
        glfwWindowHint(GLFW_SCALE_TO_MONITOR, GLFW_FALSE);
    } else {
        glfwWindowHint(GLFW_SCALE_TO_MONITOR, GLFW_TRUE);
    }
}

void installWindowChrome(GLFWwindow* win, WindowChromeLayout*) {
    glfwSetWindowAttrib(win, GLFW_DECORATED, GLFW_FALSE);
}

void tickWindowChrome(GLFWwindow* win, WindowChromeLayout* layout) {
    if (!win || !layout) return;
    const bool down = glfwGetMouseButton(win, GLFW_MOUSE_BUTTON_LEFT) == GLFW_PRESS;
    double cx, cy;
    glfwGetCursorPos(win, &cx, &cy);
    int ww, wh;
    glfwGetWindowSize(win, &ww, &wh);
    const int H = layout->titleBarHeight;
    const int B = layout->resizeBorder > 0 ? layout->resizeBorder : 6;
    const bool maxed = maximized(win);

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
        } else {
            const int MINW = 640, MINH = 420;
            int nx = g_startX, ny = g_startY, nw = g_startW, nh = g_startH;
            if (g_edge & kRight) nw = g_startW + dx;
            if (g_edge & kBottom) nh = g_startH + dy;
            if (g_edge & kLeft) {
                nw = g_startW - dx;
                if (nw < MINW) nw = MINW;
                nx = g_startX + g_startW - nw;
            }
            if (g_edge & kTop) {
                nh = g_startH - dy;
                if (nh < MINH) nh = MINH;
                ny = g_startY + g_startH - nh;
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

}  // namespace platform
