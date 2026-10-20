// PropertyManager (C17) — SolidWorks-style feature forms.
//
// A single PM host occupies the FeatureManager tree's dock slot while a ribbon command (or a
// tree "Edit") is active; the tree returns when the PM closes (OK/Cancel). Forms are DATA-DRIVEN:
// each feature type maps to a list of FieldDef rows (matching docs/contracts/feature_params.md
// field order), so adding a feature later is a table edit, not new draw code.
//
// Field widgets:
//   - Dim:           slider (bounded range) / drag (open-ended) with the unit in the value text;
//                    CTRL+click types an exact number. The row's "=" toggle switches to free text
//                    entry accepting 25 / 25mm / 1.5in / 3/8in (parsed to canonical mm) and
//                    "=expr" to bind an equation. Inline red on parse/validation error.
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

#include <array>
#include <cstdint>
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
enum class PMFieldKind { Dim, SelectorWell, Combo, Check, Int, DatumRef, FeatureWell, Text };

// Which topology kinds a SelectorWell accepts (mirrors feature_params sel(kinds…)).
// `plane` is special: it does not come from the id-buffer (the origin datum planes aren't
// tessellated topology). A plane well is pickable in the viewport via SketchMode::drawPlanePicker,
// which routes a reserved datum-plane ref ("plane.front"/"plane.top"/"plane.right") into the well
// — the SolidWorks "click a reference plane" interaction reused outside the new-sketch flow.
struct PMSelKinds {
    bool face = false, edge = false, vertex = false, body = false, plane = false, axis = false;
    bool point = false;  // T-BL37: world-origin point ("point.origin") or point-datum feature
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
    // Numeric range/step metadata (canonical mm/deg). dimMax > dimMin => BOUNDED field, rendered
    // as a slider (angles, draft, k-factor); otherwise a drag clamped below at dimMin (default 0;
    // offsets that may reverse direction lower it — see pm_forms Signed()). dimStep 0 picks a
    // kind default (1 mm length — 1/16 in in IPS — 1 deg angle, 0.05 unit-less) used by the
    // CTRL+mouse-wheel step. Ranges/clamps apply to widget edits only; the "=" text-entry path
    // stays unclamped (engine validation is the backstop, same as before).
    double dimMin  = 0.0;
    double dimMax  = 0.0;       // <= dimMin => unbounded above
    double dimStep = 0.0;       // 0 = kind default

    // Int range (pattern/hole counts). Clamped on both drag and CTRL+click typed entry, so a
    // stray "100000 instances" can never reach the engine preview.
    int intMin = 1;
    int intMax = 200;

    // SelectorWell
    PMSelKinds selKinds;        // allowed kinds
    bool selMulti = true;       // array of selectors (edges/remove) vs single (axis/plane)

    // Combo
    const char* comboOptions = nullptr;  // "boss|cut" — param values emitted to engine (first = default)
    const char* comboLabels  = nullptr;  // "Boss|Cut" — display labels; null = use comboOptions
    bool segmented = false;              // render a 2-option combo as a segmented pill pair

    // Text (free-form string param, e.g. pattern instances-to-skip "2:1, 3:0")
    const char* placeholder = nullptr;   // greyed hint shown when the field is empty

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
    // Id-pass identity of the pick AT pick time (body + topo id + render::PickKind as int), so the
    // viewport can highlight every selected edge — not just the last one in st.selection — including
    // when a preview breaks. -1 kind = no viewport identity (datum/loaded pick); not highlightable.
    uint32_t pickBody = 0;
    uint32_t pickTopo = 0;
    int      pickKind = -1;
};

struct PMField {
    const PMFieldDef* def = nullptr;

    // Dim: editable text buffer + parsed canonical value + expr binding.
    std::vector<char> dimBuf{std::vector<char>(64, 0)};
    double dimValue = 0.0;       // canonical mm (when not an expr)
    std::string dimExpr;         // non-empty => bound to this expression
    std::string dimError;        // inline validation error (empty = ok)
    bool dimTextEntry = false;   // "=" toggle: unit-math / "=expr" text entry instead of the widget

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

