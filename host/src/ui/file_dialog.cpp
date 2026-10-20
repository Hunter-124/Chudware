// Native file dialogs — implementation. See file_dialog.h for the contract.
// Windows: the common dialogs (GetSaveFileNameW / GetOpenFileNameW).
// Other (Linux/macOS): launch kdialog (KDE) or zenity (GTK) with argv, parsing the
// same NUL-separated Win32 filter into the chosen tool's syntax. No shell.
// Emscripten: workspace tree browser via /workspace/list (Open/Save/Folder) with an
//             optional browser <input type=file> upload path for Open; afterFileWritten
//             triggers a browser download of the server path.

#ifdef __EMSCRIPTEN__
// ---------------------------------------------------------------------------
// Web (Emscripten): browser pick + gateway workspace.
// ---------------------------------------------------------------------------
#include <emscripten.h>
#include <string>

#include "file_dialog.h"
#include "../core/fs_backend.h"
#include "../core/web_file_pick.h"


namespace ui {
namespace {

// Convert a NUL-separated Win32 filter into an HTML accept= string
// ("desc\0*.step+;*.step\0...\0\0" -> ".step+,.step,...").
std::string filterToAccept(const char* filter) {
    if (!filter) return {};
    std::string out;
    const char* p = filter;
    while (p && *p) {
        // skip description
        p += std::char_traits<char>::length(p) + 1;
        if (!*p) break;
        std::string pat(p);
        p += pat.size() + 1;
        // patterns may be "a;b;c"
        size_t i = 0;
        while (i < pat.size()) {
            size_t j = pat.find(';', i);
            if (j == std::string::npos) j = pat.size();
            std::string one = pat.substr(i, j - i);
            // strip leading "*."
            if (one.size() >= 2 && one[0] == '*' && one[1] == '.') {
                one = one.substr(1);  // keep ".ext"
            } else if (one == "*.*") {
                one.clear();
            }
            if (!one.empty()) {
                if (!out.empty()) out += ',';
                out += one;
            }
            i = j + 1;
        }
    }
    return out;
}


std::string ensureExt(std::string path, const char* defExt) {
    if (!defExt || !*defExt || path.empty()) return path;
    size_t slash = path.find_last_of('/');
    size_t dot = path.find_last_of('.');
    bool hasExt = dot != std::string::npos && (slash == std::string::npos || dot > slash);
    if (!hasExt) {
        path += '.';
        path += defExt;
    }
    return path;
}

// Remember last workspace-relative folder across dialogs.
std::string s_lastDir;

}  // namespace

std::string saveFileDialog(const char* /*filter*/, const char* defExt, const char* suggestedName) {
    std::string suggest = (suggestedName && *suggestedName) ? suggestedName : "untitled";
    // Prefer basename only for the name box.
    {
        size_t cut = suggest.find_last_of("/\\");
        if (cut != std::string::npos) suggest = suggest.substr(cut + 1);
    }
    if (defExt && *defExt) {
        size_t slash = suggest.find_last_of('/');
        size_t dot = suggest.find_last_of('.');
        bool hasExt = dot != std::string::npos && (slash == std::string::npos || dot > slash);
        if (!hasExt) {
            suggest += '.';
            suggest += defExt;
        }
    }

    // Folder browser returns a workspace-relative destination.
    std::string path = chudware::browseWorkspace(chudware::WorkspaceBrowseMode::SaveAs,
                                               /*accept=*/nullptr, suggest.c_str());
    if (path.empty()) return {};
    path = ensureExt(path, defExt);
    std::string relative, pathError;
    if (!chudware::workspaceRelativePath(path, relative, pathError) || relative.empty()) return {};
    path = std::move(relative);
    // Remember parent for next dialog.
    size_t cut = path.find_last_of('/');
    s_lastDir = cut != std::string::npos && cut > 0 ? path.substr(0, cut) : ".";
    return path;
}

std::string openFileDialog(const char* filter) {
    std::string accept = filterToAccept(filter);

    // Workspace tree browser first (pick existing server file OR choose upload).
    // "Upload from my machine…" reads the file in JS (gesture-safe) into the pick
    // buffer, then returns "__UPLOAD__" so we consume + fsUpload here — never a
    // second input.click() after ASYNCIFY resume (that loses the user gesture).
    std::string chosen = chudware::browseWorkspace(chudware::WorkspaceBrowseMode::OpenFile,
                                                 accept.c_str(), /*suggestedName=*/nullptr);
    if (chosen.empty()) return {};  // cancel

    if (chosen != "__UPLOAD__") {
        // Existing file stays relative for the cell's doc_open mapping.
        std::string relative, pathError;
        if (!chudware::workspaceRelativePath(chosen, relative, pathError) ||
            relative.empty()) return {};
        chosen = std::move(relative);
        size_t cut = chosen.find_last_of('/');
        s_lastDir = cut != std::string::npos && cut > 0 ? chosen.substr(0, cut) : ".";
        return chosen;
    }

    // Bytes already delivered by chudware_web_dialogs.js → chudware_file_pick_done.
    chudware::PickedFile picked;
    std::string err;
    if (!chudware::consumePickedFile(picked, err) || picked.name.empty() || picked.data.empty()) {
        // Fallback for any caller that still signals __UPLOAD__ without bytes.
        if (err == "no pending upload") {
            if (!chudware::pickFile(accept.c_str(), picked, err) ||
                picked.name.empty() || picked.data.empty()) {
                if (!err.empty()) {
                    const std::string msg = "Upload failed: " + err;
                    EM_ASM({
                        var m = UTF8ToString($0);
                        try { window.alert(m); } catch (e) {}
                    }, msg.c_str());
                }
                return {};
            }
        } else {
            if (!err.empty()) {
                const std::string msg = "Upload failed: " + err;
                EM_ASM({
                    var m = UTF8ToString($0);
                    try { window.alert(m); } catch (e) {}
                }, msg.c_str());
            }
            return {};
        }
    }
    std::string dirRel, upErr;
    const std::string folder = s_lastDir.empty() ? "." : s_lastDir;
    if (!chudware::workspaceRelativePath(folder, dirRel, upErr)) return {};
    std::string uploaded = chudware::fsUpload(dirRel, picked.name, picked.data.data(),
                                            picked.data.size(), upErr);
    if (uploaded.empty()) {
        const std::string msg = "Upload failed: " +
            (upErr.empty() ? std::string("unknown error") : upErr);
        EM_ASM({
            var m = UTF8ToString($0);
            try { window.alert(m); } catch (e) {}
        }, msg.c_str());
        return {};
    }
    size_t cut = uploaded.find_last_of('/');
    s_lastDir = cut != std::string::npos && cut > 0 ? uploaded.substr(0, cut) : ".";
    return uploaded;
}

std::string openFolderDialog() {
    std::string chosen = chudware::browseWorkspace(chudware::WorkspaceBrowseMode::OpenFolder,
                                                 /*accept=*/nullptr, /*suggestedName=*/nullptr);
    if (chosen.empty()) return {};
    std::string relative, pathError;
    if (!chudware::workspaceRelativePath(chosen, relative, pathError)) return {};
    chosen = relative.empty() ? "." : relative;
    s_lastDir = chosen;
    return chosen;
}

int promptImportMeshAsSolid() {
    // window.confirm is Yes/No only (no Cancel) — map Cancel-less to Yes=1 / No=0.
    int r = MAIN_THREAD_EM_ASM_INT({
        var msg = "Import this mesh as a solid body?\n\n" +
                  "OK  — convert it into a solid (editable B-rep)\n" +
                  "Cancel — keep it as a graphics mesh";
        return window.confirm(msg) ? 1 : 0;
    });
    return r ? 1 : 0;
}

void afterFileWritten(const std::string& path) {
    if (!path.empty()) chudware::fsTriggerDownload(path);
}

void revealInFileManager(const std::string& /*path*/) {
    // Browser build: no OS file manager to reveal in.
}

bool openExternalFile(const std::string& /*path*/) {
    // Browser workspace files are server-side, not local OS paths.
    return false;
}

}  // namespace ui


