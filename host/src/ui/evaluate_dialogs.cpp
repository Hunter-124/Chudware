// Evaluate dialogs implementation (C19 + Evaluate overhaul) — see evaluate_dialogs.h.
#include "evaluate_dialogs.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <unordered_map>

#include "imgui.h"
#include "imgui_internal.h"     // ImRect (ui::adaptivePanel / ui::glassPanel rect argument)

#include "appearance_common.h"
#include "layout.h"
#include "metrics.h"
#include "widgets/kit.h"
#include "../core/app_state.h"
#include "../renderer.h"
#include "../ui_theme.h"
#include "property_manager.h"   // pmFormatDim / Area / Volume / Mass / AreaMoment / MassMoment

namespace ui {

// Zebra stripe density used by the Zebra Stripes control window. File-scope so it persists across
// frames without adding a member to EvaluateDialogs (the class has a single instance). Matches the
// renderer's default frequency; the slider (4..60) drives it live via Renderer::setZebra().
static float gZebraFreq = 18.0f;

// Shared with the viewport and MCP measurement emphasis contract.
static const auto& kEvalPalette = ui::evalEmphasisPalette;

// Format a linear value (canonical mm) for an evaluate readout in the active document units.
static std::string evalLen(AppState& st, double mm) { return pmFormatDim(mm, st.ips()); }

struct MeasureSelection {
    nlohmann::json refs = nlohmann::json::array();
    std::string key;
    std::string error;
    bool resolving = false;
};

// Preserve every selected pick in the identity, including unresolved and failed picks.
// Never submit a resolved subset: that would label the wrong measurement as the selection.
static MeasureSelection measureSelection(const AppState& st) {
    MeasureSelection out;
    nlohmann::json identity = nlohmann::json::array();
    for (const auto& item : st.selection.items()) {
        identity.push_back({item.hit.body_id, item.hit.topo_id, (int)item.hit.kind,
                            item.pending_resolve, item.selector, item.resolution_error});
        out.resolving |= item.pending_resolve;
        if (!item.resolution_error.empty()) {
            if (out.error.empty()) out.error = item.resolution_error;
            continue;
        }
        if (item.pending_resolve) continue;
        if (item.hit.body_id == 0 || item.selector.empty()) {
            out.error = "Selected geometry has no resolved body reference. Remove it and pick again.";
            continue;
        }
        auto selector = nlohmann::json::parse(item.selector, nullptr, false);
        if (selector.is_discarded() || !selector.is_object()) {
            out.error = "Selected geometry returned an invalid reference. Remove it and pick again.";
            continue;
        }
        out.refs.push_back({{"body_id", item.hit.body_id}, {"selector", std::move(selector)}});
    }
    if (st.selection.size() > 8)
        out.error = "Measure supports up to 8 selections. Remove selections to continue.";
    out.key = identity.dump();
    return out;
}

// True when the open doc is an assembly or multi-body part — field analyses (draft/thickness/
// curvature) only support a single solid body.
static bool fieldEvalUnsupported(AppState& st) {
    if (!st.docMirror.hasDoc()) return false;
    const auto& tree = st.docMirror.tree();
    if (tree.is_object() && tree.value("schema", std::string()) == "aicad.asm/1") return true;
    if (st.docBodyIds.size() > 1) return true;
    return false;
}

// Copy palette color into dst with override alpha.
static void copyRgba(float dst[4], const float src[4], float a = -1.0f) {
    dst[0] = src[0];
    dst[1] = src[1];
    dst[2] = src[2];
    dst[3] = (a >= 0.0f) ? a : src[3];
}

// Read [x,y,z] from a JSON array into out; returns false if missing/invalid.
static bool readXYZ(const nlohmann::json& j, float out[3]) {
    if (!j.is_array() || j.size() < 3) return false;
    if (!j[0].is_number() || !j[1].is_number() || !j[2].is_number()) return false;
    out[0] = (float)j[0].get<double>();
    out[1] = (float)j[1].get<double>();
    out[2] = (float)j[2].get<double>();
    return true;
}

// Flatten polyline [[x,y,z],...] into consecutive xyz floats for EvalHighlight.
static void flattenPolyline(const nlohmann::json& pl, std::vector<float>& out) {
    out.clear();
    if (!pl.is_array()) return;
    out.reserve(pl.size() * 3);
    for (const auto& p : pl) {
        float xyz[3];
        if (!readXYZ(p, xyz)) continue;
        out.push_back(xyz[0]);
        out.push_back(xyz[1]);
        out.push_back(xyz[2]);
    }
}

// Build name -> body_id from the open doc (assembly component order / part "bN" names).
static std::unordered_map<std::string, int> buildNameToBodyId(AppState& st) {
    std::unordered_map<std::string, int> map;
    if (!st.docMirror.hasDoc()) return map;
    const auto& tree = st.docMirror.tree();
    if (!tree.is_object()) return map;

    if (tree.value("schema", std::string()) == "aicad.asm/1") {
        // Non-suppressed components map 1-based ordinal == AICM body_id.
        if (tree.contains("components") && tree["components"].is_array()) {
            uint32_t ordinal = 0;
            for (const auto& c : tree["components"]) {
                if (!c.is_object()) continue;
                if (c.value("suppressed", false)) continue;
                ++ordinal;
                std::string name = c.value("name", std::string());
                if (name.empty()) name = c.value("id", std::string());
                if (!name.empty()) map[name] = (int)ordinal;
            }
        }
    } else {
        // Part multi-body: engine names bodies "b1","b2",… matching AICM body ids.
        for (uint32_t bid : st.docBodyIds) {
            map["b" + std::to_string(bid)] = (int)bid;
            map["Body" + std::to_string(bid)] = (int)bid;
            map["Body " + std::to_string(bid)] = (int)bid;
        }
        // Also accept bare numeric strings.
        for (uint32_t bid : st.docBodyIds) map[std::to_string(bid)] = (int)bid;
    }
    // Prefer docBodyIds order when names come from parts[] as "bN" even if tree is empty.
    if (map.empty()) {
        for (uint32_t bid : st.docBodyIds) map["b" + std::to_string(bid)] = (int)bid;
    }
    return map;
}

using MeasureRows = std::vector<std::pair<std::string, std::string>>;

// Render only quantities actually supplied by the kernel. Radius and diameter accompany
// arc length rather than competing with it for a single summary slot.
static MeasureRows measureRows(AppState& st, const nlohmann::json& result) {
    MeasureRows rows;
    auto number = [&](const char* key, const char* label, auto format) {
        const auto it = result.find(key);
        if (it != result.end() && it->is_number() && std::isfinite(it->get<double>()))
            rows.emplace_back(label, format(it->get<double>()));
    };
    auto length = [&](double value) { return evalLen(st, value); };
    number("distance", "Minimum distance", length);
    number("length", "Length", length);
    number("radius", "Radius", length);
    number("diameter", "Diameter", length);
    if (!result.contains("diameter"))
        number("radius", "Diameter", [&](double radius) { return length(2.0 * radius); });
    number("perimeter", "Perimeter", length);
    number("area", "Area", [&](double value) { return pmFormatArea(value, st.ips()); });
    number("volume", "Volume", [&](double value) { return pmFormatVolume(value, st.ips()); });
    number("total_length", "Total length", length);
    number("total_distance", "Chain distance", length);
    number("angle", "Angle", [](double angle) {
        char value[48];
        std::snprintf(value, sizeof(value), "%.4f deg", angle);
        return std::string(value);
    });
    for (const char* key : {"segment_lengths", "distances"}) {
        const auto it = result.find(key);
        if (it == result.end() || !it->is_array()) continue;
        for (size_t i = 0; i < it->size(); ++i) {
            const auto& value = (*it)[i];
            if (!value.is_number() || !std::isfinite(value.get<double>())) continue;
            const std::string label = std::string(key[0] == 's' ? "Segment " : "Distance ") +
                                      std::to_string(i + 1);
            rows.emplace_back(label, length(value.get<double>()));
        }
    }
    if (result.value("kind", std::string()) == "point" && result.contains("points") &&
        result["points"].is_array() && !result["points"].empty()) {
        const auto& p = result["points"][0];
        if (p.is_array() && p.size() == 3) {
            const char* axes[] = {"X", "Y", "Z"};
            for (int i = 0; i < 3; ++i)
                if (p[i].is_number()) rows.emplace_back(axes[i], length(p[i].get<double>()));
        }
    }
    return rows;
}

static std::string measureReadingText(AppState& st, const nlohmann::json& result) {
    std::string text;
    for (const auto& row : measureRows(st, result)) {
        if (!text.empty()) text += "; ";
        text += row.first + ": " + row.second;
    }
    return text;
}

static void drawColorChipRow(const char* text, const float rgba[4]) {
    ImGui::PushID(text);
    if (kit::cardBegin("##colored_reading", nullptr, nullptr, nullptr, rgba))
        ImGui::TextWrapped("%s", text);
    kit::cardEnd();
    ImGui::PopID();
}

static const char* kPlaneNames[] = {"plane.top", "plane.front", "plane.right"};

void EvaluateDialogs::clearOverlays() {
    // Shared exclusive-tool overlays only. Mass markers / section planes have independent
    // lifetimes tied to massOpen_ / sectionOpen_ so concurrent windows survive measure clears.
    overlaySegs_.clear();
    overlayLabels_.clear();
    overlayHighlights_.clear();
    overlayCallouts_.clear();
    interferenceView_ = InterferenceView{};
    // Preserve massBodyAlpha_ when mass or section is still open (their rebuilders own it).
    if (!massOpen_ && !sectionOpen_) massBodyAlpha_ = 1.0f;
}

const std::vector<float>& EvaluateDialogs::overlaySegments() const {
    // Compose measure segs + mass COM/axis segs without thrashing either source buffer.
    if (massSegs_.empty()) return overlaySegs_;
    if (overlaySegs_.empty()) return massSegs_;
    composedSegs_.clear();
    composedSegs_.reserve(overlaySegs_.size() + massSegs_.size());
    composedSegs_.insert(composedSegs_.end(), overlaySegs_.begin(), overlaySegs_.end());
    composedSegs_.insert(composedSegs_.end(), massSegs_.begin(), massSegs_.end());
    return composedSegs_;
}

float EvaluateDialogs::requestedBodyAlpha() const {
    if (sectionOpen_) return 0.45f;
    if (massOpen_ && !massMarkers_.empty()) return 0.45f;
    return massBodyAlpha_;  // interference translucency path
}

void EvaluateDialogs::clearFieldColormap(AppState& st) {
    st.colormapOn = false;
    st.legendOn = false;
    if (st.renderer) st.renderer->setColormapMode(false);
}

void EvaluateDialogs::seedMassFromDoc(AppState& st) {
    massDocKey_.clear();
    massDocName_ = "(not set)";
    // Density falls back to steel only when the part has no material and the user has not set a
    // custom override. Callers that open the window clear massCustomDensity_ first so this seeds
    // the live density from the part material.
    if (!massCustomDensity_) massDensity_ = 7.85f;
    if (!st.docMirror.hasDoc()) return;
    const auto& tree = st.docMirror.tree();
    if (!tree.is_object()) return;
    auto m = tree.find("material");
    if (m == tree.end() || !m->is_object()) return;
    std::string key = m->value("key", std::string());
    if (key.empty()) return;
    massDocKey_ = key;
    massDocName_ = materialDisplayName(key);
    if (!massCustomDensity_) massDensity_ = materialDensityGcm3(key);
}

// C39: evals run async on st.rpc — heavy methods (interference, thickness on a dense model)
// used to freeze the window for their full duration. The callback runs on the GL thread
// inside the per-frame drain; gen_ guards against a newer eval superseding a pending one.
// Exclusive-tool slot (measure/check/interference/symmetry) — not mass/section.
void EvaluateDialogs::runEval(AppState& st, const std::string& method,
                              const nlohmann::json& extra) {
    const int gen = ++gen_;
    clearOverlays();
    errorText_.clear();
    result_ = nlohmann::json::object();
    pending_ = false;
    if (!st.docMirror.hasDoc()) {
        errorText_ = "No document loaded.";
        return;
    }
    nlohmann::json params = {{"doc_id", st.docMirror.docId()}};
    for (auto it = extra.begin(); it != extra.end(); ++it) params[it.key()] = it.value();
    pending_ = true;
    const std::string docId = st.docMirror.docId();
    const int revision = st.docMirror.revision();
    const std::string selectionKey = method == "eval_measure" ? lastMeasureRefs_ : std::string();
    const bool fullInterferenceInventory = method == "eval_interference" && !extra.contains("members");
    ++st.rebuildsInFlight;
    st.rpc.call(method, params, [this, &st, gen, method, docId, revision, selectionKey, fullInterferenceInventory](const core::RpcResult& r) {
        --st.rebuildsInFlight;
        if (gen != gen_ || openWindow_.empty()) return;
        if (st.docMirror.docId() != docId || st.docMirror.revision() != revision) {
            evalDocRevision_ = -1;  // refresh this per-document window when it is reactivated
            pending_ = false;
            return;
        }
        if (method == "eval_measure" && measureSelection(st).key != selectionKey) return;
        pending_ = false;
        if (!r.ok) {
            errorText_ = r.err.empty() ? ("worker call failed: " + method) : r.err;
            if (openWindow_ == "interference") rebuildInterferenceView(st);
            return;
        }
        if (r.result.is_object() && r.result.contains("code")) {
            errorText_ = r.result.value("message", std::string("evaluate error"));
            if (openWindow_ == "interference") rebuildInterferenceView(st);
            return;
        }
        if (!r.result.is_object()) {
            errorText_ = "Analysis returned an invalid result.";
            return;
        }
        result_ = r.result;
        if (openWindow_ == "measure") {
            const std::string reading = measureReadingText(st, result_);
            if (reading.empty()) {
                result_ = nlohmann::json::object();
                errorText_ = "No measurable quantity was returned for this selection. Try another face, edge, or vertex.";
                return;
            }
            rebuildMeasureOverlays(st);
            if (measureLog_.size() == 20) measureLog_.erase(measureLog_.begin());
            measureLog_.push_back(reading);
        }
        if (openWindow_ == "interference" && fullInterferenceInventory) {
            // Subset results must not remove unchecked members from the full inventory.
            std::vector<std::string> names;
            if (result_.contains("parts") && result_["parts"].is_array()) {
                for (const auto& p : result_["parts"]) {
                    if (!p.is_object()) continue;
                    std::string n = p.value("name", std::string());
                    if (!n.empty()) names.push_back(n);
                }
            }
            if (names != interferencePartNames_) {
                interferencePartNames_ = names;
                interferencePartChecked_.assign(names.size(), 1);
            }
        }
        if (openWindow_ == "interference") rebuildInterferenceView(st);
    }, 60000);
}

void EvaluateDialogs::runGeometryAnalysis(AppState& st) {
    nlohmann::json options = {{"include_discontinuities", geometryDiscontinuities_}};
    if (geometryMinArea_ > 0) options["min_face_area"] = geometryMinArea_;
    if (geometryMinEdge_ > 0) options["min_edge_length"] = geometryMinEdge_;
    if (geometrySliverRatio_ > 0) options["sliver_ratio"] = geometrySliverRatio_;
    if (geometryGapTol_ > 0) options["gap_tol"] = geometryGapTol_;
    runEval(st, "eval_geometry_analysis", options);
}

void EvaluateDialogs::drawGeometryAnalysis(AppState& st) {
    kit::dimText("Zero uses a size-scaled default for each body. All dimensions are model mm.");
    ImGui::InputFloat("Min face area (mm²)", &geometryMinArea_, 0, 0, "%.6g");
    ImGui::InputFloat("Min edge length (mm)", &geometryMinEdge_, 0, 0, "%.6g");
    ImGui::InputFloat("Sliver area / perimeter²", &geometrySliverRatio_, 0, 0, "%.6g");
    ImGui::InputFloat("Gap tolerance (mm)", &geometryGapTol_, 0, 0, "%.6g");
    ImGui::Checkbox("Inspect tangent discontinuities", &geometryDiscontinuities_);
    if (kit::primaryButton("Run", !pending_)) {
        geometryMinArea_ = std::max(0.f, geometryMinArea_);
        geometryMinEdge_ = std::max(0.f, geometryMinEdge_);
        geometrySliverRatio_ = std::max(0.f, geometrySliverRatio_);
        geometryGapTol_ = std::max(0.f, geometryGapTol_);
        runGeometryAnalysis(st);
    }
    if (pending_) {
        kit::spinner("Analyzing geometry...");
        return;
    }
    if (!errorText_.empty()) {
        kit::blockedState("Geometry analysis unavailable", errorText_.c_str());
        return;
    }
    if (!result_.contains("bodies") || !result_["bodies"].is_array()) return;
    if (!result_.value("complete", false))
        kit::blockedState("Incomplete source geometry",
                          "Some source features or assembly components did not compile. Findings below cover surviving bodies only.");
    for (const auto& body : result_["bodies"]) {
        if (!body.contains("thresholds") || !body["thresholds"].is_object()) continue;
        const auto& t = body["thresholds"];
        ImGui::TextWrapped("Body %d limits: face %.4g mm², edge %.4g mm, "
                           "sliver %.4g, gap %.4g mm",
                           body.value("body_id", 0),
                           t.value("min_face_area", 0.0), t.value("min_edge_length", 0.0),
                           t.value("sliver_ratio", 0.0), t.value("gap_tol", 0.0));
    }
    static constexpr const char* kinds[] = {
        "brep_invalid", "small_face", "short_edge", "sliver_face",
        "open_edge", "gap", "non_g1_edge"
    };
    static constexpr const char* labels[] = {
        "Invalid B-rep", "Small faces", "Short edges", "Sliver faces",
        "Open edges", "Near gaps", "Non-tangent edges"
    };
    bool any = false;
    for (int k = 0; k < 7; ++k) {
        int count = 0;
        for (const auto& body : result_["bodies"])
            if (body.contains("findings") && body["findings"].is_array())
                for (const auto& row : body["findings"])
                    if (row.value("kind", std::string()) == kinds[k]) ++count;
        if (!count) continue;
        any = true;
        kit::sectionHeader(labels[k], nullptr, nullptr, count, true);
        if (!ImGui::BeginTable(kinds[k], 3, ImGuiTableFlags_BordersInnerH | ImGuiTableFlags_RowBg))
            continue;
        ImGui::TableSetupColumn("Entity");
        ImGui::TableSetupColumn("Measured");
        ImGui::TableSetupColumn("Limit");
        ImGui::TableHeadersRow();
        for (const auto& body : result_["bodies"]) {
            if (!body.contains("findings") || !body["findings"].is_array()) continue;
            for (const auto& row : body["findings"]) {
                if (row.value("kind", std::string()) != kinds[k]) continue;
                const int bid = row.value("body_id", 0);
                const int fid = row.value("face_id", 0);
                const int eid = row.value("edge_id", 0);
                const std::string entity = "Body " + std::to_string(bid) +
                    (fid ? " / Face " + std::to_string(fid) : "") +
                    (eid ? " / Edge " + std::to_string(eid) : "");
                ImGui::TableNextRow();
                ImGui::TableSetColumnIndex(0);
                ImGui::PushID(&row);
                if (ImGui::Selectable(entity.c_str(), false, ImGuiSelectableFlags_SpanAllColumns) &&
                    bid > 0 && (fid > 0 || eid > 0))
                    st.selection.selectOne({static_cast<uint32_t>(bid),
                                            fid ? render::PickKind::Face : render::PickKind::Edge,
                                            static_cast<uint32_t>(fid ? fid : eid)});
                ImGui::PopID();
                ImGui::TableSetColumnIndex(1);
                ImGui::Text("%.5g", row.value("value", 0.0));
                ImGui::TableSetColumnIndex(2);
                ImGui::Text("%.5g", row.value("threshold", 0.0));
            }
        }
        ImGui::EndTable();
    }
    if (!any) kit::dimText("No findings for the enabled checks.");
    for (const auto& body : result_["bodies"])
        if (!body.value("gap_scan_complete", true))
            kit::dimText("Gap scan skipped: too many open edge endpoints on a body.");
}

// Dedicated mass eval slot so Mass Properties can stay open while exclusive tools use result_.
void EvaluateDialogs::runMassEval(AppState& st, const nlohmann::json& extra) {
    const int gen = ++massGen_;
    ++designSectionGen_;
    designSectionsPending_ = 0;
    designSectionsDirty_ = false;
    designSections_ = nlohmann::json::array({nullptr, nullptr, nullptr});
    massMarkers_.clear();
    massSegs_.clear();
    massErrorText_.clear();
    massResult_ = nlohmann::json::object();
    massPending_ = false;
    if (!st.docMirror.hasDoc()) {
        massErrorText_ = "No document loaded.";
        return;
    }
    nlohmann::json params = {{"doc_id", st.docMirror.docId()}};
    for (auto it = extra.begin(); it != extra.end(); ++it) params[it.key()] = it.value();
    massPending_ = true;
    const std::string docId = st.docMirror.docId();
    const int revision = st.docMirror.revision();
    ++st.rebuildsInFlight;
    st.rpc.call("eval_mass", params, [this, &st, gen, docId, revision](const core::RpcResult& r) {
        --st.rebuildsInFlight;
        if (gen != massGen_ || !massOpen_) return;
        if (st.docMirror.docId() != docId || st.docMirror.revision() != revision) {
            evalDocRevision_ = -1;
            massPending_ = false;
            return;
        }
        massPending_ = false;
        if (!r.ok) {
            massErrorText_ = r.err.empty() ? "worker call failed: eval_mass" : r.err;
            return;
        }
        if (r.result.is_object() && r.result.contains("code")) {
            massErrorText_ = r.result.value("message", std::string("evaluate error"));
            return;
        }
        if (!r.result.is_object() || !r.result.contains("volume_mm3") ||
            !r.result.contains("mass_g")) {
            massErrorText_ = "Mass properties returned no volume or mass.";
            return;
        }
        massResult_ = r.result;
        if (massOpen_) {
            designSectionsDirty_ = true;
            rebuildMassOverlays(st);
        }
    }, 60000);
}

void EvaluateDialogs::runFieldEval(AppState& st, const std::string& method,
                                   const nlohmann::json& extra,
                                   const std::string& legendTitle, const std::string& units) {
    const int gen = ++gen_;
    clearOverlays();
    clearFieldColormap(st);
    errorText_.clear();
    result_ = nlohmann::json::object();
    pending_ = false;
    title_ = legendTitle;
    // Remember which dock tool launched this so success keeps the dock panel open.
    const std::string restoreDock =
        (openWindow_ == "draft" || openWindow_ == "thickness" || openWindow_ == "curvature")
            ? openWindow_
            : std::string();
    if (!st.docMirror.hasDoc()) {
        errorText_ = "No document loaded.";
        openWindow_ = restoreDock.empty() ? "field_err" : restoreDock;
        clearFieldColormap(st);
        return;
    }
    // Assembly / multi-body: refuse colormap mode so the viewport is never corrupted.
    if (fieldEvalUnsupported(st)) {
        errorText_ =
            "Thickness/Draft/Curvature runs on a single part body; open the component to analyze it";
        openWindow_ = restoreDock.empty() ? "field_err" : restoreDock;
        clearFieldColormap(st);
        return;
    }
    nlohmann::json params = {{"doc_id", st.docMirror.docId()}};
    for (auto it = extra.begin(); it != extra.end(); ++it) params[it.key()] = it.value();
    pending_ = true;
    const int revision = st.docMirror.revision();
    const std::string fieldDocId = st.docMirror.docId();
    // Keep the dock id visible while computing (do not flip to field_wait).
    if (restoreDock.empty()) openWindow_ = "field_wait";
    ++st.rebuildsInFlight;
    st.rpc.call(method, params,
        [this, &st, gen, method, legendTitle, units, fieldDocId, revision, restoreDock](const core::RpcResult& r) {
            --st.rebuildsInFlight;
            if (gen != gen_ || openWindow_.empty()) return;
            if (st.docMirror.docId() != fieldDocId || st.docMirror.revision() != revision) {
                pending_ = false;
                evalDocRevision_ = -1;
                return;
            }
            pending_ = false;
            auto failTo = [&](const std::string& msg) {
                errorText_ = msg;
                openWindow_ = restoreDock.empty() ? "field_err" : restoreDock;
                clearFieldColormap(st);
            };
            if (!r.ok) {
                failTo(r.err.empty() ? ("worker call failed: " + method) : r.err);
                return;
            }
            if (r.result.is_object() && r.result.contains("code")) {
                failTo(r.result.value("message", std::string("evaluate error")));
                return;
            }
            result_ = r.result;
            std::string aicm = result_.value("aicm_path", std::string());
            if (aicm.empty()) {
                failTo("no overlay produced");
                return;
            }
            // Guard again at apply time (doc may have switched to multi-body mid-flight).
            if (fieldEvalUnsupported(st)) {
                failTo("Thickness/Draft/Curvature runs on a single part body; open the component "
                       "to analyze it");
                return;
            }
            std::string le;
            if (!st.loadDocMesh(aicm, le)) {
                failTo("overlay load failed: " + le);
                return;
            }
            st.colormapOn = true;
            if (st.renderer) st.renderer->setColormapMode(true);
            st.legendOn = true;
            st.legendTitle = legendTitle;
            st.legendUnits = units;
            float vmin = 0, vmax = 1;
            if (st.renderer && st.renderer->scalarRange(vmin, vmax)) {
                st.legendMin = vmin;
                st.legendMax = vmax;
            }
            // Keep the interactive dock open (draft/thickness/curvature); legacy field_wait clears.
            if (!restoreDock.empty())
                openWindow_ = restoreDock;
            else
                openWindow_.clear();
            st.addLog("[eval] " + method + " overlay loaded");
        }, 60000);
}

void EvaluateDialogs::rebuildMeasureOverlays(AppState& st) {
    overlaySegs_.clear();
    overlayLabels_.clear();
    overlayHighlights_.clear();
    overlayCallouts_.clear();

    auto pushSeg = [&](double ax, double ay, double az, double bx, double by, double bz) {
        overlaySegs_.push_back((float)ax);
        overlaySegs_.push_back((float)ay);
        overlaySegs_.push_back((float)az);
        overlaySegs_.push_back((float)bx);
        overlaySegs_.push_back((float)by);
        overlaySegs_.push_back((float)bz);
    };
    auto midLabel = [&](double ax, double ay, double az, double bx, double by, double bz,
                        const std::string& text) {
        overlayLabels_.push_back(
            {{(float)((ax + bx) * 0.5), (float)((ay + by) * 0.5), (float)((az + bz) * 0.5)}, text});
    };

    // Prefer explicit path, else points pair (distance line + mid labels).
    nlohmann::json pts = nlohmann::json::array();
    if (result_.contains("path") && result_["path"].is_array()) pts = result_["path"];
    else if (result_.value("kind", std::string()) != "multi_distance" &&
             result_.contains("points") && result_["points"].is_array())
        pts = result_["points"];

    if (pts.size() >= 2) {
        for (size_t i = 0; i + 1 < pts.size(); ++i) {
            if (!pts[i].is_array() || pts[i].size() < 3) continue;
            if (!pts[i + 1].is_array() || pts[i + 1].size() < 3) continue;
            double ax = pts[i][0].get<double>(), ay = pts[i][1].get<double>(),
                   az = pts[i][2].get<double>();
            double bx = pts[i + 1][0].get<double>(), by = pts[i + 1][1].get<double>(),
                   bz = pts[i + 1][2].get<double>();
            pushSeg(ax, ay, az, bx, by, bz);
            double dx = bx - ax, dy = by - ay, dz = bz - az;
            double d = std::sqrt(dx * dx + dy * dy + dz * dz);
            midLabel(ax, ay, az, bx, by, bz, evalLen(st, d));
        }
    }
    if (result_.contains("distance") && result_["distance"].is_number() && pts.size() >= 2) {
        // Ensure at least one label uses the reported distance (more accurate than recompute).
        if (!overlayLabels_.empty())
            overlayLabels_.back().second = evalLen(st, result_["distance"].get<double>());
    }

    // ---- Color-coded per-ref highlights + callouts ----
    // Build ordered list of selection hits that contributed to the last measure (cap 8).
    struct RefHit {
        uint32_t body = 0;
        uint32_t topo = 0;
        int kind = 0;  // PickKind
    };
    std::vector<RefHit> hits;
    for (const auto& it : st.selection.items()) {
        if (it.selector.empty() && !it.hit.valid()) continue;
        if (!it.hit.valid() && it.selector.empty()) continue;
        RefHit h;
        if (it.hit.valid()) {
            h.body = it.hit.body_id;
            h.topo = it.hit.topo_id;
            h.kind = (int)it.hit.kind;
        } else {
            // Fall back to selector kind string only.
            try {
                auto s = nlohmann::json::parse(it.selector);
                std::string k = s.value("kind", std::string());
                if (k == "face") h.kind = 1;
                else if (k == "edge") h.kind = 2;
                else if (k == "vertex") h.kind = 3;
            } catch (...) {
            }
        }
        hits.push_back(h);
        if (hits.size() >= 8) break;
    }

    auto applyEnrich = [&](EvalHighlight& hl, const nlohmann::json& enrich) {
        float a[3];
        if (enrich.contains("anchor") && readXYZ(enrich["anchor"], a)) {
            hl.point = {a[0], a[1], a[2]};
        }
        if (enrich.contains("polyline") && enrich["polyline"].is_array()) {
            flattenPolyline(enrich["polyline"], hl.polyline);
            // Edges without hit kind still get polyline.
            if (hl.kind == 0 && !hl.polyline.empty()) hl.kind = 2;
        }
    };

    // Collect per-ref enrichment objects from the measure result.
    std::vector<nlohmann::json> enrichList;
    if (result_.contains("elements") && result_["elements"].is_array()) {
        for (const auto& el : result_["elements"]) enrichList.push_back(el);
    } else if (result_.contains("a") || result_.contains("b")) {
        if (result_.contains("a")) enrichList.push_back(result_["a"]);
        if (result_.contains("b")) enrichList.push_back(result_["b"]);
    } else if (result_.contains("anchor") || result_.contains("polyline")) {
        // Single-ref path: enrichment is top-level.
        enrichList.push_back(result_);
    }

    const size_t n = std::max(hits.size(), enrichList.size());
    const std::string reading = measureReadingText(st, result_);

    for (size_t i = 0; i < n; ++i) {
        const float* col = kEvalPalette[i % 10];
        EvalHighlight hl;
        copyRgba(hl.rgba, col, 0.55f);
        if (i < hits.size()) {
            hl.bodyId = (int)hits[i].body;
            hl.kind = hits[i].kind;
            if (hits[i].kind == 1 /*Face*/ && hits[i].body != 0 && hits[i].topo != 0)
                hl.faceKey = ((uint64_t)hits[i].body << 32) | (uint64_t)hits[i].topo;
            // Vertex / face fallback from host caches when engine enrich is missing.
            if (hits[i].body != 0 && hits[i].topo != 0) {
                uint64_t key = ((uint64_t)hits[i].body << 32) | (uint64_t)hits[i].topo;
                if (hits[i].kind == 1) {
                    auto it = st.faceNormalCache.find(key);
                    if (it != st.faceNormalCache.end())
                        hl.point = {it->second[3], it->second[4], it->second[5]};
                } else if (hits[i].kind == 2) {
                    auto it = st.edgeCentroidCache.find(key);
                    if (it != st.edgeCentroidCache.end())
                        hl.point = {it->second[0], it->second[1], it->second[2]};
                } else if (hits[i].kind == 3) {
                    // Vertex position not cached host-side; enrich will fill if present.
                }
            }
        }
        if (i < enrichList.size() && enrichList[i].is_object()) applyEnrich(hl, enrichList[i]);

        // If still no kind but we have a faceKey, treat as face.
        if (hl.kind == 0 && hl.faceKey != 0) hl.kind = 1;

        overlayHighlights_.push_back(std::move(hl));

        // Single-ref reading or a stable ref number matching the selection list.
        EvalCallout co;
        copyRgba(co.rgba, col, 0.95f);
        const EvalHighlight& hlr = overlayHighlights_.back();
        if (hlr.point[0] != 0.0f || hlr.point[1] != 0.0f || hlr.point[2] != 0.0f ||
            (i < enrichList.size() && enrichList[i].contains("anchor"))) {
            co.pos = hlr.point;
        } else if (!hlr.polyline.empty() && hlr.polyline.size() >= 3) {
            // Midpoint of polyline samples.
            size_t mid = (hlr.polyline.size() / 3) / 2;
            co.pos = {hlr.polyline[mid * 3 + 0], hlr.polyline[mid * 3 + 1],
                      hlr.polyline[mid * 3 + 2]};
        } else {
            continue;  // no anchor — skip callout
        }
        co.text = n == 1 ? reading : "Ref " + std::to_string(i + 1);
        if (!co.text.empty()) overlayCallouts_.push_back(std::move(co));
    }
}

void EvaluateDialogs::rebuildMassOverlays(AppState& st) {
    // Mass geometry lives in massSegs_ (never thrash measure's overlaySegs_).
    massSegs_.clear();
    massMarkers_.clear();
    if (!massOpen_ || !st.docMirror.hasDoc()) {
        if (!sectionOpen_ && openWindow_ != "interference") massBodyAlpha_ = 1.0f;
        return;
    }

    const bool anyMark = showCom_ || showI_ || showJ_;
    if (anyMark) massBodyAlpha_ = 0.45f;
    else if (!sectionOpen_ && openWindow_ != "interference") massBodyAlpha_ = 1.0f;

    if (!massResult_.contains("com") || !massResult_["com"].is_array() ||
        massResult_["com"].size() != 3)
        return;
    const double cx = massResult_["com"][0].get<double>();
    const double cy = massResult_["com"][1].get<double>();
    const double cz = massResult_["com"][2].get<double>();

    // Cross size ~ 5% of a typical part; fall back to 5 mm.
    float arm = 5.0f;
    if (st.renderer) {
        double r = std::sqrt(cx * cx + cy * cy + cz * cz);
        if (r > 1.0) arm = (float)(r * 0.08);
        if (arm < 2.0f) arm = 2.0f;
        if (arm > 50.0f) arm = 50.0f;
        float mr = st.renderer->modelRadius();
        if (mr > 1.0f) {
            arm = mr * 0.08f;
            if (arm < 2.0f) arm = 2.0f;
            if (arm > 80.0f) arm = 80.0f;
        }
    }

    auto pushSeg = [&](float ax, float ay, float az, float bx, float by, float bz) {
        massSegs_.push_back(ax);
        massSegs_.push_back(ay);
        massSegs_.push_back(az);
        massSegs_.push_back(bx);
        massSegs_.push_back(by);
        massSegs_.push_back(bz);
    };
    auto pushMarker = [&](float x, float y, float z, const std::string& label,
                          const std::string& tip, const float rgba[4]) {
        MassMarker m;
        m.pos = {x, y, z};
        m.label = label;
        m.tooltip = tip;
        copyRgba(m.rgba, rgba, 0.95f);
        massMarkers_.push_back(std::move(m));
    };

    if (showCom_) {
        pushSeg(cx - arm, cy, cz, cx + arm, cy, cz);
        pushSeg(cx, cy - arm, cz, cx, cy + arm, cz);
        pushSeg(cx, cy, cz - arm, cx, cy, cz + arm);
        pushMarker((float)cx, (float)cy, (float)cz, "CG", "Center of gravity", kEvalPalette[5]);
    }

    // Principal axes through COM for I/J markers (geometry in overlaySegs_, values in massMarkers_).
    if ((showI_ || showJ_) && massResult_.contains("principal_axes") &&
        massResult_["principal_axes"].is_array() && massResult_["principal_axes"].size() == 3) {
        const auto& Pa = massResult_["principal_axes"];
        for (int i = 0; i < 3; ++i) {
            if (!Pa[i].is_array() || Pa[i].size() != 3) continue;
            float nx = (float)Pa[i][0].get<double>();
            float ny = (float)Pa[i][1].get<double>();
            float nz = (float)Pa[i][2].get<double>();
            float len = std::sqrt(nx * nx + ny * ny + nz * nz);
            if (len < 1e-8f) continue;
            nx /= len;
            ny /= len;
            nz /= len;
            float L = arm * 2.5f;
            if (showI_ || showJ_) {
                pushSeg(cx - nx * L, cy - ny * L, cz - nz * L, cx + nx * L, cy + ny * L, cz + nz * L);
            }

            const float* col = kEvalPalette[(i + 1) % 10];
            const float tipX = (float)(cx + nx * L);
            const float tipY = (float)(cy + ny * L);
            const float tipZ = (float)(cz + nz * L);

            if (designSections_.is_array() && (size_t)i < designSections_.size() &&
                designSections_[i].is_object()) {
                const auto& sec = designSections_[i];
                if (sec.contains("error")) continue;
                double Ixx = sec.value("Ixx", 0.0), Iyy = sec.value("Iyy", 0.0);
                double Imax = std::max(Ixx, Iyy);
                double J = sec.value("J", Ixx + Iyy);
                if (showI_) {
                    char labA[96];
                    std::snprintf(labA, sizeof(labA), "I%d %s", i + 1,
                                  pmFormatAreaMoment(Imax, st.ips()).c_str());
                    pushMarker(tipX, tipY, tipZ, labA,
                               "Bending (area) moment of inertia about this centroidal plane — "
                               "resists bending. Units: in⁴ (IPS) or mm⁴ (MMGS).",
                               col);
                }
                if (showJ_) {
                    // Offset slightly along a secondary axis so I and J chips don't stack.
                    float ux = 1, uy = 0, uz = 0;
                    if (std::fabs(nx) > 0.9f) {
                        ux = 0;
                        uy = 1;
                    }
                    float cxu = ny * uz - nz * uy;
                    float cyu = nz * ux - nx * uz;
                    float czu = nx * uy - ny * ux;
                    float ul = std::sqrt(cxu * cxu + cyu * cyu + czu * czu);
                    float ox = 0, oy = 0, oz = 0;
                    if (ul > 1e-8f) {
                        cxu /= ul;
                        cyu /= ul;
                        czu /= ul;
                        ox = cxu * (L * 0.12f);
                        oy = cyu * (L * 0.12f);
                        oz = czu * (L * 0.12f);
                    }
                    char labJ[96];
                    std::snprintf(labJ, sizeof(labJ), "J%d %s", i + 1,
                                  pmFormatAreaMoment(J, st.ips()).c_str());
                    pushMarker(tipX + ox, tipY + oy, tipZ + oz, labJ,
                               "Polar (torsional) moment of inertia J = Ixx+Iyy about this axis — "
                               "resists torsion. Units: in⁴ (IPS) or mm⁴ (MMGS).",
                               kEvalPalette[(i + 4) % 10]);
                }
            } else {
                if (showI_) {
                    char labA[32];
                    std::snprintf(labA, sizeof(labA), "I%d", i + 1);
                    pushMarker(tipX, tipY, tipZ, labA,
                               "Bending (area) moment of inertia about this centroidal plane "
                               "(computing…).",
                               col);
                } else if (showJ_) {
                    char labJ[32];
                    std::snprintf(labJ, sizeof(labJ), "J%d", i + 1);
                    pushMarker(tipX, tipY, tipZ, labJ,
                               "Polar (torsional) moment of inertia about this axis (computing…).",
                               kEvalPalette[(i + 4) % 10]);
                }
            }

            // Approximate polar-moment ring in plane ⟂ axis i.
            if (showJ_) {
                float ux = 1, uy = 0, uz = 0;
                if (std::fabs(nx) > 0.9f) {
                    ux = 0;
                    uy = 1;
                }
                float cxu = ny * uz - nz * uy;
                float cyu = nz * ux - nx * uz;
                float czu = nx * uy - ny * ux;
                float ul = std::sqrt(cxu * cxu + cyu * cyu + czu * czu);
                if (ul < 1e-8f) continue;
                cxu /= ul;
                cyu /= ul;
                czu /= ul;
                float vx = ny * czu - nz * cyu;
                float vy = nz * cxu - nx * czu;
                float vz = nx * cyu - ny * cxu;
                float R = L * 0.55f;
                const int N = 16;
                for (int k = 0; k < N; ++k) {
                    float a0 = (float)(2.0 * 3.14159265358979323846 * k / N);
                    float a1 = (float)(2.0 * 3.14159265358979323846 * (k + 1) / N);
                    float x0 = (float)cx + R * (std::cos(a0) * cxu + std::sin(a0) * vx);
                    float y0 = (float)cy + R * (std::cos(a0) * cyu + std::sin(a0) * vy);
                    float z0 = (float)cz + R * (std::cos(a0) * czu + std::sin(a0) * vz);
                    float x1 = (float)cx + R * (std::cos(a1) * cxu + std::sin(a1) * vx);
                    float y1 = (float)cy + R * (std::cos(a1) * cyu + std::sin(a1) * vy);
                    float z1 = (float)cz + R * (std::cos(a1) * czu + std::sin(a1) * vz);
                    pushSeg(x0, y0, z0, x1, y1, z1);
                }
            }
        }
    }
}

void EvaluateDialogs::rebuildInterferenceView(AppState& st) {
    (void)st;
    interferenceView_ = InterferenceView{};
    massBodyAlpha_ = 1.0f;
    if (openWindow_ != "interference") return;
    if (pending_ || !errorText_.empty()) return;

    bool hasPairs = result_.contains("interferences") && result_["interferences"].is_array() &&
                    !result_["interferences"].empty();
    if (!hasPairs) return;

    interferenceView_.active = true;
    massBodyAlpha_ = 0.45f;

    auto nameMap = buildNameToBodyId(st);
    int pairIdx = 0;
    for (const auto& p : result_["interferences"]) {
        if (!p.is_object()) continue;
        InterferenceView::Pair pr;
        std::string aName = p.value("a", std::string());
        std::string bName = p.value("b", std::string());
        auto ia = nameMap.find(aName);
        auto ib = nameMap.find(bName);
        if (ia != nameMap.end()) pr.bodyA = ia->second;
        if (ib != nameMap.end()) pr.bodyB = ib->second;

        // Distinct palette colors per body in the pair (pair index drives hue).
        const float* ca = kEvalPalette[(pairIdx * 2) % 10];
        const float* cb = kEvalPalette[(pairIdx * 2 + 1) % 10];
        copyRgba(pr.rgbaA, ca, 0.35f);
        copyRgba(pr.rgbaB, cb, 0.35f);
        // Overlap region always red (Contract) at alpha 0.6.
        copyRgba(pr.overlapRgba, kEvalPalette[0], 0.60f);

        // Expand overlap_mesh {positions, indices} into flat triangle xyz.
        if (p.contains("overlap_mesh") && p["overlap_mesh"].is_object()) {
            const auto& mesh = p["overlap_mesh"];
            const auto& pos = mesh.contains("positions") ? mesh["positions"] : nlohmann::json();
            const auto& idx = mesh.contains("indices") ? mesh["indices"] : nlohmann::json();
            if (pos.is_array() && idx.is_array() && pos.size() >= 9 && idx.size() >= 3) {
                // positions may be flat [x,y,z,...] or nested [[x,y,z],...]
                std::vector<float> flatPos;
                if (!pos.empty() && pos[0].is_number()) {
                    flatPos.reserve(pos.size());
                    for (const auto& v : pos) {
                        if (v.is_number()) flatPos.push_back((float)v.get<double>());
                    }
                } else {
                    for (const auto& pt : pos) {
                        float xyz[3];
                        if (readXYZ(pt, xyz)) {
                            flatPos.push_back(xyz[0]);
                            flatPos.push_back(xyz[1]);
                            flatPos.push_back(xyz[2]);
                        }
                    }
                }
                const size_t nVerts = flatPos.size() / 3;
                pr.overlapTris.reserve(idx.size() * 3);
                for (size_t t = 0; t + 2 < idx.size(); t += 3) {
                    if (!idx[t].is_number() || !idx[t + 1].is_number() || !idx[t + 2].is_number())
                        continue;
                    int i0 = idx[t].get<int>();
                    int i1 = idx[t + 1].get<int>();
                    int i2 = idx[t + 2].get<int>();
                    if (i0 < 0 || i1 < 0 || i2 < 0) continue;
                    if ((size_t)i0 >= nVerts || (size_t)i1 >= nVerts || (size_t)i2 >= nVerts)
                        continue;
                    for (int ii : {i0, i1, i2}) {
                        pr.overlapTris.push_back(flatPos[(size_t)ii * 3 + 0]);
                        pr.overlapTris.push_back(flatPos[(size_t)ii * 3 + 1]);
                        pr.overlapTris.push_back(flatPos[(size_t)ii * 3 + 2]);
                    }
                }
            }
        }
        interferenceView_.pairs.push_back(std::move(pr));
        ++pairIdx;
    }
}

void EvaluateDialogs::tickLiveMeasure(AppState& st) {
    if (openWindow_ != "measure") return;
    auto selection = measureSelection(st);
    if (selection.key != lastMeasureRefs_) {
        ++gen_;
        lastMeasureRefs_ = selection.key;
        lastMeasureChangeT_ = ImGui::GetTime();
        measureDirty_ = true;
        result_ = nlohmann::json::object();
        pending_ = false;
        errorText_ = selection.error;
        measureResolving_ = selection.resolving;
        clearOverlays();
    }
    if (!st.docMirror.hasDoc()) {
        errorText_ = "No document loaded. Open a part or assembly to measure.";
        return;
    }
    if (!measureDirty_ || st.selection.empty() || measureResolving_ || !errorText_.empty())
        return;
    if (ImGui::GetTime() - lastMeasureChangeT_ < 0.05) return;
    measureDirty_ = false;
    runEval(st, "eval_measure", {{"refs", selection.refs}});
}

void EvaluateDialogs::tickMassDesignSections(AppState& st) {
    if (!massOpen_) return;
    if (!designSectionsDirty_) return;
    if (massPending_ || designSectionsPending_ > 0) return;
    if (!massResult_.contains("com") || !massResult_["com"].is_array() ||
        massResult_["com"].size() != 3)
        return;
    if (!massResult_.contains("principal_axes") || !massResult_["principal_axes"].is_array() ||
        massResult_["principal_axes"].size() != 3)
        return;

    designSectionsDirty_ = false;
    designSections_ = nlohmann::json::array({nullptr, nullptr, nullptr});
    const int batch = ++designSectionGen_;
    designSectionsPending_ = 3;
    const double cx = massResult_["com"][0].get<double>();
    const double cy = massResult_["com"][1].get<double>();
    const double cz = massResult_["com"][2].get<double>();
    const auto Pa = massResult_["principal_axes"];

    for (int i = 0; i < 3; ++i) {
        if (!Pa[i].is_array() || Pa[i].size() != 3) {
            --designSectionsPending_;
            continue;
        }
        nlohmann::json plane = {
            {"origin", {cx, cy, cz}},
            {"normal",
             {Pa[i][0].get<double>(), Pa[i][1].get<double>(), Pa[i][2].get<double>()}},
        };
        nlohmann::json params = {{"doc_id", st.docMirror.docId()}, {"plane", plane}};
        const std::string docId = st.docMirror.docId();
        const int revision = st.docMirror.revision();
        ++st.rebuildsInFlight;
        st.rpc.call(
            "eval_section_props", params,
            [this, &st, batch, i, docId, revision](const core::RpcResult& r) {
                --st.rebuildsInFlight;
                if (batch != designSectionGen_ || !massOpen_) return;
                if (st.docMirror.docId() != docId || st.docMirror.revision() != revision) {
                    evalDocRevision_ = -1;
                    return;
                }
                --designSectionsPending_;
                if (r.ok && r.result.is_object() && !r.result.contains("code")) {
                    nlohmann::json sec = r.result;
                    double Ixx = sec.value("Ixx", 0.0);
                    double Iyy = sec.value("Iyy", 0.0);
                    sec["J"] = Ixx + Iyy;
                    // Axial stiffness k = E*A/L when E known. L estimated from principal extent
                    // is not available here cheaply — leave k null unless we have E and area only
                    // as a note. Parent can still show EA without L.
                    std::string key = massDocKey_;
                    float E = materialYoungsEMpa(key);
                    double A = sec.value("area_mm2", 0.0);
                    if (E > 0.0f && A > 0.0) {
                        sec["E_mpa"] = E;
                        sec["EA"] = (double)E * A;  // N (MPa*mm² = N)
                    }
                    if (designSections_.is_array() && (size_t)i < designSections_.size())
                        designSections_[i] = std::move(sec);
                } else {
                    const std::string message = !r.ok
                        ? (r.err.empty() ? "Section analysis failed." : r.err)
                        : (r.result.is_object() ? r.result.value("message", std::string("Section analysis failed."))
                                                : "Section analysis returned an invalid result.");
                    designSections_[i] = nlohmann::json{{"error", message}};
                }
                if (designSectionsPending_ <= 0) rebuildMassOverlays(st);
            },
            60000);
    }
}

// Orthonormal in-plane axes for a unit normal (right-handed).
static void sectionBasis(const float n[3], float u[3], float v[3]) {
    float ax = 1.f, ay = 0.f, az = 0.f;
    if (std::fabs(n[0]) > 0.9f) {
        ax = 0.f;
        ay = 1.f;
    }
    // u = normalize(cross(n, arbitrary))
    u[0] = n[1] * az - n[2] * ay;
    u[1] = n[2] * ax - n[0] * az;
    u[2] = n[0] * ay - n[1] * ax;
    float ul = std::sqrt(u[0] * u[0] + u[1] * u[1] + u[2] * u[2]);
    if (ul < 1e-8f) {
        u[0] = 0.f;
        u[1] = 1.f;
        u[2] = 0.f;
        ul = 1.f;
    }
    u[0] /= ul;
    u[1] /= ul;
    u[2] /= ul;
    // v = n × u
    v[0] = n[1] * u[2] - n[2] * u[1];
    v[1] = n[2] * u[0] - n[0] * u[2];
    v[2] = n[0] * u[1] - n[1] * u[0];
}

// Unit normals for the three orthogonal section planes (+X, +Y, +Z).
static const float kSectionNormals[3][3] = {
    {1.f, 0.f, 0.f},
    {0.f, 1.f, 0.f},
    {0.f, 0.f, 1.f},
};
static const char* kSectionNames[3] = {"YZ (X-cut)", "XZ (Y-cut)", "XY (Z-cut)"};

void EvaluateDialogs::initSectionSession(AppState& st) {
    // Centroid: prefer model center; fall back to bodyBBoxCache midpoints / origin.
    sectionCentroid_[0] = sectionCentroid_[1] = sectionCentroid_[2] = 0.f;
    sectionHalfSize_ = 50.f;
    if (st.renderer) {
        Vec3 c = st.renderer->modelCenter();
        sectionCentroid_[0] = c.x;
        sectionCentroid_[1] = c.y;
        sectionCentroid_[2] = c.z;
        float r = st.renderer->modelRadius();
        if (r > 1.f) sectionHalfSize_ = r;
    }
    if (!st.bodyBBoxCache.empty()) {
        float mn[3] = {1e30f, 1e30f, 1e30f}, mx[3] = {-1e30f, -1e30f, -1e30f};
        for (const auto& kv : st.bodyBBoxCache) {
            const auto& b = kv.second;
            for (int k = 0; k < 3; ++k) {
                mn[k] = std::min(mn[k], b[k]);
                mx[k] = std::max(mx[k], b[3 + k]);
            }
        }
        if (mn[0] < 1e29f) {
            // Prefer bbox centre when renderer centre is empty/zero-ish.
            if (!st.renderer || st.renderer->modelRadius() < 1e-3f) {
                sectionCentroid_[0] = 0.5f * (mn[0] + mx[0]);
                sectionCentroid_[1] = 0.5f * (mn[1] + mx[1]);
                sectionCentroid_[2] = 0.5f * (mn[2] + mx[2]);
            }
            float dx = mx[0] - mn[0], dy = mx[1] - mn[1], dz = mx[2] - mn[2];
            float ext = 0.5f * std::sqrt(dx * dx + dy * dy + dz * dz);
            if (ext > 1.f) sectionHalfSize_ = ext;
        }
    }
    if (sectionHalfSize_ < 5.f) sectionHalfSize_ = 5.f;
    sectionOffset_[0] = sectionOffset_[1] = sectionOffset_[2] = 0.f;
    sectionResults_ = nlohmann::json::array({nullptr, nullptr, nullptr});
    sectionPendingIdx_ = -1;
    sectionDragLastT_ = -1e9;
    sectionPendingCount_ = 0;
    rebuildSectionPlaneViews(st);
}

void EvaluateDialogs::rebuildSectionPlaneViews(AppState& st) {
    sectionPlanes_.clear();
    if (!sectionOpen_ || !st.docMirror.hasDoc()) return;
    const float hs = sectionHalfSize_ * 0.6f;
    for (int i = 0; i < 3; ++i) {
        SectionPlaneView pv;
        pv.index = i;
        pv.active = true;
        pv.halfSize = hs;
        const float* n = kSectionNormals[i];
        pv.normal = {n[0], n[1], n[2]};
        pv.origin = {sectionCentroid_[0] + n[0] * sectionOffset_[i],
                     sectionCentroid_[1] + n[1] * sectionOffset_[i],
                     sectionCentroid_[2] + n[2] * sectionOffset_[i]};
        float u[3], v[3];
        sectionBasis(n, u, v);
        pv.uAxis = {u[0], u[1], u[2]};
        pv.vAxis = {v[0], v[1], v[2]};
        const float* col = kEvalPalette[i % 10];
        copyRgba(pv.rgba, col, 0.35f);

        char lab[160];
        if (sectionResults_.is_array() && (size_t)i < sectionResults_.size() &&
            sectionResults_[i].is_object()) {
            const auto& sec = sectionResults_[i];
            if (sec.contains("error") && sec["error"].is_string()) {
                std::snprintf(lab, sizeof(lab), "Sec %d  %s", i + 1, kSectionNames[i]);
            } else {
                double Ixx = sec.value("Ixx", 0.0);
                // Viewport label: plane index + bending I (clear per-plane moment callout).
                std::snprintf(lab, sizeof(lab), "Sec %d  I=%s", i + 1,
                              pmFormatAreaMoment(Ixx, st.ips()).c_str());
            }
        } else {
            std::snprintf(lab, sizeof(lab), "Sec %d  %s", i + 1, kSectionNames[i]);
        }
        pv.label = lab;
        sectionPlanes_.push_back(std::move(pv));
    }
}

void EvaluateDialogs::fireSectionEval(AppState& st, int idx) {
    if (idx < 0 || idx > 2) return;
    if (!st.docMirror.hasDoc()) {
        sectionErrorText_ = "No document loaded.";
        if (sectionResults_.is_array() && (size_t)idx < sectionResults_.size())
            sectionResults_[idx] = nlohmann::json{{"error", "No document loaded."}};
        rebuildSectionPlaneViews(st);
        return;
    }
    const float* n = kSectionNormals[idx];
    const double ox = sectionCentroid_[0] + n[0] * sectionOffset_[idx];
    const double oy = sectionCentroid_[1] + n[1] * sectionOffset_[idx];
    const double oz = sectionCentroid_[2] + n[2] * sectionOffset_[idx];
    nlohmann::json plane = {
        {"origin", {ox, oy, oz}},
        {"normal", {(double)n[0], (double)n[1], (double)n[2]}},
    };
    nlohmann::json params = {{"doc_id", st.docMirror.docId()}, {"plane", plane}};
    sectionResults_[idx] = nullptr;
    const std::string docId = st.docMirror.docId();
    const int revision = st.docMirror.revision();
    const int batch = sectionEvalGen_;  // shared with fireAll; single-plane uses current gen
    const float offset = sectionOffset_[idx];
    ++sectionPendingCount_;
    ++st.rebuildsInFlight;
    st.rpc.call(
        "eval_section_props", params,
        [this, &st, batch, idx, docId, revision, offset](const core::RpcResult& r) {
            --st.rebuildsInFlight;
            if (batch != sectionEvalGen_ || !sectionOpen_) return;
            if (st.docMirror.docId() != docId || st.docMirror.revision() != revision) {
                evalDocRevision_ = -1;
                return;
            }
            if (sectionPendingCount_ > 0) --sectionPendingCount_;
            if (offset != sectionOffset_[idx]) return;
            if (!r.ok) {
                std::string msg = r.err.empty() ? "worker call failed: eval_section_props" : r.err;
                sectionErrorText_ = msg;
                if (sectionResults_.is_array() && (size_t)idx < sectionResults_.size())
                    sectionResults_[idx] = nlohmann::json{{"error", msg}};
            } else if (r.result.is_object() && r.result.contains("code")) {
                std::string msg = r.result.value("message", std::string("evaluate error"));
                sectionErrorText_ = msg;
                if (sectionResults_.is_array() && (size_t)idx < sectionResults_.size())
                    sectionResults_[idx] = nlohmann::json{{"error", msg}};
            } else if (r.ok && r.result.is_object()) {
                nlohmann::json sec = r.result;
                double Ixx = sec.value("Ixx", 0.0);
                double Iyy = sec.value("Iyy", 0.0);
                sec["J"] = Ixx + Iyy;
                if (sectionResults_.is_array() && (size_t)idx < sectionResults_.size())
                    sectionResults_[idx] = std::move(sec);
                // Clear window-level error once any plane succeeds (per-plane errors stay).
                if (!sectionErrorText_.empty()) {
                    bool anyErr = false;
                    if (sectionResults_.is_array()) {
                        for (const auto& s : sectionResults_) {
                            if (s.is_object() && s.contains("error")) {
                                anyErr = true;
                                break;
                            }
                        }
                    }
                    if (!anyErr) sectionErrorText_.clear();
                }
            }
            rebuildSectionPlaneViews(st);
        },
        60000);
}

void EvaluateDialogs::fireAllSectionEvals(AppState& st) {
    if (!st.docMirror.hasDoc()) {
        sectionErrorText_ = "No document loaded.";
        return;
    }
    sectionErrorText_.clear();
    sectionResults_ = nlohmann::json::array({nullptr, nullptr, nullptr});
    const int batch = ++sectionEvalGen_;
    sectionPendingCount_ = 0;
    (void)batch;
    for (int i = 0; i < 3; ++i) fireSectionEval(st, i);
    rebuildSectionPlaneViews(st);
}

void EvaluateDialogs::applySectionOffset(AppState& st, int idx, float newOffset, bool scheduleEval) {
    if (idx < 0 || idx > 2) return;
    const float lim = sectionHalfSize_;
    if (newOffset > lim) newOffset = lim;
    if (newOffset < -lim) newOffset = -lim;
    sectionOffset_[idx] = newOffset;
    sectionResults_[idx] = nullptr;
    // Live plane origin update for the viewport.
    if ((size_t)idx < sectionPlanes_.size()) {
        const float* n = kSectionNormals[idx];
        sectionPlanes_[idx].origin = {sectionCentroid_[0] + n[0] * newOffset,
                                      sectionCentroid_[1] + n[1] * newOffset,
                                      sectionCentroid_[2] + n[2] * newOffset};
    } else {
        rebuildSectionPlaneViews(st);
    }
    if (scheduleEval) {
        sectionPendingIdx_ = idx;
        sectionDragLastT_ = ImGui::GetTime();
        rebuildSectionPlaneViews(st);
    }
}

void EvaluateDialogs::dragSectionPlane(AppState& st, int idx, float deltaAlongNormal) {
    if (!sectionOpen_) return;
    if (idx < 0 || idx > 2) return;
    applySectionOffset(st, idx, sectionOffset_[idx] + deltaAlongNormal, true);
}

void EvaluateDialogs::tickSectionDrag(AppState& st) {
    if (!sectionOpen_) return;
    if (sectionPendingIdx_ < 0) return;
    const double now = ImGui::GetTime();
    if (now - sectionDragLastT_ < 0.08) return;  // ~80 ms debounce
    const int idx = sectionPendingIdx_;
    sectionPendingIdx_ = -1;
    fireSectionEval(st, idx);
}


bool EvaluateDialogs::handleCommand(AppState& st, const std::string& cmdId) {
    syncDocument(st);
    const bool exclusive = cmdId == "eval_measure" || cmdId == "eval_check" ||
        cmdId == "eval_geometry" ||
        cmdId == "eval_interference" || cmdId == "eval_symmetry" ||
        cmdId == "eval_draft" || cmdId == "eval_thickness" || cmdId == "eval_curvature" ||
        cmdId == "eval_zebra";
    if (exclusive) {
        ++gen_;
        pending_ = false;
        result_ = nlohmann::json::object();
        errorText_.clear();
        clearOverlays();
        if (openWindow_ == "zebra" && st.renderer) st.renderer->setZebra(false, gZebraFreq);
        if (openWindow_ == "draft" || openWindow_ == "thickness" || openWindow_ == "curvature")
            clearFieldColormap(st);
    }
    if (cmdId == "eval_mass_props") {
        massCustomDensity_ = false;
        seedMassFromDoc(st);
        designSections_ = nlohmann::json::array({nullptr, nullptr, nullptr});
        designSectionsDirty_ = false;
        massOpen_ = true;
        runMassEval(st, {{"density_g_cm3", (double)massDensity_}});
        return true;
    }
    if (cmdId == "eval_measure") {
        lastMeasureRefs_.clear();
        measureResolving_ = false;
        measureDirty_ = true;
        openWindow_ = "measure";
        title_ = "Measure";
        tickLiveMeasure(st);
        return true;
    }
    if (cmdId == "eval_section_props") {
        sectionOpen_ = true;
        sectionErrorText_.clear();
        initSectionSession(st);
        fireAllSectionEvals(st);
        return true;
    }
    if (cmdId == "eval_check") {
        runEval(st, "eval_check", nlohmann::json::object());
        openWindow_ = "check";
        title_ = "Geometry Check";
        return true;
    }
    if (cmdId == "eval_geometry") {
        openWindow_ = "geometry";
        title_ = "Geometry Analysis";
        runGeometryAnalysis(st);
        return true;
    }
    if (cmdId == "eval_interference") {
        // Assemblies / multi-body: real fit report. Single-body parts get an explanatory window.
        interferencePartNames_.clear();
        interferencePartChecked_.clear();
        interferenceView_ = InterferenceView{};
        runEval(st, "eval_interference", nlohmann::json::object());
        openWindow_ = "interference";
        title_ = "Interference Detection";
        return true;
    }
    if (cmdId == "eval_symmetry") {
        symmetryPlane_ = 0;
        runEval(st, "eval_symmetry", {{"plane", kPlaneNames[1]}});  // front default
        openWindow_ = "symmetry";
        title_ = "Symmetry Check";
        return true;
    }
    if (cmdId == "eval_draft") {
        draftPull_ = 2;
        draftAngleDeg_ = 1.0f;
        openWindow_ = "draft";
        title_ = "Draft Analysis";
        // Initial field eval — runFieldEval temporarily sets field_wait; restore dock id after.
        runFieldEval(st, "eval_draft", {{"pull", {0, 0, 1}}, {"angle_deg", (double)draftAngleDeg_}},
                     "Draft angle", "deg");
        if (openWindow_ == "field_wait" || openWindow_.empty()) openWindow_ = "draft";
        return true;
    }
    if (cmdId == "eval_thickness") {
        thicknessRes_ = 0.5f;
        openWindow_ = "thickness";
        title_ = "Thickness Analysis";
        runFieldEval(st, "eval_thickness", nlohmann::json::object(), "Wall thickness",
                     st.ips() ? "in" : "mm");
        if (openWindow_ == "field_wait" || openWindow_.empty()) openWindow_ = "thickness";
        return true;
    }
    if (cmdId == "eval_curvature") {
        curvatureKind_ = 0;
        openWindow_ = "curvature";
        title_ = "Curvature";
        runFieldEval(st, "eval_curvature", {{"kind", "mean"}}, "Mean curvature", "1/mm");
        if (openWindow_ == "field_wait" || openWindow_.empty()) openWindow_ = "curvature";
        return true;
    }
    if (cmdId == "eval_zebra") {
        errorText_.clear();
        result_ = nlohmann::json::object();
        pending_ = false;
        if (st.renderer) st.renderer->setZebra(true, gZebraFreq);
        openWindow_ = "zebra";
        title_ = "Zebra Stripes";
        return true;
    }
    return false;
}

void EvaluateDialogs::syncDocument(AppState& st) {
    const std::string docId = st.docMirror.docId();
    const int revision = st.docMirror.revision();
    if (docId == evalDocId_ && revision == evalDocRevision_) return;
    const bool switched = docId != evalDocId_;
    evalDocId_ = docId;
    evalDocRevision_ = revision;
    ++gen_;
    ++massGen_;
    ++designSectionGen_;
    ++sectionEvalGen_;
    pending_ = massPending_ = false;
    designSectionsPending_ = sectionPendingCount_ = 0;
    designSectionsDirty_ = false;
    sectionPendingIdx_ = -1;
    result_ = massResult_ = nlohmann::json::object();
    designSections_ = sectionResults_ = nlohmann::json::array({nullptr, nullptr, nullptr});
    errorText_.clear();
    massErrorText_.clear();
    sectionErrorText_.clear();
    clearOverlays();
    massMarkers_.clear();
    massSegs_.clear();
    sectionPlanes_.clear();
    lastMeasureRefs_.clear();
    measureLog_.clear();
    measureDirty_ = true;
    measureResolving_ = false;
    if (openWindow_ == "draft" || openWindow_ == "thickness" || openWindow_ == "curvature")
        clearFieldColormap(st);
    if (!st.docMirror.hasDoc()) {
        errorText_ = massErrorText_ = sectionErrorText_ = "No document loaded.";
        return;
    }
    if (massOpen_) {
        if (switched) massCustomDensity_ = false;
        seedMassFromDoc(st);
        runMassEval(st, {{"density_g_cm3", (double)massDensity_}});
    }
    if (sectionOpen_) {
        initSectionSession(st);
        fireAllSectionEvals(st);
    }
    if (openWindow_ == "check") runEval(st, "eval_check", nlohmann::json::object());
    else if (openWindow_ == "geometry") runGeometryAnalysis(st);
    else if (openWindow_ == "interference") {
        interferencePartNames_.clear();
        interferencePartChecked_.clear();
        runEval(st, "eval_interference", nlohmann::json::object());
    } else if (openWindow_ == "symmetry") {
        static const int map[] = {1, 0, 2};
        runEval(st, "eval_symmetry", {{"plane", kPlaneNames[map[symmetryPlane_]]}});
    } else if (openWindow_ == "draft" || openWindow_ == "thickness" || openWindow_ == "curvature") {
        errorText_ = "Document changed. Update the analysis for the current geometry.";
    }
}

void EvaluateDialogs::drawMeasure(AppState& st) {
    kit::dimText("Pick a face, edge, or vertex to measure it. Ctrl-click adds or removes picks. "
                 "Two picks measure distance; three to eight measure a chain.");
    kit::sectionHeader("Selection", nullptr, nullptr, (int)st.selection.size(), true);
    if (kit::secondaryButton("Clear selection", !st.selection.empty())) {
        st.selection.clear();
        tickLiveMeasure(st);
    }
    int remove = -1;
    const auto& items = st.selection.items();
    for (size_t i = 0; i < items.size(); ++i) {
        const auto& item = items[i];
        ImGui::PushID((int)i);
        if (kit::cardBegin("##pick", nullptr, nullptr, nullptr, kEvalPalette[i % 10])) {
            const char* kind = item.hit.kind == render::PickKind::Face ? "Face" :
                               item.hit.kind == render::PickKind::Edge ? "Edge" : "Vertex";
            ImGui::TextWrapped("%zu. %s %u · Body %u", i + 1, kind,
                               item.hit.topo_id, item.hit.body_id);
            if (!item.resolution_error.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, kit::toneVec(kit::Tone::Err));
                ImGui::TextWrapped("%s", item.resolution_error.c_str());
                ImGui::PopStyleColor();
            } else if (item.pending_resolve) kit::dimText("Resolving geometry...");
            if (kit::secondaryButton("Remove")) remove = (int)i;
        }
        kit::cardEnd();
        ImGui::PopID();
    }
    if (remove >= 0) {
        const auto hit = items[(size_t)remove].hit;
        st.selection.toggle(hit);
        tickLiveMeasure(st);
    }
    kit::sectionHeader("Reading");
    if (!errorText_.empty()) {
        kit::blockedState("Measurement unavailable", errorText_.c_str(),
                          "Remove an unresolved pick and select its geometry again.");
        if (!measureResolving_ && st.selection.size() <= 8 &&
            measureSelection(st).error.empty() && kit::secondaryButton("Retry measurement")) {
            lastMeasureRefs_.clear();
            tickLiveMeasure(st);
        }
    } else if (st.selection.empty()) {
        kit::emptyState("measure", "Ready to measure", "Select geometry in the viewport to see its dimensions here.");
    } else if (measureResolving_) {
        kit::spinner("Resolving selection...");
        kit::dimText("Waiting for every selected item. No partial reading is shown.");
    } else if (pending_) {
        kit::spinner("Computing measurement...");
    } else if (measureDirty_) {
        kit::dimText("Ready — waiting for selection to settle.");
    } else {
        kit::chip("Ready", kit::Tone::Ok);
        const auto rows = measureRows(st, result_);
        for (const auto& row : rows)
            kit::keyValueRow(row.first.c_str(), row.second.c_str(), kit::Tone::Neutral, true);
        if (result_.contains("closed") && result_["closed"].is_boolean())
            kit::keyValueRow("Closed profile", result_["closed"].get<bool>() ? "Yes" : "No");
        if (kit::secondaryButton("Copy reading")) {
            const auto text = measureReadingText(st, result_);
            ImGui::SetClipboardText(text.c_str());
        }
    }
    if (!measureLog_.empty() && ImGui::CollapsingHeader("Recent readings (this document)")) {
        kit::dimText("Previous readings are history, not the current selection.");
        if (kit::secondaryButton("Clear history")) measureLog_.clear();
        for (size_t i = measureLog_.size(); i-- > 0;) {
            ImGui::PushID((int)i);
            ImGui::TextWrapped("%s", measureLog_[i].c_str());
            if (kit::secondaryButton("Copy")) ImGui::SetClipboardText(measureLog_[i].c_str());
            kit::rule();
            ImGui::PopID();
        }
    }
}

