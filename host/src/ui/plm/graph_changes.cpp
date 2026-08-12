// PLM Graph — change diff + watch overlay (contracts/cad_graph.md, "Change diff").
//
// See graph_changes.h for the surface and, more importantly, for why the diff may only ever be
// user-initiated. Everything below follows graph_tab.cpp's async idiom: build a flat params object
// carrying both project_dir and path, st.rpc.call() it, and reconcile inside the completion, which
// AsyncRpc runs on the GL thread during drain().
#include "graph_changes.h"

#include <algorithm>
#include <cstdint>
#include <ctime>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#include "imgui.h"

#include "core/app_state.h"
#include "core/async_rpc.h"
#include "ui/anim.h"
#include "ui/metrics.h"
#include "ui/widgets/kit.h"

namespace ui {
namespace plmgraph {
namespace {

// ---- state ---------------------------------------------------------------------------------------

struct NodeRow {
    std::string id, kind, label, path;
    bool        watched = false;
};

struct EdgeRec {
    std::string src, dst, kind;
    Delta       d = Delta::None;
};

struct WatchEntry {
    std::string entryId;   // "w<8hex>" — the only handle cad_graph_watch_remove accepts
    std::string nodeId;    // set for a node watch
    std::string keyword;   // set for a keyword watch (mutually exclusive with nodeId)
    std::string note;
};

struct ChangesState {
    bool        active    = false;
    bool        available = true;   // cleared on unknown_method
    bool        busy      = false;
    bool        haveResult = false;
    std::string err, errHint;

    std::string projectDir;         // the project the current diff belongs to
    bool        linked = false;     // the linked-projects policy it was run with

    std::string generatedAt;        // worker's build stamp for the FRESH graph (ISO 8601)
    double      ranAt   = 0.0;      // unix seconds of the reply ("diffed 3 min ago")
    double      enterT0 = 0.0;      // stagger origin for the rail's entrance wave
    int         edgesAdded = 0, edgesRemoved = 0;

    std::vector<NodeRow> added, removed;
    std::unordered_map<std::string, Delta> nodeDeltas;
    // Edge verdicts. The canvas asks per edge per frame, so the query must not build a key string:
    // hash the three fields into a multimap and verify the survivors by comparison.
    std::vector<EdgeRec>                        edgeRecs;
    std::unordered_multimap<std::uint64_t, int> edgeIdx;