    // Text (free-form string param)
    std::vector<char> textBuf{std::vector<char>(128, 0)};
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
std::string pmFormatDim(double mm, bool ips, bool angle = false);
// Format derived quantities (canonical mm-based) for Evaluate / mass / section readouts.
// IPS uses in² / in³ / lb / in⁴; MMGS uses mm² / mm³ / g / mm⁴.
std::string pmFormatArea(double mm2, bool ips);
std::string pmFormatVolume(double mm3, bool ips);
std::string pmFormatMass(double g, bool ips);
std::string pmFormatAreaMoment(double mm4, bool ips);
std::string pmFormatMassMoment(double g_mm2, bool ips);  // mass·length²: lb·in² (IPS) / g·mm² (MMGS)


// ---- in-viewport callouts (C27-A) -----------------------------------------

// One viewport callout chip for a visible+enabled Dim field while a form is open.
// SolidWorks-style: "Radius: 4mm" attached by a leader line to the affected geometry.
struct PMCalloutItem {
    std::string label;          // field label ("Radius", "Depth", …)
    std::string value;          // formatted with unit ("4mm", "0.2in")
    bool hasAnchor = false;     // false => no geometry to point at (dock the chip top-right)
    float anchor[3] = {0, 0, 0};  // world-space anchor point (valid when hasAnchor)
};

// ---- shared selection-well widget (used by feature forms AND assembly tools) ----
// Draws the same glass selection well as Extrude/Revolve reference wells: label row with
// optional "Pick in viewport" hint when active, frosted pill chip, accent border when armed,
// right-click clear. Returns click/right-click so the caller owns pick storage.
struct PMSelectionWellResult {
    bool clicked = false;       // left-click: activate this well
    bool rightClicked = false;  // right-click: clear
    bool hovered = false;
};
PMSelectionWellResult pmDrawSelectionWell(const char* id, const char* label,
                                          const char* chipText, bool active,
                                          bool dangling = false);

// Shared PM form header strip (icon + accent title) used by modeling forms and assembly guest forms.
void pmDrawFormHeader(const char* icon, const char* title, const char* description = nullptr);


// ---- the PropertyManager --------------------------------------------------

class PropertyManager {
public:
    bool active() const { return active_ || guestActive_; }


    // ---- guest form (assembly tools hosted in the left PM column) -----------------------------
    // Assembly Mate/Insert/Pattern/Explode/Interference are NOT data-driven feature forms; they
    // still need to occupy the FeatureManager/PM column like Extrude. openGuest() makes active()
    // true so layout.cpp auto-fronts the left pane and calls drawBody(); the body is a free-form
    // callback (AsmPanels draws its own fields + standard selection wells). OK/Cancel chrome
    // buttons call onOk/onCancel. Mutual exclusion: guest entry keeps and closes any feature form,
    // and openForCommand/openForEdit close any guest.
    using GuestFn = std::function<void(AppState&)>;
    void openGuest(AppState& st, const char* title, const char* icon,
                   GuestFn body, GuestFn onOk, GuestFn onCancel, bool readOnly = false);
    void closeGuest();
    bool guestActive() const { return guestActive_; }


    // C27-A: build the list of viewport callout items for the active form (empty if not active).
    // For each visible+enabled Dim field: {label, formatted value, anchor}. The anchor is derived
    // from the form's selected refs — first edge ref's centroid, else first face ref's centroid,
    // else none (chip docks top-right). Cheap to call every frame (rebuilds a small vector).
    std::vector<PMCalloutItem> calloutItems(AppState& st) const;
    // The feature id under live edit/preview (empty until the first preview lands or on edit).
    const std::string& targetId() const { return targetId_; }

