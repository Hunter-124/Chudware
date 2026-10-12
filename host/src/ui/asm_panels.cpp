// Assembly UI panels (C20b) implementation — see asm_panels.h.
#include "asm_panels.h"

#include <algorithm>
#include <cctype>
#include <cfloat>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <system_error>
#include <utility>


#include <map>
#include <string>

#include "imgui.h"

#include "icons.h"
#include "layout.h"           // setNextCommandDialogPlacement (floating dialog anchor)
#include "metrics.h"
#include "widgets/kit.h"
#include "property_manager.h"   // pmParseDim / pmFormatDim (DimField parsing)

#include "../core/app_state.h"
#include "../core/doc_mirror.h"
#include "file_dialog.h"
#include "toast.h"
#include "../ui_theme.h"

namespace ui {

// Liquid-glass v2 for the floating assembly tool windows (Insert / Mate / Pattern /
// Explode / Interference / BOM). These are independent top-level ImGui::Begin() windows (NOT embedded
// in a pinned column), so each paints its OWN frosted backdrop — the layout.cpp drawLeftPane recipe:
//   pushGlassWindow()  -> WindowBg transparent (so the native fill doesn't sit over our glassPanel)
//   Begin(...)
//   popGlassWindow()
//   if (win) { paintGlassWindow(id) -> adaptivePanel(rect) + glassPanel + push surf.textPrimary; }
//   ... content ...
//   if (win) endGlassWindow();       // pop the content text color
//   End();
// The scrim tracks the LIVE viewport behind the window, so text contrast holds in dark + light theme.
static void pushGlassWindow() {
    ImGui::PushStyleColor(ImGuiCol_WindowBg, ImVec4(0, 0, 0, 0));  // v2: transparent — glassPanel fills
}
static void popGlassWindow() { ImGui::PopStyleColor(); }

// Paint the frosted backdrop over the just-Begun window and push its adaptive content text color.
// `id` MUST be unique + stable per window (independent rects; a shared id thrashes the adaptivePanel
// cache). Call once right after a successful Begin(); pair with endGlassWindow() before End(). Mirrors
// evaluate_dialogs.cpp's proven floating-window recipe (glassPanel straight onto the window draw list;
// the frost is translucent so the native title-bar text bleeds through legibly).
// The active window's adaptive surface (set by paintGlassWindow while a live viewport backdrop is
// under the window; cleared by endGlassWindow). pushText() consults it so the file's many
// pushText(pal.text)/pushText(pal.textDim) call sites become backdrop-adaptive with ZERO edits:
// over the frosted light glass, theme near-white ink would vanish, so we remap the primary/secondary
// text ROLES to the surface's contrast-matched tones. Semantic roles (accent/status*) pass through
// unchanged — they must keep their meaning.
static const ui::AdaptiveSurface* g_asmSurf = nullptr;

static void paintGlassWindow(const char* id) {
    ImVec2 wp = ImGui::GetWindowPos(), ws = ImGui::GetWindowSize();
    ImRect r(wp, ImVec2(wp.x + ws.x, wp.y + ws.y));
    const ui::AdaptiveSurface& surf =
        ui::adaptivePanel(ui::panelId(id), r, ui::GlassGroup::Global);
    ui::glassPanel(ImGui::GetWindowDrawList(), r, ui::GlassLevel::Regular, 12.0f, surf);
    g_asmSurf = ui::viewportBackdrop().valid() ? &surf : nullptr;
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);  // adaptive content ink (default)
}
static void endGlassWindow() { ImGui::PopStyleColor(); g_asmSurf = nullptr; }

// ---- small theme helpers (mirror feature_tree.cpp / property_manager.cpp) ----

static void pushText(const float c[4]) {
    // Remap the primary / secondary text ROLES to adaptive tones when a frosted surface is active
    // (pointer-identity compare against the palette role arrays — cheap + exact). All other colors
    // (accent, statusWarn/statusErr, custom) pass through so semantic meaning is preserved.
    if (g_asmSurf) {
        if (c == ui::palette.text)    { ImGui::PushStyleColor(ImGuiCol_Text, g_asmSurf->textPrimary);   return; }
        if (c == ui::palette.textDim) { ImGui::PushStyleColor(ImGuiCol_Text, g_asmSurf->textSecondary); return; }
    }
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(c[0], c[1], c[2], c[3]));
}
static void asmIcon(const char* name, const char* fallback, float size = 18.0f) {
    IconUV iv = getIcon(name, 20);
    if (iv.valid) ImGui::Image(iv.texId, ImVec2(size, size), iv.uv0, iv.uv1);
    else ImGui::TextUnformatted(fallback);
}

// Classify a resolved selector's surface for the mate auto-suggest.
static MateSurfKind classifySurf(const nlohmann::json& sel) {
    if (!sel.is_object()) return MateSurfKind::Other;
    std::string kind = sel.value("kind", std::string());
    std::string surf = sel.value("surf", std::string());
    if (kind == "face") {
        if (surf == "plane") return MateSurfKind::Plane;
        if (surf == "cylinder" || surf == "cone") return MateSurfKind::Cylinder;
        return MateSurfKind::Other;
    }
    if (kind == "edge") {
        if (surf == "circle") return MateSurfKind::Cylinder;  // axis-bearing
        return MateSurfKind::Other;
    }
    return MateSurfKind::Other;
}

// Map a component k-id to its display name via the doc tree.
static std::string componentName(AppState& st, const std::string& cid) {
    const auto& tree = st.docMirror.tree();
    if (tree.is_object() && tree.contains("components") && tree["components"].is_array())
        for (const auto& c : tree["components"])
            if (c.is_object() && c.value("id", std::string()) == cid)
                return c.value("name", cid);
    return cid;
}

// ===========================================================================
// command dispatch
// ===========================================================================

bool AsmPanels::handleCommand(AppState& st, const std::string& cmdId) {
    if (cmdId == "new_assembly") {
        std::string err;
        if (st.docMirror.newAssembly(st.worker, err)) {
            st.addLog("[asm] new assembly  rev=" + std::to_string(st.docMirror.revision()));
            insertTargetDocId_ = st.docMirror.docId();
        } else {
            st.addLog("[err] new assembly: " + err);
        }
        insertSel_ = -1; insertChosenPath_.clear(); insertNote_.clear();
        if (insertBrowseDir_.empty() && !st.workspaceDir.empty())
            insertBrowseDir_ = st.workspaceDir;
        insertOpen_ = true; insertFocus_ = true;
        openPmGuest(st, "insert");
        return true;
    }
    if (cmdId == "asm_insert") {
        insertSel_ = -1; insertChosenPath_.clear(); insertNote_.clear();
        if (insertBrowseDir_.empty() && !st.workspaceDir.empty())
            insertBrowseDir_ = st.workspaceDir;
        if (st.docMirror.hasDoc() && st.docMirror.tree().is_object() &&
            st.docMirror.tree().value("schema", std::string()) == "aicad.asm/1")
            insertTargetDocId_ = st.docMirror.docId();
        else if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
                 st.docTabs[st.activeTab].kind == AppState::DocTab::TabKind::Assembly)
            insertTargetDocId_ = st.docTabs[st.activeTab].docId;
        else
            insertTargetDocId_.clear();
        insertOpen_ = true; insertFocus_ = true;
        openPmGuest(st, "insert");
        return true;
    }
    if (cmdId == "asm_parts_library") {
        st.activeWorkspace = AppState::Workspace::PartsLibrary;
        st.toast(ui::ToastLevel::Info,
                 "Parts Library creates an editable part; save it, then use Insert Component.");
        st.addLog("[asm] Std. Parts → Parts Library workspace");
        return true;
    }
    if (cmdId == "asm_mate") {
        beginMateForm();
        openPmGuest(st, "mate");
        return true;
    }
    if (cmdId == "asm_explode") {
        explodeOpen_ = true; explodeFocus_ = true;
        openPmGuest(st, "explode");
        return true;
    }
    if (cmdId == "asm_interference") {
        interferenceOpen_ = true; interferenceFocus_ = true;
        refreshInterference(st);
        openPmGuest(st, "interference");
        return true;
    }
    if (cmdId == "asm_bom") { bomOpen_ = true; bomFocus_ = true; refreshBom(st); return true; }
    if (cmdId == "asm_move") {
        if (st.docMirror.hasDoc()) {
            nlohmann::json res; std::string err;
            if (st.worker.call("doc_solve_mates", {{"doc_id", st.docMirror.docId()}}, res, err)) {
                std::string aicm;
                if (res.contains("tess") && res["tess"].is_object())
                    aicm = res["tess"].value("aicm_path", std::string());
                if (!aicm.empty()) { std::string le; st.loadDocMesh(aicm, le); }
                st.addLog("[asm] move: re-solved mates — select a component and drag it in the "
                          "viewport to reposition.");
            } else st.addLog("[err] asm_move: " + err);
        }
        return true;
    }
    if (cmdId == "asm_pattern") {
        beginPatternForm();
        openPmGuest(st, "pattern");
        return true;
    }
    return false;
}

namespace { bool mirrorIsAssembly(AppState& st); }

