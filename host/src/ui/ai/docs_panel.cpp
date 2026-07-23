// See docs_panel.h. Two-pane glass layout: session document list (agent artifacts + user
// attachments) on the left, extension-keyed preview/edit on the right.
#include "docs_panel.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <sstream>
#include <string>
#include <system_error>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"   // ImRect

#include <nlohmann/json.hpp>

#include "core/app_state.h"
#include "ui/file_dialog.h"   // ui::openFileDialog (native open dialog)
#include "ui_theme.h"         // ui::palette / uiScale / adaptivePanel / glassPanel / glassRowBackground

namespace ui {
namespace {

constexpr size_t kMaxEditBytes = 1024 * 1024;   // above this a text file is read-only preview
constexpr size_t kCsvMaxRows   = 1000;
constexpr size_t kCsvMaxCols   = 64;

// Native-dialog filter (Win32 NUL-pair form; file_dialog.cpp maps it to kdialog/zenity).
const char kDocsFilter[] =
    "Documents\0*.txt;*.md;*.csv;*.tsv;*.png;*.jpg;*.jpeg;*.pdf;*.pptx;*.xlsx;*.xlsm;*.doc;"
    "*.docx;*.py;*.json;*.yaml;*.yml;*.log\0"
    "All files (*.*)\0*.*\0";

enum class DocKind { Text, Csv, Image, Office, Other };

struct DocEntry {
    std::string path;
    std::string kind;      // artifact kind ("file"/"image"/...) or "attached"
    std::string tool;      // producing tool/stage for artifacts (tooltip only)
    bool        attached = false;
};

struct DocsState {
    // Per-session user attachments, keyed by conversation id ("" -> "default").
    std::unordered_map<std::string, std::vector<std::string>> attachedByConv;
    std::string selected;

    // Text edit buffer (loaded lazily when `selected` changes).
    std::string textPath;
    std::string buf;
    bool        dirty = false, editMode = false, truncated = false;
    std::string loadErr;

    // office_extract request/response for the selected office/pdf path.
    std::string    officePath;
    bool           officePending = false;
    bool           officeAvail = true;   // false once the RPC reports the method is absent
    std::string    officeErr;
    nlohmann::json officeResult;

    // Image texture cache keyed by path (session-scale; same precedent as copilot's s_artTex).
    std::unordered_map<std::string, Tex> tex;

