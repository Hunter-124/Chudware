// Automated ImGui UI-correctness audit — see ui_audit.h for the detection model.
#include "ui_audit.h"

#include <imgui.h>
#include <imgui_internal.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace ui::audit {
namespace {

using json = nlohmann::json;

// ---- one frame of hook data ----

struct Rec {
    ImGuiWindow* win;
    ImGuiID id;
    ImRect rect;
    bool alt;       // ButtonBehavior() registration of an ID that skipped ItemAdd()
    bool allowDup;  // ImGuiItemFlags_AllowDuplicateId
    int labelOff;   // into Frame::labels (NUL-terminated), -1 = no ItemInfo()
    ImRect clip;
    bool enabled;
    ImGuiItemStatusFlags status = 0;
    ImRect overflowClip{};  // content or chrome bounds captured at submission, before clips change
    int nativeLabelOff = -1;  // first primitive label retains stable IDs behind semantic captions
};

struct KeyInfo {
    int first, last;  // Frame::recs indices
    int adds, alts;   // ItemAdd() / ButtonBehavior() registrations not flagged AllowDuplicateId
};

struct Frame {
    bool recording = false;
    std::vector<Rec> recs;
    std::unordered_map<std::uint64_t, KeyInfo> keys;  // (window ID, item ID)
    std::vector<std::uint64_t> dups;
    std::vector<int> overflowRecs;
    std::string labels;

