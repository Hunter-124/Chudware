// Assembly UI panels (C20b) implementation — see asm_panels.h.
#include "asm_panels.h"

#include <algorithm>
#include <cfloat>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <map>
#include <string>

#include "imgui.h"

#include "icons.h"
#include "layout.h"           // setNextCommandDialogPlacement (floating dialog anchor)
#include "metrics.h"
#include "property_manager.h"   // pmParseDim / pmFormatDim (DimField parsing)

#include "../core/app_state.h"
#include "../core/doc_mirror.h"
#include "../ui_theme.h"

namespace ui {

// Liquid-glass v2 for the floating assembly tool windows (Insert / Parts Library / Mate / Pattern /
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
        if (st.docMirror.newAssembly(st.worker, err))
            st.addLog("[asm] new assembly  rev=" + std::to_string(st.docMirror.revision()));
        else
            st.addLog("[err] new assembly: " + err);
        insertOpen_ = true; insertFocus_ = true;  // SolidWorks opens Insert Component on a new assembly
        return true;
    }
    if (cmdId == "asm_insert")  { insertOpen_ = true; insertFocus_ = true; return true; }
    if (cmdId == "asm_parts_library") { partsLibOpen_ = true; partsLibFocus_ = true; return true; }
    if (cmdId == "asm_mate")    { beginMateForm(); return true; }
    if (cmdId == "asm_explode") { explodeOpen_ = true; explodeFocus_ = true; return true; }
    if (cmdId == "asm_interference") {
        interferenceOpen_ = true; interferenceFocus_ = true; refreshInterference(st); return true;
    }
    if (cmdId == "asm_bom") { bomOpen_ = true; bomFocus_ = true; refreshBom(st); return true; }
    if (cmdId == "asm_move") {
        // Move Component: the actual gesture is a constrained viewport drag — select a (floating)
        // component, then drag it in the graphics area. The viewport owns that gesture
        // (viewport_panel.cpp s_asmDrag): doc_checkpoint on press, transient doc_solve_mates
        // {drag:{component, target}} during the drag, one final solve on release (single undo).
        // The ribbon command itself just re-solves the current mates (so a manually-edited or
        // newly-mated component re-seats) and points the user at the drag.
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
        return true;
    }
    return false;
}

void AsmPanels::draw(AppState& st) {
    drawInsert(st);
    drawPartsLibrary(st);
    drawMateForm(st);
    drawPatternForm(st);
    drawExplode(st);
    drawInterference(st);
    drawBom(st);
}

// ===========================================================================
// Insert Component
// ===========================================================================

bool AsmPanels::insertComponent(AppState& st, const std::string& path,
                                const nlohmann::json& transform, std::string& aicmOut) {
    aicmOut.clear();
    if (!st.docMirror.hasDoc()) return false;
    nlohmann::json op = {{"op", "asm_insert_component"}, {"source", {{"path", path}}}};
    if (!transform.is_null()) op["transform"] = transform;
    // T-PERF1: async so large component inserts don't freeze the GL thread. aicmOut is filled
    // only on the sync path for callers that need it immediately; the mesh reloads in the
    // drain callback either way.
    aicmOut.clear();
    ++st.rebuildsInFlight;
    st.docMirror.applyOpsAsync(st.rpc, st.worker, nlohmann::json::array({op}),
        [&st](const core::OpsResponse& r, bool applied) {
            --st.rebuildsInFlight;
            if (!applied) return;
            if (!r.ok) { st.addLog("[err] insert: " + r.error_message); return; }
            if (!r.aicm_path.empty()) st.loadDocMeshAsync(r.aicm_path);  // T-PERF3
        });
    return true;
}

