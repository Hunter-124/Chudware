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
//   live preview (debounced ~400 ms): add (first preview) or edit_feature -> viewport refresh.
//   Cancel -> worker undo of every preview batch applied (count tracked). OK -> leave committed +
//   tree refresh. Edit path -> openForEdit() populates from the feature's params/refs.
#pragma once

#include <functional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

struct AppState;

namespace ui {

// ---- field model ----------------------------------------------------------

enum class PMFieldKind { Dim, SelectorWell, Combo, Check };

// Which topology kinds a SelectorWell accepts (mirrors feature_params sel(kinds…)).
struct PMSelKinds {
    bool face = false, edge = false, vertex = false;
};

// One row of a feature form (data-driven; one per param/ref in feature_params order).
struct PMFieldDef {
    PMFieldKind kind = PMFieldKind::Dim;
    const char* key = "";       // params/refs key emitted in the op
    const char* label = "";     // PM display label
    const char* group = "";     // collapsible group caption (empty = ungrouped header)

    // Dim
    double dimDefault = 0.0;    // canonical mm default

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

    // Combo
    int comboIndex = 0;

    // Check
    bool checkValue = false;
};

// Parse a dim string ("25", "25mm", "1.5in", "3/8in") to canonical mm. Returns true on success.
// Leading "=" is NOT handled here (that is an expr binding, detected by the caller).
bool pmParseDim(const std::string& text, double& outMm, std::string& err);
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
    void draw(AppState& st);

    // Feed a resolved viewport pick into the active selector well (called from the selection
    // observer). Returns true if the pick was consumed (an active well accepted it).
    bool routePick(const nlohmann::json& selector, int pickKind /*render::PickKind*/);

    // Called every frame: fires the debounced live preview if the form is dirty.
    void tick(AppState& st);

    // Close the form without committing or rolling back (used by scripted flows / app teardown).
    // For interactive use prefer the OK/Cancel header buttons.
    void closeIfActive() { if (active_) close(); }

private:
    void close();
    void commit(AppState& st);     // OK
    void cancelEdits(AppState& st); // Cancel: undo every preview batch
    void buildOpFeature(nlohmann::json& feature) const;   // assemble {type,name,params,refs}
    void applyPreview(AppState& st);  // add (first) or edit_feature; debounced
    void loadFromTree(AppState& st, const nlohmann::json& feature);  // edit-path populate
    PMField* activeWell();

    bool active_ = false;
    bool editMode_ = false;        // true = reopened on an existing feature (no add op)
    const PMFormDef* form_ = nullptr;
    std::string mode_;             // "boss"/"cut"/"" from the command
    std::string title_;
    std::string targetId_;         // feature id once it exists (add returns it; edit knows it)
    std::vector<PMField> fields_;

    int previewBatches_ = 0;       // preview ops applied (for Cancel undo)
    bool dirty_ = false;           // a field changed since last preview
    double lastEditTime_ = 0.0;    // ImGui::GetTime() of the last field change (debounce)
    bool firstPreviewDone_ = false;
};

}  // namespace ui