void AsmPanels::draw(AppState& st) {
    // Feature-tree "Edit" on a component → open + focus the Mate form for it.
    if (!st.requestEditComponentMates.empty()) {
        const std::string cid = st.requestEditComponentMates;
        st.requestEditComponentMates.clear();
        beginMateForm();
        editComponentTarget_ = cid;
        openPmGuest(st, "mate");
        st.addLog("[asm] edit mates for component " + cid);
    }
    if (!pendingLibraryInsert_.targetDocId.empty()) {
        auto& pending = pendingLibraryInsert_;
        int targetTab = -1;
        for (int i = 0; i < static_cast<int>(st.docTabs.size()); ++i)
            if (st.docTabs[i].docId == pending.targetDocId &&
                st.docTabs[i].kind == AppState::DocTab::TabKind::Assembly) {
                targetTab = i;
                break;
            }
        if (targetTab < 0 || ImGui::GetTime() > pending.deadline) {
            auto done = std::move(pending.onComplete);
            pending = {};
            if (done) done(false, targetTab < 0 ? "Target assembly was closed"
                                               : "Timed out opening the target assembly");
        } else if (st.activeTab != targetTab ||
                   (!pending.activationRequested && st.docMirror.docId() != pending.targetDocId)) {
            pending.activationRequested = true;
            st.switchToTab(targetTab);  // async activation when the target is not already bound
        } else if (st.docMirror.docId() == pending.targetDocId && mirrorIsAssembly(st)) {
            auto ready = std::move(pending);
            pending = {};
            std::string aicm;
            if (!insertComponent(st, ready.path, nullptr, aicm,
                                 ready.onComplete, ready.targetDocId) && ready.onComplete)
                ready.onComplete(false, insertNote_);
        }
    }
    // Mate/Insert/Pattern/Explode/Interference draw via PropertyManager::drawBody guest callback.
    // BOM stays a floating glass window.
    drawBom(st);
}

void AsmPanels::insertComponentWhenReady(
    AppState& st, const std::string& path, const std::string& targetDocId,
    std::function<void(bool, const std::string&)> onComplete) {
    pendingLibraryInsert_ = {path, targetDocId, std::move(onComplete),
                             ImGui::GetTime() + 45.0, false};
    st.addLog("[asm] queued library component: " + path);
}

void AsmPanels::closeAllToolForms(AppState& st) {
    const bool ownedGuest = pmFormActive();
    mateOpen_ = false; editComponentTarget_.clear();
    for (auto& pick : picks_) pick = MatePick{};
    activeWell_ = 0;
    insertOpen_ = false; insertSel_ = -1; insertChosenPath_.clear(); insertNote_.clear();
    insertTargetDocId_.clear();
    patternOpen_ = false; patternComps_.clear();
    explodeOpen_ = false;
    interferenceOpen_ = false;
    if (ownedGuest && st.propertyManager && st.propertyManager->guestActive())
        st.propertyManager->closeGuest();
}

void AsmPanels::openPmGuest(AppState& st, const char* which) {
    if (!st.propertyManager || !which) return;
    // Mutual exclusion among assembly tools (keep BOM independent).
    const bool wantMate = std::strcmp(which, "mate") == 0;
    const bool wantInsert = std::strcmp(which, "insert") == 0;
    const bool wantPattern = std::strcmp(which, "pattern") == 0;
    const bool wantExplode = std::strcmp(which, "explode") == 0;
    const bool wantInterf = std::strcmp(which, "interference") == 0;
    if (!wantMate) { mateOpen_ = false; editComponentTarget_.clear(); }
    if (!wantInsert) { insertOpen_ = false; }
    if (!wantPattern) patternOpen_ = false;
    if (!wantExplode) explodeOpen_ = false;
    if (!wantInterf) interferenceOpen_ = false;

    const char* title = "Assembly";
    const char* icon = "mate";
    std::string interferenceTitle;
    if (wantMate)        { title = "Mate";                  icon = "mate"; }
    else if (wantInsert) { title = "Insert Component";      icon = "insert_component"; }
    else if (wantPattern){ title = "Component Pattern";     icon = "pattern_component"; }
    else if (wantExplode){ title = "Exploded View";         icon = "exploded_view"; }
    else if (wantInterf) {
        interferenceTitle = "Interference Detection — " + interferenceDocName_;
        title = interferenceTitle.c_str(); icon = "interference";
    }

    // Capture `this` + which tool; body draws only while the matching flag is still open.
    st.propertyManager->openGuest(
        title, icon,
        /*body=*/[this, which](AppState& s) {
            if (std::strcmp(which, "mate") == 0)              drawMateFormBody(s);
            else if (std::strcmp(which, "insert") == 0)       drawInsertBody(s);
            else if (std::strcmp(which, "pattern") == 0)      drawPatternFormBody(s);
            else if (std::strcmp(which, "explode") == 0)      drawExplodeBody(s);
            else if (std::strcmp(which, "interference") == 0) drawInterferenceBody(s);
        },
        /*onOk=*/[this, which](AppState& s) {
            // Mate / Pattern / Insert treat OK as "Add" when ready; Explode/Interference just close.
            if (std::strcmp(which, "mate") == 0) {
                // Attempt add; leave form open on success for chaining (SolidWorks-like).
                std::string type = effectiveMateType();
                const int wellCount = typeNeedsThirdRef(type) ? 3 : 2;
                bool refsReady = true;
                for (int i = 0; i < wellCount; ++i) if (!picks_[i].has) refsReady = false;
                if (!refsReady) {
                    mateError_ = "Pick the required references first.";
                    return;
                }
                nlohmann::json refs = nlohmann::json::array();
                for (int i = 0; i < wellCount; ++i)
                    refs.push_back({{"component", picks_[i].component},
                                    {"selector", picks_[i].selector}});
                std::string aicm;
                if (addMate(s, type, aligned_ ? "aligned" : "anti", refs, aicm)) {
                    s.addLog("[asm] mate added: " + type);
                    picks_[0] = MatePick{}; picks_[1] = MatePick{}; picks_[2] = MatePick{};
                    activeWell_ = 0; mateError_.clear();
                }
            } else if (std::strcmp(which, "insert") == 0) {
                if (insertChosenPath_.empty()) {
                    insertNote_ = "Select a part or Browse from disk…";
                    return;
                }
                std::string aicm;
                if (insertComponent(s, insertChosenPath_, nullptr, aicm)) {
                    {
                        size_t slash = insertChosenPath_.find_last_of("/\\");
                        insertNote_ = "Inserted " + (slash == std::string::npos
                            ? insertChosenPath_
                            : insertChosenPath_.substr(slash + 1));
                    }
                    s.addLog("[asm] inserted: " + insertChosenPath_);
                }
            } else if (std::strcmp(which, "pattern") == 0) {
                std::string aicm, err;
                if (addPattern(s, aicm, err)) {
                    s.addLog("[asm] component pattern added");
                    patternError_.clear();
                    patternComps_.clear();
                } else {
                    patternError_ = err;
                }
            } else {
                // Explode / Interference: OK = close
                if (std::strcmp(which, "explode") == 0) explodeOpen_ = false;
                if (std::strcmp(which, "interference") == 0) interferenceOpen_ = false;
                if (s.propertyManager) s.propertyManager->closeGuest();
            }
        },
        /*onCancel=*/[this, which](AppState& s) {
            if (std::strcmp(which, "mate") == 0) closeMateForm();
            else if (std::strcmp(which, "insert") == 0) insertOpen_ = false;
            else if (std::strcmp(which, "pattern") == 0) patternOpen_ = false;
            else if (std::strcmp(which, "explode") == 0) explodeOpen_ = false;
            else if (std::strcmp(which, "interference") == 0) interferenceOpen_ = false;
            if (s.propertyManager) s.propertyManager->closeGuest();
        });

    // Auto-front FeatureManager tab so the guest form is visible (pm->active() is true).
    st.leftPaneTab = AppState::LP_FeatureManager;
}

// ===========================================================================
// Insert Component
// ===========================================================================

namespace {
// True when the active DocMirror document is an assembly.
bool mirrorIsAssembly(AppState& st) {
    const auto& tree = st.docMirror.tree();
    return st.docMirror.hasDoc() && tree.is_object() &&
           tree.value("schema", std::string()) == "aicad.asm/1";
}

// Basename (no directory) for list rows / tooltips.
std::string pathBaseName(const std::string& p) {
    size_t slash = p.find_last_of("/\\");
    return (slash == std::string::npos) ? p : p.substr(slash + 1);
}

// Lower-cased extension including the leading dot (handles ".step+" via last-dot).
std::string insertLowerExt(const std::string& path) {
    size_t slash = path.find_last_of("/\\");
    std::string name = (slash == std::string::npos) ? path : path.substr(slash + 1);
    size_t dot = name.find_last_of('.');
    if (dot == std::string::npos || dot == 0) return {};
    std::string e = name.substr(dot);
    std::transform(e.begin(), e.end(), e.begin(),
                   [](unsigned char ch) { return (char)std::tolower(ch); });
    return e;
}

// Component source extensions for the Insert Workspace browser (mirrors kInsertFilter /
// file_explorer isCadExt plus .obj/.ply which Browse already accepts, the AutoCAD
// .dxf/.dwg lane and ACIS .sat/.sab, which the engine's component loader imports like any
// neutral format).
bool insertIsCadExt(const std::string& e) {
    return e == ".step+" || e == ".step" || e == ".stp" || e == ".iges" || e == ".igs" ||
           e == ".brep"  || e == ".stl"  || e == ".3mf" || e == ".obj"  || e == ".ply" ||
           e == ".dxf"   || e == ".dwg"  || e == ".sat" || e == ".sab";
}

// Non-recursive CAD files under dir. Errors → empty list (caller shows dim text).
std::vector<std::string> listInsertWorkspaceFiles(const std::string& dir) {
    std::vector<std::string> out;
    if (dir.empty()) return out;
    namespace fs = std::filesystem;
    std::error_code ec;
    fs::path root(dir);
    if (!fs::is_directory(root, ec) || ec) return out;
    for (fs::directory_iterator it(root, fs::directory_options::skip_permission_denied, ec), end;
         !ec && it != end; it.increment(ec)) {
        if (ec) break;
        std::error_code fec;
        if (!it->is_regular_file(fec) || fec) continue;
        const std::string full = it->path().string();
        if (!insertIsCadExt(insertLowerExt(full))) continue;
        out.push_back(full);
    }
    std::sort(out.begin(), out.end(), [](const std::string& a, const std::string& b) {
        const std::string an = pathBaseName(a), bn = pathBaseName(b);
        return std::lexicographical_compare(
            an.begin(), an.end(), bn.begin(), bn.end(),
            [](char c1, char c2) {
                return std::tolower((unsigned char)c1) < std::tolower((unsigned char)c2);
            });
    });
    return out;
}

// One selectable entry in the Insert Component chooser.
struct InsertCandidate {
    std::string path;    // absolute path (empty for unsaved open parts)
    std::string label;   // display title
    std::string detail;  // secondary line (Open / Recent / path)
    bool isOpen = false;
};

// Build open-part tabs + recent files (deduped). Open tabs first so the user can
// drop a part they already have loaded without re-browsing the disk.
std::vector<InsertCandidate> buildInsertCandidates(AppState& st) {
    std::vector<InsertCandidate> out;
    std::vector<std::string> seenPaths;
    auto already = [&](const std::string& p) {
        if (p.empty()) return false;
        for (const auto& s : seenPaths)
            if (s == p) return true;
        return false;
    };
    auto pushSeen = [&](const std::string& p) {
        if (!p.empty()) seenPaths.push_back(p);
    };

    // Open part tabs (skip the active assembly itself and non-part docs).
    for (int i = 0; i < (int)st.docTabs.size(); ++i) {
        const auto& t = st.docTabs[i];
        if (t.kind != AppState::DocTab::TabKind::Part) continue;
        if (already(t.path)) continue;
        InsertCandidate c;
        c.path = t.path;
        c.label = t.displayTitle();
        if (c.label.empty()) c.label = t.title.empty() ? std::string("Part") : t.title;
        c.detail = t.path.empty() ? std::string("Open · unsaved")
                                  : std::string("Open");
        c.isOpen = true;
        out.push_back(std::move(c));
        pushSeen(t.path);
    }

    // Recent files — parts only (skip assemblies / unknown extensions).
    for (const auto& rp : st.recentFiles) {
        if (rp.empty() || already(rp)) continue;
        std::string low = rp;
        std::transform(low.begin(), low.end(), low.begin(),
                       [](unsigned char ch) { return (char)std::tolower(ch); });
        // Skip assembly-looking recent entries; component sources are parts.
        if (low.size() >= 7 && low.compare(low.size() - 7, 7, ".sldasm") == 0) continue;
        InsertCandidate c;
        c.path = rp;
        c.label = pathBaseName(rp);
        c.detail = "Recent";
        c.isOpen = false;
        out.push_back(std::move(c));
        pushSeen(rp);
    }
    return out;
}

// Filter for Browse… (component source files the engine can load).
static const char* kInsertFilter =
    "All CAD & mesh files\0*.step+;*.step;*.stp;*.iges;*.igs;*.brep;*.sat;*.sab;*.dxf;*.dwg;*.stl;*.3mf;*.obj;*.ply\0"
    "Chudware part (*.step+)\0*.step+\0"
    "STEP (*.step;*.stp)\0*.step;*.stp\0"
    "ACIS (*.sat;*.sab)\0*.sat;*.sab\0"
    "AutoCAD (*.dwg;*.dxf)\0*.dwg;*.dxf\0"
    "All files (*.*)\0*.*\0";
}  // namespace

