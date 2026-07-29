// PLM Compare page — rendered geometry diff between two saved versions
// (see plm_compare.h for the data contract).
//
// One async RPC per selection: plm_compare {path, item_id, commit_a, commit_b,
// visual:true}. It does real geometry work (two rebuilds + OCCT booleans + a
// handful of renders), so it runs with a generous timeout and the page shows a
// spinner while in flight — never a sync worker call on the GL thread.
//
// Item + revision pickers read g_plmHub's already-cached item list and history
// (plmEnsureItems / plmEnsureHistory); this page adds no second fetch path for
// data the hub owns. Revisions are named by their message + a user-facing
// version ordinal + a relative time — never a raw commit id (contract §8.1).
#include "plm_compare.h"

#include "graph_thumbs.h"
#include "plm_hub.h"

#include <cmath>
#include <cstdarg>
#include <cstdio>
#include <ctime>
#include <string>
#include <unordered_map>
#include <vector>

#include "imgui.h"

#include "core/app_state.h"
#include "core/async_rpc.h"
#include "ui_theme.h"

namespace ui {
namespace {

// ---- fixed diff colour semantics (shared contract; same RGB in engine + PIL) --
constexpr float kAdded[3]     = {0.16f, 0.72f, 0.32f};
constexpr float kRemoved[3]   = {0.86f, 0.24f, 0.24f};
constexpr float kUnchanged[3] = {0.62f, 0.64f, 0.67f};

ImVec4 col4(const float c[3], float a = 1.0f) { return ImVec4(c[0], c[1], c[2], a); }

nlohmann::json pathParam(AppState& st) {
    nlohmann::json p;
    p["path"] = st.plmProjectPath();
    return p;
}

std::string getStr(const nlohmann::json& j, const char* k) {
    auto it = j.find(k);
    if (it == j.end() || it->is_null()) return "";
    if (it->is_string()) return it->get<std::string>();
    return it->dump();
}

// ---- page state ---------------------------------------------------------------

struct DiffTex {
    unsigned int tex = 0;
    int w = 0, h = 0;
    bool failed = false;      // decode failed — show the empty state, not a blank rect
};

struct CompareState {
    std::string projectDir;

    // selection
    std::string itemId, commitA, commitB;

    // request/result bookkeeping (mirrors the hub's gen-staleness guard)
    bool        loading = false;
    std::string pendingKey;         // request in flight ("" = none)
    std::string resultKey;          // selection the held result belongs to
    std::string err, errReason;
    bool        have = false;

    // semantic half (always present when the RPC succeeds)
    std::string    summary;
    nlohmann::json docDiff;

    // visual half (eval_compare)
    bool        degraded = false;
    std::string reason, hint;
    bool        identical = false;
    bool        haveMatch = false;
    double      matchPct = 0.0;
    bool        haveVols = false;
    double      addedVol = 0.0, removedVol = 0.0;
    std::string imageA, imageB, sideBySide;
    std::string diff3d, diff3dChangesOnly, diff2d, diffPixel;
    std::string view3dIso, view3dFront, view3dTop;
    bool        haveCounts = false;
    int         cAdded = 0, cRemoved = 0, cUnchanged = 0;

    // view state
    int  view = 0;         // 0 Diff | 1 Side by side | 2 Changes
    int  viewDir = 0;      // 0 ISO | 1 FRONT | 2 TOP
    bool changesOnly = false;

