// Native ECAD schematic/PCB editor and per-document pinned manager.
#include "imgui.h"
#include "imgui_internal.h"   // GetWindowDockNode + NoTabBar for the shared central-node doc tabs

#include "ecad_panel.h"
#include "../doc_tabs.h"      // document tabs share the central node with the Viewport (see below)
#include "../layout.h"        // overlayColumnEdge — the pinned columns float over this window
#include "../feature_tree.h"  // fmPaneHeaderChip/fmSectionDivider/fmRowPill/fmToolbarIconBtn — the
                              // ECAD manager body reuses the Feature Manager's own row/header chrome
                              // so the two panels read as the same design language (item 2).
#include "../metrics.h"
#include "../widgets/kit.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <memory>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

#include "../../core/app_state.h"
#include "../../ui_theme.h"

#include "ecad_canvas.h"
#include "ecad_mirror.h"

namespace ui {

// The document strip is drawn by whichever central view window is active. ECAD participates so
// Part/Assembly/ECAD tabs stay contained in the shared view area.
static void ecadDockStripDraw(AppState& st) {
    ImGuiDockNode* node = ImGui::GetWindowDockNode();
    if (node) node->LocalFlags |= ImGuiDockNodeFlags_NoTabBar;   // never show the redundant window tab
    if (ui::docTabStripHeight(st) > 0.0f) ui::drawDocTabs(st, ImGui::GetContentRegionAvail().x);
}

// Per-document ECAD editor state. Wave 2 (GUI rework) promoted this OFF the file-static singleton:
// the per-document fields (mirror, canvas, selection, edit buffers) now live on each Schematic/PCB
// DocTab (AppState::DocTab::ecadState, a shared_ptr<ui::EcadEditorState>) so multiple ECAD docs can
// be open at once. The struct is in `namespace ui` (not the anonymous namespace) so app_state.h's
// forward declaration `struct ui::EcadEditorState` matches; its full layout still lives ONLY here.
struct EcadEditorState {
    EcadMirror mirror;
    EcadCanvas canvas;
    EcadMirror::Kind lastKind = EcadMirror::Kind::None;
    // Canvas editing context. Every value is per document so switching tabs restores the exact
    // schematic/board command state instead of leaking the last document's choices.
    std::string placeLib = "Device:R";
    std::string placeFp  = "Resistor_SMD:R_0603";
    int schPlaceKind = 0;       // 0 symbol, 1 label, 2 power, 3 junction, 4 no-connect, 5 text
    int pcbPlaceKind = 0;       // 0 footprint, 1 via, 2 zone
    char placeLabel[96] = "NET";
    int placeLabelScope = 0;    // 0 local, 1 global, 2 hierarchical
    char placePowerNet[64] = "GND";
    std::string placePowerSymbol = "GND";
    char placeText[256] = "Note";
    char placeBusName[96] = "DATA[0..7]";
    std::string activeLayer = "F.Cu";
    std::string activeNet;
    float routeWidth = 0.25f;
    float viaDrill = 0.30f;
    float viaDiameter = 0.60f;
    float snapGrid = 0.50f;
    bool routing = false;
    bool routeBus = false;
    bool routeZone = false;
    std::vector<EcadPt> routePath;
    bool routeHorizontalFirst = true;
    EcadPt routeAnchor{};
    std::string routeStartSym, routeStartPin;
    // Ordered-set selection. Order is stable for clipboard/delete and duplicates are prohibited.
    std::vector<std::string> selected;
    bool dragging = false;
    bool marquee = false;
    EcadPt dragStart{};
    EcadPt marqueeEnd{};
    float dragDx = 0, dragDy = 0;
    nlohmann::json clipboard = nlohmann::json::array();
    int pasteSerial = 0;
    // Property inspector buffers. Reloaded whenever the primary selection changes.
    char editRef[64] = "";
    char editValue[96] = "";
    char editFootprint[128] = "";
    char editDatasheet[256] = "";
    bool editDnp = false;
    bool editInBom = true;
    bool editOnBoard = true;
    int editUnit = 1;
    char editSimParams[512] = "";
    char editFields[1024] = "";
    bool catalogLoaded = false;
    char newFieldKey[64] = "";
    char newFieldValue[128] = "";
    std::string editLoadedFor;
    // Searchable engine-backed symbol catalog and the footprint ids it exposes.
    char symbolSearch[96] = "";
    std::string catalogQuery;
    std::vector<nlohmann::json> symbolCatalog;
    std::vector<std::string> footprintCatalog;
    // Reused pointer list for data-derived kit pickers; strings remain mirror/catalog owned.
    std::vector<const char*> comboItems;
    bool starterCanvasChosen = false;
    std::string catalogError;
    // Sheet controls.
    char newSheetName[96] = "Sheet";
    bool activateNewSheet = false;
    char renameSheetName[96] = "";
    std::string renameLoadedFor;
    float bottomStripTopScreenY = 0.0f;
    // Simulation plot: snapshot of the last results we plotted + the analysis that produced them,
    // so the plot axes match the run (not the live combo) and we can pop the Sim tab on new results.
    nlohmann::json lastSimResults;
    int lastAnalysis = 0;
    // Item 4 — runtime workspace health. Re-scanned once whenever the mirror revision changes
    // (tracked by healthScannedRev) so the scan never runs every frame or fires RPCs from the draw
    // path. healthMsg is a human status line; healthLevel: 0=ok 1=warn 2=error.
    std::string healthMsg;
    int  healthLevel = 0;       // 0 ok, 1 warn, 2 error
    int  healthScannedRev = -1; // mirror revision the current healthMsg was computed for
    std::string healthToasted;  // last healthMsg surfaced as a toast (post-once-per-change guard)
    // Dense manager state. These views, searches, routing settings and selection filters belong to
    // the ECAD document and therefore survive tab switches independently.
    int managerView = 0;        // 0 Design, 1 Inspect, 2 Checks
    int hierarchyKind = 0;      // 0 all; remaining values are document-kind specific
    char managerSearch[96] = "";
    char checksSearch[96] = "";
    int checksSeverity = 0;     // 0 all, 1 errors, 2 warnings
    bool pickSymbols = true;
    bool pickWires = true;
    bool pickFootprints = true;
    bool pickTracks = true;
    bool pickVias = true;
    // Manager-tree row selection ("layer:<id>" / "net:<name>").
    std::string mgrSel;
};

namespace {

// ---------------------------------------------------------------------------------------------
// Active-document resolution. Wave 2 moved the editor state onto the active Schematic/PCB DocTab,
// but the public accessors below (ecadEditorMirror / ecadEditorLoadResponse / ...) take no AppState.
// drawEcadEditor(st) records the AppState each frame; state() then returns the active ECAD tab's
// EcadEditorState (lazily allocating the DocTab's shared_ptr). A process-wide fallback covers the
// no-tab paths (headless shots that set st.ecadDocId directly, or a call before the first draw).
// ---------------------------------------------------------------------------------------------
AppState* g_app = nullptr;  // set by drawEcadEditor(st) each frame; null before the first draw

// The fallback used when no ECAD tab is active (or no AppState is known yet). With zero ECAD tabs
// every accessor resolves here, preserving the legacy single-document and headless paths.
EcadEditorState& fallbackState() {
    static EcadEditorState s;
    return s;
}

// Resolve the EcadEditorState the panel + accessors operate on: the active Schematic/PCB tab's
// (created on demand), else the process-wide fallback. Always returns a live reference.
EcadEditorState& state() {
    if (g_app) {
        AppState& st = *g_app;
        if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
            AppState::DocTab& t = st.docTabs[st.activeTab];
            if (t.kind == AppState::DocTab::TabKind::Schematic ||
                t.kind == AppState::DocTab::TabKind::PCB) {
                if (!t.ecadState) t.ecadState = std::make_shared<EcadEditorState>();
                return *t.ecadState;
            }
        }
    }
    return fallbackState();
}

float ecadBottomStripHeight(const AppState& st, const EcadMirror& mirror) {
    if (!mirror.isSch()) return 0.0f;
    const bool hasSimulation = st.ecadSimResults.is_object() && !st.ecadSimResults.empty();
    return (hasSimulation ? 260.0f : 140.0f) * ui::uiScale;
}
ImVec4 v4(const float c[4], float a = 1.0f) { return ImVec4(c[0], c[1], c[2], c[3] * a); }

// Item 4 — runtime workspace health scan. Reads the loaded mirror (pure, no RPC) and flags missing
// deps / unlinked components / broken schematic->board hooks. Only re-runs when the revision changed
// (guarded by es.healthScannedRev) so it costs nothing per frame. Records es.healthMsg/healthLevel;
// the header renders it. Auto-init of the empty board itself happens in app.cpp at workspace entry —
// here we only DETECT + FLAG (and never render a silently-broken viewport).
void scanEcadHealth(EcadEditorState& es) {
    const int rev = es.mirror.revision();
    if (rev == es.healthScannedRev) return;   // already scanned this revision
    es.healthScannedRev = rev;
    es.healthMsg.clear();
    es.healthLevel = 0;

    if (!es.mirror.hasDoc()) return;   // launchpad state — nothing to scan

    if (es.mirror.isSch()) {
        int unlinked = 0, ercErrors = 0, ercWarnings = 0;
        for (const auto& s : es.mirror.symbols())
            if (!s.dnp && s.onBoard && s.footprint.empty()) ++unlinked;
        for (const auto& v : es.mirror.erc())
            (v.severity == "error" ? ercErrors : ercWarnings)++;
        if (es.mirror.symbols().empty()) {
            es.healthMsg = "Empty schematic — place symbols or use Design with AI.";
            es.healthLevel = 1;
        } else if (ercErrors > 0) {
            es.healthMsg = std::to_string(ercErrors) + " ERC error(s), " +
                           std::to_string(ercWarnings) + " warning(s) — review Checks.";
            es.healthLevel = 2;
        } else if (unlinked > 0 || ercWarnings > 0) {
            es.healthMsg = std::to_string(unlinked) + " footprint assignment(s) missing, " +
                           std::to_string(ercWarnings) + " ERC warning(s).";
            es.healthLevel = 1;
        } else {
            es.healthMsg = "Schematic OK — ERC clean and board components assigned.";
            es.healthLevel = 0;
        }
    } else if (es.mirror.isPcb()) {
        const int unrouted = es.mirror.unroutedCount();
        if (es.mirror.footprints().empty()) {
            es.healthMsg = "Board has no footprints — run Update Board from the schematic to populate it.";
            es.healthLevel = 1;
        } else if (unrouted > 0) {
            es.healthMsg = std::to_string(unrouted) +
                " net(s) unrouted — route manually or run Autoroute.";
            es.healthLevel = 1;
        } else {
            es.healthMsg = "Board OK — populated and fully routed.";
            es.healthLevel = 0;
        }
    }
}

// Copper-layer swatch color, kept in sync with ecad_canvas.cpp::copperColor (palette roles, theme
// aware). Returns an ImVec4 for ImGui widget tinting.
ImVec4 layerSwatch(int copperIndex) {
    const ui::Palette& p = ui::palette;
    switch (((copperIndex % 6) + 6) % 6) {
        case 0:  return v4(p.axisX);
        case 1:  return v4(p.axisZ);
        case 2:  return v4(p.statusOk);
        case 3:  return v4(p.statusWarn);
        case 4:  return v4(p.axisY);
        default: return v4(p.statusErr);
    }
}

// Snap a world coordinate to a sensible ECAD grid (schematic 1.27mm; board 0.5mm).
float snapTo(float v, float g) { return std::round(v / g) * g; }

// Distance (world mm) from point p to segment a-b.
float ptSegDistP(const EcadPt& p, const EcadPt& a, const EcadPt& b) {
    const float vx = b.x - a.x, vy = b.y - a.y;
    const float wx = p.x - a.x, wy = p.y - a.y;
    const float vv = vx * vx + vy * vy;
    float t = (vv > 1e-9f) ? (wx * vx + wy * vy) / vv : 0.0f;
    t = std::max(0.0f, std::min(1.0f, t));
    const float cx = a.x + t * vx, cy = a.y + t * vy;
    return std::hypot(p.x - cx, p.y - cy);
}

bool splitToken(const std::string& token, std::string& kind, std::string& id) {
    const size_t c = token.find(':');
    if (c == std::string::npos || c == 0 || c + 1 >= token.size()) return false;
    kind = token.substr(0, c);
    id = token.substr(c + 1);
    return true;
}

const std::string& primarySelection(const EcadEditorState& es) {
    static const std::string empty;
    return es.selected.empty() ? empty : es.selected.back();
}

bool hasSelection(const EcadEditorState& es, const std::string& token) {
    return std::find(es.selected.begin(), es.selected.end(), token) != es.selected.end();
}

void applyPick(EcadEditorState& es, const std::string& token, bool toggle, bool extend) {
    if (token.empty()) {
        if (!toggle && !extend) es.selected.clear();
        return;
    }
    auto it = std::find(es.selected.begin(), es.selected.end(), token);
    if (toggle) {
        if (it == es.selected.end()) es.selected.push_back(token);
        else es.selected.erase(it);
    } else if (extend) {
        if (it == es.selected.end()) es.selected.push_back(token);
    } else {
        es.selected.assign(1, token);
    }
    es.editLoadedFor.clear();
}

bool onSheet(const EcadMirror& m, const std::string& sheet) { return m.onActiveSheet(sheet); }

// Local fast path used for hover/drag-start only. Authoritative click picks go through
// ecad_resolve_pick below, which shares the engine's geometry and priority rules.
std::string hitTest(const EcadEditorState& es, const EcadPt& w, float tol) {
    const EcadMirror& m = es.mirror;
    std::string best;
    float bestD = 1e30f;
    auto point = [&](const char* kind, const std::string& id, const EcadPt& p, float radius = -1.0f) {
        const float d = std::hypot(w.x - p.x, w.y - p.y);
        const float r = radius > 0.0f ? radius : tol;
        if (d <= r && d < bestD) { bestD = d; best = std::string(kind) + ":" + id; }
    };
    if (m.isSch()) {
        if (es.pickSymbols) {
            for (const auto& s : m.symbols()) if (onSheet(m, s.sheet)) {
                for (const auto& pin : s.pins)
                    point("pin", s.id + "." + pin.number, pin.tip);
            }
            if (!best.empty()) return best;
        }
        for (const auto& j : m.junctions()) if (onSheet(m, j.sheet)) point("junction", j.id, j.pos);
        for (const auto& l : m.labels()) if (onSheet(m, l.sheet)) point("label", l.id, l.pos);
        for (const auto& p : m.powers()) if (onSheet(m, p.sheet)) point("power", p.id, p.pos);
        for (const auto& n : m.noConnects()) if (onSheet(m, n.sheet)) point("nc", n.id, n.pos);
        for (const auto& e : m.busEntries()) if (onSheet(m, e.sheet)) point("busentry", e.id, e.pos);
        for (const auto& t : m.texts()) if (onSheet(m, t.sheet)) point("text", t.id, t.pos);
        for (const auto& s : m.sheetSymbols()) if (onSheet(m, s.sheet))
            point("sheetsym", s.id, {s.pos.x + s.size.x * 0.5f, s.pos.y + s.size.y * 0.5f},
                  std::max(tol, 0.5f * std::hypot(s.size.x, s.size.y)));
        if (!best.empty()) return best;
        if (es.pickSymbols) {
            for (const auto& s : m.symbols()) if (onSheet(m, s.sheet))
                point("sym", s.id, s.pos, std::max(tol, 2.6f));
            if (!best.empty()) return best;
        }
        auto lines = [&](const char* kind, const auto& items) {
            for (const auto& item : items) if (onSheet(m, item.sheet))
                for (size_t i = 1; i < item.pts.size(); ++i) {
                    const float d = ptSegDistP(w, item.pts[i - 1], item.pts[i]);
                    if (d <= tol && d < bestD) { bestD = d; best = std::string(kind) + ":" + item.id; }
                }
        };
        if (es.pickWires) lines("wire", m.wires());
        lines("bus", m.buses());
        lines("graphic", m.graphics());
    } else {
        if (es.pickFootprints) for (const auto& fp : m.footprints()) {
            for (const auto& pad : fp.pads)
                point("pad", fp.id + "." + pad.number, pad.pos,
                      std::max(tol, 0.5f * std::max(pad.w, pad.h)));
            point("fp", fp.id, fp.pos, std::max(tol, 1.5f));
        }
        if (!best.empty()) return best;
        if (es.pickVias) for (const auto& via : m.vias())
            point("via", via.id, via.at, std::max(tol, via.diameter * 0.5f));
        if (es.pickTracks) for (const auto& t : m.tracks()) {
            const float d = ptSegDistP(w, t.p1, t.p2);
            if (d <= std::max(tol, t.width * 0.5f) && d < bestD) {
                bestD = d; best = "track:" + t.id;
            }
        }
    }
    return best;
}

std::string selectorToken(const nlohmann::json& sel) {
    if (!sel.is_object()) return {};
    const std::string kind = sel.value("kind", std::string());
    const std::string id = sel.value("id", std::string());
    if (id.empty()) return {};
    if (kind == "symbol") return "sym:" + id;
    if (kind == "pin")
        return "pin:" + sel.value("sym", id) + "." + sel.value("pin", std::string());
    if (kind == "no_connect") return "nc:" + id;
    if (kind == "bus_entry") return "busentry:" + id;
    if (kind == "sheet_symbol") return "sheetsym:" + id;
    if (kind == "footprint") return "fp:" + id;
    if (kind == "pad")
        return "pad:" + sel.value("fp", id) + "." + sel.value("pad", std::string());
    static const std::set<std::string> direct = {
        "wire", "bus", "junction", "label", "power", "text", "graphic",
        "track", "via", "zone"};
    return direct.count(kind) ? kind + ":" + id : std::string();
}

std::string resolvePick(AppState& st, const EcadEditorState& es, const EcadPt& w, float tol) {
    nlohmann::json kinds = nlohmann::json::array();
    if (es.mirror.isSch()) {
        if (es.pickSymbols) { kinds.push_back("pin"); kinds.push_back("symbol"); }
        if (es.pickWires) kinds.push_back("wire");
        for (const char* k : {"junction", "label", "power", "no_connect", "bus_entry",
                              "bus", "text", "graphic", "sheet_symbol"})
            kinds.push_back(k);
    } else {
        if (es.pickFootprints) { kinds.push_back("pad"); kinds.push_back("footprint"); }
        if (es.pickTracks) kinds.push_back("track");
        if (es.pickVias) kinds.push_back("via");
        kinds.push_back("zone");
    }
    nlohmann::json result;
    std::string err;
    nlohmann::json params{{"doc_id", es.mirror.docId()}, {"xy", {w.x, w.y}},
                          {"tol", tol}, {"kinds", kinds}};
    if (es.mirror.isPcb() && !es.activeLayer.empty()) params["layer"] = es.activeLayer;
    if (st.worker.call("ecad_resolve_pick", params, result, err, 5000)) {
        const nlohmann::json selector = result.value("selector", nlohmann::json(nullptr));
        if (!selector.is_object() || !es.mirror.isSch() ||
            es.mirror.onActiveSheet(selector.value("sheet", std::string())))
            return selectorToken(selector);
        return hitTest(es, w, tol);
    }
    st.addLog("[ecad] resolve pick failed: " + err);
    return hitTest(es, w, tol);
}

bool selectedPos(const EcadMirror& m, const std::string& token, EcadPt& out) {
    std::string kind, id;
    if (!splitToken(token, kind, id)) return false;
    if (kind == "sym" || kind == "pin") {
        const std::string sid = kind == "pin" ? id.substr(0, id.rfind('.')) : id;
        for (const auto& s : m.symbols()) if (s.id == sid) {
            if (kind == "pin") {
                const std::string pinNo = id.substr(id.rfind('.') + 1);
                for (const auto& pin : s.pins) if (pin.number == pinNo) { out = pin.tip; return true; }
            }
            out = s.pos; return true;
        }
    } else if (kind == "fp" || kind == "pad") {
        const std::string fid = kind == "pad" ? id.substr(0, id.rfind('.')) : id;
        for (const auto& fp : m.footprints()) if (fp.id == fid) {
            if (kind == "pad") {
                const std::string padNo = id.substr(id.rfind('.') + 1);
                for (const auto& pad : fp.pads) if (pad.number == padNo) { out = pad.pos; return true; }
            }
            out = fp.pos; return true;
        }
    } else if (kind == "wire") {
        for (const auto& x : m.wires()) if (x.id == id && !x.pts.empty()) { out = x.pts.front(); return true; }
    } else if (kind == "bus") {
        for (const auto& x : m.buses()) if (x.id == id && !x.pts.empty()) { out = x.pts.front(); return true; }
    } else if (kind == "busentry") {
        for (const auto& x : m.busEntries()) if (x.id == id) { out = x.pos; return true; }
    } else if (kind == "junction") {
        for (const auto& x : m.junctions()) if (x.id == id) { out = x.pos; return true; }
    } else if (kind == "label") {
        for (const auto& x : m.labels()) if (x.id == id) { out = x.pos; return true; }
    } else if (kind == "power") {
        for (const auto& x : m.powers()) if (x.id == id) { out = x.pos; return true; }
    } else if (kind == "nc") {
        for (const auto& x : m.noConnects()) if (x.id == id) { out = x.pos; return true; }
    } else if (kind == "text") {
        for (const auto& x : m.texts()) if (x.id == id) { out = x.pos; return true; }
    } else if (kind == "graphic") {
        for (const auto& x : m.graphics()) if (x.id == id && !x.pts.empty()) { out = x.pts.front(); return true; }
    } else if (kind == "sheetsym") {
        for (const auto& x : m.sheetSymbols()) if (x.id == id) { out = x.pos; return true; }
    } else if (kind == "track") {
        for (const auto& x : m.tracks()) if (x.id == id) { out = x.p1; return true; }
    } else if (kind == "via") {
        for (const auto& x : m.vias()) if (x.id == id) { out = x.at; return true; }
    } else if (kind == "zone") {
        for (const auto& x : m.zones()) if (x.id == id && !x.polygon.empty()) {
            out = x.polygon.front(); return true;
        }
    }
    return false;
}

void rotateOrFlipSelected(AppState& st, EcadEditorState& es, bool rotate) {
    std::string kind, id;
    if (!splitToken(primarySelection(es), kind, id)) return;
    if (kind == "pin") { kind = "sym"; id = id.substr(0, id.rfind('.')); }
    if (kind == "pad") { kind = "fp"; id = id.substr(0, id.rfind('.')); }
    nlohmann::json op;
    if (kind == "sym") {
        const EcadSymbol* sym = nullptr;
        for (const auto& s : es.mirror.symbols()) if (s.id == id) { sym = &s; break; }
        if (!sym) return;
        op = {{"op", "sch_edit_symbol"}, {"id", id}};
        if (rotate) op["angle"] = std::fmod(sym->angle + 90.0f, 360.0f);
        else op["mirror"] = sym->mirror == "none" ? "x" : "none";
    } else if (kind == "fp") {
        const EcadFootprint* fp = nullptr;
        for (const auto& f : es.mirror.footprints()) if (f.id == id) { fp = &f; break; }
        if (!fp) return;
        op = {{"op", "pcb_edit_footprint"}, {"id", id}};
        if (rotate) op["angle"] = std::fmod(fp->angle + 90.0f, 360.0f);
        else op["side"] = fp->side == "top" ? "bottom" : "top";
    } else return;
    st.ecadPendingOps = nlohmann::json::array({op});
    st.ecadOpsGo = true;
}

// Narrow-rail property editor for the selected symbol/footprint. Buffers reload on selection
// changes, then commit through the same contract-valid edit ops used by canvas hotkeys.
bool drawPropInspector(AppState& st, EcadEditorState& es, float sc) {
    (void)sc;
    std::string kind, id;
    const std::string token = primarySelection(es);
    if (!splitToken(token, kind, id)) return false;
    if (kind == "pin") { kind = "sym"; id = id.substr(0, id.rfind('.')); }
    if (kind == "pad") { kind = "fp"; id = id.substr(0, id.rfind('.')); }
    const bool sch = kind == "sym";
    if (!sch && kind != "fp") return false;

    const std::string* ref = nullptr;
    const std::string* value = nullptr;
    std::string libLabel;
    const EcadSymbol* sym = nullptr;
    if (sch) {
        for (const auto& s : es.mirror.symbols()) if (s.id == id) {
            sym = &s; ref = &s.ref; value = &s.value; libLabel = s.libid; break;
        }
    } else {
        for (const auto& fp : es.mirror.footprints()) if (fp.id == id) {
            ref = &fp.ref; value = &fp.value; libLabel = fp.fpid; break;
        }
    }
    if (!ref) return false;

    if (es.editLoadedFor != token) {
        std::snprintf(es.editRef, sizeof(es.editRef), "%s", ref->c_str());
        std::snprintf(es.editValue, sizeof(es.editValue), "%s", value->c_str());
        if (sym) {
            std::snprintf(es.editFootprint, sizeof(es.editFootprint), "%s", sym->footprint.c_str());
            std::snprintf(es.editDatasheet, sizeof(es.editDatasheet), "%s", sym->datasheet.c_str());
            es.editDnp = sym->dnp;
            es.editInBom = sym->inBom;
            es.editOnBoard = sym->onBoard;
            es.editUnit = sym->unit;
            const std::string params =
                sym->simParams.is_object() && !sym->simParams.empty() ? sym->simParams.dump() : "";
            std::snprintf(es.editSimParams, sizeof(es.editSimParams), "%s", params.c_str());
            const std::string fields = sym->fields.is_object() ? sym->fields.dump(2) : "{}";
            std::snprintf(es.editFields, sizeof(es.editFields), "%s", fields.c_str());
        } else {
            es.editFootprint[0] = es.editDatasheet[0] = es.editSimParams[0] = '\0';
            std::snprintf(es.editFields, sizeof(es.editFields), "{}");
            es.editDnp = false; es.editInBom = true; es.editOnBoard = true; es.editUnit = 1;
        }
        es.editLoadedFor = token;
    }

    ui::kit::keyValueRow(sch ? "Symbol library" : "Footprint library", libLabel.c_str());
    ui::kit::dimText("Reference");
    ImGui::SetNextItemWidth(-1.0f);
    bool commit = ImGui::InputText("##ecadEditRef", es.editRef, sizeof(es.editRef),
                                   ImGuiInputTextFlags_EnterReturnsTrue);
    ui::kit::dimText("Value");
    ImGui::SetNextItemWidth(-1.0f);
    commit |= ImGui::InputText("##ecadEditValue", es.editValue, sizeof(es.editValue),
                               ImGuiInputTextFlags_EnterReturnsTrue);
    if (sch) {
        ui::kit::dimText("Datasheet");
        ImGui::SetNextItemWidth(-1.0f);
        commit |= ImGui::InputText("##ecadDatasheet", es.editDatasheet,
                                   sizeof(es.editDatasheet), ImGuiInputTextFlags_EnterReturnsTrue);
        if (sym->unitCount > 1) {
            ui::kit::dimText("Unit");
            ImGui::SetNextItemWidth(-1.0f);
            ImGui::SliderInt("##ecadUnit", &es.editUnit, 1, sym->unitCount);
        }
        ImGui::Checkbox("Do not populate", &es.editDnp);
        ImGui::Checkbox("Include in BOM", &es.editInBom);
        ImGui::Checkbox("Place on board", &es.editOnBoard);
    }
    if (ui::kit::primaryButton("Apply properties", true, nullptr, ImVec2(-1.0f, 0.0f)))
        commit = true;
    if (commit) {
        nlohmann::json op{{"op", sch ? "sch_edit_symbol" : "pcb_edit_footprint"},
                          {"id", id}, {"ref", es.editRef}, {"value", es.editValue}};
        if (sch) {
            op["datasheet"] = es.editDatasheet;
            op["dnp"] = es.editDnp;
            op["in_bom"] = es.editInBom;
            op["on_board"] = es.editOnBoard;
            op["unit"] = es.editUnit;
        }
        st.ecadPendingOps = nlohmann::json::array({op});
        st.ecadOpsGo = true;
        es.editLoadedFor.clear();
    }

    const float actionW =
        (ImGui::GetContentRegionAvail().x - ui::kit::rowGap()) * 0.5f;
    if (ui::kit::secondaryButton("Rotate (R)", true, nullptr, ImVec2(actionW, 0.0f)))
        rotateOrFlipSelected(st, es, true);
    ImGui::SameLine(0.0f, ui::kit::rowGap());
    if (ui::kit::secondaryButton(sch ? "Flip (F)" : "Flip side (F)", true, nullptr,
                                 ImVec2(actionW, 0.0f)))
        rotateOrFlipSelected(st, es, false);

    if (!sch) return true;
    ui::kit::sectionHeader("Assembly", nullptr, nullptr, -1);
    es.comboItems.clear();
    es.comboItems.push_back("No footprint");
    int footprintChoice = 0;
    for (const std::string& fp : es.footprintCatalog) {
        if (fp == es.editFootprint) footprintChoice = static_cast<int>(es.comboItems.size());
        es.comboItems.push_back(fp.c_str());
    }
    if (ui::kit::fieldEnum("##ecadFootprintLibrary", "Footprint", &footprintChoice,
                           es.comboItems.data(), static_cast<int>(es.comboItems.size()),
                           ui::kit::FieldOpts().withEnumStyle(ui::kit::EnumStyle::Combo)
                               .withPreview(es.editFootprint[0] ? es.editFootprint : "No footprint")))
        std::snprintf(es.editFootprint, sizeof(es.editFootprint), "%s",
                      footprintChoice == 0 ? "" : es.comboItems[footprintChoice]);
    ImGui::SetNextItemWidth(-1.0f);
    bool fpCommit = ImGui::InputTextWithHint("##ecadFootprint",
        "Footprint id (blank clears)", es.editFootprint, sizeof(es.editFootprint),
        ImGuiInputTextFlags_EnterReturnsTrue);
    if (ui::kit::secondaryButton("Assign footprint", true, nullptr, ImVec2(-1.0f, 0.0f)))
        fpCommit = true;
    if (fpCommit) {
        nlohmann::json op{{"op", "sch_edit_symbol"}, {"id", id}};
        op["footprint"] = es.editFootprint[0] == '\0'
            ? nlohmann::json(nullptr) : nlohmann::json(std::string(es.editFootprint));
        st.ecadPendingOps = nlohmann::json::array({op});
        st.ecadOpsGo = true;
        es.editLoadedFor.clear();
    }

    ui::kit::sectionHeader("User fields", nullptr);
    ui::kit::dimText("JSON object (edit values or remove keys)");
    ImGui::SetNextItemWidth(-1.0f);
    ImGui::InputTextMultiline("##ecadFields", es.editFields, sizeof(es.editFields),
                              ImVec2(-1.0f, ui::metrics::U(16.0f)));
    ImGui::SetNextItemWidth(actionW);
    ImGui::InputTextWithHint("##newFieldKey", "Field name", es.newFieldKey,
                             sizeof(es.newFieldKey));
    ImGui::SameLine(0.0f, ui::kit::rowGap());
    ImGui::SetNextItemWidth(actionW);
    ImGui::InputTextWithHint("##newFieldValue", "Value", es.newFieldValue,
                             sizeof(es.newFieldValue));
    bool fieldsCommit = false;
    if (ui::kit::secondaryButton("Add field", es.newFieldKey[0] != '\0', nullptr,
                                 ImVec2(actionW, 0.0f))) {
        nlohmann::json fields = nlohmann::json::parse(es.editFields, nullptr, false);
        if (!fields.is_object()) fields = nlohmann::json::object();
        fields[es.newFieldKey] = es.newFieldValue;
        const std::string text = fields.dump(2);
        std::snprintf(es.editFields, sizeof(es.editFields), "%s", text.c_str());
        es.newFieldKey[0] = es.newFieldValue[0] = '\0';
        fieldsCommit = true;
    }
    ImGui::SameLine(0.0f, ui::kit::rowGap());
    if (ui::kit::secondaryButton("Apply fields", true, nullptr, ImVec2(actionW, 0.0f)))
        fieldsCommit = true;
    if (fieldsCommit) {
        nlohmann::json fields = nlohmann::json::parse(es.editFields, nullptr, false);
        if (!fields.is_object()) {
            st.toast(ui::ToastLevel::Warn, "User fields must be a JSON object");
        } else {
            st.ecadPendingOps = nlohmann::json::array(
                {{{"op", "sch_edit_symbol"}, {"id", id}, {"fields", fields}}});
            st.ecadOpsGo = true;
            es.editLoadedFor.clear();
        }
    }

    ui::kit::dimText("Simulation parameters (JSON object)");
    ImGui::SetNextItemWidth(-1.0f);
    ImGui::InputTextMultiline("##ecadSimParams", es.editSimParams, sizeof(es.editSimParams),
                              ImVec2(-1.0f, ui::metrics::U(12.0f)));
    if (ui::kit::secondaryButton("Apply simulation parameters", true, nullptr,
                                 ImVec2(-1.0f, 0.0f))) {
        const std::string txt(es.editSimParams);
        const bool blank = txt.find_first_not_of(" \t\r\n") == std::string::npos;
        nlohmann::json parsed = blank ? nlohmann::json::object()
                                      : nlohmann::json::parse(txt, nullptr, false);
        if (!parsed.is_object()) {
            st.toast(ui::ToastLevel::Warn, "Simulation parameters must be a JSON object");
        } else {
            nlohmann::json op{{"op", "sch_edit_symbol"}, {"id", id}};
            op["sim_params"] = blank ? nlohmann::json(nullptr) : parsed;
            st.ecadPendingOps = nlohmann::json::array({op});
            st.ecadOpsGo = true;
            es.editLoadedFor.clear();
        }
    }
    return true;
}
bool deleteSelected(AppState& st, EcadEditorState& es) {
    nlohmann::json ops = nlohmann::json::array();
    std::set<std::string> emitted;
    for (const std::string& token : es.selected) {
        std::string kind, id;
        if (!splitToken(token, kind, id)) continue;
        if (kind == "pin") { kind = "sym"; id = id.substr(0, id.rfind('.')); }
        if (kind == "pad") { kind = "fp"; id = id.substr(0, id.rfind('.')); }
        const std::string dedupe = kind + ":" + id;
        if (!emitted.insert(dedupe).second) continue;
        const char* opName = nullptr;
        if (kind == "sym") opName = "sch_delete_symbol";
        else if (kind == "wire") opName = "sch_delete_wire";
        else if (kind == "junction") opName = "sch_delete_junction";
        else if (kind == "label") opName = "sch_delete_label";
        else if (kind == "power") opName = "sch_delete_power";
        else if (kind == "nc") opName = "sch_delete_no_connect";
        else if (kind == "bus") opName = "sch_delete_bus";
        else if (kind == "busentry") opName = "sch_delete_bus_entry";
        else if (kind == "text") opName = "sch_delete_text";
        else if (kind == "graphic") opName = "sch_delete_graphic";
        else if (kind == "sheetsym") opName = "sch_delete_sheet_symbol";
        else if (kind == "fp") opName = "pcb_delete_footprint";
        else if (kind == "track") opName = "pcb_delete_track";
        else if (kind == "via") opName = "pcb_delete_via";
        else if (kind == "zone") opName = "pcb_delete_zone";
        if (opName) ops.push_back({{"op", opName}, {"id", id}});
    }
    if (ops.empty()) return false;
    st.ecadPendingOps = std::move(ops);
    st.ecadOpsGo = true;
    es.selected.clear();
    es.editLoadedFor.clear();
    return true;
}

bool focusSelected(EcadEditorState& es) {
    EcadPt p;
    if (!selectedPos(es.mirror, primarySelection(es), p)) return false;
    es.canvas.focusOn(p);
    return true;
}

const char* graphicKind(EcadGraphic::Kind kind) {
    switch (kind) {
        case EcadGraphic::Rect: return "rect";
        case EcadGraphic::Circle: return "circle";
        case EcadGraphic::Arc: return "arc";
        case EcadGraphic::Polyline: return "polyline";
        default: return "line";
    }
}

bool nearPt(const EcadPt& a, const EcadPt& b, float eps) {
    return std::fabs(a.x - b.x) <= eps && std::fabs(a.y - b.y) <= eps;
}

void moveSelected(AppState& st, EcadEditorState& es, float dx, float dy, bool rubberBand) {
    if (std::fabs(dx) < 1e-5f && std::fabs(dy) < 1e-5f) return;
    nlohmann::json ops = nlohmann::json::array();
    std::set<std::string> moved;
    std::vector<EcadPt> wireAnchors;
    bool identitiesChanged = false;
    auto pos = [&](const EcadPt& p) { return nlohmann::json::array({p.x + dx, p.y + dy}); };
    auto movedPts = [&](const std::vector<EcadPt>& pts) {
        nlohmann::json out = nlohmann::json::array();
        for (const auto& p : pts) out.push_back({p.x + dx, p.y + dy});
        return out;
    };
    for (const std::string& token : es.selected) {
        std::string kind, id;
        if (!splitToken(token, kind, id)) continue;
        if (kind == "pin") { kind = "sym"; id = id.substr(0, id.rfind('.')); }
        if (kind == "pad") { kind = "fp"; id = id.substr(0, id.rfind('.')); }
        if (!moved.insert(kind + ":" + id).second) continue;
        if (kind == "sym") for (const auto& x : es.mirror.symbols()) if (x.id == id) {
            ops.push_back({{"op", "sch_edit_symbol"}, {"id", id}, {"pos", pos(x.pos)}});
            for (const auto& pin : x.pins) wireAnchors.push_back(pin.tip);
        } else if (kind == "wire") for (const auto& x : es.mirror.wires()) if (x.id == id) {
            std::vector<EcadPt> pts = x.pts;
            const float endpointTol = std::max(0.01f, es.mirror.grid() * 1.5f);
            const bool primary = token == primarySelection(es);
            const bool atFront = primary && !pts.empty() &&
                std::hypot(es.dragStart.x - pts.front().x, es.dragStart.y - pts.front().y) <= endpointTol;
            const bool atBack = primary && !pts.empty() &&
                std::hypot(es.dragStart.x - pts.back().x, es.dragStart.y - pts.back().y) <= endpointTol;
            if (atFront || atBack) {
                EcadPt& endpoint = atFront ? pts.front() : pts.back();
                wireAnchors.push_back(endpoint);
                endpoint.x += dx; endpoint.y += dy;
                nlohmann::json jp = nlohmann::json::array();
                for (const auto& p : pts) jp.push_back({p.x, p.y});
                ops.push_back({{"op", "sch_edit_wire"}, {"id", id}, {"pts", jp}});
            } else {
                ops.push_back({{"op", "sch_edit_wire"}, {"id", id}, {"pts", movedPts(x.pts)}});
            }
        } else if (kind == "bus") for (const auto& x : es.mirror.buses()) if (x.id == id) {
            ops.push_back({{"op", "sch_edit_bus"}, {"id", id}, {"pts", movedPts(x.pts)}});
        } else if (kind == "junction") for (const auto& x : es.mirror.junctions()) if (x.id == id) {
            ops.push_back({{"op", "sch_edit_junction"}, {"id", id}, {"pos", pos(x.pos)}});
            wireAnchors.push_back(x.pos);
        } else if (kind == "label") for (const auto& x : es.mirror.labels()) if (x.id == id) {
            ops.push_back({{"op", "sch_edit_label"}, {"id", id}, {"pos", pos(x.pos)}});
            wireAnchors.push_back(x.pos);
        } else if (kind == "power") for (const auto& x : es.mirror.powers()) if (x.id == id) {
            ops.push_back({{"op", "sch_edit_power"}, {"id", id}, {"pos", pos(x.pos)}});
            wireAnchors.push_back(x.pos);
        } else if (kind == "nc") for (const auto& x : es.mirror.noConnects()) if (x.id == id) {
            ops.push_back({{"op", "sch_delete_no_connect"}, {"id", id}});
            ops.push_back({{"op", "sch_add_no_connect"}, {"pos", pos(x.pos)}, {"sheet", x.sheet}});
            identitiesChanged = true;
        } else if (kind == "busentry") for (const auto& x : es.mirror.busEntries()) if (x.id == id) {
            ops.push_back({{"op", "sch_delete_bus_entry"}, {"id", id}});
            nlohmann::json op{{"op", "sch_add_bus_entry"}, {"pos", pos(x.pos)},
                              {"size", {x.size.x, x.size.y}}, {"sheet", x.sheet}};
            if (!x.bus.empty()) {
                std::string busName = x.bus;
                for (const auto& bus : es.mirror.buses())
                    if (bus.id == x.bus) { busName = bus.name; break; }
                op["bus"] = busName;
            }
            if (!x.member.empty()) op["member"] = x.member;
            ops.push_back(std::move(op));
            identitiesChanged = true;
        } else if (kind == "text") for (const auto& x : es.mirror.texts()) if (x.id == id) {
            ops.push_back({{"op", "sch_edit_text"}, {"id", id}, {"pos", pos(x.pos)}});
        } else if (kind == "graphic") for (const auto& x : es.mirror.graphics()) if (x.id == id) {
            ops.push_back({{"op", "sch_edit_graphic"}, {"id", id}, {"pts", movedPts(x.pts)}});
        } else if (kind == "sheetsym") for (const auto& x : es.mirror.sheetSymbols()) if (x.id == id) {
            ops.push_back({{"op", "sch_edit_sheet_symbol"}, {"id", id}, {"pos", pos(x.pos)}});
        } else if (kind == "fp") for (const auto& x : es.mirror.footprints()) if (x.id == id) {
            ops.push_back({{"op", "pcb_edit_footprint"}, {"id", id}, {"pos", pos(x.pos)}});
        } else if (kind == "track") for (const auto& x : es.mirror.tracks()) if (x.id == id) {
            ops.push_back({{"op", "pcb_delete_track"}, {"id", id}});
            nlohmann::json op{{"op", "pcb_add_track"}, {"layer", x.layer}, {"width", x.width},
                              {"p1", pos(x.p1)}, {"p2", pos(x.p2)}};
            if (!x.net.empty()) op["net"] = x.net;
            ops.push_back(std::move(op));
            identitiesChanged = true;
        } else if (kind == "via") for (const auto& x : es.mirror.vias()) if (x.id == id) {
            ops.push_back({{"op", "pcb_delete_via"}, {"id", id}});
            nlohmann::json op{{"op", "pcb_add_via"}, {"at", pos(x.at)}, {"drill", x.drill},
                              {"diameter", x.diameter}, {"layers", x.layers}, {"type", x.type}};
            if (!x.net.empty()) op["net"] = x.net;
            ops.push_back(std::move(op));
            identitiesChanged = true;
        } else if (kind == "zone") for (const auto& x : es.mirror.zones()) if (x.id == id) {
            ops.push_back({{"op", "pcb_edit_zone"}, {"id", id},
                           {"polygon", movedPts(x.polygon)}});
        }
    }
    if (rubberBand && !wireAnchors.empty()) {
        const float eps = std::max(0.01f, es.mirror.grid() * 0.05f);
        for (const auto& wire : es.mirror.wires()) {
            if (moved.count("wire:" + wire.id)) continue;
            std::vector<EcadPt> pts = wire.pts;
            bool changed = false;
            for (auto& p : pts) for (const auto& anchor : wireAnchors)
                if (nearPt(p, anchor, eps)) { p.x += dx; p.y += dy; changed = true; break; }
            if (changed) {
                nlohmann::json jp = nlohmann::json::array();
                for (const auto& p : pts) jp.push_back({p.x, p.y});
                ops.push_back({{"op", "sch_edit_wire"}, {"id", wire.id}, {"pts", jp}});
            }
        }
    }
    if (!ops.empty()) {
        st.ecadPendingOps = std::move(ops);
        st.ecadOpsGo = true;
        if (identitiesChanged) es.selected.clear();
    }
}

bool insideRect(const EcadPt& p, const EcadPt& a, const EcadPt& b) {
    return p.x >= std::min(a.x, b.x) && p.x <= std::max(a.x, b.x) &&
           p.y >= std::min(a.y, b.y) && p.y <= std::max(a.y, b.y);
}

std::vector<std::string> marqueeSelection(const EcadMirror& m, const EcadPt& a, const EcadPt& b) {
    std::vector<std::string> out;
    auto add = [&](const char* k, const std::string& id, const EcadPt& p, const std::string& sheet) {
        if (m.onActiveSheet(sheet) && insideRect(p, a, b)) out.push_back(std::string(k) + ":" + id);
    };
    if (m.isSch()) {
        for (const auto& x : m.symbols()) if (m.onActiveSheet(x.sheet)) {
            add("sym", x.id, x.pos, x.sheet);
            for (const auto& p : x.pins) add("pin", x.id + "." + p.number, p.tip, x.sheet);
        }
        for (const auto& x : m.wires()) for (const auto& p : x.pts) add("wire", x.id, p, x.sheet);
        for (const auto& x : m.buses()) for (const auto& p : x.pts) add("bus", x.id, p, x.sheet);
        for (const auto& x : m.busEntries()) add("busentry", x.id, x.pos, x.sheet);
        for (const auto& x : m.junctions()) add("junction", x.id, x.pos, x.sheet);
        for (const auto& x : m.labels()) add("label", x.id, x.pos, x.sheet);
        for (const auto& x : m.powers()) add("power", x.id, x.pos, x.sheet);
        for (const auto& x : m.noConnects()) add("nc", x.id, x.pos, x.sheet);
        for (const auto& x : m.texts()) add("text", x.id, x.pos, x.sheet);
        for (const auto& x : m.graphics()) for (const auto& p : x.pts) add("graphic", x.id, p, x.sheet);
        for (const auto& x : m.sheetSymbols()) add("sheetsym", x.id, x.pos, x.sheet);
    } else {
        for (const auto& x : m.footprints()) {
            add("fp", x.id, x.pos, "");
            for (const auto& p : x.pads) add("pad", x.id + "." + p.number, p.pos, "");
        }
        for (const auto& x : m.tracks()) { add("track", x.id, x.p1, ""); add("track", x.id, x.p2, ""); }
        for (const auto& x : m.vias()) add("via", x.id, x.at, "");
        for (const auto& x : m.zones()) for (const auto& p : x.polygon)
            add("zone", x.id, p, "");
    }
    std::vector<std::string> unique;
    for (const auto& token : out)
        if (std::find(unique.begin(), unique.end(), token) == unique.end()) unique.push_back(token);
    return unique;
}

void handleSelectEdit(AppState& st, EcadEditorState& es, const EcadCanvasIO& io) {
    const float g = es.mirror.isSch() ? std::max(0.01f, es.mirror.grid())
                                      : std::max(0.01f, es.snapGrid);
    const float worldTol = 9.0f / std::max(0.01f, es.canvas.zoom());
    if (io.hovered && io.hasWorldMouse && !io.leftDragging && !io.leftDragStarted) {
        const std::string hover = hitTest(es, io.worldMouse, worldTol);
        if (!hover.empty()) ImGui::SetTooltip("%s", hover.c_str());
    }
    if (io.leftDragStarted && io.hasWorldMouse) {
        es.dragStart = io.dragStartWorld;
        const std::string hit = hitTest(es, io.dragStartWorld, worldTol);
        if (!hit.empty()) {
            if (!hasSelection(es, hit))
                applyPick(es, hit, io.ctrl || ImGui::GetIO().KeySuper, io.shift);
            es.dragging = true;
        } else {
            es.marquee = true;
            es.marqueeEnd = io.worldMouse;
            if (!io.ctrl && !ImGui::GetIO().KeySuper && !io.shift) es.selected.clear();
        }
    }
    if (es.dragging && io.hasWorldMouse) {
        es.dragDx = io.worldMouse.x - es.dragStart.x;
        es.dragDy = io.worldMouse.y - es.dragStart.y;
        if (io.leftDragEnded) {
            const float dx = snapTo(es.dragStart.x + es.dragDx, g) - snapTo(es.dragStart.x, g);
            const float dy = snapTo(es.dragStart.y + es.dragDy, g) - snapTo(es.dragStart.y, g);
            moveSelected(st, es, dx, dy, !io.alt);
            es.dragging = false;
            es.dragDx = es.dragDy = 0.0f;
        }
        return;
    }
    if (es.marquee && io.hasWorldMouse) {
        es.marqueeEnd = io.worldMouse;
        if (io.leftDragEnded) {
            const auto hits = marqueeSelection(es.mirror, es.dragStart, es.marqueeEnd);
            for (const auto& token : hits) applyPick(es, token, false, true);
            es.marquee = false;
        }
        return;
    }
    es.dragDx = es.dragDy = 0.0f;
    if (!es.selected.empty() && io.hovered && !ImGui::GetIO().WantTextInput) {
        if (ImGui::IsKeyPressed(ImGuiKey_R)) { rotateOrFlipSelected(st, es, true); return; }
        if (ImGui::IsKeyPressed(ImGuiKey_F)) { rotateOrFlipSelected(st, es, false); return; }
        if (ImGui::IsKeyPressed(ImGuiKey_Delete)) { deleteSelected(st, es); return; }
    }
    if (io.leftClicked && io.hasWorldMouse) {
        applyPick(es, resolvePick(st, es, io.worldMouse, worldTol),
                  io.ctrl || ImGui::GetIO().KeySuper, io.shift);
    }
}

struct RouteSnap {
    EcadPt p{};
    bool active = false;
    std::string label;
    std::string sym, pin;
};

RouteSnap routeSnap(const EcadMirror& m, const EcadPt& mouse, float grid) {
    RouteSnap out; out.p = {snapTo(mouse.x, grid), snapTo(mouse.y, grid)};
    float best = grid * 1.5f;
    auto take = [&](const EcadPt& p, const std::string& label,
                    const std::string& sym = {}, const std::string& pin = {}) {
        const float d = std::hypot(mouse.x - p.x, mouse.y - p.y);
        if (d < best) { best = d; out = {p, true, label, sym, pin}; }
    };
    for (const auto& s : m.symbols()) if (m.onActiveSheet(s.sheet))
        for (const auto& p : s.pins)
            take(p.tip, s.ref + "." + p.number, s.id, p.number);
    for (const auto& j : m.junctions()) if (m.onActiveSheet(j.sheet))
        take(j.pos, "Junction");
    for (const auto& w : m.wires()) if (m.onActiveSheet(w.sheet) && !w.pts.empty()) {
        take(w.pts.front(), "Wire endpoint");
        take(w.pts.back(), "Wire endpoint");
    }
    return out;
}

std::vector<EcadPt> orthogonalPath(const EcadPt& a, const EcadPt& b, bool horizontalFirst) {
    if (nearPt(a, b, 1e-6f)) return {a};
    if (std::fabs(a.x - b.x) < 1e-6f || std::fabs(a.y - b.y) < 1e-6f) return {a, b};
    return horizontalFirst ? std::vector<EcadPt>{a, {b.x, a.y}, b}
                           : std::vector<EcadPt>{a, {a.x, b.y}, b};
}

bool wireInteriorAt(const EcadMirror& m, const EcadPt& p, float eps) {
    for (const auto& w : m.wires()) if (m.onActiveSheet(w.sheet))
        for (size_t i = 1; i < w.pts.size(); ++i) {
            if (nearPt(p, w.pts[i - 1], eps) || nearPt(p, w.pts[i], eps)) continue;
            if (ptSegDistP(p, w.pts[i - 1], w.pts[i]) <= eps) return true;
        }
    return false;
}

void stopRoute(EcadEditorState& es) {
    es.routing = false;
    es.routePath.clear();
    es.routeStartSym.clear();
    es.routeStartPin.clear();
    es.canvas.setRoutePreview({}, false);
    es.canvas.setSnapTarget(false, {}, "");
}

void handleCanvasEdit(AppState& st, EcadEditorState& es, const EcadCanvasIO& io) {
    if (st.ecadTool == 0) { handleSelectEdit(st, es, io); return; }
    if (es.routing && ImGui::IsKeyPressed(ImGuiKey_Space))
        es.routeHorizontalFirst = !es.routeHorizontalFirst;
    if (es.routing && (ImGui::IsKeyPressed(ImGuiKey_Escape) || io.rightClicked)) {
        stopRoute(es);
        return;
    }
    if (!io.hasWorldMouse) return;
    const bool sch = es.mirror.isSch();
    const float g = sch ? std::max(0.01f, es.mirror.grid()) : std::max(0.01f, es.snapGrid);
    const RouteSnap snap = sch ? routeSnap(es.mirror, io.worldMouse, g)
                               : RouteSnap{{snapTo(io.worldMouse.x, g), snapTo(io.worldMouse.y, g)}};
    const EcadPt p = snap.p;
    es.canvas.setSnapTarget(snap.active, p, snap.label);
    if (es.routing) {
        std::vector<EcadPt> preview = es.routePath;
        const auto tail = orthogonalPath(es.routeAnchor, p, es.routeHorizontalFirst);
        if (preview.empty()) preview.push_back(es.routeAnchor);
        for (size_t i = 1; i < tail.size(); ++i) preview.push_back(tail[i]);
        es.canvas.setRoutePreview(preview, es.routeBus);
    } else {
        es.canvas.setRoutePreview({}, false);
    }
    if (!io.leftClicked) return;
    nlohmann::json ops = nlohmann::json::array();
    if (st.ecadTool == 1) {
        if (sch) {
            static const char* scopes[] = {"local", "global", "hier"};
            if (es.schPlaceKind == 0)
                ops.push_back({{"op", "sch_add_symbol"}, {"libid", es.placeLib},
                               {"pos", {p.x, p.y}}, {"sheet", es.mirror.activeSheet()}});
            else if (es.schPlaceKind == 1 && es.placeLabel[0])
                ops.push_back({{"op", "sch_add_label"}, {"pos", {p.x, p.y}},
                               {"text", es.placeLabel}, {"scope", scopes[es.placeLabelScope]},
                               {"sheet", es.mirror.activeSheet()}});
            else if (es.schPlaceKind == 2 && es.placePowerNet[0])
                ops.push_back({{"op", "sch_add_power"}, {"pos", {p.x, p.y}},
                               {"net", es.placePowerNet}, {"symbol", es.placePowerSymbol},
                               {"sheet", es.mirror.activeSheet()}});
            else if (es.schPlaceKind == 3)
                ops.push_back({{"op", "sch_add_junction"}, {"pos", {p.x, p.y}},
                               {"sheet", es.mirror.activeSheet()}});
            else if (es.schPlaceKind == 4)
                ops.push_back({{"op", "sch_add_no_connect"}, {"pos", {p.x, p.y}},
                               {"sheet", es.mirror.activeSheet()}});
            else if (es.schPlaceKind == 5 && es.placeText[0])
                ops.push_back({{"op", "sch_add_text"}, {"pos", {p.x, p.y}},
                               {"text", es.placeText}, {"sheet", es.mirror.activeSheet()}});
        } else if (es.pcbPlaceKind == 0) {
            ops.push_back({{"op", "pcb_add_footprint"}, {"fpid", es.placeFp},
                           {"pos", {p.x, p.y}}});
        } else if (es.pcbPlaceKind == 1) {
            nlohmann::json op{{"op", "pcb_add_via"}, {"at", {p.x, p.y}},
                              {"drill", std::max(0.05f, es.viaDrill)},
                              {"diameter", std::max(es.viaDiameter, es.viaDrill + 0.10f)}};
            if (!es.activeNet.empty()) op["net"] = es.activeNet;
            ops.push_back(std::move(op));
        }
    } else if (st.ecadTool == 2) {
        if (!es.routing) {
            es.routing = true;
            es.routeAnchor = p;
            es.routeStartSym = snap.sym;
            es.routeStartPin = snap.pin;
            es.routePath = {p};
        } else if (es.routeBus || es.routeZone) {
            if (!nearPt(es.routePath.back(), p, 1e-6f)) es.routePath.push_back(p);
            es.routeAnchor = p;
            if (io.leftDoubleClicked) {
                if (es.routeBus && es.routePath.size() >= 2) {
                    if (es.placeBusName[0] == '\0')
                        st.toast(ui::ToastLevel::Warn, "Enter a bus name before routing");
                    else
                        ops.push_back({{"op", "sch_add_bus"}, {"name", es.placeBusName},
                                       {"pts", nlohmann::json::array()},
                                       {"sheet", es.mirror.activeSheet()}});
                } else if (es.routeZone && es.routePath.size() >= 3) {
                    if (es.activeNet.empty()) {
                        st.toast(ui::ToastLevel::Warn, "Choose an active net before drawing a zone");
                    } else {
                        ops.push_back({{"op", "pcb_add_zone"}, {"layer", es.activeLayer},
                                       {"net", es.activeNet},
                                       {"polygon", nlohmann::json::array()}});
                    }
                }
                if (!ops.empty()) for (const auto& q : es.routePath)
                    ops.back()[es.routeBus ? "pts" : "polygon"].push_back({q.x, q.y});
                stopRoute(es);
            }
        } else {
            const auto pts = orthogonalPath(es.routeAnchor, p, es.routeHorizontalFirst);
            if (pts.size() >= 2) {
                if (sch && !es.routeStartSym.empty() && !snap.sym.empty()) {
                    ops.push_back({{"op", "sch_connect"},
                                   {"from", {{"sym", es.routeStartSym}, {"pin", es.routeStartPin}}},
                                   {"to", {{"sym", snap.sym}, {"pin", snap.pin}}},
                                   {"sheet", es.mirror.activeSheet()}});
                    stopRoute(es);
                } else if (sch) {
                    nlohmann::json jp = nlohmann::json::array();
                    for (const auto& q : pts) jp.push_back({q.x, q.y});
                    ops.push_back({{"op", "sch_add_wire"}, {"pts", jp},
                                   {"sheet", es.mirror.activeSheet()}});
                    const float junctionEps = std::max(0.01f, g * 0.05f);
                    if (wireInteriorAt(es.mirror, es.routeAnchor, junctionEps))
                        ops.push_back({{"op", "sch_add_junction"},
                                       {"pos", {es.routeAnchor.x, es.routeAnchor.y}},
                                       {"sheet", es.mirror.activeSheet()}});
                    if (wireInteriorAt(es.mirror, p, junctionEps))
                        ops.push_back({{"op", "sch_add_junction"}, {"pos", {p.x, p.y}},
                                       {"sheet", es.mirror.activeSheet()}});
                } else {
                    for (size_t i = 1; i < pts.size(); ++i) {
                        nlohmann::json op{{"op", "pcb_add_track"}, {"layer", es.activeLayer},
                                          {"width", std::max(0.01f, es.routeWidth)},
                                          {"p1", {pts[i - 1].x, pts[i - 1].y}},
                                          {"p2", {pts[i].x, pts[i].y}}};
                        if (!es.activeNet.empty()) op["net"] = es.activeNet;
                        ops.push_back(std::move(op));
                    }
                }
                es.routeAnchor = p;
                es.routePath = {p};
                es.routeStartSym = snap.sym;
                es.routeStartPin = snap.pin;
                if (io.leftDoubleClicked || !snap.sym.empty()) stopRoute(es);
            }
        }
    }
    if (!ops.empty()) { st.ecadPendingOps = std::move(ops); st.ecadOpsGo = true; }
}

void copySelection(EcadEditorState& es) {
    es.clipboard = nlohmann::json::array();
    std::set<std::string> copied;
    for (const std::string& token : es.selected) {
        std::string kind, id;
        if (!splitToken(token, kind, id)) continue;
        if (kind == "pin") { kind = "sym"; id = id.substr(0, id.rfind('.')); }
        if (kind == "pad") { kind = "fp"; id = id.substr(0, id.rfind('.')); }
        if (!copied.insert(kind + ":" + id).second) continue;
        nlohmann::json d;
        if (kind == "sym") for (const auto& x : es.mirror.symbols()) if (x.id == id)
            d = {{"libid", x.libid}, {"ref", x.ref}, {"value", x.value},
                 {"pos", {x.pos.x, x.pos.y}}, {"angle", x.angle}, {"mirror", x.mirror},
                 {"footprint", x.footprint}, {"datasheet", x.datasheet}, {"dnp", x.dnp},
                 {"in_bom", x.inBom}, {"on_board", x.onBoard}, {"unit", x.unit},
                 {"fields", x.fields}, {"sheet", x.sheet}};
        else if (kind == "wire") for (const auto& x : es.mirror.wires()) if (x.id == id) {
            d = {{"pts", nlohmann::json::array()}, {"bus", x.bus}, {"sheet", x.sheet}};
            for (const auto& p : x.pts) d["pts"].push_back({p.x, p.y});
        } else if (kind == "bus") for (const auto& x : es.mirror.buses()) if (x.id == id) {
            d = {{"name", x.name}, {"pts", nlohmann::json::array()}, {"sheet", x.sheet}};
            for (const auto& p : x.pts) d["pts"].push_back({p.x, p.y});
        } else if (kind == "busentry") for (const auto& x : es.mirror.busEntries()) if (x.id == id) {
            std::string busName = x.bus;
            for (const auto& bus : es.mirror.buses())
                if (bus.id == x.bus) { busName = bus.name; break; }
            d = {{"pos", {x.pos.x, x.pos.y}}, {"size", {x.size.x, x.size.y}},
                 {"bus", busName}, {"member", x.member}, {"sheet", x.sheet}};
        }
        else if (kind == "junction") for (const auto& x : es.mirror.junctions()) if (x.id == id)
            d = {{"pos", {x.pos.x, x.pos.y}}, {"sheet", x.sheet}};
        else if (kind == "label") for (const auto& x : es.mirror.labels()) if (x.id == id)
            d = {{"pos", {x.pos.x, x.pos.y}}, {"text", x.text}, {"scope", x.scope},
                 {"angle", x.angle}, {"sheet", x.sheet}};
        else if (kind == "power") for (const auto& x : es.mirror.powers()) if (x.id == id)
            d = {{"pos", {x.pos.x, x.pos.y}}, {"net", x.net}, {"symbol", x.symbol},
                 {"angle", x.angle}, {"sheet", x.sheet}};
        else if (kind == "nc") for (const auto& x : es.mirror.noConnects()) if (x.id == id)
            d = {{"pos", {x.pos.x, x.pos.y}}, {"sheet", x.sheet}};
        else if (kind == "text") for (const auto& x : es.mirror.texts()) if (x.id == id)
            d = {{"pos", {x.pos.x, x.pos.y}}, {"text", x.text}, {"size", x.size},
                 {"angle", x.angle}, {"sheet", x.sheet}};
        else if (kind == "graphic") for (const auto& x : es.mirror.graphics()) if (x.id == id) {
            d = {{"kind", graphicKind(x.kind)}, {"pts", nlohmann::json::array()},
                 {"radius", x.radius}, {"a0", x.a0}, {"a1", x.a1}, {"width", x.width},
                 {"filled", x.filled}, {"sheet", x.sheet}};
            for (const auto& p : x.pts) d["pts"].push_back({p.x, p.y});
        } else if (kind == "sheetsym") for (const auto& x : es.mirror.sheetSymbols()) if (x.id == id)
            d = {{"child", x.child}, {"pos", {x.pos.x, x.pos.y}},
                 {"size", {x.size.x, x.size.y}}, {"sheet", x.sheet}};
        else if (kind == "fp") for (const auto& x : es.mirror.footprints()) if (x.id == id)
            d = {{"fpid", x.fpid}, {"ref", x.ref}, {"value", x.value},
                 {"pos", {x.pos.x, x.pos.y}}, {"angle", x.angle}, {"side", x.side}};
        else if (kind == "track") for (const auto& x : es.mirror.tracks()) if (x.id == id)
            d = {{"p1", {x.p1.x, x.p1.y}}, {"p2", {x.p2.x, x.p2.y}},
                 {"net", x.net}, {"layer", x.layer}, {"width", x.width}};
        else if (kind == "via") for (const auto& x : es.mirror.vias()) if (x.id == id)
            d = {{"at", {x.at.x, x.at.y}}, {"net", x.net}, {"drill", x.drill},
                 {"diameter", x.diameter}, {"layers", x.layers}, {"type", x.type}};
        else if (kind == "zone") for (const auto& x : es.mirror.zones()) if (x.id == id) {
            d = {{"polygon", nlohmann::json::array()}, {"net", x.net},
                 {"layer", x.layer}, {"fill", x.fill}};
            for (const auto& p : x.polygon) d["polygon"].push_back({p.x, p.y});
        }
        if (!d.is_null()) es.clipboard.push_back({{"kind", kind}, {"data", std::move(d)}});
    }
    es.pasteSerial = 0;
}

void offsetJsonPoint(nlohmann::json& p, float d) {
    if (p.is_array() && p.size() >= 2 && p[0].is_number() && p[1].is_number()) {
        p[0] = p[0].get<float>() + d;
        p[1] = p[1].get<float>() + d;
    }
}

bool pasteClipboard(AppState& st, EcadEditorState& es) {
    if (!es.clipboard.is_array() || es.clipboard.empty()) return false;
    const float offset = (es.mirror.isSch() ? es.mirror.grid() : es.snapGrid) * ++es.pasteSerial;
    nlohmann::json ops = nlohmann::json::array();
    std::unordered_map<std::string, std::string> busNames;
    for (const auto& r : es.clipboard) if (r.value("kind", std::string()) == "bus") {
        nlohmann::json d = r["data"];
        const std::string old = d.value("name", std::string("BUS"));
        const std::string name = old + "_copy" + std::to_string(es.pasteSerial);
        busNames[old] = name;
        for (auto& p : d["pts"]) offsetJsonPoint(p, offset);
        ops.push_back({{"op", "sch_add_bus"}, {"name", name}, {"pts", d["pts"]},
                       {"sheet", es.mirror.activeSheet()}});
    }
    for (const auto& r : es.clipboard) {
        const std::string kind = r.value("kind", std::string());
        nlohmann::json d = r["data"];
        if (kind == "bus") continue;
        auto shift = [&](const char* key) { if (d.contains(key)) offsetJsonPoint(d[key], offset); };
        shift("pos"); shift("at"); shift("p1"); shift("p2");
        if (d.contains("pts")) for (auto& p : d["pts"]) offsetJsonPoint(p, offset);
        if (d.contains("polygon")) for (auto& p : d["polygon"]) offsetJsonPoint(p, offset);
        nlohmann::json op;
        if (kind == "sym") {
            op = {{"op", "sch_add_symbol"}, {"libid", d["libid"]}, {"pos", d["pos"]},
                  {"angle", d["angle"]}, {"mirror", d["mirror"]}, {"value", d["value"]},
                  {"footprint", d["footprint"]}, {"datasheet", d["datasheet"]},
                  {"dnp", d["dnp"]}, {"in_bom", d["in_bom"]}, {"on_board", d["on_board"]},
                  {"unit", d["unit"]}, {"fields", d["fields"]}, {"sheet", es.mirror.activeSheet()}};
        } else if (kind == "wire") op = {{"op", "sch_add_wire"}, {"pts", d["pts"]},
                                          {"bus", d.value("bus", false)},
                                          {"sheet", es.mirror.activeSheet()}};
        else if (kind == "busentry") {
            op = {{"op", "sch_add_bus_entry"}, {"pos", d["pos"]}, {"size", d["size"]},
                  {"sheet", es.mirror.activeSheet()}};
            const std::string old = d.value("bus", std::string());
            if (!old.empty()) op["bus"] = busNames.count(old) ? busNames[old] : old;
            if (!d.value("member", std::string()).empty()) op["member"] = d["member"];
        } else if (kind == "junction") op = {{"op", "sch_add_junction"}, {"pos", d["pos"]},
                                              {"sheet", es.mirror.activeSheet()}};
        else if (kind == "label") op = {{"op", "sch_add_label"}, {"pos", d["pos"]},
                                         {"text", d["text"]}, {"scope", d["scope"]},
                                         {"angle", d["angle"]}, {"sheet", es.mirror.activeSheet()}};
        else if (kind == "power") op = {{"op", "sch_add_power"}, {"pos", d["pos"]},
                                         {"net", d["net"]}, {"symbol", d["symbol"]},
                                         {"angle", d["angle"]}, {"sheet", es.mirror.activeSheet()}};
        else if (kind == "nc") op = {{"op", "sch_add_no_connect"}, {"pos", d["pos"]},
                                      {"sheet", es.mirror.activeSheet()}};
        else if (kind == "text") op = {{"op", "sch_add_text"}, {"pos", d["pos"]},
                                        {"text", d["text"]}, {"size", d["size"]},
                                        {"angle", d["angle"]}, {"sheet", es.mirror.activeSheet()}};
        else if (kind == "graphic") op = {{"op", "sch_add_graphic"}, {"kind", d["kind"]},
                                           {"pts", d["pts"]}, {"radius", d["radius"]},
                                           {"a0", d["a0"]}, {"a1", d["a1"]},
                                           {"width", d["width"]}, {"filled", d["filled"]},
                                           {"sheet", es.mirror.activeSheet()}};
        else if (kind == "sheetsym") op = {{"op", "sch_add_sheet_symbol"}, {"child", d["child"]},
                                            {"pos", d["pos"]}, {"size", d["size"]},
                                            {"sheet", es.mirror.activeSheet()}};
        else if (kind == "fp") op = {{"op", "pcb_add_footprint"}, {"fpid", d["fpid"]},
                                      {"pos", d["pos"]}, {"angle", d["angle"]},
                                      {"side", d["side"]}, {"value", d["value"]}};
        else if (kind == "track") {
            op = {{"op", "pcb_add_track"}, {"p1", d["p1"]}, {"p2", d["p2"]},
                  {"layer", d["layer"]}, {"width", d["width"]}};
            if (!d.value("net", std::string()).empty()) op["net"] = d["net"];
        } else if (kind == "via") {
            op = {{"op", "pcb_add_via"}, {"at", d["at"]}, {"drill", d["drill"]},
                  {"diameter", d["diameter"]}, {"layers", d["layers"]}, {"type", d["type"]}};
            if (!d.value("net", std::string()).empty()) op["net"] = d["net"];
        } else if (kind == "zone")
            op = {{"op", "pcb_add_zone"}, {"polygon", d["polygon"]}, {"net", d["net"]},
                  {"layer", d["layer"]}, {"fill", d["fill"]}};
        if (!op.is_null()) ops.push_back(std::move(op));
    }
    if (ops.empty()) return false;
    st.ecadPendingOps = std::move(ops);
    st.ecadOpsGo = true;
    es.selected.clear();
    return true;
}

void selectAll(EcadEditorState& es) {
    es.selected.clear();
    const EcadMirror& m = es.mirror;
    auto add = [&](const char* k, const std::string& id, const std::string& sheet = {}) {
        if (m.onActiveSheet(sheet)) es.selected.push_back(std::string(k) + ":" + id);
    };
    if (m.isSch()) {
        for (const auto& x : m.symbols()) add("sym", x.id, x.sheet);
        for (const auto& x : m.wires()) add("wire", x.id, x.sheet);
        for (const auto& x : m.buses()) add("bus", x.id, x.sheet);
        for (const auto& x : m.busEntries()) add("busentry", x.id, x.sheet);
        for (const auto& x : m.junctions()) add("junction", x.id, x.sheet);
        for (const auto& x : m.labels()) add("label", x.id, x.sheet);
        for (const auto& x : m.powers()) add("power", x.id, x.sheet);
        for (const auto& x : m.noConnects()) add("nc", x.id, x.sheet);
        for (const auto& x : m.texts()) add("text", x.id, x.sheet);
        for (const auto& x : m.graphics()) add("graphic", x.id, x.sheet);
        for (const auto& x : m.sheetSymbols()) add("sheetsym", x.id, x.sheet);
    } else {
        for (const auto& x : m.footprints()) add("fp", x.id);
        for (const auto& x : m.tracks()) add("track", x.id);
        for (const auto& x : m.vias()) add("via", x.id);
        for (const auto& x : m.zones()) add("zone", x.id);
    }
}

// ---- ECAD manager: dense per-document Design / Inspect / Checks command center -----------------
bool containsFolded(const std::string& text, const char* query) {
    if (!query || *query == '\0') return true;
    const std::string needle(query);
    return std::search(text.begin(), text.end(), needle.begin(), needle.end(),
        [](char a, char b) {
            return std::tolower(static_cast<unsigned char>(a)) ==
                   std::tolower(static_cast<unsigned char>(b));
        }) != text.end();
}

std::string pointText(const EcadPt& p) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.2f, %.2f mm", p.x, p.y);
    return buf;
}