void EvaluateDialogs::drawMassWindow(AppState& st) {
    if (!massOpen_) return;

    ui::setNextCommandDialogPlacement("Mass Properties##eval_mass",
        ImVec2(ui::metrics::px(480), ui::metrics::px(620)),
        ImVec2(ui::metrics::px(340), ui::metrics::px(260)));
    const auto& gpal = ui::palette;
    bool open = true;
    ImGui::SetNextWindowBgAlpha(1.0f);
    const bool vis = ImGui::Begin("Mass Properties##eval_mass", &open, ImGuiWindowFlags_NoScrollbar);
    if (vis) {
        const auto surf = ui::adaptiveSurface(-1.0f);
        ImGui::PushTextWrapPos(0.0f);
        // Material combo lists curated physical materials (same keys as FeatureManager Material).
        // Selecting one WRITES set_material (+ derived set_appearance) so mass, sim, and the tree
        // all share ONE active part material. Density follows that key unless the user has set an
        // explicit advanced density override below.
        auto applyMassMaterial = [&](const std::string& key) {
            if (!st.docMirror.hasDoc() || !st.workerStarted) return;
            std::vector<uint32_t> bodies = st.docBodyIds;
            if (bodies.empty()) bodies.push_back(1);
            float col[3]; float mtl = 0.0f, rgh = 0.5f;
            materialAppearance(key, col, mtl, rgh);
            nlohmann::json ops = nlohmann::json::array();
            ops.push_back({{"op", "set_material"}, {"key", key}});
            for (uint32_t bid : bodies)
                ops.push_back({{"op", "set_appearance"},
                               {"id", "b" + std::to_string(bid)},
                               {"color", {col[0], col[1], col[2]}},
                               {"metallic", mtl}, {"roughness", rgh}});
            // Optimistic local update so density re-evals immediately; DocMirror listener
            // will confirm via the tree once applyOpsAsync drains.
            massDocKey_ = key;
            massDocName_ = materialDisplayName(key);
            massCustomDensity_ = false;
            massDensity_ = materialDensityGcm3(key);
            ++st.rebuildsInFlight;
            st.docMirror.applyOpsAsync(st.rpc, st.worker, ops,
                [&st](const core::OpsResponse& resp, bool applied) {
                    --st.rebuildsInFlight;
                    if (!applied) return;
                    if (resp.ok && !resp.aicm_path.empty()) {
                        st.loadDocMeshAsync(resp.aicm_path);
                    } else if (!resp.ok) {
                        st.addLog(std::string("[err] set_material: ") + resp.error_message);
                        if (resp.error_code != "transport")
                            st.toast(ui::ToastLevel::Error,
                                     std::string("set_material failed: ") +
                                     (resp.error_message.empty() ? resp.error_code
                                                                 : resp.error_message));
                    }
                    if (resp.ok) st.markActiveTabDirty();
                });
            runMassEval(st, {{"density_g_cm3", (double)massDensity_}});
        };

        // Live re-seed from doc while open (FeatureManager / sim may have changed the material).
        // Skip when a custom density override is active so we don't clobber the user's override.
        {
            std::string liveKey;
            if (st.docMirror.hasDoc()) {
                const auto& tree = st.docMirror.tree();
                if (tree.is_object() && tree.contains("material") && tree["material"].is_object())
                    liveKey = tree["material"].value("key", std::string());
            }
            if (liveKey != massDocKey_) {
                massDocKey_ = liveKey;
                massDocName_ = materialDisplayName(liveKey);
                if (!massCustomDensity_) {
                    massDensity_ = liveKey.empty() ? 7.85f : materialDensityGcm3(liveKey);
                    if (!massPending_)
                        runMassEval(st, {{"density_g_cm3", (double)massDensity_}});
                }
            }
        }

        kit::keyValueRow("Part material", massDocKey_.empty() ? "Not set" : massDocName_.c_str());

        ImGui::TextUnformatted("Material");
        ImGui::SetNextItemWidth(-1.0f);
        const std::string comboLabel = massDocKey_.empty()
            ? std::string("(select material)")
            : massDocName_;
        if (ImGui::BeginCombo("##Material", comboLabel.c_str())) {
            for (int i = 0; i < kPhysicalMaterialsCount; ++i) {
                const auto& pm = kPhysicalMaterials[i];
                bool sel = (massDocKey_ == pm.key);
                if (ImGui::Selectable(pm.name, sel)) applyMassMaterial(pm.key);
                if (sel) ImGui::SetItemDefaultFocus();
            }
            ImGui::EndCombo();
        }
        // Density is stored and sent in g/cm3 (the eval RPC's density_g_cm3), but Volume and
        // Mass above already honour st.ips() - so an IPS document read "Volume 4.322in^3,
        // Mass 0.4216lb, Density 2.700 g/cm3", mixing unit systems inside one panel. Edit and
        // display a converted proxy in IPS; the wire value stays metric.
        constexpr double kLbPerIn3PerGcm3 = 0.0361272929;
        const bool ips = st.ips();
        ImGui::TextUnformatted("Density override");
        ImGui::SetNextItemWidth(-1.0f);
        float shown = ips ? (float)(massDensity_ * kLbPerIn3PerGcm3) : massDensity_;
        const float shownMax = ips ? (float)(25.0 * kLbPerIn3PerGcm3) : 25.0f;
        if (ImGui::DragFloat("##Density", &shown, ips ? 0.0005f : 0.01f, 0.0f, shownMax,
                             ips ? "%.4f lb/in3" : "%.3f g/cm3", ImGuiSliderFlags_AlwaysClamp)) {
            massDensity_ = ips ? (float)(shown / kLbPerIn3PerGcm3) : shown;
            massCustomDensity_ = true;
        }
        if (ImGui::IsItemDeactivatedAfterEdit())
            runMassEval(st, {{"density_g_cm3", (double)massDensity_}});
        if (massCustomDensity_ && !massDocKey_.empty()) {
            if (ImGui::SmallButton("Reset##mass_dens")) {
                massCustomDensity_ = false;
                massDensity_ = materialDensityGcm3(massDocKey_);
                runMassEval(st, {{"density_g_cm3", (double)massDensity_}});
            }
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Restore density from the part material");
        }

        if (!massErrorText_.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(gpal.statusErr[0], gpal.statusErr[1],
                                                       gpal.statusErr[2], gpal.statusErr[3]));
            ImGui::TextWrapped("%s", massErrorText_.c_str());
            ImGui::PopStyleColor();
        } else if (massPending_) {
            kit::spinner("Computing mass properties...");
        } else {
            ImGui::Separator();
            kit::keyValueRow("Volume", pmFormatVolume(massResult_.value("volume_mm3", 0.0), st.ips()).c_str());
            kit::keyValueRow("Mass", pmFormatMass(massResult_.value("mass_g", 0.0), st.ips()).c_str());
            if (ips)
                ImGui::Text("Density: %.4f lb/in3", massDensity_ * kLbPerIn3PerGcm3);
            else
                ImGui::Text("Density: %.3f g/cm3", massDensity_);
            if (massResult_.contains("com") && massResult_["com"].is_array() &&
                massResult_["com"].size() == 3) {
                kit::sectionHeader("Center of gravity");
                // A CG on a symmetry plane comes back as floating-point zero noise, and the panel
                // printed it verbatim: "X: 4.449e-17in  Y: -1.062e-17in  Z: -6.856e-19in" for a
                // centred box. Anything under a nanometre is zero at any display precision.
                auto com = [&](int i) {
                    double mm = massResult_["com"][i].get<double>();
                    if (std::abs(mm) < 1e-6) mm = 0.0;
                    return evalLen(st, mm);
                };
                kit::keyValueRow("X", com(0).c_str());
                kit::keyValueRow("Y", com(1).c_str());
                kit::keyValueRow("Z", com(2).c_str());
            }

            // ---- Moment of inertia (bending area I + polar J) — clear callout ----
            kit::sectionHeader("Moment of inertia");
            ImGui::TextWrapped(
                "Area moments about centroidal planes through CG: I resists bending; "
                "polar J = Imax+Imin resists torsion. Units follow document (in⁴ / mm⁴).");
            bool cb = false;
            cb |= ImGui::Checkbox("Show COM in viewport", &showCom_);
            cb |= ImGui::Checkbox("Area moment I (bending)", &showI_);
            cb |= ImGui::Checkbox("Polar moment J (torsion)", &showJ_);
            cb |= ImGui::Checkbox("Axial stiffness k (EA/L est.)", &showK_);
            if (cb) rebuildMassOverlays(st);

            if (designSectionsPending_ > 0)
                kit::spinner("Computing section I/J...");
            if (designSections_.is_array()) {
                for (int i = 0; i < 3; ++i) {
                    if ((size_t)i >= designSections_.size() || !designSections_[i].is_object())
                        continue;
                    const auto& sec = designSections_[i];
                    if (sec.contains("error")) {
                        ImGui::PushStyleColor(ImGuiCol_Text, kit::toneVec(kit::Tone::Err));
                        ImGui::TextWrapped("Principal axis %d: %s", i + 1,
                                           sec["error"].get<std::string>().c_str());
                        ImGui::PopStyleColor();
                        continue;
                    }
                    ImGui::Separator();
                    double Ixx = sec.value("Ixx", 0.0), Iyy = sec.value("Iyy", 0.0);
                    double Imax = std::max(Ixx, Iyy), Imin = std::min(Ixx, Iyy);
                    double J = sec.value("J", Ixx + Iyy);
                    ImGui::Text("Principal axis %d", i + 1);
                    if (showI_) {
                        kit::keyValueRow("Imax", pmFormatAreaMoment(Imax, st.ips()).c_str());
                        kit::keyValueRow("Imin", pmFormatAreaMoment(Imin, st.ips()).c_str());
                    }
                    if (showJ_)
                        kit::keyValueRow("Polar J", pmFormatAreaMoment(J, st.ips()).c_str());
                    if (showK_ && sec.contains("EA") && sec["EA"].is_number()) {
                        ImGui::TextDisabled("  EA: %.4g N  (divide by length L for k)",
                                            sec["EA"].get<double>());
                    }
                    if (sec.contains("area_mm2"))
                        ImGui::Text("  Section area: %s",
                                    pmFormatArea(sec.value("area_mm2", 0.0), st.ips()).c_str());
                }
            }

            if (ImGui::TreeNode("Advanced mass inertia")) {
                const char* mmUnit = st.ips() ? "lb·in²" : "g·mm²";
                if (massResult_.contains("inertia") && massResult_["inertia"].is_array() &&
                    massResult_["inertia"].size() == 9) {
                    ImGui::TextDisabled("Inertia tensor (%s, about origin)", mmUnit);
                    const auto& I = massResult_["inertia"];
                    for (int r = 0; r < 3; ++r)
                        ImGui::Text("%s  %s  %s",
                                    pmFormatMassMoment(I[r * 3 + 0].get<double>(), st.ips())
                                        .c_str(),
                                    pmFormatMassMoment(I[r * 3 + 1].get<double>(), st.ips())
                                        .c_str(),
                                    pmFormatMassMoment(I[r * 3 + 2].get<double>(), st.ips())
                                        .c_str());
                }
                if (massResult_.contains("principal_moments") &&
                    massResult_["principal_moments"].is_array() &&
                    massResult_["principal_moments"].size() == 3) {
                    ImGui::TextDisabled("Principal mass moments (%s)", mmUnit);
                    const auto& Pm = massResult_["principal_moments"];
                    for (int i = 0; i < 3; ++i)
                        ImGui::Text("  P%d  %s", i + 1,
                                    pmFormatMassMoment(Pm[i].get<double>(), st.ips()).c_str());
                }
                ImGui::TreePop();
            }
        }
        ImGui::PopTextWrapPos();
        ui::drawWindowPillScrollbarV(surf);
    }
    ImGui::End();

    if (!open) {
        ++massGen_;
        ++designSectionGen_;
        designSectionsPending_ = 0;
        massOpen_ = false;
        massMarkers_.clear();
        massSegs_.clear();
        massPending_ = false;
        massErrorText_.clear();
        designSectionsDirty_ = false;
        // Restore body alpha only if section/interference also gone.
        if (!sectionOpen_ && openWindow_ != "interference") {
            massBodyAlpha_ = 1.0f;
            if (st.renderer) st.renderer->setBodyAlpha(1.0f);
        } else if (sectionOpen_) {
            massBodyAlpha_ = 0.45f;
        }
    }
}

