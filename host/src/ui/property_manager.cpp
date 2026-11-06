// PropertyManager implementation (C17).
//
// Draws the active feature form, routes viewport picks, debounces live previews through
// doc_apply_ops, accepts only a successful final-quality result, and cancels via a
// revision-guarded opening checkpoint.
#include <algorithm>
#include <cfloat>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdarg>
#include <cstdio>
#include <cstring>
#include <string>
#include <string_view>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"   // ImRect (ui::adaptivePanel rect argument)

#include "property_manager.h"
#include "property_manager_fields.inl"
#include "icons.h"
#include "widgets/kit_form.h"  // display-only dimension formatting for viewport callouts
#include "sketch_mode.h"   // C38 (P1f): SketchMode::active() — sketch owns Esc while editing
#include "../ui_theme.h"   // liquid glass v2: adaptivePanel / glassRowBackground / AdaptiveSurface

#include "../core/app_state.h"
#include "../core/doc_mirror.h"
#include "engine_backend.h"   // Phase 4: daemon edit_hold/edit_release (services/worker)
#include "../render/id_pass.h"
#include "../renderer.h"     // Renderer::refDatums() for axis.auto geometry lookup
#include "../vec_math.h"     // W2-ref: explicit auto-axis geometry (Vec3 / cross / normalize)

