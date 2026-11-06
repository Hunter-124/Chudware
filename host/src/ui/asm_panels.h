// Assembly UI panels (C20b) — SolidWorks-style assembly workflow on the C20 engine.
//
// One AsmPanels object owns every assembly-mode window/form and the ribbon dispatch for the
// Assembly tab. It drives the LIVE worker through core::DocMirror (assembly doc), mirroring the
// verified engine flow in engine/doc/test_worker_asm.py:
//
//   - Insert Component:  open/recent-parts chooser + Browse… (asm_insert_component). The first
//                        component is auto-fixed by the engine (fixed = first). Re-tessellates -> viewport.
//   - Mate PM form:      C17 PropertyManager visual style. Two SelectorWells fed by viewport picks
//                        resolved via doc_resolve_pick (assembly picks return {component, selector};
//                        the component id is taken straight from the resolve response). Mate-type
//                        buttons auto-suggested from the two picked surfaces (plane+plane ->
//                        coincident/parallel/distance, cyl+cyl -> concentric). An Align (Aligned /
//                        Anti-Aligned) flip toggle, SolidWorks-labelled. A distance/angle DimField
//                        for the dimension mates. "Add" emits asm_add_mate then doc_solve_mates.
//   - Exploded View:     a factor slider + Collapse/Exploded (eased) buttons. The engine returns
//                        each moving component's factor-1 vector (authored steps, else its
//                        automatic plan); the viewport slides bodies on the GPU at frame rate and
//                        commits doc_set_explode_state once per gesture. Dashed explode lines
//                        run from each part's home to its current position.
//   - Interference:      eval_interference -> a window listing pairs; red statusErr rows on overlap,
//                        clearance rows otherwise.
//   - BOM:               components from the doc tree (name, source path, qty rollup by source,
//                        mass via eval_mass per component); CSV export to a typed path.
//
// Move Component: a viewport drag of a selected component is a transform preview + doc_solve_mates
// on mouse release (the simplest correct path; see asm_panels.cpp Move-Component note).
#pragma once

#include <array>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

struct AppState;

namespace ui {

// Which mate kinds the auto-suggest offers given the two picked surfaces.
enum class MateSurfKind { None, Plane, Cylinder, Other };

class AsmPanels {
public:
    enum class InsertOutcome { Success, Failure, Superseded };
    using InsertCompletion = std::function<void(InsertOutcome, const std::string&)>;

    // Ribbon dispatch for the Assembly tab (and File > New Assembly). Returns true if consumed.
    // cmdId: "asm_insert" | "asm_parts_library" | "asm_mate" | "asm_move" | "asm_pattern" |
    //        "asm_explode" | "asm_bom" | "asm_interference" | "new_assembly".
    bool handleCommand(AppState& st, const std::string& cmdId);

    // Draw BOM (floating) + service any pending tree "Edit" mate requests. Mate/Insert/Pattern/
    // Explode/Interference now render as PropertyManager guest forms in the left column (openGuest);
    // their bodies are driven by the PM while open. Call once per frame after the other panels.
    void draw(AppState& st);

    // True while any left-pane assembly tool form is open (Mate/Insert/Pattern/Explode/Interference).
    // layout.cpp can OR this with pm->active() if needed; currently openGuest makes pm->active() true.
    bool pmFormActive() const {
        return mateOpen_ || insertOpen_ || patternOpen_ || explodeOpen_ || interferenceOpen_;
    }

    // Disarm document-bound assembly tools before switching or closing their tab.
    void closeAllToolForms(AppState& st);
    // Feed a resolved assembly pick into the active mate well. Called from the selection observer
    // when the Mate form is open. Returns true if a well consumed the pick.
    bool routeAsmPick(const nlohmann::json& selector, const std::string& component, uint32_t bodyId);

    // Feed a picked COMPONENT id into the active Component Pattern form's component list (toggles
    // membership). Called from the selection observer when the Pattern form is open. Returns true
    // if the form consumed the pick.
    bool routeAsmPatternPick(const std::string& component);