    // Feature type of the active form ("fillet"/"chamfer"/…), or "" when no form is open. Cheap
    // read of the open form's PMFormDef::type — lets scripted flows assert WHICH form survived a
    // document switch (e.g. multidoc proving doc A keeps its fillet form while doc C has chamfer).
    std::string activeFormType() const { return (active_ && form_) ? form_->type : std::string(); }

    // Open a fresh form for a ribbon command (defaults populated). No-op if the command has no
    // form (returns false so the caller can fall back to a log line).
    bool openForCommand(AppState& st, const std::string& cmdId);
    // Open the form for an existing feature, populated from its params/refs (tree "Edit").
    bool openForEdit(AppState& st, const std::string& featureId);

    // Headless test hook (CHUDWARE_SHOT=pmform): set a field's value by key so type-gated fields
    // can be rendered for screenshot verification. value matches a Combo option, a Check
    // ("true"/"false"/"1"/"0"), or a Dim/Int number. No-op if no field has that key.
    void presetFieldForShot(const std::string& key, const std::string& value);

    // Viewport Instant3D coupling: while the PM is open, dragging an on-screen dimension handle sets
    // the matching Dim/Angle field to `value` (canonical mm/deg) and schedules the live preview — so
    // the drag flows through the SAME edit path as typing in the field (one source of truth, the PM's
    // OK/Cancel owns the commit/undo). Returns true if a visible+enabled Dim field `key` took it.
    bool dragSetDim(const std::string& key, double value);
    // Same Instant3D path for Check fields (e.g. extrude "flip" / Reverse direction). Dragging the
    // depth arrow past the sketch plane auto-toggles flip; this writes the checkbox + schedules the
    // same immediate preview as dragSetDim. Returns true if a visible+enabled Check field took it.
    bool dragSetCheck(const std::string& key, bool value);
    // Read a visible+enabled Check field's current value (for flip_at_press capture). False if absent.
    bool dragGetCheck(const std::string& key, bool& out) const;
    // True once the create-mode preview feature exists or in edit mode — i.e. there is a concrete
    // feature whose dimensions the viewport may show drag handles for.
    bool hasPreviewTarget() const { return active_ && !targetId_.empty(); }
    // True only while a form is open on an EXISTING feature via tree "Edit Feature" (not a
    // create-mode add). Gates the viewport push-pull (drag-to-extrude) arrow so it appears only
    // when the user is explicitly editing that feature, never on a passive face selection.
    bool isEditMode() const { return active_ && editMode_; }

    // Draw the PM panel into the dock slot (called in place of the tree while active()).
    // Compatibility wrapper: handles the dock-visible / floating-fallback Begin/End, then calls
    // drawBody(). The Wave 1D tabbed left pane calls drawBody() directly inside its own container.
    void draw(AppState& st);

    // Wave 1D: the PM form's inner content WITHOUT its own Begin/End — the caller owns the window
    // (the LeftPane container child, or draw()'s dock/floating window). No-op unless active().
    void drawBody(AppState& st);

    // T-G19: OK / Cancel command buttons, relocated ABOVE the PM into the left-pane top row
    // (between the Configurations tab and the collapse chevron). layout.cpp calls this from that row
    // while a form is active; the cluster is right-aligned so it ENDS at `rightX` and is vertically
    // centered on `centerY` (both screen-space px). Clicking OK commits (commit() guards readiness);
    // Cancel reverts every preview batch. No-op unless active(). Returns the cluster's pixel width so
    // the caller can reserve space / avoid the chevron.
    float drawChromeButtons(AppState& st, float rightX, float centerY);

    // Feed a resolved viewport pick into the active selector well (called from the selection
    // observer). Returns true if the pick was consumed (an active well accepted it). pickBody/
    // pickTopo are the id-pass identity of the clicked element (0/0 if unknown), stored so the
    // viewport can highlight every well pick — see collectActiveWellHighlights().
    bool routePick(const nlohmann::json& selector, int pickKind /*render::PickKind*/,
                   uint32_t pickBody = 0, uint32_t pickTopo = 0);