    char pathBuf[512] = {};
};

DocsState& DS() {
    static DocsState s;
    return s;
}

// ---------------------------------------------------------------- helpers

std::string baseName(const std::string& p) {
    const size_t s = p.find_last_of("/\\");
    return s == std::string::npos ? p : p.substr(s + 1);
}

std::string lowerExt(const std::string& p) {
    const size_t dot = p.find_last_of('.');
    const size_t slash = p.find_last_of("/\\");
    if (dot == std::string::npos || (slash != std::string::npos && dot < slash)) return "";
    std::string e = p.substr(dot);
    for (char& c : e) c = (char)std::tolower((unsigned char)c);
    return e;
}

bool extIn(const std::string& ext, const char* const* list, size_t n) {
    for (size_t i = 0; i < n; ++i) if (ext == list[i]) return true;
    return false;
}

DocKind kindForExt(const std::string& ext) {
    static const char* const kImage[]  = {".png", ".jpg", ".jpeg"};
    static const char* const kOffice[] = {".pptx", ".xlsx", ".xlsm", ".pdf", ".doc", ".docx",
                                          ".ppt", ".xls"};
    static const char* const kCsv[]    = {".csv", ".tsv"};
    static const char* const kText[]   = {".txt", ".md", ".markdown", ".log", ".json",
                                          ".yaml", ".yml", ".toml", ".xml", ".ini", ".cfg",
                                          ".py", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh",
                                          ".hpp", ".js", ".ts", ".css", ".html", ".svg",
                                          ".sh", ".step", ".stp"};
    if (extIn(ext, kImage,  std::size(kImage)))  return DocKind::Image;
    if (extIn(ext, kOffice, std::size(kOffice))) return DocKind::Office;
    if (extIn(ext, kCsv,    std::size(kCsv)))    return DocKind::Csv;
    if (extIn(ext, kText,   std::size(kText)))   return DocKind::Text;
    return DocKind::Other;
}

std::string fmtSize(unsigned long long n) {
    char b[48];
    if (n < 1024ull)                        std::snprintf(b, sizeof(b), "%llu B", n);
    else if (n < 1024ull * 1024ull)         std::snprintf(b, sizeof(b), "%.1f KB", n / 1024.0);
    else                                    std::snprintf(b, sizeof(b), "%.1f MB", n / (1024.0 * 1024.0));
    return b;
}

// Same headless guard as feature_tree.cpp's fileDialogsDisabled().
bool dialogsDisabled() {
    static const bool disabled = std::getenv("CHUDWARE_NO_FILE_DIALOGS") != nullptr;
    return disabled;
}

void emptyState(const char* msg, const char* sub) {
    ImGui::Dummy(ImVec2(0, ImGui::GetContentRegionAvail().y * 0.28f));
    const float tw = ImGui::CalcTextSize(msg).x;
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                         std::max(0.0f, (ImGui::GetContentRegionAvail().x - tw) * 0.5f));
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDisabled[0], palette.textDisabled[1], palette.textDisabled[2], 0.55f));
    ImGui::TextUnformatted(msg);
    ImGui::PopStyleColor();
    if (sub) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 0.75f));
        ImGui::TextWrapped("%s", sub);
        ImGui::PopStyleColor();
    }
}

// Clip a label to fit `maxW`, appending "..." when it must be shortened.
std::string ellipsize(const std::string& s, float maxW) {
    if (ImGui::CalcTextSize(s.c_str()).x <= maxW) return s;
    std::string out = s;
    while (out.size() > 4 &&
           ImGui::CalcTextSize((out + "...").c_str()).x > maxW)
        out.pop_back();
    return out + "...";
}

// ---------------------------------------------------------------- artifact collection

// Agent-created files packed into activity detail after "\x1eART\x1e" (live + restored) —
// the same blob conversations.cpp persists as {kind,path,tool} and panel.cpp's Artifacts
// panel lists. Active conversation messages live in st.chat.
void collectArtifacts(const AppState& st, std::vector<DocEntry>& out,
                      std::unordered_set<std::string>& seen) {
    for (const auto& m : st.chat) {
        if (m.fromUser) continue;
        for (const auto& a : m.activity) {
            const size_t sep = a.detail.find("\x1e" "ART\x1e");
            if (sep == std::string::npos) continue;
            std::string blob = a.detail.substr(sep);
            const size_t nl = blob.find('\n');
            if (nl != std::string::npos) blob = blob.substr(nl + 1);
            std::istringstream iss(blob);
            std::string line;
            while (std::getline(iss, line)) {
                if (line.empty()) continue;
                const size_t tab = line.find('\t');
                DocEntry e;
                e.kind = tab == std::string::npos ? "file" : line.substr(0, tab);
                e.path = tab == std::string::npos ? line : line.substr(tab + 1);
                e.tool = a.title.empty() ? a.stage : a.title;
                if (e.path.empty() || !seen.insert(e.path).second) continue;
                out.push_back(std::move(e));
            }
        }
    }
}

std::vector<std::string>& attachedFor(const AppState& st) {
    std::string key = "default";
    if (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size() &&
        !st.conversations[st.activeConv].id.empty())
        key = st.conversations[st.activeConv].id;
    return DS().attachedByConv[key];
}

void attachPath(DocsState& D, std::vector<std::string>& attached, const std::string& p) {
    if (p.empty()) return;
    if (std::find(attached.begin(), attached.end(), p) == attached.end())
        attached.push_back(p);
    D.selected = p;
}

