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
#include "layout.h"    // setNextCommandDialogPlacement / evalRibbonDock
#include "metrics.h"
#include "../core/app_state.h"
#include "../renderer.h"
#include "../ui_theme.h"
#include "property_manager.h"   // pmFormatDim / Area / Volume / Mass / AreaMoment

namespace ui {

// Zebra stripe density used by the Zebra Stripes control window. File-scope so it persists across
// frames without adding a member to EvaluateDialogs (the class has a single instance). Matches the
// renderer's default frequency; the slider (4..60) drives it live via Renderer::setZebra().
static float gZebraFreq = 18.0f;

// Fixed 10-color palette for Measure ref chips + Interference body/overlap tints (Contract).
static const float kEvalPalette[10][4] = {
    {0.90f, 0.20f, 0.18f, 1.0f},  // red
    {0.95f, 0.55f, 0.15f, 1.0f},  // orange
    {0.95f, 0.80f, 0.20f, 1.0f},  // amber
    {0.30f, 0.75f, 0.30f, 1.0f},  // green
    {0.18f, 0.78f, 0.72f, 1.0f},  // teal
    {0.20f, 0.65f, 0.90f, 1.0f},  // cyan
    {0.25f, 0.45f, 0.90f, 1.0f},  // blue
    {0.55f, 0.35f, 0.85f, 1.0f},  // violet
    {0.85f, 0.30f, 0.70f, 1.0f},  // magenta
    {0.60f, 0.40f, 0.25f, 1.0f},  // brown
};

// Format a linear value (canonical mm) for an evaluate readout in the active document units.
static std::string evalLen(AppState& st, double mm) { return pmFormatDim(mm, st.ips()); }

// Build the refs array for eval_measure from the current selection's resolved selectors (cap 8).
static nlohmann::json selectionRefs(AppState& st) {
    nlohmann::json refs = nlohmann::json::array();
    for (const auto& it : st.selection.items()) {
        if (it.selector.empty()) continue;
        try {
            refs.push_back(nlohmann::json::parse(it.selector));
        } catch (...) {
        }
        if (refs.size() >= 8) break;
    }
    return refs;
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

// Unit-aware reading text from a measure result (summary line).
static std::string measureReadingText(AppState& st, const nlohmann::json& r) {
    auto numAt = [&](const char* k, double& out) -> bool {
        if (r.contains(k) && r[k].is_number()) {
            out = r[k].get<double>();
            return true;
        }
        return false;
    };
    std::string summary;
    double v = 0;
    if (numAt("distance", v))
        summary = evalLen(st, v);
    else if (numAt("length", v))
        summary = evalLen(st, v);
    else if (numAt("radius", v))
        summary = "R " + evalLen(st, v);
    else if (numAt("area", v))
        summary = pmFormatArea(v, st.ips());
    else if (numAt("volume", v))
        summary = pmFormatVolume(v, st.ips());
    else if (numAt("total_length", v))
        summary = evalLen(st, v);
    else if (numAt("total_distance", v))
        summary = evalLen(st, v);
    else if (numAt("perimeter", v))
        summary = "P " + evalLen(st, v);
    double ang = 0;
    if (numAt("angle", ang)) {
        char b[48];
        std::snprintf(b, sizeof(b), "%.2f°", ang);
        summary = summary.empty() ? std::string(b) : summary + " " + b;
    }
    if (summary.empty()) summary = r.value("kind", std::string("measure"));
    return summary;
}

// Draw a color-chip row: filled rounded rect behind the label text (axis/plane chip language).
static void drawColorChipRow(const char* text, const float rgba[4]) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 ts = ImGui::CalcTextSize(text);
    const float padX = 6.0f * ui::uiScale;
    const float padY = 2.0f * ui::uiScale;
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImVec2 p1(p0.x + ts.x + padX * 2.0f, p0.y + ts.y + padY * 2.0f);
    const float rnd = 4.0f * ui::uiScale;
    ImU32 fill = IM_COL32((int)(rgba[0] * 255), (int)(rgba[1] * 255), (int)(rgba[2] * 255),
                          (int)(0.35f * 255));
    ImU32 bord = IM_COL32((int)(rgba[0] * 255), (int)(rgba[1] * 255), (int)(rgba[2] * 255),
                          (int)(0.90f * 255));
    dl->AddRectFilled(p0, p1, fill, rnd);
    dl->AddRect(p0, p1, bord, rnd, 0, 1.0f * ui::uiScale);
    ImGui::SetCursorScreenPos(ImVec2(p0.x + padX, p0.y + padY));
    ImGui::TextUnformatted(text);
    // Advance layout past the chip.
    ImGui::SetCursorScreenPos(ImVec2(p0.x, p1.y + 2.0f * ui::uiScale));
    ImGui::Dummy(ImVec2(1, 0));
}

static const char* kPlaneNames[] = {"plane.top", "plane.front", "plane.right"};

void EvaluateDialogs::clearOverlays() {
    overlaySegs_.clear();
    overlayLabels_.clear();
    overlayHighlights_.clear();
    overlayCallouts_.clear();
    interferenceView_ = InterferenceView{};
    massBodyAlpha_ = 1.0f;
}

void EvaluateDialogs::clearFieldColormap(AppState& st) {
    st.colormapOn = false;
    st.legendOn = false;
    if (st.renderer) st.renderer->setColormapMode(false);
}

void EvaluateDialogs::seedMassFromDoc(AppState& st) {
    massDocKey_.clear();
    massDocName_ = "(not set)";
    massDensity_ = 7.85f;
    massMaterial_ = 1;  // Steel preset after Document slot
    if (!st.docMirror.hasDoc()) return;
    const auto& tree = st.docMirror.tree();
    if (!tree.is_object()) return;
    auto m = tree.find("material");
    if (m == tree.end() || !m->is_object()) return;
    std::string key = m->value("key", std::string());
    if (key.empty()) return;
    massDocKey_ = key;
    massDocName_ = materialDisplayName(key);
    massDensity_ = materialDensityGcm3(key);
    massMaterial_ = 0;  // Document material row
}

// C39: evals run async on st.rpc — heavy methods (interference, thickness on a dense model)
// used to freeze the window for their full duration. The callback runs on the GL thread
// inside the per-frame drain; gen_ guards against a newer eval superseding a pending one.
void EvaluateDialogs::runEval(AppState& st, const std::string& method,
                              const nlohmann::json& extra) {
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
    const int gen = ++gen_;
    ++st.rebuildsInFlight;
    st.rpc.call(method, params, [this, &st, gen, method](const core::RpcResult& r) {
        --st.rebuildsInFlight;
        if (gen != gen_) return;  // a newer eval replaced this one
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
        result_ = r.result;
        if (openWindow_ == "mass") designSectionsDirty_ = true;
        if (openWindow_ == "measure") rebuildMeasureOverlays(st);
        if (openWindow_ == "mass") rebuildMassOverlays(st);
        if (openWindow_ == "interference") {
            // Seed member checklist from parts[] once (preserve checks on re-run when names match).
            std::vector<std::string> names;
            if (result_.contains("parts") && result_["parts"].is_array()) {
                for (const auto& p : result_["parts"]) {
                    if (!p.is_object()) continue;
                    std::string n = p.value("name", std::string());
                    if (!n.empty()) names.push_back(n);
                }
            }
            if (names != interferencePartNames_) {
                // Rebuild checklist; default all checked.
                interferencePartNames_ = names;
                interferencePartChecked_.assign(names.size(), 1);
            }
            rebuildInterferenceView(st);
        }
    }, 60000);
}

void EvaluateDialogs::runFieldEval(AppState& st, const std::string& method,
                                   const nlohmann::json& extra,
                                   const std::string& legendTitle, const std::string& units) {
    errorText_.clear();
    result_ = nlohmann::json::object();
    pending_ = false;
    title_ = legendTitle;
    if (!st.docMirror.hasDoc()) {
        errorText_ = "No document loaded.";
        openWindow_ = "field_err";
        clearFieldColormap(st);
        return;
    }
    // Assembly / multi-body: refuse colormap mode so the viewport is never corrupted.
    if (fieldEvalUnsupported(st)) {
        errorText_ =
            "Thickness/Draft/Curvature runs on a single part body; open the component to analyze it";
        openWindow_ = "field_err";
        clearFieldColormap(st);
        return;
    }
    nlohmann::json params = {{"doc_id", st.docMirror.docId()}};
    for (auto it = extra.begin(); it != extra.end(); ++it) params[it.key()] = it.value();
    pending_ = true;
    const int gen = ++gen_;
    const std::string fieldDocId = st.docMirror.docId();
    openWindow_ = "field_wait";
    ++st.rebuildsInFlight;
    st.rpc.call(method, params,
        [this, &st, gen, method, legendTitle, units, fieldDocId](const core::RpcResult& r) {
            --st.rebuildsInFlight;
            if (gen != gen_) return;
            if (st.docMirror.docId() != fieldDocId) {
                pending_ = false;
                clearFieldColormap(st);
                return;
            }
            pending_ = false;
            if (!r.ok) {
                errorText_ = r.err.empty() ? ("worker call failed: " + method) : r.err;
                openWindow_ = "field_err";
                clearFieldColormap(st);
                return;
            }
            if (r.result.is_object() && r.result.contains("code")) {
                errorText_ = r.result.value("message", std::string("evaluate error"));
                openWindow_ = "field_err";
                clearFieldColormap(st);
                return;
            }
            result_ = r.result;
            std::string aicm = result_.value("aicm_path", std::string());
            if (aicm.empty()) {
                errorText_ = "no overlay produced";
                openWindow_ = "field_err";
                clearFieldColormap(st);
                return;
            }
            // Guard again at apply time (doc may have switched to multi-body mid-flight).
            if (fieldEvalUnsupported(st)) {
                errorText_ =
                    "Thickness/Draft/Curvature runs on a single part body; open the component to "
                    "analyze it";
                openWindow_ = "field_err";
                clearFieldColormap(st);
                return;
            }
            std::string le;
            if (!st.loadDocMesh(aicm, le)) {
                errorText_ = "overlay load failed: " + le;
                openWindow_ = "field_err";
                clearFieldColormap(st);
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
    else if (result_.contains("points") && result_["points"].is_array()) pts = result_["points"];

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

    // Stable palette colors for chips / highlights (ref order).
    measureRefColors_.clear();
    measureRefColors_.reserve(hits.empty() ? 1 : hits.size());
    const size_t nColor = hits.empty() ? 1 : hits.size();
    for (size_t i = 0; i < nColor; ++i) {
        const float* p = kEvalPalette[i % 10];
        measureRefColors_.push_back({p[0], p[1], p[2], p[3]});
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

        // Callout at anchor with the reading text (one per ref; same reading for multi is fine).
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
        // Per-ref text: single reading, or segment length for multi.
        if (result_.contains("distances") && result_["distances"].is_array() &&
            i < result_["distances"].size() && result_["distances"][i].is_number()) {
            co.text = evalLen(st, result_["distances"][i].get<double>());
        } else {
            co.text = reading;
        }
        if (!co.text.empty()) overlayCallouts_.push_back(std::move(co));
    }
}

void EvaluateDialogs::rebuildMassOverlays(AppState& st) {
    overlaySegs_.clear();
    overlayLabels_.clear();
    // Keep measure-only vectors alone when mass is open.
    if (openWindow_ != "measure") {
        overlayHighlights_.clear();
        overlayCallouts_.clear();
    }
    massBodyAlpha_ = 1.0f;
    if (openWindow_ != "mass") return;

    const bool anyMark = showCom_ || showI_ || showJ_;
    if (anyMark) massBodyAlpha_ = 0.45f;

    if (!result_.contains("com") || !result_["com"].is_array() || result_["com"].size() != 3)
        return;
    const double cx = result_["com"][0].get<double>();
    const double cy = result_["com"][1].get<double>();
    const double cz = result_["com"][2].get<double>();

    // Cross size ~ 5% of a typical part; fall back to 5 mm.
    float arm = 5.0f;
    if (st.renderer) {
        // radius_ is private; use a fixed scale relative to COM magnitude as a soft fallback.
        double r = std::sqrt(cx * cx + cy * cy + cz * cz);
        if (r > 1.0) arm = (float)(r * 0.08);
        if (arm < 2.0f) arm = 2.0f;
        if (arm > 50.0f) arm = 50.0f;
    }

    auto pushSeg = [&](float ax, float ay, float az, float bx, float by, float bz) {
        overlaySegs_.push_back(ax);
        overlaySegs_.push_back(ay);
        overlaySegs_.push_back(az);
        overlaySegs_.push_back(bx);
        overlaySegs_.push_back(by);
        overlaySegs_.push_back(bz);
    };

    if (showCom_) {
        pushSeg(cx - arm, cy, cz, cx + arm, cy, cz);
        pushSeg(cx, cy - arm, cz, cx, cy + arm, cz);
        pushSeg(cx, cy, cz - arm, cx, cy, cz + arm);
        overlayLabels_.push_back({{(float)cx, (float)cy, (float)cz}, "CG"});
    }

    // Principal axes through COM for I/J markers.
    if ((showI_ || showJ_) && result_.contains("principal_axes") &&
        result_["principal_axes"].is_array() && result_["principal_axes"].size() == 3) {
        const auto& Pa = result_["principal_axes"];
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
            // Label with J or I from design section if available.
            std::string lab;
            if (designSections_.is_array() && (size_t)i < designSections_.size() &&
                designSections_[i].is_object()) {
                const auto& sec = designSections_[i];
                if (showJ_ && sec.contains("J") && sec["J"].is_number())
                    lab = "J" + std::to_string(i + 1) + " " +
                          pmFormatAreaMoment(sec["J"].get<double>(), st.ips());
                else if (showI_) {
                    double Ixx = sec.value("Ixx", 0.0), Iyy = sec.value("Iyy", 0.0);
                    double Imax = std::max(Ixx, Iyy);
                    lab = "I" + std::to_string(i + 1) + " " +
                          pmFormatAreaMoment(Imax, st.ips());
                }
            } else if (showJ_) {
                lab = "J" + std::to_string(i + 1);
            } else if (showI_) {
                lab = "I" + std::to_string(i + 1);
            }
            if (!lab.empty())
                overlayLabels_.push_back(
                    {{(float)(cx + nx * L), (float)(cy + ny * L), (float)(cz + nz * L)}, lab});

            // Approximate polar-moment ring in plane ⟂ axis i (8 segments).
            if (showJ_) {
                // Build orthonormal basis u,v ⟂ n
                float ux = 1, uy = 0, uz = 0;
                if (std::fabs(nx) > 0.9f) {
                    ux = 0;
                    uy = 1;
                }
                // u = normalize(cross(n, arbitrary))
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
        pr.overlapRgba[0] = 0.90f;
        pr.overlapRgba[1] = 0.20f;
        pr.overlapRgba[2] = 0.18f;
        pr.overlapRgba[3] = 0.60f;

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
    nlohmann::json refs = selectionRefs(st);
    std::string key = refs.dump();
    if (key == lastMeasureRefs_) return;
    const double now = ImGui::GetTime();
    if (now - lastMeasureFireT_ < 0.05) return;  // 50 ms debounce
    lastMeasureFireT_ = now;
    lastMeasureRefs_ = key;
    if (refs.empty()) {
        result_ = nlohmann::json::object();
        errorText_.clear();
        pending_ = false;
        clearOverlays();
        measureRefColors_.clear();
        return;
    }
    runEval(st, "eval_measure", {{"refs", refs}});
}

void EvaluateDialogs::tickMassDesignSections(AppState& st) {
    if (openWindow_ != "mass") return;
    if (!designSectionsDirty_) return;
    if (pending_ || designSectionsPending_ > 0) return;
    if (!result_.contains("com") || !result_["com"].is_array() || result_["com"].size() != 3)
        return;
    if (!result_.contains("principal_axes") || !result_["principal_axes"].is_array() ||
        result_["principal_axes"].size() != 3)
        return;

    designSectionsDirty_ = false;
    designSections_ = nlohmann::json::array({nullptr, nullptr, nullptr});
    const int batch = ++designSectionGen_;
    designSectionsPending_ = 3;
    const double cx = result_["com"][0].get<double>();
    const double cy = result_["com"][1].get<double>();
    const double cz = result_["com"][2].get<double>();
    const auto Pa = result_["principal_axes"];

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
        ++st.rebuildsInFlight;
        st.rpc.call(
            "eval_section_props", params,
            [this, &st, batch, i](const core::RpcResult& r) {
                --st.rebuildsInFlight;
                if (batch != designSectionGen_) return;
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
                }
                if (designSectionsPending_ <= 0) rebuildMassOverlays(st);
            },
            60000);
    }
}

bool EvaluateDialogs::handleCommand(AppState& st, const std::string& cmdId) {
    if (cmdId == "eval_mass_props") {
        seedMassFromDoc(st);
        designSections_ = nlohmann::json::array({nullptr, nullptr, nullptr});
        designSectionsDirty_ = false;
        runEval(st, "eval_mass", {{"density_g_cm3", (double)massDensity_}});
        openWindow_ = "mass";
        title_ = "Mass Properties";
        return true;
    }
    if (cmdId == "eval_measure") {
        lastMeasureRefs_.clear();
        lastMeasureFireT_ = -1e9;
        measureLoggedGen_ = -1;
        openWindow_ = "measure";
        title_ = "Measure";
        // Immediate first eval if selection present.
        nlohmann::json refs = selectionRefs(st);
        if (!refs.empty()) {
            lastMeasureRefs_ = refs.dump();
            runEval(st, "eval_measure", {{"refs", refs}});
        } else {
            result_ = nlohmann::json::object();
            errorText_.clear();
            pending_ = false;
            clearOverlays();
            measureRefColors_.clear();
        }
        return true;
    }
    if (cmdId == "eval_section_props") {
        sectionPlane_ = 0;
        runEval(st, "eval_section_props", {{"plane", kPlaneNames[0]}});
        openWindow_ = "section";
        title_ = "Section Properties";
        return true;
    }
    if (cmdId == "eval_check" || cmdId == "eval_geometry") {
        runEval(st, "eval_check", nlohmann::json::object());
        openWindow_ = "check";
        title_ = "Geometry Check";
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
        runFieldEval(st, "eval_draft", {{"pull", {0, 0, 1}}, {"angle_deg", 1.0}}, "Draft angle",
                     "deg");
        return true;
    }
    if (cmdId == "eval_thickness") {
        runFieldEval(st, "eval_thickness", nlohmann::json::object(), "Wall thickness",
                     st.ips() ? "in" : "mm");
        return true;
    }
    if (cmdId == "eval_curvature") {
        runFieldEval(st, "eval_curvature", {{"kind", "mean"}}, "Mean curvature", "1/mm");
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

void EvaluateDialogs::draw(AppState& st) {
    if (openWindow_.empty()) {
        if (massBodyAlpha_ < 0.999f || !overlaySegs_.empty() || interferenceView_.active ||
            !overlayHighlights_.empty())
            clearOverlays();
        return;
    }

    // Live measure re-eval + mass design section chain.
    tickLiveMeasure(st);
    tickMassDesignSections(st);
    if (openWindow_ == "mass" && !pending_) rebuildMassOverlays(st);

    bool open = true;

    // Zebra / Symmetry pin into the Evaluate ribbon dead-space strip when available.
    const bool dockWindows = (openWindow_ == "zebra" || openWindow_ == "symmetry");
    const ui::EvalRibbonDock dock = ui::evalRibbonDock();
    if (dockWindows && dock.valid) {
        const float pad = 6.0f * ui::uiScale;
        ImGui::SetNextWindowPos(ImVec2(dock.minX + pad, dock.minY + pad), ImGuiCond_Always);
        ImGui::SetNextWindowSize(
            ImVec2(std::max(80.0f, dock.maxX - dock.minX - 2.0f * pad),
                   std::max(40.0f, dock.maxY - dock.minY - 2.0f * pad)),
            ImGuiCond_Always);
    } else {
        ui::setNextCommandDialogPlacement();
        ImGui::SetNextWindowSize(ImVec2(ui::metrics::px(360), 0), ImGuiCond_Always);
    }

    const auto& gpal = ui::palette;
    const ImVec4 titleTint(gpal.glassBg[0], gpal.glassBg[1], gpal.glassBg[2], gpal.glassBg[3]);
    ImGui::PushStyleColor(ImGuiCol_WindowBg, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_TitleBg, titleTint);
    ImGui::PushStyleColor(ImGuiCol_TitleBgActive, titleTint);
    ImGui::PushStyleColor(ImGuiCol_TitleBgCollapsed, titleTint);
    const bool evVis = ImGui::Begin(title_.c_str(), &open);
    ImGui::PopStyleColor(3);
    if (evVis) {
        const ImVec2 wp = ImGui::GetWindowPos(), ws = ImGui::GetWindowSize();
        const float contentTopY =
            ImGui::GetCursorScreenPos().y - ImGui::GetStyle().WindowPadding.y;
        const ImRect winRect(ImVec2(wp.x, contentTopY), ImVec2(wp.x + ws.x, wp.y + ws.y));
        const ui::AdaptiveSurface& surf =
            ui::adaptivePanel(ui::panelId("host_evaluate_glass"), winRect, ui::GlassGroup::Global);
        ui::glassPanel(ImGui::GetWindowDrawList(), winRect, ui::GlassLevel::Regular, 12.0f, surf);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);

        if (pending_ && openWindow_ != "measure") {
            // Measure stays interactive while computing.
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
            ImGui::TextUnformatted("Computing");
            ImGui::SameLine(0, 0);
            ImGui::TextUnformatted(&"..."[2 - ((int)(ImGui::GetTime() * 3.0) % 3)]);
            ImGui::PopStyleColor();
        } else if (!errorText_.empty() && openWindow_ != "measure" && openWindow_ != "mass" &&
                   openWindow_ != "field_err") {
            ImGui::PushStyleColor(
                ImGuiCol_Text,
                ImVec4(gpal.statusErr[0], gpal.statusErr[1], gpal.statusErr[2], gpal.statusErr[3]));
            ImGui::TextWrapped("%s", errorText_.c_str());
            ImGui::PopStyleColor();
        } else if (openWindow_ == "mass") {
            // ---- Material seed + override ----
            // Combo: 0 = Document material (if any), then Steel…PLA, Custom.
            static const struct {
                const char* name;
                float d;
            } kPresets[] = {
                {"Steel", 7.85f},      {"Stainless", 8.00f}, {"Aluminum", 2.70f},
                {"Titanium", 4.51f},   {"Brass", 8.50f},     {"ABS plastic", 1.05f},
                {"PLA", 1.24f},        {"Custom", 0.0f},
            };
            const int kPresetN = (int)(sizeof(kPresets) / sizeof(kPresets[0]));
            const int kCustom = kPresetN;  // after Document slot: indices 1..kPresetN are presets
            // Build items: [0]=Document, [1..]=presets
            if (!massDocKey_.empty())
                ImGui::Text("Document: %s", massDocName_.c_str());
            else
                ImGui::TextDisabled("Document material: not set");

            ImGui::SetNextItemWidth(180 * ui::uiScale);
            // Simplified combo: Document | presets
            int comboIdx = massMaterial_;
            std::string comboLabel;
            if (comboIdx == 0 && !massDocKey_.empty())
                comboLabel = "Document: " + massDocName_;
            else if (comboIdx >= 1 && comboIdx <= kPresetN)
                comboLabel = kPresets[comboIdx - 1].name;
            else
                comboLabel = "Custom";
            if (ImGui::BeginCombo("Material", comboLabel.c_str())) {
                if (!massDocKey_.empty()) {
                    bool sel = (massMaterial_ == 0);
                    if (ImGui::Selectable(("Document: " + massDocName_).c_str(), sel)) {
                        massMaterial_ = 0;
                        massDensity_ = materialDensityGcm3(massDocKey_);
                        runEval(st, "eval_mass", {{"density_g_cm3", (double)massDensity_}});
                    }
                    if (sel) ImGui::SetItemDefaultFocus();
                }
                for (int i = 0; i < kPresetN; ++i) {
                    int idx = i + 1;
                    bool sel = (massMaterial_ == idx);
                    if (ImGui::Selectable(kPresets[i].name, sel)) {
                        massMaterial_ = idx;
                        if (i != kPresetN - 1) {  // not Custom
                            massDensity_ = kPresets[i].d;
                            runEval(st, "eval_mass",
                                    {{"density_g_cm3", (double)massDensity_}});
                        }
                    }
                    if (sel) ImGui::SetItemDefaultFocus();
                }
                ImGui::EndCombo();
            }
            ImGui::SetNextItemWidth(140 * ui::uiScale);
            if (ImGui::DragFloat("Density", &massDensity_, 0.01f, 0.0f, 25.0f, "%.3f g/cm3",
                                 ImGuiSliderFlags_AlwaysClamp))
                massMaterial_ = kCustom;  // force custom when dragging
            if (ImGui::IsItemDeactivatedAfterEdit())
                runEval(st, "eval_mass", {{"density_g_cm3", (double)massDensity_}});

            if (!errorText_.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(gpal.statusErr[0], gpal.statusErr[1],
                                                           gpal.statusErr[2], gpal.statusErr[3]));
                ImGui::TextWrapped("%s", errorText_.c_str());
                ImGui::PopStyleColor();
            } else if (pending_) {
                ImGui::TextDisabled("Computing…");
            } else {
                ImGui::Separator();
                ImGui::Text("Volume:  %s",
                            pmFormatVolume(result_.value("volume_mm3", 0.0), st.ips()).c_str());
                ImGui::Text("Mass:    %s",
                            pmFormatMass(result_.value("mass_g", 0.0), st.ips()).c_str());
                ImGui::Text("Density: %.3f g/cm3", massDensity_);
                if (result_.contains("com") && result_["com"].is_array() &&
                    result_["com"].size() == 3) {
                    ImGui::SeparatorText("Center of gravity");
                    ImGui::Text("X: %s   Y: %s   Z: %s",
                                evalLen(st, result_["com"][0].get<double>()).c_str(),
                                evalLen(st, result_["com"][1].get<double>()).c_str(),
                                evalLen(st, result_["com"][2].get<double>()).c_str());
                }

                ImGui::SeparatorText("Design properties");
                bool cb = false;
                cb |= ImGui::Checkbox("Show COM in viewport", &showCom_);
                cb |= ImGui::Checkbox("Area moment I (bending)", &showI_);
                cb |= ImGui::Checkbox("Polar moment J (torsion)", &showJ_);
                cb |= ImGui::Checkbox("Axial stiffness k (EA/L est.)", &showK_);
                if (cb) rebuildMassOverlays(st);

                if (designSectionsPending_ > 0)
                    ImGui::TextDisabled("Computing section I/J…");
                if (designSections_.is_array()) {
                    for (int i = 0; i < 3; ++i) {
                        if ((size_t)i >= designSections_.size() || !designSections_[i].is_object())
                            continue;
                        const auto& sec = designSections_[i];
                        ImGui::Separator();
                        ImGui::Text("Principal axis %d", i + 1);
                        double Ixx = sec.value("Ixx", 0.0), Iyy = sec.value("Iyy", 0.0);
                        double J = sec.value("J", Ixx + Iyy);
                        if (showI_) {
                            ImGui::Text("  Imax: %s   Imin: %s",
                                        pmFormatAreaMoment(std::max(Ixx, Iyy), st.ips()).c_str(),
                                        pmFormatAreaMoment(std::min(Ixx, Iyy), st.ips()).c_str());
                        }
                        if (showJ_)
                            ImGui::Text("  J:    %s",
                                        pmFormatAreaMoment(J, st.ips()).c_str());
                        if (showK_ && sec.contains("EA") && sec["EA"].is_number()) {
                            ImGui::TextDisabled("  EA: %.4g N  (divide by length L for k)");
                            ImGui::Text("  EA:  %.4g N", sec["EA"].get<double>());
                        }
                        if (sec.contains("area_mm2"))
                            ImGui::Text("  Section area: %s",
                                        pmFormatArea(sec.value("area_mm2", 0.0), st.ips()).c_str());
                    }
                }

                if (ImGui::TreeNode("Advanced mass inertia")) {
                    if (result_.contains("inertia") && result_["inertia"].is_array() &&
                        result_["inertia"].size() == 9) {
                        ImGui::TextDisabled("Inertia tensor (g·mm², about origin)");
                        const auto& I = result_["inertia"];
                        for (int r = 0; r < 3; ++r)
                            ImGui::Text("%12.2f %12.2f %12.2f", I[r * 3 + 0].get<double>(),
                                        I[r * 3 + 1].get<double>(), I[r * 3 + 2].get<double>());
                    }
                    if (result_.contains("principal_moments") &&
                        result_["principal_moments"].is_array() &&
                        result_["principal_moments"].size() == 3) {
                        ImGui::TextDisabled("Principal mass moments (g·mm²)");
                        const auto& Pm = result_["principal_moments"];
                        for (int i = 0; i < 3; ++i)
                            ImGui::Text("  P%d  %12.2f", i + 1, Pm[i].get<double>());
                    }
                    ImGui::TreePop();
                }
            }
        } else if (openWindow_ == "measure") {
            if (pending_) {
                ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
                ImGui::TextUnformatted("Computing…");
                ImGui::PopStyleColor();
            }
            if (!errorText_.empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(gpal.statusErr[0], gpal.statusErr[1],
                                                           gpal.statusErr[2], gpal.statusErr[3]));
                ImGui::TextWrapped("%s", errorText_.c_str());
                ImGui::PopStyleColor();
            }
            std::string kind = result_.value("kind", std::string("(none)"));
            auto numAt = [&](const char* k, double& out) -> bool {
                if (result_.contains(k) && result_[k].is_number()) {
                    out = result_[k].get<double>();
                    return true;
                }
                return false;
            };
            std::string summary;
            double v;
            if (numAt("distance", v))
                summary = "Distance " + evalLen(st, v);
            else if (numAt("length", v))
                summary = "Length " + evalLen(st, v);
            else if (numAt("radius", v))
                summary =
                    "Radius " + evalLen(st, v) + " (Dia " + evalLen(st, 2.0 * v) + ")";
            else if (numAt("area", v))
                summary = "Area " + pmFormatArea(v, st.ips());
            else if (numAt("volume", v))
                summary = "Volume " + pmFormatVolume(v, st.ips());
            else if (numAt("total_length", v))
                summary = "Path " + evalLen(st, v);
            else if (numAt("total_distance", v))
                summary = "Chain " + evalLen(st, v);
            double ang;
            if (numAt("angle", ang)) {
                char b[48];
                std::snprintf(b, sizeof(b), "Angle %.3f deg", ang);
                summary = summary.empty() ? std::string(b) : summary + ", " + b;
            }
            if (!pending_ && !summary.empty() && gen_ != measureLoggedGen_) {
                measureLoggedGen_ = gen_;
                measureLog_.push_back(summary);
            }

            // Color-coded summary chip + per-ref chips.
            if (!summary.empty() && !measureRefColors_.empty()) {
                drawColorChipRow(summary.c_str(), measureRefColors_[0].data());
            } else if (!summary.empty()) {
                ImGui::TextUnformatted(summary.c_str());
            }
            ImGui::Text("Kind: %s", kind.c_str());

            // One chip per resolved selection ref.
            int ri = 0;
            for (const auto& it : st.selection.items()) {
                if (it.selector.empty() && !it.hit.valid()) continue;
                std::string label;
                if (it.hit.valid()) {
                    const char* kn =
                        it.hit.kind == render::PickKind::Face
                            ? "Face"
                            : (it.hit.kind == render::PickKind::Edge
                                   ? "Edge"
                                   : (it.hit.kind == render::PickKind::Vertex ? "Vertex" : "Elem"));
                    char buf[64];
                    std::snprintf(buf, sizeof(buf), "%s %u · Body%u", kn, it.hit.topo_id,
                                  it.hit.body_id);
                    label = buf;
                } else {
                    label = "Ref " + std::to_string(ri + 1);
                }
                const float* col = kEvalPalette[ri % 10];
                drawColorChipRow(label.c_str(), col);
                ++ri;
                if (ri >= 8) break;
            }

            auto showLen = [&](const char* k) {
                if (result_.contains(k) && result_[k].is_number())
                    ImGui::Text("%s: %s", k, evalLen(st, result_[k].get<double>()).c_str());
            };
            showLen("distance");
            if (result_.contains("angle") && result_["angle"].is_number())
                ImGui::Text("angle: %.4f deg", result_["angle"].get<double>());
            showLen("length");
            showLen("radius");
            showLen("diameter");
            showLen("perimeter");
            showLen("total_length");
            showLen("total_distance");
            if (result_.contains("area") && result_["area"].is_number())
                ImGui::Text("area: %s",
                            pmFormatArea(result_["area"].get<double>(), st.ips()).c_str());
            if (result_.contains("volume") && result_["volume"].is_number())
                ImGui::Text("volume: %s",
                            pmFormatVolume(result_["volume"].get<double>(), st.ips()).c_str());
            if (result_.contains("segment_lengths") && result_["segment_lengths"].is_array()) {
                ImGui::TextUnformatted("Segments:");
                int si = 0;
                for (const auto& s : result_["segment_lengths"]) {
                    if (!s.is_number()) continue;
                    ImGui::BulletText("#%d  %s", ++si, evalLen(st, s.get<double>()).c_str());
                }
            }
            if (result_.contains("closed") && result_["closed"].is_boolean())
                ImGui::Text("Closed profile: %s", result_["closed"].get<bool>() ? "yes" : "no");

            if (kind == "(none)" || result_.empty())
                ImGui::TextDisabled(
                    "Select faces, edges, or vertices. 1 = property; 2 = distance; 3+ = chain.");

            if (!measureLog_.empty()) {
                ImGui::SeparatorText("History");
                if (ImGui::SmallButton("Clear")) {
                    measureLog_.clear();
                    measureLoggedGen_ = -1;
                }
                ImGui::BeginChild("##measlog", ImVec2(0, 120 * ui::uiScale),
                                  ImGuiChildFlags_Border);
                for (size_t i = measureLog_.size(); i-- > 0;)
                    ImGui::TextUnformatted(measureLog_[i].c_str());
                ImGui::EndChild();
            }
        } else if (openWindow_ == "section") {
            ImGui::SetNextItemWidth(160 * ui::uiScale);
            const char* planeItems = "Top\0Front\0Right\0Selected face\0";
            if (ImGui::Combo("Plane", &sectionPlane_, planeItems)) {
                nlohmann::json plane;
                if (sectionPlane_ >= 0 && sectionPlane_ <= 2) {
                    plane = kPlaneNames[sectionPlane_];
                } else {
                    // Selected planar face: first face selector in selection.
                    nlohmann::json refs = selectionRefs(st);
                    plane = "plane.top";
                    for (const auto& r : refs) {
                        if (r.is_object() && r.value("kind", std::string()) == "face") {
                            plane = r;
                            break;
                        }
                    }
                }
                runEval(st, "eval_section_props", {{"plane", plane}});
            }
            if (pending_) {
                ImGui::TextDisabled("Computing…");
            } else if (!errorText_.empty()) {
                ImGui::TextWrapped("%s", errorText_.c_str());
            } else {
                ImGui::Text("Area:     %s",
                            pmFormatArea(result_.value("area_mm2", 0.0), st.ips()).c_str());
                if (result_.contains("centroid") && result_["centroid"].is_array() &&
                    result_["centroid"].size() == 3)
                    ImGui::Text("Centroid: %s, %s, %s",
                                evalLen(st, result_["centroid"][0].get<double>()).c_str(),
                                evalLen(st, result_["centroid"][1].get<double>()).c_str(),
                                evalLen(st, result_["centroid"][2].get<double>()).c_str());
                double Ixx = result_.value("Ixx", 0.0);
                double Iyy = result_.value("Iyy", 0.0);
                double Ixy = result_.value("Ixy", 0.0);
                ImGui::Text("Ixx: %s", pmFormatAreaMoment(Ixx, st.ips()).c_str());
                ImGui::Text("Iyy: %s", pmFormatAreaMoment(Iyy, st.ips()).c_str());
                ImGui::Text("Ixy: %s", pmFormatAreaMoment(Ixy, st.ips()).c_str());
                ImGui::Text("J (Ixx+Iyy): %s",
                            pmFormatAreaMoment(Ixx + Iyy, st.ips()).c_str());
            }
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
            // Member subset multiselect — re-runs with members=[checked...] when toggled.
            if (!interferencePartNames_.empty()) {
                ImGui::SeparatorText("Members");
                bool anyChange = false;
                for (size_t i = 0; i < interferencePartNames_.size(); ++i) {
                    if (i >= interferencePartChecked_.size()) break;
                    bool checked = interferencePartChecked_[i] != 0;
                    if (ImGui::Checkbox(interferencePartNames_[i].c_str(), &checked)) {
                        interferencePartChecked_[i] = checked ? 1 : 0;
                        anyChange = true;
                    }
                }
                if (anyChange && !pending_) {
                    nlohmann::json members = nlohmann::json::array();
                    for (size_t i = 0; i < interferencePartNames_.size(); ++i) {
                        if (i < interferencePartChecked_.size() && interferencePartChecked_[i])
                            members.push_back(interferencePartNames_[i]);
                    }
                    nlohmann::json extra = nlohmann::json::object();
                    if (!members.empty()) extra["members"] = members;
                    runEval(st, "eval_interference", extra);
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
            if (hasPairs) {
                ImGui::SeparatorText("Interferences");
                int pi = 0;
                for (const auto& p : result_["interferences"]) {
                    std::string a = p.value("a", std::string("?"));
                    std::string b = p.value("b", std::string("?"));
                    double vol = p.value("overlap_volume_mm3",
                                         p.value("volume", p.value("volume_mm3", 0.0)));
                    // BBox WxDxH from overlap_bbox [[min],[max]].
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
                    char line[256];
                    if (!bboxTxt.empty())
                        std::snprintf(line, sizeof(line), "%s × %s: overlap %s, bbox %s", a.c_str(),
                                      b.c_str(), pmFormatVolume(vol, st.ips()).c_str(),
                                      bboxTxt.c_str());
                    else
                        std::snprintf(line, sizeof(line), "%s × %s: overlap %s", a.c_str(),
                                      b.c_str(), pmFormatVolume(vol, st.ips()).c_str());
                    drawColorChipRow(line, ca);
                    ++pi;
                }
            }
            if (hasClear) {
                ImGui::SeparatorText("Clearances");
                for (const auto& p : result_["clearances"]) {
                    std::string a = p.value("a", std::string("?"));
                    std::string b = p.value("b", std::string("?"));
                    double d = p.value("distance", 0.0);
                    ImGui::BulletText("%s <-> %s : %s", a.c_str(), b.c_str(),
                                      evalLen(st, d).c_str());
                }
            }
            if (!hasPairs && !hasClear && !result_.contains("summary")) {
                ImGui::TextWrapped(
                    "Interference detection runs across assembly components or multi-body parts. "
                    "Open an assembly or a multi-body part to check clashes.");
            }
            if (result_.value("ok", true) && !hasPairs && errorText_.empty() && !pending_)
                ImGui::TextColored(ImVec4(gpal.statusOk[0], gpal.statusOk[1], gpal.statusOk[2], 1),
                                   "No interference.");
        } else if (openWindow_ == "symmetry") {
            // Compact horizontal layout for ribbon-dock strip.
            ImGui::SetNextItemWidth(100 * ui::uiScale);
            if (ImGui::Combo("Plane", &symmetryPlane_, "Front\0Top\0Right\0")) {
                // Map UI order front/top/right → kPlaneNames front=1, top=0, right=2
                static const int map[] = {1, 0, 2};
                int pi = (symmetryPlane_ >= 0 && symmetryPlane_ < 3) ? map[symmetryPlane_] : 1;
                runEval(st, "eval_symmetry", {{"plane", kPlaneNames[pi]}});
            }
            ImGui::SameLine();
            if (pending_) {
                ImGui::TextDisabled("Computing…");
            } else {
                bool sym = result_.value("symmetric", false);
                ImGui::Text("Symmetric: %s  ·  Dev: %s", sym ? "yes" : "no",
                            pmFormatVolume(result_.value("deviation_mm3", 0.0), st.ips()).c_str());
            }
        } else if (openWindow_ == "zebra") {
            // Compact horizontal layout for ribbon-dock strip.
            ImGui::TextWrapped(
                "Smooth even stripes = fair surface; kinks = G1 break; jogs = G2 break.");
            ImGui::SetNextItemWidth(160 * ui::uiScale);
            if (ImGui::SliderFloat("Density", &gZebraFreq, 4.0f, 60.0f, "%.0f")) {
                if (st.renderer) st.renderer->setZebra(true, gZebraFreq);
            }
            ImGui::SameLine();
            if (ImGui::Button("Turn Off")) {
                if (st.renderer) st.renderer->setZebra(false, gZebraFreq);
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
        ImGui::PopStyleColor();  // default text ink
    }
    ImGui::End();
    ImGui::PopStyleColor();  // ImGuiCol_WindowBg
    if (!open) {
        if (openWindow_ == "zebra" && st.renderer) st.renderer->setZebra(false, gZebraFreq);
        clearOverlays();
        openWindow_.clear();
        if (st.renderer) st.renderer->setBodyAlpha(1.0f);
    }
}

}  // namespace ui
