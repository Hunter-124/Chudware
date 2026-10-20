// Evaluate dialogs implementation (C19 + Evaluate overhaul) — see evaluate_dialogs.h.
#include "evaluate_dialogs.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <initializer_list>
#include <string>
#include <unordered_map>

#include "imgui.h"
#include "imgui_internal.h"     // ImRect (ui::adaptivePanel / ui::glassPanel rect argument)

#include "appearance_common.h"
#include "layout.h"
#include "metrics.h"
#include "viewport_panel.h"
#include "widgets/kit.h"
#include "../core/app_state.h"
#include "../renderer.h"
#include "../ui_theme.h"

namespace ui {

// Zebra stripe density used by the Zebra Stripes control window. File-scope so it persists across
// frames without adding a member to EvaluateDialogs (the class has a single instance). Matches the
// renderer's default frequency; the slider (4..60) drives it live via Renderer::setZebra().
static float gZebraFreq = 18.0f;

// Shared with the viewport and MCP measurement emphasis contract.
static const auto& kEvalPalette = ui::evalEmphasisPalette;

// Display-only readouts. The property-manager formatters ("%.4g" glued to the unit) double as
// edit-buffer text, so the dialogs format through kit::fmtQuantity instead: significant digits,
// thousands separators, a space before the unit, no e-notation noise. Values arrive canonical
// (mm, mm², mm³, mm⁴, g, g·mm², N) and read in the document's unit system. Anything below 1e-9 of
// `ref` (the magnitude the value is read against: part size, the larger moment), and anything
// below 1e-9 canonical units, is floating-point residue from a symmetric part ("-1.049e-17in")
// and reads as 0.
enum class EvalQty { Length, Area, Volume, Mass, AreaMoment, MassMoment, Force };
static std::string evalQty(bool ips, EvalQty q, double canonical, double ref = 0.0) {
    struct Sys {
        double      perUnit;   // canonical units per displayed unit
        const char* unit;
    };
    static const Sys kMetric[] = {
        {1.0, kit::unitText(kit::Unit::mm)},  {1.0, kit::unitText(kit::Unit::mm2)},
        {1.0, kit::unitText(kit::Unit::mm3)}, {1.0, kit::unitText(kit::Unit::g)},
        {1.0, "mm\xE2\x81\xB4"},              {1.0, "g\xC2\xB7mm\xC2\xB2"},
        {1.0, kit::unitText(kit::Unit::N)},
    };
    static const Sys kIps[] = {
        {25.4, kit::unitText(kit::Unit::in)}, {645.16, kit::unitText(kit::Unit::in2)},
        {16387.064, "in\xC2\xB3"},            {453.59237, kit::unitText(kit::Unit::lb)},
        {416231.4256, "in\xE2\x81\xB4"},      {292639.66, "lb\xC2\xB7in\xC2\xB2"},
        {4.4482216152605, kit::unitText(kit::Unit::lbf)},
    };
    const Sys& s = (ips ? kIps : kMetric)[(int)q];
    const double noise = std::max(1e-9, std::fabs(ref) * 1e-9);
    if (std::isfinite(canonical) && std::fabs(canonical) <= noise) canonical = 0.0;
    return kit::fmtQuantity(canonical / s.perUnit, s.unit);
}

// A linear value (canonical mm) in the active document units.
static std::string evalLen(AppState& st, double mm, double ref = 0.0) {
    return evalQty(st.ips(), EvalQty::Length, mm, ref);
}

// Small filled dot in a palette tint: pairs a card or row with its viewport highlight without
// washing the whole card in a colour that reads as a status (a pink card looks like an error).
// `tip` says what the colour refers to, so the dot is never an unexplained mark.
static void evalSwatch(const float rgba[4], const char* tip = nullptr) {
    const float  h = ImGui::GetTextLineHeight();
    const ImVec2 p = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(h, h));
    kit::itemTooltip(tip);
    ImGui::GetWindowDrawList()->AddCircleFilled(ImVec2(p.x + h * 0.5f, p.y + h * 0.5f),
                                                ui::metrics::U(1.25f),
                                                ImGui::GetColorU32(ImVec4(rgba[0], rgba[1], rgba[2], 1.0f)));
}

// Window name of the exclusive Evaluate tool window ("Measure###eval_measure"). One definition so
// the cascade below can find it from the other dialogs.
static std::string evalExclusiveWindowName(const std::string& title, const std::string& kind) {
    return (title.empty() ? std::string("Evaluate") : title) + "###eval_" + kind;
}

// Recover Evaluate windows inside the clear viewport, below the doc tabs. The shared placement
// helper constrains to the full overlay band, which is taller than the clear viewport: a 620dp
// Mass Properties window otherwise gets pushed upward over the tabs by ImGui's viewport clamp.
// Only new/reopened windows cascade; active windows keep user placement unless outside the rect.
static void evalPlaceAppearing(const char* windowName, ImVec2 preferredSizePx,
                               std::initializer_list<const char*> others) {
    ImGuiWindow* self = ImGui::FindWindowByName(windowName);
    const bool appearing = !self || !self->WasActive;
    const ImGuiViewport* vp = ImGui::GetMainViewport();
    ImVec2 lo = vp->WorkPos, hi(lo.x + vp->WorkSize.x, lo.y + vp->WorkSize.y);
    const OverlayBand& band = overlayBand();
    if (band.valid) {
        lo.x = std::max(lo.x, band.minX); lo.y = std::max(lo.y, band.minY);
        hi.x = std::min(hi.x, band.maxX); hi.y = std::min(hi.y, band.maxY);
    }
    ImVec2 clearLo, clearHi;
    if (ui::viewportClearRect(clearLo, clearHi)) {
        lo.x = std::max(lo.x, clearLo.x); lo.y = std::max(lo.y, clearLo.y);
        hi.x = std::min(hi.x, clearHi.x); hi.y = std::min(hi.y, clearHi.y);
    }
    lo.y = std::max(lo.y, docTabBandBottom());
    if (hi.x <= lo.x || hi.y <= lo.y) return;
    const float margin = metrics::px(12);
    lo.x += std::min(margin, (hi.x - lo.x) * 0.05f);
    lo.y += std::min(margin, (hi.y - lo.y) * 0.05f);
    hi.x -= std::min(margin, (hi.x - lo.x) * 0.05f);
    hi.y -= std::min(margin, (hi.y - lo.y) * 0.05f);
    const ImVec2 maximum(hi.x - lo.x, hi.y - lo.y);
    const ImVec2 sharedMinimum = ImGui::GetCurrentContext()->NextWindowData.SizeConstraintRect.Min;
    const ImVec2 minimum(std::min(sharedMinimum.x, maximum.x),
                         std::min(sharedMinimum.y, maximum.y));
    ImVec2 size = preferredSizePx, pos;
    if (self) {
        pos = self->Pos; size = self->SizeFull;
    } else if (const ImGuiWindowSettings* s = ImGui::FindWindowSettingsByID(ImHashStr(windowName))) {
        const ImVec2 vpPos = s->ViewportId ? ImVec2((float)s->ViewportPos.x, (float)s->ViewportPos.y)
                                           : vp->Pos;
        pos = ImVec2(s->Pos.x + vpPos.x, s->Pos.y + vpPos.y);
        size = ImVec2((float)s->Size.x, (float)s->Size.y);
    } else {
        // Leave part of the evaluated model visible beside the initial readout.
        size.x = std::min(size.x, std::max(minimum.x, maximum.x * 0.6f));
        pos = ImVec2(hi.x - size.x, lo.y);
    }
    size.x = std::clamp(size.x, minimum.x, maximum.x);
    size.y = std::clamp(size.y, minimum.y, maximum.y);
    pos.x = std::clamp(pos.x, lo.x, hi.x - size.x);
    pos.y = std::clamp(pos.y, lo.y, hi.y - size.y);
    if (appearing) {
        // A full-height dialog has no room for a vertical cascade. Reserve room for the new
        // window's exposed title bar before positioning it, rather than clamping it back onto
        // the other dialog after a nominal 32dp shift.
        const float step = metrics::px(32);
        for (int k = 0; k < 8; ++k) {
            const ImGuiWindow* clash = nullptr;
            for (const char* name : others) {
                const ImGuiWindow* w = name ? ImGui::FindWindowByName(name) : nullptr;
                if (!w || w == self || !w->WasActive) continue;
                if (std::fabs(w->Pos.x - pos.x) < step &&
                    std::fabs(w->Pos.y - pos.y) < step) { clash = w; break; }
            }
            if (!clash) break;
            const float nextTop = clash->Pos.y + step;
            if (hi.y - nextTop < minimum.y) break;
            pos.x = std::max(lo.x, clash->Pos.x - step);
            pos.y = nextTop;
            size.y = std::min(size.y, hi.y - pos.y);
        }
    }
    ImGui::SetNextWindowSizeConstraints(minimum, maximum);
    ImGui::SetNextWindowSize(size, ImGuiCond_Always);
    ImGui::SetNextWindowPos(pos, ImGuiCond_Always);
}

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
    number("area", "Area", [&](double value) { return evalQty(st.ips(), EvalQty::Area, value); });
    number("volume", "Volume", [&](double value) { return evalQty(st.ips(), EvalQty::Volume, value); });
    number("total_length", "Total length", length);
    number("total_distance", "Chain distance", length);
    number("angle", "Angle", [](double angle) {
        return kit::fmtQuantity(angle, kit::unitText(kit::Unit::deg));
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
        if (!text.empty()) text += " \xc2\xb7 ";
        text += row.first + ": " + row.second;
    }
    return text;
}

