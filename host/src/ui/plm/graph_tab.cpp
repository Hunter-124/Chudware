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
#include "graph_thumbs.h"
#include "plm_hub.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <map>
#include <string>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "imgui.h"

#include "core/app_state.h"
#include "core/async_rpc.h"
#include "gl_compat.h"
#include "miniz.h"
#include "stb_image.h"
#include "ui/icons.h"
#include "ui_theme.h"

namespace ui {
namespace {

// ---- row shapes
// --------------------------------------------------------------

struct GNode {
  std::string id, kind, label, path, docId, origin;
  std::string metaSummary;    // short key=val dump for tooltip/panel
  std::string metaDh;         // content hash (blake2b-8) for thumb freshness
  double metaMt = 0.0;        // source mtime (fallback freshness signal)
  float lx = 0.0f, ly = 0.0f; // layout coords (graph space)
  float lz = 0.0f;            // depth coord (3D projection mode only)
  float radius = 14.0f;
  float bubbleR = 0.0f; // packed containment bubble (0 = leaf/no children)
  // Real model coordinates from the persisted assembly transform (meta.wp);
  // the 3D mode overlays these so instances lay out like the actual model.
  float wx = 0.0f, wy = 0.0f, wz = 0.0f;
  bool hasW = false;
};

// Persisted / stashed node position (x, y + depth z).
struct Pos3 {
  float x = 0.0f, y = 0.0f, z = 0.0f;
};

struct GEdge {
  std::string src, dst, kind, origin;
  std::string metaSummary; // short key=val dump for label/panel
  std::string dimLabel;    // compact value label for dimensional edges
  nlohmann::json meta;     // full meta for the side panel
  int aggCount = 1;        // >1 when project-view edge is an aggregation
  int srcEdgeIdx = -1;     // original edges[] index (representative for panel)
};

// Drawn edge after scope filtering / project-view aggregation.
struct VEdge {
  std::string src, dst, kind, origin;
  std::string dimLabel;
  int aggCount = 1;
  int srcEdgeIdx = -1; // index into GraphTabState::edges
};

// Per structural node: hidden descendant tallies (features / sketches / other).
struct DescCounts {
  int features = 0;
  int sketches = 0;
  int other = 0;
  int total() const { return features + sketches + other; }
};

struct GraphTabState {
  bool available = true; // false after permanent unknown_method
  bool loading = false;
  bool blastLoading = false;
  bool layoutDirty = true;
  bool centerPending = false;
  std::string projectDir;
  std::string err;
  std::string generatedAt;
  std::vector<GNode> nodes;
  std::vector<GEdge> edges;
  std::unordered_set<std::string> blast; // highlighted node ids (impact set)
  std::string blastEpicenter;            // node id rings expand from
  double blastT0 = 0.0;                  // ImGui::GetTime() when blast landed
  std::string selectedId;
  std::string hoverId;
  char search[128] = {};
  float panX = 0.0f, panY = 0.0f, zoom = 1.0f;
  bool wasAppFocusLost = false;
  double lastFetch = -1e9;
  // One-shot fit request — set ONLY by the explicit Fit button. Nothing
  // else may touch the user's view (data polls/relayouts never refit).
  bool fitRequested = false;
  // edge kind filter — hidden kinds skip drawing (all visible by default)
  std::unordered_set<std::string> hiddenKinds;
  // edge selection indexes into visibleEdges (not the full edges vector)
  int selectedEdge = -1;
  // link authoring
  std::string linkArmSrc; // node id armed as the link source
  bool linkPopupOpen = false;
  std::string linkPopupDst; // node id chosen as the target
  int linkKindIdx = 4;      // default REFERENCES (see kEdgeKinds)
  char linkNote[192] = {};
  std::string scopePending; // plmGraphScopeNode request applied on next draw
  std::string focusPending; // plmGraphFocus request to apply on next draw
  // ME489-scale guards ----------------------------------------------------
  bool hideArchive = true;
  // Project-view cleanup: skip nodes with no visible links (scattered dots).
  bool hideUnconnected = true;
  bool truncated = false;
  int nodeCountFull = 0; // full graph size (pre-cap)
  int edgeCountFull = 0;
  bool listView = false;
  bool listViewUserOverride = false; // true once the user picks a mode
  // Grid view: structural nodes (assembly/part/doc) of the current scope as
  // square iso-preview cells. Mutually exclusive with listView.
  bool gridView = false;
  // Assembly structure view (project scope only): assemblies + parts with
  // CONTAINS / INSTANCE_OF / MATES / DEPENDS_ON, laid out as layered tree.
  bool asmView = false;
  // Hierarchy (balloon) layout: positions are pure deterministic structure
  // and the force sim stays FROZEN (reheat 0) so the tree never homogenizes
  // into a blob. Saved/restored across asm-view and scope swaps via
  // projWasHierarchy.
  bool hierarchyLayout = false;
  bool projWasHierarchy = false; // saved across asm-view/scope swaps
  // 3D projection of the (still 2D-solved) layout; right-drag orbits.
  bool view3d = false;
  float orbitYaw = 0.55f;
  float orbitPitch = -0.30f;
  // In-session per-view position stashes — toggling views swaps these in so
  // each view keeps its own arrangement without clobbering the other's.
  std::unordered_map<std::string, Pos3> projPosStash;
  std::unordered_map<std::string, Pos3> asmPosStash;
  // 2D positions stashed while the 3D world-position overlay is active.
  std::unordered_map<std::string, Pos3> preWorldStash;

