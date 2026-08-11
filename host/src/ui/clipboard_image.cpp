// clipboard_image.cpp — pasting an image out of the SYSTEM clipboard into the copilot composer.
//
// Why not glfwGetClipboardString: GLFW's clipboard API is text only (it maps to
// CF_UNICODETEXT / the UTF8_STRING X selection target). There is no image accessor in the
// version we link, so an image sitting on the clipboard is simply invisible to GLFW.
//
// Why the POSIX side shells out: no portable X11/Wayland image-clipboard API is linked into
// this binary. Chudware links GLFW and OpenGL — not Xlib/xcb, not wayland-client — and on
// Wayland the clipboard is only reachable through a wl_data_device on a live seat. Rather than
// pull in two display-server client libraries (and two code paths that must agree), we ask the
// tool the user's session already ships — wl-paste on Wayland, xclip on X11 — for the
// image/png flavour and capture its stdout. Nothing user-controlled is ever interpolated into
// a shell command: every command string below is a compile-time literal, and the destination
// path is written by this process with std::ofstream.
//
// Windows uses the real API (CF_DIBV5 / CF_DIB), no external tools.
//
// CHUDWARE_CLIPBOARD_IMAGE_TEST compiles ONLY the pure helpers plus stubs, so
// host/tests/test_clipboard_image.cpp can link this TU standalone — no GL, no GLFW, no Win32.

#include "ui/clipboard_image.h"

#include <cstdio>
#include <filesystem>
#include <string>
#include <system_error>

#include "util/time_local.h"

// appdataDir() is a free function at namespace scope (host/src/core/app_state.h:221). We
// declare it here instead of including that header: app_state.h drags in GLFW, ImGui and the
// whole AppState graph, none of which the standalone test TU can compile or link against. The
// signature must stay in sync with app_state.h.
std::string appdataDir();

namespace ui {

namespace fs = std::filesystem;

// ---------------- pure helpers (always compiled, including in the test TU) ----------------

// Extension of the LAST path component, lowercased. Component-scoped so a directory named
// "shots.png" cannot make the file inside it an image, and so "foo.png.txt" is a .txt.
static std::string lowerExt(const std::string& path) {
    const size_t slash = path.find_last_of("/\\");
    const std::string base = slash == std::string::npos ? path : path.substr(slash + 1);
    const size_t dot = base.find_last_of('.');
    if (dot == std::string::npos || dot + 1 >= base.size()) return {};
    std::string ext = base.substr(dot);
    for (char& c : ext)
        if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
    return ext;
}

std::string imageMimeForPath(const std::string& path) {
    const std::string ext = lowerExt(path);
    if (ext == ".png") return "image/png";
    if (ext == ".jpg" || ext == ".jpeg") return "image/jpeg";
    if (ext == ".bmp") return "image/bmp";
    if (ext == ".gif") return "image/gif";
    if (ext == ".webp") return "image/webp";
    if (ext == ".tga") return "image/x-tga";
    return {};
}

bool isImageAttachment(const std::string& path) { return !imageMimeForPath(path).empty(); }

// Root of the pasted-image cache. appdataDir() is the PARENT of the data dir, so the
// "/Chudware" component is ours to append (same convention as every other caller).
static fs::path pastedImageRoot() {
    return fs::path(appdataDir()) / "Chudware" / "cache" / "pasted";
}

// `dateStamp` arrives as a plain caller-supplied string (today it is strftime output, but the
// signature promises nothing) and becomes a path component. Keep ONLY [0-9-], so it can never
// contribute a separator, a ".." or "." segment ('.' is not in the keep set), a drive letter,
// or a leading '/'. A stamp that strips to nothing becomes "undated" rather than collapsing
// the component away and letting files land straight in the root.
static std::string sanitizeDateStamp(const std::string& dateStamp) {
    std::string out;
    out.reserve(dateStamp.size());
    for (char c : dateStamp)
        if ((c >= '0' && c <= '9') || c == '-') out.push_back(c);
    if (out.empty()) return "undated";
    return out;
}

// Directory holding one day's pasted images. Does not touch the filesystem.
static fs::path pastedImageDir(const std::string& dateStamp) {
    return pastedImageRoot() / sanitizeDateStamp(dateStamp);
}

std::string pastedImagePath(int seq, const std::string& dateStamp) {
    if (seq < 0) seq = 0;  // a negative counter would render a '-' into the basename
    const fs::path dir = pastedImageDir(dateStamp);
    std::error_code ec;
    fs::create_directories(dir, ec);  // ignored: the eventual write reports the real failure

    char name[32];
    std::snprintf(name, sizeof(name), "paste-%04d.png", seq);
    const fs::path full = dir / name;

    // Safe by construction: `dir` is the root plus one sanitized component, and `name` is
    // snprintf'd from an int. Re-check anyway — this is the one spot where a bug here would
    // turn into an arbitrary-file-write primitive.
    const std::string root = pastedImageRoot().string();
    std::string s = full.string();
    if (s.size() < root.size() || s.compare(0, root.size(), root) != 0)
        s = (pastedImageRoot() / "undated" / name).string();
    return s;
}

// ---------------- platform clipboard ----------------

#if defined(CHUDWARE_CLIPBOARD_IMAGE_TEST)

bool clipboardHasImage() { return false; }

bool clipboardImageToPng(std::string& outPath, std::string& err) {
    (void)outPath;
    err = "clipboard not available in test build";
    return false;
}

#else  // real builds

}  // namespace ui

