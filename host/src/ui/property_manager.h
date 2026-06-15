// PropertyManager (C17) — SolidWorks-style feature forms.
//
// A single PM host occupies the FeatureManager tree's dock slot while a ribbon command (or a
// tree "Edit") is active; the tree returns when the PM closes (OK/Cancel). Forms are DATA-DRIVEN:
// each feature type maps to a list of FieldDef rows (matching docs/contracts/feature_params.md
// field order), so adding a feature later is a table edit, not new draw code.
//
// Field widgets:
//   - Dim:           numeric input accepting 25 / 25mm / 1.5in / 3/8in (parsed to canonical mm)
//                    and "=expr" to bind an equation. Inline red on parse/validation error.
//   - SelectorWell:  labeled pick target; when active, viewport picks (filtered by allowed kinds)
//                    route into it. Shows "Edge×3" chips; right-click clears; red tint on a
//                    dangling ref reported in feature_status.
//   - Combo / Check: enum / bool fields.
//
// Flow: ribbon cmd -> openForCommand() opens the form with defaults -> user edits ->
//   live preview (debounced ~250 ms): add (first preview) or edit_feature -> viewport refresh.
//   Cancel -> single doc_revert call (C37-F checkpoint/revert) replaces the old N-undo loop.
//   OK -> leave committed + tree refresh. Edit path -> openForEdit() populates from params/refs.
//
// C37-F additions: checkpoint/revert cancel, keyboard policy, form polish, defensive fixes.
#pragma once

#include <functional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

struct AppState;

namespace ui {

// ---- field model ----------------------------------------------------------

// Dim          numeric (mm/deg) input.   SelectorWell  viewport face/edge/vertex picks.
// Combo/Check  enum / bool params.        Int           integer param (pattern counts).
// DatumRef     a combo whose chosen value is emitted as a refs string (datum axis/plane, e.g.
//              "axis.x"/"plane.right") — for _SEL_OR_DATUM ref slots (pattern direction, mirror
//              plane, draft neutral). FeatureWell  a list of feature ids (refs _FEAT_LIST) seeded
//              from the FeatureManager selection — the features a pattern/mirror replicates.
enum class PMFieldKind { Dim, SelectorWell, Combo, Check, Int, DatumRef, FeatureWell };

// Which topology kinds a SelectorWell accepts (mirrors feature_params sel(kinds…)).
struct PMSelKinds {
    bool face = false, edge = false, vertex = false, body = false;
};

// One row of a feature form (data-driven; one per param/ref in feature_params order).
struct PMFieldDef {
    PMFieldKind kind = PMFieldKind::Dim;
    const char* key = "";       // params/refs key emitted in the op
    const char* label = "";     // PM display label
    const char* group = "";     // collapsible group caption (empty = ungrouped header)

    // Dim
    double dimDefault = 0.0;    // canonical mm default (also the Int default, truncated)
    const char* unitLabel = nullptr;   // override the "[mm]" suffix (e.g. "[deg]"; "" = none)

    // SelectorWell
    PMSelKinds selKinds;        // allowed kinds
    bool selMulti = true;       // array of selectors (edges/remove) vs single (axis/plane)

    // Combo
    const char* comboOptions = nullptr;  // "boss|cut" — param values emitted to engine (first = default)
    const char* comboLabels  = nullptr;  // "Boss|Cut" — display labels; null = use comboOptions

    // Check
    bool checkDefault = false;

    // Conditional enable: when enableKey is non-null, this field is enabled only when the
    // sibling Combo named enableKey has its selected value in the pipe-separated enableVals
    // list (e.g. enableVals="blind|mid_plane"). A null enableKey means always enabled.
    // comingSoon=true renders the field greyed/disabled with a "(coming soon)" tooltip even
    // when the enable condition would otherwise pass — for discoverability of future params.
    const char* enableKey  = nullptr;
    const char* enableVals = nullptr;   // pipe-separated allowed values; empty = all
    bool comingSoon = false;
    // When true, this field is OMITTED from the emitted op while its enable condition is false
    // (instead of the default "emit anyway; engine ignores"). Needed when a present-but-default
    // value would change engine behaviour — e.g. hole.diameter must be ABSENT for the ISO
    // `standard` table to drive the diameter; emitting a default would override the table.
    bool omitWhenDisabled = false;
};

// A complete feature form: type + display title + ordered fields.
struct PMFormDef {
    const char* type = "";       // feature type ("box", "fillet", …)
    const char* title = "";      // PM header title ("Boss-Extrude" style display name)
    const char* icon = "";       // atlas icon name
    std::vector<PMFieldDef> fields;
};

// Look up the form for a feature type. Returns nullptr if no form is defined (pm_forms.cpp).
const PMFormDef* pmFindForm(const std::string& type);
// Map a ribbon cmd_id (e.g. "fillet", "extrude_boss", "shell") to a feature type + mode.
// Returns nullptr if the command has no PM form. *mode set to "boss"/"cut"/"" .
const PMFormDef* pmFormForCommand(const std::string& cmdId, std::string& mode);

// ---- live field state -----------------------------------------------------

struct PMSelEntry {
    nlohmann::json selector;   // resolved selector object (kind/surf/centroid/…)
    std::string label;         // short chip text ("Edge")
};

struct PMField {
    const PMFieldDef* def = nullptr;

