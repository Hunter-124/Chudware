// FsBackend — filesystem abstraction for desktop (std::filesystem) and web
// (gateway /workspace/*). File Explorer + file dialogs go through this so the
// WASM client can browse/mutate the server workspace without a local FS.
//
// Paths:
//   Desktop: ordinary OS paths (absolute).
//   Web: workspace-relative paths only; "." is the logical workspace root.
//        The cell maps RPC paths beneath /work. Gateway host roots never reach
//        the browser, and are never accepted as workspace paths.
#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <string_view>
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

enum class FsReadState { Loading, Ready, Missing, Error };

struct FsDirectorySnapshot {
    FsReadState state = FsReadState::Loading;
    bool pending = false;
    uint64_t revision = 0;
    std::vector<FsEntry> entries;
    std::string error;
};

struct FsStatSnapshot {
    FsReadState state = FsReadState::Loading;
    FsStat value;
    std::string error;
};

// Abstract FS. Thread affinity: call from the GL/UI thread. The command APIs below
// await confirmed results (web uses ASYNCIFY). Rendering MUST use the poll APIs:
// web polling starts asynchronous reads and returns immediately, without yielding.
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
    // Web: "." (logical workspace root; never a server filesystem path).
    virtual std::string defaultRoot() = 0;

    // Render-time reads. Snapshots refresh after two seconds, coalesce requests,
    // and distinguish pending/error from confirmed absence. A snapshot reference
    // is valid only until invalidation; do not retain it across a mutation/yield.
    virtual const FsDirectorySnapshot& pollDirectory(const std::string& dir);
    virtual FsStatSnapshot pollStat(const std::string& path);
    // Empty while discovering the web root; error is not a placeholder path.
    virtual std::string pollDefaultRoot(std::string& err) {
        err.clear();
        return defaultRoot();
    }
    // Workspace changes retire in-flight workspace reads.
    virtual void invalidateBrowseCache(bool workspaceChanged = false);

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
private:
    struct DirectoryCache {
        FsDirectorySnapshot snapshot;
        double checkedAt = -1e9;
    };
    std::map<std::string, DirectoryCache> browseCache_;
    uint64_t browseRevision_ = 0;
};


// Validate and canonicalize a browser workspace reference without filesystem IO.
// Reject rather than reinterpret OS paths, traversal, backslashes or embedded NUL.
inline bool workspaceRelativePath(const std::string& path, std::string& relative,
                                  std::string& err) {
    relative.clear();
    err.clear();
    if ((!path.empty() && path.front() == '/') ||
        path.find_first_of("\\:") != std::string::npos ||
        path.find('\0') != std::string::npos) {
        err = "Invalid workspace-relative path";
        return false;
    }
    size_t start = 0;
    while (start < path.size()) {
        size_t end = path.find('/', start);
        if (end == std::string::npos) end = path.size();
        const std::string_view component(path.data() + start, end - start);
        if (component == "..") {
            relative.clear();
            err = "Workspace path traversal is not allowed";
            return false;
        }
        if (!component.empty() && component != ".") {
            if (!relative.empty()) relative += '/';
            relative += component;
        }
        start = end + 1;
    }
    return true;
}

// Process-wide backend (desktop or web, selected at compile time).
FsBackend& fsBackend();

// Web-only helpers (no-ops / fail on desktop). Used by file_dialog import/export.
// Upload bytes as `filename` under workspace `dir` ("" = root). Returns relative path.
std::string fsUpload(const std::string& dir, const std::string& filename,
                     const uint8_t* data, size_t len, std::string& err);
// Trigger a browser download of a server workspace path (GET /workspace/download).
void fsTriggerDownload(const std::string& path);

}  // namespace chudware