std::string mmText(float value) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%.3f mm", value);
    return buf;
}

void ensurePcbContext(EcadEditorState& es) {
    if (!es.mirror.isPcb()) return;
    const auto& layers = es.mirror.layers();
    const bool haveLayer = std::any_of(layers.begin(), layers.end(),
        [&](const EcadLayer& layer) { return layer.id == es.activeLayer; });
    if (!haveLayer && !layers.empty()) es.activeLayer = layers.front().id;
    if (!es.activeNet.empty()) {
        const auto& nets = es.mirror.nets();
        const bool haveNet = std::any_of(nets.begin(), nets.end(),
            [&](const EcadNet& net) { return net.name == es.activeNet; });
        if (!haveNet) es.activeNet.clear();
    }
    es.routeWidth = std::max(0.01f, es.routeWidth);
    es.viaDrill = std::max(0.05f, es.viaDrill);
    es.viaDiameter = std::max(es.viaDiameter, es.viaDrill + 0.10f);
    es.snapGrid = std::max(0.01f, es.snapGrid);
}

void drawManagerToolbar(AppState& st, EcadEditorState& es, bool pcb) {
    const float size = ui::metrics::camBtnH();
    const float gap = ui::kit::rowGap();
    if (ui::kit::iconButton("ecad_save", "save", "Save", true,
                            ui::kit::Tone::Neutral, size))
        st.ecadPanelCmd = "ecad_save";
    ImGui::SameLine(0.0f, gap);
    if (ui::kit::iconButton("ecad_open", "open", "Open ECAD or KiCad file", true,
                            ui::kit::Tone::Neutral, size))
        st.ecadPanelCmd = "ecad_open";
    ImGui::SameLine(0.0f, gap);
    if (ui::kit::iconButton("ecad_fit", "zoom_fit", "Zoom to fit", true,
                            ui::kit::Tone::Neutral, size))
        es.canvas.requestFit();
    ImGui::SameLine(0.0f, gap);
    if (ui::kit::iconButton("ecad_undo", "undo", "Undo", true,
                            ui::kit::Tone::Neutral, size))
        st.ecadUndoGo = true;
    ImGui::SameLine(0.0f, gap);
    if (ui::kit::iconButton("ecad_redo", "redo", "Redo", true,
                            ui::kit::Tone::Neutral, size))
        st.ecadRedoGo = true;
    if (!pcb) {
        ImGui::SameLine(0.0f, gap);
        if (ui::kit::iconButton("ecad_annotate", "health_check",
                                "Annotate reference designators", true,
                                ui::kit::Tone::Neutral, size))
            st.ecadPanelCmd = "sch_annotate";
    }
}

