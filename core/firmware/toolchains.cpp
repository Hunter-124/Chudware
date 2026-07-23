#include "firmware/toolchains.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <sstream>

#include "firmware/subprocess.h"

namespace chudware::firmware {
namespace {

namespace fs = std::filesystem;

constexpr size_t kStderrCap = 64000;  // contract §3 bound

std::string cap(const std::string& text) {
    if (text.size() <= kStderrCap) return text;
    return text.substr(0, kStderrCap) + "\n\xE2\x80\xA6[truncated]";  // "…"
}

std::vector<std::string> parse_warnings(const std::string& text) {
    std::vector<std::string> out;
    std::string line;
    std::istringstream in(text);
    while (std::getline(in, line)) {
        std::string stripped = line;
        size_t b = stripped.find_first_not_of(" \t\r");
        stripped = b == std::string::npos ? "" : stripped.substr(b);
        size_t e = stripped.find_last_not_of(" \t\r");
        if (e != std::string::npos) stripped = stripped.substr(0, e + 1);
        if (line.find(": warning:") != std::string::npos ||
            stripped.rfind("warning:", 0) == 0) {
            out.push_back(stripped);
        }
    }
    return out;
}

// First file under `base` matching any of the suffixes, sorted order (mirrors
// toolchains._first_glob — sorted rglob hits, first match wins).
std::optional<std::string> first_glob(const fs::path& base,
                                      std::initializer_list<const char*> exts) {
    std::error_code ec;
    for (const char* ext : exts) {
        std::vector<fs::path> hits;
        if (fs::is_directory(base, ec)) {
            for (auto it = fs::recursive_directory_iterator(base, ec); !ec && it != fs::recursive_directory_iterator(); it.increment(ec)) {
                if (it->is_regular_file(ec) && it->path().extension() == ext)
                    hits.push_back(it->path());
            }
        }
        std::sort(hits.begin(), hits.end());
        if (!hits.empty()) return hits.front().string();
    }
    return std::nullopt;
}

BuildResult not_found(const std::string& tool, const std::vector<std::string>& cmd,
                      std::chrono::steady_clock::time_point t0) {
    BuildResult r;
    r.success = false;
    r.returncode = -1;
    r.cmd = cmd;
    r.stderr_text = "toolchain_not_found: '" + tool + "' is not installed / not on PATH";
    r.ms = std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
    return r;
}

double elapsed_ms(std::chrono::steady_clock::time_point t0) {
    return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0).count();
}

BuildResult run_build(const std::vector<std::string>& cmd, const fs::path& cwd,
                      double timeout_s, std::chrono::steady_clock::time_point t0,
                      const std::string& combined_prefix = "") {
    RunResult rr = run_capture(cmd, cwd.string(), timeout_s);
    std::string combined = combined_prefix + rr.stdout_text + rr.stderr_text;
    BuildResult r;
    if (rr.not_found) {
        r.success = false;
        r.returncode = -1;
        r.stderr_text = "toolchain_not_found: executable '" + cmd[0] + "' not on PATH";
    } else if (rr.timed_out) {
        r.success = false;
        r.returncode = -1;
        r.stderr_text = "timeout: '" + cmd[0] + "' exceeded " +
                        std::to_string(static_cast<long long>(timeout_s)) + "s";
    } else {
        r.returncode = rr.returncode;
        r.stderr_text = cap(combined);
    }
    r.cmd = cmd;
    r.warnings = parse_warnings(combined);
    r.ms = elapsed_ms(t0);
    return r;
}

BuildResult build_cmake(const Project& project, double timeout_s) {
    auto t0 = std::chrono::steady_clock::now();
    fs::path root = project.root_dir();
    fs::path build_dir = project.build_path();
    if (which("cmake").empty()) return not_found("cmake", {"cmake"}, t0);
    if (which("arm-none-eabi-gcc").empty())
        return not_found("arm-none-eabi-gcc", {"arm-none-eabi-gcc"}, t0);

    std::string generator = which("ninja").empty() ? "Unix Makefiles" : "Ninja";
    fs::path tc_file = root / "cmake" / "arm-none-eabi-toolchain.cmake";
    std::vector<std::string> configure = {
        "cmake", "-S", root.string(), "-B", build_dir.string(),
        "-G", generator, "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
    };
    std::error_code ec;
    if (fs::is_regular_file(tc_file, ec))
        configure.push_back("-DCMAKE_TOOLCHAIN_FILE=" + tc_file.string());

    RunResult conf = run_capture(configure, root.string(), timeout_s);
    std::string combined = conf.stdout_text + conf.stderr_text;
    if (conf.not_found || conf.timed_out || conf.returncode != 0) {
        BuildResult r;
        r.success = false;
        r.returncode = conf.returncode;
        r.cmd = configure;
        r.stderr_text = cap(combined);
        r.warnings = parse_warnings(combined);
        r.ms = elapsed_ms(t0);
        return r;
    }

    std::vector<std::string> build = {"cmake", "--build", build_dir.string()};
    BuildResult r = run_build(build, root, timeout_s, t0, combined);
    r.elf_path = first_glob(build_dir, {".elf", ".bin", ".hex"});
    r.map_path = first_glob(build_dir, {".map"});
    r.success = (r.returncode == 0 && r.elf_path.has_value());
    return r;
}

BuildResult build_espidf(const Project& project, double timeout_s) {
    auto t0 = std::chrono::steady_clock::now();
    fs::path root = project.root_dir();
    if (which("idf.py").empty()) return not_found("idf.py", {"idf.py", "build"}, t0);
    std::vector<std::string> cmd = {"idf.py", "-C", root.string(), "build"};
    BuildResult r = run_build(cmd, root, timeout_s, t0);
    r.elf_path = first_glob(project.build_path(), {".elf", ".bin"});
    r.map_path = first_glob(project.build_path(), {".map"});
    r.success = (r.returncode == 0 && r.elf_path.has_value());
    return r;
}

BuildResult build_arduino(const Project& project, double timeout_s) {
    auto t0 = std::chrono::steady_clock::now();
    fs::path root = project.root_dir();
    if (which("arduino-cli").empty())
        return not_found("arduino-cli", {"arduino-cli", "compile"}, t0);
    fs::path out_dir = project.build_path();
    std::vector<std::string> cmd = {
        "arduino-cli", "compile", "--fqbn", project.mcu_family,
        "--output-dir", out_dir.string(), root.string(),
    };
    BuildResult r = run_build(cmd, root, timeout_s, t0);
    r.elf_path = first_glob(out_dir, {".elf", ".bin", ".hex"});
    r.map_path = first_glob(out_dir, {".map"});
    r.success = (r.returncode == 0 && r.elf_path.has_value());
    return r;
}

}  // namespace

nlohmann::json BuildResult::to_dict() const {
    return {
        {"success", success},
        {"elf_path", elf_path ? nlohmann::json(*elf_path) : nlohmann::json(nullptr)},
        {"map_path", map_path ? nlohmann::json(*map_path) : nlohmann::json(nullptr)},
        {"stderr", stderr_text},
        {"warnings", warnings},
        {"returncode", returncode},
        {"cmd", cmd},
        {"ms", std::round(ms * 100.0) / 100.0},
    };
}

BuildResult build(const Project& project, double timeout_s) {
    if (project.toolchain == "cmake") return build_cmake(project, timeout_s);
    if (project.toolchain == "espidf") return build_espidf(project, timeout_s);
    if (project.toolchain == "arduino") return build_arduino(project, timeout_s);
    // project.new_project validates the toolchain; a hand-edited sidecar is a
    // graceful toolchain_not_found rather than a crash.
    BuildResult r;
    r.success = false;
    r.returncode = -1;
    r.stderr_text = "toolchain_not_found: unknown toolchain '" + project.toolchain + "'";
    return r;
}

}  // namespace chudware::firmware