    // Append (body_id, topo_id, render::PickKind) for every pick in the ACTIVE edge-accepting well
    // that carries a viewport identity. The viewport feeds these into the overlay highlight set so
    // all selected edges glow (not just the last-clicked one), including when the preview breaks.
    void collectActiveWellHighlights(std::vector<std::array<uint32_t, 3>>& out) const;

    // A selected fillet/chamfer edge reconstructed in WORLD space from its selector, for the dashed
    // 3D overlay that lets the user see + deselect edges that the live round has already eaten away.
    // Stored as a world polyline so it covers every curve type the same way: a straight edge is two
    // points; a circular edge (revolve!) is a sampled loop (`closed`); anything we can't reconstruct
    // is a single centroid point (drawn as a small ring). `pickIndex` is the entry's position in the
    // active well (passed back to removeActiveWellPick).
    struct EdgeSeg {
        std::vector<std::array<float, 3>> pts;  // world polyline (>=1 pt)
        bool closed = false;                    // connect last->first (full circle)
        int  pickIndex = -1;                    // index into the active well's pick list
    };
    // Reconstruct the original (pre-round) edges of the active fillet/chamfer well from their stored
    // selectors (line: centroid ± ½·len·axis; circle: radius=len/2π loop in the axis plane). Empty
    // unless a blend PM is active. The viewport draws these dashed and hit-tests them so a click near
    // one deselects that edge.
    void collectActiveWellEdgeSegments(std::vector<EdgeSeg>& out) const;

    // Remove the pick at `pickIndex` from the active edge well (the dashed-line deselect path) and
    // schedule a live preview. Returns true if a pick was removed.
    bool removeActiveWellPick(int pickIndex);

    // True when the active selector well wants BODY selectors (e.g. boolean targets/tools). The
    // selection observer uses this to resolve the picked element's owning body (resolvePick
    // kind="body") instead of the face/edge/vertex it clicked.
    bool activeWellWantsBody() const;

    // True when the active selector well accepts origin datum-plane picks (PMSelKinds.plane). The
    // viewport plane picker (SketchMode::drawPlanePicker) only arms while this is true.
    bool activeWellWantsPlane() const;

    // True when the active well is a fillet/chamfer edge well: a FACE pick should expand into ALL of
    // that face's edges (SolidWorks "fillet a whole face"). The selection observer handles the
    // expansion (doc_face_edges -> per-edge resolvePick -> routePick).
    bool activeWellExpandsFaceToEdges() const;

    // Route a datum-plane ref into the active plane-accepting well (called by the viewport plane
    // picker). datumId is either a reserved origin plane ("plane.front"/"plane.top"/"plane.right")
    // OR a created datum-plane FEATURE ID (e.g. "F7") — both resolve as a bare-string ref in the
    // engine (_SEL_OR_DATUM / resolve_plane_ref). label overrides the chip text (the datum's name,
    // e.g. "Plane1"); empty => derive a short label from datumId. Re-picking the same plane toggles
    // it off (SolidWorks). Returns true if a well consumed the pick.
    bool routeDatumPlane(const std::string& datumId, const std::string& label = "");

    // True when the active selector well accepts datum-axis picks (PMSelKinds.axis). The viewport
    // axis picker (SketchMode::drawAxisPicker) only arms while this is true.
    bool activeWellWantsAxis() const;

    // True when the active well wants EDGES but not faces (e.g. fillet/chamfer "Edges"). The viewport
    // then uses an edge-PREFERRING pick (snap to the nearest edge within a grab radius) so 1-px-wide
    // curved rims on a revolved/round body are clickable instead of losing the centre pixel to the
    // face behind them — the "fillet can't find edges on a revolve" fix.
    bool activeWellPrefersEdge() const;