bool AsmPanels::insertComponent(
    AppState& st, const std::string& path, const nlohmann::json& transform,
    std::string& aicmOut, std::function<void(bool, const std::string&)> onComplete,
    const std::string& targetDocId) {
    aicmOut.clear();
    if (path.empty()) {
        insertNote_ = "No part selected.";
        return false;
    }

    // Resolve the intended assembly document. Prefer the explicit target captured when
    // Insert opened / New Assembly ran; fall back to the active assembly tab / live mirror.
    std::string targetDoc = targetDocId.empty() ? insertTargetDocId_ : targetDocId;
    if (targetDoc.empty() && st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
        st.docTabs[st.activeTab].kind == AppState::DocTab::TabKind::Assembly &&
        !st.docTabs[st.activeTab].docId.empty()) {
        targetDoc = st.docTabs[st.activeTab].docId;
    }
    if (targetDoc.empty() && mirrorIsAssembly(st))
        targetDoc = st.docMirror.docId();

    if (targetDoc.empty()) {
        insertNote_ = "No assembly open. Use File > New Assembly first.";
        st.addLog("[err] insert: no target assembly");
        return false;
    }

    // If the live mirror is pointing at a different doc (user switched tabs), re-activate the
    // intended assembly so the op lands on the right document — never a part or another asm.
    if (!st.docMirror.hasDoc() || st.docMirror.docId() != targetDoc) {
        // Prefer switchToTab so camera/session state stays coherent.
        int tabIdx = -1;
        for (int i = 0; i < (int)st.docTabs.size(); ++i) {
            if (st.docTabs[i].docId == targetDoc) { tabIdx = i; break; }
        }
        if (tabIdx >= 0) {
            st.switchToTab(tabIdx);
        } else {
            std::string aicm, err;
            if (!st.docMirror.activate(st.worker, targetDoc, aicm, err)) {
                insertNote_ = "Could not activate assembly: " + err;
                st.addLog("[err] insert activate: " + err);
                return false;
            }
            if (!aicm.empty()) { std::string le; st.loadDocMesh(aicm, le); }
        }
    }

    if (!mirrorIsAssembly(st) || st.docMirror.docId() != targetDoc) {
        insertNote_ = "Active document is not the target assembly.";
        st.addLog("[err] insert: active doc is not the target assembly (" +
                  st.docMirror.docId() + " vs " + targetDoc + ")");
        return false;
    }

    // Only the insert dialog owns its sticky target; external callers pin their own doc.
    if (targetDocId.empty()) insertTargetDocId_ = targetDoc;

    nlohmann::json op = {{"op", "asm_insert_component"}, {"source", {{"path", path}}}};
    if (!transform.is_null()) op["transform"] = transform;
    // T-PERF1: async so large component inserts don't freeze the GL thread. aicmOut is filled
    // only on the sync path for callers that need it immediately; the mesh reloads in the
    // drain callback either way. Capture targetDoc so a superseded completion can't load mesh
    // into a different document.
    aicmOut.clear();
    ++st.rebuildsInFlight;
    st.docMirror.applyOpsAsync(st.rpc, st.worker, nlohmann::json::array({op}),
        [&st, targetDoc, onComplete = std::move(onComplete)](
            const core::OpsResponse& r, bool applied) {
            --st.rebuildsInFlight;
            if (!r.ok) {
                const std::string error = r.error_message.empty() ? "component insert failed"
                                                                    : r.error_message;
                st.addLog("[err] insert: " + error);
                if (onComplete) onComplete(false, error);
                return;
            }
            if (!applied) {
                // The engine may have accepted the op while another tab superseded this mirror.
                st.addLog("[asm] insert completed on " + targetDoc +
                          " (active doc changed — mesh not reloaded here)");
                for (auto& tab : st.docTabs)
                    if (tab.docId == targetDoc) tab.meshCacheInvalidated = true;
                if (onComplete) onComplete(true, {});
                return;
            }
            if (st.docMirror.docId() == targetDoc && !r.aicm_path.empty())
                st.loadDocMeshAsync(r.aicm_path);  // T-PERF3
            if (onComplete) onComplete(true, {});
        });
    return true;
}