#elif defined(_WIN32)
// ---------------------------------------------------------------------------
// Windows common dialogs.
// ---------------------------------------------------------------------------
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <commdlg.h>   // not pulled in by lean-and-mean windows.h
#include <shlobj.h>    // SHGetKnownFolderPath (FOLDERID_Documents)

#include <cstring>
#include <string>
#include <filesystem>
#include <limits>
#include <shellapi.h>

#include "file_dialog.h"

namespace ui {

// ---- UTF-8 <-> UTF-16 ----

static std::wstring widen(const std::string& s) {
    if (s.empty() || s.size() > static_cast<size_t>(std::numeric_limits<int>::max())) return {};
    int n = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.c_str(),
                                (int)s.size(), nullptr, 0);
    if (n <= 0) return {};
    std::wstring w(n, L'\0');
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, s.c_str(),
                             (int)s.size(), w.data(), n))
        return {};
    return w;
}

static std::string narrow(const wchar_t* w) {
    if (!w || !*w) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, nullptr, 0, nullptr, nullptr);
    if (n <= 1) return {};
    std::string s(n, '\0');  // include space for the terminator written by Win32
    if (!WideCharToMultiByte(CP_UTF8, 0, w, -1, s.data(), n, nullptr, nullptr))
        return {};
    s.pop_back();
    return s;
}