    // Watch overlay (.plm/graph_watch.json).
    std::string                     watchProject;   // project whose overlay is loaded ("" = none)
    bool                            watchBusy = false;
    std::string                     watchErr;
    std::vector<WatchEntry>         watch;
    std::unordered_set<std::string> watchedIds;     // node-id watches, for isWatched()
    std::string                     pendingRemoveNode;  // awaiting an entry-id resolution
};

ChangesState& S() {
    static ChangesState s;
    return s;
}

// Rows drawn per section. Beyond this the section reports the remainder instead of paying for
// thousands of rows — the diff data itself is never truncated.
constexpr int kRailRows = 200;
// Past this index every row shares the last slot's timing, so a long list still lands in kSlow
// rather than crawling in for half a minute.
constexpr int kStaggerCap = 12;
// Entrance offset: rows slide up into place.
constexpr float kEnterLift = 8.0f;

// ---- rpc helpers (mirrors graph_tab.cpp) ---------------------------------------------------------

std::string jstr(const nlohmann::json& j, const char* k) {
    auto it = j.find(k);
    if (it == j.end() || it->is_null()) return std::string();
    if (it->is_string()) return it->get<std::string>();
    return it->dump();
}

// An older services tree simply does not have these methods; that is a permanent, non-error
// condition and must disable the control rather than nag.
bool isAbsent(const core::RpcResult& r) {
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
        if (c == "unknown_method" || c == "method_not_found" || c == "not_implemented" ||
            c.find("unknown") != std::string::npos)
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

std::string failReason(const core::RpcResult& r) {
    if (!r.ok) return r.err.empty() ? std::string("the service did not respond") : r.err;
    if (r.result.is_object()) {
        const std::string reason = r.result.value("reason", std::string());
        if (!reason.empty()) return reason;
    }
    return std::string("that could not be done");
}

std::string failHint(const core::RpcResult& r) {
    if (r.result.is_object()) return r.result.value("hint", std::string());
    return std::string();
}

// ---- diff bookkeeping ----------------------------------------------------------------------------

void resetDiff(ChangesState& C) {
    C.haveResult = false;
    C.err.clear();
    C.errHint.clear();
    C.generatedAt.clear();
    C.ranAt        = 0.0;
    C.edgesAdded   = 0;
    C.edgesRemoved = 0;
    C.added.clear();
    C.removed.clear();
    C.nodeDeltas.clear();
    C.edgeRecs.clear();
    C.edgeIdx.clear();
}

std::uint64_t edgeHash(const std::string& a, const std::string& b, const std::string& c) {
    std::uint64_t h = 1469598103934665603ull;   // FNV-1a
    const auto mix = [&h](const std::string& s) {
        for (unsigned char ch : s) {
            h ^= (std::uint64_t)ch;
            h *= 1099511628211ull;
        }
        h ^= 0x1full;   // unit separator: "ab|c" must not collide with "a|bc"
        h *= 1099511628211ull;
    };
    mix(a);
    mix(b);
    mix(c);
    return h;
}

void parseNodes(const nlohmann::json& owner, const char* key, std::vector<NodeRow>& out) {
    auto it = owner.find(key);
    if (it == owner.end() || !it->is_array()) return;
    out.reserve(it->size());
    for (const auto& jn : *it) {
        if (!jn.is_object()) continue;
        NodeRow n;
        n.id = jstr(jn, "id");
        if (n.id.empty()) continue;
        n.kind    = jstr(jn, "kind");
        n.label   = jstr(jn, "label");
        n.path    = jstr(jn, "path");
        n.watched = jn.value("watched", false);
        out.push_back(std::move(n));
    }
}

void parseEdges(ChangesState& C, const nlohmann::json& owner, const char* key, Delta d, int& count) {
    auto it = owner.find(key);
    if (it == owner.end() || !it->is_array()) return;
    count = (int)it->size();
    C.edgeRecs.reserve(C.edgeRecs.size() + it->size());
    for (const auto& je : *it) {
        if (!je.is_object()) continue;
        EdgeRec e;
        e.src  = jstr(je, "src");
        e.dst  = jstr(je, "dst");
        e.kind = jstr(je, "kind");
        if (e.src.empty() || e.dst.empty()) continue;
        e.d = d;
        C.edgeRecs.push_back(std::move(e));
        const EdgeRec& rec = C.edgeRecs.back();
        C.edgeIdx.emplace(edgeHash(rec.src, rec.dst, rec.kind), (int)C.edgeRecs.size() - 1);
    }
}

// Watched outranks Added/Removed: a change to something the user asked about is the loudest signal.
void sortRows(std::vector<NodeRow>& rows) {
    std::sort(rows.begin(), rows.end(), [](const NodeRow& a, const NodeRow& b) {
        if (a.watched != b.watched) return a.watched;
        if (a.kind != b.kind) return a.kind < b.kind;
        const std::string& la = a.label.empty() ? a.id : a.label;
        const std::string& lb = b.label.empty() ? b.id : b.label;
        if (la != lb) return la < lb;
        return a.id < b.id;
    });
}

void applyDiff(ChangesState& C, const nlohmann::json& j) {
    resetDiff(C);
    parseNodes(j, "nodes_added", C.added);
    parseNodes(j, "nodes_removed", C.removed);
    parseEdges(C, j, "edges_added", Delta::Added, C.edgesAdded);
    parseEdges(C, j, "edges_removed", Delta::Removed, C.edgesRemoved);

    // `watched` is the worker's own summary — it resolves node-id AND keyword watches against node
    // metadata this TU does not hold, so it is authoritative over the per-node flags.
    std::unordered_set<std::string> watched;
    if (auto it = j.find("watched"); it != j.end() && it->is_array()) {
        for (const auto& jw : *it)
            if (jw.is_string()) watched.insert(jw.get<std::string>());
    }
    C.nodeDeltas.reserve(C.added.size() + C.removed.size());
    for (NodeRow& n : C.added) {
        if (watched.count(n.id)) n.watched = true;
        C.nodeDeltas[n.id] = n.watched ? Delta::Watched : Delta::Added;
    }
    for (NodeRow& n : C.removed) {
        if (watched.count(n.id)) n.watched = true;
        C.nodeDeltas[n.id] = n.watched ? Delta::Watched : Delta::Removed;
    }

    sortRows(C.added);
    sortRows(C.removed);

    C.generatedAt = jstr(j, "generated_at");
    C.ranAt       = (double)std::time(nullptr);
    C.enterT0     = ImGui::GetTime();
    C.haveResult  = true;
}

// ---- watch bookkeeping ---------------------------------------------------------------------------

void rebuildWatchedIds(ChangesState& C) {
    C.watchedIds.clear();
    for (const WatchEntry& e : C.watch)
        if (!e.nodeId.empty()) C.watchedIds.insert(e.nodeId);
}

void mergeEntry(ChangesState& C, const nlohmann::json& je) {
    if (!je.is_object()) return;
    WatchEntry e;
    e.entryId = jstr(je, "id");
    e.nodeId  = jstr(je, "node_id");
    e.keyword = jstr(je, "keyword");
    e.note    = jstr(je, "note");
    if (e.entryId.empty()) return;
    for (WatchEntry& cur : C.watch) {
        if (cur.entryId == e.entryId) {
            cur = e;
            rebuildWatchedIds(C);
            return;
        }
    }
    C.watch.push_back(std::move(e));
    rebuildWatchedIds(C);
}

void eraseEntry(ChangesState& C, const std::string& entryId) {
    C.watch.erase(std::remove_if(C.watch.begin(), C.watch.end(),
                                 [&entryId](const WatchEntry& e) { return e.entryId == entryId; }),
                  C.watch.end());
    rebuildWatchedIds(C);
}

std::string entryIdForNode(const ChangesState& C, const std::string& nodeId) {
    for (const WatchEntry& e : C.watch)
        if (e.nodeId == nodeId) return e.entryId;
    return std::string();
}

void watchRemoveEntry(AppState& st, const std::string& projectDir, const std::string& entryId);

// ---- presentation helpers -------------------------------------------------------------------------

// Node kind -> contracts/icons.md name. Same vocabulary the canvas placeholder tiles use, extended
// across every kind the graph can emit so a removed node is never a nameless generic page.
const char* kindIcon(const std::string& kind) {
    if (kind == "assembly") return "assembly";
    if (kind == "part") return "part";
    if (kind == "doc") return "new_doc";
    if (kind == "project") return "body_folder";
    if (kind == "body") return "body_folder";
    if (kind == "feature") return "ws_features";
    if (kind == "sketch") return "sketch_node";
    if (kind == "mate") return "mate";
    if (kind == "bom_item") return "bom";
    if (kind == "drawing") return "file_doc";
    if (kind == "sim_study") return "run_study";
    if (kind == "board") return "plan_board";
    if (kind == "firmware") return "file_code";
    if (kind == "equation") return "equations";
    if (kind == "keyword") return "milestone_flag";
    if (kind == "instance") return "insert_component";
    return "file_generic";
}

float enterAlpha(double t0, int index) {
    return anim::stagger(t0, index < kStaggerCap ? index : kStaggerCap, anim::kSlow);
}

// Scale every vertex's alpha in a just-submitted range. The kit paints with palette roles; fading
// the emitted geometry is how a row enters without any call site inventing a color.
void fadeVerts(ImDrawList* dl, int firstVtx, float a) {
    const float f = ImClamp(a, 0.0f, 1.0f);
    for (int i = firstVtx; i < dl->VtxBuffer.Size; ++i) {
        ImU32&      c  = dl->VtxBuffer.Data[i].col;
        const ImU32 al = (c >> IM_COL32_A_SHIFT) & 0xFFu;
        c = (c & ~IM_COL32_A_MASK) | ((ImU32)((float)al * f) << IM_COL32_A_SHIFT);
    }
}

// kit::chip's own geometry (kit.cpp chipGeom), so a reserved action lane matches the chip exactly.
float chipWidth(const char* label, bool dot) {
    const float padX = ui::metrics::U(1.5f);
    const float gap  = ui::metrics::U(1.0f);
    const float dotR = dot ? ui::metrics::U(0.75f) : 0.0f;
    return padX * 2.0f + ImGui::CalcTextSize(label ? label : "").x +
           (dot ? (dotR * 2.0f + gap) : 0.0f);
}

kit::Tone deltaTone(Delta d) {
    switch (d) {
        case Delta::Watched: return kit::Tone::Accent;
        case Delta::Added:   return kit::Tone::Ok;
        case Delta::Removed: return kit::Tone::Warn;
        default:             return kit::Tone::Neutral;
    }
}

// One diff row. `navigable` false = a removed node: there is nothing on the canvas to fly to, so it
// offers a copy-id action instead of activation. Returns the node id when the row was activated.
std::string diffRow(const NodeRow& n, Delta d, int index, double t0, bool navigable) {
    const char* chipLabel = n.watched ? "watched" : nullptr;

    std::string tip = n.id;
    if (!n.kind.empty()) tip += "\nkind: " + n.kind;
    if (!n.path.empty()) tip += "\n" + n.path;
    if (!navigable) tip += "\nno longer in the project — nothing to open";

    const std::string second = n.path.empty() ? n.id : n.path;

    kit::RowStyle rs;
    rs.icon      = kindIcon(n.kind);
    rs.iconTone  = deltaTone(n.watched ? Delta::Watched : d);
    rs.secondary = second.c_str();
    rs.meta      = n.kind.empty() ? nullptr : n.kind.c_str();
    rs.tooltip   = tip.c_str();

    const float btn    = ImGui::GetFrameHeight();
    const float btnGap = ui::metrics::U(0.5f);
    const float chipW  = chipLabel ? chipWidth(chipLabel, true) : 0.0f;
    const float copyW  = navigable ? 0.0f : btn;
    rs.actionsW        = chipW + copyW + ((chipW > 0.0f && copyW > 0.0f) ? btnGap : 0.0f);

    const float lineH = ImGui::GetTextLineHeight();
    const float rowH  = lineH * 2.0f + ui::metrics::U(0.5f) + kit::padY() * 2.0f;
    const float rowW  = ImGui::GetContentRegionAvail().x;
    const float a     = enterAlpha(t0, index);
    const float off   = (1.0f - a) * ui::metrics::px(kEnterLift);

    ImDrawList* dl   = ImGui::GetWindowDrawList();
    const int   vtx0 = dl->VtxBuffer.Size;

    const ImVec2 base = ImGui::GetCursorScreenPos();
    if (off > 0.0f) ImGui::SetCursorScreenPos(ImVec2(base.x, base.y - off));
    const ImVec2 p0 = ImGui::GetCursorScreenPos();

    ImGui::PushID(index);
    ImGui::SetNextItemAllowOverlap();   // the action lane is submitted ON TOP of the row's hit box
    const bool   activated = kit::row("chg", n.label.empty() ? n.id.c_str() : n.label.c_str(), rs);
    const ImVec2 after     = ImGui::GetCursorScreenPos();

    float       lx       = p0.x + rowW - kit::padX() - rs.actionsW;
    const bool  overLane = rs.actionsW > 0.0f &&
                          ImGui::IsMouseHoveringRect(ImVec2(lx, p0.y),
                                                     ImVec2(lx + rs.actionsW, p0.y + rowH), false);
    if (chipLabel) {
        ImGui::SetCursorScreenPos(ImVec2(lx, p0.y + (rowH - ImGui::GetTextLineHeight() -
                                                    ui::metrics::U(1.0f)) * 0.5f));
        kit::chip(chipLabel, kit::Tone::Accent, true, "You are watching this node");
        lx += chipW + btnGap;
    }
    if (!navigable) {
        ImGui::SetCursorScreenPos(ImVec2(lx, p0.y + (rowH - btn) * 0.5f));
        if (kit::iconButton("cp", "copy", "Copy this node id to the clipboard", true,
                            kit::Tone::Neutral, btn))
            ImGui::SetClipboardText(n.id.c_str());
    }
    ImGui::PopID();

    if (a < 1.0f) fadeVerts(dl, vtx0, a);
    ImGui::SetCursorScreenPos(ImVec2(after.x, after.y + off));

    return (activated && !overLane && navigable) ? n.id : std::string();
}

// Added / Removed section. Returns a clicked node id, else empty.
std::string diffSection(const char* title, const std::vector<NodeRow>& rows, Delta d, double t0,
                        int& staggerIndex, bool navigable) {
    kit::sectionHeader(title, nullptr, nullptr, (int)rows.size());
    std::string clicked;
    const int   shown = (int)rows.size() < kRailRows ? (int)rows.size() : kRailRows;
    for (int i = 0; i < shown; ++i) {
        const std::string hit = diffRow(rows[(size_t)i], d, staggerIndex++, t0, navigable);
        if (!hit.empty()) clicked = hit;
    }
    if ((int)rows.size() > shown) {
        const std::string more =
            "+ " + kit::fmtCount((long long)rows.size() - shown) + " more not listed";
        kit::dimText(more.c_str());
    }
    return clicked;
}

}  // namespace

// ---- diff surface ---------------------------------------------------------------------------------

bool changesActive() { return S().active; }
bool changesAvailable() { return S().available; }
bool changesBusy() { return S().busy; }
int  changesCount() { return (int)(S().added.size() + S().removed.size()); }

void toggleChanges(AppState& st, const std::string& projectDir, bool linked) {
    (void)st;   // deliberately no fetch here — see graph_changes.h, THE SCHEDULING RULE
    ChangesState& C = S();
    C.active        = !C.active;
    C.linked        = linked;
    if (!projectDir.empty() && projectDir != C.projectDir) {
        resetDiff(C);   // another project's verdicts are worse than none
        C.projectDir = projectDir;
    }
    if (C.active) C.enterT0 = ImGui::GetTime();   // the rail re-enters as a wave on every open
}

void requestChanges(AppState& st, const std::string& projectDir, bool linked) {
    ChangesState& C = S();
    if (!C.available || C.busy) return;
    if (projectDir.empty()) {
        C.err     = "no project folder is open";
        C.errHint = "Pick a project folder in the PLM hub first.";
        return;
    }
    if (projectDir != C.projectDir) resetDiff(C);
    C.projectDir = projectDir;
    C.linked     = linked;
    C.busy       = true;
    C.err.clear();
    C.errHint.clear();

    nlohmann::json p;
    p["project_dir"] = projectDir;
    p["path"]        = projectDir;   // path-style callers, exactly as requestGraph sends both
    p["linked"]      = linked;
    // This call rebuilds the whole graph before diffing it; the default 2-minute budget is not
    // enough headroom for a large project, and a timeout here reads as a bug rather than a wait.
    st.rpc.call(
        "cad_graph_changes", p,
        [](const core::RpcResult& r) {
            ChangesState& C = S();
            C.busy          = false;
            if (isAbsent(r)) {
                C.available = false;
                C.err.clear();
                C.errHint.clear();
                return;
            }
            if (!r.ok || !r.result.is_object() || !r.result.value("ok", true)) {
                C.err     = failReason(r);
                C.errHint = failHint(r);
                return;
            }
            applyDiff(C, r.result);
        },
        600000);
}

Delta nodeDelta(const std::string& id) {
    const ChangesState& C = S();
    if (!C.active || C.nodeDeltas.empty()) return Delta::None;
    const auto it = C.nodeDeltas.find(id);
    return it == C.nodeDeltas.end() ? Delta::None : it->second;
}

Delta edgeDelta(const std::string& src, const std::string& dst, const std::string& kind) {
    const ChangesState& C = S();
    if (!C.active || C.edgeIdx.empty()) return Delta::None;
    const auto range = C.edgeIdx.equal_range(edgeHash(src, dst, kind));
    for (auto it = range.first; it != range.second; ++it) {
        const EdgeRec& e = C.edgeRecs[(std::size_t)it->second];
        if (e.src == src && e.dst == dst && e.kind == kind) return e.d;
    }
    return Delta::None;
}

// ---- the rail ------------------------------------------------------------------------------------

std::string drawChangesRail(AppState& st) {
    ChangesState& C = S();
    std::string   clicked;

    const std::string cur = st.plmProjectPath();
    // A diff belongs to exactly one project. Self-heal instead of showing another one's verdicts.
    if (!cur.empty() && !C.projectDir.empty() && cur != C.projectDir) {
        resetDiff(C);
        C.projectDir = cur;
    }
    // One cheap overlay read per project so the Watched section is correct without the host having
    // to prime it. This is NOT the expensive call — it reads a small JSON file.
    if (!cur.empty() && C.watchProject != cur && !C.watchBusy) requestWatchList(st, cur);

    const std::string trailing = C.haveResult ? "diffed " + kit::fmtAgo(C.ranAt) : std::string();
    kit::sectionHeader("Changes", "history", trailing.empty() ? nullptr : trailing.c_str(), -1,
                       true);

    // Exactly ONE run control is on screen at a time: the spinner while working, "Diff again" once
    // there is something to replace, and otherwise the empty state's own action button below.
    if (C.busy) {
        kit::spinner("Rebuilding the graph to diff it");
        kit::dimText("Comparing the cached graph with a fresh rebuild of this project.");
    } else if (C.haveResult) {
        const bool can = C.available && !cur.empty();
        if (kit::secondaryButton("Diff again", can, can ? nullptr : "Open a project folder first."))
            requestChanges(st, cur, C.linked);
        kit::dimText("Rebuilds this project's graph to compare it with the cached one — seconds of "
                     "work on a large project, so it only ever runs when you ask.");
    }

    // Each branch here is a terminal state for the diff area; the watch section always follows.
    bool showDiff = false;
    if (cur.empty()) {
        kit::blockedState("No project folder",
                          "The change diff runs against a project folder, and none is open.",
                          "Pick one in the PLM hub, then come back.");
    } else if (!C.available) {
        kit::blockedState("Changes unavailable",
                          "The worker serving this project has no cad_graph_changes method.",
                          "Update the CAD services, then reopen the project.");
    } else if (!C.err.empty()) {
        // An error must never render as an empty rail.
        if (kit::blockedState("The diff did not run", C.err.c_str(),
                              C.errHint.empty() ? nullptr : C.errHint.c_str(), "Try again"))
            requestChanges(st, cur, C.linked);
    } else if (!C.haveResult) {
        if (!C.busy &&
            kit::emptyState("history", "Nothing compared yet",
                            "Run the diff to see which nodes and edges appeared or vanished since "
                            "the last cached build. It rebuilds this project's graph to compare "
                            "against, which is seconds of work on a large project.",
                            "Run the diff"))
            requestChanges(st, cur, C.linked);
    } else {
        showDiff = true;
    }

    if (showDiff) {
        const bool empty = C.added.empty() && C.removed.empty() && C.edgesAdded == 0 &&
                           C.edgesRemoved == 0;
        if (empty) {
            kit::emptyState("legend", "No changes",
                            "Nothing has changed since the last cached build of this project.");
        } else {
            if (C.edgesAdded != 0 || C.edgesRemoved != 0) {
                const std::string edges = "+" + kit::fmtCount(C.edgesAdded) + " / -" +
                                          kit::fmtCount(C.edgesRemoved);
                kit::valueChip("edges", edges.c_str(), kit::Tone::Neutral,
                               "Links added / removed. Edge changes show on the canvas, not as "
                               "rows: both of their endpoints are already listed or unchanged.");
            }
            int staggerIndex = 0;
            if (!C.added.empty()) {
                const std::string hit = diffSection("Added", C.added, Delta::Added, C.enterT0,
                                                    staggerIndex, true);
                if (!hit.empty()) clicked = hit;
            }
            if (!C.removed.empty()) {
                // Removed nodes are gone from the graph, so the rail is the ONLY place they can
                // appear at all.
                const std::string hit = diffSection("Removed", C.removed, Delta::Removed, C.enterT0,
                                                    staggerIndex, false);
                if (!hit.empty()) clicked = hit;
            }
        }
        if (!C.generatedAt.empty())
            kit::keyValueRow("graph built", C.generatedAt.c_str(), kit::Tone::Neutral, true);
    }

    // ---- watch overlay -----------------------------------------------------------------------
    kit::sectionHeader("Watched", "hide_show_eye", nullptr, (int)C.watch.size());
    kit::inlineStatus(C.watchErr.c_str(), kit::Tone::Err);
    if (C.watch.empty()) {
        if (C.watchBusy) {
            kit::spinner("Reading the watch list");
        } else {
            kit::emptyState("hide_show_eye", "Nothing watched",
                            "Select a node and choose Watch. The next diff tells you first when "
                            "anything you watch appears or vanishes.");
        }
    } else {
        const float btn = ImGui::GetFrameHeight();
        // The removal is DEFERRED: watchRemoveEntry erases from C.watch, which would invalidate the
        // entry reference and the loop bound mid-iteration.
        std::string removeEntry;
        for (std::size_t i = 0; i < C.watch.size(); ++i) {
            const WatchEntry& e     = C.watch[i];
            const bool        isKw  = e.nodeId.empty();
            const std::string title = isKw ? e.keyword : e.nodeId;
            const std::string second =
                e.note.empty() ? (isKw ? std::string("keyword — matches any node tagged with it")
                                       : std::string("node"))
                               : e.note;

            kit::RowStyle rs;
            // A node watch knows only the id — "circle" is the canvas's own node mark, so the row
            // does not claim a kind it cannot know.
            rs.icon      = isKw ? "milestone_flag" : "circle";
            rs.iconTone  = kit::Tone::Accent;
            rs.secondary = second.c_str();
            rs.tooltip   = isKw ? "Keyword watch. Matching nodes are flagged by the next diff."
                                : "Node watch. Click to centre the view on it.";
            rs.actionsW  = btn;

            const float  lineH = ImGui::GetTextLineHeight();
            const float  rowH  = lineH * 2.0f + ui::metrics::U(0.5f) + kit::padY() * 2.0f;
            const float  rowW  = ImGui::GetContentRegionAvail().x;
            const ImVec2 p0    = ImGui::GetCursorScreenPos();

            ImGui::PushID((int)i);
            ImGui::SetNextItemAllowOverlap();
            const bool   activated = kit::row("wch", title.c_str(), rs);
            const ImVec2 after     = ImGui::GetCursorScreenPos();

            const float lx = p0.x + rowW - kit::padX() - rs.actionsW;
            const bool  overLane =
                ImGui::IsMouseHoveringRect(ImVec2(lx, p0.y), ImVec2(lx + rs.actionsW, p0.y + rowH),
                                           false);
            ImGui::SetCursorScreenPos(ImVec2(lx, p0.y + (rowH - btn) * 0.5f));
            if (kit::iconButton("rm", "delete", "Stop watching this", true, kit::Tone::Neutral,
                                btn) &&
                !cur.empty())
                removeEntry = e.entryId;
            ImGui::PopID();
            ImGui::SetCursorScreenPos(after);

            if (activated && !overLane && !isKw) clicked = e.nodeId;
        }
        if (!removeEntry.empty()) watchRemoveEntry(st, cur, removeEntry);
    }

    return clicked;
}

// ---- watch overlay -------------------------------------------------------------------------------

void requestWatchList(AppState& st, const std::string& projectDir) {
    ChangesState& C = S();
    if (projectDir.empty() || C.watchBusy) return;
    C.watchBusy = true;
    // Claimed up front so the rail's lazy prime fires once per project, not once per frame.
    C.watchProject = projectDir;

    nlohmann::json p;
    p["project_dir"] = projectDir;
    p["path"]        = projectDir;
    AppState* stp    = &st;
    st.rpc.call("cad_graph_watch_list", p, [stp, projectDir](const core::RpcResult& r) {
        ChangesState& C = S();
        C.watchBusy     = false;
        if (isAbsent(r)) {
            C.watch.clear();
            C.watchedIds.clear();
            C.pendingRemoveNode.clear();
            C.watchErr = "this worker has no watch overlay";
            return;
        }
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", true)) {
            C.watchErr = failReason(r);
            C.pendingRemoveNode.clear();
            return;
        }
        C.watchErr.clear();
        C.watch.clear();
        if (auto it = r.result.find("watch"); it != r.result.end() && it->is_array()) {
            for (const auto& je : *it) {
                if (!je.is_object()) continue;
                WatchEntry e;
                e.entryId = jstr(je, "id");
                e.nodeId  = jstr(je, "node_id");
                e.keyword = jstr(je, "keyword");
                e.note    = jstr(je, "note");
                if (!e.entryId.empty()) C.watch.push_back(std::move(e));
            }
        }
        rebuildWatchedIds(C);

        // A removal that was waiting on an entry id can finish now.
        if (!C.pendingRemoveNode.empty()) {
            const std::string node = C.pendingRemoveNode;
            C.pendingRemoveNode.clear();
            watchRemove(*stp, projectDir, node);
        }
    });
}

bool isWatched(const std::string& id) {
    const ChangesState& C = S();
    return !C.watchedIds.empty() && C.watchedIds.count(id) != 0;
}

void watchAdd(AppState& st, const std::string& projectDir, const std::string& id) {
    ChangesState& C = S();
    if (projectDir.empty() || id.empty()) return;
    if (C.watchedIds.count(id)) return;   // already watched; watch_add would only dedupe
    C.watchedIds.insert(id);              // optimistic: the inspector's toggle is live this frame

    nlohmann::json p;
    p["project_dir"] = projectDir;
    p["path"]        = projectDir;
    p["node_id"]     = id;
    p["origin"]      = "manual";
    AppState* stp    = &st;
    st.rpc.call("cad_graph_watch_add", p, [stp, projectDir, id](const core::RpcResult& r) {
        ChangesState& C = S();
        if (isAbsent(r)) {
            C.watchedIds.erase(id);
            C.watchErr = "this worker has no watch overlay";
            return;
        }
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", true)) {
            C.watchedIds.erase(id);   // the optimistic flag was a lie — take it back
            C.watchErr = failReason(r);
            return;
        }
        C.watchErr.clear();
        // The reply carries the stored entry, so a later un-watch resolves without a round trip.
        if (auto e = r.result.find("entry"); e != r.result.end() && e->is_object())
            mergeEntry(C, *e);
        else
            requestWatchList(*stp, projectDir);
    });
}

void watchRemove(AppState& st, const std::string& projectDir, const std::string& id) {
    ChangesState& C = S();
    if (projectDir.empty() || id.empty()) return;
    const std::string entryId = entryIdForNode(C, id);
    if (entryId.empty()) {
        // cad_graph_watch_remove only takes an entry id. If the overlay is loaded and the node is
        // not in it, say so; otherwise fetch the overlay and finish the removal in its completion.
        if (C.watchProject == projectDir && !C.watchBusy) {
            C.watchErr = "\"" + id + "\" is not being watched";
            return;
        }
        C.pendingRemoveNode = id;
        requestWatchList(st, projectDir);
        return;
    }
    watchRemoveEntry(st, projectDir, entryId);
}

namespace {

void watchRemoveEntry(AppState& st, const std::string& projectDir, const std::string& entryId) {
    ChangesState& C = S();
    if (projectDir.empty() || entryId.empty()) return;
    eraseEntry(C, entryId);   // optimistic: the row and the inspector toggle update this frame

    nlohmann::json p;
    p["project_dir"] = projectDir;
    p["path"]        = projectDir;
    p["entry_id"]    = entryId;
    AppState* stp    = &st;
    st.rpc.call("cad_graph_watch_remove", p, [stp, projectDir](const core::RpcResult& r) {
        ChangesState& C = S();
        if (isAbsent(r)) {
            C.watchErr = "this worker has no watch overlay";
            return;
        }
        if (!r.ok || !r.result.is_object() || !r.result.value("ok", true)) {
            C.watchErr = failReason(r);
            requestWatchList(*stp, projectDir);   // the optimistic erase was wrong — resync
            return;
        }
        C.watchErr.clear();
    });
}

}  // namespace

}  // namespace plmgraph
}  // namespace ui