void drawLayerHierarchyRow(EcadEditorState& es, const EcadLayer& layer) {
    EcadMirror& m = es.mirror;
    ImGui::PushID(layer.id.c_str());
    const std::string selId = "layer:" + layer.id;
    const bool selected = es.mgrSel == selId;
    bool visible = m.layerVisible(layer.id);
    float overrideColor[3];
    const ImVec4 color = m.layerColor(layer.id, overrideColor)
        ? ImVec4(overrideColor[0], overrideColor[1], overrideColor[2], 1.0f)
        : layerSwatch(layer.copperIndex);
    const float tint[3] = {color.x, color.y, color.z};
    ui::FmRowRect row = ui::fmRowPill(selected, tint);
    ImGui::AlignTextToFramePadding();
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() + ui::metrics::U());
    if (ImGui::Checkbox("##layerVisible", &visible)) m.setLayerVisible(layer.id, visible);
    const bool overVisibility = ImGui::IsItemHovered();
    if (overVisibility) ImGui::SetTooltip(visible ? "Hide layer" : "Show layer");
    ImGui::SameLine(0.0f, ui::metrics::U());
    const ImVec2 swatchSize(ImGui::GetFrameHeight(), ImGui::GetFrameHeight());
    if (ImGui::ColorButton("##layerColor", color,
                           ImGuiColorEditFlags_NoTooltip | ImGuiColorEditFlags_NoDragDrop,
                           swatchSize))
        ImGui::OpenPopup("##layerColorPicker");
    const bool overColor = ImGui::IsItemHovered();
    ImGui::SameLine(0.0f, ui::metrics::U());
    ImGui::TextColored(visible ? v4(ui::palette.text) : v4(ui::palette.textDisabled),
                       "%s", layer.id.c_str());
    ui::fmRowPillEnd(row, selected);
    if (!overVisibility && !overColor && ImGui::IsMouseHoveringRect(row.mn, row.mx) &&
        ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
        es.mgrSel = selId;
        es.activeLayer = layer.id;
    }
    if (ImGui::BeginPopup("##layerColorPicker")) {
        float colorEdit[3] = {color.x, color.y, color.z};
        if (ImGui::ColorPicker3("##color", colorEdit,
                                ImGuiColorEditFlags_NoSidePreview |
                                ImGuiColorEditFlags_NoInputs))
            m.setLayerColor(layer.id, colorEdit[0], colorEdit[1], colorEdit[2]);
        ImGui::EndPopup();
    }
    ImGui::PopID();
}