    // True while the Mate form is open (the selection observer routes picks to it then).
    bool mateFormActive() const { return mateOpen_; }
    // Bodies already in the active mate wells are shown translucent and can be picked through.
    std::array<uint32_t, 3> matePickedBodies() const;
    // True while the Component Pattern form is open (the selection observer routes component picks).
    bool patternFormActive() const { return patternOpen_; }

    // --- scripted scenario helpers (CHUDWARE_SHOT=asm) ---
    // Scenario/shot helper — opens state only. Prefer handleCommand(st,"asm_mate") so the form
    // hosts in the left PM column (openPmGuest). Callers with AppState should use handleCommand.
    void openMateForm() { beginMateForm(); }
    // Preselect a mate type for a headless shot (CHUDWARE_MATETYPE=gear), so the type-aware
    // parameter block renders. Mirrors PropertyManager::presetFieldForShot. No-op if the type
    // is unknown; the mate form must already be open.
    void presetMateTypeForShot(const std::string& type);
    void openInterference(AppState& st) { handleCommand(st, "asm_interference"); }
    // Async completion distinguishes an engine failure from an obsolete presentation.
    // targetDocId overrides the insert dialog's sticky target for Parts Library.
    bool insertComponent(AppState& st, const std::string& path,
                         const nlohmann::json& transform, std::string& aicmOut,
                         InsertCompletion onComplete = {},
                         const std::string& targetDocId = {});
    // Wait for the selected target assembly's mirror to bind. Switching away supersedes
    // the presentation rather than selecting that assembly on the user's behalf.
    void insertComponentWhenReady(AppState& st, const std::string& path,
                                  const std::string& targetDocId,
                                  InsertCompletion onComplete);
    // Add a mate directly (scenario). type/align per doc_schema; refs already shaped
    // [{component, selector}, ...]. Solves after. Returns the solved aicm path.
    bool addMate(AppState& st, const std::string& type, const std::string& align,
                 const nlohmann::json& refs, std::string& aicmOut);
    // Jump the explode view to `factor` (0..1; collapsed when !active) and commit it.
    bool setExplode(AppState& st, bool active, float factor);
    // Close transient windows (scenario housekeeping so only the intended window is on top).
    void closeInsert()  {
        insertOpen_ = false; insertSel_ = -1; insertChosenPath_.clear(); insertNote_.clear();
    }
    void closeExplode() { explodeOpen_ = false; }
    // Ease the explode view to `target` (the Collapse/Exploded buttons), framing the camera on
    // where the parts are heading while they move; bakes when it settles.
    void animateExplodeTo(AppState& st, float target);
    // Slider drag: show `factor` on the GPU without an engine call; releaseExplode bakes it.
    void previewExplode(float factor);
    void releaseExplode(AppState& st) { commitExplode(st, explodeShown_); }
    bool explodeAnimating() const { return explodeAnimating_; }
    size_t explodeComponentCount() const { return explodeComps_.size(); }
    // Per frame (from draw): advance the explode animation, track which factor the uploaded mesh
    // carries, and feed the renderer the GPU offsets that bridge it to the factor on screen.
    void tickExplode(AppState& st);
    // Dashed explode lines (home -> current position) into the bound overlay FBO.
    void drawExplodeLines(AppState& st);
    // The viewport holds auto-framing while this document's explode view is open or exploded,
    // so the camera never jumps when the baked exploded mesh replaces the GPU-offset frame.
    bool explodeHoldsFraming(const AppState& st) const;

    // Close the mate form (public so scripted scenarios can dismiss it before other shots).
    void closeMateForm();

private:
    void beginMateForm();
    void beginPatternForm();
    // Open/replace the left-pane PM guest for the named tool (mutual-excludes other asm tools).
    void openPmGuest(AppState& st, const char* which);
    void drawInsertBody(AppState& st);
    void drawMateFormBody(AppState& st);
    void drawPatternFormBody(AppState& st);
    void drawExplodeBody(AppState& st);
    void drawInterferenceBody(AppState& st);
    void drawBom(AppState& st);  // stays floating
    // Emit the component-pattern add_feature op + reload. Returns the solved aicm path (empty on
    // error, errorOut filled).
    bool addPattern(AppState& st, std::string& aicmOut, std::string& errorOut);

