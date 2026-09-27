// See docs_panel.h. Project-scoped conversation artifacts and agent-touched tabs on the
// left, with extension-keyed preview/edit of the selected visible document on the right.
#include "docs_panel.h"

#include <algorithm>
#include <cctype>
#include <chrono>
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
#include "ui/ai/active_work.h" // saved/unsaved agent-tab project ownership
#include "ui/copilot/copilot_internal.h" // convProjectDir / convProjectKey
#include "ui/file_dialog.h"   // ui::openFileDialog (native open dialog)
#include "ui_theme.h"         // ui::palette / uiScale / adaptivePanel / glassPanel / glassRowBackground
#include "ui/metrics.h"       // cpMenu* rhythm tokens (gutter / row pad / corner)
#include "gl_compat.h"        // glDeleteTextures for the bounded preview-texture cache
#include "ui/ai/ai_prefs.h"   // ui::chatShowRenders (chat display prefs)
#include "ui/widgets/kit.h"   // ui::kit::emptyState / elide — the shared painters

namespace ui {
namespace {

// glassPanel / glassRowBackground take a LOGICAL dp radius (they multiply by uiScale
// themselves), so hand them the shared cpMenu corner converted back to dp — one source of
// truth for the corner without double-scaling it.
inline float rowRoundDp() { return ui::metrics::cpMenuRowRound() / ui::uiScale; }

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
    std::string name;      // display label (basename, or a doc tab's displayTitle())
    std::string path;      // file on disk; empty for an agent part not written out yet
    std::string key;       // selection identity (path, or the unsaved-doc sentinel)
    std::string kind;      // artifact kind ("file"/"image"/"part") or "attached"
    std::string tool;      // producing tool/stage for artifacts (tooltip only)
    bool        attached = false;
    // True for anything the agent produced or touched (activity artifacts + agent doc tabs);
    // false only for the user's own attachments. Drives the two list sections.
    bool        agentTouched = false;
};

// Selection key for an agent part that has no file yet — never a real path, so the preview
// pane can tell it apart from a missing file instead of crying "no longer exists on disk".
const std::string kUnsavedKeyPrefix = "\x1f" "unsaved\x1f";

// One decoded preview/thumbnail image. `tried` remembers a failed decode so a broken path is
// not re-decoded every frame.
struct TexSlot {
    Tex    t;
    double lastUse = 0.0;
    bool   tried = false;
};

// Hard ceiling on resident preview textures. A long session that scrolls through many renders
// used to keep one GL texture per path forever; past this the least-recently-drawn is deleted.
constexpr size_t kMaxTexCache = 24;

// How long a cached filesystem-existence answer stays good, in seconds. Out-of-band deletion
// is rare, so one stat per path per second is plenty to keep the "not found on disk" warning
// honest without stat-ing every visible row every frame.
constexpr double kExistsTtl = 1.0;
// Guard against unbounded growth as conversations churn through artifact paths; the whole map
// is dropped when it gets silly, and the next frame simply re-stats what it draws.
constexpr size_t kMaxExistsCache = 512;

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

    // Image texture cache keyed by path, bounded by kMaxTexCache with LRU eviction.
    std::unordered_map<std::string, TexSlot> tex;

    // Low-frequency existence cache keyed by row path, mirroring the preview pane's approach:
    // the list stats a path at most once per kExistsTtl, so steady-state drawing costs no
    // syscall per visible row per frame while an out-of-band deletion still surfaces in ~1s.
    struct ExistsSlot {
        double checkedAt = 0.0;
        bool   exists    = false;
    };
    std::unordered_map<std::string, ExistsSlot> existsCache;

