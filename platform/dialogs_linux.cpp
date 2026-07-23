#include "platform/dialogs.h"

#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

namespace platform {
namespace {

std::string s_lastDir;

bool dialogsDisabled() { return std::getenv("CHUDWARE_NO_FILE_DIALOGS") != nullptr; }

bool haveCmd(const char* c) {
    return std::system((std::string("command -v ") + c + " >/dev/null 2>&1").c_str()) == 0;
}

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

std::string startDir() { return s_lastDir.empty() ? homeDir() : s_lastDir; }

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

struct FilterEntry {
    std::string desc;
    std::string patterns;
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
    if (dialogsDisabled()) return {};
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
    if (dialogsDisabled()) return {};
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

int promptImportMeshAsSolid() {
    if (dialogsDisabled()) return 1;
    const char* msg =
        "Import this mesh as a solid body?\n\n"
        "Yes  —  convert it into a solid (editable B-rep: cut, fillet, combine)\n"
        "No   —  keep it as a graphics mesh (you can convert it later: "
        "right-click the import in the tree)";
    if (haveCmd("kdialog")) {
        int rc = std::system(("kdialog --title 'Import Mesh' --warningyesnocancel " +
                              shq(msg) + " >/dev/null 2>&1")
                                 .c_str());
        int code = (rc == -1) ? -1 : (rc >> 8);
        if (code == 0) return 1;
        if (code == 1) return 0;
        return -1;
    }
    if (haveCmd("zenity")) {
        int rc = std::system(("zenity --question --title='Import Mesh' --text=" + shq(msg) +
                              " >/dev/null 2>&1")
                                 .c_str());
        return (rc != -1 && (rc >> 8) == 0) ? 1 : 0;
    }
    return 1;
}

}  // namespace platform