namespace ui {

// C37-F: Debounce window for live preview (was 0.40 s; reduced for snappier feel).
static const double kDebounceSec = 0.25;
// A live drag/keystroke preview is disposable: cap its engine round-trip so a pathological or
// very slow rebuild can't freeze the form — or machine-gun the shared daemon — for the full 120 s
// default. On timeout the preview drops to an inline error and the GUI stays responsive; the
// OK/commit path (committing_) keeps the patient default so a real commit never aborts early.
static const int kPreviewTimeoutMs = 20000;

void PropertyManager::takeOpenHold(AppState& st) {
    const uint64_t generation = ++editSessionGeneration_;
    rpcLifetime_ = std::make_shared<int>(0);
    cancelPending_ = cancelInFlight_ = false;
    const std::weak_ptr<int> lifetime = rpcLifetime_;
    sessionDocId_ = st.docMirror.docId();
    holdRpc_ = &st.rpc;
    checkpointId_.clear();
    cancelRevision_ = -1;
    cancelSafe_ = false;
    checkpointInFlight_ = st.docMirror.hasDoc();
    holdInFlight_ = false;
    lastHoldTime_ = ImGui::GetTime();
    if (checkpointInFlight_) {
        auto onCheckpoint = [this, &st, generation, lifetime](const core::RpcResult& r) {
            if (lifetime.expired() || generation != editSessionGeneration_) return;
            checkpointInFlight_ = false;
            if (r.ok && r.result.is_object() && !r.result.contains("code")) {
                checkpointId_ = r.result.value("checkpoint", std::string());
                cancelRevision_ = r.result.value("revision", -1);
                cancelSafe_ = !checkpointId_.empty() && cancelRevision_ >= 0;
            }
            if (!cancelSafe_)
                st.addLog("[pm] checkpoint failed — Cancel will keep applied previews");
        };
        nlohmann::json params = {{"doc_id", sessionDocId_}};
        // The primary lane is a daemon connection; in-process documents use WorkerClient's
        // native core route instead. Previews still wait until this checkpoint completes.
        if (engine::daemonMode())
            st.rpc.callPrimary("doc_checkpoint", std::move(params), std::move(onCheckpoint));
        else
            st.rpc.call("doc_checkpoint", std::move(params), std::move(onCheckpoint));
    }
    holdDocId_ = engine::daemonMode() ? sessionDocId_ : std::string();
    if (holdDocId_.empty()) return;
    holdInFlight_ = true;
    st.rpc.callPrimary("edit_hold", {{"doc_id", holdDocId_}},
                [this, &st, generation, lifetime](const core::RpcResult& r) {
        if (lifetime.expired() || generation != editSessionGeneration_) return;
        holdInFlight_ = false;
        if (r.result.is_object() &&
            r.result.value("code", std::string()) == "busy_preview") {
            holdDocId_.clear();
            const std::string holder = r.result.value("holder", std::string("another client"));
            if (st.isActiveMirrorDoc())
                st.toast(ui::ToastLevel::Warn,
                         "An " + holder + " client is editing this part — your edit is not blocked, "
                         "but its lock is briefly held and previews may conflict");
        }
    });
}

// ---- open / close ---------------------------------------------------------

bool PropertyManager::openForCommand(AppState& st, const std::string& cmdId) {
    std::string mode;
    const PMFormDef* form = pmFormForCommand(cmdId, mode);
    if (!form) return false;
    if (guestActive_) closeGuest();
    if (active_ && !cancelEdits(st)) return false;   // replacing a PM must roll back previews first

    // C40: SolidWorks mode exclusion. Invoking a feature command while a sketch is being
    // edited EXITS the sketch first (its profile then pre-seeds extrude/revolve below);
    // an armed plane-pick is cancelled — a feature PM and the pick overlay must never
    // coexist (they fight over the viewport mouse).
    std::string justExitedSketch;
    if (st.sketchMode && st.sketchMode->active()) {
        justExitedSketch = st.sketchMode->sketchId();
        st.sketchMode->exitSketch(st, /*keep=*/true);
    }
    st.sketchPlanePickActive = false;

    active_ = true;
    editMode_ = false;
    form_ = form;
    mode_ = mode;
    title_ = form->title;
    targetId_.clear();
    legacyFillet_ = legacyFilletModeKnown_ = false;
    previewBatches_ = 0;
    previewUndoGroupStarted_ = false;
    dirty_ = false;
    firstPreviewDone_ = false;
    formSourceRevision_ = st.docMirror.revision();
    freshEditRequired_ = false;
    previewInFlight_ = false;
    lastEditTime_ = ImGui::GetTime();
    previewError_.clear();
    focusedOnOpen_ = false;
    wantFocusFirstField_ = true;  // C37-F: focus first field on next draw
    aiBusy_ = false;
    aiMessage_.clear();
    aiHighlights_.clear();
    ++aiRequestSeq_;

    takeOpenHold(st);

    ips_ = st.ips();   // document-unit display follows the active unit system (MMGS / IPS)
    fields_.clear();
    fields_.resize(form->fields.size());
    for (size_t i = 0; i < form->fields.size(); ++i)
        initField(fields_[i], form->fields[i], ips_);
    // IPS: replace metric-seeded Dim defaults with clean native imperial values (0.25 in, not
    // 0.3937 in from 10 mm / 25.4). Metric mode keeps the form table defaults unchanged.
    if (ips_) applyIpsNativeDefaults(form_, fields_);


    // Thread form in an IPS document defaults to an SAE size (1/4-20 UNC) instead of
    // the metric first option; metric docs keep the M-series default (combo index 0).
    if (ips_ && form_->type && std::string(form_->type) == "thread") {
        for (auto& f : fields_) {
            if (f.def->kind != PMFieldKind::Combo || std::strcmp(f.def->key, "size") != 0)
                continue;
            int idx = pmComboIndexOf(*f.def, "1/4-20");
            if (idx >= 0) f.comboIndex = idx;
            break;
        }
    }

    // If a mode/op combo exists, preselect it from the command (boss/cut split button; boolean
    // combine/subtract/intersect). Match mode_ against the combo's option VALUES so any N-way combo
    // works (not just boss/cut), falling back to index 0 (legacy "cut" -> 1).
    if (!mode_.empty()) {
        for (auto& f : fields_) {
            if (f.def->kind != PMFieldKind::Combo) continue;
            if (std::strcmp(f.def->key, "mode") != 0 && std::strcmp(f.def->key, "op") != 0) continue;
            const std::string opts = f.def->comboOptions ? f.def->comboOptions : "";
            int idx = -1, cur = 0; size_t start = 0;
            while (start <= opts.size()) {
                size_t bar = opts.find('|', start);
                if (opts.substr(start, bar - start) == mode_) { idx = cur; break; }
                ++cur;
                if (bar == std::string::npos) break;
                start = bar + 1;
            }
            f.comboIndex = (idx >= 0) ? idx : ((mode_ == "cut") ? 1 : 0);
        }
    }

    // First selector well becomes active so the user can pick immediately.
    for (auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell) { f.wellActive = true; break; }

    // Seed any already-resolved viewport selection into the first well (SolidWorks pre-select).
    PMField* well = activeWell();
    if (well) {
        for (const auto& it : st.selection.items()) {
            if (it.pending_resolve || it.selector.empty()) continue;
            try {
                nlohmann::json selObj = nlohmann::json::parse(it.selector);
                int k = (int)it.hit.kind;
                routePick(selObj, k);
            } catch (...) {}
        }
    }

    // C40/C51: SolidWorks pre-select for extrude/revolve/sweep — the sketch chosen in the
    // FeatureManager (or, failing that, the most recent non-empty sketch) seeds the Profile well
    // as a WHOLE-SKETCH region ({kind:region, sketch:fid} with no centroid/index). Whole-sketch
    // resolves to all material (even-odd) regions, so a circle-in-a-square previews as a washer and
    // the old fragile centroid match (which produced spurious "dangling reference" errors on
    // concentric loops) is gone. The viewport region picker then lets the user narrow to specific
    // regions. Loft is NOT seeded — its sections are chosen explicitly (needs 2+, ordered).
    if (std::strcmp(form_->type, "extrude") == 0 || std::strcmp(form_->type, "revolve") == 0 ||
        std::strcmp(form_->type, "sweep") == 0) {
        PMField* prof = profileWell();
        const nlohmann::json& tree = st.docMirror.tree();
        if (prof && prof->picks.empty() && tree.is_object() &&
            tree.contains("features") && tree["features"].is_array()) {
            const nlohmann::json* pick = nullptr;   // just-exited/selected sketch wins, else last
            for (const auto& f : tree["features"]) {
                if (!f.is_object() || f.value("type", std::string()) != "sketch") continue;
                const auto& ents = f.contains("params") ? f["params"].value("entities", nlohmann::json::array())
                                                        : nlohmann::json::array();
                if (!ents.is_array() || ents.empty()) continue;   // empty sketch: nothing to extrude
                const std::string fidv = f.value("id", std::string());
                if (fidv == justExitedSketch || fidv == st.selectedFeatureId) { pick = &f; break; }
                pick = &f;
            }
            if (pick) {
                PMSelEntry se;
                se.selector = {{"kind", "region"}, {"sketch", pick->value("id", std::string())}};
                se.label = "Whole sketch";
                prof->picks.push_back(std::move(se));
                dirty_ = true;   // schedule the live preview against the seeded whole-sketch profile
                lastEditTime_ = ImGui::GetTime();
                st.addLog("[pm] profile seeded (whole sketch) from " + pick->value("id", std::string()));
            }
        }
    }

    // Pattern/mirror: seed the "features" FeatureWell from the FeatureManager selection so the
    // command operates immediately on the selected feature (SolidWorks pre-select). A sketch/datum
    // selection is skipped (nothing to replicate); an empty list leaves mirror as a whole-body
    // mirror and the patterns waiting for an "Add selected" pick.
    if (std::strcmp(form_->type, "mirror") == 0 ||
        std::strcmp(form_->type, "linear_pattern") == 0 ||
        std::strcmp(form_->type, "circular_pattern") == 0) {
        const std::string& sfid = st.selectedFeatureId;
        if (!sfid.empty()) {
            const nlohmann::json& tree = st.docMirror.tree();
            std::string nm = sfid, ftype;
            if (tree.is_object() && tree.contains("features") && tree["features"].is_array())
                for (const auto& tf : tree["features"])
                    if (tf.is_object() && tf.value("id", std::string()) == sfid) {
                        nm = tf.value("name", sfid); ftype = tf.value("type", std::string()); break;
                    }
            const bool replicable = !(ftype == "sketch" || ftype == "plane" || ftype == "axis" ||
                                      ftype == "point" || ftype == "csys" || ftype.empty());
            if (replicable)
                for (auto& fld : fields_)
                    if (fld.def->kind == PMFieldKind::FeatureWell) {
                        PMSelEntry e; e.selector = sfid; e.label = nm;
                        fld.picks.push_back(std::move(e));
                        dirty_ = true; lastEditTime_ = ImGui::GetTime();
                        break;
                    }
        }
    }

    // Weldments: seed the single "sketch" FeatureWell from a tree-selected SKETCH so a path picked
    // before the command auto-fills (SolidWorks pre-select). Non-sketch selections are ignored —
    // the user picks the path sketch in the FeatureManager then clicks "Add selected".
    if (std::strcmp(form_->type, "weld_member") == 0) {
        const std::string& sfid = st.selectedFeatureId;
        if (!sfid.empty()) {
            const nlohmann::json& tree = st.docMirror.tree();
            std::string nm = sfid, ftype;
            if (tree.is_object() && tree.contains("features") && tree["features"].is_array())
                for (const auto& tf : tree["features"])
                    if (tf.is_object() && tf.value("id", std::string()) == sfid) {
                        nm = tf.value("name", sfid); ftype = tf.value("type", std::string()); break;
                    }
            if (ftype == "sketch")
                for (auto& fld : fields_)
                    if (fld.def->kind == PMFieldKind::FeatureWell &&
                        std::strcmp(fld.def->key, "sketch") == 0) {
                        PMSelEntry e; e.selector = sfid; e.label = nm;
                        fld.picks.push_back(std::move(e));
                        dirty_ = true; lastEditTime_ = ImGui::GetTime();
                        break;
                    }
        }
    }

    // Plane pre-select (SolidWorks "select a plane, then run the command"): if a plane is selected —
    // a default origin plane ("plane.front/top/right") or a created datum-plane feature — seed the
    // form's first empty plane-accepting well with it (ref-plane base, mirror plane, draft neutral,
    // helix base, …). Routed as a BARE string, matching routeDatumPlane / the engine's datum resolve.
    {
        const std::string& sfid = st.selectedFeatureId;
        std::string label;
        bool isPlane = false;
        if (sfid == "plane.front" || sfid == "plane.top" || sfid == "plane.right") {
            isPlane = true;
            label = sfid.substr(sfid.rfind('.') + 1);
            if (!label.empty()) label[0] = (char)std::toupper((unsigned char)label[0]);
        } else if (!sfid.empty()) {
            const nlohmann::json& tree = st.docMirror.tree();
            if (tree.is_object() && tree.contains("features") && tree["features"].is_array())
                for (const auto& tf : tree["features"])
                    if (tf.is_object() && tf.value("id", std::string()) == sfid &&
                        tf.value("type", std::string()) == "plane") {
                        isPlane = true; label = tf.value("name", sfid); break;
                    }
        }
        if (isPlane)
            for (auto& fld : fields_)
                if (fld.def->kind == PMFieldKind::SelectorWell && fld.def->selKinds.plane &&
                    fld.picks.empty() && fieldVisible(*fld.def, fields_)) {
                    PMSelEntry e; e.selector = sfid; e.label = label;
                    fld.picks.push_back(std::move(e));
                    fld.wellDangling = false;
                    dirty_ = true; lastEditTime_ = ImGui::GetTime();
                    st.addLog("[pm] plane well seeded from preselected " + sfid);
                    break;
                }
    }

    st.addLog("[pm] open " + std::string(form_->type) +
              (mode_.empty() ? "" : " (" + mode_ + ")"));
    return true;
}

bool PropertyManager::openForEdit(AppState& st, const std::string& featureId) {
    const nlohmann::json& tree = st.docMirror.tree();
    if (!tree.is_object()) return false;
    auto feat = tree.find("features");
    if (feat == tree.end() || !feat->is_array()) return false;
    const nlohmann::json* target = nullptr;
    for (const auto& f : *feat) {
        if (f.is_object() && f.value("id", std::string()) == featureId) { target = &f; break; }
    }
    if (!target) return false;
    std::string type = target->value("type", std::string());
    const PMFormDef* form = pmFindForm(type);
    if (!form) { st.addLog("[pm] no form for feature type '" + type + "'"); return false; }
    if (guestActive_) closeGuest();
    if (active_ && !cancelEdits(st)) return false;

    active_ = true;
    editMode_ = true;
    form_ = form;
    mode_.clear();
    title_ = form->title;
    targetId_ = featureId;
    previewBatches_ = 0;
    previewUndoGroupStarted_ = false;
    dirty_ = false;
    firstPreviewDone_ = true;   // the feature already exists; edits go straight to edit_feature
    formSourceRevision_ = st.docMirror.revision();
    freshEditRequired_ = false;
    previewInFlight_ = false;
    lastEditTime_ = ImGui::GetTime();
    previewError_.clear();
    focusedOnOpen_ = false;
    wantFocusFirstField_ = true;  // C37-F: focus first field on next draw
    aiBusy_ = false;
    aiMessage_.clear();
    aiHighlights_.clear();
    ++aiRequestSeq_;

    takeOpenHold(st);

    ips_ = st.ips();   // document-unit display follows the active unit system (MMGS / IPS)
    fields_.clear();
    fields_.resize(form->fields.size());
    for (size_t i = 0; i < form->fields.size(); ++i)
        initField(fields_[i], form->fields[i], ips_);

    loadFromTree(*target);
    hydrateFilletFromMirror(st);

    // A fillet/chamfer is shown LIVE and APPLIED while editing — the session already has it applied,
    // so the rounded result is on screen the moment the PM opens. The original edges in the feature
    // are drawn as dashed 3D overlay lines (viewport_panel + collectActiveWellEdgeSegments), built
    // from each selector's geometry, so the user can see and click-to-deselect edges that have been
    // rounded away. No suppressed-base preview is needed.

    st.addLog("[pm] edit " + featureId + " (" + type + ")");
    return true;
}

void PropertyManager::presetFieldForShot(const std::string& key, const std::string& value) {
    if (!active_) return;
    for (auto& f : fields_) {
        if (!f.def->key || key != f.def->key) continue;
        switch (f.def->kind) {
            case PMFieldKind::Combo:
            case PMFieldKind::DatumRef: {
                std::string opts = f.def->comboOptions ? f.def->comboOptions : "";
                int cur = 0; size_t start = 0;
                while (start <= opts.size()) {
                    size_t bar = opts.find('|', start);
                    if (opts.substr(start, bar - start) == value) { f.comboIndex = cur; break; }
                    ++cur;
                    if (bar == std::string::npos) break;
                    start = bar + 1;
                }
                break;
            }
            case PMFieldKind::Check:
                f.checkValue = (value == "true" || value == "1" || value == "yes");
                break;
            case PMFieldKind::Dim: {
                try {
                    f.dimValue = std::stod(value); f.dimExpr.clear(); f.dimError.clear();
                    std::string disp = pmFieldValueText(f.def, f.dimValue, ips_);
                    std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
                } catch (...) {}
                break;
            }
            case PMFieldKind::Int:
                try { f.intValue = std::stoi(value); } catch (...) {}
                break;
            default: break;
        }
        return;
    }
}

bool PropertyManager::dragSetDim(const std::string& key, double value) {
    if (!active_) return false;
    for (auto& f : fields_) {
        if (f.def->kind != PMFieldKind::Dim || !f.def->key || key != f.def->key) continue;
        if (!fieldVisible(*f.def, fields_) || !fieldEnabled(*f.def, fields_)) return false;
        f.dimValue = value;
        f.dimExpr.clear();
        f.dimError.clear();
        f.dimExprRawString = false;
        f.dimValueKnown = true;
        f.dimTextDirty = f.dimTextEditing = false;
        f.dimInputIps = ips_;
        std::string disp = pmFieldValueText(f.def, f.dimValue, ips_);
        std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
        dirty_ = true;
        lastEditTime_ = ImGui::GetTime() - kDebounceSec;  // preview immediately (like a drag release)
        return true;
    }
    return false;
}

bool PropertyManager::dragSetCheck(const std::string& key, bool value) {
    if (!active_) return false;
    for (auto& f : fields_) {
        if (f.def->kind != PMFieldKind::Check || !f.def->key || key != f.def->key) continue;
        if (!fieldVisible(*f.def, fields_) || !fieldEnabled(*f.def, fields_)) return false;
        if (f.checkValue == value) return true;  // already set — still a hit for the caller
        f.checkValue = value;
        dirty_ = true;
        lastEditTime_ = ImGui::GetTime() - kDebounceSec;  // preview immediately (like dragSetDim)
        return true;
    }
    return false;
}

bool PropertyManager::dragGetCheck(const std::string& key, bool& out) const {
    if (!active_) return false;
    for (const auto& f : fields_) {
        if (f.def->kind != PMFieldKind::Check || !f.def->key || key != f.def->key) continue;
        if (!fieldVisible(*f.def, fields_) || !fieldEnabled(*f.def, fields_)) return false;
        out = f.checkValue;
        return true;
    }
    return false;
}

void PropertyManager::hydrateFilletFromMirror(AppState& st) {
    if (!form_ || std::strcmp(form_->type, "fillet") != 0 ||
        !st.docMirror.featureStatusCurrent() || !st.docMirror.featureStatus().is_array()) return;
    for (const auto& status : st.docMirror.featureStatus()) {
        if (!status.is_object() || status.value("id", std::string()) != targetId_ ||
            !status.value("ok", false) || !status.contains("data") || !status["data"].is_object())
            continue;
        auto fillet = status["data"].find("fillet");
        if (fillet != status["data"].end()) hydrateFilletEvidence(*fillet);
        break;
    }
}

// ---- in-viewport callouts (C27-A) -----------------------------------------

// Pull a world-space anchor from a selector entry: prefer the selector's own "centroid"
// (resolve_pick stamps a world centroid on every face/edge selector); fall back to the
// AppState topo-id centroid caches keyed by (body_id<<32 | topo_id) when absent.
static bool anchorFromSel(AppState& st, const PMSelEntry& e, bool wantEdge, float out[3]) {
    const nlohmann::json& s = e.selector;
    // Datum-plane / feature-id picks store a bare string selector — no centroid/hint to anchor on
    // (and .value() below would throw on a non-object). They carry no in-viewport callout anchor.
    if (!s.is_object()) return false;
    auto c = s.find("centroid");
    if (c != s.end() && c->is_array() && c->size() == 3 &&
        (*c)[0].is_number() && (*c)[1].is_number() && (*c)[2].is_number()) {
        out[0] = (float)(*c)[0].get<double>();
        out[1] = (float)(*c)[1].get<double>();
        out[2] = (float)(*c)[2].get<double>();
        return true;
    }
    // Cache fallback: hint_index is 0-based; cache keys are 1-based AICM topo ids.
    int hint = s.value("hint_index", -1);
    if (hint < 0) return false;
    uint64_t key = (uint64_t)1 << 32 | (uint64_t)(hint + 1);  // body 1 (single-part edit path)
    if (wantEdge) {
        auto it = st.edgeCentroidCache.find(key);
        if (it == st.edgeCentroidCache.end()) return false;
        out[0] = it->second[0]; out[1] = it->second[1]; out[2] = it->second[2];
        return true;
    }
    auto it = st.faceNormalCache.find(key);  // {nx,ny,nz, cx,cy,cz}
    if (it == st.faceNormalCache.end()) return false;
    out[0] = it->second[3]; out[1] = it->second[4]; out[2] = it->second[5];
    return true;
}

std::vector<PMCalloutItem> PropertyManager::calloutItems(AppState& st) const {
    std::vector<PMCalloutItem> out;
    if (!active_ || !form_) return out;

    // Resolve a single shared anchor for the feature: first edge ref's centroid, else first
    // face ref's centroid, else none. Edges win (dress-up features point at an edge).
    float anchor[3] = {0, 0, 0};
    bool hasAnchor = false;
    for (const auto& f : fields_) {
        if (f.def->kind != PMFieldKind::SelectorWell || f.picks.empty()) continue;
        if (!f.def->selKinds.edge) continue;
        if (anchorFromSel(st, f.picks.front(), /*wantEdge=*/true, anchor)) { hasAnchor = true; break; }
    }
    if (!hasAnchor) {
        for (const auto& f : fields_) {
            if (f.def->kind != PMFieldKind::SelectorWell || f.picks.empty()) continue;
            if (!f.def->selKinds.face) continue;
            if (anchorFromSel(st, f.picks.front(), /*wantEdge=*/false, anchor)) { hasAnchor = true; break; }
        }
    }

    for (const auto& f : fields_) {
        if (f.def->kind != PMFieldKind::Dim) continue;
        if (!fieldVisible(*f.def, fields_)) continue;
        if (!fieldEnabled(*f.def, fields_)) continue;   // skip greyed / coming-soon dims
        PMCalloutItem it;
        it.label = f.def->label;
        it.value = f.dimExpr.empty()
            ? (pmFieldUnitless(f.def) ? ui::kit::fmtValue(f.dimValue)
               : pmFieldIsAngle(f.def) ? ui::kit::fmtQuantity(f.dimValue, "deg")
               : ui::kit::fmtQuantity(ips_ ? f.dimValue / 25.4 : f.dimValue, ips_ ? "in" : "mm"))
            : ("=" + f.dimExpr);
        it.hasAnchor = hasAnchor;
        if (hasAnchor) { it.anchor[0] = anchor[0]; it.anchor[1] = anchor[1]; it.anchor[2] = anchor[2]; }
        out.push_back(std::move(it));
    }
    return out;
}

void PropertyManager::close() {
    if (holdRpc_ && !holdDocId_.empty())
        holdRpc_->callPrimary("edit_release", {{"doc_id", holdDocId_}}, {});
    ++editSessionGeneration_;
    rpcLifetime_.reset();
    cancelPending_ = cancelInFlight_ = false;
    checkpointInFlight_ = holdInFlight_ = false;
    holdDocId_.clear();
    sessionDocId_.clear();
    active_ = false;
    editMode_ = false;
    form_ = nullptr;
    fields_.clear();
    mode_.clear();
    title_.clear();
    targetId_.clear();
    legacyFillet_ = legacyFilletModeKnown_ = false;
    previewBatches_ = 0;
    dirty_ = false;
    firstPreviewDone_ = false;
    formSourceRevision_ = -1;
    freshEditRequired_ = false;
    // Any drained callback balances the app-wide count before its session/lifetime guard.
    previewInFlight_ = false;
    acceptRequested_ = acceptPending_ = false;
    checkpointId_.clear();    // C37-F
    focusedOnOpen_ = false;   // C37-F
    wantFocusFirstField_ = false; // C37-F
    previewError_.clear();    // C37-F
    previewWarning_.clear();
    committing_ = false;
    aiBusy_ = false;
    aiMessage_.clear();
    aiHighlights_.clear();
    ++aiRequestSeq_;
}


// ---- selector wells -------------------------------------------------------

PMField* PropertyManager::activeWell() {
    for (auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell && f.wellActive &&
            fieldVisible(*f.def, fields_)) return &f;
    return nullptr;
}

void PropertyManager::advanceWellAfterFill(PMField* filled) {
    // Only single-ref wells advance; a multi-pick well stays active to gather more refs.
    if (!filled || filled->def->selMulti || filled->picks.empty()) return;
    // Find the next EMPTY visible selector well after `filled` (in field order); if none follows,
    // wrap to the first empty one (so a 2-well form fills the other well no matter the click order).
    bool seenFilled = false;
    PMField* next = nullptr;
    PMField* firstEmpty = nullptr;
    for (auto& f : fields_) {
        if (f.def->kind != PMFieldKind::SelectorWell || !fieldVisible(*f.def, fields_)) continue;
        const bool empty = f.picks.empty();
        if (&f == filled) { seenFilled = true; continue; }
        if (empty && !firstEmpty) firstEmpty = &f;
        if (empty && seenFilled && !next) next = &f;
    }
    PMField* target = next ? next : firstEmpty;
    if (!target) return;                    // no empty well left — keep focus where it is
    for (auto& o : fields_)
        if (o.def->kind == PMFieldKind::SelectorWell || o.def->kind == PMFieldKind::FeatureWell)
            o.wellActive = false;
    target->wellActive = true;
}

bool PropertyManager::routePick(const nlohmann::json& selector, int pickKind,
                                uint32_t pickBody, uint32_t pickTopo) {
    PMField* well = activeWell();
    if (!well || !selector.is_object()) return false;
    // Filter by allowed kinds.
    render::PickKind k = (render::PickKind)pickKind;
    const PMSelKinds& ak = well->def->selKinds;
    bool allowed = (k == render::PickKind::Face   && ak.face) ||
                   (k == render::PickKind::Edge   && ak.edge) ||
                   (k == render::PickKind::Vertex && ak.vertex);
    // Body wells (boolean): the selection observer resolved a body selector (kind="body") from
    // whatever element was clicked — accept it regardless of the underlying pick kind.
    if (!allowed && ak.body && selector.value("kind", std::string()) == "body") allowed = true;
    if (!allowed) return false;

    // De-dup by hint_index + centroid (same element picked twice toggles off — SolidWorks).
    auto same = [&](const nlohmann::json& a, const nlohmann::json& b) {
        // A topology pick (object) never equals a datum-plane pick (bare string), and .value()
        // would throw on the string — so a mixed well just replaces (single) / appends (multi).
        if (!a.is_object() || !b.is_object()) return false;
        // hint_index uses the SAME absent-sentinel on both sides — with asymmetric defaults
        // (-1 vs -2) two picks that both lack a hint_index never compared equal, so a re-pick of a
        // hint-less selector couldn't toggle off. The centroid term still distinguishes distinct
        // elements that happen to share a missing hint_index.
        return a.value("kind", std::string()) == b.value("kind", std::string()) &&
               a.value("hint_index", -1) == b.value("hint_index", -1) &&
               a.value("centroid", nlohmann::json()) == b.value("centroid", nlohmann::json());
    };
    for (size_t i = 0; i < well->picks.size(); ++i) {
        if (same(well->picks[i].selector, selector)) {
            well->picks.erase(well->picks.begin() + i);
            dirty_ = true; lastEditTime_ = ImGui::GetTime();
            return true;
        }
    }
    if (!well->def->selMulti) well->picks.clear();   // single-ref wells hold one pick
    PMSelEntry e; e.selector = selector;
    std::string kind = selector.value("kind", std::string("ref"));
    e.label = kind.empty() ? "Ref" : std::string(1, (char)std::toupper((unsigned char)kind[0])) + kind.substr(1);
    e.pickBody = pickBody; e.pickTopo = pickTopo; e.pickKind = pickKind;  // for viewport highlight
    well->picks.push_back(e);
    well->wellDangling = false;
    dirty_ = true; lastEditTime_ = ImGui::GetTime();

    // THREAD AUTO-SIZE: picking a cylindrical face into the thread form's target well
    // snaps the Size combo to the nearest standard thread for that diameter (a 5/16
    // shaft → 5/16-18). The pick selector already carries the kernel-computed radius
    // (GSL Stage 0), so no engine round-trip is needed. A shaft is compared against the
    // MAJOR Ø (turned slightly undersize is expected); a bore (concavity=="bore")
    // against the basic minor Ø. Sizes from the doc's unit system are preferred
    // (SAE in IPS, metric in MMGS), falling back to the other family on a clear miss.
    if (form_ && form_->type && std::string(form_->type) == "thread" &&
        std::strcmp(well->def->key, "target_face") == 0 &&
        selector.value("surf", std::string()) == "cylinder" &&
        selector.contains("radius") && selector["radius"].is_number()) {
        bool customSize = false;
        for (const auto& f : fields_)
            if (std::strcmp(f.def->key, "custom_size") == 0) customSize = f.checkValue;
        if (!customSize) {
            const double dia = 2.0 * selector["radius"].get<double>();
            const bool bore = selector.value("concavity", std::string()) == "bore";
            auto bestIn = [&](bool metric, const PMThreadSize*& hit, double& err) {
                for (const auto& t : kPmThreadSizes) {
                    if (t.metric != metric) continue;
                    // Reference Ø this pick should measure near: major for a shaft,
                    // ISO/UN basic minor (major − 1.0825·pitch) for a bore.
                    const double ref = bore ? t.major - 1.0825 * t.pitch : t.major;
                    const double d = std::abs(dia - ref);
                    if (d < err) { err = d; hit = &t; }
                }
            };
            const PMThreadSize* hit = nullptr; double err = 1e300;
            bestIn(!ips_, hit, err);
            // Accept within a band that covers undersize-turned shafts / drilled bores
            // but rejects a clearly off-family diameter; else try the other family.
            auto tol = [](const PMThreadSize& t) { return std::max(0.25, 0.05 * t.major); };
            if (!hit || err > tol(*hit)) { hit = nullptr; err = 1e300; bestIn(ips_, hit, err); }
            if (hit && err <= tol(*hit)) {
                for (auto& f : fields_) {
                    if (f.def->kind != PMFieldKind::Combo ||
                        std::strcmp(f.def->key, "size") != 0) continue;
                    int idx = pmComboIndexOf(*f.def, hit->value);
                    if (idx >= 0) {
                        f.comboIndex = idx;
                        f.comboUnsupported.clear();
                    }
                    break;
                }
            }
        }
    }

    advanceWellAfterFill(well);
    return true;
}

void PropertyManager::collectActiveWellHighlights(std::vector<std::array<uint32_t, 3>>& out) const {
    for (const auto& f : fields_) {
        if (f.def->kind != PMFieldKind::SelectorWell || !f.wellActive ||
            !fieldVisible(*f.def, fields_))
            continue;
        // Only edge/face/vertex topology wells carry highlightable picks (skip body/datum wells).
        if (!(f.def->selKinds.edge || f.def->selKinds.face || f.def->selKinds.vertex)) return;
        for (const auto& p : f.picks)
            if (p.pickKind >= 0)
                out.push_back({p.pickBody, p.pickTopo, (uint32_t)p.pickKind});
        return;  // a single well is active at a time
    }
}

void PropertyManager::collectActiveWellEdgeSegments(std::vector<EdgeSeg>& out) const {
    if (!active_ || !form_) return;
    const std::string ftype = form_->type ? form_->type : "";
    if (ftype != "fillet" && ftype != "chamfer") return;  // dashed old-edge overlay is blend-only
    for (const auto& f : fields_) {
        if (f.def->kind != PMFieldKind::SelectorWell || !f.wellActive ||
            !fieldVisible(*f.def, fields_) || !f.def->selKinds.edge)
            continue;
        for (int i = 0; i < (int)f.picks.size(); ++i) {
            const nlohmann::json& sel = f.picks[i].selector;
            if (!sel.is_object() || !sel.contains("centroid") || !sel["centroid"].is_array() ||
                sel["centroid"].size() < 3)
                continue;
            EdgeSeg seg;
            seg.pickIndex = i;
            const auto& c = sel["centroid"];
            float cx = c[0].get<float>(), cy = c[1].get<float>(), cz = c[2].get<float>();
            const std::string surf = sel.value("surf", std::string());
            const bool hasAxis = sel.contains("axis") && sel["axis"].is_array() &&
                                 sel["axis"].size() == 3;
            const bool hasLen = !sel.value("area_or_len", nlohmann::json()).is_null();
            if (surf == "line" && hasAxis && hasLen) {
                // Straight edge: reconstruct the segment from centroid ± half-length along its axis.
                float ax = sel["axis"][0].get<float>(), ay = sel["axis"][1].get<float>(),
                      az = sel["axis"][2].get<float>();
                float half = 0.5f * sel["area_or_len"].get<float>();
                seg.pts.push_back({cx - ax * half, cy - ay * half, cz - az * half});
                seg.pts.push_back({cx + ax * half, cy + ay * half, cz + az * half});
                seg.closed = false;
            } else if (surf == "circle" && hasAxis && hasLen) {
                // Circular edge (the revolve case): centroid is the center, area_or_len is the
                // circumference (=2πr), axis is the circle normal. Sample a closed loop in the plane
                // perpendicular to the axis so the dashed overlay traces the real edge.
                float nx = sel["axis"][0].get<float>(), ny = sel["axis"][1].get<float>(),
                      nz = sel["axis"][2].get<float>();
                float nl = std::sqrt(nx * nx + ny * ny + nz * nz);
                float r = sel["area_or_len"].get<float>() / (2.0f * 3.14159265358979f);
                if (nl > 1e-6f && r > 1e-6f) {
                    nx /= nl; ny /= nl; nz /= nl;
                    // Two basis vectors spanning the circle plane (u ⟂ n, v = n × u).
                    float rx = (std::fabs(nx) < 0.9f) ? 1.0f : 0.0f;
                    float ry = (std::fabs(nx) < 0.9f) ? 0.0f : 1.0f;
                    float ux = ny * 0.0f - nz * ry, uy = nz * rx - nx * 0.0f, uz = nx * ry - ny * rx;
                    float ul = std::sqrt(ux * ux + uy * uy + uz * uz);
                    if (ul > 1e-6f) {
                        ux /= ul; uy /= ul; uz /= ul;
                        float vx = ny * uz - nz * uy, vy = nz * ux - nx * uz, vz = nx * uy - ny * ux;
                        const int N = 48;
                        seg.pts.reserve(N);
                        for (int k = 0; k < N; ++k) {
                            float t = (float)k / (float)N * 2.0f * 3.14159265358979f;
                            float ct = std::cos(t) * r, stt = std::sin(t) * r;
                            seg.pts.push_back({cx + ux * ct + vx * stt, cy + uy * ct + vy * stt,
                                               cz + uz * ct + vz * stt});
                        }
                        seg.closed = true;
                    }
                }
                if (seg.pts.empty()) { seg.pts.push_back({cx, cy, cz}); seg.closed = false; }
            } else {
                // Arc/spline/ellipse we can't reconstruct from the selector: mark the centroid so the
                // viewport still draws a small ring there as a deselect target.
                seg.pts.push_back({cx, cy, cz});
                seg.closed = false;
            }
            out.push_back(seg);
        }
        return;  // a single well is active at a time
    }
}

bool PropertyManager::removeActiveWellPick(int pickIndex) {
    for (auto& f : fields_) {
        if (f.def->kind != PMFieldKind::SelectorWell || !f.wellActive ||
            !fieldVisible(*f.def, fields_) || !f.def->selKinds.edge)
            continue;
        if (pickIndex < 0 || pickIndex >= (int)f.picks.size()) return false;
        f.picks.erase(f.picks.begin() + pickIndex);
        dirty_ = true; lastEditTime_ = ImGui::GetTime();
        return true;
    }
    return false;
}

bool PropertyManager::activeWellWantsBody() const {
    for (const auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell && f.wellActive &&
            fieldVisible(*f.def, fields_))
            return f.def->selKinds.body;
    return false;
}

bool PropertyManager::activeWellWantsPlane() const {
    for (const auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell && f.wellActive &&
            fieldVisible(*f.def, fields_))
            return f.def->selKinds.plane;
    return false;
}

bool PropertyManager::activeWellWantsAxis() const {
    for (const auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell && f.wellActive &&
            fieldVisible(*f.def, fields_))
            return f.def->selKinds.axis;
    return false;
}

// Returns true when the active selector well accepts a point datum.
bool PropertyManager::activeWellWantsPoint() const {
    for (const auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell && f.wellActive &&
            fieldVisible(*f.def, fields_))
            return f.def->selKinds.point;
    return false;
}

bool PropertyManager::activeWellPrefersEdge() const {
    for (const auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell && f.wellActive &&
            fieldVisible(*f.def, fields_))
            // Pure edge well (edge, not face) -> snap to nearby edges. A face-or-edge well stays on
            // the exact-pixel pick so face selection isn't hijacked by a nearby edge.
            return f.def->selKinds.edge && !f.def->selKinds.face;
    return false;
}

bool PropertyManager::activeWellWantsVertex() const {
    for (const auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell && f.wellActive &&
            fieldVisible(*f.def, fields_))
            return f.def->selKinds.vertex;
    return false;
}

bool PropertyManager::activeWellExpandsFaceToEdges() const {
    // SolidWorks "fillet a whole face": clicking a FACE while a fillet/chamfer edge well is active
    // adds ALL of that face's edges. Only for blend edge wells (edge-only, not face/body wells).
    if (!form_) return false;
    const std::string t = form_->type ? form_->type : "";
    if (t != "fillet" && t != "chamfer") return false;
    for (const auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell && f.wellActive &&
            fieldVisible(*f.def, fields_))
            return f.def->selKinds.edge && !f.def->selKinds.face;
    return false;
}

bool PropertyManager::routeDatumAxis(AppState& st, const std::string& fid,
                                     const std::string& label) {
    PMField* well = activeWell();
    if (!well || !well->def->selKinds.axis || fid.empty()) return false;

    auto isSameAxisPick = [&](const nlohmann::json& sel) -> bool {
        if (!sel.is_object()) return false;
        // Reserved / feature datum form, or explicit auto-axis form tagged with feature id.
        const std::string k = sel.value("kind", std::string());
        if ((k == "datum" || k == "axis") && sel.value("feature", std::string()) == fid)
            return true;
        return false;
    };

    // Toggle off if this exact datum axis is already in the well (SolidWorks re-pick).
    for (size_t i = 0; i < well->picks.size(); ++i) {
        if (isSameAxisPick(well->picks[i].selector)) {
            well->picks.erase(well->picks.begin() + i);
            dirty_ = true; lastEditTime_ = ImGui::GetTime();
            return true;
        }
    }
    if (!well->def->selMulti) well->picks.clear();   // single-ref wells hold one pick

    PMSelEntry e;
    e.label = label.empty() ? fid : label;

    // Host-only auto plane-intersection axes (axis.auto:…) are NOT in the engine reserved
    // set — store explicit geometry so document.cpp:axis_from_explicit can resolve them.
    // Geometry comes from the inject cache / renderer (never re-solve plane pairs here).
    if (fid.rfind("axis.auto:", 0) == 0) {
        auto emitExplicit = [&](float ox, float oy, float oz,
                                float dx, float dy, float dz) {
            e.selector = {
                {"kind", "axis"},
                {"feature", fid},
                {"point", {ox, oy, oz}},
                {"axis",  {dx, dy, dz}}
            };
            well->picks.push_back(e);
            well->wellDangling = false;
            dirty_ = true; lastEditTime_ = ImGui::GetTime();
            advanceWellAfterFill(well);
        };
        bool ok = false;
        for (const auto& ent : st.axisAutoCache) {
            if (ent.id != fid) continue;
            emitExplicit(ent.origin[0], ent.origin[1], ent.origin[2],
                         ent.dir[0], ent.dir[1], ent.dir[2]);
            ok = true;
            break;
        }
        if (!ok && st.renderer) {
            for (const auto& d : st.renderer->refDatums()) {
                if (d.kind != Renderer::RefDatum::Kind::Axis || d.id != fid) continue;
                emitExplicit(d.origin.x, d.origin.y, d.origin.z,
                             d.dir.x, d.dir.y, d.dir.z);
                ok = true;
                break;
            }
        }
        if (ok) return true;
        // If geometry is unavailable, fall through to datum form (chip still shows).
    }

    // Reserved axis.x/y/z + user axis features: engine axis_from_datum / resolve_axis_ref.
    e.selector = {{"kind", "datum"}, {"feature", fid}};
    well->picks.push_back(e);
    well->wellDangling = false;
    dirty_ = true; lastEditTime_ = ImGui::GetTime();
    advanceWellAfterFill(well);
    return true;
}

bool PropertyManager::routeDatumPlane(const std::string& datumId, const std::string& labelIn) {
    PMField* well = activeWell();
    if (!well || !well->def->selKinds.plane || datumId.empty()) return false;
    // Toggle off if this exact datum plane is already in the well (SolidWorks re-pick).
    for (size_t i = 0; i < well->picks.size(); ++i) {
        const auto& sel = well->picks[i].selector;
        if (sel.is_string() && sel.get<std::string>() == datumId) {
            well->picks.erase(well->picks.begin() + i);
            dirty_ = true; lastEditTime_ = ImGui::GetTime();
            return true;
        }
    }
    if (!well->def->selMulti) well->picks.clear();   // single-ref wells hold one pick
    // Chip label: the datum's own name when given (created plane "Plane1"); else a short label
    // derived from the reserved id ("plane.front" -> "Front").
    std::string label = labelIn;
    if (label.empty()) {
        label = datumId;
        auto dot = label.rfind('.');
        if (dot != std::string::npos) label = label.substr(dot + 1);
        if (!label.empty()) label[0] = (char)std::toupper((unsigned char)label[0]);
    }
    PMSelEntry e;
    e.selector = datumId;   // bare reserved-datum string; _SEL_OR_DATUM slots accept it
    e.label = label;
    well->picks.push_back(e);
    well->wellDangling = false;
    dirty_ = true; lastEditTime_ = ImGui::GetTime();
    advanceWellAfterFill(well);
    return true;
}

// Route a point-datum (world origin or user-created point datum) into the active
// selector well.  Uses the {kind:"datum",feature:fid} selector so resolve_point in
// compile_document() picks it up.  "point.origin" is the canonical world-origin id.
bool PropertyManager::routeDatumPoint(const std::string& fid, const std::string& label) {
    PMField* well = activeWell();
    if (!well || !well->def->selKinds.point || fid.empty()) return false;
    // Toggle off if this exact datum point is already in the well (SolidWorks re-pick).
    for (size_t i = 0; i < well->picks.size(); ++i) {
        const auto& sel = well->picks[i].selector;
        if (sel.is_object() && sel.value("kind", std::string()) == "datum" &&
            sel.value("feature", std::string()) == fid) {
            well->picks.erase(well->picks.begin() + i);
            dirty_ = true; lastEditTime_ = ImGui::GetTime();
            return true;
        }
    }
    if (!well->def->selMulti) well->picks.clear();   // single-ref wells hold one pick
    PMSelEntry e;
    e.selector = {{"kind", "datum"}, {"feature", fid}};  // resolve_point accepts this
    e.label = label.empty() ? fid : label;
    well->picks.push_back(e);
    well->wellDangling = false;
    dirty_ = true; lastEditTime_ = ImGui::GetTime();
    advanceWellAfterFill(well);
    return true;
}

std::string PropertyManager::activeWellKindHint() const {
    for (const auto& f : fields_) {
        if (f.def->kind != PMFieldKind::SelectorWell || !f.wellActive ||
            !fieldVisible(*f.def, fields_)) continue;
        const PMSelKinds& k = f.def->selKinds;
        std::vector<const char*> parts;
        if (k.face)   parts.push_back("faces");
        if (k.edge)   parts.push_back("edges");
        if (k.vertex) parts.push_back("vertices");
        if (k.body)   parts.push_back("bodies");
        if (k.plane)  parts.push_back("planes");
        if (k.axis)   parts.push_back("axes");
        if (k.point)  parts.push_back("points");
        std::string s;
        for (size_t i = 0; i < parts.size(); ++i) {
            if (i) s += (i + 1 == parts.size()) ? " or " : ", ";
            s += parts[i];
        }
        return s;
    }
    return "";
}

// ---- C51: sketched-feature region picking ---------------------------------

// Short chip label for a region/face selector (region picks read "Whole sketch" / "Region N").
static std::string pmRegionLabel(const nlohmann::json& sel) {
    std::string kind = sel.value("kind", std::string("ref"));
    if (kind == "region") {
        if (sel.contains("index") && sel["index"].is_number())
            return "Region " + std::to_string((int)sel["index"].get<double>() + 1);
        return "Whole sketch";
    }
    if (kind.empty()) return "Ref";
    return std::string(1, (char)std::toupper((unsigned char)kind[0])) + kind.substr(1);
}

PMField* PropertyManager::profileWell() {
    for (auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell &&
            (std::strcmp(f.def->key, "profile") == 0 || std::strcmp(f.def->key, "sections") == 0))
            return &f;
    return nullptr;
}

const PMField* PropertyManager::profileWell() const {
    for (const auto& f : fields_)
        if (f.def->kind == PMFieldKind::SelectorWell &&
            (std::strcmp(f.def->key, "profile") == 0 || std::strcmp(f.def->key, "sections") == 0))
            return &f;
    return nullptr;
}

bool PropertyManager::activeWellWantsPathFeature() const {
    if (!active_) return false;
    // activeWell() is non-const; inline the same lookup here.
    for (const auto& f : fields_) {
        if (f.def->kind != PMFieldKind::SelectorWell || !f.wellActive ||
            !fieldVisible(*f.def, fields_)) continue;
        return std::strcmp(f.def->key, "path") == 0 ||
               std::strcmp(f.def->key, "centerline") == 0;
    }
    return false;
}

bool PropertyManager::routePathFeature(const std::string& fid, const std::string& name,
                                      bool isCurve) {
    PMField* well = activeWell();
    if (!well || fid.empty()) return false;
    if (std::strcmp(well->def->key, "path") != 0 &&
        std::strcmp(well->def->key, "centerline") != 0) return false;
    // Schema-valid selector: a sketch path is a region (its wire = spine); a curve/helix
    // path is a datum-feature ref. Both resolve to `fid` via selector_owner_id.
    nlohmann::json sel = isCurve ? nlohmann::json{{"kind", "datum"}, {"feature", fid}}
                                 : nlohmann::json{{"kind", "region"}, {"sketch", fid}};
    auto owner = [](const nlohmann::json& s) -> std::string {
        if (!s.is_object()) return std::string();
        if (s.contains("feature") && s["feature"].is_string()) return s["feature"];
        if (s.contains("sketch") && s["sketch"].is_string()) return s["sketch"];
        return std::string();
    };
    // Toggle off if this exact feature already fills the path (SolidWorks re-pick).
    for (size_t i = 0; i < well->picks.size(); ++i) {
        if (owner(well->picks[i].selector) == fid) {
            well->picks.erase(well->picks.begin() + i);
            dirty_ = true; lastEditTime_ = ImGui::GetTime();
            return true;
        }
    }
    well->picks.clear();   // path / centerline are single-ref
    PMSelEntry e;
    e.selector = std::move(sel);
    e.label = name.empty() ? fid : name;
    well->picks.push_back(std::move(e));
    well->wellDangling = false;
    dirty_ = true; lastEditTime_ = ImGui::GetTime();
    advanceWellAfterFill(well);
    return true;
}

std::vector<std::string> PropertyManager::pathHighlightFeatureIds() const {
    std::vector<std::string> out;
    if (!active_) return out;
    for (const auto& f : fields_) {
        if (f.def->kind != PMFieldKind::SelectorWell) continue;
        const bool isPath = std::strcmp(f.def->key, "path") == 0 ||
                            std::strcmp(f.def->key, "centerline") == 0 ||
                            std::strcmp(f.def->key, "guides") == 0;
        if (!isPath) continue;
        for (const auto& p : f.picks) {
            const auto& s = p.selector;
            if (!s.is_object()) continue;
            if (s.contains("feature") && s["feature"].is_string()) out.push_back(s["feature"]);
            else if (s.contains("sketch") && s["sketch"].is_string()) out.push_back(s["sketch"]);
        }
    }
    return out;
}

bool PropertyManager::wantsRegionPicker() const {
    return active_ && profileWell() != nullptr;
}

bool PropertyManager::sectionsAreOrdered() const {
    const PMField* w = profileWell();
    return w && std::strcmp(w->def->key, "sections") == 0;
}

std::vector<nlohmann::json> PropertyManager::profilePicks() const {
    std::vector<nlohmann::json> out;
    if (const PMField* w = profileWell())
        for (const auto& p : w->picks) out.push_back(p.selector);
    return out;
}

void PropertyManager::setProfilePicks(AppState& st, const std::vector<nlohmann::json>& sels) {
    (void)st;
    PMField* w = profileWell();
    if (!w) return;
    w->picks.clear();
    for (const auto& s : sels) {
        PMSelEntry e; e.selector = s; e.label = pmRegionLabel(s);
        w->picks.push_back(std::move(e));
    }
    w->wellDangling = false;
    // A region click re-enters the select session (user is actively picking contours).
    for (auto& o : fields_)
        if (o.def->kind == PMFieldKind::SelectorWell ||
            o.def->kind == PMFieldKind::FeatureWell)
            o.wellActive = false;
    w->wellActive = true;
    suppressWellAutoArm_ = false;
    dirty_ = true; lastEditTime_ = ImGui::GetTime();
}

void PropertyManager::endProfileSelection() {
    if (!active_) return;
    for (auto& f : fields_) {
        if (f.def->kind == PMFieldKind::SelectorWell ||
            f.def->kind == PMFieldKind::FeatureWell)
            f.wellActive = false;
    }
    // Stop the always-on re-home from re-arming next frame (would undo the dismiss).
    suppressWellAutoArm_ = true;
}


bool PropertyManager::hasActiveSelectorWell() const {
    if (!active_) return false;
    for (const auto& f : fields_) {
        if ((f.def->kind == PMFieldKind::SelectorWell ||
             f.def->kind == PMFieldKind::FeatureWell) &&
            f.wellActive && fieldVisible(*f.def, fields_))
            return true;
    }
    return false;
}



std::vector<std::string> PropertyManager::regionCandidateSketches(AppState& st) const {
    std::vector<std::string> out;
    const PMField* w = profileWell();
    if (!w) return out;
    auto pushUnique = [&](const std::string& s) {
        if (s.empty()) return;
        for (const auto& e : out) if (e == s) return;
        out.push_back(s);
    };
    // Sketches already referenced by current picks (preserve order).
    for (const auto& p : w->picks)
        if (p.selector.value("kind", std::string()) == "region")
            pushUnique(p.selector.value("sketch", std::string()));
    const bool sections = std::strcmp(w->def->key, "sections") == 0;
    if (!out.empty() && !sections) return out;   // single-profile well is bound to its sketch
    // Otherwise enumerate non-empty unsuppressed sketches from the tree (loft / unseeded profile).
    const nlohmann::json& tree = st.docMirror.tree();
    if (tree.is_object() && tree.contains("features") && tree["features"].is_array()) {
        for (const auto& f : tree["features"]) {
            if (!f.is_object() || f.value("type", std::string()) != "sketch") continue;
            if (f.value("suppressed", false)) continue;
            const auto& ents = f.contains("params")
                ? f["params"].value("entities", nlohmann::json::array())
                : nlohmann::json::array();
            if (!ents.is_array() || ents.empty()) continue;
            pushUnique(f.value("id", std::string()));
        }
    }
    return out;
}

nlohmann::json PropertyManager::buildAiAssistPayload(AppState& st) const {
    nlohmann::json schema = {
        {"type", form_ ? form_->type : ""},
        {"title", title_},
        {"mode", mode_},
        {"fields", nlohmann::json::array()},
    };
    nlohmann::json current = {{"fields", nlohmann::json::object()}};

    for (const auto& f : fields_) {
        const bool visible = fieldVisible(*f.def, fields_);
        const bool enabled = fieldEnabled(*f.def, fields_);
        nlohmann::json sf = {
            {"key", f.def->key},
            {"label", f.def->label},
            {"group", f.def->group},
            {"kind", aiKindName(f.def->kind)},
            {"visible", visible},
            {"enabled", enabled},
        };
        if (f.def->kind == PMFieldKind::Combo || f.def->kind == PMFieldKind::DatumRef)
            sf["options"] = comboOptionsList(*f.def);
        if (f.def->kind == PMFieldKind::SelectorWell) {
            sf["multi"] = f.def->selMulti;
            sf["accepts"] = {
                {"face", f.def->selKinds.face}, {"edge", f.def->selKinds.edge},
                {"vertex", f.def->selKinds.vertex}, {"body", f.def->selKinds.body},
                {"plane", f.def->selKinds.plane},
            };
        }
        schema["fields"].push_back(sf);

        nlohmann::json cf = {
            {"kind", aiKindName(f.def->kind)},
            {"visible", visible},
            {"enabled", enabled},
        };
        switch (f.def->kind) {
            case PMFieldKind::Dim:
                cf["value"] = f.dimValue;
                cf["text"] = std::string(f.dimBuf.data());
                cf["error"] = f.dimError;
                break;
            case PMFieldKind::Combo:
            case PMFieldKind::DatumRef:
                cf["value"] = comboValue(fields_, f.def->key);
                break;
            case PMFieldKind::Check:
                cf["value"] = f.checkValue;
                break;
            case PMFieldKind::Int:
                cf["value"] = f.intValue;
                break;
            case PMFieldKind::SelectorWell:
            case PMFieldKind::FeatureWell:
                cf["count"] = (int)f.picks.size();
                break;
        }
        current["fields"][f.def->key] = cf;
    }

    nlohmann::json selected = nlohmann::json::array();
    for (const auto& item : st.selection.items()) {
        if (item.selector.empty()) continue;
        try {
            selected.push_back(nlohmann::json::parse(item.selector));
        } catch (...) {
            selected.push_back(item.selector);
        }
    }

    std::string intent = "Fill the active " + title_ +
                         " PropertyManager. Prefer safe visible field patches, and activate a "
                         "selector well when a real reference pick is needed.";
    return {
        {"doc_id", st.docMirror.docId()},
        {"intent", intent},
        {"form_schema", schema},
        {"current", current},
        {"selected_refs", selected},
    };
}

void PropertyManager::requestAiAssist(AppState& st) {
    if (!active_ || aiBusy_) return;
    if (!st.workerStarted) {
        st.toast(ui::ToastLevel::Warn, "Start the runtime before using Select with AI");
        return;
    }
    aiBusy_ = true;
    aiMessage_ = "AI is checking the visible options...";
    aiHighlights_.clear();
    const int seq = ++aiRequestSeq_;
    nlohmann::json params = buildAiAssistPayload(st);
    // Multi-document: this PropertyManager is now owned by its document's DocSession, but its result
    // patches fields via st (the ACTIVE doc). Bind the request to its doc so a tab switch before the
    // result drains can't apply this (backgrounded) form's AI suggestions against the foreground doc.
    const std::string assistDocId = st.docMirror.docId();
    st.rpc.call("agent_pm_assist_v1", params, [this, &st, seq, assistDocId](const core::RpcResult& r) {
        if (!active_ || seq != aiRequestSeq_) return;
        if (st.docMirror.docId() != assistDocId) { aiBusy_ = false; return; }  // active doc changed
        aiBusy_ = false;
        if (!r.ok) {
            aiMessage_ = "AI assist failed: " + r.err;
            if (st.isActiveMirrorDoc()) st.toast(ui::ToastLevel::Error, aiMessage_);
            return;
        }
        if (r.result.is_object() && r.result.contains("code")) {
            aiMessage_ = "AI assist failed: " + r.result.value("message", std::string("worker error"));
            if (st.isActiveMirrorDoc()) st.toast(ui::ToastLevel::Error, aiMessage_);
            return;
        }
        applyAiAssistResult(st, r.result, seq);
    }, 120000);
}

void PropertyManager::applyAiAssistResult(AppState& st, const nlohmann::json& result, int requestSeq) {
    if (!active_ || requestSeq != aiRequestSeq_ || !result.is_object()) return;
    int patched = 0;
    auto findVisible = [&](const std::string& key) -> PMField* {
        for (auto& f : fields_)
            if (key == f.def->key && fieldVisible(*f.def, fields_) &&
                fieldEnabled(*f.def, fields_)) return &f;
        return nullptr;
    };

    auto patches = result.find("patches");
    if (patches != result.end() && patches->is_array()) {
        for (const auto& p : *patches) {
            if (!p.is_object() || !p.contains("value")) continue;
            std::string key = p.value("key", std::string());
            PMField* f = findVisible(key);
            if (!f) continue;
            const auto& val = p["value"];
            bool changed = false;
            switch (f->def->kind) {
                case PMFieldKind::Dim:
                    changed = applyAiDimPatch(*f, val, ips_);
                    break;
                case PMFieldKind::Combo:
                case PMFieldKind::DatumRef: {
                    if (!val.is_string()) break;
                    std::string want = val.get<std::string>();
                    auto opts = comboOptionsList(*f->def);
                    for (int i = 0; i < (int)opts.size(); ++i) {
                        if (opts[i] == want) {
                            if (f->comboIndex != i) {
                                f->comboIndex = i;
                                f->comboUnsupported.clear();
                                changed = true;
                            }
                            if (legacyFillet_ && f->def->kind == PMFieldKind::Combo &&
                                std::strcmp(f->def->key, "type") == 0) {
                                legacyFillet_ = legacyFilletModeKnown_ = false;
                                changed = true;
                            }
                            if (f->def->kind == PMFieldKind::DatumRef) {
                                for (auto& well : fields_)
                                    if (well.def->kind == PMFieldKind::SelectorWell &&
                                        std::strcmp(well.def->key, f->def->key) == 0 &&
                                        !well.picks.empty()) {
                                        well.picks.clear();
                                        changed = true;
                                    }
                            }
                            break;
                        }
                    }
                    break;
                }
                case PMFieldKind::Check:
                    if (val.is_boolean() && f->checkValue != val.get<bool>()) {
                        f->checkValue = val.get<bool>();
                        changed = true;
                    }
                    break;
                case PMFieldKind::Int:
                    if (val.is_number_integer() && f->intValue != val.get<int>()) {
                        f->intValue = std::min(std::max(val.get<int>(), f->def->intMin),
                                               f->def->intMax);
                        changed = true;
                    }
                    break;
                case PMFieldKind::SelectorWell:
                case PMFieldKind::FeatureWell:
                    break;  // AI never patches refs; it can only activate a well.
            }
            if (changed) ++patched;
        }
    }

    aiHighlights_.clear();
    auto hi = result.find("highlights");
    if (hi != result.end() && hi->is_array()) {
        for (const auto& h : *hi)
            if (h.is_string()) aiHighlights_.push_back(h.get<std::string>());
    }

    bool activated = false;
    std::string activate = result.value("activate_well", std::string());
    if (!activate.empty()) {
        PMField* w = findVisible(activate);
        if (w && (w->def->kind == PMFieldKind::SelectorWell ||
                  w->def->kind == PMFieldKind::FeatureWell)) {
            for (auto& of : fields_)
                if (of.def->kind == PMFieldKind::SelectorWell ||
                    of.def->kind == PMFieldKind::FeatureWell)
                    of.wellActive = false;
            w->wellActive = true;
            activated = true;
            if (!hasString(aiHighlights_, w->def->key)) aiHighlights_.push_back(w->def->key);
        }
    }
    if (!activated && patched > 0) {
        for (auto& w : fields_) {
            if (w.def->kind != PMFieldKind::SelectorWell) continue;
            if (!fieldVisible(*w.def, fields_) || !w.picks.empty()) continue;
            for (auto& of : fields_)
                if (of.def->kind == PMFieldKind::SelectorWell ||
                    of.def->kind == PMFieldKind::FeatureWell)
                    of.wellActive = false;
            w.wellActive = true;
            activated = true;
            if (!hasString(aiHighlights_, w.def->key)) aiHighlights_.push_back(w.def->key);
            break;
        }
    }

    aiMessage_ = result.value("answer", std::string());
    auto qs = result.find("questions");
    if (qs != result.end() && qs->is_array() && !qs->empty()) {
        aiMessage_ += "\n";
        int n = 1;
        for (const auto& q : *qs) {
            if (!q.is_object()) continue;
            aiMessage_ += "\n" + std::to_string(n++) + ". " +
                          q.value("prompt", std::string());
        }
    }
    if (aiMessage_.empty()) {
        if (patched || activated) aiMessage_ = "AI applied safe form guidance.";
        else aiMessage_ = "AI did not find a safe automatic change.";
    }
    if (patched > 0) {
        dirty_ = true;
        lastEditTime_ = ImGui::GetTime();
    }
    if (st.isActiveMirrorDoc())
        st.toast(ui::ToastLevel::Info,
                 activated ? "AI selected the next reference well" :
                 patched > 0 ? "AI filled safe visible fields" : "AI guidance ready");
}

// ---- preview / commit / cancel --------------------------------------------

bool PropertyManager::formSourceCurrent(AppState& st) {
    if (st.docMirror.hasDoc() && st.docMirror.docId() == sessionDocId_ &&
        st.docMirror.revision() == formSourceRevision_)
        return true;

    cancelSafe_ = false;
    // A new feature has no model state to reload; reopen the command to select fresh references.
    if (!st.docMirror.hasDoc() || st.docMirror.docId() != sessionDocId_ || targetId_.empty()) {
        if (st.isActiveMirrorDoc())
            st.toast(ui::ToastLevel::Warn,
                     "The part changed while editing. Reopen the feature command and make a fresh edit.");
        close();
        return false;
    }

    const auto& tree = st.docMirror.tree();
    const nlohmann::json* target = nullptr;
    if (tree.is_object()) {
        auto features = tree.find("features");
        if (features != tree.end() && features->is_array()) {
            for (const auto& feature : *features) {
                if (feature.is_object() && feature.value("id", std::string()) == targetId_) {
                    target = &feature;
                    break;
                }
            }
        }
    }
    if (!target || target->value("type", std::string()) != form_->type) {
        if (st.isActiveMirrorDoc())
            st.toast(ui::ToastLevel::Warn,
                     "The feature changed or no longer exists; reopen it to make a fresh edit.");
        close();
        return false;
    }

    // Drop an active PM widget's edit buffer as well as our cached fields.
    if (st.isActiveMirrorDoc() && pmWidgetActivePrevFrame_) ImGui::ClearActiveID();
    fields_.clear();
    fields_.resize(form_->fields.size());
    for (size_t i = 0; i < form_->fields.size(); ++i)
        initField(fields_[i], form_->fields[i], ips_);
    loadFromTree(*target);
    hydrateFilletFromMirror(st);
    formSourceRevision_ = st.docMirror.revision();
    dirty_ = false;
    freshEditRequired_ = true;
    ++aiRequestSeq_;
    aiBusy_ = false;
    aiMessage_.clear();
    aiHighlights_.clear();
    previewWarning_.clear();
    previewError_ = "The part changed while editing. Fields were reloaded; make a fresh edit.";
    if (st.isActiveMirrorDoc()) st.toast(ui::ToastLevel::Warn, previewError_);
    st.addLog("[pm] " + previewError_);
    return false;
}

bool PropertyManager::applyPreview(AppState& st) {
    if (checkpointInFlight_ || cancelPending_) return false;
    if (!st.docMirror.hasDoc() || !st.workerStarted) {
        previewError_ = "Document runtime is not ready.";
        return false;
    }
    // Do not mistake our own in-flight preview for a foreign revision.
    if (previewInFlight_) return false;
    if (!formSourceCurrent(st)) return false;
    if (freshEditRequired_ && !dirty_) return false;
    if (!readyForPreview(form_, fields_)) {
        previewError_ = "Select the required references and enter valid dimensions before accepting.";
        return false;
    }

    // C37-F: Defensive check — if the target feature has vanished from the mirror tree (agent
    // run / external undo replaced the doc), close the PM rather than firing an edit_feature
    // that will be rejected. Only relevant for the edit-path where targetId_ is known.
    if (firstPreviewDone_ && !targetId_.empty()) {
        const nlohmann::json& tree = st.docMirror.tree();
        bool found = false;
        if (tree.is_object()) {
            auto feat = tree.find("features");
            if (feat != tree.end() && feat->is_array()) {
                for (const auto& f : *feat)
                    if (f.is_object() && f.value("id", std::string()) == targetId_) { found = true; break; }
            }
        }
        if (!found) {
            st.toast(ui::ToastLevel::Warn, "Feature " + targetId_ + " no longer exists — closing PM");
            st.addLog("[pm] tick: feature " + targetId_ + " vanished; closing PM");
            close();
            return false;
        }
    }

    nlohmann::json feature;
    buildOpFeature(feature);

    // Fillet/chamfer preview is LIVE and APPLIED (SolidWorks style): the feature stays un-suppressed
    // so the viewport shows the actual rounded result as the user edits. The original (pre-round)
    // edges are drawn as dashed 3D lines (see viewport_panel + collectActiveWellEdgeSegments) so the
    // user can still see and deselect them even after they're rounded away. Picking a NEW (un-rounded)
    // edge on the live body resolves correctly now that make_pick_selector enumerates edges in the
    // tessellator's unique order (the earlier "couldn't be located" / 3-way-corner-drop bug).
    const std::string ftype = feature.value("type", std::string());
    const bool blend = (ftype == "fillet" || ftype == "chamfer");

    nlohmann::json ops = nlohmann::json::array();
    if (!firstPreviewDone_ && targetId_.empty()) {
        ops.push_back({{"op", "add_feature"}, {"feature", feature}, {"before", nullptr}});
    } else {
        // Subsequent previews / edit-path: edit the existing feature in place.
        if (targetId_.empty()) {
            previewError_ = "Preview target was not created.";
            dirty_ = false;
            return false;
        }
        ops.push_back({{"op", "edit_feature"}, {"id", targetId_},
                       {"params", feature.value("params", nlohmann::json::object())},
                       {"refs",   feature.value("refs",   nlohmann::json::object())}});
    }
    if (blend)
        st.addLog("[pm] blend preview: edges=" + std::to_string(
                      feature.value("refs", nlohmann::json::object())
                          .value("edges", nlohmann::json::array()).size()));

    // Async applyOps so typing stays live during a multi-second rebuild. Serialize
    // in-flight previews so the first add_feature can learn targetId_ before a second batch
    // races a second add. Latest-wins on the mirror is handled by mutationGen inside
    // applyOpsAsync; we just refuse to queue a second while one is open.
    if (previewInFlight_) {
        // Keep dirty_ true so tick re-fires after the in-flight result drains.
        return false;
    }
    freshEditRequired_ = false;
    dirty_ = false;
    previewInFlight_ = true;
    ++st.rebuildsInFlight;
    const int baseRevision = st.docMirror.revision();
    if (baseRevision != cancelRevision_) cancelSafe_ = false;
    const bool coalesce = previewUndoGroupStarted_ && cancelSafe_;
    const uint64_t generation = editSessionGeneration_;
    const std::weak_ptr<int> lifetime = rpcLifetime_;
    const std::string docId = sessionDocId_;
    const bool finalQuality = committing_ || ftype != "thread";
    const bool blendCap = blend;
    // Disposable live preview -> short timeout; a genuine commit -> patient default (never abort).
    const int timeoutMs = committing_ ? 120000 : kPreviewTimeoutMs;
    st.docMirror.applyOpsAsync(st.rpc, st.worker, ops,
        [this, &st, blendCap, baseRevision, coalesce, generation, lifetime, docId, finalQuality]
        (const core::OpsResponse& resp, bool current) {
            --st.rebuildsInFlight;
            if (lifetime.expired() || generation != editSessionGeneration_) return;
            previewInFlight_ = false;
            if (!active_ || sessionDocId_ != docId) return;
            if (!current || st.docMirror.docId() != docId) {
                // Obsolete responses carry no presentable diagnostics or accepted payload.
                cancelSafe_ = false;
                acceptPending_ = false;
                freshEditRequired_ = true;
                return;
            }
            if (!resp.ok) {
                const bool unchanged = resp.rejected && resp.unchanged &&
                    resp.revision == baseRevision && st.docMirror.revision() == baseRevision &&
                    cancelRevision_ == baseRevision;
                if (!unchanged) cancelSafe_ = false;
                if (resp.error_code == "stale_revision") {
                    acceptPending_ = false;
                    if (st.docMirror.revision() != formSourceRevision_)
                        formSourceCurrent(st);
                    else {
                        if (st.isActiveMirrorDoc())
                            st.toast(ui::ToastLevel::Warn,
                                     "The part changed and could not be reloaded. Reopen the feature to edit it.");
                        close();
                    }
                    return;
                }
                previewError_ = resp.error_message.empty() ? resp.error_code : resp.error_message;
                if (previewError_.empty())
                    previewError_ = "Preview was superseded or could not be applied.";
                previewWarning_.clear();
                if (!dirty_ && readyForPreview(form_, fields_)) acceptPending_ = false;
                st.addLog("[pm] preview failed: " + previewError_);
                return;
            }
            if (resp.revision != baseRevision + 1) cancelSafe_ = false;
            if (resp.revision == baseRevision + 1)
                formSourceRevision_ = resp.revision;
            cancelRevision_ = resp.revision;
            previewError_.clear();
            if (!coalesce) ++previewBatches_;
            previewUndoGroupStarted_ = true;
            if (targetId_.empty() && resp.id_map.is_object()) {
                auto it = resp.id_map.find("$0");
                if (it != resp.id_map.end() && it->is_string())
                    targetId_ = it->get<std::string>();
            }
            firstPreviewDone_ = true;
            if (targetId_.empty()) {
                firstPreviewDone_ = false;
                acceptPending_ = false;
                previewError_ = "The preview did not return a feature ID.";
                return;
            }

            if (st.isActiveMirrorDoc() && !resp.aicm_path.empty())
                st.loadDocMeshAsync(resp.aicm_path);  // parked tabs are handled by the observer

            const bool currentFields = !dirty_ && readyForPreview(form_, fields_);
            if (currentFields) previewWarning_.clear();
            if (resp.auto_rollback.is_object()) {
                const std::string msg = resp.warning.empty()
                    ? "Downstream features failed; history was rolled back for repair."
                    : resp.warning;
                if (currentFields) previewWarning_ = msg;
                st.addLog("[pm] " + msg);
                if (st.isActiveMirrorDoc()) st.toast(ui::ToastLevel::Warn, msg);
            }
            if (currentFields && !resp.auto_rollback.is_object() && resp.feature_status.is_array()) {
                std::string hidden;
                for (const auto& status : resp.feature_status) {
                    if (!status.is_object() || !status.value("rolled_back", false)) continue;
                    if (!hidden.empty()) hidden += ", ";
                    hidden += status.value("id", std::string("?"));
                }
                if (!hidden.empty())
                    previewWarning_ = "History is rolled back; unbuilt features: " + hidden;
            }
            if (!targetId_.empty() && resp.feature_status.is_array()) {
                for (const auto& s : resp.feature_status) {
                    if (!s.is_object() || s.value("id", std::string()) != targetId_) continue;
                    auto wt = s.find("warning");
                    if (currentFields && wt != s.end() && wt->is_string()) {
                        if (!previewWarning_.empty()) previewWarning_ += " ";
                        previewWarning_ += wt->get<std::string>();
                    }
                    const bool sok = s.value("ok", true);
                    if (!sok) previewError_ = s.value("error", std::string());
                    if (blendCap)
                        st.addLog("[pm] feature status " + targetId_ +
                                  ": ok=" + std::string(sok ? "1" : "0") +
                                  " warn=\"" + (wt != s.end() && wt->is_string()
                                                    ? wt->get<std::string>() : "") + "\"" +
                                  (sok ? "" : " err=\"" + s.value("error", std::string()) + "\""));
                    auto dr = s.find("dangling_refs");
                    std::vector<std::string> dangling;
                    if (dr != s.end() && dr->is_array())
                        for (const auto& d : *dr)
                            if (d.is_string()) dangling.push_back(d.get<std::string>());
                    for (auto& f : fields_) {
                        if (f.def->kind != PMFieldKind::SelectorWell) continue;
                        f.wellDangling = false;
                        for (const auto& dref : dangling)
                            if (dref.rfind(f.def->key, 0) == 0) f.wellDangling = true;
                    }
                    // The compiler reports actual radii and full edge coverage. Synchronize only
                    // the generation that produced this result; never overwrite newer edits or an
                    // authored equation. Literal corrections already landed in the same tree.
                    if (currentFields && form_ && std::string(form_->type) == "fillet" &&
                        s.contains("data") && s["data"].is_object() &&
                        s["data"].contains("fillet") && s["data"]["fillet"].is_object()) {
                        const auto& data = s["data"]["fillet"];
                        hydrateFilletEvidence(data);
                        if (data.contains("requested") && data["requested"].is_object() &&
                            data.contains("effective") && data["effective"].is_object()) {
                            const auto& requested = data["requested"];
                            const auto& effective = data["effective"];
                            const std::string adjustment = data.value("adjustment", std::string("none"));
                            for (auto& f : fields_) {
                                if (f.def->kind != PMFieldKind::Dim ||
                                    (std::strcmp(f.def->key, "radius") != 0 &&
                                     std::strcmp(f.def->key, "radius2") != 0)) continue;
                                const char* key = f.def->key;
                                if (!effective.contains(key) || !effective[key].is_number()) continue;
                                const double value = effective[key].get<double>();
                                if (!std::isfinite(value)) continue;
                                f.dimValue = value; // resolved geometry, also shown beside equations
                                f.dimValueKnown = true;
                                if (f.dimExpr.empty()) {
                                    if (f.dimTextEditing) f.dimSyncPending = true;
                                    else {
                                        const std::string text = pmFieldValueText(f.def, value, ips_);
                                        std::snprintf(f.dimBuf.data(), f.dimBuf.size(), "%s", text.c_str());
                                        f.dimSyncPending = false;
                                    }
                                }
                                if (adjustment != "none" && requested.contains(key) &&
                                    requested[key].is_number()) {
                                    const double from = requested[key].get<double>();
                                    if (std::isfinite(from) && from != value) {
                                        if (!previewWarning_.empty()) previewWarning_ += "\n";
                                        previewWarning_ += std::string(f.def->label) + " adjusted from " +
                                            pmFormatDim(from, ips_) + " to " + pmFormatDim(value, ips_);
                                    }
                                }
                            }
                        }
                    }
                }
            }
            if (!cancelPending_ && acceptPending_ && finalQuality && currentFields && previewError_.empty() &&
                resp.revision == baseRevision + 1) {
                st.addLog("[pm] OK -> committed " + targetId_);
                close();
            }
        }, coalesce, timeoutMs);
    return true;
}

void PropertyManager::tick(AppState& st) {
    if (cancelPending_) {
        cancelEditsAsync(st);
        return;
    }
    // Phase 4: keep the daemon edit-hold fresh while the form is open. The
    // daemon already refreshes it on every preview op; this covers an open-but-
    // idle form, rate-limited to once/second.
    if (active_ && !previewInFlight_ && !holdInFlight_ && !holdDocId_.empty() &&
        ImGui::GetTime() - lastHoldTime_ > 1.0) {
        holdInFlight_ = true;
        const uint64_t generation = editSessionGeneration_;
        const std::weak_ptr<int> lifetime = rpcLifetime_;
        st.rpc.callPrimary("edit_hold", {{"doc_id", holdDocId_}},
                    [this, generation, lifetime](const core::RpcResult&) {
            if (!lifetime.expired() && generation == editSessionGeneration_) holdInFlight_ = false;
        });
        lastHoldTime_ = ImGui::GetTime();
    }
    if (!active_ || previewInFlight_ || checkpointInFlight_) return;
    if (acceptPending_) {
        if (!readyForPreview(form_, fields_)) return; // wait for any newer edit to be applied
        if (!dirty_ && firstPreviewDone_ && previewError_.empty() &&
            std::string(form_->type) != "thread") {
            if (formSourceCurrent(st)) {
                st.addLog("[pm] OK -> committed " + targetId_);
                close();
            }
            return;
        }
        committing_ = true;
        if (!applyPreview(st) && active_) acceptPending_ = false;
        committing_ = false;
        return;
    }
    if (!dirty_ || !readyForPreview(form_, fields_)) return;
    if (ImGui::GetTime() - lastEditTime_ < kDebounceSec) return;
    applyPreview(st);
}

void PropertyManager::commit(AppState& st) {
    if (cancelPending_ || !active_) return;
    if (!flushDimText() || !readyForPreview(form_, fields_)) {
        previewError_ = "Select the required references and enter valid dimensions before accepting.";
        st.addLog("[pm] OK blocked: form is not ready");
        return;
    }
    if (!previewInFlight_ && !formSourceCurrent(st)) return;
    if (freshEditRequired_ && !dirty_) return;
    // An explicit retry after an error must submit again, never close on the old rejection.
    if (!previewError_.empty()) dirty_ = true;
    acceptPending_ = true;
}

bool PropertyManager::cancelEdits(AppState& st) {
    if (cancelPending_) return false;
    if (checkpointInFlight_ || previewInFlight_) {
        cancelEditsAsync(st);
        return false;
    }
    if (!st.docMirror.hasDoc() || st.docMirror.docId() != sessionDocId_) {
        close();
        return false;
    }
    if (!cancelSafe_ || st.docMirror.revision() != cancelRevision_) {
        if (st.isActiveMirrorDoc() && (previewBatches_ || !cancelSafe_))
            st.toast(ui::ToastLevel::Warn,
                     "The edit could not be safely rolled back; the current document was kept.");
        close();
        return false;
    }
    if (!previewBatches_) {
        close();
        return true;
    }
    nlohmann::json result;
    std::string error;
    if (!st.worker.call("doc_revert", {{"doc_id", sessionDocId_}, {"checkpoint", checkpointId_},
                                      {"expect_revision", cancelRevision_}}, result, error)) {
        if (st.isActiveMirrorDoc()) st.toast(ui::ToastLevel::Error, "Cancel revert failed: " + error);
        return false;
    }
    if (result.value("code", std::string()) == "stale_revision") {
        if (st.isActiveMirrorDoc())
            st.toast(ui::ToastLevel::Warn,
                     "The part changed while cancelling; the current document was kept.");
        close();
        return false;
    }
    const auto response = st.docMirror.ingestOpsResponse(result);
    if (!response.ok) {
        if (st.isActiveMirrorDoc()) st.toast(ui::ToastLevel::Error, "Cancel revert failed: " + response.error_message);
        st.addLog("[pm] Cancel revert failed: " + response.error_message);
        return false;
    }
    if (st.isActiveMirrorDoc() && !response.aicm_path.empty()) {
        std::string error;
        st.loadDocMesh(response.aicm_path, error);
    }
    st.addLog("[pm] Cancel -> reverted to checkpoint");
    close();
    return true;
}

void PropertyManager::cancelEditsAsync(AppState& st) {
    if (!active_ || cancelInFlight_) return;
    cancelPending_ = true;
    if (checkpointInFlight_ || previewInFlight_) return;
    if (!st.docMirror.hasDoc() || st.docMirror.docId() != sessionDocId_) {
        close();
        return;
    }
    if (!cancelSafe_ || st.docMirror.revision() != cancelRevision_) {
        if (st.isActiveMirrorDoc() && (previewBatches_ || !cancelSafe_))
            st.toast(ui::ToastLevel::Warn,
                     "The edit could not be safely rolled back; the current document was kept.");
        close();
        return;
    }
    if (!previewBatches_) {
        close();
        return;
    }
    cancelInFlight_ = true;
    const std::string docId = sessionDocId_;
    const uint64_t generation = editSessionGeneration_;
    const uint64_t mutation = st.docMirror.mutationGen();
    const std::weak_ptr<int> lifetime = rpcLifetime_;
    const long baseRevision = cancelRevision_;
    nlohmann::json params = {{"doc_id", docId}, {"checkpoint", checkpointId_},
                             {"expect_revision", cancelRevision_}};
    st.beginHumanMutation(docId);
    st.rpc.call("doc_revert", std::move(params),
                [this, &st, docId, generation, mutation, lifetime, baseRevision](const core::RpcResult& r) {
        st.endHumanMutation(docId);
        const bool accepted = r.ok && r.result.is_object() && !r.result.contains("code") &&
                              r.result.value("revision", -1L) > baseRevision;
        if (accepted) {
            st.markHumanEdit(docId, r.result.value("revision", -1));
            if (lifetime.expired() || st.docMirror.docId() != docId ||
                st.docMirror.mutationGen() != mutation)
                st.catchUpSupersededOps(docId, static_cast<int>(baseRevision));
        }
        if (lifetime.expired() || generation != editSessionGeneration_) return;
        if (!st.docMirror.hasDoc() || st.docMirror.docId() != docId ||
            st.docMirror.mutationGen() != mutation) {
            close();
            return;
        }
        if (r.result.is_object() &&
            r.result.value("code", std::string()) == "stale_revision") {
            if (st.isActiveMirrorDoc())
                st.toast(ui::ToastLevel::Warn,
                         "The part changed while cancelling; the current document was kept.");
            close();
            return;
        }
        core::OpsResponse response;
        if (r.ok && r.result.is_object())
            response = st.docMirror.ingestOpsResponse(r.result);
        if (!r.ok || !response.ok) {
            cancelPending_ = cancelInFlight_ = false;
            const std::string detail = response.error_message.empty() ? r.err
                                                                     : response.error_message;
            if (st.isActiveMirrorDoc()) st.toast(ui::ToastLevel::Error, "Cancel revert failed: " + detail);
            st.addLog("[pm] Cancel revert failed: " + detail);
            return;
        }
        if (st.isActiveMirrorDoc() && !response.aicm_path.empty()) st.loadDocMeshAsync(response.aicm_path);
        st.addLog("[pm] Cancel -> reverted to checkpoint");
        close();
    });
}

// ---- drawing --------------------------------------------------------------

// Liquid glass v2 (host contract §H4): the PropertyManager form renders EMBEDDED inside the pinned
// FeatureManager glass column (layout.cpp drawLeftPane -> drawBody), which already painted ONE glass
// surface for the WHOLE pane via ui::adaptivePanel(panelId("host_left_glass"), …). We must NOT paint
// our own glassPanel (that would double-paint / fight the column background). Instead we fetch that
// SAME surface (same id + group => same cached AdaptiveSurface, idempotent per frame — design doc
// A2) so labels/values read against the LIVE glass tint (surf.textPrimary/textSecondary track the
// backdrop luminance in both themes) instead of the theme-static pal.text/pal.textDim. Mirrors
// feature_tree.cpp's fmSurf(). Semantic roles (accent/status/selection) stay on palette.
static const ui::AdaptiveSurface& pmSurf() {
    ImVec2 wp = ImGui::GetWindowPos();
    ImVec2 ws = ImGui::GetWindowSize();
    ImRect r(wp, ImVec2(wp.x + ws.x, wp.y + ws.y));
    // Group tag MUST match layout.cpp's paint site (adaptivePanel contract: same group every frame
    // for a given id) so the FeatureManager per-group glass knobs ride through unchanged.
    return ui::adaptivePanel(ui::panelId("host_left_glass"), r, ui::GlassGroup::FeatureManager);
}

// Forward decls — used by the exported selection-well / form header below.
static void pushTextColorV(const ImVec4& c);
static void pushTextColor(const float c[4]);
static void pmTooltip(const char* fmt, ...);

// ---- exported selection-well + form header (asm tools + feature forms share these) ------------

PMSelectionWellResult pmDrawSelectionWell(const char* id, const char* label,
                                          const char* chipText, bool active,
                                          bool dangling) {
    PMSelectionWellResult r;
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& wsurf = pmSurf();
    const float S = ui::uiScale;

    if (active) pushTextColor(pal.accent);
    else        pushTextColorV(wsurf.textSecondary);
    ImGui::PushTextWrapPos(0.0f);
    ImGui::TextUnformatted(label ? label : "");
    ImGui::PopTextWrapPos();
    ImGui::PopStyleColor();
    if (active) {
        const char* pickHint = "Pick in viewport";
        const float hintW = ImGui::CalcTextSize(pickHint).x;
        const ImVec2 captionSize = ImGui::GetItemRectSize();
        const float rowWidth = ImGui::GetContentRegionAvail().x;
        const float gap = ImGui::GetStyle().ItemSpacing.x;
        if (captionSize.y <= ImGui::GetTextLineHeight() &&
            captionSize.x + gap + hintW <= rowWidth)
            ImGui::SameLine(0, gap);
        const float remain = ImGui::GetContentRegionAvail().x;
        if (remain > hintW)
            ImGui::SetCursorPosX(ImGui::GetCursorPosX() + remain - hintW);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 0.75f));
        ImGui::PushTextWrapPos(0.0f);
        ImGui::TextUnformatted(pickHint);
        ImGui::PopTextWrapPos();
        ImGui::PopStyleColor();
    }

    const char* chip = (chipText && chipText[0]) ? chipText : "(pick in viewport)";
    ImVec4 border = dangling
        ? ImVec4(pal.statusErr[0], pal.statusErr[1], pal.statusErr[2], 1)
        : (active
            ? ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1)
            : ImVec4(pal.border[0], pal.border[1], pal.border[2], pal.border[3]));
    const float availW = ImGui::GetContentRegionAvail().x;
    const float textPadX = 10.0f * S;
    const float textPadY = 7.0f * S;
    const float wrapW = std::max(40.0f * S, availW - textPadX * 2.0f);
    ImVec2 chipSz = ImGui::CalcTextSize(chip, nullptr, false, wrapW);
    float wellH = std::max(ImGui::GetFrameHeight(), chipSz.y + textPadY * 2.0f);

    ImGui::PushID(id ? id : "##well");
    ImVec2 wp0 = ImGui::GetCursorScreenPos();
    ImVec2 wp1(wp0.x + availW, wp0.y + wellH);
    if (ImGui::InvisibleButton("##well", ImVec2(availW, wellH)))
        r.clicked = true;
    r.hovered = ImGui::IsItemHovered();
    if (ImGui::IsItemClicked(ImGuiMouseButton_Right))
        r.rightClicked = true;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (active) {
        ui::glassRowBackground(dl, wp0, wp1, 6.0f * S, /*stripe=*/false,
                               /*selected=*/true, 0.42f);
    } else {
        ui::glassRowBackground(dl, wp0, wp1, 6.0f * S, /*stripe=*/false,
                               /*selected=*/false, r.hovered ? 0.42f : 0.28f);
    }
    dl->AddRect(wp0, wp1, ImGui::ColorConvertFloat4ToU32(border), 6.0f * S, 0,
                active ? std::max(1.0f, 2.0f * S) : std::max(1.0f, 1.0f * S));
    ImU32 chipCol = (!chipText || !chipText[0] || std::strcmp(chip, "(pick in viewport)") == 0)
        ? ImGui::ColorConvertFloat4ToU32(wsurf.textSecondary)
        : ImGui::ColorConvertFloat4ToU32(wsurf.textPrimary);
    // Treat empty-looking chips (placeholder) as secondary; callers pass a filled label otherwise.
    if (chipText && chipText[0] && std::strcmp(chipText, "(pick in viewport)") != 0)
        chipCol = ImGui::ColorConvertFloat4ToU32(wsurf.textPrimary);
    else
        chipCol = ImGui::ColorConvertFloat4ToU32(wsurf.textSecondary);
    dl->AddText(nullptr, 0.0f, ImVec2(wp0.x + textPadX, wp0.y + textPadY),
                chipCol, chip, nullptr, wrapW);
    if (r.hovered) {
        if (active) pmTooltip("Active — pick faces/edges in the viewport");
        else if (chipText && chipText[0] && std::strcmp(chipText, "(pick in viewport)") != 0)
            pmTooltip("Click to re-activate; right-click to clear");
        else
            pmTooltip("Click to activate pick target");
    }
    if (dangling) {
        pushTextColor(pal.statusErr);
        ImGui::TextUnformatted("! dangling reference");
        ImGui::PopStyleColor();
    }
    ImGui::PopID();
    return r;
}

