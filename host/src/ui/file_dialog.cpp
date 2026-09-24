// Native file dialogs — implementation. See file_dialog.h for the contract.
// Windows: the common dialogs (GetSaveFileNameW / GetOpenFileNameW).
// Other (Linux/macOS): shell out to kdialog (KDE) or zenity (GTK), parsing the same
// NUL-separated Win32 filter into the chosen tool's syntax.
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

std::string pathJoinWs(const std::string& dir, const std::string& name) {
    if (dir.empty() || dir == "/") return dir == "/" ? ("/" + name) : name;
    char last = dir.back();
    if (last == '/' || last == '\\') return dir + name;
    return dir + "/" + name;
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

// Remember last save/open folder (server abs) across dialogs.
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

    // Folder browser + filename (server abs path under workspace).
    std::string path = chudware::browseWorkspace(chudware::WorkspaceBrowseMode::SaveAs,
                                               /*accept=*/nullptr, suggest.c_str());
    if (path.empty()) return {};
    path = ensureExt(path, defExt);
    // Remember parent for next dialog.
    size_t cut = path.find_last_of('/');
    if (cut != std::string::npos && cut > 0) s_lastDir = path.substr(0, cut);
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
        // Existing workspace file — return server abs path for doc_open.
        size_t cut = chosen.find_last_of('/');
        if (cut != std::string::npos && cut > 0) s_lastDir = chosen.substr(0, cut);
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
    auto& fsb = chudware::fsBackend();
    std::string root = s_lastDir.empty() ? fsb.defaultRoot() : s_lastDir;
    // Gateway wants a relative dir under the workspace; "" = root.
    // Upload into last-browsed folder when known, else workspace root.
    std::string dirRel;
    if (!root.empty() && root != "/") {
        // Best-effort: if root is a subfolder of the workspace abs root, strip it.
        std::string absRoot = fsb.defaultRoot();
        if (!absRoot.empty() && root.size() > absRoot.size() &&
            root.compare(0, absRoot.size(), absRoot) == 0 &&
            (root[absRoot.size()] == '/' || root[absRoot.size()] == '\\')) {
            dirRel = root.substr(absRoot.size() + 1);
        } else if (root != absRoot) {
            // Already relative-looking.
            if (!root.empty() && root[0] != '/') dirRel = root;
        }
    }
    std::string upErr;
    std::string abs = chudware::fsUpload(dirRel, picked.name, picked.data.data(),
                                       picked.data.size(), upErr);
    if (abs.empty()) {
        const std::string msg = "Upload failed: " +
            (upErr.empty() ? std::string("unknown error") : upErr);
        EM_ASM({
            var m = UTF8ToString($0);
            try { window.alert(m); } catch (e) {}
        }, msg.c_str());
        return {};
    }
    size_t cut = abs.find_last_of('/');
    if (cut != std::string::npos && cut > 0) s_lastDir = abs.substr(0, cut);
    return abs;
}

std::string openFolderDialog() {
    std::string chosen = chudware::browseWorkspace(chudware::WorkspaceBrowseMode::OpenFolder,
                                                 /*accept=*/nullptr, /*suggestedName=*/nullptr);
    if (chosen.empty()) return {};
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

#include "file_dialog.h"

namespace ui {

// ---- UTF-8 <-> UTF-16 ----

static std::wstring widen(const std::string& s) {
    if (s.empty()) return {};
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

void revealInFileManager(const std::string& path) {
    if (path.empty()) return;
    const std::wstring wide = widen(path);
    if (wide.empty()) return;
    // SHOpenFolderAndSelectItems requires COM on the calling thread. S_FALSE
    // still requires a matching CoUninitialize; a failed init owns nothing.
    const HRESULT com = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    if (FAILED(com)) return;
    PIDLIST_ABSOLUTE item = ILCreateFromPathW(wide.c_str());
    if (!item) {
        CoUninitialize();
        return;
    }
    PIDLIST_ABSOLUTE folder = ILClone(item);
    if (folder && ILRemoveLastID(folder)) {
        PCUITEMID_CHILD child = ILFindLastID(item);
        (void)SHOpenFolderAndSelectItems(folder, 1, &child, 0);
    }
    if (folder) ILFree(folder);
    ILFree(item);
    CoUninitialize();
}

}  // namespace ui

#else  // ---------------------------------------------------------------------
       // POSIX (Linux/macOS): kdialog / zenity backend.

#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include "file_dialog.h"

namespace ui {
namespace {

std::string s_lastDir;  // remembered across dialogs so re-saves prefill

bool haveCmd(const char* c) {
    return std::system((std::string("command -v ") + c + " >/dev/null 2>&1").c_str()) == 0;
}

// Capture a command's stdout (trimmed of trailing newlines).
std::string runCapture(const std::string& cmd) {
    std::string out;
    FILE* p = popen(cmd.c_str(), "r");
    if (!p) return "";
    char buf[512];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), p)) > 0) out.append(buf, n);
    pclose(p);
    while (!out.empty() && (out.back() == '\n' || out.back() == '\r')) out.pop_back();
    return out;
}