  // Scope: "" = project view; else the structural doc/asm/part node drilled
  // into.
  std::string scopeDocId;
  // Ancestor chain for nested drills ("" entries = came from project view).
  std::vector<std::string> scopeStack;
  // Decomposition scope: assembly → subassemblies/parts with instance
  // counts (bom_item INSTANCE_OF resolution). false = raw doc internals.
  bool scopeDecomp = false;
  // Instance quantity per child doc in decomposition scope (label badges).
  std::unordered_map<std::string, int> instCount;
  // Visible node indices (into nodes[]) for the active scope.
  std::vector<int> visibleIdx;
  // Scope-filtered / aggregated edges for drawing + hit-test.
  std::vector<VEdge> visibleEdges;
  // Hidden-descendant badges keyed by structural node id (project view).
  std::unordered_map<std::string, DescCounts> descCounts;
  // id → nodes[] index (rebuilt with data).
  std::unordered_map<std::string, int> idIndex;
  // CONTAINS parent: child id → parent id (first parent wins; rebuilt with
  // data).
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
  std::unordered_map<std::string, Pos3> layoutCache;
  bool layoutCacheLoaded = false;
  double lastLayoutSave = -1e9;
  bool wasSettled = true;
  // Once the user pans/zooms, never auto-fit again until scope/project change.
  bool userNavigated = false;
};

// List view auto-threshold (user can still force Graph view).
static constexpr int kListViewThreshold = 2000;

GraphTabState &S() {
  static GraphTabState s;
  return s;
}

// ---- helpers
// -----------------------------------------------------------------

bool isAbsent(const core::RpcResult &r) {
  if (!r.ok) {
    const std::string &e = r.err;
    if (e.find("unknown") != std::string::npos ||
        e.find("not found") != std::string::npos ||
        e.find("not_found") != std::string::npos ||
        e.find("method") != std::string::npos)
      return true;
    return false;
  }
  if (!r.result.is_object())
    return false;
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

std::string jstr(const nlohmann::json &j, const char *k) {
  auto it = j.find(k);
  if (it == j.end() || it->is_null())
    return "";
  if (it->is_string())
    return it->get<std::string>();
  return it->dump();
}

std::string summarizeMeta(const nlohmann::json &meta) {
  if (!meta.is_object() || meta.empty())
    return "";
  std::string out;
  int n = 0;
  for (auto it = meta.begin(); it != meta.end() && n < 6; ++it, ++n) {
    if (!out.empty())
      out += " · ";
    out += it.key();
    out += "=";
    if (it.value().is_string())
      out += it.value().get<std::string>();
    else if (it.value().is_number_integer())
      out += std::to_string(it.value().get<long long>());
    else if (it.value().is_number_float()) {
      char b[32];
      std::snprintf(b, sizeof(b), "%.4g", it.value().get<double>());
      out += b;
    } else if (it.value().is_boolean())
      out += it.value().get<bool>() ? "true" : "false";
    else
      out += it.value().dump();
  }
  if ((int)meta.size() > n)
    out += " …";
  return out;
}

// Structural kinds shown in project view (everything else is drill-down only).
bool isProjectKind(const std::string &k) {
  return k == "project" || k == "assembly" || k == "part" || k == "doc" ||
         k == "drawing" || k == "board" || k == "firmware" || k == "sim_study";
}

// Kinds a user can double-click to drill into.
bool isDrillableKind(const std::string &k) {
  return k == "assembly" || k == "part" || k == "doc" || k == "drawing" ||
         k == "board" || k == "firmware" || k == "sim_study";
}

// Stable kind order for list view grouping.
int kindOrder(const std::string &k) {
  if (k == "project")
    return 0;
  if (k == "assembly" || k == "doc")
    return 1;
  if (k == "part")
    return 2;
  if (k == "body" || k == "bom_item")
    return 3;
  if (k == "feature" || k == "sketch" || k == "mate" || k == "equation")
    return 4;
  if (k == "drawing" || k == "sim_study" || k == "board" || k == "firmware")
    return 5;
  return 3;
}

// Theme-token colours only (gui_lint theme rule).
ImU32 kindColor(const std::string &k, float a = 1.0f) {
  const float *c = palette.accent;
  if (k == "project")
    c = palette.accent;
  else if (k == "assembly" || k == "doc")
    c = palette.axisZ;
  else if (k == "part" || k == "body")
    c = palette.axisY;
  else if (k == "feature" || k == "sketch")
    c = palette.sketchUnder;
  else if (k == "equation")
    c = palette.statusOk;
  else if (k == "mate")
    c = palette.axisX;
  else if (k == "bom_item")
    c = palette.statusWarn;
  else if (k == "drawing")
    c = palette.diffHunk;
  else if (k == "sim_study")
    c = palette.simMesh;
  else if (k == "board" || k == "firmware")
    c = palette.sketchExternal;
  else
    c = palette.textDim;
  return ImGui::GetColorU32(ImVec4(c[0], c[1], c[2], a));
}

ImU32 edgeColor(const std::string &k, float a = 0.70f) {
  const float *c = palette.textDim;
  if (k == "CONTAINS")
    c = palette.textDim;
  else if (k == "DEPENDS_ON")
    c = palette.statusWarn;
  else if (k == "MATES")
    c = palette.axisX;
  else if (k == "INSTANCE_OF")
    c = palette.axisY;
  else if (k == "REFERENCES")
    c = palette.diffHunk;
  else if (k == "DERIVES_FROM")
    c = palette.sketchExternal;
  else if (k == "DOCUMENTS")
    c = palette.accent;
  else if (k == "SIMULATES")
    c = palette.simMesh;
  else if (k == "MANUFACTURES")
    c = palette.statusOk;
  return ImGui::GetColorU32(ImVec4(c[0], c[1], c[2], a));
}

const char *kindShort(const std::string &k) {
  if (k == "project")
    return "PROJ";
  if (k == "assembly")
    return "ASM";
  if (k == "doc")
    return "DOC";
  if (k == "part")
    return "PART";
  if (k == "body")
    return "BODY";
  if (k == "feature")
    return "FEAT";
  if (k == "sketch")
    return "SK";
  if (k == "mate")
    return "MATE";
  if (k == "bom_item")
    return "BOM";
  if (k == "drawing")
    return "DWG";
  if (k == "sim_study")
    return "SIM";
  if (k == "board")
    return "PCB";
  if (k == "firmware")
    return "FW";
  if (k == "equation")
    return "EQ";
  return "NODE";
}

// Canonical edge-kind vocabulary (contracts/cad_graph.md) — filter chips + the
// link-authoring combo iterate this. Index 4 == REFERENCES (linkKindIdx
// default).
static const char *kEdgeKinds[] = {
    "CONTAINS",     "DEPENDS_ON", "MATES",     "INSTANCE_OF",  "REFERENCES",
    "DERIVES_FROM", "DOCUMENTS",  "SIMULATES", "MANUFACTURES",
};

// Compact value label for a dimensional edge, e.g. "width=12mm" or "25mm".
std::string edgeDimLabel(const nlohmann::json &meta) {
  if (!meta.is_object())
    return "";
  std::string dim;
  if (auto it = meta.find("dim"); it != meta.end() && it->is_string())
    dim = it->get<std::string>();
  std::string val;
  if (auto it = meta.find("value"); it != meta.end() && !it->is_null()) {
    if (it->is_number_integer())
      val = std::to_string(it->get<long long>());
    else if (it->is_number_float()) {
      char b[32];
      std::snprintf(b, sizeof(b), "%.4g", it->get<double>());
      val = b;
    } else if (it->is_string())
      val = it->get<std::string>();
  }
  std::string unit;
  if (auto it = meta.find("unit"); it != meta.end() && it->is_string())
    unit = it->get<std::string>();
  if (val.empty() && dim.empty())
    return "";
  std::string out;
  if (!dim.empty()) {
    // trim a "fid." prefix so the label reads as the dimension name
    auto dot = dim.rfind('.');
    out = (dot == std::string::npos) ? dim : dim.substr(dot + 1);
  }
  if (!val.empty()) {
    if (!out.empty())
      out += "=";
    out += val;
    out += unit;
  }
  return out;
}

// ---- scope / visibility / aggregation ---------------------------------------

// Prefer CONTAINS-ancestor walk to the nearest project-view structural node.
// Fall back to id-prefix decoding ("feature:doc:REL:FID" → "doc:REL").
std::string resolveOwnerId(const GraphTabState &G, const std::string &nodeId) {
  auto it = G.idIndex.find(nodeId);
  if (it == G.idIndex.end())
    return {};
  const GNode &self = G.nodes[it->second];
  if (isProjectKind(self.kind))
    return self.id;

  // Walk CONTAINS parents; pick nearest structural ancestor.
  std::unordered_set<std::string> seen;
  std::string cur = nodeId;
  while (!cur.empty() && !seen.count(cur)) {
    seen.insert(cur);
    auto pit = G.containsParent.find(cur);
    if (pit == G.containsParent.end())
      break;
    cur = pit->second;
    auto nit = G.idIndex.find(cur);
    if (nit == G.idIndex.end())
      break;
    if (isProjectKind(G.nodes[nit->second].kind))
      return cur;
  }

  // Id-prefix fallback: "kind:doc:REL:…" → "doc:REL"
  // Also handles "eq:doc:REL:name".
  auto c1 = nodeId.find(':');
  if (c1 == std::string::npos)
    return {};
  auto c2 = nodeId.find(':', c1 + 1);
  if (c2 == std::string::npos)
    return {};
  auto c3 = nodeId.find(':', c2 + 1);
  if (c3 == std::string::npos)
    return {};
  std::string cand = nodeId.substr(c1 + 1, c3 - (c1 + 1)); // "doc:REL"
  if (G.idIndex.count(cand))
    return cand;
  // Some ids are "feature:<docId>:fid" where docId itself has no extra colon.
  std::string cand2 = nodeId.substr(c1 + 1, c2 - (c1 + 1));
  if (G.idIndex.count(cand2) &&
      isProjectKind(G.nodes[G.idIndex.at(cand2)].kind))
    return cand2;
  // doc_id field on the node
  if (!self.docId.empty() && G.idIndex.count(self.docId))
    return self.docId;
  return {};
}

void rebuildContainsParent(GraphTabState &G) {
  G.containsParent.clear();
  for (const auto &e : G.edges) {
    if (e.kind != "CONTAINS")
      continue;
    // First parent wins (deterministic: edges stay in load order; stable
    // enough).
    if (!G.containsParent.count(e.dst))
      G.containsParent[e.dst] = e.src;
  }
}

void rebuildIdIndex(GraphTabState &G) {
  G.idIndex.clear();
  G.idIndex.reserve(G.nodes.size() * 2);
  for (int i = 0; i < (int)G.nodes.size(); ++i)
    G.idIndex[G.nodes[i].id] = i;
}

// Transitive CONTAINS descendants → per-structural-node feature/sketch badges.
void rebuildDescCounts(GraphTabState &G) {
  G.descCounts.clear();
  // Build adjacency: parent → children via CONTAINS.
  std::unordered_map<std::string, std::vector<std::string>> kids;
  kids.reserve(G.nodes.size());
  for (const auto &e : G.edges) {
    if (e.kind != "CONTAINS")
      continue;
    kids[e.src].push_back(e.dst);
  }
  // For each structural node, DFS over CONTAINS descendants.
  for (const auto &n : G.nodes) {
    if (!isProjectKind(n.kind) || n.kind == "project")
      continue;
    DescCounts dc;
    std::vector<std::string> stack = kids[n.id];
    std::unordered_set<std::string> seen;
    while (!stack.empty()) {
      std::string id = std::move(stack.back());
      stack.pop_back();
      if (!seen.insert(id).second)
        continue;
      auto it = G.idIndex.find(id);
      if (it == G.idIndex.end())
        continue;
      const GNode &c = G.nodes[it->second];
      if (c.kind == "feature")
        ++dc.features;
      else if (c.kind == "sketch")
        ++dc.sketches;
      else if (!isProjectKind(c.kind))
        ++dc.other;
      // Always walk further (assemblies may nest parts, etc.).
      auto kit = kids.find(id);
      if (kit != kids.end())
        for (const auto &ch : kit->second)
          stack.push_back(ch);
    }
    if (dc.total() > 0)
      G.descCounts[n.id] = dc;
  }
}

// Compact badge text: "12F · 3SK" or "·47" when mixed/other-only.
std::string formatDescBadge(const DescCounts &dc) {
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
bool hasBomChildren(GraphTabState &G, const std::string &id);
void clearBlast(GraphTabState &G);

void rebuildVisible(GraphTabState &G) {
  G.visibleIdx.clear();
  G.visibleEdges.clear();
  G.selectedEdge = -1;

  if (G.nodes.empty())
    return;

  std::unordered_set<std::string> visIds;

  if (G.scopeDocId.empty()) {
    // ---- PROJECT VIEW ---------------------------------------------------
    for (int i = 0; i < (int)G.nodes.size(); ++i) {
      // The project-root node itself is not drawn — it is just a
      // fan-out hub for CONTAINS edges and clutters the canvas.
      if (isProjectKind(G.nodes[i].kind) && G.nodes[i].kind != "project") {
        G.visibleIdx.push_back(i);
        visIds.insert(G.nodes[i].id);
      }
    }
    // Deterministic node order by id.
    std::sort(G.visibleIdx.begin(), G.visibleIdx.end(),
              [&](int a, int b) { return G.nodes[a].id < G.nodes[b].id; });

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
      const GEdge &e = G.edges[ei];
      std::string os = e.src, od = e.dst;
      bool srcVis = visIds.count(os) > 0;
      bool dstVis = visIds.count(od) > 0;
      if (!srcVis) {
        os = resolveOwnerId(G, e.src);
        if (os.empty() || !visIds.count(os))
          continue;
      }
      if (!dstVis) {
        od = resolveOwnerId(G, e.dst);
        if (od.empty() || !visIds.count(od))
          continue;
      }
      if (os == od)
        continue; // drop self-loops after aggregation

      std::string key = os;
      key.push_back('\0');
      key += od;
      key.push_back('\0');
      key += e.kind;

      auto &a = aggs[key];
      if (a.count == 0) {
        a.src = os;
        a.dst = od;
        a.kind = e.kind;
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
    for (const auto &kv : aggs)
      keys.push_back(kv.first);
    std::sort(keys.begin(), keys.end());
    G.visibleEdges.reserve(keys.size());
    for (const auto &k : keys) {
      const Agg &a = aggs[k];
      VEdge ve;
      ve.src = a.src;
      ve.dst = a.dst;
      ve.kind = a.kind;
      ve.origin = a.origin;
      ve.dimLabel = a.dimLabel;
      ve.aggCount = a.count;
      ve.srcEdgeIdx = a.srcEdgeIdx;
      G.visibleEdges.push_back(std::move(ve));
    }

    // Assembly structure view: only assemblies/parts/docs and the
    // structural relation web between them (containment, instances,
    // mates, dependencies). Drawings/sim/ecad + reference noise drop out.
    if (G.asmView) {
      auto asmNode = [](const std::string &k) {
        return k == "assembly" || k == "part" || k == "doc";
      };
      auto asmEdge = [](const std::string &k) {
        return k == "CONTAINS" || k == "INSTANCE_OF" || k == "MATES" ||
               k == "DEPENDS_ON";
      };
      std::vector<int> kept;
      kept.reserve(G.visibleIdx.size());
      std::unordered_set<std::string> keepIds;
      for (int i : G.visibleIdx) {
        if (!asmNode(G.nodes[i].kind))
          continue;
        kept.push_back(i);
        keepIds.insert(G.nodes[i].id);
      }
      G.visibleIdx.swap(kept);
      std::vector<VEdge> ke;
      ke.reserve(G.visibleEdges.size());
      for (auto &ve : G.visibleEdges) {
        if (!asmEdge(ve.kind))
          continue;
        if (!keepIds.count(ve.src) || !keepIds.count(ve.dst))
          continue;
        ke.push_back(std::move(ve));
      }
      G.visibleEdges.swap(ke);
    }

    // "Hide unconnected": drop nodes with no visible edges — they render
    // as scattered dots with no context. List view still shows them.
    if (G.hideUnconnected) {
      std::unordered_set<std::string> linked;
      linked.reserve(G.visibleEdges.size() * 2);
      for (const auto &ve : G.visibleEdges) {
        linked.insert(ve.src);
        linked.insert(ve.dst);
      }
      std::vector<int> kept;
      kept.reserve(G.visibleIdx.size());
      for (int i : G.visibleIdx)
        if (linked.count(G.nodes[i].id))
          kept.push_back(i);
      G.visibleIdx.swap(kept);
    }
  } else if (G.scopeDecomp) {
    // ---- ASSEMBLY DECOMPOSITION ----------------------------------------
    // scope root + resolved child docs (bom_item INSTANCE_OF targets,
    // aggregated with instance counts; direct doc/part/assembly kids).
    if (!G.idIndex.count(G.scopeDocId)) {
      G.scopeDocId.clear();
      G.scopeStack.clear();
      G.scopeDecomp = false;
      rebuildVisible(G);
      return;
    }
    G.instCount.clear();
    const std::string &root = G.scopeDocId;
    visIds.insert(root);
    G.visibleIdx.push_back(G.idIndex[root]);

    std::vector<std::pair<std::string, int>> targets; // (docId, qty)
    std::unordered_map<std::string, int> tpos;
    auto addTarget = [&](const std::string &t, int qty) {
      if (t == root || !G.idIndex.count(t))
        return;
      auto it = tpos.find(t);
      if (it == tpos.end()) {
        tpos[t] = (int)targets.size();
        targets.push_back({t, qty});
      } else {
        targets[it->second].second += qty;
      }
    };
    for (const auto &e : G.edges) {
      if (e.kind != "CONTAINS" || e.src != root)
        continue;
      auto kit = G.idIndex.find(e.dst);
      if (kit == G.idIndex.end())
        continue;
      const std::string &kkind = G.nodes[kit->second].kind;
      if (kkind == "bom_item") {
        for (const auto &ie : G.edges)
          if (ie.kind == "INSTANCE_OF" && ie.src == e.dst)
            addTarget(ie.dst, 1);
      } else if (kkind == "part" || kkind == "assembly" || kkind == "doc") {
        addTarget(e.dst, 1);
      }
    }
    std::sort(targets.begin(), targets.end(),
              [](const auto &a, const auto &b) { return a.first < b.first; });
    for (const auto &[t, qty] : targets) {
      visIds.insert(t);
      G.visibleIdx.push_back(G.idIndex[t]);
      G.instCount[t] = qty;
      VEdge ve;
      ve.src = root;
      ve.dst = t;
      ve.kind = "INSTANCE_OF";
      ve.origin = "extracted";
      ve.aggCount = qty;
      ve.srcEdgeIdx = -1; // synthetic; side panel null-guards this
      G.visibleEdges.push_back(std::move(ve));
    }
    std::sort(G.visibleIdx.begin(), G.visibleIdx.end(),
              [&](int a, int b) { return G.nodes[a].id < G.nodes[b].id; });
  } else if (!G.scopeDecomp && G.idIndex.count(G.scopeDocId) &&
             (G.nodes[G.idIndex[G.scopeDocId]].kind == "assembly" ||
              hasBomChildren(G, G.scopeDocId))) {
    // ---- DOC INTERNALS (assembly Internals toggle) ---------------------
    // Explicit feature/sketch/mate dump — only when user opts into
    // Internals on a decomposable assembly scope.
    std::unordered_map<std::string, std::vector<std::string>> kids;
    for (const auto &e : G.edges) {
      if (e.kind != "CONTAINS")
        continue;
      kids[e.src].push_back(e.dst);
    }
    std::vector<std::string> stack = {G.scopeDocId};
    std::unordered_set<std::string> seen;
    while (!stack.empty()) {
      std::string id = std::move(stack.back());
      stack.pop_back();
      if (!seen.insert(id).second)
        continue;
      auto it = G.idIndex.find(id);
      if (it == G.idIndex.end())
        continue;
      G.visibleIdx.push_back(it->second);
      visIds.insert(id);
      auto kit = kids.find(id);
      if (kit != kids.end())
        for (const auto &ch : kit->second)
          stack.push_back(ch);
    }
    std::sort(G.visibleIdx.begin(), G.visibleIdx.end(),
              [&](int a, int b) { return G.nodes[a].id < G.nodes[b].id; });
    for (int ei = 0; ei < (int)G.edges.size(); ++ei) {
      const GEdge &e = G.edges[ei];
      if (!visIds.count(e.src) || !visIds.count(e.dst))
        continue;
      VEdge ve;
      ve.src = e.src;
      ve.dst = e.dst;
      ve.kind = e.kind;
      ve.origin = e.origin;
      ve.dimLabel = e.dimLabel;
      ve.aggCount = 1;
      ve.srcEdgeIdx = ei;
      G.visibleEdges.push_back(std::move(ve));
    }
  } else {
    // ---- REFERENCE ISOLATE (N-hop) ------------------------------------
    // Part/doc drill default: root + undirected 2-hop neighborhood along
    // reference/mate/depends/instance edges (and structural CONTAINS only).
    // Feature/sketch/body fan-out is intentionally excluded.
    if (!G.idIndex.count(G.scopeDocId)) {
      G.scopeDocId.clear();
      G.scopeStack.clear();
      rebuildVisible(G);
      return;
    }
    const std::string &root = G.scopeDocId;

    auto isolateEdgeOk = [&](const GEdge &e) -> bool {
      if (e.kind == "DEPENDS_ON" || e.kind == "REFERENCES" ||
          e.kind == "MATES" || e.kind == "DERIVES_FROM" ||
          e.kind == "INSTANCE_OF")
        return true;
      if (e.kind == "CONTAINS") {
        auto si = G.idIndex.find(e.src), di = G.idIndex.find(e.dst);
        if (si == G.idIndex.end() || di == G.idIndex.end())
          return false;
        return isProjectKind(G.nodes[si->second].kind) &&
               isProjectKind(G.nodes[di->second].kind);
      }
      return false;
    };

    // Undirected adjacency for the isolate walk.
    std::unordered_map<std::string, std::vector<std::string>> adj;
    adj.reserve(G.edges.size());
    for (const auto &e : G.edges) {
      if (!isolateEdgeOk(e))
        continue;
      if (!G.idIndex.count(e.src) || !G.idIndex.count(e.dst))
        continue;
      adj[e.src].push_back(e.dst);
      adj[e.dst].push_back(e.src);
    }

    // BFS depth <= 2 from root.
    std::unordered_map<std::string, int> dist;
    std::vector<std::string> q;
    dist[root] = 0;
    q.push_back(root);
    for (size_t qi = 0; qi < q.size(); ++qi) {
      const std::string &u = q[qi];
      const int d = dist[u];
      if (d >= 2)
        continue;
      auto ait = adj.find(u);
      if (ait == adj.end())
        continue;
      for (const auto &v : ait->second) {
        if (dist.count(v))
          continue;
        dist[v] = d + 1;
        q.push_back(v);
      }
    }

    for (const auto &[id, d] : dist) {
      (void)d;
      auto it = G.idIndex.find(id);
      if (it == G.idIndex.end())
        continue;
      if (G.nodes[it->second].kind == "project")
        continue; // never drawn
      G.visibleIdx.push_back(it->second);
      visIds.insert(id);
    }
    std::sort(G.visibleIdx.begin(), G.visibleIdx.end(),
              [&](int a, int b) { return G.nodes[a].id < G.nodes[b].id; });

    // Visible edges: both ends visible and kind allowed for isolate view.
    for (int ei = 0; ei < (int)G.edges.size(); ++ei) {
      const GEdge &e = G.edges[ei];
      if (!visIds.count(e.src) || !visIds.count(e.dst))
        continue;
      if (!isolateEdgeOk(e))
        continue;
      VEdge ve;
      ve.src = e.src;
      ve.dst = e.dst;
      ve.kind = e.kind;
      ve.origin = e.origin;
      ve.dimLabel = e.dimLabel;
      ve.aggCount = 1;
      ve.srcEdgeIdx = ei;
      G.visibleEdges.push_back(std::move(ve));
    }
  }

  G.layoutDirty = true;
  G.matchDirty = true;
}

// True when the node has bom_item children — i.e. it can be viewed as an
// assembly decomposition (subassemblies/parts) rather than raw internals.
bool hasBomChildren(GraphTabState &G, const std::string &id) {
  for (const auto &e : G.edges) {
    if (e.kind != "CONTAINS" || e.src != id)
      continue;
    auto it = G.idIndex.find(e.dst);
    if (it != G.idIndex.end() && G.nodes[it->second].kind == "bom_item")
      return true;
  }
  return false;
}

void setScope(GraphTabState &G, const std::string &docId);

// Nested drill navigation: drillInto pushes the current scope, scopeBack
// pops one level (empty string = project view). Stack invariant: it holds
// the ancestor chain, so depth == stack.size() when scoped.
void drillInto(GraphTabState &G, const std::string &id) {
  if (id.empty() || id == G.scopeDocId)
    return;
  G.scopeStack.push_back(G.scopeDocId);
  setScope(G, id);
}

void scopeBack(GraphTabState &G) {
  if (!G.scopeStack.empty()) {
    std::string up = std::move(G.scopeStack.back());
    G.scopeStack.pop_back();
    setScope(G, up);
  } else {
    setScope(G, "");
  }
}

void setScope(GraphTabState &G, const std::string &docId) {
  if (G.scopeDocId == docId)
    return;
  G.scopeDocId = docId;
  G.selectedId.clear();
  G.selectedEdge = -1;
  clearBlast(G);
  G.hoverId.clear();
  G.userNavigated = false; // fresh scope → allow one auto-fit again
  // Assemblies (or anything with bom_items) open in decomposition view.
  G.scopeDecomp = false;
  // Scopes run live sims; the balloon freeze resumes back in project view.
  if (docId.empty()) {
    G.hierarchyLayout = G.projWasHierarchy;
  } else {
    if (G.scopeDocId.empty())
      G.projWasHierarchy = G.hierarchyLayout;
    G.hierarchyLayout = false;
  }
  if (!docId.empty()) {
    auto it = G.idIndex.find(docId);
    if (it != G.idIndex.end() &&
        (G.nodes[it->second].kind == "assembly" || hasBomChildren(G, docId)))
      G.scopeDecomp = true;
  }
  rebuildVisible(G);
  // Fit on next canvas paint.
}

bool hasPosition(const GNode &n) {
  return n.lx != 0.0f || n.ly != 0.0f || n.lz != 0.0f;
}

// Stash live positions into the active view's stash (mode-aware).
void stashLivePositions(GraphTabState &G) {
  auto &stash = G.asmView ? G.asmPosStash : G.projPosStash;
  for (const auto &n : G.nodes) {
    if (!hasPosition(n))
      continue;
    stash[n.id] = {n.lx, n.ly, n.lz};
  }
}

// Assembly ⇄ project web toggle. Each view keeps its own arrangement
// in-session; entering a view with no stash reseeds deterministically.
void toggleAsmView(GraphTabState &G) {
  stashLivePositions(G);
  G.asmView = !G.asmView;
  // Asm view runs the live layered sim; preserve the project freeze.
  if (G.asmView) {
    G.projWasHierarchy = G.hierarchyLayout;
    G.hierarchyLayout = false;
  } else {
    G.hierarchyLayout = G.projWasHierarchy;
  }
  const auto &stash = G.asmView ? G.asmPosStash : G.projPosStash;
  for (auto &n : G.nodes) {
    auto it = stash.find(n.id);
    if (it != stash.end()) {
      n.lx = it->second.x;
      n.ly = it->second.y;
      n.lz = it->second.z;
    } else {
      n.lx = n.ly = n.lz = 0.0f; // force a fresh deterministic seed
    }
  }
  rebuildVisible(G); // sets layoutDirty
}

// ---- deterministic seeds ----------------------------------------------------

// Balloon hierarchy seed over the visible CONTAINS forest: each subtree gets
// a wedge of the circle proportional to its leaf count; per-depth radii grow
// so every level keeps >= hop arc spacing. PURE STRUCTURE — the force sim
// stays frozen in this mode (hierarchyLayout): force polish of any strength
// homogenizes hierarchy into a uniform blob (verified in sim bake-offs AND
// three rounds of live screenshots: ring, wedge, dandelion). Non-tree fresh
// nodes (the REFERENCES web) anchor on small phyllotaxis discs around their
// already-placed neighbors instead of fanning in from remote wells. z =
// containment depth — a volumetric cone in 3D mode. Deterministic: sorted
// children, golden angle, no RNG, O(N).
void hierarchySeed(GraphTabState &G,
                   const std::unordered_map<std::string, int> &visOrd,
                   const std::vector<std::vector<int>> &adjAll,
                   const std::vector<int> &comp,
                   const std::vector<std::pair<float, float>> &well,
                   const std::vector<char> &isFresh, float spacing,
                   std::vector<float> &outX, std::vector<float> &outY,
                   std::vector<float> &outZ) {
  const int V = (int)adjAll.size();
  outX.assign(static_cast<size_t>(V), 0.0f);
  outY.assign(static_cast<size_t>(V), 0.0f);
  outZ.assign(static_cast<size_t>(V), 0.0f);
  if (V == 0)
    return;
  constexpr float GOLDEN = 2.39996323f;
  const float hop = spacing * 0.9f;

  // CONTAINS forest in visOrd space (parent -> sorted children).
  std::vector<std::vector<int>> kids(static_cast<size_t>(V));
  std::vector<char> hasParent(static_cast<size_t>(V), 0);
  if (!G.hiddenKinds.count("CONTAINS")) {
    for (const auto &e : G.visibleEdges) {
      if (e.kind != "CONTAINS")
        continue;
      auto a = visOrd.find(e.src), b = visOrd.find(e.dst);
      if (a == visOrd.end() || b == visOrd.end() || a->second == b->second)
        continue;
      kids[static_cast<size_t>(a->second)].push_back(b->second);
      hasParent[static_cast<size_t>(b->second)] = 1;
    }
  }
  for (auto &k : kids) {
    std::sort(k.begin(), k.end());
    k.erase(std::unique(k.begin(), k.end()), k.end());
  }

  // Roots: visible children but no visible parent.
  std::vector<int> roots;
  for (int i = 0; i < V; ++i)
    if (!kids[static_cast<size_t>(i)].empty() &&
        !hasParent[static_cast<size_t>(i)])
      roots.push_back(i);

  // Depth + leaf counts (iterative preorder; cycle-guarded).
  std::vector<int> depth(static_cast<size_t>(V), -1),
      leaves(static_cast<size_t>(V), 1), order;
  order.reserve(static_cast<size_t>(V));
  for (int r : roots) {
    std::vector<std::pair<int, int>> st{{r, 0}};
    while (!st.empty()) {
      const auto pr = st.back();
      st.pop_back();
      const int u = pr.first, d = pr.second;
      if (depth[static_cast<size_t>(u)] >= 0)
        continue;
      depth[static_cast<size_t>(u)] = d;
      order.push_back(u);
      for (int v : kids[static_cast<size_t>(u)])
        st.push_back({v, d + 1});
    }
  }
  int maxDepth = 0;
  for (int u : order)
    maxDepth = std::max(maxDepth, depth[static_cast<size_t>(u)]);
  for (auto it = order.rbegin(); it != order.rend(); ++it) {
    const int u = *it;
    if (kids[static_cast<size_t>(u)].empty())
      continue;
    int s = 0;
    for (int v : kids[static_cast<size_t>(u)])
      s += leaves[static_cast<size_t>(v)];
    leaves[static_cast<size_t>(u)] = std::max(s, 1);
  }

  // ---- nested circle packing ----------------------------------------------
  // Bottom-up: leaf bubble = node disc radius + pad; each parent lays its
  // children on an area-cumulative phyllotaxis spiral in its LOCAL frame and
  // takes the smallest enclosing radius. Subassemblies render as bubbles
  // MADE OF their contents (containment = nesting, not spokes) — the user's
  // requested nested formatting. Wedges/spokes abandoned: 3500 feature/
  // sketch leaves on outer rings dominated the zoomed-out view.
  const float pad = spacing * 0.06f;
  std::vector<float> bubR(static_cast<size_t>(V), 0.0f);
  std::vector<float> locX(static_cast<size_t>(V), 0.0f),
      locY(static_cast<size_t>(V), 0.0f); // child center in parent's frame
  for (auto it = order.rbegin(); it != order.rend(); ++it) {
    const int u = *it;
    const auto &ch = kids[static_cast<size_t>(u)];
    if (ch.empty()) {
      bubR[static_cast<size_t>(u)] =
          G.nodes[G.visibleIdx[static_cast<size_t>(u)]].radius + pad;
      continue;
    }
    double acc = 0.0;
    float need = 0.0f;
    for (size_t i = 0; i < ch.size(); ++i) {
      const int c = ch[i];
      const float rc = bubR[static_cast<size_t>(c)];
      acc += (double)(2.2f * rc) * (2.2f * rc);
      const float r = (float)std::sqrt(acc / 3.14159265358979);
      const float a = (float)i * GOLDEN;
      const float x = r * std::cos(a), y = r * std::sin(a);
      locX[static_cast<size_t>(c)] = x;
      locY[static_cast<size_t>(c)] = y;
      need = std::max(need, r + rc);
    }
    bubR[static_cast<size_t>(u)] = need + pad;
  }
  // Roots pack around a virtual super-root at the origin (same spiral).
  {
    double acc = 0.0;
    for (size_t i = 0; i < roots.size(); ++i) {
      const int r = roots[i];
      const float rc = bubR[static_cast<size_t>(r)];
      acc += (double)(2.2f * rc) * (2.2f * rc);
      const float rr = (float)std::sqrt(acc / 3.14159265358979);
      const float a = (float)i * GOLDEN;
      locX[static_cast<size_t>(r)] = rr * std::cos(a);
      locY[static_cast<size_t>(r)] = rr * std::sin(a);
    }
  }

  // Mean depth (z centring).
  double meanDepth = 0.0;
  for (int u : order)
    meanDepth += depth[static_cast<size_t>(u)];
  if (!order.empty())
    meanDepth /= (double)order.size();

  // Top-down: global = parent's global + local offset (preorder ⇒ parents
  // first). Cached nodes anchor at their persisted position; their subtree
  // still packs around it.
  for (int u : order) {
    float gx, gy;
    if (isFresh[static_cast<size_t>(u)]) {
      gx = locX[static_cast<size_t>(u)];
      gy = locY[static_cast<size_t>(u)];
      outX[static_cast<size_t>(u)] = gx;
      outY[static_cast<size_t>(u)] = gy;
      outZ[static_cast<size_t>(u)] =
          ((float)depth[static_cast<size_t>(u)] - (float)meanDepth) * hop;
    } else {
      const GNode &n = G.nodes[G.visibleIdx[static_cast<size_t>(u)]];
      gx = n.lx;
      gy = n.ly;
    }
    for (int c : kids[static_cast<size_t>(u)]) {
      locX[static_cast<size_t>(c)] += gx;
      locY[static_cast<size_t>(c)] += gy;
    }
  }
  // Bubble radii onto nodes for outline drawing + fit extents.
  for (int u : order)
    G.nodes[G.visibleIdx[static_cast<size_t>(u)]].bubbleR =
        bubR[static_cast<size_t>(u)];

  // Anchor pass: fresh non-tree nodes group onto phyllotaxis discs around
  // their first placed neighbor (ANY edge kind). 3 sweeps settle chains.
  std::vector<char> placed(static_cast<size_t>(V), 0);
  for (int u : order)
    placed[static_cast<size_t>(u)] = 1;
  for (int i = 0; i < V; ++i)
    if (!isFresh[static_cast<size_t>(i)])
      placed[static_cast<size_t>(i)] = 1;
  auto posOf = [&](int v, float &x, float &y) {
    if (isFresh[static_cast<size_t>(v)]) {
      x = outX[static_cast<size_t>(v)];
      y = outY[static_cast<size_t>(v)];
    } else {
      const GNode &n = G.nodes[G.visibleIdx[static_cast<size_t>(v)]];
      x = n.lx;
      y = n.ly;
    }
  };
  for (int sweep = 0; sweep < 3; ++sweep) {
    std::map<int, std::vector<int>> groups; // ordered -> deterministic
    for (int i = 0; i < V; ++i) {
      if (placed[static_cast<size_t>(i)] || !isFresh[static_cast<size_t>(i)])
        continue;
      int tgt = -1;
      for (int v : adjAll[static_cast<size_t>(i)])
        if (placed[static_cast<size_t>(v)]) {
          tgt = v;
          break;
        }
      if (tgt >= 0)
        groups[tgt].push_back(i);
    }
    if (groups.empty())
      break;
    for (const auto &kv : groups) {
      float cx, cy;
      posOf(kv.first, cx, cy);
      for (size_t k = 0; k < kv.second.size(); ++k) {
        const int i = kv.second[k];
        const float rr = hop * 0.7f * std::sqrt((float)k + 0.5f);
        const float a = (float)k * GOLDEN;
        outX[static_cast<size_t>(i)] = cx + rr * std::cos(a);
        outY[static_cast<size_t>(i)] = cy + rr * std::sin(a);
        placed[static_cast<size_t>(i)] = 1;
      }
    }
  }
  // Leftovers (no placed neighbor): component well + golden offset.
  for (int i = 0; i < V; ++i) {
    if (placed[static_cast<size_t>(i)] || !isFresh[static_cast<size_t>(i)])
      continue;
    const int c = comp[static_cast<size_t>(i)];
    const float wx = c >= 0 ? well[static_cast<size_t>(c)].first : 0.0f;
    const float wy = c >= 0 ? well[static_cast<size_t>(c)].second : 0.0f;
    const float a = (float)i * GOLDEN;
    outX[static_cast<size_t>(i)] = wx + spacing * 0.35f * std::cos(a);
    outY[static_cast<size_t>(i)] = wy + spacing * 0.35f * std::sin(a);
  }
}

// Layered assembly-tree seed (Sugiyama-lite, deterministic). Layers =
// containment/instance depth from root assemblies; within-layer order runs
// barycenter sweeps over hierarchy + mates edges so mated siblings sit
// together and hierarchy webs cross minimally. z = 0 (planar tree).
void layeredAsmSeed(GraphTabState &G, const std::vector<float> &collR, float S,
                    std::vector<float> &outX, std::vector<float> &outY,
                    std::vector<float> &outZ) {
  const int V = (int)G.visibleIdx.size();
  outX.assign(static_cast<size_t>(V), 0.0f);
  outY.assign(static_cast<size_t>(V), 0.0f);
  outZ.assign(static_cast<size_t>(V), 0.0f);
  if (V == 0)
    return;

  std::unordered_map<std::string, int> ord;
  ord.reserve(static_cast<size_t>(V) * 2);
  for (int i = 0; i < V; ++i)
    ord[G.nodes[G.visibleIdx[static_cast<size_t>(i)]].id] = i;

  // Hierarchy (parent → child) + undirected mates adjacency for ordering.
  std::vector<int> parent(static_cast<size_t>(V), -1);
  std::vector<std::vector<int>> kids(static_cast<size_t>(V));
  std::vector<std::vector<int>> orderAdj(static_cast<size_t>(V));
  for (const auto &e : G.visibleEdges) {
    auto ai = ord.find(e.src), bi = ord.find(e.dst);
    if (ai == ord.end() || bi == ord.end())
      continue;
    const int a = ai->second, b = bi->second;
    if (e.kind == "CONTAINS" || e.kind == "INSTANCE_OF") {
      if (parent[static_cast<size_t>(b)] < 0 && a != b) {
        parent[static_cast<size_t>(b)] = a;
        kids[static_cast<size_t>(a)].push_back(b);
      }
      orderAdj[static_cast<size_t>(a)].push_back(b);
      orderAdj[static_cast<size_t>(b)].push_back(a);
    } else if (e.kind == "MATES") {
      orderAdj[static_cast<size_t>(a)].push_back(b);
      orderAdj[static_cast<size_t>(b)].push_back(a);
    }
  }

  // Kahn layering: roots (no visible parent) at depth 0.
  std::vector<int> depth(static_cast<size_t>(V), -1);
  std::vector<int> q;
  q.reserve(static_cast<size_t>(V));
  for (int i = 0; i < V; ++i)
    if (parent[static_cast<size_t>(i)] < 0) {
      depth[static_cast<size_t>(i)] = 0;
      q.push_back(i);
    }
  int maxDepth = 0;
  for (size_t qi = 0; qi < q.size(); ++qi) {
    const int u = q[qi];
    for (int ch : kids[static_cast<size_t>(u)]) {
      if (depth[static_cast<size_t>(ch)] >= 0)
        continue; // cycle guard
      depth[static_cast<size_t>(ch)] = depth[static_cast<size_t>(u)] + 1;
      if (depth[static_cast<size_t>(ch)] > maxDepth)
        maxDepth = depth[static_cast<size_t>(ch)];
      q.push_back(ch);
    }
  }
  // Unreached nodes (cycles / mates-only clusters): loose bottom layer.
  for (int i = 0; i < V; ++i)
    if (depth[static_cast<size_t>(i)] < 0)
      depth[static_cast<size_t>(i)] = maxDepth + 1;
  const int nLayer = maxDepth + 2;

  std::vector<std::vector<int>> layers(static_cast<size_t>(nLayer));
  for (int i = 0; i < V; ++i)
    layers[static_cast<size_t>(depth[static_cast<size_t>(i)])].push_back(i);
  // Members arrive in visOrd order (id-sorted) — deterministic start.

  // Barycenter sweeps over hierarchy + mates (stable ties → deterministic).
  std::vector<int> posInLayer(static_cast<size_t>(V), 0);
  for (const auto &L : layers)
    for (int i = 0; i < (int)L.size(); ++i)
      posInLayer[static_cast<size_t>(L[static_cast<size_t>(i)])] = i;
  constexpr int kSweeps = 8;
  for (int sw = 0; sw < kSweeps; ++sw) {
    for (auto &L : layers) {
      if (L.size() < 2)
        continue;
      std::vector<float> bc(L.size(), 0.0f);
      for (int i = 0; i < (int)L.size(); ++i) {
        const int u = L[static_cast<size_t>(i)];
        float sum = 0.0f;
        int cnt = 0;
        for (int v : orderAdj[static_cast<size_t>(u)]) {
          sum += (float)posInLayer[static_cast<size_t>(v)];
          ++cnt;
        }
        bc[static_cast<size_t>(i)] = cnt > 0 ? sum / (float)cnt : (float)i;
      }
      std::vector<int> idx(L.size());
      for (int i = 0; i < (int)idx.size(); ++i)
        idx[static_cast<size_t>(i)] = i;
      std::stable_sort(idx.begin(), idx.end(), [&](int x, int y) {
        return bc[static_cast<size_t>(x)] < bc[static_cast<size_t>(y)];
      });
      std::vector<int> NL;
      NL.reserve(L.size());
      for (int i : idx)
        NL.push_back(L[static_cast<size_t>(i)]);
      L.swap(NL);
      for (int i = 0; i < (int)L.size(); ++i)
        posInLayer[static_cast<size_t>(L[static_cast<size_t>(i)])] = i;
    }
  }

  // Coordinates: rows by depth, x packed by collision radius, centered.
  const float layerGap = 300.0f * S;
  const float xGap = 90.0f * S;
  for (int li = 0; li < nLayer; ++li) {
    const auto &L = layers[static_cast<size_t>(li)];
    if (L.empty())
      continue;
    float width = 0.0f;
    for (int u : L)
      width += 2.0f * collR[static_cast<size_t>(u)] + xGap;
    width -= xGap;
    float cursor = -0.5f * width;
    for (int u : L) {
      const float r = collR[static_cast<size_t>(u)];
      outX[static_cast<size_t>(u)] = cursor + r;
      outY[static_cast<size_t>(u)] = (float)li * layerGap;
      cursor += 2.0f * r + xGap;
    }
  }
}

// Rebuild the force simulation over the VISIBLE subgraph (called when
// layoutDirty, not per frame). Existing layout coords are reused as seeds so
// refreshes/filter toggles stay stable; new nodes seed near their CONTAINS
// parent or on a deterministic golden-angle spiral. Hidden edge kinds exert
// no spring force. Sets fitOnSettle so the view frames the relaxed layout.
void rebuildForce(GraphTabState &G) {
  G.force.clear();
  G.force.set3D(G.view3d); // true 3D physics only in 3D mode
  G.forceSlot.assign(G.nodes.size(), -1);
  G.dragNode = G.dragCandidate = -1;
  G.layoutDirty = false;
  if (G.visibleIdx.empty())
    return;

  const float S = ui::uiScale;
  // Looser d3-style spacing: longer springs + stronger repulsion + roomy
  // collision pad so sized hubs don't pack into a hairball.
  const float collPad = 12.0f * S;
  G.force.setLinkDistance(190.0f * S);
  G.force.setRepulsion(1600.0f * S * S * (190.0f / 60.0f) * (190.0f / 60.0f));
  G.force.setCollision(11.0f, collPad);
  G.force.setVelocityDecay(0.55f);
  // Slower cooling than the d3 default: at ~4k nodes 300 substeps freezes
  // mid-organization; ~740 substeps reaches equilibrium (~4 s visible
  // animation at 3 substeps/frame, proportionally less from small reheats).
  G.force.setAlphaDecay(0.009f);
  G.force.setCentering(0.015f);
  // Cluster gravity — O(N) pull toward connected-component centroids.
  G.force.setGroupPull(0.08f);

  // Node sizing metric (d3-style: area ∝ importance). Structural nodes use
  // their hidden-descendant count (features/sketches inside the document);
  // decomposition children accrue instance quantity; everything accrues
  // visible edge degree. radius ∝ sqrt so disc AREA scales with the metric.
  std::unordered_map<int, int> degree;
  degree.reserve(G.visibleIdx.size() * 2);
  for (const auto &e : G.visibleEdges) {
    if (G.hiddenKinds.count(e.kind))
      continue;
    auto si = G.idIndex.find(e.src), di = G.idIndex.find(e.dst);
    if (si != G.idIndex.end())
      degree[si->second]++;
    if (di != G.idIndex.end())
      degree[di->second]++;
  }
  // Instance quantity makes repeatedly-used parts visually bigger.
  for (const auto &[id, qty] : G.instCount) {
    auto it = G.idIndex.find(id);
    if (it != G.idIndex.end())
      degree[it->second] += qty;
  }

  // ---- Connected components on "binding" edges (O(N+E)) --------------------
  // Group parts that share mates / instances / depends / contains structure.
  // Weak REFERENCES alone do not bridge components — keeps clusters meaningful.
  auto bindingKind = [](const std::string &k) {
    return k == "MATES" || k == "INSTANCE_OF" || k == "DEPENDS_ON" ||
           k == "CONTAINS" || k == "DERIVES_FROM";
  };
  std::unordered_map<std::string, int> visOrd; // node id → 0..V-1
  visOrd.reserve(G.visibleIdx.size() * 2);
  for (int i = 0; i < (int)G.visibleIdx.size(); ++i)
    visOrd[G.nodes[G.visibleIdx[i]].id] = i;
  const int V = (int)G.visibleIdx.size();
  std::vector<std::vector<int>> adj(static_cast<size_t>(V));
  std::vector<std::vector<int>> adjAll(
      static_cast<size_t>(V)); // every edge kind
  for (const auto &e : G.visibleEdges) {
    if (G.hiddenKinds.count(e.kind))
      continue;
    auto a = visOrd.find(e.src), b = visOrd.find(e.dst);
    if (a == visOrd.end() || b == visOrd.end())
      continue;
    adjAll[static_cast<size_t>(a->second)].push_back(b->second);
    adjAll[static_cast<size_t>(b->second)].push_back(a->second);
    if (!bindingKind(e.kind))
      continue;
    adj[static_cast<size_t>(a->second)].push_back(b->second);
    adj[static_cast<size_t>(b->second)].push_back(a->second);
  }
  std::vector<int> comp(static_cast<size_t>(V), -1);
  int nComp = 0;
  std::vector<int> stack;
  stack.reserve(static_cast<size_t>(V));
  for (int i = 0; i < V; ++i) {
    if (comp[static_cast<size_t>(i)] >= 0)
      continue;
    const int cid = nComp++;
    stack.clear();
    stack.push_back(i);
    comp[static_cast<size_t>(i)] = cid;
    while (!stack.empty()) {
      int u = stack.back();
      stack.pop_back();
      for (int v : adj[static_cast<size_t>(u)]) {
        if (comp[static_cast<size_t>(v)] >= 0)
          continue;
        comp[static_cast<size_t>(v)] = cid;
        stack.push_back(v);
      }
    }
  }
  // Deterministic cluster well centers on a ring (sorted by component size).
  std::vector<int> compSize(static_cast<size_t>(std::max(nComp, 1)), 0);
  for (int c : comp)
    if (c >= 0)
      compSize[static_cast<size_t>(c)]++;
  std::vector<int> order(static_cast<size_t>(nComp));
  for (int i = 0; i < nComp; ++i)
    order[static_cast<size_t>(i)] = i;
  std::sort(order.begin(), order.end(), [&](int a, int b) {
    if (compSize[static_cast<size_t>(a)] != compSize[static_cast<size_t>(b)])
      return compSize[static_cast<size_t>(a)] >
             compSize[static_cast<size_t>(b)];
    return a < b;
  });
  std::vector<std::pair<float, float>> well(
      static_cast<size_t>(std::max(nComp, 1)), {0.f, 0.f});
  if (nComp > 1) {
    const float ring = 220.0f * S * std::sqrt((float)std::max(nComp, 1));
    for (int oi = 0; oi < nComp; ++oi) {
      const int cid = order[static_cast<size_t>(oi)];
      const float ang =
          (float)oi * (6.28318530718f / (float)nComp) - 1.57079632679f;
      well[static_cast<size_t>(cid)] = {ring * std::cos(ang),
                                        ring * std::sin(ang)};
    }
  }

  // Collision radius may exceed disc radius so label boxes don't stack.
  // Keyed by nodes[] index; only filled for visible nodes in the sim.
  std::unordered_map<int, float> collR;
  collR.reserve(G.visibleIdx.size() * 2);
  std::vector<float> collROrd(static_cast<size_t>(V), 14.0f); // by visOrd

  // Radii + freshness first — the deterministic seeds need both.
  std::vector<char> isFresh(static_cast<size_t>(V), 0);
  int fresh = 0;
  for (int i = 0; i < V; ++i) {
    const int vi = G.visibleIdx[static_cast<size_t>(i)];
    GNode &n = G.nodes[vi];
    int desc = 0;
    if (auto dit = G.descCounts.find(n.id); dit != G.descCounts.end())
      desc = dit->second.total();
    // Milder radius growth so big hubs don't dominate the canvas.
    const float metric = std::sqrt((float)(desc + degree[vi]));
    float discR = (10.0f + 2.4f * metric) * S;
    if (n.kind == "project")
      discR *= 1.30f;
    discR = std::clamp(discR, 11.0f * S, 36.0f * S);
    n.radius = discR; // drawing uses disc radius
    n.bubbleR = 0.0f; // repacked below when the hierarchy seed runs

    const float labelW = ImGui::CalcTextSize(n.label.c_str()).x;
    const float collRadius =
        std::max(discR, std::min(labelW, 140.0f * S) * 0.5f + 4.0f * S);
    collR[vi] = collRadius;
    collROrd[static_cast<size_t>(i)] = collRadius;

    if (n.lx == 0.0f && n.ly == 0.0f) {
      isFresh[static_cast<size_t>(i)] = 1;
      ++fresh;
    }
  }

  // ---- deterministic seed dispatch ------------------------------------------
  // Fresh nodes (no persisted/stashed position) get placed by a deterministic
  // algorithm; cached positions are never touched.
  //  - Assembly view: layered tree (Sugiyama-lite) — reads top-down.
  //  - Mostly-fresh web: balloon hierarchy over the CONTAINS forest +
  //    neighbor-anchored reference singles (hierarchySeed) — pure
  //    structure, sim frozen; no collapse/homogenization modes.
  //  - A few new nodes on a warm layout: cheap golden-angle append (below).
  std::vector<float> seedX, seedY, seedZ;
  bool haveSeed = false;
  if (fresh > 0 && G.asmView && G.scopeDocId.empty()) {
    layeredAsmSeed(G, collROrd, S, seedX, seedY, seedZ);
    haveSeed = true;
  } else if (fresh * 2 > V) {
    hierarchySeed(G, visOrd, adjAll, comp, well, isFresh, 190.0f * S, seedX,
                  seedY, seedZ);
    haveSeed = true;
    G.hierarchyLayout = true; // pure structure — freeze the sim below
  }

  const float GOLDEN = 2.39996323f; // golden angle (rad)
  int goldIdx = 0;
  for (int i = 0; i < V; ++i) {
    const int vi = G.visibleIdx[static_cast<size_t>(i)];
    GNode &n = G.nodes[vi];
    const float collRadius = collROrd[static_cast<size_t>(i)];

    float sx = n.lx, sy = n.ly;
    const int cid = comp[static_cast<size_t>(i)];
    // Scope root seeds at origin so isolate view frames on the drilled node.
    if (!G.scopeDocId.empty() && n.id == G.scopeDocId && sx == 0.0f &&
        sy == 0.0f) {
      sx = 0.0f;
      sy = 0.0f;
    } else if (isFresh[static_cast<size_t>(i)]) {
      if (haveSeed) {
        sx = seedX[static_cast<size_t>(i)];
        sy = seedY[static_cast<size_t>(i)];
        n.lz = seedZ[static_cast<size_t>(i)]; // depth for 3D projection
      } else {
        // Few fresh nodes on a warm layout: seed near their component well
        // (deterministic) with golden-angle jitter — never fight persisted.
        float wx = 0.f, wy = 0.f;
        if (cid >= 0 && cid < (int)well.size()) {
          wx = well[static_cast<size_t>(cid)].first;
          wy = well[static_cast<size_t>(cid)].second;
        }
        bool seeded = false;
        auto pit = G.containsParent.find(n.id);
        if (pit != G.containsParent.end()) {
          auto pi = G.idIndex.find(pit->second);
          if (pi != G.idIndex.end()) {
            const GNode &p = G.nodes[pi->second];
            if (p.lx != 0.0f || p.ly != 0.0f) {
              const float a = (float)goldIdx * GOLDEN;
              sx = p.lx + 80.0f * S * std::cos(a);
              sy = p.ly + 80.0f * S * std::sin(a);
              seeded = true;
            }
          }
        }
        if (!seeded) {
          const float rr = 40.0f * S * std::sqrt((float)std::max(goldIdx, 1));
          const float a = (float)goldIdx * GOLDEN;
          sx = wx + rr * std::cos(a);
          sy = wy + rr * std::sin(a);
        }
        ++goldIdx;
      }
    }
    const int slot = G.force.addNode(sx, sy, n.lz, collRadius);
    G.forceSlot[vi] = slot;
    // World-placed nodes hold their real model coordinates in 3D.
    if (G.view3d && n.hasW)
      G.force.setPinned(slot, true);
    if (cid >= 0)
      G.force.setGroup(slot, cid);
  }

  // Springs per visible edge; kind tunes rest / stiffness. Cap springs per
  // node (~12) so dense project graphs stay O(N) and don't form hairballs —
  // keep strongest binding edges first (MATES > INSTANCE > DEPENDS > …).
  auto kindPri = [](const std::string &k) -> int {
    if (k == "MATES")
      return 0;
    if (k == "INSTANCE_OF")
      return 1;
    if (k == "DEPENDS_ON")
      return 2;
    if (k == "CONTAINS")
      return 3;
    if (k == "DERIVES_FROM")
      return 4;
    if (k == "REFERENCES")
      return 5;
    return 6;
  };
  struct Cand {
    int a, b;
    float rest, strength;
    int pri;
    int agg;
  };
  std::vector<Cand> cands;
  cands.reserve(G.visibleEdges.size());
  for (const auto &e : G.visibleEdges) {
    if (G.hiddenKinds.count(e.kind))
      continue;
    auto si = G.idIndex.find(e.src), di = G.idIndex.find(e.dst);
    if (si == G.idIndex.end() || di == G.idIndex.end())
      continue;
    int a = G.forceSlot[si->second], b = G.forceSlot[di->second];
    if (a < 0 || b < 0 || a == b)
      continue;
    const float ra = collR.count(si->second) ? collR[si->second]
                                             : G.nodes[si->second].radius;
    const float rb = collR.count(di->second) ? collR[di->second]
                                             : G.nodes[di->second].radius;
    const float radii = G.nodes[si->second].radius + G.nodes[di->second].radius;
    float kindRest = 160.0f, strength = 0.35f;
    if (e.kind == "CONTAINS") {
      kindRest = 150.0f;
      strength = 0.45f;
    } else if (e.kind == "INSTANCE_OF") {
      kindRest = 140.0f;
      strength = 0.72f;
    } else if (e.kind == "DEPENDS_ON") {
      kindRest = 180.0f;
      strength = 0.48f;
    } else if (e.kind == "MATES") {
      kindRest = 130.0f;
      strength = 0.80f;
    } else if (e.kind == "REFERENCES" || e.kind == "DERIVES_FROM") {
      kindRest = 240.0f;
      strength = 0.22f;
    }
    if (e.aggCount > 1)
      strength += std::min(0.20f, 0.03f * (float)(e.aggCount - 1));
    const float rest =
        std::max(kindRest * S + 1.20f * radii, ra + rb + collPad);
    cands.push_back({a, b, rest, strength, kindPri(e.kind), e.aggCount});
  }
  std::sort(cands.begin(), cands.end(), [](const Cand &x, const Cand &y) {
    if (x.pri != y.pri)
      return x.pri < y.pri;
    if (x.agg != y.agg)
      return x.agg > y.agg;
    return x.strength > y.strength;
  });
  constexpr int kMaxSpringsPerNode = 12;
  std::unordered_map<int, int> springCount;
  springCount.reserve(cands.size() * 2);
  for (const Cand &c : cands) {
    int &ca = springCount[c.a];
    int &cb = springCount[c.b];
    // Always keep top-priority mates/instance even if one end is saturated.
    if (c.pri > 2 && (ca >= kMaxSpringsPerNode || cb >= kMaxSpringsPerNode))
      continue;
    if (ca >= kMaxSpringsPerNode && cb >= kMaxSpringsPerNode)
      continue;
    G.force.addEdge(c.a, c.b, c.rest, c.strength);
    ++ca;
    ++cb;
  }

  // Hierarchy mode: frozen — the balloon IS the layout; only manual drags
  // change positions (persisted via the layout cache like before).
  G.force.reheat(G.hierarchyLayout
                     ? 0.0f
                     : (fresh > (int)G.visibleIdx.size() / 2 ? 0.8f : 0.35f));
  // Auto-fit on settle only until the user first pans/zooms.
  G.fitOnSettle = !G.userNavigated;
  G.wasSettled = false;

  // Warm-up burst: run substeps NOW (wall-time bounded so huge drill-down
  // scopes can't hitch the UI) — the first painted frame is already
  // semi-relaxed instead of an exploding seed spiral.
  const auto t0 = std::chrono::steady_clock::now();
  // Clustered seed + group pull settles faster; allow a slightly longer
  // first-paint burst (still wall-capped) so the hairball never flashes.
  for (int i = 0; i < 180; ++i) {
    G.force.step(1.0f / 60.0f);
    if (G.force.settled())
      break;
    if ((i & 15) == 15 && std::chrono::duration<double, std::milli>(
                              std::chrono::steady_clock::now() - t0)
                                  .count() > 22.0)
      break;
  }
  for (int vi : G.visibleIdx) {
    int slot = (vi < (int)G.forceSlot.size()) ? G.forceSlot[vi] : -1;
    if (slot < 0)
      continue;
    G.nodes[vi].lx = G.force.x(slot);
    G.nodes[vi].ly = G.force.y(slot);
    G.nodes[vi].lz = G.force.z(slot);
  }
}

void drawDashedLine(ImDrawList *dl, ImVec2 a, ImVec2 b, ImU32 col, float thick,
                    float dash = 6.0f, float gap = 4.0f) {
  float dx = b.x - a.x, dy = b.y - a.y;
  float len = std::sqrt(dx * dx + dy * dy);
  if (len < 0.5f)
    return;
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

// Rotating arc spinner for the graph canvas overlay. Draw-list only (no
// widgets), driven by GetTime() so it stays alive while G.loading keeps the
// frame loop busy via st.rpc.busy(). Visual language matches plmSpinner: accent
// stroke, ~2 rev/s.
void drawArcSpinner(ImDrawList *dl, ImVec2 c, float radius, ImU32 col) {
  const float t = (float)ImGui::GetTime();
  const float a0 = t * (4.0f * 3.14159265f); // 2 revolutions per second
  const float sweep = 3.14159265f * 1.5f;    // 270° arc
  const float th = std::max(1.5f, radius * 0.18f);
  dl->PathArcTo(c, radius, a0, a0 + sweep, 24);
  dl->PathStroke(col, 0, th);
}

// ---- settled-layout cache (solve state) -------------------------------------
//
// Bump kLayoutVersion whenever seed/clustering/force policy changes enough that
// old positions would re-open as a hairball. Missing/mismatched version →
// ignore the file and re-seed from the current algorithm (cluster wells).
constexpr int kLayoutVersion = 7;

std::filesystem::path layoutCachePath(const GraphTabState &G) {
  return std::filesystem::path(G.projectDir) / ".plm" / "graph_layout.json";
}

// Load <project>/.plm/graph_layout.json once per project. Only file mtimes
// invalidate the worker's graph cache; this UI-owned file does not.
void ensureLayoutCache(GraphTabState &G) {
  if (G.layoutCacheLoaded)
    return;
  G.layoutCacheLoaded = true;
  G.layoutCache.clear();
  if (G.projectDir.empty())
    return;
  std::ifstream f(layoutCachePath(G));
  if (!f)
    return;
  nlohmann::json j = nlohmann::json::parse(f, nullptr, false);
  if (j.is_discarded() || !j.is_object())
    return;
  // Pre-version caches (and any future mismatch) are discarded so the new
  // clustered seed runs automatically — no manual delete required.
  const int ver = j.value("layoutVersion", 0);
  if (ver != kLayoutVersion)
    return;
  auto it = j.find("positions");
  if (it == j.end() || !it->is_object())
    return;
  for (auto pit = it->begin(); pit != it->end(); ++pit) {
    const auto &xy = pit.value();
    if (!xy.is_array() || (xy.size() != 2 && xy.size() != 3) ||
        !xy[0].is_number() || !xy[1].is_number())
      continue;
    const float z =
        (xy.size() == 3 && xy[2].is_number()) ? xy[2].get<float>() : 0.0f;
    G.layoutCache[pit.key()] = {xy[0].get<float>(), xy[1].get<float>(), z};
  }
}

// Persist every node with a real position (any scope) so the next session
// seeds the sim from the relaxed arrangement. Throttled to once per 2s.
// The on-disk cache holds the PROJECT view solve state — while the assembly
// view is active we persist its stashed project positions, not the layered
// arrangement (which is deterministic and cheap to re-derive).
void maybeSaveLayout(GraphTabState &G, bool force = false) {
  if (G.projectDir.empty() || G.nodes.empty())
    return;
  const double now = ImGui::GetTime();
  if (!force && (now - G.lastLayoutSave) < 2.0)
    return;
  G.lastLayoutSave = now;
  nlohmann::json j;
  j["layoutVersion"] = kLayoutVersion;
  auto &pos = j["positions"];
  for (const auto &n : G.nodes) {
    float px = n.lx, py = n.ly, pz = n.lz;
    if (G.asmView) {
      auto it = G.projPosStash.find(n.id);
      if (it == G.projPosStash.end())
        continue; // never arranged in project view — leave uncached
      px = it->second.x;
      py = it->second.y;
      pz = it->second.z;
    }
    if (px != 0.0f || py != 0.0f)
      pos[n.id] = {px, py, pz};
  }
  std::error_code ec;
  std::filesystem::create_directories(layoutCachePath(G).parent_path(), ec);
  std::ofstream out(layoutCachePath(G), std::ios::trunc);
  if (out)
    out << j.dump();
}

// ---- fetch
// -------------------------------------------------------------------

void requestGraph(AppState &st, bool force) {
  auto &G = S();
  if (!G.available)
    return;
  if (G.loading)
    return;
  const std::string proj = st.plmProjectPath();
  if (proj.empty())
    return;
  const double now = ImGui::GetTime();
  if (!force && (now - G.lastFetch) < 2.0 && proj == G.projectDir &&
      !G.nodes.empty())
    return;

  G.err.clear();
  G.projectDir = proj;
  G.lastFetch = now;
  // Background change-probes stay invisible: no spinner, no button flicker.
  const bool silent = !force && G.mtimesMax > 0.0 && !G.nodes.empty();
  G.loading = !silent;

  nlohmann::json p;
  p["project_dir"] = proj;
  // Also accept path-style callers; harmless extra key for robust workers.
  p["path"] = proj;
  // Archive dirs are excluded server-side by default; flip only when the
  // user unchecks "Hide archive/stale".
  p["include_archive"] = !G.hideArchive;
  if (force)
    p["force"] = true;
  // Change probe: when the worker confirms nothing changed we get a tiny
  // "unchanged" reply instead of the full multi-MB payload.
  if (!force && G.mtimesMax > 0.0) {
    p["if_mtimes_max"] = G.mtimesMax;
    p["if_sources_count"] = G.sourcesCount;
  }

  st.rpc.call("cad_graph_get", p, [](const core::RpcResult &r) {
    auto &G = S();
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
      G.err = r.result.value(
          "hint",
          r.result.value("reason", std::string("that could not be done")));
      return;
    }

    // Change probe hit: graph unchanged — keep data, layout, and view.
    if (r.result.is_object() && r.result.value("unchanged", false))
      return;

    // Stash current positions so a refetch doesn't re-solve from zero.
    // Mode-aware: the active view's stash keeps its own arrangement.
    stashLivePositions(G);

    G.nodes.clear();
    G.edges.clear();
    clearBlast(G);
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

    if (auto it = r.result.find("nodes");
        it != r.result.end() && it->is_array()) {
      G.nodes.reserve(it->size());
      for (const auto &jn : *it) {
        if (!jn.is_object())
          continue;
        GNode n;
        n.id = jstr(jn, "id");
        n.kind = jstr(jn, "kind");
        n.label = jstr(jn, "label");
        n.path = jstr(jn, "path");
        n.docId = jstr(jn, "doc_id");
        n.origin = jstr(jn, "origin");
        if (n.origin.empty())
          n.origin = "extracted";
        if (auto m = jn.find("meta"); m != jn.end() && m->is_object()) {
          n.metaSummary = summarizeMeta(*m);
          n.metaDh = jstr(*m, "dh");
          if (auto mt = m->find("mt"); mt != m->end() && mt->is_number())
            n.metaMt = mt->get<double>();
          if (auto wp = m->find("wp");
              wp != m->end() && wp->is_array() && wp->size() == 3 &&
              (*wp)[0].is_number() && (*wp)[1].is_number() &&
              (*wp)[2].is_number()) {
            n.wx = (*wp)[0].get<float>();
            n.wy = (*wp)[1].get<float>();
            n.wz = (*wp)[2].get<float>();
            n.hasW = true;
          }
        }
        if (n.id.empty())
          continue;
        if (n.label.empty())
          n.label = n.id;
        G.nodes.push_back(std::move(n));
      }
    }
    if (auto it = r.result.find("edges");
        it != r.result.end() && it->is_array()) {
      G.edges.reserve(it->size());
      for (const auto &je : *it) {
        if (!je.is_object())
          continue;
        GEdge e;
        e.src = jstr(je, "src");
        e.dst = jstr(je, "dst");
        e.kind = jstr(je, "kind");
        e.origin = jstr(je, "origin");
        if (e.origin.empty())
          e.origin = "extracted";
        if (auto m = je.find("meta"); m != je.end() && m->is_object()) {
          e.meta = *m;
          e.metaSummary = summarizeMeta(*m);
          if (e.kind == "DERIVES_FROM" || e.kind == "REFERENCES" ||
              e.kind == "MATES")
            e.dimLabel = edgeDimLabel(*m);
        }
        if (e.src.empty() || e.dst.empty())
          continue;
        G.edges.push_back(std::move(e));
      }
    }

    // Backfill full counts when an older worker omitted them.
    if (G.nodeCountFull <= 0)
      G.nodeCountFull = (int)G.nodes.size();
    if (G.edgeCountFull <= 0)
      G.edgeCountFull = (int)G.edges.size();

    // Probe fingerprint for subsequent polls.
    G.mtimesMax = r.result.value("sources_mtime_max", 0.0);
    G.sourcesCount = r.result.value("sources_count", 0);

    // Restore positions: in-session stash wins, then the on-disk solve
    // state from previous sessions (loaded once per project).
    ensureLayoutCache(G);
    for (auto &n : G.nodes) {
      const auto &stash = G.asmView ? G.asmPosStash : G.projPosStash;
      if (auto it = stash.find(n.id); it != stash.end()) {
        n.lx = it->second.x;
        n.ly = it->second.y;
        n.lz = it->second.z;
      } else if (!G.asmView) {
        if (auto it = G.layoutCache.find(n.id); it != G.layoutCache.end()) {
          n.lx = it->second.x;
          n.ly = it->second.y;
          n.lz = it->second.z;
        }
      }
    }

    // Indexes + descendant badges + scoped visible set (once per load).
    rebuildIdIndex(G);
    rebuildContainsParent(G);
    rebuildDescCounts(G);
    // Drop stale drill target if the node vanished.
    if (!G.scopeDocId.empty() && !G.idIndex.count(G.scopeDocId)) {
      G.scopeDocId.clear();
      G.scopeStack.clear();
    }
    rebuildVisible(G);

    // With project-view scoping the canvas stays readable; only auto-list
    // when the *visible* set is still enormous (or user forced list).
    if (!G.listViewUserOverride) {
      G.listView = (int)G.visibleIdx.size() > kListViewThreshold;
      if (G.listView)
        G.gridView = false; // auto-list wins over a stale grid flag
    }

    // Keep selection if it still exists in the full graph.
    if (!G.selectedId.empty()) {
      if (!G.idIndex.count(G.selectedId)) {
        G.selectedId.clear();
        clearBlast(G);
      }
    }
    G.selectedEdge = -1;
  });
}

void clearBlast(GraphTabState &G) {
  G.blast.clear();
  G.blastEpicenter.clear();
  G.blastT0 = 0.0;
}

void requestBlast(AppState &st, const std::string &nodeId) {
  auto &G = S();
  if (!G.available || nodeId.empty() || G.blastLoading)
    return;
  G.blastLoading = true;
  clearBlast(G);
  nlohmann::json p;
  p["node_id"] = nodeId;
  p["project_dir"] = st.plmProjectPath();
  p["path"] = st.plmProjectPath();
  st.rpc.call("cad_graph_blast", p, [nodeId](const core::RpcResult &r) {
    auto &G = S();
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
    if (auto it = r.result.find("affected");
        it != r.result.end() && it->is_array()) {
      for (const auto &a : *it) {
        if (a.is_string())
          G.blast.insert(a.get<std::string>());
      }
    }
    G.blastEpicenter = nodeId;
    G.blastT0 = ImGui::GetTime();
  });
}

void requestLinkAdd(AppState &st, const std::string &src,
                    const std::string &dst, const std::string &kind,
                    const std::string &note) {
  auto &G = S();
  if (!G.available || src.empty() || dst.empty())
    return;
  nlohmann::json p;
  p["project_dir"] = st.plmProjectPath();
  p["path"] = st.plmProjectPath();
  p["src"] = src;
  p["dst"] = dst;
  p["kind"] = kind;
  p["note"] = note;
  p["origin"] = "manual";
  st.rpc.call("cad_graph_link_add", p, [](const core::RpcResult &r) {
    if (isAbsent(r)) {
      S().available = false;
      return;
    }
    // Rebuild on the next frame so the new edge appears.
    S().lastFetch = -1e9;
  });
}

void requestLinkRemove(AppState &st, const std::string &linkId) {
  auto &G = S();
  if (!G.available || linkId.empty())
    return;
  nlohmann::json p;
  p["project_dir"] = st.plmProjectPath();
  p["path"] = st.plmProjectPath();
  p["link_id"] = linkId;
  st.rpc.call("cad_graph_link_remove", p, [](const core::RpcResult &r) {
    if (isAbsent(r)) {
      S().available = false;
      return;
    }
    S().selectedEdge = -1;
    S().lastFetch = -1e9;
  });
}

// Local glass action button — supports optional atlas icon (matches
// plmGlassButton).
bool graphGlassButton(const char *id, const char *label, bool accent = false,
                      float widthOverride = 0.0f, const char *icon = nullptr) {
  const float S = ui::uiScale;
  const float iconSz = (icon && icon[0]) ? 16.0f * S : 0.0f;
  const float iconGap = iconSz > 0.0f ? 6.0f * S : 0.0f;
  const float padX = 12.0f * S;
  const float textW = (label && label[0]) ? ImGui::CalcTextSize(label).x : 0.0f;
  const float content = iconSz + iconGap + textW;
  const float w = widthOverride > 0
                      ? widthOverride
                      : (widthOverride < 0 ? ImGui::GetContentRegionAvail().x
                                           : (padX * 2 + content));
  const float h = ImGui::GetFrameHeight();
  ImVec2 p0 = ImGui::GetCursorScreenPos();
  ImVec2 p1(p0.x + w, p0.y + h);
  bool clicked = ImGui::InvisibleButton(id, ImVec2(w, h));
  bool hov = ImGui::IsItemHovered(), held = ImGui::IsItemActive();
  ImDrawList *dl = ImGui::GetWindowDrawList();
  ui::glassBackground(dl, p0, p1, 9.0f);
  if (accent) {
    float a = held ? 0.92f : hov ? 0.80f : 0.66f;
    dl->AddRectFilled(
        p0, p1,
        ImGui::GetColorU32(
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], a)),
        9.0f * S);
  } else if (hov) {
    dl->AddRectFilled(
        p0, p1,
        ImGui::GetColorU32(ImVec4(palette.text[0], palette.text[1],
                                  palette.text[2], held ? 0.10f : 0.06f)),
        9.0f * S);
  }
  ImU32 tcol =
      accent ? IM_COL32(255, 255, 255, 255) : Palette::toU32(palette.text);
  float x = p0.x + (w - content) * 0.5f;
  if (iconSz > 0.0f) {
    ui::drawIconGlass(dl, icon, ImVec2(x + iconSz * 0.5f, p0.y + h * 0.5f),
                      iconSz, tcol, 0);
    x += iconSz + iconGap;
  }
  if (textW > 0)
    dl->AddText(ImVec2(x, p0.y + (h - ImGui::GetTextLineHeight()) * 0.5f), tcol,
                label);
  return clicked;
}

// Segmented 2D|3D mode switch: two segments under one shared glass frame;
// the active segment uses the same accent fill as an accent graphGlassButton
// (the active view-chip style). Returns 0 when 2D was clicked, 1 for 3D,
// -1 otherwise.
int graphModeSegment(const char *id, bool view3d) {
  const float S = ui::uiScale;
  ImDrawList *dl = ImGui::GetWindowDrawList();
  const float h = ImGui::GetFrameHeight();
  const float iconSz = 14.0f * S;
  const float iconGap = 5.0f * S;
  const float padX = 10.0f * S;
  const char *labels[2] = {"2D", "3D"};
  const char *icons[2] = {"plane", "view_cube"};
  const char *tips[2] = {
      "2D force layout (flat x/y). Drag nodes to rearrange.",
      "3D force layout: physics runs on x/y/z. Left-drag orbits,\n"
      "right-drag pans, Shift+drag moves a node in depth."};
  float segW[2], w = 0.0f;
  for (int i = 0; i < 2; ++i) {
    segW[i] = padX * 2.0f + iconSz + iconGap + ImGui::CalcTextSize(labels[i]).x;
    w += segW[i];
  }
  const ImVec2 p0 = ImGui::GetCursorScreenPos();
  const ImVec2 p1(p0.x + w, p0.y + h);
  ui::glassBackground(dl, p0, p1, 9.0f); // one shared frame
  const float rnd = 9.0f * S;
  int clicked = -1;
  float x = p0.x;
  ImGui::PushID(id);
  for (int i = 0; i < 2; ++i) {
    const bool active = (i == 1) == view3d;
    const ImVec2 s0(x, p0.y), s1(x + segW[i], p0.y + h);
    ImGui::SetCursorScreenPos(s0);
    ImGui::PushID(i);
    if (ImGui::InvisibleButton("##seg", ImVec2(segW[i], h)))
      clicked = i;
    ImGui::PopID();
    const bool hov = ImGui::IsItemHovered(), held = ImGui::IsItemActive();
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal))
      ImGui::SetTooltip("%s", tips[i]);
    const ImDrawFlags corners =
        i == 0 ? ImDrawFlags_RoundCornersLeft : ImDrawFlags_RoundCornersRight;
    if (active) {
      const float a = held ? 0.92f : hov ? 0.80f : 0.66f;
      dl->AddRectFilled(
          s0, s1,
          ImGui::GetColorU32(ImVec4(palette.accent[0], palette.accent[1],
                                    palette.accent[2], a)),
          rnd, corners);
    } else if (hov) {
      dl->AddRectFilled(
          s0, s1,
          ImGui::GetColorU32(ImVec4(palette.text[0], palette.text[1],
                                    palette.text[2], held ? 0.10f : 0.06f)),
          rnd, corners);
    }
    const ImU32 tcol =
        active ? IM_COL32(255, 255, 255, 255) : Palette::toU32(palette.text);
    const float content = iconSz + iconGap + ImGui::CalcTextSize(labels[i]).x;
    float cx = x + (segW[i] - content) * 0.5f;
    ui::drawIconGlass(dl, icons[i], ImVec2(cx + iconSz * 0.5f, p0.y + h * 0.5f),
                      iconSz, tcol, 0);
    cx += iconSz + iconGap;
    dl->AddText(ImVec2(cx, p0.y + (h - ImGui::GetTextLineHeight()) * 0.5f),
                tcol, labels[i]);
    x += segW[i];
  }
  ImGui::PopID();
  // Segment divider.
  dl->AddLine(ImVec2(p0.x + segW[0], p0.y + 5.0f * S),
              ImVec2(p0.x + segW[0], p1.y - 5.0f * S),
              ImGui::GetColorU32(ImVec4(palette.textDisabled[0],
                                        palette.textDisabled[1],
                                        palette.textDisabled[2], 0.35f)));
  ImGui::SetCursorScreenPos(p0);
  ImGui::Dummy(ImVec2(w, h)); // reserve the rect the buttons were drawn over
  return clicked;
}

// Resolve a graph node path to an absolute filesystem path under the project.
std::string resolveNodeAbsPath(AppState &st, const GNode &n) {
  if (n.path.empty())
    return {};
  std::filesystem::path p(n.path);
  if (p.is_absolute())
    return p.generic_string();
  std::string root = st.plmProjectPath();
  if (root.empty())
    return {};
  return (std::filesystem::path(root) / p).lexically_normal().generic_string();
}

// ---- hover thumbnails (.step+ package thumbnail.png via miniz)
// ----------------

struct GraphThumb {
  GLuint tex = 0;
  int w = 0, h = 0;
  bool failed = false;
  double lastUse = 0.0;
};

std::unordered_map<std::string, GraphThumb> &thumbCache() {
  static std::unordered_map<std::string, GraphThumb> c;
  return c;
}

bool readZipEntry(const std::string &zipPath, const char *entry,
                  std::string &out) {
  mz_zip_archive z;
  std::memset(&z, 0, sizeof(z));
  if (!mz_zip_reader_init_file(&z, zipPath.c_str(), 0))
    return false;
  bool ok = false;
  int idx = mz_zip_reader_locate_file(&z, entry, nullptr, 0);
  if (idx >= 0) {
    size_t sz = 0;
    void *p = mz_zip_reader_extract_to_heap(&z, (mz_uint)idx, &sz, 0);
    if (p) {
      out.assign(static_cast<const char *>(p), sz);
      mz_free(p);
      ok = true;
    }
  }
  mz_zip_reader_end(&z);
  return ok;
}

void freeAllGraphThumbs() {
  auto &c = thumbCache();
  for (auto &kv : c) {
    if (kv.second.tex)
      glDeleteTextures(1, &kv.second.tex);
  }
  c.clear();
}

// Lazy-load thumbnail.png from a .step+ package. Returns nullptr on miss/fail.
// Cap 64 entries with simple LRU eviction.
const GraphThumb *ensureGraphThumb(const std::string &absPath) {
  if (absPath.empty())
    return nullptr;
  auto &c = thumbCache();
  auto it = c.find(absPath);
  if (it != c.end()) {
    it->second.lastUse = ImGui::GetTime();
    return it->second.failed || !it->second.tex ? nullptr : &it->second;
  }

  GraphThumb slot;
  slot.lastUse = ImGui::GetTime();
  // Only packages (.step+) carry an embedded thumbnail.png.
  std::string lower = absPath;
  for (char &ch : lower)
    ch = (char)std::tolower((unsigned char)ch);
  const bool isPkg =
      lower.size() >= 6 && (lower.compare(lower.size() - 6, 6, ".step+") == 0 ||
                            lower.compare(lower.size() - 5, 5, ".step") == 0 ||
                            lower.find(".step+") != std::string::npos);
  if (!isPkg) {
    // Still try if path exists as zip-like; otherwise mark failed once.
    std::error_code ec;
    if (!std::filesystem::is_regular_file(absPath, ec)) {
      slot.failed = true;
      c[absPath] = slot;
      return nullptr;
    }
  }

  std::string png;
  if (!readZipEntry(absPath, "thumbnail.png", png) || png.empty()) {
    slot.failed = true;
    c[absPath] = std::move(slot);
    return nullptr;
  }
  int w = 0, h = 0, n = 0;
  unsigned char *px =
      stbi_load_from_memory(reinterpret_cast<const unsigned char *>(png.data()),
                            (int)png.size(), &w, &h, &n, 4);
  if (!px || w <= 0 || h <= 0) {
    if (px)
      stbi_image_free(px);
    slot.failed = true;
    c[absPath] = std::move(slot);
    return nullptr;
  }
  GLuint tex = 0;
  glGenTextures(1, &tex);
  glBindTexture(GL_TEXTURE_2D, tex);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
  glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE,
               px);
  stbi_image_free(px);
  slot.tex = tex;
  slot.w = w;
  slot.h = h;