    void clear() {
        recs.clear();
        keys.clear();
        dups.clear();
        overflowRecs.clear();
        labels.clear();
    }
};

// ---- ID-stack path resolution (ImGui's ID Stack Tool machinery, driven for a chosen ID) ----

enum class Target { RunConflict, RunOverflow, IdAudit };

struct PathQuery {
    ImGuiID id = 0;
    Target target = Target::RunConflict;
    int index = -1;  // entry in the run's conflicts / overflows, or IdAudit::pending
    int gen = 0;     // run generation the entry belongs to
    bool armed = false;
    int frames = 0;
};

// ---- a bounded audit (begin / active / lastResult) ----

struct Over {
    int rec = -1;  // representative entry index once reported
    int frames = 0, lastFrame = -1;
    ImGuiWindow* win = nullptr;
    ImGuiID id = 0;
    ImRect rect;
    ImRect clip;
    std::string label;
};

struct Run {
    bool active = false;
    int gen = 0;
    std::string name;
    AuditOptions opt;
    int frames = 0, totalFrames = 0, queries = 0;
    bool findingsFinal = false;
    double t0 = 0.0;
    std::unordered_map<std::uint64_t, int> conflictIdx;
    std::unordered_map<std::uint64_t, Over> overs;
    std::vector<std::uint64_t> overOrder;
    std::unordered_set<std::uint64_t> items;
    std::unordered_map<const ImGuiWindow*, unsigned> cls;  // window -> region of its root
    std::vector<const ImGuiWindow*> roots;
    json conflicts = json::array(), overflows = json::array();
};

// ---- whole-run conflict detection (CHUDWARE_IDAUDIT=1) ----

struct IdAudit {
    bool checked = false, on = false, finished = false;
    int conflicts = 0;
    std::unordered_set<std::uint64_t> seen;
    std::vector<json> pending;  // conflicts waiting for their ID-stack path before printing
    std::vector<bool> printed;
};

ImGuiContext* gHookCtx = nullptr;
Frame F;
Run R;
int gGen = 0;
json gLast = json::object();
json gObserved = json::array();
json gObservedWindows = json::array();
bool gObserve = false;
bool gObserveShot = false;
std::deque<PathQuery> gQueries;
IdAudit ID;

double nowS() {
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

std::uint64_t itemKey(ImGuiID win, ImGuiID id) { return (std::uint64_t(win) << 32) | id; }

std::string hexId(ImGuiID id) {
    char b[16];
    std::snprintf(b, sizeof(b), "0x%08X", id);
    return b;
}

json rectJson(const ImRect& r) {
    auto q = [](float v) { return std::round(v * 10.0f) / 10.0f; };
    return json::array({q(r.Min.x), q(r.Min.y), q(r.Max.x), q(r.Max.y)});
}

const char* recLabel(const Rec& r) { return r.labelOff >= 0 ? F.labels.c_str() + r.labelOff : ""; }

// The text ImGui displays for a label: everything before "##" (which also starts "###").
std::string visiblePart(const std::string& s) {
    const size_t p = s.find("##");
    return p == std::string::npos ? s : s.substr(0, p);
}

// A readable name for a finding: the item's own visible label, else the innermost ID-stack level
// with a visible part (PushID("Name") + InputText("##v") reads "Name"), else the raw label.
std::string displayLabel(const std::string& raw, const std::string& path) {
    std::string v = visiblePart(raw);
    if (!v.empty()) return v;
    size_t end = path.size();
    while (end > 0) {
        const size_t slash = path.rfind('/', end - 1);
        const size_t begin = slash == std::string::npos ? 0 : slash + 1;
        v = visiblePart(path.substr(begin, end - begin));
        if (!v.empty() && v != "???") return v;
        if (slash == std::string::npos) break;
        end = slash;
    }
    return raw;
}

unsigned classify(const ImGuiWindow* w) {
    const ImGuiWindow* root = w->RootWindow ? w->RootWindow : w;
    if (root->Flags & (ImGuiWindowFlags_Popup | ImGuiWindowFlags_Modal)) return RegionFloating;
    if (std::strncmp(root->Name, "FeatureManager##LeftPane", 24) == 0) return RegionLeftPane;
    if (std::strcmp(root->Name, "##RibbonBar") == 0) return RegionRibbon;
    if (std::strstr(root->Name, "###Viewport")) return RegionViewport;
    const ImGuiViewport* vp = ImGui::GetMainViewport();
    if (root->Size.x >= vp->Size.x * 0.9f && root->Size.y <= vp->Size.y * 0.15f) return RegionChrome;
    return RegionFloating;
}

bool runCovers(const ImGuiWindow* w) {
    auto it = R.cls.find(w);
    if (it == R.cls.end()) {
        it = R.cls.emplace(w, classify(w)).first;
        const ImGuiWindow* root = w->RootWindow ? w->RootWindow : w;
        if ((it->second & R.opt.regions) && std::find(R.roots.begin(), R.roots.end(), root) == R.roots.end())
            R.roots.push_back(root);
    }
    return (it->second & R.opt.regions) != 0;
}

// ---- hooks ----

// The window's own resize grips and borders sit on its edges by design.
bool isResizeHandle(ImGuiWindow* w, ImGuiID id) {
    for (int n = 0; n < 4; ++n)
        if (id == ImGui::GetWindowResizeCornerID(w, n) || id == ImGui::GetWindowResizeBorderID(w, (ImGuiDir)n))
            return true;
    return false;
}

// Scrollbars are window decorations: Begin() renders them under the host/ancestor clip before
// pushing InnerClipRect, whose right edge deliberately excludes the scrollbar gutter. Hash the
// renderer's root-window IDs without invoking GetID()/the ID-stack query hook.
bool isWindowScrollbar(const ImGuiWindow* w, ImGuiID id) {
    return (w->ScrollbarX && id == ImHashStr("#SCROLLX", 0, w->ID)) ||
           (w->ScrollbarY && id == ImHashStr("#SCROLLY", 0, w->ID));
}

// Item rect reaching right of what its renderer shows. ScrollMax.x is no content filter:
// ImGui sets it from the content width whether or not the window can scroll.
void checkOverflow(const ImGuiContext& g, ImGuiWindow* w, int idx) {
    if (w->Hidden || w->Appearing || (w->Flags & ImGuiWindowFlags_DockNodeHost)) return;
    Rec& rec = F.recs[idx];
    const ImRect& r = rec.rect;
    if (isWindowScrollbar(w, rec.id)) {
        rec.overflowClip = w->OuterRectClipped;
        rec.overflowClip.ClipWith(rec.clip);  // actual decoration clip, including child ancestors
        if (r.Max.y <= rec.overflowClip.Min.y || r.Min.y >= rec.overflowClip.Max.y) return;
        if (r.Max.x > rec.overflowClip.Max.x + 1.0f) F.overflowRecs.push_back(idx);
        return;
    }
    if (w->ScrollbarX || w->Scroll.x != 0.0f ||
        (w->Flags & ImGuiWindowFlags_AlwaysHorizontalScrollbar)) return;
    const ImRect& clip = w->InnerClipRect;
    rec.overflowClip = clip;
    if (r.Max.y <= clip.Min.y || r.Min.y >= clip.Max.y) return;  // scrolled out / title or menu bar
    if (w->ClipRect.Max.x < clip.Max.x - 1.0f) return;           // inside a narrower pushed clip region
    const float limit = std::max(clip.Max.x, w->WorkRect.Max.x + g.Style.ItemSpacing.x * 0.5f) + 1.0f;
    if (r.Max.x > limit && !isResizeHandle(w, rec.id)) F.overflowRecs.push_back(idx);
}

void onItemAdd(ImGuiContext* ctx, ImGuiID id, const ImRect& bb, const ImGuiLastItemData* item) {
    if (!F.recording || ctx != gHookCtx || id == 0) return;
    ImGuiContext& g = *ctx;
    ImGuiWindow* w = g.CurrentWindow;
    if (!w) return;
    const bool alt = item == nullptr;
    if (alt && id == w->ID) return;  // Begin() registering the window itself
    if (w->IsFallbackWindow) return;  // the implicit "Debug##Default" window's own decorations
    const ImGuiItemFlags flags = alt ? g.CurrentItemFlags : item->ItemFlags;
    const bool allowDup = (flags & ImGuiItemFlags_AllowDuplicateId) != 0;
    const int idx = (int)F.recs.size();
    F.recs.push_back({w, id, alt ? bb : item->Rect, alt, allowDup, -1,
                      w->ClipRect, (flags & ImGuiItemFlags_Disabled) == 0});
    const std::uint64_t k = itemKey(w->ID, id);
    KeyInfo& ki = F.keys.try_emplace(k, KeyInfo{idx, idx, 0, 0}).first->second;
    ki.last = idx;
    if (!allowDup && ++(alt ? ki.alts : ki.adds) == 2) F.dups.push_back(k);
    if (R.active && !alt && R.frames < R.opt.frames) checkOverflow(g, w, idx);
}

void onItemInfo(ImGuiContext* ctx, ImGuiID id, const char* label, ImGuiItemStatusFlags flags) {
    if (!F.recording || ctx != gHookCtx || !label) return;
    ImGuiWindow* w = ctx->CurrentWindow;
    if (!w) return;
    int idx = -1;
    if (id == 0) {
        // Status text is real rendered geometry, not an interactive ID. Observe it only
        // for the bounded shot snapshot; never register it in the duplicate-ID index.
        if (!gObserve || ctx->LastItemData.ID != 0 || F.recs.size() >= 4096) return;
        idx = (int)F.recs.size();
        F.recs.push_back({w, 0, ctx->LastItemData.Rect, false, false, -1, w->ClipRect,
                          (ctx->LastItemData.ItemFlags & ImGuiItemFlags_Disabled) == 0});
    } else if (!F.recs.empty() && F.recs.back().id == id && F.recs.back().win == w) {
        idx = (int)F.recs.size() - 1;
    } else {
        auto it = F.keys.find(itemKey(w->ID, id));
        if (it != F.keys.end()) idx = it->second.last;
    }
    if (idx < 0) return;
    F.recs[idx].status = flags;  // The final semantic annotation owns the actual disclosure state.
    if (F.recs[idx].labelOff >= 0 && std::strcmp(recLabel(F.recs[idx]), label) == 0) return;
    // A kit's explicit semantic annotation follows the primitive's internal ID label.
    // Keep that final label, including card/footer IDs without an invisible "##" prefix.
    F.recs[idx].labelOff = (int)F.labels.size();
    if (F.recs[idx].nativeLabelOff < 0) F.recs[idx].nativeLabelOff = F.recs[idx].labelOff;
    F.labels.append(label);
    F.labels.push_back('\0');
}

const char* findItemLabel(const ImGuiContext* ctx, ImGuiID id) {
    if (ctx != gHookCtx) return nullptr;
    for (size_t i = F.recs.size(); i-- > 0;)
        if (F.recs[i].id == id && F.recs[i].labelOff >= 0) return recLabel(F.recs[i]);
    return nullptr;
}

// ---- findings ----

void queuePath(ImGuiID id, Target target, int index) {
    gQueries.push_back({id, target, index, R.gen, false, 0});
}

void printIdAudit(size_t i) {
    if (ID.printed[i]) return;
    ID.printed[i] = true;
    const json& e = ID.pending[i];
    std::printf("idaudit: conflict window=%s label=%s id=%s count=%d path=%s\n",
                e.value("window", std::string()).c_str(), e.value("label", std::string()).c_str(),
                e.value("id", std::string()).c_str(), e.value("count", 0),
                e["id_path"].is_string() ? e["id_path"].get<std::string>().c_str() : "?");
    std::fflush(stdout);
}

void applyPath(const PathQuery& q, const std::string& path) {
    json* e = nullptr;
    if (q.target == Target::IdAudit) {
        if (q.index < 0 || q.index >= (int)ID.pending.size()) return;
        e = &ID.pending[q.index];
    } else {
        if (!R.active || q.gen != R.gen) return;
        json& list = q.target == Target::RunConflict ? R.conflicts : R.overflows;
        if (q.index < 0 || q.index >= (int)list.size()) return;
        e = &list[q.index];
    }
    if (!path.empty()) {
        (*e)["id_path"] = path;
        (*e)["label"] = displayLabel(e->value("item_label", std::string()), path);
    }
    if (q.target == Target::IdAudit) printIdAudit(q.index);
}

// EndChild() ItemAdd()s the child into its parent under ChildId on every BeginChild()/EndChild()
// pair, so appending to a child twice in a frame (legal) and a multiline InputText (its own
// ItemAdd() plus its child's) both register that ID twice.
bool isChildItem(const ImGuiWindow* parent, ImGuiID id) {
    for (const ImGuiWindow* c : parent->DC.ChildWindows)
        if (c->ChildId == id) return true;
    return false;
}

void recordConflicts() {
    std::sort(F.dups.begin(), F.dups.end());
    F.dups.erase(std::unique(F.dups.begin(), F.dups.end()), F.dups.end());
    for (const std::uint64_t k : F.dups) {
        const KeyInfo& ki = F.keys[k];
        const Rec& head = F.recs[ki.first];
        if (isChildItem(head.win, head.id)) continue;
        const int count = std::max(ki.adds, ki.alts);
        json items = json::array();
        std::string raw;
        for (int i = ki.first; i <= ki.last; ++i) {
            const Rec& r = F.recs[i];
            if (r.win != head.win || r.id != head.id || r.allowDup) continue;
            items.push_back({{"window", r.win->Name}, {"rect", rectJson(r.rect)}, {"label", recLabel(r)}});
            if (raw.empty()) raw = recLabel(r);
        }
        json e = {{"window", head.win->Name},
                  {"root_window", head.win->RootWindow ? head.win->RootWindow->Name : head.win->Name},
                  {"id", hexId(head.id)},
                  {"count", count},
                  {"label", displayLabel(raw, "")},
                  {"item_label", raw},
                  {"id_path", nullptr},
                  {"frames", 1},
                  {"items", items}};
        if (R.active && runCovers(head.win)) {
            auto [it, fresh] = R.conflictIdx.try_emplace(k, (int)R.conflicts.size());
            if (fresh) {
                R.conflicts.push_back(e);
                if (R.queries < R.opt.maxPathQueries) {
                    ++R.queries;
                    queuePath(head.id, Target::RunConflict, it->second);
                }
            } else {
                json& c = R.conflicts[it->second];
                c["frames"] = c.value("frames", 0) + 1;
                if (count > c.value("count", 0)) {
                    c["count"] = count;
                    c["items"] = std::move(items);
                }
            }
        }
        if (ID.on && !ID.finished && ID.seen.insert(k).second) {
            ++ID.conflicts;
            ID.pending.push_back(std::move(e));
            ID.printed.push_back(false);
            queuePath(head.id, Target::IdAudit, (int)ID.pending.size() - 1);
        }
    }
}

void recordRunFrame() {
    ++R.frames;
    for (const Rec& r : F.recs)
        if (runCovers(r.win)) R.items.insert(itemKey(r.win->ID, r.id));
    for (const int idx : F.overflowRecs) {
        const Rec& r = F.recs[idx];
        if (!runCovers(r.win)) continue;
        const std::uint64_t k = itemKey(r.win->ID, r.id);
        auto [it, fresh] = R.overs.try_emplace(k);
        Over& o = it->second;
        if (fresh) R.overOrder.push_back(k);
        if (o.lastFrame == R.frames) continue;  // the same ID twice in a frame (a conflict) counts once
        o.lastFrame = R.frames;
        ++o.frames;
        o.win = r.win;
        o.id = r.id;
        o.rect = r.rect;
        o.clip = r.overflowClip;
        if (r.labelOff >= 0) o.label = recLabel(r);
    }
}

// Overflows persisting through at least two recorded frames become findings.
void finalizeOverflows(bool withQueries) {
    R.findingsFinal = true;
    const int need = std::min(2, std::max(1, R.opt.frames));
    for (const std::uint64_t k : R.overOrder) {
        Over& o = R.overs[k];
        if (o.frames < need) continue;
        const float over = o.rect.Max.x - o.clip.Max.x;
        o.rec = (int)R.overflows.size();
        R.overflows.push_back({{"window", o.win->Name},
                               {"root_window", o.win->RootWindow ? o.win->RootWindow->Name : o.win->Name},
                               {"id", hexId(o.id)},
                               {"rect", rectJson(o.rect)},
                               {"clip_x", json::array({o.clip.Min.x, o.clip.Max.x})},
                               {"overflow_px", std::round(over)},
                               {"label", displayLabel(o.label, "")},
                               {"item_label", o.label},
                               {"id_path", nullptr},
                               {"frames", o.frames}});
        if (withQueries && R.queries < R.opt.maxPathQueries) {
            ++R.queries;
            queuePath(o.id, Target::RunOverflow, o.rec);
        }
    }
}

void resetStackTool(ImGuiContext& g) {
    g.DebugIDStackTool.QueryId = 0;
    g.DebugIDStackTool.StackLevel = -1;
    g.DebugIDStackTool.Results.resize(0);
    g.DebugHookIdInfo = 0;
}

void dropRunQueries(ImGuiContext& g) {
    for (auto it = gQueries.begin(); it != gQueries.end();) {
        if (it->target == Target::IdAudit) {
            ++it;
            continue;
        }
        if (it == gQueries.begin() && it->armed) resetStackTool(g);
        it = gQueries.erase(it);
    }
}

bool runHasQueries() {
    for (const PathQuery& q : gQueries)
        if (q.target != Target::IdAudit && q.gen == R.gen) return true;
    return false;
}

void finishRun(ImGuiContext& g, bool truncated) {
    if (!R.findingsFinal) finalizeOverflows(false);
    dropRunQueries(g);
    for (const json& c : R.conflicts) {
        std::printf("ui_audit: [%s] ID conflict %s x%d label='%s' path=%s window='%s'\n", R.name.c_str(),
                    c.value("id", std::string()).c_str(), c.value("count", 0),
                    c.value("label", std::string()).c_str(),
                    c["id_path"].is_string() ? c["id_path"].get<std::string>().c_str() : "?",
                    c.value("window", std::string()).c_str());
    }
    for (const json& o : R.overflows) {
        std::printf("ui_audit: [%s] clipped item %s label='%s' window='%s' overflow=%.0f px\n", R.name.c_str(),
                    o.value("id", std::string()).c_str(), o.value("label", std::string()).c_str(),
                    o.value("window", std::string()).c_str(), o.value("overflow_px", 0.0));
    }
    std::fflush(stdout);
    json windows = json::array();
    for (const ImGuiWindow* w : R.roots) windows.push_back(w->Name);
    gLast = {{"name", R.name},
             {"conflicts", R.conflicts},
             {"overflows", R.overflows},
             {"frames", R.totalFrames},
             {"recorded_frames", R.frames},
             {"seconds", std::round((nowS() - R.t0) * 100.0) / 100.0},
             {"items_seen", R.items.size()},
             {"windows_seen", R.roots.size()},
             {"windows", std::move(windows)},
             {"regions", R.opt.regions},
             {"truncated", truncated}};
    R = Run();
}

// ---- ID-stack path resolution, one query at a time, one stack level per frame ----

// NewFramePost (after ImGui's own UpdateDebugToolStackQueries() cleared the hook): point
// DebugHookIdInfo at the ID (stack capture) or at the stack level being described.
void armQuery(ImGuiContext& g) {
    if (gQueries.empty()) return;
    ImGuiIDStackTool& tool = g.DebugIDStackTool;
    if (tool.LastActiveFrame + 2 >= g.FrameCount) return;  // the ID Stack Tool window owns the hook
    PathQuery& q = gQueries.front();
    if (!q.armed) {
        resetStackTool(g);
        tool.QueryId = q.id;
        q.armed = true;
    }
    ++q.frames;
    if (tool.StackLevel == -1) {
        g.DebugHookIdInfo = q.id;
    } else if (tool.StackLevel < tool.Results.Size) {
        ImGuiStackLevelInfo& info = tool.Results[tool.StackLevel];
        if (info.QueryFrameCount < 100) ++info.QueryFrameCount;
        g.DebugHookIdInfo = info.ID;
    }
}

std::string levelDesc(const ImGuiStackLevelInfo& info, int n) {
    if (n == 0 && info.Desc[0] == 0)
        if (const ImGuiWindow* w = ImGui::FindWindowByID(info.ID)) return w->Name;
    if (info.QuerySuccess) return info.Desc;
    if (const char* l = ImGuiTestEngine_FindItemDebugLabel(gHookCtx, info.ID); l && *l) return l;
    return "???";
}

// EndFramePost: collect what DebugHookIdInfo() wrote this frame and advance.
void stepQuery(ImGuiContext& g) {
    if (gQueries.empty() || !gQueries.front().armed) return;
    PathQuery& q = gQueries.front();
    ImGuiIDStackTool& tool = g.DebugIDStackTool;
    bool done = false;
    if (tool.StackLevel == -1) {
        done = q.frames >= 3;  // the ID was not hashed again (the item is gone)
    } else {
        const int level = tool.StackLevel;
        if (level < tool.Results.Size &&
            (tool.Results[level].QuerySuccess || tool.Results[level].QueryFrameCount > 2))
            ++tool.StackLevel;
        // Level 0 is the window: its ID never goes through GetID(), the window name describes it.
        if (tool.StackLevel == 0 && tool.Results.Size > 0 && ImGui::FindWindowByID(tool.Results[0].ID))
            ++tool.StackLevel;
        done = tool.StackLevel >= tool.Results.Size || q.frames > 3 * (tool.Results.Size + 2);
    }
    if (!done) return;
    std::string path;
    for (int n = 0; n < tool.Results.Size; ++n) path += (n ? "/" : "") + levelDesc(tool.Results[n], n);
    const PathQuery finished = q;
    gQueries.pop_front();
    resetStackTool(g);
    applyPath(finished, path);
}

// ---- context hooks ----

void onNewFramePre(ImGuiContext* ctx, ImGuiContextHook*) {
    F.clear();
    if (gObserve && !gObserveShot && !R.active) gObserve = false;
    F.recording = gObserve || R.active || (ID.on && !ID.finished);
    ctx->TestEngineHookItems = F.recording;
}

void onNewFramePost(ImGuiContext* ctx, ImGuiContextHook*) { armQuery(*ctx); }

void onEndFramePost(ImGuiContext* ctx, ImGuiContextHook*) {
    ImGuiContext& g = *ctx;
    if (F.recording) {
        if (gObserve) {
            gObserved = json::array();
            gObservedWindows = json::array();
            for (ImGuiWindow* w : g.Windows) {
                if (gObservedWindows.size() >= 256) break;
                if (!w->Active || w->Hidden) continue;
                ImRect clip = w->InnerClipRect;
                clip.ClipWith(w->ClipRect);
                if (clip.GetWidth() <= 0.0f || clip.GetHeight() <= 0.0f) continue;
                ImVec2 wheelPoint = clip.GetCenter();
                if (w->ScrollbarY) {
                    const ImVec2 scrollbarPoint(w->InnerRect.Max.x + w->ScrollbarSizes.x * 0.5f,
                                                wheelPoint.y);
                    if (w->OuterRectClipped.Contains(scrollbarPoint)) wheelPoint = scrollbarPoint;
                }
                else if (w->ScrollMax.y > 0.0f) {
                    // NoScrollbar bodies may draw their own gutter. Hover inside the outer
                    // content edge, not the center where an independently scrolling editor sits.
                    const ImVec2 edgePoint(clip.Max.x - 0.5f, wheelPoint.y);
                    if (w->OuterRectClipped.Contains(edgePoint)) wheelPoint = edgePoint;
                }
                gObservedWindows.push_back({{"id", w->ID}, {"window", w->Name},
                    {"parent", w->ParentWindow ? w->ParentWindow->Name : ""},
                    {"rect", rectJson(clip)}, {"wheel_point", {wheelPoint.x, wheelPoint.y}},
                    {"scroll_y", w->Scroll.y},
                    {"scroll_max_y", w->ScrollMax.y},
                    // Same displacement used by ImGui::UpdateMouseWheel, in screen pixels.
                    {"wheel_step_y", ImTrunc(ImMin(5.0f * w->CalcFontSize(), w->InnerRect.GetHeight() * 0.67f))},
                    {"wheel_enabled", (w->Flags & ImGuiWindowFlags_NoScrollWithMouse) == 0}});
            }
            for (const Rec& r : F.recs) {
                if (gObserved.size() >= 4096) break;
                if (r.alt || r.win->Hidden || r.labelOff < 0) continue;
                ImRect visible = r.rect;
                visible.ClipWith(r.clip);
                const bool shown = visible.GetWidth() > 0.0f && visible.GetHeight() > 0.0f;
                gObserved.push_back({{"id", r.id}, {"label", recLabel(r)},
                                     {"native_label", r.nativeLabelOff < 0 ? recLabel(r) :
                                                      F.labels.c_str() + r.nativeLabelOff},
                                     {"window", r.win->Name}, {"visible", shown}, {"enabled", r.enabled},
                                     {"text_only", r.id == 0},
                                     {"rect", rectJson(shown ? visible : r.rect)},
                                     {"full_rect", rectJson(r.rect)}, {"clip", rectJson(r.clip)},
                                     {"openable", (r.status & ImGuiItemStatusFlags_Openable) != 0},
                                     {"opened", (r.status & ImGuiItemStatusFlags_Opened) != 0}});
            }
        }
        recordConflicts();
        if (R.active) {
            ++R.totalFrames;
            if (R.frames < R.opt.frames) recordRunFrame();
        }
    }
    F.recording = false;
    stepQuery(g);
    if (!R.active) return;
    if (!R.findingsFinal && R.frames >= R.opt.frames) finalizeOverflows(true);
    const bool capped = R.opt.maxFrames > 0 && R.totalFrames >= R.opt.maxFrames;
    if (R.findingsFinal && !runHasQueries()) finishRun(g, false);
    else if (capped) finishRun(g, true);
}

void onShutdown(ImGuiContext* ctx, ImGuiContextHook*) {
    if (ctx != gHookCtx) return;
    ctx->TestEngineHookItems = false;
    gHookCtx = nullptr;
    gObserve = false;
    gObserveShot = false;
    gObserved = json::array();
    gObservedWindows = json::array();
    if (idAuditFinish() > 0) {
        std::fflush(nullptr);
        std::_Exit(EXIT_FAILURE);
    }
}

void installHooks(ImGuiContext* ctx) {
    if (gHookCtx == ctx) return;
    const struct {
        ImGuiContextHookType type;
        ImGuiContextHookCallback cb;
    } hooks[] = {{ImGuiContextHookType_NewFramePre, onNewFramePre},
                 {ImGuiContextHookType_NewFramePost, onNewFramePost},
                 {ImGuiContextHookType_EndFramePost, onEndFramePost},
                 {ImGuiContextHookType_Shutdown, onShutdown}};
    for (const auto& h : hooks) {
        ImGuiContextHook hook;
        hook.Type = h.type;
        hook.Callback = h.cb;
        ImGui::AddContextHook(ctx, &hook);
    }
    gHookCtx = ctx;
}

}  // namespace

bool begin(const std::string& name, const AuditOptions& opt) {
    ImGuiContext* ctx = ImGui::GetCurrentContext();
    if (!ctx || R.active) return false;
    installHooks(ctx);
    R = Run();
    R.gen = ++gGen;
    R.name = name;
    R.opt = opt;
    R.opt.frames = std::max(1, opt.frames);
    R.t0 = nowS();
    R.active = true;
    return true;
}

bool active() { return R.active; }

const nlohmann::json& lastResult() { return gLast; }

const nlohmann::json& observedItems() {
    ImGuiContext* ctx = ImGui::GetCurrentContext();
    const char* shot = std::getenv("CHUDWARE_SHOT");
    const bool pointerShot = shot && (std::strcmp(shot, "siminteractive") == 0 ||
                                      std::strcmp(shot, "simaudit") == 0 ||
                                      std::strcmp(shot, "pipedraw") == 0 ||
                                      std::strcmp(shot, "hydraulics3d") == 0 ||
                                      std::strcmp(shot, "dwgopen") == 0);
    if (ctx && (R.active || pointerShot)) {
        installHooks(ctx);
        gObserve = true;
        gObserveShot = pointerShot;
    }
    return gObserved;
}

const nlohmann::json& observedWindows() {
    observedItems();  // The same shot/audit-only opt-in; no ordinary-frame collection.
    return gObservedWindows;
}

void idAuditAutoStart() {
    if (ID.checked) return;
    ImGuiContext* ctx = ImGui::GetCurrentContext();
    if (!ctx) return;
    ID.checked = true;
    const char* on = std::getenv("CHUDWARE_IDAUDIT");
    const char* shot = std::getenv("CHUDWARE_SHOT");
    if (!on || !*on || std::strcmp(on, "0") == 0 || !shot || !*shot) return;
    installHooks(ctx);
    ID.on = true;
    std::printf("idaudit: enabled (scenario=%s)\n", shot);
    std::fflush(stdout);
}

int idAuditFinish() {
    if (!ID.on) return 0;
    if (!ID.finished) {
        ID.finished = true;
        for (size_t i = 0; i < ID.pending.size(); ++i) printIdAudit(i);  // still waiting for a path
        std::printf("idaudit: done conflicts=%d\n", ID.conflicts);
        std::fflush(stdout);
    }
    return ID.conflicts;
}

}  // namespace ui::audit

// ---- test-engine hook definitions (global namespace, called from the imgui sources) ----

void ImGuiTestEngineHook_ItemAdd(ImGuiContext* ctx, ImGuiID id, const ImRect& bb, const ImGuiLastItemData* item_data) {
    ui::audit::onItemAdd(ctx, id, bb, item_data);
}

void ImGuiTestEngineHook_ItemInfo(ImGuiContext* ctx, ImGuiID id, const char* label, ImGuiItemStatusFlags flags) {
    ui::audit::onItemInfo(ctx, id, label, flags);
}

// Only reached with ImGuiDebugLogFlags_OutputToTestEngine set, which nothing here enables.
void ImGuiTestEngineHook_Log(ImGuiContext*, const char*, ...) {}

// The ItemInfo() label of `id` in the current frame (the ID Stack Tool's fallback description).
const char* ImGuiTestEngine_FindItemDebugLabel(ImGuiContext* ctx, ImGuiID id) {
    return ui::audit::findItemLabel(ctx, id);
}
