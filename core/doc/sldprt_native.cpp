// SolidWorks / Parasolid source resolution — see sldprt_native.h.
#include "doc/sldprt_native.h"

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <optional>
#include <random>
#include <sstream>

#include "doc/formats.h"
#include "doc/session.h"  // OpError
#include "util/platform_paths.h"
#include "util/subprocess.h"

namespace chudware::doc {
namespace fs = std::filesystem;
using nlohmann::json;

namespace {

#ifdef _WIN32
constexpr const char* kExeSuffix = ".exe";
constexpr char kPathSep = ';';
#else
constexpr const char* kExeSuffix = "";
constexpr char kPathSep = ':';
#endif

std::string env_or_empty(const char* name) {
    const char* v = std::getenv(name);
    return v ? std::string(v) : std::string();
}

bool is_file(const fs::path& p) {
    std::error_code ec;
    return !p.empty() && fs::is_regular_file(p, ec);
}

fs::path exe_dir() {
    const std::string exe = platform::executablePath();
    return exe.empty() ? fs::path() : fs::u8path(exe).parent_path();
}

fs::path find_on_path(const std::string& name) {
    const std::string dirs = env_or_empty("PATH");
    const std::string exe = name + kExeSuffix;
    std::size_t start = 0;
    while (start <= dirs.size()) {
        std::size_t end = dirs.find(kPathSep, start);
        if (end == std::string::npos) end = dirs.size();
        if (end > start) {
            const fs::path candidate = fs::u8path(dirs.substr(start, end - start)) / exe;
            if (is_file(candidate)) return candidate;
        }
        start = end + 1;
    }
    return {};
}

// A JavaScript runtime that can run the converter. omp is a compiled Bun binary that behaves as
// the Bun CLI when BUN_BE_BUN=1, so the agent runtime Chudware already ships doubles as this one.
struct JsRuntime {
    fs::path exe;
    bool bun_be_bun = false;
};

std::optional<JsRuntime> find_runtime() {
    if (const std::string v = env_or_empty("CHUDWARE_JS_RUNTIME"); !v.empty())
        return is_file(fs::u8path(v)) ? std::optional<JsRuntime>({fs::u8path(v), false})
                                      : std::nullopt;
    if (const std::string v = env_or_empty("CHUDWARE_OMP_BIN"); !v.empty() && is_file(fs::u8path(v)))
        return JsRuntime{fs::u8path(v), true};
    const std::string omp = std::string("omp") + kExeSuffix;
    if (const fs::path dir = exe_dir(); is_file(dir / omp)) return JsRuntime{dir / omp, true};
    if (fs::path p = find_on_path("node"); !p.empty()) return JsRuntime{p, false};
    if (fs::path p = find_on_path("bun"); !p.empty()) return JsRuntime{p, false};
    if (fs::path p = find_on_path("omp"); !p.empty()) return JsRuntime{p, true};
    return std::nullopt;
}

// The converter driver: CHUDWARE_SLDPRT_ROOT, the payload's sldprt/ beside the executable, or the
// developer checkout's vendor/sldprt.
fs::path find_driver() {
    std::vector<fs::path> roots;
    if (const std::string v = env_or_empty("CHUDWARE_SLDPRT_ROOT"); !v.empty())
        roots.push_back(fs::u8path(v));
    if (const fs::path dir = exe_dir(); !dir.empty()) roots.push_back(dir / "sldprt");
    if (const fs::path src = platform::developerSourceRoot(); !src.empty())
        roots.push_back(src / "vendor" / "sldprt");
    for (const fs::path& r : roots)
        if (is_file(r / "chudware_driver.js")) return r / "chudware_driver.js";
    return {};
}

std::chrono::milliseconds converter_timeout() {
    const std::string v = env_or_empty("CHUDWARE_SLDPRT_TIMEOUT_S");
    long s = 300;
    if (!v.empty()) {
        try { s = std::stol(v); } catch (...) {}
    }
    return std::chrono::milliseconds(std::max(1L, s) * 1000L);
}

fs::path fresh_output(const std::string& stem) {
    std::random_device rd;
    const auto nonce = std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()) +
                       "-" + std::to_string(rd());
    std::error_code ec;
    const fs::path dir = fs::temp_directory_path(ec) / ("chudware-sldprt-" + nonce);
    fs::create_directories(dir, ec);
    if (ec)
        throw OpError("import_failed", "could not create a temporary directory for the STEP "
                                       "conversion: " + ec.message());
    return dir / (stem + ".step");
}

// The driver prints one JSON line; stderr is merged into the same stream, so take the last line
// that parses as an object.
std::optional<json> last_json_line(const std::string& output) {
    std::istringstream in(output);
    std::string line;
    std::optional<json> found;
    while (std::getline(in, line)) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (line.empty() || line.front() != '{') continue;
        json j = json::parse(line, nullptr, /*allow_exceptions=*/false);
        if (j.is_object()) found = std::move(j);
    }
    return found;
}