// ---------------------------------------------------------------- text load / save

void ensureTextLoaded(DocsState& D, const std::string& path) {
    if (D.textPath == path) return;
    D.textPath  = path;
    D.dirty     = false;
    D.editMode  = false;
    D.truncated = false;
    D.loadErr.clear();
    D.buf.clear();
    std::ifstream in(path, std::ios::binary);
    if (!in) { D.loadErr = "could not open the file"; return; }
    D.buf.resize(kMaxEditBytes + 1);
    in.read(D.buf.data(), (std::streamsize)(kMaxEditBytes + 1));
    D.buf.resize((size_t)in.gcount());
    if (D.buf.size() > kMaxEditBytes) {
        D.buf.resize(kMaxEditBytes);
        D.truncated = true;
    }
    // Headroom so the multiline editor can grow the text beyond the loaded size.
    D.buf.reserve(D.buf.size() + 8192);
}

void reloadText(DocsState& D) {
    const std::string p = D.textPath;
    D.textPath.clear();
    ensureTextLoaded(D, p);
}

void saveText(AppState& st, DocsState& D) {
    const size_t len = std::strlen(D.buf.c_str());
    try {
        const std::string tmp = D.textPath + ".tmp";
        {
            std::ofstream out(tmp, std::ios::binary | std::ios::trunc);
            if (!out) throw 1;
            out.write(D.buf.data(), (std::streamsize)len);
            if (!out) throw 1;
        }
        std::filesystem::rename(tmp, D.textPath);
        D.buf.resize(len);
        D.buf.reserve(len + 8192);
        D.dirty = false;
        st.toast(ToastLevel::Success, "Saved " + baseName(D.textPath));
    } catch (...) {
        st.toast(ToastLevel::Error, "Save failed: " + D.textPath);
    }
}

// ---------------------------------------------------------------- CSV

using CsvRows = std::vector<std::vector<std::string>>;

// Minimal RFC-4180-ish split (quoted fields, embedded commas/quotes/newlines honored).
CsvRows parseCsv(const char* text, char delim) {
    CsvRows rows;
    std::vector<std::string> row;
    std::string cell;
    bool inQ = false;
    const size_t n = std::strlen(text);
    for (size_t i = 0; i <= n; ++i) {
        const char c = i < n ? text[i] : '\n';
        if (inQ) {
            if (c == '"') {
                if (i + 1 < n && text[i + 1] == '"') { cell += '"'; ++i; }
                else inQ = false;
            } else if (i < n) {
                cell += c;
            }
            continue;
        }
        if (c == '"') inQ = true;
        else if (c == delim) { row.push_back(cell); cell.clear(); }
        else if (c == '\n' || c == '\r') {
            if (c == '\r' && i + 1 < n && text[i + 1] == '\n') ++i;
            row.push_back(cell);
            cell.clear();
            if (row.size() > 1 || !row.front().empty()) rows.push_back(row);
            row.clear();
            if (rows.size() >= kCsvMaxRows) break;
        } else {
            cell += c;
        }
    }
    for (auto& r : rows) if (r.size() > kCsvMaxCols) r.resize(kCsvMaxCols);
    return rows;
}

void drawRowsTable(const char* id, const CsvRows& rows, float maxH) {
    if (rows.empty() || rows.front().empty()) return;
    const int ncols = (int)rows.front().size();
    if (ImGui::BeginTable(id, ncols,
                          ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg |
                          ImGuiTableFlags_SizingStretchProp | ImGuiTableFlags_ScrollY,
                          ImVec2(0, maxH))) {
        ImGui::TableSetupScrollFreeze(0, 1);
        for (int c = 0; c < ncols; ++c)
            ImGui::TableSetupColumn(rows.front()[c].empty() ? " " : rows.front()[c].c_str());
        ImGui::TableHeadersRow();
        for (size_t r = 1; r < rows.size(); ++r) {
            ImGui::TableNextRow();
            for (int c = 0; c < ncols; ++c) {
                ImGui::TableSetColumnIndex(c);
                ImGui::TextUnformatted(c < (int)rows[r].size() ? rows[r][c].c_str() : "");
            }
        }
        ImGui::EndTable();
    }
}