    void refreshInterference(AppState& st);
    void refreshBom(AppState& st);
    const char* suggestedMate() const;   // from the two picked surfaces

    // Windows open behind the docked viewport unless focused on the rising edge of "open"; each
    // window sets its *Focus_ flag when opened and consumes it (SetNextWindowFocus once) in draw.

    // ---- Insert ----
    bool insertOpen_ = false, insertFocus_ = false;
    int  insertSel_ = -1;                 // selected row in the open/recent chooser (-1 = none)
    struct PendingLibraryInsert {
        std::string path, targetDocId;
        InsertCompletion onComplete;
        double deadline = 0.0;
        bool activationRequested = false;
    };
    PendingLibraryInsert pendingLibraryInsert_;
    std::string insertChosenPath_;        // absolute path of the selected open/recent/browse part
    std::string insertNote_;              // last insert status / error line
    std::string insertTargetDocId_;       // assembly doc_id captured when the dialog opened / last insert
    std::string insertBrowseDir_;         // Workspace section directory (seeded from st.workspaceDir)


    // ---- Mate form (PM-style) ----
    bool mateOpen_ = false, mateFocus_ = false;
    int  activeWell_ = 0;                 // 0..2 — which well receives the next pick (2 = width/symmetric 3rd ref)
    struct MatePick {
        bool has = false;
        uint32_t bodyId = 0;
        nlohmann::json selector;
        std::string component;            // component k-id
        std::string label;                // chip text ("base · Face")
        MateSurfKind surf = MateSurfKind::None;
    } picks_[3];                          // [0],[1] standard; [2] is width's jaw / symmetric's plane
    int  mateType_ = 0;                   // index into the auto-suggested STANDARD type list
    // Component the Mate form was opened FOR via the tree "Edit" seam (requestEditComponentMates).
    // Shown as a header note so the form reads as "editing this component's placement"; cleared when
    // the form is closed/reopened. Empty for a plain ribbon-launched Mate.
    std::string editComponentTarget_;
    // Explicit selected mate type. Set by BOTH the Standard suggested buttons (= suggestedMate())
    // and the Mechanical/Advanced buttons. Empty => fall back to suggestedMate(). This is the
    // single source of truth read by addMate / the parameter block / the Add gate.
    std::string selectedMateType_;
    bool aligned_ = true;                 // Align: true = Aligned, false = Anti-Aligned
    std::vector<char> dimBuf_{std::vector<char>(64, 0)};   // distance / angle value (single dim)
    std::vector<char> minBuf_{std::vector<char>(64, 0)};   // *_limit / hinge min
    std::vector<char> maxBuf_{std::vector<char>(64, 0)};   // *_limit / hinge max
    std::vector<char> ratioBuf_{std::vector<char>(64, 0)}; // gear ratio
    std::vector<char> pitchBuf_{std::vector<char>(64, 0)}; // screw pitch (mm/rev)
    std::vector<char> pitchRadBuf_{std::vector<char>(64, 0)};  // rack_pinion pitch radius (optional)
    std::string mateError_;
    // The effective mate type for this frame (selectedMateType_ or the standard suggestion).
    std::string effectiveMateType() const;
    // True for types that consume a 3rd reference (width / symmetric).
    static bool typeNeedsThirdRef(const std::string& t);