void AsmPanels::drawInsert(AppState& st) {
    if (!insertOpen_) return;
    bool open = true;
    ui::setNextCommandDialogPlacement();   // first-open anchor over the full-width viewport
    ImGui::SetNextWindowSize(ImVec2(380 * ui::uiScale, 0), ImGuiCond_Appearing);  // W1-B (item 10): DPI
    if (insertFocus_) { ImGui::SetNextWindowFocus(); insertFocus_ = false; }
    pushGlassWindow();
    const bool insWin = ImGui::Begin("Insert Component", &open);
    popGlassWindow();
    if (insWin) {
        paintGlassWindow("asm_insert_glass");
        asmIcon("insert_component", "[+]"); ImGui::SameLine(0, ui::metrics::asmHeaderIconGap());
        ImGui::TextUnformatted("Insert a part into the assembly");
        ImGui::Spacing();
        if (!st.docMirror.hasDoc()) {
            pushText(ui::palette.statusWarn);
            ImGui::TextWrapped("No assembly open. Use File > New Assembly first.");
            ImGui::PopStyleColor();
        } else {
            ImGui::TextUnformatted("Source file (.step+ / .step / .stl):");
            ImGui::SetNextItemWidth(-1);
            ImGui::InputText("##asm_insert_path", insertPath_.data(), insertPath_.size());
            const bool hasComps =
                st.docMirror.tree().is_object() &&
                st.docMirror.tree().contains("components") &&
                st.docMirror.tree()["components"].is_array() &&
                !st.docMirror.tree()["components"].empty();
            pushText(ui::palette.textDim);
            ImGui::TextUnformatted(hasComps
                ? "Inserted at the origin (drag/mate to position)."
                : "First component is fixed automatically (SolidWorks behaviour).");
            ImGui::PopStyleColor();
            ImGui::Spacing();
            if (ImGui::Button("Insert", ImVec2(ui::metrics::asmBtnW(), 0)) && insertPath_[0]) {
                std::string aicm;
                if (insertComponent(st, insertPath_.data(), nullptr, aicm))
                    st.addLog("[asm] inserted: " + std::string(insertPath_.data()));
            }
            ImGui::SameLine();
            if (ImGui::Button("Close", ImVec2(ui::metrics::asmBtnW(), 0))) insertOpen_ = false;
        }
        endGlassWindow();
    }
    ImGui::End();
    if (!open) insertOpen_ = false;
}

// ===========================================================================
// Standard Parts Library — parametric catalog browser (parts_catalog_list / parts_make)
// ===========================================================================

namespace {
// A stable cache path per part identity, so the same part dedupes to one .step and the
// assembly's source.path stays valid across rebuilds. Sanitizes size designators
// ("1/4-20" / "#6-32") into filename-safe tokens.
std::string partsCacheDir() {
    // appdataDir() resolves %APPDATA% on Windows / XDG (~/.local/share) on POSIX; '/' is a
    // valid separator on both, so the cache path is correct cross-platform.
    return appdataAppDir() + "/cache/parts_library";
}
std::string sanitizeToken(const std::string& s) {
    std::string out;
    for (char c : s) out += (c == '/' || c == '\\' || c == '#' || c == ' ' || c == '.') ? '_' : c;
    return out;
}
}  // namespace

bool AsmPanels::insertCatalogPart(AppState& st, const std::string& family,
                                  const std::string& size, double length_mm,
                                  std::string& aicmOut) {
    aicmOut.clear();
    if (!st.docMirror.hasDoc()) {
        partsNote_ = "Open or create an assembly first.";
        return false;
    }
    std::error_code ec;
    std::filesystem::create_directories(partsCacheDir(), ec);
    std::string fname = family + "_" + sanitizeToken(size);
    if (length_mm > 0.0) fname += "_" + std::to_string((int)length_mm);
    const std::string stepPath = partsCacheDir() + "\\" + fname + ".step";

    nlohmann::json params = {{"family", family}, {"size", size}, {"save_path", stepPath}};
    if (length_mm > 0.0) params["length_mm"] = length_mm;
    nlohmann::json res;
    std::string err;
    if (!st.worker.call("parts_make", params, res, err)) {
        partsNote_ = "parts_make failed: " + err;
        st.addLog("[err] parts_make: " + err);
        return false;
    }
    if (!res.value("ok", false)) {
        const std::string e = res.value("error", std::string("unknown error"));
        partsNote_ = "parts_make: " + e;
        st.addLog("[err] parts_make: " + e);
        return false;
    }
    const std::string sp = res.value("step_path", stepPath);
    if (!insertComponent(st, sp, nullptr, aicmOut)) return false;
    partsNote_ = "Inserted " + res.value("name", fname) + ".";
    st.addLog("[asm] inserted catalog part: " + res.value("name", fname));
    return true;
}