// ---------------------------------------------------------------- office_extract

// True when the RPC is permanently absent (unknown method / older worker) — mirrors
// isAbsent in plm_overview.cpp.
bool officeAbsent(const core::RpcResult& r) {
    if (!r.ok) {
        const std::string& e = r.err;
        return e.find("unknown") != std::string::npos ||
               e.find("not found") != std::string::npos ||
               e.find("not_found") != std::string::npos ||
               e.find("method") != std::string::npos;
    }
    if (!r.result.is_object()) return false;
    if (r.result.contains("code")) {
        const std::string c = r.result.value("code", std::string());
        if (c.find("unknown") != std::string::npos || c == "method_not_found" ||
            c == "not_implemented")
            return true;
    }
    if (!r.result.value("ok", true)) {
        const std::string reason = r.result.value("reason", std::string());
        if (reason == "unknown_method" || reason == "method_not_found" ||
            reason == "not_implemented")
            return true;
    }
    return false;
}

void requestOfficeExtract(AppState& st, DocsState& D, const std::string& path) {
    D.officePath    = path;
    D.officePending = true;
    D.officeErr.clear();
    D.officeResult  = nullptr;
    st.rpc.call("office_extract", {{"path", path}}, [path](const core::RpcResult& r) {
        DocsState& D2 = DS();
        D2.officePending = false;
        if (D2.officePath != path) return;  // user switched selection mid-flight
        if (officeAbsent(r)) { D2.officeAvail = false; return; }
        if (!r.ok) {
            D2.officeErr = r.err.empty() ? "the service did not respond" : r.err;
            return;
        }
        if (r.result.is_object() && !r.result.value("ok", true)) {
            D2.officeErr = r.result.value("reason", std::string());
            if (D2.officeErr.empty()) D2.officeErr = "extraction failed";
            return;
        }
        D2.officeResult = r.result.is_object() ? r.result : nlohmann::json::object();
    });
}

void drawOfficeResult(AppState& st, const nlohmann::json& res,
                      const AdaptiveSurface& surf, float S) {
    const std::string kind = res.value("kind", std::string());
    if (!kind.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::Text("%s (office_extract)", kind.c_str());
        ImGui::PopStyleColor();
        ImGui::Spacing();
    }
    const float availH = ImGui::GetContentRegionAvail().y;
    bool any = false;
    const std::string text = res.value("text", std::string());
    if (!text.empty()) {
        any = true;
        drawCodeBox("##docs_office_txt", text, st.fonts.mono, availH * 0.55f);
        ImGui::Spacing();
    }
    if (res.contains("sheets") && res["sheets"].is_array()) {
        int si = 0;
        for (const auto& sh : res["sheets"]) {
            ImGui::PushID(si);
            std::string name = sh.is_object() ? sh.value("name", std::string()) : std::string();
            if (name.empty()) name = "Sheet " + std::to_string(si + 1);
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
            ImGui::TextUnformatted(name.c_str());
            ImGui::PopStyleColor();
            if (sh.is_object() && sh.contains("rows") && sh["rows"].is_array()) {
                CsvRows rows;
                for (const auto& r : sh["rows"]) {
                    if (!r.is_array()) continue;
                    std::vector<std::string> row;
                    for (const auto& c : r)
                        row.push_back(c.is_string() ? c.get<std::string>() : c.dump());
                    if (!row.empty()) rows.push_back(std::move(row));
                    if (rows.size() >= kCsvMaxRows) break;
                }
                drawRowsTable("##docs_sheet", rows, 260.0f * S);
            } else if (sh.is_object() && sh.value("text", std::string()).size() > 0) {
                drawCodeBox("##docs_sheet_txt", sh["text"].get<std::string>(),
                            st.fonts.mono, 240.0f * S);
            } else if (sh.is_string() && !sh.get<std::string>().empty()) {
                drawCodeBox("##docs_sheet_txt", sh.get<std::string>(), st.fonts.mono,
                            240.0f * S);
            }
            ImGui::Spacing();
            ImGui::PopID();
            ++si;
        }
        any = true;
    }
    if (!any) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextWrapped("The extractor returned no text for this file.");
        ImGui::PopStyleColor();
    }
}

