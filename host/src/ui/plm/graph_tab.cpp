// PLM Project Hub — Graph tab body (contracts/cad_graph.md).
//
// Project-view + document drill-down scopes over cad_graph_get. Interactive
// force-directed layout (ui::ForceGraph, graph_force.h) animates the visible
// subgraph; project view aggregates cross-doc edges whose endpoints are
// hidden internals onto owning docs. Nodes are draggable with live physics;
// the sim freezes once settled so idle frames are near-free.
// Pan/zoom, hover tooltip, click → side panel + blast radius, search filter,
// refresh + auto-refresh on OS app focus regain. Degrades: unknown_method
// permanently hides the tab via plmGraphAvailable().
#include "graph_tab.h"

#include "graph_force.h"
#include "plm_hub.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "imgui.h"

#include "core/app_state.h"
#include "core/async_rpc.h"
#include "ui/icons.h"
#include "ui_theme.h"

namespace ui {
namespace {

// ---- row shapes --------------------------------------------------------------

struct GNode {
    std::string id, kind, label, path, docId, origin;
    std::string metaSummary;   // short key=val dump for tooltip/panel
    float lx = 0.0f, ly = 0.0f;  // layout coords (graph space)
    float radius = 14.0f;
};

struct GEdge {
    std::string src, dst, kind, origin;
    std::string metaSummary;   // short key=val dump for label/panel
    std::string dimLabel;      // compact value label for dimensional edges
    nlohmann::json meta;       // full meta for the side panel
    int aggCount = 1;          // >1 when project-view edge is an aggregation
    int srcEdgeIdx = -1;       // original edges[] index (representative for panel)
};

// Drawn edge after scope filtering / project-view aggregation.
struct VEdge {
    std::string src, dst, kind, origin;
    std::string dimLabel;
    int aggCount = 1;
    int srcEdgeIdx = -1;       // index into GraphTabState::edges
};

// Per structural node: hidden descendant tallies (features / sketches / other).
struct DescCounts {
    int features = 0;
    int sketches = 0;
    int other = 0;
    int total() const { return features + sketches + other; }
};

struct GraphTabState {
    bool available = true;     // false after permanent unknown_method
    bool loading = false;
    bool blastLoading = false;
    bool layoutDirty = true;
    bool centerPending = false;
    std::string projectDir;
    std::string err;
    std::string generatedAt;
    std::vector<GNode> nodes;
    std::vector<GEdge> edges;
    std::unordered_set<std::string> blast;  // highlighted node ids
    std::string selectedId;
    std::string hoverId;
    char search[128] = {};
    float panX = 0.0f, panY = 0.0f, zoom = 1.0f;
    bool wasAppFocusLost = false;
    double lastFetch = -1e9;
    int layoutToken = 0;  // bumps when nodes/edges replace → re-layout
    // edge kind filter — hidden kinds skip drawing (all visible by default)
    std::unordered_set<std::string> hiddenKinds;
    // edge selection indexes into visibleEdges (not the full edges vector)
    int selectedEdge = -1;
    // link authoring
    std::string linkArmSrc;   // node id armed as the link source
    bool linkPopupOpen = false;
    std::string linkPopupDst; // node id chosen as the target
    int linkKindIdx = 4;      // default REFERENCES (see kEdgeKinds)
    char linkNote[192] = {};
    std::string focusPending; // plmGraphFocus request to apply on next draw
    // ME489-scale guards ----------------------------------------------------
    bool hideArchive = true;
    bool truncated = false;
    int nodeCountFull = 0;   // full graph size (pre-cap)
    int edgeCountFull = 0;
    bool listView = false;
    bool listViewUserOverride = false;  // true once the user picks a mode

    // Scope: "" = project view; else the structural doc/asm/part node drilled into.
    std::string scopeDocId;
    // Visible node indices (into nodes[]) for the active scope.
    std::vector<int> visibleIdx;
    // Scope-filtered / aggregated edges for drawing + hit-test.
    std::vector<VEdge> visibleEdges;
    // Hidden-descendant badges keyed by structural node id (project view).
    std::unordered_map<std::string, DescCounts> descCounts;
    // id → nodes[] index (rebuilt with data).
    std::unordered_map<std::string, int> idIndex;
    // CONTAINS parent: child id → parent id (first parent wins; rebuilt with data).
    std::unordered_map<std::string, std::string> containsParent;

    // Force-directed layout (graph_force.h). forceSlot maps nodes[] index →
    // sim node index (-1 when not in the sim); GNode::lx/ly stay the render
    // cache. The sim is rebuilt when layoutDirty, stepped until settled.
    ui::ForceGraph force;
    std::vector<int> forceSlot;
    int dragNode = -1;        // nodes[] index currently physics-dragged
    int dragCandidate = -1;   // nodes[] index pressed, awaiting drag threshold
    bool fitOnSettle = false; // fit once when a fresh layout first settles
    // Search match cache (rebuilt when the query or visible set changes).
    std::unordered_set<std::string> matchIds;
    int visMatches = 0;
    int allMatches = 0;
    bool matchDirty = true;