    // ---- Component Pattern form (linear / circular) ----
    bool patternOpen_ = false, patternFocus_ = false;
    int  patternKind_ = 0;                 // 0 = linear, 1 = circular
    int  patternDir_  = 0;                 // index into the world direction/axis option table
    std::vector<char> patternCountBuf_{std::vector<char>(32, 0)};    // count1 (linear) / count (circular)
    std::vector<char> patternSpacingBuf_{std::vector<char>(32, 0)};  // spacing1 (mm) (linear)
    std::vector<char> patternAngleBuf_{std::vector<char>(32, 0)};    // angle (deg) (circular)
    bool patternEqual_ = true;             // equal_spacing (circular)
    std::vector<std::string> patternComps_;  // picked seed component ids (membership toggled by pick)
    std::string patternError_;

    // ---- Explode ----
    bool explodeOpen_ = false, explodeFocus_ = false;
    float explodeFactor_ = 0.0f;     // slider / target factor (what the user asked for)
    float explodeShown_ = 0.0f;      // factor currently on screen
    float explodeBaked_ = 0.0f;      // factor the uploaded mesh carries (engine-baked)
    float explodeConfirmed_ = 0.0f;  // engine factor after the last successful commit
    bool explodeAwaitMesh_ = false;  // confirmed factor's mesh not yet uploaded
    uint64_t explodeAwaitGen_ = 0;   // renderer meshGeneration() when the commit returned
    bool explodeOffsetsSet_ = false; // this panel owns the renderer's current body offsets
    bool explodeAuto_ = false;       // vectors come from the automatic plan, not authored steps
    bool explodeLines_ = true;
    std::string explodeDocId_;       // document the vectors/bake state below belong to
    struct ExplodeComp {
        std::vector<uint32_t> bodyIds;
        float vec[3] = {0, 0, 0};     // factor-1 offset (mm)
        float center[3] = {0, 0, 0};  // collapsed world box center
    };
    std::vector<ExplodeComp> explodeComps_;
    std::vector<float> explodeLineXyz_;  // reused dashed-line scratch
    // World boxes of the assembly collapsed and fully exploded (engine-reported), for framing.
    bool explodeBoundsValid_ = false;
    float explodeCollapsedBox_[6] = {0, 0, 0, 0, 0, 0};  // min xyz, max xyz
    float explodeExplodedBox_[6] = {0, 0, 0, 0, 0, 0};
    bool explodeAnimating_ = false;
    float explodeAnimFrom_ = 0.0f, explodeAnimTo_ = 0.0f;
    double explodeAnimT0_ = 0.0, explodeAnimDur_ = 0.0;
    // Latest-wins async doc_set_explode_state (never blocks GL)
    int  explodeGen_ = 0;
    bool explodeInFlight_ = false;
    bool explodePending_ = false;
    bool explodePendingActive_ = false;
    float explodePendingFactor_ = 0.0f;
    void kickExplodeAsync(AppState& st);
    void commitExplode(AppState& st, float factor);
    // Bind the explode state to the active document (resetting it for a different one) and
    // fetch that document's vectors without moving anything.
    void beginExplodeSession(AppState& st);
    void syncExplodeDoc(AppState& st);

    // ---- Interference ----
    bool interferenceOpen_ = false, interferenceFocus_ = false;
    nlohmann::json interferenceResult_;
    std::string interferenceError_;
    bool interferencePending_ = false;  // C39: eval_interference in flight (async)
    int  interferenceGen_ = 0;          // C39: bumped per refresh; stale callbacks ignored
    std::string interferenceDocId_;
    std::string interferenceDocName_;

    // ---- BOM ----
    bool bomOpen_ = false, bomFocus_ = false;
    struct BomRow { std::string name, source; int qty = 0; double massG = 0; double volMm3 = 0; bool haveMass = false; };
    std::vector<BomRow> bomRows_;
    std::vector<char> bomCsvPath_{std::vector<char>(512, 0)};
    std::string bomNote_;
    int bomPending_ = 0;                // C39: eval_mass calls still in flight (async)
    int bomGen_ = 0;                    // C39: bumped per refresh; stale callbacks ignored
    std::string bomDocId_;
    std::string bomDocName_;
};

}  // namespace ui