void AsmPanels::drawInsertBody(AppState& st) {
    if (!insertOpen_) return;
    const auto& pal = ui::palette;
    const float S = ui::uiScale;

    const bool haveAsm = mirrorIsAssembly(st) ||
        (!insertTargetDocId_.empty()) ||
        (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
         st.docTabs[st.activeTab].kind == AppState::DocTab::TabKind::Assembly);

    if (!haveAsm) {
        pushText(pal.statusWarn);
        ImGui::TextWrapped("No assembly open. Use File > New Assembly first.");
        ImGui::PopStyleColor();
        return;
    }

    // Lazy-seed Workspace browse dir if still empty (workspace may have resolved after open).
    if (insertBrowseDir_.empty() && !st.workspaceDir.empty())
        insertBrowseDir_ = st.workspaceDir;

    std::string targetLabel;
    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
        st.docTabs[st.activeTab].kind == AppState::DocTab::TabKind::Assembly)
        targetLabel = st.docTabs[st.activeTab].displayTitle();
    if (targetLabel.empty() && mirrorIsAssembly(st))
        targetLabel = st.docMirror.tree().value("name", std::string("Assembly"));
    if (targetLabel.empty()) targetLabel = "Assembly";
    pushText(pal.textDim);
    ImGui::Text("Target: %s", targetLabel.c_str());
    ImGui::PopStyleColor();
    ImGui::Spacing();

    // Browse from disk — prominent, in addition to open/recent + workspace lists.
    if (ImGui::Button("Browse from disk\xe2\x80\xa6", ImVec2(-1, 0))) {
        if (std::getenv("CHUDWARE_NO_FILE_DIALOGS")) {
            st.toast(ui::ToastLevel::Info,
                     "File dialogs disabled (CHUDWARE_NO_FILE_DIALOGS).");
        } else {
            std::string path = openFileDialog(kInsertFilter);
            if (!path.empty()) {
                insertChosenPath_ = path;
                insertSel_ = -1;
                insertNote_.clear();
            }
        }
    }
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Open a part file from disk (STEP+, STEP, mesh, \xe2\x80\xa6)");

    ImGui::Spacing();
    ImGui::SeparatorText("Open & recent parts");

    const auto candidates = buildInsertCandidates(st);
    // Paths already offered under Open/Recent — Workspace rows skip these (already() pattern).
    auto alreadyInOpenRecent = [&](const std::string& p) {
        if (p.empty()) return false;
        for (const auto& c : candidates)
            if (!c.path.empty() && c.path == p) return true;
        return false;
    };

    // Split remaining height between Open/Recent and Workspace lists.
    const float listH = std::max(80.0f * S, (ImGui::GetContentRegionAvail().y -
                                (ImGui::GetFrameHeightWithSpacing() * 8.0f)) * 0.45f);
    ImGui::BeginChild("##asm_insert_list", ImVec2(0, listH), true);
    if (candidates.empty()) {
        pushText(pal.textDim);
        ImGui::TextWrapped("No open or recent parts. Use Browse from disk\xe2\x80\xa6 or Workspace below.");
        ImGui::PopStyleColor();
    } else {
        for (int i = 0; i < (int)candidates.size(); ++i) {
            const auto& c = candidates[i];
            const bool selectable = !c.path.empty();
            if (!selectable) ImGui::BeginDisabled();
            std::string row = c.label;
            if (!c.detail.empty()) row += "  —  " + c.detail;
            const bool sel = (insertSel_ == i);
            if (ImGui::Selectable((row + "##ins" + std::to_string(i)).c_str(), sel,
                                  ImGuiSelectableFlags_AllowDoubleClick)) {
                insertSel_ = i;
                insertChosenPath_ = c.path;
                insertNote_.clear();
                if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left) && !c.path.empty()) {
                    std::string aicm;
                    if (insertComponent(st, c.path, nullptr, aicm)) {
                        insertNote_ = "Inserting " + pathBaseName(c.path) + "\xe2\x80\xa6";
                        st.addLog("[asm] inserted: " + c.path);
                    }
                }
            }
            if (!selectable) {
                ImGui::EndDisabled();
                if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                    ImGui::SetTooltip("Save this part first so it has a path to insert.");
            } else if (ImGui::IsItemHovered() && !c.path.empty()) {
                ImGui::SetTooltip("%s", c.path.c_str());
            }
        }
    }
    ImGui::EndChild();

    // ---- Workspace directory browser ---------------------------------------
    ImGui::Spacing();
    ImGui::SeparatorText("Workspace");

    if (insertBrowseDir_.empty()) {
        pushText(pal.textDim);
        ImGui::TextWrapped(
            "No workspace folder set. Set one in Settings \xe2\x86\x92 General, or use Browse from disk\xe2\x80\xa6.");
        ImGui::PopStyleColor();
    } else {
        // Dim path label + optional editable path + Parent folder.
        pushText(pal.textDim);
        ImGui::TextWrapped("%s", insertBrowseDir_.c_str());
        ImGui::PopStyleColor();

        static char s_wsPathBuf[1024] = {};
        static std::string s_wsPathSynced;
        if (s_wsPathSynced != insertBrowseDir_) {
            std::snprintf(s_wsPathBuf, sizeof(s_wsPathBuf), "%s", insertBrowseDir_.c_str());
            s_wsPathSynced = insertBrowseDir_;
        }
        ImGui::SetNextItemWidth(-1);
        if (ImGui::InputText("##asm_insert_ws_path", s_wsPathBuf, sizeof(s_wsPathBuf),
                             ImGuiInputTextFlags_EnterReturnsTrue)) {
            std::string typed = s_wsPathBuf;
            // Trim trailing whitespace.
            while (!typed.empty() && (typed.back() == ' ' || typed.back() == '\t'))
                typed.pop_back();
            if (!typed.empty()) {
                insertBrowseDir_ = typed;
                s_wsPathSynced = insertBrowseDir_;
            }
        }
        if (ImGui::IsItemDeactivatedAfterEdit()) {
            std::string typed = s_wsPathBuf;
            while (!typed.empty() && (typed.back() == ' ' || typed.back() == '\t'))
                typed.pop_back();
            if (!typed.empty()) {
                insertBrowseDir_ = typed;
                s_wsPathSynced = insertBrowseDir_;
            }
        }

        namespace fs = std::filesystem;
        std::error_code pec;
        fs::path cur(insertBrowseDir_);
        fs::path parent = cur.parent_path();
        const bool canParent = !parent.empty() && parent != cur &&
                               fs::is_directory(parent, pec);
        if (!canParent) ImGui::BeginDisabled();
        if (ImGui::Button("Parent folder", ImVec2(-1, 0)) && canParent) {
            insertBrowseDir_ = parent.string();
            s_wsPathSynced.clear();  // force path buffer resync next frame
        }
        if (!canParent) ImGui::EndDisabled();

        const auto wsFiles = listInsertWorkspaceFiles(insertBrowseDir_);
        std::vector<std::string> shown;
        shown.reserve(wsFiles.size());
        for (const auto& p : wsFiles) {
            if (alreadyInOpenRecent(p)) continue;
            shown.push_back(p);
        }

        const float wsH = std::max(80.0f * S, ImGui::GetContentRegionAvail().y -
                                    (ImGui::GetFrameHeightWithSpacing() * 4.0f));
        ImGui::BeginChild("##asm_insert_ws_list", ImVec2(0, wsH), true);
        if (shown.empty()) {
            pushText(pal.textDim);
            if (wsFiles.empty())
                ImGui::TextWrapped("No CAD parts in this folder (or folder unreadable).");
            else
                ImGui::TextWrapped("All CAD parts here are already listed under Open & recent.");
            ImGui::PopStyleColor();
        } else {
            for (int i = 0; i < (int)shown.size(); ++i) {
                const std::string& p = shown[i];
                const std::string label = pathBaseName(p);
                const bool sel = (!insertChosenPath_.empty() && insertChosenPath_ == p);
                if (ImGui::Selectable((label + "##wsi" + std::to_string(i)).c_str(), sel,
                                      ImGuiSelectableFlags_AllowDoubleClick)) {
                    insertSel_ = -1;
                    insertChosenPath_ = p;
                    insertNote_.clear();
                    if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) {
                        std::string aicm;
                        if (insertComponent(st, p, nullptr, aicm)) {
                            insertNote_ = "Inserting " + label + "\xe2\x80\xa6";
                            st.addLog("[asm] inserted: " + p);
                        }
                    }
                }
                if (ImGui::IsItemHovered())
                    ImGui::SetTooltip("%s", p.c_str());
            }
        }
        ImGui::EndChild();
    }

    ImGui::Spacing();
    if (!insertChosenPath_.empty()) {
        pushText(pal.textDim);
        ImGui::Text("Selected: %s", pathBaseName(insertChosenPath_).c_str());
        ImGui::PopStyleColor();
    } else {
        pushText(pal.textDim);
        ImGui::TextUnformatted("Select a part above, or Browse from disk\xe2\x80\xa6");
        ImGui::PopStyleColor();
    }

    const bool hasComps =
        st.docMirror.tree().is_object() &&
        st.docMirror.tree().contains("components") &&
        st.docMirror.tree()["components"].is_array() &&
        !st.docMirror.tree()["components"].empty();
    pushText(pal.textDim);
    ImGui::TextUnformatted(hasComps
        ? "Inserted at the origin (drag/mate to position)."
        : "The first component is fixed in place automatically.");
    ImGui::PopStyleColor();

    if (!insertNote_.empty()) {
        ImGui::Spacing();
        pushText(pal.textDim);
        ImGui::TextWrapped("%s", insertNote_.c_str());
        ImGui::PopStyleColor();
    }

    ImGui::Spacing();
    pushText(pal.textDim);
    ImGui::TextUnformatted("OK inserts the selected part \xc2\xb7 Cancel closes.");
    ImGui::PopStyleColor();
}

// ===========================================================================
// Mate PropertyManager form (C17 PM visual style)
// ===========================================================================

void AsmPanels::beginMateForm() {
    mateOpen_ = true;
    mateFocus_ = true;
    activeWell_ = 0;
    picks_[0] = MatePick{};
    picks_[1] = MatePick{};
    picks_[2] = MatePick{};
    mateType_ = 0;
    selectedMateType_.clear();
    editComponentTarget_.clear();   // plain launch; the tree "Edit" seam re-stamps this after us
    aligned_ = true;
    auto initBuf = [](std::vector<char>& b, const char* v) {
        std::memset(b.data(), 0, b.size());
        std::strncpy(b.data(), v, b.size() - 1);
    };
    initBuf(dimBuf_,      "10");
    initBuf(minBuf_,      "0");
    initBuf(maxBuf_,      "100");
    initBuf(ratioBuf_,    "1");
    initBuf(pitchBuf_,    "1");
    initBuf(pitchRadBuf_, "");
    mateError_.clear();
}

void AsmPanels::closeMateForm() { mateOpen_ = false; editComponentTarget_.clear(); }

// The mate type acting this frame: an explicit Mechanical/Advanced pick (or a Standard pick that
// also stamps selectedMateType_), else the surface-driven suggestion.
std::string AsmPanels::effectiveMateType() const {
    if (!selectedMateType_.empty()) return selectedMateType_;
    return suggestedMate();
}

bool AsmPanels::typeNeedsThirdRef(const std::string& t) {
    return t == "width" || t == "symmetric";
}

void AsmPanels::presetMateTypeForShot(const std::string& type) {
    selectedMateType_ = type;
    mateError_.clear();
}

bool AsmPanels::routeAsmPick(const nlohmann::json& selector, const std::string& component,
                             int /*pickKind*/) {
    if (!mateOpen_) return false;
    // Width / symmetric expose a 3rd well (the jaw / symmetry plane); other types only have 2.
    const int wellCount = typeNeedsThirdRef(effectiveMateType()) ? 3 : 2;
    int w = activeWell_;
    if (w >= wellCount) w = 0;
    picks_[w].has = true;
    picks_[w].selector = selector;
    picks_[w].component = component;
    picks_[w].surf = classifySurf(selector);
    // Chip label "name · Face/Edge".
    std::string kind = selector.value("kind", std::string("face"));
    std::string kindLabel = kind == "face" ? "Face" : kind == "edge" ? "Edge" : "Vert";
    picks_[w].label = component + " \xC2\xB7 " + kindLabel;  // middle dot
    // Advance to the next empty well (SolidWorks fills references in order).
    activeWell_ = w;
    for (int i = 0; i < wellCount; ++i)
        if (!picks_[i].has) { activeWell_ = i; return true; }
    activeWell_ = (w + 1) % wellCount;  // all full — cycle so a re-pick replaces
    return true;
}

// Auto-suggested mate types from the two picked surfaces.
// plane+plane -> coincident / parallel / distance ; cyl+cyl -> concentric.
static const char* const kPlanePlane[] = {"coincident", "parallel", "distance"};
static const char* const kCylCyl[]     = {"concentric"};
static const char* const kGeneric[]    = {"coincident", "parallel", "distance", "concentric"};