    // Change probe: fingerprint from the last full cad_graph_get response.
    // Polls echo it via if_mtimes_max/if_sources_count; "unchanged" replies
    // skip the payload entirely (no re-parse, no re-layout, no view reset).
    double mtimesMax = 0.0;
    int sourcesCount = 0;
    // Settled-layout cache (solve state) — persisted to
    // <project>/.plm/graph_layout.json so reopening starts from the last
    // relaxed arrangement instead of re-solving from scratch.
    std::unordered_map<std::string, std::pair<float, float>> layoutCache;
    bool layoutCacheLoaded = false;
    double lastLayoutSave = -1e9;
    bool wasSettled = true;
    // Once the user pans/zooms, never auto-fit again until scope/project change.
    bool userNavigated = false;
};

// List view auto-threshold (user can still force Graph view).
static constexpr int kListViewThreshold = 2000;

GraphTabState& S() {
    static GraphTabState s;
    return s;
}

// ---- helpers -----------------------------------------------------------------

bool isAbsent(const core::RpcResult& r) {
    if (!r.ok) {
        const std::string& e = r.err;
        if (e.find("unknown") != std::string::npos ||
            e.find("not found") != std::string::npos ||
            e.find("not_found") != std::string::npos ||
            e.find("method") != std::string::npos)
            return true;
        return false;
    }
    if (!r.result.is_object()) return false;
    if (r.result.contains("code")) {
        const std::string c = r.result.value("code", std::string());
        if (c == "unknown_method" || c == "method_not_found" ||
            c == "not_implemented" || c == "unknown" ||
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

std::string jstr(const nlohmann::json& j, const char* k) {
    auto it = j.find(k);
    if (it == j.end() || it->is_null()) return "";
    if (it->is_string()) return it->get<std::string>();
    return it->dump();
}

std::string summarizeMeta(const nlohmann::json& meta) {
    if (!meta.is_object() || meta.empty()) return "";
    std::string out;
    int n = 0;
    for (auto it = meta.begin(); it != meta.end() && n < 6; ++it, ++n) {
        if (!out.empty()) out += " · ";
        out += it.key();
        out += "=";
        if (it.value().is_string()) out += it.value().get<std::string>();
        else if (it.value().is_number_integer()) out += std::to_string(it.value().get<long long>());
        else if (it.value().is_number_float()) {
            char b[32];
            std::snprintf(b, sizeof(b), "%.4g", it.value().get<double>());
            out += b;
        } else if (it.value().is_boolean()) out += it.value().get<bool>() ? "true" : "false";
        else out += it.value().dump();
    }
    if ((int)meta.size() > n) out += " …";
    return out;
}

// Structural kinds shown in project view (everything else is drill-down only).
bool isProjectKind(const std::string& k) {
    return k == "project" || k == "assembly" || k == "part" || k == "doc" ||
           k == "drawing" || k == "board" || k == "firmware" || k == "sim_study";
}

// Kinds a user can double-click to drill into.
bool isDrillableKind(const std::string& k) {
    return k == "assembly" || k == "part" || k == "doc" || k == "drawing" ||
           k == "board" || k == "firmware" || k == "sim_study";
}

// Stable kind order for list view grouping.
int kindOrder(const std::string& k) {
    if (k == "project") return 0;
    if (k == "assembly" || k == "doc") return 1;
    if (k == "part") return 2;
    if (k == "body" || k == "bom_item") return 3;
    if (k == "feature" || k == "sketch" || k == "mate" || k == "equation") return 4;
    if (k == "drawing" || k == "sim_study" || k == "board" || k == "firmware") return 5;
    return 3;
}

// Theme-token colours only (gui_lint theme rule).
ImU32 kindColor(const std::string& k, float a = 1.0f) {
    const float* c = palette.accent;
    if (k == "project")                              c = palette.accent;
    else if (k == "assembly" || k == "doc")          c = palette.axisZ;
    else if (k == "part" || k == "body")             c = palette.axisY;
    else if (k == "feature" || k == "sketch")        c = palette.sketchUnder;
    else if (k == "equation")                        c = palette.statusOk;
    else if (k == "mate")                            c = palette.axisX;
    else if (k == "bom_item")                        c = palette.statusWarn;
    else if (k == "drawing")                         c = palette.diffHunk;
    else if (k == "sim_study")                       c = palette.simMesh;
    else if (k == "board" || k == "firmware")        c = palette.sketchExternal;
    else                                             c = palette.textDim;
    return ImGui::GetColorU32(ImVec4(c[0], c[1], c[2], a));
}

ImU32 edgeColor(const std::string& k, float a = 0.70f) {
    const float* c = palette.textDim;
    if (k == "CONTAINS")      c = palette.textDim;
    else if (k == "DEPENDS_ON") c = palette.statusWarn;
    else if (k == "MATES")      c = palette.axisX;
    else if (k == "INSTANCE_OF") c = palette.axisY;
    else if (k == "REFERENCES")  c = palette.diffHunk;
    else if (k == "DERIVES_FROM") c = palette.sketchExternal;
    else if (k == "DOCUMENTS")   c = palette.accent;
    else if (k == "SIMULATES")   c = palette.simMesh;
    else if (k == "MANUFACTURES") c = palette.statusOk;
    return ImGui::GetColorU32(ImVec4(c[0], c[1], c[2], a));
}

const char* kindShort(const std::string& k) {
    if (k == "project") return "PROJ";
    if (k == "assembly") return "ASM";
    if (k == "doc") return "DOC";
    if (k == "part") return "PART";
    if (k == "body") return "BODY";
    if (k == "feature") return "FEAT";
    if (k == "sketch") return "SK";
    if (k == "mate") return "MATE";
    if (k == "bom_item") return "BOM";
    if (k == "drawing") return "DWG";
    if (k == "sim_study") return "SIM";
    if (k == "board") return "PCB";
    if (k == "firmware") return "FW";
    if (k == "equation") return "EQ";
    return "NODE";
}

// Canonical edge-kind vocabulary (contracts/cad_graph.md) — filter chips + the
// link-authoring combo iterate this. Index 4 == REFERENCES (linkKindIdx default).
static const char* kEdgeKinds[] = {
    "CONTAINS", "DEPENDS_ON", "MATES", "INSTANCE_OF", "REFERENCES",
    "DERIVES_FROM", "DOCUMENTS", "SIMULATES", "MANUFACTURES",
};

// Compact value label for a dimensional edge, e.g. "width=12mm" or "25mm".
std::string edgeDimLabel(const nlohmann::json& meta) {
    if (!meta.is_object()) return "";
    std::string dim;
    if (auto it = meta.find("dim"); it != meta.end() && it->is_string())
        dim = it->get<std::string>();
    std::string val;
    if (auto it = meta.find("value"); it != meta.end() && !it->is_null()) {
        if (it->is_number_integer()) val = std::to_string(it->get<long long>());
        else if (it->is_number_float()) {
            char b[32];
            std::snprintf(b, sizeof(b), "%.4g", it->get<double>());
            val = b;
        } else if (it->is_string()) val = it->get<std::string>();
    }
    std::string unit;
    if (auto it = meta.find("unit"); it != meta.end() && it->is_string())
        unit = it->get<std::string>();
    if (val.empty() && dim.empty()) return "";
    std::string out;
    if (!dim.empty()) {
        // trim a "fid." prefix so the label reads as the dimension name
        auto dot = dim.rfind('.');
        out = (dot == std::string::npos) ? dim : dim.substr(dot + 1);
    }
    if (!val.empty()) {
        if (!out.empty()) out += "=";
        out += val;
        out += unit;
    }
    return out;
}

// ---- scope / visibility / aggregation ---------------------------------------

// Prefer CONTAINS-ancestor walk to the nearest project-view structural node.
// Fall back to id-prefix decoding ("feature:doc:REL:FID" → "doc:REL").
std::string resolveOwnerId(const GraphTabState& G, const std::string& nodeId) {
    auto it = G.idIndex.find(nodeId);
    if (it == G.idIndex.end()) return {};
    const GNode& self = G.nodes[it->second];
    if (isProjectKind(self.kind)) return self.id;

    // Walk CONTAINS parents; pick nearest structural ancestor.
    std::unordered_set<std::string> seen;
    std::string cur = nodeId;
    while (!cur.empty() && !seen.count(cur)) {
        seen.insert(cur);
        auto pit = G.containsParent.find(cur);
        if (pit == G.containsParent.end()) break;
        cur = pit->second;
        auto nit = G.idIndex.find(cur);
        if (nit == G.idIndex.end()) break;
        if (isProjectKind(G.nodes[nit->second].kind)) return cur;
    }

    // Id-prefix fallback: "kind:doc:REL:…" → "doc:REL"
    // Also handles "eq:doc:REL:name".
    auto c1 = nodeId.find(':');
    if (c1 == std::string::npos) return {};
    auto c2 = nodeId.find(':', c1 + 1);
    if (c2 == std::string::npos) return {};
    auto c3 = nodeId.find(':', c2 + 1);
    if (c3 == std::string::npos) return {};
    std::string cand = nodeId.substr(c1 + 1, c3 - (c1 + 1));  // "doc:REL"
    if (G.idIndex.count(cand)) return cand;
    // Some ids are "feature:<docId>:fid" where docId itself has no extra colon.
    std::string cand2 = nodeId.substr(c1 + 1, c2 - (c1 + 1));
    if (G.idIndex.count(cand2) && isProjectKind(G.nodes[G.idIndex.at(cand2)].kind))
        return cand2;
    // doc_id field on the node
    if (!self.docId.empty() && G.idIndex.count(self.docId)) return self.docId;
    return {};
}

void rebuildContainsParent(GraphTabState& G) {
    G.containsParent.clear();
    for (const auto& e : G.edges) {
        if (e.kind != "CONTAINS") continue;
        // First parent wins (deterministic: edges stay in load order; stable enough).
        if (!G.containsParent.count(e.dst))
            G.containsParent[e.dst] = e.src;
    }
}

void rebuildIdIndex(GraphTabState& G) {
    G.idIndex.clear();
    G.idIndex.reserve(G.nodes.size() * 2);
    for (int i = 0; i < (int)G.nodes.size(); ++i)
        G.idIndex[G.nodes[i].id] = i;
}

// Transitive CONTAINS descendants → per-structural-node feature/sketch badges.
void rebuildDescCounts(GraphTabState& G) {
    G.descCounts.clear();
    // Build adjacency: parent → children via CONTAINS.
    std::unordered_map<std::string, std::vector<std::string>> kids;
    kids.reserve(G.nodes.size());
    for (const auto& e : G.edges) {
        if (e.kind != "CONTAINS") continue;
        kids[e.src].push_back(e.dst);
    }
    // For each structural node, DFS over CONTAINS descendants.
    for (const auto& n : G.nodes) {
        if (!isProjectKind(n.kind) || n.kind == "project") continue;
        DescCounts dc;
        std::vector<std::string> stack = kids[n.id];
        std::unordered_set<std::string> seen;
        while (!stack.empty()) {
            std::string id = std::move(stack.back());
            stack.pop_back();
            if (!seen.insert(id).second) continue;
            auto it = G.idIndex.find(id);
            if (it == G.idIndex.end()) continue;
            const GNode& c = G.nodes[it->second];
            if (c.kind == "feature") ++dc.features;
            else if (c.kind == "sketch") ++dc.sketches;
            else if (!isProjectKind(c.kind)) ++dc.other;
            // Always walk further (assemblies may nest parts, etc.).
            auto kit = kids.find(id);
            if (kit != kids.end())
                for (const auto& ch : kit->second) stack.push_back(ch);
        }
        if (dc.total() > 0) G.descCounts[n.id] = dc;
    }
}

// Compact badge text: "12F · 3SK" or "·47" when mixed/other-only.
std::string formatDescBadge(const DescCounts& dc) {
    char buf[64];
    if (dc.features > 0 && dc.sketches > 0 && dc.other == 0)
        std::snprintf(buf, sizeof(buf), "%dF · %dSK", dc.features, dc.sketches);
    else if (dc.features > 0 && dc.sketches == 0 && dc.other == 0)
        std::snprintf(buf, sizeof(buf), "%dF", dc.features);
    else if (dc.sketches > 0 && dc.features == 0 && dc.other == 0)
        std::snprintf(buf, sizeof(buf), "%dSK", dc.sketches);
    else
        std::snprintf(buf, sizeof(buf), "·%d", dc.total());
    return buf;
}

// Rebuild visibleIdx + visibleEdges for the active scope. Call when data lands
// or scopeDocId changes — not per frame.
void rebuildVisible(GraphTabState& G) {
    G.visibleIdx.clear();
    G.visibleEdges.clear();
    G.selectedEdge = -1;

    if (G.nodes.empty()) return;

    std::unordered_set<std::string> visIds;

    if (G.scopeDocId.empty()) {
        // ---- PROJECT VIEW ---------------------------------------------------
        for (int i = 0; i < (int)G.nodes.size(); ++i) {
            if (isProjectKind(G.nodes[i].kind)) {
                G.visibleIdx.push_back(i);
                visIds.insert(G.nodes[i].id);
            }
        }
        // Deterministic node order by id.
        std::sort(G.visibleIdx.begin(), G.visibleIdx.end(), [&](int a, int b) {
            return G.nodes[a].id < G.nodes[b].id;
        });

        // Aggregate edges whose endpoints may be hidden internals → owners.
        // Key: ownerSrc\0ownerDst\0kind
        struct Agg {
            std::string src, dst, kind, origin, dimLabel;
            int count = 0;
            int srcEdgeIdx = -1;
        };
        std::unordered_map<std::string, Agg> aggs;
        aggs.reserve(G.edges.size());

        for (int ei = 0; ei < (int)G.edges.size(); ++ei) {
            const GEdge& e = G.edges[ei];
            std::string os = e.src, od = e.dst;
            bool srcVis = visIds.count(os) > 0;
            bool dstVis = visIds.count(od) > 0;
            if (!srcVis) {
                os = resolveOwnerId(G, e.src);
                if (os.empty() || !visIds.count(os)) continue;
            }
            if (!dstVis) {
                od = resolveOwnerId(G, e.dst);
                if (od.empty() || !visIds.count(od)) continue;
            }
            if (os == od) continue;  // drop self-loops after aggregation

            std::string key = os;
            key.push_back('\0');
            key += od;
            key.push_back('\0');
            key += e.kind;

            auto& a = aggs[key];
            if (a.count == 0) {
                a.src = os; a.dst = od; a.kind = e.kind;
                a.origin = e.origin;
                a.dimLabel = e.dimLabel;
                a.srcEdgeIdx = ei;
            } else {
                // Prefer extracted origin as representative when mixed.
                if (a.origin != "extracted" && e.origin == "extracted") {
                    a.origin = e.origin;
                    a.dimLabel = e.dimLabel;
                    a.srcEdgeIdx = ei;
                }
            }
            ++a.count;
        }

        // Emit sorted for stability.
        std::vector<std::string> keys;
        keys.reserve(aggs.size());
        for (const auto& kv : aggs) keys.push_back(kv.first);
        std::sort(keys.begin(), keys.end());
        G.visibleEdges.reserve(keys.size());
        for (const auto& k : keys) {
            const Agg& a = aggs[k];
            VEdge ve;
            ve.src = a.src; ve.dst = a.dst; ve.kind = a.kind;
            ve.origin = a.origin; ve.dimLabel = a.dimLabel;
            ve.aggCount = a.count;
            ve.srcEdgeIdx = a.srcEdgeIdx;
            G.visibleEdges.push_back(std::move(ve));
        }
    } else {
        // ---- DRILL-DOWN -----------------------------------------------------
        // Visible = scope root + all CONTAINS-descendants.
        if (!G.idIndex.count(G.scopeDocId)) {
            // Stale scope — fall back to project.
            G.scopeDocId.clear();
            rebuildVisible(G);
            return;
        }
        std::unordered_map<std::string, std::vector<std::string>> kids;
        for (const auto& e : G.edges) {
            if (e.kind != "CONTAINS") continue;
            kids[e.src].push_back(e.dst);
        }
        std::vector<std::string> stack = {G.scopeDocId};
        std::unordered_set<std::string> seen;
        while (!stack.empty()) {
            std::string id = std::move(stack.back());
            stack.pop_back();
            if (!seen.insert(id).second) continue;
            auto it = G.idIndex.find(id);
            if (it == G.idIndex.end()) continue;
            G.visibleIdx.push_back(it->second);
            visIds.insert(id);
            auto kit = kids.find(id);
            if (kit != kids.end())
                for (const auto& ch : kit->second) stack.push_back(ch);
        }
        std::sort(G.visibleIdx.begin(), G.visibleIdx.end(), [&](int a, int b) {
            return G.nodes[a].id < G.nodes[b].id;
        });

        // Edges whose BOTH ends are in the visible set (internal focus).
        for (int ei = 0; ei < (int)G.edges.size(); ++ei) {
            const GEdge& e = G.edges[ei];
            if (!visIds.count(e.src) || !visIds.count(e.dst)) continue;
            VEdge ve;
            ve.src = e.src; ve.dst = e.dst; ve.kind = e.kind;
            ve.origin = e.origin; ve.dimLabel = e.dimLabel;
            ve.aggCount = 1;
            ve.srcEdgeIdx = ei;
            G.visibleEdges.push_back(std::move(ve));
        }
    }

    G.layoutDirty = true;
    G.layoutToken++;
    G.matchDirty = true;
}

void setScope(GraphTabState& G, const std::string& docId) {
    if (G.scopeDocId == docId) return;
    G.scopeDocId = docId;
    G.selectedId.clear();
    G.selectedEdge = -1;
    G.blast.clear();
    G.hoverId.clear();
    G.userNavigated = false;  // fresh scope → allow one auto-fit again
    rebuildVisible(G);
    // Fit on next canvas paint.
}

// Rebuild the force simulation over the VISIBLE subgraph (called when
// layoutDirty, not per frame). Existing layout coords are reused as seeds so
// refreshes/filter toggles stay stable; new nodes seed near their CONTAINS
// parent or on a deterministic golden-angle spiral. Hidden edge kinds exert
// no spring force. Sets fitOnSettle so the view frames the relaxed layout.
void rebuildForce(GraphTabState& G) {
    G.force.clear();
    G.forceSlot.assign(G.nodes.size(), -1);
    G.dragNode = G.dragCandidate = -1;
    G.layoutDirty = false;
    if (G.visibleIdx.empty()) return;

    const float S = ui::uiScale;
    // Scale the engine's defaults (link 60, repulsion 900) up to our spacing.
    G.force.setLinkDistance(100.0f * S);
    G.force.setRepulsion(900.0f * S * S * (100.0f / 60.0f) * (100.0f / 60.0f));

    const float GOLDEN = 2.39996323f;  // golden angle (rad)
    int fresh = 0;
    for (int vi : G.visibleIdx) {
        GNode& n = G.nodes[vi];
        n.radius = (n.kind == "project") ? 18.0f * S : 15.0f * S;
        float sx = n.lx, sy = n.ly;
        if (sx == 0.0f && sy == 0.0f) {
            bool seeded = false;
            auto pit = G.containsParent.find(n.id);
            if (pit != G.containsParent.end()) {
                auto pi = G.idIndex.find(pit->second);
                if (pi != G.idIndex.end()) {
                    const GNode& p = G.nodes[pi->second];
                    if (p.lx != 0.0f || p.ly != 0.0f) {
                        const float a = (float)fresh * GOLDEN;
                        sx = p.lx + 40.0f * S * std::cos(a);
                        sy = p.ly + 40.0f * S * std::sin(a);
                        seeded = true;
                    }
                }
            }
            if (!seeded) {
                const float r = 90.0f * S * std::sqrt((float)fresh);
                const float a = (float)fresh * GOLDEN;
                sx = r * std::cos(a);
                sy = r * std::sin(a);
            }
            ++fresh;
        }
        G.forceSlot[vi] = G.force.addNode(sx, sy, n.radius);
    }

    // Springs per visible edge; kind tunes rest length / stiffness.
    for (const auto& e : G.visibleEdges) {
        if (G.hiddenKinds.count(e.kind)) continue;
        auto si = G.idIndex.find(e.src), di = G.idIndex.find(e.dst);
        if (si == G.idIndex.end() || di == G.idIndex.end()) continue;
        int a = G.forceSlot[si->second], b = G.forceSlot[di->second];
        if (a < 0 || b < 0 || a == b) continue;
        float rest = 110.0f, strength = 0.45f;
        if (e.kind == "CONTAINS")               { rest = 85.0f;  strength = 0.9f; }
        else if (e.kind == "INSTANCE_OF")       { rest = 90.0f;  strength = 0.7f; }
        else if (e.kind == "DEPENDS_ON")        { rest = 120.0f; strength = 0.5f; }
        else if (e.kind == "MATES")             { rest = 110.0f; strength = 0.55f; }
        else if (e.kind == "REFERENCES" ||
                 e.kind == "DERIVES_FROM")      { rest = 150.0f; strength = 0.3f; }
        if (e.aggCount > 1) strength += std::min(0.3f, 0.05f * (float)(e.aggCount - 1));
        G.force.addEdge(a, b, rest * S, strength);
    }

    G.force.reheat(fresh > (int)G.visibleIdx.size() / 2 ? 0.8f : 0.35f);
    // Auto-fit on settle only until the user first pans/zooms.
    G.fitOnSettle = !G.userNavigated;
    G.wasSettled = false;
}

void drawDashedLine(ImDrawList* dl, ImVec2 a, ImVec2 b, ImU32 col, float thick,
                    float dash = 6.0f, float gap = 4.0f) {
    float dx = b.x - a.x, dy = b.y - a.y;
    float len = std::sqrt(dx * dx + dy * dy);
    if (len < 0.5f) return;
    float ux = dx / len, uy = dy / len;
    float t = 0.0f;
    bool on = true;
    while (t < len) {
        float seg = on ? dash : gap;
        float t1 = std::min(t + seg, len);
        if (on) {
            ImVec2 p0(a.x + ux * t, a.y + uy * t);
            ImVec2 p1(a.x + ux * t1, a.y + uy * t1);
            dl->AddLine(p0, p1, col, thick);
        }
        t = t1;
        on = !on;
    }
}

// Rotating arc spinner for the graph canvas overlay. Draw-list only (no widgets),
// driven by GetTime() so it stays alive while G.loading keeps the frame loop busy
// via st.rpc.busy(). Visual language matches plmSpinner: accent stroke, ~2 rev/s.
void drawArcSpinner(ImDrawList* dl, ImVec2 c, float radius, ImU32 col) {
    const float t = (float)ImGui::GetTime();
    const float a0 = t * (4.0f * 3.14159265f);  // 2 revolutions per second
    const float sweep = 3.14159265f * 1.5f;     // 270° arc
    const float th = std::max(1.5f, radius * 0.18f);
    dl->PathArcTo(c, radius, a0, a0 + sweep, 24);
    dl->PathStroke(col, 0, th);
}


// ---- settled-layout cache (solve state) -------------------------------------

std::filesystem::path layoutCachePath(const GraphTabState& G) {
    return std::filesystem::path(G.projectDir) / ".plm" / "graph_layout.json";
}

// Load <project>/.plm/graph_layout.json once per project. Only file mtimes
// invalidate the worker's graph cache; this UI-owned file does not.
void ensureLayoutCache(GraphTabState& G) {
    if (G.layoutCacheLoaded) return;
    G.layoutCacheLoaded = true;
    G.layoutCache.clear();
    if (G.projectDir.empty()) return;
    std::ifstream f(layoutCachePath(G));
    if (!f) return;
    nlohmann::json j = nlohmann::json::parse(f, nullptr, false);
    if (j.is_discarded()) return;
    auto it = j.find("positions");
    if (it == j.end() || !it->is_object()) return;
    for (auto pit = it->begin(); pit != it->end(); ++pit) {
        const auto& xy = pit.value();
        if (!xy.is_array() || xy.size() != 2 || !xy[0].is_number() || !xy[1].is_number())
            continue;
        G.layoutCache[pit.key()] = {xy[0].get<float>(), xy[1].get<float>()};
    }
}

// Persist every node with a real position (any scope) so the next session
// seeds the sim from the relaxed arrangement. Throttled to once per 2s.
void maybeSaveLayout(GraphTabState& G, bool force = false) {
    if (G.projectDir.empty() || G.nodes.empty()) return;
    const double now = ImGui::GetTime();
    if (!force && (now - G.lastLayoutSave) < 2.0) return;
    G.lastLayoutSave = now;
    nlohmann::json j;
    auto& pos = j["positions"];
    for (const auto& n : G.nodes)
        if (n.lx != 0.0f || n.ly != 0.0f)
            pos[n.id] = {n.lx, n.ly};
    std::error_code ec;
    std::filesystem::create_directories(layoutCachePath(G).parent_path(), ec);
    std::ofstream out(layoutCachePath(G), std::ios::trunc);
    if (out) out << j.dump();
}

// ---- fetch -------------------------------------------------------------------

void requestGraph(AppState& st, bool force) {
    auto& G = S();
    if (!G.available) return;
    if (G.loading) return;
    const std::string proj = st.plmProjectPath();
    if (proj.empty()) return;
    const double now = ImGui::GetTime();
    if (!force && (now - G.lastFetch) < 2.0 && proj == G.projectDir && !G.nodes.empty())
        return;

    G.loading = true;
    G.err.clear();
    G.projectDir = proj;
    G.lastFetch = now;

    nlohmann::json p;
    p["project_dir"] = proj;
    // Also accept path-style callers; harmless extra key for robust workers.
    p["path"] = proj;
    // Archive dirs are excluded server-side by default; flip only when the
    // user unchecks "Hide archive/stale".
    p["include_archive"] = !G.hideArchive;
    if (force) p["force"] = true;
    // Change probe: when the worker confirms nothing changed we get a tiny
    // "unchanged" reply instead of the full multi-MB payload.
    if (!force && G.mtimesMax > 0.0) {
        p["if_mtimes_max"] = G.mtimesMax;
        p["if_sources_count"] = G.sourcesCount;
    }

    st.rpc.call("cad_graph_get", p, [](const core::RpcResult& r) {
        auto& G = S();
        G.loading = false;
        if (isAbsent(r)) {
            G.available = false;
            G.nodes.clear();
            G.edges.clear();
            G.err.clear();
            return;
        }
        if (!r.ok) {
            G.err = r.err.empty() ? "the service did not respond" : r.err;
            return;
        }
        if (r.result.is_object() && !r.result.value("ok", true)) {
            G.err = r.result.value("hint", r.result.value("reason",
                        std::string("that could not be done")));
            return;
        }

        // Change probe hit: graph unchanged — keep data, layout, and view.
        if (r.result.is_object() && r.result.value("unchanged", false))
            return;

        // Stash current positions so a refetch doesn't re-solve from zero.
        std::unordered_map<std::string, std::pair<float, float>> posStash;
        posStash.reserve(G.nodes.size());
        for (const auto& n : G.nodes)
            if (n.lx != 0.0f || n.ly != 0.0f)
                posStash[n.id] = {n.lx, n.ly};

        G.nodes.clear();
        G.edges.clear();
        G.blast.clear();
        G.visibleIdx.clear();
        G.visibleEdges.clear();
        G.descCounts.clear();
        G.idIndex.clear();
        G.containsParent.clear();
        G.generatedAt = jstr(r.result, "generated_at");

        // Truncation / full counts (honest "showing N of M").
        G.truncated = r.result.value("truncated", false);
        G.nodeCountFull = r.result.value("node_count", 0);
        G.edgeCountFull = r.result.value("edge_count", 0);

        if (auto it = r.result.find("nodes"); it != r.result.end() && it->is_array()) {
            G.nodes.reserve(it->size());
            for (const auto& jn : *it) {
                if (!jn.is_object()) continue;
                GNode n;
                n.id     = jstr(jn, "id");
                n.kind   = jstr(jn, "kind");
                n.label  = jstr(jn, "label");
                n.path   = jstr(jn, "path");
                n.docId  = jstr(jn, "doc_id");
                n.origin = jstr(jn, "origin");
                if (n.origin.empty()) n.origin = "extracted";
                if (auto m = jn.find("meta"); m != jn.end() && m->is_object())
                    n.metaSummary = summarizeMeta(*m);
                if (n.id.empty()) continue;
                if (n.label.empty()) n.label = n.id;
                G.nodes.push_back(std::move(n));
            }
        }
        if (auto it = r.result.find("edges"); it != r.result.end() && it->is_array()) {
            G.edges.reserve(it->size());
            for (const auto& je : *it) {
                if (!je.is_object()) continue;
                GEdge e;
                e.src    = jstr(je, "src");
                e.dst    = jstr(je, "dst");
                e.kind   = jstr(je, "kind");
                e.origin = jstr(je, "origin");
                if (e.origin.empty()) e.origin = "extracted";
                if (auto m = je.find("meta"); m != je.end() && m->is_object()) {
                    e.meta = *m;
                    e.metaSummary = summarizeMeta(*m);
                    if (e.kind == "DERIVES_FROM" || e.kind == "REFERENCES" ||
                        e.kind == "MATES")
                        e.dimLabel = edgeDimLabel(*m);
                }
                if (e.src.empty() || e.dst.empty()) continue;
                G.edges.push_back(std::move(e));
            }
        }

        // Backfill full counts when an older worker omitted them.
        if (G.nodeCountFull <= 0) G.nodeCountFull = (int)G.nodes.size();
        if (G.edgeCountFull <= 0) G.edgeCountFull = (int)G.edges.size();

        // Probe fingerprint for subsequent polls.
        G.mtimesMax = r.result.value("sources_mtime_max", 0.0);
        G.sourcesCount = r.result.value("sources_count", 0);

        // Restore positions: in-session stash wins, then the on-disk solve
        // state from previous sessions (loaded once per project).
        ensureLayoutCache(G);
        for (auto& n : G.nodes) {
            if (auto it = posStash.find(n.id); it != posStash.end()) {
                n.lx = it->second.first;
                n.ly = it->second.second;
            } else if (auto it = G.layoutCache.find(n.id); it != G.layoutCache.end()) {
                n.lx = it->second.first;
                n.ly = it->second.second;
            }
        }

        // Indexes + descendant badges + scoped visible set (once per load).
        rebuildIdIndex(G);
        rebuildContainsParent(G);
        rebuildDescCounts(G);
        // Drop stale drill target if the node vanished.
        if (!G.scopeDocId.empty() && !G.idIndex.count(G.scopeDocId))
            G.scopeDocId.clear();
        rebuildVisible(G);

        // With project-view scoping the canvas stays readable; only auto-list
        // when the *visible* set is still enormous (or user forced list).
        if (!G.listViewUserOverride) {
            G.listView = (int)G.visibleIdx.size() > kListViewThreshold;
        }

        // Keep selection if it still exists in the full graph.
        if (!G.selectedId.empty()) {
            if (!G.idIndex.count(G.selectedId)) {
                G.selectedId.clear();
                G.blast.clear();
            }
        }
        G.selectedEdge = -1;
    });
}

void requestBlast(AppState& st, const std::string& nodeId) {
    auto& G = S();
    if (!G.available || nodeId.empty() || G.blastLoading) return;
    G.blastLoading = true;
    G.blast.clear();
    nlohmann::json p;
    p["node_id"] = nodeId;
    p["project_dir"] = st.plmProjectPath();
    p["path"] = st.plmProjectPath();
    st.rpc.call("cad_graph_blast", p, [nodeId](const core::RpcResult& r) {
        auto& G = S();
        G.blastLoading = false;
        if (isAbsent(r)) {
            G.available = false;
            return;
        }
        if (!r.ok || (r.result.is_object() && !r.result.value("ok", true))) {
            // Soft fail — leave previous highlight empty, keep selection.
            return;
        }
        G.blast.clear();
        G.blast.insert(nodeId);
        if (auto it = r.result.find("affected"); it != r.result.end() && it->is_array()) {
            for (const auto& a : *it) {
                if (a.is_string()) G.blast.insert(a.get<std::string>());
            }
        }
    });
}

void requestLinkAdd(AppState& st, const std::string& src, const std::string& dst,
                    const std::string& kind, const std::string& note) {
    auto& G = S();
    if (!G.available || src.empty() || dst.empty()) return;
    nlohmann::json p;
    p["project_dir"] = st.plmProjectPath();
    p["path"] = st.plmProjectPath();
    p["src"] = src;
    p["dst"] = dst;
    p["kind"] = kind;
    p["note"] = note;
    p["origin"] = "manual";
    st.rpc.call("cad_graph_link_add", p, [](const core::RpcResult& r) {
        if (isAbsent(r)) { S().available = false; return; }
        // Rebuild on the next frame so the new edge appears.
        S().lastFetch = -1e9;
    });
}

void requestLinkRemove(AppState& st, const std::string& linkId) {
    auto& G = S();
    if (!G.available || linkId.empty()) return;
    nlohmann::json p;
    p["project_dir"] = st.plmProjectPath();
    p["path"] = st.plmProjectPath();
    p["link_id"] = linkId;
    st.rpc.call("cad_graph_link_remove", p, [](const core::RpcResult& r) {
        if (isAbsent(r)) { S().available = false; return; }
        S().selectedEdge = -1;
        S().lastFetch = -1e9;
    });
}

// Local glass action button (mirrors plmGlassButton without coupling to pages TU).
bool graphGlassButton(const char* id, const char* label, bool accent = false,
                      float widthOverride = 0.0f) {
    const float S = ui::uiScale;
    const float padX = 12 * S;
    const float textW = (label && label[0]) ? ImGui::CalcTextSize(label).x : 0.0f;
    const float w = widthOverride > 0 ? widthOverride : (padX * 2 + textW);
    const float h = ImGui::GetFrameHeight();
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImVec2 p1(p0.x + w, p0.y + h);
    bool clicked = ImGui::InvisibleButton(id, ImVec2(w, h));
    bool hov = ImGui::IsItemHovered(), held = ImGui::IsItemActive();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ui::glassBackground(dl, p0, p1, 9.0f);
    if (accent) {
        float a = held ? 0.92f : hov ? 0.80f : 0.66f;
        dl->AddRectFilled(p0, p1, ImGui::GetColorU32(ImVec4(
            palette.accent[0], palette.accent[1], palette.accent[2], a)), 9.0f * S);
    } else if (hov) {
        dl->AddRectFilled(p0, p1, ImGui::GetColorU32(ImVec4(
            palette.text[0], palette.text[1], palette.text[2], held ? 0.10f : 0.06f)),
            9.0f * S);
    }
    ImU32 tcol = accent ? Palette::toU32(palette.text) : Palette::toU32(palette.text);
    if (textW > 0)
        dl->AddText(ImVec2(p0.x + (w - textW) * 0.5f,
                           p0.y + (h - ImGui::GetTextLineHeight()) * 0.5f), tcol, label);
    return clicked;
}

void fitView(GraphTabState& G, ImVec2 canvasSz) {
    if (G.visibleIdx.empty() || canvasSz.x < 1.0f || canvasSz.y < 1.0f) return;
    float minX = 1e9f, minY = 1e9f, maxX = -1e9f, maxY = -1e9f;
    for (int vi : G.visibleIdx) {
        const GNode& n = G.nodes[vi];
        minX = std::min(minX, n.lx - n.radius);
        minY = std::min(minY, n.ly - n.radius);
        maxX = std::max(maxX, n.lx + n.radius);
        maxY = std::max(maxY, n.ly + n.radius);
    }
    float gw = std::max(40.0f, maxX - minX);
    float gh = std::max(40.0f, maxY - minY);
    float pad = 40.0f * ui::uiScale;
    float zx = (canvasSz.x - pad * 2) / gw;
    float zy = (canvasSz.y - pad * 2) / gh;
    G.zoom = std::clamp(std::min(zx, zy), 0.15f, 4.0f);
    float cx = (minX + maxX) * 0.5f;
    float cy = (minY + maxY) * 0.5f;
    G.panX = canvasSz.x * 0.5f - cx * G.zoom;
    G.panY = canvasSz.y * 0.5f - cy * G.zoom;
}

void centerOnNode(GraphTabState& G, const GNode& n, ImVec2 canvasSz) {
    G.panX = canvasSz.x * 0.5f - n.lx * G.zoom;
    G.panY = canvasSz.y * 0.5f - n.ly * G.zoom;
}

bool matchesSearch(const GNode& n, const char* q) {
    if (!q || !q[0]) return true;
    // case-insensitive substring on label / kind / id / path
    auto has = [q](const std::string& s) {
        if (s.empty()) return false;
        std::string a = s, b = q;
        for (char& c : a) c = (char)std::tolower((unsigned char)c);
        for (char& c : b) c = (char)std::tolower((unsigned char)c);
        return a.find(b) != std::string::npos;
    };
    return has(n.label) || has(n.kind) || has(n.id) || has(n.path);
}

// Search match cache: matchIds (visible), vis/all match tallies for the
// hidden-match hint. Rebuilt only when the query or visible set changes.
void rebuildMatchCache(GraphTabState& G) {
    G.matchIds.clear();
    G.visMatches = 0;
    G.allMatches = 0;
    std::unordered_set<std::string> visSet;
    visSet.reserve(G.visibleIdx.size() * 2);
    for (int vi : G.visibleIdx) visSet.insert(G.nodes[vi].id);
    for (const auto& n : G.nodes) {
        if (!matchesSearch(n, G.search)) continue;
        ++G.allMatches;
        if (visSet.count(n.id)) {
            ++G.visMatches;
            G.matchIds.insert(n.id);
        }
    }
    G.matchDirty = false;
}

void drawLegend(ImDrawList* dl, ImVec2 origin, bool projectScope) {
    const float S = ui::uiScale;
    // Scope note + edge styles + kinds actually shown in this view.
    struct Row { const char* label; ImU32 col; bool dashed; bool isNode; bool header; };
    std::vector<Row> rows;
    rows.push_back({projectScope ? "Project view" : "Document view",
                    Palette::toU32(palette.textDim), false, false, true});
    rows.push_back({"extracted", edgeColor("CONTAINS", 0.85f), false, false, false});
    rows.push_back({"inferred",  edgeColor("DEPENDS_ON", 0.85f), true,  false, false});
    rows.push_back({"manual",    kindColor("project", 0.90f), false, false, false});
    rows.push_back({"ai link",   kindColor("project", 0.90f), true,  false, false});
    if (projectScope) {
        rows.push_back({"project",  kindColor("project"), false, true, false});
        rows.push_back({"assembly", kindColor("assembly"), false, true, false});
        rows.push_back({"part",     kindColor("part"), false, true, false});
        rows.push_back({"drawing",  kindColor("drawing"), false, true, false});
        rows.push_back({"(agg ×N)", edgeColor("REFERENCES", 0.85f), false, false, false});
    } else {
        rows.push_back({"feature",  kindColor("feature"), false, true, false});
        rows.push_back({"sketch",   kindColor("sketch"), false, true, false});
        rows.push_back({"equation", kindColor("equation"), false, true, false});
        rows.push_back({"mate",     kindColor("mate"), false, true, false});
        rows.push_back({"body",     kindColor("body"), false, true, false});
    }
    float y = origin.y;
    float x = origin.x;
    ImU32 tdim = Palette::toU32(palette.textDim);
    ImU32 tdis = Palette::toU32(palette.textDisabled);
    for (const auto& r : rows) {
        if (r.header) {
            dl->AddText(ImVec2(x, y), tdis, r.label);
            y += 16 * S;
            continue;
        }
        if (r.isNode) {
            dl->AddCircleFilled(ImVec2(x + 6 * S, y + 6 * S), 5 * S, r.col);
            dl->AddCircle(ImVec2(x + 6 * S, y + 6 * S), 5 * S,
                          ImGui::GetColorU32(ImVec4(palette.border[0], palette.border[1],
                                                    palette.border[2], 0.9f)), 0, 1.0f);
        } else if (r.dashed) {
            drawDashedLine(dl, ImVec2(x, y + 6 * S), ImVec2(x + 18 * S, y + 6 * S),
                           r.col, 1.5f * S, 4 * S, 3 * S);
        } else {
            dl->AddLine(ImVec2(x, y + 6 * S), ImVec2(x + 18 * S, y + 6 * S), r.col, 1.5f * S);
        }
        dl->AddText(ImVec2(x + 24 * S, y), tdim, r.label);
        y += 16 * S;
    }
}

void drawSidePanel(AppState& st, float panelW) {
    auto& G = S();
    const float S = ui::uiScale;
    ui::pushGlassChildStyle(10.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12 * S, 10 * S));
    ImGui::BeginChild("##graph_side", ImVec2(panelW, 0), ImGuiChildFlags_Borders);
    ui::popGlassChildStyle();

    auto row = [](const char* k, const std::string& v) {
        if (v.empty()) return;
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::TextUnformatted(k);
        ImGui::PopStyleColor();
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::TextWrapped("%s", v.c_str());
        ImGui::PopStyleColor();
    };
    auto nodeLabel = [&](const std::string& id) -> std::string {
        for (const auto& n : G.nodes) if (n.id == id) return n.label;
        return id;
    };

    // --- an edge is selected (indexes into visibleEdges) ---------------------
    if (G.selectedEdge >= 0 && G.selectedEdge < (int)G.visibleEdges.size()) {
        const VEdge& ve = G.visibleEdges[G.selectedEdge];
        const GEdge* srcE = (ve.srcEdgeIdx >= 0 && ve.srcEdgeIdx < (int)G.edges.size())
            ? &G.edges[ve.srcEdgeIdx] : nullptr;
        plmSectionHeader("Link", "mesh");
        ImGui::Dummy(ImVec2(0, 4 * S));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImGui::ColorConvertU32ToFloat4(edgeColor(ve.kind, 1.0f)));
        if (ve.aggCount > 1)
            ImGui::Text("[%s ×%d]", ve.kind.c_str(), ve.aggCount);
        else
            ImGui::Text("[%s]", ve.kind.c_str());
        ImGui::PopStyleColor();
        ImGui::Dummy(ImVec2(0, 4 * S));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::TextUnformatted("from");
        ImGui::PopStyleColor();
        ImGui::SameLine();
        if (ImGui::SmallButton((nodeLabel(ve.src) + "##lnksrc").c_str())) {
            G.selectedId = ve.src; G.selectedEdge = -1; G.blast.clear();
        }
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::TextUnformatted("to");
        ImGui::PopStyleColor();
        ImGui::SameLine();
        if (ImGui::SmallButton((nodeLabel(ve.dst) + "##lnkdst").c_str())) {
            G.selectedId = ve.dst; G.selectedEdge = -1; G.blast.clear();
        }
        ImGui::Dummy(ImVec2(0, 4 * S));
        if (ve.aggCount > 1) {
            char aggBuf[64];
            std::snprintf(aggBuf, sizeof(aggBuf),
                          "aggregated from %d links", ve.aggCount);
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                       palette.statusWarn[2], 1.0f));
            ImGui::TextWrapped("%s", aggBuf);
            ImGui::PopStyleColor();
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDim[0], palette.textDim[1],
                       palette.textDim[2], 1.0f));
            ImGui::TextWrapped("Showing representative link meta below. "
                               "Drill into a document to inspect individuals.");
            ImGui::PopStyleColor();
            ImGui::Dummy(ImVec2(0, 4 * S));
        }
        row("origin", srcE ? srcE->origin : ve.origin);
        if (srcE && srcE->meta.is_object()) {
            for (auto it = srcE->meta.begin(); it != srcE->meta.end(); ++it) {
                std::string v = it->is_string() ? it->get<std::string>()
                    : (it->is_null() ? std::string() : it->dump());
                row(it.key().c_str(), v);
            }
        }
        ImGui::Dummy(ImVec2(0, 8 * S));
        const std::string& origin = srcE ? srcE->origin : ve.origin;
        bool authored = (origin == "manual" || origin == "ai");
        // Only allow remove when this is a single authored edge (not an agg).
        if (authored && ve.aggCount <= 1 && srcE) {
            std::string linkId = srcE->meta.is_object()
                ? srcE->meta.value("link_id", std::string()) : "";
            if (graphGlassButton("##rmlink", "Remove link", true, -1.0f)
                    && !linkId.empty())
                requestLinkRemove(st, linkId);
        } else if (!authored) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDim[0], palette.textDim[1],
                       palette.textDim[2], 1.0f));
            ImGui::TextWrapped("Derived from the model — edit the model to change it.");
            ImGui::PopStyleColor();
        }
        ImGui::EndChild();
        ImGui::PopStyleVar();
        return;
    }

    plmSectionHeader("Node", "mesh");
    ImGui::Dummy(ImVec2(0, 4 * S));

    const GNode* sel = nullptr;
    if (!G.selectedId.empty()) {
        auto it = G.idIndex.find(G.selectedId);
        if (it != G.idIndex.end()) sel = &G.nodes[it->second];
    }

    if (!sel) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        if (!G.linkArmSrc.empty())
            ImGui::TextWrapped("Click the target node to link it — or Esc to cancel.");
        else if (G.scopeDocId.empty())
            ImGui::TextWrapped("Click a node to inspect it. Double-click a part or "
                               "document to drill into its features.");
        else
            ImGui::TextWrapped("Click a node to inspect it, or a link to see its "
                               "properties. Double-click empty space to go back.");
        ImGui::PopStyleColor();
        ImGui::EndChild();
        ImGui::PopStyleVar();
        return;
    }

    // Kind pill
    ImGui::PushStyleColor(ImGuiCol_Text, ImGui::ColorConvertU32ToFloat4(kindColor(sel->kind)));
    ImGui::Text("[%s]", kindShort(sel->kind));
    ImGui::PopStyleColor();
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.text[0], palette.text[1], palette.text[2], 1.0f));
    ImGui::TextWrapped("%s", sel->label.c_str());
    ImGui::PopStyleColor();

    ImGui::Dummy(ImVec2(0, 4 * S));
    row("kind", sel->kind);
    row("id", sel->id);
    row("origin", sel->origin);
    row("path", sel->path);
    row("doc", sel->docId);
    row("meta", sel->metaSummary);

    // Descendant badge summary in project view.
    if (G.scopeDocId.empty()) {
        auto dit = G.descCounts.find(sel->id);
        if (dit != G.descCounts.end() && dit->second.total() > 0) {
            row("inside", formatDescBadge(dit->second));
        }
    }

    ImGui::Dummy(ImVec2(0, 8 * S));

    // Drill into a structural node from project view.
    if (G.scopeDocId.empty() && isDrillableKind(sel->kind)) {
        if (graphGlassButton("##drill", "Drill in", true, -1.0f)) {
            setScope(G, sel->id);
        }
        ImGui::Dummy(ImVec2(0, 4 * S));
    } else if (!G.scopeDocId.empty() && G.scopeDocId == sel->id) {
        if (graphGlassButton("##drillup", "Back to project", false, -1.0f)) {
            setScope(G, "");
        }
        ImGui::Dummy(ImVec2(0, 4 * S));
    }

    if (graphGlassButton("##blast", G.blastLoading ? "Blasting…" : "Blast radius",
                         true, -1.0f) && !G.blastLoading) {
        requestBlast(st, sel->id);
    }
    // Link authoring: arm this node as a link source.
    ImGui::Dummy(ImVec2(0, 4 * S));
    if (G.linkArmSrc == sel->id) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
        ImGui::TextWrapped("Click the target node…");
        ImGui::PopStyleColor();
        if (graphGlassButton("##armcancel", "Cancel", false, -1.0f))
            G.linkArmSrc.clear();
    } else if (graphGlassButton("##armlink", "Link from here…", false, -1.0f)) {
        G.linkArmSrc = sel->id;
    }
    if (!G.blast.empty()) {
        ImGui::Dummy(ImVec2(0, 4 * S));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                   palette.statusWarn[2], 1.0f));
        ImGui::Text("%d node%s in blast", (int)G.blast.size(),
                    G.blast.size() == 1 ? "" : "s");
        ImGui::PopStyleColor();
    }

    // Outgoing / incoming edge counts over the FULL graph for orientation.
    int outE = 0, inE = 0;
    for (const auto& e : G.edges) {
        if (e.src == sel->id) ++outE;
        if (e.dst == sel->id) ++inE;
    }
    ImGui::Dummy(ImVec2(0, 6 * S));
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDisabled[0], palette.textDisabled[1],
               palette.textDisabled[2], 1.0f));
    ImGui::Text("edges  out %d · in %d", outE, inE);
    ImGui::PopStyleColor();

    ImGui::EndChild();
    ImGui::PopStyleVar();
}

