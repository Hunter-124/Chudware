// Configurations panel (SolidWorks "ConfigurationManager").
//
// A dockable window that lists the active PART/ASM document's named configurations and lets the
// user switch (activate), add, and delete them. Per-config param overrides + feature suppressions
// are shown READ-ONLY under the selected config (a later "design tables" wave adds grid editing).
//
// Backed by the engine config_* RPCs via DocMirror::configList (read-only cache) and
// DocMirror::callConfig (activate/add/delete ingest the ops-response so the viewport + tree refresh
// on switch). The cache is refreshed on demand: a doc/revision change or any config op invalidates
// it, and the next draw re-pulls config_list.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

struct AppState;

namespace ui {

class ConfigPanel {
public:
    // Draw the ConfigurationManager contents into the current window/child. Pulls config_list
    // lazily and caches it; gated to part/asm documents (shows a "no part" hint otherwise).
    void drawEmbedded(AppState& st);

private:
    // (Re)pull config_list into the cache for the active doc. Guarded; toasts + leaves the old cache
    // on failure. Records the doc id + revision the cache reflects so staleness is auto-detected.
    void refresh(AppState& st);
    void drawBody(AppState& st);

    // Wrapper around DocMirror::callConfig: ingests the ops-response (viewport mesh reload from
    // aicm_path) and invalidates the cache. Returns true on success; toasts + returns false on error.
    bool runConfigOp(AppState& st, const std::string& method, const nlohmann::json& params);

    // ---- cached config_list result ----
    bool        cacheValid_ = false;
    std::string cacheDocId_;        // doc id the cache reflects
    int         cacheRevision_ = -1;  // revision the cache reflects (re-pull when it advances)
    std::vector<std::string>          names_;     // configuration names (list order)
    std::string                       active_;    // active configuration name
    nlohmann::json                    detail_;    // [{name, param_overrides, suppressions}]
    int         selected_ = -1;     // index into names_ of the row whose overrides are expanded

    // ---- "Add configuration" inline prompt ----
    bool        addPromptOpen_ = false;
    char        addNameBuf_[128] = {0};
    bool        addActivate_ = true;  // activate the new config right after creating it
    bool        focusAddField_ = false;
};

}  // namespace ui