void pmDrawFormHeader(const char* icon, const char* title, const char* description) {
    const auto& pal = ui::palette;
    const ui::AdaptiveSurface& surf = pmSurf();
    const float S = ui::uiScale;
    const float glyphSz = 26.0f * S;
    const float hdrPadV = 7.0f * S;
    const float hdrPadH = 10.0f * S;
    const float accentBarW = 4.0f * S;
    const float rowAvail = std::max(1.0f, ImGui::GetContentRegionAvail().x);
    const float hdrH = glyphSz + hdrPadV * 2.0f;

    ImVec2 hdrMin = ImGui::GetCursorScreenPos();
    ImVec2 hdrMax(hdrMin.x + rowAvail, hdrMin.y + hdrH);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ui::glassBackground(dl, hdrMin, hdrMax, 6.0f);
    {
        const float bi = 4.0f * S;
        dl->AddRectFilled(ImVec2(hdrMin.x,             hdrMin.y + bi),
                          ImVec2(hdrMin.x + accentBarW, hdrMax.y - bi),
                          Palette::toU32(pal.accent), 2.0f * S);
    }
    ImGui::SetCursorScreenPos(ImVec2(hdrMin.x + accentBarW + hdrPadH, hdrMin.y + hdrPadV));
    float afterGlyphX = hdrMin.x + accentBarW + hdrPadH;
    if (icon && icon[0]) {
        IconUV ic = getIcon(icon, 32);
        if (ic.valid) {
            ImGui::Image(ic.texId, ImVec2(glyphSz, glyphSz), ic.uv0, ic.uv1);
            afterGlyphX += glyphSz + 8.0f * S;
        }
    }
    float titleY = hdrMin.y + (hdrH - ImGui::GetTextLineHeight()) * 0.5f;
    ImGui::SetCursorScreenPos(ImVec2(afterGlyphX, titleY));
    pushTextColor(pal.accent);
    ImGui::TextUnformatted(title ? title : "");
    ImGui::PopStyleColor();
    ImGui::SetCursorScreenPos(ImVec2(hdrMin.x, hdrMax.y + 4.0f * S));
    if (description && description[0]) {
        pushTextColorV(surf.textSecondary);
        float wrapW = ImGui::GetContentRegionAvail().x;
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + wrapW);
        ImGui::TextUnformatted(description);
        ImGui::PopTextWrapPos();
        ImGui::PopStyleColor();
        ImGui::Dummy(ImVec2(0, 2.0f * S));
    }
}