// The filter has embedded NULs, so strlen() only sees the first segment. Walk the
// NUL-separated segments until the empty one (the literal's own terminator supplies the
// final double NUL) and rebuild the same layout in UTF-16.
static std::wstring widenFilter(const char* filter) {
    std::wstring out;
    const char* p = filter;
    while (p && *p) {
        size_t len = std::strlen(p);
        out += widen(std::string(p, len));
        out.push_back(L'\0');
        p += len + 1;
    }
    out.push_back(L'\0');
    return out;
}

// ---- initial directory: last used, else the user's Documents folder ----

static std::wstring s_lastDir;   // remembered across dialogs so re-saves prefill

static std::wstring initialDir() {
    if (!s_lastDir.empty()) return s_lastDir;
    PWSTR docs = nullptr;
    std::wstring out;
    if (SUCCEEDED(SHGetKnownFolderPath(FOLDERID_Documents, 0, nullptr, &docs)) && docs)
        out = docs;
    if (docs) CoTaskMemFree(docs);
    return out;
}

static void rememberDir(const wchar_t* fullPath) {
    std::wstring p = fullPath ? fullPath : L"";
    size_t cut = p.find_last_of(L"\\/");
    if (cut != std::wstring::npos && cut > 0) s_lastDir = p.substr(0, cut);
}

// Append ".defExt" when the chosen file name has no extension (we do not use lpstrDefExt —
// it only honors three characters, so "step+" would become ".ste").
static std::string ensureExt(std::string path, const char* defExt) {
    if (!defExt || !*defExt || path.empty()) return path;
    size_t slash = path.find_last_of("\\/");
    size_t dot   = path.find_last_of('.');
    bool hasExt = dot != std::string::npos &&
                  (slash == std::string::npos || dot > slash);
    if (!hasExt) { path += '.'; path += defExt; }
    return path;
}

// ---- public API ----

std::string saveFileDialog(const char* filter, const char* defExt, const char* suggestedName) {
    wchar_t buf[2048] = L"";
    if (suggestedName && *suggestedName) {
        std::wstring w = widen(suggestedName);
        wcsncpy_s(buf, w.c_str(), _TRUNCATE);
    }
    std::wstring wfilter = widenFilter(filter ? filter : "All files (*.*)\0*.*\0");
    std::wstring dir = initialDir();

    OPENFILENAMEW ofn{};
    ofn.lStructSize  = sizeof(ofn);
    ofn.hwndOwner    = GetActiveWindow();
    ofn.lpstrFilter  = wfilter.c_str();
    ofn.lpstrFile    = buf;
    ofn.nMaxFile     = (DWORD)(sizeof(buf) / sizeof(buf[0]));
    ofn.lpstrInitialDir = dir.empty() ? nullptr : dir.c_str();
    ofn.Flags = OFN_OVERWRITEPROMPT | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR;
    if (!GetSaveFileNameW(&ofn)) return {};
    rememberDir(buf);
    return ensureExt(narrow(buf), defExt);
}

