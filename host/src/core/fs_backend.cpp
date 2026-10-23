// FsBackend implementations — desktop (std::filesystem) and web (gateway).
// See fs_backend.h for the contract.
#include "fs_backend.h"

#include <chrono>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <system_error>
#include <stdexcept>
#include <vector>

#include <nlohmann/json.hpp>

#ifdef __EMSCRIPTEN__
#include <emscripten.h>
#include <emscripten/fetch.h>
#endif

namespace chudware {
namespace {

namespace fs = std::filesystem;

double browseNow() {
    return std::chrono::duration<double>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

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
        fs::directory_iterator it(dir, fs::directory_options::none, ec), end;
        if (ec) {
            err = ec.message();
            return {};
        }
        for (; it != end; it.increment(ec)) {
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
        // Mirrors defaultWorkspaceDir() in app_state.cpp: always Documents/Chudware
        // (the one-time legacy Documents merge runs there at GUI startup). Kept local
        // so fs_backend has no app_state dependency.
#ifdef _WIN32
        const char* up = std::getenv("USERPROFILE");
        const fs::path base = up && *up ? fs::path(up) / "Documents" : fs::path(".");
#else
        const char* home = std::getenv("HOME");
        const fs::path base = home && *home ? fs::path(home) / "Documents" : fs::path(".");
#endif
        return (base / "Chudware").string();
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
// Web backend — gateway /workspace/* (see web/gateway/workspace.py).
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

// All browser paths remain workspace-relative. The OS-enforced cell owns /work;
// no gateway filesystem root is discovered, stripped, or sent back over RPC.
class WebFsBackend final : public FsBackend {
public:

    struct BrowseEntry {
        FsDirectorySnapshot snapshot;
        double checkedAt = -1e9;
    };
    std::map<std::string, BrowseEntry> directories_;
    uint64_t browseEpoch_ = 0;
    uint64_t snapshotRevision_ = 0;

    struct BrowseRequest {
        WebFsBackend* backend;
        std::string relative;
        uint64_t epoch;
    };

    void invalidateBrowseCache(bool /*workspaceChanged*/ = false) override {
        ++browseEpoch_;
        directories_.clear();
    }

    static void browseComplete(emscripten_fetch_t* fetch) {
        auto* request = static_cast<BrowseRequest*>(fetch->userData);
        auto& backend = *request->backend;
        // No map references/pointers survive a request. Mutations and workspace
        // switches retire the entire epoch, including responses still in flight.
        if (request->epoch == backend.browseEpoch_) {
            auto it = backend.directories_.find(request->relative);
            if (it != backend.directories_.end()) {
                auto& cache = it->second;
                auto& snapshot = cache.snapshot;
                snapshot.pending = false;
                snapshot.revision = ++backend.snapshotRevision_;
                cache.checkedAt = browseNow();
                snapshot.error.clear();
                if (fetch->status < 200 || fetch->status >= 300) {
                    snapshot.state = fetch->status == 404 ? FsReadState::Missing : FsReadState::Error;
                    snapshot.error = fetch->status ? "HTTP " + std::to_string(fetch->status)
                                                   : "Workspace network error";
                    if (snapshot.state == FsReadState::Missing) snapshot.entries.clear();
                } else {
                    try {
                        if (!fetch->data || fetch->numBytes == 0)
                            throw std::runtime_error("Empty workspace listing");
                        auto json = nlohmann::json::parse(fetch->data, fetch->data + fetch->numBytes);
                        if (json.at("root") != "." || !json.at("dirs").is_array() ||
                            !json.at("files").is_array())
                            throw std::runtime_error("Invalid workspace listing");
                        std::vector<FsEntry> entries;
                        auto take = [&](const nlohmann::json& array, bool isDir) {
                            for (const auto& item : array) {
                                FsEntry entry;
                                entry.name = item.at("name").get<std::string>();
                                entry.isDir = isDir;
                                entry.size = item.value("size", uint64_t{0});
                                entry.mtime = item.value("mtime", int64_t{0});
                                if (entry.name.empty() || entry.name == "." || entry.name == ".." ||
                                    entry.name.find_first_of("/\\:") != std::string::npos ||
                                    entry.name.find('\0') != std::string::npos)
                                    throw std::runtime_error("Invalid workspace entry name");
                                entries.push_back(std::move(entry));
                            }
                        };
                        take(json.at("dirs"), true);
                        take(json.at("files"), false);
                        snapshot.entries = std::move(entries);
                        snapshot.state = FsReadState::Ready;
                    } catch (const std::exception& e) {
                        snapshot.state = FsReadState::Error;
                        snapshot.error = std::string("Workspace listing: ") + e.what();
                    }
                }
            }
        }
        delete request;
        emscripten_fetch_close(fetch);
    }

    const FsDirectorySnapshot& pollRelative(const std::string& relative) {
        auto& cache = directories_[relative];
        auto& snapshot = cache.snapshot;
        if (!snapshot.pending && browseNow() - cache.checkedAt >= 2.0) {
            snapshot.pending = true;
            auto* request = new BrowseRequest{this, relative, browseEpoch_};
            emscripten_fetch_attr_t attr;
            emscripten_fetch_attr_init(&attr);
            std::strcpy(attr.requestMethod, "GET");
            attr.attributes = EMSCRIPTEN_FETCH_LOAD_TO_MEMORY;
            attr.timeoutMSecs = 15000;
            attr.userData = request;
            attr.onsuccess = browseComplete;
            attr.onerror = browseComplete;
            const auto url = "/workspace/list?dir=" + urlEncode(relative);
            if (!emscripten_fetch(&attr, url.c_str())) {
                delete request;
                snapshot.pending = false;
                snapshot.state = FsReadState::Error;
                snapshot.error = "Could not start workspace request";
                snapshot.revision = ++snapshotRevision_;
                cache.checkedAt = browseNow();
            }
        }
        return snapshot;
    }

    const FsDirectorySnapshot& pollDirectory(const std::string& dir) override {
        std::string relative, err;
        if (!workspaceRelativePath(dir, relative, err)) {
            static const FsDirectorySnapshot outside{
                FsReadState::Error, false, 0, {}, "Invalid workspace-relative path"};
            return outside;
        }
        return pollRelative(relative);
    }

    std::string pollDefaultRoot(std::string& err) override {
        err.clear();
        return ".";
    }

    FsStatSnapshot pollStat(const std::string& path) override {
        FsStatSnapshot result;
        std::string relative;
        if (!workspaceRelativePath(path, relative, result.error)) {
            result.state = FsReadState::Error;
            return result;
        }
        const bool isRoot = relative.empty();
        const auto& directory = pollDirectory(isRoot ? "." : pathParent(relative));
        result.state = directory.state;
        result.error = directory.error;
        if (directory.state == FsReadState::Missing) {
            result.state = FsReadState::Ready;
        } else if (directory.state == FsReadState::Ready) {
            if (isRoot) {
                result.value.exists = result.value.isDir = true;
            } else {
                const auto name = pathFilename(relative);
                for (const auto& entry : directory.entries) {
                    if (entry.name != name) continue;
                    result.value = {true, entry.isDir, !entry.isDir, entry.size, entry.mtime};
                    break;
                }
            }
        }
        return result;
    }


    // ---- ASYNCIFY-yielding fetch helpers -----------------------------------
    // Main-thread ImGui + ASYNCIFY (no PROXY_TO_PTHREAD): a SYNCHRONOUS
    // emscripten_fetch never completes and wedges the tab. Pattern matches
    // web_file_pick.cpp — fire async fetch, then emscripten_sleep until the
    // callback marks done. Payload/response live on the heap across the yield.

    struct FetchWait {
        volatile int done = 0;  // 0=pending, 1=ok, -1=err
        int          status = 0;
        std::string  body;
        std::string  err;
    };

    // Heap-owned request body for POST/PUT; freed in callbacks.
    struct OwnedPayload {
        char*  data = nullptr;
        size_t size = 0;
        FetchWait* wait = nullptr;
        OwnedPayload(const void* src, size_t n, FetchWait* w) : size(n), wait(w) {
            if (n == 0) { data = nullptr; return; }
            data = static_cast<char*>(std::malloc(n));
            if (data && src) std::memcpy(data, src, n);
        }
        ~OwnedPayload() { /* freed by fetch callback */ }
    };

    static void fetchSuccess(emscripten_fetch_t* f) {
        auto* own = static_cast<OwnedPayload*>(f->userData);
        FetchWait* w = own ? own->wait : nullptr;
        if (w) {
            w->status = f->status;
            if (f->data && f->numBytes > 0)
                w->body.assign(f->data, f->data + f->numBytes);
            if (f->status >= 200 && f->status < 300) {
                w->done = 1;
            } else {
                w->err = "HTTP " + std::to_string(f->status);
                if (!w->body.empty()) w->err += ": " + w->body.substr(0, 200);
                w->done = -1;
            }
        }
        if (own) {
            std::free(own->data);
            delete own;
        }
        emscripten_fetch_close(f);
    }

    static void fetchError(emscripten_fetch_t* f) {
        auto* own = static_cast<OwnedPayload*>(f->userData);
        FetchWait* w = own ? own->wait : nullptr;
        if (w) {
            w->status = f ? f->status : 0;
            w->err = "network error";
            if (f && f->status)
                w->err = "HTTP " + std::to_string(f->status);
            w->done = -1;
        }
        if (own) {
            std::free(own->data);
            delete own;
        }
        if (f) emscripten_fetch_close(f);
    }

    // Blocking GET with ASYNCIFY yield. Returns body bytes; sets err on failure.
    static std::string fetchGetBlocking(const std::string& url, std::string& err,
                                        int* status = nullptr) {
        err.clear();
        if (status) *status = 0;
        FetchWait wait;
        auto* own = new OwnedPayload(nullptr, 0, &wait);
        emscripten_fetch_attr_t attr;
        emscripten_fetch_attr_init(&attr);
        std::strcpy(attr.requestMethod, "GET");
        attr.attributes = EMSCRIPTEN_FETCH_LOAD_TO_MEMORY;  // async
        attr.userData = own;
        attr.onsuccess = fetchSuccess;
        attr.onerror = fetchError;
        emscripten_fetch_t* fetch = emscripten_fetch(&attr, url.c_str());
        if (!fetch) {
            std::free(own->data);
            delete own;
            err = "emscripten_fetch failed: " + url;
            return {};
        }
        while (wait.done == 0) emscripten_sleep(5);
        if (status) *status = wait.status;
        if (wait.done < 0) {
            err = wait.err.empty() ? ("fetch failed: " + url) : (wait.err + " for " + url);
            return {};
        }
        return std::move(wait.body);
    }

    // Blocking POST/PUT with ASYNCIFY yield. method is "POST" or "PUT".
    static bool fetchWriteBlocking(const char* method, const char* url,
                                  const void* body, size_t bodyLen,
                                  const char* contentType,
                                  std::string& response, std::string& err) {
        err.clear();
        response.clear();
        FetchWait wait;
        auto* own = new OwnedPayload(body, bodyLen, &wait);
        if (bodyLen > 0 && !own->data) {
            delete own;
            err = "out of memory";
            return false;
        }
        emscripten_fetch_attr_t attr;
        emscripten_fetch_attr_init(&attr);
        std::strcpy(attr.requestMethod, method);
        attr.attributes = EMSCRIPTEN_FETCH_LOAD_TO_MEMORY;  // async
        static thread_local char ctBuf[128];
        const char* headers[3] = {nullptr, nullptr, nullptr};
        if (contentType && *contentType) {
            std::strncpy(ctBuf, contentType, sizeof(ctBuf) - 1);
            ctBuf[sizeof(ctBuf) - 1] = '\0';
            headers[0] = "Content-Type";
            headers[1] = ctBuf;
            headers[2] = nullptr;
            attr.requestHeaders = headers;
        }
        attr.requestData = own->data;
        attr.requestDataSize = own->size;
        attr.userData = own;
        attr.onsuccess = fetchSuccess;
        attr.onerror = fetchError;
        emscripten_fetch_t* fetch = emscripten_fetch(&attr, url);
        if (!fetch) {
            std::free(own->data);
            delete own;
            err = std::string("emscripten_fetch ") + method + " failed: " + url;
            return false;
        }
        while (wait.done == 0) emscripten_sleep(5);
        if (wait.done < 0) {
            err = wait.err.empty() ? (std::string(method) + " failed: " + url)
                                  : (wait.err + " for " + url);
            return false;
        }
        response = std::move(wait.body);
        return true;
    }

    // JSON POST helper (mkdir/rename/delete/copy) — ASYNCIFY-yielding.
    bool postJson(const char* url, const nlohmann::json& body, nlohmann::json& out,
                  std::string& err) {
        err.clear();
        const std::string payload = body.dump();
        invalidateBrowseCache();
        // Also retire reads completed while ASYNCIFY yielded for this mutation,
        // even on ambiguous network failure: the server may have committed it.
        struct MutationFence {
            WebFsBackend& backend;
            ~MutationFence() { backend.invalidateBrowseCache(); }
        } fence{*this};
        std::string text;
        if (!fetchWriteBlocking("POST", url, payload.data(), payload.size(),
                               "application/json", text, err)) {
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

    std::vector<FsEntry> listImpl(const std::string& dirRel, std::string& err,
                                  int* status = nullptr) {
        err.clear();
        const std::string url = std::string("/workspace/list?dir=") + urlEncode(dirRel);
        std::string fetchErr;
        auto text = fetchGetBlocking(url, fetchErr, status);
        if (!fetchErr.empty() && text.empty()) {
            err = fetchErr;
            return {};
        }
        nlohmann::json j;
        try {
            j = nlohmann::json::parse(text.empty() ? "{}" : text);
        } catch (const std::exception& e) {
            err = std::string("list JSON: ") + e.what();
            return {};
        }
        if (!j.is_object() || j.value("root", std::string()) != "." ||
            !j.contains("dirs") || !j["dirs"].is_array() ||
            !j.contains("files") || !j["files"].is_array()) {
            err = "Invalid workspace listing";
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
                if (e.name.empty() || e.name == "." || e.name == ".." ||
                    e.name.find_first_of("/\\:") != std::string::npos ||
                    e.name.find('\0') != std::string::npos) {
                    err = "Invalid workspace entry name";
                    return;
                }
                out.push_back(std::move(e));
            }
        };
        take(j.value("dirs", nlohmann::json::array()), true);
        take(j.value("files", nlohmann::json::array()), false);
        return err.empty() ? out : std::vector<FsEntry>{};
    }

    std::vector<FsEntry> list(const std::string& dir, std::string& err) override {
        std::string relative;
        if (!workspaceRelativePath(dir, relative, err)) return {};
        return listImpl(relative, err);
    }

    bool mkdir(const std::string& path, std::string& err) override {
        std::string relative;
        if (!workspaceRelativePath(path, relative, err)) return false;
        nlohmann::json out;
        return postJson("/workspace/mkdir", {{"path", relative}}, out, err);
    }

    bool rename(const std::string& src, const std::string& dst, std::string& err) override {
        std::string source, destination;
        if (!workspaceRelativePath(src, source, err) ||
            !workspaceRelativePath(dst, destination, err)) return false;
        nlohmann::json out;
        return postJson("/workspace/rename", {{"src", source}, {"dst", destination}}, out, err);
    }

    bool remove(const std::string& path, std::string& err) override {
        std::string relative;
        if (!workspaceRelativePath(path, relative, err)) return false;
        nlohmann::json out;
        return postJson("/workspace/delete", {{"path", relative}}, out, err);
    }

    FsStat stat(const std::string& path, std::string& err) override {
        err.clear();
        FsStat s;
        std::string rel;
        if (!workspaceRelativePath(path, rel, err)) return s;
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
        int listStatus = 0;
        auto entries = listImpl(parent, listErr, &listStatus);
        if (listStatus == 404) return s;
        if (!listErr.empty()) {
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

    std::string defaultRoot() override { return "."; }

    bool copy(const std::string& src, const std::string& dst, bool recursive,
              std::string& err) override {
        std::string source, destination;
        if (!workspaceRelativePath(src, source, err) ||
            !workspaceRelativePath(dst, destination, err)) return false;
        nlohmann::json out;
        // Never retry a failed mutation with download/upload: an ambiguous
        // response may already have copied it, and upload could overwrite dst.
        return postJson("/workspace/copy",
                        nlohmann::json{{"src", source},
                                       {"dst", destination},
                                       {"recursive", recursive}}, out, err);
    }

    std::vector<std::string> loadRecents(std::string& err) override {
        // Startup-only durable load; render-time recents use pollStat instead.
        err.clear();
        // Best-effort blocking GET via ASYNCIFY — loadSettings runs once at boot on the
        // main thread where a short yield is acceptable and keeps the API sync for callers.
        auto text = fetchGetBlocking("/workspace/recents", err);
        if (text.empty() && !err.empty()) return {};
        err.clear();
        try {
            auto j = nlohmann::json::parse(text.empty() ? "{}" : text);
            std::vector<std::string> out;
            if (j.contains("files") && j["files"].is_array()) {
                for (const auto& e : j["files"]) {
                    std::string relative, pathError;
                    if (e.is_string() &&
                        workspaceRelativePath(e.get<std::string>(), relative, pathError) &&
                        !relative.empty()) out.push_back(std::move(relative));
                }
            }
            return out;
        } catch (const std::exception& e) {
            err = std::string("recents JSON: ") + e.what();
            return {};
        }
    }

    bool saveRecents(const std::vector<std::string>& files, std::string& err) override {
        // Fire-and-forget async PUT. A SYNCHRONOUS emscripten_fetch on the browser
        // main thread wedges the tab: this build is main-thread ImGui + ASYNCIFY with
        // no PROXY_TO_PTHREAD, so a sync fetch here never completes (same class as the
        // doc_tabs.cpp mesh-load note). saveSettings() runs on the GL/UI (main) thread,
        // and setActiveProjectDir() -> saveSettings() calls this, so a sync PUT froze
        // the page on "open a project folder" (PLM tab / title bar / onboarding).
        // Recents persistence is best-effort, so post the write and return immediately;
        // the payload is heap-owned until the fetch's success/error callback frees it.
        err.clear();
        for (const auto& file : files) {
            std::string relative;
            if (!workspaceRelativePath(file, relative, err)) return false;
        }
        nlohmann::json body = {{"files", files}};
        const std::string payload = body.dump();
        char* buf = static_cast<char*>(std::malloc(payload.size()));
        if (!buf) { err = "out of memory"; return false; }
        std::memcpy(buf, payload.data(), payload.size());
        emscripten_fetch_attr_t attr;
        emscripten_fetch_attr_init(&attr);
        std::strcpy(attr.requestMethod, "PUT");
        attr.attributes = EMSCRIPTEN_FETCH_LOAD_TO_MEMORY;  // async (no EMSCRIPTEN_FETCH_SYNCHRONOUS)
        static const char* headers[] = {"Content-Type", "application/json", nullptr};
        attr.requestHeaders = headers;
        attr.requestData = buf;
        attr.requestDataSize = payload.size();
        attr.userData = buf;
        attr.onsuccess = [](emscripten_fetch_t* f) {
            std::free(f->userData);
            emscripten_fetch_close(f);
        };
        attr.onerror = [](emscripten_fetch_t* f) {
            std::free(f->userData);
            emscripten_fetch_close(f);
        };
        emscripten_fetch(&attr, "/workspace/recents");
        return true;  // best-effort; completion delivered via callbacks
    }
};

WebFsBackend& webInstance() {
    static WebFsBackend inst;
    return inst;
}

#endif  // __EMSCRIPTEN__

}  // namespace

// ---- public entry points ---------------------------------------------------

const FsDirectorySnapshot& FsBackend::pollDirectory(const std::string& dir) {
    auto& cache = browseCache_[dir];
    if (browseNow() - cache.checkedAt >= 2.0) {
        cache.checkedAt = browseNow();
        auto& snapshot = cache.snapshot;
        auto entries = list(dir, snapshot.error);
        snapshot.state = snapshot.error.empty() ? FsReadState::Ready : FsReadState::Error;
        snapshot.revision = ++browseRevision_;
        if (snapshot.error.empty()) snapshot.entries = std::move(entries);
        else {
            std::string statError;
            const auto directory = stat(dir, statError);
            if (statError.empty() && !directory.exists) {
                snapshot.state = FsReadState::Missing;
                snapshot.entries.clear();
            }
        }
    }
    return cache.snapshot;
}

FsStatSnapshot FsBackend::pollStat(const std::string& path) {
    FsStatSnapshot result;
    result.value = stat(path, result.error);
    result.state = result.error.empty() ? FsReadState::Ready : FsReadState::Error;
    return result;
}

void FsBackend::invalidateBrowseCache(bool /*workspaceChanged*/) {
    browseCache_.clear();
}

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
    std::string relative;
    if (!workspaceRelativePath(dir, relative, err)) return {};
    std::string safeName;
    if (!workspaceRelativePath(filename, safeName, err) || safeName.empty() ||
        safeName != filename || filename.find('/') != std::string::npos ||
        filename.find_first_of("\"\r\n") != std::string::npos) {
        err = "Invalid upload filename";
        return {};
    }
    webInstance().invalidateBrowseCache();
    struct UploadFence {
        ~UploadFence() { webInstance().invalidateBrowseCache(); }
    } fence;
    // Multipart form: dir (form field) + file (file field).
    // ASYNCIFY-yielding upload — never EMSCRIPTEN_FETCH_SYNCHRONOUS on main thread.
    const std::string boundary = "----ChudwareBoundary7MA4YWxkTrZu0gW";
    std::string body;
    body.reserve(len + 512);
    auto append = [&](const std::string& s) { body.append(s); };

    // dir field
    append("--" + boundary + "\r\n");
    append("Content-Disposition: form-data; name=\"dir\"\r\n\r\n");
    append(relative);
    append("\r\n");

    // file field
    append("--" + boundary + "\r\n");
    append("Content-Disposition: form-data; name=\"file\"; filename=\"" + filename + "\"\r\n");
    append("Content-Type: application/octet-stream\r\n\r\n");
    body.append(reinterpret_cast<const char*>(data), len);
    append("\r\n");
    append("--" + boundary + "--\r\n");

    struct UploadWait {
        volatile int done = 0;
        int status = 0;
        std::string text;
        std::string err;
        char* payload = nullptr;
    };
    UploadWait wait;
    wait.payload = static_cast<char*>(std::malloc(body.size()));
    if (!wait.payload) { err = "out of memory"; return {}; }
    std::memcpy(wait.payload, body.data(), body.size());

    emscripten_fetch_attr_t attr;
    emscripten_fetch_attr_init(&attr);
    std::strcpy(attr.requestMethod, "POST");
    attr.attributes = EMSCRIPTEN_FETCH_LOAD_TO_MEMORY;  // async
    const std::string ct = "multipart/form-data; boundary=" + boundary;
    const char* headers[] = {"Content-Type", ct.c_str(), nullptr};
    attr.requestHeaders = headers;
    attr.requestData = wait.payload;
    attr.requestDataSize = body.size();
    attr.userData = &wait;
    attr.onsuccess = [](emscripten_fetch_t* f) {
        auto* w = static_cast<UploadWait*>(f->userData);
        w->status = f->status;
        if (f->data && f->numBytes > 0)
            w->text.assign(f->data, f->data + f->numBytes);
        if (f->status >= 200 && f->status < 300) w->done = 1;
        else {
            w->err = "HTTP " + std::to_string(f->status) + " upload";
            if (!w->text.empty()) w->err += ": " + w->text.substr(0, 200);
            w->done = -1;
        }
        std::free(w->payload);
        w->payload = nullptr;
        emscripten_fetch_close(f);
    };
    attr.onerror = [](emscripten_fetch_t* f) {
        auto* w = static_cast<UploadWait*>(f->userData);
        w->status = f ? f->status : 0;
        w->err = w->status ? ("HTTP " + std::to_string(w->status) + " upload")
                           : "upload network error";
        w->done = -1;
        std::free(w->payload);
        w->payload = nullptr;
        if (f) emscripten_fetch_close(f);
    };

    emscripten_fetch_t* fetch = emscripten_fetch(&attr, "/workspace/upload");
    if (!fetch) {
        std::free(wait.payload);
        err = "emscripten_fetch upload failed";
        return {};
    }
    while (wait.done == 0) emscripten_sleep(5);
    if (wait.done < 0) {
        err = wait.err.empty() ? "upload failed" : wait.err;
        return {};
    }
    try {
        auto j = nlohmann::json::parse(wait.text.empty() ? "{}" : wait.text);
        if (j.contains("path") && j["path"].is_string()) {
            std::string result;
            if (workspaceRelativePath(j["path"].get<std::string>(), result, err) &&
                !result.empty()) return result;
            err = "upload: invalid workspace path";
            return {};
        }
        err = "upload: missing path";
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
    std::string rel, err;
    if (!workspaceRelativePath(path, rel, err) || rel.empty()) return;
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

}  // namespace chudware
