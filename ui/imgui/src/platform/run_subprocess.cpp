// See run_subprocess.h. OS branching is confined to this platform/ file.
#include "platform/run_subprocess.h"

#include <cstdio>

namespace platform {

std::string shellQuote(const std::string& s) {
    std::string out = "'";
    for (char c : s) { if (c == '\'') out += "'\\''"; else out += c; }
    out += "'";
    return out;
}

std::string runShellCapture(const std::string& command, const std::string& stdinText, bool& ok) {
    ok = false;
#if defined(_WIN32)
    // Windows routes routing.py through the worker/bootstrap path instead; the
    // shell-capture seam is POSIX-only for now (the GUI still runs offline from
    // the seeded view fixture there).
    (void)command; (void)stdinText;
    return {};
#else
    std::string cmd = command;
    if (!stdinText.empty())
        cmd = "printf '%s' " + shellQuote(stdinText) + " | " + cmd;
    FILE* p = popen(cmd.c_str(), "r");
    if (!p) return {};
    std::string out;
    char buf[4096];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), p)) > 0) out.append(buf, n);
    const int rc = pclose(p);
    ok = (rc == 0) && !out.empty();
    return out;
#endif
}

}  // namespace platform