void EvaluateDialogs::drawSectionWindow(AppState& st) {
    if (!sectionOpen_) return;

    ui::setNextCommandDialogPlacement("Section Properties##eval_section",
        ImVec2(ui::metrics::px(480), ui::metrics::px(640)),
        ImVec2(ui::metrics::px(340), ui::metrics::px(280)));
    const auto& gpal = ui::palette;
    bool open = true;
    ImGui::SetNextWindowBgAlpha(1.0f);
    const bool vis = ImGui::Begin("Section Properties##eval_section", &open, ImGuiWindowFlags_NoScrollbar);
    if (vis) {
        const auto surf = ui::adaptiveSurface(-1.0f);
        ImGui::PushTextWrapPos(0.0f);

        if (!sectionErrorText_.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(gpal.statusErr[0], gpal.statusErr[1],
                                                       gpal.statusErr[2], gpal.statusErr[3]));
            ImGui::TextWrapped("%s", sectionErrorText_.c_str());
            ImGui::PopStyleColor();
        }
        if (sectionPendingCount_ > 0)
            kit::spinner("Computing section properties...");
        kit::dimText("Drag a plane in the viewport or use the offset sliders.");

        // 3-card layout: one card per orthogonal section.
        for (int i = 0; i < 3; ++i) {
            const float* col = kEvalPalette[i % 10];
            char header[64];
            std::snprintf(header, sizeof(header), "Sec %d · %s", i + 1, kSectionNames[i]);
            drawColorChipRow(header, col);

            ImGui::PushID(i);
            // Hidden label + explicit width so "Offset" text never clips the right edge.
            ImGui::TextUnformatted("Offset");
            const float avail = ImGui::GetContentRegionAvail().x;
            ImGui::SetNextItemWidth(std::max(80.0f * ui::uiScale, avail - 4.0f * ui::uiScale));
            const float unitScale = st.ips() ? 25.4f : 1.0f;
            float off = sectionOffset_[i] / unitScale;
            char sliderId[32];
            std::snprintf(sliderId, sizeof(sliderId), "##sec%d", i);
            if (ImGui::SliderFloat(sliderId, &off, -sectionHalfSize_ / unitScale,
                                   sectionHalfSize_ / unitScale, st.ips() ? "%.3f in" : "%.2f mm")) {
                applySectionOffset(st, i, off * unitScale, true);
            }
            if (ImGui::IsItemDeactivatedAfterEdit()) {
                sectionPendingIdx_ = i;
                sectionDragLastT_ = ImGui::GetTime() - 1.0;
            }

            if (sectionResults_.is_array() && (size_t)i < sectionResults_.size() &&
                sectionResults_[i].is_object()) {
                const auto& sec = sectionResults_[i];
                if (sec.contains("error") && sec["error"].is_string()) {
                    ImGui::PushStyleColor(ImGuiCol_Text,
                                          ImVec4(gpal.statusErr[0], gpal.statusErr[1],
                                                 gpal.statusErr[2], gpal.statusErr[3]));
                    ImGui::TextWrapped("%s", sec["error"].get<std::string>().c_str());
                    ImGui::PopStyleColor();
                } else {
                    kit::keyValueRow("Area", pmFormatArea(sec.value("area_mm2", 0.0), st.ips()).c_str());
                    if (sec.contains("centroid") && sec["centroid"].is_array() &&
                        sec["centroid"].size() == 3) {
                        const char* labels[] = {"Centroid X", "Centroid Y", "Centroid Z"};
                        for (int axis = 0; axis < 3; ++axis)
                            kit::keyValueRow(labels[axis], evalLen(st, sec["centroid"][axis].get<double>()).c_str());
                    }
                    double Ixx = sec.value("Ixx", 0.0);
                    double Iyy = sec.value("Iyy", 0.0);
                    double Ixy = sec.value("Ixy", 0.0);
                    double J = sec.value("J", Ixx + Iyy);
                    kit::keyValueRow("Ixx", pmFormatAreaMoment(Ixx, st.ips()).c_str());
                    kit::keyValueRow("Iyy", pmFormatAreaMoment(Iyy, st.ips()).c_str());
                    kit::keyValueRow("Ixy", pmFormatAreaMoment(Ixy, st.ips()).c_str());
                    kit::keyValueRow("Polar J", pmFormatAreaMoment(J, st.ips()).c_str());
                }
            } else if (sectionPendingCount_ > 0) {
                ImGui::TextDisabled("Waiting for section…");
            } else {
                ImGui::TextDisabled("No section data.");
            }
            ImGui::PopID();
        }
        ImGui::PopTextWrapPos();
        ui::drawWindowPillScrollbarV(surf);
    }
    ImGui::End();

    if (!open) {
        sectionOpen_ = false;
        sectionPlanes_.clear();
        sectionPendingCount_ = 0;
        sectionPendingIdx_ = -1;
        sectionErrorText_.clear();
        ++sectionEvalGen_;  // supersede in-flight
        if (!massOpen_ || massMarkers_.empty()) {
            if (openWindow_ != "interference") {
                massBodyAlpha_ = 1.0f;
                if (st.renderer) st.renderer->setBodyAlpha(1.0f);
            }
        }
    }
}