void drawNetHierarchyRow(EcadEditorState& es, const EcadNet& net, bool withPins) {
    EcadMirror& m = es.mirror;
    ImGui::PushID(net.name.c_str());
    const std::string selId = "net:" + net.name;
    const bool selected = es.mgrSel == selId;
    bool visible = m.netVisible(net.name);
    ui::FmRowRect row = ui::fmRowPill(selected);
    ImGui::AlignTextToFramePadding();
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() + ui::metrics::U());
    if (ImGui::Checkbox("##netVisible", &visible)) m.setNetVisible(net.name, visible);
    const bool overVisibility = ImGui::IsItemHovered();
    if (overVisibility) ImGui::SetTooltip(visible ? "Hide net" : "Show net");
    ImGui::SameLine(0.0f, ui::metrics::U());
    ImGui::TextColored(visible ? v4(ui::palette.text) : v4(ui::palette.textDisabled),
                       "%s", net.name.c_str());
    if (withPins) {
        ImGui::SameLine();
        ImGui::TextColored(v4(ui::palette.textDim), "%d pins", net.pinCount);
    }
    ui::fmRowPillEnd(row, selected);
    if (withPins) {
        std::string members;
        for (const auto& symbol : m.symbols()) for (const auto& pin : symbol.pins)
            if (pin.net == net.name) {
                if (!members.empty()) members += ", ";
                members += symbol.ref + "." + pin.number;
            }
        std::string scope;
        for (const auto& sheet : net.sheets) {
            if (!scope.empty()) scope += ", ";
            auto it = std::find_if(m.sheets().begin(), m.sheets().end(),
                [&](const EcadSheet& sh) { return sh.id == sheet; });
            scope += it == m.sheets().end() ? sheet : it->name;
        }
        if (!net.bus.empty()) {
            if (!scope.empty()) scope += " | ";
            scope += "bus " + net.bus;
        }
        ImGui::Indent(ui::metrics::U(3.0f));
        ImGui::TextColored(v4(ui::palette.textDim), "%s",
                           members.empty() ? "No resolved member pins" : members.c_str());
        if (!scope.empty())
            ImGui::TextColored(v4(ui::palette.textDisabled), "Sheets: %s", scope.c_str());
        ImGui::Unindent(ui::metrics::U(3.0f));
    }
    if (!overVisibility && ImGui::IsMouseHoveringRect(row.mn, row.mx) &&
        ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
        if (!m.isPcb() && selected) {
            es.mgrSel.clear();
            es.canvas.setHighlightedNet(std::string());
        } else {
            es.mgrSel = selId;
            if (m.isPcb()) es.activeNet = net.name;
            es.canvas.setHighlightedNet(net.name);
        }
    }
    ImGui::PopID();
}

