// FsBackend implementations — desktop (std::filesystem) and web (gateway).
// See fs_backend.h for the contract.
#include "fs_backend.h"

#include <chrono>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <system_error>
#include <vector>

#include <nlohmann/json.hpp>

#ifdef __EMSCRIPTEN__
#include "web_fetch.h"
#include <emscripten.h>
#include <emscripten/fetch.h>
#endif

namespace billet {
namespace {

namespace fs = std::filesystem;

// ---- path helpers (shared, pure string) ------------------------------------

std::string pathFilename(const std::string& p) {
    size_t cut = p.find_last_of("/\\");
    return cut == std::string::npos ? p : p.substr(cut + 1);
}

std::string pathParent(const std::string& p) {
    size_t cut = p.find_last_of("/\\");
    if (cut == std::string::npos) return {};
    if (cut == 0) return p.substr(0, 1);  // "/"
    return p.substr(0, cut);
}

std::string pathJoin(const std::string& a, const std::string& b) {
    if (a.empty()) return b;
    if (b.empty()) return a;
    char last = a.back();
    if (last == '/' || last == '\\') return a + b;
    // Prefer '/' on web; on desktop fs::path will normalize.
#ifdef __EMSCRIPTEN__
    return a + "/" + b;
#else
    return (fs::path(a) / b).string();
#endif
}

// ============================================================================
// Desktop backend — mechanical extraction of prior file_explorer std::filesystem use.
// ============================================================================
#ifndef __EMSCRIPTEN__

class DesktopFsBackend final : public FsBackend {
public:
    std::vector<FsEntry> list(const std::string& dir, std::string& err) override {
        err.clear();
        std::vector<FsEntry> out;
        std::error_code ec;
        for (fs::directory_iterator it(dir, fs::directory_options::skip_permission_denied, ec), end;
             it != end; it.increment(ec)) {
            if (ec) {
                err = ec.message();
                break;
            }
            const fs::path& c = it->path();
            std::string name = c.filename().string();
            if (!name.empty() && name[0] == '.') continue;
            FsEntry e;
            e.name = name;
            std::error_code e2;
            e.isDir = fs::is_directory(c, e2);
            if (!e.isDir) {
                e.size = static_cast<uint64_t>(fs::file_size(c, e2));
                if (e2) e.size = 0;
            }
            auto ft = fs::last_write_time(c, e2);
            if (!e2) {
                // Portable file_clock -> system_clock unix seconds.
                using namespace std::chrono;
                auto sctp = time_point_cast<system_clock::duration>(
                    ft - fs::file_time_type::clock::now() + system_clock::now());
                e.mtime = static_cast<int64_t>(system_clock::to_time_t(sctp));
            }
            out.push_back(std::move(e));
        }
        return out;
    }

    bool mkdir(const std::string& path, std::string& err) override {
        err.clear();
        std::error_code ec;
        fs::create_directories(path, ec);
        if (ec) {
            err = ec.message();
            return false;
        }
        return true;
    }

    bool rename(const std::string& src, const std::string& dst, std::string& err) override {
        err.clear();
        std::error_code ec;
        fs::rename(src, dst, ec);
        if (ec) {
            err = ec.message();
            return false;
        }
        return true;
    }

    bool remove(const std::string& path, std::string& err) override {
        err.clear();
        std::error_code ec;
        fs::remove_all(path, ec);
        if (ec) {
            err = ec.message();
            return false;
        }
        return true;
    }

    FsStat stat(const std::string& path, std::string& err) override {
        err.clear();
        FsStat s;
        std::error_code ec;
        if (!fs::exists(path, ec) || ec) {
            if (ec) err = ec.message();
            return s;
        }
        s.exists = true;
        s.isDir  = fs::is_directory(path, ec);
        s.isFile = fs::is_regular_file(path, ec);
        if (s.isFile) {
            auto sz = fs::file_size(path, ec);
            if (!ec) s.size = static_cast<uint64_t>(sz);
        }
        auto ft = fs::last_write_time(path, ec);
        if (!ec) {
            using namespace std::chrono;
            auto sctp = time_point_cast<system_clock::duration>(
                ft - fs::file_time_type::clock::now() + system_clock::now());
            s.mtime = static_cast<int64_t>(system_clock::to_time_t(sctp));
        }
        return s;
    }

