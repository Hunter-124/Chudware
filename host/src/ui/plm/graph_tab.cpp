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
#include "graph_thumbnail.h"
#include "../file_dialog.h"
#include "graph_changes.h" // cad_graph_changes / watch overlay (the "what changed" rail)
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
#include <set>
#include <string>
#include <tuple>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h" // ShadeVertsLinearColorGradientKeepAlpha (edge gradients)

#include "core/app_state.h"
#include "core/async_rpc.h"
#include "gl_compat.h"
#include "ui/anim.h"
#include "ui/icons.h"
#include "ui/metrics.h"
#include "ui/tesseract.h"
#include "ui/widgets/kit.h"
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
  std::string metaUn;         // unit system ("ips"/"mmgs") from doc meta.un
  std::vector<std::string> metaMat; // material names from doc meta.mat
  std::vector<std::string> metaKeywords; // user tags from meta.keywords
  std::string metaPreview;    // file text_preview (<=300 chars, file nodes)
  std::string metaProject;    // owning project root (cross-project merges)
  std::string metaFolder;     // folder bucket for file nodes (By-folder layout)
  // Vault identity stamped by cad_graph._merge_items (meta.item_id /
  // part_number / lifecycle). lifecycle is the actionable one: it drives the
  // node rim, so an assembly of Released parts with one In-work child reads
  // without a click.
  std::string metaItemId;
  std::string metaPartNumber;
  std::string metaLifecycle; // "In work" / "Released" / "Superseded" / ...
  // Features and mates carry meta.suppressed. A suppressed feature that still
  // has downstream DEPENDS_ON edges is exactly what makes a rebuild behave
  // inexplicably, so it gets a hollow disc and dashed outgoing edges.
  bool metaSuppressed = false;
  // Equation nodes: expr / value / unit (meta.expr, meta.value, meta.unit).
  std::string metaExpr;
  std::string metaEqValue; // pre-formatted; the payload value may be any JSON
  std::string metaEqUnit;
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
  // REFERENCES edges with meta.via == "external_ref" carry meta.stale. Promoted
  // out of the json so the draw path branches on a bool, not a per-frame parse.
  bool stale = false;
};

// Drawn edge after scope filtering / project-view aggregation.
struct VEdge {
  std::string src, dst, kind, origin;
  std::string dimLabel;
  int aggCount = 1;
  int srcEdgeIdx = -1; // index into GraphTabState::edges
  bool stale = false;
};

// Per structural node: hidden descendant tallies (features / sketches / other).
struct DescCounts {
  int features = 0;
  int sketches = 0;
  int other = 0;
  int total() const { return features + sketches + other; }
};

// One label the canvas would like to paint this frame. Collected before the
// node paint so the whole set can be ranked and collision-culled: labels used
// to be drawn unconditionally, and any tight cluster overprinted into mush.
struct LabelCand {
  int vi = -1;      // nodes[] index
  ImVec2 c{};       // disc centre, screen space
  float r = 0.0f;   // drawn disc radius (includes the selection/blast bump)
  float persp = 1.0f;
  int deg = 0;      // whole-graph out+in degree (primary priority)
  bool forced = false; // selection / hover / search match — never dropped
};

struct GraphTabState {
  bool available = true; // false after permanent unknown_method
  bool loading = false;
  bool fetchInFlight = false;
  unsigned fetchSerial = 0;
  bool blastLoading = false;
  bool layoutDirty = true;
  bool centerPending = false;
  // Focus Document: id of a node to select + center on the next canvas paint.
  std::string pendingCenterId;
  std::string projectDir;
  std::string err;
  std::string generatedAt;
  std::vector<GNode> nodes;
  std::vector<GEdge> edges;
  std::unordered_set<std::string> blast; // highlighted node ids (impact set)
  std::string blastEpicenter;            // node id the wave leaves from
  double blastT0 = 0.0;                  // ImGui::GetTime() when blast landed
  // Hop distance from blastEpicenter over visibleEdges, so the shockwave can
  // leave the epicentre ALONG the edges instead of as concentric rings drawn
  // over unrelated geometry. Rebuilt lazily; the BFS needs visibleEdges, which
  // does not exist yet when the impact RPC lands.
  std::unordered_map<std::string, int> blastHop;
  bool blastHopDirty = true;
  // False when the impact set has more internal edges than kBlastEdgeAnimMax:
  // past that the travelling heads are visual noise AND a per-frame cost, so
  // the wave animates the nodes only. Set by rebuildBlastHops.
  bool blastEdgeAnim = false;
  std::string selectedId;
  std::string hoverId;
  char search[128] = {};
  float panX = 0.0f, panY = 0.0f, zoom = 1.0f;
  bool wasAppFocusLost = false;
  double lastFetch = -1e9;
  // When the current blocking (non-silent) load was issued — drives the
  // elapsed-time line of the loading overlay.
  double loadStartedAt = 0.0;
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
  // Keyword tagging: add-field buffer (chips render from GNode::metaKeywords).
  char keywordAdd[96] = {};
  // Cluster-seed layout mode: 0 = current behavior, 1 = By project,
  // 2 = By keyword, 3 = By folder (project web view only).
  int clusterMode = 0;
  // Merge linked projects into the graph (cad_graph_get linked=true).
  bool linkedProjects = false;
  // Distinct meta.project values across loaded nodes, sorted (cross-project
  // rim tint + legend when >1). Rebuilt with every load.
  std::vector<std::string> projects;
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
  // Grid/List controls (graph-only controls hide in those modes): kind
  // filter (empty = all) and sort mode (0=Name, 1=Kind, 2=Newest).
  std::unordered_set<std::string> kindFilter;
  int gridSort = 0;
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
  // Camera TARGETS. Every input site writes these; the live orbit/pan/zoom
  // above chase them through ui::anim::toward once per frame, which is what
  // makes fly-to, inertia and idle drift a camera concern only — no layout
  // position is ever touched (the hierarchy pack must stay frozen).
  float orbitYawT = 0.55f;
  float orbitPitchT = -0.30f;
  float panXT = 0.0f, panYT = 0.0f, zoomT = 1.0f;
  // Orbit throw: angular velocity captured on the last drag frame, decayed to
  // rest after release (~0.6 s).
  float orbitVelYaw = 0.0f, orbitVelPitch = 0.0f;
  bool orbitDragging = false;
  // Cursor parallax (3D): the projection leans a few degrees toward the
  // cursor's offset from canvas centre. Live values; eased at kFast.
  float parYaw = 0.0f, parPitch = 0.0f;
  // Fly-to in flight until this timestamp — the camera eases at kSlow instead
  // of kBase while a framing move is running.
  double flyUntil = -1.0;
  // Idle auto-orbit ("cinematic drift"): on by default, defeatable from the
  // canvas mode badge. lastInputT is stamped by ANY pointer motion.
  bool autoOrbit = true;
  double lastInputT = 0.0;
  // Depth normalization for 3D: raw seed z (containment depth) is scaled so
  // the deepest node sits at a bounded fraction of the focal distance. Keeps
  // perspective sane (no clamp/blow-up) and depth readable for ANY tree size.
  float zScale = 1.0f;
  // In-session per-view position stashes — toggling views swaps these in so
  // each view keeps its own arrangement without clobbering the other's.
  std::unordered_map<std::string, Pos3> projPosStash;
  std::unordered_map<std::string, Pos3> asmPosStash;
  // 2D positions stashed while the 3D world-position overlay is active.
  std::unordered_map<std::string, Pos3> preWorldStash;
  // 2D<->3D toggle transition: positions lerp from->to over 0.35 s
  // (smoothstep) so mode switches read as motion instead of a jump cut.
  std::unordered_map<std::string, Pos3> trans3dFrom, trans3dTo;
  double t3dStart = -1.0;

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
  // Whole-graph out/in degree per node id, hoisted out of the inspector's
  // per-frame O(edges) rescan. Rebuilt with the data, not with the frame.
  std::unordered_map<std::string, std::pair<int, int>> degree;
  // instance:… node id → nodes[] index of the doc it instances (INSTANCE_OF
  // target). Thumbnails MUST resolve through this: an instance's own path is
  // the CONTAINING assembly, so the raw path renders the parent every time.
  std::unordered_map<std::string, int> instTargetIdx;
  bool edgeIndexDirty = true;
  // Endpoints of a stale external reference, for the warning rim.
  std::unordered_set<std::string> staleNodes;

  // Force-directed layout (graph_force.h). forceSlot maps nodes[] index →
  // sim node index (-1 when not in the sim); GNode::lx/ly stay the render
  // cache. The sim is rebuilt when layoutDirty, stepped until settled.
  ui::ForceGraph force;
  std::vector<int> forceSlot;
  int dragNode = -1;        // nodes[] index currently physics-dragged
  int dragCandidate = -1;   // nodes[] index pressed, awaiting drag threshold
  bool fitOnSettle = false; // fit once when a fresh layout first settles
  // Per-frame scratch: visibleIdx re-sorted back-to-front by projected depth
  // (3D only) plus the perspective each visible node projected to. Members so
  // the sort does not allocate every frame at ME489 scale.
  std::vector<int> drawOrder;
  std::vector<float> perspOf; // indexed by nodes[] index
  // Label occupancy scratch (per frame). labelShown is indexed by nodes[]
  // index and gates the paint; labelTaken holds the rects already reserved.
  // Members so the pass does not allocate every frame at ME489 scale.
  std::vector<LabelCand> labelCand;
  std::vector<ImRect> labelTaken;
  std::vector<char> labelShown;
  // Search match cache (rebuilt when the query or visible set changes).
  std::unordered_set<std::string> matchIds;
  // Query the cache was last built for, and when that query changed. The search
  // spotlight's breathing ring decays from matchT0, so a data poll (which also
  // dirties the cache) must not restart it.
  std::string matchQuery;
  double matchT0 = 0.0;
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
  // Legend card: a bottom-left canvas overlay, collapsed to an icon chip by
  // default so it stops covering the graph it explains.
  bool legendOpen = false;
  // '/' focuses the search field — SetKeyboardFocusHere has to run in the
  // frame that submits the widget, so the key handler defers through this.
  bool focusSearchPending = false;
  // Delete on a selected authored link raises the SAME confirmation the
  // inspector's popover trigger opens.
  bool linkRemoveConfirm = false;
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
         k == "drawing" || k == "board" || k == "firmware" || k == "sim_study" ||
         k == "file" || k == "keyword";
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
  if (k == "drawing" || k == "sim_study" || k == "board" || k == "firmware" ||
      k == "file")
    return 5;
  if (k == "keyword")
    return 6;
  return 3;
}

// file/keyword get dedicated hues (same static-color convention as the
// DOCUMENTS/SIMULATES/MANUFACTURES edge colors below): no palette token fits
// "neutral file asset" or "pop-out keyword tag" without colliding with an
// existing kind.
static const float kFileNodeColor[4] = {0.482f, 0.549f, 0.639f,
                                        1.0f}; // slate gray-blue
static const float kKeywordNodeColor[4] = {0.988f, 0.749f, 0.180f,
                                           1.0f}; // amber/gold — pops

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
  else if (k == "file")
    c = kFileNodeColor;
  else if (k == "keyword")
    c = kKeywordNodeColor;
  else
    c = palette.textDim;
  return ImGui::GetColorU32(ImVec4(c[0], c[1], c[2], a));
}

// DOCUMENTS/SIMULATES/MANUFACTURES used to alias palette.accent/simMesh/
// statusOk — accent and statusOk are the SAME teal (statusOk is defined as
// "same as accent"), so DOCUMENTS and MANUFACTURES rendered as literally
// identical colors, and simMesh sits a hair off diffHunk (REFERENCES),
// clustering three edge kinds into one indistinguishable blue/teal family.
// These three get their own hues, spread away from every other edge color
// (red/amber/green/blue/purple already claimed) instead of reusing
// generically-named theme fields whose values happen to collide.
static const float kDocumentsColor[4] = {0.902f, 0.478f, 0.702f, 1.0f}; // rose
static const float kSimulatesColor[4] = {0.902f, 0.451f, 0.251f, 1.0f}; // burnt orange
static const float kManufacturesColor[4] = {0.243f, 0.780f, 0.729f,
                                            1.0f}; // cyan-teal (distinct from
                                                   // INSTANCE_OF's true green)
static const float kTaggedColor[4] = {0.965f, 0.820f, 0.110f,
                                      1.0f}; // pure gold — more yellow than
                                             // DEPENDS_ON's sandy amber

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
    c = kDocumentsColor;
  else if (k == "SIMULATES")
    c = kSimulatesColor;
  else if (k == "MANUFACTURES")
    c = kManufacturesColor;
  else if (k == "TAGGED")
    c = kTaggedColor;
  return ImGui::GetColorU32(ImVec4(c[0], c[1], c[2], a));
}

// Stable per-project rim color for cross-project merged graphs: FNV-1a of the
// project root → hue (same idiom as the ai role colors), fixed sat/val keeps
// it legible as a node border on the dark canvas.
ImU32 projectColor(const std::string &proj, float a = 1.0f) {
  std::uint32_t h = 2166136261u;
  for (unsigned char c : proj) {
    h ^= c;
    h *= 16777619u;
  }
  const float hue = (float)(h % 360u) / 360.0f;
  float r, g, b;
  ImGui::ColorConvertHSVtoRGB(hue, 0.58f, 0.92f, r, g, b);
  return ImGui::GetColorU32(ImVec4(r, g, b, a));
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
  if (k == "file")
    return "FILE";
  if (k == "keyword")
    return "KW";
  return "NODE";
}

// Canonical edge-kind vocabulary (contracts/cad_graph.md) — filter chips + the
// link-authoring combo iterate this. Index 4 == REFERENCES (linkKindIdx
// default).
static const char *kEdgeKinds[] = {
    "CONTAINS",     "DEPENDS_ON", "MATES",     "INSTANCE_OF",  "REFERENCES",
    "DERIVES_FROM", "DOCUMENTS",  "SIMULATES", "MANUFACTURES", "TAGGED",
};

// Display label for an edge-kind token: "DEPENDS_ON" -> "Depends On". The
// raw upper_snake token is still what's stored in hiddenKinds / sent to the
// backend; only the chip/legend text is reformatted for readability.
std::string edgeKindLabel(const std::string &k) {
  std::string out;
  out.reserve(k.size());
  bool startOfWord = true;
  for (char c : k) {
    if (c == '_') {
      out += ' ';
      startOfWord = true;
    } else if (startOfWord) {
      out += (char)std::toupper((unsigned char)c);
      startOfWord = false;
    } else {
      out += (char)std::tolower((unsigned char)c);
    }
  }
  return out;
}

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

// Whole-graph derived edge facts that the DRAW and INSPECT paths used to
// recompute by scanning G.edges every frame: out/in degree per node, and the
// INSTANCE_OF target of every instance node. Both are O(edges) once per data
// load instead of O(edges) per node per frame — at ME489 scale the old
// inspector rescan alone was ~60k string compares a frame, and resolving an
// instance thumbnail was the same scan again per instance.
void rebuildEdgeIndex(GraphTabState &G) {
  G.edgeIndexDirty = false;
  G.degree.clear();
  G.instTargetIdx.clear();
  G.degree.reserve(G.nodes.size() * 2);
  for (const auto &e : G.edges) {
    ++G.degree[e.src].first;
    ++G.degree[e.dst].second;
    if (e.kind == "INSTANCE_OF" && e.src.rfind("instance:", 0) == 0 &&
        !G.instTargetIdx.count(e.src)) {
      auto it = G.idIndex.find(e.dst);
      if (it != G.idIndex.end())
        G.instTargetIdx[e.src] = it->second;
    }
  }
}

void ensureEdgeIndex(GraphTabState &G) {
  if (G.edgeIndexDirty)
    rebuildEdgeIndex(G);
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
      bool stale = false; // any aggregated member stale → the bundle is stale
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
        a.stale = e.stale;
      } else {
        // Prefer extracted origin as representative when mixed.
        if (a.origin != "extracted" && e.origin == "extracted") {
          a.origin = e.origin;
          a.dimLabel = e.dimLabel;
          a.srcEdgeIdx = ei;
        }
        a.stale = a.stale || e.stale;
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
      ve.stale = a.stale;
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
      ve.stale = e.stale;
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
      ve.stale = e.stale;
      G.visibleEdges.push_back(std::move(ve));
    }
  }

  // Endpoints of a stale external reference get a warning rim; recomputed
  // here so the paint loop never re-scans.
  G.staleNodes.clear();
  for (const auto &ve : G.visibleEdges) {
    if (!ve.stale)
      continue;
    G.staleNodes.insert(ve.src);
    G.staleNodes.insert(ve.dst);
  }

  G.layoutDirty = true;
  G.matchDirty = true;
  G.blastHopDirty = true;
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
  const bool wasInScope = !G.scopeDocId.empty();
  G.scopeDocId = docId;
  G.selectedId.clear();
  G.selectedEdge = -1;
  clearBlast(G);
  G.hoverId.clear();
  G.userNavigated = false; // fresh scope → allow one auto-fit again
  // Assemblies (or anything with bom_items) open in decomposition view.
  G.scopeDecomp = false;
  // Keep the deterministic frozen layout in scopes too — running the live
  // force sim on a scoped subtree collapses it into a horizontal band (the
  // "assembly breaks 2D/3D" bug). hierarchySeed builds its forest from
  // visibleEdges, which is already scope-filtered, so it packs the scoped
  // CONTAINS subtree correctly with the sim frozen.
  if (docId.empty()) {
    G.hierarchyLayout = G.projWasHierarchy;
    // Restore flat project positions (stash first, then persisted cache) so
    // the web view isn't distorted by the scoped repack we forced on entry.
    for (auto &n : G.nodes) {
      if (auto it = G.projPosStash.find(n.id); it != G.projPosStash.end()) {
        n.lx = it->second.x;
        n.ly = it->second.y;
        n.lz = it->second.z;
      } else if (auto it = G.layoutCache.find(n.id);
                 it != G.layoutCache.end()) {
        n.lx = it->second.x;
        n.ly = it->second.y;
        n.lz = it->second.z;
      }
    }
  } else {
    // Entering a scope from project view: save the freeze flag and stash the
    // flat arrangement so scope-EXIT restores it exactly (no distortion).
    // Nested drills (scope→scope) skip the stash — projPosStash still holds
    // the true project layout, and scope positions are always re-derived.
    if (!wasInScope) {
      G.projWasHierarchy = G.hierarchyLayout;
      for (auto &n : G.nodes)
        if (n.lx != 0.0f || n.ly != 0.0f || n.lz != 0.0f)
          G.projPosStash[n.id] = {n.lx, n.ly, n.lz};
    }
    G.hierarchyLayout = true; // frozen deterministic structure in scope
    auto it = G.idIndex.find(docId);
    if (it != G.idIndex.end() &&
        (G.nodes[it->second].kind == "assembly" || hasBomChildren(G, docId)))
      G.scopeDecomp = true;
    // Force a fresh deterministic repack of the scoped subtree (frozen sim).
    // Without this the scoped nodes keep stale positions, the seed never
    // fires, and the live force sim would collapse them into a band.
    for (auto &n : G.nodes)
      n.lx = n.ly = 0.0f;
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
      // z = containment depth, stretched 2.2x so 3D reads with real
      // separation between levels instead of a near-flat disc.
      outZ[static_cast<size_t>(u)] =
          ((float)depth[static_cast<size_t>(u)] - (float)meanDepth) * hop *
          2.2f;
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
  // Leftovers (no placed neighbor anywhere): disconnected FRAGMENTS. Cluster
  // each fragment (connected over ANY edge kind) into its own phyllotaxis
  // ball and shelf-pack the balls to the RIGHT of the main pack — the user's
  // "each interlinked group gets its own circle" instead of the old golden
  // ring of clutter around the main web.
  {
    // Fragment ids over adjAll, unplaced fresh nodes only.
    std::vector<int> frag(static_cast<size_t>(V), -1);
    int nFrag = 0;
    std::vector<int> stack;
    for (int i = 0; i < V; ++i) {
      if (placed[static_cast<size_t>(i)] || !isFresh[static_cast<size_t>(i)] ||
          frag[static_cast<size_t>(i)] >= 0)
        continue;
      const int fid = nFrag++;
      stack.clear();
      stack.push_back(i);
      frag[static_cast<size_t>(i)] = fid;
      while (!stack.empty()) {
        const int u = stack.back();
        stack.pop_back();
        for (int v : adjAll[static_cast<size_t>(u)]) {
          if (placed[static_cast<size_t>(v)] ||
              !isFresh[static_cast<size_t>(v)] ||
              frag[static_cast<size_t>(v)] >= 0)
            continue;
          frag[static_cast<size_t>(v)] = fid;
          stack.push_back(v);
        }
      }
    }
    if (nFrag > 0) {
      std::vector<std::vector<int>> members(static_cast<size_t>(nFrag));
      for (int i = 0; i < V; ++i)
        if (frag[static_cast<size_t>(i)] >= 0)
          members[static_cast<size_t>(frag[static_cast<size_t>(i)])].push_back(i);
      // Biggest fragments first (stable: lower id wins ties — deterministic).
      std::vector<int> ford(static_cast<size_t>(nFrag));
      for (int f = 0; f < nFrag; ++f)
        ford[static_cast<size_t>(f)] = f;
      std::sort(ford.begin(), ford.end(), [&](int a, int b) {
        if (members[static_cast<size_t>(a)].size() !=
            members[static_cast<size_t>(b)].size())
          return members[static_cast<size_t>(a)].size() >
                 members[static_cast<size_t>(b)].size();
        return a < b;
      });
      // Main-pack extent anchors the shelf origin. Fresh nodes wrote their
      // position into outX/outY; already-placed (cached/warm) nodes never
      // did, so their real extent must come from the node's own lx/ly —
      // skipping them (as before) left mainExt pinned at an oversized
      // fallback floor whenever the whole main pack was warm (the common
      // case on a routine refresh), stranding fragments far off-canvas from
      // an already-tight main cluster. Only fall back to a small floor when
      // there is truly no main pack to anchor against.
      float mainExt = 0.0f;
      bool haveMain = false;
      for (int i = 0; i < V; ++i) {
        if (!placed[static_cast<size_t>(i)])
          continue;
        float px, py;
        if (isFresh[static_cast<size_t>(i)]) {
          px = outX[static_cast<size_t>(i)];
          py = outY[static_cast<size_t>(i)];
        } else {
          const GNode &pn = G.nodes[G.visibleIdx[static_cast<size_t>(i)]];
          px = pn.lx;
          py = pn.ly;
        }
        mainExt = std::max(mainExt, std::fabs(px));
        mainExt = std::max(mainExt, std::fabs(py));
        haveMain = true;
      }
      if (!haveMain)
        mainExt = spacing * 2.0f; // no main pack at all: small, sane floor
      const float x0 = mainExt + 2.2f * spacing;
      float sx = x0, sy = -mainExt, rowH = 0.0f;
      const float rowW = 2.0f * mainExt + 4.0f * spacing;
      for (int f : ford) {
        const auto &mem = members[static_cast<size_t>(f)];
        const float br = spacing * 0.9f *
                             std::sqrt((float)mem.size() + 0.5f) +
                         spacing * 0.5f;
        if (sx + 2.0f * br > x0 + rowW) {
          sx = x0;
          sy += rowH + spacing;
          rowH = 0.0f;
        }
        const float cx = sx + br, cy = sy + br;
        for (size_t k = 0; k < mem.size(); ++k) {
          const int i = mem[k];
          const float rr = spacing * 0.9f * std::sqrt((float)k + 0.5f);
          const float a = (float)k * GOLDEN;
          outX[static_cast<size_t>(i)] = cx + rr * std::cos(a);
          outY[static_cast<size_t>(i)] = cy + rr * std::sin(a);
          placed[static_cast<size_t>(i)] = 1;
        }
        sx += 2.0f * br + spacing;
        rowH = std::max(rowH, 2.0f * br);
      }
    }
  }
  // Anything still unplaced (no edges at all): deterministic origin cluster.
  for (int i = 0; i < V; ++i) {
    if (placed[static_cast<size_t>(i)] || !isFresh[static_cast<size_t>(i)])
      continue;
    const float a = (float)i * GOLDEN;
    outX[static_cast<size_t>(i)] = spacing * 0.35f * std::cos(a);
    outY[static_cast<size_t>(i)] = spacing * 0.35f * std::sin(a);
  }
}