    // Dim: editable text buffer + parsed canonical value + expr binding.
    std::vector<char> dimBuf{std::vector<char>(64, 0)};
    double dimValue = 0.0;       // canonical mm (when not an expr)
    std::string dimExpr;         // non-empty => bound to this expression
    std::string dimError;        // inline validation error (empty = ok)

    // SelectorWell
    std::vector<PMSelEntry> picks;
    bool wellActive = false;     // this well is receiving viewport picks
    bool wellDangling = false;   // a ref came back dangling in feature_status

    // Combo / DatumRef
    int comboIndex = 0;

    // Check
    bool checkValue = false;

    // Int (pattern counts)
    int intValue = 1;
};

// Parse a dim string to canonical units (mm; degrees when angle=true). Accepts SolidWorks-style
// unit math: full +-*/() expressions with per-literal unit suffixes ("100mm+2in/0.5m",
// "(3+1/8)in", "25.4/2"); a single TRAILING unit applies to the whole expression ("1/2in" =
// half an inch). Length units: mm cm m um/µm in/" ft/' mil/thou; angle units: deg ° rad. Returns
// true on success. Leading "=" is NOT handled here (that is an expr binding, detected by caller).
//
// bareLengthScale is the canonical-mm value of ONE bare (unit-less) length unit — the document's
// active length unit. 1.0 = MMGS (a bare "1.5" means 1.5 mm); 25.4 = IPS (a bare "1.5" means
// 1.5 in). It scales bare LENGTH literals only; literals carrying an explicit suffix (1.5mm, 2in)
// always honour that suffix, and ANGLE values ignore it entirely (degrees in both systems).
bool pmParseDim(const std::string& text, double& outMm, std::string& err, bool angle = false,
                double bareLengthScale = 1.0);
// Format a canonical mm value for display, honouring IPS when ips==true.
std::string pmFormatDim(double mm, bool ips);

// ---- in-viewport callouts (C27-A) -----------------------------------------

// One viewport callout chip for a visible+enabled Dim field while a form is open.
// SolidWorks-style: "Radius: 4mm" attached by a leader line to the affected geometry.
struct PMCalloutItem {
    std::string label;          // field label ("Radius", "Depth", …)
    std::string value;          // formatted with unit ("4mm", "0.2in")
    bool hasAnchor = false;     // false => no geometry to point at (dock the chip top-right)
    float anchor[3] = {0, 0, 0};  // world-space anchor point (valid when hasAnchor)
};

// ---- the PropertyManager --------------------------------------------------

class PropertyManager {
public:
    bool active() const { return active_; }

    // C27-A: build the list of viewport callout items for the active form (empty if not active).
    // For each visible+enabled Dim field: {label, formatted value, anchor}. The anchor is derived
    // from the form's selected refs — first edge ref's centroid, else first face ref's centroid,
    // else none (chip docks top-right). Cheap to call every frame (rebuilds a small vector).
    std::vector<PMCalloutItem> calloutItems(AppState& st) const;
    // The feature id under live edit/preview (empty until the first preview lands or on edit).
    const std::string& targetId() const { return targetId_; }

    // Open a fresh form for a ribbon command (defaults populated). No-op if the command has no
    // form (returns false so the caller can fall back to a log line).
    bool openForCommand(AppState& st, const std::string& cmdId);
    // Open the form for an existing feature, populated from its params/refs (tree "Edit").
    bool openForEdit(AppState& st, const std::string& featureId);

    // Draw the PM panel into the dock slot (called in place of the tree while active()).
    // Compatibility wrapper: handles the dock-visible / floating-fallback Begin/End, then calls
    // drawBody(). The Wave 1D tabbed left pane calls drawBody() directly inside its own container.
    void draw(AppState& st);

    // Wave 1D: the PM form's inner content WITHOUT its own Begin/End — the caller owns the window
    // (the LeftPane container child, or draw()'s dock/floating window). No-op unless active().
    void drawBody(AppState& st);

    // Feed a resolved viewport pick into the active selector well (called from the selection
    // observer). Returns true if the pick was consumed (an active well accepted it).
    bool routePick(const nlohmann::json& selector, int pickKind /*render::PickKind*/);

    // True when the active selector well wants BODY selectors (e.g. boolean targets/tools). The
    // selection observer uses this to resolve the picked element's owning body (resolvePick
    // kind="body") instead of the face/edge/vertex it clicked.
    bool activeWellWantsBody() const;