void PropertyManager::openGuest(AppState& st, const char* title, const char* icon,
                                GuestFn body, GuestFn onOk, GuestFn onCancel, bool readOnly) {
    // Guest entry keeps any live feature preview, releasing its edit_hold without a revert.
    closeIfActive();
    sessionDocId_ = st.docMirror.docId();
    guestReadOnly_ = readOnly;
    guestActive_ = true;
    guestTitle_ = title ? title : "";
    guestIcon_ = icon ? icon : "";
    guestBody_ = std::move(body);
    guestOnOk_ = std::move(onOk);
    guestOnCancel_ = std::move(onCancel);
}

void PropertyManager::closeGuest() {
    guestActive_ = false;
    guestReadOnly_ = false;
    sessionDocId_.clear();
    guestTitle_.clear();
    guestIcon_.clear();
    guestBody_ = nullptr;
    guestOnOk_ = nullptr;
    guestOnCancel_ = nullptr;
}

// Push a text color from an adaptive ImVec4 (surf.textPrimary/textSecondary).
static void pushTextColorV(const ImVec4& c) { ImGui::PushStyleColor(ImGuiCol_Text, c); }

static void pushTextColor(const float c[4]) {
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(c[0], c[1], c[2], c[3]));
}

// drawBody() pushes ImGuiCol_Text = surf.textPrimary (adaptive, DARK over the light
// viewport) as the default so on-panel VALUE text tracks the frosted-glass tint. But a tooltip is
// a SEPARATE popup window whose PopupBg is the THEME-toned glassBg (dark in the dark theme), so it
// must use THEME-toned text (pal.text = light in the dark theme), not the adaptive backdrop tone —
// otherwise the inherited surf.textPrimary paints dark text on the dark popup (black-on-black).
// Mirror the open-combo pattern (which re-pushes pal.text). Use this for every tooltip inside the
// drawBody/label call-graph.
static void pmTooltip(const char* fmt, ...) {
    const auto& t = ui::palette.text;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(t[0], t[1], t[2], t[3]));
    va_list args;
    va_start(args, fmt);
    ImGui::SetTooltipV(fmt, args);
    va_end(args);
    ImGui::PopStyleColor();
}