void AsmPanels::drawPartsLibrary(AppState& st) {
    if (!partsLibOpen_) return;
    bool open = true;
    ui::setNextCommandDialogPlacement();   // first-open anchor over the full-width viewport
    ImGui::SetNextWindowSize(ImVec2(560 * ui::uiScale, 420 * ui::uiScale), ImGuiCond_Appearing);
    if (partsLibFocus_) { ImGui::SetNextWindowFocus(); partsLibFocus_ = false; }
    pushGlassWindow();
    const bool partsLibWin = ImGui::Begin("Standard Parts Library", &open);
    popGlassWindow();
    if (partsLibWin) {
        paintGlassWindow("asm_partslib_glass");
        asmIcon("insert_component", "[+]"); ImGui::SameLine(0, ui::metrics::asmHeaderIconGap());
        ImGui::TextUnformatted("Insert standard hardware (generated to spec)");
        ImGui::Spacing();

        // Lazy fetch the catalog from the core (synchronous; tiny payload).
        if (!partsCatalogFetched_) {
            nlohmann::json res;
            std::string err;
            if (st.worker.call("parts_catalog_list", nlohmann::json::object(), res, err) &&
                res.is_object() && res.contains("families")) {
                partsCatalog_ = res;
                partsCatalogError_.clear();
            } else {
                partsCatalogError_ = err.empty() ? "catalog unavailable" : err;
            }
            partsCatalogFetched_ = true;
        }

        if (!partsCatalogError_.empty()) {
            pushText(ui::palette.statusWarn);
            ImGui::TextWrapped("Could not load the parts catalog: %s", partsCatalogError_.c_str());
            ImGui::PopStyleColor();
            if (ImGui::Button("Retry")) partsCatalogFetched_ = false;
            endGlassWindow();
            ImGui::End();
            if (!open) partsLibOpen_ = false;
            return;
        }

        const nlohmann::json& fams =
            (partsCatalog_.is_object() && partsCatalog_["families"].is_array())
                ? partsCatalog_["families"]
                : nlohmann::json::array();

        if (!st.docMirror.hasDoc()) {
            pushText(ui::palette.statusWarn);
            ImGui::TextWrapped("No assembly open — use File > New Assembly to insert parts.");
            ImGui::PopStyleColor();
            ImGui::Spacing();
        }

        // Search filter over family names/categories.
        ImGui::SetNextItemWidth(220 * ui::uiScale);
        ImGui::InputTextWithHint("##parts_filter", "filter (e.g. screw, bearing, M3)",
                                 partsFilter_.data(), partsFilter_.size());
        std::string filt = partsFilter_.data();
        std::transform(filt.begin(), filt.end(), filt.begin(), ::tolower);

        ImGui::Spacing();
        ImGui::BeginChild("parts_list", ImVec2(220 * ui::uiScale, -1), true);
        std::string lastCat;
        for (int i = 0; i < (int)fams.size(); ++i) {
            const nlohmann::json& f = fams[i];
            const std::string name = f.value("name", std::string());
            const std::string cat = f.value("category", std::string());
            const std::string key = f.value("key", std::string());
            if (!filt.empty()) {
                std::string hay = name + " " + cat + " " + key;
                std::transform(hay.begin(), hay.end(), hay.begin(), ::tolower);
                // also match any size token (e.g. "m3")
                for (const auto& s : f.value("sizes", nlohmann::json::array())) {
                    std::string sv = s.get<std::string>();
                    std::transform(sv.begin(), sv.end(), sv.begin(), ::tolower);
                    hay += " " + sv;
                }
                if (hay.find(filt) == std::string::npos) continue;
            }
            if (cat != lastCat) {
                pushText(ui::palette.textDim);
                ImGui::TextUnformatted(cat.c_str());
                ImGui::PopStyleColor();
                lastCat = cat;
            }
            if (ImGui::Selectable((name + "##fam" + std::to_string(i)).c_str(),
                                  partsSelFamily_ == i)) {
                partsSelFamily_ = i;
                partsSelSize_ = 0;
                partsLenBuf_[0] = 0;
            }
        }
        ImGui::EndChild();

        ImGui::SameLine();
        ImGui::BeginChild("parts_detail", ImVec2(0, -1), false);
        if (partsSelFamily_ >= 0 && partsSelFamily_ < (int)fams.size()) {
            const nlohmann::json& f = fams[partsSelFamily_];
            ImGui::TextUnformatted(f.value("name", std::string("?")).c_str());
            pushText(ui::palette.textDim);
            ImGui::TextWrapped("%s", f.value("description", std::string()).c_str());
            ImGui::Text("Standard: %s", f.value("standard", std::string("-")).c_str());
            ImGui::PopStyleColor();
            ImGui::Spacing();

            const nlohmann::json sizes = f.value("sizes", nlohmann::json::array());
            if (partsSelSize_ >= (int)sizes.size()) partsSelSize_ = 0;
            const std::string curSize =
                sizes.empty() ? std::string() : sizes[partsSelSize_].get<std::string>();

            ImGui::TextUnformatted("Size");
            ImGui::SetNextItemWidth(160 * ui::uiScale);
            if (ImGui::BeginCombo("##parts_size", curSize.c_str())) {
                for (int s = 0; s < (int)sizes.size(); ++s) {
                    const std::string sv = sizes[s].get<std::string>();
                    if (ImGui::Selectable(sv.c_str(), partsSelSize_ == s)) partsSelSize_ = s;
                }
                ImGui::EndCombo();
            }

            const bool needsLen = f.value("needs_length", false);
            double lenVal = 0.0;
            if (needsLen) {
                ImGui::TextUnformatted("Length (mm)");
                ImGui::SetNextItemWidth(160 * ui::uiScale);
                ImGui::InputText("##parts_len", partsLenBuf_.data(), partsLenBuf_.size(),
                                 ImGuiInputTextFlags_CharsDecimal);
                const nlohmann::json lens = f.value("lengths_mm", nlohmann::json::array());
                if (!lens.empty()) {
                    ImGui::SameLine();
                    if (ImGui::BeginCombo("##parts_len_menu", "common", ImGuiComboFlags_NoPreview)) {
                        for (const auto& L : lens) {
                            const int lv = (int)L.get<double>();
                            if (ImGui::Selectable((std::to_string(lv) + " mm").c_str()))
                                std::snprintf(partsLenBuf_.data(), partsLenBuf_.size(), "%d", lv);
                        }
                        ImGui::EndCombo();
                    }
                }
                lenVal = std::atof(partsLenBuf_.data());
            }

            ImGui::Spacing();
            const bool canInsert = st.docMirror.hasDoc() && !curSize.empty() &&
                                   (!needsLen || lenVal > 0.0);
            if (!canInsert) ImGui::BeginDisabled();
            if (ImGui::Button("Insert into Assembly", ImVec2(180 * ui::uiScale, 0))) {
                std::string aicm;
                insertCatalogPart(st, f.value("key", std::string()), curSize, lenVal, aicm);
            }
            if (!canInsert) ImGui::EndDisabled();
            if (needsLen && lenVal <= 0.0) {
                pushText(ui::palette.textDim);
                ImGui::TextUnformatted("Enter or pick a length.");
                ImGui::PopStyleColor();
            }
        } else {
            pushText(ui::palette.textDim);
            ImGui::TextWrapped("Select a part family on the left.");
            ImGui::PopStyleColor();
        }
        if (!partsNote_.empty()) {
            ImGui::Spacing();
            pushText(ui::palette.textDim);
            ImGui::TextWrapped("%s", partsNote_.c_str());
            ImGui::PopStyleColor();
        }
        ImGui::EndChild();
        endGlassWindow();
    }
    ImGui::End();
    if (!open) partsLibOpen_ = false;
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

void AsmPanels::closeMateForm() { mateOpen_ = false; }

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

void AsmPanels::drawMateForm(AppState& st) {
    if (!mateOpen_) return;
    const auto& pal = ui::palette;
    bool open = true;
    // Place near the top-left of the work area on first appearance so the now-taller form (Standard
    // + Mechanical/Advanced + param block) is fully on-screen, and cap the height to the viewport so
    // overflow scrolls inside the window instead of clipping off the bottom edge.
    const ImGuiViewport* vp = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(ImVec2(vp->WorkPos.x + 24 * ui::uiScale,
                                   vp->WorkPos.y + 96 * ui::uiScale), ImGuiCond_Appearing);
    ImGui::SetNextWindowSize(ImVec2(320 * ui::uiScale, 0), ImGuiCond_Appearing);  // W1-B (item 10): DPI
    ImGui::SetNextWindowSizeConstraints(ImVec2(0, 0),
                                        ImVec2(FLT_MAX, vp->WorkSize.y - 120 * ui::uiScale));
    if (mateFocus_) { ImGui::SetNextWindowFocus(); mateFocus_ = false; }
    pushGlassWindow();
    const bool mateWin = ImGui::Begin("Mate##asm", &open);
    popGlassWindow();
    if (mateWin) {
        paintGlassWindow("asm_mate_glass");
        // header: icon + title + Add / Cancel (PM style)
        asmIcon("mate", "[M]"); ImGui::SameLine(0, ui::metrics::asmHeaderIconGap());
        pushText(pal.text);
        ImGui::TextUnformatted("Mate");
        ImGui::PopStyleColor();
        ImGui::Separator();

        // The effective type drives the well count + the parameter block + the Add gate.
        std::string type = effectiveMateType();
        const int wellCount = typeNeedsThirdRef(type) ? 3 : 2;
        if (activeWell_ >= wellCount) activeWell_ = 0;

        // ---- Mate Selections (two or three wells) ----
        ImGui::SeparatorText("Mate Selections");
        static const char* const kWellHint[3] = {
            "(pick a face/edge)", "(pick a face/edge)", "(pick the jaw / symmetry plane)"};
        for (int i = 0; i < wellCount; ++i) {
            bool active = (activeWell_ == i);
            pushText(active ? pal.accent : pal.textDim);
            ImGui::Text("Reference %d", i + 1);
            ImGui::PopStyleColor();
            ImGui::SameLine();
            // The well: a selectable that, when clicked, becomes the active pick target.
            std::string cell = picks_[i].has ? picks_[i].label : std::string(kWellHint[i]);
            ImVec4 wellBg = active
                ? ImVec4(pal.accentSoft[0], pal.accentSoft[1], pal.accentSoft[2], 0.6f)
                : ImVec4(pal.frameBg[0], pal.frameBg[1], pal.frameBg[2], 1.0f);
            ImGui::PushStyleColor(ImGuiCol_Header, wellBg);
            ImGui::PushStyleColor(ImGuiCol_HeaderHovered, wellBg);
            if (ImGui::Selectable(("##well" + std::to_string(i)).c_str(), active,
                                  ImGuiSelectableFlags_None, ImVec2(-1, 0)))
                activeWell_ = i;
            ImGui::PopStyleColor(2);
            ImGui::SameLine(0, 0);
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() + 6);
            pushText(picks_[i].has ? pal.text : pal.textDim);
            ImGui::TextUnformatted(cell.c_str());
            ImGui::PopStyleColor();
            // right-click clears
            if (ImGui::IsItemClicked(ImGuiMouseButton_Right)) { picks_[i] = MatePick{}; activeWell_ = i; }
        }

        ImGui::Spacing();
        // ---- Standard Mates (auto-suggested type buttons) ----
        ImGui::SeparatorText("Standard Mates");
        bool bothPlanes = picks_[0].surf == MateSurfKind::Plane && picks_[1].surf == MateSurfKind::Plane;
        bool bothCyl    = picks_[0].surf == MateSurfKind::Cylinder && picks_[1].surf == MateSurfKind::Cylinder;
        const char* const* opts; int nopts;
        if (bothPlanes)   { opts = kPlanePlane; nopts = 3; }
        else if (bothCyl) { opts = kCylCyl;     nopts = 1; }
        else              { opts = kGeneric;    nopts = 4; }
        if (mateType_ >= nopts) mateType_ = 0;
        // A standard button is highlighted only when the explicit selection matches its label
        // (selecting a Mechanical/Advanced type deselects the standard row).
        for (int i = 0; i < nopts; ++i) {
            if (i) ImGui::SameLine();
            bool sel = selectedMateType_.empty()
                ? (mateType_ == i)               // surface-suggest mode: highlight the cycled index
                : (selectedMateType_ == opts[i]); // explicit mode: highlight the chosen standard type
            if (sel) ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1.0f));
            std::string label = opts[i];
            if (!label.empty()) label[0] = (char)std::toupper((unsigned char)label[0]);
            if (ImGui::Button((label + "##mt" + std::to_string(i)).c_str())) {
                mateType_ = i;
                selectedMateType_ = opts[i];   // standard buttons now also stamp the explicit type
            }
            if (sel) ImGui::PopStyleColor();
        }

        // ---- Mechanical & Advanced Mates ----
        ImGui::Spacing();
        ImGui::SeparatorText("Mechanical & Advanced Mates");
        struct AdvOpt { const char* label; const char* type; };
        // Order groups mechanical (coupling), then limit, then advanced (geometry / 3rd-ref).
        static const AdvOpt kAdvOpts[] = {
            {"Gear",           "gear"},
            {"Screw",          "screw"},
            {"Rack & Pinion",  "rack_pinion"},
            {"Distance Limit", "distance_limit"},
            {"Angle Limit",    "angle_limit"},
            {"Hinge",          "hinge"},
            {"Slot",           "slot"},
            {"Cam Follower",   "cam_follower"},
            {"Profile Center", "profile_center"},
            {"Width",          "width"},
            {"Symmetric",      "symmetric"},
        };
        const float availW = ImGui::GetContentRegionAvail().x;
        for (int i = 0; i < (int)(sizeof(kAdvOpts) / sizeof(kAdvOpts[0])); ++i) {
            bool sel = (selectedMateType_ == kAdvOpts[i].type);
            // Wrap onto a new row when the next button would overflow the form width.
            if (i) {
                float next = ImGui::GetItemRectMax().x - ImGui::GetWindowPos().x +
                             ImGui::GetStyle().ItemSpacing.x +
                             ImGui::CalcTextSize(kAdvOpts[i].label).x +
                             ImGui::GetStyle().FramePadding.x * 2.0f;
                if (next <= availW) ImGui::SameLine();
            }
            if (sel) ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1.0f));
            if (ImGui::Button((std::string(kAdvOpts[i].label) + "##adv" + std::to_string(i)).c_str())) {
                selectedMateType_ = kAdvOpts[i].type;
                mateError_.clear();
                if (activeWell_ >= (typeNeedsThirdRef(selectedMateType_) ? 3 : 2)) activeWell_ = 0;
            }
            if (sel) ImGui::PopStyleColor();
        }
        // Re-read the effective type (a button click this frame may have changed it).
        type = effectiveMateType();

        // ---- Align flip (SolidWorks "Aligned" / "Anti-Aligned") ----
        ImGui::Spacing();
        ImGui::TextUnformatted("Mate alignment:");
        if (ImGui::RadioButton("Aligned", aligned_)) aligned_ = true;
        ImGui::SameLine();
        if (ImGui::RadioButton("Anti-Aligned", !aligned_)) aligned_ = false;
        pushText(pal.textDim);
        ImGui::TextWrapped("Seating one face flat onto another is Anti-Aligned (the normals oppose).");
        ImGui::PopStyleColor();

        // ---- type-aware parameter block ----
        // A labelled numeric field at the assembly input width. angle=true for degree-valued fields.
        auto field = [](const char* label, std::vector<char>& buf, const char* id) {
            ImGui::TextUnformatted(label);
            ImGui::SetNextItemWidth(ui::metrics::asmInputW());
            ImGui::InputText(id, buf.data(), buf.size());
        };
        bool paramsOk = true;  // gates the Add button for types with REQUIRED params
        ImGui::Spacing();
        if (type == "distance" || type == "angle") {
            field(type == "distance" ? "Distance:" : "Angle (deg):", dimBuf_, "##asm_mate_dim");
        } else if (type == "distance_limit" || type == "angle_limit") {
            const bool angle = (type == "angle_limit");
            field(angle ? "Min angle (deg):" : "Min distance (mm):", minBuf_, "##asm_mate_min");
            field(angle ? "Max angle (deg):" : "Max distance (mm):", maxBuf_, "##asm_mate_max");
            paramsOk = minBuf_[0] && maxBuf_[0];  // both required
        } else if (type == "gear") {
            field("Ratio:", ratioBuf_, "##asm_mate_ratio");
            paramsOk = ratioBuf_[0];
        } else if (type == "screw") {
            field("Pitch (mm/rev):", pitchBuf_, "##asm_mate_pitch");
            paramsOk = pitchBuf_[0];
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
            // width / symmetric / slot / cam_follower / profile_center carry no params.
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

        ImGui::Separator();
        // Gate on the required reference count (3 for width/symmetric, else 2) AND required params.
        bool refsReady = true;
        for (int i = 0; i < wellCount; ++i) if (!picks_[i].has) refsReady = false;
        bool ready = refsReady && paramsOk;
        if (!ready) ImGui::BeginDisabled();
        if (ImGui::Button("Add##asm_mate", ImVec2(ui::metrics::asmBtnW(), 0))) {
            nlohmann::json refs = nlohmann::json::array();
            for (int i = 0; i < wellCount; ++i)
                refs.push_back({{"component", picks_[i].component}, {"selector", picks_[i].selector}});
            std::string aicm;
            if (addMate(st, type, aligned_ ? "aligned" : "anti", refs, aicm)) {
                st.addLog("[asm] mate added: " + type + " (" + (aligned_ ? "aligned" : "anti") + ")");
                // keep the form open for chaining more mates, reset selections
                picks_[0] = MatePick{}; picks_[1] = MatePick{}; picks_[2] = MatePick{};
                activeWell_ = 0; mateError_.clear();
            }
        }
        if (!ready) ImGui::EndDisabled();
        ImGui::SameLine();
        if (ImGui::Button("Close##asm_mate", ImVec2(ui::metrics::asmBtnW(), 0))) closeMateForm();
        endGlassWindow();
    }
    ImGui::End();
    if (!open) closeMateForm();
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