std::string openFileDialog(const char* filter) {
    wchar_t buf[2048] = L"";
    std::wstring wfilter = widenFilter(filter ? filter : "All files (*.*)\0*.*\0");
    std::wstring dir = initialDir();

    OPENFILENAMEW ofn{};
    ofn.lStructSize  = sizeof(ofn);
    ofn.hwndOwner    = GetActiveWindow();
    ofn.lpstrFilter  = wfilter.c_str();
    ofn.lpstrFile    = buf;
    ofn.nMaxFile     = (DWORD)(sizeof(buf) / sizeof(buf[0]));
    ofn.lpstrInitialDir = dir.empty() ? nullptr : dir.c_str();
    ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR;
    if (!GetOpenFileNameW(&ofn)) return {};
    rememberDir(buf);
    return narrow(buf);
}

std::string openFolderDialog() {
    BROWSEINFOW bi{};
    bi.hwndOwner = GetActiveWindow();
    bi.lpszTitle = L"Choose folder";
    bi.ulFlags   = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE | BIF_EDITBOX;
    LPITEMIDLIST pidl = SHBrowseForFolderW(&bi);
    if (!pidl) return {};
    wchar_t path[MAX_PATH] = L"";
    bool ok = SHGetPathFromIDListW(pidl, path);
    CoTaskMemFree(pidl);
    if (!ok) return {};
    rememberDir(path);
    return narrow(path);
}

int promptImportMeshAsSolid() {
    int r = MessageBoxW(
        GetActiveWindow(),
        L"Import this mesh as a solid body?\n\n"
        L"Yes  —  convert it into a solid (editable B-rep: cut, fillet, combine)\n"
        L"No   —  keep it as a graphics mesh (you can convert it later: "
        L"right-click the import in the tree)",
        L"Import Mesh", MB_YESNOCANCEL | MB_ICONQUESTION);
    if (r == IDCANCEL) return -1;
    return (r == IDYES) ? 1 : 0;
}

void afterFileWritten(const std::string& /*path*/) {
    // Desktop: the file is already on the user's disk.
}

bool openExternalFile(const std::string& path) {
    if (path.empty() || path.find('\0') != std::string::npos) return false;
    const std::wstring input = widen(path);
    if (input.empty()) return false;
    std::error_code ec;
    const auto absolute = std::filesystem::absolute(std::filesystem::path(input), ec);
    if (ec) return false;
    const std::wstring wide = absolute.wstring();
    return reinterpret_cast<INT_PTR>(
               ShellExecuteW(GetActiveWindow(), L"open", wide.c_str(), nullptr, nullptr,
                             SW_SHOWNORMAL)) > 32;
}

void revealInFileManager(const std::string& path) {
    if (path.empty() || path.find('\0') != std::string::npos) return;
    const std::wstring input = widen(path);
    if (input.empty()) return;
    std::error_code ec;
    const auto absolute = std::filesystem::absolute(std::filesystem::path(input), ec);
    if (ec) return;
    const std::wstring wide = absolute.wstring();
    if (std::filesystem::is_directory(absolute, ec)) {
        (void)openExternalFile(path);
        return;
    }
    // An existing apartment is usable even when its threading model differs.
    const HRESULT com = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    if (FAILED(com) && com != RPC_E_CHANGED_MODE) return;
    const bool ownsCom = SUCCEEDED(com);
    HRESULT selected = E_FAIL;
    PIDLIST_ABSOLUTE item = ILCreateFromPathW(wide.c_str());
    if (item) {
        PIDLIST_ABSOLUTE folder = ILClone(item);
        if (folder && ILRemoveLastID(folder)) {
            PCUITEMID_CHILD child = ILFindLastID(item);
            selected = SHOpenFolderAndSelectItems(folder, 1, &child, 0);
        }
        if (folder) ILFree(folder);
        ILFree(item);
    }
    if (ownsCom) CoUninitialize();
    if (FAILED(selected))
        (void)openExternalFile(absolute.parent_path().u8string());
}

}  // namespace ui

#else  // ---------------------------------------------------------------------
       // POSIX (Linux/macOS): kdialog / zenity backend.

#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>
#include <cerrno>
#include <filesystem>
#include <fcntl.h>
#include <spawn.h>
#include <sys/wait.h>
#include <thread>
#include <unistd.h>

extern char** environ;

#include "file_dialog.h"