// Layered assembly-tree seed (Sugiyama-lite, deterministic). Layers =
// containment/instance depth from root assemblies; within-layer order runs
// barycenter sweeps over hierarchy + mates edges so mated siblings sit
// together and hierarchy webs cross minimally. z = depth (3D readable).
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
  // z = depth so the 3D toggle gives the tree real vertical separation
  // (was z=0 flat plane — the "asm view breaks 3D" bug).
  const float layerGap = 300.0f * S;
  const float xGap = 90.0f * S;
  const float zMid = 0.5f * (float)(nLayer - 1);
  for (int li = 0; li < nLayer; ++li) {
    const auto &L = layers[static_cast<size_t>(li)];
    if (L.empty())
      continue;
    float width = 0.0f;
    for (int u : L)
      width += 2.0f * collR[static_cast<size_t>(u)] + xGap;
    width -= xGap;
    float cursor = -0.5f * width;
    const float lz = ((float)li - zMid) * layerGap * 0.9f;
    for (int u : L) {
      const float r = collR[static_cast<size_t>(u)];
      outX[static_cast<size_t>(u)] = cursor + r;
      outY[static_cast<size_t>(u)] = (float)li * layerGap;
      outZ[static_cast<size_t>(u)] = lz;
      cursor += 2.0f * r + xGap;
    }
  }
}

// Directory portion of a graph-relative path ("" when the path has none).
std::string pathDir(const std::string &p) {
  auto sl = p.find_last_of("/\\");
  return sl == std::string::npos ? std::string() : p.substr(0, sl);
}

// Cluster grouping for the toolbar "Layout" modes, by visOrd (-1 = no group).
// Deterministic: group keys sort, ids assign in sorted-key order.
//   mode 1 (By project): meta.project root.
//   mode 2 (By keyword): a node joins the group of the first keyword node
//     (sorted id) whose TAGGED edge reaches it; keyword nodes anchor their
//     own group so the tag hub sits inside its neighborhood.
//   mode 3 (By folder): meta.folder for file nodes, else the path's dir.
std::vector<int> clusterGroups(GraphTabState &G, int mode) {
  const int V = (int)G.visibleIdx.size();
  std::vector<std::string> keys(static_cast<size_t>(V));
  if (mode == 2) {
    std::unordered_map<std::string, std::string> firstKw; // target → kw node
    for (const auto &e : G.edges) {
      if (e.kind != "TAGGED")
        continue;
      auto it = firstKw.find(e.dst);
      if (it == firstKw.end() || e.src < it->second)
        firstKw[e.dst] = e.src;
    }
    for (int i = 0; i < V; ++i) {
      const GNode &n = G.nodes[G.visibleIdx[static_cast<size_t>(i)]];
      if (n.kind == "keyword")
        keys[static_cast<size_t>(i)] = n.id;
      else if (auto it = firstKw.find(n.id); it != firstKw.end())
        keys[static_cast<size_t>(i)] = it->second;
    }
  } else if (mode == 1) {
    for (int i = 0; i < V; ++i)
      keys[static_cast<size_t>(i)] = G.nodes[G.visibleIdx[static_cast<size_t>(i)]].metaProject;
  } else if (mode == 3) {
    for (int i = 0; i < V; ++i) {
      const GNode &n = G.nodes[G.visibleIdx[static_cast<size_t>(i)]];
      keys[static_cast<size_t>(i)] =
          !n.metaFolder.empty() ? n.metaFolder : pathDir(n.path);
    }
  }
  std::map<std::string, int> gid;
  for (int i = 0; i < V; ++i)
    if (!keys[static_cast<size_t>(i)].empty())
      gid[keys[static_cast<size_t>(i)]];
  int next = 0;
  for (auto &kv : gid)
    kv.second = next++;
  std::vector<int> out(static_cast<size_t>(V), -1);
  for (int i = 0; i < V; ++i)
    if (!keys[static_cast<size_t>(i)].empty())
      out[static_cast<size_t>(i)] = gid[keys[static_cast<size_t>(i)]];
  return out;
}

// Cluster seed: initial-position grouping BEFORE force relaxation (the sim
// stays live — unlike hierarchySeed nothing freezes). Group centroids sit on
// a coarse deterministic circle; fresh members seed on a golden-angle
// phyllotaxis disc around their centroid. Warm nodes keep persisted positions.
// outGid (by visOrd) feeds force group-pull so clusters hold during the
// relax. Returns false when nothing groups under this mode (caller falls back
// to the regular seeds).
bool clusterSeed(GraphTabState &G, const std::vector<char> &isFresh,
                 float spacing, int mode, std::vector<float> &outX,
                 std::vector<float> &outY, std::vector<float> &outZ,
                 std::vector<int> &outGid) {
  const int V = (int)G.visibleIdx.size();
  outX.assign(static_cast<size_t>(V), 0.0f);
  outY.assign(static_cast<size_t>(V), 0.0f);
  outZ.assign(static_cast<size_t>(V), 0.0f);
  outGid = clusterGroups(G, mode);
  int nG = 0;
  for (int g : outGid)
    nG = std::max(nG, g + 1);
  if (nG == 0)
    return false;

  constexpr float GOLDEN = 2.39996323f;
  const float ring =
      nG > 1 ? spacing * 1.7f * std::sqrt((float)nG) : 0.0f;
  std::vector<std::pair<float, float>> cent(static_cast<size_t>(nG), {0.f, 0.f});
  for (int g = 0; g < nG; ++g) {
    const float ang =
        (float)g * (6.28318530718f / (float)nG) - 1.57079632679f;
    cent[static_cast<size_t>(g)] = {ring * std::cos(ang), ring * std::sin(ang)};
  }
  std::vector<int> memb(static_cast<size_t>(nG), 0);
  for (int i = 0; i < V; ++i) {
    const int g = outGid[static_cast<size_t>(i)];
    if (g < 0)
      continue;
    const int k = memb[static_cast<size_t>(g)]++;
    if (!isFresh[static_cast<size_t>(i)])
      continue;
    const float rr = spacing * 0.75f * std::sqrt((float)k + 0.5f);
    const float a = (float)k * GOLDEN;
    outX[static_cast<size_t>(i)] =
        cent[static_cast<size_t>(g)].first + rr * std::cos(a);
    outY[static_cast<size_t>(i)] =
        cent[static_cast<size_t>(g)].second + rr * std::sin(a);
  }
  // Ungrouped fresh nodes: loose ring outside the cluster pack.
  int u = 0;
  for (int i = 0; i < V; ++i) {
    if (outGid[static_cast<size_t>(i)] >= 0 ||
        !isFresh[static_cast<size_t>(i)])
      continue;
    const float a = (float)u * GOLDEN;
    const float rr = ring + spacing * (1.2f + 0.4f * std::sqrt((float)u));
    outX[static_cast<size_t>(i)] = rr * std::cos(a);
    outY[static_cast<size_t>(i)] = rr * std::sin(a);
    ++u;
  }
  return true;
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
  // Keyword hubs scale with their TAGGED degree (r *= min(1 + 0.15*deg, 2)).
  std::unordered_map<std::string, int> taggedDeg;
  for (const auto &e : G.edges)
    if (e.kind == "TAGGED")
      ++taggedDeg[e.src];
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
    else if (n.kind == "keyword") {
      auto td = taggedDeg.find(n.id);
      if (td != taggedDeg.end())
        discR *= std::min(1.0f + 0.15f * (float)td->second, 2.0f);
    }
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
  //  - Cluster modes (Layout combo): group centroids on a coarse circle,
  //    members around their centroid — seed ONLY, sim relaxes live.
  //  - Mostly-fresh web: balloon hierarchy over the CONTAINS forest +
  //    neighbor-anchored reference singles (hierarchySeed) — pure
  //    structure, sim frozen; no collapse/homogenization modes.
  //  - A few new nodes on a warm layout: cheap golden-angle append (below).
  std::vector<float> seedX, seedY, seedZ;
  std::vector<int> clusterGid; // by visOrd; overrides component group-pull
  bool haveSeed = false;
  if (fresh > 0 && G.asmView && G.scopeDocId.empty()) {
    layeredAsmSeed(G, collROrd, S, seedX, seedY, seedZ);
    haveSeed = true;
  } else if (G.clusterMode != 0 && G.scopeDocId.empty() && fresh > 0 &&
             clusterSeed(G, isFresh, 190.0f * S, G.clusterMode, seedX, seedY,
                         seedZ, clusterGid)) {
    haveSeed = true; // clustered seed — the sim relaxes from it, no freeze
  } else if (fresh * 2 > V) {
    hierarchySeed(G, visOrd, adjAll, isFresh, 190.0f * S, seedX,
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
    // Cluster modes group-pull by cluster key so the seeded clusters hold
    // together while the live sim relaxes; otherwise component grouping.
    int gid = cid;
    if (!clusterGid.empty() && i < (int)clusterGid.size() &&
        clusterGid[static_cast<size_t>(i)] >= 0)
      gid = clusterGid[static_cast<size_t>(i)];
    if (gid >= 0)
      G.force.setGroup(slot, gid);
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

// `phase` in [0,1) slides the dash pattern along the line by that fraction of
// one dash+gap period — "marching ants". Callers that pass a live phase MUST
// come from ui::anim::phase(), which requests frames; a raw GetTime() march
// steps at the 20 Hz idle floor whenever the mouse is still.
void drawDashedLine(ImDrawList *dl, ImVec2 a, ImVec2 b, ImU32 col, float thick,
                    float dash = 6.0f, float gap = 4.0f, float phase = 0.0f) {
  float dx = b.x - a.x, dy = b.y - a.y;
  float len = std::sqrt(dx * dx + dy * dy);
  if (len < 0.5f)
    return;
  float ux = dx / len, uy = dy / len;
  const float period = dash + gap;
  // Start one full period behind so the marched-in dash enters from the source
  // end instead of popping into existence mid-line.
  float t = -period + std::fmod(std::max(0.0f, phase), 1.0f) * period;
  bool on = true;
  while (t < len) {
    float seg = on ? dash : gap;
    float t1 = std::min(t + seg, len);
    if (on && t1 > 0.0f) {
      const float ts = std::max(t, 0.0f);
      ImVec2 p0(a.x + ux * ts, a.y + uy * ts);
      ImVec2 p1(a.x + ux * t1, a.y + uy * t1);
      dl->AddLine(p0, p1, col, thick);
    }
    t = t1;
    on = !on;
  }
}

// ---- settled-layout cache (solve state) -------------------------------------
//
// Bump kLayoutVersion whenever seed/clustering/force policy changes enough that
// old positions would re-open as a hairball. Missing/mismatched version →
// ignore the file and re-seed from the current algorithm (cluster wells).
constexpr int kLayoutVersion = 10;

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

// Second line of the first-load indicator: what is being mapped (the hub's
// tracked-file count once Files/Explorer has it) and, after a couple of
// seconds, how long it has been running — so a slow scan reads as progress,
// not a hang.
std::string graphLoadingDetail(const GraphTabState &G) {
  const auto &H = g_plmHub;
  std::string s;
  if (H.itemsProjectDir == G.projectDir && !H.items.empty())
    s = "Linking " + std::to_string(H.items.size()) +
        (H.items.size() == 1 ? " tracked file" : " tracked files");
  else
    s = "Reading project files";
  const double elapsed = ImGui::GetTime() - G.loadStartedAt;
  if (elapsed >= 2.0) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), " \xc2\xb7 %d s", (int)elapsed);
    s += buf;
  }
  return s;
}