std::string tail(const std::string& s, std::size_t n = 400) {
    return s.size() <= n ? s : "…" + s.substr(s.size() - n);
}

}  // namespace

std::string sldprt_native_unavailable_reason() {
    if (!find_runtime())
        return "no JavaScript runtime for the native SolidWorks reader (looked for "
               "CHUDWARE_JS_RUNTIME, CHUDWARE_OMP_BIN, omp beside Chudware, and node/bun/omp on "
               "PATH)";
    if (find_driver().empty())
        return "the native SolidWorks reader (sldprt/chudware_driver.js) is not installed beside "
               "Chudware; set CHUDWARE_SLDPRT_ROOT";
    return {};
}

json sldprt_native_export_step(const std::string& src_path) {
    const std::optional<JsRuntime> rt = find_runtime();
    const fs::path driver = find_driver();
    if (!rt || driver.empty())
        throw OpError("import_failed", sldprt_native_unavailable_reason(), {{"path", src_path}});
    const fs::path out = fresh_output(fs::u8path(src_path).stem().u8string());

    util::ProcessOptions opts;
    opts.timeout = converter_timeout();
    if (rt->bun_be_bun) opts.env["BUN_BE_BUN"] = "1";
    const util::ProcessResult r =
        util::run_process({rt->exe.u8string(), driver.u8string(), src_path, out.u8string()}, opts);
    const json detail = {{"path", src_path}, {"runtime", rt->exe.u8string()}};
    if (!r.error.empty())
        throw OpError("import_failed", "native SolidWorks reader could not start: " + r.error, detail);
    if (r.timed_out)
        throw OpError("import_failed",
                      "native SolidWorks reader timed out after " +
                          std::to_string(opts.timeout.count() / 1000) +
                          " s (raise CHUDWARE_SLDPRT_TIMEOUT_S)",
                      detail);
    const std::optional<json> res = last_json_line(r.output);
    if (!res)
        throw OpError("import_failed",
                      "native SolidWorks reader returned no result (exit " +
                          std::to_string(r.exit_code) + "): " + tail(r.output),
                      detail);
    if (!res->value("ok", false) || r.exit_code != 0 || !is_file(out))
        throw OpError("import_failed",
                      "native SolidWorks reader: " + res->value("error", std::string("failed")),
                      detail);
    json result = *res;
    result["step"] = out.u8string();
    return result;
}

SolidWorksSource resolve_solidworks_source(const std::string& path, const std::string& fmt) {
    SolidWorksSource src;
    const bool parasolid = fmt == "x_t" || fmt == "x_b";
    if (fmt == "sldprt" || parasolid) {
        try {
            json r = sldprt_native_export_step(path);
            src.path = r.at("step").get<std::string>();
            src.format = "step";
            src.fidelity = "exact_native_brep";
            src.provenance = {{"reader", "sldprt (vendored)"}};
            if (r.contains("saved") && r["saved"].is_object()) src.provenance["saved"] = r["saved"];
            if (r.contains("report") && r["report"].is_object()) {
                const json& rep = r["report"];
                for (const char* k : {"uncertaintyMM", "faces", "bodies", "surfaces"})
                    if (rep.contains(k)) src.provenance[k] = rep[k];
            }
            return src;
        } catch (const OpError& e) {
            src.notes.push_back(std::string("native exact B-rep reader: ") + e.what());
        }
    }
    if (solidworks_available()) {
        try {
            src.path = sw_export_step(path);
            src.format = "step";
            src.fidelity = "solidworks_com_step";
            return src;
        } catch (const OpError& e) {
            src.notes.push_back(std::string("SolidWorks COM: ") + e.what());
        }
    } else {
        src.notes.push_back("SolidWorks COM: SolidWorks is not installed on this machine");
    }
    if (parasolid) {
        std::string why;
        for (const std::string& n : src.notes) why += (why.empty() ? "" : "; ") + n;
        throw OpError("import_failed", "Parasolid import failed — " + why, {{"path", path}});
    }
    src.path = path;
    src.format = fmt;
    src.fidelity = "saved_display_tessellation";
    return src;
}

}  // namespace chudware::doc