    // loaded textures, keyed by the absolute png path the RPC returned
    std::unordered_map<std::string, DiffTex> tex;
};

CompareState& C() {
    static CompareState c;
    return c;
}

void releaseTextures() {
    for (auto& kv : C().tex) plmFreePngTexture(kv.second.tex);
    C().tex.clear();
}

// Lazily decode one result png. Returns null while/after a failed decode so the
// caller paints an empty state instead of a blank rectangle.
const DiffTex* ensureTex(const std::string& path) {
    if (path.empty()) return nullptr;
    auto& map = C().tex;
    auto it = map.find(path);
    if (it == map.end()) {
        DiffTex t;
        if (!plmLoadPngTexture(path, t.tex, t.w, t.h)) t.failed = true;
        it = map.emplace(path, t).first;
    }
    return it->second.failed ? nullptr : &it->second;
}

void clearResult() {
    releaseTextures();
    CompareState& c = C();
    c.have = false;
    c.summary.clear();
    c.docDiff = nlohmann::json::object();
    c.degraded = false;
    c.reason.clear();
    c.hint.clear();
    c.identical = false;
    c.haveMatch = false; c.matchPct = 0.0;
    c.haveVols = false;  c.addedVol = 0.0; c.removedVol = 0.0;
    c.imageA.clear(); c.imageB.clear(); c.sideBySide.clear();
    c.diff3d.clear(); c.diff3dChangesOnly.clear();
    c.diff2d.clear(); c.diffPixel.clear();
    c.view3dIso.clear(); c.view3dFront.clear(); c.view3dTop.clear();
    c.haveCounts = false; c.cAdded = c.cRemoved = c.cUnchanged = 0;
}

std::string selectionKey() {
    return C().itemId + "|" + C().commitA + "|" + C().commitB;
}

// ---- fetch --------------------------------------------------------------------

void fetchCompare(AppState& st) {
    CompareState& c = C();
    if (c.loading || c.itemId.empty() || c.commitA.empty() || c.commitB.empty()) return;
    const std::string key = selectionKey();
    c.loading = true;
    c.pendingKey = key;
    c.err.clear();
    c.errReason.clear();
    const int gen = g_plmHub.generation;
    nlohmann::json p = pathParam(st);
    p["item_id"]  = c.itemId;
    p["commit_a"] = c.commitA;   // older
    p["commit_b"] = c.commitB;   // newer
    p["visual"]   = true;
    // Two rebuilds + booleans + several renders: allow well past the 120s default.
    st.rpc.call("plm_compare", p, [gen, key](const core::RpcResult& r) {
        CompareState& c = C();
        c.loading = false;
        c.pendingKey.clear();
        if (gen != g_plmHub.generation) return;   // project/selection moved on
        c.resultKey = key;
        clearResult();
        if (!plmOk(r, c.err)) { c.errReason = plmReason(r); return; }
        c.err.clear();
        c.errReason.clear();
        c.have = true;
        const auto& res = r.result;
        c.summary = getStr(res, "summary");
        if (auto d = res.find("doc_diff"); d != res.end() && d->is_object()) c.docDiff = *d;

        auto ev = res.find("eval_compare");
        if (ev == res.end() || !ev->is_object()) {
            c.degraded = true;
            c.reason   = "no_visual";
            c.hint     = "This comparison has no rendered images. The change list below "
                         "is still complete.";
            return;
        }
        const auto& e  = *ev;
        c.degraded  = e.value("degraded", false);
        c.reason    = getStr(e, "reason");
        c.hint      = getStr(e, "hint");
        c.identical = e.value("identical", false);
        if (auto m = e.find("match_pct"); m != e.end() && m->is_number()) {
            c.haveMatch = true;
            c.matchPct  = m->get<double>();
        }
        if (auto a = e.find("added_volume_mm3"); a != e.end() && a->is_number()) {
            c.haveVols = true;
            c.addedVol = a->get<double>();
        }
        if (auto rv = e.find("removed_volume_mm3"); rv != e.end() && rv->is_number()) {
            c.haveVols   = true;
            c.removedVol = rv->get<double>();
        }
        c.imageA            = getStr(e, "image_a");
        c.imageB            = getStr(e, "image_b");
        c.sideBySide        = getStr(e, "side_by_side");
        c.diff3d            = getStr(e, "diff_3d");
        c.diff3dChangesOnly = getStr(e, "diff_3d_changes_only");
        c.diff2d            = getStr(e, "diff_2d");
        c.diffPixel         = getStr(e, "diff_pixel");
        if (auto v = e.find("diff_3d_views"); v != e.end() && v->is_object()) {
            c.view3dIso   = getStr(*v, "ISO");
            c.view3dFront = getStr(*v, "FRONT");
            c.view3dTop   = getStr(*v, "TOP");
        }
        if (auto k = e.find("counts"); k != e.end() && k->is_object()) {
            c.haveCounts = true;
            c.cAdded     = k->value("added", 0);
            c.cRemoved   = k->value("removed", 0);
            c.cUnchanged = k->value("unchanged", 0);
        }
    }, /*timeoutMs=*/600000);
}

// ---- small helpers ------------------------------------------------------------

// Days since 1970-01-01 for a proleptic Gregorian y/m/d (Howard Hinnant's
// days_from_civil) — no timegm, no locale, no time zone surprises.
long long civilDays(int y, unsigned m, unsigned d) {
    y -= m <= 2;
    const int era = (y >= 0 ? y : y - 399) / 400;
    const unsigned yoe = (unsigned)(y - era * 400);
    const unsigned doy = (153u * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return (long long)era * 146097LL + (long long)doe - 719468LL;
}

// "2026-07-07T19:54:28Z" -> seconds since epoch, or 0 when unparseable.
long long isoToEpoch(const std::string& iso) {
    int y = 0, mo = 0, d = 0, h = 0, mi = 0, s = 0;
    if (std::sscanf(iso.c_str(), "%d-%d-%dT%d:%d:%d", &y, &mo, &d, &h, &mi, &s) < 5)
        return 0;
    if (y < 1970 || mo < 1 || mo > 12 || d < 1 || d > 31) return 0;
    return civilDays(y, (unsigned)mo, (unsigned)d) * 86400LL + h * 3600LL + mi * 60LL + s;
}

// Human, ASCII-only relative time ("just now", "3 hours ago", "2 days ago").
std::string relTime(const std::string& iso) {
    long long t = isoToEpoch(iso);
    if (t == 0) return iso;
    long long now = (long long)std::time(nullptr);
    long long dt  = now - t;
    if (dt < 0) dt = 0;
    char buf[48];
    if (dt < 90) std::snprintf(buf, sizeof(buf), "just now");
    else if (dt < 5400) std::snprintf(buf, sizeof(buf), "%lld minutes ago", dt / 60);
    else if (dt < 172800) std::snprintf(buf, sizeof(buf), "%lld hours ago", dt / 3600);
    else if (dt < 2592000) std::snprintf(buf, sizeof(buf), "%lld days ago", dt / 86400);
    else if (dt < 31536000) std::snprintf(buf, sizeof(buf), "%lld months ago", dt / 2592000);
    else std::snprintf(buf, sizeof(buf), "%lld years ago", dt / 31536000);
    return buf;
}

std::string entryHeadline(const PlmEntryRow& e) {
    if (!e.message.empty()) return e.message;
    if (!e.summary.empty()) return e.summary;
    return "(saved)";
}

// User-facing label for one history entry. NEVER the commit id: a version
// ordinal (newest = highest), the change message and a relative time.
std::string revLabel(const std::vector<PlmEntryRow>& hist, size_t idx) {
    char num[24];
    std::snprintf(num, sizeof(num), "Version %d", (int)(hist.size() - idx));
    return std::string(num) + " - " + entryHeadline(hist[idx]) +
           "  (" + relTime(hist[idx].authoredAt) + ")";
}

int indexOfCommit(const std::vector<PlmEntryRow>& hist, const std::string& commit) {
    for (size_t i = 0; i < hist.size(); ++i)
        if (hist[i].commit == commit) return (int)i;
    return -1;
}

std::string itemLabel(const PlmItemRow& it) {
    std::string base = !it.partNumber.empty() ? it.partNumber : it.path;
    if (base.empty()) base = "(untitled)";
    if (!it.path.empty() && base != it.path) {
        size_t sl = it.path.find_last_of("/\\");
        base += "  " + (sl == std::string::npos ? it.path : it.path.substr(sl + 1));
    }
    return base;
}

const PlmItemRow* findItem(const std::string& id) {
    for (const auto& it : g_plmHub.items)
        if (it.itemId == id) return &it;
    return nullptr;
}

void dimText(const char* fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
    ImGui::TextV(fmt, ap);
    ImGui::PopStyleColor();
    va_end(ap);
}

// Small pill-shaped toggle row used for the view segmented controls. Matches the
// SmallButton scope toggle the History page already uses.
bool segButton(const char* label, bool sel) {
    ImGui::PushStyleColor(ImGuiCol_Button, sel
        ? ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.30f)
        : ImVec4(palette.button[0], palette.button[1], palette.button[2], 0.5f));
    bool hit = ImGui::SmallButton(label);
    ImGui::PopStyleColor();
    return hit;
}

// One colour swatch + caption, inline on the current row.
void legendSwatch(const float c[3], const char* label) {
    const float S = ui::uiScale;
    const float sz = 10 * S;
    ImVec2 p = ImGui::GetCursorScreenPos();
    float  fh = ImGui::GetFrameHeight();
    ImGui::GetWindowDrawList()->AddRectFilled(
        ImVec2(p.x, p.y + (fh - sz) * 0.5f), ImVec2(p.x + sz, p.y + (fh + sz) * 0.5f),
        ImGui::GetColorU32(col4(c, 0.95f)), 2.0f * S);
    ImGui::Dummy(ImVec2(sz + 5 * S, fh));
    ImGui::SameLine();
    dimText("%s", label);
}

// Draw one result png scaled to fit `avail`, preserving aspect and centred.
// No re-fit of the pixels themselves: the engine framed every view and both
// modes on the SAME camera, so toggling must not move the image.
void drawFitted(const std::string& path, ImVec2 avail, const char* emptyMsg) {
    const DiffTex* t = ensureTex(path);
    if (!t || t->w <= 0 || t->h <= 0) {
        plmEmptyState("[ ]", emptyMsg, nullptr);
        return;
    }
    float sx = avail.x / (float)t->w;
    float sy = avail.y / (float)t->h;
    float k  = sx < sy ? sx : sy;
    if (k <= 0.0f) return;
    float w = (float)t->w * k, h = (float)t->h * k;
    float padX = (avail.x - w) * 0.5f;
    if (padX > 0) ImGui::SetCursorPosX(ImGui::GetCursorPosX() + padX);
    ImGui::Image((ImTextureID)(intptr_t)t->tex, ImVec2(w, h));
}

// ---- header: item + revision pickers -----------------------------------------

void drawSelectors(AppState& st) {
    const float S = ui::uiScale;
    CompareState& c = C();
    auto& H = g_plmHub;

    const float comboW = 300 * S;

    // item
    const PlmItemRow* cur = findItem(c.itemId);
    std::string curLabel = cur ? itemLabel(*cur) : "Pick an item";
    ImGui::AlignTextToFramePadding();
    dimText("Item");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(comboW);
    if (ImGui::BeginCombo("##cmp_item", curLabel.c_str())) {
        for (const auto& it : H.items) {
            bool sel = (it.itemId == c.itemId);
            if (ImGui::Selectable(itemLabel(it).c_str(), sel) && !sel) {
                c.itemId = it.itemId;
                H.selectedItem = it.itemId;
            }
        }
        ImGui::EndCombo();
    }
    ImGui::SameLine();
    if (cur) plmDocTypeTag(cur->docType, cur->kind);

    // revisions
    int ia = indexOfCommit(H.history, c.commitA);
    int ib = indexOfCommit(H.history, c.commitB);
    auto revCombo = [&](const char* id, const char* label, std::string& commit, int idx) {
        ImGui::AlignTextToFramePadding();
        dimText("%s", label);
        ImGui::SameLine();
        ImGui::SetNextItemWidth(comboW * 1.35f);
        std::string preview = (idx >= 0) ? revLabel(H.history, (size_t)idx) : "Pick a version";
        if (ImGui::BeginCombo(id, preview.c_str())) {
            for (size_t i = 0; i < H.history.size(); ++i) {
                bool sel = ((int)i == idx);
                if (ImGui::Selectable(revLabel(H.history, i).c_str(), sel) && !sel)
                    commit = H.history[i].commit;
            }
            ImGui::EndCombo();
        }
    };
    revCombo("##cmp_a", "Older", c.commitA, ia);
    ImGui::SameLine(0, 12 * S);
    revCombo("##cmp_b", "Newer", c.commitB, ib);

    ImGui::SameLine(0, 12 * S);
    bool ready = !c.itemId.empty() && !c.commitA.empty() && !c.commitB.empty() &&
                 c.commitA != c.commitB;
    ImGui::BeginDisabled(!ready || c.loading);
    if (ImGui::Button("Compare")) {
        c.resultKey.clear();       // force a refetch of the current selection
        fetchCompare(st);
    }
    ImGui::EndDisabled();
}

// ---- stats strip --------------------------------------------------------------

void drawStats() {
    const float S = ui::uiScale;
    const CompareState& c = C();

    plmRowPill(false, 1.0f);
    ImGui::Dummy(ImVec2(8 * S, ImGui::GetFrameHeight()));
    ImGui::SameLine();
    if (c.haveMatch) {
        ImGui::AlignTextToFramePadding();
        ImGui::Text("%.1f%% match", c.matchPct);
    } else {
        ImGui::AlignTextToFramePadding();
        dimText("match n/a");
    }
    ImGui::SameLine(0, 14 * S);

    ImGui::PushStyleColor(ImGuiCol_Text, col4(kAdded));
    ImGui::AlignTextToFramePadding();
    if (c.haveVols) ImGui::Text("+%.1f mm3", c.addedVol);
    else            ImGui::TextUnformatted("+ mm3 n/a");
    ImGui::PopStyleColor();
    ImGui::SameLine(0, 10 * S);

    ImGui::PushStyleColor(ImGuiCol_Text, col4(kRemoved));
    ImGui::AlignTextToFramePadding();
    if (c.haveVols) ImGui::Text("-%.1f mm3", c.removedVol);
    else            ImGui::TextUnformatted("- mm3 n/a");
    ImGui::PopStyleColor();

    if (c.haveCounts) {
        ImGui::SameLine(0, 14 * S);
        ImGui::AlignTextToFramePadding();
        dimText("strokes  +%d  -%d  =%d", c.cAdded, c.cRemoved, c.cUnchanged);
    }
    if (c.identical) {
        ImGui::SameLine(0, 14 * S);
        ImGui::AlignTextToFramePadding();
        ImGui::PushStyleColor(ImGuiCol_Text, col4(kUnchanged));
        ImGui::TextUnformatted("no geometry change");
        ImGui::PopStyleColor();
    }

    ImGui::SameLine(0, 18 * S);
    legendSwatch(kAdded, "Added");
    ImGui::SameLine(0, 10 * S);
    legendSwatch(kRemoved, "Removed");
    ImGui::SameLine(0, 10 * S);
    legendSwatch(kUnchanged, "Unchanged");
}

// ---- Diff view ----------------------------------------------------------------

void drawDiffView() {
    const float S = ui::uiScale;
    CompareState& c = C();

    const PlmItemRow* it = findItem(c.itemId);
    const bool vector2d = it && (it->docType == "drawing" || it->docType == "ecad" ||
                                 it->kind == "drawing" || it->kind == "ecad");

    std::string path;
    if (vector2d) {
        path = !c.diff2d.empty() ? c.diff2d : c.diffPixel;
    } else {
        // ISO / FRONT / TOP all come from the same camera framing, so switching
        // view or mode swaps the source png only — no client-side re-fit.
        const std::string* named = (c.viewDir == 1) ? &c.view3dFront
                                 : (c.viewDir == 2) ? &c.view3dTop
                                                    : &c.view3dIso;
        if (c.changesOnly && !c.diff3dChangesOnly.empty()) path = c.diff3dChangesOnly;
        else if (!named->empty())                          path = *named;
        else if (!c.diff3d.empty())                        path = c.diff3d;
        else                                               path = c.diffPixel;

        static const char* kDirs[] = {"ISO", "FRONT", "TOP"};
        for (int i = 0; i < 3; ++i) {
            if (i) ImGui::SameLine();
            if (segButton(kDirs[i], c.viewDir == i)) c.viewDir = i;
        }
        ImGui::SameLine(0, 14 * S);
        if (segButton("Changes only", c.changesOnly)) c.changesOnly = !c.changesOnly;
        ImGui::SameLine();
        ImGui::AlignTextToFramePadding();
        dimText("%s", c.changesOnly ? "unchanged geometry hidden"
                                    : "unchanged geometry ghosted");
        ImGui::Dummy(ImVec2(0, 3 * S));
    }

    ImVec2 avail = ImGui::GetContentRegionAvail();
    if (avail.y < 40 * S) avail.y = 40 * S;
    drawFitted(path, avail, "No diff image for this pair");
}

// ---- Side by side view --------------------------------------------------------

void drawSideBySideView() {
    const float S = ui::uiScale;
    CompareState& c = C();
    auto& H = g_plmHub;

    int ia = indexOfCommit(H.history, c.commitA);
    int ib = indexOfCommit(H.history, c.commitB);
    std::string la = ia >= 0 ? revLabel(H.history, (size_t)ia) : "Older version";
    std::string lb = ib >= 0 ? revLabel(H.history, (size_t)ib) : "Newer version";

    ImVec2 avail = ImGui::GetContentRegionAvail();
    float colW = (avail.x - 10 * S) * 0.5f;
    float colH = avail.y - ImGui::GetFrameHeight() * 2.0f;
    if (colH < 40 * S) colH = 40 * S;

    ImGui::BeginGroup();
    ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + colW);
    dimText("%s", la.c_str());
    ImGui::PopTextWrapPos();
    ImGui::BeginChild("##cmp_a_img", ImVec2(colW, colH), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    drawFitted(c.imageA, ImGui::GetContentRegionAvail(), "No render for this version");
    ImGui::EndChild();
    ImGui::EndGroup();

    ImGui::SameLine(0, 10 * S);

    ImGui::BeginGroup();
    ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + colW);
    dimText("%s", lb.c_str());
    ImGui::PopTextWrapPos();
    ImGui::BeginChild("##cmp_b_img", ImVec2(colW, colH), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    drawFitted(c.imageB, ImGui::GetContentRegionAvail(), "No render for this version");
    ImGui::EndChild();
    ImGui::EndGroup();
}

// ---- Changes view (the semantic half) ----------------------------------------

// One coloured change row inside the row pill.
void changeRow(const float col[3], const char* sign, const std::string& text) {
    const float S = ui::uiScale;
    plmRowPill(false, 1.0f);
    ImGui::Dummy(ImVec2(8 * S, ImGui::GetFrameHeight()));
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text, col4(col));
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(sign);
    ImGui::PopStyleColor();
    ImGui::SameLine();
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(text.c_str());
}

// A doc_diff list of ids (part/asm shape) or of {id, summary} records (ECAD).
void drawIdList(const nlohmann::json& diff, const char* key, const float col[3],
                const char* sign, const char* title, int& rows) {
    auto it = diff.find(key);
    if (it == diff.end() || !it->is_array() || it->empty()) return;
    plmSectionHeader(title, nullptr);
    for (const auto& v : *it) {
        std::string label;
        if (v.is_string()) label = v.get<std::string>();
        else if (v.is_object()) {
            label = getStr(v, "summary");
            if (label.empty()) label = getStr(v, "id");
            if (label.empty()) label = v.dump();
        } else {
            label = v.dump();
        }
        changeRow(col, sign, label);
        ++rows;
    }
}

std::string numText(const nlohmann::json& v) {
    if (v.is_number_float()) {
        char buf[48];
        std::snprintf(buf, sizeof(buf), "%g", v.get<double>());
        return buf;
    }
    if (v.is_string()) return v.get<std::string>();
    if (v.is_null()) return "none";
    return v.dump();
}

void drawChangesView() {
    const float S = ui::uiScale;
    CompareState& c = C();

    if (!c.summary.empty()) {
        ImGui::PushTextWrapPos(0.0f);
        ImGui::TextUnformatted(c.summary.c_str());
        ImGui::PopTextWrapPos();
        ImGui::Dummy(ImVec2(0, 4 * S));
    }

    ImGui::BeginChild("##cmp_changes", ImVec2(0, 0), ImGuiChildFlags_None);
    int rows = 0;
    const nlohmann::json& d = c.docDiff;

    // part / assembly shape
    drawIdList(d, "features_added",   kAdded,     "+", "Features added",   rows);
    drawIdList(d, "features_removed", kRemoved,   "-", "Features removed", rows);
    drawIdList(d, "features_changed", kUnchanged, "~", "Features changed", rows);

    if (auto pc = d.find("params_changed"); pc != d.end() && pc->is_array() && !pc->empty()) {
        plmSectionHeader("Dimensions changed", nullptr);
        for (const auto& rec : *pc) {
            if (!rec.is_object()) continue;
            std::string feat  = getStr(rec, "feature");
            std::string param = getStr(rec, "param");
            std::string oldV  = rec.contains("old") ? numText(rec["old"]) : "";
            std::string newV  = rec.contains("new") ? numText(rec["new"]) : "";
            changeRow(kUnchanged, "~",
                      feat + "  " + param + ": " + oldV + " -> " + newV);
            ++rows;
        }
    }

    // ECAD shape (<section>_added / _removed / _changed)
    static const char* kSections[] = {"nets", "components", "footprints", "tracks"};
    static const char* kTitles[]   = {"Nets", "Components", "Footprints", "Tracks"};
    for (int i = 0; i < 4; ++i) {
        std::string base = kSections[i];
        drawIdList(d, (base + "_added").c_str(),   kAdded,
                   "+", (std::string(kTitles[i]) + " added").c_str(),   rows);
        drawIdList(d, (base + "_removed").c_str(), kRemoved,
                   "-", (std::string(kTitles[i]) + " removed").c_str(), rows);
        drawIdList(d, (base + "_changed").c_str(), kUnchanged,
                   "~", (std::string(kTitles[i]) + " changed").c_str(), rows);
    }

    if (rows == 0)
        plmEmptyState("[=]", "No design changes",
                      "These two versions have the same modelled content.");
    ImGui::EndChild();
}

}  // namespace