void AsmPanels::drawPatternForm(AppState& st) {
    if (!patternOpen_) return;
    const auto& pal = ui::palette;
    bool open = true;
    ui::setNextCommandDialogPlacement();   // first-open anchor over the full-width viewport
    ImGui::SetNextWindowSize(ImVec2(340 * ui::uiScale, 0), ImGuiCond_Appearing);
    if (patternFocus_) { ImGui::SetNextWindowFocus(); patternFocus_ = false; }
    pushGlassWindow();
    const bool patternWin = ImGui::Begin("Component Pattern##asm", &open);
    popGlassWindow();
    if (patternWin) {
        paintGlassWindow("asm_pattern_glass");
        asmIcon("pattern_component", "[#]"); ImGui::SameLine(0, ui::metrics::asmHeaderIconGap());
        pushText(pal.text);
        ImGui::TextUnformatted("Component Pattern");
        ImGui::PopStyleColor();
        ImGui::Separator();

        if (!st.docMirror.hasDoc()) {
            pushText(pal.statusWarn);
            ImGui::TextWrapped("No assembly open.");
            ImGui::PopStyleColor();
            endGlassWindow();
            ImGui::End();
            if (!open) patternOpen_ = false;
            return;
        }

        // ---- Pattern type ----
        ImGui::TextUnformatted("Type:");
        ImGui::SameLine();
        if (ImGui::RadioButton("Linear", patternKind_ == 0)) patternKind_ = 0;
        ImGui::SameLine();
        if (ImGui::RadioButton("Circular", patternKind_ == 1)) patternKind_ = 1;

        // ---- Direction / Axis (world/datum) ----
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

        // ---- Count + spacing/angle ----
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

        // ---- Seed components (filled by viewport / tree picks while this form is open) ----
        ImGui::Spacing();
        ImGui::SeparatorText("Components to Pattern");
        if (patternComps_.empty()) {
            pushText(pal.textDim);
            ImGui::TextWrapped("Click components in the graphics area or the tree to add them.");
            ImGui::PopStyleColor();
        } else {
            for (size_t i = 0; i < patternComps_.size(); ++i) {
                ImGui::Bullet();
                ImGui::TextUnformatted(componentName(st, patternComps_[i]).c_str());
                ImGui::SameLine(ImGui::GetWindowWidth() - 40);
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

        ImGui::Separator();
        bool ready = !patternComps_.empty();
        if (!ready) ImGui::BeginDisabled();
        if (ImGui::Button("Add##asm_pat", ImVec2(ui::metrics::asmBtnW(), 0))) {
            std::string aicm, err;
            if (addPattern(st, aicm, err)) {
                st.addLog("[asm] component pattern added (" +
                          std::string(patternKind_ == 0 ? "linear" : "circular") + ", " +
                          std::to_string(patternComps_.size()) + " seed(s))");
                patternError_.clear();
                // keep the form open for chaining; clear the seed picks
                patternComps_.clear();
            } else {
                patternError_ = err;
                st.addLog("[err] asm_pattern: " + err);
            }
        }
        if (!ready) ImGui::EndDisabled();
        ImGui::SameLine();
        if (ImGui::Button("Close##asm_pat", ImVec2(ui::metrics::asmBtnW(), 0))) patternOpen_ = false;
        endGlassWindow();
    }
    ImGui::End();
    if (!open) patternOpen_ = false;
}

// ===========================================================================
// Exploded View
// ===========================================================================

bool AsmPanels::setExplode(AppState& st, bool active, float factor) {
    if (!st.docMirror.hasDoc()) return false;
    nlohmann::json res; std::string err;
    nlohmann::json params = {{"doc_id", st.docMirror.docId()},
                             {"active", active}, {"factor", factor}};
    if (!st.worker.call("doc_set_explode_state", params, res, err)) {
        st.addLog("[err] explode: " + err); return false;
    }
    if (res.is_object() && res.contains("code")) {
        st.addLog("[err] explode: " + res.value("message", std::string())); return false;
    }
    // View-only: the engine re-tessellated IN PLACE — reload the (same) aicm path it returns.
    std::string aicm;
    if (res.contains("tess") && res["tess"].is_object())
        aicm = res["tess"].value("aicm_path", std::string());
    if (!aicm.empty()) { std::string le; st.loadDocMesh(aicm, le); }
    explodeActive_ = active;
    explodeFactor_ = factor;
    return true;
}

void AsmPanels::drawExplode(AppState& st) {
    if (!explodeOpen_) return;
    bool open = true;
    ui::setNextCommandDialogPlacement();   // first-open anchor over the full-width viewport
    ImGui::SetNextWindowSize(ImVec2(300 * ui::uiScale, 0), ImGuiCond_Appearing);  // W1-B (item 10): DPI
    if (explodeFocus_) { ImGui::SetNextWindowFocus(); explodeFocus_ = false; }
    pushGlassWindow();
    const bool explodeWin = ImGui::Begin("Exploded View", &open);
    popGlassWindow();
    if (explodeWin) {
        paintGlassWindow("asm_explode_glass");
        asmIcon("exploded_view", "[X]"); ImGui::SameLine(0, ui::metrics::asmHeaderIconGap());
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
        if (ImGui::Button("Collapse", ImVec2(ui::metrics::asmBtnW(), 0))) { explodeFactor_ = 0.0f; setExplode(st, false, 0.0f); }
        ImGui::SameLine();
        if (ImGui::Button("Exploded", ImVec2(ui::metrics::asmBtnW(), 0))) { explodeFactor_ = 1.0f; setExplode(st, true, 1.0f); }
        endGlassWindow();
    }
    ImGui::End();
    if (!open) explodeOpen_ = false;
}

// ===========================================================================
// Interference Detection
// ===========================================================================

// C39: async — pairwise boolean intersections scale with component count and used to freeze
// the window for up to 60s. The window opens immediately showing "Computing..."; the result
// fills in when the call drains (GL thread). A re-refresh while pending supersedes (gen).
void AsmPanels::refreshInterference(AppState& st) {
    interferenceError_.clear();
    interferenceResult_ = nlohmann::json::object();
    interferencePending_ = false;
    if (!st.docMirror.hasDoc()) { interferenceError_ = "No assembly open."; return; }
    interferencePending_ = true;
    const int gen = ++interferenceGen_;
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

void AsmPanels::drawInterference(AppState& st) {
    if (!interferenceOpen_) return;
    const auto& pal = ui::palette;
    bool open = true;
    ui::setNextCommandDialogPlacement();   // first-open anchor over the full-width viewport
    ImGui::SetNextWindowSize(ImVec2(420 * ui::uiScale, 0), ImGuiCond_Appearing);  // W1-B (item 10): DPI
    if (interferenceFocus_) { ImGui::SetNextWindowFocus(); interferenceFocus_ = false; }
    pushGlassWindow();
    const bool interfWin = ImGui::Begin("Interference Detection", &open);
    popGlassWindow();
    if (interfWin) {
        paintGlassWindow("asm_interference_glass");
        asmIcon("interference", "[!]"); ImGui::SameLine(0, ui::metrics::asmHeaderIconGap());
        ImGui::TextUnformatted("Component interference + clearance");
        ImGui::SameLine(ImGui::GetWindowWidth() - 80);
        if (ImGui::Button("Refresh")) refreshInterference(st);
        ImGui::Separator();

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
            pushText(clash ? pal.statusErr : pal.statusOk);
            ImGui::TextUnformatted(clash ? "Interference detected" : "No interference");
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
                    ImGui::TableNextColumn(); ImGui::Text("%.3f mm gap", it.value("distance", 0.0));
                }
                ImGui::EndTable();
            }
            if (inter.empty() && clear.empty()) {
                pushText(pal.textDim);
                ImGui::TextWrapped("Fewer than two components, or nothing to compare.");
                ImGui::PopStyleColor();
            }
        }
        endGlassWindow();
    }
    ImGui::End();
    if (!open) interferenceOpen_ = false;
}