void drawCanvas(AppState& st, ImVec2 canvasSz) {
    auto& G = S();
    const float S = ui::uiScale;
    (void)st;

    ImGui::InvisibleButton("##graph_canvas", canvasSz,
                           ImGuiButtonFlags_MouseButtonLeft |
                           ImGuiButtonFlags_MouseButtonMiddle |
                           ImGuiButtonFlags_MouseButtonRight);
    const bool hovered = ImGui::IsItemHovered();
    const ImVec2 origin = ImGui::GetItemRectMin();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->PushClipRect(origin, ImVec2(origin.x + canvasSz.x, origin.y + canvasSz.y), true);

    // Frosted canvas bed.
    ui::glassBackground(dl, origin,
                        ImVec2(origin.x + canvasSz.x, origin.y + canvasSz.y), 10.0f);

    if (G.layoutDirty) rebuildForce(G);

    // First layout after data/scope change → fit the view once.
    static int s_fitToken = -1;
    if (s_fitToken != G.layoutToken && !G.visibleIdx.empty()) {
        fitView(G, canvasSz);
        s_fitToken = G.layoutToken;
    }

    // Advance physics and copy graph-space positions back into the render
    // cache. Frozen when settled unless a node is being dragged — idle
    // frames cost nothing but the draw itself.
    if (!G.visibleIdx.empty() && (G.dragNode >= 0 || !G.force.settled())) {
        G.force.step(std::min(ImGui::GetIO().DeltaTime, 0.05f));
        for (int vi : G.visibleIdx) {
            int slot = (vi < (int)G.forceSlot.size()) ? G.forceSlot[vi] : -1;
            if (slot < 0) continue;
            G.nodes[vi].lx = G.force.x(slot);
            G.nodes[vi].ly = G.force.y(slot);
        }
        // Settle transition: persist the solve state; frame the relaxed
        // layout once (unless the user already took over the view).
        bool nowSettled = G.force.settled();
        if (!G.wasSettled && nowSettled) {
            maybeSaveLayout(G);
            if (G.fitOnSettle) {
                fitView(G, canvasSz);
                s_fitToken = G.layoutToken;
                G.fitOnSettle = false;
            }
        }
        G.wasSettled = nowSettled;
    }

    // Zoom (wheel) around cursor.
    if (hovered) {
        float wheel = ImGui::GetIO().MouseWheel;
        if (wheel != 0.0f) {
            G.userNavigated = true;
            ImVec2 mp = ImGui::GetIO().MousePos;
            float lx = (mp.x - origin.x - G.panX) / G.zoom;
            float ly = (mp.y - origin.y - G.panY) / G.zoom;
            G.zoom = std::clamp(G.zoom * (wheel > 0 ? 1.12f : 1.0f / 1.12f), 0.12f, 6.0f);
            G.panX = (mp.x - origin.x) - lx * G.zoom;
            G.panY = (mp.y - origin.y) - ly * G.zoom;
        }
    }

    // Pan: middle-drag or left-drag on empty background (never mid node-drag).
    if (hovered && G.dragNode < 0 && G.dragCandidate < 0 &&
        (ImGui::IsMouseDragging(ImGuiMouseButton_Middle) ||
         (ImGui::IsMouseDragging(ImGuiMouseButton_Left) &&
          G.hoverId.empty() && !ImGui::GetIO().KeyCtrl))) {
        G.userNavigated = true;
        ImVec2 d = ImGui::GetIO().MouseDelta;
        G.panX += d.x;
        G.panY += d.y;
    }

    auto toScreen = [&](float lx, float ly) -> ImVec2 {
        return ImVec2(origin.x + G.panX + lx * G.zoom,
                      origin.y + G.panY + ly * G.zoom);
    };

    // Viewport culling helpers (screen space, padded past the clip rect).
    const ImVec2 scrMin(origin.x - 40.0f, origin.y - 40.0f);
    const ImVec2 scrMax(origin.x + canvasSz.x + 40.0f,
                        origin.y + canvasSz.y + 40.0f);
    auto onScreen = [&](ImVec2 p) {
        return p.x >= scrMin.x && p.x <= scrMax.x && p.y >= scrMin.y && p.y <= scrMax.y;
    };

    // Search filter over VISIBLE nodes (cached; rebuilt on query/visible change).
    const bool filtering = G.search[0] != 0;
    if (filtering && G.matchDirty) rebuildMatchCache(G);

    // Focus id for incidence dimming: hover wins, else selection.
    const std::string& focusId = !G.hoverId.empty() ? G.hoverId : G.selectedId;
    const bool incidenceDim = !focusId.empty() || !G.blast.empty();

    // Precompute which visible edges touch the focus node (or blast set).
    auto edgeIncident = [&](const VEdge& e) -> bool {
        if (!G.blast.empty())
            return G.blast.count(e.src) || G.blast.count(e.dst);
        if (focusId.empty()) return false;
        return e.src == focusId || e.dst == focusId;
    };

    // ---- Edges (visibleEdges only) ------------------------------------------
    // Coarse LOD for huge webs: solid 1px lines, no mid-edge labels.
    const bool edgeLod = (int)G.visibleEdges.size() > 4000;
    for (int ei = 0; ei < (int)G.visibleEdges.size(); ++ei) {
        const VEdge& e = G.visibleEdges[ei];
        if (G.hiddenKinds.count(e.kind)) continue;
        auto si = G.idIndex.find(e.src), di = G.idIndex.find(e.dst);
        if (si == G.idIndex.end() || di == G.idIndex.end()) continue;
        const GNode& a = G.nodes[si->second];
        const GNode& b = G.nodes[di->second];

        // Skip same-layer CONTAINS (defensive declutter).
        if (e.kind == "CONTAINS" && std::fabs(a.ly - b.ly) < 1.0f)
            continue;

        bool dimf = filtering && !G.matchIds.count(a.id) && !G.matchIds.count(b.id);
        bool incident = edgeIncident(e);
        bool selEdge = (ei == G.selectedEdge);
        bool authored = (e.origin == "manual" || e.origin == "ai");

        float alpha;
        if (dimf) alpha = 0.08f;
        else if (selEdge) alpha = 1.0f;
        else if (incidenceDim && !incident) alpha = 0.08f;
        else if (incident) alpha = 0.95f;
        else alpha = 0.55f;

        ImU32 col = edgeColor(e.kind, alpha);
        if (authored && !dimf && (incident || selEdge || !incidenceDim))
            col = ImGui::GetColorU32(ImVec4(palette.accent[0], palette.accent[1],
                                            palette.accent[2], selEdge ? 1.0f : 0.85f));
        else if (incident && !G.blast.empty() && !dimf)
            col = ImGui::GetColorU32(ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                                           palette.statusWarn[2], 0.90f));

        ImVec2 p0 = toScreen(a.lx, a.ly);
        ImVec2 p1 = toScreen(b.lx, b.ly);
        if (!onScreen(p0) && !onScreen(p1)) continue;  // viewport cull
        float thick = (edgeLod ? 1.0f
                      : selEdge ? 3.0f : (incident && !dimf ? 2.2f : 1.3f)) * S;
        if (!edgeLod && (e.origin == "inferred" || e.origin == "ai"))
            drawDashedLine(dl, p0, p1, col, thick, 7 * S, 4 * S);
        else
            dl->AddLine(p0, p1, col, thick);

        // Dimensional / aggregation label at midpoint when zoomed in.
        if (!edgeLod && !dimf && alpha > 0.2f && G.zoom >= 0.8f) {
            std::string lab = e.dimLabel;
            if (e.aggCount > 1) {
                char buf[48];
                if (lab.empty())
                    std::snprintf(buf, sizeof(buf), "%s ×%d", e.kind.c_str(), e.aggCount);
                else
                    std::snprintf(buf, sizeof(buf), "%s ×%d", lab.c_str(), e.aggCount);
                lab = buf;
            }
            if (!lab.empty()) {
                ImVec2 midp((p0.x + p1.x) * 0.5f, (p0.y + p1.y) * 0.5f);
                ImVec2 ts = ImGui::CalcTextSize(lab.c_str());
                dl->AddText(ImVec2(midp.x - ts.x * 0.5f, midp.y - ts.y * 0.5f),
                            Palette::toU32(palette.textDim), lab.c_str());
            }
        }
    }

    // ---- Hit-test VISIBLE nodes ---------------------------------------------
    G.hoverId.clear();
    ImVec2 mp = ImGui::GetIO().MousePos;
    float bestD = 1e9f;
    if (hovered) {
        for (int vi : G.visibleIdx) {
            const GNode& n = G.nodes[vi];
            ImVec2 c = toScreen(n.lx, n.ly);
            float r = n.radius * G.zoom;
            float dx = mp.x - c.x, dy = mp.y - c.y;
            float d = dx * dx + dy * dy;
            if (d <= r * r && d < bestD) {
                bestD = d;
                G.hoverId = n.id;
            }
        }
    }

    // ---- Node physics-drag ---------------------------------------------------
    // Press on a node arms a candidate; crossing the drag threshold pins it to
    // the cursor (live physics) until release. Click-select still fires on the
    // press frame below, so plain clicks are unaffected.
    if (hovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !G.hoverId.empty()) {
        auto it = G.idIndex.find(G.hoverId);
        G.dragCandidate = (it != G.idIndex.end()) ? it->second : -1;
    }
    if (G.dragCandidate >= 0 && G.dragNode < 0 &&
        ImGui::IsMouseDragging(ImGuiMouseButton_Left, 4.0f * S)) {
        G.dragNode = G.dragCandidate;
        int slot = (G.dragNode < (int)G.forceSlot.size()) ? G.forceSlot[G.dragNode] : -1;
        if (slot >= 0) G.force.setPinned(slot, true);
    }
    if (G.dragNode >= 0) {
        int slot = (G.dragNode < (int)G.forceSlot.size()) ? G.forceSlot[G.dragNode] : -1;
        if (ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
            if (slot >= 0) {
                G.force.setPos(slot, (mp.x - origin.x - G.panX) / G.zoom,
                                     (mp.y - origin.y - G.panY) / G.zoom);
                G.force.reheat(0.03f);  // neighbours react while dragging
            }
            ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
        } else {
            if (slot >= 0) G.force.setPinned(slot, false);
            G.force.reheat(0.15f);
            G.dragNode = -1;
        }
    }
    if (!ImGui::IsMouseDown(ImGuiMouseButton_Left)) G.dragCandidate = -1;

    // Single-click select (nodes) — also completes an armed link.
    if (hovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !G.hoverId.empty()) {
        if (!G.linkArmSrc.empty() && G.hoverId != G.linkArmSrc) {
            G.linkPopupDst = G.hoverId;
            G.linkPopupOpen = true;
        } else {
            G.selectedId = G.hoverId;
            G.selectedEdge = -1;
            G.blast.clear();
        }
    }

    // Double-click drill / back.
    if (hovered && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) {
        if (!G.hoverId.empty() && G.scopeDocId.empty()) {
            auto it = G.idIndex.find(G.hoverId);
            if (it != G.idIndex.end() && isDrillableKind(G.nodes[it->second].kind))
                setScope(G, G.hoverId);
        } else if (G.hoverId.empty() && !G.scopeDocId.empty()) {
            setScope(G, "");
        }
    }

    // Nearest-edge pick when no node is under the cursor (< 6px, screen space).
    if (hovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) && G.hoverId.empty()
        && G.linkArmSrc.empty()) {
        int bestEdge = -1;
        float bestEd = 6.0f * 6.0f;
        for (int ei = 0; ei < (int)G.visibleEdges.size(); ++ei) {
            const VEdge& e = G.visibleEdges[ei];
            if (G.hiddenKinds.count(e.kind)) continue;
            auto si = G.idIndex.find(e.src), di = G.idIndex.find(e.dst);
            if (si == G.idIndex.end() || di == G.idIndex.end()) continue;
            const GNode& a = G.nodes[si->second];
            const GNode& b = G.nodes[di->second];
            if (e.kind == "CONTAINS" && std::fabs(a.ly - b.ly) < 1.0f) continue;
            ImVec2 p0 = toScreen(a.lx, a.ly);
            ImVec2 p1 = toScreen(b.lx, b.ly);
            float vx = p1.x - p0.x, vy = p1.y - p0.y;
            float wx = mp.x - p0.x, wy = mp.y - p0.y;
            float len2 = vx * vx + vy * vy;
            float t = len2 > 0 ? std::clamp((wx * vx + wy * vy) / len2, 0.0f, 1.0f) : 0.0f;
            float ex = p0.x + t * vx, ey = p0.y + t * vy;
            float dx = mp.x - ex, dy = mp.y - ey;
            float d2 = dx * dx + dy * dy;
            if (d2 < bestEd) { bestEd = d2; bestEdge = ei; }
        }
        G.selectedEdge = bestEdge;
        if (bestEdge >= 0) { G.selectedId.clear(); G.blast.clear(); }
    }

    // ---- Nodes (visibleIdx only) --------------------------------------------
    const bool projectScope = G.scopeDocId.empty();
    // Label LOD: with few nodes on screen, label everything; otherwise only
    // hovered/selected/search-matched nodes (text is the draw bottleneck).
    int onScreenNodes = 0;
    for (int vi : G.visibleIdx) {
        const GNode& n = G.nodes[vi];
        if (onScreen(toScreen(n.lx, n.ly))) ++onScreenNodes;
    }
    const bool labelAll = onScreenNodes <= 150;
    for (int vi : G.visibleIdx) {
        const GNode& n = G.nodes[vi];
        ImVec2 c = toScreen(n.lx, n.ly);
        if (!onScreen(c)) continue;  // viewport cull
        float r = n.radius * G.zoom;
        bool dim = filtering && !G.matchIds.count(n.id);
        bool sel = (n.id == G.selectedId);
        bool hov = (n.id == G.hoverId);
        bool inBlast = G.blast.count(n.id) > 0;
        // Soft-dim non-incident nodes when something is focused.
        bool nonIncident = incidenceDim && !sel && !hov && !inBlast &&
                           n.id != focusId;
        float a = dim ? 0.18f : (nonIncident ? 0.45f : 1.0f);
        ImU32 fill = kindColor(n.kind, 0.85f * a);
        ImU32 ring = Palette::toU32(palette.border);
        if (sel) {
            ring = Palette::toU32(palette.accent);
            r += 2.0f * S;
        } else if (inBlast) {
            ring = Palette::toU32(palette.statusWarn);
            r += 1.5f * S;
        } else if (hov) {
            ring = Palette::toU32(palette.accentDim);
        }
        dl->AddCircleFilled(c, r, fill, 24);
        dl->AddCircle(c, r, ring, 24, (sel || inBlast ? 2.4f : 1.4f) * S);
        if (hov && !dim)
            dl->AddCircleFilled(c, r * 0.35f,
                ImGui::GetColorU32(ImVec4(palette.text[0], palette.text[1],
                                          palette.text[2], 0.55f)), 12);

        // Labels: LOD-gated (few on screen, or zoomed, or focused/matched).
        const bool showLabel = !dim &&
            ((labelAll && G.zoom >= 0.35f) || sel || hov ||
             (filtering && G.matchIds.count(n.id)) || G.zoom >= 0.9f);
        if (showLabel) {
            const char* lab = n.label.c_str();
            ImVec2 ts = ImGui::CalcTextSize(lab);
            float maxW = 140.0f * S * std::max(0.8f, G.zoom);
            float drawW = std::min(ts.x, maxW);
            ImU32 tc = ImGui::GetColorU32(ImVec4(palette.text[0], palette.text[1],
                                                palette.text[2], sel || hov ? 1.0f : 0.90f));
            ImVec2 tp(c.x - drawW * 0.5f, c.y + r + 3 * S);

            // Subtle dark halo/backing rect for legibility over edges.
            const float padX = 3.0f * S, padY = 1.0f * S;
            ImU32 halo = ImGui::GetColorU32(ImVec4(palette.panelBg[0], palette.panelBg[1],
                                                   palette.panelBg[2], 0.72f));
            dl->AddRectFilled(ImVec2(tp.x - padX, tp.y - padY),
                              ImVec2(tp.x + drawW + padX, tp.y + ts.y + padY),
                              halo, 3.0f * S);

            if (ts.x > maxW) {
                dl->PushClipRect(tp, ImVec2(tp.x + maxW, tp.y + ts.y), true);
                dl->AddText(tp, tc, lab);
                dl->PopClipRect();
            } else {
                dl->AddText(tp, tc, lab);
            }

            // Descendant badge under the label (project view).
            if (projectScope && (G.zoom >= 0.55f || sel || hov)) {
                auto dit = G.descCounts.find(n.id);
                if (dit != G.descCounts.end() && dit->second.total() > 0) {
                    std::string badge = formatDescBadge(dit->second);
                    ImVec2 bs = ImGui::CalcTextSize(badge.c_str());
                    ImU32 bc = ImGui::GetColorU32(ImVec4(
                        palette.textDisabled[0], palette.textDisabled[1],
                        palette.textDisabled[2], 0.95f));
                    dl->AddText(ImVec2(c.x - bs.x * 0.5f, tp.y + ts.y + 1 * S),
                                bc, badge.c_str());
                }
            }
        }
    }

    // Tooltip.
    if (!G.hoverId.empty() && hovered) {
        const GNode* hn = nullptr;
        auto it = G.idIndex.find(G.hoverId);
        if (it != G.idIndex.end()) hn = &G.nodes[it->second];
        if (hn) {
            ImGui::BeginTooltip();
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImGui::ColorConvertU32ToFloat4(kindColor(hn->kind)));
            ImGui::Text("[%s]", kindShort(hn->kind));
            ImGui::PopStyleColor();
            ImGui::SameLine();
            ImGui::TextUnformatted(hn->label.c_str());
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
            ImGui::Text("kind  %s", hn->kind.c_str());
            ImGui::Text("origin  %s", hn->origin.c_str());
            if (!hn->path.empty()) ImGui::Text("path  %s", hn->path.c_str());
            if (!hn->metaSummary.empty())
                ImGui::TextWrapped("meta  %s", hn->metaSummary.c_str());
            if (projectScope) {
                auto dit = G.descCounts.find(hn->id);
                if (dit != G.descCounts.end() && dit->second.total() > 0)
                    ImGui::Text("inside  %s", formatDescBadge(dit->second).c_str());
                if (isDrillableKind(hn->kind))
                    ImGui::TextUnformatted("double-click to drill in");
            }
            ImGui::PopStyleColor();
            ImGui::EndTooltip();
        }
    }

    // Empty / loading overlays.
    if (G.nodes.empty() && !G.loading) {
        const char* msg = G.err.empty() ? "No graph nodes yet" : G.err.c_str();
        const char* sub = G.err.empty()
            ? "Open or save parts in this project to populate the graph."
            : nullptr;
        ImVec2 ts = ImGui::CalcTextSize(msg);
        dl->AddText(ImVec2(origin.x + (canvasSz.x - ts.x) * 0.5f,
                           origin.y + canvasSz.y * 0.45f),
                    Palette::toU32(palette.textDim), msg);
        if (sub) {
            ImVec2 ss = ImGui::CalcTextSize(sub);
            dl->AddText(ImVec2(origin.x + (canvasSz.x - ss.x) * 0.5f,
                               origin.y + canvasSz.y * 0.45f + ts.y + 6 * S),
                        Palette::toU32(palette.textDisabled), sub);
        }
    }
    if (G.loading && G.nodes.empty()) {
        const char* msg = "Loading graph…";
        ImVec2 ts = ImGui::CalcTextSize(msg);
        const float r = 12.0f * S;
        const float gap = 10.0f * S;
        const float blockH = r * 2.0f + gap + ts.y;
        const ImVec2 spinC(origin.x + canvasSz.x * 0.5f,
                           origin.y + (canvasSz.y - blockH) * 0.5f + r);
        drawArcSpinner(dl, spinC, r, Palette::toU32(palette.accent));
        dl->AddText(ImVec2(origin.x + (canvasSz.x - ts.x) * 0.5f,
                           spinC.y + r + gap),
                    Palette::toU32(palette.accent), msg);
    }

    // Legend bottom-left of canvas.
    drawLegend(dl, ImVec2(origin.x + 12 * S,
                          origin.y + canvasSz.y - 200 * S),
               projectScope);

    dl->PopClipRect();

    // Search-driven center: when the user types a filter, center on first VISIBLE match.
    if (G.centerPending && filtering && !G.matchIds.empty()) {
        for (int vi : G.visibleIdx) {
            if (G.matchIds.count(G.nodes[vi].id)) {
                centerOnNode(G, G.nodes[vi], canvasSz);
                break;
            }
        }
        G.centerPending = false;
    }
}