// C37-F: descriptions for each form type shown under the title.
static const char* formDescription(const char* type) {
    if (!type) return "";
    if (std::strcmp(type, "box")      == 0) return "Create a rectangular solid at the origin.";
    if (std::strcmp(type, "cylinder") == 0) return "Create a cylinder at the origin.";
    if (std::strcmp(type, "extrude")  == 0) return "Extrude a sketch profile along its normal.";
    if (std::strcmp(type, "revolve")  == 0) return "Revolve a profile around an axis.";
    if (std::strcmp(type, "fillet")   == 0) return "Round selected edges with a constant or variable radius.";
    if (std::strcmp(type, "chamfer")  == 0) return "Bevel selected edges by distance.";
    if (std::strcmp(type, "shell")    == 0) return "Hollow the solid to a uniform wall thickness.";
    return "";
}

static bool pmCompactLabel(const char* label, const char* suffix,
                           const Palette& pal, bool enabled) {
    (void)pal;
    const float S = ui::uiScale;
    const float avail = ImGui::GetContentRegionAvail().x;
    const bool inlineRow = avail >= 220.0f * S;
    float labelW = avail * 0.40f;
    labelW = std::max(112.0f * S, std::min(labelW, 138.0f * S));

    // Liquid glass v2: labels read against the LIVE column glass tint (adaptive max-contrast text)
    // instead of theme-static pal.text/pal.textDim. Enabled -> primary, disabled -> dimmer secondary.
    const ui::AdaptiveSurface& surf = pmSurf();
    pushTextColorV(enabled ? surf.textPrimary : surf.textSecondary);
    std::string full = std::string(label) + (suffix ? suffix : "");
    if (inlineRow) {
        ImGui::AlignTextToFramePadding();
        // The field column starts at SameLine(labelW). If the label text is WIDER than the
        // column (e.g. "Tangent propagation"), the next widget lands ON TOP of the tail of the text
        // (the PM-tool-menu overlap). Truncate the drawn label to the column budget with an
        // ellipsis (full text stays in the hover tooltip, per this row's stated two-column intent)
        // so the widget at labelW never overlaps.
        const float gap    = ImGui::GetStyle().ItemInnerSpacing.x;
        const float budget = labelW - gap;
        std::string shown = full;
        bool clipped = false;
        if (ImGui::CalcTextSize(shown.c_str()).x > budget && budget > 0.0f) {
            clipped = true;
            const char* ell = "\xE2\x80\xA6";  // U+2026 ellipsis
            const float ellW = ImGui::CalcTextSize(ell).x;
            while (!shown.empty() &&
                   ImGui::CalcTextSize(shown.c_str()).x + ellW > budget)
                shown.pop_back();
            shown += ell;
        }
        ImGui::TextUnformatted(shown.c_str());
        ImGui::PopStyleColor();
        (void)clipped;
        if (ImGui::IsItemHovered()) pmTooltip("%s", full.c_str());
        ImGui::SameLine(labelW);
        return true;
    }

    ImGui::TextUnformatted(full.c_str());
    ImGui::PopStyleColor();
    return false;
}

// Optional / secondary PM groups start collapsed; primary required groups stay open.
// Matched by group title string from pm_forms.cpp (e.g. "Direction 2", "Options").
static bool pmGroupDefaultOpen(const char* label) {
    if (!label || !label[0]) return true;
    // Secondary / optional groups — keep this list in sync with optional sections in pm_forms.
    static const char* kCollapsedDefaults[] = {
        "Direction 2",
        "Thin Feature",
        "Options",
        "Selected Contours",
        "Guide Curves",
        "Advanced",
        "Instances to Skip",
        "Centerline",
        "Tangency",
    };
    for (const char* g : kCollapsedDefaults)
        if (std::strcmp(label, g) == 0) return false;
    return true;
}

// NOT a ui::kit::sectionHeader in disguise: this is the PM group's DISCLOSURE control — an
// ImGui::CollapsingHeader whose open state it returns, with the app's shared glass row pill behind
// it and the adaptive-surface ink required over the live viewport. The kit's sectionHeader is
// a static caption and its collapsible primitive (cardBegin/cardEnd) is a paired container, which
// this call site cannot be: the group card that follows is opened here and closed by the NEXT
// group's header. Nothing below duplicates a kit painter.
static bool pmSectionHeader(const char* label, const Palette& pal) {  // gui-lint: allow (disclosure control, not a caption painter)
    if (!label || !label[0]) return true;
    const float S = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(9.0f * S, 4.0f * S));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 6.0f * S);

    // Liquid glass v2: the section header is a hoverable row, so give it a translucent glass PILL
    // (glassRowBackground) consistent with the rest of the app (FeatureManager rows / the collapse
    // chevron in layout.cpp) instead of the theme-static glassBg Header fill. ImGui paints the
    // Header* fills UNDER our pill draw, so make them transparent and let the pill be the surface.
    // Section captions read against the live glass tint (adaptive secondary text). Active state keeps
    // the semantic accent wash so an open/pressed header still flashes accent (unchanged intent).
    const ui::AdaptiveSurface& surf = pmSurf();
    ImGui::PushStyleColor(ImGuiCol_Header,        ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_HeaderActive,
        ImVec4(pal.accentSoft[0], pal.accentSoft[1], pal.accentSoft[2], pal.accentSoft[3]));
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);

    // Draw the frosted pill FIRST (behind the header text/arrow) at the row rect. Hover reads last
    // frame's state via IsItemHovered() after submit would paint over the label, so use the mouse
    // rect test up-front for the brighter hover wash. The pill sits on the window draw list before
    // CollapsingHeader submits its glyphs, so text/arrow render on top.
    const ImVec2 rowMin = ImGui::GetCursorScreenPos();
    const float  rowW   = ImGui::GetContentRegionAvail().x;
    const float  rowH   = ImGui::GetFrameHeight();
    const ImVec2 rowMax(rowMin.x + rowW, rowMin.y + rowH);
    const bool hov = ImGui::IsMouseHoveringRect(rowMin, rowMax);
    ui::glassRowBackground(ImGui::GetWindowDrawList(), rowMin, rowMax, 6.0f * S,
                           /*stripe=*/false, /*selected=*/false, hov ? 0.42f : 0.30f);
    // Force default open/closed when the header appears (form open / first show). User toggles
    // stick for the rest of the session while the form stays open (ImGuiCond_Appearing).
    ImGui::SetNextItemOpen(pmGroupDefaultOpen(label), ImGuiCond_Appearing);
    const bool open = ImGui::CollapsingHeader(label);

    ImGui::PopStyleColor(4);
    ImGui::PopStyleVar(2);
    return open;
}

