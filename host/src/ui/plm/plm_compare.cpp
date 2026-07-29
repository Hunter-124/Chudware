// PLM Compare page — rendered geometry diff between a file's adjacent versions
// (see plm_compare.h for the data contract).
//
// The user picks a FILE in the left rail and steps through that file's own
// versions in a chip strip; every diff is resolved AUTOMATICALLY as
// "this version vs the one before it". There is no revision picker, no
// Compare button, and no dropdown anywhere on this page.
//
// Two async RPCs, both gen-guarded:
//   * plm_history {path, item_id} — the SELECTED FILE's own timeline. The hub's
//     cached history is project-wide, so its entries do not all contain the
//     selected item; pairing two of them is what produced "that item is not
//     present at one of those points". This page therefore owns a per-item
//     fetch. Per the contract those entries are newest-first, all contain the
//     item, and every adjacent pair is a valid compare pair; the last one is the
//     item's creation and has nothing before it.
//   * plm_compare {path, item_id, commit_a, commit_b, visual:true} — real
//     geometry work (two rebuilds + OCCT booleans + several renders), so a
//     generous timeout and a spinner while in flight; never a sync worker call
//     on the GL thread.
//
// Versions are named by a user-facing ordinal + change text + relative time —
// never a raw commit id (contract §8.1).
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

// author may arrive as a string or as an actor object — same rule plm_state uses.
std::string getActorStr(const nlohmann::json& j, const char* k) {
    auto it = j.find(k);
    if (it == j.end() || it->is_null()) return "";
    if (it->is_object()) return getStr(*it, "display");
    if (it->is_string()) return it->get<std::string>();
    return "";
}

