// Design Table panel (SolidWorks "Design Table").
//
// A dockable spreadsheet-style grid over the active PART/ASM document's configurations: ROWS are
// configurations, COLUMNS are driven dimensions/params (plus per-feature suppression columns).
// Editing a cell writes that config's override (or suppression) through the Configurations engine
// RPCs, so the model updates — editing the ACTIVE config's row updates the viewport live; editing
// an inactive row is a metadata write that takes effect when that config is activated. This is the
// editing front-end for the read-only Configurations panel (config_panel.{h,cpp}).
//
// Backed by the same engine config_* RPCs the ConfigurationManager uses, via DocMirror::configList
// (read-only cache of names/active/detail) and DocMirror::callConfig (config_set_override /
// config_set_suppression / config_add / config_delete / config_activate ingest the ops-response so
// the viewport + FeatureManager refresh on the active config). The config_list cache is re-pulled on
// a doc/revision change or after any edit. Columns are derived from (a) every (feature_id.param)
// any config currently overrides, plus (b) user-added columns chosen from the active model's
// feature dims (the same dim fields the PropertyManager forms expose) or per-feature suppression.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

struct AppState;

namespace ui {

class DesignTablePanel {
public:
    // Draw the "Design Table" window (no-op unless st.showDesignTablePanel). Call every frame from
    // the main loop. Pulls config_list lazily and caches it; gated to part/asm documents (shows a
    // hint for drawing tabs / no document).
    void draw(AppState& st);

private:
    // One grid column. Two kinds: a driven param (feature_id.param, numeric, possibly an angle) or a
    // per-feature suppression checkbox. label_ is the human header ("Extrude1.depth" / "Suppress:
    // Fillet1"). For a param column, key() == "fid.param" matches the engine's param_overrides keys.
    struct Column {
        bool        suppress = false;     // true => suppression checkbox column; false => param column
        std::string featureId;            // owning feature id
        std::string param;                // param key (empty for suppression columns)
        std::string label;                // header label
        bool        angle = false;        // param is an angle (parse/format in degrees, no length unit)
        std::string key() const {         // engine param_overrides key for a param column
            return featureId + "." + param;
        }
    };

    // A candidate column the user can ADD: enumerated from the active model's features (param dims
    // surfaced by the PropertyManager form for that feature type) + a suppression entry per feature.
    struct Candidate {
        Column      col;                  // the column it would add
        std::string menuLabel;            // "Extrude1 / Depth" style picker label
    };

    // (Re)pull config_list into the cache for the active doc. Guarded; toasts + leaves the old cache
    // on failure. Records the doc id + revision the cache reflects so staleness is auto-detected.
    // Also (re)seeds columns_ with every param ANY config overrides (preserving user-added columns).
    void refresh(AppState& st);

    // Wrapper around DocMirror::callConfig: ingests the ops-response (viewport mesh reload from
    // aicm_path) and invalidates the cache. Returns true on success; toasts + returns false on error.
    bool runConfigOp(AppState& st, const std::string& method, const nlohmann::json& params);

    // The detail entry for config `name` (param_overrides + suppressions), or nullptr.
    const nlohmann::json* detailFor(const std::string& name) const;

    // Build the add-column candidate list from the active model's features (dim params from the PM
    // form for each feature type + a suppression candidate per feature). Skips columns already shown.
    void buildCandidates(AppState& st, std::vector<Candidate>& out) const;

    // Add a column if it is not already present (dedup by kind + key).
    void addColumn(const Column& c);

    // ---- cached config_list result ----
    bool        cacheValid_ = false;
    std::string cacheDocId_;          // doc id the cache reflects
    int         cacheRevision_ = -1;  // revision the cache reflects (re-pull when it advances)
    std::vector<std::string> names_;  // configuration names (list order = rows)
    std::string active_;              // active configuration name
    nlohmann::json detail_;           // [{name, param_overrides, suppressions}]

    // ---- column set (persisted in panel state across refreshes) ----
    std::vector<Column> columns_;
    // True once refresh has merged in the overridden-param columns for the current doc; reset when
    // the doc id changes so a fresh doc starts from its own override set.
    std::string columnsForDocId_;

    // ---- "Add column" popup state ----
    bool addOpen_ = false;            // the add-column popup is requested open this frame

    // ---- "Add configuration" inline prompt ----
    bool        addCfgOpen_ = false;
    char        addCfgName_[128] = {0};
    bool        addCfgActivate_ = false;
    bool        focusAddCfg_ = false;

    // Row selected in the grid (index into names_); drives Delete/Activate. -1 = none.
    int         selectedRow_ = -1;
};

}  // namespace ui