void requestGraph(AppState &st, bool force) {
  auto &G = S();
  if (!G.available)
    return;
  if (G.fetchInFlight)
    return;
  const std::string proj = st.plmProjectPath();
  if (proj.empty())
    return;
  const double now = ImGui::GetTime();
  // Background poll cadence. Every request — including an empty graph or a
  // failed one — waits this long before the next automatic attempt: the
  // worker answers RPCs one at a time, so an unthrottled retry loop starves
  // every other panel's requests behind it.
  constexpr double kGraphPollSeconds = 10.0;
  if (!force && proj == G.projectDir && (now - G.lastFetch) < kGraphPollSeconds)
    return;

  G.err.clear();
  G.projectDir = proj;
  G.lastFetch = now;
  // Background change-probes stay invisible: no spinner, no button flicker.
  const bool silent = !force && G.mtimesMax > 0.0 && !G.nodes.empty();
  G.loading = !silent;
  G.fetchInFlight = true;
  const unsigned serial = ++G.fetchSerial;
  if (G.loading)
    G.loadStartedAt = now;

  nlohmann::json p;
  p["project_dir"] = proj;
  // Also accept path-style callers; harmless extra key for robust workers.
  p["path"] = proj;
  // Archive dirs are excluded server-side by default; flip only when the
  // user unchecks "Hide archive/stale".
  p["include_archive"] = !G.hideArchive;
  // Cross-project merge: linked projects' graphs fold into the response,
  // each node carrying its owning root in meta.project.
  p["linked"] = G.linkedProjects;
  if (force)
    p["force"] = true;
  // Change probe: when the worker confirms nothing changed we get a tiny
  // "unchanged" reply instead of the full multi-MB payload.
  if (!force && G.mtimesMax > 0.0) {
    p["if_mtimes_max"] = G.mtimesMax;
    p["if_sources_count"] = G.sourcesCount;
  }

  st.rpc.call("cad_graph_get", p, [proj, serial, &st](const core::RpcResult &r) {
    auto &G = S();
    if (serial != G.fetchSerial || proj != G.projectDir) return;
    G.fetchInFlight = false;
    G.loading = false;
    if (proj != st.plmProjectPath()) return;
    // The poll interval runs from when the answer landed, not from when it
    // was queued, so a slow response is never immediately re-requested.
    G.lastFetch = ImGui::GetTime();
    G.loading = false;
    if (isAbsent(r)) {
      G.available = false;
      G.nodes.clear();
      G.edges.clear();
      G.edgeIndexDirty = true;
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
          n.metaUn = jstr(*m, "un");
          if (auto mat = m->find("mat"); mat != m->end() && mat->is_array()) {
            for (const auto &mv : *mat)
              if (mv.is_string())
                n.metaMat.push_back(mv.get<std::string>());
          }
          if (auto kw = m->find("keywords");
              kw != m->end() && kw->is_array()) {
            for (const auto &kv : *kw)
              if (kv.is_string())
                n.metaKeywords.push_back(kv.get<std::string>());
          }
          n.metaPreview = jstr(*m, "text_preview");
          n.metaProject = jstr(*m, "project");
          n.metaFolder = jstr(*m, "folder");
          // Vault identity (_merge_items) — _sanitize_meta keeps these.
          n.metaItemId = jstr(*m, "item_id");
          n.metaPartNumber = jstr(*m, "part_number");
          n.metaLifecycle = jstr(*m, "lifecycle");
          if (auto sup = m->find("suppressed");
              sup != m->end() && sup->is_boolean())
            n.metaSuppressed = sup->get<bool>();
          n.metaExpr = jstr(*m, "expr");
          n.metaEqUnit = jstr(*m, "unit");
          if (auto val = m->find("value"); val != m->end()) {
            if (val->is_number()) {
              char vb[32];
              std::snprintf(vb, sizeof(vb), "%g", val->get<double>());
              n.metaEqValue = vb;
            } else if (val->is_string()) {
              n.metaEqValue = val->get<std::string>();
            }
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
          if (auto stl = m->find("stale"); stl != m->end() && stl->is_boolean())
            e.stale = stl->get<bool>();
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

    // Distinct owning project roots (meta.project) — >1 means the response
    // merged linked projects; drives the rim tint + legend.
    {
      std::set<std::string> ps;
      for (const auto &n : G.nodes)
        if (!n.metaProject.empty())
          ps.insert(n.metaProject);
      G.projects.assign(ps.begin(), ps.end());
    }

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
    rebuildEdgeIndex(G);
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
    G.blastHopDirty = true;
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

// Keyword tagging (cad_graph_keyword_add/remove). Same invalidation pattern
// as requestLinkAdd: on any reply the next frame refetches so the kw: node +
// its TAGGED edge land in the graph.
void requestKeywordAdd(AppState &st, const std::string &target,
                       const std::string &keyword) {
  auto &G = S();
  if (!G.available || target.empty() || keyword.empty())
    return;
  nlohmann::json p;
  p["project_dir"] = st.plmProjectPath();
  p["path"] = st.plmProjectPath();
  p["target"] = target;
  p["keyword"] = keyword;
  st.rpc.call("cad_graph_keyword_add", p, [](const core::RpcResult &r) {
    if (isAbsent(r)) {
      S().available = false;
      return;
    }
    S().lastFetch = -1e9;
  });
}

void requestKeywordRemove(AppState &st, const std::string &target,
                          const std::string &keyword) {
  auto &G = S();
  if (!G.available || target.empty() || keyword.empty())
    return;
  nlohmann::json p;
  p["project_dir"] = st.plmProjectPath();
  p["path"] = st.plmProjectPath();
  p["target"] = target;
  p["keyword"] = keyword;
  st.rpc.call("cad_graph_keyword_remove", p, [](const core::RpcResult &r) {
    if (isAbsent(r)) {
      S().available = false;
      return;
    }
    S().lastFetch = -1e9;
  });
}

// ---- filter bookkeeping -----------------------------------------------------
// Every display filter now lives inside one toolbar popover, so the popover
// badge and the blocked-canvas message are the only things left telling the
// user a filter is on. Both read from here.
int activeFilterCount(const GraphTabState &G, bool graphMode) {
  int n = 0;
  if (G.hideArchive)
    ++n;
  if (graphMode && G.hideUnconnected)
    ++n;
  n += (int)G.hiddenKinds.size();
  n += (int)G.kindFilter.size();
  if (G.search[0])
    ++n;
  return n;
}

std::string activeFilterSummary(const GraphTabState &G) {
  std::string s;
  auto add = [&](const std::string &t) {
    if (!s.empty())
      s += " \xc2\xb7 ";
    s += t;
  };
  if (G.hideArchive)
    add("hiding archive/stale");
  if (G.hideUnconnected)
    add("hiding unconnected nodes");
  if (!G.hiddenKinds.empty())
    add(std::to_string(G.hiddenKinds.size()) + " relation kinds hidden");
  if (!G.kindFilter.empty())
    add(std::to_string(G.kindFilter.size()) + " node kinds selected");
  if (G.search[0])
    add(std::string("search \"") + G.search + "\"");
  if (G.asmView)
    add("assembly structure only");
  if (s.empty())
    return "No filters are active.";
  return "Active: " + s + ".";
}

void clearAllFilters(AppState &st, GraphTabState &G) {
  // include_archive is a BACKEND walk policy, so dropping it needs a refetch;
  // everything else is a local visibility rebuild.
  const bool refetch = G.hideArchive;
  G.hideArchive = false;
  G.hideUnconnected = false;
  G.hiddenKinds.clear();
  G.kindFilter.clear();
  G.search[0] = 0;
  G.matchDirty = true;
  if (refetch) {
    G.lastFetch = -1e9;
    requestGraph(st, /*force=*/true);
  } else {
    rebuildVisible(G);
  }
  G.layoutDirty = true;
}

// link_id of the selected edge when it is a hand-authored overlay link (the
// only kind that can be removed at all); empty otherwise.
std::string selectedAuthoredLinkId(const GraphTabState &G) {
  if (G.selectedEdge < 0 || G.selectedEdge >= (int)G.visibleEdges.size())
    return {};
  const VEdge &ve = G.visibleEdges[G.selectedEdge];
  if (ve.aggCount > 1 || ve.srcEdgeIdx < 0 ||
      ve.srcEdgeIdx >= (int)G.edges.size())
    return {};
  const GEdge &e = G.edges[ve.srcEdgeIdx];
  if (e.origin != "manual" && e.origin != "ai")
    return {};
  return e.meta.is_object() ? e.meta.value("link_id", std::string())
                            : std::string();
}

// Graph-panel action button: the published plmGlassButton (one surface for every
// PLM page), plus the Graph inspector's "fill the row" width (-1).
bool graphGlassButton(const char *id, const char *label, bool accent = false,
                      float widthOverride = 0.0f, const char *icon = nullptr) {
  const float w = widthOverride < 0.0f ? ImGui::GetContentRegionAvail().x
                                       : widthOverride;
  return plmGlassButton(id, label, icon, accent, w);
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

// For instance: nodes, the doc node they instance (INSTANCE_OF edge
// src=instance, dst=doc), else nullptr. Thumbnails/open must target the
// REFERENCED doc — the instance's own path is the containing assembly, so
// every instance would otherwise render/open the assembly.
const GNode *instanceTarget(const GraphTabState &G, const GNode &n) {
  if (n.id.rfind("instance:", 0) != 0)
    return nullptr;
  // O(1) through the hoisted index: the node paint loop resolves one of these
  // per instance per frame for billboards, and the old full-edge scan made
  // that quadratic.
  auto it = G.instTargetIdx.find(n.id);
  return it == G.instTargetIdx.end() ? nullptr : &G.nodes[it->second];
}

// Kinds that have an iso render worth billboarding onto the node disc.
bool renderableKind(const std::string &k) {
  return k == "part" || k == "assembly" || k == "doc" || k == "drawing";
}

// Screen radius past which the kind letter dissolves into the real iso render.
constexpr float kBillboardR = 26.0f;
// New thumbnail keys admitted per frame. graphThumbEnsure is O(1) for a key it
// has already seen, but a FIRST touch stats the disk, decodes a PNG and uploads
// a texture — a zoom sweep across 200 nodes would otherwise do all of that in
// one frame. Four keeps the sweep's worst frame under a millisecond of I/O and
// still fills a screenful of billboards within half a second.
constexpr int kBillboardNewPerFrame = 4;

// Keys already handed to graphThumbEnsure at least once, so only genuinely
// cold touches are rate-limited. Cleared with the thumbnail cache itself.
std::unordered_set<std::string> &billboardSeen() {
  static std::unordered_set<std::string> s;
  return s;
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
  if (!graph_thumbnail::readPackageThumbnail(absPath, png)) {
    slot.failed = true;
    c[absPath] = std::move(slot);
    return nullptr;
  }
  int w = 0, h = 0;
  unsigned char *px = graph_thumbnail::decodeThumbnailPng(png, w, h);
  if (!px) {
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

  auto [ins, ok] = c.emplace(absPath, std::move(slot));
  (void)ok;
  return &ins->second;
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

// Two spellings on purpose. makeProj() is the PAINT projection: it folds in
// the cursor parallax lean, so hit-testing and drawing agree on exactly what
// the user sees. makeProjTarget() is the FRAMING projection at the camera's
// resting target, used by fitView/centerOnNode/flyTo so a fit computed while
// the camera is still easing (or leaning) arrives correctly framed.
Proj3 makeProjAt(float yaw, float pitch) {
  Proj3 P;
  P.cy = std::cos(yaw);
  P.sy = std::sin(yaw);
  P.cp = std::cos(pitch);
  P.sp = std::sin(pitch);
  P.focal = 2400.0f * ui::uiScale;
  return P;
}

Proj3 makeProj(const GraphTabState &G) {
  return makeProjAt(G.orbitYaw + G.parYaw, G.orbitPitch + G.parPitch);
}

Proj3 makeProjTarget(const GraphTabState &G) {
  return makeProjAt(G.orbitYawT, G.orbitPitchT);
}

// Ease the LIVE camera toward its target, once per frame, before makeProj. This is the
// single place the camera actually moves; every input site writes only targets.
//
// Camera-side only, by construction: it writes orbitYaw/orbitPitch/pan/zoom, which feed
// nothing but Proj3 at paint time. It never touches G.force or GNode::lx/ly/lz, so the
// frozen hierarchy pack cannot be disturbed by any of it — that invariant is what makes
// this whole feature safe (see the skill: force polish of ANY strength homogenizes the
// tree into a blob).
//
// Frames: anim::toward requests them while it is moving and stops when it settles, so a
// camera at rest lets the app fall back to its 20 Hz idle. Drift, when it engages,
// deliberately holds 60 fps — hence the gates on it.
void tickCamera(GraphTabState &G, ImVec2 origin, ImVec2 canvasSz, bool hovered) {
  (void)origin;
  (void)canvasSz;
  const double now = ImGui::GetTime();
  const float dt = ui::anim::dt();

  // ---- orbit throw: release a drag and the graph keeps turning, decaying to rest.
  // Velocity feeds the TARGET so the tween below stays the only thing that moves the
  // live camera. tau 0.20s => visually at rest in ~0.6s.
  if (!G.orbitDragging && G.view3d &&
      (std::fabs(G.orbitVelYaw) > 1e-4f || std::fabs(G.orbitVelPitch) > 1e-4f)) {
    G.orbitYawT += G.orbitVelYaw * dt;
    G.orbitPitchT =
        std::clamp(G.orbitPitchT + G.orbitVelPitch * dt, -1.35f, 1.35f);
    const float decay = std::exp(-dt / 0.20f);
    G.orbitVelYaw *= decay;
    G.orbitVelPitch *= decay;
    if (std::fabs(G.orbitVelYaw) < 1e-4f) G.orbitVelYaw = 0.0f;
    if (std::fabs(G.orbitVelPitch) < 1e-4f) G.orbitVelPitch = 0.0f;
    ui::anim::requestFrames();
  }

  // ---- idle drift ("cinematic"): a slow yaw the user did not ask for, so it must be
  // easy to defeat and must never fight a reading task. Three gates, all deliberate:
  // nothing selected (reading a node), no search query (hunting a match), and ~6s since
  // the last pointer input. It also does NOT set userNavigated — doing so would silently
  // disable the Fit-on-settle behaviour for the rest of the session.
  const bool driftAllowed = G.autoOrbit && G.view3d && !G.orbitDragging &&
                            G.selectedId.empty() && G.search[0] == '\0' &&
                            G.orbitVelYaw == 0.0f &&
                            (now - G.lastInputT) > 6.0;
  if (driftAllowed) {
    G.orbitYawT += 0.02f * dt;
    ui::anim::requestFrames();
  }

  // ---- cursor parallax: the projection leans a few degrees toward the pointer's offset
  // from canvas centre. Bounded hard at ~0.06 rad — this is depth cueing, not a second
  // orbit control, and anything larger reads as the graph sliding around under the mouse.
  float parYawT = 0.0f, parPitchT = 0.0f;
  if (G.view3d && hovered && canvasSz.x > 1.0f && canvasSz.y > 1.0f) {
    const ImVec2 mp = ImGui::GetIO().MousePos;
    const float nx =
        std::clamp((mp.x - (origin.x + canvasSz.x * 0.5f)) / (canvasSz.x * 0.5f), -1.0f, 1.0f);
    const float ny =
        std::clamp((mp.y - (origin.y + canvasSz.y * 0.5f)) / (canvasSz.y * 0.5f), -1.0f, 1.0f);
    parYawT = nx * 0.06f;
    parPitchT = ny * 0.04f;
  }
  G.parYaw = ui::anim::toward(ImGui::GetID("##plmg_par_yaw"), parYawT, ui::anim::kFast);
  G.parPitch = ui::anim::toward(ImGui::GetID("##plmg_par_pitch"), parPitchT, ui::anim::kFast);

  // ---- the tween. kSlow while a framing move (Fit / fly-to) is in flight so it reads as
  // a dolly; kBase otherwise so ordinary navigation still feels immediate.
  const float dur = now < G.flyUntil ? ui::anim::kSlow : ui::anim::kBase;
  G.orbitYaw = ui::anim::toward(ImGui::GetID("##plmg_yaw"), G.orbitYawT, dur);
  G.orbitPitch = ui::anim::toward(ImGui::GetID("##plmg_pitch"), G.orbitPitchT, dur);
  G.zoom = ui::anim::toward(ImGui::GetID("##plmg_zoom"), G.zoomT, dur);
  G.panX = ui::anim::toward(ImGui::GetID("##plmg_panx"), G.panXT, dur);
  G.panY = ui::anim::toward(ImGui::GetID("##plmg_pany"), G.panYT, dur);
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
  const float lzs = lz * G.zScale;
  const float x1 = lx * P.cy + lzs * P.sy;
  const float z1 = -lx * P.sy + lzs * P.cy;
  const float y1 = ly * P.cp - z1 * P.sp;
  const float z2 = ly * P.sp + z1 * P.cp;
  const float persp = P.focal / std::max(200.0f * ui::uiScale, P.focal + z2);
  px = x1 * persp;
  py = y1 * persp;
  perspOut = persp;
}

// Recompute the 3D depth normalization so the deepest node sits at a bounded
// fraction of the focal distance. Without this, deep hierarchies push z past
// the focal plane and the perspective divide clamps/explodes (nodes fly off
// screen, Fit can't frame them). Called after any layout/position change.
void updateZScale(GraphTabState &G) {
  float maxAbsZ = 0.0f;
  for (int vi : G.visibleIdx)
    maxAbsZ = std::max(maxAbsZ, std::fabs(G.nodes[vi].lz));
  // Target: deepest node at ~0.6 * focal — strong parallax + perspective
  // without ever crossing the near clamp. focal = 2400 * uiScale.
  const float target = 0.6f * 2400.0f * ui::uiScale;
  G.zScale = maxAbsZ > 1.0f ? target / maxAbsZ : 1.0f;
}

// Frame a set of visible nodes. Writes camera TARGETS only — the tween in
// drawCanvas eases the live camera there, which is why Fit and fly-to read as
// a dolly instead of a jump cut. `subset` empty = the whole visible set.
void fitViewTo(GraphTabState &G, ImVec2 canvasSz,
               const std::vector<int> &subset, float maxZoom) {
  const std::vector<int> &set = subset.empty() ? G.visibleIdx : subset;
  if (set.empty() || canvasSz.x < 1.0f || canvasSz.y < 1.0f)
    return;
  const Proj3 P = makeProjTarget(G);
  float minX = 1e9f, minY = 1e9f, maxX = -1e9f, maxY = -1e9f;
  for (int vi : set) {
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
  G.zoomT = std::clamp(std::min(zx, zy), 0.15f, maxZoom);
  float cx = (minX + maxX) * 0.5f;
  float cy = (minY + maxY) * 0.5f;
  G.panXT = canvasSz.x * 0.5f - cx * G.zoomT;
  G.panYT = canvasSz.y * 0.5f - cy * G.zoomT;
}

void fitView(GraphTabState &G, ImVec2 canvasSz) {
  fitViewTo(G, canvasSz, {}, 4.0f);
}

void centerOnNode(GraphTabState &G, const GNode &n, ImVec2 canvasSz) {
  const Proj3 P = makeProjTarget(G);
  float px, py, persp;
  projectPoint(G, P, n.lx, n.ly, n.lz, px, py, persp);
  G.panXT = canvasSz.x * 0.5f - px * G.zoomT;
  G.panYT = canvasSz.y * 0.5f - py * G.zoomT;
}

// Cinematic framing of one node plus its visible neighbourhood. Pan, zoom and
// (in 3D) orbit all retarget together and the tween runs at kSlow, so the
// camera swings around to the node instead of teleporting. Purely camera-side:
// no layout position is touched, so the frozen hierarchy pack is untouched.
void flyToNode(GraphTabState &G, const std::string &id, ImVec2 canvasSz) {
  auto self = G.idIndex.find(id);
  if (self == G.idIndex.end())
    return;
  std::vector<int> set;
  set.push_back(self->second);
  std::unordered_set<std::string> seen{id};
  for (const auto &e : G.visibleEdges) {
    if (G.hiddenKinds.count(e.kind))
      continue;
    const std::string *other = nullptr;
    if (e.src == id)
      other = &e.dst;
    else if (e.dst == id)
      other = &e.src;
    if (!other || !seen.insert(*other).second)
      continue;
    auto it = G.idIndex.find(*other);
    if (it != G.idIndex.end())
      set.push_back(it->second);
  }
  if (G.view3d) {
    // Swing a little so the move reads as a camera orbit, and settle the pitch
    // at a three-quarter view where depth separation is legible.
    G.orbitYawT += 0.35f;
    G.orbitPitchT = -0.28f;
    G.orbitVelYaw = 0.0f;
    G.orbitVelPitch = 0.0f;
  }
  // A lone node would otherwise frame at the 4x clamp and fill the canvas.
  fitViewTo(G, canvasSz, set, 1.8f);
  G.flyUntil = ImGui::GetTime() + (double)ui::anim::kSlow * 2.0;
  G.userNavigated = true;
}

// Atmospheric perspective: lerp a color toward the canvas bed by `k` (0 =
// untouched, 1 = fully dissolved into the background), keeping its alpha. This
// is what makes a 3D web read as depth rather than as a flat tangle.
ImU32 hazeCol(ImU32 c, float k) {
  if (k <= 0.001f)
    return c;
  k = std::clamp(k, 0.0f, 1.0f);
  ImVec4 v = ImGui::ColorConvertU32ToFloat4(c);
  v.x += (palette.panelBg[0] - v.x) * k;
  v.y += (palette.panelBg[1] - v.y) * k;
  v.z += (palette.panelBg[2] - v.z) * k;
  return ImGui::ColorConvertFloat4ToU32(v);
}

// Depth ramp bounds for the 3D atmospheric pass. 0.75 is a ceiling, not taste:
// at 1.0 the farthest nodes dissolve fully into the bed and the graph reads as
// though it lost data.
constexpr float kHazeMax = 0.75f;
// Bloom is reserved for the front slice of the depth range — applied to the
// whole frame it stops meaning "nearest" and becomes a uniform glow.
constexpr float kBloomFront = 0.20f;
// Screen radius at which a containment bubble earns the five-shell volumetric
// treatment instead of the flat fill + outline.
constexpr float kBubbleVolumeR = 40.0f;

// Painter's-algorithm draw order. visibleIdx is sorted by node id, so in 3D a
// node hundreds of units BEHIND another painted over it purely because its id
// sorted later. persp is a monotone depth proxy (bigger = nearer), so
// back-to-front is ascending persp and the sort key costs nothing.
//
// 2D skips the sort deliberately: every persp is exactly 1.0 there, and
// std::sort would scramble the deterministic id order that label-overlap
// behaviour depends on.
void buildDrawOrder(GraphTabState &G, const Proj3 &P, float &perspMin,
                    float &perspMax) {
  perspMin = 1.0f;
  perspMax = 1.0f;
  G.drawOrder.assign(G.visibleIdx.begin(), G.visibleIdx.end());
  if (!G.view3d)
    return;
  if (G.perspOf.size() != G.nodes.size())
    G.perspOf.assign(G.nodes.size(), 1.0f);
  bool first = true;
  for (int vi : G.visibleIdx) {
    const GNode &n = G.nodes[vi];
    float px, py, persp;
    projectPoint(G, P, n.lx, n.ly, n.lz, px, py, persp);
    G.perspOf[static_cast<size_t>(vi)] = persp;
    if (first) {
      perspMin = perspMax = persp;
      first = false;
    } else {
      perspMin = std::min(perspMin, persp);
      perspMax = std::max(perspMax, persp);
    }
  }
  std::sort(G.drawOrder.begin(), G.drawOrder.end(), [&G](int a, int b) {
    return G.perspOf[static_cast<size_t>(a)] < G.perspOf[static_cast<size_t>(b)];
  });
}

// Straight U32 lerp in linear-ish sRGB — good enough for tinting one colour
// toward another over an animation, and it keeps the alpha channel honest.
ImU32 mixCol(ImU32 a, ImU32 b, float t) {
  t = std::clamp(t, 0.0f, 1.0f);
  const ImVec4 x = ImGui::ColorConvertU32ToFloat4(a);
  const ImVec4 y = ImGui::ColorConvertU32ToFloat4(b);
  return ImGui::ColorConvertFloat4ToU32(
      ImVec4(x.x + (y.x - x.x) * t, x.y + (y.y - x.y) * t,
             x.z + (y.z - x.z) * t, x.w + (y.w - x.w) * t));
}

ImVec2 lerpPt(ImVec2 a, ImVec2 b, float t) {
  return ImVec2(a.x + (b.x - a.x) * t, a.y + (b.y - a.y) * t);
}

// Hops are one kStagger apart by default (20 ms) — far too quick to read as
// propagation across a dependency chain, so each hop is widened to ~8 of them.
constexpr int kBlastHopStride = 8;
// Above this many impact-internal edges the travelling heads stop reading as a
// wave and start reading as flicker, so the wave animates the nodes only.
constexpr int kBlastEdgeAnimMax = 400;

// Hop distance from the blast epicentre along the VISIBLE edges. The impact
// wave travels this, one hop at a time, so the animation shows the dependency
// path the change actually propagates down rather than a circle expanding over
// unrelated geometry. Rebuilt only when the blast or the visible set changes.
void rebuildBlastHops(GraphTabState &G) {
  G.blastHopDirty = false;
  G.blastHop.clear();
  G.blastEdgeAnim = false;
  if (G.blastEpicenter.empty() || G.visibleEdges.empty())
    return;
  // Adjacency over IMPACT-INTERNAL edges only. The wave must trace the
  // dependency path the change propagates down, so an edge leaving the impact
  // set is not part of it — and restricting the walk here yields the animatable
  // edge count for free.
  std::unordered_map<std::string, std::vector<const std::string *>> adj;
  adj.reserve(G.blast.size() * 2);
  int impactEdges = 0;
  for (const auto &e : G.visibleEdges) {
    if (!G.blast.count(e.src) || !G.blast.count(e.dst))
      continue;
    adj[e.src].push_back(&e.dst);
    adj[e.dst].push_back(&e.src);
    ++impactEdges;
  }
  G.blastEdgeAnim = impactEdges > 0 && impactEdges <= kBlastEdgeAnimMax;
  std::vector<const std::string *> q;
  q.reserve(G.blast.size() + 1);
  G.blastHop[G.blastEpicenter] = 0;
  q.push_back(&G.blastEpicenter);
  for (size_t qi = 0; qi < q.size(); ++qi) {
    const std::string &u = *q[qi];
    const int d = G.blastHop[u];
    auto ait = adj.find(u);
    if (ait == adj.end())
      continue;
    for (const std::string *v : ait->second) {
      auto ins = G.blastHop.emplace(*v, d + 1);
      if (ins.second)
        q.push_back(&ins.first->first);
    }
  }
}

int blastHopOf(const GraphTabState &G, const std::string &id) {
  auto it = G.blastHop.find(id);
  return it == G.blastHop.end() ? 0 : it->second;
}

// Vault lifecycle → rim tone. Released reads healthy, In work reads "this is
// moving under you", anything terminal (Superseded / Obsolete) recedes.
bool lifecycleRim(const std::string &lifecycle, float alpha, ImU32 &out) {
  if (lifecycle.empty())
    return false;
  if (lifecycle == "Released")
    out = ui::kit::toneU32(ui::kit::Tone::Ok, alpha);
  else if (lifecycle == "In work")
    out = ui::kit::toneU32(ui::kit::Tone::Warn, alpha);
  else
    out = ImGui::GetColorU32(ImVec4(palette.textDisabled[0],
                                    palette.textDisabled[1],
                                    palette.textDisabled[2], 0.55f * alpha));
  return true;
}

bool matchesSearch(const GNode &n, const char *q) {
  if (!q || !q[0])
    return true;
  // case-insensitive substring on label / kind / id / path / owning project /
  // keyword tags
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
  if (has(n.label) || has(n.kind) || has(n.id) || has(n.path) ||
      has(n.metaProject))
    return true;
  for (const auto &kw : n.metaKeywords)
    if (has(kw))
      return true;
  return false;
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
  if (G.matchQuery != G.search) {
    G.matchQuery = G.search;
    G.matchT0 = ImGui::GetTime();
  }
}

// ---- legend -----------------------------------------------------------------
// Row model shared by the builder and the painter, so the card's height is
// DERIVED from the rows it will actually paint. The previous canvas painter
// offset itself by a hardcoded 232 px that clipped or left dead space the
// moment a row was added or removed.
struct LegendRow {
  const char *label;
  ImU32 col;
  bool dashed;
  bool isNode;
  bool header;
};

std::vector<LegendRow> legendRows(bool projectScope) {
  std::vector<LegendRow> rows;
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
    rows.push_back({"file", kindColor("file"), false, true, false});
    rows.push_back({"keyword", kindColor("keyword"), false, true, false});
    rows.push_back(
        {"(agg \xc3\x97N)", edgeColor("REFERENCES", 0.85f), false, false,
         false});
  } else {
    rows.push_back({"feature", kindColor("feature"), false, true, false});
    rows.push_back({"sketch", kindColor("sketch"), false, true, false});
    rows.push_back({"equation", kindColor("equation"), false, true, false});
    rows.push_back({"mate", kindColor("mate"), false, true, false});
    rows.push_back({"body", kindColor("body"), false, true, false});
  }
  return rows;
}

// Collapsible legend pinned bottom-left INSIDE the canvas. Hosted in its own
// child window so its widgets win the hit test over the canvas InvisibleButton
// underneath (an overlapping item in the SAME window would never be hoverable).
void drawLegendCard(GraphTabState &G, ImVec2 canvasOrigin, ImVec2 canvasSz,
                    bool projectScope) {
  const float inset = ui::metrics::U(3.0f);
  const float lineH = ImGui::GetTextLineHeight();
  const float iconH = ui::metrics::iconHeader();

  if (!G.legendOpen) {
    const float chip = ImGui::GetFrameHeight();
    ImGui::SetCursorScreenPos(
        ImVec2(canvasOrigin.x + inset,
               canvasOrigin.y + canvasSz.y - inset - chip));
    ImGui::BeginChild("##legend_chip", ImVec2(chip, chip), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar |
                          ImGuiWindowFlags_NoScrollWithMouse |
                          ImGuiWindowFlags_NoBackground);
    if (ui::kit::iconButton("##legend_show", "legend",
                            "Colour key for node kinds and link origins"))
      G.legendOpen = true;
    ImGui::EndChild();
    return;
  }

  const std::vector<LegendRow> rows = legendRows(projectScope);
  const float swatchW = ui::metrics::U(5.0f);
  const float gap = ui::metrics::U(1.5f);
  const float rowH = lineH + ui::metrics::U(0.5f);
  const float step = rowH + ImGui::GetStyle().ItemSpacing.y;

  float contentW = lineH + gap + iconH + gap + ImGui::CalcTextSize("Legend").x;
  for (const auto &r : rows)
    contentW = std::max(contentW,
                        swatchW + gap + ImGui::CalcTextSize(r.label).x);
  const float cardW = contentW + ui::kit::padX() * 2.0f +
                      ui::metrics::stroke(1.0f) * 2.0f + ui::metrics::U(1.0f);
  const float cardH = ui::kit::padY() * 2.0f + std::max(lineH, iconH) +
                      ui::kit::headGap() + step * (float)rows.size() +
                      ui::metrics::stroke(1.0f) * 2.0f;

  ImGui::SetCursorScreenPos(ImVec2(canvasOrigin.x + inset,
                                   canvasOrigin.y + canvasSz.y - inset - cardH));
  ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0.0f, 0.0f));
  ImGui::BeginChild("##legend_host", ImVec2(cardW, cardH), ImGuiChildFlags_None,
                    ImGuiWindowFlags_NoScrollbar |
                        ImGuiWindowFlags_NoScrollWithMouse |
                        ImGuiWindowFlags_NoBackground);
  ImGui::PopStyleVar();
  if (ui::kit::cardBegin("##legend", "Legend", "legend", &G.legendOpen)) {
    ImDrawList *dl = ImGui::GetWindowDrawList();
    const ImU32 tdim = Palette::toU32(palette.textDim);
    const ImU32 tdis = Palette::toU32(palette.textDisabled);
    for (const auto &r : rows) {
      const ImVec2 p = ImGui::GetCursorScreenPos();
      const float cy = p.y + rowH * 0.5f;
      float tx = p.x;
      if (!r.header) {
        tx += swatchW + gap;
        if (r.isNode) {
          dl->AddCircleFilled(ImVec2(p.x + swatchW * 0.5f, cy),
                              ui::metrics::U(1.25f), r.col);
          dl->AddCircle(ImVec2(p.x + swatchW * 0.5f, cy),
                        ui::metrics::U(1.25f),
                        ImGui::GetColorU32(ImVec4(palette.border[0],
                                                  palette.border[1],
                                                  palette.border[2], 0.9f)),
                        0, ui::metrics::stroke(1.0f));
        } else if (r.dashed) {
          drawDashedLine(dl, ImVec2(p.x, cy), ImVec2(p.x + swatchW, cy), r.col,
                         ui::metrics::stroke(1.5f), ui::metrics::U(1.0f),
                         ui::metrics::U(0.75f));
        } else {
          dl->AddLine(ImVec2(p.x, cy), ImVec2(p.x + swatchW, cy), r.col,
                      ui::metrics::stroke(1.5f));
        }
      }
      dl->AddText(ImVec2(tx, p.y + (rowH - lineH) * 0.5f),
                  r.header ? tdis : tdim, r.label);
      ImGui::Dummy(ImVec2(tx - p.x + ImGui::CalcTextSize(r.label).x, rowH));
    }
  }
  ui::kit::cardEnd();
  ImGui::EndChild();
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
    // Elide + tooltip, never TextWrapped: link ids and paths carry no
    // whitespace and break mid-token inside a stretch column.
    ui::kit::keyValueRow(k, v.c_str());
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
      const std::string linkId =
          srcE->meta.is_object() ? srcE->meta.value("link_id", std::string())
                                 : std::string();
      // Irreversible (it rewrites .plm/graph_links.json) and it used to sit
      // one row below "Copy path" in identical styling. The confirmation names
      // BOTH endpoints. The Delete key arms the same popup: kit::popoverBegin
      // opens "##kit_popover_body" under PushID(id), so this matches it.
      ImGui::PushID("##rmlink");
      if (G.linkRemoveConfirm) {
        ImGui::OpenPopup("##kit_popover_body");
        G.linkRemoveConfirm = false;
      }
      ImGui::PopID();
      if (ui::kit::popoverBegin("##rmlink", "Remove link\xe2\x80\xa6",
                                "delete")) {
        const std::string ask = "Delete the authored " + ve.kind +
                                " link \"" + nodeLabel(ve.src) + "\" \xe2\x80\x94 \"" +
                                nodeLabel(ve.dst) +
                                "\"? This rewrites .plm/graph_links.json and "
                                "cannot be undone.";
        ui::kit::dimText(ask.c_str());
        ui::kit::gapRow();
        if (ui::kit::dangerButton("Remove link", !linkId.empty(),
                                  "This link carries no link_id to remove.")) {
          requestLinkRemove(st, linkId);
          ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine(0, ui::metrics::U(1.0f));
        if (ui::kit::secondaryButton("Cancel"))
          ImGui::CloseCurrentPopup();
        ui::kit::popoverEnd();
      }
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

  const GNode *sel = nullptr;
  if (!G.selectedId.empty()) {
    auto it = G.idIndex.find(G.selectedId);
    if (it != G.idIndex.end())
      sel = &G.nodes[it->second];
  }

  if (!sel) {
    // A bare accent "Node" title over two lines of hint read as a broken pane.
    if (!G.linkArmSrc.empty())
      ui::kit::emptyState("isolate", "Pick a link target",
                          "Click the node to link to \xe2\x80\x94 or press Esc "
                          "to cancel.");
    else if (G.scopeDocId.empty())
      ui::kit::emptyState("isolate", "Nothing selected",
                          "Click a node to inspect it. Double-click a part or "
                          "document to isolate its references.");
    else
      ui::kit::emptyState("isolate", "Nothing selected",
                          "Click a node to inspect it, or a link to see its "
                          "properties. Backspace goes back a scope.");
    ImGui::EndChild();
    ImGui::PopStyleVar();
    return;
  }

  plmSectionHeader("Node", "mesh");
  ImGui::Dummy(ImVec2(0, 6 * S));

  // ---- header: kind pill + title -----------------------------------------
  {
    ImDrawList *dl = ImGui::GetWindowDrawList();
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const char *ks = kindShort(sel->kind);
    const ImVec2 ksSz = ImGui::CalcTextSize(ks);
    const float pillH = ImGui::GetTextLineHeight() + 4 * S;
    const float pillW = ksSz.x + 12 * S;
    const ImVec4 fill = ImGui::ColorConvertU32ToFloat4(
        kindColor(sel->kind, 0.85f));
    dl->AddRectFilled(p0, ImVec2(p0.x + pillW, p0.y + pillH),
                      ImGui::GetColorU32(fill), 6.0f * S);
    dl->AddText(ImVec2(p0.x + 6 * S, p0.y + 2 * S),
                ImGui::GetColorU32(ui::maxContrastTextOn(fill)), ks);
    ImGui::Dummy(ImVec2(pillW + 8 * S, pillH));
    ImGui::SameLine(0, 0);
    ImGui::PushStyleColor(
        ImGuiCol_Text,
        ImVec4(palette.text[0], palette.text[1], palette.text[2], 1.0f));
    ImGui::SetCursorPosY(ImGui::GetCursorPosY() + 2 * S);
    ImGui::TextWrapped("%s", sel->label.c_str());
    ImGui::PopStyleColor();
  }

  // ---- vault identity: part number, lifecycle, suppression ----------------
  // A Released, part-numbered component and a scratch experiment were
  // textually identical everywhere in this tab.
  if (!sel->metaPartNumber.empty() || !sel->metaLifecycle.empty() ||
      sel->metaSuppressed) {
    ImGui::Dummy(ImVec2(0, 4 * S));
    bool first = true;
    if (!sel->metaPartNumber.empty()) {
      ui::kit::valueChip("PN", sel->metaPartNumber.c_str(),
                         ui::kit::Tone::Info,
                         sel->metaItemId.empty()
                             ? nullptr
                             : ("vault item " + sel->metaItemId).c_str());
      first = false;
    }
    if (!sel->metaLifecycle.empty()) {
      if (!first)
        ImGui::SameLine(0, ui::metrics::U(1.0f));
      const ui::kit::Tone lct = sel->metaLifecycle == "Released"
                                    ? ui::kit::Tone::Ok
                                    : (sel->metaLifecycle == "In work"
                                           ? ui::kit::Tone::Warn
                                           : ui::kit::Tone::Neutral);
      ui::kit::chip(sel->metaLifecycle.c_str(), lct, /*dot=*/true,
                    "Vault lifecycle state (meta.lifecycle).");
      first = false;
    }
    if (sel->metaSuppressed) {
      if (!first)
        ImGui::SameLine(0, ui::metrics::U(1.0f));
      ui::kit::chip("Suppressed", ui::kit::Tone::Warn, /*dot=*/true,
                    "Suppressed in the model. Downstream DEPENDS_ON edges "
                    "still exist, which is what makes a rebuild behave "
                    "inexplicably.");
    }
  }

  ImGui::Dummy(ImVec2(0, 8 * S));

  // Resolve open path once for preview + actions. Instance nodes resolve to
  // the doc they instance (otherwise everything opens/renders the assembly).
  std::string absPath;
  const GNode *instTgt = instanceTarget(G, *sel);
  const GNode &openNode = instTgt ? *instTgt : *sel;
  if ((isDrillableKind(openNode.kind) || openNode.kind == "project" ||
       openNode.kind == "file") &&
      !openNode.path.empty())
    absPath = resolveNodeAbsPath(st, openNode);
  else if (!openNode.docId.empty()) {
    auto dit = G.idIndex.find(openNode.docId);
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

    // Primary: worker-rendered iso thumb (content-hash cache, same pipeline
    // as the grid). Fallback: any thumbnail.png packaged inside the .step+.
    // file nodes skip both — a pdf/image has no CAD render and no package
    // thumbnail; the empty state (or the text_preview block below) covers it.
    GraphGridThumb gth;
    if (!absPath.empty() && sel->kind != "file") {
      const std::string tkey =
          graphThumbKey(openNode.metaDh, openNode.metaMt, absPath);
      gth = graphThumbEnsure(st, G.projectDir, tkey, absPath);
    }
    const GraphThumb *th = nullptr;
    if (!gth.tex && !absPath.empty() && sel->kind != "file")
      th = ensureGraphThumb(absPath);
    // Also try the node's own path if different from open target.
    if (!gth.tex && !th && !sel->path.empty() && sel->kind != "file") {
      const std::string own = resolveNodeAbsPath(st, *sel);
      if (!own.empty() && own != absPath)
        th = ensureGraphThumb(own);
    }

    if (gth.tex || (th && th->tex)) {
      const GLuint tex = gth.tex ? gth.tex : th->tex;
      float tw = (float)(gth.tex ? gth.w : th->w),
            thh = (float)(gth.tex ? gth.h : th->h);
      const float pad = 10.0f * S;
      const float maxW = previewW - pad * 2, maxH = previewH - pad * 2;
      float sc = std::min(maxW / std::max(tw, 1.f), maxH / std::max(thh, 1.f));
      tw *= sc;
      thh *= sc;
      const ImVec2 ip0(p0.x + (previewW - tw) * 0.5f,
                       p0.y + (previewH - thh) * 0.5f);
      dl->AddImage((ImTextureID)(intptr_t)tex, ip0,
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
      if (gth.pending) {
        // A render is queued/in flight — the brand mark above the hint. Placed
        // by cursor because ui::kit::spinner is the app-wide busy indicator.
        const ImVec2 keep = ImGui::GetCursorScreenPos();
        const float box = ImGui::GetTextLineHeight();
        ImGui::SetCursorScreenPos(
            ImVec2(p0.x + previewW * 0.5f - box * 0.5f,
                   p0.y + previewH * 0.20f - box * 0.5f));
        ui::kit::spinner("");
        ImGui::SetCursorScreenPos(keep);
      }
    }
    ImGui::Dummy(ImVec2(previewW, previewH));
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal) && !absPath.empty())
      ImGui::SetTooltip("%s", absPath.c_str());
  }

  ImGui::Dummy(ImVec2(0, 10 * S));

  // ---- metric strip (degree + hidden descendants + instance count) --------
  {
    // Degree comes from the index rebuilt with the data, not from an O(edges)
    // rescan on every frame the inspector is open.
    ensureEdgeIndex(G);
    const auto dit = G.degree.find(sel->id);
    const int outE = dit == G.degree.end() ? 0 : dit->second.first;
    const int inE = dit == G.degree.end() ? 0 : dit->second.second;
    ui::kit::valueChip("out", ui::kit::fmtCount(outE).c_str(),
                       ui::kit::Tone::Accent, "Outgoing links.");
    ImGui::SameLine(0, ui::metrics::U(1.0f));
    ui::kit::valueChip("in", ui::kit::fmtCount(inE).c_str(),
                       ui::kit::Tone::Accent, "Incoming links.");
    if (G.scopeDocId.empty()) {
      auto cit = G.descCounts.find(sel->id);
      if (cit != G.descCounts.end() && cit->second.total() > 0) {
        ImGui::SameLine(0, ui::metrics::U(1.0f));
        ui::kit::valueChip("inside",
                           ui::kit::fmtCount(cit->second.total()).c_str(),
                           ui::kit::Tone::Info,
                           formatDescBadge(cit->second).c_str());
      }
    }
    if (G.scopeDecomp) {
      auto qit = G.instCount.find(sel->id);
      if (qit != G.instCount.end() && qit->second > 0) {
        ImGui::SameLine(0, ui::metrics::U(1.0f));
        ui::kit::valueChip("qty", ui::kit::fmtCount(qit->second).c_str(),
                           ui::kit::Tone::Neutral, "Instances in this scope.");
      }
    }
  }

  // Path row — a Feature-Manager-style row (icon vertically centered with the
  // filename); clicking it reveals + highlights the file in the OS file
  // manager. Full-width, hover-lit, like an FM feature entry.
  const std::string abs =
      absPath.empty() ? resolveNodeAbsPath(st, *sel) : absPath;
  if (!sel->path.empty()) {
    ImGui::Dummy(ImVec2(0, 6 * S));
    const float rowH = ImGui::GetFrameHeight();
    const float rowW = ImGui::GetContentRegionAvail().x;
    const ImVec2 rp0 = ImGui::GetCursorScreenPos();
    const bool rowClick = ImGui::InvisibleButton("##path_row", ImVec2(rowW, rowH));
    const bool rh = ImGui::IsItemHovered();
    ImDrawList *dl = ImGui::GetWindowDrawList();
    if (rh)
      dl->AddRectFilled(
          rp0, ImVec2(rp0.x + rowW, rp0.y + rowH),
          ImGui::GetColorU32(
              ImVec4(palette.text[0], palette.text[1], palette.text[2], 0.08f)),
          6.0f * S);
    const float icoR = 15.0f * S;
    const float padX = 6.0f * S;
    ui::drawIconGlass(dl, "body_folder",
                      ImVec2(rp0.x + padX + icoR * 0.5f, rp0.y + rowH * 0.5f),
                      icoR, Palette::toU32(rh ? palette.accent : palette.textDim),
                      0);
    // Basename, vertically centered with the icon; clipped to the row width.
    std::string base = sel->path;
    if (auto slash = base.find_last_of("/\\"); slash != std::string::npos)
      base = base.substr(slash + 1);
    const float txX = rp0.x + padX + icoR + 6.0f * S;
    const float txAvail = rp0.x + rowW - txX - padX;
    std::string shown = base;
    while (shown.size() > 4 &&
           ImGui::CalcTextSize(("…" + shown).c_str()).x > txAvail)
      shown.erase(shown.begin());
    if (shown != base)
      shown = "…" + shown;
    const ImVec2 tsz = ImGui::CalcTextSize(shown.c_str());
    dl->AddText(ImVec2(txX, rp0.y + (rowH - tsz.y) * 0.5f),
                Palette::toU32(rh ? palette.text : palette.textDim),
                shown.c_str());
    if (rowClick && !abs.empty())
      ui::revealInFileManager(abs);
    if (rh)
      ImGui::SetTooltip("Show in file manager\n%s",
                        abs.empty() ? sel->path.c_str() : abs.c_str());
  }

  // ---- details: a tight 2-column table (no tree nesting / left indent) -----
  plmSectionHeader("Details", "bom");
  ImGui::Dummy(ImVec2(0, 4 * S));
  {
    // units string, upper-cased for display (ips -> IPS).
    std::string units = sel->metaUn;
    for (char &c : units)
      c = (char)std::toupper((unsigned char)c);
    // materials joined, clipped so a big assembly can't blow up the row.
    std::string mats;
    for (size_t i = 0; i < sel->metaMat.size() && i < 6; ++i) {
      if (!mats.empty())
        mats += ", ";
      mats += sel->metaMat[i];
    }
    if (sel->metaMat.size() > 6)
      mats += " +" + std::to_string(sel->metaMat.size() - 6);

    // One row painter for the whole table: keyValueRow elides its value and
    // tooltips the full string, which is exactly what an unbroken id or docId
    // needs inside a 260 px column.
    auto trow = [&](const char *k, const std::string &v, bool copyable) {
      if (v.empty())
        return;
      ui::kit::keyValueRow(k, v.c_str(), ui::kit::Tone::Neutral, copyable);
    };
    trow("units", units, false);
    trow("material", mats, false);
    trow("origin", sel->origin, false);
    trow("lifecycle", sel->metaLifecycle, false);
    trow("part number", sel->metaPartNumber, true);
    trow("item id", sel->metaItemId, true);
    if (!sel->metaProject.empty() && G.projects.size() > 1) {
      std::string projBase = sel->metaProject;
      if (auto sl = projBase.find_last_of("/\\"); sl != std::string::npos)
        projBase = projBase.substr(sl + 1);
      trow("project", projBase, false);
    }
    if (!sel->docId.empty() && sel->docId != sel->id)
      trow("document", sel->docId, true);
    trow("id", sel->id, true);
  }

  // ---- file text preview (meta.text_preview, <=300 chars) ------------------
  if (sel->kind == "file" && !sel->metaPreview.empty()) {
    ImGui::Dummy(ImVec2(0, 4 * S));
    plmSectionHeader("Preview", "new_doc");
    ImGui::Dummy(ImVec2(0, 4 * S));
    ImGui::PushStyleColor(ImGuiCol_Text,
                          ImVec4(palette.textDim[0], palette.textDim[1],
                                 palette.textDim[2], 1.0f));
    ImGui::TextWrapped("%s", sel->metaPreview.c_str());
    ImGui::PopStyleColor();
    ImGui::Dummy(ImVec2(0, 6 * S));
  }

  // ---- keywords (meta.keywords chips + add/remove) -------------------------
  ImGui::Dummy(ImVec2(0, 4 * S));
  plmSectionHeader("Keywords", "bom");
  ImGui::Dummy(ImVec2(0, 4 * S));
  if (sel->metaKeywords.empty()) {
    ImGui::PushStyleColor(ImGuiCol_Text,
                          ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                                 palette.textDisabled[2], 1.0f));
    ImGui::TextWrapped("No keywords yet.");
    ImGui::PopStyleColor();
  } else {
    // Flow-wrap chips: measured SameLine rows, click a chip's × to untag.
    const float availW = ImGui::GetContentRegionAvail().x;
    float rowX = 0.0f;
    for (int ki = 0; ki < (int)sel->metaKeywords.size(); ++ki) {
      const std::string &kw = sel->metaKeywords[ki];
      const std::string chip = kw + "  \xc3\x97";
      const float chipW = ImGui::CalcTextSize(chip.c_str()).x +
                          ImGui::GetStyle().FramePadding.x * 2.0f;
      if (rowX > 0.0f && rowX + chipW > availW)
        rowX = 0.0f;
      if (rowX > 0.0f)
        ImGui::SameLine(0, 4 * S);
      ImGui::PushID(ki);
      ImGui::PushStyleColor(
          ImGuiCol_Text,
          ImGui::ColorConvertU32ToFloat4(kindColor("keyword", 1.0f)));
      if (ImGui::SmallButton(chip.c_str()))
        requestKeywordRemove(st, sel->id, kw);
      ImGui::PopStyleColor();
      if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal))
        ImGui::SetTooltip("Remove keyword \"%s\"", kw.c_str());
      ImGui::PopID();
      rowX += chipW + 4 * S;
    }
  }
  ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x);
  const bool kwEnter = ImGui::InputTextWithHint(
      "##kw_add", "add a keyword, Enter to tag", G.keywordAdd,
      sizeof(G.keywordAdd), ImGuiInputTextFlags_EnterReturnsTrue);
  const bool kwBtn = graphGlassButton("##kw_add_btn", "Add keyword", false,
                                      -1.0f);
  if ((kwEnter || kwBtn) && G.keywordAdd[0]) {
    std::string kw = G.keywordAdd;
    const auto b = kw.find_first_not_of(" \t");
    const auto e = kw.find_last_not_of(" \t");
    kw = (b == std::string::npos) ? std::string() : kw.substr(b, e - b + 1);
    if (!kw.empty()) {
      requestKeywordAdd(st, sel->id, kw);
      G.keywordAdd[0] = 0;
    }
  }

  ImGui::Dummy(ImVec2(0, 10 * S));
  ImGui::Separator();
  ImGui::Dummy(ImVec2(0, 8 * S));

  // ---- actions ------------------------------------------------------------
  // Open document: launch in its native editor. requestOpenPath routes by
  // extension in app.cpp's pump, and doc_tabs auto-selects the matching
  // workspace on the switch (Drawing->Drawing, board->Electronics, part/asm->
  // Model, sim->Simulate). Focus Document: stay in PLM and center the graph
  // on the resolved doc node.
  if (!absPath.empty()) {
    if (sel->kind == "file") {
      // Non-CAD asset: hand to the OS, never the doc parser.
      if (graphGlassButton("##open_ext", "Open externally", true, -1.0f,
                           "new_doc"))
        ui::openExternalFile(absPath);
      if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal))
        ImGui::SetTooltip("Open with the system default application.");
      ImGui::Dummy(ImVec2(0, 4 * S));
      if (graphGlassButton("##reveal_file", "Reveal in folder", false, -1.0f,
                           "body_folder"))
        ui::revealInFileManager(absPath);
      if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal))
        ImGui::SetTooltip("Show in the file manager.");
      ImGui::Dummy(ImVec2(0, 4 * S));
    } else {
      if (graphGlassButton("##open_doc", "Open document", true, -1.0f,
                           "new_doc"))
        st.requestOpenPath = absPath;
      if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal))
        ImGui::SetTooltip("Open in its editor (Model / Drawing / Simulate /\n"
                          "Electronics / Manufacture, by document kind).");
      ImGui::Dummy(ImVec2(0, 4 * S));
      if (graphGlassButton("##focus_doc", "Focus document", false, -1.0f,
                           "view_selector")) {
        G.selectedId = openNode.id;
        G.selectedEdge = -1;
        G.pendingCenterId = openNode.id;
      }
      if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal))
        ImGui::SetTooltip("Center this document in the PLM graph.");
      ImGui::Dummy(ImVec2(0, 4 * S));
    }
    if (graphGlassButton("##copy_path", "Copy path", false, -1.0f, nullptr))
      ImGui::SetClipboardText(absPath.c_str());
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