const char* AsmPanels::suggestedMate() const {
    MateSurfKind a = picks_[0].surf, b = picks_[1].surf;
    if (a == MateSurfKind::Plane && b == MateSurfKind::Plane) {
        int n = mateType_ % 3; return kPlanePlane[n];
    }
    if (a == MateSurfKind::Cylinder && b == MateSurfKind::Cylinder) {
        return kCylCyl[0];
    }
    int n = mateType_ % 4; return kGeneric[n];
}

bool AsmPanels::addMate(AppState& st, const std::string& type, const std::string& align,
                        const nlohmann::json& refs, std::string& aicmOut) {
    aicmOut.clear();
    if (!st.docMirror.hasDoc()) return false;
    nlohmann::json mate = {
        {"type", type}, {"align", align}, {"suppressed", false}, {"value", nullptr}, {"refs", refs}};
    // ---- value object per the engine contract (schema.py MATE_VALUE_TYPES / MATE_COUPLING_SPEC) ----
    // pmParseDim wrapper for one field; angle=true parses degrees. On failure mateError_ is set.
    auto parseField = [&](std::vector<char>& buf, const char* label, bool angle,
                          double& out) -> bool {
        std::string txt(buf.data()); std::string perr;
        // Bare length literals follow the active document unit (IPS -> inch, MMGS -> mm);
        // angles are always degrees.
        const double bareScale = (!angle && st.ips()) ? 25.4 : 1.0;
        if (!pmParseDim(txt, out, perr, angle, bareScale)) { mateError_ = std::string(label) + ": " + perr; return false; }
        return true;
    };
    if (type == "distance" || type == "angle") {
        // Single dimension {value, expr}.
        double v = 0;
        if (!parseField(dimBuf_, "value", type == "angle", v)) return false;
        mate["value"] = {{"value", v}, {"expr", nullptr}};
    } else if (type == "distance_limit" || type == "angle_limit") {
        // Required {min, max} (numeric scalars, NOT dims).
        const bool angle = (type == "angle_limit");
        double lo = 0, hi = 0;
        if (!parseField(minBuf_, "min", angle, lo)) return false;
        if (!parseField(maxBuf_, "max", angle, hi)) return false;
        mate["value"] = {{"min", lo}, {"max", hi}};
    } else if (type == "gear") {
        double ratio = 1;
        if (!parseField(ratioBuf_, "ratio", false, ratio)) return false;
        mate["value"] = {{"ratio", ratio}};
    } else if (type == "screw") {
        double pitch = 1;
        if (!parseField(pitchBuf_, "pitch", false, pitch)) return false;
        mate["value"] = {{"pitch", pitch}};
    } else if (type == "rack_pinion") {
        // pitch_radius is OPTIONAL: blank -> value:null (engine derives from the pinion cylinder).
        std::string txt(pitchRadBuf_.data());
        bool blank = txt.find_first_not_of(" \t") == std::string::npos;
        if (!blank) {
            double pr = 0;
            if (!parseField(pitchRadBuf_, "pitch_radius", false, pr)) return false;
            mate["value"] = {{"pitch_radius", pr}};
        }
        // else leave value:null
    } else if (type == "hinge") {
        // min/max are OPTIONAL (defaults 0..360). Emit them when given (both buffers non-blank).
        std::string lotxt(minBuf_.data()), hitxt(maxBuf_.data());
        bool loBlank = lotxt.find_first_not_of(" \t") == std::string::npos;
        bool hiBlank = hitxt.find_first_not_of(" \t") == std::string::npos;
        if (!loBlank || !hiBlank) {
            double lo = 0, hi = 360;
            if (!loBlank && !parseField(minBuf_, "min", true, lo)) return false;
            if (!hiBlank && !parseField(maxBuf_, "max", true, hi)) return false;
            mate["value"] = {{"min", lo}, {"max", hi}};
        }
        // else leave value:null (engine defaults 0..360)
    }
    // All other types (coincident/parallel/perpendicular/tangent/concentric/lock, slot,
    // cam_follower, width, symmetric, profile_center) keep value:null.
    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({
        {{"op", "asm_add_mate"}, {"mate", mate}}}));
    if (!r.ok) { mateError_ = r.error_message; st.addLog("[err] add_mate: " + r.error_message); return false; }
    // Solve the mates so the placed transform updates (asm_add_mate alone re-tessellates but the
    // engine already solves on rebuild; an explicit solve keeps drag/warm state consistent and
    // matches the verified flow). The apply already rebuilt+solved, so reload its aicm.
    aicmOut = r.aicm_path;
    if (!aicmOut.empty()) { std::string le; st.loadDocMesh(aicmOut, le); }
    return true;
}

void AsmPanels::drawMateFormBody(AppState& st) {
    if (!mateOpen_) return;
    const auto& pal = ui::palette;
    mateFocus_ = false;
    const ImGuiStyle& style = ImGui::GetStyle();
    auto continueFlow = [&style](float nextWidth) {
        const ImVec2 cursor = ImGui::GetCursorScreenPos();
        const float contentRight = cursor.x + ImGui::GetContentRegionAvail().x;
        const float nextRight =
            ImGui::GetItemRectMax().x + style.ItemSpacing.x + nextWidth;
        if (nextRight <= contentRight) ImGui::SameLine();
    };
    auto continueButtonFlow = [&style, &continueFlow](const char* label) {
        continueFlow(ImGui::CalcTextSize(label).x + style.FramePadding.x * 2.0f);
    };


    if (!editComponentTarget_.empty()) {
        pushText(pal.textDim);
        ImGui::TextWrapped("Editing placement of: %s",
                           componentName(st, editComponentTarget_).c_str());
        ImGui::PopStyleColor();
        ImGui::Spacing();
    }

    std::string type = effectiveMateType();
    const int wellCount = typeNeedsThirdRef(type) ? 3 : 2;
    if (activeWell_ >= wellCount) activeWell_ = 0;

    // ---- Mate Selections — standard PM selection wells ----
    ImGui::SeparatorText("Mate Selections");
    static const char* const kWellLabels[3] = {
        "Reference 1", "Reference 2", "Reference 3 (jaw / plane)"};
    static const char* const kWellIds[3] = {
        "##asm_mate_well0", "##asm_mate_well1", "##asm_mate_well2"};
    for (int i = 0; i < wellCount; ++i) {
        const char* chip = picks_[i].has ? picks_[i].label.c_str() : "(pick in viewport)";
        auto wr = pmDrawSelectionWell(kWellIds[i], kWellLabels[i], chip,
                                      /*active=*/activeWell_ == i);
        if (wr.clicked) activeWell_ = i;
        if (wr.rightClicked) { picks_[i] = MatePick{}; activeWell_ = i; }
        ImGui::Spacing();
    }

    // ---- Standard Mates ----
    ImGui::SeparatorText("Standard Mates");
    bool bothPlanes = picks_[0].surf == MateSurfKind::Plane && picks_[1].surf == MateSurfKind::Plane;
    bool bothCyl    = picks_[0].surf == MateSurfKind::Cylinder && picks_[1].surf == MateSurfKind::Cylinder;
    const char* const* opts; int nopts;
    if (bothPlanes)   { opts = kPlanePlane; nopts = 3; }
    else if (bothCyl) { opts = kCylCyl;     nopts = 1; }
    else              { opts = kGeneric;    nopts = 4; }
    if (mateType_ >= nopts) mateType_ = 0;
    for (int i = 0; i < nopts; ++i) {
        std::string label = opts[i];
        if (!label.empty()) label[0] = (char)std::toupper((unsigned char)label[0]);
        if (i) continueButtonFlow(label.c_str());
        bool sel = selectedMateType_.empty()
            ? (mateType_ == i)
            : (selectedMateType_ == opts[i]);
        if (sel) ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1.0f));
        if (ImGui::Button((label + "##mt" + std::to_string(i)).c_str())) {
            mateType_ = i;
            selectedMateType_ = opts[i];
        }
        if (sel) ImGui::PopStyleColor();
    }

    ImGui::Spacing();
    ImGui::SeparatorText("Mechanical & Advanced Mates");
    struct AdvOpt { const char* label; const char* type; };
    static const AdvOpt kAdvOpts[] = {
        {"Gear", "gear"}, {"Screw", "screw"}, {"Rack & Pinion", "rack_pinion"},
        {"Distance Limit", "distance_limit"}, {"Angle Limit", "angle_limit"},
        {"Hinge", "hinge"}, {"Slot", "slot"}, {"Cam Follower", "cam_follower"},
        {"Profile Center", "profile_center"}, {"Width", "width"}, {"Symmetric", "symmetric"},
    };
    for (int i = 0; i < (int)(sizeof(kAdvOpts) / sizeof(kAdvOpts[0])); ++i) {
        bool sel = (selectedMateType_ == kAdvOpts[i].type);
        if (i) continueButtonFlow(kAdvOpts[i].label);
        if (sel) ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1.0f));
        if (ImGui::Button((std::string(kAdvOpts[i].label) + "##adv" + std::to_string(i)).c_str())) {
            selectedMateType_ = kAdvOpts[i].type;
            mateError_.clear();
            if (activeWell_ >= (typeNeedsThirdRef(selectedMateType_) ? 3 : 2)) activeWell_ = 0;
        }
        if (sel) ImGui::PopStyleColor();
    }
    type = effectiveMateType();

    ImGui::Spacing();
    ImGui::TextUnformatted("Mate alignment:");
    if (ImGui::RadioButton("Aligned", aligned_)) aligned_ = true;
    const float antiAlignedW = ImGui::GetFrameHeight() + style.ItemInnerSpacing.x +
                               ImGui::CalcTextSize("Anti-Aligned").x;
    continueFlow(antiAlignedW);
    if (ImGui::RadioButton("Anti-Aligned", !aligned_)) aligned_ = false;
    pushText(pal.textDim);
    ImGui::TextWrapped("Seating one face flat onto another is Anti-Aligned (the normals oppose).");
    ImGui::PopStyleColor();

    auto field = [](const char* label, std::vector<char>& buf, const char* id) {
        ImGui::TextUnformatted(label);
        ImGui::SetNextItemWidth(
            std::min(ui::metrics::asmInputW(), ImGui::GetContentRegionAvail().x));
        ImGui::InputText(id, buf.data(), buf.size());
    };
    ImGui::Spacing();
    if (type == "distance" || type == "angle") {
        field(type == "distance" ? "Distance:" : "Angle (deg):", dimBuf_, "##asm_mate_dim");
    } else if (type == "distance_limit" || type == "angle_limit") {
        const bool angle = (type == "angle_limit");
        field(angle ? "Min angle (deg):" : "Min distance (mm):", minBuf_, "##asm_mate_min");
        field(angle ? "Max angle (deg):" : "Max distance (mm):", maxBuf_, "##asm_mate_max");
    } else if (type == "gear") {
        field("Ratio:", ratioBuf_, "##asm_mate_ratio");
    } else if (type == "screw") {
        field("Pitch (mm/rev):", pitchBuf_, "##asm_mate_pitch");
    } else if (type == "rack_pinion") {
        field("Pitch radius (mm, optional):", pitchRadBuf_, "##asm_mate_pitchrad");
        pushText(pal.textDim);
        ImGui::TextWrapped("Leave blank to derive from the pinion's cylinder radius.");
        ImGui::PopStyleColor();
    } else if (type == "hinge") {
        field("Min angle (deg, optional):", minBuf_, "##asm_mate_hmin");
        field("Max angle (deg, optional):", maxBuf_, "##asm_mate_hmax");
        pushText(pal.textDim);
        ImGui::TextWrapped("Leave blank for the default 0..360 swing range.");
        ImGui::PopStyleColor();
    } else {
        pushText(pal.textDim);
        ImGui::TextWrapped(typeNeedsThirdRef(type)
            ? "Pick two faces/edges plus the jaw or symmetry plane (Reference 3)."
            : "No parameters — this mate is geometry-driven.");
        ImGui::PopStyleColor();
    }

    if (!mateError_.empty()) {
        ImGui::Spacing();
        pushText(pal.statusErr);
        ImGui::TextWrapped("%s", mateError_.c_str());
        ImGui::PopStyleColor();
    }

    ImGui::Spacing();
    pushText(pal.textDim);
    ImGui::TextUnformatted("OK adds the mate (form stays open) \xc2\xb7 Cancel closes.");
    ImGui::PopStyleColor();
}