namespace ui {
namespace {

std::string s_lastDir;  // remembered across dialogs so re-saves prefill

bool haveCmd(const char* command) {
    const char* path = std::getenv("PATH");
    if (!path) return false;
    const std::string search(path);
    size_t start = 0;
    do {
        const size_t end = search.find(':', start);
        const std::string dir = search.substr(start, end - start);
        const auto candidate = std::filesystem::path(dir.empty() ? "." : dir) / command;
        std::error_code ec;
        if (::access(candidate.c_str(), X_OK) == 0 &&
            std::filesystem::is_regular_file(candidate, ec)) return true;
        if (end == std::string::npos) break;
        start = end + 1;
    } while (start <= search.size());
    return false;
}

int waitForChild(pid_t child) {
    int status = 0;
    pid_t waited;
    do { waited = ::waitpid(child, &status, 0); } while (waited < 0 && errno == EINTR);
    return waited == child && WIFEXITED(status) ? WEXITSTATUS(status) : -1;
}

// Only this process's child is reaped; launch failure never signals another app.
pid_t spawnArgs(const std::vector<std::string>& args, int outputFd = -1,
                int readFd = -1) {
    if (args.empty()) return -1;
    std::vector<char*> argv;
    argv.reserve(args.size() + 1);
    for (const auto& arg : args) {
        if (arg.find('\0') != std::string::npos) return -1;
        argv.push_back(const_cast<char*>(arg.c_str()));
    }
    argv.push_back(nullptr);
    posix_spawn_file_actions_t actions;
    if (posix_spawn_file_actions_init(&actions) != 0) return -1;
    int error = posix_spawn_file_actions_addopen(&actions, STDIN_FILENO, "/dev/null",
                                                O_RDONLY, 0);
    if (!error)
        error = posix_spawn_file_actions_addopen(&actions, STDERR_FILENO, "/dev/null",
                                                 O_WRONLY, 0);
    if (!error && outputFd >= 0)
        error = posix_spawn_file_actions_adddup2(&actions, outputFd, STDOUT_FILENO);
    else if (!error)
        error = posix_spawn_file_actions_addopen(&actions, STDOUT_FILENO, "/dev/null",
                                                 O_WRONLY, 0);
    if (!error && outputFd > STDERR_FILENO)
        error = posix_spawn_file_actions_addclose(&actions, outputFd);
    if (!error && readFd > STDERR_FILENO)
        error = posix_spawn_file_actions_addclose(&actions, readFd);
    pid_t child = -1;
    if (!error)
        error = posix_spawnp(&child, argv[0], &actions, nullptr, argv.data(), environ);
    posix_spawn_file_actions_destroy(&actions);
    return error ? -1 : child;
}

int runArgs(const std::vector<std::string>& args, std::string* output = nullptr) {
    int pipeFd[2] = {-1, -1};
    if (output) {
        output->clear();
        if (::pipe(pipeFd) != 0) return -1;
        if (::fcntl(pipeFd[0], F_SETFD, FD_CLOEXEC) < 0 ||
            ::fcntl(pipeFd[1], F_SETFD, FD_CLOEXEC) < 0) {
            ::close(pipeFd[0]);
            ::close(pipeFd[1]);
            return -1;
        }
    }
    const pid_t child = spawnArgs(args, pipeFd[1], pipeFd[0]);
    if (pipeFd[1] >= 0) ::close(pipeFd[1]);
    bool refused = false;
    if (output && child > 0) {
        constexpr size_t kDialogOutputCap = 64 * 1024;
        char buffer[512];
        for (;;) {
            const ssize_t n = ::read(pipeFd[0], buffer, sizeof(buffer));
            if (n < 0 && errno == EINTR) continue;
            if (n < 0) refused = true;
            if (n <= 0) break;
            if (static_cast<size_t>(n) > kDialogOutputCap - output->size())
                refused = true;
            if (!refused) output->append(buffer, static_cast<size_t>(n));
        }
    }
    if (pipeFd[0] >= 0) ::close(pipeFd[0]);
    const int result = child > 0 ? waitForChild(child) : -1;
    if (output && (refused || result != 0 || output->find('\0') != std::string::npos))
        output->clear();
    return refused ? -1 : result;
}

std::string runCapture(const std::vector<std::string>& args) {
    std::string out;
    if (runArgs(args, &out) != 0) return {};
    // Remove the dialog's single record terminator, not newlines in the filename.
    if (!out.empty() && out.back() == '\n') out.pop_back();
    return out;
}

std::string absoluteLocalPath(const std::string& path) {
    if (path.empty() || path.find('\0') != std::string::npos) return {};
    std::error_code ec;
    const auto absolute = std::filesystem::absolute(std::filesystem::path(path), ec);
    return ec ? std::string() : absolute.string();
}

#ifndef __APPLE__
// A file URI is D-Bus data, not a shell/array delimiter. Encode UTF-8 bytes too.
std::string fileUri(const std::string& absolute) {
    static constexpr char hex[] = "0123456789ABCDEF";
    std::string uri = "file://";
    for (unsigned char c : absolute) {
        if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
            (c >= '0' && c <= '9') || c == '/' || c == '-' || c == '_' ||
            c == '.' || c == '~') {
            uri.push_back(static_cast<char>(c));
        } else {
            uri.push_back('%');
            uri.push_back(hex[c >> 4]);
            uri.push_back(hex[c & 15]);
        }
    }
    return uri;
}
#endif

std::string homeDir() {
    const char* h = std::getenv("HOME");
    return h ? std::string(h) : std::string(".");
}

std::string startDir() {
    if (!s_lastDir.empty()) return s_lastDir;
    // Managed sessions (chudware-web) point dialogs at the shared-projects tree.
    if (const char* d = std::getenv("CHUDWARE_START_DIR"); d && *d) return d;
    return homeDir();
}

void rememberDir(const std::string& full) {
    size_t cut = full.find_last_of('/');
    if (cut != std::string::npos) s_lastDir = full.substr(0, cut);
}

std::string ensureExt(std::string path, const char* defExt) {
    if (!defExt || !*defExt || path.empty()) return path;
    size_t slash = path.find_last_of('/');
    size_t dot = path.find_last_of('.');
    bool hasExt = dot != std::string::npos && (slash == std::string::npos || dot > slash);
    if (!hasExt) {
        path += '.';
        path += defExt;
    }
    return path;
}

// Parse the NUL-separated Win32 filter (desc\0pattern\0...\0\0) into pairs. Win32 separates
// multiple patterns with ';'; we keep them split for per-tool formatting.
struct FilterEntry {
    std::string desc;
    std::string patterns;  // e.g. "*.step+;*.step"
};
std::vector<FilterEntry> parseFilter(const char* filter) {
    std::vector<FilterEntry> out;
    const char* p = filter;
    while (p && *p) {
        std::string desc(p);
        p += desc.size() + 1;
        if (!*p) break;
        std::string pat(p);
        p += pat.size() + 1;
        out.push_back({desc, pat});
    }
    return out;
}

// kdialog filter syntax: "pat1 pat2|desc\npat3|desc2".
std::string kdialogFilter(const std::vector<FilterEntry>& f) {
    std::string out;
    for (const auto& e : f) {
        std::string pats = e.patterns;
        for (char& c : pats)
            if (c == ';') c = ' ';
        if (!out.empty()) out += "\n";
        out += pats + "|" + e.desc;
    }
    return out;
}

// zenity: one --file-filter per entry, value "desc | pat1 pat2".
void appendZenityFilters(std::vector<std::string>& args,
                         const std::vector<FilterEntry>& filters) {
    for (const auto& entry : filters) {
        std::string pats = entry.patterns;
        for (char& c : pats)
            if (c == ';') c = ' ';
        args.push_back("--file-filter=" + entry.desc + " | " + pats);
    }
}

}  // namespace