void drawPcbEditingContext(EcadEditorState& es) {
    EcadMirror& m = es.mirror;
    ensurePcbContext(es);
    ui::kit::sectionHeader("Routing context", "ecad_route", nullptr, -1, true);
    es.comboItems.clear();
    int layerChoice = -1;
    for (const auto& layer : m.layers()) {
        if (es.activeLayer == layer.id) layerChoice = static_cast<int>(es.comboItems.size());
        es.comboItems.push_back(layer.id.c_str());
    }
    if (ui::kit::fieldEnum("##activeCopper", "Copper layer", &layerChoice,
                           es.comboItems.data(), static_cast<int>(es.comboItems.size()),
                           ui::kit::FieldOpts().withEnumStyle(ui::kit::EnumStyle::Combo))) {
        es.activeLayer = m.layers()[layerChoice].id;
        es.mgrSel = "layer:" + es.activeLayer;
    }
    es.comboItems.clear();
    es.comboItems.push_back("No net");
    int netChoice = 0;
    for (const auto& net : m.nets()) {
        if (es.activeNet == net.name) netChoice = static_cast<int>(es.comboItems.size());
        es.comboItems.push_back(net.name.c_str());
    }
    if (ui::kit::fieldEnum("##activeNet", "Active net", &netChoice,
                           es.comboItems.data(), static_cast<int>(es.comboItems.size()),
                           ui::kit::FieldOpts().withEnumStyle(ui::kit::EnumStyle::Combo))) {
        es.activeNet = netChoice == 0 ? std::string() : m.nets()[netChoice - 1].name;
        if (!es.activeNet.empty()) es.mgrSel = "net:" + es.activeNet;
        else if (es.mgrSel.rfind("net:", 0) == 0) es.mgrSel.clear();
        es.canvas.setHighlightedNet(es.activeNet);
    }
    ui::kit::dimText("Route width (mm)");
    ImGui::SetNextItemWidth(-1.0f);
    if (ImGui::InputFloat("##routeWidth", &es.routeWidth, 0.0f, 0.0f, "%.3f"))
        es.routeWidth = std::max(0.01f, es.routeWidth);
    ui::kit::dimText("Snap grid (mm)");
    ImGui::SetNextItemWidth(-1.0f);
    if (ImGui::InputFloat("##snapGrid", &es.snapGrid, 0.0f, 0.0f, "%.3f"))
        es.snapGrid = std::max(0.01f, es.snapGrid);
}
void loadSymbolCatalog(AppState& st, EcadEditorState& es) {
    const std::string query(es.symbolSearch);
    if (es.catalogLoaded && query == es.catalogQuery) return;
    nlohmann::json result;
    std::string err;
    es.catalogQuery = query;
    es.catalogLoaded = true;
    es.symbolCatalog.clear();
    es.footprintCatalog.clear();
    if (!st.worker.call("ecad_symbols", {{"query", query}, {"limit", 100}}, result, err, 10000)) {
        es.catalogError = err;
        return;
    }
    es.catalogError.clear();
    if (result.contains("symbols") && result["symbols"].is_array())
        for (const auto& symbol : result["symbols"]) if (symbol.is_object()) {
            es.symbolCatalog.push_back(symbol);
            const std::string fp = symbol.value("default_footprint", std::string());
            if (!fp.empty() && std::find(es.footprintCatalog.begin(),
                                         es.footprintCatalog.end(), fp) ==
                               es.footprintCatalog.end())
                es.footprintCatalog.push_back(fp);
        }
}


void drawCanvasToolContext(AppState& st, EcadEditorState& es) {
    const bool pcb = es.mirror.isPcb();
    loadSymbolCatalog(st, es);
    ui::kit::sectionHeader("Canvas tools", nullptr, nullptr, -1, !pcb);
    static const char* labels[] = {"Select", "Place", "Route"};
    static const char* tips[] = {
        "Pick, marquee, move, copy or delete entities",
        "Place the configured schematic or PCB entity",
        "Orthogonal route; Space changes the corner, double-click finishes"};
    if (ui::kit::segmented("##ecadTool", labels, 3, &st.ecadTool,
                           ImGui::GetContentRegionAvail().x, tips)) {
        stopRoute(es);
        es.routeBus = es.routeZone = false;
    }

    if (!pcb) {
        static const float grids[] = {0.254f, 0.5f, 1.0f, 1.27f, 2.54f, 5.08f};
        static const char* gridLabels[] = {
            "0.254 mm", "0.5 mm", "1 mm", "1.27 mm", "2.54 mm", "5.08 mm"};
        int gridChoice = -1;
        for (int i = 0; i < 6; ++i)
            if (std::fabs(es.mirror.grid() - grids[i]) < 1e-5f) gridChoice = i;
        char gridLabel[32];
        std::snprintf(gridLabel, sizeof(gridLabel), "%.3g mm", es.mirror.grid());
        if (ui::kit::fieldEnum("##schGrid", "Schematic grid", &gridChoice, gridLabels, 6,
                               ui::kit::FieldOpts().withEnumStyle(ui::kit::EnumStyle::Combo)
                                   .withPreview(gridLabel))) {
            st.ecadPendingOps = nlohmann::json::array(
                {{{"op", "sch_set_grid"}, {"grid", grids[gridChoice]}}});
            st.ecadOpsGo = true;
        }
    }

    if (st.ecadTool == 1 && pcb) {
        static const char* placeLabels[] = {"Footprint", "Via"};
        ui::kit::segmented("##pcbPlaceKind", placeLabels, 2, &es.pcbPlaceKind,
                           ImGui::GetContentRegionAvail().x);
        if (es.pcbPlaceKind == 0) {
            int footprintChoice = -1;
            for (size_t i = 0; i < es.footprintCatalog.size(); ++i)
                if (es.footprintCatalog[i] == es.placeFp) footprintChoice = static_cast<int>(i);
            if (ui::kit::fieldCombo("##placeFootprint", "Footprint library", &footprintChoice,
                                    es.footprintCatalog,
                                    ui::kit::FieldOpts().withPreview(es.placeFp.c_str())))
                es.placeFp = es.footprintCatalog[footprintChoice];
            if (es.footprintCatalog.empty())
                ui::kit::dimText("No catalog footprint matches the current symbol search.");
        } else {
            ui::kit::dimText("Via drill / diameter (mm)");
            const float fieldW =
                (ImGui::GetContentRegionAvail().x - ui::kit::rowGap()) * 0.5f;
            ImGui::SetNextItemWidth(fieldW);
            if (ImGui::InputFloat("##viaDrill", &es.viaDrill, 0.0f, 0.0f, "%.3f"))
                es.viaDrill = std::max(0.05f, es.viaDrill);
            ImGui::SameLine(0.0f, ui::kit::rowGap());
            ImGui::SetNextItemWidth(fieldW);
            if (ImGui::InputFloat("##viaDiameter", &es.viaDiameter, 0.0f, 0.0f, "%.3f"))
                es.viaDiameter = std::max(es.viaDiameter, es.viaDrill + 0.10f);
        }
    } else if (st.ecadTool == 1 && !pcb) {
        static const char* kinds[] = {
            "Symbol", "Net label", "Power", "Junction", "No-connect", "Text"};
        ui::kit::fieldEnum("##schPlaceKind", "Entity", &es.schPlaceKind, kinds, 6,
                           ui::kit::FieldOpts().withEnumStyle(ui::kit::EnumStyle::Combo));
        if (es.schPlaceKind == 0) {
            ui::kit::dimText("Search symbol library");
            ImGui::SetNextItemWidth(-1.0f);
            if (ImGui::InputTextWithHint("##symbolSearch", "name, description or prefix...",
                                         es.symbolSearch, sizeof(es.symbolSearch)))
                es.catalogLoaded = false;
            loadSymbolCatalog(st, es);
            if (!es.catalogError.empty())
                ui::kit::inlineStatus(es.catalogError.c_str(), ui::kit::Tone::Err);
            for (size_t i = 0; i < es.symbolCatalog.size(); ++i) {
                const auto& symbol = es.symbolCatalog[i];
                const std::string libid = symbol.value("libid", std::string());
                const std::string name = symbol.value("name", libid);
                const std::string desc = symbol.value("description", std::string());
                const std::string fp = symbol.value("default_footprint", std::string());
                const int pins = symbol.value("pin_count", 0);
                std::string detail = desc;
                if (!detail.empty()) detail += " | ";
                detail += std::to_string(pins) + " pins";
                if (!fp.empty()) detail += " | " + fp;
                ui::kit::RowStyle style;
                style.secondary = detail.c_str();
                style.selected = es.placeLib == libid;
                ImGui::PushID(static_cast<int>(i));
                const bool choose = ui::kit::row("##symbolResult", name.c_str(), style);
                ImGui::PopID();
                if (choose) es.placeLib = libid;
            }
            if (es.symbolCatalog.empty() && es.catalogError.empty())
                ui::kit::emptyState("search", "No matching symbols",
                                    "Try a component name, prefix or description.");
        } else if (es.schPlaceKind == 1) {
            ImGui::SetNextItemWidth(-1.0f);
            ImGui::InputText("##placeLabel", es.placeLabel, sizeof(es.placeLabel));
            static const char* scopes[] = {"Local", "Global", "Hierarchical"};
            ui::kit::fieldEnum("##labelScope", "Scope", &es.placeLabelScope, scopes, 3,
                               ui::kit::FieldOpts().withEnumStyle(ui::kit::EnumStyle::Combo));
        } else if (es.schPlaceKind == 2) {
            ImGui::SetNextItemWidth(-1.0f);
            ImGui::InputText("##powerNet", es.placePowerNet, sizeof(es.placePowerNet));
            static const char* powers[] = {"GND", "VCC", "+5V", "+3V3"};
            int powerChoice = -1;
            for (int i = 0; i < 4; ++i)
                if (es.placePowerSymbol == powers[i]) powerChoice = i;
            if (ui::kit::fieldEnum("##powerSymbol", "Power symbol", &powerChoice, powers, 4,
                                   ui::kit::FieldOpts().withEnumStyle(ui::kit::EnumStyle::Combo)
                                       .withPreview(es.placePowerSymbol.c_str())))
                es.placePowerSymbol = powers[powerChoice];
        } else if (es.schPlaceKind == 5) {
            ImGui::SetNextItemWidth(-1.0f);
            ImGui::InputTextMultiline("##placeText", es.placeText, sizeof(es.placeText),
                                      ImVec2(-1.0f, ui::metrics::U(12.0f)));
        } else {
            ui::kit::dimText(es.schPlaceKind == 3
                ? "Click the sheet to place an electrical junction."
                : "Click a pin endpoint to mark it intentionally unconnected.");
        }
    } else if (st.ecadTool == 2) {
        if (es.routeBus) {
            ui::kit::dimText("Bus name");
            ImGui::SetNextItemWidth(-1.0f);
            ImGui::InputText("##routeBusName", es.placeBusName, sizeof(es.placeBusName));
        }
        ui::kit::dimText(es.routeZone ? "Click polygon corners; double-click to close the zone."
                           : es.routeBus ? "Click bus corners; double-click to finish."
                                         : "Click to commit orthogonal segments. Space changes corner.");
    }
}

