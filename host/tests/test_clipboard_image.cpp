// test_clipboard_image.cpp — ctest for the pure helpers behind "paste an image into the
// copilot composer" (ui::isImageAttachment / ui::imageMimeForPath / ui::pastedImagePath).
// Links the REAL implementation (src/ui/clipboard_image.cpp) compiled with
// CHUDWARE_CLIPBOARD_IMAGE_TEST, so there is no GL, no GLFW, no Win32 clipboard here — only
// the naming and classification logic every other layer trusts.
//
// What these guard:
//
//  1. Attachment classification decides the wire "kind": an image the composer misfiles as a
//     plain file is never turned into an image_part, so the vision model silently receives a
//     path it cannot see. Extension matching therefore has to be case-insensitive AND anchored
//     to the end of the last path component — "foo.png.txt" is a text file.
//  2. pastedImagePath() builds a filesystem path out of a caller-supplied dateStamp. That
//     string must never be able to introduce a separator, a ".." segment or an absolute path:
//     this process writes clipboard bytes to whatever comes back, so an escape here is an
//     arbitrary-file-write. "../../etc", "a/b" and "" all have to land inside the cache dir.
//
// Acceptance:
//   ✓ .png/.PNG/.jpeg/.webp are images; .txt/.step/no-extension/""/"foo.png.txt" are not
//   ✓ jpg and jpeg both map to image/jpeg; a non-image maps to ""
//   ✓ pastedImagePath creates its parent dir, zero-pads the sequence, and varies with seq
//   ✓ a hostile dateStamp still yields a path under .../cache/pasted with no ".." segment
//   ✓ a negative seq is clamped instead of rendering "paste--001.png"

#include "ui/clipboard_image.h"

#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <string>
#include <system_error>

namespace fs = std::filesystem;

static int g_failures = 0;

#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);          \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)