static const char* kPlaneNames[] = {"plane.top", "plane.front", "plane.right"};

// Field options for canonical-mm values shown in the document's unit system.
static constexpr double kMmPerIn = 25.4;
static kit::FieldOpts lengthOpts(bool ips) {
    return kit::FieldOpts()
        .withUnit(ips ? kit::Unit::in : kit::Unit::mm)
        .withScale(ips ? 1.0 / kMmPerIn : 1.0);
}
static kit::FieldOpts areaOpts(bool ips) {
    return kit::FieldOpts()
        .withUnit(ips ? kit::Unit::in2 : kit::Unit::mm2)
        .withScale(ips ? 1.0 / (kMmPerIn * kMmPerIn) : 1.0);
}

// Optional analysis limit: 0 (stored) means the engine's size-scaled default. The field shows the
// "Auto" hint while unset; clearing it restores the default.
static bool autoLimitField(const char* id, const char* label, float* v, kit::FieldOpts o) {
    bool present = *v > 0.0f;
    float shown = present ? *v : 0.0f;
    const bool committed =
        kit::fieldNumber(id, label, &shown, o.withPresent(&present).withHint("Auto").withMin(0.0));
    if (committed) *v = present ? std::max(0.0f, shown) : 0.0f;
    return committed;
}

// "measured vs limit" readout for one geometry-analysis finding (worker_rpc.md eval_geometry_analysis).
static std::string findingMeta(AppState& st, const std::string& kind, double value, double limit) {
    const bool ips = st.ips();
    if (kind == "small_face")
        return evalQty(ips, EvalQty::Area, value) + " < " + evalQty(ips, EvalQty::Area, limit);
    if (kind == "short_edge" || kind == "gap") return evalLen(st, value) + " < " + evalLen(st, limit);
    if (kind == "sliver_face") return kit::fmtValue(value, 3) + " < " + kit::fmtValue(limit, 3);
    if (kind == "non_g1_edge") {
        const char* deg = kit::unitText(kit::Unit::deg);
        return kit::fmtQuantity(value, deg, 3) + " > " + kit::fmtQuantity(limit, deg, 3);
    }
    if (kind == "open_edge") return "Bounds one face";
    return "Fails B-rep validation";
}

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
    const bool ips = st.ips();
    {
        kit::FieldGroup g("geo_limits", "Sliver area / perimeter²");
        autoLimitField("min_area", "Min face area", &geometryMinArea_,
                       areaOpts(ips).withTip("Faces with a smaller area are reported."));
        autoLimitField("min_edge", "Min edge length", &geometryMinEdge_,
                       lengthOpts(ips).withTip("Edges shorter than this are reported."));
        autoLimitField("sliver", "Sliver area / perimeter²", &geometrySliverRatio_,
                       kit::FieldOpts().withTip("Faces whose area / perimeter² ratio falls below "
                                                "this are reported as slivers."));
        autoLimitField("gap", "Gap tolerance", &geometryGapTol_,
                       lengthOpts(ips).withTip("Open boundaries whose ends are closer than this "
                                               "are reported as near gaps."));
        kit::fieldCheckbox("g1", "Inspect tangent discontinuities", &geometryDiscontinuities_,
                           kit::FieldOpts().withTip("Also report edges where the adjacent faces "
                                                    "meet at more than 5° (G1 breaks)."));
    }
    kit::dimText("Empty limits use a size-scaled default for each body.");
    if (pending_) return;   // the action footer shows progress
    if (!errorText_.empty()) {
        kit::blockedState("Geometry analysis unavailable", errorText_.c_str());
        return;
    }
    if (!result_.contains("bodies") || !result_["bodies"].is_array()) {
        kit::emptyState("geometry_analysis", "No analysis yet",
                        "Set the limits above, then run the analysis.");
        return;
    }
    static constexpr const char* kinds[] = {
        "brep_invalid", "small_face", "short_edge", "sliver_face",
        "open_edge", "gap", "non_g1_edge"
    };
    static constexpr const char* labels[] = {
        "Invalid B-rep", "Small faces", "Short edges", "Sliver faces",
        "Open edges", "Near gaps", "Non-tangent edges"
    };
    const auto& bodies = result_["bodies"];
    int counts[7] = {};
    int total = 0;
    bool gapScanSkipped = false;
    for (const auto& body : bodies) {
        if (!body.value("gap_scan_complete", true)) gapScanSkipped = true;
        if (!body.contains("findings") || !body["findings"].is_array()) continue;
        for (const auto& row : body["findings"]) {
            const std::string kind = row.value("kind", std::string());
            for (int k = 0; k < 7; ++k)
                if (kind == kinds[k]) { ++counts[k]; ++total; }
        }
    }
    const bool complete = result_.value("complete", false);

    // Outcome first: one wrapping chip line, then the caveats inline.
    kit::chipFlowBegin("geo_outcome");
    if (total > 0) {
        const std::string n = std::to_string(total) + (total == 1 ? " finding" : " findings");
        kit::flowChip(n.c_str(), kit::Tone::Warn, true);
    } else {
        kit::flowChip("No findings", kit::Tone::Ok, true);
    }
    if (!complete) kit::flowChip("Incomplete source", kit::Tone::Warn, true);
    kit::flowValueChip("Bodies", std::to_string(bodies.size()).c_str());
    kit::chipFlowEnd();
    if (!complete)
        kit::inlineStatus("Some source features or assembly components did not compile. "
                          "Findings cover surviving bodies only.", kit::Tone::Warn);
    if (gapScanSkipped)
        kit::inlineStatus("Gap scan skipped: too many open edge endpoints on a body.",
                          kit::Tone::Warn);
    if (total == 0) kit::dimText("The enabled checks found nothing to fix.");

    // Limits the engine actually used (resolved per body), collapsed by default.
    std::vector<kit::KeyValueItem> used;
    for (const auto& body : bodies) {
        if (!body.contains("thresholds") || !body["thresholds"].is_object()) continue;
        const auto& t = body["thresholds"];
        const std::string pre = bodies.size() > 1
            ? "Body " + std::to_string(body.value("body_id", 0)) + " · " : std::string();
        used.push_back({pre + "Min face area", evalQty(ips, EvalQty::Area, t.value("min_face_area", 0.0))});
        used.push_back({pre + "Min edge length", evalLen(st, t.value("min_edge_length", 0.0))});
        used.push_back({pre + "Sliver ratio", kit::fmtValue(t.value("sliver_ratio", 0.0))});
        used.push_back({pre + "Gap tolerance", evalLen(st, t.value("gap_tol", 0.0))});
    }
    if (!used.empty()) kit::keyValueCard("geo_used", "Limits used", nullptr, used, &geometryLimitsOpen_);

    if (total == 0) return;
    kit::sectionHeader("Findings", nullptr, nullptr, total);
    for (int k = 0; k < 7; ++k) {
        if (!counts[k]) continue;
        const std::string title = std::string(labels[k]) + " (" + std::to_string(counts[k]) + ")";
        if (kit::cardBegin(kinds[k], title.c_str(), nullptr, &geometryKindOpen_[k])) {
            int idx = 0;
            for (const auto& body : bodies) {
                if (!body.contains("findings") || !body["findings"].is_array()) continue;
                for (const auto& row : body["findings"]) {
                    if (row.value("kind", std::string()) != kinds[k]) continue;
                    const int bid = row.value("body_id", 0);
                    const int fid = row.value("face_id", 0);
                    const int eid = row.value("edge_id", 0);
                    const std::string entity = "Body " + std::to_string(bid) +
                        (fid ? " · Face " + std::to_string(fid) : "") +
                        (eid ? " · Edge " + std::to_string(eid) : "");
                    const std::string meta = findingMeta(st, kinds[k], row.value("value", 0.0),
                                                         row.value("threshold", 0.0));
                    const bool selectable = bid > 0 && (fid > 0 || eid > 0);
                    kit::RowStyle rs;
                    rs.meta = meta.c_str();
                    rs.tooltip = selectable ? "Select it in the viewport." : nullptr;
                    ImGui::PushID(idx++);
                    if (kit::row("finding", entity.c_str(), rs) && selectable)
                        st.selection.selectOne({static_cast<uint32_t>(bid),
                                                eid ? render::PickKind::Edge : render::PickKind::Face,
                                                static_cast<uint32_t>(eid ? eid : fid)});
                    ImGui::PopID();
                }
            }
        }
        kit::cardEnd();
    }
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
    // Without a custom override the engine applies each body's own material (per-body
    // overrides included); the dialog's seeded density would flatten them to one value.
    if (!massCustomDensity_) params.erase("density_g_cm3");
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
        if (!massCustomDensity_ && r.result.contains("material") &&
            r.result["material"].is_object() &&
            r.result["material"].value("density_g_cm3", 0.0) > 0.0)
            massDensity_ = (float)r.result["material"]["density_g_cm3"].get<double>();
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
                                  evalQty(st.ips(), EvalQty::AreaMoment, Imax).c_str());
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
                                  evalQty(st.ips(), EvalQty::AreaMoment, J).c_str());
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
                              evalQty(st.ips(), EvalQty::AreaMoment, Ixx).c_str());
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
    const auto& items = st.selection.items();
    // Reading first (the result), then the picks that produced it.
    if (!errorText_.empty()) {
        kit::blockedState("Measurement unavailable", errorText_.c_str(),
                          "Remove an unresolved pick and select its geometry again.");
        if (!measureResolving_ && st.selection.size() <= 8 &&
            measureSelection(st).error.empty() &&
            kit::secondaryButton("Retry measurement###measure_retry")) {
            lastMeasureRefs_.clear();
            tickLiveMeasure(st);
        }
    } else if (items.empty()) {
        kit::emptyState("measure", "Ready to measure",
                        "Pick a face, edge, or vertex in the viewport. Ctrl-click adds or removes "
                        "picks: two measure a distance, three to eight measure a chain.");
    } else if (measureResolving_) {
        kit::spinner("Resolving selection…");
        kit::dimText("Waiting for every selected item. No partial reading is shown.");
    } else if (pending_) {
        kit::spinner("Computing measurement…");
    } else if (measureDirty_) {
        kit::spinner("Waiting for the selection to settle…");
    } else {
        // One copy affordance per reading: the header copies every row at once.
        if (kit::cardBegin("reading", "Reading", "measure")) {
            kit::cardHeaderRight(ImGui::GetFrameHeight());
            if (kit::iconButton("copy", "copy", "Copy this reading")) {
                const auto text = measureReadingText(st, result_);
                ImGui::SetClipboardText(text.c_str());
            }
            int r = 0;
            for (const auto& row : measureRows(st, result_)) {
                ImGui::PushID(r++);
                kit::keyValueRowId("v", row.first.c_str(), row.second.c_str());
                ImGui::PopID();
            }
            if (result_.contains("closed") && result_["closed"].is_boolean())
                kit::keyValueRowId("closed", "Closed profile",
                                   result_["closed"].get<bool>() ? "Yes" : "No");
        }
        kit::cardEnd();
    }

    // Picks: one row each (dot = its viewport highlight colour, × removes it), not a card apiece.
    int remove = -1;
    bool clearAll = false;
    if (!items.empty()) {
        const std::string selTitle = "Selection (" + std::to_string(items.size()) + ")";
        if (kit::cardBegin("picks", selTitle.c_str(), "point")) {
            const float lineH = ImGui::GetTextLineHeight();
            const float gap   = ImGui::GetStyle().ItemInnerSpacing.x;
            for (size_t i = 0; i < items.size(); ++i) {
                const auto& item = items[i];
                const char* kind = item.hit.kind == render::PickKind::Face ? "Face" :
                                   item.hit.kind == render::PickKind::Edge ? "Edge" : "Vertex";
                char title[96];
                std::snprintf(title, sizeof(title), "%zu. %s %u · Body %u", i + 1, kind,
                              item.hit.topo_id, item.hit.body_id);
                kit::RowStyle rs;
                rs.secondary = item.resolution_error.empty() && item.pending_resolve
                                   ? "Resolving geometry…" : nullptr;
                // Dot + a text-height remove glyph, so the action reads at the row's own scale.
                rs.actionsW = lineH + gap + lineH;
                ImGui::PushID((int)i);
                ImGui::SetNextItemAllowOverlap();
                kit::row("pick", title, rs);
                const ImVec2 rmin = ImGui::GetItemRectMin(), rmax = ImGui::GetItemRectMax();
                const float  rowH = rmax.y - rmin.y;
                const float  laneX = rmax.x - kit::padX() - rs.actionsW;
                ImGui::SetCursorScreenPos(ImVec2(laneX, rmin.y + (rowH - lineH) * 0.5f));
                evalSwatch(kEvalPalette[i % 10], "This pick's highlight colour in the viewport.");
                ImGui::SetCursorScreenPos(ImVec2(laneX + lineH + gap, rmin.y + (rowH - lineH) * 0.5f));
                if (kit::iconButton("remove", "cancel_x", "Remove this pick", true,
                                    kit::Tone::Neutral, lineH))
                    remove = (int)i;
                ImGui::SetCursorScreenPos(ImVec2(rmin.x, rmax.y));
                ImGui::Dummy(ImVec2(0.0f, 0.0f));
                kit::inlineStatus(item.resolution_error.c_str(), kit::Tone::Err);
                ImGui::PopID();
            }
            clearAll = kit::secondaryButton("Clear selection###measure_clear");
        }
        kit::cardEnd();
    }
    if (clearAll) {
        st.selection.clear();
        tickLiveMeasure(st);
    } else if (remove >= 0) {
        const auto hit = items[(size_t)remove].hit;
        st.selection.toggle(hit);
        tickLiveMeasure(st);
    }

    if (!measureLog_.empty()) {
        // Newest first, one wrapped line each (never elided in a key/value column); the header
        // copies the whole list, the same single copy affordance the Reading card uses.
        const std::string title = "Recent readings (" + std::to_string(measureLog_.size()) + ")";
        if (kit::cardBegin("history", title.c_str(), "history", &measureHistoryOpen_)) {
            kit::cardHeaderRight(ImGui::GetFrameHeight());
            if (kit::iconButton("copy", "copy", "Copy every recent reading")) {
                std::string all;
                for (size_t i = measureLog_.size(); i-- > 0;)
                    all += std::to_string(i + 1) + ". " + measureLog_[i] + "\n";
                ImGui::SetClipboardText(all.c_str());
            }
            for (size_t i = measureLog_.size(); i-- > 0;) {
                const std::string line = std::to_string(i + 1) + ". " + measureLog_[i];
                kit::dimText(line.c_str());
            }
            if (kit::secondaryButton("Clear history###measure_clear_log")) measureLog_.clear();
        }
        kit::cardEnd();
    }
}

