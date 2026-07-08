// cef/cef_trace_file.h — append CEF/vendor-wizard trace lines to a fixed log file.
//
// The GUI re-execs into the runtime and its stderr lands on a socket, so stderr
// traces are invisible post-launch. This sink mirrors every CEF_TRACE / [wizard]
// line into $XDG_DATA_HOME/ai-cad/ai_cad_cef_trace.log (or ~/.local/share/…) so a
// live reproduction can be inspected after the fact. Low-volume (lifecycle only),
// so a fopen/append/fclose per line is fine and guarantees a flush; O_APPEND keeps
// concurrent writes from CEF threads from interleaving mid-line.
#pragma once
#include <cstdio>
#include <cstdlib>
#include <string>

namespace ui::cef {

inline std::string trace_file_path() {
    if (const char* x = std::getenv("XDG_DATA_HOME"); x && x[0])
        return std::string(x) + "/ai-cad/ai_cad_cef_trace.log";
    if (const char* h = std::getenv("HOME"); h && h[0])
        return std::string(h) + "/.local/share/ai-cad/ai_cad_cef_trace.log";
    return "/tmp/ai_cad_cef_trace.log";
}

inline void trace_file_line(const std::string& line) {
    // Same gate as CEF_TRACE's stderr sink: on by default, AI_CAD_CEF_TRACE=0 silences.
    static const bool on = [] {
        const char* v = std::getenv("AI_CAD_CEF_TRACE");
        return !(v && v[0] == '0');
    }();
    if (!on) return;
    static const std::string path = trace_file_path();
    if (FILE* f = std::fopen(path.c_str(), "a")) {
        std::fputs(line.c_str(), f);
        std::fputc('\n', f);
        std::fclose(f);
    }
}

}  // namespace ui::cef