#include <cstring>
#include <ctime>
#include <fstream>
#include <vector>

#if defined(_WIN32)
#include <windows.h>

#include "stb_image_write.h"  // implementation lives in stb_impl.cpp (linked into Chudware)
#else
#include <cstdlib>
#include <time.h>
#endif

namespace ui {

namespace {

// Local date, "%Y-%m-%d" — the pasted-image dir is bucketed per day so the cache stays
// browsable and prunable.
std::string todayStamp() {
    const std::time_t t = std::time(nullptr);
    std::tm tmv {};
    if (!chudware::util::localTime(t, tmv)) return {};
    char buf[16];
    std::strftime(buf, sizeof(buf), "%Y-%m-%d", &tmv);
    return std::string(buf);
}

// Per-process paste counter. Restarting the app restarts at 0 and would overwrite the day's
// earlier pastes, so skip past any name that already exists.
int nextPasteSeq() {
    static int seq = 0;
    return seq++;
}

const unsigned char kPngSig[8] = {0x89, 'P', 'N', 'G', 0x0D, 0x0A, 0x1A, 0x0A};

// pastedImagePath() for the next free slot today, so a second session on the same day does not
// clobber the first session's chips (paths already handed to the model must stay valid).
std::string nextFreePastePath() {
    const std::string stamp = todayStamp();
    for (int tries = 0; tries < 10000; ++tries) {
        std::string p = pastedImagePath(nextPasteSeq(), stamp);
        std::error_code ec;
        if (!fs::exists(p, ec)) return p;
    }
    return pastedImagePath(nextPasteSeq(), stamp);
}

}  // namespace

#if defined(_WIN32)

namespace {
// Every acquire below has exactly one release, on every path including early failure.
struct ClipboardGuard {
    bool open = false;
    explicit ClipboardGuard(HWND owner) { open = ::OpenClipboard(owner) != FALSE; }
    ~ClipboardGuard() {
        if (open) ::CloseClipboard();
    }
    ClipboardGuard(const ClipboardGuard&) = delete;
    ClipboardGuard& operator=(const ClipboardGuard&) = delete;
};
struct GlobalLockGuard {
    HGLOBAL h = nullptr;
    void* p = nullptr;
    explicit GlobalLockGuard(HGLOBAL handle) : h(handle) { p = h ? ::GlobalLock(h) : nullptr; }
    ~GlobalLockGuard() {
        if (p) ::GlobalUnlock(h);
    }
    GlobalLockGuard(const GlobalLockGuard&) = delete;
    GlobalLockGuard& operator=(const GlobalLockGuard&) = delete;
};
}  // namespace

bool clipboardHasImage() {
    return ::IsClipboardFormatAvailable(CF_DIBV5) || ::IsClipboardFormatAvailable(CF_DIB);
}

bool clipboardImageToPng(std::string& outPath, std::string& err) {
    if (!clipboardHasImage()) {
        err = "The clipboard does not contain an image.";
        return false;
    }
    ClipboardGuard clip(nullptr);
    if (!clip.open) {
        err = "Another application is holding the clipboard. Try again.";
        return false;
    }
    const UINT fmt = ::IsClipboardFormatAvailable(CF_DIBV5) ? (UINT)CF_DIBV5 : (UINT)CF_DIB;
    HANDLE hMem = ::GetClipboardData(fmt);  // owned by the clipboard; do NOT free
    if (!hMem) {
        err = "Could not read the image from the clipboard.";
        return false;
    }
    GlobalLockGuard lock((HGLOBAL)hMem);
    if (!lock.p) {
        err = "Could not read the image from the clipboard.";
        return false;
    }
    const SIZE_T total = ::GlobalSize((HGLOBAL)hMem);
    const auto* bytes = static_cast<const unsigned char*>(lock.p);
    if (total < sizeof(BITMAPINFOHEADER)) {
        err = "The clipboard image is malformed.";
        return false;
    }
    // BITMAPV5HEADER begins with the BITMAPINFOHEADER fields, so biSize tells us where the
    // palette / colour masks end and the pixels begin — one code path covers both formats.
    const auto* bih = reinterpret_cast<const BITMAPINFOHEADER*>(bytes);
    const DWORD headerSize = bih->biSize;
    if (headerSize < sizeof(BITMAPINFOHEADER) || (SIZE_T)headerSize > total) {
        err = "The clipboard image is malformed.";
        return false;
    }
    const int w = (int)bih->biWidth;
    const int srcH = (int)bih->biHeight;  // negative => rows already stored top-down
    const bool bottomUp = srcH > 0;
    const int h = srcH < 0 ? -srcH : srcH;
    const int bpp = (int)bih->biBitCount;
    if (w <= 0 || h <= 0 || (bpp != 24 && bpp != 32) ||
        (bih->biCompression != BI_RGB && bih->biCompression != BI_BITFIELDS)) {
        err = "Unsupported clipboard image format.";
        return false;
    }
    SIZE_T offset = headerSize;
    if (bih->biCompression == BI_BITFIELDS && headerSize == sizeof(BITMAPINFOHEADER))
        offset += 3 * sizeof(DWORD);  // masks follow a plain V3 header
    if (bih->biClrUsed) offset += (SIZE_T)bih->biClrUsed * sizeof(RGBQUAD);
    const SIZE_T stride = ((SIZE_T)w * (SIZE_T)bpp + 31) / 32 * 4;  // rows are DWORD-aligned
    if (offset > total || stride > (total - offset) / (SIZE_T)h) {
        err = "The clipboard image is truncated.";
        return false;
    }
    const unsigned char* pix = bytes + offset;

    std::vector<unsigned char> rgba((size_t)w * (size_t)h * 4);
    bool anyAlpha = false;
    for (int y = 0; y < h; ++y) {
        const int srcRow = bottomUp ? (h - 1 - y) : y;  // DIB rows run bottom-up by default
        const unsigned char* src = pix + (SIZE_T)srcRow * stride;
        unsigned char* dst = rgba.data() + (size_t)y * (size_t)w * 4;
        for (int x = 0; x < w; ++x) {
            dst[0] = src[2];  // BGR(A) -> RGBA
            dst[1] = src[1];
            dst[2] = src[0];
            if (bpp == 32) {
                dst[3] = src[3];
                if (src[3]) anyAlpha = true;
            } else {
                dst[3] = 255;
            }
            src += bpp / 8;
            dst += 4;
        }
    }
    // Most apps put a 32bpp DIB on the clipboard with the alpha channel left at zero. Taken
    // literally that is a fully transparent image, so an all-zero alpha means "no alpha here".
    if (bpp == 32 && !anyAlpha)
        for (size_t i = 3; i < rgba.size(); i += 4) rgba[i] = 255;

    const std::string path = nextFreePastePath();
    if (!stbi_write_png(path.c_str(), w, h, 4, rgba.data(), w * 4)) {
        err = "Could not save the pasted image.";
        return false;
    }
    outPath = path;
    return true;
}

#else  // ---------------- Linux / POSIX ----------------

namespace {

// Run one literal command line and return its stdout. `ok` means clean exit WITH output.
// Every call site passes a compile-time literal; nothing user-controlled is interpolated.
std::string runCapture(const char* cmd, bool& ok) {
    ok = false;
    std::string out;
    FILE* p = ::popen(cmd, "r");
    if (!p) return out;
    char buf[16384];
    size_t n;
    while ((n = std::fread(buf, 1, sizeof(buf), p)) > 0) out.append(buf, n);
    const int rc = ::pclose(p);
    ok = (rc == 0) && !out.empty();
    return out;
}

// which-style probe, so a missing tool becomes a sentence for the user instead of a shell
// error on stderr and an empty capture we would have to guess at.
bool haveTool(const char* literalName) {
    std::string cmd = "command -v ";
    cmd += literalName;  // only ever "wl-paste" / "xclip"
    cmd += " >/dev/null 2>&1";
    return std::system(cmd.c_str()) == 0;
}
bool haveWlPaste() {
    static const bool v = haveTool("wl-paste");
    return v;
}
bool haveXclip() {
    static const bool v = haveTool("xclip");
    return v;
}

double monotonicSeconds() {
    struct timespec ts {};
    ::clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec * 1e-9;
}

}  // namespace

bool clipboardHasImage() {
    // The composer calls this EVERY frame and each probe forks a process, so cache the answer
    // for ~500 ms. A paste the user makes mid-window is still caught: Ctrl+V goes straight to
    // clipboardImageToPng(), which never consults this.
    static double lastPoll = -1.0e9;
    static bool lastValue = false;
    const double now = monotonicSeconds();
    if (now - lastPoll < 0.5) return lastValue;
    lastPoll = now;

    bool ok = false;
    std::string types;
    if (haveWlPaste()) types = runCapture("wl-paste --list-types 2>/dev/null", ok);
    if (!ok && haveXclip())
        types = runCapture("xclip -selection clipboard -t TARGETS -o 2>/dev/null", ok);
    lastValue = ok && types.find("image/") != std::string::npos;
    return lastValue;
}

bool clipboardImageToPng(std::string& outPath, std::string& err) {
    const bool wl = haveWlPaste();
    const bool xc = haveXclip();
    if (!wl && !xc) {
        err = "Pasting images needs wl-paste (Wayland) or xclip (X11); neither is installed.";
        return false;
    }
    bool ok = false;
    std::string png;
    if (wl) png = runCapture("wl-paste --no-newline --type image/png 2>/dev/null", ok);
    if (!ok && xc) png = runCapture("xclip -selection clipboard -t image/png -o 2>/dev/null", ok);
    if (!ok || png.empty()) {
        err = "The clipboard does not contain an image.";
        return false;
    }
    // The tools claim image/png; verify rather than trust, so a stray error page or a text
    // selection never lands in the composer as a "picture" the model cannot decode. Checked
    // BEFORE writing so a bad capture never creates a file at all.
    if (png.size() < 8 || std::memcmp(png.data(), kPngSig, 8) != 0) {
        err = "The clipboard image could not be read as a PNG.";
        return false;
    }

    const std::string path = nextFreePastePath();
    // The destination is written by this process, never handed to a shell.
    std::ofstream f(path, std::ios::binary | std::ios::trunc);
    if (f) {
        f.write(png.data(), (std::streamsize)png.size());
        f.close();
    }
    if (!f) {
        std::error_code ec;
        fs::remove(path, ec);
        err = "Could not save the pasted image.";
        return false;
    }
    outPath = path;
    return true;
}

#endif  // _WIN32

#endif  // CHUDWARE_CLIPBOARD_IMAGE_TEST

}  // namespace ui
