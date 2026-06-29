#include "platform/file_dialog.h"

#if defined(_WIN32)
#  define WIN32_LEAN_AND_MEAN
#  include <windows.h>
#  include <commdlg.h>
#  include <vector>

namespace {
std::string narrow(const wchar_t* w) {
    if (!w || !*w) return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, nullptr, 0, nullptr, nullptr);
    std::string s(n > 0 ? n-1 : 0, '\0');
    if (n > 1) WideCharToMultiByte(CP_UTF8, 0, w, -1, s.data(), n, nullptr, nullptr);
    return s;
}
std::wstring widen(const std::string& s) {
    if (s.empty()) return {};
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, nullptr, 0);
    std::wstring w(n > 0 ? n-1 : 0, L'\0');
    if (n > 1) MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, w.data(), n);
    return w;
}
// Double-null-terminated filter for CAD formats.
const wchar_t* kFilter =
    L"AI-CAD / CAD files\0*.step+;*.step;*.stp;*.iges;*.igs;*.brep;*.stl;*.3mf;*.obj\0"
    L"AI-CAD native (.step+)\0*.step+\0"
    L"All files\0*.*\0\0";
} // namespace

namespace platform {

std::string openCadFile() {
    wchar_t buf[2048] = {0};
    OPENFILENAMEW ofn{}; ofn.lStructSize = sizeof(ofn);
    ofn.hwndOwner = GetActiveWindow();
    ofn.lpstrFilter = kFilter;
    ofn.lpstrFile = buf; ofn.nMaxFile = 2048;
    ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR | OFN_EXPLORER;
    ofn.lpstrTitle = L"Open";
    if (GetOpenFileNameW(&ofn)) return narrow(buf);
    return {};
}

std::string saveCadFile(const std::string& defaultName) {
    wchar_t buf[2048] = {0};
    std::wstring dn = widen(defaultName.empty() ? "Part1.step+" : defaultName);
    wcsncpy_s(buf, dn.c_str(), _TRUNCATE);
    OPENFILENAMEW ofn{}; ofn.lStructSize = sizeof(ofn);
    ofn.hwndOwner = GetActiveWindow();
    ofn.lpstrFilter = kFilter;
    ofn.lpstrFile = buf; ofn.nMaxFile = 2048;
    ofn.Flags = OFN_PATHMUSTEXIST | OFN_OVERWRITEPROMPT | OFN_NOCHANGEDIR | OFN_EXPLORER;
    ofn.lpstrTitle = L"Save As";
    ofn.lpstrDefExt = L"step+";
    if (GetSaveFileNameW(&ofn)) return narrow(buf);
    return {};
}

} // namespace platform

#else
namespace platform {
std::string openCadFile() { return {}; }
std::string saveCadFile(const std::string&) { return {}; }
}
#endif