    // Artifact kind of the entry `selected` addresses, refreshed by the list each frame. Lets
    // the preview honour an artifact the agent tagged "image" whatever its extension is.
    std::string selectedKind;

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
    static const char* const kImage[]  = {".png", ".jpg", ".jpeg", ".bmp"};
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

// Domain wrapper over the shared painter: only the vertical push is this pane's own, so the
// state lands inside the list/preview body rather than at its top edge.
void emptyState(const char* msg, const char* sub) {
    ui::kit::pushDensity(ui::kit::Density::Cozy);
    ImGui::Dummy(ImVec2(0, ImGui::GetContentRegionAvail().y * 0.28f));
    ui::kit::emptyState(nullptr, msg, sub);
    ui::kit::popDensity();
}

// Clip a label to fit `maxW`. Forwards to ui::kit::elide, which cuts on a UTF-8 boundary —
// the local byte-wise pop_back could split a multi-byte codepoint into mojibake.
std::string ellipsize(const std::string& s, float maxW) {
    return ui::kit::elide(s.c_str(), maxW);
}

// A render, either because the agent tagged the artifact "image" or because the path says so.
bool isImageEntry(const DocEntry& e) {
    if (e.kind == "image") return true;
    return kindForExt(lowerExt(e.path)) == DocKind::Image;
}

// `seen` holds raw paths for file identity plus this prefixed form for display identity, so a
// doc tab whose file already arrived as an activity artifact is not listed twice.
std::string nameKey(const std::string& n) { return "\x1f" "name\x1f" + n; }

void releaseTex(Tex& t) {
    if (!t.id) return;
    GLuint id = (GLuint)t.id;
    glDeleteTextures(1, &id);
    t.id = 0;
    t.w = t.h = 0;
}

// Lazy GL upload (same loadTexture path the copilot timeline uses for artifact images) behind
// the bounded LRU. Returns the slot for `path`; its texture may be 0 when the decode failed.
const TexSlot& texFor(DocsState& D, const std::string& path) {
    auto it = D.tex.find(path);
    if (it == D.tex.end()) {
        TexSlot s;
        s.t = loadTexture(path);
        s.tried = true;
        it = D.tex.emplace(path, s).first;
    }
    it->second.lastUse = ImGui::GetTime();
    while (D.tex.size() > kMaxTexCache) {
        auto oldest = D.tex.end();
        for (auto i = D.tex.begin(); i != D.tex.end(); ++i) {
            if (i->first == path) continue;   // never evict the entry we just handed out
            if (oldest == D.tex.end() || i->second.lastUse < oldest->second.lastUse) oldest = i;
        }
        if (oldest == D.tex.end()) break;
        releaseTex(oldest->second.t);
        D.tex.erase(oldest);   // erase never invalidates `it` (only the erased iterator)
    }
    return it->second;
}

// Cached std::filesystem::exists for a list row. Same answer as a direct stat within kExistsTtl.
bool pathExists(DocsState& D, const std::string& path) {
    const double now = ImGui::GetTime();
    auto it = D.existsCache.find(path);
    if (it != D.existsCache.end() && now - it->second.checkedAt < kExistsTtl) return it->second.exists;
    if (it == D.existsCache.end()) {
        if (D.existsCache.size() >= kMaxExistsCache) D.existsCache.clear();
        it = D.existsCache.emplace(path, DocsState::ExistsSlot{}).first;
    }
    std::error_code ec;
    it->second.exists    = std::filesystem::exists(path, ec);
    it->second.checkedAt = now;
    return it->second.exists;
}

// ---------------------------------------------------------------- artifact collection

// Activity artifact blobs are attached to a conversation, not to an arbitrary selected tab.
// Include both the live chat and parked conversations belonging to this project.
void collectArtifacts(const AppState& st, const std::string& projectDir,
                      std::vector<DocEntry>& out,
                      std::unordered_set<std::string>& seen) {
    const std::string projectKey = convProjectKey(projectDir);
    if (projectKey.empty()) return;
    for (int i = 0; i < (int)st.conversations.size(); ++i) {
        const Conversation& conv = st.conversations[(size_t)i];
        if (convProjectKey(conv.projectDir) != projectKey) continue;
        const auto& msgs = i == st.activeConv ? st.chat : conv.msgs;
        for (const auto& m : msgs) {
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
                    if (e.path.empty() ||
                        !agentDocInProject(e.path, conv.projectDir, projectDir) ||
                        !seen.insert(e.path).second) continue;
                    e.name = baseName(e.path);
                    e.key  = e.path;
                    e.agentTouched = true;
                    seen.insert(nameKey(e.name));
                    out.push_back(std::move(e));
                }
            }
        }
    }
}

// The agent's DOCUMENTS, not just its files: a part it opened or edited belongs in this list
// even when no file was ever written for it. "Agent-touched" is the shared contract —
// aiHandoff, or a non-zero lastAgentEditAt.
void collectAgentDocs(const AppState& st, const std::string& projectDir,
                      std::vector<DocEntry>& out,
                      std::unordered_set<std::string>& seen) {
    for (const auto& t : st.docTabs) {
        if (!agentDocInProject(t.path, t.projectDir, projectDir)) continue;
        if (!t.aiHandoff && t.lastAgentEditAt == std::chrono::steady_clock::time_point{})
            continue;
        DocEntry e;
        e.name = t.displayTitle();   // never the raw doc id
        if (e.name.empty()) continue;
        e.path = t.path;
        e.kind = "part";
        e.agentTouched = true;
        if (!e.path.empty() && !seen.insert(e.path).second) continue;
        if (!seen.insert(nameKey(e.name)).second) continue;
        e.key = e.path.empty() ? kUnsavedKeyPrefix + e.name : e.path;
        out.push_back(std::move(e));
    }
}