// OK / Cancel command buttons, relocated ABOVE the PM into the left-pane top row (between
// the Configurations tab and the collapse chevron). Rendered by layout.cpp from that
// row while a form is active. Wider than the old 26dp squares, with regenerated ok_check / cancel_x
// glyphs. The cluster is right-aligned to END at `rightX`, vertically centered on `centerY`. Clicking
// OK commits (commit() still guards form readiness + toasts on failure); Cancel reverts every preview
// batch. Returns the cluster's pixel width (0 when inactive) so the caller can reserve space.
float PropertyManager::drawChromeButtons(AppState& st, float rightX, float centerY) {
    if (!active()) return 0.0f;
    if (active_ && !guestActive_ && !form_) return 0.0f;
    const float S   = ui::uiScale;
    const auto& pal = ui::palette;
    // TASK C: larger, SQUARE, easy-to-hit targets. The old cluster was a 40x26 non-square pair using
    // atlas sprites; these are generous 34dp squares — the biggest square that still fits inside the
    // ~42dp left-pane header band (rowPad 6 + btn 30 + rowPad 6) without spilling into the tabs or the
    // separator below. Square + bigger = an obvious click target, per the brief.
    const float btn      = 34.0f * S;    // square edge
    const float gap      = 8.0f * S;
    const float clusterW = btn * 2.0f + gap;
    const float x0       = rightX - clusterW;
    const float y0       = centerY - btn * 0.5f;

    // Draw at absolute screen positions but leave the caller's layout cursor untouched.
    const ImVec2 savedCur = ImGui::GetCursorScreenPos();
    ImGui::PushID("pm_chrome_okcancel");
    bool ok = false, cancel = false;

    // TASK B: crisp, programmatic glyphs drawn straight onto the button rect via ImDrawList — no more
    // muddy atlas sprite, and no atlas/POST_BUILD staging hazard. A clean two-segment check and a
    // two-stroke X, both centred, thickness scaled with the button so they read at any DPI.
    auto drawCheck = [&](ImDrawList* dl, const ImVec2& c, float half, ImU32 col, float th) {
        // Classic checkmark: short down-stroke from upper-left to a low pivot, long up-stroke to the
        // upper-right. Proportions tuned to look balanced inside a square.
        ImVec2 p0(c.x - half * 0.72f, c.y + half * 0.05f);
        ImVec2 p1(c.x - half * 0.18f, c.y + half * 0.58f);
        ImVec2 p2(c.x + half * 0.74f, c.y - half * 0.55f);
        dl->PathLineTo(p0); dl->PathLineTo(p1); dl->PathLineTo(p2);
        dl->PathStroke(col, ImDrawFlags_None, th);
        // Round the corners/ends so the crook reads clean, not chiselled.
        dl->AddCircleFilled(p0, th * 0.5f, col);
        dl->AddCircleFilled(p1, th * 0.5f, col);
        dl->AddCircleFilled(p2, th * 0.5f, col);
    };
    auto drawX = [&](ImDrawList* dl, const ImVec2& c, float half, ImU32 col, float th) {
        float r = half * 0.62f;
        dl->AddLine(ImVec2(c.x - r, c.y - r), ImVec2(c.x + r, c.y + r), col, th);
        dl->AddLine(ImVec2(c.x + r, c.y - r), ImVec2(c.x - r, c.y + r), col, th);
    };

    ImDrawList* dl = ImGui::GetWindowDrawList();

    // Confirm (OK) + Cancel: neutral rest fill always. Hover/active is a status-tinted glowing
    // edge (layered outer rings + crisp border + faint inner wash) — never a solid status block.
    // Matches the soft-halo language of drawManipArrow / doc_tabs AI pulse, statusOk for check
    // and statusErr for X so the pair reads as a matched set.
    auto drawChromeHoverGlow = [&](const ImVec2& mn, const ImVec2& mx, const float status[4],
                                   bool hovered, bool active) {
        if (!hovered && !active) return;
        const float rnd = ImGui::GetStyle().FrameRounding;
        // Faint status wash inside the face (low alpha so the neutral fill still dominates).
        {
            float wash[4];
            Palette::withAlpha(status, wash, active ? 0.16f : 0.09f);
            dl->AddRectFilled(mn, mx, Palette::toU32(wash), rnd);
        }
        // Soft outer glow: layered AddRect rings stepping outward with decreasing alpha
        // (same layered-halo idea as doc_tabs AI pulse / drawManipArrow under-stroke).
        const float baseA = active ? 0.30f : 0.20f;
        for (int k = 3; k >= 1; --k) {
            const float grow = (float)k * 2.0f * S;
            const float a = baseA * (float)k / 3.0f;
            float g[4]; Palette::withAlpha(status, g, a);
            dl->AddRect(ImVec2(mn.x - grow, mn.y - grow),
                        ImVec2(mx.x + grow, mx.y + grow),
                        Palette::toU32(g), rnd + grow, 0, 2.0f * S);
        }
        // Crisp status-tinted border ring; pressed deepens it.
        {
            float edge[4];
            Palette::withAlpha(status, edge, active ? 1.0f : 0.90f);
            dl->AddRect(mn, mx, Palette::toU32(edge), rnd, 0,
                        (active ? 2.2f : 1.6f) * S);
        }
    };

    // Neutral face for both buttons (rest fill stays put on hover — glow carries the state).
    const ImVec4 faceRest(
        pal.button[0], pal.button[1], pal.button[2], 0.92f);
    const ImVec4 faceHover(
        pal.buttonHovered[0], pal.buttonHovered[1], pal.buttonHovered[2], 0.92f);
    const ImVec4 faceActive(
        pal.buttonActive[0], pal.buttonActive[1], pal.buttonActive[2], 0.95f);
    // Glyph on the neutral face: palette text role is light-on-dark / dark-on-light.
    const ImU32 glyphCol = Palette::toU32(pal.text);

    // Confirm (OK) — statusOk glow on hover/active.
    ImGui::SetCursorScreenPos(ImVec2(x0, y0));
    ImGui::PushStyleColor(ImGuiCol_Button,        faceRest);
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, faceHover);
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,  faceActive);
    ok = ImGui::Button("##pm_ok", ImVec2(btn, btn));
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("OK (Enter)");
    {
        ImVec2 mn = ImGui::GetItemRectMin(), mx = ImGui::GetItemRectMax();
        ImVec2 c((mn.x + mx.x) * 0.5f, (mn.y + mx.y) * 0.5f);
        drawChromeHoverGlow(mn, mx, pal.statusOk,
                            ImGui::IsItemHovered(), ImGui::IsItemActive());
        drawCheck(dl, c, btn * 0.5f, glyphCol, 3.0f * S);
    }
    ImGui::PopStyleColor(3);

    // Cancel — statusErr glow on hover/active (no solid red face).
    ImGui::SetCursorScreenPos(ImVec2(x0 + btn + gap, y0));
    ImGui::PushStyleColor(ImGuiCol_Button,        faceRest);
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, faceHover);
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,  faceActive);
    cancel = ImGui::Button("##pm_cancel", ImVec2(btn, btn));
    if (ImGui::IsItemHovered()) ImGui::SetTooltip("Cancel (Esc)");
    {
        ImVec2 mn = ImGui::GetItemRectMin(), mx = ImGui::GetItemRectMax();
        ImVec2 c((mn.x + mx.x) * 0.5f, (mn.y + mx.y) * 0.5f);
        drawChromeHoverGlow(mn, mx, pal.statusErr,
                            ImGui::IsItemHovered(), ImGui::IsItemActive());
        drawX(dl, c, btn * 0.5f, glyphCol, 3.0f * S);
    }
    ImGui::PopStyleColor(3);

    ImGui::PopID();
    ImGui::SetCursorScreenPos(savedCur);   // restore the caller's layout flow

    if (guestActive_) {
        if (ok && guestOnOk_)          guestOnOk_(st);
        else if (cancel && guestOnCancel_) guestOnCancel_(st);
        else if (cancel)              closeGuest();
    } else {
        // Chrome is drawn before the form. Let the focused editor process its blur first.
        if (ok) acceptRequested_ = true;
        else if (cancel) { acceptRequested_ = false; cancelEdits(st); }
    }
    return clusterW;
}

// Wave 1D: standalone wrapper — owns the FeatureManager-slot Begin/End plus the C37-F floating
// fallback, then defers the form content to drawBody(). The Wave 1D tabbed left pane calls
// drawBody() directly inside its "LeftPane" container child (no separate window).
void PropertyManager::draw(AppState& st) {
    if (!active_) return;

    // C37-F: Floating fallback — if the FeatureManager dock window isn't visible this frame
    // (dock closed or un-docked and minimised), draw the PM as an independent floating window
    // so it can never be invisibly active and trap keyboard/pick input.
    bool dockVisible = ImGui::Begin("FeatureManager");
    if (!dockVisible) {
        ImGui::End();
        // Open as a floating fallback.
        ImGui::SetNextWindowSize(ImVec2(260.0f * ui::uiScale, 400.0f * ui::uiScale),
                                 ImGuiCond_Appearing);
        ImGui::SetNextWindowPos(ImVec2(80.0f * ui::uiScale, 80.0f * ui::uiScale),
                                ImGuiCond_Appearing);
        // Frosted-glass fill so the floating PropertyManager fallback joins the app-wide glass
        // family (the docked FeatureManager keeps its solid panel chrome). Use the ADAPTIVE
        // tint for the intended window rect (NOT the theme-static pal.glassBg, which is dark in the
        // dark theme and gave black-on-black over the light viewport). drawBody's own pmSurf()
        // samples the same backdrop for text, so fill + text stay contrasting. Same panelId as the
        // docked column so both resolve to the same cached surface.
        const float uS = ui::uiScale;
        const ImRect floatRect(ImVec2(80.0f * uS, 80.0f * uS),
                               ImVec2((80.0f + 260.0f) * uS, (80.0f + 400.0f) * uS));
        const ui::AdaptiveSurface& fsurf =
            ui::adaptivePanel(ui::panelId("host_left_glass"), floatRect, ui::GlassGroup::FeatureManager);
        ImGui::PushStyleColor(ImGuiCol_WindowBg,
            ImVec4(fsurf.tint.x, fsurf.tint.y, fsurf.tint.z, 0.92f));
        const bool pmFloat = ImGui::Begin("##PMFloating", nullptr,
                          ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoNav);
        ImGui::PopStyleColor();
        if (!pmFloat) {
            ImGui::End(); return;
        }
    }
    drawBody(st);
    ImGui::End();
}

// A popup owns Escape before the underlying PM. ImGui only closes popups on
// Escape when keyboard navigation is enabled; keep the normal mouse-first
// configuration and close only the focused, non-modal popup opened by this PM.
static bool pmPopupOwnsEscape() {
    ImGuiContext& g = *ImGui::GetCurrentContext();
    if (!g.OpenPopupStack.empty()) {
        ImGuiWindow* popup = g.OpenPopupStack.back().Window;
        ImGuiWindow* pmWindow = ImGui::GetCurrentWindow();
        if (popup && !(popup->Flags & ImGuiWindowFlags_Modal) && g.NavWindow &&
            ImGui::IsWindowWithinBeginStackOf(popup, pmWindow) &&
            ImGui::IsWindowWithinBeginStackOf(g.NavWindow, popup) &&
            !(g.IO.ConfigFlags & ImGuiConfigFlags_NavEnableKeyboard) && !g.IO.WantTextInput)
            ImGui::ClosePopupToLevel(g.OpenPopupStack.Size - 1, true);
        // An unrelated or modal popup must handle its own Escape, not the PM's.
        return true;
    }
    // With keyboard navigation enabled ImGui can close the popup in NewFrame,
    // before drawBody sees the same key. Do not also cancel the underlying PM.
    if (g.IO.ConfigFlags & ImGuiConfigFlags_NavEnableKeyboard)
        for (ImGuiWindow* window : g.Windows)
            if (window->WasActive && (window->Flags & ImGuiWindowFlags_Popup) &&
                window->LastFrameActive == g.FrameCount - 1)
                return true;
    return false;
}

