// Native Win32 file dialogs — implementation. See file_dialog.h for the contract.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
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
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
    if (n <= 0) return {};
    std::wstring w(n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), &w[0], n);
    return w;
}

static std::string narrow(const wchar_t* w) {
    if (!w || !*w) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, nullptr, 0, nullptr, nullptr);
    if (n <= 1) return {};
    std::string s(n - 1, '\0');  // n includes the terminating NUL
    WideCharToMultiByte(CP_UTF8, 0, w, -1, &s[0], n, nullptr, nullptr);
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

}  // namespace ui