// ===========================================================================
// Component Pattern (linear / circular)
// ===========================================================================
//
// SolidWorks "Linear/Circular Component Pattern": pick one or more seed components, a direction (or
// axis), and a count/spacing (or count/angle). Emits an add_feature op carrying an
// asm_linear_pattern / asm_circular_pattern feature (schema FEATURE_SPECS) which the engine's
// assembly compiler expands into synthetic instance bodies. The direction/axis for v1 are the
// assembly WORLD/datum axes (engine assembly._world_axis_from_ref); model-edge picks are a
// follow-up.

// World direction/axis options the engine resolver (_world_axis_from_ref) accepts as plain strings.
struct AsmDirOpt { const char* label; const char* ref; };
static const AsmDirOpt kAsmDirOpts[] = {
    {"X axis",          "axis.x"},
    {"Y axis",          "axis.y"},
    {"Z axis",          "axis.z"},
    {"Front plane (Y)", "plane.front"},
    {"Top plane (Z)",   "plane.top"},
    {"Right plane (X)", "plane.right"},
};

void AsmPanels::beginPatternForm() {
    patternOpen_ = true;
    patternFocus_ = true;
    patternKind_ = 0;
    patternDir_ = 0;
    patternEqual_ = true;
    patternComps_.clear();
    patternError_.clear();
    std::memset(patternCountBuf_.data(), 0, patternCountBuf_.size());
    std::strncpy(patternCountBuf_.data(), "3", patternCountBuf_.size() - 1);
    std::memset(patternSpacingBuf_.data(), 0, patternSpacingBuf_.size());
    std::strncpy(patternSpacingBuf_.data(), "20", patternSpacingBuf_.size() - 1);
    std::memset(patternAngleBuf_.data(), 0, patternAngleBuf_.size());
    std::strncpy(patternAngleBuf_.data(), "360", patternAngleBuf_.size() - 1);
}

bool AsmPanels::routeAsmPatternPick(const std::string& component) {
    if (!patternOpen_ || component.empty()) return false;
    // Toggle membership so a second pick on the same component removes it.
    auto it = std::find(patternComps_.begin(), patternComps_.end(), component);
    if (it == patternComps_.end()) patternComps_.push_back(component);
    else                           patternComps_.erase(it);
    return true;
}

bool AsmPanels::addPattern(AppState& st, std::string& aicmOut, std::string& errorOut) {
    aicmOut.clear();
    errorOut.clear();
    if (!st.docMirror.hasDoc()) { errorOut = "No assembly open."; return false; }
    if (patternComps_.empty()) { errorOut = "Pick at least one seed component."; return false; }

    const char* dirRef = kAsmDirOpts[patternDir_].ref;
    nlohmann::json comps = nlohmann::json::array();
    for (const auto& c : patternComps_) comps.push_back(c);

    nlohmann::json feature;
    if (patternKind_ == 0) {  // linear
        double count = 0, spacing = 0; std::string perr;
        std::string ctxt(patternCountBuf_.data()), stxt(patternSpacingBuf_.data());
        if (!pmParseDim(ctxt, count, perr, /*isAngle=*/false)) { errorOut = "count: " + perr; return false; }
        // spacing is a length -> bare literals follow the active doc unit (IPS inch / MMGS mm).
        if (!pmParseDim(stxt, spacing, perr, /*isAngle=*/false, st.ips() ? 25.4 : 1.0)) { errorOut = "spacing: " + perr; return false; }
        feature = {
            {"type", "asm_linear_pattern"}, {"name", "Component Pattern"}, {"suppressed", false},
            {"params", {{"count1", {{"value", count}, {"expr", nullptr}}},
                        {"spacing1", {{"value", spacing}, {"expr", nullptr}}}}},
            {"refs", {{"dir1", dirRef}, {"components", comps}}}};
    } else {                  // circular
        double count = 0, angle = 0; std::string perr;
        std::string ctxt(patternCountBuf_.data()), atxt(patternAngleBuf_.data());
        if (!pmParseDim(ctxt, count, perr, /*isAngle=*/false)) { errorOut = "count: " + perr; return false; }
        if (!pmParseDim(atxt, angle, perr, /*isAngle=*/true)) { errorOut = "angle: " + perr; return false; }
        feature = {
            {"type", "asm_circular_pattern"}, {"name", "Component Pattern"}, {"suppressed", false},
            {"params", {{"count", {{"value", count}, {"expr", nullptr}}},
                        {"angle", {{"value", angle}, {"expr", nullptr}}},
                        {"equal_spacing", patternEqual_}}},
            {"refs", {{"axis", dirRef}, {"components", comps}}}};
    }

    auto r = st.docMirror.applyOps(st.worker, nlohmann::json::array({
        {{"op", "add_feature"}, {"feature", feature}}}));
    if (!r.ok) { errorOut = r.error_message; return false; }
    aicmOut = r.aicm_path;
    if (!aicmOut.empty()) { std::string le; st.loadDocMesh(aicmOut, le); }
    return true;
}

void AsmPanels::drawPatternFormBody(AppState& st) {
    if (!patternOpen_) return;
    const auto& pal = ui::palette;
    patternFocus_ = false;

    if (!st.docMirror.hasDoc()) {
        pushText(pal.statusWarn);
        ImGui::TextWrapped("No assembly open.");
        ImGui::PopStyleColor();
        return;
    }

    ImGui::TextUnformatted("Type:");
    ImGui::SameLine();
    if (ImGui::RadioButton("Linear", patternKind_ == 0)) patternKind_ = 0;
    ImGui::SameLine();
    if (ImGui::RadioButton("Circular", patternKind_ == 1)) patternKind_ = 1;

    ImGui::Spacing();
    ImGui::TextUnformatted(patternKind_ == 0 ? "Direction:" : "Axis:");
    ImGui::SetNextItemWidth(-1);
    if (ImGui::BeginCombo("##asm_pat_dir", kAsmDirOpts[patternDir_].label)) {
        for (int i = 0; i < (int)(sizeof(kAsmDirOpts) / sizeof(kAsmDirOpts[0])); ++i) {
            bool sel = (patternDir_ == i);
            if (ImGui::Selectable(kAsmDirOpts[i].label, sel)) patternDir_ = i;
            if (sel) ImGui::SetItemDefaultFocus();
        }
        ImGui::EndCombo();
    }

    ImGui::Spacing();
    if (patternKind_ == 0) {
        ImGui::TextUnformatted("Instances (count):");
        ImGui::SetNextItemWidth(ui::metrics::asmInputW());
        ImGui::InputText("##asm_pat_count", patternCountBuf_.data(), patternCountBuf_.size());
        ImGui::TextUnformatted("Spacing (mm):");
        ImGui::SetNextItemWidth(ui::metrics::asmInputW());
        ImGui::InputText("##asm_pat_spacing", patternSpacingBuf_.data(), patternSpacingBuf_.size());
    } else {
        ImGui::TextUnformatted("Instances (count):");
        ImGui::SetNextItemWidth(ui::metrics::asmInputW());
        ImGui::InputText("##asm_pat_count", patternCountBuf_.data(), patternCountBuf_.size());
        ImGui::TextUnformatted("Angle (deg):");
        ImGui::SetNextItemWidth(ui::metrics::asmInputW());
        ImGui::InputText("##asm_pat_angle", patternAngleBuf_.data(), patternAngleBuf_.size());
        ImGui::Checkbox("Equal spacing (fill angle)", &patternEqual_);
    }

    ImGui::Spacing();
    ImGui::SeparatorText("Components to Pattern");
    // Multi-select well visual for seed components (picks toggle via routeAsmPatternPick).
    {
        std::string chip;
        if (patternComps_.empty()) {
            chip = "(pick components in viewport)";
        } else if (patternComps_.size() == 1) {
            chip = componentName(st, patternComps_[0]);
        } else {
            chip = componentName(st, patternComps_[0]) + " x" +
                   std::to_string(patternComps_.size());
        }
        auto wr = pmDrawSelectionWell("##asm_pat_comps", "Seed components",
                                      chip.c_str(), /*active=*/true);
        if (wr.rightClicked) patternComps_.clear();
        (void)wr.clicked;  // always active while form is open
    }
    if (!patternComps_.empty()) {
        for (size_t i = 0; i < patternComps_.size(); ++i) {
            ImGui::Bullet();
            ImGui::TextUnformatted(componentName(st, patternComps_[i]).c_str());
            ImGui::SameLine();
            if (ImGui::SmallButton(("x##pc" + std::to_string(i)).c_str())) {
                patternComps_.erase(patternComps_.begin() + i);
                break;
            }
        }
    }

    if (!patternError_.empty()) {
        ImGui::Spacing();
        pushText(pal.statusErr);
        ImGui::TextWrapped("%s", patternError_.c_str());
        ImGui::PopStyleColor();
    }

    ImGui::Spacing();
    pushText(pal.textDim);
    ImGui::TextUnformatted("OK adds the pattern (form stays open) \xc2\xb7 Cancel closes.");
    ImGui::PopStyleColor();
}