// Wave 1D: the form's inner content (no Begin/End). Caller owns the surrounding window/child.
void PropertyManager::drawBody(AppState& st) {
    if (!guestActive_ && (!active_ || !form_)) return;
    const bool sketchActive = st.sketchMode && st.sketchMode->active();
    const bool escPressed = ImGui::IsKeyPressed(ImGuiKey_Escape, false);
    const bool popupOwnsEscape = escPressed && !st.sketchPlanePickActive && !sketchActive &&
                                 pmPopupOwnsEscape();
    // Guest assembly forms (Mate/Insert/…) occupy the same left-pane slot as feature forms.
    if (guestActive_) {
        const ui::AdaptiveSurface& surf = pmSurf();
        ImGui::PushStyleColor(ImGuiCol_FrameBg,
            ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.16f));
        ImGui::PushStyleColor(ImGuiCol_FrameBgHovered,
            ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.26f));
        ImGui::PushStyleColor(ImGuiCol_FrameBgActive,
            ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.30f));
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);

        // Esc cancels the guest form only after a popup or active editor releases it.
        if (escPressed && !popupOwnsEscape &&
            !st.sketchPlanePickActive && !sketchActive && !ImGui::GetIO().WantTextInput) {
            if (guestOnCancel_) guestOnCancel_(st);
            else closeGuest();
            ImGui::PopStyleColor(4);
            return;
        }

        pmDrawFormHeader(guestIcon_.c_str(), guestTitle_.c_str(), nullptr);
        ImGui::Separator();
        if (guestBody_) guestBody_(st);

        ImGui::PopStyleColor(4);
        return;
    }


    // Keep a VISIBLE selector well active at all times. A method combo can HIDE the currently-
    // active well (ref_axis/ref_plane use duplicate-key, method-gated wells: switching
    // "Cylindrical Face" -> "Two Planes" hides the face well and reveals two plane wells). The
    // active flag would stay stranded on the now-hidden well, leaving NO visible active well — so
    // the datum-plane/axis picker goes dark and the references don't show until the user clicks the
    // new well. Re-home the active flag onto the first visible selector well (preferring an empty
    // one) so the relevant references are pickable the instant a form opens OR its method changes —
    // SolidWorks selection-first behaviour, for every tool. Only fires when nothing visible is
    // active, so it never steals a well the user is actively filling. (Lives in drawBody, not
    // draw(), because the tabbed left pane calls drawBody() directly — see layout.cpp.)
    {
        // After a deliberate endProfileSelection(), leave wells inactive until the user
        // re-arms (well chip click / region pick). Without this, the re-home undoes dismiss
        // every frame and empty-space click appears to do nothing.
        if (!suppressWellAutoArm_) {
            bool haveVisibleActive = false;
            for (const auto& f : fields_)
                if (f.def->kind == PMFieldKind::SelectorWell && f.wellActive &&
                    fieldVisible(*f.def, fields_)) { haveVisibleActive = true; break; }
            if (!haveVisibleActive) {
                PMField* target = nullptr;
                for (auto& f : fields_) {
                    if (f.def->kind != PMFieldKind::SelectorWell || !fieldVisible(*f.def, fields_))
                        continue;
                    if (!target) target = &f;                       // fallback: first visible well
                    if (f.picks.empty()) { target = &f; break; }    // prefer the first empty well
                }
                if (target) {
                    for (auto& o : fields_)
                        if (o.def->kind == PMFieldKind::SelectorWell ||
                            o.def->kind == PMFieldKind::FeatureWell)
                            o.wellActive = false;
                    target->wellActive = true;
                }
            }
        }
    }

    // Live unit-system toggle: if the document units changed while this form is open (e.g. the
    // user clicked the status-bar MMGS/IPS cluster), reformat every open Dim field's DISPLAY from
    // its canonical mm value. The stored value is unchanged — purely a display reformat.
    if (st.ips() != ips_) { ips_ = st.ips(); reformatDimDisplays(); }

    const auto& pal = ui::palette;
    // Liquid glass v2: the ONE shared column surface (same id layout.cpp painted this frame). Used
    // for adaptive max-contrast SECONDARY/dim text throughout the form so it reads against the live
    // glass tint; semantic roles (accent/status) stay on `pal`. Idempotent per frame (id-cached).
    const ui::AdaptiveSurface& surf = pmSurf();

    // Fix black-on-black: SliderFloat/DragFloat/InputText/BeginCombo draw an OPAQUE dark FrameBg
    // by default. Over the fully-transparent FeatureManager column the adaptive `surf.textPrimary`
    // is dark (contrasts the light viewport), so dark ink on dark frame = invisible text.
    // Push a translucent glass-tinted frame fill so the viewport shows through the field boxes and
    // the adaptive ink stays legible over both light and dark viewport backdrops. Must be popped on
    // EVERY exit path of this function (balanced below at each return and at function end).
    ImGui::PushStyleColor(ImGuiCol_FrameBg,
        ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.16f));
    ImGui::PushStyleColor(ImGuiCol_FrameBgHovered,
        ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.26f));
    ImGui::PushStyleColor(ImGuiCol_FrameBgActive,
        ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.30f));
    // value/default text must also track the live glass tint. Field VALUE widgets
    // (SliderFloat/DragFloat/InputText "##dim", Int, Text) render with the DEFAULT ImGuiCol_Text,
    // which is the theme-static pal.text (light in dark theme). Over the light viewport the group
    // card + frame fills are the adaptive LIGHT tint, so theme-static light value text washed out
    // (and dark-on-dark in the palette-fallback regime). Push surf.textPrimary as the default so
    // values contrast the same surface the labels/fills use. In the palette-fallback regime
    // surf.textPrimary == pal.text, so this is a strict no-op there and only corrects the
    // live-backdrop regime. Popped on EVERY exit path (balanced with the 3 FrameBg colors below).
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);

    // ---- C37-F: Keyboard policy -------------------------------------------
    // Gather focus/hover state for this PM window BEFORE processing buttons.
    bool pmWindowFocused = ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows);
    bool pmWindowHovered = ImGui::IsWindowHovered(ImGuiHoveredFlags_RootAndChildWindows |
                                                   ImGuiHoveredFlags_AllowWhenBlockedByActiveItem);
    // C38 (P2f): use last frame's PM-scoped "a PM widget was active" flag (set at the END of this
    // draw, below) instead of a global, prior-frame ImGui::IsAnyItemActive() — the latter is true
    // whenever ANY window has an active item, so Enter in an unrelated panel would commit the PM.
    bool pmWidgetWasActive = pmWidgetActivePrevFrame_;

    const ImGuiIO& io = ImGui::GetIO();

    // C37-F: Esc cancels from anywhere UNLESS sketch-plane-pick mode owns Esc.
    // When a text field is active (WantTextInput), the first Esc deactivates the field (ImGui
    // handles this natively via IsItemDeactivatedAfterEdit / InputText Escape handling).
    // We check WantTextInput BEFORE the key is consumed to avoid double-cancelling the PM.
    // A focused popup also receives the first Escape without cancelling the form.
    bool cancel = false;
    bool ok     = false;
    // C38 (P1f): sketch mode owns Esc while a sketch is active (its Modify box / tool / cancel all
    // consume it). Without this guard the PM grabs the same Esc the frame the sketch Modify box
    // closes and cancels the PM out from under the user. Skip PM Esc whenever a sketch is active, in
    // addition to the existing plane-pick guard.
    if (escPressed && !popupOwnsEscape && !st.sketchPlanePickActive && !sketchActive) {
        if (io.WantTextInput) {
            // A text field is active; Esc will blur it this frame (ImGui's default behaviour).
            // Don't cancel the PM — let the field deactivate first.
        } else {
            cancel = true;
        }
    }

    // ---- header: polished glass command strip ----------------------------------
    {
        const float S         = ui::uiScale;
        const float glyphSz   = 26.0f * S;
        const float hdrPadV   = 7.0f * S;
        const float hdrPadH   = 10.0f * S;
        const float accentBarW = 4.0f * S;
        const float rowAvail  = std::max(1.0f, ImGui::GetContentRegionAvail().x);
        const float hdrH      = glyphSz + hdrPadV * 2.0f;

        // Mode-aware icon: use the cut variant for boss/cut operations when mode_ is "cut".
        const char* hdrIcon = form_->icon;
        {
            const std::string& ft = form_->type;
            if (mode_ == "cut") {
                if      (ft == "extrude") hdrIcon = "extrude_cut";
                else if (ft == "revolve") hdrIcon = "revolve_cut";
                else if (ft == "sweep")   hdrIcon = "sweep_cut";
                else if (ft == "loft")    hdrIcon = "loft_cut";
            }
        }

        // Draw glass background + left accent bar before advancing the cursor.
        ImVec2 hdrMin = ImGui::GetCursorScreenPos();
        ImVec2 hdrMax(hdrMin.x + rowAvail, hdrMin.y + hdrH);
        ImDrawList* dl = ImGui::GetWindowDrawList();
        ui::glassBackground(dl, hdrMin, hdrMax, 6.0f);
        {
            const float bi = 4.0f * S;
            dl->AddRectFilled(ImVec2(hdrMin.x,             hdrMin.y + bi),
                              ImVec2(hdrMin.x + accentBarW, hdrMax.y - bi),
                              Palette::toU32(pal.accent), 2.0f * S);
        }

        // Feature glyph (26 dp; use @32 atlas cell for sharpness).
        ImGui::SetCursorScreenPos(ImVec2(hdrMin.x + accentBarW + hdrPadH, hdrMin.y + hdrPadV));
        float afterGlyphX = hdrMin.x + accentBarW + hdrPadH;
        IconUV ic = getIcon(hdrIcon, 32);
        if (ic.valid) {
            ImGui::Image(ic.texId, ImVec2(glyphSz, glyphSz), ic.uv0, ic.uv1);
            afterGlyphX += glyphSz + 8.0f * S;
        }

        // Title — vertically centred, in accent color.
        float titleY = hdrMin.y + (hdrH - ImGui::GetTextLineHeight()) * 0.5f;
        ImGui::SetCursorScreenPos(ImVec2(afterGlyphX, titleY));
        pushTextColor(pal.accent);
        ImGui::TextUnformatted(title_.c_str());
        ImGui::PopStyleColor();

        // OK / Cancel buttons no longer live in this in-PM header strip. They were
        // relocated ABOVE the PM — into the left-pane top row, between the
        // Configurations tab and the collapse chevron — and are rendered by drawChromeButtons(),
        // which layout.cpp calls from that row while a form is active. The keyboard Enter/Esc commit
        // path (below) is unchanged and still drives commit()/cancelEdits() directly.

        // Resume layout below the glass header strip.
        ImGui::SetCursorScreenPos(ImVec2(hdrMin.x, hdrMax.y + 4.0f * S));

        // Optional form description.
        const char* desc = formDescription(form_->type);
        if (desc && desc[0]) {
            pushTextColorV(surf.textSecondary);
            float wrapW = ImGui::GetContentRegionAvail().x;
            ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + wrapW);
            ImGui::TextUnformatted(desc);
            ImGui::PopTextWrapPos();
            ImGui::PopStyleColor();
        }

        // Tight gap under the description / header — Spacing() was too loose above the AI button.
        ImGui::Dummy(ImVec2(0, 2.0f * S));
        if (aiBusy_) ImGui::BeginDisabled();
        ImGui::PushStyleColor(ImGuiCol_Button,
            ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.20f));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
            ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.35f));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive,
            ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.50f));
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        if (ImGui::Button(aiBusy_ ? "AI is selecting..." : "Select with AI", ImVec2(-1, 0))) {
            requestAiAssist(st);
        }
        ImGui::PopStyleColor(4);
        if (aiBusy_) ImGui::EndDisabled();
        if (ImGui::IsItemHovered())
            pmTooltip("Ask AI to fill safe visible fields or activate the next reference well");
        if (!aiMessage_.empty()) {
            pushTextColor(aiBusy_ ? pal.textDim : pal.accent);
            ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x);
            ImGui::TextUnformatted(aiMessage_.c_str());
            ImGui::PopTextWrapPos();
            ImGui::PopStyleColor();
        }

        // C37-F: Enter commits ONLY when the PM window is focused/hovered or a PM widget was
        // active this frame. This prevents Enter-in-chat from accidentally committing the form.
        //
        // BUT: while a text field (a Dim/Int/Text input) is being edited, Enter means "apply the
        // value I just typed to the preview" — NOT "OK the whole feature". io.WantTextInput is true
        // on the frame Enter is pressed (the InputText is still the active item; it deactivates
        // later this same frame), so gating on it lets the field's own EnterReturnsTrue handler take
        // the Enter (parse + schedule preview) instead of the form committing out from under the
        // user. With focus then released, a second Enter (no field active) commits as before.
        if (ImGui::IsKeyPressed(ImGuiKey_Enter, false) && !io.WantTextInput &&
            (pmWindowFocused || pmWindowHovered || pmWidgetWasActive)) {
            ok = true;
        }

        ImGui::Separator();

        // Wave 1D: drawBody() does NOT own the window — early-out is a plain return; the caller
        // (draw() or the LeftPane container) closes its own Begin/child.
        if (ok) acceptRequested_ = true;
        if (cancel) { ImGui::PopStyleColor(4); cancelEdits(st); return; }
    }

    // ---- field rows grouped by `group`, collapsible ------------------------
    const char* curGroup = nullptr;
    bool groupOpen = true;
    bool groupCardOpen = false;
    const bool useIps = ips_;   // document-unit display (set at open; re-synced above on toggle)

    // C37-F: track which Dim field is first/editable for SetKeyboardFocusHere.
    bool firstEditableDimSeen = false;
    // True if the user clicked any SelectorWell chip this frame (re-arm). Exempts that click
    // from the post-loop "click elsewhere in PM ends profile select" dismiss.
    bool wellClickedThisFrame = false;

    // PM group cards are CONTENT-sized groups, not auto-resize child windows. An AutoResizeY child
    // sizes itself from LAST frame's content, so on the frame a form opens (feature edit, command,
    // scenario capture) every card painted as an empty padding-tall shell with its fields clipped
    // away. A group measures this frame's content: fields draw on draw-list channel 1 inside a
    // work rect inset by the card padding, and closeGroupCard paints the wash + border behind them
    // on channel 0 once the height is final, so the form is complete on its very first frame.
    const float cardPadX = 9.0f * ui::uiScale;
    const float cardPadY = 7.0f * ui::uiScale;
    ImVec2 cardMin(0.0f, 0.0f);
    float  cardRight = 0.0f;
    float  backupWorkMaxX = 0.0f, backupRegionMaxX = 0.0f;

    auto openGroupCard = [&]() {
        ImGuiWindow* win = ImGui::GetCurrentWindow();
        ImGui::PushID(curGroup);
        cardMin   = ImGui::GetCursorScreenPos();
        cardRight = cardMin.x + ImGui::GetContentRegionAvail().x;
        // Inset the right edge so full-width rows (wells, combos, "-1" widths, wrapped text) stop
        // at the card padding, exactly like the old child window's content region.
        backupWorkMaxX   = win->WorkRect.Max.x;
        backupRegionMaxX = win->ContentRegionRect.Max.x;
        win->WorkRect.Max.x          = cardRight - cardPadX;
        win->ContentRegionRect.Max.x = cardRight - cardPadX;
        win->DrawList->ChannelsSplit(2);
        win->DrawList->ChannelsSetCurrent(1);
        ImGui::BeginGroup();
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                            ImVec2(7.0f * ui::uiScale, 5.0f * ui::uiScale));
        // Group offset stays at the card edge (SameLine(labelW) columns measure from it, as they
        // did from the child's origin); new lines start one padding in.
        ImGui::Indent(cardPadX);
        ImGui::SetCursorScreenPos(ImVec2(cardMin.x + cardPadX, cardMin.y + cardPadY));
        groupCardOpen = true;
    };

    auto closeGroupCard = [&]() {
        if (!groupCardOpen) return;
        ImGuiWindow* win = ImGui::GetCurrentWindow();
        ImGui::Unindent(cardPadX);
        ImGui::PopStyleVar();
        ImGui::EndGroup();
        const float cardBottom = ImGui::GetItemRectMax().y + cardPadY;
        win->WorkRect.Max.x          = backupWorkMaxX;
        win->ContentRegionRect.Max.x = backupRegionMaxX;
        // PM group cards adopt the ADAPTIVE glass tint (surf.tint) — NOT the theme-static
        // pal.glassBg — so the card fill tracks the SAME live-backdrop surface the labels and
        // values read against (surf.textPrimary/textSecondary). pal.glassBg is dark in the
        // dark theme, which over the light viewport gave dark card + dark adaptive label text
        // (black-on-black), and light-on-light when the tone flipped. surf.tint contrasts
        // surf.text* by construction in both themes and both regimes.
        const ImGuiStyle& stl = ImGui::GetStyle();
        const ImVec2 cardMax(cardRight, cardBottom);
        win->DrawList->ChannelsSetCurrent(0);
        win->DrawList->AddRectFilled(cardMin, cardMax,
            ImGui::ColorConvertFloat4ToU32(ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.55f)),
            stl.ChildRounding);
        if (stl.ChildBorderSize > 0.0f)
            win->DrawList->AddRect(cardMin, cardMax, Palette::toU32(pal.glassBorder),
                                   stl.ChildRounding, 0, stl.ChildBorderSize);
        win->DrawList->ChannelsMerge();
        // Advance the parent layout past the whole card (bottom padding included).
        ImGui::SetCursorScreenPos(ImVec2(cardMin.x, cardBottom));
        ImGui::Dummy(ImVec2(cardRight - cardMin.x, 0.0f));
        ImGui::PopID();
        ImGui::Dummy(ImVec2(0, 4.0f * ui::uiScale));
        groupCardOpen = false;
    };

    for (size_t i = 0; i < fields_.size(); ++i) {
        PMField& f = fields_[i];
        if (!fieldVisible(*f.def, fields_)) continue;
        // New group header?
        if (!curGroup || std::strcmp(curGroup, f.def->group) != 0) {
            closeGroupCard();
            curGroup = f.def->group;
            groupOpen = pmSectionHeader(curGroup, pal);
            if (groupOpen) openGroupCard();
        }
        if (!groupOpen) continue;

        // Conditional enable: grey-out when the sibling Combo condition isn't met or comingSoon.
        bool enabled = fieldEnabled(*f.def, fields_);
        if (!enabled) ImGui::BeginDisabled();

        ImGui::PushID((int)i);
        if (hasString(aiHighlights_, f.def->key)) {
            pushTextColor(pal.accent);
            ImGui::TextUnformatted("AI suggestion");
            ImGui::PopStyleColor();
        }
        switch (f.def->kind) {
            case PMFieldKind::Dim: {
                const bool isAngle  = pmFieldIsAngle(f.def);
                const bool unitless = pmFieldUnitless(f.def);
                // C37-F: unit suffix in label ("Radius [mm]"). A field may override it (angle =
                // "[deg]"; an empty override = no suffix).
                const char* unitSuffix = f.def->unitLabel ? f.def->unitLabel
                                                          : (useIps ? " [in]" : " [mm]");
                pmCompactLabel(f.def->label, unitSuffix, pal, enabled);
                if (f.def->comingSoon && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                    pmTooltip("Not yet supported by engine");

                // Widgets audit: bounded dims (angles, draft, k-factor) are sliders; open-ended
                // ones (depth, radii, spacing) are drags — both show the unit in the value text and
                // CTRL+click types an exact number. Unit-math / "=expr" entry lives behind the "="
                // toggle so the SolidWorks expression path is never lost. All conversion happens at
                // the widget edge: dimValue stays canonical mm/deg.
                const bool widgetIps = f.dimNumericEditing ? f.dimInputIps : useIps;
                const double toDisp = (!isAngle && !unitless && widgetIps) ? (1.0 / 25.4) : 1.0;
                const bool   bounded = f.def->dimMax > f.def->dimMin;
                // Per-kind step (canonical units): metadata, else 1 mm lengths (1/16 in in IPS),
                // 1 deg angles, 0.05 unit-less ratios. Drives the CTRL+wheel step + drag speed.
                double step = f.def->dimStep;
                if (step <= 0.0)
                    step = isAngle ? 1.0 : (unitless ? 0.05 : (useIps ? 25.4 / 16.0 : 1.0));
                const char* valFmt = isAngle ? "%.4g deg"
                                             : (unitless ? "%.4g" : (widgetIps ? "%.4g in" : "%.4g mm"));

                // Commit a canonical value coming from the slider/drag/wheel: clamp to the field
                // range, clear any expr/error, and resync the text buffer (the "=" path reads it).
                auto commitDim = [&](double v, bool immediate) {
                    v = std::max(v, f.def->dimMin);
                    if (bounded) v = std::min(v, f.def->dimMax);
                    f.dimValue = v; f.dimExpr.clear(); f.dimError.clear();
                    f.dimExprRawString = false;
                    f.dimValueKnown = true;
                    f.dimTextDirty = f.dimTextEditing = false;
                    f.dimInputIps = useIps;
                    std::string disp = pmFieldValueText(f.def, f.dimValue, useIps);
                    std::strncpy(f.dimBuf.data(), disp.c_str(), f.dimBuf.size() - 1);
                    dirty_ = true;
                    lastEditTime_ = immediate ? ImGui::GetTime() - kDebounceSec  // fire now
                                              : ImGui::GetTime();               // debounced
                };

                // Reserve room for the "=" toggle so the widget never shoves it past the pane edge.
                const float eqBtnW = ImGui::GetFrameHeight();
                ImGui::SetNextItemWidth(-(eqBtnW + 4.0f * ui::uiScale));

                // C37-F: SetKeyboardFocusHere on the very first editable Dim field when opening
                // (on a drag/slider this opens ImGui's CTRL+click text input, so typing works
                // immediately, same as the old InputText).
                if (wantFocusFirstField_ && enabled && !f.def->comingSoon && !firstEditableDimSeen) {
                    ImGui::SetKeyboardFocusHere(0);
                    firstEditableDimSeen = true;
                    // Don't clear wantFocusFirstField_ yet — clear it after this frame so the
                    // focus request is processed.
                }

                const bool textMode = !f.dimExpr.empty() || f.dimTextEntry;
                if (!textMode) {
                    float v = (float)(f.dimValue * toDisp);
                    // Value-fraction fill bar / drag track inside the numeric field.
                    // 0d717a1a ("PM UX + slider fix") fixed Signed() so open-ended dims correctly
                    // use DragFloat instead of a broken SliderFloat(-1e9..0). That also removed the
                    // native slider grab/track affordance on fields like plane Offset Distance.
                    // Restore a theme-colored fill that tracks the value for BOTH bounded sliders
                    // and open-ended drags, painted under the widget (transparent FrameBg) so
                    // typing (CTRL+click) + drag still work and the "=" equation button stays.
                    {
                        const float Sloc = ui::uiScale;
                        const ImGuiStyle& stl = ImGui::GetStyle();
                        const ImVec2 frameMin = ImGui::GetCursorScreenPos();
                        const float  frameW  = ImGui::CalcItemWidth();
                        const float  frameH  = ImGui::GetFrameHeight();
                        const ImVec2 frameMax(frameMin.x + frameW, frameMin.y + frameH);
                        const float  rnd     = stl.FrameRounding;
                        ImDrawList*  dl      = ImGui::GetWindowDrawList();

                        // Soft fraction in display units. Bounded = true range; positive open-ended
                        // grows a soft max with |value|/default/step; Signed (dimMin << 0) is
                        // zero-centered so negative offsets fill left of mid.
                        const bool signedOpen = !bounded && f.def->dimMin < -1.0e6;
                        float t = 0.0f;          // 0..1 (or -1..1 when signedOpen)
                        float midX = 0.0f, halfW = 0.0f;
                        if (bounded) {
                            const float mn = (float)(f.def->dimMin * toDisp);
                            const float mx = (float)(f.def->dimMax * toDisp);
                            t = (mx > mn) ? (v - mn) / (mx - mn) : 0.0f;
                            t = std::max(0.0f, std::min(1.0f, t));
                        } else if (signedOpen) {
                            const float defAbs = (float)std::fabs(f.def->dimDefault * toDisp);
                            const float soft = std::max(std::max(std::fabs(v), defAbs),
                                                        (float)(step * toDisp) * 16.0f) * 1.5f;
                            t = (soft > 1e-12f) ? (v / soft) : 0.0f;
                            t = std::max(-1.0f, std::min(1.0f, t));
                            midX  = (frameMin.x + frameMax.x) * 0.5f;
                            halfW = (frameMax.x - frameMin.x) * 0.5f;
                        } else {
                            const float mn = (float)(f.def->dimMin * toDisp);
                            const float defAbs = (float)std::fabs(f.def->dimDefault * toDisp);
                            const float softMax = std::max(std::max(std::max(v, mn), defAbs),
                                                           (float)(step * toDisp) * 16.0f) * 1.5f;
                            t = (softMax > mn) ? (v - mn) / (softMax - mn) : 0.0f;
                            t = std::max(0.0f, std::min(1.0f, t));
                        }

                        // Track base (glass tint) — replaces the opaque FrameBg we push transparent.
                        dl->AddRectFilled(frameMin, frameMax,
                            ImGui::ColorConvertFloat4ToU32(
                                ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.18f)), rnd);
                        // Value fill (accentSoft via palette — theme lint ok).
                        if (signedOpen) {
                            const float x0 = (t >= 0.0f) ? midX : (midX + t * halfW);
                            const float x1 = (t >= 0.0f) ? (midX + t * halfW) : midX;
                            if (x1 > x0 + 0.5f) {
                                const ImDrawFlags corners = (t >= 0.0f)
                                    ? ImDrawFlags_RoundCornersRight
                                    : ImDrawFlags_RoundCornersLeft;
                                // Near full-range either side: round both ends of that half.
                                const ImDrawFlags fl = (std::fabs(t) >= 0.999f)
                                    ? ImDrawFlags_RoundCornersAll : corners;
                                dl->AddRectFilled(ImVec2(x0, frameMin.y), ImVec2(x1, frameMax.y),
                                                  Palette::toU32(pal.accentSoft), rnd, fl);
                            }
                            // Zero tick (hairline) so signed direction is readable.
                            dl->AddLine(ImVec2(midX, frameMin.y + 2.0f * Sloc),
                                        ImVec2(midX, frameMax.y - 2.0f * Sloc),
                                        ImGui::ColorConvertFloat4ToU32(
                                            ImVec4(pal.border[0], pal.border[1], pal.border[2],
                                                   0.55f)), 1.0f);
                        } else if (t > 0.001f) {
                            const float x1 = frameMin.x + (frameMax.x - frameMin.x) * t;
                            const ImDrawFlags fl = (t >= 0.999f)
                                ? ImDrawFlags_RoundCornersAll
                                : ImDrawFlags_RoundCornersLeft;
                            dl->AddRectFilled(frameMin, ImVec2(x1, frameMax.y),
                                              Palette::toU32(pal.accentSoft), rnd, fl);
                        }
                        // Value-edge tick (draggable-position cue).
                        {
                            float tickX;
                            if (signedOpen) tickX = midX + t * halfW;
                            else            tickX = frameMin.x + (frameMax.x - frameMin.x) * t;
                            const float tw = 1.5f * Sloc;
                            dl->AddRectFilled(ImVec2(tickX - tw, frameMin.y + 2.0f * Sloc),
                                              ImVec2(tickX + tw, frameMax.y - 2.0f * Sloc),
                                              Palette::toU32(pal.accent), 1.0f * Sloc);
                        }
                        // Subtle track border.
                        dl->AddRect(frameMin, frameMax,
                            ImGui::ColorConvertFloat4ToU32(
                                ImVec4(pal.border[0], pal.border[1], pal.border[2],
                                       pal.border[3] * 0.65f)),
                            rnd, 0, 1.0f);
                    }

                    // Transparent FrameBg so our pre-drawn track/fill is not covered; keep a light
                    // hover/active wash. Push SliderGrab so bounded sliders still show a knob.
                    ImGui::PushStyleColor(ImGuiCol_FrameBg,
                        ImVec4(0, 0, 0, 0));
                    ImGui::PushStyleColor(ImGuiCol_FrameBgHovered,
                        ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.10f));
                    ImGui::PushStyleColor(ImGuiCol_FrameBgActive,
                        ImVec4(surf.tint.x, surf.tint.y, surf.tint.z, 0.14f));
                    ImGui::PushStyleColor(ImGuiCol_SliderGrab,
                        ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1.0f));
                    ImGui::PushStyleColor(ImGuiCol_SliderGrabActive,
                        ImVec4(pal.accentDim[0], pal.accentDim[1], pal.accentDim[2], 1.0f));

                    bool widgetEdited;
                    if (bounded) {
                        widgetEdited = ImGui::SliderFloat("##dim", &v,
                                           (float)(f.def->dimMin * toDisp),
                                           (float)(f.def->dimMax * toDisp),
                                           valFmt, ImGuiSliderFlags_AlwaysClamp);
                    } else {
                        widgetEdited = ImGui::DragFloat("##dim", &v,
                                           (float)(step * toDisp) * 0.25f,
                                           (float)(f.def->dimMin * toDisp), FLT_MAX,
                                           valFmt, ImGuiSliderFlags_AlwaysClamp);
                    }
                    ImGui::PopStyleColor(5);
                    const bool wasNumericText = f.dimNumericEditing;
                    const ImGuiID itemId = ImGui::GetItemID();
                    const bool numericText = ImGui::TempInputIsActive(itemId);
                    const bool canceledScalar = wasNumericText && !numericText && escPressed;
                    if (numericText && !wasNumericText && !f.dimTextDirty)
                        f.dimInputIps = useIps;
                    if (numericText || wasNumericText) {
                        if (ImGuiInputTextState* state = ImGui::GetInputTextState(itemId)) {
                            if (state->Edited && state->TextA.Data) {
                                std::snprintf(f.dimBuf.data(), f.dimBuf.size(), "%s", state->TextA.Data);
                                f.dimTextDirty = true;
                            }
                        }
                        f.dimNumericEditing = numericText;
                        f.dimTextEditing = numericText;
                        if (canceledScalar) {
                            f.dimTextDirty = f.dimSyncPending = false;
                            f.dimError.clear();
                            f.dimInputIps = useIps;
                            const std::string display = pmFieldValueText(f.def, f.dimValue, useIps);
                            std::snprintf(f.dimBuf.data(), f.dimBuf.size(), "%s", display.c_str());
                        } else if (!numericText && f.dimTextDirty) {
                            if (parseFieldText(f, f.dimInputIps)) {
                                dirty_ = true;
                                lastEditTime_ = ImGui::GetTime() - kDebounceSec;
                                syncAcceptedDimText(f, useIps);
                            } else {
                                f.dimTextEntry = true; // expose invalid text for correction
                            }
                        }
                    } else if (widgetEdited && enabled && !f.def->comingSoon) {
                        commitDim((double)v / toDisp, /*immediate=*/false);
                    }
                    if (!canceledScalar && !numericText && ImGui::IsItemDeactivatedAfterEdit() && dirty_)
                        lastEditTime_ = ImGui::GetTime() - kDebounceSec;
                    // CTRL+wheel (or wheel mid-drag) steps by the field step. A bare hover-wheel
                    // scrolls the pane instead of silently mutating the value (widgets audit).
                    if (enabled && !f.def->comingSoon && io.MouseWheel != 0.0f &&
                        (ImGui::IsItemActive() || (ImGui::IsItemHovered() && io.KeyCtrl)))
                        commitDim(f.dimValue + step * (double)io.MouseWheel, /*immediate=*/true);
                } else {
                    const bool changed = ImGui::InputText("##dim", f.dimBuf.data(), f.dimBuf.size(),
                                                         ImGuiInputTextFlags_AutoSelectAll);
                    const bool activeText = ImGui::IsItemActive();
                    const bool deact = ImGui::IsItemDeactivatedAfterEdit();
                    if (ImGui::IsItemActivated() && !f.dimTextDirty && f.dimError.empty())
                        f.dimInputIps = useIps;
                    f.dimTextEditing = activeText;
                    if (enabled && changed) f.dimTextDirty = true;
                    const bool enter = activeText && ImGui::IsKeyPressed(ImGuiKey_Enter, false);
                    if (enabled && (enter || deact) && (f.dimTextDirty || !f.dimError.empty())) {
                        if (parseFieldText(f, f.dimInputIps)) {
                            dirty_ = true;
                            lastEditTime_ = enter ? ImGui::GetTime() - kDebounceSec : ImGui::GetTime();
                            syncAcceptedDimText(f, useIps);
                            if (deact && f.dimExpr.empty()) f.dimTextEntry = false;
                        }
                    }
                }
                if (f.dimSyncPending && !f.dimTextEditing && !f.dimTextDirty &&
                    f.dimError.empty() && f.dimExpr.empty()) {
                    const std::string text = pmFieldValueText(f.def, f.dimValue, useIps);
                    std::snprintf(f.dimBuf.data(), f.dimBuf.size(), "%s", text.c_str());
                    f.dimInputIps = useIps;
                    f.dimSyncPending = false;
                }

                // "=" toggle: switch the row to free text entry (unit math / equations). While an
                // expr is bound the row stays in text mode; the toggle then UNBINDS it (keeping
                // the last resolved value) and returns to the widget.
                ImGui::SameLine(0, 4.0f * ui::uiScale);
                {
                    const bool exprBound = !f.dimExpr.empty();
                    // Liquid glass v2: active "=" (text/expr mode) keeps the semantic accent wash;
                    // idle uses a translucent button fill so the column glass bleeds through (not the
                    // opaque theme button), and its glyph reads via adaptive secondary text.
                    ImGui::PushStyleColor(ImGuiCol_Button, textMode
                        ? ImVec4(pal.accentSoft[0], pal.accentSoft[1], pal.accentSoft[2],
                                 pal.accentSoft[3])
                        : ImVec4(pal.button[0], pal.button[1], pal.button[2], 0.40f));
                    ImGui::PushStyleColor(ImGuiCol_Text, textMode
                        ? ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1)
                        : surf.textSecondary);
                    const bool unresolved = exprBound && !f.dimValueKnown;
                    if (unresolved) ImGui::BeginDisabled();
                    if (ImGui::Button("=##dim_txt", ImVec2(eqBtnW, 0)) &&
                        enabled && !f.def->comingSoon) {
                        if ((f.dimTextDirty || !f.dimError.empty()) &&
                            !parseFieldText(f, f.dimInputIps)) {
                            // Do not discard invalid text when leaving the text editor.
                        } else if (exprBound) {
                            f.dimExpr.clear(); f.dimTextEntry = false;
                            commitDim(f.dimValue, /*immediate=*/true);
                        } else if (textMode) {
                            f.dimTextEntry = false;
                        } else {
                            f.dimTextEntry = true;
                            f.dimInputIps = useIps;
                        }
                    }
                    if (unresolved) ImGui::EndDisabled();
                    ImGui::PopStyleColor(2);
                    if (ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                        pmTooltip(unresolved
                            ? "Preview the equation before unbinding its resolved value"
                            : exprBound ? "Unbind the equation (keep the current value)"
                            : textMode ? "Back to slider entry"
                            : "Type units or an equation (25mm, 3/8in, =width/2)");
                }
                // expr resolved-value + tooltip
                if (enabled && !f.dimExpr.empty()) {
                    pushTextColor(pal.accent);
                    if (f.dimValueKnown)
                        ImGui::Text(u8"= %s  (%s)", f.dimExpr.c_str(),
                                    pmFieldValueText(f.def, f.dimValue, useIps).c_str());
                    else
                        ImGui::Text(u8"= %s  (not yet resolved)", f.dimExpr.c_str());
                    ImGui::PopStyleColor();
                    if (ImGui::IsItemHovered())
                        pmTooltip("Bound to equation '%s'", f.dimExpr.c_str());
                }
                // C37-F: inline red error — shown as a red hint line, not silently ignored.
                if (enabled && !f.dimError.empty()) {
                    pushTextColor(pal.statusErr);
                    ImGui::Text(u8"! %s", f.dimError.c_str());
#ifdef IMGUI_ENABLE_TEST_ENGINE
                    {
                        ImGuiContext& g = *GImGui;
                        IMGUI_TEST_ENGINE_ITEM_INFO(0, g.TempBuffer.Data, g.LastItemData.StatusFlags);
                    }
#endif
                    ImGui::PopStyleColor();
                }
                break;
            }
            case PMFieldKind::SelectorWell: {
                // Chip shows count + entity kind for filled wells.
                std::string chip;
                if (f.picks.empty()) {
                    chip = "(pick in viewport)";
                } else if (f.picks.size() == 1) {
                    chip = f.picks.front().label;
                } else {
                    // ASCII "x" — the UI fonts don't carry U+00D7 and a count suffix on a
                    // single pick was pure noise anyway.
                    chip = f.picks.front().label + " x" + std::to_string(f.picks.size());
                }
                auto wr = pmDrawSelectionWell("##well", f.def->label, chip.c_str(),
                                             f.wellActive, f.wellDangling);
                if (wr.clicked) {
                    for (auto& o : fields_)
                        if (o.def->kind == PMFieldKind::SelectorWell) o.wellActive = false;
                    f.wellActive = true;
                    suppressWellAutoArm_ = false;  // user re-armed a well
                    wellClickedThisFrame = true;
                }
                if (wr.rightClicked) {
                    f.picks.clear(); f.wellDangling = false;
                    dirty_ = true; lastEditTime_ = ImGui::GetTime();
                }
                break;
            }
            case PMFieldKind::Combo: {
                pmCompactLabel(f.def->label, "", pal, enabled);
                if (f.def->comingSoon && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                    pmTooltip("Not yet supported by engine");
                const auto& labels = f.comboLabels;
                const std::string unavailable = "Unsupported: " + f.comboUnsupported;
                const char* preview = !f.comboUnsupported.empty() ? unavailable.c_str() :
                    (legacyFillet_ && !legacyFilletModeKnown_ && std::strcmp(f.def->key, "type") == 0
                        ? "Automatic (legacy)"
                        : (f.comboIndex >= 0 && f.comboIndex < (int)labels.size()
                            ? labels[f.comboIndex].c_str() : ""));
                // Shared select handler for both render styles.
                auto selectOption = [&](int n) {
                    if (!enabled) return;
                    f.comboIndex = n; dirty_ = true; lastEditTime_ = ImGui::GetTime();
                    f.comboUnsupported.clear();
                    if (legacyFillet_ && std::strcmp(f.def->key, "type") == 0)
                        legacyFillet_ = legacyFilletModeKnown_ = false;
                    // SolidWorks: choosing an end/start condition that reveals a
                    // reference selection box auto-activates that box, so the next
                    // viewport click picks the plane/face/vertex (without this the
                    // active well stays on `profile` and the plane picker never arms).
                    // Re-home the active well onto the first now-visible, empty
                    // selector well gated by THIS combo.
                    const char* ckey = f.def->key;
                    PMField* reveal = nullptr;
                    for (auto& w : fields_) {
                        if (w.def->kind != PMFieldKind::SelectorWell) continue;
                        if (!w.def->enableKey ||
                            std::strcmp(w.def->enableKey, ckey) != 0) continue;
                        if (!w.picks.empty() || !fieldVisible(*w.def, fields_)) continue;
                        reveal = &w; break;
                    }
                    if (reveal) {
                        for (auto& o : fields_)
                            if (o.def->kind == PMFieldKind::SelectorWell ||
                                o.def->kind == PMFieldKind::FeatureWell)
                                o.wellActive = false;
                        reveal->wellActive = true;
                        suppressWellAutoArm_ = false;
                    }
                };
                if (f.def->segmented && labels.size() == 2) {
                    // Widgets audit: a combo-of-two (Boss|Cut, Both|One Side) renders as a
                    // segmented pill pair — one click to flip, no dropdown popup.
                    const float S = ui::uiScale;
                    const float gap  = 4.0f * S;
                    const float segW = (ImGui::GetContentRegionAvail().x - gap) * 0.5f;
                    for (int n = 0; n < 2; ++n) {
                        if (n) ImGui::SameLine(0, gap);
                        const bool selSeg = (f.comboIndex == n);
                        ImGui::PushID(n);
                        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 6.0f * S);
                        // Liquid glass v2: selected segment keeps the semantic accent wash; the idle
                        // segment uses a translucent frame fill so the frosted column bleeds through
                        // (not the opaque theme frameBg), with adaptive secondary text on top.
                        ImGui::PushStyleColor(ImGuiCol_Button, selSeg
                            ? ImVec4(pal.accentSoft[0], pal.accentSoft[1], pal.accentSoft[2],
                                     pal.accentSoft[3])
                            : ImVec4(pal.frameBg[0], pal.frameBg[1], pal.frameBg[2], 0.40f));
                        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, selSeg
                            ? ImVec4(pal.accentMid[0], pal.accentMid[1], pal.accentMid[2],
                                     pal.accentMid[3])
                            : ImVec4(pal.frameBgHovered[0], pal.frameBgHovered[1],
                                     pal.frameBgHovered[2], 0.60f));
                        ImGui::PushStyleColor(ImGuiCol_Text, selSeg
                            ? ImVec4(pal.accent[0], pal.accent[1], pal.accent[2], 1)
                            : surf.textSecondary);
                        if (ImGui::Button(labels[n].c_str(), ImVec2(segW, 0)) && !selSeg)
                            selectOption(n);
                        ImGui::PopStyleColor(3);
                        ImGui::PopStyleVar();
                        ImGui::PopID();
                    }
                } else {
                    ImGui::SetNextItemWidth(-1);
                    if (ImGui::BeginCombo("##combo", preview)) {
                        // The dropdown popup has its own opaque dark PopupBg — the adaptive (dark)
                        // ink pushed for the transparent column would be black-on-black here, so use
                        // the theme's normal light text for the popup items.
                        ImGui::PushStyleColor(ImGuiCol_Text,
                            ImVec4(pal.text[0], pal.text[1], pal.text[2], 1.0f));
                        for (int n = 0; n < (int)labels.size(); ++n) {
                            bool sel = (f.comboIndex == n);
                            if (ImGui::Selectable(labels[n].c_str(), sel)) selectOption(n);
                            if (sel) ImGui::SetItemDefaultFocus();
                        }
                        ImGui::PopStyleColor();
                        ImGui::EndCombo();
                    }
                }
                if (!f.comboUnsupported.empty()) {
                    pushTextColor(pal.statusErr);
                    ImGui::TextUnformatted("Choose a supported option before applying.");
                    ImGui::PopStyleColor();
                }
                break;
            }
            case PMFieldKind::Check: {
                // Check rows: label uses the full content width minus the checkbox, checkbox is
                // right-aligned at the trailing edge. Unlike Dim/Combo (fixed label column + field),
                // checkboxes are small so the label gets the remaining budget — "Reverse direction"
                // and similar must stay fully readable without the box landing on the text tail.
                const float avail = ImGui::GetContentRegionAvail().x;
                const float gap = ImGui::GetStyle().ItemInnerSpacing.x;
                // Square checkbox frame ≈ GetFrameHeight() (ImGui Checkbox size).
                const float chkW = ImGui::GetFrameHeight();
                const float labelBudget = std::max(0.0f, avail - chkW - gap);

                const ui::AdaptiveSurface& surf = pmSurf();
                pushTextColorV(enabled ? surf.textPrimary : surf.textSecondary);
                const char* full = f.def->label ? f.def->label : "";
                std::string shown = full;
                bool clipped = false;
                if (labelBudget > 0.0f &&
                    ImGui::CalcTextSize(shown.c_str()).x > labelBudget) {
                    clipped = true;
                    const char* ell = "\xE2\x80\xA6";  // U+2026 ellipsis
                    const float ellW = ImGui::CalcTextSize(ell).x;
                    while (!shown.empty() &&
                           ImGui::CalcTextSize(shown.c_str()).x + ellW > labelBudget)
                        shown.pop_back();
                    shown += ell;
                }
                ImGui::AlignTextToFramePadding();
                ImGui::TextUnformatted(shown.c_str());
                ImGui::PopStyleColor();
                if (clipped && ImGui::IsItemHovered())
                    pmTooltip("%s", full);

                // Right-align the checkbox at the trailing edge (same pattern as pick-hint).
                ImGui::SameLine(0.0f, 0.0f);
                {
                    const float remain = ImGui::GetContentRegionAvail().x;
                    if (remain > chkW)
                        ImGui::SetCursorPosX(ImGui::GetCursorPosX() + remain - chkW);
                }
                bool val = f.checkValue;
                if (ImGui::Checkbox("##chk", &val)) {
                    if (enabled) { f.checkValue = val; dirty_ = true; lastEditTime_ = ImGui::GetTime(); }
                }
                if (f.def->comingSoon && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
                    pmTooltip("Not yet supported by engine");
                break;
            }
            case PMFieldKind::Int: {
                pmCompactLabel(f.def->label, "", pal, enabled);
                ImGui::SetNextItemWidth(-1);
                // DragInt bounded to the field's intMin..intMax (AlwaysClamp covers CTRL+click
                // typed entry too) — a stray "100000 instances" can't reach the engine preview.
                int v = f.intValue;
                if (ImGui::DragInt("##int", &v, 0.1f, f.def->intMin, f.def->intMax, "%d",
                                   ImGuiSliderFlags_AlwaysClamp)) {
                    if (enabled) {
                        v = std::min(std::max(v, f.def->intMin), f.def->intMax);
                        if (v != f.intValue) {
                            f.intValue = v; dirty_ = true; lastEditTime_ = ImGui::GetTime();
                        }
                    }
                }
                if (ImGui::IsItemDeactivatedAfterEdit() && dirty_)
                    lastEditTime_ = ImGui::GetTime() - kDebounceSec;  // fire on release/commit
                break;
            }
            case PMFieldKind::Text: {
                pmCompactLabel(f.def->label, "", pal, enabled);
                ImGui::SetNextItemWidth(-1);
                const char* hint = f.def->placeholder ? f.def->placeholder : "";
                if (ImGui::InputTextWithHint("##text", hint, f.textBuf.data(), f.textBuf.size())) {
                    if (enabled) { dirty_ = true; lastEditTime_ = ImGui::GetTime(); }
                }
                break;
            }
            case PMFieldKind::DatumRef: {
                pmCompactLabel(f.def->label, "", pal, enabled);
                const auto& labels = f.comboLabels;
                const char* preview = f.comboIndex >= 0 && f.comboIndex < (int)labels.size()
                                          ? labels[f.comboIndex].c_str() : "";
                ImGui::SetNextItemWidth(-1);
                if (ImGui::BeginCombo("##datum", preview)) {
                    // Light text on the opaque dark popup (see the ##combo note above).
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(pal.text[0], pal.text[1], pal.text[2], 1.0f));
                    for (int n = 0; n < (int)labels.size(); ++n) {
                        bool sel = (f.comboIndex == n);
                        if (ImGui::Selectable(labels[n].c_str(), sel) && enabled) {
                            f.comboIndex = n;
                            for (auto& well : fields_)
                                if (well.def->kind == PMFieldKind::SelectorWell &&
                                    std::strcmp(well.def->key, f.def->key) == 0)
                                    well.picks.clear();
                            dirty_ = true; lastEditTime_ = ImGui::GetTime();
                        }
                        if (sel) ImGui::SetItemDefaultFocus();
                    }
                    ImGui::PopStyleColor();
                    ImGui::EndCombo();
                }
                break;
            }
            case PMFieldKind::FeatureWell: {
                pushTextColorV(surf.textSecondary);
                ImGui::TextUnformatted(f.def->label);
                ImGui::PopStyleColor();
                const bool single = !f.def->selMulti;   // FeatWell1: holds exactly one feature id
                std::string chip;
                if (f.picks.empty()) chip = "(select a feature in the tree, then Add)";
                else for (size_t k = 0; k < f.picks.size(); ++k) {
                    if (k) chip += ", ";
                    chip += f.picks[k].label;
                }
                ImGui::PushTextWrapPos(0.0f);
                ImGui::TextUnformatted(chip.c_str());
                ImGui::PopTextWrapPos();
                // Add the currently tree-selected feature to the list. A single-emit well
                // (selMulti=false) replaces its one pick instead of appending.
                if (ImGui::SmallButton("Add selected")) {
                    const std::string& fid = st.selectedFeatureId;
                    if (!fid.empty()) {
                        bool dup = false;
                        for (const auto& p : f.picks)
                            if (p.selector.is_string() && p.selector.get<std::string>() == fid) dup = true;
                        if (!dup) {
                            // Look up the feature's display name from the mirror tree.
                            std::string nm = fid;
                            const nlohmann::json& tree = st.docMirror.tree();
                            if (tree.is_object() && tree.contains("features") && tree["features"].is_array())
                                for (const auto& tf : tree["features"])
                                    if (tf.is_object() && tf.value("id", std::string()) == fid) {
                                        nm = tf.value("name", fid); break;
                                    }
                            if (single) f.picks.clear();   // single well holds one id only
                            PMSelEntry e; e.selector = fid; e.label = nm;
                            f.picks.push_back(std::move(e));
                            dirty_ = true; lastEditTime_ = ImGui::GetTime();
                        }
                    } else {
                        st.toast(ui::ToastLevel::Info, "Select a feature in the FeatureManager first");
                    }
                }
                ImGui::SameLine();
                if (ImGui::SmallButton("Clear##feat")) {
                    f.picks.clear(); dirty_ = true; lastEditTime_ = ImGui::GetTime();
                }
                break;
            }
        }
        ImGui::PopID();

        if (!enabled) ImGui::EndDisabled();
    }
    closeGroupCard();

    if (acceptRequested_) {
        acceptRequested_ = false;
        commit(st);
    }

    // C38 (P3f): After iterating all fields on the first draw after open, mark the focus request
    // handled UNCONDITIONALLY. Previously this only cleared when an editable Dim field was seen, so a
    // SelectorWell-first (or Dim-less) form re-issued SetKeyboardFocusHere every frame and stole
    // focus continuously. We still record focusedOnOpen_ only when a field actually took focus.
    if (wantFocusFirstField_) {
        if (firstEditableDimSeen) focusedOnOpen_ = true;
        wantFocusFirstField_ = false;
    }

    // Click elsewhere in the PM (dim/check/combo/header) while a profile is chosen and the
    // select session is still armed → end select (keep picks). Well-chip clicks re-arm above
    // and are exempted. Empty profile well stays armed (first-pick UX).
    if (!wellClickedThisFrame && hasActiveSelectorWell() && !profilePicks().empty() &&
        ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
        ImGui::IsWindowHovered(ImGuiHoveredFlags_RootAndChildWindows) &&
        !ImGui::IsAnyItemActive()) {
        // Hovered window is the PM (or its children) but no well chip claimed the click.
        endProfileSelection();
    }

    // C37-F: Inline preview error block (red text, shown when the last preview RPC failed).
    if (!previewError_.empty()) {
        ImGui::Separator();
        pushTextColor(pal.statusErr);
        float wrapW = ImGui::GetContentRegionAvail().x;
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + wrapW);
        ImGui::Text(u8"! Preview error: %s", previewError_.c_str());
        ImGui::PopTextWrapPos();
        ImGui::PopStyleColor();
    } else if (!previewWarning_.empty()) {
        // A successful preview may have an authoritative adjustment or other warning.
        ImGui::Separator();
        pushTextColor(pal.statusWarn);
        float wrapW = ImGui::GetContentRegionAvail().x;
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + wrapW);
        ImGui::Text(u8"! %s", previewWarning_.c_str());
        ImGui::PopTextWrapPos();
        ImGui::PopStyleColor();
    }

    // ---- live-preview status hint -----------------------------------------
    ImGui::Separator();
    pushTextColorV(surf.textSecondary);
    const char* status;
    if (acceptPending_ && !previewInFlight_ && !readyForPreview(form_, fields_))
        status = "Editing\xe2\x80\xa6 (live preview)";
    else if (acceptPending_) status = "Applying final result\xe2\x80\xa6";
    else if (previewInFlight_) status = "Updating preview\xe2\x80\xa6";
    else if (!previewError_.empty()) status = "Preview failed";
    else if (freshEditRequired_) status = "Make a fresh edit to update the preview";
    else if (dirty_ || !readyForPreview(form_, fields_))
        status = "Editing\xe2\x80\xa6 (live preview)";
    else if (firstPreviewDone_ && !targetId_.empty()) status = "Preview up to date";
    else status = "Set parameters to preview";
    ImGui::TextUnformatted(status);
#ifdef IMGUI_ENABLE_TEST_ENGINE
    ImGuiContext& g = *ImGui::GetCurrentContext();
    IMGUI_TEST_ENGINE_ITEM_INFO(0, status, g.LastItemData.StatusFlags);
#endif
    ImGui::PopStyleColor();

    // C38 (P2f): record whether a PM widget is active at the end of OUR draw, scoped to this PM
    // window (active item AND PM window focus). Read at the top of next frame's draw() to gate
    // Enter-commit. We're still inside the PM window's Begin/End here, so IsAnyItemActive() +
    // window-focus is PM-scoped — an active item in another window won't set this.
    pmWidgetActivePrevFrame_ = ImGui::IsAnyItemActive() &&
        ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows);

    // Balanced pop for the 3 FrameBg colors + 1 adaptive Text color pushed after surf was
    // resolved (above).
    ImGui::PopStyleColor(4);
}

}  // namespace ui