void drawHierarchy(AppState& st, EcadEditorState& es) {
    EcadMirror& m = es.mirror;
    const bool pcb = m.isPcb();
    ui::kit::sectionHeader(pcb ? "Board hierarchy" : "Schematic hierarchy",
                           pcb ? "ecad_pcb" : "sch_new");
    ImGui::SetNextItemWidth(-1.0f);
    ImGui::InputTextWithHint("##ecadHierarchySearch", "Search hierarchy...",
                             es.managerSearch, sizeof(es.managerSearch));
    static const char* kPcbKinds[] = {"All", "Layers", "Nets", "Footprints"};
    static const char* kSchKinds[] = {"All", "Nets", "Symbols"};
    const int kindCount = pcb ? 4 : 3;
    if (es.hierarchyKind >= kindCount) es.hierarchyKind = 0;
    ui::kit::fieldEnum("##ecadHierarchyKind", "Show", &es.hierarchyKind,
                       pcb ? kPcbKinds : kSchKinds, kindCount,
                       ui::kit::FieldOpts().withEnumStyle(ui::kit::EnumStyle::Combo));

    int shown = 0;
    ui::fmRowStripeReset();
    if (pcb && (es.hierarchyKind == 0 || es.hierarchyKind == 1)) {
        if (ui::fmSectionDividerAddButton("Copper layers", "ecad_add_layer",
                                          "Add a symmetric copper layer pair")) {
            const int current = static_cast<int>(m.layers().size());
            const int next = current < 2 ? 2 : current + 2;
            if (next > 32) {
                st.toast(ui::ToastLevel::Warn, "PCB stackup is capped at 32 copper layers");
            } else {
                st.ecadPendingOps = nlohmann::json::array(
                    {{{"op", "pcb_set_stackup"}, {"copper_layers", next}}});
                st.ecadOpsGo = true;
            }
        }
        for (const auto& layer : m.layers()) {
            if (!containsFolded(layer.id, es.managerSearch)) continue;
            drawLayerHierarchyRow(es, layer);
            ++shown;
        }
    }
    if (es.hierarchyKind == 0 || es.hierarchyKind == (pcb ? 2 : 1)) {
        ui::kit::sectionHeader("Nets", nullptr, nullptr, static_cast<int>(m.nets().size()));
        for (const auto& net : m.nets()) {
            if (!containsFolded(net.name, es.managerSearch)) continue;
            drawNetHierarchyRow(es, net, !pcb);
            ++shown;
        }
    }
    if (pcb && (es.hierarchyKind == 0 || es.hierarchyKind == 3)) {
        ui::kit::sectionHeader("Footprints", "ecad_footprint", nullptr,
                               static_cast<int>(m.footprints().size()));
        for (const auto& fp : m.footprints()) {
            const std::string searchable = fp.ref + " " + fp.value + " " + fp.fpid;
            if (!containsFolded(searchable, es.managerSearch)) continue;
            ui::kit::RowStyle style;
            const std::string secondary = fp.value.empty() ? fp.fpid : fp.value + " | " + fp.fpid;
            style.secondary = secondary.c_str();
            style.selected = hasSelection(es, "fp:" + fp.id);
            if (ui::kit::row(fp.id.c_str(), fp.ref.empty() ? fp.fpid.c_str() : fp.ref.c_str(),
                             style)) {
                applyPick(es, "fp:" + fp.id, ImGui::GetIO().KeyCtrl, ImGui::GetIO().KeyShift);
                focusSelected(es);
            }
            ++shown;
        }
    } else if (!pcb && (es.hierarchyKind == 0 || es.hierarchyKind == 2)) {
        ui::kit::sectionHeader("Symbols", "sch_symbol", nullptr,
                               static_cast<int>(m.symbols().size()));
        for (const auto& symbol : m.symbols()) {
            const std::string searchable =
                symbol.ref + " " + symbol.value + " " + symbol.libid;
            if (!containsFolded(searchable, es.managerSearch)) continue;
            ui::kit::RowStyle style;
            const std::string secondary =
                symbol.value.empty() ? symbol.libid : symbol.value + " | " + symbol.libid;
            style.secondary = secondary.c_str();
            if (symbol.generated) {
                style.icon = "warning_badge";
                style.iconTone = ui::kit::Tone::Warn;
            }
            style.selected = hasSelection(es, "sym:" + symbol.id);
            if (ui::kit::row(symbol.id.c_str(),
                             symbol.ref.empty() ? symbol.libid.c_str() : symbol.ref.c_str(),
                             style)) {
                applyPick(es, "sym:" + symbol.id,
                          ImGui::GetIO().KeyCtrl, ImGui::GetIO().KeyShift);
                focusSelected(es);
            }
            ++shown;
        }
    }
    if (shown == 0) {
        ui::kit::emptyState("filter_funnel", "No hierarchy matches",
                            "Change the search text or show a different entity type.");
    }
}

void drawSheetControls(AppState& st, EcadEditorState& es) {
    EcadMirror& m = es.mirror;
    ui::kit::sectionHeader("Sheets", nullptr, nullptr, static_cast<int>(m.sheets().size()));
    const EcadSheet* active = nullptr;
    for (const auto& sheet : m.sheets()) if (sheet.id == m.activeSheet()) { active = &sheet; break; }
    es.comboItems.clear();
    int sheetChoice = -1;
    for (const auto& sheet : m.sheets()) {
        if (sheet.id == m.activeSheet()) sheetChoice = static_cast<int>(es.comboItems.size());
        es.comboItems.push_back(sheet.name.c_str());
    }
    if (ui::kit::fieldEnum("##activeSheet", "Active sheet", &sheetChoice, es.comboItems.data(),
                           static_cast<int>(es.comboItems.size()),
                           ui::kit::FieldOpts().withEnumStyle(ui::kit::EnumStyle::Combo))) {
        m.setActiveSheet(m.sheets()[sheetChoice].id);
        es.selected.clear();
        es.canvas.requestFit();
    }
    if (es.renameLoadedFor != m.activeSheet()) {
        std::snprintf(es.renameSheetName, sizeof(es.renameSheetName), "%s",
                      active ? active->name.c_str() : "");
        es.renameLoadedFor = m.activeSheet();
    }
    ImGui::SetNextItemWidth(-1.0f);
    ImGui::InputTextWithHint("##renameSheet", "Active sheet name", es.renameSheetName,
                             sizeof(es.renameSheetName));
    if (ui::kit::secondaryButton("Rename sheet", active && es.renameSheetName[0], nullptr,
                                 ImVec2(-1.0f, 0.0f))) {
        st.ecadPendingOps = nlohmann::json::array(
            {{{"op", "sch_edit_sheet"}, {"id", active->id}, {"name", es.renameSheetName}}});
        st.ecadOpsGo = true;
    }
    ImGui::SetNextItemWidth(-1.0f);
    ImGui::InputTextWithHint("##newSheet", "New sheet name", es.newSheetName,
                             sizeof(es.newSheetName));
    if (ui::kit::primaryButton("New sheet", es.newSheetName[0] != '\0', nullptr,
                               ImVec2(-1.0f, 0.0f))) {
        st.ecadPendingOps = nlohmann::json::array(
            {{{"op", "sch_add_sheet"}, {"name", es.newSheetName},
              {"parent", m.activeSheet()}}});
        st.ecadOpsGo = true;
        es.activateNewSheet = true;
    }
}

void drawBomView(AppState& st, EcadEditorState& es) {
    struct Group {
        std::string value, footprint, libid;
        std::vector<std::string> refs;
    };
    std::vector<Group> groups;
    int excluded = 0;
    for (const auto& symbol : es.mirror.symbols()) {
        if (symbol.dnp || !symbol.inBom) { ++excluded; continue; }
        auto it = std::find_if(groups.begin(), groups.end(), [&](const Group& g) {
            return g.value == symbol.value && g.footprint == symbol.footprint &&
                   g.libid == symbol.libid;
        });
        if (it == groups.end()) {
            groups.push_back({symbol.value, symbol.footprint, symbol.libid, {}});
            it = groups.end() - 1;
        }
        it->refs.push_back(symbol.ref);
    }
    ui::kit::sectionHeader("Bill of materials", nullptr, nullptr,
                           static_cast<int>(groups.size()), true);
    ui::kit::keyValueRow("Excluded (DNP / not in BOM)", std::to_string(excluded).c_str());
    if (ui::kit::primaryButton("Export BOM", true, nullptr, ImVec2(-1.0f, 0.0f)))
        st.ecadPanelCmd = "sch_export_bom";
    for (size_t i = 0; i < groups.size(); ++i) {
        const Group& group = groups[i];
        std::string refs;
        for (const auto& ref : group.refs) {
            if (!refs.empty()) refs += ", ";
            refs += ref;
        }
        std::string secondary = std::to_string(group.refs.size()) + " × " + refs;
        if (!group.footprint.empty()) secondary += " | " + group.footprint;
        ui::kit::RowStyle style;
        style.secondary = secondary.c_str();
        ImGui::PushID(static_cast<int>(i));
        const bool clicked = ui::kit::row(
            "##bomRow", group.value.empty() ? group.libid.c_str() : group.value.c_str(), style);
        ImGui::PopID();
        if (clicked) {
            es.selected.clear();
            for (const auto& symbol : es.mirror.symbols())
                if (symbol.value == group.value && symbol.footprint == group.footprint &&
                    symbol.libid == group.libid)
                    es.selected.push_back("sym:" + symbol.id);
            focusSelected(es);
        }
    }
    if (groups.empty())
        ui::kit::emptyState("ecad_footprint", "No populated BOM components",
                            "Place components and enable Include in BOM.");
}

void drawDesignView(AppState& st, EcadEditorState& es) {
    if (es.mirror.isPcb()) drawPcbEditingContext(es);
    else drawSheetControls(st, es);
    drawCanvasToolContext(st, es);
    drawHierarchy(st, es);
    if (es.mirror.isSch()) {
        ui::kit::sectionHeader("Simulation", "sch_simulate");
        static const char* kAnalyses[] = {"Transient", "AC"};
        ui::kit::fieldEnum("##ecadAnalysis", "Analysis", &st.ecadSimAnalysis, kAnalyses, 2,
                           ui::kit::FieldOpts().withEnumStyle(ui::kit::EnumStyle::Combo));
        if (ui::kit::primaryButton("Simulate", true, nullptr, ImVec2(-1.0f, 0.0f)))
            st.ecadSimGo = true;
    }
}

void drawInspectView(AppState& st, EcadEditorState& es) {
    EcadMirror& m = es.mirror;
    const bool pcb = m.isPcb();
    ui::kit::sectionHeader("Document statistics", "health_check", nullptr, -1, true);
    auto countRow = [](const char* label, size_t count) {
        const std::string value = std::to_string(count);
        ui::kit::keyValueRow(label, value.c_str());
    };
    if (pcb) {
        countRow("Footprints", m.footprints().size());
        countRow("Tracks", m.tracks().size());
        countRow("Vias", m.vias().size());
        countRow("Zones", m.zones().size());
    } else {
        countRow("Sheets", m.sheets().size());
        countRow("Symbols", m.symbols().size());
        countRow("Wires", m.wires().size());
        countRow("Buses / entries", m.buses().size() + m.busEntries().size());
        countRow("Junctions", m.junctions().size());
        countRow("Labels / power", m.labels().size() + m.powers().size());
        countRow("Notes / graphics", m.texts().size() + m.graphics().size());
    }
    countRow("Nets", m.nets().size());

    ui::kit::sectionHeader("Selection filters", "filter_funnel");
    if (pcb) {
        ui::kit::toggleChip("Footprints", &es.pickFootprints);
        ImGui::SameLine(0.0f, ui::kit::rowGap());
        ui::kit::toggleChip("Tracks", &es.pickTracks);
        ImGui::SameLine(0.0f, ui::kit::rowGap());
        ui::kit::toggleChip("Vias", &es.pickVias);
    } else {
        ui::kit::toggleChip("Symbols + pins", &es.pickSymbols);
        ImGui::SameLine(0.0f, ui::kit::rowGap());
        ui::kit::toggleChip("Wires", &es.pickWires);
    }

    ui::kit::sectionHeader("Selected entities", nullptr, nullptr,
                           static_cast<int>(es.selected.size()));
    if (es.selected.empty()) {
        ui::kit::emptyState("zoom_fit", "Nothing selected",
                            "Click, Ctrl-click or drag a marquee on the canvas.");
        return;
    }
    for (size_t i = 0; i < es.selected.size(); ++i) {
        ui::kit::RowStyle style;
        style.selected = i + 1 == es.selected.size();
        ImGui::PushID(static_cast<int>(i));
        if (ui::kit::row("##selectedToken", es.selected[i].c_str(), style)) {
            const std::string token = es.selected[i];
            es.selected.erase(es.selected.begin() + static_cast<std::ptrdiff_t>(i));
            es.selected.push_back(token);
            es.editLoadedFor.clear();
        }
        ImGui::PopID();
    }
    std::string kind, id;
    splitToken(primarySelection(es), kind, id);
    ui::kit::keyValueRow("Primary kind", kind.c_str());
    ui::kit::keyValueRow("Id", id.c_str(), ui::kit::Tone::Neutral, true);
    EcadPt p;
    if (selectedPos(m, primarySelection(es), p))
        ui::kit::keyValueRow("Position", pointText(p).c_str());
    if (kind == "sym" || kind == "pin" || kind == "fp" || kind == "pad")
        drawPropInspector(st, es, ui::uiScale);
    const float actionW =
        (ImGui::GetContentRegionAvail().x - ui::kit::rowGap()) * 0.5f;
    if (ui::kit::secondaryButton("Focus", true, nullptr, ImVec2(actionW, 0.0f)))
        focusSelected(es);
    ImGui::SameLine(0.0f, ui::kit::rowGap());
    if (ui::kit::dangerButton("Delete all", true, nullptr, ImVec2(actionW, 0.0f)))
        deleteSelected(st, es);
}

void drawChecksView(AppState& st, EcadEditorState& es) {
    EcadMirror& m = es.mirror;
    const bool pcb = m.isPcb();
    const auto& checks = pcb ? m.drc() : m.erc();
    int errors = 0;
    int warnings = 0;
    for (const auto& check : checks)
        (check.severity == "error" ? errors : warnings)++;

    ui::kit::sectionHeader(pcb ? "Design rule check" : "Electrical rule check",
                           pcb ? "ecad_drc" : "sch_erc", nullptr,
                           static_cast<int>(checks.size()), true);
    const std::string errorText = std::to_string(errors);
    const std::string warningText = std::to_string(warnings);
    ui::kit::valueChip("Errors", errorText.c_str(),
                       errors ? ui::kit::Tone::Err : ui::kit::Tone::Ok);
    ImGui::SameLine(0.0f, ui::kit::rowGap());
    ui::kit::valueChip("Warnings", warningText.c_str(),
                       warnings ? ui::kit::Tone::Warn : ui::kit::Tone::Ok);
    if (pcb) {
        ImGui::SameLine(0.0f, ui::kit::rowGap());
        const std::string unrouted = std::to_string(m.unroutedCount());
        ui::kit::valueChip("Unrouted", unrouted.c_str(),
                           m.unroutedCount() ? ui::kit::Tone::Warn : ui::kit::Tone::Ok);
    }
    if (ui::kit::primaryButton(pcb ? "Run DRC" : "Run ERC", true, nullptr,
                               ImVec2(-1.0f, 0.0f)))
        st.ecadPanelCmd = pcb ? "ecad_run_drc" : "ecad_run_erc";

    ImGui::SetNextItemWidth(-1.0f);
    ImGui::InputTextWithHint("##ecadCheckSearch", "Search rule, message, net or layer...",
                             es.checksSearch, sizeof(es.checksSearch));
    static const char* kSeverity[] = {"All", "Errors", "Warnings"};
    ui::kit::segmented("##ecadCheckSeverity", kSeverity, 3, &es.checksSeverity,
                       ImGui::GetContentRegionAvail().x);

    int shown = 0;
    for (size_t i = 0; i < checks.size(); ++i) {
        const EcadViolation& check = checks[i];
        if (es.checksSeverity == 1 && check.severity != "error") continue;
        if (es.checksSeverity == 2 && check.severity != "warning") continue;
        const std::string searchable =
            check.rule + " " + check.message + " " + check.net + " " + check.layer;
        if (!containsFolded(searchable, es.checksSearch)) continue;
        ui::kit::RowStyle style;
        style.icon = check.severity == "error" ? "error_badge" : "warning_badge";
        style.iconTone = check.severity == "error" ? ui::kit::Tone::Err : ui::kit::Tone::Warn;
        std::string secondary = check.message;
        if (!check.net.empty()) secondary += (secondary.empty() ? "" : " | ") + check.net;
        if (!check.layer.empty()) secondary += (secondary.empty() ? "" : " | ") + check.layer;
        style.secondary = secondary.empty() ? nullptr : secondary.c_str();
        style.meta = check.severity.c_str();
        ImGui::PushID(static_cast<int>(i));
        const bool clicked = ui::kit::row("##check", check.rule.c_str(), style);
        ImGui::PopID();
        if (clicked) {
            if (check.hasLoc) {
                es.canvas.focusOn(check.loc, check.severity == "error");
                applyPick(es, resolvePick(st, es, check.loc,
                                          10.0f / std::max(0.01f, es.canvas.zoom())),
                          false, false);
                st.addLog("[ecad] focus " + check.rule + " @ " + pointText(check.loc));
            } else {
                st.addLog("[ecad] " + check.rule + " has no reported location");
            }
        }
        ++shown;
    }
    if (shown == 0) {
        if (checks.empty()) {
            ui::kit::emptyState("ok_badge", pcb ? "No DRC violations" : "No ERC violations",
                                "Run the check again after editing to refresh this result.");
        } else {
            ui::kit::emptyState("filter_funnel", "No check rows match",
                                "Change the search text or severity filter.");
        }
    }
}

void drawEcadManagerBodyImpl(AppState& st, const ui::AdaptiveSurface& surf, ImU32 halo) {
    (void)halo;
    EcadEditorState& es = state();
    EcadMirror& m = es.mirror;
    ui::kit::pushDensity(ui::kit::Density::Compact);
    struct DensityPop { ~DensityPop() { ui::kit::popDensity(); } } densityPop;

    if (!m.hasDoc()) {
        ui::kit::emptyState("ecad_pcb", "No ECAD document open",
                            "Describe a circuit on the canvas, or start a blank document with "
                            "PCB Layout or New Schematic on the ribbon.");
        return;
    }
    const bool pcb = m.isPcb();
    ensurePcbContext(es);
    loadSymbolCatalog(st, es);
    scanEcadHealth(es);
    if (es.healthLevel >= 1 && es.healthMsg != es.healthToasted)
        st.toast(es.healthLevel >= 2 ? ui::ToastLevel::Error : ui::ToastLevel::Warn, es.healthMsg);
    es.healthToasted = es.healthMsg;

    const std::string name = m.name().empty() ? "(untitled)" : m.name();
    char revision[32];
    std::snprintf(revision, sizeof(revision), "rev %d", m.revision());
    ui::kit::sectionHeader(name.c_str(), pcb ? "ecad_pcb" : "sch_new", revision, -1, true);
    drawManagerToolbar(st, es, pcb);
    ui::kit::gapRow();

    static const char* pcbViews[] = {"Design", "Inspect", "Checks"};
    static const char* schViews[] = {"Design", "Inspect", "BOM", "Checks"};
    static const char* pcbTips[] = {
        "Hierarchy, canvas tools and editing context",
        "Live statistics, pick filters and selected-entity properties",
        "Search and focus DRC violations"};
    static const char* schTips[] = {
        "Hierarchy, canvas tools and editing context",
        "Live statistics, pick filters and selected-entity properties",
        "Grouped populated components and BOM export",
        "Search and focus ERC violations"};
    const int viewCount = pcb ? 3 : 4;
    if (es.managerView >= viewCount) es.managerView = 0;
    ui::kit::segmented("##ecadManagerView", pcb ? pcbViews : schViews, viewCount,
                       &es.managerView, ImGui::GetContentRegionAvail().x,
                       pcb ? pcbTips : schTips);
    const float bodyTopY = ImGui::GetCursorScreenPos().y;
    const float availableBodyH = ImGui::GetContentRegionAvail().y;
    float bodyH = availableBodyH;
    if (!pcb) {
        if (es.bottomStripTopScreenY > bodyTopY)
            bodyH = std::min(bodyH, es.bottomStripTopScreenY - bodyTopY -
                                     ui::metrics::cpMenuRowGap());
        else
            bodyH -= ecadBottomStripHeight(st, m);
    }
    bodyH = std::max(ui::metrics::cpMenuRowGap(), bodyH);
    const float laneW = ui::metrics::px(ui::kChatScrollbarLane);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(laneW, 0.0f));
    ImGui::BeginChild("##ecadManagerScroll", ImVec2(0.0f, bodyH),
                      ImGuiChildFlags_AlwaysUseWindowPadding, ImGuiWindowFlags_NoScrollbar);
    ImGui::PopStyleVar();
    ImGui::Indent(-laneW);
    if (es.managerView == 0) drawDesignView(st, es);
    else if (es.managerView == 1) drawInspectView(st, es);
    else if (!pcb && es.managerView == 2) drawBomView(st, es);
    else drawChecksView(st, es);
    ImGui::Indent(laneW);
    ui::drawWindowPillScrollbarV(surf);
    ImGui::EndChild();
}