    // Human phrase for the active well's accepted topology ("edges", "faces or edges", …), or ""
    // when no selector well is active. Used to toast feedback when a pick is rejected for the
    // wrong kind (SolidWorks tells you what a field wants).
    std::string activeWellKindHint() const;

    // ---- C51: sketched-feature region picking ----------------------------------------------
    // The viewport region picker (SketchMode::drawRegionPicker) drives these to render region
    // highlights and feed click-to-toggle selections into the profile (extrude/revolve/sweep) or
    // sections (loft) well. The picker owns all selection policy; the PM just stores selectors and
    // fires the debounced preview.
    //
    // wantsRegionPicker(): the active form has a profile/sections well (so regions are pickable).
    bool wantsRegionPicker() const;
    // sectionsAreOrdered(): true for loft — clicks APPEND in order; false = unordered toggle set.
    bool sectionsAreOrdered() const;
    // Current selectors held by the profile/sections well (region/face selector objects).
    std::vector<nlohmann::json> profilePicks() const;
    // Replace the profile/sections well selectors and schedule a preview. An empty list on a
    // profile well is coerced to nothing here — the picker guarantees a whole-sketch fallback so
    // the profile never dangles.
    void setProfilePicks(AppState& st, const std::vector<nlohmann::json>& sels);
    // Sketch feature ids whose regions the picker should display: the sketches already referenced
    // by the current picks, else (sections / unseeded) every unconsumed non-empty sketch.
    std::vector<std::string> regionCandidateSketches(AppState& st) const;

    // Called every frame: fires the debounced live preview if the form is dirty.
    void tick(AppState& st);

    // Close the form without committing or rolling back (used by scripted flows / app teardown).
    // For interactive use prefer the OK/Cancel header buttons.
    void closeIfActive() { if (active_) close(); }

    // C43: public cancel — revert every preview batch to the open-time checkpoint and close.
    // Used by the ribbon dispatcher when a sketch command must displace an open form
    // (mutual exclusion: a PM and the plane-pick overlay must never coexist).
    void cancelEdits(AppState& st); // Cancel: undo every preview batch

    // C48: id of the IN-FLIGHT create-mode preview feature ("" when none / edit mode).
    // The sketch ghost renderer exempts this feature's sketch consumption so the profile
    // curves stay visible during an Extrude/Revolve preview (SolidWorks behavior) — the
    // consumption only "counts" once the feature is committed.
    std::string previewFeatureId() const {
        return (active_ && !editMode_) ? targetId_ : std::string();
    }

private:
    void close();
    void commit(AppState& st);     // OK
    void buildOpFeature(nlohmann::json& feature) const;   // assemble {type,name,params,refs}
    void applyPreview(AppState& st);  // add (first) or edit_feature; debounced
    void loadFromTree(AppState& st, const nlohmann::json& feature);  // edit-path populate
    PMField* activeWell();
    // C51: the profile/sections well (first SelectorWell keyed "profile" or "sections").
    PMField* profileWell();
    const PMField* profileWell() const;

    // Active document-unit display state. Set from AppState.ips() at open; re-synced each draw so a
    // live MMGS<->IPS toggle reformats every open Dim field's DISPLAY (the canonical mm dimValue is
    // untouched). reformatDimDisplays() rewrites each non-expr dim buffer from its dimValue.
    bool ips_ = false;
    void reformatDimDisplays();

    bool active_ = false;
    bool editMode_ = false;        // true = reopened on an existing feature (no add op)
    const PMFormDef* form_ = nullptr;
    std::string mode_;             // "boss"/"cut"/"" from the command
    std::string title_;
    std::string targetId_;         // feature id once it exists (add returns it; edit knows it)
    std::vector<PMField> fields_;

    int previewBatches_ = 0;       // preview ops applied (fallback undo count if checkpoint failed)
    bool dirty_ = false;           // a field changed since last preview
    double lastEditTime_ = 0.0;    // ImGui::GetTime() of the last field change (debounce)
    bool firstPreviewDone_ = false;

    // C37-F: checkpoint id taken at open (before any preview). Empty = checkpoint failed at open
    // (fallback: undo loop). revert(checkpointId_) is the single cancel call.
    std::string checkpointId_;

    // C37-F: true when the first field in the form has received SetKeyboardFocusHere.
    bool focusedOnOpen_ = false;

    // C37-F: inline preview-error message shown in the PM body (cleared on next successful preview).
    std::string previewError_;

    // C37-F: true when we need to set keyboard focus on the first editable field next frame.
    bool wantFocusFirstField_ = false;

    // C38 (P2f): "was a PM widget active during the PM's own draw last frame". Set at the END of
    // draw() (scoped to the PM window), read at the START of the next draw() to gate Enter-commit.
    // Replaces the prior-frame, ANY-window ImGui::IsAnyItemActive() read that let Enter in unrelated
    // panels commit the PM.
    bool pmWidgetActivePrevFrame_ = false;
};

}  // namespace ui