void EvaluateDialogs::drawExclusiveWindow(AppState& st) {
    if (openWindow_.empty()) return;

    const auto& gpal = ui::palette;
    bool open = true;
    char winTitle[128];
    std::snprintf(winTitle, sizeof(winTitle), "%s###eval_%s",
                  title_.empty() ? "Evaluate" : title_.c_str(), openWindow_.c_str());
    ui::setNextCommandDialogPlacement(winTitle,
        ImVec2(ui::metrics::px(480),
               ui::metrics::px(openWindow_ == "measure" || openWindow_ == "interference" ? 580 : 380)),
        ImVec2(ui::metrics::px(340), ui::metrics::px(240)));
    ImGui::SetNextWindowBgAlpha(1.0f);
    const bool evVis = ImGui::Begin(winTitle, &open, ImGuiWindowFlags_NoScrollbar);
    if (evVis) {
        const auto surf = ui::adaptiveSurface(-1.0f);
        ImGui::PushTextWrapPos(0.0f);

        const bool interactivePending =
            openWindow_ == "measure" || openWindow_ == "draft" || openWindow_ == "thickness" ||
            openWindow_ == "curvature" || openWindow_ == "zebra" || openWindow_ == "symmetry" ||
            openWindow_ == "geometry";
        if (pending_ && !interactivePending) {
            kit::spinner("Computing analysis...");
        } else if (!errorText_.empty() && openWindow_ != "measure" && openWindow_ != "draft" &&
                   openWindow_ != "thickness" && openWindow_ != "curvature" &&
                   openWindow_ != "field_err" && openWindow_ != "interference" &&
                   openWindow_ != "geometry") {
            ImGui::PushStyleColor(
                ImGuiCol_Text,
                ImVec4(gpal.statusErr[0], gpal.statusErr[1], gpal.statusErr[2], gpal.statusErr[3]));
            ImGui::TextWrapped("%s", errorText_.c_str());
            ImGui::PopStyleColor();
        } else if (openWindow_ == "measure") {
            drawMeasure(st);
        } else if (openWindow_ == "geometry") {
            drawGeometryAnalysis(st);
        } else if (openWindow_ == "check") {
            if (result_.contains("issues") && result_["issues"].is_array()) {
                const auto& issues = result_["issues"];
                if (issues.empty()) {
                    ImGui::PushStyleColor(
                        ImGuiCol_Text, ImVec4(gpal.statusOk[0], gpal.statusOk[1], gpal.statusOk[2],
                                              gpal.statusOk[3]));
                    ImGui::TextWrapped("No geometry issues found.");
                    ImGui::PopStyleColor();
                } else {
                    ImGui::Text("%d issue(s):", (int)issues.size());
                    for (const auto& it : issues) {
                        std::string sev = it.value("severity", std::string("info"));
                        std::string txt = it.value("text", std::string());
                        ImGui::BulletText("[%s] %s", sev.c_str(), txt.c_str());
                    }
                }
            }
        } else if (openWindow_ == "interference") {
            if (!errorText_.empty())
                kit::blockedState("Interference unavailable", errorText_.c_str());
            if (!interferencePartNames_.empty()) {
                kit::sectionHeader("Members");
                bool anyChange = false;
                for (size_t i = 0; i < interferencePartNames_.size(); ++i) {
                    if (i >= interferencePartChecked_.size()) break;
                    bool checked = interferencePartChecked_[i] != 0;
                    ImGui::PushID((int)i);
                    if (ImGui::Checkbox("##member", &checked)) {
                        interferencePartChecked_[i] = checked ? 1 : 0;
                        anyChange = true;
                    }
                    ImGui::SameLine();
                    ImGui::TextWrapped("%s", interferencePartNames_[i].c_str());
                    ImGui::PopID();
                }
                if (anyChange) {
                    nlohmann::json members = nlohmann::json::array();
                    for (size_t i = 0; i < interferencePartNames_.size(); ++i) {
                        if (i < interferencePartChecked_.size() && interferencePartChecked_[i])
                            members.push_back(interferencePartNames_[i]);
                    }
                    nlohmann::json extra = nlohmann::json::object();
                    if (members.empty()) {
                        ++gen_;
                        pending_ = false;
                        result_ = nlohmann::json::object();
                        errorText_ = "Select at least one member to check interference.";
                        clearOverlays();
                    } else {
                        extra["members"] = members;
                        runEval(st, "eval_interference", extra);
                    }
                }
                ImGui::Separator();
            }

            bool hasPairs = result_.contains("interferences") &&
                            result_["interferences"].is_array() &&
                            !result_["interferences"].empty();
            bool hasClear = result_.contains("clearances") && result_["clearances"].is_array() &&
                            !result_["clearances"].empty();
            if (result_.contains("summary"))
                ImGui::TextWrapped("%s", result_.value("summary", std::string()).c_str());
            const bool complete = result_.value("complete", false);
            if (!result_.empty() && !complete) {
                kit::blockedState("Incomplete interference check",
                    "Some geometry or pair checks failed. Known clashes below remain valid; "
                    "this report cannot establish clearance.");
                for (const char* key : {"geometry_failures", "pair_failures"}) {
                    if (!result_.contains(key) || !result_[key].is_array()) continue;
                    kit::sectionHeader(key[0] == 'g' ? "Geometry failures" : "Pair failures");
                    for (const auto& failure : result_[key]) {
                        if (!failure.is_object()) continue;
                        std::string subject = failure.value("name",
                            failure.value("component", failure.value("feature", std::string())));
                        if (failure.contains("a") && failure.contains("b"))
                            subject = failure["a"].is_string() ? failure["a"].get<std::string>() : failure["a"].dump();
                        if (failure.contains("b"))
                            subject += " / " + (failure["b"].is_string() ? failure["b"].get<std::string>() : failure["b"].dump());
                        ImGui::TextWrapped("%s: %s", subject.empty() ? "Geometry" : subject.c_str(),
                                           failure.value("reason", std::string("Analysis failed")).c_str());
                    }
                }
            }
            if (hasPairs) {
                kit::sectionHeader("Interferences");
                int pi = 0;
                for (const auto& p : result_["interferences"]) {
                    std::string a = p.value("a", std::string("?"));
                    std::string b = p.value("b", std::string("?"));
                    double vol = p.value("overlap_volume_mm3",
                                         p.value("volume", p.value("volume_mm3", 0.0)));
                    std::string bboxTxt;
                    if (p.contains("overlap_bbox") && p["overlap_bbox"].is_array() &&
                        p["overlap_bbox"].size() == 2 && p["overlap_bbox"][0].is_array() &&
                        p["overlap_bbox"][1].is_array() && p["overlap_bbox"][0].size() >= 3 &&
                        p["overlap_bbox"][1].size() >= 3) {
                        double dx =
                            p["overlap_bbox"][1][0].get<double>() - p["overlap_bbox"][0][0].get<double>();
                        double dy =
                            p["overlap_bbox"][1][1].get<double>() - p["overlap_bbox"][0][1].get<double>();
                        double dz =
                            p["overlap_bbox"][1][2].get<double>() - p["overlap_bbox"][0][2].get<double>();
                        bboxTxt = evalLen(st, dx) + " × " + evalLen(st, dy) + " × " + evalLen(st, dz);
                    }
                    const float* ca = kEvalPalette[(pi * 2) % 10];
                    std::string line = a + " × " + b + ": overlap " + pmFormatVolume(vol, st.ips());
                    if (!bboxTxt.empty()) line += ", bounding box " + bboxTxt;
                    drawColorChipRow(line.c_str(), ca);
                    ++pi;
                }
            }
            if (hasClear) {
                kit::sectionHeader("Clearances");
                for (const auto& p : result_["clearances"]) {
                    std::string a = p.value("a", std::string("?"));
                    std::string b = p.value("b", std::string("?"));
                    std::string reading = "exact gap not measured";
                    if (p.contains("distance_lower_bound_mm") && p["distance_lower_bound_mm"].is_number())
                        reading = ">= " + evalLen(st, p["distance_lower_bound_mm"].get<double>()) +
                                  " (lower bound, not measured)";
                    else if (p.contains("distance") && p["distance"].is_number()) {
                        reading = evalLen(st, p["distance"].get<double>());
                        if (p.value("approx", false)) reading += " (lower bound, not measured)";
                    }
                    ImGui::BulletText("%s <-> %s : %s", a.c_str(), b.c_str(), reading.c_str());
                }
            }
            if (!hasPairs && !hasClear && !result_.contains("summary")) {
                ImGui::TextWrapped(
                    "Interference detection runs across assembly components or multi-body parts. "
                    "Open an assembly or a multi-body part to check clashes.");
            }
            if (complete && result_.value("ok", false) && result_.value("pairs_checked", 0) > 0 &&
                !hasPairs && errorText_.empty() && !pending_)
                kit::chip("No interference", kit::Tone::Ok);
        } else if (openWindow_ == "symmetry") {
            ImGui::SetNextItemWidth(100 * ui::uiScale);
            if (ImGui::Combo("Plane", &symmetryPlane_, "Front\0Top\0Right\0")) {
                static const int map[] = {1, 0, 2};
                int pi = (symmetryPlane_ >= 0 && symmetryPlane_ < 3) ? map[symmetryPlane_] : 1;
                runEval(st, "eval_symmetry", {{"plane", kPlaneNames[pi]}});
            }
            if (pending_) {
                kit::spinner("Computing symmetry...");
            } else {
                bool sym = result_.value("symmetric", false);
                kit::keyValueRow("Symmetric", sym ? "Yes" : "No");
                kit::keyValueRow("Deviation", pmFormatVolume(result_.value("deviation_mm3", 0.0), st.ips()).c_str());
            }
        } else if (openWindow_ == "zebra") {
            ImGui::TextWrapped(
                "Smooth even stripes = fair surface; kinks = G1 break; jogs = G2 break.");
            ImGui::SetNextItemWidth(160 * ui::uiScale);
            if (ImGui::SliderFloat("Density", &gZebraFreq, 4.0f, 60.0f, "%.0f")) {
                if (st.renderer) st.renderer->setZebra(true, gZebraFreq);
            }
            if (kit::secondaryButton("Turn Off")) {
                if (st.renderer) st.renderer->setZebra(false, gZebraFreq);
                openWindow_.clear();
            }
        } else if (openWindow_ == "draft") {
            if (!errorText_.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(gpal.statusErr[0], gpal.statusErr[1],
                                                           gpal.statusErr[2], gpal.statusErr[3]));
                ImGui::TextWrapped("%s", errorText_.c_str());
                ImGui::PopStyleColor();
            }
            if (pending_) {
                kit::spinner("Computing draft...");
            }
            ImGui::SetNextItemWidth(90 * ui::uiScale);
            ImGui::Combo("Pull", &draftPull_, "X\0Y\0Z\0-Z\0");
            ImGui::SetNextItemWidth(80 * ui::uiScale);
            ImGui::DragFloat("Angle", &draftAngleDeg_, 0.1f, 0.0f, 45.0f, "%.1f°",
                             ImGuiSliderFlags_AlwaysClamp);
            if (kit::primaryButton("Update", !pending_)) {
                static const double kPull[4][3] = {
                    {1, 0, 0}, {0, 1, 0}, {0, 0, 1}, {0, 0, -1}};
                int pi = (draftPull_ >= 0 && draftPull_ < 4) ? draftPull_ : 2;
                runFieldEval(st, "eval_draft",
                             {{"pull", {kPull[pi][0], kPull[pi][1], kPull[pi][2]}},
                              {"angle_deg", (double)draftAngleDeg_}},
                             "Draft angle", "deg");
            }
            if (kit::secondaryButton("Turn Off")) {
                clearFieldColormap(st);
                openWindow_.clear();
            }
        } else if (openWindow_ == "thickness") {
            if (!errorText_.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(gpal.statusErr[0], gpal.statusErr[1],
                                                           gpal.statusErr[2], gpal.statusErr[3]));
                ImGui::TextWrapped("%s", errorText_.c_str());
                ImGui::PopStyleColor();
            }
            if (pending_) {
                kit::spinner("Computing thickness...");
            }
            ImGui::SetNextItemWidth(90 * ui::uiScale);
            ImGui::DragFloat("Resolution", &thicknessRes_, 0.05f, 0.05f, 5.0f, "%.2f mm",
                             ImGuiSliderFlags_AlwaysClamp);
            if (kit::primaryButton("Update", !pending_)) {
                nlohmann::json extra = nlohmann::json::object();
                if (thicknessRes_ > 0.0f) extra["resolution"] = (double)thicknessRes_;
                runFieldEval(st, "eval_thickness", extra, "Wall thickness",
                             st.ips() ? "in" : "mm");
            }
            if (kit::secondaryButton("Turn Off")) {
                clearFieldColormap(st);
                openWindow_.clear();
            }
        } else if (openWindow_ == "curvature") {
            if (!errorText_.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(gpal.statusErr[0], gpal.statusErr[1],
                                                           gpal.statusErr[2], gpal.statusErr[3]));
                ImGui::TextWrapped("%s", errorText_.c_str());
                ImGui::PopStyleColor();
            }
            if (pending_) {
                kit::spinner("Computing curvature...");
            }
            ImGui::SetNextItemWidth(110 * ui::uiScale);
            ImGui::Combo("Kind", &curvatureKind_, "Mean\0Gaussian\0Max\0Min\0");
            if (kit::primaryButton("Update", !pending_)) {
                static const char* kKinds[] = {"mean", "gaussian", "max", "min"};
                int ki = (curvatureKind_ >= 0 && curvatureKind_ < 4) ? curvatureKind_ : 0;
                const char* kind = kKinds[ki];
                char titleBuf[48];
                std::snprintf(titleBuf, sizeof(titleBuf), "%s curvature",
                              ki == 0   ? "Mean"
                              : ki == 1 ? "Gaussian"
                              : ki == 2 ? "Max"
                                        : "Min");
                runFieldEval(st, "eval_curvature", {{"kind", kind}}, titleBuf, "1/mm");
            }
            if (kit::secondaryButton("Turn Off")) {
                clearFieldColormap(st);
                openWindow_.clear();
            }
        } else if (openWindow_ == "field_wait") {
            ImGui::TextDisabled("Computing field overlay…");
        } else if (openWindow_ == "field_err") {
            ImGui::PushStyleColor(
                ImGuiCol_Text,
                ImVec4(gpal.statusErr[0], gpal.statusErr[1], gpal.statusErr[2], gpal.statusErr[3]));
            ImGui::TextWrapped("%s", errorText_.c_str());
            ImGui::PopStyleColor();
        }
        ImGui::PopTextWrapPos();
        ui::drawWindowPillScrollbarV(surf);
    }
    ImGui::End();
    if (!open || openWindow_.empty()) {
        ++gen_;
        pending_ = false;
        measureDirty_ = false;
        result_ = nlohmann::json::object();
        errorText_.clear();
        if (openWindow_ == "zebra" && st.renderer) st.renderer->setZebra(false, gZebraFreq);
        if (openWindow_ == "draft" || openWindow_ == "thickness" || openWindow_ == "curvature" ||
            openWindow_ == "field_wait" || openWindow_ == "field_err")
            clearFieldColormap(st);
        // Exclusive close does NOT clear mass/section — only exclusive overlays.
        clearOverlays();
        openWindow_.clear();
        // Body alpha: leave translucent if mass/section still open.
        if (!massOpen_ && !sectionOpen_) {
            massBodyAlpha_ = 1.0f;
            if (st.renderer) st.renderer->setBodyAlpha(1.0f);
        }
    }
}