// A distinct, theme-aware trace color per index (palette roles, like ecad_canvas::copperColor).
ImU32 traceColor(int i) {
    const ui::Palette& p = ui::palette;
    switch (((i % 6) + 6) % 6) {
        case 0:  return ui::Palette::toU32(p.accent);    // teal
        case 1:  return ui::Palette::toU32(p.axisX);     // red
        case 2:  return ui::Palette::toU32(p.axisY);     // green
        case 3:  return ui::Palette::toU32(p.axisZ);     // blue
        case 4:  return ui::Palette::toU32(p.statusWarn);// amber
        default: return ui::Palette::toU32(p.statusErr); // orange
    }
}

// One plotted trace: a label + the points (in DATA space) it samples. Filled by collectTraces.
struct PlotTrace {
    std::string name;
    std::vector<float> x, y;
};

// Gather voltage traces from the sim results for the active analysis. Transient -> x=time(s),
// y=volts. AC -> x=log10(freq Hz), y=magnitude in dB (20*log10(mag), guarded). I(...) current
// traces are skipped (voltages are the priority for the waveform view). xLabel/yLabel describe
// the axes for the caller. Returns false when there is nothing to plot.
bool collectTraces(const nlohmann::json& results, int analysis,
                   std::vector<PlotTrace>& out, std::string& xLabel, std::string& yLabel) {
    out.clear();
    auto wantTrace = [](const std::string& key) {
        // Plot voltage nodes; skip current traces like "I(V1)".
        return key.rfind("I(", 0) != 0 && key.rfind("i(", 0) != 0;
    };
    if (analysis == 1) {  // AC: Bode magnitude
        xLabel = "log10(f / Hz)";
        yLabel = "Magnitude (dB)";
        if (!results.contains("ac") || !results["ac"].is_object()) return false;
        const auto& ac = results["ac"];
        if (!ac.contains("x_f") || !ac.contains("mag")) return false;
        const auto& xf = ac["x_f"];
        const auto& mag = ac["mag"];
        if (!xf.is_array() || !mag.is_object()) return false;
        for (auto it = mag.begin(); it != mag.end(); ++it) {
            if (!it.value().is_array() || !wantTrace(it.key())) continue;
            PlotTrace tr; tr.name = it.key();
            const size_t n = std::min(xf.size(), it.value().size());
            for (size_t i = 0; i < n; ++i) {
                const double f = xf[i].is_number() ? xf[i].get<double>() : 0.0;
                double m = it.value()[i].is_number() ? it.value()[i].get<double>() : 0.0;
                if (f <= 0.0) continue;
                if (m <= 1e-12) m = 1e-12;  // guard log of non-positive magnitude
                double db = 20.0 * std::log10(m);
                if (db < -120.0) db = -120.0;  // floor so a single near-zero sample can't flatten scale
                tr.x.push_back((float)std::log10(f));
                tr.y.push_back((float)db);
            }
            if (tr.x.size() >= 2) out.push_back(std::move(tr));
        }
    } else {  // Transient: voltage vs time
        xLabel = "Time (s)";
        yLabel = "Volts (V)";
        if (!results.contains("tran") || !results["tran"].is_object()) return false;
        const auto& tran = results["tran"];
        if (!tran.contains("x_t") || !tran.contains("traces")) return false;
        const auto& xt = tran["x_t"];
        const auto& traces = tran["traces"];
        if (!xt.is_array() || !traces.is_object()) return false;
        for (auto it = traces.begin(); it != traces.end(); ++it) {
            if (!it.value().is_array() || !wantTrace(it.key())) continue;
            PlotTrace tr; tr.name = it.key();
            const size_t n = std::min(xt.size(), it.value().size());
            for (size_t i = 0; i < n; ++i) {
                const double t = xt[i].is_number() ? xt[i].get<double>() : 0.0;
                const double v = it.value()[i].is_number() ? it.value()[i].get<double>() : 0.0;
                tr.x.push_back((float)t);
                tr.y.push_back((float)v);
            }
            if (tr.x.size() >= 2) out.push_back(std::move(tr));
        }
    }
    return !out.empty();
}

void drawSimMetadata(const nlohmann::json& results) {
    const ui::Palette& pal = ui::palette;
    if (!results.is_object()) return;

    if (results.contains("convergence") && results["convergence"].is_object()) {
        std::string summary;
        bool allOk = true;
        for (auto it = results["convergence"].begin(); it != results["convergence"].end(); ++it) {
            const bool ok = it.value().is_boolean() ? it.value().get<bool>() : false;
            allOk = allOk && ok;
            if (!summary.empty()) summary += "  ";
            summary += it.key() + std::string(ok ? ": ok" : ": review");
        }
        ImGui::TextColored(v4(allOk ? pal.statusOk : pal.statusWarn), "%s", summary.c_str());
    }
    if (results.contains("ms") && results["ms"].is_number()) {
        ImGui::SameLine();
        ImGui::TextColored(v4(pal.textDim), "%.2f ms", results["ms"].get<double>());
    }
    if (results.contains("log") && results["log"].is_array() && !results["log"].empty()) {
        const auto& last = results["log"].back();
        std::string msg = last.is_string() ? last.get<std::string>() : last.dump();
        ImGui::TextWrapped("%s", msg.c_str());
    }
}

bool drawScalarResults(const nlohmann::json& results) {
    const ui::Palette& pal = ui::palette;
    const char* keys[] = {"op", "dc", "tf", "noise", "four"};
    for (const char* key : keys) {
        if (!results.contains(key) || !results[key].is_object()) continue;
        ImGui::TextColored(v4(pal.accent), "%s", key);
        if (ImGui::BeginTable("##simScalarResults", 2,
                              ImGuiTableFlags_BordersInnerH | ImGuiTableFlags_RowBg)) {
            ImGui::TableSetupColumn("Signal");
            ImGui::TableSetupColumn("Value");
            ImGui::TableHeadersRow();
            int shown = 0;
            for (auto it = results[key].begin(); it != results[key].end() && shown < 16; ++it, ++shown) {
                ImGui::TableNextRow();
                ImGui::TableSetColumnIndex(0);
                ImGui::TextUnformatted(it.key().c_str());
                ImGui::TableSetColumnIndex(1);
                std::string val = it.value().is_number() ? std::to_string(it.value().get<double>())
                                                         : it.value().dump();
                ImGui::TextUnformatted(val.c_str());
            }
            ImGui::EndTable();
        }
        return true;
    }
    return false;
}

// ---- bottom area: simulation waveform plot ----
// A bordered plot rect with labeled axes, each voltage trace an auto-scaled colored polyline, and a
// legend (node name + color) in the top-right corner. Drawn into the current child window with the
// window draw list. Falls back to a hint line when the results carry no plottable voltage traces.
void drawSimPlot(const nlohmann::json& results, int analysis) {
    const ui::Palette& pal = ui::palette;
    const float sc = ui::uiScale;

    drawSimMetadata(results);

    std::vector<PlotTrace> traces;
    std::string xLabel, yLabel;
    if (!collectTraces(results, analysis, traces, xLabel, yLabel)) {
        if (drawScalarResults(results)) return;
        ImGui::TextColored(v4(pal.textDim),
                           "Simulated, but no voltage traces to plot for this analysis.");
        return;
    }

    ImGui::TextColored(v4(pal.accent), "%s",
                       analysis == 1 ? "AC Sweep (Bode)" : "Transient Response");

    // X range: full span across all traces. Y range: percentile-clipped so a few extreme outliers
    // (e.g. an AC node that dips to ~0 magnitude -> a huge negative dB) can't flatten the curve.
    float xMin = 1e30f, xMax = -1e30f;
    std::vector<float> allY;
    for (const auto& tr : traces) {
        for (size_t i = 0; i < tr.x.size(); ++i) {
            xMin = std::min(xMin, tr.x[i]); xMax = std::max(xMax, tr.x[i]);
            allY.push_back(tr.y[i]);
        }
    }
    if (xMax - xMin < 1e-12f) xMax = xMin + 1.0f;
    std::sort(allY.begin(), allY.end());
    auto pctl = [&](float q) {
        if (allY.empty()) return 0.0f;
        const float idx = q * (float)(allY.size() - 1);
        const size_t lo = (size_t)idx;
        const size_t hi = std::min(lo + 1, allY.size() - 1);
        return allY[lo] + (allY[hi] - allY[lo]) * (idx - (float)lo);
    };
    float yMin = pctl(0.02f), yMax = pctl(0.98f);
    if (yMax - yMin < 1e-6f) { yMax += 0.5f; yMin -= 0.5f; }
    const float yPad = (yMax - yMin) * 0.08f;
    yMin -= yPad; yMax += yPad;

    // Plot rect inside the child, leaving room for axis labels.
    const float lpad = 52 * sc, rpad = 12 * sc, tpad = 6 * sc, bpad = 22 * sc;
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    const ImVec2 org = ImGui::GetCursorScreenPos();
    const float plotW = std::max(40.0f, avail.x - lpad - rpad);
    const float plotH = std::max(30.0f, avail.y - tpad - bpad);
    const ImVec2 p0(org.x + lpad, org.y + tpad);
    const ImVec2 p1(p0.x + plotW, p0.y + plotH);

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImU32 borderCol = ui::Palette::toU32(pal.textDim);
    const ImU32 gridCol   = ui::Palette::toU32(pal.panelBg);  // faint gridlines
    const ImU32 axisLblCol = ui::Palette::toU32(pal.textDim);

    dl->AddRectFilled(p0, p1, ui::Palette::toU32(pal.windowBg));
    // Light gridlines (4 vertical, 4 horizontal).
    for (int g = 1; g < 4; ++g) {
        const float fx = p0.x + plotW * (g / 4.0f);
        const float fy = p0.y + plotH * (g / 4.0f);
        dl->AddLine(ImVec2(fx, p0.y), ImVec2(fx, p1.y), gridCol, 1.0f);
        dl->AddLine(ImVec2(p0.x, fy), ImVec2(p1.x, fy), gridCol, 1.0f);
    }
    dl->AddRect(p0, p1, borderCol);

    auto toScreen = [&](float x, float y) {
        const float sx = p0.x + (x - xMin) / (xMax - xMin) * plotW;
        float sy = p1.y - (y - yMin) / (yMax - yMin) * plotH;
        sy = std::max(p0.y, std::min(p1.y, sy));  // clamp outliers to the plot rect
        return ImVec2(sx, sy);
    };

    // Zero line for transient (volts).
    if (analysis != 1 && yMin < 0.0f && yMax > 0.0f) {
        const ImVec2 z0 = toScreen(xMin, 0.0f), z1 = toScreen(xMax, 0.0f);
        dl->AddLine(ImVec2(p0.x, z0.y), ImVec2(p1.x, z1.y), borderCol, 1.0f);
    }

    // Trace polylines.
    for (size_t ti = 0; ti < traces.size(); ++ti) {
        const PlotTrace& tr = traces[ti];
        const ImU32 c = traceColor((int)ti);
        std::vector<ImVec2> pts;
        pts.reserve(tr.x.size());
        for (size_t i = 0; i < tr.x.size(); ++i) pts.push_back(toScreen(tr.x[i], tr.y[i]));
        if (pts.size() >= 2)
            dl->AddPolyline(pts.data(), (int)pts.size(), c, ImDrawFlags_None, 1.8f);
    }

    // Axis min/max labels.
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.3g", yMax);
    dl->AddText(ImVec2(org.x + 4 * sc, p0.y - 2 * sc), axisLblCol, buf);
    std::snprintf(buf, sizeof(buf), "%.3g", yMin);
    dl->AddText(ImVec2(org.x + 4 * sc, p1.y - 14 * sc), axisLblCol, buf);
    std::snprintf(buf, sizeof(buf), "%.3g", xMin);
    dl->AddText(ImVec2(p0.x, p1.y + 4 * sc), axisLblCol, buf);
    std::snprintf(buf, sizeof(buf), "%.3g", xMax);
    dl->AddText(ImVec2(p1.x - 40 * sc, p1.y + 4 * sc), axisLblCol, buf);
    // X-axis title centered under the plot (between the min/max ticks); Y-axis title at mid-height.
    const float xLblW = ImGui::CalcTextSize(xLabel.c_str()).x;
    dl->AddText(ImVec2(p0.x + (plotW - xLblW) * 0.5f, p1.y + 4 * sc), axisLblCol, xLabel.c_str());
    dl->AddText(ImVec2(org.x + 2 * sc, p0.y + plotH * 0.5f), axisLblCol, yLabel.c_str());

    // Legend (top-right corner inside the plot).
    float ly = p0.y + 4 * sc;
    for (size_t ti = 0; ti < traces.size(); ++ti) {
        const ImU32 c = traceColor((int)ti);
        const float sw = ImGui::GetTextLineHeight();
        const ImVec2 lp(p1.x - 110 * sc, ly);
        dl->AddRectFilled(lp, ImVec2(lp.x + sw, lp.y + sw), c, 2.0f * ui::uiScale);
        dl->AddText(ImVec2(lp.x + sw + 4 * sc, lp.y), ui::Palette::toU32(pal.text),
                    traces[ti].name.c_str());
        ly += sw + 3 * sc;
    }

    // Reserve the space we drew into so the child sizes correctly.
    ImGui::Dummy(ImVec2(avail.x, avail.y));
}

// ---- bottom strip: ERC / DRC results ----
void drawResultsStrip(AppState& st, EcadEditorState& es) {
    const ui::Palette& pal = ui::palette;
    EcadMirror& m = es.mirror;

    const auto& list = m.isSch() ? m.erc() : m.drc();
    int errs = 0, warns = 0;
    for (const auto& v : list) (v.severity == "error" ? errs : warns)++;

    const char* kindLbl = m.isSch() ? "ERC" : "DRC";
    ImGui::TextColored(errs ? v4(pal.statusErr) : v4(pal.statusOk),
                       "%s: %d error%s", kindLbl, errs, errs == 1 ? "" : "s");
    ImGui::SameLine();
    ImGui::TextColored(warns ? v4(pal.statusWarn) : v4(pal.textDim),
                       "  %d warning%s", warns, warns == 1 ? "" : "s");
    if (m.isPcb()) {
        ImGui::SameLine();
        const int un = m.unroutedCount();
        ImGui::TextColored(un ? v4(pal.statusWarn) : v4(pal.statusOk),
                           "  |  Unrouted: %d", un);
    }
    ImGui::Separator();

    ImGui::BeginChild("##ecadResults", ImVec2(0, 0), ImGuiChildFlags_None);
    for (size_t i = 0; i < list.size(); ++i) {
        const EcadViolation& v = list[i];
        const ImVec4 c = (v.severity == "error") ? v4(pal.statusErr) : v4(pal.statusWarn);
        // One full-width selectable carrying the whole row; tint the text by the severity. Clicking
        // a located violation pans + zooms the canvas to v.loc and flashes a marker there ("zoom to
        // violation"). A unique ##id keeps ImGui ids stable across frames.
        char label[256];
        std::snprintf(label, sizeof(label), "[%s] %s%s%s##viol%zu",
                      v.severity.c_str(), v.rule.c_str(),
                      v.message.empty() ? "" : " — ", v.message.c_str(), i);
        ImGui::PushStyleColor(ImGuiCol_Text, c);
        const bool clicked = ImGui::Selectable(label);
        ImGui::PopStyleColor();
        if (v.hasLoc && ImGui::IsItemHovered())
            ImGui::SetTooltip("Click to zoom to (%.2f, %.2f) mm", v.loc.x, v.loc.y);
        if (clicked) {
            if (v.hasLoc) {
                es.canvas.focusOn(v.loc, v.severity == "error");
                applyPick(es, resolvePick(st, es, v.loc,
                                          10.0f / std::max(0.01f, es.canvas.zoom())),
                          false, false);
                st.addLog("[ecad] zoom to " + v.rule + " @ (" +
                          std::to_string(v.loc.x) + ", " + std::to_string(v.loc.y) + ")");
            } else {
                st.addLog("[ecad] " + v.rule + " (no location)");
            }
        }
    }
    if (list.empty())
        ImGui::TextColored(v4(pal.textDim), "No %s violations.", kindLbl);
    ImGui::EndChild();
}

// Screen-x span of the ECAD window that no pinned glass column covers, measured from the current
// cursor row. The PCB/Schematic Manager column (left, slot 0) and the Copilot column (right,
// slot 1) float OVER this window, so anything laid out beneath them is hidden.
struct ClearSpan {
    float x0, x1;
};
ClearSpan ecadClearSpan() {
    const float left = ImGui::GetCursorScreenPos().x;
    ClearSpan span{left, left + ImGui::GetContentRegionAvail().x};
    const float managerEdge = ui::overlayColumnEdge(0);
    const float copilotEdge = ui::overlayColumnEdge(1);
    if (managerEdge > span.x0 && managerEdge < span.x1) span.x0 = managerEdge;
    if (copilotEdge > span.x0 && copilotEdge < span.x1) span.x1 = copilotEdge;
    return span;
}