std::string saveFileDialog(const char* filter, const char* defExt, const char* suggestedName) {
    auto entries = parseFilter(filter ? filter : "All files (*.*)\0*.*\0");
    std::string suggested = (suggestedName && *suggestedName) ? suggestedName : "";
    std::string res;
    const std::string startPath = absoluteLocalPath(startDir() + "/" + suggested);
    if (startPath.empty()) return {};
    if (haveCmd("kdialog")) {
        std::vector<std::string> args{"kdialog", "--getsavefilename", startPath};
        std::string f = kdialogFilter(entries);
        if (!f.empty()) args.push_back(std::move(f));
        res = runCapture(args);
    } else if (haveCmd("zenity")) {
        std::vector<std::string> args{"zenity", "--file-selection", "--save",
                                      "--confirm-overwrite", "--filename=" + startPath};
        appendZenityFilters(args, entries);
        res = runCapture(args);
    } else {
        return {};
    }
    if (res.empty()) return {};
    rememberDir(res);
    return ensureExt(res, defExt);
}

std::string openFileDialog(const char* filter) {
    auto entries = parseFilter(filter ? filter : "All files (*.*)\0*.*\0");
    std::string res;
    const std::string startPath = absoluteLocalPath(startDir());
    if (startPath.empty()) return {};
    if (haveCmd("kdialog")) {
        std::vector<std::string> args{"kdialog", "--getopenfilename", startPath};
        std::string f = kdialogFilter(entries);
        if (!f.empty()) args.push_back(std::move(f));
        res = runCapture(args);
    } else if (haveCmd("zenity")) {
        std::vector<std::string> args{"zenity", "--file-selection",
                                      "--filename=" + startPath + "/"};
        appendZenityFilters(args, entries);
        res = runCapture(args);
    } else {
        return {};
    }
    if (res.empty()) return {};
    rememberDir(res);
    return res;
}