  constexpr size_t kThumbCap = 64;
  while (c.size() >= kThumbCap) {
    auto oldest = c.begin();
    for (auto jt = c.begin(); jt != c.end(); ++jt)
      if (jt->second.lastUse < oldest->second.lastUse)
        oldest = jt;
    if (oldest->second.tex)
      glDeleteTextures(1, &oldest->second.tex);
    c.erase(oldest);
  }
  auto [ins, ok] = c.emplace(absPath, std::move(slot));
  (void)ok;
  return &ins->second;
}

// Reveal a path (or its parent folder) in the OS file manager.
void revealInFolder(const std::string &absPath) {
  if (absPath.empty())
    return;
  std::error_code ec;
  std::filesystem::path p(absPath);
  std::filesystem::path target = p;
  if (!std::filesystem::is_directory(p, ec))
    target = p.parent_path();
  if (target.empty())
    return;
  std::string s = target.generic_string();
#if defined(_WIN32)
  std::string cmd = "explorer \"" + s + "\"";
  (void)std::system(cmd.c_str());
#elif defined(__APPLE__)
  std::string cmd = "open \"" + s + "\" >/dev/null 2>&1 &";
  (void)std::system(cmd.c_str());
#else
  std::string cmd = "xdg-open \"" + s + "\" >/dev/null 2>&1 &";
  (void)std::system(cmd.c_str());
#endif
}