// Welcome surface for no document or an untouched automatic PCB starter. Existing
// designs bypass it; actions use the same app hooks as the ribbon and AI launchpad.
void drawEcadLaunchpad(AppState& st, bool starterBoard = false) {
    const ClearSpan span = ecadClearSpan();
    const float pad = ui::metrics::U(6.0f);
    const float spanW = std::max(span.x1 - span.x0 - pad * 2.0f, ui::metrics::U(1.0f));
    const float colW = std::min(spanW, ui::metrics::px(560.0f));
    const float top = ImGui::GetCursorScreenPos().y + ui::docTabStripHeight(st) + pad;
    ImGui::SetCursorScreenPos(ImVec2(span.x0 + pad + (spanW - colW) * 0.5f, top));
    const float laneW = ui::metrics::px(ui::kChatScrollbarLane);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(laneW, 0.0f));
    ImGui::BeginChild("##ecadLaunchpad", ImVec2(colW, 0.0f),
                      ImGuiChildFlags_AlwaysUseWindowPadding,
                      ImGuiWindowFlags_NoBackground | ImGuiWindowFlags_NoScrollbar);
    ImGui::PopStyleVar();
    ImGui::Indent(-laneW);
    const auto surf = ui::adaptiveSurface(-1.0f);
    ui::kit::pushDensity(ui::kit::Density::Roomy);
    if (ui::kit::cardBegin("ecad_start", starterBoard ? "Start your PCB" : "Start an electronics design",
                           "ecad_pcb")) {
        ui::kit::dimText(starterBoard
            ? "Your blank board is ready. Edit it on the grid, start with a schematic, "
              "or describe a circuit for the AI team."
            : "Open a design or start a new board or schematic.");
        if (ui::kit::primaryButton(starterBoard ? "Edit this board" : "New PCB",
                                   true, nullptr, ImVec2(-1.0f, 0.0f))) {
            if (starterBoard) {
                state().starterCanvasChosen = true;
                st.docTabs[st.activeTab].isStarter = false;
                state().canvas.requestFit();
            } else st.ecadPanelCmd = "ecad_new_pcb";
        }
        if (ui::kit::secondaryButton("New Schematic", true, nullptr, ImVec2(-1.0f, 0.0f)))
            st.ecadPanelCmd = "ecad_new_sch";
        if (ui::kit::secondaryButton("Open design", true, nullptr, ImVec2(-1.0f, 0.0f)))
            st.ecadPanelCmd = "ecad_open";
    }
    ui::kit::cardEnd();
    ui::kit::gapSection();
    if (ui::kit::cardBegin("ecad_launchpad", "Design a circuit with AI", "ai_panel")) {
        ui::kit::dimText("Describe a circuit for a verified schematic. AI Board continues "
                         "to a populated, outlined and autorouted PCB for Board to 3D.");
        static char aibuf[512] = "5V divider with 2.5V output";
        ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x);
        ImGui::InputTextWithHint("##ecad_ai", "Describe a circuit...", aibuf, sizeof(aibuf));
        const bool haveRequest = aibuf[0] != '\0';
        const char* needRequest = "Describe a circuit first.";

        // Two equal buttons on one row while both labels fit; stacked full-width otherwise.
        const ImGuiStyle& style = ImGui::GetStyle();
        const float rowW = ImGui::GetContentRegionAvail().x;
        const float halfW = (rowW - style.ItemSpacing.x) * 0.5f;
        const bool oneRow =
            halfW >= ImGui::CalcTextSize("Design with AI").x + style.FramePadding.x * 4.0f;
        const float btnW = oneRow ? halfW : rowW;
        if (ui::kit::primaryButton("Design with AI", haveRequest, needRequest,
                                   ImVec2(btnW, 0.0f))) {
            st.ecadAiRequest = aibuf;
            st.ecadAiGo = true;
        }
        if (haveRequest) ui::kit::itemTooltip("Generate a verified schematic from the description.");
        if (oneRow) ImGui::SameLine(0.0f, style.ItemSpacing.x);
        if (ui::kit::secondaryButton("AI Board", haveRequest, needRequest, ImVec2(btnW, 0.0f))) {
            // One click all the way to a routed PCB: design the schematic, then populate +
            // outline + autoroute a board from it (ecadAiBoard in the app loop).
            st.ecadAiRequest = aibuf;
            st.ecadAiBoardGo = true;
        }
        if (haveRequest)
            ui::kit::itemTooltip("Schematic plus a populated, outlined, autorouted PCB, ready for "
                                 "Board to 3D.");

        static const char* const kExamples[] = {"Inverting amp, gain 10", "RC low-pass, 1 kHz",
                                                "555 astable"};
        ui::kit::chipFlowBegin("ecad_examples");
        for (int i = 0; i < (int)(sizeof(kExamples) / sizeof(kExamples[0])); ++i) {
            ImGui::PushID(i);
            bool on = std::strcmp(aibuf, kExamples[i]) == 0;
            if (ui::kit::flowToggleChip(kExamples[i], &on, ui::kit::Tone::Accent,
                                        "Use this example as the description") && on)
                std::snprintf(aibuf, sizeof(aibuf), "%s", kExamples[i]);
            ImGui::PopID();
        }
        ui::kit::chipFlowEnd();
    }
    ui::kit::cardEnd();
    ui::kit::popDensity();
    ImGui::Indent(laneW);
    ui::drawWindowPillScrollbarV(surf);
    ImGui::EndChild();
}

}  // namespace

// ---- public API ----

EcadMirror& ecadEditorMirror() { return state().mirror; }

void ecadEditorSelect(const std::string& sel) {
    EcadEditorState& es = state();
    es.selected.clear();
    if (!sel.empty()) es.selected.push_back(sel);
    es.editLoadedFor.clear();
}
void ecadArmPlacement(const std::string& kind) {
    EcadEditorState& es = state();
    if (!g_app) return;
    stopRoute(es);
    es.routeBus = es.routeZone = false;
    if (kind == "symbol") { es.schPlaceKind = 0; g_app->ecadTool = 1; }
    else if (kind == "label") { es.schPlaceKind = 1; g_app->ecadTool = 1; }
    else if (kind == "power") { es.schPlaceKind = 2; g_app->ecadTool = 1; }
    else if (kind == "junction") { es.schPlaceKind = 3; g_app->ecadTool = 1; }
    else if (kind == "no_connect") { es.schPlaceKind = 4; g_app->ecadTool = 1; }
    else if (kind == "text") { es.schPlaceKind = 5; g_app->ecadTool = 1; }
    else if (kind == "bus") { es.routeBus = true; g_app->ecadTool = 2; }
    else if (kind == "footprint") { es.pcbPlaceKind = 0; g_app->ecadTool = 1; }
    else if (kind == "via") { es.pcbPlaceKind = 1; g_app->ecadTool = 1; }
    else if (kind == "zone") { es.routeZone = true; g_app->ecadTool = 2; }
}
void ecadEditorHighlightNet(const std::string& net) {
    EcadEditorState& es = state();
    es.mgrSel = net.empty() ? std::string() : "net:" + net;
    es.canvas.setHighlightedNet(net);
}

bool ecadEditorFocusFirstViolation() {
    EcadEditorState& es = state();
    const auto& list = es.mirror.isSch() ? es.mirror.erc() : es.mirror.drc();
    for (const auto& v : list)
        if (v.hasLoc) { es.canvas.focusOn(v.loc, v.severity == "error"); return true; }
    return false;
}

void ecadEditorLoadResponse(const nlohmann::json& resp) {
    EcadEditorState& es = state();
    es.mirror.loadFromTree(resp);
    if (es.activateNewSheet && !es.mirror.sheets().empty()) {
        es.mirror.setActiveSheet(es.mirror.sheets().back().id);
        es.activateNewSheet = false;
        es.selected.clear();
        es.canvas.requestFit();
    }
    if (es.mirror.kind() != es.lastKind) {
        es.canvas.requestFit();
        es.lastKind = es.mirror.kind();
    }
}

// Called from layout.cpp's Electronics branch of drawLeftPane, BEFORE drawEcadEditor runs this same
// frame — so route state() to the active ECAD tab here too (drawEcadEditor does it again a few lines
// later; both are idempotent, g_app just needs to point at the live AppState before state() is used).
void drawEcadManagerBody(AppState& st, const ui::AdaptiveSurface& surf, unsigned int halo) {
    g_app = &st;
    drawEcadManagerBodyImpl(st, surf, (ImU32)halo);
}

const char* ecadManagerTitle() {
    EcadEditorState& es = state();
    if (!es.mirror.hasDoc()) return "PCB Manager";
    return es.mirror.isPcb() ? "PCB Manager" : "Schematic Manager";
}

// See ecad_panel.h. Call once at the top of the app frame — cheap (one pointer store) — so a
// workspace-entry RPC adopt (ecadAutoInitBoard's ecad_new -> ecadAdopt) that fires before either draw
// function has run this session still resolves state() to the real per-tab EcadEditorState instead of
// the process-wide fallback.
void ecadPrimeActiveApp(AppState& st) { g_app = &st; }

// Draw the PCB DRC / unrouted summary as a right-aligned glass pill in the ribbon band's trailing
// dead space (item: the bottom results strip moved up here so the board canvas can fill the height).
// No-op for schematic docs (they keep their own bottom ERC strip) and when no board is open.
void drawEcadRibbonStatus(AppState& st, float bandTopY, float bandH) {
    g_app = &st;
    EcadEditorState& es = state();
    EcadMirror& m = es.mirror;
    if (!m.hasDoc() || !m.isPcb()) return;
    const ui::Palette& pal = ui::palette;
    const float sc = ui::uiScale;

    int errs = 0, warns = 0;
    for (const auto& v : m.drc()) (v.severity == "error" ? errs : warns)++;
    const int unrouted = m.unroutedCount();

    char buf[128];
    std::snprintf(buf, sizeof(buf), "DRC  %d err   %d warn    |    Unrouted %d", errs, warns, unrouted);

    ImGuiViewport* vp = ImGui::GetMainViewport();
    ImDrawList* dl = ImGui::GetForegroundDrawList(vp);
    const ImVec2 tsz = ImGui::CalcTextSize(buf);
    const float padX = 12.0f * sc, padY = 5.0f * sc, rightMargin = 24.0f * sc;
    const float cy = bandTopY + bandH * 0.5f;
    ImVec2 p1(vp->Pos.x + vp->Size.x - rightMargin, cy + tsz.y * 0.5f + padY);
    ImVec2 p0(p1.x - tsz.x - 2.0f * padX, cy - tsz.y * 0.5f - padY);
    ui::glassRowBackground(dl, p0, p1, 6.0f * sc, false, false, 0.24f);
    const ImU32 col = (errs > 0)                     ? ui::Palette::toU32(pal.statusErr)
                    : (warns > 0 || unrouted > 0)    ? ui::Palette::toU32(pal.statusWarn)
                                                     : ui::Palette::toU32(pal.statusOk);
    dl->AddText(ImVec2(p0.x + padX, cy - tsz.y * 0.5f), col, buf);
}

void drawEcadEditor(AppState& st) {
    g_app = &st;                  // Wave 2: route state() to this frame's active ECAD tab
    EcadEditorState& es = state();
    const ui::Palette& pal = ui::palette;

    // Pop the ECAD tab forward the frame a document first appears (New Schematic/PCB, or a shot),
    // so the editor surfaces instead of staying hidden behind the Viewport tab.
    static bool prevHadDoc = false;
    const bool hasDocNow = es.mirror.hasDoc();
    if (hasDocNow && !prevHadDoc) ImGui::SetNextWindowFocus();  // applies to the Begin below
    prevHadDoc = hasDocNow;

    // Zero window padding so the PCB canvas is full-bleed like the 3D viewport — its edges reach the
    // ribbon, the window sides, and the status bar (the grid extends flush to the frame
    // so the PCB Manager / file strip / copilot get their proper spacing around it).
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0.0f, 0.0f));
    const bool ecadOpen = ImGui::Begin("ECAD");
    ImGui::PopStyleVar();
    if (!ecadOpen) { ImGui::End(); return; }
    ecadDockStripDraw(st);   // hide the redundant dock tab; app.cpp owns the filename strip

    bool emptyStarter = false;
    if (st.activeTab >= 0 && st.activeTab < static_cast<int>(st.docTabs.size())) {
        const auto& tab = st.docTabs[st.activeTab];
        emptyStarter = tab.kind == AppState::DocTab::TabKind::PCB && tab.isStarter &&
                       !tab.dirty && tab.path.empty() && es.mirror.isPcb() &&
                       tab.ecadDocId == es.mirror.docId() && !es.mirror.hasBoardContent();
    }
    // Manager/ribbon placement is also an explicit choice to work on the canvas.
    if (emptyStarter && st.ecadTool != 0) es.starterCanvasChosen = true;
    if (!es.mirror.hasDoc() || (emptyStarter && !es.starterCanvasChosen)) {
        drawEcadLaunchpad(st, emptyStarter);
        ImGui::End();
        return;
    }

    // The document tab strip (ui::drawDocTabs) is a FLOATING overlay pinned flush under the ribbon
    // (see doc_tabs.cpp "pinned floating overlay window" note) — it reserves no layout space in this
    // window. The Model viewport doesn't reserve space for it either (viewport_panel.cpp's
    // vpDockStripDraw): its 3D image is full-bleed and the overlay simply floats on top. The ECAD
    // canvas now follows the SAME convention (item 4) — no toolbar chrome above it any more (Zoom
    // Fit / Select-Place-Route / Layers / Undo-Redo / Run DRC / the File Display badge all moved into
    // the pinned left column's PCB/Schematic Manager body — see ecad_panel.h::drawEcadManagerBody and
    // layout.cpp's Electronics branch of drawLeftPane), so the canvas reaches all the way up to the
    // ribbon exactly like the 3D viewport does, with the floating doc tabs over its top-left corner.

    // ---- keyboard history: Ctrl+Z = undo, Ctrl+Y / Ctrl+Shift+Z = redo ----
    // GATED on the ECAD window (incl. its child windows) being focused so it never collides with the
    // CAD doc's global Ctrl+Z (app.cpp), which fires whenever st.docMirror.hasDoc() regardless of
    // focus. Skip while a text field has focus (the field owns its own edit history).
    if (ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows) &&
        !ImGui::GetIO().WantTextInput) {
        const auto chord = [](ImGuiKey key, bool shift = false) {
            const ImGuiKeyChord mods = (ImGui::GetIO().KeySuper ? ImGuiMod_Super : ImGuiMod_Ctrl) |
                                       (shift ? ImGuiMod_Shift : 0);
            return ImGui::IsKeyChordPressed(mods | key);
        };
        if (chord(ImGuiKey_Z, true) || chord(ImGuiKey_Y)) st.ecadRedoGo = true;
        else if (chord(ImGuiKey_Z)) st.ecadUndoGo = true;
        else if (chord(ImGuiKey_C)) copySelection(es);
        else if (chord(ImGuiKey_X)) { copySelection(es); deleteSelected(st, es); }
        else if (chord(ImGuiKey_V)) pasteClipboard(st, es);
        else if (chord(ImGuiKey_D)) { copySelection(es); pasteClipboard(st, es); }
        else if (chord(ImGuiKey_A)) selectAll(es);
    }

    // PCB reclaims the WHOLE height — the DRC/unrouted readout moved to the ribbon band's dead space
    // (drawEcadRibbonStatus, called from app.cpp), so the board canvas now reaches the bottom of the
    // screen exactly like the Model tab's 3D viewport. The schematic keeps a bottom
    // strip: it hosts the ERC list AND the simulation waveform plot, which have nowhere else to live.
    const float bottomH = ecadBottomStripHeight(st, es.mirror);
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    const float mainH = bottomH > 0.0f ? (avail.y - bottomH) : 0.0f;  // 0 => fill remaining height
    es.bottomStripTopScreenY =
        bottomH > 0.0f ? ImGui::GetCursorScreenPos().y + mainH : 0.0f;

    // Canvas. The pinned Manager column floats over this window's left side, so the canvas starts
    // at the column's published edge: its origin, zoom-to-fit and hit area then cover only what is
    // visible (fitting the full window centred boards partly underneath the column and cut their
    // traces at the left edge). The Copilot side keeps the canvas's own visible-strip fit. The strip
    // under the column keeps the canvas fill so the column's glass backdrop is unchanged.
    {
        const ImVec2 hostMin = ImGui::GetCursorScreenPos();
        const float canvasX = ecadClearSpan().x0;
        if (canvasX > hostMin.x) {
            const float hostH = mainH > 0.0f ? mainH : avail.y;
            ImGui::GetWindowDrawList()->AddRectFilled(
                hostMin, ImVec2(canvasX, hostMin.y + hostH),
                ui::Palette::toU32(pal.viewportBottom));
            ImGui::SetCursorScreenPos(ImVec2(canvasX, hostMin.y));
        }
    }
    ImGui::BeginChild("##ecadCanvasHost",
                      ImVec2(0, mainH), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    const ImVec2 canvasAvail = ImGui::GetContentRegionAvail();
    // Push ordered typed selection and transient edit overlays into the canvas.
    es.canvas.setSelection(st.ecadTool == 0 ? es.selected : std::vector<std::string>{});
    const std::string netPrefix = "net:";
    es.canvas.setHighlightedNet(
        es.mgrSel.rfind(netPrefix, 0) == 0 ? es.mgrSel.substr(netPrefix.size())
                                          : std::string());
    es.canvas.setDragOffset(es.dragDx, es.dragDy);
    es.canvas.setMarquee(es.marquee, es.dragStart, es.marqueeEnd);
    es.canvas.setShowErcMarkers(es.mirror.isSch());
    es.canvas.setShowDrcMarkers(es.mirror.isPcb());
    // Sheet border + title block: the affordance that makes a schematic look like a
    // drawing and previews what ecad_export(kind="svg"|"pdf") will plot. It was briefly
    // disabled because the frame painted beneath the translucent pinned rail; the canvas
    // now clips to the rail's published edge, so it is on for schematics.
    es.canvas.setShowSheetFrame(es.mirror.isSch());
    EcadCanvasIO io = es.canvas.draw(es.mirror, canvasAvail);
    handleCanvasEdit(st, es, io);  // Wave Q: select/place/route + delete/drag on the canvas
    // Surface the world cursor in the corner.
    if (io.hasWorldMouse) {
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%.2f, %.2f mm", io.worldMouse.x, io.worldMouse.y);
        ImVec2 p = ImGui::GetWindowPos();
        ImGui::GetWindowDrawList()->AddText(
            ImVec2(p.x + ui::metrics::px(8), p.y + ImGui::GetWindowHeight() - ui::metrics::px(20)),
            ui::Palette::toU32(pal.textDim), buf);
    }
    ImGui::EndChild();

    // Bottom area (schematic only): ERC results, plus a Simulation waveform tab when results are
    // present. PCB has no bottom strip any more — its canvas fills the height and the DRC summary
    // lives in the ribbon band (drawEcadRibbonStatus).
    if (bottomH > 0.0f) {
        ImGui::BeginChild("##ecadStrip", ImVec2(0, 0), ImGuiChildFlags_Border);
        const bool haveSim = es.mirror.isSch() && st.ecadSimResults.is_object() &&
                             !st.ecadSimResults.empty();
        if (haveSim && ImGui::BeginTabBar("##ecadBottomTabs")) {
            if (ImGui::BeginTabItem(es.mirror.isSch() ? "ERC" : "DRC")) {
                drawResultsStrip(st, es);
                ImGui::EndTabItem();
            }
            // Force the Simulation tab forward the frame results first appear (or change).
            ImGuiTabItemFlags simFlags = ImGuiTabItemFlags_None;
            if (es.lastSimResults != st.ecadSimResults) {
                simFlags = ImGuiTabItemFlags_SetSelected;
                es.lastSimResults = st.ecadSimResults;
                es.lastAnalysis = st.ecadSimAnalysis;
            }
            if (ImGui::BeginTabItem("Simulation", nullptr, simFlags)) {
                drawSimPlot(st.ecadSimResults, es.lastAnalysis);
                ImGui::EndTabItem();
            }
            ImGui::EndTabBar();
        } else {
            drawResultsStrip(st, es);
        }
        ImGui::EndChild();
    }

    ImGui::End();
}

}  // namespace ui