// ===========================================================================
// Exploded View
// ===========================================================================

void AsmPanels::kickExplodeAsync(AppState& st) {
    if (explodeInFlight_ || !explodePending_) return;
    if (!st.docMirror.hasDoc() || !st.workerStarted) return;
    explodePending_ = false;
    explodeInFlight_ = true;
    const int gen = ++explodeGen_;
    const bool active = explodePendingActive_;
    const float factor = explodePendingFactor_;
    // Optimistic UI: slider already owns explodeFactor_; keep active flag in step with pending.
    explodeActive_ = active;
    explodeFactor_ = factor;
    nlohmann::json params = {{"doc_id", st.docMirror.docId()},
                             {"active", active}, {"factor", factor}};
    st.rpc.call("doc_set_explode_state", std::move(params),
                [this, &st, gen, active, factor](const core::RpcResult& r) {
                    explodeInFlight_ = false;
                    if (gen != explodeGen_) {
                        if (explodePending_) kickExplodeAsync(st);
                        return;
                    }
                    if (!r.ok) {
                        st.addLog("[err] explode: " + r.err);
                    } else if (r.result.is_object() && r.result.contains("code")) {
                        st.addLog("[err] explode: " +
                                  r.result.value("message", std::string()));
                    } else {
                        std::string aicm;
                        if (r.result.contains("tess") && r.result["tess"].is_object())
                            aicm = r.result["tess"].value("aicm_path", std::string());
                        if (!aicm.empty()) { std::string le; st.loadDocMesh(aicm, le); }
                        explodeActive_ = active;
                        explodeFactor_ = factor;
                    }
                    if (explodePending_) kickExplodeAsync(st);
                }, /*timeoutMs=*/60000);
}

bool AsmPanels::setExplode(AppState& st, bool active, float factor) {
    if (!st.docMirror.hasDoc()) return false;
    // T-PERF2: coalesce rapid slider samples to latest factor; never block GL.
    explodePendingActive_ = active;
    explodePendingFactor_ = factor;
    explodePending_ = true;
    explodeFactor_ = factor;  // slider tracks immediately
    explodeActive_ = active;
    kickExplodeAsync(st);
    return true;
}

void AsmPanels::drawExplodeBody(AppState& st) {
    if (!explodeOpen_) return;
    explodeFocus_ = false;
    ImGui::TextUnformatted("Explode factor");
    ImGui::Spacing();
    ImGui::SetNextItemWidth(-1);
    if (ImGui::SliderFloat("##explode_factor", &explodeFactor_, 0.0f, 1.0f, "%.2f")) {
        setExplode(st, explodeFactor_ > 0.001f, explodeFactor_);
    }
    pushText(ui::palette.textDim);
    ImGui::TextWrapped("View-only — drag to slide components apart along their explode vectors.");
    ImGui::PopStyleColor();
    ImGui::Spacing();
    if (ImGui::Button("Collapse", ImVec2(ui::metrics::asmBtnW(), 0))) {
        explodeFactor_ = 0.0f; setExplode(st, false, 0.0f);
    }
    ImGui::SameLine();
    if (ImGui::Button("Exploded", ImVec2(ui::metrics::asmBtnW(), 0))) {
        explodeFactor_ = 1.0f; setExplode(st, true, 1.0f);
    }
}

// ===========================================================================
// Interference Detection
// ===========================================================================

static bool assemblyResultIsActive(const AppState& st, const std::string& docId) {
    return !docId.empty() && st.activeTab >= 0 &&
        st.activeTab < static_cast<int>(st.docTabs.size()) &&
        st.docTabs[st.activeTab].kind == AppState::DocTab::TabKind::Assembly &&
        st.docTabs[st.activeTab].docId == docId;
}

static void staleAssemblyResultNotice(const AppState& st, const std::string& docId,
                                      const std::string& docName) {
    bool open = false;
    for (const auto& tab : st.docTabs)
        if (tab.docId == docId) { open = true; break; }
    const std::string label = docName.empty() ? docId : docName;
    ImGui::TextWrapped("Results are for %s, not the active document%s. "
                       "Select that assembly or refresh for the active assembly.",
                       label.c_str(), open ? "" : " (source document is closed)");
}

// C39: async — pairwise boolean intersections scale with component count and used to freeze
// the window for up to 60s. The window opens immediately showing "Computing..."; the result
// fills in when the call drains (GL thread). A re-refresh while pending supersedes (gen).
void AsmPanels::refreshInterference(AppState& st) {
    interferenceError_.clear();
    interferenceResult_ = nlohmann::json::object();
    interferencePending_ = false;
    interferenceDocId_ = st.docMirror.hasDoc() ? st.docMirror.docId() : std::string();
    interferenceDocName_ = interferenceDocId_;
    for (const auto& tab : st.docTabs)
        if (tab.docId == interferenceDocId_) {
            interferenceDocName_ = tab.displayTitle();
            break;
        }
    ++interferenceGen_;
    if (!st.docMirror.hasDoc()) { interferenceError_ = "No assembly open."; return; }
    interferencePending_ = true;
    const int gen = interferenceGen_;
    ++st.rebuildsInFlight;
    st.rpc.call("eval_interference", {{"doc_id", st.docMirror.docId()}},
        [this, &st, gen](const core::RpcResult& r) {
            --st.rebuildsInFlight;
            if (gen != interferenceGen_) return;
            interferencePending_ = false;
            if (!r.ok) {
                interferenceError_ = r.err.empty() ? "eval_interference failed" : r.err; return;
            }
            if (r.result.is_object() && r.result.contains("code")) {
                interferenceError_ = r.result.value("message", std::string("interference error"));
                return;
            }
            interferenceResult_ = r.result;
            // Surface a summary in the console so the result is observable even when the window
            // is behind the docked viewport (and useful for scripted shots).
            const auto& res = interferenceResult_;
            int nInter = (res.contains("interferences") && res["interferences"].is_array())
                ? (int)res["interferences"].size() : 0;
            int nClear = (res.contains("clearances") && res["clearances"].is_array())
                ? (int)res["clearances"].size() : 0;
            st.addLog("[asm] interference: " + std::to_string(nInter) + " overlap(s), " +
                      std::to_string(nClear) + " clearance(s)");
        }, 60000);
}

void AsmPanels::drawInterferenceBody(AppState& st) {
    if (!interferenceOpen_) return;
    const auto& pal = ui::palette;
    interferenceFocus_ = false;

    ImGui::Text("Interference Detection — %s", interferenceDocName_.c_str());
    const bool activeAssembly = st.activeTab >= 0 &&
        st.activeTab < static_cast<int>(st.docTabs.size()) &&
        st.docTabs[st.activeTab].kind == AppState::DocTab::TabKind::Assembly &&
        st.docMirror.docId() == st.docTabs[st.activeTab].docId;
    ImGui::BeginDisabled(!activeAssembly);
    if (ImGui::Button("Refresh", ImVec2(ui::metrics::asmBtnW(), 0)))
        refreshInterference(st);
    ImGui::EndDisabled();
    ImGui::Spacing();
    if (!assemblyResultIsActive(st, interferenceDocId_)) {
        staleAssemblyResultNotice(st, interferenceDocId_, interferenceDocName_);
        return;
    }

    if (interferencePending_) {
        pushText(pal.textDim);
        ImGui::TextUnformatted("Computing interference...");
        ImGui::PopStyleColor();
    } else if (!interferenceError_.empty()) {
        pushText(pal.statusErr);
        ImGui::TextWrapped("%s", interferenceError_.c_str());
        ImGui::PopStyleColor();
    } else {
        const auto& r = interferenceResult_;
        auto inter = r.contains("interferences") && r["interferences"].is_array()
            ? r["interferences"] : nlohmann::json::array();
        auto clear = r.contains("clearances") && r["clearances"].is_array()
            ? r["clearances"] : nlohmann::json::array();
        bool clash = !inter.empty();
        const bool verified = r.value("complete", false) && r.value("pairs_checked", 0) > 0;
        pushText(clash || !verified ? pal.statusErr : pal.statusOk);
        ImGui::TextUnformatted(!r.value("complete", false) ? "Interference check incomplete" :
                              clash ? "Interference detected" :
                              verified ? "No interference" : "No body pairs checked");
        ImGui::PopStyleColor();
        ImGui::Spacing();
        if (ImGui::BeginTable("##inter_tbl", 3,
            ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg | ImGuiTableFlags_SizingStretchProp)) {
            ImGui::TableSetupColumn("Component A");
            ImGui::TableSetupColumn("Component B");
            ImGui::TableSetupColumn("Overlap / Gap");
            ImGui::TableHeadersRow();
            for (const auto& it : inter) {
                ImGui::TableNextRow();
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1.0f));
                ImGui::TableNextColumn(); ImGui::TextUnformatted(it.value("a", std::string()).c_str());
                ImGui::TableNextColumn(); ImGui::TextUnformatted(it.value("b", std::string()).c_str());
                ImGui::TableNextColumn();
                ImGui::Text("%.3f mm3 overlap", it.value("volume", 0.0));
                ImGui::PopStyleColor();
            }
            for (const auto& it : clear) {
                ImGui::TableNextRow();
                ImGui::TableNextColumn(); ImGui::TextUnformatted(it.value("a", std::string()).c_str());
                ImGui::TableNextColumn(); ImGui::TextUnformatted(it.value("b", std::string()).c_str());
                ImGui::TableNextColumn();
                if (it.contains("distance_lower_bound_mm") && it["distance_lower_bound_mm"].is_number())
                    ImGui::Text(">= %.3f mm (lower bound, not measured)",
                                it["distance_lower_bound_mm"].get<double>());
                else if (it.contains("distance") && it["distance"].is_number())
                    ImGui::Text("%.3f mm %s", it["distance"].get<double>(),
                                it.value("approx", false) ? "lower bound (not measured)" : "gap");
                else
                    ImGui::TextUnformatted("Exact gap not measured");
            }
            ImGui::EndTable();
        }
        if (inter.empty() && clear.empty()) {
            pushText(pal.textDim);
            ImGui::TextWrapped("Fewer than two components, or nothing to compare.");
            ImGui::PopStyleColor();
        }
    }
}