std::string openFolderDialog() {
    std::string res;
    const std::string startPath = absoluteLocalPath(startDir());
    if (startPath.empty()) return {};
    if (haveCmd("kdialog")) {
        res = runCapture({"kdialog", "--getexistingdirectory", startPath});
    } else if (haveCmd("zenity")) {
        res = runCapture({"zenity", "--file-selection", "--directory",
                          "--filename=" + startPath + "/"});
    } else {
        return {};
    }
    if (res.empty()) return {};
    // a directory path won't round-trip through rememberDir's dirname split cleanly;
    // just remember it as-is for the next dialog.
    s_lastDir = res;
    return res;
}

int promptImportMeshAsSolid() {
    const char* msg =
        "Import this mesh as a solid body?\n\n"
        "Yes  —  convert it into a solid (editable B-rep: cut, fillet, combine)\n"
        "No   —  keep it as a graphics mesh (you can convert it later: "
        "right-click the import in the tree)";
    if (haveCmd("kdialog")) {
        // kdialog --warningyesnocancel: exit 0=yes, 1=no, 2=cancel.
        const int code = runArgs({"kdialog", "--title", "Import Mesh",
                                   "--warningyesnocancel", msg});
        if (code == 0) return 1;
        if (code == 1) return 0;
        return -1;
    }
    if (haveCmd("zenity")) {
        // zenity --question has no cancel; exit 0=yes, else no.
        return runArgs({"zenity", "--question", "--title=Import Mesh",
                         std::string("--text=") + msg}) == 0 ? 1 : 0;
    }
    return 1;  // no dialog tool: default to solid (the more useful import)
}

void afterFileWritten(const std::string& /*path*/) {
    // Desktop: the file is already on the user's disk.
}

bool openExternalFile(const std::string& path) {
    const std::string absolute = absoluteLocalPath(path);
    if (absolute.empty()) return false;
#if defined(__APPLE__)
    const pid_t child = spawnArgs({"open", absolute});
#else
    const pid_t child = spawnArgs({"xdg-open", absolute});
#endif
    if (child <= 0) return false;
    std::thread([child] { (void)waitForChild(child); }).detach();
    return true;
}

void revealInFileManager(const std::string& path) {
    const std::string absolute = absoluteLocalPath(path);
    if (absolute.empty()) return;
    std::error_code ec;
    if (std::filesystem::is_directory(absolute, ec)) {
        (void)openExternalFile(absolute);
        return;
    }
    // Keep D-Bus/Finder waits and containing-folder fallback off the UI thread.
    std::thread([absolute] {
        const std::string folder = std::filesystem::path(absolute).parent_path().string();
#if defined(__APPLE__)
        if (runArgs({"open", "-R", absolute}) != 0)
            (void)runArgs({"open", folder});
#else
        if (runArgs({"dbus-send", "--session", "--print-reply", "--reply-timeout=2000",
                     "--dest=org.freedesktop.FileManager1", "--type=method_call",
                     "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1.ShowItems",
                     "array:string:" + fileUri(absolute), "string:"}) != 0)
            (void)runArgs({"xdg-open", folder});
#endif
    }).detach();
}

}  // namespace ui

#endif  // platform