// Same field names plm_state.cpp's parseEntry uses — one entry shape repo-wide.
PlmEntryRow parseEntryRow(const nlohmann::json& e) {
    PlmEntryRow r;
    r.commit     = getStr(e, "commit");
    r.kind       = getStr(e, "kind");
    r.scope      = getStr(e, "scope");
    r.authoredAt = getStr(e, "authored_at");
    r.author     = getActorStr(e, "author");
    r.message    = getStr(e, "message");
    r.summary    = getStr(e, "summary");
    return r;
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
    int         verIdx = 0;        // index into `entries` (0 = newest); older side is verIdx+1
    bool        scrollToVer = false;

    // the SELECTED FILE's own timeline (this page's fetch — not the hub's
    // project-wide history, which is what made the old page pair commits that
    // did not both contain the item).
    std::vector<PlmEntryRow> entries;
    std::string histItem;          // item `entries` (or the in-flight fetch) belongs to
    bool        histLoading = false;
    std::string histErr;
    int         histGen = -1;      // g_plmHub.generation the timeline was fetched at

    // per-item version counts for the left rail ("which files have something to
    // compare?"). Filled one item per frame at most, so a wide project does not
    // fan out N RPCs in a single frame.
    std::unordered_map<std::string, int> verCount;
    std::string countPending;

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

    // measured width of the stats group (last frame) — decides whether the dense
    // control row fits on one line or wraps to two.
    float statsW = 0.0f;

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

// ---- fetch: the selected file's own timeline -----------------------------------

void fetchItemHistory(AppState& st, const std::string& itemId) {
    CompareState& c = C();
    if (itemId.empty()) return;
    c.histLoading = true;
    c.histItem    = itemId;
    c.histGen     = g_plmHub.generation;
    c.histErr.clear();
    c.entries.clear();
    c.verIdx = 0;
    nlohmann::json p = pathParam(st);
    p["item_id"] = itemId;
    const int gen = c.histGen;
    st.rpc.call("plm_history", p, [gen, itemId](const core::RpcResult& r) {
        CompareState& c = C();
        c.histLoading = false;
        // Stale: the project moved on, or the user picked another file.
        if (gen != g_plmHub.generation || itemId != c.histItem) return;
        if (!plmOk(r, c.histErr)) return;
        c.histErr.clear();
        c.entries.clear();
        if (auto e = r.result.find("entries"); e != r.result.end() && e->is_array())
            for (const auto& en : *e) c.entries.push_back(parseEntryRow(en));
        c.verIdx      = 0;
        c.scrollToVer = true;
        c.verCount[itemId] = (int)c.entries.size();
    });
}

// One background timeline fetch per frame at most, purely to show a version
// count next to each file in the rail.
void fetchVersionCount(AppState& st, const std::string& itemId) {
    CompareState& c = C();
    if (itemId.empty() || !c.countPending.empty()) return;
    c.countPending = itemId;
    nlohmann::json p = pathParam(st);
    p["item_id"] = itemId;
    const int gen = g_plmHub.generation;
    st.rpc.call("plm_history", p, [gen, itemId](const core::RpcResult& r) {
        CompareState& c = C();
        if (c.countPending == itemId) c.countPending.clear();
        if (gen != g_plmHub.generation) return;
        std::string ignored;
        if (!plmOk(r, ignored)) return;         // no count is fine; the rail just omits it
        int n = 0;
        if (auto e = r.result.find("entries"); e != r.result.end() && e->is_array())
            n = (int)e->size();
        c.verCount[itemId] = n;
    });
}

// ---- fetch: the comparison -----------------------------------------------------

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
    // This page draws the view name, the legend and the dimensions as real
    // widgets, so ask the engine NOT to bake them into the pixels.
    p["annotate"] = false;
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

// The human text for one entry. `message` is often empty in real projects (the
// text lives in `summary`), so fall through — and never surface a commit id.
std::string entryHeadline(const PlmEntryRow& e) {
    if (!e.message.empty()) return e.message;
    if (!e.summary.empty()) return e.summary;
    return "(saved)";
}

// "Version 3 - Added Body.  (2 days ago)" for the side-by-side captions. The
// ordinal comes from the file's OWN timeline, so it is unique per version even
// when two entries share identical change text.
std::string versionLabel(size_t idx) {
    const CompareState& c = C();
    if (idx >= c.entries.size()) return "This version";
    char num[24];
    std::snprintf(num, sizeof(num), "Version %d", (int)(c.entries.size() - idx));
    return std::string(num) + " - " + entryHeadline(c.entries[idx]) +
           "  (" + relTime(c.entries[idx].authoredAt) + ")";
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

// Just the file name, for sentences ("the first saved version of keychain.cad").
std::string itemFileName(const PlmItemRow& it) {
    if (!it.path.empty()) {
        size_t sl = it.path.find_last_of("/\\");
        return sl == std::string::npos ? it.path : it.path.substr(sl + 1);
    }
    if (!it.partNumber.empty()) return it.partNumber;
    return "this file";
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

// Width one segButton will occupy — SmallButton is text + horizontal padding.
float segWidth(const char* label) {
    return ImGui::CalcTextSize(label).x + ImGui::GetStyle().FramePadding.x * 2.0f;
}

void tip(const std::string& text) {
    if (!text.empty() && ImGui::IsItemHovered()) ImGui::SetTooltip("%s", text.c_str());
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

// ---- left rail: the file list (replaces the old Item dropdown) -----------------

// Returns the item the user just clicked ("" = no change).
std::string drawFileRail(AppState& st, float railW, float bodyH) {
    const float S = ui::uiScale;
    CompareState& c = C();
    auto& H = g_plmHub;
    std::string picked;

    ImGui::BeginChild("##cmp_files", ImVec2(railW, bodyH), ImGuiChildFlags_None);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float rowH = ImGui::GetFrameHeight() * 1.75f;
    std::string needCount;

    for (const auto& it : H.items) {
        ImGui::PushID(it.itemId.c_str());
        const bool  sel = (it.itemId == c.itemId);
        const ImVec2 p0 = ImGui::GetCursorScreenPos();
        const float  w  = ImGui::GetContentRegionAvail().x;
        const ImVec2 p1(p0.x + w, p0.y + rowH);
        const bool   hov = ImGui::IsMouseHoveringRect(p0, p1);
        ui::glassRowBackground(dl, p0, p1, 4.0f, false, sel,
                               sel ? 0.42f : (hov ? 0.32f : 0.20f));
        if (ImGui::InvisibleButton("##row", ImVec2(w, rowH))) picked = it.itemId;
        const ImVec2 after = ImGui::GetCursorScreenPos();

        ImGui::PushClipRect(p0, p1, true);
        ImGui::SetCursorScreenPos(ImVec2(p0.x + 8 * S, p0.y + 3 * S));
        ImGui::TextUnformatted(itemLabel(it).c_str());
        ImGui::SetCursorScreenPos(
            ImVec2(p0.x + 8 * S, p0.y + 3 * S + ImGui::GetTextLineHeightWithSpacing()));
        plmDocTypeTag(it.docType, it.kind);
        auto cnt = c.verCount.find(it.itemId);
        if (cnt != c.verCount.end()) {
            ImGui::SameLine(0, 6 * S);
            ImGui::AlignTextToFramePadding();
            if (cnt->second >= 2) dimText("%d versions", cnt->second);
            else if (cnt->second == 1) dimText("1 version");
            else                      dimText("no versions");
        } else if (needCount.empty()) {
            needCount = it.itemId;
        }
        ImGui::PopClipRect();

        ImGui::SetCursorScreenPos(after);
        ImGui::Dummy(ImVec2(w, 3 * S));
        ImGui::PopID();
    }
    ImGui::EndChild();

    // One count fetch per frame, and never while the selected file's own
    // timeline is still in flight (that fetch fills its count too).
    if (!needCount.empty() && !c.histLoading) fetchVersionCount(st, needCount);
    return picked;
}

// ---- version strip: newest on the LEFT, plus < / > step buttons ----------------

void drawVersionStrip() {
    const float S = ui::uiScale;
    CompareState& c = C();
    const int n = (int)c.entries.size();

    const float stripH = ImGui::GetFrameHeight() + ImGui::GetStyle().ScrollbarSize + 4 * S;
    const float btnW   = 26 * S;
    const float gap    = 5 * S;
    float innerW = ImGui::GetContentRegionAvail().x - 2.0f * (btnW + gap);
    if (innerW < 80 * S) innerW = 80 * S;

    // "<" walks toward the newest version, ">" toward the oldest. Both clamp.
    ImGui::BeginDisabled(c.verIdx <= 0);
    if (ImGui::Button("<", ImVec2(btnW, stripH))) { c.verIdx--; c.scrollToVer = true; }
    ImGui::EndDisabled();
    tip("Newer version");
    ImGui::SameLine(0, gap);

    ImGui::BeginChild("##cmp_verstrip", ImVec2(innerW, stripH), ImGuiChildFlags_None,
                      ImGuiWindowFlags_HorizontalScrollbar);
    if (n == 0) {
        ImGui::AlignTextToFramePadding();
        dimText("no saved versions yet");
    }
    for (int i = 0; i < n; ++i) {
        const bool sel = (i == c.verIdx);
        const bool oldest = (i == n - 1);
        char label[96];
        std::snprintf(label, sizeof(label), "V%d  %s%s##ver%d",
                      n - i, relTime(c.entries[i].authoredAt).c_str(),
                      oldest ? "  created" : "", i);
        ImGui::PushStyleColor(ImGuiCol_Button, sel
            ? ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.30f)
            : ImVec4(palette.button[0], palette.button[1], palette.button[2], 0.5f));
        if (ImGui::Button(label, ImVec2(0, ImGui::GetFrameHeight()))) {
            c.verIdx = i;
        }
        ImGui::PopStyleColor();
        std::string t = entryHeadline(c.entries[i]);
        if (oldest) t += "\n(the first saved version of this file)";
        tip(t);
        if (sel && c.scrollToVer) ImGui::SetScrollHereX(0.5f);
        if (i + 1 < n) ImGui::SameLine(0, 6 * S);
    }
    ImGui::EndChild();
    c.scrollToVer = false;

    ImGui::SameLine(0, gap);
    ImGui::BeginDisabled(n == 0 || c.verIdx >= n - 1);
    if (ImGui::Button(">", ImVec2(btnW, stripH))) { c.verIdx++; c.scrollToVer = true; }
    ImGui::EndDisabled();
    tip("Older version");
}

// ---- one dense control row: stats left, view/direction toggles right ----------

void drawStatsInline() {
    const float S = ui::uiScale;
    const CompareState& c = C();

    ImGui::Dummy(ImVec2(8 * S, ImGui::GetFrameHeight()));
    ImGui::SameLine();
    ImGui::AlignTextToFramePadding();
    if (c.haveMatch) ImGui::Text("%.1f%% match", c.matchPct);
    else             dimText("match n/a");
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
}

static const char* kViews[] = {"Diff", "Side by side", "Changes"};
static const char* kDirs[]  = {"ISO", "FRONT", "TOP"};

float controlsWidth(bool vector2d) {
    const float S = ui::uiScale;
    float w = 0;
    for (int i = 0; i < 3; ++i) w += segWidth(kViews[i]);
    w += 8 * S;
    if (!vector2d) {
        w += 12 * S;
        for (int i = 0; i < 3; ++i) w += segWidth(kDirs[i]);
        w += 8 * S + 12 * S + segWidth("Changes only");
    }
    return w;
}

void drawControls(bool noImages, bool vector2d) {
    const float S = ui::uiScale;
    CompareState& c = C();
    for (int i = 0; i < 3; ++i) {
        if (i) ImGui::SameLine(0, 4 * S);
        ImGui::BeginDisabled(noImages && i != 2);
        if (segButton(kViews[i], c.view == i)) c.view = i;
        ImGui::EndDisabled();
    }
    if (vector2d) return;
    ImGui::SameLine(0, 12 * S);
    for (int i = 0; i < 3; ++i) {
        if (i) ImGui::SameLine(0, 4 * S);
        if (segButton(kDirs[i], c.viewDir == i)) c.viewDir = i;
    }
    ImGui::SameLine(0, 12 * S);
    if (segButton("Changes only", c.changesOnly)) c.changesOnly = !c.changesOnly;
    tip(c.changesOnly ? "unchanged geometry hidden" : "unchanged geometry ghosted");
}

void drawControlRow(bool noImages, bool vector2d) {
    const float S = ui::uiScale;
    CompareState& c = C();

    const float rightW = controlsWidth(vector2d);
    const float fullW  = ImGui::GetContentRegionAvail().x;
    // Wrap decision uses last frame's measured stats width; the pill has to be
    // painted before the text that sits on it, so it cannot be measured first.
    const bool wrap = (c.statsW > 0.0f) && (c.statsW + 16 * S + rightW > fullW);
    plmRowPill(false, wrap ? 2.0f : 1.0f);

    ImGui::BeginGroup();
    drawStatsInline();
    ImGui::EndGroup();
    c.statsW = ImGui::GetItemRectSize().x;

    const float slack = fullW - c.statsW - rightW;
    if (!wrap && slack > 0.0f) ImGui::SameLine(0, slack);
    else if (!wrap)            ImGui::SameLine(0, 8 * S);
    drawControls(noImages, vector2d);
}

// ---- Diff view ----------------------------------------------------------------

void drawDiffView(bool vector2d) {
    const float S = ui::uiScale;
    CompareState& c = C();

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
    }

    ImVec2 avail = ImGui::GetContentRegionAvail();
    if (avail.y < 40 * S) avail.y = 40 * S;
    drawFitted(path, avail, "No diff image for this pair");
}

// ---- Side by side view --------------------------------------------------------

void drawSideBySideView() {
    const float S = ui::uiScale;
    CompareState& c = C();

    // The pair is always (this version, the one before it) from the file's own
    // timeline — so the captions come straight off verIdx.
    std::string la = versionLabel((size_t)c.verIdx + 1);
    std::string lb = versionLabel((size_t)c.verIdx);

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

// ---- right column -------------------------------------------------------------

// The whole diff column: version strip, then whatever state the selection is in.
void drawDiffColumn(AppState& st, const PlmItemRow* item) {
    const float S = ui::uiScale;
    CompareState& c = C();
    const int n = (int)c.entries.size();
    const std::string fileName = item ? itemFileName(*item) : std::string("this file");

    if (c.verIdx < 0) c.verIdx = 0;
    if (n > 0 && c.verIdx >= n) c.verIdx = n - 1;

    // Per-item timeline states — all empty states, never a red error.
    if (c.histLoading && n == 0) { plmSpinner("Loading versions"); return; }
    if (!c.histErr.empty()) {
        plmEmptyState("[~]", "Versions for this file are not available yet",
                      c.histErr.c_str());
        return;
    }

    drawVersionStrip();
    ImGui::Dummy(ImVec2(0, 3 * S));

    if (n == 0) {
        plmEmptyState("[ ]", ("No saved versions of " + fileName + " yet").c_str(),
                      "Save a checkpoint on the History page, then come back here.");
        return;
    }

    // Resolve the pair automatically: this version vs the one before it.
    c.commitB = c.entries[(size_t)c.verIdx].commit;                        // newer
    c.commitA = ((size_t)c.verIdx + 1 < c.entries.size())
                    ? c.entries[(size_t)c.verIdx + 1].commit : std::string();  // older

    if (c.commitA.empty()) {
        // Terminal step: the file's creation. Nothing before it to diff against —
        // that is normal, not a failure.
        std::string title = (n == 1)
            ? fileName + " has one saved version so far"
            : "Version 1 is the first saved version of " + fileName;
        plmEmptyState("[*]", title.c_str(),
                      "There is nothing before it to compare against. Make a change and "
                      "save another version, then step back here to see exactly what moved.");
        return;
    }

    // Auto-run: no button. Fires whenever the resolved triple changes and is
    // neither already answered nor in flight.
    const std::string key = selectionKey();
    if (!c.loading && key != c.resultKey && key != c.pendingKey) fetchCompare(st);

    if (!c.err.empty()) {
        plmError(c.err);
        if (c.errReason == "no_such_item")
            dimText("This file has no history under this project.");
        return;
    }
    if (c.loading && !c.have) { plmSpinner("Comparing versions"); return; }
    if (!c.have) { plmEmptyState("[ ]", "Pick a version to compare", nullptr); return; }
    if (c.loading) { plmSpinner("Comparing versions"); }

    const bool vector2d = item && (item->docType == "drawing" || item->docType == "ecad" ||
                                   item->kind == "drawing" || item->kind == "ecad");
    const bool noImages = c.degraded && c.diff3d.empty() && c.diff2d.empty() &&
                          c.diffPixel.empty() && c.imageA.empty() && c.imageB.empty();
    if (noImages) c.view = 2;

    if (c.degraded && !c.hint.empty()) {
        ImGui::PushTextWrapPos(0.0f);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusWarn[0], palette.statusWarn[1], palette.statusWarn[2], 1.0f));
        ImGui::TextUnformatted(c.hint.c_str());
        ImGui::PopStyleColor();
        ImGui::PopTextWrapPos();
        ImGui::Dummy(ImVec2(0, 3 * S));
    }

    drawControlRow(noImages, vector2d);
    ImGui::Dummy(ImVec2(0, 3 * S));

    // The body takes every remaining pixel, so the image grows with the window.
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::BeginChild("##cmp_body", ImVec2(0, 0), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ImGui::PopStyleVar();
    if (c.view == 0)      drawDiffView(vector2d);
    else if (c.view == 1) drawSideBySideView();
    else                  drawChangesView();
    ImGui::EndChild();
}

}  // namespace

// ---- page ---------------------------------------------------------------------
bool plmCompareBusy() {
    const CompareState& c = C();
    return c.histLoading || c.loading;
}


void plmDrawComparePage(AppState& st) {
    const float S = ui::uiScale;
    CompareState& c = C();
    auto& H = g_plmHub;

    // Project switch: drop every texture, the held result and every timeline.
    if (c.projectDir != H.projectDir) {
        c.projectDir = H.projectDir;
        c.itemId.clear();
        c.commitA.clear();
        c.commitB.clear();
        c.resultKey.clear();
        c.pendingKey.clear();
        c.err.clear();
        c.entries.clear();
        c.histItem.clear();
        c.histErr.clear();
        c.histGen = -1;
        c.verIdx = 0;
        c.verCount.clear();
        c.countPending.clear();
        clearResult();
    }

    // The file list rides the hub's item cache; the per-item timeline is ours.
    plmEnsureItems(st);

    plmSectionHeader("Compare versions", "split");
    ImGui::SameLine(0, 18 * S);
    legendSwatch(kAdded, "Added");
    ImGui::SameLine(0, 10 * S);
    legendSwatch(kRemoved, "Removed");
    ImGui::SameLine(0, 10 * S);
    legendSwatch(kUnchanged, "Unchanged");
    dimText("Pick a file on the left, then step through its versions: each one is "
            "drawn against the version before it.");
    ImGui::Dummy(ImVec2(0, 2 * S));

    if (!H.itemsErr.empty()) { plmError(H.itemsErr); return; }
    if (H.itemsLoading && H.items.empty()) { plmSpinner("Loading"); return; }
    if (H.items.empty()) {
        plmEmptyState("[ ]", "Nothing to compare yet",
                      "Track a file on the Files page first.");
        return;
    }

    if (c.itemId.empty())
        c.itemId = !H.selectedItem.empty() ? H.selectedItem : H.items.front().itemId;

    const float railW = 248 * S;
    float bodyH = ImGui::GetContentRegionAvail().y;
    if (bodyH < 120 * S) bodyH = 120 * S;

    std::string picked = drawFileRail(st, railW, bodyH);
    if (!picked.empty() && picked != c.itemId) {
        c.itemId = picked;
        H.selectedItem = picked;
        // New file: no stale result, no stale textures, no stale pair.
        c.commitA.clear();
        c.commitB.clear();
        c.resultKey.clear();
        c.err.clear();
        c.errReason.clear();
        clearResult();
    }

    // Refetch the selected file's timeline on selection change or invalidation.
    if (!c.histLoading && (c.histItem != c.itemId || c.histGen != H.generation))
        fetchItemHistory(st, c.itemId);

    ImGui::SameLine(0, 8 * S);
    ImGui::BeginChild("##cmp_col", ImVec2(0, bodyH), ImGuiChildFlags_None);
    drawDiffColumn(st, findItem(c.itemId));
    ImGui::EndChild();
}

}  // namespace ui