// Hierarchy / list view — full graph (search finds hidden internals too).
// Sorted by kind order then label; search filters the rows. Click selects.
// Double-click a structural row drills in (mirrors canvas).
void drawListView(AppState& /*st*/, ImVec2 canvasSz) {
    auto& G = S();
    const float Sc = ui::uiScale;

    ImGui::BeginChild("##graph_list", canvasSz, ImGuiChildFlags_None);

    if (G.nodes.empty() && !G.loading) {
        ImGui::Dummy(ImVec2(0, 24 * Sc));
        const char* msg = G.err.empty() ? "No graph nodes yet" : G.err.c_str();
        ImGui::TextColored(ImVec4(palette.textDim[0], palette.textDim[1],
                                  palette.textDim[2], 1.0f), "%s", msg);
        ImGui::EndChild();
        return;
    }
    if (G.loading && G.nodes.empty()) {
        ImGui::Dummy(ImVec2(0, 24 * Sc));
        // Widget spinner (plmSpinner) — same accent-dot language as the rest of PLM.
        // Continuous redraw while loading is covered by st.rpc.busy() in the frame loop.
        plmSpinner("Loading graph");
        ImGui::EndChild();
        return;
    }

    // Stable order: kind order, then label, then id.
    std::vector<int> order(G.nodes.size());
    for (int i = 0; i < (int)order.size(); ++i) order[i] = i;
    std::sort(order.begin(), order.end(), [&](int a, int b) {
        int ra = kindOrder(G.nodes[a].kind), rb = kindOrder(G.nodes[b].kind);
        if (ra != rb) return ra < rb;
        if (G.nodes[a].label != G.nodes[b].label)
            return G.nodes[a].label < G.nodes[b].label;
        return G.nodes[a].id < G.nodes[b].id;
    });

    const bool filtering = G.search[0] != 0;
    ImGuiListClipper clipper;
    // Pre-filter indices so clipper sees only visible rows.
    std::vector<int> visible;
    visible.reserve(order.size());
    for (int i : order) {
        if (filtering && !matchesSearch(G.nodes[i], G.search)) continue;
        visible.push_back(i);
    }
    clipper.Begin((int)visible.size());
    while (clipper.Step()) {
        for (int row = clipper.DisplayStart; row < clipper.DisplayEnd; ++row) {
            const GNode& n = G.nodes[visible[row]];
            const bool sel = (n.id == G.selectedId);
            const bool inBlast = G.blast.count(n.id) > 0;

            ImGui::PushID(n.id.c_str());
            // Kind swatch
            ImVec2 p = ImGui::GetCursorScreenPos();
            float r = 5.0f * Sc;
            ImGui::GetWindowDrawList()->AddCircleFilled(
                ImVec2(p.x + r + 2 * Sc, p.y + ImGui::GetTextLineHeight() * 0.5f),
                r, kindColor(n.kind), 12);
            ImGui::Dummy(ImVec2(r * 2 + 8 * Sc, 0));
            ImGui::SameLine(0, 0);

            ImGui::PushStyleColor(ImGuiCol_Text,
                ImGui::ColorConvertU32ToFloat4(kindColor(n.kind)));
            ImGui::Text("[%s]", kindShort(n.kind));
            ImGui::PopStyleColor();
            ImGui::SameLine();

            // Selectable label
            if (sel) {
                ImGui::PushStyleColor(ImGuiCol_Header,
                    ImVec4(palette.accent[0], palette.accent[1],
                           palette.accent[2], 0.35f));
            } else if (inBlast) {
                ImGui::PushStyleColor(ImGuiCol_Header,
                    ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                           palette.statusWarn[2], 0.30f));
            }
            if (ImGui::Selectable(n.label.c_str(), sel || inBlast,
                                  ImGuiSelectableFlags_SpanAllColumns)) {
                G.selectedId = n.id;
                G.selectedEdge = -1;
                G.blast.clear();
            }
            if (ImGui::IsItemHovered() && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)
                && isDrillableKind(n.kind) && G.scopeDocId.empty()) {
                setScope(G, n.id);
            }
            if (sel || inBlast) ImGui::PopStyleColor();

            if (!n.path.empty()) {
                ImGui::SameLine();
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                           palette.textDisabled[2], 1.0f));
                ImGui::TextUnformatted(n.path.c_str());
                ImGui::PopStyleColor();
            }
            ImGui::PopID();
        }
    }
    clipper.End();

    ImGui::EndChild();
}

}  // namespace