#define CHECK_EQ(a, b)                                                           \
    do {                                                                         \
        std::string _a = (a);                                                    \
        std::string _b = (b);                                                    \
        if (_a != _b) {                                                          \
            std::printf("FAIL %s:%d: %s == %s\n  got: [%s]\n  want:[%s]\n",      \
                        __FILE__, __LINE__, #a, #b, _a.c_str(), _b.c_str());     \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)

// clipboard_image.cpp declares appdataDir() and links against whatever provides it (the app
// provides host/src/core/app_state.cpp, which we cannot pull in without GL). Supply the same
// XDG_DATA_HOME/HOME semantics here so the test drives the real path-building code.
std::string appdataDir() {
    if (const char* x = std::getenv("XDG_DATA_HOME")) {
        if (*x) return std::string(x);
    }
    if (const char* h = std::getenv("HOME")) {
        if (*h) return std::string(h) + "/.local/share";
    }
    return ".";
}

static std::string pastedRoot() {
    return (fs::path(appdataDir()) / "Chudware" / "cache" / "pasted").string();
}

static bool startsWith(const std::string& s, const std::string& prefix) {
    return s.size() >= prefix.size() && s.compare(0, prefix.size(), prefix) == 0;
}

// True when any '/'-separated component of `s` is exactly "..".
static bool hasDotDotSegment(const std::string& s) {
    for (const auto& part : fs::path(s))
        if (part == "..") return true;
    return false;
}

// ---- 1. classification ------------------------------------------------------------------

static void test_is_image_attachment() {
    CHECK(ui::isImageAttachment("shot.png"));
    CHECK(ui::isImageAttachment("SHOT.PNG"));
    CHECK(ui::isImageAttachment("/tmp/a/photo.jpeg"));
    CHECK(ui::isImageAttachment("photo.JpEg"));
    CHECK(ui::isImageAttachment("anim.webp"));
    CHECK(ui::isImageAttachment("x.jpg"));
    CHECK(ui::isImageAttachment("x.bmp"));
    CHECK(ui::isImageAttachment("x.gif"));
    CHECK(ui::isImageAttachment("x.tga"));

    CHECK(!ui::isImageAttachment("notes.txt"));
    CHECK(!ui::isImageAttachment("bracket.step"));
    CHECK(!ui::isImageAttachment("Makefile"));
    CHECK(!ui::isImageAttachment(""));
    CHECK(!ui::isImageAttachment("."));
    CHECK(!ui::isImageAttachment("trailing."));
    // ".png" mid-string must not win: this is a text file with a misleading name.
    CHECK(!ui::isImageAttachment("foo.png.txt"));
    CHECK(!ui::isImageAttachment("png"));
    // A directory component ending in .png does not make the file an image.
    CHECK(!ui::isImageAttachment("/tmp/shots.png/readme"));
}

static void test_image_mime_for_path() {
    CHECK_EQ(ui::imageMimeForPath("a.png"), "image/png");
    CHECK_EQ(ui::imageMimeForPath("A.PNG"), "image/png");
    CHECK_EQ(ui::imageMimeForPath("a.jpg"), "image/jpeg");
    CHECK_EQ(ui::imageMimeForPath("a.jpeg"), "image/jpeg");
    CHECK_EQ(ui::imageMimeForPath("a.bmp"), "image/bmp");
    CHECK_EQ(ui::imageMimeForPath("a.gif"), "image/gif");
    CHECK_EQ(ui::imageMimeForPath("a.webp"), "image/webp");
    CHECK_EQ(ui::imageMimeForPath("a.tga"), "image/x-tga");
    CHECK_EQ(ui::imageMimeForPath("a.txt"), "");
    CHECK_EQ(ui::imageMimeForPath("a.png.txt"), "");
    CHECK_EQ(ui::imageMimeForPath(""), "");
}

// ---- 2. pasted-image naming -------------------------------------------------------------

static void test_pasted_path_shape() {
    const std::string p = ui::pastedImagePath(7, "2026-07-28");
    const std::string want = (fs::path(pastedRoot()) / "2026-07-28" / "paste-0007.png").string();
    CHECK_EQ(p, want);
    // The directory is created as a side effect, so the caller can just open the file.
    CHECK(fs::is_directory(fs::path(p).parent_path()));
    // Zero padded to four digits.
    CHECK_EQ(fs::path(p).filename().string(), "paste-0007.png");
    CHECK_EQ(fs::path(ui::pastedImagePath(0, "2026-07-28")).filename().string(), "paste-0000.png");
    CHECK_EQ(fs::path(ui::pastedImagePath(1234, "2026-07-28")).filename().string(),
             "paste-1234.png");
    // Distinct sequences never collide.
    CHECK(ui::pastedImagePath(1, "2026-07-28") != ui::pastedImagePath(2, "2026-07-28"));
    // Deterministic for the same inputs.
    CHECK_EQ(ui::pastedImagePath(7, "2026-07-28"), p);
}

static void test_pasted_path_sanitises_date_stamp() {
    const std::string root = pastedRoot();
    const char* hostile[] = {"../../etc", "a/b", "", "..", "/absolute", "C:\\win",
                             "2026-07-28/../../..", ".", "  ", "2026\n07"};
    for (const char* stamp : hostile) {
        const std::string p = ui::pastedImagePath(3, stamp);
        CHECK(startsWith(p, root));
        CHECK(!hasDotDotSegment(p));
        // Exactly one component between the root and the file: <root>/<stamp>/paste-0003.png.
        CHECK_EQ(fs::path(p).filename().string(), "paste-0003.png");
        const std::string rel = fs::path(p).parent_path().string().substr(root.size());
        CHECK(rel.find('/') == 0);                      // leading separator only
        CHECK(rel.find('/', 1) == std::string::npos);   // and no second one
        CHECK(!rel.empty());
    }
    // A stamp with nothing to keep falls back to a literal bucket rather than the root itself.
    CHECK_EQ(ui::pastedImagePath(3, "a/b"),
             (fs::path(root) / "undated" / "paste-0003.png").string());
    // Digits and dashes survive; everything else is dropped.
    CHECK_EQ(ui::pastedImagePath(3, "2026-07-28"),
             (fs::path(root) / "2026-07-28" / "paste-0003.png").string());
    CHECK_EQ(ui::pastedImagePath(3, "../2026-07-28"),
             (fs::path(root) / "2026-07-28" / "paste-0003.png").string());
}

static void test_pasted_path_negative_seq_clamped() {
    const std::string p = ui::pastedImagePath(-5, "2026-07-28");
    CHECK_EQ(fs::path(p).filename().string(), "paste-0000.png");
    CHECK_EQ(p, ui::pastedImagePath(0, "2026-07-28"));
    CHECK(startsWith(p, pastedRoot()));
}

// ---- 3. the test-build clipboard stubs ---------------------------------------------------

static void test_clipboard_stubs_report_unavailable() {
    CHECK(!ui::clipboardHasImage());
    std::string out = "untouched", err;
    CHECK(!ui::clipboardImageToPng(out, err));
    CHECK(!err.empty());
    CHECK_EQ(out, "untouched");  // a failed grab must not hand back a path
}

int main() {
    // Redirect the app data dir at a disposable temp tree BEFORE anything calls appdataDir(),
    // so pastedImagePath's create_directories cannot touch the real ~/.local/share/Chudware.
    std::error_code ec;
    const fs::path tmp = fs::temp_directory_path(ec) / "chudware-test-clipboard-image";
    fs::remove_all(tmp, ec);
    fs::create_directories(tmp, ec);
    if (ec) {
        std::printf("test_clipboard_image: could not create temp dir %s\n", tmp.string().c_str());
        return 1;
    }
    ::setenv("XDG_DATA_HOME", tmp.string().c_str(), 1);
    ::setenv("HOME", tmp.string().c_str(), 1);
    ::setenv("APPDATA", tmp.string().c_str(), 1);

    test_is_image_attachment();
    test_image_mime_for_path();
    test_pasted_path_shape();
    test_pasted_path_sanitises_date_stamp();
    test_pasted_path_negative_seq_clamped();
    test_clipboard_stubs_report_unavailable();

    fs::remove_all(tmp, ec);

    if (g_failures) {
        std::printf("test_clipboard_image: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_clipboard_image: all checks passed\n");
    return 0;
}