// Compact labelled field row used by the node inspector.
void panelFieldLabel(const char *k) {
  ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                               palette.textDisabled[2], 1.0f));
  ImGui::TextUnformatted(k);
  ImGui::PopStyleColor();
}

// 3D projection (view3d mode only): yaw around Y, pitch around X, then a
// weak perspective divide. O(1) per point per frame with precomputed trig;
// the layout itself stays 2D (z is a static per-node attribute from the
// harmonic seed), so compute cost is negligible and fully deterministic.
struct Proj3 {
  float cy = 1.0f, sy = 0.0f, cp = 1.0f, sp = 0.0f;
  float focal = 2400.0f;
};

Proj3 makeProj(const GraphTabState &G) {
  Proj3 P;
  P.cy = std::cos(G.orbitYaw);
  P.sy = std::sin(G.orbitYaw);
  P.cp = std::cos(G.orbitPitch);
  P.sp = std::sin(G.orbitPitch);
  P.focal = 2400.0f * ui::uiScale;
  return P;
}

// Returns perspective scale in perspOut (1 = at focal plane).
void projectPoint(const GraphTabState &G, const Proj3 &P, float lx, float ly,
                  float lz, float &px, float &py, float &perspOut) {
  if (!G.view3d) {
    px = lx;
    py = ly;
    perspOut = 1.0f;
    return;
  }
  const float x1 = lx * P.cy + lz * P.sy;
  const float z1 = -lx * P.sy + lz * P.cy;
  const float y1 = ly * P.cp - z1 * P.sp;
  const float z2 = ly * P.sp + z1 * P.cp;
  const float persp = P.focal / std::max(200.0f * ui::uiScale, P.focal + z2);
  px = x1 * persp;
  py = y1 * persp;
  perspOut = persp;
}