    // Route a datum-AXIS feature reference into the active axis-accepting well (called by the
    // viewport axis picker). `fid` is the datum-axis feature id; `label` is the chip text (the
    // datum's name). Reserved axis.x/y/z + user features store {"kind":"datum","feature":fid};
    // host-only auto plane-intersection axes store explicit {"kind":"axis",point,axis} geometry
    // (document.cpp axis_from_explicit). Re-picking the same axis toggles it off.
    bool routeDatumAxis(AppState& st, const std::string& fid, const std::string& label);
    // T-BL37: route origin/point-datum into a SelectorWell that has selKinds.point=true.
    bool routeDatumPoint(const std::string& fid, const std::string& label = "");
    bool activeWellWantsPoint() const;  // T-BL37

    // Human phrase for the active well's accepted topology ("edges", "faces or edges", …), or ""
    // when no selector well is active. Used to toast feedback when a pick is rejected for the
    // wrong kind (SolidWorks tells you what a field wants).
    std::string activeWellKindHint() const;

    // ---- sweep/loft PATH selection (a whole SKETCH or a CURVE/helix feature) ----
    // A sweep path / loft centerline is a SKETCH or a CURVE (helix/spiral) feature,
    // not a model edge — so it's picked by feature (viewport curve picker or a
    // FeatureManager-tree click), not by topology. True while the active well is the
    // "path"/"centerline" well so those pickers arm.
    bool activeWellWantsPathFeature() const;
    // Route a sketch / curve feature into the active path well. The selector uses a
    // schema-valid kind: a SKETCH path emits {"kind":"region","sketch":fid} (its wire
    // becomes the spine); a CURVE/helix path (isCurve=true) emits {"kind":"datum",
    // "feature":fid}. selector_owner_id resolves either to the owning feature, which a
    // sweep follows as its spine. Re-picking the same feature toggles it off. Returns
    // true if a path well consumed it.
    bool routePathFeature(const std::string& fid, const std::string& name, bool isCurve);

    // Feature ids currently held by the path/centerline/guides wells, so the viewport can
    // keep the selected sweep path + guides HIGHLIGHTED while the form is open (even when
    // the preview is failing). Extracts the owning feature from each selector.
    std::vector<std::string> pathHighlightFeatureIds() const;

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
    // End the profile/sections SELECT session (deactivate selector wells) without clearing
    // picks — click-off white space / other UI while a profile is already chosen. Empty wells
    // stay armed so the first pick can still land.
    void endProfileSelection();
    // True while any visible selector/feature well is armed for viewport picks.
    bool hasActiveSelectorWell() const;


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
    bool cancelEdits(AppState& st); // Cancel: undo every preview batch

    // C48: id of the IN-FLIGHT create-mode preview feature ("" when none / edit mode).
    // The sketch ghost renderer exempts this feature's sketch consumption so the profile
    // curves stay visible during an Extrude/Revolve preview (SolidWorks behavior) — the
    // consumption only "counts" once the feature is committed.
    std::string previewFeatureId() const {
        return (active_ && !editMode_) ? targetId_ : std::string();
    }

private:
    void close();
    bool closeForAgentLock(AppState& st);
    void commit(AppState& st);     // OK
    void buildOpFeature(nlohmann::json& feature) const;   // assemble {type,name,params,refs}
    bool applyPreview(AppState& st);  // add (first) or edit_feature; debounced
    nlohmann::json buildAiAssistPayload(AppState& st) const;
    void requestAiAssist(AppState& st);
    void applyAiAssistResult(AppState& st, const nlohmann::json& result, int requestSeq);
    void loadFromTree(AppState& st, const nlohmann::json& feature);  // edit-path populate
    PMField* activeWell();
    // After a single-ref well gets a pick, hand focus to the next empty visible selector well
    // (SolidWorks "pick A then B" flow) so multi-reference tools don't make the user click each
    // well first. No-op for multi-pick wells (they keep focus to gather more).
    void advanceWellAfterFill(PMField* filled);
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
    bool previewUndoGroupStarted_ = false;  // true after the first undo snapshot for this PM
    bool dirty_ = false;           // a field changed since last preview
    double lastEditTime_ = 0.0;    // ImGui::GetTime() of the last field change (debounce)
    bool firstPreviewDone_ = false;
    bool previewInFlight_ = false; // T-PERF1: async applyOps in flight (serialize first add_feature)

