// Open a file/URI with the OS default application. See open_external.h.
#include "platform/open_external.h"

#if defined(_WIN32)
#  include <windows.h>
#  include <shellapi.h>
#else
#  include <cstdlib>
#endif

namespace ui {

namespace {

// Strip a leading "file://" scheme (render_document's resource_link / a plain path both land
// here) so the OS opener gets a normal filesystem path.
std::string stripFileScheme(const std::string& s) {
    const std::string kScheme = "file://";
    if (s.rfind(kScheme, 0) == 0) return s.substr(kScheme.size());
    return s;
}

#if !defined(_WIN32)
// Single-quote the path for a shell command line: close the quote, escape the embedded quote,
// reopen it — the standard POSIX-shell trick for arbitrary bytes inside single quotes.
std::string shellQuote(const std::string& s) {
    std::string out = "'";
    for (char c : s) {
        if (c == '\'') out += "'\\''";
        else out += c;
    }
    out += "'";
    return out;
}
#endif

}  // namespace

void openPathExternally(const std::string& pathOrUri) {
    if (pathOrUri.empty()) return;
    const std::string path = stripFileScheme(pathOrUri);

#if defined(_WIN32)
    ShellExecuteA(nullptr, "open", path.c_str(), nullptr, nullptr, SW_SHOWNORMAL);
#elif defined(__APPLE__)
    const std::string cmd = "open " + shellQuote(path) + " >/dev/null 2>&1 &";
    (void)std::system(cmd.c_str());
#else
    const std::string cmd = "xdg-open " + shellQuote(path) + " >/dev/null 2>&1 &";
    (void)std::system(cmd.c_str());
#endif
}

}  // namespace ui