// ---- public ------------------------------------------------------------------

bool plmGraphAvailable() { return S().available; }

void plmGraphOnProjectChanged() {
    auto& G = S();
    maybeSaveLayout(G, /*force=*/true);  // final solve-state save for old project
    G.nodes.clear();
    G.edges.clear();
    G.blast.clear();
    G.selectedId.clear();
    G.hoverId.clear();
    G.err.clear();
    G.generatedAt.clear();
    G.layoutDirty = true;
    G.loading = false;
    G.blastLoading = false;
    G.projectDir.clear();
    G.lastFetch = -1e9;
    G.panX = G.panY = 0.0f;
    G.zoom = 1.0f;
    G.selectedEdge = -1;
    G.hiddenKinds.clear();
    G.linkArmSrc.clear();
    G.linkPopupOpen = false;
    G.linkPopupDst.clear();
    G.linkNote[0] = 0;
    G.truncated = false;
    G.nodeCountFull = 0;
    G.edgeCountFull = 0;
    G.listView = false;
    G.listViewUserOverride = false;
    // Scope / visible caches — never open a new project inside a stale drill.
    G.scopeDocId.clear();
    G.visibleIdx.clear();
    G.visibleEdges.clear();
    G.descCounts.clear();
    G.idIndex.clear();
    G.containsParent.clear();
    // Force sim + interaction + search cache.
    G.force.clear();
    G.forceSlot.clear();
    G.dragNode = -1;
    G.dragCandidate = -1;
    G.fitOnSettle = false;
    G.matchIds.clear();
    G.visMatches = 0;
    G.allMatches = 0;
    G.matchDirty = true;
    // Probe + solve-state cache belong to the old project.
    G.mtimesMax = 0.0;
    G.sourcesCount = 0;
    G.layoutCache.clear();
    G.layoutCacheLoaded = false;
    G.lastLayoutSave = -1e9;
    G.wasSettled = true;
    G.userNavigated = false;
    // hideArchive stays — user preference across projects.
    // Keep available flag — method absence is session-wide, not per-project.
}