    // C37-F: checkpoint id taken at open (before any preview). Empty = checkpoint failed at open
    // (fallback: undo loop). revert(checkpointId_) is the single cancel call.
    std::string checkpointId_;

    // Phase 4 (shared engine daemon): while this form previews, the daemon holds the doc for
    // exclusive edit (edit_hold at open — foreign clients get retriable busy_preview) and the hold
    // is released in close() (both OK and Cancel funnel there). The daemon auto-refreshes the hold
    // on every preview op; tick() additionally re-holds at most once/second while the form is
    // active so an idle-but-open form outlives brief pauses. If the hold is REJECTED busy at open
    // (another client mid-edit), the form still opens with a warning toast — non-blocking, CAS +
    // the other side's hold guard actual conflicts (documented Phase 4 choice). Empty = no hold
    // (legacy mode / hold rejected).
    std::string holdDocId_;
    std::string sessionDocId_;   // retained even when edit_hold is unavailable
    double lastHoldTime_ = 0.0;   // ImGui::GetTime() of the last edit_hold call

    // C37-F: true when the first field in the form has received SetKeyboardFocusHere.
    bool focusedOnOpen_ = false;

    // C37-F: inline preview-error message shown in the PM body (cleared on next successful preview).
    std::string previewError_;
    // Non-fatal note from the last successful preview (e.g. fillet rounded a subset of the selected
    // edges) — surfaced in the PM body in amber so the user can lower the size before committing.
    std::string previewWarning_;

    // True only during the final commit apply. Blend features (fillet/chamfer) preview with the
    // feature SUPPRESSED so the viewport shows + picks the UN-rounded base body — edge picks are then
    // captured against stable base topology (no fillet blend/trim edges to corrupt them), making
    // select/unselect reliable. On commit the feature is un-suppressed so the rounds actually apply.
    bool committing_ = false;

    // C37-F: true when we need to set keyboard focus on the first editable field next frame.
    bool wantFocusFirstField_ = false;

    // C38 (P2f): "was a PM widget active during the PM's own draw last frame". Set at the END of
    // draw() (scoped to the PM window), read at the START of the next draw() to gate Enter-commit.
    // Replaces the prior-frame, ANY-window ImGui::IsAnyItemActive() read that let Enter in unrelated
    // panels commit the PM.
    bool pmWidgetActivePrevFrame_ = false;
    // After endProfileSelection() (user clicked empty space / non-well UI while picks exist),
    // skip the always-on "keep a visible well active" re-home so the select session stays ended
    // until the user re-arms by clicking a well chip or a region. Cleared on any well click /
    // setProfilePicks.
    bool suppressWellAutoArm_ = false;


    // Select-with-AI state. Responses are request-sequenced so a late result from a closed or
    // superseded form cannot patch the wrong command.
    bool aiBusy_ = false;
    int aiRequestSeq_ = 0;
    std::string aiMessage_;
    std::vector<std::string> aiHighlights_;

    // Guest form (assembly tools in the left PM column). When guestActive_, drawBody renders the
    // guest header + body callback instead of a feature form; drawChromeButtons routes OK/Cancel
    // to guestOnOk_/guestOnCancel_.
    bool guestActive_ = false;
    bool guestReadOnly_ = false;
    std::string guestTitle_;
    std::string guestIcon_;
    GuestFn guestBody_;
    GuestFn guestOnOk_;
    GuestFn guestOnCancel_;
};

}  // namespace ui