std::vector<std::string>* attachedFor(const AppState& st,
                                      const std::string& projectDir) {
    if (st.activeConv < 0 || st.activeConv >= (int)st.conversations.size())
        return nullptr;
    const auto& conv = st.conversations[(size_t)st.activeConv];
    if (convProjectKey(conv.projectDir) != convProjectKey(projectDir) ||
        conv.projectDir.empty()) return nullptr;
    return &DS().attachedByConv[conv.id.empty() ? "default" : conv.id];
}

void attachPath(AppState& st, DocsState& D, std::vector<std::string>& attached,
                const std::string& p, const std::string& projectDir) {
    if (p.empty()) return;
    if (!agentDocInProject(p, std::string(), projectDir)) {
        st.toast(ToastLevel::Warn, "Choose a file inside the active project folder.");
        return;
    }
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

void drawImagePreview(AppState& st, DocsState& D, const std::string& path,
                      const AdaptiveSurface& surf, float S) {
    const TexSlot& slot = texFor(D, path);
    const Tex& t = slot.t;
    if (!t.id || t.w <= 0 || t.h <= 0) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextUnformatted("could not load image");
        ImGui::PopStyleColor();
        // Never a blank pane: show whatever text presentation the file itself supports.
        const DocKind fk = kindForExt(lowerExt(path));
        if (fk == DocKind::Text || fk == DocKind::Csv) {
            drawTextPreview(st, D, path, fk == DocKind::Csv, surf, S);
        } else {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.statusWarn[0], palette.statusWarn[1], palette.statusWarn[2], 1.0f));
            ImGui::TextWrapped("Nothing decodable at this path yet - a render still being "
                               "written, or not an image file:\n%s", path.c_str());
            ImGui::PopStyleColor();
        }
        return;
    }
    const float capH = ImGui::GetTextLineHeightWithSpacing() + 6.0f * S;
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    // Fit the pane on both axes, aspect ratio preserved (never upscale past 1:1).
    const float sc = std::min(1.0f, std::min(avail.x / (float)t.w,
                                             std::max(1.0f, avail.y - capH) / (float)t.h));
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
    // An agent part with no file yet: not a missing file, so say what it actually is.
    if (D.selected.rfind(kUnsavedKeyPrefix, 0) == 0) {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        ImGui::TextUnformatted(D.selected.substr(kUnsavedKeyPrefix.size()).c_str());
        ImGui::PopStyleColor();
        ImGui::Spacing();
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        ImGui::TextWrapped("The agent is working on this part in the session - nothing has been "
                           "written to disk for it yet. Save it from its document tab to read "
                           "it here.");
        ImGui::PopStyleColor();
        return;
    }
    const std::string& path = D.selected;
    // Out-of-band deletion is rare; stat once per selection and then only about once a
    // second, so a steady preview costs no syscall per frame.
    static std::string existsCachedPath;
    static double existsCheckedAt = 0.0;
    static bool existsCached = false;
    const double now = ImGui::GetTime();
    if (existsCachedPath != path || now - existsCheckedAt >= 1.0) {
        std::error_code ec;
        existsCached = std::filesystem::exists(path, ec);
        existsCachedPath = path;
        existsCheckedAt = now;
    }
    if (!existsCached) {
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
    DocKind kind = kindForExt(ext);
    // An artifact the agent tagged "image" is a render whatever its extension looks like.
    if (D.selectedKind == "image") kind = DocKind::Image;
    const char* kindLabel =
        kind == DocKind::Text ? "text" :
        kind == DocKind::Csv  ? "csv"  :
        kind == DocKind::Image ? "image" :
        kind == DocKind::Office ? "office/pdf" : ext.c_str();
    drawPreviewHeader(path, kindLabel, surf, S);
    if (kind == DocKind::Image) {
        if (chatShowRenders()) {
            drawImagePreview(st, D, path, surf, S);
        } else {
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextWrapped("Render previews are off (Settings > AI > show renders in chat).");
            ImGui::PopStyleColor();
        }
        return;
    }
    switch (kind) {
        case DocKind::Text:  drawTextPreview(st, D, path, false, surf, S); break;
        case DocKind::Csv:   drawTextPreview(st, D, path, true,  surf, S); break;
        case DocKind::Image: break;   // handled above
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
bool docRow(DocsState& D, const DocEntry& e, bool selected, const AdaptiveSurface& surf,
            float S, bool& removeClicked) {
    removeClicked = false;
    // Renders get a two-line-tall thumbnail chip so the list is scannable at a glance.
    const bool wantThumb = !e.path.empty() && isImageEntry(e) && chatShowRenders();
    const float lineH  = ImGui::GetTextLineHeightWithSpacing();
    const float avail  = ImGui::GetContentRegionAvail().x;
    const float rowH   = (wantThumb ? lineH * 2.0f : lineH) + ui::metrics::cpMenuRowPadY();
    const float rowW   = avail - (e.attached ? 40.0f * S : 0.0f);
    ImGui::PushID(e.key.c_str());
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::InvisibleButton("##hit", ImVec2(rowW, rowH));
    const bool clicked = ImGui::IsItemClicked();
    const bool hovered = ImGui::IsItemHovered();
    if (hovered) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ui::glassRowBackground(dl, p0, ImVec2(p0.x + rowW, p0.y + rowH), rowRoundDp(), false,
                           selected, selected ? 0.5f : 0.30f);
    const bool missing = !e.path.empty() && !pathExists(D, e.path);
    const ImVec4 txt = missing    ? surf.textTertiary
                     : selected   ? surf.textPrimary
                                  : surf.textSecondary;
    const ImVec2 tagSz = ImGui::CalcTextSize(e.kind.c_str());
    const float padX = ui::metrics::cpMenuRowPadX();
    const float textY = p0.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f;
    float nameX = p0.x + padX;
    if (wantThumb) {
        const TexSlot& slot = texFor(D, e.path);
        if (slot.t.id && slot.t.w > 0 && slot.t.h > 0) {
            const float chipH = lineH * 2.0f - 4.0f * S;
            const float chipW = std::min(chipH * (float)slot.t.w / (float)slot.t.h, chipH * 2.0f);
            const ImVec2 a(nameX, p0.y + (rowH - chipH) * 0.5f);
            dl->AddImageRounded((ImTextureID)(intptr_t)slot.t.id, a,
                                ImVec2(a.x + chipW, a.y + chipH), ImVec2(0, 0), ImVec2(1, 1),
                                IM_COL32_WHITE, ui::metrics::cpMenuRowRound() * 0.5f);
            nameX = a.x + chipW + padX;
        }
    }
    const float nameMaxW = (p0.x + rowW - tagSz.x - padX) - nameX - ui::metrics::cpMenuRowGap();
    const std::string label = ellipsize(e.name.empty() ? baseName(e.path) : e.name,
                                        std::max(24.0f, nameMaxW));
    dl->AddText(ImVec2(nameX, textY), ImGui::ColorConvertFloat4ToU32(txt), label.c_str());
    dl->AddText(ImVec2(p0.x + rowW - tagSz.x - padX, textY),
                ImGui::ColorConvertFloat4ToU32(surf.textTertiary), e.kind.c_str());
    if (hovered) {
        ImGui::BeginTooltip();
        ImGui::PushTextWrapPos(ImGui::GetFontSize() * 30.0f);
        ImGui::TextUnformatted(e.path.empty() ? e.name.c_str() : e.path.c_str());
        if (!e.tool.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::Text("from: %s", e.tool.c_str());
            ImGui::PopStyleColor();
        }
        if (e.path.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextUnformatted("not written to disk yet");
            ImGui::PopStyleColor();
        } else if (missing) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.statusWarn[0], palette.statusWarn[1], palette.statusWarn[2], 1.0f));
            ImGui::TextUnformatted("not found on disk");
            ImGui::PopStyleColor();
        }
        ImGui::PopTextWrapPos();
        ImGui::EndTooltip();
    }
    if (e.attached) {
        ImGui::SameLine(0, ui::metrics::cpMenuRowGap());
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
        removeClicked = ImGui::SmallButton("(x)");
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("Remove from this session's list");
    }
    ImGui::PopID();
    return clicked;
}