void fitView(GraphTabState &G, ImVec2 canvasSz) {
  if (G.visibleIdx.empty() || canvasSz.x < 1.0f || canvasSz.y < 1.0f)
    return;
  const Proj3 P = makeProj(G);
  float minX = 1e9f, minY = 1e9f, maxX = -1e9f, maxY = -1e9f;
  for (int vi : G.visibleIdx) {
    const GNode &n = G.nodes[vi];
    float px, py, persp;
    projectPoint(G, P, n.lx, n.ly, n.lz, px, py, persp);
    const float r = std::max(n.radius, n.bubbleR) * persp;
    minX = std::min(minX, px - r);
    minY = std::min(minY, py - r);
    maxX = std::max(maxX, px + r);
    maxY = std::max(maxY, py + r);
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

void centerOnNode(GraphTabState &G, const GNode &n, ImVec2 canvasSz) {
  const Proj3 P = makeProj(G);
  float px, py, persp;
  projectPoint(G, P, n.lx, n.ly, n.lz, px, py, persp);
  G.panX = canvasSz.x * 0.5f - px * G.zoom;
  G.panY = canvasSz.y * 0.5f - py * G.zoom;
}

bool matchesSearch(const GNode &n, const char *q) {
  if (!q || !q[0])
    return true;
  // case-insensitive substring on label / kind / id / path
  auto has = [q](const std::string &s) {
    if (s.empty())
      return false;
    std::string a = s, b = q;
    for (char &c : a)
      c = (char)std::tolower((unsigned char)c);
    for (char &c : b)
      c = (char)std::tolower((unsigned char)c);
    return a.find(b) != std::string::npos;
  };
  return has(n.label) || has(n.kind) || has(n.id) || has(n.path);
}

// Search match cache: matchIds (visible), vis/all match tallies for the
// hidden-match hint. Rebuilt only when the query or visible set changes.
void rebuildMatchCache(GraphTabState &G) {
  G.matchIds.clear();
  G.visMatches = 0;
  G.allMatches = 0;
  std::unordered_set<std::string> visSet;
  visSet.reserve(G.visibleIdx.size() * 2);
  for (int vi : G.visibleIdx)
    visSet.insert(G.nodes[vi].id);
  for (const auto &n : G.nodes) {
    if (!matchesSearch(n, G.search))
      continue;
    ++G.allMatches;
    if (visSet.count(n.id)) {
      ++G.visMatches;
      G.matchIds.insert(n.id);
    }
  }
  G.matchDirty = false;
}

void drawLegend(ImDrawList *dl, ImVec2 origin, bool projectScope) {
  const float S = ui::uiScale;
  // Scope note + edge styles + kinds actually shown in this view.
  struct Row {
    const char *label;
    ImU32 col;
    bool dashed;
    bool isNode;
    bool header;
  };
  std::vector<Row> rows;
  rows.push_back({projectScope ? "Project view" : "Document view",
                  Palette::toU32(palette.textDim), false, false, true});
  rows.push_back(
      {"extracted", edgeColor("CONTAINS", 0.85f), false, false, false});
  rows.push_back(
      {"inferred", edgeColor("DEPENDS_ON", 0.85f), true, false, false});
  rows.push_back({"manual", kindColor("project", 0.90f), false, false, false});
  rows.push_back({"ai link", kindColor("project", 0.90f), true, false, false});
  if (projectScope) {
    rows.push_back({"project", kindColor("project"), false, true, false});
    rows.push_back({"assembly", kindColor("assembly"), false, true, false});
    rows.push_back({"part", kindColor("part"), false, true, false});
    rows.push_back({"drawing", kindColor("drawing"), false, true, false});
    rows.push_back(
        {"(agg ×N)", edgeColor("REFERENCES", 0.85f), false, false, false});
  } else {
    rows.push_back({"feature", kindColor("feature"), false, true, false});
    rows.push_back({"sketch", kindColor("sketch"), false, true, false});
    rows.push_back({"equation", kindColor("equation"), false, true, false});
    rows.push_back({"mate", kindColor("mate"), false, true, false});
    rows.push_back({"body", kindColor("body"), false, true, false});
  }
  float y = origin.y;
  float x = origin.x;
  ImU32 tdim = Palette::toU32(palette.textDim);
  ImU32 tdis = Palette::toU32(palette.textDisabled);
  for (const auto &r : rows) {
    if (r.header) {
      dl->AddText(ImVec2(x, y), tdis, r.label);
      y += 16 * S;
      continue;
    }
    if (r.isNode) {
      dl->AddCircleFilled(ImVec2(x + 6 * S, y + 6 * S), 5 * S, r.col);
      dl->AddCircle(
          ImVec2(x + 6 * S, y + 6 * S), 5 * S,
          ImGui::GetColorU32(ImVec4(palette.border[0], palette.border[1],
                                    palette.border[2], 0.9f)),
          0, 1.0f);
    } else if (r.dashed) {
      drawDashedLine(dl, ImVec2(x, y + 6 * S), ImVec2(x + 18 * S, y + 6 * S),
                     r.col, 1.5f * S, 4 * S, 3 * S);
    } else {
      dl->AddLine(ImVec2(x, y + 6 * S), ImVec2(x + 18 * S, y + 6 * S), r.col,
                  1.5f * S);
    }
    dl->AddText(ImVec2(x + 24 * S, y), tdim, r.label);
    y += 16 * S;
  }
}

void drawSidePanel(AppState &st, float panelW) {
  auto &G = S();
  const float S = ui::uiScale;
  ui::pushGlassChildStyle(10.0f);
  ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12 * S, 10 * S));
  ImGui::BeginChild("##graph_side", ImVec2(panelW, 0), ImGuiChildFlags_Borders);
  ui::popGlassChildStyle();

  auto row = [](const char *k, const std::string &v) {
    if (v.empty())
      return;
    panelFieldLabel(k);
    ImGui::SameLine(0, 8 * ui::uiScale);
    ImGui::PushStyleColor(ImGuiCol_Text,
                          ImVec4(palette.textDim[0], palette.textDim[1],
                                 palette.textDim[2], 1.0f));
    ImGui::TextWrapped("%s", v.c_str());
    ImGui::PopStyleColor();
  };
  auto nodeLabel = [&](const std::string &id) -> std::string {
    auto it = G.idIndex.find(id);
    if (it != G.idIndex.end())
      return G.nodes[it->second].label;
    return id;
  };

  // --- an edge is selected (indexes into visibleEdges) ---------------------
  if (G.selectedEdge >= 0 && G.selectedEdge < (int)G.visibleEdges.size()) {
    const VEdge &ve = G.visibleEdges[G.selectedEdge];
    const GEdge *srcE =
        (ve.srcEdgeIdx >= 0 && ve.srcEdgeIdx < (int)G.edges.size())
            ? &G.edges[ve.srcEdgeIdx]
            : nullptr;
    plmSectionHeader("Link", "mesh");
    ImGui::Dummy(ImVec2(0, 6 * S));
    ImGui::PushStyleColor(ImGuiCol_Text, ImGui::ColorConvertU32ToFloat4(
                                             edgeColor(ve.kind, 1.0f)));
    if (ve.aggCount > 1)
      ImGui::Text("[%s ×%d]", ve.kind.c_str(), ve.aggCount);
    else
      ImGui::Text("[%s]", ve.kind.c_str());
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, 6 * S));

    panelFieldLabel("from");
    ImGui::SameLine(0, 8 * S);
    if (ImGui::SmallButton((nodeLabel(ve.src) + "##lnksrc").c_str())) {
      G.selectedId = ve.src;
      G.selectedEdge = -1;
      clearBlast(G);
    }
    panelFieldLabel("to");
    ImGui::SameLine(0, 8 * S);
    if (ImGui::SmallButton((nodeLabel(ve.dst) + "##lnkdst").c_str())) {
      G.selectedId = ve.dst;
      G.selectedEdge = -1;
      clearBlast(G);
    }

    ImGui::Dummy(ImVec2(0, 6 * S));
    if (ve.aggCount > 1) {
      char aggBuf[64];
      std::snprintf(aggBuf, sizeof(aggBuf), "aggregated from %d links",
                    ve.aggCount);
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
        std::string v = it->is_string()
                            ? it->get<std::string>()
                            : (it->is_null() ? std::string() : it->dump());
        row(it.key().c_str(), v);
      }
    }
    ImGui::Dummy(ImVec2(0, 10 * S));
    const std::string &origin = srcE ? srcE->origin : ve.origin;
    bool authored = (origin == "manual" || origin == "ai");
    if (authored && ve.aggCount <= 1 && srcE) {
      std::string linkId = srcE->meta.is_object()
                               ? srcE->meta.value("link_id", std::string())
                               : "";
      if (graphGlassButton("##rmlink", "Remove link", true, -1.0f) &&
          !linkId.empty())
        requestLinkRemove(st, linkId);
    } else if (!authored) {
      ImGui::PushStyleColor(ImGuiCol_Text,
                            ImVec4(palette.textDim[0], palette.textDim[1],
                                   palette.textDim[2], 1.0f));
      ImGui::TextWrapped(
          "Derived from the model — edit the model to change it.");
      ImGui::PopStyleColor();
    }
    ImGui::EndChild();
    ImGui::PopStyleVar();
    return;
  }

  plmSectionHeader("Node", "mesh");
  ImGui::Dummy(ImVec2(0, 6 * S));

  const GNode *sel = nullptr;
  if (!G.selectedId.empty()) {
    auto it = G.idIndex.find(G.selectedId);
    if (it != G.idIndex.end())
      sel = &G.nodes[it->second];
  }

  if (!sel) {
    ImGui::PushStyleColor(ImGuiCol_Text,
                          ImVec4(palette.textDim[0], palette.textDim[1],
                                 palette.textDim[2], 1.0f));
    if (!G.linkArmSrc.empty())
      ImGui::TextWrapped(
          "Click the target node to link it — or Esc to cancel.");
    else if (G.scopeDocId.empty())
      ImGui::TextWrapped("Click a node to inspect it. Double-click a part or "
                         "document to isolate its references.");
    else
      ImGui::TextWrapped("Click a node to inspect it, or a link to see its "
                         "properties. Double-click empty space to go back.");
    ImGui::PopStyleColor();
    ImGui::EndChild();
    ImGui::PopStyleVar();
    return;
  }

  // ---- header: kind pill + title -----------------------------------------
  {
    ImDrawList *dl = ImGui::GetWindowDrawList();
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const char *ks = kindShort(sel->kind);
    const ImVec2 ksSz = ImGui::CalcTextSize(ks);
    const float pillH = ImGui::GetTextLineHeight() + 4 * S;
    const float pillW = ksSz.x + 12 * S;
    const ImU32 fill = kindColor(sel->kind, 0.85f);
    dl->AddRectFilled(p0, ImVec2(p0.x + pillW, p0.y + pillH), fill, 6.0f * S);
    dl->AddText(ImVec2(p0.x + 6 * S, p0.y + 2 * S),
                IM_COL32(255, 255, 255, 240), ks);
    ImGui::Dummy(ImVec2(pillW + 8 * S, pillH));
    ImGui::SameLine(0, 0);
    ImGui::PushStyleColor(
        ImGuiCol_Text,
        ImVec4(palette.text[0], palette.text[1], palette.text[2], 1.0f));
    ImGui::SetCursorPosY(ImGui::GetCursorPosY() + 2 * S);
    ImGui::TextWrapped("%s", sel->label.c_str());
    ImGui::PopStyleColor();
  }

  ImGui::Dummy(ImVec2(0, 8 * S));

  // Resolve open path once for preview + actions.
  std::string absPath;
  if ((isDrillableKind(sel->kind) || sel->kind == "project") &&
      !sel->path.empty())
    absPath = resolveNodeAbsPath(st, *sel);
  else if (!sel->docId.empty()) {
    auto dit = G.idIndex.find(sel->docId);
    if (dit != G.idIndex.end() && !G.nodes[dit->second].path.empty())
      absPath = resolveNodeAbsPath(st, G.nodes[dit->second]);
  }

  // ---- iso preview (package thumbnail / last-solved art) -----------------
  // Fills the empty inspector band under the title — SolidWorks-style card.
  {
    const float previewH = 148.0f * S;
    const float previewW = ImGui::GetContentRegionAvail().x;
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const ImVec2 p1(p0.x + previewW, p0.y + previewH);
    ImDrawList *dl = ImGui::GetWindowDrawList();
    ui::glassBackground(dl, p0, p1, 10.0f);
    dl->AddRect(p0, p1,
                ImGui::GetColorU32(ImVec4(palette.glassBorder[0],
                                          palette.glassBorder[1],
                                          palette.glassBorder[2], 0.55f)),
                10.0f * S, 0, 1.0f * S);

    const GraphThumb *th = nullptr;
    if (!absPath.empty())
      th = ensureGraphThumb(absPath);
    // Also try the node's own path if different from open target.
    if (!th && !sel->path.empty()) {
      const std::string own = resolveNodeAbsPath(st, *sel);
      if (!own.empty() && own != absPath)
        th = ensureGraphThumb(own);
    }

    if (th && th->tex) {
      float tw = (float)th->w, thh = (float)th->h;
      const float pad = 10.0f * S;
      const float maxW = previewW - pad * 2, maxH = previewH - pad * 2;
      float sc = std::min(maxW / std::max(tw, 1.f), maxH / std::max(thh, 1.f));
      tw *= sc;
      thh *= sc;
      const ImVec2 ip0(p0.x + (previewW - tw) * 0.5f,
                       p0.y + (previewH - thh) * 0.5f);
      dl->AddImage((ImTextureID)(intptr_t)th->tex, ip0,
                   ImVec2(ip0.x + tw, ip0.y + thh));
    } else {
      // Empty state — kind glyph + short hint (no dead white box).
      const char *hint = absPath.empty() ? "No preview" : "No preview yet";
      const ImVec2 hsz = ImGui::CalcTextSize(hint);
      dl->AddText(ImVec2(p0.x + (previewW - hsz.x) * 0.5f,
                         p0.y + (previewH - hsz.y) * 0.5f),
                  Palette::toU32(palette.textDisabled), hint);
      // Soft kind disc behind the hint.
      const float r = 22.0f * S;
      dl->AddCircleFilled(
          ImVec2(p0.x + previewW * 0.5f, p0.y + previewH * 0.38f), r,
          kindColor(sel->kind, 0.35f), 28);
    }
    ImGui::Dummy(ImVec2(previewW, previewH));
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal) && !absPath.empty())
      ImGui::SetTooltip("%s", absPath.c_str());
  }

  ImGui::Dummy(ImVec2(0, 10 * S));

  // ---- compact facts (no raw id dump; path is the locator) ---------------
  {
    int outE = 0, inE = 0;
    for (const auto &e : G.edges) {
      if (e.src == sel->id)
        ++outE;
      if (e.dst == sel->id)
        ++inE;
    }
    char linksBuf[48];
    std::snprintf(linksBuf, sizeof(linksBuf), "%d out · %d in", outE, inE);

    // One-line summary chips instead of a wall of key/value.
    ImGui::PushStyleColor(ImGuiCol_Text,
                          ImVec4(palette.textDim[0], palette.textDim[1],
                                 palette.textDim[2], 1.0f));
    ImGui::Text("%s", sel->kind.c_str());
    ImGui::SameLine(0, 10 * S);
    ImGui::TextUnformatted("·");
    ImGui::SameLine(0, 10 * S);
    ImGui::TextUnformatted(linksBuf);
    if (G.scopeDocId.empty()) {
      auto dit = G.descCounts.find(sel->id);
      if (dit != G.descCounts.end() && dit->second.total() > 0) {
        ImGui::SameLine(0, 10 * S);
        ImGui::TextUnformatted("·");
        ImGui::SameLine(0, 10 * S);
        {
          const std::string badge = formatDescBadge(dit->second);
          ImGui::TextUnformatted(badge.c_str());
        }
      }
    }
    if (G.scopeDecomp) {
      auto qit = G.instCount.find(sel->id);
      if (qit != G.instCount.end() && qit->second > 0) {
        ImGui::SameLine(0, 10 * S);
        ImGui::TextUnformatted("·");
        ImGui::SameLine(0, 10 * S);
        ImGui::Text("×%d", qit->second);
      }
    }
    ImGui::PopStyleColor();
  }

  // Path row with folder reveal — keep; hide full id under a details fold.
  if (!sel->path.empty()) {
    ImGui::Dummy(ImVec2(0, 6 * S));
    panelFieldLabel("path");
    ImGui::SameLine(0, 8 * S);
    const std::string abs =
        absPath.empty() ? resolveNodeAbsPath(st, *sel) : absPath;
    const float iconBtn = ImGui::GetFrameHeight();
    ImVec2 ip0 = ImGui::GetCursorScreenPos();
    bool folderClick =
        ImGui::InvisibleButton("##reveal_folder", ImVec2(iconBtn, iconBtn));
    bool fh = ImGui::IsItemHovered();
    ImDrawList *dl = ImGui::GetWindowDrawList();
    if (fh) {
      dl->AddRectFilled(
          ip0, ImVec2(ip0.x + iconBtn, ip0.y + iconBtn),
          ImGui::GetColorU32(
              ImVec4(palette.text[0], palette.text[1], palette.text[2], 0.08f)),
          6.0f * S);
    }
    ui::drawIconGlass(dl, "body_folder",
                      ImVec2(ip0.x + iconBtn * 0.5f, ip0.y + iconBtn * 0.5f),
                      16.0f * S,
                      Palette::toU32(fh ? palette.accent : palette.textDim), 0);
    if (folderClick && !abs.empty())
      revealInFolder(abs);
    if (fh)
      ImGui::SetTooltip("Show in folder\n%s",
                        abs.empty() ? sel->path.c_str() : abs.c_str());
    ImGui::SameLine(0, 6 * S);
    ImGui::PushStyleColor(ImGuiCol_Text,
                          ImVec4(palette.textDim[0], palette.textDim[1],
                                 palette.textDim[2], 1.0f));
    const float avail = ImGui::GetContentRegionAvail().x;
    // Prefer basename; full path on hover.
    std::string base = sel->path;
    {
      auto slash = base.find_last_of("/\\");
      if (slash != std::string::npos)
        base = base.substr(slash + 1);
    }
    ImVec2 psz = ImGui::CalcTextSize(base.c_str());
    if (psz.x > avail && avail > 24 * S) {
      std::string clipped = base;
      while (clipped.size() > 4 &&
             ImGui::CalcTextSize(("…" + clipped).c_str()).x > avail)
        clipped.erase(clipped.begin());
      ImGui::TextUnformatted(("…" + clipped).c_str());
    } else {
      ImGui::TextUnformatted(base.c_str());
    }
    ImGui::PopStyleColor();
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal))
      ImGui::SetTooltip("%s", abs.empty() ? sel->path.c_str() : abs.c_str());
  }

  // Collapsed technical details — id / origin / meta (keeps the panel clean).
  ImGui::Dummy(ImVec2(0, 4 * S));
  ImGui::PushStyleColor(
      ImGuiCol_Header,
      ImVec4(palette.text[0], palette.text[1], palette.text[2], 0.06f));
  ImGui::PushStyleColor(
      ImGuiCol_HeaderHovered,
      ImVec4(palette.text[0], palette.text[1], palette.text[2], 0.10f));
  if (ImGui::TreeNode("Details##node_details")) {
    row("id", sel->id);
    row("origin", sel->origin);
    if (!sel->docId.empty() && sel->docId != sel->id)
      row("document", sel->docId);
    row("meta", sel->metaSummary);
    ImGui::TreePop();
  }
  ImGui::PopStyleColor(2);

  ImGui::Dummy(ImVec2(0, 10 * S));
  ImGui::Separator();
  ImGui::Dummy(ImVec2(0, 8 * S));

  // ---- actions ------------------------------------------------------------
  if (!absPath.empty()) {
    if (graphGlassButton("##open_doc", "Open document", true, -1.0f, "new_doc"))
      st.requestOpenPath = absPath;
    ImGui::Dummy(ImVec2(0, 4 * S));
  }

  if (sel->id != G.scopeDocId && isDrillableKind(sel->kind)) {
    if (graphGlassButton("##drill", "Drill in", true, -1.0f))
      drillInto(G, sel->id);
    ImGui::Dummy(ImVec2(0, 4 * S));
  }
  if (!G.scopeDocId.empty()) {
    if (graphGlassButton("##drillup", "Back", false, -1.0f))
      scopeBack(G);
    ImGui::Dummy(ImVec2(0, 4 * S));
  }

  if (graphGlassButton("##blast", G.blastLoading ? "Blasting…" : "Impact blast",
                       true, -1.0f) &&
      !G.blastLoading) {
    requestBlast(st, sel->id);
  }
  if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal))
    ImGui::SetTooltip("Show change impact — ripple + affected neighborhood.");
  ImGui::Dummy(ImVec2(0, 4 * S));

  if (G.linkArmSrc == sel->id) {
    ImGui::PushStyleColor(
        ImGuiCol_Text,
        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f));
    ImGui::TextWrapped("Click the target node…");
    ImGui::PopStyleColor();
    if (graphGlassButton("##armcancel", "Cancel", false, -1.0f))
      G.linkArmSrc.clear();
  } else if (graphGlassButton("##armlink", "Link from here…", false, -1.0f)) {
    G.linkArmSrc = sel->id;
  }

  if (!G.blast.empty()) {
    ImGui::Dummy(ImVec2(0, 6 * S));
    ImGui::PushStyleColor(ImGuiCol_Text,
                          ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                                 palette.statusWarn[2], 1.0f));
    ImGui::Text("Impact: %d node%s", (int)G.blast.size(),
                G.blast.size() == 1 ? "" : "s");
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, 4 * S));
    if (graphGlassButton("##clear_blast", "Clear blast", false, -1.0f))
      clearBlast(G);
  }

  ImGui::EndChild();
  ImGui::PopStyleVar();
}