// Live re-seed from the doc while Mass Properties is open (FeatureManager / sim may have changed
// the material). Runs before the window draws; a custom density override is never clobbered.
void EvaluateDialogs::syncMassMaterial(AppState& st) {
    if (!massOpen_) return;
    std::string liveKey;
    if (st.docMirror.hasDoc()) {
        const auto& tree = st.docMirror.tree();
        if (tree.is_object() && tree.contains("material") && tree["material"].is_object())
            liveKey = tree["material"].value("key", std::string());
    }
    if (liveKey == massDocKey_) return;
    massDocKey_ = liveKey;
    massDocName_ = materialDisplayName(liveKey);
    if (massCustomDensity_) return;
    massDensity_ = liveKey.empty() ? 7.85f : materialDensityGcm3(liveKey);
    if (!massPending_) runMassEval(st, {{"density_g_cm3", (double)massDensity_}});
}

void EvaluateDialogs::drawMassWindow(AppState& st) {
    if (!massOpen_) return;

    const ImVec2 massSize(ui::metrics::px(480), ui::metrics::px(620));
    ui::setNextCommandDialogPlacement("Mass Properties##eval_mass", massSize,
        ImVec2(ui::metrics::px(340), ui::metrics::px(260)));
    {
        const std::string exclusive =
            openWindow_.empty() ? std::string() : evalExclusiveWindowName(title_, openWindow_);
        evalPlaceAppearing("Mass Properties##eval_mass", massSize,
                           {"Section Properties##eval_section",
                            exclusive.empty() ? nullptr : exclusive.c_str()});
    }
    bool open = true;
    ImGui::SetNextWindowBgAlpha(1.0f);
    const bool vis = ImGui::Begin("Mass Properties##eval_mass", &open, ImGuiWindowFlags_NoScrollbar);
    if (vis) {
        const auto surf = ui::adaptiveSurface(-1.0f);
        kit::panelBegin("eval_mass", &surf);
        kit::panelBodyBegin();
        // Material combo lists curated physical materials (same keys as FeatureManager Material).
        // Selecting one WRITES set_material (+ derived set_appearance) so mass, sim, and the tree
        // all share ONE active part material. Density follows that key unless the user has set an
        // explicit advanced density override below.
        auto applyMassMaterial = [&](const std::string& key) {
            if (!st.docMirror.hasDoc() || !st.workerStarted) return;
            if (st.docLockedByAgent(st.docMirror.docId())) {
                st.toast(ui::ToastLevel::Warn, st.agentLockReason(st.docMirror.docId()));
                return;
            }
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

        // Density is stored and sent in g/cm³ (the eval RPC's density_g_cm3), but Volume and Mass
        // honour st.ips(); an IPS document edits and reads density in lb/in³ so one panel never
        // mixes unit systems. The wire value stays metric.
        constexpr double kLbPerIn3PerGcm3 = 0.0361272929;
        const bool ips = st.ips();
        const char* densityUnit = kit::unitText(ips ? kit::Unit::lb_in3 : kit::Unit::g_cm3);
        // Material + density share one card so the inputs sit on the same surface as the results.
        if (kit::cardBegin("mass_material", "Material", "material")) {
            kit::FieldGroup g("mass_material", "Material");
            static const std::vector<std::string> kNames = [] {
                std::vector<std::string> v;
                for (int i = 0; i < kPhysicalMaterialsCount; ++i) v.emplace_back(kPhysicalMaterials[i].name);
                return v;
            }();
            int sel = -1;
            for (int i = 0; i < kPhysicalMaterialsCount; ++i)
                if (massDocKey_ == kPhysicalMaterials[i].key) sel = i;
            kit::FieldOpts mo = kit::FieldOpts().withHint("Not set").withTip(
                "The part material. Choosing one updates the part, so mass, simulation and the "
                "feature tree all use it.");
            if (sel < 0 && !massDocKey_.empty()) mo.withPreview(massDocName_.c_str());
            if (kit::fieldCombo("material", "Material", &sel, kNames, mo) && sel >= 0 &&
                sel < kPhysicalMaterialsCount)
                applyMassMaterial(kPhysicalMaterials[sel].key);

            double density = massDensity_;
            if (kit::fieldNumber("density", "Density", &density,
                                 kit::FieldOpts()
                                     .withUnit(densityUnit)
                                     .withScale(ips ? kLbPerIn3PerGcm3 : 1.0)
                                     .withRange(0.0, 25.0)
                                     .withTip("Overrides the density for this readout only. The "
                                              "part material is unchanged."))) {
                massDensity_ = (float)density;
                massCustomDensity_ = true;
                runMassEval(st, {{"density_g_cm3", (double)massDensity_}});
            }
            if (massCustomDensity_) {
                kit::inlineStatus("Custom density. The part material is unchanged.", kit::Tone::Info);
                if (!massDocKey_.empty() &&
                    kit::secondaryButton("Use material density###mass_reset_density")) {
                    massCustomDensity_ = false;
                    massDensity_ = materialDensityGcm3(massDocKey_);
                    runMassEval(st, {{"density_g_cm3", (double)massDensity_}});
                }
            }
        }
        kit::cardEnd();

        if (!massErrorText_.empty()) {
            kit::blockedState("Mass properties unavailable", massErrorText_.c_str());
        } else if (massPending_) {
            kit::spinner("Computing mass properties…");
        } else {
            // Density is the editable field above; Totals carries only what was computed.
            const double volume = massResult_.value("volume_mm3", 0.0);
            kit::keyValueCard("mass_totals", "Totals", "mass_props", {
                {"Volume", evalQty(ips, EvalQty::Volume, volume), kit::Tone::Neutral, true},
                {"Mass", evalQty(ips, EvalQty::Mass, massResult_.value("mass_g", 0.0)),
                 kit::Tone::Neutral, true},
            });
            if (massResult_.contains("com") && massResult_["com"].is_array() &&
                massResult_["com"].size() == 3) {
                if (kit::cardBegin("mass_com", "Center of gravity", "origin")) {
                    // A CG on a symmetry plane comes back as floating-point residue; read it
                    // against the part's size so it shows as 0.
                    const double partSize = std::cbrt(std::fabs(volume));
                    static const char* const kAxes[] = {"X", "Y", "Z"};
                    std::string vals[3];
                    for (int i = 0; i < 3; ++i) {
                        const auto& c = massResult_["com"][i];
                        vals[i] = evalLen(st, c.is_number() ? c.get<double>() : 0.0, partSize);
                    }
                    // One copy affordance for the whole point (not one per axis row).
                    kit::cardHeaderRight(ImGui::GetFrameHeight());
                    if (kit::iconButton("copy", "copy", "Copy the center of gravity")) {
                        const std::string text = "X: " + vals[0] + " \xc2\xb7 Y: " + vals[1] +
                                                 " \xc2\xb7 Z: " + vals[2];
                        ImGui::SetClipboardText(text.c_str());
                    }
                    for (int i = 0; i < 3; ++i) {
                        ImGui::PushID(i);
                        kit::keyValueRowId("c", kAxes[i], vals[i].c_str());
                        ImGui::PopID();
                    }
                    if (kit::fieldCheckbox("show_com", "Show in viewport", &showCom_,
                                           kit::FieldOpts().withTip(
                                               "Mark the center of gravity in the viewport.")))
                        rebuildMassOverlays(st);
                }
                kit::cardEnd();
            }

            // Mass moment of inertia: the engine's inertia tensor is central (OCCT
            // MatrixOfInertia is taken at the center of mass, model-axis aligned) and its
            // eigenvalues are the principal moments about the CG — the number an engineer reads
            // for rotational dynamics. Mass units follow the document (lb·in² / g·mm²).
            if (massResult_.contains("principal_moments") &&
                massResult_["principal_moments"].is_array() &&
                massResult_["principal_moments"].size() == 3) {
                if (kit::cardBegin("mass_moi", "Moment of inertia", "mass_props")) {
                    const auto& Pm = massResult_["principal_moments"];
                    std::string vals[3];
                    for (int i = 0; i < 3; ++i)
                        vals[i] = evalQty(ips, EvalQty::MassMoment,
                                          Pm[i].is_number() ? Pm[i].get<double>() : 0.0);
                    kit::cardHeaderRight(ImGui::GetFrameHeight());
                    if (kit::iconButton("copy", "copy", "Copy the principal moments of inertia")) {
                        const std::string text = "P1: " + vals[0] + " \xc2\xb7 P2: " + vals[1] +
                                                 " \xc2\xb7 P3: " + vals[2];
                        ImGui::SetClipboardText(text.c_str());
                    }
                    static const char* const kPrincipal[] = {"Principal axis 1", "Principal axis 2",
                                                             "Principal axis 3"};
                    for (int i = 0; i < 3; ++i) {
                        ImGui::PushID(i);
                        kit::keyValueRowId("p", kPrincipal[i], vals[i].c_str());
                        ImGui::PopID();
                    }
                    kit::dimText("Principal mass moments about the center of gravity.");
                }
                kit::cardEnd();
            }

            // Section properties: AREA moments (bending I + polar J) of the cut through the CG
            // normal to each principal axis — geometry stiffness, not mass inertia. Numbers
            // first, then what to show.
            if (kit::cardBegin("mass_section", "Section properties", "section_props")) {
                if (designSectionsPending_ > 0) kit::spinner("Computing section I and J…");
                bool firstAxis = true;
                for (int i = 0; designSections_.is_array() && i < 3; ++i) {
                    if ((size_t)i >= designSections_.size() || !designSections_[i].is_object())
                        continue;
                    const auto& sec = designSections_[i];
                    const std::string axis = "Principal axis " + std::to_string(i + 1);
                    ImGui::PushID(i);
                    if (!firstAxis) kit::rule();
                    firstAxis = false;
                    if (sec.contains("error")) {
                        const std::string msg = axis + ": " +
                            (sec["error"].is_string() ? sec["error"].get<std::string>()
                                                      : sec["error"].dump());
                        kit::inlineStatus(msg.c_str(), kit::Tone::Err);
                        ImGui::PopID();
                        continue;
                    }
                    const double Ixx = sec.value("Ixx", 0.0), Iyy = sec.value("Iyy", 0.0);
                    const double J = sec.value("J", Ixx + Iyy);
                    ImGui::TextUnformatted(axis.c_str());
                    if (showI_) {
                        kit::keyValueRowId("imax", "Area moment Imax",
                            evalQty(ips, EvalQty::AreaMoment, std::max(Ixx, Iyy)).c_str());
                        kit::keyValueRowId("imin", "Area moment Imin",
                            evalQty(ips, EvalQty::AreaMoment, std::min(Ixx, Iyy)).c_str());
                    }
                    if (showJ_)
                        kit::keyValueRowId("j", "Polar moment J",
                                           evalQty(ips, EvalQty::AreaMoment, J).c_str());
                    if (showK_ && sec.contains("EA") && sec["EA"].is_number())
                        kit::keyValueRowId("ea", "EA (k = EA / L)",
                            evalQty(ips, EvalQty::Force, sec["EA"].get<double>()).c_str());
                    if (sec.contains("area_mm2"))
                        kit::keyValueRowId("area", "Section area",
                            evalQty(ips, EvalQty::Area, sec.value("area_mm2", 0.0)).c_str());
                    ImGui::PopID();
                }
                kit::dimText("Area moments of the section through the center of gravity, normal "
                             "to each principal axis. I resists bending; polar J resists torsion.");
                bool overlays = false;
                kit::chipFlowBegin("mass_moi_show");
                overlays |= kit::flowToggleChip("Bending I", &showI_, kit::Tone::Accent,
                                                "Show Imax / Imin here and as viewport chips.");
                overlays |= kit::flowToggleChip("Polar J", &showJ_, kit::Tone::Accent,
                                                "Show polar J here and as viewport chips.");
                overlays |= kit::flowToggleChip("Axial stiffness EA", &showK_, kit::Tone::Accent,
                                                "Show EA (k = EA / L for a member of length L).");
                kit::chipFlowEnd();
                if (overlays) rebuildMassOverlays(st);
            }
            kit::cardEnd();

            if (kit::cardBegin("mass_tensor", "Mass inertia tensor", "csys", &massTensorOpen_)) {
                // Rows first, so the header's one copy action carries the whole tensor. The
                // principal moments live in the "Moment of inertia" card above.
                std::vector<std::pair<std::string, std::string>> tensor;
                if (massResult_.contains("inertia") && massResult_["inertia"].is_array() &&
                    massResult_["inertia"].size() == 9) {
                    const auto& I = massResult_["inertia"];
                    // Off-diagonal residue reads against the largest diagonal term.
                    const double ref = std::max({std::fabs(I[0].get<double>()),
                                                 std::fabs(I[4].get<double>()),
                                                 std::fabs(I[8].get<double>())});
                    static const char* const kKeys[] = {"Ixx", "Iyy", "Izz", "Ixy", "Ixz", "Iyz"};
                    static const int kIdx[] = {0, 4, 8, 1, 2, 5};
                    for (int r = 0; r < 6; ++r)
                        tensor.emplace_back(kKeys[r], evalQty(ips, EvalQty::MassMoment,
                                                              I[kIdx[r]].get<double>(), ref));
                }
                if (!tensor.empty()) {
                    kit::cardHeaderRight(ImGui::GetFrameHeight());
                    if (kit::iconButton("copy", "copy", "Copy the inertia tensor")) {
                        std::string text;
                        for (const auto& kv : tensor) {
                            if (!text.empty()) text += " \xc2\xb7 ";
                            text += kv.first + ": " + kv.second;
                        }
                        ImGui::SetClipboardText(text.c_str());
                    }
                    // The engine reports the central tensor (taken at the center of mass, axes
                    // parallel to the model X/Y/Z).
                    kit::dimText("About the center of gravity, aligned with the model axes.");
                    for (int r = 0; r < (int)tensor.size(); ++r) {
                        ImGui::PushID(r);
                        kit::keyValueRowId("t", tensor[r].first.c_str(), tensor[r].second.c_str());
                        ImGui::PopID();
                    }
                } else {
                    kit::dimText("No inertia tensor was reported for this part.");
                }
            }
            kit::cardEnd();
        }
        kit::panelEnd();
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

    const ImVec2 sectionSize(ui::metrics::px(480), ui::metrics::px(640));
    ui::setNextCommandDialogPlacement("Section Properties##eval_section", sectionSize,
        ImVec2(ui::metrics::px(340), ui::metrics::px(280)));
    {
        const std::string exclusive =
            openWindow_.empty() ? std::string() : evalExclusiveWindowName(title_, openWindow_);
        evalPlaceAppearing("Section Properties##eval_section", sectionSize,
                           {"Mass Properties##eval_mass",
                            exclusive.empty() ? nullptr : exclusive.c_str()});
    }
    bool open = true;
    ImGui::SetNextWindowBgAlpha(1.0f);
    const bool vis = ImGui::Begin("Section Properties##eval_section", &open, ImGuiWindowFlags_NoScrollbar);
    if (vis) {
        const auto surf = ui::adaptiveSurface(-1.0f);
        kit::panelBegin("eval_section", &surf);
        kit::panelBodyBegin();
        const bool ips = st.ips();
        kit::inlineStatus(sectionErrorText_.c_str(), kit::Tone::Err);
        kit::dimText("Drag a plane in the viewport or type an offset from the centroid. Each "
                     "card's dot is its plane's colour in the viewport.");

        // One neutral card per orthogonal section; the header dot matches the viewport plane tint
        // and the header's one copy action carries every row of the section.
        for (int i = 0; i < 3; ++i) {
            char title[64];
            std::snprintf(title, sizeof(title), "Section %d · %s", i + 1, kSectionNames[i]);
            const nlohmann::json* sec =
                sectionResults_.is_array() && (size_t)i < sectionResults_.size() &&
                        sectionResults_[i].is_object()
                    ? &sectionResults_[i]
                    : nullptr;
            const bool secError = sec && sec->contains("error") && (*sec)["error"].is_string();
            const std::string secErrorText = secError ? (*sec)["error"].get<std::string>() : "";
            std::vector<std::pair<std::string, std::string>> rows;
            if (sec && !secError) {
                rows.emplace_back("Area", evalQty(ips, EvalQty::Area, sec->value("area_mm2", 0.0)));
                if (sec->contains("centroid") && (*sec)["centroid"].is_array() &&
                    (*sec)["centroid"].size() == 3) {
                    static const char* const kLabels[] = {"Centroid X", "Centroid Y", "Centroid Z"};
                    for (int axis = 0; axis < 3; ++axis) {
                        const auto& c = (*sec)["centroid"][axis];
                        // Read against the part size: residue on a symmetry plane is 0.
                        rows.emplace_back(kLabels[axis],
                                          evalLen(st, c.is_number() ? c.get<double>() : 0.0,
                                                  sectionHalfSize_));
                    }
                }
                const double Ixx = sec->value("Ixx", 0.0);
                const double Iyy = sec->value("Iyy", 0.0);
                const double Ixy = sec->value("Ixy", 0.0);
                const double J = sec->value("J", Ixx + Iyy);
                // The product moment of a symmetric section is residue against Ixx/Iyy.
                const double ref = std::max(std::fabs(Ixx), std::fabs(Iyy));
                rows.emplace_back("Ixx", evalQty(ips, EvalQty::AreaMoment, Ixx));
                rows.emplace_back("Iyy", evalQty(ips, EvalQty::AreaMoment, Iyy));
                rows.emplace_back("Ixy", evalQty(ips, EvalQty::AreaMoment, Ixy, ref));
                rows.emplace_back("Polar J", evalQty(ips, EvalQty::AreaMoment, J));
            }
            ImGui::PushID(i);
            if (kit::cardBegin("section", title, "section_props")) {
                const float lineH = ImGui::GetTextLineHeight();
                const float gap   = ImGui::GetStyle().ItemInnerSpacing.x;
                kit::cardHeaderRight(lineH + (rows.empty() ? 0.0f : gap + ImGui::GetFrameHeight()));
                evalSwatch(kEvalPalette[i % 10], "This section plane's colour in the viewport.");
                if (!rows.empty()) {
                    ImGui::SameLine(0.0f, gap);
                    if (kit::iconButton("copy", "copy", "Copy this section's properties")) {
                        std::string text;
                        for (const auto& kv : rows) {
                            if (!text.empty()) text += " \xc2\xb7 ";
                            text += kv.first + ": " + kv.second;
                        }
                        ImGui::SetClipboardText(text.c_str());
                    }
                }
                {
                    kit::FieldGroup g("plane", "Offset");
                    float off = sectionOffset_[i];
                    if (kit::fieldNumber("offset", "Offset", &off,
                            lengthOpts(ips)
                                .withRange(-sectionHalfSize_, sectionHalfSize_)
                                .withTip("Distance of the cutting plane from the part centroid "
                                         "along its normal."))) {
                        applySectionOffset(st, i, off, true);
                        sectionDragLastT_ = ImGui::GetTime() - 1.0;   // typed: evaluate now
                    }
                }
                if (sectionOffset_[i] != 0.0f &&
                    kit::secondaryButton("Reset to centroid###reset")) {
                    applySectionOffset(st, i, 0.0f, true);
                    sectionDragLastT_ = ImGui::GetTime() - 1.0;
                }
                if (secError) {
                    kit::inlineStatus(secErrorText.c_str(), kit::Tone::Err);
                } else if (!rows.empty()) {
                    for (int r = 0; r < (int)rows.size(); ++r) {
                        ImGui::PushID(r);
                        kit::keyValueRowId("v", rows[r].first.c_str(), rows[r].second.c_str());
                        ImGui::PopID();
                    }
                } else if (sectionPendingCount_ > 0 || sectionPendingIdx_ == i) {
                    kit::spinner("Computing this section…");
                } else {
                    kit::dimText("No section data. Move the plane to evaluate it.");
                }
            }
            kit::cardEnd();
            ImGui::PopID();
        }
        kit::panelEnd();
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

void EvaluateDialogs::drawCheck(AppState&) {
    if (pending_) {
        kit::spinner("Checking geometry…");
        return;
    }
    if (!errorText_.empty()) {
        kit::blockedState("Geometry check unavailable", errorText_.c_str());
        return;
    }
    if (!result_.contains("issues") || !result_["issues"].is_array()) {
        kit::emptyState("check", "No check result", "Run Check from the Evaluate tab to inspect the part.");
        return;
    }
    const auto& issues = result_["issues"];
    auto toneOf = [](const std::string& sev) {
        if (sev == "error") return kit::Tone::Err;
        if (sev == "warning" || sev == "warn") return kit::Tone::Warn;
        return kit::Tone::Info;
    };
    kit::Tone worst = kit::Tone::Info;
    for (const auto& it : issues) {
        const kit::Tone t = toneOf(it.value("severity", std::string("info")));
        if (t == kit::Tone::Err || (t == kit::Tone::Warn && worst != kit::Tone::Err)) worst = t;
    }
    kit::chipFlowBegin("check_outcome");
    if (issues.empty()) {
        kit::flowChip("No geometry issues", kit::Tone::Ok, true);
    } else {
        const std::string n = std::to_string(issues.size()) + (issues.size() == 1 ? " issue" : " issues");
        kit::flowChip(n.c_str(), worst, true);
    }
    kit::chipFlowEnd();
    if (issues.empty()) {
        kit::dimText("The part passed every geometry check.");
        return;
    }
    for (const auto& it : issues)
        kit::inlineStatus(it.value("text", std::string()).c_str(),
                          toneOf(it.value("severity", std::string("info"))));
}

void EvaluateDialogs::drawInterference(AppState& st) {
    const bool ips = st.ips();
    if (!errorText_.empty()) kit::blockedState("Interference unavailable", errorText_.c_str());
    if (!interferencePartNames_.empty()) {
        const size_t n = std::min(interferencePartNames_.size(), interferencePartChecked_.size());
        size_t included = 0;
        for (size_t i = 0; i < n; ++i) included += interferencePartChecked_[i] ? 1 : 0;
        const std::string title =
            "Members (" + std::to_string(included) + " of " + std::to_string(n) + ")";
        bool anyChange = false;
        if (kit::cardBegin("members", title.c_str())) {
            for (size_t i = 0; i < n; ++i) {
                bool checked = interferencePartChecked_[i] != 0;
                ImGui::PushID((int)i);
                if (kit::checkbox("member", interferencePartNames_[i].c_str(), &checked)) {
                    interferencePartChecked_[i] = checked ? 1 : 0;
                    anyChange = true;
                }
                ImGui::PopID();
            }
        }
        kit::cardEnd();
        if (anyChange) {
            nlohmann::json members = nlohmann::json::array();
            for (size_t i = 0; i < n; ++i)
                if (interferencePartChecked_[i]) members.push_back(interferencePartNames_[i]);
            if (members.empty()) {
                ++gen_;
                pending_ = false;
                result_ = nlohmann::json::object();
                errorText_ = "Select at least one member to check interference.";
                clearOverlays();
            } else {
                runEval(st, "eval_interference", {{"members", members}});
            }
        }
    }
    if (pending_) {
        kit::spinner("Checking interference…");
        return;
    }
    if (result_.empty()) {
        if (errorText_.empty())
            kit::emptyState("interference_eval", "Nothing to check",
                            "Interference detection runs across assembly components or multi-body "
                            "parts. Open an assembly or a multi-body part to check clashes.");
        return;
    }

    const bool hasPairs = result_.contains("interferences") && result_["interferences"].is_array() &&
                          !result_["interferences"].empty();
    const bool hasClear = result_.contains("clearances") && result_["clearances"].is_array() &&
                          !result_["clearances"].empty();
    const bool complete = result_.value("complete", false);
    const int pairsChecked = result_.value("pairs_checked", 0);

    // Outcome first: one wrapping chip line, then the summary.
    kit::chipFlowBegin("intf_outcome");
    if (hasPairs) {
        const size_t c = result_["interferences"].size();
        const std::string clashes = std::to_string(c) + (c == 1 ? " clash" : " clashes");
        kit::flowChip(clashes.c_str(), kit::Tone::Err, true);
    } else if (complete && result_.value("ok", false) && pairsChecked > 0 && errorText_.empty()) {
        kit::flowChip("No interference", kit::Tone::Ok, true);
    }
    if (!complete) kit::flowChip("Incomplete", kit::Tone::Warn, true);
    if (pairsChecked > 0) kit::flowValueChip("Pairs checked", std::to_string(pairsChecked).c_str());
    kit::chipFlowEnd();
    if (result_.contains("summary")) kit::dimText(result_.value("summary", std::string()).c_str());

    if (!complete) {
        kit::inlineStatus("Some geometry or pair checks failed. Known clashes below remain valid; "
                          "this report cannot establish clearance.", kit::Tone::Warn);
        for (const char* key : {"geometry_failures", "pair_failures"}) {
            if (!result_.contains(key) || !result_[key].is_array()) continue;
            std::vector<kit::KeyValueItem> rows;
            for (const auto& failure : result_[key]) {
                if (!failure.is_object()) continue;
                std::string subject = failure.value("name",
                    failure.value("component", failure.value("feature", std::string())));
                if (failure.contains("a") && failure.contains("b"))
                    subject = failure["a"].is_string() ? failure["a"].get<std::string>() : failure["a"].dump();
                if (failure.contains("b"))
                    subject += " / " + (failure["b"].is_string() ? failure["b"].get<std::string>() : failure["b"].dump());
                rows.push_back({subject.empty() ? "Geometry" : subject,
                                failure.value("reason", std::string("Analysis failed")),
                                kit::Tone::Err});
            }
            kit::keyValueCard(key, key[0] == 'g' ? "Geometry failures" : "Pair failures", nullptr, rows);
        }
    }
    if (hasPairs) {
        const auto& pairs = result_["interferences"];
        kit::sectionHeader("Interferences", nullptr, nullptr, (int)pairs.size());
        int pi = 0;
        for (const auto& p : pairs) {
            const std::string a = p.value("a", std::string("?"));
            const std::string b = p.value("b", std::string("?"));
            const double vol = p.value("overlap_volume_mm3",
                                       p.value("volume", p.value("volume_mm3", 0.0)));
            std::string bboxTxt;
            if (p.contains("overlap_bbox") && p["overlap_bbox"].is_array() &&
                p["overlap_bbox"].size() == 2 && p["overlap_bbox"][0].is_array() &&
                p["overlap_bbox"][1].is_array() && p["overlap_bbox"][0].size() >= 3 &&
                p["overlap_bbox"][1].size() >= 3) {
                const auto& lo = p["overlap_bbox"][0];
                const auto& hi = p["overlap_bbox"][1];
                bboxTxt = evalLen(st, hi[0].get<double>() - lo[0].get<double>()) + " × " +
                          evalLen(st, hi[1].get<double>() - lo[1].get<double>()) + " × " +
                          evalLen(st, hi[2].get<double>() - lo[2].get<double>());
            }
            const std::string title = a + " × " + b;
            // Index-scoped: patterned parts produce pairs with identical names and volumes.
            ImGui::PushID(pi);
            if (kit::cardBegin("pair", title.c_str())) {
                // Header dots = the two bodies' viewport tints (the overlap itself is red).
                kit::cardHeaderRight(ImGui::GetTextLineHeight() * 2.0f);
                evalSwatch(kEvalPalette[(pi * 2) % 10], "First body's colour in the viewport.");
                ImGui::SameLine(0.0f, 0.0f);
                evalSwatch(kEvalPalette[(pi * 2 + 1) % 10], "Second body's colour in the viewport.");
                kit::keyValueRowId("vol", "Overlap volume",
                                   evalQty(ips, EvalQty::Volume, vol).c_str(),
                                   kit::Tone::Err, true);
                if (!bboxTxt.empty())
                    kit::keyValueRowId("bbox", "Overlap extent", bboxTxt.c_str(), kit::Tone::Neutral, true);
            }
            kit::cardEnd();
            ImGui::PopID();
            ++pi;
        }
    }
    if (hasClear) {
        std::vector<kit::KeyValueItem> rows;
        for (const auto& p : result_["clearances"]) {
            std::string reading = "Exact gap not measured";
            if (p.contains("distance_lower_bound_mm") && p["distance_lower_bound_mm"].is_number()) {
                reading = ">= " + evalLen(st, p["distance_lower_bound_mm"].get<double>()) +
                          " (lower bound)";
            } else if (p.contains("distance") && p["distance"].is_number()) {
                reading = evalLen(st, p["distance"].get<double>());
                if (p.value("approx", false)) reading = ">= " + reading + " (lower bound)";
            }
            rows.push_back({p.value("a", std::string("?")) + " / " + p.value("b", std::string("?")),
                            reading, kit::Tone::Neutral, true});
        }
        kit::keyValueCard("clearances", "Clearances", nullptr, rows);
    }
    if (!hasPairs && !hasClear && !result_.contains("summary") && pairsChecked == 0)
        kit::emptyState("interference_eval", "Nothing to check",
                        "Interference detection runs across assembly components or multi-body "
                        "parts. Open an assembly or a multi-body part to check clashes.");
}

void EvaluateDialogs::drawSymmetry(AppState& st) {
    {
        kit::FieldGroup g("symmetry", "Mirror plane");
        static const char* const kPlanes[] = {"Front", "Top", "Right"};
        if (kit::fieldEnum("plane", "Mirror plane", &symmetryPlane_, kPlanes, 3,
                           kit::FieldOpts().withTip("The datum plane the part is mirrored across."))) {
            static const int map[] = {1, 0, 2};
            const int pi = (symmetryPlane_ >= 0 && symmetryPlane_ < 3) ? map[symmetryPlane_] : 1;
            runEval(st, "eval_symmetry", {{"plane", kPlaneNames[pi]}});
        }
    }
    if (pending_) {
        kit::spinner("Checking symmetry…");
    } else if (!errorText_.empty()) {
        kit::blockedState("Symmetry check unavailable", errorText_.c_str());
    } else if (result_.contains("symmetric")) {
        const bool sym = result_.value("symmetric", false);
        kit::chipFlowBegin("sym_outcome");
        kit::flowChip(sym ? "Symmetric" : "Not symmetric", sym ? kit::Tone::Ok : kit::Tone::Warn, true);
        kit::chipFlowEnd();
        kit::keyValueRowId("deviation", "Deviation volume",
                           evalQty(st.ips(), EvalQty::Volume, result_.value("deviation_mm3", 0.0)).c_str(),
                           kit::Tone::Neutral, true);
    } else {
        kit::emptyState("symmetry_check", "No result yet", "Choose a mirror plane to check the part.");
    }
}

void EvaluateDialogs::drawZebra(AppState& st) {
    kit::dimText("Even, smooth stripes mean a fair surface. A kink marks a G1 break; a jog marks "
                 "a G2 break.");
    kit::FieldGroup g("zebra", "Stripe density");
    const float before = gZebraFreq;
    kit::fieldSlider("density", "Stripe density", &gZebraFreq, 4.0f, 60.0f,
                     kit::FieldOpts().withSigFigs(2).withTip(
                         "Stripes across the reflection. Higher shows finer curvature changes."));
    if (gZebraFreq != before && st.renderer) st.renderer->setZebra(true, gZebraFreq);
}

void EvaluateDialogs::drawFieldTool(AppState& st, const std::string& kind) {
    kit::inlineStatus(errorText_.c_str(), kit::Tone::Err);
    {
        kit::FieldGroup g("field_params", "Pull direction");
        if (kind == "draft") {
            static const char* const kPulls[] = {"+X", "+Y", "+Z", "-Z"};
            kit::fieldEnum("pull", "Pull direction", &draftPull_, kPulls, 4,
                           kit::FieldOpts().withTip("Direction the part is pulled from the mold."));
            kit::fieldNumber("angle", "Draft angle", &draftAngleDeg_,
                             kit::FieldOpts()
                                 .withUnit(kit::Unit::deg)
                                 .withRange(0.0, 45.0)
                                 .withTip("Faces with less draft than this against the pull "
                                          "direction are highlighted."));
        } else if (kind == "thickness") {
            kit::fieldNumber("resolution", "Resolution", &thicknessRes_,
                             lengthOpts(st.ips())
                                 .withRange(0.05, 5.0)
                                 .withTip("Sample spacing for the wall-thickness rays. Smaller is "
                                          "finer and slower."));
        } else {
            static const char* const kKinds[] = {"Mean", "Gaussian", "Max", "Min"};
            kit::fieldEnum("kind", "Curvature", &curvatureKind_, kKinds, 4,
                           kit::FieldOpts().withTip("Which curvature the colormap shows."));
        }
    }
    kit::dimText("Update recomputes the viewport colormap; the legend shows the value range.");
}

void EvaluateDialogs::runFieldTool(AppState& st, const std::string& kind) {
    if (kind == "draft") {
        static const double kPull[4][3] = {{1, 0, 0}, {0, 1, 0}, {0, 0, 1}, {0, 0, -1}};
        const int pi = (draftPull_ >= 0 && draftPull_ < 4) ? draftPull_ : 2;
        runFieldEval(st, "eval_draft",
                     {{"pull", {kPull[pi][0], kPull[pi][1], kPull[pi][2]}},
                      {"angle_deg", (double)draftAngleDeg_}},
                     "Draft angle", "deg");
    } else if (kind == "thickness") {
        nlohmann::json extra = nlohmann::json::object();
        if (thicknessRes_ > 0.0f) extra["resolution"] = (double)thicknessRes_;
        runFieldEval(st, "eval_thickness", extra, "Wall thickness", st.ips() ? "in" : "mm");
    } else if (kind == "curvature") {
        static const char* const kKinds[] = {"mean", "gaussian", "max", "min"};
        static const char* const kTitles[] = {"Mean curvature", "Gaussian curvature",
                                              "Max curvature", "Min curvature"};
        const int ki = (curvatureKind_ >= 0 && curvatureKind_ < 4) ? curvatureKind_ : 0;
        runFieldEval(st, "eval_curvature", {{"kind", kKinds[ki]}}, kTitles[ki], "1/mm");
    }
}

// The tool window's single primary action (pinned footer). Run / Update are primary; Turn off is
// secondary. Evaluations run to completion on the worker, so the running state explains why there
// is no Cancel instead of offering a fake one.
void EvaluateDialogs::drawExclusiveFooter(AppState& st, const std::string& kind) {
    kit::ActionSpec a;
    a.running = pending_;
    a.cancellable = false;
    a.cancelBlocker = "the analysis finishes on the worker. Close this window to discard its result.";
    if (kind == "zebra") {
        a.primaryLabel = "Turn off";
        a.primaryAsSecondary = true;
        a.running = false;
        if (kit::actionFooter("zebra", a) == kit::FooterAction::Primary) {
            if (st.renderer) st.renderer->setZebra(false, gZebraFreq);
            openWindow_.clear();
        }
        return;
    }
    if (kind == "geometry") {
        a.primaryLabel = "Run analysis";
        a.primaryEnabled = !pending_;
        a.stage = "Analyzing geometry";
        if (kit::actionFooter("geometry", a) == kit::FooterAction::Primary) runGeometryAnalysis(st);
        return;
    }
    a.primaryLabel = "Update";
    a.primaryEnabled = !pending_;
    a.stage = kind == "draft" ? "Computing draft" : kind == "thickness" ? "Computing thickness"
                                                                        : "Computing curvature";
    a.secondaryLabel = "Turn off";
    switch (kit::actionFooter("field", a)) {
        case kit::FooterAction::Primary:
            runFieldTool(st, kind);
            break;
        case kit::FooterAction::Secondary:
            clearFieldColormap(st);
            openWindow_.clear();
            break;
        default:
            break;
    }
}

void EvaluateDialogs::drawExclusiveWindow(AppState& st) {
    if (openWindow_.empty()) return;

    bool open = true;
    const std::string winTitle = evalExclusiveWindowName(title_, openWindow_);
    // Measure's reading + picks fit ~420 px (a long chain scrolls); interference lists pairs.
    const ImVec2 toolSize(ui::metrics::px(480),
                          ui::metrics::px(openWindow_ == "interference" ? 580
                                          : openWindow_ == "measure"    ? 420
                                                                        : 380));
    ui::setNextCommandDialogPlacement(winTitle.c_str(), toolSize,
        ImVec2(ui::metrics::px(340), ui::metrics::px(240)));
    evalPlaceAppearing(winTitle.c_str(), toolSize,
                       {"Mass Properties##eval_mass", "Section Properties##eval_section"});
    ImGui::SetNextWindowBgAlpha(1.0f);
    const bool evVis = ImGui::Begin(winTitle.c_str(), &open, ImGuiWindowFlags_NoScrollbar);
    if (evVis) {
        const auto surf = ui::adaptiveSurface(-1.0f);
        // A footer action may close the tool mid-frame; the layout keys off the tool that opened it.
        const std::string kind = openWindow_;
        kit::panelBegin("eval_tool", &surf);
        kit::panelBodyBegin(kind.c_str());
        if (kind == "measure") {
            drawMeasure(st);
        } else if (kind == "geometry") {
            drawGeometryAnalysis(st);
        } else if (kind == "check") {
            drawCheck(st);
        } else if (kind == "interference") {
            drawInterference(st);
        } else if (kind == "symmetry") {
            drawSymmetry(st);
        } else if (kind == "zebra") {
            drawZebra(st);
        } else if (kind == "draft" || kind == "thickness" || kind == "curvature") {
            drawFieldTool(st, kind);
        } else if (kind == "field_wait") {
            kit::spinner("Computing field overlay…");
        } else if (kind == "field_err") {
            kit::blockedState("Field analysis unavailable", errorText_.c_str());
        }
        if (kind == "geometry" || kind == "zebra" || kind == "draft" || kind == "thickness" ||
            kind == "curvature") {
            kit::panelFooterBegin();
            drawExclusiveFooter(st, kind);
        }
        kit::panelEnd();
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
    // Mass material re-seed, design section chain and section plane drag debounce.
    syncMassMaterial(st);
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
