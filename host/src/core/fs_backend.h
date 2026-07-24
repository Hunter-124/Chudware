// FsBackend — filesystem abstraction for desktop (std::filesystem) and web
// (gateway /workspace/*). File Explorer + file dialogs go through this so the
// WASM client can browse/mutate the server workspace without a local FS.
//
// Paths:
//   Desktop: ordinary OS paths (absolute).
//   Web:     server-side absolute paths returned by the gateway (upload's
//            abs_path, list's root + entry path). defaultRoot() is that server
//            workspace root. Daemon RPCs (doc_open/doc_save/export_file) take
//            these same abs paths.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace chudware {

struct FsEntry {
    std::string name;
    bool        isDir = false;
    uint64_t    size  = 0;   // bytes; 0 for directories
    int64_t     mtime = 0;   // unix seconds; 0 if unknown
};

struct FsStat {
    bool     exists = false;
    bool     isDir  = false;
    bool     isFile = false;
    uint64_t size   = 0;
    int64_t  mtime  = 0;
};

// Abstract FS. Thread affinity: call from the GL/UI thread. Web gateway calls use
// async emscripten_fetch + ASYNCIFY yields (emscripten_sleep) — never EMSCRIPTEN_FETCH_SYNCHRONOUS
// on the main thread (this build is main-thread ImGui + ASYNCIFY, no PROXY_TO_PTHREAD).
class FsBackend {
public:
    virtual ~FsBackend() = default;

    // Directory listing. On success returns entries (dirs + files); on failure
    // returns {} and sets err. Does not filter by extension — callers do that.
    virtual std::vector<FsEntry> list(const std::string& dir, std::string& err) = 0;

    // Create a single directory (parents created as needed). true on success.
    virtual bool mkdir(const std::string& path, std::string& err) = 0;

    // Rename/move src -> dst. Fails if dst exists (web) or on OS error (desktop).
    virtual bool rename(const std::string& src, const std::string& dst, std::string& err) = 0;

    // Remove a file or a directory tree (recursive).
    virtual bool remove(const std::string& path, std::string& err) = 0;

    // Stat a path. exists=false is not an error (err stays empty).
    virtual FsStat stat(const std::string& path, std::string& err) = 0;

    // Workspace root. Desktop: Documents/Chudware or Documents/Chudware.
    // Web: server abs workspace root (from gateway list).
    virtual std::string defaultRoot() = 0;

    // ---- convenience (implemented in terms of the above where possible) ----

    // Copy file or (when recursive) directory tree. Desktop uses std::filesystem;
    // web posts /workspace/copy (recursive server-side).
    virtual bool copy(const std::string& src, const std::string& dst, bool recursive,
                      std::string& err) = 0;

    // Web-only: load/save durable recents from workspace .chudware_recents.json.
    // Desktop no-ops (returns empty / true) — desktop uses config.json.
    virtual std::vector<std::string> loadRecents(std::string& err) {
        (void)err;
        return {};
    }
    virtual bool saveRecents(const std::vector<std::string>& files, std::string& err) {
        (void)files;
        (void)err;
        return true;
    }

    bool exists(const std::string& path) {
        std::string e;
        return stat(path, e).exists;
    }
    bool isDirectory(const std::string& path) {
        std::string e;
        return stat(path, e).isDir;
    }
    bool isRegularFile(const std::string& path) {
        std::string e;
        return stat(path, e).isFile;
    }
};

// Process-wide backend (desktop or web, selected at compile time).
FsBackend& fsBackend();

// Web-only helpers (no-ops / fail on desktop). Used by file_dialog import/export.
// Upload bytes as `filename` under workspace `dir` ("" = root). Returns abs_path.
std::string fsUpload(const std::string& dir, const std::string& filename,
                     const uint8_t* data, size_t len, std::string& err);
// Trigger a browser download of a server workspace path (GET /workspace/download).
void fsTriggerDownload(const std::string& path);

}  // namespace chudware