    std::string defaultRoot() override {
        // Mirrors defaultWorkspaceDir() in app_state.cpp (Documents/AI-CAD if present, else
        // Documents/Billet). Kept local so fs_backend has no app_state dependency.
#ifdef _WIN32
        const char* up = std::getenv("USERPROFILE");
        const fs::path base = up && *up ? fs::path(up) / "Documents" : fs::path(".");
#else
        const char* home = std::getenv("HOME");
        const fs::path base = home && *home ? fs::path(home) / "Documents" : fs::path(".");
#endif
        std::error_code ec;
        const fs::path legacy = base / "AI-CAD";
        if (fs::exists(legacy, ec)) return legacy.string();
        return (base / "Billet").string();
    }

    bool copy(const std::string& src, const std::string& dst, bool recursive,
              std::string& err) override {
        err.clear();
        std::error_code ec;
        if (recursive && fs::is_directory(src, ec)) {
            fs::copy(src, dst, fs::copy_options::recursive, ec);
        } else {
            fs::copy_file(src, dst, fs::copy_options::none, ec);
        }
        if (ec) {
            err = ec.message();
            return false;
        }
        return true;
    }
};

DesktopFsBackend& desktopInstance() {
    static DesktopFsBackend inst;
    return inst;
}

#endif  // !__EMSCRIPTEN__

// ============================================================================
// Web backend — gateway /workspace/* (see Webservice/gateway/workspace.py).
// ============================================================================
#ifdef __EMSCRIPTEN__

// URL-encode a path for query strings.
std::string urlEncode(const std::string& s) {
    static const char* hex = "0123456789ABCDEF";
    std::string out;
    out.reserve(s.size() * 3);
    for (unsigned char c : s) {
        if ((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
            c == '-' || c == '_' || c == '.' || c == '~' || c == '/') {
            // Keep '/' so workspace paths stay readable; FastAPI still decodes.
            out.push_back(static_cast<char>(c));
        } else if (c == ' ') {
            out += "%20";
        } else {
            out.push_back('%');
            out.push_back(hex[c >> 4]);
            out.push_back(hex[c & 0xF]);
        }
    }
    return out;
}

// Convert a server abs path (or relative-to-root) into a workspace-relative path for
// gateway query/body params. list() returns root as abs; entries return rel under root.
// We keep the abs form for daemon RPCs and strip the known root for gateway calls.
class WebFsBackend final : public FsBackend {
public:
    std::string rootAbs_;   // cached from last list() {"root": ...}
    bool        rootKnown_ = false;

    // Ensure we know the server workspace abs root (one list of "").
    bool ensureRoot(std::string& err) {
        if (rootKnown_ && !rootAbs_.empty()) return true;
        auto entries = listImpl("", /*wantRoot=*/true, err);
        (void)entries;
        return rootKnown_ && !rootAbs_.empty();
    }

    // Map an abs path (or already-relative) to a gateway-relative path.
    std::string toRel(const std::string& path) {
        if (path.empty() || path == "/") return "";
        if (!rootKnown_ || rootAbs_.empty()) {
            // Strip leading slash only.
            if (!path.empty() && path[0] == '/') return path.substr(1);
            return path;
        }
        // Exact root.
        if (path == rootAbs_ || path == rootAbs_ + "/") return "";
        // Prefix match.
        if (path.size() > rootAbs_.size() &&
            path.compare(0, rootAbs_.size(), rootAbs_) == 0 &&
            (path[rootAbs_.size()] == '/' || path[rootAbs_.size()] == '\\')) {
            return path.substr(rootAbs_.size() + 1);
        }
        // Already relative (no leading slash, or starts with something else).
        if (!path.empty() && path[0] == '/') return path.substr(1);
        return path;
    }

    // Map gateway relative path -> abs (for returning to callers / daemon).
    std::string toAbs(const std::string& rel) const {
        if (!rootKnown_ || rootAbs_.empty()) {
            if (rel.empty()) return "/";
            return rel[0] == '/' ? rel : ("/" + rel);
        }
        if (rel.empty()) return rootAbs_;
        if (rel[0] == '/') return rootAbs_ + rel;
        return rootAbs_ + "/" + rel;
    }

    // JSON POST helper (mkdir/rename/delete).
    bool postJson(const char* url, const nlohmann::json& body, nlohmann::json& out,
                  std::string& err) {
        err.clear();
        const std::string payload = body.dump();
        emscripten_fetch_attr_t attr;
        emscripten_fetch_attr_init(&attr);
        std::strcpy(attr.requestMethod, "POST");
        attr.attributes = EMSCRIPTEN_FETCH_LOAD_TO_MEMORY | EMSCRIPTEN_FETCH_SYNCHRONOUS;
        const char* headers[] = {"Content-Type", "application/json", nullptr};
        attr.requestHeaders = headers;
        attr.requestData = payload.data();
        attr.requestDataSize = payload.size();

        emscripten_fetch_t* fetch = emscripten_fetch(&attr, url);
        if (!fetch) {
            err = std::string("emscripten_fetch POST failed: ") + url;
            return false;
        }
        const int status = fetch->status;
        std::string text;
        if (fetch->data && fetch->numBytes > 0)
            text.assign(fetch->data, fetch->data + fetch->numBytes);
        emscripten_fetch_close(fetch);

        if (status < 200 || status >= 300) {
            err = "HTTP " + std::to_string(status) + " for " + url;
            if (!text.empty()) err += ": " + text.substr(0, 200);
            return false;
        }
        try {
            out = nlohmann::json::parse(text.empty() ? "{}" : text);
        } catch (const std::exception& e) {
            err = std::string("JSON parse: ") + e.what();
            return false;
        }
        return true;
    }

    std::vector<FsEntry> listImpl(const std::string& dirRel, bool wantRoot, std::string& err) {
        err.clear();
        const std::string url = std::string("/workspace/list?dir=") + urlEncode(dirRel);
        std::string fetchErr;
        auto bytes = webFetch(url, fetchErr);
        if (!fetchErr.empty() && bytes.empty()) {
            err = fetchErr;
            return {};
        }
        nlohmann::json j;
        try {
            j = nlohmann::json::parse(bytes.begin(), bytes.end());
        } catch (const std::exception& e) {
            err = std::string("list JSON: ") + e.what();
            return {};
        }
        if (j.contains("root") && j["root"].is_string()) {
            rootAbs_ = j["root"].get<std::string>();
            rootKnown_ = true;
        }
        if (wantRoot && !rootKnown_) {
            err = "list: missing root";
            return {};
        }
        std::vector<FsEntry> out;
        auto take = [&](const nlohmann::json& arr, bool isDir) {
            if (!arr.is_array()) return;
            for (const auto& it : arr) {
                if (!it.is_object()) continue;
                FsEntry e;
                e.name  = it.value("name", std::string());
                e.isDir = isDir;
                if (!isDir) {
                    e.size  = it.value("size", static_cast<uint64_t>(0));
                    e.mtime = it.value("mtime", static_cast<int64_t>(0));
                }
                if (!e.name.empty()) out.push_back(std::move(e));
            }
        };
        take(j.value("dirs", nlohmann::json::array()), true);
        take(j.value("files", nlohmann::json::array()), false);
        return out;
    }

    std::vector<FsEntry> list(const std::string& dir, std::string& err) override {
        if (!ensureRoot(err) && rootAbs_.empty()) {
            // ensureRoot may set err; still try listing with toRel best-effort
        }
        return listImpl(toRel(dir), /*wantRoot=*/false, err);
    }

    bool mkdir(const std::string& path, std::string& err) override {
        ensureRoot(err);
        nlohmann::json out;
        return postJson("/workspace/mkdir",
                        nlohmann::json{{"path", toRel(path)}}, out, err);
    }

    bool rename(const std::string& src, const std::string& dst, std::string& err) override {
        ensureRoot(err);
        nlohmann::json out;
        return postJson("/workspace/rename",
                        nlohmann::json{{"src", toRel(src)}, {"dst", toRel(dst)}}, out, err);
    }

    bool remove(const std::string& path, std::string& err) override {
        ensureRoot(err);
        nlohmann::json out;
        return postJson("/workspace/delete",
                        nlohmann::json{{"path", toRel(path)}}, out, err);
    }

    FsStat stat(const std::string& path, std::string& err) override {
        err.clear();
        FsStat s;
        if (!ensureRoot(err) && rootAbs_.empty()) {
            // fall through — may still work with relative listing
            err.clear();
        }
        const std::string rel = toRel(path);
        if (rel.empty()) {
            // Workspace root always exists as a directory once known (or by convention).
            s.exists = true;
            s.isDir  = true;
            return s;
        }
        // Stat via parent list: find the entry by name.
        const std::string parent = pathParent(rel);
        const std::string name   = pathFilename(rel);
        std::string listErr;
        auto entries = listImpl(parent, /*wantRoot=*/false, listErr);
        if (!listErr.empty() && entries.empty()) {
            // Treat list failure of parent as "does not exist" only if 404-ish; else surface.
            if (listErr.find("404") != std::string::npos ||
                listErr.find("not found") != std::string::npos) {
                return s;
            }
            err = listErr;
            return s;
        }
        for (const auto& e : entries) {
            if (e.name == name) {
                s.exists = true;
                s.isDir  = e.isDir;
                s.isFile = !e.isDir;
                s.size   = e.size;
                s.mtime  = e.mtime;
                return s;
            }
        }
        return s;  // exists=false
    }

    std::string defaultRoot() override {
        std::string err;
        if (!ensureRoot(err) || rootAbs_.empty()) {
            // Before first successful list, report "/" as the logical workspace root.
            // Callers that pass paths to the daemon must use abs paths from list/upload;
            // ensureWorkspaceDir will re-resolve once list succeeds.
            return rootAbs_.empty() ? std::string("/") : rootAbs_;
        }
        return rootAbs_;
    }

    bool copy(const std::string& src, const std::string& dst, bool recursive,
              std::string& err) override {
        ensureRoot(err);
        nlohmann::json out;
        // Prefer a single server-side copy (handles dirs recursively).
        if (postJson("/workspace/copy",
                     nlohmann::json{{"src", toRel(src)},
                                    {"dst", toRel(dst)},
                                    {"recursive", recursive}},
                     out, err)) {
            return true;
        }
        // Fallback for older gateways: single-file download+upload only.
        if (recursive) {
            // Directory copy without /workspace/copy is not supported by this fallback.
            return false;
        }
        std::string fallbackErr;
        const std::string srcRel = toRel(src);
        const std::string url = std::string("/workspace/download?path=") + urlEncode(srcRel);
        auto bytes = webFetch(url, fallbackErr);
        if (bytes.empty() && !fallbackErr.empty()) {
            err = fallbackErr;
            return false;
        }
        const std::string destName = pathFilename(dst);
        const std::string destParent = pathParent(toRel(dst));
        std::string upErr;
        std::string abs = fsUpload(destParent, destName, bytes.data(), bytes.size(), upErr);
        if (abs.empty()) {
            err = upErr.empty() ? "upload failed" : upErr;
            return false;
        }
        err.clear();
        return true;
    }

    std::vector<std::string> loadRecents(std::string& err) override {
        err.clear();
        auto bytes = webFetch("/workspace/recents", err);
        if (bytes.empty() && !err.empty()) return {};
        err.clear();
        try {
            auto j = nlohmann::json::parse(bytes.begin(), bytes.end());
            std::vector<std::string> out;
            if (j.contains("files") && j["files"].is_array()) {
                for (const auto& e : j["files"]) {
                    if (e.is_string()) out.push_back(e.get<std::string>());
                }
            }
            return out;
        } catch (const std::exception& e) {
            err = std::string("recents JSON: ") + e.what();
            return {};
        }
    }

    bool saveRecents(const std::vector<std::string>& files, std::string& err) override {
        // PUT JSON body — emscripten_fetch supports PUT.
        err.clear();
        nlohmann::json body = {{"files", files}};
        const std::string payload = body.dump();
        emscripten_fetch_attr_t attr;
        emscripten_fetch_attr_init(&attr);
        std::strcpy(attr.requestMethod, "PUT");
        attr.attributes = EMSCRIPTEN_FETCH_LOAD_TO_MEMORY | EMSCRIPTEN_FETCH_SYNCHRONOUS;
        const char* headers[] = {"Content-Type", "application/json", nullptr};
        attr.requestHeaders = headers;
        attr.requestData = payload.data();
        attr.requestDataSize = payload.size();
        emscripten_fetch_t* fetch = emscripten_fetch(&attr, "/workspace/recents");
        if (!fetch) {
            err = "emscripten_fetch PUT /workspace/recents failed";
            return false;
        }
        const int status = fetch->status;
        std::string text;
        if (fetch->data && fetch->numBytes > 0)
            text.assign(fetch->data, fetch->data + fetch->numBytes);
        emscripten_fetch_close(fetch);
        if (status < 200 || status >= 300) {
            err = "HTTP " + std::to_string(status) + " for /workspace/recents";
            if (!text.empty()) err += ": " + text.substr(0, 200);
            return false;
        }
        return true;
    }
};

WebFsBackend& webInstance() {
    static WebFsBackend inst;
    return inst;
}

#endif  // __EMSCRIPTEN__

}  // namespace

// ---- public entry points ---------------------------------------------------

FsBackend& fsBackend() {
#ifdef __EMSCRIPTEN__
    return webInstance();
#else
    return desktopInstance();
#endif
}

std::string fsUpload(const std::string& dir, const std::string& filename,
                     const uint8_t* data, size_t len, std::string& err) {
    err.clear();
#ifdef __EMSCRIPTEN__
    // Multipart form: dir (form field) + file (file field).
    // Build a minimal multipart body.
    const std::string boundary = "----BilletBoundary7MA4YWxkTrZu0gW";
    std::string body;
    body.reserve(len + 512);
    auto append = [&](const std::string& s) { body.append(s); };

    // dir field
    append("--" + boundary + "\r\n");
    append("Content-Disposition: form-data; name=\"dir\"\r\n\r\n");
    append(dir);
    append("\r\n");

    // file field
    append("--" + boundary + "\r\n");
    append("Content-Disposition: form-data; name=\"file\"; filename=\"" + filename + "\"\r\n");
    append("Content-Type: application/octet-stream\r\n\r\n");
    body.append(reinterpret_cast<const char*>(data), len);
    append("\r\n");
    append("--" + boundary + "--\r\n");

    emscripten_fetch_attr_t attr;
    emscripten_fetch_attr_init(&attr);
    std::strcpy(attr.requestMethod, "POST");
    attr.attributes = EMSCRIPTEN_FETCH_LOAD_TO_MEMORY | EMSCRIPTEN_FETCH_SYNCHRONOUS;
    const std::string ct = "multipart/form-data; boundary=" + boundary;
    const char* headers[] = {"Content-Type", ct.c_str(), nullptr};
    attr.requestHeaders = headers;
    attr.requestData = body.data();
    attr.requestDataSize = body.size();

    emscripten_fetch_t* fetch = emscripten_fetch(&attr, "/workspace/upload");
    if (!fetch) {
        err = "emscripten_fetch upload failed";
        return {};
    }
    const int status = fetch->status;
    std::string text;
    if (fetch->data && fetch->numBytes > 0)
        text.assign(fetch->data, fetch->data + fetch->numBytes);
    emscripten_fetch_close(fetch);

    if (status < 200 || status >= 300) {
        err = "HTTP " + std::to_string(status) + " upload";
        if (!text.empty()) err += ": " + text.substr(0, 200);
        return {};
    }
    try {
        auto j = nlohmann::json::parse(text);
        if (j.contains("abs_path") && j["abs_path"].is_string())
            return j["abs_path"].get<std::string>();
        if (j.contains("path") && j["path"].is_string()) {
            // Build abs from known root if needed.
            std::string rel = j["path"].get<std::string>();
            return webInstance().toAbs(rel);
        }
        err = "upload: missing abs_path";
        return {};
    } catch (const std::exception& e) {
        err = std::string("upload JSON: ") + e.what();
        return {};
    }
#else
    (void)dir;
    (void)filename;
    (void)data;
    (void)len;
    err = "fsUpload is only available in the web build";
    return {};
#endif
}

void fsTriggerDownload(const std::string& path) {
#ifdef __EMSCRIPTEN__
    // Use gateway-relative path when we know the root.
    std::string rel = path;
    {
        auto& w = webInstance();
        std::string e;
        w.ensureRoot(e);
        rel = w.toRel(path);
    }
    // window.open('/workspace/download?path=' + encodeURIComponent(p))
    EM_ASM({
        var p = UTF8ToString($0);
        var url = '/workspace/download?path=' + encodeURIComponent(p);
        window.open(url, '_blank');
    }, rel.c_str());
#else
    (void)path;
#endif
}

}  // namespace billet