void drawCanvas(AppState &st, ImVec2 canvasSz) {
  auto &G = S();
  const float S = ui::uiScale;
  // st used for path resolution / hover thumbs

  ImGui::InvisibleButton("##graph_canvas", canvasSz,
                         ImGuiButtonFlags_MouseButtonLeft |
                             ImGuiButtonFlags_MouseButtonMiddle |
                             ImGuiButtonFlags_MouseButtonRight);
  const bool hovered = ImGui::IsItemHovered();
  const ImVec2 origin = ImGui::GetItemRectMin();
  ImDrawList *dl = ImGui::GetWindowDrawList();
  dl->PushClipRect(origin, ImVec2(origin.x + canvasSz.x, origin.y + canvasSz.y),
                   true);

  // Frosted canvas bed.
  ui::glassBackground(
      dl, origin, ImVec2(origin.x + canvasSz.x, origin.y + canvasSz.y), 10.0f);

  if (G.layoutDirty)
    rebuildForce(G);

  // Fit ONLY on explicit request (Fit button). Polls/relayouts never refit.
  if (G.fitRequested && !G.visibleIdx.empty()) {
    fitView(G, canvasSz);
    G.fitRequested = false;
  }

  // Advance physics and copy graph-space positions back into the render
  // cache. Frozen when settled unless a node is being dragged — idle
  // frames cost nothing but the draw itself. While active we run extra
  // substeps per frame so the visible settle takes ~2s, not ~6s.
  if (!G.visibleIdx.empty() && (G.dragNode >= 0 || !G.force.settled())) {
    G.force.step(std::min(ImGui::GetIO().DeltaTime, 0.05f));
    if (!G.force.settled()) {
      G.force.step(1.0f / 60.0f);
      // 3D repulsion costs ~3× (27-cell neighborhood) — half the catch-up
      // rate keeps frame time flat; settle still takes ~2–4 s.
      if (!G.view3d)
        G.force.step(1.0f / 60.0f);
    }
    for (int vi : G.visibleIdx) {
      int slot = (vi < (int)G.forceSlot.size()) ? G.forceSlot[vi] : -1;
      if (slot < 0)
        continue;
      G.nodes[vi].lx = G.force.x(slot);
      G.nodes[vi].ly = G.force.y(slot);
      G.nodes[vi].lz = G.force.z(slot);
    }
    // Settle transition: persist the solve state; frame the relaxed
    // layout once (only if the user hasn't taken over the view).
    bool nowSettled = G.force.settled();
    if (!G.wasSettled && nowSettled) {
      maybeSaveLayout(G);
      if (G.fitOnSettle && !G.userNavigated) {
        fitView(G, canvasSz);
      }
      G.fitOnSettle = false;
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
      G.zoom =
          std::clamp(G.zoom * (wheel > 0 ? 1.12f : 1.0f / 1.12f), 0.12f, 6.0f);
      G.panX = (mp.x - origin.x) - lx * G.zoom;
      G.panY = (mp.y - origin.y) - ly * G.zoom;
    }
  }

  // Pan: middle-drag anywhere; in 3D right-drag pans, in 2D left-drag on
  // empty background pans (3D swaps left/right per CAD convention).
  if (hovered && G.dragNode < 0 && G.dragCandidate < 0 &&
      (ImGui::IsMouseDragging(ImGuiMouseButton_Middle) ||
       (G.view3d && ImGui::IsMouseDragging(ImGuiMouseButton_Right)) ||
       (!G.view3d && ImGui::IsMouseDragging(ImGuiMouseButton_Left) &&
        G.hoverId.empty() && !ImGui::GetIO().KeyCtrl))) {
    G.userNavigated = true;
    ImVec2 d = ImGui::GetIO().MouseDelta;
    G.panX += d.x;
    G.panY += d.y;
  }

  // Orbit: left-drag on empty background rotates the 3D projection
  // (view3d only; swapped with pan per CAD convention).
  if (G.view3d && hovered && G.dragNode < 0 && G.dragCandidate < 0 &&
      G.hoverId.empty() && !ImGui::GetIO().KeyCtrl &&
      ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
    G.userNavigated = true;
    ImVec2 d = ImGui::GetIO().MouseDelta;
    G.orbitYaw += d.x * 0.008f;
    G.orbitPitch = std::clamp(G.orbitPitch + d.y * 0.008f, -1.35f, 1.35f);
  }

  // 3D projection state: trig precomputed once per frame, O(1) per point.
  const Proj3 P = makeProj(G);
  auto toScreen = [&](float lx, float ly, float lz) -> ImVec2 {
    float px, py, persp;
    projectPoint(G, P, lx, ly, lz, px, py, persp);
    return ImVec2(origin.x + G.panX + px * G.zoom,
                  origin.y + G.panY + py * G.zoom);
  };
  auto nodeScreen = [&](const GNode &n, float &perspOut) -> ImVec2 {
    float px, py;
    projectPoint(G, P, n.lx, n.ly, n.lz, px, py, perspOut);
    return ImVec2(origin.x + G.panX + px * G.zoom,
                  origin.y + G.panY + py * G.zoom);
  };

  // Viewport culling helpers (screen space, padded past the clip rect).
  const ImVec2 scrMin(origin.x - 40.0f, origin.y - 40.0f);
  const ImVec2 scrMax(origin.x + canvasSz.x + 40.0f,
                      origin.y + canvasSz.y + 40.0f);
  auto onScreen = [&](ImVec2 p) {
    return p.x >= scrMin.x && p.x <= scrMax.x && p.y >= scrMin.y &&
           p.y <= scrMax.y;
  };

  // Search filter over VISIBLE nodes (cached; rebuilt on query/visible change).
  const bool filtering = G.search[0] != 0;
  if (filtering && G.matchDirty)
    rebuildMatchCache(G);

  // Focus id for incidence dimming: hover wins, else selection.
  const std::string &focusId = !G.hoverId.empty() ? G.hoverId : G.selectedId;
  const bool incidenceDim = !focusId.empty() || !G.blast.empty();

  // Precompute which visible edges touch the focus node (or blast set).
  auto edgeIncident = [&](const VEdge &e) -> bool {
    if (!G.blast.empty())
      return G.blast.count(e.src) || G.blast.count(e.dst);
    if (focusId.empty())
      return false;
    return e.src == focusId || e.dst == focusId;
  };

  // ---- Containment bubble outlines (under edges) --------------------------
  // Nested packing makes every parent a bubble of its contents; outline it
  // so assembly/part nesting reads at a glance. LOD-culled when tiny.
  for (int vi : G.visibleIdx) {
    const GNode &n = G.nodes[vi];
    if (n.bubbleR <= n.radius + 1.0f)
      continue;
    float perspB = 1.0f;
    const ImVec2 c = nodeScreen(n, perspB);
    const float rb = n.bubbleR * perspB * G.zoom;
    if (rb < 10.0f)
      continue;
    dl->AddCircleFilled(c, rb, kindColor(n.kind, 0.045f), 48);
    dl->AddCircle(c, rb, kindColor(n.kind, 0.30f), 48, 1.5f);
  }

  // ---- Edges (visibleEdges only) ------------------------------------------
  // Coarse LOD for huge webs: solid 1px lines, no mid-edge labels.
  const bool edgeLod = (int)G.visibleEdges.size() > 4000;
  for (int ei = 0; ei < (int)G.visibleEdges.size(); ++ei) {
    const VEdge &e = G.visibleEdges[ei];
    if (G.hiddenKinds.count(e.kind))
      continue;
    auto si = G.idIndex.find(e.src), di = G.idIndex.find(e.dst);
    if (si == G.idIndex.end() || di == G.idIndex.end())
      continue;
    const GNode &a = G.nodes[si->second];
    const GNode &b = G.nodes[di->second];

    // Skip same-layer CONTAINS (defensive declutter).
    if (e.kind == "CONTAINS" && std::fabs(a.ly - b.ly) < 1.0f)
      continue;

    float perspA = 1.0f, perspB = 1.0f;
    ImVec2 p0 = nodeScreen(a, perspA);
    ImVec2 p1 = nodeScreen(b, perspB);

    bool dimf = filtering && !G.matchIds.count(a.id) && !G.matchIds.count(b.id);
    bool incident = edgeIncident(e);
    bool selEdge = (ei == G.selectedEdge);
    bool authored = (e.origin == "manual" || e.origin == "ai");

    float alpha;
    if (dimf)
      alpha = 0.08f;
    else if (selEdge)
      alpha = 1.0f;
    else if (!G.blast.empty() && !incident)
      alpha = 0.07f;
    else if (incidenceDim && !incident)
      alpha = 0.08f;
    else if (incident)
      alpha = 0.95f;
    else
      alpha = 0.55f;
    // Depth cue: far edges fade back (3D mode only).
    if (G.view3d)
      alpha *= std::clamp(0.5f * (perspA + perspB), 0.55f, 1.0f);

    ImU32 col = edgeColor(e.kind, alpha);
    if (authored && !dimf && (incident || selEdge || !incidenceDim))
      col =
          ImGui::GetColorU32(ImVec4(palette.accent[0], palette.accent[1],
                                    palette.accent[2], selEdge ? 1.0f : 0.85f));
    else if (incident && !G.blast.empty() && !dimf)
      col = ImGui::GetColorU32(ImVec4(palette.statusWarn[0],
                                      palette.statusWarn[1],
                                      palette.statusWarn[2], 0.90f));

    if (!onScreen(p0) && !onScreen(p1))
      continue; // viewport cull
    float thick = (edgeLod   ? 1.0f
                   : selEdge ? 3.0f
                             : (incident && !dimf ? 2.2f : 1.3f)) *
                  S;
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
      const GNode &n = G.nodes[vi];
      float persp = 1.0f;
      ImVec2 c = nodeScreen(n, persp);
      float r = n.radius * persp * G.zoom;
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
  if (hovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
      !G.hoverId.empty()) {
    auto it = G.idIndex.find(G.hoverId);
    G.dragCandidate = (it != G.idIndex.end()) ? it->second : -1;
  }
  if (G.dragCandidate >= 0 && G.dragNode < 0 &&
      ImGui::IsMouseDragging(ImGuiMouseButton_Left, 4.0f * S)) {
    G.dragNode = G.dragCandidate;
    int slot =
        (G.dragNode < (int)G.forceSlot.size()) ? G.forceSlot[G.dragNode] : -1;
    if (slot >= 0)
      G.force.setPinned(slot, true);
  }
  if (G.dragNode >= 0) {
    int slot =
        (G.dragNode < (int)G.forceSlot.size()) ? G.forceSlot[G.dragNode] : -1;
    if (ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
      if (slot >= 0) {
        if (G.view3d) {
          // Unproject the cursor onto the node's depth plane: convert the
          // desired projected position back into graph x/y (z unchanged).
          // First-order inverse — ignores the small pitch×yaw cross-term
          // (~15% tracking skew at default orbit), intentional: exact
          // inversion isn't worth the complexity for an interactive drag.
          // Shift+drag instead moves the node in DEPTH (z).
          GNode &dn = G.nodes[G.dragNode];
          float px, py, persp;
          projectPoint(G, P, dn.lx, dn.ly, dn.lz, px, py, persp);
          if (ImGui::GetIO().KeyShift) {
            G.force.setZ(slot, dn.lz - ImGui::GetIO().MouseDelta.y /
                                           (G.zoom * std::max(persp, 0.05f)));
          } else {
            const float tx = (mp.x - origin.x - G.panX) / G.zoom;
            const float ty = (mp.y - origin.y - G.panY) / G.zoom;
            const float ux = (tx - px) / std::max(persp, 0.05f);
            const float uy = (ty - py) / std::max(persp, 0.05f);
            const float cyC = std::clamp(P.cy, 0.35f, 1.0f);
            const float cpC = std::clamp(P.cp, 0.35f, 1.0f);
            G.force.setPos(slot, dn.lx + ux / cyC, dn.ly + uy / cpC);
          }
        } else {
          G.force.setPos(slot, (mp.x - origin.x - G.panX) / G.zoom,
                         (mp.y - origin.y - G.panY) / G.zoom);
        }
        if (!G.hierarchyLayout)
          G.force.reheat(0.06f); // neighbours react while dragging
      }
      ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
    } else {
      if (slot >= 0)
        G.force.setPinned(slot, false);
      G.force.reheat(G.hierarchyLayout ? 0.0f : 0.50f);
      G.dragNode = -1;
    }
  }
  if (!ImGui::IsMouseDown(ImGuiMouseButton_Left))
    G.dragCandidate = -1;

  // Single-click select (nodes) — also completes an armed link.
  if (hovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
      !G.hoverId.empty()) {
    if (!G.linkArmSrc.empty() && G.hoverId != G.linkArmSrc) {
      G.linkPopupDst = G.hoverId;
      G.linkPopupOpen = true;
    } else {
      const bool sameSel = (G.hoverId == G.selectedId);
      G.selectedId = G.hoverId;
      G.selectedEdge = -1;
      // Keep blast when re-clicking the same selected node.
      if (!sameSel)
        clearBlast(G);
    }
  }

  // Double-click drill / back (works at any nesting depth).
  if (hovered && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) {
    if (!G.hoverId.empty() && G.hoverId != G.scopeDocId) {
      auto it = G.idIndex.find(G.hoverId);
      if (it != G.idIndex.end() && isDrillableKind(G.nodes[it->second].kind))
        drillInto(G, G.hoverId);
    } else if (G.hoverId.empty() && !G.scopeDocId.empty()) {
      scopeBack(G);
    }
  }

  // Nearest-edge pick when no node is under the cursor (< 6px, screen space).
  if (hovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
      G.hoverId.empty() && G.linkArmSrc.empty()) {
    int bestEdge = -1;
    float bestEd = 6.0f * 6.0f;
    for (int ei = 0; ei < (int)G.visibleEdges.size(); ++ei) {
      const VEdge &e = G.visibleEdges[ei];
      if (G.hiddenKinds.count(e.kind))
        continue;
      auto si = G.idIndex.find(e.src), di = G.idIndex.find(e.dst);
      if (si == G.idIndex.end() || di == G.idIndex.end())
        continue;
      const GNode &a = G.nodes[si->second];
      const GNode &b = G.nodes[di->second];
      if (e.kind == "CONTAINS" && std::fabs(a.ly - b.ly) < 1.0f)
        continue;
      ImVec2 p0 = toScreen(a.lx, a.ly, a.lz);
      ImVec2 p1 = toScreen(b.lx, b.ly, b.lz);
      float vx = p1.x - p0.x, vy = p1.y - p0.y;
      float wx = mp.x - p0.x, wy = mp.y - p0.y;
      float len2 = vx * vx + vy * vy;
      float t =
          len2 > 0 ? std::clamp((wx * vx + wy * vy) / len2, 0.0f, 1.0f) : 0.0f;
      float ex = p0.x + t * vx, ey = p0.y + t * vy;
      float dx = mp.x - ex, dy = mp.y - ey;
      float d2 = dx * dx + dy * dy;
      if (d2 < bestEd) {
        bestEd = d2;
        bestEdge = ei;
      }
    }
    G.selectedEdge = bestEdge;
    if (bestEdge >= 0) {
      G.selectedId.clear();
      clearBlast(G);
    }
  }

  // ---- Nodes (visibleIdx only) --------------------------------------------
  // ---- Blast ripple rings (expanding from epicenter) ----------------------
  if (!G.blast.empty() && G.blastT0 > 0.0 && !G.blastEpicenter.empty()) {
    auto eit = G.idIndex.find(G.blastEpicenter);
    if (eit != G.idIndex.end()) {
      const GNode &en = G.nodes[eit->second];
      ImVec2 ec = toScreen(en.lx, en.ly, en.lz);
      const float t = (float)(ImGui::GetTime() - G.blastT0);
      if (t >= 0.0f && t < 1.25f) {
        const float u = std::clamp(t / 1.2f, 0.0f, 1.0f);
        // Ease-out: fast start, slow end.
        const float ease = 1.0f - (1.0f - u) * (1.0f - u);
        for (int i = 0; i < 3; ++i) {
          const float rr = ease * 200.0f * S * (float)(i + 1) / 3.0f * G.zoom;
          const float fade = (1.0f - u) * (1.0f - 0.2f * (float)i);
          ImU32 rc = ImGui::GetColorU32(
              ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                     palette.statusWarn[2], 0.55f * fade));
          dl->AddCircle(ec, rr, rc, 48, (2.0f - 0.4f * (float)i) * S);
        }
      }
    }
  }

  const bool projectScope = G.scopeDocId.empty();
  // Label LOD: with few nodes on screen, label everything; otherwise only
  // hovered/selected/search-matched nodes (text is the draw bottleneck).
  int onScreenNodes = 0;
  for (int vi : G.visibleIdx) {
    const GNode &n = G.nodes[vi];
    if (onScreen(toScreen(n.lx, n.ly, n.lz)))
      ++onScreenNodes;
  }
  const bool labelAll = onScreenNodes <= 150;
  for (int vi : G.visibleIdx) {
    const GNode &n = G.nodes[vi];
    float persp = 1.0f;
    ImVec2 c = nodeScreen(n, persp);
    if (!onScreen(c))
      continue; // viewport cull
    float r = n.radius * persp * G.zoom;
    bool dim = filtering && !G.matchIds.count(n.id);
    bool sel = (n.id == G.selectedId);
    bool hov = (n.id == G.hoverId);
    bool inBlast = G.blast.count(n.id) > 0;
    // Soft-dim non-incident nodes when something is focused; blast dims harder.
    bool nonIncident =
        incidenceDim && !sel && !hov && !inBlast && n.id != focusId;
    float a;
    if (dim)
      a = 0.18f;
    else if (!G.blast.empty() && nonIncident)
      a = 0.07f;
    else if (nonIncident)
      a = 0.45f;
    else
      a = 1.0f;
    ImU32 fill = kindColor(n.kind, 0.85f * a);
    if (inBlast && !dim) {
      // Warm tint on blast members so impact reads differently from selection.
      fill = ImGui::GetColorU32(
          ImVec4(palette.statusWarn[0] * 0.55f + palette.accent[0] * 0.15f,
                 palette.statusWarn[1] * 0.45f + 0.15f,
                 palette.statusWarn[2] * 0.20f, 0.88f * a));
    }
    ImU32 ring = Palette::toU32(palette.border);
    if (sel) {
      ring = Palette::toU32(palette.accent);
      r += 2.0f * S;
    } else if (inBlast) {
      ring = Palette::toU32(palette.statusWarn);
      r += 2.0f * S;
    } else if (hov) {
      ring = Palette::toU32(palette.accentDim);
    }
    dl->AddCircleFilled(c, r, fill, 24);
    dl->AddCircle(c, r, ring, 24, (sel || inBlast ? 2.6f : 1.4f) * S);
    // Pulsing outer halo on blast nodes (distinct from selection accent).
    if (inBlast && !dim) {
      const float pulse =
          0.55f + 0.45f * std::sin((float)ImGui::GetTime() * 4.0f);
      ImU32 halo = ImGui::GetColorU32(
          ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                 palette.statusWarn[2], 0.35f * pulse * a));
      dl->AddCircle(c, r + (4.0f + 3.0f * pulse) * S, halo, 28, 2.0f * S);
    }
    if (hov && !dim)
      dl->AddCircleFilled(
          c, r * 0.35f,
          ImGui::GetColorU32(
              ImVec4(palette.text[0], palette.text[1], palette.text[2], 0.55f)),
          12);

    // Kind glyph inside the disc once it's big enough on screen.
    if (r >= 9.0f) {
      const char *glyph = kindShort(n.kind);
      ImVec2 gs = ImGui::CalcTextSize(glyph);
      if (gs.x <= r * 1.6f) {
        dl->AddText(
            ImVec2(c.x - gs.x * 0.5f, c.y - gs.y * 0.5f),
            ImGui::GetColorU32(ImVec4(palette.panelBg[0], palette.panelBg[1],
                                      palette.panelBg[2], dim ? 0.5f : 0.95f)),
            glyph);
      }
    }

    // Labels: LOD-gated (few on screen, or zoomed, or focused/matched).
    const bool showLabel =
        !dim && ((labelAll && G.zoom >= 0.35f) || sel || hov ||
                 (filtering && G.matchIds.count(n.id)) || G.zoom >= 0.9f);
    if (showLabel) {
      const char *lab = n.label.c_str();
      ImVec2 ts = ImGui::CalcTextSize(lab);
      float maxW = 140.0f * S * std::max(0.8f, G.zoom);
      float drawW = std::min(ts.x, maxW);
      ImU32 tc = ImGui::GetColorU32(ImVec4(palette.text[0], palette.text[1],
                                           palette.text[2],
                                           sel || hov ? 1.0f : 0.90f));
      ImVec2 tp(c.x - drawW * 0.5f, c.y + r + 3 * S);

      // Subtle dark halo/backing rect for legibility over edges.
      const float padX = 3.0f * S, padY = 1.0f * S;
      ImU32 halo = ImGui::GetColorU32(ImVec4(
          palette.panelBg[0], palette.panelBg[1], palette.panelBg[2], 0.72f));
      dl->AddRectFilled(ImVec2(tp.x - padX, tp.y - padY),
                        ImVec2(tp.x + drawW + padX, tp.y + ts.y + padY), halo,
                        3.0f * S);

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
          ImU32 bc = ImGui::GetColorU32(ImVec4(palette.textDisabled[0],
                                               palette.textDisabled[1],
                                               palette.textDisabled[2], 0.95f));
          dl->AddText(ImVec2(c.x - bs.x * 0.5f, tp.y + ts.y + 1 * S), bc,
                      badge.c_str());
        }
      }
      // Instance quantity badge (decomposition view).
      if (G.scopeDecomp && (G.zoom >= 0.4f || sel || hov)) {
        auto qit = G.instCount.find(n.id);
        if (qit != G.instCount.end() && qit->second > 1) {
          char qty[24];
          std::snprintf(qty, sizeof(qty), "\u00d7%d", qit->second);
          ImVec2 bs = ImGui::CalcTextSize(qty);
          ImU32 bc = ImGui::GetColorU32(ImVec4(
              palette.accent[0], palette.accent[1], palette.accent[2], 0.95f));
          dl->AddText(ImVec2(c.x - bs.x * 0.5f, tp.y + ts.y + 1 * S), bc, qty);
        }
      }
    }
  }

  // Tooltip (optional package thumbnail for part/asm/doc).
  if (!G.hoverId.empty() && hovered) {
    const GNode *hn = nullptr;
    auto it = G.idIndex.find(G.hoverId);
    if (it != G.idIndex.end())
      hn = &G.nodes[it->second];
    if (hn) {
      ImGui::BeginTooltip();
      if ((hn->kind == "part" || hn->kind == "assembly" || hn->kind == "doc" ||
           hn->kind == "drawing") &&
          !hn->path.empty()) {
        const std::string abs = resolveNodeAbsPath(st, *hn);
        if (const GraphThumb *th = ensureGraphThumb(abs)) {
          const float maxSide = 120.0f * S;
          float tw = (float)th->w, thh = (float)th->h;
          if (tw > 1.0f && thh > 1.0f) {
            const float sc = std::min(maxSide / tw, maxSide / thh);
            tw *= sc;
            thh *= sc;
          } else {
            tw = thh = maxSide;
          }
          ImGui::Image((ImTextureID)(intptr_t)th->tex, ImVec2(tw, thh));
        }
      }
      ImGui::PushStyleColor(
          ImGuiCol_Text, ImGui::ColorConvertU32ToFloat4(kindColor(hn->kind)));
      ImGui::Text("[%s]", kindShort(hn->kind));
      ImGui::PopStyleColor();
      ImGui::SameLine();
      ImGui::TextUnformatted(hn->label.c_str());
      ImGui::PushStyleColor(ImGuiCol_Text,
                            ImVec4(palette.textDim[0], palette.textDim[1],
                                   palette.textDim[2], 1.0f));
      ImGui::Text("kind  %s", hn->kind.c_str());
      ImGui::Text("origin  %s", hn->origin.c_str());
      if (!hn->path.empty())
        ImGui::Text("path  %s", hn->path.c_str());
      if (!hn->metaSummary.empty())
        ImGui::TextWrapped("meta  %s", hn->metaSummary.c_str());
      if (projectScope) {
        auto dit = G.descCounts.find(hn->id);
        if (dit != G.descCounts.end() && dit->second.total() > 0)
          ImGui::Text("inside  %s", formatDescBadge(dit->second).c_str());
        if (isDrillableKind(hn->kind))
          ImGui::TextUnformatted("double-click to isolate references");
      }
      ImGui::PopStyleColor();
      ImGui::EndTooltip();
    }
  }

  // Empty / loading overlays.
  if (G.nodes.empty() && !G.loading) {
    const char *msg = G.err.empty() ? "No graph nodes yet" : G.err.c_str();
    const char *sub =
        G.err.empty()
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
    const char *msg = "Loading graph…";
    ImVec2 ts = ImGui::CalcTextSize(msg);
    const float r = 12.0f * S;
    const float gap = 10.0f * S;
    const float blockH = r * 2.0f + gap + ts.y;
    const ImVec2 spinC(origin.x + canvasSz.x * 0.5f,
                       origin.y + (canvasSz.y - blockH) * 0.5f + r);
    drawArcSpinner(dl, spinC, r, Palette::toU32(palette.accent));
    dl->AddText(
        ImVec2(origin.x + (canvasSz.x - ts.x) * 0.5f, spinC.y + r + gap),
        Palette::toU32(palette.accent), msg);
  }

  // Legend bottom-left of canvas.
  drawLegend(dl, ImVec2(origin.x + 12 * S, origin.y + canvasSz.y - 200 * S),
             projectScope);

  // Mode badge top-left: at-a-glance 2D/3D state; in 3D it also lists the
  // actual bindings (right-drag orbits, Shift+drag moves a node in depth).
  {
    const char *mode = G.view3d ? "3D" : "2D";
    const char *bindings =
        G.view3d
            ? "  |  left-drag orbit  |  right-drag pan  |  Shift+drag depth"
            : "";
    const ImVec2 ms = ImGui::CalcTextSize(mode);
    const ImVec2 bs = ImGui::CalcTextSize(bindings);
    const float padX = 8.0f * S, padY = 4.0f * S;
    const ImVec2 b0(origin.x + 10.0f * S, origin.y + 10.0f * S);
    const ImVec2 b1(b0.x + ms.x + bs.x + padX * 2.0f,
                    b0.y + ms.y + padY * 2.0f);
    dl->AddRectFilled(b0, b1, Palette::toU32(palette.overlayBg), 7.0f * S);
    dl->AddRect(b0, b1, Palette::toU32(palette.overlayBorder), 7.0f * S);
    const float ty = b0.y + padY;
    dl->AddText(ImVec2(b0.x + padX, ty),
                G.view3d ? Palette::toU32(palette.accent)
                         : Palette::toU32(palette.overlayText),
                mode);
    if (bindings[0])
      dl->AddText(ImVec2(b0.x + padX + ms.x, ty),
                  Palette::toU32(palette.overlayText), bindings);
  }

  dl->PopClipRect();

  // Search-driven center: when the user types a filter, center on first VISIBLE
  // match.
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
void drawListView(AppState & /*st*/, ImVec2 canvasSz) {
  auto &G = S();
  const float Sc = ui::uiScale;

  ImGui::BeginChild("##graph_list", canvasSz, ImGuiChildFlags_None);

  if (G.nodes.empty() && !G.loading) {
    ImGui::Dummy(ImVec2(0, 24 * Sc));
    const char *msg = G.err.empty() ? "No graph nodes yet" : G.err.c_str();
    ImGui::TextColored(ImVec4(palette.textDim[0], palette.textDim[1],
                              palette.textDim[2], 1.0f),
                       "%s", msg);
    ImGui::EndChild();
    return;
  }
  if (G.loading && G.nodes.empty()) {
    ImGui::Dummy(ImVec2(0, 24 * Sc));
    // Widget spinner (plmSpinner) — same accent-dot language as the rest of
    // PLM. Continuous redraw while loading is covered by st.rpc.busy() in the
    // frame loop.
    plmSpinner("Loading graph");
    ImGui::EndChild();
    return;
  }

  // Stable order: kind order, then label, then id.
  std::vector<int> order(G.nodes.size());
  for (int i = 0; i < (int)order.size(); ++i)
    order[i] = i;
  std::sort(order.begin(), order.end(), [&](int a, int b) {
    int ra = kindOrder(G.nodes[a].kind), rb = kindOrder(G.nodes[b].kind);
    if (ra != rb)
      return ra < rb;
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
    if (filtering && !matchesSearch(G.nodes[i], G.search))
      continue;
    visible.push_back(i);
  }
  clipper.Begin((int)visible.size());
  while (clipper.Step()) {
    for (int row = clipper.DisplayStart; row < clipper.DisplayEnd; ++row) {
      const GNode &n = G.nodes[visible[row]];
      const bool sel = (n.id == G.selectedId);
      const bool inBlast = G.blast.count(n.id) > 0;

      ImGui::PushID(n.id.c_str());
      // Kind swatch
      ImVec2 p = ImGui::GetCursorScreenPos();
      float r = 5.0f * Sc;
      ImGui::GetWindowDrawList()->AddCircleFilled(
          ImVec2(p.x + r + 2 * Sc, p.y + ImGui::GetTextLineHeight() * 0.5f), r,
          kindColor(n.kind), 12);
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
                              ImVec4(palette.statusWarn[0],
                                     palette.statusWarn[1],
                                     palette.statusWarn[2], 0.30f));
      }
      if (ImGui::Selectable(n.label.c_str(), sel || inBlast,
                            ImGuiSelectableFlags_SpanAllColumns)) {
        const bool sameSel = (n.id == G.selectedId);
        G.selectedId = n.id;
        G.selectedEdge = -1;
        if (!sameSel)
          clearBlast(G);
      }
      if (ImGui::IsItemHovered() &&
          ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left) &&
          isDrillableKind(n.kind) && n.id != G.scopeDocId) {
        drillInto(G, n.id);
      }
      if (sel || inBlast)
        ImGui::PopStyleColor();

      if (!n.path.empty()) {
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text,
                              ImVec4(palette.textDisabled[0],
                                     palette.textDisabled[1],
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

// Elide a label to fit maxW with a trailing "..." (visible cells only, so the
// incremental trim is bounded and cheap).
std::string elideToWidth(const std::string &s, float maxW) {
  if (ImGui::CalcTextSize(s.c_str()).x <= maxW)
    return s;
  const float ellW = ImGui::CalcTextSize("...").x;
  std::string out = s;
  while (!out.empty() && ImGui::CalcTextSize(out.c_str()).x + ellW > maxW)
    out.pop_back();
  return out + "...";
}

// Grid view — structural nodes (assembly/part/doc) of the current scope's
// visible set as responsive square cells: iso-preview tile + kind chip +
// elided label. Click selects (inspector), double-click drills (mirrors the
// list rows). Missing previews paint a kind-icon placeholder immediately and
// render on demand via the .plm/thumbs disk cache (graph_thumbs).
void drawGridView(AppState &st, ImVec2 canvasSz) {
  auto &G = S();
  const float Sc = ui::uiScale;

  ImGui::BeginChild("##graph_grid", canvasSz, ImGuiChildFlags_None);

  if (G.nodes.empty() && !G.loading) {
    ImGui::Dummy(ImVec2(0, 24 * Sc));
    const char *msg = G.err.empty() ? "No graph nodes yet" : G.err.c_str();
    ImGui::TextColored(ImVec4(palette.textDim[0], palette.textDim[1],
                              palette.textDim[2], 1.0f),
                       "%s", msg);
    ImGui::EndChild();
    return;
  }
  if (G.loading && G.nodes.empty()) {
    ImGui::Dummy(ImVec2(0, 24 * Sc));
    plmSpinner("Loading graph");
    ImGui::EndChild();
    return;
  }

  // Structural nodes of the current scope, search-filtered (same row
  // filtering the list view applies). In a drill scope the visible set IS
  // the scope's child set; in project view the grid lists every structural
  // doc node — the canvas's "Hide unconnected" cleanup drops loose docs
  // because scattered dots have no context, but grid cells (like list rows)
  // carry their own context.
  const bool filtering = G.search[0] != 0;
  auto gridKind = [](const std::string &k) {
    return k == "assembly" || k == "part" || k == "doc";
  };
  std::vector<int> items;
  if (G.scopeDocId.empty()) {
    items.reserve(G.nodes.size());
    for (int i = 0; i < (int)G.nodes.size(); ++i) {
      const GNode &n = G.nodes[i];
      if (!gridKind(n.kind))
        continue;
      if (filtering && !matchesSearch(n, G.search))
        continue;
      items.push_back(i);
    }
  } else {
    items.reserve(G.visibleIdx.size());
    for (int vi : G.visibleIdx) {
      const GNode &n = G.nodes[vi];
      if (!gridKind(n.kind))
        continue;
      if (filtering && !matchesSearch(n, G.search))
        continue;
      items.push_back(vi);
    }
  }
  std::sort(items.begin(), items.end(), [&](int a, int b) {
    int ra = kindOrder(G.nodes[a].kind), rb = kindOrder(G.nodes[b].kind);
    if (ra != rb)
      return ra < rb;
    if (G.nodes[a].label != G.nodes[b].label)
      return G.nodes[a].label < G.nodes[b].label;
    return G.nodes[a].id < G.nodes[b].id;
  });

  if (items.empty()) {
    ImGui::Dummy(ImVec2(0, 24 * Sc));
    ImGui::TextColored(ImVec4(palette.textDim[0], palette.textDim[1],
                              palette.textDim[2], 1.0f),
                       "%s",
                       filtering ? "No parts or assemblies match the search"
                                 : "No parts or assemblies in this view");
    ImGui::EndChild();
    return;
  }

  // Responsive columns: target ~168px cells, then stretch to fill the row so
  // the grid tracks the window width with no dead gutter.
  const float availW = ImGui::GetContentRegionAvail().x;
  const float gap = 10.0f * Sc;
  const float ideal = 168.0f * Sc;
  const int cols = std::max(1, (int)((availW + gap) / (ideal + gap)));
  const float cellW =
      std::min((availW - gap * (float)(cols - 1)) / (float)cols, 240.0f * Sc);
  const float pad = 6.0f * Sc;
  const float tile = cellW - 2.0f * pad; // square preview box
  const float lineH = ImGui::GetTextLineHeight();
  const float cellH =
      pad + tile + 4.0f * Sc + lineH + 2.0f * Sc + lineH + 6.0f * Sc;
  const int rows = ((int)items.size() + cols - 1) / cols;

  ImDrawList *dl = ImGui::GetWindowDrawList();
  ImGuiListClipper clipper;
  clipper.Begin(rows, cellH + gap);
  while (clipper.Step()) {
    for (int row = clipper.DisplayStart; row < clipper.DisplayEnd; ++row) {
      for (int col = 0; col < cols; ++col) {
        const int idx = row * cols + col;
        if (idx >= (int)items.size())
          break;
        const GNode &n = G.nodes[items[idx]];
        const bool sel = (n.id == G.selectedId);
        const bool inBlast = G.blast.count(n.id) > 0;

        // The clipper positions the row cursor; columns are placed
        // explicitly from the row origin so they line up.
        const ImVec2 rowPos = ImGui::GetCursorScreenPos();
        const ImVec2 cellPos(rowPos.x + (float)col * (cellW + gap), rowPos.y);
        ImGui::SetCursorScreenPos(cellPos);

        ImGui::PushID(n.id.c_str());
        ImGui::InvisibleButton("##cell", ImVec2(cellW, cellH));
        const bool hov = ImGui::IsItemHovered();
        if (ImGui::IsItemClicked(ImGuiMouseButton_Left)) {
          const bool sameSel = (n.id == G.selectedId);
          G.selectedId = n.id;
          G.selectedEdge = -1;
          if (!sameSel)
            clearBlast(G);
        }
        if (hov && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left) &&
            isDrillableKind(n.kind) && n.id != G.scopeDocId) {
          drillInto(G, n.id);
        }

        const ImVec2 p1(cellPos.x + cellW, cellPos.y + cellH);
        // Cell state fills (mirrors the list-row selection accents).
        if (sel) {
          dl->AddRectFilled(
              cellPos, p1,
              ImGui::GetColorU32(ImVec4(palette.accent[0], palette.accent[1],
                                        palette.accent[2], 0.30f)),
              9.0f * Sc);
          dl->AddRect(
              cellPos, p1,
              ImGui::GetColorU32(ImVec4(palette.accent[0], palette.accent[1],
                                        palette.accent[2], 0.85f)),
              9.0f * Sc, 0, 1.5f * Sc);
        } else if (inBlast) {
          dl->AddRectFilled(cellPos, p1,
                            ImGui::GetColorU32(ImVec4(
                                palette.statusWarn[0], palette.statusWarn[1],
                                palette.statusWarn[2], 0.25f)),
                            9.0f * Sc);
        } else if (hov) {
          dl->AddRectFilled(
              cellPos, p1,
              ImGui::GetColorU32(ImVec4(palette.text[0], palette.text[1],
                                        palette.text[2], 0.06f)),
              9.0f * Sc);
        }

        // ---- square preview tile ----------------------------------------
        const ImVec2 t0(cellPos.x + pad, cellPos.y + pad);
        const ImVec2 t1(t0.x + tile, t0.y + tile);
        ui::glassBackground(dl, t0, t1, 8.0f);

        const std::string absPath = resolveNodeAbsPath(st, n);
        GraphGridThumb th;
        if (!absPath.empty()) {
          const std::string key = graphThumbKey(n.metaDh, n.metaMt, absPath);
          th = graphThumbEnsure(st, G.projectDir, key, absPath);
        }
        if (th.tex) {
          const float sc = std::min(tile / std::max((float)th.w, 1.0f),
                                    tile / std::max((float)th.h, 1.0f));
          const float tw = (float)th.w * sc, thh = (float)th.h * sc;
          const ImVec2 i0(t0.x + (tile - tw) * 0.5f,
                          t0.y + (tile - thh) * 0.5f);
          dl->AddImage((ImTextureID)(intptr_t)th.tex, i0,
                       ImVec2(i0.x + tw, i0.y + thh));
        } else {
          // Placeholder: kind glyph disc (+ spinner while rendering).
          const char *icon = n.kind == "assembly" ? "assembly"
                             : n.kind == "part"   ? "part"
                                                  : "new_doc";
          const ImVec2 ic(t0.x + tile * 0.5f, t0.y + tile * 0.44f);
          dl->AddCircleFilled(ic, tile * 0.22f, kindColor(n.kind, 0.22f), 28);
          ui::drawIconGlass(dl, icon, ic, 26.0f * Sc, kindColor(n.kind, 0.9f),
                            0);
          if (th.pending) {
            drawArcSpinner(dl, ImVec2(t0.x + tile * 0.5f, t0.y + tile * 0.78f),
                           7.0f * Sc, kindColor(n.kind, 0.9f));
          }
        }

        // ---- kind chip + elided label -----------------------------------
        const float textX = cellPos.x + pad;
        const float textW = cellW - 2.0f * pad;
        float ty = t1.y + 4.0f * Sc;
        dl->AddText(ImVec2(textX, ty), kindColor(n.kind), kindShort(n.kind));
        ty += lineH + 2.0f * Sc;
        const std::string lab = elideToWidth(n.label, textW);
        dl->AddText(ImVec2(textX, ty), Palette::toU32(palette.text),
                    lab.c_str());

        if (hov) {
          ImGui::BeginTooltip();
          ImGui::TextUnformatted(n.label.c_str());
          if (!n.path.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                                  ImVec4(palette.textDisabled[0],
                                         palette.textDisabled[1],
                                         palette.textDisabled[2], 1.0f));
            ImGui::TextUnformatted(n.path.c_str());
            ImGui::PopStyleColor();
          }
          if (th.pending) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                                  ImVec4(palette.textDim[0], palette.textDim[1],
                                         palette.textDim[2], 1.0f));
            ImGui::TextUnformatted("Preview rendering...");
            ImGui::PopStyleColor();
          }
          ImGui::EndTooltip();
        }

        ImGui::PopID();
        // Restore the row origin for the next column (the InvisibleButton
        // moved the cursor below the cell).
        ImGui::SetCursorScreenPos(rowPos);
      }
      // Advance past this row for the clipper.
      ImGui::SetCursorScreenPos(
          ImVec2(ImGui::GetCursorScreenPos().x,
                 ImGui::GetCursorScreenPos().y + cellH + gap));
    }
  }
  clipper.End();

  ImGui::EndChild();
}

} // namespace