// ===========================================================================
// BOM (Bill of Materials)
// ===========================================================================

void AsmPanels::refreshBom(AppState& st) {
    bomRows_.clear();
    bomNote_.clear();
    bomPending_ = 0;
    bomDocId_ = st.docMirror.hasDoc() ? st.docMirror.docId() : std::string();
    bomDocName_ = bomDocId_;
    for (const auto& tab : st.docTabs)
        if (tab.docId == bomDocId_) {
            bomDocName_ = tab.displayTitle();
            break;
        }
    const int gen = ++bomGen_;  // C39: invalidates eval_mass callbacks of an older refresh
    const auto& tree = st.docMirror.tree();
    if (!tree.is_object() || !tree.contains("components") || !tree["components"].is_array()) {
        bomNote_ = "No assembly components.";
        return;
    }
    // Qty rollup by source path; first occurrence holds the name + ordinal (for eval_mass).
    std::map<std::string, int> rowIndexBySource;
    std::vector<int> ordinalForRow;   // 1-based component ordinal of the first occurrence
    int ordinal = 0;
    for (const auto& c : tree["components"]) {
        if (!c.is_object()) continue;
        ++ordinal;
        std::string source = (c.contains("source") && c["source"].is_object())
            ? c["source"].value("path", std::string()) : std::string();
        std::string name = c.value("name", std::string());
        auto it = rowIndexBySource.find(source);
        if (it == rowIndexBySource.end()) {
            BomRow row; row.name = name; row.source = source; row.qty = 1;
            rowIndexBySource[source] = (int)bomRows_.size();
            ordinalForRow.push_back(ordinal);
            bomRows_.push_back(row);
        } else {
            bomRows_[it->second].qty += 1;
        }
    }
    // Mass per distinct source via eval_mass on its component ordinal. C39: the rows draw
    // immediately; each mass fills in as its async call drains (the old sync loop froze the
    // window for rows x call-time). Failures just leave haveMass=false (blank cell), as before.
    for (size_t i = 0; i < bomRows_.size(); ++i) {
        nlohmann::json params = {{"doc_id", st.docMirror.docId()}, {"body", ordinalForRow[i]}};
        ++bomPending_;
        ++st.rebuildsInFlight;
        st.rpc.call("eval_mass", params, [this, &st, gen, i](const core::RpcResult& r) {
            --st.rebuildsInFlight;
            if (gen != bomGen_) return;  // a newer refresh rebuilt the rows
            --bomPending_;
            if (r.ok && r.result.is_object() && !r.result.contains("code") &&
                i < bomRows_.size()) {
                bomRows_[i].volMm3 = r.result.value("volume_mm3", 0.0);
                bomRows_[i].massG = r.result.value("mass_g", 0.0);
                bomRows_[i].haveMass = true;
            }
        }, 60000);
    }
}

void AsmPanels::drawBom(AppState& st) {
    if (!bomOpen_) return;
    bool open = true;
    const std::string title = "Bill of Materials — " + bomDocName_ + "###Bill of Materials";
    ui::setNextCommandDialogPlacement("Bill of Materials",
        ImVec2(ui::metrics::px(700), ui::metrics::px(480)),
        ImVec2(ui::metrics::px(340), ui::metrics::px(300)));
    if (bomFocus_) { ImGui::SetNextWindowFocus(); bomFocus_ = false; }
    ImGui::SetNextWindowBgAlpha(1.0f);
    const bool bomWin = ImGui::Begin(title.c_str(), &open, ImGuiWindowFlags_NoDocking);
    if (bomWin) {
        ui::kit::sectionHeader("Components", "bom", nullptr, (int)bomRows_.size(), true);
        const bool activeAssembly = st.activeTab >= 0 &&
            st.activeTab < static_cast<int>(st.docTabs.size()) &&
            st.docTabs[st.activeTab].kind == AppState::DocTab::TabKind::Assembly &&
            st.docMirror.docId() == st.docTabs[st.activeTab].docId;
        ImGui::BeginDisabled(!activeAssembly);
        if (ui::kit::secondaryButton("Refresh")) refreshBom(st);
        ImGui::EndDisabled();
        ImGui::Separator();
        if (!assemblyResultIsActive(st, bomDocId_)) {
            staleAssemblyResultNotice(st, bomDocId_, bomDocName_);
            ImGui::End();
            if (!open) bomOpen_ = false;
            return;
        }

        if (!bomNote_.empty()) ui::kit::dimText(bomNote_.c_str());
        const float footerHeight = ImGui::GetTextLineHeightWithSpacing() +
                                   2 * ImGui::GetFrameHeightWithSpacing() + ui::kit::sectionGap();
        const float tableHeight = std::max(ImGui::GetFrameHeightWithSpacing(),
                                          ImGui::GetContentRegionAvail().y - footerHeight);
        if (ImGui::BeginTable("##bom_tbl", 5,
            ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg | ImGuiTableFlags_SizingFixedFit |
            ImGuiTableFlags_Resizable | ImGuiTableFlags_ScrollX | ImGuiTableFlags_ScrollY,
            ImVec2(0, tableHeight))) {
            ImGui::TableSetupScrollFreeze(0, 1);
            ImGui::TableSetupColumn("#", ImGuiTableColumnFlags_WidthFixed, ui::metrics::px(32));
            ImGui::TableSetupColumn("Part", ImGuiTableColumnFlags_WidthFixed, ui::metrics::px(180));
            ImGui::TableSetupColumn("Source", ImGuiTableColumnFlags_WidthFixed, ui::metrics::px(220));
            ImGui::TableSetupColumn("Qty", ImGuiTableColumnFlags_WidthFixed, ui::metrics::px(44));
            ImGui::TableSetupColumn("Mass (g)", ImGuiTableColumnFlags_WidthFixed, ui::metrics::px(100));
            ImGui::TableHeadersRow();
            int n = 1;
            for (const auto& row : bomRows_) {
                ImGui::TableNextRow();
                ImGui::TableNextColumn(); ImGui::Text("%d", n++);
                ImGui::TableNextColumn(); ImGui::TextWrapped("%s", row.name.c_str());
                ImGui::TableNextColumn();
                // show just the file stem to keep the column readable; full path on hover
                std::string src = row.source;
                std::string stem = src;
                size_t slash = stem.find_last_of("/\\");
                if (slash != std::string::npos) stem = stem.substr(slash + 1);
                ImGui::TextWrapped("%s", stem.c_str());
                if (ImGui::IsItemHovered() && !src.empty()) ImGui::SetTooltip("%s", src.c_str());
                ImGui::TableNextColumn(); ImGui::Text("%d", row.qty);
                ImGui::TableNextColumn();
                if (row.haveMass) ImGui::Text("%.2f", row.massG * row.qty);
                else if (bomPending_ > 0) ImGui::TextDisabled("...");  // C39: eval_mass in flight
                else ImGui::TextDisabled("Unavailable");
            }
            ImGui::EndTable();
        }

        ImGui::Spacing();
        ImGui::TextUnformatted("Export CSV to:");
        ImGui::SetNextItemWidth(-1);
        ImGui::InputText("##bom_csv", bomCsvPath_.data(), bomCsvPath_.size());
        if (ui::kit::primaryButton("Export CSV", bomCsvPath_[0] != 0)) {
            FILE* f = std::fopen(bomCsvPath_.data(), "wb");
            if (f) {
                std::fprintf(f, "Item,Part,Source,Qty,Mass_g,Volume_mm3\n");
                int idx = 1;
                for (const auto& row : bomRows_) {
                    std::fprintf(f, "%d,%s,%s,%d,%.4f,%.4f\n", idx++, row.name.c_str(),
                                 row.source.c_str(), row.qty,
                                 row.massG * row.qty, row.volMm3 * row.qty);
                }
                std::fclose(f);
                bomNote_ = "Exported to " + std::string(bomCsvPath_.data());
                st.addLog("[asm] BOM CSV -> " + std::string(bomCsvPath_.data()));
            } else {
                bomNote_ = "Could not open CSV path for writing.";
            }
        }
        ImGui::SameLine();
        if (ui::kit::secondaryButton("Close")) open = false;
    }
    ImGui::End();
    if (!open) bomOpen_ = false;
}

}  // namespace ui