// ---------------------------------------------------------------- preview pane

void drawPreviewHeader(const std::string& path, const char* kindLabel,
                       const AdaptiveSurface& surf, float S) {
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::TextUnformatted(baseName(path).c_str());
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", path.c_str());
    std::error_code ec;
    const auto sz = std::filesystem::file_size(path, ec);
    ImGui::SameLine(0, 8.0f * S);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
    ImGui::Text("%s%s%s", kindLabel, ec ? "" : "  -  ", ec ? "" : fmtSize(sz).c_str());
    ImGui::PopStyleColor();
    ImGui::Spacing();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 a(ImGui::GetWindowPos().x + 6.0f * S, ImGui::GetCursorScreenPos().y);
    const ImVec2 b(ImGui::GetWindowPos().x + ImGui::GetWindowSize().x - 6.0f * S, a.y);
    dl->AddLine(a, b, Palette::toU32(palette.glassBorder), 1.0f);
    ImGui::Spacing();
}

void drawTextPreview(AppState& st, DocsState& D, const std::string& path, bool isCsv,
                     const AdaptiveSurface& surf, float S) {
    ensureTextLoaded(D, path);
    if (!D.loadErr.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusErr[0], palette.statusErr[1], palette.statusErr[2], 1.0f));
        ImGui::TextWrapped("%s: %s", D.loadErr.c_str(), path.c_str());
        ImGui::PopStyleColor();
        return;
    }
    // Toolbar: Edit/Preview toggle + Save/Revert (read-only when the file is too large).
    if (D.truncated) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextWrapped("Large file - read-only preview of the first %.0f KB.",
                           kMaxEditBytes / 1024.0);
        ImGui::PopStyleColor();
    } else {
        if (ImGui::SmallButton(D.editMode ? "Preview" : "Edit")) {
            if (D.editMode) {
                // Leaving edit mode: drop stale bytes past the edited length.
                D.buf.resize(std::strlen(D.buf.c_str()));
                D.buf.reserve(D.buf.size() + 8192);
            }
            D.editMode = !D.editMode;
        }
        if (D.editMode) {
            ImGui::SameLine();
            ImGui::BeginDisabled(!D.dirty);
            if (ImGui::SmallButton("Save")) saveText(st, D);
            ImGui::EndDisabled();
            ImGui::SameLine();
            if (ImGui::SmallButton("Revert")) reloadText(D);
        }
    }
    ImGui::Spacing();
    const float bodyH = ImGui::GetContentRegionAvail().y;
    if (D.editMode && !D.truncated) {
        if (st.fonts.mono) ImGui::PushFont(st.fonts.mono);
        if (ImGui::InputTextMultiline("##docs_edit", D.buf.data(), D.buf.capacity(),
                                      ImVec2(-1.0f, bodyH)))
            D.dirty = true;
        if (st.fonts.mono) ImGui::PopFont();
    } else if (isCsv) {
        const char delim = lowerExt(path) == ".tsv" ? '\t' : ',';
        CsvRows rows = parseCsv(D.buf.c_str(), delim);
        if (rows.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextWrapped("(empty file)");
            ImGui::PopStyleColor();
        } else {
            drawRowsTable("##docs_csv", rows, bodyH);
        }
    } else {
        drawCodeBox("##docs_code", D.buf, st.fonts.mono, bodyH);
    }
}