// ---- public
// ------------------------------------------------------------------

bool plmGraphAvailable() { return S().available; }

// True once a cad_graph_get response has landed (headless shot sync).
bool plmGraphLoaded() { return !S().loading && !S().nodes.empty(); }

void plmGraphOnProjectChanged() {
  auto &G = S();
  maybeSaveLayout(G, /*force=*/true); // final solve-state save for old project
  freeAllGraphThumbs();
  G.nodes.clear();
  G.edges.clear();
  clearBlast(G);
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
  G.gridView = false;
  graphThumbsReset();
  G.asmView = false;
  G.projPosStash.clear();
  G.asmPosStash.clear();
  G.preWorldStash.clear();
  // Scope / visible caches — never open a new project inside a stale drill.
  G.scopeDocId.clear();
  G.scopeStack.clear();
  G.scopeDecomp = false;
  G.instCount.clear();
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
  G.scopePending.clear();
  G.focusPending.clear();
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

void plmGraphFocus(const std::string &query) {
  auto &G = S();
  std::snprintf(G.search, sizeof(G.search), "%s", query.c_str());
  G.focusPending = query;
  G.centerPending = !query.empty();
  G.matchDirty = true;
  G.lastFetch = -1e9; // force a refetch on the next draw
}

void plmGraphScopeNode(const std::string &idOrQuery) {
  auto &G = S();
  G.scopePending = idOrQuery;
  G.lastFetch = -1e9; // ensure graph is warm before we resolve
}

void plmDrawGraphPage(AppState &st) {
  auto &G = S();
  const float S = ui::uiScale;

  if (!G.available) {
    // Should not be reached (rail hides us); belt-and-suspenders.
    plmEmptyState("[ ]", "Graph unavailable",
                  "This Chudware build does not expose the CAD graph service.");
    return;
  }

  // Shot-only hook (AI_CAD_PLM_GRAPH_VIEW=grid): force Grid view so headless
  // --ui-screenshot runs can capture it (mirrors AI_CAD_FM_COLLAPSED). No-op
  // in normal use.
  {
    static const bool shotGrid = [] {
      const char *v = std::getenv("AI_CAD_PLM_GRAPH_VIEW");
      return v && std::strcmp(v, "grid") == 0;
    }();
    if (shotGrid && !G.listViewUserOverride) {
      G.gridView = true;
      G.listView = false;
      G.listViewUserOverride = true;
    }
  }

  // Project change → drop caches.
  const std::string proj = st.plmProjectPath();
  if (proj != G.projectDir && !G.loading) {
    // Only wipe when we have a previous project; first paint just fetches.
    if (!G.projectDir.empty())
      plmGraphOnProjectChanged();
    G.projectDir = proj;
  }

  // Auto-refresh when the OS app regains focus. Use force=false so a warm
  // ME489-scale cache can serve without a full rebuild (mtime fast-path).
  const bool lost = ImGui::GetIO().AppFocusLost;
  if (G.wasAppFocusLost && !lost)
    requestGraph(st, /*force=*/false);
  G.wasAppFocusLost = lost;

  requestGraph(st, /*force=*/false);

  // Grid thumbnails: drive at most one queued iso render per frame (no-op
  // when idle). Placeholders paint immediately; tiles swap in as renders
  // land in .plm/thumbs.
  graphThumbsPump(st);

  // Apply deferred scope drill (plmGraphScopeNode) once nodes are present.
  if (!G.scopePending.empty() && !G.nodes.empty()) {
    const std::string q = G.scopePending;
    G.scopePending.clear();
    auto ends_with = [](const std::string &s, const std::string &suf) {
      return s.size() >= suf.size() &&
             s.compare(s.size() - suf.size(), suf.size(), suf) == 0;
    };
    int best = -1;
    for (int i = 0; i < (int)G.nodes.size(); ++i) {
      const GNode &n = G.nodes[i];
      if (n.id == q) {
        best = i;
        break;
      }
      if (!n.path.empty() && (n.path == q || ends_with(n.path, q))) {
        best = i; // keep first path suffix match; exact id wins above
        if (ends_with(n.path, "/" + q) || ends_with(n.path, "\\" + q))
          break;
      }
      if (!n.label.empty() && (n.label == q || ends_with(n.label, q))) {
        if (best < 0)
          best = i;
      }
    }
    if (best >= 0 && isDrillableKind(G.nodes[best].kind)) {
      G.scopeStack.clear();
      setScope(G, G.nodes[best].id);
      G.selectedId = G.nodes[best].id;
      G.centerPending = true;
      G.fitRequested = true;
    } else if (best >= 0) {
      G.selectedId = G.nodes[best].id;
      G.centerPending = true;
    }
  }

  // ---- toolbar -------------------------------------------------------------
  ImGui::SetNextItemWidth(220 * S);
  if (ImGui::InputTextWithHint("##graph_search", "Search nodes…", G.search,
                               sizeof(G.search))) {
    G.centerPending = G.search[0] != 0;
    G.matchDirty = true;
    G.force.reheat(G.hierarchyLayout
                       ? 0.0f
                       : 0.12f); // matched nodes draw the eye as they settle
  }
  ImGui::SameLine();
  if (graphGlassButton("##graph_refresh", G.loading ? "Refreshing…" : "Refresh",
                       false, 110 * S) &&
      !G.loading) {
    requestGraph(st, /*force=*/true);
  }
  if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal))
    ImGui::SetTooltip("Re-fetch the project graph from the backend.");
  ImGui::SameLine();
  if (graphGlassButton("##graph_fit", "Fit", false, 70 * S)) {
    G.fitRequested = true; // the ONLY auto-fit path left
    G.userNavigated = false;
  }
  if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal))
    ImGui::SetTooltip("Frame all visible nodes in the canvas.");
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
      ImGui::SetTooltip("Exclude _archive_stale / _archive / archive / .trash\n"
                        "directories from the graph (default ON).");
    }
  }
  ImGui::SameLine();
  // Hide unconnected — ON by default; project view only, no refetch needed.
  {
    bool hu = G.hideUnconnected;
    if (ImGui::Checkbox("Hide unconnected", &hu) && hu != G.hideUnconnected) {
      G.hideUnconnected = hu;
      rebuildVisible(G);
    }
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) {
      ImGui::SetTooltip("Hide nodes with no visible links (default ON).\n"
                        "They remain available in List view.");
    }
  }
  ImGui::SameLine();
  // View mode chips: Graph / List / Grid. The active chip carries the accent
  // fill (same active-chip style as the 2D|3D segment).
  {
    const bool graphActive = !G.listView && !G.gridView;
    if (graphGlassButton("##view_graph", "Graph view", graphActive, 0.0f)) {
      if (G.listView || G.gridView) {
        G.listView = false;
        G.gridView = false;
        G.layoutDirty = true;
      }
      G.listViewUserOverride = true;
    }
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) {
      ImGui::SetTooltip("Graph view animates the current scope as a live\n"
                        "force-directed layout (drag nodes to rearrange).");
    }
    ImGui::SameLine(0, 4 * S);
    if (graphGlassButton("##view_list", "List view", G.listView, 0.0f)) {
      G.listView = true;
      G.gridView = false;
      G.listViewUserOverride = true;
    }
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) {
      ImGui::SetTooltip("List view shows every loaded node (including\n"
                        "features) as filterable rows.");
    }
    ImGui::SameLine(0, 4 * S);
    if (graphGlassButton("##view_grid", "Grid view", G.gridView, 0.0f)) {
      G.gridView = true;
      G.listView = false;
      G.listViewUserOverride = true;
    }
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) {
      ImGui::SetTooltip("Grid view shows the parts and assemblies of the\n"
                        "current scope as square cells with iso previews.\n"
                        "Previews render on demand and cache on disk.");
    }
  }
  ImGui::SameLine();
  // Dimension mode: segmented 2D|3D switch — the accent segment is the
  // active mode (no glyph badges; labels/icons only).
  {
    const int seg = graphModeSegment("##graph_dim", G.view3d);
    if (seg >= 0 && (seg == 1) != G.view3d) {
      G.view3d = !G.view3d;
      // World-position overlay: nodes whose doc carries a persisted
      // assembly transform (meta.wp) take their REAL model coordinates in
      // 3D — scaled + centered onto the current layout frame — so the 3D
      // view mirrors the actual assembly. 2D positions stash + restore.
      if (G.view3d) {
        float wmin[3] = {1e30f, 1e30f, 1e30f},
              wmax[3] = {-1e30f, -1e30f, -1e30f};
        int wn = 0;
        for (const auto &n : G.nodes) {
          if (!n.hasW)
            continue;
          wmin[0] = std::min(wmin[0], n.wx);
          wmax[0] = std::max(wmax[0], n.wx);
          wmin[1] = std::min(wmin[1], n.wy);
          wmax[1] = std::max(wmax[1], n.wy);
          wmin[2] = std::min(wmin[2], n.wz);
          wmax[2] = std::max(wmax[2], n.wz);
          ++wn;
        }
        if (wn > 0) {
          float lx0 = 1e30f, ly0 = 1e30f, lx1 = -1e30f, ly1 = -1e30f;
          for (const auto &n : G.nodes) {
            if (!hasPosition(n))
              continue;
            lx0 = std::min(lx0, n.lx);
            lx1 = std::max(lx1, n.lx);
            ly0 = std::min(ly0, n.ly);
            ly1 = std::max(ly1, n.ly);
          }
          const float wExt = std::max(
              {wmax[0] - wmin[0], wmax[1] - wmin[1], wmax[2] - wmin[2]});
          const float lExt = std::max(lx1 - lx0, ly1 - ly0);
          const float k = (wExt > 1e-6f && lExt > 1e-6f) ? lExt / wExt : 1.0f;
          const float wcx = (wmin[0] + wmax[0]) * 0.5f,
                      wcy = (wmin[1] + wmax[1]) * 0.5f,
                      wcz = (wmin[2] + wmax[2]) * 0.5f;
          const float lcx = (lx0 + lx1) * 0.5f, lcy = (ly0 + ly1) * 0.5f;
          for (auto &n : G.nodes) {
            if (!n.hasW)
              continue;
            G.preWorldStash[n.id] = {n.lx, n.ly, n.lz};
            n.lx = (n.wx - wcx) * k + lcx;
            n.ly = (n.wy - wcy) * k + lcy;
            n.lz = (n.wz - wcz) * k;
          }
        }
      } else {
        for (const auto &kv : G.preWorldStash) {
          auto it = G.idIndex.find(kv.first);
          if (it == G.idIndex.end())
            continue;
          G.nodes[it->second].lx = kv.second.x;
          G.nodes[it->second].ly = kv.second.y;
          G.nodes[it->second].lz = kv.second.z;
        }
        G.preWorldStash.clear();
      }
      G.layoutDirty = true; // rebuild the sim in the new dimensionality
      // Refit so the projected bounds frame correctly on entry.
      G.fitRequested = true;
      G.userNavigated = false;
    }
  }
  if (!G.generatedAt.empty()) {
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.textDisabled[0],
                                                palette.textDisabled[1],
                                                palette.textDisabled[2], 1.0f));
    ImGui::AlignTextToFramePadding();
    ImGui::Text("generated %s", G.generatedAt.c_str());
    ImGui::PopStyleColor();
  }
  if (!G.err.empty() && !G.nodes.empty()) {
    ImGui::SameLine();
    plmError(G.err);
  }

  // ---- breadcrumb: Project › asm › subasm › … ----------------------------
  {
    ImGui::AlignTextToFramePadding();
    ImGui::PushStyleColor(ImGuiCol_Text,
                          G.scopeDocId.empty()
                              ? ImVec4(palette.accent[0], palette.accent[1],
                                       palette.accent[2], 1.0f)
                              : ImVec4(palette.textDim[0], palette.textDim[1],
                                       palette.textDim[2], 1.0f));
    if (ImGui::SmallButton("▣ Project##bc_root")) {
      if (!G.scopeDocId.empty()) {
        G.scopeStack.clear();
        setScope(G, "");
      }
    }
    ImGui::PopStyleColor();
    // Ancestor chain + current scope; all but the current are clickable
    // and pop the stack back to that level.
    std::vector<std::string> crumbs;
    for (const auto &a : G.scopeStack)
      if (!a.empty())
        crumbs.push_back(a);
    if (!G.scopeDocId.empty())
      crumbs.push_back(G.scopeDocId);
    for (int ci = 0; ci < (int)crumbs.size(); ++ci) {
      const bool current = (ci == (int)crumbs.size() - 1);
      ImGui::SameLine(0, 4 * S);
      ImGui::PushStyleColor(ImGuiCol_Text,
                            ImVec4(palette.textDisabled[0],
                                   palette.textDisabled[1],
                                   palette.textDisabled[2], 1.0f));
      ImGui::TextUnformatted("›");
      ImGui::PopStyleColor();
      ImGui::SameLine(0, 4 * S);
      std::string lab = crumbs[ci];
      auto it = G.idIndex.find(crumbs[ci]);
      if (it != G.idIndex.end())
        lab = G.nodes[it->second].label;
      ImGui::PushStyleColor(ImGuiCol_Text,
                            current
                                ? ImVec4(palette.accent[0], palette.accent[1],
                                         palette.accent[2], 1.0f)
                                : ImVec4(palette.textDim[0], palette.textDim[1],
                                         palette.textDim[2], 1.0f));
      ImGui::PushID(ci);
      if (!current && ImGui::SmallButton(lab.c_str())) {
        // Jump straight to this crumb: scope = crumb, ancestors =
        // project entry + crumbs before it.
        std::vector<std::string> newStack;
        newStack.push_back(std::string());
        for (int k = 0; k < ci; ++k)
          newStack.push_back(crumbs[k]);
        G.scopeStack = std::move(newStack);
        setScope(G, crumbs[ci]);
      } else if (current) {
        ImGui::TextUnformatted(lab.c_str());
      }
      ImGui::PopID();
      ImGui::PopStyleColor();
    }
    if (!G.scopeDocId.empty()) {
      auto it = G.idIndex.find(G.scopeDocId);
      // Decomposable scopes can flip between the parts tree and the
      // raw doc internals (features/sketches/mates).
      if (hasBomChildren(G, G.scopeDocId) ||
          (it != G.idIndex.end() && G.nodes[it->second].kind == "assembly")) {
        ImGui::SameLine(0, 12 * S);
        // Active chip reads by accent text (the app font lacks dot glyphs).
        if (G.scopeDecomp)
          ImGui::PushStyleColor(ImGuiCol_Text,
                                ImVec4(palette.accent[0], palette.accent[1],
                                       palette.accent[2], 1.0f));
        if (ImGui::SmallButton("Parts##sc_parts")) {
          if (!G.scopeDecomp) {
            G.scopeDecomp = true;
            rebuildVisible(G);
          }
        }
        if (G.scopeDecomp)
          ImGui::PopStyleColor();
        ImGui::SameLine(0, 2 * S);
        if (!G.scopeDecomp)
          ImGui::PushStyleColor(ImGuiCol_Text,
                                ImVec4(palette.accent[0], palette.accent[1],
                                       palette.accent[2], 1.0f));
        if (ImGui::SmallButton("Internals##sc_int")) {
          if (G.scopeDecomp) {
            G.scopeDecomp = false;
            rebuildVisible(G);
          }
        }
        if (!G.scopeDecomp)
          ImGui::PopStyleColor();
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal))
          ImGui::SetTooltip(
              "Parts: subassembly/part decomposition with instance counts.\n"
              "Internals: raw features / sketches / mates of this document.");
      }
    }
  }

  // Honest counts: visible vs loaded vs full.
  if (!G.nodes.empty()) {
    const int visN = (int)G.visibleIdx.size();
    const int loadN = (int)G.nodes.size();
    const int fullN = G.nodeCountFull > 0 ? G.nodeCountFull : loadN;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.textDisabled[0],
                                                palette.textDisabled[1],
                                                palette.textDisabled[2], 1.0f));
    if (G.scopeDocId.empty()) {
      if (visN < fullN)
        ImGui::Text(
            "%d of %d nodes visible · double-click to isolate references", visN,
            fullN);
      else
        ImGui::Text("%d nodes · %d edges", visN, (int)G.visibleEdges.size());
    } else {
      ImGui::Text("%d nodes in scope · %d of %d loaded", visN, loadN, fullN);
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
    if (G.matchDirty)
      rebuildMatchCache(G);
    const int hiddenMatches = G.allMatches - G.visMatches;
    if (hiddenMatches > 0) {
      ImGui::PushStyleColor(ImGuiCol_Text,
                            ImVec4(palette.textDisabled[0],
                                   palette.textDisabled[1],
                                   palette.textDisabled[2], 1.0f));
      ImGui::Text(
          "+%d matches inside documents — switch to List view or drill in",
          hiddenMatches);
      ImGui::PopStyleColor();
    }
  }

  // ---- edge-kind filter row -----------------------------------------------
  if (graphGlassButton("##links_only", "Links only", false, 0.0f)) {
    G.hiddenKinds.clear();
    G.hiddenKinds.insert("CONTAINS"); // hide containment → the link web
    G.layoutDirty = true;             // hidden kinds exert no spring force
  }
  ImGui::SameLine();
  if (graphGlassButton("##all_kinds", "All", false, 0.0f)) {
    G.hiddenKinds.clear();
    G.layoutDirty = true;
  }
  ImGui::SameLine();
  // Assembly structure view: containment/instance tree + mates, layered.
  // Accent fill while active (same active-chip style as the 2D|3D segment).
  if (graphGlassButton("##asm_view", "Assembly", G.asmView, 0.0f)) {
    toggleAsmView(G);
  }
  if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) {
    ImGui::SetTooltip(
        "Assembly structure: assemblies + parts with containment, instance\n"
        "and mate relations, laid out as a deterministic layered tree.\n"
        "Arrangement is kept per-session (the layered layout is re-derived\n"
        "deterministically on entry).");
  }
  for (const char *k : kEdgeKinds) {
    ImGui::SameLine();
    bool vis = !G.hiddenKinds.count(k);
    ImU32 tc = vis ? edgeColor(k, 1.0f) : Palette::toU32(palette.textDisabled);
    ImGui::PushStyleColor(ImGuiCol_Text, ImGui::ColorConvertU32ToFloat4(tc));
    if (ImGui::SmallButton(k)) {
      if (vis)
        G.hiddenKinds.insert(k);
      else
        G.hiddenKinds.erase(k);
      G.layoutDirty = true;
    }
    ImGui::PopStyleColor();
  }

  ImGui::Dummy(ImVec2(0, 4 * S));

  // ---- body: canvas/list + side panel -------------------------------------
  const float gap = 10.0f * S;
  const float sideW = 260.0f * S;
  const float bodyH = ImGui::GetContentRegionAvail().y;
  const float canvasW =
      std::max(120.0f * S, ImGui::GetContentRegionAvail().x - sideW - gap);

  ImGui::BeginChild("##graph_body", ImVec2(0, bodyH), ImGuiChildFlags_None,
                    ImGuiWindowFlags_NoScrollbar |
                        ImGuiWindowFlags_NoScrollWithMouse);

  ImGui::BeginChild(
      "##graph_canvas_host", ImVec2(canvasW, 0), ImGuiChildFlags_None,
      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
  ImVec2 csz = ImGui::GetContentRegionAvail();
  if (G.gridView)
    drawGridView(st, csz);
  else if (G.listView)
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
    G.linkPopupOpen = false; // consume: the popup now owns its lifetime
  }
  if (ImGui::BeginPopup("Add link##graph_link")) {
    ImGui::TextUnformatted("New link");
    ImGui::SetNextItemWidth(180 * S);
    ImGui::Combo("kind##lk", &G.linkKindIdx, kEdgeKinds,
                 IM_ARRAYSIZE(kEdgeKinds));
    ImGui::SetNextItemWidth(220 * S);
    ImGui::InputTextWithHint("##lknote", "note (optional)", G.linkNote,
                             sizeof(G.linkNote));
    if (ImGui::Button("Create")) {
      int ki = std::clamp(G.linkKindIdx, 0, (int)IM_ARRAYSIZE(kEdgeKinds) - 1);
      requestLinkAdd(st, G.linkArmSrc, G.linkPopupDst, kEdgeKinds[ki],
                     G.linkNote);
      G.linkArmSrc.clear();
      G.linkPopupDst.clear();
      G.linkNote[0] = 0;
      ImGui::CloseCurrentPopup();
    }
    ImGui::SameLine();
    if (ImGui::Button("Cancel")) {
      G.linkArmSrc.clear();
      G.linkPopupDst.clear();
      ImGui::CloseCurrentPopup();
    }
    ImGui::EndPopup();
  }
  // Esc clears blast, else cancels an armed link when no popup is open.
  if (!ImGui::IsPopupOpen("Add link##graph_link") &&
      ImGui::IsKeyPressed(ImGuiKey_Escape)) {
    if (!G.blast.empty())
      clearBlast(G);
    else if (!G.linkArmSrc.empty())
      G.linkArmSrc.clear();
  }
}

} // namespace ui