// Single-quote an argument for the shell (escaping embedded single quotes).
std::string shq(const std::string& s) {
    std::string o = "'";
    for (char c : s) {
        if (c == '\'')
            o += "'\\''";
        else
            o += c;
    }
    o += "'";
    return o;
}

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
std::string zenityFilters(const std::vector<FilterEntry>& f) {
    std::string out;
    for (const auto& e : f) {
        std::string pats = e.patterns;
        for (char& c : pats)
            if (c == ';') c = ' ';
        out += " --file-filter=" + shq(e.desc + " | " + pats);
    }
    return out;
}

}  // namespace

std::string saveFileDialog(const char* filter, const char* defExt, const char* suggestedName) {
    auto entries = parseFilter(filter ? filter : "All files (*.*)\0*.*\0");
    std::string suggested = (suggestedName && *suggestedName) ? suggestedName : "";
    std::string res;
    if (haveCmd("kdialog")) {
        std::string startPath = startDir() + "/" + suggested;
        std::string cmd = "kdialog --getsavefilename " + shq(startPath);
        std::string f = kdialogFilter(entries);
        if (!f.empty()) cmd += " " + shq(f);
        res = runCapture(cmd + " 2>/dev/null");
    } else if (haveCmd("zenity")) {
        std::string startPath = startDir() + "/" + suggested;
        std::string cmd = "zenity --file-selection --save --confirm-overwrite --filename=" +
                          shq(startPath) + zenityFilters(entries);
        res = runCapture(cmd + " 2>/dev/null");
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
    if (haveCmd("kdialog")) {
        std::string cmd = "kdialog --getopenfilename " + shq(startDir());
        std::string f = kdialogFilter(entries);
        if (!f.empty()) cmd += " " + shq(f);
        res = runCapture(cmd + " 2>/dev/null");
    } else if (haveCmd("zenity")) {
        std::string cmd = "zenity --file-selection --filename=" + shq(startDir() + "/") +
                          zenityFilters(entries);
        res = runCapture(cmd + " 2>/dev/null");
    } else {
        return {};
    }
    if (res.empty()) return {};
    rememberDir(res);
    return res;
}

std::string openFolderDialog() {
    std::string res;
    if (haveCmd("kdialog")) {
        res = runCapture("kdialog --getexistingdirectory " + shq(startDir()) + " 2>/dev/null");
    } else if (haveCmd("zenity")) {
        res = runCapture("zenity --file-selection --directory --filename=" +
                         shq(startDir() + "/") + " 2>/dev/null");
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
        int rc = std::system(("kdialog --title 'Import Mesh' --warningyesnocancel " +
                              shq(msg) + " >/dev/null 2>&1")
                                 .c_str());
        int code = (rc == -1) ? -1 : (rc >> 8);  // WEXITSTATUS
        if (code == 0) return 1;
        if (code == 1) return 0;
        return -1;
    }
    if (haveCmd("zenity")) {
        // zenity --question has no cancel; exit 0=yes, else no.
        int rc = std::system(("zenity --question --title='Import Mesh' --text=" + shq(msg) +
                              " >/dev/null 2>&1")
                                 .c_str());
        return (rc != -1 && (rc >> 8) == 0) ? 1 : 0;
    }
    return 1;  // no dialog tool: default to solid (the more useful import)
}

void afterFileWritten(const std::string& /*path*/) {
    // Desktop: the file is already on the user's disk.
}

void revealInFileManager(const std::string& path) {
    if (path.empty()) return;
#if defined(__APPLE__)
    // open -R reveals + selects the file in Finder.
    (void)std::system(("open -R " + shq(path) + " >/dev/null 2>&1 &").c_str());
#else
    // freedesktop FileManager1 D-Bus API selects the item (Nautilus/Dolphin/Nemo/…);
    // fall back to xdg-open on the containing folder when dbus-send isn't available.
    const size_t slash = path.find_last_of('/');
    const std::string dir = (slash == std::string::npos) ? std::string(".") : path.substr(0, slash);
    const std::string cmd =
        "dbus-send --session --print-reply --dest=org.freedesktop.FileManager1 "
        "--type=method_call /org/freedesktop/FileManager1 "
        "org.freedesktop.FileManager1.ShowItems array:string:" +
        shq("file://" + path) + " string:\"\" >/dev/null 2>&1 || xdg-open " + shq(dir) +
        " >/dev/null 2>&1 &";
    (void)std::system(cmd.c_str());
#endif
}

}  // namespace ui

#endif  // platform