void drawImagePreview(DocsState& D, const std::string& path,
                      const AdaptiveSurface& surf, float S) {
    auto it = D.tex.find(path);
    if (it == D.tex.end()) it = D.tex.emplace(path, loadTexture(path)).first;
    const Tex& t = it->second;
    if (!t.id || t.w <= 0 || t.h <= 0) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusWarn[0], palette.statusWarn[1], palette.statusWarn[2], 1.0f));
        ImGui::TextWrapped("Could not decode this image.");
        ImGui::PopStyleColor();
        return;
    }
    const float capH = ImGui::GetTextLineHeightWithSpacing() + 6.0f * S;
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    const float sc = std::min(avail.x / (float)t.w,
                              std::max(1.0f, avail.y - capH) / (float)t.h);
    const float tw = (float)t.w * sc, th = (float)t.h * sc;
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() + std::max(0.0f, (avail.x - tw) * 0.5f));
    ImGui::Image((ImTextureID)(intptr_t)t.id, ImVec2(tw, th));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
    ImGui::Text("%d x %d px", t.w, t.h);
    ImGui::PopStyleColor();
}

void drawOfficePreview(AppState& st, DocsState& D, const std::string& path,
                       const AdaptiveSurface& surf, float S) {
    if (!D.officeAvail) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusWarn[0], palette.statusWarn[1], palette.statusWarn[2], 1.0f));
        ImGui::TextWrapped(
            "Office/PDF text extraction is not available in this worker (office_extract "
            "missing) - preview only here. The file stays listed so the agent can open it "
            "directly.");
        ImGui::PopStyleColor();
        if (ImGui::SmallButton("Retry")) {
            D.officeAvail = true;
            D.officeErr.clear();
            D.officePath.clear();
        }
        return;
    }
    if (D.officePath != path && !D.officePending)
        requestOfficeExtract(st, D, path);
    if (D.officePending || D.officePath != path) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextWrapped("Extracting text (office_extract)...");
        ImGui::PopStyleColor();
        return;
    }
    if (!D.officeErr.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusErr[0], palette.statusErr[1], palette.statusErr[2], 1.0f));
        ImGui::TextWrapped("Extraction failed: %s", D.officeErr.c_str());
        ImGui::PopStyleColor();
        if (ImGui::SmallButton("Retry")) D.officePath.clear();
        return;
    }
    drawOfficeResult(st, D.officeResult, surf, S);
}

void drawPreview(AppState& st, DocsState& D, const AdaptiveSurface& surf, float S) {
    if (D.selected.empty()) {
        emptyState("Select a document",
                   "Pick a file on the left, or attach one with the button or path entry.");
        return;
    }
    const std::string& path = D.selected;
    std::error_code ec;
    if (!std::filesystem::exists(path, ec)) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        ImGui::TextUnformatted(baseName(path).c_str());
        ImGui::PopStyleColor();
        ImGui::Spacing();
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusWarn[0], palette.statusWarn[1], palette.statusWarn[2], 1.0f));
        ImGui::TextWrapped("This file no longer exists on disk:\n%s", path.c_str());
        ImGui::PopStyleColor();
        return;
    }
    const std::string ext = lowerExt(path);
    const DocKind kind = kindForExt(ext);
    const char* kindLabel =
        kind == DocKind::Text ? "text" :
        kind == DocKind::Csv  ? "csv"  :
        kind == DocKind::Image ? "image" :
        kind == DocKind::Office ? "office/pdf" : ext.c_str();
    drawPreviewHeader(path, kindLabel, surf, S);
    switch (kind) {
        case DocKind::Text:  drawTextPreview(st, D, path, false, surf, S); break;
        case DocKind::Csv:   drawTextPreview(st, D, path, true,  surf, S); break;
        case DocKind::Image: drawImagePreview(D, path, surf, S); break;
        case DocKind::Office: {
            drawOfficePreview(st, D, path, surf, S);
            ImGui::Spacing();
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextWrapped("Office and PDF files are preview-only here.");
            ImGui::PopStyleColor();
            break;
        }
        case DocKind::Other:
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextWrapped("No inline preview for '%s' files.",
                               ext.empty() ? "(no extension)" : ext.c_str());
            ImGui::PopStyleColor();
            break;
    }
}