void plmGraphFocus(const std::string& query) {
    auto& G = S();
    std::snprintf(G.search, sizeof(G.search), "%s", query.c_str());
    G.focusPending = query;
    G.centerPending = !query.empty();
    G.matchDirty = true;
    G.lastFetch = -1e9;  // force a refetch on the next draw
}

void plmDrawGraphPage(AppState& st) {
    auto& G = S();
    const float S = ui::uiScale;

    if (!G.available) {
        // Should not be reached (rail hides us); belt-and-suspenders.
        plmEmptyState("[ ]", "Graph unavailable",
                      "This Chudware build does not expose the CAD graph service.");
        return;
    }

    // Project change → drop caches.
    const std::string proj = st.plmProjectPath();
    if (proj != G.projectDir && !G.loading) {
        // Only wipe when we have a previous project; first paint just fetches.
        if (!G.projectDir.empty()) plmGraphOnProjectChanged();
        G.projectDir = proj;
    }

    // Auto-refresh when the OS app regains focus. Use force=false so a warm
    // ME489-scale cache can serve without a full rebuild (mtime fast-path).
    const bool lost = ImGui::GetIO().AppFocusLost;
    if (G.wasAppFocusLost && !lost)
        requestGraph(st, /*force=*/false);
    G.wasAppFocusLost = lost;

    requestGraph(st, /*force=*/false);

    // ---- toolbar -------------------------------------------------------------
    ImGui::SetNextItemWidth(220 * S);
    if (ImGui::InputTextWithHint("##graph_search", "Search nodes…",
                                 G.search, sizeof(G.search))) {
        G.centerPending = G.search[0] != 0;
        G.matchDirty = true;
        G.force.reheat(0.12f);  // matched nodes draw the eye as they settle
    }
    ImGui::SameLine();
    if (graphGlassButton("##graph_refresh", G.loading ? "Refreshing…" : "Refresh",
                         false, 110 * S) && !G.loading) {
        requestGraph(st, /*force=*/true);
    }
    ImGui::SameLine();
    if (graphGlassButton("##graph_fit", "Fit", false, 70 * S)) {
        // Fit is applied next canvas draw via token bump.
        G.layoutToken++;
    }
    ImGui::SameLine();
    // Hide archive/stale — ON by default; toggling re-fetches with
    // include_archive flipped so the backend walk policy matches the UI.
    {
        bool hide = G.hideArchive;
        if (ImGui::Checkbox("Hide archive/stale", &hide) && hide != G.hideArchive) {
            G.hideArchive = hide;
            G.lastFetch = -1e9;
            requestGraph(st, /*force=*/true);
        }
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) {
            ImGui::SetTooltip(
                "Exclude _archive_stale / _archive / archive / .trash\n"
                "directories from the graph (default ON).");
        }
    }
    ImGui::SameLine();
    // View mode: list vs layered DAG. Auto-list only if visible set is huge.
    {
        const char* modeLabel = G.listView ? "List view" : "Graph view";
        if (graphGlassButton("##graph_view_mode", modeLabel, false, 100 * S)) {
            G.listView = !G.listView;
            G.listViewUserOverride = true;
            if (!G.listView) G.layoutDirty = true;
        }
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) {
            ImGui::SetTooltip(
                "Graph view animates the current scope as a live force-directed\n"
                "layout (drag nodes to rearrange). List view shows every\n"
                "loaded node (including features).");
        }
    }
    if (!G.generatedAt.empty()) {
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::AlignTextToFramePadding();
        ImGui::Text("generated %s", G.generatedAt.c_str());
        ImGui::PopStyleColor();
    }
    if (!G.err.empty() && !G.nodes.empty()) {
        ImGui::SameLine();
        plmError(G.err);
    }

    // ---- breadcrumb: Project › <doc> ----------------------------------------
    {
        ImGui::AlignTextToFramePadding();
        ImGui::PushStyleColor(ImGuiCol_Text,
            G.scopeDocId.empty()
                ? ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f)
                : ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        if (ImGui::SmallButton("▣ Project##bc_root")) {
            if (!G.scopeDocId.empty()) setScope(G, "");
        }
        ImGui::PopStyleColor();
        if (!G.scopeDocId.empty()) {
            ImGui::SameLine(0, 4 * S);
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                       palette.textDisabled[2], 1.0f));
            ImGui::TextUnformatted("›");
            ImGui::PopStyleColor();
            ImGui::SameLine(0, 4 * S);
            std::string lab = G.scopeDocId;
            auto it = G.idIndex.find(G.scopeDocId);
            if (it != G.idIndex.end()) lab = G.nodes[it->second].label;
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
            ImGui::TextUnformatted(lab.c_str());
            ImGui::PopStyleColor();
        }
    }

    // Honest counts: visible vs loaded vs full.
    if (!G.nodes.empty()) {
        const int visN = (int)G.visibleIdx.size();
        const int loadN = (int)G.nodes.size();
        const int fullN = G.nodeCountFull > 0 ? G.nodeCountFull : loadN;
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        if (G.scopeDocId.empty()) {
            if (visN < fullN)
                ImGui::Text("%d of %d nodes visible · drill into a document for features",
                            visN, fullN);
            else
                ImGui::Text("%d nodes · %d edges", visN, (int)G.visibleEdges.size());
        } else {
            ImGui::Text("%d nodes in document · %d of %d loaded",
                        visN, loadN, fullN);
        }
        ImGui::PopStyleColor();
        if (G.truncated || loadN < fullN) {
            ImGui::SameLine();
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                       palette.statusWarn[2], 1.0f));
            ImGui::Text("(loaded %d/%d)", loadN, fullN);
            ImGui::PopStyleColor();
        }
    }

    // Hidden search-match hint (matches outside the current visible scope).
    if (G.search[0] != 0 && !G.nodes.empty()) {
        if (G.matchDirty) rebuildMatchCache(G);
        const int hiddenMatches = G.allMatches - G.visMatches;
        if (hiddenMatches > 0) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                       palette.textDisabled[2], 1.0f));
            ImGui::Text("+%d matches inside documents — switch to List view or drill in",
                        hiddenMatches);
            ImGui::PopStyleColor();
        }
    }

    // ---- edge-kind filter row -----------------------------------------------
    if (graphGlassButton("##links_only", "Links only", false, 0.0f)) {
        G.hiddenKinds.clear();
        G.hiddenKinds.insert("CONTAINS");  // hide containment → the link web
        G.layoutDirty = true;  // hidden kinds exert no spring force
    }
    ImGui::SameLine();
    if (graphGlassButton("##all_kinds", "All", false, 0.0f)) {
        G.hiddenKinds.clear();
        G.layoutDirty = true;
    }
    for (const char* k : kEdgeKinds) {
        ImGui::SameLine();
        bool vis = !G.hiddenKinds.count(k);
        ImU32 tc = vis ? edgeColor(k, 1.0f) : Palette::toU32(palette.textDisabled);
        ImGui::PushStyleColor(ImGuiCol_Text, ImGui::ColorConvertU32ToFloat4(tc));
        if (ImGui::SmallButton(k)) {
            if (vis) G.hiddenKinds.insert(k); else G.hiddenKinds.erase(k);
            G.layoutDirty = true;
        }
        ImGui::PopStyleColor();
    }

    ImGui::Dummy(ImVec2(0, 4 * S));

    // ---- body: canvas/list + side panel -------------------------------------
    const float gap = 10.0f * S;
    const float sideW = 260.0f * S;
    const float bodyH = ImGui::GetContentRegionAvail().y;
    const float canvasW = std::max(120.0f * S,
                                   ImGui::GetContentRegionAvail().x - sideW - gap);

    ImGui::BeginChild("##graph_body", ImVec2(0, bodyH), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);

    ImGui::BeginChild("##graph_canvas_host", ImVec2(canvasW, 0), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    ImVec2 csz = ImGui::GetContentRegionAvail();
    if (G.listView)
        drawListView(st, csz);
    else
        drawCanvas(st, csz);
    ImGui::EndChild();

    ImGui::SameLine(0, gap);
    drawSidePanel(st, sideW);

    ImGui::EndChild();

    // ---- link authoring popup (outside the canvas child) --------------------
    if (G.linkPopupOpen) {
        ImGui::OpenPopup("Add link##graph_link");
        G.linkPopupOpen = false;  // consume: the popup now owns its lifetime
    }
    if (ImGui::BeginPopup("Add link##graph_link")) {
        ImGui::TextUnformatted("New link");
        ImGui::SetNextItemWidth(180 * S);
        ImGui::Combo("kind##lk", &G.linkKindIdx, kEdgeKinds,
                     IM_ARRAYSIZE(kEdgeKinds));
        ImGui::SetNextItemWidth(220 * S);
        ImGui::InputTextWithHint("##lknote", "note (optional)",
                                 G.linkNote, sizeof(G.linkNote));
        if (ImGui::Button("Create")) {
            int ki = std::clamp(G.linkKindIdx, 0,
                                (int)IM_ARRAYSIZE(kEdgeKinds) - 1);
            requestLinkAdd(st, G.linkArmSrc, G.linkPopupDst, kEdgeKinds[ki],
                           G.linkNote);
            G.linkArmSrc.clear(); G.linkPopupDst.clear(); G.linkNote[0] = 0;
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel")) {
            G.linkArmSrc.clear(); G.linkPopupDst.clear();
            ImGui::CloseCurrentPopup();
        }
        ImGui::EndPopup();
    }
    // Esc cancels an armed link when no popup is open.
    if (!G.linkArmSrc.empty() && !ImGui::IsPopupOpen("Add link##graph_link") &&
        ImGui::IsKeyPressed(ImGuiKey_Escape))
        G.linkArmSrc.clear();
}

}  // namespace ui
