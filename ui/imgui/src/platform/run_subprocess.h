// Run a child process, feed it stdin, capture stdout (T-W17 routing.py seam).
// The OS-specific #ifdef lives here in platform/ per the ui_style_check rule; the
// panel layer stays framework/OS-agnostic.
#pragma once

#include <string>

namespace platform {

// Run `command` through the system shell. If `stdinText` is non-empty it is fed
// on the child's stdin. Returns captured stdout; sets `ok` true iff the child
// exited 0 with non-empty output. On unsupported platforms returns ok=false.
std::string runShellCapture(const std::string& command, const std::string& stdinText, bool& ok);

// POSIX single-quote a string for safe inclusion in a shell command line.
std::string shellQuote(const std::string& s);

}  // namespace platform