void drawDocList(AppState& st, DocsState& D, const AdaptiveSurface& surf, float S) {
    const std::string projectDir = convProjectDir(st);
    std::vector<std::string>* attached = attachedFor(st, projectDir);

    ImGui::BeginDisabled(attached == nullptr);
    // ---- attach affordances ----
    if (!dialogsDisabled()) {
        if (ImGui::Button("Attach file...", ImVec2(-1.0f, 0.0f))) {
            const std::string p = ui::openFileDialog(kDocsFilter);
            if (attached && !p.empty())
                attachPath(st, D, *attached, p, projectDir);
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
            if (attached) attachPath(st, D, *attached, D.pathBuf, projectDir);
            D.pathBuf[0] = '\0';
        }
        ImGui::EndDisabled();
    }
    ImGui::Spacing();
    ImGui::EndDisabled();

    // ---- entries: everything the agent touched first, then the user's own attachments ----
    std::vector<DocEntry> entries;
    std::unordered_set<std::string> seen;
    collectArtifacts(st, projectDir, entries, seen);
    collectAgentDocs(st, projectDir, entries, seen);
    if (attached) {
        for (const auto& p : *attached) {
            if (!agentDocInProject(p, std::string(), projectDir) ||
                !seen.insert(p).second) continue;
            DocEntry e;
            e.path = p;
            e.name = baseName(p);
            e.key  = p;
            e.kind = "attached";
            e.attached = true;
            entries.push_back(std::move(e));
        }
    }
    // ONE flat vector — selection still addresses entries[i] directly; the two labelled
    // sections are only a header drawn before each run.
    const size_t attachedFrom = (size_t)(
        std::stable_partition(entries.begin(), entries.end(),
                              [](const DocEntry& e) { return e.agentTouched; }) -
        entries.begin());
    // Selection survives panel visits but cannot survive project changes or row removal:
    // otherwise the right-hand preview exposes a document from another project.
    if (!D.selected.empty() &&
        std::none_of(entries.begin(), entries.end(),
                     [&](const DocEntry& e) { return e.key == D.selected; }))
        D.selected.clear();
    if (entries.empty()) {
        const std::string name = lexicalPath(projectDir).filename().string();
        const std::string title = "Nothing for " +
            (name.empty() ? std::string("this project") : name) + " yet";
        emptyState(title.c_str(),
                   "Files agents create in this project show up here. Attach a project file "
                   "to preview or edit it next to the chat.");
        return;
    }
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
    ImGui::Text("Documents (%d)", (int)entries.size());
    ImGui::PopStyleColor();
    ImGui::Spacing();

    int removeIdx = -1;
    D.selectedKind.clear();
    for (int i = 0; i < (int)entries.size(); ++i) {
        if (i == 0 && attachedFrom > 0) {
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextUnformatted("Agent output");
            ImGui::PopStyleColor();
        }
        if (i == (int)attachedFrom && attachedFrom < entries.size()) {
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::TextUnformatted("Attached");
            ImGui::PopStyleColor();
        }
        const DocEntry& e = entries[(size_t)i];
        if (D.selected == e.key) D.selectedKind = e.kind;
        bool removeClicked = false;
        if (docRow(D, e, D.selected == e.key, surf, S, removeClicked)) {
            D.selected     = e.key;
            D.selectedKind = e.kind;
        }
        if (removeClicked) removeIdx = i;
    }
    if (attached && removeIdx >= 0 && entries[(size_t)removeIdx].attached) {
        const std::string p = entries[(size_t)removeIdx].path;
        attached->erase(std::remove(attached->begin(), attached->end(), p), attached->end());
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

    // Both content children scroll with the app's pill-on-hairline scrollbar (NoScrollbar
    // suppresses ImGui's default bar so the pill replaces it rather than doubling it) and
    // carry the shared menu gutter so rows never touch the glass edge.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(ui::metrics::cpMenuGutter(), ui::metrics::px(8.0f)));

    // ---- LEFT: session document list ----
    ImGui::BeginChild("##docs_left", ImVec2(leftW, 0.0f),
                      ImGuiChildFlags_AlwaysUseWindowPadding, ImGuiWindowFlags_NoScrollbar);
    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const ImVec2 c0 = ImGui::GetWindowPos();
        const ImVec2 c1(c0.x + ImGui::GetWindowSize().x, c0.y + ImGui::GetWindowSize().y);
        glassPanel(dl, ImRect(c0, c1), GlassLevel::Thin, rowRoundDp(), surf);
        drawDocList(st, D, surf, S);
    }
    drawWindowPillScrollbarV(surf);
    ImGui::EndChild();

    ImGui::SameLine(0.0f, ui::metrics::cpMenuRowGap());

    // ---- RIGHT: preview / edit ----
    ImGui::BeginChild("##docs_right", ImVec2(0.0f, 0.0f),
                      ImGuiChildFlags_AlwaysUseWindowPadding, ImGuiWindowFlags_NoScrollbar);
    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const ImVec2 c0 = ImGui::GetWindowPos();
        const ImVec2 c1(c0.x + ImGui::GetWindowSize().x, c0.y + ImGui::GetWindowSize().y);
        glassPanel(dl, ImRect(c0, c1), GlassLevel::Thin, rowRoundDp(), surf);
        drawPreview(st, D, surf, S);
    }
    drawWindowPillScrollbarV(surf);
    ImGui::EndChild();

    ImGui::PopStyleVar();
}

}  // namespace ui