// ---------------------------------------------------------------- list pane

// One clickable glass row: basename + right-aligned kind tag. Returns true on click;
// `removeClicked` is set when the trailing (x) of an attached row is hit.
bool docRow(const DocEntry& e, bool selected, const AdaptiveSurface& surf, float S,
            bool& removeClicked) {
    removeClicked = false;
    const float avail  = ImGui::GetContentRegionAvail().x;
    const float rowH   = ImGui::GetTextLineHeightWithSpacing() + 8.0f * S;
    const float rowW   = avail - (e.attached ? 40.0f * S : 0.0f);
    ImGui::PushID(e.path.c_str());
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::InvisibleButton("##hit", ImVec2(rowW, rowH));
    const bool clicked = ImGui::IsItemClicked();
    const bool hovered = ImGui::IsItemHovered();
    if (hovered) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ui::glassRowBackground(dl, p0, ImVec2(p0.x + rowW, p0.y + rowH), 4.0f, false,
                           selected, selected ? 0.5f : 0.30f);
    std::error_code ec;
    const bool missing = !std::filesystem::exists(e.path, ec);
    const ImVec4 txt = missing    ? surf.textTertiary
                     : selected   ? surf.textPrimary
                                  : surf.textSecondary;
    const ImVec2 tagSz = ImGui::CalcTextSize(e.kind.c_str());
    const float nameMaxW = rowW - tagSz.x - 22.0f * S;
    const std::string name = ellipsize(baseName(e.path), std::max(24.0f, nameMaxW));
    dl->AddText(ImVec2(p0.x + 8.0f * S, p0.y + 4.0f * S),
                ImGui::ColorConvertFloat4ToU32(txt), name.c_str());
    dl->AddText(ImVec2(p0.x + rowW - tagSz.x - 8.0f * S, p0.y + 4.0f * S),
                ImGui::ColorConvertFloat4ToU32(surf.textTertiary), e.kind.c_str());
    if (hovered) {
        ImGui::BeginTooltip();
        ImGui::PushTextWrapPos(ImGui::GetFontSize() * 30.0f);
        ImGui::TextUnformatted(e.path.c_str());
        if (!e.tool.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::Text("from: %s", e.tool.c_str());
            ImGui::PopStyleColor();
        }
        if (missing) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.statusWarn[0], palette.statusWarn[1], palette.statusWarn[2], 1.0f));
            ImGui::TextUnformatted("not found on disk");
            ImGui::PopStyleColor();
        }
        ImGui::PopTextWrapPos();
        ImGui::EndTooltip();
    }
    if (e.attached) {
        ImGui::SameLine(0, 4.0f * S);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        removeClicked = ImGui::SmallButton("(x)");
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Remove from this session's list");
    }
    ImGui::PopID();
    return clicked;
}

