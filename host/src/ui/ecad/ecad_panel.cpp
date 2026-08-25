// Native ECAD schematic/PCB editor and per-document pinned manager.
#include "imgui.h"
#include "imgui_internal.h"   // GetWindowDockNode + NoTabBar for the shared central-node doc tabs

#include "ecad_panel.h"
#include "../doc_tabs.h"      // document tabs share the central node with the Viewport (see below)
#include "../feature_tree.h"  // fmPaneHeaderChip/fmSectionDivider/fmRowPill/fmToolbarIconBtn — the
                              // ECAD manager body reuses the Feature Manager's own row/header chrome
                              // so the two panels read as the same design language (item 2).
#include "../metrics.h"
#include "../widgets/kit.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <memory>   // Wave 2: std::make_shared<EcadEditorState> for per-DocTab editor state
#include <string>
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
    int schPlaceKind = 0;       // 0 symbol, 1 net label, 2 power port, 3 junction, 4 no-connect
    int pcbPlaceKind = 0;       // 0 footprint, 1 via
    char placeLabel[96] = "NET";
    int placeLabelScope = 0;    // 0 local, 1 global, 2 hierarchical
    char placePowerNet[64] = "GND";
    std::string placePowerSymbol = "GND";
    std::string activeLayer = "F.Cu";
    std::string activeNet;
    float routeWidth = 0.25f;
    float viaDrill = 0.30f;
    float viaDiameter = 0.60f;
    float snapGrid = 0.50f;
    bool  routing = false;
    float lastX = 0, lastY = 0;
    // Select-tool state: highlighted entity + live drag-move (symbols/footprints only).
    std::string selected;        // "<kind>:<id>" ("" = nothing), kind in {s,fp,w,t,v}
    bool  dragging = false;
    float dragDx = 0, dragDy = 0;
    // Property inspector buffers (the selected symbol/footprint's editable Ref + Value). Reloaded
    // from the mirror whenever `selected` changes (tracked by editLoadedFor) so edits never bleed
    // between parts; committed via sch_edit_symbol / pcb_edit_footprint.
    char  editRef[64] = "";
    char  editValue[96] = "";
    // Schematic-only extras: footprint assignment, do-not-populate, and free-form sim-params JSON.
    char  editFootprint[128] = "";
    bool  editDnp = false;
    char  editSimParams[512] = "";  // sim_params as JSON text (parsed on Apply)
    std::string editLoadedFor;   // selection id the buffers were filled from ("" = stale/empty)
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
        int unlinked = 0;
        for (const auto& s : es.mirror.symbols())
            if (!s.dnp && s.footprint.empty()) ++unlinked;
        if (es.mirror.symbols().empty()) {
            es.healthMsg = "Empty schematic — place symbols or use Design with AI.";
            es.healthLevel = 1;
        } else if (unlinked > 0) {
            es.healthMsg = std::to_string(unlinked) +
                " component(s) have no footprint assigned — assign before Update Board.";
            es.healthLevel = 1;
        } else {
            es.healthMsg = "Schematic OK — all components have footprints.";
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

// Hit-test the mirror at world point w (tolerance worldTol mm). The per-document Inspect filters
// are authoritative: disabling a kind removes it from pick eligibility, not merely from the UI.
std::string hitTest(const EcadEditorState& es, const EcadPt& w, float worldTol) {
    const EcadMirror& m = es.mirror;
    std::string best;
    float bestD = 1e30f;
    if (m.isSch()) {
        if (es.pickSymbols) {
            const float bodyR = std::max(worldTol, 2.6f);
            for (const auto& s : m.symbols()) {
                const float d = std::hypot(w.x - s.pos.x, w.y - s.pos.y);
                if (d <= bodyR && d < bestD) { bestD = d; best = "s:" + s.id; }
            }
            if (!best.empty()) return best;
        }
        if (es.pickWires) {
            for (const auto& wire : m.wires()) {
                for (size_t i = 1; i < wire.pts.size(); ++i) {
                    const float d = ptSegDistP(w, wire.pts[i - 1], wire.pts[i]);
                    if (d <= worldTol && d < bestD) { bestD = d; best = "w:" + wire.id; }
                }
            }
        }
    } else if (m.isPcb()) {
        if (es.pickFootprints) {
            for (const auto& fp : m.footprints()) {
                float bodyR = std::max(worldTol, 1.5f);
                if (fp.hasCourtyard) {
                    bodyR = std::max(bodyR, 0.5f * std::hypot(
                        fp.courtyardMax.x - fp.courtyardMin.x,
                        fp.courtyardMax.y - fp.courtyardMin.y));
                }
                const float d = std::hypot(w.x - fp.pos.x, w.y - fp.pos.y);
                if (d <= bodyR && d < bestD) { bestD = d; best = "fp:" + fp.id; }
            }
            if (!best.empty()) return best;
        }
        if (es.pickVias) {
            for (const auto& via : m.vias()) {
                const float d = std::hypot(w.x - via.at.x, w.y - via.at.y);
                const float tol = std::max(worldTol, via.diameter * 0.5f);
                if (d <= tol && d < bestD) { bestD = d; best = "v:" + via.id; }
            }
            if (!best.empty()) return best;
        }
        if (es.pickTracks) {
            for (const auto& t : m.tracks()) {
                const float tol = std::max(worldTol, t.width * 0.5f);
                const float d = ptSegDistP(w, t.p1, t.p2);
                if (d <= tol && d < bestD) { bestD = d; best = "t:" + t.id; }
            }
        }
    }
    return best;
}

// Look up the world pos of the selected symbol/footprint (the only movable kinds). Returns false if
// the selection is not a sym/fp or the id is gone.
bool selectedPos(const EcadMirror& m, const std::string& sel, EcadPt& out) {
    const auto c = sel.find(':');
    if (c == std::string::npos) return false;
    const std::string kind = sel.substr(0, c), id = sel.substr(c + 1);
    if (kind == "s") {
        for (const auto& s : m.symbols()) if (s.id == id) { out = s.pos; return true; }
    } else if (kind == "fp") {
        for (const auto& fp : m.footprints()) if (fp.id == id) { out = fp.pos; return true; }
    }
    return false;
}

// Rotate (+90 deg CCW) or flip (mirror/side toggle) the selected symbol/footprint, emitting
// sch_edit_symbol (angle/mirror) or pcb_edit_footprint (angle/side) through the shared
// st.ecadPendingOps/ecadOpsGo path. No-op unless a symbol/footprint is selected. Reads the current
// transform from the mirror so each press advances from the live value.
void rotateOrFlipSelected(AppState& st, EcadEditorState& es, bool rotate) {
    const auto c = es.selected.find(':');
    if (c == std::string::npos) return;
    const std::string kind = es.selected.substr(0, c), id = es.selected.substr(c + 1);
    nlohmann::json op;
    if (kind == "s") {
        const EcadSymbol* sym = nullptr;
        for (const auto& s : es.mirror.symbols()) if (s.id == id) { sym = &s; break; }
        if (!sym) return;
        op = {{"op", "sch_edit_symbol"}, {"id", id}};
        if (rotate) op["angle"]  = std::fmod(sym->angle + 90.0f, 360.0f);
        else        op["mirror"] = (sym->mirror == "none" ? "x" : "none");  // toggle horizontal flip
    } else if (kind == "fp") {
        const EcadFootprint* fp = nullptr;
        for (const auto& f : es.mirror.footprints()) if (f.id == id) { fp = &f; break; }
        if (!fp) return;
        op = {{"op", "pcb_edit_footprint"}, {"id", id}};
        if (rotate) op["angle"] = std::fmod(fp->angle + 90.0f, 360.0f);
        else        op["side"]  = (fp->side == "top" ? "bottom" : "top");   // flip to the other side
    } else {
        return;  // wires/tracks don't rotate
    }
    nlohmann::json ops = nlohmann::json::array();
    ops.push_back(op);
    st.ecadPendingOps = ops; st.ecadOpsGo = true;
}

// Narrow-rail property editor for the selected symbol/footprint. Buffers reload on selection
// changes, then commit through the same contract-valid edit ops used by canvas hotkeys.
bool drawPropInspector(AppState& st, EcadEditorState& es, float sc) {
    (void)sc;
    const auto c = es.selected.find(':');
    if (c == std::string::npos) return false;
    const std::string kind = es.selected.substr(0, c), id = es.selected.substr(c + 1);
    const bool sch = kind == "s";
    if (!sch && kind != "fp") return false;

    const std::string* ref = nullptr;
    const std::string* value = nullptr;
    std::string libLabel;
    const EcadSymbol* sym = nullptr;
    if (sch) {
        for (const auto& s : es.mirror.symbols()) {
            if (s.id == id) {
                sym = &s;
                ref = &s.ref;
                value = &s.value;
                libLabel = s.libid;
                break;
            }
        }
    } else {
        for (const auto& fp : es.mirror.footprints()) {
            if (fp.id == id) {
                ref = &fp.ref;
                value = &fp.value;
                libLabel = fp.fpid;
                break;
            }
        }
    }
    if (!ref) return false;

    if (es.editLoadedFor != es.selected) {
        std::snprintf(es.editRef, sizeof(es.editRef), "%s", ref->c_str());
        std::snprintf(es.editValue, sizeof(es.editValue), "%s", value->c_str());
        if (sym) {
            std::snprintf(es.editFootprint, sizeof(es.editFootprint), "%s", sym->footprint.c_str());
            es.editDnp = sym->dnp;
            const std::string params =
                sym->simParams.is_object() && !sym->simParams.empty() ? sym->simParams.dump() : "";
            std::snprintf(es.editSimParams, sizeof(es.editSimParams), "%s", params.c_str());
        } else {
            es.editFootprint[0] = '\0';
            es.editDnp = false;
            es.editSimParams[0] = '\0';
        }
        es.editLoadedFor = es.selected;
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
    if (ui::kit::primaryButton("Apply properties", true, nullptr, ImVec2(-1.0f, 0.0f)))
        commit = true;
    if (commit) {
        st.ecadPendingOps = nlohmann::json::array(
            {{{"op", sch ? "sch_edit_symbol" : "pcb_edit_footprint"},
              {"id", id}, {"ref", es.editRef}, {"value", es.editValue}}});
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

    if (sch) {
        ui::kit::sectionHeader("Assembly", nullptr, nullptr, -1);
        static const char* kFps[] = {
            "Resistor_SMD:R_0603", "Capacitor_SMD:C_0603",
            "Package_SO:SOIC-8", "Package_TO_SOT_SMD:SOT-23"};
        ImGui::SetNextItemWidth(-1.0f);
        if (ImGui::BeginCombo("##ecadFootprintLibrary",
                              es.editFootprint[0] ? es.editFootprint : "No footprint")) {
            for (const char* fp : kFps) {
                if (ImGui::Selectable(fp, std::string(es.editFootprint) == fp))
                    std::snprintf(es.editFootprint, sizeof(es.editFootprint), "%s", fp);
            }
            ImGui::EndCombo();
        }
        ImGui::SetNextItemWidth(-1.0f);
        bool fpCommit = ImGui::InputTextWithHint("##ecadFootprint",
            "Footprint id (blank clears)", es.editFootprint, sizeof(es.editFootprint),
            ImGuiInputTextFlags_EnterReturnsTrue);
        if (ui::kit::secondaryButton("Assign footprint", true, nullptr, ImVec2(-1.0f, 0.0f)))
            fpCommit = true;
        if (fpCommit) {
            nlohmann::json op = {{"op", "sch_edit_symbol"}, {"id", id}};
            op["footprint"] = es.editFootprint[0] == '\0'
                ? nlohmann::json(nullptr) : nlohmann::json(std::string(es.editFootprint));
            st.ecadPendingOps = nlohmann::json::array({op});
            st.ecadOpsGo = true;
            es.editLoadedFor.clear();
        }
        if (ImGui::Checkbox("Do not populate", &es.editDnp)) {
            st.ecadPendingOps = nlohmann::json::array(
                {{{"op", "sch_edit_symbol"}, {"id", id}, {"dnp", es.editDnp}}});
            st.ecadOpsGo = true;
            es.editLoadedFor.clear();
        }

        ui::kit::dimText("Simulation parameters (JSON object)");
        ImGui::SetNextItemWidth(-1.0f);
        ImGui::InputTextMultiline("##ecadSimParams", es.editSimParams,
                                  sizeof(es.editSimParams),
                                  ImVec2(-1.0f, ui::metrics::U(12.0f)));
        if (ui::kit::secondaryButton("Apply simulation parameters", true, nullptr,
                                     ImVec2(-1.0f, 0.0f))) {
            const std::string txt(es.editSimParams);
            const bool blank = txt.find_first_not_of(" \t\r\n") == std::string::npos;
            nlohmann::json parsed;
            bool valid = true;
            if (!blank) {
                parsed = nlohmann::json::parse(txt, nullptr, false);
                valid = !parsed.is_discarded() && parsed.is_object();
            }
            if (!valid) {
                st.toast(ui::ToastLevel::Warn, "Simulation parameters must be a JSON object");
            } else {
                nlohmann::json op = {{"op", "sch_edit_symbol"}, {"id", id}};
                op["sim_params"] = blank ? nlohmann::json(nullptr) : parsed;
                st.ecadPendingOps = nlohmann::json::array({op});
                st.ecadOpsGo = true;
                es.editLoadedFor.clear();
            }
        }
    }
    return true;
}
bool deleteSelected(AppState& st, EcadEditorState& es) {
    const auto c = es.selected.find(':');
    if (c == std::string::npos) return false;
    const std::string kind = es.selected.substr(0, c), id = es.selected.substr(c + 1);
    const char* opName = nullptr;
    if (kind == "s") opName = "sch_delete_symbol";
    else if (kind == "w") opName = "sch_delete_wire";
    else if (kind == "fp") opName = "pcb_delete_footprint";
    else if (kind == "t") opName = "pcb_delete_track";
    else if (kind == "v") opName = "pcb_delete_via";
    if (!opName) return false;
    st.ecadPendingOps = nlohmann::json::array({{{"op", opName}, {"id", id}}});
    st.ecadOpsGo = true;
    es.selected.clear();
    es.editLoadedFor.clear();
    return true;
}

bool focusSelected(EcadEditorState& es) {
    const auto c = es.selected.find(':');
    if (c == std::string::npos) return false;
    const std::string kind = es.selected.substr(0, c), id = es.selected.substr(c + 1);
    EcadPt p;
    bool found = false;
    if (kind == "s") {
        for (const auto& s : es.mirror.symbols())
            if (s.id == id) { p = s.pos; found = true; break; }
    } else if (kind == "fp") {
        for (const auto& fp : es.mirror.footprints())
            if (fp.id == id) { p = fp.pos; found = true; break; }
    } else if (kind == "w") {
        for (const auto& w : es.mirror.wires()) {
            if (w.id != id || w.pts.empty()) continue;
            p = {(w.pts.front().x + w.pts.back().x) * 0.5f,
                 (w.pts.front().y + w.pts.back().y) * 0.5f};
            found = true;
            break;
        }
    } else if (kind == "t") {
        for (const auto& t : es.mirror.tracks()) {
            if (t.id != id) continue;
            p = {(t.p1.x + t.p2.x) * 0.5f, (t.p1.y + t.p2.y) * 0.5f};
            found = true;
            break;
        }
    } else if (kind == "v") {
        for (const auto& v : es.mirror.vias())
            if (v.id == id) { p = v.at; found = true; break; }
    }
    if (!found) return false;
    es.canvas.focusOn(p);
    return true;
}


// Select-tool interaction: pick/highlight on click, Delete to remove, left-drag to move a
// symbol/footprint (emits sch_edit_symbol / pcb_edit_footprint on release, grid-snapped). Sets
// es.dragDx/dragDy each frame for the canvas's live visual feedback. Emits ops via
// st.ecadPendingOps / st.ecadOpsGo, same mechanism as Place/Route.
void handleSelectEdit(AppState& st, EcadEditorState& es, const EcadCanvasIO& io) {
    const bool sch = es.mirror.isSch();
    const float g = sch ? 1.27f : std::max(0.01f, es.snapGrid);
    const float pxTol = 9.0f;
    const float worldTol = pxTol / std::max(0.01f, es.canvas.zoom());

    // ---- drag-move of the selected symbol/footprint ----
    // Begin a drag when a left-drag starts over the (already) selected, movable entity.
    if (!es.dragging && io.hovered && ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
        EcadPt selPos;
        if (selectedPos(es.mirror, es.selected, selPos)) {
            // Anchor the drag only if it started on/near the selected body (avoid grabbing on a
            // far-away drag, which should pan-free in Select mode does nothing).
            const ImVec2 d0 = ImGui::GetMouseDragDelta(ImGuiMouseButton_Left);
            const EcadPt startWorld{io.worldMouse.x - d0.x / es.canvas.zoom(),
                                    io.worldMouse.y + d0.y / es.canvas.zoom()};
            float bodyR = std::max(worldTol, sch ? 2.6f : 1.5f);
            if (std::hypot(startWorld.x - selPos.x, startWorld.y - selPos.y) <= bodyR * 1.5f)
                es.dragging = true;
        }
    }
    if (es.dragging) {
        const ImVec2 d = ImGui::GetMouseDragDelta(ImGuiMouseButton_Left);
        es.dragDx = d.x / es.canvas.zoom();
        es.dragDy = -d.y / es.canvas.zoom();  // screen down -> world down
        if (ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
            EcadPt selPos;
            if (selectedPos(es.mirror, es.selected, selPos) &&
                (std::fabs(es.dragDx) > 1e-4f || std::fabs(es.dragDy) > 1e-4f)) {
                const float nx = snapTo(selPos.x + es.dragDx, g);
                const float ny = snapTo(selPos.y + es.dragDy, g);
                const std::string id = es.selected.substr(es.selected.find(':') + 1);
                nlohmann::json ops = nlohmann::json::array();
                if (sch) ops.push_back({{"op", "sch_edit_symbol"}, {"id", id}, {"pos", {nx, ny}}});
                else     ops.push_back({{"op", "pcb_edit_footprint"}, {"id", id}, {"pos", {nx, ny}}});
                st.ecadPendingOps = ops; st.ecadOpsGo = true;
            }
            es.dragging = false;
            es.dragDx = es.dragDy = 0.0f;
        }
        return;  // a drag in progress consumes the interaction (no select-on-click)
    }
    es.dragDx = es.dragDy = 0.0f;

    // ---- Rotate (R) / flip (F) the selected symbol/footprint. Gated on the canvas being hovered
    // and no text field having focus, so the keys never fire while typing a Ref/Value (KiCad binds
    // R = rotate, F = flip). ----
    if (!es.selected.empty() && io.hovered && !ImGui::GetIO().WantTextInput) {
        if (ImGui::IsKeyPressed(ImGuiKey_R)) { rotateOrFlipSelected(st, es, /*rotate=*/true);  return; }
        if (ImGui::IsKeyPressed(ImGuiKey_F)) { rotateOrFlipSelected(st, es, /*rotate=*/false); return; }
    }

    if (!es.selected.empty() && io.hovered && ImGui::IsKeyPressed(ImGuiKey_Delete)) {
        deleteSelected(st, es);
        return;
    }

    // ---- pick on click (hit-test nearest eligible entity; empty space clears) ----
    if (io.clicked && io.hasWorldMouse) {
        es.selected = hitTest(es, io.worldMouse, worldTol);
        es.editLoadedFor.clear();
    }
}

// Wave Q — translate a canvas click into a place/route op for the active tool. Sets
// st.ecadPendingOps / st.ecadOpsGo; the app loop applies them via ecad_apply_ops + reloads.
void handleCanvasEdit(AppState& st, EcadEditorState& es, const EcadCanvasIO& io) {
    // Esc or right-click ends an in-progress route chain.
    if (es.routing && (ImGui::IsKeyPressed(ImGuiKey_Escape) ||
                       ImGui::IsMouseClicked(ImGuiMouseButton_Right)))
        es.routing = false;
    if (st.ecadTool == 0) { handleSelectEdit(st, es, io); return; }
    if (!io.clicked || !io.hasWorldMouse) return;

    const bool sch = es.mirror.isSch();
    const float g = sch ? 1.27f : std::max(0.01f, es.snapGrid);
    const float gx = snapTo(io.worldMouse.x, g), gy = snapTo(io.worldMouse.y, g);
    nlohmann::json ops = nlohmann::json::array();

    if (st.ecadTool == 1) {
        if (sch) {
            static const char* kScopes[] = {"local", "global", "hier"};
            if (es.schPlaceKind == 0) {
                ops.push_back({{"op", "sch_add_symbol"}, {"libid", es.placeLib},
                               {"pos", {gx, gy}}});
            } else if (es.schPlaceKind == 1) {
                if (es.placeLabel[0] == '\0') {
                    st.toast(ui::ToastLevel::Warn, "Enter net label text before placing");
                } else {
                    ops.push_back({{"op", "sch_add_label"}, {"pos", {gx, gy}},
                                   {"text", es.placeLabel},
                                   {"scope", kScopes[es.placeLabelScope]}});
                }
            } else if (es.schPlaceKind == 2) {
                if (es.placePowerNet[0] == '\0') {
                    st.toast(ui::ToastLevel::Warn, "Enter a power net before placing");
                } else {
                    ops.push_back({{"op", "sch_add_power"}, {"pos", {gx, gy}},
                                   {"net", es.placePowerNet},
                                   {"symbol", es.placePowerSymbol}});
                }
            } else if (es.schPlaceKind == 3) {
                ops.push_back({{"op", "sch_add_junction"}, {"pos", {gx, gy}}});
            } else if (es.schPlaceKind == 4) {
                ops.push_back({{"op", "sch_add_no_connect"}, {"pos", {gx, gy}}});
            }
        } else if (es.pcbPlaceKind == 0) {
            ops.push_back({{"op", "pcb_add_footprint"}, {"fpid", es.placeFp},
                           {"pos", {gx, gy}}});
        } else {
            nlohmann::json op = {{"op", "pcb_add_via"}, {"at", {gx, gy}},
                                 {"drill", std::max(0.05f, es.viaDrill)},
                                 {"diameter", std::max(es.viaDiameter, es.viaDrill + 0.10f)}};
            if (!es.activeNet.empty()) op["net"] = es.activeNet;
            if (es.mirror.layers().size() >= 2) {
                op["layers"] = {es.mirror.layers().front().id,
                                es.mirror.layers().back().id};
            }
            ops.push_back(op);
        }
    } else if (st.ecadTool == 2) {
        if (es.routing) {
            if (sch) {
                ops.push_back({{"op", "sch_add_wire"},
                               {"pts", {{es.lastX, es.lastY}, {gx, gy}}}});
            } else {
                nlohmann::json op = {{"op", "pcb_add_track"}, {"layer", es.activeLayer},
                                     {"width", std::max(0.01f, es.routeWidth)},
                                     {"p1", {es.lastX, es.lastY}}, {"p2", {gx, gy}}};
                if (!es.activeNet.empty()) op["net"] = es.activeNet;
                ops.push_back(op);
            }
        }
        es.routing = true;
        es.lastX = gx;
        es.lastY = gy;
    }
    if (!ops.empty()) { st.ecadPendingOps = ops; st.ecadOpsGo = true; }
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
    if (ui::kit::iconButton("ecad_save", "save", "Save (Shift-click: Save As)", true,
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
    ui::kit::dimText("Active copper layer");
    ImGui::SetNextItemWidth(-1.0f);
    if (ImGui::BeginCombo("##activeCopper", es.activeLayer.c_str())) {
        for (const auto& layer : m.layers()) {
            if (ImGui::Selectable(layer.id.c_str(), es.activeLayer == layer.id)) {
                es.activeLayer = layer.id;
                es.mgrSel = "layer:" + layer.id;
            }
        }
        ImGui::EndCombo();
    }
    ui::kit::dimText("Active net");
    ImGui::SetNextItemWidth(-1.0f);
    const char* netPreview = es.activeNet.empty() ? "No net" : es.activeNet.c_str();
    if (ImGui::BeginCombo("##activeNet", netPreview)) {
        if (ImGui::Selectable("No net", es.activeNet.empty())) {
            es.activeNet.clear();
            if (es.mgrSel.rfind("net:", 0) == 0) es.mgrSel.clear();
            es.canvas.setHighlightedNet(std::string());
        }
        for (const auto& net : m.nets()) {
            if (ImGui::Selectable(net.name.c_str(), es.activeNet == net.name)) {
                es.activeNet = net.name;
                es.mgrSel = "net:" + net.name;
                es.canvas.setHighlightedNet(net.name);
            }
        }
        ImGui::EndCombo();
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

void drawCanvasToolContext(AppState& st, EcadEditorState& es) {
    const bool pcb = es.mirror.isPcb();
    ui::kit::sectionHeader("Canvas tools", nullptr, nullptr, -1, !pcb);
    static const char* kToolLabels[] = {"Select", "Place", "Route"};
    static const char* kToolTips[] = {
        "Pick, move, rotate, flip or delete entities",
        "Place the configured schematic or PCB entity",
        "Click points to route; Esc or right-click ends the chain"};
    if (ui::kit::segmented("##ecadTool", kToolLabels, 3, &st.ecadTool,
                           ImGui::GetContentRegionAvail().x, kToolTips))
        es.routing = false;

    if (st.ecadTool == 1 && pcb) {
        static const char* kPlaceLabels[] = {"Footprint", "Via"};
        if (ui::kit::segmented("##pcbPlaceKind", kPlaceLabels, 2, &es.pcbPlaceKind,
                               ImGui::GetContentRegionAvail().x))
            es.routing = false;
        if (es.pcbPlaceKind == 0) {
            static const char* kFps[] = {
                "Resistor_SMD:R_0603", "Capacitor_SMD:C_0603",
                "Package_SO:SOIC-8", "Package_TO_SOT_SMD:SOT-23"};
            ui::kit::dimText("Footprint library");
            ImGui::SetNextItemWidth(-1.0f);
            if (ImGui::BeginCombo("##placeFootprint", es.placeFp.c_str())) {
                for (const char* fp : kFps)
                    if (ImGui::Selectable(fp, es.placeFp == fp)) es.placeFp = fp;
                ImGui::EndCombo();
            }
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
            ui::kit::dimText("Click the board to place a through via on the active net.");
        }
    } else if (st.ecadTool == 1) {
        static const char* kKinds[] = {
            "Symbol", "Net label", "Power port", "Junction", "No-connect"};
        ui::kit::dimText("Placement type");
        ImGui::SetNextItemWidth(-1.0f);
        ImGui::Combo("##schPlaceKind", &es.schPlaceKind, kKinds, 5);
        if (es.schPlaceKind == 0) {
            static const char* kLibs[] = {
                "Device:R", "Device:C", "Device:L", "Device:D",
                "Simulation_SPICE:V_source", "power:GND", "power:VCC"};
            ui::kit::dimText("Symbol library");
            ImGui::SetNextItemWidth(-1.0f);
            if (ImGui::BeginCombo("##placeSymbol", es.placeLib.c_str())) {
                for (const char* lib : kLibs)
                    if (ImGui::Selectable(lib, es.placeLib == lib)) es.placeLib = lib;
                ImGui::EndCombo();
            }
        } else if (es.schPlaceKind == 1) {
            ui::kit::dimText("Net label text");
            ImGui::SetNextItemWidth(-1.0f);
            ImGui::InputText("##placeLabel", es.placeLabel, sizeof(es.placeLabel));
            static const char* kScopes[] = {"Local", "Global", "Hierarchical"};
            ui::kit::dimText("Label scope");
            ImGui::SetNextItemWidth(-1.0f);
            ImGui::Combo("##labelScope", &es.placeLabelScope, kScopes, 3);
        } else if (es.schPlaceKind == 2) {
            ui::kit::dimText("Power net");
            ImGui::SetNextItemWidth(-1.0f);
            ImGui::InputText("##powerNet", es.placePowerNet, sizeof(es.placePowerNet));
            static const char* kPowerSymbols[] = {"GND", "VCC", "+5V", "+3V3"};
            ImGui::SetNextItemWidth(-1.0f);
            if (ImGui::BeginCombo("##powerSymbol", es.placePowerSymbol.c_str())) {
                for (const char* symbol : kPowerSymbols)
                    if (ImGui::Selectable(symbol, es.placePowerSymbol == symbol))
                        es.placePowerSymbol = symbol;
                ImGui::EndCombo();
            }
        } else {
            ui::kit::dimText(es.schPlaceKind == 3
                ? "Click the sheet to place an electrical junction."
                : "Click a pin endpoint to mark it intentionally unconnected.");
        }
    } else if (st.ecadTool == 2) {
        ui::kit::dimText("Click points to add segments. Esc or right-click finishes the route.");
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
    ImGui::SetNextItemWidth(-1.0f);
    ImGui::Combo("##ecadHierarchyKind", &es.hierarchyKind,
                 pcb ? kPcbKinds : kSchKinds, kindCount);

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
            style.selected = es.selected == "fp:" + fp.id;
            if (ui::kit::row(fp.id.c_str(), fp.ref.empty() ? fp.fpid.c_str() : fp.ref.c_str(),
                             style)) {
                es.selected = "fp:" + fp.id;
                es.editLoadedFor.clear();
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
            style.selected = es.selected == "s:" + symbol.id;
            if (ui::kit::row(symbol.id.c_str(),
                             symbol.ref.empty() ? symbol.libid.c_str() : symbol.ref.c_str(),
                             style)) {
                es.selected = "s:" + symbol.id;
                es.editLoadedFor.clear();
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

void drawDesignView(AppState& st, EcadEditorState& es) {
    if (es.mirror.isPcb()) drawPcbEditingContext(es);
    drawCanvasToolContext(st, es);
    drawHierarchy(st, es);
    if (es.mirror.isSch()) {
        ui::kit::sectionHeader("Simulation", "sch_simulate");
        static const char* kAnalyses[] = {"Transient", "AC"};
        ImGui::SetNextItemWidth(-1.0f);
        ImGui::Combo("##ecadAnalysis", &st.ecadSimAnalysis, kAnalyses, 2);
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
        countRow("Copper layers", m.layers().size());
        countRow("Footprints", m.footprints().size());
        countRow("Tracks", m.tracks().size());
        countRow("Vias", m.vias().size());
        countRow("Zones", m.zones().size());
        countRow("Nets", m.nets().size());
        ui::kit::keyValueRow("Unrouted", std::to_string(m.unroutedCount()).c_str(),
                             m.unroutedCount() ? ui::kit::Tone::Warn : ui::kit::Tone::Ok);
    } else {
        countRow("Symbols", m.symbols().size());
        countRow("Wires", m.wires().size());
        countRow("Junctions", m.junctions().size());
        countRow("Net labels", m.labels().size());
        countRow("Power ports", m.powers().size());
        countRow("Nets", m.nets().size());
        int unassigned = 0;
        for (const auto& symbol : m.symbols())
            if (!symbol.dnp && symbol.footprint.empty()) ++unassigned;
        ui::kit::keyValueRow("Missing footprints", std::to_string(unassigned).c_str(),
                             unassigned ? ui::kit::Tone::Warn : ui::kit::Tone::Ok);
    }

    ui::kit::sectionHeader("Selection filters", "filter_funnel");
    if (pcb) {
        if (ui::kit::toggleChip("Footprints", &es.pickFootprints) &&
            !es.pickFootprints && es.selected.rfind("fp:", 0) == 0)
            es.selected.clear();
        ImGui::SameLine(0.0f, ui::kit::rowGap());
        if (ui::kit::toggleChip("Tracks", &es.pickTracks) &&
            !es.pickTracks && es.selected.rfind("t:", 0) == 0)
            es.selected.clear();
        ImGui::SameLine(0.0f, ui::kit::rowGap());
        if (ui::kit::toggleChip("Vias", &es.pickVias) &&
            !es.pickVias && es.selected.rfind("v:", 0) == 0)
            es.selected.clear();
    } else {
        if (ui::kit::toggleChip("Symbols", &es.pickSymbols) &&
            !es.pickSymbols && es.selected.rfind("s:", 0) == 0)
            es.selected.clear();
        ImGui::SameLine(0.0f, ui::kit::rowGap());
        if (ui::kit::toggleChip("Wires", &es.pickWires) &&
            !es.pickWires && es.selected.rfind("w:", 0) == 0)
            es.selected.clear();
    }
    if (es.selected.empty()) es.editLoadedFor.clear();

    ui::kit::sectionHeader("Selected entity", nullptr);
    if (es.selected.empty()) {
        ui::kit::emptyState("zoom_fit", "Nothing selected",
                            "Use Select on the canvas or choose an item in Design.");
        return;
    }

    const auto colon = es.selected.find(':');
    const std::string kind = es.selected.substr(0, colon);
    const std::string id = es.selected.substr(colon + 1);
    bool found = false;
    if (kind == "s") {
        for (const auto& symbol : m.symbols()) if (symbol.id == id) {
            found = true;
            ui::kit::keyValueRow("Entity", "Schematic symbol");
            ui::kit::keyValueRow("Position", pointText(symbol.pos).c_str());
            ui::kit::keyValueRow("Angle", (std::to_string(symbol.angle) + " deg").c_str());
            ui::kit::keyValueRow("Mirror", symbol.mirror.c_str());
            break;
        }
        if (found) drawPropInspector(st, es, ui::uiScale);
    } else if (kind == "fp") {
        for (const auto& fp : m.footprints()) if (fp.id == id) {
            found = true;
            ui::kit::keyValueRow("Entity", "PCB footprint");
            ui::kit::keyValueRow("Position", pointText(fp.pos).c_str());
            ui::kit::keyValueRow("Angle", (std::to_string(fp.angle) + " deg").c_str());
            ui::kit::keyValueRow("Side", fp.side.c_str());
            countRow("Pads", fp.pads.size());
            break;
        }
        if (found) drawPropInspector(st, es, ui::uiScale);
    } else if (kind == "w") {
        for (const auto& wire : m.wires()) if (wire.id == id) {
            found = true;
            float length = 0.0f;
            for (size_t i = 1; i < wire.pts.size(); ++i)
                length += std::hypot(wire.pts[i].x - wire.pts[i - 1].x,
                                     wire.pts[i].y - wire.pts[i - 1].y);
            ui::kit::keyValueRow("Entity", wire.bus ? "Schematic bus" : "Schematic wire");
            ui::kit::keyValueRow("Id", wire.id.c_str(), ui::kit::Tone::Neutral, true);
            countRow("Points", wire.pts.size());
            ui::kit::keyValueRow("Length", mmText(length).c_str());
            if (!wire.pts.empty()) {
                ui::kit::keyValueRow("Start", pointText(wire.pts.front()).c_str());
                ui::kit::keyValueRow("End", pointText(wire.pts.back()).c_str());
            }
            break;
        }
    } else if (kind == "t") {
        for (const auto& track : m.tracks()) if (track.id == id) {
            found = true;
            float length = std::hypot(track.p2.x - track.p1.x, track.p2.y - track.p1.y);
            if (track.kind == "arc") {
                const float radius = std::hypot(track.p1.x - track.center.x,
                                                track.p1.y - track.center.y);
                length = radius * std::fabs(track.a1 - track.a0) * 3.14159265f / 180.0f;
            }
            ui::kit::keyValueRow("Entity", "PCB track");
            ui::kit::keyValueRow("Id", track.id.c_str(), ui::kit::Tone::Neutral, true);
            ui::kit::keyValueRow("Net", track.net.empty() ? "No net" : track.net.c_str());
            ui::kit::keyValueRow("Layer", track.layer.c_str());
            ui::kit::keyValueRow("Width", mmText(track.width).c_str());
            ui::kit::keyValueRow("Geometry", track.kind.c_str());
            ui::kit::keyValueRow("Length", mmText(length).c_str());
            ui::kit::keyValueRow("Start", pointText(track.p1).c_str());
            ui::kit::keyValueRow("End", pointText(track.p2).c_str());
            break;
        }
    } else if (kind == "v") {
        for (const auto& via : m.vias()) if (via.id == id) {
            found = true;
            std::string layers;
            for (const auto& layer : via.layers) {
                if (!layers.empty()) layers += " to ";
                layers += layer;
            }
            ui::kit::keyValueRow("Entity", "PCB via");
            ui::kit::keyValueRow("Id", via.id.c_str(), ui::kit::Tone::Neutral, true);
            ui::kit::keyValueRow("Net", via.net.empty() ? "No net" : via.net.c_str());
            ui::kit::keyValueRow("Position", pointText(via.at).c_str());
            ui::kit::keyValueRow("Drill", mmText(via.drill).c_str());
            ui::kit::keyValueRow("Diameter", mmText(via.diameter).c_str());
            ui::kit::keyValueRow("Span", layers.empty() ? "Default stack" : layers.c_str());
            break;
        }
    }
    if (!found) {
        es.selected.clear();
        ui::kit::emptyState("warning_badge", "Selection is stale",
                            "The entity no longer exists in the current document revision.");
        return;
    }

    const float actionW =
        (ImGui::GetContentRegionAvail().x - ui::kit::rowGap()) * 0.5f;
    if (ui::kit::secondaryButton("Focus", true, nullptr, ImVec2(actionW, 0.0f)))
        focusSelected(es);
    ImGui::SameLine(0.0f, ui::kit::rowGap());
    if (ui::kit::dangerButton("Delete", true, nullptr, ImVec2(actionW, 0.0f)))
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
                es.canvas.focusOn(check.loc);
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
                            "Open a schematic or PCB to use the manager.");
        return;
    }
    const bool pcb = m.isPcb();
    ensurePcbContext(es);
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

    static const char* kViews[] = {"Design", "Inspect", "Checks"};
    static const char* kViewTips[] = {
        "Hierarchy, canvas tools and editing context",
        "Live statistics, pick filters and selected-entity properties",
        "Search and focus individual ERC or DRC violations"};
    ui::kit::segmented("##ecadManagerView", kViews, 3, &es.managerView,
                       ImGui::GetContentRegionAvail().x, kViewTips);

    ImGui::BeginChild("##ecadManagerScroll", ImVec2(0.0f, 0.0f), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar);
    if (es.managerView == 0) drawDesignView(st, es);
    else if (es.managerView == 1) drawInspectView(st, es);
    else drawChecksView(st, es);
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
                es.canvas.focusOn(v.loc);   // pan + zoom + flash the marker
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

}  // namespace

// ---- public API ----

EcadMirror& ecadEditorMirror() { return state().mirror; }

void ecadEditorSelect(const std::string& sel) { state().selected = sel; }
void ecadEditorHighlightNet(const std::string& net) {
    EcadEditorState& es = state();
    es.mgrSel = net.empty() ? std::string() : "net:" + net;
    es.canvas.setHighlightedNet(net);
}

bool ecadEditorFocusFirstViolation() {
    EcadEditorState& es = state();
    const auto& list = es.mirror.isSch() ? es.mirror.erc() : es.mirror.drc();
    for (const auto& v : list)
        if (v.hasLoc) { es.canvas.focusOn(v.loc); return true; }
    return false;
}

void ecadEditorLoadResponse(const nlohmann::json& resp) {
    EcadEditorState& es = state();
    es.mirror.loadFromTree(resp);
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
    const float sc = ui::uiScale;

    // Pop the ECAD tab forward the frame a document first appears (New Schematic/PCB, or a shot),
    // so the editor surfaces instead of staying hidden behind the Viewport tab.
    static bool prevHadDoc = false;
    const bool hasDocNow = es.mirror.hasDoc();
    if (hasDocNow && !prevHadDoc) ImGui::SetNextWindowFocus();  // applies to the Begin below
    prevHadDoc = hasDocNow;

    // Zero window padding so the PCB canvas is full-bleed like the 3D viewport — its edges reach the
    // ribbon, the window sides, and the status bar (Hunter round: extend the grid flush to the frame
    // so the PCB Manager / file strip / copilot get their proper spacing around it).
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0.0f, 0.0f));
    const bool ecadOpen = ImGui::Begin("ECAD");
    ImGui::PopStyleVar();
    if (!ecadOpen) { ImGui::End(); return; }
    ecadDockStripDraw(st);   // hide the redundant dock tab; app.cpp owns the filename strip

    if (!es.mirror.hasDoc()) {
        // Launchpad: describe a circuit -> the AI designs a verified schematic. (Blank docs come
        // from the PCB ribbon's New Schematic / New PCB.) The button hands the request to the app
        // loop via st.ecadAiRequest/ecadAiGo, which runs ecad_design + loads the result here.
        // Reserve the floating doc-tab strip's height first (see the toolbar's identical Dummy below)
        // so the launchpad copy doesn't start under the tab pills.
        ImGui::Dummy(ImVec2(0, ui::docTabStripHeight(st) + 18 * sc));
        ImGui::Indent(24 * sc);
        ImGui::TextColored(v4(pal.accent), "AI-ECAD  -  electronics design");
        ImGui::Spacing();
        ImGui::TextColored(v4(pal.textDim),
            "Describe a circuit and the AI designs a verified schematic, or start a blank");
        ImGui::TextColored(v4(pal.textDim),
            "document from the PCB ribbon (New Schematic / New PCB).");
        ImGui::Dummy(ImVec2(0, 10 * sc));
        static char aibuf[512] = "5V divider with 2.5V output";
        ImGui::SetNextItemWidth(440 * sc);
        ImGui::InputTextWithHint("##ecad_ai", "describe a circuit...", aibuf, sizeof(aibuf));
        ImGui::SameLine();
        if (ImGui::Button("Design with AI", ImVec2(150 * sc, 0))) {
            st.ecadAiRequest = aibuf;
            st.ecadAiGo = true;
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Generate a verified schematic from the description.");
        ImGui::SameLine();
        if (ImGui::Button("AI Board", ImVec2(110 * sc, 0))) {
            // One-click all the way to a routed PCB: design the schematic, then populate +
            // outline + autoroute a board from it (ecadAiBoard in the app loop).
            st.ecadAiRequest = aibuf;
            st.ecadAiBoardGo = true;
        }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Schematic + populated, outlined, autorouted PCB - ready for Board to 3D.");
        ImGui::Dummy(ImVec2(0, 6 * sc));
        ImGui::TextColored(v4(pal.textDisabled),
            "examples:  inverting amp gain 10    RC low-pass 1kHz    555 astable");
        ImGui::Unindent(24 * sc);
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
        if (ImGui::IsKeyChordPressed(ImGuiMod_Ctrl | ImGuiMod_Shift | ImGuiKey_Z) ||
            ImGui::IsKeyChordPressed(ImGuiMod_Ctrl | ImGuiKey_Y)) {
            st.ecadRedoGo = true;
        } else if (ImGui::IsKeyChordPressed(ImGuiMod_Ctrl | ImGuiKey_Z)) {
            st.ecadUndoGo = true;
        }
    }

    // PCB reclaims the WHOLE height — the DRC/unrouted readout moved to the ribbon band's dead space
    // (drawEcadRibbonStatus, called from app.cpp), so the board canvas now reaches the bottom of the
    // screen exactly like the Model tab's 3D viewport (Hunter red-pen). The schematic keeps a bottom
    // strip: it hosts the ERC list AND the simulation waveform plot, which have nowhere else to live.
    const bool isPcbDoc = es.mirror.isPcb();
    const bool hasSimNow = es.mirror.isSch() && st.ecadSimResults.is_object() &&
                           !st.ecadSimResults.empty();
    const float bottomH = isPcbDoc ? 0.0f : (hasSimNow ? 260.0f : 140.0f) * sc;
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    const float mainH = bottomH > 0.0f ? (avail.y - bottomH) : 0.0f;  // 0 => fill remaining height

    // Canvas.
    ImGui::BeginChild("##ecadCanvasHost",
                      ImVec2(0, mainH), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    const ImVec2 canvasAvail = ImGui::GetContentRegionAvail();
    // Push the current selection + live drag offset into the canvas so it haloes the picked entity
    // (and shows drag feedback). Only the Select tool shows a selection.
    es.canvas.setSelection(st.ecadTool == 0 ? es.selected : std::string());
    const std::string netPrefix = "net:";
    es.canvas.setHighlightedNet(
        es.mgrSel.rfind(netPrefix, 0) == 0 ? es.mgrSel.substr(netPrefix.size())
                                          : std::string());
    es.canvas.setDragOffset(es.dragDx, es.dragDy);
    EcadCanvasIO io = es.canvas.draw(es.mirror, canvasAvail);
    handleCanvasEdit(st, es, io);  // Wave Q: select/place/route + delete/drag on the canvas
    // Surface the world cursor in the corner.
    if (io.hasWorldMouse) {
        char buf[64];
        std::snprintf(buf, sizeof(buf), "%.2f, %.2f mm", io.worldMouse.x, io.worldMouse.y);
        ImVec2 p = ImGui::GetWindowPos();
        ImGui::GetWindowDrawList()->AddText(
            ImVec2(p.x + 8, p.y + ImGui::GetWindowHeight() - 20),
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
