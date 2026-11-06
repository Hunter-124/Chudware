// KiCad project hub and per-document read-only ECAD preview.
#include "imgui.h"
#include "imgui_internal.h"   // GetWindowDockNode + NoTabBar for the shared central-node doc tabs

#include "ecad_panel.h"
#include "../doc_tabs.h"      // document tabs share the central node with the Viewport (see below)
#include "../layout.h"        // overlayColumnEdge — the pinned columns float over this window
#include "../metrics.h"
#include "../widgets/kit.h"
#include "../ribbon_def.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <memory>
#include <set>
#include <string>
#include <vector>

#include "../../app_commands.h"
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

// Per-document preview and KiCad state. The opaque type is owned by each ECAD DocTab.
struct EcadEditorState {
    EcadMirror mirror;
    EcadCanvas canvas;
    EcadMirror::Kind lastKind = EcadMirror::Kind::None;
    std::vector<std::string> selected;
    std::string highlightedNet;
    std::string path;
    bool dirty = false;
    bool fileChanged = false;
    bool kicadLocked = false;
    bool hasCheck = false;
    int checkRevision = -1;
    int checkErrors = 0;
    int checkWarnings = 0;
    std::string checkVersion;
    std::vector<EcadViolation> checks;
    float bottomStripTopScreenY = 0.0f;
    nlohmann::json lastSimResults;
    int lastAnalysis = 0;
    int managerView = 0;
    char managerSearch[96] = "";
    char checksSearch[96] = "";
    int checksSeverity = 0;
    bool pickSymbols = true, pickWires = true;
    bool pickFootprints = true, pickTracks = true, pickVias = true;
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
nlohmann::json g_kicadStatus;

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
EcadEditorState* stateForDoc(const std::string& docId) {
    if (docId.empty()) return nullptr;
    if (g_app) {
        for (auto& tab : g_app->docTabs) {
            if (tab.ecadDocId != docId) continue;
            if (!tab.ecadState) tab.ecadState = std::make_shared<EcadEditorState>();
            return tab.ecadState.get();
        }
        if (g_app->ecadDocId == docId && fallbackState().mirror.docId() == docId)
            return &fallbackState();
    }
    return nullptr;
}

float ecadBottomStripHeight(const AppState& st, const EcadMirror& mirror) {
    if (!mirror.isSch()) return 0.0f;
    const bool hasSimulation = st.ecadSimResults.is_object() && !st.ecadSimResults.empty();
    return (hasSimulation ? 260.0f : 140.0f) * ui::uiScale;
}
ImVec4 v4(const float c[4], float a = 1.0f) { return ImVec4(c[0], c[1], c[2], c[3] * a); }



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

bool hasSelection(const EcadEditorState& es, const char* kind, const std::string& id) {
    const size_t n = std::strlen(kind);
    return std::any_of(es.selected.begin(), es.selected.end(), [&](const std::string& token) {
        return token.size() == n + 1 + id.size() && token.compare(0, n, kind) == 0 &&
               token[n] == ':' && token.compare(n + 1, id.size(), id) == 0;
    });
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
}

bool onSheet(const EcadMirror& m, const std::string& sheet) { return m.onActiveSheet(sheet); }

// Fallback picking uses the preview geometry when the worker's resolver is unavailable.
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

bool focusSelected(EcadEditorState& es) {
    EcadPt p;
    if (!selectedPos(es.mirror, primarySelection(es), p)) return false;
    es.canvas.focusOn(p);
    return true;
}

// ---- ECAD manager: dense per-document Design / Inspect / Checks command center -----------------
bool containsFolded(const std::string& text, const char* query) {
    if (!query || *query == '\0') return true;
    return std::search(text.begin(), text.end(), query, query + std::strlen(query),
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

void drawKicadStatus() {
    if (!g_kicadStatus.is_object()) {
        ui::kit::chip("Checking KiCad availability…", ui::kit::Tone::Neutral, true);
        return;
    }
    if (!g_kicadStatus.value("found", false)) {
        ui::kit::chip("KiCad not found — install KiCad 10", ui::kit::Tone::Warn, true);
        return;
    }
    const std::string version = g_kicadStatus.value("version", std::string());
    ui::kit::valueChip("KiCad", version.empty() ? "Installed" : version.c_str(),
                       ui::kit::Tone::Ok);
}

void queueEcadCommand(AppState& st, const char* id, const std::string& docId) {
    st.ecadPanelCmd = id;
    st.ecadPanelDocId = docId;
    if (std::strcmp(id, "ecad_simulate") == 0)
        st.ecadPanelSimAnalysis = st.ecadSimAnalysis;
}

void drawCommand(AppState& st, const CommandContext& context, const char* id,
                 const char* label, bool primary = false) {
    const auto availability = commandAvailability(id, context);
    const bool enabled = availability.visible && availability.enabled;
    const char* reason = availability.reason ? availability.reason : "Unavailable in this context.";
    ImGui::PushID(id);
    const bool clicked = primary
        ? ui::kit::primaryButton(label, enabled, reason, ImVec2(-1.0f, 0.0f))
        : ui::kit::secondaryButton(label, enabled, reason, ImVec2(-1.0f, 0.0f));
    ImGui::PopID();
    if (clicked) {
        const bool projectAction = std::strcmp(id, "ecad_new_project") == 0 ||
                                   std::strcmp(id, "ecad_open") == 0;
        queueEcadCommand(st, id, projectAction ? std::string() : state().mirror.docId());
    }
}


void drawConflict(AppState& st, EcadEditorState& es, const CommandContext& context) {
    if (!es.dirty || !es.fileChanged) return;
    ui::kit::inlineStatus("The KiCad file changed while this document has unsaved changes.",
                          ui::kit::Tone::Warn);
    ui::kit::dimText("Reload discards Chudware changes. Save As keeps them in a new file.");
    drawCommand(st, context, "ecad_reload", "Reload from KiCad");
    drawCommand(st, context, "ecad_save_as", "Save As…");
    if (es.kicadLocked)
        ui::kit::dimText("KiCad has the original file locked. Save As uses a different file.");
}

void drawProjectView(AppState& st, EcadEditorState& es, const CommandContext& context) {
    static const std::string empty;
    const std::string* projectPath = &empty;
    const std::string* schPath = es.mirror.isSch() ? &es.path : &empty;
    const std::string* pcbPath = es.mirror.isPcb() ? &es.path : &empty;
    for (const auto& tab : st.docTabs)
        if (tab.ecadDocId == es.mirror.docId()) {
            projectPath = &tab.ecadProjectPath;
            if (schPath->empty() && !tab.ecadProjectSchPath.empty())
                schPath = &tab.ecadProjectSchPath;
            if (pcbPath->empty() && !tab.ecadProjectPcbPath.empty())
                pcbPath = &tab.ecadProjectPcbPath;
            break;
        }
    if (!projectPath->empty()) {
        for (const auto& tab : st.docTabs) {
            if (tab.ecadProjectPath != *projectPath || tab.path.empty()) continue;
            if (tab.kind == AppState::DocTab::TabKind::Schematic && schPath->empty())
                schPath = &tab.path;
            if (tab.kind == AppState::DocTab::TabKind::PCB && pcbPath->empty())
                pcbPath = &tab.path;
        }
    }
    ui::kit::sectionHeader("Project files", nullptr, nullptr, -1, true);
    auto fileRow = [](const char* label, const std::string& path) {
        ui::kit::keyValueRow(label, path.empty() ? "Not available" : path.c_str(),
                             ui::kit::Tone::Neutral, !path.empty());
    };
    if (es.path.empty()) ui::kit::dimText("This document has not been saved to a KiCad file.");
    fileRow("Project", *projectPath);
    fileRow("Schematic", *schPath);
    fileRow("PCB", *pcbPath);
    if (es.kicadLocked) ui::kit::inlineStatus("File open in KiCad", ui::kit::Tone::Warn);

    ui::kit::sectionHeader("Open in KiCad");
    drawCommand(st, context, "ecad_kicad_open_sch", "Schematic Editor", true);
    drawCommand(st, context, "ecad_kicad_open_pcb", "PCB Editor");
    drawCommand(st, context, "ecad_kicad_open_project", "KiCad Project Manager");
    drawCommand(st, context, "ecad_open", "Open project or file");
    drawCommand(st, context, "ecad_save", "Save KiCad file");
    if (ui::kit::secondaryButton("Fit preview", true, nullptr, ImVec2(-1.0f, 0.0f)))
        es.canvas.requestFit();

    ui::kit::sectionHeader("Checks and outputs");
    drawCommand(st, context, "ecad_run_erc", "Run ERC");
    drawCommand(st, context, "ecad_run_drc", "Run DRC");
    drawCommand(st, context, "ecad_export_fab", "Export fabrication files");
    drawCommand(st, context, "ecad_export_step", "Export STEP");
    drawCommand(st, context, "ecad_export_bom", "Export BOM");
    drawCommand(st, context, "ecad_to_3d", "Board to 3D");

    ui::kit::sectionHeader("Board tools");
    drawCommand(st, context, "pcb_update_from_sch", "Update PCB from schematic");
    drawCommand(st, context, "ecad_autoroute", "Autoroute");
    drawCommand(st, context, "ecad_fill_zones", "Fill zones");

    ui::kit::sectionHeader("Circuit simulation", "sch_simulate");
    if (es.mirror.isSch()) {
        static const char* analyses[] = {"Transient", "AC"};
        ui::kit::fieldEnum("##ecadAnalysis", "Analysis", &st.ecadSimAnalysis, analyses, 2,
                           ui::kit::FieldOpts().withEnumStyle(ui::kit::EnumStyle::Combo));
    }
    drawCommand(st, context, "ecad_simulate", "Simulate");
    if (es.mirror.isSch() && st.ecadSimResults.is_object() && !st.ecadSimResults.empty())
        ui::kit::dimText("Simulation results and plots are shown below the preview.");
}

void drawSelectionDetails(EcadEditorState& es) {
    ui::kit::sectionHeader("Selection", nullptr, nullptr, static_cast<int>(es.selected.size()), true);
    if (es.selected.empty()) {
        ui::kit::dimText("Select an item on the preview or in the lists below.");
        return;
    }
    for (size_t i = 0; i < es.selected.size(); ++i) {
        ui::kit::RowStyle style;
        style.selected = i + 1 == es.selected.size();
        ImGui::PushID(static_cast<int>(i));
        if (ui::kit::row("##selected", es.selected[i].c_str(), style)) {
            const std::string token = es.selected[i];
            es.selected.erase(es.selected.begin() + static_cast<std::ptrdiff_t>(i));
            es.selected.push_back(token);
        }
        ImGui::PopID();
    }
    std::string kind, id;
    if (!splitToken(primarySelection(es), kind, id)) return;
    ui::kit::keyValueRow("Kind", kind.c_str());
    ui::kit::keyValueRow("Id", id.c_str(), ui::kit::Tone::Neutral, true);
    EcadPt pos;
    const bool canFocus = selectedPos(es.mirror, primarySelection(es), pos);
    if (canFocus)
        ui::kit::keyValueRow("Position", pointText(pos).c_str());
    if (kind == "sym" || kind == "pin") {
        const std::string symbolId = kind == "pin" ? id.substr(0, id.rfind('.')) : id;
        for (const auto& symbol : es.mirror.symbols()) if (symbol.id == symbolId) {
            ui::kit::keyValueRow("Reference", symbol.ref.c_str());
            ui::kit::keyValueRow("Value", symbol.value.c_str());
            ui::kit::keyValueRow("Library", symbol.libid.c_str());
            ui::kit::keyValueRow("Footprint", symbol.footprint.c_str());
            ui::kit::keyValueRow("Datasheet", symbol.datasheet.c_str(), ui::kit::Tone::Neutral, true);
            if (kind == "pin") for (const auto& pin : symbol.pins)
                if (pin.number == id.substr(id.rfind('.') + 1)) {
                    ui::kit::keyValueRow("Pin name", pin.name.c_str());
                    ui::kit::keyValueRow("Net", pin.net.c_str());
                }
            break;
        }
    } else if (kind == "fp" || kind == "pad") {
        const std::string fpId = kind == "pad" ? id.substr(0, id.rfind('.')) : id;
        for (const auto& fp : es.mirror.footprints()) if (fp.id == fpId) {
            ui::kit::keyValueRow("Reference", fp.ref.c_str());
            ui::kit::keyValueRow("Value", fp.value.c_str());
            ui::kit::keyValueRow("Footprint", fp.fpid.c_str());
            ui::kit::keyValueRow("Side", fp.side.c_str());
            if (kind == "pad") for (const auto& pad : fp.pads)
                if (pad.number == id.substr(id.rfind('.') + 1))
                    ui::kit::keyValueRow("Net", pad.net.c_str());
            break;
        }
    }
    if (ui::kit::secondaryButton("Focus selection", canFocus,
                                 "This item has no position in the current document.",
                                 ImVec2(-1.0f, 0.0f)))
        es.canvas.focusOn(pos);
}

void drawInspectView(EcadEditorState& es) {
    EcadMirror& m = es.mirror;
    drawSelectionDetails(es);
    ui::kit::sectionHeader("Selection filters", "filter_funnel");
    if (m.isPcb()) {
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
    ui::kit::sectionHeader("Inspect design");
    ImGui::SetNextItemWidth(-1.0f);
    ImGui::InputTextWithHint("##ecadInspectSearch", "Search components, nets or layers…",
                             es.managerSearch, sizeof(es.managerSearch));
    if (m.isSch()) {
        ui::kit::sectionHeader("Sheets", nullptr, nullptr, static_cast<int>(m.sheets().size()));
        for (const auto& sheet : m.sheets()) {
            if (!containsFolded(sheet.name, es.managerSearch)) continue;
            ui::kit::RowStyle style;
            style.selected = m.activeSheet() == sheet.id;
            ImGui::PushID(sheet.id.c_str());
            if (ui::kit::row("##sheet", sheet.name.c_str(), style)) {
                m.setActiveSheet(sheet.id);
                es.selected.clear();
                es.canvas.requestFit();
            }
            ImGui::PopID();
        }
    } else {
        ui::kit::sectionHeader("Layers", nullptr, nullptr, static_cast<int>(m.layers().size()));
        for (const auto& layer : m.layers()) {
            if (!containsFolded(layer.id, es.managerSearch)) continue;
            ImGui::PushID(layer.id.c_str());
            bool visible = m.layerVisible(layer.id);
            if (ImGui::Checkbox("##layerVisible", &visible)) m.setLayerVisible(layer.id, visible);
            ui::kit::itemTooltip(visible ? "Hide layer in preview" : "Show layer in preview");
            ImGui::SameLine(0.0f, ui::kit::rowGap());
            ImGui::TextUnformatted(layer.id.c_str());
            ImGui::PopID();
        }
    }
    ui::kit::sectionHeader("Nets", nullptr, nullptr, static_cast<int>(m.nets().size()));
    for (const auto& net : m.nets()) {
        if (!containsFolded(net.name, es.managerSearch)) continue;
        ImGui::PushID(net.name.c_str());
        bool visible = m.netVisible(net.name);
        if (ImGui::Checkbox("##netVisible", &visible)) m.setNetVisible(net.name, visible);
        ui::kit::itemTooltip(visible ? "Hide net in preview" : "Show net in preview");
        ImGui::SameLine(0.0f, ui::kit::rowGap());
        if (ImGui::Selectable(net.name.c_str(), es.highlightedNet == net.name)) {
            if (es.highlightedNet == net.name) es.highlightedNet.clear();
            else es.highlightedNet = net.name;
            es.canvas.setHighlightedNet(es.highlightedNet);
        }
        ImGui::PopID();
    }
    ui::kit::sectionHeader(m.isPcb() ? "Footprints" : "Components", nullptr, nullptr,
                           static_cast<int>(m.isPcb() ? m.footprints().size() : m.symbols().size()));
    if (m.isPcb()) {
        for (const auto& fp : m.footprints()) {
            if (!containsFolded(fp.ref, es.managerSearch) &&
                !containsFolded(fp.value, es.managerSearch) &&
                !containsFolded(fp.fpid, es.managerSearch)) continue;
            ui::kit::RowStyle style;
            style.secondary = fp.value.empty() ? fp.fpid.c_str() : fp.value.c_str();
            style.tooltip = fp.fpid.c_str();
            style.selected = hasSelection(es, "fp", fp.id);
            ImGui::PushID(fp.id.c_str());
            if (ui::kit::row("##footprint", fp.ref.empty() ? fp.fpid.c_str() : fp.ref.c_str(), style)) {
                applyPick(es, "fp:" + fp.id, ImGui::GetIO().KeyCtrl, ImGui::GetIO().KeyShift);
                focusSelected(es);
            }
            ImGui::PopID();
        }
    } else {
        for (const auto& symbol : m.symbols()) {
            if (!containsFolded(symbol.ref, es.managerSearch) &&
                !containsFolded(symbol.value, es.managerSearch) &&
                !containsFolded(symbol.libid, es.managerSearch)) continue;
            ui::kit::RowStyle style;
            style.secondary = symbol.value.empty() ? symbol.libid.c_str() : symbol.value.c_str();
            style.tooltip = symbol.libid.c_str();
            style.selected = hasSelection(es, "sym", symbol.id);
            ImGui::PushID(symbol.id.c_str());
            if (ui::kit::row("##symbol", symbol.ref.empty() ? symbol.libid.c_str() : symbol.ref.c_str(), style)) {
                m.setActiveSheet(symbol.sheet);
                applyPick(es, "sym:" + symbol.id, ImGui::GetIO().KeyCtrl, ImGui::GetIO().KeyShift);
                focusSelected(es);
            }
            ImGui::PopID();
        }
    }
}

void drawChecksView(AppState& st, EcadEditorState& es, const CommandContext& context) {
    const bool pcb = es.mirror.isPcb();
    ui::kit::sectionHeader(pcb ? "KiCad DRC" : "KiCad ERC", nullptr, nullptr,
                           es.hasCheck ? static_cast<int>(es.checks.size()) : -1, true);
    drawCommand(st, context, pcb ? "ecad_run_drc" : "ecad_run_erc",
                pcb ? "Run DRC" : "Run ERC", true);
    if (!es.hasCheck) {
        ui::kit::dimText("Run the KiCad check to see violations.");
        return;
    }
    const std::string errors = std::to_string(es.checkErrors);
    const std::string warnings = std::to_string(es.checkWarnings);
    ui::kit::valueChip("Errors", errors.c_str(), es.checkErrors ? ui::kit::Tone::Err : ui::kit::Tone::Ok);
    ImGui::SameLine(0.0f, ui::kit::rowGap());
    ui::kit::valueChip("Warnings", warnings.c_str(), es.checkWarnings ? ui::kit::Tone::Warn : ui::kit::Tone::Ok);
    if (!es.checkVersion.empty()) ui::kit::keyValueRow("KiCad version", es.checkVersion.c_str());
    if (es.checkRevision != es.mirror.revision() || es.fileChanged)
        ui::kit::inlineStatus("The document changed since this check; run it again.", ui::kit::Tone::Warn);
    ImGui::SetNextItemWidth(-1.0f);
    ImGui::InputTextWithHint("##ecadCheckSearch", "Search checks…",
                             es.checksSearch, sizeof(es.checksSearch));
    static const char* severities[] = {"All", "Errors", "Warnings"};
    ui::kit::segmented("##ecadCheckSeverity", severities, 3, &es.checksSeverity,
                       ImGui::GetContentRegionAvail().x);
    int shown = 0;
    for (size_t i = 0; i < es.checks.size(); ++i) {
        const auto& check = es.checks[i];
        if (es.checksSeverity == 1 && check.severity != "error") continue;
        if (es.checksSeverity == 2 && check.severity != "warning") continue;
        if (!containsFolded(check.rule, es.checksSearch) &&
            !containsFolded(check.message, es.checksSearch)) continue;
        ui::kit::RowStyle style;
        style.icon = check.severity == "error" ? "error_badge" : "warning_badge";
        style.iconTone = check.severity == "error" ? ui::kit::Tone::Err : ui::kit::Tone::Warn;
        style.secondary = check.message.c_str();
        style.meta = check.severity.c_str();
        style.tooltip = check.hasLoc ? "Focus this violation in the preview" : "No location reported";
        ImGui::PushID(static_cast<int>(i));
        if (ui::kit::row("##check", check.rule.c_str(), style) && check.hasLoc)
            es.canvas.focusOn(check.loc, check.severity == "error");
        ImGui::PopID();
        ++shown;
    }
    if (!shown)
        ui::kit::dimText(es.checks.empty() ? "No violations reported by KiCad."
                                            : "No violations match these filters.");
}

void drawEcadManagerBodyImpl(AppState& st, const ui::AdaptiveSurface& surf, ImU32 halo) {
    (void)halo;
    EcadEditorState& es = state();
    const CommandContext context = commandContext(st);
    ui::kit::pushDensity(ui::kit::Density::Compact);
    struct DensityPop { ~DensityPop() { ui::kit::popDensity(); } } densityPop;
    drawKicadStatus();
    if (!es.mirror.hasDoc()) {
        ui::kit::emptyState("ecad_pcb", "No KiCad project open",
                            "Create or open a project from the launchpad.");
        return;
    }
    drawConflict(st, es, context);
    static const char* views[] = {"Project", "Inspect", "Checks"};
    ui::kit::segmented("##ecadManagerView", views, 3, &es.managerView,
                       ImGui::GetContentRegionAvail().x);
    const float bodyTopY = ImGui::GetCursorScreenPos().y;
    float bodyH = ImGui::GetContentRegionAvail().y;
    if (es.mirror.isSch()) {
        if (es.bottomStripTopScreenY > bodyTopY)
            bodyH = std::min(bodyH, es.bottomStripTopScreenY - bodyTopY -
                                     ui::metrics::cpMenuRowGap());
        else bodyH -= ecadBottomStripHeight(st, es.mirror);
    }
    bodyH = std::max(ui::metrics::cpMenuRowGap(), bodyH);
    const float laneW = ui::metrics::px(ui::kChatScrollbarLane);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(laneW, 0.0f));
    ImGui::BeginChild("##ecadManagerScroll", ImVec2(0.0f, bodyH),
                      ImGuiChildFlags_AlwaysUseWindowPadding, ImGuiWindowFlags_NoScrollbar);
    ImGui::PopStyleVar();
    ImGui::Indent(-laneW);
    if (es.managerView == 0) drawProjectView(st, es, context);
    else if (es.managerView == 1) drawInspectView(es);
    else drawChecksView(st, es, context);
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
void drawResultsStrip(EcadEditorState& es) {
    const ui::Palette& pal = ui::palette;
    const auto& list = es.checks;
    const char* kindLbl = es.mirror.isSch() ? "ERC" : "DRC";
    if (!es.hasCheck) {
        ImGui::TextColored(v4(pal.textDim), "Run KiCad %s to see violations.", kindLbl);
        return;
    }
    const int errs = es.checkErrors, warns = es.checkWarnings;
    ImGui::TextColored(errs ? v4(pal.statusErr) : v4(pal.statusOk),
                       "KiCad %s: %d error%s", kindLbl, errs, errs == 1 ? "" : "s");
    ImGui::SameLine();
    ImGui::TextColored(warns ? v4(pal.statusWarn) : v4(pal.textDim),
                       "  %d warning%s", warns, warns == 1 ? "" : "s");
    if (es.checkRevision != es.mirror.revision() || es.fileChanged) {
        ImGui::SameLine();
        ImGui::TextColored(v4(pal.statusWarn), "  (out of date)");
    }
    ImGui::Separator();

    ImGui::BeginChild("##ecadResults", ImVec2(0, 0), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar);
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
        ImGui::PushID(static_cast<int>(i));
        const bool clicked = ImGui::Selectable(label);
        ImGui::PopID();
        ImGui::PopStyleColor();
        if (v.hasLoc && ImGui::IsItemHovered())
            ImGui::SetTooltip("Click to zoom to (%.2f, %.2f) mm", v.loc.x, v.loc.y);
        if (clicked && v.hasLoc) es.canvas.focusOn(v.loc, v.severity == "error");
    }
    if (list.empty())
        ImGui::TextColored(v4(pal.textDim), "No %s violations.", kindLbl);
    ui::drawWindowPillScrollbarV(ui::adaptiveSurface(-1.0f));
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
    ClearSpan span{left, left + std::max(0.0f, ImGui::GetContentRegionAvail().x)};
    const float managerEdge = ui::overlayColumnEdge(0);
    const float copilotEdge = ui::overlayColumnEdge(1);
    if (managerEdge >= 0.0f) span.x0 = std::clamp(managerEdge, left, span.x1);
    if (copilotEdge >= 0.0f) span.x1 = std::clamp(copilotEdge, left, span.x1);
    if (span.x1 < span.x0) span.x1 = span.x0;
    return span;
}

// Welcome surface shown whenever no ECAD document is active. Actions use the same app hooks as the
// ribbon; nothing is created until the user picks one.
void drawEcadLaunchpad(AppState& st) {
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
    const CommandContext context = commandContext(st);
    if (ui::kit::cardBegin("ecad_start", "KiCad Project", "ecad_pcb")) {
        ui::kit::dimText("Create or open a KiCad project. Edit in KiCad and preview it here.");
        drawKicadStatus();
        drawCommand(st, context, "ecad_new_project", "New KiCad project", true);
        drawCommand(st, context, "ecad_open", "Open project or file");
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
        const auto ai = commandAvailability("ecad_ai_design", context);
        const bool aiEnabled = haveRequest && ai.visible && ai.enabled;
        const char* aiReason = !haveRequest ? "Describe a circuit first."
            : ai.reason ? ai.reason : "AI design is unavailable in this context.";

        // Two equal buttons on one row while both labels fit; stacked full-width otherwise.
        const ImGuiStyle& style = ImGui::GetStyle();
        const float rowW = ImGui::GetContentRegionAvail().x;
        const float halfW = (rowW - style.ItemSpacing.x) * 0.5f;
        const bool oneRow =
            halfW >= ImGui::CalcTextSize("Design with AI").x + style.FramePadding.x * 4.0f;
        const float btnW = oneRow ? halfW : rowW;
        if (ui::kit::primaryButton("Design with AI", aiEnabled, aiReason,
                                   ImVec2(btnW, 0.0f))) {
            st.ecadAiRequest = aibuf;
            st.ecadAiGo = true;
        }
        if (aiEnabled) ui::kit::itemTooltip("Generate a schematic from the description.");
        if (oneRow) ImGui::SameLine(0.0f, style.ItemSpacing.x);
        if (ui::kit::secondaryButton("AI Board", aiEnabled, aiReason, ImVec2(btnW, 0.0f))) {
            // One click all the way to a routed PCB: design the schematic, then populate +
            // outline + autoroute a board from it (ecadAiBoard in the app loop).
            st.ecadAiRequest = aibuf;
            st.ecadAiBoardGo = true;
        }
        if (aiEnabled)
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
void ecadSetKicadStatus(const nlohmann::json& status) {
    g_kicadStatus = status;
}

void ecadSetCheckResult(const std::string& docId, const nlohmann::json& result) {
    EcadEditorState* es = stateForDoc(docId);
    if (!es || !result.is_object()) return;
    es->checks.clear();
    es->checkErrors = 0;
    es->checkWarnings = 0;
    es->checkVersion = result.value("kicad_version", std::string());
    es->checkRevision = result.value("_source_revision", es->mirror.revision());
    for (const char* category : {"violations", "unconnected", "parity"}) {
        auto entries = result.find(category);
        if (entries == result.end() || !entries->is_array()) continue;
        for (const auto& entry : *entries) {
            if (!entry.is_object()) continue;
            EcadViolation violation;
            violation.rule = entry.value("type", std::string(category));
            violation.severity = entry.value("severity", std::string("warning"));
            violation.message = entry.value("description", std::string());
            auto items = entry.find("items");
            if (items != entry.end() && items->is_array()) {
                for (const auto& item : *items) {
                    if (!item.is_object()) continue;
                    const std::string description = item.value("description", std::string());
                    if (!description.empty() && description != violation.message) {
                        if (!violation.message.empty()) violation.message += " | ";
                        violation.message += description;
                    }
                    auto xy = item.find("xy");
                    if (xy == item.end() || !xy->is_array() || xy->size() < 2 ||
                        !(*xy)[0].is_number() || !(*xy)[1].is_number()) continue;
                    const EcadPt pt{(*xy)[0].get<float>(), (*xy)[1].get<float>()};
                    if (!violation.hasLoc) {
                        violation.loc = pt;
                        violation.hasLoc = true;
                    }
                    violation.locs.push_back(pt);
                }
            }
            if (violation.severity == "error") ++es->checkErrors;
            else if (violation.severity == "warning") ++es->checkWarnings;
            es->checks.push_back(std::move(violation));
        }
    }
    auto counts = result.find("counts");
    if (counts != result.end() && counts->is_object()) {
        if (counts->contains("error") && (*counts)["error"].is_number_integer())
            es->checkErrors = (*counts)["error"].get<int>();
        if (counts->contains("warning") && (*counts)["warning"].is_number_integer())
            es->checkWarnings = (*counts)["warning"].get<int>();
    }
    es->hasCheck = true;
}

void ecadSetFileState(const std::string& docId, const std::string& path,
                      bool dirty, bool fileChanged, bool kicadLocked) {
    EcadEditorState* es = stateForDoc(docId);
    if (!es) return;
    es->path = path;
    es->dirty = dirty;
    es->fileChanged = fileChanged;
    es->kicadLocked = kicadLocked;
}

void ecadEditorShowChecks() { state().managerView = 2; }

void ecadEditorSelect(const std::string& sel) {
    EcadEditorState& es = state();
    es.selected.clear();
    if (!sel.empty()) es.selected.push_back(sel);
}
void ecadEditorHighlightNet(const std::string& net) {
    EcadEditorState& es = state();
    es.highlightedNet = net;
    es.canvas.setHighlightedNet(net);
}

bool ecadEditorFocusFirstViolation() {
    EcadEditorState& es = state();
    for (const auto& v : es.checks)
        if (v.hasLoc) { es.canvas.focusOn(v.loc, v.severity == "error"); return true; }
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

const char* ecadManagerTitle() { return "KiCad Project"; }

// See ecad_panel.h. Call once at the top of the app frame — cheap (one pointer store) — so an ECAD
// RPC adopt that fires before either draw function has run this session still resolves state() to
// the real per-tab EcadEditorState instead of the process-wide fallback.
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

    const int errs = es.checkErrors, warns = es.checkWarnings;
    const int unrouted = m.unroutedCount();
    char buf[128];
    if (es.hasCheck)
        std::snprintf(buf, sizeof(buf), "KiCad DRC  %d err   %d warn    |    Unrouted %d",
                      errs, warns, unrouted);
    else
        std::snprintf(buf, sizeof(buf), "KiCad DRC not run    |    Unrouted %d", unrouted);

    ImGuiViewport* vp = ImGui::GetMainViewport();
    ImDrawList* dl = ImGui::GetForegroundDrawList(vp);
    const ImVec2 tsz = ImGui::CalcTextSize(buf);
    const float padX = 12.0f * sc, padY = 5.0f * sc, rightMargin = 24.0f * sc;
    const float cy = bandTopY + bandH * 0.5f;
    ImVec2 p1(vp->Pos.x + vp->Size.x - rightMargin, cy + tsz.y * 0.5f + padY);
    ImVec2 p0(p1.x - tsz.x - 2.0f * padX, cy - tsz.y * 0.5f - padY);
    ui::glassRowBackground(dl, p0, p1, 6.0f * sc, false, false, 0.24f);
    const bool stale = es.hasCheck && (es.checkRevision != m.revision() || es.fileChanged);
    const ImU32 col = (es.hasCheck && errs > 0) ? ui::Palette::toU32(pal.statusErr)
                    : (!es.hasCheck || stale || warns > 0 || unrouted > 0)
                        ? ui::Palette::toU32(pal.statusWarn)
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

    if (!es.mirror.hasDoc()) {
        drawEcadLaunchpad(st);
        ImGui::End();
        return;
    }

    // PCB reclaims the WHOLE height — the DRC/unrouted readout moved to the ribbon band's dead space
    // (drawEcadRibbonStatus, called from app.cpp), so the board canvas now reaches the bottom of the
    // screen exactly like the Model tab's 3D viewport. The schematic keeps a bottom
    // strip: it hosts the ERC list AND the simulation waveform plot, which have nowhere else to live.
    const float bottomH = ecadBottomStripHeight(st, es.mirror);
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    const ClearSpan clearSpan = ecadClearSpan();
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
        const float canvasX = clearSpan.x0;
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
    es.canvas.setSelection(es.selected);
    es.canvas.setShowSheetFrame(es.mirror.isSch());
    EcadCanvasIO io = es.canvas.draw(es.mirror, es.checks, canvasAvail);
    if (io.leftClicked && io.hasWorldMouse) {
        const float tolerance = 9.0f / std::max(0.01f, es.canvas.zoom());
        applyPick(es, resolvePick(st, es, io.worldMouse, tolerance),
                  io.ctrl || ImGui::GetIO().KeySuper, io.shift);
        es.managerView = 1;
    }
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
    if (bottomH > 0.0f && clearSpan.x1 > clearSpan.x0) {
        const float stripY = ImGui::GetCursorScreenPos().y;
        ImGui::SetCursorScreenPos(ImVec2(clearSpan.x0, stripY));
        ImGui::BeginChild("##ecadStrip", ImVec2(clearSpan.x1 - clearSpan.x0, 0),
                          ImGuiChildFlags_Border);
        const bool haveSim = es.mirror.isSch() && st.ecadSimResults.is_object() &&
                             !st.ecadSimResults.empty();
        if (haveSim && ImGui::BeginTabBar("##ecadBottomTabs")) {
            if (ImGui::BeginTabItem(es.mirror.isSch() ? "ERC" : "DRC")) {
                drawResultsStrip(es);
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
            drawResultsStrip(es);
        }
        ImGui::EndChild();
    }

    ImGui::End();
}

}  // namespace ui
