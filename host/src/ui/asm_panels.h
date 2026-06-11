// Assembly UI panels (C20b) — SolidWorks-style assembly workflow on the C20 engine.
//
// One AsmPanels object owns every assembly-mode window/form and the ribbon dispatch for the
// Assembly tab. It drives the LIVE worker through core::DocMirror (assembly doc), mirroring the
// verified engine flow in engine/doc/test_worker_asm.py:
//
//   - Insert Component:  a path input + "Insert" (asm_insert_component). The first component is
//                        auto-fixed by the engine (fixed = first). Re-tessellates -> viewport.
//   - Mate PM form:      C17 PropertyManager visual style. Two SelectorWells fed by viewport picks
//                        resolved via doc_resolve_pick (assembly picks return {component, selector};
//                        the component id is taken straight from the resolve response). Mate-type
//                        buttons auto-suggested from the two picked surfaces (plane+plane ->
//                        coincident/parallel/distance, cyl+cyl -> concentric). An Align (Aligned /
//                        Anti-Aligned) flip toggle, SolidWorks-labelled. A distance/angle DimField
//                        for the dimension mates. "Add" emits asm_add_mate then doc_solve_mates.
//   - Exploded View:     a factor slider (doc_set_explode_state {active, factor}); view-only, the
//                        engine re-tessellates IN PLACE, so we reload the SAME aicm path after each
//                        call.
//   - Interference:      eval_interference -> a window listing pairs; red statusErr rows on overlap,
//                        clearance rows otherwise.
//   - BOM:               components from the doc tree (name, source path, qty rollup by source,
//                        mass via eval_mass per component); CSV export to a typed path.
//
// Move Component: a viewport drag of a selected component is a transform preview + doc_solve_mates
// on mouse release (the simplest correct path; see asm_panels.cpp Move-Component note).
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

struct AppState;

namespace ui {

// Which mate kinds the auto-suggest offers given the two picked surfaces.
enum class MateSurfKind { None, Plane, Cylinder, Other };

class AsmPanels {
public:
    // Ribbon dispatch for the Assembly tab (and File > New Assembly). Returns true if consumed.
    // cmdId: "asm_insert" | "asm_mate" | "asm_move" | "asm_pattern" | "asm_explode" |
    //        "asm_bom" | "asm_interference" | "new_assembly".
    bool handleCommand(AppState& st, const std::string& cmdId);

    // Draw every open assembly window/form. Call once per frame after the other panels.
    void draw(AppState& st);

    // Feed a resolved assembly pick into the active mate well. Called from the selection observer
    // when the Mate form is open. Returns true if a well consumed the pick.
    bool routeAsmPick(const nlohmann::json& selector, const std::string& component, int pickKind);

    // True while the Mate form is open (the selection observer routes picks to it then).
    bool mateFormActive() const { return mateOpen_; }

    // --- scripted scenario helpers (AI_CAD_SHOT=asm) ---
    void openInsert()       { insertOpen_ = true; insertFocus_ = true; }
    void openMateForm()     { beginMateForm(); }
    void openInterference(AppState& st) { handleCommand(st, "asm_interference"); }
    void openBom(AppState& st)          { handleCommand(st, "asm_bom"); }
    // Insert a component by path directly (scenario / programmatic). Returns the new aicm path.
    bool insertComponent(AppState& st, const std::string& path,
                         const nlohmann::json& transform, std::string& aicmOut);
    // Add a mate directly (scenario). type/align per doc_schema; refs already shaped
    // [{component, selector}, ...]. Solves after. Returns the solved aicm path.
    bool addMate(AppState& st, const std::string& type, const std::string& align,
                 const nlohmann::json& refs, std::string& aicmOut);
    // Set the explode factor (0..1); reloads the re-tessellated mesh in place.
    bool setExplode(AppState& st, bool active, float factor);
    // Close transient windows (scenario housekeeping so only the intended window is on top).
    void closeInsert()  { insertOpen_ = false; }
    void closeExplode() { explodeOpen_ = false; }

    // Close the mate form (public so scripted scenarios can dismiss it before other shots).
    void closeMateForm();

private:
    void beginMateForm();
    void drawInsert(AppState& st);
    void drawMateForm(AppState& st);
    void drawExplode(AppState& st);
    void drawInterference(AppState& st);
    void drawBom(AppState& st);

    void refreshInterference(AppState& st);
    void refreshBom(AppState& st);
    const char* suggestedMate() const;   // from the two picked surfaces

    // Windows open behind the docked viewport unless focused on the rising edge of "open"; each
    // window sets its *Focus_ flag when opened and consumes it (SetNextWindowFocus once) in draw.

    // ---- Insert ----
    bool insertOpen_ = false, insertFocus_ = false;
    std::vector<char> insertPath_{std::vector<char>(512, 0)};

    // ---- Mate form (PM-style) ----
    bool mateOpen_ = false, mateFocus_ = false;
    int  activeWell_ = 0;                 // 0 or 1 — which well receives the next pick
    struct MatePick {
        bool has = false;
        nlohmann::json selector;
        std::string component;            // component k-id
        std::string label;                // chip text ("base · Face")
        MateSurfKind surf = MateSurfKind::None;
    } picks_[2];
    int  mateType_ = 0;                   // index into the auto-suggested type list
    bool aligned_ = true;                 // Align: true = Aligned, false = Anti-Aligned
    std::vector<char> dimBuf_{std::vector<char>(64, 0)};  // distance/angle value
    std::string mateError_;

    // ---- Explode ----
    bool explodeOpen_ = false, explodeFocus_ = false;
    bool explodeActive_ = false;
    float explodeFactor_ = 0.0f;

    // ---- Interference ----
    bool interferenceOpen_ = false, interferenceFocus_ = false;
    nlohmann::json interferenceResult_;
    std::string interferenceError_;

    // ---- BOM ----
    bool bomOpen_ = false, bomFocus_ = false;
    struct BomRow { std::string name, source; int qty = 0; double massG = 0; double volMm3 = 0; bool haveMass = false; };
    std::vector<BomRow> bomRows_;
    std::vector<char> bomCsvPath_{std::vector<char>(512, 0)};
    std::string bomNote_;
};

}  // namespace ui