// ===========================================================================
// BOM (Bill of Materials)
// ===========================================================================

void AsmPanels::refreshBom(AppState& st) {
    bomRows_.clear();
    bomNote_.clear();
    bomPending_ = 0;
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
    ui::setNextCommandDialogPlacement();   // first-open anchor over the full-width viewport
    ImGui::SetNextWindowSize(ImVec2(560 * ui::uiScale, 0), ImGuiCond_Appearing);  // W1-B (item 10): DPI
    if (bomFocus_) { ImGui::SetNextWindowFocus(); bomFocus_ = false; }
    pushGlassWindow();
    const bool bomWin = ImGui::Begin("Bill of Materials", &open);
    popGlassWindow();
    if (bomWin) {
        paintGlassWindow("asm_bom_glass");
        asmIcon("bom", "[B]"); ImGui::SameLine(0, ui::metrics::asmHeaderIconGap());
        ImGui::TextUnformatted("Components");
        ImGui::SameLine(ImGui::GetWindowWidth() - 80);
        if (ImGui::Button("Refresh")) refreshBom(st);
        ImGui::Separator();

        if (!bomNote_.empty()) {
            pushText(ui::palette.textDim);
            ImGui::TextWrapped("%s", bomNote_.c_str());
            ImGui::PopStyleColor();
        }
        if (ImGui::BeginTable("##bom_tbl", 5,
            ImGuiTableFlags_Borders | ImGuiTableFlags_RowBg | ImGuiTableFlags_SizingStretchProp)) {
            ImGui::TableSetupColumn("#", ImGuiTableColumnFlags_WidthFixed, 28);
            ImGui::TableSetupColumn("Part");
            ImGui::TableSetupColumn("Source");
            ImGui::TableSetupColumn("Qty", ImGuiTableColumnFlags_WidthFixed, 40);
            ImGui::TableSetupColumn("Mass (g)", ImGuiTableColumnFlags_WidthFixed, 90);
            ImGui::TableHeadersRow();
            int n = 1;
            for (const auto& row : bomRows_) {
                ImGui::TableNextRow();
                ImGui::TableNextColumn(); ImGui::Text("%d", n++);
                ImGui::TableNextColumn(); ImGui::TextUnformatted(row.name.c_str());
                ImGui::TableNextColumn();
                // show just the file stem to keep the column readable; full path on hover
                std::string src = row.source;
                std::string stem = src;
                size_t slash = stem.find_last_of("/\\");
                if (slash != std::string::npos) stem = stem.substr(slash + 1);
                ImGui::TextUnformatted(stem.c_str());
                if (ImGui::IsItemHovered() && !src.empty()) ImGui::SetTooltip("%s", src.c_str());
                ImGui::TableNextColumn(); ImGui::Text("%d", row.qty);
                ImGui::TableNextColumn();
                if (row.haveMass) ImGui::Text("%.2f", row.massG * row.qty);
                else if (bomPending_ > 0) ImGui::TextDisabled("...");  // C39: eval_mass in flight
                else ImGui::Text("%.0f mm3", row.volMm3 * row.qty);
            }
            ImGui::EndTable();
        }

        ImGui::Spacing();
        ImGui::TextUnformatted("Export CSV to:");
        ImGui::SetNextItemWidth(-ui::metrics::asmBtnW());
        ImGui::InputText("##bom_csv", bomCsvPath_.data(), bomCsvPath_.size());
        ImGui::SameLine();
        if (ImGui::Button("Export", ImVec2(ui::metrics::px(80), 0)) && bomCsvPath_[0]) {
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
        endGlassWindow();
    }
    ImGui::End();
    if (!open) bomOpen_ = false;
}

}  // namespace ui