// RAII guard around an ImDrawListSplitter, used by the search spotlight to lift
// matches above one even scrim. Merging is the whole point: an unmerged splitter
// does not assert, it silently corrupts every later primitive in the window, so
// the destructor merges if the caller has not and no early return can leak one.
// A null draw list makes the whole guard inert, which is how the not-filtering
// path pays nothing.
class ChannelSplit {
public:
  ChannelSplit(ImDrawList *dl, int channels) : dl_(dl) {
    if (!dl_)
      return;
    split_.Split(dl_, channels);
    active_ = true;
  }
  ChannelSplit(const ChannelSplit &) = delete;
  ChannelSplit &operator=(const ChannelSplit &) = delete;
  ~ChannelSplit() { merge(); }
  void setChannel(int c) {
    if (active_)
      split_.SetCurrentChannel(dl_, c);
  }
  void merge() {
    if (!active_)
      return;
    active_ = false;
    split_.Merge(dl_);
  }

private:
  ImDrawList *dl_ = nullptr;
  ImDrawListSplitter split_;
  bool active_ = false;
};

void drawCanvas(AppState &st, ImVec2 canvasSz) {
  auto &G = S();
  const float S = ui::uiScale;
  // st used for path resolution / hover thumbs

  ImGui::InvisibleButton("##graph_canvas", canvasSz,
                         ImGuiButtonFlags_MouseButtonLeft |
                             ImGuiButtonFlags_MouseButtonMiddle |
                             ImGuiButtonFlags_MouseButtonRight);
  const bool canvasHovered = ImGui::IsItemHovered();
  const ImVec2 origin = ImGui::GetItemRectMin();

  // The 2D|3D state and its gesture bindings now live in the toolbar (the
  // segmented control and the persistent hint line), so no widget floats over
  // the canvas and nothing has to be subtracted from the canvas hover.
  const bool hovered = canvasHovered;
  ImDrawList *dl = ImGui::GetWindowDrawList();
  dl->PushClipRect(origin, ImVec2(origin.x + canvasSz.x, origin.y + canvasSz.y),
                   true);

  // Frosted canvas bed.
  ui::glassBackground(
      dl, origin, ImVec2(origin.x + canvasSz.x, origin.y + canvasSz.y), 10.0f);

  if (G.layoutDirty)
    rebuildForce(G);

  // 3D depth normalization: keep the deepest node within the focal frustum so
  // perspective never clamps/explodes (the "3D stopped rendering / can't fit"
  // regression). Cheap O(visible); only meaningful in 3D.
  if (G.view3d)
    updateZScale(G);
  else
    G.zScale = 1.0f;

  // Fit ONLY on explicit request (Fit button). Polls/relayouts never refit.
  if (G.fitRequested && !G.visibleIdx.empty()) {
    fitView(G, canvasSz);
    G.fitRequested = false;
  }

  // Focus Document: center the view on a specific node once its layout is
  // resolved. Zoom in a touch if the current view is very zoomed out.
  if (!G.pendingCenterId.empty()) {
    auto it = G.idIndex.find(G.pendingCenterId);
    if (it != G.idIndex.end()) {
      if (G.zoom < 0.6f)
        G.zoom = 0.9f;
      centerOnNode(G, G.nodes[it->second], canvasSz);
    }
    G.pendingCenterId.clear();
  }

  // Advance physics and copy graph-space positions back into the render
  // cache. Frozen when settled unless a node is being dragged — idle
  // frames cost nothing but the draw itself. While active we run extra
  // substeps per frame so the visible settle takes ~2s, not ~6s.
  // 2D<->3D toggle transition: lerp from->to (smoothstep 0.35 s), skipping
  // the sim while active so the motion reads cleanly.
  if (G.t3dStart >= 0.0) {
    const float t = (float)((ImGui::GetTime() - G.t3dStart) / 0.35);
    if (t >= 1.0f || G.dragNode >= 0) {
      G.trans3dFrom.clear();
      G.trans3dTo.clear();
      G.t3dStart = -1.0;
    } else {
      const float s = t * t * (3.0f - 2.0f * t);
      for (const auto &kv : G.trans3dFrom) {
        const auto to = G.trans3dTo.find(kv.first);
        const auto ni = G.idIndex.find(kv.first);
        if (to == G.trans3dTo.end() || ni == G.idIndex.end())
          continue;
        GNode &n = G.nodes[ni->second];
        n.lx = kv.second.x + (to->second.x - kv.second.x) * s;
        n.ly = kv.second.y + (to->second.y - kv.second.y) * s;
        n.lz = kv.second.z + (to->second.z - kv.second.z) * s;
      }
    }
  }

  if (!G.visibleIdx.empty() && G.t3dStart < 0.0 &&
      (G.dragNode >= 0 || !G.force.settled())) {
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

  // Fit is NOT only a settle-transition concern, and treating it as one was a real bug: a WARM
  // layout cache restores node positions with the sim already settled, so the block above never
  // runs, the pending fit is never consumed, and the tab opens at pan=0 / zoom=1 with the layout
  // wherever the cache happened to leave it — i.e. clipped into the top-left corner, which is
  // exactly how the Graph tab has been presenting itself. Consume the request here instead, as
  // soon as there is something to frame and a canvas to frame it in. Writes camera targets only.
  if (G.fitOnSettle && !G.userNavigated && !G.visibleIdx.empty() &&
      canvasSz.x > 1.0f && canvasSz.y > 1.0f) {
    fitView(G, canvasSz);
    G.fitOnSettle = false;
    // Land the framing immediately on the first paint rather than easing in from the default
    // camera: an opening dolly from a wrong position reads as a glitch, not as polish.
    G.orbitYaw = G.orbitYawT;
    G.orbitPitch = G.orbitPitchT;
    G.zoom = G.zoomT;
    G.panX = G.panXT;
    G.panY = G.panYT;
    ui::anim::forget(ImGui::GetID("##plmg_zoom"));
    ui::anim::forget(ImGui::GetID("##plmg_panx"));
    ui::anim::forget(ImGui::GetID("##plmg_pany"));
    ui::anim::forget(ImGui::GetID("##plmg_yaw"));
    ui::anim::forget(ImGui::GetID("##plmg_pitch"));
  }

  // ---- camera: ONE authority ------------------------------------------------
  // Every input below writes the camera TARGET, and the tick at the end eases the
  // live camera there. That single rule is what makes Fit, fly-to, orbit throw and
  // idle drift read as a dolly instead of a jump cut — and it is why the inputs
  // must not touch the live values: two writers would fight every frame.
  // Nothing here goes anywhere near G.force or GNode::lx/ly/lz, so the frozen
  // hierarchy pack is untouched by all of it.

  // Zoom (wheel) around cursor. Solved against the LIVE camera so the point under
  // the pointer stays put during a fast scroll, then expressed as a target.
  if (hovered) {
    float wheel = ImGui::GetIO().MouseWheel;
    if (wheel != 0.0f) {
      G.userNavigated = true;
      G.lastInputT = ImGui::GetTime();
      ImVec2 mp = ImGui::GetIO().MousePos;
      float lx = (mp.x - origin.x - G.panX) / G.zoom;
      float ly = (mp.y - origin.y - G.panY) / G.zoom;
      G.zoomT =
          std::clamp(G.zoom * (wheel > 0 ? 1.12f : 1.0f / 1.12f), 0.12f, 6.0f);
      G.panXT = (mp.x - origin.x) - lx * G.zoomT;
      G.panYT = (mp.y - origin.y) - ly * G.zoomT;
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
    G.lastInputT = ImGui::GetTime();
    ImVec2 d = ImGui::GetIO().MouseDelta;
    // A drag must track the pointer 1:1, so it advances BOTH the target and the
    // live value: easing a pan would feel like dragging through treacle.
    G.panXT += d.x;
    G.panYT += d.y;
    G.panX += d.x;
    G.panY += d.y;
  }

  // Orbit: left-drag on empty background rotates the 3D projection
  // (view3d only; swapped with pan per CAD convention).
  const bool orbitDragNow =
      G.view3d && hovered && G.dragNode < 0 && G.dragCandidate < 0 &&
      G.hoverId.empty() && !ImGui::GetIO().KeyCtrl &&
      ImGui::IsMouseDragging(ImGuiMouseButton_Left);
  if (orbitDragNow) {
    G.userNavigated = true;
    G.lastInputT = ImGui::GetTime();
    ImVec2 d = ImGui::GetIO().MouseDelta;
    G.orbitYawT += d.x * 0.008f;
    G.orbitPitchT = std::clamp(G.orbitPitchT + d.y * 0.008f, -1.35f, 1.35f);
    G.orbitYaw = G.orbitYawT;      // 1:1 while held, same reason as pan
    G.orbitPitch = G.orbitPitchT;
    // Capture angular velocity for the throw. dt() is clamped, so a stall cannot
    // record an absurd fling.
    const float dt = ui::anim::dt();
    if (dt > 0.0f) {
      G.orbitVelYaw = d.x * 0.008f / dt;
      G.orbitVelPitch = d.y * 0.008f / dt;
    }
  }
  if (G.orbitDragging && !orbitDragNow && !G.view3d) {
    G.orbitVelYaw = G.orbitVelPitch = 0.0f;   // never throw a 2D view
  }
  G.orbitDragging = orbitDragNow;
  if (hovered && (ImGui::GetIO().MouseDelta.x != 0.0f ||
                  ImGui::GetIO().MouseDelta.y != 0.0f))
    G.lastInputT = ImGui::GetTime();

  tickCamera(G, origin, canvasSz, hovered);

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

  // Back-to-front paint order + this frame's depth range. Reuses two member
  // vectors, so the sort does not allocate at ME489 scale.
  float perspMin = 1.0f, perspMax = 1.0f;
  buildDrawOrder(G, P, perspMin, perspMax);
  const float perspSpan = std::max(perspMax - perspMin, 1e-4f);
  // 0 at the nearest node in frame, 1 at the farthest. Flat in 2D.
  auto depthT = [&](float persp) -> float {
    if (!G.view3d)
      return 0.0f;
    return std::clamp((perspMax - persp) / perspSpan, 0.0f, 1.0f);
  };
  auto hazeK = [&](float persp) { return depthT(persp) * kHazeMax; };

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

  // ---- Impact shockwave timing ---------------------------------------------
  // Light leaves the epicentre ALONG the edges, one hop at a time, so the
  // animation traces the dependency path the change actually propagates down.
  // The BFS runs once per blast (or per visible-set change), never per frame —
  // it needs visibleEdges, which does not exist when the impact RPC lands.
  const bool blastLive =
      !G.blast.empty() && G.blastT0 > 0.0 && !G.blastEpicenter.empty();
  if (blastLive && G.blastHopDirty)
    rebuildBlastHops(G);
  // 0 before the wave reaches this node, 1 once it has passed. stagger() pumps
  // frames while any hop is still moving and stops the moment all have landed.
  auto blastWave = [&](const std::string &id) -> float {
    return ui::anim::stagger(G.blastT0, blastHopOf(G, id) * kBlastHopStride,
                             ui::anim::kBase);
  };

  // ---- Search spotlight ----------------------------------------------------
  // Two channels: 0 is the world, 1 is one even scrim plus the matches that sit
  // above it. That replaces per-element alpha dimming — which was correct but
  // flat, and cost a branch on every edge and node — with a single rect, and it
  // means a match keeps its true colour instead of everything else losing its.
  ChannelSplit spot(filtering ? dl : nullptr, 2);
  if (filtering) {
    spot.setChannel(1);
    dl->AddRectFilled(
        origin, ImVec2(origin.x + canvasSz.x, origin.y + canvasSz.y),
        ImGui::GetColorU32(ImVec4(palette.panelBg[0], palette.panelBg[1],
                                  palette.panelBg[2], 0.55f)));
    spot.setChannel(0);
  }
  // Breathing amplitude for the match rings: full for ~4 s after the query
  // changes, then zero. A ring that pulses forever is a distraction AND it pins
  // the frame pump at 60 fps for as long as a query sits in the box.
  const float spotAmp =
      filtering ? 1.0f - std::clamp((float)((ImGui::GetTime() - G.matchT0) /
                                            4.0),
                                    0.0f, 1.0f)
                : 0.0f;

  // ---- Containment bubble outlines (under edges) --------------------------
  // Nested packing makes every parent a bubble of its contents; outline it
  // so assembly/part nesting reads at a glance. LOD-culled when tiny.
  for (int vi : G.drawOrder) {
    const GNode &n = G.nodes[vi];
    if (n.bubbleR <= n.radius + 1.0f)
      continue;
    float perspB = 1.0f;
    const ImVec2 c = nodeScreen(n, perspB);
    const float rb = n.bubbleR * perspB * G.zoom;
    if (rb < 10.0f)
      continue;
    const float k = hazeK(perspB);
    const ImU32 rim = hazeCol(kindColor(n.kind, 0.30f), k);
    if (rb < kBubbleVolumeR * S) {
      // Below this the shells land sub-pixel apart: a packed hierarchy of
      // hundreds of tiny bubbles would pay for the volume and show none of it.
      dl->AddCircleFilled(c, rb, hazeCol(kindColor(n.kind, 0.045f), k), 48);
      dl->AddCircle(c, rb, rim, 48, 1.5f);
      continue;
    }
    // Rim-weighted radial falloff. Nested fills accumulate toward the centre,
    // so brightening the rim means fading each inner shell further into the
    // bed instead of raising its alpha. Nested assemblies then stack as glass
    // shells rather than as overlapping outlines.
    static constexpr float kShellR[5] = {1.0f, 0.90f, 0.78f, 0.62f, 0.42f};
    static constexpr float kShellA[5] = {0.10f, 0.075f, 0.055f, 0.04f, 0.03f};
    static constexpr float kShellFade[5] = {0.0f, 0.45f, 0.70f, 0.86f, 0.96f};
    for (int s = 0; s < 5; ++s)
      dl->AddCircleFilled(
          c, rb * kShellR[s],
          hazeCol(kindColor(n.kind, kShellA[s]),
                  std::min(1.0f, k + kShellFade[s] * (1.0f - k))),
          48);
    dl->AddCircle(c, rb, rim, 48, 1.5f);
    // One upper-left specular arc — the cue that says "curved glass".
    dl->PathArcTo(c, rb * 0.94f, IM_PI * 1.06f, IM_PI * 1.52f, 20);
    dl->PathStroke(hazeCol(ImGui::GetColorU32(ImVec4(palette.glassHighlight[0],
                                                     palette.glassHighlight[1],
                                                     palette.glassHighlight[2],
                                                     0.32f)),
                           k),
                   ImDrawFlags_None, 1.6f * S);
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

    // Spotlight: an edge is lit only when BOTH endpoints match, so a half-lit
    // line never runs out of the spotlight and into the scrim. Lit edges go to
    // the channel above the scrim; everything else keeps its normal colour and
    // is dimmed uniformly by the scrim instead of per-element alpha.
    const bool edgeLit =
        !filtering ||
        (G.matchIds.count(a.id) != 0 && G.matchIds.count(b.id) != 0);
    if (filtering)
      spot.setChannel(edgeLit ? 1 : 0);
    bool incident = edgeIncident(e);
    bool selEdge = (ei == G.selectedEdge);
    bool authored = (e.origin == "manual" || e.origin == "ai");

    float alpha;
    if (selEdge)
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
    if (authored && (incident || selEdge || !incidenceDim))
      col =
          ImGui::GetColorU32(ImVec4(palette.accent[0], palette.accent[1],
                                    palette.accent[2], selEdge ? 1.0f : 0.85f));
    else if (incident && !G.blast.empty())
      col = ImGui::GetColorU32(ImVec4(palette.statusWarn[0],
                                      palette.statusWarn[1],
                                      palette.statusWarn[2], 0.90f));
    // A stale external reference outranks every other edge colour: it is the
    // one that silently produces wrong geometry downstream.
    if (e.stale)
      col = ui::kit::toneU32(ui::kit::Tone::Err, std::max(alpha, 0.80f));
    // Atmospheric perspective, from the midpoint depth of the two endpoints.
    col = hazeCol(col, hazeK(0.5f * (perspA + perspB)));

    if (!onScreen(p0) && !onScreen(p1))
      continue; // viewport cull
    float thick = (edgeLod   ? 1.0f
                   : selEdge ? 3.0f
                             : (incident ? 2.2f : 1.3f)) *
                  S;
    // Dashed = "this link is not a solid fact": inferred/AI-authored, an edge
    // leaving a suppressed feature, or a stale external ref. Stale dashes even
    // under edge LOD — it is rare and it is the diagnostic.
    const bool dashed =
        e.stale || (!edgeLod && (e.origin == "inferred" || e.origin == "ai" ||
                                 a.metaSuppressed));
    if (dashed)
      drawDashedLine(dl, p0, p1, col, thick, 7 * S, 4 * S);
    else
      dl->AddLine(p0, p1, col, thick);

    // Shockwave head: a bright segment running this edge during the hop that
    // reaches its FAR endpoint, so the light visibly leaves the epicentre and
    // travels outward instead of appearing everywhere at once.
    if (blastLive && G.blastEdgeAnim && G.blast.count(e.src) &&
        G.blast.count(e.dst)) {
      const int ha = blastHopOf(G, e.src), hb = blastHopOf(G, e.dst);
      if (ha != hb) {
        const float wv = ui::anim::stagger(
            G.blastT0, std::max(ha, hb) * kBlastHopStride, ui::anim::kBase);
        if (wv > 0.0f && wv < 1.0f) {
          const ImVec2 pn = (ha < hb) ? p0 : p1;
          const ImVec2 pf = (ha < hb) ? p1 : p0;
          const float tail = std::max(0.0f, wv - 0.35f);
          dl->AddLine(lerpPt(pn, pf, tail), lerpPt(pn, pf, wv),
                      ui::kit::toneU32(ui::kit::Tone::Warn, 0.95f), 3.0f * S);
        }
      }
    }

    // Dimensional / aggregation label at midpoint when zoomed in. Suppressed on
    // unlit edges: under the scrim it is unreadable noise.
    if (!edgeLod && edgeLit && alpha > 0.2f && G.zoom >= 0.8f) {
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
  if (filtering)
    spot.setChannel(0); // back to the world for the hit-test and blast flash

  // ---- Hit-test VISIBLE nodes ---------------------------------------------
  G.hoverId.clear();
  ImVec2 mp = ImGui::GetIO().MousePos;
  float bestD = 1e9f;
  if (hovered) {
    // The paint walks back-to-front, so picking walks the opposite way: in 3D
    // the first disc the cursor lands in is the one closest to the camera, and
    // it wins even if a node far behind has its centre nearer in screen space.
    // 2D keeps the original forward id-order min-distance scan verbatim, so
    // which node a click selects there is byte-for-byte unchanged.
    const std::vector<int> &hitSet = G.view3d ? G.drawOrder : G.visibleIdx;
    const int hitN = (int)hitSet.size();
    for (int k = 0; k < hitN; ++k) {
      const GNode &n = G.nodes[hitSet[static_cast<size_t>(
          G.view3d ? (hitN - 1 - k) : k)]];
      float persp = 1.0f;
      ImVec2 c = nodeScreen(n, persp);
      float r = n.radius * persp * G.zoom;
      float dx = mp.x - c.x, dy = mp.y - c.y;
      float d = dx * dx + dy * dy;
      if (d > r * r)
        continue;
      if (G.view3d) {
        G.hoverId = n.id;
        break;
      }
      if (d < bestD) {
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
            // Exact inverse of projectPoint at fixed depth: solve the 2x2
            // rotation directly, with one fixed-point persp refresh (the
            // old first-order offset skewed ~15% off-cursor at default
            // orbit).
            const float tx = (mp.x - origin.x - G.panX) / G.zoom;
            const float ty = (mp.y - origin.y - G.panY) / G.zoom;
            const float cyC = std::clamp(P.cy, 0.35f, 1.0f);
            const float cpC = std::clamp(P.cp, 0.35f, 1.0f);
            const float syC =
                std::sqrt(std::max(0.0f, 1.0f - cyC * cyC)) *
                (P.sy < 0 ? -1.0f : 1.0f);
            const float spC =
                std::sqrt(std::max(0.0f, 1.0f - cpC * cpC)) *
                (P.sp < 0 ? -1.0f : 1.0f);
            float nlx = dn.lx, nly = dn.ly, perspU = persp;
            for (int it = 0; it < 2; ++it) {
              const float x1 = tx / std::max(perspU, 0.05f);
              const float y1 = ty / std::max(perspU, 0.05f);
              nlx = (x1 - dn.lz * syC) / cyC;
              const float z1 = -nlx * syC + dn.lz * cyC;
              nly = (y1 + z1 * spC) / cpC;
              const float z1n = -nlx * P.sy + dn.lz * P.cy;
              const float z2n = nly * P.sp + z1n * P.cp;
              perspU =
                  P.focal / std::max(200.0f * ui::uiScale, P.focal + z2n);
            }
            G.force.setPos(slot, nlx, nly);
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

  // Double-click drill / back (works at any nesting depth). file nodes open
  // with the OS handler; keyword nodes filter the graph to their tagged
  // targets; structural kinds scope-drill as before.
  if (hovered && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) {
    if (!G.hoverId.empty() && G.hoverId != G.scopeDocId) {
      auto it = G.idIndex.find(G.hoverId);
      if (it != G.idIndex.end()) {
        const GNode &hn = G.nodes[it->second];
        if (hn.kind == "file") {
          const std::string abs = resolveNodeAbsPath(st, hn);
          if (!abs.empty())
            ui::openExternalFile(abs);
        } else if (hn.kind == "keyword") {
          std::snprintf(G.search, sizeof(G.search), "%s", hn.label.c_str());
          G.centerPending = true;
          G.matchDirty = true;
        } else if (isDrillableKind(hn.kind)) {
          drillInto(G, G.hoverId);
        }
      }
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
  // ---- Blast: epicentre flash ---------------------------------------------
  // ONE ring, during hop 0 only. Three concentric rings expanding over
  // unrelated geometry claimed a blast radius the dependency graph does not
  // have; the wave itself now travels the edges, so this is just "it started
  // here".
  if (blastLive) {
    auto eit = G.idIndex.find(G.blastEpicenter);
    if (eit != G.idIndex.end()) {
      const float w0 = ui::anim::stagger(G.blastT0, 0, ui::anim::kSlow);
      if (w0 < 1.0f) {
        const GNode &en = G.nodes[eit->second];
        const ImVec2 ec = toScreen(en.lx, en.ly, en.lz);
        const float ease = ui::anim::easeOut(w0);
        dl->AddCircle(ec, (en.radius + ease * 60.0f) * G.zoom,
                      ui::kit::toneU32(ui::kit::Tone::Warn,
                                       0.60f * (1.0f - w0)),
                      48, 2.0f * S);
      }
    }
  }

  const bool projectScope = G.scopeDocId.empty();
  // Label geometry — ONE definition, shared by the occupancy pass below and
  // the paint, so a reserved rect can never disagree with the pixels drawn.
  auto labelGeom = [&](ImVec2 c, float r, const char *lab, ImVec2 &ts,
                       float &drawW, ImVec2 &tp) {
    ts = ImGui::CalcTextSize(lab);
    const float maxW = 140.0f * S * std::max(0.8f, G.zoom);
    drawW = std::min(ts.x, maxW);
    tp = ImVec2(c.x - drawW * 0.5f, c.y + r + 3 * S);
  };
  const float labelPadX = 3.0f * S, labelPadY = 1.0f * S;
  auto labelRect = [&](ImVec2 tp, float drawW, ImVec2 ts) {
    // The halo rect the paint lays down, plus a 2 px breathing gap so two
    // accepted labels never end up flush against each other.
    return ImRect(tp.x - labelPadX - 2.0f * S, tp.y - labelPadY,
                  tp.x + drawW + labelPadX + 2.0f * S,
                  tp.y + ts.y + labelPadY);
  };

  // ---- Label occupancy pass ------------------------------------------------
  // Labels were painted unconditionally, so any tight cluster overprinted into
  // an unreadable stack. Rank the candidates and drop any whose backing rect
  // already overlaps a reserved one. Nothing becomes unreachable: the hover
  // tooltip still names every node.
  int onScreenNodes = 0;
  G.labelCand.clear();
  for (int vi : G.drawOrder) {
    const GNode &n = G.nodes[vi];
    float persp = 1.0f;
    const ImVec2 c = nodeScreen(n, persp);
    if (!onScreen(c))
      continue;
    ++onScreenNodes;
    if (n.label.empty())
      continue;
    const bool isMatch = filtering && G.matchIds.count(n.id) > 0;
    if (filtering && !isMatch)
      continue; // under the spotlight scrim — its label is noise
    LabelCand lc;
    lc.vi = vi;
    lc.c = c;
    lc.persp = persp;
    const bool sel = (n.id == G.selectedId);
    lc.r = n.radius * persp * G.zoom +
           ((sel || G.blast.count(n.id)) ? 2.0f * S : 0.0f);
    auto dg = G.degree.find(n.id);
    lc.deg = dg == G.degree.end() ? 0 : dg->second.first + dg->second.second;
    lc.forced = sel || n.id == G.hoverId || isMatch;
    G.labelCand.push_back(lc);
  }
  const bool labelAll = onScreenNodes <= 150;
  // Descending priority: forced first (they reserve their space, so a
  // lower-priority neighbour is the one that drops), then whole-graph degree —
  // the hub of a cluster is the label worth keeping — then screen radius, then
  // nearest-first in 3D. vi last so ties resolve identically every frame and
  // labels cannot flicker between two indistinguishable nodes.
  std::sort(G.labelCand.begin(), G.labelCand.end(),
            [](const LabelCand &a, const LabelCand &b) {
              if (a.forced != b.forced)
                return a.forced;
              if (a.deg != b.deg)
                return a.deg > b.deg;
              if (a.r != b.r)
                return a.r > b.r;
              if (a.persp != b.persp)
                return a.persp > b.persp;
              return a.vi < b.vi;
            });
  G.labelShown.assign(G.nodes.size(), 0);
  G.labelTaken.clear();
  for (const LabelCand &lc : G.labelCand) {
    if (!lc.forced && !((labelAll && G.zoom >= 0.35f) || G.zoom >= 0.9f))
      continue; // LOD: text is the draw bottleneck on a wide web
    ImVec2 ts, tp;
    float drawW = 0.0f;
    labelGeom(lc.c, lc.r, G.nodes[lc.vi].label.c_str(), ts, drawW, tp);
    const ImRect rc = labelRect(tp, drawW, ts);
    if (!lc.forced) {
      bool clash = false;
      for (const ImRect &t : G.labelTaken)
        if (t.Overlaps(rc)) {
          clash = true;
          break;
        }
      if (clash)
        continue;
    }
    G.labelTaken.push_back(rc);
    G.labelShown[static_cast<size_t>(lc.vi)] = 1;
  }
  // Cold-thumbnail admissions left this frame (see kBillboardNewPerFrame).
  int billboardBudget = kBillboardNewPerFrame;
  for (int vi : G.drawOrder) {
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
    // Spotlight: a match is painted above the scrim at its true brightness, so
    // its own alpha is no longer collapsed to 0.18 — the scrim does the dimming
    // for the whole world in one rect. Selection and hover ride up with it,
    // because the occupancy pass forces THEIR labels and a label above the
    // scrim over a disc below it would read as a floating orphan.
    if (filtering)
      spot.setChannel((dim && !sel && !hov) ? 0 : 1);
    // Soft-dim non-incident nodes when something is focused; blast dims harder.
    bool nonIncident =
        incidenceDim && !sel && !hov && !inBlast && n.id != focusId;
    float a;
    if (!G.blast.empty() && nonIncident)
      a = 0.07f;
    else if (nonIncident)
      a = 0.45f;
    else
      a = 1.0f;
    ImU32 fill = kindColor(n.kind, 0.85f * a);
    // How far the shockwave has got at THIS node. The warm tint and halo below
    // fade in with it, so a five-hop impact set arrives as five waves rather
    // than as one instantaneous flash.
    const float blastW = (inBlast && blastLive) ? blastWave(n.id) : 0.0f;
    if (inBlast && !dim && blastW > 0.0f) {
      // Warm tint on blast members so impact reads differently from selection.
      const ImU32 warm = ImGui::GetColorU32(
          ImVec4(palette.statusWarn[0] * 0.55f + palette.accent[0] * 0.15f,
                 palette.statusWarn[1] * 0.45f + 0.15f,
                 palette.statusWarn[2] * 0.20f, 0.88f * a));
      fill = mixCol(fill, warm, blastW);
    }
    ImU32 ring = Palette::toU32(palette.border);
    // Rim slot precedence: selection / blast / hover while active, then vault
    // lifecycle, then the cross-project tint. Lifecycle outranks the project
    // tint because it is the actionable one — an assembly of Released parts
    // with one In-work child has to read without a click — but the tint is not
    // dropped: it moves to a thinner outer ring below.
    const bool projTint = G.projects.size() > 1 && !n.metaProject.empty();
    ImU32 lcRim = 0;
    const bool hasLc = lifecycleRim(n.metaLifecycle, a, lcRim);
    if (hasLc)
      ring = lcRim;
    else if (projTint)
      ring = projectColor(n.metaProject, 0.85f * a);
    if (sel) {
      ring = Palette::toU32(palette.accent);
      r += 2.0f * S;
    } else if (inBlast) {
      ring = Palette::toU32(palette.statusWarn);
      r += 2.0f * S;
    } else if (hov) {
      ring = Palette::toU32(palette.accentDim);
    }
    const float k = hazeK(persp);
    fill = hazeCol(fill, k);
    ring = hazeCol(ring, k);
    // Nearest slice of the depth range gets a soft bloom, so "in front" is
    // legible without moving the camera. 3D only; flat frames have no front.
    if (G.view3d && !dim) {
      const float dt = depthT(persp);
      if (dt <= kBloomFront) {
        const float b = 1.0f - dt / kBloomFront;
        dl->AddCircleFilled(c, r * 1.5f, kindColor(n.kind, 0.06f * b * a), 24);
        dl->AddCircleFilled(c, r * 2.2f, kindColor(n.kind, 0.03f * b * a), 24);
      }
    }
    if (n.metaSuppressed) {
      // Hollow instead of solid. A suppressed feature that still has
      // downstream DEPENDS_ON edges is what makes a rebuild behave
      // inexplicably, and a solid disc makes it identical to a live one.
      dl->AddCircleFilled(c, r, hazeCol(kindColor(n.kind, 0.16f * a), k), 24);
      if (r < 9.0f) {
        // Too small for the kind glyph, so hatch it — otherwise a suppressed
        // node at this zoom is just a faint disc.
        const float h = r * 0.62f;
        const ImU32 hc = hazeCol(kindColor(n.kind, 0.60f * a), k);
        dl->AddLine(ImVec2(c.x - h, c.y - h), ImVec2(c.x + h, c.y + h), hc,
                    1.2f * S);
        dl->AddLine(ImVec2(c.x - h, c.y + h), ImVec2(c.x + h, c.y - h), hc,
                    1.2f * S);
      }
    } else {
      dl->AddCircleFilled(c, r, fill, 24);
    }
    // Iso billboard: past kBillboardR the kind letter stops being the most
    // useful thing in the disc — the part itself is. The kind ring and any
    // lifecycle rim stay on top, so identity is never traded for the picture.
    // Instance nodes MUST resolve INSTANCE_OF first: their own path is the
    // CONTAINING assembly, so the raw path renders the parent every time.
    float billT = 0.0f;
    // !dim: under the search scrim a billboard is invisible detail, and the
    // cold-thumbnail budget belongs to whatever the user is actually hunting.
    if (!n.metaSuppressed && !dim) {
      const GNode *bt = instanceTarget(G, n);
      const GNode &bn = bt ? *bt : n;
      if (renderableKind(bn.kind) && !bn.path.empty()) {
        const ImGuiID bid = ImGui::GetID(n.id.c_str());
        const bool want = r >= kBillboardR;
        // peek() so a disc nowhere near the threshold never even allocates a
        // tween slot: at ME489 scale that would be a table entry per node.
        if (want || ui::anim::peek(bid) > 0.004f)
          billT = ui::anim::toward(bid, want ? 1.0f : 0.0f, ui::anim::kFast);
        GraphGridThumb th;
        if (billT > 0.01f) {
          const std::string abs = resolveNodeAbsPath(st, bn);
          if (!abs.empty()) {
            const std::string tkey = graphThumbKey(bn.metaDh, bn.metaMt, abs);
            auto &seen = billboardSeen();
            if (seen.count(tkey)) {
              th = graphThumbEnsure(st, G.projectDir, tkey, abs);
            } else if (billboardBudget > 0) {
              --billboardBudget;
              seen.insert(tkey);
              th = graphThumbEnsure(st, G.projectDir, tkey, abs);
            }
          }
        }
        // r >= 1: AddImageRounded degrades to a SQUARE AddImage below a 0.5 px
        // corner radius, which would flash a rectangle mid-dissolve.
        if (th.tex && th.w > 0 && th.h > 0 && r >= 1.0f) {
          // Centre-crop to a square: the mask is circular, so a non-square
          // render stretched to fit would distort the part.
          float u0 = 0.0f, v0 = 0.0f, u1 = 1.0f, v1 = 1.0f;
          if (th.w > th.h) {
            const float f = 0.5f * (1.0f - (float)th.h / (float)th.w);
            u0 = f;
            u1 = 1.0f - f;
          } else if (th.h > th.w) {
            const float f = 0.5f * (1.0f - (float)th.w / (float)th.h);
            v0 = f;
            v1 = 1.0f - f;
          }
          // rounding == r turns the rounded-rect path into the circular mask,
          // and hazing the identity tint keeps a far billboard behind the haze
          // instead of punching through it.
          dl->AddImageRounded(
              (ImTextureID)(intptr_t)th.tex, ImVec2(c.x - r, c.y - r),
              ImVec2(c.x + r, c.y + r), ImVec2(u0, v0), ImVec2(u1, v1),
              hazeCol(ImGui::GetColorU32(ImVec4(1.0f, 1.0f, 1.0f, billT * a)),
                      k),
              r);
        } else {
          billT = 0.0f; // nothing to dissolve into — keep the kind glyph
        }
      }
    }
    dl->AddCircle(c, r, ring, 24, (sel || inBlast ? 2.6f : 1.4f) * S);
    // Project tint displaced by the lifecycle rim — kept as a thinner outer
    // ring so a cross-project Released part still reads as foreign.
    if (projTint && hasLc)
      dl->AddCircle(c, r + 2.5f * S,
                    hazeCol(projectColor(n.metaProject, 0.65f * a), k), 24,
                    1.0f * S);
    // Endpoint of a stale external reference: the geometry downstream of this
    // node may already be wrong.
    if (G.staleNodes.count(n.id))
      dl->AddCircle(c, r + 4.5f * S,
                    hazeCol(ui::kit::toneU32(ui::kit::Tone::Err, 0.85f * a), k),
                    24, 1.6f * S);
    // Spotlight ring on a search match: breathes while spotAmp is alive, then
    // holds steady. pulse() is only called while it is still breathing, so a
    // settled query lets the app fall back to its 20 Hz idle.
    if (filtering && !dim) {
      float sp = 0.5f;
      if (spotAmp > 0.002f)
        sp = 0.5f + 0.5f * spotAmp *
                        (2.0f * ui::anim::pulse(ui::anim::kBreathe) - 1.0f);
      // Each literal scaled individually rather than the group: (5 + 3*sp) * S is correct but
      // the scale lint can only see a scale token adjacent to a literal, and a suppression here
      // would be hiding a true positive's warning for a false one.
      dl->AddCircle(c, r + 5.0f * S + 3.0f * S * sp,
                    ui::kit::toneU32(ui::kit::Tone::Accent,
                                     0.55f + 0.35f * sp),
                    32, 1.8f * S);
    }
    // Pulsing outer halo on blast nodes (distinct from selection accent).
    // ui::anim::pulse, NOT std::sin(GetTime()*k): a raw sine paints whatever
    // phase the next frame happens to land on, and the app idles at 20 Hz the
    // instant the pointer stops — so the "pulse" stepped instead of breathing.
    if (inBlast && !dim && blastW > 0.01f) {
      const float pl = 0.55f + 0.45f * ui::anim::pulse(ui::anim::kBreathe);
      const ImU32 halo =
          ui::kit::toneU32(ui::kit::Tone::Warn, 0.35f * pl * a * blastW);
      dl->AddCircle(c, r + (4.0f + 3.0f * pl) * S, halo, 28, 2.0f * S);
    }
    if (hov && !dim)
      dl->AddCircleFilled(
          c, r * 0.35f,
          ImGui::GetColorU32(
              ImVec4(palette.text[0], palette.text[1], palette.text[2], 0.55f)),
          12);

    // Kind glyph inside the disc once it's big enough on screen — dissolved out
    // by whatever the billboard has faded in, so the two never fight.
    const float glyphA = (dim ? 0.5f : 0.95f) * (1.0f - billT);
    if (r >= 9.0f && glyphA > 0.02f) {
      const char *glyph = kindShort(n.kind);
      ImVec2 gs = ImGui::CalcTextSize(glyph);
      if (gs.x <= r * 1.6f) {
        // Knocked out of the fill normally; on a hollow suppressed disc there
        // is no fill to knock out of, so it inverts to the kind colour.
        const ImU32 gc =
            n.metaSuppressed
                ? hazeCol(kindColor(n.kind, glyphA), k)
                : ImGui::GetColorU32(ImVec4(palette.panelBg[0],
                                            palette.panelBg[1],
                                            palette.panelBg[2], glyphA));
        dl->AddText(ImVec2(c.x - gs.x * 0.5f, c.y - gs.y * 0.5f), gc, glyph);
      }
    }

    // Labels: the occupancy pass above already applied the LOD gate and culled
    // every colliding rect, so the paint just honours its verdict.
    const bool showLabel = G.labelShown[static_cast<size_t>(vi)] != 0;
    if (showLabel) {
      const char *lab = n.label.c_str();
      ImVec2 ts, tp;
      float drawW = 0.0f;
      labelGeom(c, r, lab, ts, drawW, tp);
      const float maxW = 140.0f * S * std::max(0.8f, G.zoom);
      ImU32 tc = ImGui::GetColorU32(ImVec4(palette.text[0], palette.text[1],
                                           palette.text[2],
                                           sel || hov ? 1.0f : 0.90f));

      // Subtle dark halo/backing rect for legibility over edges.
      ImU32 halo = ImGui::GetColorU32(ImVec4(
          palette.panelBg[0], palette.panelBg[1], palette.panelBg[2], 0.72f));
      dl->AddRectFilled(ImVec2(tp.x - labelPadX, tp.y - labelPadY),
                        ImVec2(tp.x + drawW + labelPadX, tp.y + ts.y + labelPadY),
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
  // World paint done: fold the scrim + matches back down before anything else
  // touches this draw list. The guard would do it, but doing it HERE keeps the
  // tooltip and the empty/loading overlays out of the split entirely.
  spot.merge();

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
        // Prefer the worker-rendered iso thumbnail (same pipeline as the
        // sidebar/grid). The embedded thumbnail.png is often a blank grey
        // placeholder for extracted/reverse-engineered packages, which is why
        // the hover preview looked dead while the sidebar looked fine. Resolve
        // instance nodes to their referenced doc first.
        const GNode *instTgt = instanceTarget(G, *hn);
        const GNode &tn = instTgt ? *instTgt : *hn;
        const std::string abs = resolveNodeAbsPath(st, tn);
        GraphGridThumb gth;
        if (!abs.empty()) {
          const std::string tkey = graphThumbKey(tn.metaDh, tn.metaMt, abs);
          gth = graphThumbEnsure(st, G.projectDir, tkey, abs);
        }
        // Only fall back to the embedded png when the worker render is not
        // coming (failed / nothing queued) — avoids flashing the grey stub
        // while a proper render is in flight.
        const GraphThumb *th = nullptr;
        if (!gth.tex && !gth.pending && !abs.empty())
          th = ensureGraphThumb(abs);
        if (gth.tex || (th && th->tex)) {
          const GLuint tex = gth.tex ? gth.tex : th->tex;
          const float maxSide = 120.0f * S;
          float tw = (float)(gth.tex ? gth.w : th->w),
                thh = (float)(gth.tex ? gth.h : th->h);
          if (tw > 1.0f && thh > 1.0f) {
            const float sc = std::min(maxSide / tw, maxSide / thh);
            tw *= sc;
            thh *= sc;
          } else {
            tw = thh = maxSide;
          }
          ImGui::Image((ImTextureID)(intptr_t)tex, ImVec2(tw, thh));
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
        else if (hn->kind == "file")
          ImGui::TextUnformatted("double-click to open");
        else if (hn->kind == "keyword")
          ImGui::TextUnformatted("double-click to filter by this keyword");
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
    const char *msg = "Mapping project graph\xe2\x80\xa6";
    const std::string detail = graphLoadingDetail(G);
    ImVec2 ts = ImGui::CalcTextSize(msg);
    ImVec2 ds = ImGui::CalcTextSize(detail.c_str());
    const float r = 9.0f * S;
    const float gap = 8.0f * S;
    const float blockH = r * 2.0f + gap + ts.y + gap * 0.5f + ds.y;
    const ImVec2 spinC(origin.x + canvasSz.x * 0.5f,
                       origin.y + (canvasSz.y - blockH) * 0.5f + r);
    // The brand mark, not a bespoke arc: this file used to carry its own drawArcSpinner, one of
    // six independent spinner idioms across the app. requestFrames() is required because a
    // draw-list paint declares no motion of its own — without it this turns at the 20 Hz idle
    // floor whenever the pointer is still, which is precisely the stutter ui/anim.h exists to fix.
    ui::anim::requestFrames();
    ui::drawTesseractSpinner(dl, spinC, r * 2.0f, ImGui::GetTime(),
                             Palette::toU32(palette.accent));
    const float textY = spinC.y + r + gap;
    dl->AddText(ImVec2(origin.x + (canvasSz.x - ts.x) * 0.5f, textY),
                Palette::toU32(palette.accent), msg);
    dl->AddText(ImVec2(origin.x + (canvasSz.x - ds.x) * 0.5f,
                       textY + ts.y + gap * 0.5f),
                Palette::toU32(palette.textDim), detail.c_str());
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
// Double-click a structural row drills in (mirrors canvas); file rows open
// with the OS handler; keyword rows filter to their targets.
void drawListView(AppState &st, ImVec2 canvasSz) {
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
    // Widget spinner (plmSpinner) — same brand mark as the rest of PLM.
    // Continuous redraw while loading is covered by st.rpc.busy() in the
    // frame loop.
    plmSpinner("Mapping project graph");
    ui::kit::dimText(graphLoadingDetail(G).c_str());
    ImGui::EndChild();
    return;
  }

  // Stable order (sort mode 1 = Kind: kind order, then label, then id).
  std::vector<int> order(G.nodes.size());
  for (int i = 0; i < (int)order.size(); ++i)
    order[i] = i;
  std::sort(order.begin(), order.end(), [&](int a, int b) {
    const GNode &A = G.nodes[a], &B = G.nodes[b];
    if (G.gridSort == 0) { // Name
      if (A.label != B.label)
        return A.label < B.label;
      return A.id < B.id;
    }
    if (G.gridSort == 2) { // Newest
      if (A.metaMt != B.metaMt)
        return A.metaMt > B.metaMt;
      if (A.label != B.label)
        return A.label < B.label;
      return A.id < B.id;
    }
    int ra = kindOrder(A.kind), rb = kindOrder(B.kind);
    if (ra != rb)
      return ra < rb;
    if (A.label != B.label)
      return A.label < B.label;
    return A.id < B.id;
  });

  const bool filtering = G.search[0] != 0;
  ImGuiListClipper clipper;
  // Pre-filter indices so clipper sees only visible rows.
  std::vector<int> visible;
  visible.reserve(order.size());
  for (int i : order) {
    if (filtering && !matchesSearch(G.nodes[i], G.search))
      continue;
    if (!G.kindFilter.empty() && !G.kindFilter.count(G.nodes[i].kind))
      continue;
    visible.push_back(i);
  }
  // Same hole the canvas had: rows exist but every one is filtered away, and
  // the view degenerates to a blank scroll region under a "0 visible" count.
  if (visible.empty()) {
    ImGui::Dummy(ImVec2(0, ui::metrics::U(6.0f)));
    if (activeFilterCount(G, /*graphMode=*/false) > 0) {
      char reason[128];
      std::snprintf(reason, sizeof(reason),
                    "All %d loaded nodes are hidden by the active filters.",
                    (int)G.nodes.size());
      if (ui::kit::blockedState("Nothing to show", reason,
                                activeFilterSummary(G).c_str(),
                                "Clear filters"))
        clearAllFilters(st, G);
    } else {
      ui::kit::emptyState("isolate", "Nothing to list",
                          "This project graph has no nodes in the current "
                          "scope.");
    }
    ImGui::EndChild();
    return;
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
          n.id != G.scopeDocId) {
        if (n.kind == "file") {
          const std::string abs = resolveNodeAbsPath(st, n);
          if (!abs.empty())
            ui::openExternalFile(abs);
        } else if (n.kind == "keyword") {
          std::snprintf(G.search, sizeof(G.search), "%s", n.label.c_str());
          G.centerPending = true;
          G.matchDirty = true;
        } else if (isDrillableKind(n.kind)) {
          drillInto(G, n.id);
        }
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
// elided label + vault identity (part number / lifecycle) when the manifest
// carries it. Click selects (inspector). Double-click is the primary
// activation: leaf design/document tiles open the underlying document
// (st.requestOpenPath — the side panel's "Open document" routing); assembly
// group tiles keep the drill-in navigation. Missing previews paint a
// kind-icon placeholder immediately and render on demand via the
// .plm/thumbs disk cache (graph_thumbs).
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
    plmSpinner("Mapping project graph");
    ui::kit::dimText(graphLoadingDetail(G).c_str());
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
      if (!G.kindFilter.empty() && !G.kindFilter.count(n.kind))
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
      if (!G.kindFilter.empty() && !G.kindFilter.count(n.kind))
        continue;
      items.push_back(vi);
    }
  }
  std::sort(items.begin(), items.end(), [&](int a, int b) {
    const GNode &A = G.nodes[a], &B = G.nodes[b];
    if (G.gridSort == 0) { // Name
      if (A.label != B.label)
        return A.label < B.label;
      return A.id < B.id;
    }
    if (G.gridSort == 2) { // Newest
      if (A.metaMt != B.metaMt)
        return A.metaMt > B.metaMt;
      if (A.label != B.label)
        return A.label < B.label;
      return A.id < B.id;
    }
    int ra = kindOrder(A.kind), rb = kindOrder(B.kind);
    if (ra != rb)
      return ra < rb;
    if (A.label != B.label)
      return A.label < B.label;
    return A.id < B.id;
  });

  if (items.empty()) {
    ImGui::Dummy(ImVec2(0, ui::metrics::U(6.0f)));
    if (activeFilterCount(G, /*graphMode=*/false) > 0) {
      char reason[128];
      std::snprintf(reason, sizeof(reason),
                    "All %d loaded nodes are hidden by the active filters.",
                    (int)G.nodes.size());
      if (ui::kit::blockedState("Nothing to show", reason,
                                activeFilterSummary(G).c_str(),
                                "Clear filters"))
        clearAllFilters(st, G);
    } else {
      ui::kit::emptyState("isolate", "No parts or assemblies here",
                          "This scope carries no assembly, part or document "
                          "nodes to tile.");
    }
    ImGui::EndChild();
    return;
  }

  // Vault identity (manifest part number / lifecycle) earns a third text
  // line only when at least one listed tile actually carries it — projects
  // with no vault data keep the compact two-line cell. Never invented:
  // tiles without meta simply leave the row blank.
  bool anyMeta = false;
  for (int ii : items) {
    const GNode &mn = G.nodes[ii];
    if (!mn.metaPartNumber.empty() || !mn.metaLifecycle.empty()) {
      anyMeta = true;
      break;
    }
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
  const float cellH = pad + tile + 4.0f * Sc + lineH + 2.0f * Sc + lineH +
                      (anyMeta ? 2.0f * Sc + lineH : 0.0f) + 6.0f * Sc;
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

        // Resolve the preview/open target once. Instance nodes resolve
        // INSTANCE_OF first (their own path is the containing assembly), so
        // both the thumbnail and the open action land on the instanced doc.
        const GNode *instTgt = instanceTarget(G, n);
        const GNode &tn = instTgt ? *instTgt : n;
        const std::string absPath = resolveNodeAbsPath(st, tn);
        // Assemblies are the grid's group tiles: activation keeps the
        // drill-in navigation. Part/doc tiles are leaf documents: activation
        // opens the document itself.
        const bool groupTile = (n.kind == "assembly");

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
        if (hov && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) {
          if (!groupTile && !absPath.empty()) {
            // Leaf design/document tile: the primary activation opens the
            // underlying document. requestOpenPath routes by document kind
            // in app.cpp's pump (Model / Drawing / Simulate / Electronics),
            // exactly like the side panel's "Open document" action.
            st.requestOpenPath = absPath;
          } else if (isDrillableKind(n.kind) && n.id != G.scopeDocId) {
            // Group tile (assembly) — or a leaf whose file did not resolve —
            // keeps the drill navigation. Single click never drills.
            drillInto(G, n.id);
          }
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

        GraphGridThumb th;
        if (!absPath.empty()) {
          const std::string key = graphThumbKey(tn.metaDh, tn.metaMt, absPath);
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
            const ImVec2 keep = ImGui::GetCursorScreenPos();
            const float box = ImGui::GetTextLineHeight();
            ImGui::SetCursorScreenPos(
                ImVec2(t0.x + tile * 0.5f - box * 0.5f,
                       t0.y + tile * 0.78f - box * 0.5f));
            ui::kit::spinner("");
            ImGui::SetCursorScreenPos(keep);
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

        // ---- vault identity row (part number · lifecycle) ----------------
        // The lifecycle badge is the shared hub widget, so Released reads
        // the same green here as on the Files page and in the feature tree.
        if (anyMeta) {
          ty += lineH + 2.0f * Sc;
          float mx = textX;
          if (!n.metaPartNumber.empty()) {
            float pnW = textW;
            if (!n.metaLifecycle.empty())
              pnW -=
                  ImGui::CalcTextSize(n.metaLifecycle.c_str()).x + 8.0f * Sc;
            const std::string pn =
                elideToWidth(n.metaPartNumber, std::max(pnW, 24.0f * Sc));
            dl->AddText(ImVec2(mx, ty),
                        ui::kit::toneU32(ui::kit::Tone::Info, 1.0f),
                        pn.c_str());
            mx += ImGui::CalcTextSize(pn.c_str()).x + 8.0f * Sc;
          }
          if (!n.metaLifecycle.empty() &&
              mx + ImGui::CalcTextSize(n.metaLifecycle.c_str()).x <=
                  textX + textW + 1.0f) {
            const ImVec2 keep = ImGui::GetCursorScreenPos();
            ImGui::SetCursorScreenPos(ImVec2(mx, ty));
            plmLifecycleBadge(n.metaLifecycle);
            ImGui::SetCursorScreenPos(keep);
          }
        }

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
          if (!n.metaPartNumber.empty() || !n.metaLifecycle.empty()) {
            std::string idLine = n.metaPartNumber;
            if (!n.metaLifecycle.empty()) {
              if (!idLine.empty())
                idLine += " \xc2\xb7 ";
              idLine += n.metaLifecycle;
            }
            ImGui::PushStyleColor(ImGuiCol_Text,
                                  ImVec4(palette.textDim[0], palette.textDim[1],
                                         palette.textDim[2], 1.0f));
            ImGui::TextUnformatted(idLine.c_str());
            ImGui::PopStyleColor();
          }
          if (th.pending) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                                  ImVec4(palette.textDim[0], palette.textDim[1],
                                         palette.textDim[2], 1.0f));
            ImGui::TextUnformatted("Preview rendering...");
            ImGui::PopStyleColor();
          }
          ImGui::PushStyleColor(ImGuiCol_Text,
                                ImVec4(palette.textDisabled[0],
                                       palette.textDisabled[1],
                                       palette.textDisabled[2], 1.0f));
          if (!groupTile && !absPath.empty())
            ImGui::TextUnformatted("double-click to open in its editor");
          else if (isDrillableKind(n.kind) && n.id != G.scopeDocId)
            ImGui::TextUnformatted("double-click to drill in");
          ImGui::PopStyleColor();
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

// One popover for every display filter. Data-source toggles, node/edge
// visibility and the eleven relation pills used to be ~15 flat controls
// wrapped across three toolbar rows with nothing grouping them; the popover's
// active-count badge is what keeps a collapsed filter honest.
void drawFilterPopover(AppState &st, GraphTabState &G, bool graphMode) {
  if (!ui::kit::popoverBegin("##graph_filters", "Filters", "filter_funnel",
                             activeFilterCount(G, graphMode)))
    return;

  ui::kit::sectionHeader("Source", nullptr, nullptr, -1, /*first=*/true);
  {
    bool hide = G.hideArchive;
    if (ui::kit::toggleChip("Hide archive/stale", &hide, ui::kit::Tone::Accent,
                            "Exclude _archive_stale / _archive / archive / "
                            ".trash\ndirectories from the graph (default "
                            "ON).")) {
      // include_archive is a backend walk policy: flipping it must refetch.
      G.hideArchive = hide;
      G.lastFetch = -1e9;
      requestGraph(st, /*force=*/true);
    }
    ImGui::SameLine(0, ui::metrics::U(1.0f));
    bool linked = G.linkedProjects;
    if (ui::kit::toggleChip("Linked projects", &linked, ui::kit::Tone::Info,
                            "Merge graphs of projects linked to this one.\n"
                            "Node rims tint per owning project.")) {
      G.linkedProjects = linked;
      G.lastFetch = -1e9;
      requestGraph(st, /*force=*/true);
    }
  }

  if (graphMode) {
    bool hu = G.hideUnconnected;
    if (ui::kit::toggleChip("Hide unconnected", &hu, ui::kit::Tone::Accent,
                            "Hide nodes with no visible links (default ON).\n"
                            "They remain available in List view.")) {
      G.hideUnconnected = hu;
      rebuildVisible(G);
    }

    const int kindTotal = (int)IM_ARRAYSIZE(kEdgeKinds);
    ui::kit::sectionHeader("Relations", nullptr, nullptr,
                           kindTotal - (int)G.hiddenKinds.size());
    const float swatchW = ui::metrics::U(5.0f);
    ImDrawList *pdl = ImGui::GetWindowDrawList();
    for (int ki = 0; ki < kindTotal; ++ki) {
      const char *k = kEdgeKinds[ki];
      const ImVec2 p = ImGui::GetCursorScreenPos();
      const float h = ImGui::GetFrameHeight();
      // kit::Tone is six SEMANTIC roles, so it cannot carry an arbitrary edge
      // hue: the swatch holds the exact legend colour, the chip holds state.
      pdl->AddLine(ImVec2(p.x, p.y + h * 0.5f),
                   ImVec2(p.x + swatchW, p.y + h * 0.5f), edgeColor(k, 1.0f),
                   ui::metrics::stroke(2.0f));
      ImGui::Dummy(ImVec2(swatchW, h));
      ImGui::SameLine(0, ui::metrics::U(1.5f));
      bool vis = !G.hiddenKinds.count(k);
      if (ui::kit::toggleChip(edgeKindLabel(k).c_str(), &vis)) {
        if (vis)
          G.hiddenKinds.erase(k);
        else
          G.hiddenKinds.insert(k);
        G.layoutDirty = true; // hidden kinds exert no spring force
      }
    }
  } else {
    ui::kit::sectionHeader("Node kinds", nullptr, nullptr,
                           (int)G.kindFilter.size());
    if (ui::kit::secondaryButton("All kinds", !G.kindFilter.empty(),
                                 "Every kind is already shown."))
      G.kindFilter.clear();
    std::set<std::string> present;
    for (const auto &n : G.nodes)
      present.insert(n.kind);
    for (const auto &k : present) {
      bool on = G.kindFilter.count(k) > 0;
      if (ui::kit::toggleChip(k.c_str(), &on)) {
        if (on)
          G.kindFilter.insert(k);
        else
          G.kindFilter.erase(k);
      }
    }

    ui::kit::sectionHeader("Sort");
    static const char *kSorts[3] = {"Name", "Kind", "Newest"};
    ui::kit::segmented("##graph_sort", kSorts, 3, &G.gridSort);
  }

  ui::kit::popoverEnd();
}

// Canvas overlay for "every node is filtered out". drawCanvas guards an empty
// NODE list but not an empty VISIBLE list, which ordinary filter combinations
// reach (hide-archive + hide-unconnected + a drill into a fully-filtered
// scope, or Links-only on a project with no cross-doc links) — the canvas then
// paints a blank glass rectangle while the status line reports "0 of N nodes
// visible". Hosted in its own child so the action button wins the hit test
// over the canvas InvisibleButton underneath.
void drawCanvasBlocked(AppState &st, GraphTabState &G, ImVec2 canvasOrigin,
                       ImVec2 canvasSz) {
  const float w = std::min(canvasSz.x - ui::metrics::U(6.0f),
                           ui::metrics::px(400.0f));
  const float h = std::min(canvasSz.y - ui::metrics::U(6.0f),
                           ui::metrics::px(240.0f));
  if (w <= 0.0f || h <= 0.0f)
    return;
  ImGui::SetCursorScreenPos(ImVec2(canvasOrigin.x + (canvasSz.x - w) * 0.5f,
                                   canvasOrigin.y + (canvasSz.y - h) * 0.5f));
  ImGui::BeginChild("##graph_blocked", ImVec2(w, h), ImGuiChildFlags_None,
                    ImGuiWindowFlags_NoScrollbar |
                        ImGuiWindowFlags_NoScrollWithMouse |
                        ImGuiWindowFlags_NoBackground);
  char reason[128];
  std::snprintf(reason, sizeof(reason),
                "All %d loaded nodes are hidden by the active filters.",
                (int)G.nodes.size());
  if (ui::kit::blockedState("Nothing to show", reason,
                            activeFilterSummary(G).c_str(), "Clear filters"))
    clearAllFilters(st, G);
  ImGui::EndChild();
}

} // namespace

// ---- public
// ------------------------------------------------------------------

bool plmGraphAvailable() { return S().available; }

// True once a cad_graph_get response has landed (headless shot sync).
bool plmGraphLoaded() { return !S().loading && !S().nodes.empty(); }

void plmGraphPrefetch(AppState &st) {
  auto &G = S();
  // lastFetch is -1e9 only until the first request for this project (project
  // changes reset it), so this issues at most one background load per project.
  if (!G.available || G.loading || !G.nodes.empty() || G.lastFetch > -1e8)
    return;
  requestGraph(st, /*force=*/false);
}

void plmGraphOnProjectChanged() {
  auto &G = S();
  maybeSaveLayout(G, /*force=*/true); // final solve-state save for old project
  freeAllGraphThumbs();
  G.nodes.clear();
  G.edges.clear();
  G.edgeIndexDirty = true;
  clearBlast(G);
  G.selectedId.clear();
  G.hoverId.clear();
  G.err.clear();
  G.generatedAt.clear();
  G.layoutDirty = true;
  G.loading = false;
  G.fetchInFlight = false;
  ++G.fetchSerial;
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
  G.keywordAdd[0] = 0;
  G.projects.clear();
  G.truncated = false;
  G.nodeCountFull = 0;
  G.edgeCountFull = 0;
  G.listView = false;
  G.listViewUserOverride = false;
  G.gridView = false;
  graphThumbsReset();
  billboardSeen().clear(); // stale keys would bypass the per-frame cold budget
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

  // Shot-only hook (CHUDWARE_PLM_GRAPH_VIEW=grid): force Grid view so headless
  // --ui-screenshot runs can capture it (mirrors CHUDWARE_FM_COLLAPSED). No-op
  // in normal use.
  {
    static const bool shotGrid = [] {
      const char *v = std::getenv("CHUDWARE_PLM_GRAPH_VIEW");
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

  // Auto-refresh when the OS app regains focus (bypasses the poll interval).
  // Use force=false so a warm ME489-scale cache can serve without a full
  // rebuild (mtime fast-path).
  const bool lost = ImGui::GetIO().AppFocusLost;
  if (G.wasAppFocusLost && !lost)
    G.lastFetch = -1e9;
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
  if (G.focusSearchPending) { // '/' binding
    ImGui::SetKeyboardFocusHere();
    G.focusSearchPending = false;
  }
  ImGui::SetNextItemWidth(ui::metrics::px(220.0f));
  if (ImGui::InputTextWithHint("##graph_search", "Search nodes\xe2\x80\xa6  (/)",
                               G.search,
                               sizeof(G.search))) {
    G.centerPending = G.search[0] != 0;
    G.matchDirty = true;
    G.force.reheat(G.hierarchyLayout
                       ? 0.0f
                       : 0.12f); // matched nodes draw the eye as they settle
  }
  // View mode: Graph | List | Grid.
  ImGui::SameLine();
  {
    static const char *kViewLabels[3] = {"Graph", "List", "Grid"};
    static const char *kViewTips[3] = {
        "Graph view animates the current scope as a live force-directed\n"
        "layout (drag nodes to rearrange).",
        "List view shows every loaded node (including features) as\n"
        "filterable rows.",
        "Grid view tiles the parts and assemblies of the current scope as\n"
        "square cells with iso previews, rendered on demand and cached."};
    int vm = G.gridView ? 2 : (G.listView ? 1 : 0);
    if (ui::kit::segmented("##graph_view", kViewLabels, 3, &vm, 0.0f,
                           kViewTips)) {
      if (vm == 0 && (G.listView || G.gridView))
        G.layoutDirty = true;
      G.listView = (vm == 1);
      G.gridView = (vm == 2);
      G.listViewUserOverride = true;
    }
  }
  // Dimension: 2D | 3D — graph view only.
  if (!G.listView && !G.gridView) {
    ImGui::SameLine();
    static const char *kDimLabels[2] = {"2D", "3D"};
    static const char *kDimTips[2] = {
        "2D force layout (flat x/y). Drag nodes to rearrange.",
        "3D force layout: physics runs on x/y/z. Left-drag orbits,\n"
        "right-drag pans, Shift+drag moves a node in depth."};
    int dim = G.view3d ? 1 : 0;
    if (ui::kit::segmented("##graph_dim", kDimLabels, 2, &dim, 0.0f,
                           kDimTips) &&
        (dim == 1) != G.view3d) {
      G.view3d = !G.view3d;
      // Transition: capture pre-toggle positions for the lerp animation.
      G.trans3dFrom.clear();
      G.trans3dTo.clear();
      for (const auto &n : G.nodes)
        if (hasPosition(n))
          G.trans3dFrom[n.id] = {n.lx, n.ly, n.lz};
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
        // Planetary ball: nodes WITHOUT real model coordinates map the 2D
        // pack disc onto a sphere (azimuth preserved, disc radius -> polar
        // angle) — a solid 3D ball instead of a flat cone. Deterministic;
        // stashed + restored through the same overlay flow.
        {
          float bcx = 0.0f, bcy = 0.0f, r2d = 0.0f;
          int pn = 0;
          for (const auto &n : G.nodes) {
            if (n.hasW || !hasPosition(n))
              continue;
            bcx += n.lx;
            bcy += n.ly;
            ++pn;
          }
          if (pn > 0) {
            bcx /= (float)pn;
            bcy /= (float)pn;
            for (const auto &n : G.nodes) {
              if (n.hasW || !hasPosition(n))
                continue;
              r2d = std::max(r2d, std::hypot(n.lx - bcx, n.ly - bcy));
            }
            if (r2d < 1.0f)
              r2d = 1.0f;
            for (auto &n : G.nodes) {
              if (n.hasW || !hasPosition(n))
                continue;
              if (G.preWorldStash.find(n.id) == G.preWorldStash.end())
                G.preWorldStash[n.id] = {n.lx, n.ly, n.lz};
              const float dx = (n.lx - bcx) / r2d, dy = (n.ly - bcy) / r2d;
              const float d = std::clamp(std::hypot(dx, dy), 0.0f, 1.0f);
              const float th = d * 3.14159265f;
              const float ph = d > 1e-5f ? std::atan2(dy, dx) : 0.0f;
              const float sth = std::sin(th);
              n.lx = bcx + r2d * sth * std::cos(ph);
              n.ly = bcy + r2d * sth * std::sin(ph);
              n.lz = r2d * std::cos(th);
            }
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
      // Transition targets: animate from pre-toggle to post-toggle.
      for (const auto &n : G.nodes)
        if (hasPosition(n))
          G.trans3dTo[n.id] = {n.lx, n.ly, n.lz};
      G.t3dStart = ImGui::GetTime();
      G.layoutDirty = true; // rebuild the sim in the new dimensionality
      // Refit so the projected bounds frame correctly on entry.
      G.fitRequested = true;
      G.userNavigated = false;
    }
  }

  // Filters: source policy, node/link visibility and the relation checklist,
  // collapsed into ONE popover that badges its active count.
  ImGui::SameLine();
  drawFilterPopover(st, G, !G.listView && !G.gridView);

  ImGui::SameLine();
  if (ui::kit::iconButton("##graph_refresh", "refresh",
                          G.loading
                              ? "Refreshing\xe2\x80\xa6"
                              : "Re-fetch the project graph from the backend.",
                          !G.loading))
    requestGraph(st, /*force=*/true);
  // "What changed since I last looked?" — the one question this tab could not answer at all.
  // cad_graph_changes has been registered, contracted and tested on the worker the whole time and
  // the GUI simply never called it (6 of 18 cad_graph_* RPCs were reachable). It is a TOGGLE, and
  // fetching is a SEPARATE explicit gesture inside the rail, because the worker's diff runs a full
  // forced rebuild — wiring it into the poll path or the focus-regain refresh would turn a 20 Hz
  // idle app into a rebuild treadmill.
  if (ui::plmgraph::changesAvailable() && !G.projectDir.empty()) {
    ImGui::SameLine();
    const int nch = ui::plmgraph::changesCount();
    std::string lbl = "Changes";
    if (nch > 0) lbl += " " + ui::kit::fmtCount(nch);
    bool on = ui::plmgraph::changesActive();
    if (ui::kit::toggleChip(lbl.c_str(), &on, ui::kit::Tone::Accent,
                            on ? "Hide the changes rail."
                               : "Show what was added or removed since the last cached build "
                                 "of this project."))
      ui::plmgraph::toggleChanges(st, G.projectDir, G.linkedProjects);
  }
  if (!G.listView && !G.gridView) {
    ImGui::SameLine();
    if (ui::kit::iconButton("##graph_fit", "zoom_fit",
                            "Frame all visible nodes in the canvas.  (F)")) {
      G.fitRequested = true; // the ONLY auto-fit path left
      G.userNavigated = false;
    }
    // Relation scope: the link web, everything, or assembly structure.
    // One word per segment. "Links only" was eliding to "Links o..." in the segment's own
    // measured width, and three parallel one-word choices read better anyway; the tooltip
    // below carries the full meaning.
    static const char *kScopeLabels[3] = {"Links", "All", "Assembly"};
    static const char *kScopeTips[3] = {
        "Hide containment and show only the cross-document link web.",
        "Show every relation kind.",
        "Assembly structure: assemblies + parts with containment, instance\n"
        "and mate relations, laid out as a deterministic layered tree.\n"
        "Arrangement is kept per-session."};
    const bool linksOnly =
        G.hiddenKinds.size() == 1 && G.hiddenKinds.count("CONTAINS") > 0;
    int scope = G.asmView ? 2 : (linksOnly ? 0 : 1);
    const int wasScope = scope;
    const float scopeW = 3.0f * (ImGui::CalcTextSize("Assembly").x +
                                 2.0f * ui::kit::padX());
    if (ui::kit::segmented("##graph_scope", kScopeLabels, 3, &scope, scopeW,
                           kScopeTips) &&
        scope != wasScope) {
      if ((scope == 2) != G.asmView)
        toggleAsmView(G);
      if (scope == 0) {
        G.hiddenKinds.clear();
        G.hiddenKinds.insert("CONTAINS"); // hide containment → the link web
        G.layoutDirty = true;             // hidden kinds exert no spring force
      } else if (scope == 1) {
        G.hiddenKinds.clear();
        G.layoutDirty = true;
      }
    }
  }
  if (!G.err.empty() && !G.nodes.empty()) {
    ImGui::SameLine();
    plmError(G.err);
  }
  // Layout seeding is a force-layout concern: graph view only (as before).
  if (!G.listView && !G.gridView) {
  {
    static const char *kClusterModes[] = {"Default", "By project", "By keyword",
                                          "By folder"};
    if (ui::kit::fieldEnum("##cluster_mode", "Layout", &G.clusterMode, kClusterModes,
                          (int)IM_ARRAYSIZE(kClusterModes),
                          ui::kit::FieldOpts().withEnumStyle(ui::kit::EnumStyle::Combo))) {
      for (auto &n : G.nodes)
        n.lx = n.ly = n.lz = 0.0f;
      G.projPosStash.clear();
      G.asmPosStash.clear();
      G.layoutCache.clear(); // disk file is overwritten on the next settle
      G.hierarchyLayout = false;
      G.userNavigated = false;
      G.layoutDirty = true;
    }
    if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal)) {
      ImGui::SetTooltip(
          "Initial-position seeding for the force layout (project web):\n"
          "Default — balloon hierarchy / current behavior.\n"
          "By project — cluster per owning project root (merged graphs).\n"
          "By keyword — cluster around each keyword's tagged targets.\n"
          "By folder — cluster per folder (file assets / doc paths).");
    }
  }
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
    // Project root crumb: atlas icon (no unicode glyphs — the app font
    // lacks them and they render as "?"). body_folder reads as the project
    // container.
    {
      ImGui::AlignTextToFramePadding();
      const ImVec2 ip = ImGui::GetCursorScreenPos();
      const float ic = 14.0f * S;
      // Reserve the full button-row height (not just the icon's own ic x ic
      // box) so the icon centers on the "Project" button beside it instead
      // of sitting top-anchored above a taller frame item.
      const float rowH = ImGui::GetFrameHeight();
      ImGui::Dummy(ImVec2(ic, rowH));
      ImGui::SameLine(0, 3.0f * S);
      ui::drawIconGlass(ImGui::GetWindowDrawList(), "body_folder",
                        ImVec2(ip.x + ic * 0.5f, ip.y + rowH * 0.5f), ic,
                        kindColor("project", 0.9f), 0);
    }
    if (ImGui::SmallButton("Project##bc_root")) {
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
      ImGui::TextUnformatted(">"); // ASCII separator (font lacks U+203A)
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
        // Was two raw SmallButtons whose active state was a text-colour hack.
        static const char *kDecompLabels[2] = {"Internals", "Parts"};
        static const char *kDecompTips[2] = {
            "Internals: raw features / sketches / mates of this document.",
            "Parts: subassembly/part decomposition with instance counts."};
        int decomp = G.scopeDecomp ? 1 : 0;
        if (ui::kit::segmented("##scope_decomp", kDecompLabels, 2, &decomp,
                               0.0f, kDecompTips)) {
          G.scopeDecomp = (decomp == 1);
          rebuildVisible(G);
        }
      }
    }
  }

  // Honest counts consolidated into ONE dim status line (was scattered across
  // the toolbar): visible / loaded / edges / generated stamp, with the
  // load-truncation warning appended in the warn tint.
  if (!G.nodes.empty()) {
    const int visN = (int)G.visibleIdx.size();
    const int loadN = (int)G.nodes.size();
    const int fullN = G.nodeCountFull > 0 ? G.nodeCountFull : loadN;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.textDim[0],
                                                palette.textDim[1],
                                                palette.textDim[2], 1.0f));
    if (G.scopeDocId.empty())
      ImGui::Text("%d of %d nodes visible \xc2\xb7 %d edges", visN, fullN,
                  (int)G.visibleEdges.size());
    else
      ImGui::Text("%d in scope \xc2\xb7 %d of %d loaded \xc2\xb7 %d edges", visN,
                  loadN, fullN, (int)G.visibleEdges.size());
    ImGui::PopStyleColor();
    if (!G.generatedAt.empty()) {
      ImGui::SameLine(0, 8 * S);
      ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.textDisabled[0],
                                                  palette.textDisabled[1],
                                                  palette.textDisabled[2],
                                                  1.0f));
      ImGui::Text("\xc2\xb7 generated %s", G.generatedAt.c_str());
      ImGui::PopStyleColor();
    }
    if (G.truncated || loadN < fullN) {
      ImGui::SameLine(0, 8 * S);
      ImGui::PushStyleColor(ImGuiCol_Text,
                            ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                                   palette.statusWarn[2], 1.0f));
      ImGui::Text("(loaded %d/%d)", loadN, fullN);
      ImGui::PopStyleColor();
    }
  }

  // Cross-project legend: one rim-tint swatch + project basename per owning
  // root in the merged graph (matches the node border colors on canvas).
  if (G.projects.size() > 1) {
    ImDrawList *ldl = ImGui::GetWindowDrawList();
    const float sq = 10.0f * S;
    const float rowH = ImGui::GetTextLineHeight();
    ImGui::PushStyleColor(ImGuiCol_Text,
                          ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                                 palette.textDisabled[2], 1.0f));
    ImGui::TextUnformatted("Projects:");
    ImGui::PopStyleColor();
    for (const auto &proj : G.projects) {
      ImGui::SameLine(0, 10 * S);
      const ImVec2 sp = ImGui::GetCursorScreenPos();
      ldl->AddRectFilled(
          ImVec2(sp.x, sp.y + (rowH - sq) * 0.5f),
          ImVec2(sp.x + sq, sp.y + (rowH - sq) * 0.5f + sq),
          projectColor(proj), 2.0f * S);
      ImGui::Dummy(ImVec2(sq, rowH));
      ImGui::SameLine(0, 4 * S);
      std::string base = proj;
      if (auto sl = base.find_last_of("/\\"); sl != std::string::npos)
        base = base.substr(sl + 1);
      ImGui::PushStyleColor(ImGuiCol_Text,
                            ImVec4(palette.textDim[0], palette.textDim[1],
                                   palette.textDim[2], 1.0f));
      ImGui::TextUnformatted(base.c_str());
      ImGui::PopStyleColor();
      if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal))
        ImGui::SetTooltip("%s", proj.c_str());
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


  // Persistent gesture/key hints. These used to be documented ONLY in the
  // inspector's placeholder text, which disappears the moment anything is
  // selected — i.e. exactly when the user starts needing them.
  {
    ImGui::PushStyleColor(ImGuiCol_Text,
                          ImVec4(palette.textDisabled[0],
                                 palette.textDisabled[1],
                                 palette.textDisabled[2], 1.0f));
    if (!G.listView && !G.gridView && G.view3d)
      ImGui::TextUnformatted("double-click a node to drill in \xc2\xb7 "
                             "Backspace goes back \xc2\xb7 / search \xc2\xb7 "
                             "F fit \xc2\xb7 left-drag orbits \xc2\xb7 "
                             "Shift+drag moves depth");
    else
      ImGui::TextUnformatted("double-click a node to drill in \xc2\xb7 "
                             "Backspace goes back \xc2\xb7 / search \xc2\xb7 "
                             "F fit");
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
  const ImVec2 corigin = ImGui::GetCursorScreenPos();
  ImVec2 csz = ImGui::GetContentRegionAvail();
  if (G.gridView)
    drawGridView(st, csz);
  else if (G.listView)
    drawListView(st, csz);
  else {
    drawCanvas(st, csz);
    // Canvas overlays: both live in their own child windows so they can be
    // clicked through the canvas's full-bleed InvisibleButton.
    if (G.visibleIdx.empty() && !G.nodes.empty() && !G.loading)
      drawCanvasBlocked(st, G, corigin, csz);
    drawLegendCard(G, corigin, csz, G.scopeDocId.empty());
  }
  ImGui::EndChild();

  ImGui::SameLine(0, gap);
  // The changes rail takes the inspector's column while the overlay is on: the two answer
  // different questions and there is no room for both. A clicked row returns a node id, and we
  // centre the camera on it rather than only selecting it, so "what changed" and "where is it"
  // are one gesture.
  if (ui::plmgraph::changesActive()) {
    ImGui::BeginChild("##graph_changes_rail", ImVec2(sideW, 0), false,
                      ImGuiWindowFlags_NoSavedSettings);
    const std::string jump = ui::plmgraph::drawChangesRail(st);
    ImGui::EndChild();
    if (!jump.empty()) {
      const auto it = G.idIndex.find(jump);
      if (it != G.idIndex.end()) {
        G.selectedId = jump;
        centerOnNode(G, G.nodes[it->second], csz);
      }
    }
  } else {
    drawSidePanel(st, sideW);
  }

  ImGui::EndChild();

  // ---- link authoring popup (outside the canvas child) --------------------
  if (G.linkPopupOpen) {
    ImGui::OpenPopup("Add link##graph_link");
    G.linkPopupOpen = false; // consume: the popup now owns its lifetime
  }
  if (ImGui::BeginPopup("Add link##graph_link")) {
    ImGui::TextUnformatted("New link");
    ui::kit::fieldEnum("kind##lk", "Kind", &G.linkKindIdx, kEdgeKinds,
                      IM_ARRAYSIZE(kEdgeKinds),
                      ui::kit::FieldOpts().withEnumStyle(ui::kit::EnumStyle::Combo));
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
  // ---- keyboard ------------------------------------------------------------
  // Every one of these already existed as a function and was reachable only by
  // mouse. Text fields keep their keys (WantTextInput), and a live popup owns
  // Escape.
  const bool addLinkOpen = ImGui::IsPopupOpen("Add link##graph_link");
  if (!addLinkOpen && ImGui::IsKeyPressed(ImGuiKey_Escape)) {
    if (!G.blast.empty())
      clearBlast(G);
    else if (!G.linkArmSrc.empty())
      G.linkArmSrc.clear();
  }
  if (!addLinkOpen && !ImGui::GetIO().WantTextInput &&
      ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows)) {
    if (ImGui::IsKeyPressed(ImGuiKey_Slash))
      G.focusSearchPending = true;
    if (ImGui::IsKeyPressed(ImGuiKey_F) && !G.listView && !G.gridView) {
      G.fitRequested = true;
      G.userNavigated = false;
    }
    if (ImGui::IsKeyPressed(ImGuiKey_Enter) ||
        ImGui::IsKeyPressed(ImGuiKey_KeypadEnter)) {
      auto it = G.idIndex.find(G.selectedId);
      if (it != G.idIndex.end() && G.nodes[it->second].id != G.scopeDocId &&
          isDrillableKind(G.nodes[it->second].kind))
        drillInto(G, G.nodes[it->second].id);
    }
    if (ImGui::IsKeyPressed(ImGuiKey_Backspace) && !G.scopeDocId.empty())
      scopeBack(G);
    if (ImGui::IsKeyPressed(ImGuiKey_Delete) &&
        !selectedAuthoredLinkId(G).empty())
      G.linkRemoveConfirm = true; // raises the inspector's confirmation
  }
}

} // namespace ui