void drawDocList(AppState& st, DocsState& D, const AdaptiveSurface& surf, float S) {
    std::vector<std::string>& attached = attachedFor(st);

    // ---- attach affordances ----
    if (!dialogsDisabled()) {
        if (ImGui::Button("Attach file...", ImVec2(-1.0f, 0.0f))) {
            const std::string p = ui::openFileDialog(kDocsFilter);
            if (!p.empty()) attachPath(D, attached, p);
        }
    }
    {
        const float addW = 40.0f * S;
        ImGui::SetNextItemWidth(std::max(60.0f * S, ImGui::GetContentRegionAvail().x -
                                                  addW - 4.0f * S));
        const bool entered = ImGui::InputTextWithHint("##docs_path", "path/to/file",
                                                      D.pathBuf, sizeof(D.pathBuf),
                                                      ImGuiInputTextFlags_EnterReturnsTrue);
        ImGui::SameLine(0, 4.0f * S);
        ImGui::BeginDisabled(D.pathBuf[0] == '\0');
        if (ImGui::SmallButton("Add") || (entered && D.pathBuf[0] != '\0')) {
            attachPath(D, attached, D.pathBuf);
            D.pathBuf[0] = '\0';
        }
        ImGui::EndDisabled();
    }
    ImGui::Spacing();

    // ---- entries: session artifacts first, then user attachments ----
    std::vector<DocEntry> entries;
    std::unordered_set<std::string> seen;
    collectArtifacts(st, entries, seen);
    size_t attachedFrom = entries.size();
    for (const auto& p : attached) {
        if (!seen.insert(p).second) continue;
        DocEntry e;
        e.path = p;
        e.kind = "attached";
        e.attached = true;
        entries.push_back(std::move(e));
    }
    if (entries.empty()) {
        emptyState("No documents yet",
                   "Files agents create in this session show up here. Attach a file to "
                   "preview or edit it next to the chat.");
        return;
    }
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::Text("Documents (%d)", (int)entries.size());
    ImGui::PopStyleColor();
    ImGui::Spacing();

    int removeIdx = -1;
    for (int i = 0; i < (int)entries.size(); ++i) {
        if (i == 0 && attachedFrom > 0) {
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextUnformatted("Session artifacts");
            ImGui::PopStyleColor();
        }
        if (i == (int)attachedFrom && attachedFrom < entries.size()) {
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextUnformatted("Attached");
            ImGui::PopStyleColor();
        }
        bool removeClicked = false;
        if (docRow(entries[i], D.selected == entries[i].path, surf, S, removeClicked))
            D.selected = entries[i].path;
        if (removeClicked) removeIdx = i;
    }
    if (removeIdx >= (int)attachedFrom && removeIdx >= 0) {
        const std::string p = entries[(size_t)removeIdx].path;
        attached.erase(std::remove(attached.begin(), attached.end(), p), attached.end());
        if (D.selected == p) D.selected.clear();
    }
}

}  // namespace

void drawDocsPanel(AppState& st) {
    const float S = ui::uiScale;
    DocsState& D = DS();

    // One adaptive glass surface for the whole pane (same recipe as drawActiveWorkView).
    const ImVec2 paneMin = ImGui::GetWindowPos();
    const ImRect paneRect(paneMin, ImVec2(paneMin.x + ImGui::GetWindowSize().x,
                                          paneMin.y + ImGui::GetWindowSize().y));
    const AdaptiveSurface& surf =
        adaptivePanel(panelId("ai_docs_panel_glass"), paneRect, GlassGroup::Global);

    const float availW = ImGui::GetContentRegionAvail().x;
    const float leftW  = std::clamp(availW * 0.32f, 190.0f * S, 340.0f * S);

    // ---- LEFT: session document list ----
    ImGui::BeginChild("##docs_left", ImVec2(leftW, 0.0f), ImGuiChildFlags_None);
    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const ImVec2 c0 = ImGui::GetWindowPos();
        const ImVec2 c1(c0.x + ImGui::GetWindowSize().x, c0.y + ImGui::GetWindowSize().y);
        glassPanel(dl, ImRect(c0, c1), GlassLevel::Thin, 8.0f, surf);
        drawDocList(st, D, surf, S);
    }
    ImGui::EndChild();

    ImGui::SameLine();

    // ---- RIGHT: preview / edit ----
    ImGui::BeginChild("##docs_right", ImVec2(0.0f, 0.0f), ImGuiChildFlags_None);
    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const ImVec2 c0 = ImGui::GetWindowPos();
        const ImVec2 c1(c0.x + ImGui::GetWindowSize().x, c0.y + ImGui::GetWindowSize().y);
        glassPanel(dl, ImRect(c0, c1), GlassLevel::Thin, 8.0f, surf);
        drawPreview(st, D, surf, S);
    }
    ImGui::EndChild();
}

}  // namespace ui