void EvaluateDialogs::prepareFrame(AppState& st) {
    syncDocument(st);
    tickLiveMeasure(st);
}

void EvaluateDialogs::draw(AppState& st) {
    prepareFrame(st);
    // Mass design section chain and section plane drag debounce.
    tickMassDesignSections(st);
    tickSectionDrag(st);
    if (massOpen_ && !massPending_) rebuildMassOverlays(st);
    if (sectionOpen_) rebuildSectionPlaneViews(st);

    // Nothing open → clear leftover exclusive overlays (mass/section have their own close paths).
    if (openWindow_.empty() && !massOpen_ && !sectionOpen_) {
        if (massBodyAlpha_ < 0.999f || !overlaySegs_.empty() || !massSegs_.empty() ||
            interferenceView_.active || !overlayHighlights_.empty() || !massMarkers_.empty() ||
            !sectionPlanes_.empty())
            clearOverlays();
        massMarkers_.clear();
        massSegs_.clear();
        sectionPlanes_.clear();
        massBodyAlpha_ = 1.0f;
        return;
    }

    // Independent windows — order: mass, section, exclusive.
    kit::pushDensity(kit::Density::Cozy);
    drawMassWindow(st);
    drawSectionWindow(st);
    drawExclusiveWindow(st);
    kit::popDensity();
}

}  // namespace ui