// ---- page ---------------------------------------------------------------------

void plmDrawComparePage(AppState& st) {
    const float S = ui::uiScale;
    CompareState& c = C();
    auto& H = g_plmHub;

    // Project switch: drop every texture and the held result.
    if (c.projectDir != H.projectDir) {
        c.projectDir = H.projectDir;
        c.itemId.clear();
        c.commitA.clear();
        c.commitB.clear();
        c.resultKey.clear();
        c.pendingKey.clear();
        c.err.clear();
        clearResult();
    }

    // The pickers ride the hub's caches — no second fetch path.
    plmEnsureItems(st);
    plmEnsureHistory(st);

    plmSectionHeader("Compare versions", "split");
    dimText("The difference between two saved versions, drawn on the model: "
            "green is added, red is removed, grey is unchanged.");
    ImGui::Dummy(ImVec2(0, 4 * S));

    if (!H.itemsErr.empty()) { plmError(H.itemsErr); return; }
    if (!H.histErr.empty())  { plmError(H.histErr);  return; }
    if ((H.itemsLoading && H.items.empty()) || (H.histLoading && H.history.empty())) {
        plmSpinner("Loading");
        return;
    }
    if (H.items.empty()) {
        plmEmptyState("[ ]", "Nothing to compare yet",
                      "Track a file on the Files page first.");
        return;
    }
    if (H.history.size() < 2) {
        plmEmptyState("[~]", "Need at least two saved versions",
                      "Save a checkpoint, make a change, then save another.");
        return;
    }

    // Defaults: newest version on the right, the one before it on the left.
    if (c.itemId.empty())
        c.itemId = !H.selectedItem.empty() ? H.selectedItem : H.items.front().itemId;
    if (c.commitB.empty()) c.commitB = H.history[0].commit;   // newest
    if (c.commitA.empty()) c.commitA = H.history[1].commit;   // the one before

    drawSelectors(st);
    ImGui::Dummy(ImVec2(0, 4 * S));

    // Auto-run whenever a valid, changed selection is not already answered.
    const std::string key = selectionKey();
    const bool valid = !c.itemId.empty() && !c.commitA.empty() && !c.commitB.empty() &&
                       c.commitA != c.commitB;
    if (valid && !c.loading && key != c.resultKey && key != c.pendingKey)
        fetchCompare(st);

    if (!c.err.empty()) {
        plmError(c.err);
        if (c.errReason == "no_such_item")
            dimText("Pick a different item — this one has no history under this project.");
        return;
    }
    if (c.loading && !c.have) { plmSpinner("Comparing versions"); return; }
    if (!c.have) {
        plmEmptyState("[ ]", "Pick two versions to compare", nullptr);
        return;
    }
    if (c.loading) { plmSpinner("Comparing versions"); ImGui::Dummy(ImVec2(0, 2 * S)); }

    if (c.commitA == c.commitB) {
        plmEmptyState("[=]", "Pick two different versions", nullptr);
        return;
    }

    drawStats();
    ImGui::Dummy(ImVec2(0, 4 * S));

    // Degraded visual half: show the user-facing hint and land on Changes, which
    // always works (the semantic diff needs no engine).
    const bool noImages = c.degraded && c.diff3d.empty() && c.diff2d.empty() &&
                          c.diffPixel.empty() && c.imageA.empty() && c.imageB.empty();
    if (c.degraded && !c.hint.empty()) {
        ImGui::PushTextWrapPos(0.0f);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusWarn[0], palette.statusWarn[1], palette.statusWarn[2], 1.0f));
        ImGui::TextUnformatted(c.hint.c_str());
        ImGui::PopStyleColor();
        ImGui::PopTextWrapPos();
        ImGui::Dummy(ImVec2(0, 4 * S));
    }
    if (noImages) c.view = 2;

    static const char* kViews[] = {"Diff", "Side by side", "Changes"};
    for (int i = 0; i < 3; ++i) {
        if (i) ImGui::SameLine();
        ImGui::BeginDisabled(noImages && i != 2);
        if (segButton(kViews[i], c.view == i)) c.view = i;
        ImGui::EndDisabled();
    }
    ImGui::Separator();
    ImGui::Dummy(ImVec2(0, 3 * S));

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::BeginChild("##cmp_body", ImVec2(0, 0), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ImGui::PopStyleVar();
    if (c.view == 0)      drawDiffView();
    else if (c.view == 1) drawSideBySideView();
    else                  drawChangesView();
    ImGui::EndChild();
}

}  // namespace ui
